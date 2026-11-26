"""P4 (#5) 人工纠偏（Correction）、留出集评测自进化门禁（LearnGate）、今日行为动态与补偿撤销、源头 ModelCall 计量汇总（learning_loop.py）。"""

from __future__ import annotations

import json
import secrets
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional

from siming.db import SimingDB, iso_now, logical_day_str, now_local

# 涉及以下高敏能力域的规则晋升必须由本人显式裁决
HUMAN_REQUIRED_CAPABILITIES: frozenset[str] = frozenset(
    {
        "meituan_pay",
        "payment",
        "approval",
        "permissions",
        "health",
        "health_cockpit",
        "med_care",
        "global_agent",
    }
)


# ---------------------------------------------------------------------------
# 1. Correction：人工纠偏立即按范围生效、跨重启与再采集持久、支持版本回滚
# ---------------------------------------------------------------------------
def add_correction(
    db: SimingDB,
    *,
    user_intent: str,
    target_object: str,
    field_name: str,
    new_value: str,
    old_value: str = "",
    scope: str = "exact",
    source: str = "qq",
    expires_at: str = "",
) -> dict[str, Any]:
    """记录一条带范围与版本的人工纠偏（如「这笔是工作餐」「这家只在公司点」「XX 停车场非订阅」）。"""
    db.init_db()
    ts = iso_now()
    cid = f"COR-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
    with db.transaction() as conn:
        prev = conn.execute(
            """
            SELECT corr_id, version, new_value FROM corrections
            WHERE target_object = ? AND field_name = ? AND scope = ? AND status = 'ACTIVE'
            ORDER BY version DESC LIMIT 1
            """,
            (target_object, field_name, scope),
        ).fetchone()
        ver = (int(prev["version"]) + 1) if prev else 1
        eff_old = old_value or (str(prev["new_value"]) if prev else "")
        if prev:
            conn.execute(
                "UPDATE corrections SET status='SUPERSEDED', superseded_by=?, updated_at=? WHERE corr_id=?",
                (cid, ts, prev["corr_id"]),
            )
        conn.execute(
            """
            INSERT INTO corrections (
                corr_id, user_intent, target_object, field_name, old_value, new_value,
                scope, source, version, status, superseded_by, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', '', ?, ?, ?)
            """,
            (cid, user_intent[:300], target_object[:160], field_name[:80], eff_old[:240], new_value[:240], scope[:40], source[:40], ver, expires_at, ts, ts),
        )
        db.record_human_use("correction", "add_correction", ref_id=cid, conn=conn)
        db.write_audit(
            "A1",
            "correction_applied",
            {"target_object": target_object, "field": field_name, "new_value": new_value, "scope": scope},
            target_id=cid,
            detail=user_intent[:240],
            conn=conn,
        )
        return dict(conn.execute("SELECT * FROM corrections WHERE corr_id = ?", (cid,)).fetchone())


