"""A2 级动作纯代码审批网关（preview -> apply <TOKEN>），严禁经过模型判断。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from siming.config import load_policy
from siming.db import SimingDB, current_boot_id, iso_now, now_local, parse_iso, pid_is_siming_process
from siming.receipts import (
    ActionResult,
    ReceiptSummary,
    approval_status_for_outcome,
    build_intent_idempotency_key,
    coerce_action_result,
    create_or_get_intent,
    format_receipt_status_line,
    get_intent_with_receipts,
    record_receipt_stage,
)


def _canonical_digest(params: dict[str, Any]) -> str:
    raw = json.dumps(params, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def expire_stale_approvals(
    db: SimingDB,
    now: Optional[Any] = None,
) -> int:
    """将已过期仍滞留 PENDING 状态的审批口令自动清理为 EXPIRED，防污染多卡消歧与列表。"""
    db.init_db()
    cur_iso = iso_now(now)
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE approvals SET status = 'EXPIRED' WHERE status = 'PENDING' AND expires_at < ?",
            (cur_iso,),
        )
        return int(cur.rowcount or 0)


def create_approval(
    db: SimingDB,
    action: str,
    target_id: str,
    params: dict[str, Any],
    state_version: int = 1,
    tier: str = "A2",
    ttl_minutes: Optional[int] = None,
    bundle_id: str = "",
) -> dict[str, Any]:
    policy = load_policy(db.paths)
    ttl = ttl_minutes if ttl_minutes is not None else int(policy["guardrails"]["approval_ttl_minutes"])
    created = now_local()
    expires = created + timedelta(minutes=ttl)
    digest = _canonical_digest(params)

    with db.transaction() as conn:
        token = db.next_seq_id(conn, "A")
        conn.execute(
            """
            INSERT INTO approvals (
                token, bundle_id, tier, action, target_id, params_json, params_digest,
                state_version, status, expires_at, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
            """,
            (
                token,
                bundle_id.strip(),
                tier,
                action,
                target_id,
                json.dumps(params, ensure_ascii=False, sort_keys=True),
                digest,
                state_version,
                iso_now(expires),
                iso_now(created),
            ),
        )
        db.write_audit(
            tier=tier,
            action=f"preview:{action}",
            params=params,
            target_id=target_id,
            approval_ref=token,
            detail=f"expires_at={iso_now(expires)} bundle={bundle_id}",
            conn=conn,
        )

    return {
        "token": token,
        "bundle_id": bundle_id.strip(),
        "action": action,
        "target_id": target_id,
        "params_digest": digest,
        "expires_at": iso_now(expires),
    }


def _exec_git_apply_worktree(params: dict[str, Any], db: SimingDB) -> str:
    repo_dir = Path(params["repo_dir"]).resolve()
    worktree_dir = Path(params["worktree_dir"]).resolve()
    expected_head = params.get("base_head", "")
    if not repo_dir.is_dir() or not worktree_dir.is_dir():
        raise RuntimeError(f"仓库或隔离目录已不存在: {worktree_dir}")

    if expected_head:
        cur_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if cur_head != expected_head:
            raise RuntimeError(
                f"主仓库 HEAD 已变化 ({expected_head[:8]} -> {cur_head[:8]})，拒绝盲目合入，请重新派单或手工 cherry-pick"
            )

    # 生成相对 base_head 的工作区完整补丁（含 Agent 在 worktree 内的 commit 与未提交新文件），强制校验不可变 patch_sha256
    subprocess.run(["git", "add", "-A"], cwd=str(worktree_dir), check=True, capture_output=True)
    diff_cmd = ["git", "diff", "--cached", "--binary", expected_head] if expected_head else ["git", "diff", "--cached", "--binary"]
    diff_proc = subprocess.run(
        diff_cmd,
        cwd=str(worktree_dir),
        check=True,
        capture_output=True,
    )
    patch_bytes = diff_proc.stdout
    expected_patch_sha = str(params.get("patch_sha256") or "").strip()
    actual_patch_sha = hashlib.sha256(patch_bytes).hexdigest()
    if not expected_patch_sha:
        raise RuntimeError("隔离工作区补丁摘要缺失，拒绝执行无摘要旧口令")
    if actual_patch_sha != expected_patch_sha:
        raise RuntimeError(
            f"隔离工作区补丁摘要与审批生成时不一致（expected={expected_patch_sha[:12]}, actual={actual_patch_sha[:12]}），拒绝合入"
        )
    if not patch_bytes.strip():
        return "隔离分支无任何文件改动，无需合入。"

    from siming.leases import hold_resource_lease, repo_ref_resource

    patch_file = db.paths.tmp_root / f"apply-{hashlib.sha256(patch_bytes).hexdigest()[:12]}.patch"
    with hold_resource_lease(
        db,
        resource_key=repo_ref_resource(repo_dir.name, "HEAD"),
        holder_run_id=f"git_apply:{os.getpid()}",
        mode="exclusive",
        ttl_sec=60,
    ):
        try:
            patch_file.write_bytes(patch_bytes)
            chk = subprocess.run(
                ["git", "apply", "--check", str(patch_file)],
                cwd=str(repo_dir),
                capture_output=True,
                text=True,
            )
            if chk.returncode != 0:
                raise RuntimeError(f"git apply --check 冲突: {chk.stderr.strip()}")
            subprocess.run(
                ["git", "apply", str(patch_file)],
                cwd=str(repo_dir),
                check=True,
                capture_output=True,
                text=True,
            )
        finally:
            patch_file.unlink(missing_ok=True)

    # 合入成功后清理临时 worktree
    subprocess.run(
        ["git", "worktree", "remove", "--force", str(worktree_dir)],
        cwd=str(repo_dir),
        capture_output=True,
    )
    shutil.rmtree(worktree_dir, ignore_errors=True)
    return f"已将改动合入 {repo_dir} 并清理隔离 worktree。"


def _exec_append_block_with_backup(params: dict[str, Any], db: SimingDB) -> str:
    target_path = Path(params["path"]).expanduser()
    marker = str(params["marker"])
    block = str(params["block"])
    existing = target_path.read_text(encoding="utf-8") if target_path.exists() else ""
    if marker in existing:
        return f"{target_path} 已包含该规则块，未重复写入。"
    stamp = now_local().strftime("%Y%m%dT%H%M%S")
    if target_path.exists():
        bak = db.paths.backup_dir / f"{target_path.name}.bak-{stamp}"
        bak.write_text(existing, encoding="utf-8")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    sep = "\n" if (existing and not existing.endswith("\n")) else ""
    target_path.write_text(existing + sep + "\n" + block.strip() + "\n", encoding="utf-8")
    return f"已备份原文件并写入 {target_path}。"


def _exec_didi_create_order(params: dict[str, Any], db: SimingDB) -> str:
    from siming.didi_mcp import execute_didi_create_order

    return execute_didi_create_order(params, db)


def _exec_luckin_confirm_order(params: dict[str, Any], db: SimingDB) -> str:
    from siming.luckin_mcp import execute_luckin_confirm_order

    return execute_luckin_confirm_order(params, db)


def _exec_meituan_lock_cart(params: dict[str, Any], db: SimingDB) -> str:
    from siming.meituan_agent import execute_meituan_lock_cart

    return execute_meituan_lock_cart(params, db)


def _exec_meituan_submit_pay(params: dict[str, Any], db: SimingDB) -> str:
    from siming.meituan_pay import execute_meituan_submit_pay

    tok = str(params.get("__approval_token__") or "").strip().upper()
    clean_p = {k: v for k, v in params.items() if k != "__approval_token__"}
    return execute_meituan_submit_pay(clean_p, db, approval_token=tok)


def _exec_jd_add_cart(params: dict[str, Any], db: SimingDB) -> str:
    from siming.replenish_cart import execute_jd_cart_add

    return execute_jd_cart_add(params, db)


def _exec_shift_expectation(params: dict[str, Any], db: SimingDB) -> str:
    exp_id = str(params.get("exp_id") or "").strip()
    new_at = str(params.get("new_target_at") or "").strip()
    draft_msg = str(params.get("draft_message") or "").strip()
    phone = str(params.get("counterparty_phone") or "").strip()
    title = str(params.get("title") or exp_id)
    ts = iso_now()
    with db.transaction() as conn:
        if exp_id:
            conn.execute(
                """
                UPDATE expectations
                SET target_at=?, status='SHIFTED', version=version+1, updated_at=?
                WHERE exp_id=?
                """,
                (new_at, ts, exp_id),
            )
        case_id = str(params.get("case_id") or "").strip()
        if case_id:
            conn.execute(
                "UPDATE cases SET due_at=?, next_step=?, state_version=state_version+1, updated_at=? WHERE case_id=?",
                (new_at[:10], f"已按事件链重排至 {new_at[:16]}", ts, case_id),
            )
    parts = [f"✅ 已将「{title}」重排顺延至 `{new_at[:16]}`。"]
    if phone:
        parts.append(f"📞 联系号码：`{phone}`")
    if draft_msg:
        parts.append(f"📋 可直接复制发给对方的话术：\n> {draft_msg}")
    return "\n".join(parts)


def _exec_compound_voice_batch(params: dict[str, Any], db: SimingDB) -> str:
    from siming.compound_voice import execute_compound_intents

    intents = params.get("intents") if isinstance(params.get("intents"), list) else []
    res = execute_compound_intents(db, intents)
    summary = str(res.get("reply") or res.get("summary") or f"已执行复合指令 {len(intents)} 项。")
    results = res.get("results") if isinstance(res.get("results"), list) else []
    failed_items = [r for r in results if isinstance(r, dict) and r.get("ok") is False]
    if failed_items:
        return ReceiptSummary(
            summary,
            ActionResult(
                outcome="submitted_pending" if len(failed_items) < len(results) else "failed",
                summary=summary,
                authorization_consumed=True,
                order_submitted=True,
                external_verified=False,
                evidence={"total": len(results), "failed_count": len(failed_items)},
                error=f"{len(failed_items)}/{len(results)} 子动作失败",
            ),
        )
    return ReceiptSummary(
        summary,
        ActionResult(
            outcome="verified",
            summary=summary,
            authorization_consumed=True,
            order_submitted=True,
            external_verified=True,
            evidence={"total": len(results), "failed_count": 0},
        ),
    )


def _exec_meituan_combo_order(params: dict[str, Any], db: SimingDB) -> str:
    shops = params.get("shops") or params.get("legs") or []
    names = " + ".join(str(s.get("shop") or s.get("query") or "") for s in shops if isinstance(s, dict))
    loc = str(params.get("location") or "默认位置")
    summary = (
        f"🍱 已确认跨店双拼方案（{names or params.get('query', '')} · 定位：{loc}）[仅方案确认，未触发外部扣款/下单]，"
        "请按各店子确认卡依次加购结算。"
    )
    return ReceiptSummary(
        summary,
        ActionResult(
            outcome="plan_only",
            summary=summary,
            authorization_consumed=True,
            order_submitted=False,
            external_verified=False,
            evidence={"mode": "plan_only", "shops": names, "location": loc},
        ),
    )


def _exec_jd_replenish_spool(params: dict[str, Any], db: SimingDB) -> str:
    url = str(params.get("jd_url") or params.get("url") or "").strip()
    if url:
        from siming.replenish_cart import execute_jd_cart_add

        return execute_jd_cart_add({"url": url, "name": params.get("name") or params.get("spool_id", "3D耗材")}, db)
    summary = f"🧵 已登记拓竹耗材补货方案（仅登记未下单）：{params.get('spool_id', '')}（{params.get('material', 'PLA')} {params.get('color', '')}）。"
    return ReceiptSummary(
        summary,
        ActionResult(
            outcome="plan_only",
            summary=summary,
            authorization_consumed=True,
            order_submitted=False,
            external_verified=False,
            evidence={"mode": "plan_only", "spool_id": params.get("spool_id", "")},
        ),
    )


def _exec_commute_rescue_bundle(params: dict[str, Any], db: SimingDB) -> str:
    reason = str(params.get("reason") or "极端天气/起晚")
    late_eta = str(params.get("late_eta_str") or "")
    delay_m = int(params.get("coffee_delay_min") or 0)
    summary = (
        f"🚨 已确认通勤应急预案（{reason}）[仅方案与草稿确认，未自动叫车/改单]：\n"
        f"• 🚕 滴滴叫车预估：¥{params.get('didi_fare_yuan', 0)}（需点叫车确认卡真发单）\n"
        f"• ☕ 咖啡取餐建议顺延 {delay_m} 分钟\n"
        f"• 💬 报备草稿：「路上稍堵/天气原因，预计 {late_eta or '稍晚'} 到司」"
    )
    return ReceiptSummary(
        summary,
        ActionResult(
            outcome="plan_only",
            summary=summary,
            authorization_consumed=True,
            order_submitted=False,
            external_verified=False,
            evidence={"mode": "plan_only", "reason": reason},
        ),
    )


def _exec_overnight_next_step(params: dict[str, Any], db: SimingDB) -> str:
    hid = str(params.get("handoff_id") or "")
    nxt = str(params.get("suggested_next") or params.get("action") or "继续推进")
    summary = f"🧪 已确认凌晨实验后续方案（仅方案确认，未自动改动远端训练）：`{hid}` -> `{nxt}`。"
    return ReceiptSummary(
        summary,
        ActionResult(
            outcome="plan_only",
            summary=summary,
            authorization_consumed=True,
            order_submitted=False,
            external_verified=False,
            evidence={"mode": "plan_only", "handoff_id": hid, "next": nxt},
        ),
    )


def _exec_runtime_dispatch_a2(params: dict[str, Any], db: SimingDB) -> str:
    """已授权的 A2 Runtime 派单执行器：持久化真实 Ticket + Assignment 并进入调度队列，绝不返回空洞假文案。"""
    import secrets

    ts = iso_now()
    rt = str(params.get("runtime") or "codex_sub")
    task_preview = str(params.get("task_preview") or params.get("task") or "").strip()
    cwd = str(params.get("cwd") or "")
    ticket_id = f"TK-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
    assign_id = f"AS-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO tickets (ticket_id, thread_id, parent_id, goal, mandate_json, status, created_at, updated_at)
            VALUES (?, '', '', ?, ?, 'OPEN', ?, ?)
            """,
            (ticket_id, task_preview[:300], json.dumps({"runtime": rt, "cwd": cwd, "a2_approved": True}, ensure_ascii=False), ts, ts),
        )
        conn.execute(
            """
            INSERT INTO assignments (assignment_id, ticket_id, adapter, role, context_ref, status, created_at, updated_at)
            VALUES (?, ?, ?, 'worker', ?, 'QUEUED', ?, ?)
            """,
            (assign_id, ticket_id, rt, cwd, ts, ts),
        )
    summary = f"✅ 已放行并持久化 A2 派单工单 `{ticket_id}`（Assignment `{assign_id}` · Runtime `{rt}` · 状态 `QUEUED`）。"
    return ReceiptSummary(
        summary,
        ActionResult(
            outcome="submitted_pending",
            summary=summary,
            external_order_id=ticket_id,
            authorization_consumed=True,
            order_submitted=True,
            external_verified=False,
            evidence={"ticket_id": ticket_id, "assignment_id": assign_id, "runtime": rt, "cwd": cwd},
        ),
    )


