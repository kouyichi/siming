"""外部推送入口 siming push 与 Hermes cron 输出收割器。"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

import siming.router as router
from siming.cron_harvest import compact_for_digest, harvest_cron_outputs, parse_cron_output
from siming.push import normalize_level, push_external


def _outbox(db, rule):
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM outbox WHERE rule_id=? ORDER BY rowid", (rule,))]
    finally:
        conn.close()


@pytest.fixture()
def asleep(monkeypatch):
    monkeypatch.setattr(router, "_in_quiet_hours", lambda policy, db=None: True)


@pytest.fixture()
def awake(monkeypatch):
    monkeypatch.setattr(router, "_in_quiet_hours", lambda policy, db=None: False)


def test_level_aliases():
    assert normalize_level("L3") == "now"
    assert normalize_level("urgent") == "now"
    assert normalize_level("L2") == "normal"
    assert normalize_level("brief") == "digest"
    with pytest.raises(ValueError):
        normalize_level("loud")


def test_now_level_breaks_through_sleep(sandbox_db, asleep):
    res = push_external(sandbox_db, "1-2", rule_id="x_dm", ref_id="x:user_demo:a", level="now", dedup_window=timedelta(minutes=2))
    assert res["delivered"] is True
    assert res["rule_id"] == "ext_x_dm"
    rows = _outbox(sandbox_db, "ext_x_dm")
    assert [r["status"] for r in rows] == ["SENT"]


def test_normal_level_held_while_asleep(sandbox_db, asleep):
    res = push_external(sandbox_db, "Steam 新免费游戏", rule_id="free_games", level="normal")
    assert res.get("queued_for_morning") is True
    assert _outbox(sandbox_db, "ext_free_games")[0]["status"] == "MORNING_QUEUE"


def test_dedup_zero_allows_repeated_bare_code(sandbox_db, awake):
    for _ in range(3):
        res = push_external(sandbox_db, "1-2", rule_id="x_dm", ref_id="x:dm", level="now", dedup_window=timedelta(0))
        assert res["delivered"] is True
    assert len(_outbox(sandbox_db, "ext_x_dm")) == 3


def test_default_dedup_swallows_same_text(sandbox_db, awake):
    push_external(sandbox_db, "同一条", rule_id="kuma", level="normal")
    res = push_external(sandbox_db, "同一条", rule_id="kuma", level="normal")
    assert res["skipped"] == "duplicate"


def test_frozen_rule_blocks_now_but_hard_rule_passes(sandbox_db, awake):
    push_external(sandbox_db, "first", rule_id="x_activity", level="now")
    push_external(sandbox_db, "first", rule_id="backup_fail", level="now", hard=True)
    with sandbox_db.transaction() as conn:
        conn.execute("UPDATE rule_stats SET mode='FROZEN' WHERE rule_id IN ('ext_x_activity', 'ext_backup_fail')")
    blocked = push_external(sandbox_db, "second", rule_id="x_activity", level="now")
    assert blocked["skipped"] == "rule_demoted_or_observe"
    passed = push_external(sandbox_db, "second", rule_id="backup_fail", level="now")
    assert passed["delivered"] is True


def test_digest_keeps_only_latest_per_ref(sandbox_db, awake):
    push_external(sandbox_db, "PR 12:00", rule_id="pr_digest", ref_id="cron:pr", level="digest")
    res = push_external(sandbox_db, "PR 22:00", rule_id="pr_digest", ref_id="cron:pr", level="digest")
    assert res["queued_for_morning"] is True and res["superseded"] == 1
    rows = _outbox(sandbox_db, "ext_pr_digest")
    assert [(r["text"], r["status"]) for r in rows] == [("PR 12:00", "WITHDRAWN"), ("PR 22:00", "MORNING_QUEUE")]


def test_cli_push_reads_stdin_and_exit_code(sandbox_db, awake, monkeypatch, capsys):
    import io

    from siming import cli

    monkeypatch.setattr(cli, "SimingDB", lambda: sandbox_db)
    # 1. 普通紧急规则直达推送（delivered=True）
    monkeypatch.setattr("sys.stdin", io.StringIO("【备份告警】81 备份失败\n"))
    rc = cli.main(["push", "--rule", "backup_fail", "--level", "now", "--dedup", "2m", "-"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 0 and out["delivered"] is True

    # 2. D3-B：uptime_kuma 默认进入 3 分钟闪断防抖，退出码仍为 0（避免上游触发兜底告警）
    monkeypatch.setattr("sys.stdin", io.StringIO("【主机监控】S-UI：异常\n"))
    rc_kuma = cli.main(["push", "--rule", "uptime_kuma", "--level", "now", "--dedup", "2m", "-"])
    out_kuma = json.loads(capsys.readouterr().out)
    assert rc_kuma == 0 and out_kuma["ok"] is True and out_kuma["debounced"] is True
    assert out_kuma["delivered"] is False and out_kuma["skipped"] == "debounced_3m"

    rc = cli.main(["push", "--rule", "x", "--level", "loud", "hi"])
    assert rc == 2


# ---------------- cron 收割器 ----------------

_OK_MD = """# Cron Job: docker-image-drift

