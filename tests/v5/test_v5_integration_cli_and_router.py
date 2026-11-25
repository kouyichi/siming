"""司命 v5（Wave 1–4）全链路总装集成测试：CLI、modules、evald tick、router/QQ 按钮与控制塔。"""

from __future__ import annotations

import json

from siming import cli
from siming.approval import verify_and_apply_approval
from siming.db import SimingDB, iso_now
from siming.evald import run_tick
from siming.modules import MODULE_SPECS, is_module_enabled, set_module_enabled
from siming.qq_cards import handle_qq_button_action, infer_keyboard_for_message
from siming.router import handle_inbound_qq


def test_v5_modules_registry_count_and_deferral_boundaries(sandbox_db: SimingDB) -> None:
    """确认全部 24 项已拍板 v5 模块已注册并默认启用（D2-C），且暂缓项 W4-2/W4-4 未注册。"""
    ids = {m.module_id for m in MODULE_SPECS}
    expected_v5 = {
        "sure_finance_os",
        "compute_assets",
        "sub_assassin",
        "meal_combo_guard",
        "parcel_sentinel",
        "health_cockpit",
        "vehicle_cfo",
        "home_air_guard",
        "homelab_ops",
        "bambu_copilot",
        "haos_night_owl",
        "commute_hub",
        "home_passport",
        "multi_runtime",
        "hindsight_wiki",
        "msim_memory",
        "overnight_lab",
        "research_pr_radar",
        "digest_indie",
        "infra_healer",
        "context_graph",
        "trip_packager",
        "social_boardgames",
        "compound_tower",
    }
    assert expected_v5.issubset(ids)
    assert len(MODULE_SPECS) >= 53
    # 暂缓项 W4-2 (个税) 与 W4-4 (跑腿/证件死线) 不应被注册
    assert "tax_deduction" not in ids
    assert "city_errands" not in ids
    for mid in expected_v5:
        assert is_module_enabled(sandbox_db, mid) is True
    assert set_module_enabled(sandbox_db, "sub_assassin", False)["ok"] is True
    assert is_module_enabled(sandbox_db, "sub_assassin") is False


def test_v5_cli_commands_smoke_all_four_waves(sandbox_db: SimingDB, monkeypatch, capsys) -> None:
    """端到端覆盖 Wave 1–4 关键 CLI 子命令。"""
    monkeypatch.setattr(cli, "SimingDB", lambda: sandbox_db)

    # Wave 1
    assert cli.main(["sure", "status"]) == 0
    assert "Sure" in capsys.readouterr().out
    assert cli.main(["compute", "upsert", "deepseek", "main", "--balance", "500", "--daily-spend", "80", "--avg-daily", "20", "--fallback", "kimi-k2"]) == 0
    capsys.readouterr()
    assert cli.main(["compute", "check"]) == 0
    out_c = json.loads(capsys.readouterr().out)
    assert len(out_c.get("alerts") or []) >= 1
    assert cli.main(["subs", "audit"]) == 0
    capsys.readouterr()
    assert cli.main(["meal", "combo", "示例粉面炒米粉 + 示例果切西瓜"]) == 0
    assert "双拼" in capsys.readouterr().out or "示例粉面" in capsys.readouterr().out
    assert cli.main(["health", "ingest", "空腹血糖 5.4 mmol/L，尿酸 460 umol/L"]) == 0
    capsys.readouterr()
    assert cli.main(["health", "doctor-brief"]) == 0
    assert "常用医院" in capsys.readouterr().out
    assert cli.main(["car", "status"]) == 0
    capsys.readouterr()

    # Wave 2
    assert cli.main(["home-air", "ingest", "--hcho", "0.14", "--temp", "24", "--aqi", "42"]) == 0
    capsys.readouterr()
    assert cli.main(["home-air", "status"]) == 0
    capsys.readouterr()
    assert cli.main(["homelab", "backup-plan"]) == 0
    capsys.readouterr()
    assert cli.main(["print3d", "add-spool", "SP-01", "--material", "PLA", "--grams", "300"]) == 0
    capsys.readouterr()
    assert cli.main(["haos", "vacuum"]) == 0
    capsys.readouterr()
    assert cli.main(["commute", "compare", "--ev-min", "45", "--metro-min", "60"]) == 0
    capsys.readouterr()
    assert cli.main(["passport", "fault", "格力", "E1"]) == 0
    capsys.readouterr()

    # Wave 3
    assert cli.main(["runtime", "select", "--kind", "coding", "--complexity", "high"]) == 0
    rt_out = json.loads(capsys.readouterr().out)
    assert rt_out.get("runtime") in ("claude_sub", "codex_sub", "pi_deepseek", "pi_kimi", "pi_redpill", "pi_local_ollama")
    assert cli.main(["wiki", "status"]) == 0
    capsys.readouterr()
    assert cli.main(["msim", "record", "meituan_dom", "claude-opus", "直航 poi_id_str 避开搜索页", "--score", "0.95", "--baseline", "0.40"]) == 0
    capsys.readouterr()
    assert cli.main(["msim", "inject", "meituan_dom", "--target-model", "gemini"]) == 0
    assert "poi_id_str" in capsys.readouterr().out
    assert cli.main(["handoff", "add", "indie_game-eval", "--host", "gpu-server"]) == 0
    capsys.readouterr()
    assert cli.main(["radar", "add-pain", "siming", "sqlite lock", "--keywords", "sqlite,wal,lock"]) == 0
    capsys.readouterr()
    assert cli.main(["digest", "indie", "肉鸽卡牌"]) == 0
    capsys.readouterr()

    # Wave 4
    assert cli.main(["trip", "create", "东京", "2026-12-20", "2026-12-25", "--country", "JP"]) == 0
    capsys.readouterr()
    assert cli.main(["social", "crowdfund", "诡镇奇谈大盒", "--platform", "新物集", "--balance", "280", "--due", "2026-11-26"]) == 0
    capsys.readouterr()
    assert cli.main(["graph", "index"]) == 0
    capsys.readouterr()
    assert cli.main(["graph", "query", "东京"]) == 0
    capsys.readouterr()
    assert cli.main(["tower", "snapshot"]) == 0
    snap = json.loads(capsys.readouterr().out)
    assert "wave1_finance" in snap and "wave2_smarthome" in snap and "wave3_knowledge" in snap and "wave4_personal_os" in snap


