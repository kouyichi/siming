"""W2-1 (`home_air_guard`)：新房（示例小区B）放甲醛远程自治、通风策略调度、指数衰减安全入住预测与水/气/电 L3 破静告警。"""

from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any, Optional

from siming import db as _db_mod
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, parse_iso
from siming.event_chain import set_situation
from siming.modules import is_module_enabled
from siming.push import push_message

SAFE_HCHO_THRESHOLD_MG_M3 = 0.08
WARM_BAKE_MIN_TEMP_C = 22.0
PURGE_MAX_OUTDOOR_AQI = 100
TARGET_MOVEIN_DATE = "2028-01-01"

MODE_LABELS: dict[str, str] = {
    "bake_offgas": "闷放加速释放（高温封闭游离）",
    "strong_purge": "强排新风换气（室外优良）",
    "recirculate_filter": "内循环过滤（室外雾霾隔离）",
    "eco_standby": "节能微通风待机（甲醛已达标）",
}


def _coerce_now(now: Optional[datetime | str] = None) -> datetime:
    if now is None:
        return _db_mod.now_local()
    if isinstance(now, str):
        return parse_iso(now)
    if now.tzinfo is None:
        return now.replace(tzinfo=SHANGHAI_TZ)
    return now.astimezone(SHANGHAI_TZ)


def _classify_ventilation(
    temp_c: float,
    hcho_mg_m3: float,
    voc_ppb: float,
    outdoor_aqi: int,
    cur: datetime,
    safe_threshold: float = SAFE_HCHO_THRESHOLD_MG_M3,
) -> tuple[str, str, dict[str, Any]]:
    """根据室内温湿度/甲醛/VOC、室外 AQI 与时段决策通风模式及 HAOS 服务调用载荷。"""
    if outdoor_aqi > PURGE_MAX_OUTDOOR_AQI:
        return (
            "recirculate_filter",
            f"室外 AQI={outdoor_aqi}（> {PURGE_MAX_OUTDOOR_AQI}），关闭新风外循环，切换内循环 + 活性炭/HEPA 净化。",
            {
                "domain": "fan",
                "service": "set_preset_mode",
                "entity_id": "fan.xixingfuli_fresh_air",
                "service_data": {"preset_mode": "recirculate", "percentage": 60},
            },
        )

    needs_deformaldehyde = hcho_mg_m3 > safe_threshold or voc_ppb >= 400.0
    if not needs_deformaldehyde:
        return (
            "eco_standby",
            f"室内甲醛 {hcho_mg_m3:.3f} mg/m³（≤ 国标 {safe_threshold:.2f}）且 VOC {voc_ppb:.0f} ppb 正常，低速节能微通风。",
            {
                "domain": "fan",
                "service": "set_preset_mode",
                "entity_id": "fan.xixingfuli_fresh_air",
                "service_data": {"preset_mode": "eco", "percentage": 20},
            },
        )

    if 10 <= cur.hour < 15 and temp_c >= WARM_BAKE_MIN_TEMP_C and hcho_mg_m3 >= 0.10:
        return (
            "bake_offgas",
            f"午间升温闷放窗（{cur.strftime('%H:%M')} · {temp_c:.1f}°C ≥ 22°C · 甲醛 {hcho_mg_m3:.3f} mg/m³），关闭新风闷放加速板材释放，待 15:00 后切强排。",
            {
                "domain": "fan",
                "service": "turn_off",
                "entity_id": "fan.xixingfuli_fresh_air",
                "service_data": {"mode": "bake_offgas", "resume_purge_at": "15:00"},
            },
        )

    return (
        "strong_purge",
        f"室内甲醛 {hcho_mg_m3:.3f} mg/m³ 超标且室外 AQI={outdoor_aqi} 优良，开启新风最大档强排换气。",
        {
            "domain": "fan",
            "service": "set_preset_mode",
            "entity_id": "fan.xixingfuli_fresh_air",
            "service_data": {"preset_mode": "turbo", "percentage": 100},
        },
    )


