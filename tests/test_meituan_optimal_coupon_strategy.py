"""美团点饭 & 日用品补货「最优红包省钱策略」全链路回归测试（纯离线零真扣款）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import pytest

from siming.approval import create_approval, verify_and_apply_approval
from siming.db import SHANGHAI_TZ, SimingDB, iso_now
from siming.meal_guard import plan_dual_shop_combo
from siming.meituan_cart_live import parse_cart_preview_bill
from siming.meituan_coupons import (
    apply_optimal_preview_coupons,
    classify_coupon_category,
    extract_near_threshold_tip_from_cards,
    find_cash_saving_topup,
    infer_order_category,
    is_paid_coupon_upsell,
    optimize_multi_leg_coupons,
    parse_coupon_card_info,
    plan_replenish_coupon_strategy,
    select_optimal_coupon_candidate,
)
from siming.meituan_pay import parse_meituan_preview_bill, stage_meituan_payment_preview
from siming.meituan_speculate import (
    COUPONS_CACHE_META_KEY,
    compute_coupon_pricing,
    infer_dishes_for_shop,
    parse_meituan_account_redpackets,
    parse_meituan_all_account_redpackets,
    parse_meituan_menu_cards,
)
from siming.replenish_cart import (
    _load_replenish_items,
    send_replenish_due_reminder,
    stage_replenish_buy,
    upsert_replenish_item,
)


SAMPLE_MIXED_COUPONS_TEXT = """美团红包
12张券
吃喝玩乐神券
¥
5
有效期至2026.12.15
无门槛
限非到店自取订单，天天神券商家使用。
去使用
闪购便利店神券
¥
20
有效期至2026.11.25
满59可用
限非到店自取订单，便利店神券商家使用。
去使用
大药房买药神券
¥
15
有效期至2026.11.25
满49可用
限非到店自取订单，医药大药房商家使用。
去使用
外卖大额神券
¥
30
有效期至2026.11.24 12:35:52
满76可用
限商品现价+打包费-所有优惠金额满76元使用（不含配送费）。限非到店自取订单，神券活动商家使用。
去使用
外卖大额神券
¥
17
有效期至2026.11.24 12:35:52
满39可用
限商品现价+打包费-所有优惠金额满39元使用（不含配送费）。限非到店自取订单，神券活动商家使用。
去使用
外卖神券
¥
10
有效期至2026.11.24 12:35:52
满35可用
限商品现价+打包费-所有优惠金额满35元使用（不含配送费）。限非到店自取订单，神券活动商家使用。
去使用
闪购暖衣季服饰神券
¥
70
有效期至2026.11.26
满279可用
限非到店自取订单，闪购部分服饰商家使用。
去使用
"""


def test_optimal_coupon_selection_beats_dom_order_and_skips_paid_upsells() -> None:
    now = datetime(2026, 11, 23, 20, 0, tzinfo=SHANGHAI_TZ)
    raw_cards = [
        # DOM 第一张：付费神券包诱饵（金额更大但需要花钱买，必须严格剔除！）
        "9.9元购4张15元神券包\n¥60\n立即购买",
        # DOM 第二张：面额较小的无门槛券
        "¥\n5\n无门槛\n吃喝玩乐神券\n有效期至2026.12.15",
        # DOM 第三张：26元券（A24 曾经误选的次优券）
        "¥\n26\n满70可用\n外卖大额神券\n有效期至2026.11.24 12:00",
        # DOM 第四张：真正的最大额 30元券（全角 ￥ 也必须识别）
        "￥\n30\n满76可用\n外卖大额神券\n有效期至2026.11.24 12:35",
        # DOM 第五张：不可用（还差凑单）
        "¥\n38\n满98可用\n还差12元可用\n外卖超大额神券",
    ]
    parsed = [parse_coupon_card_info(t, dom_idx=i, x=350, y=120 + i * 90, now_dt=now) for i, t in enumerate(raw_cards)]
    cards = [c for c in parsed if c is not None]

    assert is_paid_coupon_upsell(raw_cards[0]) is True
    best = select_optimal_coupon_candidate(cards, now_dt=now)
    assert best is not None
    assert best["amount_yuan"] == 30.0
    assert best["threshold_yuan"] == 76.0
    assert best["dom_idx"] == 3

    # 不可用卡还能提取凑单提示
    near = extract_near_threshold_tip_from_cards(cards, current_discount=30.0)
    assert "12" in near and "满98减38" in near


def test_optimal_coupon_tiebreakers_preserve_no_threshold_and_burn_soonest_expiry() -> None:
    now = datetime(2026, 11, 23, 20, 0, tzinfo=SHANGHAI_TZ)
    # 1. 同面额 ¥10：优先消耗高门槛满35减10，留住无门槛减10给小单！
    c_no_th = parse_coupon_card_info("¥10\n无门槛\n吃喝玩乐神券\n有效期至2026.11.24", dom_idx=0, now_dt=now)
    c_hi_th = parse_coupon_card_info("¥10\n满35可用\n外卖神券\n有效期至2026.11.30", dom_idx=1, now_dt=now)
    assert c_no_th and c_hi_th
    best1 = select_optimal_coupon_candidate([c_no_th, c_hi_th], now_dt=now)
    assert best1 is not None and best1["threshold_yuan"] == 35.0

    # 2. 同面额同门槛：优先消耗临近过期的券！
    c_late = parse_coupon_card_info("¥10\n满35可用\n外卖神券\n有效期至2026.12.10", dom_idx=0, now_dt=now)
    c_soon = parse_coupon_card_info("¥10\n满35可用\n外卖神券\n有效期至2026.11.24 12:00", dom_idx=1, now_dt=now)
    assert c_late and c_soon
    best2 = select_optimal_coupon_candidate([c_late, c_soon], now_dt=now)
    assert best2 is not None and best2["dom_idx"] == 1

    # 3. 同面额同门槛同过期：优先消耗品类专享券，保留吃喝玩乐通用券！
    c_univ = parse_coupon_card_info("¥5\n无门槛\n吃喝玩乐神券\n有效期至2026.12.15", dom_idx=0, now_dt=now)
    c_spec = parse_coupon_card_info("¥5\n无门槛\n外卖神券\n有效期至2026.12.15", dom_idx=1, now_dt=now)
    assert c_univ and c_spec
    best3 = select_optimal_coupon_candidate([c_univ, c_spec], now_dt=now)
    assert best3 is not None and best3["dom_idx"] == 1


def test_flash_and_pharmacy_coupons_parsed_and_priced_for_replenish() -> None:
    dt_now = datetime(2026, 11, 23, 20, 0, tzinfo=SHANGHAI_TZ)
    all_rp = parse_meituan_all_account_redpackets(SAMPLE_MIXED_COUPONS_TEXT, now_dt=dt_now)

    # 餐饮列表仍严格排除闪购/医药/服饰（向后兼容）
    food_titles = [c["title"] for c in all_rp["platform_coupons"]]
    assert not any("闪购" in t or "药房" in t or "服饰" in t for t in food_titles)
    assert any(c["amount"] == 30.0 for c in all_rp["platform_coupons"])

    # 闪购/补货列表保留商超神券、医药神券与吃喝玩乐通用券，仍排除服饰神券
    flash_titles = [c["title"] for c in all_rp["flash_coupons"]]
    assert any("闪购便利店神券" in t for t in flash_titles)
    assert any("大药房买药神券" in t for t in flash_titles)
    assert any("吃喝玩乐神券" in t for t in flash_titles)
    assert not any("服饰" in t for t in flash_titles)
    assert parse_meituan_account_redpackets(SAMPLE_MIXED_COUPONS_TEXT, now_dt=dt_now) == all_rp["platform_coupons"]

    acc = {**all_rp, "store_vouchers": [{"shop": "示例鲜果切（学院店）", "amount": 8.0, "threshold": 49.0, "stackable": False, "label": "商家券 满49减8"}]}

    # 商超补货：¥62 在即购超市自动命中「闪购便利店神券 满59减20」！
    assert infer_order_category("即购超市（知春路店）", store_kind="subtrade") == "supermarket"
    cp_sm = compute_coupon_pricing("即购超市（知春路店）", subtotal=62.0, delivery_fee=3.0, account_coupons=acc, order_category="supermarket", now_dt=dt_now)
    assert cp_sm["platform_coupon_off"] == 20.0
    assert cp_sm["after_coupon_total"] == 45.0

    # 医药补货：¥52 在叮当快药自动命中「大药房买药神券 满49减15」！
    assert infer_order_category("叮当快药(北京知春路店)", store_kind="subtrade") == "pharmacy"
    cp_ph = compute_coupon_pricing("叮当快药(北京知春路店)", subtotal=52.0, delivery_fee=4.0, account_coupons=acc, order_category="pharmacy", now_dt=dt_now)
    assert cp_ph["platform_coupon_off"] == 15.0
    assert cp_ph["after_coupon_total"] == 41.0

    # 餐饮含打包费门槛：餐品 ¥73 + 打包费 ¥4 = ¥77 >= 76 -> 正确触发「满76减30」！
    cp_pack = compute_coupon_pricing("南京大牌档(中关村店)", subtotal=73.0, packing_fee=4.0, delivery_fee=5.0, account_coupons=acc, order_category="food", now_dt=dt_now)
    assert cp_pack["platform_coupon_off"] == 30.0
    assert cp_pack["after_coupon_total"] == 52.0

    # 不可叠加商家券（stackable=False）绝不和平台券重复叠加，且在 ¥50 时正确二选一取最优（¥8 商家券 vs ¥5 吃喝玩乐券 -> 选 ¥8）
    cp_ex = compute_coupon_pricing("示例鲜果切（学院店）", subtotal=50.0, delivery_fee=2.0, account_coupons=acc, order_category="fruit", now_dt=dt_now)
    assert cp_ex["total_discount"] in (8.0, 17.0)  # 若果切命中外卖满39减17则选17，绝不把 stackable=False 的 8 叠加上去变 25！
    assert cp_ex["total_discount"] < 20.0


def test_dining_infer_auto_topup_saves_real_cash() -> None:
    """点饭场景：主菜 ¥34（原用无门槛减5实付 ¥33），加 ¥5 荷包蛋凑满39减17 -> 实付降至 ¥26（倒省 ¥7 + 白得荷包蛋）！"""
    dt_now = datetime(2026, 11, 23, 20, 0, tzinfo=SHANGHAI_TZ)
    all_rp = parse_meituan_all_account_redpackets(SAMPLE_MIXED_COUPONS_TEXT, now_dt=dt_now)
    acc = {**all_rp, "store_vouchers": []}

    cards = [
        {"idx": 0, "hasPlus": True, "lines": ["招牌辣椒炒肉盖码饭", "1人份", "月售1200", "¥34"]},
        {"idx": 1, "hasPlus": True, "lines": ["现煎荷包蛋", "1人份", "月售800", "¥5"]},
        {"idx": 2, "hasPlus": True, "lines": ["五常白米饭", "1人份", "月售600", "¥3"]},
    ]
    menu = parse_meituan_menu_cards(cards, "预估加配送费 ¥4\n¥20起送")

    # 直接验证 find_cash_saving_topup
    top = find_cash_saving_topup(
        shop_name="湘菜馆",
        current_subtotal=34.0,
        delivery_fee=4.0,
        candidate_items=menu["items"][1:],
        account_coupons=acc,
        now_dt=dt_now,
    )
    assert top["auto_topup"] is not None
    assert top["auto_topup"]["name"] == "现煎荷包蛋"
    assert top["auto_topup"]["before_after_total"] == 33.0  # 34 + 4 - 5
    assert top["auto_topup"]["new_after_total"] == 26.0     # 39 + 4 - 17
    assert top["auto_topup"]["net_cash_saved"] == 7.0

    # 端到端 infer_dishes_for_shop 自动顺手加配倒省钱的荷包蛋
    inf = infer_dishes_for_shop(
        "湘菜馆",
        menu,
        cur_slot="晚餐",
        account_coupons=acc,
        now_dt=dt_now,
    )
    assert inf["ok"] is True
    names = [d["name"] for d in inf["dishes"]]
    assert "招牌辣椒炒肉盖码饭" in names
    assert "现煎荷包蛋" in names
    assert inf["est_subtotal"] == 39.0
    assert "倒省¥7" in inf["reason"]


def test_apply_optimal_preview_coupons_mindex_picks_max_and_guards_non_stackable_voucher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟 mindex/preview 结算页：
    1) 美团红包页有 3 张券（DOM 顺序 ¥5 / ¥26 / ¥30 + 付费诱饵），必须选中 ¥30（即使首次 mouse click 未生效也会用 touch tap 重试核验）；
    2) 接着尝试叠加商家代金券（-¥5），模拟商家券互斥顶掉 ¥30 红包导致实付飙升 -> 自动回滚重选 ¥30 红包保住最低价 ¥60.1！
    """
    import siming.meituan_coupons as mc

    monkeypatch.setattr(mc, "_wait", lambda _s: None)
    monkeypatch.setattr(mc, "_ensure_active", lambda _ws: None)

    state: dict[str, Any] = {
        "url": "https://h5.waimai.meituan.com/waimai/mindex/preview?placeholder=1&redirectfrom=1",
        "rp_off": 0.0,
        "sv_off": 0.0,
        "pending_rp": 0.0,
        "pending_sv": 0.0,
        "rp_clicks": 0,
        "rp_touches": 0,
        "sv_applied_once": False,
    }

    def _preview_text() -> str:
        base_tot = 90.1
        pay = round(base_tot - state["rp_off"] - state["sv_off"], 2)
        rp_line = f"-¥{state['rp_off']:g}" if state["rp_off"] > 0 else "未选红包，最高30元可用"
        sv_line = f"-¥{state['sv_off']:g}" if state["sv_off"] > 0 else "1张可用"
        return (
            f"提交订单\n美团配送\n示例写字楼 10层\n张三 先生 13800138000\n立即送出\n"
            f"南京大牌档(中关村店)\n蟹粉火鸭烩粉皮\n¥79\nx1\n打包费\n¥4\n配送费\n¥7.1\n"
            f"美团红包\n{rp_line}\n商家代金券\n{sv_line}\n配送券\n暂无可用\n"
            f"小计¥{pay:g}\n合计¥{pay:g}\n提交订单"
        )

    def fake_page_url(_ws: Any) -> str:
        return state["url"]

    def fake_body_text(_ws: Any) -> str:
        if "red_packet" in state["url"]:
            return f"选择美团红包\n已选1张 可减¥{state['pending_rp']:g}\n确认"
        if "poicoupon" in state["url"]:
            return f"选择商家代金券\n已选1张 可减¥{state['pending_sv']:g}\n确认"
        return _preview_text()

    def fake_find_row(_ws: Any, kind: str) -> dict[str, Any]:
        if kind == "redpacket":
            return {"ok": True, "x": 300.0, "y": 420.0}
        if kind == "store_voucher":
            return {"ok": True, "x": 300.0, "y": 470.0}
        return {"ok": False}

    def fake_scan_mindex(_ws: Any) -> dict[str, Any]:
        if "red_packet" in state["url"]:
            return {
                "ok": True,
                "cards": [
                    {"domIdx": 0, "aboveDivider": True, "text": "9.9元购神券包\n¥50\n立即购买", "x": 350.0, "y": 150.0, "cardX": 200.0, "cardY": 150.0, "selected": False},
                    {"domIdx": 1, "aboveDivider": True, "text": "¥5\n无门槛\n吃喝玩乐神券", "x": 350.0, "y": 240.0, "cardX": 200.0, "cardY": 240.0, "selected": False},
                    {"domIdx": 2, "aboveDivider": True, "text": "¥26\n满70可用\n外卖大额神券", "x": 350.0, "y": 330.0, "cardX": 200.0, "cardY": 330.0, "selected": False},
                    {"domIdx": 3, "aboveDivider": True, "text": "¥30\n满76可用\n外卖大额神券", "x": 350.0, "y": 420.0, "cardX": 200.0, "cardY": 420.0, "selected": False},
                ],
            }
        return {
            "ok": True,
            "cards": [
                {"domIdx": 0, "aboveDivider": True, "text": "¥5\n满30可用\n南京大牌档商家代金券", "x": 350.0, "y": 200.0, "cardX": 200.0, "cardY": 200.0, "selected": False},
            ],
        }

    def fake_eval_obj(_ws: Any, expr: str) -> dict[str, Any]:
        if "packet-confirm-btn" in expr:
            return {"ok": True, "x": 320.0, "y": 790.0, "text": "确认"}
        if "cell.scrollIntoView" in expr:
            return {"ok": True, "x": 350.0, "y": 420.0, "cardX": 200.0, "cardY": 420.0}
        return {"ok": False}

    def fake_click_xy(_ws: Any, x: float, y: float) -> None:
        if "preview" in state["url"] and (x, y) == (300.0, 420.0):
            state["url"] = "https://h5.waimai.meituan.com/waimai/machpro/red_packet"
            state["pending_rp"] = 0.0
        elif "preview" in state["url"] and (x, y) == (300.0, 470.0):
            state["url"] = "https://h5.waimai.meituan.com/waimai/machpro/poicoupon"
            state["pending_sv"] = 0.0
        elif "red_packet" in state["url"] and y < 700:
            # 模拟第一次鼠标点击被 MachPro 吞掉，迫使引擎走 touch tap 核验！
            state["rp_clicks"] += 1
        elif "poicoupon" in state["url"] and y < 700:
            state["pending_sv"] = 5.0
        elif y >= 700:
            if "red_packet" in state["url"]:
                state["rp_off"] = state["pending_rp"]
                if state["rp_off"] > 0:
                    state["sv_off"] = 0.0
            elif "poicoupon" in state["url"]:
                state["sv_off"] = state["pending_sv"]
                # 模拟互斥：商家代金券把 30 元平台神券顶掉！
                if state["sv_off"] > 0:
                    state["rp_off"] = 0.0
                    state["sv_applied_once"] = True
            state["url"] = "https://h5.waimai.meituan.com/waimai/mindex/preview?placeholder=1&redirectfrom=1"

    def fake_touch_tap(_ws: Any, x: float, y: float) -> None:
        if "red_packet" in state["url"] and (x, y) == (350.0, 420.0):
            state["rp_touches"] += 1
            state["pending_rp"] = 30.0

    monkeypatch.setattr(mc, "_page_url", fake_page_url)
    monkeypatch.setattr(mc, "_body_text", fake_body_text)
    monkeypatch.setattr(mc, "_find_preview_row_pos", fake_find_row)
    monkeypatch.setattr(mc, "_scan_mindex_redpacket_cards", fake_scan_mindex)
    monkeypatch.setattr(mc, "_eval_obj", fake_eval_obj)
    monkeypatch.setattr(mc, "_click_xy", fake_click_xy)
    monkeypatch.setattr(mc, "_touch_tap_xy", fake_touch_tap)

    res = apply_optimal_preview_coupons(object(), store_kind="mindex")  # type: ignore[arg-type]
    assert res["ok"] is True
    assert res["applied"] is True
    assert res["before_payable_yuan"] == 90.1
    assert res["after_payable_yuan"] == 60.1
    assert res["saved_yuan"] == 30.0
    assert res["red_packet_chosen"]["amount_yuan"] == 30.0
    assert state["rp_touches"] >= 1          # 确实用 touch tap 兜底成功勾选
    assert state["sv_applied_once"] is True  # 确实尝试了商家券并因互斥涨价自动回滚保住 ¥60.1！
    assert state["rp_off"] == 30.0 and state["sv_off"] == 0.0
    assert res["warning"] == ""


