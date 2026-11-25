"""美团 H5 真实加购/预览的离线回归测试（不触网、不碰 9222）。"""

from __future__ import annotations

import json
from typing import Any

from siming.db import SimingDB
from siming.meituan_agent import (
    _extract_order_qty,
    _live_stage_meituan_cart,
    stage_meituan_h5_order,
)
from siming.meituan_cart_live import parse_cart_preview_bill

PREVIEW_TEXT = """配送方式已变更，请重新核对信息
外卖配送
到店自取
示例小区A-1号楼 1016
乌
先生
13700137000
立即送出
大约23:37送达
订单距离较远，送达时间可能波动
示例鲜果切（学院店）
美团快送
【鲜果切】麒麟西瓜果切+混合果切约-500g
单价:¥25.8
数量:3
¥
77
.4
打包费
¥2
配送费
¥9
¥2
美团红包
未选红包，最高5元可用
商家代金券
1张可用
配送券
暂无可用
小计￥81.4
已优惠￥7
号码保护
为保护你的隐私安全，已对商家、骑手隐藏你的真实手机号
支付方式
在线支付
合计
¥81.4
提交订单
￥"""


def test_parse_cart_preview_bill() -> None:
    bill = parse_cart_preview_bill(PREVIEW_TEXT)
    assert bill["delivery_address"] == "示例小区A-1号楼 1016"
    assert bill["shop"] == "示例鲜果切（学院店）"
    assert bill["items"] == [
        {
            "name": "【鲜果切】麒麟西瓜果切+混合果切约-500g",
            "price_yuan": 25.8,
            "spec": "",
            "qty": 3,
            "line_total_yuan": 77.4,
        }
    ]
    assert bill["packing_fee_yuan"] == 2.0
    assert bill["delivery_fee_yuan"] == 2.0  # 配送费 ¥9 划线，实收 ¥2
    assert bill["discount_yuan"] == 7.0
    assert bill["final_payable_yuan"] == 81.4
    assert bill["has_submit_btn"] is True


def test_extract_order_qty() -> None:
    assert _extract_order_qty("示例鲜果切（学院店） 麒麟西瓜 1") == (1, "示例鲜果切（学院店） 麒麟西瓜")
    assert _extract_order_qty("素坤泰 打抛饭 x2") == (2, "素坤泰 打抛饭")
    assert _extract_order_qty("苹果美式 2份") == (2, "苹果美式")
    # 不应误伤重量、楼号与「第N家」
    assert _extract_order_qty("果切 500g") == (1, "果切 500g")
    assert _extract_order_qty("第1家") == (1, "第1家")
    assert _extract_order_qty("示例小区A-1号楼") == (1, "示例小区A-1号楼")


def test_live_stage_honors_loc_override(monkeypatch: Any, sandbox_db: SimingDB) -> None:
    captured: dict[str, Any] = {}

    def fake_live(db: SimingDB, shop: str, dish: str, qty: int, **kwargs: Any) -> dict[str, Any]:
        captured.update({"shop": shop, "dish": dish, "qty": qty, **kwargs})
        return {"ok": True, "added_to_cart": True, "preview": {"preview_ready": True}}

    import siming.meituan_cart_live as cart_live

    monkeypatch.setattr(cart_live, "live_cart_via_trusted_input", fake_live)
    params = {
        "shop": "示例鲜果切（学院店）",
        "dish_name": "【鲜果切】麒麟西瓜果切+混合果切约-500g",
        "qty": 2,
        "query": "示例果切 麒麟西瓜",
        "delivery_poi": "示例写字楼 10层",
        "loc_override": "家",
    }
    res = _live_stage_meituan_cart(params, sandbox_db)
    assert res["ok"] is True
    assert captured["qty"] == 2
    assert captured["location_override"] == "家"  # 地址只认审批 loc_override
    assert captured["address_hint"] == "示例写字楼 10层"  # 仅作为文本提示


