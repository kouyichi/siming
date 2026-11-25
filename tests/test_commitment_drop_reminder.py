"""2026-11-20 手机截屏「周末桌游局约四点局」漏提醒全链路复盘与回归测试。"""

from __future__ import annotations

import json
from datetime import datetime

from siming.cn_calendar import strip_chat_divider_timestamps, try_parse_explicit_datetime
from siming.collectors.edge_sync import process_edge_records
from siming.db import SHANGHAI_TZ, SimingDB, iso_now
from siming.evald import consume_pending_effects
from siming.outing_chain import bind_commitment_destination, evaluate_upcoming_trip_prep
from siming.qq_cards import handle_qq_button_action, infer_keyboard_for_message
from siming.remote import ask_knowledge, drop_item
from siming.router import handle_inbound_qq
from siming.session_scanner import META_SKIP_RE
from siming.superagent_triage import extract_commitment_from_drop
from siming.tesla_link import evaluate_departure_auto_climate, ingest_tesla_state, sync_mobile_outing_plan
from siming.waits import evaluate_waits, list_waits


REAL_SHOT_20261120 = (
    "周末桌游局（7）\n"
    "你必到寺丁个木删\n"
    "王决：我这边逼事很多也得加加班\n"
    "加班好晚和七点起床你航美美吃饭去了就\n"
    "孟帅\n"
    "他四点一刻到家昂\n"
    "20:14\n"
    "孟帅\n"
    "咱们约个四点局\n"
    "王决那不会\n"
    "孟帅\n"
    "已经和迪迪子说的是四点了昂\n"
    "王决\n"
    "我航今天美睡了一天\n"
    "王决\n"
    "明天应该精力大充足\n"
    "王决\n"
    "问题不大"
)


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI_TZ)


