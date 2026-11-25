"""账本历史先验概率推断与跨渠道碎片消费补盲器（F08 · ledger/enrich_priors.py）：
1. 从本机 5,866+ 笔历史真实流水（含微信/支付宝/抖音导出账单）自动统计 `(card_last4, amount_cents)` 与全局 `amount_cents` 的高频商户分布先验；
   - 例如 `¥9.90 (990 cents)` 在历史记录中 `>85%` 命中 `抖音小程序 / 点众小说 / 短篇解锁`，银行短信进来空白商户 `¥9.90` 时当场给出带命中率的透明推断注记（严守 D-06：只写 `notes` 推断标签，绝不伪造已核实 `merchant`，等月度账单或订单截图精确命中时再转正！）；
2. 当用户通过轻点背面投递抖音/拼多多订单截图，或同步美团/抖音商品明细时，按「精确金额 + 时间窗（且无同额多笔歧义）」自动回填 `PENDING` 银行流水的缺失商户名。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from datetime import timedelta
from typing import Any, Optional

from siming.db import SimingDB, iso_now, parse_iso

_NOVEL_KEYWORDS_RE = re.compile(
    r"(抖音小程序|点众|小说|短篇|书屋|书城|阅读|追阅|故事会|球文|畅听|酌翡|芥子文化|解锁整本|解锁单本|7天会员)"
)


def canonicalize_merchant_bucket(merchant: str) -> str:
    m = (merchant or "").strip()
    if not m:
        return ""
    if _NOVEL_KEYWORDS_RE.search(m):
        return "抖音小说/短剧小程序"
    if "滴滴" in m:
        return "滴滴出行"
    if "瑞幸" in m:
        return "瑞幸咖啡"
    if "便利蜂" in m:
        return "便利蜂"
    if "山姆" in m:
        return "山姆会员商店"
    if "NOUS RESEARCH" in m.upper():
        return "Nous Research API"
    if "Apple" in m or "iCloud" in m:
        return "Apple 订阅服务"
    if "抖音电商" in m or "抖音App" in m:
        return "抖音商城购物"
    return m.split("·")[0].strip()[:28]


def learn_amount_priors(
    db: SimingDB,
    min_samples: int = 5,
    min_share: float = 0.65,
) -> dict[str, dict[str, Any]]:
    """从已有带商户流水统计先验：`amount_cents` 及 `card_last4:amount_cents` -> 高概率品类。"""
    db.init_db()
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT card_last4, amount_cents, merchant
            FROM ledger_txns
            WHERE kind = '消费' AND amount_cents > 0 AND trim(merchant) != ''
            """
        ).fetchall()
    finally:
        conn.close()

    by_amt: dict[int, Counter[str]] = {}
    by_card_amt: dict[str, Counter[str]] = {}
    for r in rows:
        amt = int(r["amount_cents"])
        bucket = canonicalize_merchant_bucket(str(r["merchant"] or ""))
        if not bucket:
            continue
        by_amt.setdefault(amt, Counter())[bucket] += 1
        c4 = str(r["card_last4"] or "").strip()
        if c4:
            by_card_amt.setdefault(f"{c4}:{amt}", Counter())[bucket] += 1

    priors: dict[str, dict[str, Any]] = {}
    for key_prefix, table in (("amt", by_amt), ("card_amt", by_card_amt)):
        for k, cnt in table.items():
            total = sum(cnt.values())
            if total < min_samples:
                continue
            top_cat, top_n = cnt.most_common(1)[0]
            share = top_n / total
            if share >= min_share:
                priors[f"{key_prefix}:{k}"] = {
                    "category": top_cat,
                    "hits": top_n,
                    "total": total,
                    "probability": round(share, 3),
                }
    return priors


