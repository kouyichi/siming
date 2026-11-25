"""Siming v5 Behavioral UX & Common-Sense QA Scenarios (QQ Chat, Meal Awareness, Morning Brief, Parcels/Pickups, Medical Care, Compound Voice & Weather Commute).

Locks real-world human-expectation behavioral fixes so Siming never behaves weirdly,
annoyingly, or dumbly in everyday QQ usage.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from siming.approval import create_approval
from siming.collectors.pickup_codes import try_ingest_pickup_code
from siming.compound_voice import parse_compound_utterance, stage_compound_approval
from siming.context_loc import ingest_ios_location
from siming.daily_care import (
    estimate_supplies,
    get_pen_state,
    record_refill,
    run_injection_guard,
    run_supply_radar,
    run_weather_guard,
    set_doses_left,
    set_supply_stock,
)
from siming.db import SHANGHAI_TZ, SimingDB, iso_now
from siming.event_chain import set_situation
from siming.meal_guard import (
    evaluate_meal_regularity,
    plan_dual_shop_combo,
    stage_dual_shop_combo_approval,
)
from siming.morning import build_morning_editions
from siming.qq_cards import handle_qq_button_action
from siming.router import (
    flush_morning_brief_if_due,
    flush_pickup_window_if_due,
    handle_inbound_qq,
    send_qq_message,
)
from siming.spatial_life import check_and_fire_offwork_pickup, evaluate_overtime_meal_ab


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI_TZ)


def _outbox_rows(db: SimingDB) -> list[dict]:
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM outbox ORDER BY created_at ASC").fetchall()]
    finally:
        conn.close()


def _write_two_home_policy(db: SimingDB, *, pickup_batch: str = "18:00") -> None:
    (db.paths.config_dir / "policy.yaml").write_text(
        f"""quiet_hours:
  start: '23:59'
  end: '00:00'
windows:
  morning_brief: '08:15'
  pickup_batch: '{pickup_batch}'
night_owl:
  latest_morning_brief: '11:45'
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
    leave_home_at: '09:15'
    commute: 步行
    commute_min: 10
  - id: secondary_home
    from: '2028-01-01'
    home_label: 示例小区B
    home_lat: 39.8600
    home_lon: 116.3200
    office_label: 公司
    office_lat: 39.908000
    office_lon: 116.407000
    leave_home_at: '08:25'
    commute: 地铁/打车
    commute_min: 60
