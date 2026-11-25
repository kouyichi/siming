"""测试数据库、/tmp 防呆红线、wait_source 四种触发器、Mandate 重验、worktree stage 与 apply 纯代码网关。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from siming.approval import create_approval, verify_and_apply_approval
from siming.crypto import contains_otp, load_encrypted_raw, store_encrypted_raw
from siming.db import SimingDB
from siming.doctor import run_replay_fixtures
from siming.evald import consume_pending_effects, process_new_events
from siming.paths import _reject_slash_tmp
from siming.waits import add_wait, cancel_wait, evaluate_waits, list_waits, trigger_wait_now


def test_slash_tmp_rejected() -> None:
    with pytest.raises(RuntimeError, match="禁止使用 /tmp"):
        _reject_slash_tmp(Path("/tmp/siming-bad"))


def test_encrypted_raw_and_otp_guard(sandbox_db: SimingDB) -> None:
    assert contains_otp("【招行】您的登录验证码为 482910，请勿泄露")
    assert not contains_otp("【菜鸟驿站】您的包裹已到站，凭取件码 3-2-1048 取件")
    ref = store_encrypted_raw("机密原始报文 ¥128.00", sandbox_db.paths)
    assert ref.startswith("raw://")
    assert load_encrypted_raw(ref, sandbox_db.paths).decode("utf-8") == "机密原始报文 ¥128.00"


def test_online_backup_and_restore_drill(sandbox_db: SimingDB) -> None:
    res = sandbox_db.online_backup_and_verify()
    assert res["ok"] is True
    assert res["integrity"] == "ok"
    assert Path(res["backup_path"]).is_file()


def test_seeded_omamail_intent_wait_fires_on_reimburse_mention(sandbox_db: SimingDB) -> None:
    waits = list_waits(sandbox_db, status="WAITING")
    assert any("Omamail" in w["quote"] for w in waits)
    active_md = sandbox_db.paths.shared_memory_dir / "ACTIVE_WAITS.md"

    res = evaluate_waits(sandbox_db, intent_texts=["我准备整理本月的报销单了"])
    assert "W01" in res["fired"]
    consume_pending_effects(sandbox_db)
    assert active_md.is_file()


def test_probe_and_event_waits(sandbox_db: SimingDB) -> None:
    marker_file = sandbox_db.paths.tmp_root / "ready.flag"
    w_probe = add_wait(
        sandbox_db,
        when=f"probe:test -f {marker_file}",
        mode="remind",
        quote="等标记文件生成再提醒我",
    )
    # 文件未出现时 probe 不触发
    r1 = evaluate_waits(sandbox_db)
    assert w_probe["wait_id"] not in r1["fired"]

    # 强制把 next_fire_at 拨回当前并创建文件
    with sandbox_db.transaction() as conn:
        conn.execute("UPDATE waits SET next_fire_at=NULL WHERE wait_id=?", (w_probe["wait_id"],))
    marker_file.write_text("ok", encoding="utf-8")
    r2 = evaluate_waits(sandbox_db)
    assert w_probe["wait_id"] in r2["fired"]

    # event 触发器测试
    w_ev = add_wait(
        sandbox_db,
        when="event:洗烘机.*签收",
        mode="remind",
        quote="等洗烘机签收再约安装",
    )
    with sandbox_db.transaction() as conn:
        sandbox_db.append_event(conn, "buy_track", "order:washer", "西门子洗烘机已签收")
    ev_res = process_new_events(sandbox_db)
    assert w_ev["wait_id"] in ev_res["fired_waits"]
    assert cancel_wait(sandbox_db, "W99") is False


def test_stage_worktree_done_check_and_apply_gate(
    sandbox_db: SimingDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 在沙盒内初始化一个干净测试 Git 仓库
    repo = sandbox_db.paths.tmp_root / "demo-repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=repo, check=True)
    (repo / "hello.txt").write_text("old\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=repo, check=True, capture_output=True)

    monkeypatch.setenv("SIMING_MOCK_WORKER_CMD", "printf 'new-content\\n' > hello.txt")
    w = add_wait(
        sandbox_db,
        when="1m",
        mode="stage",
        cwd=str(repo),
        task="把 hello.txt 改为 new-content",
        done_check="grep -qx 'new-content' hello.txt",
        quote="等一会改 hello.txt",
    )
    assert trigger_wait_now(sandbox_db, w["wait_id"], "unit test")
    consume_pending_effects(sandbox_db)
    consume_pending_effects(sandbox_db)

    # apply 前主工作区保持原样
    assert (repo / "hello.txt").read_text(encoding="utf-8") == "old\n"

    conn = sandbox_db.connect()
    ap_row = conn.execute("SELECT token FROM approvals WHERE target_id=?", (w["wait_id"],)).fetchone()
    conn.close()
    assert ap_row is not None
    token = ap_row["token"]

    # 执行纯代码 apply 口令合入主工作区
    ap_res = verify_and_apply_approval(sandbox_db, token)
    assert ap_res["ok"] is True
    assert (repo / "hello.txt").read_text(encoding="utf-8") == "new-content\n"

    # 同一口令二次重放必须被拒绝
    ap_dup = verify_and_apply_approval(sandbox_db, token)
    assert ap_dup["ok"] is False


def test_approval_tamper_rejected(sandbox_db: SimingDB) -> None:
    ap = create_approval(
        sandbox_db,
        action="append_block_with_backup",
        target_id="S01",
        params={"path": str(sandbox_db.paths.tmp_root / "a.md"), "marker": "M", "block": "M"},
    )
    with sandbox_db.transaction() as conn:
        conn.execute(
            "UPDATE approvals SET params_json=? WHERE token=?",
            ('{"path":"/etc/passwd","marker":"M","block":"M"}', ap["token"]),
        )
    res = verify_and_apply_approval(sandbox_db, ap["token"])
    assert res["ok"] is False
    assert "摘要校验失败" in res["error"]


def test_replay_all_fixtures(sandbox_db: SimingDB) -> None:
    rep = run_replay_fixtures(sandbox_db.paths, fixture="all")
    assert rep["ok"] is True
    assert len(rep["results"]) == 4
