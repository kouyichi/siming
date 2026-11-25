"""Siming v5 Wave 2 测试套件：覆盖 W2-1 ~ W2-6（放甲醛自治、家庭内网与三地灾备、拓竹 3D 打印副驾、夜猫子全屋智能、60m 通勤中枢、家电住宅护照）。"""

from __future__ import annotations

import math
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from siming.bambu_copilot import (
    consume_filament,
    format_bambu_report,
    update_print_job,
    upsert_spool,
)
from siming.commute_hub import (
    check_regime_transition,
    compare_commute_modes,
    format_commute_advice,
    stage_commute_rescue_bundle,
)
from siming.db import SHANGHAI_TZ
from siming.event_chain import get_situation
from siming.haos_night_owl import (
    evaluate_night_owl_haos_rules,
    format_haos_report,
    is_vacuum_locked,
    sync_night_owl_to_haos,
)
from siming.home_air_guard import (
    format_home_air_report,
    ingest_air_reading,
    predict_safe_movein_date,
    recommend_ventilation_strategy,
)
from siming.home_passport import (
    check_due_consumables,
    check_due_utilities,
    compute_appliance_asset_valuation,
    format_home_passport_report,
    lookup_fault_code,
    upsert_consumable,
    upsert_utility,
)
from siming.homelab_ops import (
    build_three_site_backup_plan,
    evaluate_homelab_health,
    format_homelab_report,
    get_modem_bridge_playbook,
    record_homelab_node,
    verify_backup_manifest,
)


# ---------------------------------------------------------------------------
# W2-1: 放甲醛远程自治 (home_air_guard)
# ---------------------------------------------------------------------------
def test_w2_1_formaldehyde_decay_and_ventilation_strategies(sandbox_db, frozen_clock):
    frozen_clock("2027-01-15T12:30:00+08:00")
    # 构造精确指数衰减序列：C(t) = 0.32 * exp(-ln(2)/30 * t)，半衰期 30 天
    # t=0 (2026-12-16): 0.32 -> t=15: 0.2263 -> t=30 (2027-01-15): 0.16
    # 降到 0.08 mg/m³ 需从 t=30 再过 30 天 -> 2027-02-14
    t0 = datetime(2026, 12, 16, 12, 0, tzinfo=SHANGHAI_TZ)
    k = math.log(2.0) / 30.0
    for day in (0, 10, 20, 30):
        ts = (t0 + timedelta(days=day)).isoformat(timespec="seconds")
        hcho = 0.32 * math.exp(-k * day)
        res = ingest_air_reading(
            sandbox_db,
            room="客厅",
            temp_c=24.0,
            humidity_pct=48.0,
            hcho_mg_m3=hcho,
            voc_ppb=380.0,
            outdoor_aqi=45,
            recorded_at=ts,
            notify=False,
        )
        assert res["ok"] is True
        assert res["l3_triggered"] is False

    pred = predict_safe_movein_date(sandbox_db, safe_threshold=0.08, now="2027-01-15T12:30:00+08:00")
    assert pred["ok"] is True
    assert pred["enough_data"] is True
    assert pred["already_safe"] is False
    assert abs(pred["half_life_days"] - 30.0) < 0.2
    assert pred["predicted_safe_date"] == "2027-02-14"
    assert pred["on_track_for_movein"] is True
    assert pred["r_squared"] > 0.999

    # 12:30 午间高温（24°C）+ 高甲醛（0.16）+ 优良室外 AQI -> 闷放 (bake_offgas)
    strat_bake = recommend_ventilation_strategy(sandbox_db, now="2027-01-15T12:30:00+08:00")
    assert strat_bake["mode"] == "bake_offgas"
    assert strat_bake["haos_action"]["domain"] == "fan"

    # 16:00 排风窗口 + 室外 AQI=45 -> 强排 (strong_purge)
    strat_purge = recommend_ventilation_strategy(sandbox_db, now="2027-01-15T16:00:00+08:00")
    assert strat_purge["mode"] == "strong_purge"

    # 室外重度雾霾 AQI=165 -> 内循环过滤 (recirculate_filter)
    ingest_air_reading(
        sandbox_db,
        room="客厅",
        temp_c=23.0,
        hcho_mg_m3=0.14,
        outdoor_aqi=165,
        recorded_at="2027-01-15T16:30:00+08:00",
        notify=False,
    )
    strat_smog = recommend_ventilation_strategy(sandbox_db, now="2027-01-15T16:35:00+08:00")
    assert strat_smog["mode"] == "recirculate_filter"

    # 甲醛达标（0.05）且室外良好 -> 节能待机 (eco_standby)
    ingest_air_reading(
        sandbox_db,
        room="客厅",
        temp_c=21.0,
        hcho_mg_m3=0.05,
        voc_ppb=150.0,
        outdoor_aqi=35,
        recorded_at="2027-02-20T16:00:00+08:00",
        notify=False,
    )
    strat_eco = recommend_ventilation_strategy(sandbox_db, now="2027-02-20T16:05:00+08:00")
    assert strat_eco["mode"] == "eco_standby"

    report = format_home_air_report(sandbox_db, now="2027-02-20T16:05:00+08:00")
    assert "新房（示例小区B）放甲醛远程自治报告" in report
    assert "水/气/电正常" in report


