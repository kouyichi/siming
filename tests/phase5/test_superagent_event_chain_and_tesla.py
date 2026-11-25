"""Stage N1 + N2 全量集成测试（F01~F12, F14, F15 & Directives #1~#6）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta

from siming.approval import create_approval
from siming.collectors.edge_sync import process_edge_records
from siming.collectors.pickup_codes import try_ingest_pickup_code
from siming.daily_care import run_injection_guard
from siming.db import SHANGHAI_TZ, iso_now
from siming.doctor import run_replay_fixtures
from siming.evald import consume_pending_effects
from siming.event_chain import replay_event_chain_history, seed_deco_and_obligation_chains
from siming.ledger.enrich_priors import learn_amount_priors
from siming.ledger.sms_parser import try_ingest_bank_sms
from siming.remote import drop_item
from siming.router import build_morning_brief, handle_inbound_qq
from siming.spatial_life import evaluate_overtime_meal_ab


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI_TZ)


def _outbox_texts(db) -> list[str]:
    conn = db.connect()
    try:
        return [str(r["text"]) for r in conn.execute("SELECT text FROM outbox ORDER BY created_at ASC").fetchall()]
    finally:
        conn.close()


def test_superagent_triage_bookmark_douyin_parcel_and_ignore(sandbox_db):
    # 1. GitHub 技术灵感截图 -> BOOKMARK（入 bookmarks 表与 bookmarks.md，绝不塞进待办 cases）
    r_gh = drop_item(
        sandbox_db,
        text="10:41 5G\nGitHub - demo/modem-bridge-tool: 光猫桥接与 Telnet 开启工具\n★ 1.2k stars · 路由器拨号配套",
        source_channel="ios_drop",
    )
    assert r_gh["ok"] and r_gh["kind"] == "bookmark"
    conn = sandbox_db.connect()
    try:
        assert conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0] == 0
        bm = conn.execute("SELECT bookmark_id, title, related_projects_json FROM bookmarks").fetchone()
        assert bm is not None and "modem-bridge-tool" in bm["title"]
    finally:
        conn.close()

    q_bm = handle_inbound_qq(sandbox_db, "收藏夹")
    assert q_bm["handled"] and "modem-bridge-tool" in q_bm["reply"]

    sb_res = handle_inbound_qq(sandbox_db, f"试跑 {bm['bookmark_id']}")
    assert sb_res["handled"] and "隔离评测目录" in sb_res["reply"]

    # 2. 抖音多单截图（含冷链冰淇淋 + 常温零食） -> PARCEL 入 shop_order_items(douyin) + 冷链高优单
    douyin_ocr = (
        "抖音商城 全部订单\n"
        "蒙牛官方旗舰店\n运输中\n蒙牛随变冰淇淋生巧流心雪糕冷冻冰品 10支\n实付¥39.90\n"
        "良品铺子旗舰店\n待收货\n猪肉脯靖江风味 200g\n实付¥19.90"
    )
    r_dy = drop_item(sandbox_db, text=douyin_ocr, source_channel="ios_drop")
    assert r_dy["ok"] and r_dy["kind"] == "parcel"
    conn = sandbox_db.connect()
    try:
        dy_items = conn.execute("SELECT item_name FROM shop_order_items WHERE platform='douyin'").fetchall()
        assert len(dy_items) >= 1
        cold_case = conn.execute("SELECT title FROM cases WHERE category='parcel' AND status='待处理'").fetchone()
        assert cold_case is not None and "冷链" in cold_case["title"]
    finally:
        conn.close()

    # 3. Directive #6 & 锁屏/设置页误触截图 -> IGNORE（桌游拍卖群截图与系统设置页均不建单、不收藏）
    r_noise = drop_item(sandbox_db, text="设置 蜂窝网络 屏幕使用时间 电池", source_channel="ios_drop")
    assert r_noise.get("ignored") is True
    r_bg = drop_item(sandbox_db, text="桌游二手拍卖群 截标 22:00 50起拍", source_channel="ios_drop")
    assert r_bg.get("ignored") is True


def test_event_chain_cascade_bundle_and_d19_natural_confirm(sandbox_db):
    seed_deco_and_obligation_chains(sandbox_db)

    # QQ 查询规律白盒
    r_rt = handle_inbound_qq(sandbox_db, "你记了我哪些规律")
    assert r_rt["handled"] and "routines.md" in r_rt["reply"]

    # 燃气通气改到 12.13 -> 级联命中老板烟灶与林内R32F（均需顺延），不误伤科沃斯扫地机，合并为单张 bundle 卡
    r_cas = handle_inbound_qq(sandbox_db, "燃气通气改到 12.13")
    assert r_cas["handled"]
    assert "烟灶" in r_cas["reply"] and "热水器" in r_cas["reply"]
    assert "不受影响：上下水扫拖机器人" in r_cas["reply"]

    # D-19：单张活跃 bundle 确认卡时，直接回单字「确认」纯代码一键执行整包顺延！
    r_ok = handle_inbound_qq(sandbox_db, "确认")
    assert r_ok["handled"] and "已一键执行整包确认" in r_ok["reply"]

    conn = sandbox_db.connect()
    try:
        robam = conn.execute("SELECT target_at FROM expectations WHERE exp_id='E-INST-ROBAM'").fetchone()
        ecovacs = conn.execute("SELECT target_at FROM expectations WHERE exp_id='E-INST-ECOVACS'").fetchone()
        assert robam["target_at"].startswith("2026-12-13")
        assert ecovacs["target_at"].startswith("2026-12-10")
    finally:
        conn.close()

    # D-19 防呆：同时存在 2 张独立未捆绑确认卡且未引用回复时，回「确认」必须拒绝盲猜并列出编号
    ap1 = create_approval(sandbox_db, "meituan_lock_cart", "MT-1", {"shop": "A"})
    ap2 = create_approval(sandbox_db, "luckin_mcp_order", "LK-1", {"spec": "B"})
    r_amb = handle_inbound_qq(sandbox_db, "确认")
    assert r_amb["handled"] and ap1["token"] in r_amb["reply"] and ap2["token"] in r_amb["reply"]

    # 历史回放器与沙盒 replay event-chain 夹具
    rep30 = replay_event_chain_history(sandbox_db, days=30)
    assert rep30["ok"]
    fix_res = run_replay_fixtures(sandbox_db.paths, fixture="event-chain")
    assert fix_res["ok"]


def test_awake_escalating_injection_nudge_and_home_gate(sandbox_db):
    handle_inbound_qq(sandbox_db, "周期用药还剩 4 针")
    # 将人类最近活跃时间锚定在 00:50，避免真实测试钟点干扰模拟时间线
    t0 = _dt("2026-11-19 00:52")
    sandbox_db.set_meta("last_human_active_at", iso_now(_dt("2026-11-19 00:50")))
    with sandbox_db.transaction() as c:
        c.execute("DELETE FROM feature_usage")

    # 1. 仍在公司加班时：暂缓首推，等回家再催
    sandbox_db.set_meta(
        "latest_ios_location",
        json.dumps({"place_alias": "公司", "updated_at": iso_now(t0)}, ensure_ascii=False),
    )
    res_office = run_injection_guard(sandbox_db, now=t0)
    assert res_office.get("deferred") == "not_at_home_yet"

    # 2. 回到家后：立即发出首条提醒
    sandbox_db.set_meta(
        "latest_ios_location",
        json.dumps({"place_alias": "家", "updated_at": iso_now(t0)}, ensure_ascii=False),
    )
    res_home = run_injection_guard(sandbox_db, now=_dt("2026-11-19 00:55"))
    assert "created" in res_home

    # 3. 凌晨 02:05 仍醒着写代码（AWAKE_ACTIVE）且未回「打了」 -> 满 60m 自动阶梯重催！
    awake_probe = lambda: {"available": True, "locked": False, "idle_sec": 12.0}
    res_nudge1 = run_injection_guard(sandbox_db, now=_dt("2026-11-19 02:05"), desktop_probe=awake_probe)
    assert res_nudge1.get("awake_nudged") == 1
    assert any("看你还没睡" in m for m in _outbox_texts(sandbox_db))

    # 4. 凌晨 03:15 锁屏入睡（ASLEEP） -> 立即静默停催，绝不吵醒！
    asleep_probe = lambda: {"available": True, "locked": True, "idle_sec": 3600.0}
    res_sleep = run_injection_guard(sandbox_db, now=_dt("2026-11-19 03:15"), desktop_probe=asleep_probe)
    assert "awake_nudged" not in res_sleep


def test_tesla_dual_track_low_soc_and_outing_replan(sandbox_db):
    # 1. iPhone 断开车载蓝牙上报 /siming/in/tesla：19% 未插枪 -> 更新 situation 并推送低电量提醒
    process_edge_records(
        sandbox_db,
        [
            {
                "source": "ios_tesla",
                "soc": 19,
                "range_km": 82,
                "charging_state": "Disconnected",
                "lat": 39.9042,
                "lon": 116.4074,
            }
        ],
    )
    consume_pending_effects(sandbox_db, include_workers=False)
    assert any("低电量提醒" in m for m in _outbox_texts(sandbox_db))

    q_ts = handle_inbound_qq(sandbox_db, "特斯拉")
    assert q_ts["handled"] and "19" in q_ts["reply"] and "82" in q_ts["reply"]

    # 2. 外出约局评估：往返安全电量与实时 ETA 参谋
    now_hm = (datetime.now(tz=SHANGHAI_TZ) + timedelta(minutes=8)).strftime("%H:%M")
    r_out = handle_inbound_qq(sandbox_db, f"开车去 新房（示例小区B） {now_hm}到")
    assert r_out["handled"] and ("Model Y" in r_out["reply"] or "续航" in r_out["reply"])

    # 3. Tesla App 轻点背面截图直提电量 + OwnTracks 行车里程动态扣减估算
    r_shot = drop_item(sandbox_db, text="Model Y\n已驻车 · 已锁车\n82%\n356 km\n哨兵模式开启", source_channel="ios_drop")
    assert r_shot["ok"] and r_shot["kind"] == "tesla_state" and "82" in r_shot["summary"]

    from siming.tesla_link import deduct_tesla_soc_from_driving_km

    est = deduct_tesla_soc_from_driving_km(sandbox_db, 17.0)
    assert est is not None and est["soc_pct"] < 82.0 and est["estimated"] is True


def test_douyin_990_empirical_prior_enrichment(sandbox_db):
    # 写入历史 12 笔 ¥9.90 抖音小程序实锤记录，验证无监督先验学习与无商户银行短信透明打注
    with sandbox_db.transaction() as conn:
        for i in range(12):
            conn.execute(
                """
                INSERT INTO ledger_txns (
                    txn_id, occurred_at, account_alias, card_last4, amount_cents, currency,
                    kind, merchant, status, notes, created_at, updated_at
                ) VALUES (?, ?, '建设银行储蓄卡', '5222', 990, 'CNY', '消费', '抖音小程序', 'MATCHED', '', ?, ?)
                """,
                (f"TX-HIST-{i}", f"2026-10-{10+i:02d}T21:00:00+08:00", "2026-10-25T00:00:00+08:00", "2026-10-25T00:00:00+08:00"),
            )

    priors = learn_amount_priors(sandbox_db)
    assert "amt:990" in priors and "抖音小说/短剧小程序" in priors["amt:990"]["category"]

    # 新来一条只有金额、没有商户名的建行 ¥9.90 消费短信 -> 自动透明打注且保留 merchant=''
    ing = try_ingest_bank_sms(
        sandbox_db,
        "【建设银行】您尾号5222的储蓄卡11月19日03:15消费支出人民币9.90元，活期余额1234.56元。",
    )
    assert ing["matched"]
    conn = sandbox_db.connect()
    try:
        row = conn.execute("SELECT merchant, notes FROM ledger_txns WHERE txn_id=?", (ing["txn_id"],)).fetchone()
        assert row["merchant"] == ""
        assert "抖音小说/短剧小程序" in row["notes"]
    finally:
        conn.close()

    q_nv = handle_inbound_qq(sandbox_db, "小说消费")
    assert q_nv["handled"] and "9.90" in q_nv["reply"]

    # 验证抖音网页收藏夹技术灵感提取器与抖音钱包导出 CSV 对账器
    from siming.collectors.douyin_orders import (
        extract_tech_bookmarks_from_douyin_favorites,
        ingest_douyin_bill_csv_text,
    )

    favs = extract_tech_bookmarks_from_douyin_favorites("763\n网络与智能家居：让万物皆智能 既要有线智能的稳定也要无线智能的生态 #全屋智能 #米家\n108\nGitHub 今日榜 5 个宝藏工具")
    assert len(favs) == 2
    csv_res = ingest_douyin_bill_csv_text(
        sandbox_db,
        "交易时间,商品说明,收/支,金额\n2026-11-19 03:15:00,点众短篇小说解锁,支出,9.90\n",
    )
    assert csv_res["ok"] and csv_res["ingested"] == 1


def test_spatial_pickup_overtime_meal_hospital_curiosity_and_night_lab(sandbox_db):
    # 1. F04：冷链生鲜快递短信即刻防化冻高优提醒 + 下班离开公司顺路弹取件码
    try_ingest_pickup_code(sandbox_db, "【顺丰速运】您的生鲜冷冻包裹已放入示例小区A丰巢柜，取件码881234。")
    consume_pending_effects(sandbox_db, include_workers=False)
    assert any("冷链/生鲜包裹到站急件提醒" in m for m in _outbox_texts(sandbox_db))

    with sandbox_db.transaction() as conn:
        sandbox_db.append_event(
            conn,
            source="ios_loc",
            thread_key="loc:me",
            summary="离场: 公司",
            payload={"event": "离场: 公司", "place_alias": "公司"},
            occurred_at=iso_now(_dt("2026-11-19 21:30")),
        )
    from siming.spatial_life import check_and_fire_offwork_pickup

    p_res = check_and_fire_offwork_pickup(sandbox_db, now=_dt("2026-11-19 21:32"))
    consume_pending_effects(sandbox_db, include_workers=False)
    assert p_res["ok"] and p_res.get("triggered") is True

    # 2. F05：加班晚餐 A（19:40 静默预备，问『吃什么』0ms 命中缓存）+ B（20:08 仍在公司轻推点餐卡）
    sandbox_db.set_meta(
        "latest_ios_location",
        json.dumps({"place_alias": "公司", "event": "到场: 公司", "updated_at": iso_now(_dt("2026-11-19 19:38"))}, ensure_ascii=False),
    )
    a_res = evaluate_overtime_meal_ab(sandbox_db, now=_dt("2026-11-19 19:40"))
    assert a_res["ok"]
    q_eat = handle_inbound_qq(sandbox_db, "吃什么")
    assert q_eat["handled"]

    b_res = evaluate_overtime_meal_ab(sandbox_db, now=_dt("2026-11-19 20:08"))
    consume_pending_effects(sandbox_db, include_workers=False)
    assert b_res["ok"]

    # 3. F06：常用医院预约联动前晚 20:00 抽血单挂起
    r_hosp = handle_inbound_qq(sandbox_db, "我挂了常用医院 2026-12-03 内分泌复诊")
    assert r_hosp["handled"] and "20:00" in r_hosp["reply"]

    # 4. Directive #3：未知坐标驻留满 40m 主动轻量提问 + 『记住地点』一键建档
    process_edge_records(
        sandbox_db,
        [
            {"source": "ios_loc", "lat": 39.99120, "lon": 116.36100, "received_at": "2026-11-19T14:00:00+08:00"},
            {"source": "ios_loc", "lat": 39.99125, "lon": 116.36105, "received_at": "2026-11-19T14:45:00+08:00"},
        ],
    )
    consume_pending_effects(sandbox_db, include_workers=False)
    assert any("停留" in m or "记住地点" in m for m in _outbox_texts(sandbox_db))
    r_save = handle_inbound_qq(sandbox_db, "记住地点 羽毛球馆")
    assert r_save["handled"] and "羽毛球馆" in r_save["reply"]

    # 5. F10 + F12 + F14 + F15：睡前盯实验 -> 晨札战报 + 钥匙检查 + 桌游柜容量 + HAOS 状态
    r_watch = handle_inbound_qq(sandbox_db, "睡前盯 personas对比评测")
    assert r_watch["handled"] and "已接手睡前实验托管" in r_watch["reply"]
    brief = build_morning_brief(sandbox_db, full=True)
    assert "personas对比评测" in brief

    assert handle_inbound_qq(sandbox_db, "钥匙检查")["handled"]
    assert handle_inbound_qq(sandbox_db, "桌游柜")["handled"]
    assert handle_inbound_qq(sandbox_db, "haos")["handled"]


def test_offwork_pickup_respects_custom_radius_handles_direct_transitions_and_doorstep_sms(sandbox_db):
    """回归 2026-11-19 下班漏提醒复盘：
    1. custom_geo_places_json 中『车库』(radius_m=120) 不能按硬编码 180m 吞掉 141m 外的示例小区A↔公司步行中点；
    2. 即使公司直接跳到车库/家或 OwnTracks 发 leave 围栏事件，17:00 后离司都能触发顺路取件；
    3. 『【京东快递】快递尾号48171已放在家门口』这类无取件码送达短信也能建 pickup 待办并合并推送；
    4. 到家久坐 >10 分钟后不会再补发过期的『刚离开公司』提醒。
    """
    from siming.context_loc import ingest_ios_location
    from siming.router import flush_pickup_window_if_due
    from siming.spatial_life import check_and_fire_offwork_pickup

    # 1) 写入真实示例小区A/公司锚点与自定义车库(radius_m=120)
    (sandbox_db.paths.config_dir / "policy.yaml").write_text(
        """quiet_hours:
  start: '23:59'
  end: '00:00'
