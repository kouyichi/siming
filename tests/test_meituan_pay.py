"""美团外卖 H5 实付结算预览、安全防呆校验与许可制提交支付回归测试（全离线零真扣款）。"""

from __future__ import annotations

from typing import Any

import pytest

from siming.approval import create_approval, resolve_and_apply_natural_confirm, verify_and_apply_approval
from siming.db import SimingDB
from siming.meituan_pay import (
    cancel_meituan_pending_order,
    classify_cashier_or_order_state,
    execute_meituan_submit_pay,
    extract_cashier_payable_yuan,
    parse_meituan_preview_bill,
    stage_meituan_payment_preview,
    validate_pre_submit_guardrails,
)
from siming.meituan_speculate import (
    confirm_and_execute_speculative_pick,
    create_speculative_batch,
    parse_meituan_menu_cards,
    run_speculative_meal_preplan,
)
from siming.router import handle_inbound_qq

SAMPLE_PREVIEW_COTTI = """美团配送
到店自取
示例写字楼 10层
张三
先生 13900139000
立即送出
大约21:23送达
因天气、配送距离等原因，送达时间可能波动
库迪咖啡（北京大钟寺融寓店）
美团快送
苹果美式
¥19.01
大杯、冰、不另外加糖、默认奶油、默认
x1
打包费
¥1
配送费
¥0.4
美团红包
未选红包，最高5元可用
商家代金券
暂无可用
优惠说明
已优惠¥0
小计¥20.41
备注、餐具、发票
合计¥20.41
提交订单
"""

SAMPLE_PREVIEW_MULTI = """美团配送
示例写字楼 10层
张三 先生 13900139000
立即送出
大约19:10送达
素坤泰(泰式打抛饭)
美团专送
招牌打抛饭（鸡肉）
¥39
¥45
微辣
x1
五常米饭
¥4
x2
打包费
¥2
配送费
免配送费
美团红包
-¥5
优惠说明
已优惠¥5
合计¥44
提交订单
"""


def test_parse_meituan_preview_bill_real_snapshots() -> None:
    b1 = parse_meituan_preview_bill(SAMPLE_PREVIEW_COTTI)
    assert b1["shop"] == "库迪咖啡（北京大钟寺融寓店）"
    assert b1["delivery_address"] == "示例写字楼 10层"
    assert "139****9000" in b1["recipient_masked"]
    assert "13900139000" not in b1["recipient_masked"]
    assert "21:23" in b1["eta_text"]
    assert len(b1["items"]) == 1
    assert b1["items"][0] == {
        "name": "苹果美式",
        "price_yuan": 19.01,
        "spec": "大杯、冰、不另外加糖、默认奶油、默认",
        "qty": 1,
    }
    assert b1["packing_fee_yuan"] == 1.0
    assert b1["delivery_fee_yuan"] == 0.4
    assert b1["coupon_text"] == "未选红包，最高5元可用"
    assert b1["final_payable_yuan"] == 20.41
    assert b1["has_submit_btn"] is True

    b2 = parse_meituan_preview_bill(SAMPLE_PREVIEW_MULTI)
    assert b2["shop"] == "素坤泰(泰式打抛饭)"
    assert len(b2["items"]) == 2
    assert b2["items"][0]["name"] == "招牌打抛饭（鸡肉）"
    assert b2["items"][0]["price_yuan"] == 39.0
    assert b2["items"][0]["spec"] == "微辣"
    assert b2["items"][1] == {"name": "五常米饭", "price_yuan": 4.0, "spec": "", "qty": 2}
    assert b2["delivery_fee_yuan"] == 0.0
    assert b2["discount_yuan"] == 5.0
    assert b2["final_payable_yuan"] == 44.0


def test_extract_cashier_payable_yuan_ignores_balance_and_limits() -> None:
    cashier_txt = """美团收银台
剩余支付时间 14:50
¥20.41
美团余额（剩余 ¥523.80）
极速支付（单笔免密额度 ¥800）
工商银行储蓄卡 立减 ¥3
微信支付
确认支付 ¥20.41
"""
    assert extract_cashier_payable_yuan(cashier_txt) == 20.41

    header_only = """美团收银台
美团余额 ¥999.00
¥18.50
极速支付
立即支付
"""
    assert extract_cashier_payable_yuan(header_only) == 18.50


