"""Wave 1 (W1-5 · parcel_sentinel)：丰巢 18h 防超期畅存费倒计时 + 抖音/拼多多/京东 7 天无理由退货与运费险哨兵。

能力：
1. 丰巢 18h 免费存放倒计时：仅当 `0 < remaining_min <= 120` 且本人在家（或位置未知）时触发高优穿透告警，单件去重严防 畅存费；
2. 7 天无理由退货与运费险到期哨兵：扫描抖音/拼多多/京东已签收包裹，剩余 `<= 48h` 时提前预警，防错过退货窗口。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Optional

from siming import db as _db_mod
from siming.db import SimingDB, iso_now, now_local, parse_iso
from siming.event_chain import get_situation
from siming.modules import is_module_enabled

_ORIG_NOW_LOCAL = now_local


def _cur_now() -> datetime:
    return now_local() if now_local is not _ORIG_NOW_LOCAL else _db_mod.now_local()

_HIVEBOX_RE = re.compile(r"(丰巢|hivebox)", re.IGNORECASE)
_NOT_HIVEBOX_RE = re.compile(r"(非丰巢|不是丰巢)", re.IGNORECASE)
_NON_HIVEBOX_BRAND_RE = re.compile(r"(菜鸟|兔喜|妈妈驿站|近邻宝|中通|圆通|申通|韵达|极兔|京东快递|顺丰驿站)", re.IGNORECASE)
_PICKED_UP_RE = re.compile(r"(已(?:成功)?取(?:件|出)|取(?:件|出)成功|成功取(?:件|出)|签收成功|已签收|已退回|已取消)", re.IGNORECASE)
_FREE_HOURS_RE = re.compile(r"(\d+)\s*(?:小时|h)\s*(?:内)?免费", re.IGNORECASE)
_ECOM_PLATFORM_RE = re.compile(r"(抖音|douyin|拼多多|pdd|京东|jd)", re.IGNORECASE)
_DELIVERED_RE = re.compile(r"(已签收|已送达|签收成功|已取件|delivered|completed|已完成)", re.IGNORECASE)


def _is_home_alias(alias: str) -> bool:
    return alias.strip().lower() in ("家", "home", "at_home")


def _infer_is_home(db: SimingDB, cur: datetime, is_home: Optional[bool]) -> bool:
    if is_home is not None:
        return bool(is_home)

    # 1) 先查事件链实时情境 `loc:me`
    sit = get_situation(db, "loc:me")
    if sit and isinstance(sit.get("value"), dict):
        val = sit["value"]
        alias = str(val.get("place_alias") or val.get("alias") or val.get("place") or val.get("state") or "").strip()
        if alias:
            return _is_home_alias(alias)

    # 2) 再查 `latest_ios_location`
    raw_loc = db.get_meta("latest_ios_location") or ""
    if raw_loc:
        try:
            obj = json.loads(raw_loc)
            if isinstance(obj, dict):
                upd_s = str(obj.get("updated_at") or obj.get("ts") or obj.get("as_of") or "")
                fresh = True
                if upd_s:
                    fresh = abs((cur - parse_iso(upd_s)).total_seconds()) <= 4 * 3600
                if fresh:
                    alias = str(obj.get("place_alias") or obj.get("alias") or obj.get("place") or "").strip()
                    if alias:
                        return _is_home_alias(alias)
        except Exception:
            pass

    # 位置未知时默认按可能在家处理（不漏防超期告警）
    return True


def _is_hivebox_parcel(hay: str, brand: str = "") -> bool:
    b = (brand or "").strip()
    if b and not _HIVEBOX_RE.search(b) and _NON_HIVEBOX_BRAND_RE.search(b):
        return False
    if _NOT_HIVEBOX_RE.search(hay or ""):
        return False
    return bool(_HIVEBOX_RE.search(hay or ""))


def _extract_free_hours(text: str, ctx: dict[str, Any]) -> float:
    if ctx.get("free_hours") is not None:
        try:
            return max(1.0, float(ctx["free_hours"]))
        except (TypeError, ValueError):
            pass
    m = _FREE_HOURS_RE.search(f"{ctx.get('free_window') or ''} {text or ''}")
    if m:
        return max(1.0, float(m.group(1)))
    return 18.0


def evaluate_hivebox_countdown(
    db: SimingDB,
    now: Optional[datetime] = None,
    is_home: Optional[bool] = None,
    notify: bool = False,
) -> dict[str, Any]:
    """扫描未取件的丰巢柜包裹，计算 18h 免费存放剩余分钟；仅在 `0 < remaining_min <= 120` 且在家/未知时触发单次穿透告警。"""
    db.init_db()
    cur = now or _cur_now()
    module_enabled = is_module_enabled(db, "parcel_sentinel")
    user_is_home = _infer_is_home(db, cur, is_home)

    conn = db.connect()
    try:
        case_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT case_id, title, next_step, context_json, created_at FROM cases WHERE status = '待处理' ORDER BY created_at ASC"
            ).fetchall()
        ]
        closed_case_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT case_id, context_json FROM cases WHERE status != '待处理'"
            ).fetchall()
        ]
        ev_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT event_id, source, occurred_at, summary, payload_json FROM events WHERE occurred_at >= ? ORDER BY occurred_at ASC",
                (iso_now(cur - timedelta(hours=36)),),
            ).fetchall()
        ]
    finally:
        conn.close()

    closed_case_ids: set[str] = set()
    closed_codes: set[str] = set()
    for cc in closed_case_rows:
        closed_case_ids.add(str(cc["case_id"]))
        try:
            cctx = json.loads(cc.get("context_json") or "{}")
            if isinstance(cctx, dict):
                ccode = str(cctx.get("code") or "").strip()
                if ccode:
                    closed_codes.add(ccode)
        except Exception:
            pass

    # 先收集事件中明确已取件/关闭的取件码，防历史入柜事件重放误报
    for ev in ev_rows:
        try:
            pl = json.loads(ev.get("payload_json") or "{}")
            if not isinstance(pl, dict):
                pl = {}
        except Exception:
            pl = {}
        summary_s = str(ev.get("summary") or "")
        if pl.get("picked_up") or pl.get("closed") or _PICKED_UP_RE.search(summary_s):
            pcode = str(pl.get("code") or "").strip()
            if pcode:
                closed_codes.add(pcode)

    candidates: list[dict[str, Any]] = []
    seen_codes: set[str] = set()

    for c in case_rows:
        try:
            ctx = json.loads(c.get("context_json") or "{}")
            if not isinstance(ctx, dict):
                ctx = {}
        except Exception:
            ctx = {}
        if ctx.get("picked_up") or ctx.get("closed"):
            continue
        brand = str(ctx.get("brand") or "")
        hay = f"{c.get('title') or ''} {c.get('next_step') or ''} {brand} {ctx.get('location') or ''} {ctx.get('station') or ''}"
        if not _is_hivebox_parcel(hay, brand=brand):
            continue
        code = str(ctx.get("code") or "").strip()
        if code and code in closed_codes:
            continue
        if code:
            seen_codes.add(code)
        dep_s = str(ctx.get("deposited_at") or ctx.get("arrived_at") or c["created_at"])
        candidates.append(
            {
                "ref_id": str(c["case_id"]),
                "source": "case",
                "title": str(c["title"]),
                "location": str(ctx.get("location") or ctx.get("station") or c["title"]),
                "code": code,
                "site_alias": str(ctx.get("site_alias") or ""),
                "deposited_at": dep_s,
                "free_hours": _extract_free_hours(hay, ctx),
            }
        )

    for ev in ev_rows:
        try:
            pl = json.loads(ev.get("payload_json") or "{}")
            if not isinstance(pl, dict):
                pl = {}
        except Exception:
            pl = {}
        if pl.get("picked_up") or pl.get("closed"):
            continue
        linked_cid = str(pl.get("case_id") or "").strip()
        if linked_cid and linked_cid in closed_case_ids:
            continue
        summary_s = str(ev.get("summary") or "")
        if _PICKED_UP_RE.search(summary_s):
            continue
        brand = str(pl.get("brand") or "")
        hay = f"{summary_s} {pl.get('location') or ''} {pl.get('station') or ''} {brand}"
        if not _is_hivebox_parcel(hay, brand=brand):
            continue
        code = str(pl.get("code") or "").strip()
        if code and (code in seen_codes or code in closed_codes):
            continue
        if code:
            seen_codes.add(code)
        candidates.append(
            {
                "ref_id": str(ev["event_id"]),
                "source": "event",
                "title": summary_s,
                "location": str(pl.get("location") or pl.get("station") or summary_s),
                "code": code,
                "site_alias": str(pl.get("site_alias") or ""),
                "deposited_at": str(pl.get("deposited_at") or ev["occurred_at"]),
                "free_hours": _extract_free_hours(hay, pl),
            }
        )

    tracked: list[dict[str, Any]] = []
    urgent: list[dict[str, Any]] = []
    overdue: list[dict[str, Any]] = []
    notified: list[str] = []

    for item in candidates:
        try:
            dep_dt = parse_iso(item["deposited_at"])
        except Exception:
            continue
        deadline = dep_dt + timedelta(hours=float(item["free_hours"]))
        remaining_min = round((deadline - cur).total_seconds() / 60.0, 1)
        site_alias = str(item.get("site_alias") or "").strip()
        # 非当前住址（如新房示例小区B）的丰巢柜不按「在家顺手下楼取」穿透告警
        at_matching_home = user_is_home and (not site_alias or _is_home_alias(site_alias))
        rec = {
            **item,
            "deadline_at": iso_now(deadline),
            "remaining_min": remaining_min,
            "is_urgent": 0.0 < remaining_min <= 120.0,
            "is_overdue": remaining_min <= 0.0,
        }
        tracked.append(rec)

        if remaining_min <= 0.0:
            overdue.append(rec)
            continue

        if 0.0 < remaining_min <= 120.0 and at_matching_home:
            urgent.append(rec)
            dedup_key = f"hivebox_urgent_notified:{item['ref_id']}"
            already = bool(db.get_meta(dedup_key))
            rec["already_notified"] = already
            if notify and not already and module_enabled:
                from siming.push import push_message

                code_s = f" · 取件码 `{item['code']}`" if item["code"] else ""
                text = (
                    f"📦 **丰巢防超期紧急提醒** [`{item['ref_id']}`]\n"
                    f"• 柜机：**{item['location']}**{code_s}\n"
                    f"• ⏳ 距离 {int(item['free_hours'])}h 免费截止仅剩 **{int(remaining_min)} 分钟**（截止 `{iso_now(deadline)[11:16]}`），快下楼顺手取走免交畅存费！"
                )
                res = push_message(
                    db,
                    text=text,
                    rule_id="hivebox_overdue_guard",
                    ref_id=f"hivebox:{item['ref_id']}",
                    level="now",
                    hard=True,
                    dedup_window=timedelta(hours=24),
                    now=cur,
                )
                if res.get("ok"):
                    db.set_meta(dedup_key, iso_now(cur))
                    notified.append(item["ref_id"])

    out: dict[str, Any] = {
        "ok": True,
        "is_home": user_is_home,
        "tracked": tracked,
        "urgent": urgent,
        "overdue": overdue,
        "notified": notified,
    }
    if notify and not module_enabled:
        out["skipped"] = "module_disabled"
    return out


def evaluate_return_window_sentinel(
    db: SimingDB,
    now: Optional[datetime] = None,
    notify: bool = False,
) -> dict[str, Any]:
    """扫描抖音/拼多多/京东已签收包裹，标记 7 天无理由退货与运费险剩余 `<= 48h` 的商品。"""
    db.init_db()
    cur = now or _cur_now()
    module_enabled = is_module_enabled(db, "parcel_sentinel")

    conn = db.connect()
    try:
        case_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT case_id, title, next_step, status, category, context_json, created_at, closed_at FROM cases ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
        ]
        order_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT item_key, platform, shop, ordered_at, order_status, order_amount_cents, item_name FROM shop_order_items ORDER BY ordered_at DESC LIMIT 200"
            ).fetchall()
        ]
        ev_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT event_id, source, occurred_at, summary, payload_json FROM events WHERE occurred_at >= ? ORDER BY occurred_at DESC",
                (iso_now(cur - timedelta(days=10)),),
            ).fetchall()
        ]
    finally:
        conn.close()

    delivered_items: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    # 1) cases
    for c in case_rows:
        try:
            ctx = json.loads(c.get("context_json") or "{}")
            if not isinstance(ctx, dict):
                ctx = {}
        except Exception:
            ctx = {}
        if ctx.get("return_checked") or ctx.get("kept"):
            continue
        platform = str(ctx.get("platform") or "")
        hay = f"{platform} {c.get('title') or ''} {c.get('next_step') or ''}"
        if not _ECOM_PLATFORM_RE.search(hay):
            continue
        delivered_at = str(ctx.get("delivered_at") or "").strip()
        if not delivered_at and (c.get("status") == "完成" or _DELIVERED_RE.search(hay)):
            delivered_at = str(c.get("closed_at") or c.get("created_at") or "")
        if not delivered_at:
            continue
        iid = str(c["case_id"])
        seen_ids.add(iid)
        delivered_items.append(
            {
                "item_id": iid,
                "source": "case",
                "platform": platform or _ECOM_PLATFORM_RE.search(hay).group(1),
                "title": str(c["title"]),
                "delivered_at": delivered_at,
                "has_shipping_insurance": bool(ctx.get("shipping_insurance", True)),
                "return_days": int(ctx.get("return_days") or 7),
            }
        )

    # 2) shop_order_items
    for o in order_rows:
        plat = str(o.get("platform") or "")
        st = str(o.get("order_status") or "")
        if not _ECOM_PLATFORM_RE.search(plat):
            continue
        if not _DELIVERED_RE.search(st):
            continue
        iid = str(o["item_key"])
        if iid in seen_ids:
            continue
        seen_ids.add(iid)
        delivered_items.append(
            {
                "item_id": iid,
                "source": "shop_order_items",
                "platform": plat,
                "title": f"{o['shop']} · {o['item_name']}" if o["shop"] else str(o["item_name"]),
                "delivered_at": str(o["ordered_at"]),
                "has_shipping_insurance": True,
                "return_days": 7,
            }
        )

    # 3) events
    for ev in ev_rows:
        try:
            pl = json.loads(ev.get("payload_json") or "{}")
            if not isinstance(pl, dict):
                pl = {}
        except Exception:
            pl = {}
        hay = f"{pl.get('platform') or ''} {ev.get('summary') or ''}"
        if not _ECOM_PLATFORM_RE.search(hay):
            continue
        delivered_at = str(pl.get("delivered_at") or "").strip()
        if not delivered_at and _DELIVERED_RE.search(hay):
            delivered_at = str(ev["occurred_at"])
        if not delivered_at:
            continue
        iid = str(pl.get("order_id") or ev["event_id"])
        if iid in seen_ids:
            continue
        seen_ids.add(iid)
        delivered_items.append(
            {
                "item_id": iid,
                "source": "event",
                "platform": str(pl.get("platform") or _ECOM_PLATFORM_RE.search(hay).group(1)),
                "title": str(pl.get("item_name") or ev["summary"]),
                "delivered_at": delivered_at,
                "has_shipping_insurance": bool(pl.get("shipping_insurance", True)),
                "return_days": int(pl.get("return_days") or 7),
            }
        )

    tracked: list[dict[str, Any]] = []
    expiring_soon: list[dict[str, Any]] = []
    notified: list[str] = []

    for item in delivered_items:
        try:
            deliv_dt = parse_iso(item["delivered_at"])
        except Exception:
            continue
        deadline = deliv_dt + timedelta(days=int(item["return_days"]))
        remaining_hours = round((deadline - cur).total_seconds() / 3600.0, 1)
        if remaining_hours <= 0.0:
            continue
        rec = {
            **item,
            "return_deadline_at": iso_now(deadline),
            "remaining_hours": remaining_hours,
            "expiring_soon": remaining_hours <= 48.0,
        }
        tracked.append(rec)

        if remaining_hours <= 48.0:
            expiring_soon.append(rec)
            dedup_key = f"return_window_notified:{item['item_id']}"
            already = bool(db.get_meta(dedup_key))
            rec["already_notified"] = already
            if notify and not already and module_enabled:
                from siming.push import push_message

                ins_note = "（含运费险）" if item["has_shipping_insurance"] else ""
                text = (
                    f"🛡️ **7天无理由退货哨兵** [`{item['item_id']}`]\n"
                    f"• [{item['platform']}] **{item['title']}**{ins_note}\n"
                    f"• ⏳ 退货与运费险窗口仅剩 **{remaining_hours:.1f} 小时**（截止 `{iso_now(deadline)[:16]}`），不满意请尽快发起退货！"
                )
                res = push_message(
                    db,
                    text=text,
                    rule_id="return_window_sentinel",
                    ref_id=f"return_win:{item['item_id']}",
                    level="normal",
                    dedup_window=timedelta(hours=48),
                    now=cur,
                )
                if res.get("ok"):
                    db.set_meta(dedup_key, iso_now(cur))
                    notified.append(item["item_id"])

    out: dict[str, Any] = {
        "ok": True,
        "tracked": tracked,
        "expiring_soon": expiring_soon,
        "notified": notified,
    }
    if notify and not module_enabled:
        out["skipped"] = "module_disabled"
    return out


def run_parcel_sentinel(
    db: SimingDB,
    now: Optional[datetime] = None,
    notify: bool = False,
) -> dict[str, Any]:
    """统一运行丰巢 18h 超期守卫与 7 天无理由退货哨兵。"""
    cur = now or _cur_now()
    module_enabled = is_module_enabled(db, "parcel_sentinel")
    if notify and not module_enabled:
        return {
            "ok": True,
            "skipped": "module_disabled",
            "checked_at": iso_now(cur),
            "hivebox": {"ok": True, "skipped": "module_disabled", "tracked": [], "urgent": [], "overdue": [], "notified": []},
            "return_window": {"ok": True, "skipped": "module_disabled", "tracked": [], "expiring_soon": [], "notified": []},
            "urgent_count": 0,
        }
    hivebox = evaluate_hivebox_countdown(db, now=cur, notify=notify)
    returns = evaluate_return_window_sentinel(db, now=cur, notify=notify)
    return {
        "ok": True,
        "checked_at": iso_now(cur),
        "hivebox": hivebox,
        "return_window": returns,
        "urgent_count": len(hivebox["urgent"]) + len(returns["expiring_soon"]),
    }


def format_sentinel_report(db: SimingDB, now: Optional[datetime] = None) -> str:
    """格式化丰巢倒计时与 7 天退货窗口哨兵总览。"""
    res = run_parcel_sentinel(db, now=now, notify=False)
    hb = res["hivebox"]
    rw = res["return_window"]

    lines = ["📦 **包裹防超期与退货哨兵**"]
    if hb["tracked"]:
        lines.append(f"• 丰巢柜在途：**{len(hb['tracked'])}** 件（紧急 ≤2h：{len(hb['urgent'])} 件 · 已超期：{len(hb['overdue'])} 件）")
        for item in hb["tracked"][:5]:
            badge = "🚨" if item["is_urgent"] else ("❌" if item["is_overdue"] else "🟢")
            lines.append(f"  - {badge} {item['location']} · 剩余 `{item['remaining_min']:.0f} 分钟`")
    else:
        lines.append("• 丰巢柜在途：0 件")

    if rw["tracked"]:
        lines.append(f"• 7天退货期监控：**{len(rw['tracked'])}** 件（48h内到期：{len(rw['expiring_soon'])} 件）")
        for item in rw["expiring_soon"][:5]:
            lines.append(f"  - 🛡️ [{item['platform']}] {item['title']} · 剩 `{item['remaining_hours']:.1f}h`")
    else:
        lines.append("• 7天退货期监控：0 件")
    return "\n".join(lines)
