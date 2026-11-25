"""Phase 2 硬验收（S2-C + 决定 4=B）：siming run 零轮询包装器（附脱敏最后 5 行）、断电进程消失 INTERRUPTED 恢复、herdr 监听四态转换与 session_scanner 半行截断防护。"""

from __future__ import annotations

import json
import sys
from datetime import timedelta

from siming.db import SimingDB, iso_now, now_local
from siming.session_scanner import scan_agent_sessions
from siming.task_watch import (
    poll_herdr_watches,
    reconcile_interrupted_task_runs,
    run_wrapped_command,
    watch_herdr_pane,
)


def test_siming_run_wrapper_exit_code_and_redacted_last_5_lines(sandbox_db: SimingDB) -> None:
    py = sys.executable
    script = (
        "import sys\n"
        "for i in range(1, 8):\n"
        "    print(f'step {i} ok')\n"
        "print('API_KEY=sk-proj-1234567890abcdefghijklmno')\n"
        "print('Authorization: Bearer super_secret_bearer_token_999999')\n"
        "print('final summary: 42 passed in 1.2s')\n"
        "sys.exit(3)\n"
    )
    res = run_wrapped_command(
        sandbox_db,
        cmd_argv=[py, "-c", script],
        name="模拟评测任务",
        cwd=str(sandbox_db.paths.tmp_root),
        notify_qq=True,
    )
    assert res["ok"] is False
    assert res["exit_code"] == 3
    lines = res["tail_excerpt"].splitlines()
    assert len(lines) == 5
    # 验证最后 5 行精确保留摘要，但 API_KEY 与 Bearer 已被严格脱敏！
    assert "final summary: 42 passed" in res["tail_excerpt"]
    assert "sk-proj-" not in res["tail_excerpt"]
    assert "super_secret_bearer_token" not in res["tail_excerpt"]
    assert "[REDACTED" in res["tail_excerpt"]

    conn = sandbox_db.connect()
    ob = conn.execute("SELECT text FROM outbox WHERE rule_id='task_run_done'").fetchone()
    conn.close()
    assert ob is not None
    assert "退出码：3" in ob["text"]
    assert "末尾 5 行（已脱敏）" in ob["text"]
    assert "super_secret_bearer_token" not in ob["text"]