def test_apply_optimal_preview_coupons_subtrade_drawer_fullwidth_yen_and_confirm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """模拟补货商超 package/preview/preview 抽屉：识别全角 ￥20 满59 优于 ￥5 无门槛，点击确认后立省 ¥20！"""
    import siming.meituan_coupons as mc

    monkeypatch.setattr(mc, "_wait", lambda _s: None)
    monkeypatch.setattr(mc, "_ensure_active", lambda _ws: None)

    state = {
        "url": "https://cactivityapi-sc.waimai.meituan.com/h5/package/preview/preview?poi_id_str=JIGOU",
        "drawer_open": False,
        "rp_off": 0.0,
        "pending_rp": 0.0,
    }

    def fake_body(_ws: Any) -> str:
        pay = round(65.8 - state["rp_off"], 2)
        rp_ln = f"-¥{state['rp_off']:g}" if state["rp_off"] > 0 else "未选红包，最高20元可用"
        base = (
            f"外卖配送\n示例小区A-1号楼 1016\n即购超市（知春路店）\n农夫山泉 2L*8瓶\n"
            f"单价:¥62.8\n数量:1\n打包费\n¥1\n配送费\n¥2\n美团红包\n{rp_ln}\n"
            f"商家代金券\n暂无可用\n配送券\n暂无可用\n合计\n¥{pay:g}\n提交订单"
        )
        if state["drawer_open"]:
            return base + "\n可用优惠券\n￥5 无门槛\n￥20 满59可用\n确认"
        return base

    def fake_scan_sub(_ws: Any) -> dict[str, Any]:
        return {
            "ok": True,
            "cards": [
                {"domIdx": 0, "aboveDivider": True, "text": "￥5\n无门槛\n吃喝玩乐神券", "x": 355.0, "y": 320.0, "cardX": 200.0, "cardY": 320.0, "selected": False},
                {"domIdx": 1, "aboveDivider": True, "text": "￥20\n满59可用\n闪购便利店神券", "x": 355.0, "y": 430.0, "cardX": 200.0, "cardY": 430.0, "selected": False},
            ],
        }

    def fake_click_xy(_ws: Any, x: float, y: float) -> None:
        if not state["drawer_open"] and (x, y) == (310.0, 500.0):
            state["drawer_open"] = True
        elif state["drawer_open"] and (x, y) == (355.0, 430.0):
            state["pending_rp"] = 20.0
        elif state["drawer_open"] and y >= 700:
            state["rp_off"] = state["pending_rp"]
            state["drawer_open"] = False

    monkeypatch.setattr(mc, "_page_url", lambda _ws: state["url"])
    monkeypatch.setattr(mc, "_body_text", fake_body)
    monkeypatch.setattr(mc, "_find_preview_row_pos", lambda _ws, k: {"ok": True, "x": 310.0, "y": 500.0} if k == "redpacket" else {"ok": False})
    monkeypatch.setattr(mc, "_scan_subtrade_drawer_cards", fake_scan_sub)
    monkeypatch.setattr(mc, "_find_confirm_btn_pos", lambda _ws: {"ok": True, "x": 200.0, "y": 780.0, "text": "确认"})
    monkeypatch.setattr(mc, "_click_xy", fake_click_xy)

    res = apply_optimal_preview_coupons(object(), store_kind="subtrade")  # type: ignore[arg-type]
    assert res["ok"] is True
    assert res["applied"] is True
    assert res["saved_yuan"] == 20.0
    assert res["after_payable_yuan"] == 45.8
    assert res["red_packet_chosen"]["amount_yuan"] == 20.0


