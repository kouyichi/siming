"""银行实时动账短信解析：生成整数分 PENDING 流水，夜间不明大额扣款即时告警。"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from siming.config import load_accounts_config, load_policy
from siming.crypto import contains_otp, store_encrypted_raw
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local, parse_iso
from siming.modules import is_module_enabled

BANK_SIGN_RE = re.compile(
    r"[【\[]([^】\]]*(?:银行|招行|工行|建行|农行|中行|交行|中信|浦发|民生|兴业|光大|广发|平安|邮储)[^】\]]*)[】\]]"
)
LAST4_RE = re.compile(r"(?:尾号|尾数|卡号尾数|账户|卡号后四位|信用卡|借记卡|储蓄卡)\s*(?:为|是)?\*{0,4}(\d{4})")
AMOUNT_RE = re.compile(
    r"(?:消费|支出|扣款|转出|支取|网上支付|快捷支付|付款|还款|转入|存入|入账|工资|奖金|代发|报销|收入|退款|退货|交易|进行了)"
    r"[^0-9¥￥$。；！？\n]{0,14}?"
    r"(?:"
    r"(?P<ccy_pre>人民币|CNY|RMB|美元|USD|港币|HKD|欧元|EUR|日元|JPY|英镑|GBP|新加坡元|新币|SGD|澳元|AUD|加元|CAD|[¥￥$])\s*"
    r"(?P<amt1>[0-9,]+(?:\.[0-9]{1,2})?)\s*"
    r"(?P<ccy_suf1>美元|USD|港币|HKD|欧元|EUR|日元|JPY|英镑|GBP|新加坡元|新币|SGD|澳元|AUD|加元|CAD|元)?"
    r"|"
    r"(?P<amt2>[0-9,]+(?:\.[0-9]{1,2})?)\s*"
    r"(?P<ccy_suf2>人民币|CNY|RMB|美元|USD|港币|HKD|欧元|EUR|日元|JPY|英镑|GBP|新加坡元|新币|SGD|澳元|AUD|加元|CAD|元)"
    r"(?!\s*/\s*(?:月|年|笔|季|日))"
    r")",
    re.IGNORECASE,
)
# 过滤非真实成交的服务开通回执、快捷支付签约、交易失败/额度不足拒付、光大二次风控重发提醒、资费说明与营销短信
NON_TXN_NOTICE_RE = re.compile(
    r"(?:"
    r"开通短信[^\n。；]{0,16}服务|收费标准|起点通知金额|包月费标准|"
    r"\d+(?:\.\d+)?\s*元\s*/\s*(?:月|年|笔|季)|"
    r"成功签约[^\n。；]{0,16}(?:支付|代扣)|已成功开通[^\n。；]{0,16}快捷支付|已绑定快捷支付|中止您的账户交易|申请赎回|"
    r"(?:消费|交易|扣款|支付|还款)[^\n。；]{0,16}(?:未成功|不成功|失败)|(?:可用额度不足|余额不足)[^\n。；]{0,20}(?:未成功|不成功|失败)|"
    r"进行了\s*[0-9,.]+[^\n。；]{0,10}的交易，交易已成功。若交易非您本人操作|"
    r"临时额度|固定额度|调额|超授信额度用卡服务|可享.*分期|邀请您办理|账单已生成|本期应还|最低还款额|"
    r"拒收请回复|退订回|返现活动|活动报名|立减金"
    r")"
)
_CURRENCY_NORM = {
    "元": "CNY", "人民币": "CNY", "CNY": "CNY", "RMB": "CNY", "¥": "CNY", "￥": "CNY",
    "美元": "USD", "USD": "USD", "$": "USD",
    "港币": "HKD", "HKD": "HKD",
    "欧元": "EUR", "EUR": "EUR",
    "日元": "JPY", "JPY": "JPY",
    "英镑": "GBP", "GBP": "GBP",
    "新加坡元": "SGD", "新币": "SGD", "SGD": "SGD",
    "澳元": "AUD", "AUD": "AUD",
    "加元": "CAD", "CAD": "CAD",
}
MERCHANT_SMS_RE = re.compile(
    r"(?:在\s*([^“”\"\n，。]{2,45}?)\s*[“\"]?(?:境外)?网上支付(?:交易)?[”\"]?"
    r"|(?:在|于|向|商户[：:]?)([^\s，,。；;（）()0-9]{2,24})(?:消费|支取|扣款|付款|完成))"
)


_BOUND_CCY = r"(?:人民币|CNY|RMB|美元|USD|港币|HKD|欧元|EUR|日元|JPY|英镑|GBP|[¥￥$])?\s*"
_BOUND_IN_RE = re.compile(r"(?:收入|存入|转入|汇入|入账|到账)\s*" + _BOUND_CCY + r"[0-9][0-9,]*(?:\.[0-9]{1,2})?")
_BOUND_OUT_RE = re.compile(r"(?:支出|消费|扣款|支取|转出|付款|扣费)\s*" + _BOUND_CCY + r"[0-9][0-9,]*(?:\.[0-9]{1,2})?")


def yuan_to_cents(raw_amount: str | int | float) -> int:
    cleaned = str(raw_amount).replace(",", "").replace("¥", "").replace("￥", "").strip()
    dec = (Decimal(cleaned) * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP)
    return abs(int(dec))


def classify_txn_kind(text: str, last4: str = "", repay_cards: Optional[set[str]] = None) -> str:
    if re.search(r"(退款|退货|冲正|撤销)", text):
        return "退款"
    if re.search(
        r"(信用卡还款|自动还款|约定还款|掌上生活还款|还信用卡|账单已还清|还款\s*[0-9,]+|零钱充值|零钱提现|本人.*转账|同名.*转|朝朝宝|余额宝转)",
        text,
    ):
        return "划转"
    # 专用信用卡还款卡（在 accounts.toml 标记 repay_card = true）：其转入或自动扣还信用卡归为内部划转，防重复统计消费
    if repay_cards and last4 and last4 in repay_cards and re.search(r"(转入|存入|还款|自动扣款|扣款|跨行)", text):
        return "划转"
    # 方向词紧贴金额时以它为准：中行「网上支付收入人民币3227.58元」是入账（退款/转入），
    # 不能因为句中出现「支付」二字就被下面的消费兜底吞掉（2026-11-17 林内热水器退款误记为消费）。
    bound_in = _BOUND_IN_RE.search(text)
    bound_out = _BOUND_OUT_RE.search(text)
    if bound_in and not bound_out:
        return "收入"
    if (
        re.search(r"(工资|奖金|薪酬|代发|报销.*入账|转入|存入|入账|收入)", text)
        and "入账金额以账单为准" not in text
        and not re.search(r"(消费|支出|支取|扣款|支付|交易)", text)
    ):
        return "收入"
    return "消费"


def resolve_account_alias(bank_hint: str, last4: str, db: SimingDB) -> str:
    accs = load_accounts_config(db.paths)
    for acc in accs:
        if last4 and str(acc.get("last4") or "") == last4:
            return str(acc.get("alias") or f"{bank_hint}-{last4}")
    wants_credit = "信用卡" in bank_hint
    for acc in accs:
        b = str(acc.get("bank") or "")
        if b and (b in bank_hint or bank_hint in b):
            if wants_credit and acc.get("kind") != "credit":
                continue
            return str(acc.get("alias"))
    for acc in accs:
        b = str(acc.get("bank") or "")
        if b and (b in bank_hint or bank_hint in b):
            return str(acc.get("alias"))
    return f"{bank_hint or '银行卡'}{('-' + last4) if last4 else ''}"


def extract_sms_timestamp_and_body(raw_text: str) -> tuple[Optional[str], str, Optional[date]]:
    """解析快捷指令存量回溯附带的 `<日期> ||| <短信正文>` 或短信正文内的日期（如 10月05日10:00）。"""
    text = (raw_text or "").strip()
    now_dt = now_local()
    today = now_dt.date()
    prefix_iso: Optional[str] = None
    msg_date: Optional[date] = None

    if "|||" in text:
        left, right = text.split("|||", 1)
        left_s = left.strip()
        text = right.strip()
        m_full = re.search(
            r"(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})日?(?:[^\d]*(上午|下午)?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?)?",
            left_s,
        )
        if m_full:
            try:
                yr, mo, da = int(m_full.group(1)), int(m_full.group(2)), int(m_full.group(3))
                ampm = m_full.group(4) or ""
                has_hm = m_full.group(5) is not None
                hh = int(m_full.group(5) or 12)
                mm = int(m_full.group(6) or 0)
                ss = int(m_full.group(7) or 0)
                if ampm == "下午" and hh < 12:
                    hh += 12
                elif ampm == "上午" and hh == 12:
                    hh = 0
                dt = datetime(yr, mo, da, hh, mm, ss, tzinfo=SHANGHAI_TZ)
                if not has_hm and dt > now_dt:
                    dt = now_dt
                prefix_iso = dt.isoformat(timespec="seconds")
                msg_date = dt.date()
            except ValueError:
                pass

    if not prefix_iso:
        m_in = re.search(
            r"(?:(20\d{2})[-年])?(\d{1,2})月(\d{1,2})日\s*(?:(\d{1,2})[:时](\d{2}))?",
            text,
        )
        if m_in:
            mo, da = int(m_in.group(2)), int(m_in.group(3))
            yr = int(m_in.group(1)) if m_in.group(1) else (
                today.year - 1 if (mo, da) > (today.month, today.day) else today.year
            )
            has_hm = m_in.group(4) is not None
            hh = int(m_in.group(4)) if has_hm else 12
            mm = int(m_in.group(5)) if m_in.group(5) is not None else 0
            try:
                dt = datetime(yr, mo, da, hh, mm, 0, tzinfo=SHANGHAI_TZ)
                # 防止日级无时分短信在凌晨/上午默认填 12:00 造成未来时间戳倒挂
                if not has_hm and dt > now_dt:
                    dt = now_dt
                prefix_iso = dt.isoformat(timespec="seconds")
                msg_date = dt.date()
            except ValueError:
                pass

    return prefix_iso, text, msg_date


def try_ingest_bank_sms(
    db: SimingDB,
    text: str,
    source: str = "ios_sms",
    raw_ref: Optional[str] = None,
    occurred_at: Optional[str] = None,
    silent_backfill: bool = False,
) -> dict[str, Any]:
    parsed_occ, clean_body, _ = extract_sms_timestamp_and_body(text)
    body = clean_body or text
    if not body or contains_otp(body):
        return {"matched": False}
    if NON_TXN_NOTICE_RE.search(body):
        return {"matched": False, "skipped_notice": True}
    m_bank = BANK_SIGN_RE.search(body)
    m_amt = AMOUNT_RE.search(body)
    if not m_bank or not m_amt:
        return {"matched": False}

    bank_name = m_bank.group(1).strip()
    raw_num = m_amt.group("amt1") or m_amt.group("amt2") or "0"
    cents = yuan_to_cents(raw_num)
    if cents <= 0:
        return {"matched": False}
    m_l4 = LAST4_RE.search(body)
    last4 = m_l4.group(1) if m_l4 else ""
    alias = resolve_account_alias(bank_name, last4, db)
    repay_cards = {
        str(a.get("last4") or "")
        for a in load_accounts_config(db.paths)
        if a.get("last4") and a.get("repay_card")
    }
    kind = classify_txn_kind(body, last4=last4, repay_cards=repay_cards)
    m_mer = MERCHANT_SMS_RE.search(body)
    merchant = ((m_mer.group(1) or m_mer.group(2)) if m_mer else "").strip()

    ccy_pre = (m_amt.group("ccy_pre") or "").strip()
    ccy_suf1 = (m_amt.group("ccy_suf1") or "").strip()
    ccy_suf2 = (m_amt.group("ccy_suf2") or "").strip()
    # 当出现“消费美元100.00元”或“USD 100元”时，明确外币前缀优先于泛化量词“元”
    if ccy_pre and ccy_suf1 == "元":
        raw_ccy = ccy_pre
    else:
        raw_ccy = ccy_suf1 or ccy_pre or ccy_suf2 or "CNY"
    currency = _CURRENCY_NORM.get(raw_ccy.upper(), _CURRENCY_NORM.get(raw_ccy, "CNY"))
    notes = "外币交易暂不自动跨源硬并" if currency != "CNY" else ""

    occ = occurred_at or parsed_occ or iso_now()
    ts = iso_now()
    norm_body = re.sub(r"\s+", " ", body.strip())
    body_sha1 = hashlib.sha1(norm_body.encode("utf-8")).hexdigest()
    ref = raw_ref or store_encrypted_raw(body, db.paths)
    fp = f"banksms:{hashlib.sha256((occ[:16] + ':' + norm_body).encode('utf-8')).hexdigest()[:28]}"

    amt_disp = f"${cents/100:.2f}" if currency == "USD" else (f"¥{cents/100:.2f}" if currency == "CNY" else f"{currency} {cents/100:.2f}")
    with db.transaction() as conn:
        ev_id = db.append_event(
            conn,
            source=source,
            thread_key=f"ledger:{alias}",
            summary=f"动账短信：{alias} {kind} {amt_disp}",
            payload={
                "alias": alias,
                "last4": last4,
                "amount_cents": cents,
                "currency": currency,
                "kind": kind,
                "merchant": merchant,
                "body_sha1": body_sha1,
            },
            raw_ref=ref,
            occurred_at=occ,
            fingerprint=fp,
        )
        if ev_id is None:
            return {"matched": True, "duplicate": True}

        # 若存量回溯撞上同日已实时入库的「完全相同短信正文哈希」（兼容无时分短信的日内时间差，绝不误吞同日同额不同消费！）
        if silent_backfill:
            same_day_live = conn.execute(
                """
                SELECT e.payload_json, e.raw_ref FROM events e
                WHERE e.event_id != ? AND e.source = 'ios_sms' AND e.occurred_at LIKE ?
                """,
                (ev_id, occ[:10] + "%"),
            ).fetchall()
            for lr in same_day_live:
                try:
                    lp = json.loads(lr["payload_json"] or "{}")
                except Exception:
                    lp = {}
                if lp.get("body_sha1") == body_sha1 or (lr["raw_ref"] and lr["raw_ref"] == ref):
                    return {"matched": True, "duplicate": True}

        # 若先导入了微信/支付宝/抖音明细，后补扫存量短信，反向与「非短信来源」明细配对升级「已对账」（严禁把两条独立银行短信合并成一条！）
        if currency == "CNY" and last4:
            occ_dt = parse_iso(occ)
            w_start = iso_now(occ_dt - timedelta(minutes=15))
            w_end = iso_now(occ_dt + timedelta(minutes=15))
            existing_cands = conn.execute(
                """
                SELECT l.txn_id FROM ledger_txns l
                WHERE l.currency = 'CNY' AND l.card_last4 = ? AND l.amount_cents = ? AND l.kind = ?
                  AND l.occurred_at >= ? AND l.occurred_at <= ?
                  AND NOT EXISTS (SELECT 1 FROM links lk WHERE lk.txn_id = l.txn_id AND lk.role = 'sms')
                """,
                (last4, cents, kind, w_start, w_end),
            ).fetchall()
            if len(existing_cands) == 1:
                ex_id = str(existing_cands[0]["txn_id"])
                conn.execute(
                    """
                    UPDATE ledger_txns
                    SET account_alias = ?,
                        merchant = COALESCE(NULLIF(merchant, ''), ?),
                        status = '已对账',
                        updated_at = ?
                    WHERE txn_id = ?
                    """,
                    (alias, merchant, ts, ex_id),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO links (txn_id, event_id, role, created_at) VALUES (?, ?, 'sms', ?)",
                    (ex_id, ev_id, ts),
                )
                return {
                    "matched": True,
                    "txn_id": ex_id,
                    "paired_existing": True,
                    "amount_cents": cents,
                    "kind": kind,
                    "account_alias": alias,
                }

        txn_id = f"TX-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
        status = "待核实" if currency != "CNY" else "PENDING"
        conn.execute(
            """
            INSERT INTO ledger_txns (
                txn_id, occurred_at, amount_cents, currency, account_alias,
                card_last4, kind, status, merchant, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (txn_id, occ, cents, currency, alias, last4, kind, status, merchant, notes, ts, ts),
        )
        conn.execute(
            "INSERT INTO links (txn_id, event_id, role, created_at) VALUES (?, ?, 'sms', ?)",
            (txn_id, ev_id, ts),
        )

        # 降噪原则（用户拍板 #5）：日常/深夜零散动账一律静默入库并整合进晨札汇总，绝不逐笔打扰；
        # 仅当单笔达到「超大额阈值」（默认人民币 ≥20,000 元 / 外币 ≥$2,000）时才发单条实时提醒！
        policy = load_policy(db.paths)
        alert_cfg = policy.get("ledger_alerts") or {}
        cny_super_cents = int(float(alert_cfg.get("super_large_cny_yuan", 20000)) * 100)
        fx_super_cents = int(float(alert_cfg.get("super_large_fx_usd", 2000)) * 100)
        super_thresh = cny_super_cents if currency == "CNY" else fx_super_cents

        if (
            not silent_backfill
            and kind in ("消费", "划转")
            and cents >= super_thresh
            and is_module_enabled(db, "bank_sms_alert")
        ):
            db.enqueue_effect(
                conn,
                "send_qq",
                {
                    "ref_id": txn_id,
                    "rule_id": "bank_sms_alert",
                    "urgent": False,
                    "text": (
                        f"💳 超大额动账提醒：{alias} {kind} {amt_disp}"
                        + (f"（商户：{merchant}）" if merchant else "（其余日常动账已静默整合至晨札）")
                    ),
                },
            )

    # S2-D：在 accounts.toml 标记 repay_card/reimburse_cursor 的卡片收到还款或大额公事付款入账时，自动刷新「报销游标卡」
    accs_cfg = load_accounts_config(db.paths)
    reimburse_repay_last4s = {
        str(a.get("last4") or "")
        for a in accs_cfg
        if a.get("last4") and (a.get("repay_card") or a.get("reimburse_cursor") or a.get("kind") == "credit")
    }
    reimburse_spend_last4s = {
        str(a.get("last4") or "")
        for a in accs_cfg
        if a.get("last4") and a.get("reimburse_cursor") and a.get("kind") == "debit"
    }
    if not silent_backfill and (
        (
            last4 in reimburse_repay_last4s
            and kind == "划转"
            and ((currency == "CNY" and cents >= 20000) or (currency != "CNY" and cents >= 5000))
        )
        or (currency == "CNY" and last4 in reimburse_spend_last4s and kind == "消费" and cents >= 100000)
    ):
        try:
            from siming.ledger.reimburse_audit import upsert_reimbursement_cursor_case

            upsert_reimbursement_cursor_case(
                db,
                trigger_txn={
                    "txn_id": txn_id,
                    "occurred_at": occ,
                    "amount_cents": cents,
                    "currency": currency,
                    "account_alias": alias,
                    "card_last4": last4,
                    "kind": kind,
                },
            )
        except Exception:
            pass

    if not silent_backfill and currency == "CNY" and kind in ("收入", "退款"):
        try:
            from siming.quiet_cfo import match_incoming_receivable

            match_incoming_receivable(db, txn_id=txn_id, amount_cents=cents, last4=last4, kind=kind, raw_text=text)
        except Exception:
            pass

    # F08 + Directive #5：无商户名的新增银行卡消费流水（如 ¥9.90），立即尝试匹配当日电商订单或历史贝叶斯先验（抖音小说/小程序等）透明打注
    if not silent_backfill and kind == "消费" and not merchant:
        try:
            from siming.ledger.enrich_priors import enrich_pending_ledger_with_priors_and_orders

            enrich_pending_ledger_with_priors_and_orders(db, days=3)
        except Exception:
            pass

    return {"matched": True, "txn_id": txn_id, "amount_cents": cents, "currency": currency, "kind": kind, "account_alias": alias}