def test_validate_pre_submit_guardrails() -> None:
    base_bill = parse_meituan_preview_bill(SAMPLE_PREVIEW_COTTI)
    params = {"final_payable_yuan": 20.41, "delivery_address": "示例写字楼 10层"}

    # 1) 正常通过 & 允许小幅波动 <= ¥2
    assert validate_pre_submit_guardrails(base_bill, params, max_yuan=120.0)["ok"] is True
    assert validate_pre_submit_guardrails({**base_bill, "final_payable_yuan": 22.0}, params, max_yuan=120.0)["ok"] is True

    # 2) 缺实付金额拒绝
    r_miss = validate_pre_submit_guardrails({**base_bill, "final_payable_yuan": None}, params, max_yuan=120.0)
    assert r_miss["ok"] is False and r_miss["reason"] == "missing_payable"

    # 3) 超单笔安全上限拒绝
    r_cap = validate_pre_submit_guardrails({**base_bill, "final_payable_yuan": 125.0}, {**params, "final_payable_yuan": 125.0}, max_yuan=120.0)
    assert r_cap["ok"] is False and r_cap["reason"] == "over_cap"

    # 4) 价格跳涨 > ¥2 拒绝
    r_drift = validate_pre_submit_guardrails({**base_bill, "final_payable_yuan": 23.0}, params, max_yuan=120.0, max_drift_yuan=2.0)
    assert r_drift["ok"] is False and r_drift["reason"] == "price_drift"

    # 5) 配送地址不一致拒绝
    r_addr = validate_pre_submit_guardrails({**base_bill, "delivery_address": "示例小区B 501"}, params, max_yuan=120.0)
    assert r_addr["ok"] is False and r_addr["reason"] == "address_mismatch"


def test_classify_cashier_or_order_state_all_branches() -> None:
    # 即使 title 仍保留「美团收银台」，正文已显示「支付成功」应识别为 PAID
    assert (
        classify_cashier_or_order_state(
            "https://npay.meituan.com/i/cashier/result",
            title="美团收银台",
            text="支付成功\n¥20.41\n等待商家接单",
        )["state"]
        == "PAID"
    )
    # 含美团支付 + 微信选项的常规收银台应识别为 CASHIER_READY（不能误判成 EXTERNAL_APP）
    assert (
        classify_cashier_or_order_state(
            "https://npay.meituan.com/i/cashier/show/index?tradeno=123",
            title="美团收银台",
            text="极速支付\n微信支付\n支付宝\n确认支付 ¥20.41",
        )["state"]
        == "CASHIER_READY"
    )
    # 6 位支付密码 / 短信验证码优先于收银台（合规停手）
    assert (
        classify_cashier_or_order_state(
            "https://npay.meituan.com/i/cashier/show/index",
            text="美团收银台\n请输入6位支付密码\n忘记密码",
        )["state"]
        == "PASSWORD_OR_2FA"
    )
    assert (
        classify_cashier_or_order_state(
            "https://npay.meituan.com/i/cashier/sms",
            text="请输入短信验证码",
        )["state"]
        == "PASSWORD_OR_2FA"
    )
    # 仅限外部 App 拉起
    assert (
        classify_cashier_or_order_state(
            "https://wx.tenpay.com/cgi-bin/mmpayweb-bin/checkmweb",
            text="请打开微信完成支付",
        )["state"]
        == "EXTERNAL_APP"
    )
    # 扣款失败 / 余额不足
    assert (
        classify_cashier_or_order_state(
            "https://npay.meituan.com/i/cashier/show/index",
            text="余额不足，支付失败，请更换银行卡",
        )["state"]
        == "PAY_FAILED"
    )
    # 风控验证码与登录门禁
    assert classify_cashier_or_order_state("https://verify.meituan.com/v2/app/general_page", text="请拖动滑块")["state"] == "CAPTCHA"
    assert classify_cashier_or_order_state("https://passport.meituan.com/account/unitivelogin", text="请先登录")["state"] == "LOGIN"
    # 结算页拒单
    assert (
        classify_cashier_or_order_state(
            "https://h5.waimai.meituan.com/waimai/mindex/preview",
            text="提交订单\n部分商品已售罄",
        )["state"]
        == "SUBMIT_ERROR"
    )