def test_real_20261120_boardgame_screenshot_schedules_1600_reminder_and_receipt(sandbox_db: SimingDB) -> None:
    """完整回放 2026-11-20 23:40 真实截屏：
    1. 剥离微信分隔时间戳 20:14，忽略闲聊里的『七点起床』与『他四点一刻到家』；
    2. 精准锁定『周末桌游局』2026-11-21 16:00（下午四点，绝非凌晨四点或 20:14）；
    3. 不被 tesla_link.sync_mobile_outing_plan 误劫持成『开车出门前往 了就』；
    4. 同时落 events + 显式定时提醒 wait (15:00) + expectations(kind='outing', 16:00) + QQ 确认回执与取消按钮；
    5. 到点后可穿透安静/补觉窗口正常发出提醒，且点击取消按钮同步撤回 expectation。
    """
    # 1. 单独验证 sync_mobile_outing_plan 绝不再劫持多行群聊截图
    assert sync_mobile_outing_plan(sandbox_db, REAL_SHOT_20261120, source="ios_drop")["matched"] is False

    # 2. 通过 edge_sync 入口完整回放 2026-11-20T23:40:15+08:00 投递
    stats = process_edge_records(
        sandbox_db,
        [
            {
                "id": 2338,
                "source": "ios_drop",
                "text": REAL_SHOT_20261120,
                "received_at": "2026-11-20T23:40:15+08:00",
            }
        ],
    )
    assert stats["drop"] == 1
    assert stats["deadletter"] == 0

    # 3. 核验 waits：已建显式定时提醒，提前 60m 在 2026-11-21 15:00 触发
    waits = [w for w in list_waits(sandbox_db, status="WAITING") if w["trigger_kind"] == "time"]
    assert len(waits) == 1
    w = waits[0]
    assert w["strength"] == "explicit"
    assert w["action_mode"] == "remind"
    assert w["next_fire_at"] == "2026-11-21T15:00:00+08:00"
    assert "周末桌游局" in w["quote"]
    assert "16:00" in w["quote"]

    # 4. 核验 events 与可检索性（不再像以前一样被 outing_plan 静默吞掉）
    conn = sandbox_db.connect()
    try:
        ev = conn.execute("SELECT * FROM events WHERE source='ios_drop'").fetchone()
        assert ev is not None
        assert "周末桌游局" in ev["summary"]
        assert "四点局" in ev["payload_json"]

        # 5. 核验 expectations：正确写入 outing 16:00，且绝无『了就』垃圾项
        exps = conn.execute("SELECT * FROM expectations WHERE status='ACTIVE'").fetchall()
        assert len(exps) == 1
        assert exps[0]["kind"] == "outing"
        assert exps[0]["target_at"] == "2026-11-21T16:00:00+08:00"
        assert exps[0]["source_ref"] == w["wait_id"]
        assert "了就" not in exps[0]["title"]

        # 6. 核验 QQ 即时确认回执已发出、主动追问目的地并挂载『就在家/线上』+『取消提醒』原生按钮
        obs = conn.execute("SELECT * FROM outbox WHERE rule_id='commitment_drop'").fetchall()
        assert len(obs) == 1
        receipt_txt = str(obs[0]["text"])
        assert "周末桌游局" in receipt_txt
        assert "11-21" in receipt_txt and "16:00" in receipt_txt and "15:00" in receipt_txt
        assert "这次去哪里" in receipt_txt
        kb = infer_keyboard_for_message(
            sandbox_db, receipt_txt, ref_id=w["wait_id"], rule_id="commitment_drop"
        )
        assert kb is not None
        btn_datas = [b["action"]["data"] for b in kb["content"]["rows"][0]["buttons"]]
        assert f"siming:trip_local:{w['wait_id']}" in btn_datas
        assert f"siming:case_act:{w['wait_id']}:取消" in btn_datas
        # 同步登记了 trip_destination 主动求证问题
        cq = conn.execute("SELECT status, topic FROM curiosity_questions WHERE topic='trip_destination'").fetchone()
        assert cq is not None and cq["status"] == "ASKED"
    finally:
        conn.close()

    # 7. siming ask 可以检索到该截屏约定
    found = ask_knowledge(sandbox_db, "桌游局")
    assert found["found"] is True
    assert "周末桌游局" in found["answer"]

    # 8. 重复投递同一张截图幂等去重，不重复建 wait
    dup_res = drop_item(
        sandbox_db,
        text=REAL_SHOT_20261120,
        source_channel="ios_drop",
        occurred_at="2026-11-20T23:41:00+08:00",
    )
    assert dup_res["ok"] is True and dup_res.get("duplicate") is True
    assert len([x for x in list_waits(sandbox_db, status="WAITING") if x["trigger_kind"] == "time"]) == 1

    # 9. 点击 QQ『取消提醒』按钮应同时取消 wait、关联 expectation 与 trip_destination 求证项
    btn_res = handle_qq_button_action(sandbox_db, f"siming:case_act:{w['wait_id']}:取消")
    assert btn_res["ok"] is True
    conn = sandbox_db.connect()
    try:
        assert conn.execute("SELECT status FROM waits WHERE wait_id=?", (w["wait_id"],)).fetchone()["status"] == "CANCELLED"
        assert conn.execute("SELECT status FROM expectations WHERE source_ref=?", (w["wait_id"],)).fetchone()["status"] == "CANCELLED"
        assert conn.execute("SELECT status FROM curiosity_questions WHERE topic='trip_destination'").fetchone()["status"] == "DISMISSED"
    finally:
        conn.close()


def test_explicit_commitment_wait_fires_even_during_quiet_hours(sandbox_db: SimingDB) -> None:
    """显式定时约定提醒到点时必须穿透安静/补觉窗口，绝不能被压进 MORNING_QUEUE 导致错过约定。"""
    # 置为显式睡眠态，模拟睡懒觉场景
    sandbox_db.set_meta("attention_explicit_state", "ASLEEP")
    sandbox_db.set_meta("attention_explicit_at", iso_now())

    res = drop_item(
        sandbox_db,
        text=REAL_SHOT_20261120,
        source_channel="ios_drop",
        occurred_at="2026-11-20T23:40:15+08:00",
    )
    wid = res["wait_id"]

    # 把触发时间拨到过去模拟到点（15:00）
    with sandbox_db.transaction() as conn:
        conn.execute("UPDATE waits SET next_fire_at='2020-01-01T15:00:00+08:00' WHERE wait_id=?", (wid,))

    ev_res = evaluate_waits(sandbox_db)
    assert wid in ev_res["fired"]
    consume_pending_effects(sandbox_db)
    consume_pending_effects(sandbox_db, include_workers=False)

    conn = sandbox_db.connect()
    try:
        ob = conn.execute(
            "SELECT status, text FROM outbox WHERE ref_id=? AND rule_id='wait_remind' ORDER BY created_at DESC LIMIT 1",
            (wid,),
        ).fetchone()
        assert ob is not None
        assert ob["status"] == "SENT"  # 穿透 ASLEEP，绝不滞留 MORNING_QUEUE
        assert "周末桌游局" in ob["text"]
    finally:
        conn.close()