""",
        encoding="utf-8",
    )


def _seed_office_location(db: SimingDB, at_dt: datetime) -> None:
    _write_two_home_policy(db)
    db.set_meta(
        "latest_ios_location",
        json.dumps(
            {
                "place_alias": "公司",
                "lat": 39.908000,
                "lng": 116.407000,
                "updated_at": iso_now(at_dt),
            },
            ensure_ascii=False,
        ),
    )


def _meteo(
    day: date,
    *,
    today_feel: float = 16.0,
    y_feel: float = 16.0,
    night_delta: float = -2.0,
    rain_hours: tuple[int, ...] = (),
    raining_now: bool = False,
) -> dict:
    times, temp, feel, prob, mm, code, wind = [], [], [], [], [], [], []
    for off in (-1, 0, 1):
        d = day + timedelta(days=off)
        for h in range(24):
            times.append(f"{d.isoformat()}T{h:02d}:00")
            base = y_feel if off == -1 else today_feel
            night = night_delta if h >= 21 else 0.0
            temp.append(base + 1 + night)
            feel.append(base + night)
            wet = off == 0 and h in rain_hours
            prob.append(85 if wet else 5)
            mm.append(1.5 if wet else 0.0)
            code.append(61 if wet else 1)
            wind.append(8.0)
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
        "current": {"precipitation": 0.5 if raining_now else 0.0, "weather_code": 61 if raining_now else 1},
    }


# ===========================================================================
# 1. Already-Ate / Recently-Ordered Awareness
# ===========================================================================


def test_group1_recent_food_order_ledger_or_cabinet_suppresses_overtime_and_meal_guard(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    """30–60 分钟前刚在美团/账本/外卖柜吃过晚饭 -> overtime_meal (20:05) 和 meal_guard 均不得再催吃晚饭。"""
    now = frozen_clock("2026-11-23T20:10:00+08:00")  # 周一 20:10
    _seed_office_location(sandbox_db, now)

    # 1A) 40 分钟前有一笔餐饮流水（ledger_txns，此前 overtime_meal 完全盲视！）
    tx_at = iso_now(now - timedelta(minutes=40))
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ledger_txns (
                txn_id, occurred_at, account_alias, card_last4, amount_cents, currency,
                kind, merchant, status, notes, created_at, updated_at
            ) VALUES ('TX-DINNER-1', ?, '招商银行', '1001', 4200, 'CNY', '消费', '示例粉面炒米粉', 'MATCHED', '晚餐', ?, ?)
            """,
            (tx_at, tx_at, tx_at),
        )
    ot1 = evaluate_overtime_meal_ab(sandbox_db, now=now, notify=True)
    assert ot1.get("skipped") == "dinner_already_detected"
    assert not ot1.get("mode_b_pushed")
    mg1 = evaluate_meal_regularity(sandbox_db, now=now, notify=True)
    assert mg1["should_nudge"] is False and mg1["reason"] == "recent_meal_found"

    # 1B) 换成仅有一条 35 分钟前的 food_pickup 外卖柜待办（无 shop_order_items / 无 ledger_txns）
    with sandbox_db.transaction() as conn:
        conn.execute("DELETE FROM ledger_txns")
        c_at = iso_now(now - timedelta(minutes=35))
        conn.execute(
            """
            INSERT INTO cases (case_id, title, next_step, status, category, rule_id, created_at, updated_at)
            VALUES ('S801', '美团外卖柜：示例写字楼3格口（取餐码 8842）', '热食及时取餐', '完成', 'food_pickup', 'meituan_cabinet', ?, ?)
            """,
            (c_at, c_at),
        )
    ot2 = evaluate_overtime_meal_ab(sandbox_db, now=now, notify=True)
    assert ot2.get("skipped") == "dinner_already_detected"
    mg2 = evaluate_meal_regularity(sandbox_db, now=now, notify=True)
    assert mg2["should_nudge"] is False and mg2["reason"] == "recent_meal_found"

    # 1C) 已取消的美团订单 & 美团药店买口罩绝不能误判成“已经吃过晚饭”！
    with sandbox_db.transaction() as conn:
        conn.execute("DELETE FROM cases")
        o_at = iso_now(now - timedelta(minutes=30))
        conn.execute(
            """
            INSERT INTO shop_order_items (item_key, platform, shop, ordered_at, order_status, order_amount_cents, item_name, qty, created_at)
            VALUES
              ('MT-CANCEL', 'meituan', '老乡鸡', ?, '已取消', 3500, '肥西老母鸡汤', 1, ?),
              ('MT-MASK', 'meituan', '养生堂药店', ?, '已完成', 2000, '海氏海诺一次性医用外科口罩 50只/盒', 1, ?)
            """,
            (o_at, o_at, o_at, o_at),
        )
    mg3 = evaluate_meal_regularity(sandbox_db, now=now, notify=False)
    assert mg3["should_nudge"] is True

    # 1D) 正常未取消的饿了么/美团餐食订单 -> 立刻抑制
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO shop_order_items (item_key, platform, shop, ordered_at, order_status, order_amount_cents, item_name, qty, created_at)
            VALUES ('MT-REAL', 'meituan', '示例瓦罐汤南昌粉面', ?, '已完成', 3600, '瓦罐汤拌粉套餐', 1, ?)
            """,
            (o_at, o_at),
        )
    ot4 = evaluate_overtime_meal_ab(sandbox_db, now=now, notify=True)
    assert ot4.get("skipped") == "dinner_already_detected"
    mg4 = evaluate_meal_regularity(sandbox_db, now=now, notify=True)
    assert mg4["should_nudge"] is False and mg4["reason"] == "recent_meal_found"


def test_group1_qq_meal_ack_text_and_button_suppress_nudges(sandbox_db: SimingDB, frozen_clock) -> None:
    """用户发「吃过了」/「今晚不点了」或点击 QQ 卡片按钮「✅ 吃过了」均能确定性生效并抑制本餐窗提醒。"""
    now = frozen_clock("2026-11-23T19:50:00+08:00")
    _seed_office_location(sandbox_db, now)

    # 1) QQ 文本「吃过了」
    r_ack = handle_inbound_qq(sandbox_db, "吃过了")
    assert r_ack["handled"] is True
    assert "不再打扰" in r_ack["reply"]
    assert evaluate_meal_regularity(sandbox_db, now=_dt("2026-11-23 20:10"), notify=True)["should_nudge"] is False
    assert evaluate_overtime_meal_ab(sandbox_db, now=_dt("2026-11-23 20:10"), notify=True).get("skipped") == "dinner_already_acked"

    # 2) 次日点击 meal_guard 卡片自带按钮 `siming:meal_ack:2026-11-24:dinner`（此前返回未知按钮类型报错！）
    day2 = frozen_clock("2026-11-24T19:45:00+08:00")
    _seed_office_location(sandbox_db, day2)
    mg_card = evaluate_meal_regularity(sandbox_db, now=day2, notify=False)
    assert mg_card["should_nudge"] is True and mg_card["keyboard"] is not None
    ack_btn_data = [
        b["action"]["data"]
        for row in mg_card["keyboard"]["content"]["rows"]
        for b in row["buttons"]
        if "meal_ack" in b["action"]["data"]
    ]
    assert ack_btn_data == ["siming:meal_ack:2026-11-24:dinner"]

    btn_res = handle_qq_button_action(sandbox_db, ack_btn_data[0])
    assert btn_res["ok"] is True
    assert "未识别" not in btn_res["reply"]
    assert evaluate_meal_regularity(sandbox_db, now=_dt("2026-11-24 20:15"), notify=True)["should_nudge"] is False
    assert evaluate_overtime_meal_ab(sandbox_db, now=_dt("2026-11-24 20:15"), notify=True).get("skipped") == "dinner_already_acked"

    # 3) 第三日发「今晚不点了」同样优雅抑制
    day3 = frozen_clock("2026-11-25T19:40:00+08:00")
    _seed_office_location(sandbox_db, day3)
    r_no = handle_inbound_qq(sandbox_db, "今晚不点了")
    assert r_no["handled"] is True
    assert evaluate_meal_regularity(sandbox_db, now=_dt("2026-11-25 20:10"), notify=True)["should_nudge"] is False


# ===========================================================================
# 2. Casual Chat Mis-Routing (False Command Hijacking)
# ===========================================================================


def test_group2_casual_chat_not_hijacked_while_genuine_commands_still_work(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    """自然闲聊绝不能被劫持成硬邦邦的状态转储或伪造 A2 复合确认卡；真正的指令必须照常工作。"""
    frozen_clock("2026-11-23T15:00:00+08:00")

    casual_messages = [
        "算力资产这块以后再优化吧",
        "帮我查一下什么是订阅刺客",
        "我朋友说示例粉面挺好吃的，周末想去",
        "销掉那个念头吧、明天再说",
        "销掉那个念头吧、顺便提醒我买水",
        "今天加班真累，但已经吃过了",
    ]
    for msg in casual_messages:
        res = handle_inbound_qq(sandbox_db, msg)
        assert res["handled"] is False, f"Casual chat was falsely hijacked: {msg!r} -> {res}"

    # 确认没有被污染出任何伪造 A2 审批卡
    conn = sandbox_db.connect()
    try:
        pending_apps = conn.execute("SELECT * FROM approvals WHERE status='PENDING'").fetchall()
    finally:
        conn.close()
    assert len(pending_apps) == 0

    # 真正指令必须继续 100% 正常工作
    r_comp = handle_inbound_qq(sandbox_db, "算力资产")
    assert r_comp["handled"] is True and "算力" in r_comp["reply"]

    r_sub = handle_inbound_qq(sandbox_db, "订阅体检")
    assert r_sub["handled"] is True and "订阅" in r_sub["reply"]

    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO cases (case_id, title, status, category, created_at, updated_at)
            VALUES ('S710', '在途包裹：延边牛板筋', '待处理', 'parcel', ?, ?)
            """,
            (iso_now(), iso_now()),
        )
    r_cmp = handle_inbound_qq(sandbox_db, "销掉牛板筋、设明天9点提醒我抢号")
    assert r_cmp["handled"] is True
    assert "复合指令统一确认卡" in r_cmp["reply"]
    assert "S710" in r_cmp["reply"]
    assert "2026-11-24 09:00" in r_cmp["reply"]
    assert r_cmp.get("keyboard") is not None


