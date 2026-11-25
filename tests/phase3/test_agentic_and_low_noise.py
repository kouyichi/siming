"""验证用户最新拍板的 4 项核心升级：
1. 电车（纯电绿牌）配置：晨札与打车提示自动显示「电车不限行」，不再误报工作日尾号限行；
2. 银行卡彻底降噪：凌晨 ¥9.90 / 白天 ¥3227.58 一律静默落库并整合进晨札「近24h动账整合」，仅单笔 ≥¥20,000（或 ≥$2,000）超大额才实时推送；
3. 滴滴官方 MCP 代叫车闭环：API Key 鉴权 -> maps_textsearch -> taxi_estimate -> 生成 apply Axx -> 确认后调 taxi_create_order 发单 + 状态查询/撤单；
4. 美团外卖口味画像智能推荐：剥离订单号后缀、过滤单车/充电宝/超市噪音、自动避开近 36h 刚吃过的店（除非「再来一单」）。
"""

from __future__ import annotations

from typing import Any

from siming.approval import verify_and_apply_approval
from siming.db import SimingDB, now_local
from siming.didi_mcp import execute_didi_create_order, query_or_cancel_didi_order, request_didi_ride, save_didi_mcp_key
from siming.evald import consume_pending_effects
from siming.ledger.reconcile import match_or_record_wallet_row
from siming.ledger.sms_parser import try_ingest_bank_sms
from siming.meituan_agent import recommend_meituan_meals
from siming.morning import build_morning_editions
from siming.router import handle_inbound_qq


def test_ev_exempt_header_and_bank_sms_noise_consolidation(sandbox_db: SimingDB) -> None:
    # 1. 凌晨 04:07 的 ¥9.90 与白天 ¥3227.58 普通/大额消费：必须 0 条实时推送，全部静默整合进晨札！
    r_small = try_ingest_bank_sms(
        sandbox_db,
        "您尾号1004的储蓄卡账户消费人民币9.90元。[建设银行]",
        occurred_at=now_local().isoformat(timespec="seconds"),
    )
    assert r_small["matched"] is True

    r_mid = try_ingest_bank_sms(
        sandbox_db,
        "【中国银行】您尾号1003的借记卡消费人民币3227.58元。",
        occurred_at=now_local().isoformat(timespec="seconds"),
    )
    assert r_mid["matched"] is True
    consume_pending_effects(sandbox_db, include_workers=False)

    conn = sandbox_db.connect()
    sent_rows = conn.execute("SELECT * FROM outbox WHERE rule_id IN ('bank_sms_alert', 'reimburse_cursor')").fetchall()
    conn.close()
    assert len(sent_rows) == 0  # 零噪音打扰！

    # 2. 晨札自动展示「电车不限行」与「近24h动账整合」一行汇总
    editions = build_morning_editions(sandbox_db)
    assert "电车不限行" in editions["concise"]
    assert "近24h动账整合：人民币支出 2 笔共 ¥3237.48" in editions["concise"]

    # 3. 只有单笔 ≥¥20,000 超大额消费/划转才触发实时提醒！
    r_super = try_ingest_bank_sms(
        sandbox_db,
        "【中国银行】您尾号1003的借记卡消费人民币26750.65元。",
        occurred_at=now_local().isoformat(timespec="seconds"),
    )
    assert r_super["matched"] is True
    consume_pending_effects(sandbox_db, include_workers=False)

    conn = sandbox_db.connect()
    super_rows = conn.execute("SELECT text FROM outbox WHERE rule_id='bank_sms_alert'").fetchall()
    conn.close()
    assert len(super_rows) == 1
    assert "超大额动账提醒" in super_rows[0]["text"]
    assert "26750.65" in super_rows[0]["text"]


