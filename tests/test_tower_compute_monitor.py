"""Tower 算力资源监控端到端与安全回归测试（AI 配额/余额池 + 流量 + 账本 + 本机硬件 + 熔断控制）。"""

from __future__ import annotations

import json
import re
import sqlite3
import subprocess
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from siming.compute_monitor import (
    build_compute_monitor_payload,
    collect_host_resources,
    collect_magpie_usage_stats,
    collect_omniroute_usage_stats,
    reset_compute_cache_for_tests,
    set_provider_circuit,
    sync_live_compute_assets,
)
from siming.control_tower import create_tower_server, render_tower_html
from siming.db import SimingDB, iso_now
from siming.multi_runtime import select_runtime
from siming.social_boardgames import upsert_friend_profile


@contextmanager
def _tower(db: SimingDB, token: str = "test-compute-tower-secret") -> Iterator[tuple[str, dict[str, str]]]:
    reset_compute_cache_for_tests()
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


def test_tower_html_includes_compute_monitor_panel_valid_js_and_zero_xss(tmp_path: Path) -> None:
    html = render_tower_html()
    assert 'data-tab="compute"' in html
    assert 'id="tc-compute"' in html
    assert 'id="panel-compute"' in html
    assert 'id="compute-kpis"' in html
    assert 'id="compute-pools"' in html
    assert 'id="compute-hardware"' in html
    assert 'id="compute-hourly-svg"' in html
    assert 'id="compute-runtimes"' in html
    assert "/api/compute" in html
    assert "/api/compute/refresh" in html
    assert "/api/compute/assets" in html
    assert "/api/compute/circuit/" in html
    assert "/api/compute/guardrails" in html
    assert "JSON.stringify(cm" not in html

    # ZERO XSS inline handler interpolation sinks
    inline_breakout = re.findall(r"""on(?:click|change)="[^"]*'?\$\{esc\([^}]+\)\}'?[^"]*" """, html)
    inline_breakout2 = re.findall(r"""on(?:click|change)="[^"]*'\$\{esc\(""", html)
    assert not inline_breakout, f"Unsafe inline handler: {inline_breakout}"
    assert not inline_breakout2, f"Unsafe inline handler breakout: {inline_breakout2}"
    assert 'data-provider="${esc(p.provider)}"' in html
    assert "余量 0% · 已耗尽" in html
    assert "prog-track exhausted" in html

    # Validate inline JS syntax via node --check
    m = re.search(r"<script>([\s\S]*?)</script>", html)
    assert m is not None
    js_file = tmp_path / "tower_compute_check.js"
    js_file.write_text(m.group(1), encoding="utf-8")
    proc = subprocess.run(["node", "--check", str(js_file)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_compute_monitor_merge_sync_usage_telemetry_and_currency_isolation(
    sandbox_db: SimingDB, frozen_clock, tmp_path: Path
) -> None:
    frozen_clock("2026-11-23T20:00:00+08:00")
    ts = iso_now()

    # Seed ledger AI compute txns (CNY + USD + VOID + transfer)
    with sandbox_db.transaction() as conn:
        for row in (
            ("TX-DS1", "2026-11-10T10:00:00+08:00", 500000, "CNY", "建行", "1004", "消费", "POSTED", "杭州深度求索 DeepSeek-API", ""),
            ("TX-NS1", "2026-11-12T10:00:00+08:00", 200000, "USD", "光大", "1001", "消费", "POSTED", "NOUS RESEARCH INC.", ""),
            ("TX-VOID", "2026-11-13T10:00:00+08:00", 999900, "CNY", "建行", "1004", "消费", "VOID_DUPLICATE", "DeepSeek-API", ""),
            ("TX-TR", "2026-11-14T10:00:00+08:00", 100000, "USD", "光大", "1001", "划转", "POSTED", "NOUS RESEARCH INC.", ""),
        ):
            conn.execute(
                """
                INSERT INTO ledger_txns (
                    txn_id, occurred_at, amount_cents, currency, account_alias, card_last4,
                    kind, status, merchant, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*row, ts, ts),
            )
        conn.execute(
            """
            INSERT INTO overnight_handoffs (
                handoff_id, name, host, pane_or_pid, log_path, status,
                whitelist_json, metrics_json, report_md, created_at, updated_at
            ) VALUES ('OH01', 'indie_game-sft', 'gpu-server', 'gpu:p1', '', 'WATCHING', '["checkpoint_save"]', '{"step":1200,"loss":1.42}', '', ?, ?)
            """,
            (ts, ts),
        )

    # Mock Magpie usage.jsonl
    usage_jsonl = tmp_path / "usage.jsonl"
    usage_jsonl.write_text(
        "\n".join(
            [
                json.dumps({"t": "2026-11-23T19:10:00+08:00", "provider": "nous", "model": "anthropic/claude-opus-5.5", "in": 1000, "out": 500, "cache_read": 2000, "ms": 3200, "status": 200}),
                json.dumps({"t": "2026-11-23T19:30:00+08:00", "provider": "codex", "model": "gpt-6.2-astra", "in": 4000, "out": 800, "cache_read": 0, "ms": 2100, "status": 200}),
                json.dumps({"t": "2026-11-23T19:45:00+08:00", "provider": "nous", "model": "anthropic/claude-opus-5.5", "in": 100, "out": 0, "ms": 500, "status": 429}),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    # Mock OmniRoute storage.sqlite
    omni_db = tmp_path / "omni.sqlite"
    oconn = sqlite3.connect(omni_db)
    oconn.execute(
        """
        CREATE TABLE usage_history (
            id INTEGER PRIMARY KEY, provider TEXT, model TEXT,
            tokens_input INTEGER, tokens_output INTEGER, success INTEGER, latency_ms REAL, timestamp TEXT
        )
        """
    )
    oconn.execute(
        "INSERT INTO usage_history VALUES (1, 'nvidia', 'meta/muse', 500, 300, 1, 1200.0, '2026-11-23T11:30:00Z')"
    )
    oconn.commit()
    oconn.close()

    mag_mock = lambda: {
        "ok": True,
        "running": True,
        "status_code": 200,
        "mode": "live_http",
        "items": [
            {
                "pool_id": "magpie:deepseek",
                "provider": "deepseek",
                "display_name": "DeepSeek",
                "account_label": "default",
                "kind": "balance",
                "plan": "",
                "source": "magpie",
                "balance": 1173.15,
                "currency": "CNY",
                "balance_display": "¥1,173.15",
                "windows": [],
                "reset_credits": None,
                "extra_notes": [],
                "updated_at": ts,
                "stale": False,
                "error": "",
            },
            {
                "pool_id": "magpie:codex:devuser",
                "provider": "codex",
                "display_name": "Codex",
                "account_label": "user@example.com",
                "kind": "hybrid",
                "plan": "pro",
                "source": "magpie",
                "balance": 36675.74,
                "currency": "CREDITS",
                "balance_display": "36675.74 credits",
                "windows": [{"name": "7 days", "used_pct": 0.0, "remaining_pct": 100.0, "display": "0%", "resets_at": "2026-11-30T20:00:00+08:00", "detail": "", "severity": "ok"}],
                "reset_credits": {"count": 3, "next_expires_at": "2026-11-30T20:22:46Z"},
                "extra_notes": [],
                "updated_at": ts,
                "stale": False,
                "error": "",
            },
        ],
        "error": "",
    }
    ub_mock = lambda: {
        "ok": True,
        "mode": "live_cli",
        "items": [
            {
                "pool_id": "usagebar:nous",
                "provider": "nous",
                "display_name": "Nous Research",
                "account_label": "default",
                "kind": "balance",
                "plan": "Nous Research",
                "source": "ai-usagebar",
                "balance": 95.08,
                "currency": "USD",
                "balance_display": "$95.08",
                "windows": [],
                "reset_credits": None,
                "extra_notes": ["Renews: 29d 0h"],
                "updated_at": ts,
                "stale": False,
                "error": "",
            },
            {
                "pool_id": "usagebar:antigravity",
                "provider": "antigravity",
                "display_name": "Antigravity",
                "account_label": "default",
                "kind": "subscription",
                "plan": "Antigravity",
                "source": "ai-usagebar",
                "balance": None,
                "currency": "",
                "balance_display": "",
                "windows": [
                    {"name": "Weekly · Claude & GPT OSS", "used_pct": 100.0, "remaining_pct": 0.0, "display": "100%", "resets_at": "2026-11-25T07:09:24Z", "detail": "Resets in 1d 18h", "severity": "critical"},
                ],
                "reset_credits": None,
                "extra_notes": [],
                "updated_at": ts,
                "stale": False,
                "error": "",
            },
        ],
        "error": "",
    }
    aq_mock = lambda: {
        "ok": True,
        "running": True,
        "status_code": 200,
        "codex_available": True,
        "codex_remaining": 100.0,
        "claude_available": False,
        "claude_remaining": None,
        "claude_label": "未暴露",
        "items": [],
    }

    payload = build_compute_monitor_payload(
        sandbox_db,
        live=True,
        sync_assets=True,
        magpie_collector=mag_mock,
        usagebar_collector=ub_mock,
        ai_quota_collector=aq_mock,
        magpie_usage_file=usage_jsonl,
        omniroute_db_path=omni_db,
    )
    assert payload["ok"] is True
    # CNY and USD balances strictly separated (Codex credits must NOT pollute CNY/USD!)
    assert payload["summary"]["total_balance_cny"] == 1173.15
    assert payload["summary"]["total_balance_usd"] == 95.08
    assert payload["summary"]["pool_count"] >= 5  # deepseek, codex, nous, antigravity, claude
    assert payload["summary"]["critical_count"] >= 1  # Antigravity 100%

    # Synced DeepSeek (CNY) + Nous (USD) into compute_assets DB table (not credits-only Codex)
    assert payload["sync_result"]["synced"] == 2
    assert set(payload["sync_result"]["providers"]) == {"deepseek", "nous"}
    assert len(payload["compute_assets"]) == 2

    # Usage telemetry: 3 Magpie + 1 OmniRoute = 4 reqs in 24h
    assert payload["usage_telemetry"]["magpie"]["requests_24h"] == 3
    assert payload["usage_telemetry"]["omniroute"]["requests_24h"] == 1
    assert payload["summary"]["requests_24h"] == 4

    # Ledger AI spend strictly isolates CNY (5000) vs USD (2000) and drops VOID & transfer
    assert payload["ledger_spend"]["current_month_cny"] == 5000.0
    assert payload["ledger_spend"]["current_month_usd"] == 2000.0

    # Remote handoff visible
    assert len(payload["remote_jobs"]["overnight_handoffs"]) == 1
    assert payload["remote_jobs"]["overnight_handoffs"][0]["handoff_id"] == "OH01"

    # Re-sync preserves user-customized billing_scope & manual circuit state
    set_provider_circuit(sandbox_db, "deepseek", True)
    sync_live_compute_assets(sandbox_db, pools=payload["pools"], allow_live=True)
    conn = sandbox_db.connect()
    try:
        ds_row = conn.execute("SELECT status, billing_scope FROM compute_assets WHERE provider='deepseek'").fetchone()
        assert ds_row["status"] == "CIRCUIT_OPEN"
        assert ds_row["billing_scope"] == "work_cw08"
    finally:
        conn.close()


def test_host_resources_collector_returns_valid_telemetry() -> None:
    host = collect_host_resources()
    assert host["hostname"]
    assert host["cpu"]["cores_logical"] >= 1
    assert host["memory"]["total_gib"] > 0
    assert 0.0 <= host["memory"]["used_pct"] <= 100.0
    assert any(d["mount"] == "/" for d in host["disks"])


def test_tower_compute_http_endpoints_auth_csrf_crud_circuit_and_privacy(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    frozen_clock("2026-11-23T20:00:00+08:00")
    secret_note = "SECRET-COMPUTE-FRIEND-PRIVACY-CHECK-999"
    upsert_friend_profile(sandbox_db, name="老王", notes=secret_note)

    with _tower(sandbox_db) as (base_url, hdrs):
        # 1. Auth required (401 without token)
        for method, path in (
            ("GET", "/api/compute"),
            ("POST", "/api/compute/refresh"),
            ("POST", "/api/compute/assets"),
            ("POST", "/api/compute/guardrails"),
            ("POST", "/api/compute/circuit/deepseek"),
        ):
            with pytest.raises(urllib.error.HTTPError) as exc:
                _req(base_url, path, method=method, payload={} if method == "POST" else None)
            assert exc.value.code == 401

        # 2. CSRF checks on POST endpoints
        with pytest.raises(urllib.error.HTTPError) as e_ct:
            _req(base_url, "/api/compute/assets", method="POST", headers={**hdrs, "Content-Type": "text/plain"}, raw_body=b"{}")
        assert e_ct.value.code == 415

        with pytest.raises(urllib.error.HTTPError) as e_org:
            _req(
                base_url,
                "/api/compute/circuit/deepseek",
                method="POST",
                headers={**hdrs, "Origin": "https://evil.example.com"},
                payload={"open": True},
            )
        assert e_org.value.code == 403

        # 3. GET /api/compute (also verify Caddy /siming/tower/ prefix & zero friend privacy leak)
        st, cm0 = _req(base_url, "/siming/tower/api/compute", headers=hdrs)
        assert st == 200 and cm0["ok"] is True
        assert "host" in cm0 and "pools" in cm0 and "runtimes" in cm0
        assert secret_note not in json.dumps(cm0, ensure_ascii=False)

        # 4. POST /api/compute/assets validation + valid upsert
        with pytest.raises(urllib.error.HTTPError) as e_bad:
            _req(base_url, "/api/compute/assets", method="POST", headers=hdrs, payload={"provider": "aliyun", "balance": "nan_str"})
        assert e_bad.value.code == 400

        st, up = _req(
            base_url,
            "/api/compute/assets",
            method="POST",
            headers=hdrs,
            payload={
                "provider": "aliyun",
                "account_label": "coding",
                "balance": 150.0,
                "currency": "CNY",
                "daily_spend": 12.0,
                "avg_daily_spend": 10.0,
                "billing_scope": "personal",
                "plan_name": "百炼套餐",
                "plan_expires_at": "2026-11-29",
            },
        )
        assert st == 200 and up["ok"] is True and up["provider"] == "aliyun"
        assert len(up["guardrails"]["low_balance"]) == 1
        assert len(up["guardrails"]["expiring"]) == 1

        # 5. POST /api/compute/circuit/deepseek -> trips circuit AND updates multi_runtime routing!
        with pytest.raises(urllib.error.HTTPError) as e_cir_bad:
            _req(base_url, "/api/compute/circuit/deepseek", method="POST", headers=hdrs, payload={"open": "maybe"})
        assert e_cir_bad.value.code == 400

        st, c_open = _req(base_url, "/api/compute/circuit/deepseek", method="POST", headers=hdrs, payload={"open": True})
        assert st == 200 and c_open["ok"] is True and c_open["circuit_open"] is True
        sel_after_open = select_runtime(sandbox_db, task_kind="triage", complexity="low")
        assert sel_after_open["runtime"] != "pi_deepseek"

        st, c_close = _req(base_url, "/api/compute/circuit/deepseek", method="POST", headers=hdrs, payload={"open": False})
        assert st == 200 and c_close["ok"] is True and c_close["circuit_open"] is False
        sel_after_close = select_runtime(sandbox_db, task_kind="triage", complexity="low")
        assert sel_after_close["runtime"] == "pi_deepseek"

        # 6. POST /api/compute/guardrails & POST /api/compute/refresh
        st, gr = _req(base_url, "/api/compute/guardrails", method="POST", headers=hdrs, payload={})
        assert st == 200 and gr["ok"] is True and gr["checked"] == 1

        st, rf = _req(base_url, "/api/compute/refresh", method="POST", headers=hdrs, payload={})
        assert st == 200 and rf["ok"] is True

        # 7. Audit records written
        conn = sandbox_db.connect()
        try:
            acts = {r["action"] for r in conn.execute("SELECT DISTINCT action FROM audit").fetchall()}
            assert {
                "tower_compute_asset_upsert",
                "tower_compute_circuit_toggle",
                "tower_compute_guardrails",
                "tower_compute_refresh",
            }.issubset(acts)
        finally:
            conn.close()


def test_compute_monitor_local_tz_fresh_merge_and_guardrail_updated_at(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    from siming.compute_assets import evaluate_compute_guardrails, upsert_compute_asset
    from siming.compute_monitor import _merge_pools, _normalize_magpie_item

    frozen_clock("2026-11-23T22:00:00+08:00")
    mag_item = _normalize_magpie_item(
        {
            "provider": "factory",
            "name": "Factory",
            "kind": "subscription",
            "user": "u1",
            "windows": [
                {"name": "7 days", "used": 42, "remaining": 58, "resetsAt": "2026-11-24T05:53:22.68Z"},
            ],
            "resets": {"count": 3, "until": "2026-11-30T20:22:46.166148Z"},
            "readAt": "2026-11-23T22:00:00+08:00",
        }
    )
    # UTC -> +08:00 conversion
    assert mag_item["windows"][0]["resets_at_local"] == "2026-11-24 13:53"
    assert "重置 2026-11-24 13:53" in mag_item["windows"][0]["detail"]
    assert mag_item["reset_credits"]["next_expires_local"] == "2026-12-01 04:22"

    # Fresher usagebar balance overrides older magpie balance, and mismatched Breakdown note is dropped
    merged = _merge_pools(
        magpie_items=[
            {
                "pool_id": "magpie:deepseek",
                "provider": "deepseek",
                "display_name": "DeepSeek",
                "account_label": "default",
                "kind": "balance",
                "plan": "",
                "source": "magpie",
                "balance": 1146.97,
                "currency": "CNY",
                "balance_display": "¥1146.97",
                "windows": [],
                "reset_credits": None,
                "extra_notes": [],
                "updated_at": "2026-11-23T22:10:00+08:00",
                "stale": False,
                "error": "",
            }
        ],
        usagebar_items=[
            {
                "pool_id": "usagebar:deepseek",
                "provider": "deepseek",
                "display_name": "DeepSeek",
                "account_label": "default",
                "kind": "balance",
                "plan": "DeepSeek",
                "source": "ai-usagebar",
                "balance": 1146.54,
                "currency": "CNY",
                "balance_display": "¥1146.54",
                "windows": [],
                "reset_credits": None,
                "extra_notes": ["Breakdown: granted ¥0.00 · topped-up ¥1146.54"],
                "updated_at": "2026-11-23T14:14:00Z",  # 22:14+08:00 (fresher!)
                "stale": False,
                "error": "",
            }
        ],
        ai_quota={"ok": False},
        db_assets=[],
        open_circuits=set(),
        nous_sentinel={},
    )
    ds = next(p for p in merged if p["provider"] == "deepseek")
    assert ds["balance"] == 1146.54
    assert ds["balance_display"] == "¥1146.54"

    # When magpie is newer than usagebar, conflicting Breakdown note must NOT be appended
    merged2 = _merge_pools(
        magpie_items=[
            {
                "pool_id": "magpie:deepseek",
                "provider": "deepseek",
                "display_name": "DeepSeek",
                "account_label": "default",
                "kind": "balance",
                "plan": "",
                "source": "magpie",
                "balance": 1146.50,
                "currency": "CNY",
                "balance_display": "¥1146.50",
                "windows": [],
                "reset_credits": None,
                "extra_notes": [],
                "updated_at": "2026-11-23T22:15:00+08:00",
                "stale": False,
                "error": "",
            }
        ],
        usagebar_items=[
            {
                "pool_id": "usagebar:deepseek",
                "provider": "deepseek",
                "display_name": "DeepSeek",
                "account_label": "default",
                "kind": "balance",
                "plan": "DeepSeek",
                "source": "ai-usagebar",
                "balance": 1147.11,
                "currency": "CNY",
                "balance_display": "¥1147.11",
                "windows": [],
                "reset_credits": None,
                "extra_notes": ["Breakdown: granted ¥0.00 · topped-up ¥1147.11"],
                "updated_at": "2026-11-23T14:11:00Z",  # older
                "stale": False,
                "error": "",
            }
        ],
        ai_quota={"ok": False},
        db_assets=[],
        open_circuits=set(),
        nous_sentinel={},
    )
    ds2 = next(p for p in merged2 if p["provider"] == "deepseek")
    assert ds2["balance"] == 1146.50
    assert not any("Breakdown:" in n for n in ds2["extra_notes"])

    # evaluate_compute_guardrails must NOT overwrite updated_at when status is unchanged
    upsert_compute_asset(sandbox_db, provider="deepseek", account_label="main", balance=1000.0, currency="CNY")
    conn = sandbox_db.connect()
    try:
        before_ts = conn.execute("SELECT updated_at FROM compute_assets WHERE provider='deepseek'").fetchone()["updated_at"]
    finally:
        conn.close()
    frozen_clock("2026-11-23T22:30:00+08:00")
    evaluate_compute_guardrails(sandbox_db, notify=False)
    conn = sandbox_db.connect()
    try:
        after_ts = conn.execute("SELECT updated_at FROM compute_assets WHERE provider='deepseek'").fetchone()["updated_at"]
    finally:
        conn.close()
    assert after_ts == before_ts


def test_compute_monitor_redpill_billing_integration_and_db_sync(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    from siming.compute_monitor import _normalize_redpill_payload

    frozen_clock("2026-11-24T01:25:00+08:00")
    url = "https://redpill.ai/orgs/example-org-000000/billing"
    norm = _normalize_redpill_payload(
        {"ok": True, "balance": {"balance_usd": "9.290269942", "can_top_up": True}, "subscription": {"subscription": None}},
        billing_url=url,
        mode="cdp",
    )
    assert norm is not None
    assert norm["provider"] == "redpill"
    assert norm["balance"] == 9.29
    assert norm["currency"] == "USD"
    assert norm["balance_display"] == "$9.29"
    assert norm["account_label"] == "example-org-000000"
    assert norm["billing_url"] == url

    payload = build_compute_monitor_payload(
        sandbox_db,
        live=True,
        sync_assets=True,
        redpill_collector=lambda: {"ok": True, "mode": "cdp_tab", "items": [norm], "error": ""},
    )
    assert payload["summary"]["total_balance_usd"] == 9.29
    assert "redpill" in payload["sync_result"]["providers"]
    rp_pool = next(p for p in payload["pools"] if p["provider"] == "redpill")
    assert rp_pool["balance_display"] == "$9.29"
    assert rp_pool["billing_url"] == url

    # Even when live probe is offline, DB fallback retains balance & billing_url
    payload_offline = build_compute_monitor_payload(
        sandbox_db,
        live=False,
        sync_assets=False,
        redpill_collector=lambda: {"ok": False, "mode": "offline", "items": [], "error": ""},
    )
    rp_fallback = next(p for p in payload_offline["pools"] if p["provider"] == "redpill")
    assert rp_fallback["balance"] == 9.29
    assert rp_fallback["billing_url"] == url
