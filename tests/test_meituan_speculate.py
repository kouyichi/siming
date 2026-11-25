"""美团外卖并行投机菜品推演与确认即执行预操作测试。"""

from __future__ import annotations

import json
import threading
import time
from typing import Any

from siming.db import SimingDB, iso_now, now_local
from siming.meituan_agent import recommend_meituan_meals, stage_meituan_h5_order
from siming.meituan_speculate import (
    confirm_and_execute_speculative_pick,
    create_speculative_batch,
    format_speculative_plans_overview,
    infer_dishes_for_shop,
    load_active_speculative_batch,
    parse_meituan_menu_cards,
    run_speculative_meal_preplan,
)
from siming.router import handle_inbound_qq

SAMPLE_NONGENGJI_CARDS = [
    {"idx": 0, "hasPlus": False, "lines": ["【大团聚】5-6人套餐", "5人份", "月售5", "¥218¥288"]},
    {"idx": 1, "hasPlus": True, "lines": ["【每日限量20份】原味酸奶（仅限1份）", "1人份", "月售400", "¥0.99¥1.99", "限1份"]},
    {"idx": 2, "hasPlus": True, "lines": ["青椒火腿炒鸡蛋盖码饭", "1人份", "月售800", "¥28.9"]},
    {"idx": 3, "hasPlus": True, "lines": ["农家一碗香盖码饭", "1人份", "月售200", "¥33.9"]},
    {"idx": 4, "hasPlus": True, "lines": ["已售罄招牌红烧肉", "1人份", "已抢光", "月售900", "¥45"]},
    {"idx": 5, "hasPlus": True, "lines": ["需要餐具包（筷勺+纸巾）", "月售1000", "¥0.1"]},
    {"idx": 6, "hasPlus": True, "lines": ["五常米饭", "1人份", "月售900", "¥4"]},
    {"idx": 7, "hasPlus": True, "lines": ["清炒时蔬（小份）", "1人份", "月售300", "¥14"]},
]

SAMPLE_SUKUNTAI_CARDS = [
    {"idx": 0, "hasPlus": True, "lines": ["招牌打抛饭（鸡肉）", "1人份", "月售1200", "¥39"]},
    {"idx": 1, "hasPlus": True, "lines": ["舂荷包蛋", "1人份", "月售600", "¥16"]},
    {"idx": 2, "hasPlus": True, "lines": ["冬阴功薯条", "1人份", "月售500", "¥18"]},
    {"idx": 3, "hasPlus": True, "lines": ["泰式奶茶", "1人份", "月售700", "¥19"]},
]