def test_stage_live_cart_failure_is_honest(monkeypatch: Any, sandbox_db: SimingDB) -> None:
    monkeypatch.delenv("SIMING_TEST_OFFLINE", raising=False)
    shop = {
        "shop": "示例鲜果切（学院店）",
        "rating": 4.5,
        "min_order": 20.0,
        "delivery_fee": 9.0,
        "eta_min": 30,
        "distance_km": 4.4,
        "distance_str": "4.4km",
        "dishes": [{"name": "【鲜果切】麒麟西瓜果切+混合果切约-500g", "price": 25.8}],
    }
    monkeypatch.setattr(
        "siming.meituan_agent._default_meituan_h5_probe",
        lambda *_a, **_k: {
            "ok": True,
            "gated": False,
            "target_id": "fake-tab",
            "delivery_poi": "示例小区A-1号楼 1016",
            "shops": [shop],
            "data": json.dumps({"delivery_poi": "示例小区A-1号楼 1016", "shops": [shop]}, ensure_ascii=False),
        },
    )
    monkeypatch.setattr(
        "siming.meituan_agent._live_cart_via_trusted_input",
        lambda *_a, **_k: {
            "ok": False,
            "gated": False,
            "stage": "preview",
            "reason": "preview",
            "error": "结算预览页未渲染",
        },
    )
    monkeypatch.setattr("siming.meituan_pay._close_tab_quietly", lambda *_a, **_k: None)

    res = stage_meituan_h5_order(sandbox_db, "示例鲜果切（学院店） 麒麟西瓜 1", location_override="家")
    assert res["ok"] is False
    assert res.get("live_cart_failed") is True
    assert res.get("stage") == "preview"
    assert "结算预览页未渲染" in res["reply"]
    assert "没有伪造" in res["reply"]
    assert "approval_token" not in res


def test_parse_multi_dish_and_chinese_pick_index() -> None:
    from siming.meituan_agent import _parse_dish_items, extract_order_query_parts

    p1 = extract_order_query_parts("示例粉面广东大排档(鸟巢店) 三丝炒米粉 滑蛋牛肉 爽口萝卜 确认下单")
    assert p1["shop_hint"] == "示例粉面广东大排档(鸟巢店)"
    assert p1["dishes"] == [
        {"name": "三丝炒米粉", "qty": 1},
        {"name": "滑蛋牛肉", "qty": 1},
        {"name": "爽口萝卜", "qty": 1},
    ]

    p2 = extract_order_query_parts("第二个，三丝炒米粉，滑蛋牛肉，爽口萝卜 确认下单")
    assert p2["pick_index"] == 2
    assert p2["dishes"] == [
        {"name": "三丝炒米粉", "qty": 1},
        {"name": "滑蛋牛肉", "qty": 1},
        {"name": "爽口萝卜", "qty": 1},
    ]

    p3 = extract_order_query_parts("示例粉面广东大排档 鸟巢店 三丝炒米粉x2 滑蛋牛肉")
    assert p3["shop_hint"] == "示例粉面广东大排档(鸟巢店)"
    assert p3["dishes"] == [
        {"name": "三丝炒米粉", "qty": 2},
        {"name": "滑蛋牛肉", "qty": 1},
    ]

    # 单品名内部无空格 `+` 号不应被误拆为多道菜
    assert _parse_dish_items("【鲜果切】麒麟西瓜果切+混合果切约-500g") == [
        {"name": "【鲜果切】麒麟西瓜果切+混合果切约-500g", "qty": 1}
    ]
    # 普通以「三」开头的菜品绝不能被误判为 pick_index=3
    p4 = extract_order_query_parts("三丝炒米粉")
    assert p4["pick_index"] is None
    assert p4["dish_hint"] == "三丝炒米粉"


def test_dish_match_score_fuzzy_subsequence() -> None:
    from siming.meituan_cart_live import _dish_match_score

    assert _dish_match_score("滑蛋炒牛肉", "滑蛋牛肉") >= 62.0
    assert _dish_match_score("三丝炒米粉", "三丝米粉") >= 62.0
    assert _dish_match_score("爽口酸甜萝卜", "爽口萝卜") >= 62.0
    assert _dish_match_score("蜜汁叉烧拼烧鸭饭", "滑蛋牛肉") < 40.0


