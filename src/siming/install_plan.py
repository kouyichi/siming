"""S2-B：2026-12-10 新房家电送装准备与预约缺口只读投影（零重复台账）。
- 实购设备与默认房间真源：~/buy/track/items.json
- 到货与物流进度真源：~/buy/track/data/auto.json（严格校验快照新鲜度，区分「拣货完成」与真签收）
- 人工确认预约状态真源：81:/var/lib/track/manual.json 本地只读缓存（home:8443/track/ 唯一编辑入口）
- 装修前置约束：仅提取 ~/Work/deco/plans/ 白名单规划文件，显式标明「规划基线/待现场实测」，无记录售后标「未记录」。
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any, Optional

from siming.cn_calendar import day_info
from siming.db import SHANGHAI_TZ, SimingDB, now_local, parse_iso

DEFAULT_ITEMS_JSON = Path.home() / "buy" / "track" / "items.json"
DEFAULT_AUTO_JSON = Path.home() / "buy" / "track" / "data" / "auto.json"

_SIGNED_RAW_EXACT = {"完成", "已完成", "已签收", "交易成功", "妥投"}
_CANCELLED_RAW_EXACT = {"交易关闭", "已取消", "取消", "已退款", "退款成功", "订单取消"}
_SIGNED_PROGRESS_RE = re.compile(r"(已签收|签收人|投递并签收|代收点已签收|本人签收|家人签收|您的订单已送达|已送达)")
_IN_TRANSIT_PROGRESS_RE = re.compile(r"(正在派送|派送中|配送中|运输中|正在出库|准备出库|揽收|发往)")
_CONFIRMED_BOOKING_VALUES = {"已预约", "已确认", "已约", "booked", "confirmed", "已安装", "装好", "完成", "已验收", "完成安装", "验收完成"}


def _resolve_default_items_json() -> Path:
    ov = os.environ.get("SIMING_BUY_ROOT")
    if ov:
        return Path(ov) / "track" / "items.json"
    return DEFAULT_ITEMS_JSON


def _resolve_default_auto_json() -> Path:
    ov = os.environ.get("SIMING_BUY_ROOT")
    if ov:
        return Path(ov) / "track" / "data" / "auto.json"
    return DEFAULT_AUTO_JSON


def is_order_cancelled(stage: str, raw_state: str, progress: str = "") -> bool:
    st = (stage or "").strip().lower()
    raw = (raw_state or "").strip()
    prog = (progress or "").strip()
    if st in ("cancelled", "canceled", "closed", "refunded"):
        return True
    if raw in _CANCELLED_RAW_EXACT:
        return True
    if any(k in raw for k in ("交易关闭", "已取消", "订单取消", "已退款", "退款成功", "退款关闭", "已退货")):
        return True
    if any(k in prog for k in ("交易关闭", "订单已取消", "已退款", "退款成功", "退款关闭")):
        return True
    return False


def is_order_truly_signed(stage: str, raw_state: str, progress: str) -> bool:
    """严格判定快递/大家电是否真签收：绝不把「拣货完成 / 正在派送中 / 交易关闭」误判为已签收！"""
    if is_order_cancelled(stage, raw_state, progress):
        return False
    st = (stage or "").strip().lower()
    raw = (raw_state or "").strip()
    prog = (progress or "").strip()
    if raw in _SIGNED_RAW_EXACT:
        return True
    if _SIGNED_PROGRESS_RE.search(prog):
        return True
    # 若物流明确处于「正在派送中 / 配送中 / 运输中」且无已签收/已送达字样，即使上游 stage 标成 delivered 也绝不误判！
    if _IN_TRANSIT_PROGRESS_RE.search(prog) or _IN_TRANSIT_PROGRESS_RE.search(raw):
        return False
    if st in ("delivered", "signed"):
        return True
    return False


def classify_arrival_status(order_info: Optional[dict[str, Any]]) -> tuple[str, str]:
    """根据 auto.json 的 orderInfo 返回 (arrival_code, arrival_label)。"""
    if not order_info:
        return ("unknown", "待同步物流")
    stage = str(order_info.get("stage") or "")
    raw = str(order_info.get("raw") or "")
    progress = str(order_info.get("progress") or "")
    eta_date = str((order_info.get("eta") or {}).get("date") or "")

    if is_order_cancelled(stage, raw, progress):
        return ("cancelled", f"已取消·{raw or stage}")
    if is_order_truly_signed(stage, raw, progress):
        return ("signed", "已签收")
    if any(k in (raw + " " + progress) for k in ("派送", "运输", "发往", "到达", "配送", "途中")):
        eta_s = f"（预计 {eta_date}）" if eta_date else ""
        return ("shipping", f"运输中·{raw or '在途'}{eta_s}")
    if raw or stage:
        eta_s = f"（预计 {eta_date}）" if eta_date else ""
        return ("preparing", f"待收货·{raw or stage}{eta_s}")
    return ("unknown", "待同步物流")


def _load_manual_bookings(
    db: SimingDB, manual_json_path: Optional[Path] = None
) -> tuple[dict[str, Any], str]:
    """只读读取 81:/var/lib/track/manual.json 人工确认状态缓存。"""
    if manual_json_path is not None:
        if not manual_json_path.is_file():
            return ({}, "missing_override")
        raw_obj = json.loads(manual_json_path.read_text(encoding="utf-8"))
        if isinstance(raw_obj, dict) and "manual" in raw_obj and isinstance(raw_obj["manual"], dict):
            return (raw_obj["manual"], str(raw_obj.get("fetched_at") or "override"))
        return (raw_obj if isinstance(raw_obj, dict) else {}, "override")

    cache_p = db.paths.data_dir / "track_manual_cache.json"
    if cache_p.is_file():
        try:
            obj = json.loads(cache_p.read_text(encoding="utf-8"))
            if isinstance(obj, dict) and isinstance(obj.get("manual"), dict):
                return (obj["manual"], str(obj.get("fetched_at") or ""))
        except Exception:
            pass
    return ({}, "")


def _classify_booking_status(
    item: dict[str, Any], manual_entry: Optional[dict[str, Any]]
) -> tuple[str, str]:
    """严格区分 items.json 中的「意向日期 (2026-12-10)」与 manual.json 中的「人工已确认预约」。"""
    install_mode = str(item.get("install") or "none")
    if install_mode in ("none", ""):
        return ("not_needed", "无需上门安装")
    if install_mode == "self":
        return ("self", "自装/自行放置")

    m = manual_entry if isinstance(manual_entry, dict) else {}
    m_inst = str(m.get("install") or m.get("installStatus") or m.get("booking") or "").strip()
    m_date = str(m.get("installDate") or m.get("appointedAt") or m.get("date") or "").strip()
    m_note = str(m.get("note") or m.get("installNote") or "").strip()

    if m_inst in ("已安装", "装好", "完成", "已验收", "完成安装", "验收完成", "installed", "done"):
        return ("installed", f"已安装完结{f' ({m_date})' if m_date else ''}")
    if m_inst.lower() in {v.lower() for v in _CONFIRMED_BOOKING_VALUES} or (m_date and m.get("confirmed") is True):
        slot = f" {m_date}" if m_date else ""
        note_s = f" · {m_note}" if m_note else ""
        return ("booked", f"已确认预约{slot}{note_s}")

    target_intent = str(item.get("installDate") or "2026-12-10")
    return ("unbooked", f"待人工预约（意向 {target_intent}）")


def _parse_auto_generated_at(raw_ts: str) -> Optional[datetime]:
    if not raw_ts:
        return None
    try:
        return datetime.fromisoformat(raw_ts.replace("Z", "+00:00")).astimezone(SHANGHAI_TZ)
    except Exception:
        return None


def build_installation_projection(
    db: SimingDB,
    items_json_path: Optional[Path] = None,
    auto_json_path: Optional[Path] = None,
    manual_json_path: Optional[Path] = None,
) -> dict[str, Any]:
    """构建 12.10 全量家电送装与前置缺口结构化投影（100% 只读，不写回 track）。"""
    i_path = items_json_path or _resolve_default_items_json()
    a_path = auto_json_path or _resolve_default_auto_json()

    if not i_path.is_file():
        return {"ok": False, "error": f"缺少清单真源 {i_path}"}

    items_doc = json.loads(i_path.read_text(encoding="utf-8"))
    auto_doc: dict[str, Any] = {}
    if a_path.is_file():
        try:
            auto_doc = json.loads(a_path.read_text(encoding="utf-8"))
        except Exception:
            auto_doc = {}

    auto_by_id: dict[str, dict[str, Any]] = {}
    for a_it in auto_doc.get("items") or []:
        if isinstance(a_it, dict) and a_it.get("id"):
            auto_by_id[str(a_it["id"])] = a_it

    gen_at_raw = str(auto_doc.get("generatedAt") or "")
    gen_dt = _parse_auto_generated_at(gen_at_raw)
    auto_age_hours = (
        round((now_local() - gen_dt).total_seconds() / 3600.0, 1)
        if gen_dt is not None
        else None
    )
    auto_stale = auto_age_hours is not None and auto_age_hours > 36.0

    manual_map, manual_fetched_at = _load_manual_bookings(db, manual_json_path=manual_json_path)

    target_date_str = str((items_doc.get("preferences") or {}).get("install_date") or "2026-12-10")
    try:
        target_d = date.fromisoformat(target_date_str)
    except ValueError:
        target_d = date(2026, 12, 10)
    cal = day_info(target_d, db.paths)
    days_left = (target_d - now_local().date()).days

    projected_items: list[dict[str, Any]] = []
    cancelled_items: list[dict[str, Any]] = []
    missing_rooms: list[str] = []
    unbooked_install_items: list[str] = []
    missing_svc_brand_items: list[str] = []
    signed_waiting_install: list[str] = []

    for raw_item in items_doc.get("items") or []:
        iid = str(raw_item.get("id") or "")
        name = str(raw_item.get("name") or iid)
        install_mode = str(raw_item.get("install") or "none")
        auto_item = auto_by_id.get(iid) or {}
        manual_entry = manual_map.get(iid) if isinstance(manual_map.get(iid), dict) else {}

        # 房间归属优先读 manual 人工修正，否则读 items.json
        room = str(manual_entry.get("room") or raw_item.get("room") or "").strip()
        svc_raw = raw_item.get("svc") or auto_item.get("svc") or []
        svc_list = [
            str(s.get("id") or s.get("svcId") or s.get("orderId") or s.get("name") or s) if isinstance(s, dict) else str(s)
            for s in svc_raw
            if s
        ]
        extra_list = list(raw_item.get("extra") or auto_item.get("extra") or [])
        install_note = str(raw_item.get("installNote") or "")

        arr_code, arr_label = classify_arrival_status(auto_item.get("orderInfo"))
        m_arrive = str(manual_entry.get("arrive") or manual_entry.get("arrival") or "").strip()
        if m_arrive:
            if m_arrive in ("退货", "换货", "退换货", "已取消", "交易关闭"):
                arr_code, arr_label = ("cancelled", f"人工标注·{m_arrive}")
            elif m_arrive in ("未到", "在途"):
                arr_code, arr_label = ("preparing", f"人工标注·{m_arrive}")
            elif m_arrive in ("已到", "已到货", "已签收", "到货"):
                arr_code, arr_label = ("signed", f"人工确认·{m_arrive}")
        book_code, book_label = _classify_booking_status(raw_item, manual_entry)

        if arr_code == "cancelled":
            cancelled_items.append({"id": iid, "name": name, "arrival_label": arr_label})

        needs_vendor_install = install_mode in ("brand", "jd")
        if needs_vendor_install and not room:
            missing_rooms.append(f"{name}({iid})")
        if needs_vendor_install and book_code == "unbooked":
            unbooked_install_items.append(name)
        if needs_vendor_install and arr_code == "signed" and book_code == "unbooked":
            signed_waiting_install.append(name)
        if install_mode == "brand" and not svc_list:
            missing_svc_brand_items.append(f"{name}({iid})")

        contact_record = (
            f"京东服务单 {', '.join(svc_list)}"
            if svc_list
            else ("订单页联系品牌客服（服务单: 未记录）" if needs_vendor_install else "无需预约单")
        )

        projected_items.append(
            {
                "id": iid,
                "name": name,
                "cat": str(raw_item.get("cat") or ""),
                "room": room or "未填房间",
                "room_missing": not bool(room),
                "platform": str(raw_item.get("platform") or ""),
                "order": str(raw_item.get("order") or ""),
                "install_mode": install_mode,
                "needs_vendor_install": needs_vendor_install,
                "is_cancelled": arr_code == "cancelled",
                "intent_date": str(raw_item.get("installDate") or ""),
                "arrival_code": arr_code,
                "arrival_label": arr_label,
                "booking_code": book_code,
                "booking_label": "订单已取消（无需安装）" if arr_code == "cancelled" else book_label,
                "svc_orders": svc_list,
                "extra": extra_list,
                "install_note": install_note,
                "contact_record": contact_record,
                "source_ref": f"~/buy/track/items.json (id={iid})",
            }
        )

    install_11 = [it for it in projected_items if it["needs_vendor_install"]]
    self_or_none = [it for it in projected_items if not it["needs_vendor_install"]]
    active_install_items = [it for it in install_11 if it["arrival_code"] != "cancelled"]
    active_other_items = [it for it in self_or_none if it["arrival_code"] != "cancelled"]
    active_unbooked_install_items = [it["name"] for it in active_install_items if it["booking_code"] == "unbooked"]
    booked_cnt = sum(1 for it in active_install_items if it["booking_code"] in ("booked", "installed"))
    signed_install_cnt = sum(1 for it in active_install_items if it["arrival_code"] == "signed")

    # 前置条件与施工依赖顺序（只引确定性事实，规划尺寸显式标注待现场实测）
    prerequisites = [
        "1. 燃气与水路前置（先于林内热水器/烟灶/洗烘/科沃斯）：确认燃气表 G2.5 是否需燃气公司升级 G4 + 零冷水回水管就位 [items.json:52]；卫生间干区科沃斯上下水孔位与减压阀预留 [items.json:94]。",
        "2. 橱柜吊顶与墙面开孔（先于烟灶/晾衣机/壁挂洗衣机）：厨房橱柜台面开孔尺寸需在老板 E2S 上门前与橱柜方实测核准 [items.json:31]；阳台吊顶前确认米家晾衣机顶装打孔位 [items.json:193]；海尔 3kg 壁挂洗衣机需确认承重墙体与上下水 [items.json:79]（注：deco/plans/constraints-baseline.md 为规划基线，严禁代替现场实测放线）。",
        "3. 挂架与独立挂装预约：客厅小米 85 寸电视（配 NB P65 挂架 9000000000000002）主单不含挂装服务单，到货后需单独找小米/京东约挂装 [items.json:106-112]；卧室为极米 120 寸光子悬浮幕布（包安装）+ RS30 投影自定 [items.json:118-137]。",
    ]

    return {
        "ok": True,
        "target_date": target_d.isoformat(),
        "days_left": days_left,
        "calendar_header": cal.brief_header,
        "is_workday": cal.is_workday,
        "is_makeup_workday": cal.is_makeup_workday,
        "beijing_tail_limit": cal.beijing_tail_limit,
        "auto_generated_at": gen_dt.isoformat(timespec="seconds") if gen_dt else "",
        "auto_age_hours": auto_age_hours,
        "auto_stale": auto_stale,
        "manual_fetched_at": manual_fetched_at,
        "total_orders": len(projected_items),
        "active_orders": len(active_install_items) + len(active_other_items),
        "vendor_install_total": len(install_11),
        "active_vendor_install_total": len(active_install_items),
        "vendor_booked_count": booked_cnt,
        "vendor_signed_count": signed_install_cnt,
        "missing_rooms": missing_rooms,
        "unbooked_install_items": unbooked_install_items,
        "active_unbooked_install_items": active_unbooked_install_items,
        "signed_waiting_install": signed_waiting_install,
        "missing_svc_brand_items": missing_svc_brand_items,
        "install_items": install_11,
        "active_install_items": active_install_items,
        "other_items": self_or_none,
        "active_other_items": active_other_items,
        "all_items": projected_items,
        "cancelled_items": cancelled_items,
        "prerequisites": prerequisites,
    }


def _load_arrival_checked_map(db: SimingDB) -> dict[str, dict[str, Any]]:
    raw = db.get_meta("install_arrival_checked_json")
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _save_arrival_checked_map(db: SimingDB, data: dict[str, dict[str, Any]]) -> None:
    db.set_meta("install_arrival_checked_json", json.dumps(data, ensure_ascii=False))


def _arrival_specific_check_tip(item: dict[str, Any]) -> str:
    name = str(item.get("name") or "")
    iid = str(item.get("id") or "")
    if "热水器" in name or "林内" in name or "rinnai" in iid.lower():
        return "必核外箱铭牌气源为「天然气 12T」（严禁错发液化气），外箱勿自拆"
    if "电视" in name or "85" in name:
        return "严查大屏外箱六面有无叉车压痕/破洞（勿自拆封！必须等师傅上门录像开箱），并核对独立包裹 NB P65 挂架(9000000000000002) 是否到齐"
    if "烟灶" in name or "老板" in name:
        return "核对烟机+灶具两件主箱及止逆阀子件箱数齐全，灶具铭牌确认为天然气"
    if "冰箱" in name:
        return "检查外箱倾倒标/防撞角完好，搬入厨房/餐厅暂存后静置勿通电"
    if "洗烘" in name or "洗衣机" in name:
        return "检查底座泡沫无塌陷、运输螺栓未松脱"
    if "幕布" in name:
        return "核对 120 寸长条包装管无折弯压瘪"
    if "投影" in name:
        return "核对外箱封条与防撞角完好（贵重光学镜头等安装时当面拆封核验）"
    if "科沃斯" in name or "扫地" in name:
        return "核对主机箱与上下水模组配件是否齐全"
    return "核对外箱型号一致、六面无破损受潮并拍照留档（大件勿提前自拆封）"


def mark_arrival_inspected(
    db: SimingDB,
    target_query: str = "all",
    note: str = "",
    items_json_path: Optional[Path] = None,
    auto_json_path: Optional[Path] = None,
    manual_json_path: Optional[Path] = None,
) -> dict[str, Any]:
    """标记到货外箱初验完成（仅记录外箱验收状态，绝不提前关闭 12.10 上门安装待办！）。"""
    proj = build_installation_projection(
        db,
        items_json_path=items_json_path,
        auto_json_path=auto_json_path,
        manual_json_path=manual_json_path,
    )
    if not proj.get("ok"):
        return {"ok": False, "error": str(proj.get("error") or "无法读取清单")}

    checked = _load_arrival_checked_map(db)
    all_items: list[dict[str, Any]] = list(proj.get("all_items") or [])
    q = (target_query or "").strip()
    tokens = [t.strip() for t in re.split(r"[,，、;\s]+", q) if t.strip() and t.strip() not in ("已验外观", "外箱完好", "全部", "all")]

    matched_ids: list[str] = []
    matched_names: list[str] = []
    ts = now_local().isoformat(timespec="seconds")

    for it in all_items:
        if it.get("arrival_code") == "cancelled":
            continue
        iid = str(it["id"])
        name = str(it["name"])
        if not tokens:
            # 默认将当前所有「已签收」单标记为已验外箱；若尚无已签收单则提示
            if it["arrival_code"] == "signed":
                checked[iid] = {"checked_at": ts, "note": note[:120]}
                matched_ids.append(iid)
                matched_names.append(name)
        else:
            if any(tok.lower() in iid.lower() or tok.lower() in name.lower() for tok in tokens):
                checked[iid] = {"checked_at": ts, "note": note[:120]}
                matched_ids.append(iid)
                matched_names.append(name)

    if not matched_ids:
        return {
            "ok": False,
            "error": "当前尚无「已签收」设备可整批标已验（若某件已现场收货，可指明名称如：`已验外观 林内,电视`）。",
        }

    _save_arrival_checked_map(db, checked)
    db.record_human_use("install_plan", "mark_arrival_checked", ref_id=",".join(matched_ids[:3]))
    return {
        "ok": True,
        "count": len(matched_ids),
        "items": matched_names,
        "summary": (
            f"✅ 已登记 {len(matched_ids)} 件设备「外箱初验完好」：{ '、'.join(matched_names) }\n"
            "（仅核销到货外箱检查，12.10 上门安装与通电验收待办保持不变。）"
        ),
    }


def format_arrival_inspection_checklist(
    db: SimingDB,
    items_json_path: Optional[Path] = None,
    auto_json_path: Optional[Path] = None,
    manual_json_path: Optional[Path] = None,
) -> str:
    """S3-1：生成大家电集中到货开箱前「外箱六面/型号/气源/子件」初验清单（化解静置 21 天超 7 天无理由/破损索赔期风险）。"""
    proj = build_installation_projection(
        db,
        items_json_path=items_json_path,
        auto_json_path=auto_json_path,
        manual_json_path=manual_json_path,
    )
    if not proj.get("ok"):
        return f"⚠️ 无法生成到货初验清单：{proj.get('error')}"

    checked = _load_arrival_checked_map(db)
    all_items: list[dict[str, Any]] = [
        it for it in (proj.get("all_items") or []) if it.get("arrival_code") != "cancelled"
    ]
    cancelled_items: list[dict[str, Any]] = list(proj.get("cancelled_items") or [])

    signed_unchecked = [it for it in all_items if it["arrival_code"] == "signed" and it["id"] not in checked]
    already_checked = [it for it in all_items if it["id"] in checked]
    in_transit = [it for it in all_items if it["arrival_code"] != "signed" and it["id"] not in checked]

    lines = [
        f"📦 新房家电集中到货「外箱初验」核对单（距 {proj['target_date']} 集中安装还有 {proj['days_left']} 天）",
        f"• 风险提示：货品到场至 12.10 安装静置约 3 周，务必在签收 48h 内完成「外箱破损/型号/气源/配件包裹」初验拍照，大件切勿自行拆封（等安装师傅上门拆箱录像防推诿）。",
        f"• 当前进度：有效 {len(all_items)} 单 ｜ 已签收待初验 {len(signed_unchecked)} 单 ｜ 已验外箱 {len(already_checked)} 单 ｜ 在途/待发 {len(in_transit)} 单"
        + (f" ｜ 已取消跳过 {len(cancelled_items)} 单" if cancelled_items else ""),
    ]

    if signed_unchecked:
        lines.append("\n【一、已签收 · 请现场核验外箱并拍照】")
        for idx, it in enumerate(signed_unchecked, start=1):
            lines.append(
                f"{idx:2d}. [{it['room']}] {it['name']} — {_arrival_specific_check_tip(it)}"
            )

    if in_transit:
        lines.append("\n【二、运输中 / 待收货（收货当场按此核对）】")
        for it in in_transit:
            lines.append(
                f"• [{it['room']}] {it['name']}（{it['arrival_label']}）— {_arrival_specific_check_tip(it)}"
            )

    if already_checked:
        names_done = "、".join(f"{it['name']}({checked[it['id']]['checked_at'][:10]})" for it in already_checked)
        lines.append(f"\n【三、已完成外箱初验】{names_done}")

    lines.append("\n回执口令：验完拍照后可直接回 `已验外观`（整批核销已签收项）或 `已验外观 林内,电视`（按件核销，不影响 12.10 安装待办）。")
    return "\n".join(lines)


def format_onsite_grouped_checklist(
    db: SimingDB,
    items_json_path: Optional[Path] = None,
    auto_json_path: Optional[Path] = None,
    manual_json_path: Optional[Path] = None,
) -> str:
    """S3-1：生成 12.09 前夜 / 12.10 当天按「施工依赖梯队 + 预约状态 + 服务单号」分组的现场作战单。"""
    proj = build_installation_projection(
        db,
        items_json_path=items_json_path,
        auto_json_path=auto_json_path,
        manual_json_path=manual_json_path,
    )
    if not proj.get("ok"):
        return f"⚠️ 无法生成现场作战单：{proj.get('error')}"

    active_installs = list(proj.get("active_install_items") or [])
    active_others = list(proj.get("active_other_items") or [])
    active_unbooked = list(proj.get("active_unbooked_install_items") or [])
    cancelled_cnt = len(proj.get("cancelled_items") or [])
    canc_s = f"（已取消跳过 {cancelled_cnt} 单）" if cancelled_cnt else ""

    lines = [
        f"🛠️ {proj['target_date']} 新房集中送装「现场分组作战单」（{proj['calendar_header']}）",
        f"• 就绪总览：{len(active_installs)} 台需上门安装设备中，JD/TB 已到货 {proj['vendor_signed_count']}/{len(active_installs)}，人工已确认预约 {proj['vendor_booked_count']}/{len(active_installs)}{canc_s}",
    ]
    if active_unbooked:
        lines.append(
            "⚠️ 尚未在 track 确认预约项："
            + "、".join(active_unbooked)
            + "（改状态唯一入口：https://track.example.com/）"
        )
    if proj["is_workday"]:
        lines.append(
            f"• 物业与交通：{proj['target_date']} 为工作日（本人电车不限行；安装师傅蓝牌车限行 {proj['beijing_tail_limit']}），请确认物业施工放行条、货梯保护与门口留车位。"
        )

    waves: list[tuple[str, list[dict[str, Any]]]] = [
        ("第一梯队 · 水气主干与涉水设备（优先进场核对燃气表 G4 / 减压阀 / 排水三通）", []),
        ("第二梯队 · 厨房橱柜与阳台吊顶联动设备（盯紧开孔防尘与止逆阀密封）", []),
        ("第三梯队 · 客厅与卧室视听环控（空调抽真空 ≥15 分钟 / 85寸电视挂架承重）", []),
    ]
    for it in active_installs:
        nm = it["name"]
        if any(k in nm for k in ("热水器", "林内", "洗烘", "洗衣机", "科沃斯", "扫地", "净水", "马桶")):
            waves[0][1].append(it)
        elif any(k in nm for k in ("烟灶", "老板", "冰箱", "晾衣", "洗碗")):
            waves[1][1].append(it)
        else:
            waves[2][1].append(it)

    for wave_title, w_items in waves:
        if not w_items:
            continue
        lines.append(f"\n【{wave_title}】")
        for it in w_items:
            note_s = f" ｜ 现场核验：{it['install_note']}" if it["install_note"] else ""
            lines.append(
                f"• [{it['room']}] {it['name']} — {it['booking_label']} ｜ 到货：{it['arrival_label']} ｜ {it['contact_record']}{note_s}"
            )

    if active_others:
        other_desc = "、".join(f"[{it['room']}]{it['name']}({it['arrival_label']})" for it in active_others)
        lines.append(f"\n【第四梯队 · 自装/免装点位清点】{other_desc}")

    lines.append("\n提示：每装完验收一台，可在 https://track.example.com/ 更新为「已安装」。")
    return "\n".join(lines)


def ensure_arrival_inspection_wait(db: SimingDB) -> Optional[dict[str, Any]]:
    """若当前日期早于 2026-11-19 19:30 且尚未登记首批家电到货集中初验提醒，幂等种入一条提醒挂起。"""
    target_iso = "2026-11-19T19:30:00+08:00"
    if now_local().isoformat() >= target_iso:
        return None
    if db.get_meta("seeded_arrival_1119_wait") == "1":
        return None
    conn = db.connect()
    try:
        exists = conn.execute(
            "SELECT wait_id FROM waits WHERE status='WAITING' AND (quote LIKE '%到货%初验%' OR next_fire_at LIKE '2026-11-19T19:30%')"
        ).fetchone()
        if exists:
            db.set_meta("seeded_arrival_1119_wait", "1")
            return {"wait_id": str(exists["wait_id"]), "existing": True}
    finally:
        conn.close()

    from siming.waits import add_wait

    w = add_wait(
        db,
        when=target_iso,
        mode="remind",
        strength="explicit",
        quote="首批新房大家电集中到货外箱初验（核气源12T/型号/NB-P65挂架/六面破损拍照）",
        task="发送新房家电到货外箱初验清单（siming install-check --arrival）",
        agent="siming",
        ttl="10d",
    )
    db.set_meta("seeded_arrival_1119_wait", "1")
    return w


def format_installation_checklist(
    db: SimingDB,
    compact: bool = False,
    items_json_path: Optional[Path] = None,
    auto_json_path: Optional[Path] = None,
    manual_json_path: Optional[Path] = None,
) -> str:
    proj = build_installation_projection(
        db,
        items_json_path=items_json_path,
        auto_json_path=auto_json_path,
        manual_json_path=manual_json_path,
    )
    if not proj.get("ok"):
        return f"⚠️ 无法生成送装清单：{proj.get('error')}"

    stale_warn = (
        f"（⚠️ JD/TB 物流快照已滞后 {proj['auto_age_hours']}h）"
        if proj["auto_stale"]
        else ""
    )
    active_installs = list(proj.get("active_install_items") or [])
    active_others = list(proj.get("active_other_items") or [])
    cancelled_cnt = len(proj.get("cancelled_items") or [])
    canc_s = f" ｜ 已取消跳过 {cancelled_cnt} 单" if cancelled_cnt else ""
    header = (
        f"🏠 {proj['target_date']} 新房送装核对单（距今 {proj['days_left']} 天 · {proj['calendar_header']}）{stale_warn}\n"
        f"• 进度总览：有效 {proj['active_orders']} 单，需上门安装 {len(active_installs)} 台 ｜ "
        f"JD/TB 已签收 {proj['vendor_signed_count']}/{len(active_installs)} ｜ "
        f"人工已确认预约 {proj['vendor_booked_count']}/{len(active_installs)}{canc_s}"
    )
    lines = [header]

    if proj["signed_waiting_install"]:
        lines.append("• 已到货待约装：" + "、".join(proj["signed_waiting_install"]))
    active_missing_rooms = [f"{it['name']}({it['id']})" for it in active_installs if it.get("room_missing")]
    if active_missing_rooms:
        lines.append("• 房间待定缺口（items.json room 为空）：" + "、".join(active_missing_rooms))
    if proj["is_workday"]:
        lines.append(
            f"• 日历提醒：{proj['target_date']} 为工作日（本人电车不限行；外地/蓝牌送货车注意高峰限行 {proj['beijing_tail_limit']}），集中送装请提前办妥物业出入许可。"
        )

    if compact:
        lines.append("• 前置关键项：燃气表 G2.5->G4 核验（林内）、橱柜开孔实测（老板 E2S）、85寸电视单独约 NB P65 挂装。")
        lines.append("• 预约状态唯一编辑入口：https://track.example.com/ （查完整 15 单发「查 送装清单」）")
        return "\n".join(lines)

    lines.append(f"\n【一、{len(active_installs)} 台需上门安装设备（区分「JD/TB 到货」与「人工确认预约」）】")
    for idx, it in enumerate(active_installs, start=1):
        note_s = f" ｜ 要点：{it['install_note']}" if it["install_note"] else ""
        lines.append(
            f"{idx:2d}. [{it['room']}] {it['name']} — 到货：{it['arrival_label']} ｜ 预约：{it['booking_label']} ｜ {it['contact_record']}{note_s}"
        )

    if active_others:
        lines.append(f"\n【二、{len(active_others)} 台免安装 / 自装设备】")
        for it in active_others:
            lines.append(f"• [{it['room']}] {it['name']} — 到货：{it['arrival_label']}（{it['booking_label']}）")

    lines.append("\n【三、前置依赖与防踩坑顺序】")
    lines.extend(proj["prerequisites"])
    lines.append("注：预约状态以 https://track.example.com/ 人工确认为唯一真源，司命只读投影不造第二套台账。")
    return "\n".join(lines)