def test_parse_menu_cards_and_infer_solo_and_history_dishes(sandbox_db: SimingDB) -> None:
    parsed_ngj = parse_meituan_menu_cards(SAMPLE_NONGENGJI_CARDS, "预估加配送费 ¥4.9\n¥15起送")
    assert parsed_ngj["min_order"] == 15.0
    assert parsed_ngj["delivery_fee"] == 4.9

    # 1) 新店推演：剔除 5 人套餐 / ¥0.99 限购酸奶 / 售罄菜 / 餐具，命中热销一键直加单人主食
    inf_new = infer_dishes_for_shop("农耕记（六道口店）", parsed_ngj, history_combo=None, cur_slot="晚餐")
    assert inf_new["ok"] is True
    assert inf_new["plan_source"] == "live_menu_signature"
    dish_names = [d["name"] for d in inf_new["dishes"]]
    assert "青椒火腿炒鸡蛋盖码饭" in dish_names
    assert not any("5-6人" in n or "酸奶" in n or "售罄" in n or "餐具" in n for n in dish_names)
    assert inf_new["est_subtotal"] == 28.9
    assert inf_new["est_total"] == 33.8

    # 2) 起送价补齐：若门店 ¥40 起送，自动补配小菜/米饭凑满起送价
    parsed_high_min = parse_meituan_menu_cards(SAMPLE_NONGENGJI_CARDS, "预估加配送费 ¥5\n¥40起送")
    inf_topup = infer_dishes_for_shop("农耕记高起送测试", parsed_high_min, history_combo=None, cur_slot="晚餐")
    assert inf_topup["ok"] is True
    assert inf_topup["meets_min_order"] is True
    assert inf_topup["est_subtotal"] >= 40.0
    assert len(inf_topup["dishes"]) >= 2

    # 3) 熟店推演：写入历史订单（含餐具噪音与常点菜），核验优先沿用历史常点并在实时菜单核价
    ts = iso_now()
    with sandbox_db.transaction() as conn:
        for k, nm in (("s1", "招牌打抛饭🌶️（鸡肉）"), ("s2", "舂荷包蛋"), ("s3", "需要餐具")):
            conn.execute(
                """
                INSERT INTO shop_order_items (
                    item_key, platform, shop, ordered_at, order_status, order_amount_cents, item_name, qty, created_at
                ) VALUES (?, 'meituan', '素坤泰(泰式打抛饭)', ?, '已完成', 5500, ?, 1, ?)
                """,
                (k, ts, nm, ts),
            )
    from siming.meituan_speculate import load_shop_historical_dish_combos

    hist = load_shop_historical_dish_combos(sandbox_db, "素坤泰(泰式打抛饭)")
    assert hist["has_history"] is True
    assert "餐具" not in hist["preview"]

    parsed_skt = parse_meituan_menu_cards(SAMPLE_SUKUNTAI_CARDS, "预估加配送费 ¥6\n¥15起送")
    inf_hist = infer_dishes_for_shop("素坤泰(泰式打抛饭)", parsed_skt, history_combo=hist, cur_slot="晚餐")
    assert inf_hist["ok"] is True
    assert inf_hist["plan_source"] == "history_verified_live"
    assert [d["name"] for d in inf_hist["dishes"]] == ["招牌打抛饭（鸡肉）", "舂荷包蛋"]
    assert inf_hist["est_subtotal"] == 55.0
    assert inf_hist["est_total"] == 61.0


def test_parallel_speculative_preplan_fast_stage_and_direct_confirm(sandbox_db: SimingDB) -> None:
    picks = [
        {"shop": "素坤泰(泰式打抛饭)", "avg_yuan": 55.0, "rating": 4.6, "eta_min": 40, "distance_str": "4.0km"},
        {"shop": "农耕记·湘菜小炒·盖码饭（六道口店）", "avg_yuan": 32.0, "rating": 4.9, "eta_min": 33, "distance_str": "3.8km"},
        {"shop": "验证门禁测试店", "avg_yuan": 40.0, "rating": 4.5, "eta_min": 30, "distance_str": "2.0km"},
    ]
    batch = create_speculative_batch(
        sandbox_db,
        picks=picks,
        delivery_poi="示例写字楼 10层",
        cur_slot="晚餐",
        max_budget=None,
        loc_target={"alias": "公司", "label": "示例写字楼", "gcj_lat": 39.9094, "gcj_lng": 116.4136},
    )
    assert batch["status"] == "RUNNING"

    # 验证真正并行执行（active_workers 峰值 >= 2）且单家门禁不影响其余店铺
    active_lock = threading.Lock()
    active_now = 0
    peak_parallel = 0

    def _mock_inspect(shop_name: str, _loc: dict[str, Any]) -> dict[str, Any]:
        nonlocal active_now, peak_parallel
        with active_lock:
            active_now += 1
            peak_parallel = max(peak_parallel, active_now)
        time.sleep(0.08)
        try:
            if "门禁" in shop_name:
                return {"ok": False, "gated": True, "gate": {"reason": "需要滑块验证"}}
            if "农耕记" in shop_name:
                return {
                    "ok": True,
                    "live_shop_name": "农耕记·湘菜小炒·盖码饭（六道口二店）",
                    "menu_url": "https://h5.waimai.meituan.com/waimai/mindex/menu?poi_id_str=NGJ123",
                    **parse_meituan_menu_cards(SAMPLE_NONGENGJI_CARDS, "预估加配送费 ¥4.9\n¥15起送"),
                }
            return {
                "ok": True,
                "live_shop_name": "素坤泰(泰式打抛饭)",
                "menu_url": "https://h5.waimai.meituan.com/waimai/mindex/menu?poi_id_str=SKT456",
                **parse_meituan_menu_cards(SAMPLE_SUKUNTAI_CARDS, "预估加配送费 ¥6\n¥15起送"),
            }
        finally:
            with active_lock:
                active_now -= 1

    res = run_speculative_meal_preplan(sandbox_db, batch_id=batch["batch_id"], inspect_shop_fn=_mock_inspect, max_workers=3)
    assert res["ok"] is True
    assert res["ready_count"] == 2
    assert peak_parallel >= 2

    loaded = load_active_speculative_batch(sandbox_db)
    assert loaded is not None and loaded["status"] == "READY"

    # 1) QQ「看看方案」0ms 展示全部推演结果
    r_ov = handle_inbound_qq(sandbox_db, "看看方案")
    assert r_ov["handled"] is True
    assert "青椒火腿炒鸡蛋盖码饭" in r_ov["reply"]
    assert "招牌打抛饭（鸡肉）" in r_ov["reply"]
    assert "验证门禁" in r_ov["reply"]

    # 2) 「第2家」0ms 命中投机预案生成含精确菜品与直达 menu_url 的 Action Card
    staged = stage_meituan_h5_order(sandbox_db, "第2家")
    assert staged["ok"] is True
    assert staged.get("speculative_hit") is True
    assert "青椒火腿炒鸡蛋盖码饭" in staged["reply"]
    assert "apply A" in staged["reply"]

    # 3) 用户直接发「确认第2家」→ 立即走 A2 审批审计并执行预推加购，零重复搜索！
    executed_plans: list[dict[str, Any]] = []

    def _fake_cart_exec(plan: dict[str, Any]) -> dict[str, Any]:
        executed_plans.append(plan)
        return {
            "ok": True,
            "added_to_cart": True,
            "shop": plan["shop"],
            "added_dishes": [d["name"] for d in plan["dishes"]],
            "cart_count": len(plan["dishes"]),
            "payable_text": "¥28.9",
            "delivery_text": "配送费约¥4.9",
            "checkout_status": "去结算",
            "delivery_poi": plan["delivery_poi"],
        }

    cf = confirm_and_execute_speculative_pick(sandbox_db, "第2家", cdp_exec_fn=_fake_cart_exec)
    assert cf["ok"] is True
    assert len(executed_plans) == 1
    assert executed_plans[0]["menu_url"] == "https://h5.waimai.meituan.com/waimai/mindex/menu?poi_id_str=NGJ123"
    assert executed_plans[0]["dishes"][0]["name"] == "青椒火腿炒鸡蛋盖码饭"
    assert "加入美团购物车" in cf["reply"]
    assert "去结算" in cf["reply"]

    # 4) 负例：非点餐确认语句绝不被投机确认路由误拦截
    assert handle_inbound_qq(sandbox_db, "确认一下明天的会议")["handled"] is False
    assert handle_inbound_qq(sandbox_db, "确认第3家")["handled"] is True  # 第3家触发门禁应给出合规提示而非执行
    assert "门禁" in handle_inbound_qq(sandbox_db, "确认第3家")["reply"]


SAMPLE_ACCOUNT_REDPACKETS = """美团红包
18张券
吃喝玩乐神券
¥
5
有效期至2026.12.15
无门槛
限非到店自取订单，天天神券商家使用。
去使用
闪购暖衣季服饰神券
¥
70
有效期至2026.11.20
满279可用
限非到店自取订单，闪购部分服饰商家使用。
去使用
专属大额神券
¥
22
有效期至2026.11.20
满88可用
限商品现价+打包费-所有优惠金额满88元使用（不含配送费）。限非到店自取订单，北京市地区，部分神券活动商家使用。
去使用
闪购便利店神券
¥
20
有效期至2026.11.20
满59可用
限非到店自取订单，便利店神券商家使用。
去使用
外卖大额神券
¥
16
有效期至2026.11.20 17:48:46
满67可用
限商品现价+打包费-所有优惠金额满67元使用（不含配送费）。限非到店自取订单，神券活动商家使用。
去使用
外卖大额神券
¥
14
有效期至2026.11.20 17:48:46
满57可用
限商品现价+打包费-所有优惠金额满57元使用（不含配送费）。限非到店自取订单，神券活动商家使用。
去使用
外卖神券
¥
10
有效期至2026.11.20 17:48:46
满35可用
限商品现价+打包费-所有优惠金额满35元使用（不含配送费）。限非到店自取订单，神券活动商家使用。
去使用
"""

SAMPLE_STORE_VOUCHERS = """商家代金券
大牌健康守护券
¥
10
有效期至2026.11.25 23:59
满49可用
去使用
农耕记·湘菜小炒·盖码饭（六道口二店）
¥
3
有效期至2026.11.26 23:59
满25可用
可与其他活动优惠同时享受，在线支付专享。限指定门店使用。
去使用
"""


def test_coupon_parsing_stacking_and_discounted_pricing_in_speculate(sandbox_db: SimingDB) -> None:
    from datetime import datetime
    from siming.db import SHANGHAI_TZ
    from siming.meituan_speculate import (
        compute_coupon_pricing,
        parse_meituan_account_redpackets,
        parse_meituan_store_vouchers,
        parse_store_promos,
    )

    dt_now = datetime(2026, 11, 19, 20, 30, tzinfo=SHANGHAI_TZ)
    plats = parse_meituan_account_redpackets(SAMPLE_ACCOUNT_REDPACKETS, now_dt=dt_now)
    titles = [c["title"] for c in plats]
    # 严禁混入闪购服饰/闪购便利店等非餐饮券！
    assert not any("闪购" in t or "服饰" in t or "便利店" in t for t in titles)
    assert any(c["amount"] == 22.0 and c["threshold"] == 88.0 for c in plats)
    assert any(c["amount"] == 10.0 and c["threshold"] == 35.0 for c in plats)
    assert any(c["amount"] == 5.0 and c["threshold"] == 0.0 for c in plats)

    vouchers = parse_meituan_store_vouchers(SAMPLE_STORE_VOUCHERS, now_dt=dt_now)
    assert len(vouchers) == 1
    assert "农耕记" in vouchers[0]["shop"]
    assert vouchers[0]["amount"] == 3.0

    # 门店优惠解析：正确识别满减与店内可领券，且不把「返满88减3」误算成当单满减
    promos = parse_store_promos("优惠\n¥\n3\n领\n50减2\n领2元券\n实际支付0元返满88减3商家代金券")
    assert promos["manjian"] == [{"threshold": 50.0, "discount": 2.0}]
    assert any(c["amount"] == 3.0 for c in promos["claimable_coupons"])
    assert any("返满88减3" in r for r in promos["rebates"])

    acc_obj = {"platform_coupons": plats, "store_vouchers": vouchers}

    # 1) 荆九爷 餐品 ¥93 + 配送 ¥3.3 -> 命中 满88减22，券后 ¥74.3
    cp_j9 = compute_coupon_pricing("荆九爷爆炒桥头三嫩（五道口店）", subtotal=93.0, delivery_fee=3.3, account_coupons=acc_obj, now_dt=dt_now)
    assert cp_j9["total_discount"] == 22.0
    assert cp_j9["after_coupon_total"] == 74.3

    # 2) 姥姥家 餐品 ¥65 + 配送 ¥4.4 -> 命中 满57减14（券后 ¥55.4）且提示差 ¥2 可换满67减16
    cp_ll = compute_coupon_pricing("姥姥家春饼店（牡丹园店）", subtotal=65.0, delivery_fee=4.4, account_coupons=acc_obj, now_dt=dt_now)
    assert cp_ll["total_discount"] == 14.0
    assert cp_ll["after_coupon_total"] == 55.4
    assert "67减16" in cp_ll["near_coupon_tip"]

    # 3) 农耕记 餐品 ¥28.9 + 配送 ¥4.9 -> 叠加商家代金券(满25减3) + 无门槛¥5神券 = 共省¥8 -> 券后 ¥25.8
    cp_ngj = compute_coupon_pricing(
        "农耕记·湘菜小炒·盖码饭（六道口二店）",
        subtotal=28.9,
        delivery_fee=4.9,
        store_promos=promos,
        account_coupons=acc_obj,
        now_dt=dt_now,
    )
    assert cp_ngj["store_voucher_off"] == 3.0
    assert cp_ngj["platform_coupon_off"] == 5.0
    assert cp_ngj["total_discount"] == 8.0
    assert cp_ngj["after_coupon_total"] == 25.8

    # 4) 端到端串联投机推演 -> 看看方案 / 第1家 / 确认第1家 均完整带券后预估价
    picks = [{"shop": "农耕记·湘菜小炒·盖码饭（六道口店）", "avg_yuan": 32.0, "rating": 4.9, "eta_min": 30, "distance_str": "3.8km"}]
    batch = create_speculative_batch(
        sandbox_db,
        picks=picks,
        delivery_poi="示例写字楼 10层",
        cur_slot="晚餐",
        max_budget=None,
        loc_target={"alias": "公司", "label": "示例写字楼"},
    )
    run_speculative_meal_preplan(
        sandbox_db,
        batch_id=batch["batch_id"],
        inspect_shop_fn=lambda _s, _l: {
            "ok": True,
            "live_shop_name": "农耕记·湘菜小炒·盖码饭（六道口二店）",
            "menu_url": "https://h5.waimai.meituan.com/waimai/mindex/menu?poi_id_str=NGJ123",
            "store_promos": promos,
            **parse_meituan_menu_cards(SAMPLE_NONGENGJI_CARDS, "预估加配送费 ¥4.9\n¥15起送"),
        },
        fetch_coupons_fn=lambda: acc_obj,
        max_workers=2,
    )
    ov = format_speculative_plans_overview(sandbox_db)
    assert "券后预估 **¥25.8**" in ov
    assert "省¥8" in ov
    assert "满88减22" in ov

    card = stage_meituan_h5_order(sandbox_db, "第1家")
    assert card["ok"] is True
    assert "券后预估 **¥25.8**" in card["reply"]
    assert card["est_yuan"] == 25.8

    # 5) 历史订单仅命中低价配菜/小料（无主菜）时，自动从实时菜单补配单人主菜，绝不产出 ¥3.1 纯小料单！
    topping_only_cards = [
        {"idx": 0, "hasPlus": True, "lines": ["吸汁兰花干1串（多点不送）", "1人份", "月售500", "¥2.9"]},
        {"idx": 1, "hasPlus": True, "lines": ["手工酸萝卜1份（只限购一份）", "1人份", "月售900", "¥0.2"]},
        {"idx": 2, "hasPlus": True, "lines": ["招牌大骨螺蛳粉", "1人份", "月售1500", "¥26.8"]},
    ]
    inf_side = infer_dishes_for_shop(
        "肥姨妈·大骨螺蛳粉",
        parse_meituan_menu_cards(topping_only_cards, "预估加配送费 ¥5.7\n¥15起送"),
        history_combo={"has_history": True, "last_combo": [{"name": "吸汁兰花干1串", "qty": 1}, {"name": "手工酸萝卜1份", "qty": 1}]},
        cur_slot="晚餐",
    )
    assert inf_side["ok"] is True
    assert inf_side["dishes"][0]["name"] == "招牌大骨螺蛳粉"
    assert inf_side["est_subtotal"] >= 26.8