def lookup_correction(
    db: SimingDB,
    *,
    target_object: str,
    field_name: str,
    scope: str = "",
) -> Optional[dict[str, Any]]:
    """采集、推荐或 Wiki 蒸馏前查询有效人工纠偏覆盖。"""
    conn = db.connect_readonly()
    try:
        if scope:
            row = conn.execute(
                """
                SELECT * FROM corrections
                WHERE target_object = ? AND field_name = ? AND scope IN (?, 'exact', 'global') AND status = 'ACTIVE'
                ORDER BY CASE scope WHEN ? THEN 0 WHEN 'exact' THEN 1 ELSE 2 END, version DESC
                LIMIT 1
                """,
                (target_object, field_name, scope, scope),
            ).fetchone()
        else:
            row = conn.execute(
                """
                SELECT * FROM corrections
                WHERE target_object = ? AND field_name = ? AND status = 'ACTIVE'
                ORDER BY version DESC LIMIT 1
                """,
                (target_object, field_name),
            ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def rollback_correction(db: SimingDB, corr_id: str) -> dict[str, Any]:
    """回滚某条纠偏并恢复上一版本（如有），保留追加式历史记录。"""
    db.init_db()
    ts = iso_now()
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM corrections WHERE corr_id = ?", (corr_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"correction_not_found:{corr_id}"}
        conn.execute("UPDATE corrections SET status='REVERTED', updated_at=? WHERE corr_id=?", (ts, corr_id))
        prev = conn.execute(
            "SELECT corr_id FROM corrections WHERE superseded_by = ? ORDER BY version DESC LIMIT 1",
            (corr_id,),
        ).fetchone()
        restored_id = ""
        if prev:
            restored_id = str(prev["corr_id"])
            conn.execute("UPDATE corrections SET status='ACTIVE', superseded_by='', updated_at=? WHERE corr_id=?", (ts, restored_id))
        db.write_audit("A1", "correction_reverted", {"corr_id": corr_id, "restored": restored_id}, target_id=corr_id, conn=conn)
    return {"ok": True, "reverted": corr_id, "restored_prev": restored_id}


# ---------------------------------------------------------------------------
# 2. LearnGate：candidate -> replay -> shadow -> promoted/declined -> reverted
# ---------------------------------------------------------------------------
def propose_learning_candidate(
    db: SimingDB,
    *,
    trigger_case: str,
    proposed_rule: str,
    target_capability: str,
    positive_examples: Optional[list[dict[str, Any]]] = None,
    negative_examples: Optional[list[dict[str, Any]]] = None,
    expected_metric: str = "",
    baseline_version: str = "",
    proposer: str = "system",
) -> dict[str, Any]:
    db.init_db()
    ts = iso_now()
    cid = f"LC-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
    need_human = target_capability.strip().lower() in HUMAN_REQUIRED_CAPABILITIES
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO learning_candidates (
                cand_id, trigger_case, proposed_rule, target_capability,
                positive_examples_json, negative_examples_json, expected_metric,
                baseline_version, proposer, requires_human_approval, state,
                holdout_passed, holdout_total, eval_report_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'candidate', 0, 0, '{}', ?, ?)
            """,
            (
                cid,
                trigger_case[:300],
                proposed_rule[:800],
                target_capability[:80],
                json.dumps(positive_examples or [], ensure_ascii=False),
                json.dumps(negative_examples or [], ensure_ascii=False),
                expected_metric[:160],
                baseline_version[:64],
                proposer[:64],
                1 if need_human else 0,
                ts,
                ts,
            ),
        )
        return dict(conn.execute("SELECT * FROM learning_candidates WHERE cand_id = ?", (cid,)).fetchone())


def replay_candidate_on_holdout(
    db: SimingDB,
    *,
    cand_id: str,
    holdout_cases: list[dict[str, Any]],
    evaluator_fn: Callable[[dict[str, Any], dict[str, Any]], bool],
    min_samples: int = 3,
    min_pass_rate: float = 0.8,
) -> dict[str, Any]:
    """在留出案例与真实纠偏集上离线回放评测候选规则：显式记录样本数与分母，数据不足留在 shadow。"""
    db.init_db()
    conn_r = db.connect_readonly()
    try:
        row = conn_r.execute("SELECT * FROM learning_candidates WHERE cand_id = ?", (cand_id,)).fetchone()
    finally:
        conn_r.close()
    if not row:
        return {"ok": False, "error": f"candidate_not_found:{cand_id}"}
    cand = dict(row)

    total = len(holdout_cases)
    passed = 0
    details: list[dict[str, Any]] = []
    for idx, case in enumerate(holdout_cases):
        ok_case = bool(evaluator_fn(cand, case))
        if ok_case:
            passed += 1
        details.append({"case_index": idx, "passed": ok_case})

    pass_rate = (passed / total) if total > 0 else 0.0
    enough_data = total >= max(1, int(min_samples))
    if not enough_data:
        next_state = "shadow"
        verdict = "insufficient_samples_hold_in_shadow"
    elif pass_rate >= float(min_pass_rate):
        next_state = "shadow"
        verdict = "ready_for_promotion"
    else:
        next_state = "declined"
        verdict = "holdout_failed"

    report = {
        "passed": passed,
        "total": total,
        "pass_rate": round(pass_rate, 4),
        "min_samples": min_samples,
        "min_pass_rate": min_pass_rate,
        "enough_data": enough_data,
        "verdict": verdict,
        "details": details,
    }
    ts = iso_now()
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE learning_candidates
            SET state = ?, holdout_passed = ?, holdout_total = ?, eval_report_json = ?, updated_at = ?
            WHERE cand_id = ?
            """,
            (next_state, passed, total, json.dumps(report, ensure_ascii=False), ts, cand_id),
        )
    return {"ok": True, "cand_id": cand_id, "state": next_state, "report": report}