def test_didi_mcp_estimate_and_create_order_hitl(sandbox_db: SimingDB) -> None:
    save_res = save_didi_mcp_key(sandbox_db, "didi_mcp_test_secret_key_8888")
    assert save_res["ok"] is True

    calls: list[tuple[str, dict[str, Any]]] = []

    class FakeDidiSession:
        def __init__(self, base_url: str, api_key: str) -> None:
            self.base_url = base_url
            self.api_key = api_key

        def initialize_and_list_tools(self) -> list[dict[str, Any]]:
            return [
                {"name": "maps_textsearch", "inputSchema": {"properties": {"keywords": {}, "city": {}}}},
                {"name": "taxi_estimate", "inputSchema": {"properties": {"origin": {}, "destination": {}, "city": {}}}},
                {
                    "name": "taxi_create_order",
                    "inputSchema": {"properties": {"origin": {}, "destination": {}, "trace_id": {}, "product_id": {}}},
                },
                {"name": "taxi_query_order", "inputSchema": {"properties": {"order_id": {}}}},
                {"name": "taxi_cancel_order", "inputSchema": {"properties": {"order_id": {}}}},
            ]

        def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
            calls.append((name, arguments))
            if name == "maps_textsearch":
                if "示例小区A" in str(arguments.get("keywords")):
                    return {"pois": [{"name": "示例小区A", "location": "116.3805,39.9080"}]}
                return {"pois": [{"name": "示例写字楼A座", "location": "116.3260,39.9830"}]}
            if name == "taxi_estimate":
                return {
                    "traceId": "trace-abc-999",
                    "prices": [
                        {"name": "特惠快车", "product_id": "201", "price": 24.5},
                        {"name": "滴滴专车", "product_id": "400", "price": 46.0},
                    ],
                }
            if name == "taxi_create_order":
                assert arguments["trace_id"] == "trace-abc-999"
                return {"order_id": "DIDI-ORD-2026111701", "status": "呼叫中"}
            if name == "taxi_query_order":
                return {"order_id": arguments["order_id"], "driver_plate": "京AD88888", "eta_minutes": 3}
            if name == "taxi_cancel_order":
                return {"order_id": arguments["order_id"], "status": "已取消"}
            return {}

    r1 = request_didi_ride(
        sandbox_db,
        dest_raw="示例写字楼",
        origin_raw="家",
        mcp_factory=FakeDidiSession,
    )
    assert r1["ok"] is True
    assert r1["mode"] == "pro_ready_to_order"
    tok = r1["approval_token"]
    assert f"apply {tok}" in r1["reply"]
    assert "¥24.5" in r1["reply"]

    # 用户确认 apply 口令后，真正调用 taxi_create_order 发单！
    ap_res = verify_and_apply_approval(
        sandbox_db,
        tok,
        extra_executors={"didi_create_order": lambda p, d: execute_didi_create_order(p, d, mcp_factory=FakeDidiSession)},
    )
    assert ap_res["ok"] is True
    assert "DIDI-ORD-2026111701" in ap_res["summary"]

    # 查询司机车牌状态与取消订单
    st_res = query_or_cancel_didi_order(sandbox_db, cancel=False, mcp_factory=FakeDidiSession)
    assert st_res["ok"] is True
    assert "京AD88888" in st_res["reply"]

    cn_res = query_or_cancel_didi_order(sandbox_db, cancel=True, mcp_factory=FakeDidiSession)
    assert cn_res["ok"] is True


def test_meituan_taste_profile_recommendation(sandbox_db: SimingDB) -> None:
    # 写入噪音（单车/超市）+ 历史常点店 + 2 小时前刚吃过的店
    match_or_record_wallet_row(
        sandbox_db,
        occurred_at="2026-11-05T13:00:00+08:00",
        amount_cents=150,
        card_last4="",
        wallet_alias="微信",
        merchant="美团 · 先骑后付",
        kind="消费",
    )
    for d in ("01", "05", "09"):
        match_or_record_wallet_row(
            sandbox_db,
            occurred_at=f"2026-11-{d}T19:00:00+08:00",
            amount_cents=3950,
            card_last4="",
            wallet_alias="微信",
            merchant=f"美团 · 融柳大铁牛螺蛳粉-美团外卖App-2611{d}001",
            kind="消费",
        )
    match_or_record_wallet_row(
        sandbox_db,
        occurred_at=now_local().isoformat(timespec="seconds"),
        amount_cents=11940,
        card_last4="",
        wallet_alias="微信",
        merchant="美团 · 素坤泰(泰式打抛饭)-美团外卖App-261117999",
        kind="消费",
    )

    rec = recommend_meituan_meals(sandbox_db, hint="晚餐吃什么", check_cdp=False)
    assert rec["ok"] is True
    assert any("螺蛳粉" in p["shop"] for p in rec["picks"])
    assert not any("先骑后付" in p["shop"] for p in rec["picks"])
    # 刚刚吃过的素坤泰自动进防腻跳过列表
    assert any("素坤泰" in s for s in rec["recent_skipped"])

    q_in = handle_inbound_qq(sandbox_db, "今晚吃什么")
    assert q_in["handled"] is True
    assert "螺蛳粉" in q_in["reply"]
