"""Siming v5 主动红队对抗场景测试套件（Temporal / Sleep / Quiet-Hours, Alert Spam Storms, Cross-Midnight & Module-Off Enforcement）。

主动前置拦截以下 5 类高危真实生活故障：
1. Repeated Tick Alert Storm / Dedup Chaos（30 分钟内连跑 4 次 tick，严防 QQ 刷屏与 HAOS 重复下发）
2. Night-Owl 03:30 AM & 09:30 AM Sleep-In Protection（`sleep:state` / 显式睡眠保护、扫地机硬锁、紧急穿透）
3. Cross-Midnight Logical Day & Year-Rollover Chaos（跨年 `2026-12-31 23:55 -> 2027-01-01 00:45 -> 05:10` 与 `2028-01-01` 搬家切轨幂等）
4. Module-Off Direct Enforcement Audit（关闭模块后直接调用守卫函数，严禁绕过 `evald` 继续发通知/改状态）
5. HiveBox False-Urgent Edge Cases（在公司不误破静、新房非顺路不误催、已关单+旧事件不复活、已取件短信与菜鸟驿站不误归类丰巢）
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from siming.attention import (
    evaluate_attention_state,
    record_explicit_sleep_state,
    should_defer_morning_brief_for_sleep,
)
from siming.commute_hub import (
    check_regime_transition,
    stage_commute_rescue_bundle,
)
from siming.daily_care import (
    active_regime,
    dose_slot_for,
    format_care_report,
    load_care_cfg,
    run_injection_guard,
    run_supply_radar,
    run_weather_guard,
    set_supply_stock,
)
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, logical_today
from siming.evald import run_tick
from siming.event_chain import get_situation, set_situation
from siming.haos_night_owl import (
    evaluate_night_owl_haos_rules,
    is_vacuum_locked,
    sync_night_owl_to_haos,
)
from siming.meal_guard import evaluate_meal_regularity
from siming.modules import set_module_enabled
from siming.parcel_sentinel import (
    evaluate_hivebox_countdown,
    evaluate_return_window_sentinel,
    run_parcel_sentinel,
)
from siming.push import push_external, push_message
from siming.tesla_link import evaluate_departure_auto_climate, ingest_tesla_state


def _dt(iso_str: str) -> datetime:
    d = datetime.fromisoformat(iso_str)
    return d.replace(tzinfo=SHANGHAI_TZ) if d.tzinfo is None else d.astimezone(SHANGHAI_TZ)


def _outbox_rows(db: SimingDB, rule_id: str | None = None) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        if rule_id:
            rows = conn.execute(
                "SELECT * FROM outbox WHERE rule_id = ? ORDER BY rowid ASC", (rule_id,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM outbox ORDER BY rowid ASC").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _meteo(
    day: date,
    *,
    today_feel: float = 1.0,
    y_feel: float = 10.0,
    rain_hours: tuple[int, ...] = (9,),
    raining_now: bool = False,
) -> dict[str, Any]:
    times, temp, feel, prob, mm, code, wind = [], [], [], [], [], [], []
    for off in (-1, 0, 1):
        d = day + timedelta(days=off)
        for h in range(24):
            times.append(f"{d.isoformat()}T{h:02d}:00")
            base = y_feel if off == -1 else today_feel
            temp.append(base + 2.0)
            feel.append(base)
            wet = off == 0 and h in rain_hours
            prob.append(85 if wet else 5)
            mm.append(1.5 if wet else 0.0)
            code.append(61 if wet else 1)
            wind.append(12.0)
    return {
        "hourly": {
            "time": times,
            "temperature_2m": temp,
            "apparent_temperature": feel,
            "precipitation_probability": prob,
            "precipitation": mm,
            "weather_code": code,
            "wind_speed_10m": wind,
        },
        "current": {
            "precipitation": 0.8 if raining_now else 0.0,
            "weather_code": 61 if raining_now else 1,
        },
    }


def _seed_hivebox_case(
    db: SimingDB,
    case_id: str,
    deposited_at: datetime,
    *,
    status: str = "待处理",
    code: str = "884210",
    location: str = "示例小区A大堂丰巢柜",
    site_alias: str = "家",
    brand: str = "丰巢",
    title: str | None = None,
) -> None:
    dep_iso = iso_now(deposited_at)
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO cases (
                case_id, title, next_step, status, strength, category, rule_id,
                context_json, created_at, updated_at, closed_at
            ) VALUES (?, ?, ?, ?, 'NORMAL', 'pickup', 'pickup_1800', ?, ?, ?, ?)
            """,
            (
                case_id,
                title or f"{brand} · {location}",
                f"凭取件码 {code} 取件，18小时内免费",
                status,
                json.dumps(
                    {
                        "brand": brand,
                        "code": code,
                        "location": location,
                        "site_alias": site_alias,
                        "free_window": "18小时内免费",
                        "deposited_at": dep_iso,
                    },
                    ensure_ascii=False,
                ),
                dep_iso,
                dep_iso,
                dep_iso if status != "待处理" else None,
            ),
        )


