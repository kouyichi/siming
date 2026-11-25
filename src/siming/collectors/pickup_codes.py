"""快递取件码（18:00 回家顺路合并）与美团/饿了么热食外卖入柜（即时提醒，不压到 18:00）分流采集器。"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Optional

from siming.crypto import contains_otp, store_encrypted_raw
from siming.db import SimingDB, iso_now
from siming.modules import is_module_enabled

# 1. 热食外卖柜特征（示例：「【美团】您的外卖已放在示例小区A公寓入口北侧3格口，点击 mt.cn/xxx 或使用0435取件」
#    以及超时催取：「【美团】您的美团订单入柜已超过1小时，请及时取餐」）
FOOD_CABINET_RE = re.compile(r"(外卖已放在|外卖柜|智能取餐柜|取餐码|美团订单入柜已超过|饿了么.*(?:柜|格口))")
FOOD_LOC_RE = re.compile(r"(?:已放在|放至|存放在)([^，,。；;]{3,42})")
FOOD_CODE_RE = re.compile(r"(?:取餐码|取件码|密码|凭|使用)\s*[：:]?\s*(\d{4,8})(?:取件|取餐|\b)")
FOOD_OVERDUE_RE = re.compile(r"入柜已超过\s*(\d+)\s*小时")

# 2. 普通快递驿站 / 快递柜（静默入库，下班离司或 18:00 回家窗口按地点合并推送）
PICKUP_KEYWORDS_RE = re.compile(
    r"(菜鸟驿站|菜鸟|丰巢柜?|妈妈驿站|兔喜|近邻宝|熊猫快收|小兵驿站|快宝|速递易|蜜罐|"
    r"顺丰快递柜|京东快递柜|智能快递柜|快递柜|自提柜|驿站|取件码|提货码|取货码|货架码|出库码|开柜码|开箱码)"
)
STATION_RE = re.compile(
    r"【([^】]{2,16})】|(菜鸟驿站[^\s，,。]{0,14}|丰巢[^\s，,。]{0,14}|妈妈驿站[^\s，,。]{0,14}|兔喜[^\s，,。]{0,14}|近邻宝[^\s，,。]{0,14}|熊猫快收[^\s，,。]{0,14})"
)
CODE_EXPLICIT_RE = re.compile(
    r"(?:取件码|提货码|取货码|货架码|出库码|开柜码|开箱码|密码)[：:\s为是]*([A-Za-z0-9]+(?:-[A-Za-z0-9]+){1,3}|[A-Za-z0-9]{4,10})"
)
CODE_PING_RE = re.compile(
    r"凭[：:\s]*([A-Za-z0-9]+(?:-[A-Za-z0-9]+){1,3}|[A-Za-z0-9]{4,10})(?=\s*(?:取件|领取|自取|自提|开柜|开箱|至|到))"
)
CODE_RE = CODE_EXPLICIT_RE
PICKUP_LOCATION_RE = re.compile(
    r"(?:已到达|到达|至|已到|到|已存入|存入|已放入|放入|已放至|放至|已放在|放在|已放置于|已放置到|已投递至|投递至|已投递到|投递到|投放至|已送达至|送达至|送至|位于)"
    r"\s*([^，,。；;\n]{2,48}?)(?=(?:取件|领取|自取|自提|凭|取件码|提货码|取货码|，|,|。|；|;|\n|$))"
)
FREE_WINDOW_RE = re.compile(r"(\d+\s*(?:小时|天)内?免费)")
NON_PICKUP_MARKETING_RE = re.compile(
    r"(家电特邀|享.*补贴|上门安装|工程师|派单成功|服务师|立减金|月付|额度消费|流量使用|套餐内|活动时间|拒收请回复R.*立减|预售成功|预计\d{1,2}:\d{2}送达)"
)

# 3. 放家门口 / 前台 / 门卫 / 物业 / 代收点等无取件码快递送达短信
#    示例：「【京东快递】快递尾号48171已放在家门口，快递员电话 13800138000，查看签收照片或反馈异常」
DOORSTEP_CARRIER_RE = re.compile(
    r"(京东快递|顺丰速运|顺丰|中通快递|中通|圆通速递|圆通快递|圆通|申通快递|申通|韵达快递|韵达|极兔速递|极兔|德邦快递|中国邮政|EMS|菜鸟|快递|快件|包裹)"
)
DOORSTEP_DROP_RE = re.compile(
    r"(?:已放在|已放至|已放置于|已放置到|已放入|已放于|已投递至|已投递到|已投放至|已送达至|已送达到|已送达|已存放在|已存放至|放在|放至|放于)(?P<loc>[^，,。；;\n]{2,32})"
    r"|(?P<door>放(?:在)?(?:您)?家?门口)"
    r"|(?P<proxy>已由[^，,。；;\n]{1,16}(?:代收|签收))"
)
TAIL_NO_RE = re.compile(r"(?:尾号|运单号|单号)[：:\s]*([A-Za-z0-9]{4,14})")
COURIER_PHONE_RE = re.compile(r"(?:快递员|派件员|小哥)?(?:联系)?电话[：:\s]*(1\d{10})")

# 默认站点关键字（可在 ~/.config/siming/policy.yaml `pickup_sites` 中自定义）
_HOME_PRIMARY_TOKENS = ("示例小区A", "阳光花园")
_HOME_ALT_TOKENS = ("示例小区B", "翠竹苑")
_OFFICE_TOKENS = ("示例写字楼", "科技园大厦")


def _normalize_pickup_brand(brand: str) -> str:
    b = (brand or "").strip()
    if "丰巢" in b:
        return "丰巢"
    if "菜鸟" in b:
        return "菜鸟驿站"
    if "妈妈驿站" in b:
        return "妈妈驿站"
    if "兔喜" in b:
        return "兔喜"
    if "近邻宝" in b:
        return "近邻宝"
    return b or "快递点"


def _find_open_duplicate_pickup_case(
    conn,
    *,
    code: str = "",
    tail_no: str = "",
    site_alias: str = "",
) -> Optional[str]:
    """跨短信品牌变体去重：若库中已有未结 pickup 单据且取件码（或尾号）相同、且站点不冲突（如非家 vs 新房），复用已有单据不再重复建单。"""
    norm_code = (code or "").strip().upper()
    norm_tail = (tail_no or "").strip().upper()
    if not norm_code and not norm_tail:
        return None
    rows = conn.execute(
        "SELECT case_id, context_json FROM cases WHERE status='待处理' AND category IN ('pickup', 'pickup_code') ORDER BY created_at DESC LIMIT 80"
    ).fetchall()
    for r in rows:
        try:
            ctx = json.loads(r["context_json"] or "{}")
            if not isinstance(ctx, dict):
                continue
        except Exception:
            continue
        ex_site = str(ctx.get("site_alias") or "").strip()
        if site_alias and ex_site and site_alias != ex_site:
            continue
        if norm_code and str(ctx.get("code") or "").strip().upper() == norm_code:
            return str(r["case_id"])
        if norm_tail and str(ctx.get("tail_no") or "").strip().upper() == norm_tail:
            return str(r["case_id"])
    return None


def _extract_pickup_code(text: str) -> str:
    m = CODE_EXPLICIT_RE.search(text)
    if m:
        return m.group(1).strip()
    m2 = CODE_PING_RE.search(text)
    if m2:
        return m2.group(1).strip()
    return ""


def _extract_pickup_location(text: str) -> str:
    """提取取件短信里的具体柜机/驿站地址（如『示例小区A大堂门口丰巢柜』『示例小区B1号柜』）。"""
    for m in PICKUP_LOCATION_RE.finditer(text):
        cand = m.group(1).strip(" ，,。；;：:！!（）()")
        if not cand or len(cand) < 2:
            continue
        if re.fullmatch(r"(?:取件|领取|自取|自提|本柜|快递柜|驿站|柜机)", cand):
            continue
        if re.search(r"^(?:http|https|www\.|3\.cn|mt\.cn)", cand, re.IGNORECASE):
            continue
        return cand[:48]
    return ""


def _extract_free_window(text: str) -> str:
    m = FREE_WINDOW_RE.search(text)
    return re.sub(r"\s+", "", m.group(1)) if m else ""


def classify_pickup_site(db: SimingDB, location: str, raw_text: str = "") -> dict[str, str]:
    """结合 policy.yaml 当前生效住址与自定义小区别名（`pickup_sites`），判定包裹所在地点（家 / 新房 / 公司 / 未知）。"""
    hay = f"{location or ''} {raw_text or ''}"
    active_is_alt = False
    home_tokens = list(_HOME_PRIMARY_TOKENS)
    alt_tokens = list(_HOME_ALT_TOKENS)
    office_tokens = list(_OFFICE_TOKENS)
    try:
        from siming.config import load_policy
        from siming.db import now_local

        policy = load_policy(db.paths)
        ps = policy.get("pickup_sites") or {}
        if isinstance(ps, dict):
            if ps.get("home_tokens"):
                home_tokens = [str(x) for x in ps["home_tokens"] if str(x).strip()]
            if ps.get("alt_home_tokens"):
                alt_tokens = [str(x) for x in ps["alt_home_tokens"] if str(x).strip()]
            if ps.get("office_tokens"):
                office_tokens = [str(x) for x in ps["office_tokens"] if str(x).strip()]
        regimes = ((policy.get("daily_care") or {}).get("regimes") or []) if isinstance(policy, dict) else []
        today_s = now_local().date().isoformat()
        for rg in regimes:
            if not isinstance(rg, dict):
                continue
            f_s = str(rg.get("from") or "")
            u_s = str(rg.get("until") or "")
            if (not f_s or today_s >= f_s) and (not u_s or today_s < u_s):
                h_lbl = str(rg.get("home_label") or "")
                if any(k in h_lbl for k in alt_tokens):
                    active_is_alt = True
                break
    except Exception:
        active_is_alt = False

    home_tag = home_tokens[0] if home_tokens else "常住地址"
    alt_tag = alt_tokens[0] if alt_tokens else "备用地址"
    office_tag = office_tokens[0] if office_tokens else "办公地址"

    if any(k in hay for k in alt_tokens):
        if active_is_alt:
            return {"site_alias": "家", "site_label": f"家（{alt_tag}）"}
        return {"site_alias": "新房", "site_label": f"新房（{alt_tag}）"}
    if any(k in hay for k in home_tokens):
        if active_is_alt:
            return {"site_alias": "旧住址", "site_label": f"旧住址（{home_tag}）"}
        return {"site_alias": "家", "site_label": f"家（{home_tag}）"}
    if any(k in hay for k in office_tokens):
        return {"site_alias": "公司", "site_label": f"公司（{office_tag}）"}
    if "家门口" in (location or "") or "家门口" in (raw_text or ""):
        return {"site_alias": "家", "site_label": "家门口"}
    return {"site_alias": "", "site_label": ""}


def _match_buy_track_delivering_item(
    text: str = "",
    courier_phone: str = "",
    tail_no: str = "",
) -> str:
    """仅当短信与 buy/track 订单存在明确关联（快递员电话或订单/尾号命中）时才挂接商品名，严禁把大家电盲挂到普通丰巢短信！"""
    auto_path = Path.home() / "buy" / "track" / "data" / "auto.json"
    if not auto_path.is_file():
        return ""
    if not courier_phone and not tail_no:
        return ""
    try:
        data = json.loads(auto_path.read_text(encoding="utf-8"))
        for item in data.get("items") or []:
            info = item.get("orderInfo") or {}
            prog = str(info.get("progress") or "")
            order_id = str(item.get("order") or "")
            if tail_no and len(tail_no) >= 4 and order_id and order_id.endswith(tail_no):
                return str(item.get("name") or "")
            if courier_phone and courier_phone in prog and any(k in prog for k in ("派送", "送达", "签收")):
                if "京东" in text or "家门口" in text:
                    return str(item.get("name") or "")
        return ""
    except Exception:
        return ""


def _ingest_food_cabinet(
    db: SimingDB,
    text: str,
    source: str,
    raw_ref: Optional[str],
) -> dict[str, Any]:
    ref = raw_ref or store_encrypted_raw(text, db.paths)
    ts = iso_now()
    should_notify = (source != "ios_sms_backfill") and is_module_enabled(db, "meituan_cabinet")

    m_overdue = FOOD_OVERDUE_RE.search(text)
    if m_overdue and not FOOD_CODE_RE.search(text):
        hours = m_overdue.group(1)
        fp_ov = f"food_overdue:{ts[:13]}:{hours}:{hashlib.sha1(text.encode('utf-8')).hexdigest()[:8]}"
        with db.transaction() as conn:
            ev_id = db.append_event(
                conn,
                source=source,
                thread_key="food_pickup:meituan",
                summary=f"外卖入柜超时提醒（已超 {hours} 小时）",
                payload={"overdue_hours": int(hours)},
                raw_ref=ref,
                fingerprint=fp_ov,
            )
            if ev_id is None:
                return {"matched": True, "duplicate": True, "kind": "food_pickup"}

            open_case = conn.execute(
                "SELECT case_id, title FROM cases WHERE category='food_pickup' AND status='待处理' ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
            if open_case:
                case_id = str(open_case["case_id"])
                title_ref = str(open_case["title"])
                conn.execute(
                    "UPDATE cases SET next_step=?, state_version=state_version+1, updated_at=? WHERE case_id=?",
                    (f"已入柜超 {hours} 小时，请尽快取餐", ts, case_id),
                )
            else:
                case_id = db.next_seq_id(conn, "S")
                title_ref = f"美团外卖待取（入柜已超 {hours} 小时）"
                conn.execute(
                    """
                    INSERT INTO cases (
                        case_id, title, next_step, done_rule, status, strength,
                        category, rule_id, evidence_ids, created_at, updated_at
                    ) VALUES (?, ?, '请尽快去外卖柜取餐', '回「拿了」或「取了」', '待处理', 'NORMAL', 'food_pickup', 'meituan_cabinet', ?, ?, ?)
                    """,
                    (case_id, title_ref, json.dumps([ev_id]), ts, ts),
                )

            if should_notify:
                msg = f"⏰ [{case_id}] 外卖入柜已超 {hours} 小时：{title_ref}\n拿完引用本条回「拿了」即可销账。"
                db.enqueue_effect(
                    conn,
                    "send_qq",
                    {"ref_id": case_id, "rule_id": "meituan_cabinet", "text": msg},
                )
        return {"matched": True, "kind": "food_pickup", "case_id": case_id, "overdue_hours": int(hours)}

    m_code = FOOD_CODE_RE.search(text)
    if not m_code:
        return {"matched": False}
    code = m_code.group(1).strip()
    m_loc = FOOD_LOC_RE.search(text)
    location = m_loc.group(1).strip() if m_loc else "美团外卖柜"
    brand = "饿了么" if "饿了么" in text else "美团"
    fp = f"food_pickup:{ts[:10]}:{location}:{code}"

    with db.transaction() as conn:
        ev_id = db.append_event(
            conn,
            source=source,
            thread_key=f"food_pickup:{brand}",
            summary=f"{brand}外卖入柜：{location} 取餐码 {code}",
            payload={"brand": brand, "location": location, "code": code},
            raw_ref=ref,
            fingerprint=fp,
        )
        if ev_id is None:
            return {"matched": True, "duplicate": True, "kind": "food_pickup", "code": code}

        case_id = db.next_seq_id(conn, "S")
        title = f"{brand}外卖柜：{location}（取餐码 {code}）"
        conn.execute(
            """
            INSERT INTO cases (
                case_id, title, next_step, done_rule, status, strength,
                category, rule_id, evidence_ids, context_json, created_at, updated_at
            ) VALUES (?, ?, '热食及时取餐', '回「拿了」或「取了」', '待处理', 'NORMAL', 'food_pickup', 'meituan_cabinet', ?, ?, ?, ?)
            """,
            (
                case_id,
                title[:120],
                json.dumps([ev_id]),
                json.dumps({"brand": brand, "location": location, "code": code}, ensure_ascii=False),
                ts,
                ts,
            ),
        )
        if should_notify:
            msg = (
                f"🍱 [{case_id}] {brand}外卖已入柜：{location}\n"
                f"• 取餐码：`{code}`\n"
                "拿完引用回一句「拿了」或「取了」即销账。"
            )
            db.enqueue_effect(
                conn,
                "send_qq",
                {"ref_id": case_id, "rule_id": "meituan_cabinet", "text": msg},
            )
    return {
        "matched": True,
        "kind": "food_pickup",
        "case_id": case_id,
        "station": location,
        "code": code,
        "summary": f"[{case_id}] 外卖入柜 {location} 取餐码 `{code}`（即时提醒）。",
    }


def _ingest_doorstep_delivery(
    db: SimingDB,
    text: str,
    source: str,
    raw_ref: Optional[str],
) -> dict[str, Any]:
    """识别无取件码的快递送达短信（如『【京东快递】快递尾号48171已放在家门口』），同样建 `pickup` 待办供下班顺路提醒。"""
    if NON_PICKUP_MARKETING_RE.search(text):
        return {"matched": False}
    if not DOORSTEP_CARRIER_RE.search(text):
        return {"matched": False}
    m_drop = DOORSTEP_DROP_RE.search(text)
    if not m_drop:
        return {"matched": False}

    drop_loc = (m_drop.group("loc") or m_drop.group("door") or m_drop.group("proxy") or "").strip(" ，,。；;：:！")
    if not drop_loc or re.search(r"^(?:http|https|www\.|请)", drop_loc, re.IGNORECASE):
        return {"matched": False}

    m_st = STATION_RE.search(text)
    carrier = (m_st.group(1) or m_st.group(2) or "快递").strip() if m_st else "快递"
    m_tail = TAIL_NO_RE.search(text)
    tail_no = m_tail.group(1).strip() if m_tail else ""
    m_phone = COURIER_PHONE_RE.search(text)
    courier_phone = m_phone.group(1).strip() if m_phone else ""

    site = classify_pickup_site(db, drop_loc, text)
    site_alias = site["site_alias"]
    site_label = site["site_label"]
    matched_name = _match_buy_track_delivering_item(text=text, courier_phone=courier_phone, tail_no=tail_no)
    ref = raw_ref or store_encrypted_raw(text, db.paths)
    ts = iso_now()
    fp_tail = tail_no or hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
    fp = f"pickup_drop:{ts[:10]}:{carrier}:{fp_tail}"
    raw_snippet = re.sub(r"\s+", " ", text.strip())[:180]

    with db.transaction() as conn:
        dup_cid = _find_open_duplicate_pickup_case(conn, tail_no=tail_no, site_alias=site_alias) if tail_no else None
        if dup_cid:
            return {
                "matched": True,
                "duplicate": True,
                "kind": "doorstep_pickup",
                "case_id": dup_cid,
                "station": drop_loc,
                "location": drop_loc,
                "site_alias": site_alias,
                "carrier": carrier,
                "tail_no": tail_no,
                "summary": f"{carrier} 尾号{tail_no}（{drop_loc}）已在库中（[{dup_cid}]）",
            }
        ev_id = db.append_event(
            conn,
            source=source,
            thread_key=f"pickup:{drop_loc}",
            summary=f"快递送达待拿：{carrier}" + (f" 尾号{tail_no}" if tail_no else "") + f"（{drop_loc}）",
            payload={
                "station": drop_loc,
                "location": drop_loc,
                "carrier": carrier,
                "drop_loc": drop_loc,
                "site_alias": site_alias,
                "site_label": site_label,
                "tail_no": tail_no,
                "courier_phone": courier_phone,
                "matched_order_name": matched_name,
                "raw_snippet": raw_snippet,
            },
            raw_ref=ref,
            fingerprint=fp,
        )
        if ev_id is None:
            return {"matched": True, "duplicate": True, "case_id": "", "summary": f"{carrier}（{drop_loc}）已在库中"}

        case_id = db.next_seq_id(conn, "S")
        tail_part = f" 尾号{tail_no}" if tail_no else ""
        item_part = f"（{matched_name}）" if matched_name else ""
        site_prefix = "【新房】" if site_alias == "新房" else ("【公司】" if site_alias == "公司" else "")
        title = f"{site_prefix}{carrier}{tail_part}（{drop_loc}）{item_part}"
        if site_alias == "新房":
            next_step = f"⚠️ 新房地址（{drop_loc}），非当前住址顺路"
        elif site_alias == "公司":
            next_step = f"已送达公司附近（{drop_loc}），在司期间顺手取"
        elif "门口" in drop_loc:
            next_step = f"已放在{drop_loc}，回家顺路拿进屋"
        else:
            next_step = f"已送达{drop_loc}，回家顺路取件"
        conn.execute(
            """
            INSERT INTO cases (
                case_id, title, next_step, done_rule, status, strength,
                category, rule_id, evidence_ids, context_json, created_at, updated_at
            ) VALUES (?, ?, ?, '本人确认已取', '待处理', 'SOFT', 'pickup', 'pickup_1800', ?, ?, ?, ?)
            """,
            (
                case_id,
                title[:120],
                next_step,
                json.dumps([ev_id]),
                json.dumps(
                    {
                        "station": drop_loc,
                        "location": drop_loc,
                        "code": f"尾号{tail_no}" if tail_no else drop_loc,
                        "carrier": carrier,
                        "drop_loc": drop_loc,
                        "site_alias": site_alias,
                        "site_label": site_label,
                        "tail_no": tail_no,
                        "courier_phone": courier_phone,
                        "matched_order_name": matched_name,
                        "raw_snippet": raw_snippet,
                    },
                    ensure_ascii=False,
                ),
                ts,
                ts,
            ),
        )

    if source != "ios_sms_backfill":
        try:
            from siming.spatial_life import check_urgent_cold_chain_sms

            check_urgent_cold_chain_sms(db, text, case_id=case_id, site_alias=site_alias, location=drop_loc)
        except Exception:
            pass

    return {
        "matched": True,
        "kind": "doorstep_pickup",
        "case_id": case_id,
        "station": drop_loc,
        "location": drop_loc,
        "site_alias": site_alias,
        "carrier": carrier,
        "tail_no": tail_no,
        "summary": f"[{case_id}] 已收录 {title}，将在下班离司或回家窗口提醒。",
    }


def try_ingest_pickup_code(
    db: SimingDB,
    text: str,
    source: str = "ios_sms",
    raw_ref: Optional[str] = None,
) -> dict[str, Any]:
    if not text or contains_otp(text):
        return {"matched": False}

    # 优先识别美团/饿了么热食外卖入柜（即时提醒，绝不等到 18:00 放凉！）
    if FOOD_CABINET_RE.search(text):
        food_res = _ingest_food_cabinet(db, text, source=source, raw_ref=raw_ref)
        if food_res.get("matched"):
            return food_res

    if NON_PICKUP_MARKETING_RE.search(text):
        return {"matched": False}

    if not PICKUP_KEYWORDS_RE.search(text):
        return _ingest_doorstep_delivery(db, text, source=source, raw_ref=raw_ref)

    code = _extract_pickup_code(text)
    if not code:
        return _ingest_doorstep_delivery(db, text, source=source, raw_ref=raw_ref)

    m_st = STATION_RE.search(text)
    brand = "快递点"
    if m_st:
        brand = (m_st.group(2) or m_st.group(1) or "快递点").strip()
    location = _extract_pickup_location(text)
    free_window = _extract_free_window(text)
    m_phone = COURIER_PHONE_RE.search(text)
    courier_phone = m_phone.group(1).strip() if m_phone else ""
    m_tail = TAIL_NO_RE.search(text)
    tail_no = m_tail.group(1).strip() if m_tail else ""

    if location:
        station = location if brand in location else f"{brand}·{location}"
    else:
        station = brand

    site = classify_pickup_site(db, location or station, text)
    site_alias = site["site_alias"]
    site_label = site["site_label"]
    matched_name = _match_buy_track_delivering_item(text=text, courier_phone=courier_phone, tail_no=tail_no)
    ref = raw_ref or store_encrypted_raw(text, db.paths)
    norm_brand = _normalize_pickup_brand(brand)
    fp = f"pickup:{norm_brand}:{code.upper()}"
    ts = iso_now()
    raw_snippet = re.sub(r"\s+", " ", text.strip())[:180]

    with db.transaction() as conn:
        dup_cid = _find_open_duplicate_pickup_case(conn, code=code, site_alias=site_alias)
        if dup_cid:
            return {
                "matched": True,
                "duplicate": True,
                "kind": "parcel_pickup",
                "case_id": dup_cid,
                "station": station,
                "brand": brand,
                "location": location,
                "site_alias": site_alias,
                "code": code,
                "summary": f"取件码 {code} 已在库中（[{dup_cid}]）",
            }
        ev_id = db.append_event(
            conn,
            source=source,
            thread_key=f"pickup:{station}",
            summary=f"取件码入库：{station} {code}",
            payload={
                "station": station,
                "brand": brand,
                "location": location,
                "site_alias": site_alias,
                "site_label": site_label,
                "free_window": free_window,
                "code": code,
                "courier_phone": courier_phone,
                "tail_no": tail_no,
                "matched_order_name": matched_name,
                "raw_snippet": raw_snippet,
            },
            raw_ref=ref,
            fingerprint=fp,
        )
        if ev_id is None:
            return {"matched": True, "duplicate": True, "case_id": "", "summary": f"取件码 {code} 已在库中"}

        case_id = db.next_seq_id(conn, "S")
        site_prefix = "【新房】" if site_alias == "新房" else ("【公司】" if site_alias == "公司" else "")
        title = f"{site_prefix}{station} 取件码 {code}" + (f"（{matched_name}）" if matched_name else "")
        free_part = f" · {free_window}" if free_window else ""
        if site_alias == "新房":
            next_step = f"⚠️ 新房地址（{location or station}{free_part}），非当前住址顺路"
        elif site_alias == "公司":
            next_step = f"公司附近（{location or station}{free_part}），在司期间顺手取"
        elif location:
            loc_tag = f"（{site_label}{free_part}）" if site_label else (f"（{free_window}）" if free_window else "")
            next_step = f"地点：{location}{loc_tag}，回家顺路取件"
        else:
            next_step = f"下班回家顺路取件{f'（{free_window}）' if free_window else ''}"
        conn.execute(
            """
            INSERT INTO cases (
                case_id, title, next_step, done_rule, status, strength,
                category, rule_id, evidence_ids, context_json, created_at, updated_at
            ) VALUES (?, ?, ?, '本人确认已取', '待处理', 'SOFT', 'pickup', 'pickup_1800', ?, ?, ?, ?)
            """,
            (
                case_id,
                title[:120],
                next_step[:240],
                json.dumps([ev_id]),
                json.dumps(
                    {
                        "station": station,
                        "brand": brand,
                        "location": location,
                        "site_alias": site_alias,
                        "site_label": site_label,
                        "free_window": free_window,
                        "code": code,
                        "courier_phone": courier_phone,
                        "tail_no": tail_no,
                        "matched_order_name": matched_name,
                        "raw_snippet": raw_snippet,
                    },
                    ensure_ascii=False,
                ),
                ts,
                ts,
            ),
        )
    if source != "ios_sms_backfill":
        try:
            from siming.spatial_life import check_urgent_cold_chain_sms

            check_urgent_cold_chain_sms(db, text, case_id=case_id, site_alias=site_alias, location=location or station)
        except Exception:
            pass

    return {
        "matched": True,
        "kind": "parcel_pickup",
        "case_id": case_id,
        "station": station,
        "brand": brand,
        "location": location,
        "site_alias": site_alias,
        "code": code,
        "summary": f"[{case_id}] 已收录 {title}，将在下班离司或回家窗口提醒。",
    }
