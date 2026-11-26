"""P4 控制塔只读查询投影层（tower_projections.py）：
所有只读投影统一使用 `db.connect_readonly()`，零 `init_db()` 写锁竞争，供控制塔 API 与 CLI 共用。
"""

from __future__ import annotations

import json
from typing import Any

from siming.apple_health import get_health_readiness_summary
from siming.db import SimingDB
from siming.learning_loop import get_today_activity_feed, summarize_model_calls
from siming.leases import list_active_leases
from siming.modules import list_modules_status


def receipts_projection(db: SimingDB, limit: int = 40) -> dict[str, Any]:
    conn = db.connect_readonly()
    try:
        intents = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM action_intents ORDER BY updated_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        ]
        for it in intents:
            recs = [
                dict(x)
                for x in conn.execute(
                    "SELECT receipt_id, stage, ok, external_ref, created_at FROM action_receipts WHERE intent_id=? ORDER BY created_at ASC",
                    (it["intent_id"],),
                ).fetchall()
            ]
            stages = {x["stage"] for x in recs if int(x["ok"] or 0) == 1}
            it["receipts"] = recs
            it["authorization_consumed"] = bool(it.get("auth_consumed_at") or "authorization_consumed" in stages)
            it["order_submitted"] = bool(it.get("submitted_at") or "order_submitted" in stages)
            it["paid_verified"] = bool("paid_verified" in stages or it.get("state") == "PAID_VERIFIED")
    finally:
        conn.close()
    return {"ok": True, "count": len(intents), "intents": intents}


def capabilities_projection(db: SimingDB) -> dict[str, Any]:
    mods = list_modules_status(db)
    by_readiness: dict[str, int] = {}
    for m in mods:
        r = str(m.get("readiness") or "live")
        by_readiness[r] = by_readiness.get(r, 0) + 1
    return {"ok": True, "total": len(mods), "by_readiness": by_readiness, "modules": mods}


def v6_overview_projection(db: SimingDB) -> dict[str, Any]:
    """司命 v6.2 综合控制塔只读投影：今日动态、三态收据、三维能力态、活跃资源租约、Apple Health 与真实 ModelCall 计量。"""
    return {
        "ok": True,
        "activity": get_today_activity_feed(db),
        "receipts": receipts_projection(db, limit=20),
        "capabilities": capabilities_projection(db),
        "leases": list_active_leases(db),
        "health_readiness": get_health_readiness_summary(db),
        "model_calls_7d": summarize_model_calls(db, days=7),
    }


def corrections_projection(db: SimingDB, limit: int = 50) -> dict[str, Any]:
    conn = db.connect_readonly()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM corrections ORDER BY updated_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        ]
        cands = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM learning_candidates ORDER BY updated_at DESC LIMIT 30"
            ).fetchall()
        ]
    finally:
        conn.close()
    return {"ok": True, "corrections": rows, "learning_candidates": cands}


def caldav_projection(db: SimingDB, limit: int = 50) -> dict[str, Any]:
    conn = db.connect_readonly()
    try:
        sync_states = [dict(r) for r in conn.execute("SELECT * FROM calendar_sync_state ORDER BY updated_at DESC").fetchall()]
        objects = [
            dict(r)
            for r in conn.execute(
                "SELECT obj_key, account, calendar, uid, summary, start_at, end_at, status, origin_ref, etag FROM calendar_objects WHERE status != 'CANCELLED' ORDER BY start_at ASC LIMIT ?",
                (int(limit),),
            ).fetchall()
        ]
    finally:
        conn.close()
    return {"ok": True, "sync_states": sync_states, "events": objects}