# ===========================================================================
# 3. Morning Brief Realism & Anti-Noise
# ===========================================================================


def test_group3_morning_brief_closed_cases_weekend_sleep_defer_ttl_and_full_reply(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    """验证晨札：已结单不出现、周末不报工作日通勤、补觉延后并在醒后/死线刷新夜间 apply Axx TTL、回「全文」准确返回详版。"""
    _write_two_home_policy(sandbox_db)

    # 3A) 已完成单据绝对不出现在晨札待办；周末晨札绝不提「工作日通勤/上班带伞」
    sat = frozen_clock("2026-11-21T08:20:00+08:00")  # 周六
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO cases (case_id, title, next_step, status, category, created_at, updated_at)
            VALUES
              ('S720', '已完成旧快递单', '已取走', '完成', 'pickup', ?, ?),
              ('S721', '仍待处理的周六事项', '核对账单', '待处理', 'manual', ?, ?)
            """,
            (iso_now(sat), iso_now(sat), iso_now(sat), iso_now(sat)),
        )
    # 模拟 cron 天气输出里混入「工作日通勤 / 上班带伞」文案，周末晨札必须过滤掉！
    cron_root = sandbox_db.paths.tmp_root / "cron_out"
    rep_dir = cron_root / "affd2f6445fe"
    rep_dir.mkdir(parents=True, exist_ok=True)
    (rep_dir / "2026-11-21_08-00-00.md").write_text(
        "---\n🌤️ 北京天气：阴转小雨 4~11°C，工作日通勤建议上班带伞\n",
        encoding="utf-8",
    )
    ed_sat = build_morning_editions(sandbox_db, cron_output_root=cron_root)
    assert "S720" not in ed_sat["concise"] and "已完成旧快递单" not in ed_sat["concise"]
    assert "S721" in ed_sat["concise"]
    assert "休息日" in ed_sat["concise"]
    assert "工作日通勤" not in ed_sat["concise"]
    assert "上班带伞" not in ed_sat["concise"]

    # 3B) 夜猫子补觉保护：08:15 sleep:state == sleep -> 延后投递；夜间暂存含 apply Axx；醒来后冲刷并刷新 TTL！
    mon_0300 = frozen_clock("2026-11-23T03:00:00+08:00")
    set_situation(sandbox_db, "sleep:state", {"state": "sleep"}, source="ios_sleep", as_of=iso_now(mon_0300))
    ap = create_approval(
        sandbox_db,
        action="didi_create_order",
        target_id="NIGHT-1",
        params={"dest": "常用医院"},
        ttl_minutes=15,  # 原本 03:15 就过期
    )
    q_res = send_qq_message(
        sandbox_db,
        f"🚕 夜间暂存预约卡，醒后回复 apply {ap['token']} 发单。",
        ref_id="NIGHT-1",
        rule_id="night_test",
    )
    assert q_res.get("queued_for_morning") is True

    # 08:15 仍在睡 -> 延后，不吵醒！
    frozen_clock("2026-11-23T08:15:00+08:00")
    fl_sleep = flush_morning_brief_if_due(sandbox_db)
    assert fl_sleep is not None and fl_sleep.get("deferred_for_sleep") is True
    assert fl_sleep.get("delivered") is False

    # 10:10 醒来（sleep:state == awake）-> 晨札正式送达，并刷新夜间 apply Axx 口令 30m 有效期！
    mon_1010 = frozen_clock("2026-11-23T10:10:00+08:00")
    set_situation(sandbox_db, "sleep:state", {"state": "awake"}, source="ios_wake", as_of=iso_now(mon_1010))
    fl_awake = flush_morning_brief_if_due(sandbox_db)
    assert fl_awake is not None and fl_awake.get("delivered") is True

    conn = sandbox_db.connect()
    try:
        ap_row = conn.execute("SELECT status, expires_at FROM approvals WHERE token=?", (ap["token"],)).fetchone()
    finally:
        conn.close()
    assert ap_row["status"] == "PENDING"
    assert datetime.fromisoformat(ap_row["expires_at"]) >= mon_1010 + timedelta(minutes=25)

    # 3C) 晨札发出后回复「全文」返回完整详版（包含夜间暂存原文与待办）
    r_full = handle_inbound_qq(sandbox_db, "全文")
    assert r_full["handled"] is True
    assert "司命晨札全文" in r_full["reply"]
    assert ap["token"] in r_full["reply"]
    assert "S721" in r_full["reply"]
    assert "S720" not in r_full["reply"]


def test_group3_morning_brief_flushes_at_latest_deadline_even_if_still_asleep(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    """即使一直没检测到醒来，到达 latest_morning_brief (11:45) 死线时仍会兜底送达晨札。"""
    _write_two_home_policy(sandbox_db)
    mon_0815 = frozen_clock("2026-11-23T08:15:00+08:00")
    set_situation(sandbox_db, "sleep:state", {"state": "sleep"}, source="ios_sleep", as_of=iso_now(mon_0815))
    assert flush_morning_brief_if_due(sandbox_db).get("deferred_for_sleep") is True

    frozen_clock("2026-11-23T11:46:00+08:00")
    fl_late = flush_morning_brief_if_due(sandbox_db)
    assert fl_late is not None and fl_late.get("delivered") is True


# ===========================================================================
# 4. Parcel & Pickup Site Isolation (家 vs 新房) & Duplicate SMS Dedup
# ===========================================================================


def test_group4_site_isolation_duplicate_sms_dedup_and_no_false_cold_chain_merge(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    """示例小区A(家) vs 示例小区B(新房) 严格隔离且批销不误伤新房件；重复/变体品牌取件短信严格去重；普通丰巢绝不误报冷链急件。"""
    _write_two_home_policy(sandbox_db, pickup_batch="18:00")
    frozen_clock("2026-11-23T11:30:00+08:00")

    # 种入一条在途冷链单（无运单号关联）
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO cases (
                case_id, title, next_step, due_at, status, strength, category, rule_id,
                evidence_ids, context_json, created_at, updated_at
            ) VALUES (
                'S740', '🧊在途冷链 · 顺丰冷运榴莲冰品', '⚠️ 冷链生鲜预计今日送达', '2026-11-23',
                '待处理', 'SOFT', 'parcel', 'superagent_parcel', '[]',
                '{"is_cold_chain": true, "eta_date": "2026-11-23", "eta_kind": "deliver"}',
                ?, ?
            )
            """,
            (iso_now(), iso_now()),
        )

    # 1) 示例小区A(家) 丰巢短信到达，且同一包裹以不同品牌变体再次发来催取短信 -> 绝不能建 2 个 case！
    sms_home_1 = "【丰巢】凭取件码884422至示例小区A大堂门口丰巢柜取件，18小时内免费。"
    sms_home_2 = "【丰巢柜】凭取件码884422至示例小区A大堂门口丰巢柜取件，请尽快领取！"
    sms_home_3 = "【中通快递】您的包裹已到达示例小区A大堂门口丰巢柜，取件码884422。"

    r1 = try_ingest_pickup_code(sandbox_db, sms_home_1, source="ios_sms")
    r2 = try_ingest_pickup_code(sandbox_db, sms_home_2, source="ios_sms")
    r3 = try_ingest_pickup_code(sandbox_db, sms_home_3, source="ios_sms")
    assert r1["matched"] is True and r1["site_alias"] == "家"
    assert r2["matched"] is True and r2.get("duplicate") is True
    assert r3["matched"] is True and r3.get("duplicate") is True
    # 普通非冷链丰巢短信绝不得触发冷链急件提醒！
    assert not any("冷链/生鲜包裹到站急件提醒" in r["text"] for r in _outbox_rows(sandbox_db))

    # 2) 示例小区B(新房) 丰巢短信到达（15km 外）
    sms_new = "【丰巢】凭取件码669911至示例小区B示例小区B丰巢柜取件，18小时内免费。"
    r_new = try_ingest_pickup_code(sandbox_db, sms_new, source="ios_sms")
    assert r_new["matched"] is True and r_new["site_alias"] == "新房"

    # 确认数据库里仅建了 2 条 pickup case（家 1 条 + 新房 1 条），绝无重复！
    conn = sandbox_db.connect()
    try:
        pickup_cases = conn.execute(
            "SELECT case_id, title FROM cases WHERE category='pickup' AND status='待处理' ORDER BY case_id"
        ).fetchall()
    finally:
        conn.close()
    assert len(pickup_cases) == 2

    # 3) 18:00 汇总推送清晰隔离 家 vs 新房，且回复「取了」只销账示例小区A(家)包裹、不误销 15km 外的新房包裹！
    res_1800 = flush_pickup_window_if_due(sandbox_db, now=_dt("2026-11-23 18:05"))
    assert res_1800 is not None and res_1800.get("delivered") is True
    msg_id = res_1800["msg_id"]
    sent_txt = _outbox_rows(sandbox_db)[-1]["text"]
    assert "回家顺路取件" in sent_txt
    assert "非顺路 · 新房（示例小区B）" in sent_txt
    assert "示例小区A" in sent_txt and "示例小区B" in sent_txt

    # 引用回复「取了」 -> 只关闭家门口件，新房件必须保持「待处理」！
    r_take = handle_inbound_qq(sandbox_db, "取了", reply_to_msg_id=msg_id)
    assert r_take["handled"] is True
    conn = sandbox_db.connect()
    try:
        st_home = conn.execute("SELECT status FROM cases WHERE case_id=?", (r1["case_id"],)).fetchone()["status"]
        st_new = conn.execute("SELECT status FROM cases WHERE case_id=?", (r_new["case_id"],)).fetchone()["status"]
    finally:
        conn.close()
    assert st_home == "完成"
    assert st_new == "待处理"