BUILTIN_APPROVAL_EXECUTORS: dict[str, Callable[[dict[str, Any], SimingDB], str]] = {
    "git_apply_worktree": _exec_git_apply_worktree,
    "append_block_with_backup": _exec_append_block_with_backup,
    "didi_create_order": _exec_didi_create_order,
    "luckin_confirm_order": _exec_luckin_confirm_order,
    "meituan_lock_cart": _exec_meituan_lock_cart,
    "meituan_submit_pay": _exec_meituan_submit_pay,
    "jd_add_cart": _exec_jd_add_cart,
    "shift_expectation": _exec_shift_expectation,
    "compound_voice_batch": _exec_compound_voice_batch,
    "meituan_combo_order": _exec_meituan_combo_order,
    "jd_replenish_spool": _exec_jd_replenish_spool,
    "commute_rescue_bundle": _exec_commute_rescue_bundle,
    "overnight_next_step": _exec_overnight_next_step,
    "overnight_handoff_next_step": _exec_overnight_next_step,
    "runtime_dispatch_a2": _exec_runtime_dispatch_a2,
}


def is_builtin_action_registered(action: str) -> bool:
    return str(action or "").strip() in BUILTIN_APPROVAL_EXECUTORS


def refresh_approval_ttl_on_delivery(
    db: SimingDB,
    text: str,
    conn: Optional[sqlite3.Connection] = None,
) -> list[str]:
    """当夜间暂存（MORNING_QUEUE）消息在晨间真正发出时，刷新其中 PENDING 审批口令的 30m 有效期。"""
    tokens = sorted({m.upper() for m in re.findall(r"\bapply\s+(A\d{2,6})\b", text or "", re.IGNORECASE)})
    if not tokens:
        return []
    policy = load_policy(db.paths)
    ttl = int(policy["guardrails"].get("approval_ttl_minutes", 30))
    new_exp = iso_now(now_local() + timedelta(minutes=ttl))
    refreshed: list[str] = []

    def _do(c: sqlite3.Connection) -> None:
        for tok in tokens:
            row = c.execute("SELECT status, expires_at FROM approvals WHERE token=?", (tok,)).fetchone()
            if not row or row["status"] != "PENDING":
                continue
            if parse_iso(row["expires_at"]) < parse_iso(new_exp):
                c.execute("UPDATE approvals SET expires_at=? WHERE token=? AND status='PENDING'", (new_exp, tok))
                refreshed.append(tok)

    if conn is not None:
        _do(conn)
    else:
        with db.transaction() as c:
            _do(c)
    return refreshed


