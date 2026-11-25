"""日常照护链：出门天气卡、周期用药打针/开药、美团订单 → 耗材余量雷达。"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta

from siming.collectors.meituan_orders import collect_meituan_orders, parse_olist_text, store_orders
from siming.daily_care import (
    analyze_commute_weather,
    active_regime,
    dose_slot_for,
    estimate_supplies,
    format_care_report,
    handle_care_inbound,
    load_care_cfg,
    run_injection_guard,
    run_supply_radar,
    run_weather_guard,
    _units_per_item,
)
from siming.db import SHANGHAI_TZ
from siming.router import handle_inbound_qq

OLIST_SAMPLE = """南京大牌档(中关村店)

蟹粉火鸭烩粉皮
x1

2026-11-18 19:46
实付¥92.3

已完成
删除
再来一单

京亿康医疗器械（德胜门店）

酒精棉片[海氏海诺]医用酒精消毒棉片12*18cm*50片/盒
x2

2026-11-11 23:11
实付¥32.7

已完成
删除
再来一单

示例生活超市（学院店）

【整盒】海氏海诺一次性医用外科口罩 50只/盒熔喷布无菌三层防护口罩蓝色（新老包装随机）
x1

2026-11-08 12:00
实付¥20.0

已取消
删除

示例生活超市（学院店）

实用方便日常小剪刀约13.5cm 1把/份
x1

【整盒】海氏海诺一次性医用外科口罩 50只/盒熔喷布无菌三层防护口罩蓝色（新老包装随机）
x1

2026-10-05 00:46
实付¥38.19

已完成
删除
再来一单

养生堂药店（北土城店）

[新优锐]一次性使用注射笔用针头0.23*4mm(32G*4mm)*7支/盒
x2

2026-09-30 13:28
实付¥38.7

已完成
删除
再来一单

叮当快药(北京知春路店)

[袋鼠医生]医用酒精棉签(双头)8cm*20支/盒
x1

2026-07-27 23:08
实付¥18.1

已完成

京亿康医疗器械（德胜门店）

酒精棉片[海氏海诺]医用酒精消毒棉片12cm*18cm*50片/盒
x1

2026-07-27 22:08
实付¥18.1

