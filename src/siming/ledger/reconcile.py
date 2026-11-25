"""整数分保守核账引擎：多笔同额歧义标「待核实」绝不硬配、退款挂原单、疑似周期暗扣检测与诚实覆盖月报。"""

from __future__ import annotations

import hashlib
import re
import secrets
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Optional

from siming.config import load_accounts_config
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local, parse_iso

_DAILY_NON_SUB_MERCHANT_RE = re.compile(
    r"(?:滴滴|高德打车|打车|出行|停车|ETCP|美团|饿了么|盒马|山姆|京东|淘宝|天猫|拼多多|便利蜂|7-ELEVEn|罗森|全家|麦当劳|肯德基|星巴克|瑞幸|Manner|丰e足食|菜鸟|顺丰)"
    ,
    re.IGNORECASE,
)
_EXPLICIT_SUB_HINT_RE = re.compile(
    r"(?:自动续费|自动扣款|连续包|会员|神会员|订阅|套餐|省钱卡|畅行卡|月卡|年卡|季卡|iCloud|Apple\s*服务|GOOGLE\*ONE|PAID\s*FEATURES|BOARDGAMEARENA)",
    re.IGNORECASE,
)
_MANUAL_TOPUP_HINT_RE = re.compile(r"(?:·\s*充值|转账备注|经营码交易|API服务|外卖App)")


