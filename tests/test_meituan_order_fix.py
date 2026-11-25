"""美团口语点餐（如「困了 给我点一杯苹果美式啥的」）、不劫持瑞幸官方 MCP、错误页拦截、真实加购反馈与登录态检测回归测试。"""

from __future__ import annotations

import json
from typing import Any

from siming.approval import verify_and_apply_approval
from siming.db import SimingDB
from siming.luckin_mcp import save_luckin_mcp_token
from siming.meituan_agent import (
    _score_order_dish_match,
    check_meituan_cdp_status,
    execute_meituan_lock_cart,
    extract_order_query_parts,
    parse_meituan_search_shops,
    stage_meituan_h5_order,
)
from siming.router import _extract_natural_meituan_order, handle_inbound_qq

APPLE_SEARCH_TEXT = """苹果美式
Peet's皮爷咖啡(中关村鼎好店)
4.8
月售
3000+
起送
¥
40
配送
¥
5
20分钟
895m
意式红心苹果美式
¥
33
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
苹果C美式
¥
24
全冰黑巧美式
¥
24
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


def test_extract_order_query_parts_and_fuzzy_dish_match() -> None:
    p1 = extract_order_query_parts("困了 给我点一杯苹果美式啥的")
    assert p1["shop_hint"] == ""
    assert p1["dish_hint"] == "苹果美式"
    assert p1["search_kw"] == "苹果美式"
    assert p1["spec_prefs"] == []

    p2 = extract_order_query_parts("给我点一杯大杯冰苹果美式不加糖")
    assert p2["dish_hint"] == "苹果美式"
    assert set(p2["spec_prefs"]) == {"大杯", "冰", "不另外加糖"}

    # 短常用名「冰美式」不被破坏成「美式」，同时保留冰规格
    p3 = extract_order_query_parts("点一杯冰美式")
    assert p3["dish_hint"] == "冰美式"
    assert "冰" in p3["spec_prefs"]

    p4 = extract_order_query_parts("瑞幸苹果美式")
    assert p4["shop_hint"] == "瑞幸"
    assert p4["dish_hint"] == "苹果美式"

    p5 = extract_order_query_parts("点外卖 素坤泰 打抛饭 微辣")
    assert p5["shop_hint"] == "素坤泰"
    assert p5["dish_hint"] == "打抛饭"
    assert p5["spec_prefs"] == ["微辣"]

    assert _score_order_dish_match("苹果C美式", "苹果美式") >= 8.5
    assert _score_order_dish_match("红苹果气泡冰美式", "苹果美式") >= 7.0
    assert _score_order_dish_match("擂椒皮蛋", "苹果美式") < 2.0


def test_stage_meituan_order_never_hijacks_luckin_and_prefers_deliverable_min_order(sandbox_db: SimingDB) -> None:
    shops = parse_meituan_search_shops(APPLE_SEARCH_TEXT)

    def _probe(q: str) -> dict[str, Any]:
        return {
            "ok": True,
            "gated": False,
            "delivery_poi": "示例写字楼 10层",
            "shops": shops,
            "data": json.dumps({"delivery_poi": "示例写字楼 10层", "shops": shops}, ensure_ascii=False),
        }

    # 1) 显式带「瑞幸」走美团点餐，绝不劫持去要求配置瑞幸官方 MCP Token
    r_lk = stage_meituan_h5_order(sandbox_db, "瑞幸 苹果美式", cdp_probe_fn=_probe)
    assert r_lk["ok"] is True
    assert "open.lkcoffee.com" not in r_lk["reply"]
    assert r_lk["shop"] == "瑞幸咖啡(人工智能创业基地店)"
    assert r_lk["dish_name"] == "苹果C美式"
    assert r_lk["est_yuan"] == 24.0
    assert "apply A" in r_lk["reply"]

    # 2) 口语「困了 给我点一杯苹果美式啥的」：即使皮爷排第 1，但单杯 ¥33 < 起送 ¥40，应优先挑满起送且近距的瑞幸 ¥24
    r_sleepy = stage_meituan_h5_order(sandbox_db, "困了 给我点一杯苹果美式啥的", cdp_probe_fn=_probe)
    assert r_sleepy["ok"] is True
    assert r_sleepy["shop"] == "瑞幸咖啡(人工智能创业基地店)"
    assert r_sleepy["dish_name"] == "苹果C美式"
    assert "示例写字楼 10层" in r_sleepy["reply"]
    assert "备选参考" in r_sleepy["reply"]


def test_stage_meituan_rejects_h5_error_page_without_fake_approval(sandbox_db: SimingDB) -> None:
    # 1) reason == page_error
    r1 = stage_meituan_h5_order(
        sandbox_db,
        "苹果美式",
        cdp_probe_fn=lambda _q: {"ok": False, "reason": "page_error", "error": "参数错误 错误码: H1#2JC8#CNDD4G"},
    )
    assert r1["ok"] is False
    assert r1.get("page_error") is True
    assert "apply A" not in r1["reply"]

    # 2) 错误页文字藏在 legacy top_lines 中也必须拦截，绝不伪造确认卡
    r2 = stage_meituan_h5_order(
        sandbox_db,
        "苹果美式",
        cdp_probe_fn=lambda _q: {
            "ok": True,
            "gated": False,
            "data": json.dumps(
                {"top_lines": ["中关村-34号楼", "参数错误", "刷新试试", "参数错误 错误码: H1#2JC8#CNDD4G"], "prices": []},
                ensure_ascii=False,
            ),
        },
    )
    assert r2["ok"] is False
    assert r2.get("page_error") is True
    assert "apply A" not in r2["reply"]


def test_execute_meituan_lock_cart_reports_real_cart_summary(sandbox_db: SimingDB) -> None:
    shops = parse_meituan_search_shops(APPLE_SEARCH_TEXT)
    staged = stage_meituan_h5_order(
        sandbox_db,
        "困了 给我点一杯大杯苹果美式不加糖",
        cdp_probe_fn=lambda _q: {
            "ok": True,
            "gated": False,
            "delivery_poi": "示例写字楼 10层",
            "shops": shops,
            "data": json.dumps({"delivery_poi": "示例写字楼 10层", "shops": shops}, ensure_ascii=False),
        },
    )
    assert staged["ok"] is True
    tok = staged["approval_token"]

    fake_cart_res = {
        "ok": True,
        "gated": False,
        "added_to_cart": True,
        "shop": "瑞幸咖啡(人工智能创业基地店)",
        "dish": "苹果C美式",
        "cart_count": 1,
        "payable_text": "¥21.5",
        "delivery_text": "另需配送费约￥6",
        "checkout_status": "去结算",
        "selected_specs": "默认、大杯、冰、不另外加糖",
        "delivery_poi": "示例写字楼 10层",
    }
    applied = verify_and_apply_approval(
        sandbox_db,
        tok,
        extra_executors={
            "meituan_lock_cart": lambda p, d: execute_meituan_lock_cart(
                p, d, cdp_stage_fn=lambda _s, _i: fake_cart_res
            )
        },
    )
    assert applied["ok"] is True
    assert "加入美团购物车" in applied["summary"]
    assert "购物车 1 件" in applied["summary"]
    assert "¥21.5" in applied["summary"]
    assert "去结算" in applied["summary"]
    assert "示例写字楼 10层" in applied["summary"]
    assert "imeituan://" in applied["summary"]


def test_router_natural_coffee_order_and_luckin_fallback(sandbox_db: SimingDB, monkeypatch: Any) -> None:
    # 1) 口语解析正负例
    assert _extract_natural_meituan_order("困了 给我点一杯苹果美式啥的") == "苹果美式"
    assert _extract_natural_meituan_order("给我点一杯冰美式") == "冰美式"
    assert _extract_natural_meituan_order("饿了，帮我点一份打抛饭") == "打抛饭"
    assert _extract_natural_meituan_order("帮我点一下按钮") is None
    assert _extract_natural_meituan_order("给我点建议") is None
    assert _extract_natural_meituan_order("来一份月度报告") is None
    assert _extract_natural_meituan_order("苹果美式好喝吗？") is None
    assert _extract_natural_meituan_order("qq上我提了困了给我点一杯苹果美式，没有成功，修复") is None

    # 2) 「困了 给我点一杯苹果美式啥的」直达美团点餐，生成 apply 确认卡
    r_nat = handle_inbound_qq(sandbox_db, "困了 给我点一杯苹果美式啥的")
    assert r_nat["handled"] is True
    assert "美团外卖点餐确认卡" in r_nat["reply"]
    assert "open.lkcoffee.com" not in r_nat["reply"]

    # 3) 未配瑞幸官方 Token 时，「来杯瑞幸 苹果美式」自动回落走美团，不再逼用户配 Token
    r_lk_fb = handle_inbound_qq(sandbox_db, "来杯瑞幸 苹果美式")
    assert r_lk_fb["handled"] is True
    assert "美团外卖点餐确认卡" in r_lk_fb["reply"]
    assert "open.lkcoffee.com" not in r_lk_fb["reply"]

    # 4) 已配瑞幸官方 Token 时，「来杯瑞幸 大杯冰美式」走瑞幸官方 MCP 通道
    save_luckin_mcp_token(sandbox_db, "Bearer test_token_luckin_123456")
    called_luckin: list[str] = []
    monkeypatch.setattr(
        "siming.luckin_mcp.stage_luckin_order",
        lambda _db, spec: (called_luckin.append(spec), {"ok": True, "reply": f"LUCKIN_MCP:{spec}"})[1],
    )
    r_lk_mcp = handle_inbound_qq(sandbox_db, "来杯瑞幸 大杯冰美式")
    assert r_lk_mcp["handled"] is True
    assert r_lk_mcp["reply"].startswith("LUCKIN_MCP:")
    assert called_luckin


def test_check_meituan_cdp_status_detects_cookie_session_without_open_tab(monkeypatch: Any) -> None:
    monkeypatch.setattr(
        "siming.meituan_agent.resolve_chromium_cdp_base",
        lambda port=9222: {"http_base": "http://127.0.0.1:9222", "ws_browser": "ws://127.0.0.1:9222/devtools/browser/x"},
    )
    monkeypatch.setattr("siming.meituan_agent.list_chromium_tabs", lambda port=9222: [])
    monkeypatch.setattr("siming.meituan_agent.has_domain_session_cookies", lambda *a, **kw: True)

    st = check_meituan_cdp_status()
    assert st["cdp_online"] is True
    assert st["has_meituan_tab"] is False
    assert st["has_meituan_session"] is True
