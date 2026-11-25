"""发票与行程单持久归集（位于 ~/.local/share/siming/reimburse/，防 7 天 tmp 清理误删）、
滴滴「电子发票 ↔ 行程报销单」成对核验、美团餐饮归类，以及滴滴预设地址一键唤端链接生成。"""

from __future__ import annotations

import hashlib
import os
import re
import urllib.parse
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional

from pypdf import PdfReader

from siming.cn_calendar import day_info
from siming.config import load_policy
from siming.db import SimingDB, now_local

INVOICE_AMOUNT_RE = re.compile(r"(?:价税合计|小写|合计金额)[^0-9¥￥]{0,16}[¥￥]?\s*([0-9]+\.[0-9]{2})")
INVOICE_CN_UPPER_AMT_RE = re.compile(r"[零壹贰叁肆伍陆柒捌玖拾佰仟万亿圆元角分整正]{3,}\s*[¥￥]?\s*([0-9]+\.[0-9]{2})")
ITINERARY_TOTAL_RE = re.compile(r"(?:共\s*\d+\s*笔行程[，,\s]*合计\s*([0-9]+\.[0-9]{2})\s*元|合计[：:\s]*([0-9]+\.[0-9]{2})\s*元)")
INVOICE_YUAN_ALL_RE = re.compile(r"[¥￥]\s*([0-9]+\.[0-9]{2})")
INVOICE_NO_RE = re.compile(r"(?:发票号码|电子客票号)[：:\s]*(\d{8,24})")
INVOICE_20D_RE = re.compile(r"(?<!\d)(\d{20})(?!\d)")
ISSUE_DATE_RE = re.compile(r"(?:开票日期|申请日期|行程时间)[：:\s]*(20\d{2})[-年/.](\d{1,2})[-月/.](\d{1,2})")

# 滴滴/高德行程报销单明细行：例如 "1 快车 11-12 21:35 周四 北京市 示例小区A 示例写字楼 8.4 32.50"
DIDI_TRIP_LINE_RE = re.compile(
    r"(?:^|\n)\s*\d{1,3}\s+"
    r"(快车|专车|特惠快车|豪华车|出租车|优享|拼车|轻享|滴滴特快|经济型|舒适型)\s+"
    r"(?:(20\d{2})[-/])?(\d{1,2})[-/](\d{1,2})\s+(\d{1,2}:\d{2})"
    r"(?:\s+周[一二三四五六日天]|\s+星期[一二三四五六日天])?"
    r"(?:\s+[^\s]{2,10}?市?)?\s+"
    r"([^\s\d]{2,28})\s+([^\s\d]{2,28})\s+"
    r"([0-9]+(?:\.[0-9]+)?)\s+([0-9]+\.[0-9]{2})"
)

DEFAULT_SAVED_PLACES: dict[str, str] = {
    "家": "示例小区A",
}


def _yuan_to_cents(val: float | str) -> int:
    try:
        return int((Decimal(str(val)) * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP))
    except Exception:
        return 0


def parse_didi_itinerary_text(text: str, default_year: Optional[int] = None) -> dict[str, Any]:
    """从滴滴/高德《行程报销单》PDF 文本提取总金额、行程笔数与每笔起终点明细。"""
    yr = default_year or now_local().year
    m_date = ISSUE_DATE_RE.search(text)
    if m_date:
        yr = int(m_date.group(1))

    trips: list[dict[str, Any]] = []
    for m in DIDI_TRIP_LINE_RE.finditer(text):
        car_type = m.group(1)
        row_yr = int(m.group(2)) if m.group(2) else yr
        mo = int(m.group(3))
        dy = int(m.group(4))
        hhmm = m.group(5)
        origin = m.group(6).strip("|｜ ")
        dest = m.group(7).strip("|｜ ")
        km = float(m.group(8))
        amt_s = m.group(9)
        amt_c = _yuan_to_cents(amt_s)
        trips.append(
            {
                "car_type": car_type,
                "occurred_at": f"{row_yr:04d}-{mo:02d}-{dy:02d}T{hhmm}:00+08:00",
                "origin": origin,
                "dest": dest,
                "route": f"{origin}→{dest}",
                "distance_km": km,
                "amount_cents": amt_c,
                "amount_yuan": round(amt_c / 100.0, 2),
            }
        )

    total_cents = 0
    m_tot = ITINERARY_TOTAL_RE.search(text)
    if m_tot:
        total_cents = _yuan_to_cents(m_tot.group(1) or m_tot.group(2))
    elif trips:
        total_cents = sum(t["amount_cents"] for t in trips)

    return {
        "is_itinerary": bool("行程报销单" in text or "行程单" in text or trips),
        "total_cents": total_cents,
        "total_yuan": round(total_cents / 100.0, 2),
        "trip_count": len(trips),
        "trips": trips,
    }