def infer_missing_merchant_hint(
    db: SimingDB,
    amount_cents: int,
    card_last4: str = "",
    priors: Optional[dict[str, dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    """对单笔无商户银行流水返回历史高概率推断（若无足够把握则返回 None）。"""
    p_map = priors if priors is not None else learn_amount_priors(db)
    c4 = (card_last4 or "").strip()
    if c4 and f"card_amt:{c4}:{amount_cents}" in p_map:
        return p_map[f"card_amt:{c4}:{amount_cents}"]
    return p_map.get(f"amt:{amount_cents}")


def enrich_pending_ledger_with_priors_and_orders(db: SimingDB, days: int = 30) -> dict[str, Any]:
    """扫描所有 `merchant == ''` 的 PENDING 消费流水：
    1. 先尝试与 `shop_order_items`（美团/抖音订单明细）做唯一无歧义（同额±30m内仅 1 笔）精确回填；
    2. 未精确命中时，用历史经验分布写入透明 `notes` 推断（如 `[历史推断: 抖音小说/短剧小程序 88%]`）。
    """
    db.init_db()
    priors = learn_amount_priors(db)
    ts = iso_now()
    conn = db.connect()
    try:
        blanks = [
            dict(r)
            for r in conn.execute(
                """
                SELECT txn_id, occurred_at, amount_cents, card_last4, notes
                FROM ledger_txns
                WHERE kind = '消费' AND trim(merchant) = ''
                ORDER BY occurred_at DESC
                LIMIT 200
                """
            ).fetchall()
        ]
        order_rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT platform, shop, ordered_at, order_amount_cents, item_name
                FROM shop_order_items
                WHERE order_amount_cents > 0
                ORDER BY ordered_at DESC
                LIMIT 500
                """
            ).fetchall()
        ]
    finally:
        conn.close()

    exact_matched = 0
    prior_annotated = 0
    blank_amt_counts = Counter(int(tx["amount_cents"]) for tx in blanks)

    with db.transaction() as c:
        for tx in blanks:
            amt = int(tx["amount_cents"])
            try:
                tx_dt = parse_iso(str(tx["occurred_at"]))
            except Exception:
                tx_dt = None

            # 1) 尝试与订单明细精确无歧义配对（严守 D-06：多笔候选宁留待核实不硬配）
            candidates = []
            candidates_72h = []
            if tx_dt is not None:
                for od in order_rows:
                    if int(od["order_amount_cents"]) != amt:
                        continue
                    try:
                        od_dt = parse_iso(str(od["ordered_at"]))
                    except Exception:
                        continue
                    gap_sec = abs((tx_dt - od_dt).total_seconds())
                    label = f"{od['platform']} · {od['shop'] or od['item_name']}"[:48]
                    if gap_sec <= 1800:
                        candidates.append(label)
                    elif od["platform"] == "douyin" and amt != 990 and blank_amt_counts[amt] == 1 and gap_sec <= 72 * 3600:
                        candidates_72h.append(label)
            uniq_cands = sorted(set(candidates)) or sorted(set(candidates_72h))
            if len(uniq_cands) == 1:
                c.execute(
                    "UPDATE ledger_txns SET merchant=?, status='MATCHED', updated_at=? WHERE txn_id=? AND trim(merchant)=''",
                    (uniq_cands[0], ts, tx["txn_id"]),
                )
                exact_matched += 1
                continue

            # 2) 历史经验先验透明标注（不篡改 merchant 列）
            cur_notes = str(tx.get("notes") or "")
            if "[历史推断:" in cur_notes:
                continue
            hint = infer_missing_merchant_hint(db, amt, str(tx.get("card_last4") or ""), priors=priors)
            if hint:
                pct = int(round(float(hint["probability"]) * 100))
                tag = f"[历史推断: {hint['category']} ({pct}% · {hint['hits']}/{hint['total']}笔)]"
                new_notes = f"{cur_notes} {tag}".strip()
                c.execute(
                    "UPDATE ledger_txns SET notes=?, updated_at=? WHERE txn_id=?",
                    (new_notes, ts, tx["txn_id"]),
                )
                prior_annotated += 1

    return {
        "ok": True,
        "blank_scanned": len(blanks),
        "exact_matched": exact_matched,
        "prior_annotated": prior_annotated,
        "learned_priors_count": len(priors),
    }


def summarize_novel_and_micro_spend(db: SimingDB, days: int = 30) -> dict[str, Any]:
    """汇总近 N 天抖音小说/短剧小程序消费笔数与总额（含已核实商户与高置信 ¥9.90 推断）。"""
    db.init_db()
    cutoff = iso_now(parse_iso(iso_now()) - timedelta(days=days))
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT amount_cents, merchant, notes, occurred_at
            FROM ledger_txns
            WHERE kind = '消费' AND currency = 'CNY' AND occurred_at >= ?
            ORDER BY occurred_at DESC
            """,
            (cutoff,),
        ).fetchall()
    finally:
        conn.close()

    confirmed_cents = 0
    confirmed_cnt = 0
    inferred_cents = 0
    inferred_cnt = 0
    for r in rows:
        m = str(r["merchant"] or "")
        nt = str(r["notes"] or "")
        amt = int(r["amount_cents"])
        if _NOVEL_KEYWORDS_RE.search(m):
            confirmed_cents += amt
            confirmed_cnt += 1
        elif not m.strip() and "抖音小说/短剧小程序" in nt:
            inferred_cents += amt
            inferred_cnt += 1

    total_yuan = (confirmed_cents + inferred_cents) / 100.0
    return {
        "days": days,
        "confirmed_count": confirmed_cnt,
        "confirmed_yuan": round(confirmed_cents / 100.0, 2),
        "inferred_count": inferred_cnt,
        "inferred_yuan": round(inferred_cents / 100.0, 2),
        "total_yuan": round(total_yuan, 2),
        "summary": (
            f"📖 近 {days} 天抖音小说/短剧小程序消费：共 **{confirmed_cnt + inferred_cnt}** 笔 · **¥{total_yuan:.2f}**"
            f"（已对账 {confirmed_cnt} 笔 ¥{confirmed_cents/100:.2f}"
            + (f"，另有银行短信先验推断 {inferred_cnt} 笔 ¥{inferred_cents/100:.2f}" if inferred_cnt else "")
            + "）"
        ),
    }