def test_v5_router_compound_voice_approval_and_qq_buttons(sandbox_db: SimingDB) -> None:
    """验证 QQ 确定性路由：复合语音单卡审批 + apply 执行 + 订阅刺客按钮回调。"""
    ts = iso_now()
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO cases (case_id, title, next_step, done_rule, status, strength, category, rule_id, context_json, created_at, updated_at)
            VALUES ('S90', '美团外卖·牛板筋', '下楼取餐', '回拿了', '待处理', 'NORMAL', 'food_pickup', 'meituan_cabinet', '{}', ?, ?)
            """,
            (ts, ts),
        )
        conn.execute(
            """
            INSERT INTO subscription_audit (sub_id, merchant, normalized_name, cadence_days, amount_cents, currency, occurrences, last_charged_at, next_expected_at, status, updated_at)
            VALUES ('SUB-01', '点众小说', '点众小说', 7, 1500, 'CNY', 5, ?, ?, 'ACTIVE', ?)
            """,
            (ts, ts, ts),
        )

    # 1. 复合口语指令自动打包为单张 A2 确认卡 + 原生按钮
    r_cmp = handle_inbound_qq(sandbox_db, "销掉牛板筋、设明天9点提醒我抢号")
    assert r_cmp["handled"] is True
    assert "apply A" in r_cmp["reply"]
    assert r_cmp.get("keyboard") is not None

    # 2. 通过 approval.py 纯代码 apply 执行该复合审批卡
    import re

    m_tok = re.search(r"apply\s+(A\d+)", r_cmp["reply"])
    assert m_tok
    app_res = verify_and_apply_approval(sandbox_db, m_tok.group(1))
    assert app_res["ok"] is True

    conn = sandbox_db.connect()
    try:
        assert conn.execute("SELECT status FROM cases WHERE case_id='S90'").fetchone()["status"] == "完成"
    finally:
        conn.close()

    # 3. 订阅刺客止损卡按钮推断 + 点击标记 ZOMBIE
    kb = infer_keyboard_for_message(sandbox_db, "✂️ 订阅刺客预警：点众小说 48h 内续费", ref_id="SUB-01", rule_id="sub_assassin")
    assert kb is not None
    btn_res = handle_qq_button_action(sandbox_db, "siming:sub_act:SUB-01:ZOMBIE")
    assert btn_res["ok"] is True

    # 4. 其它 v5 QQ 轻量捷径
    assert handle_inbound_qq(sandbox_db, "算力资产")["handled"] is True
    assert handle_inbound_qq(sandbox_db, "进化日记")["handled"] is True
    assert handle_inbound_qq(sandbox_db, "行程包")["handled"] is True

    # 5. evald run_tick 跑通 v5 守卫零异常
    tick_res = run_tick(sandbox_db, include_collectors=False)
    assert tick_res["ok"] is True
