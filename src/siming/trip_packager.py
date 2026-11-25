"""W4-3 · 差旅与出境游全生命周期行程包（国内高铁/航旅 + 出境东京/海外 Ctrip/Grab/酒店 + CW01 报销草稿 + Sure 标签）。"""

from __future__ import annotations

import csv
import json
import os
import re
import secrets
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional

from siming import db as _db
from siming.db import SimingDB, iso_now, logical_today
from siming.modules import is_module_enabled

# 常见出境城市 -> 国家代码与货币
_OVERSEAS_CITY_MAP: dict[str, tuple[str, str]] = {
    "东京": ("JP", "JPY"),
    "tokyo": ("JP", "JPY"),
    "大阪": ("JP", "JPY"),
    "osaka": ("JP", "JPY"),
    "京都": ("JP", "JPY"),
    "北海道": ("JP", "JPY"),
    "冲绳": ("JP", "JPY"),
    "日本": ("JP", "JPY"),
    "新加坡": ("SG", "SGD"),
    "singapore": ("SG", "SGD"),
    "首尔": ("KR", "KRW"),
    "seoul": ("KR", "KRW"),
    "曼谷": ("TH", "THB"),
    "bangkok": ("TH", "THB"),
    "香港": ("HK", "HKD"),
    "澳门": ("MO", "MOP"),
    "台北": ("TW", "TWD"),
    "伦敦": ("GB", "GBP"),
    "london": ("GB", "GBP"),
    "巴黎": ("FR", "EUR"),
    "paris": ("FR", "EUR"),
    "纽约": ("US", "USD"),
    "旧金山": ("US", "USD"),
    "洛杉矶": ("US", "USD"),
    "悉尼": ("AU", "AUD"),
}

_DOMESTIC_CITIES: tuple[str, ...] = (
    "上海", "北京", "深圳", "广州", "杭州", "成都", "重庆", "南京", "武汉", "西安",
    "苏州", "厦门", "三亚", "青岛", "长沙", "合肥", "天津", "郑州", "大连", "珠海",
)

# 参考汇率（1 外币 -> CNY，用于行程预算、刷卡对比与报销估算）
_FX_TO_CNY: dict[str, float] = {
    "CNY": 1.0,
    "RMB": 1.0,
    "USD": 7.25,
    "JPY": 0.048,
    "EUR": 7.85,
    "GBP": 9.15,
    "SGD": 5.40,
    "HKD": 0.93,
    "KRW": 0.0052,
    "THB": 0.21,
    "AUD": 4.75,
    "TWD": 0.225,
    "MOP": 0.90,
}