def test_w2_1_l3_emergency_water_gas_power_breakthrough(sandbox_db, frozen_clock):
    # 凌晨 03:15 静默时段，浸水 + 燃气泄漏 + 断电必须触发 L3 破静告警
    frozen_clock("2027-01-20T03:15:00+08:00")
    res = ingest_air_reading(
        sandbox_db,
        room="厨房",
        temp_c=19.5,
        hcho_mg_m3=0.11,
        water_leak=True,
        gas_leak=True,
        power_ok=False,
        notify=True,
    )
    assert res["l3_triggered"] is True
    kinds = {e["kind"] for e in res["emergencies"]}
    assert kinds == {"water_leak", "gas_leak", "power_outage"}
    assert "L3 紧急破静" in res["alert_text"]
    assert res["push_result"] is not None
    assert res["push_result"]["level"] == "now"

    # 验证事件已落库且 outbox 已入队（dry_run 下 SENT）
    conn = sandbox_db.connect()
    try:
        ev = conn.execute("SELECT * FROM events WHERE source = 'home_air_guard'").fetchone()
        ob = conn.execute("SELECT * FROM outbox WHERE rule_id = 'home_air_emergency'").fetchone()
    finally:
        conn.close()
    assert ev is not None
    assert ob is not None
    assert "燃气泄漏" in ob["text"]


# ---------------------------------------------------------------------------
# W2-2: 家庭内网与三地灾备 (homelab_ops + NAS docker-compose)
# ---------------------------------------------------------------------------
def test_w2_2_nas_docker_compose_valid_and_safe():
    compose_path = Path(__file__).resolve().parents[2] / "deploy" / "nas" / "docker-compose.nas.yml"
    assert compose_path.is_file()
    doc = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = doc["services"]
    for svc in ("sure-db", "sure-redis", "sure-web", "sure-worker", "homeassistant", "sure-hass", "backup-sidecar"):
        assert svc in services, f"missing service {svc}"

    # 确认没有暴露裸 0.0.0.0 端口，均使用 LAN/Tailscale 绑定变量
    for svc_name, cfg in services.items():
        for p in cfg.get("ports") or []:
            assert str(p).startswith("${NAS_BIND_IP:-127.0.0.1}:"), f"{svc_name} port must bind safely: {p}"
    assert "we-promise/sure" in services["sure-web"]["image"]
    assert "sidekiq" in " ".join(services["sure-worker"]["command"])