def classify_pdf_document(pdf_path: Path) -> dict[str, Any]:
    """识别单张国内 PDF 是「电子发票（打车/美团餐饮/其他）」还是「滴滴/高德行程报销单」。"""
    name = pdf_path.name
    txt = ""
    try:
        reader = PdfReader(str(pdf_path))
        txt = "\n".join((page.extract_text() or "") for page in reader.pages[:4])
    except Exception:
        txt = ""

    combined = f"{name}\n{txt}"
    m_dt = ISSUE_DATE_RE.search(txt)
    doc_date = (
        f"{int(m_dt.group(1)):04d}-{int(m_dt.group(2)):02d}-{int(m_dt.group(3)):02d}"
        if m_dt
        else ""
    )

    if any(k in combined for k in ("行程报销单", "滴滴出行—行程", "高德打车行程")):
        itin = parse_didi_itinerary_text(txt)
        amt_c = itin["total_cents"]
        if amt_c == 0:
            all_y = [_yuan_to_cents(x) for x in INVOICE_YUAN_ALL_RE.findall(txt)]
            amt_c = max(all_y) if all_y else 0
        routes = [t["route"] for t in itin["trips"][:5]]
        return {
            "file": name,
            "path": str(pdf_path),
            "doc_kind": "itinerary",
            "category": "打车出行",
            "invoice_no": "",
            "issue_date": doc_date,
            "amount_cents": amt_c,
            "amount_yuan": round(amt_c / 100.0, 2),
            "trip_count": itin["trip_count"],
            "routes": routes,
            "trips": itin["trips"],
        }

    amt_c = 0
    m_a = INVOICE_AMOUNT_RE.search(txt) or INVOICE_CN_UPPER_AMT_RE.search(txt)
    if m_a:
        amt_c = _yuan_to_cents(m_a.group(1))
    else:
        all_y = [_yuan_to_cents(x) for x in INVOICE_YUAN_ALL_RE.findall(txt)]
        if all_y:
            amt_c = max(all_y)

    m_n = INVOICE_NO_RE.search(txt) or INVOICE_20D_RE.search(txt) or INVOICE_20D_RE.search(name)
    inv_no = m_n.group(1) if m_n else ""

    if any(k in combined for k in ("运输服务", "客运服务", "滴滴", "小桔", "高德", "曹操出行", "首汽约车", "打车")):
        cat = "打车出行"
    elif any(k in combined for k in ("餐饮服务", "美团", "三快", "饿了么", "拉扎斯", "肯德基", "麦当劳", "星巴克", "瑞幸")):
        cat = "餐饮外卖"
    else:
        cat = "其他国内票据"

    return {
        "file": name,
        "path": str(pdf_path),
        "doc_kind": "invoice",
        "category": cat,
        "invoice_no": inv_no,
        "issue_date": doc_date,
        "amount_cents": amt_c,
        "amount_yuan": round(amt_c / 100.0, 2),
        "trip_count": 0,
        "routes": [],
        "trips": [],
    }


def stage_invoice_attachment(db: SimingDB, filename: str, content: bytes) -> Path:
    ym = now_local().strftime("%Y-%m")
    out_dir = db.paths.reimburse_dir / ym
    out_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(content).hexdigest()[:10]
    safe_name = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", Path(filename).name)
    out_path = out_dir / f"{digest}-{safe_name}"
    if not out_path.exists():
        out_path.write_bytes(content)
        os.chmod(out_path, 0o600)
    return out_path


