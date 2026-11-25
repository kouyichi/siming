"""Tower 运维与调度面板（方案 A）端到端、安全边界与真实探针回归测试。

覆盖：
1. infra_healer.read_local_resource_snapshot() 真实读取本机 RAM/根分区（非写死假值）及 disk81_used_pct=None 诚实降级；
2. 三合一调度总表（systemd user timers + Hermes cron jobs + Siming waits）：直推 QQ / 司命收割 / 失败/超时识别；
3. 只读输出/日志预览安全护栏（禁止路径穿越、禁止读取非 timer 关联 unit 日志、强制脱敏）；
4. 司命自运维动作（push_guard 一键恢复、worker 暂停/恢复、只读链路体检）与审计留痕；
5. Tower 全局 POST CSRF 强校验（涵盖新 /api/ops/* 与旧 /api/routines、/api/modules/*/toggle）；
6. 前端 HTML/JS 完整元素、零 XSS 注入槽位与 `node --check` 语法校验。
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from typing import Any, Iterator

import pytest

from siming.control_tower import create_tower_server, render_tower_html
from siming.db import SimingDB, iso_now, now_local
from siming.infra_healer import format_infra_report, read_local_resource_snapshot, run_infra_patrol
from siming.tower_ops import (
    build_ops_tower_payload,
    collect_external_tools,
    ops_restore_push_rule,
    ops_set_pause,
    read_schedule_output_preview,
)


@contextmanager
def _tower(db: SimingDB, token: str = "test-ops-tower-secret-2026") -> Iterator[tuple[str, dict[str, str]]]:
    srv = create_tower_server(db, host="127.0.0.1", port=0, token=token)
    port = srv.server_address[1]
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    try:
        yield f"http://127.0.0.1:{port}", {"X-Siming-Token": token}
    finally:
        srv.shutdown()
        srv.server_close()
        th.join(timeout=2)


def _req(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    raw_body: bytes | None = None,
) -> tuple[int, dict[str, Any]]:
    hdrs = dict(headers or {})
    data = raw_body
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(base_url + path, data=data, headers=hdrs, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=8) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_infra_healer_real_local_resource_snapshot_and_honest_disk81(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-23T22:00:00+08:00")
    snap = read_local_resource_snapshot()
    assert snap["mem_total_gb"] is not None and snap["mem_total_gb"] > 0
    assert snap["mem_used_pct"] is not None and 0.0 <= snap["mem_used_pct"] <= 100.0
    assert snap["disk_root_total_gb"] is not None and snap["disk_root_total_gb"] > 0
    assert snap["disk_root_used_pct"] is not None and 0.0 <= snap["disk_root_used_pct"] <= 100.0
    # 未远程探测时必须诚实返回 None，而非假占位 50.0
    assert snap["disk81_used_pct"] is None

    rep = run_infra_patrol(sandbox_db, auto_heal=False)
    assert rep["ok"] is True
    assert rep["resources"]["disk81_used_pct"] is None
    txt = format_infra_report(rep)
    assert "未探测" in txt


def test_ops_unified_schedules_systemd_hermes_waits_and_log_preview_security(
    sandbox_db: SimingDB, frozen_clock, tmp_path: Path
) -> None:
    frozen_clock("2026-11-23T22:00:00+08:00")
    ts = iso_now()

    # 1. Seed DB state: waits (WAITING + PROPOSED), effects (FAILED), outbox (SENT + THROTTLED), rule_stats (QUIET + cooling)
    future_cd = iso_now(now_local() + timedelta(hours=2))
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO waits (wait_id, status, strength, trigger_kind, trigger_expr, next_fire_at, expires_at, action_mode, quote, mandate_json, created_at, updated_at)
            VALUES
              ('W10', 'WAITING', 'explicit', 'time', '2026-11-24 09:00', '2026-11-24T09:00:00+08:00', '2026-12-10T00:00:00+08:00', 'remind', '提醒核对账单', '{"task":"提醒核对账单"}', ?, ?),
              ('W11', 'PROPOSED', 'inferred', 'time', '2026-11-24 20:00', '2026-11-24T20:00:00+08:00', '2026-11-26T22:00:00+08:00', 'remind', '顺手看看快递', '{"task":"顺手看看快递"}', ?, ?)
            """,
            (ts, ts, ts, ts),
        )
        conn.execute(
            """
            INSERT INTO effects (effect_id, kind, payload_json, status, retry_count, next_run_at, last_error, created_at, completed_at)
            VALUES ('EF-FAIL-1', 'send_qq', '{}', 'FAILED', 2, ?, 'gateway timeout bearer sk-ant-secret1234567890', ?, ?)
            """,
            (ts, ts, ts),
        )
        conn.execute(
            """
            INSERT INTO rule_stats (rule_id, mode, hard_rule, sent_14d, acted_14d, dismissed_14d, cooldown_until, updated_at)
            VALUES
              ('sub_renew', 'QUIET', 0, 140, 1, 0, NULL, ?),
              ('noisy_rule', 'ACTIVE', 0, 12, 0, 0, ?, ?)
            """,
            (ts, future_cd, ts),
        )
        conn.execute(
            """
            INSERT INTO outbox (id, ref_id, rule_id, text, send_at, status, channel, dedup_hash, created_at)
            VALUES
              ('OB-1', 'r1', 'morning_brief', '早安简报', ?, 'SENT', 'qqbot', 'h1', ?),
              ('OB-2', 'r2', 'sub_renew', '订阅续费测试', ?, 'QUIETED', 'qqbot', 'h2', ?),
              ('OB-3', 'r3', 'noisy_rule', '刷屏消息', ?, 'THROTTLED', 'qqbot', 'h3', ?)
            """,
            (ts, ts, ts, ts, ts, ts),
        )

    # 2. Fake systemd JSON + show blocks (1 ok siming-tick, 1 timeout host timer)
    fake_timers_json = json.dumps(
        [
            {
                "next": 1795442460000000,
                "last": 1795442340000000,
                "unit": "siming-tick.timer",
                "activates": "siming-tick.service",
            },
            {
                "next": 1795446000000000,
                "last": 1795442400000000,
                "unit": "buy-track-sync.timer",
                "activates": "buy-track-sync.service",
            },
        ]
    )
    fake_show_out = (
        "Id=siming-tick.service\n"
        "Description=司命主节拍\n"
        "ActiveState=inactive\n"
        "SubState=dead\n"
        "Result=success\n"
        "ExecMainStatus=0\n"
        "ExecMainStartTimestampMonotonic=10000000\n"
        "ExecMainExitTimestampMonotonic=11250000\n"
        "\n"
        "Id=buy-track-sync.service\n"
        "Description=家电装修同步\n"
        "ActiveState=failed\n"
        "SubState=failed\n"
        "Result=timeout\n"
        "ExecMainStatus=15\n"
        "ExecMainStartTimestampMonotonic=20000000\n"
        "ExecMainExitTimestampMonotonic=50000000\n"
    )

    def _fake_cmd(cmd: list[str], timeout: float) -> tuple[int, str, str]:
        if "list-timers" in cmd:
            return 0, fake_timers_json, ""
        if "show" in cmd:
            return 0, fake_show_out, ""
        if cmd[0] == "journalctl":
            return 0, "2026-11-23T21:58:00+08:00 omarchy siming: ok token=sk-1234567890abcdef1234567890", ""
        return 1, "", "unexpected cmd"

    # 3. Synthetic Hermes jobs.json + output dir
    hermes_dir = tmp_path / "hermes_cron"
    out_root = hermes_dir / "output"
    (out_root / "b4824fcdd178").mkdir(parents=True, exist_ok=True)
    (out_root / "b4824fcdd178" / "2026-11-23_22-00-00.md").write_text(
        "**Status:** ok\n---\n## Response\nPR #418 已通过 CI，授权头 Bearer secret_token_abcdef123456 应脱敏。\n",
        encoding="utf-8",
    )
    jobs_file = hermes_dir / "jobs.json"
    jobs_file.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": "b4824fcdd178",
                        "name": "digest-my-prs",
                        "script": "pr_digest.py",
                        "schedule": {"kind": "cron", "expr": "0 12,22 * * *", "display": "0 12,22 * * *"},
                        "enabled": True,
                        "deliver": "local",
                        "last_status": "ok",
                        "last_run_at": "2026-11-23T22:00:00+08:00",
                        "next_run_at": "2026-11-24T12:00:00+08:00",
                        "failure_streak": 0,
                    },
                    {
                        "id": "0d9f9a2e5d8d",
                        "name": "x-watch-activity",
                        "script": "x_watch.py",
                        "schedule": {"kind": "interval", "minutes": 5, "display": "every 5m"},
                        "enabled": True,
                        "deliver": "qqbot:00000000000000000000000000000000",
                        "last_status": "error",
                        "last_error": "HTTP 429 rate limit",
                        "failure_streak": 2,
                    },
                    {
                        "id": "deadbeef1234",
                        "name": "old-disabled-job",
                        "schedule": {"display": "0 8 * * *"},
                        "enabled": False,
                        "deliver": "qqbot:123",
                        "last_status": "ok",
                    },
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    payload = build_ops_tower_payload(
        sandbox_db,
        live=True,
        cmd_runner=_fake_cmd,
        hermes_jobs_json=jobs_file,
        hermes_output_root=out_root,
        cdp_check_fn=lambda: {"main_chromium_up": True, "atrust_collision_suspect": False},
        tcp_probe_fn=lambda host, port: port in (7890, 8090, 3001),
    )
    assert payload["ok"] is True
    s = payload["summary"]
    assert s["overall_status"] == "WARN"  # 非司命本机 timer 超时标 WARN，不误拉红警
    assert s["timers_total"] == 2
    assert s["timers_failed"] == 1
    assert s["crons_total"] == 3
    assert s["crons_enabled"] == 2
    assert s["crons_failed"] == 1
    assert s["waits_total"] >= 2
    assert s["schedules_total"] == s["timers_total"] + s["crons_total"] + s["waits_total"]
    wait_ids = {x["raw_id"] for x in payload["schedules"]["items"] if x["source"] == "siming_wait"}
    assert {"W10", "W11"}.issubset(wait_ids)
    assert s["direct_qq_count"] == 1  # only enabled x-watch-activity, not disabled job
    assert s["siming_harvest_count"] == 1  # digest-my-prs
    assert s["push_guard_quiet"] == 1
    assert s["push_guard_cooling"] == 1
    assert s["outbox_sent_24h"] == 1
    assert s["outbox_held_24h"] == 2
    assert s["effects_failed"] == 1

    # Check siming core timer duration parsed accurately (1.25s)
    assert len(payload["siming_core_timers"]) == 1
    assert payload["siming_core_timers"][0]["name"] == "siming-tick.timer"
    assert payload["siming_core_timers"][0]["duration_sec"] == 1.25

    # Check effects error redacted
    eff_err = payload["siming_health"]["effects"]["recent_issues"][0]["last_error"]
    assert "sk-ant-secret1234567890" not in eff_err

    # Check external tools reflect tcp_probe_fn
    tools_by_id = {t["id"]: t for t in payload["external_tools"]}
    assert tools_by_id["beszel"]["reachable"] is True
    assert tools_by_id["uptime_kuma"]["reachable"] is True
    assert tools_by_id["backrest"]["reachable"] is False

    # 4. Read-only log preview security checks
    # 4a. Hermes valid job -> returns parsed + redacted preview
    prev_h = read_schedule_output_preview(
        sandbox_db, "hermes", "b4824fcdd178", jobs_json=jobs_file, output_root=out_root
    )
    assert prev_h["ok"] is True
    assert "PR #418" in prev_h["preview"]
    assert "secret_token_abcdef123456" not in prev_h["preview"]

    # 4b. Hermes traversal / unknown job blocked
    assert read_schedule_output_preview(sandbox_db, "hermes", "../etc", jobs_json=jobs_file, output_root=out_root)["ok"] is False
    assert read_schedule_output_preview(sandbox_db, "hermes", "unknown9999", jobs_json=jobs_file, output_root=out_root)["ok"] is False

    # 4c. Systemd allowed timer-activated service -> ok + redacted
    prev_s = read_schedule_output_preview(sandbox_db, "systemd", "siming-tick.service", cmd_runner=_fake_cmd, live=True)
    assert prev_s["ok"] is True
    assert "sk-1234567890abcdef1234567890" not in prev_s["preview"]

    # 4d. Systemd arbitrary non-timer service / flag injection blocked!
    assert read_schedule_output_preview(sandbox_db, "systemd", "sshd.service", cmd_runner=_fake_cmd, live=True)["ok"] is False
    assert read_schedule_output_preview(sandbox_db, "systemd", "-u.service", cmd_runner=_fake_cmd, live=True)["ok"] is False