def test_siming_run_interrupted_recovery_when_pid_vanishes(sandbox_db: SimingDB) -> None:
    log_p = sandbox_db.paths.runs_dir / "R99.log"
    log_p.write_text("epoch 1\nepoch 2\nSECRET=topsecretvalue123\nOOM killed\n", encoding="utf-8")
    old_ts = iso_now(now_local() - timedelta(seconds=30))
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO task_runs (
                run_id, kind, name, status, pid, cwd, log_path, notify_qq, started_at, updated_at
            ) VALUES ('R99', 'cmd', '训练任务', 'RUNNING', 99999999, '/tmp-safe', ?, 1, ?, ?)
            """,
            (str(log_p), old_ts, old_ts),
        )

    intr = reconcile_interrupted_task_runs(sandbox_db)
    assert intr == ["R99"]
    conn = sandbox_db.connect()
    row = conn.execute("SELECT status, tail_excerpt FROM task_runs WHERE run_id='R99'").fetchone()
    conn.close()
    assert row["status"] == "INTERRUPTED"
    assert "OOM killed" in row["tail_excerpt"]
    assert "topsecretvalue123" not in row["tail_excerpt"]


def test_herdr_watch_all_four_lifecycle_branches_and_zero_overhead_when_empty(sandbox_db: SimingDB) -> None:
    calls = 0

    def counting_runner(_args: list[str]) -> dict:
        nonlocal calls
        calls += 1
        return {}

    # 0. 无活跃 watch 时，poll_herdr_watches 零外部调用直接返回
    empty_res = poll_herdr_watches(sandbox_db, herdr_runner=counting_runner)
    assert empty_res["active"] == 0
    assert calls == 0

    state = {
        "agent_status": "working",
        "completion_seq": 10,
        "terminal_id": "term_1",
        "session": "sess-aaa",
        "present": True,
    }

    def fake_herdr(args: list[str]) -> dict:
        if not state["present"]:
            return {"result": {"panes": [], "agents": []}}
        item = {
            "pane_id": "w4:p9",
            "tab_id": "w4:t9",
            "label": "重构路由模块",
            "cwd": "/home/user/Work/siming",
            "terminal_id": state["terminal_id"],
            "agent": "claude",
            "agent_status": state["agent_status"],
            "completion_seq": state["completion_seq"],
            "agent_session": {"value": state["session"]},
        }
        if args == ["pane", "list"]:
            return {"result": {"panes": [item]}}
        if args == ["agent", "list"]:
            return {"result": {"agents": [item]}}
        return {}

    fake_tail = lambda _pid: "line1\nline2\nline3\nTOKEN=secret_abc12345\nAll tests green!\n"

    # 1. working -> idle (completion_seq 10 -> 11) => DONE + 脱敏末尾 5 行
    w1 = watch_herdr_pane(sandbox_db, "w4:p9", herdr_runner=fake_herdr)
    assert w1["ok"] is True
    assert poll_herdr_watches(sandbox_db, herdr_runner=fake_herdr, pane_reader=fake_tail)["notified"] == []

    state["agent_status"] = "idle"
    state["completion_seq"] = 11
    p_done = poll_herdr_watches(sandbox_db, herdr_runner=fake_herdr, pane_reader=fake_tail)
    assert p_done["notified"] == [{"run_id": w1["run_id"], "status": "DONE"}]

    conn = sandbox_db.connect()
    ob_done = conn.execute("SELECT text FROM outbox WHERE ref_id=?", (w1["run_id"],)).fetchone()["text"]
    conn.close()
    assert "本轮完成待查看" in ob_done
    assert "All tests green!" in ob_done
    assert "secret_abc12345" not in ob_done

    # 2. working -> blocked => BLOCKED（等待审批/输入）
    state["agent_status"] = "working"
    w2 = watch_herdr_pane(sandbox_db, "w4:t9", herdr_runner=fake_herdr)
    state["agent_status"] = "blocked"
    p_blk = poll_herdr_watches(sandbox_db, herdr_runner=fake_herdr, pane_reader=lambda _: "Allow bash command? [y/n]")
    assert p_blk["notified"] == [{"run_id": w2["run_id"], "status": "BLOCKED"}]

    # 3. 面板关闭或切换会话 => INTERRUPTED
    state["agent_status"] = "working"
    w3 = watch_herdr_pane(sandbox_db, "w4:p9", herdr_runner=fake_herdr)
    state["present"] = False
    p_lost = poll_herdr_watches(sandbox_db, herdr_runner=fake_herdr, pane_reader=fake_tail)
    assert p_lost["notified"] == [{"run_id": w3["run_id"], "status": "INTERRUPTED"}]


def test_session_scanner_holds_partial_line_and_filters_claude_sidechain(sandbox_db: SimingDB) -> None:
    log_dir = sandbox_db.paths.tmp_root / "sess-p2"
    log_dir.mkdir(parents=True)
    log_f = log_dir / "c1.jsonl"

    # 1. isSidechain=True 子代理消息必须被忽略；紧跟一条尚未写完换行符的半行 JSON
    sidechain = json.dumps(
        {"type": "user", "isSidechain": True, "userType": "external", "message": {"content": "明天上午十点提醒我重启服务器"}}
    )
    partial_user = '{"type": "user", "userType": "external", "message": {"content": "12月10日下午提醒我验收柜子"}}'
    log_f.write_text(sidechain + "\n" + partial_user, encoding="utf-8")

    r1 = scan_agent_sessions(
        sandbox_db, session_roots={"claude": str(log_dir / "*.jsonl")}, bootstrap_to_eof=False
    )
    assert r1["new_waits"] == []

    # 2. 上游进程补写完换行符后，下一轮扫描精准读出刚刚那条半行，且不发即时 QQ 打扰！
    with log_f.open("a", encoding="utf-8") as fh:
        fh.write("\n")

    r2 = scan_agent_sessions(
        sandbox_db, session_roots={"claude": str(log_dir / "*.jsonl")}, bootstrap_to_eof=False
    )
    assert len(r2["new_waits"]) == 1
    w = r2["new_waits"][0]
    assert "-12-10T15:00:00" in str(w["next_fire_at"])

    conn = sandbox_db.connect()
    instant_pushes = conn.execute("SELECT COUNT(*) FROM effects WHERE kind='send_qq'").fetchone()[0]
    conn.close()
    assert instant_pushes == 0