def test_w2_2_homelab_smart_alerts_and_three_site_backup(sandbox_db, frozen_clock):
    frozen_clock("2027-02-01T10:00:00+08:00")

    record_homelab_node(
        sandbox_db,
        node_id="rb5009",
        kind="router",
        name="MikroTik RB5009",
        status="OK",
        metrics={"online": True, "wan_up": True, "cpu_pct": 12},
    )
    record_homelab_node(
        sandbox_db,
        node_id="nas-homelab",
        kind="nas",
        name="NAS",
        status="OK",
        metrics={
            "online": True,
            "smart_bad_sectors": 3,
            "disk_temp_c": 54.0,
            "raid_status": "DEGRADED",
            "sure_healthy": False,
        },
    )
    record_homelab_node(
        sandbox_db,
        node_id="bkp-3site",
        kind="backup",
        name="三地加密增量备份",
        status="OK",
        metrics={"last_backup_at": "2027-01-29T08:00:00+08:00"},  # > 48h stale
    )

    health = evaluate_homelab_health(sandbox_db, now="2027-02-01T10:00:00+08:00", notify=True)
    assert health["overall_status"] == "ERROR"
    codes = {a["code"] for a in health["alerts"]}
    assert "smart_bad_sectors" in codes
    assert "disk_overheat" in codes
    assert "raid_degraded" in codes
    assert "sure_container_unhealthy" in codes
    assert "backup_stale" in codes
    assert health["push_result"] is not None

    # 三地加密增量备份清单生成 + SHA-256 校验 + 篡改检出
    sample_a = sandbox_db.paths.tmp_root / "vault-note.md"
    sample_b = sandbox_db.paths.tmp_root / "siming-snapshot.json"
    sample_a.write_text("# 示例小区B网络拓扑\nRouter -> NAS\n", encoding="utf-8")
    sample_b.write_text('{"ok": true, "version": 5}\n', encoding="utf-8")

    plan = build_three_site_backup_plan(sandbox_db, sources=[sample_a, sample_b])
    assert plan["ok"] is True
    assert plan["topology"] == ["desktop", "ugreen_nas", "vps_81"]
    assert len(plan["manifest"]["files"]) == 2
    assert len(plan["commands"]) == 3

    ver_ok = verify_backup_manifest(sandbox_db, plan["manifest"])
    assert ver_ok["verified"] is True
    assert ver_ok["manifest_digest_ok"] is True
    assert ver_ok["matched_files"] == 2

    # 篡改文件后验证必能捕获不匹配
    sample_a.write_text("CORRUPTED DATA", encoding="utf-8")
    ver_bad = verify_backup_manifest(sandbox_db, plan["manifest"])
    assert ver_bad["verified"] is False
    assert len(ver_bad["mismatches"]) == 1

    playbook = get_modem_bridge_playbook()
    assert "modem-bridge-tool" in playbook
    assert "RB5009" in playbook
    report = format_homelab_report(sandbox_db)
    assert "NAS" in report