def test_tower_ops_endpoints_auth_global_csrf_and_actions(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-23T22:10:00+08:00")
    ts = iso_now()
    with sandbox_db.transaction() as conn:
        conn.execute(
            "INSERT INTO rule_stats (rule_id, mode, hard_rule, sent_14d, acted_14d, updated_at) VALUES ('sub_renew', 'QUIET', 0, 50, 0, ?)",
            (ts,),
        )

    with _tower(sandbox_db) as (base_url, hdrs):
        # 1. All new endpoints enforce 401 without token
        for method, path in (
            ("GET", "/api/ops"),
            ("GET", "/api/ops/schedule-output?source=hermes&id=abcd1234"),
            ("POST", "/api/ops/push-guard/restore"),
            ("POST", "/api/ops/pause"),
            ("POST", "/api/ops/diagnose"),
        ):
            with pytest.raises(urllib.error.HTTPError) as exc:
                _req(base_url, path, method=method, payload={"rule_id": "sub_renew"} if method == "POST" else None)
            assert exc.value.code == 401

        # 2. Global CSRF enforcement on BOTH new /api/ops/* AND legacy POST endpoints (/api/routines, /api/modules/*/toggle)
        for path in ("/api/ops/pause", "/api/routines", "/api/modules/price_watch/toggle"):
            with pytest.raises(urllib.error.HTTPError) as exc_ct:
                _req(base_url, path, method="POST", headers={**hdrs, "Content-Type": "text/plain"}, raw_body=b"{}")
            assert exc_ct.value.code == 415

            with pytest.raises(urllib.error.HTTPError) as exc_org:
                _req(
                    base_url,
                    path,
                    method="POST",
                    headers={**hdrs, "Origin": "https://evil.example.com"},
                    payload={"action": "resume", "content": "x", "enabled": True},
                )
            assert exc_org.value.code == 403

        # 3. GET /api/ops & Caddy prefix /siming/tower/api/ops
        st, ops = _req(base_url, "/siming/tower/api/ops", headers=hdrs)
        assert st == 200 and ops["ok"] is True
        assert ops["push_guard"]["quiet_count"] == 1
        assert len(ops["external_tools"]) >= 4

        # 4. POST /api/ops/push-guard/restore
        st_r, res_r = _req(
            base_url,
            "/api/ops/push-guard/restore",
            method="POST",
            headers=hdrs,
            payload={"rule_id": "sub_renew"},
        )
        assert st_r == 200 and res_r["ok"] is True and res_r["mode"] == "ACTIVE"

        _, ops2 = _req(base_url, "/api/ops", headers=hdrs)
        assert ops2["push_guard"]["quiet_count"] == 0

        # 5. POST /api/ops/pause -> pause 2h -> reject 999h -> resume
        st_p, res_p = _req(
            base_url,
            "/api/ops/pause",
            method="POST",
            headers=hdrs,
            payload={"action": "pause", "duration": "2h"},
        )
        assert st_p == 200 and res_p["ok"] is True and res_p["paused"] is True
        assert sandbox_db.get_meta("paused_until") == res_p["paused_until"]

        with pytest.raises(urllib.error.HTTPError) as exc_dur:
            _req(
                base_url,
                "/api/ops/pause",
                method="POST",
                headers=hdrs,
                payload={"action": "pause", "duration": "999h"},
            )
        assert exc_dur.value.code == 400

        st_u, res_u = _req(
            base_url,
            "/api/ops/pause",
            method="POST",
            headers=hdrs,
            payload={"action": "resume"},
        )
        assert st_u == 200 and res_u["ok"] is True and res_u["paused"] is False
        assert sandbox_db.get_meta("paused_until") == ""

        # 6. POST /api/ops/diagnose
        st_d, res_d = _req(
            base_url,
            "/api/ops/diagnose",
            method="POST",
            headers=hdrs,
            payload={"allow_heal": False},
        )
        assert st_d == 200 and res_d["ok"] is True
        assert isinstance(res_d["doctor_checks"], list) and len(res_d["doctor_checks"]) >= 3
        assert "reply" in res_d

        # 7. Audit table logged all ops actions
        conn = sandbox_db.connect()
        try:
            actions = {r["action"] for r in conn.execute("SELECT DISTINCT action FROM audit").fetchall()}
            for expected in (
                "tower_ops_push_guard_restore",
                "tower_ops_pause",
                "tower_ops_resume",
                "tower_ops_diagnose",
            ):
                assert expected in actions
        finally:
            conn.close()


def test_tower_html_ops_panel_valid_js_and_zero_xss(tmp_path: Path) -> None:
    html = render_tower_html()
    assert 'data-tab="ops"' in html
    assert 'id="tc-ops"' in html
    assert 'id="panel-ops"' in html
    assert 'id="ops-ext-tools"' in html
    assert 'id="ops-core-timers"' in html
    assert 'id="ops-push-guard"' in html
    assert 'id="ops-schedules-wrap"' in html
    assert 'id="ops-preview-card"' in html
    assert "/api/ops" in html
    assert "/api/ops/push-guard/restore" in html
    assert "/api/ops/pause" in html
    assert "/api/ops/diagnose" in html
    assert "/api/ops/schedule-output" in html

    # Zero XSS inline handler interpolation sinks
    inline_breakout = re.findall(r"""on(?:click|change)="[^"]*'?\$\{esc\([^}]+\)\}'?[^"]*" """, html)
    inline_breakout2 = re.findall(r"""on(?:click|change)="[^"]*'\$\{esc\(""", html)
    assert not inline_breakout, f"Unsafe inline handler: {inline_breakout}"
    assert not inline_breakout2, f"Unsafe inline handler breakout: {inline_breakout2}"

    # Validate inline JS syntax via node --check
    m = re.search(r"<script>([\s\S]*?)</script>", html)
    assert m is not None
    js_file = tmp_path / "tower_ops_check.js"
    js_file.write_text(m.group(1), encoding="utf-8")
    proc = subprocess.run(["node", "--check", str(js_file)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr
