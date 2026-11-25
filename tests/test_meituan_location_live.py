"""美团外卖实时位置同步、附近在售商家解析与防异地历史误推回归测试。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

import yaml

from siming.context_loc import ingest_ios_location, resolve_current_location_context
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local
from siming.ledger.reconcile import match_or_record_wallet_row
from siming.meituan_agent import (
    _parse_meal_hint,
    parse_meituan_home_shops,
    parse_meituan_search_shops,
    recommend_meituan_meals,
    stage_meituan_h5_order,
    wgs84_to_gcj02,
)
from siming.router import handle_inbound_qq

HOME_FEED_ZHIZHEN = """示例写字楼
点击授权获取精确定位信息
开启定位才能看到附近的商家哦~
去开启
搜索
异国料理
小吃馆
家常菜
晚餐优选
荆九爷爆炒桥头三嫩（五道口店）
4.7分
月售1000+
人均 ¥66
起送 ¥39
配送 约¥3.3
32分钟
2.8km
五道口川菜好评榜第3名
大众点评高分店铺
常吃的店
已收藏
南京大牌档(西直门凯德店)
4.6分
月售9000+
人均 ¥35
起送 ¥40
配送 约¥9.2
48分钟
5.0km
西城区本帮江浙菜人气榜第1名
常吃的店
已收藏
姥姥家春饼店（牡丹园店）
4.5分
月售2000+
人均 ¥48
起送 ¥30
配送 约¥4.4
40分钟
1.5km
海淀区家常菜好评榜第1名
大众点评高分店铺
常吃的店
味多美·动物奶油·生日蛋糕（紫金数码园店）
4.6分
月售1000+
人均 ¥53
起送 ¥30
配送 约¥4.3
20分钟
1.6km
瑞幸咖啡(人工智能创业基地店)
4.5分
月售2000+
人均 ¥15
起送 ¥20
配送 约¥6
30分钟
801m
常吃的店
农耕记·湘菜小炒·盖码饭（六道口店）
4.9分
月售9000+
人均 ¥26
起送 ¥20
配送 约¥6
36分钟
4.1km
五道口湘菜人气榜第1名
外婆家·外婆送到家(北京京果大厦店)
4.7分
月售9999+
人均 ¥29
起送 ¥20
配送 约¥7.1
28分钟
3.6km
海淀区本帮江浙菜人气榜第2名
已收藏
叮当快药(北京知春路店)
4.9分
月售7000+
起送 ¥20
配送 约¥9
31分钟
850m
常吃的店
老巷烤肉拌饭（休息中店）
4.6分
月售500+
人均 ¥24
起送 ¥20
配送 约¥4
30分钟
1.2km
休息中
素坤泰(泰式打抛饭)
4.6分
月售1000+
人均 ¥37
起送 ¥15
配送 约¥7.9
48分钟
5.1km
常吃的店
正在加载...
首页
订单
我的
"""

HOME_FEED_XIXINGFU = """示例小区B
搜索
皖峰饭馆
4.7分
月售700+
人均 ¥77
起送 ¥60
配送 约¥5
20分钟
614m
北大地/万丰路北京菜复购榜第2名
大众点评高分店铺
常吃的店
邢记肉夹馍（总店）
4.9分
月售8000+
人均 ¥27
起送 ¥20
配送 约¥3.6
20分钟
734m
北京肉夹馍热销榜第1名
常吃的店
眉州东坡(丰台店)
4.7分
月售2000+
人均 ¥45
起送 ¥25
配送 约¥5.1
20分钟
2.1km
已收藏
正在加载...
首页
订单
我的
"""

SEARCH_FEED_APPLE_AMERICANO = """苹果美式
瑞幸咖啡(人工智能创业基地店)
4.5
月售
2000+
起送
¥
20
配送
¥
6
30分钟
801m
6.26折起
支持自取
票
苹果C美式
¥
24
加星气泡美式
¥
24
只此芳菲·茶咖叙
月售
30
起送
¥
29
配送
¥
3.6
20分钟
910m
青苹果薄荷美式
¥
36
¥
42
库迪咖啡（北京大钟寺融寓店）
4.7
月售
700+
起送
¥
0
配送
¥
4.4
29分钟
2.8km
苹果美式
¥
19.01
"""


def _write_regime_policy(db: SimingDB) -> None:
    db.paths.config_dir.mkdir(parents=True, exist_ok=True)
    pol = {
        "policy_version": 1,
        "daily_care": {
            "regimes": [
                {
                    "id": "primary_home",
                    "until": "2028-01-01",
                    "home_label": "示例小区A",
                    "home_lat": 39.904200,
                    "home_lon": 116.407400,
                    "office_label": "示例写字楼",
                    "office_lat": 39.908000,
                    "office_lon": 116.407000,
                },
                {
                    "id": "secondary_home",
                    "from": "2028-01-01",
                    "home_label": "示例小区B",
                    "home_lat": 39.8600,
                    "home_lon": 116.3200,
                    "office_label": "示例写字楼",
                    "office_lat": 39.908000,
                    "office_lon": 116.407000,
                },
            ]
        },
    }
    (db.paths.config_dir / "policy.yaml").write_text(yaml.safe_dump(pol, allow_unicode=True), encoding="utf-8")


def test_wgs84_to_gcj02_beijing_accuracy() -> None:
    lat_o, lng_o = wgs84_to_gcj02(39.908000, 116.407000)
    assert abs(lat_o - 39.909400) < 0.0005
    assert abs(lng_o - 116.413650) < 0.0005

    lat_n, lng_n = wgs84_to_gcj02(39.8600, 116.3200)
    assert abs(lat_n - 39.861200) < 0.0005
    assert abs(lng_n - 116.326110) < 0.0005


def test_parse_home_and_search_feeds() -> None:
    home_shops = parse_meituan_home_shops(HOME_FEED_ZHIZHEN)
    names = [s["shop"] for s in home_shops]
    assert "荆九爷爆炒桥头三嫩（五道口店）" in names
    assert "农耕记·湘菜小炒·盖码饭（六道口店）" in names
    assert "素坤泰(泰式打抛饭)" in names
    j9 = next(s for s in home_shops if "荆九爷" in s["shop"])
    assert j9["rating"] == 4.7
    assert j9["per_capita"] == 66.0
    assert j9["eta_min"] == 32
    assert j9["distance_str"] == "2.8km"
    assert j9["is_frequent"] is True
    assert j9["is_favorite"] is True
    assert "五道口川菜好评榜" in j9["rank_badge"]
    closed = next(s for s in home_shops if "休息中店" in s["shop"])
    assert closed["is_closed"] is True

    search_shops = parse_meituan_search_shops(SEARCH_FEED_APPLE_AMERICANO)
    assert len(search_shops) == 3
    assert search_shops[0]["shop"] == "瑞幸咖啡(人工智能创业基地店)"
    assert search_shops[0]["dishes"][0] == {"name": "苹果C美式", "price": 24.0}
    assert search_shops[1]["shop"] == "只此芳菲·茶咖叙"
    assert search_shops[1]["rating"] is None
    assert search_shops[1]["dishes"] == [{"name": "青苹果薄荷美式", "price": 36.0}]
    assert search_shops[2]["dishes"][0] == {"name": "苹果美式", "price": 19.01}


def test_recommend_uses_live_meituan_at_current_location_never_wanfeng(sandbox_db: SimingDB) -> None:
    _write_regime_policy(sandbox_db)
    # 写入大量「皖峰饭馆」历史订单（丰台区老店）+ 昨天刚吃过的「南京大牌档(中关村店)」+ 「素坤泰」
    for d in ("01", "03", "05", "07", "09", "11", "14"):
        match_or_record_wallet_row(
            sandbox_db,
            occurred_at=f"2026-11-{d}T18:45:00+08:00",
            amount_cents=10500,
            card_last4="",
            wallet_alias="微信",
            merchant=f"美团 · 皖峰饭馆-美团外卖App-2611{d}01",
            kind="消费",
        )
    for d in ("02", "06", "10"):
        match_or_record_wallet_row(
            sandbox_db,
            occurred_at=f"2026-11-{d}T18:30:00+08:00",
            amount_cents=7500,
            card_last4="",
            wallet_alias="微信",
            merchant=f"美团 · 素坤泰(泰式打抛饭)-美团外卖App-2611{d}02",
            kind="消费",
        )
    # 20 小时前刚在美团点过南京大牌档（写入 shop_order_items 验证两表联合 36h 防腻）
    yesterday_iso = iso_now(now_local() - timedelta(hours=20))
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO shop_order_items (
                item_key, platform, shop, ordered_at, order_status, order_amount_cents, item_name, qty, created_at
            ) VALUES ('k-nj-1', 'meituan', '南京大牌档(中关村店)', ?, '已完成', 9200, '美龄粥', 1, ?)
            """,
            (yesterday_iso, yesterday_iso),
        )

    # 1) 用户此刻 GPS 在公司（示例写字楼，OwnTracks 未带 Wi-Fi SSID）
    ingest_ios_location(
        sandbox_db,
        json.dumps({"wifi": "", "lat": 39.907995, "lng": 116.407021}),
        received_at=iso_now(),
    )
    loc_ctx = resolve_current_location_context(sandbox_db)
    assert loc_ctx["has_fresh"] is True
    assert loc_ctx["place_alias"] == "公司"

    captured_targets: list[tuple[dict[str, Any], str]] = []

    def _fake_live(target: dict[str, Any], kw: str) -> dict[str, Any]:
        captured_targets.append((target, kw))
        if target.get("gcj_lat") and float(target["gcj_lat"]) < 39.90:
            return {
                "ok": True,
                "gated": False,
                "delivery_poi": "示例小区B",
                "delivery_address": "3门501",
                "shops": parse_meituan_home_shops(HOME_FEED_XIXINGFU),
            }
        return {
            "ok": True,
            "gated": False,
            "delivery_poi": "示例写字楼",
            "delivery_address": "11层",
            "shops": parse_meituan_home_shops(HOME_FEED_ZHIZHEN),
        }

    rec = recommend_meituan_meals(sandbox_db, hint="我要点晚饭了，推荐点。", live_fetch_fn=_fake_live)
    assert rec["ok"] is True
    assert rec["live_verified"] is True
    assert rec["slot"] == "晚餐"
    assert captured_targets[0][1] == ""  # 自然问句被剥干净，未残余无效关键词
    assert abs(captured_targets[0][0]["gcj_lat"] - 39.9094) < 0.001

    pick_names = [p["shop"] for p in rec["picks"]]
    # 核心回归断言：绝对不能推荐当前位置送不到的「皖峰饭馆」！
    assert not any("皖峰" in n for n in pick_names)
    assert "皖峰" not in rec["reply"]
    # 晚餐绝不混入药房、蛋糕、纯咖啡、休息中门店
    assert not any(k in n for n in pick_names for k in ("快药", "味多美", "瑞幸", "休息中"))
    # 昨天吃过的南京大牌档（跨分店同品牌）自动 36h 防腻跳过
    assert not any("南京大牌档" in n for n in pick_names)
    assert any("南京大牌档" in s for s in rec["recent_skipped"])
    # 兼顾当前可送常吃店与附近高分新发现（如农耕记）
    assert any("荆九爷" in n or "姥姥家" in n or "素坤泰" in n for n in pick_names)
    assert any("农耕记" in n for n in pick_names)
    assert "示例写字楼" in rec["reply"]

    # 2) 当用户真的回到新房（示例小区B）时，实时定位同步过去后，皖峰饭馆才应当合法出现！
    ingest_ios_location(
        sandbox_db,
        json.dumps({"wifi": "", "lat": 39.8600, "lng": 116.3200}),
        received_at=iso_now(),
    )
    rec_ft = recommend_meituan_meals(sandbox_db, hint="我要点晚饭了，推荐点。", live_fetch_fn=_fake_live)
    assert rec_ft["live_verified"] is True
    assert rec_ft["delivery_poi"] == "示例小区B"
    assert any("皖峰饭馆" in p["shop"] for p in rec_ft["picks"])


def test_router_natural_dinner_phrasing_and_hint_cleanup(sandbox_db: SimingDB) -> None:
    now_dt = datetime(2026, 11, 19, 17, 55, tzinfo=SHANGHAI_TZ)
    p1 = _parse_meal_hint("我要点晚饭了，推荐点。", now_dt)
    assert p1["slot"] == "晚餐" and p1["keyword"] == "" and p1["max_budget"] is None
    p2 = _parse_meal_hint("我要点晚饭了，推荐点川菜 预算50", now_dt)
    assert p2["slot"] == "晚餐" and p2["keyword"] == "川菜" and p2["max_budget"] == 50.0

    r1 = handle_inbound_qq(sandbox_db, "我要点晚饭了，推荐点。")
    assert r1["handled"] is True
    assert "晚餐" in r1["reply"]
    # 离线模式必须诚实声明未能实时核验附近可配送商家
    assert "未能实时核验" in r1["reply"]

    r2 = handle_inbound_qq(sandbox_db, "我要点晚饭了 推荐点")
    assert r2["handled"] is True

    # 负例绝不误拦截
    assert handle_inbound_qq(sandbox_db, "晚饭吃完了")["handled"] is False
    assert handle_inbound_qq(sandbox_db, "推荐个开源项目")["handled"] is False


def test_recommend_gated_and_order_undeliverable_guard(sandbox_db: SimingDB) -> None:
    # 1) 遇到登录/验证码门禁：严禁拿历史订单冒充实时推荐
    gated_rec = recommend_meituan_meals(
        sandbox_db,
        hint="晚餐",
        live_fetch_fn=lambda _t, _kw: {
            "ok": False,
            "gated": True,
            "gate": {"gate_kind": "captcha_verification", "reason": "页面触发安全验证"},
        },
    )
    assert gated_rec["ok"] is False
    assert gated_rec["gated"] is True
    assert "绝不绕过验证码" in gated_rec["reply"]

    # 2) 指定点「皖峰饭馆」但当前定位搜出来的全是替代店 -> 诚实拦截不生成虚假 apply 卡
    undeliv = stage_meituan_h5_order(
        sandbox_db,
        "点外卖 皖峰饭馆",
        cdp_probe_fn=lambda _q: {
            "ok": True,
            "gated": False,
            "delivery_poi": "示例写字楼",
            "data": json.dumps(
                {
                    "delivery_poi": "示例写字楼",
                    "shops": [{"shop": "荆九爷爆炒桥头三嫩（五道口店）", "dishes": [{"name": "擂椒皮蛋", "price": 33.0}]}],
                    "top_lines": ["荆九爷爆炒桥头三嫩（五道口店）"],
                    "prices": ["¥33"],
                },
                ensure_ascii=False,
            ),
        },
    )
    assert undeliv["ok"] is False
    assert undeliv.get("undeliverable") is True
    assert "apply A" not in undeliv["reply"]
    assert "示例写字楼" in undeliv["reply"]

    # 3) 点「苹果美式」走美团实时检索，匹配「苹果C美式」且不被强转未配置的瑞幸官方 MCP
    ok_coffee = stage_meituan_h5_order(
        sandbox_db,
        "点外卖 苹果美式",
        cdp_probe_fn=lambda _q: {
            "ok": True,
            "gated": False,
            "delivery_poi": "示例写字楼",
            "data": json.dumps(
                {
                    "delivery_poi": "示例写字楼",
                    "shops": parse_meituan_search_shops(SEARCH_FEED_APPLE_AMERICANO),
                    "top_lines": ["瑞幸咖啡(人工智能创业基地店) · 4.5分 · 30分钟", "苹果C美式 ¥24"],
                    "prices": ["¥24"],
                },
                ensure_ascii=False,
            ),
        },
    )
    assert ok_coffee["ok"] is True
    assert "瑞幸咖啡(人工智能创业基地店)" in ok_coffee["reply"]
    assert "苹果C美式" in ok_coffee["reply"]
    assert "apply A" in ok_coffee["reply"]