def test_address_matching_excludes_takeout_locker() -> None:
    from siming.meituan_agent import _match_best_addstore_entry
    from siming.meituan_cart_live import _score_address_candidate

    addstore = [
        {
            "id": 1001,
            "poi": "示例小区A公寓入口北侧美团外卖柜",
            "address": "取餐柜",
            "lat": 39974073,
            "lng": 116372202,
        },
        {
            "id": 1002,
            "poi": "示例小区A-1号楼",
            "address": "1016",
            "lat": 39974120,
            "lng": 116372260,
        },
        {
            "id": 1003,
            "poi": "示例写字楼",
            "address": "11层",
            "lat": 39978900,
            "lng": 116353200,
        },
    ]
    loc_target = {
        "has_location": True,
        "alias": "家",
        "label": "示例小区A",
        "match_hint": "示例小区A",
        "lat": 39.905200,
        "lng": 116.372202,
    }
    best = _match_best_addstore_entry(addstore, loc_target)
    assert best is not None
    assert best["id"] == 1002
    assert "外卖柜" not in best["poi"]

    assert _score_address_candidate("示例小区A公寓入口北侧美团外卖柜", "示例小区a1号楼1016", "示例小区A", False) < 0
    assert _score_address_candidate("示例小区A-1号楼 1016", "示例小区a1号楼1016", "示例小区A", False) >= 30


def test_stage_order_preserves_user_dishes_and_routes_qq_second_shop(monkeypatch: Any, sandbox_db: SimingDB) -> None:
    from siming.modules import set_module_enabled
    from siming.router import handle_inbound_qq

    monkeypatch.delenv("SIMING_TEST_OFFLINE", raising=False)
    set_module_enabled(sandbox_db, "meituan_agent", True)
    sandbox_db.set_meta(
        "last_meituan_picks",
        json.dumps(
            [
                {"shop": "小谷姐姐麻辣拌（北太平庄店）", "avg_yuan": 35.0},
                {"shop": "示例粉面广东大排档(鸟巢店)", "avg_yuan": 68.0},
            ],
            ensure_ascii=False,
        ),
    )

    shop = {
        "shop": "示例粉面广东大排档(鸟巢店)",
        "rating": 4.7,
        "min_order": 20.0,
        "delivery_fee": "¥6",
        "eta_min": 42,
        "distance_km": 3.6,
        "distance_str": "3.6km",
        # 搜索卡片仅展示招牌烧鹅饭，绝不能覆盖用户手点的菜！
        "dishes": [{"name": "古井深井烧鹅例牌", "price": 68.0}],
    }
    monkeypatch.setattr(
        "siming.meituan_agent._default_meituan_h5_probe",
        lambda *_a, **_k: {
            "ok": True,
            "gated": False,
            "target_id": "tab-lyj",
            "delivery_poi": "示例小区A-1号楼 1016",
            "shops": [shop],
            "data": json.dumps({"delivery_poi": "示例小区A-1号楼 1016", "shops": [shop]}, ensure_ascii=False),
        },
    )

    captured_calls: list[dict[str, Any]] = []

    def fake_cart(db: SimingDB, s_name: str, d_name: str, qty: int, **kw: Any) -> dict[str, Any]:
        captured_calls.append({"shop": s_name, "dish": d_name, "qty": qty, **kw})
        return {
            "ok": True,
            "added_to_cart": True,
            "shop": s_name,
            "dish": "三丝炒米粉 + 滑蛋炒牛肉 + 爽口萝卜",
            "store_kind": "mindex",
            "target_id": "tab-lyj",
            "preview": {
                "ok": True,
                "preview_ready": True,
                "target_id": "tab-lyj",
                "preview_url": "https://h5.waimai.meituan.com/waimai/mindex/preview?placeholder=1&redirectfrom=1",
                "bill": {
                    "delivery_address": "示例小区A-1号楼 1016",
                    "recipient_masked": "乌(先生) 130****8837",
                    "eta_text": "大约23:25送达",
                    "shop": s_name,
                    "items": [
                        {"name": "三丝炒米粉", "price_yuan": 32.0, "spec": "", "qty": 1},
                        {"name": "滑蛋炒牛肉", "price_yuan": 48.0, "spec": "", "qty": 1},
                        {"name": "爽口萝卜", "price_yuan": 18.0, "spec": "", "qty": 1},
                    ],
                    "packing_fee_yuan": 3.0,
                    "delivery_fee_yuan": 6.0,
                    "discount_yuan": 12.0,
                    "final_payable_yuan": 95.0,
                    "has_submit_btn": True,
                },
            },
        }

    monkeypatch.setattr("siming.meituan_agent._live_cart_via_trusted_input", fake_cart)

    r_qq = handle_inbound_qq(sandbox_db, "第二个，三丝炒米粉，滑蛋牛肉，爽口萝卜 确认下单")
    assert r_qq["handled"] is True
    assert "¥95" in r_qq["reply"]
    assert "示例小区A-1号楼 1016" in r_qq["reply"]
    assert len(captured_calls) == 1
    assert captured_calls[0]["shop"] == "示例粉面广东大排档(鸟巢店)"
    assert captured_calls[0]["dishes"] == [
        {"name": "三丝炒米粉", "qty": 1},
        {"name": "滑蛋牛肉", "qty": 1},
        {"name": "爽口萝卜", "qty": 1},
    ]

    # 单菜场景下，搜索卡片仅展示无关招牌菜时同样不得覆盖用户点的「滑蛋牛肉」
    captured_calls.clear()
    r_single = stage_meituan_h5_order(sandbox_db, "示例粉面广东大排档(鸟巢店) 滑蛋牛肉", location_override="家")
    assert r_single["ok"] is True
    assert captured_calls[0]["dish"] == "滑蛋牛肉"
    assert captured_calls[0]["dishes"] is None