def promote_candidate(
    db: SimingDB,
    *,
    cand_id: str,
    approved_by: str = "system",
    approved_scope: str = "trial",
) -> dict[str, Any]:
    """将通过留出集回放的候选晋升为带回滚版本的 PolicyVersion；高敏领域强制要求本人批准。"""
    db.init_db()
    ts = iso_now()
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM learning_candidates WHERE cand_id = ?", (cand_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"candidate_not_found:{cand_id}"}
        cand = dict(row)
        report = json.loads(cand.get("eval_report_json") or "{}")
        if not report.get("enough_data") or report.get("verdict") != "ready_for_promotion":
            return {"ok": False, "error": "candidate_not_ready_for_promotion", "state": cand["state"], "report": report}
        if int(cand.get("requires_human_approval") or 0) == 1 and approved_by != "human":
            return {"ok": False, "error": "human_approval_required", "target_capability": cand["target_capability"]}

        prev_v = conn.execute(
            "SELECT version_id FROM policy_versions WHERE target_capability = ? AND status = 'ACTIVE' ORDER BY deployed_at DESC LIMIT 1",
            (cand["target_capability"],),
        ).fetchone()
        prev_vid = str(prev_v["version_id"]) if prev_v else ""
        vid = f"PV-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
        conn.execute(
            """
            INSERT INTO policy_versions (
                version_id, cand_id, target_capability, rule_diff, eval_result_json,
                approved_scope, prev_version_id, status, deployed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?)
            """,
            (vid, cand_id, cand["target_capability"], cand["proposed_rule"], cand["eval_report_json"], approved_scope, prev_vid, ts),
        )
        conn.execute("UPDATE learning_candidates SET state='promoted', updated_at=? WHERE cand_id=?", (ts, cand_id))
        db.write_audit("A1", "policy_promoted", {"cand_id": cand_id, "version_id": vid, "approved_by": approved_by}, target_id=vid, conn=conn)
    return {"ok": True, "version_id": vid, "prev_version_id": prev_vid, "cand_id": cand_id}


def revert_policy_version(db: SimingDB, version_id: str) -> dict[str, Any]:
    db.init_db()
    ts = iso_now()
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM policy_versions WHERE version_id = ?", (version_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"version_not_found:{version_id}"}
        conn.execute("UPDATE policy_versions SET status='REVERTED' WHERE version_id=?", (version_id,))
        if row["cand_id"]:
            conn.execute("UPDATE learning_candidates SET state='reverted', updated_at=? WHERE cand_id=?", (ts, row["cand_id"]))
        db.write_audit("A1", "policy_reverted", {"version_id": version_id, "prev": row["prev_version_id"]}, target_id=version_id, conn=conn)
    return {"ok": True, "reverted": version_id, "prev_version_id": str(row["prev_version_id"] or "")}


