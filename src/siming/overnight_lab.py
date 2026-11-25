"""W3-4 · 睡前异步实验交班（Overnight Lab Handoff）。

夜猫子睡前把 `gpu-server` GPU 训练或 `personas`/`indie_game` 长评测交给司命看护：
- 登记交班单并强制白名单限制夜间自动处置动作（绝不执行破坏性操作）；
- 夜间静默巡检日志末尾与指标：识别 `gpu_drop`、`loss_nan`、`permission_stall`、`completed`，
  仅执行白名单内安全处置（`checkpoint_save` / `safe_resume` / `auto_approve_readonly`），全程零吵醒；
- 自然醒时汇总对比表格 + A2 `apply <TOKEN>` 下一步确认卡。
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from siming.approval import create_approval
from siming.crypto import _ANSI_ESCAPE_RE, redact_sensitive
from siming.db import SimingDB, iso_now

SAFE_ACTION_WHITELIST: frozenset[str] = frozenset(
    {
        "checkpoint_save",
        "safe_resume",
        "auto_approve_readonly",
        "record_metrics",
        "mark_done",
    }
)
DEFAULT_WHITELIST: list[str] = [
    "checkpoint_save",
    "safe_resume",
    "auto_approve_readonly",
]

_GPU_DROP_RE = re.compile(
    r"(CUDA\s+error|CUDA\s+out\s+of\s+memory|NCCL\s+(error|timeout|unhandled)|NVRM:\s+Xid\s+\d+|Xid\s+\d+|GPU\s+has\s+fallen\s+off\s+the\s+bus|RuntimeError:\s*CUDA)",
    re.IGNORECASE,
)
_GPU_NEGATED_RE = re.compile(
    r"(\bno\s+(?:cuda|nccl)\s+(?:error|timeout|oom)\b|(?:cuda|nccl)[_\s]+errors?\s*(?:count)?\s*[:=]\s*(?:0|false|none|ok)\b)",
    re.IGNORECASE,
)
_LOSS_NAN_RE = re.compile(
    r"((?<![A-Za-z0-9_])(?:(?:train|val|eval|step)_)?loss\s*[:=]\s*(?:nan|inf|-inf)\b|\bnan\s+loss\b|\bloss\s+is\s+nan\b|\bgrad\w*\s*[:=]\s*nan\b|\bNaN\s+detected\b)",
    re.IGNORECASE,
)
_LOSS_NAN_NEGATED_RE = re.compile(
    r"(\bno\s+nan\s+detected\b|\b0\s+nan\s+detected\b|\bnan\s+detected\s*[:=]\s*(?:0|false|none|no)\b|\bloss_nan_(?:check|guard|watch)\s*[:=])",
    re.IGNORECASE,
)
_PERM_STALL_RE = re.compile(
    r"(waiting\s+for\s+permission|approve\s+tool|do\s+you\s+want\s+to\s+proceed|\ballow\s+[^=\n]{1,64}\[y/n\]|herdr.*blocked|permission\s+requested)",
    re.IGNORECASE,
)
_PERM_NEGATED_RE = re.compile(
    r"(\b(?:no|not|zero)\s+(?:permission\s+(?:error|requested|stall)|waiting\s+for\s+permission)\b|waiting\s+for\s+permission\s*[:=]\s*(?:false|0|no)\b)",
    re.IGNORECASE,
)
_COMPLETED_RE = re.compile(
    r"(training\s+complete|evaluation\s+finished|all\s+done|exit_code\s*=\s*0|finished\s+training|benchmark\s+completed)",
    re.IGNORECASE,
)

_METRIC_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("step", re.compile(r"\bstep\s*[:=]\s*(\d+)", re.IGNORECASE)),
    ("epoch", re.compile(r"\bepoch\s*[:=]\s*(\d+(?:\.\d+)?)", re.IGNORECASE)),
    ("loss", re.compile(r"\bloss\s*[:=]\s*(nan|inf|-inf|[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?)", re.IGNORECASE)),
    ("val_loss", re.compile(r"\bval_loss\s*[:=]\s*(nan|inf|-inf|[-+]?\d+(?:\.\d+)?(?:e[-+]?\d+)?)", re.IGNORECASE)),
    ("acc", re.compile(r"\b(?:acc|accuracy)\s*[:=]\s*([-+]?\d+(?:\.\d+)?%?)", re.IGNORECASE)),
    ("eval_score", re.compile(r"\b(?:eval_score|score)\s*[:=]\s*([-+]?\d+(?:\.\d+)?)", re.IGNORECASE)),
)


def _row_to_handoff(row: Any) -> dict[str, Any]:
    return {
        "handoff_id": str(row["handoff_id"]),
        "name": str(row["name"]),
        "host": str(row["host"]),
        "pane_or_pid": str(row["pane_or_pid"] or ""),
        "log_path": str(row["log_path"] or ""),
        "status": str(row["status"]),
        "whitelist": json.loads(str(row["whitelist_json"] or "[]")),
        "metrics": json.loads(str(row["metrics_json"] or "{}")),
        "report_md": str(row["report_md"] or ""),
        "created_at": str(row["created_at"]),
        "updated_at": str(row["updated_at"]),
    }


def register_overnight_handoff(
    db: SimingDB,
    name: str,
    host: str = "gpu-server",
    pane_or_pid: str = "",
    log_path: str = "",
    whitelist: Optional[list[str]] = None,
) -> dict[str, Any]:
    """登记睡前异步实验交班单，自动过滤非白名单危险操作。"""
    db.init_db()
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("实验交班名称 name 不能为空")

    requested = list(DEFAULT_WHITELIST if whitelist is None else whitelist)
    safe_list = [a for a in requested if a in SAFE_ACTION_WHITELIST]
    rejected = [a for a in requested if a not in SAFE_ACTION_WHITELIST]

    ts = iso_now()
    with db.transaction() as conn:
        handoff_id = db.next_seq_id(conn, "OH")
        conn.execute(
            """
            INSERT INTO overnight_handoffs (
                handoff_id, name, host, pane_or_pid, log_path, status,
                whitelist_json, metrics_json, report_md, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'WATCHING', ?, '{}', '', ?, ?)
            """,
            (
                handoff_id,
                clean_name,
                (host or "gpu-server").strip(),
                str(pane_or_pid or "").strip(),
                str(log_path or "").strip(),
                json.dumps(safe_list, ensure_ascii=False),
                ts,
                ts,
            ),
        )
        db.write_audit(
            "A1",
            "overnight_handoff_register",
            {
                "handoff_id": handoff_id,
                "name": clean_name,
                "host": host,
                "whitelist": safe_list,
                "rejected_unsafe": rejected,
            },
            target_id=handoff_id,
            conn=conn,
        )

    return {
        "ok": True,
        "handoff_id": handoff_id,
        "name": clean_name,
        "host": (host or "gpu-server").strip(),
        "pane_or_pid": str(pane_or_pid or "").strip(),
        "log_path": str(log_path or "").strip(),
        "status": "WATCHING",
        "whitelist": safe_list,
        "rejected_unsafe_actions": rejected,
        "created_at": ts,
    }


def inspect_handoff_log(log_text: str) -> dict[str, Any]:
    """解析实验日志末尾：剥离 ANSI 转义序列，过滤否定/配置良性行，检测 GPU 掉卡 / Loss NaN / 权限卡住 / 正常完工并抽取最新指标。"""
    raw = _ANSI_ESCAPE_RE.sub("", log_text or "")
    lines: list[str] = []
    for raw_ln in raw.splitlines():
        if "\r" in raw_ln:
            segs = [s for s in raw_ln.split("\r") if s.strip()]
            raw_ln = segs[-1] if segs else ""
        s = raw_ln.strip()
        if s:
            lines.append(s)
    tail_line = redact_sensitive(lines[-1]) if lines else ""

    metrics: dict[str, Any] = {}
    last_loss_nan_idx = -1
    last_finite_loss_idx = -1
    last_gpu_drop_idx = -1
    last_perm_stall_idx = -1
    last_completed_idx = -1
    last_resume_idx = -1

    for idx, ln in enumerate(lines):
        for key, pat in _METRIC_PATTERNS:
            m = pat.search(ln)
            if m:
                raw_val = m.group(1)
                if raw_val.lower() in ("nan", "inf", "-inf"):
                    metrics[key] = raw_val.lower()
                elif raw_val.endswith("%"):
                    metrics[key] = raw_val
                else:
                    try:
                        metrics[key] = int(raw_val) if key == "step" else float(raw_val)
                        if key in ("loss", "val_loss"):
                            last_finite_loss_idx = idx
                    except ValueError:
                        metrics[key] = raw_val

        if re.search(r"(resum(?:ing|ed)\s+from|restarted\s+from\s+checkpoint|recovered\s+gpu)", ln, re.IGNORECASE):
            last_resume_idx = idx
        if _LOSS_NAN_RE.search(ln) and not _LOSS_NAN_NEGATED_RE.search(ln):
            last_loss_nan_idx = idx
        if _GPU_DROP_RE.search(ln) and not _GPU_NEGATED_RE.search(ln):
            last_gpu_drop_idx = idx
        if _PERM_STALL_RE.search(ln) and not _PERM_NEGATED_RE.search(ln):
            last_perm_stall_idx = idx
        if _COMPLETED_RE.search(ln):
            last_completed_idx = idx

    # 若在早期 gpu_drop / permission_stall 之后已经显式恢复并持续输出正常有限 loss 或正常完工，则不再反复触发历史已恢复异常
    has_loss_nan = last_loss_nan_idx >= 0 and (
        str(metrics.get("loss", "")).lower() in ("nan", "inf", "-inf")
        or last_loss_nan_idx >= max(last_finite_loss_idx, last_completed_idx)
    )
    gpu_recovered = last_gpu_drop_idx >= 0 and (
        (last_completed_idx > last_gpu_drop_idx and not has_loss_nan)
        or (last_resume_idx > last_gpu_drop_idx and last_finite_loss_idx > last_resume_idx)
    )
    has_gpu_drop = last_gpu_drop_idx >= 0 and not gpu_recovered
    perm_recovered = last_perm_stall_idx >= 0 and (
        last_completed_idx > last_perm_stall_idx
        or last_finite_loss_idx > last_perm_stall_idx
    )
    has_perm_stall = last_perm_stall_idx >= 0 and not perm_recovered
    has_completed = last_completed_idx >= 0

    anomalies: list[str] = []
    if has_loss_nan:
        anomalies.append("loss_nan")
    if has_gpu_drop:
        anomalies.append("gpu_drop")
    if has_perm_stall:
        anomalies.append("permission_stall")

    state = "running"
    recommended = "none"
    if "loss_nan" in anomalies:
        state = "loss_nan"
        recommended = "checkpoint_save"
    elif "gpu_drop" in anomalies:
        state = "gpu_drop"
        recommended = "safe_resume"
    elif "permission_stall" in anomalies:
        state = "permission_stall"
        recommended = "auto_approve_readonly"
    elif has_completed:
        state = "completed"
        recommended = "mark_done"

    return {
        "state": state,
        "anomalies": anomalies,
        "metrics": metrics,
        "recommended_action": recommended,
        "tail_line": tail_line[:160],
    }


def patrol_overnight_handoffs(
    db: SimingDB,
    log_reader_fn: Optional[Callable[[dict[str, Any]], str]] = None,
    action_fn: Optional[Callable[[dict[str, Any], str], dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """夜间静默巡检所有在途交班单：仅执行白名单内安全处置，绝不半夜打扰用户。"""
    db.init_db()
    ts = iso_now(now)
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM overnight_handoffs WHERE status IN ('WATCHING', 'RESUMED') ORDER BY created_at ASC"
        ).fetchall()
    finally:
        conn.close()

    remediations: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    for row in rows:
        handoff = _row_to_handoff(row)
        log_text = ""
        if log_reader_fn is not None:
            log_text = str(log_reader_fn(handoff) or "")
        elif handoff["log_path"]:
            p = Path(handoff["log_path"])
            if p.is_file():
                log_text = p.read_text(encoding="utf-8", errors="replace")[-16384:]

        diagnosis = inspect_handoff_log(log_text)
        state = diagnosis["state"]
        wl = set(handoff["whitelist"]) & SAFE_ACTION_WHITELIST
        new_status = handoff["status"]
        actions_taken: list[str] = []

        def _run_safe_action(act: str) -> None:
            if act not in wl or act not in SAFE_ACTION_WHITELIST:
                return
            exec_res: dict[str, Any] = {"ok": True, "dry_run": action_fn is None}
            if action_fn is not None:
                exec_res = action_fn(handoff, act) or {"ok": True}
            actions_taken.append(act)
            remediations.append(
                {
                    "handoff_id": handoff["handoff_id"],
                    "name": handoff["name"],
                    "trigger": state,
                    "action": act,
                    "result": exec_res,
                    "silent": True,
                    "ts": ts,
                }
            )
            db.write_audit(
                "A1",
                f"overnight_remediate:{act}",
                {"handoff_id": handoff["handoff_id"], "trigger": state, "result": exec_res},
                target_id=handoff["handoff_id"],
            )

        if state == "loss_nan":
            if "checkpoint_save" in wl:
                _run_safe_action("checkpoint_save")
                new_status = "CHECKPOINTED"
            else:
                new_status = "FAILED"
        elif state == "gpu_drop":
            if "checkpoint_save" in wl:
                _run_safe_action("checkpoint_save")
            if "safe_resume" in wl:
                _run_safe_action("safe_resume")
                new_status = "RESUMED"
            elif "checkpoint_save" in wl:
                new_status = "CHECKPOINTED"
            else:
                new_status = "FAILED"
        elif state == "permission_stall":
            if "auto_approve_readonly" in wl:
                _run_safe_action("auto_approve_readonly")
        elif state == "completed":
            new_status = "DONE"
            db.write_audit(
                "A1",
                "overnight_handoff_done",
                {"handoff_id": handoff["handoff_id"], "metrics": diagnosis["metrics"]},
                target_id=handoff["handoff_id"],
            )

        merged_metrics = dict(handoff["metrics"])
        merged_metrics.update(diagnosis["metrics"])
        merged_metrics["last_state"] = state
        if actions_taken:
            prev_actions = list(merged_metrics.get("remediations") or [])
            prev_actions.extend(actions_taken)
            merged_metrics["remediations"] = prev_actions
        if diagnosis["tail_line"]:
            merged_metrics["tail_line"] = diagnosis["tail_line"]

        with db.transaction() as c:
            c.execute(
                """
                UPDATE overnight_handoffs
                SET status = ?, metrics_json = ?, updated_at = ?
                WHERE handoff_id = ?
                """,
                (new_status, json.dumps(merged_metrics, ensure_ascii=False), ts, handoff["handoff_id"]),
            )

        results.append(
            {
                "handoff_id": handoff["handoff_id"],
                "name": handoff["name"],
                "state": state,
                "status": new_status,
                "metrics": merged_metrics,
                "actions_taken": actions_taken,
            }
        )

    return {
        "ok": True,
        "checked": len(results),
        "remediations": remediations,
        "woke_user": False,
        "results": results,
        "patrolled_at": ts,
    }


def build_morning_handoff_report(
    db: SimingDB,
    create_a2_card: bool = True,
) -> dict[str, Any]:
    """生成自然醒一页对比战报 Markdown 表格，并为需要拍板的下一步生成 A2 `apply` 口令。"""
    db.init_db()
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM overnight_handoffs ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
    finally:
        conn.close()

    handoffs = [_row_to_handoff(r) for r in rows]
    if not handoffs:
        return {
            "ok": True,
            "count": 0,
            "summary": "🌙 昨晚暂无登记的异步实验交班单。",
            "markdown_table": "",
            "approval_token": "",
            "handoffs": [],
        }

    table_lines = [
        "| ID | 实验任务 | 主机 | 状态 | 最新指标 | 夜间静默处置 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    needs_next_step: list[dict[str, Any]] = []
    for h in handoffs:
        m = h["metrics"]
        metric_bits = []
        for k in ("step", "epoch", "loss", "val_loss", "acc", "eval_score"):
            if k in m:
                metric_bits.append(f"{k}={m[k]}")
        metric_str = ", ".join(metric_bits) or "-"
        rems = m.get("remediations") or []
        rem_str = ", ".join(str(x) for x in rems) if rems else "无异常"
        safe_name = str(h["name"]).replace("|", "/")
        table_lines.append(
            f"| `{h['handoff_id']}` | {safe_name} | `{h['host']}` | **{h['status']}** | `{metric_str}` | {rem_str} |"
        )
        if h["status"] in ("CHECKPOINTED", "DONE", "RESUMED", "FAILED"):
            needs_next_step.append(h)

    markdown_table = "\n".join(table_lines)

    approval_token = ""
    if create_a2_card and needs_next_step:
        primary = needs_next_step[0]
        app = create_approval(
            db,
            action="overnight_handoff_next_step",
            target_id=primary["handoff_id"],
            params={
                "handoff_id": primary["handoff_id"],
                "name": primary["name"],
                "host": primary["host"],
                "status": primary["status"],
                "suggested_next": (
                    "adjust_lr_and_resume_from_checkpoint"
                    if primary["status"] == "CHECKPOINTED"
                    else "promote_best_checkpoint"
                ),
            },
        )
        approval_token = app["token"]

    done_cnt = sum(1 for h in handoffs if h["status"] == "DONE")
    ckpt_cnt = sum(1 for h in handoffs if h["status"] in ("CHECKPOINTED", "RESUMED"))
    summary_lines = [
        f"☀️ **自然醒夜间实验战报**（共 `{len(handoffs)}` 项 · 完工 `{done_cnt}` · 静默兜底/恢复 `{ckpt_cnt}`）",
        markdown_table,
    ]
    if approval_token:
        summary_lines.append(
            f"👉 如需基于最新安全检查点继续下一步实验，回复 `apply {approval_token}` 确认执行（A2 门禁保护）。"
        )
    summary = "\n\n".join(summary_lines)

    ts = iso_now()
    with db.transaction() as c:
        for h in handoffs:
            c.execute(
                "UPDATE overnight_handoffs SET report_md = ?, updated_at = ? WHERE handoff_id = ?",
                (markdown_table, ts, h["handoff_id"]),
            )

    return {
        "ok": True,
        "count": len(handoffs),
        "summary": summary,
        "markdown_table": markdown_table,
        "approval_token": approval_token,
        "handoffs": handoffs,
    }
