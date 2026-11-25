"""高德实时路况 ETA、外出承诺重排与电车快充/停车参谋（F07 · EC-4 · outing_chain.py）：
1. 支持高德 Web 服务 Key（`~/.config/siming/secrets/amap-key`，驾车实时路况 `/v3/direction/driving` + 周边特斯拉超充/停车场 `/v3/place/around`）；
   未配 Key 时自动采用北京早晚高峰分段速度模型 + Haversine 路网曲折系数（1.32）估算精确度兜底，开箱即用；
2. 结合 `OwnTracks` 当前实时坐标、特斯拉续航核算（`tesla_link`）与当日外出 `expectations`，
   一旦在途 `slack < 0`（预计迟到），自动生成包含「顺延取餐/预约 + 复制迟到话术」的单张捆绑卡，回『发吧 / 确认』闭环！
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

import httpx

from siming import db as _db_mod
from siming.approval import create_approval
from siming.cn_calendar import WEEKDAY_CN, day_info
from siming.context_loc import resolve_current_location_context
from siming.crypto import read_secret, redact_sensitive
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local, parse_iso

_ORIG_NOW_LOCAL = now_local


def _cur_now() -> datetime:
    return now_local() if now_local is not _ORIG_NOW_LOCAL else _db_mod.now_local()
from siming.event_chain import get_situation, replan_impacted_dependencies, set_situation, upsert_expectation
from siming.tesla_link import check_tesla_range_for_trip

KNOWN_DEST_COORDS: dict[str, tuple[float, float, str]] = {
    "新房": (39.860000, 116.320000, "示例小区B（新房）"),
    "示例小区B": (39.860000, 116.320000, "示例小区B（新房）"),
    "家": (39.904200, 116.407400, "示例小区A"),
    "示例小区A": (39.904200, 116.407400, "示例小区A"),
    "公司": (39.908000, 116.407000, "示例写字楼"),
    "示例写字楼": (39.908000, 116.407000, "示例写字楼"),
    "车库": (39.906500, 116.409500, "当前常用车库"),
    "常用医院": (39.915000, 116.418000, "常用医院"),
    "北京南站": (39.865100, 116.378500, "北京南站"),
    "北京西站": (39.894900, 116.321300, "北京西站"),
    "清河站": (40.038900, 116.320400, "清河站"),
    "首都机场": (40.079900, 116.603100, "首都国际机场"),
    "大兴机场": (39.509800, 116.410500, "北京大兴国际机场"),
    "三里屯": (39.933800, 116.455100, "三里屯"),
    "国贸": (39.908700, 116.458600, "国贸"),
    "望京": (39.996800, 116.470100, "望京"),
    "朝阳大悦城": (39.923400, 116.512800, "朝阳大悦城"),
    "西直门": (39.940400, 116.355400, "西直门"),
    "合生汇": (39.893500, 116.478500, "朝阳合生汇"),
}

_LOCAL_NO_TRAVEL_RE = re.compile(
    r"^(?:就在家|在家|家里|我家里|线上|在线|语音|视频|腾讯会议|飞书会议|不用出门|不出门|在家玩|云桌游)[。!！~～\s]*$"
)


def _http_proxy_url() -> str:
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        v = (os.environ.get(k) or "").strip()
        if v.startswith(("http://", "https://")):
            return v
    return "http://127.0.0.1:7890"


def _beijing_traffic_multiplier(dt: datetime, db: SimingDB) -> float:
    """北京按时段与工作日/周末的动态路况倍率（无高德实时 Key 时校准 OSRM 自由流时长）。"""
    di = day_info(dt.date(), db.paths)
    h = dt.hour
    if di.is_workday:
        if h in (7, 8, 9, 17, 18, 19):
            return 1.50
        if 10 <= h <= 20:
            return 1.25
        return 1.08
    if 11 <= h <= 20:
        return 1.30
    return 1.08


def resolve_destination_coords(
    db: SimingDB,
    dest_query: str,
    *,
    geocoder: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
) -> Optional[dict[str, Any]]:
    """多级解析目的地经纬度：内置北京地标 -> 本人收藏/自定义地点 -> 本地地理编码缓存 -> 高德 POI / OSM Nominatim。"""
    d_clean = (dest_query or "").strip()
    if not d_clean:
        return None

    # 1. 本人自定义地点（`记住地点` 沉淀）优先，支持精确覆盖
    try:
        custom_geo = json.loads(db.get_meta("custom_geo_places_json") or "{}")
    except Exception:
        custom_geo = {}
    for cname, cinfo in custom_geo.items():
        if cname and (cname == d_clean or cname in d_clean):
            if cinfo.get("lat") is not None and cinfo.get("lng") is not None:
                return {
                    "lat": float(cinfo["lat"]),
                    "lng": float(cinfo["lng"]),
                    "label": d_clean if len(d_clean) > len(cname) else cname,
                    "source": "custom_geo",
                }

    # 2. 内置北京高频地标（长关键词优先，避免『新房』误吞更长地名）
    for k, (klat, klng, kfull) in sorted(KNOWN_DEST_COORDS.items(), key=lambda kv: -len(kv[0])):
        if k in d_clean:
            label = kfull if d_clean == k else d_clean
            return {"lat": klat, "lng": klng, "label": label, "source": "known_dest"}

    # 3. 本地已缓存的地理编码结果
    try:
        geo_cache = json.loads(db.get_meta("geocoded_places_cache_json") or "{}")
    except Exception:
        geo_cache = {}
    if d_clean in geo_cache and isinstance(geo_cache[d_clean], dict):
        hit = geo_cache[d_clean]
        if hit.get("lat") is not None and hit.get("lng") is not None:
            return {
                "lat": float(hit["lat"]),
                "lng": float(hit["lng"]),
                "label": str(hit.get("label") or d_clean),
                "source": "cache",
            }

    # 4. 注入测试 geocoder
    if geocoder is not None:
        g_res = geocoder(d_clean)
        if g_res and g_res.get("lat") is not None and g_res.get("lng") is not None:
            return {
                "lat": float(g_res["lat"]),
                "lng": float(g_res["lng"]),
                "label": str(g_res.get("label") or d_clean),
                "source": str(g_res.get("source") or "injected"),
            }
        return None

    if os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return None

    found: Optional[dict[str, Any]] = None
    amap_key = (read_secret("amap-key", db.paths) or "").strip()
    if amap_key:
        try:
            url_poi = (
                "https://restapi.amap.com/v3/place/text?"
                f"keywords={urllib.parse.quote(d_clean)}&city={urllib.parse.quote('北京')}&citylimit=true&offset=1&key={urllib.parse.quote(amap_key)}"
            )
            with httpx.Client(trust_env=False, timeout=4.5) as client:
                r = client.get(url_poi)
                if r.status_code == 200:
                    pois = (r.json().get("pois") or [])
                    if pois and pois[0].get("location"):
                        lng_s, lat_s = str(pois[0]["location"]).split(",", 1)
                        found = {
                            "lat": float(lat_s),
                            "lng": float(lng_s),
                            "label": str(pois[0].get("name") or d_clean),
                            "source": "amap_poi",
                        }
        except Exception:
            found = None

    if found is None:
        try:
            q_str = d_clean if "北京" in d_clean else f"北京 {d_clean}"
            url_nom = (
                "https://nominatim.openstreetmap.org/search?"
                f"q={urllib.parse.quote(q_str)}&format=json&limit=1"
            )
            with httpx.Client(
                trust_env=False,
                proxy=_http_proxy_url(),
                timeout=4.5,
                headers={"User-Agent": "siming-personal-assistant/1.0"},
            ) as client:
                r = client.get(url_nom)
                if r.status_code == 200:
                    arr = r.json()
                    if isinstance(arr, list) and arr:
                        lat_f = float(arr[0]["lat"])
                        lng_f = float(arr[0]["lon"])
                        if 39.4 <= lat_f <= 41.1 and 115.4 <= lng_f <= 117.6:
                            found = {
                                "lat": lat_f,
                                "lng": lng_f,
                                "label": d_clean,
                                "source": "nominatim",
                            }
        except Exception:
            found = None

    if found is not None:
        geo_cache[d_clean] = {**found, "cached_at": iso_now()}
        if len(geo_cache) > 100:
            geo_cache = dict(list(geo_cache.items())[-80:])
        db.set_meta("geocoded_places_cache_json", json.dumps(geo_cache, ensure_ascii=False))
    return found


def _haversine_km(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lng2 - lng1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2.0 * r * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def estimate_drive_eta_and_chargers(
    db: SimingDB,
    dest_query: str,
    orig_lat: Optional[float] = None,
    orig_lng: Optional[float] = None,
    amap_fetcher: Optional[Callable[[str], dict[str, Any]]] = None,
    *,
    now: Optional[datetime] = None,
    geocoder: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
    route_fetcher: Optional[Callable[[float, float, float, float], Optional[dict[str, Any]]]] = None,
    dest_coords: Optional[tuple[float, float]] = None,
) -> dict[str, Any]:
    """计算从当前 OwnTracks 坐标到目的地的实时驾车耗时、里程与周边超充站参考。"""
    now_dt = now or _cur_now()
    loc = resolve_current_location_context(db, pull_edge_if_older_sec=0)
    o_lat = orig_lat if orig_lat is not None else (float(loc["lat"]) if loc.get("lat") is not None else 39.904200)
    o_lng = orig_lng if orig_lng is not None else (float(loc["lng"]) if loc.get("lng") is not None else 116.407400)
    origin_label = str(loc.get("place_alias") or loc.get("addr") or "当前位置")

    d_clean = dest_query.strip()
    geocoded = False
    d_lat: Optional[float] = None
    d_lng: Optional[float] = None
    d_label = d_clean
    if dest_coords is not None:
        d_lat, d_lng = float(dest_coords[0]), float(dest_coords[1])
        geocoded = True
    else:
        resolved = resolve_destination_coords(db, d_clean, geocoder=geocoder)
        if resolved is not None:
            d_lat, d_lng = float(resolved["lat"]), float(resolved["lng"])
            d_label = str(resolved.get("label") or d_clean)
            geocoded = True

    amap_key = (read_secret("amap-key", db.paths) or "").strip()
    chargers: list[str] = []
    used_live_amap = False
    route_source = "fallback_city_default"
    road_km: Optional[float] = None
    dur_min: Optional[int] = None

    if geocoded and d_lat is not None and d_lng is not None and (amap_key or amap_fetcher is not None):
        try:
            url_dir = (
                f"https://restapi.amap.com/v3/direction/driving?origin={o_lng:.6f},{o_lat:.6f}"
                f"&destination={d_lng:.6f},{d_lat:.6f}&strategy=0&key={urllib.parse.quote(amap_key)}"
            )
            if amap_fetcher:
                res_dir = amap_fetcher(url_dir)
            else:
                with urllib.request.urlopen(url_dir, timeout=5.0) as resp:
                    res_dir = json.loads(resp.read().decode("utf-8"))
            paths = ((res_dir.get("route") or {}).get("paths") or [])
            if paths:
                road_km = round(float(paths[0].get("distance", 0)) / 1000.0, 1)
                dur_min = max(1, int(round(float(paths[0].get("duration", 0)) / 60.0)))
                used_live_amap = True
                route_source = "amap_live"

            url_chg = (
                f"https://restapi.amap.com/v3/place/around?location={d_lng:.6f},{d_lat:.6f}"
                f"&keywords={urllib.parse.quote('特斯拉超级充电站|快充充电站')}&radius=3000&offset=3&key={urllib.parse.quote(amap_key)}"
            )
            res_chg = amap_fetcher(url_chg) if amap_fetcher else json.loads(urllib.request.urlopen(url_chg, timeout=5.0).read().decode("utf-8"))
            for poi in (res_chg.get("pois") or [])[:3]:
                chargers.append(f"{poi.get('name')}（距目的地 {poi.get('distance')}m）")
        except Exception:
            pass

    # 次优：OSRM 真实路网 + 北京时段拥堵倍率（免 Key 可用）
    if (road_km is None or dur_min is None) and geocoded and d_lat is not None and d_lng is not None:
        if route_fetcher is not None:
            try:
                rf = route_fetcher(o_lat, o_lng, d_lat, d_lng)
                if rf and rf.get("road_km") is not None and rf.get("duration_min") is not None:
                    road_km = round(float(rf["road_km"]), 1)
                    dur_min = max(3, int(round(float(rf["duration_min"]))))
                    route_source = str(rf.get("source") or "injected_route")
            except Exception:
                pass
        elif os.environ.get("SIMING_TEST_OFFLINE") != "1":
            try:
                url_osrm = (
                    f"https://router.project-osrm.org/route/v1/driving/"
                    f"{o_lng:.6f},{o_lat:.6f};{d_lng:.6f},{d_lat:.6f}?overview=false"
                )
                with httpx.Client(
                    trust_env=False,
                    proxy=_http_proxy_url(),
                    timeout=4.5,
                    headers={"User-Agent": "siming-personal-assistant/1.0"},
                ) as client:
                    r = client.get(url_osrm)
                    if r.status_code == 200:
                        routes = r.json().get("routes") or []
                        if routes:
                            road_km = max(0.5, round(float(routes[0].get("distance", 0)) / 1000.0, 1))
                            free_min = float(routes[0].get("duration", 0)) / 60.0
                            mult = _beijing_traffic_multiplier(now_dt, db)
                            dur_min = max(5, int(round(free_min * mult + 3)))
                            route_source = "osrm_traffic_model"
            except Exception:
                pass

    if road_km is None or dur_min is None:
        if geocoded and d_lat is not None and d_lng is not None:
            straight_km = _haversine_km(o_lat, o_lng, d_lat, d_lng)
            road_km = max(1.0, round(straight_km * 1.32, 1))
            cur_h = now_dt.hour
            # 北京早晚高峰 (07:30-09:30 / 17:30-20:00) 平均环路+联络线约 24km/h，平峰约 36km/h
            avg_kmh = 24.0 if cur_h in (7, 8, 9, 17, 18, 19) else 36.0
            dur_min = max(5, int(round((road_km / avg_kmh) * 60.0 + 4)))
            route_source = "haversine_model"
        else:
            # 未知陌生地址（未地理编码）：按市内常规跨区车程保守预估 35 分钟，不误充默认住宅坐标
            road_km = 12.0
            dur_min = 35
            route_source = "fallback_city_default"

    eta_dt = now_dt + timedelta(minutes=dur_min)
    eta_iso = iso_now(eta_dt)
    sit_key = f"eta:{d_clean}"
    set_situation(
        db,
        sit_key,
        {
            "dest": d_label,
            "road_km": road_km,
            "duration_min": dur_min,
            "eta_at": eta_iso,
            "live_amap": used_live_amap,
            "geocoded": geocoded,
            "route_source": route_source,
        },
        source="amap_mcp" if used_live_amap else route_source,
    )

    tesla_check = check_tesla_range_for_trip(db, road_km, dest_label=d_label)
    return {
        "ok": True,
        "origin_label": origin_label,
        "dest_label": d_label,
        "dest_lat": d_lat,
        "dest_lng": d_lng,
        "geocoded": geocoded,
        "road_km": road_km,
        "duration_min": dur_min,
        "eta_at": eta_iso,
        "live_amap": used_live_amap,
        "route_source": route_source,
        "chargers": chargers,
        "tesla_range": tesla_check,
        "situation_key": sit_key,
    }


def plan_tesla_or_didi_outing(db: SimingDB, dest_query: str, target_at_iso: str = "") -> dict[str, Any]:
    """生成『自驾特斯拉（含续航与快充） vs 打滴滴』参谋卡；若传了预约到达时刻 `target_at_iso` 则自动落 `expectations` 盯路况余量！"""
    est = estimate_drive_eta_and_chargers(db, dest_query)
    # 估算滴滴快车参考价（北京起步 13 元 + ~2.8 元/km + 时长费）
    didi_est_yuan = int(round(max(14.0, 8.0 + est["road_km"] * 2.9 + est["duration_min"] * 0.5)))
    chg_line = f"\n• ⚡ 目的地周边快充：{'；'.join(est['chargers'])}" if est["chargers"] else ""

    exp_id = ""
    replan_res = None
    if target_at_iso:
        exp_id = upsert_expectation(
            db,
            kind="outing",
            chain_type="dependency",
            title=f"前往 {est['dest_label']} 赴约/办事",
            target_at=target_at_iso,
            flex_after_min=5,
            place=est["dest_label"],
            visibility="medium",
            depends=[est["situation_key"]],
            counterparty={"name": "对方联系人", "phone": ""},
        )
        replan_res = replan_impacted_dependencies(db, changed_keys=[est["situation_key"]], notify=False)

    lines = [
        f"🗺️ **出行参谋：前往 {est['dest_label']}**（单程约 `{est['road_km']} km`）",
        f"• 🚗 **自驾特斯拉**：预计耗时 **{est['duration_min']} 分钟**（预计 `{est['eta_at'][11:16]}` 到达）",
        f"• {est['tesla_range']['advice']}{chg_line}",
        f"• 🚕 **备选打滴滴快车**：预估约 `¥{didi_est_yuan}`（发 `打车去{dest_query}` 可直接出下单卡）",
    ]
    if replan_res and replan_res.get("impacted_count", 0) > 0:
        lines.append("\n" + replan_res["card_text"])
    return {"ok": True, "estimate": est, "expectation_id": exp_id, "replan": replan_res, "reply": "\n".join(lines)}


def register_commitment_destination_prompt(
    db: SimingDB,
    *,
    wait_id: str,
    exp_id: str,
    title: str,
    event_at: str,
) -> Optional[str]:
    """当截屏识别到约定但未包含目的地时，登记一条待主人回复的 `trip_destination` 主动求证问题。"""
    from siming.curiosity import enqueue_curiosity_question

    q_txt = f"约定「{title}（{event_at[:16].replace('T', ' ')}）」去哪里？"
    qid = enqueue_curiosity_question(
        db,
        topic="trip_destination",
        question=q_txt,
        context={
            "wait_id": wait_id,
            "exp_id": exp_id,
            "title": title,
            "event_at": event_at,
        },
        dedup_key=f"trip_dest:{wait_id}",
    )
    if qid:
        with db.transaction() as conn:
            conn.execute(
                "UPDATE curiosity_questions SET status='ASKED', asked_at=? WHERE qid=?",
                (iso_now(), qid),
            )
    return qid


def _clean_destination_answer(raw: str) -> str:
    s = (raw or "").strip()
    s = re.sub(
        r"^(?:地点(?:是|在)?|地址(?:是|在)?|目的地(?:是|在)?|这次(?:去|在)|约在|定在|我们去|要去|去|在)\s*[：:]?\s*",
        "",
        s,
    ).strip()
    s = re.sub(r"[。！!~～,，]+$", "", s).strip()
    return s


def _sync_wait_fire_time_and_prescheduled(
    db: SimingDB,
    *,
    wait_id: str,
    new_fire_iso: str,
    new_quote: str,
    new_context: str,
    now_dt: datetime,
    mark_done: bool = False,
) -> None:
    """更新挂起项的触发时刻与文案，并同步刷新边缘预排发件箱。"""
    from dataclasses import replace
    from siming.mandate import Mandate
    from siming.waits import export_active_waits_md

    wid = (wait_id or "").strip().upper()
    if not wid:
        return
    ts = iso_now(now_dt)
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM waits WHERE wait_id=?", (wid,)).fetchone()
        if not row or row["status"] in ("CANCELLED", "EXPIRED"):
            return
        mandate = replace(Mandate.from_json(row["mandate_json"]), context=new_context)
        safe_quote = redact_sensitive(new_quote, db.paths)
        if mark_done:
            conn.execute(
                "UPDATE waits SET status='DONE', next_fire_at=?, trigger_expr=?, quote=?, mandate_json=?, result_summary=?, updated_at=? WHERE wait_id=?",
                (new_fire_iso, new_fire_iso, safe_quote, mandate.to_json(), "已发送实时路况准备提醒", ts, wid),
            )
            conn.execute(
                "UPDATE outbox SET status='WITHDRAWN' WHERE ref_id=? AND status='PRESCHEDULED'",
                (wid,),
            )
            db.enqueue_effect(conn, "edge_withdraw_outbox", {"ref_id": wid})
        else:
            conn.execute(
                "UPDATE waits SET next_fire_at=?, trigger_expr=?, quote=?, mandate_json=?, updated_at=? WHERE wait_id=? AND status='WAITING'",
                (new_fire_iso, new_fire_iso, safe_quote, mandate.to_json(), ts, wid),
            )
            conn.execute(
                "UPDATE outbox SET status='WITHDRAWN' WHERE ref_id=? AND status='PRESCHEDULED'",
                (wid,),
            )
            fire_dt = parse_iso(new_fire_iso)
            if fire_dt > now_dt + timedelta(minutes=15):
                conn.execute(
                    """
                    INSERT INTO outbox (
                        id, ref_id, rule_id, text, send_at, status, channel,
                        hard_deadline, edge_synced, created_at
                    ) VALUES (?, ?, 'wait_time', ?, ?, 'PRESCHEDULED', 'qqbot', 1, 0, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        text=excluded.text, send_at=excluded.send_at, status='PRESCHEDULED', edge_synced=0
                    """,
                    (f"OB-{wid}", wid, f"[{wid}] 到点提醒：{safe_quote}", new_fire_iso, ts),
                )
                db.enqueue_effect(conn, "edge_sync_outbox", {"outbox_id": f"OB-{wid}", "ref_id": wid})
            else:
                db.enqueue_effect(conn, "edge_withdraw_outbox", {"ref_id": wid})
    export_active_waits_md(db)


def bind_commitment_destination(
    db: SimingDB,
    dest_text: str,
    *,
    wait_id: str = "",
    now: Optional[datetime] = None,
    geocoder: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
    amap_fetcher: Optional[Callable[[str], dict[str, Any]]] = None,
    route_fetcher: Optional[Callable[[float, float, float, float], Optional[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """为主人的约定行程绑定目的地（或标记就在家/线上），立即测算参考车程并动态校准动身准备提醒时间。"""
    db.init_db()
    now_dt = now or _cur_now()
    ts = iso_now(now_dt)

    # 1. 定位目标 expectation + wait + curiosity_question
    target_exp: Optional[dict[str, Any]] = None
    target_qid = ""
    conn = db.connect()
    try:
        wid_clean = (wait_id or "").strip().upper()
        if wid_clean:
            row = conn.execute(
                "SELECT * FROM expectations WHERE source_ref=? AND status IN ('ACTIVE','SHIFTED') ORDER BY created_at DESC LIMIT 1",
                (wid_clean,),
            ).fetchone()
            if row:
                target_exp = dict(row)
            q_r = conn.execute(
                "SELECT qid FROM curiosity_questions WHERE topic='trip_destination' AND status IN ('ASKED','PENDING') AND context_json LIKE ? ORDER BY created_at DESC LIMIT 1",
                (f"%{wid_clean}%",),
            ).fetchone()
            if q_r:
                target_qid = str(q_r["qid"])
        else:
            q_r = conn.execute(
                "SELECT * FROM curiosity_questions WHERE topic='trip_destination' AND status IN ('ASKED','PENDING') ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if q_r:
                target_qid = str(q_r["qid"])
                q_ctx = json.loads(q_r["context_json"] or "{}")
                q_eid = str(q_ctx.get("exp_id") or "")
                q_wid = str(q_ctx.get("wait_id") or "")
                row = None
                if q_eid:
                    row = conn.execute(
                        "SELECT * FROM expectations WHERE exp_id=? AND status IN ('ACTIVE','SHIFTED')",
                        (q_eid,),
                    ).fetchone()
                if not row and q_wid:
                    row = conn.execute(
                        "SELECT * FROM expectations WHERE source_ref=? AND status IN ('ACTIVE','SHIFTED')",
                        (q_wid,),
                    ).fetchone()
                if row:
                    target_exp = dict(row)
            if not target_exp:
                row = conn.execute(
                    "SELECT * FROM expectations WHERE kind='outing' AND status IN ('ACTIVE','SHIFTED') ORDER BY created_at DESC LIMIT 1"
                ).fetchone()
                if row:
                    target_exp = dict(row)
    finally:
        conn.close()

    if not target_exp:
        return {"ok": False, "matched": False, "reply": "⚠️ 当前没有待补充地点的活跃约定。"}

    exp_id = str(target_exp["exp_id"])
    wid = str(target_exp.get("source_ref") or wait_id or "").strip().upper()
    event_at = str(target_exp["target_at"])
    ev_dt = parse_iso(event_at)
    wd_cn = WEEKDAY_CN[ev_dt.weekday()]
    cp = json.loads(target_exp.get("counterparty_json") or "{}")
    base_title = str(cp.get("title") or target_exp["title"]).strip()
    raw_ans = (dest_text or "").strip()

    # 2. 特殊情形：在家 / 线上（无需出行、不开特斯拉备车、改为提前 15 分钟轻提醒）
    if _LOCAL_NO_TRAVEL_RE.match(raw_ans):
        remind_dt = max(now_dt + timedelta(minutes=1), ev_dt - timedelta(minutes=15))
        remind_iso = iso_now(remind_dt)
        cp.update({"needs_destination": False, "no_travel": True, "dest_label": "在家/线上"})
        prep_obj = {"no_travel": True, "prep_notified": False, "updated_at": ts}
        with db.transaction() as conn:
            conn.execute(
                "UPDATE expectations SET place='在家/线上', counterparty_json=?, prepared_json=?, updated_at=? WHERE exp_id=?",
                (json.dumps(cp, ensure_ascii=False), json.dumps(prep_obj, ensure_ascii=False), ts, exp_id),
            )
            if target_qid:
                conn.execute(
                    "UPDATE curiosity_questions SET status='ANSWERED', answer='在家/线上', answered_at=? WHERE qid=?",
                    (ts, target_qid),
                )
        if wid:
            _sync_wait_fire_time_and_prescheduled(
                db,
                wait_id=wid,
                new_fire_iso=remind_iso,
                new_quote=f"行程提醒：{base_title}（在家/线上 · {ev_dt.strftime('%m-%d')} {wd_cn} {ev_dt.strftime('%H:%M')}）",
                new_context="在家/线上进行，提前 15 分钟轻提醒",
                now_dt=now_dt,
            )
        return {
            "ok": True,
            "matched": True,
            "no_travel": True,
            "wait_id": wid,
            "exp_id": exp_id,
            "remind_at": remind_iso,
            "reply": (
                f"🏠 收到！**{base_title}（{ev_dt.strftime('%m-%d')} {wd_cn} {ev_dt.strftime('%H:%M')}）** 就在家/线上进行，"
                f"已将提醒 [{wid}] 调为 **{remind_dt.strftime('%m-%d %H:%M')}（提前 15 分钟）**，不会触发出门备车。"
            ),
        }

    # 3. 常规外出目的地：解析坐标 + 估算车程 + 校准准备/出发时间
    dest_clean = _clean_destination_answer(raw_ans)
    if not dest_clean:
        return {"ok": False, "matched": False, "reply": "⚠️ 没看清地点名称，请直接发地点（例如 `朝阳大悦城` 或 `去三里屯`）。"}

    est = estimate_drive_eta_and_chargers(
        db,
        dest_clean,
        now=now_dt,
        geocoder=geocoder,
        amap_fetcher=amap_fetcher,
        route_fetcher=route_fetcher,
    )
    drive_min = int(est["duration_min"])
    road_km = float(est["road_km"])
    dest_label = str(est["dest_label"])
    parking_buf = 10
    prep_buf = 15
    total_lead = min(180, max(25, drive_min + parking_buf + prep_buf))
    leave_dt = ev_dt - timedelta(minutes=min(165, max(15, drive_min + parking_buf)))
    prep_dt = ev_dt - timedelta(minutes=total_lead)

    imminent = prep_dt <= now_dt
    eff_remind_dt = now_dt if imminent else prep_dt
    eff_remind_iso = iso_now(eff_remind_dt)

    cp.update(
        {
            "needs_destination": False,
            "no_travel": False,
            "dest_raw": dest_clean,
            "dest_label": dest_label,
            "dest_lat": est.get("dest_lat"),
            "dest_lng": est.get("dest_lng"),
            "geocoded": bool(est.get("geocoded")),
            "est_drive_min": drive_min,
            "est_road_km": road_km,
            "leave_at": iso_now(leave_dt),
            "prep_at": iso_now(prep_dt),
        }
    )
    prep_obj = {
        "last_eta_check_at": ts,
        "duration_min": drive_min,
        "road_km": road_km,
        "route_source": est.get("route_source"),
        "leave_at": iso_now(leave_dt),
        "prep_at": iso_now(prep_dt),
        "prep_notified": imminent,
        "prep_notified_at": ts if imminent else "",
    }
    with db.transaction() as conn:
        conn.execute(
            "UPDATE expectations SET place=?, counterparty_json=?, prepared_json=?, updated_at=? WHERE exp_id=?",
            (dest_label, json.dumps(cp, ensure_ascii=False), json.dumps(prep_obj, ensure_ascii=False), ts, exp_id),
        )
        if target_qid:
            conn.execute(
                "UPDATE curiosity_questions SET status='ANSWERED', answer=?, answered_at=? WHERE qid=?",
                (dest_label, ts, target_qid),
            )

    if wid:
        _sync_wait_fire_time_and_prescheduled(
            db,
            wait_id=wid,
            new_fire_iso=eff_remind_iso,
            new_quote=f"行程准备提醒：{base_title} · 前往{dest_label}（{ev_dt.strftime('%m-%d')} {wd_cn} {ev_dt.strftime('%H:%M')}）",
            new_context=f"目的地：{dest_label}（参考车程约 {drive_min} 分钟 / {road_km} km，建议 {leave_dt.strftime('%H:%M')} 出门）",
            now_dt=now_dt,
            mark_done=imminent,
        )

    if imminent:
        set_situation(
            db,
            "outing:active",
            {
                "dest": dest_label,
                "depart_at": iso_now(leave_dt),
                "driving": True,
                "raw": f"{base_title} 前往 {dest_label}",
                "synced_at": ts,
            },
            source="trip_destination_bind",
            as_of=ts,
        )

    src_tag = (
        "高德实时路况"
        if est.get("live_amap")
        else ("路网+北京时段预估" if est.get("geocoded") else "市内常规车程参考")
    )
    dist_part = f"（单程约 `{road_km} km` · {src_tag}）" if est.get("geocoded") else f"（未查到精确坐标，按{src_tag}）"
    lines = [
        f"📍 记下了！**{base_title}（{ev_dt.strftime('%m-%d')} {wd_cn} {ev_dt.strftime('%H:%M')}）** 目的地已更新为 **{dest_label}**",
        f"• ⏱️ 参考车程：约 **{drive_min} 分钟**{dist_part}",
    ]
    if imminent:
        lines.append(f"• 🚦 **现在就该准备出门了**（建议 `{leave_dt.strftime('%H:%M')}` 前动身，含 {parking_buf} 分钟停车缓冲）")
    else:
        lines.append(
            f"• ⏰ 已将准备提醒 [{wid}] 校准至 **{prep_dt.strftime('%m-%d %H:%M')}**（预留 {drive_min}m 车程 + {parking_buf}m 停车 + {prep_buf}m 准备，建议 `{leave_dt.strftime('%H:%M')}` 出门）"
        )
        lines.append("• 🔄 临出行前约 2.5 小时我还会按**届时实时位置与路况**复算一次，若堵车会动态提前叫你准备并联动 Model Y 备车！")
    tesla_adv = (est.get("tesla_range") or {}).get("advice")
    if tesla_adv:
        lines.append(f"• {tesla_adv}")

    return {
        "ok": True,
        "matched": True,
        "no_travel": False,
        "wait_id": wid,
        "exp_id": exp_id,
        "dest_label": dest_label,
        "duration_min": drive_min,
        "road_km": road_km,
        "leave_at": iso_now(leave_dt),
        "prep_at": iso_now(prep_dt),
        "imminent": imminent,
        "reply": "\n".join(lines),
    }


def evaluate_upcoming_trip_prep(
    db: SimingDB,
    *,
    now: Optional[datetime] = None,
    geocoder: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
    amap_fetcher: Optional[Callable[[str], dict[str, Any]]] = None,
    route_fetcher: Optional[Callable[[float, float, float, float], Optional[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """临出行前动态路况巡检与准点准备提醒（每 2m tick 驱动）：
    - 在约定开始前 2.5 小时内，按主人当前 OwnTracks 位置重新查一次到目的地的驾车耗时；
    - 若路况变化导致出发时间提前，自动前移关联 wait 闹钟与出门备车窗口；
    - 一旦进入「该开始准备出门」时刻（`event_at - drive_min - 25m`），立即推送含实时车程、建议出门时刻、Model Y 电量/车温与打车备选的出行准备卡，并核销保底 wait 避免重复推送。
    """
    from siming.push import push_message

    db.init_db()
    now_dt = now or _cur_now()
    ts = iso_now(now_dt)

    conn = db.connect()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM expectations
                WHERE status IN ('ACTIVE', 'SHIFTED')
                  AND kind IN ('outing', 'hospital_visit')
                ORDER BY target_at ASC
                """
            ).fetchall()
        ]
    finally:
        conn.close()

    checked: list[str] = []
    notified: list[str] = []
    shifted: list[str] = []

    for exp in rows:
        exp_id = str(exp["exp_id"])
        place = str(exp.get("place") or "").strip()
        if not place:
            continue
        try:
            ev_dt = parse_iso(str(exp["target_at"]))
        except Exception:
            continue
        mins_to_event = (ev_dt - now_dt).total_seconds() / 60.0
        # 仅在约定前 2.5 小时内巡检（已开始的跳过）
        if mins_to_event <= 0 or mins_to_event > 150.0:
            continue

        cp = json.loads(exp.get("counterparty_json") or "{}")
        if cp.get("no_travel"):
            continue
        prep = json.loads(exp.get("prepared_json") or "{}")
        if prep.get("prep_notified"):
            continue

        # 控制刷新频率：临出行窗口首次必查；之后每 20 分钟复查一次，或距上次算出的 prep_at <= 10m 时临门再核一次
        last_live = str(prep.get("live_window_checked_at") or "")
        need_poll = not last_live
        if last_live:
            try:
                age_min = (now_dt - parse_iso(last_live)).total_seconds() / 60.0
                if age_min >= 20.0:
                    need_poll = True
            except Exception:
                need_poll = True
        if not need_poll and prep.get("prep_at"):
            try:
                mins_to_prep = (parse_iso(str(prep["prep_at"])) - now_dt).total_seconds() / 60.0
                if mins_to_prep <= 10.0:
                    age_min = (now_dt - parse_iso(last_live)).total_seconds() / 60.0 if last_live else 999.0
                    if age_min >= 5.0:
                        need_poll = True
            except Exception:
                pass

        saved_coords = None
        if cp.get("dest_lat") is not None and cp.get("dest_lng") is not None:
            saved_coords = (float(cp["dest_lat"]), float(cp["dest_lng"]))

        if need_poll:
            est = estimate_drive_eta_and_chargers(
                db,
                place,
                now=now_dt,
                geocoder=geocoder,
                amap_fetcher=amap_fetcher,
                route_fetcher=route_fetcher,
                dest_coords=saved_coords,
            )
            drive_min = int(est["duration_min"])
            road_km = float(est["road_km"])
            dest_label = str(est["dest_label"])
            origin_label = str(est.get("origin_label") or "当前位置")
            route_src = str(est.get("route_source") or "")
            live_amap = bool(est.get("live_amap"))
            geocoded = bool(est.get("geocoded"))
            tesla_range = est.get("tesla_range") or {}
            checked.append(exp_id)
        else:
            drive_min = int(prep.get("duration_min") or cp.get("est_drive_min") or 35)
            road_km = float(prep.get("road_km") or cp.get("est_road_km") or 12.0)
            dest_label = str(cp.get("dest_label") or place)
            origin_label = "当前位置"
            route_src = str(prep.get("route_source") or "")
            live_amap = route_src == "amap_live"
            geocoded = bool(cp.get("geocoded", True))
            tesla_range = check_tesla_range_for_trip(db, road_km, dest_label=dest_label)

        parking_buf = 10
        prep_buf = 15
        total_lead = min(180, max(25, drive_min + parking_buf + prep_buf))
        leave_dt = ev_dt - timedelta(minutes=min(165, max(15, drive_min + parking_buf)))
        prep_dt = ev_dt - timedelta(minutes=total_lead)

        old_prep_at = str(prep.get("prep_at") or "")
        new_prep_iso = iso_now(prep_dt)
        new_leave_iso = iso_now(leave_dt)

        prep.update(
            {
                "live_window_checked_at": ts if need_poll else last_live,
                "last_eta_check_at": ts if need_poll else str(prep.get("last_eta_check_at") or ts),
                "duration_min": drive_min,
                "road_km": road_km,
                "route_source": route_src,
                "leave_at": new_leave_iso,
                "prep_at": new_prep_iso,
            }
        )
        cp.update(
            {
                "est_drive_min": drive_min,
                "est_road_km": road_km,
                "leave_at": new_leave_iso,
                "prep_at": new_prep_iso,
            }
        )
        wid = str(exp.get("source_ref") or "").strip().upper()
        base_title = str(cp.get("title") or exp["title"]).strip()
        wd_cn = WEEKDAY_CN[ev_dt.weekday()]

        if now_dt >= prep_dt:
            prep["prep_notified"] = True
            prep["prep_notified_at"] = ts
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE expectations SET counterparty_json=?, prepared_json=?, updated_at=? WHERE exp_id=?",
                    (json.dumps(cp, ensure_ascii=False), json.dumps(prep, ensure_ascii=False), ts, exp_id),
                )
            if wid:
                _sync_wait_fire_time_and_prescheduled(
                    db,
                    wait_id=wid,
                    new_fire_iso=new_prep_iso,
                    new_quote=f"行程准备提醒：{base_title} · 前往{dest_label}（{ev_dt.strftime('%m-%d %H:%M')}）",
                    new_context=f"已推送临出行准备卡（车程 {drive_min}m，建议 {leave_dt.strftime('%H:%M')} 出门）",
                    now_dt=now_dt,
                    mark_done=True,
                )
            set_situation(
                db,
                "outing:active",
                {
                    "dest": dest_label,
                    "depart_at": new_leave_iso,
                    "driving": True,
                    "raw": f"{base_title} 前往 {dest_label}",
                    "synced_at": ts,
                },
                source="trip_prep_engine",
                as_of=ts,
            )

            sit_car = get_situation(db, "tesla:main")
            car_v = dict(sit_car["value"]) if sit_car else {}
            car_note = ""
            if car_v.get("soc_pct") is not None:
                in_t = car_v.get("inside_temp_c")
                temp_s = f" · 车内 `{float(in_t):.1f}°C`" if in_t is not None else ""
                car_note = f"⚡ Model Y：电量 **{float(car_v['soc_pct']):.0f}%**{temp_s}"
                if tesla_range.get("advice"):
                    car_note += f"（{tesla_range['advice']}）"
            elif tesla_range.get("advice"):
                car_note = str(tesla_range["advice"])

            src_tag = "实时路况" if live_amap else ("路网+时段预估" if geocoded else "市内参考")
            dist_s = f"（从「{origin_label}」出发约 `{road_km} km`）" if geocoded else ""
            msg_lines = [
                f"🚦 **该准备出门了**：**{base_title}**（`{ev_dt.strftime('%H:%M')}` 开始）",
                f"• 📍 目的地：**{dest_label}**{dist_s}",
                f"• ⏱️ {src_tag}预计车程：**{drive_min} 分钟**，建议 **`{leave_dt.strftime('%H:%M')}` 前出门**（含 {parking_buf} 分钟停车缓冲）",
            ]
            if car_note:
                msg_lines.append(f"• {car_note}")
            msg_lines.append(f"• 🚕 若不想开车，直接回 `打车去{dest_label}` 即可唤出滴滴")

            push_message(
                db,
                text="\n".join(msg_lines),
                rule_id="trip_prep",
                ref_id=exp_id,
                level="now",
                sub_meta={"kind": "trip_prep", "exp_id": exp_id, "wait_id": wid, "dest": dest_label},
                now=now_dt,
            )
            notified.append(exp_id)
        else:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE expectations SET counterparty_json=?, prepared_json=?, updated_at=? WHERE exp_id=?",
                    (json.dumps(cp, ensure_ascii=False), json.dumps(prep, ensure_ascii=False), ts, exp_id),
                )
            # 若路况使建议准备时间变动 >=5 分钟，同步刷新挂起项与 81 预排
            shift_needed = False
            if old_prep_at:
                try:
                    shift_needed = abs((prep_dt - parse_iso(old_prep_at)).total_seconds()) >= 300
                except Exception:
                    shift_needed = True
            else:
                shift_needed = True
            if wid and shift_needed:
                _sync_wait_fire_time_and_prescheduled(
                    db,
                    wait_id=wid,
                    new_fire_iso=new_prep_iso,
                    new_quote=f"行程准备提醒：{base_title} · 前往{dest_label}（{ev_dt.strftime('%m-%d')} {wd_cn} {ev_dt.strftime('%H:%M')}）",
                    new_context=f"临出行复算车程约 {drive_min} 分钟（{road_km} km），建议 {leave_dt.strftime('%H:%M')} 出门",
                    now_dt=now_dt,
                )
                shifted.append(exp_id)

    return {"ok": True, "checked": checked, "notified": notified, "shifted": shifted}