_TRIP_EVENT_RE = re.compile(
    r"(机票|航班|携程|ctrip|高铁|12306|动车|登机|出票|行程单|酒店预订|酒店确认|hotel|flight|ana|jal|grab|国航|东航|南航)",
    re.IGNORECASE,
)
_ISO_DATE_RE = re.compile(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?")
_TRAVEL_MERCHANT_RE = re.compile(
    r"(机票|航班|航空|国航|东航|南航|ana|jal|携程|ctrip|飞猪|去哪儿|12306|铁路|高铁|火车|"
    r"滴滴|didi|grab|uber|出租|网约车|地铁|suica|西瓜卡|机场|快轨|"
    r"酒店|宾馆|住宿|hotel|东横|toyoko|希尔顿|hilton|万豪|marriott|凯悦|hyatt|亚朵|全季|格拉斯丽)",
    re.IGNORECASE,
)


def _infer_trip_kind_and_country(destination: str, country: str) -> tuple[str, str]:
    c_norm = (country or "CN").strip().upper()
    dest_low = (destination or "").strip().lower()
    if c_norm in ("CN", "CHN", "CHINA", "中国"):
        for city, (cc, _) in _OVERSEAS_CITY_MAP.items():
            if city in dest_low:
                return ("international", cc)
        return ("domestic", "CN")
    return ("international", c_norm)


def _compute_trip_status(start_date: str, end_date: str) -> str:
    today = logical_today()
    try:
        sd = date.fromisoformat(start_date[:10])
        ed = date.fromisoformat(end_date[:10])
    except ValueError:
        return "PLANNED"
    if today < sd:
        return "PLANNED"
    if sd <= today <= ed:
        return "ACTIVE"
    return "RETURNED"


def _build_checklist(destination: str, country: str, kind: str) -> list[dict[str, Any]]:
    dest_low = destination.lower()
    if kind == "international":
        local_transit = "Suica/西瓜卡(Apple Pay) & 机场快线" if country == "JP" or "东京" in dest_low or "大阪" in dest_low else "Grab / 当地打车与离线地图"
        entry_doc = "签证 & Visit Japan Web 入境二维码" if country == "JP" else "目的地签证 / 电子入境卡"
        return [
            {"id": "passport", "title": "核验护照有效期（剩余≥6个月）及空白页", "phase": "T-14d", "done": False},
            {"id": "visa", "title": f"确认 {entry_doc}", "phase": "T-14d", "done": False},
            {"id": "esim", "title": f"开通 {destination} 境外 eSIM / 运营商漫游流量包", "phase": "T-3d", "done": False},
            {"id": "weather", "title": f"查 {destination} 逐日天气预报并备齐衣物/转换插头", "phase": "T-2d", "done": False},
            {"id": "checkin", "title": "起飞 T-24h 航司线上值机选座 & 核对航站楼", "phase": "T-24h", "done": False},
            {"id": "transit", "title": f"准备目的地出行与支付：{local_transit}", "phase": "T-24h", "done": False},
            {"id": "fx_card", "title": "确认多币种信用卡境外额度并牢记拒绝 DCC（刷当地币种）", "phase": "T-24h", "done": False},
        ]
    return [
        {"id": "id_card", "title": "核验居民身份证与电子乘车码/航旅纵横", "phase": "T-24h", "done": False},
        {"id": "checkin", "title": "起飞 T-24h 值机选座 / 核对 12306 车次与检票口", "phase": "T-24h", "done": False},
        {"id": "weather", "title": f"查 {destination} 天气预报与穿衣/雨具准备", "phase": "T-24h", "done": False},
        {"id": "cw01_header", "title": "确认差旅数电发票抬头（返程一键组 CW01 报销包）", "phase": "IN_TRIP", "done": False},
    ]


def create_trip(
    db: SimingDB,
    destination: str,
    start_date: str,
    end_date: str,
    country: str = "CN",
    bookings: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """创建国内或出境差旅/游玩行程包，自动生成动态行前清单与 Sure 标签。"""
    db.init_db()
    dest = (destination or "").strip()
    if not dest:
        raise ValueError("destination 不能为空")
    sd = start_date.strip()[:10]
    ed = (end_date or start_date).strip()[:10]
    if sd and ed and ed < sd:
        sd, ed = ed, sd
    kind, resolved_country = _infer_trip_kind_and_country(dest, country)
    status = _compute_trip_status(sd, ed)
    checklist = _build_checklist(dest, resolved_country, kind)
    booking_list = list(bookings or [])
    ym = sd[:7] if len(sd) >= 7 else _db.now_local().strftime("%Y-%m")
    sure_tag = f"trip:{dest}:{ym}"
    ts = iso_now()

    with db.transaction() as conn:
        trip_id = f"TR-{_db.now_local().strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"
        conn.execute(
            """
            INSERT INTO trips (
                trip_id, destination, country, start_date, end_date, kind, status,
                checklist_json, bookings_json, expenses_json, sure_tag, cw01_draft_path,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, '[]', ?, '', ?, ?)
            """,
            (
                trip_id,
                dest,
                resolved_country,
                sd,
                ed,
                kind,
                status,
                json.dumps(checklist, ensure_ascii=False),
                json.dumps(booking_list, ensure_ascii=False),
                sure_tag,
                ts,
                ts,
            ),
        )
        db.write_audit(
            tier="A1",
            action="trip_create",
            params={"trip_id": trip_id, "destination": dest, "start_date": sd, "end_date": ed, "kind": kind},
            target_id=trip_id,
            conn=conn,
        )

    return {
        "ok": True,
        "trip_id": trip_id,
        "destination": dest,
        "country": resolved_country,
        "start_date": sd,
        "end_date": ed,
        "kind": kind,
        "status": status,
        "checklist": checklist,
        "bookings": booking_list,
        "expenses": [],
        "sure_tag": sure_tag,
        "cw01_draft_path": "",
        "created_at": ts,
    }


def _extract_dates_from_text(text: str, fallback_iso: str) -> tuple[str, str]:
    found: list[str] = []
    for m in _ISO_DATE_RE.finditer(text or ""):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            iso = f"{y:04d}-{mo:02d}-{d:02d}"
            if iso not in found:
                found.append(iso)
    if len(found) >= 2:
        found.sort()
        return (found[0], found[-1])
    if len(found) == 1:
        try:
            ed = (date.fromisoformat(found[0]) + timedelta(days=2)).isoformat()
        except ValueError:
            ed = found[0]
        return (found[0], ed)
    fb = (fallback_iso or _db.now_local().strftime("%Y-%m-%d"))[:10]
    return (fb, fb)


def _extract_destination_from_text(text: str, payload: dict[str, Any]) -> tuple[str, str]:
    if payload.get("destination"):
        dest = str(payload["destination"]).strip()
        country = str(payload.get("country") or "CN").strip()
        _, cc = _infer_trip_kind_and_country(dest, country)
        return (dest, cc)
    low = (text or "").lower()
    for city, (cc, _) in _OVERSEAS_CITY_MAP.items():
        if city in low:
            # 优先返回中文名
            return (city if any("\u4e00" <= ch <= "\u9fff" for ch in city) else city.title(), cc)
    for city in _DOMESTIC_CITIES:
        if city in text:
            return (city, "CN")
    return ("", "CN")


def detect_trips_from_events(db: SimingDB) -> list[dict[str, Any]]:
    """从邮件/短信/航旅事件自动识别差旅与出境行程，幂等创建或合并 bookings 到 trips 表。"""
    db.init_db()
    if not is_module_enabled(db, "trip_packager"):
        return []

    conn = db.connect()
    try:
        ev_rows = conn.execute(
            """
            SELECT * FROM (
                SELECT * FROM events ORDER BY occurred_at DESC LIMIT 500
            ) ORDER BY occurred_at ASC
            """
        ).fetchall()
        existing_rows = conn.execute("SELECT * FROM trips").fetchall()
    finally:
        conn.close()

    existing_trips = [dict(r) for r in existing_rows]
    detected: list[dict[str, Any]] = []

    for ev in ev_rows:
        summary = str(ev["summary"] or "")
        try:
            payload = json.loads(ev["payload_json"] or "{}")
        except ValueError:
            payload = {}
        blob = f"{summary} {json.dumps(payload, ensure_ascii=False)}"
        if not _TRIP_EVENT_RE.search(blob):
            continue

        dest, country = _extract_destination_from_text(blob, payload)
        if not dest:
            continue
        sd = str(payload.get("start_date") or "").strip()[:10]
        ed = str(payload.get("end_date") or "").strip()[:10]
        if not sd:
            sd, ed = _extract_dates_from_text(blob, str(ev["occurred_at"] or ""))
        if not ed:
            ed = sd

        booking_entry = {
            "event_id": str(ev["event_id"]),
            "source": str(ev["source"]),
            "summary": summary[:160],
            "provider": str(payload.get("provider") or payload.get("merchant") or ""),
            "order_no": str(payload.get("order_no") or payload.get("pnr") or ""),
            "occurred_at": str(ev["occurred_at"] or ""),
        }

        # 检查是否与已有行程匹配（同目的地且日期重叠或相邻3天内）
        matched_trip: Optional[dict[str, Any]] = None
        for tr in existing_trips:
            if str(tr["destination"]).lower() != dest.lower():
                continue
            try:
                t_s = date.fromisoformat(str(tr["start_date"])[:10])
                t_e = date.fromisoformat(str(tr["end_date"])[:10])
                e_s = date.fromisoformat(sd)
                e_e = date.fromisoformat(ed)
                if e_s <= (t_e + timedelta(days=3)) and e_e >= (t_s - timedelta(days=3)):
                    matched_trip = tr
                    break
            except ValueError:
                if tr["start_date"] == sd:
                    matched_trip = tr
                    break

        if matched_trip is not None:
            cur_bookings = json.loads(matched_trip["bookings_json"] or "[]")
            seen_ids = {b.get("event_id") for b in cur_bookings if isinstance(b, dict)}
            if booking_entry["event_id"] not in seen_ids:
                cur_bookings.append(booking_entry)
                new_sd = min(str(matched_trip["start_date"]), sd)
                new_ed = max(str(matched_trip["end_date"]), ed)
                ts = iso_now()
                with db.transaction() as c:
                    c.execute(
                        "UPDATE trips SET start_date=?, end_date=?, bookings_json=?, updated_at=? WHERE trip_id=?",
                        (new_sd, new_ed, json.dumps(cur_bookings, ensure_ascii=False), ts, matched_trip["trip_id"]),
                    )
                matched_trip["start_date"] = new_sd
                matched_trip["end_date"] = new_ed
                matched_trip["bookings_json"] = json.dumps(cur_bookings, ensure_ascii=False)
                detected.append(
                    {
                        "trip_id": matched_trip["trip_id"],
                        "destination": matched_trip["destination"],
                        "country": matched_trip["country"],
                        "start_date": new_sd,
                        "end_date": new_ed,
                        "kind": matched_trip["kind"],
                        "sure_tag": matched_trip["sure_tag"],
                        "bookings": cur_bookings,
                        "action": "merged",
                    }
                )
        else:
            created = create_trip(
                db,
                destination=dest,
                start_date=sd,
                end_date=ed,
                country=country,
                bookings=[booking_entry],
            )
            created["action"] = "created"
            existing_trips.append(
                {
                    "trip_id": created["trip_id"],
                    "destination": created["destination"],
                    "country": created["country"],
                    "start_date": created["start_date"],
                    "end_date": created["end_date"],
                    "kind": created["kind"],
                    "sure_tag": created["sure_tag"],
                    "bookings_json": json.dumps(created["bookings"], ensure_ascii=False),
                }
            )
            detected.append(created)

    return detected


def recommend_travel_card(
    currency: str = "JPY",
    amount_foreign: float = 10000.0,
    category: str = "dining",
) -> dict[str, Any]:
    """境内外刷卡返现与外汇省钱建议（对比中行 1003 / 建行 1004 / 光大 1001 多币种卡与普通人民币卡）。"""
    cur = (currency or "JPY").strip().upper()
    amt = max(0.0, float(amount_foreign or 0.0))
    cat = (category or "dining").strip().lower()
    rate = _FX_TO_CNY.get(cur, 1.0)
    est_cny = round(amt * rate, 2)

    if cur in ("CNY", "RMB"):
        if cat in ("flight", "hotel", "travel", "rail"):
            best = {
                "card": "建行龙卡 1004（商旅/延误险优先）",
                "fx_fee_pct": 0.0,
                "cashback_pct": 1.0,
                "est_cashback_cny": round(est_cny * 0.01, 2),
                "reason": "国内差旅航延险覆盖 + 积分回血，便于 CW01 统一对账",
            }
            alt = "中行 1003（日常消费与报销备用）"
        else:
            best = {
                "card": "光大 1001 / 中行 1003（国内餐饮日常返现）",
                "fx_fee_pct": 0.0,
                "cashback_pct": 0.8,
                "est_cashback_cny": round(est_cny * 0.008, 2),
                "reason": "境内人民币零汇兑费，餐饮商户积分与立减覆盖广",
            }
            alt = "建行 1004"
        return {
            "ok": True,
            "currency": "CNY",
            "amount_foreign": amt,
            "category": cat,
            "fx_rate_to_cny": 1.0,
            "estimated_cny": est_cny,
            "recommended_card": best["card"],
            "alternative_card": alt,
            "fx_fee_pct": 0.0,
            "cashback_pct": best["cashback_pct"],
            "estimated_cashback_cny": best["est_cashback_cny"],
            "estimated_savings_vs_cny_card": best["est_cashback_cny"],
            "dcc_warning": "",
            "advice": f"国内 {cat} 消费 ¥{est_cny:.2f}：推荐刷 {best['card']}（{best['reason']}）。",
        }

    # 境外多币种卡规则对比
    candidates = [
        {
            "card": "中行全币种卡 1003",
            "fx_fee_pct": 0.0,
            "cashback_pct": 2.0 if cur == "JPY" or cat in ("dining", "shopping") else 1.5,
            "reason": "0 货币转换费 + 境外线下/日本消费高返现，支持自动人民币购汇还款",
        },
        {
            "card": "建行全球支付卡 1004",
            "fx_fee_pct": 0.0,
            "cashback_pct": 1.8 if cat in ("hotel", "flight", "travel") else 1.2,
            "reason": "全币种 0 货转费 + 境外航旅酒店返现与延误保障",
        },
        {
            "card": "光大多币种卡 1001",
            "fx_fee_pct": 0.0,
            "cashback_pct": 1.0,
            "reason": "免外币货转费，适合作为境外备卡与 Grab/Suica 绑卡",
        },
    ]
    candidates.sort(key=lambda c: c["cashback_pct"] - c["fx_fee_pct"], reverse=True)
    best = candidates[0]
    alt = candidates[1]["card"]

    # 对比普通单币种人民币卡（通常 1.5% 货币转换费 + 0% 返现）
    cny_card_fx_cost = round(est_cny * 0.015, 2)
    est_cashback = round(est_cny * (best["cashback_pct"] / 100.0), 2)
    net_savings = round(cny_card_fx_cost + est_cashback, 2)
    dcc_warning = f"⚠️ 切勿接受 POS 机 DCC 动态货币转换（刷人民币会多亏 3%~5%），务必选择以 {cur} 当地币种结算！"

    return {
        "ok": True,
        "currency": cur,
        "amount_foreign": amt,
        "category": cat,
        "fx_rate_to_cny": rate,
        "estimated_cny": est_cny,
        "recommended_card": best["card"],
        "alternative_card": alt,
        "fx_fee_pct": best["fx_fee_pct"],
        "cashback_pct": best["cashback_pct"],
        "estimated_cashback_cny": est_cashback,
        "estimated_savings_vs_cny_card": net_savings,
        "dcc_warning": dcc_warning,
        "advice": (
            f"境外 {cur} {amt:,.2f}（约 ¥{est_cny:.2f}，类目 {cat}）：首选 {best['card']} "
            f"（0 货转费 + 预估返现 {best['cashback_pct']:.1f}% ≈ ¥{est_cashback:.2f}，比普通人民币卡共省约 ¥{net_savings:.2f}）。{dcc_warning}"
        ),
    }


def _classify_cw01_expense(merchant: str, kind: str, notes: str) -> str:
    blob = f"{merchant} {kind} {notes}".lower()
    if any(k in blob for k in ("机票", "航班", "航空", "ana", "jal", "国航", "东航", "南航", "flight")):
        return "差旅交通费(机票)"
    if any(k in blob for k in ("12306", "高铁", "火车", "铁路", "动车")):
        return "差旅交通费(高铁)"
    if any(k in blob for k in ("酒店", "宾馆", "住宿", "hotel", "东横", "toyoko", "希尔顿", "万豪", "亚朵", "全季", "格拉斯丽")):
        return "差旅住宿费(酒店)"
    if any(k in blob for k in ("滴滴", "didi", "grab", "uber", "出租", "打车", "suica", "西瓜卡", "地铁", "快轨")):
        return "市内/接驳交通费"
    if any(k in blob for k in ("携程", "ctrip", "飞猪", "去哪儿")):
        return "差旅预订(机酒)"
    return "差旅其他费用"


def pack_trip_cw01_draft(
    db: SimingDB,
    trip_id: str,
    out_dir: Optional[str | Path] = None,
) -> dict[str, Any]:
    """返程收集 [start_date, end_date] 内的高铁/机票/打车/酒店账本流水，打成 CW01 报销草稿包并绑定 Sure 标签。"""
    db.init_db()
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM trips WHERE trip_id = ?", (trip_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"未找到行程 {trip_id}"}
        trip = dict(row)
        start_d = str(trip["start_date"])[:10]
        end_d = str(trip["end_date"])[:10]
        txns = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM ledger_txns
                WHERE substr(occurred_at, 1, 10) >= ? AND substr(occurred_at, 1, 10) <= ?
                  AND COALESCE(UPPER(status), '') NOT IN ('VOID_DUPLICATE', 'VOID', 'CANCELLED')
                  AND amount_cents > 0
                ORDER BY occurred_at ASC
                """,
                (start_d, end_d),
            ).fetchall()
        ]
    finally:
        conn.close()

    trip_kind = str(trip.get("kind") or "domestic")
    trip_country = str(trip.get("country") or "CN").upper()
    trip_local_currencies: set[str] = set()
    for _city, (cc, ccy) in _OVERSEAS_CITY_MAP.items():
        if cc == trip_country:
            trip_local_currencies.add(ccy)
    _non_travel_saas_re = re.compile(
        r"(openai|anthropic|claude|deepseek|nous|redpill|cursor|github|aliyun|dashscope|icloud|spotify|netflix)",
        re.IGNORECASE,
    )

    matched: list[dict[str, Any]] = []
    total_cny_cents = 0
    for t in txns:
        kind = str(t.get("kind") or "")
        if kind in ("退款", "还款", "转入", "收入"):
            continue
        cur = str(t.get("currency") or "CNY").upper()
        blob = f"{t.get('merchant', '')} {kind} {t.get('notes', '')}"
        # 国内差旅不混入外币消费（例如当日预付的境外 JPY/USD 酒店或海外算力账单）
        if trip_kind == "domestic" and cur not in ("CNY", "RMB"):
            continue
        is_travel_merchant = bool(_TRAVEL_MERCHANT_RE.search(blob))
        if not is_travel_merchant:
            # 出境行程仅自动收录目的地当地币种的非典型商户（并排除明确 AI/云 SaaS）
            if trip_kind != "international" or cur == "CNY" or _non_travel_saas_re.search(blob):
                continue
            if trip_local_currencies and cur not in trip_local_currencies:
                continue
        orig_amt = round(float(t.get("amount_cents") or 0) / 100.0, 2)
        fx = _FX_TO_CNY.get(cur, 1.0)
        cny_amt = round(orig_amt * fx, 2)
        cny_cents = int((Decimal(str(cny_amt)) * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP))
        total_cny_cents += cny_cents
        matched.append(
            {
                "txn_id": str(t["txn_id"]),
                "occurred_at": str(t["occurred_at"]),
                "merchant": str(t.get("merchant") or ""),
                "category": _classify_cw01_expense(str(t.get("merchant") or ""), kind, str(t.get("notes") or "")),
                "currency": cur,
                "amount_orig": orig_amt,
                "fx_rate": fx,
                "amount_cny": cny_amt,
                "account_alias": str(t.get("account_alias") or ""),
                "card_last4": str(t.get("card_last4") or ""),
                "notes": str(t.get("notes") or ""),
            }
        )

    dest = str(trip["destination"])
    sure_tag = str(trip.get("sure_tag") or f"trip:{dest}:{start_d[:7]}")
    target_dir = Path(out_dir) if out_dir else (db.paths.reimburse_dir / "trips" / trip_id)
    if str(target_dir) == "/tmp" or str(target_dir).startswith("/tmp/"):
        raise RuntimeError("禁止将 CW01 差旅草稿写入 /tmp")
    target_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(target_dir, 0o700)

    csv_path = target_dir / f"CW01-{trip_id}-明细汇总.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        fieldnames = ["序号", "发生时间", "费用类型", "商户", "原币种", "原币金额", "折算人民币", "支付卡", "流水号", "Sure标签"]
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for idx, item in enumerate(matched, 1):
            card = f"{item['account_alias']}({item['card_last4']})" if item["card_last4"] else item["account_alias"]
            writer.writerow(
                {
                    "序号": idx,
                    "发生时间": item["occurred_at"],
                    "费用类型": item["category"],
                    "商户": item["merchant"],
                    "原币种": item["currency"],
                    "原币金额": f"{item['amount_orig']:.2f}",
                    "折算人民币": f"{item['amount_cny']:.2f}",
                    "支付卡": card,
                    "流水号": item["txn_id"],
                    "Sure标签": sure_tag,
                }
            )

    total_cny_yuan = round(total_cny_cents / 100.0, 2)
    ts = iso_now()
    manifest = {
        "form_kind": "CW01",
        "trip_id": trip_id,
        "destination": dest,
        "country": trip["country"],
        "start_date": start_d,
        "end_date": end_d,
        "sure_tag": sure_tag,
        "expense_count": len(matched),
        "total_cny_cents": total_cny_cents,
        "total_cny_yuan": total_cny_yuan,
        "csv_path": str(csv_path),
        "generated_at": ts,
        "expenses": matched,
    }
    manifest_path = target_dir / f"CW01-{trip_id}-manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(manifest_path, 0o600)

    with db.transaction() as c:
        c.execute(
            """
            UPDATE trips
            SET expenses_json = ?, sure_tag = ?, cw01_draft_path = ?, updated_at = ?
            WHERE trip_id = ?
            """,
            (json.dumps(matched, ensure_ascii=False), sure_tag, str(manifest_path), ts, trip_id),
        )
        c.execute(
            "DELETE FROM sure_sync_map WHERE local_kind = 'trip_tag' AND local_id LIKE ?",
            (f"{trip_id}:%",),
        )
        for item in matched:
            c.execute(
                """
                INSERT INTO sure_sync_map (local_kind, local_id, sure_id, sure_account, payload_hash, last_synced_at, status)
                VALUES ('trip_tag', ?, ?, ?, ?, ?, 'QUEUED')
                ON CONFLICT(local_kind, local_id) DO UPDATE SET
                    sure_id = excluded.sure_id,
                    sure_account = excluded.sure_account,
                    last_synced_at = excluded.last_synced_at,
                    status = 'QUEUED'
                """,
                (
                    f"{trip_id}:{item['txn_id']}",
                    sure_tag,
                    item["account_alias"],
                    f"{item['txn_id']}:{sure_tag}",
                    ts,
                ),
            )
        db.write_audit(
            tier="A1",
            action="trip_pack_cw01",
            params={"trip_id": trip_id, "count": len(matched), "total_cny_yuan": total_cny_yuan, "sure_tag": sure_tag},
            target_id=trip_id,
            conn=c,
        )

    return {
        "ok": True,
        "trip_id": trip_id,
        "destination": dest,
        "sure_tag": sure_tag,
        "expense_count": len(matched),
        "total_cny_yuan": total_cny_yuan,
        "manifest_path": str(manifest_path),
        "csv_path": str(csv_path),
        "expenses": matched,
    }


def format_trip_summary(db: SimingDB, trip_id: Optional[str] = None) -> str:
    """格式化单个行程包或全部行程概览。"""
    db.init_db()
    conn = db.connect()
    try:
        if trip_id:
            rows = conn.execute("SELECT * FROM trips WHERE trip_id = ?", (trip_id,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM trips ORDER BY start_date DESC LIMIT 10").fetchall()
    finally:
        conn.close()

    if not rows:
        return "✈️ 暂无已登记的差旅/出境行程包。"

    lines = ["✈️ 司命差旅与出境游行程包"]
    for r in rows:
        chk = json.loads(r["checklist_json"] or "[]")
        done_cnt = sum(1 for item in chk if isinstance(item, dict) and item.get("done"))
        bookings = json.loads(r["bookings_json"] or "[]")
        expenses = json.loads(r["expenses_json"] or "[]")
        kind_lbl = "出境游/差旅" if r["kind"] == "international" else "国内差旅"
        lines.append(
            f"• [{r['status']}] {r['destination']} ({r['country']} · {kind_lbl}) {r['start_date']} ~ {r['end_date']} | ID `{r['trip_id']}`"
        )
        lines.append(
            f"  行前清单 {done_cnt}/{len(chk)} · 预订 {len(bookings)} 项 · 关联费用 {len(expenses)} 笔 · Sure标签 `{r['sure_tag']}`"
        )
        if r["cw01_draft_path"]:
            lines.append(f"  CW01 草稿包：`{r['cw01_draft_path']}`")
        if trip_id and chk:
            for item in chk:
                mark = "✅" if item.get("done") else "⬜"
                lines.append(f"  {mark} [{item.get('phase', '')}] {item.get('title', '')}")
    return "\n".join(lines)
