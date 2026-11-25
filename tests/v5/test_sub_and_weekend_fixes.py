"""Siming v5 精准信号与周末日历守卫回归测试（Point 2 & Point 4）。

覆盖：
1. Point 2 — 订阅刺客猎手防污染（美团外卖/滴滴/瑞幸/便利店/超市/丰巢/ETC/停车/京东/抖音电商零漏网 + 历史推断备注不串台），
   Tier 1 显式订阅（iCloud+/Apple/点众/Game Pass/迅雷/美团神会员/88VIP）与 Tier 2 未知商户统计门控（>=3次、±12%金额、标准账期、CV<=0.25），
   以及 `purge_false_positive_subscriptions(db)` 清理误报行与自动 `sure_rules`（同时保留用户显式 `KEEP`/`CANCELLED`）。
2. Point 4 — 周末/节假日/调休补班日历守卫：
   - `evaluate_overtime_meal_ab` 与 `check_and_fire_offwork_pickup` 在周六、周日、法定节假日一律 `skipped="non_workday"`，
     在调休补班日（如国庆调休补班周六 `2026-10-10`）正常生效，且 `evaluate_overtime_meal_ab` 严格核验在司/不在家与 `overtime_meal` 开关；
   - `evaluate_meal_regularity` 严格遵守 `meal_combo_guard` 开关，周末/节假日放宽午餐窗、20:30 前不扰、未确认清醒不扰，
     仅在确认 `sleep:state == awake` 且 20:30 后无饮食消费时轻柔提醒且绝不含“加班”话术。
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from siming.attention import record_explicit_sleep_state
from siming.db import SHANGHAI_TZ, SimingDB, iso_now
from siming.event_chain import set_situation
from siming.meal_guard import evaluate_meal_regularity
from siming.modules import set_module_enabled
from siming.spatial_life import check_and_fire_offwork_pickup, evaluate_overtime_meal_ab
from siming.sub_hunter import (
    is_excluded_non_subscription,
    mark_subscription_status,
    purge_false_positive_subscriptions,
    scan_subscriptions,
)
from siming.sure_bridge import list_sure_rules, upsert_sure_rule


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI_TZ)


def _insert_txn(
    db: SimingDB,
    txn_id: str,
    occurred_at: str,
    amount_cents: int,
    merchant: str,
    notes: str = "",
    kind: str = "消费",
) -> None:
    ts = iso_now()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ledger_txns (
                txn_id, occurred_at, amount_cents, currency, account_alias,
                card_last4, kind, status, merchant, notes, created_at, updated_at
            ) VALUES (?, ?, ?, 'CNY', '建行1004', '1004', ?, 'CONFIRMED', ?, ?, ?, ?)
            """,
            (txn_id, occurred_at, amount_cents, kind, merchant, notes, ts, ts),
        )