def verify_and_apply_approval(
    db: SimingDB,
    token: str,
    extra_executors: Optional[dict[str, Callable[[dict[str, Any], SimingDB], str]]] = None,
) -> dict[str, Any]:
    """纯代码确定性原子认领（CAS）并执行 apply 口令（绑定对象、参数哈希、补丁哈希、状态版本与有效期）。"""
    clean_tok = token.strip().upper()
    executors = dict(BUILTIN_APPROVAL_EXECUTORS)
    if extra_executors:
        executors.update(extra_executors)

    reject_audit: Optional[tuple[str, dict[str, Any], str, int, str]] = None
    claimed_row: Optional[dict[str, Any]] = None
    params: dict[str, Any] = {}
    target_id = ""
    action = ""
    fn: Optional[Callable[[dict[str, Any], SimingDB], str]] = None

    conn = db.connect()
    try:
        with db.transaction(conn):
            row = conn.execute("SELECT * FROM approvals WHERE token = ?", (clean_tok,)).fetchone()
            if not row:
                reject_audit = ("A2", {"token": clean_tok}, "", 2, "口令不存在")
                err_msg = f"未找到审批口令 {clean_tok}。"
            elif row["status"] != "PENDING":
                reject_audit = (row["tier"], {"token": clean_tok}, str(row["target_id"]), 2, f"状态非法: {row['status']}")
                extra_st = "（关联项已取消 CANCELLED 或版本已变更）" if row["status"] == "INVALIDATED" else ""
                err_msg = f"口令 {clean_tok} 当前状态为 {row['status']}{extra_st}，不可重复使用。"
            elif parse_iso(row["expires_at"]) < now_local():
                conn.execute("UPDATE approvals SET status='EXPIRED' WHERE token=? AND status='PENDING'", (clean_tok,))
                reject_audit = (row["tier"], {"token": clean_tok}, str(row["target_id"]), 2, "口令已过期")
                err_msg = f"口令 {clean_tok} 已超过有效期（{row['expires_at']}），请重新生成预览。"
            else:
                target_id = str(row["target_id"])
                action = str(row["action"])
                fn = executors.get(action)
                try:
                    loaded_params = json.loads(row["params_json"])
                except Exception:
                    loaded_params = None
                if not isinstance(loaded_params, dict):
                    conn.execute("UPDATE approvals SET status='TAMPERED' WHERE token=? AND status='PENDING'", (clean_tok,))
                    reject_audit = (row["tier"], {"token": clean_tok}, target_id, 3, "参数JSON损坏或被篡改")
                    err_msg = f"口令 {clean_tok} 参数载荷损坏或被篡改，拒绝执行。"
                elif _canonical_digest(loaded_params) != row["params_digest"]:
                    conn.execute("UPDATE approvals SET status='TAMPERED' WHERE token=? AND status='PENDING'", (clean_tok,))
                    reject_audit = (row["tier"], {"token": clean_tok}, target_id, 3, "参数哈希被篡改")
                    err_msg = f"口令 {clean_tok} 参数摘要校验失败，拒绝执行。"
                elif fn is None:
                    params = loaded_params
                    reject_audit = (row["tier"], {"token": clean_tok}, target_id, 2, f"未知动作:{action}")
                    err_msg = f"未知审批动作类型: {action}"
                else:
                    params = loaded_params
                    err_msg = ""
                    if target_id.startswith("S"):
                        case_row = conn.execute(
                            "SELECT status, state_version FROM cases WHERE case_id = ?", (target_id,)
                        ).fetchone()
                        if case_row and case_row["status"] == "取消":
                            conn.execute("UPDATE approvals SET status='INVALIDATED' WHERE token=?", (clean_tok,))
                            err_msg = f"目标单据 {target_id} 已取消，旧口令作废。"
                        elif case_row and int(case_row["state_version"]) != int(row["state_version"]):
                            conn.execute("UPDATE approvals SET status='INVALIDATED' WHERE token=?", (clean_tok,))
                            err_msg = f"目标单据 {target_id} 状态已推进（v{row['state_version']} -> v{case_row['state_version']}），旧口令作废。"
                    elif target_id.startswith("W"):
                        wait_row = conn.execute(
                            "SELECT status FROM waits WHERE wait_id = ?", (target_id,)
                        ).fetchone()
                        if wait_row and wait_row["status"] in ("CANCELLED", "EXPIRED", "DONE"):
                            conn.execute("UPDATE approvals SET status='INVALIDATED' WHERE token=?", (clean_tok,))
                            err_msg = f"关联挂起项 {target_id} 已处于 {wait_row['status']} 状态，拒绝执行旧口令。"

                    if err_msg:
                        reject_audit = (row["tier"], {"token": clean_tok}, target_id, 2, err_msg)
                    else:
                        cur = conn.execute(
                            """
                            UPDATE approvals
                            SET status='CLAIMING', claimed_at=?, claimer_pid=?, claimer_boot_id=?
                            WHERE token=? AND status='PENDING'
                            """,
                            (iso_now(), os.getpid(), current_boot_id(), clean_tok),
                        )
                        if cur.rowcount != 1:
                            reject_audit = (row["tier"], {"token": clean_tok}, target_id, 2, "并发抢占未获锁")
                            err_msg = f"口令 {clean_tok} 已被并发请求认领，拒绝重复执行。"
                        else:
                            claimed_row = dict(row)

        if claimed_row is None or fn is None:
            if reject_audit:
                tier, p_obj, tid, ec, det = reject_audit
                db.write_audit(tier, "apply_rejected", p_obj, target_id=tid, approval_ref=clean_tok, exit_code=ec, detail=det)
            existing_intent = get_intent_with_receipts(db, approval_token=clean_tok)
            out_err: dict[str, Any] = {"ok": False, "error": err_msg}
            if existing_intent is not None:
                out_err["intent_id"] = existing_intent["intent_id"]
                out_err["intent"] = existing_intent
                out_err["idempotent_replay"] = True
            return out_err

        # P0 三态收据第 1 态：授权已消费（authorization_consumed），在执行外部动作之前独立落账
        idem_key = build_intent_idempotency_key(action, approval_token=clean_tok, target_id=target_id, params=params)
        amt_c = int(round(float(params.get("final_payable_yuan") or params.get("amount_yuan") or 0.0) * 100))
        max_c = int(round(float(params.get("max_authorized_yuan") or params.get("final_payable_yuan") or 0.0) * 100))
        with db.transaction(conn):
            intent, _ = create_or_get_intent(
                db,
                action=action,
                idempotency_key=idem_key,
                approval_token=clean_tok,
                target_id=target_id,
                amount_cents=amt_c,
                max_authorized_cents=max_c,
                quote=params if action == "meituan_submit_pay" else {},
                initial_state="AUTHORIZED",
                conn=conn,
            )
            record_receipt_stage(
                db,
                intent_id=intent["intent_id"],
                stage="authorization_consumed",
                approval_token=clean_tok,
                ok=True,
                summary=f"审批口令 {clean_tok} 已核验并消费授权",
                new_intent_state="AUTHORIZED",
                conn=conn,
            )
            conn.execute(
                "UPDATE approvals SET intent_id=?, receipt_stage='authorization_consumed' WHERE token=?",
                (intent["intent_id"], clean_tok),
            )

        exec_params = dict(params)
        if action == "meituan_submit_pay":
            exec_params["__approval_token__"] = clean_tok

        try:
            raw_res = fn(exec_params, db)
        except BaseException as exc:
            is_regular = isinstance(exc, Exception)
            fail_status = "FAILED" if is_regular else "FAILED_UNKNOWN"
            with db.transaction(conn):
                conn.execute("UPDATE approvals SET status=?, receipt_stage=? WHERE token=?", (fail_status, fail_status.lower(), clean_tok))
                record_receipt_stage(
                    db,
                    intent_id=intent["intent_id"],
                    stage="failed",
                    approval_token=clean_tok,
                    ok=False,
                    error=f"{type(exc).__name__}: {exc}",
                    new_intent_state=fail_status,
                    conn=conn,
                )
            db.write_audit(
                claimed_row["tier"],
                f"apply_failed:{action}",
                params,
                target_id=target_id,
                approval_ref=clean_tok,
                exit_code=1,
                detail=f"{type(exc).__name__}: {exc}",
            )
            if not is_regular:
                raise
            return {"ok": False, "error": f"执行失败：{exc}", "intent_id": intent["intent_id"]}

        act_res = coerce_action_result(action, raw_res)
        summary = act_res.summary or str(raw_res)
        app_status = approval_status_for_outcome(act_res.outcome)
        applied_ts = iso_now()
        with db.transaction(conn):
            if act_res.order_submitted:
                record_receipt_stage(
                    db,
                    intent_id=intent["intent_id"],
                    stage="order_submitted",
                    approval_token=clean_tok,
                    ok=True,
                    external_ref=act_res.external_order_id,
                    evidence=act_res.evidence,
                    summary=summary,
                    new_intent_state="SUBMITTED" if not act_res.external_verified else None,
                    conn=conn,
                )
            final_stage = (
                "paid_verified"
                if act_res.paid_verified
                else ("external_verified" if act_res.external_verified else act_res.outcome)
            )
            final_intent_state = {
                "paid_verified": "PAID_VERIFIED",
                "verified": "VERIFIED",
                "plan_only": "PLAN_CONFIRMED",
                "needs_user_action": "NEEDS_USER_ACTION",
                "submitted_pending": "RECONCILING",
                "failed": "FAILED",
            }.get(act_res.outcome, "VERIFIED")
            record_receipt_stage(
                db,
                intent_id=intent["intent_id"],
                stage=final_stage,
                approval_token=clean_tok,
                ok=(act_res.outcome != "failed"),
                external_ref=act_res.external_order_id,
                evidence=act_res.evidence,
                summary=summary,
                new_intent_state=final_intent_state,
                error=act_res.error,
                conn=conn,
            )
            conn.execute(
                "UPDATE approvals SET status=?, applied_at=?, receipt_stage=? WHERE token=?",
                (app_status, applied_ts, final_stage, clean_tok),
            )
            if target_id.startswith("W") and act_res.outcome in ("verified", "paid_verified", "plan_only"):
                conn.execute(
                    "UPDATE waits SET status='DONE', result_summary=?, updated_at=? WHERE wait_id=? AND status NOT IN ('CANCELLED')",
                    (summary, applied_ts, target_id),
                )
            db.record_human_use("approval_apply", "apply", ref_id=clean_tok, conn=conn)
            db.write_audit(
                claimed_row["tier"],
                f"applied:{action}",
                params,
                target_id=target_id,
                approval_ref=clean_tok,
                exit_code=0 if act_res.outcome != "failed" else 1,
                detail=f"[{final_stage}] {summary}",
                conn=conn,
            )
        if (action == "jd_add_cart" and act_res.external_verified) or (action == "meituan_submit_pay" and act_res.paid_verified):
            try:
                from siming.replenish_cart import on_approval_applied_for_replenish

                on_approval_applied_for_replenish(db, clean_tok)
            except Exception:
                pass
        return {
            "ok": act_res.outcome != "failed",
            "token": clean_tok,
            "target_id": target_id,
            "summary": summary,
            "approval_status": app_status,
            "intent_id": intent["intent_id"],
            "outcome": act_res.outcome,
            "receipt": act_res.to_dict(),
            "receipt_line": format_receipt_status_line(act_res),
        }
    finally:
        conn.close()