# ---------------------------------------------------------------------------
# 3. 今日替我做了什么（Activity Feed）与逐项补偿撤销（Undo）
# ---------------------------------------------------------------------------
def get_today_activity_feed(
    db: SimingDB,
    *,
    day_prefix: Optional[str] = None,
    limit: int = 80,
) -> dict[str, Any]:
    """从真实业务转换、三态收据、Outbox 与人工纠偏生成「今日替我做了什么」，每条完成都可追溯证据。"""
    day_s = day_prefix or logical_day_str()
    like_pat = f"{day_s}%"
    conn = db.connect_readonly()
    try:
        intents = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM action_intents WHERE created_at LIKE ? ORDER BY created_at DESC LIMIT ?",
                (like_pat, limit),
            ).fetchall()
        ]
        outbox_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM outbox WHERE created_at LIKE ? ORDER BY created_at DESC LIMIT ?",
                (like_pat, limit),
            ).fetchall()
        ]
        corrs = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM corrections WHERE created_at LIKE ? ORDER BY created_at DESC LIMIT ?",
                (like_pat, limit),
            ).fetchall()
        ]
        pending_apps = [
            dict(r)
            for r in conn.execute(
                "SELECT token, action, target_id, status, expires_at, created_at FROM approvals WHERE status = 'PENDING' ORDER BY created_at DESC LIMIT 20"
            ).fetchall()
        ]
    finally:
        conn.close()

    done_items: list[dict[str, Any]] = []
    pending_human: list[dict[str, Any]] = []
    incomplete_items: list[dict[str, Any]] = []

    for it in intents:
        st = str(it.get("state") or "")
        entry = {
            "ts": it["created_at"],
            "kind": "action_intent",
            "id": it["intent_id"],
            "action": it["action"],
            "target": it["target_id"],
            "state": st,
            "authorization_source": it["approval_token"] or "direct",
            "summary": it["summary"] or f"{it['action']} ({st})",
            "evidence": json.loads(it.get("evidence_json") or "{}"),
            "external_order_id": it.get("external_order_id") or "",
            "undoable": st in ("PAID_VERIFIED", "SUBMITTING", "NEEDS_USER_ACTION"),
            "undo_kind": "order" if it["action"] == "meituan_submit_pay" else "none",
        }
        if st in ("PAID_VERIFIED", "VERIFIED", "PLAN_CONFIRMED"):
            done_items.append(entry)
        elif st in ("NEEDS_USER_ACTION",):
            pending_human.append(entry)
        else:
            incomplete_items.append(entry)

    for ob in outbox_rows:
        st = str(ob.get("status") or "")
        entry = {
            "ts": ob["created_at"],
            "kind": "notice",
            "id": ob["id"],
            "rule_id": ob["rule_id"],
            "state": ob.get("delivery_state") or st,
            "summary": str(ob.get("text") or "")[:120],
            "evidence": {"channel": ob["channel"], "sent_msg_id": ob.get("sent_msg_id") or ""},
            "undoable": st in ("QUEUED", "MORNING_QUEUE", "SENT"),
            "undo_kind": "queued_notice" if st in ("QUEUED", "MORNING_QUEUE") else "sent_notice",
        }
        if st == "SENT":
            done_items.append(entry)
        elif st in ("QUEUED", "MORNING_QUEUE", "SENDING"):
            incomplete_items.append(entry)

    for c in corrs:
        done_items.append(
            {
                "ts": c["created_at"],
                "kind": "correction",
                "id": c["corr_id"],
                "state": c["status"],
                "summary": f"纠偏 {c['target_object']}.{c['field_name']} -> {c['new_value']} ({c['scope']})",
                "evidence": {"old_value": c["old_value"], "version": c["version"]},
                "undoable": c["status"] == "ACTIVE",
                "undo_kind": "correction",
            }
        )

    for a in pending_apps:
        pending_human.append(
            {
                "ts": a["created_at"],
                "kind": "pending_approval",
                "id": a["token"],
                "action": a["action"],
                "target": a["target_id"],
                "state": a["status"],
                "summary": f"待确认审批卡 {a['token']} ({a['action']})",
                "evidence": {"expires_at": a["expires_at"]},
                "undoable": True,
                "undo_kind": "pending_approval",
            }
        )

    done_items.sort(key=lambda x: str(x.get("ts") or ""), reverse=True)
    return {
        "ok": True,
        "day": day_s,
        "done": done_items,
        "pending_human": pending_human,
        "incomplete": incomplete_items,
        "counts": {
            "done": len(done_items),
            "pending_human": len(pending_human),
            "incomplete": len(incomplete_items),
        },
    }


