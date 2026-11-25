"""测试中国日历（调休补班、北京限行、12306、农历生日）与周边上下文/硬规矩冲突检查。"""

from __future__ import annotations

from datetime import date

from siming.cn_calendar import (
    day_info,
    hospital_114_alert_window,
    next_lunar_birthday,
    rail_12306_alert_window,
    subtract_workdays,
    upcoming_non_makeup_rest_days,
)
from siming.db import SimingDB
from siming.enrich import detect_hard_rule_conflicts, enrich_appliance_arrival


def test_makeup_weekend_and_beijing_tail_limit(sandbox_db: SimingDB) -> None:
    # 2026-10-10 周六为国庆补班：必须是工作日 + 补班标记 + 北京不限行！
    info_makeup = day_info(date(2026, 10, 10), sandbox_db.paths)
    assert info_makeup.is_workday is True
    assert info_makeup.is_makeup_workday is True
    assert info_makeup.is_rest_day is False
    assert info_makeup.beijing_tail_limit == "不限行"

    # 2026-10-01 国庆正日（周四）：休息日 + 不限行
    info_holiday = day_info(date(2026, 10, 1), sandbox_db.paths)
    assert info_holiday.is_rest_day is True
    assert info_holiday.beijing_tail_limit == "不限行"

    # 2026-11-16 周一正常工作日：工作日 + 尾号 3/8
    info_mon = day_info(date(2026, 11, 16), sandbox_db.paths)
    assert info_mon.is_workday is True
    assert info_mon.beijing_tail_limit == "3/8"

    # 非补班休息日列表必须自动排掉 2026-10-10 补班周六
    rest_list = upcoming_non_makeup_rest_days(start=date(2026, 10, 8), horizon_days=5, paths=sandbox_db.paths)
    rest_dates = {r.day for r in rest_list}
    assert date(2026, 10, 10) not in rest_dates
    assert date(2026, 10, 11) in rest_dates


def test_subtract_workdays_across_national_day(sandbox_db: SimingDB) -> None:
    # 2026-10-08 紧接国庆 7 天长假后：往前推 2 个中国工作日应跳过 10-01..10-07 落到 9-29
    prev = subtract_workdays(date(2026, 10, 8), 2, paths=sandbox_db.paths)
    assert prev == date(2026, 9, 29)


def test_12306_114_and_lunar(sandbox_db: SimingDB) -> None:
    r = rail_12306_alert_window(date(2026, 12, 1), "北京南站", paths=sandbox_db.paths)
    assert r["known"] is True
    assert r["sale_date"] == "2026-11-17"
    assert r["sale_time"] == "12:30"
    assert "12:15:00" in r["alert_at"]

    unknown_r = rail_12306_alert_window(date(2026, 12, 1), "火星东站", paths=sandbox_db.paths)
    assert unknown_r["known"] is False
    assert unknown_r["sale_time"] == "需核验"

    h = hospital_114_alert_window(date(2026, 12, 1), "北医三院", paths=sandbox_db.paths)
    assert h["known"] is True
    assert h["release_time"] == "07:00"

    lb = next_lunar_birthday(8, 15, ref_date=date(2026, 1, 1))
    assert lb["solar_date"] == "2026-09-25"


def test_hard_rule_conflicts_and_appliance_enrich(sandbox_db: SimingDB) -> None:
    conflicts = detect_hard_rule_conflicts("已预约 2026-10-10 上门安装洗烘机", db=sandbox_db)
    assert any(c["rule_id"] == "conflict_makeup_workday" for c in conflicts)

    c_bg = detect_hard_rule_conflicts("新下单了一套 KS 众筹 All-in 大盒桌游", db=sandbox_db)
    assert any(c["rule_id"] == "conflict_boardgame_volume" for c in c_bg)

    c_port = detect_hard_rule_conflicts("准备连接 http://127.0.0.1:9222/json", db=sandbox_db)
    assert any(c["rule_id"] == "conflict_port_9222" for c in c_port)

    enriched = enrich_appliance_arrival(
        name="西门子 WN52J7I10W 10kg 洗烘一体",
        room="厨房",
        install_note="京东送装一体",
        platform="jd",
        svc_orders=["9000000000000003"],
        db=sandbox_db,
    )
    assert "400-889-9999" in enriched
    assert "可约休息日" in enriched
