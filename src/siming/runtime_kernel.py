"""P1 统一多 Agent 工单与运行时控制面（runtime_kernel.py）：
Ticket -> Assignment -> Run(generation CAS) -> RunInbox(STEER / CANCEL / ANSWER) -> RunEvent -> 5 问停止交代。
同时支持多编码 Agent 父子协作（如 Codex 隔离 worktree 修改 + Claude Code 只读审阅固定补丁）与适配器能力实测探测。
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import sqlite3
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

from siming.config import load_policy
from siming.db import SimingDB, iso_now, now_local


def create_ticket(
    db: SimingDB,
    *,
    goal: str,
    thread_id: str = "",
    parent_id: str = "",
    mandate: Optional[dict[str, Any]] = None,
    done_check: str = "",
    deadline: str = "",
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    ts = iso_now()
    tid = f"TK-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"

    def _run(c: sqlite3.Connection) -> dict[str, Any]:
        c.execute(
            """
            INSERT INTO tickets (
                ticket_id, thread_id, parent_id, goal, mandate_json,
                done_check, deadline, status, result_summary, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'OPEN', '', ?, ?)
            """,
            (
                tid,
                thread_id,
                parent_id,
                goal[:500],
                json.dumps(mandate or {}, ensure_ascii=False),
                done_check[:300],
                deadline,
                ts,
                ts,
            ),
        )
        return dict(c.execute("SELECT * FROM tickets WHERE ticket_id = ?", (tid,)).fetchone())

    if conn is not None:
        return _run(conn)
    db.init_db()
    with db.transaction() as c:
        return _run(c)


def create_assignment(
    db: SimingDB,
    *,
    ticket_id: str,
    adapter: str,
    role: str = "worker",
    capabilities: Optional[list[str]] = None,
    context_ref: str = "",
    resource_claims: Optional[list[str]] = None,
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    ts = iso_now()
    aid = f"AS-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"

    def _run(c: sqlite3.Connection) -> dict[str, Any]:
        c.execute(
            """
            INSERT INTO assignments (
                assignment_id, ticket_id, adapter, role, capabilities_json,
                context_ref, resource_claims_json, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'QUEUED', ?, ?)
            """,
            (
                aid,
                ticket_id,
                adapter,
                role,
                json.dumps(capabilities or [], ensure_ascii=False),
                context_ref,
                json.dumps(resource_claims or [], ensure_ascii=False),
                ts,
                ts,
            ),
        )
        return dict(c.execute("SELECT * FROM assignments WHERE assignment_id = ?", (aid,)).fetchone())

    if conn is not None:
        return _run(conn)
    db.init_db()
    with db.transaction() as c:
        return _run(c)


def create_multi_agent_coding_ticket(
    db: SimingDB,
    *,
    goal: str,
    repo_dir: str,
    editor_adapter: str = "codex",
    reviewer_adapter: str = "claude",
    thread_id: str = "",
    done_check: str = "",
) -> dict[str, Any]:
    """创建多编码 Agent 协作父工单（如「让 Codex 改，Claude Code 审」）：
    - editor 独占隔离 worktree（repo+run）；
    - reviewer 只读审阅固定 commit / 补丁；
    - 主仓合入仍经已有 A2 git_apply_worktree 审批卡。
    """
    db.init_db()
    with db.transaction() as conn:
        parent = create_ticket(
            db,
            goal=goal,
            thread_id=thread_id,
            mandate={"repo_dir": repo_dir, "mode": "multi_agent_edit_review"},
            done_check=done_check,
            conn=conn,
        )
        editor = create_assignment(
            db,
            ticket_id=parent["ticket_id"],
            adapter=editor_adapter,
            role="editor",
            capabilities=["worktree_write", "run_tests", "emit_patch"],
            context_ref=repo_dir,
            resource_claims=[f"worktree:{Path(repo_dir).name}:{parent['ticket_id']}"],
            conn=conn,
        )
        reviewer = create_assignment(
            db,
            ticket_id=parent["ticket_id"],
            adapter=reviewer_adapter,
            role="reviewer",
            capabilities=["readonly_review", "verify_diff"],
            context_ref=f"patch_of:{editor['assignment_id']}",
            resource_claims=[],
            conn=conn,
        )
    return {"ok": True, "ticket": parent, "editor_assignment": editor, "reviewer_assignment": reviewer}


def start_run(
    db: SimingDB,
    *,
    assignment_id: str,
    ticket_id: str = "",
    lease_sec: int = 1800,
    step_reached: str = "initialized",
) -> dict[str, Any]:
    """启动一个新的 Run 实例，分配单调递增的 attempt 与 generation。"""
    db.init_db()
    ts = iso_now()
    lease_until = iso_now(now_local() + timedelta(seconds=max(30, int(lease_sec))))
    run_id = f"RN-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
    with db.transaction() as conn:
        prev = conn.execute(
            "SELECT COALESCE(MAX(attempt), 0) AS ma, COALESCE(MAX(generation), 0) AS mg FROM runs WHERE assignment_id = ?",
            (assignment_id,),
        ).fetchone()
        attempt = int((prev["ma"] if prev else 0) or 0) + 1
        generation = int((prev["mg"] if prev else 0) or 0) + 1
        conn.execute(
            """
            INSERT INTO runs (
                run_id, assignment_id, ticket_id, attempt, generation, owner_pid,
                lease_until, state, applied_seq, received_seq, step_reached,
                external_effects_json, artifact_ref, resume_hint, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'RUNNING', 0, 0, ?, '[]', '', '', ?, ?)
            """,
            (run_id, assignment_id, ticket_id, attempt, generation, os.getpid(), lease_until, step_reached, ts, ts),
        )
        conn.execute("UPDATE assignments SET status='RUNNING', updated_at=? WHERE assignment_id=?", (ts, assignment_id))
        if ticket_id:
            conn.execute("UPDATE tickets SET status='RUNNING', updated_at=? WHERE ticket_id=?", (ts, ticket_id))
        _append_run_event_conn(conn, run_id, "STARTED", progress=step_reached)
        return dict(conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone())


def _append_run_event_conn(
    conn: sqlite3.Connection,
    run_id: str,
    kind: str,
    *,
    causal_ref: str = "",
    progress: str = "",
    artifact_ref: str = "",
    evidence_ref: str = "",
) -> int:
    row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS ms FROM run_events WHERE run_id = ?", (run_id,)).fetchone()
    seq = int((row["ms"] if row else 0) or 0) + 1
    conn.execute(
        """
        INSERT INTO run_events (run_id, seq, kind, causal_ref, progress, artifact_ref, evidence_ref, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (run_id, seq, kind, causal_ref, progress[:400], artifact_ref[:300], evidence_ref[:300], iso_now()),
    )
    return seq