def test_cn_calendar_chat_timestamps_quarters_and_pm_disambiguation(sandbox_db: SimingDB) -> None:
    # 1. 多行聊天里独立行 20:14 必须被剥离，不覆盖真正的约定时间
    stripped = strip_chat_divider_timestamps("孟帅\n20:14\n咱们约个四点局")
    assert "20:14" not in stripped and "四点局" in stripped

    # 2. 两行短备忘无其它时间线索时保留第 2 行时刻
    assert strip_chat_divider_timestamps("明天的会议\n16:00") == "明天的会议\n16:00"

    # 3. 无上午/凌晨标注的 1-6 点社交/未来日程默认下午（13:00-18:00），同时保留凌晨/早上/半夜 00:30 说 2 点睡
    ref_day = _dt("2026-11-20 14:00")
    dt_4pm, _ = try_parse_explicit_datetime("约了4点", ref_dt=ref_day, paths=sandbox_db.paths)  # type: ignore[misc]
    assert (dt_4pm.day, dt_4pm.hour, dt_4pm.minute) == (20, 16, 0)

    dt_qtr, _ = try_parse_explicit_datetime("明天四点一刻开会", ref_dt=ref_day, paths=sandbox_db.paths)  # type: ignore[misc]
    assert (dt_qtr.day, dt_qtr.hour, dt_qtr.minute) == (21, 16, 15)

    dt_dawn, sn = try_parse_explicit_datetime("明天凌晨4点提醒我", ref_dt=ref_day, paths=sandbox_db.paths)  # type: ignore[misc]
    assert (dt_dawn.day, dt_dawn.hour, sn) == (21, 4, True)

    ref_mid = _dt("2026-11-21 00:30")
    dt_sleep, _ = try_parse_explicit_datetime("2点提醒我睡觉", ref_dt=ref_mid, paths=sandbox_db.paths)  # type: ignore[misc]
    assert (dt_sleep.day, dt_sleep.hour) == (21, 2)

    # 4. 未来日（周末/下周）优先于闲聊里的『今天』
    ref_fri = _dt("2026-11-20 23:40")
    dt_wknd, _ = try_parse_explicit_datetime("今天没空，周末四点局", ref_dt=ref_fri, paths=sandbox_db.paths)  # type: ignore[misc]
    assert (dt_wknd.day, dt_wknd.hour) == (21, 16)


def test_commitment_extractor_variants_and_anti_false_positives(sandbox_db: SimingDB) -> None:
    ref = _dt("2026-11-20 20:00")

    # 1. 一对一聊天（日期与时间跨行）
    cm1 = extract_commitment_from_drop(
        "22:15\n王总\n明天有空吗\n有的\n那咱们下午2点开个会\n行",
        ref_dt=ref,
        paths=sandbox_db.paths,
    )
    assert cm1 is not None
    assert cm1["event_at"] == "2026-11-21T14:00:00+08:00"
    assert cm1["remind_at"] == "2026-11-21T13:00:00+08:00"

    # 2. 预约确认单
    cm2 = extract_commitment_from_drop(
        "10:05\n预约成功\n就诊日期：2026-12-03 14:30\n常用医院 内分泌门诊",
        ref_dt=ref,
        paths=sandbox_db.paths,
    )
    assert cm2 is not None
    assert cm2["event_at"] == "2026-12-03T14:30:00+08:00"
    assert cm2["remind_at"] == "2026-12-03T13:30:00+08:00"

    # 3. MM/DD 日期不被误判为 GitHub 仓库（如 11/21 下午4点见）
    r_md = drop_item(
        sandbox_db,
        text="11/21 下午4点三里屯碰面",
        source_channel="ios_drop",
        occurred_at="2026-11-20T20:00:00+08:00",
    )
    assert r_md["ok"] is True and r_md["kind"] == "commitment"
    assert r_md["event_at"] == "2026-11-21T16:00:00+08:00"

    # 4. 已取消的约局绝不建提醒
    cm_cancel = extract_commitment_from_drop(
        "周末桌游局（7）\n20:14\n孟帅\n咱们约个四点局\n孟帅\n明天四点局取消了，改天再约",
        ref_dt=ref,
        paths=sandbox_db.paths,
    )
    assert cm_cancel is None

    # 5. 论文/资讯截图与无钟点报价绝不误判为约定
    assert (
        extract_commitment_from_drop(
            "15:04\n答案，或许藏在谷歌与宾夕法尼亚州立大学深夜联合发布的一篇最新论文里。",
            ref_dt=ref,
            paths=sandbox_db.paths,
        )
        is None
    )
    assert (
        extract_commitment_from_drop(
            "工长报水电增项 1680 元，周五现场核实",
            ref_dt=ref,
            paths=sandbox_db.paths,
        )
        is None
    )

    # 6. 排障吐槽句（『但是你没有提醒我，为什么，修复』）绝不再被 session_scanner 误建 inferred wait
    assert META_SKIP_RE.search("昨天你收到了一个手机截图指令，说的是约了4点，但是你没有提醒我，为什么，修复") is not None