def ingest_air_reading(
    db: SimingDB,
    room: str = "客厅",
    temp_c: float = 22.0,
    humidity_pct: float = 45.0,
    hcho_mg_m3: float = 0.12,
    voc_ppb: float = 300.0,
    outdoor_aqi: int = 50,
    water_leak: bool = False,
    gas_leak: bool = False,
    power_ok: bool = True,
    recorded_at: Optional[str] = None,
    notify: bool = True,
) -> dict[str, Any]:
    """写入示例小区B传感器读数，自动计算通风模式；遇漏水/燃气泄漏/断电触发 L3 破静告警。"""
    db.init_db()
    cur = parse_iso(recorded_at) if recorded_at else _coerce_now()
    ts = iso_now(cur)

    hcho_val = max(0.0, float(hcho_mg_m3))
    mode, reason, haos_action = _classify_ventilation(
        temp_c=float(temp_c),
        hcho_mg_m3=hcho_val,
        voc_ppb=float(voc_ppb),
        outdoor_aqi=int(outdoor_aqi),
        cur=cur,
    )

    with db.transaction() as conn:
        cur_sql = conn.execute(
            """
            INSERT INTO home_air_readings (
                recorded_at, room, temp_c, humidity_pct, hcho_mg_m3, voc_ppb,
                outdoor_aqi, water_leak, gas_leak, power_ok, fresh_air_mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                room,
                float(temp_c),
                float(humidity_pct),
                hcho_val,
                float(voc_ppb),
                int(outdoor_aqi),
                1 if water_leak else 0,
                1 if gas_leak else 0,
                1 if power_ok else 0,
                mode,
            ),
        )
        reading_id = int(cur_sql.lastrowid or 0)

    emergencies: list[dict[str, str]] = []
    if water_leak:
        emergencies.append({"kind": "water_leak", "label": "浸水漏水告警", "action": "立即远程关闭入户总水阀并联系物业/工长上门核查"})
    if gas_leak:
        emergencies.append({"kind": "gas_leak", "label": "燃气泄漏告警", "action": "立即切断燃气电磁阀、禁止明火与远程拉闸，拨打北京燃气 96777"})
    if not power_ok:
        emergencies.append({"kind": "power_outage", "label": "全屋断电告警", "action": "检查强电箱跳闸与冰箱/NAS UPS 续航状态"})

    set_situation(
        db,
        "home_air:latest",
        {
            "reading_id": reading_id,
            "recorded_at": ts,
            "room": room,
            "temp_c": float(temp_c),
            "humidity_pct": float(humidity_pct),
            "hcho_mg_m3": hcho_val,
            "voc_ppb": float(voc_ppb),
            "outdoor_aqi": int(outdoor_aqi),
            "water_leak": bool(water_leak),
            "gas_leak": bool(gas_leak),
            "power_ok": bool(power_ok),
            "fresh_air_mode": mode,
            "emergency_kinds": [e["kind"] for e in emergencies],
        },
        source="home_air_guard",
        as_of=ts,
    )

    l3_triggered = bool(emergencies)
    push_result: Optional[dict[str, Any]] = None
    alert_text = ""

    if l3_triggered:
        lines = [f"🚨 **【L3 紧急破静】新房（示例小区B）安防告警（{room} · {ts[11:16]}）**"]
        for em in emergencies:
            lines.append(f"• 🔴 **{em['label']}**：{em['action']}")
        lines.append(f"• 当前读数：温度 {temp_c:.1f}°C · 湿度 {humidity_pct:.0f}% · 甲醛 {hcho_val:.3f} mg/m³")
        alert_text = "\n".join(lines)

        with db.transaction() as conn:
            db.append_event(
                conn,
                source="home_air_guard",
                thread_key=f"home_air:emergency:{room}",
                summary=f"新房 L3 安防告警: {', '.join(e['label'] for e in emergencies)}",
                payload={"reading_id": reading_id, "room": room, "emergencies": emergencies},
                occurred_at=ts,
            )
            db.write_audit(
                "A1",
                "home_air_l3_alert",
                {"reading_id": reading_id, "room": room, "emergencies": [e["kind"] for e in emergencies]},
                target_id=f"reading:{reading_id}",
                conn=conn,
            )

        if notify:
            kinds_slug = "+".join(e["kind"] for e in emergencies)
            stable_ref = f"home_air:{room}:{kinds_slug}"
            dedup_meta_key = f"home_air:l3_dedup:{room}:{kinds_slug}"
            last_sent_raw = db.get_meta(dedup_meta_key) or ""
            is_dup = False
            if last_sent_raw:
                try:
                    last_dt = parse_iso(last_sent_raw)
                    if abs((cur - last_dt).total_seconds()) < 300.0:
                        is_dup = True
                except Exception:
                    is_dup = False
            if is_dup:
                push_result = {
                    "ok": True,
                    "delivered": False,
                    "skipped": "duplicate",
                    "level": "now",
                    "rule_id": "home_air_emergency",
                    "ref_id": stable_ref,
                }
            else:
                push_result = push_message(
                    db,
                    text=alert_text,
                    rule_id="home_air_emergency",
                    ref_id=stable_ref,
                    level="now",
                    dedup_window=timedelta(minutes=5),
                    hard=True,
                    sub_meta={"kind": "home_air_l3", "room": room, "emergencies": [e["kind"] for e in emergencies]},
                )
                if push_result.get("ok") and push_result.get("skipped") != "duplicate":
                    db.set_meta(dedup_meta_key, ts)

    return {
        "ok": True,
        "reading_id": reading_id,
        "recorded_at": ts,
        "room": room,
        "temp_c": float(temp_c),
        "humidity_pct": float(humidity_pct),
        "hcho_mg_m3": hcho_val,
        "voc_ppb": float(voc_ppb),
        "outdoor_aqi": int(outdoor_aqi),
        "water_leak": bool(water_leak),
        "gas_leak": bool(gas_leak),
        "power_ok": bool(power_ok),
        "fresh_air_mode": mode,
        "ventilation_reason": reason,
        "haos_action": haos_action,
        "l3_triggered": l3_triggered,
        "emergencies": emergencies,
        "alert_text": alert_text,
        "push_result": push_result,
    }


def recommend_ventilation_strategy(db: SimingDB, now: Optional[datetime | str] = None) -> dict[str, Any]:
    """基于最新空气传感器读数与当前时段，输出通风调度模式与 HAOS 执行载荷。"""
    db.init_db()
    cur = _coerce_now(now)
    enabled = is_module_enabled(db, "home_air_guard")

    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM home_air_readings ORDER BY recorded_at DESC, id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    if not row:
        return {
            "ok": True,
            "enabled": enabled,
            "has_data": False,
            "mode": "eco_standby",
            "mode_label": MODE_LABELS["eco_standby"],
            "reason": "暂无新房空气读数，保持默认低速待机。",
            "haos_action": {
                "domain": "fan",
                "service": "set_preset_mode",
                "entity_id": "fan.xixingfuli_fresh_air",
                "service_data": {"preset_mode": "eco", "percentage": 20},
            },
            "latest_reading": None,
        }

    latest = dict(row)
    mode, reason, haos_action = _classify_ventilation(
        temp_c=float(latest["temp_c"]),
        hcho_mg_m3=float(latest["hcho_mg_m3"]),
        voc_ppb=float(latest["voc_ppb"]),
        outdoor_aqi=int(latest["outdoor_aqi"]),
        cur=cur,
    )
    return {
        "ok": True,
        "enabled": enabled,
        "has_data": True,
        "evaluated_at": iso_now(cur),
        "mode": mode,
        "mode_label": MODE_LABELS[mode],
        "reason": reason,
        "haos_action": haos_action,
        "room": latest["room"],
        "temp_c": float(latest["temp_c"]),
        "hcho_mg_m3": float(latest["hcho_mg_m3"]),
        "voc_ppb": float(latest["voc_ppb"]),
        "outdoor_aqi": int(latest["outdoor_aqi"]),
        "latest_reading": latest,
    }


def predict_safe_movein_date(
    db: SimingDB,
    safe_threshold: float = SAFE_HCHO_THRESHOLD_MG_M3,
    now: Optional[datetime | str] = None,
) -> dict[str, Any]:
    """对历史甲醛读数做对数线性指数衰减拟合 C(t) = C0 * exp(-k*t)，预测降至国标阈值（0.08 mg/m³）的安全入住日期。"""
    db.init_db()
    cur = _coerce_now(now)
    threshold = max(0.001, float(safe_threshold))

    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT recorded_at, room, hcho_mg_m3 FROM home_air_readings WHERE hcho_mg_m3 >= 0 ORDER BY recorded_at ASC, id ASC"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return {
            "ok": False,
            "enough_data": False,
            "already_safe": False,
            "trend": "no_data",
            "safe_threshold": threshold,
            "current_hcho": None,
            "half_life_days": None,
            "days_remaining": None,
            "predicted_safe_date": None,
            "target_movein_date": TARGET_MOVEIN_DATE,
            "on_track_for_movein": None,
            "sample_count": 0,
            "reason": "暂无有效甲醛读数",
        }

    pts: list[tuple[datetime, float]] = []
    for r in rows:
        try:
            dt = parse_iso(str(r["recorded_at"]))
            val = float(r["hcho_mg_m3"])
            if val >= 0.0 and math.isfinite(val):
                pts.append((dt, val))
        except Exception:
            continue

    if not pts:
        return {
            "ok": False,
            "enough_data": False,
            "already_safe": False,
            "trend": "no_data",
            "safe_threshold": threshold,
            "current_hcho": None,
            "half_life_days": None,
            "days_remaining": None,
            "predicted_safe_date": None,
            "target_movein_date": TARGET_MOVEIN_DATE,
            "on_track_for_movein": None,
            "sample_count": 0,
            "reason": "无合法时间戳的甲醛样本",
        }

    t0 = pts[0][0]
    latest_dt, current_hcho = pts[-1]

    if current_hcho <= threshold:
        return {
            "ok": True,
            "enough_data": True,
            "already_safe": True,
            "trend": "safe",
            "safe_threshold": threshold,
            "current_hcho": round(current_hcho, 4),
            "decay_rate_k": None,
            "half_life_days": None,
            "days_remaining": 0.0,
            "predicted_safe_date": cur.date().isoformat(),
            "target_movein_date": TARGET_MOVEIN_DATE,
            "on_track_for_movein": True,
            "sample_count": len(pts),
            "r_squared": None,
            "reason": f"当前甲醛 {current_hcho:.3f} mg/m³ 已低于国标阈值 {threshold:.2f} mg/m³，可直接安排入住前复检。",
        }

    xs = [(dt - t0).total_seconds() / 86400.0 for dt, _ in pts]
    ys = [math.log(max(v, 1e-4)) for _, v in pts]
    n = len(xs)
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)

    if n < 2 or sxx <= 1e-9:
        return {
            "ok": True,
            "enough_data": False,
            "already_safe": False,
            "trend": "insufficient_data",
            "safe_threshold": threshold,
            "current_hcho": round(current_hcho, 4),
            "decay_rate_k": None,
            "half_life_days": None,
            "days_remaining": None,
            "predicted_safe_date": None,
            "target_movein_date": TARGET_MOVEIN_DATE,
            "on_track_for_movein": None,
            "sample_count": n,
            "r_squared": None,
            "reason": "样本跨度不足，需至少 2 个不同时间点读数方可拟合衰减曲线。",
        }

    sxy = sum((x - mean_x) * (y - mean_y) for x, y in zip(xs, ys))
    slope = sxy / sxx
    intercept = mean_y - slope * mean_x
    decay_k = -slope

    ss_tot = sum((y - mean_y) ** 2 for y in ys)
    ss_res = sum((y - (intercept + slope * x)) ** 2 for x, y in zip(xs, ys))
    r_squared = round(1.0 - (ss_res / ss_tot), 4) if ss_tot > 1e-12 else 1.0

    if decay_k <= 1e-6:
        is_rising = decay_k < -1e-6
        return {
            "ok": True,
            "enough_data": True,
            "already_safe": False,
            "trend": "rising" if is_rising else "flat",
            "safe_threshold": threshold,
            "current_hcho": round(current_hcho, 4),
            "decay_rate_k": round(decay_k, 6),
            "half_life_days": None,
            "days_remaining": None,
            "predicted_safe_date": None,
            "target_movein_date": TARGET_MOVEIN_DATE,
            "on_track_for_movein": False,
            "sample_count": n,
            "r_squared": r_squared,
            "reason": (
                "甲醛浓度呈上升/反弹趋势（疑似升温或地暖开启引发二次释放），暂无法预测达标日期，建议加强白天闷放与强排换气。"
                if is_rising
                else "甲醛浓度暂未呈现有效下降趋势，建议检查门窗密封或增强白天升温闷放 + 强排频次。"
            ),
        }

    half_life_days = math.log(2.0) / decay_k
    days_from_latest = max(0.0, math.log(current_hcho / threshold) / decay_k)
    if days_from_latest > 3650.0:
        return {
            "ok": True,
            "enough_data": True,
            "already_safe": False,
            "trend": "stalled",
            "safe_threshold": threshold,
            "current_hcho": round(current_hcho, 4),
            "decay_rate_k": round(decay_k, 6),
            "half_life_days": round(half_life_days, 2),
            "days_remaining": None,
            "predicted_safe_date": None,
            "target_movein_date": TARGET_MOVEIN_DATE,
            "on_track_for_movein": False,
            "sample_count": n,
            "r_squared": r_squared,
            "reason": "甲醛衰减速率过慢（按当前斜率超过 10 年），建议排查释放源并增强排风。",
        }

    predicted_dt = latest_dt + timedelta(days=days_from_latest)
    predicted_date_str = predicted_dt.date().isoformat()
    days_from_now = max(0.0, (predicted_dt - cur).total_seconds() / 86400.0)

    return {
        "ok": True,
        "enough_data": True,
        "already_safe": False,
        "trend": "decaying",
        "safe_threshold": threshold,
        "current_hcho": round(current_hcho, 4),
        "decay_rate_k": round(decay_k, 6),
        "half_life_days": round(half_life_days, 2),
        "days_remaining": round(days_from_now, 1),
        "predicted_safe_date": predicted_date_str,
        "target_movein_date": TARGET_MOVEIN_DATE,
        "on_track_for_movein": predicted_date_str <= TARGET_MOVEIN_DATE,
        "sample_count": n,
        "r_squared": r_squared,
        "reason": f"按指数衰减半衰期 {half_life_days:.1f} 天推算，预计 {predicted_date_str} 降至 {threshold:.2f} mg/m³ 以下。",
    }


def format_home_air_report(db: SimingDB, now: Optional[datetime | str] = None) -> str:
    """生成示例小区B放甲醛远程自治与安防态势简报。"""
    cur = _coerce_now(now)
    strat = recommend_ventilation_strategy(db, now=cur)
    pred = predict_safe_movein_date(db, now=cur)

    lines = [f"🌿 **新房（示例小区B）放甲醛远程自治报告**（{cur.strftime('%Y-%m-%d %H:%M')}）"]
    if not strat.get("has_data"):
        lines.append("• 暂无空气传感器读数，接入 HAOS 后将自动追踪温湿度/甲醛/VOC 与水气电安防。")
        return "\n".join(lines)

    latest = strat["latest_reading"]
    safety_flags: list[str] = []
    if latest.get("water_leak"):
        safety_flags.append("🔴 浸水告警")
    if latest.get("gas_leak"):
        safety_flags.append("🔴 燃气告警")
    if not latest.get("power_ok", 1):
        safety_flags.append("🔴 断电告警")
    safety_str = " / ".join(safety_flags) if safety_flags else "🟢 水/气/电正常"

    lines.append(
        f"• 📡 最新读数（{latest['room']} · {str(latest['recorded_at'])[:16]}）："
        f"甲醛 **{latest['hcho_mg_m3']:.3f} mg/m³** ｜ VOC {latest['voc_ppb']:.0f} ppb ｜ "
        f"{latest['temp_c']:.1f}°C / {latest['humidity_pct']:.0f}% ｜ 室外 AQI {latest['outdoor_aqi']}"
    )
    lines.append(f"• 🛡️ 远程安防：{safety_str}")
    lines.append(f"• 🌀 通风策略：**{strat['mode_label']}**（`{strat['mode']}`）— {strat['reason']}")

    if pred.get("already_safe"):
        lines.append(f"• ✅ 入住预测：{pred['reason']}")
    elif pred.get("predicted_safe_date"):
        track_badge = "🟢 赶得上 04-30 入住" if pred.get("on_track_for_movein") else "🟠 晚于 04-30 目标，需加强排风"
        lines.append(
            f"• 📉 衰减预测：预计 **`{pred['predicted_safe_date']}`** 达标（≤{pred['safe_threshold']:.2f} mg/m³ · "
            f"半衰期 {pred['half_life_days']:.1f} 天 · R²={pred.get('r_squared', 0):.2f}）｜ {track_badge}"
        )
    else:
        lines.append(f"• 📉 衰减预测：{pred.get('reason', '样本不足')}")

    return "\n".join(lines)