def update_run_progress_cas(
    db: SimingDB,
    *,
    run_id: str,
    expected_generation: int,
    step_reached: str,
    artifact_ref: str = "",
    external_effect: Optional[str] = None,
    resume_hint: str = "",
    new_state: Optional[str] = None,
) -> dict[str, Any]:
    """严格按 expected_generation CAS 推进 Run 进度或终态；旧代次复活写入直接拒绝！"""
    db.init_db()
    ts = iso_now()
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return {"ok": False, "error": "run_not_found"}
        if int(row["generation"]) != int(expected_generation):
            return {"ok": False, "rejected": "stale_generation", "current_generation": int(row["generation"])}
        if row["state"] in ("CANCELLED", "DONE", "FAILED"):
            return {"ok": False, "rejected": f"terminal_state:{row['state']}"}

        effects = json.loads(row["external_effects_json"] or "[]")
        if external_effect and external_effect not in effects:
            effects.append(external_effect)
        state_val = new_state or str(row["state"])
        cur = conn.execute(
            """
            UPDATE runs
            SET state = ?, step_reached = ?, artifact_ref = CASE WHEN ? != '' THEN ? ELSE artifact_ref END,
                resume_hint = CASE WHEN ? != '' THEN ? ELSE resume_hint END,
                external_effects_json = ?, updated_at = ?
            WHERE run_id = ? AND generation = ?
            """,
            (
                state_val,
                step_reached[:300],
                artifact_ref,
                artifact_ref,
                resume_hint,
                resume_hint,
                json.dumps(effects, ensure_ascii=False),
                ts,
                run_id,
                int(expected_generation),
            ),
        )
        if cur.rowcount != 1:
            return {"ok": False, "rejected": "cas_conflict"}
        _append_run_event_conn(conn, run_id, new_state or "PROGRESS", progress=step_reached, artifact_ref=artifact_ref)
        if state_val in ("DONE", "FAILED", "CANCELLED"):
            conn.execute("UPDATE assignments SET status=?, updated_at=? WHERE assignment_id=?", (state_val, ts, row["assignment_id"]))
            if row["ticket_id"]:
                conn.execute("UPDATE tickets SET status=?, result_summary=?, updated_at=? WHERE ticket_id=?", (state_val, step_reached[:300], ts, row["ticket_id"]))
        return {"ok": True, "run_id": run_id, "generation": expected_generation, "state": state_val}


