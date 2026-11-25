"""外部生产者统一入口 `siming push`：让本机其它脚本（X 看门狗、Uptime Kuma 桥、免费游戏、备份失败、开机自检……）
不再各自 `hermes send`，而是经司命唯一出口 router 发 QQ，从而共享去重、作息暂扣、rule_stats 降级/冻结、
`siming feedback 少推/别再提`、outbox 审计与飞书兜底。

三档级别（与 attention.py 的 L0~L3 对齐，名字用人话避免和数字混淆）：
  • now    —— 全天穿透（L3 语义）：不受夜间作息暂扣，但仍尊重 rule_stats 的冻结/降级（hard 规则除外）；
  • normal —— 常规（L2）：清醒时即时送达，真睡着后转入 MORNING_QUEUE 随晨札/醒来一起给；
  • digest —— 只进晨札（L0/L1）：直接写 MORNING_QUEUE，同一 ref 只保留最新一条（旧的标 WITHDRAWN）。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from siming import db as _db_mod
from siming.crypto import redact_sensitive
from siming.db import SimingDB, iso_now, now_local, parse_iso

_ORIG_NOW_LOCAL = now_local


def _cur_now() -> datetime:
    return now_local() if now_local is not _ORIG_NOW_LOCAL else _db_mod.now_local()

KUMA_DEBOUNCE_SECONDS = 180
_KUMA_UP_RE = re.compile(r"(恢复|已恢复|正常|在线|\bup\b|recovered|🟢|✅)", re.IGNORECASE)
_KUMA_DOWN_RE = re.compile(r"(异常|故障|离线|宕机|不可达|超时|失败|\bdown\b|🔴|❌)", re.IGNORECASE)

LEVEL_ALIASES = {
    "now": "now",
    "urgent": "now",
    "l3": "now",
    "normal": "normal",
    "l2": "normal",
    "digest": "digest",
    "brief": "digest",
    "l1": "digest",
    "l0": "digest",
}


def normalize_level(level: str) -> str:
    key = (level or "normal").strip().lower()
    if key not in LEVEL_ALIASES:
        raise ValueError(f"未知级别 {level!r}，可选 now / normal / digest")
    return LEVEL_ALIASES[key]


def _ensure_rule(conn, rule_id: str, hard: bool) -> None:
    ts = iso_now()
    conn.execute(
        """
        INSERT INTO rule_stats (rule_id, mode, hard_rule, updated_at)
        VALUES (?, 'ACTIVE', ?, ?)
        ON CONFLICT(rule_id) DO UPDATE SET
            hard_rule = CASE WHEN excluded.hard_rule = 1 THEN 1 ELSE rule_stats.hard_rule END,
            updated_at = excluded.updated_at
        """,
        (rule_id, 1 if hard else 0, ts),
    )


def _rule_blocked(conn, rule_id: str) -> bool:
    row = conn.execute("SELECT mode, hard_rule FROM rule_stats WHERE rule_id = ?", (rule_id,)).fetchone()
    if not row or int(row["hard_rule"] or 0):
        return False
    return str(row["mode"]) in ("DEMOTED", "FROZEN", "OBSERVE")


def _queue_digest(
    db: SimingDB,
    text: str,
    rule_id: str,
    ref_id: str,
    dedup_window: timedelta,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    clean_text = redact_sensitive(text.strip(), db.paths)
    if not clean_text:
        return {"ok": False, "skipped": "empty"}
    cur = now or _cur_now()
    dedup_hash = hashlib.sha256(f"{ref_id}:{clean_text}".encode("utf-8")).hexdigest()[:32]
    ts = iso_now(cur)
    with db.transaction() as conn:
        if _rule_blocked(conn, rule_id):
            return {"ok": True, "delivered": False, "skipped": "rule_demoted_or_observe"}
        if dedup_window.total_seconds() > 0:
            dup = conn.execute(
                "SELECT id FROM outbox WHERE dedup_hash = ? AND status IN ('SENT', 'MORNING_QUEUE') AND created_at >= ?",
                (dedup_hash, iso_now(cur - dedup_window)),
            ).fetchone()
            if dup:
                return {"ok": True, "delivered": False, "skipped": "duplicate", "outbox_id": dup["id"]}
        withdrawn = 0
        if ref_id:
            withdrawn = conn.execute(
                "UPDATE outbox SET status='WITHDRAWN' WHERE ref_id = ? AND rule_id = ? AND status = 'MORNING_QUEUE'",
                (ref_id, rule_id),
            ).rowcount
        ob_id = f"OB-D-{hashlib.sha1((dedup_hash + ts).encode()).hexdigest()[:10]}"
        conn.execute(
            """
            INSERT INTO outbox (id, ref_id, rule_id, text, send_at, status, channel, dedup_hash, created_at)
            VALUES (?, ?, ?, ?, ?, 'MORNING_QUEUE', 'qqbot', ?, ?)
            """,
            (ob_id, ref_id, rule_id, clean_text, ts, dedup_hash, ts),
        )
    return {"ok": True, "delivered": False, "queued_for_morning": True, "outbox_id": ob_id, "superseded": withdrawn}


def _hold_for_sleep_at_now(
    db: SimingDB,
    text: str,
    rule_id: str,
    ref_id: str,
    dedup_window: timedelta,
    now: datetime,
) -> Optional[dict[str, Any]]:
    """当调用方显式传入 `now` 且该时刻主人处于睡眠/免打扰态时，将常规(normal/L2)消息暂存至 MORNING_QUEUE，防止墙钟漂移误吵醒。"""
    from siming.attention import evaluate_attention_state

    att = evaluate_attention_state(db, now_override=now)
    if not att.get("should_hold_l2"):
        return None
    clean_text = redact_sensitive(text.strip(), db.paths)
    if not clean_text:
        return {"ok": False, "skipped": "empty"}
    dedup_hash = hashlib.sha256(f"{ref_id}:{clean_text}".encode("utf-8")).hexdigest()[:32]
    ts = iso_now(now)
    with db.transaction() as conn:
        if _rule_blocked(conn, rule_id):
            return {"ok": True, "delivered": False, "skipped": "rule_demoted_or_observe"}
        if dedup_window.total_seconds() > 0:
            dup = conn.execute(
                "SELECT id FROM outbox WHERE dedup_hash = ? AND status IN ('SENT', 'MORNING_QUEUE') AND created_at >= ?",
                (dedup_hash, iso_now(now - dedup_window)),
            ).fetchone()
            if dup:
                return {"ok": True, "delivered": False, "skipped": "duplicate", "outbox_id": dup["id"]}
        ob_id = f"OB-Q-{hashlib.sha1((dedup_hash + ts).encode()).hexdigest()[:10]}"
        conn.execute(
            """
            INSERT INTO outbox (id, ref_id, rule_id, text, send_at, status, channel, dedup_hash, created_at)
            VALUES (?, ?, ?, ?, ?, 'MORNING_QUEUE', 'qqbot', ?, ?)
            """,
            (ob_id, ref_id, rule_id, clean_text, ts, dedup_hash, ts),
        )
    return {"ok": True, "delivered": False, "queued_for_morning": True, "outbox_id": ob_id}


def _classify_kuma_status(text: str) -> str:
    first = ((text or "").strip().splitlines() or [""])[0]
    if first.endswith("恢复") or (_KUMA_UP_RE.search(first) and not _KUMA_DOWN_RE.search(first)):
        return "up"
    if first.endswith("异常") or _KUMA_DOWN_RE.search(first):
        return "down"
    return "other"


def _handle_kuma_debounce(
    db: SimingDB,
    text: str,
    rule: str,
    ref: str,
    lvl: str,
    window: timedelta,
    keyboard: Optional[dict[str, Any]],
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """D3-B：仅对 ext_uptime_kuma 启用 3 分钟瞬断防抖（X 监控保持原样实时不动）。

    - 收到「异常」：暂存 180s（`kuma_debounce:<ref>`），返回 ok=True, skipped='debounced_3m'；
    - 180s 内收到「恢复」：静默抵消异常与恢复双推送（瞬断零打扰）；
    - 180s 后仍未恢复：由 `flush_debounced_kuma_alerts()` 在 tick 中真实外发异常，之后再收到「恢复」立即放行。
    """
    cur = now or now_local()
    state_key = f"kuma_debounce:{ref}"
    raw_st = db.get_meta(state_key)
    st: dict[str, Any] = {}
    if raw_st:
        try:
            parsed = json.loads(raw_st)
            if isinstance(parsed, dict):
                st = parsed
        except Exception:
            st = {}

    kind = _classify_kuma_status(text)
    if kind == "down":
        if st.get("state") == "SENT_DOWN":
            # 已发过异常仍持续异常：按普通 router 去重走
            return None
        if st.get("state") == "PENDING_DOWN":
            # 保持最早异常时刻，不无限延后 180s 告警
            st["text"] = redact_sensitive(text.strip(), db.paths)
            db.set_meta(state_key, json.dumps(st, ensure_ascii=False))
            return {"ok": True, "delivered": False, "skipped": "debounced_3m", "debounced": True, "ref_id": ref}
        due_at = cur + timedelta(seconds=KUMA_DEBOUNCE_SECONDS)
        rec = {
            "state": "PENDING_DOWN",
            "rule_id": rule,
            "ref_id": ref,
            "level": lvl,
            "text": redact_sensitive(text.strip(), db.paths),
            "dedup_sec": int(window.total_seconds()),
            "keyboard": keyboard,
            "first_seen_at": iso_now(cur),
            "due_at": iso_now(due_at),
        }
        db.set_meta(state_key, json.dumps(rec, ensure_ascii=False))
        return {"ok": True, "delivered": False, "skipped": "debounced_3m", "debounced": True, "due_at": rec["due_at"], "ref_id": ref}

    if kind == "up":
        if st.get("state") == "PENDING_DOWN":
            # 3 分钟内瞬断自愈：撤销挂起异常并静默吞掉成对恢复
            db.set_meta(state_key, json.dumps({"state": "IDLE", "last_suppressed_at": iso_now(cur)}, ensure_ascii=False))
            return {"ok": True, "delivered": False, "skipped": "flap_suppressed", "flap_suppressed": True, "ref_id": ref}
        # 之前确实发过真实异常（或无记录）：清理状态并放行恢复通知
        if st:
            db.set_meta(state_key, json.dumps({"state": "IDLE", "recovered_at": iso_now(cur)}, ensure_ascii=False))
        return None
    return None


def flush_debounced_kuma_alerts(
    db: SimingDB,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """在 evald.run_tick 中冲刷超过 3 分钟仍未恢复的 Uptime Kuma 真实异常。"""
    from siming.router import send_qq_message

    db.init_db()
    cur = now or now_local()
    conn = db.connect()
    try:
        rows = conn.execute("SELECT key, value FROM meta WHERE key LIKE 'kuma_debounce:%'").fetchall()
    finally:
        conn.close()

    flushed: list[str] = []
    for r in rows:
        key = str(r["key"])
        try:
            st = json.loads(r["value"] or "{}")
        except Exception:
            continue
        if not isinstance(st, dict) or st.get("state") != "PENDING_DOWN":
            continue
        due_s = str(st.get("due_at") or "")
        if not due_s:
            continue
        try:
            due_dt = parse_iso(due_s)
        except Exception:
            continue
        if cur < due_dt:
            continue

        ref_id = str(st.get("ref_id") or key.removeprefix("kuma_debounce:"))
        rule_id = str(st.get("rule_id") or "ext_uptime_kuma")
        lvl = str(st.get("level") or "now")
        text = str(st.get("text") or "").strip()
        dedup_sec = int(st.get("dedup_sec") or 120)
        kb = st.get("keyboard") if isinstance(st.get("keyboard"), dict) else None
        if not text:
            db.set_meta(key, json.dumps({"state": "IDLE"}, ensure_ascii=False))
            continue

        res = send_qq_message(
            db,
            text,
            ref_id=ref_id,
            rule_id=rule_id,
            bypass_quiet=(lvl == "now"),
            dedup_window=timedelta(seconds=max(0, dedup_sec)),
            sub_meta={"kind": "external_push", "level": lvl, "debounced": True},
            keyboard=kb,
        )
        if res.get("ok"):
            st["state"] = "SENT_DOWN"
            st["sent_at"] = iso_now(cur)
            db.set_meta(key, json.dumps(st, ensure_ascii=False))
            flushed.append(ref_id)
    return {"flushed": flushed, "count": len(flushed)}


def push_external(
    db: SimingDB,
    text: str,
    rule_id: str,
    ref_id: str = "",
    level: str = "normal",
    dedup_window: Optional[timedelta] = None,
    hard: bool = False,
    keyboard: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """外部脚本推送入口；返回 router 风格结果字典，额外带 level/rule_id。"""
    from siming.router import send_qq_message

    db.init_db()
    lvl = normalize_level(level)
    rule = (rule_id or "").strip()
    if not rule:
        raise ValueError("--rule 不能为空")
    rule = rule if rule.startswith("ext_") else f"ext_{rule}"
    ref = ref_id.strip() or f"push:{rule}"
    window = timedelta(hours=24) if dedup_window is None else dedup_window

    with db.transaction() as conn:
        _ensure_rule(conn, rule, hard)

    if rule == "ext_uptime_kuma" and lvl != "digest":
        deb = _handle_kuma_debounce(db, text, rule, ref, lvl, window, keyboard, now=now)
        if deb is not None:
            deb["level"] = lvl
            deb["rule_id"] = rule
            return deb

    if lvl == "digest":
        res = _queue_digest(db, text, rule, ref, window, now=now)
    else:
        held = _hold_for_sleep_at_now(db, text, rule, ref, window, now) if (lvl == "normal" and now is not None) else None
        if held is not None:
            res = held
        else:
            res = send_qq_message(
                db,
                text,
                ref_id=ref,
                rule_id=rule,
                bypass_quiet=(lvl == "now"),
                dedup_window=window,
                sub_meta={"kind": "external_push", "level": lvl},
                keyboard=keyboard,
            )
    res["level"] = lvl
    res["rule_id"] = rule
    return res


def push_message(
    db: SimingDB,
    text: str,
    rule_id: str,
    ref_id: str = "",
    level: str = "normal",
    dedup_window: Optional[timedelta] = None,
    hard: bool = False,
    keyboard: Optional[dict[str, Any]] = None,
    sub_meta: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """内部模块统一推送入口（保留原 rule_id，不加 ext_ 前缀；共享作息暂扣、去重与 outbox 审计）。"""
    from siming.router import send_qq_message

    db.init_db()
    lvl = normalize_level(level)
    rule = (rule_id or "system").strip()
    ref = ref_id.strip() or f"push:{rule}"
    window = timedelta(hours=24) if dedup_window is None else dedup_window

    with db.transaction() as conn:
        _ensure_rule(conn, rule, hard)

    if lvl == "digest":
        res = _queue_digest(db, text, rule, ref, window, now=now)
    else:
        held = _hold_for_sleep_at_now(db, text, rule, ref, window, now) if (lvl == "normal" and now is not None) else None
        if held is not None:
            res = held
        else:
            res = send_qq_message(
                db,
                text,
                ref_id=ref,
                rule_id=rule,
                bypass_quiet=(lvl == "now"),
                dedup_window=window,
                sub_meta=sub_meta,
                keyboard=keyboard,
            )
    res["level"] = lvl
    res["rule_id"] = rule
    return res