def reconcile_stuck_claiming_approvals(db: SimingDB, max_age_sec: int = 120) -> list[str]:
    """回收因外部强杀（SIGKILL/网关超时/断电）卡在 CLAIMING 的口令，转入 FAILED_UNKNOWN 并告警核对现场。"""
    db.init_db()
    now = now_local()
    ts = iso_now(now)
    conn = db.connect()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM approvals WHERE status='CLAIMING'").fetchall()]
    finally:
        conn.close()

    recovered: list[str] = []
    for r in rows:
        claimed_str = str(r.get("claimed_at") or r["created_at"])
        age = (now - parse_iso(claimed_str)).total_seconds()
        pid = r.get("claimer_pid")
        boot_id = str(r.get("claimer_boot_id") or "")
        alive = pid_is_siming_process(pid, expected_boot_id=boot_id)
        if alive and age <= max_age_sec:
            continue
        if alive and age > 600:
            alive = False
        if alive:
            continue
        tok = str(r["token"])
        tid = str(r.get("target_id") or "")
        with db.transaction() as c:
            cur = c.execute(
                "UPDATE approvals SET status='FAILED_UNKNOWN' WHERE token=? AND status='CLAIMING'",
                (tok,),
            )
            if cur.rowcount == 0:
                continue
            db.enqueue_effect(
                c,
                "send_qq",
                {
                    "ref_id": tid or tok,
                    "rule_id": "watchdog_alert",
                    "text": f"⚠️ 审批口令 `{tok}`（关联 {tid or '-'}）在执行途中进程异常中断，状态已置为 `FAILED_UNKNOWN`，请检查主仓库 `git status` 确认补丁是否已落盘后再决定下一步。",
                },
            )
            db.write_audit(
                str(r.get("tier") or "A2"),
                "claiming_recovered_unknown",
                {"token": tok, "claimer_pid": pid},
                target_id=tid,
                approval_ref=tok,
                exit_code=1,
                detail="claimer process gone while CLAIMING",
                conn=c,
            )
        recovered.append(tok)
    return recovered