# ---------------------------------------------------------------------------
# W2-3: 3D 打印车间副驾 (bambu_copilot)
# ---------------------------------------------------------------------------
def test_w2_3_bambu_job_completion_anomaly_and_spool_restock(sandbox_db, frozen_clock):
    frozen_clock("2027-02-10T21:00:00+08:00")

    sp = upsert_spool(
        sandbox_db,
        spool_id="spool-pla-white-01",
        material="PLA",
        color="哑光白",
        brand="Bambu",
        initial_grams=1000.0,
        remaining_grams=300.0,
        drybox_humidity_pct=28.0,
        low_threshold_grams=180.0,
        jd_url="https://item.jd.com/100099999999.html",
    )
    assert sp["low_stock"] is False

    # 1) 异常炒面告警：RUNNING + spaghetti -> 自动转 PAUSED 并推送紧急告警
    anom_res = update_print_job(
        sandbox_db,
        job_id="job-bg-insert-01",
        name="桌游收纳盒_底托",
        status="RUNNING",
        progress_pct=42.0,
        spool_id="spool-pla-white-01",
        snapshot_path=str(sandbox_db.paths.tmp_root / "printer-spaghetti.jpg"),
        anomaly="spaghetti",
        notify=True,
    )
    assert anom_res["status"] == "PAUSED"
    assert anom_res["pause_triggered"] is True
    assert anom_res["alert_sent"] is True
    assert anom_res["alert_level"] == "now"
    assert "炒面" in anom_res["alert_text"]

    # 2) 重新打印完工并消耗 160g（300 -> 140 <= 180），触发低库存 A2 补货卡 + 完工带快照通知
    done_res = update_print_job(
        sandbox_db,
        job_id="job-bg-insert-02",
        name="桌游收纳盒_顶盖",
        status="DONE",
        progress_pct=100.0,
        spool_id="spool-pla-white-01",
        used_grams=160.0,
        snapshot_path=str(sandbox_db.paths.tmp_root / "printer-done.jpg"),
        notify=True,
    )
    assert done_res["status"] == "DONE"
    assert done_res["alert_sent"] is True
    assert "printer-done.jpg" in done_res["alert_text"]
    assert done_res["filament_update"] is not None
    assert done_res["filament_update"]["remaining_grams"] == 140.0
    assert done_res["filament_update"]["low_stock"] is True
    ap = done_res["filament_update"]["restock_approval"]
    assert ap is not None
    assert ap["action"] == "jd_replenish_spool"
    assert ap["target_id"] == "spool-pla-white-01"

    # 3) 湿度升高到 46% (>40%) 触发防潮告警，且不重复创建同料盘 PENDING 审批卡
    hum_res = consume_filament(sandbox_db, "spool-pla-white-01", grams=10.0, drybox_humidity_pct=46.0, stage_restock=True)
    assert hum_res["humidity_alert"] is True
    assert hum_res["remaining_grams"] == 130.0
    assert hum_res["restock_approval"]["token"] == ap["token"]

    report = format_bambu_report(sandbox_db)
    assert "桌游收纳盒_顶盖" in report
    assert "低库存" in report
    assert "高湿" in report


# ---------------------------------------------------------------------------
# W2-4: 懂夜猫子的全屋智能 (haos_night_owl)
# ---------------------------------------------------------------------------
def test_w2_4_haos_night_owl_rules_and_sync(sandbox_db, frozen_clock):
    # 场景 A: 上午 09:30 补觉 -> 扫地机必须硬锁
    lock_check = is_vacuum_locked(sandbox_db, now="2027-05-05T09:30:00+08:00", sleep_state="sleep")
    assert lock_check["vacuum_locked"] is True

    calls: list[dict] = []

    def _fake_haos(action: dict) -> dict:
        calls.append(action)
        return {"called": True, "entity_id": action["entity_id"]}

    res_sleep = sync_night_owl_to_haos(
        sandbox_db,
        now="2027-05-05T09:30:00+08:00",
        sleep_state="sleep",
        location_state="home",
        haos_call_fn=_fake_haos,
    )
    assert res_sleep["vacuum_locked"] is True
    assert res_sleep["auto_clean_released"] is False
    assert any(a["rule"] == "sleep_vacuum_hard_lock" for a in res_sleep["actions"])
    sit = get_situation(sandbox_db, "haos:night_owl")
    assert sit is not None and sit["value"]["vacuum_locked"] is True

    # 场景 B: 凌晨 02:00 清醒在家写代码 -> 书房 2700K 暖光护眼，不放扫地机
    res_coding = evaluate_night_owl_haos_rules(
        sandbox_db,
        now="2027-05-06T02:00:00+08:00",
        sleep_state="awake",
        location_state="home",
    )
    assert res_coding["warm_study_light_active"] is True
    assert res_coding["vacuum_locked"] is False
    assert res_coding["auto_clean_released"] is False
    light_act = next(a for a in res_coding["actions"] if a["rule"] == "late_night_study_warm_light")
    assert light_act["data"]["color_temp_kelvin"] == 2700

    # 场景 C: 离家上班 -> 释放扫地机自动清扫
    res_away = evaluate_night_owl_haos_rules(
        sandbox_db,
        now="2027-05-06T11:30:00+08:00",
        sleep_state="awake",
        location_state="away",
    )
    assert res_away["vacuum_locked"] is False
    assert res_away["auto_clean_released"] is True
    assert any(a["rule"] == "away_start_vacuum_clean" for a in res_away["actions"])

    # 场景 D: 晚间下班回家 T-12m -> 预开格力空调 + 林内零冷水
    res_pre = sync_night_owl_to_haos(
        sandbox_db,
        now="2027-05-06T20:15:00+08:00",
        sleep_state="awake",
        location_state="heading_home",
        eta_home_min=12.0,
        haos_call_fn=_fake_haos,
    )
    assert res_pre["pre_arrival_triggered"] is True
    rules = {a["rule"] for a in res_pre["actions"]}
    assert "pre_arrival_gree_ac" in rules
    assert "pre_arrival_rinnai_hot_water" in rules
    assert len(calls) >= 3

    report = format_haos_report(sandbox_db, now="2027-05-06T20:15:00+08:00")
    assert "格力空调" in report
    assert "科沃斯 T90S Pro" in report


