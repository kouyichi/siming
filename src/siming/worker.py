"""按需拉起的执行器（worker）：重验 Mandate 后执行 remind / resume / stage / run。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
from pathlib import Path
from typing import Any, Optional

from siming.approval import create_approval
from siming.config import load_policy
from siming.crypto import redact_sensitive
from siming.db import SimingDB, iso_now
from siming.enrich import lookup_deco_snippets
from siming.mandate import Mandate, revalidate_mandate
from siming.modules import is_module_enabled
from siming.quota import run_coding_plan_prompt

RESTART_RECOVERY_NOTE = (
    "【重启恢复保护】上一轮后台执行曾因进程中断未走完。请先运行 `git status` 与检查目录现场，"
    "确认上一步已完成到哪里，严禁不看现场直接重复跑可能产生副作用的命令。\n\n"
)

_PROBLEM_FAMILY_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("meituan_dom", ("美团", "meituan", "点餐", "购物车", "poi_id")),
    ("sqlite_db", ("sqlite", "wal", "busy", "lock", "schema", "数据库", "事务")),
    ("context_graph", ("图谱", "context_graph", "联邦检索", "knowledge graph")),
    ("sub_assassin", ("订阅", "sub_hunter", "sub_assassin", "连续包月")),
    ("sure_finance", ("sure", "账本", "ledger", "报销", "cw08", "cw01", "pipelock")),
    ("tesla_ev", ("tesla", "特斯拉", "空调", "续航", "充电", "owntracks")),
    ("smarthome", ("haos", "新风", "甲醛", "扫地机", "拓竹", "bambu", "nas")),
    ("rag_rerank", ("rag", "rerank", "桌游", "boardgame_kb", "规则书")),
    ("infra_net", ("sing-box", "atrust", "dns", "tls", "omarchy", "代理")),
)


def infer_problem_family(task: str, cwd: str = "", mode: str = "run") -> str:
    """从任务描述与仓库目录推断 M-SIM 问题族，便于跨模型经验精确匹配与归档。"""
    low = f"{task or ''} {cwd or ''}".lower()
    for fam, kws in _PROBLEM_FAMILY_HINTS:
        if any(k in low for k in kws):
            return fam
    repo_name = Path(cwd).name.strip().lower() if cwd else ""
    if repo_name and repo_name not in (".", "/", "tmp", "worktrees"):
        return f"repo_{re.sub(r'[^a-z0-9_]+', '_', repo_name).strip('_') or 'coding'}"
    return f"worker_{mode or 'coding'}"


def select_worker_runtime(
    db: SimingDB,
    mandate: Mandate,
    mode: str = "stage",
    quota_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """联动 W3-1 multi_runtime：按订阅配额水位与算力熔断状态选最优 Runner 与回退链。"""
    raw_pref = (mandate.runner or "").strip().lower()
    fallback_runner = raw_pref if raw_pref in ("claude", "codex") else ""
    default_out = {
        "enabled": False,
        "runtime": f"{fallback_runner}_sub" if fallback_runner else "coding_plan",
        "preferred_runner": fallback_runner,
        "model": fallback_runner or "coding_plan",
        "fallback_chain": [],
        "reason": "multi_runtime_disabled_or_default",
    }
    try:
        if not is_module_enabled(db, "multi_runtime"):
            return default_out
        from siming.multi_runtime import select_runtime

        pref_rt = {"claude": "claude_sub", "codex": "codex_sub"}.get(raw_pref, raw_pref)
        task_kind = "coding" if mode == "stage" else "debugging"
        sel = select_runtime(
            db,
            task_kind=task_kind,
            complexity="high",
            quota_snapshot=quota_snapshot,
            preferred=pref_rt,
        )
        rt = str(sel.get("runtime") or "")
        rt_kind = str(sel.get("kind") or "")
        mapped_runner = "claude" if rt == "claude_sub" else ("codex" if rt == "codex_sub" else "")
        return {
            "enabled": True,
            "runtime": rt or default_out["runtime"],
            "runtime_kind": rt_kind,
            "preferred_runner": mapped_runner,
            "model": str(sel.get("model") or rt or "coding_plan"),
            "fallback_chain": list(sel.get("fallback_chain") or []),
            "skipped": list(sel.get("skipped") or []),
            "reason": str(sel.get("reason") or ""),
            "selection": sel,
        }
    except Exception as exc:
        default_out["reason"] = f"multi_runtime_fallback:{str(exc)[:100]}"
        return default_out


def build_worker_knowledge_context(
    db: SimingDB,
    task: str,
    *,
    target_model: str = "worker",
    problem_family: str = "",
    cwd: str = "",
    mode: str = "stage",
    limit: int = 3,
) -> dict[str, Any]:
    """联动 W3-2 Hindsight Wiki 与 W3-3 M-SIM：为后台派单拼装共享知识与跨模型前世记忆注入块。"""
    clean_task = (task or "").strip()
    fam = (problem_family or "").strip() or infer_problem_family(clean_task, cwd=cwd, mode=mode)
    wiki_hits: list[dict[str, Any]] = []
    msim_prompt = ""

    if clean_task:
        try:
            if is_module_enabled(db, "hindsight_wiki"):
                from siming.hindsight_wiki import query_hindsight_wiki

                wiki_hits = query_hindsight_wiki(db, clean_task, limit=limit)
        except Exception:
            wiki_hits = []

        try:
            if is_module_enabled(db, "msim_memory"):
                from siming.msim_memory import build_msim_transfer_prompt

                msim_prompt = build_msim_transfer_prompt(
                    db,
                    problem_family=fam,
                    target_model=target_model or "worker",
                    query=clean_task,
                    limit=limit,
                )
        except Exception:
            msim_prompt = ""

    parts: list[str] = []
    if wiki_hits:
        parts.append("### Hindsight 共享 Wiki 命中")
        for h in wiki_hits:
            snip = re.sub(r"\s+", " ", str(h.get("snippet") or "")).strip()[:240]
            parts.append(f"- [{h['slug']}] {h['title']}: {snip}")
    if msim_prompt:
        parts.append(msim_prompt)

    block = ""
    if parts:
        block = "## 司命共享知识与跨模型前世记忆 (Hindsight Wiki + M-SIM)\n\n" + "\n\n".join(parts)

    return {
        "problem_family": fam,
        "wiki_hits": wiki_hits,
        "msim_prompt": msim_prompt,
        "wiki_injected": bool(wiki_hits),
        "msim_injected": bool(msim_prompt),
        "block": block,
    }


def build_worker_task_prompt(
    db: SimingDB,
    wait_row: dict[str, Any],
    mandate: Mandate,
    wt_dir: Path,
    *,
    runner_label: str = "worker",
    mode: str = "stage",
    was_interrupted: bool = False,
) -> tuple[str, dict[str, Any]]:
    """构造带重启保护 + Hindsight Wiki + M-SIM 知识注入的后台任务 Prompt。"""
    wid = str(wait_row.get("wait_id") or "")
    task_text = (mandate.task or wait_row.get("quote") or "").strip()
    prefix = RESTART_RECOVERY_NOTE if was_interrupted else ""
    base_prompt = (
        f"{prefix}你在隔离工作目录 `{wt_dir}` 执行用户预授权任务（编号 {wid}）。\n"
        f"用户原话：{wait_row.get('quote', '')}\n"
        f"具体任务：{task_text}\n"
        f"补充上下文：{mandate.context}\n"
        f"验收命令：{mandate.done_check or '无'}\n"
        "如果需求存在重大歧义导致无法安全修改，请直接输出 `AMBIGUOUS: <具体原因>` 并停手，不要硬猜。"
    )
    query_text = " ".join(x for x in (task_text, mandate.context, mandate.done_check) if x).strip()
    kb_ctx = build_worker_knowledge_context(
        db,
        query_text,
        target_model=runner_label or mandate.runner or "worker",
        cwd=str(mandate.cwd or wt_dir),
        mode=mode,
        limit=3,
    )
    if kb_ctx["block"]:
        base_prompt = f"{base_prompt}\n\n{kb_ctx['block']}"
    return base_prompt, kb_ctx


def _record_worker_msim_outcome(
    db: SimingDB,
    *,
    wid: str,
    mode: str,
    problem_family: str,
    source_model: str,
    task_desc: str,
    outcome_summary: str,
    done_check: str,
    passed: bool,
    attempts: int,
    runtime_name: str,
    repo_name: str,
) -> Optional[dict[str, Any]]:
    """执行收口后自动向 W3-3 M-SIM 写入本次成败轨迹，沉淀跨模型经验。"""
    try:
        if not is_module_enabled(db, "msim_memory"):
            return None
        from siming.msim_memory import record_msim_entry

        rationale = f"[{wid}/{mode}] {task_desc[:140]} => {outcome_summary[:180]}".strip()
        return record_msim_entry(
            db,
            problem_family=problem_family or f"worker_{mode}",
            source_model=source_model or runtime_name or "worker",
            change_rationale=rationale,
            expected_outcome=f"done_check={done_check or 'exit_0'}",
            measured_score=1.0 if passed else 0.0,
            baseline_score=0.5,
            passed=bool(passed),
            eval_context=f"worker:{mode} runtime={runtime_name} attempts={attempts}",
            target_artifact=repo_name,
            tags=[mode, wid, runtime_name, "auto_worker"],
        )
    except Exception:
        return None


def _enqueue_qq_or_morning(
    db: SimingDB,
    ref_id: str,
    text: str,
    silent_night: bool = False,
    rule_id: str = "worker_notify",
    bypass_quiet: bool = False,
    conn=None,
) -> None:
    clean_text = redact_sensitive(text.strip(), db.paths)

    def _do(c) -> None:
        if silent_night:
            c.execute(
                """
                INSERT INTO outbox (
                    id, ref_id, rule_id, text, send_at, status, channel, created_at
                ) VALUES (?, ?, ?, ?, ?, 'MORNING_QUEUE', 'qqbot', ?)
                """,
                (f"MQ-{ref_id}-{secrets.token_hex(4)}", ref_id, rule_id, clean_text, iso_now(), iso_now()),
            )
        else:
            db.enqueue_effect(
                c,
                "send_qq",
                {
                    "ref_id": ref_id,
                    "rule_id": rule_id,
                    "trial": False,
                    "bypass_quiet": bool(bypass_quiet),
                    "text": clean_text,
                },
            )

    if conn is not None:
        _do(conn)
    else:
        with db.transaction() as c:
            _do(c)


def _execute_remind(
    db: SimingDB,
    wait_row: dict[str, Any],
    mandate: Mandate,
    trigger_reason: str,
    downgrade_note: str = "",
) -> dict[str, Any]:
    wid = wait_row["wait_id"]
    quote = wait_row.get("quote") or mandate.task or wait_row["trigger_expr"]
    extra_lines: list[str] = []

    if downgrade_note:
        extra_lines.append(f"权限说明：{downgrade_note}")
    if mandate.cwd:
        extra_lines.append(f"关联目录：{mandate.cwd}")
    if mandate.task and mandate.task != quote:
        extra_lines.append(f"原定动作：{mandate.task}")
    if mandate.context:
        extra_lines.append(f"现场备注：{mandate.context}")

    # 若为家电到货初验提醒（11.19）或 12.09/12.10 送装里程碑提醒（W04 / W05），自动附上对应实时清单
    fire_at_s = str(wait_row.get("next_fire_at") or "")
    if "到货" in quote and "初验" in quote:
        try:
            from siming.install_plan import format_arrival_inspection_checklist

            extra_lines.append(format_arrival_inspection_checklist(db))
        except Exception:
            pass
    elif fire_at_s.startswith("2026-12-09") or any(k in quote for k in ("12.09", "12-09", "前一天", "前夜", "作战单")):
        try:
            from siming.install_plan import format_onsite_grouped_checklist

            extra_lines.append(format_onsite_grouped_checklist(db))
        except Exception:
            pass
    elif any(k in quote for k in ("12.10", "12-10", "12月10", "送装", "安装时间")):
        try:
            from siming.install_plan import format_installation_checklist

            extra_lines.append(format_installation_checklist(db, compact=True))
        except Exception:
            pass
    else:
        # 顺手备齐周边只读线索
        kws = [t for t in re.split(r"[\s，。！？,!?/]+", quote) if len(t) >= 2][:3]
        if any(k in quote for k in ("尺寸", "窗帘", "橱柜", "安装", "洗烘", "烟灶", "电视")):
            snippets = lookup_deco_snippets(kws, db=db, max_hits=2)
            if snippets:
                extra_lines.append("顺手查到的档案：" + " | ".join(snippets))

    msg = f"[{wid}] 挂起项已触发：{quote}\n触发原因：{trigger_reason}"
    if extra_lines:
        msg += "\n" + "\n".join(extra_lines)

    # 显式定时硬提醒（非 silent_night 夜间静默任务）必须准点穿透，严禁在睡懒觉时段被压进晨札导致错过约定！
    explicit_time_alarm = (
        str(wait_row.get("trigger_kind") or "") == "time"
        and str(wait_row.get("strength") or "") == "explicit"
        and not mandate.silent_night
    )
    _enqueue_qq_or_morning(
        db,
        wid,
        msg,
        silent_night=mandate.silent_night,
        rule_id="wait_remind",
        bypass_quiet=explicit_time_alarm,
    )
    with db.transaction() as conn:
        conn.execute(
            "UPDATE waits SET status='DONE', result_summary=?, updated_at=? WHERE wait_id=? AND status NOT IN ('CANCELLED', 'EXPIRED')",
            (msg[:300], iso_now(), wid),
        )
    db.write_audit("A0", "worker_remind", {"trigger": trigger_reason}, target_id=wid)
    return {"ok": True, "mode": "remind", "summary": msg}


def _execute_resume_herdr(
    db: SimingDB,
    wait_row: dict[str, Any],
    mandate: Mandate,
    trigger_reason: str,
) -> dict[str, Any]:
    """在 herdr 后台不抢焦点新建标签页并拉起原会话，绝不往现有用户 pane 发字符。"""
    wid = wait_row["wait_id"]
    policy = load_policy(db.paths)
    herdr_bin = str(policy["coding_plan"].get("herdr_bin") or "/usr/bin/herdr")
    short_title = (wait_row.get("quote") or mandate.task or "恢复会话")[:8].strip()
    label = f"● {wid} {short_title}"

    if os.environ.get("SIMING_DRY_RUN_HERDR") == "1" or not Path(herdr_bin).is_file():
        summary = f"[dry-run] 将执行 herdr tab create --cwd {mandate.cwd} --label '{label}' --no-focus"
        _enqueue_qq_or_morning(db, wid, f"[{wid}] {summary}")
        return {"ok": True, "mode": "resume", "summary": summary}

    # 先记录已有 pane 集合，确保后续只操作刚刚新建的后台标签 pane
    before_panes: set[str] = set()
    p_list = subprocess.run([herdr_bin, "pane", "list"], capture_output=True, text=True, timeout=5)
    if p_list.returncode == 0:
        try:
            for p in (json.loads(p_list.stdout).get("result") or {}).get("panes") or []:
                if p.get("pane_id"):
                    before_panes.add(str(p["pane_id"]))
        except Exception:
            pass

    create_cmd = [
        herdr_bin,
        "tab",
        "create",
        "--cwd",
        mandate.cwd,
        "--label",
        label,
        "--no-focus",
    ]
    proc = subprocess.run(create_cmd, capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        return _execute_remind(
            db,
            wait_row,
            mandate,
            trigger_reason,
            downgrade_note=f"herdr 后台开标签失败 ({proc.stderr.strip()[:80]})，已退回提醒",
        )

    new_pane_id = ""
    try:
        res_obj = json.loads(proc.stdout).get("result") or {}
        new_pane_id = str(res_obj.get("pane_id") or "")
        new_tab_id = str(res_obj.get("tab_id") or "")
    except Exception:
        new_tab_id = ""

    if not new_pane_id:
        p_after = subprocess.run([herdr_bin, "pane", "list"], capture_output=True, text=True, timeout=5)
        if p_after.returncode == 0:
            try:
                for p in (json.loads(p_after.stdout).get("result") or {}).get("panes") or []:
                    pid_str = str(p.get("pane_id") or "")
                    if pid_str and pid_str not in before_panes and (not new_tab_id or p.get("tab_id") == new_tab_id):
                        new_pane_id = pid_str
                        break
            except Exception:
                pass

    # 把现场备忘写到 $TMPDIR/siming/ 供恢复后随时看
    ctx_note_path = db.paths.tmp_root / f"resume-{wid}.md"
    ctx_note_path.write_text(
        f"# {wid} 恢复现场说明\n\n- 触发原因：{trigger_reason}\n- 原话：{wait_row.get('quote', '')}\n- 上下文：{mandate.context}\n",
        encoding="utf-8",
    )

    resumed_cmd_desc = "已打开目录后台标签"
    if new_pane_id and new_pane_id not in before_panes and mandate.session_id:
        if mandate.agent == "claude":
            run_args = [
                herdr_bin,
                "pane",
                "run",
                new_pane_id,
                str(policy["coding_plan"]["claude_bin"]),
                "--resume",
                mandate.session_id,
                "--fork-session",
            ]
            subprocess.run(run_args, capture_output=True, text=True, timeout=8)
            resumed_cmd_desc = f"claude --resume {mandate.session_id} --fork-session"
        elif mandate.agent == "codex":
            run_args = [
                herdr_bin,
                "pane",
                "run",
                new_pane_id,
                str(policy["coding_plan"]["codex_bin"]),
                "resume",
                mandate.session_id,
            ]
            subprocess.run(run_args, capture_output=True, text=True, timeout=8)
            resumed_cmd_desc = f"codex resume {mandate.session_id}"

    msg = (
        f"[{wid}] 已在 herdr 后台标签 `{label}` 恢复现场（未抢当前焦点）\n"
        f"触发条件：{trigger_reason}\n"
        f"目录：{mandate.cwd}\n"
        f"动作：{resumed_cmd_desc}"
    )
    if mandate.context:
        msg += f"\n现场纪要：{mandate.context}"

    _enqueue_qq_or_morning(db, wid, msg, silent_night=mandate.silent_night, rule_id="wait_resume")
    with db.transaction() as conn:
        conn.execute(
            "UPDATE waits SET status='DONE', result_summary=?, updated_at=? WHERE wait_id=? AND status NOT IN ('CANCELLED', 'EXPIRED')",
            (msg[:300], iso_now(), wid),
        )
    db.write_audit("A1", "worker_resume", {"tab_label": label, "pane_id": new_pane_id}, target_id=wid)
    return {"ok": True, "mode": "resume", "summary": msg}


def _run_done_check(cmd_str: str, cwd: Path, db: SimingDB) -> tuple[int, str]:
    env = dict(os.environ)
    env["TMPDIR"] = str(db.paths.tmp_root.parent)
    proc = subprocess.run(
        ["/bin/bash", "-c", cmd_str],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    out = ((proc.stdout or "") + "\n" + (proc.stderr or "")).strip()
    return proc.returncode, out[-600:]


def _execute_worktree_stage_or_run(
    db: SimingDB,
    wait_row: dict[str, Any],
    mandate: Mandate,
    mode: str,
    trigger_reason: str,
    was_interrupted: bool = False,
) -> dict[str, Any]:
    wid = wait_row["wait_id"]
    policy = load_policy(db.paths)
    max_attempts = int(policy["guardrails"].get("max_done_check_attempts", 2))
    timeout_sec = int(policy["guardrails"].get("worker_timeout_sec", 1800))

    repo_dir = Path(mandate.cwd).expanduser().resolve()
    is_git_repo = (repo_dir / ".git").exists()
    wt_dir = db.paths.worktrees_dir / wid

    base_head = ""
    if is_git_repo:
        base_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        if not wt_dir.exists():
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(wt_dir), base_head],
                cwd=str(repo_dir),
                check=True,
                capture_output=True,
                text=True,
            )
    else:
        wt_dir.mkdir(parents=True, exist_ok=True)

    runtime_sel = select_worker_runtime(db, mandate, mode=mode)
    base_prompt, kb_ctx = build_worker_task_prompt(
        db,
        wait_row,
        mandate,
        wt_dir,
        runner_label=runtime_sel["model"],
        mode=mode,
        was_interrupted=was_interrupted,
    )

    last_err = ""
    llm_summary = ""
    passed = False
    used_runner = runtime_sel["model"]
    attempts_run = 0

    for attempt in range(1, max_attempts + 1):
        attempts_run = attempt
        prompt = (
            base_prompt
            if attempt == 1
            else f"{base_prompt}\n\n上一轮验收命令失败（第 {attempt - 1} 次），输出如下，请修复：\n```\n{last_err}\n```"
        )
        # 若设置了 SIMING_MOCK_WORKER_CMD（测试回放用），跑确定性脚本代替调外部模型
        mock_cmd = os.environ.get("SIMING_MOCK_WORKER_CMD")
        if mock_cmd:
            m_proc = subprocess.run(
                ["/bin/bash", "-c", mock_cmd],
                cwd=str(wt_dir),
                capture_output=True,
                text=True,
                timeout=30,
            )
            res = {"ok": m_proc.returncode == 0, "output": m_proc.stdout.strip(), "error": m_proc.stderr.strip(), "runner": "mock"}
        else:
            res = run_coding_plan_prompt(
                prompt,
                db=db,
                cwd=wt_dir,
                preferred_runner=runtime_sel["preferred_runner"] or mandate.runner,
                read_only=False,
                timeout_sec=timeout_sec,
                ref_id=wid,
            )
        used_runner = str(res.get("runner") or runtime_sel["model"] or "worker")
        llm_summary = res.get("output", "")
        if "AMBIGUOUS:" in llm_summary:
            last_err = llm_summary
            break

        if not mandate.done_check.strip():
            passed = bool(res.get("ok"))
            last_err = res.get("error", "")
            if passed:
                break
        else:
            rc, chk_out = _run_done_check(mandate.done_check, wt_dir, db)
            if rc == 0:
                passed = True
                break
            last_err = f"done_check exit={rc}: {chk_out}"

    # 提交副作用或签发口令前强制重查取消状态，防止长耗时执行期间用户已取消 Wxx
    conn_recheck = db.connect()
    try:
        cur_w = conn_recheck.execute("SELECT status FROM waits WHERE wait_id=?", (wid,)).fetchone()
        if not cur_w or cur_w["status"] in ("CANCELLED", "EXPIRED"):
            if is_git_repo and wt_dir.exists():
                subprocess.run(["git", "worktree", "remove", "--force", str(wt_dir)], cwd=str(repo_dir), capture_output=True)
            shutil.rmtree(wt_dir, ignore_errors=True)
            db.write_audit("A1", f"worker_{mode}_aborted_cancelled", {"status": cur_w["status"] if cur_w else "MISSING"}, target_id=wid)
            return {"ok": False, "aborted": True, "mode": mode, "summary": f"[{wid}] 执行期间已被取消，已清理隔离目录"}
    finally:
        conn_recheck.close()

    # 无论成功或失败，自动向 W3-3 M-SIM 写入本次执行轨迹，让后续跨模型调用继承经验
    _record_worker_msim_outcome(
        db,
        wid=wid,
        mode=mode,
        problem_family=kb_ctx["problem_family"],
        source_model=used_runner,
        task_desc=(mandate.task or wait_row.get("quote") or wid),
        outcome_summary=(llm_summary or "done_check passed") if passed else (last_err or "worker_failed"),
        done_check=mandate.done_check,
        passed=passed,
        attempts=attempts_run,
        runtime_name=runtime_sel["runtime"],
        repo_name=repo_dir.name,
    )

    if not passed:
        fail_msg = (
            f"[{wid}] 后台 {mode} 任务停手待人工确认\n"
            f"触发原因：{trigger_reason}\n"
            f"停止原因：{last_err[:350] or '模型调用未返回成功状态'}\n"
            f"隔离现场保留在：{wt_dir}"
        )
        with db.transaction() as conn:
            cur = conn.execute(
                "UPDATE waits SET status='FAILED', result_summary=?, updated_at=? WHERE wait_id=? AND status NOT IN ('CANCELLED', 'EXPIRED')",
                (fail_msg[:300], iso_now(), wid),
            )
            if cur.rowcount > 0:
                _enqueue_qq_or_morning(db, wid, fail_msg, silent_night=False, rule_id="worker_failed", conn=conn)
        db.write_audit(
            "A1",
            f"worker_{mode}_stopped",
            {
                "error": last_err[:200],
                "runtime": runtime_sel["runtime"],
                "wiki_injected": kb_ctx["wiki_injected"],
                "msim_injected": kb_ctx["msim_injected"],
            },
            target_id=wid,
            exit_code=1,
        )
        return {"ok": False, "mode": mode, "summary": fail_msg}

    if mode == "stage" and is_git_repo:
        subprocess.run(["git", "add", "-A"], cwd=str(wt_dir), check=True, capture_output=True)
        diff_cmd = ["git", "diff", "--cached", "--binary", base_head] if base_head else ["git", "diff", "--cached", "--binary"]
        stat_cmd = ["git", "diff", "--cached", "--stat", base_head] if base_head else ["git", "diff", "--cached", "--stat"]
        diff_proc = subprocess.run(
            diff_cmd,
            cwd=str(wt_dir),
            check=True,
            capture_output=True,
        )
        patch_sha256 = hashlib.sha256(diff_proc.stdout).hexdigest()
        diff_stat = subprocess.run(
            stat_cmd,
            cwd=str(wt_dir),
            capture_output=True,
            text=True,
        ).stdout.strip() or "（无文件行级差异）"

        approval = create_approval(
            db=db,
            action="git_apply_worktree",
            target_id=wid,
            params={
                "repo_dir": str(repo_dir),
                "worktree_dir": str(wt_dir),
                "base_head": base_head,
                "patch_sha256": patch_sha256,
            },
            tier="A2",
        )
        tok = approval["token"]
        msg = (
            f"[{wid}] 后台 stage 改动已就绪并通过验收（`{mandate.done_check}` = 0）\n"
            f"仓库：{repo_dir}\n"
            f"改动统计：\n{diff_stat[:400]}\n"
            f"30 分钟内直接回复 `apply {tok}` 合入主工作区。"
        )
        # 原子检查并置 STAGED：若在 create_approval 间隙用户刚取消了 Wxx，当场作废新口令且不复活状态
        with db.transaction() as conn:
            cur = conn.execute(
                "UPDATE waits SET status='STAGED', result_summary=?, updated_at=? WHERE wait_id=? AND status NOT IN ('CANCELLED', 'EXPIRED')",
                (f"awaiting apply {tok}", iso_now(), wid),
            )
            if cur.rowcount == 0:
                conn.execute("UPDATE approvals SET status='INVALIDATED' WHERE token=?", (tok,))
                return {"ok": False, "aborted": True, "mode": "stage", "summary": f"[{wid}] 签发口令瞬间已被取消"}
            _enqueue_qq_or_morning(db, wid, msg, silent_night=mandate.silent_night, rule_id="worker_stage_ready", conn=conn)
        return {"ok": True, "mode": "stage", "approval_token": tok, "summary": msg}

    # run 模式：验收通过后直接交回执并清理临时 worktree
    if is_git_repo and wt_dir.exists():
        subprocess.run(["git", "worktree", "remove", "--force", str(wt_dir)], cwd=str(repo_dir), capture_output=True)
    shutil.rmtree(wt_dir, ignore_errors=True)

    msg = (
        f"[{wid}] 后台任务已完成\n"
        f"触发：{trigger_reason}\n"
        f"结论摘要：{llm_summary[:450] or '已通过验收命令'}"
    )
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE waits SET status='DONE', result_summary=?, updated_at=? WHERE wait_id=? AND status NOT IN ('CANCELLED', 'EXPIRED')",
            (msg[:300], iso_now(), wid),
        )
        if cur.rowcount > 0:
            _enqueue_qq_or_morning(db, wid, msg, silent_night=mandate.silent_night, rule_id="worker_run_done", conn=conn)
    db.write_audit("A1", "worker_run_done", {"summary": llm_summary[:160]}, target_id=wid)
    return {"ok": True, "mode": "run", "summary": msg}


def run_worker_for_wait(
    db: SimingDB,
    wait_id: str,
    trigger_reason: str = "",
    was_interrupted: bool = False,
) -> dict[str, Any]:
    """加载挂起项、硬重验 Mandate，并按有效模式执行。"""
    wid = wait_id.strip().upper()
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM waits WHERE wait_id = ?", (wid,)).fetchone()
        if not row:
            return {"ok": False, "error": f"挂起项 {wid} 不存在"}
        wait_dict = dict(row)
    finally:
        conn.close()

    if wait_dict["status"] not in ("WAITING", "FIRED"):
        db.write_audit(
            "A1",
            "worker_skipped_inactive_wait",
            {"status": wait_dict["status"]},
            target_id=wid,
        )
        return {
            "ok": False,
            "aborted": True,
            "reason": f"挂起项 {wid} 当前状态为 {wait_dict['status']}，已跳过执行",
        }

    mandate = Mandate.from_json(wait_dict["mandate_json"])
    chk = revalidate_mandate(
        mandate=mandate,
        wait_strength=wait_dict["strength"],
        wait_action_mode=wait_dict["action_mode"],
        db=db,
    )
    if not chk.allowed:
        return {"ok": False, "deferred": True, "reason": chk.reason}

    eff_mode = chk.effective_mode
    downgrade_note = chk.reason if chk.reason != "ok" else ""

    if eff_mode == "remind":
        return _execute_remind(db, wait_dict, mandate, trigger_reason, downgrade_note=downgrade_note)
    if eff_mode == "resume":
        return _execute_resume_herdr(db, wait_dict, mandate, trigger_reason)
    if eff_mode in ("stage", "run"):
        return _execute_worktree_stage_or_run(
            db, wait_dict, mandate, eff_mode, trigger_reason, was_interrupted=was_interrupted
        )
    return _execute_remind(db, wait_dict, mandate, trigger_reason, downgrade_note=f"未知模式 {eff_mode}")
