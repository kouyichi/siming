"""S2-C：显式长任务零轮询完工回执（siming run）与 herdr TUI Agent 单次状态转换提醒（siming watch）。
- 遵循审批决定 4=B：完工/阻塞/异常中断通知附带经过 sanitize_terminal_tail 严格脱敏的最后 5 行输出。
- 无活跃 watch 时零轮询开销；永不向现有用户 pane 发送任何按键或抢占 Hyprland 焦点。
"""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

from siming.config import load_policy
from siming.crypto import redact_sensitive, sanitize_terminal_tail
from siming.db import SimingDB, current_boot_id, iso_now, now_local, parse_iso, pid_is_siming_process
from siming.evald import consume_pending_effects

_BLOCKED_AGENT_STATES = {"blocked", "waiting_input", "needs_attention", "approval_needed", "permission", "waiting_approval"}
_DONE_AGENT_STATES = {"idle", "done"}
_ROLLING_TAIL_MAX_BYTES = 65536


def _format_duration(seconds: float) -> str:
    sec = max(0, int(round(seconds)))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m{sec % 60:02d}s"
    return f"{sec // 3600}h{(sec % 3600) // 60:02d}m"


def run_wrapped_command(
    db: SimingDB,
    cmd_argv: list[str],
    name: str = "",
    cwd: str = "",
    notify_qq: bool = True,
    respect_quiet: bool = False,
) -> dict[str, Any]:
    """执行 `siming run -- <cmd>`：实时透传终端输出、落本机日志，并在退出瞬间推送退出码+耗时+脱敏末尾 5 行。"""
    if not cmd_argv:
        return {"ok": False, "exit_code": 2, "error": "缺少待执行命令（用法：siming run -- <命令>）"}

    db.init_db()
    work_cwd = str(Path(cwd).expanduser().resolve()) if cwd else os.getcwd()
    raw_name = name.strip() or " ".join(cmd_argv)
    task_name = redact_sensitive(raw_name, db.paths)[:80]
    redacted_argv = [redact_sensitive(a, db.paths) for a in cmd_argv]
    started_dt = now_local()
    started_ts = iso_now(started_dt)
    boot_id = current_boot_id()
    wrap_pid = os.getpid()

    with db.transaction() as conn:
        run_id = db.next_seq_id(conn, "R")
        log_path = db.paths.runs_dir / f"{run_id}.log"
        conn.execute(
            """
            INSERT INTO task_runs (
                run_id, kind, name, status, pid, wrapper_pid, boot_id, cwd, cmd_json, log_path,
                notify_qq, started_at, updated_at
            ) VALUES (?, 'cmd', ?, 'RUNNING', ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                task_name,
                wrap_pid,
                wrap_pid,
                boot_id,
                work_cwd,
                json.dumps(redacted_argv, ensure_ascii=False),
                str(log_path),
                1 if notify_qq else 0,
                started_ts,
                started_ts,
            ),
        )
        db.record_human_use("task_run", "siming_run", ref_id=run_id, conn=conn)

    env = dict(os.environ)
    env.setdefault("TMPDIR", str(db.paths.tmp_root.parent))

    fd_log = os.open(str(log_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    tail_buf = bytearray()
    exit_code = 1
    start_mono = time.monotonic()

    try:
        proc = subprocess.Popen(
            cmd_argv,
            cwd=work_cwd,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
        with db.transaction() as conn:
            conn.execute(
                "UPDATE task_runs SET pid=?, wrapper_pid=?, updated_at=? WHERE run_id=?",
                (proc.pid, wrap_pid, iso_now(), run_id),
            )

        def _forward_term(signum: int, _frame: Any) -> None:
            if proc.poll() is None:
                try:
                    proc.send_signal(signum)
                except OSError:
                    pass

        # SIGINT 在同一终端进程组内已由内核直达子进程，包装器仅忽略自身 KeyboardInterrupt 等待子进程优雅收尾（防双发 SIGINT 打断 PyTorch 保存）
        old_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
        old_term = signal.signal(signal.SIGTERM, _forward_term)
        try:
            assert proc.stdout is not None
            pipe_fd = proc.stdout.fileno()
            os.set_blocking(pipe_fd, False)
            sel = selectors.DefaultSelector()
            sel.register(pipe_fd, selectors.EVENT_READ)
            while True:
                events = sel.select(timeout=0.4)
                if events:
                    try:
                        chunk = os.read(pipe_fd, 8192)
                    except BlockingIOError:
                        chunk = b""
                    if not chunk:
                        if proc.poll() is not None:
                            break
                        continue
                    os.write(fd_log, chunk)
                    try:
                        sys.stdout.buffer.write(chunk)
                        sys.stdout.buffer.flush()
                    except OSError:
                        pass
                    tail_buf.extend(chunk)
                    if len(tail_buf) > _ROLLING_TAIL_MAX_BYTES:
                        del tail_buf[: len(tail_buf) - _ROLLING_TAIL_MAX_BYTES]
                elif proc.poll() is not None:
                    # 非阻塞限时排空剩余管道（防后台孙进程继承 stdout 造成无限挂死！）
                    drain_deadline = time.monotonic() + 1.2
                    while time.monotonic() < drain_deadline:
                        if not sel.select(timeout=0.1):
                            break
                        try:
                            rem = os.read(pipe_fd, 8192)
                        except BlockingIOError:
                            break
                        if not rem:
                            break
                        os.write(fd_log, rem)
                        try:
                            sys.stdout.buffer.write(rem)
                            sys.stdout.buffer.flush()
                        except OSError:
                            pass
                        tail_buf.extend(rem)
                    break
            exit_code = proc.wait(timeout=5)
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)
    except FileNotFoundError:
        err_line = f"命令不存在: {cmd_argv[0]}\n".encode("utf-8")
        os.write(fd_log, err_line)
        tail_buf.extend(err_line)
        exit_code = 127
    finally:
        os.close(fd_log)

    elapsed = time.monotonic() - start_mono
    dur_str = _format_duration(elapsed)
    tail_5 = sanitize_terminal_tail(
        tail_buf.decode("utf-8", errors="replace"), max_lines=5, paths=db.paths
    )
    fin_ts = iso_now()
    final_status = "DONE" if exit_code == 0 else "FAILED"

    icon = "✅" if exit_code == 0 else "❌"
    msg_lines = [
        f"{icon} [{run_id}] 长任务{'已完成' if exit_code == 0 else '失败退出'}：{task_name}",
        f"退出码：{exit_code} ｜ 耗时：{dur_str} ｜ 目录：{work_cwd}",
        f"本机日志：{log_path}",
    ]
    if tail_5:
        msg_lines.append("末尾 5 行（已脱敏）：\n" + "\n".join(f"  {ln}" for ln in tail_5.splitlines()))
    msg = "\n".join(msg_lines)

    # 终态与通知 effect 同事务原子落盘，即使当下网络抖动也由 effects 队列持久重试不丢回执！
    with db.transaction() as conn:
        conn.execute(
            """
            UPDATE task_runs
            SET status=?, exit_code=?, tail_excerpt=?, updated_at=?, finished_at=?
            WHERE run_id=?
            """,
            (final_status, exit_code, tail_5, fin_ts, fin_ts, run_id),
        )
        if notify_qq:
            db.enqueue_effect(
                conn,
                "send_qq",
                {
                    "ref_id": run_id,
                    "rule_id": "task_run_done",
                    "urgent": (exit_code != 0 and not respect_quiet),
                    "bypass_quiet": (not respect_quiet),
                    "text": msg,
                },
            )
        db.write_audit(
            "A0",
            "task_run_finished",
            {"run_id": run_id, "exit_code": exit_code, "duration": dur_str},
            target_id=run_id,
            exit_code=exit_code,
            conn=conn,
        )

    if notify_qq:
        consume_pending_effects(db, include_workers=False)
    return {
        "ok": exit_code == 0,
        "run_id": run_id,
        "status": final_status,
        "exit_code": exit_code,
        "duration": dur_str,
        "log_path": str(log_path),
        "tail_excerpt": tail_5,
    }


def _default_herdr_json(db: SimingDB, args: list[str]) -> dict[str, Any]:
    if os.environ.get("SIMING_DRY_RUN_HERDR") == "1" or os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return {"ok": False, "offline": True}
    policy = load_policy(db.paths)
    herdr_bin = str(policy["coding_plan"].get("herdr_bin") or "/usr/bin/herdr")
    if not Path(herdr_bin).is_file():
        return {"ok": False, "missing": True}
    try:
        proc = subprocess.run([herdr_bin, *args], capture_output=True, text=True, timeout=4)
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout).strip()[:160]}
    try:
        return json.loads(proc.stdout or "{}")
    except Exception:
        return {"ok": True, "raw": (proc.stdout or "")}


def _default_pane_tail_reader(db: SimingDB, pane_id: str, lines: int = 12) -> str:
    if os.environ.get("SIMING_DRY_RUN_HERDR") == "1" or os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return ""
    policy = load_policy(db.paths)
    herdr_bin = str(policy["coding_plan"].get("herdr_bin") or "/usr/bin/herdr")
    if not Path(herdr_bin).is_file():
        return ""
    try:
        proc = subprocess.run(
            [herdr_bin, "pane", "read", "--lines", str(lines), "--format", "text", pane_id],
            capture_output=True,
            text=True,
            timeout=4,
        )
        if proc.returncode != 0:
            return ""
        raw = (proc.stdout or "").strip()
        if raw.startswith("{"):
            try:
                obj = json.loads(raw)
                res = obj.get("result") or {}
                if isinstance(res, dict) and isinstance(res.get("text"), str):
                    return res["text"]
                if isinstance(res, dict) and isinstance(res.get("lines"), list):
                    return "\n".join(str(x) for x in res["lines"])
            except Exception:
                pass
        return raw
    except Exception:
        return ""


def _snapshot_herdr_panes(
    db: SimingDB,
    herdr_runner: Optional[Callable[[list[str]], dict[str, Any]]] = None,
) -> tuple[bool, dict[str, dict[str, Any]]]:
    """合并 herdr pane list 与 herdr agent list，返回 (daemon_reachable, {pane_id: info})。"""
    call = herdr_runner or (lambda a: _default_herdr_json(db, a))
    p_resp = call(["pane", "list"])
    a_resp = call(["agent", "list"])

    panes_raw = ((p_resp.get("result") or {}).get("panes")) if isinstance(p_resp, dict) else None
    agents_raw = ((a_resp.get("result") or {}).get("agents")) if isinstance(a_resp, dict) else None
    if panes_raw is None and agents_raw is None:
        return (False, {})

    merged: dict[str, dict[str, Any]] = {}
    for p in panes_raw or []:
        if isinstance(p, dict) and p.get("pane_id"):
            merged[str(p["pane_id"])] = dict(p)
    for a in agents_raw or []:
        if isinstance(a, dict) and a.get("pane_id"):
            pid = str(a["pane_id"])
            cur = merged.setdefault(pid, {})
            cur.update(a)
    return (True, merged)


def watch_herdr_pane(
    db: SimingDB,
    target: str,
    label: str = "",
    herdr_runner: Optional[Callable[[list[str]], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """登记对指定 herdr pane/tab 的单次状态转换提醒（绑定 terminal_id + session_identity + completion_seq）。"""
    db.init_db()
    call = herdr_runner or (lambda a: _default_herdr_json(db, a))
    tgt = (target or "current").strip()

    if tgt == "current":
        cur_resp = call(["pane", "current"])
        cur_pane = ((cur_resp.get("result") or {}).get("pane") or cur_resp.get("result") or {}) if isinstance(cur_resp, dict) else {}
        if isinstance(cur_pane, dict) and cur_pane.get("pane_id"):
            tgt = str(cur_pane["pane_id"])

    ok, pane_map = _snapshot_herdr_panes(db, herdr_runner=herdr_runner)
    if not ok:
        return {"ok": False, "error": "无法连接 herdr 守护（请确认在桌面会话或指定有效 herdr 环境）"}

    matched_info: Optional[dict[str, Any]] = pane_map.get(tgt)
    if matched_info is None:
        # 支持直接传 tab_id（如 w4:t7N）匹配其下唯一或首个活跃 pane
        by_tab = [v for v in pane_map.values() if str(v.get("tab_id") or "") == tgt]
        if by_tab:
            matched_info = by_tab[0]

    if matched_info is None:
        return {"ok": False, "error": f"未在 herdr 中找到面板或标签 `{target}`"}

    pane_id = str(matched_info["pane_id"])
    terminal_id = str(matched_info.get("terminal_id") or "")
    sess_obj = matched_info.get("agent_session") or {}
    sess_id = str(sess_obj.get("value") or "") if isinstance(sess_obj, dict) else ""
    base_seq = int(matched_info.get("completion_seq") or 0)
    agent_status = str(matched_info.get("agent_status") or "unknown").lower()
    disp_name = (
        label.strip()
        or str(matched_info.get("label") or "")
        or str(matched_info.get("terminal_title_stripped") or "")
        or pane_id
    )[:80]
    cwd = str(matched_info.get("foreground_cwd") or matched_info.get("cwd") or "")
    ts = iso_now()

    with db.transaction() as conn:
        # 同一 pane 已有活跃监听（含处于 BLOCKED 等待人工审批中）时幂等刷新，不重复刷两条通知
        existing = conn.execute(
            "SELECT run_id FROM task_runs WHERE kind='herdr_watch' AND status IN ('RUNNING', 'BLOCKED') AND target_pane=?",
            (pane_id,),
        ).fetchone()
        run_id = str(existing["run_id"]) if existing else db.next_seq_id(conn, "R")
        if existing:
            conn.execute(
                """
                UPDATE task_runs
                SET status='RUNNING', name=?, cwd=?, terminal_id=?, session_identity=?, baseline_seq=?,
                    blocked_notified_seq=-1, last_agent_status=?, finished_at=NULL, updated_at=?
                WHERE run_id=?
                """,
                (disp_name, cwd, terminal_id, sess_id, base_seq, agent_status, ts, run_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO task_runs (
                    run_id, kind, name, status, cwd, target_pane, terminal_id,
                    session_identity, baseline_seq, last_agent_status, notify_qq,
                    started_at, updated_at
                ) VALUES (?, 'herdr_watch', ?, 'RUNNING', ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (run_id, disp_name, cwd, pane_id, terminal_id, sess_id, base_seq, agent_status, ts, ts),
            )
        db.record_human_use("herdr_watch", "siming_watch", ref_id=run_id, conn=conn)
        db.write_audit(
            "A0",
            "herdr_watch_added",
            {"pane_id": pane_id, "baseline_seq": base_seq, "agent_status": agent_status},
            target_id=run_id,
            conn=conn,
        )

    return {
        "ok": True,
        "run_id": run_id,
        "pane_id": pane_id,
        "name": disp_name,
        "agent_status": agent_status,
        "baseline_seq": base_seq,
        "session_identity": sess_id,
    }


def poll_herdr_watches(
    db: SimingDB,
    herdr_runner: Optional[Callable[[list[str]], dict[str, Any]]] = None,
    pane_reader: Optional[Callable[[str], str]] = None,
) -> dict[str, Any]:
    """推进所有活跃的 herdr_watch：无活跃订阅时 0 外部调用直接返回！"""
    db.init_db()
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM task_runs WHERE kind='herdr_watch' AND status IN ('RUNNING', 'BLOCKED') ORDER BY started_at ASC"
        ).fetchall()
        watches = [dict(r) for r in rows]
    finally:
        conn.close()

    if not watches:
        return {"active": 0, "notified": []}

    reachable, pane_map = _snapshot_herdr_panes(db, herdr_runner=herdr_runner)
    if not reachable:
        return {"active": len(watches), "notified": [], "skipped": "herdr_unreachable"}

    read_tail = pane_reader or (lambda pid: _default_pane_tail_reader(db, pid, lines=12))
    notified: list[dict[str, str]] = []
    terminated_cnt = 0
    ts = iso_now()

    for w in watches:
        rid = str(w["run_id"])
        pid = str(w["target_pane"])
        info = pane_map.get(pid)

        # 1. 面板已关闭、终端实例已重建或 Agent 会话已切换到另一轮对话 -> 报 INTERRUPTED 并解除监听
        cur_term = str(info.get("terminal_id") or "") if info else ""
        sess_obj = (info.get("agent_session") or {}) if info else {}
        cur_sess = str(sess_obj.get("value") or "") if isinstance(sess_obj, dict) else ""

        lost_reason = ""
        if info is None:
            lost_reason = f"面板 `{pid}` 已关闭"
        elif w["terminal_id"] and cur_term and cur_term != w["terminal_id"]:
            lost_reason = f"面板 `{pid}` 终端实例已更换"
        elif w["session_identity"] and cur_sess and cur_sess != w["session_identity"]:
            lost_reason = f"面板 `{pid}` 已切换至新会话 ({cur_sess[:8]})"

        if lost_reason:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE task_runs SET status='INTERRUPTED', updated_at=?, finished_at=? WHERE run_id=?",
                    (ts, ts, rid),
                )
                if int(w["notify_qq"]):
                    db.enqueue_effect(
                        conn,
                        "send_qq",
                        {
                            "ref_id": rid,
                            "rule_id": "herdr_watch",
                            "bypass_quiet": True,
                            "text": f"⚠️ [{rid}] herdr 监听结束（{w['name']}）：{lost_reason}，未确认完工，请按需查看。",
                        },
                    )
            notified.append({"run_id": rid, "status": "INTERRUPTED"})
            terminated_cnt += 1
            continue

        assert info is not None
        cur_status = str(info.get("agent_status") or "unknown").lower()
        cur_seq = int(info.get("completion_seq") or 0)
        base_seq = int(w["baseline_seq"] or 0)
        prev_status = str(w["last_agent_status"] or "").lower()
        prev_blk_seq = int(w.get("blocked_notified_seq") if w.get("blocked_notified_seq") is not None else -1)

        # 2. 进入阻塞 / 等待用户审批状态 -> 单次提醒（保留监听在活跃池，用户审批继续跑完后仍会通知 DONE！）
        if cur_status in _BLOCKED_AGENT_STATES:
            if w["status"] != "BLOCKED" or prev_blk_seq != cur_seq:
                tail_5 = sanitize_terminal_tail(read_tail(pid), max_lines=5, paths=db.paths)
                msg = f"⏸️ [{rid}] herdr Agent 等待你输入/审批：{w['name']} (`{pid}`)"
                if tail_5:
                    msg += "\n末尾 5 行（已脱敏）：\n" + "\n".join(f"  {ln}" for ln in tail_5.splitlines())
                with db.transaction() as conn:
                    conn.execute(
                        "UPDATE task_runs SET status='BLOCKED', blocked_notified_seq=?, last_agent_status=?, tail_excerpt=?, updated_at=? WHERE run_id=?",
                        (cur_seq, cur_status, tail_5, ts, rid),
                    )
                    if int(w["notify_qq"]):
                        db.enqueue_effect(
                            conn,
                            "send_qq",
                            {"ref_id": rid, "rule_id": "herdr_watch", "urgent": True, "bypass_quiet": True, "text": msg},
                        )
                notified.append({"run_id": rid, "status": "BLOCKED"})
            continue

        # 3. 本轮完成待查看：completion_seq 递增且回到 idle/done，或从 working/blocked 转为 idle/done
        completed_now = (cur_seq > base_seq and cur_status in _DONE_AGENT_STATES) or (
            (prev_status == "working" or prev_status in _BLOCKED_AGENT_STATES) and cur_status in _DONE_AGENT_STATES
        )
        if completed_now:
            tail_5 = sanitize_terminal_tail(read_tail(pid), max_lines=5, paths=db.paths)
            msg = f"✅ [{rid}] herdr Agent 本轮完成待查看：{w['name']} (`{pid}`)"
            if w["cwd"]:
                msg += f"\n目录：{w['cwd']}"
            if tail_5:
                msg += "\n末尾 5 行（已脱敏）：\n" + "\n".join(f"  {ln}" for ln in tail_5.splitlines())
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE task_runs SET status='DONE', last_agent_status=?, tail_excerpt=?, updated_at=?, finished_at=? WHERE run_id=?",
                    (cur_status, tail_5, ts, ts, rid),
                )
                if int(w["notify_qq"]):
                    db.enqueue_effect(
                        conn,
                        "send_qq",
                        {"ref_id": rid, "rule_id": "herdr_watch", "bypass_quiet": True, "text": msg},
                    )
            notified.append({"run_id": rid, "status": "DONE"})
            terminated_cnt += 1
            continue

        if cur_status != prev_status or w["status"] == "BLOCKED":
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE task_runs SET status='RUNNING', last_agent_status=?, updated_at=? WHERE run_id=?",
                    (cur_status, ts, rid),
                )

    if notified:
        consume_pending_effects(db, include_workers=False)
    return {"active": len(watches) - terminated_cnt, "notified": notified}


def reconcile_interrupted_task_runs(db: SimingDB) -> list[str]:
    """巡检因断电/重启/SIGKILL 意外消失的 `siming run` 包装进程，诚实发一次 INTERRUPTED 回执。"""
    db.init_db()
    now = now_local()
    ts = iso_now(now)
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM task_runs WHERE kind='cmd' AND status='RUNNING'"
        ).fetchall()
        running_cmds = [dict(r) for r in rows]
    finally:
        conn.close()

    interrupted: list[str] = []
    for r in running_cmds:
        chk_pid = r.get("wrapper_pid") or r.get("pid")
        boot_id = str(r.get("boot_id") or "")
        # 给刚启动或正在正常收尾写库的进程留 5s 缓冲防误判
        if (now - parse_iso(r["updated_at"])).total_seconds() < 5.0:
            continue
        if pid_is_siming_process(chk_pid, expected_boot_id=boot_id):
            continue

        tail_5 = ""
        log_p = Path(str(r.get("log_path") or ""))
        if log_p.is_file():
            try:
                sz = log_p.stat().st_size
                with log_p.open("rb") as fh:
                    if sz > 16384:
                        fh.seek(sz - 16384)
                    raw_tail = fh.read().decode("utf-8", errors="replace")
                tail_5 = sanitize_terminal_tail(raw_tail, max_lines=5, paths=db.paths)
            except OSError:
                pass

        rid = str(r["run_id"])
        msg = f"⚠️ [{rid}] 长任务异常中断（进程已消失，可能因重启或强杀）：{r['name']}\n本机日志：{r.get('log_path') or '-'}"
        if tail_5:
            msg += "\n末尾 5 行（已脱敏）：\n" + "\n".join(f"  {ln}" for ln in tail_5.splitlines())

        with db.transaction() as conn:
            cur = conn.execute(
                "UPDATE task_runs SET status='INTERRUPTED', tail_excerpt=?, updated_at=?, finished_at=? WHERE run_id=? AND status='RUNNING'",
                (tail_5, ts, ts, rid),
            )
            if cur.rowcount == 0:
                continue
            if int(r.get("notify_qq") or 0):
                db.enqueue_effect(
                    conn,
                    "send_qq",
                    {"ref_id": rid, "rule_id": "task_run_interrupted", "bypass_quiet": True, "text": msg},
                )
        interrupted.append(rid)

    if interrupted:
        consume_pending_effects(db, include_workers=False)
    return interrupted


def probe_herdr_pane_idle(
    db: SimingDB,
    pane_id: str,
    herdr_runner: Optional[Callable[[list[str]], dict[str, Any]]] = None,
) -> tuple[bool, str]:
    ok, pane_map = _snapshot_herdr_panes(db, herdr_runner=herdr_runner)
    if not ok:
        return False, "herdr unreachable"
    info = pane_map.get(pane_id)
    if not info:
        return False, f"pane {pane_id} not found"
    st = str(info.get("agent_status") or "unknown").lower()
    return st in _DONE_AGENT_STATES, f"{pane_id} status={st}"


def ingest_remote_run_receipt(db: SimingDB, raw_json_text: str) -> dict[str, Any]:
    """S3-4：处理从 81 `/siming/in/run-receipt` 同步回本机的远端服务器（如 gpu-server）任务完工回执。"""
    import hashlib
    from siming.modules import is_module_enabled

    db.init_db()
    try:
        data = json.loads(raw_json_text)
        if not isinstance(data, dict):
            return {"ok": False, "error": "receipt_not_dict"}
    except Exception as exc:
        return {"ok": False, "error": f"invalid_receipt_json:{exc}"}

    ext_run_id = str(data.get("run_id") or "").strip()[:64]
    host = redact_sensitive(str(data.get("host") or "remote").strip(), db.paths)[:48]
    cwd = redact_sensitive(str(data.get("cwd") or "").strip(), db.paths)[:160]
    cmd_str = redact_sensitive(str(data.get("cmd") or "").strip(), db.paths)[:120]
    if not cmd_str:
        return {"ok": False, "error": "empty_cmd"}

    try:
        exit_code = int(data.get("exit_code", 0))
    except Exception:
        exit_code = 1
    try:
        dur_sec = max(0.0, float(data.get("duration_sec", 0.0)))
    except Exception:
        dur_sec = 0.0

    dur_str = _format_duration(dur_sec)
    tail_5 = sanitize_terminal_tail(str(data.get("tail") or ""), max_lines=5, paths=db.paths)
    fp = f"remote_receipt:{ext_run_id or hashlib.sha256(raw_json_text.encode('utf-8')).hexdigest()[:24]}"
    ts = iso_now()
    final_status = "DONE" if exit_code == 0 else "FAILED"

    with db.transaction() as conn:
        ev_id = db.append_event(
            conn,
            source="remote_run_receipt",
            thread_key=f"remote_run:{host}",
            summary=f"[{host}] 远端任务{'完成' if exit_code == 0 else f'失败({exit_code})'}：{cmd_str[:56]}",
            payload={
                "ext_run_id": ext_run_id,
                "host": host,
                "cwd": cwd,
                "cmd": cmd_str,
                "exit_code": exit_code,
                "duration": dur_str,
            },
            fingerprint=fp,
        )
        if ev_id is None:
            return {"ok": True, "duplicate": True}

        run_id = db.next_seq_id(conn, "R")
        conn.execute(
            """
            INSERT INTO task_runs (
                run_id, kind, name, status, cwd, cmd_json, exit_code, tail_excerpt,
                notify_qq, started_at, updated_at, finished_at
            ) VALUES (?, 'remote_cmd', ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)
            """,
            (
                run_id,
                f"[{host}] {cmd_str}"[:80],
                final_status,
                cwd,
                json.dumps([cmd_str], ensure_ascii=False),
                exit_code,
                tail_5,
                ts,
                ts,
                ts,
            ),
        )
        db.record_usage_event("remote_run_done", "srr_receipt", actor="system_receipt", ref_id=run_id, conn=conn)

        if is_module_enabled(db, "remote_run"):
            icon = "🖥️✅" if exit_code == 0 else "🖥️❌"
            msg_lines = [
                f"{icon} [{run_id}] 远端任务{'已完成' if exit_code == 0 else '失败退出'}（主机 `{host}`）：{cmd_str}",
                f"退出码：{exit_code} ｜ 耗时：{dur_str}" + (f" ｜ 目录：{cwd}" if cwd else ""),
            ]
            if tail_5:
                msg_lines.append("末尾 5 行（已脱敏）：\n" + "\n".join(f"  {ln}" for ln in tail_5.splitlines()))
            db.enqueue_effect(
                conn,
                "send_qq",
                {
                    "ref_id": run_id,
                    "rule_id": "remote_run_done",
                    "urgent": (exit_code != 0),
                    "bypass_quiet": True,
                    "text": "\n".join(msg_lines),
                },
            )

    return {"ok": True, "run_id": run_id, "status": final_status, "exit_code": exit_code}


def fetch_or_init_edge_receipt_token(db: SimingDB) -> tuple[bool, str]:
    """经 SSH 从 81 读取（若不存在则初始化）受限 receipt-token；离线测试支持环境变量覆盖。"""
    override = os.environ.get("SIMING_RECEIPT_TOKEN_OVERRIDE", "").strip()
    if override:
        return True, override
    if os.environ.get("SIMING_DISABLE_SSH_EDGE") == "1" or os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return False, "offline_mode"

    policy = load_policy(db.paths)
    edge_cfg = policy["edge"]
    host = str(edge_cfg["ssh_host"])
    remote_cli = str(edge_cfg["remote_cli"])
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, f"{remote_cli} receipt-token"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if proc.returncode != 0:
            return False, (proc.stderr or proc.stdout).strip()[:180]
        obj = json.loads((proc.stdout or "").strip() or "{}")
        tok = str(obj.get("receipt_token") or "").strip()
        return (True, tok) if tok else (False, "empty_receipt_token")
    except Exception as exc:
        return False, str(exc)[:180]


def render_remote_srr_hook(
    db: SimingDB,
    host_label: str = "",
    receipt_token: Optional[str] = None,
    install_local_bin: bool = False,
    deploy_ssh_host: str = "",
) -> dict[str, Any]:
    """生成约 28 行 POSIX 兼容的 `srr` (siming-remote-run，避开 Slurm `srun`) 包装函数与本地可执行脚本：
    - 令牌存于 0600 独立文件并通过 `curl -K -` 标准输入传递 Header，共享服务器其他用户 `ps aux` 零泄露；
    - 内置 `$HOME/.cache/srr-spool` 断网暂存重试队列。
    """
    policy = load_policy(db.paths)
    base_in_url = str(policy["edge"]["public_in_url"]).rstrip("/")
    receipt_url = f"{base_in_url}/run-receipt"

    tok = receipt_token or ""
    tok_err = ""
    if not tok:
        ok_t, val_t = fetch_or_init_edge_receipt_token(db)
        if ok_t:
            tok = val_t
        else:
            tok_err = val_t

    host_expr = f'"{host_label}"' if host_label else '"$(hostname -s 2>/dev/null || hostname)"'
    func_snippet = f"""# === siming-remote-run (srr) 零轮询远端完工回执 ===
srr() {{
  [ "$#" -gt 0 ] || {{ echo "Usage: srr <command> [args...]" >&2; return 2; }}
  _srr_tok_f="${{SRR_TOKEN_FILE:-$HOME/.config/siming/receipt-token}}"
  _srr_spool="$HOME/.cache/srr-spool"
  mkdir -p "$_srr_spool" && chmod 700 "$_srr_spool" 2>/dev/null || true
  _srr_tmp="$(mktemp "${{_srr_spool}}/out.XXXXXX")"
  _srr_t0="$(date +%s)"
  "$@" 2>&1 | tee "$_srr_tmp"
  _srr_rc="${{PIPESTATUS[0]:-$?}}"
  _srr_dur="$(( $(date +%s) - _srr_t0 ))"
  if [ -r "$_srr_tok_f" ]; then
    _srr_id="srr-$(date +%Y%m%d%H%M%S)-$$"
    _srr_json="${{_srr_spool}}/${{_srr_id}}.json"
    python3 - "$_srr_id" {host_expr} "$PWD" "$*" "$_srr_rc" "$_srr_dur" "$_srr_tmp" > "$_srr_json" <<'PY'
import json, pathlib, sys
rid, h, cwd, cmd, rc, dur, tf = sys.argv[1:8]
lines = pathlib.Path(tf).read_text(encoding="utf-8", errors="replace").splitlines()[-8:]
print(json.dumps({{"run_id": rid, "host": h, "cwd": cwd, "cmd": cmd[:200], "exit_code": int(rc), "duration_sec": int(dur), "tail": "\\n".join(lines)[-1800:]}}, ensure_ascii=False))
PY
    for _f in "$_srr_spool"/srr-*.json; do
      [ -f "$_f" ] || continue
      printf 'header = "Authorization: Bearer %s"\\nheader = "Content-Type: application/json"\\n' "$(tr -d '\\r\\n' < "$_srr_tok_f")" | \\
        curl -fsS --connect-timeout 5 -m 10 -X POST -K - --data-binary "@$_f" "{receipt_url}" >/dev/null 2>&1 && rm -f "$_f"
    done
  fi
  rm -f "$_srr_tmp"
  return "$_srr_rc"
}}"""

    local_srr_path = ""
    if install_local_bin:
        if tok:
            tok_p = db.paths.secrets_dir / "receipt-token"
            fd_t = os.open(str(tok_p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd_t, (tok + "\n").encode("utf-8"))
            finally:
                os.close(fd_t)
        bin_p = Path.home() / ".local" / "bin" / "srr"
        bin_p.parent.mkdir(parents=True, exist_ok=True)
        bin_body = (
            "#!/usr/bin/env bash\nset -u\n"
            f'export SRR_TOKEN_FILE="${{SRR_TOKEN_FILE:-{db.paths.secrets_dir / "receipt-token"}}}"\n'
            + func_snippet
            + '\nsrr "$@"\n'
        )
        bin_p.write_text(bin_body, encoding="utf-8")
        os.chmod(bin_p, 0o755)
        local_srr_path = str(bin_p)

    deployed_remote = False
    deploy_err = ""
    if deploy_ssh_host.strip():
        if not tok:
            deploy_err = tok_err or "missing_receipt_token"
        else:
            remote_setup = (
                "umask 077 && mkdir -p \"$HOME/.config/siming\" \"$HOME/.local/bin\" && "
                "cat > \"$HOME/.config/siming/receipt-token\" && chmod 600 \"$HOME/.config/siming/receipt-token\""
            )
            try:
                d_proc = subprocess.run(
                    ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", deploy_ssh_host.strip(), remote_setup],
                    input=tok + "\n",
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if d_proc.returncode == 0:
                    deployed_remote = True
                else:
                    deploy_err = (d_proc.stderr or d_proc.stdout).strip()[:160]
            except Exception as exc:
                deploy_err = str(exc)[:160]

    return {
        "ok": True,
        "receipt_url": receipt_url,
        "has_token": bool(tok),
        "token_error": tok_err,
        "local_srr_path": local_srr_path,
        "deployed_remote": deployed_remote,
        "deploy_error": deploy_err,
        "snippet": func_snippet,
    }