NATURAL_CONFIRM_RE = re.compile(r"^(?:确认|发吧|好|好的|行|可以|1)$")
PAYMENT_CONFIRM_RE = re.compile(r"^(?:确认支付|确认付款|确认下单|去支付|立即支付|确认并支付)$")
REAL_MONEY_PAY_ACTIONS = frozenset({"meituan_submit_pay"})
PAYMENT_ORDER_ACTIONS = frozenset({"meituan_submit_pay", "luckin_confirm_order"})


def resolve_and_apply_natural_confirm(
    db: SimingDB,
    text: str,
    quoted_ref: str = "",
    extra_executors: Optional[dict[str, Callable[[dict[str, Any], SimingDB], str]]] = None,
) -> dict[str, Any]:
    """D-19 纯代码自然确认与实付许可校验（零模型介入）：
    - 普通确认卡支持 `确认 / 发吧 / 好 / 好的 / 行 / 可以 / 1`；
    - 涉及真实扣款的 `meituan_submit_pay` 严禁被未引用的随口 `好/好的/行/可以/1/发吧/确认` 误触发：
      仅放行显式付款口令（`确认支付 / 确认付款 / 确认下单 / 去支付 / 立即支付 / 确认并支付`），
      或明确引用该确认卡并回复 `确认 / 支付 / 付款 / 同意 / apply`；
    - 当存在 >=2 张独立未过期确认卡且未引用回复时，纯代码列出编号请本人点明，绝不盲猜！
    """
    clean = re.sub(r"^\s*\[Voice\]\s*", "", (text or "").strip(), flags=re.IGNORECASE).strip()
    clean = re.sub(r"[。！!~～\s]+$", "", clean).strip()
    q_ref = (quoted_ref or "").strip()
    is_explicit_pay = bool(PAYMENT_CONFIRM_RE.match(clean))
    is_natural_confirm = bool(NATURAL_CONFIRM_RE.match(clean))
    is_quoted_pay_word = bool(q_ref and clean.lower() in ("确认", "支付", "付款", "同意", "apply"))
    if not (is_explicit_pay or is_natural_confirm or is_quoted_pay_word):
        return {"matched": False}

    db.init_db()
    now = now_local()
    conn = db.connect()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM approvals WHERE status='PENDING' ORDER BY created_at ASC"
            ).fetchall()
        ]
    finally:
        conn.close()

    active_rows = [r for r in rows if parse_iso(str(r["expires_at"])) >= now]
    if is_explicit_pay:
        pay_rows = [r for r in active_rows if str(r.get("action") or "") in PAYMENT_ORDER_ACTIONS]
        if not pay_rows:
            return {
                "matched": True,
                "ok": False,
                "reply": "当前没有有效期内（10 分钟）的待付款订单确认卡，未产生扣款。",
            }
        active_rows = pay_rows

    if not active_rows:
        return {"matched": False}

    chosen: list[dict[str, Any]] = []
    quoted_matched = False
    if q_ref:
        q_up = q_ref.upper()
        matched_bundle = next(
            (str(r.get("bundle_id") or "") for r in active_rows if r["token"] == q_up or str(r.get("bundle_id") or "").upper() == q_up or str(r.get("target_id") or "").upper() == q_up),
            None,
        )
        if matched_bundle:
            chosen = [r for r in active_rows if str(r.get("bundle_id") or "") == matched_bundle]
        else:
            chosen = [
                r
                for r in active_rows
                if r["token"] == q_up or str(r.get("target_id") or "").upper() == q_up
            ]
        quoted_matched = bool(chosen)

    if not chosen:
        # 按捆绑组聚合：无 bundle_id 的单卡各自独立为一组
        groups: dict[str, list[dict[str, Any]]] = {}
        for r in active_rows:
            bid = str(r.get("bundle_id") or "").strip()
            gkey = f"bundle:{bid}" if bid else f"tok:{r['token']}"
            groups.setdefault(gkey, []).append(r)

        if len(groups) > 1:
            desc = " / ".join(
                f"`{grp[0]['token']}`({grp[0]['action']})"
                for grp in list(groups.values())[:5]
            )
            return {
                "matched": True,
                "ok": False,
                "ambiguous": True,
                "reply": f"⚠️ 当前同时有 {len(groups)} 张待确认卡（{desc}），为防误触请直接引用对应消息回『确认』，或发送 `apply 编号`。",
            }
        chosen = next(iter(groups.values()))

    # 真实扣款卡防误触红线：绝不接受未引用的随口「好/好的/行/可以/1/发吧/确认」直接扣款
    pay_items = [r for r in chosen if str(r.get("action") or "") in REAL_MONEY_PAY_ACTIONS]
    if pay_items:
        explicit_quoted_ok = quoted_matched and clean.lower() in ("确认", "支付", "付款", "同意", "apply")
        if not (is_explicit_pay or explicit_quoted_ok):
            p0 = pay_items[0]
            tok0 = str(p0["token"])
            try:
                p_obj = json.loads(str(p0.get("params_json") or "{}"))
            except Exception:
                p_obj = {}
            shop0 = str(p_obj.get("shop") or "美团订单")
            amt0 = p_obj.get("final_payable_yuan")
            amt_s = f" · 实付 **¥{float(amt0):g}**" if amt0 is not None else ""
            return {
                "matched": True,
                "ok": False,
                "requires_explicit_payment_confirm": True,
                "reply": (
                    f"💳 [`{tok0}`] 涉及美团真实扣款（**{shop0}**{amt_s}），为防误触不接受随口「{clean}」。\n"
                    f"👉 确认付款请回复 **`确认支付`** 或 **`apply {tok0}`**（或引用确认卡回 `确认`）；放弃请回 **`取消 {tok0}`**。"
                ),
            }

    summaries: list[str] = []
    errors: list[str] = []
    applied_tokens: list[str] = []
    for r in chosen:
        res = verify_and_apply_approval(db, str(r["token"]), extra_executors=extra_executors)
        if res.get("ok"):
            applied_tokens.append(str(r["token"]))
            summaries.append(str(res.get("summary") or f"{r['token']} 已执行"))
        else:
            errors.append(str(res.get("error") or f"{r['token']} 执行失败"))

    if errors and not applied_tokens:
        return {"matched": True, "ok": False, "applied_count": 0, "reply": "\n".join(errors)}
    if len(applied_tokens) == 1:
        head = f"✅ [{applied_tokens[0]}] {summaries[0]}"
        reply_lines = [head] + ([f"⚠️ 部分失败：{'；'.join(errors)}"] if errors else [])
    else:
        reply_lines = [f"✅ 已一键执行整包确认（共 {len(applied_tokens)} 项）："] + [
            f"• [{tok}] {s}" for tok, s in zip(applied_tokens, summaries)
        ]
        if errors:
            reply_lines.append(f"⚠️ 部分失败：{'；'.join(errors)}")
    return {
        "matched": True,
        "ok": True,
        "applied_count": len(applied_tokens),
        "tokens": applied_tokens,
        "reply": "\n".join(reply_lines),
    }