def match_or_record_wallet_row(
    db: SimingDB,
    occurred_at: str,
    amount_cents: int,
    card_last4: str,
    wallet_alias: str,
    merchant: str,
    kind: str,
    channel_order_id: str = "",
    source: str = "export_wechat",
) -> dict[str, Any]:
    """将微信/支付宝明细与银行短信或月结单配对（±10 分钟对短信，±1 天对月结单；同额多笔歧义标「待核实」）。"""
    occ_dt = parse_iso(occurred_at)
    ts = iso_now()
    fp = f"wallet:{channel_order_id}" if channel_order_id else f"wallet:{hashlib.sha1(f'{occurred_at}:{amount_cents}:{merchant}'.encode()).hexdigest()[:20]}"

    with db.transaction() as conn:
        ev_id = db.append_event(
            conn,
            source=source,
            thread_key=f"wallet:{card_last4 or wallet_alias}",
            summary=f"支付明细：{merchant[:40]} ¥{amount_cents/100:.2f}",
            payload={"amount_cents": amount_cents, "card_last4": card_last4, "merchant": merchant, "kind": kind},
            occurred_at=occurred_at,
            fingerprint=fp,
        )
        if ev_id is None:
            return {"duplicate": True, "status": "SKIPPED"}

        # 1. 先按渠道单号精确匹配
        if channel_order_id:
            exact = conn.execute(
                "SELECT txn_id, status FROM ledger_txns WHERE channel_order_id = ?",
                (channel_order_id,),
            ).fetchone()
            if exact:
                conn.execute(
                    "UPDATE ledger_txns SET merchant=COALESCE(NULLIF(?, ''), merchant), status='已对账', updated_at=? WHERE txn_id=?",
                    (merchant, ts, exact["txn_id"]),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO links (txn_id, event_id, role, created_at) VALUES (?, ?, ?, ?)",
                    (exact["txn_id"], ev_id, source, ts),
                )
                return {"txn_id": exact["txn_id"], "status": "已对账"}

        # 2. 若走银行卡扣款，仅允许与「互补来源（银行短信 sms / 月结单 statement）」且无冲突渠道单号的记录配对！
        #    严禁把同一钱包里两笔相隔 1 分钟、同额不同订单号的消费合并成一条！
        if card_last4:
            win_start = iso_now(occ_dt - timedelta(days=1))
            win_end = iso_now(occ_dt + timedelta(days=1))
            cands = conn.execute(
                """
                SELECT l.* FROM ledger_txns l
                WHERE l.card_last4 = ? AND l.currency = 'CNY' AND l.amount_cents = ? AND l.kind = ?
                  AND l.status != 'VOID_DUPLICATE'
                  AND l.occurred_at >= ? AND l.occurred_at <= ?
                  AND (l.channel_order_id = '' OR l.channel_order_id IS NULL OR l.channel_order_id = ?)
                  AND EXISTS (
                      SELECT 1 FROM links lk
                      WHERE lk.txn_id = l.txn_id AND lk.role IN ('sms', 'statement', 'export_didi', 'export_meituan')
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM links lk2
                      WHERE lk2.txn_id = l.txn_id AND lk2.role = ?
                  )
                """,
                (card_last4, amount_cents, kind, win_start, win_end, channel_order_id, source),
            ).fetchall()

            # 进一步优先看 ±10 分钟内的未配对短信流水
            close_10m = [
                c
                for c in cands
                if abs((parse_iso(c["occurred_at"]) - occ_dt).total_seconds()) <= 600
                and c["status"] == "PENDING"
            ]
            pool = close_10m if close_10m else [c for c in cands if c["status"] in ("PENDING", "待核实")]

            if len(pool) == 1:
                matched_id = pool[0]["txn_id"]
                conn.execute(
                    """
                    UPDATE ledger_txns
                    SET merchant=?, channel_order_id=COALESCE(NULLIF(?, ''), channel_order_id),
                        status='已对账', updated_at=?
                    WHERE txn_id=?
                    """,
                    (merchant, channel_order_id, ts, matched_id),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO links (txn_id, event_id, role, created_at) VALUES (?, ?, ?, ?)",
                    (matched_id, ev_id, source, ts),
                )
                return {"txn_id": matched_id, "status": "已对账"}

            if len(pool) >= 2:
                # 铁律：同一窗口内多笔同额交易无法唯一区分，全部标「待核实」，严禁按时间先后硬配！
                for amb in pool:
                    conn.execute(
                        "UPDATE ledger_txns SET status='待核实', notes='同日多笔同额待人工确认归属', updated_at=? WHERE txn_id=?",
                        (ts, amb["txn_id"]),
                    )
                new_id = f"TX-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
                conn.execute(
                    """
                    INSERT INTO ledger_txns (
                        txn_id, occurred_at, amount_cents, currency, account_alias, card_last4,
                        kind, status, merchant, channel_order_id, notes, created_at, updated_at
                    ) VALUES (?, ?, ?, 'CNY', ?, ?, ?, '待核实', ?, ?, '同日多笔同额待核实，未硬配', ?, ?)
                    """,
                    (new_id, occurred_at, amount_cents, wallet_alias, card_last4, kind, merchant, channel_order_id, ts, ts),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO links (txn_id, event_id, role, created_at) VALUES (?, ?, ?, ?)",
                    (new_id, ev_id, source, ts),
                )
                return {"txn_id": new_id, "status": "待核实", "ambiguous_candidates": len(pool)}

        # 3. 若为退款，尝试关联近 90 天同商户原消费单（parent_txn_id）
        parent_id: Optional[str] = None
        if kind == "退款" and merchant:
            mer_prefix = merchant.split("·")[0].strip()[:10]
            origs = conn.execute(
                """
                SELECT txn_id FROM ledger_txns
                WHERE kind='消费' AND currency='CNY' AND amount_cents >= ? AND merchant LIKE ?
                ORDER BY occurred_at DESC LIMIT 2
                """,
                (amount_cents, f"%{mer_prefix}%"),
            ).fetchall()
            if len(origs) == 1:
                parent_id = str(origs[0]["txn_id"])

        # 4. 独立建账（如零钱 / 花呗直接支付，或无短信通知的低频副卡消费）
        txn_id = f"TX-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
        status = "已对账" if (not card_last4) else "PENDING"
        conn.execute(
            """
            INSERT INTO ledger_txns (
                txn_id, occurred_at, amount_cents, currency, account_alias, card_last4,
                kind, status, merchant, channel_order_id, parent_txn_id, created_at, updated_at
            ) VALUES (?, ?, ?, 'CNY', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                txn_id,
                occurred_at,
                amount_cents,
                wallet_alias,
                card_last4,
                kind,
                status,
                merchant,
                channel_order_id,
                parent_id,
                ts,
                ts,
            ),
        )
        conn.execute(
            "INSERT OR IGNORE INTO links (txn_id, event_id, role, created_at) VALUES (?, ?, ?, ?)",
            (txn_id, ev_id, source, ts),
        )
        return {"txn_id": txn_id, "status": status}


def match_or_record_statement_row(
    db: SimingDB,
    occurred_date: str,
    amount_cents: int,
    account_alias: str,
    card_last4: str,
    merchant: str,
    kind: str,
    event_id: Optional[str] = None,
    currency: str = "CNY",
    row_index: int = 0,
) -> dict[str, Any]:
    """用信用卡/借记卡官方月结单敲定最终入账状态（支持外币短信核销，一行月结单严格只消费一条未挂 statement 的记录）。"""
    base_dt = parse_iso(f"{occurred_date}T12:00:00+08:00")
    win_start = iso_now(base_dt - timedelta(days=1, hours=12))
    win_end = iso_now(base_dt + timedelta(days=1, hours=12))
    ts = iso_now()
    curr = (currency or "CNY").upper()
    stmt_fp = (
        "stmt_row:"
        + hashlib.sha256(
            f"{event_id or ''}:{row_index}:{account_alias}:{card_last4}:{occurred_date}:{curr}:{kind}:{amount_cents}:{merchant.strip()}".encode("utf-8")
        ).hexdigest()[:28]
    )

    with db.transaction() as conn:
        stmt_ev_id = db.append_event(
            conn,
            source="statement",
            thread_key=f"stmt:{card_last4 or account_alias}",
            summary=f"月结单明细：{occurred_date} {merchant[:32]} {curr} {amount_cents/100:.2f}",
            payload={"date": occurred_date, "amount_cents": amount_cents, "currency": curr, "merchant": merchant, "row_index": row_index},
            occurred_at=f"{occurred_date}T12:00:00+08:00",
            fingerprint=stmt_fp,
        )
        if stmt_ev_id is None:
            return {"duplicate": True, "status": "已对账"}

        link_ev = event_id or stmt_ev_id
        # 严格排除已绑定过 statement 的记录，杜绝同一笔已对账交易被月结单多行反复复用！
        cands = conn.execute(
            """
            SELECT l.* FROM ledger_txns l
            WHERE (l.account_alias = ? OR (l.card_last4 != '' AND l.card_last4 = ?))
              AND l.currency = ? AND l.amount_cents = ? AND l.kind = ?
              AND l.occurred_at >= ? AND l.occurred_at <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM links lk
                  WHERE lk.txn_id = l.txn_id AND lk.role = 'statement'
              )
            """,
            (account_alias, card_last4, curr, amount_cents, kind, win_start, win_end),
        ).fetchall()

        # 外币短信初始状态为「待核实（外币交易暂不自动跨源硬并）」，同币种官方月结单到来时允许与唯一外币短信核销升级为「已对账」
        pending_cands = [
            c
            for c in cands
            if c["status"] == "PENDING"
            or (curr != "CNY" and c["status"] == "待核实" and "外币交易" in str(c["notes"] or ""))
        ]
        reconciled_cands = [c for c in cands if c["status"] == "已对账"]

        # 1. 若有唯一待结短信/钱包单（含同币种外币短信），敲定为已对账
        if len(pending_cands) == 1:
            tid = pending_cands[0]["txn_id"]
            conn.execute(
                """
                UPDATE ledger_txns
                SET status='已对账', merchant=COALESCE(NULLIF(merchant, ''), ?), notes='', updated_at=?
                WHERE txn_id=?
                """,
                (merchant, ts, tid),
            )
            conn.execute(
                "INSERT OR IGNORE INTO links (txn_id, event_id, role, created_at) VALUES (?, ?, 'statement', ?)",
                (tid, link_ev, ts),
            )
            return {"txn_id": tid, "status": "已对账"}

        # 2. 若无 PENDING，但已有唯一一条（如银行短信+微信账单已提前配成的）「已对账」记录，挂接月结单证据而不重复建账！
        if not pending_cands and len(reconciled_cands) == 1:
            tid = reconciled_cands[0]["txn_id"]
            conn.execute(
                """
                UPDATE ledger_txns
                SET merchant=COALESCE(NULLIF(merchant, ''), ?), updated_at=?
                WHERE txn_id=?
                """,
                (merchant, ts, tid),
            )
            conn.execute(
                "INSERT OR IGNORE INTO links (txn_id, event_id, role, created_at) VALUES (?, ?, 'statement', ?)",
                (tid, link_ev, ts),
            )
            return {"txn_id": tid, "status": "已对账", "already_reconciled": True}

        if len(pending_cands) >= 2 or (not pending_cands and len(reconciled_cands) >= 2):
            target_pool = pending_cands if pending_cands else reconciled_cands
            for c in target_pool:
                if c["status"] == "PENDING":
                    conn.execute(
                        "UPDATE ledger_txns SET status='待核实', notes='月结单同额多笔歧义待核实', updated_at=? WHERE txn_id=?",
                        (ts, c["txn_id"]),
                    )
            return {"txn_id": "", "status": "待核实", "ambiguous": len(target_pool)}

        txn_id = f"TX-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
        conn.execute(
            """
            INSERT INTO ledger_txns (
                txn_id, occurred_at, amount_cents, currency, account_alias, card_last4,
                kind, status, merchant, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '已对账', ?, ?, ?)
            """,
            (txn_id, f"{occurred_date}T12:00:00+08:00", amount_cents, curr, account_alias, card_last4, kind, merchant, ts, ts),
        )
        conn.execute(
            "INSERT OR IGNORE INTO links (txn_id, event_id, role, created_at) VALUES (?, ?, 'statement', ?)",
            (txn_id, link_ev, ts),
        )
        return {"txn_id": txn_id, "status": "已对账"}


def detect_recurring_subscriptions(
    db: SimingDB,
    reference_month: Optional[str] = None,
) -> list[dict[str, Any]]:
    """检测当前仍活跃的疑似周期自动续费暗扣（检查最近两笔间隔为月度 26.5~34.5d 或年度 360~370d，过滤打车/外卖/手工充值假阳性）。"""
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT merchant, amount_cents, currency, occurred_at
            FROM ledger_txns
            WHERE kind = '消费' AND merchant != '' AND status != 'VOID_DUPLICATE'
            ORDER BY occurred_at ASC
            """
        ).fetchall()
    finally:
        conn.close()

    ref_ym = reference_month or now_local().strftime("%Y-%m")
    try:
        yr_s, mo_s = ref_ym.split("-")[:2]
        yr_i, mo_i = int(yr_s), int(mo_s)
        next_yr, next_mo = (yr_i + 1, 1) if mo_i == 12 else (yr_i, mo_i + 1)
        ref_end = datetime(next_yr, next_mo, 1, tzinfo=SHANGHAI_TZ)
    except Exception:
        ref_end = now_local() + timedelta(days=1)

    grouped: dict[tuple[str, str, int], list[tuple[str, str]]] = defaultdict(list)
    for r in rows:
        full_mer = str(r["merchant"] or "").strip()
        if _MANUAL_TOPUP_HINT_RE.search(full_mer):
            continue
        mer_base = full_mer.split("·")[0].strip()[:24]
        curr = str(r["currency"] or "CNY").upper()
        cents = int(r["amount_cents"])
        if _DAILY_NON_SUB_MERCHANT_RE.search(mer_base) and not _EXPLICIT_SUB_HINT_RE.search(full_mer):
            continue
        grouped[(mer_base, curr, cents)].append((str(r["occurred_at"]), full_mer))

    suspects: list[dict[str, Any]] = []
    for (mer, curr, cents), entries in grouped.items():
        if len(entries) < 2:
            continue
        # 仅保留在参考月末之前发生的记录，并检查「最近两笔」是否仍处于周期扣款节奏（避免按量高频充值碰巧撞上某两次间隔 30d）
        valid_entries = [(parse_iso(t), t, fm) for (t, fm) in entries if parse_iso(t) < ref_end]
        if len(valid_entries) < 2:
            continue
        dt_prev, _, _ = valid_entries[-2]
        dt_last, t_last, _ = valid_entries[-1]
        gap_days = (dt_last - dt_prev).total_seconds() / 86400.0
        age_days = (ref_end - dt_last).total_seconds() / 86400.0
        is_monthly = 26.5 <= gap_days <= 34.5 and age_days <= 50.0
        is_annual = 360.0 <= gap_days <= 370.0 and age_days <= 385.0
        if is_monthly or is_annual:
            suspects.append(
                {
                    "merchant": mer,
                    "currency": curr,
                    "amount_cents": cents,
                    "amount_yuan": f"{cents / 100:.2f}",
                    "interval_days": round(gap_days, 1),
                    "last_charged_at": t_last[:10],
                }
            )
    suspects.sort(key=lambda x: (x["last_charged_at"], x["amount_cents"]), reverse=True)
    return suspects


