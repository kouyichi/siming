"""QQ 点餐 → 历史整单按钮复购 → 必选品/预览/红包 → 点击确认支付全链路回归测试。"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from siming.approval import create_approval
from siming.db import SimingDB, iso_now
from siming.meituan_agent import (
    list_meituan_history_orders,
    match_history_order_for_shop,
    stage_meituan_history_reorder,
)
from siming.meituan_cart_live import ensure_mindex_required_products
from siming.meituan_pay import (
    _run_hermes_confidential_payment_handoff,
    classify_cashier_or_order_state,
    extract_cashier_payable_yuan,
    parse_meituan_preview_bill,
    stage_meituan_payment_preview,
)
from siming.meituan_speculate import load_shop_historical_dish_combos
from siming.modules import set_module_enabled
from siming.qq_cards import handle_qq_button_action, infer_keyboard_for_message
from siming.router import handle_inbound_qq


LIVE_CASHIER_COMBO_BALANCE_TXT = """支付订单
交易剩余时间 14:53
¥
90.40
南京大牌档(中关村店)-800000000000000002
支付方式
美团支付
中国光大银行储蓄卡
(1002)
中国建设银行储蓄卡
(1004)
余额 (可用¥9.66)
可用余额不足，需组合支付
可组合支付
换卡支付，支持境内境外卡
微信支付
支付宝支付
确认支付
"""

LIVE_NJ_PREVIEW_TXT = """提交订单
美团配送
到店自取
示例写字楼 10层
张三
\xa0先生13800138000
订单距离较远，送达时间可能波动
立即送出
(大约19:49送达)
南京大牌档(中关村店)
蟹粉火鸭烩粉皮（口味原料升级）
¥48
标准
x1
优质五常大米饭（口感升级）
¥3
标准
x1
豉椒鸭肫拌鸭肠
¥28
标准
x1
不需要餐具
¥0
x1
打包费
¥4
配送费
¥7.1
美团红包
-¥30
商家代金券
暂无可用
优惠规则
小计¥60.1
合计¥60.1
提交订单
"""


def _seed_history_orders(db: SimingDB) -> None:
    ts = iso_now()
    rows = [
        # 最新一笔：药房/口罩（列正餐历史菜单时应自动排除）
        ("k-mask", "乐购达（五道口店）", "2026-11-22T14:56:00+08:00", "已完成", 1880, "医用外科口罩 50只", 1),
        # 取消单（应排除）
        ("k-can", "某已取消门店", "2026-11-21T20:00:00+08:00", "已取消", 5000, "红烧肉", 1),
        # 南京大牌档（含 11-18 三菜 + 11-17 含活动说明诱饵项）
        ("k-nj1", "南京大牌档(中关村店)", "2026-11-18T19:46:00+08:00", "已完成", 9230, "蟹粉火鸭烩粉皮（口味原料升级）", 1),
        ("k-nj2", "南京大牌档(中关村店)", "2026-11-18T19:46:00+08:00", "已完成", 9230, "优质五常大米饭（口感升级）", 1),
        ("k-nj3", "南京大牌档(中关村店)", "2026-11-18T19:46:00+08:00", "已完成", 9230, "豉椒鸭肫拌鸭肠", 1),
        ("k-nj4", "南京大牌档(中关村店)", "2026-11-18T19:46:00+08:00", "已完成", 9230, "点击右上角♥，收藏领券立减1元", 1),
        ("k-nj5", "南京大牌档(中关村店)", "2026-11-18T19:46:00+08:00", "已完成", 9230, "需要餐具（需要几份点几份）", 1),
        # 素坤泰
        ("k-sk1", "素坤泰(泰式打抛饭)", "2026-11-16T18:10:00+08:00", "已完成", 8180, "招牌打抛饭🌶️（鸡肉）", 1),
        ("k-sk2", "素坤泰(泰式打抛饭)", "2026-11-16T18:10:00+08:00", "已完成", 8180, "舂荷包蛋", 1),
    ]
    with db.transaction() as conn:
        for ikey, shop, occ, st, cents, item, qty in rows:
            conn.execute(
                """
                INSERT INTO shop_order_items (
                    item_key, platform, shop, ordered_at, order_status, order_amount_cents, item_name, qty, created_at
                ) VALUES (?, 'meituan', ?, ?, ?, ?, ?, ?, ?)
                """,
                (ikey, shop, occ, st, cents, item, qty, ts),
            )


def test_cashier_combo_balance_hint_is_not_pay_failed() -> None:
    """余额行「可用余额不足，需组合支付」绝不能误判成 PAY_FAILED 阻断储蓄卡/机密支付通道！"""
    url = "https://mpay.meituan.com/resource/icashier/s/index.html?tradeno=26112311100301670002013601890103#/"
    cls = classify_cashier_or_order_state(url, title="支付订单", text=LIVE_CASHIER_COMBO_BALANCE_TXT)
    assert cls["state"] == "CASHIER_READY"
    assert extract_cashier_payable_yuan(LIVE_CASHIER_COMBO_BALANCE_TXT) == 90.4

    # 真实扣款失败/密码错误/银行卡余额不足仍必须严格识别为 PAY_FAILED
    assert classify_cashier_or_order_state(url, text="支付失败\n银行卡余额不足")["state"] == "PAY_FAILED"
    assert classify_cashier_or_order_state(url, text="支付订单\n密码错误，请重试")["state"] == "PAY_FAILED"
    assert classify_cashier_or_order_state(url, text="支付订单\n扣款失败")["state"] == "PAY_FAILED"
    assert classify_cashier_or_order_state(url, text="支付订单\n余额不足")["state"] == "PAY_FAILED"


def test_hermes_handoff_uses_query_file_not_cmdline_argv(sandbox_db: SimingDB, tmp_path: Path) -> None:
    fake_instr = tmp_path / "fake_payment_guide.md"
    fake_instr.write_text("说明内容\n", encoding="utf-8")
    fake_hermes = tmp_path / "hermes"
    fake_hermes.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_hermes.chmod(0o755)

    policy = {
        "channels": {"hermes_bin": str(fake_hermes)},
        "meituan_pay": {
            "hermes_handoff": {
                "enabled": True,
                "provider": "RedPill",
                "model": "phala/deepseek-v4.1-flash-1017-uncensored",
                "instruction_file": str(fake_instr),
                "max_turns": 15,
                "timeout_sec": 90,
            }
        },
    }
    captured_cmds: list[str] = []

    class _Proc:
        returncode = 0
        stdout = b"ok"
        stderr = b""

    def _fake_run(cmd: list[str], **kwargs: Any) -> _Proc:
        captured_cmds.append(cmd[-1])
        return _Proc()

    res = _run_hermes_confidential_payment_handoff(
        {
            "target_id": "TID-1",
            "cashier_url": "https://mpay.meituan.com/resource/icashier/s/index.html?tradeno=1",
            "shop": "南京大牌档(中关村店)",
            "item_spec": "粉皮",
            "order_id": "800000000000000002",
            "verified_yuan": 60.1,
            "cashier_yuan": 60.1,
            "max_yuan": 120.0,
            "delivery_address": "示例写字楼 10层",
            "eta_text": "大约19:49送达",
            "cashier_text_head": "支付订单\n¥\n60.10\n",
        },
        sandbox_db,
        policy,
        subprocess_run_fn=_fake_run,
        verify_cashier_fn=lambda _t, _p: {"ok": True, "paid": True, "state": "PAID", "payable_yuan": 60.1, "url": "https://mpay.meituan.com/ok"},
        verify_olist_fn=lambda _s, _o, _p: {"ok": True, "found": True, "paid": True, "shop": "南京大牌档(中关村店)", "status": "商家已接单"},
    )
    assert res["paid"] is True
    assert captured_cmds
    assert '--query-file "$SIMING_MT_COMBINED"' in captured_cmds[0]
    assert '-q "$(cat' not in captured_cmds[0]


def test_history_orders_filter_decoys_typo_reorder_and_non_food_guard(sandbox_db: SimingDB) -> None:
    _seed_history_orders(sandbox_db)
    set_module_enabled(sandbox_db, "meituan_agent", True)

    # 1) 历史常点过滤「点击右上角♥，收藏领券立减1元」与「需要餐具」
    hist_nj = load_shop_historical_dish_combos(sandbox_db, "南京大牌档(中关村店)")
    dish_names = [d["name"] for d in hist_nj["last_combo"]]
    assert "蟹粉火鸭烩粉皮（口味原料升级）" in dish_names
    assert "优质五常大米饭（口感升级）" in dish_names
    assert "豉椒鸭肫拌鸭肠" in dish_names
    assert not any("收藏领券" in n or "餐具" in n for n in dish_names)

    # 2) QQ 说「找一个我之前买过的菜单吧 不要新的」-> 返回带原生按钮的历史整单列表
    r_list = handle_inbound_qq(sandbox_db, "找一个我之前买过的菜单吧 不要新的")
    assert r_list["handled"] is True
    assert "美团历史常点整单" in r_list["reply"]
    assert "南京大牌档(中关村店)" in r_list["reply"]
    assert "素坤泰(泰式打抛饭)" in r_list["reply"]
    assert "收藏领券" not in r_list["reply"]
    assert "乐购达" not in r_list["reply"]
    assert "某已取消门店" not in r_list["reply"]
    kb = r_list.get("keyboard")
    assert kb is not None
    flat_data = [b["action"]["data"] for row in kb["content"]["rows"] for b in row["buttons"]]
    assert "siming:cmd:复购第1单" in flat_data
    assert "siming:cmd:复购第2单" in flat_data

    # 3) 错别字容错匹配：「南京大排档」->「南京大牌档(中关村店)」
    hit = match_history_order_for_shop(sandbox_db, "南京大排档")
    assert hit is not None
    assert hit["shop"] == "南京大牌档(中关村店)"
    assert len(hit["dishes"]) == 3

    # 4) 防误劫持：非外卖短语「上一个版本」「上次那个会议」绝不命中
    assert handle_inbound_qq(sandbox_db, "上一个版本有什么改动")["handled"] is False
    assert handle_inbound_qq(sandbox_db, "上次那个会议纪要在哪")["handled"] is False


def test_full_qq_click_history_to_click_confirm_pay_flow(
    sandbox_db: SimingDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """端到端模拟 QQ 点击复购按钮 → 签发付款确认卡（带按钮）→ 点击确认支付按钮 → 返回支付成功回执。"""
    _seed_history_orders(sandbox_db)
    set_module_enabled(sandbox_db, "meituan_agent", True)

    # Step 1: 用户发「找一个我之前买过的菜单吧 不要新的」获取可点按钮卡片
    r1 = handle_inbound_qq(sandbox_db, "找一个我之前买过的菜单吧 不要新的")
    assert r1["handled"] is True and r1.get("keyboard") is not None

    # Step 2: 模拟在线加购并进入预览页，签发真实 meituan_submit_pay 付款确认卡
    nj_bill = parse_meituan_preview_bill(LIVE_NJ_PREVIEW_TXT)
    assert nj_bill["final_payable_yuan"] == 60.1
    assert len(nj_bill["items"]) == 4  # 展开更多后包含 3 道菜 + 不需要餐具

    captured_stage_args: dict[str, Any] = {}

    def _fake_stage_order(
        db: SimingDB,
        order_request: str,
        cdp_probe_fn: Any = None,
        auto_preview_pay: bool = True,
        location_override: str = "",
        explicit_shop: str = "",
        explicit_dishes: Any = None,
    ) -> dict[str, Any]:
        captured_stage_args.update(
            {
                "order_request": order_request,
                "location_override": location_override,
                "explicit_shop": explicit_shop,
                "explicit_dishes": explicit_dishes,
            }
        )
        return stage_meituan_payment_preview(
            db,
            cart_res={"ok": True, "added_to_cart": True, "target_id": "TID-NJ-PREVIEW", "menu_url": "https://h5.waimai.meituan.com/waimai/mindex/menu?poi_id_str=Q2Nd"},
            shop=explicit_shop,
            item_spec="蟹粉火鸭烩粉皮 + 优质五常大米饭 + 豉椒鸭肫拌鸭肠",
            deeplink="imeituan://nj",
            location_override=location_override,
            preview_fn=lambda _d, t, _m: {
                "ok": True,
                "preview_ready": True,
                "preview_target_id": t,
                "bill": nj_bill,
                "final_payable_yuan": 60.1,
            },
        )

    monkeypatch.setattr("siming.meituan_agent.stage_meituan_h5_order", _fake_stage_order)

    # 用户点击 QQ 历史整单按钮 [🔁 1.南京大牌档]
    r2 = handle_qq_button_action(sandbox_db, "siming:cmd:复购第1单")
    assert r2["ok"] is True
    assert captured_stage_args["explicit_shop"] == "南京大牌档(中关村店)"
    assert len(captured_stage_args["explicit_dishes"]) == 3
    assert "付款许可确认卡" in r2["reply"]
    assert "¥60.1" in r2["reply"]
    pay_kb = r2.get("keyboard")
    assert pay_kb is not None
    pay_btn = pay_kb["content"]["rows"][0]["buttons"][0]
    assert pay_btn["action"]["data"].startswith("siming:apply:A")
    tok = pay_btn["action"]["data"].split(":")[-1]

    # Step 3: 用户点击 QQ 按钮 [💳 确认支付 Axx] -> 执行提交 + 机密支付 -> 返回支付成功回执
    paid_calls: list[dict[str, Any]] = []
    monkeypatch.setattr(
        "siming.meituan_pay._live_submit_and_pay_meituan_order",
        lambda p, _d, port=9222: (
            paid_calls.append(p)
            or {
                "ok": True,
                "paid": True,
                "order_submitted": True,
                "state": "PAID",
                "order_id": "800000000000000002",
                "paid_yuan": 60.1,
                "shop": p["shop"],
                "order_status": "商家已接单",
                "delivery_address": p["delivery_address"],
                "eta_text": p["eta_text"],
                "pay_channel": "hermes_confidential_handoff",
            }
        ),
    )

    r3 = handle_qq_button_action(sandbox_db, f"siming:apply:{tok}")
    assert r3["ok"] is True
    assert "美团外卖已下单并支付成功" in r3["reply"]
    assert "¥60.1" in r3["reply"]
    assert "800000000000000002" in r3["reply"]
    assert len(paid_calls) == 1

    # 同理测试口语「上一个南京大排档 送到公司」直达付款确认卡 + 按钮
    r_oral = handle_inbound_qq(sandbox_db, "上一个南京大排档 送到公司")
    assert r_oral["handled"] is True
    assert captured_stage_args["location_override"] == "公司"
    assert captured_stage_args["explicit_shop"] == "南京大牌档(中关村店)"
    assert r_oral.get("keyboard") is not None


def test_ensure_mindex_required_products_handles_required_tag_info(monkeypatch: pytest.MonkeyPatch) -> None:
    """验证必选品处理在检测到 required_product_info.has_required_product=false 时自动切分类、加选并核验。"""
    calls: list[str] = []

    class _FakeWs:
        def __init__(self) -> None:
            self.step = 0

        def call(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
            if method == "Runtime.evaluate":
                expr = str(params.get("expression") or "")
                calls.append(expr[:80])
                if "required_product_info" in expr:
                    # 首次调用报缺失必选品；补完后报已满足
                    self.step += 1
                    if self.step == 1:
                        return {
                            "result": {
                                "value": json.dumps(
                                    {
                                        "hasComp": True,
                                        "needsRequired": True,
                                        "hasRequired": False,
                                        "requiredTagId": "376941227",
                                        "catLinkCls": "link-13",
                                        "sidebarHasReq": True,
                                    }
                                )
                            }
                        }
                    return {
                        "result": {
                            "value": json.dumps(
                                {
                                    "hasComp": True,
                                    "needsRequired": False,
                                    "hasRequired": True,
                                    "requiredTagId": "376941227",
                                    "catLinkCls": "link-13",
                                    "sidebarHasReq": True,
                                }
                            )
                        }
                    }
                if "byCls" in expr:
                    return {"result": {"value": True}}
                if "FREE_NO_UTENSIL_RE" in expr:
                    return {
                        "result": {
                            "value": json.dumps(
                                {"ok": True, "via": "dom", "title": "不需要餐具", "price": 0, "inSafeView": True, "x": 350, "y": 500}
                            )
                        }
                    }
            return {"result": {}}

    monkeypatch.setattr("siming.meituan_cart_live._wait", lambda _s: None)
    fake = _FakeWs()
    assert ensure_mindex_required_products(fake) is True  # type: ignore[arg-type]
    assert fake.step == 2


def test_siming_bridge_reorder_progress_and_300s_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_dir = Path(__file__).resolve().parents[1] / "integrations" / "hermes-plugin"
    spec_q = importlib.util.spec_from_file_location("siming_qq_int_flow_test", plugin_dir / "qq_interactive.py")
    assert spec_q and spec_q.loader
    qmod = importlib.util.module_from_spec(spec_q)
    spec_q.loader.exec_module(qmod)

    sent_plain: list[str] = []
    sent_kb: list[tuple[str, dict]] = []
    seen_timeouts: list[float] = []

    class _FakeAdapter:
        def _is_dm_intake_allowed(self, _op: str) -> bool:
            return True

        async def send(self, chat_id: str, content: str, metadata: Any = None) -> Any:
            sent_plain.append(content)
            return type("_R", (), {"success": True})()

        async def send_with_keyboard(self, chat_id: str, content: str, keyboard: Any, reply_to: Any = None) -> Any:
            sent_kb.append((content, keyboard.to_dict()))
            return type("_R", (), {"success": True})()

    async def _fake_cli(args: list[str], timeout_sec: float = 60.0, stdin_bytes: Any = None) -> tuple[int, str]:
        seen_timeouts.append(float(timeout_sec))
        return 0, json.dumps({"ok": True, "reply": "付款许可确认卡 apply A88", "keyboard": {"content": {"rows": []}}}, ensure_ascii=False)

    monkeypatch.setattr(qmod, "_RUN_SIMING_CLI_FN", _fake_cli)
    ev = type("_Ev", (), {"id": "ev-flow-1", "scene": "c2c", "chat_type": 2, "operator_openid": "U1", "user_openid": "U1"})()
    asyncio.run(qmod._handle_siming_interaction(_FakeAdapter(), ev, "siming:cmd:复购第1单"))

    assert seen_timeouts == [300.0]
    assert any("正在为你后台加购" in m for m in sent_plain)
    assert any("付款许可确认卡" in m for m, _k in sent_kb) or any("付款许可确认卡" in m for m in sent_plain)


def test_natural_reorder_variants_and_async_hook_handoff_beats_30s_limit(
    sandbox_db: SimingDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归：
    1) 「给我点上一次的南京大排档」「上一个南京大排档 送到银海」均由司命确定性命中历史复购；
    2) siming-bridge 的 post_gateway_admission 遇到 >8s 的真实加购/支付长任务时，抢在网关 30s 钩子限时前返回 handled，
       立即向 QQ 发送进度提示并在后台完成后推送带原生 [💳 确认支付 Axx] 按钮的确认卡，绝不漏给对话模型。
    """
    _seed_history_orders(sandbox_db)
    set_module_enabled(sandbox_db, "meituan_agent", True)

    seen_reorders: list[tuple[str, str]] = []

    def _fake_reorder(db: SimingDB, target: str, location_override: str = "", **_kw: Any) -> dict[str, Any]:
        seen_reorders.append((target, location_override))
        return {"ok": True, "reply": f"REORDER:{target}:{location_override}"}

    monkeypatch.setattr("siming.meituan_agent.stage_meituan_history_reorder", _fake_reorder)

    r_a = handle_inbound_qq(sandbox_db, "给我点上一次的南京大排档")
    assert r_a["handled"] is True
    assert r_a["reply"] == "REORDER:南京大排档:"

    r_b = handle_inbound_qq(sandbox_db, "上一个南京大排档 送到银海")
    assert r_b["handled"] is True
    assert r_b["reply"] == "REORDER:南京大排档:银海"

    # 测试 siming-bridge 超过 _HOOK_SYNC_WAIT_SEC 时自动转后台推送而不触发网关 30s 超时
    plugin_dir = Path(__file__).resolve().parents[1] / "integrations" / "hermes-plugin"
    spec_p = importlib.util.spec_from_file_location("siming_bridge_async_handoff_test", plugin_dir / "__init__.py")
    assert spec_p and spec_p.loader
    pmod = importlib.util.module_from_spec(spec_p)
    spec_p.loader.exec_module(pmod)

    sent_plain: list[str] = []
    sent_kb: list[tuple[str, dict]] = []

    class _FakeQQAdapter:
        async def send(self, chat_id: str, content: str, metadata: Any = None) -> Any:
            sent_plain.append(content)
            return type("_R", (), {"success": True})()

        async def send_with_keyboard(self, chat_id: str, content: str, keyboard: Any, reply_to: Any = None) -> Any:
            sent_kb.append((content, keyboard.to_dict()))
            return type("_R", (), {"success": True})()

    fake_adapter = _FakeQQAdapter()
    monkeypatch.setattr(pmod, "_HOOK_SYNC_WAIT_SEC", 0.05)
    monkeypatch.setattr(pmod._QQ_INTERACTIVE, "_ACTIVE_QQ_ADAPTER", fake_adapter)

    async def _slow_cli(args: list[str], timeout_sec: float = 60.0, stdin_bytes: Any = None) -> tuple[int, str]:
        assert timeout_sec == 300.0
        await asyncio.sleep(0.15)
        return 0, json.dumps(
            {
                "handled": True,
                "reply": "💳 **美团外卖付款许可确认卡** [`A99`]",
                "keyboard": {
                    "content": {
                        "rows": [
                            {"buttons": [{"id": "confirm", "action": {"type": 1, "data": "siming:apply:A99"}}]}
                        ]
                    }
                },
            },
            ensure_ascii=False,
        )

    async def _fast_unhandled_cli(args: list[str], timeout_sec: float = 60.0, stdin_bytes: Any = None) -> tuple[int, str]:
        return 0, json.dumps({"handled": False}, ensure_ascii=False)

    async def _run_cases() -> None:
        # 1) 慢速真实加购：0.05s 即返回 handled，后台 0.15s 后把带按钮确认卡推到 QQ
        monkeypatch.setattr(pmod, "_run_siming_cli", _slow_cli)
        ret = await pmod._on_post_gateway_admission(
            platform="qqbot",
            text="上一个南京大排档 送到公司",
            message_id="msg-slow-1",
            source={"chat_id": "CHAT-1"},
        )
        assert ret == {"action": "handled"}
        assert any("正在后台为你实时加购" in m for m in sent_plain)
        assert len(pmod._BACKGROUND_TASKS) == 1
        await asyncio.gather(*list(pmod._BACKGROUND_TASKS))
        assert len(sent_kb) == 1
        assert "A99" in sent_kb[0][0]
        assert sent_kb[0][1]["content"]["rows"][0]["buttons"][0]["action"]["data"] == "siming:apply:A99"

        # 2) 普通非司命闲聊：快速返回 None 交给常规对话，绝不误劫持
        monkeypatch.setattr(pmod, "_run_siming_cli", _fast_unhandled_cli)
        ret2 = await pmod._on_post_gateway_admission(
            platform="qqbot",
            text="上一个版本有什么改动",
            message_id="msg-fast-2",
            source={"chat_id": "CHAT-1"},
        )
        assert ret2 is None

    asyncio.run(_run_cases())