windows:
  morning_brief: '08:15'
  pickup_batch: '00:00'
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
    sandbox_db.set_meta(
        "custom_geo_places_json",
        json.dumps({"车库": {"lat": 39.9065, "lng": 116.409500, "radius_m": 120.0}}, ensure_ascii=False),
    )

    # 2) 收录无取件码的京东放家门口短信（不应被忽略到 sms:notice），营销/服务预约短信不误判
    assert not try_ingest_pickup_code(sandbox_db, "【京东】家电特邀：购买扫地机器人享补贴")["matched"]
    assert not try_ingest_pickup_code(sandbox_db, "【京东服务】您好，工程师预计2026-12-10上门安装")["matched"]
    door_res = try_ingest_pickup_code(
        sandbox_db,
        "【京东快递】快递尾号48171已放在家门口，快递员电话 13600136000，查看签收照片或反馈异常 3.cn/3-cMvKcs",
    )
    assert door_res["matched"] and door_res["kind"] == "doorstep_pickup"
    assert door_res["station"] == "家门口" and door_res["tail_no"] == "48171"

    # 3) 模拟 2026-11-19 真实轨迹：18:06 在公司 -> 22:51 走在距车库 141.1m 的回家步行线上
    r_off = ingest_ios_location(
        sandbox_db,
        json.dumps({"lat": 39.907950, "lng": 116.406950}),
        received_at="2026-11-19T18:06:32+08:00",
    )
    assert r_off["place_alias"] == "公司"

    r_walk = ingest_ios_location(
        sandbox_db,
        json.dumps({"lat": 39.906100, "lng": 116.407200, "vel": 5}),
        received_at="2026-11-19T22:51:27+08:00",
    )
    # 距离车库 141.1m > 120m，不得误判为车库，而应识别为离场: 公司
    assert r_walk["place_alias"] == ""

    p_res = check_and_fire_offwork_pickup(sandbox_db, now=_dt("2026-11-19 22:52"))
    assert p_res["ok"] and p_res.get("triggered") is True
    assert "48171" in p_res["text"] and "家门口" in p_res["text"]

    # 4) 验证公司直接跳到家（跳过中途点）也能触发，以及到家久坐 >10m 后不补发过期提醒
    sandbox_db.set_meta("last_spatial_pickup_day", "")
    with sandbox_db.transaction() as conn:
        conn.execute("DELETE FROM events WHERE source='ios_loc'")
    sandbox_db.set_meta("last_logged_ios_loc", "")

    ingest_ios_location(sandbox_db, json.dumps({"lat": 39.908000, "lng": 116.407000}), received_at="2026-11-20T18:10:00+08:00")
    ingest_ios_location(sandbox_db, json.dumps({"lat": 39.904220, "lng": 116.407440, "vel": 4}), received_at="2026-11-20T22:54:00+08:00")

    # 刚到家楼下（1 分钟后）仍应提醒
    p_home_fresh = check_and_fire_offwork_pickup(sandbox_db, now=_dt("2026-11-20 22:55"))
    assert p_home_fresh["ok"] and p_home_fresh.get("triggered") is True

    # 到家已过 25 分钟后不应再误报『刚离开公司下班』
    sandbox_db.set_meta("last_spatial_pickup_day", "")
    p_home_stale = check_and_fire_offwork_pickup(sandbox_db, now=_dt("2026-11-20 23:20"))
    assert p_home_stale["ok"] and p_home_stale.get("triggered") is False and p_home_stale.get("skipped") == "already_home_settled"

    # 5) 验证 OwnTracks 离开公司围栏 (event=leave) 在工作日能触发，且周末非工作日与居家日出门绝不误触
    sandbox_db.set_meta("last_spatial_pickup_day", "")
    with sandbox_db.transaction() as conn:
        conn.execute("DELETE FROM events WHERE source='ios_loc'")
    sandbox_db.set_meta("last_logged_ios_loc", "")

    # 2026-11-21 为周六非工作日：即使有离司事件也必须跳过，不发下班取件提醒
    ingest_ios_location(sandbox_db, json.dumps({"lat": 39.908000, "lng": 116.407000}), received_at="2026-11-21T17:30:00+08:00")
    ingest_ios_location(sandbox_db, json.dumps({"event": "leave", "lat": 39.906200, "lng": 116.407200, "vel": 4}), received_at="2026-11-21T19:05:00+08:00")
    p_sat = check_and_fire_offwork_pickup(sandbox_db, now=_dt("2026-11-21 19:06"))
    assert p_sat["ok"] and p_sat.get("triggered") is False and p_sat.get("skipped") == "non_workday"

    # 2026-11-24 为周二工作日：OwnTracks leave 离开公司围栏正常触发
    sandbox_db.set_meta("last_spatial_pickup_day", "")
    with sandbox_db.transaction() as conn:
        conn.execute("DELETE FROM events WHERE source='ios_loc'")
    sandbox_db.set_meta("last_logged_ios_loc", "")

    ingest_ios_location(sandbox_db, json.dumps({"lat": 39.908000, "lng": 116.407000}), received_at="2026-11-24T17:30:00+08:00")
    ingest_ios_location(sandbox_db, json.dumps({"event": "leave", "lat": 39.906200, "lng": 116.407200, "vel": 4}), received_at="2026-11-24T19:05:00+08:00")
    p_ot = check_and_fire_offwork_pickup(sandbox_db, now=_dt("2026-11-24 19:06"))
    assert p_ot["ok"] and p_ot.get("triggered") is True

    # 全天在家未去公司（2026-11-22 周日 / 工作日居家）-> 晚上离家外出绝不误触发离司取件
    sandbox_db.set_meta("last_spatial_pickup_day", "")
    with sandbox_db.transaction() as conn:
        conn.execute("DELETE FROM events WHERE source='ios_loc'")
    sandbox_db.set_meta("last_logged_ios_loc", "")

    ingest_ios_location(sandbox_db, json.dumps({"lat": 39.904200, "lng": 116.407400}), received_at="2026-11-22T10:00:00+08:00")
    ingest_ios_location(sandbox_db, json.dumps({"lat": 39.906100, "lng": 116.407200, "vel": 5}), received_at="2026-11-22T19:30:00+08:00")
    p_wfh = check_and_fire_offwork_pickup(sandbox_db, now=_dt("2026-11-22 19:31"))
    assert p_wfh["ok"] and p_wfh.get("triggered") is False and p_wfh.get("skipped") == "non_workday"

    # 6) 验证 OwnTracks 自定义 Region 名称映射（如『公司』->『公司』）：
    #    即使 leave 发生时距公司中心 95m（< 160m GPS 锚点半径），也不得被反吸回『公司』，应立即触发离司取件！
    from siming.context_loc import bind_region_to_place

    bind_region_to_place(sandbox_db, "公司", "公司")
    sandbox_db.set_meta("last_spatial_pickup_day", "")
    with sandbox_db.transaction() as conn:
        conn.execute("DELETE FROM events WHERE source='ios_loc'")
    sandbox_db.set_meta("last_logged_ios_loc", "")

    r_in = ingest_ios_location(
        sandbox_db,
        json.dumps({"_type": "location", "lat": 39.90798, "lon": 116.40700, "inregions": ["公司"]}),
        received_at="2026-11-23T18:00:00+08:00",
    )
    assert r_in["place_alias"] == "公司"

    r_lv = ingest_ios_location(
        sandbox_db,
        json.dumps({"_type": "transition", "event": "leave", "desc": "公司", "lat": 39.90715, "lon": 116.40710, "vel": 4}),
        received_at="2026-11-23T21:10:00+08:00",
    )
    assert r_lv["place_alias"] == ""
    p_reg = check_and_fire_offwork_pickup(sandbox_db, now=_dt("2026-11-23 21:11"))
    assert p_reg["ok"] and p_reg.get("triggered") is True

    # 7) 18:00 窗口格式化门口快递不出现难看的『取件码 `尾号48171`』
    sandbox_db.set_meta("last_spatial_pickup_day", "")
    flushed = flush_pickup_window_if_due(sandbox_db, now=_dt("2026-11-25 18:05"))
    assert flushed is not None and flushed.get("ok")
    assert any("家门口" in m and "尾号`48171`" in m and "取件码 `尾号48171`" not in m for m in _outbox_texts(sandbox_db))