def enrich_or_record_platform_order(
    db: SimingDB,
    occurred_at: str,
    amount_cents: int,
    platform: str,
    detail_merchant: str,
    kind: str = "消费",
    channel_order_id: str = "",
    card_last4: str = "",
    window_minutes: int = 35,
) -> dict[str, Any]:
    """导入滴滴行程单 / 美团订单明细：优先在 ±35m 内寻找已有微信/支付宝/银行卡同额流水并补全起终点或菜品说明（严防重复记两遍消费！）。"""
    occ_dt = parse_iso(occurred_at)
    ts = iso_now()
    source = f"export_{platform}"
    fp_seed = channel_order_id or hashlib.sha1(f"{platform}:{occurred_at}:{amount_cents}:{detail_merchant}".encode("utf-8")).hexdigest()[:20]
    fp = f"plat:{platform}:{fp_seed}"

    plat_kw_map = {
        "didi": ("滴滴", "小桔", "高德", "打车", "出行"),
        "meituan": ("美团", "三快", "外卖", "大众点评"),
    }
    plat_kws = plat_kw_map.get(platform, ())

    with db.transaction() as conn:
        ev_id = db.append_event(
            conn,
            source=source,
            thread_key=f"plat:{platform}",
            summary=f"{platform}明细：{detail_merchant[:44]} ¥{amount_cents / 100:.2f}",
            payload={"platform": platform, "amount_cents": amount_cents, "merchant": detail_merchant, "kind": kind},
            occurred_at=occurred_at,
            fingerprint=fp,
        )
        if ev_id is None:
            return {"duplicate": True, "status": "SKIPPED"}

        if channel_order_id:
            exact = conn.execute(
                "SELECT txn_id, status FROM ledger_txns WHERE channel_order_id = ? AND status != 'VOID_DUPLICATE'",
                (channel_order_id,),
            ).fetchone()
            if exact:
                conn.execute(
                    "UPDATE ledger_txns SET merchant=?, status='已对账', updated_at=? WHERE txn_id=?",
                    (detail_merchant[:80], ts, exact["txn_id"]),
                )
                conn.execute(
                    "INSERT OR IGNORE INTO links (txn_id, event_id, role, created_at) VALUES (?, ?, ?, ?)",
                    (exact["txn_id"], ev_id, source, ts),
                )
                return {"txn_id": exact["txn_id"], "status": "已对账", "enriched": True}

        win_start = iso_now(occ_dt - timedelta(minutes=window_minutes))
        win_end = iso_now(occ_dt + timedelta(minutes=window_minutes))
        cands = conn.execute(
            """
            SELECT l.* FROM ledger_txns l
            WHERE l.currency = 'CNY' AND l.amount_cents = ? AND l.kind = ?
              AND l.status != 'VOID_DUPLICATE'
              AND l.occurred_at >= ? AND l.occurred_at <= ?
              AND NOT EXISTS (
                  SELECT 1 FROM links lk
                  WHERE lk.txn_id = l.txn_id AND lk.role = ?
              )
            """,
            (amount_cents, kind, win_start, win_end, source),
        ).fetchall()

        # 优先匹配商户名已带「滴滴/美团」特征或银行短信无商户名的流水
        preferred = [
            c
            for c in cands
            if not str(c["merchant"] or "").strip()
            or any(kw in str(c["merchant"] or "") or kw in str(c["account_alias"] or "") for kw in plat_kws)
        ]
        pool = preferred if preferred else list(cands)

        if len(pool) == 1:
            tid = str(pool[0]["txn_id"])
            new_status = "已对账" if pool[0]["status"] in ("PENDING", "已对账") else str(pool[0]["status"])
            conn.execute(
                """
                UPDATE ledger_txns
                SET merchant=?, status=?, updated_at=?
                WHERE txn_id=?
                """,
                (detail_merchant[:80], new_status, ts, tid),
            )
            conn.execute(
                "INSERT OR IGNORE INTO links (txn_id, event_id, role, created_at) VALUES (?, ?, ?, ?)",
                (tid, ev_id, source, ts),
            )
            return {"txn_id": tid, "status": new_status, "enriched": True}

        if len(pool) >= 2:
            # 多笔同额近距消费不硬猜配哪条，标待核实留给 resolve_ledger_txn
            new_id = f"TX-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
            default_alias = "滴滴账单" if platform == "didi" else ("美团账单" if platform == "meituan" else platform)
            conn.execute(
                """
                INSERT INTO ledger_txns (
                    txn_id, occurred_at, amount_cents, currency, account_alias, card_last4,
                    kind, status, merchant, channel_order_id, notes, created_at, updated_at
                ) VALUES (?, ?, ?, 'CNY', ?, ?, ?, '待核实', ?, ?, '多笔同额支付流水待配对消歧', ?, ?)
                """,
                (new_id, occurred_at, amount_cents, default_alias, card_last4, kind, detail_merchant[:80], channel_order_id, ts, ts),
            )
            conn.execute(
                "INSERT OR IGNORE INTO links (txn_id, event_id, role, created_at) VALUES (?, ?, ?, ?)",
                (new_id, ev_id, source, ts),
            )
            return {"txn_id": new_id, "status": "待核实", "ambiguous_candidates": len(pool)}

        txn_id = f"TX-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
        default_alias = "滴滴账单" if platform == "didi" else ("美团账单" if platform == "meituan" else platform)
        status = "已对账" if not card_last4 else "PENDING"
        conn.execute(
            """
            INSERT INTO ledger_txns (
                txn_id, occurred_at, amount_cents, currency, account_alias, card_last4,
                kind, status, merchant, channel_order_id, created_at, updated_at
            ) VALUES (?, ?, ?, 'CNY', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (txn_id, occurred_at, amount_cents, default_alias, card_last4, kind, status, detail_merchant[:80], channel_order_id, ts, ts),
        )
        conn.execute(
            "INSERT OR IGNORE INTO links (txn_id, event_id, role, created_at) VALUES (?, ?, ?, ?)",
            (txn_id, ev_id, source, ts),
        )
        return {"txn_id": txn_id, "status": status, "enriched": False}


def resolve_ledger_txn(
    db: SimingDB,
    txn_id: str,
    action: str,
    value: str = "",
) -> dict[str, Any]:
    """S3-3：确定性三选一流水消歧（note=补充备注标已对账 / pair=合并至目标TX / void-dup=标记重复作废不计支出）。"""
    db.init_db()
    tid = (txn_id or "").strip()
    act = (action or "").strip().lower()
    val = (value or "").strip()
    if act not in ("note", "pair", "void-dup"):
        return {"ok": False, "error": f"不支持的消歧动作 `{action}`（可选：note / pair / void-dup）"}

    ts = iso_now()
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM ledger_txns WHERE txn_id = ?", (tid,)).fetchone()
        if not row:
            return {"ok": False, "error": f"未找到流水 `{tid}`"}

        if act == "note":
            note_txt = f"人工确认: {val}" if val else "人工确认无误"
            conn.execute(
                "UPDATE ledger_txns SET status='已对账', notes=?, updated_at=? WHERE txn_id=?",
                (note_txt[:160], ts, tid),
            )
            summary = f"✅ 流水 `{tid}` 已标记为「已对账」（{note_txt}）。"

        elif act == "void-dup":
            note_txt = f"人工标记重复作废: {val}" if val else "人工标记重复流水作废"
            conn.execute(
                "UPDATE ledger_txns SET status='VOID_DUPLICATE', notes=?, updated_at=? WHERE txn_id=?",
                (note_txt[:160], ts, tid),
            )
            summary = f"🗑️ 流水 `{tid}` 已标为 `VOID_DUPLICATE`（月报将不再重复计入支出与笔数）。"

        else:  # pair
            target_tid = val
            if not target_tid or target_tid == tid:
                return {"ok": False, "error": "pair 模式需提供另一条目标流水编号（如 TX-...）"}
            target_row = conn.execute("SELECT * FROM ledger_txns WHERE txn_id = ?", (target_tid,)).fetchone()
            if not target_row:
                return {"ok": False, "error": f"未找到配对目标流水 `{target_tid}`"}
            if target_row["status"] == "VOID_DUPLICATE":
                return {"ok": False, "error": f"目标流水 `{target_tid}` 已作废，无法作为配对主单"}

            # 将当前流水的证据链挂到主流水，若主流水缺商户名则用当前流水补全，当前流水标 VOID_DUPLICATE
            for lk in conn.execute("SELECT event_id, role FROM links WHERE txn_id = ?", (tid,)).fetchall():
                conn.execute(
                    "INSERT OR IGNORE INTO links (txn_id, event_id, role, created_at) VALUES (?, ?, ?, ?)",
                    (target_tid, lk["event_id"], lk["role"], ts),
                )
            merged_mer = str(target_row["merchant"] or "").strip() or str(row["merchant"] or "").strip()
            conn.execute(
                "UPDATE ledger_txns SET status='已对账', merchant=?, notes=?, updated_at=? WHERE txn_id=?",
                (merged_mer, f"已人工合并关联流水 {tid}", ts, target_tid),
            )
            conn.execute(
                "UPDATE ledger_txns SET status='VOID_DUPLICATE', parent_txn_id=?, notes=?, updated_at=? WHERE txn_id=?",
                (target_tid, f"人工配对归并至 {target_tid}", ts, tid),
            )
            summary = f"🔗 已将 `{tid}` 归并配对至 `{target_tid}`（主单升为已对账，副单作废防双算）。"

        db.record_human_use("ledger_reconcile", f"resolve_{act}", ref_id=tid, conn=conn)
        db.write_audit("A1", "ledger_resolve_txn", {"txn_id": tid, "action": act, "value": val[:80]}, target_id=tid, conn=conn)

    return {"ok": True, "txn_id": tid, "action": act, "summary": summary}


_CAT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "餐饮外卖(美团等)",
        re.compile(
            r"(美团|三快|饿了么|拉扎斯|麦当劳|肯德基|汉堡王|必胜客|星巴克|瑞幸|Manner|霸王茶姬|喜茶|茶百道|海底捞|农小锅|餐饮|火锅|烧烤|烤肉|外卖|面馆|餐厅|咖啡|奶茶|烘焙|美食)",
            re.IGNORECASE,
        ),
    ),
    (
        "打车出行(滴滴/停车等)",
        re.compile(
            r"(滴滴|小桔|高德打车|曹操出行|首汽|T3出行|打车|快车|专车|出租车|停车|ETCP|捷顺|加油|中石化|中石油|ETC|高速|12306|铁路|航空|机票|地铁|一卡通)",
            re.IGNORECASE,
        ),
    ),
    (
        "家装家电",
        re.compile(
            r"(林内|老板电器|美的|海尔|科沃斯|极米|小米之家|米家|绿米|Aqara|家电|家具|建材|五金|灯具|卫浴|橱柜|窗帘|装修|德邦|货拉拉)",
            re.IGNORECASE,
        ),
    ),
    (
        "AI与云研发",
        re.compile(
            r"(深度求索|DeepSeek|智谱|月之暗面|阿里云|腾讯云|火山引擎|华为云|Cloudflare|GitHub|OpenAI|Anthropic|Nous|Google|Apple|iCloud)",
            re.IGNORECASE,
        ),
    ),
    (
        "桌游文娱",
        re.compile(
            r"(桌游|BoardGame|BGA|Steam|任天堂|PlayStation|摩点|集换|大麦|猫眼|电影|图书|游戏)",
            re.IGNORECASE,
        ),
    ),
    (
        "商超日用",
        re.compile(
            r"(山姆|盒马|物美|永辉|超市|便利蜂|7-ELEVEn|罗森|全家|屈臣氏|京东|淘宝|天猫|拼多多|顺丰|菜鸟)",
            re.IGNORECASE,
        ),
    ),
)


def classify_cny_category(merchant: str) -> str:
    m = (merchant or "").strip()
    for label, pat in _CAT_RULES:
        if pat.search(m):
            return label
    return "其他日常消费"


def generate_monthly_ledger_report(db: SimingDB, year_month: str) -> str:
    """生成只说实话的月度钱账报告：按币种严格隔离统计（严禁 USD+CNY 直接相加），输出美团/滴滴等分类全景并剔除已消歧重复项。"""
    db.init_db()
    conn = db.connect()
    try:
        all_rows = conn.execute(
            "SELECT * FROM ledger_txns WHERE occurred_at LIKE ? ORDER BY occurred_at ASC",
            (f"{year_month}%",),
        ).fetchall()
    finally:
        conn.close()

    void_cnt = sum(1 for r in all_rows if r["status"] == "VOID_DUPLICATE")
    rows = [r for r in all_rows if r["status"] != "VOID_DUPLICATE"]

    cny_rows = [r for r in rows if (r["currency"] or "CNY").upper() == "CNY"]
    fx_by_curr: dict[str, list[Any]] = defaultdict(list)
    for r in rows:
        curr = (r["currency"] or "CNY").upper()
        if curr != "CNY":
            fx_by_curr[curr].append(r)

    spend_cents = sum(int(r["amount_cents"]) for r in cny_rows if r["kind"] == "消费")
    refund_cents = sum(int(r["amount_cents"]) for r in cny_rows if r["kind"] == "退款")
    transfer_cents = sum(int(r["amount_cents"]) for r in cny_rows if r["kind"] == "划转")
    income_cents = sum(int(r["amount_cents"]) for r in cny_rows if r["kind"] == "收入")
    net_spend_cents = spend_cents - refund_cents
    ambiguous_cnt = sum(1 for r in rows if r["status"] == "待核实")
    pending_cnt = sum(1 for r in rows if r["status"] == "PENDING")

    cat_totals: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [cents, count]
    for r in cny_rows:
        if r["kind"] == "消费":
            cat = classify_cny_category(str(r["merchant"] or ""))
            cat_totals[cat][0] += int(r["amount_cents"])
            cat_totals[cat][1] += 1

    accs = load_accounts_config(db.paths)
    covered: list[str] = []
    gaps: list[str] = []
    for acc in accs:
        alias = str(acc.get("alias") or "账户")
        thresh = int(acc.get("sms_threshold_yuan") or 0)
        v_thru = str(acc.get("verified_through") or "")
        if not acc.get("configured"):
            gaps.append(f"{alias}（尚未在 accounts.toml 填入真实尾号与月结单）")
        elif thresh > 0 and v_thru < f"{year_month}-28":
            gaps.append(f"{alias} 低于 ¥{thresh} 的无短信零星变动（待月结单补齐）")
        else:
            covered.append(f"{alias}" + (f"（核至 {v_thru}）" if v_thru else ""))

    subs = detect_recurring_subscriptions(db, reference_month=year_month)
    lines = [
        f"📊 {year_month} 钱账盘点（按币种隔离统计，绝不混加外币或虚报全量）",
        f"• 人民币净消费：¥{net_spend_cents/100:.2f}（消费 ¥{spend_cents/100:.2f} - 退款 ¥{refund_cents/100:.2f}）",
        f"• 工资/报销等入账：¥{income_cents/100:.2f}",
        f"• 卡间/还款划转（不计重复消费）：¥{transfer_cents/100:.2f}",
    ]
    if cat_totals:
        cat_parts = [
            f"{cat} ¥{vals[0]/100:.2f}({vals[1]}笔)"
            for cat, vals in sorted(cat_totals.items(), key=lambda kv: -kv[1][0])
        ]
        lines.append("• 人民币消费结构：" + " ｜ ".join(cat_parts))

    sym_map = {"USD": "$", "HKD": "HK$", "EUR": "€", "GBP": "£", "JPY": "JPY "}
    for curr, fx_rows in sorted(fx_by_curr.items()):
        fx_spend = sum(int(r["amount_cents"]) for r in fx_rows if r["kind"] == "消费")
        fx_refund = sum(int(r["amount_cents"]) for r in fx_rows if r["kind"] == "退款")
        fx_repay = sum(int(r["amount_cents"]) for r in fx_rows if r["kind"] == "划转")
        fx_spend_cnt = sum(1 for r in fx_rows if r["kind"] == "消费")
        sym = sym_map.get(curr, f"{curr} ")
        repay_note = f"，当月已还款 {sym}{fx_repay/100:.2f}" if fx_repay > 0 else ""
        lines.append(
            f"• 外币流水（{curr}，独立统计不混入人民币）：净消费 {sym}{(fx_spend - fx_refund)/100:.2f}（消费 {fx_spend_cnt} 笔{repay_note}，按实际购汇还款单结清）"
        )
    void_note = f"，已消歧剔除重复 {void_cnt} 笔" if void_cnt else ""
    lines.extend(
        [
            f"• 对账状态：有效流水 {len(rows)} 笔（待月结单确认 {pending_cnt} 笔，外币或同额歧义「待核实」 {ambiguous_cnt} 笔{void_note}）",
            "• 已覆盖账户：" + ("、".join(covered) if covered else "暂无完整配置账户"),
            "• 未覆盖缺口：" + ("；".join(gaps) if gaps else "无已知缺口"),
        ]
    )
    if subs:
        s_txt = "、".join(
            f"{s['merchant']}({sym_map.get(str(s.get('currency') or 'CNY').upper(), str(s.get('currency')) + ' ') if str(s.get('currency') or 'CNY').upper() != 'CNY' else '¥'}{s['amount_yuan']}/{int(s['interval_days'])}d)"
            for s in subs[:5]
        )
        lines.append(f"• 疑似周期自动续费：{s_txt}")
    return "\n".join(lines)
