"""Siming v5 真实多日生活场景端到端对抗测试套件（Adversarial Scenario QA）。

目标：模拟真实多日生活与工作使用，在用户发现前自动拦截以下高频隐患：
- Scenario 1: 中国日历、周末/法定节假日/调休补班与定位防误报（绝不在周末/节假日催加班餐或下班取件）
- Scenario 2: 120 笔高度噪杂真实账本下的订阅刺客精准度（零外卖/打车/咖啡/便利店/丰巢/网购误报 + 100% 真实订阅与涨价检出）
- Scenario 3: Tower 管理控制台 -> 运行时行为真实联动 E2E（模块开关生效、销账单真源原子联动、僵尸订阅持久化、已学知识与可视化图谱）
- Scenario 4: 跨模块知识联动链路（偏好/决策 + M-SIM + Curiosity + Bookmark + 流水 + 行程 -> Hindsight Wiki -> Context Graph -> Worker 自动注入 -> Tower 图谱）
- Scenario 5: 全量模块注册表完整性审计（合法 ID/Rule、无暂缓 W4-2/W4-4 泄漏、全模块开关往返无损）
"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any

import pytest

from siming.cn_calendar import day_info
from siming.context_graph import get_graph_stats, index_context_graph
from siming.control_tower import create_tower_server
from siming.daily_care import _injection_log, _save_pen_state, get_pen_state
from siming.db import SHANGHAI_TZ, SimingDB, iso_now
from siming.event_chain import get_situation, routines_md_path, set_situation
from siming.hindsight_wiki import distill_hindsight_wiki
from siming.meal_guard import evaluate_meal_regularity
from siming.modules import (
    MODULE_SPECS,
    is_module_enabled,
    resolve_module_spec,
    set_module_enabled,
)
from siming.morning import build_morning_editions
from siming.msim_memory import record_msim_entry
from siming.spatial_life import check_and_fire_offwork_pickup, evaluate_overtime_meal_ab
from siming.sub_hunter import scan_subscriptions
from siming.trip_packager import create_trip
from siming.waits import add_wait
from siming.worker import run_worker_for_wait


def _dt(iso_str: str) -> datetime:
    d = datetime.fromisoformat(iso_str)
    return d.replace(tzinfo=SHANGHAI_TZ) if d.tzinfo is None else d.astimezone(SHANGHAI_TZ)


def _set_ios_loc(db: SimingDB, alias: str, at_dt: datetime) -> None:
    payload = {
        "place_alias": alias,
        "label": "公司研发楼" if alias == "公司" else "示例小区A",
        "lat": 39.98 if alias == "公司" else 39.96,
        "lon": 116.33 if alias == "公司" else 116.35,
        "ts": iso_now(at_dt),
    }
    db.set_meta("latest_ios_location", json.dumps(payload, ensure_ascii=False))
    set_situation(db, "loc:me", payload, source="ios_loc", as_of=iso_now(at_dt))


def _seed_office_leave_and_pickup(db: SimingDB, day_dt: datetime, case_id: str) -> None:
    leave_dt = day_dt.replace(hour=19, minute=30, second=0, microsecond=0)
    arrive_office_dt = day_dt.replace(hour=10, minute=0, second=0, microsecond=0)
    with db.transaction() as conn:
        db.append_event(
            conn,
            source="ios_loc",
            thread_key=f"loc-{day_dt.date().isoformat()}",
            summary="到场: 公司",
            payload={"place_alias": "公司", "ot_event": "enter"},
            occurred_at=iso_now(arrive_office_dt),
            fingerprint=f"fp-enter-office-{case_id}-{day_dt.date().isoformat()}",
        )
        db.append_event(
            conn,
            source="ios_loc",
            thread_key=f"loc-{day_dt.date().isoformat()}",
            summary="离场: 公司",
            payload={"place_alias": "在途", "prev_alias": "公司", "ot_event": "leave"},
            occurred_at=iso_now(leave_dt),
            fingerprint=f"fp-leave-office-{case_id}-{day_dt.date().isoformat()}",
        )
        conn.execute(
            """
            INSERT OR REPLACE INTO cases (
                case_id, title, next_step, due_at, status, category, rule_id,
                context_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, '待处理', 'pickup', 'pickup_1800', ?, ?, ?)
            """,
            (
                case_id,
                "示例小区A丰巢快递待取",
                "凭取件码 884210 取件",
                iso_now(day_dt),
                json.dumps(
                    {
                        "code": "884210",
                        "brand": "丰巢",
                        "location": "示例小区A丰巢柜",
                        "site_alias": "家",
                    },
                    ensure_ascii=False,
                ),
                iso_now(arrive_office_dt),
                iso_now(arrive_office_dt),
            ),
        )


def _outbox_texts_by_rule(db: SimingDB, rule_ids: tuple[str, ...]) -> list[str]:
    conn = db.connect()
    try:
        placeholders = ",".join("?" for _ in rule_ids)
        rows = conn.execute(
            f"SELECT text FROM outbox WHERE rule_id IN ({placeholders}) ORDER BY created_at ASC",
            rule_ids,
        ).fetchall()
        return [str(r["text"]) for r in rows]
    finally:
        conn.close()


def _http_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    token: str = "",
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    body = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(f"{base_url}{path}", data=body, headers=headers, method=method)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


# ==============================================================================
# Scenario 1: Calendar & Weekend/Holiday Realism
# ==============================================================================


def test_scenario_1_calendar_weekend_holiday_and_makeup_workday_realism(
    sandbox_db: SimingDB,
    frozen_clock,
) -> None:
    """模拟周六(在家+在司)、周日、国庆法定节假日与周六调休补班，验证零周末/节假日误报且工作日正常触发。"""
    # --- 1A. 周六 20:05 在家 (2026-11-21 Saturday, non-workday) ---
    sat_dt = _dt("2026-11-21T20:05:00+08:00")
    frozen_clock(sat_dt)
    assert day_info(date(2026, 11, 21), sandbox_db.paths).is_workday is False

    _set_ios_loc(sandbox_db, "家", sat_dt)
    _seed_office_leave_and_pickup(sandbox_db, sat_dt, "S901")

    res_sat_home = evaluate_overtime_meal_ab(sandbox_db, now=sat_dt, notify=True)
    assert not res_sat_home.get("mode_b_pushed")
    assert res_sat_home.get("skipped") in ("not_workday", "non_workday", "not_at_office")

    res_sat_pickup_home = check_and_fire_offwork_pickup(sandbox_db, now=sat_dt)
    assert not res_sat_pickup_home.get("triggered")
    assert res_sat_pickup_home.get("skipped") in ("not_workday", "non_workday")

    # 饮食节律守卫在周末绝不能出现“加班”措辞
    set_situation(sandbox_db, "sleep:state", {"state": "awake"}, source="desktop", as_of=iso_now(sat_dt))
    mg_sat = evaluate_meal_regularity(sandbox_db, now=sat_dt, notify=True)
    assert "加班" not in str(mg_sat.get("message") or "")

    # --- 1B. 周六 20:05 即使定位在公司，非工作日也绝不误发“加班吃饭”或“下班取件” ---
    _set_ios_loc(sandbox_db, "公司", sat_dt)
    res_sat_office = evaluate_overtime_meal_ab(sandbox_db, now=sat_dt, notify=True)
    assert not res_sat_office.get("mode_b_pushed")
    assert res_sat_office.get("skipped") in ("not_workday", "non_workday")

    res_sat_pickup_office = check_and_fire_offwork_pickup(sandbox_db, now=sat_dt)
    assert not res_sat_pickup_office.get("triggered")
    assert res_sat_pickup_office.get("skipped") in ("not_workday", "non_workday")

    # --- 1C. 周日 19:45 在公司 (2026-11-22 Sunday, non-workday) ---
    sun_dt = _dt("2026-11-22T19:45:00+08:00")
    frozen_clock(sun_dt)
    assert day_info(date(2026, 11, 22), sandbox_db.paths).is_workday is False

    _set_ios_loc(sandbox_db, "公司", sun_dt)
    _seed_office_leave_and_pickup(sandbox_db, sun_dt, "S902")
    res_sun = evaluate_overtime_meal_ab(sandbox_db, now=sun_dt, notify=True)
    assert not res_sun.get("mode_a_ready")
    assert not res_sun.get("mode_b_pushed")
    assert res_sun.get("skipped") in ("not_workday", "non_workday")
    assert get_situation(sandbox_db, "prestage:dinner") is None

    res_sun_pickup = check_and_fire_offwork_pickup(sandbox_db, now=sun_dt)
    assert not res_sun_pickup.get("triggered")
    assert res_sun_pickup.get("skipped") in ("not_workday", "non_workday")

    # --- 1D. 国庆节法定假日 (2026-10-01 Thursday, isOffDay=True) 20:05 在公司 ---
    nat_dt = _dt("2026-10-01T20:05:00+08:00")
    frozen_clock(nat_dt)
    nat_info = day_info(date(2026, 10, 1), sandbox_db.paths)
    assert nat_info.is_workday is False
    assert "国庆" in nat_info.holiday_name

    _set_ios_loc(sandbox_db, "公司", nat_dt)
    _seed_office_leave_and_pickup(sandbox_db, nat_dt, "S903")
    res_nat = evaluate_overtime_meal_ab(sandbox_db, now=nat_dt, notify=True)
    assert not res_nat.get("mode_b_pushed")
    assert res_nat.get("skipped") in ("not_workday", "non_workday")

    res_nat_pickup = check_and_fire_offwork_pickup(sandbox_db, now=nat_dt)
    assert not res_nat_pickup.get("triggered")
    assert res_nat_pickup.get("skipped") in ("not_workday", "non_workday")

    # 确认前 4 个非工作日场景下，overtime_meal / spatial_pickup 零推送
    non_workday_msgs = _outbox_texts_by_rule(sandbox_db, ("overtime_meal", "spatial_pickup"))
    assert non_workday_msgs == [], f"非工作日绝不应出现加班餐或下班取件提醒，实际发出：{non_workday_msgs}"

    # --- 1E. 周六调休补班 (2026-10-10 Saturday, 国庆补班 isOffDay=False -> is_workday=True) ---
    makeup_dt = _dt("2026-10-10T20:05:00+08:00")
    frozen_clock(makeup_dt)
    makeup_info = day_info(date(2026, 10, 10), sandbox_db.paths)
    assert makeup_info.is_workday is True
    assert makeup_info.is_makeup_workday is True

    _set_ios_loc(sandbox_db, "公司", makeup_dt)
    res_makeup_meal = evaluate_overtime_meal_ab(sandbox_db, now=makeup_dt, notify=True)
    assert res_makeup_meal.get("mode_a_ready") is True
    assert res_makeup_meal.get("mode_b_pushed") is True
    assert "加班" in str(res_makeup_meal.get("card_text") or "")

    _seed_office_leave_and_pickup(sandbox_db, makeup_dt, "S904")
    res_makeup_pickup = check_and_fire_offwork_pickup(sandbox_db, now=makeup_dt)
    assert res_makeup_pickup.get("triggered") is True
    assert res_makeup_pickup.get("count", 0) >= 1

    # --- 1F. 正常工作日周五 (2026-11-20) 在家不推，在司正常推 ---
    fri_dt = _dt("2026-11-20T20:05:00+08:00")
    frozen_clock(fri_dt)
    assert day_info(date(2026, 11, 20), sandbox_db.paths).is_workday is True

    _set_ios_loc(sandbox_db, "家", fri_dt)
    res_fri_home = evaluate_overtime_meal_ab(sandbox_db, now=fri_dt, notify=True)
    assert res_fri_home.get("skipped") == "not_at_office"
    assert not res_fri_home.get("mode_b_pushed")

    _set_ios_loc(sandbox_db, "公司", fri_dt)
    res_fri_office = evaluate_overtime_meal_ab(sandbox_db, now=fri_dt, notify=True)
    assert res_fri_office.get("mode_b_pushed") is True


# ==============================================================================
# Scenario 2: Messy Real-World Ledger Subscription Precision (120 txns)
# ==============================================================================


def test_scenario_2_messy_120_txn_ledger_subscription_precision(
    sandbox_db: SimingDB,
    frozen_clock,
) -> None:
    """注入 120 笔真实噪杂流水（美团/滴滴/瑞幸/7-11/丰巢/京东/日常消费 + 4 个真实订阅含涨价），
    断言 subscription_audit 与 sure_rules 零日常消费污染，且 100% 检出真实订阅与涨价刺客。"""
    now_dt = _dt("2026-11-20T14:00:00+08:00")
    frozen_clock(now_dt)

    rows_to_insert: list[tuple[str, str, int, str, str, str]] = []
    seq = 0

    def _add(occ_dt: datetime, amount_yuan: float, merchant: str, notes: str = "", acct: str = "招行信用卡") -> None:
        nonlocal seq
        seq += 1
        rows_to_insert.append(
            (
                f"TXN-REAL-{seq:03d}",
                iso_now(occ_dt),
                int(round(amount_yuan * 100)),
                acct,
                merchant,
                notes,
            )
        )

    base = _dt("2026-08-01T12:15:00+08:00")

    # 1) 30 笔美团外卖（高频、且刻意包含若干接近 7 天/30 天间隔与相近金额的对抗样本）
    meituan_merchants = [
        ("美团外卖", "示例粉面炒米粉"),
        ("美团平台商户", "外卖订单"),
        ("美团外卖-示例瓦罐汤瓦罐汤", "晚餐"),
        ("美团闪购", "示例鲜果切"),
        ("美团买菜", "生鲜配送"),
    ]
    for i in range(30):
        m_name, m_note = meituan_merchants[i % len(meituan_merchants)]
        amt = 32.5 if i % 4 == 0 else (28.0 + (i % 7) * 3.5)
        _add(base + timedelta(days=i * 3 + (i % 2), hours=(i % 8)), amt, m_name, m_note)

    # 2) 20 笔滴滴/网约车
    didi_merchants = ["滴滴出行", "滴滴快车", "高德打车"]
    for i in range(20):
        amt = 24.0 if i % 3 == 0 else (18.5 + (i % 5) * 7.2)
        _add(base + timedelta(days=i * 5 + 1, hours=9), amt, didi_merchants[i % len(didi_merchants)], "通勤打车")

    # 3) 15 笔瑞幸/咖啡奶茶（刻意包含固定 ¥9.9 金额对抗）
    coffee_merchants = ["瑞幸咖啡", "星巴克", "霸王茶姬"]
    for i in range(15):
        amt = 9.9 if i % 2 == 0 else 18.0
        _add(base + timedelta(days=i * 7, hours=10), amt, coffee_merchants[i % len(coffee_merchants)], "咖啡饮品")

    # 4) 10 笔 7-11 / 便利店 / 超市
    mart_merchants = ["7-11便利店", "罗森便利店", "盒马鲜生", "山姆会员商店"]
    for i in range(10):
        amt = 22.0 if i % 2 == 0 else 168.0
        _add(base + timedelta(days=i * 10 + 2, hours=18), amt, mart_merchants[i % len(mart_merchants)], "日用采购")

    # 5) 6 笔丰巢超时保管费（固定 ¥0.50 / ¥1.00，每 14 天出现，强对抗！）
    for i in range(6):
        _add(base + timedelta(days=i * 14 + 3, hours=19), 0.5, "丰巢快递柜", "丰巢超时畅存费")

    # 6) 8 笔京东普通购物
    for i in range(8):
        _add(base + timedelta(days=i * 13 + 4, hours=21), 129.0 + i * 40.0, "京东商城", "日常网购")

    # 7) 14 笔其他日常非订阅消费（ETC/充电/快递/快餐/淘宝/拼多多）
    misc_samples = [
        ("北京高速ETC", 25.0, "高速通行费"),
        ("特来电充电", 48.0, "特斯拉充电"),
        ("顺丰速运", 18.0, "寄件运费"),
        ("肯德基", 39.0, "快餐"),
        ("淘宝商城", 88.0, "网购百货"),
        ("拼多多商户", 29.9, "日用品"),
        ("示例小区A地下停车场", 16.0, "停车费"),
    ]
    for i in range(14):
        m_name, amt, note = misc_samples[i % len(misc_samples)]
        _add(base + timedelta(days=i * 7 + 2, hours=15), amt, m_name, note)

    # 8) 4 个真实订阅（共 17 笔，使总笔数精确 = 120）
    # 8a. iCloud+ ¥6/月 × 4
    for mo_idx, day_str in enumerate(("2026-08-15", "2026-09-15", "2026-10-15", "2026-11-15")):
        _add(_dt(f"{day_str}T06:00:00+08:00"), 6.0, "Apple iCloud+", "iCloud+ 50GB 连续包月")

    # 8b. 点众小说 ¥15/7天 × 5
    dz_start = _dt("2026-10-20T08:00:00+08:00")
    for w in range(5):
        _add(dz_start + timedelta(days=w * 7), 15.0, "点众小说", "点众小说连续包周自动续费")

    # 8c. PC Game Pass ¥39/月 × 4
    for day_str in ("2026-08-10", "2026-09-10", "2026-10-10", "2026-11-10"):
        _add(_dt(f"{day_str}T09:30:00+08:00"), 39.0, "Microsoft PC Game Pass", "Xbox Game Pass 月度订阅")

    # 8d. 迅雷 ¥19/月 × 3 -> 11 月悄悄涨价至 ¥25/月 (+31.58%)
    for day_str, yuan in (
        ("2026-08-18", 19.0),
        ("2026-09-18", 19.0),
        ("2026-10-18", 19.0),
        ("2026-11-18", 25.0),
    ):
        _add(_dt(f"{day_str}T10:00:00+08:00"), yuan, "迅雷超级会员", "迅雷连续包月自动续费")

    assert len(rows_to_insert) == 120

    with sandbox_db.transaction() as conn:
        for txn_id, occ_s, cents, acct, merchant, notes in rows_to_insert:
            conn.execute(
                """
                INSERT INTO ledger_txns (
                    txn_id, occurred_at, amount_cents, currency, account_alias, card_last4,
                    kind, status, merchant, notes, created_at, updated_at
                ) VALUES (?, ?, ?, 'CNY', ?, '1003', '消费', 'POSTED', ?, ?, ?, ?)
                """,
                (txn_id, occ_s, cents, acct, merchant, notes, iso_now(now_dt), iso_now(now_dt)),
            )

    res = scan_subscriptions(sandbox_db, now=now_dt)
    assert res["ok"] is True

    conn = sandbox_db.connect()
    try:
        sub_rows = [dict(r) for r in conn.execute("SELECT * FROM subscription_audit").fetchall()]
        rule_rows = [dict(r) for r in conn.execute("SELECT * FROM sure_rules WHERE is_recurring = 1").fetchall()]
    finally:
        conn.close()

    banned_keywords = (
        "美团", "外卖", "滴滴", "打车", "瑞幸", "星巴克", "霸王茶姬",
        "7-11", "罗森", "盒马", "山姆", "便利店", "丰巢", "京东商城",
        "ETC", "特来电", "顺丰", "肯德基", "淘宝", "拼多多", "停车",
    )

    for r in sub_rows:
        hay = f"{r['normalized_name']} {r['merchant']}"
        for bad in banned_keywords:
            assert bad not in hay, f"日常消费误入 subscription_audit: {hay!r} (命中黑词 {bad!r})"

    for rule in rule_rows:
        hay = f"{rule['name']} {rule['match_pattern']}"
        for bad in banned_keywords:
            assert bad not in hay, f"日常消费误入 sure_rules: {hay!r} (命中黑词 {bad!r})"

    sub_by_norm = {str(r["normalized_name"]): r for r in sub_rows}
    assert "iCloud+" in sub_by_norm, f"漏检 iCloud+，当前检出：{list(sub_by_norm)}"
    assert "点众小说" in sub_by_norm, f"漏检 点众小说，当前检出：{list(sub_by_norm)}"
    assert "PC Game Pass" in sub_by_norm, f"漏检 PC Game Pass，当前检出：{list(sub_by_norm)}"
    assert "迅雷" in sub_by_norm, f"漏检 迅雷，当前检出：{list(sub_by_norm)}"
    assert len(sub_rows) == 4, f"应仅检出 4 个真实订阅，实际检出 {len(sub_rows)} 个：{list(sub_by_norm)}"

    assert int(sub_by_norm["iCloud+"]["cadence_days"]) == 30
    assert int(sub_by_norm["点众小说"]["cadence_days"]) == 7
    assert int(sub_by_norm["PC Game Pass"]["cadence_days"]) == 30
    xunlei = sub_by_norm["迅雷"]
    assert int(xunlei["cadence_days"]) == 30
    assert int(xunlei["amount_cents"]) == 2500
    assert int(xunlei["prev_amount_cents"]) == 1900
    assert float(xunlei["price_hike_pct"]) > 25.0

    # 同步检验历史误报清理函数 purge_false_positive_subscriptions
    from siming import sub_hunter as _sh

    assert hasattr(_sh, "purge_false_positive_subscriptions"), "缺少 purge_false_positive_subscriptions 清理接口"
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO subscription_audit (
                sub_id, merchant, normalized_name, cadence_days, amount_cents, currency,
                occurrences, last_charged_at, next_expected_at, status, sure_rule_id, updated_at
            ) VALUES ('SUB-POLLUTED-MT', '美团外卖', '美团外卖', 7, 3250, 'CNY', 12, ?, ?, 'ACTIVE', 'SR-POLLUTED-MT', ?)
            """,
            (iso_now(now_dt), iso_now(now_dt + timedelta(days=7)), iso_now(now_dt)),
        )
        conn.execute(
            """
            INSERT INTO sure_rules (
                rule_id, name, match_pattern, account_name, category, tags_json,
                split_ratio, is_recurring, cadence_days, created_at
            ) VALUES ('SR-POLLUTED-MT', '订阅·美团外卖', '美团外卖', '', '订阅', '["subscription","sub_hunter"]', 1.0, 1, 7, ?)
            """,
            (iso_now(now_dt),),
        )

    purge_res = _sh.purge_false_positive_subscriptions(sandbox_db)
    assert purge_res.get("ok") is True
    conn = sandbox_db.connect()
    try:
        rem_mt_sub = conn.execute("SELECT 1 FROM subscription_audit WHERE sub_id='SUB-POLLUTED-MT'").fetchone()
        rem_mt_rule = conn.execute("SELECT 1 FROM sure_rules WHERE rule_id='SR-POLLUTED-MT'").fetchone()
        rem_real_cnt = int(conn.execute("SELECT COUNT(*) FROM subscription_audit").fetchone()[0])
    finally:
        conn.close()
    assert rem_mt_sub is None, "purge_false_positive_subscriptions 未清理污染订阅行"
    assert rem_mt_rule is None, "purge_false_positive_subscriptions 未清理污染 sure_rules 行"
    assert rem_real_cnt == 4