def test_stage_preview_and_strict_payment_permission_gates(sandbox_db: SimingDB, monkeypatch: Any) -> None:
    bill = parse_meituan_preview_bill(SAMPLE_PREVIEW_COTTI)
    cart_res = {
        "ok": True,
        "added_to_cart": True,
        "target_id": "TID-PREVIEW-01",
        "menu_url": "https://h5.waimai.meituan.com/waimai/mindex/menu?poi_id_str=COTTI",
        "shop": "库迪咖啡（北京大钟寺融寓店）",
        "dish": "苹果美式",
    }

    # 1) 超上限预览直接拒绝，不签发确认卡
    over_res = stage_meituan_payment_preview(
        sandbox_db,
        cart_res=cart_res,
        shop="库迪咖啡",
        item_spec="苹果美式",
        deeplink="imeituan://x",
        preview_fn=lambda _d, tid, m: {
            "ok": True,
            "preview_ready": True,
            "preview_target_id": tid,
            "bill": {**bill, "final_payable_yuan": 188.0},
            "final_payable_yuan": 188.0,
        },
    )
    assert over_res["ok"] is False and over_res.get("over_cap") is True
    assert "apply A" not in over_res["reply"]

    # 2) 缺实付金额预览拒绝签发
    miss_res = stage_meituan_payment_preview(
        sandbox_db,
        cart_res=cart_res,
        shop="库迪咖啡",
        item_spec="苹果美式",
        deeplink="imeituan://x",
        preview_fn=lambda _d, tid, m: {
            "ok": True,
            "preview_ready": True,
            "preview_target_id": tid,
            "bill": {**bill, "final_payable_yuan": None},
            "final_payable_yuan": None,
        },
    )
    assert miss_res["ok"] is False and miss_res.get("reason") == "missing_payable"

    # 3) 正常签发付款许可确认卡
    staged = stage_meituan_payment_preview(
        sandbox_db,
        cart_res=cart_res,
        shop="库迪咖啡",
        item_spec="苹果美式",
        deeplink="imeituan://x",
        preview_fn=lambda _d, tid, m: {
            "ok": True,
            "preview_ready": True,
            "preview_target_id": tid,
            "bill": bill,
            "final_payable_yuan": bill["final_payable_yuan"],
        },
    )
    assert staged["ok"] is True
    assert staged["action"] == "meituan_submit_pay"
    tok = staged["approval_token"]
    assert "付款许可确认卡" in staged["reply"]
    assert "¥20.41" in staged["reply"]
    assert "示例写字楼 10层" in staged["reply"]
    assert "确认支付" in staged["reply"]
    assert f"apply {tok}" in staged["reply"]

    paid_calls: list[dict[str, Any]] = []

    def _fake_live_pay(params: dict[str, Any], _db: SimingDB, port: int = 9222) -> dict[str, Any]:
        paid_calls.append(params)
        return {
            "ok": True,
            "paid": True,
            "order_submitted": True,
            "state": "PAID",
            "order_id": "MT202611190001",
            "paid_yuan": 20.41,
            "shop": params["shop"],
            "delivery_address": params["delivery_address"],
            "eta_text": params["eta_text"],
        }

    monkeypatch.setattr("siming.meituan_pay._live_submit_and_pay_meituan_order", _fake_live_pay)

    # 4) 安全红线：未引用的随口「好 / 好的 / 行 / 可以 / 1 / 发吧 / 确认」绝不允许触发真实扣款！
    for casual in ("好", "好的", "行", "可以", "1", "发吧", "确认"):
        r_blk = handle_inbound_qq(sandbox_db, casual)
        assert r_blk["handled"] is True
        assert "防误触" in r_blk["reply"]
        assert "确认支付" in r_blk["reply"]
        assert len(paid_calls) == 0

    # 5) 显式「确认支付」放行扣款，返回真实回执
    r_pay = handle_inbound_qq(sandbox_db, "确认支付")
    assert r_pay["handled"] is True
    assert "美团外卖已下单并支付成功" in r_pay["reply"]
    assert "MT202611190001" in r_pay["reply"]
    assert "¥20.41" in r_pay["reply"]
    assert len(paid_calls) == 1

    # 6) 再次 apply 同一口令 -> 已被 CAS 消费拒绝
    r_dup_tok = handle_inbound_qq(sandbox_db, f"apply {tok}")
    assert r_dup_tok["handled"] is True
    assert "APPLIED" in r_dup_tok["reply"]
    assert len(paid_calls) == 1

    # 7) 相同订单参数再建一张卡 -> 幂等防重付锁拦截
    with pytest.raises(RuntimeError, match="防重复扣款"):
        execute_meituan_submit_pay(paid_calls[0], sandbox_db, cdp_pay_fn=lambda p: {"ok": True, "paid": True})