def test_payment_preview_card_warns_unapplied_coupon_and_reports_savings(sandbox_db: SimingDB) -> None:
    """确认卡必须：① 若仍未选可用红包，显式弹出防多花钱警告；② 成功优选红包时明确报出省钱明细。"""
    bill_unapplied = parse_meituan_preview_bill(
        "美团配送\n示例写字楼 10层\n张三 先生 13800138000\n立即送出\n"
        "南京大牌档(中关村店)\n蟹粉火鸭烩粉皮\n¥79\nx1\n打包费\n¥4\n配送费\n¥7.1\n"
        "美团红包\n未选红包，最高30元可用\n商家代金券\n1张可用\n配送券\n暂无可用\n合计¥90.1\n提交订单"
    )
    assert bill_unapplied["store_voucher_text"] == "1张可用"
    card_warn = stage_meituan_payment_preview(
        sandbox_db,
        cart_res={"ok": True, "added_to_cart": True, "target_id": "TID-W"},
        shop="南京大牌档(中关村店)",
        item_spec="蟹粉火鸭烩粉皮",
        deeplink="imeituan://nj",
        preview_fn=lambda _d, t, _m: {
            "ok": True,
            "preview_ready": True,
            "preview_target_id": t,
            "bill": bill_unapplied,
            "final_payable_yuan": 90.1,
        },
    )
    assert card_warn["ok"] is True
    assert "红包未生效提醒" in card_warn["reply"]
    assert "未选红包，最高30元可用" in card_warn["reply"]

    bill_applied = parse_cart_preview_bill(
        "外卖配送\n示例小区A-1号楼 1016\n即购超市（知春路店）\n农夫山泉 2L*8瓶\n"
        "单价:¥62.8\n数量:1\n打包费\n¥1\n配送费\n¥2\n美团红包\n-¥20\n"
        "商家代金券\n-¥3\n配送券\n暂无可用\n已优惠￥23\n合计\n¥42.8\n提交订单"
    )
    assert bill_applied["store_voucher_text"] == "-¥3"
    assert bill_applied["discount_yuan"] == 23.0
    card_ok = stage_meituan_payment_preview(
        sandbox_db,
        cart_res={"ok": True, "added_to_cart": True, "target_id": "TID-OK", "store_kind": "subtrade"},
        shop="即购超市（知春路店）",
        item_spec="农夫山泉 2L*8瓶",
        deeplink="imeituan://jg",
        preview_fn=lambda _d, t, _m: {
            "ok": True,
            "preview_ready": True,
            "preview_target_id": t,
            "store_kind": "subtrade",
            "bill": bill_applied,
            "final_payable_yuan": 42.8,
            "coupon_opt": {
                "ok": True,
                "applied": True,
                "saved_yuan": 23.0,
                "red_packet_chosen": {"short_label": "满59减20", "amount_yuan": 20.0},
                "store_voucher_chosen": {"short_label": "减3", "amount_yuan": 3.0},
            },
        },
    )
    assert card_ok["ok"] is True
    assert "红包优选" in card_ok["reply"]
    assert "满59减20" in card_ok["reply"]
    assert "立省 **¥23**" in card_ok["reply"]
    assert "红包未生效提醒" not in card_ok["reply"]