# ---------------------------------------------------------------------------
# W2-5: 60m 通勤决策中枢 (commute_hub)
# ---------------------------------------------------------------------------
def test_w2_5_commute_mode_ranking_rescue_bundle_and_regime_switch(sandbox_db, frozen_clock):
    frozen_clock("2027-05-01T08:40:00+08:00")

    # 晴天正常电量：EV 48m + ¥40 vs Metro 58m(high) vs Didi 45m + ¥68 -> EV 最优
    sunny = compare_commute_modes(
        sandbox_db,
        ev_eta_min=48,
        ev_soc_pct=70,
        parking_yuan=40.0,
        metro_eta_min=58,
        metro_crowding="high",
        didi_eta_min=45,
        didi_fare_yuan=68.0,
        rain=False,
        late_min=0,
    )
    assert sunny["recommended_mode"] == "ev"
    assert sunny["rescue_recommended"] is False

    # 暴雨 + 起晚 15m -> 滴滴夺冠并建议触发一键救援捆绑卡
    storm_late = compare_commute_modes(
        sandbox_db,
        ev_eta_min=55,
        ev_soc_pct=60,
        parking_yuan=40.0,
        metro_eta_min=60,
        metro_crowding="high",
        didi_eta_min=45,
        didi_fare_yuan=72.0,
        rain=True,
        late_min=15,
    )
    assert storm_late["recommended_mode"] == "didi"
    assert storm_late["rescue_recommended"] is True

    advice = format_commute_advice(storm_late)
    assert "滴滴打车" in advice
    assert "救援捆绑卡" in advice

    # 生成单张 A2 救援捆绑卡（call_didi + delay_coffee + late_notice）
    bundle = stage_commute_rescue_bundle(
        sandbox_db,
        reason="暴雨+起晚15m",
        didi_fare_yuan=72.0,
        coffee_delay_min=20,
        late_eta_str="09:50",
    )
    assert bundle["ok"] is True
    assert bundle["actions"] == ["call_didi", "delay_coffee", "late_notice"]
    assert bundle["token"].startswith("A")
    assert "09:50" in bundle["card_text"]

    # 2027-12-31 搬家前 -> 仍为 primary_home，未进入 relearning
    pre_switch = check_regime_transition(sandbox_db, today=date(2027, 12, 31))
    assert pre_switch["regime"] == "primary_home"
    assert pre_switch["transitioned"] is False
    assert pre_switch["relearning"] is False
    assert pre_switch["days_until_switch"] == 1

    # 2028-01-01 搬入示例小区B -> 切换为 secondary_home 并写入 meta 与 routines.md relearning
    switched = check_regime_transition(sandbox_db, today=date(2028, 1, 1))
    assert switched["regime"] == "secondary_home"
    assert switched["transitioned"] is True
    assert switched["relearning"] is True
    assert sandbox_db.get_meta("commute_regime") == "secondary_home"
    assert "relearning" in (sandbox_db.get_meta("routines_relearning") or "")
    assert "relearning" in Path(switched["routines_md_path"]).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# W2-6: 家电与住宅全周期护照 (home_passport)