def test_quoted_confirm_explicit_pay_token_and_2fa_handoff(sandbox_db: SimingDB, monkeypatch: Any) -> None:
    bill = parse_meituan_preview_bill(SAMPLE_PREVIEW_COTTI)

    def _stage_one(tid: str) -> dict[str, Any]:
        return stage_meituan_payment_preview(
            sandbox_db,
            cart_res={"ok": True, "added_to_cart": True, "target_id": tid, "menu_url": "https://h5.waimai.meituan.com/waimai/mindex/menu?x=1"},
            shop="库迪咖啡",
            item_spec="苹果美式",
            deeplink="imeituan://x",
            preview_fn=lambda _d, t, m: {
                "ok": True,
                "preview_ready": True,
                "preview_target_id": t,
                "bill": bill,
                "final_payable_yuan": 20.41,
            },
        )

    # 1) 引用确认卡回「确认」合法放行（且遇支付密码 2FA 诚实停手交接）
    st1 = _stage_one("TID-Q1")
    tok1 = st1["approval_token"]
    monkeypatch.setattr(
        "siming.meituan_pay._live_submit_and_pay_meituan_order",
        lambda p, _d, port=9222: {
            "ok": True,
            "paid": False,
            "order_submitted": True,
            "needs_user_action": True,
            "state": "PASSWORD_OR_2FA",
            "reason": "收银台要求支付密码或二次身份验证，已按安全红线停手保留现场",
            "order_id": "MT202611190002",
            "payable_yuan": 20.41,
            "cashier_url": "https://npay.meituan.com/i/cashier/show/index?tradeno=2",
        },
    )
    r_q = handle_inbound_qq(sandbox_db, "确认", reply_to_text=st1["reply"])
    assert r_q["handled"] is True
    assert "等待你完成最终支付验证" in r_q["reply"]
    assert "https://npay.meituan.com/i/cashier/show/index?tradeno=2" in r_q["reply"]
    assert "imeituan://www.meituan.com/waimai/orders" in r_q["reply"]

    # 2) 「确认支付」在仅有非支付审批卡时绝不误触发 git_apply_worktree
    non_pay = create_approval(sandbox_db, "git_apply_worktree", "W99", {"repo_dir": "/tmp/x"})
    r_wrong_phrase = handle_inbound_qq(sandbox_db, "确认支付")
    assert r_wrong_phrase["handled"] is True
    assert "没有有效期" in r_wrong_phrase["reply"]

    r_wrong_tok = handle_inbound_qq(sandbox_db, f"支付 {non_pay['token']}")
    assert r_wrong_tok["handled"] is True
    assert "不是外卖/咖啡支付卡" in r_wrong_tok["reply"]

    # 3) 「支付 Axx」显式编号执行付款
    st2 = _stage_one("TID-Q2")
    tok2 = st2["approval_token"]
    monkeypatch.setattr(
        "siming.meituan_pay._live_submit_and_pay_meituan_order",
        lambda p, _d, port=9222: {
            "ok": True,
            "paid": True,
            "order_submitted": True,
            "state": "PAID",
            "order_id": "MT202611190003",
            "paid_yuan": 20.41,
            "shop": p["shop"],
            "delivery_address": p["delivery_address"],
            "eta_text": p["eta_text"],
        },
    )
    r_pt = handle_inbound_qq(sandbox_db, f"支付 {tok2}")
    assert r_pt["handled"] is True
    assert "支付成功" in r_pt["reply"]
    assert "MT202611190003" in r_pt["reply"]


