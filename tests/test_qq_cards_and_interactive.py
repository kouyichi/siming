"""QQ 原生确认框 / 选择框（qq_cards.py）与 siming-bridge 交互回调全链路回归测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

from siming.approval import cancel_approval, create_approval
from siming.curiosity import enqueue_curiosity_question
from siming.db import SimingDB, iso_now
from siming.qq_cards import (
    build_approval_card_keyboard,
    build_meituan_recommend_keyboard,
    handle_qq_button_action,
    infer_keyboard_for_message,
)
from siming.router import handle_inbound_qq, send_qq_message


def test_approval_card_keyboard_inference_and_apply_cancel(sandbox_db: SimingDB, tmp_path: Path) -> None:
    target_file = tmp_path / "rule.md"
    ap = create_approval(
        sandbox_db,
        action="append_block_with_backup",
        target_id="W01",
        params={"path": str(target_file), "marker": "M1", "block": "hello-qq-card"},
        ttl_minutes=15,
    )
    tok = ap["token"]
    card_txt = f"确认写入规则文件，15 分钟内回复 `apply {tok}` 执行。"

    # 1. PENDING 审批自动推断出 [确认写入 Axx] [作废 Axx]
    kb = infer_keyboard_for_message(sandbox_db, card_txt)
    assert kb is not None
    btns = kb["content"]["rows"][0]["buttons"]
    assert len(btns) == 2
    assert btns[0]["action"]["data"] == f"siming:apply:{tok}"
    assert btns[1]["action"]["data"] == f"siming:cancel:{tok}"
    assert btns[0]["group_id"] == btns[1]["group_id"]

    # 2. 点击取消按钮 -> 原子作废，不写目标文件
    res_cancel = handle_qq_button_action(sandbox_db, f"siming:cancel:{tok}")
    assert res_cancel["ok"] is True
    assert "已取消" in res_cancel["reply"]
    assert not target_file.exists()

    # 3. 已作废口令不再推断出按钮
    assert infer_keyboard_for_message(sandbox_db, card_txt) is None

    # 4. 新建第二张审批卡并点击确认按钮 -> 纯代码执行落盘
    ap2 = create_approval(
        sandbox_db,
        action="append_block_with_backup",
        target_id="W02",
        params={"path": str(target_file), "marker": "M2", "block": "applied-via-button"},
        ttl_minutes=15,
    )
    res_apply = handle_qq_button_action(sandbox_db, f"siming:apply:{ap2['token']}")
    assert res_apply["ok"] is True
    assert target_file.is_file()
    assert "applied-via-button" in target_file.read_text(encoding="utf-8")


def test_bundle_and_multi_approval_keyboards(sandbox_db: SimingDB) -> None:
    # 捆绑包 -> 整包确认/取消按钮
    b1 = create_approval(
        sandbox_db,
        action="shift_expectation",
        target_id="EXP-1",
        params={"exp_id": "EXP-1", "title": "林内安装", "new_target_at": "2026-12-11T10:00:00+08:00"},
        bundle_id="BND-TEST1234",
        ttl_minutes=30,
    )
    b2 = create_approval(
        sandbox_db,
        action="shift_expectation",
        target_id="EXP-2",
        params={"exp_id": "EXP-2", "title": "电视挂装", "new_target_at": "2026-12-11T14:00:00+08:00"},
        bundle_id="BND-TEST1234",
        ttl_minutes=30,
    )
    kb_b = infer_keyboard_for_message(sandbox_db, f"事件链重排就绪（apply {b1['token']}，包 BND-TEST1234）")
    assert kb_b is not None
    b_btns = kb_b["content"]["rows"][0]["buttons"]
    assert b_btns[0]["action"]["data"] == "siming:apply_bundle:BND-TEST1234"
    assert b_btns[1]["action"]["data"] == "siming:cancel_bundle:BND-TEST1234"

    # 点击整包取消 -> 两张同时 CANCELLED
    c_res = handle_qq_button_action(sandbox_db, "siming:cancel_bundle:BND-TEST1234")
    assert c_res["ok"] is True
    assert b1["token"] in c_res["reply"] and b2["token"] in c_res["reply"]

    # 两张独立卡 -> 多卡消歧键盘
    a1 = create_approval(sandbox_db, action="didi_create_order", target_id="DIDI-1", params={"x": 1}, ttl_minutes=15)
    a2 = create_approval(sandbox_db, action="jd_add_cart", target_id="JD-1", params={"sku": "123"}, ttl_minutes=15)
    kb_m = infer_keyboard_for_message(
        sandbox_db,
        f"当前同时有 2 张待确认卡（`{a1['token']}`(didi) / `{a2['token']}`(jd)）",
    )
    assert kb_m is not None
    flat_data = [b["action"]["data"] for row in kb_m["content"]["rows"] for b in row["buttons"]]
    assert f"siming:apply:{a1['token']}" in flat_data
    assert f"siming:apply:{a2['token']}" in flat_data
    assert f"siming:cancel:{a1['token']}" in flat_data
    assert f"siming:cancel:{a2['token']}" in flat_data


def test_meituan_recommend_and_speculative_plan_keyboards(sandbox_db: SimingDB) -> None:
    rec_text = (
        "🍽️ 附近推荐：\n"
        "1. **示例鲜果切（学院店）** — 人均¥20\n"
        "2. **优果逸品（清华店）** — 人均¥26\n"
        "👉 回 `看看方案` 看各家推演好的具体菜品；回 `第1家` 看确认卡；或直接回 `确认第1家`！"
    )
    kb_rec = infer_keyboard_for_message(sandbox_db, rec_text)
    assert kb_rec is not None
    flat_rec = [b["action"]["data"] for row in kb_rec["content"]["rows"] for b in row["buttons"]]
    assert "siming:cmd:第1家" in flat_rec
    assert "siming:cmd:第2家" in flat_rec
    assert "siming:cmd:看看方案" in flat_rec

    plan_text = (
        "⚡ **美团投机推演菜品与券后价预案**（定位：**家** · 已就绪）\n"
        "1. ✅ **示例鲜果切（学院店）**\n"
        "2. ✅ **优果逸品（清华店）**\n"
        "👉 直接回 `确认第1家` 立刻自动加购"
    )
    kb_plan = infer_keyboard_for_message(sandbox_db, plan_text)
    assert kb_plan is not None
    flat_plan = [b["action"]["data"] for row in kb_plan["content"]["rows"] for b in row["buttons"]]
    assert "siming:cmd:确认第1家" in flat_plan
    assert "siming:cmd:确认第2家" in flat_plan


def test_case_curiosity_and_allowlist_security(sandbox_db: SimingDB) -> None:
    # 1. 普通纯文本通知不应强行挂载无意义按钮
    assert infer_keyboard_for_message(sandbox_db, "📦 今日状态正常，无异常事件。") is None

    # 2. 主动求证问题 -> 忽略按钮 + 点击回调关闭问题
    qid = enqueue_curiosity_question(
        sandbox_db,
        topic="place_dwell",
        question="停留了 40 分钟，临时路过回 忽略，记住地点 <名称>",
        context={"lat": 39.9, "lng": 116.3},
        dedup_key="dwell:test1",
    )
    assert qid is not None
    kb_q = infer_keyboard_for_message(sandbox_db, "停留了 40 分钟", ref_id=qid, rule_id="curiosity_ask")
    assert kb_q is not None
    ign_data = kb_q["content"]["rows"][0]["buttons"][0]["action"]["data"]
    assert ign_data == f"siming:curiosity_ignore:{qid}"
    ign_res = handle_qq_button_action(sandbox_db, ign_data)
    assert ign_res["ok"] is True
    assert "已忽略" in ign_res["reply"]

    # 3. 越权按钮命令必须拦截
    bad_cmd = handle_qq_button_action(sandbox_db, "siming:cmd:派单 siming: rm -rf /")
    assert bad_cmd["ok"] is False
    assert "白名单" in bad_cmd["reply"]

    bad_act = handle_qq_button_action(sandbox_db, "siming:case_act:S01:rm -rf")
    assert bad_act["ok"] is False

    # 4. send_qq_message 在有审批卡时走键盘分支并标记 has_keyboard=True（dry-run）
    ap = create_approval(sandbox_db, action="didi_create_order", target_id="DIDI-2", params={"a": 1}, ttl_minutes=15)
    res = send_qq_message(
        sandbox_db,
        f"🚕 滴滴确认卡 ready，回复 apply {ap['token']} 发单。",
        ref_id="DIDI-2",
        rule_id="didi_order",
        bypass_quiet=True,
    )
    assert res["ok"] is True
    assert res["has_keyboard"] is True


def test_siming_bridge_post_admission_sends_native_keyboard(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_py = Path(__file__).resolve().parents[1] / "integrations" / "hermes-plugin" / "__init__.py"
    spec = importlib.util.spec_from_file_location("siming_bridge_kb_test", plugin_py)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    sent_cards: list[tuple[str, str, dict, str | None]] = []

    class FakeAdapter:
        async def send_with_keyboard(self, chat_id, content, keyboard, reply_to=None):
            sent_cards.append((chat_id, content, keyboard.to_dict(), reply_to))

            class _R:
                success = True
                message_id = "m-kb-1"
                error = ""

            return _R()

    kb = build_approval_card_keyboard("A99", action="meituan_submit_pay")
    assert kb is not None

    captured_timeouts: list[float] = []

    async def fake_cli(_args, timeout_sec=25.0, stdin_bytes=None):
        captured_timeouts.append(float(timeout_sec))
        return 0, json.dumps({"handled": True, "reply": "付款确认卡 apply A99", "keyboard": kb}, ensure_ascii=False)

    monkeypatch.setattr(mod, "_run_siming_cli", fake_cli)
    monkeypatch.setattr(mod._QQ_INTERACTIVE, "_ACTIVE_QQ_ADAPTER", FakeAdapter())

    res = asyncio.run(
        mod._on_post_gateway_admission(
            platform="qqbot",
            text="点外卖 西瓜",
            message_id="msg-123",
            source={"chat_id": "USER_OPENID_1"},
        )
    )
    # 卡片已由 QQAdapter.send_with_keyboard 直接发送，返回 action=handled 且不带重复 reply
    assert res == {"action": "handled"}
    assert len(sent_cards) == 1
    assert sent_cards[0][0] == "USER_OPENID_1"
    assert sent_cards[0][3] == "msg-123"
    assert sent_cards[0][2]["content"]["rows"][0]["buttons"][0]["action"]["data"] == "siming:apply:A99"
    assert captured_timeouts[0] >= 180.0

    # 确认支付口令也必须获得长超时，防止在 Hermes 机密支付途中被提前强杀
    asyncio.run(
        mod._on_post_gateway_admission(
            platform="qqbot",
            text="确认支付",
            message_id="msg-124",
            source={"chat_id": "USER_OPENID_1"},
        )
    )
    assert captured_timeouts[-1] >= 180.0


def test_group_id_normalization_satisfies_qq_api_constraints() -> None:
    """QQ Bot API 对 rows.buttons.group_id 有严格长度与字符集限制（超长或含 - / : 会触发 400 整卡降级）。"""
    import re
    from siming.qq_cards import _normalize_group_id, _sanitize_keyboard_payload, make_button, make_keyboard
    from siming.replenish_cart import build_replenish_reminder_keyboard

    safe_re = re.compile(r"^[A-Za-z0-9_]{1,12}$")

    # 1. 合法短 group_id 原样保留
    assert _normalize_group_id("sm_act_S61") == "sm_act_S61"
    assert _normalize_group_id("sm_app_A25") == "sm_app_A25"
    assert _normalize_group_id("default") == "default"

    # 2. 超长或含非法字符（- / :）确定性映射为 <=12 字符的安全标识，且同组保持一致、异组互不冲突
    g_rpl_1 = _normalize_group_id("sm_rpl_rpl_bottled_water")
    g_rpl_2 = _normalize_group_id("sm_rpl_rpl_bottled_water")
    g_meal = _normalize_group_id("meal_2026-11-23_dinner")
    g_sub = _normalize_group_id("sub_SUB-7667efb4ef")
    g_brief = _normalize_group_id("sm_act_brief:2026-11-23")
    for g in (g_rpl_1, g_meal, g_sub, g_brief):
        assert safe_re.fullmatch(g), f"invalid normalized group_id: {g}"
    assert g_rpl_1 == g_rpl_2
    assert len({g_rpl_1, g_meal, g_sub, g_brief}) == 4

    # 3. 补货卡片按钮全部满足 QQ API 约束且同卡互斥
    kb = build_replenish_reminder_keyboard({"item_id": "rpl_bottled_water", "channel": "meituan"})
    assert kb is not None
    gids = [b["group_id"] for row in kb["content"]["rows"] for b in row["buttons"]]
    assert len(gids) == 4
    assert len(set(gids)) == 1
    assert safe_re.fullmatch(gids[0])

    # 4. 手写/历史脏字典在发送层也会被净化
    dirty_kb = {
        "content": {
            "rows": [
                {
                    "buttons": [
                        {
                            "id": "b1",
                            "render_data": {"label": "测试", "visited_label": "已点", "style": 1},
                            "action": {"type": 1, "data": "siming:noop", "permission": {"type": 2}, "click_limit": 1},
                            "group_id": "continue_or_cancel_very_long-id:1",
                        }
                    ]
                }
            ]
        }
    }
    cleaned = _sanitize_keyboard_payload(dirty_kb)
    assert safe_re.fullmatch(cleaned["content"]["rows"][0]["buttons"][0]["group_id"])
    assert make_button("x", "测试", "siming:noop", group_id="q-uuid-1234567890")["group_id"]
    assert safe_re.fullmatch(make_button("x", "测试", "siming:noop", group_id="q-uuid-1234567890")["group_id"])
    assert make_keyboard([[make_button("x", "测试", "siming:noop", group_id="q-uuid-1234567890")]]) is not None