# ---------------------------------------------------------------------------
def test_w2_6_home_passport_utilities_consumables_faults_and_depreciation(sandbox_db, frozen_clock):
    frozen_clock("2027-05-10T10:00:00+08:00")

    # 1) 水电气网物业周期守卫
    upsert_utility(sandbox_db, kind="electric", account_no="BJ-ELEC-8801", cycle_days=30, next_due_at="2027-05-13", amount_yuan=260.5)
    upsert_utility(sandbox_db, kind="property", account_no="XFL-PROP-1203", cycle_days=90, next_due_at="2027-05-08", amount_yuan=1200.0)
    upsert_utility(sandbox_db, kind="broadband", account_no="CU-1000M", cycle_days=30, next_due_at="2027-06-01", amount_yuan=129.0)

    util_check = check_due_utilities(sandbox_db, now="2027-05-10T10:00:00+08:00", warn_days=5, notify=True)
    assert len(util_check["overdue"]) == 1
    assert util_check["overdue"][0]["kind"] == "property"
    assert len(util_check["due_soon"]) == 1
    assert util_check["due_soon"][0]["kind"] == "electric"
    assert len(util_check["upcoming"]) == 1

    # 2) 耗材寿命追踪（净水滤芯 / 扫地机尘袋 / 新风 HEPA）
    upsert_consumable(sandbox_db, "app-09", "科沃斯 T90S Pro", "基站集尘袋", lifespan_days=60, installed_at="2027-03-15")
    upsert_consumable(sandbox_db, "app-14", "新风除醛主机", "H13 HEPA 滤网", lifespan_days=180, installed_at="2027-04-01")
    due_cons = check_due_consumables(sandbox_db, now="2027-05-10T10:00:00+08:00", warn_days=14)
    assert len(due_cons) == 1
    assert due_cons[0]["consumable_name"] == "基站集尘袋"
    assert due_cons[0]["days_remaining"] == 4
    assert due_cons[0]["status"] == "DUE_SOON"

    # 3) 故障码秒查（格力 E4 + 科沃斯 C0 + 保修期精准计算）
    gree_e4 = lookup_fault_code(
        sandbox_db,
        brand_or_appliance="格力空调",
        fault_code="E4",
        purchase_date="2026-11-11",
        warranty_years=6,
        now="2027-05-10T10:00:00+08:00",
    )
    assert gree_e4["found"] is True
    assert gree_e4["brand"] == "gree"
    assert "排气温度" in gree_e4["meaning"]
    assert gree_e4["hotline"] == "400-836-5315"
    assert gree_e4["in_warranty"] is True
    assert gree_e4["warranty_expires_at"] == "2032-11-11"
    assert gree_e4["warranty_days_remaining"] > 1800

    eco_c0 = lookup_fault_code(sandbox_db, "科沃斯 T90S Pro", "C0", purchase_date="2026-11-11", warranty_years=2, now="2027-05-10T10:00:00+08:00")
    assert eco_c0["found"] is True
    assert "上下水" in eco_c0["meaning"]

    # 4) 15 件大家电直线折旧估值 + 同步 sure_sync_map
    val_15 = compute_appliance_asset_valuation(sandbox_db, as_of="2027-11-11")
    assert val_15["item_count"] == 15
    assert val_15["total_book_value_yuan"] < val_15["total_purchase_yuan"]
    assert val_15["sure_synced_count"] == 15

    # 验证单件精确 1 年直线折旧（10,000 元 / 10 年 -> 365 天后净值 9,000.00）
    val_exact = compute_appliance_asset_valuation(
        sandbox_db,
        items=[{"id": "test-ac", "name": "测试空调", "purchase_price": 10000.0, "purchase_date": "2026-11-11", "useful_life_years": 10}],
        as_of="2027-11-11",
    )
    assert val_exact["total_book_value_yuan"] == 9000.0
    assert val_exact["total_depreciation_yuan"] == 1000.0

    conn = sandbox_db.connect()
    try:
        cnt = conn.execute("SELECT COUNT(*) FROM sure_sync_map WHERE local_kind = 'asset'").fetchone()[0]
    finally:
        conn.close()
    assert cnt >= 15

    report = format_home_passport_report(sandbox_db, now="2027-05-10T10:00:00+08:00")
    assert "示例小区B家电与住宅全周期护照" in report
    assert "基站集尘袋" in report
    assert "固定资产净值" in report