# ===========================================================================
# 5. Medical Care (周期用药) Full Lifecycle Expectations
# ===========================================================================


def test_group5_periodic_med_skip_done_refill_and_needle_stock_lifecycle(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    """周期用药全周期：本周跳过不扣针数且不再催、打了扣一针且不再催、refill 2 增 8 针停催挂号、stock needle 20 清除低库存告警。"""
    # 5A) 本周跳过：不扣针数、不继续追催！
    set_doses_left(sandbox_db, 3)
    thu_0055 = frozen_clock("2026-11-19T00:55:00+08:00")
    res_w1 = run_injection_guard(sandbox_db, now=thu_0055)
    assert res_w1.get("created")

    r_skip = handle_inbound_qq(sandbox_db, "本周跳过")
    assert r_skip["handled"] is True and "跳过" in r_skip["reply"]
    assert get_pen_state(sandbox_db)["doses_left"] == 3  # 绝不扣减针数！

    # 当晚清醒继续跑 guard & 次日 23:30 均不得再催！
    nudge_after_skip = run_injection_guard(
        sandbox_db,
        now=_dt("2026-11-19 02:30"),
        desktop_probe=lambda: {"available": True, "locked": False, "idle_sec": 10},
    )
    assert nudge_after_skip.get("skipped") == "already_logged"
    assert run_injection_guard(sandbox_db, now=_dt("2026-11-19 23:35")).get("skipped") == "already_logged"

    # 5B) 下周（11-26 凌晨）：正常「打了」 -> 扣 1 针（3 -> 2），当周不再催
    thu_w2 = frozen_clock("2026-11-26T00:55:00+08:00")
    res_w2 = run_injection_guard(sandbox_db, now=thu_w2)
    assert res_w2.get("created")
    r_done = handle_inbound_qq(sandbox_db, "打了")
    assert r_done["handled"] is True and "还剩 2 针" in r_done["reply"]
    assert get_pen_state(sandbox_db)["doses_left"] == 2
    assert run_injection_guard(sandbox_db, now=_dt("2026-11-26 02:30")).get("skipped") == "already_logged"

    # 5C) 剩 1 针触发挂号提醒后，refill 2 支（+8 针 -> 共 9 针）清掉预约催促！
    set_doses_left(sandbox_db, 1)
    frozen_clock("2026-12-02T10:00:00+08:00")
    res_book = run_injection_guard(sandbox_db, now=_dt("2026-12-02 10:00"))
    assert res_book["hospital"].get("case")

    rf = record_refill(sandbox_db, 2, now=_dt("2026-12-02 14:00"))
    assert rf["ok"] is True and "共 9 针" in rf["summary"]
    assert get_pen_state(sandbox_db)["doses_left"] == 9
    # 挂号提醒单已关闭，不再催挂号
    res_after_refill = run_injection_guard(sandbox_db, now=_dt("2026-12-03 10:00"))
    assert not res_after_refill["hospital"].get("text")

    # 5C2) QQ 口语「补药 1支」（与 spatial_life 提示文案一致）同样生效 +4 针
    r_buyao = handle_inbound_qq(sandbox_db, "补药 1支")
    assert r_buyao["handled"] is True and "共 13 针" in r_buyao["reply"]
    assert get_pen_state(sandbox_db)["doses_left"] == 13

    # 5D) 针头低库存 -> 校准 stock needle 20 -> 立即变 ok 并消除低库存提醒！
    now_s = frozen_clock("2026-12-03T22:40:00+08:00")
    set_supply_stock(sandbox_db, "needle", 1, now=now_s)
    assert next(s for s in estimate_supplies(sandbox_db, now_s) if s["key"] == "needle")["status"] == "low"
    assert "needle" in run_supply_radar(sandbox_db, now=now_s)["pushed"]

    cal = set_supply_stock(sandbox_db, "needle", 20, now=now_s)
    assert cal["ok"] is True and "20支" in cal["summary"]
    assert next(s for s in estimate_supplies(sandbox_db, now_s) if s["key"] == "needle")["status"] == "ok"
    assert "needle" not in run_supply_radar(sandbox_db, now=now_s + timedelta(minutes=5))["pushed"]


# ===========================================================================
# 6. Dual-Shop Combo & Compound Voice Natural Language Realism
# ===========================================================================


def test_group6_dual_shop_single_query_and_compound_voice_relative_dates(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    """单店查询不伪造第二家店且 QQ 回复非空；复合语音相对日期与钟点（下周三、明天下午3点半）精准无误。"""
    base = frozen_clock("2026-11-23T10:00:00+08:00")  # 周一

    # 6A) 单店查询传入 plan_dual_shop_combo / QQ 「双拼点餐」 -> 优雅提示，绝不编造第二家店，绝不返回空回复！
    for single_q in ("示例粉面炒米粉", "和府捞面", "太和板面", "酸辣粉配辣椒"):
        p_single = plan_dual_shop_combo(sandbox_db, single_q)
        assert p_single["ok"] is False
        assert "legs" not in p_single
        assert p_single.get("reply")

    r_qq_single = handle_inbound_qq(sandbox_db, "双拼点餐 示例粉面炒米粉")
    assert r_qq_single["handled"] is True
    assert r_qq_single["reply"] != ""
    assert "示例粉面炒米粉" in r_qq_single["reply"]

    # 真实双店查询 via QQ 也必须返回完整审批卡正文（此前返回空字符串 ""！）
    r_qq_combo = handle_inbound_qq(sandbox_db, "双拼点餐 示例粉面炒米粉 + 示例果切西瓜")
    assert r_qq_combo["handled"] is True
    assert "跨店双拼点餐方案" in r_qq_combo["reply"]
    assert "apply A" in r_qq_combo["reply"]
    assert r_qq_combo.get("keyboard") is not None

    # 「和府捞面 + 瑞幸冰美式」不得把「和府捞面」截断成「府捞面」！
    p_hefu = plan_dual_shop_combo(sandbox_db, "和府捞面 + 瑞幸冰美式")
    assert p_hefu["ok"] is True
    assert p_hefu["legs"][0]["item_name"] == "和府捞面"

    # 6B) 复合语音相对日期与钟点：「下周三抢号」绝不在过去；「明天下午3点半提醒我开会」解析为明天 15:30（不是 09:00！）
    parsed = parse_compound_utterance(sandbox_db, "设下周三抢号、明天下午3点半提醒我开会")
    assert parsed["ok"] is True and parsed["is_compound"] is True
    assert len(parsed["intents"]) == 2

    i_wed, i_meet = parsed["intents"]
    wed_dt = datetime.fromisoformat(i_wed["params"]["fire_at"])
    assert wed_dt > base
    assert wed_dt.weekday() == 2  # Wednesday (2026-12-02)
    assert wed_dt.date() == date(2026, 12, 2)
    assert i_wed["params"]["task"] == "抢号"

    meet_dt = datetime.fromisoformat(i_meet["params"]["fire_at"])
    assert meet_dt.date() == date(2026, 11, 24)
    assert (meet_dt.hour, meet_dt.minute) == (15, 30)
    assert i_meet["params"]["task"] == "开会"

    # 也验证中文数字钟点与「今天下午3点半」
    parsed_cn = parse_compound_utterance(sandbox_db, "今天下午三点半提醒我开会、点份示例粉面送到家")
    assert parsed_cn["is_compound"] is True
    cn_dt = datetime.fromisoformat(parsed_cn["intents"][0]["params"]["fire_at"])
    assert cn_dt.date() == date(2026, 11, 23)
    assert (cn_dt.hour, cn_dt.minute) == (15, 30)


# ===========================================================================
# 7. Weather Commute Guard Realism
# ===========================================================================


def test_group7_weather_commute_guard_silent_sunny_dedup_rain_and_weekend_quiet(
    sandbox_db: SimingDB,
) -> None:
    """晴朗温差小工作日 100% 静默；雨天工作日早间只提醒一次（5 分钟后不重复）；雨天周六绝不发工作日通勤提醒。"""
    _write_two_home_policy(sandbox_db)

    # 7A) 晴朗工作日 + 小温差 -> 100% 静默（0 条推送）
    mon = date(2026, 11, 23)
    sunny = lambda lat, lon: _meteo(mon, today_feel=15.0, y_feel=15.5, night_delta=-2.0)
    r_sun = run_weather_guard(sandbox_db, now=_dt("2026-11-23 08:52"), fetch=sunny)
    assert r_sun["ok"] is True and r_sun["phase"] == "morning" and r_sun["sent"] is False
    assert len(_outbox_rows(sandbox_db)) == 0

    # 7B) 雨天工作日早晨 -> 提醒带伞一次；5 分钟后与临出门均不重复刷屏！
    tue = date(2026, 11, 24)
    rainy_tue = lambda lat, lon: _meteo(tue, today_feel=14.0, y_feel=15.0, rain_hours=(9,), raining_now=True)
    r_rain1 = run_weather_guard(sandbox_db, now=_dt("2026-11-24 08:52"), fetch=rainy_tue)
    assert r_rain1["ok"] is True and r_rain1["phase"] == "morning" and r_rain1["sent"] is True
    assert len(_outbox_rows(sandbox_db)) == 1
    assert "带伞" in _outbox_rows(sandbox_db)[0]["text"]

    r_rain2 = run_weather_guard(sandbox_db, now=_dt("2026-11-24 08:57"), fetch=rainy_tue)
    assert r_rain2["skipped"] == "not_due"
    r_rain3 = run_weather_guard(sandbox_db, now=_dt("2026-11-24 09:11"), fetch=rainy_tue)
    assert r_rain3["skipped"] == "not_due"
    assert len(_outbox_rows(sandbox_db)) == 1

    # 7C) 下雨的周六 -> 休息日直接跳过，绝不发工作日通勤带伞提醒！
    sat = date(2026, 11, 21)
    rainy_sat = lambda lat, lon: _meteo(sat, today_feel=12.0, y_feel=13.0, rain_hours=(9, 10, 22), raining_now=True)
    r_sat = run_weather_guard(sandbox_db, now=_dt("2026-11-21 08:52"), fetch=rainy_sat)
    assert r_sat["skipped"] == "rest_day"
    assert len(_outbox_rows(sandbox_db)) == 1