def test_20261124_incident_recommend_and_reorder_with_colloquial_add_dish(
    sandbox_db: SimingDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """回归 2026-11-24 18:12-18:24 QQ 真实点餐失败事故：
    1) 「推荐点晚上吃啥」不再漏进 LLM，正确解析为晚餐无关键词推荐并挂 QQ 按钮；
    2) 「南京大排档吧 昨天的加上芫荽那个凉菜」不再漏进 LLM/手搓脚本，而是直达历史复购 + 同店历史口语菜名解析（「芫荽那个凉菜」→「御膳芫炝羊肚」）并签发带按钮的付款确认卡；
    3) 同步验证增删菜、累加份数、同义词（香菜→芫荽）、防误劫持（吐槽/非店名）以及 CLI `--reorder --add --exclude`。
    """
    from siming.meituan_agent import _parse_meal_hint, resolve_shop_history_dish_alias
    from siming.meituan_cart_live import _dish_match_score, _dish_search_keywords

    _seed_history_orders(sandbox_db)
    # 补充用户此前在南京大牌档买过「御膳芫炝羊肚」的历史记录（对应真实库 11-05 / 11-02）
    ts = iso_now()
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO shop_order_items (
                item_key, platform, shop, ordered_at, order_status, order_amount_cents, item_name, qty, created_at
            ) VALUES
            ('k-nj-yd1', 'meituan', '南京大牌档(中关村店)', '2026-11-05T17:52:00+08:00', '已完成', 6300, '御膳芫炝羊肚', 1, ?),
            ('k-nj-jj1', 'meituan', '南京大牌档(中关村店)', '2026-11-02T18:48:00+08:00', '已完成', 5800, '金牌煎饺（个大馅满）', 1, ?)
            """,
            (ts, ts),
        )
    set_module_enabled(sandbox_db, "meituan_agent", True)

    # 1) _parse_meal_hint 必须把「推荐点晚上吃啥」干净解析为 slot=晚餐、keyword=""（绝不能把「晚上吃」当搜店关键词！）
    from datetime import datetime
    from siming.db import SHANGHAI_TZ

    now_1812 = datetime(2026, 11, 24, 18, 12, tzinfo=SHANGHAI_TZ)
    ph1 = _parse_meal_hint("推荐点晚上吃啥", now_1812)
    assert ph1["slot"] == "晚餐" and ph1["keyword"] == ""
    ph2 = _parse_meal_hint("推荐点晚上吃川菜", now_1812)
    assert ph2["slot"] == "晚餐" and ph2["keyword"] == "川菜"
    ph3 = _parse_meal_hint("推荐点中午吃面 预算40", now_1812)
    assert ph3["slot"] == "午餐" and ph3["keyword"] == "面" and ph3["max_budget"] == 40.0

    # 2) QQ 入站「推荐点晚上吃啥」被确定性接管并返回带按钮推荐卡
    rec_calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "siming.meituan_agent.recommend_meituan_meals",
        lambda db, hint="", location_override="", **_kw: (
            rec_calls.append((hint, location_override))
            or {
                "ok": True,
                "picks": [{"shop": "荆九爷爆炒桥头三嫩（五道口店）"}],
                "reply": (
                    "🍽️ **美团外卖推荐（晚餐）**\n"
                    "1. **荆九爷爆炒桥头三嫩（五道口店）** — 约 ¥52\n"
                    "2. **素坤泰(泰式打抛饭)** — 约 ¥27\n"
                    "👉 回 `看看方案` 看各家推演；或直接回 `确认第1家`！"
                ),
            }
        ),
    )
    for phrase in (
        "推荐点晚上吃啥",
        "帮我推荐点晚上吃的",
        "推荐点中午吃啥",
        "推荐点好吃的",
        "今天晚上吃啥好",
    ):
        r_rec = handle_inbound_qq(sandbox_db, phrase)
        assert r_rec["handled"] is True, f"推荐口语未接管: {phrase!r}"
        assert r_rec.get("keyboard") is not None, f"推荐回复未挂 QQ 按钮: {phrase!r}"
    assert rec_calls[0] == ("推荐点晚上吃啥", "")

    # 3) 历史口语菜名映射：「芫荽那个凉菜」&「香菜那个凉菜」→「御膳芫炝羊肚」
    assert resolve_shop_history_dish_alias(sandbox_db, "南京大牌档(中关村店)", "芫荽那个凉菜") == "御膳芫炝羊肚"
    assert resolve_shop_history_dish_alias(sandbox_db, "南京大牌档(中关村店)", "香菜那个凉菜") == "御膳芫炝羊肚"
    assert resolve_shop_history_dish_alias(sandbox_db, "南京大牌档(中关村店)", "煎饺") == "金牌煎饺（个大馅满）"
    # 未买过的陌生菜保持原样交由实时菜单搜索匹配
    assert resolve_shop_history_dish_alias(sandbox_db, "南京大牌档(中关村店)", "鸭血粉丝汤") == "鸭血粉丝汤"

    # 4) QQ 入站「南京大排档吧 昨天的加上芫荽那个凉菜」→ 4 道菜全齐 + 签发付款确认卡 + QQ 按钮
    captured_orders: list[dict[str, Any]] = []

    def _fake_stage_order(
        db: SimingDB,
        order_request: str,
        cdp_probe_fn: Any = None,
        auto_preview_pay: bool = True,
        location_override: str = "",
        explicit_shop: str = "",
        explicit_dishes: Any = None,
        **_kw: Any,
    ) -> dict[str, Any]:
        captured_orders.append(
            {
                "shop": explicit_shop,
                "dishes": list(explicit_dishes or []),
                "loc": location_override,
                "req": order_request,
            }
        )
        app = create_approval(
            db,
            action="meituan_submit_pay",
            target_id="MTPAY-南京大牌档中关村店",
            params={"shop": explicit_shop, "final_payable_yuan": 101.4, "delivery_address": "示例写字楼 10层"},
            ttl_minutes=10,
        )
        return {
            "ok": True,
            "approval_token": app["token"],
            "reply": f"💳 **美团外卖付款许可确认卡** [`{app['token']}`]\n• 实付：**¥101.4**\n👉 `apply {app['token']}`",
        }

    monkeypatch.setattr("siming.meituan_agent.stage_meituan_h5_order", _fake_stage_order)

    r_ord = handle_inbound_qq(sandbox_db, "南京大排档吧 昨天的加上芫荽那个凉菜")
    assert r_ord["handled"] is True
    assert "美团外卖付款许可确认卡" in r_ord["reply"]
    assert r_ord.get("keyboard") is not None
    kb_data = [b["action"]["data"] for row in r_ord["keyboard"]["content"]["rows"] for b in row["buttons"]]
    assert any(d.startswith("siming:apply:A") for d in kb_data)

    last_o = captured_orders[-1]
    assert last_o["shop"] == "南京大牌档(中关村店)"
    dish_map = {d["name"]: d["qty"] for d in last_o["dishes"]}
    assert dish_map == {
        "蟹粉火鸭烩粉皮（口味原料升级）": 1,
        "优质五常大米饭（口感升级）": 1,
        "豉椒鸭肫拌鸭肠": 1,
        "御膳芫炝羊肚": 1,
    }

    # 5) 多种词序 + 地点 + 多份加菜 + 减菜 + 序号复购加菜
    r_ord2 = handle_inbound_qq(sandbox_db, "昨天的南京大排档加上芫荽那个凉菜 送到公司")
    assert r_ord2["handled"] is True
    assert captured_orders[-1]["loc"] == "公司"
    assert any(d["name"] == "御膳芫炝羊肚" for d in captured_orders[-1]["dishes"])

    r_ord3 = handle_inbound_qq(sandbox_db, "把上次的南京大排档再点一次，多加两份米饭 不要鸭肠")
    assert r_ord3["handled"] is True
    dmap3 = {d["name"]: d["qty"] for d in captured_orders[-1]["dishes"]}
    assert dmap3.get("优质五常大米饭（口感升级）") == 3
    assert not any("鸭肠" in k for k in dmap3)

    r_ord4 = handle_inbound_qq(sandbox_db, "南京大排档 老样子加个煎饺和羊肚")
    assert r_ord4["handled"] is True
    dmap4 = {d["name"]: d["qty"] for d in captured_orders[-1]["dishes"]}
    assert "金牌煎饺（个大馅满）" in dmap4 and "御膳芫炝羊肚" in dmap4

    r_ord5 = handle_inbound_qq(sandbox_db, "南京大排档吧 昨天的")
    assert r_ord5["handled"] is True
    assert len(captured_orders[-1]["dishes"]) == 3

    r_ord6 = handle_inbound_qq(sandbox_db, "复购第1单 加一份羊肚 不要米饭")
    assert r_ord6["handled"] is True
    dmap6 = {d["name"]: d["qty"] for d in captured_orders[-1]["dishes"]}
    assert "御膳芫炝羊肚" in dmap6 and not any("米饭" in k for k in dmap6)

    # 6) 防误劫持：吐槽、非历史店名、医药/宠物问句绝不误命中
    for non_order in (
        "南京大排档 昨天的不好吃",
        "南京大排档 昨天的还没退款",
        "这个方案吧 昨天的加上新需求",
        "昨天的会议加上新议题",
        "感冒吃什么药",
        "猫吃什么",
    ):
        r_no = handle_inbound_qq(sandbox_db, non_order)
        assert r_no["handled"] is False, f"非点餐句被误劫持: {non_order!r} -> {r_no}"

    # 7) 实时菜单口语关键词与描述/分类兜底匹配（即便历史没买过该菜也能在 H5 菜单中命中）
    kws = _dish_search_keywords("芫荽那个凉菜")
    assert "芫荽" in kws[:2]
    assert "芫" in kws[:3]
    assert _dish_match_score(
        "御膳芫炝羊肚",
        "芫荽那个凉菜",
        card_lines=["御膳芫炝羊肚", "1人份", "羊肚配新鲜芫荽凉拌", "¥32"],
        category_name="烧卤冷味",
    ) >= 62.0
    assert _dish_match_score(
        "御膳芫炝羊肚",
        "香菜那个凉菜",
        card_lines=["御膳芫炝羊肚", "1人份", "羊肚配新鲜芫荽", "¥32"],
        category_name="烧卤冷味",
    ) >= 62.0
    assert _dish_match_score(
        "招牌烤鸭",
        "芫荽那个凉菜",
        card_lines=["招牌烤鸭", "1人份", "果木炭烤", "¥68"],
        category_name="招牌必点",
    ) < 62.0


def test_cli_meituan_reorder_add_and_exclude_flags(
    sandbox_db: SimingDB, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI `siming meituan --reorder 南京大排档 --add 芫荽那个凉菜 --exclude 米饭 --loc 公司` 正确透传增删菜。"""
    from siming.cli import main as cli_main

    captured: dict[str, Any] = {}

    def _fake_reorder(
        db: SimingDB,
        target: str,
        location_override: str = "",
        extra_dishes: Any = None,
        exclude_dishes: Any = None,
        **_kw: Any,
    ) -> dict[str, Any]:
        captured.update(
            {
                "target": target,
                "loc": location_override,
                "extra": extra_dishes,
                "exclude": exclude_dishes,
            }
        )
        return {"ok": True, "reply": "CLI_REORDER_OK"}

    monkeypatch.setattr("siming.cli.SimingDB", lambda *_a, **_kw: sandbox_db)
    monkeypatch.setattr("siming.meituan_agent.stage_meituan_history_reorder", _fake_reorder)

    rc = cli_main(
        [
            "meituan",
            "--reorder",
            "南京大排档",
            "--add",
            "芫荽那个凉菜,煎饺",
            "--exclude",
            "米饭",
            "--loc",
            "公司",
        ]
    )
    assert rc == 0
    assert "CLI_REORDER_OK" in capsys.readouterr().out
    assert captured["target"] == "南京大排档"
    assert captured["loc"] == "公司"
    assert [d["name"] for d in captured["extra"]] == ["芫荽那个凉菜", "煎饺"]
    assert captured["exclude"] == ["米饭"]