def test_cancel_pending_meituan_order_and_speculative_preview_chain(sandbox_db: SimingDB) -> None:
    bill = parse_meituan_preview_bill(SAMPLE_PREVIEW_COTTI)
    st = stage_meituan_payment_preview(
        sandbox_db,
        cart_res={"ok": True, "added_to_cart": True, "target_id": "TID-CAN1", "menu_url": "https://h5.waimai.meituan.com/waimai/mindex/menu?x=1"},
        shop="库迪咖啡",
        item_spec="苹果美式",
        deeplink="imeituan://x",
        preview_fn=lambda _d, t, m: {
            "ok": True,
            "preview_ready": True,
            "preview_target_id": t,
            "bill": bill,
            "final_payable_yuan": 20.41,
        },
    )
    tok = st["approval_token"]

    # 取消 Axx 作废付款卡
    r_c = handle_inbound_qq(sandbox_db, f"取消 {tok}")
    assert r_c["handled"] is True
    assert "已取消美团待付确认卡" in r_c["reply"]
    assert tok in r_c["reply"]

    # 作废后再次确认支付应无可用卡
    assert verify_and_apply_approval(sandbox_db, tok)["ok"] is False

    # 无待付美团单时，「不点了」不劫持普通对话
    assert handle_inbound_qq(sandbox_db, "不点了")["handled"] is False

    # 投机预操作 confirm_and_execute_speculative_pick 接入 preview_fn 直出付款许可卡
    picks = [{"shop": "农耕记·湘菜小炒·盖码饭（六道口店）", "avg_yuan": 32.0, "rating": 4.9, "eta_min": 30, "distance_str": "3.8km"}]
    batch = create_speculative_batch(
        sandbox_db,
        picks=picks,
        delivery_poi="示例写字楼 10层",
        cur_slot="晚餐",
        max_budget=None,
        loc_target={"alias": "公司", "label": "示例写字楼"},
    )
    sample_cards = [
        {"idx": 0, "hasPlus": True, "lines": ["青椒火腿炒鸡蛋盖码饭", "1人份", "月售800", "¥28.9"]},
    ]
    run_speculative_meal_preplan(
        sandbox_db,
        batch_id=batch["batch_id"],
        inspect_shop_fn=lambda _s, _l: {
            "ok": True,
            "live_shop_name": "农耕记·湘菜小炒·盖码饭（六道口二店）",
            "menu_url": "https://h5.waimai.meituan.com/waimai/mindex/menu?poi_id_str=NGJ123",
            **parse_meituan_menu_cards(sample_cards, "预估加配送费 ¥4.9\n¥15起送"),
        },
        max_workers=1,
    )
    cf = confirm_and_execute_speculative_pick(
        sandbox_db,
        "第1家",
        cdp_exec_fn=lambda plan: {
            "ok": True,
            "added_to_cart": True,
            "shop": plan["shop"],
            "added_dishes": [d["name"] for d in plan["dishes"]],
            "target_id": "TID-SPEC-1",
            "menu_url": plan["menu_url"],
        },
        preview_fn=lambda _d, t, m: {
            "ok": True,
            "preview_ready": True,
            "preview_target_id": t,
            "bill": {
                **bill,
                "shop": "农耕记·湘菜小炒·盖码饭（六道口二店）",
                "items": [{"name": "青椒火腿炒鸡蛋盖码饭", "price_yuan": 28.9, "spec": "", "qty": 1}],
                "final_payable_yuan": 33.8,
            },
            "final_payable_yuan": 33.8,
        },
    )
    assert cf["ok"] is True
    assert cf["action"] == "meituan_submit_pay"
    assert "付款许可确认卡" in cf["reply"]
    assert "¥33.8" in cf["reply"]
    assert "确认支付" in cf["reply"]