已完成
"""


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI_TZ)


def _meteo(day: date, today_feel: float = 2.0, y_feel: float = 10.0, rain_hours: tuple[int, ...] = (), raining_now: bool = False) -> dict:
    times, temp, feel, prob, mm, code, wind = [], [], [], [], [], [], []
    for off in (-1, 0, 1):
        d = day + timedelta(days=off)
        for h in range(24):
            times.append(f"{d.isoformat()}T{h:02d}:00")
            base = y_feel if off == -1 else today_feel
            night = -3.0 if h >= 21 else 0.0
            temp.append(base + 2 + night)
            feel.append(base + night)
            wet = off == 0 and h in rain_hours
            prob.append(80 if wet else 5)
            mm.append(1.2 if wet else 0.0)
            code.append(61 if wet else 1)
            wind.append(10.0)
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
        "current": {"precipitation": 0.6 if raining_now else 0.0, "weather_code": 61 if raining_now else 1},
    }


def _outbox(db, like: str) -> list:
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT rule_id, text, status FROM outbox WHERE text LIKE ?", (f"%{like}%",)).fetchall()]
    finally:
        conn.close()


def test_parse_olist_handles_status_and_cancelled_orders():
    orders = parse_olist_text(OLIST_SAMPLE)
    assert [o["ordered_at"] for o in orders][:3] == ["2026-11-18 19:46", "2026-11-11 23:11", "2026-11-08 12:00"]
    pads = orders[1]
    assert pads["shop"] == "京亿康医疗器械（德胜门店）" and pads["items"][0][1] == 2 and pads["status"] == "已完成"
    assert orders[2]["cancelled"] is True and orders[2]["shop"] == "示例生活超市（学院店）"
    multi = orders[3]
    assert multi["shop"] == "示例生活超市（学院店）" and len(multi["items"]) == 2
    assert _units_per_item("一次性使用注射笔用针头0.23*4mm(32G*4mm)*7支/盒", "支") == 7
    assert _units_per_item("消毒棉片12*18cm*50片/盒", "片") == 50
    assert _units_per_item("医用口罩 10只*5包", "只") == 50


def test_supply_estimates_from_meituan_orders_and_calibration(sandbox_db):
    stats = store_orders(sandbox_db, parse_olist_text(OLIST_SAMPLE))
    assert stats["inserted"] >= 7
    assert store_orders(sandbox_db, parse_olist_text(OLIST_SAMPLE))["inserted"] == 0  # 幂等

    now = _dt("2026-11-19 23:00")
    items = {s["key"]: s for s in estimate_supplies(sandbox_db, now)}
    mask = items["mask"]
    # 10-05 买 50 只（11-08 那单已取消不计），每天 1 只 → 约剩 4 只
    assert 3 <= mask["stock"] <= 5 and mask["status"] == "low"
    needle = items["needle"]
    assert needle["rate_src"] == "每周一针" and 5 <= needle["stock"] <= 8 and needle["status"] == "ok"
    pads = items["alcohol_pad"]  # 棉签不计入棉片
    assert pads["last"]["qty"] == 2 and pads["status"] == "ok"

    r = handle_inbound_qq(sandbox_db, "口罩还剩 30")
    assert r["handled"] and "30只" in r["reply"]
    mask2 = next(s for s in estimate_supplies(sandbox_db, now) if s["key"] == "mask")
    assert mask2["anchored"] and mask2["status"] == "ok"


def test_supply_radar_pushes_once_per_batch(sandbox_db):
    store_orders(sandbox_db, parse_olist_text(OLIST_SAMPLE))
    now = _dt("2026-11-19 22:40")
    first = run_supply_radar(sandbox_db, now)
    assert first["pushed"] == ["mask"]
    assert run_supply_radar(sandbox_db, now + timedelta(hours=1))["pushed"] == []
    msgs = _outbox(sandbox_db, "口罩快用完了")
    assert len(msgs) == 1 and "再来一单" in msgs[0]["text"]
    assert run_supply_radar(sandbox_db, _dt("2026-11-19 10:00"))["skipped"] == "not_due"


def test_collect_meituan_orders_with_injected_fetch(sandbox_db):
    res = collect_meituan_orders(sandbox_db, fetch_text=lambda since: {"ok": True, "text": OLIST_SAMPLE})
    assert res["ok"] and res["orders"] == 7
    gated = collect_meituan_orders(sandbox_db, fetch_text=lambda since: {"ok": False, "reason": "gated", "gate": {"reason": "滑块"}})
    assert gated["ok"] is False and gated["reason"] == "gated"


def test_weather_analysis_rain_on_way_home_and_cold_snap(sandbox_db):
    cfg = load_care_cfg(sandbox_db)
    day = date(2026, 11, 19)
    regime = active_regime(cfg, day)
    assert regime["home_label"] == "示例小区A"
    a = analyze_commute_weather(_meteo(day, today_feel=1.0, y_feel=9.0, rain_hours=(22,)), day, regime, cfg)
    assert a["rain_back"]["hit"] and not a["rain_out"]["hit"]
    assert a["swing"] and a["d_out"] == -8.0 and a["actionable"]
    assert "示例小区B" in active_regime(cfg, date(2028, 1, 6))["home_label"]


def test_weather_guard_morning_card_only_when_actionable(sandbox_db):
    day = date(2026, 11, 19)  # 周四工作日
    fetch_bad = lambda lat, lon: _meteo(day, today_feel=1.0, y_feel=9.0, rain_hours=(9,))
    r = run_weather_guard(sandbox_db, now=_dt("2026-11-19 08:52"), fetch=fetch_bad)
    assert r["phase"] == "morning" and r["sent"]
    msg = _outbox(sandbox_db, "出门前看一眼")[0]["text"]
    assert "带伞" in msg and "气温骤变" in msg and "穿衣" in msg
    # 同一天不重复
    assert run_weather_guard(sandbox_db, now=_dt("2026-11-19 08:55"), fetch=fetch_bad)["skipped"] == "not_due"

    day2 = date(2026, 11, 20)
    calm = lambda lat, lon: _meteo(day2, today_feel=8.0, y_feel=9.0)
    r2 = run_weather_guard(sandbox_db, now=_dt("2026-11-20 08:52"), fetch=calm)
    assert r2["phase"] == "morning" and not r2["sent"]
    # 晚上新出现降雨 → 下班前补一句
    wet_night = lambda lat, lon: _meteo(day2, today_feel=8.0, y_feel=9.0, rain_hours=(22, 23))
    r3 = run_weather_guard(sandbox_db, now=_dt("2026-11-20 21:40"), fetch=wet_night)
    assert r3["phase"] == "evening" and r3["sent"]
    # 周六不提醒
    assert run_weather_guard(sandbox_db, now=_dt("2026-11-21 08:52"), fetch=calm)["skipped"] == "rest_day"


def test_injection_cycle_reminder_done_followup_and_refill(sandbox_db, frozen_clock):
    frozen_clock("2026-11-19T23:30:00+08:00")
    cfg = load_care_cfg(sandbox_db)
    slot, fire = dose_slot_for(cfg, _dt("2026-11-19 01:00"))
    assert slot == date(2026, 11, 18) and fire == _dt("2026-11-19 00:50")
    slot_b, _ = dose_slot_for(cfg, _dt("2026-11-18 23:00"))
    assert slot_b == date(2026, 11, 11)

    assert handle_inbound_qq(sandbox_db, "周期用药还剩 3 针")["handled"]
    res = run_injection_guard(sandbox_db, now=_dt("2026-11-19 00:52"))
    cid = res["created"]
    assert "今晚该打周期用药" in _outbox(sandbox_db, "今晚该打")[0]["text"]
    assert "created" not in run_injection_guard(sandbox_db, now=_dt("2026-11-19 00:56"))

    # 次日 23:30 还没登记 → 追问一次
    fu = run_injection_guard(sandbox_db, now=_dt("2026-11-19 23:25"))
    assert fu.get("followed_up") == cid
    assert len(_outbox(sandbox_db, "还没登记")) == 1

    r = handle_inbound_qq(sandbox_db, "打了")
    assert r["handled"] and "还剩 2 针" in r["reply"] and "11-25" in r["reply"]
    conn = sandbox_db.connect()
    try:
        assert conn.execute("SELECT status FROM cases WHERE case_id=?", (cid,)).fetchone()["status"] == "完成"
    finally:
        conn.close()
    # 刚打完第 2 针（剩 2 针，处于第2→3针那周 11-19..11-24）：绝不能提前推挂号提醒！
    run_injection_guard(sandbox_db, now=_dt("2026-11-19 04:10"))
    run_injection_guard(sandbox_db, now=_dt("2026-11-20 10:00"))
    assert len(_outbox(sandbox_db, "预约下周开周期用药")) == 0

    # 到了第 3→4 针那周的周三（11-25，真实去医院 12-03～12-09 的一周前）：
    # 凌晨 04:10 不打扰，09:30 后推周三预约提醒（当天只推一次）
    run_injection_guard(sandbox_db, now=_dt("2026-11-25 04:10"))
    assert len(_outbox(sandbox_db, "预约下周开周期用药")) == 0
    run_injection_guard(sandbox_db, now=_dt("2026-11-25 10:00"))
    run_injection_guard(sandbox_db, now=_dt("2026-11-25 10:05"))
    assert len(_outbox(sandbox_db, "预约下周开周期用药")) == 1

    # 周三晚打完第 3 针（剩 1 针），周四（11-26）若还未约号，再提醒一次！
    res_w3 = run_injection_guard(sandbox_db, now=_dt("2026-11-26 00:55"))
    assert res_w3.get("created")
    assert handle_inbound_qq(sandbox_db, "打了")["handled"]
    run_injection_guard(sandbox_db, now=_dt("2026-11-26 10:00"))
    assert len(_outbox(sandbox_db, "周四再提醒一次")) == 1
    # 周五不再重复催
    run_injection_guard(sandbox_db, now=_dt("2026-11-27 10:00"))
    assert len(_outbox(sandbox_db, "周四再提醒一次")) == 1

    rf = handle_inbound_qq(sandbox_db, "开药了 1支")
    assert rf["handled"] and "共 5 针" in rf["reply"]
    assert "周期用药" in format_care_report(sandbox_db)


def test_short_done_word_not_hijacked_outside_window(sandbox_db):
    # 周一下午没有打针卡、也不在注射窗口：「打了」交还给常规对话
    from siming import daily_care as dc

    assert dc._in_injection_window(sandbox_db, _dt("2026-11-23 15:00")) is False
    assert dc._in_injection_window(sandbox_db, _dt("2026-11-19 02:00")) is True


def test_quoted_yiban_and_bysy_sms_sync(sandbox_db):
    from siming.collectors.edge_sync import process_edge_records
    from siming.doctor import evaluate_30d_feature_freeze

    handle_inbound_qq(sandbox_db, "周期用药还剩 3 针")
    res = run_injection_guard(sandbox_db, now=_dt("2026-11-19 00:52"))
    cid = res["created"]
    card_text = _outbox(sandbox_db, "今晚该打")[0]["text"]

    # 引用打针卡回「已办」（而非「打了」）同样必须扣针数并记入日志，绝不能在下一节拍重复建单
    r = handle_inbound_qq(sandbox_db, "已办", reply_to_text=card_text)
    assert r["handled"] and "还剩 2 针" in r["reply"]
    assert "created" not in run_injection_guard(sandbox_db, now=_dt("2026-11-19 01:05"))

    # 第 2→3 针那周（11-20）不建挂号卡；到第 3→4 针那周周三（11-25）触发预约挂号卡，
    # 收到【常用医院】内分泌门诊预约短信后自动更新待办步骤，且周四（11-26）不再重复催约号
    run_injection_guard(sandbox_db, now=_dt("2026-11-20 10:00"))
    assert len(_outbox(sandbox_db, "预约下周开周期用药")) == 0
    run_injection_guard(sandbox_db, now=_dt("2026-11-25 10:00"))
    assert len(_outbox(sandbox_db, "预约下周开周期用药")) == 1
    sms = "【常用医院】挂号提醒：张三您已成功预约2026-12-03 下午内分泌门诊门诊号，请提前到院缴费取号。"
    process_edge_records(sandbox_db, [{"source": "ios_sms", "text": sms}])
    run_injection_guard(sandbox_db, now=_dt("2026-11-26 10:00"))
    assert len(_outbox(sandbox_db, "周四再提醒一次")) == 0
    conn = sandbox_db.connect()
    try:
        rf_case = conn.execute("SELECT case_id, next_step FROM cases WHERE category='med_refill' AND status='待处理'").fetchone()
        assert rf_case is not None and "2026-12-03" in rf_case["next_step"] and "常用医院" in rf_case["next_step"]
    finally:
        conn.close()

    # 引用开药卡回「已办」自动 +4 针并关单
    r2 = handle_inbound_qq(sandbox_db, "已办", reply_to_text=f"[{rf_case['case_id']}] 🏥 该在 APP 预约下周开周期用药的门诊号了")
    assert r2["handled"] and "共 6 针" in r2["reply"]

    # 30 天断舍离巡检不冻结日常照护三项与低频天气/订单源
    frz = evaluate_30d_feature_freeze(sandbox_db, inactive_days=0, apply_freeze=False)
    for k in ("commute_weather", "med_care", "supply_radar", "open_meteo", "meituan_orders"):
        assert k not in frz["candidates"]


def test_reply_yiyue_suppresses_thursday_without_premature_doses(sandbox_db):
    handle_inbound_qq(sandbox_db, "周期用药还剩 3 针")
    run_injection_guard(sandbox_db, now=_dt("2026-11-19 00:52"))
    handle_inbound_qq(sandbox_db, "打了")
    # 11-25 周三触发约号提醒，用户回「约好了」：不提前加针数，且 11-26 周四不再重复催约号
    run_injection_guard(sandbox_db, now=_dt("2026-11-25 10:00"))
    msg_wed = _outbox(sandbox_db, "预约下周开周期用药")[0]["text"]
    r_book = handle_inbound_qq(sandbox_db, "约好了", reply_to_text=msg_wed)
    assert r_book["handled"] and "不再重复催约号" in r_book["reply"]
    run_injection_guard(sandbox_db, now=_dt("2026-11-26 10:00"))
    assert len(_outbox(sandbox_db, "周四再提醒一次")) == 0
    assert "剩 2 针" in format_care_report(sandbox_db, now=_dt("2026-11-26 10:05"))