def test_replenish_coupon_strategy_co_bundle_and_multi_item_cycle_advance(
    sandbox_db: SimingDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    """补货场景：
    1) 写入含「闪购便利店神券 满59减20」与「无门槛减5」的账户券缓存；
    2) 即购超市的「瓶装水」(¥42.9) + 同店临期「卫生湿巾」(¥18) 触发同店合单省钱策略并展示在提醒卡与备单卡；
    3) 发送「补货 瓶装水 + 卫生湿巾」自动走同店合单多品加购，付款成功后同步推进两项周期！
    """
    now = datetime(2026, 11, 23, 15, 0, tzinfo=SHANGHAI_TZ)
    all_rp = parse_meituan_all_account_redpackets(SAMPLE_MIXED_COUPONS_TEXT, now_dt=now)
    sandbox_db.set_meta(
        COUPONS_CACHE_META_KEY,
        json.dumps({"fetched_at": iso_now(now), **all_rp, "store_vouchers": []}, ensure_ascii=False),
    )

    upsert_replenish_item(
        sandbox_db,
        name="瓶装水",
        cycle_days=18,
        last_bought_date="2026-11-04",
        item_id="rpl_bottled_water",
        location="家",
        channel="meituan",
        meituan_shop="即购超市",
        meituan_query="农夫山泉饮用天然水 2L 8瓶",
        target_yuan=42.9,
        needs_calibration=False,
    )
    upsert_replenish_item(
        sandbox_db,
        name="卫生湿巾",
        cycle_days=60,
        last_bought_date="2026-10-01",
        item_id="rpl_wet_wipes",
        location="家",
        channel="meituan",
        meituan_shop="即购超市",
        meituan_query="心相印卫生湿纸巾 80片",
        target_yuan=18.0,
        needs_calibration=False,
    )

    items = _load_replenish_items(sandbox_db)
    water = next(x for x in items if x["item_id"] == "rpl_bottled_water")
    strat = plan_replenish_coupon_strategy(sandbox_db, water, items, now=now)
    assert strat["ok"] is True
    assert strat["order_category"] == "supermarket"
    assert strat["bundle"] is not None
    assert strat["bundle"]["peer_item_id"] == "rpl_wet_wipes"
    assert strat["bundle"]["saved_vs_separate"] >= 10.0  # 满59减20 vs 两单各减5 + 省一笔运费！
    assert any("同店合单更省" in ln for ln in strat["summary_lines"])

    # 提醒卡展示红包省钱策略
    rem = send_replenish_due_reminder(sandbox_db, "rpl_bottled_water", now=now, bypass_quiet=True)
    assert rem["ok"] is True
    assert "红包策略" in rem["text"]
    assert "同店合单更省" in rem["text"]

    # 合单补货一次把两项送入同一美团订单，付款后两项周期同步推进
    captured: list[dict[str, Any]] = []

    def _fake_stage(db: SimingDB, order_request: str, **kw: Any) -> dict[str, Any]:
        captured.append({"order_request": order_request, **kw})
        app = create_approval(
            db,
            action="meituan_submit_pay",
            target_id="MTPAY-BUNDLE",
            params={"shop": "即购超市", "final_payable_yuan": 43.9},
            ttl_minutes=10,
        )
        return {
            "ok": True,
            "approval_token": app["token"],
            "reply": f"💳 **美团外卖付款许可确认卡** [`{app['token']}`]\n👉 回复 `apply {app['token']}` 确认支付",
        }

    res = stage_replenish_buy(sandbox_db, "补货 瓶装水 + 卫生湿巾", meituan_stage_fn=_fake_stage)
    assert res["ok"] is True
    assert res["bundled"] is True
    assert len(captured) == 1
    assert captured[0]["explicit_shop"] == "即购超市"
    assert len(captured[0]["explicit_dishes"]) == 2
    assert "美团同店合单补货" in res["reply"]

    ap = verify_and_apply_approval(
        sandbox_db,
        res["approval_token"],
        extra_executors={"meituan_submit_pay": lambda _p, _d: "🎉 已支付成功"},
    )
    assert ap["ok"] is True
    after_by_id = {x["item_id"]: x for x in _load_replenish_items(sandbox_db)}
    today_iso = datetime.now(SHANGHAI_TZ).date().isoformat()
    assert after_by_id["rpl_bottled_water"]["last_bought"] == today_iso
    assert after_by_id["rpl_wet_wipes"]["last_bought"] == today_iso


def test_dual_shop_combo_assigns_distinct_account_coupons_globally(sandbox_db: SimingDB) -> None:
    """双拼点餐：全局 1 对 1 分配不同账户神券（不重复使用同一张单次神券，总省钱最大化）。"""
    now = datetime(2026, 11, 23, 20, 0, tzinfo=SHANGHAI_TZ)
    all_rp = parse_meituan_all_account_redpackets(SAMPLE_MIXED_COUPONS_TEXT, now_dt=now)
    acc = {**all_rp, "store_vouchers": []}
    sandbox_db.set_meta(COUPONS_CACHE_META_KEY, json.dumps({"fetched_at": iso_now(now), **acc}, ensure_ascii=False))

    legs = [
        {"shop_name": "示例粉面炒米粉", "item_name": "炒米粉", "item_price": 40.0},
        {"shop_name": "示例瓦罐汤粉面", "item_name": "拌粉", "item_price": 36.0},
    ]
    assigned = optimize_multi_leg_coupons(legs, acc, now_dt=now)
    # 腿1(¥40) 拿 满39减17，腿2(¥36) 拿 满35减10，共省 ¥27（绝不把 满39减17 算两次！）
    assert assigned[0]["coupon_yuan"] == 17.0
    assert assigned[1]["coupon_yuan"] == 10.0

    # 端到端 plan_dual_shop_combo 在未传 shops_meta 时自动接入账户缓存神券
    sandbox_db.set_meta(COUPONS_CACHE_META_KEY, json.dumps({"fetched_at": iso_now(), **acc}, ensure_ascii=False))
    plan = plan_dual_shop_combo(sandbox_db, "麦当劳 + 示例瓦罐汤")
    assert plan["ok"] is True
    # 麦当劳默认 ¥38 -> 满35减10；示例瓦罐汤默认 ¥34 -> 无门槛减5（共 ¥15，高于硬编码默认）
    assert plan["coupon_savings_yuan"] >= 15.0


def test_benign_scope_disclaimer_does_not_reject_usable_coupon() -> None:
    """回归 A28 真实故障：吃喝玩乐¥5无门槛神券文案含「部分商品不可用」「到店自取不可用」绝不能误判为不可用！"""
    now = datetime(2026, 11, 24, 13, 25, tzinfo=SHANGHAI_TZ)
    real_card = (
        "吃喝玩乐神券\n¥\n5\n无门槛\n有效期至2026.12.15\n"
        "限非到店自取订单，天天神券商家使用。部分商品不可用限美食团购、国内酒店..."
    )
    info = parse_coupon_card_info(real_card, dom_idx=0, x=350, y=320, now_dt=now)
    assert info is not None
    assert info["amount_yuan"] == 5.0
    assert info["usable"] is True
    assert info["is_unavailable"] is False

    best = select_optimal_coupon_candidate([info], now_dt=now)
    assert best is not None and best["amount_yuan"] == 5.0

    # 但真正的本单不可用/还差凑单/已过期仍必须严格拦截
    truly_unavail = parse_coupon_card_info("外卖神券\n¥20\n满59可用\n本单不可用\n还差16元可用", dom_idx=1, now_dt=now)
    assert truly_unavail is not None and truly_unavail["usable"] is False


def test_bottled_water_1_5l_spec_matching_and_cross_shop_lowest_net_price() -> None:
    """回归：1.5L 瓶装水严格压过 2L/550ml/单瓶，且补货比价模式跨商超选券后到手价最低门店。"""
    from siming.meituan_agent import _rank_order_candidates, _score_order_dish_match
    from siming.meituan_cart_live import _dish_match_score
    from siming.replenish_cart import DEFAULT_DAILY_CATALOG

    water_cfg = next(x for x in DEFAULT_DAILY_CATALOG if x["item_id"] == "rpl_bottled_water")
    assert "1.5L" in water_cfg["spec"]
    assert "1.5L" in water_cfg["meituan_query"]

    # 店内匹配：1.5L*12瓶 决定性高于 2L*8瓶、550ml*12瓶、以及单瓶 1.5L/瓶
    q = "农夫山泉饮用天然水 1.5L 12瓶"
    s_15_12 = _dish_match_score("【12瓶整箱】农夫山泉 饮用天然水 1.5L*12瓶", q)
    s_15_6 = _dish_match_score("农夫山泉 饮用天然水 1.5L*6瓶", q)
    s_15_1 = _dish_match_score("农夫山泉 饮用天然水 1.5L/瓶", q)
    s_2l_8 = _dish_match_score("【8瓶装】农夫山泉 国产饮用天然水 2L*8瓶", q)
    s_550 = _dish_match_score("农夫山泉 饮用天然水 550ml*12瓶", q)
    assert s_15_12 > s_15_6 > s_15_1
    assert s_15_12 > s_2l_8 + 15.0
    assert s_15_12 > s_550 + 15.0

    # 搜索页菜品匹配同样严格区分 1.5L vs 2L
    assert _score_order_dish_match("农夫山泉饮用天然水 1.5L*12瓶", q) > _score_order_dish_match("农夫山泉饮用天然水 2L*8瓶", q) + 5.0

    # 跨商超比价：小象超市 ¥33.9 免配送 + 满30减5 = 券后 ¥28.9，必须战胜常购的即购超市 ¥42.9 + ¥3 = ¥45.9！
    now = datetime(2026, 11, 24, 13, 30, tzinfo=SHANGHAI_TZ)
    all_rp = parse_meituan_all_account_redpackets(SAMPLE_MIXED_COUPONS_TEXT, now_dt=now)
    shops = [
        {
            "shop": "即购超市（知春路店）",
            "min_order": 20.0,
            "delivery_fee": "¥3",
            "eta_min": 30,
            "distance_km": 1.2,
            "promo_lines": [],
            "dishes": [{"name": "农夫山泉 饮用天然水 1.5L*12瓶", "price": 42.9}],
        },
        {
            "shop": "小象超市（太平庄店）",
            "min_order": 20.0,
            "delivery_fee": "免配送费",
            "eta_min": 32,
            "distance_km": 1.5,
            "promo_lines": ["30减5"],
            "dishes": [{"name": "农夫山泉 饮用天然水 1.5L*12瓶", "price": 33.9}],
        },
    ]
    ranked = _rank_order_candidates(
        shops,
        shop_hint="",
        dish_hint=q,
        profile=[],
        preferred_shop="即购超市",
        prefer_lowest_net_price=True,
        account_coupons=all_rp,
        order_category="supermarket",
    )
    assert ranked[0]["shop"] == "小象超市（太平庄店）"
    assert ranked[0]["est_net_yuan"] < ranked[1]["est_net_yuan"]


def test_brand_locked_flash_coupons_do_not_bleed_into_other_supermarkets() -> None:
    """松鼠神券/屈臣氏专属神券严禁串算到即购超市；即购超市 ¥42+¥1 应精准提示「差¥2换闪购便利店神券 满45减12」！"""
    dt_now = datetime(2026, 11, 24, 14, 0, tzinfo=SHANGHAI_TZ)
    acc = {
        "platform_coupons": [],
        "flash_coupons": [
            {"title": "闪购屈臣氏专属神券", "amount": 60.0, "threshold": 199.0, "expires_at": "2026.11.24", "category": "supermarket", "rule": "屈臣氏神券商家使用", "short_label": "满199减60"},
            {"title": "闪购松鼠神券", "amount": 18.0, "threshold": 55.0, "expires_at": "2026.11.24", "category": "supermarket", "rule": "部分松鼠便利神券商家使用", "short_label": "满55减18"},
            {"title": "闪购便利店神券", "amount": 12.0, "threshold": 45.0, "expires_at": "2026.11.24", "category": "supermarket", "rule": "部分便利店神券商家使用", "short_label": "满45减12"},
            {"title": "吃喝玩乐神券", "amount": 5.0, "threshold": 0.0, "expires_at": "2026.12.15", "category": "universal", "rule": "天天神券商家使用", "short_label": "无门槛减5"},
        ],
        "store_vouchers": [],
    }
    cp_jg = compute_coupon_pricing("即购超市（知春路店）", subtotal=42.0, packing_fee=1.0, delivery_fee=7.9, account_coupons=acc, order_category="supermarket", now_dt=dt_now)
    assert "45减12" in cp_jg["near_coupon_tip"]
    assert "55减18" not in cp_jg["near_coupon_tip"]

    cp_sq = compute_coupon_pricing("松鼠便利（中关村店）", subtotal=56.0, delivery_fee=3.0, account_coupons=acc, order_category="supermarket", now_dt=dt_now)
    assert cp_sq["platform_coupon_off"] == 18.0