def _write_calendar_and_policy(db: SimingDB) -> None:
    (db.paths.config_dir / "calendar_rules.json").write_text(
        json.dumps(
            {
                "version": "test-2026",
                "days": {
                    "2026-10-01": {"name": "国庆节", "isOffDay": True},
                    "2026-10-02": {"name": "国庆节", "isOffDay": True},
                    "2026-10-10": {"name": "国庆节", "isOffDay": False},  # 周六调休补班
                },
                "beijing_tail_rotations": [],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (db.paths.config_dir / "policy.yaml").write_text(
        """quiet_hours:
  start: '23:59'
  end: '00:00'
windows:
  morning_brief: '08:15'
  pickup_batch: '18:00'
daily_care:
  regimes:
  - id: primary_home
    until: '2028-01-01'
    home_label: 示例小区A
    home_lat: 39.904200
    home_lon: 116.407400
    office_label: 公司
    office_lat: 39.908000
    office_lon: 116.407000
""",
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Point 2: Subscription Assassin Precision & Purge Tests
# ---------------------------------------------------------------------------


def test_subscription_denylist_blocks_meituan_didi_luckin_and_keeps_true_subs(sandbox_db: SimingDB) -> None:
    now = _dt("2026-11-22 10:00")

    # 1) 高频日常消费噪声（即使恰好每隔 7 天消费、甚至金额固定或 notes 含历史推断，也绝不能进入订阅！）
    noisy_merchants = [
        ("美团平台商户 · 素坤泰-美团外卖App-261116001", 3800, "[历史推断: Apple 订阅服务 (76%)]"),
        ("meituan · 示例粉面广东大排档(鸟巢店)", 3800, "[历史推断: Apple 订阅服务 (76%)]"),
        ("美团平台商户 · 小象超市（太平庄店）-美团外卖App", 6500, ""),
        ("滴滴出行 · 先乘车后付款", 2400, ""),
        ("高德打车 · 聚合出行", 3100, ""),
        ("瑞幸咖啡 · 冰美式", 1500, ""),
        ("霸王茶姬 · 伯牙绝弦", 1800, ""),
        ("北京便利蜂连锁商业有限公司 · 便利蜂购物", 1200, ""),
        ("山姆会员商店SamsCLUB · 北京亦庄山姆会员商店", 28500, ""),
        ("盒马 · 薯片海苔等多件", 8900, ""),
        ("支付宝 · ETC通行费", 1500, ""),
        ("捷停车 · 商场停车费", 1600, ""),
        ("特斯拉超级充电站·大钟寺", 4500, ""),
        ("丰巢科技 · 畅存费", 100, ""),
        ("京东商城 · 订单编号20261101", 12900, ""),
        ("抖音电商商家 · 抖音电商-订单编号691798616", 2990, ""),
    ]
    base = _dt("2026-09-01 12:00")
    for idx, (m, amt, note) in enumerate(noisy_merchants):
        for k in range(5):
            t = base + timedelta(days=k * 7)
            _insert_txn(sandbox_db, f"NOISE-{idx}-{k}", iso_now(t), amt, m, notes=note)

    # 2) 真实订阅（Tier 1 显式关键词）
    _insert_txn(sandbox_db, "SUB-IC1", "2026-09-20T22:40:00+08:00", 600, "iCloud 由云上贵州运营")
    _insert_txn(sandbox_db, "SUB-IC2", "2026-10-20T22:45:00+08:00", 600, "iCloud 由云上贵州运营")
    _insert_txn(sandbox_db, "SUB-IC3", "2026-11-20T22:45:00+08:00", 600, "iCloud 由云上贵州运营")

    _insert_txn(sandbox_db, "SUB-DZ1", "2026-10-30T14:29:00+08:00", 1900, "点众科技 · 点众小说-7天会员")
    _insert_txn(sandbox_db, "SUB-DZ2", "2026-11-06T14:01:00+08:00", 1900, "点众科技 · 点众小说-7天会员")
    _insert_txn(sandbox_db, "SUB-DZ3", "2026-11-13T14:32:00+08:00", 1900, "点众科技 · 点众小说-7天会员")

    _insert_txn(sandbox_db, "SUB-GP1", "2026-09-21T10:00:00+08:00", 7898, "Microsoft Payments Pte. Ltd. · Xbox Game Pass Premium 1 Month")
    _insert_txn(sandbox_db, "SUB-GP2", "2026-10-21T10:00:00+08:00", 7898, "Microsoft Payments Pte. Ltd. · Xbox Game Pass Premium 1 Month")

    _insert_txn(sandbox_db, "SUB-XL1", "2026-09-15T12:00:00+08:00", 1900, "深圳市迅雷网络技术有限公司 · 超级会员")
    _insert_txn(sandbox_db, "SUB-XL2", "2026-10-15T12:00:00+08:00", 2500, "深圳市迅雷网络技术有限公司 · 超级会员")

    # 3) 显式平台会员（美团神会员 / 盒马X 88VIP）应该被放行，而外卖/商超消费被拒绝
    _insert_txn(sandbox_db, "SUB-MTVIP1", "2026-09-10T09:00:00+08:00", 1500, "美团神会员连续包月")
    _insert_txn(sandbox_db, "SUB-MTVIP2", "2026-10-10T09:00:00+08:00", 1500, "美团神会员连续包月")

    scan = scan_subscriptions(sandbox_db, now=now)
    assert scan["ok"] is True
    found_names = {s["normalized_name"] for s in scan["subscriptions"]}

    # 真实订阅全部命中
    assert {"iCloud+", "点众小说", "PC Game Pass", "迅雷", "美团神会员"}.issubset(found_names)
    assert any(h["normalized_name"] == "迅雷" and h["price_hike_pct"] > 25.0 for h in scan["price_hikes"])

    # 零日常消费泄漏（包括美团外卖、滴滴、瑞幸、山姆、盒马、丰巢、ETC、京东、抖音电商，且无 Apple 误归类）
    assert len(found_names) == 5, f"意外检出多余订阅: {found_names}"
    assert "Apple 服务" not in found_names

    # Sure 自动周期规则中同样零日常消费污染
    rules = list_sure_rules(sandbox_db)
    for r in rules:
        assert not is_excluded_non_subscription(r["name"].removeprefix("订阅:"))


def test_subscription_tier2_strict_stats_gate(sandbox_db: SimingDB) -> None:
    now = _dt("2026-11-22 10:00")

    # 1) 合法 Tier 2：未知非黑名单 SaaS，3 次、固定金额 ¥49、精准 30 天间隔 -> 通过
    _insert_txn(sandbox_db, "T2-OK1", "2026-09-01T10:00:00+08:00", 4900, "Acme Cloud Labs")
    _insert_txn(sandbox_db, "T2-OK2", "2026-10-01T10:00:00+08:00", 4900, "Acme Cloud Labs")
    _insert_txn(sandbox_db, "T2-OK3", "2026-10-31T10:00:00+08:00", 5000, "Acme Cloud Labs")

    # 2) 仅 2 次的未知商户 -> 拒绝（Tier 2 需要 >= 3 次）
    _insert_txn(sandbox_db, "T2-TWO1", "2026-09-05T10:00:00+08:00", 3000, "Beta Studio")
    _insert_txn(sandbox_db, "T2-TWO2", "2026-10-05T10:00:00+08:00", 3000, "Beta Studio")

    # 3) 金额波动过大（> ±12%）-> 拒绝
    _insert_txn(sandbox_db, "T2-AMT1", "2026-09-02T10:00:00+08:00", 2000, "Gamma Craft")
    _insert_txn(sandbox_db, "T2-AMT2", "2026-10-02T10:00:00+08:00", 4500, "Gamma Craft")
    _insert_txn(sandbox_db, "T2-AMT3", "2026-11-01T10:00:00+08:00", 3000, "Gamma Craft")

    # 4) 间隔不规则（非标准账期 / CV > 0.25）-> 拒绝
    _insert_txn(sandbox_db, "T2-IR1", "2026-08-01T10:00:00+08:00", 2900, "Delta Workshop")
    _insert_txn(sandbox_db, "T2-IR2", "2026-08-20T10:00:00+08:00", 2900, "Delta Workshop")
    _insert_txn(sandbox_db, "T2-IR3", "2026-10-15T10:00:00+08:00", 2900, "Delta Workshop")

    scan = scan_subscriptions(sandbox_db, now=now)
    names = {s["normalized_name"] for s in scan["subscriptions"]}
    assert names == {"Acme Cloud Labs"}
    assert scan["subscriptions"][0]["cadence_days"] == 30


def test_purge_false_positive_subscriptions_preserves_explicit_keep_and_user_rules(sandbox_db: SimingDB) -> None:
    ts = iso_now(_dt("2026-11-22 10:00"))

    # 种入用户手动创建的非订阅 Sure 规则（绝对不能被误删）
    user_rule = upsert_sure_rule(
        sandbox_db,
        name="山姆家庭分账",
        match_pattern="山姆",
        category="家居商超",
        split_ratio=0.5,
        is_recurring=False,
    )
    # 种入 sub_hunter 误建的 Sure 周期规则
    bad_rule1 = upsert_sure_rule(
        sandbox_db,
        name="订阅:美团平台商户 · 小象超市-美团外卖App",
        match_pattern="美团平台商户",
        category="订阅服务",
        is_recurring=True,
        cadence_days=7,
    )
    bad_rule2 = upsert_sure_rule(
        sandbox_db,
        name="订阅:滴滴出行 · 先乘车后付款",
        match_pattern="滴滴出行",
        category="订阅服务",
        is_recurring=True,
        cadence_days=7,
    )
    good_rule = upsert_sure_rule(
        sandbox_db,
        name="订阅:iCloud+",
        match_pattern=r"iCloud\+",
        category="订阅服务",
        is_recurring=True,
        cadence_days=30,
    )

    with sandbox_db.transaction() as conn:
        conn.executemany(
            """
            INSERT INTO subscription_audit (
                sub_id, merchant, normalized_name, cadence_days, amount_cents, currency,
                occurrences, last_charged_at, next_expected_at, prev_amount_cents,
                price_hike_pct, status, sure_rule_id, notes, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'CNY', ?, ?, ?, ?, 0.0, ?, ?, '', ?)
            """,
            [
                ("SUB-BAD1", "美团平台商户 · 小象超市-美团外卖App", "美团平台商户 · 小象超市-美团外卖App", 7, 7900, 61, ts, ts, 7900, "ACTIVE", bad_rule1["rule_id"], ts),
                ("SUB-BAD2", "滴滴出行 · 先乘车后付款", "滴滴出行 · 先乘车后付款", 7, 2500, 120, ts, ts, 2500, "ACTIVE", bad_rule2["rule_id"], ts),
                ("SUB-GOOD", "iCloud 由云上贵州运营", "iCloud+", 30, 600, 12, ts, ts, 600, "ACTIVE", good_rule["rule_id"], ts),
                ("SUB-KEEP", "用户自定义保留服务", "用户自定义保留服务", 30, 5000, 2, ts, ts, 5000, "KEEP", "", ts),
                ("SUB-CANC", "已手动取消项", "已手动取消项", 30, 1500, 2, ts, ts, 1500, "CANCELLED", "", ts),
            ],
        )

    res = purge_false_positive_subscriptions(sandbox_db)
    assert res["ok"] is True
    assert res["before_count"] == 5
    assert res["purged_subscriptions"] == 2
    assert res["after_count"] == 3
    assert res["purged_sure_rules"] == 2

    conn = sandbox_db.connect()
    try:
        remaining_ids = {r["sub_id"] for r in conn.execute("SELECT sub_id FROM subscription_audit").fetchall()}
    finally:
        conn.close()
    assert remaining_ids == {"SUB-GOOD", "SUB-KEEP", "SUB-CANC"}

    # 手动标记后依然保留
    mk = mark_subscription_status(sandbox_db, "SUB-GOOD", "KEEP")
    assert mk["ok"] is True

    remaining_rules = {r["rule_id"]: r["name"] for r in list_sure_rules(sandbox_db)}
    assert user_rule["rule_id"] in remaining_rules
    assert good_rule["rule_id"] in remaining_rules
    assert bad_rule1["rule_id"] not in remaining_rules
    assert bad_rule2["rule_id"] not in remaining_rules


# ---------------------------------------------------------------------------
# Point 4: Weekend / Holiday / Make-Up Workday & Location Guards
# ---------------------------------------------------------------------------


def test_overtime_meal_and_offwork_pickup_calendar_and_location_guards(sandbox_db: SimingDB) -> None:
    _write_calendar_and_policy(sandbox_db)

    # 种一条待取包裹 + 离司轨迹，确保工作日时能正常触发
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO cases (
                case_id, title, next_step, status, strength, category, rule_id,
                context_json, created_at, updated_at
            ) VALUES ('S50', '丰巢 · 示例小区A', '取件码 123456', '待处理', 'NORMAL', 'pickup', 'pickup_1800',
                '{"code":"123456","location":"示例小区A","site_alias":"家"}',
                '2026-10-01T10:00:00+08:00', '2026-10-01T10:00:00+08:00')
            """
        )

    def _set_office_loc() -> None:
        sandbox_db.set_meta(
            "latest_ios_location",
            json.dumps(
                {"place_alias": "公司", "lat": 39.908000, "lng": 116.407000, "updated_at": iso_now()},
                ensure_ascii=False,
            ),
        )

    def _seed_office_departure(day_str: str) -> None:
        with sandbox_db.transaction() as conn:
            conn.execute("DELETE FROM events WHERE source='ios_loc'")
            sandbox_db.append_event(
                conn,
                source="ios_loc",
                thread_key="loc:me",
                summary="离场: 公司",
                payload={"event": "离场: 公司", "place_alias": "公司"},
                occurred_at=f"{day_str}T19:30:00+08:00",
            )
        sandbox_db.set_meta("last_spatial_pickup_day", "")

    _set_office_loc()

    # 1) 周六（2026-11-21）-> 即使位置在公司或离司，也绝不发加班晚餐或下班取件！
    _seed_office_departure("2026-11-21")
    sat_meal = evaluate_overtime_meal_ab(sandbox_db, now=_dt("2026-11-21 20:10"), notify=True)
    sat_pick = check_and_fire_offwork_pickup(sandbox_db, now=_dt("2026-11-21 19:35"))
    assert sat_meal["skipped"] == "non_workday"
    assert sat_pick["skipped"] == "non_workday" and sat_pick["triggered"] is False

    # 2) 周日（2026-11-22）-> 同理跳过
    _seed_office_departure("2026-11-22")
    sun_meal = evaluate_overtime_meal_ab(sandbox_db, now=_dt("2026-11-22 20:10"), notify=True)
    sun_pick = check_and_fire_offwork_pickup(sandbox_db, now=_dt("2026-11-22 19:35"))
    assert sun_meal["skipped"] == "non_workday"
    assert sun_pick["skipped"] == "non_workday" and sun_pick["triggered"] is False

    # 3) 国庆法定节假日（2026-10-01 周四）-> 虽然是周四但法定放假，必须跳过！
    _seed_office_departure("2026-10-01")
    hol_meal = evaluate_overtime_meal_ab(sandbox_db, now=_dt("2026-10-01 20:10"), notify=True)
    hol_pick = check_and_fire_offwork_pickup(sandbox_db, now=_dt("2026-10-01 19:35"))
    assert hol_meal["skipped"] == "non_workday"
    assert hol_pick["skipped"] == "non_workday" and hol_pick["triggered"] is False

    # 4) 国庆调休补班周六（2026-10-10）-> 虽然是周六但是补班工作日，应正常生效！
    _seed_office_departure("2026-10-10")
    mk_meal = evaluate_overtime_meal_ab(sandbox_db, now=_dt("2026-10-10 20:10"), notify=True)
    mk_pick = check_and_fire_offwork_pickup(sandbox_db, now=_dt("2026-10-10 19:35"))
    assert mk_meal.get("mode_b_pushed") is True
    assert mk_pick.get("triggered") is True

    # 5) 工作日在家（家 alias 或 家坐标）-> 绝不发加班晚餐
    sandbox_db.set_meta("last_overtime_meal_b_day", "")
    sandbox_db.set_meta(
        "latest_ios_location",
        json.dumps({"place_alias": "家", "lat": 39.904200, "lng": 116.407400}, ensure_ascii=False),
    )
    home_meal = evaluate_overtime_meal_ab(sandbox_db, now=_dt("2026-11-23 20:10"), notify=True)
    assert home_meal["skipped"] == "not_at_office"

    # 6) 工作日关闭 overtime_meal 模块 -> 立即跳过
    _set_office_loc()
    set_module_enabled(sandbox_db, "overtime_meal", False)
    off_meal = evaluate_overtime_meal_ab(sandbox_db, now=_dt("2026-11-23 20:10"), notify=True)
    assert off_meal["skipped"] == "module_disabled"


def test_meal_guard_weekend_relaxation_sleep_state_and_module_toggle(sandbox_db: SimingDB) -> None:
    _write_calendar_and_policy(sandbox_db)

    # 1) 周六 13:30 午饭窗 -> 放宽不打扰补觉
    sat_lunch = evaluate_meal_regularity(sandbox_db, now=_dt("2026-11-21 13:30"), notify=True)
    assert sat_lunch["should_nudge"] is False
    assert sat_lunch["reason"] == "non_workday_relaxed"

    # 2) 周日 20:08 晚饭窗（< 20:30）-> 不打扰（彻底消除原 20:08 周日误提醒！）
    sun_early = evaluate_meal_regularity(sandbox_db, now=_dt("2026-11-22 20:08"), notify=True)
    assert sun_early["should_nudge"] is False
    assert sun_early["reason"] == "non_workday_relaxed"

    # 3) 周日 20:45 但未确认清醒（无 sleep:state=awake）-> 不打扰
    sun_unconfirmed = evaluate_meal_regularity(sandbox_db, now=_dt("2026-11-22 20:45"), notify=True)
    assert sun_unconfirmed["should_nudge"] is False
    assert sun_unconfirmed["reason"] == "non_workday_not_confirmed_awake"

    # 4) 周日 20:45 确认 sleep:state == awake 且无饮食消费 -> 温柔提醒，绝不含“加班”字样
    set_situation(sandbox_db, "sleep:state", {"state": "awake"}, source="test")
    sun_awake = evaluate_meal_regularity(sandbox_db, now=_dt("2026-11-22 20:45"), notify=True)
    assert sun_awake["should_nudge"] is True and sun_awake["nudged"] is True
    assert "加班" not in sun_awake["message"]
    assert "周末" in sun_awake["message"]

    # 5) 节假日（2026-10-01）20:45 若处于睡眠态 -> 不打扰
    set_situation(sandbox_db, "sleep:state", {"state": "asleep"}, source="test")
    record_explicit_sleep_state(sandbox_db, sleeping=True, source="qq", at_ts="2026-10-01T19:00:00+08:00")
    hol_sleep = evaluate_meal_regularity(sandbox_db, now=_dt("2026-10-01 20:45"), notify=True)
    assert hol_sleep["should_nudge"] is False

    # 6) 遵守 meal_combo_guard 模块开关（关闭后即便工作日也不提醒）
    set_situation(sandbox_db, "sleep:state", {"state": "awake"}, source="test")
    record_explicit_sleep_state(sandbox_db, sleeping=False, source="qq", at_ts="2026-11-23T12:00:00+08:00")
    set_module_enabled(sandbox_db, "meal_combo_guard", False)
    disabled_res = evaluate_meal_regularity(sandbox_db, now=_dt("2026-11-23 13:30"), notify=True)
    assert disabled_res["should_nudge"] is False
    assert disabled_res["reason"] == "module_disabled"