# ==============================================================================
# Scenario 3: Tower Management Console -> Runtime Behavior E2E
# ==============================================================================


def test_scenario_3_tower_management_console_to_runtime_e2e(
    sandbox_db: SimingDB,
    frozen_clock,
) -> None:
    """启动真实 ephemeral 端口 Control Tower，端到端验证：
    1. POST /api/modules/<id>/toggle 真实抑制/恢复运行时行为并写审计；
    2. POST /api/cases/<cid>/close 单真源原子更新 cases + subscriptions + daily_care + morning_brief；
    3. POST /api/subs/<id>/status 持久化并在 /api/finance 可见；
    4. GET /api/learned 与 GET /api/graph/visual 返回真实结构化数据；
    5. 写接口强制鉴权保护（401）。
    """
    fri_dt = _dt("2026-11-20T20:05:00+08:00")
    frozen_clock(fri_dt)
    _set_ios_loc(sandbox_db, "公司", fri_dt)

    # 准备 med_injection case S77 + 关联 subscription + 初始 4 针库存
    _save_pen_state(sandbox_db, {"doses_left": 4, "last_visit": "2026-11-01", "updated_at": iso_now(fri_dt)})
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO cases (
                case_id, title, next_step, due_at, status, category, rule_id,
                context_json, created_at, updated_at
            ) VALUES (
                'S77', '本周周期用药打针提醒', '打完回「打了」扣库存', ?, '待处理', 'med_injection', 'med_care',
                '{"slot":"2026-11-18"}', ?, ?
            )
            """,
            (iso_now(fri_dt), iso_now(fri_dt), iso_now(fri_dt)),
        )
        conn.execute(
            """
            INSERT INTO subscriptions (sub_key, target_kind, target_id, state, created_at, metadata_json)
            VALUES ('case:S77', 'case', 'S77', 'ACTIVE', ?, '{}')
            """,
            (iso_now(fri_dt),),
        )
        # 预置 1 条真实订阅 + 学习知识资产（wiki / hypothesis / msim / curiosity / bookmark）
        conn.execute(
            """
            INSERT INTO subscription_audit (
                sub_id, merchant, normalized_name, cadence_days, amount_cents, currency,
                occurrences, last_charged_at, next_expected_at, status, updated_at
            ) VALUES ('SUB-DZ-01', '点众小说', '点众小说', 7, 1500, 'CNY', 4, ?, ?, 'ACTIVE', ?)
            """,
            (iso_now(fri_dt - timedelta(days=5)), iso_now(fri_dt + timedelta(days=2)), iso_now(fri_dt)),
        )
        conn.execute(
            """
            INSERT INTO wiki_pages (slug, title, category, content_md, sources_json, conflicts_json, updated_at, last_distilled_at)
            VALUES ('preferences', '用户偏好与工程习惯', 'preference',
                    '统一使用 pixi 管理 Python 环境，严禁写 /tmp。', '["session"]', '[]', ?, ?)
            """,
            (iso_now(fri_dt), iso_now(fri_dt)),
        )
        conn.execute(
            """
            INSERT INTO hypotheses (id, claim, evidence_ids, status, expires_at, created_at)
            VALUES ('H-01', '工作日 20:05 仍在公司大概率需要加班晚餐提醒', '["EV-1"]', 'OBSERVING', ?, ?)
            """,
            (iso_now(fri_dt + timedelta(days=30)), iso_now(fri_dt)),
        )
        conn.execute(
            """
            INSERT INTO curiosity_questions (qid, topic, question, context_json, status, answer, created_at, answered_at)
            VALUES ('CQ-01', 'routine_drift', '周六通常在家休息不上班？', '{}', 'ANSWERED', '是的，周六日不上班除非调休补班', ?, ?)
            """,
            (iso_now(fri_dt - timedelta(days=1)), iso_now(fri_dt)),
        )
        conn.execute(
            """
            INSERT INTO bookmarks (bookmark_id, kind, title, url, repo_slug, summary, related_projects_json, tags_json, created_at)
            VALUES ('BM-01', 'github', 'Linger Context Graph', 'https://github.com/example/linger', 'example/linger',
                    '跨应用只读联邦图谱参考', '["siming"]', '["graph","wiki"]', ?)
            """,
            (iso_now(fri_dt),),
        )
    record_msim_entry(
        sandbox_db,
        problem_family="siming",
        source_model="claude-opus",
        change_rationale="用单真源 close_case 统一闭环 cases 与 daily_care",
        expected_outcome="消除晨札与打针提醒重复打扰",
        measured_score=0.95,
        baseline_score=0.72,
        passed=True,
        tags=["siming", "daily_care"],
    )
    routines_md_path(sandbox_db).parent.mkdir(parents=True, exist_ok=True)
    routines_md_path(sandbox_db).write_text("# Routines\n- 周末不上班，绝不催加班餐\n", encoding="utf-8")
    index_context_graph(sandbox_db)

    # 关单前晨札应感知待处理的 S77
    brief_before = build_morning_editions(sandbox_db)
    brief_before_txt = str(brief_before.get("full") or "") + str(brief_before.get("concise") or "")
    assert "S77" in brief_before_txt or "周期用药" in brief_before_txt

    token = "scenario-tower-secret-token-2026"
    server = create_tower_server(sandbox_db, host="127.0.0.1", port=0, token=token)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()

    try:
        # 0) 鉴权保护：无 token 调用写接口必须 401
        with pytest.raises(urllib.error.HTTPError) as exc_toggle_401:
            _http_json(base_url, "/api/modules/overtime_meal/toggle", method="POST", payload={"enabled": False})
        assert exc_toggle_401.value.code == 401

        with pytest.raises(urllib.error.HTTPError) as exc_close_401:
            _http_json(base_url, "/api/cases/S77/close", method="POST", payload={"cancel": False})
        assert exc_close_401.value.code == 401

        # 1) Tower 关闭 overtime_meal -> 运行时被真实抑制 -> 重新开启 -> 运行时恢复触发
        st_off, resp_off = _http_json(
            base_url,
            "/api/modules/overtime_meal/toggle",
            method="POST",
            token=token,
            payload={"enabled": False},
        )
        assert st_off == 200 and resp_off.get("ok") is True and resp_off.get("enabled") is False
        assert is_module_enabled(sandbox_db, "overtime_meal") is False

        suppressed = evaluate_overtime_meal_ab(sandbox_db, now=fri_dt, notify=True)
        assert suppressed.get("skipped") == "module_disabled"
        assert not suppressed.get("mode_b_pushed")

        _, mods_after_off = _http_json(base_url, "/api/modules", token=token)
        ot_entry = next(m for m in mods_after_off["modules"] if m["module_id"] == "overtime_meal")
        assert ot_entry["enabled"] is False

        st_on, resp_on = _http_json(
            base_url,
            "/api/modules/overtime_meal/toggle",
            method="POST",
            token=token,
            payload={"enabled": True},
        )
        assert st_on == 200 and resp_on.get("ok") is True and resp_on.get("enabled") is True
        assert is_module_enabled(sandbox_db, "overtime_meal") is True

        restored = evaluate_overtime_meal_ab(sandbox_db, now=fri_dt, notify=True)
        assert restored.get("mode_b_pushed") is True

        # 2) Tower 销账 S77 -> 原子更新 cases / subscriptions / daily_care / morning_brief
        st_cls, resp_cls = _http_json(
            base_url,
            "/api/cases/S77/close",
            method="POST",
            token=token,
            payload={"cancel": False, "note": "tower"},
        )
        assert st_cls == 200 and resp_cls.get("ok") is True
        assert resp_cls.get("status") == "完成"

        conn = sandbox_db.connect()
        try:
            c77 = conn.execute("SELECT status, closed_at FROM cases WHERE case_id='S77'").fetchone()
            sub77 = conn.execute("SELECT state, ended_at FROM subscriptions WHERE target_id='S77'").fetchone()
            audit_toggle = conn.execute(
                "SELECT COUNT(*) FROM audit WHERE action='module_toggle' AND target_id='overtime_meal'"
            ).fetchone()[0]
        finally:
            conn.close()

        assert c77 is not None and c77["status"] == "完成" and c77["closed_at"]
        assert sub77 is not None and sub77["state"] == "ENDED" and sub77["ended_at"]
        assert int(audit_toggle) >= 2

        inj_log = _injection_log(sandbox_db)
        assert any(str(x.get("slot")) == "2026-11-18" and not x.get("skipped") for x in inj_log)
        assert get_pen_state(sandbox_db).get("doses_left") == 3

        brief_after = build_morning_editions(sandbox_db)
        brief_after_txt = str(brief_after.get("full") or "") + str(brief_after.get("concise") or "")
        assert "S77" not in brief_after_txt

        # 3) Tower 标记订阅 ZOMBIE -> 持久化并反映在 /api/finance
        st_sub, resp_sub = _http_json(
            base_url,
            "/api/subs/SUB-DZ-01/status",
            method="POST",
            token=token,
            payload={"status": "ZOMBIE"},
        )
        assert st_sub == 200 and resp_sub.get("ok") is True

        _, fin_data = _http_json(base_url, "/api/finance", token=token)
        dz_sub = next(s for s in fin_data["subscriptions"] if s["sub_id"] == "SUB-DZ-01")
        assert dz_sub["status"] == "ZOMBIE"

        # 4) GET /api/learned 与 GET /api/graph/visual 返回真实结构化内容
        st_lrn, lrn_data = _http_json(base_url, "/api/learned", token=token)
        assert st_lrn == 200 and lrn_data.get("ok") is True
        assert len(lrn_data.get("wiki_pages") or []) >= 1
        assert len(lrn_data.get("hypotheses") or []) >= 1
        assert len(lrn_data.get("msim_entries") or []) >= 1
        assert len(lrn_data.get("curiosity_questions") or []) >= 1
        assert len(lrn_data.get("bookmarks") or []) >= 1

        st_gv, gv_data = _http_json(base_url, "/api/graph/visual", token=token)
        assert st_gv == 200 and gv_data.get("ok") is True
        nodes = gv_data.get("nodes") or []
        edges = gv_data.get("edges") or []
        assert len(nodes) >= 3, f"视觉图谱节点过少：{len(nodes)}"
        assert all((n.get("node_id") or n.get("id")) and n.get("app") and n.get("title") for n in nodes)
        assert isinstance(edges, list)
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=3)


# ==============================================================================
# Scenario 4: Cross-Module Knowledge Linkage Chain
# ==============================================================================


def test_scenario_4_cross_module_knowledge_linkage_chain(
    sandbox_db: SimingDB,
    frozen_clock,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """种子化真实决策/偏好 + M-SIM + 已答 Curiosity + Bookmark + 流水 + 行程，
    验证：distill_hindsight_wiki -> index_context_graph -> worker prompt 自动注入 Wiki+M-SIM -> Tower 视觉图谱跨应用连边！"""
    now_dt = _dt("2026-11-20T15:00:00+08:00")
    frozen_clock(now_dt)

    # 1) 写入真实决策/偏好/红线到 DB (feedback + hypotheses + curiosity + bookmarks + routines.md)
    routines_md_path(sandbox_db).parent.mkdir(parents=True, exist_ok=True)
    routines_md_path(sandbox_db).write_text(
        "# Siming Routines\n"
        "- 决定采用 pixi 统一管理 siming 仓库的 Python 环境，禁止往 /tmp 写临时文件。\n"
        "- 偏好使用 pytest 搭配 frozen_clock 进行东京差旅与定时逻辑测试。\n",
        encoding="utf-8",
    )
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO feedback (target_id, rule_id, action, note, ts)
            VALUES ('S10', 'worker_run', '纠正', '决定采用 pixi 执行 pytest，禁止直接写 /tmp 目录', ?)
            """,
            (iso_now(now_dt),),
        )
        conn.execute(
            """
            INSERT INTO hypotheses (id, claim, evidence_ids, status, expires_at, created_at)
            VALUES ('H-LINK-1', '偏好使用 pixi 运行 siming 测试并遵循东京差旅中行1003刷卡规则', '["EV-L1"]', 'CONFIRMED', ?, ?)
            """,
            (iso_now(now_dt + timedelta(days=30)), iso_now(now_dt)),
        )
        conn.execute(
            """
            INSERT INTO curiosity_questions (qid, topic, question, context_json, status, answer, created_at, answered_at)
            VALUES ('CQ-LINK-1', 'trip_intent', '2026-11-12 东京行程刷哪张卡最划算？', '{}', 'ANSWERED',
                    '决定采用中行全币种卡尾号1003支付东京机票与酒店并归入CW01报销', ?, ?)
            """,
            (iso_now(now_dt - timedelta(days=2)), iso_now(now_dt)),
        )
        conn.execute(
            """
            INSERT INTO bookmarks (bookmark_id, kind, title, url, repo_slug, summary, related_projects_json, tags_json, created_at)
            VALUES ('BM-LINK-1', 'github', 'pixi-siming-harness', 'https://github.com/example/pixi-siming',
                    'example/pixi-siming', 'pixi 虚拟环境与东京账单对账工具', '["siming"]', '["pixi","东京","siming"]', ?)
            """,
            (iso_now(now_dt),),
        )
        conn.execute(
            """
            INSERT INTO ledger_txns (
                txn_id, occurred_at, amount_cents, currency, account_alias, card_last4,
                kind, status, merchant, notes, created_at, updated_at
            ) VALUES ('TXN-LINK-01', '2026-11-12T10:00:00+08:00', 385000, 'CNY', '中行全币种', '1003',
                      '消费', 'POSTED', '携程东京机票', '2026-11-12 东京往返机票 CW01 报销', ?, ?)
            """,
            (iso_now(now_dt), iso_now(now_dt)),
        )

    create_trip(sandbox_db, destination="东京", start_date="2026-11-12", end_date="2026-11-16", country="JP")

    record_msim_entry(
        sandbox_db,
        problem_family="siming",
        target_artifact="src/siming/worker.py",
        source_model="claude-opus",
        change_rationale="在 siming worker 调度前优先用 pixi 环境校验 pytest，并过滤 /tmp 临时路径",
        expected_outcome="降低多模型切换时的环境漂移失败率",
        measured_score=0.96,
        baseline_score=0.74,
        passed=True,
        eval_context="pytest",
        tags=["pixi", "siming", "pytest", "东京"],
    )

    # 2) 运行 Hindsight Wiki 蒸馏（零外部文件参数，必须能直接从 DB 沉淀真实 Wiki 页！）
    wiki_res = distill_hindsight_wiki(sandbox_db)
    assert wiki_res.get("ok") is True
    pages_written = wiki_res.get("pages_written")
    pages_cnt = len(pages_written) if isinstance(pages_written, list) else int(pages_written or 0)
    assert pages_cnt >= 1, f"distill_hindsight_wiki 应从 DB 蒸馏出至少 1 个页面：{wiki_res}"

    conn = sandbox_db.connect()
    try:
        wiki_rows = [dict(r) for r in conn.execute("SELECT slug, title, content_md FROM wiki_pages").fetchall()]
    finally:
        conn.close()
    assert len(wiki_rows) >= 1
    all_wiki_md = "\n".join(r["content_md"] for r in wiki_rows)
    assert ("pixi" in all_wiki_md.lower()) or ("1003" in all_wiki_md) or ("/tmp" in all_wiki_md)

    # 3) 运行跨应用上下文图谱索引 -> 必须覆盖多应用且生成跨应用边
    idx_res = index_context_graph(sandbox_db)
    assert idx_res.get("ok") is True
    assert idx_res.get("nodes_indexed", 0) >= 5
    assert idx_res.get("edges_created", 0) >= 1, f"跨应用图谱应构建实体连边：{idx_res}"
    stats = get_graph_stats(sandbox_db)
    assert stats["total_nodes"] >= 5
    assert stats["total_edges"] >= 1
    assert len(stats.get("by_app") or {}) >= 3

    # 4) 验证 worker.py 执行链路自动注入 Hindsight Wiki + M-SIM 记忆到任务 prompt
    work_dir = sandbox_db.paths.tmp_root / "siming-sandbox-repo"
    work_dir.mkdir(parents=True, exist_ok=True)
    # 将沙盒目录临时加入白名单以便 mandate 放行 run 模式
    policy_path = sandbox_db.paths.config_dir / "policy.yaml"
    if policy_path.exists():
        import yaml

        pol = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
        pol.setdefault("whitelisted_repos", {})["siming-sandbox"] = str(work_dir)
        policy_path.write_text(yaml.safe_dump(pol, allow_unicode=True), encoding="utf-8")

    w_res = add_wait(
        sandbox_db,
        when=f"time:{iso_now(now_dt)}",
        task="修复 siming 的 pixi 与 pytest 测试环境配置并避免 /tmp 写入",
        mode="run",
        cwd=str(work_dir),
        strength="explicit",
        quote="帮我用 pixi 跑 siming 的 pytest 修复配置",
    )
    wid = w_res["wait_id"]

    captured_prompts: list[str] = []

    def _fake_coding_plan(prompt: str, **kwargs: Any) -> dict[str, Any]:
        captured_prompts.append(prompt)
        return {"ok": True, "output": "已完成 pixi pytest 配置验证", "runner": "codex", "model": "codex-5.3"}

    monkeypatch.setattr("siming.worker.run_coding_plan_prompt", _fake_coding_plan)
    worker_out = run_worker_for_wait(sandbox_db, wait_id=wid, trigger_reason="scenario_test")
    assert worker_out.get("ok") is True, f"worker 执行失败：{worker_out}"
    assert len(captured_prompts) == 1
    injected_prompt = captured_prompts[0]
    assert "Hindsight" in injected_prompt or "Wiki" in injected_prompt or "共享知识" in injected_prompt, (
        f"worker prompt 未自动注入 Hindsight Wiki 共享知识：{injected_prompt[:500]}"
    )
    assert "M-SIM" in injected_prompt or "claude-opus" in injected_prompt or "跨模型" in injected_prompt, (
        f"worker prompt 未自动注入 M-SIM 跨模型记忆：{injected_prompt[:500]}"
    )

    # 5) 验证 Tower /api/graph/visual 暴露跨应用节点与边
    token = "scenario4-tower-token"
    server = create_tower_server(sandbox_db, host="127.0.0.1", port=0, token=token)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    try:
        st, gv = _http_json(base_url, "/api/graph/visual", token=token)
        assert st == 200 and gv.get("ok") is True
        apps_present = {str(n.get("app") or "") for n in (gv.get("nodes") or [])}
        assert len(apps_present) >= 3, f"视觉图谱应跨越至少 3 个应用域，实际：{apps_present}"
        assert len(gv.get("edges") or []) >= 1, "视觉图谱缺少跨应用连边"
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=3)


