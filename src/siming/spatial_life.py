"""空间情境驱动生活闭环（F04 + F05 + F06 · spatial_life.py）：
1. F04 `OwnTracks` 真下班离司触发顺路取件 + 冷链生鲜防化冻加急守卫（取代死板 18:00 闹钟）；
2. F05 加班晚餐「A 19:35 静默预备秒回 + B 20:05 轻推锁购物车卡」双模点餐员；
3. F06 医院复诊挂号 → 就诊前晚 20:00 带抽血单/空腹提醒 → 离院补药库存全闭环。
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, time, timedelta
from typing import Any, Optional

from siming.cn_calendar import day_info, try_parse_explicit_datetime
from siming.config import load_policy
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, logical_today, now_local, parse_iso
from siming.event_chain import get_situation, set_situation, upsert_expectation
from siming.modules import is_module_enabled

COLD_PICKUP_RE = re.compile(r"(冷链|冷冻|冰品|冰淇淋|榴莲|生鲜|顺丰冷运|冷藏)")


def _find_offwork_office_departure(conn, today_s: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """按当日空间轨迹识别 17:00 后真正离开公司的事件（覆盖 `离场: 公司`、OwnTracks `leave`、以及 `公司 -> 车库/家` 直接跳转），
    并返回离司后首次『到场: 家』的时间戳（防止到家久坐后再误发过期提醒）。"""
    cutoff_17 = f"{today_s}T17:00:00+08:00"
    rows = conn.execute(
        """
        SELECT event_id, occurred_at, summary, payload_json FROM events
        WHERE source='ios_loc' AND occurred_at >= ?
        ORDER BY occurred_at ASC, rowid ASC
        """,
        (f"{today_s}T00:00:00+08:00",),
    ).fetchall()

    was_at_office = False
    dep_ev: Optional[dict[str, Any]] = None
    home_arrived_after_dep: Optional[str] = None

    for r in rows:
        summary = str(r["summary"] or "")
        occ = str(r["occurred_at"] or "")
        try:
            payload = json.loads(r["payload_json"] or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}

        ev_alias = str(payload.get("place_alias") or "").strip()
        ev_prev = str(payload.get("prev_alias") or "").strip()
        ev_ot = str(payload.get("ot_event") or "").strip().lower()

        explicit_leave = (
            summary.startswith("离场: 公司")
            or ("离开围栏" in summary and "公司" in summary)
            or (ev_ot == "leave" and (ev_prev == "公司" or ev_alias == "公司" or "公司" in summary or was_at_office))
        )
        alias_transition_away = ev_prev == "公司" and ev_alias != "公司"
        trajectory_left_office = was_at_office and ev_alias != "公司"

        if occ >= cutoff_17 and (explicit_leave or alias_transition_away or trajectory_left_office):
            dep_ev = {"event_id": r["event_id"], "occurred_at": occ, "summary": summary}
            home_arrived_after_dep = occ if (ev_alias == "家" or summary.startswith("到场: 家")) else None
        elif dep_ev is not None:
            if ev_alias == "家" or summary.startswith("到场: 家"):
                if home_arrived_after_dep is None:
                    home_arrived_after_dep = occ
            elif ev_alias != "家":
                home_arrived_after_dep = None

        if explicit_leave:
            was_at_office = False
        elif ev_alias == "公司" or summary.startswith("到场: 公司") or ("进入围栏" in summary and "公司" in summary):
            was_at_office = True
        else:
            was_at_office = False

    return dep_ev, home_arrived_after_dep


def _parse_case_ctx(raw_ctx: Any) -> dict[str, Any]:
    if not raw_ctx:
        return {}
    try:
        obj = json.loads(raw_ctx)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _parcel_is_due_today(row: dict[str, Any], ctx: dict[str, Any], today_dt: datetime) -> bool:
    """严格判定在途订单（截图预测，未收快递短信）是否预计今日送达：
    - 备货中 / 预计发货一律跳过；
    - 有明确日期（eta_date / due_at / next_step 里的 MM-DD 或 M月D日）时，必须严格等于今天（彻底消除昨日『今日(11-19)』隔天误推）；
    - 无明确日期的旧文案仅当创建于当天/前一天且相对词对齐今日时才算，绝不再靠 title 含『冷链』跨天盲推。
    """
    today_d = today_dt.date()
    today_s = today_d.isoformat()
    ns = str(row.get("next_step") or "")
    if str(ctx.get("eta_kind") or "") == "ship" or "备货中" in ns or ("发货" in ns and "送达" not in ns):
        return False

    eta_date = str(ctx.get("eta_date") or "").strip()[:10]
    if eta_date and re.fullmatch(r"\d{4}-\d{2}-\d{2}", eta_date):
        return eta_date == today_s

    due_at = str(row.get("due_at") or "").strip()[:10]
    if due_at and re.fullmatch(r"\d{4}-\d{2}-\d{2}", due_at):
        return due_at == today_s

    explicit_mds = re.findall(r"(?<!\d)(\d{1,2})[-月](\d{1,2})日?(?!\d)", ns)
    if explicit_mds:
        for mo_s, dy_s in explicit_mds:
            if int(mo_s) == today_d.month and int(dy_s) == today_d.day:
                return True
        return False

    created_d = str(row.get("created_at") or "").strip()[:10]
    if created_d:
        if any(k in ns for k in ("今日", "今天")):
            return created_d == today_s
        if any(k in ns for k in ("明日", "明天")):
            return created_d == (today_d - timedelta(days=1)).isoformat()
    return False


def _format_confirmed_pickup_line(row: dict[str, Any], ctx: dict[str, Any]) -> str:
    cid = str(row["case_id"])
    location = str(ctx.get("location") or ctx.get("drop_loc") or "").strip()
    brand = str(ctx.get("brand") or ctx.get("carrier") or "").strip()
    station = str(ctx.get("station") or "").strip()
    site_alias = str(ctx.get("site_alias") or "").strip()
    site_label = str(ctx.get("site_label") or "").strip()
    free_win = str(ctx.get("free_window") or "").strip()
    matched_item = str(ctx.get("matched_order_name") or "").strip()

    loc_disp = location or station or str(row["title"])
    if brand and brand not in loc_disp and not ctx.get("drop_loc"):
        loc_disp = f"{brand}·{loc_disp}"
    site_note_parts: list[str] = []
    if site_alias == "家" and "阳光花园" in loc_disp and "示例小区A" not in loc_disp:
        site_note_parts.append("示例小区A同院")
    elif site_alias == "公司":
        site_note_parts.append("公司附近")
    elif site_alias == "新房":
        site_note_parts.append("新房·示例小区B，非当前住址顺路")
    elif site_label and site_label not in loc_disp and site_label != "家门口":
        site_note_parts.append(site_label)
    if free_win:
        site_note_parts.append(free_win)

    if ctx.get("drop_loc"):
        carrier = str(ctx.get("carrier") or "快递")
        tail_s = f" 尾号`{ctx['tail_no']}`" if ctx.get("tail_no") else ""
        item_s = f"（{matched_item}）" if matched_item else ""
        note_s = f"（{' · '.join(site_note_parts)}）" if site_note_parts else ""
        return f"• `[{cid}]` **{carrier}{tail_s}**{item_s} · 已放 **{loc_disp}**{note_s}"

    code = str(ctx.get("code") or "").strip()
    code_part = f" · 取件码 `{code}`" if code else ""
    item_s = f"（关联：{matched_item}）" if matched_item else ""
    note_s = f"（{' · '.join(site_note_parts)}）" if site_note_parts else ""
    return f"• `[{cid}]` **{loc_disp}**{code_part}{item_s}{note_s}"


def check_and_fire_offwork_pickup(db: SimingDB, now: Optional[datetime] = None) -> dict[str, Any]:
    """F04：工作日 17:00 后一旦检测到离开公司，区分『已到货确切取件码/门口快递』、『截图预计今日到货（待物流确认）』与『非顺路新房（示例小区B）快递』分层推送！"""
    if not is_module_enabled(db, "spatial_pickup"):
        return {"ok": True, "triggered": False, "skipped": "module_disabled"}
    now_dt = now or now_local()
    di = day_info(logical_today(now_dt), db.paths)
    if not di.is_workday:
        return {"ok": True, "triggered": False, "skipped": "non_workday", "day_kind": di.kind_label}
    if now_dt.hour < 17:
        return {"ok": True, "triggered": False, "skipped": "before_1700"}

    today_s = now_dt.date().isoformat()
    if db.get_meta("last_spatial_pickup_day") == today_s:
        return {"ok": True, "skipped": "already_pushed_today"}

    from siming.collectors.pickup_codes import classify_pickup_site

    conn = db.connect()
    try:
        dep_ev, home_arrived_after_dep = _find_offwork_office_departure(conn, today_s)
        raw_cases = [
            dict(r)
            for r in conn.execute(
                """
                SELECT case_id, title, next_step, due_at, category, context_json, created_at
                FROM cases
                WHERE status='待处理' AND category IN ('pickup', 'pickup_code', 'parcel')
                ORDER BY created_at ASC
                """
            ).fetchall()
        ]
    finally:
        conn.close()

    try:
        notified_other_ids = set(json.loads(db.get_meta("notified_other_site_pickup_ids") or "[]"))
    except Exception:
        notified_other_ids = set()

    home_pickups: list[tuple[dict[str, Any], dict[str, Any]]] = []
    other_site_pickups: list[tuple[dict[str, Any], dict[str, Any]]] = []
    home_due_parcels: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for r in raw_cases:
        ctx = _parse_case_ctx(r.get("context_json"))
        cat = str(r.get("category") or "")
        if cat in ("pickup", "pickup_code"):
            site_alias = str(ctx.get("site_alias") or "").strip()
            if not site_alias:
                loc_hint = str(ctx.get("location") or ctx.get("station") or r.get("title") or "")
                site_info = classify_pickup_site(db, loc_hint, f"{r.get('title') or ''} {r.get('next_step') or ''}")
                site_alias = site_info["site_alias"]
                if site_alias:
                    ctx = {**ctx, **site_info}
            if site_alias == "新房":
                if str(r["case_id"]) not in notified_other_ids:
                    other_site_pickups.append((r, ctx))
            else:
                home_pickups.append((r, ctx))
        elif cat == "parcel":
            if not _parcel_is_due_today(r, ctx, now_dt):
                continue
            p_site = str(ctx.get("site_alias") or "").strip()
            if p_site == "新房":
                continue
            home_due_parcels.append((r, ctx))

    total_items = len(home_pickups) + len(home_due_parcels) + len(other_site_pickups)
    if not dep_ev or total_items == 0:
        return {"ok": True, "triggered": False, "pending_pickups": total_items}

    if home_arrived_after_dep:
        try:
            settled_sec = (now_dt - parse_iso(home_arrived_after_dep)).total_seconds()
            if settled_sec > 600:
                return {"ok": True, "triggered": False, "skipped": "already_home_settled", "pending_pickups": total_items}
        except Exception:
            pass

    lines: list[str] = []
    if home_pickups or home_due_parcels:
        lines.append("📦 **检测到你刚离开公司下班，回家包裹清单已整理好**：")
    else:
        lines.append("🏠 **新房（示例小区B）快递到站提醒（非当前回家顺路）**：")

    if home_pickups:
        lines.append("✅ **已到货 / 确切待取（来自快递短信）**：")
        for r, ctx in home_pickups[:6]:
            lines.append(_format_confirmed_pickup_line(r, ctx))

    if home_due_parcels:
        lines.append("🚚 **预计今日送达（截图预测，尚未收到取件短信，请看平台物流核实）**：")
        for r, ctx in home_due_parcels[:6]:
            lines.append(f"• `[{r['case_id']}]` **{r['title']}** — {r['next_step']}")

    if other_site_pickups:
        if home_pickups or home_due_parcels:
            lines.append("🏠 **非顺路提醒 · 新房（示例小区B）已到快递（不在示例小区A）**：")
        for r, ctx in other_site_pickups[:4]:
            lines.append(_format_confirmed_pickup_line(r, ctx))

    lines.append("（取完可点下方按钮或回 `已办 S编号` 逐件销账；不同包裹独立计件）")
    msg = "\n".join(lines)

    confirmed_home_ids = [str(r["case_id"]) for r, _ in home_pickups]
    all_reminded_ids = (
        confirmed_home_ids
        + [str(r["case_id"]) for r, _ in home_due_parcels]
        + [str(r["case_id"]) for r, _ in other_site_pickups]
    )
    primary_ref = all_reminded_ids[0]

    db.set_meta("last_spatial_pickup_day", today_s)
    db.set_meta("last_spatial_pickup_at", iso_now(now_dt))
    if other_site_pickups:
        notified_other_ids.update(str(r["case_id"]) for r, _ in other_site_pickups)
        db.set_meta("notified_other_site_pickup_ids", json.dumps(sorted(notified_other_ids)[-50:], ensure_ascii=False))

    from siming.push import push_message

    push_message(
        db,
        text=msg,
        level="normal",
        rule_id="spatial_pickup",
        ref_id=primary_ref,
        sub_meta={
            "rule_id": "spatial_pickup",
            "batch_case_ids": confirmed_home_ids or all_reminded_ids,
            "all_case_ids": all_reminded_ids,
        },
    )
    return {
        "ok": True,
        "triggered": True,
        "count": total_items,
        "confirmed_home_count": len(home_pickups),
        "due_parcel_count": len(home_due_parcels),
        "other_site_count": len(other_site_pickups),
        "text": msg,
    }


def check_urgent_cold_chain_sms(
    db: SimingDB,
    sms_text: str,
    *,
    case_id: str = "",
    site_alias: str = "",
    location: str = "",
) -> Optional[str]:
    """仅当短信自身明确含冷链/生鲜/冷冻词，或凭运单/尾号精确命中在途冷链单时，才不等下班立即发防化冻提醒！
    严禁把无冷链词、无运单号的普通丰巢/驿站短信误判为冷链急件。"""
    from siming.superagent_triage import match_pickup_sms_to_tracked_parcel

    sms_is_cold = bool(COLD_PICKUP_RE.search(sms_text or ""))
    matched_parcel = match_pickup_sms_to_tracked_parcel(db, sms_text, site_alias=site_alias)
    verified_match = bool(
        matched_parcel
        and matched_parcel.get("is_cold_chain")
        and matched_parcel.get("match_confidence") in ("verified_waybill", "cold_sms")
    )
    if not sms_is_cold and not verified_match:
        return None

    parcel_hint = f"（关联在途单：`[{matched_parcel['case_id']}] {matched_parcel['title']}`）" if matched_parcel else ""
    site_warn = ""
    if site_alias == "新房":
        site_warn = f"\n🏠 **注意：短信显示地点在新房（{location or '示例小区B'}），不在当前住址示例小区A！**"
    elif location:
        site_warn = f"\n📍 地点：{location}"
    ref_id = case_id or (str(matched_parcel["case_id"]) if matched_parcel else f"cold-{hash(sms_text) & 0xFFFFF}")
    alert = (
        f"🧊 **冷链/生鲜包裹到站急件提醒**{parcel_hint}：\n"
        f"> {sms_text.strip()[:160]}{site_warn}\n"
        "⚠️ 短信或运单提示含需冷冻/冷藏物品，请尽快取回放冰箱防化冻！"
    )
    from siming.push import push_message

    push_message(db, text=alert, level="normal", rule_id="spatial_pickup", ref_id=ref_id)
    return alert


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))


def _is_confirmed_at_office(db: SimingDB, loc_obj: dict[str, Any]) -> bool:
    """确认当前空间坐标/围栏确实在公司且明确不在家（防止居家/周末/漂移误判）。"""
    alias = str(loc_obj.get("place_alias") or "").strip()
    if alias == "家":
        return False

    lat_raw = loc_obj.get("lat")
    lon_raw = loc_obj.get("lng") if loc_obj.get("lng") is not None else loc_obj.get("lon")
    has_coords = False
    lat = lon = 0.0
    if lat_raw is not None and lon_raw is not None:
        try:
            lat, lon = float(lat_raw), float(lon_raw)
            has_coords = True
        except Exception:
            has_coords = False

    office_coords: list[tuple[float, float]] = []
    home_coords: list[tuple[float, float]] = []
    try:
        pol = load_policy(db.paths)
        for reg in (pol.get("daily_care") or {}).get("regimes") or []:
            if reg.get("office_lat") is not None and reg.get("office_lon") is not None:
                office_coords.append((float(reg["office_lat"]), float(reg["office_lon"])))
            if reg.get("home_lat") is not None and reg.get("home_lon") is not None:
                home_coords.append((float(reg["home_lat"]), float(reg["home_lon"])))
    except Exception:
        pass

    if alias == "公司":
        if has_coords and home_coords and office_coords:
            d_home = min(_haversine_m(lat, lon, hlat, hlon) for hlat, hlon in home_coords)
            d_off = min(_haversine_m(lat, lon, olat, olon) for olat, olon in office_coords)
            if d_home <= 150.0 and d_home < d_off:
                return False
        return True

    if has_coords and office_coords:
        d_off = min(_haversine_m(lat, lon, olat, olon) for olat, olon in office_coords)
        d_home = min((_haversine_m(lat, lon, hlat, hlon) for hlat, hlon in home_coords), default=999999.0)
        if d_off <= 200.0 and d_off < d_home:
            return True

    return False


def evaluate_overtime_meal_ab(db: SimingDB, now: Optional[datetime] = None, notify: bool = True) -> dict[str, Any]:
    """F05 加班晚餐 A+B 双模：
    - A 档（19:35+）：工作日仍在公司且 17:00 后无点餐/取餐柜记录 -> 后台静默算好 36h 防腻主推+备选写入 `situation['prestage:dinner']`，问 `吃什么` 0 秒秒回；
    - B 档（20:05+）：仍未点餐 -> 自动轻推 1 张双选卡，直接支持回 `1` 或 `确认` 后台锁美团购物车！
    """
    if not is_module_enabled(db, "overtime_meal"):
        return {"ok": True, "skipped": "module_disabled"}
    now_dt = now or now_local()
    di = day_info(logical_today(now_dt), db.paths)
    if not di.is_workday:
        return {"ok": True, "skipped": "non_workday", "day_kind": di.kind_label}
    hhmm = now_dt.strftime("%H:%M")
    if not ("19:35" <= hhmm <= "21:45"):
        return {"ok": True, "skipped": "outside_dinner_window"}

    raw_loc = db.get_meta("latest_ios_location") or ""
    if not raw_loc:
        return {"ok": True, "skipped": "no_loc"}
    try:
        loc_obj = json.loads(raw_loc)
        if not isinstance(loc_obj, dict):
            return {"ok": True, "skipped": "bad_loc"}
    except Exception:
        return {"ok": True, "skipped": "bad_loc"}
    if not _is_confirmed_at_office(db, loc_obj):
        return {"ok": True, "skipped": "not_at_office"}

    today_s = now_dt.date().isoformat()
    since_17 = f"{today_s}T17:00:00+08:00"
    from siming.meal_guard import find_recent_food_activity, is_meal_acked

    if is_meal_acked(db, now_dt, window="dinner", lookback_hours=5.0):
        return {"ok": True, "skipped": "dinner_already_acked"}

    recent_food = find_recent_food_activity(db, now_dt, since_iso=since_17)
    if recent_food:
        return {"ok": True, "skipped": "dinner_already_detected", "recent_food": recent_food}

    # Mode A：静默预计算并缓存到 situation["prestage:dinner"]（在线时走美团当前位置实时核验，离线测试自动退回历史）
    from siming.meituan_agent import recommend_meituan_meals, resolve_meituan_delivery_target, stage_meituan_h5_order

    loc_target = resolve_meituan_delivery_target(db)
    loc_key = str(loc_target.get("alias") or loc_target.get("label") or "")

    sit = get_situation(db, "prestage:dinner")
    if not sit or str(sit["value"].get("date") or "") != today_s or str(sit["value"].get("loc_key") or "") != loc_key:
        rec = recommend_meituan_meals(db, hint="晚餐", check_cdp=True, speculate=False)
        if rec.get("live_verified") and rec.get("speculative_batch_id"):
            try:
                from siming.meituan_speculate import run_speculative_meal_preplan

                run_speculative_meal_preplan(db, batch_id=str(rec["speculative_batch_id"]), max_workers=3)
            except Exception:
                pass
        picks = (rec.get("picks") or [])[:2]
        if not picks:
            picks = [{"shop": "素坤泰·泰式简餐", "avg_yuan": 38.0}, {"shop": "老乡鸡", "avg_yuan": 34.0}]
        set_situation(
            db,
            "prestage:dinner",
            {
                "date": today_s,
                "loc_key": loc_key,
                "live_verified": bool(rec.get("live_verified")),
                "picks": picks,
                "formatted": rec.get("reply") or "",
            },
            source="overtime_meal_a",
            as_of=iso_now(now_dt),
        )
    else:
        picks = sit["value"].get("picks") or []

    # Mode B：20:05 后轻推单张可一键确认锁购物车的加班晚餐卡（每日最多 1 次）
    pushed_b = False
    card_txt = ""
    if hhmm >= "20:05" and db.get_meta("last_overtime_meal_b_day") != today_s and picks:
        db.set_meta("last_overtime_meal_b_day", today_s)
        top1 = picks[0]
        top2 = picks[1] if len(picks) > 1 else None
        shop1 = str(top1.get("shop") or top1.get("merchant") or "常点外卖")
        staged = stage_meituan_h5_order(db, shop1, auto_preview_pay=False)
        tok1 = str(staged.get("approval_token") or staged.get("token") or "")
        shop2 = str((top2 or {}).get("shop") or (top2 or {}).get("merchant") or "")
        opt2_line = f"\n• 2️⃣ 备选：**{shop2}**（均价 `¥{top2.get('avg_yuan', 35):.0f}` · 发 `点外卖 {shop2}`）" if top2 and shop2 else ""
        card_txt = (
            f"🍱 **加班辛苦了（20:05 仍在公司未见晚餐记录），已提前备好 36h 防腻方案**：\n"
            f"• 1️⃣ 主推：**{shop1}**（均价 `¥{top1.get('avg_yuan', 38):.0f}`）{opt2_line}\n"
            f"👉 直接回 **「1」** 或 **「确认」**（或 `apply {tok1}`），后台立即在美团加好购物车等你手机点一下付款！"
        )
        if notify:
            from siming.push import push_message

            push_message(db, text=card_txt, level="normal", rule_id="overtime_meal", ref_id=f"ot-dinner-{today_s}")
        pushed_b = True

    return {"ok": True, "mode_a_ready": True, "mode_b_pushed": pushed_b, "picks_count": len(picks), "card_text": card_txt}


def get_prestaged_dinner_fast_reply(db: SimingDB) -> Optional[str]:
    """当用户在 QQ 主动问『吃什么』时，若 Mode A 已按同一位置静默备好今日实测方案，0ms 直接命中返回！"""
    import os

    sit = get_situation(db, "prestage:dinner")
    if not sit:
        return None
    val = sit.get("value") or {}
    if str(val.get("date") or "") != now_local().date().isoformat():
        return None
    # 在线模式下，仅当缓存已做过美团实时位置核验且用户当前未换位置时才复用，防止返回异地/未核验方案
    if os.environ.get("SIMING_TEST_OFFLINE") != "1" and not val.get("live_verified"):
        return None
    cached_loc = str(val.get("loc_key") or "")
    if cached_loc:
        from siming.meituan_agent import resolve_meituan_delivery_target

        cur_loc = resolve_meituan_delivery_target(db)
        cur_key = str(cur_loc.get("alias") or cur_loc.get("label") or "")
        if cur_key and cur_key != cached_loc:
            return None
    fmt = str(val.get("formatted") or "").strip()
    return f"⚡ *[已命中后台静默预响应]*\n{fmt}" if fmt else None


def ingest_hospital_appointment_and_arm_chain(db: SimingDB, text: str) -> dict[str, Any]:
    """F06：识别医院复诊预约短信或口头告知（如『约了12月3日上午医院内分泌』），
    自动建立就诊日承诺 + 前一天晚上 20:00『带抽血单/空腹要求』义务提醒！
    """
    clean = (text or "").strip()
    from siming.daily_care import load_care_cfg

    care_cfg = load_care_cfg(db)
    inj_cfg = care_cfg.get("injection") or {}
    hosp_name = str(inj_cfg.get("hospital") or "常用医院").strip()
    drug_name = str(inj_cfg.get("drug") or "周期用药").strip()

    if re.search(r"(?:一般|每周|平时|如果|不是|？|\?)", clean):
        return {"matched": False}
    has_hosp_kw = (hosp_name and hosp_name in clean) or ("内分泌" in clean) or ("抽血" in clean) or (
        "医院" in clean and bool(re.search(r"(?:约了|已约|预约了|预约成功|已成功预约|挂上|挂号成功|复诊)", clean))
    )
    if not has_hosp_kw:
        return {"matched": False}
    parsed = try_parse_explicit_datetime(clean, paths=db.paths)
    if parsed is None:
        return {"matched": False}
    visit_dt, _ = parsed
    prep_dt = datetime.combine(visit_dt.date() - timedelta(days=1), time(20, 0), tzinfo=SHANGHAI_TZ)

    visit_eid = upsert_expectation(
        db,
        exp_id=f"E-HOSP-{visit_dt.strftime('%Y%m%d')}",
        kind="hospital_visit",
        chain_type="obligation",
        title=f"{hosp_name}复诊开药（{visit_dt.strftime('%m-%d %H:%M')}）",
        target_at=iso_now(visit_dt),
        place=hosp_name,
        visibility="high",
        source_ref="hospital_chain",
    )
    prep_eid = upsert_expectation(
        db,
        exp_id=f"E-HOSP-PREP-{visit_dt.strftime('%Y%m%d')}",
        kind="hospital_prep",
        chain_type="obligation",
        title=f"明日去{hosp_name}前准备：带抽血化验单/医保凭证（确认是否需空腹）",
        target_at=iso_now(prep_dt),
        visibility="high",
        source_ref="hospital_chain",
    )
    from siming.waits import add_wait

    if prep_dt > now_local():
        add_wait(
            db,
            when=f"time:{iso_now(prep_dt)}",
            task=f"明天去{hosp_name}复诊开{drug_name}，今晚提前把化验单装包里，若查空腹指标明早别吃早饭；开完药回来发『补药 1支』自动更新库存。",
            mode="remind",
            strength="explicit",
            quote=clean[:120],
        )
    return {"matched": True, "visit_exp_id": visit_eid, "prep_exp_id": prep_eid, "visit_at": iso_now(visit_dt)}