def test_mindex_preview_bill_multiline_night_shipping_fee() -> None:
    from siming.meituan_pay import parse_meituan_preview_bill

    txt = """外卖配送
示例小区A-1号楼 1016
乌(先生) 130****8837
立即送出
大约23:42送达
示例粉面广东大排档(鸟巢店)
三丝炒米粉
¥38
x1
滑蛋炒牛肉
¥68
x1
爽口萝卜
¥32
x1
打包费
¥6.9
配送费
夜间配送难度增大，配送费上调
¥9.2
美团红包
未选红包，最高25元可用
小计 ¥154.1
合计 ¥154.1
提交订单"""
    bill = parse_meituan_preview_bill(txt)
    assert bill["delivery_address"] == "示例小区A-1号楼 1016"
    assert bill["delivery_fee_yuan"] == 9.2
    assert bill["packing_fee_yuan"] == 6.9
    assert bill["final_payable_yuan"] == 154.1
    assert len(bill["items"]) == 3


def test_cli_meituan_plans_alias_preserves_speculative_batch(
    monkeypatch: Any, sandbox_db: SimingDB, capsys: Any
) -> None:
    import siming.cli as cli_mod
    from siming.db import iso_now

    batch_payload = {
        "batch_id": "SPEC-TEST-01",
        "created_at": iso_now(),
        "updated_at": iso_now(),
        "delivery_poi": "示例小区A-1号楼 1016",
        "entries": [
            {
                "index": 1,
                "shop": "示例粉面广东大排档(鸟巢店)",
                "status": "READY",
                "dishes": [{"name": "三丝炒米粉", "qty": 1, "price": 38.0}],
                "est_subtotal": 38.0,
                "delivery_fee": 6.0,
                "est_total": 44.0,
                "est_after_coupon": 39.0,
                "reason": "历史常点",
            }
        ],
    }
    sandbox_db.set_meta("meituan_speculative_batch", json.dumps(batch_payload, ensure_ascii=False))
    monkeypatch.setattr(cli_mod, "SimingDB", lambda *_a, **_k: sandbox_db)
    monkeypatch.setattr(
        "siming.meituan_agent.recommend_meituan_meals",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("should not overwrite batch via recommend")),
    )

    rc = cli_mod.main(["meituan", "看看方案"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "示例粉面广东大排档(鸟巢店)" in out
    saved = json.loads(sandbox_db.get_meta("meituan_speculative_batch") or "{}")
    assert saved.get("batch_id") == "SPEC-TEST-01"


def test_mask_keywords_and_spec_pack_scoring() -> None:
    from siming.meituan_cart_live import _dish_match_score, _dish_search_keywords
    from siming.router import _extract_order_location_override

    kws = _dish_search_keywords("海氏海诺一次性医用外科口罩 50只")
    assert "0只" not in kws
    assert any("口罩" in k for k in kws[:4])
    assert kws[0] == "海氏海诺一次"

    s50 = _dish_match_score("【整盒】海氏海诺一次性医用外科口罩 50只/盒熔喷布无菌三层防护口罩蓝色", "海氏海诺一次性医用外科口罩 50只")
    s30 = _dish_match_score("【30只装】海氏海诺 医用外科一次性口罩无菌口罩10只*3包/件", "海氏海诺一次性医用外科口罩 50只")
    s10 = _dish_match_score("【10只装】海氏海诺 一次性医用外科无菌口罩10只/包", "海氏海诺一次性医用外科口罩 50只")
    assert s50 > s30 > s10 >= 62.0

    loc, cleaned = _extract_order_location_override("再给我买一份口罩到示例小区A")
    assert loc == "家"
    assert "示例小区A" not in cleaned
    assert "口罩" in cleaned


def test_select_subtrade_address_waits_for_drawer_animation_before_clicking(monkeypatch: Any) -> None:
    import siming.meituan_cart_live as mcl

    monkeypatch.setattr(mcl, "_wait", lambda _s: None)
    monkeypatch.setattr(mcl, "_ensure_active", lambda _ws: None)

    state = {"poll": 0, "clicked_xy": [], "addr": "示例写字楼 10层"}

    def fake_body(_ws: Any) -> str:
        return f"外卖配送\n{state['addr']}\n示例生活超市（学院店）\n口罩\n单价:¥25.8\n数量:1\n合计\n¥25.8\n提交订单"

    def fake_click_text(_ws: Any, txt: str) -> dict[str, Any]:
        return {"ok": False}

    def fake_click_selector(_ws: Any, sel: str, index: int = 0) -> dict[str, Any]:
        return {"ok": True, "x": 205.0, "y": 78.0}

    def fake_eval_obj(_ws: Any, expr: str) -> dict[str, Any]:
        if "drawerSettled" in expr:
            state["poll"] += 1
            if state["poll"] <= 3:
                # 模拟抽屉仍在底部 translateY(600px) 视口外
                return {
                    "items": [
                        {"i": 0, "addr": "示例小区A公寓入口北侧美团外卖柜", "top": 827, "height": 62},
                        {"i": 1, "addr": "示例小区A-1号楼 1016", "top": 890, "height": 62},
                    ],
                    "drawerSettled": False,
                    "pcTop": 845,
                    "lbVisible": False,
                    "vh": 844,
                }
            # 第 4 次轮询抽屉滑入视口
            return {
                "items": [
                    {"i": 0, "addr": "示例小区A公寓入口北侧美团外卖柜", "top": 290, "height": 62},
                    {"i": 1, "addr": "示例小区A-1号楼 1016", "top": 352, "height": 62},
                ],
                "drawerSettled": True,
                "pcTop": 245,
                "lbVisible": False,
                "vh": 844,
            }
        # _measure_subtrade_address_item
        return {
            "ok": True,
            "inViewport": True,
            "hitOk": True,
            "hitCls": "p-address-list-item",
            "x": 160.0,
            "y": 383.0,
            "addr": "示例小区A-1号楼 1016",
        }

    def fake_click_xy(_ws: Any, x: float, y: float) -> None:
        state["clicked_xy"].append((x, y))
        assert 0 <= y <= 844, f"clicked offscreen y={y}"
        state["addr"] = "示例小区A-1号楼 1016"

    monkeypatch.setattr(mcl, "_body_text", fake_body)
    monkeypatch.setattr(mcl, "_click_text", fake_click_text)
    monkeypatch.setattr(mcl, "_click_selector", fake_click_selector)
    monkeypatch.setattr(mcl, "_eval_obj", fake_eval_obj)
    monkeypatch.setattr(mcl, "_click_xy", fake_click_xy)

    res = mcl._select_subtrade_address(object(), "示例小区A-1号楼 1016", "示例小区A")  # type: ignore[arg-type]
    assert res["ok"] is True
    assert res["address"] == "示例小区A-1号楼 1016"
    assert state["poll"] == 4
    assert state["clicked_xy"] == [(160.0, 383.0)]


def test_open_restaurant_prefers_matching_dish_tile_in_search_card(monkeypatch: Any) -> None:
    import siming.meituan_cart_live as mcl

    monkeypatch.setattr(mcl, "_wait", lambda _s: None)
    state = {"url": "https://h5.waimai.meituan.com/waimai/mindex/searchresults?keyword=mask", "clicks": []}

    monkeypatch.setattr(mcl, "_page_url", lambda _ws: state["url"])

    def fake_eval_obj(_ws: Any, expr: str) -> dict[str, Any]:
        if "shop_not_found" in expr:
            return {"ok": True, "idx": 0, "name": "叮当快药(北京知春路店)", "x": 150.0, "y": 120.0}
        if "dishes:" in expr or "shopNameTxt" in expr:
            return {
                "ok": True,
                "dishes": [
                    {"text": "[稳健医疗]医用外科口罩(长方形)(挂耳型)17*9cm*10只/包", "idx": 0},
                ],
            }
        if "targetText" in expr:
            return {"ok": True, "x": 124.0, "y": 294.0, "text": "[稳健医疗]医用外科口罩(长方形)(挂耳型)17*9cm*10只/包"}
        return {"ok": False}

    def fake_click_xy(_ws: Any, x: float, y: float) -> None:
        state["clicks"].append((x, y))
        if (x, y) == (124.0, 294.0):
            state["url"] = (
                "https://cactivityapi-sc.waimai.meituan.com/h5/sub-trade/restaurant/restaurant"
                "?source=searchresult&poi_id_str=Ozm1NAdRbFsjcNOHFVKHywI&spu_id=27267144684"
            )

    monkeypatch.setattr(mcl, "_eval_obj", fake_eval_obj)
    monkeypatch.setattr(mcl, "_click_xy", fake_click_xy)

    url, poi_id, kind = mcl._open_restaurant(object(), "叮当快药(北京知春路店)", target_dish="医用外科口罩")  # type: ignore[arg-type]
    assert poi_id == "Ozm1NAdRbFsjcNOHFVKHywI"
    assert kind == "subtrade"
    assert "spu_id=27267144684" in url
    assert state["clicks"] == [(124.0, 294.0)]


def test_parse_cart_preview_bill_pharmacy_xqty_format() -> None:
    txt = """外卖配送
到店自取
示例小区A-1号楼 1016
乌
先生
13700137000
立即送出
立即送出(大约16:08送达)
叮当快药(北京知春路店)
商家自配
[怡航]医用外科口罩(无菌平面挂耳型大号)10只/包
10只/包
x5
￥18
￥18
[怡航]医用外科口罩(无菌平面挂耳型大号)10只/包
10只/包
x1
￥3.4
￥3.6
打包费
￥0.5
配送费
¥9
美团红包
暂无可用
小计￥30.9
已优惠￥0.2
合计
¥30.9
提交订单"""
    bill = parse_cart_preview_bill(txt)
    assert bill["delivery_address"] == "示例小区A-1号楼 1016"
    assert bill["shop"] == "叮当快药(北京知春路店)"
    assert bill["final_payable_yuan"] == 30.9
    assert bill["packing_fee_yuan"] == 0.5
    assert bill["delivery_fee_yuan"] == 9.0
    assert len(bill["items"]) == 2
    assert sum(it["qty"] for it in bill["items"]) == 6
    assert bill["items"][0]["name"] == "[怡航]医用外科口罩(无菌平面挂耳型大号)10只/包"
    assert bill["items"][0]["spec"] == "10只/包"
    assert bill["items"][0]["line_total_yuan"] == 18.0
    assert bill["items"][1]["line_total_yuan"] == 3.4