# ==============================================================================
# Scenario 5: Module Registry Integrity Audit
# ==============================================================================


def test_scenario_5_module_registry_integrity_audit(sandbox_db: SimingDB) -> None:
    """全量审计 MODULE_SPECS：合法 ID/Rule、无重复、无已暂缓 W4-2/W4-4 泄漏、所有模块开关往返无损。"""
    assert len(MODULE_SPECS) >= 40, f"模块数量异常偏少：{len(MODULE_SPECS)}"

    id_re = re.compile(r"^[a-z][a-z0-9_]{1,48}$")
    deferred_patterns = (
        "w4-2", "w4-4", "个税", "体检", "跑腿", "证件死线", "tax_deduction", "errand", "id_deadline",
    )

    seen_module_ids: set[str] = set()
    for spec in MODULE_SPECS:
        assert id_re.match(spec.module_id), f"非法 module_id: {spec.module_id!r}"
        assert id_re.match(spec.rule_id), f"非法 rule_id: {spec.rule_id!r}"
        assert spec.module_id not in seen_module_ids, f"重复 module_id: {spec.module_id!r}"
        seen_module_ids.add(spec.module_id)

        assert spec.title.strip(), f"模块 {spec.module_id} 缺少 title"
        assert spec.description.strip(), f"模块 {spec.module_id} 缺少 description"
        assert len(spec.aliases) >= 1 and all(a.strip() for a in spec.aliases), f"模块 {spec.module_id} aliases 非法"

        combined_low = " ".join(
            [spec.module_id, spec.rule_id, spec.title, spec.description, *spec.aliases]
        ).lower()
        for bad in deferred_patterns:
            assert bad not in combined_low, f"模块 {spec.module_id} 泄漏已暂缓特性关键词 {bad!r}: {spec.title}"

        # 解析一致性
        assert resolve_module_spec(spec.module_id) == spec

        # 开关完整往返
        off_res = set_module_enabled(sandbox_db, spec.module_id, False)
        assert off_res.get("ok") is True and off_res.get("enabled") is False
        assert is_module_enabled(sandbox_db, spec.module_id) is False

        on_res = set_module_enabled(sandbox_db, spec.module_id, True)
        assert on_res.get("ok") is True and on_res.get("enabled") is True
        assert is_module_enabled(sandbox_db, spec.module_id) is True