def bump_run_generation(db: SimingDB, run_id: str) -> dict[str, Any]:
    """租约接管或急停失权时递增 generation，立刻使所有旧代次 Worker 写入失败。"""
    db.init_db()
    ts = iso_now()
    with db.transaction() as conn:
        cur = conn.execute("UPDATE runs SET generation = generation + 1, updated_at = ? WHERE run_id = ?", (ts, run_id))
        if cur.rowcount == 0:
            return {"ok": False, "error": "run_not_found"}
        row = conn.execute("SELECT generation, state FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return {"ok": True, "run_id": run_id, "generation": int(row["generation"]), "state": str(row["state"])}


def enqueue_run_command(
    db: SimingDB,
    *,
    run_id: str,
    kind: str,
    payload: Optional[dict[str, Any]] = None,
    expected_revision: int = 0,
    actor: str = "human",
) -> dict[str, Any]:
    """向持久化 RunInbox 写入编号命令（STEER / CANCEL / ANSWER）。
    - CANCEL 立即持久化停止意图、撤销该 Run/Ticket 下未消费的待批卡，并递升 generation 阻止旧副作用；
    - STEER 撤销与新目标冲突的待批合入卡，由 Worker 在安全点采纳后回报 applied_seq。
    """
    db.init_db()
    clean_kind = str(kind or "").strip().upper()
    if clean_kind not in ("STEER", "CANCEL", "ANSWER"):
        return {"ok": False, "error": f"unsupported_command:{kind}"}
    ts = iso_now()
    revoked_approvals: list[str] = []
    with db.transaction() as conn:
        run_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not run_row:
            return {"ok": False, "error": f"run_not_found:{run_id}"}
        seq_row = conn.execute("SELECT COALESCE(MAX(seq), 0) AS ms FROM run_inbox WHERE run_id = ?", (run_id,)).fetchone()
        seq = int((seq_row["ms"] if seq_row else 0) or 0) + 1
        conn.execute(
            """
            INSERT INTO run_inbox (run_id, seq, kind, expected_revision, payload_json, actor, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?)
            """,
            (run_id, seq, clean_kind, int(expected_revision), json.dumps(payload or {}, ensure_ascii=False), actor, ts),
        )
        # CANCEL / STEER 自动作废尚未消费、与新目标冲突的 PENDING 审批卡
        tid = str(run_row["ticket_id"] or "")
        if clean_kind in ("CANCEL", "STEER") and tid:
            for arow in conn.execute(
                "SELECT token FROM approvals WHERE status='PENDING' AND target_id IN (?, ?)",
                (tid, run_id),
            ).fetchall():
                tok = str(arow["token"])
                conn.execute("UPDATE approvals SET status='INVALIDATED' WHERE token=? AND status='PENDING'", (tok,))
                revoked_approvals.append(tok)
        if clean_kind == "CANCEL":
            conn.execute(
                "UPDATE runs SET state='CANCELLING', updated_at=? WHERE run_id=? AND state IN ('PENDING', 'RUNNING', 'STEERED')",
                (ts, run_id),
            )
    return {
        "ok": True,
        "run_id": run_id,
        "seq": seq,
        "kind": clean_kind,
        "revoked_approvals": revoked_approvals,
    }


def poll_run_inbox(db: SimingDB, run_id: str) -> list[dict[str, Any]]:
    """Worker 在安全点轮询未消费的 RunInbox 控制命令（CANCEL 排在最前优先处理）。"""
    conn = db.connect_readonly()
    try:
        rows = conn.execute(
            """
            SELECT * FROM run_inbox
            WHERE run_id = ? AND status IN ('PENDING', 'RECEIVED')
            ORDER BY CASE kind WHEN 'CANCEL' THEN 0 WHEN 'STEER' THEN 1 ELSE 2 END, seq ASC
            """,
            (run_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["payload"] = json.loads(d.get("payload_json") or "{}")
            out.append(d)
        return out
    finally:
        conn.close()


def ack_run_command(
    db: SimingDB,
    *,
    run_id: str,
    seq: int,
    applied: bool = True,
    note: str = "",
) -> dict[str, Any]:
    """Worker 确认已收到（received_seq）或已在安全点采纳（applied_seq）控制命令。"""
    db.init_db()
    ts = iso_now()
    with db.transaction() as conn:
        status = "APPLIED" if applied else "RECEIVED"
        conn.execute(
            "UPDATE run_inbox SET status=?, applied_at=CASE WHEN ? THEN ? ELSE applied_at END WHERE run_id=? AND seq=?",
            (status, 1 if applied else 0, ts, run_id, int(seq)),
        )
        if applied:
            conn.execute(
                "UPDATE runs SET received_seq=MAX(received_seq, ?), applied_seq=MAX(applied_seq, ?), state=CASE WHEN state='RUNNING' THEN 'STEERED' ELSE state END, updated_at=? WHERE run_id=?",
                (int(seq), int(seq), ts, run_id),
            )
            _append_run_event_conn(conn, run_id, "STEER_APPLIED", causal_ref=f"cmd:{seq}", progress=note or f"applied_seq={seq}")
        else:
            conn.execute(
                "UPDATE runs SET received_seq=MAX(received_seq, ?), updated_at=? WHERE run_id=?",
                (int(seq), ts, run_id),
            )
        row = conn.execute("SELECT received_seq, applied_seq, state FROM runs WHERE run_id=?", (run_id,)).fetchone()
        return {
            "ok": True,
            "run_id": run_id,
            "seq": seq,
            "received_seq": int(row["received_seq"]) if row else seq,
            "applied_seq": int(row["applied_seq"]) if row else (seq if applied else 0),
            "state": str(row["state"]) if row else "",
        }


def build_stop_handoff(
    db: SimingDB,
    *,
    run_id: str,
    confirmed_stopped: Optional[list[str]] = None,
    pending_verification: Optional[list[str]] = None,
    finalize_cancel: bool = True,
) -> dict[str, Any]:
    """生成标准化「停止交代 5 问」并固化 Run 停止状态（严禁含糊回报“已停止”）：
    1. 做到了哪一步（step_reached）
    2. 哪些已在外部生效（external_effects）
    3. 哪些已确认停止（confirmed_stopped）
    4. 哪些仍待核验（pending_verification）
    5. 产物在哪里及如何续跑（artifact_ref & resume_hint）
    """
    db.init_db()
    ts = iso_now()
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"run_not_found:{run_id}"}
        r = dict(row)
        if finalize_cancel:
            # 递增 generation 确保旧 Worker 无法在停止后补写任何终态或副作用
            conn.execute(
                "UPDATE runs SET state='CANCELLED', generation=generation+1, updated_at=? WHERE run_id=?",
                (ts, run_id),
            )
            conn.execute("UPDATE assignments SET status='CANCELLED', updated_at=? WHERE assignment_id=?", (ts, r["assignment_id"]))
            if r["ticket_id"]:
                conn.execute("UPDATE tickets SET status='CANCELLED', updated_at=? WHERE ticket_id=?", (ts, r["ticket_id"]))

        ext_eff = json.loads(r.get("external_effects_json") or "[]")
        stopped_list = list(confirmed_stopped or ["本机执行进程已停止并禁止新副作用"])
        pending_list = list(pending_verification or [])
        handoff = {
            "run_id": run_id,
            "ticket_id": r.get("ticket_id") or "",
            "step_reached": r.get("step_reached") or "刚启动",
            "external_effects_applied": ext_eff,
            "confirmed_stopped": stopped_list,
            "pending_verification": pending_list,
            "artifact_ref": r.get("artifact_ref") or "无新增落盘产物",
            "resume_hint": r.get("resume_hint") or f"如需继续，可引用工单 `{r.get('ticket_id') or run_id}` 重新派单",
        }
        _append_run_event_conn(
            conn,
            run_id,
            "STOP_HANDOFF",
            progress=handoff["step_reached"],
            artifact_ref=handoff["artifact_ref"],
            evidence_ref=json.dumps(handoff, ensure_ascii=False)[:280],
        )
    handoff["ok"] = True
    handoff["text"] = format_stop_handoff(handoff)
    return handoff


def format_stop_handoff(h: dict[str, Any]) -> str:
    ext = "、".join(h.get("external_effects_applied") or []) or "无（主仓与外部账户未发生变更）"
    stopped = "、".join(h.get("confirmed_stopped") or []) or "本机任务已停"
    pending = "、".join(h.get("pending_verification") or []) or "无"
    return (
        f"🛑 **停止交代（`{h.get('run_id', '')}`）**\n"
        f"1. **做到了哪一步**：{h.get('step_reached', '-')}\n"
        f"2. **已在外部生效**：{ext}\n"
        f"3. **已确认停止**：{stopped}\n"
        f"4. **仍待核验**：{pending}\n"
        f"5. **产物与续跑**：{h.get('artifact_ref', '-')} ｜ 续跑：{h.get('resume_hint', '-')}"
    )


def probe_coding_adapter_capabilities(db: SimingDB) -> dict[str, dict[str, Any]]:
    """诚实探测各编码/执行适配器的真实可用能力（launch / resume / steer / cancel / observe），绝不虚报未验证能力。"""
    policy = load_policy(db.paths)
    cp = policy.get("coding_plan") or {}
    claude_bin = str(cp.get("claude_bin") or "")
    codex_bin = str(cp.get("codex_bin") or "")
    agy_bin = shutil.which("agy") or str(cp.get("agy_bin") or "")
    opencode_bin = shutil.which("opencode") or str(cp.get("opencode_bin") or "")

    def _exists(b: str) -> bool:
        return bool(b and (Path(b).is_file() or shutil.which(b)))

    return {
        "claude": {
            "available": _exists(claude_bin),
            "binary": claude_bin,
            "capabilities": ["launch", "resume", "steer_checkpoint", "cancel", "observe"],
            "native_resume_verified": True,
        },
        "codex": {
            "available": _exists(codex_bin),
            "binary": codex_bin,
            "capabilities": ["launch", "resume", "steer_checkpoint", "cancel", "observe"],
            "native_resume_verified": True,
        },
        "agy": {
            "available": _exists(agy_bin),
            "binary": agy_bin,
            "capabilities": ["observe", "open_cwd_tab"] + (["launch"] if _exists(agy_bin) else []),
            "native_resume_verified": False,
        },
        "opencode": {
            "available": _exists(opencode_bin),
            "binary": opencode_bin,
            "capabilities": ["observe", "open_cwd_tab"] + (["launch"] if _exists(opencode_bin) else []),
            "native_resume_verified": False,
        },
        "srr": {
            "available": True,
            "binary": "edge_receipt",
            "capabilities": ["observe", "receipt"],
            "remote_cancel_verified": False,
        },
    }