SAMPLE_PACKAGE_PREVIEW_GUOJI = """外卖配送
到店自取
示例小区A公寓入口北侧美团外卖柜
张三
先生
13800138000
立即送出
大约22:39送达
订单距离较远，送达时间可能波动
示例鲜果切（学院店）
美团快送
【冰镇】鲜切麒麟西瓜-500g
单价:¥25.8
数量:1
¥
18
.8
¥
25
.8
打包费
¥2
配送费
¥9
¥2
优惠活动
-¥2.0
美团红包
-¥5
小计￥22.8
已优惠￥14
合计
¥22.8
提交订单
"""


def test_package_preview_bill_split_cashier_amount_and_olist_status() -> None:
    from siming.meituan_pay import _is_meituan_preview_url, _olist_status_is_paid

    assert _is_meituan_preview_url("https://cactivityapi-sc.waimai.meituan.com/h5/package/preview/preview?poi_id_str=X")
    assert _is_meituan_preview_url("https://h5.waimai.meituan.com/waimai/mindex/preview")
    assert not _is_meituan_preview_url("https://mpay.meituan.com/resource/icashier/s/index.html")

    bill = parse_meituan_preview_bill(SAMPLE_PACKAGE_PREVIEW_GUOJI)
    assert bill["shop"] == "示例鲜果切（学院店）"
    assert "示例小区A" in bill["delivery_address"]
    assert bill["final_payable_yuan"] == 22.8
    assert bill["items"] and bill["items"][0]["qty"] == 1
    assert bill["has_submit_btn"] is True

    # mpay.meituan.com 拆行金额与交易成功金额识别
    split_cashier = "支付订单\n交易剩余时间 14:58\n¥\n20.34\n示例鲜果切（学院店）-800000000000000001\n余额 (可用¥30.00)\n确认支付\n"
    assert extract_cashier_payable_yuan(split_cashier) == 20.34

    paid_overlay = "支付订单\n交易成功 ¥20.34\n找回密码\n"
    assert extract_cashier_payable_yuan(paid_overlay) == 20.34
    assert classify_cashier_or_order_state("https://mpay.meituan.com/resource/icashier/s/index.html", text=paid_overlay)["state"] == "PAID"

    keypad_open = "支付订单\n请输入密码\n¥20.34\n找回密码\n1\n2\n3\n"
    assert classify_cashier_or_order_state("https://mpay.meituan.com/resource/icashier/s/index.html", text=keypad_open)["state"] == "PASSWORD_OR_2FA"

    # olist 状态判定：「订单已提交 + 立即支付」绝不能误判成已支付；「商家已接单」判为已支付
    assert _olist_status_is_paid("订单已提交", "实付¥22.8\n订单已提交\n立即支付") is False
    assert _olist_status_is_paid("待支付", "实付¥22.8\n待支付") is False
    assert _olist_status_is_paid("商家已接单", "实付¥22.8\n商家已接单\n删除\n再来一单") is True
    assert _olist_status_is_paid("等待商家接单", "实付¥22.8\n等待商家接单") is True


