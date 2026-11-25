"""Coding Plan 订阅配额水位查询（对接 ai-quota）与无头模型调用路由。"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

import httpx

from siming.config import load_policy
from siming.db import SimingDB, iso_now


def fetch_ai_quota(db: Optional[SimingDB] = None) -> dict[str, Any]:
    policy = load_policy(db.paths if db else None)
    url = str(policy["coding_plan"]["ai_quota_url"])
    try:
        # 强制直连 127.0.0.1，绕开任何 HTTP_PROXY
        with httpx.Client(timeout=2.5, trust_env=False) as client:
            resp = client.get(url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        return {"ok": False, "error": str(exc), "codex_remaining": None, "claude_available": None}

    codex_info = data.get("codex") or {}
    codex_primary = codex_info.get("primary") or {}
    codex_rem = codex_primary.get("remaining_percent")

    claude_info = data.get("claude") or {}
    claude_avail = bool(claude_info.get("available"))
    claude_sess = claude_info.get("session") or {}
    claude_rem = claude_sess.get("remaining_percent")

    return {
        "ok": True,
        "generated_at": data.get("generated_at"),
        "codex_available": bool(codex_info.get("available")),
        "codex_remaining": float(codex_rem) if isinstance(codex_rem, (int, float)) else None,
        "codex_label": codex_info.get("remaining_label") or "unknown",
        "claude_available": claude_avail,
        "claude_remaining": float(claude_rem) if isinstance(claude_rem, (int, float)) else None,
        "claude_label": claude_info.get("remaining_label") or "unknown",
        "raw": data,
    }


def select_coding_runner(
    preferred: str = "", db: Optional[SimingDB] = None
) -> tuple[str, str]:
    """根据用户偏好与 ai-quota 实时水位选择健康 runner（返回 (runner_name, binary_path)）。"""
    policy = load_policy(db.paths if db else None)
    cp_cfg = policy["coding_plan"]
    min_pct = float(cp_cfg.get("min_remaining_percent", 10.0))
    quota = fetch_ai_quota(db)

    candidates: dict[str, str] = {}
    for name, cfg_key in (("codex", "codex_bin"), ("claude", "claude_bin")):
        configured = str(cp_cfg.get(cfg_key, ""))
        resolved = configured if (configured and Path(configured).is_file()) else (shutil.which(name) or "")
        if resolved:
            candidates[name] = resolved

    if not candidates:
        return ("none", "")

    codex_tight = (
        quota.get("codex_remaining") is not None
        and float(quota["codex_remaining"]) < min_pct
    )
    claude_tight = (
        quota.get("claude_remaining") is not None
        and float(quota["claude_remaining"]) < min_pct
    )

    order = [preferred] if preferred in candidates else []
    for fallback in ("codex", "claude"):
        if fallback in candidates and fallback not in order:
            order.append(fallback)

    for r in order:
        if r == "codex" and not codex_tight:
            return ("codex", candidates["codex"])
        if r == "claude" and not claude_tight:
            return ("claude", candidates["claude"])

    first = order[0]
    return (first, candidates[first])


def run_coding_plan_prompt(
    prompt: str,
    db: SimingDB,
    cwd: Optional[Path] = None,
    preferred_runner: str = "",
    read_only: bool = True,
    timeout_sec: int = 120,
    ref_id: str = "",
) -> dict[str, Any]:
    """调用 Coding Plan CLI 执行单轮分析或工作区修改（强制 TMPDIR 隔离与超时保护）。"""
    if os.environ.get("SIMING_DISABLE_LLM") == "1":
        return {"ok": False, "runner": "disabled", "output": "", "error": "SIMING_DISABLE_LLM=1"}

    runner, binary = select_coding_runner(preferred=preferred_runner, db=db)
    if not binary:
        return {"ok": False, "runner": "none", "output": "", "error": "未找到可用 Coding Plan CLI"}

    work_cwd = str(cwd or db.paths.tmp_root)
    env = dict(os.environ)
    env["TMPDIR"] = str(db.paths.tmp_root.parent)
    env.setdefault("HTTP_PROXY", "http://127.0.0.1:7890")
    env.setdefault("HTTPS_PROXY", "http://127.0.0.1:7890")
    env.setdefault("NO_PROXY", "localhost,127.0.0.1,::1")

    if runner == "codex":
        sandbox_mode = "read-only" if read_only else "workspace-write"
        cmd = [
            binary,
            "exec",
            "--skip-git-repo-check",
            "-s",
            sandbox_mode,
            "-C",
            work_cwd,
            prompt,
        ]
    else:
        cmd = [binary, "-p", prompt]
        if read_only:
            cmd.extend(
                [
                    "--permission-mode",
                    "plan",
                    "--disallowedTools",
                    "Bash,Edit,Write,NotebookEdit",
                    "--allowedTools",
                    "Read,Grep,Glob",
                ]
            )

    try:
        proc = subprocess.run(
            cmd,
            cwd=work_cwd,
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        ok = proc.returncode == 0 and bool(out)
    except subprocess.TimeoutExpired:
        out, err, ok = "", f"执行超时 ({timeout_sec}s)", False
    except Exception as exc:
        out, err, ok = "", str(exc), False

    tok_in = max(1, len(prompt) // 3)
    tok_out = len(out) // 3
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO usage (ts, model, runner, tokens_in, tokens_out, cost_cny, ref_id)
            VALUES (?, ?, ?, ?, ?, 0.0, ?)
            """,
            (iso_now(), runner, "coding_plan", tok_in, tok_out, ref_id),
        )
    return {"ok": ok, "runner": runner, "output": out, "error": err}
