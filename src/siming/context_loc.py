"""手机实时位置与 Wi-Fi 场景融合定位器（context_loc）：
- 接收 iPhone 快捷指令发往 `/siming/in/loc` 的 `{wifi, lat, lng, address, scene}`；
- 支持 Wi-Fi SSID -> 常用地点别名映射（如连上家里 Wi-Fi 自动识别为「家」，连上公司 Wi-Fi 自动识别为「公司」，连上新房 Wi-Fi 自动识别为「新房」）；
- 当人在户外无熟知 Wi-Fi 时，采用手机上报的实时经纬度 + 街道 POI 作为滴滴打车起点与美团/瑞幸外卖定位参考！
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import datetime
from typing import Any, Optional

from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local, parse_iso
from siming.ledger.reimburse import load_saved_places
from siming.paths import get_paths

_GENERIC_LOC_PLACEHOLDERS = {
    "位置",
    "当前位置",
    "我的位置",
    "location",
    "current location",
    "dropped pin",
}


def _clean_ios_addr(raw: Any) -> str:
    val = str(raw or "").strip()
    if val.lower() in _GENERIC_LOC_PLACEHOLDERS:
        return ""
    return val


def _haversine_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371000.0
    p1 = math.radians(lat1)
    p2 = math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def load_known_geo_anchors(db: SimingDB) -> list[dict[str, Any]]:
    """从 policy.yaml `daily_care.regimes` 与 `custom_geo_places_json` 加载已知地点坐标锚点（WGS-84）。"""
    from siming.config import load_policy

    anchors: list[dict[str, Any]] = []
    seen: set[tuple[str, int, int]] = set()

    def _add(alias: str, label: str, lat_v: Any, lng_v: Any, radius_m: float = 160.0) -> None:
        if not alias or lat_v in (None, "") or lng_v in (None, ""):
            return
        try:
            la, ln = float(lat_v), float(lng_v)
        except (TypeError, ValueError):
            return
        key = (alias, int(round(la * 10000)), int(round(ln * 10000)))
        if key in seen:
            return
        seen.add(key)
        anchors.append({"alias": alias, "label": label or alias, "lat": la, "lng": ln, "radius_m": radius_m})

    policy = load_policy(db.paths)
    regimes = ((policy.get("daily_care") or {}).get("regimes") or []) if isinstance(policy, dict) else []
    today_s = now_local().date().isoformat()
    active_idx = -1
    for i, rg in enumerate(regimes):
        if not isinstance(rg, dict):
            continue
        f_s = str(rg.get("from") or "")
        u_s = str(rg.get("until") or "")
        if (not f_s or today_s >= f_s) and (not u_s or today_s < u_s):
            active_idx = i
            break
    if active_idx < 0 and regimes:
        active_idx = 0

    for i, rg in enumerate(regimes):
        if not isinstance(rg, dict):
            continue
        h_alias = "家" if i == active_idx else "新房"
        _add(h_alias, str(rg.get("home_label") or h_alias), rg.get("home_lat"), rg.get("home_lon"), radius_m=160.0)
        o_label = str(rg.get("office_label") or "公司")
        _add("公司", o_label, rg.get("office_lat"), rg.get("office_lon"), radius_m=160.0)

    raw_custom = db.get_meta("custom_geo_places_json") or "{}"
    try:
        custom_obj = json.loads(raw_custom)
        if isinstance(custom_obj, dict):
            for c_alias, c_info in custom_obj.items():
                if isinstance(c_info, dict):
                    rad = 180.0
                    if c_info.get("radius_m") not in (None, ""):
                        try:
                            rad = float(c_info["radius_m"])
                        except (TypeError, ValueError):
                            rad = 180.0
                    _add(str(c_alias).strip(), str(c_alias).strip(), c_info.get("lat"), c_info.get("lng"), radius_m=rad)
    except Exception:
        pass
    return anchors


def match_known_geo_place(
    db: SimingDB,
    lat: float,
    lng: float,
    radius_m: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """将手机实时 GPS (WGS-84) 匹配到半径内最近的已知地点锚点（防止 OwnTracks 未带 Wi-Fi 时丢掉公司/家识别）。"""
    best: Optional[dict[str, Any]] = None
    for anc in load_known_geo_anchors(db):
        lim = float(radius_m) if radius_m is not None else float(anc.get("radius_m") or 160.0)
        d = _haversine_m(lat, lng, float(anc["lat"]), float(anc["lng"]))
        if d <= lim and (best is None or d < best["dist_m"]):
            best = {**anc, "dist_m": round(d, 1)}
    return best


def resolve_anchor_coords_for_alias(db: SimingDB, alias: str) -> Optional[dict[str, Any]]:
    """按地点别名（如「公司」「家」「新房」或自定义地点）反查已配置的 WGS-84 坐标与标签。"""
    target = (alias or "").strip()
    if not target:
        return None
    for anc in load_known_geo_anchors(db):
        if anc["alias"] == target or target in anc["label"] or anc["label"] in target:
            return anc
    return None


def load_wifi_place_map(db: SimingDB) -> dict[str, str]:
    """加载 Wi-Fi SSID -> 常用地点别名（如 {'Office-WiFi': '公司', 'Home-WiFi': '家'}）。"""
    raw = db.get_meta("wifi_place_map") or "{}"
    try:
        obj = json.loads(raw)
        return {str(k).strip(): str(v).strip() for k, v in obj.items() if str(k).strip() and str(v).strip()}
    except Exception:
        return {}


def load_region_place_map(db: SimingDB) -> dict[str, str]:
    """加载 OwnTracks Region 名称 -> 常用地点别名（如 {'Office': '公司'}）。"""
    raw = db.get_meta("region_place_map") or "{}"
    try:
        obj = json.loads(raw)
        return {str(k).strip(): str(v).strip() for k, v in obj.items() if str(k).strip() and str(v).strip()}
    except Exception:
        return {}


def bind_region_to_place(db: SimingDB, region_name: str, place_alias: str) -> dict[str, Any]:
    clean_reg = (region_name or "").strip()
    clean_alias = (place_alias or "").strip()
    if not clean_reg or not clean_alias:
        return {"ok": False, "error": "Region 名称与地点别名不能为空"}
    mp = load_region_place_map(db)
    mp[clean_reg] = clean_alias
    db.set_meta("region_place_map", json.dumps(mp, ensure_ascii=False))
    db.write_audit("A1", "bind_region_place", {"region": clean_reg, "place": clean_alias})
    return {
        "ok": True,
        "region_map": mp,
        "summary": f"📍 已绑定 OwnTracks 围栏 `{clean_reg}` → 地点别名 **{clean_alias}**。",
    }


def _resolve_region_alias(region_name: str, region_map: dict[str, str], places: dict[str, Any]) -> str:
    reg_s = (region_name or "").strip()
    if not reg_s:
        return ""
    if reg_s in region_map:
        return region_map[reg_s]
    for k_reg, alias_v in region_map.items():
        if k_reg.lower() == reg_s.lower():
            return alias_v
    if reg_s in places or reg_s in ("家", "公司", "新房"):
        return reg_s
    return ""


def bind_wifi_to_place(db: SimingDB, ssid: str, place_alias: str) -> dict[str, Any]:
    clean_ssid = (ssid or "").strip()
    clean_alias = (place_alias or "").strip()
    if not clean_ssid or not clean_alias:
        return {"ok": False, "error": "Wi-Fi 名称与地点别名不能为空"}
    mp = load_wifi_place_map(db)
    mp[clean_ssid] = clean_alias
    db.set_meta("wifi_place_map", json.dumps(mp, ensure_ascii=False))
    db.write_audit("A1", "bind_wifi_place", {"wifi": clean_ssid, "place": clean_alias})
    places = load_saved_places(db)
    addr_hint = f"（对应地址：{places[clean_alias]}）" if clean_alias in places else ""
    return {
        "ok": True,
        "wifi_map": mp,
        "summary": f"📶 已绑定 Wi-Fi `{clean_ssid}` → 地点别名 **{clean_alias}**{addr_hint}。以后手机连上该 Wi-Fi 时，打车与点餐将自动切换至此位置！",
    }


def ingest_ios_location(db: SimingDB, raw_text: str, received_at: Optional[str] = None) -> dict[str, Any]:
    """解析并持久化来自 iPhone `/siming/in/loc` 的位置与 Wi-Fi 快照。"""
    db.init_db()
    txt = (raw_text or "").strip()
    data: dict[str, Any] = {}
    if txt.startswith("{"):
        try:
            data = json.loads(txt)
        except Exception:
            data = {"address": txt}
    else:
        for seg in re.split(r"[|;\n]+", txt):
            if "=" in seg:
                k, v = seg.split("=", 1)
                data[k.strip().lower()] = v.strip()
        if not data and txt:
            data = {"address": txt}

    wifi = str(data.get("wifi") or data.get("ssid") or data.get("SSID") or "").strip()
    address = _clean_ios_addr(data.get("address") or data.get("addr") or data.get("desc"))
    scene = str(data.get("scene") or "").strip()
    ot_event = str(data.get("event") or "").strip().lower()  # OwnTracks transition: enter / leave
    app_name = str(data.get("app") or "").strip()
    inregions = data.get("inregions") if isinstance(data.get("inregions"), list) else []

    lat: Optional[float] = None
    lng: Optional[float] = None
    try:
        raw_lat = data.get("lat")
        raw_lng = data.get("lng") if data.get("lng") not in (None, "") else data.get("lon")
        if raw_lat not in (None, ""):
            lat = round(float(raw_lat), 6)
        if raw_lng not in (None, ""):
            lng = round(float(raw_lng), 6)
    except (TypeError, ValueError):
        lat, lng = None, None

    vel_kmh: Optional[float] = None
    try:
        if data.get("vel") not in (None, "") and float(data["vel"]) >= 0:
            vel_kmh = round(float(data["vel"]), 1)
    except (TypeError, ValueError):
        vel_kmh = None

    batt_pct: Optional[int] = None
    try:
        if data.get("batt") not in (None, "") and 0 <= int(float(data["batt"])) <= 100:
            batt_pct = int(float(data["batt"]))
    except (TypeError, ValueError):
        batt_pct = None

    motion = "stationary"
    if vel_kmh is not None:
        if vel_kmh >= 18.0:
            motion = "driving"
        elif vel_kmh >= 2.5:
            motion = "walking"

    prev_raw = db.get_meta("last_logged_ios_loc") or ""
    prev_anchor: dict[str, Any] = {}
    if prev_raw:
        try:
            prev_anchor = json.loads(prev_raw)
        except Exception:
            prev_anchor = {}
    prev_alias = str(prev_anchor.get("place_alias") or "")
    prev_wifi = str(prev_anchor.get("wifi") or "")

    wifi_map = load_wifi_place_map(db)
    region_map = load_region_place_map(db)
    places = load_saved_places(db)
    transition_alias = _resolve_region_alias(address, region_map, places) if ot_event in ("enter", "leave") else ""

    matched_alias = ""
    if scene and scene in places:
        matched_alias = scene
    elif ot_event == "enter" and transition_alias:
        matched_alias = transition_alias
    elif inregions and ot_event != "leave":
        for reg in inregions:
            resolved_reg = _resolve_region_alias(str(reg), region_map, places)
            if resolved_reg:
                matched_alias = resolved_reg
                break
    if not matched_alias and wifi and ot_event != "leave":
        if wifi in wifi_map:
            matched_alias = wifi_map[wifi]
        else:
            for k_ssid, alias_v in wifi_map.items():
                if k_ssid.lower() == wifi.lower():
                    matched_alias = alias_v
                    break

    leave_excluded_alias = (transition_alias or prev_alias) if ot_event == "leave" else ""
    geo_hit: Optional[dict[str, Any]] = None
    if not matched_alias and lat is not None and lng is not None and (vel_kmh is None or vel_kmh < 18.0):
        cand_hit = match_known_geo_place(db, lat, lng)
        if cand_hit is not None and (not leave_excluded_alias or str(cand_hit["alias"]) != leave_excluded_alias):
            geo_hit = cand_hit
            matched_alias = str(geo_hit["alias"])

    resolved_addr = address
    if matched_alias and matched_alias in places and not resolved_addr:
        resolved_addr = places[matched_alias]
    elif geo_hit is not None and not resolved_addr:
        resolved_addr = str(geo_hit.get("label") or "")

    ts = received_at or iso_now()
    if not received_at and data.get("tst"):
        try:
            ts = datetime.fromtimestamp(int(data["tst"]), tz=SHANGHAI_TZ).isoformat(timespec="seconds")
        except Exception:
            pass

    snap = {
        "wifi": wifi,
        "place_alias": matched_alias,
        "address": resolved_addr,
        "lat": lat,
        "lng": lng,
        "vel_kmh": vel_kmh,
        "motion": motion,
        "batt_pct": batt_pct,
        "ot_event": ot_event,
        "leave_excluded_alias": leave_excluded_alias,
        "updated_at": ts,
    }
    db.set_meta("latest_ios_location", json.dumps(snap, ensure_ascii=False))

    # 行为轨迹落库（仅在地点/Wi-Fi切换、地理围栏进出、或移动距离 >=200m 时记入 events，原地刷手机零噪音）
    moved_m = 0.0
    if (
        lat is not None
        and lng is not None
        and prev_anchor.get("lat") is not None
        and prev_anchor.get("lng") is not None
    ):
        moved_m = _haversine_m(float(prev_anchor["lat"]), float(prev_anchor["lng"]), lat, lng)

    alias_changed = bool(prev_anchor) and (matched_alias != prev_alias or wifi != prev_wifi)
    is_first = not prev_anchor
    should_log_event = is_first or alias_changed or ot_event in ("enter", "leave") or moved_m >= 200.0

    logged_event_id: Optional[str] = None
    if should_log_event:
        label = matched_alias or resolved_addr or (f"{lat},{lng}" if lat is not None and lng is not None else wifi or "未知位置")
        if ot_event == "leave":
            if transition_alias and address and transition_alias != address:
                action_tag = f"离开围栏 {transition_alias} ({address})"
            else:
                action_tag = f"离开围栏 {transition_alias or address or prev_alias or label}"
        elif ot_event == "enter":
            if transition_alias and address and transition_alias != address:
                action_tag = f"进入围栏 {transition_alias} ({address})"
            else:
                action_tag = f"进入围栏 {transition_alias or address or label}"
        elif prev_alias and not matched_alias:
            action_tag = f"离场: {prev_alias} → 外出 ({label})"
        elif matched_alias and matched_alias != prev_alias:
            action_tag = f"到场: {matched_alias}" + (f" (`{wifi}`)" if wifi else "")
        else:
            action_tag = f"位置移动: {label}"
        if vel_kmh is not None and vel_kmh >= 3.0:
            mode_cn = "驾车" if motion == "driving" else "步行"
            action_tag += f" · {mode_cn} {vel_kmh}km/h"
        if app_name:
            action_tag += f" [App:{app_name}]"

        effective_prev = (transition_alias or prev_alias) if ot_event == "leave" else prev_alias
        fp = hashlib.sha256(f"ios_loc|{ts[:16]}|{matched_alias}|{wifi}|{round(lat or 0, 3)}|{round(lng or 0, 3)}|{ot_event}".encode("utf-8")).hexdigest()
        with db.transaction() as conn:
            logged_event_id = db.append_event(
                conn,
                source="ios_loc",
                thread_key=f"loc:{matched_alias or 'move'}",
                summary=action_tag,
                payload={**snap, "prev_alias": effective_prev, "moved_m": round(moved_m, 1), "ot_event": ot_event, "app": app_name},
                occurred_at=ts,
                fingerprint=fp,
            )
        db.set_meta("last_logged_ios_loc", json.dumps(snap, ensure_ascii=False))
        # 若检测到驾车位移或进出围栏：优先趁车刚停稳在线（state=='online'）经官方 Fleet API 零唤醒拉精准电量，未在线则按里程折算兜底！
        if moved_m >= 1500.0 or ot_event in ("enter", "leave") or motion == "driving":
            try:
                from siming.tesla_link import deduct_tesla_soc_from_driving_km, poll_tesla_cn_fleet_if_configured

                last_drv_pull = db.get_meta("last_tesla_drive_pull_at") or ""
                can_pull = True
                if last_drv_pull:
                    from siming.db import parse_iso

                    can_pull = (now_local() - parse_iso(last_drv_pull)).total_seconds() >= 300
                pulled_exact = False
                if can_pull:
                    db.set_meta("last_tesla_drive_pull_at", iso_now())
                    f_res = poll_tesla_cn_fleet_if_configured(db, force=True, allow_wake=False)
                    pulled_exact = bool(f_res.get("soc_pct") is not None)
                if not pulled_exact and 2500.0 <= moved_m <= 180000.0 and (motion == "driving" or vel_kmh is None):
                    deduct_tesla_soc_from_driving_km(db, round((moved_m / 1000.0) * 1.25, 2))
            except Exception:
                pass

    # 联动无感睡醒：仅当检测到真实离家/户外移动（离开家围栏、从家切为外出、或非家场景下位移 >=150m / 速度 >=3.5km/h）才唤醒，
    # 严防夜间手机放在床头时 OwnTracks 每 3 分钟一次的室内静止心跳误触发唤醒！
    departed_or_moving = (ot_event == "leave") or (
        matched_alias != "家"
        and ((prev_alias == "家") or moved_m >= 150.0 or (vel_kmh is not None and vel_kmh >= 3.5))
    )
    if departed_or_moving:
        try:
            from siming.attention import ingest_ios_sleep_signal

            ingest_ios_sleep_signal(db, "loc_move", received_at=ts)
        except Exception:
            pass

    # EC-5 出门自动备车事件链：检测到离开围栏、从室内切为外出、或户外移动 >=80m 时核验是否有同步的驾车行程并按温度/电量自动开空调！
    is_departing = (ot_event == "leave") or (bool(prev_alias) and matched_alias != prev_alias) or (not matched_alias and moved_m >= 80.0)
    if is_departing:
        try:
            from siming.tesla_link import evaluate_departure_auto_climate

            evaluate_departure_auto_climate(
                db,
                departed_now=True,
                depart_place=prev_alias,
                trigger="owntracks_depart",
            )
        except Exception:
            pass

    db.mark_source_ok("ios_hb", detail=f"loc_update wifi={wifi or '-'} alias={matched_alias or '-'}", ok_at=ts)
    return {"ok": True, "logged_event_id": logged_event_id, "departed_or_moving": departed_or_moving, **snap}


def resolve_current_location_context(
    db: SimingDB,
    max_gps_age_min: float = 90.0,
    max_wifi_age_min: float = 480.0,
    pull_edge_if_older_sec: float = 45.0,
) -> dict[str, Any]:
    """返回当前最新有效的手机位置上下文：
    - 若在真实主库且缓存超过 `pull_edge_if_older_sec`（如刚点开 QQ 触发了手机上报），先快速拉一次 `81` 队列；
    - 连着已绑定 Wi-Fi（8h 内新鲜）-> 返回对应预设地点（如「公司」/「家」/「新房」）；
    - 户外 GPS 坐标/街道地址（90m 内新鲜）-> 返回精确经纬度与街道名；
    - 否则回退 `has_fresh=False`。
    """
    raw = db.get_meta("latest_ios_location") or ""
    need_pull = not raw
    if raw and pull_edge_if_older_sec > 0:
        try:
            s0 = json.loads(raw)
            u0 = str(s0.get("updated_at") or "")
            if not u0 or (now_local() - parse_iso(u0)).total_seconds() >= pull_edge_if_older_sec:
                need_pull = True
        except Exception:
            need_pull = True
    if need_pull and pull_edge_if_older_sec > 0 and db.db_path == get_paths().db_path:
        try:
            from siming.collectors.edge_sync import sync_with_edge

            sync_with_edge(db)
            raw = db.get_meta("latest_ios_location") or ""
        except Exception:
            pass

    if not raw:
        return {"has_fresh": False}
    try:
        snap = json.loads(raw)
    except Exception:
        return {"has_fresh": False}

    upd_s = str(snap.get("updated_at") or "")
    if not upd_s:
        return {"has_fresh": False}
    try:
        age_min = max(0.0, (now_local() - parse_iso(upd_s)).total_seconds() / 60.0)
    except Exception:
        return {"has_fresh": False}

    places = load_saved_places(db)
    wifi_map = load_wifi_place_map(db)
    wifi = str(snap.get("wifi") or "").strip()
    alias = str(snap.get("place_alias") or "").strip()
    if not alias and wifi:
        alias = wifi_map.get(wifi) or next((v for k, v in wifi_map.items() if k.lower() == wifi.lower()), "")
    addr = _clean_ios_addr(snap.get("address"))
    lat = snap.get("lat")
    lng = snap.get("lng")

    if alias and age_min <= max_wifi_age_min:
        coord_disp = f"{lat},{lng}" if (lat is not None and lng is not None) else ""
        full_addr = places.get(alias) or addr or coord_disp or alias
        return {
            "has_fresh": True,
            "source_kind": "wifi" if wifi else "scene",
            "place_alias": alias,
            "address": full_addr,
            "wifi": wifi,
            "lat": lat if age_min <= max_gps_age_min else None,
            "lng": lng if age_min <= max_gps_age_min else None,
            "age_min": round(age_min, 1),
            "badge": (
                f"📍 手机 Wi-Fi 定位：**{alias}**（`{wifi}`" + (f" · {full_addr}" if full_addr and full_addr != alias else "") + "）"
                if wifi
                else (f"📍 手机场景定位：**{alias}**（{full_addr}）" if full_addr and full_addr != alias else f"📍 手机场景定位：**{alias}**")
            ),
        }

    if (addr or (lat is not None and lng is not None)) and age_min <= max_gps_age_min:
        leave_excl = str(snap.get("leave_excluded_alias") or "").strip()
        geo_hit = (
            match_known_geo_place(db, float(lat), float(lng))
            if (not alias and lat is not None and lng is not None)
            else None
        )
        if geo_hit is not None and leave_excl and str(geo_hit.get("alias") or "") == leave_excl:
            geo_hit = None
        if geo_hit is not None:
            matched_alias = str(geo_hit["alias"])
            geo_label = places.get(matched_alias) or addr or str(geo_hit.get("label") or matched_alias)
            coord_disp = f"{lat},{lng}"
            wifi_hint = f" · Wi-Fi `{wifi}`" if wifi else ""
            label_part = f"{geo_label} · " if geo_label and geo_label != matched_alias else ""
            return {
                "has_fresh": True,
                "source_kind": "gps_anchor",
                "place_alias": matched_alias,
                "address": geo_label,
                "wifi": wifi,
                "lat": lat,
                "lng": lng,
                "age_min": round(age_min, 1),
                "badge": f"📍 手机 GPS 定位：**{matched_alias}**（{label_part}{coord_disp}{wifi_hint} · {int(age_min)}m 前更新）",
            }
        disp = addr or f"{lat},{lng}"
        wifi_hint = f" · Wi-Fi `{wifi}`" if wifi else ""
        return {
            "has_fresh": True,
            "source_kind": "gps",
            "place_alias": "",
            "address": disp,
            "wifi": wifi,
            "lat": lat,
            "lng": lng,
            "age_min": round(age_min, 1),
            "badge": f"📍 手机实时定位：**{disp}**{wifi_hint}（{int(age_min)}m 前更新）",
        }

    return {"has_fresh": False, "stale_age_min": round(age_min, 1)}


def format_location_status(db: SimingDB) -> str:
    ctx = resolve_current_location_context(db)
    w_map = load_wifi_place_map(db)
    places = load_saved_places(db)
    lines = ["📍 **司命空间定位与 Wi-Fi 映射状态**"]
    if ctx.get("has_fresh"):
        lines.append(f"• 当前生效定位：{ctx['badge']}")
    else:
        stale = f"（上次手机位置已过 {int(ctx['stale_age_min'])} 分钟）" if ctx.get("stale_age_min") is not None else "（尚未收到手机 `/siming/in/loc` 上报）"
        lines.append(f"• 当前生效定位：默认回退预设 `家`={places.get('家', '未设')}{stale}")
    if w_map:
        lines.append("• 已绑定 Wi-Fi 场景：" + "、".join(f"`{k}`→{v}" for k, v in w_map.items()))
    else:
        lines.append("• 绑定 Wi-Fi 命令：`绑定wifi <Wi-Fi名称> 公司` 或 `siming loc --map-wifi <SSID> 家`")
    lines.append("• 常用地址库：" + "、".join(f"`{k}`={v}" for k, v in places.items()))
    conn = db.connect()
    try:
        recent_evs = conn.execute(
            "SELECT occurred_at, summary FROM events WHERE source='ios_loc' ORDER BY occurred_at DESC LIMIT 5"
        ).fetchall()
    finally:
        conn.close()
    if recent_evs:
        lines.append("• 最近空间轨迹：" + " ； ".join(f"`{str(r['occurred_at'])[11:16]}` {r['summary']}" for r in recent_evs))
    return "\n".join(lines)