def cancel_approval(db: SimingDB, token: str) -> dict[str, Any]:
    """取消任意类型的 PENDING 审批口令（美团类委托清车关页，其余直接原子作废并写审计）。"""
    clean_tok = (token or "").strip().upper()
    if not clean_tok:
        return {"ok": False, "reply": "⚠️ 请指明要取消的审批口令编号。"}

    db.init_db()
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM approvals WHERE token = ?", (clean_tok,)).fetchone()
    finally:
        conn.close()

    if not row:
        return {"ok": False, "reply": f"⚠️ 未找到审批口令 `{clean_tok}`。"}

    action = str(row["action"] or "")
    if action in ("meituan_submit_pay", "meituan_lock_cart"):
        from siming.meituan_pay import cancel_meituan_pending_order

        return cancel_meituan_pending_order(db, token=clean_tok)

    if row["status"] != "PENDING":
        return {
            "ok": False,
            "reply": f"⚠️ 口令 `{clean_tok}` 当前状态为 `{row['status']}`，无需重复取消。",
        }

    with db.transaction() as conn_w:
        cur = conn_w.execute(
            "UPDATE approvals SET status='CANCELLED' WHERE token=? AND status='PENDING'",
            (clean_tok,),
        )
        if cur.rowcount == 0:
            return {"ok": False, "reply": f"⚠️ 口令 `{clean_tok}` 状态已变更，未能取消。"}
        db.write_audit(
            str(row["tier"] or "A2"),
            f"cancelled:{action}",
            {"token": clean_tok},
            target_id=str(row["target_id"] or ""),
            approval_ref=clean_tok,
            exit_code=0,
            detail="user cancelled approval",
            conn=conn_w,
        )
    return {
        "ok": True,
        "token": clean_tok,
        "reply": f"🗑️ 已取消审批确认卡 `{clean_tok}`（{action}），未执行任何操作。",
    }