def undo_activity_item(
    db: SimingDB,
    *,
    undo_kind: str,
    target_id: str,
    note: str = "",
) -> dict[str, Any]:
    """逐项补偿撤销（追加历史、绝不抹除事实）：
    - queued_notice：撤回排队中的提醒；
    - correction：回滚到上一版本纠偏；
    - policy_version：回滚已推广规则版本；
    - pending_approval：作废待确认卡；
    - sent_notice：保留原已送达事实并追加一条更正通知；
    - order：登记平台取消/退款跟踪单（不假装已退款）。
    """
    db.init_db()
    ts = iso_now()
    kind = str(undo_kind or "").strip().lower()
    if kind == "correction":
        return rollback_correction(db, target_id)
    if kind == "policy_version":
        return revert_policy_version(db, target_id)
    if kind == "queued_notice":
        with db.transaction() as conn:
            cur = conn.execute(
                "UPDATE outbox SET status='WITHDRAWN', delivery_state='withdrawn', version=version+1 WHERE id=? AND status IN ('QUEUED', 'MORNING_QUEUE')",
                (target_id,),
            )
            db.write_audit("A1", "undo_queued_notice", {"outbox_id": target_id, "withdrawn": cur.rowcount}, target_id=target_id, conn=conn)
        return {"ok": True, "undo_kind": kind, "target_id": target_id, "withdrawn": int(cur.rowcount or 0)}
    if kind == "pending_approval":
        with db.transaction() as conn:
            cur = conn.execute(
                "UPDATE approvals SET status='INVALIDATED' WHERE token=? AND status='PENDING'",
                (target_id.strip().upper(),),
            )
            db.write_audit("A1", "undo_pending_approval", {"token": target_id}, target_id=target_id, conn=conn)
        return {"ok": True, "undo_kind": kind, "target_id": target_id, "invalidated": int(cur.rowcount or 0)}
    if kind == "sent_notice":
        corr_msg = note.strip() or f"更正：上一条消息（{target_id}）已由本人标记撤销，请以最新状态为准。"
        with db.transaction() as conn:
            db.write_audit("A1", "undo_sent_notice_append_correction", {"orig_outbox_id": target_id, "correction": corr_msg}, target_id=target_id, conn=conn)
        return {"ok": True, "undo_kind": kind, "target_id": target_id, "fact_preserved": True, "correction_text": corr_msg}
    if kind == "order":
        with db.transaction() as conn:
            cid = db.next_seq_id(conn, "S")
            conn.execute(
                """
                INSERT INTO cases (case_id, title, next_step, status, strength, category, rule_id, created_at, updated_at)
                VALUES (?, ?, '前往平台核验取消结果并跟踪退款到账', '待核实', 'SOFT', 'refund', 'order_undo', ?, ?)
                """,
                (cid, f"订单取消与退款跟踪（{target_id}）", ts, ts),
            )
            db.write_audit("A1", "undo_order_track_refund", {"intent_id": target_id, "case_id": cid}, target_id=target_id, conn=conn)
        return {"ok": True, "undo_kind": kind, "target_id": target_id, "refund_case_id": cid}
    return {"ok": False, "error": f"unsupported_undo_kind:{kind}"}


# ---------------------------------------------------------------------------
# 4. 源头 ModelCall 用量汇总（订阅额度 vs 现金花费分栏）
# ---------------------------------------------------------------------------
def summarize_model_calls(db: SimingDB, *, days: int = 7) -> dict[str, Any]:
    cutoff = iso_now(now_local() - timedelta(days=max(1, int(days))))
    conn = db.connect_readonly()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM usage WHERE ts >= ? ORDER BY ts DESC",
                (cutoff,),
            ).fetchall()
        ]
    finally:
        conn.close()

    by_billing = {"subscription": {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_cny": 0.0}, "cash": {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_cny": 0.0}}
    by_source: dict[str, int] = {}
    for r in rows:
        bk = str(r.get("billing_kind") or ("cash" if float(r.get("cost_cny") or 0.0) > 0 else "subscription"))
        if bk not in by_billing:
            by_billing[bk] = {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_cny": 0.0}
        b = by_billing[bk]
        b["calls"] += 1
        b["tokens_in"] += int(r.get("tokens_in") or 0)
        b["tokens_out"] += int(r.get("tokens_out") or 0)
        b["cost_cny"] = round(b["cost_cny"] + float(r.get("cost_cny") or 0.0), 6)
        ms = str(r.get("meter_source") or "estimated")
        by_source[ms] = by_source.get(ms, 0) + 1

    return {
        "ok": True,
        "days": days,
        "total_calls": len(rows),
        "by_billing_kind": by_billing,
        "by_meter_source": by_source,
    }
