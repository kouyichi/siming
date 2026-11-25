"""Siming v5 真实生活行为预期 QA 测试套件（财务/Sure自然查账、订阅持久化、特斯拉备车与车务、12.10装修与新房空气/HAOS、通勤、差旅、桌游与人情）。

聚焦真实人类日常使用一年的常识预期与行为缺陷回归，全程隔离在 sandbox_db + frozen_clock。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from siming.commute_hub import (
    check_regime_transition,
    compare_commute_modes,
    format_commute_advice,
)
from siming.compute_assets import (
    evaluate_compute_guardrails,
    upsert_compute_asset,
)
from siming.db import SHANGHAI_TZ, SimingDB, iso_now
from siming.event_chain import get_situation
from siming.haos_night_owl import (
    evaluate_night_owl_haos_rules,
    is_vacuum_locked,
    sync_night_owl_to_haos,
)
from siming.home_air_guard import (
    ingest_air_reading,
    recommend_ventilation_strategy,
)
from siming.home_passport import (
    format_home_passport_report,
    upsert_consumable,
    upsert_utility,
)
from siming.install_plan import (
    build_installation_projection,
    classify_arrival_status,
    format_arrival_inspection_checklist,
    format_installation_checklist,
    format_onsite_grouped_checklist,
    is_order_cancelled,
    is_order_truly_signed,
    mark_arrival_inspected,
)
from siming.movein_pm import check_install_conflicts
from siming.social_boardgames import (
    check_cabinet_capacity,
    check_upcoming_crowdfund_balances,
    upsert_crowdfund,
)
from siming.sub_hunter import (
    emit_subscription_stoploss_alerts,
    find_upcoming_charges,
    format_subscription_audit,
    mark_subscription_status,
    scan_subscriptions,
)
from siming.sure_bridge import (
    ask_sure_natural,
    sync_fixed_asset_to_sure,
    upsert_sure_rule,
)
from siming.tesla_link import (
    control_tesla_climate,
    evaluate_departure_auto_climate,
    ingest_tesla_state,
    sync_mobile_outing_plan,
)
from siming.trip_packager import (
    create_trip,
    detect_trips_from_events,
    pack_trip_cw01_draft,
)
from siming.vehicle_cfo import (
    evaluate_lfp_calibration_and_insurance,
    record_vehicle_event,
)


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI_TZ)


def _insert_txn(
    db: SimingDB,
    txn_id: str,
    occurred_at: str,
    amount_cents: int,
    merchant: str,
    *,
    account_alias: str = "建行1004",
    card_last4: str = "1004",
    kind: str = "消费",
    status: str = "CONFIRMED",
    notes: str = "",
    currency: str = "CNY",
) -> None:
    ts = iso_now()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ledger_txns (
                txn_id, occurred_at, amount_cents, currency, account_alias,
                card_last4, kind, status, merchant, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                txn_id,
                occurred_at,
                amount_cents,
                currency,
                account_alias,
                card_last4,
                kind,
                status,
                merchant,
                notes,
                ts,
                ts,
            ),
        )


# ===========================================================================
# 1. Subscription User-Decision Persistence & Annual Projection
# ===========================================================================


def test_behavior_1_subscription_decision_persistence_cancelled_exclusion_and_annual_projection(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    now = frozen_clock("2026-11-20T10:00:00+08:00")

    # 1) 月度订阅：iCloud+（将在 24h 内扣费）、迅雷（准备取消）、点众小说（记为僵尸）
    _insert_txn(sandbox_db, "S-IC1", "2026-09-22T10:00:00+08:00", 2200, "iCloud 由云上贵州运营")
    _insert_txn(sandbox_db, "S-IC2", "2026-10-22T10:00:00+08:00", 2200, "iCloud 由云上贵州运营")

    _insert_txn(sandbox_db, "S-XL1", "2026-09-22T12:00:00+08:00", 1900, "深圳市迅雷网络技术有限公司 · 超级会员")
    _insert_txn(sandbox_db, "S-XL2", "2026-10-22T12:00:00+08:00", 2500, "深圳市迅雷网络技术有限公司 · 超级会员")

    _insert_txn(sandbox_db, "S-DZ1", "2026-09-25T10:00:00+08:00", 1500, "点众小说连续包月")
    _insert_txn(sandbox_db, "S-DZ2", "2026-10-25T10:00:00+08:00", 1500, "点众小说连续包月")

    # 2) 年付订阅：88VIP 仅 11 个月前扣过一次（2025-12-20，单次年度会员）+ 历史更早扣费但漏导入的年付京东PLUS
    _insert_txn(sandbox_db, "S-88VIP", "2025-12-20T09:00:00+08:00", 8800, "淘宝 88VIP 年度会员")
    _insert_txn(sandbox_db, "S-JD1", "2024-01-15T10:00:00+08:00", 9900, "京东PLUS年度会员")
    _insert_txn(sandbox_db, "S-JD2", "2025-01-15T10:00:00+08:00", 9900, "京东PLUS年度会员")

    # 3) 外币订阅（USD $20/月）以及一条 VOID_DUPLICATE 重复脏流水（绝不能把重复单算成多次订阅）
    _insert_txn(sandbox_db, "S-GPT1", "2026-09-28T10:00:00+08:00", 2000, "ChatGPT Plus", currency="USD")
    _insert_txn(sandbox_db, "S-GPT2", "2026-10-28T10:00:00+08:00", 2000, "ChatGPT Plus", currency="USD")
    _insert_txn(
        sandbox_db,
        "S-VOID",
        "2026-10-28T10:01:00+08:00",
        2000,
        "ChatGPT Plus",
        currency="USD",
        status="VOID_DUPLICATE",
    )

    first_scan = scan_subscriptions(sandbox_db, now=now)
    assert first_scan["ok"] is True
    by_name = {s["normalized_name"]: s for s in first_scan["subscriptions"]}

    # 88VIP（11个月前扣费，365天周期）必须被识别，且 next_expected_at 精准指向 ~1 个月后（2026-12-20），绝不卡在过去！
    assert "88VIP" in by_name
    assert by_name["88VIP"]["cadence_days"] == 365
    assert by_name["88VIP"]["next_expected_at"].startswith("2026-12-20")

    # 京东PLUS 最后一笔在 2025-01-15（+365d=2026-01-15 已过），必须滚推到未来续费日 2027-01-15，绝不卡在过去！
    assert "京东PLUS" in by_name
    assert by_name["京东PLUS"]["cadence_days"] == 365
    assert by_name["京东PLUS"]["next_expected_at"].startswith("2027-01-15")

    # VOID_DUPLICATE 不计入 occurrences
    assert by_name["ChatGPT Plus"]["occurrences"] == 2

    # 用户手动决策：iCloud+ -> KEEP，迅雷 -> CANCELLED（带备注），点众小说 -> ZOMBIE（带备注）
    assert mark_subscription_status(sandbox_db, "iCloud+", "KEEP", note="全家照片刚需")["ok"] is True
    assert mark_subscription_status(sandbox_db, "迅雷", "CANCELLED", note="已在支付宝免密关闭")["ok"] is True
    assert mark_subscription_status(sandbox_db, "点众小说", "ZOMBIE", note="下个月前退订")["ok"] is True

    # 再次运行 scan_subscriptions：必须严格保留用户的 KEEP / CANCELLED / ZOMBIE 决策和备注，绝不重置回 ACTIVE！
    second_scan = scan_subscriptions(sandbox_db, now=now)
    by_name2 = {s["normalized_name"]: s for s in second_scan["subscriptions"]}
    assert by_name2["iCloud+"]["status"] == "KEEP"
    assert by_name2["iCloud+"]["notes"] == "全家照片刚需"
    assert by_name2["迅雷"]["status"] == "CANCELLED"
    assert by_name2["迅雷"]["notes"] == "已在支付宝免密关闭"
    assert by_name2["点众小说"]["status"] == "ZOMBIE"
    assert by_name2["点众小说"]["notes"] == "下个月前退订"
    # CANCELLED 的涨价订阅不得再次进入 price_hikes
    assert all(h["normalized_name"] != "迅雷" for h in second_scan["price_hikes"])

    # CANCELLED 订阅绝不能触发 48h 扣费预警或止损卡
    upcoming = find_upcoming_charges(sandbox_db, now=now, horizon_hours=48)
    assert any(u["normalized_name"] == "iCloud+" for u in upcoming)
    assert all(u["normalized_name"] != "迅雷" for u in upcoming)

    alerts = emit_subscription_stoploss_alerts(sandbox_db, now=now)
    assert all(a["normalized_name"] != "迅雷" for a in alerts["alerts"])

    # 年度总成本必须排除 CANCELLED（迅雷 25*12=300），且按精确年化 + 外币汇率折算：
    # iCloud+ 22*12=264 + 点众 15*12=180 + 88VIP 88 + 京东PLUS 99 + ChatGPT 20*7.25*12=1740 = ¥2371.00/年
    audit_txt = format_subscription_audit(sandbox_db, now=now)
    assert "约 ¥2371.00/年" in audit_txt


# ===========================================================================
# 2. Sure Natural Language Finance Query Expectations (sure_bridge.ask_sure_natural)
# ===========================================================================


def test_behavior_2_sure_natural_queries_time_scope_category_net_worth_and_cw08_aging(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    frozen_clock("2026-11-20T15:00:00+08:00")

    # 规则：山姆家庭 50% AA 分摊
    upsert_sure_rule(
        sandbox_db,
        name="山姆AA",
        match_pattern="山姆",
        category="家居商超",
        split_ratio=0.5,
    )

    # 上月（2026-10）流水
    _insert_txn(sandbox_db, "OCT-WM", "2026-10-12T12:00:00+08:00", 5200, "美团外卖 · 眉州东坡")
    _insert_txn(sandbox_db, "OCT-CAR", "2026-10-18T22:00:00+08:00", 6000, "滴滴出行", notes="加班打车 CW01")
    _insert_txn(sandbox_db, "OCT-SAM", "2026-10-25T15:00:00+08:00", 40000, "山姆会员商店")  # split 0.5 -> 200

    # 本月（2026-11）流水
    _insert_txn(sandbox_db, "NOV-WM1", "2026-11-05T12:10:00+08:00", 3600, "美团外卖 · 示例瓦罐汤瓦罐汤")
    _insert_txn(sandbox_db, "NOV-WM2", "2026-11-12T19:00:00+08:00", 4200, "饿了么 · 示例粉面炒米粉")
    _insert_txn(sandbox_db, "NOV-COF", "2026-11-14T09:30:00+08:00", 1500, "瑞幸咖啡 · 冰美式")
    _insert_txn(sandbox_db, "NOV-DIDI", "2026-11-15T23:00:00+08:00", 4500, "滴滴出行", notes="加班打车 CW01")
    _insert_txn(sandbox_db, "NOV-CHG", "2026-11-16T18:00:00+08:00", 8000, "特斯拉超级充电站·大钟寺")
    _insert_txn(sandbox_db, "NOV-CW08", "2026-11-10T10:00:00+08:00", 10000, "Nous API", currency="USD", notes="work_cw08")  # 725 CNY
    _insert_txn(sandbox_db, "NOV-INC", "2026-11-01T09:00:00+08:00", 2000000, "工资入账", kind="收入")
    _insert_txn(sandbox_db, "NOV-VOID", "2026-11-12T19:01:00+08:00", 99900, "美团外卖重复", status="VOID_DUPLICATE")

    sync_fixed_asset_to_sure(sandbox_db, "APP-TV", "小米85寸电视", purchase_yuan=6000.0, current_yuan=5400.0)

    # A) 本月花了多少 -> 仅统计 2026-11（36 + 42 + 15 + 45 + 80 + 725 = 943.00）
    q_this = ask_sure_natural(sandbox_db, "本月花了多少")
    assert q_this["ok"] is True
    assert q_this["data"]["period"] == "2026-11"
    assert q_this["data"]["outflow_yuan"] == pytest.approx(943.0, abs=0.01)

    # B) 上月花了多少 -> 仅统计 2026-10（52 + 60 + 200 = 312.00），绝不混入 11 月或全部历史！
    q_last = ask_sure_natural(sandbox_db, "上月花了多少")
    assert q_last["ok"] is True
    assert q_last["data"]["period"] == "2026-10"
    assert q_last["data"]["outflow_yuan"] == pytest.approx(312.0, abs=0.01)
    assert "2026-10" in q_last["reply"]

    # C) 空月份显式查询（如 2026-09 花了多少）-> 返回 0.00 且 period="2026-09"，绝不错误回退到全部历史！
    q_empty = ask_sure_natural(sandbox_db, "2026年9月花了多少")
    assert q_empty["ok"] is True
    assert q_empty["data"]["period"] == "2026-09"
    assert q_empty["data"]["outflow_yuan"] == 0.0

    # D) 本月外卖花了多少 -> 精准只算 11 月外卖（36 + 42 = 78.00），不混入瑞幸咖啡/打车/超充/10月外卖/VOID！
    q_wm = ask_sure_natural(sandbox_db, "本月外卖花了多少")
    assert q_wm["ok"] is True
    assert q_wm["intent"] == "category_spend"
    assert q_wm["data"]["period"] == "2026-11"
    assert q_wm["data"]["filter_label"] == "外卖"
    assert q_wm["data"]["outflow_yuan"] == pytest.approx(78.0, abs=0.01)
    assert q_wm["data"]["txn_count"] == 2
    assert "78.00" in q_wm["reply"]

    # E) 本月咖啡花了多少 -> 精准 15.00
    q_cof = ask_sure_natural(sandbox_db, "本月咖啡花了多少")
    assert q_cof["ok"] is True
    assert q_cof["data"]["outflow_yuan"] == pytest.approx(15.0, abs=0.01)

    # F) 本月打车花了多少 -> 精准 45.00（不含特斯拉超充 80，不含上月滴滴 60）
    q_taxi = ask_sure_natural(sandbox_db, "本月打车花了多少")
    assert q_taxi["ok"] is True
    assert q_taxi["data"]["outflow_yuan"] == pytest.approx(45.0, abs=0.01)

    # G) 上月外卖花了多少 -> 精准 52.00
    q_oct_wm = ask_sure_natural(sandbox_db, "上月外卖花了多少")
    assert q_oct_wm["ok"] is True
    assert q_oct_wm["data"]["period"] == "2026-10"
    assert q_oct_wm["data"]["outflow_yuan"] == pytest.approx(52.0, abs=0.01)

    # H) 净资产问答
    q_nw = ask_sure_natural(sandbox_db, "净资产")
    assert q_nw["ok"] is True
    assert q_nw["intent"] == "net_worth"
    assert q_nw["data"]["fixed_assets_yuan"] == pytest.approx(5400.0, abs=0.01)

    # I) CW08 报销账龄 -> 仅查 CW08 时，oldest_reimburse_days 必须反映 CW08 的 10 天，不被上月 33 天前的 CW01 污染！
    q_cw08 = ask_sure_natural(sandbox_db, "CW08报销账龄")
    assert q_cw08["ok"] is True
    assert q_cw08["intent"] == "reimbursement_aging"
    assert q_cw08["data"]["cw08_yuan"] == pytest.approx(725.0, abs=0.01)
    assert q_cw08["data"]["cw08_oldest_days"] == 10
    assert q_cw08["data"]["oldest_reimburse_days"] == 10
    assert q_cw08["data"]["cw01_oldest_days"] >= 30


# ===========================================================================
# 3. Tesla Walking vs Driving, Low SOC, 18m Safety Shutoff & Vehicle CFO LFP
# ===========================================================================


def test_behavior_3_tesla_walk_guard_low_soc_18m_shutoff_and_vehicle_lfp(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    calls: list[dict[str, Any]] = []

    def fake_cli(turn_on: bool = True, target_temp_c: float = 21.5) -> dict[str, Any]:
        calls.append({"turn_on": turn_on, "target_temp_c": target_temp_c})
        return {"ok": True, "turn_on": turn_on, "target_temp_c": target_temp_c}

    # A) 示例小区A步行期工作日 09:15 离开家去公司（无驾车行程）-> 绝不能打开车库特斯拉空调！
    ingest_tesla_state(
        sandbox_db,
        {"soc": 78, "range_km": 339, "inside_temp_c": 3.0, "charging_state": "Disconnected", "shift_state": "P"},
    )
    walk_res = evaluate_departure_auto_climate(
        sandbox_db,
        departed_now=True,
        depart_place="家",
        now=_dt("2026-11-19 09:15"),
        climate_runner=fake_cli,
    )
    assert walk_res["skipped"] == "no_driving_plan_workday_walk_guard"
    assert len(calls) == 0

    # B) 有驾车计划、车内冰冷 (3°C)、但电量仅 18% -> 必须跳过省电并给出人工放行选项，绝不静默耗干电池！
    ingest_tesla_state(
        sandbox_db,
        {"soc": 18, "range_km": 78, "inside_temp_c": 3.0, "charging_state": "Disconnected", "shift_state": "P"},
    )
    low_plan = sync_mobile_outing_plan(
        sandbox_db,
        "准备开车去新房（示例小区B）",
        now=_dt("2026-11-21 14:00"),
        climate_runner=fake_cli,
    )
    assert low_plan["matched"] is True and low_plan["driving"] is True
    assert low_plan["climate"]["action"] == "low_soc_skipped"
    assert "自动备车已跳过省电" in low_plan["climate"]["message"]
    assert "开始备车" in low_plan["climate"]["message"]
    assert len(calls) == 0
    assert sandbox_db.get_meta("tesla_low_soc_override_ctx")

    # C) 电量健康 (75%)、车内 3°C、驾车出门 -> 开启暖车一次；18 分钟未挂挡发车 -> 自动关停保护！
    ingest_tesla_state(
        sandbox_db,
        {"soc": 75, "range_km": 326, "inside_temp_c": 3.0, "charging_state": "Disconnected", "shift_state": "P"},
    )
    ok_plan = sync_mobile_outing_plan(
        sandbox_db,
        "准备开车去山姆会员店",
        now=_dt("2026-11-21 15:00"),
        climate_runner=fake_cli,
    )
    assert ok_plan["climate"]["action"] == "climate_started"
    assert ok_plan["climate"]["mode"] == "heat"
    assert len(calls) == 1 and calls[0]["turn_on"] is True
    assert get_situation(sandbox_db, "tesla:main")["value"]["is_climate_on"] is True

    # 19 分钟后仍处于 P 档未发车 -> 自动关停空调，同步把 is_climate_on 置 False！
    shut_res = evaluate_departure_auto_climate(
        sandbox_db,
        departed_now=False,
        trigger="tick",
        now=_dt("2026-11-21 15:19"),
        climate_runner=fake_cli,
    )
    assert shut_res["action"] == "auto_shutoff_18m"
    assert len(calls) == 2 and calls[1]["turn_on"] is False
    assert get_situation(sandbox_db, "tesla:main")["value"]["is_climate_on"] is False

    # D) 若开启后 5 分钟用户手动关空调 -> 必须清除 18 分钟看门狗，不能 13 分钟后又发一条误报关停！
    sandbox_db.set_meta("last_tesla_auto_climate_at", "")
    sync_mobile_outing_plan(
        sandbox_db,
        "准备开车去宜家",
        now=_dt("2026-11-21 18:00"),
        climate_runner=fake_cli,
    )
    assert sandbox_db.get_meta("tesla_auto_climate_guard_json")
    control_tesla_climate(sandbox_db, turn_on=False, cli_runner=fake_cli)
    assert sandbox_db.get_meta("tesla_auto_climate_guard_json") == ""
    calls_before = len(calls)
    after_manual_off = evaluate_departure_auto_climate(
        sandbox_db,
        departed_now=False,
        trigger="tick",
        now=_dt("2026-11-21 18:20"),
        climate_runner=fake_cli,
    )
    assert after_manual_off.get("action") != "auto_shutoff_18m"
    assert len(calls) == calls_before

    # E) vehicle_cfo：3 天前刚做过 LFP 100% 满充 -> 绝不提醒校准；即使补录更早历史充电也不覆盖；满 7 天才提醒！
    now_v = frozen_clock("2026-11-20T16:00:00+08:00")
    record_vehicle_event(
        sandbox_db,
        kind="lfp_cal",
        title="Model Y 100% 满电校准",
        occurred_at="2026-11-17T20:00:00+08:00",
        detail={"end_soc": 100, "full_charge": True},
    )
    # 回填一条 20 天前的旧满充，不应把 3 天前的校准记录覆盖成 20 天前！
    record_vehicle_event(
        sandbox_db,
        kind="charge",
        title="历史旧超充记录",
        occurred_at="2026-10-31T20:00:00+08:00",
        detail={"end_soc": 100, "full_charge": True},
    )
    ev_3d = evaluate_lfp_calibration_and_insurance(sandbox_db, now=now_v, notify=False)
    assert ev_3d["lfp_calibration_due"] is False
    assert ev_3d["days_since_lfp_cal"] == 3
    assert all("LFP" not in a for a in ev_3d["alerts"])

    # 第 6 天仍不提醒；满 7 天触发提醒
    ev_6d = evaluate_lfp_calibration_and_insurance(sandbox_db, now=_dt("2026-11-23 16:00"), notify=False)
    assert ev_6d["lfp_calibration_due"] is False
    ev_7d = evaluate_lfp_calibration_and_insurance(sandbox_db, now=_dt("2026-11-24 16:00"), notify=False)
    assert ev_7d["lfp_calibration_due"] is True
    assert ev_7d["days_since_lfp_cal"] == 7

    # 车险已过期时文案必须显示「已过期 X 天」，绝不出现「仅剩 -5 天」
    record_vehicle_event(
        sandbox_db,
        kind="insurance",
        title="去年车险",
        occurred_at="2025-11-15T10:00:00+08:00",
        detail={"expires_at": "2026-11-15"},
    )
    ev_exp = evaluate_lfp_calibration_and_insurance(sandbox_db, now=now_v, notify=False)
    assert ev_exp["insurance_due_soon"] is True
    assert any("已过期 **5 天**" in a for a in ev_exp["alerts"])
    assert all("-5" not in a for a in ev_exp["alerts"])


# ===========================================================================
# 4. Renovation 12.10, Home Air Smog & HAOS Night-Owl Expectations
# ===========================================================================


def test_behavior_4_renovation_cancelled_orders_smog_recirculate_and_haos_night_owl(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    frozen_clock("2026-11-20T14:00:00+08:00")

    # A) install_plan：交易关闭/已退款订单绝不出现在待收货或待安装列表；派送中不触发到货初验；已验外箱绝不提前关闭 12.10 安装待办
    items_p = sandbox_db.paths.tmp_root / "items.json"
    auto_p = sandbox_db.paths.tmp_root / "auto.json"
    manual_p = sandbox_db.paths.tmp_root / "manual.json"

    items_p.write_text(
        json.dumps(
            {
                "preferences": {"install_date": "2026-12-10"},
                "items": [
                    {"id": "old-washer", "name": "旧款已退单洗衣机", "room": "", "install": "brand", "installDate": "2026-12-10"},
                    {"id": "rinnai-16l", "name": "林内16L零冷水燃气热水器", "room": "厨房", "install": "brand", "installDate": "2026-12-10", "svc": ["SVC-01"]},
                    {"id": "tv-85", "name": "小米电视 85寸", "room": "客厅", "install": "jd", "installDate": "2026-12-10"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    auto_p.write_text(
        json.dumps(
            {
                "generatedAt": "2026-11-20T12:00:00+08:00",
                "items": [
                    {"id": "old-washer", "orderInfo": {"stage": "closed", "raw": "交易关闭", "progress": "订单已取消并退款"}},
                    {"id": "rinnai-16l", "orderInfo": {"stage": "delivered", "raw": "完成", "progress": "您的订单已签收"}},
                    {"id": "tv-85", "orderInfo": {"stage": "delivered", "raw": "派送中", "progress": "快递员正在派送中"}},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manual_p.write_text("{}", encoding="utf-8")

    assert is_order_cancelled("closed", "交易关闭", "订单已取消并退款") is True
    assert is_order_truly_signed("delivered", "派送中", "快递员正在派送中") is False
    assert classify_arrival_status({"stage": "delivered", "raw": "派送中", "progress": "正在派送中"})[0] == "shipping"

    proj = build_installation_projection(sandbox_db, items_json_path=items_p, auto_json_path=auto_p, manual_json_path=manual_p)
    assert proj["ok"] is True
    assert proj["active_orders"] == 2  # 仅统计有效单，已取消旧单剔除
    assert proj["active_vendor_install_total"] == 2
    assert len(proj["cancelled_items"]) == 1
    assert all(it["id"] != "old-washer" for it in proj["active_install_items"])
    assert "旧款已退单洗衣机" not in proj["active_unbooked_install_items"]
    assert "旧款已退单洗衣机" not in proj["signed_waiting_install"]

    # 送装清单和现场作战单中绝不显示已取消订单
    chk_install = format_installation_checklist(sandbox_db, items_json_path=items_p, auto_json_path=auto_p, manual_json_path=manual_p)
    chk_onsite = format_onsite_grouped_checklist(sandbox_db, items_json_path=items_p, auto_json_path=auto_p, manual_json_path=manual_p)
    assert "旧款已退单洗衣机" not in chk_install
    assert "旧款已退单洗衣机" not in chk_onsite
    assert "2 台需上门安装设备" in chk_onsite

    # 派送中的小米电视绝不触发默认批量到货初验；仅真签收的林内被标已验外观
    mk = mark_arrival_inspected(sandbox_db, target_query="all", items_json_path=items_p, auto_json_path=auto_p, manual_json_path=manual_p)
    assert mk["ok"] is True
    assert mk["items"] == ["林内16L零冷水燃气热水器"]
    assert "12.10 上门安装" in mk["summary"]

    # 显式指定已取消订单也不能被误标已验外观
    mk_canc = mark_arrival_inspected(sandbox_db, target_query="旧款已退单洗衣机", items_json_path=items_p, auto_json_path=auto_p, manual_json_path=manual_p)
    assert mk_canc["ok"] is False

    # 核销外箱初验后，林内依然保留在 12.10 待预约安装清单中（绝不提前关闭安装待办！）
    proj_after = build_installation_projection(sandbox_db, items_json_path=items_p, auto_json_path=auto_p, manual_json_path=manual_p)
    assert "林内16L零冷水燃气热水器" in proj_after["unbooked_install_items"]
    arr_txt = format_arrival_inspection_checklist(sandbox_db, items_json_path=items_p, auto_json_path=auto_p, manual_json_path=manual_p)
    assert "已完成外箱初验" in arr_txt and "林内16L零冷水燃气热水器" in arr_txt
    assert "旧款已退单洗衣机" not in arr_txt

    # movein_pm 冲突体检同样跳过已取消订单
    pm_res = check_install_conflicts(sandbox_db, items_json_path=items_p, auto_json_path=auto_p, manual_json_path=manual_p)
    assert pm_res["ok"] is True
    assert all("旧款已退单洗衣机" not in c for c in pm_res["conflicts"] + pm_res["warnings"])

    # B) home_air_guard：高甲醛 (0.16) + 室外重度雾霾 (AQI=260) -> 必须选内循环过滤 recirculate_filter，绝不 strong_purge 把毒雾霾吸进新房！
    air_res = ingest_air_reading(
        sandbox_db,
        room="客厅",
        temp_c=23.5,
        humidity_pct=46.0,
        hcho_mg_m3=0.16,
        voc_ppb=450.0,
        outdoor_aqi=260,
        recorded_at="2027-01-18T16:00:00+08:00",
        notify=False,
    )
    assert air_res["fresh_air_mode"] == "recirculate_filter"
    strat_smog = recommend_ventilation_strategy(sandbox_db, now="2027-01-18T16:05:00+08:00")
    assert strat_smog["mode"] == "recirculate_filter"
    assert strat_smog["mode"] != "strong_purge"
    assert strat_smog["haos_action"]["service_data"]["preset_mode"] == "recirculate"

    # C) haos_night_owl：09:00 补觉硬锁科沃斯 -> 离家（支持中文「公司」/「away」）解锁自动清扫 -> 回家路上（支持中文「回家路上」/「heading_home」）T-12m 预热格力空调 + 林内零冷水
    haos_calls: list[dict[str, Any]] = []
    haos_fn = lambda act: (haos_calls.append(act) or {"ok": True})

    lock_09 = is_vacuum_locked(sandbox_db, now="2027-05-06T09:00:00+08:00", sleep_state="sleep")
    assert lock_09["vacuum_locked"] is True
    r_sleep = sync_night_owl_to_haos(
        sandbox_db,
        now="2027-05-06T09:00:00+08:00",
        sleep_state="sleep",
        location_state="家",
        haos_call_fn=haos_fn,
    )
    assert r_sleep["vacuum_locked"] is True
    assert r_sleep["auto_clean_released"] is False
    assert any(a["rule"] == "sleep_vacuum_hard_lock" for a in r_sleep["actions"])

    # 离家上班（中文「公司」与英文「away」均能正确触发解锁清扫）
    r_away = evaluate_night_owl_haos_rules(
        sandbox_db,
        now="2027-05-06T11:30:00+08:00",
        sleep_state="awake",
        location_state="公司",
        haos_call_fn=haos_fn,
    )
    assert r_away["location_state"] == "away"
    assert r_away["vacuum_locked"] is False
    assert r_away["auto_clean_released"] is True
    assert any(a["rule"] == "away_start_vacuum_clean" for a in r_away["actions"])

    # 回家路上 eta=12m -> 预开格力空调 + 林内零冷水
    r_home = sync_night_owl_to_haos(
        sandbox_db,
        now="2027-05-06T20:20:00+08:00",
        sleep_state="awake",
        location_state="回家路上",
        eta_home_min=12.0,
        haos_call_fn=haos_fn,
    )
    assert r_home["location_state"] == "heading_home"
    assert r_home["pre_arrival_triggered"] is True
    rule_set = {a["rule"] for a in r_home["actions"]}
    assert {"pre_arrival_gree_ac", "pre_arrival_rinnai_hot_water"}.issubset(rule_set)


# ===========================================================================
# 5. Commute Mode Realism (commute_hub)
# ===========================================================================


def test_behavior_5_commute_weather_aware_and_extreme_low_soc_penalty(sandbox_db: SimingDB) -> None:
    # A) 暴雨 + EV 电量 80% + EV 更快 (42m) vs 拥挤地铁 (58m, high) vs 溢价滴滴 (48m, ¥85) -> 绝不推荐冒雨挤地铁，首选电车自驾！
    rain_charged = compare_commute_modes(
        sandbox_db,
        ev_eta_min=42,
        ev_soc_pct=80,
        parking_yuan=40.0,
        metro_eta_min=58,
        metro_crowding="high",
        didi_eta_min=48,
        didi_fare_yuan=85.0,
        rain=True,
        late_min=0,
    )
    assert rain_charged["recommended_mode"] == "ev"
    assert rain_charged["ranked_modes"][-1]["mode"] == "metro"
    advice_txt = format_commute_advice(rain_charged)
    assert "电车自驾" in advice_txt and "有雨" in advice_txt

    # B) 极端低电量 EV (12%)，哪怕 EV 耗时最快且免费停车 -> 必须重罚并标记不可行，绝不推荐开电车趴窝！
    low_soc = compare_commute_modes(
        sandbox_db,
        ev_eta_min=32,
        ev_soc_pct=12,
        parking_yuan=0.0,
        metro_eta_min=58,
        metro_crowding="high",
        didi_eta_min=48,
        didi_fare_yuan=75.0,
        rain=False,
        late_min=0,
    )
    assert low_soc["recommended_mode"] != "ev"
    ev_entry = next(m for m in low_soc["ranked_modes"] if m["mode"] == "ev")
    assert ev_entry["feasible"] is False
    assert low_soc["ranked_modes"][-1]["mode"] == "ev"

    # 暴雨 + 低电量 EV -> 触发救援建议
    rain_low = compare_commute_modes(
        sandbox_db,
        ev_eta_min=40,
        ev_soc_pct=12,
        parking_yuan=0.0,
        metro_eta_min=60,
        metro_crowding="high",
        didi_eta_min=45,
        didi_fare_yuan=70.0,
        rain=True,
        late_min=0,
    )
    assert rain_low["recommended_mode"] == "didi"
    assert rain_low["rescue_recommended"] is True
    assert "电量不足" in rain_low["rescue_reason"]

    # 搬家切轨边界确认
    assert check_regime_transition(sandbox_db, today="2027-04-29")["regime"] == "primary_home"
    assert check_regime_transition(sandbox_db, today="2028-01-01")["regime"] == "secondary_home"


# ===========================================================================
# 6. Domestic vs International Trip Packager Realism (trip_packager)
# ===========================================================================


def test_behavior_6_domestic_vs_international_checklist_window_isolation_and_event_recency(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    frozen_clock("2026-11-20T14:00:00+08:00")

    # A) 国际行程（东京 JP）必须含护照、签证、eSIM/漫游、Suica/本地支付、多币种卡/DCC 提醒
    jp = create_trip(sandbox_db, destination="东京", start_date="2026-12-01", end_date="2026-12-05", country="JP")
    jp_text = " ".join(c["title"] for c in jp["checklist"])
    assert jp["kind"] == "international"
    assert "护照" in jp_text
    assert "签证" in jp_text
    assert "eSIM" in jp_text or "漫游" in jp_text
    assert "Suica" in jp_text
    assert "DCC" in jp_text

    # B) 国内行程（上海 CN）绝不能唠叨护照、签证、eSIM、DCC！
    cn = create_trip(sandbox_db, destination="上海", start_date="2026-11-10", end_date="2026-11-12", country="CN")
    cn_text = " ".join(c["title"] for c in cn["checklist"])
    assert cn["kind"] == "domestic"
    for forbidden in ("护照", "签证", "eSIM", "漫游", "DCC", "Suica"):
        assert forbidden not in cn_text
    assert "身份证" in cn_text

    # C) pack_trip_cw01_draft 严格只收录 [start_date, end_date] 内的差旅支出，绝不泄漏窗前/窗后费用，并清理作废后的旧 tag
    _insert_txn(sandbox_db, "SH-BEFORE", "2026-11-09T23:50:00+08:00", 5000, "滴滴出行")
    _insert_txn(sandbox_db, "SH-IN1", "2026-11-10T08:00:00+08:00", 55300, "12306高铁北京-上海")
    _insert_txn(sandbox_db, "SH-IN2", "2026-11-11T15:00:00+08:00", 120000, "上海全季酒店")
    _insert_txn(sandbox_db, "SH-IN-PERS", "2026-11-11T19:00:00+08:00", 4500, "美团外卖")  # 非差旅个人外卖不混入
    _insert_txn(sandbox_db, "SH-AFTER", "2026-11-13T00:10:00+08:00", 6800, "滴滴出行")

    pack1 = pack_trip_cw01_draft(sandbox_db, cn["trip_id"])
    assert pack1["ok"] is True
    ids1 = {e["txn_id"] for e in pack1["expenses"]}
    assert ids1 == {"SH-IN1", "SH-IN2"}
    assert pack1["total_cny_yuan"] == pytest.approx(1753.0, abs=0.01)

    # 若后续将 SH-IN2 作废并重打 CW01 草稿，sure_sync_map 不残留旧 trip_tag
    with sandbox_db.transaction() as conn:
        conn.execute("UPDATE ledger_txns SET status = 'VOID' WHERE txn_id = 'SH-IN2'")
    pack2 = pack_trip_cw01_draft(sandbox_db, cn["trip_id"])
    assert {e["txn_id"] for e in pack2["expenses"]} == {"SH-IN1"}
    conn = sandbox_db.connect()
    try:
        tags = conn.execute(
            "SELECT local_id FROM sure_sync_map WHERE local_kind = 'trip_tag' AND local_id LIKE ?",
            (f"{cn['trip_id']}:%",),
        ).fetchall()
    finally:
        conn.close()
    assert len(tags) == 1 and tags[0]["local_id"].endswith(":SH-IN1")

    # D) 历史库超过 500 条普通事件后，detect_trips_from_events 仍必须能捕获最新的机票出票邮件！
    with sandbox_db.transaction() as conn:
        base_ev = _dt("2026-01-01 08:00")
        for i in range(510):
            ts_i = iso_now(base_ev + timedelta(hours=i))
            sandbox_db.append_event(
                conn,
                source="system",
                thread_key=f"noise-{i}",
                summary=f"常规心跳日志 #{i}",
                payload={"idx": i},
                occurred_at=ts_i,
                fingerprint=f"fp-noise-{i}",
            )
        sandbox_db.append_event(
            conn,
            source="mail163",
            thread_key="osaka-new",
            summary="携程机票出票：2026-12-25 至 2026-12-29 北京飞大阪 航班 CA927",
            payload={"provider": "携程", "order_no": "CT-OSK-01"},
            occurred_at="2026-11-19T10:00:00+08:00",
            fingerprint="fp-osaka-new",
        )
    det = detect_trips_from_events(sandbox_db)
    assert any(d["destination"] == "大阪" and d["kind"] == "international" for d in det)


# ===========================================================================
# 7. Boardgame Cabinet & Crowdfund Expectations (social_boardgames)
#    + Extra UX polish checks (compute_assets & home_passport overdue wording)
# ===========================================================================


def test_behavior_7_boardgame_cabinet_crowdfund_and_overdue_human_wording(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    now = frozen_clock("2026-11-20T14:00:00+08:00")

    # A) 199 盒 + 2 盒 -> 明确触发超 200 盒上限爆仓；199 盒 + 1 盒 -> 不超上限！
    over = check_cabinet_capacity(sandbox_db, current_boxes=199, current_volume_m3=2.8, adding_boxes=2, adding_volume_m3=0.05)
    assert over["over_capacity"] is True
    assert over["boxes_exceeded"] is True
    assert over["status"] == "OVER_CAPACITY"
    assert "爆仓" in over["message"]

    exact_ok = check_cabinet_capacity(sandbox_db, current_boxes=199, current_volume_m3=2.8, adding_boxes=1, adding_volume_m3=0.02)
    assert exact_ok["over_capacity"] is False
    assert exact_ok["boxes_exceeded"] is False
    assert exact_ok["remaining_boxes"] == 0

    # B) 众筹项目：ARRIVED / CANCELLED / PAID / REFUNDED 绝不触发补款告警；3 天内到期的 BALANCE_DUE 必须告警！
    upsert_crowdfund(
        sandbox_db,
        title="工业革命：匹兹堡",
        platform="新物集",
        pledge_yuan=199.0,
        balance_due_yuan=488.0,
        balance_due_at="2026-11-23",  # 3 天后到期
        status="BALANCE_DUE",
        notes="豪华全套",
    )
    upsert_crowdfund(
        sandbox_db,
        title="已到货桌游",
        platform="KS",
        pledge_yuan=300.0,
        balance_due_yuan=200.0,
        balance_due_at="2026-11-22",
        status="ARRIVED",
    )
    upsert_crowdfund(
        sandbox_db,
        title="已退款取消桌游",
        platform="Gamefound",
        pledge_yuan=100.0,
        balance_due_yuan=350.0,
        balance_due_at="2026-11-21",
        status="CANCELLED",
    )
    upsert_crowdfund(
        sandbox_db,
        title="已补款待发货桌游",
        platform="新物集",
        pledge_yuan=150.0,
        balance_due_yuan=280.0,
        balance_due_at="2026-11-22",
        status="PAID",
    )

    alerts = check_upcoming_crowdfund_balances(sandbox_db, now=now, warn_days=7)
    assert len(alerts) == 1
    assert alerts[0]["title"] == "工业革命：匹兹堡"
    assert alerts[0]["days_left"] == 3

    # 仅更新状态为 ARRIVED 时，必须保留原有定金和备注，且随后从尾款预警消失
    upd = upsert_crowdfund(sandbox_db, title="工业革命：匹兹堡", platform="新物集", status="ARRIVED")
    assert upd["pledge_yuan"] == pytest.approx(199.0)
    assert upd["notes"] == "豪华全套"
    assert check_upcoming_crowdfund_balances(sandbox_db, now=now, warn_days=7) == []

    # C) Extra UX: 已过期的算力套餐 & 已逾期水电/耗材报告文案绝不出现「剩余 -X 天」
    upsert_compute_asset(
        sandbox_db,
        provider="aliyun",
        account_label="expired-plan",
        balance=500.0,
        plan_name="百炼旧套餐",
        plan_expires_at="2026-11-17",
    )
    cg = evaluate_compute_guardrails(sandbox_db, now=now, notify=False)
    assert len(cg["expiring"]) == 1
    assert "已到期 3 天" in cg["expiring"][0]["message"]
    assert "-3" not in cg["expiring"][0]["message"]

    upsert_utility(sandbox_db, kind="property", account_no="XFL-01", next_due_at="2026-11-18", amount_yuan=900.0)
    upsert_consumable(sandbox_db, "app-09", "科沃斯 T90S Pro", "尘袋", lifespan_days=30, installed_at="2026-10-15")
    hp_txt = format_home_passport_report(sandbox_db, now=now)
    assert "已逾期 2 天" in hp_txt
    assert "已超期 6 天" in hp_txt
    assert "剩余 -" not in hp_txt
