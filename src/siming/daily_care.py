"""日常照护链（daily_care）：出门天气 / 穿衣、周期用药/注射与开药、常备耗材补货雷达。

所有作息、地址、药品、医院与耗材规则均可在 `~/.config/siming/policy.yaml` 的 `daily_care` 节点自定义。

三条链（全部纯代码确定性判断，模型不参与）：
1. 出门前（工作日 leave_home_at − lead）：查当前生效住址逐小时预报，上班 / 下班两个时段有雨就提醒带伞，
   比昨天同时段体感骤变 ≥ 阈值或早晚温差大就给穿衣建议；什么都没有就不打扰。临出门时正在下雨补一句；
   下班前若晚间新出现降雨（早上没提过）再补一句。
2. 周期注射/用药：按设定周期在提醒时段推送，回「打了/已服」销账并扣余量；剩余剂量 ≤ hospital_lead_doses 时推「挂号开药」。
3. 补货雷达：从外卖/电商订单页采集商品明细（collectors/meituan_orders.py），按包装规格 × 份数入库存，
   按天或按周期速率扣减，余量 ≤ lead_days 天时提醒补货。
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Optional

from siming.db import SHANGHAI_TZ, SimingDB, iso_now, logical_today, now_local, parse_iso

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

DEFAULT_CARE_CFG: dict[str, Any] = {
    "work": {
        "weekdays": [0, 1, 2, 3, 4],
        "clock_in_by": "09:35",
        "nominal_off": "19:00",
        "usual_leave_office": "22:00",
    },
    "regimes": [
        {
            "id": "primary_home",
            "until": "2028-01-01",
            "home_label": "示例小区A",
            "home_lat": 39.9042,
            "home_lon": 116.4074,
            "office_label": "公司",
            "office_lat": 39.9080,
            "office_lon": 116.4070,
            "commute": "步行",
            "commute_min": 10,
            "leave_home_at": "09:15",
        },
        {
            "id": "secondary_home",
            "from": "2028-01-01",
            "home_label": "示例小区B",
            "home_lat": 39.8600,
            "home_lon": 116.3200,
            "office_label": "公司",
            "office_lat": 39.9080,
            "office_lon": 116.4070,
            "commute": "地铁/打车",
            "commute_min": 60,
            "leave_home_at": "08:25",
        },
    ],
    "weather": {
        "remind_lead_min": 25,
        "rain_prob_pct": 50,
        "rain_mm": 0.2,
        "temp_swing_c": 5,
        "day_night_gap_c": 8,
        "heavy_rain_mm": 4.0,
    },
    "injection": {
        "drug": "周期用药",
        "weekday": 2,
        "remind_at": "00:50",
        "remind_next_day": True,
        "awake_nudge_interval_min": 60,
        "max_awake_nudges": 4,
        "followup_after_h": 22.5,
        "missed_after_h": 96,
        "doses_per_pen": 4,
        "hospital_lead_doses": 1,
        "booking_weekdays": [2, 3],
        "booking_remind_at": "09:30",
        "hospital": "常用医院",
    },
    "supplies": {
        "mask": {"label": "口罩", "match": "口罩", "exclude": "", "unit": "只", "per_day": 1.0, "lead_days": 5},
        "needle": {"label": "注射笔针头", "match": "针头", "exclude": "", "unit": "支", "per_injection": 1.0, "lead_days": 10},
        "alcohol_pad": {
            "label": "酒精棉片",
            "match": "棉片",
            "exclude": "棉签|湿巾|湿纸巾",
            "unit": "片",
            "per_injection": 1.0,
            "learn_rate": True,
            "lead_days": 7,
        },
    },
    "harvest": {"min_interval_h": 8, "active_from": "10:00", "active_to": "23:30"},
}


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = json.loads(json.dumps(base))
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_care_cfg(db: SimingDB) -> dict[str, Any]:
    from siming.config import load_policy

    try:
        pol = load_policy(db.paths)
    except Exception:
        pol = {}
    return _deep_merge(DEFAULT_CARE_CFG, pol.get("daily_care") or {})


def _hm(s: str) -> tuple[int, int]:
    hh, mm = str(s).split(":")
    return int(hh), int(mm)


def _at(d: date, hhmm: str) -> datetime:
    h, m = _hm(hhmm)
    return datetime.combine(d, time(h, m), tzinfo=SHANGHAI_TZ)


def active_regime(cfg: dict[str, Any], d: date) -> dict[str, Any]:
    regs = list(cfg.get("regimes") or [])
    for r in regs:
        frm = str(r.get("from") or "0000-01-01")
        until = str(r.get("until") or "9999-12-31")
        if frm <= d.isoformat() < until:
            return r
    return regs[-1] if regs else DEFAULT_CARE_CFG["regimes"][0]


def is_office_day(db: SimingDB, cfg: dict[str, Any], d: date) -> bool:
    """法定节假日 / 调休补班以 cn_calendar 为准；没有日历规则时退回周一到周五。"""
    try:
        from siming.cn_calendar import day_info

        return bool(day_info(d, db.paths).is_workday)
    except Exception:
        return d.weekday() in set(cfg["work"].get("weekdays") or [0, 1, 2, 3, 4])


def _meta_json(db: SimingDB, key: str, default: Any) -> Any:
    raw = db.get_meta(key)
    if not raw:
        return default
    try:
        return json.loads(raw)
    except Exception:
        return default


def _set_meta_json(db: SimingDB, key: str, val: Any) -> None:
    db.set_meta(key, json.dumps(val, ensure_ascii=False))


def _push(db: SimingDB, text: str, rule_id: str, ref_id: str, bypass_quiet: bool, hard: bool = False) -> dict[str, Any]:
    from siming.router import send_qq_message

    if hard:
        with db.transaction() as conn:
            conn.execute(
                """
                INSERT INTO rule_stats (rule_id, mode, hard_rule, updated_at) VALUES (?, 'ACTIVE', 1, ?)
                ON CONFLICT(rule_id) DO UPDATE SET hard_rule=1, updated_at=excluded.updated_at
                """,
                (rule_id, iso_now()),
            )
    return send_qq_message(db, text, ref_id=ref_id, rule_id=rule_id, bypass_quiet=bypass_quiet, sub_meta={"kind": "daily_care"})


def _module_on(db: SimingDB, module_id: str) -> bool:
    from siming.modules import is_module_enabled

    return is_module_enabled(db, module_id)


# ---------------------------------------------------------------------------
# 1. 出门天气 / 穿衣
# ---------------------------------------------------------------------------

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"
_RAIN_CODES = set(range(51, 68)) | set(range(80, 83)) | set(range(95, 100))
_SNOW_CODES = set(range(71, 78)) | {85, 86}


def fetch_open_meteo(lat: float, lon: float) -> Optional[dict[str, Any]]:
    if os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return None
    import httpx

    params = {
        "latitude": f"{lat:.4f}",
        "longitude": f"{lon:.4f}",
        "hourly": "temperature_2m,apparent_temperature,precipitation_probability,precipitation,weather_code,wind_speed_10m",
        "current": "temperature_2m,apparent_temperature,precipitation,weather_code",
        "timezone": "Asia/Shanghai",
        "past_days": 1,
        "forecast_days": 2,
    }
    last_exc: Optional[Exception] = None
    http_proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy") or "http://127.0.0.1:7890"
    proxies: list[Optional[str]] = [http_proxy] if http_proxy.startswith(("http://", "https://")) else []
    proxies.append(None)
    for prx in proxies:
        try:
            with httpx.Client(timeout=6.0, trust_env=False, proxy=prx) as client:
                resp = client.get(OPEN_METEO_URL, params=params)
                resp.raise_for_status()
                return resp.json()
        except Exception as exc:
            last_exc = exc
    if last_exc:
        raise last_exc
    return None


def _hourly_rows(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    h = data.get("hourly") or {}
    times = list(h.get("time") or [])
    rows: dict[str, dict[str, Any]] = {}
    for i, t in enumerate(times):
        def g(k: str) -> Any:
            arr = h.get(k) or []
            return arr[i] if i < len(arr) else None

        rows[t[:13]] = {
            "t": t,
            "temp": g("temperature_2m"),
            "feel": g("apparent_temperature"),
            "prob": g("precipitation_probability"),
            "mm": g("precipitation"),
            "code": g("weather_code"),
            "wind": g("wind_speed_10m"),
        }
    return rows


def _window_hours(start: datetime, end: datetime) -> list[str]:
    out = []
    cur = start.replace(minute=0, second=0, microsecond=0)
    while cur <= end:
        out.append(cur.strftime("%Y-%m-%dT%H"))
        cur += timedelta(hours=1)
    return out


def _rain_in(rows: dict[str, dict[str, Any]], hours: list[str], wcfg: dict[str, Any]) -> dict[str, Any]:
    hit_hours, max_prob, sum_mm, snow = [], 0, 0.0, False
    for hk in hours:
        r = rows.get(hk)
        if not r:
            continue
        prob = int(r.get("prob") or 0)
        mm = float(r.get("mm") or 0.0)
        code = int(r.get("code") or 0)
        wet = mm >= float(wcfg["rain_mm"]) or prob >= int(wcfg["rain_prob_pct"]) or code in _RAIN_CODES or code in _SNOW_CODES
        if wet:
            hit_hours.append(hk[-2:])
            snow = snow or code in _SNOW_CODES
        max_prob = max(max_prob, prob)
        sum_mm += mm
    return {"hit": bool(hit_hours), "hours": hit_hours, "max_prob": max_prob, "sum_mm": round(sum_mm, 1), "snow": snow}


def _avg(rows: dict[str, dict[str, Any]], hours: list[str], key: str) -> Optional[float]:
    vals = [float(rows[h][key]) for h in hours if h in rows and rows[h].get(key) is not None]
    return round(sum(vals) / len(vals), 1) if vals else None


def clothing_advice(feel: float, wind_kmh: float = 0.0) -> str:
    if feel <= -5:
        s = "长款羽绒服 + 帽子围巾手套"
    elif feel <= 3:
        s = "羽绒服 / 厚棉服，加围巾"
    elif feel <= 9:
        s = "厚外套或大衣 + 毛衣"
    elif feel <= 14:
        s = "夹克 / 风衣 + 长袖"
    elif feel <= 19:
        s = "薄外套 / 卫衣"
    elif feel <= 25:
        s = "长袖或 T 恤，备件薄外套"
    else:
        s = "短袖，注意防晒补水"
    if wind_kmh >= 30:
        s += "（风大，外层选防风的）"
    return s


def _fmt_hours(hs: list[str]) -> str:
    if not hs:
        return ""
    return f"{hs[0]}:00" if len(hs) == 1 else f"{hs[0]}:00–{int(hs[-1]) + 1:02d}:00"


def analyze_commute_weather(data: dict[str, Any], day: date, regime: dict[str, Any], cfg: dict[str, Any]) -> dict[str, Any]:
    wcfg = cfg["weather"]
    rows = _hourly_rows(data)
    leave = _at(day, str(regime.get("leave_home_at") or "09:15"))
    commute = timedelta(minutes=int(regime.get("commute_min") or 10))
    back_start = _at(day, str(cfg["work"].get("usual_leave_office") or "22:00"))
    out_hours = _window_hours(leave, leave + commute + timedelta(minutes=15))
    back_hours = _window_hours(back_start, back_start + commute + timedelta(minutes=60))
    y_out = [(datetime.strptime(h, "%Y-%m-%dT%H") - timedelta(days=1)).strftime("%Y-%m-%dT%H") for h in out_hours]
    y_back = [(datetime.strptime(h, "%Y-%m-%dT%H") - timedelta(days=1)).strftime("%Y-%m-%dT%H") for h in back_hours]

    rain_out = _rain_in(rows, out_hours, wcfg)
    rain_back = _rain_in(rows, back_hours, wcfg)
    cur = data.get("current") or {}
    cur_code = int(cur.get("weather_code") or 0)
    raining_now = float(cur.get("precipitation") or 0) > 0 or cur_code in _RAIN_CODES or cur_code in _SNOW_CODES

    feel_out, feel_back = _avg(rows, out_hours, "feel"), _avg(rows, back_hours, "feel")
    y_feel_out, y_feel_back = _avg(rows, y_out, "feel"), _avg(rows, y_back, "feel")
    wind = max([float(rows[h].get("wind") or 0) for h in out_hours + back_hours if h in rows] or [0.0])
    d_out = round(feel_out - y_feel_out, 1) if feel_out is not None and y_feel_out is not None else None
    d_back = round(feel_back - y_feel_back, 1) if feel_back is not None and y_feel_back is not None else None
    gap = round(feel_out - feel_back, 1) if feel_out is not None and feel_back is not None else None
    swing_c = float(wcfg["temp_swing_c"])
    swing = any(x is not None and abs(x) >= swing_c for x in (d_out, d_back))
    big_gap = gap is not None and abs(gap) >= float(wcfg["day_night_gap_c"])
    heavy = max(rain_out["sum_mm"], rain_back["sum_mm"]) >= float(wcfg.get("heavy_rain_mm", 4.0))
    return {
        "day": day.isoformat(),
        "regime": regime.get("id"),
        "place": regime.get("home_label"),
        "out_hours": out_hours,
        "back_hours": back_hours,
        "rain_out": rain_out,
        "rain_back": rain_back,
        "raining_now": raining_now,
        "feel_out": feel_out,
        "feel_back": feel_back,
        "y_feel_out": y_feel_out,
        "y_feel_back": y_feel_back,
        "d_out": d_out,
        "d_back": d_back,
        "gap": gap,
        "wind": wind,
        "swing": swing,
        "big_gap": big_gap,
        "heavy": heavy,
        "actionable": bool(rain_out["hit"] or rain_back["hit"] or raining_now or swing or big_gap),
    }


def format_departure_card(a: dict[str, Any], regime: dict[str, Any], extra_lines: Optional[list[str]] = None) -> str:
    d = date.fromisoformat(a["day"])
    wd = "一二三四五六日"[d.weekday()]
    lines = [f"🚪 出门前看一眼 · {d.strftime('%m-%d')} 周{wd}（{a['place']}）"]
    ro, rb = a["rain_out"], a["rain_back"]
    if a["raining_now"] or ro["hit"] or rb["hit"]:
        word = "雪" if (ro["snow"] or rb["snow"]) else "雨"
        segs = []
        if a["raining_now"]:
            segs.append(f"楼下现在就在下{word}")
        if ro["hit"]:
            segs.append(f"上班路上 {_fmt_hours(ro['hours'])} 有{word}（概率 {ro['max_prob']}%，{ro['sum_mm']}mm）")
        if rb["hit"]:
            segs.append(f"晚上下班 {_fmt_hours(rb['hours'])} 有{word}（概率 {rb['max_prob']}%，{rb['sum_mm']}mm）")
        tail = "——带伞"
        if rb["hit"] and not ro["hit"] and not a["raining_now"]:
            tail = "——早上没下也带上伞，晚上回来用得到"
        lines.append(f"☔ {'；'.join(segs)}{tail}")
        if a["heavy"] and "打车" in str(regime.get("commute") or ""):
            lines.append("🚕 雨量偏大，打车高峰难叫，建议提前 15 分钟出门或先叫车")
    if a["swing"]:
        parts = []
        for tag, now_v, y_v, dv in (("上班时", a["feel_out"], a["y_feel_out"], a["d_out"]), ("下班时", a["feel_back"], a["y_feel_back"], a["d_back"])):
            if dv is not None and abs(dv) >= 1:
                parts.append(f"{tag}体感 {now_v:.0f}°C（昨天 {y_v:.0f}°C，{'降' if dv < 0 else '升'} {abs(dv):.0f}°）")
        lines.append(f"🌡️ 气温骤变：{'；'.join(parts)}")
    elif a["big_gap"]:
        if (a.get("gap") or 0) >= 0:
            lines.append(f"🌙 早晚温差大：上班体感 {a['feel_out']:.0f}°C，22 点后回家只有 {a['feel_back']:.0f}°C")
        else:
            lines.append(f"🌤️ 早冷晚暖温差大：出门体感 {a['feel_out']:.0f}°C，下班体感 {a['feel_back']:.0f}°C")
    if a["swing"] or a["big_gap"]:
        coldest = min(x for x in (a["feel_out"], a["feel_back"]) if x is not None)
        gap_note = ""
        if a["big_gap"]:
            gap_note = "（按晚上回家的温度穿）" if (a.get("gap") or 0) >= 0 else "（外套选好穿脱的）"
        lines.append(f"👔 穿衣：{clothing_advice(coldest, a['wind'])}" + gap_note)
    for ln in extra_lines or []:
        lines.append(ln)
    return "\n".join(lines)


def _weather_state(db: SimingDB, d: date) -> dict[str, Any]:
    return _meta_json(db, f"care_weather:{d.isoformat()}", {})


def _save_weather_state(db: SimingDB, d: date, st: dict[str, Any]) -> None:
    _set_meta_json(db, f"care_weather:{d.isoformat()}", st)


def run_weather_guard(
    db: SimingDB,
    now: Optional[datetime] = None,
    fetch: Optional[Callable[[float, float], Optional[dict[str, Any]]]] = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    if not force and not dry_run and not _module_on(db, "commute_weather"):
        return {"skipped": "module_disabled"}
    cfg = load_care_cfg(db)
    now = now or now_local()
    today = now.date()
    if not force and not is_office_day(db, cfg, today):
        return {"skipped": "rest_day"}
    regime = active_regime(cfg, today)
    leave = _at(today, str(regime.get("leave_home_at") or "09:15"))
    clock_in = _at(today, str(cfg["work"].get("clock_in_by") or "09:35"))
    lead = timedelta(minutes=int(cfg["weather"].get("remind_lead_min") or 25))
    back_start = _at(today, str(cfg["work"].get("usual_leave_office") or "22:00"))
    st = _weather_state(db, today)
    fetch_fn = fetch or fetch_open_meteo

    phase = ""
    if force:
        phase = "morning"
    elif leave - lead <= now < clock_in and not st.get("morning_done"):
        phase = "morning"
    elif leave - timedelta(minutes=6) <= now < clock_in and st.get("morning_done") and not st.get("rain_now_checked"):
        phase = "rain_now"
    elif back_start - timedelta(minutes=25) <= now < back_start + timedelta(minutes=30) and not st.get("evening_done"):
        phase = "evening"
    if not phase:
        return {"skipped": "not_due"}

    try:
        data = fetch_fn(float(regime["home_lat"]), float(regime["home_lon"]))
    except Exception as exc:
        db.mark_source_error("open_meteo", "ERROR", str(exc)[:200])
        return {"ok": False, "error": str(exc)[:200]}
    if not data:
        return {"ok": False, "error": "no_weather"}
    db.mark_source_ok("open_meteo", detail=f"{regime.get('home_label')}")
    a = analyze_commute_weather(data, today, regime, cfg)

    # 搬家后家和公司相距较远：公司那边也查一遍雨
    try:
        far = abs(float(regime["home_lat"]) - float(regime["office_lat"])) + abs(float(regime["home_lon"]) - float(regime["office_lon"])) > 0.04
    except Exception:
        far = False
    if far:
        try:
            od = fetch_fn(float(regime["office_lat"]), float(regime["office_lon"]))
            if od:
                oa = analyze_commute_weather(od, today, regime, cfg)
                if oa["rain_back"]["hit"] and not a["rain_back"]["hit"]:
                    a["rain_back"] = oa["rain_back"]
                    a["actionable"] = True
        except Exception:
            pass

    sent = None
    if phase == "morning":
        extra = supply_lines_for_departure(db, now)
        if dry_run:
            return {"ok": True, "phase": phase, "analysis": a, "text": format_departure_card(a, regime, extra_lines=extra), "would_push": bool(a["actionable"] or extra)}
        if a["actionable"] or extra:
            text = format_departure_card(a, regime, extra_lines=extra)
            sent = _push(db, text, "commute_weather", f"care:weather:{today.isoformat()}", bypass_quiet=True)
        st.update(
            {
                "morning_done": True,
                "morning_at": iso_now(now),
                "warned_rain_out": bool(a["rain_out"]["hit"] or a["raining_now"]),
                "warned_rain_back": bool(a["rain_back"]["hit"]),
                "rain_now_checked": bool(a["raining_now"]),
            }
        )
    elif phase == "rain_now":
        st["rain_now_checked"] = True
        if a["raining_now"] and not st.get("warned_rain_out"):
            sent = _push(db, f"☔ {a['place']}楼下这会儿开始下雨了，出门带伞。", "commute_weather", f"care:weather:{today.isoformat()}:now", bypass_quiet=True)
            st["warned_rain_out"] = True
    elif phase == "evening":
        st["evening_done"] = True
        if (a["rain_back"]["hit"] or a["raining_now"]) and not st.get("warned_rain_back"):
            word = "雪" if a["rain_back"]["snow"] else "雨"
            when = _fmt_hours(a["rain_back"]["hours"]) or "现在"
            if st.get("warned_rain_out"):
                ev_txt = f"🌂 下班路上也有{word}：{when}（概率 {a['rain_back']['max_prob']}%）。早上已提醒带伞，走的时候别忘把伞拿上。"
            else:
                ev_txt = f"🌂 下班路上有{word}：{when}（概率 {a['rain_back']['max_prob']}%）。早上预报没有，伞没带的话公司前台/便利店借一把，或者等雨小点再走。"
            sent = _push(
                db,
                ev_txt,
                "commute_weather",
                f"care:weather:{today.isoformat()}:evening",
                bypass_quiet=True,
            )
            st["warned_rain_back"] = True
    _save_weather_state(db, today, st)
    return {"ok": True, "phase": phase, "analysis": a, "sent": bool(sent and (sent.get("delivered") or sent.get("queued_for_morning")))}


# ---------------------------------------------------------------------------
# 2. 周期用药注射 / 剩余针数 / 挂号开药
# ---------------------------------------------------------------------------


def _inj_cfg(cfg: dict[str, Any]) -> dict[str, Any]:
    return cfg["injection"]


def dose_slot_for(cfg: dict[str, Any], now: datetime) -> tuple[date, datetime]:
    """返回「当前所属的那一针」：(计划注射日=周三, 提醒时刻)。周三那针在周四凌晨提醒。"""
    ic = _inj_cfg(cfg)
    wd = int(ic.get("weekday", 2))
    offset = 1 if ic.get("remind_next_day", True) else 0
    probe = (now - timedelta(days=offset)).date()
    back = (probe.weekday() - wd) % 7
    slot = probe - timedelta(days=back)
    fire = _at(slot + timedelta(days=offset), str(ic.get("remind_at") or "00:50"))
    if fire > now:
        slot = slot - timedelta(days=7)
        fire = fire - timedelta(days=7)
    return slot, fire


def get_pen_state(db: SimingDB) -> dict[str, Any]:
    return _meta_json(db, "care_pen", {"doses_left": None, "last_visit": "", "updated_at": ""})


def _save_pen_state(db: SimingDB, st: dict[str, Any]) -> None:
    st["updated_at"] = iso_now()
    _set_meta_json(db, "care_pen", st)


def _injection_log(db: SimingDB) -> list[dict[str, Any]]:
    return list(_meta_json(db, "care_injection_log", []))


def _open_case(db: SimingDB, category: str) -> Optional[dict[str, Any]]:
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT case_id, title, next_step, context_json, created_at FROM cases WHERE category=? AND status='待处理' ORDER BY created_at DESC LIMIT 1",
            (category,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _create_case(db: SimingDB, title: str, next_step: str, done_rule: str, category: str, rule_id: str, ctx: dict[str, Any], due_at: Optional[str] = None) -> str:
    ts = iso_now()
    with db.transaction() as conn:
        cid = db.next_seq_id(conn, "S")
        conn.execute(
            """
            INSERT INTO cases (case_id, title, next_step, due_at, done_rule, status, strength, category, rule_id, context_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, '待处理', 'NORMAL', ?, ?, ?, ?, ?)
            """,
            (cid, title, next_step, due_at, done_rule, category, rule_id, json.dumps(ctx, ensure_ascii=False), ts, ts),
        )
    return cid


def _close_case(db: SimingDB, case_id: str, status: str = "完成") -> None:
    ts = iso_now()
    with db.transaction() as conn:
        conn.execute(
            "UPDATE cases SET status=?, closed_at=?, state_version=state_version+1, updated_at=? WHERE case_id=? AND status='待处理'",
            (status, ts, ts, case_id),
        )
        sub_state = "CANCELLED" if status == "取消" else "ENDED"
        conn.execute("UPDATE subscriptions SET state=?, ended_at=? WHERE target_id=?", (sub_state, ts, case_id))


def _doses_left_text(pen: dict[str, Any]) -> str:
    dl = pen.get("doses_left")
    return f"剩 {dl} 针" if isinstance(dl, int) else "剩余针数未登记"


def run_injection_guard(
    db: SimingDB,
    now: Optional[datetime] = None,
    desktop_probe: Optional[Callable[[], dict[str, Any]]] = None,
) -> dict[str, Any]:
    if not _module_on(db, "med_care"):
        return {"skipped": "module_disabled"}
    cfg = load_care_cfg(db)
    ic = _inj_cfg(cfg)
    now = now or now_local()
    slot, fire = dose_slot_for(cfg, now)
    drug = str(ic.get("drug") or "周期用药")
    log = _injection_log(db)
    done_slots = {str(x.get("slot")) for x in log}
    out: dict[str, Any] = {"slot": slot.isoformat()}
    out["hospital"] = _maybe_hospital_reminder(db, cfg, now)
    if slot.isoformat() in done_slots:
        return {**out, "skipped": "already_logged"}

    case = _open_case(db, "med_injection")
    if case and json.loads(case.get("context_json") or "{}").get("slot") != slot.isoformat():
        # 上一针的卡还开着：超过漏打窗口就按「未登记」收掉，不再追
        ctx_old = json.loads(case.get("context_json") or "{}")
        old_fire = parse_iso(ctx_old.get("fire_at")) if ctx_old.get("fire_at") else None
        if old_fire and (now - old_fire).total_seconds() / 3600 >= float(ic.get("missed_after_h", 96)):
            _close_case(db, case["case_id"], status="取消")
            case = None
    pen = get_pen_state(db)
    # 空间门控：若配置了实时定位且明确显示本人仍在「公司」或外出途中（不在「家」），暂缓创建首条催打针卡，等进家门后再提醒
    at_home_or_unknown = True
    raw_loc = db.get_meta("latest_ios_location") or ""
    if raw_loc:
        try:
            loc_obj = json.loads(raw_loc)
            upd_s = str(loc_obj.get("updated_at") or "")
            if upd_s and abs((now - parse_iso(upd_s)).total_seconds()) <= 7200:
                p_alias = str(loc_obj.get("place_alias") or "")
                if p_alias and p_alias != "家":
                    at_home_or_unknown = False
        except Exception:
            pass

    if not case and now >= fire and (now - fire) < timedelta(hours=20):
        if not at_home_or_unknown:
            return {**out, "deferred": "not_at_home_yet"}
        wd = "一二三四五六日"[slot.weekday()]
        title = f"{drug} 周{wd}这针（{slot.strftime('%m-%d')}）"
        cid = _create_case(
            db,
            title,
            "洗完澡打针：1 个新针头 + 酒精棉片消毒",
            "回「打了」",
            "med_injection",
            "med_care",
            {"slot": slot.isoformat(), "fire_at": iso_now(fire), "last_nudge_at": iso_now(now), "awake_nudges": 0},
        )
        text = (
            f"💉 [{cid}] 今晚该打{drug}了（周{wd}这针 · {_doses_left_text(pen)}）\n"
            "换新针头、棉片消毒，打完引用本条回「打了」。这周不打回「跳过」。"
        )
        _push(db, text, "med_care", cid, bypass_quiet=True, hard=True)
        out["created"] = cid
        return out
    if case:
        ctx = json.loads(case.get("context_json") or "{}")
        if ctx.get("slot") == slot.isoformat():
            # Directive #2：只要本人还醒着（AWAKE_ACTIVE）且在家、还没回「打了」，每隔 60 分钟持续追催（睡着即刻停催）！
            from siming.attention import evaluate_attention_state

            att = evaluate_attention_state(db, desktop_probe=desktop_probe, now_override=now)
            nudge_interval_min = float(ic.get("awake_nudge_interval_min", 60))
            max_nudges = int(ic.get("max_awake_nudges", 4))
            cur_nudges = int(ctx.get("awake_nudges") or 0)
            anchor_s = str(ctx.get("last_nudge_at") or case.get("created_at") or ctx.get("fire_at") or "")
            if (
                at_home_or_unknown
                and (now - fire) < timedelta(hours=10)
                and att.get("state") == "AWAKE_ACTIVE"
                and cur_nudges < max_nudges
                and anchor_s
            ):
                elapsed_min = (now - parse_iso(anchor_s)).total_seconds() / 60.0
                if elapsed_min >= nudge_interval_min:
                    cur_nudges += 1
                    nudge_txt = (
                        f"💉 [{case['case_id']}] 看你还没睡，这周的{drug}还没回「打了」（第 {cur_nudges + 1} 次提醒 · {_doses_left_text(pen)}）——"
                        "趁睡前顺手打完，回一句「打了」我就不再催啦！"
                    )
                    _push(db, nudge_txt, "med_care", f"{case['case_id']}:nudge{cur_nudges}", bypass_quiet=True, hard=True)
                    ctx["awake_nudges"] = cur_nudges
                    ctx["last_nudge_at"] = iso_now(now)
                    with db.transaction() as conn:
                        conn.execute(
                            "UPDATE cases SET context_json=?, updated_at=? WHERE case_id=?",
                            (json.dumps(ctx, ensure_ascii=False), iso_now(), case["case_id"]),
                        )
                    out["awake_nudged"] = cur_nudges
                    return out

            if not ctx.get("followed_up"):
                f_at = fire + timedelta(hours=float(ic.get("followup_after_h", 22.5)))
                if now >= f_at and at_home_or_unknown:
                    text = (
                        f"💉 [{case['case_id']}] 昨晚那针{drug}还没登记。打过了回「打了」；"
                        "还没打的话今晚补上（说明书：漏打 4 天内可补，超过就跳过这针，下周三照常）。"
                    )
                    _push(db, text, "med_care", case["case_id"], bypass_quiet=True, hard=True)
                    ctx["followed_up"] = iso_now(now)
                    with db.transaction() as conn:
                        conn.execute("UPDATE cases SET context_json=?, updated_at=? WHERE case_id=?", (json.dumps(ctx, ensure_ascii=False), iso_now(), case["case_id"]))
                    out["followed_up"] = case["case_id"]
    return out


def _in_injection_window(db: SimingDB, now: Optional[datetime] = None) -> bool:
    cfg = load_care_cfg(db)
    now = now or now_local()
    wd = int(_inj_cfg(cfg).get("weekday", 2))
    ref_day = logical_today(now)
    start = _at(ref_day - timedelta(days=(ref_day.weekday() - wd) % 7), "12:00")
    return start <= now <= start + timedelta(hours=float(_inj_cfg(cfg).get("missed_after_h", 96)) + 12)


def sync_injection_case_closed(
    db: SimingDB,
    case_id: str,
    *,
    cancel: bool = False,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """单真源原子钩子：由 `close_case` / `siming done Sxx` 在关闭 `med_injection` 单据时联动更新 `care_injection_log` 与剩余针数，杜绝 1 分钟后重推。"""
    now = now or now_local()
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT case_id, category, context_json FROM cases WHERE case_id=?",
            (case_id.strip().upper(),),
        ).fetchone()
    finally:
        conn.close()
    if not row or str(row["category"] or "") != "med_injection":
        return {"synced": False}
    try:
        ctx = json.loads(row["context_json"] or "{}")
    except Exception:
        ctx = {}
    slot_s = str(ctx.get("slot") or "")
    if not slot_s:
        cfg = load_care_cfg(db)
        slot_d, _ = dose_slot_for(cfg, now)
        slot_s = slot_d.isoformat()

    log = _injection_log(db)
    if any(str(x.get("slot")) == slot_s for x in log):
        return {"synced": True, "duplicate": True, "slot": slot_s}

    log.append({"slot": slot_s, "at": iso_now(now), "skipped": bool(cancel)})
    _set_meta_json(db, "care_injection_log", log[-120:])
    db.record_human_use("med_care", "skip_injection" if cancel else "record_injection", ref_id=slot_s)
    pen = get_pen_state(db)
    if not cancel and isinstance(pen.get("doses_left"), int):
        pen["doses_left"] = max(0, int(pen["doses_left"]) - 1)
        _save_pen_state(db, pen)
    return {"synced": True, "slot": slot_s, "skipped": bool(cancel), "doses_left": pen.get("doses_left")}


def record_injection(db: SimingDB, now: Optional[datetime] = None, skipped: bool = False) -> dict[str, Any]:
    cfg = load_care_cfg(db)
    ic = _inj_cfg(cfg)
    now = now or now_local()
    case = _open_case(db, "med_injection")
    if case:
        slot_s = str(json.loads(case.get("context_json") or "{}").get("slot") or "")
    else:
        # 没有开着的卡（比如提前打了）：归到最近的计划日
        slot_d, _fire = dose_slot_for(cfg, now + timedelta(days=2))
        slot_s = slot_d.isoformat()
    log = _injection_log(db)
    if any(str(x.get("slot")) == slot_s for x in log):
        if case:
            _close_case(db, case["case_id"])
        return {"ok": True, "duplicate": True, "summary": f"这针（{slot_s}）已经登记过了。"}
    log.append({"slot": slot_s, "at": iso_now(now), "skipped": skipped})
    _set_meta_json(db, "care_injection_log", log[-120:])
    db.record_human_use("med_care", "skip_injection" if skipped else "record_injection", ref_id=slot_s)
    if case:
        from siming.router import record_feedback

        _close_case(db, case["case_id"], status="完成" if not skipped else "取消")
        record_feedback(db, case["case_id"], "已办", note="跳过" if skipped else "打了")
    pen = get_pen_state(db)
    drug = str(ic.get("drug") or "周期用药")
    if skipped:
        return {"ok": True, "summary": f"好，{slot_s} 这针记为跳过，下周三照常提醒（{_doses_left_text(pen)}）。"}
    lines = [f"✅ 已登记 {drug} {slot_s} 这针。"]
    if isinstance(pen.get("doses_left"), int):
        pen["doses_left"] = max(0, int(pen["doses_left"]) - 1)
        _save_pen_state(db, pen)
        lines.append(f"• 笔里还剩 {pen['doses_left']} 针")
    else:
        lines.append("• 还不知道笔里剩几针：回「药笔还剩 N 针」登记后，我按针数倒推挂号日")
    hosp = _maybe_hospital_reminder(db, cfg, now, push=False)
    if hosp.get("text"):
        lines.append(hosp["text"])
    elif hosp.get("schedule_hint"):
        lines.append(hosp["schedule_hint"])
    low = [s for s in estimate_supplies(db, now) if s.get("status") == "low" and s["key"] in ("needle", "alcohol_pad")]
    for s in low:
        lines.append(f"• {s['label']}只够 {s['days_left']:.0f} 天左右，记得补")
    return {"ok": True, "summary": "\n".join(lines)}


def _pen_window(db: SimingDB, cfg: dict[str, Any], now: datetime) -> Optional[dict[str, Any]]:
    """推算当前这支笔的完整时间轴：
    - final_dose_slot：本支笔最后一针（第4针）日期
    - booking_anchor_slot：第3针日期（第3→4针那周起点，即真实去医院的一周前）
    - booking_days：提醒在 APP 预约挂号的日期（默认第3→4针那周的周三、周四）
    - visit_start ~ next_pen_slot：第4针打完到新管第1针之间的去医院开药周
    """
    ic = _inj_cfg(cfg)
    pen = get_pen_state(db)
    lead = max(1, int(ic.get("hospital_lead_doses", 1)))
    per_pen = max(1, int(ic.get("doses_per_pen", 4)))
    wd = int(ic.get("weekday", 2))

    final_dose_slot: Optional[date] = None
    next_slot: Optional[date] = None
    if isinstance(pen.get("doses_left"), int):
        dl = max(0, int(pen["doses_left"]))
        cur_slot, cur_fire = dose_slot_for(cfg, now)
        done_slots = {str(x.get("slot")) for x in _injection_log(db)}
        open_inj = _open_case(db, "med_injection")
        open_slot = str(json.loads(open_inj.get("context_json") or "{}").get("slot") or "") if open_inj else ""
        cand = cur_slot
        if cand.isoformat() in done_slots:
            while cand.isoformat() in done_slots:
                cand += timedelta(days=7)
        elif open_slot != cand.isoformat() and (now - cur_fire) >= timedelta(hours=20):
            cand += timedelta(days=7)
        next_slot = cand
        final_dose_slot = next_slot + timedelta(days=(dl - 1) * 7)
    elif pen.get("last_visit"):
        try:
            last_v = date.fromisoformat(str(pen["last_visit"]))
            first_slot = last_v + timedelta(days=(wd - last_v.weekday()) % 7)
            final_dose_slot = first_slot + timedelta(days=(per_pen - 1) * 7)
        except ValueError:
            return None
    if final_dose_slot is None:
        return None

    booking_anchor_slot = final_dose_slot - timedelta(days=lead * 7)
    b_wds = [int(x) for x in (ic.get("booking_weekdays") or [2, 3])]
    booking_days = sorted(
        {booking_anchor_slot + timedelta(days=(bwd - booking_anchor_slot.weekday()) % 7) for bwd in b_wds}
    )
    visit_start = final_dose_slot + timedelta(days=1)
    next_pen_slot = final_dose_slot + timedelta(days=7)
    return {
        "doses_left": pen.get("doses_left"),
        "next_slot": next_slot,
        "booking_anchor_slot": booking_anchor_slot,
        "booking_days": booking_days,
        "final_dose_slot": final_dose_slot,
        "visit_start": visit_start,
        "next_pen_slot": next_pen_slot,
    }


def _fmt_day_wd(d: date) -> str:
    return f"{d.strftime('%m-%d')}(周{'一二三四五六日'[d.weekday()]})"


def _maybe_hospital_reminder(db: SimingDB, cfg: dict[str, Any], now: datetime, push: bool = True) -> dict[str, Any]:
    ic = _inj_cfg(cfg)
    win = _pen_window(db, cfg, now)
    if not win:
        return {}
    drug = str(ic.get("drug") or "周期用药")
    today = now.date()
    booking_anchor: date = win["booking_anchor_slot"]
    booking_days: list[date] = win["booking_days"]
    final_slot: date = win["final_dose_slot"]
    visit_start: date = win["visit_start"]
    next_pen_slot: date = win["next_pen_slot"]
    b_days_str = " / ".join(_fmt_day_wd(d) for d in booking_days)

    # 还没到「第3针→第4针那周」（即距离真实去医院 >1 周）：绝不提前发预约提醒
    if today < booking_anchor:
        dl = win.get("doses_left")
        hint = ""
        if isinstance(dl, int) and dl <= 2:
            hint = (
                f"• 预约挂号提醒排在 {b_days_str}"
                f"（第 4 针 {final_slot.strftime('%m-%d')}，提前一周约 {visit_start.strftime('%m-%d')}～{next_pen_slot.strftime('%m-%d')} 就诊）"
            )
        return {"schedule_hint": hint} if hint else {}

    hospital = str(ic.get("hospital") or "")
    slot_hint = ""
    if hospital:
        try:
            from siming.cn_calendar import hospital_114_alert_window

            w = hospital_114_alert_window(today + timedelta(days=7), hospital, paths=db.paths)
            if w.get("known"):
                slot_hint = f"（{hospital} 每天 {w['release_time']} 放 7 天后的号）"
        except Exception:
            pass

    visit_range = f"{visit_start.strftime('%m-%d')}～{next_pen_slot.strftime('%m-%d')}"
    is_visit_week = today >= visit_start
    if is_visit_week:
        preview_text = (
            f"🏥 这支笔已打完（第 4 针 {final_slot.strftime('%m-%d')}），本周（{visit_range}）记得去医院续开{drug}"
            f"（新管第 1 针 {next_pen_slot.strftime('%m-%d')}），开完回「开药了 N支」。"
        )
    else:
        preview_text = (
            f"🏥 该在 APP 预约下周开{drug}的门诊号了：本周是第 3→4 针之间"
            f"（第 4 针 {final_slot.strftime('%m-%d')}，下周 {visit_range} 去医院续开新管），"
            f"周三/周四提前一周约号{slot_hint}。约好回「已约」，开完药回「开药了 N支」。"
        )

    case = _open_case(db, "med_refill")
    ctx = json.loads(case.get("context_json") or "{}") if case else {}
    already_booked = bool(ctx.get("booked") or (case and "已预约" in str(case.get("next_step") or "")))
    if already_booked:
        return {"text": "", "case": case["case_id"], "booked": True}
    if not push:
        return {"text": "• " + preview_text}

    remind_hm = str(ic.get("booking_remind_at") or "09:30")
    if now < _at(today, remind_hm):
        return {"text": "", "skipped": "before_remind_time"}

    sig = f"pen_final:{final_slot.isoformat()}"
    prev = _meta_json(db, "care_refill_alerted", {})
    alerted_days: list[str] = list(prev.get("days") or []) if prev.get("sig") == sig else []
    reminded_days: list[str] = list(ctx.get("reminded_days") or []) if ctx.get("sig") == sig else []

    today_s = today.isoformat()
    in_booking_day = today in booking_days
    # 若周三和周四整天关机漏跑，允许在第3→4针那周内补推一次；正常已推过周三/周四则周五~周二静默
    missed_both_booking_days = (booking_anchor < today <= final_slot) and not alerted_days and not reminded_days
    need_visit_week_nudge = is_visit_week and ("visit_week" not in alerted_days) and (not case)

    if not (in_booking_day or missed_both_booking_days or need_visit_week_nudge):
        return {"text": "", "case": case["case_id"] if case else ""}

    tag = "visit_week" if is_visit_week else today_s
    if tag in alerted_days or tag in reminded_days:
        return {"text": "", "suppressed": True, "case": case["case_id"] if case else ""}

    # 若用户已手工取消过本周期的卡，同周期不再在周四重新建新卡打扰
    if not case and alerted_days and not is_visit_week:
        return {"text": "", "suppressed": True}

    is_second_booking_day = in_booking_day and len(booking_days) >= 2 and today == booking_days[-1] and bool(reminded_days)
    if is_visit_week:
        push_body = preview_text
    elif is_second_booking_day:
        wd_cn = "一二三四五六日"[today.weekday()]
        push_body = (
            f"🏥 周{wd_cn}再提醒一次：记得在 APP 预约下周去医院开{drug}的门诊号"
            f"（提前一周约 {visit_range}，第 4 针 {final_slot.strftime('%m-%d')} 打完后去医院）{slot_hint}。"
            "约好回「已约」，开完药回「开药了 N支」。"
        )
    else:
        push_body = preview_text

    new_reminded = reminded_days + [tag]
    new_alerted = alerted_days + [tag]
    if not case:
        new_ctx = {
            "sig": sig,
            "final_slot": final_slot.isoformat(),
            "visit_range": visit_range,
            "reminded_days": new_reminded,
            "booked": False,
        }
        cid = _create_case(
            db,
            f"预约挂号开{drug}",
            f"提前一周在 APP 预约 {visit_range} 门诊 → 开完药回「开药了 N支」",
            "约好回「已约」/ 开完回「开药了 N支」",
            "med_refill",
            "med_care",
            new_ctx,
            due_at=next_pen_slot.isoformat(),
        )
        _push(db, f"[{cid}] {push_body}", "med_care", cid, bypass_quiet=False, hard=True)
    else:
        cid = case["case_id"]
        ctx["sig"] = sig
        ctx["reminded_days"] = new_reminded
        with db.transaction() as conn:
            conn.execute(
                "UPDATE cases SET context_json=?, updated_at=? WHERE case_id=?",
                (json.dumps(ctx, ensure_ascii=False), iso_now(now), cid),
            )
        _push(db, f"[{cid}] {push_body}", "med_care", f"{cid}:{tag}", bypass_quiet=False, hard=True)

    _set_meta_json(db, "care_refill_alerted", {"sig": sig, "days": new_alerted, "at": iso_now(now)})
    return {"text": push_body, "case": cid}


def record_booking(db: SimingDB, now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or now_local()
    db.record_human_use("med_care", "record_booking")
    case = _open_case(db, "med_refill")
    if case:
        ctx = json.loads(case.get("context_json") or "{}")
        ctx["booked"] = True
        ctx["booked_at"] = iso_now(now)
        cur_step = str(case.get("next_step") or "")
        new_step = cur_step if cur_step.startswith("已预约") else "已预约下周门诊，就诊开完药回「开药了 N支」"
        with db.transaction() as conn:
            conn.execute(
                "UPDATE cases SET next_step=?, context_json=?, updated_at=? WHERE case_id=?",
                (new_step, json.dumps(ctx, ensure_ascii=False), iso_now(now), case["case_id"]),
            )
        return {"ok": True, "summary": f"✅ 已记下 [{case['case_id']}] 门诊已预约，不再重复催约号；下周开完药回「开药了 N支」自动补针数。"}
    return {"ok": True, "summary": "✅ 已记下门诊预约；下周开完药回「开药了 N支」即可更新针数。"}


def record_refill(db: SimingDB, pens: int, now: Optional[datetime] = None) -> dict[str, Any]:
    cfg = load_care_cfg(db)
    now = now or now_local()
    per_pen = int(_inj_cfg(cfg).get("doses_per_pen", 4))
    pen = get_pen_state(db)
    base = int(pen["doses_left"]) if isinstance(pen.get("doses_left"), int) else 0
    pen["doses_left"] = base + max(1, pens) * per_pen
    pen["last_visit"] = now.date().isoformat()
    _save_pen_state(db, pen)
    _set_meta_json(db, "care_refill_alerted", {})
    db.record_human_use("med_care", "record_refill")
    case = _open_case(db, "med_refill")
    if case:
        from siming.router import record_feedback

        _close_case(db, case["case_id"])
        record_feedback(db, case["case_id"], "已办", note=f"开药{max(1, pens)}支")
    return {"ok": True, "summary": f"✅ 已登记开药 {max(1, pens)} 支，现在共 {pen['doses_left']} 针（约 {pen['doses_left']} 周）。"}


def set_doses_left(db: SimingDB, n: int) -> dict[str, Any]:
    pen = get_pen_state(db)
    pen["doses_left"] = max(0, int(n))
    _save_pen_state(db, pen)
    db.record_human_use("med_care", "set_doses_left")
    cfg = load_care_cfg(db)
    hosp = _maybe_hospital_reminder(db, cfg, now_local(), push=False)
    extra = hosp.get("text") or hosp.get("schedule_hint") or ""
    tail = f"\n{extra}" if extra else ""
    drug = str(_inj_cfg(cfg).get("drug") or "周期用药")
    return {"ok": True, "summary": f"✅ 记下了：{drug}还剩 {pen['doses_left']} 针。{tail}"}


# ---------------------------------------------------------------------------
# 3. 补货雷达（美团订单 → 库存模拟）
# ---------------------------------------------------------------------------


def _units_per_item(name: str, unit: str) -> int:
    """「海氏海诺医用外科口罩 50只/盒」→ 50；「针头 0.23*4mm(32G*4mm)*7支/盒」→ 7；「10只*5包」→ 50。"""
    u = re.escape(unit)
    m_mul = re.search(rf"(\d{{1,4}})\s*{u}\s*[*xX×]\s*(\d{{1,3}})\s*(?:包|盒|袋)", name)
    if m_mul:
        return int(m_mul.group(1)) * int(m_mul.group(2))
    hits = re.findall(rf"(\d{{1,4}})\s*{u}", name)
    if hits:
        return int(hits[-1])
    return 1


def _purchases_for(db: SimingDB, scfg: dict[str, Any]) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT shop, ordered_at, order_status, item_name, qty FROM shop_order_items ORDER BY ordered_at"
        ).fetchall()
    finally:
        conn.close()
    inc = re.compile(str(scfg.get("match") or "^$"))
    exc = re.compile(str(scfg["exclude"])) if scfg.get("exclude") else None
    out = []
    for r in rows:
        name = str(r["item_name"])
        if not inc.search(name) or (exc and exc.search(name)):
            continue
        if re.search(r"(取消|已退款|退款成功|全额退款)", str(r["order_status"] or "")):
            continue
        units = _units_per_item(name, str(scfg.get("unit") or "")) * int(r["qty"] or 1)
        out.append({"at": str(r["ordered_at"]), "shop": str(r["shop"]), "name": name, "qty": int(r["qty"] or 1), "units": units})
    return out


def _rate_per_day(scfg: dict[str, Any], purchases: list[dict[str, Any]]) -> tuple[float, str]:
    if scfg.get("per_day"):
        return float(scfg["per_day"]), "每天"
    base = float(scfg.get("per_injection") or 1.0) / 7.0
    if scfg.get("learn_rate") and len(purchases) >= 2:
        first, last = parse_iso(purchases[0]["at"]), parse_iso(purchases[-1]["at"])
        span = (last - first).total_seconds() / 86400
        if span >= 30:
            learned = sum(p["units"] for p in purchases[:-1]) / span
            if learned > base:
                return min(learned, 5.0), "按历史购买间隔学出"
    return base, "每周一针"


def estimate_supplies(db: SimingDB, now: Optional[datetime] = None) -> list[dict[str, Any]]:
    cfg = load_care_cfg(db)
    now = now or now_local()
    out = []
    for key, scfg in (cfg.get("supplies") or {}).items():
        purchases = _purchases_for(db, scfg)
        anchor = _meta_json(db, f"care_supply_anchor:{key}", None)
        rate, rate_src = _rate_per_day(scfg, purchases)
        events: list[tuple[datetime, float]] = [(parse_iso(p["at"]), float(p["units"])) for p in purchases]
        stock, cur_t = 0.0, None
        if anchor and anchor.get("at"):
            cur_t = parse_iso(anchor["at"])
            stock = float(anchor.get("stock") or 0)
            events = [e for e in events if e[0] > cur_t]
        for t, units in events:
            if cur_t is not None:
                stock = max(0.0, stock - rate * (t - cur_t).total_seconds() / 86400)
            stock += units
            cur_t = t
        if cur_t is None:
            out.append({"key": key, "label": scfg.get("label", key), "status": "unknown", "rate": rate, "rate_src": rate_src, "unit": scfg.get("unit", "")})
            continue
        stock = max(0.0, stock - rate * max(0.0, (now - cur_t).total_seconds()) / 86400)
        days_left = stock / rate if rate > 0 else 999.0
        lead = float(scfg.get("lead_days") or 5)
        last = purchases[-1] if purchases else None
        out.append(
            {
                "key": key,
                "label": scfg.get("label", key),
                "unit": scfg.get("unit", ""),
                "stock": round(stock, 1),
                "rate": rate,
                "rate_src": rate_src,
                "days_left": round(days_left, 1),
                "run_out": (now + timedelta(days=days_left)).date().isoformat(),
                "lead_days": lead,
                "status": "low" if days_left <= lead else "ok",
                "last": last,
                "anchored": bool(anchor),
            }
        )
    return out


def set_supply_stock(db: SimingDB, key: str, stock: int, now: Optional[datetime] = None) -> dict[str, Any]:
    now = now or now_local()
    _set_meta_json(db, f"care_supply_anchor:{key}", {"stock": int(stock), "at": iso_now(now)})
    db.set_meta(f"care_supply_alert:{key}", "")
    db.record_human_use("supply_radar", "set_stock", ref_id=key)
    item = next((s for s in estimate_supplies(db, now) if s["key"] == key), None)
    if not item or item.get("status") == "unknown":
        return {"ok": True, "summary": f"✅ 记下了：{key} 还剩 {stock}。"}
    return {"ok": True, "summary": f"✅ 记下了：{item['label']}还剩 {stock}{item['unit']}，按{item['rate_src']}约够 {item['days_left']:.0f} 天（{item['run_out'][5:]} 左右用完）。"}


def _reorder_hint(s: dict[str, Any]) -> str:
    last = s.get("last") or {}
    if not last:
        return ""
    name = re.sub(r"[【\[].*?[】\]]", "", str(last.get("name") or "")).strip()[:28]
    return f"上次 {str(last.get('at'))[5:10]} 在「{last.get('shop')}」买的 {name} ×{last.get('qty')}，美团订单里点「再来一单」最快"


def supply_lines_for_departure(db: SimingDB, now: datetime) -> list[str]:
    """出门卡顺带一句：口罩快没了（出门就要用）。"""
    if not _module_on(db, "supply_radar"):
        return []
    try:
        items = estimate_supplies(db, now)
    except Exception:
        return []
    m = next((s for s in items if s["key"] == "mask" and s.get("status") == "low"), None)
    if not m:
        return []
    return [f"😷 口罩估计只剩 {m['stock']:.0f} 只（约 {m['days_left']:.0f} 天），今天顺手下单补一盒"]


def run_supply_radar(db: SimingDB, now: Optional[datetime] = None, remind_at: str = "22:30") -> dict[str, Any]:
    if not _module_on(db, "supply_radar"):
        return {"skipped": "module_disabled", "pushed": []}
    now = now or now_local()
    if now < _at(now.date(), remind_at):
        return {"skipped": "not_due"}
    pushed = []
    for s in estimate_supplies(db, now):
        if s.get("status") != "low":
            continue
        key = s["key"]
        last_alert = _meta_json(db, f"care_supply_alert:{key}", None)
        if last_alert and last_alert.get("at"):
            same_batch = last_alert.get("last_at") == (s.get("last") or {}).get("at")
            if same_batch and (now - parse_iso(last_alert["at"])) < timedelta(days=3):
                continue
        hint = _reorder_hint(s)
        text = f"🛒 {s['label']}快用完了：估计还剩 {s['stock']:.0f}{s['unit']}，约 {s['days_left']:.0f} 天（{s['run_out'][5:]} 前后）。"
        if hint:
            text += f"\n{hint}。"
        text += f"\n实际数量不对就回「{s['label']}还剩 N」校准。"
        _push(db, text, "supply_radar", f"care:supply:{key}", bypass_quiet=False)
        _set_meta_json(db, f"care_supply_alert:{key}", {"at": iso_now(now), "last_at": (s.get("last") or {}).get("at")})
        pushed.append(key)
    return {"pushed": pushed}


def _maybe_harvest(db: SimingDB, now: datetime, cfg: dict[str, Any]) -> dict[str, Any]:
    hc = cfg.get("harvest") or {}
    if not (_at(now.date(), str(hc.get("active_from", "10:00"))) <= now <= _at(now.date(), str(hc.get("active_to", "23:30")))):
        return {"skipped": "inactive_hours"}
    last = db.get_meta("meituan_orders_last_try_at") or ""
    if last and (now - parse_iso(last)) < timedelta(hours=float(hc.get("min_interval_h", 8))):
        return {"skipped": "recent"}
    from siming.collectors.meituan_orders import collect_meituan_orders

    return collect_meituan_orders(db)


# ---------------------------------------------------------------------------
# 主节拍入口 / 报告 / QQ 指令
# ---------------------------------------------------------------------------


def run_daily_care(db: SimingDB, now: Optional[datetime] = None) -> dict[str, Any]:
    """由 evald.run_tick 每 2 分钟调用；各段独立异常隔离，任何一段失败都不影响主节拍。"""
    now = now or now_local()
    cfg = load_care_cfg(db)
    res: dict[str, Any] = {}
    steps: list[tuple[str, str, Callable[[], Any]]] = [
        ("weather", "commute_weather", lambda: run_weather_guard(db, now)),
        ("injection", "med_care", lambda: run_injection_guard(db, now)),
        ("harvest", "supply_radar", lambda: _maybe_harvest(db, now, cfg)),
        ("supply", "supply_radar", lambda: run_supply_radar(db, now)),
    ]
    for name, module_id, fn in steps:
        if not _module_on(db, module_id):
            res[name] = {"skipped": "module_off"}
            continue
        try:
            res[name] = fn()
        except Exception as exc:
            res[name] = {"ok": False, "error": str(exc)[:200]}
    return res


def format_care_report(db: SimingDB, now: Optional[datetime] = None) -> str:
    cfg = load_care_cfg(db)
    now = now or now_local()
    ref_day = logical_today(now)
    regime = active_regime(cfg, ref_day)
    ic = _inj_cfg(cfg)
    pen = get_pen_state(db)
    _cur_slot, cur_fire = dose_slot_for(cfg, now)
    nxt_fire = cur_fire + timedelta(days=7)
    open_inj = _open_case(db, "med_injection")
    log = _injection_log(db)
    lines = ["🩺 日常照护面板"]
    lines.append(
        f"• 通勤：{regime.get('home_label')} → {regime.get('office_label')}（{regime.get('commute')} ~{regime.get('commute_min')} 分钟），"
        f"工作日 {regime.get('leave_home_at')} 出门、{cfg['work']['clock_in_by']} 前打卡、{cfg['work']['usual_leave_office']} 后下班；"
        f"出门前 {cfg['weather']['remind_lead_min']} 分钟查雨和温差"
    )
    nxt = next((r for r in cfg.get("regimes") or [] if str(r.get("from") or "") > ref_day.isoformat()), None)
    if nxt:
        lines.append(f"• {nxt['from']} 起切到 {nxt['home_label']}（{nxt['commute']}，{nxt['leave_home_at']} 出门）")
    last_inj = next((x for x in reversed(log) if not x.get("skipped")), None)
    hosp_label = f" · 开药医院 {ic['hospital']}" if ic.get("hospital") else ""
    lines.append(
        f"• {ic.get('drug')}：每周三（周四 {ic.get('remind_at')} 提醒）· {_doses_left_text(pen)}"
        + (f" · 上次 {last_inj['slot']}" if last_inj else "")
        + (f" · 上次开药 {pen['last_visit']}" if pen.get("last_visit") else "")
        + hosp_label
    )
    if open_inj:
        lines.append(f"  ⏳ 当前待打：[{open_inj['case_id']}] {open_inj['title']}（打完回「打了」）")
    lines.append(f"  下一针提醒：{nxt_fire.strftime('%m-%d %H:%M')}")
    pwin = _pen_window(db, cfg, now)
    if pwin:
        b_days_s = " / ".join(_fmt_day_wd(d) for d in pwin["booking_days"])
        lines.append(
            f"  预约挂号提醒：{b_days_s}（第 4 针 {pwin['final_dose_slot'].strftime('%m-%d')}，"
            f"提前一周约 {pwin['visit_start'].strftime('%m-%d')}～{pwin['next_pen_slot'].strftime('%m-%d')} 就诊）"
        )
    lines.append("• 补货雷达（美团订单 → 余量）：")
    for s in estimate_supplies(db, now):
        if s.get("status") == "unknown":
            lines.append(f"  · {s['label']}：近 8 个月美团订单里没找到，回「{s['label']}还剩 N」登记")
            continue
        badge = "🔴" if s["status"] == "low" else "🟢"
        lines.append(
            f"  {badge} {s['label']}：约 {s['stock']:.0f}{s['unit']}，够 {s['days_left']:.0f} 天（{s['run_out'][5:]}）· {s['rate_src']} {s['rate']:.2f}{s['unit']}/天"
            + ("（已手动校准）" if s.get("anchored") else "")
        )
    last_ok = db.get_meta("meituan_orders_last_ok_at") or ""
    if last_ok:
        lines.append(f"  美团订单最近同步：{last_ok[5:16].replace('T', ' ')}")
    return "\n".join(lines)


_SUPPLY_ALIASES = {"口罩": "mask", "针头": "needle", "注射笔针头": "needle", "酒精棉片": "alcohol_pad", "棉片": "alcohol_pad"}
_CN_NUM_TOKEN = r"(?:\d{1,4}|[零〇一二两三四五六七八九十百]{1,4})"
_INJ_DONE_RE = re.compile(
    r"^(?:我)?(?:刚才|刚刚|已经|刚)?(?:这周的?|今晚的?)?(?:周期用药|药笔?|针)?(?:已经|刚)?(?:打了|打针了|打完针了?|打过针了?|打好针了?|已打|打完了?|针打了|针打完了?|打过了|打好了|注射完了?|注射过了)(?:啦|咯|哈|哦|吧)?[。!！~～]*$"
)
_INJ_SKIP_RE = re.compile(
    r"^(?:这周|本周|这针)?(?:先)?(?:跳过|跳过去|跳过这针|这针跳过|本周跳过|这周跳过|跳过本周|这周不打了?|本周不打了?|这针不打了?|漏打了?)(?:啦|咯|哈|吧)?[。!！~～]*$"
)
_DOSES_RE = re.compile(
    rf"^(?:(?:帮我)?(?:记|记录|更新|校准)(?:一下儿?)?[\s，,]*)?"
    rf"(?:(?:周期用药|药笔?|笔里)(?:现在|目前)?(?:笔里)?(?:还?剩|还有)\s*({_CN_NUM_TOKEN})\s*(?:针|次|剂)?|"
    rf"(?:现在|目前)?(?:还?剩|还有)\s*({_CN_NUM_TOKEN})\s*(?:针|次|剂)\s*(?:周期用药|药))(?:啦|咯|哈|哦)?[。!！~～]*$"
)
_BOOKED_RE = re.compile(
    r"^(?:我)?(?:已经|刚|刚才)?(?:去?医院)?(?:已约|约了|约好了?|预约了?|预约好了?|挂号了?|挂好了?|挂上号了?|挂上(?:医院的?)?号了?)(?:门诊)?(?:啦|咯|哈)?[。!！~～]*$"
)
_REFILL_RE = re.compile(
    rf"^(?:我)?(?:今天|刚才|刚刚|已经|刚)?(?:去医院)?(?:开药了?|拿药了?|开了药|开好药了?|补药了?|续开了?|开了)\s*"
    rf"(?:({_CN_NUM_TOKEN})\s*(?:支|笔|盒)\s*(?:周期用药|药)?|(?:周期用药|药)?[\s，,]*({_CN_NUM_TOKEN})?\s*(?:支|笔|盒)?)(?:啦|咯|哈)?[。!！~～]*$"
)
_STOCK_RE = re.compile(
    rf"^(?:(?:我)?(?:数了一下|看了下|帮我)?(?:记录|记|更新|校准)(?:一下儿?)?[\s，,]*)?"
    rf"(口罩|针头|注射笔针头|酒精棉片|棉片)\s*(?:现在|目前)?(?:还?剩|还有|剩)\s*({_CN_NUM_TOKEN})\s*(只|个|支|片|盒)?(?:左右)?(?:啦|咯|哈)?[。!！~～]*$"
)
_STOCK_INLINE_RE = re.compile(rf"(口罩|针头|注射笔针头|酒精棉片|棉片)\s*(?:现在|目前)?(?:还?剩|还有|剩)\s*({_CN_NUM_TOKEN})\s*(只|个|支|片|盒)?")


def _parse_small_cn_int(token: str) -> Optional[int]:
    s = (token or "").strip()
    if not s:
        return None
    if s.isdigit():
        return int(s)
    digits = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if s in digits:
        return digits[s]
    if "百" in s:
        p_h, _, rest = s.partition("百")
        h_val = digits.get(p_h, 1 if not p_h else -1)
        if h_val < 0:
            return None
        tail = 0
        if rest:
            t_v = _parse_small_cn_int(rest.lstrip("零〇"))
            if t_v is None:
                return None
            tail = t_v
        return h_val * 100 + tail
    if "十" in s:
        p_t, _, rest = s.partition("十")
        t_val = 1 if not p_t else digits.get(p_t, -1)
        if t_val < 0:
            return None
        o_val = 0 if not rest else digits.get(rest, -1)
        if o_val < 0:
            return None
        return t_val * 10 + o_val
    return None


def handle_care_inbound(db: SimingDB, text: str, reply_to_text: Optional[str] = None) -> Optional[dict[str, Any]]:
    t_raw = re.sub(r"^\s*\[Voice\]\s*", "", (text or "").strip(), flags=re.IGNORECASE).strip()
    if not t_raw:
        return None
    t = re.sub(r"[。！!~～\s]+$", "", t_raw).strip()
    t_q = re.sub(r"[。！？!?~～\s]+$", "", t_raw).strip()
    if not t_q:
        return None
    if t_q in ("照护", "日常照护", "耗材", "耗材余量", "补货雷达", "打针"):
        db.record_human_use("daily_care", "query_status")
        return {"handled": True, "reply": format_care_report(db)}
    cfg_in = load_care_cfg(db)
    drug_in = str(_inj_cfg(cfg_in).get("drug") or "周期用药").strip()
    hosp_in = str(_inj_cfg(cfg_in).get("hospital") or "常用医院").strip()
    t_norm = t
    if drug_in and drug_in != "周期用药":
        t_norm = t_norm.replace(drug_in, "周期用药")
    if hosp_in and hosp_in != "常用医院":
        t_norm = t_norm.replace(hosp_in, "医院")

    quoted_inj = bool(reply_to_text and ((drug_in and drug_in in reply_to_text) or "周期用药" in reply_to_text or "打针" in reply_to_text))
    if _INJ_DONE_RE.match(t_norm) or _INJ_SKIP_RE.match(t_norm):
        # 「打了」很短，只有在引用了打针卡、有开着的打针卡、或正处于注射窗口时才认领，免得抢别的对话
        if quoted_inj or _open_case(db, "med_injection") or _in_injection_window(db):
            return {"handled": True, "reply": record_injection(db, skipped=bool(_INJ_SKIP_RE.match(t_norm)))["summary"]}
        return None
    m = _DOSES_RE.match(t_norm)
    if m:
        n_doses = _parse_small_cn_int(m.group(1) or m.group(2) or "")
        if n_doses is not None:
            return {"handled": True, "reply": set_doses_left(db, n_doses)["summary"]}
    quoted_refill = bool(reply_to_text and ("预约" in reply_to_text or "挂号" in reply_to_text or (drug_in and drug_in in reply_to_text) or "周期用药" in reply_to_text))
    if _BOOKED_RE.match(t_norm) and (quoted_refill or _open_case(db, "med_refill")):
        return {"handled": True, "reply": record_booking(db)["summary"]}
    m = _REFILL_RE.match(t_norm)
    if m and (
        any(k in t_norm for k in ("开药", "拿药", "补药", "周期用药", "开了药"))
        or (bool(m.group(1) or m.group(2)) and any(k in t_norm for k in ("医院", "支", "笔")))
    ):
        num_tok = m.group(1) or m.group(2) or ""
        n_pens = _parse_small_cn_int(num_tok) if num_tok else 1
        if n_pens is not None:
            return {"handled": True, "reply": record_refill(db, n_pens)["summary"]}
    m = _STOCK_RE.match(t)
    if m:
        unit_n = _parse_small_cn_int(m.group(2))
        if unit_n is not None:
            if (m.group(3) or "") == "盒":
                return {"handled": True, "reply": "按盒不好算，回具体数量吧，比如「口罩还剩 12」。"}
            return {"handled": True, "reply": set_supply_stock(db, _SUPPLY_ALIASES[m.group(1)], unit_n)["summary"]}

    # 口语语音复合汇报（如『好了，针打完了，也记录一下儿，针头还有6个。』）：严格排除问句、否定、改规则讨论
    if (
        len(t_norm) <= 52
        and "\n" not in t_norm
        and not re.search(r"[?？]|吗$|不是|一般|每周|目标|预约|下周|没打|还没打|忘打|不要|别", t_norm)
        and re.search(
            r"(?:^|[，,\s])(?:好了[，,\s]*)?(?:这周的?|今晚的?)?(?:周期用药|针)?(?:已经)?(?:打完了?|打过了|打好了|打针了)(?:[，,\s]|$)",
            t_norm,
        )
        and (quoted_inj or _open_case(db, "med_injection") or _in_injection_window(db))
    ):
        parts: list[str] = [record_injection(db, skipped=False)["summary"]]
        m_st = _STOCK_INLINE_RE.search(t)
        if m_st:
            u_n = _parse_small_cn_int(m_st.group(2))
            if u_n is not None and (m_st.group(3) or "") != "盒":
                parts.append(set_supply_stock(db, _SUPPLY_ALIASES[m_st.group(1)], u_n)["summary"])
        return {"handled": True, "reply": "\n".join(parts)}
    return None
