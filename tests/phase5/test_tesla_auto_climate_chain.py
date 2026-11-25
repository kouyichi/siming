"""EC-5 出门意图同步 + 五重门控自动备车开空调事件链全场景测试。"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from siming.context_loc import ingest_ios_location
from siming.db import SHANGHAI_TZ
from siming.evald import consume_pending_effects
from siming.router import handle_inbound_qq
from siming.tesla_link import (
    evaluate_departure_auto_climate,
    ingest_tesla_state,
    sync_mobile_outing_plan,
)


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI_TZ)


def test_synced_outing_plus_departure_cold_cabin_starts_heat(sandbox_db):
    calls: list[dict[str, Any]] = []

    def fake_cli(turn_on: bool = True, target_temp_c: float = 21.5) -> dict[str, Any]:
        calls.append({"turn_on": turn_on, "target_temp_c": target_temp_c})
        return {"ok": True, "turn_on": turn_on, "target_temp_c": target_temp_c}

    # 1. 初始车况：电量 76% 充足，冬季车内仅 6.8°C
    ingest_tesla_state(
        sandbox_db,
        {"soc": 76, "range_km": 330, "inside_temp_c": 6.8, "charging_state": "Disconnected", "shift_state": "P"},
    )

    # 2. 手机提前同步下午 14:00 去新房（示例小区B）（当前 11:00 仅登记，不提前乱开空调）
    s_res = sync_mobile_outing_plan(
        sandbox_db,
        "今天14:00去新房（示例小区B）",
        now=_dt("2026-11-21 11:00"),
        climate_runner=fake_cli,
    )
    assert s_res["matched"] and s_res["driving"] is True
    assert len(calls) == 0

    # 3. 13:35 检测到本人离开家出门 -> 命中日程窗口 + 车内 6.8°C 过冷 + 电量 76% -> 自动开启制热至 21.5°C！
    ev_res = evaluate_departure_auto_climate(
        sandbox_db,
        departed_now=True,
        depart_place="家",
        trigger="owntracks_depart",
        now=_dt("2026-11-21 13:35"),
        climate_runner=fake_cli,
    )
    assert ev_res["action"] == "climate_started" and ev_res["mode"] == "heat"
    assert calls == [{"turn_on": True, "target_temp_c": 21.5}]

    # 4. 18 分钟后仍未挂挡发车（shift_state=='P'） -> 自动关停空调防忘关白耗电！
    shut_res = evaluate_departure_auto_climate(
        sandbox_db,
        departed_now=False,
        trigger="tick",
        now=_dt("2026-11-21 13:55"),
        climate_runner=fake_cli,
    )
    assert shut_res["action"] == "auto_shutoff_18m"
    assert calls[-1]["turn_on"] is False


def test_imminent_mobile_sync_summer_hot_cabin_starts_cool(sandbox_db):
    calls: list[dict[str, Any]] = []

    def fake_cli(turn_on: bool = True, target_temp_c: float = 22.0) -> dict[str, Any]:
        calls.append({"turn_on": turn_on, "target_temp_c": target_temp_c})
        return {"ok": True}

    ingest_tesla_state(
        sandbox_db,
        {"soc": 65, "range_km": 282, "inside_temp_c": 35.2, "charging_state": "Disconnected"},
    )
    # 手机直接发『我准备出门去山姆了』 -> 当场立即开启制冷降温至 22.0°C！
    res = sync_mobile_outing_plan(
        sandbox_db,
        "我准备出门去山姆了",
        now=_dt("2026-07-18 15:10"),
        climate_runner=fake_cli,
    )
    assert res["matched"] and res["climate"]["action"] == "climate_started"
    assert res["climate"]["mode"] == "cool"
    assert calls == [{"turn_on": True, "target_temp_c": 22.0}]


def test_guards_walk_commute_subway_comfortable_temp_and_low_battery(sandbox_db):
    calls: list[dict[str, Any]] = []
    fake_cli = lambda turn_on=True, target_temp_c=21.5: (calls.append({"turn_on": turn_on}) or {"ok": True})

    ingest_tesla_state(
        sandbox_db,
        {"soc": 80, "range_km": 348, "inside_temp_c": 5.0, "charging_state": "Disconnected"},
    )

    # 防呆 1：工作日早 09:15 从示例小区A『家』出门上班（无同步驾车计划） -> 判定为步行上班，绝不误开特斯拉空调！
    r_walk = evaluate_departure_auto_climate(
        sandbox_db,
        departed_now=True,
        depart_place="家",
        now=_dt("2026-11-19 09:15"),
        climate_runner=fake_cli,
    )
    assert r_walk["skipped"] == "no_driving_plan_workday_walk_guard"
    assert len(calls) == 0

    # 防呆 2：明确同步『坐地铁/打车出门』 -> 绝不开特斯拉空调！
    sync_mobile_outing_plan(sandbox_db, "一会坐地铁去五道口吃饭", now=_dt("2026-11-21 18:00"), climate_runner=fake_cli)
    r_sub = evaluate_departure_auto_climate(
        sandbox_db,
        departed_now=True,
        depart_place="家",
        now=_dt("2026-11-21 18:05"),
        climate_runner=fake_cli,
    )
    assert r_sub["skipped"] == "explicit_non_driving_outing"
    assert len(calls) == 0

    # 防呆 3：车内 20.0°C 温度适宜 -> 不开空调省电！
    ingest_tesla_state(sandbox_db, {"soc": 80, "range_km": 348, "inside_temp_c": 20.0, "charging_state": "Disconnected"})
    r_comf = sync_mobile_outing_plan(sandbox_db, "准备开车去宜家", now=_dt("2026-11-21 14:00"), climate_runner=fake_cli)
    assert r_comf["climate"]["skipped"] == "cabin_temp_comfortable"
    assert len(calls) == 0

    # 防呆 4：电量仅 22% (<25%) 且去普通商场未插枪 -> 拒绝耗电开暖风并提醒补电；但若目的地就是『去超充充电』则允许(>=20%)！
    ingest_tesla_state(sandbox_db, {"soc": 22, "range_km": 95, "inside_temp_c": 4.5, "charging_state": "Disconnected"})
    r_low = sync_mobile_outing_plan(sandbox_db, "准备开车去合生汇", now=_dt("2026-11-21 16:00"), climate_runner=fake_cli)
    consume_pending_effects(sandbox_db, include_workers=False)
    assert r_low["climate"]["action"] == "low_soc_skipped"
    assert len(calls) == 0

    r_chg = sync_mobile_outing_plan(sandbox_db, "准备出门去大钟寺超充充电", now=_dt("2026-11-21 16:10"), climate_runner=fake_cli)
    assert r_chg["climate"]["action"] == "climate_started"
    assert len(calls) == 1


def test_owntracks_leave_home_triggers_auto_climate_end_to_end(sandbox_db, monkeypatch):
    monkeypatch.setattr(
        "siming.tesla_link.control_tesla_climate",
        lambda db, turn_on=True, target_temp_c=21.5, cli_runner=None: {"ok": True, "turn_on": turn_on, "target_temp_c": target_temp_c},
    )
    ingest_tesla_state(sandbox_db, {"soc": 70, "range_km": 304, "inside_temp_c": 6.0, "charging_state": "Disconnected"})

    # 1. 在家连着 Wi-Fi，先在 QQ 同步一句『一会去新房（示例小区B）』（设置未来 20 分钟出发）
    ingest_ios_location(sandbox_db, '{"wifi":"CU_tdNN","lat":39.904200,"lon":116.407400}')
    future_hm = (datetime.now(tz=SHANGHAI_TZ) + timedelta(minutes=25)).strftime("%H:%M")
    r_q = handle_inbound_qq(sandbox_db, f"今天{future_hm}去新房（示例小区B）")
    assert r_q["handled"] and "已接入出门备车事件链" in r_q["reply"]

    # 2. OwnTracks 上报断开家 Wi-Fi / 离开围栏出门 -> 自动触发空调开启！
    ingest_ios_location(sandbox_db, '{"event":"leave","lat":39.903500,"lon":116.407400,"vel":4}')
    assert sandbox_db.get_meta("last_tesla_auto_climate_at")


def test_garage_anchor_distinguishes_walk_commute_vs_walking_into_garage(sandbox_db):
    import json
    from siming.tesla_link import HOME_GARAGE_COORDS, format_tesla_status

    calls = []
    fake_cli = lambda turn_on=True, target_temp_c=21.5: (calls.append(turn_on) or {"ok": True})

    ingest_tesla_state(
        sandbox_db,
        {
            "soc": 78,
            "range_km": 339,
            "inside_temp_c": 5.5,
            "charging_state": "Disconnected",
            "car_lat": HOME_GARAGE_COORDS[0],
            "car_lng": HOME_GARAGE_COORDS[1],
        },
    )
    assert "常用车库" in format_tesla_status(sandbox_db)

    # 1. 示例小区A -> 示例写字楼南北步行线 (lng=116.4072, 距车库 > 180m)：即使没同步行程也不会误判进车库
    sandbox_db.set_meta("latest_ios_location", json.dumps({"lat": 39.905800, "lng": 116.407200}))
    r1 = evaluate_departure_auto_climate(sandbox_db, departed_now=True, depart_place="家", now=_dt("2026-11-19 09:20"), climate_runner=fake_cli)
    assert r1["skipped"] == "no_driving_plan_workday_walk_guard" and len(calls) == 0

    # 2. 向东偏离步行线直接走入常用车库 110m 圈内 (39.90648, 116.40948)：就算忘记提前发消息，也自动识别取车暖车！
    sandbox_db.set_meta("latest_ios_location", json.dumps({"lat": 39.906480, "lng": 116.409480}))
    r2 = evaluate_departure_auto_climate(sandbox_db, departed_now=True, depart_place="公司", now=_dt("2026-11-19 21:40"), climate_runner=fake_cli)
    assert r2["action"] == "climate_started" and r2["dest"] == "车库取车出发" and len(calls) == 1


def test_explicit_qq_voice_precondition_overrides_25pct_guard_and_qq_buttons(sandbox_db, monkeypatch):
    """回归 2026-11-21 线上事故：
    Model Y 剩余 24%（车内 11.3°C）时：
    1. `[Voice] 我马上出发去车里了。` 自动触发 25% 省电保护时，不误解析目的地为『车里』、不重复双发、并自动挂出 QQ 原生『电量够，立即备车』按钮；
    2. 本人在 QQ 语音明确说 `[Voice] 我需要去的地方，电量是够的，你开始备车。` 或 `[Voice] 我再明确告诉你一遍，我自己知道需要去的地方，电量是够的，开始备车。`
       必须直接开启空调（跳过 25% 自动安全线），且绝不建出『开车出门前往 的地方』的脏行程；
    3. 在低电量跳过后的 30 分钟内，即使只回 `[Voice] 我要去的地方电量是够的。` 或点按钮 `siming:cmd:备车`，也能立即放行备车；
    4. 问句/报错/室内空调/未来定时/聊天截图残片（如『吃饭去了就』）绝不误触发备车或脏行程。
    """
    from siming.qq_cards import handle_qq_button_action
    from siming.tesla_link import parse_explicit_tesla_climate_command

    calls: list[dict[str, Any]] = []

    def fake_control(db, turn_on: bool = True, target_temp_c: float = 21.5, cli_runner=None):
        calls.append({"turn_on": turn_on, "target_temp_c": target_temp_c})
        return {"ok": True, "turn_on": turn_on, "target_temp_c": target_temp_c}

    monkeypatch.setattr("siming.tesla_link.control_tesla_climate", fake_control)

    # 初始车况：电量 24%（低于 25% 自动线，但高于特斯拉车机 20% 底线），车内 11.3°C
    ingest_tesla_state(
        sandbox_db,
        {"soc": 24.0, "range_km": 96.6, "inside_temp_c": 11.3, "charging_state": "Disconnected", "shift_state": ""},
    )

    # 1) 语音出门未显式强制备车 -> 自动备车跳过省电，目的地清洗为『取车出发』，且自动附带 QQ 原生放行按钮
    r_depart = handle_inbound_qq(sandbox_db, "[Voice] 我马上出发去车里了。")
    assert r_depart["handled"] is True
    assert "自动备车已跳过省电" in r_depart["reply"]
    assert "前往 **车里**" not in r_depart["reply"]
    assert "取车出发" in r_depart["reply"]
    assert len(calls) == 0
    kb_skip = r_depart.get("keyboard")
    assert kb_skip is not None
    btn_data = [b["action"]["data"] for row in kb_skip["content"]["rows"] for b in row["buttons"]]
    assert "siming:cmd:备车" in btn_data

    # 不应 via outbox 重复推送同一条低电量跳过
    consume_pending_effects(sandbox_db, include_workers=False)
    conn = sandbox_db.connect()
    try:
        dup_cnt = conn.execute("SELECT COUNT(*) FROM outbox WHERE rule_id='tesla_auto_climate'").fetchone()[0]
    finally:
        conn.close()
    assert dup_cnt == 0

    # 2) 用户语音明确要求『开始备车』并声明电量够 -> 立即开启制热暖车，跳过 25% 自动安全线，绝不写脏目的地『的地方』
    r_cmd1 = handle_inbound_qq(sandbox_db, "[Voice] 我需要去的地方，电量是够的，你开始备车。")
    assert r_cmd1["handled"] is True
    assert "已按指令开启 Model Y 制热暖车" in r_cmd1["reply"]
    assert "跳过 `25%` 自动省电门限" in r_cmd1["reply"]
    assert len(calls) == 1 and calls[0] == {"turn_on": True, "target_temp_c": 21.5}
    kb_on = r_cmd1.get("keyboard")
    assert kb_on is not None
    assert kb_on["content"]["rows"][0]["buttons"][0]["action"]["data"] == "siming:cmd:关空调"

    # 二次重复语音强调也应直接放行（不受 90m 自动冷却窗阻拦）
    r_cmd2 = handle_inbound_qq(
        sandbox_db,
        "[Voice] 我再明确告诉你一遍，我自己知道需要去的地方，电量是够的，开始备车。",
    )
    assert r_cmd2["handled"] is True
    assert "已按指令开启 Model Y" in r_cmd2["reply"]
    assert len(calls) == 2

    # 核对 expectations 中绝无『的地方』脏记录
    conn = sandbox_db.connect()
    try:
        bad_exps = conn.execute("SELECT place FROM expectations WHERE place = '的地方'").fetchall()
    finally:
        conn.close()
    assert bad_exps == []

    # 3) 语音关空调同样兼容 [Voice] + 句号
    r_off = handle_inbound_qq(sandbox_db, "[Voice] 关空调。")
    assert r_off["handled"] is True and "已关闭" in r_off["reply"]
    assert calls[-1]["turn_on"] is False

    # 4) 重新触发低电量跳过 -> 仅回复『我要去的地方电量是够的』也能直接人工放行备车
    sandbox_db.set_meta("last_tesla_auto_climate_at", "")
    r_skip2 = handle_inbound_qq(sandbox_db, "准备开车去合生汇")
    assert r_skip2["handled"] and "自动备车已跳过省电" in r_skip2["reply"]
    r_followup = handle_inbound_qq(sandbox_db, "[Voice] 我要去的地方电量是够的。")
    assert r_followup["handled"] is True and "已按指令开启 Model Y" in r_followup["reply"]
    assert calls[-1]["turn_on"] is True

    # 5) 点击 QQ 原生按钮 `siming:cmd:备车` 与 `siming:cmd:关空调` 确定性直通
    b_on = handle_qq_button_action(sandbox_db, "siming:cmd:备车")
    assert b_on["ok"] is True and "已按指令开启 Model Y" in b_on["reply"]
    b_off = handle_qq_button_action(sandbox_db, "siming:cmd:关空调")
    assert b_off["ok"] is True and "已关闭" in b_off["reply"]

    # 6) 显式取消备车识别为 soft_cancel（不唤醒已关车），问句/报错/室内空调/未来定时/聊天截图残片均不误触发
    assert parse_explicit_tesla_climate_command("不要备车") == {"action": "off", "soft_cancel": True}
    assert parse_explicit_tesla_climate_command("取消备车") == {"action": "off", "soft_cancel": True}
    for non_cmd in (
        "qq明确说了开始备车 我要去的地方电量是够的，但是被拒绝了，修复",
        "为什么备车被拒绝了？",
        "备车了吗？",
        "明天早上8点备车",
        "提醒我出门前备车",
        "帮我把客厅空调打开",
        "一会出门不要备车",
        "准备开车去合生汇",
    ):
        assert parse_explicit_tesla_climate_command(non_cmd) is None, f"误命中: {non_cmd}"

    r_chat_noise = sync_mobile_outing_plan(
        sandbox_db,
        "周末桌游局（7）\n加班好晚和七点起床你航美美吃饭去了就\n20:14\n咱们约个四点局",
        source="ios_drop",
    )
    assert r_chat_noise["matched"] is False