def _seed_return_case(db: SimingDB, case_id: str, delivered_at: datetime, title: str = "抖音订单 · 客制化键帽") -> None:
    deliv_iso = iso_now(delivered_at)
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO cases (
                case_id, title, next_step, status, strength, category, rule_id,
                context_json, created_at, updated_at, closed_at
            ) VALUES (?, ?, '已签收', '完成', 'SOFT', 'parcel', 'superagent_parcel', ?, ?, ?, ?)
            """,
            (
                case_id,
                title,
                json.dumps({"platform": "抖音", "delivered_at": deliv_iso, "shipping_insurance": True}, ensure_ascii=False),
                deliv_iso,
                deliv_iso,
                deliv_iso,
            ),
        )


# ==============================================================================
# Scenario 1: Repeated Tick Alert Storm / Dedup Chaos
# ==============================================================================


def test_scenario_1_repeated_tick_alert_storm_dedup_chaos(
    sandbox_db: SimingDB,
    frozen_clock,
) -> None:
    """30 分钟内连跑 4 次守卫与 run_tick：parcel_sentinel / meal_guard / daily_care / haos_night_owl 均零重复刷屏。"""
    # 1A) parcel_sentinel (HiveBox <=2h + 7天退货 <=48h) + meal_guard (午餐窗) 连跑 4 次（每 8 分钟一次）
    t0 = _dt("2026-11-20T13:00:00+08:00")  # 周五工作日午餐窗
    frozen_clock(t0)
    set_situation(sandbox_db, "sleep:state", {"state": "awake"}, source="desktop", as_of=iso_now(t0))
    set_situation(sandbox_db, "loc:me", {"place_alias": "家"}, source="ios_loc", as_of=iso_now(t0))

    _seed_hivebox_case(sandbox_db, "S101", t0 - timedelta(hours=16, minutes=30), code="112233")
    _seed_return_case(sandbox_db, "S102", t0 - timedelta(days=5, hours=12))

    hb_notified_total: list[str] = []
    rw_notified_total: list[str] = []
    meal_nudged_count = 0

    for i in range(4):
        ti = t0 + timedelta(minutes=i * 8)
        frozen_clock(ti)
        p_res = run_parcel_sentinel(sandbox_db, now=ti, notify=True)
        hb_notified_total.extend(p_res["hivebox"]["notified"])
        rw_notified_total.extend(p_res["return_window"]["notified"])

        m_res = evaluate_meal_regularity(sandbox_db, now=ti, notify=True)
        if m_res.get("nudged"):
            meal_nudged_count += 1

    assert hb_notified_total == ["S101"], f"丰巢 30m 内连跑 4 次应仅通知 1 次，实际: {hb_notified_total}"
    assert rw_notified_total == ["S102"], f"7天退货哨兵 30m 内连跑 4 次应仅通知 1 次，实际: {rw_notified_total}"
    assert meal_nudged_count == 1, f"午餐守卫 30m 内连跑 4 次应仅通知 1 次，实际: {meal_nudged_count}"
    assert len(_outbox_rows(sandbox_db, "hivebox_overdue_guard")) == 1
    assert len(_outbox_rows(sandbox_db, "return_window_sentinel")) == 1
    assert len(_outbox_rows(sandbox_db, "meal_combo_guard")) == 1

    # 1B) 晚餐窗同样连跑 4 次（19:10, 19:18, 19:26, 19:34）-> 仅 1 次晚餐提醒
    t_din = _dt("2026-11-20T19:10:00+08:00")
    din_nudges = 0
    for i in range(4):
        ti = t_din + timedelta(minutes=i * 8)
        frozen_clock(ti)
        r = evaluate_meal_regularity(sandbox_db, now=ti, notify=True)
        if r.get("nudged"):
            din_nudges += 1
    assert din_nudges == 1
    assert len(_outbox_rows(sandbox_db, "meal_combo_guard")) == 2  # 1 lunch + 1 dinner

    # 1C) daily_care: 天气早间卡 + 周期用药打针卡 + 耗材补货雷达各连跑 4 次 -> 均零重复
    t_w = _dt("2026-11-19T08:52:00+08:00")  # 周四工作日
    frozen_clock(t_w)
    fetch_rain = lambda lat, lon: _meteo(date(2026, 11, 19), rain_hours=(9,), raining_now=False)
    weather_sends = 0
    for i in range(4):
        ti = t_w + timedelta(minutes=i * 6)
        frozen_clock(ti)
        wr = run_weather_guard(sandbox_db, now=ti, fetch=fetch_rain)
        if wr.get("sent"):
            weather_sends += 1
    assert weather_sends == 1
    assert len(_outbox_rows(sandbox_db, "commute_weather")) == 1

    t_inj = _dt("2026-11-19T00:52:00+08:00")
    inj_created = 0
    for i in range(4):
        ti = t_inj + timedelta(minutes=i * 8)
        frozen_clock(ti)
        ir = run_injection_guard(sandbox_db, now=ti)
        if ir.get("created"):
            inj_created += 1
    assert inj_created == 1
    assert len(_outbox_rows(sandbox_db, "med_care")) == 1

    # 耗材低库存补货雷达 30m 内连跑 4 次 -> 仅推 1 次
    set_supply_stock(sandbox_db, "mask", 2, now=_dt("2026-11-19T22:00:00+08:00"))
    t_sup = _dt("2026-11-19T22:35:00+08:00")
    sup_pushed: list[str] = []
    for i in range(4):
        ti = t_sup + timedelta(minutes=i * 7)
        frozen_clock(ti)
        sr = run_supply_radar(sandbox_db, now=ti)
        sup_pushed.extend(sr.get("pushed") or [])
    assert sup_pushed == ["mask"]
    assert len(_outbox_rows(sandbox_db, "supply_radar")) == 1

    # 1D) haos_night_owl 30m 内同态连跑 4 次 -> HAOS 服务仅下发 1 次，后 3 次严格 deduped=True
    haos_calls: list[dict[str, Any]] = []
    t_ha = _dt("2026-11-20T02:00:00+08:00")
    for i in range(4):
        ti = t_ha + timedelta(minutes=i * 7)
        res = sync_night_owl_to_haos(
            sandbox_db,
            now=ti,
            sleep_state="awake",
            location_state="home",
            haos_call_fn=lambda act: haos_calls.append(act) or {"ok": True},
        )
        if i == 0:
            assert res["deduped"] is False and len(res["executed_actions"]) == 1
        else:
            assert res["deduped"] is True and res["executed_actions"] == []
    assert len(haos_calls) == 1, f"HAOS 同态 30m 内连跑 4 次不应重复调用，实际: {len(haos_calls)}"

    # 状态真正切换为 sleep -> 立即下发扫地机硬锁，不被旧状态去重吞掉
    res_sleep = sync_night_owl_to_haos(
        sandbox_db,
        now=t_ha + timedelta(minutes=25),
        sleep_state="sleep",
        location_state="home",
        haos_call_fn=lambda act: haos_calls.append(act) or {"ok": True},
    )
    assert res_sleep["deduped"] is False and res_sleep["vacuum_locked"] is True
    assert len(haos_calls) == 2

    # 1E) 全量 evald.run_tick 30m 内连续跑 4 次 -> 首次 tick 仅发正当到期项，第 2/3/4 次 tick 严格 0 新增（零重复刷屏！）
    t_tick = _dt("2026-11-20T13:05:00+08:00")
    after_tick1 = 0
    for i in range(4):
        frozen_clock(t_tick + timedelta(minutes=i * 8))
        tick_res = run_tick(sandbox_db, include_collectors=True)
        assert tick_res["ok"] is True
        if i == 0:
            after_tick1 = len(_outbox_rows(sandbox_db))
        else:
            assert len(_outbox_rows(sandbox_db)) == after_tick1, (
                f"第 {i + 1} 次 run_tick 不得产生任何重复通知，tick1={after_tick1}, 当前={len(_outbox_rows(sandbox_db))}"
            )
    assert len(_outbox_rows(sandbox_db, "hivebox_overdue_guard")) == 1
    assert len(_outbox_rows(sandbox_db, "return_window_sentinel")) == 1
    assert len(_outbox_rows(sandbox_db, "meal_combo_guard")) == 2
    assert len(_outbox_rows(sandbox_db, "commute_weather")) == 1
    assert len(_outbox_rows(sandbox_db, "supply_radar")) == 1


# ==============================================================================
# Scenario 2: Night-Owl 03:30 AM & 09:30 AM Sleep-In Protection
# ==============================================================================


def test_scenario_2_night_owl_0330_and_0930_sleep_in_protection(
    sandbox_db: SimingDB,
    frozen_clock,
) -> None:
    """凌晨 03:30 与上午 09:30 补觉态：非紧急提醒严禁吵醒，科沃斯扫地机硬锁，仅紧急穿透直达。"""
    t_0330 = _dt("2026-11-20T03:30:00+08:00")
    t_0930 = _dt("2026-11-20T09:30:00+08:00")  # 周五上午 09:30 仍在补觉（已过静态 07:30 宵禁！）
    t_1300 = _dt("2026-11-20T13:00:00+08:00")  # 周五午间仍在补觉

    # 通过 situation['sleep:state'] 设置夜猫子 03:00 入睡（此前 attention.py 完全忽略 sleep:state！）
    frozen_clock(t_0330)
    set_situation(
        sandbox_db,
        "sleep:state",
        {"state": "sleep"},
        source="ios_sleep_focus",
        as_of=iso_now(_dt("2026-11-20T03:00:00+08:00")),
    )
    set_situation(sandbox_db, "loc:me", {"place_alias": "家"}, source="ios_loc", as_of=iso_now(t_0330))

    # 2A) 03:30 AM 检查
    att_0330 = evaluate_attention_state(sandbox_db, now_override=t_0330)
    assert att_0330["state"] == "ASLEEP" and att_0330["should_hold_l2"] is True

    # normal push_external 在 03:30 必须进 MORNING_QUEUE，绝不直发吵醒
    ext_0330 = push_external(
        sandbox_db, "Steam 喜加一新游戏上线", rule_id="free_games", ref_id="fg:0330", level="normal", now=t_0330
    )
    assert ext_0330["delivered"] is False and ext_0330["queued_for_morning"] is True

    # 7 天退货哨兵在 03:30（level="normal"）必须暂存 MORNING_QUEUE，绝不直发吵醒
    _seed_return_case(sandbox_db, "S201", t_0330 - timedelta(days=5, hours=15), title="京东订单 · 蓝牙耳机")
    rw_0330 = evaluate_return_window_sentinel(sandbox_db, now=t_0330, notify=True)
    assert rw_0330["notified"] == ["S201"]
    rw_rows_0330 = _outbox_rows(sandbox_db, "return_window_sentinel")
    assert len(rw_rows_0330) == 1 and rw_rows_0330[0]["status"] == "MORNING_QUEUE"

    # meal_guard 在 03:30 静默跳过
    assert evaluate_meal_regularity(sandbox_db, now=t_0330, notify=True)["should_nudge"] is False

    # 2B) 09:30 AM 补觉期检查（关键：此时已过静态 07:30，最容易误发吵醒！）
    frozen_clock(t_0930)
    att_0930 = evaluate_attention_state(sandbox_db, now_override=t_0930)
    assert att_0930["state"] == "ASLEEP" and att_0930["should_hold_l2"] is True
    # 不传 now_override（模拟 router._in_quiet_hours）同样必须跟随 frozen_clock + sleep:state 判 ASLEEP！
    assert evaluate_attention_state(sandbox_db)["state"] == "ASLEEP"

    defer_brief, _ = should_defer_morning_brief_for_sleep(sandbox_db, now_override=t_0930)
    assert defer_brief is True

    # 09:30 normal push_external -> 依然暂存 MORNING_QUEUE，不吵醒补觉！
    ext_0930 = push_external(
        sandbox_db, "GitHub Star 趋势日报", rule_id="gh_trends", ref_id="gh:0930", level="normal", now=t_0930
    )
    assert ext_0930["delivered"] is False and ext_0930["queued_for_morning"] is True

    # 09:30 第二个 7 天退货包裹 -> 同样必须暂存 MORNING_QUEUE，零 SENT！
    _seed_return_case(sandbox_db, "S202", t_0930 - timedelta(days=5, hours=14), title="拼多多订单 · 桌面理线架")
    rw_0930 = evaluate_return_window_sentinel(sandbox_db, now=t_0930, notify=True)
    assert rw_0930["notified"] == ["S202"]
    rw_rows_all = _outbox_rows(sandbox_db, "return_window_sentinel")
    assert all(r["status"] == "MORNING_QUEUE" for r in rw_rows_all)

    # 09:30 haos_night_owl 必须硬锁科沃斯 T90S Pro 扫地机！
    vac_0930 = is_vacuum_locked(sandbox_db, now=t_0930)
    assert vac_0930["vacuum_locked"] is True and vac_0930["sleep_state"] == "ASLEEP"
    ha_0930 = sync_night_owl_to_haos(sandbox_db, now=t_0930, location_state="home")
    assert ha_0930["vacuum_locked"] is True and ha_0930["auto_clean_released"] is False
    assert any(a["rule"] == "sleep_vacuum_hard_lock" for a in ha_0930["actions"])

    # 2C) 13:00 PM 工作日午餐窗但仍处于 sleep:state == sleep -> meal_guard 绝不吵醒！
    frozen_clock(t_1300)
    mg_1300 = evaluate_meal_regularity(sandbox_db, now=t_1300, notify=True)
    assert mg_1300["should_nudge"] is False and mg_1300["reason"] == "user_asleep"
    assert _outbox_rows(sandbox_db, "meal_combo_guard") == []

    # 2D) 紧急穿透（level="now"）：在 09:30 补觉态下依然必须即时 SENT！
    frozen_clock(t_0930)
    urg_ext = push_external(
        sandbox_db, "🚨 服务器磁盘 99% 紧急告警", rule_id="disk_crit", ref_id="disk:0930", level="now", now=t_0930
    )
    assert urg_ext["delivered"] is True
    assert _outbox_rows(sandbox_db, "ext_disk_crit")[0]["status"] == "SENT"

    _seed_hivebox_case(sandbox_db, "S203", t_0930 - timedelta(hours=17, minutes=10), code="778899")
    hb_urg = evaluate_hivebox_countdown(sandbox_db, now=t_0930, is_home=True, notify=True)
    assert hb_urg["notified"] == ["S203"]
    assert _outbox_rows(sandbox_db, "hivebox_overdue_guard")[0]["status"] == "SENT"

    # 2E) 显式唤醒（record_explicit_sleep_state(sleeping=False)）比旧 sleep:state 更新时，立即恢复清醒
    t_1030 = _dt("2026-11-20T10:30:00+08:00")
    frozen_clock(t_1030)
    record_explicit_sleep_state(sandbox_db, sleeping=False, source="qq", at_ts=iso_now(t_1030))
    att_1030 = evaluate_attention_state(sandbox_db, now_override=t_1030)
    assert att_1030["state"] == "AWAKE_ACTIVE" and att_1030["should_hold_l2"] is False


# ==============================================================================
# Scenario 3: Cross-Midnight Logical Day & Year-Rollover + 2028-01-01 Regime Switch
# ==============================================================================


def test_scenario_3_cross_midnight_logical_day_and_year_rollover_and_regime_switch(
    sandbox_db: SimingDB,
    frozen_clock,
) -> None:
    """跨零点与跨年（2026-12-31 23:55 -> 2027-01-01 00:45 -> 05:10）及 2028-01-01 示例小区B切轨防重与零偏差。"""
    t_ny_pre = _dt("2026-12-31T23:55:00+08:00")
    t_ny_mid = _dt("2027-01-01T00:45:00+08:00")
    t_ny_post = _dt("2027-01-01T05:10:00+08:00")

    # 3A) 逻辑日（05:00 切日）在跨年零点不跳日，05:10 才切至 2027-01-01
    assert logical_today(t_ny_pre) == date(2026, 12, 31)
    assert logical_today(t_ny_mid) == date(2026, 12, 31)
    assert logical_today(t_ny_post) == date(2027, 1, 1)

    # 3B) 周期用药跨年周三针槽位连续一致（2026-12-30 是周三，提醒在 2026-12-31 00:50）
    cfg = load_care_cfg(sandbox_db)
    for t in (t_ny_pre, t_ny_mid, t_ny_post):
        slot, fire = dose_slot_for(cfg, t)
        assert slot == date(2026, 12, 30)
        assert fire == _dt("2026-12-31T00:50:00+08:00")

    # 跨年周三本身（2025-12-31 Wed -> 2026-01-01 00:50 Thu）同样零 off-by-one
    slot_pre, _ = dose_slot_for(cfg, _dt("2026-01-01T00:45:00+08:00"))
    slot_fire, fire_dt = dose_slot_for(cfg, _dt("2026-01-01T00:55:00+08:00"))
    assert slot_pre == date(2025, 12, 24)
    assert slot_fire == date(2025, 12, 31) and fire_dt == _dt("2026-01-01T00:50:00+08:00")

    # 3C) 特斯拉低电量提醒跨年零点（23:55 -> 00:45）归属同一逻辑日 2026-12-31，绝不连发两条！
    frozen_clock(t_ny_pre)
    set_situation(sandbox_db, "sleep:state", {"state": "awake"}, source="desktop", as_of=iso_now(t_ny_pre))
    r_soc1 = ingest_tesla_state(sandbox_db, {"soc": 18, "plugged_in": False}, received_at=iso_now(t_ny_pre))
    assert r_soc1["low_alerted"] is True
    frozen_clock(t_ny_mid)
    r_soc2 = ingest_tesla_state(sandbox_db, {"soc": 17, "plugged_in": False}, received_at=iso_now(t_ny_mid))
    assert r_soc2["low_alerted"] is False, "跨零点 50 分钟内同一逻辑日绝不应重复推低电量！"
    assert len(_outbox_rows(sandbox_db, "tesla_low_soc")) == 1
    # 到了 05:10 新逻辑日 2027-01-01 才允许再次提醒
    frozen_clock(t_ny_post)
    r_soc3 = ingest_tesla_state(sandbox_db, {"soc": 16, "plugged_in": False}, received_at=iso_now(t_ny_post))
    assert r_soc3["low_alerted"] is True
    assert len(_outbox_rows(sandbox_db, "tesla_low_soc")) == 2

    # 3D) 2028-01-01 示例小区B搬家切轨：commute_hub + daily_care 零 off-by-one、零提前切日、零重复触发
    assert active_regime(cfg, date(2027, 12, 31))["id"] == "primary_home"
    assert active_regime(cfg, date(2028, 1, 1))["id"] == "secondary_home"

    t_pre_move = _dt("2027-12-31T23:55:00+08:00")
    t_mid_move = _dt("2028-01-01T00:45:00+08:00")  # 凌晨 00:45，逻辑日仍是 2027-12-31！
    t_post_move = _dt("2028-01-01T05:10:00+08:00")  # 05:10 正式进入逻辑日 2028-01-01！
    t_later_same_day = _dt("2028-01-01T08:30:00+08:00")
    t_next_day = _dt("2028-01-02T08:30:00+08:00")

    r_pre = check_regime_transition(sandbox_db, today=t_pre_move)
    assert r_pre["regime"] == "primary_home" and r_pre["transitioned"] is False and r_pre["days_until_switch"] == 1
    assert "示例小区A" in format_care_report(sandbox_db, now=t_pre_move)

    # 关键：2028-01-01 00:45 逻辑日仍是 12-31，commute_hub 与 daily_care 必须一致保持 primary_home！
    r_mid = check_regime_transition(sandbox_db, today=t_mid_move)
    assert r_mid["today"] == "2027-12-31"
    assert r_mid["regime"] == "primary_home" and r_mid["transitioned"] is False
    assert "示例小区A" in format_care_report(sandbox_db, now=t_mid_move)

    # 05:10 首次跨入 2028-01-01 -> 仅此一次触发切换并进入 relearning
    r_switch1 = check_regime_transition(sandbox_db, today=t_post_move)
    assert r_switch1["today"] == "2028-01-01"
    assert r_switch1["regime"] == "secondary_home"
    assert r_switch1["transitioned"] is True and r_switch1["relearning"] is True
    assert "示例小区B" in format_care_report(sandbox_db, now=t_post_move)

    # 后续同日 08:30 与次日 05-01 再次调用 -> 必须严格幂等（transitioned=False，不覆盖 triggered_on，不重复写审计）
    r_switch2 = check_regime_transition(sandbox_db, today=t_later_same_day)
    r_switch3 = check_regime_transition(sandbox_db, today=t_next_day)
    assert r_switch2["transitioned"] is False and r_switch2.get("already_transitioned") is True
    assert r_switch3["transitioned"] is False and r_switch3.get("already_transitioned") is True

    relearn_meta = json.loads(sandbox_db.get_meta("routines_relearning") or "{}")
    assert relearn_meta.get("triggered_on") == "2028-01-01"

    routines_txt = Path(r_switch1["routines_md_path"]).read_text(encoding="utf-8")
    assert routines_txt.count("<!-- commute_regime:secondary_home:relearning -->") == 1

    conn = sandbox_db.connect()
    try:
        audit_cnt = conn.execute(
            "SELECT COUNT(*) FROM audit WHERE action = 'commute_regime_transition'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert audit_cnt == 1, f"切轨审计日志只应记录 1 次，实际: {audit_cnt}"


# ==============================================================================
# Scenario 4: Module-Off Direct Enforcement Audit
# ==============================================================================


def test_scenario_4_module_off_direct_enforcement_audit(
    sandbox_db: SimingDB,
    frozen_clock,
) -> None:
    """关闭各业务模块后直接调用其守卫函数（不经 evald），必须 100% 拦截且零通知/零副作用。"""
    t_work = _dt("2026-11-20T13:30:00+08:00")
    frozen_clock(t_work)
    set_situation(sandbox_db, "sleep:state", {"state": "awake"}, source="desktop", as_of=iso_now(t_work))
    set_situation(sandbox_db, "loc:me", {"place_alias": "家"}, source="ios_loc", as_of=iso_now(t_work))

    # 种好所有会触发告警的前置数据
    _seed_hivebox_case(sandbox_db, "S401", t_work - timedelta(hours=17, minutes=0), code="401401")
    _seed_return_case(sandbox_db, "S402", t_work - timedelta(days=5, hours=15))
    set_supply_stock(sandbox_db, "mask", 1, now=_dt("2026-11-20T12:00:00+08:00"))
    set_situation(
        sandbox_db,
        "outing:active",
        {"dest": "新房", "depart_at": iso_now(t_work), "driving": True},
        source="manual_qq",
        as_of=iso_now(t_work),
    )
    set_situation(
        sandbox_db,
        "tesla:main",
        {"soc_pct": 80.0, "range_km": 348.0, "inside_temp_c": 4.0, "is_climate_on": False, "shift_state": "P"},
        source="tesla_cn_fleet_api",
        as_of=iso_now(t_work),
    )

    # 全部关闭目标模块（刻意保持 spatial_pickup 与 price_watch 为开启，验证 parcel_sentinel 独立受控！）
    disabled_mods = (
        "meal_combo_guard",
        "parcel_sentinel",
        "haos_night_owl",
        "commute_hub",
        "med_care",
        "commute_weather",
        "supply_radar",
        "tesla_auto_climate",
        "outing_amap_ev",
    )
    for mod in disabled_mods:
        r_set = set_module_enabled(sandbox_db, mod, False)
        assert r_set["ok"] is True and r_set["enabled"] is False

    # 1) meal_combo_guard OFF -> direct evaluate_meal_regularity
    r_meal = evaluate_meal_regularity(sandbox_db, now=t_work, notify=True)
    assert r_meal["skipped"] == "module_disabled" and r_meal["nudged"] is False

    # 2) parcel_sentinel OFF -> direct evaluate_hivebox_countdown / evaluate_return_window_sentinel / run_parcel_sentinel
    r_hb = evaluate_hivebox_countdown(sandbox_db, now=t_work, is_home=True, notify=True)
    assert r_hb.get("skipped") == "module_disabled" and r_hb["notified"] == []
    r_rw = evaluate_return_window_sentinel(sandbox_db, now=t_work, notify=True)
    assert r_rw.get("skipped") == "module_disabled" and r_rw["notified"] == []
    r_ps = run_parcel_sentinel(sandbox_db, now=t_work, notify=True)
    assert r_ps.get("skipped") == "module_disabled" and r_ps["hivebox"]["notified"] == [] and r_ps["return_window"]["notified"] == []

    # 3) haos_night_owl OFF -> direct evaluate_night_owl_haos_rules / sync_night_owl_to_haos
    haos_calls: list[dict[str, Any]] = []
    r_ha = sync_night_owl_to_haos(
        sandbox_db,
        now=_dt("2026-11-20T02:00:00+08:00"),
        sleep_state="awake",
        location_state="home",
        haos_call_fn=lambda act: haos_calls.append(act),
    )
    assert r_ha["enabled"] is False and r_ha.get("skipped") == "module_disabled"
    assert r_ha["executed_actions"] == [] and haos_calls == []
    assert get_situation(sandbox_db, "haos:night_owl") is None

    # 4) commute_hub OFF -> direct stage_commute_rescue_bundle / check_regime_transition
    r_rescue = stage_commute_rescue_bundle(sandbox_db, reason="暴雨+起晚")
    assert r_rescue["ok"] is False and r_rescue["skipped"] == "module_disabled" and r_rescue["token"] == ""
    r_reg = check_regime_transition(sandbox_db, today=date(2027, 4, 30))
    assert r_reg["skipped"] == "module_disabled" and r_reg["transitioned"] is False
    assert (sandbox_db.get_meta("commute_regime") or "primary_home") == "primary_home"

    # 5) med_care OFF -> direct run_injection_guard（绝不建单、绝不篡改 hard_rule、绝不发 QQ）
    r_inj = run_injection_guard(sandbox_db, now=_dt("2026-11-19T00:55:00+08:00"))
    assert r_inj.get("skipped") == "module_disabled" and "created" not in r_inj

    # 6) commute_weather OFF -> direct run_weather_guard（绝不调用 fetch、绝不发 QQ）
    fetch_called = []
    r_w = run_weather_guard(
        sandbox_db,
        now=_dt("2026-11-19T08:52:00+08:00"),
        fetch=lambda lat, lon: fetch_called.append((lat, lon)) or _meteo(date(2026, 11, 19)),
    )
    assert r_w.get("skipped") == "module_disabled" and fetch_called == []

    # 7) supply_radar OFF -> direct run_supply_radar
    r_sup = run_supply_radar(sandbox_db, now=_dt("2026-11-20T22:40:00+08:00"))
    assert r_sup.get("skipped") == "module_disabled" and r_sup["pushed"] == []

    # 8) tesla_auto_climate OFF -> direct evaluate_departure_auto_climate
    climate_calls = []
    r_cli = evaluate_departure_auto_climate(
        sandbox_db,
        departed_now=True,
        now=t_work,
        climate_runner=lambda **kw: climate_calls.append(kw) or {"ok": True},
    )
    assert r_cli.get("skipped") == "module_disabled" and climate_calls == []

    # 9) outing_amap_ev OFF -> direct ingest_tesla_state 低电量不发通知
    r_soc = ingest_tesla_state(sandbox_db, {"soc": 15, "plugged_in": False}, received_at=iso_now(t_work))
    assert r_soc["low_alerted"] is False

    # 核验全局 outbox 与 approvals 零泄漏！
    assert _outbox_rows(sandbox_db) == [], f"所有模块关闭后 outbox 必须为空，实际: {_outbox_rows(sandbox_db)}"
    conn = sandbox_db.connect()
    try:
        app_cnt = conn.execute("SELECT COUNT(*) FROM approvals").fetchone()[0]
    finally:
        conn.close()
    assert app_cnt == 0


# ==============================================================================
# Scenario 5: HiveBox False-Urgent Edge Cases
# ==============================================================================


def test_scenario_5_hivebox_false_urgent_edge_cases(
    sandbox_db: SimingDB,
    frozen_clock,
) -> None:
    """丰巢防误报边界：在公司不破静、新房非顺路不误催、已关单+旧事件不复活、已取件短信与菜鸟驿站绝不误判为 18h 丰巢。"""
    now = _dt("2026-11-20T18:30:00+08:00")
    frozen_clock(now)

    # 5A) 用户在「公司」（距丰巢免费截止还剩 90 分钟 > 1h）-> 严禁触发在家破静告警！
    dep_90m_left = now - timedelta(hours=16, minutes=30)
    _seed_hivebox_case(sandbox_db, "S501", dep_90m_left, code="501501", site_alias="家")

    # 方式 1：situation['loc:me'] = {"place_alias": "公司"}
    set_situation(sandbox_db, "loc:me", {"place_alias": "公司"}, source="ios_loc", as_of=iso_now(now))
    r_office1 = evaluate_hivebox_countdown(sandbox_db, now=now, notify=True)
    assert r_office1["is_home"] is False and r_office1["urgent"] == [] and r_office1["notified"] == []

    # 方式 2：situation['loc:me'] = {"state": "office"}（之前 _infer_is_home 漏看 state 字段导致误判在家！）
    set_situation(sandbox_db, "loc:me", {"state": "office"}, source="owntracks", as_of=iso_now(now))
    r_office2 = evaluate_hivebox_countdown(sandbox_db, now=now, notify=True)
    assert r_office2["is_home"] is False and r_office2["urgent"] == [] and r_office2["notified"] == []

    # 方式 3：清空 loc:me，仅写 latest_ios_location（含 ts 字段而非 updated_at）
    with sandbox_db.transaction() as conn:
        conn.execute("DELETE FROM situation WHERE key = 'loc:me'")
    sandbox_db.set_meta(
        "latest_ios_location",
        json.dumps({"place_alias": "公司", "ts": iso_now(now)}, ensure_ascii=False),
    )
    r_office3 = evaluate_hivebox_countdown(sandbox_db, now=now, notify=True)
    assert r_office3["is_home"] is False and r_office3["urgent"] == [] and r_office3["notified"] == []

    # 5B) 回到「家」，但包裹在「新房（示例小区B）」丰巢柜 -> 不应按在家下楼顺手取破静告警
    set_situation(sandbox_db, "loc:me", {"place_alias": "家"}, source="ios_loc", as_of=iso_now(now))
    with sandbox_db.transaction() as conn:
        conn.execute("UPDATE cases SET status='完成' WHERE case_id='S501'")
    _seed_hivebox_case(
        sandbox_db,
        "S502",
        dep_90m_left,
        code="502502",
        location="示例小区B南门丰巢柜",
        site_alias="新房",
    )
    r_newhome = evaluate_hivebox_countdown(sandbox_db, now=now, notify=True)
    assert r_newhome["is_home"] is True
    assert len(r_newhome["tracked"]) == 1
    assert r_newhome["urgent"] == [] and r_newhome["notified"] == []

    # 5C) 已关闭的丰巢单据（status='完成' / '取消'）即使原入柜短信仍在 events 表中（36h内），绝不得复活报警！
    _seed_hivebox_case(sandbox_db, "S503", dep_90m_left, status="完成", code="503503", site_alias="家")
    _seed_hivebox_case(sandbox_db, "S504", dep_90m_left, status="取消", code="504504", site_alias="家")
    with sandbox_db.transaction() as conn:
        sandbox_db.append_event(
            conn,
            source="ios_sms",
            thread_key="sms-hb-503",
            summary="【丰巢】凭取件码503503至示例小区A大堂丰巢柜取件，18小时内免费",
            payload={"brand": "丰巢", "code": "503503", "case_id": "S503", "location": "示例小区A丰巢柜"},
            occurred_at=iso_now(dep_90m_left),
            fingerprint="fp-hb-503",
        )
        sandbox_db.append_event(
            conn,
            source="ios_sms",
            thread_key="sms-hb-504",
            summary="【丰巢】凭取件码504504至示例小区A大堂丰巢柜取件，18小时内免费",
            payload={"brand": "丰巢", "code": "504504", "location": "示例小区A丰巢柜"},
            occurred_at=iso_now(dep_90m_left),
            fingerprint="fp-hb-504",
        )
        # 5D) 只有事件、无 case，但随后收到「已取件/取件成功」短信 -> 同样不得报警！
        sandbox_db.append_event(
            conn,
            source="ios_sms",
            thread_key="sms-hb-505-in",
            summary="【丰巢】凭取件码505505至示例小区A丰巢柜取件，18小时内免费",
            payload={"brand": "丰巢", "code": "505505", "location": "示例小区A丰巢柜"},
            occurred_at=iso_now(dep_90m_left),
            fingerprint="fp-hb-505-in",
        )
        sandbox_db.append_event(
            conn,
            source="ios_sms",
            thread_key="sms-hb-505-out",
            summary="【丰巢】您已成功取出包裹（取件码505505），欢迎再次使用",
            payload={"brand": "丰巢", "code": "505505"},
            occurred_at=iso_now(now - timedelta(hours=2)),
            fingerprint="fp-hb-505-out",
        )

    # 5E) 非丰巢菜鸟驿站包裹（普通菜鸟 + 地址旁提丰巢的菜鸟 + 明确标「非丰巢」）绝不得误归类为 18h 丰巢！
    _seed_hivebox_case(
        sandbox_db,
        "S506",
        dep_90m_left,
        code="12-3-4567",
        brand="菜鸟驿站",
        location="示例小区A南门菜鸟驿站",
        title="菜鸟驿站 · 示例小区A南门菜鸟驿站",
    )
    _seed_hivebox_case(
        sandbox_db,
        "S507",
        dep_90m_left,
        code="15-2-8899",
        brand="菜鸟驿站",
        location="示例小区A丰巢柜旁菜鸟驿站",
        title="菜鸟驿站 · 示例小区A丰巢柜旁菜鸟驿站",
    )
    _seed_hivebox_case(
        sandbox_db,
        "S508",
        dep_90m_left,
        code="88-1-2233",
        brand="",
        location="示例小区A东门驿站（非丰巢，免费放3天）",
        title="普通驿站待取（非丰巢）",
    )

    r_clean = evaluate_hivebox_countdown(sandbox_db, now=now, is_home=True, notify=True)
    tracked_ids = {t["ref_id"] for t in r_clean["tracked"]}
    # 只有 S502（未取的新房丰巢）会被追踪，其余全部被正确排除，且 0 条紧急通知！
    assert tracked_ids == {"S502"}, f"已关单/已取件/菜鸟驿站绝不应混入丰巢倒计时，实际 tracked={tracked_ids}"
    assert r_clean["urgent"] == [] and r_clean["notified"] == []
    assert _outbox_rows(sandbox_db, "hivebox_overdue_guard") == []