def test_destination_inquiry_reply_and_predeparture_live_traffic_prep(sandbox_db: SimingDB, monkeypatch) -> None:
    """端到端验证：截屏未写地点 -> 主动追问去哪里 -> 主人回复地点 -> 临出行前查实时路况动态前移并在该准备时推送出行准备卡。"""
    monkeypatch.setattr("siming.outing_chain.now_local", lambda: _dt("2026-11-21 10:00"))
    ingest_tesla_state(
        sandbox_db,
        {"soc": 76, "range_km": 330, "inside_temp_c": 7.5, "charging_state": "Disconnected", "shift_state": "P"},
    )
    sandbox_db.set_meta(
        "latest_ios_location",
        json.dumps({"place_alias": "家", "lat": 39.904200, "lng": 116.407400, "updated_at": "2026-11-21T13:55:00+08:00"}, ensure_ascii=False),
    )

    # 1. 昨晚 23:40 投递无地点截屏 -> 主动追问『这次去哪里？』
    res = drop_item(
        sandbox_db,
        text=REAL_SHOT_20261120,
        source_channel="ios_drop",
        occurred_at="2026-11-20T23:40:15+08:00",
    )
    assert res["ok"] and res["needs_destination"] is True
    assert "这次去哪里" in res["summary"]
    wid = res["wait_id"]
    exp_id = res["exp_id"]

    # 2. 主人在 QQ 随口回复『去朝阳大悦城』（无需引用也能命中活跃的 trip_destination 追问）
    qq_ans = handle_inbound_qq(sandbox_db, "去朝阳大悦城")
    assert qq_ans["handled"] is True
    assert "朝阳大悦城" in qq_ans["reply"]
    assert "参考车程" in qq_ans["reply"]

    conn = sandbox_db.connect()
    try:
        cq = conn.execute("SELECT status, answer FROM curiosity_questions WHERE topic='trip_destination'").fetchone()
        assert cq is not None and cq["status"] == "ANSWERED" and "朝阳大悦城" in cq["answer"]
        exp = conn.execute("SELECT place, counterparty_json FROM expectations WHERE exp_id=?", (exp_id,)).fetchone()
        assert exp["place"] == "朝阳大悦城"
    finally:
        conn.close()

    # 3. 临出行前 2 小时（14:00）：模拟实时路况升至 48 分钟 -> 建议 15:02 出门，准备时刻前移至 14:47；此刻未到 14:47，静默校准不提前打扰
    r_early = evaluate_upcoming_trip_prep(
        sandbox_db,
        now=_dt("2026-11-21 14:00"),
        route_fetcher=lambda *_: {"road_km": 19.8, "duration_min": 48, "source": "mock_live"},
    )
    assert exp_id in r_early["checked"]
    assert exp_id in r_early["shifted"]
    assert r_early["notified"] == []
    conn = sandbox_db.connect()
    try:
        w_row = conn.execute("SELECT next_fire_at, status FROM waits WHERE wait_id=?", (wid,)).fetchone()
        assert w_row["status"] == "WAITING"
        assert w_row["next_fire_at"] == "2026-11-21T14:47:00+08:00"
    finally:
        conn.close()

    # 4. 14:40 再次复查：突然堵车飙升至 55 分钟 -> 准备时刻前移至 14:40（16:00 - 55m - 25m）-> 当场推送实时准备卡并核销保底 wait！
    r_jam = evaluate_upcoming_trip_prep(
        sandbox_db,
        now=_dt("2026-11-21 14:40"),
        route_fetcher=lambda *_: {"road_km": 19.8, "duration_min": 55, "source": "mock_live_jam"},
    )
    assert exp_id in r_jam["notified"]
    conn = sandbox_db.connect()
    try:
        w_done = conn.execute("SELECT status FROM waits WHERE wait_id=?", (wid,)).fetchone()
        assert w_done["status"] == "DONE"  # 已核销，避免 evaluate_waits 重复发 generic 提醒
        ob = conn.execute(
            "SELECT status, text FROM outbox WHERE rule_id='trip_prep' AND ref_id=?",
            (exp_id,),
        ).fetchone()
        assert ob is not None and ob["status"] == "SENT"
        assert "该准备出门了" in ob["text"]
        assert "朝阳大悦城" in ob["text"]
        assert "55 分钟" in ob["text"]
        assert "14:55" in ob["text"]
        assert "76%" in ob["text"]
    finally:
        conn.close()

    # 5. 同一行程绝不重复推送第二次准备卡
    r_dup = evaluate_upcoming_trip_prep(
        sandbox_db,
        now=_dt("2026-11-21 14:45"),
        route_fetcher=lambda *_: {"road_km": 19.8, "duration_min": 55, "source": "mock_live_jam"},
    )
    assert r_dup["notified"] == []


