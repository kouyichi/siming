"""QQ 语音转写（`[Voice] ...。` / `？` / 无空格 / 中文数字 / 复合口语）全行为覆盖回归测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

from siming.approval import create_approval
from siming.curiosity import enqueue_curiosity_question
from siming.daily_care import run_injection_guard
from siming.db import SHANGHAI_TZ, SimingDB, iso_now
from siming.router import handle_inbound_qq, send_qq_message


def test_voice_sleep_wake_and_readonly_shortcuts_with_punct(sandbox_db: SimingDB) -> None:
    # 1. 语音睡了 / 醒了（带句号），且问句『睡了？』不得误改作息状态
    r_q_sleep = handle_inbound_qq(sandbox_db, "[Voice] 睡了？")
    assert r_q_sleep["handled"] is False

    r_sleep = handle_inbound_qq(sandbox_db, "[Voice] 我要睡了。")
    assert r_sleep["handled"] is True and "睡" in r_sleep["reply"]

    r_wake = handle_inbound_qq(sandbox_db, "[Voice] 起床了！")
    assert r_wake["handled"] is True

    # 2. 常见只读查询语音（句末带句号或问号）全部直达
    for voice_msg in (
        "[Voice] 今天。",
        "[Voice] 还有啥没拿？",
        "[Voice] 待办？",
        "[Voice] 看看方案？",
        "[Voice] 推演好了吗？",
        "[Voice] 我的位置。",
        "[Voice] 特斯拉。",
        "[Voice] 你记了我哪些规律？",
        "[Voice] 挂起列表。",
        "[Voice] 模块列表。",
        "[Voice] 作息状态？",
        "[Voice] 保价。",
    ):
        r = handle_inbound_qq(sandbox_db, voice_msg)
        assert r["handled"] is True, f"语音快捷指令漏匹配: {voice_msg}"


def test_voice_approvals_quoted_replies_and_question_guard(sandbox_db: SimingDB, tmp_path: Path) -> None:
    target_file = tmp_path / "voice_rule.md"
    ap = create_approval(
        sandbox_db,
        action="append_block_with_backup",
        target_id="W01",
        params={"path": str(target_file), "marker": "VM1", "block": "voice-ok"},
        ttl_minutes=15,
    )
    tok = ap["token"]

    # 问句『确认？』绝不得触发审批合入
    r_ask = handle_inbound_qq(sandbox_db, "[Voice] 确认？")
    assert r_ask["handled"] is False
    assert not target_file.exists()

    # 陈述语音『确认。』正常合入
    r_ok = handle_inbound_qq(sandbox_db, "[Voice] 确认。")
    assert r_ok["handled"] is True
    assert target_file.is_file() and "voice-ok" in target_file.read_text(encoding="utf-8")

    # 实付卡：问句『确认支付？』严禁扣款；陈述语音『确认支付。』命中实付确认分支
    ap_pay = create_approval(
        sandbox_db,
        action="meituan_submit_pay",
        target_id="MT-V1",
        params={"shop": "测试店", "payable_yuan": 25.0},
        ttl_minutes=10,
    )
    r_pay_q = handle_inbound_qq(sandbox_db, "[Voice] 确认支付？")
    assert r_pay_q["handled"] is False

    # 引用取消语音『取消。』可直接作废审批卡
    quoted_cancel = f"[Quoted message]:\n付款确认卡 apply {ap_pay['token']}\n\n[Voice] 取消。"
    r_cancel = handle_inbound_qq(sandbox_db, quoted_cancel)
    assert r_cancel["handled"] is True and "已取消" in r_cancel["reply"]

    # 引用取件卡语音回复『取了。』/『拿完了。』直接销账
    ts = iso_now()
    with sandbox_db.transaction() as conn:
        cid = sandbox_db.next_seq_id(conn, "S")
        conn.execute(
            """
            INSERT INTO cases (case_id, title, next_step, status, strength, category, rule_id, created_at, updated_at)
            VALUES (?, '丰巢取件码 668811', '去丰巢取件', '待处理', 'SOFT', 'pickup', 'pickup_1800', ?, ?)
            """,
            (cid, ts, ts),
        )
    sent = send_qq_message(
        sandbox_db,
        f"📦 回家顺路取件（共 1 件）\n• 丰巢：[{cid}] 取件码 `668811`",
        ref_id=cid,
        rule_id="pickup_1800",
        bypass_quiet=True,
    )
    r_pick = handle_inbound_qq(sandbox_db, "[Voice] 拿完了。", reply_to_msg_id=sent["msg_id"])
    assert r_pick["handled"] is True and "已销账归档" in r_pick["reply"]


def test_voice_daily_care_real_compound_sentence_and_guards(sandbox_db: SimingDB) -> None:
    # 建一张待打针单
    t_wed_night = datetime(2026, 11, 19, 1, 0, tzinfo=SHANGHAI_TZ)
    sandbox_db.set_meta("last_human_active_at", iso_now(t_wed_night))
    run_injection_guard(sandbox_db, now=t_wed_night)

    # 真实历史 QQ 语音原话（2026-11-19 04:07）：『好了，针打完了，也记录一下儿，针头还有6个。』
    r_real = handle_inbound_qq(sandbox_db, "[Voice] 好了，针打完了，也记录一下儿，针头还有6个。")
    assert r_real["handled"] is True
    assert "已登记 周期用药" in r_real["reply"]
    assert "注射笔针头还剩 6" in r_real["reply"]

    # 真实历史规则讨论语音绝不能被误判为已打针
    r_rule_chat = handle_inbound_qq(
        sandbox_db,
        "[Voice] 不是就是因为今天还没有睡觉，所以我说的今天指的是周三。一般是每周三来打针，然后预约去医院。",
    )
    assert r_rule_chat["handled"] is False

    # 中文数字 + 还有 + 句号
    r_doses = handle_inbound_qq(sandbox_db, "[Voice] 周期用药还有三针。")
    assert r_doses["handled"] is True and "3" in r_doses["reply"]

    r_mask = handle_inbound_qq(sandbox_db, "[Voice] 口罩还有十二只。")
    assert r_mask["handled"] is True and "12" in r_mask["reply"]

    r_care_q = handle_inbound_qq(sandbox_db, "[Voice] 耗材余量？")
    assert r_care_q["handled"] is True and "日常照护面板" in r_care_q["reply"]


def test_voice_parameterized_actions_without_spaces_and_with_punct(
    sandbox_db: SimingDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. 账单语音查询（带问号与「这个月」）
    ts = iso_now()
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ledger_txns (
                txn_id, occurred_at, account_alias, card_last4, amount_cents, currency,
                kind, merchant, status, notes, created_at, updated_at
            ) VALUES ('TX-V1', ?, '招商银行', '1001', 3500, 'CNY', '消费', '美团外卖', 'MATCHED', '', ?, ?)
            """,
            (ts, ts, ts),
        )
    r_led = handle_inbound_qq(sandbox_db, "[Voice] 外卖本月花了多少？")
    assert r_led["handled"] is True and "35.00" in r_led["reply"]

    # 2. 语音无空格查知识、开关模块、更新特斯拉电量
    r_mod_off = handle_inbound_qq(sandbox_db, "[Voice] 关闭模块自动备车。")
    assert r_mod_off["handled"] is True and "已关闭" in r_mod_off["reply"]
    r_mod_on = handle_inbound_qq(sandbox_db, "[Voice] 开启模块自动备车。")
    assert r_mod_on["handled"] is True and "已开启" in r_mod_on["reply"]

    r_soc = handle_inbound_qq(sandbox_db, "[Voice] 特斯拉电量78%。")
    assert r_soc["handled"] is True and "78" in r_soc["reply"]

    r_ask = handle_inbound_qq(sandbox_db, "[Voice] 查一下次卧窗帘盒多宽？")
    assert r_ask["handled"] is True

    # 3. 语音滴滴打车（无末尾句号污染目的地）
    seen_didi: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "siming.didi_mcp.request_didi_ride",
        lambda db, dest_raw, origin_raw="": (seen_didi.append((dest_raw, origin_raw)) or {"reply": f"打车->{dest_raw}"}),
    )
    r_didi1 = handle_inbound_qq(sandbox_db, "[Voice] 帮我打车回家。")
    assert r_didi1["handled"] is True and seen_didi[-1] == ("家", "")
    r_didi2 = handle_inbound_qq(sandbox_db, "[Voice] 帮我打车去公司。")
    assert r_didi2["handled"] is True and seen_didi[-1] == ("公司", "")

    # 4. 语音美团确认第一家 / 点外卖带逗号句号
    seen_spec: list[str] = []
    monkeypatch.setattr(
        "siming.meituan_speculate.confirm_and_execute_speculative_pick",
        lambda db, q: (seen_spec.append(q) or {"reply": f"已选 {q}"}),
    )
    r_sp = handle_inbound_qq(sandbox_db, "[Voice] 确认第一家。")
    assert r_sp["handled"] is True and seen_spec == ["第一家"]

    seen_mt: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "siming.meituan_agent.stage_meituan_h5_order",
        lambda db, q, location_override="": (seen_mt.append((q, location_override)) or {"reply": f"下单 {q}"}),
    )
    r_mt = handle_inbound_qq(sandbox_db, "[Voice] 帮我点外卖，西瓜送到家。")
    assert r_mt["handled"] is True and seen_mt[-1] == ("西瓜", "家")

    # 5. 主动求证：语音『忽略。』和无空格『记住地点羽毛球馆。』
    qid = enqueue_curiosity_question(
        sandbox_db,
        topic="place_dwell",
        question="停留了 40 分钟",
        context={"lat": 39.99, "lng": 116.36},
        dedup_key="voice:dwell1",
    )
    assert qid is not None
    r_cur = handle_inbound_qq(sandbox_db, "[Voice] 记住地点羽毛球馆。")
    assert r_cur["handled"] is True and "羽毛球馆" in r_cur["reply"]

    qid2 = enqueue_curiosity_question(
        sandbox_db,
        topic="place_dwell",
        question="停留了 45 分钟",
        context={"lat": 39.98, "lng": 116.35},
        dedup_key="voice:dwell2",
    )
    assert qid2 is not None
    r_ign = handle_inbound_qq(sandbox_db, "[Voice] 忽略。")
    assert r_ign["handled"] is True and "已忽略" in r_ign["reply"]