def test_hermes_confidential_payment_handoff_zero_leak_and_independent_verify(
    sandbox_db: SimingDB, tmp_path: Any
) -> None:
    from siming.meituan_pay import _is_hermes_handoff_ready, _run_hermes_confidential_payment_handoff

    sentinel = "SYNTHETIC_SECRET_PIN_998877"
    fake_instr = tmp_path / "fake_payment_guide.md"
    fake_instr.write_text(f"付款说明：{sentinel}\n", encoding="utf-8")
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
    assert _is_hermes_handoff_ready(policy) is True

    captured_env: dict[str, str] = {}
    ctx_snapshot_during_run: list[str] = []

    class _FakeProc:
        def __init__(self, rc: int, out: bytes) -> None:
            self.returncode = rc
            self.stdout = out
            self.stderr = b""

    def _fake_run(cmd: Any, **kwargs: Any) -> _FakeProc:
        env = kwargs["env"]
        captured_env.update(env)
        ctx_p = env["SIMING_MT_CTX"]
        with open(ctx_p, encoding="utf-8") as fh:
            ctx_snapshot_during_run.append(fh.read())
        # 即使模型意外在 stdout 回显敏感词，司命也绝不将其透传到返回值
        return _FakeProc(0, f"stdout leaked {sentinel}".encode("utf-8"))

    ctx = {
        "target_id": "TID-CASHIER-1",
        "cashier_url": "https://mpay.meituan.com/resource/icashier/s/index.html?tradeno=123",
        "shop": "示例鲜果切（学院店）",
        "item_spec": "【冰镇】鲜切麒麟西瓜-500g",
        "order_id": "800000000000000001",
        "verified_yuan": 22.8,
        "cashier_yuan": 20.34,
        "max_yuan": 120.0,
        "delivery_address": "示例小区A公寓入口北侧美团外卖柜",
        "eta_text": "大约22:39送达",
        "cashier_text_head": "支付订单\n¥\n20.34\n",
    }

    # 1) 独立 CDP 核验确认已支付 -> 返回 paid=True、实际实付 ¥20.34、且零敏感词泄露 + 临时文件已删
    r_paid = _run_hermes_confidential_payment_handoff(
        ctx,
        sandbox_db,
        policy,
        subprocess_run_fn=_fake_run,
        verify_cashier_fn=lambda _t, _p: {"ok": True, "paid": True, "state": "PAID", "payable_yuan": 20.34, "url": ctx["cashier_url"]},
        verify_olist_fn=lambda _s, _o, _p: {"ok": True, "found": True, "paid": True, "shop": "示例鲜果切（学院店）", "status": "商家已接单"},
    )
    assert r_paid["ok"] is True and r_paid["paid"] is True
    assert r_paid["paid_yuan"] == 20.34
    assert r_paid["order_status"] == "商家已接单"
    assert r_paid["pay_channel"] == "hermes_confidential_handoff"
    assert sentinel not in str(r_paid)
    assert sentinel not in ctx_snapshot_during_run[0]
    assert captured_env["SIMING_MT_PROVIDER"] == "RedPill"
    assert captured_env["SIMING_MT_MODEL"] == "phala/deepseek-v4.1-flash-1017-uncensored"
    assert captured_env["SIMING_MT_INSTR"] == str(fake_instr)
    assert not list(sandbox_db.paths.tmp_root.glob("mt_pay_*"))

    # 2) 即使 Hermes 退出码 0，若纯代码核验仍未支付 -> 绝不误报支付成功，保留收银台交接人工
    r_unpaid = _run_hermes_confidential_payment_handoff(
        ctx,
        sandbox_db,
        policy,
        subprocess_run_fn=_fake_run,
        verify_cashier_fn=lambda _t, _p: {
            "ok": True,
            "paid": False,
            "state": "PASSWORD_OR_2FA",
            "payable_yuan": 20.34,
            "remaining_time": "12:30",
            "url": ctx["cashier_url"],
        },
        verify_olist_fn=lambda _s, _o, _p: {"ok": True, "found": True, "paid": False, "shop": "示例鲜果切（学院店）", "status": "订单已提交"},
    )
    assert r_unpaid["paid"] is False
    assert r_unpaid["needs_user_action"] is True
    assert r_unpaid["kept_tab_open"] is True
    assert "12:30" in r_unpaid["reason"]
    assert sentinel not in str(r_unpaid)


def test_router_extract_oral_delivery_location_override() -> None:
    from siming.router import _extract_order_location_override

    loc1, q1 = _extract_order_location_override("点回家一个西瓜")
    assert loc1 == "家" and q1 == "西瓜"

    loc2, q2 = _extract_order_location_override("送到公司 瑞幸 冰美式")
    assert loc2 == "公司" and q2 == "瑞幸 冰美式"

    loc3, q3 = _extract_order_location_override("示例果切 麒麟西瓜")
    assert loc3 == "" and q3 == "示例果切 麒麟西瓜"
