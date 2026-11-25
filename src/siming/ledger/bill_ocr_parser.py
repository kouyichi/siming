"""微信 / 支付宝 / 抖音账单页面离线 OCR 文本解析器：
- 仅处理带明确账单/支付特征的页面文本（非账单截图零落库、不污染待办）；
- 自动提取「商户 + 整数分金额 + 交易时间 + 消费/退款/划转」；
- 若 ±10 分钟内已有无商户名的银行动账短信（PENDING），自动补齐商户名并升级为「已对账」，不重复记两笔；
- 天然按 (时间, 分值, 商户) 指纹幂等去重，滚动截屏重叠或重复点击绝不重算。
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, datetime, timedelta
from typing import Any, Optional

from siming.crypto import contains_otp, store_encrypted_raw
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local, parse_iso
from siming.ledger.reconcile import match_or_record_wallet_row
from siming.ledger.sms_parser import classify_txn_kind, yuan_to_cents

BILL_PAGE_MARKERS = (
    "微信支付",
    "全部账单",
    "零钱",
    "商户消费",
    "扫二维码付款",
    "支付宝",
    "花呗",
    "余额宝",
    "月账单",
    "账单分类",
    "抖音",
    "抖音支付",
    "抖音月付",
    "全部交易类型",
    "账单管理",
    "账单详情",
    "支付成功",
    "交易成功",
    "当前状态",
    "交易单号",
    "实付",
)

SIGNED_AMOUNT_RE = re.compile(r"(?<![0-9A-Za-z])([+-])\s*[¥￥]?\s*([0-9]{1,7}(?:,[0-9]{3})*\.[0-9]{2})(?![0-9])")
DETAIL_AMOUNT_RE = re.compile(r"(?:实付|支付金额|付款金额|合计)[：:\s]*[¥￥]?\s*([0-9]{1,7}(?:,[0-9]{3})*\.[0-9]{2})(?![0-9])")
SUMMARY_SKIP_RE = re.compile(r"(支出\s*[¥￥]|收入\s*[¥￥]|月支出|月收入|总支出|总收入|可用额度|待还金额|账户余额)")
CARD_LAST4_RE = re.compile(r"(?:\(|（|尾号)(\d{4})(?:\)|）)?")
ORDER_ID_RE = re.compile(r"(?:交易单号|订单号|商家订单号)[：:\s]*([0-9A-Za-z_-]{12,40})")

FULL_DATETIME_RE = re.compile(
    r"(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})日?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?"
)
MD_TIME_RE = re.compile(r"(?<!\d)(\d{1,2})[-月/](\d{1,2})日?\s*(\d{1,2}):(\d{2})")
REL_TIME_RE = re.compile(r"(今天|今日|昨天|昨日)\s*(\d{1,2}):(\d{2})")
ONLY_DATE_RE = re.compile(r"(?:(20\d{2})[-年/.])?(\d{1,2})[-月](\d{1,2})日?")
ONLY_TIME_RE = re.compile(r"(?<!\d)(\d{1,2}):(\d{2})(?::(\d{2}))?(?!\d)")

UI_NOISE_LINES = {
    "账单",
    "全部账单",
    "筛选",
    "统计",
    "月账单",
    "全部交易类型",
    "账单管理",
    "账单详情",
    "搜索",
    "收支明细",
    "已全额退款",
    "有退款",
    "交易成功",
    "支付成功",
    "当前状态",
}


def _detect_platform(page_text: str) -> tuple[str, str]:
    if any(k in page_text for k in ("抖音", "抖音支付", "抖音月付")):
        return "douyin", "抖音账单"
    if any(k in page_text for k in ("支付宝", "花呗", "余额宝", "月账单", "账单分类")):
        return "alipay", "支付宝账单"
    if any(k in page_text for k in ("微信", "零钱", "全部账单", "商户消费", "扫二维码付款")):
        return "wechat", "微信账单"
    if any(k in page_text for k in BILL_PAGE_MARKERS):
        return "wallet", "移动支付账单"
    return "", ""


def _resolve_line_datetime(window_text: str, fallback_date: date, today: date) -> Optional[str]:
    m_full = FULL_DATETIME_RE.search(window_text)
    if m_full:
        try:
            dt = datetime(
                int(m_full.group(1)),
                int(m_full.group(2)),
                int(m_full.group(3)),
                int(m_full.group(4)),
                int(m_full.group(5)),
                int(m_full.group(6) or 0),
                tzinfo=SHANGHAI_TZ,
            )
            return dt.isoformat(timespec="seconds")
        except ValueError:
            pass

    m_rel = REL_TIME_RE.search(window_text)
    if m_rel:
        d = today if m_rel.group(1) in ("今天", "今日") else (today - timedelta(days=1))
        dt = datetime(d.year, d.month, d.day, int(m_rel.group(2)), int(m_rel.group(3)), tzinfo=SHANGHAI_TZ)
        return dt.isoformat(timespec="seconds")

    m_md = MD_TIME_RE.search(window_text)
    if m_md:
        mo, da = int(m_md.group(1)), int(m_md.group(2))
        yr = today.year - 1 if (mo, da) > (today.month, today.day) else today.year
        try:
            dt = datetime(yr, mo, da, int(m_md.group(3)), int(m_md.group(4)), tzinfo=SHANGHAI_TZ)
            return dt.isoformat(timespec="seconds")
        except ValueError:
            pass

    m_t = ONLY_TIME_RE.search(window_text)
    if m_t and 0 <= int(m_t.group(1)) <= 23 and 0 <= int(m_t.group(2)) <= 59:
        dt = datetime(
            fallback_date.year,
            fallback_date.month,
            fallback_date.day,
            int(m_t.group(1)),
            int(m_t.group(2)),
            int(m_t.group(3) or 0),
            tzinfo=SHANGHAI_TZ,
        )
        return dt.isoformat(timespec="seconds")
    return None


def _clean_merchant_candidate(line: str) -> str:
    s = SIGNED_AMOUNT_RE.sub("", line)
    s = FULL_DATETIME_RE.sub("", s)
    s = MD_TIME_RE.sub("", s)
    s = REL_TIME_RE.sub("", s)
    s = re.sub(r"\b\d{1,2}:\d{2}(?::\d{2})?\b", "", s)
    s = s.strip(" ·|-—:：¥￥\t")
    if not s or s in UI_NOISE_LINES:
        return ""
    if re.fullmatch(r"[0-9.,+-]+", s):
        return ""
    if SUMMARY_SKIP_RE.search(s):
        return ""
    return s[:60]


def _try_pair_recent_pending_sms_without_last4(
    db: SimingDB,
    occurred_at: str,
    amount_cents: int,
    merchant: str,
    kind: str,
    wallet_alias: str,
    source: str,
    raw_ref: str,
) -> Optional[dict[str, Any]]:
    """列表页截图通常不含银行卡后四位：若 ±10 分钟内恰好有 1 条同金额无商户名的银行短信 PENDING，直接补齐商户并标记已对账。"""
    if kind != "消费" or not merchant:
        return None
    occ_dt = parse_iso(occurred_at)
    win_start = iso_now(occ_dt - timedelta(minutes=10))
    win_end = iso_now(occ_dt + timedelta(minutes=10))
    fp = f"wallet:{hashlib.sha1(f'{occurred_at}:{amount_cents}:{merchant}'.encode()).hexdigest()[:20]}"
    ts = iso_now()

    with db.transaction() as conn:
        cands = conn.execute(
            """
            SELECT txn_id, merchant FROM ledger_txns
            WHERE status = 'PENDING' AND kind = '消费' AND amount_cents = ?
              AND occurred_at >= ? AND occurred_at <= ?
            """,
            (amount_cents, win_start, win_end),
        ).fetchall()
        if len(cands) != 1:
            return None

        ev_id = db.append_event(
            conn,
            source=source,
            thread_key=f"wallet:{wallet_alias}",
            summary=f"账单OCR配对：{merchant[:40]} ¥{amount_cents/100:.2f}",
            payload={"amount_cents": amount_cents, "merchant": merchant, "kind": kind},
            raw_ref=raw_ref,
            occurred_at=occurred_at,
            fingerprint=fp,
        )
        if ev_id is None:
            return {"duplicate": True, "status": "SKIPPED"}

        matched_id = str(cands[0]["txn_id"])
        conn.execute(
            "UPDATE ledger_txns SET merchant = COALESCE(NULLIF(merchant, ''), ?), status = '已对账', updated_at = ? WHERE txn_id = ?",
            (f"{merchant}（{wallet_alias}）", ts, matched_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO links (txn_id, event_id, role, created_at) VALUES (?, ?, ?, ?)",
            (matched_id, ev_id, source, ts),
        )
        return {"txn_id": matched_id, "status": "已对账"}


def try_ingest_bill_ocr(
    db: SimingDB,
    text: str,
    source: str = "ios_bill_ocr",
    raw_ref: Optional[str] = None,
) -> dict[str, Any]:
    if not text or contains_otp(text):
        return {"matched": False, "ingested": 0, "paired": 0, "skipped_dup": 0}

    pages = [p.strip() for p in re.split(r"===PAGE===", text) if p.strip()]
    today = now_local().date()
    ingested = 0
    paired = 0
    skipped_dup = 0
    recognized_pages = 0

    for page in pages:
        if contains_otp(page):
            continue
        plat, wallet_alias = _detect_platform(page)
        if not plat:
            continue
        recognized_pages += 1
        ref = raw_ref or store_encrypted_raw(page, db.paths)

        lines = [ln.strip() for ln in page.splitlines() if ln.strip()]
        running_date = today
        page_last4 = ""
        m_card = CARD_LAST4_RE.search(page)
        if m_card and ("当前状态" in page or "交易单号" in page or "付款方式" in page or "支付方式" in page):
            page_last4 = m_card.group(1)
        m_oid = ORDER_ID_RE.search(page)
        page_order_id = m_oid.group(1) if m_oid else ""

        page_items: list[tuple[str, int, str, str]] = []
        for idx, ln in enumerate(lines):
            # 更新日期游标（例如分段标题“11月15日”或“昨天”）
            if ln in ("今天", "今日"):
                running_date = today
            elif ln in ("昨天", "昨日"):
                running_date = today - timedelta(days=1)
            else:
                m_d = ONLY_DATE_RE.fullmatch(ln)
                if m_d:
                    yr = int(m_d.group(1)) if m_d.group(1) else today.year
                    try:
                        cand_d = date(yr, int(m_d.group(2)), int(m_d.group(3)))
                        if not m_d.group(1) and cand_d > today:
                            cand_d = date(today.year - 1, cand_d.month, cand_d.day)
                        running_date = cand_d
                    except ValueError:
                        pass

            if SUMMARY_SKIP_RE.search(ln):
                continue

            m_amt = SIGNED_AMOUNT_RE.search(ln)
            sign = ""
            amt_str = ""
            if m_amt:
                sign, amt_str = m_amt.group(1), m_amt.group(2)
            else:
                m_det = DETAIL_AMOUNT_RE.search(ln)
                if m_det:
                    sign, amt_str = "-", m_det.group(1)

            if not amt_str:
                continue
            cents = yuan_to_cents(amt_str)
            if cents == 0:
                continue

            ctx_lines = lines[max(0, idx - 2) : min(len(lines), idx + 3)]
            ctx_joined = "\n".join(ctx_lines)
            occ_iso = _resolve_line_datetime(ctx_joined, running_date, today) or (
                datetime(running_date.year, running_date.month, running_date.day, 12, 0, tzinfo=SHANGHAI_TZ).isoformat(timespec="seconds")
            )

            merchant = _clean_merchant_candidate(ln)
            if not merchant:
                for back in (idx - 1, idx - 2, idx + 1):
                    if 0 <= back < len(lines):
                        cand = _clean_merchant_candidate(lines[back])
                        if cand:
                            merchant = cand
                            break
            if not merchant:
                merchant = f"{wallet_alias}商户"

            kind = "退款" if sign == "+" else classify_txn_kind(f"{merchant} {ctx_joined}")
            if "已全额退款" in ctx_joined and sign == "-":
                continue
            page_items.append((occ_iso, cents, merchant, kind))

        for occ_iso, cents, merchant, kind in page_items:
            oid = page_order_id if len(page_items) == 1 else ""
            if not page_last4:
                paired_res = _try_pair_recent_pending_sms_without_last4(
                    db=db,
                    occurred_at=occ_iso,
                    amount_cents=cents,
                    merchant=merchant,
                    kind=kind,
                    wallet_alias=wallet_alias,
                    source=f"ocr_{plat}",
                    raw_ref=ref,
                )
                if paired_res is not None:
                    if paired_res.get("duplicate"):
                        skipped_dup += 1
                    else:
                        ingested += 1
                        paired += 1
                    continue

            res = match_or_record_wallet_row(
                db=db,
                occurred_at=occ_iso,
                amount_cents=cents,
                card_last4=page_last4,
                wallet_alias=wallet_alias,
                merchant=merchant,
                kind=kind,
                channel_order_id=oid,
                source=f"ocr_{plat}",
            )
            if res.get("duplicate"):
                skipped_dup += 1
            else:
                ingested += 1
                if res.get("status") == "已对账" and page_last4:
                    paired += 1

    return {
        "matched": recognized_pages > 0,
        "recognized_pages": recognized_pages,
        "ingested": ingested,
        "paired": paired,
        "skipped_dup": skipped_dup,
    }
