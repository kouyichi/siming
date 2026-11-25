"""S2-D：报销游标卡与候选批次防重证据审计器（100% 只读核验，绝不自动凑 $4,000 或触碰 OA 提交）。
核心防呆规则：
1. 识别 Stripe 门户收据号（2xxx-xxxx）与 Gmail 付款收据号（1xxx-xxxx）同笔双号映射；
2. 按「经济付款身份（币种 + 整数分金额 + UTC 付款时间 ±6s）」交叉拦截同笔重复（覆盖尚未进配对表的新票）；
3. 汇总历史批次占用状态（CW08 已提 / CW01 关联 / 退回可重用），输出不可变 SHA-256 审计 manifest；
4. 收到光大 1002/1001 还款或中行 1003 大额公事付款短信时，自动更新单张「报销游标卡」。
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional

from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local
from siming.ledger.reimburse import summarize_reimbursement_bundle

DEFAULT_REIMBURSE_RECORDS_ROOT = Path.home() / "Documents" / "报销记录"
_RECEIPT_PAIR_RE = re.compile(r"\b([12]\d{3}-\d{4})\b")
_RECEIPT_BARE8_RE = re.compile(r"\b([12]\d{7})\b")
_CN_INVOICE_20_RE = re.compile(r"(?<!\d)(\d{20})(?!\d)")
_CW08_RE = re.compile(r"(CW08-FYYS-\d{12})")
_CW01_RE = re.compile(r"(CW01-GRBX-\d{12})")


def _normalize_receipt_no(raw: str) -> str:
    s = (raw or "").strip()
    if not s:
        return ""
    if re.fullmatch(r"[12]\d{7}", s):
        return f"{s[:4]}-{s[4:]}"
    return s


def _to_cents(raw: Any) -> int:
    s = str(raw or "").strip()
    if not s:
        return 0
    s = re.sub(r"^(?:CNY|RMB|USD|[¥￥$])\s*", "", s, flags=re.IGNORECASE).replace(",", "")
    try:
        dec = (Decimal(s) * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP)
        return int(dec)
    except Exception:
        return 0


def _parse_utc_epoch(raw: str, assume_beijing: bool = False) -> Optional[int]:
    s = (raw or "").strip()
    if not s:
        return None
    s = re.sub(r"付款$", "", s).strip()
    tz_default = SHANGHAI_TZ if assume_beijing else timezone.utc

    # 1. YYYY-MM-DD HH:MM:SS 或 YYYY-MM-DDTHH:MM:SS
    for cand in (s, s.replace(" ", "T")):
        try:
            dt = datetime.fromisoformat(cand.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=tz_default)
            return int(dt.timestamp())
        except ValueError:
            pass

    # 2. Gmail / Stripe 英文时间格式："November 11, 2026 at 9:41:11 AM UTC" / "November 12, 2026"
    cleaned_en = s.replace(" UTC", "").replace(" at ", ", ")
    for fmt in ("%B %d, %Y, %I:%M:%S %p", "%b %d, %Y, %I:%M:%S %p", "%B %d, %Y", "%b %d, %Y"):
        try:
            dt = datetime.strptime(cleaned_en, fmt).replace(tzinfo=timezone.utc)
            return int(dt.timestamp())
        except ValueError:
            continue

    return None


@dataclass
class HistoricalPayment:
    payment_key: str
    currency: str = "USD"
    amount_cents: int = 0
    utc_epoch: Optional[int] = None
    portal_no: str = ""
    gmail_no: str = ""
    invoice_no: str = ""
    wallet_uuid: str = ""
    cw08_batches: set[str] = field(default_factory=set)
    cw01_batches: set[str] = field(default_factory=set)
    returned_reusable: bool = False
    sources: list[str] = field(default_factory=list)

    @property
    def is_occupied(self) -> bool:
        if self.returned_reusable and not self.cw08_batches and not self.cw01_batches:
            return False
        return bool(self.cw08_batches or self.cw01_batches)

    @property
    def occupation_label(self) -> str:
        parts: list[str] = []
        if self.cw08_batches:
            parts.append("CW08:" + "/".join(sorted(self.cw08_batches)))
        if self.cw01_batches:
            parts.append("CW01:" + "/".join(sorted(self.cw01_batches)))
        return ", ".join(parts) if parts else "未占用"


class ReimbursementLedgerIndex:
    """只读历史报销索引：支持双号等价合并与 (currency, amount_cents, utc_epoch ±6s) 时间窗防重。"""

    def __init__(self) -> None:
        self.payments: dict[str, HistoricalPayment] = {}
        self.alias_to_key: dict[str, str] = {}
        self.latest_batch_id: str = ""
        self.latest_portal_used: str = ""
        self.next_portal_cursor: str = ""
        self.unused_repayments: list[str] = []
        self.unsubmitted_notes: list[str] = []

    def _register_aliases(self, rec: HistoricalPayment) -> None:
        for tok in (rec.portal_no, rec.gmail_no, rec.invoice_no, rec.wallet_uuid):
            if not tok:
                continue
            self.alias_to_key[tok] = rec.payment_key
            bare = tok.replace("-", "")
            if bare:
                self.alias_to_key[bare] = rec.payment_key

    def find_matching_payment(
        self,
        portal_no: str = "",
        gmail_no: str = "",
        invoice_no: str = "",
        wallet_uuid: str = "",
        currency: str = "USD",
        amount_cents: int = 0,
        utc_epoch: Optional[int] = None,
        tolerance_sec: int = 6,
    ) -> Optional[tuple[HistoricalPayment, str]]:
        """返回 (命中的历史记录, 命中原因: exact_id / twin_alias / time_amount_window)。"""
        for label, val in (
            ("portal_no", _normalize_receipt_no(portal_no)),
            ("gmail_no", _normalize_receipt_no(gmail_no)),
            ("invoice_no", invoice_no.strip()),
            ("wallet_uuid", wallet_uuid.strip()),
        ):
            if not val:
                continue
            pkey = self.alias_to_key.get(val) or self.alias_to_key.get(val.replace("-", ""))
            if pkey and pkey in self.payments:
                rec = self.payments[pkey]
                # 判断是否为 1xxx <-> 2xxx 跨套号命中
                is_twin = (
                    (val.startswith("1") and rec.portal_no and val != rec.portal_no)
                    or (val.startswith("2") and rec.gmail_no and val != rec.gmail_no and not rec.portal_no)
                )
                return (rec, "twin_alias" if is_twin else f"exact_{label}")

        if utc_epoch is not None and amount_cents > 0:
            curr = currency.upper()
            window_hits: list[tuple[int, HistoricalPayment]] = []
            for rec in self.payments.values():
                # 若双方均带同类强身份号（门户 2xxx 或 Gmail 1xxx）且不相等，严禁按 ±6s 硬并两条不同付款！
                if portal_no and rec.portal_no and _normalize_receipt_no(portal_no) != rec.portal_no:
                    continue
                if gmail_no and rec.gmail_no and _normalize_receipt_no(gmail_no) != rec.gmail_no:
                    continue
                if invoice_no and rec.invoice_no and invoice_no.strip() != rec.invoice_no:
                    continue
                if rec.currency == curr and rec.amount_cents == amount_cents and rec.utc_epoch is not None:
                    diff = abs(rec.utc_epoch - utc_epoch)
                    if diff <= tolerance_sec:
                        window_hits.append((diff, rec))
            if len(window_hits) == 1:
                best_diff, best_rec = window_hits[0]
                return (best_rec, f"payment_time_window(±{best_diff}s)")
            if len(window_hits) >= 2:
                window_hits.sort(key=lambda x: x[0])
                return (window_hits[0][1], "ambiguous_time_window")
        return None

    def upsert_record(
        self,
        portal_no: str = "",
        gmail_no: str = "",
        invoice_no: str = "",
        wallet_uuid: str = "",
        currency: str = "USD",
        amount_cents: int = 0,
        utc_epoch: Optional[int] = None,
        cw08: str = "",
        cw01: str = "",
        returned_reusable: bool = False,
        source_ref: str = "",
    ) -> HistoricalPayment:
        p_norm = _normalize_receipt_no(portal_no)
        g_norm = _normalize_receipt_no(gmail_no)
        inv_norm = invoice_no.strip()
        uuid_norm = wallet_uuid.strip()
        curr = (currency or "USD").upper()

        hit = self.find_matching_payment(
            portal_no=p_norm,
            gmail_no=g_norm,
            invoice_no=inv_norm,
            wallet_uuid=uuid_norm,
            currency=curr,
            amount_cents=amount_cents,
            utc_epoch=utc_epoch,
        )
        if hit is not None and hit[1] != "ambiguous_time_window":
            rec = hit[0]
            if p_norm and not rec.portal_no:
                rec.portal_no = p_norm
            if g_norm and not rec.gmail_no:
                rec.gmail_no = g_norm
            if inv_norm and not rec.invoice_no:
                rec.invoice_no = inv_norm
            if uuid_norm and not rec.wallet_uuid:
                rec.wallet_uuid = uuid_norm
            if amount_cents > 0 and rec.amount_cents == 0:
                rec.amount_cents = amount_cents
                rec.currency = curr
            if utc_epoch is not None and rec.utc_epoch is None:
                rec.utc_epoch = utc_epoch
        else:
            pkey = p_norm or g_norm or inv_norm or uuid_norm or f"{curr}:{amount_cents}:{utc_epoch or 0}"
            rec = HistoricalPayment(
                payment_key=pkey,
                currency=curr,
                amount_cents=amount_cents,
                utc_epoch=utc_epoch,
                portal_no=p_norm,
                gmail_no=g_norm,
                invoice_no=inv_norm,
                wallet_uuid=uuid_norm,
            )
            self.payments[pkey] = rec

        if cw08:
            for m in _CW08_RE.findall(cw08):
                rec.cw08_batches.add(m)
            if not _CW08_RE.search(cw08) and cw08.strip():
                rec.cw08_batches.add(cw08.strip())
            rec.returned_reusable = False
        if cw01:
            for m in _CW01_RE.findall(cw01):
                rec.cw01_batches.add(m)
            if not _CW01_RE.search(cw01) and cw01.strip():
                rec.cw01_batches.add(cw01.strip())
        if returned_reusable and not rec.cw08_batches:
            rec.returned_reusable = True
        if source_ref and source_ref not in rec.sources:
            rec.sources.append(source_ref)

        self._register_aliases(rec)
        return rec


def load_reimbursement_index(
    db: SimingDB,
    records_root: Optional[Path] = None,
    shared_memory_md: Optional[Path] = None,
) -> ReimbursementLedgerIndex:
    root = records_root or Path(
        os.environ.get("SIMING_REIMBURSE_RECORDS_DIR") or DEFAULT_REIMBURSE_RECORDS_ROOT
    )
    mem_md = shared_memory_md or (db.paths.shared_memory_dir / "nous-reimbursement-batches.md")
    idx = ReimbursementLedgerIndex()

    # 1. 加载 593 张门户 ↔ Gmail 双号配对基线表
    pair_csv = root / "Nous" / "门户收据配对-20261112" / "配对表.csv"
    if pair_csv.is_file():
        with pair_csv.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                p_no = str(row.get("门户收据号") or "").strip()
                g_no = str(row.get("Gmail收据号") or "").strip()
                inv_no = str(row.get("账单号") or "").strip()
                w_uuid = str(row.get("票夹uuid") or "").strip()
                cents = _to_cents(row.get("金额USD") or "0")
                utc_ep = _parse_utc_epoch(
                    str(row.get("付款时间UTC(门户)") or row.get("Gmail付款时间") or ""),
                    assume_beijing=False,
                )
                cw08 = str(row.get("CW08") or "").strip()
                cw01 = str(row.get("CW01") or "").strip()
                status_col = str(row.get("状态") or "") + " " + str(row.get("票夹") or "")
                returned = "退回" in status_col or "换出" in status_col
                idx.upsert_record(
                    portal_no=p_no,
                    gmail_no=g_no,
                    invoice_no=inv_no,
                    wallet_uuid=w_uuid,
                    currency="USD",
                    amount_cents=cents,
                    utc_epoch=utc_ep,
                    cw08=cw08,
                    cw01=cw01,
                    returned_reusable=returned,
                    source_ref="配对表.csv",
                )

    # 2. 加载早期 403 张台账与总台账（若存在）
    early_csv = root / "Nous" / "发票台账-403张.csv"
    if early_csv.is_file():
        try:
            with early_csv.open("r", encoding="utf-8-sig", newline="") as fh:
                for row in csv.DictReader(fh):
                    r_no = str(row.get("收据号") or row.get("receipt_no") or "").strip()
                    batch_s = str(row.get("批次") or row.get("CW08") or row.get("状态") or "").strip()
                    if not r_no:
                        continue
                    is_used = any(k in batch_s for k in ("CW08", "第一批", "第二批", "第三批", "第四批", "第五批", "已报", "已提"))
                    idx.upsert_record(
                        portal_no=r_no if r_no.startswith("2") else "",
                        gmail_no=r_no if r_no.startswith("1") else "",
                        invoice_no=r_no if not r_no[:1].isdigit() else "",
                        currency="USD",
                        amount_cents=_to_cents(row.get("金额") or row.get("USD") or "0"),
                        cw08=batch_s if is_used else "",
                        source_ref="发票台账-403张.csv",
                    )
        except Exception:
            pass

    # 3. 逐个加载 `批次-YYYYMMDD-CW08-.../发票明细.csv`（含第 11、12 批等最新实提批次）
    if root.is_dir():
        batch_dirs = sorted(p for p in root.glob("批次-*") if p.is_dir())
        for bdir in batch_dirs:
            m_cw = _CW08_RE.search(bdir.name)
            m_cw01 = _CW01_RE.search(bdir.name)
            batch_id = m_cw.group(1) if m_cw else (m_cw01.group(1) if m_cw01 else bdir.name)
            if m_cw:
                idx.latest_batch_id = batch_id
            cw01_csv = bdir / "CW01报销明细汇总.csv"
            if cw01_csv.is_file():
                with cw01_csv.open("r", encoding="utf-8-sig", newline="") as fh01:
                    for r01 in csv.DictReader(fh01):
                        inv01 = str(r01.get("发票号码") or "").strip()
                        amt01 = _to_cents(r01.get("金额_元") or "0")
                        if inv01:
                            idx.upsert_record(
                                invoice_no=inv01,
                                currency="CNY",
                                amount_cents=amt01,
                                cw01=batch_id,
                                source_ref=f"{bdir.name}/CW01报销明细汇总.csv",
                            )
            detail_csv = bdir / "发票明细.csv"
            if detail_csv.is_file():
                with detail_csv.open("r", encoding="utf-8-sig", newline="") as fh:
                    for row in csv.DictReader(fh):
                        p_no = str(row.get("门户收据号") or "").strip()
                        raw_no = str(row.get("收据号") or "").strip()
                        inv_no = str(row.get("账单号") or "").strip()
                        w_uuid = str(row.get("票夹uuid") or "").strip()

                        # 区分门户 2xxx、Gmail 1xxx 与国内 20 位全电发票号
                        g_no = ""
                        if not p_no and raw_no:
                            norm_r = _normalize_receipt_no(raw_no)
                            if re.fullmatch(r"2\d{3}-\d{4}", norm_r):
                                p_no = norm_r
                            elif re.fullmatch(r"1\d{3}-\d{4}", norm_r):
                                g_no = norm_r
                            else:
                                inv_no = raw_no
                        elif p_no and _CN_INVOICE_20_RE.fullmatch(p_no):
                            inv_no = p_no
                            p_no = ""

                        raw_curr = str(row.get("币种") or "").strip().upper()
                        raw_amt = str(row.get("原币金额") or row.get("USD") or "").strip()
                        if raw_amt.upper().startswith(("CNY", "RMB", "¥")):
                            curr = "CNY"
                        elif raw_curr:
                            curr = raw_curr
                        else:
                            curr = "USD"
                        cents = _to_cents(raw_amt)

                        utc_str = str(row.get("付款时间UTC") or "").strip()
                        bj_str = str(row.get("付款时间(北京)") or row.get("付款时间北京") or "").strip()
                        if utc_str:
                            utc_ep = _parse_utc_epoch(utc_str, assume_beijing=False)
                        elif bj_str and len(bj_str) >= 16:
                            bj_full = bj_str if bj_str.startswith("20") else f"{bdir.name[3:7]}-{bj_str}"
                            utc_ep = _parse_utc_epoch(bj_full, assume_beijing=True)
                        else:
                            utc_ep = None

                        idx.upsert_record(
                            portal_no=p_no,
                            gmail_no=g_no,
                            invoice_no=inv_no,
                            wallet_uuid=w_uuid,
                            currency=curr,
                            amount_cents=cents,
                            utc_epoch=utc_ep,
                            cw08=batch_id,
                            source_ref=f"{bdir.name}/发票明细.csv",
                        )
                        if p_no:
                            idx.latest_portal_used = p_no

            readme_p = bdir / "README.md"
            if readme_p.is_file():
                rm_txt = readme_p.read_text(encoding="utf-8", errors="replace")
                m_not_in = re.search(r"不在本单[：:]\s*([^\n]+)", rm_txt)
                if m_not_in:
                    idx.unsubmitted_notes = [m_not_in.group(1).strip()]
                    m_first_p = _RECEIPT_PAIR_RE.search(m_not_in.group(1))
                    if m_first_p:
                        idx.next_portal_cursor = m_first_p.group(1)

    # 4. 读取共享记忆与最新状态核对提取下一批起点与未用还款笔
    if mem_md.is_file():
        mem_txt = mem_md.read_text(encoding="utf-8", errors="replace")
        m_cursors = re.findall(r"下一批\s*Nous\s*从(?:门户\s*)?([12]\d{3}-\d{4})", mem_txt)
        if m_cursors:
            idx.next_portal_cursor = m_cursors[-1]
        for m_rep in re.finditer(r"(\d{1,2}-\d{1,2}[^\n。；]{0,24}[¥￥][0-9,]+(?:\.[0-9]{2})?[^\n。；]{0,20}还没用)", mem_txt):
            val = m_rep.group(1).strip()
            if val not in idx.unused_repayments:
                idx.unused_repayments.append(val)

    if root.is_dir():
        status_mds = sorted(root.glob("状态核对-*.md"))
        if status_mds:
            st_txt = status_mds[-1].read_text(encoding="utf-8", errors="replace")
            m_unused = re.search(r"没用掉的还款[：:]\s*([^\n]+)", st_txt)
            if m_unused:
                idx.unused_repayments = [m_unused.group(1).strip()]

    # 5. 应用退回可重用（RETURNED_REUSABLE）批次覆盖：被 OA 退回且未被其他有效批次占用的票据释放占用
    raw_ret = db.get_meta("reimburse_returned_batches_json")
    if raw_ret:
        try:
            ret_batches = set(json.loads(raw_ret) or [])
        except Exception:
            ret_batches = set()
        if ret_batches:
            for rec in idx.payments.values():
                had_ret = bool((rec.cw08_batches & ret_batches) or (rec.cw01_batches & ret_batches))
                if had_ret:
                    rec.cw08_batches -= ret_batches
                    rec.cw01_batches -= ret_batches
                    if not rec.cw08_batches and not rec.cw01_batches:
                        rec.returned_reusable = True

    return idx


def get_reimbursement_cursor_status(
    db: SimingDB,
    records_root: Optional[Path] = None,
    shared_memory_md: Optional[Path] = None,
) -> dict[str, Any]:
    idx = load_reimbursement_index(db, records_root=records_root, shared_memory_md=shared_memory_md)
    staged_bundle = summarize_reimbursement_bundle(db)

    # 查询本地账本中近 14 天的光大还款卡/信用卡划转或中行大额支出
    cutoff = (now_local() - timedelta(days=14)).date().isoformat()
    conn = db.connect()
    try:
        recent_repayments = [
            dict(r)
            for r in conn.execute(
                """
                SELECT txn_id, occurred_at, amount_cents, currency, account_alias, card_last4, kind, merchant
                FROM ledger_txns
                WHERE occurred_at >= ?
                  AND ((card_last4 IN ('1002', '1001') AND kind = '划转' AND ((currency = 'CNY' AND amount_cents >= 20000) OR (currency != 'CNY' AND amount_cents >= 5000)))
                       OR (currency = 'CNY' AND card_last4 = '1003' AND kind = '消费' AND amount_cents >= 100000))
                ORDER BY occurred_at DESC LIMIT 6
                """,
                (cutoff,),
            ).fetchall()
        ]
    finally:
        conn.close()

    occupied_cnt = sum(1 for r in idx.payments.values() if r.is_occupied)
    return {
        "ok": True,
        "latest_batch_id": idx.latest_batch_id,
        "latest_portal_used": idx.latest_portal_used,
        "next_portal_cursor": idx.next_portal_cursor,
        "unused_repayments": idx.unused_repayments,
        "unsubmitted_notes": idx.unsubmitted_notes,
        "indexed_payments_total": len(idx.payments),
        "occupied_payments_count": occupied_cnt,
        "staged_local_invoices": staged_bundle,
        "recent_bank_repayments": recent_repayments,
    }


def format_reimbursement_cursor(
    db: SimingDB,
    records_root: Optional[Path] = None,
    shared_memory_md: Optional[Path] = None,
) -> str:
    st = get_reimbursement_cursor_status(db, records_root=records_root, shared_memory_md=shared_memory_md)
    lines = [
        "🧾 报销游标与待归集盘点（只读审计，不代点 OA 提交）",
        f"• 最新已提批次：{st['latest_batch_id'] or '未记录'}"
        + (f"（截止门户收据 `{st['latest_portal_used']}`）" if st["latest_portal_used"] else ""),
        f"• 下一批 Nous 起点：门户收据 `{st['next_portal_cursor'] or '待核对'}` 起（历史库已索引 {st['indexed_payments_total']} 笔，已占用防重 {st['occupied_payments_count']} 笔）",
    ]
    if st["unused_repayments"]:
        lines.append("• 历史未核销还款笔：" + "；".join(st["unused_repayments"]))
    if st["unsubmitted_notes"]:
        lines.append("• 待归集候选池备注：" + "；".join(st["unsubmitted_notes"]))
    sb = st["staged_local_invoices"]
    if sb.get("count", 0) > 0:
        f_desc = "、".join(
            f"{it['invoice_no'] or it['file']}(¥{it['amount_yuan']:.2f})" for it in sb["files"][:4]
        )
        lines.append(f"• 本月邮箱自动暂存国内发票：{sb['count']} 张（合计 ¥{sb['total_yuan']:.2f}：{f_desc}）")
    if st["recent_bank_repayments"]:
        r_desc = "；".join(
            f"{r['occurred_at'][:10]} {r['account_alias']} {r['kind']} {'$' if str(r.get('currency') or 'CNY').upper() == 'USD' else '¥'}{r['amount_cents']/100:.2f}"
            for r in st["recent_bank_repayments"][:3]
        )
        lines.append(f"• 近 14 天关联卡动账：{r_desc}")
    lines.append("提示：选好候选收据后可跑 `siming ledger audit-batch <目录或CSV>` 做双号/同笔防重核验。")
    return "\n".join(lines)


def upsert_reimbursement_cursor_case(
    db: SimingDB,
    trigger_txn: Optional[dict[str, Any]] = None,
    records_root: Optional[Path] = None,
    shared_memory_md: Optional[Path] = None,
) -> dict[str, Any]:
    """当收到光大 1002/1001 还款或中行 1003 大额付款短信时，自动创建或刷新唯一活跃的「报销游标卡」。"""
    st = get_reimbursement_cursor_status(db, records_root=records_root, shared_memory_md=shared_memory_md)
    cursor_no = st["next_portal_cursor"] or "待核对"
    sb = st["staged_local_invoices"]
    ts = iso_now()

    trig_desc = ""
    if trigger_txn:
        amt_y = int(trigger_txn.get("amount_cents") or 0) / 100.0
        t_curr = str(trigger_txn.get("currency") or "CNY").upper()
        t_sym = "$" if t_curr == "USD" else ("¥" if t_curr == "CNY" else f"{t_curr} ")
        trig_desc = f"{trigger_txn.get('account_alias', '银行卡')}{trigger_txn.get('kind', '动账')} {t_sym}{amt_y:,.2f}"

    title = f"报销游标待接续：{trig_desc}" if trig_desc else "报销游标待接续"
    next_step = (
        f"Nous 门户从 `{cursor_no}` 起接续"
        + (f"；未用还款 {'; '.join(st['unused_repayments'])}" if st["unused_repayments"] else "")
        + (f"；邮箱已暂存发票 {sb['count']} 张(¥{sb['total_yuan']:.2f})" if sb.get("count") else "")
    )

    with db.transaction() as conn:
        existing = conn.execute(
            "SELECT case_id FROM cases WHERE category='reimburse_cursor' AND status IN ('待处理', '待核实') ORDER BY updated_at DESC LIMIT 1"
        ).fetchone()
        if existing:
            case_id = str(existing["case_id"])
            conn.execute(
                """
                UPDATE cases
                SET title=?, next_step=?, context_json=?, state_version=state_version+1, updated_at=?
                WHERE case_id=?
                """,
                (
                    title[:120],
                    next_step[:280],
                    json.dumps({"trigger": trigger_txn, "cursor": cursor_no}, ensure_ascii=False),
                    ts,
                    case_id,
                ),
            )
        else:
            case_id = db.next_seq_id(conn, "S")
            conn.execute(
                """
                INSERT INTO cases (
                    case_id, title, next_step, done_rule, status, strength,
                    category, rule_id, context_json, created_at, updated_at
                ) VALUES (?, ?, ?, '完成当批 CW08 提交或回「已办」', '待处理', 'NORMAL', 'reimburse_cursor', 'reimburse_cursor', ?, ?, ?)
                """,
                (
                    case_id,
                    title[:120],
                    next_step[:280],
                    json.dumps({"trigger": trigger_txn, "cursor": cursor_no}, ensure_ascii=False),
                    ts,
                    ts,
                ),
            )

        from siming.config import load_policy

        push_rt = bool((load_policy(db.paths).get("ledger_alerts") or {}).get("realtime_reimburse_cursor_push", False))
        if trigger_txn and push_rt:
            msg = (
                f"🧾 [{case_id}] 检测到还款/公事付款入账：{trig_desc}\n"
                f"• 下一批 Nous 游标起点：门户 `{cursor_no}`（最新已提 `{st['latest_batch_id'] or '-'}`）\n"
                + (f"• 历史未用还款：{'；'.join(st['unused_repayments'])}\n" if st["unused_repayments"] else "")
                + f"• 本月已暂存国内电子发票：{sb.get('count', 0)} 张（¥{sb.get('total_yuan', 0.0):.2f}）\n"
                "组包后跑 `siming ledger audit-batch <目录>` 可自动拦截 1xxx/2xxx 双号与前批已用票。"
            )
            db.enqueue_effect(
                conn,
                "send_qq",
                {"ref_id": case_id, "rule_id": "reimburse_cursor", "trial": False, "text": msg},
            )

    return {"ok": True, "case_id": case_id, "next_portal_cursor": cursor_no}


_STRIPE_REC_TEXT_RE = re.compile(r"Receipt\s+number\s*([12]\d{3})[-\s]+(\d{4})", re.IGNORECASE)
_STRIPE_INV_TEXT_RE = re.compile(r"Invoice\s+number\s*([A-Z0-9]{6,12})[-\s]+(\d{4})", re.IGNORECASE)
_STRIPE_DATE_PAID_RE = re.compile(
    r"Date\s+paid\s*([A-Za-z]+\s+\d{1,2},\s+20\d{2}(?:\s+at\s+\d{1,2}:\d{2}:\d{2}\s*[AP]M(?:\s*UTC)?)?)",
    re.IGNORECASE,
)
_STRIPE_USD_PAID_RE = re.compile(r"(?:US)?\$\s*([0-9,]+(?:\.[0-9]{2})?)\s+paid", re.IGNORECASE)


def _load_sibling_stripe_meta(pdf_dir: Path) -> dict[str, dict[str, Any]]:
    """若同目录或父目录存在门户抓取生成的 meta-*.jsonl，提取账单号 -> 精确秒级 paid_at 映射。"""
    out: dict[str, dict[str, Any]] = {}
    search_dirs = [pdf_dir]
    if pdf_dir.parent != pdf_dir:
        search_dirs.append(pdf_dir.parent)
    for d in search_dirs:
        for mp in sorted(d.glob("meta-*.jsonl")):
            try:
                for line in mp.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not line.strip().startswith("{"):
                        continue
                    obj = json.loads(line)
                    num = str(obj.get("number") or "").strip()
                    if num:
                        out[num] = obj
            except Exception:
                continue
    return out


def _extract_single_pdf_candidate(
    pdf: Path, stripe_meta_map: Optional[dict[str, dict[str, Any]]] = None
) -> dict[str, Any]:
    """同时结合文件名、PDF 正文（兼容 Stripe 空格断字及国内数电票/滴滴行程单）与同目录 meta-*.jsonl 提取票据身份。"""
    from pypdf import PdfReader
    from siming.ledger.reimburse import classify_pdf_document

    name = pdf.name
    file_sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
    m_pair = _RECEIPT_PAIR_RE.findall(name)
    portal_no = next((x for x in m_pair if x.startswith("2")), "")
    gmail_no = next((x for x in m_pair if x.startswith("1")), "")
    m_cn20 = _CN_INVOICE_20_RE.search(name)
    inv_no = m_cn20.group(1) if m_cn20 else ""

    m_usd = re.search(r"USD\s*(-?[0-9]+(?:\.[0-9]{1,2})?)", name, re.IGNORECASE)
    m_cny = re.search(r"(?:CNY|RMB|[¥￥])\s*(-?[0-9]+(?:\.[0-9]{1,2})?)", name, re.IGNORECASE)
    curr = "CNY" if (m_cny or inv_no) and not m_usd else "USD"
    cents = _to_cents(m_usd.group(1) if m_usd else (m_cny.group(1) if m_cny else "0"))

    m_ts = re.search(r"(20\d{2})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})", name)
    utc_ep: Optional[int] = None
    if m_ts:
        utc_ep = _parse_utc_epoch(
            f"{m_ts.group(1)}-{m_ts.group(2)}-{m_ts.group(3)} {m_ts.group(4)}:{m_ts.group(5)}:{m_ts.group(6)}"
        )
    else:
        m_ymd = re.search(r"(20\d{2}-\d{2}-\d{2})", name)
        if m_ymd and (portal_no or gmail_no):
            utc_ep = _parse_utc_epoch(f"{m_ymd.group(1)} 00:00:00")

    doc_kind = "invoice"
    category = ""
    issue_date = ""

    # 若文件名未完整包含金额/时间戳/票号，读取 PDF 前两页正文补齐
    if cents == 0 or utc_ep is None or not (portal_no or gmail_no or inv_no):
        txt = ""
        try:
            reader = PdfReader(str(pdf))
            txt = "\n".join((pg.extract_text() or "") for pg in reader.pages[:2])
        except Exception:
            txt = ""

        if txt:
            m_sr = _STRIPE_REC_TEXT_RE.search(txt)
            if m_sr:
                r_num = f"{m_sr.group(1)}-{m_sr.group(2)}"
                if r_num.startswith("2") and not portal_no:
                    portal_no = r_num
                elif r_num.startswith("1") and not gmail_no:
                    gmail_no = r_num
            m_si = _STRIPE_INV_TEXT_RE.search(txt)
            if m_si and not inv_no:
                inv_no = f"{m_si.group(1)}-{m_si.group(2)}"

            if inv_no and stripe_meta_map and inv_no in stripe_meta_map:
                sm = stripe_meta_map[inv_no]
                if isinstance(sm.get("paid_at"), (int, float)) and sm["paid_at"] > 0:
                    utc_ep = int(sm["paid_at"])
                if cents == 0 and sm.get("amount_paid"):
                    cents = int(sm["amount_paid"])
                    curr = str(sm.get("currency") or "USD").upper()

            m_su = _STRIPE_USD_PAID_RE.search(txt)
            if m_su and cents == 0:
                cents = _to_cents(m_su.group(1))
                curr = "USD"
            m_sd = _STRIPE_DATE_PAID_RE.search(txt)
            if m_sd and utc_ep is None:
                utc_ep = _parse_utc_epoch(m_sd.group(1))

            # 若非 Stripe 收据，按国内数电发票 / 滴滴行程报销单解析
            if not (portal_no or gmail_no or m_si or m_su):
                cn_meta = classify_pdf_document(pdf)
                doc_kind = str(cn_meta.get("doc_kind") or "invoice")
                category = str(cn_meta.get("category") or "")
                issue_date = str(cn_meta.get("issue_date") or "")
                if cn_meta.get("invoice_no") and not inv_no:
                    inv_no = str(cn_meta["invoice_no"])
                if cn_meta.get("amount_cents") and cents == 0:
                    cents = int(cn_meta["amount_cents"])
                    curr = "CNY"

    return {
        "label": name,
        "file_path": str(pdf),
        "file_sha256": file_sha,
        "doc_kind": doc_kind,
        "category": category,
        "issue_date": issue_date,
        "portal_no": portal_no,
        "gmail_no": gmail_no,
        "invoice_no": inv_no,
        "currency": curr,
        "amount_cents": cents,
        "utc_epoch": utc_ep,
    }


def _parse_candidate_inputs(target: str | Path | list[dict[str, Any]]) -> list[dict[str, Any]]:
    """支持传入候选字典列表、CSV/JSON/PDF 文件路径、PDF 目录路径或逗号分隔的收据号串。"""
    if isinstance(target, list):
        return [dict(x) for x in target]

    p = Path(str(target)).expanduser()
    candidates: list[dict[str, Any]] = []

    if p.is_dir():
        s_meta = _load_sibling_stripe_meta(p)
        for pdf in sorted(p.glob("*.pdf")):
            cand = _extract_single_pdf_candidate(pdf, stripe_meta_map=s_meta)
            # 行程报销单属于辅助行程附件，不与对应电子发票重复作为报销票据计分
            if cand.get("doc_kind") == "itinerary":
                continue
            candidates.append(cand)
        return candidates

    if p.is_file() and p.suffix.lower() == ".pdf":
        s_meta = _load_sibling_stripe_meta(p.parent)
        cand = _extract_single_pdf_candidate(p, stripe_meta_map=s_meta)
        return [] if cand.get("doc_kind") == "itinerary" else [cand]

    if p.is_file():
        if p.suffix.lower() == ".json":
            raw_j = json.loads(p.read_text(encoding="utf-8"))
            items = raw_j.get("items") if isinstance(raw_j, dict) else raw_j
            return [dict(x) for x in (items or [])]
        with p.open("r", encoding="utf-8-sig", newline="") as fh:
            for row in csv.DictReader(fh):
                p_no = str(row.get("门户收据号") or row.get("portal_no") or "").strip()
                g_no = str(row.get("Gmail收据号") or row.get("gmail_no") or "").strip()
                raw_no = str(row.get("收据号") or row.get("receipt_no") or row.get("发票号码") or "").strip()
                inv_no = str(row.get("账单号") or row.get("invoice_no") or "").strip()
                if raw_no and not p_no and not g_no:
                    norm_r = _normalize_receipt_no(raw_no)
                    if norm_r.startswith("2") and len(norm_r) == 9:
                        p_no = norm_r
                    elif norm_r.startswith("1") and len(norm_r) == 9:
                        g_no = norm_r
                    else:
                        inv_no = raw_no
                elif p_no and _CN_INVOICE_20_RE.fullmatch(p_no):
                    inv_no = p_no
                    p_no = ""

                raw_amt = str(
                    row.get("原币金额") or row.get("金额USD") or row.get("USD") or row.get("amount") or "0"
                ).strip()
                raw_curr = str(row.get("币种") or row.get("currency") or "").strip().upper()
                curr = "CNY" if (raw_amt.upper().startswith(("CNY", "RMB", "¥")) or raw_curr == "CNY") else (raw_curr or "USD")
                cents = _to_cents(raw_amt)
                utc_raw = str(row.get("付款时间UTC") or row.get("付款时间UTC(门户)") or row.get("utc_time") or "").strip()
                bj_raw = str(row.get("付款时间(北京)") or row.get("付款时间北京") or "").strip()
                utc_ep = (
                    _parse_utc_epoch(utc_raw, assume_beijing=False)
                    if utc_raw
                    else (_parse_utc_epoch(bj_raw, assume_beijing=True) if bj_raw else None)
                )
                candidates.append(
                    {
                        "label": p_no or g_no or inv_no or "row",
                        "portal_no": p_no,
                        "gmail_no": g_no,
                        "invoice_no": inv_no,
                        "wallet_uuid": str(row.get("票夹uuid") or "").strip(),
                        "currency": curr,
                        "amount_cents": cents,
                        "utc_epoch": utc_ep,
                    }
                )
        return candidates

    # 逗号或空白分隔的收据号列表（如 "1496-2036,2805-0102"）
    for tok in re.split(r"[,\s;，；]+", str(target).strip()):
        if not tok:
            continue
        norm = _normalize_receipt_no(tok)
        if re.fullmatch(r"2\d{3}-\d{4}", norm):
            candidates.append({"label": tok, "portal_no": norm, "gmail_no": "", "invoice_no": "", "currency": "USD", "amount_cents": 0, "utc_epoch": None})
        elif re.fullmatch(r"1\d{3}-\d{4}", norm):
            candidates.append({"label": tok, "portal_no": "", "gmail_no": norm, "invoice_no": "", "currency": "USD", "amount_cents": 0, "utc_epoch": None})
        else:
            candidates.append({"label": tok, "portal_no": "", "gmail_no": "", "invoice_no": tok, "currency": "CNY" if _CN_INVOICE_20_RE.fullmatch(tok) else "USD", "amount_cents": 0, "utc_epoch": None})
    return candidates


def audit_candidate_batch(
    db: SimingDB,
    target: str | Path | list[dict[str, Any]],
    repayment_usd: Optional[float] = None,
    repayment_cny: Optional[float] = None,
    records_root: Optional[Path] = None,
    shared_memory_md: Optional[Path] = None,
    write_manifest: bool = True,
) -> dict[str, Any]:
    """对候选收据包做确定性防重审计（拦截前批已用、1xxx/2xxx 双号同笔、包内重复、$0 废票与币种混算）。"""
    db.init_db()
    idx = load_reimbursement_index(db, records_root=records_root, shared_memory_md=shared_memory_md)
    candidates = _parse_candidate_inputs(target)

    passed_items: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    unknown_items: list[dict[str, Any]] = []

    seen_batch_identities: list[dict[str, Any]] = []
    totals_by_currency_cents: dict[str, int] = {}

    for pos, cand in enumerate(candidates, start=1):
        p_no = _normalize_receipt_no(str(cand.get("portal_no") or ""))
        g_no = _normalize_receipt_no(str(cand.get("gmail_no") or ""))
        inv_no = str(cand.get("invoice_no") or "").strip()
        w_uuid = str(cand.get("wallet_uuid") or "").strip()
        curr = str(cand.get("currency") or "USD").upper()
        cents = int(cand.get("amount_cents") or 0)
        utc_ep = cand.get("utc_epoch")
        if isinstance(utc_ep, str):
            utc_ep = _parse_utc_epoch(utc_ep)
        label = str(cand.get("label") or p_no or g_no or inv_no or f"#{pos}")

        hit = idx.find_matching_payment(
            portal_no=p_no,
            gmail_no=g_no,
            invoice_no=inv_no,
            wallet_uuid=w_uuid,
            currency=curr,
            amount_cents=cents,
            utc_epoch=utc_ep,
        )
        resolved_key = ""
        match_mode = "unindexed"
        if hit is not None:
            hist_rec, match_mode = hit
            resolved_key = hist_rec.payment_key
            if cents == 0 and hist_rec.amount_cents > 0:
                cents = hist_rec.amount_cents
                curr = hist_rec.currency
            if utc_ep is None and hist_rec.utc_epoch is not None:
                utc_ep = hist_rec.utc_epoch
            if not p_no and hist_rec.portal_no:
                p_no = hist_rec.portal_no
            if not g_no and hist_rec.gmail_no:
                g_no = hist_rec.gmail_no
            if not inv_no and hist_rec.invoice_no:
                inv_no = hist_rec.invoice_no
            if not w_uuid and hist_rec.wallet_uuid:
                w_uuid = hist_rec.wallet_uuid

            # 1. 历史批次已占用拦截（含 Gmail 1xxx 撞上已提门户 2xxx 双号！）
            if hist_rec.is_occupied:
                conflicts.append(
                    {
                        "index": pos,
                        "label": label,
                        "reason": "CONFLICT_ALREADY_USED",
                        "match_mode": match_mode,
                        "portal_no": p_no,
                        "gmail_no": g_no,
                        "occupied_by": hist_rec.occupation_label,
                        "detail": f"已在 {hist_rec.occupation_label} 中使用（命中方式: {match_mode}，门户={p_no or '-'} / Gmail={g_no or '-'}）",
                    }
                )
                continue

        # 2. 候选包内部重复拦截（含同一包同时塞入 1xxx 与对应 2xxx，或时间窗 ≤6s 同金额）
        intra_dup: Optional[dict[str, Any]] = None
        for prev in seen_batch_identities:
            same_key = bool(resolved_key and prev["resolved_key"] == resolved_key)
            same_no = bool(
                (p_no and p_no == prev["portal_no"])
                or (g_no and g_no == prev["gmail_no"])
                or (inv_no and inv_no == prev["invoice_no"])
            )
            same_time_amt = bool(
                cents > 0
                and curr == prev["currency"]
                and cents == prev["amount_cents"]
                and utc_ep is not None
                and prev["utc_epoch"] is not None
                and abs(int(utc_ep) - int(prev["utc_epoch"])) <= 6
            )
            if same_key or same_no or same_time_amt:
                intra_dup = prev
                break

        if intra_dup is not None:
            conflicts.append(
                {
                    "index": pos,
                    "label": label,
                    "reason": "CONFLICT_INTRA_BATCH_DUPLICATE",
                    "duplicate_of_index": intra_dup["index"],
                    "portal_no": p_no,
                    "gmail_no": g_no,
                    "detail": f"与当前候选包第 {intra_dup['index']} 项（{intra_dup['label']}）指向同一笔付款",
                }
            )
            continue

        # 0. 负额退款/冲正单严禁混入报销支出
        if cents < 0:
            conflicts.append(
                {
                    "index": pos,
                    "label": label,
                    "reason": "CONFLICT_NEGATIVE_AMOUNT",
                    "detail": f"负额或退款单（{curr} {cents/100:.2f}），不可作为报销支出提交",
                }
            )
            continue

        # 3. 零金额废票拦截（如 2689-1717 USD0.00）
        if hit is not None and cents == 0:
            conflicts.append(
                {
                    "index": pos,
                    "label": label,
                    "reason": "CONFLICT_ZERO_AMOUNT",
                    "detail": "金额为 0.00 的空账单收据，不可纳入报销",
                }
            )
            continue

        has_strong_id = bool(p_no or g_no or inv_no or w_uuid)
        entry = {
            "index": pos,
            "label": label,
            "file_path": str(cand.get("file_path") or ""),
            "file_sha256": str(cand.get("file_sha256") or ""),
            "resolved_key": resolved_key,
            "match_mode": match_mode,
            "portal_no": p_no,
            "gmail_no": g_no,
            "invoice_no": inv_no,
            "wallet_uuid": w_uuid,
            "category": str(cand.get("category") or ""),
            "issue_date": str(cand.get("issue_date") or ""),
            "currency": curr,
            "amount_cents": cents,
            "utc_epoch": utc_ep,
        }
        seen_batch_identities.append(entry)

        # 未命中历史配对表时：缺票号身份、缺金额、或外币 Nous 收据缺付款时间戳均归入 unknown 待复核（绝不盲目放行 PASS！）
        needs_manual_verify = (
            match_mode == "ambiguous_time_window"
            or not has_strong_id
            or cents <= 0
            or (hit is None and curr == "USD" and utc_ep is None)
        )
        if needs_manual_verify:
            unknown_items.append(
                {
                    **entry,
                    "detail": "缺少完整票据号/正金额/UTC付款时间，或存在多笔近距同额歧义，需补齐配对证据后再审",
                }
            )
        else:
            passed_items.append(entry)
            totals_by_currency_cents[curr] = totals_by_currency_cents.get(curr, 0) + cents

    repayment_check: dict[str, Any] = {}
    repayment_mismatch = False
    if repayment_usd is not None:
        rep_usd_cents = _to_cents(repayment_usd)
        cand_usd_cents = totals_by_currency_cents.get("USD", 0)
        diff_cents = cand_usd_cents - rep_usd_cents
        repayment_check["repayment_usd"] = round(rep_usd_cents / 100.0, 2)
        repayment_check["candidates_usd"] = round(cand_usd_cents / 100.0, 2)
        repayment_check["diff_usd"] = round(diff_cents / 100.0, 2)
        repayment_check["exact_match"] = diff_cents == 0
        if diff_cents != 0:
            repayment_mismatch = True
    if repayment_cny is not None:
        rep_cny_cents = _to_cents(repayment_cny)
        repayment_check["repayment_cny"] = round(rep_cny_cents / 100.0, 2)
        if totals_by_currency_cents.get("CNY", 0) > 0 and not totals_by_currency_cents.get("USD", 0):
            cny_diff = totals_by_currency_cents.get("CNY", 0) - rep_cny_cents
            repayment_check["candidates_cny"] = round(totals_by_currency_cents.get("CNY", 0) / 100.0, 2)
            repayment_check["diff_cny"] = round(cny_diff / 100.0, 2)
            if cny_diff != 0:
                repayment_mismatch = True

    ok_audit = (
        len(candidates) > 0
        and len(passed_items) > 0
        and len(conflicts) == 0
        and len(unknown_items) == 0
        and not repayment_mismatch
    )
    manifest_payload = {
        "generated_at": iso_now(),
        "ok": ok_audit,
        "total_candidates": len(candidates),
        "passed_count": len(passed_items),
        "conflict_count": len(conflicts),
        "unknown_count": len(unknown_items),
        "totals_by_currency": {k: round(v / 100.0, 2) for k, v in sorted(totals_by_currency_cents.items())},
        "repayment_check": repayment_check,
        "passed_items": passed_items,
        "conflicts": conflicts,
        "unknown_items": unknown_items,
    }
    canonical = json.dumps(manifest_payload, ensure_ascii=False, sort_keys=True)
    manifest_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    manifest_payload["manifest_sha256"] = manifest_sha256

    manifest_path_str = ""
    if write_manifest:
        mdir = db.paths.reimburse_dir / "manifests"
        mdir.mkdir(parents=True, exist_ok=True)
        os.chmod(mdir, 0o700)
        mpath = mdir / f"audit-{now_local().strftime('%Y%m%d-%H%M%S')}-{manifest_sha256[:8]}.json"
        mpath.write_text(json.dumps(manifest_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.chmod(mpath, 0o600)
        manifest_path_str = str(mpath)
        manifest_payload["manifest_path"] = manifest_path_str
        db.record_human_use("reimburse_audit", "audit_batch", ref_id=manifest_sha256[:12])
        db.write_audit(
            "A0",
            "reimburse_audit_batch",
            {"total": len(candidates), "passed": len(passed_items), "conflicts": len(conflicts)},
            target_id=manifest_sha256[:12],
        )

    return manifest_payload


def format_audit_batch_report(rep: dict[str, Any]) -> str:
    badge = "✅ 全部通过防重核验" if rep["ok"] else "❌ 发现防重冲突或待补证据项"
    totals_s = "，".join(f"{k} {v:,.2f}" for k, v in (rep.get("totals_by_currency") or {}).items()) or "0.00"
    lines = [
        f"=== 报销候选包只读审计报告（{badge}）===",
        f"• 候选总数：{rep['total_candidates']} ｜ 通过：{rep['passed_count']} ｜ 冲突拦截：{rep['conflict_count']} ｜ 待补信息：{rep['unknown_count']}",
        f"• 通过项分币种合计（严禁跨币种硬加）：{totals_s}",
    ]
    rc = rep.get("repayment_check") or {}
    if "repayment_usd" in rc:
        match_s = "吻合" if rc.get("exact_match") else f"差额 USD {rc.get('diff_usd'):+.2f}"
        lines.append(f"• 对比购汇还款额：候选 USD {rc['candidates_usd']:,.2f} vs 还款 USD {rc['repayment_usd']:,.2f}（{match_s}）")
    for c in rep.get("conflicts") or []:
        lines.append(f"  [冲突 #{c['index']}] {c['label']} -> {c['detail']}")
    for u in rep.get("unknown_items") or []:
        lines.append(f"  [待补 #{u['index']}] {u['label']} -> {u['detail']}")
    if rep.get("manifest_path"):
        lines.append(f"• 不可变审计 Manifest：{rep['manifest_path']} (sha256={rep['manifest_sha256'][:12]})")
    return "\n".join(lines)


def _normalize_nous_att_filename(entry: dict[str, Any], orig_name: str) -> str:
    """将门户原始 Receipt-2xxx-xxxx.pdf 统一规整为 发票-YYYY-MM-DD-2xxx-xxxx-USD100.00.pdf。"""
    if orig_name.startswith("发票-") and orig_name.lower().endswith(".pdf"):
        return orig_name
    rec_no = entry.get("portal_no") or entry.get("gmail_no") or ""
    utc_ep = entry.get("utc_epoch")
    cents = int(entry.get("amount_cents") or 0)
    if rec_no and utc_ep is not None and cents > 0 and entry.get("currency") == "USD":
        ymd = datetime.fromtimestamp(int(utc_ep), tz=timezone.utc).strftime("%Y-%m-%d")
        return f"发票-{ymd}-{rec_no}-USD{cents / 100:.2f}.pdf"
    return re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", orig_name)


def pack_reimbursement_draft(
    db: SimingDB,
    targets: str | Path | list[str | Path],
    label: str = "",
    repayment_usd: Optional[float] = None,
    repayment_cny: Optional[float] = None,
    render_png: bool = True,
    records_root: Optional[Path] = None,
    shared_memory_md: Optional[Path] = None,
) -> dict[str, Any]:
    """S3-2（决策 2=A）：显式选票草稿拼包器。
    - 仅存储在 ~/.local/share/siming/reimburse/drafts/（0700），状态为 DRAFT_READY，绝不提前占用报销游标；
    - 先过严格防重审计（冲突或待补证据立即拦截），通过后拷贝 PDF、转 200-DPI PNG、输出 7 列 发票明细.csv + 国内票据明细.csv + 附件清单.txt。
    """
    import shutil
    import subprocess
    from siming.ledger.reimburse import classify_pdf_document

    db.init_db()
    target_list = [targets] if isinstance(targets, (str, Path)) else list(targets)
    if not target_list:
        return {"ok": False, "status": "INVALID_INPUT", "error": "请至少指定一个待拼包的收据目录或 PDF 文件"}

    raw_pdfs: list[Path] = []
    for tgt in target_list:
        p = Path(str(tgt)).expanduser()
        if p.is_dir():
            raw_pdfs.extend(sorted(p.glob("*.pdf")))
        elif p.is_file() and p.suffix.lower() == ".pdf":
            raw_pdfs.append(p)
        else:
            return {"ok": False, "status": "INVALID_INPUT", "error": f"路径不存在或不是 PDF/目录：{p}"}

    if not raw_pdfs:
        return {"ok": False, "status": "EMPTY_INPUT", "error": "指定路径下未找到任何 PDF 票据文件"}

    # 按文件字节 SHA-256 先去重同文件，分离行程报销单与主发票候选
    seen_sha: set[str] = set()
    candidates: list[dict[str, Any]] = []
    itinerary_docs: list[dict[str, Any]] = []
    stripe_meta_cache: dict[Path, dict[str, dict[str, Any]]] = {}

    for pdf in raw_pdfs:
        sha = hashlib.sha256(pdf.read_bytes()).hexdigest()
        if sha in seen_sha:
            continue
        seen_sha.add(sha)
        parent_d = pdf.parent
        if parent_d not in stripe_meta_cache:
            stripe_meta_cache[parent_d] = _load_sibling_stripe_meta(parent_d)
        cand = _extract_single_pdf_candidate(pdf, stripe_meta_map=stripe_meta_cache[parent_d])
        if cand.get("doc_kind") == "itinerary":
            itin_info = classify_pdf_document(pdf)
            itin_info["file_sha256"] = sha
            itinerary_docs.append(itin_info)
        else:
            candidates.append(cand)

    if not candidates:
        return {"ok": False, "status": "NO_INVOICES", "error": "仅包含行程单附件，缺少主电子发票或收据 PDF"}

    audit = audit_candidate_batch(
        db,
        target=candidates,
        repayment_usd=repayment_usd,
        repayment_cny=repayment_cny,
        records_root=records_root,
        shared_memory_md=shared_memory_md,
        write_manifest=True,
    )
    if not audit["ok"]:
        return {
            "ok": False,
            "status": "CONFLICT_BLOCKED",
            "error": "候选票据未通过防重/完整性核验，已拒绝生成草稿包",
            "audit": audit,
        }

    stamp = now_local().strftime("%Y%m%d-%H%M%S")
    safe_lbl = re.sub(r"[^\w\-\u4e00-\u9fff]+", "-", label.strip()).strip("-")[:24] if label else ""
    draft_id = f"draft-{stamp}" + (f"-{safe_lbl}" if safe_lbl else "")
    drafts_root = db.paths.reimburse_dir / "drafts"
    drafts_root.mkdir(parents=True, exist_ok=True)
    os.chmod(drafts_root, 0o700)

    draft_dir = drafts_root / draft_id
    att_dir = draft_dir / "att"
    png_dir = draft_dir / "png"
    att_dir.mkdir(parents=True, exist_ok=True)
    png_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(draft_dir, 0o700)

    passed_sorted = sorted(
        audit["passed_items"],
        key=lambda x: (int(x["utc_epoch"]) if x.get("utc_epoch") is not None else 0, x.get("portal_no") or x.get("invoice_no") or ""),
    )

    usd_rows: list[dict[str, str]] = []
    cny_rows: list[dict[str, str]] = []
    att_filenames: list[str] = []
    unused_itins = list(itinerary_docs)

    for item in passed_sorted:
        src_p = Path(item["file_path"])
        curr = item["currency"]
        cents = int(item["amount_cents"])
        if curr == "USD":
            att_name = _normalize_nous_att_filename(item, src_p.name)
            dst_p = att_dir / att_name
            shutil.copy2(src_p, dst_p)
            os.chmod(dst_p, 0o600)
            att_filenames.append(att_name)

            utc_ep = int(item["utc_epoch"]) if item.get("utc_epoch") is not None else 0
            utc_dt = datetime.fromtimestamp(utc_ep, tz=timezone.utc) if utc_ep else None
            bj_dt = utc_dt.astimezone(SHANGHAI_TZ) if utc_dt else None
            usd_rows.append(
                {
                    "门户收据号": item["portal_no"] or item["gmail_no"],
                    "账单号": item["invoice_no"],
                    "付款时间UTC": utc_dt.strftime("%Y-%m-%d %H:%M:%S") if utc_dt else "",
                    "付款时间北京": bj_dt.strftime("%m-%d %H:%M:%S") if bj_dt else "",
                    "USD": f"{cents / 100:.2f}",
                    "票夹uuid": item.get("wallet_uuid") or "",
                    "附件": att_name,
                }
            )
        else:
            att_name = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", src_p.name)
            dst_p = att_dir / att_name
            shutil.copy2(src_p, dst_p)
            os.chmod(dst_p, 0o600)
            att_filenames.append(att_name)

            paired_itin_name = ""
            if item.get("category") == "打车出行":
                same_amt = [it for it in unused_itins if int(it.get("amount_cents") or 0) == cents]
                if len(same_amt) == 1:
                    hit_it = same_amt[0]
                    unused_itins.remove(hit_it)
                    it_src = Path(hit_it["path"])
                    paired_itin_name = f"行程单-{item['invoice_no'][-6:] or cents}-{re.sub(r'[^\\w.\\-\\u4e00-\\u9fff]+', '_', it_src.name)}"
                    it_dst = att_dir / paired_itin_name
                    shutil.copy2(it_src, it_dst)
                    os.chmod(it_dst, 0o600)
                    att_filenames.append(paired_itin_name)

            cny_rows.append(
                {
                    "类别": item.get("category") or "国内发票",
                    "发票号码": item["invoice_no"],
                    "开票日期": item.get("issue_date") or "",
                    "金额CNY": f"{cents / 100:.2f}",
                    "配对行程单": paired_itin_name or ("⚠️缺行程单" if item.get("category") == "打车出行" else "-"),
                    "附件": att_name,
                    "SHA256前12位": item["file_sha256"][:12],
                }
            )

    # 转换 200-DPI PNG（供 OA 附件预览/图片上传使用，单文件 15s 超时保护）
    png_count = 0
    pdftoppm_bin = shutil.which("pdftoppm")
    if render_png and pdftoppm_bin:
        for pdf_in_att in sorted(att_dir.glob("*.pdf")):
            out_prefix = png_dir / pdf_in_att.stem
            try:
                subprocess.run(
                    [pdftoppm_bin, "-png", "-r", "200", str(pdf_in_att), str(out_prefix)],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    check=False,
                )
            except Exception:
                continue
        for gen_png in png_dir.glob("*.png"):
            os.chmod(gen_png, 0o600)
            png_count += 1

    # 严格写出 7 列 发票明细.csv（门户收据号,账单号,付款时间UTC,付款时间北京,USD,票夹uuid,附件）
    if usd_rows:
        csv_p = draft_dir / "发票明细.csv"
        fieldnames = ["门户收据号", "账单号", "付款时间UTC", "付款时间北京", "USD", "票夹uuid", "附件"]
        with csv_p.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(usd_rows)
        os.chmod(csv_p, 0o600)

    if cny_rows:
        cn_csv_p = draft_dir / "国内票据明细.csv"
        cn_fields = ["类别", "发票号码", "开票日期", "金额CNY", "配对行程单", "附件", "SHA256前12位"]
        with cn_csv_p.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=cn_fields)
            writer.writeheader()
            writer.writerows(cny_rows)
        os.chmod(cn_csv_p, 0o600)

    att_list_p = draft_dir / "附件清单.txt"
    att_list_p.write_text("\n".join(att_filenames) + "\n", encoding="utf-8")
    os.chmod(att_list_p, 0o600)

    meta_doc = {
        "draft_id": draft_id,
        "status": "DRAFT_READY",
        "created_at": iso_now(),
        "draft_dir": str(draft_dir),
        "manifest_sha256": audit["manifest_sha256"],
        "manifest_path": audit.get("manifest_path", ""),
        "totals_by_currency": audit["totals_by_currency"],
        "usd_invoice_count": len(usd_rows),
        "cny_invoice_count": len(cny_rows),
        "attachment_count": len(att_filenames),
        "png_count": png_count,
        "submitted_batch_no": "",
        "archived_dir": "",
    }
    meta_p = draft_dir / "draft_meta.json"
    meta_p.write_text(json.dumps(meta_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.chmod(meta_p, 0o600)

    readme_lines = [
        f"# 报销草稿包 {draft_id}（状态：DRAFT_READY · 尚未占用历史游标）",
        "",
        f"- 生成时间：{meta_doc['created_at']}",
        f"- 审计指纹：`{audit['manifest_sha256'][:16]}`（通过 {audit['passed_count']} 张，0 冲突）",
        f"- 分币种合计：{', '.join(f'{k} {v:,.2f}' for k, v in audit['totals_by_currency'].items())}",
        f"- 附件目录：`{att_dir}`（共 {len(att_filenames)} 份 PDF，已转 200-DPI PNG {png_count} 张于 `png/`）",
        "- **重要说明**：本草稿在你在 OA 实际提交并执行 `siming ledger mark-submitted <CW08或CW01单号>`（或 QQ 发 `已交单 CW08-...`）前，不会污染或占用报销游标。",
    ]
    (draft_dir / "README.md").write_text("\n".join(readme_lines) + "\n", encoding="utf-8")

    db.set_meta("latest_reimburse_draft_dir", str(draft_dir))
    db.record_human_use("reimburse_audit", "pack_draft", ref_id=draft_id)
    db.write_audit(
        "A1",
        "reimburse_pack_draft",
        {"draft_id": draft_id, "totals": audit["totals_by_currency"], "png_count": png_count},
        target_id=draft_id,
    )
    return {"ok": True, **meta_doc, "audit": audit}


def mark_batch_submitted(
    db: SimingDB,
    batch_no: str,
    draft_ref: str = "latest",
    records_root: Optional[Path] = None,
) -> dict[str, Any]:
    """当本人在 OA 实际交单拿到真实 CW08 / CW01 单号后，将 DRAFT_READY 草稿正式归档入 ~/Documents/报销记录/批次-* 并推进游标。"""
    import shutil

    db.init_db()
    clean_batch = (batch_no or "").strip().upper()
    if not (_CW08_RE.fullmatch(clean_batch) or _CW01_RE.fullmatch(clean_batch)):
        return {
            "ok": False,
            "error": f"报销单号格式不合法（需为 CW08-FYYS-YYYYMMDDXXXX 或 CW01-GRBX-YYYYMMDDXXXX，收到：{batch_no}）",
        }

    draft_dir: Optional[Path] = None
    if draft_ref in ("", "latest"):
        latest_s = db.get_meta("latest_reimburse_draft_dir") or ""
        if latest_s and Path(latest_s).is_dir():
            draft_dir = Path(latest_s)
        else:
            d_root = db.paths.reimburse_dir / "drafts"
            if d_root.is_dir():
                cands = sorted(p for p in d_root.glob("draft-*") if (p / "draft_meta.json").is_file())
                if cands:
                    draft_dir = cands[-1]
    else:
        cand_p = Path(draft_ref).expanduser()
        if cand_p.is_dir():
            draft_dir = cand_p
        else:
            cand_p2 = db.paths.reimburse_dir / "drafts" / draft_ref
            if cand_p2.is_dir():
                draft_dir = cand_p2

    if draft_dir is None or not (draft_dir / "draft_meta.json").is_file():
        return {"ok": False, "error": f"未找到待提交登记的报销草稿包（{draft_ref}），请先跑 `siming ledger pack-draft`"}

    meta_p = draft_dir / "draft_meta.json"
    meta = json.loads(meta_p.read_text(encoding="utf-8"))
    if str(meta.get("status", "")).endswith("_SUBMITTED") and meta.get("submitted_batch_no") != clean_batch:
        return {
            "ok": False,
            "error": f"草稿 `{meta.get('draft_id')}` 已登记为 `{meta.get('submitted_batch_no')}`，防重拦截重复交单",
        }

    root = records_root or Path(
        os.environ.get("SIMING_REIMBURSE_RECORDS_DIR") or DEFAULT_REIMBURSE_RECORDS_ROOT
    )
    root.mkdir(parents=True, exist_ok=True)
    ymd = now_local().strftime("%Y%m%d")
    archive_dir = root / f"批次-{ymd}-{clean_batch}"
    archive_dir.mkdir(parents=True, exist_ok=True)

    for fname in ("发票明细.csv", "国内票据明细.csv", "CW01报销明细汇总.csv", "附件清单.txt", "README.md"):
        src_f = draft_dir / fname
        if src_f.is_file():
            shutil.copy2(src_f, archive_dir / fname)

    new_status = "CW08_SUBMITTED" if clean_batch.startswith("CW08") else "CW01_SUBMITTED"
    meta["status"] = new_status
    meta["submitted_batch_no"] = clean_batch
    meta["submitted_at"] = iso_now()
    meta["archived_dir"] = str(archive_dir)
    meta_p.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    shutil.copy2(meta_p, archive_dir / "draft_meta.json")

    expected_cny = meta.get("total_cny_yuan") or meta.get("target_repayment_cny") or (meta.get("totals_by_currency") or {}).get("CNY")
    if expected_cny:
        try:
            from siming.quiet_cfo import register_receivable

            register_receivable(
                db,
                ref_id=clean_batch,
                title=f"OA报销回款 {clean_batch}",
                expected_yuan=expected_cny,
                kind="reimburse",
                card_hint="1004",
            )
        except Exception:
            pass

    db.record_human_use("reimburse_audit", "mark_submitted", ref_id=clean_batch)
    db.write_audit(
        "A1",
        "reimburse_mark_submitted",
        {"draft_id": meta.get("draft_id"), "batch_no": clean_batch, "archive_dir": str(archive_dir)},
        target_id=clean_batch,
    )
    return {
        "ok": True,
        "status": new_status,
        "batch_no": clean_batch,
        "draft_id": meta.get("draft_id"),
        "archive_dir": str(archive_dir),
        "summary": (
            f"✅ 已正式登记交单 `{clean_batch}`（状态 -> `{new_status}`）\n"
            f"• 归档真源目录：`{archive_dir}`\n"
            "• 该批收据/发票现已正式写入防重索引，后续组包将自动拦截防重。"
        ),
    }


def mark_batch_returned(db: SimingDB, batch_no: str) -> dict[str, Any]:
    """将 OA 退回重提的批次号标为 RETURNED_REUSABLE，释放该单独占票据供换票重提。"""
    clean_batch = (batch_no or "").strip().upper()
    if not clean_batch:
        return {"ok": False, "error": "请提供被退回的批次单号（如 CW08-... 或 CW01-...）"}
    raw = db.get_meta("reimburse_returned_batches_json")
    cur = set(json.loads(raw) if raw else [])
    cur.add(clean_batch)
    db.set_meta("reimburse_returned_batches_json", json.dumps(sorted(cur), ensure_ascii=False))
    db.write_audit("A1", "reimburse_mark_returned", {"batch_no": clean_batch}, target_id=clean_batch)
    return {
        "ok": True,
        "batch_no": clean_batch,
        "status": "RETURNED_REUSABLE",
        "summary": f"♻️ 已将批次 `{clean_batch}` 标记为退回可重用（`RETURNED_REUSABLE`），其独占票据已释放。",
    }


def format_pack_draft_report(res: dict[str, Any]) -> str:
    if not res.get("ok"):
        lines = [f"❌ 草稿拼包未放行（{res.get('status')}）：{res.get('error')}"]
        if res.get("audit"):
            lines.append(format_audit_batch_report(res["audit"]))
        return "\n".join(lines)
    totals_s = "，".join(f"{k} {v:,.2f}" for k, v in (res.get("totals_by_currency") or {}).items())
    return (
        f"📦 报销草稿包已就绪：`{res['draft_id']}`（状态：`DRAFT_READY` · 未占用游标）\n"
        f"• 草稿目录：{res['draft_dir']}\n"
        f"• 分币种合计：{totals_s}\n"
        f"• 生成产物：USD 明细 {res['usd_invoice_count']} 行 ｜ 国内明细 {res['cny_invoice_count']} 行 ｜ 附件 {res['attachment_count']} 份 ｜ 200-DPI PNG {res['png_count']} 张\n"
        "• 下一步：本人在 OA 核对提交拿到单号后，发 `已交单 CW08-...`（或 `siming ledger mark-submitted CW08-...`）即归档入 `~/Documents/报销记录/` 并推进游标。"
    )