**Job ID:** j1
**Run Time:** 2026-11-22 10:07:47
**Mode:** no_agent (script)

---

🐳 Docker 镜像巡检
🆕 上游有新镜像 2 个:
· caddy
· redis
"""

_SILENT_MD = """# Cron Job: check

**Job ID:** j2
**Run Time:** 2026-11-22 11:00:50
**Mode:** no_agent (script)
**Status:** silent (empty output)
"""

_FAIL_MD = """# Cron Job: check

**Job ID:** j2
**Run Time:** 2026-11-22 11:00:50
**Mode:** no_agent (script)
**Status:** script failed

Script exited with code 127
stderr:
boom
"""


def test_parse_cron_output_kinds():
    assert parse_cron_output(_SILENT_MD)[0] == "silent"
    kind, body = parse_cron_output(_OK_MD)
    assert kind == "ok" and body.startswith("🐳")
    kind, body = parse_cron_output(_FAIL_MD)
    assert kind == "failed" and "code 127" in body


def test_compact_for_digest_truncates():
    body = "\n".join(["头"] + ["━━━━━━━━"] + [f"行{i}" + "x" * 200 for i in range(20)])
    out = compact_for_digest(body, max_lines=5, max_line_chars=30)
    lines = out.splitlines()
    assert len(lines) == 6 and lines[-1] == "…（另 16 行）"
    assert all(len(ln) <= 30 for ln in lines[:5])


def _setup_cron(tmp: Path, deliver_local: bool = True) -> tuple[Path, Path, dict]:
    root = tmp / "output"
    (root / "j1").mkdir(parents=True)
    (root / "j2").mkdir(parents=True)
    jobs_json = tmp / "jobs.json"
    deliver = "local" if deliver_local else "qqbot:X"
    jobs_json.write_text(json.dumps({"jobs": [{"id": "j1", "deliver": deliver}, {"id": "j2", "deliver": deliver}]}))
    table = {
        "j1": {"name": "docker-image-drift", "rule": "docker_drift", "level": "digest", "max_lines": 8, "max_line_chars": 90},
        "j2": {"name": "check-81-retired", "rule": "check_81_retired", "level": "normal"},
    }
    return root, jobs_json, table


def test_harvest_first_run_only_sets_cursor_then_routes(sandbox_db, awake):
    tmp = sandbox_db.paths.tmp_root / "cron"
    root, jobs_json, table = _setup_cron(tmp)
    (root / "j1" / "2026-11-15_10-07-47.md").write_text(_OK_MD)
    (root / "j2" / "2026-11-15_11-00-50.md").write_text(_SILENT_MD)

    first = harvest_cron_outputs(sandbox_db, output_root=root, jobs_json=jobs_json, jobs=table)
    assert first["pushed"] == []  # 不回放历史

    (root / "j1" / "2026-11-22_10-07-47.md").write_text(_OK_MD)
    (root / "j2" / "2026-11-22_11-00-50.md").write_text(_SILENT_MD)
    (root / "j2" / "2026-11-29_11-00-50.md").write_text(_FAIL_MD)
    second = harvest_cron_outputs(sandbox_db, output_root=root, jobs_json=jobs_json, jobs=table)
    kinds = sorted((p["job"], p["kind"]) for p in second["pushed"])
    assert kinds == [("check-81-retired", "failed"), ("docker-image-drift", "ok")]

    digest = _outbox(sandbox_db, "ext_docker_drift")
    assert digest[0]["status"] == "MORNING_QUEUE" and "caddy" in digest[0]["text"]
    fail = _outbox(sandbox_db, "ext_check_81_retired_fail")
    assert fail[0]["status"] == "SENT" and "11-29 11:00" in fail[0]["text"]

    third = harvest_cron_outputs(sandbox_db, output_root=root, jobs_json=jobs_json, jobs=table)
    assert third["pushed"] == []  # 游标已推进，不重复


def test_harvest_skips_jobs_still_delivering_to_qq(sandbox_db, awake):
    tmp = sandbox_db.paths.tmp_root / "cron2"
    root, jobs_json, table = _setup_cron(tmp, deliver_local=False)
    (root / "j1" / "2026-11-15_10-07-47.md").write_text(_OK_MD)
    harvest_cron_outputs(sandbox_db, output_root=root, jobs_json=jobs_json, jobs=table)
    (root / "j1" / "2026-11-22_10-07-47.md").write_text(_OK_MD)
    res = harvest_cron_outputs(sandbox_db, output_root=root, jobs_json=jobs_json, jobs=table)
    assert res["pushed"] == [] and "docker-image-drift" in res["skipped_not_local"]

    # 切回 local 后不重放回滚期间 Hermes 已经发过的产物
    jobs_json.write_text(json.dumps({"jobs": [{"id": "j1", "deliver": "local"}, {"id": "j2", "deliver": "local"}]}))
    res = harvest_cron_outputs(sandbox_db, output_root=root, jobs_json=jobs_json, jobs=table)
    assert res["pushed"] == []