def test_siming_bridge_voice_long_timeout_and_fail_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_py = Path(__file__).resolve().parents[1] / "integrations" / "hermes-plugin" / "__init__.py"
    spec = importlib.util.spec_from_file_location("siming_bridge_voice_test", plugin_py)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    captured: list[tuple[float, str]] = []

    async def fake_cli(_args, timeout_sec=25.0, stdin_bytes=None):
        payload = json.loads((stdin_bytes or b"{}").decode("utf-8"))
        captured.append((float(timeout_sec), str(payload.get("text") or "")))
        return 0, json.dumps({"handled": True, "reply": "ok"}, ensure_ascii=False)

    monkeypatch.setattr(mod, "_run_siming_cli", fake_cli)

    for voice_long in (
        "[Voice] 确认支付。",
        "[Voice] 确认第一家。",
        "[Voice] 帮我点外卖，西瓜。",
        "[Voice] 给我点一杯冰美式。",
    ):
        asyncio.run(
            mod._on_post_gateway_admission(
                platform="qqbot",
                text=voice_long,
                message_id="m-v",
                source={"chat_id": "U1"},
            )
        )
        assert captured[-1][0] >= 180.0, f"语音长任务超时未抬升: {voice_long}"

    # 语音敏感支付口令若 CLI 超时必须 fail-closed（绝不放行给 LLM）
    async def fake_timeout(_args, timeout_sec=25.0, stdin_bytes=None):
        return 124, "司命命令执行超时"

    monkeypatch.setattr(mod, "_run_siming_cli", fake_timeout)
    res_fc = asyncio.run(
        mod._on_post_gateway_admission(
            platform="qqbot",
            text="[Voice] 确认支付。",
            message_id="m-v2",
            source={"chat_id": "U1"},
        )
    )
    assert res_fc is not None and res_fc["action"] == "handled" and "安全策略拦截" in res_fc["reply"]
