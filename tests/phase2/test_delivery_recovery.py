"""Phase 2 硬验收（S2-A + 决定 3=A / 5=B）：夜间队列不丢消息、隔夜 apply 刷新有效期、四合一晨札与回复「全文」、81 死人开关发送失败保护、批量取件与防寒暄误开单。"""

from __future__ import annotations

import importlib.util
from datetime import timedelta
from pathlib import Path

import pytest

from siming.approval import create_approval, verify_and_apply_approval
from siming.collectors.pickup_codes import try_ingest_pickup_code
from siming.db import SimingDB, iso_now, now_local
from siming.morning import build_morning_editions, save_morning_editions_to_meta
from siming.router import (
    flush_morning_brief_if_due,
    flush_pickup_window_if_due,
    handle_inbound_qq,
    send_qq_message,
)


def _load_edge_module():
    edge_py = Path(__file__).resolve().parents[2] / "edge" / "siming_edge.py"
    spec = importlib.util.spec_from_file_location("siming_edge_p2", edge_py)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_morning_queue_preserves_6_items_and_refreshes_overnight_apply_ttl(
    sandbox_db: SimingDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 将晨札窗口设为 00:00 确保任何时刻运行单测均可触发送达
    (sandbox_db.paths.config_dir / "policy.yaml").write_text(
        "windows:\n  morning_brief: '00:00'\n  pickup_batch: '00:00'\n",
        encoding="utf-8",
    )

    target_file = sandbox_db.paths.tmp_root / "overnight.md"
    # 模拟凌晨 02:00 生成的口令，原始 30m 有效期早已过期（ttl_minutes=-120）
    ap = create_approval(
        sandbox_db,
        action="append_block_with_backup",
        target_id="W09",
        params={"path": str(target_file), "marker": "<!-- ok -->", "block": "<!-- ok -->\ndone"},
        ttl_minutes=-120,
    )
    tok = ap["token"]

    ts = iso_now()
    with sandbox_db.transaction() as conn:
        for i in range(1, 6):
            conn.execute(
                "INSERT INTO outbox (id, ref_id, rule_id, text, send_at, status, channel, created_at) VALUES (?, ?, 'test', ?, ?, 'MORNING_QUEUE', 'qqbot', ?)",
                (f"MQ-{i}", f"S0{i}", f"夜间消息第 {i} 条重要进展说明", ts, ts),
            )
        # 第 6 条把 `apply Axx` 放在第 4 行末尾（超过 90 字），验证绝不被截断！
        long_stage_msg = (
            "[W09] 后台 stage 改动已就绪并通过验收（`pixi run pytest` = 0）\n"
            "仓库：/home/user/Work/siming\n"
            "改动统计：src/siming/foo.py | 12 ++++++++++++\n"
            f"30 分钟内直接回复 `apply {tok}` 合入主工作区。"
        )
        conn.execute(
            "INSERT INTO outbox (id, ref_id, rule_id, text, send_at, status, channel, created_at) VALUES ('MQ-6', 'W09', 'worker_stage_ready', ?, ?, 'MORNING_QUEUE', 'qqbot', ?)",
            (long_stage_msg, ts, ts),
        )

    res = flush_morning_brief_if_due(sandbox_db, was_reboot_gap=False)
    assert res is not None and res["ok"] is True

    # 推送的精炼晨札必须完整包含第 6 条末尾的 `apply Axx` 口令
    conn = sandbox_db.connect()
    sent_brief = conn.execute(
        "SELECT text FROM outbox WHERE rule_id='morning_brief' AND status='SENT' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()["text"]
    conn.close()
    assert f"apply {tok}" in sent_brief

    # 全文里必须完整保留第 1..6 全部 6 条夜间消息
    full_rep = handle_inbound_qq(sandbox_db, "晨札全文")
    assert full_rep["handled"] is True
    for i in range(1, 6):
        assert f"夜间消息第 {i} 条" in full_rep["reply"]

    # 晨札实际送达后，原本半夜过期的 apply 口令已被刷新 30m 有效期，用户立即回复 `apply Axx` 必须成功！
    ap_exec = verify_and_apply_approval(sandbox_db, tok)
    assert ap_exec["ok"] is True
    assert "done" in target_file.read_text(encoding="utf-8")


def test_unified_4in1_morning_brief_grounded_ziwei_and_quote_reply_full(sandbox_db: SimingDB) -> None:
    today_s = now_local().date().isoformat()
    cron_root = sandbox_db.paths.tmp_root / "hermes-cron-out"
    for jid, content in (
        (
            "affd2f6445fe",
            f"# Cron\n\n---\n\n☀️ 早安\n\n🌤️ 北京天气：晴，当前 8.0°C\n　　今日 3.0~12.0°C\n\n💳 额度剩余\n· Codex(pro)：周额度 剩 92%\n\n🔮 今日紫微运势\n旧模板套话不应出现",
        ),
        (
            "51419061eb21",
            "## Prompt\nxxx\n## Response\n☀️ 早安运维\n· cron 体检：21/21 正常\n· 共 126 个开放 PR\n需要你处理：过一次 BackerKit 验证",
        ),
        (
            "52f4fdce0602",
            "# Cron\n\n---\n\n🤖 AI 前沿日报\n· DeepSeek 发布新一代稀疏注意力内核\n· Qwen3 本地量化推理提速 40%",
        ),
        (
            "c0d6e1954cda",
            "# Cron\n\n---\n\n🌅 桌游日报\n📌 TLDR·三秒速览\n① 周三截止：最终幻想 KS\n② 待办：填 Orlog 问卷\n━━━━━━━━\n• 已建规则库 954 款 | hit@1: 0.505",
        ),
    ):
        d = cron_root / jid
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{today_s}_08-00-00.md").write_text(content, encoding="utf-8")

    # 插入一条真实待办单据，验证紫微流日直接锚定到该真实事项而非空泛套话
    ts = iso_now()
    with sandbox_db.transaction() as conn:
        conn.execute(
            "INSERT INTO cases (case_id, title, next_step, status, category, created_at, updated_at) VALUES ('S12', '海尔壁挂洗衣机待约打孔安装', '联系师傅', '待处理', 'appliance', ?, ?)",
            (ts, ts),
        )

    fake_zw = lambda _d: {
        "ok": True,
        "today": today_s,
        "daily": {
            "ganzhi": "乙未",
            "lunarDate": "二〇二六年十月初九",
            "mingGong": {"natalPalaceName": "仆役", "stars": ["太阴", "火星"]},
            "siHua": [
                {"type": "化禄", "star": "天机", "inNatalPalace": "田宅"},
                {"type": "化权", "star": "天梁", "inNatalPalace": "子女"},
                {"type": "化科", "star": "紫微", "inNatalPalace": "官禄"},
                {"type": "化忌", "star": "太阴", "inNatalPalace": "仆役"},
            ],
        },
    }

    ed = build_morning_editions(sandbox_db, cron_output_root=cron_root, ziwei_runner=fake_zw)
    save_morning_editions_to_meta(sandbox_db, ed)
    concise = ed["concise"]

    assert "晴，当前 8.0°C" in concise
    assert "旧模板套话不应出现" not in concise
    assert "流日乙未" in concise
    assert "天机化禄" in concise and "海尔壁挂洗衣机" in concise
    assert "DeepSeek" in concise
    assert "最终幻想 KS" in concise

    # 发出晨札后，用户在 QQ 点「引用回复」发「全文 桌游」与「全文 紫微」
    sent = send_qq_message(
        sandbox_db,
        concise,
        ref_id=f"brief:{today_s}",
        rule_id="morning_brief",
        bypass_quiet=True,
    )
    r_bg = handle_inbound_qq(sandbox_db, "全文 桌游", reply_to_msg_id=sent["msg_id"])
    assert r_bg["handled"] is True and "已建规则库 954 款" in r_bg["reply"]

    r_zw = handle_inbound_qq(sandbox_db, "全文 紫微", reply_to_msg_id=sent["msg_id"])
    assert r_zw["handled"] is True and "紫微化科飞入本命【官禄】" in r_zw["reply"]


def test_edge_deadman_never_marks_sent_offline_when_webhook_fails(sandbox_db: SimingDB) -> None:
    edge_mod = _load_edge_module()
    store = edge_mod.EdgeStore(sandbox_db.paths.tmp_root / "edge-deadman")
    # 写入一个 20 分钟前的心跳与一条已到期的硬期限提醒，但故意不配 alert.json（模拟发送失败）
    store.record_host_heartbeat("2020-01-01T00:00:00+08:00")
    conn = store.connect()
    conn.execute("UPDATE meta SET value='1000' WHERE key='last_host_hb_epoch'")
    conn.close()
    store.put_outbox("W77", "[W77] 硬期限还款提醒", "2020-01-01T09:00:00+08:00", "2020-01-01T08:00:00+08:00")

    dm = store.run_deadman_check(silence_limit_sec=900)
    assert dm["state"] == "host_offline"
    assert dm["alerted"] is False
    assert dm["fired_outbox"] == []
    assert dm["failed_outbox"] == ["W77"]

    conn = store.connect()
    st = conn.execute("SELECT status FROM prescheduled_outbox WHERE ref_id='W77'").fetchone()["status"]
    conn.close()
    assert st == "PENDING"


def test_batch_pickup_closes_all_and_casual_thanks_never_reopens_closed_case(sandbox_db: SimingDB) -> None:
    (sandbox_db.paths.config_dir / "policy.yaml").write_text(
        "quiet_hours:\n  start: '23:59'\n  end: '00:00'\nwindows:\n  morning_brief: '08:15'\n  pickup_batch: '00:00'\n",
        encoding="utf-8",
    )
    p1 = try_ingest_pickup_code(sandbox_db, "【菜鸟驿站】凭取件码 1-2-1001 取件")
    p2 = try_ingest_pickup_code(sandbox_db, "【菜鸟驿站】凭取件码 1-2-1002 取件")
    p3 = try_ingest_pickup_code(sandbox_db, "【丰巢】凭取件码 883322 取件")
    assert p1["matched"] and p2["matched"] and p3["matched"]

    pk_sent = flush_pickup_window_if_due(sandbox_db)
    assert pk_sent is not None and pk_sent["ok"] is True

    # 1. 对合并取件卡片引用回复一句“取了”，3 件必须全部完结！
    r_all = handle_inbound_qq(sandbox_db, "取了", reply_to_msg_id=pk_sent["msg_id"])
    assert r_all["handled"] is True
    conn = sandbox_db.connect()
    open_pickups = conn.execute(
        "SELECT COUNT(*) FROM cases WHERE category='pickup' AND status='待处理'"
    ).fetchone()[0]
    conn.close()
    assert open_pickups == 0

    # 2. 对已完结单据引用回复“好的”、“谢谢”、“收到”绝不能把单据重新打开为“待处理”！
    for casual in ("好的", "谢谢", "收到", "ok", "？"):
        r_cas = handle_inbound_qq(sandbox_db, casual, reply_to_msg_id=pk_sent["msg_id"])
        assert r_cas["handled"] is False

    conn = sandbox_db.connect()
    st_after = conn.execute("SELECT status FROM cases WHERE case_id=?", (p1["case_id"],)).fetchone()["status"]
    conn.close()
    assert st_after == "完成"

    # 3. 明确说“约了12月10日上午”则确定性更新 due_at 并续接单据！
    r_date = handle_inbound_qq(sandbox_db, "约了12月10日上午安装", reply_to_msg_id=pk_sent["msg_id"])
    assert r_date["handled"] is True
    conn = sandbox_db.connect()
    c_upd = conn.execute("SELECT status, due_at FROM cases WHERE case_id=?", (p1["case_id"],)).fetchone()
    conn.close()
    assert c_upd["status"] == "待处理"
    assert "-12-10T09:30:00" in str(c_upd["due_at"])


def test_ziwei_interpretation_is_generic_not_coupled_to_known_cases() -> None:
    from siming.morning import build_grounded_ziwei

    zw = {
        "ok": True,
        "today": "2026-11-18",
        "daily": {
            "ganzhi": "丙申",
            "lunarDate": "十月初十",
            "mingGong": {"natalPalaceName": "迁移", "stars": ["贪狼"]},
            "siHua": [
                {"type": "化禄", "star": "天同", "inNatalPalace": "兄弟"},
                {"type": "化权", "star": "天机", "inNatalPalace": "田宅"},
                {"type": "化科", "star": "文昌", "inNatalPalace": "子女"},
                {"type": "化忌", "star": "廉贞", "inNatalPalace": "仆役"},
            ],
        },
    }
    anchors = {"home": ["[S03]壁挂洗衣机"], "money": [], "self": []}
    concise, full = build_grounded_ziwei(zw, anchors)
    # 已知的装修事项只在田宅宫举例一次，不外溢到兄弟/子女/仆役等宫
    assert full.count("壁挂洗衣机") == 1 and "【田宅】" in full
    assert "壁挂洗衣机" not in concise  # 精炼版只讲化禄(兄弟)/化忌(仆役)，无对应已知事项就纯讲通用象义
    assert "同辈、搭档与手足往来" in concise and "朋友、同事与外部人脉" in concise
    assert "送装" not in concise and "师傅" not in full


def test_ziwei_refpack_natal_hints_and_qq_query(sandbox_db: SimingDB, tmp_path) -> None:
    import json as _json

    from siming.morning import build_grounded_ziwei
    from siming.ziwei_ref import load_natal, natal_reply, sync_refpack

    birth = tmp_path / "birth.json"
    birth.write_text(_json.dumps({"solarDate": "1993-10-25", "timeIndex": 6, "gender": "male"}), encoding="utf-8")
    repo = tmp_path / "repo"
    (repo / "docs" / "tianji-ziwei").mkdir(parents=True)
    (repo / "docs" / "tianji-ziwei" / "entries.jsonl").write_text(
        _json.dumps({"book": "01-stars", "h2": "廉贞（北斗）", "h3": "定性", "quote": "廉贞也代表武官带，武官星的意思。", "part": 5, "t_hms": "00:46:17"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    rec = {
        "k": "1993-10-25-6-male",
        "ms": "武曲",
        "c": {
            "wealth": "**【财运总览】**\n\n钱一阵一阵来。\n\n建议你赚得多时先把钱分走。剩下再花。\n\n**【一句话定调】**\n\n天相带来富足。\n\n**【核心论断】**\n\n略",
            "family": "**【兄弟总览】**\n\n好相处。\n\n**【一句话定调】**\n\n合伙得力。",
        },
    }
    res = sync_refpack(sandbox_db, repo=repo, birth_file=birth, record_override=rec)
    assert res["ok"] and res["counts"]["natal_dims"] == 2 and res["counts"]["tianji_quotes"] == 1
    natal = load_natal(sandbox_db)
    assert natal and natal["dims"]["wealth"]["headline"] == "天相带来富足。"

    zw = {
        "ok": True,
        "today": "2026-11-18",
        "daily": {
            "ganzhi": "丙申",
            "lunarDate": "十月初十",
            "mingGong": {"natalPalaceName": "迁移", "stars": []},
            "siHua": [
                {"type": "化禄", "star": "天同", "inNatalPalace": "兄弟"},
                {"type": "化忌", "star": "廉贞", "inNatalPalace": "财帛"},
            ],
        },
    }
    from siming.ziwei_ref import pick_tianji_quote

    concise, full = build_grounded_ziwei(zw, {}, natal=natal, quote_picker=lambda s: pick_tianji_quote(sandbox_db, s, "2026-11-18"))
    assert "本命财运提醒：建议你赚得多时先把钱分走。" in concise
    assert "本命兄弟定调：合伙得力" in full and "倪师论廉贞" in full

    assert "财运（财帛）：天相带来富足" in natal_reply(sandbox_db)
    r = handle_inbound_qq(sandbox_db, "本命 财帛")
    assert r["handled"] and "钱一阵一阵来" in r["reply"] and "CC BY 4.0" in r["reply"]