def cancel_approval_bundle(db: SimingDB, bundle_id: str) -> dict[str, Any]:
    """整包作废同一个 `bundle_id` 下所有仍处于 PENDING 的审批口令。"""
    bid = (bundle_id or "").strip().upper()
    if not bid:
        return {"ok": False, "reply": "⚠️ 请指明捆绑包编号。"}
    db.init_db()
    with db.transaction() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT token, action, tier, target_id FROM approvals WHERE UPPER(bundle_id)=? AND status='PENDING'",
                (bid,),
            ).fetchall()
        ]
        if not rows:
            return {"ok": False, "reply": f"⚠️ 捆绑确认包 `{bid}` 已过期或已处理。"}
        toks = [str(r["token"]) for r in rows]
        ph = ",".join("?" for _ in toks)
        conn.execute(f"UPDATE approvals SET status='CANCELLED' WHERE token IN ({ph}) AND status='PENDING'", tuple(toks))
        for r in rows:
            db.write_audit(
                str(r.get("tier") or "A2"),
                f"cancelled:{r.get('action')}",
                {"token": r["token"], "bundle_id": bid},
                target_id=str(r.get("target_id") or ""),
                approval_ref=str(r["token"]),
                exit_code=0,
                detail="user cancelled approval bundle",
                conn=conn,
            )
    return {
        "ok": True,
        "bundle_id": bid,
        "tokens": toks,
        "reply": f"🗑️ 已取消整包确认卡 `{bid}`（共 {len(toks)} 项：{', '.join(toks)}），未执行任何变更。",
    }
