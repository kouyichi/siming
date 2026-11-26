"""P2 唯一 Outbox 投递契约（NoticeRequested）与四原语统一提醒调度引擎（delivery_outbox.py）。

职责：
1. 业务领域只判断「什么事实值得提醒」，提交类型化 NoticeRequested；
2. 通知服务独占作息、冷却、合并、D-33 熔断与渠道选择，严格拆分 accepted / deferred / suppressed / delivered / acted；
3. 提供四原语提醒调度器（once / rrule / monitor[baselineValue] / goal），替代散落在 27 个文件的 58 个自造 meta 冷却键。
"""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from siming.db import SimingDB, iso_now, now_local, parse_iso


@dataclass(frozen=True)
class NoticeRequested:
    rule: str
    subject: str
    occurrence: str
    text: str
    revision: int = 1
    deadline: str = ""
    priority: str = "normal"  # urgent / now / normal / digest
    hard: bool = False
    dedup_window_sec: int = 86400
    evidence: dict[str, Any] = field(default_factory=dict)
    keyboard: Optional[dict[str, Any]] = None
    sub_meta: Optional[dict[str, Any]] = None


def submit_notice(
    db: SimingDB,
    notice: NoticeRequested,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """统一业务提醒提交口：返回明确拆分的 accepted / delivered / delivery_state，绝不混淆受理与手机送达。"""
    from siming.push import push_message

    ref_id = f"{notice.subject}:{notice.occurrence}:r{notice.revision}" if notice.occurrence else notice.subject
    level = "now" if notice.priority in ("urgent", "now") else ("digest" if notice.priority == "digest" else "normal")
    meta = dict(notice.sub_meta or {})
    if notice.evidence:
        meta["evidence"] = notice.evidence
    if notice.deadline:
        meta["deadline"] = notice.deadline

    res = push_message(
        db,
        text=notice.text,
        rule_id=notice.rule,
        ref_id=ref_id,
        level=level,
        dedup_window=timedelta(seconds=max(0, int(notice.dedup_window_sec))),
        hard=notice.hard,
        keyboard=notice.keyboard,
        sub_meta=meta,
        now=now,
    )
    delivered = bool(res.get("delivered") is True)
    state = str(res.get("delivery_state") or "")
    if not state:
        if delivered:
            state = "delivered"
        elif res.get("queued_for_morning") or level == "digest":
            state = "deferred"
        elif res.get("skipped") == "duplicate":
            state = "duplicate"
        elif res.get("ok"):
            state = "suppressed"
        else:
            state = "failed"
    return {
        **res,
        "accepted": bool(res.get("ok")),
        "delivered": delivered,
        "delivery_state": state,
        "ref_id": ref_id,
    }


def register_obligation(
    db: SimingDB,
    *,
    kind: str,
    rule_id: str,
    subject: str,
    next_fire_at: str = "",
    rrule: str = "",
    baseline: Optional[dict[str, Any]] = None,
    cooldown_sec: int = 3600,
    calendar_uid: str = "",
    manually_edited: bool = False,
    meta: Optional[dict[str, Any]] = None,
    ob_id: str = "",
) -> dict[str, Any]:
    """登记统一提醒/义务原语（kind ∈ {'once', 'rrule', 'monitor', 'goal'}）。"""
    clean_kind = str(kind or "once").strip().lower()
    if clean_kind not in ("once", "rrule", "monitor", "goal"):
        raise ValueError(f"unsupported obligation kind: {kind}")
    db.init_db()
    ts = iso_now()
    oid = ob_id.strip() or f"OBL-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO scheduled_obligations (
                ob_id, kind, rule_id, subject, rrule, next_fire_at, baseline_json,
                cooldown_sec, calendar_uid, manually_edited, status, meta_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?, ?)
            ON CONFLICT(ob_id) DO UPDATE SET
                kind = excluded.kind,
                rule_id = excluded.rule_id,
                subject = excluded.subject,
                rrule = excluded.rrule,
                next_fire_at = excluded.next_fire_at,
                baseline_json = excluded.baseline_json,
                cooldown_sec = excluded.cooldown_sec,
                calendar_uid = excluded.calendar_uid,
                manually_edited = excluded.manually_edited,
                meta_json = excluded.meta_json,
                updated_at = excluded.updated_at
            """,
            (
                oid,
                clean_kind,
                rule_id,
                subject[:300],
                rrule,
                next_fire_at,
                json.dumps(baseline or {}, ensure_ascii=False),
                max(0, int(cooldown_sec)),
                calendar_uid,
                1 if manually_edited else 0,
                json.dumps(meta or {}, ensure_ascii=False),
                ts,
                ts,
            ),
        )
        return dict(conn.execute("SELECT * FROM scheduled_obligations WHERE ob_id = ?", (oid,)).fetchone())


def _advance_rrule(rrule: str, current_dt: datetime) -> str:
    """轻量推进常见 RRULE（DAILY / WEEKLY / MONTHLY / INTERVAL=N），用于周期义务自动排下一期。"""
    upper = (rrule or "").upper()
    interval = 1
    for part in upper.split(";"):
        if part.startswith("INTERVAL="):
            try:
                interval = max(1, int(part.split("=", 1)[1]))
            except ValueError:
                interval = 1
    if "FREQ=DAILY" in upper:
        return iso_now(current_dt + timedelta(days=interval))
    if "FREQ=WEEKLY" in upper:
        return iso_now(current_dt + timedelta(days=7 * interval))
    if "FREQ=MONTHLY" in upper:
        month = current_dt.month - 1 + interval
        year = current_dt.year + month // 12
        month = month % 12 + 1
        day = min(current_dt.day, 28)
        return iso_now(current_dt.replace(year=year, month=month, day=day))
    if "FREQ=YEARLY" in upper:
        return iso_now(current_dt.replace(year=current_dt.year + interval))
    return ""


def evaluate_due_obligations(
    db: SimingDB,
    *,
    now: Optional[datetime] = None,
    monitor_probes: Optional[dict[str, Callable[[dict[str, Any]], Optional[dict[str, Any]]]]] = None,
    notify: bool = True,
) -> dict[str, Any]:
    """统一评估到期的 once / rrule / monitor(baselineValue) / goal 义务，仅在真实送达后记 last_delivered_at。"""
    db.init_db()
    cur = now or now_local()
    cur_iso = iso_now(cur)
    conn = db.connect_readonly()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM scheduled_obligations WHERE status = 'ACTIVE' ORDER BY next_fire_at ASC"
            ).fetchall()
        ]
    finally:
        conn.close()

    fired: list[dict[str, Any]] = []
    probes = monitor_probes or {}
    for ob in rows:
        oid = str(ob["ob_id"])
        kind = str(ob["kind"])
        cd = int(ob.get("cooldown_sec") or 0)
        last_deliv = str(ob.get("last_delivered_at") or "")
        if cd > 0 and last_deliv:
            if (cur - parse_iso(last_deliv)).total_seconds() < cd:
                continue

        should_fire = False
        notice_text = f"⏰ **{ob['subject']}**"
        new_baseline_json: Optional[str] = None

        if kind in ("once", "rrule", "goal"):
            nf = str(ob.get("next_fire_at") or "")
            if nf and parse_iso(nf) <= cur:
                should_fire = True
        elif kind == "monitor":
            probe_fn = probes.get(str(ob["rule_id"])) or probes.get(oid)
            if probe_fn is not None:
                base_obj = json.loads(ob.get("baseline_json") or "{}")
                probe_res = probe_fn(base_obj)
                if isinstance(probe_res, dict) and probe_res.get("triggered"):
                    should_fire = True
                    notice_text = str(probe_res.get("text") or notice_text)
                    if "new_baseline" in probe_res:
                        new_baseline_json = json.dumps(probe_res["new_baseline"], ensure_ascii=False)

        if not should_fire:
            continue

        res: dict[str, Any] = {"ok": True, "delivered": False, "delivery_state": "dry"}
        if notify:
            res = submit_notice(
                db,
                NoticeRequested(
                    rule=str(ob["rule_id"]),
                    subject=oid,
                    occurrence=cur_iso[:16],
                    text=notice_text,
                    priority=str(json.loads(ob.get("meta_json") or "{}").get("priority") or "normal"),
                    hard=bool(json.loads(ob.get("meta_json") or "{}").get("hard")),
                ),
                now=cur,
            )

        with db.transaction() as wconn:
            next_status = "ACTIVE"
            next_fire = str(ob.get("next_fire_at") or "")
            if kind == "once" and (res.get("delivered") or not notify):
                next_status = "DONE"
            elif kind == "rrule" and (res.get("delivered") or not notify):
                base_dt = parse_iso(next_fire) if next_fire else cur
                next_fire = _advance_rrule(str(ob.get("rrule") or ""), base_dt)
            wconn.execute(
                """
                UPDATE scheduled_obligations
                SET status = ?, next_fire_at = ?, last_fired_at = ?,
                    last_delivered_at = CASE WHEN ? THEN ? ELSE last_delivered_at END,
                    baseline_json = COALESCE(?, baseline_json),
                    updated_at = ?
                WHERE ob_id = ?
                """,
                (
                    next_status,
                    next_fire,
                    cur_iso,
                    1 if res.get("delivered") else 0,
                    cur_iso,
                    new_baseline_json,
                    cur_iso,
                    oid,
                ),
            )
        fired.append({"ob_id": oid, "kind": kind, "delivered": bool(res.get("delivered")), "delivery_state": res.get("delivery_state")})

    return {"ok": True, "checked": len(rows), "fired": fired}
