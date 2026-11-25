"""信用卡/借记卡电子月结单解析与节前提前 2 个工作日还款预警。"""

from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Any, Optional

from bs4 import BeautifulSoup

from siming.cn_calendar import day_info, subtract_workdays
from siming.db import SimingDB, iso_now
from siming.ledger.reconcile import match_or_record_statement_row
from siming.ledger.sms_parser import classify_txn_kind, resolve_account_alias, yuan_to_cents

DUE_DATE_RE = re.compile(
    r"(?:到期还款日|最后还款日|本期还款日)[：:\s]*(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})"
)
BILL_AMOUNT_RE = re.compile(
    r"(?:本期应还金额|本期账单金额|应还款额)[：:\s]*(?:人民币|RMB|CNY|[¥￥])?\s*([0-9,]+\.[0-9]{2})"
)
STMT_LINE_RE = re.compile(
    r"(20\d{2}[-/]\d{2}[-/]\d{2})\s+([^\t\n¥￥0-9]{2,32}?)\s+[¥￥]?(-?[0-9,]+\.[0-9]{2})(?:\s+(\d{4}))?"
)


def _html_to_lines(raw: str) -> str:
    if "<table" not in raw.lower() and "<html" not in raw.lower():
        return raw
    soup = BeautifulSoup(raw, "html.parser")
    rows_out: list[str] = []
    for tr in soup.find_all("tr"):
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
        if cells:
            rows_out.append("  ".join(c for c in cells if c))
    plain = soup.get_text("\n", strip=True)
    return plain + "\n" + "\n".join(rows_out)


def compute_repayment_remind_date(due: date, db: SimingDB) -> tuple[date, bool]:
    """还款日若撞上节假日或紧贴长假，提前 2 个中国实际工作日预警；否则提前 2 天提醒。"""
    holiday_near = (not day_info(due, db.paths).is_workday) or (
        not day_info(due - timedelta(days=1), db.paths).is_workday
    )
    if holiday_near:
        return subtract_workdays(due, 2, paths=db.paths), True
    return due - timedelta(days=2), False


def try_ingest_statement_text(
    db: SimingDB,
    subject: str,
    content: str,
    event_id: Optional[str] = None,
    default_last4: str = "",
) -> dict[str, Any]:
    text = _html_to_lines(content)
    alias = resolve_account_alias(subject, default_last4, db)

    m_due = DUE_DATE_RE.search(text)
    m_bill = BILL_AMOUNT_RE.search(text)
    created_case_id = ""

    if m_due:
        due_d = date(int(m_due.group(1)), int(m_due.group(2)), int(m_due.group(3)))
        remind_d, shifted_for_holiday = compute_repayment_remind_date(due_d, db)
        bill_str = f"¥{m_bill.group(1)}" if m_bill else "金额见原单"
        ts = iso_now()

        with db.transaction() as conn:
            created_case_id = db.next_seq_id(conn, "S")
            note_holiday = f"（撞休息日已提前至工作日 {remind_d.isoformat()} 预警）" if shifted_for_holiday else ""
            conn.execute(
                """
                INSERT INTO cases (
                    case_id, title, next_step, due_at, done_rule, status, strength,
                    category, rule_id, evidence_ids, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '还款入账短信或回「已还」', '待处理', 'HARD', 'credit_bill', 'credit_due', ?, ?, ?)
                """,
                (
                    created_case_id,
                    f"{alias} 账单待还 {bill_str}",
                    f"到期还款日 {due_d.isoformat()}{note_holiday}",
                    due_d.isoformat(),
                    json.dumps([event_id] if event_id else []),
                    ts,
                    ts,
                ),
            )
            # 硬期限同步到 81 预排发件箱做断电兜底（不含消费明细敏感原文）
            fire_iso = f"{remind_d.isoformat()}T09:30:00+08:00"
            conn.execute(
                """
                INSERT INTO outbox (
                    id, ref_id, rule_id, text, send_at, status, channel, hard_deadline, edge_synced, created_at
                ) VALUES (?, ?, 'credit_due', ?, ?, 'PRESCHEDULED', 'qqbot', 1, 0, ?)
                """,
                (
                    f"OB-{created_case_id}",
                    created_case_id,
                    f"[{created_case_id}] 信用卡还款提醒：{alias} 到期还款日为 {due_d.isoformat()}。",
                    fire_iso,
                    ts,
                ),
            )
            db.enqueue_effect(
                conn,
                "edge_sync_outbox",
                {"outbox_id": f"OB-{created_case_id}", "ref_id": created_case_id},
            )

    parsed_rows = 0
    for m_line in STMT_LINE_RE.finditer(text):
        d_raw, merchant_raw, amt_raw, l4_raw = (
            m_line.group(1).replace("/", "-"),
            m_line.group(2).strip(),
            m_line.group(3),
            m_line.group(4) or default_last4,
        )
        cents = yuan_to_cents(amt_raw.lstrip("-"))
        kind = "退款" if amt_raw.startswith("-") else classify_txn_kind(merchant_raw)
        row_alias = resolve_account_alias(subject, l4_raw, db)
        match_or_record_statement_row(
            db=db,
            occurred_date=d_raw,
            amount_cents=cents,
            account_alias=row_alias,
            card_last4=l4_raw,
            merchant=merchant_raw,
            kind=kind,
            event_id=event_id,
            row_index=parsed_rows,
        )
        parsed_rows += 1

    return {
        "matched": bool(m_due or parsed_rows),
        "case_id": created_case_id,
        "parsed_rows": parsed_rows,
    }
