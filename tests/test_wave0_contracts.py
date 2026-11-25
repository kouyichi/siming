"""Wave 0 (W0-4) 核心契约测试：补齐 cli、hermes-plugin、watchdog、quiet_cfo、qq_cards/router 零测试盲区。"""

from __future__ import annotations

import asyncio
import importlib.util
import io
import json
from datetime import timedelta
from pathlib import Path

from siming import cli
from siming.db import SimingDB, iso_now, now_local
from siming.qq_cards import handle_qq_button_action, infer_keyboard_for_message
from siming.quiet_cfo import match_incoming_receivable, query_ledger_natural, register_receivable
from siming.router import handle_inbound_qq
from siming.watchdog import run_watchdog


def _load_hermes_plugin():
    mod_path = Path(__file__).resolve().parents[1] / "integrations" / "hermes-plugin" / "__init__.py"
    spec = importlib.util.spec_from_file_location("siming_bridge_test_mod", mod_path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_cli_core_subcommands_and_stdin_inbound(sandbox_db: SimingDB, monkeypatch, capsys) -> None:
    """覆盖 cli.py 的 cases / done / today / doctor / inbound --stdin-json 全链路契约。"""
    monkeypatch.setattr(cli, "SimingDB", lambda: sandbox_db)

    ts = iso_now()
    with sandbox_db.transaction() as conn:
        cid = sandbox_db.next_seq_id(conn, "S")
        conn.execute(
            """
            INSERT INTO cases (case_id, title, next_step, done_rule, status, strength, category, rule_id, context_json, created_at, updated_at)
            VALUES (?, '测试外卖柜取餐', '下楼拿饭', '拿了', '待处理', 'NORMAL', 'food_pickup', 'meituan_cabinet', '{}', ?, ?)
            """,
            (cid, ts, ts),
        )

    assert cli.main(["cases"]) == 0
    assert cid in capsys.readouterr().out

    # inbound --stdin-json 接收真实 QQBot 内联引用格式
    payload = json.dumps(
        {"text": f"[Quoted message]:\n🍱 [{cid}] 测试外卖柜取餐\n\n拿了", "reply_to_id": "", "reply_to_text": "", "media": []},
        ensure_ascii=False,
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert cli.main(["inbound", "--stdin-json"]) == 0
    inb_out = json.loads(capsys.readouterr().out)
    assert inb_out["handled"] is True

    assert cli.main(["today"]) == 0
    today_out = capsys.readouterr().out
    assert any(k in today_out for k in ("司命晨札", "不限行", "工作日", "休息日"))

    assert cli.main(["doctor", "--no-edge", "--json"]) == 0
    doc_out = json.loads(capsys.readouterr().out)
    assert isinstance(doc_out, dict) and doc_out.get("ok") is True and isinstance(doc_out.get("checks"), list)


def test_hermes_plugin_inline_quote_split_and_fail_closed_admission(monkeypatch) -> None:
    """覆盖 integrations/hermes-plugin/__init__.py 内联引用拆解、只读斜杠白名单与敏感口令 fail-closed。"""
    plug = _load_hermes_plugin()

    # 1. 内联引用文本拆解
    clean, quoted = plug._split_qq_quoted_message("[Quoted message]:\n🍱 [S48] 美团外卖柜\n\n拿了")
    assert clean == "拿了"
    assert "[S48]" in quoted

    # 2. /siming 斜杠命令严格禁止写操作（apply/dispatch/pause）
    assert plug._validate_readonly_slash_args(["today", "--full"]) == ["today", "--full"]
    assert plug._validate_readonly_slash_args(["apply", "A07"]) is None
    assert plug._validate_readonly_slash_args(["done", "S01"]) is None

    # 3. 正常引用回复走 stdin JSON 传入拆分后的 text + reply_to_text
    captured: list[dict] = []

    async def _fake_cli_ok(args, timeout_sec=25.0, stdin_bytes=None):
        captured.append(json.loads((stdin_bytes or b"{}").decode("utf-8")))
        return 0, json.dumps({"handled": True, "reply": "✅ [S48] 已销账"}, ensure_ascii=False)

    monkeypatch.setattr(plug, "_run_siming_cli", _fake_cli_ok)
    res_ok = asyncio.run(
        plug._on_post_gateway_admission(
            platform="qqbot",
            text="[Quoted message]:\n🍱 [S48] 外卖柜\n\n拿了",
            message_id="m1",
        )
    )
    assert res_ok == {"action": "handled", "reply": "✅ [S48] 已销账"}
    assert captured and captured[0]["text"] == "拿了" and "[S48]" in captured[0]["reply_to_text"]

    # 4. 敏感口令 apply A07 超时/失败时必须 fail-closed 拦截，绝不 return None 透传 LLM
    async def _fake_cli_fail(args, timeout_sec=25.0, stdin_bytes=None):
        return 124, "司命命令执行超时"

    monkeypatch.setattr(plug, "_run_siming_cli", _fake_cli_fail)
    res_block = asyncio.run(
        plug._on_post_gateway_admission(
            platform="qqbot",
            text="apply A07",
            message_id="m2",
        )
    )
    assert res_block is not None and res_block["action"] == "handled"
    assert "拦截" in res_block["reply"]


def test_watchdog_recovers_dead_effect_kills_hung_and_marks_stale_source(
    sandbox_db: SimingDB, monkeypatch
) -> None:
    """覆盖 watchdog.py：识别超时卡死 worker、恢复崩溃进程 effect、标记静默过期数据源。"""
    import siming.watchdog as wd

    monkeypatch.setattr(wd, "fetch_ai_quota", lambda db: {"ok": True, "codex_remaining": 100})
    monkeypatch.setattr(wd, "check_cdp_9222_status", lambda: {"main_chromium_up": False, "atrust_collision_suspect": False})

    now = now_local()
    hung_ts = iso_now(now - timedelta(hours=2))
    recent_ts = iso_now(now - timedelta(seconds=30))
    old_src_ts = iso_now(now - timedelta(hours=5))

    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO effects (effect_id, kind, payload_json, status, retry_count, attempt_gen, worker_pid, next_run_at, started_at, created_at)
            VALUES ('EFF-HUNG', 'launch_worker', '{"wait_id":"W01"}', 'RUNNING', 0, 1, 999999, ?, ?, ?)
            """,
            (hung_ts, hung_ts, hung_ts),
        )
        conn.execute(
            """
            INSERT INTO effects (effect_id, kind, payload_json, status, retry_count, attempt_gen, worker_pid, next_run_at, started_at, created_at)
            VALUES ('EFF-DEAD', 'send_qq', '{"text":"hi"}', 'RUNNING', 0, 1, 999998, ?, ?, ?)
            """,
            (recent_ts, recent_ts, recent_ts),
        )
        conn.execute(
            "INSERT OR REPLACE INTO sources (id, kind, auth_state, freshness, last_ok_at, max_silence_sec) VALUES ('src_stale', 'poll', 'OK', 'FRESH', ?, 600)",
            (old_src_ts,),
        )

    res = run_watchdog(sandbox_db)
    assert res["ok"] is True
    assert "EFF-HUNG" in res["killed_effects"]
    assert "src_stale" in res["stale_sources"]

    conn = sandbox_db.connect()
    try:
        st_hung = conn.execute("SELECT status FROM effects WHERE effect_id='EFF-HUNG'").fetchone()["status"]
        st_dead = conn.execute("SELECT status, retry_count FROM effects WHERE effect_id='EFF-DEAD'").fetchone()
        st_src = conn.execute("SELECT freshness FROM sources WHERE id='src_stale'").fetchone()["freshness"]
        assert st_hung == "FAILED"
        assert st_dead["status"] == "PENDING" and int(st_dead["retry_count"]) == 1
        assert st_src == "STALE"
    finally:
        conn.close()


def test_quiet_cfo_receivables_and_natural_ledger_query(sandbox_db: SimingDB) -> None:
    """覆盖 quiet_cfo.py：待回款登记 + 银行卡同额入账自动核销 + 一句话自然语言查账。"""
    reg = register_receivable(sandbox_db, "CW01-202611", "11月滴滴报销", 86.50, kind="reimburse", card_hint="1004")
    assert reg["ok"] is True

    matched = match_incoming_receivable(sandbox_db, txn_id="TX-888", amount_cents=8650, last4="1004", kind="收入")
    assert matched is not None and matched["status"] == "RECEIVED" and matched["matched_txn_id"] == "TX-888"

    ts = iso_now()
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ledger_txns (txn_id, occurred_at, amount_cents, currency, account_alias, kind, merchant, status, created_at, updated_at)
            VALUES ('TX-1', ?, 3580, 'CNY', '建行1004', '消费', '美团外卖·老乡鸡', '待核对', ?, ?),
                   ('TX-2', ?, 12800, 'CNY', '中行1003', '消费', '山姆会员商店', '待核对', ?, ?)
            """,
            (ts, ts, ts, ts, ts, ts),
        )
    q_wm = query_ledger_natural(sandbox_db, "查账 外卖 本月")
    assert q_wm["ok"] is True and q_wm["count"] == 1 and "35.80" in q_wm["reply"]

    q_sam = query_ledger_natural(sandbox_db, "山姆 上次")
    assert q_sam["ok"] is True and q_sam["count"] == 1 and "128.00" in q_sam["reply"]


def test_qq_cards_wave0_buttons_suffix_strip_and_natural_shortcuts(sandbox_db: SimingDB) -> None:
    """覆盖 W0-3/W0-6：加班饭/外卖柜/家电/晨札/带后缀 S43:nudge1 按钮挂载、点击记账与自然「待办」捷径。"""
    ts = iso_now()
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO cases (case_id, title, next_step, done_rule, status, strength, category, rule_id, context_json, created_at, updated_at)
            VALUES ('S43', '周期用药 周三这针', '洗完澡打针', '回打了', '待处理', 'NORMAL', 'med_injection', 'med_care', '{"slot":"2026-11-18"}', ?, ?),
                   ('S48', '美团外卖柜', '下楼取餐', '回拿了', '待处理', 'NORMAL', 'food_pickup', 'meituan_cabinet', '{}', ?, ?)
            """,
            (ts, ts, ts, ts),
        )

    # 1. 带后缀 ref_id `S43:nudge1` 正确识别为 S43 并挂载打针按钮
    kb_inj = infer_keyboard_for_message(sandbox_db, "💉 [S43] 还没打针", ref_id="S43:nudge1", rule_id="med_care")
    assert kb_inj is not None
    assert kb_inj["content"]["rows"][0]["buttons"][0]["action"]["data"] == "siming:case_act:S43:打了"

    # 2. 加班饭、外卖柜、家电送装、晨札均挂载原生按钮
    kb_meal = infer_keyboard_for_message(sandbox_db, "今晚加班吗？要看看晚餐推荐吗", rule_id="overtime_meal")
    assert kb_meal is not None and kb_meal["content"]["rows"][0]["buttons"][0]["action"]["data"] == "siming:cmd:晚餐推荐"

    kb_cab = infer_keyboard_for_message(sandbox_db, "🍱 [S48] 美团外卖已入柜", ref_id="S48", rule_id="meituan_cabinet")
    assert kb_cab is not None and kb_cab["content"]["rows"][0]["buttons"][0]["action"]["data"] == "siming:case_act:S48:拿了"

    kb_app = infer_keyboard_for_message(sandbox_db, "📦 老板烟灶物流更新", ref_id="S20", rule_id="buy_track_watch")
    assert kb_app is not None and kb_app["content"]["rows"][0]["buttons"][0]["action"]["data"] == "siming:cmd:送装清单"

    kb_brief = infer_keyboard_for_message(sandbox_db, "☀️ 司命晨札 · 2026-11-21", ref_id="brief:2026-11-21", rule_id="morning_brief")
    assert kb_brief is not None and len(kb_brief["content"]["rows"][0]["buttons"]) == 2

    # 3. 自然触发「看看待办」返回快捷销账按钮；点击按钮写入 feature_usage
    r_todo = handle_inbound_qq(sandbox_db, "看看待办")
    assert r_todo["handled"] is True and r_todo.get("keyboard") is not None

    r_btn = handle_qq_button_action(sandbox_db, "siming:case_act:S48:拿了")
    assert r_btn["ok"] is True
    # 已结单据不再重复挂载操作按钮
    assert infer_keyboard_for_message(sandbox_db, "[S48] 已销账归档", ref_id="S48", rule_id="meituan_cabinet") is None
    conn = sandbox_db.connect()
    try:
        fu = conn.execute("SELECT COUNT(*) FROM feature_usage WHERE feature_id='qq_button'").fetchone()[0]
        assert fu >= 1
    finally:
        conn.close()
