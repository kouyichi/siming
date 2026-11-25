"""W2-4 (`haos_night_owl`)：懂夜猫子的全屋智能联动桥（T-15m 格力空调+林内零冷水预热、00:00–04:59 书房 2700K 暖光、补觉硬锁科沃斯 T90S Pro 扫地机并在离家后释放自动清扫）。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Callable, Optional

from siming import db as _db_mod
from siming.attention import evaluate_attention_state
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, parse_iso
from siming.event_chain import get_situation, set_situation
from siming.modules import is_module_enabled

SITUATION_KEY = "haos:night_owl"
HAOS_DEDUP_WINDOW_SEC = 1800
VACUUM_ENTITY_ID = "vacuum.ecovacs_t90s_pro"
VACUUM_LOCK_ENTITY_ID = "input_boolean.ecovacs_t90s_pro_dnd_lock"
GREE_AC_ENTITY_ID = "climate.xixingfuli_gree_ac"
RINNAI_CIRC_ENTITY_ID = "switch.xixingfuli_rinnai_zero_cold"
STUDY_LIGHT_ENTITY_ID = "light.xixingfuli_study_desk"
STUDY_WARM_CCT_KELVIN = 2700


def _coerce_now(now: Optional[datetime | str] = None) -> datetime:
    if now is None:
        return _db_mod.now_local()
    if isinstance(now, str):
        return parse_iso(now)
    if now.tzinfo is None:
        return now.replace(tzinfo=SHANGHAI_TZ)
    return now.astimezone(SHANGHAI_TZ)


def _normalize_sleep_state(db: SimingDB, cur: datetime, sleep_state: Optional[str]) -> tuple[str, str]:
    if sleep_state is not None and str(sleep_state).strip():
        raw = str(sleep_state).strip().lower()
        if raw in ("sleep", "asleep", "sleeping", "bedtime", "nap"):
            return "ASLEEP", f"explicit_arg({sleep_state})"
        return "AWAKE", f"explicit_arg({sleep_state})"

    sit_sleep = get_situation(db, "sleep:state")
    if sit_sleep and isinstance(sit_sleep.get("value"), dict):
        st_val = str(sit_sleep["value"].get("state") or "").strip().lower()
        if st_val in ("sleep", "asleep", "sleeping"):
            return "ASLEEP", "situation:sleep:state"
        if st_val in ("awake", "awake_active", "active"):
            return "AWAKE", "situation:sleep:state"

    att = evaluate_attention_state(db, now_override=cur)
    is_asleep = att["state"] in ("ASLEEP", "STATIC_QUIET")
    return ("ASLEEP" if is_asleep else "AWAKE"), str(att.get("reason") or att["state"])


def _canonicalize_place(place: str) -> str:
    p = (place or "").strip().lower()
    if p in ("家", "home", "at_home", "xixingfuli", "示例小区B", "示例小区A", "示例小区A"):
        return "home"
    if p in ("heading_home", "commuting_home", "en_route_home", "approaching_home", "回家路上", "返家路上"):
        return "heading_home"
    if p in ("away", "office", "departed", "outside", "公司", "离家", "外出", "示例写字楼"):
        return "away"
    return p


def _normalize_location_state(db: SimingDB, location_state: Optional[str]) -> str:
    if location_state is not None and str(location_state).strip():
        return _canonicalize_place(str(location_state))
    sit = get_situation(db, "loc:me")
    if sit and isinstance(sit.get("value"), dict):
        v = sit["value"]
        place = str(v.get("state") or v.get("place_alias") or v.get("alias") or v.get("place") or "").strip().lower()
        if place:
            canon = _canonicalize_place(place)
            return canon if canon in ("home", "heading_home", "away") else "away"
    return "home"


def _resolve_eta_home_min(db: SimingDB, eta_home_min: Optional[float]) -> Optional[float]:
    if eta_home_min is not None:
        return float(eta_home_min)
    sit = get_situation(db, "eta:home")
    if sit and isinstance(sit.get("value"), dict) and sit["value"].get("minutes") is not None:
        return float(sit["value"]["minutes"])
    return None


def is_vacuum_locked(
    db: SimingDB,
    now: Optional[datetime | str] = None,
    sleep_state: Optional[str] = None,
) -> dict[str, Any]:
    """判定科沃斯 T90S Pro 扫地机是否应处于硬锁禁扰状态（补觉/睡眠期一律硬锁，绝不吵醒）。"""
    db.init_db()
    cur = _coerce_now(now)
    norm_sleep, source_reason = _normalize_sleep_state(db, cur, sleep_state)
    locked = norm_sleep == "ASLEEP"
    return {
        "ok": True,
        "vacuum_locked": locked,
        "sleep_state": norm_sleep,
        "entity_id": VACUUM_ENTITY_ID,
        "lock_entity_id": VACUUM_LOCK_ENTITY_ID,
        "reason": "夜猫子睡眠/补觉保护期硬锁扫地机" if locked else "清醒态解除睡眠硬锁（待离家后自动清扫）",
        "source": source_reason,
        "evaluated_at": iso_now(cur),
    }


def evaluate_night_owl_haos_rules(
    db: SimingDB,
    now: Optional[datetime | str] = None,
    sleep_state: Optional[str] = None,
    location_state: Optional[str] = None,
    eta_home_min: Optional[float] = None,
    haos_call_fn: Optional[Callable[[dict[str, Any]], Any]] = None,
) -> dict[str, Any]:
    """计算三条夜猫子全屋智能规则动作并按需执行 `haos_call_fn`。"""
    db.init_db()
    cur = _coerce_now(now)
    enabled = is_module_enabled(db, "haos_night_owl")
    vac_info = is_vacuum_locked(db, now=cur, sleep_state=sleep_state)
    norm_sleep = vac_info["sleep_state"]
    loc = _normalize_location_state(db, location_state)
    eta = _resolve_eta_home_min(db, eta_home_min)

    actions: list[dict[str, Any]] = []
    pre_arrival_triggered = False
    warm_study_light_active = False
    auto_clean_released = False

    # ① 到家前 T-15m 预开格力空调 + 林内零冷水预循环
    heading_home = loc in ("heading_home", "commuting_home", "en_route_home", "approaching_home")
    if heading_home and eta is not None and 0 < eta <= 15.0:
        pre_arrival_triggered = True
        actions.append(
            {
                "rule": "pre_arrival_gree_ac",
                "domain": "climate",
                "service": "set_hvac_mode",
                "entity_id": GREE_AC_ENTITY_ID,
                "data": {"hvac_mode": "auto", "temperature": 24, "eta_home_min": eta},
                "reason": f"OwnTracks 预计 {eta:.0f} 分钟到示例小区B（≤15m），提前预开格力空调",
            }
        )
        actions.append(
            {
                "rule": "pre_arrival_rinnai_hot_water",
                "domain": "switch",
                "service": "turn_on",
                "entity_id": RINNAI_CIRC_ENTITY_ID,
                "data": {"duration_min": 20, "eta_home_min": eta},
                "reason": f"预计 {eta:.0f} 分钟到家，启动林内 R32F 零冷水循环预热",
            }
        )

    # ② 凌晨 00:00–04:59 清醒在家写代码 -> 书房 2700K 暖色护眼低色温灯
    at_home = loc in ("home", "at_home", "xixingfuli")
    if 0 <= cur.hour < 5 and norm_sleep == "AWAKE" and at_home:
        warm_study_light_active = True
        actions.append(
            {
                "rule": "late_night_study_warm_light",
                "domain": "light",
                "service": "turn_on",
                "entity_id": STUDY_LIGHT_ENTITY_ID,
                "data": {"color_temp_kelvin": STUDY_WARM_CCT_KELVIN, "brightness_pct": 45},
                "reason": f"凌晨 {cur.strftime('%H:%M')} 清醒书房编码，切换 {STUDY_WARM_CCT_KELVIN}K 低色温暖光护眼",
            }
        )

    # ③ 睡眠/补觉硬锁科沃斯 T90S Pro；清醒且离家（away）释放自动清扫
    if vac_info["vacuum_locked"]:
        actions.append(
            {
                "rule": "sleep_vacuum_hard_lock",
                "domain": "input_boolean",
                "service": "turn_on",
                "entity_id": VACUUM_LOCK_ENTITY_ID,
                "data": {"vacuum_locked": True, "target_vacuum": VACUUM_ENTITY_ID},
                "reason": "睡眠/上午补觉态硬锁科沃斯 T90S Pro，禁止定时或自动出舱清扫",
            }
        )
    elif loc in ("away", "office", "departed", "outside"):
        auto_clean_released = True
        actions.append(
            {
                "rule": "away_release_vacuum_lock",
                "domain": "input_boolean",
                "service": "turn_off",
                "entity_id": VACUUM_LOCK_ENTITY_ID,
                "data": {"vacuum_locked": False},
                "reason": "检测到已离家且清醒，解除扫地机禁扰锁",
            }
        )
        actions.append(
            {
                "rule": "away_start_vacuum_clean",
                "domain": "vacuum",
                "service": "start",
                "entity_id": VACUUM_ENTITY_ID,
                "data": {"mode": "standard_mop_vac"},
                "reason": "离家后自动释放科沃斯 T90S Pro 全屋清扫",
            }
        )

    executed_actions: list[dict[str, Any]] = []
    deduped = False
    if haos_call_fn is not None and enabled and actions:
        action_sig = f"{norm_sleep}|{loc}|{','.join(a['rule'] for a in actions)}"
        raw_prev = db.get_meta("haos_night_owl_last_exec") or ""
        skip_dup = False
        if raw_prev:
            try:
                prev_obj = json.loads(raw_prev)
                if prev_obj.get("sig") == action_sig and prev_obj.get("at"):
                    elapsed_s = (cur - parse_iso(str(prev_obj["at"]))).total_seconds()
                    if 0 <= elapsed_s < HAOS_DEDUP_WINDOW_SEC:
                        skip_dup = True
            except Exception:
                skip_dup = False
        if skip_dup:
            deduped = True
        else:
            for act in actions:
                resp = haos_call_fn(act)
                executed_actions.append({"rule": act["rule"], "entity_id": act["entity_id"], "result": resp})
            db.set_meta(
                "haos_night_owl_last_exec",
                json.dumps({"sig": action_sig, "at": iso_now(cur), "rules": [a["rule"] for a in actions]}, ensure_ascii=False),
            )

    out: dict[str, Any] = {
        "ok": True,
        "enabled": enabled,
        "evaluated_at": iso_now(cur),
        "sleep_state": norm_sleep,
        "location_state": loc,
        "eta_home_min": eta,
        "vacuum_locked": vac_info["vacuum_locked"],
        "auto_clean_released": auto_clean_released,
        "warm_study_light_active": warm_study_light_active,
        "pre_arrival_triggered": pre_arrival_triggered,
        "actions": actions,
        "executed_actions": executed_actions,
        "deduped": deduped,
    }
    if not enabled:
        out["skipped"] = "module_disabled"
    return out


def sync_night_owl_to_haos(
    db: SimingDB,
    now: Optional[datetime | str] = None,
    haos_call_fn: Optional[Callable[[dict[str, Any]], Any]] = None,
    sleep_state: Optional[str] = None,
    location_state: Optional[str] = None,
    eta_home_min: Optional[float] = None,
) -> dict[str, Any]:
    """评估夜猫子全屋智能规则、下发 HAOS 动作并将最新状态持久化至 `situation` 表 (`haos:night_owl`)。"""
    res = evaluate_night_owl_haos_rules(
        db,
        now=now,
        sleep_state=sleep_state,
        location_state=location_state,
        eta_home_min=eta_home_min,
        haos_call_fn=haos_call_fn,
    )
    if not res["enabled"]:
        res["situation_key"] = SITUATION_KEY
        res["situation_changed"] = False
        return res
    sit_payload = {
        "evaluated_at": res["evaluated_at"],
        "sleep_state": res["sleep_state"],
        "location_state": res["location_state"],
        "eta_home_min": res["eta_home_min"],
        "vacuum_locked": res["vacuum_locked"],
        "auto_clean_released": res["auto_clean_released"],
        "warm_study_light_active": res["warm_study_light_active"],
        "pre_arrival_triggered": res["pre_arrival_triggered"],
        "action_rules": [a["rule"] for a in res["actions"]],
    }
    sit = set_situation(db, SITUATION_KEY, sit_payload, source="haos_night_owl", as_of=res["evaluated_at"])
    if not res.get("deduped"):
        db.write_audit(
            "A1" if res["executed_actions"] else "A0",
            "haos_night_owl_sync",
            sit_payload,
            target_id=SITUATION_KEY,
            detail=f"actions={len(res['actions'])}, executed={len(res['executed_actions'])}",
        )
    res["situation_key"] = SITUATION_KEY
    res["situation_changed"] = sit["changed"]
    return res


def format_haos_report(db: SimingDB, now: Optional[datetime | str] = None) -> str:
    """输出夜猫子全屋智能状态简报。"""
    sit = get_situation(db, SITUATION_KEY)
    state = sit["value"] if sit else evaluate_night_owl_haos_rules(db, now=now)
    vac_badge = "🔒 已硬锁（补觉免打扰）" if state.get("vacuum_locked") else (
        "🧹 已离家释放清扫" if state.get("auto_clean_released") else "🟢 解锁待机"
    )
    light_badge = f"💡 {STUDY_WARM_CCT_KELVIN}K 暖光护眼中" if state.get("warm_study_light_active") else "⚪ 常规照明"
    pre_badge = "🔥 已触发 T-15m 格力空调 + 林内零冷水" if state.get("pre_arrival_triggered") else "⚪ 待命"
    return "\n".join(
        [
            "🏠 **夜猫子全屋智能（HAOS）联动状态**",
            f"• 🌙 作息 / 位置：`{state.get('sleep_state', 'UNKNOWN')}` / `{state.get('location_state', 'unknown')}`",
            f"• 🤖 科沃斯 T90S Pro：{vac_badge}",
            f"• 💡 深夜书房照明：{light_badge}",
            f"• ❄️ 到家前 15m 预热：{pre_badge}",
        ]
    )
