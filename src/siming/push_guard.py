"""推送熔断与隐式降级（2026-11-23 · D-33）。

背景：11-22 v5 集成后 `sub_renew` 8 分钟连推 139 条 QQ；该规则 14 天推 140 次仅 1 次被处理，
却因为「已读不回不算拒绝」永远不会被降级。本模块在全机唯一出口 `router.send_qq_message`
里加三道纯代码闸门（不调模型）：

1. **单规则小时上限**：同一 rule 近 60 分钟已发出/已排入晨札的条数 ≥ 上限（默认 6，hard/urgent 20）
   → 本条及后续进入 `THROTTLED`（outbox 留痕不丢），冷却 `push_cooldown_minutes`（默认 180）。
2. **全局小时上限**：全部非系统规则近 60 分钟合计 ≥ 上限（默认 20）→ 非 hard/urgent 消息同样转 `THROTTLED`。
3. **隐式降级（QUIET）**：非 hard、非豁免规则在 14 天内推送 ≥ `implicit_demote_min_sent`（默认 15）
   且被处理率 < `implicit_demote_max_accept`（默认 5%）→ 规则置 `QUIET`：后续只进晨札汇总，
   **不停模块后台逻辑**（与 DEMOTED 不同，`is_module_enabled` 不看 QUIET）。

被拦下的消息统一按规则汇总成**一条**晨札条目（`ref_id = guard:<rule>`，新的替换旧的），
熔断/降级发生时只发一条说明（rule = `siming_guard`）。解除：QQ 发「恢复推送 <rule>」，
或 `python -m siming.push_guard restore <rule>`。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import sys
from datetime import datetime, timedelta
from typing import Any, Optional

from siming.db import SimingDB, iso_now, now_local, parse_iso

GUARD_RULE_ID = "siming_guard"

DEFAULTS: dict[str, Any] = {
    "push_rule_max_per_hour": 6,
    "push_rule_max_per_hour_hard": 20,
    "push_global_max_per_hour": 20,
    "push_cooldown_minutes": 180,
    "implicit_demote_min_sent": 15,
    "implicit_demote_max_accept": 0.05,
    "implicit_demote_grace_days": 14,
    # 用户显式要求的提醒、晨札、外部脚本（用户自己写的监控，按 D3-B 保持原样）与安全类通知不做隐式降级
    "implicit_demote_exempt": [
        "morning_brief",
        "wait_remind",
        "wait:*",
        "system",
        "manual",
        "ext_*",
        "bank_sms_alert",
        "tesla_*",
        "med_care",
        GUARD_RULE_ID,
    ],
    # 不计入全局小时上限的系统规则
    "push_global_exempt": ["morning_brief", GUARD_RULE_ID, "wait_remind", "wait:*"],
}

_ACTED_ACTIONS = ("已办", "改期", "acted")


def _cfg(policy: Optional[dict[str, Any]]) -> dict[str, Any]:
    g = dict(DEFAULTS)
    if policy:
        g.update({k: v for k, v in (policy.get("guardrails") or {}).items() if k in DEFAULTS})
    return g


def _match_any(rule_id: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(rule_id, p) for p in patterns)


def _count_recent(conn, since: str, rule_id: Optional[str] = None, exclude: Optional[list[str]] = None) -> int:
    if rule_id is not None:
        row = conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE rule_id = ? AND status IN ('SENT', 'MORNING_QUEUE') "
            "AND created_at >= ? AND ref_id NOT LIKE 'guard:%'",
            (rule_id, since),
        ).fetchone()
        return int(row[0] or 0)
    rows = conn.execute(
        "SELECT rule_id, COUNT(*) AS n FROM outbox WHERE status = 'SENT' AND created_at >= ? GROUP BY rule_id",
        (since,),
    ).fetchall()
    excl = exclude or []
    return sum(int(r["n"] or 0) for r in rows if not _match_any(str(r["rule_id"] or ""), excl))


def _rule_row(conn, rule_id: str):
    return conn.execute("SELECT * FROM rule_stats WHERE rule_id = ?", (rule_id,)).fetchone()


def _acted_14d(conn, rule_id: str, cutoff: str) -> int:
    fb = int(
        conn.execute(
            "SELECT COUNT(*) FROM feedback WHERE rule_id = ? AND ts >= ? AND action IN (?, ?, ?)",
            (rule_id, cutoff, *_ACTED_ACTIONS),
        ).fetchone()[0]
        or 0
    )
    fu = int(
        conn.execute(
            "SELECT COUNT(*) FROM feature_usage WHERE feature_id = ? AND actor = 'human' AND ts >= ?",
            (rule_id, cutoff),
        ).fetchone()[0]
        or 0
    )
    row = _rule_row(conn, rule_id)
    col = int(row["acted_14d"] or 0) if row else 0
    return max(fb, fu, col)


def _rollup_to_digest(
    conn,
    rule_id: str,
    kind: str,
    text: str,
    ref_id: str,
    dedup_hash: str,
    ts: str,
) -> dict[str, Any]:
    """被拦消息留痕（status=THROTTLED/QUIETED），并把该规则的汇总条目刷新为一条 MORNING_QUEUE。"""
    status = "THROTTLED" if kind == "throttle" else "QUIETED"
    ob_id = f"OB-G-{hashlib.sha1((dedup_hash + ts + kind).encode()).hexdigest()[:12]}"
    conn.execute(
        """
        INSERT OR IGNORE INTO outbox (id, ref_id, rule_id, text, send_at, status, channel, dedup_hash, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'qqbot', ?, ?)
        """,
        (ob_id, ref_id, rule_id, text, ts, status, dedup_hash, ts),
    )
    since = iso_now(now_local() - timedelta(hours=24))
    held = int(
        conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE rule_id = ? AND status IN ('THROTTLED', 'QUIETED') AND created_at >= ?",
            (rule_id, since),
        ).fetchone()[0]
        or 0
    )
    first_line = (text.strip().splitlines() or [""])[0][:60]
    label = "熔断暂扣" if kind == "throttle" else "静默降级"
    summary = f"🧯 [{rule_id}] 近 24h {label} {held} 条（最新：{first_line}）· 回「恢复推送 {rule_id}」解除"
    roll_ref = f"guard:{rule_id}"
    conn.execute(
        "UPDATE outbox SET status = 'WITHDRAWN' WHERE ref_id = ? AND status = 'MORNING_QUEUE'",
        (roll_ref,),
    )
    roll_id = f"OB-GR-{hashlib.sha1((roll_ref + ts).encode()).hexdigest()[:10]}"
    conn.execute(
        """
        INSERT OR REPLACE INTO outbox (id, ref_id, rule_id, text, send_at, status, channel, dedup_hash, created_at)
        VALUES (?, ?, ?, ?, ?, 'MORNING_QUEUE', 'qqbot', ?, ?)
        """,
        (roll_id, roll_ref, GUARD_RULE_ID, summary, ts, hashlib.sha256(summary.encode()).hexdigest()[:32], ts),
    )
    return {"outbox_id": ob_id, "held_24h": held}


def _maybe_implicit_demote(db: SimingDB, conn, rule_id: str, cfg: dict[str, Any], now: datetime) -> Optional[str]:
    """每规则每天最多评估一次；满足条件则置 QUIET 并返回一次性说明文本。"""
    if _match_any(rule_id, list(cfg["implicit_demote_exempt"])):
        return None
    row = _rule_row(conn, rule_id)
    if row is None or int(row["hard_rule"] or 0) or str(row["mode"]) not in ("ACTIVE", "TRIAL"):
        return None
    day_key = f"push_guard_demote_checked:{rule_id}"
    today = now.date().isoformat()
    if db.get_meta(day_key) == today:
        return None
    db.set_meta(day_key, today)
    grace_until = db.get_meta(f"push_guard_grace_until:{rule_id}")
    if grace_until:
        try:
            if parse_iso(grace_until) > now:
                return None
        except Exception:
            pass
    cutoff = iso_now(now - timedelta(days=14))
    sent = int(
        conn.execute(
            "SELECT COUNT(*) FROM outbox WHERE rule_id = ? AND status = 'SENT' AND created_at >= ?",
            (rule_id, cutoff),
        ).fetchone()[0]
        or 0
    )
    if sent < int(cfg["implicit_demote_min_sent"]):
        return None
    acted = _acted_14d(conn, rule_id, cutoff)
    rate = acted / max(1, sent)
    if rate >= float(cfg["implicit_demote_max_accept"]):
        return None
    ts = iso_now(now)
    with db.transaction() as wconn:
        wconn.execute("UPDATE rule_stats SET mode = 'QUIET', updated_at = ? WHERE rule_id = ?", (ts, rule_id))
        db.write_audit(
            "A1",
            "push_guard_implicit_quiet",
            {"rule_id": rule_id, "sent_14d": sent, "acted_14d": acted},
            target_id=rule_id,
            conn=wconn,
        )
    return (
        f"🔕 司命自动降级：「{rule_id}」近 14 天推送 {sent} 次，你处理了 {acted} 次（{rate:.0%}）。\n"
        f"之后这类消息只进晨札汇总，后台逻辑照常运行。\n"
        f"想恢复即时推送：回「恢复推送 {rule_id}」。"
    )


def guard_outbound(
    db: SimingDB,
    conn,
    *,
    rule_id: str,
    text: str,
    ref_id: str,
    dedup_hash: str,
    urgent: bool,
    policy: Optional[dict[str, Any]],
    now: Optional[datetime] = None,
) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """返回 (拦截结果或 None=放行, 需额外发送的一次性说明文本)。"""
    if rule_id == GUARD_RULE_ID:
        return None, None
    cfg = _cfg(policy)
    cur = now or now_local()
    ts = iso_now(cur)
    row = _rule_row(conn, rule_id)
    is_hard = bool(row and int(row["hard_rule"] or 0))
    elevated = is_hard or urgent

    # 0) 已处于 QUIET：非 hard/urgent 一律汇总进晨札
    notice: Optional[str] = None
    if row is not None and str(row["mode"]) == "QUIET" and not elevated:
        with db.transaction() as wconn:
            info = _rollup_to_digest(wconn, rule_id, "quiet", text, ref_id, dedup_hash, ts)
        return {"ok": True, "delivered": False, "skipped": "rule_quiet_digest", **info}, None

    # 1) 冷却中
    if row is not None and row["cooldown_until"] and not elevated:
        try:
            if parse_iso(str(row["cooldown_until"])) > cur:
                with db.transaction() as wconn:
                    info = _rollup_to_digest(wconn, rule_id, "throttle", text, ref_id, dedup_hash, ts)
                return {"ok": True, "delivered": False, "skipped": "rule_cooldown", **info}, None
        except Exception:
            pass

    since_1h = iso_now(cur - timedelta(hours=1))
    cap = int(cfg["push_rule_max_per_hour_hard"] if elevated else cfg["push_rule_max_per_hour"])
    rule_n = _count_recent(conn, since_1h, rule_id=rule_id)
    reason = ""
    if rule_n >= cap:
        reason = f"规则「{rule_id}」1 小时内已推 {rule_n} 条（上限 {cap}）"
    elif not elevated and not _match_any(rule_id, list(cfg["push_global_exempt"])):
        global_n = _count_recent(conn, since_1h, exclude=list(cfg["push_global_exempt"]))
        if global_n >= int(cfg["push_global_max_per_hour"]):
            reason = f"全机 1 小时内已推 {global_n} 条（上限 {cfg['push_global_max_per_hour']}）"

    if reason:
        cooldown_until = iso_now(cur + timedelta(minutes=int(cfg["push_cooldown_minutes"])))
        first_trip = True
        with db.transaction() as wconn:
            if row is not None and row["cooldown_until"]:
                try:
                    first_trip = parse_iso(str(row["cooldown_until"])) <= cur
                except Exception:
                    first_trip = True
            wconn.execute(
                """
                INSERT INTO rule_stats (rule_id, mode, cooldown_until, updated_at) VALUES (?, 'ACTIVE', ?, ?)
                ON CONFLICT(rule_id) DO UPDATE SET cooldown_until = excluded.cooldown_until, updated_at = excluded.updated_at
                """,
                (rule_id, cooldown_until, ts),
            )
            info = _rollup_to_digest(wconn, rule_id, "throttle", text, ref_id, dedup_hash, ts)
            if first_trip:
                db.write_audit("A1", "push_guard_throttle", {"rule_id": rule_id, "reason": reason}, target_id=rule_id, conn=wconn)
        if first_trip:
            notice = (
                f"🧯 司命推送熔断：{reason}。\n"
                f"后续 {int(cfg['push_cooldown_minutes'])} 分钟内同类消息暂扣（不丢），晨札汇总一条。\n"
                f"立即解除：回「恢复推送 {rule_id}」。"
            )
        return {"ok": True, "delivered": False, "skipped": "rate_limited", "reason": reason, **info}, notice

    # 2) 隐式降级评估（每规则每天一次）
    if not elevated:
        demote_notice = _maybe_implicit_demote(db, conn, rule_id, cfg, cur)
        if demote_notice:
            with db.transaction() as wconn:
                info = _rollup_to_digest(wconn, rule_id, "quiet", text, ref_id, dedup_hash, ts)
            return {"ok": True, "delivered": False, "skipped": "rule_implicit_quiet", **info}, demote_notice
    return None, None


def restore_rule(db: SimingDB, rule_id: str, grace_days: Optional[int] = None) -> dict[str, Any]:
    """解除熔断/静默：mode 回 ACTIVE、清冷却，并给一段宽限期（默认 14 天）不再隐式降级。"""
    db.init_db()
    rid = (rule_id or "").strip()
    if not rid:
        return {"ok": False, "error": "缺少规则名"}
    ts = iso_now()
    days = int(grace_days if grace_days is not None else DEFAULTS["implicit_demote_grace_days"])
    with db.transaction() as conn:
        row = _rule_row(conn, rid)
        if row is None:
            return {"ok": False, "error": f"没有规则「{rid}」"}
        prev = str(row["mode"])
        new_mode = "ACTIVE" if prev in ("QUIET", "ACTIVE", "TRIAL") else prev
        conn.execute(
            "UPDATE rule_stats SET mode = ?, cooldown_until = NULL, consecutive_dismiss = 0, updated_at = ? WHERE rule_id = ?",
            (new_mode, ts, rid),
        )
        db.set_meta(f"push_guard_grace_until:{rid}", iso_now(now_local() + timedelta(days=days)), conn=conn)
        conn.execute("UPDATE outbox SET status = 'WITHDRAWN' WHERE ref_id = ? AND status = 'MORNING_QUEUE'", (f"guard:{rid}",))
        db.write_audit("A1", "push_guard_restore", {"rule_id": rid, "prev_mode": prev}, target_id=rid, conn=conn)
    note = "" if new_mode == "ACTIVE" else f"（该规则是 {prev}，属于显式关闭，需用「开启 模块」恢复）"
    return {"ok": True, "rule_id": rid, "mode": new_mode, "prev_mode": prev, "reply": f"✅ 已恢复「{rid}」即时推送，{days} 天内不再自动降级{note}"}


def guard_status(db: SimingDB) -> dict[str, Any]:
    db.init_db()
    now = now_local()
    since = iso_now(now - timedelta(hours=24))
    conn = db.connect()
    try:
        quiet = [dict(r) for r in conn.execute("SELECT rule_id, sent_14d, acted_14d FROM rule_stats WHERE mode = 'QUIET'").fetchall()]
        cooling = [
            dict(r)
            for r in conn.execute(
                "SELECT rule_id, cooldown_until FROM rule_stats WHERE cooldown_until IS NOT NULL AND cooldown_until > ?",
                (iso_now(now),),
            ).fetchall()
        ]
        held = [
            dict(r)
            for r in conn.execute(
                "SELECT rule_id, status, COUNT(*) AS n FROM outbox WHERE status IN ('THROTTLED', 'QUIETED') AND created_at >= ? GROUP BY rule_id, status",
                (since,),
            ).fetchall()
        ]
    finally:
        conn.close()
    return {"quiet": quiet, "cooling": cooling, "held_24h": held}


def _main(argv: list[str]) -> int:
    db = SimingDB()
    if not argv or argv[0] == "status":
        print(json.dumps(guard_status(db), ensure_ascii=False, indent=2))
        return 0
    if argv[0] == "restore" and len(argv) >= 2:
        print(json.dumps(restore_rule(db, argv[1]), ensure_ascii=False, indent=2))
        return 0
    print("用法：python -m siming.push_guard [status | restore <rule_id>]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