def test_quoted_reply_destination_and_local_online_button_skip_tesla(sandbox_db: SimingDB, monkeypatch) -> None:
    """验证：① 引用回执卡直接写裸地名（无需前缀）可绑定地点；② 点『就在家/线上』按钮调为提前 15m 提醒且不触发备车。"""
    monkeypatch.setattr("siming.outing_chain.now_local", lambda: _dt("2026-11-21 10:00"))
    # 1. 引用回执卡直接回裸地名『三里屯』
    r1 = drop_item(
        sandbox_db,
        text=REAL_SHOT_20261120,
        source_channel="ios_drop",
        occurred_at="2026-11-20T23:40:15+08:00",
    )
    wid1 = r1["wait_id"]
    q_res = handle_inbound_qq(
        sandbox_db,
        "三里屯",
        reply_to_text=r1["summary"],
    )
    assert q_res["handled"] is True
    assert "三里屯" in q_res["reply"]

    # 2. 点击『就在家/线上』按钮 -> 调为 15:45 提醒，不走车程查询、不误开特斯拉空调
    r2 = drop_item(
        sandbox_db,
        text="线上桌游群（5）\n20:14\n明天咱们约个四点局",
        source_channel="ios_drop",
        occurred_at="2026-11-20T23:40:15+08:00",
    )
    wid2 = r2["wait_id"]
    exp2 = r2["exp_id"]
    b_res = handle_qq_button_action(sandbox_db, f"siming:trip_local:{wid2}")
    assert b_res["ok"] is True
    assert "在家/线上" in b_res["reply"]

    conn = sandbox_db.connect()
    try:
        w2 = conn.execute("SELECT next_fire_at FROM waits WHERE wait_id=?", (wid2,)).fetchone()
        assert w2["next_fire_at"] == "2026-11-21T15:45:00+08:00"
    finally:
        conn.close()

    # 临出行巡检跳过 no_travel，且离家不会为线上局误开特斯拉空调
    prep_res = evaluate_upcoming_trip_prep(sandbox_db, now=_dt("2026-11-21 15:00"))
    assert exp2 not in prep_res["notified"]

    ingest_tesla_state(
        sandbox_db,
        {"soc": 80, "range_km": 348, "inside_temp_c": 5.0, "charging_state": "Disconnected"},
    )
    # 清除 r1 的活跃 expectation 只留 no_travel 的 exp2
    with sandbox_db.transaction() as conn:
        conn.execute("UPDATE expectations SET status='CANCELLED' WHERE exp_id=?", (r1["exp_id"],))
    clim_calls: list[bool] = []
    ev_c = evaluate_departure_auto_climate(
        sandbox_db,
        departed_now=True,
        depart_place="家",
        now=_dt("2026-11-21 15:25"),
        climate_runner=lambda turn_on=True, target_temp_c=21.5: (clim_calls.append(turn_on) or {"ok": True}),
    )
    assert clim_calls == []
    assert ev_c.get("skipped") in ("no_synced_driving_outing", "no_driving_plan_workday_walk_guard")