def summarize_reimbursement_bundle(db: SimingDB, year_month: str = "") -> dict[str, Any]:
    """盘点当期持久归集目录：严格区分发票与行程单（防行程单金额与发票双算！），并自动配对 CW01 滴滴发票↔行程单。"""
    ym = year_month or now_local().strftime("%Y-%m")
    bundle_dir = db.paths.reimburse_dir / ym
    if not bundle_dir.is_dir():
        return {
            "ok": True,
            "year_month": ym,
            "count": 0,
            "itinerary_count": 0,
            "total_yuan": 0.0,
            "files": [],
            "itineraries": [],
            "paired_rides": [],
            "missing_itinerary_invoices": [],
            "missing_invoice_itineraries": [],
            "dining_invoices": [],
            "bundle_dir": str(bundle_dir),
        }

    invoices: list[dict[str, Any]] = []
    itineraries: list[dict[str, Any]] = []

    for pdf in sorted(bundle_dir.glob("*.pdf")):
        meta = classify_pdf_document(pdf)
        if meta["doc_kind"] == "itinerary":
            itineraries.append(meta)
        else:
            invoices.append(meta)

    # 1-to-1 配对打车电子发票与行程报销单（同整数分金额优先唯一匹配）
    ride_invoices = [iv for iv in invoices if iv["category"] == "打车出行"]
    dining_invoices = [iv for iv in invoices if iv["category"] == "餐饮外卖"]
    unused_itins = list(itineraries)
    paired_rides: list[dict[str, Any]] = []
    missing_itin_invs: list[dict[str, Any]] = []

    for iv in ride_invoices:
        amt_c = iv["amount_cents"]
        same_amt = [it for it in unused_itins if amt_c > 0 and it["amount_cents"] == amt_c]
        if len(same_amt) == 1:
            matched_it = same_amt[0]
            unused_itins.remove(matched_it)
            paired_rides.append(
                {
                    "invoice_file": iv["file"],
                    "invoice_no": iv["invoice_no"],
                    "itinerary_file": matched_it["file"],
                    "amount_yuan": iv["amount_yuan"],
                    "routes": matched_it["routes"],
                }
            )
        elif len(same_amt) >= 2:
            # 若存在同日期唯一匹配则配对，否则保留待人工核对不乱配
            same_date = [it for it in same_amt if iv["issue_date"] and it["issue_date"] == iv["issue_date"]]
            if len(same_date) == 1:
                matched_it = same_date[0]
                unused_itins.remove(matched_it)
                paired_rides.append(
                    {
                        "invoice_file": iv["file"],
                        "invoice_no": iv["invoice_no"],
                        "itinerary_file": matched_it["file"],
                        "amount_yuan": iv["amount_yuan"],
                        "routes": matched_it["routes"],
                    }
                )
            else:
                missing_itin_invs.append({**iv, "reason": "多张同额行程单待人工指定配对"})
        else:
            missing_itin_invs.append({**iv, "reason": "缺对应金额行程报销单"})

    missing_inv_itins = [{**it, "reason": "缺对应金额打车电子发票"} for it in unused_itins]
    total_invoice_cents = sum(iv["amount_cents"] for iv in invoices)

    return {
        "ok": True,
        "year_month": ym,
        "count": len(invoices),
        "itinerary_count": len(itineraries),
        "total_yuan": round(total_invoice_cents / 100.0, 2),
        "files": invoices,
        "itineraries": itineraries,
        "paired_rides": paired_rides,
        "missing_itinerary_invoices": missing_itin_invs,
        "missing_invoice_itineraries": missing_inv_itins,
        "dining_invoices": dining_invoices,
        "bundle_dir": str(bundle_dir),
    }


def load_saved_places(db: SimingDB) -> dict[str, str]:
    policy = load_policy(db.paths)
    places = dict(DEFAULT_SAVED_PLACES)
    cfg_places = policy.get("places")
    if isinstance(cfg_places, dict):
        for k, v in cfg_places.items():
            if k and v:
                places[str(k).strip()] = str(v).strip()
    raw_meta = db.get_meta("saved_places_json")
    if raw_meta:
        try:
            import json

            m_obj = json.loads(raw_meta)
            if isinstance(m_obj, dict):
                for k, v in m_obj.items():
                    if k and v:
                        places[str(k).strip()] = str(v).strip()
        except Exception:
            pass
    return places


def set_saved_place(db: SimingDB, alias: str, address: str) -> dict[str, str]:
    import json

    a = (alias or "").strip()
    addr = (address or "").strip()
    cur: dict[str, str] = {}
    raw_meta = db.get_meta("saved_places_json")
    if raw_meta:
        try:
            cur = json.loads(raw_meta)
        except Exception:
            cur = {}
    cur[a] = addr
    db.set_meta("saved_places_json", json.dumps(cur, ensure_ascii=False))
    db.record_human_use("didi_shortcut", "set_place", ref_id=a)
    return load_saved_places(db)


def resolve_place_alias(db: SimingDB, name: str) -> str:
    places = load_saved_places(db)
    n = (name or "").strip()
    return places.get(n, n)


def build_didi_deeplink(origin_name: str, dest_name: str) -> str:
    """只生成唤端确认链接（严守 A3 红线，绝不自动发单叫车）。"""
    q = urllib.parse.urlencode({"from_name": origin_name, "to_name": dest_name})
    return f"diditaxi://oneTravel/open?{q}"


def format_didi_ride_card(db: SimingDB, dest_raw: str, origin_raw: str = "家") -> str:
    """生成带预设地址展开 + 北京当日尾号限行提醒的滴滴一键唤车卡。"""
    origin_resolved = resolve_place_alias(db, origin_raw or "家")
    dest_resolved = resolve_place_alias(db, dest_raw)
    link = build_didi_deeplink(origin_resolved, dest_resolved)
    cal = day_info(now_local().date(), db.paths)
    db.record_human_use("didi_shortcut", "ride_card", ref_id=dest_resolved[:24])
    return (
        f"🚕 滴滴唤车确认卡（本人点开 App 核对车型后呼叫，绝不自动下单）\n"
        f"• 路线：{origin_resolved} -> {dest_resolved}\n"
        f"• 今日北京日历：{cal.brief_header}\n"
        f"• 一键唤端：{link}"
    )
