"""Batch B-1 (`movein_pm`)：12.10 入住总控项目经理 + 装修隐蔽工程按房间留证 + 长期家电售后护照。
1. 送装前后置工序冲突检查：核验「燃气通气/橱柜开孔/吊顶打孔/上下水减压阀/电视挂架」与已预约安装单的冲突，及同日同房间多工种拥挤度；
2. 隐蔽工程与铭牌照片归档：QQ 发图 `存 主卧 水电` / `存 厨房 燃气表` 按房间落盘 `~/.local/share/siming/home_assets/photos/<房间>/` (`0600`) 并建结构化索引；
3. 家电售后护照：融合 `~/buy/track/items.json`、`auto.json`、实付价格与人工备注，随时问 `售后 林内` / `保修 电视` 秒回单号、服务单、气源/挂架规格与排障备忘。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional

from siming import db as _db_mod
from siming.db import SimingDB, iso_now, now_local
from siming.install_plan import build_installation_projection

PREREQ_GATE_LABELS: dict[str, str] = {
    "gas": "燃气表通气/G4升级与零冷水回水",
    "cabinet": "厨房橱柜安装与灶具台面开孔实测",
    "ceiling": "阳台吊顶封板前晾衣机打孔",
    "plumbing": "卫生间干区/阳台上下水三通与减压阀",
    "tv_mount": "NB-P65 电视挂架拆箱到场核验",
}

GATE_ALIASES: dict[str, str] = {
    "燃气": "gas",
    "通气": "gas",
    "gas": "gas",
    "橱柜": "cabinet",
    "台面": "cabinet",
    "开孔": "cabinet",
    "cabinet": "cabinet",
    "吊顶": "ceiling",
    "阳台吊顶": "ceiling",
    "ceiling": "ceiling",
    "上下水": "plumbing",
    "水路": "plumbing",
    "减压阀": "plumbing",
    "plumbing": "plumbing",
    "挂架": "tv_mount",
    "电视挂架": "tv_mount",
    "p65": "tv_mount",
    "tv_mount": "tv_mount",
}

WARRANTY_BASELINES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"(洗烘|洗衣机|海尔)"), "整机 3 年 · 电机/主要部件 3–10 年（海尔官方联保）"),
    (re.compile(r"(林内|热水器)"), "整机 3 年包修（林内全国联保，须用原厂或合规燃气管阀）"),
    (re.compile(r"(老板|烟机|灶具|烟灶|洗碗机)"), "烟灶整机 5 年 · 洗碗机整机 3 年（老板电器官方售后）"),
    (re.compile(r"(小米|电视|晾衣机)"), "电视整机 1 年 / 屏幕及主板 3 年 · 晾衣机整机 2 年"),
    (re.compile(r"(极米|投影|幕布)"), "投影主机 2 年 · 幕布 1 年"),
    (re.compile(r"(科沃斯|扫地|扫拖)"), "整机 2 年（上下水模块随整机联保）"),
    (re.compile(r"(空调)"), "整机 6 年 · 压缩机 6–10 年"),
]


def _assets_root(db: SimingDB) -> Path:
    root = db.paths.data_dir / "home_assets"
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    return root


def _manifest_path(db: SimingDB) -> Path:
    return _assets_root(db) / "manifest.json"


def _load_manifest(db: SimingDB) -> dict[str, Any]:
    p = _manifest_path(db)
    if not p.is_file():
        return {"photos": [], "appliance_notes": {}}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("photos", [])
            data.setdefault("appliance_notes", {})
            return data
    except Exception:
        pass
    return {"photos": [], "appliance_notes": {}}


def _save_manifest(db: SimingDB, data: dict[str, Any]) -> None:
    p = _manifest_path(db)
    tmp = p.with_suffix(f".tmp.{os.getpid()}")
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(data, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp, p)


def load_prereq_gates(db: SimingDB) -> dict[str, bool]:
    raw = db.get_meta("movein_prereq_gates") or "{}"
    try:
        parsed = json.loads(raw)
    except Exception:
        parsed = {}
    return {k: bool(parsed.get(k, False)) for k in PREREQ_GATE_LABELS}


def set_prereq_gate(db: SimingDB, gate_raw: str, ready: bool = True) -> dict[str, Any]:
    key = GATE_ALIASES.get((gate_raw or "").strip().lower()) or GATE_ALIASES.get((gate_raw or "").strip())
    if not key or key not in PREREQ_GATE_LABELS:
        opts = "、".join(f"`{k}`({v})" for k, v in PREREQ_GATE_LABELS.items())
        return {"ok": False, "error": f"未识别的前置工序「{gate_raw}」。可选：{opts}（或发 `工序就绪 燃气|橱柜|吊顶|上下水|挂架`）"}
    gates = load_prereq_gates(db)
    gates[key] = ready
    db.set_meta("movein_prereq_gates", json.dumps(gates, ensure_ascii=False))
    db.write_audit("A1", "movein_gate_update", {"gate": key, "ready": ready})
    st_s = "已就绪 ✅" if ready else "待完成 ⏳"
    return {"ok": True, "gate": key, "ready": ready, "summary": f"🏗️ 已更新新房前置工序「{PREREQ_GATE_LABELS[key]}」→ **{st_s}**"}


def check_install_conflicts(
    db: SimingDB,
    items_json_path: Optional[Path] = None,
    auto_json_path: Optional[Path] = None,
    manual_json_path: Optional[Path] = None,
) -> dict[str, Any]:
    """检查 12.10 送装计划的前后置依赖冲突、货未到先约风险与同房间多工种拥挤度。"""
    proj = build_installation_projection(
        db,
        items_json_path=items_json_path,
        auto_json_path=auto_json_path,
        manual_json_path=manual_json_path,
    )
    if not proj.get("ok"):
        return {"ok": False, "reply": f"❌ 无法加载家电清单：{proj.get('error')}"}

    gates = load_prereq_gates(db)
    conflicts: list[str] = []
    warnings: list[str] = []
    room_day_groups: dict[tuple[str, str], list[str]] = defaultdict(list)

    all_items: list[dict[str, Any]] = []
    for k in ("all_items", "install_items", "items", "projected_items"):
        if isinstance(proj.get(k), list):
            all_items = list(proj[k])
            break
    if not all_items:
        # 若 projection 未直接暴露列表则从 items.json 兜底读一次
        p_it = items_json_path or (Path.home() / "buy" / "track" / "items.json")
        if p_it.is_file():
            doc = json.loads(p_it.read_text(encoding="utf-8"))
            all_items = [
                {
                    "name": str(x.get("name") or ""),
                    "room": str(x.get("room") or "未填房间"),
                    "needs_vendor_install": str(x.get("install") or "") in ("brand", "jd"),
                    "booking_code": "unbooked",
                    "booking_label": "",
                    "arrival_code": "unknown",
                    "intent_date": str(x.get("installDate") or proj.get("target_date") or "2026-12-10"),
                }
                for x in (doc.get("items") or [])
            ]

    today_iso = _db_mod.now_local().date().isoformat()
    for it in all_items:
        if it.get("arrival_code") == "cancelled":
            continue
        if not it.get("needs_vendor_install"):
            continue
        name = str(it.get("name") or "")
        room = str(it.get("room") or "未填房间")
        b_code = str(it.get("booking_code") or "unbooked")
        b_label = str(it.get("booking_label") or "")
        a_code = str(it.get("arrival_code") or "unknown")
        m_dt = re.search(r"(20\d{2}-\d{2}-\d{2})", b_label)
        sched_date = m_dt.group(1) if m_dt else str(it.get("intent_date") or proj["target_date"])

        if b_code != "installed":
            room_day_groups[(sched_date, room)].append(name)

        if b_code == "booked":
            if any(k in name for k in ("林内", "热水器", "烟灶", "燃气灶")) and not gates["gas"]:
                conflicts.append(f"🔴 **燃气前置冲突**：`{name}` 已约上门（{sched_date}），但「{PREREQ_GATE_LABELS['gas']}」尚未标就绪，师傅上门可能无法点火试机！")
            if any(k in name for k in ("烟灶", "老板", "洗碗机", "净水")) and not gates["cabinet"]:
                conflicts.append(f"🔴 **橱柜前置冲突**：`{name}` 已约上门（{sched_date}），但「{PREREQ_GATE_LABELS['cabinet']}」未标就绪！")
            if "晾衣" in name and not gates["ceiling"]:
                conflicts.append(f"🔴 **吊顶工序冲突**：`{name}` 已约上门，请确认阳台吊顶打孔已完成！")
            if any(k in name for k in ("科沃斯", "洗烘", "洗衣")) and not gates["plumbing"]:
                warnings.append(f"🟠 **水路核验提醒**：`{name}` 上门前请确认「{PREREQ_GATE_LABELS['plumbing']}」到位。")
            if ("电视" in name or "85" in name) and not gates["tv_mount"]:
                warnings.append(f"🟠 **挂架齐套提醒**：`{name}` 上墙前请确认 NB-P65 挂架已在客厅拆箱备妥。")
            if a_code != "signed" and sched_date <= today_iso:
                conflicts.append(f"🔴 **货未签收冲突**：`{name}` 预约日为 {sched_date}，但物流尚未显示真签收！")

    for (s_date, rm), names in sorted(room_day_groups.items()):
        if rm != "未填房间" and len(names) >= 3:
            warnings.append(
                f"🟠 **空间拥挤预警（{s_date} · {rm} 共 {len(names)} 组）**：{'、'.join(names[:4])} 同处 `{rm}`，建议拆成上午/下午两拨防工种打架。"
            )

    tail_lim = str(proj.get("beijing_tail_limit") or "不限行")
    lines = [
        f"🏗️ **12.10 新房送装总控 PM 冲突体检**（目标日 `{proj['target_date']}` · 倒计时 **{proj['days_left']}** 天）",
        f"• 🚚 车辆提示：你的纯电车**不限行**；当日北京蓝牌货车高峰限行尾号 **`{tail_lim}`**（约早晚师傅带货车时留意）。",
    ]
    gate_badges = " ｜ ".join(
        f"{'✅' if ok else '⏳'}{PREREQ_GATE_LABELS[gk][:4]}" for gk, ok in gates.items()
    )
    lines.append(f"• 🔩 五大前置工序：{gate_badges}（完成后发 `工序就绪 燃气|橱柜|吊顶|上下水|挂架`）")

    if conflicts:
        lines.extend(conflicts)
    if warnings:
        lines.extend(warnings)
    if not conflicts and not warnings:
        lines.append("• 🟢 **当前无硬性工序冲突**！各房间安装梯队排布合理。")

    db.record_human_use("movein_pm", "conflict_check", ref_id=proj["target_date"])
    return {
        "ok": True,
        "conflicts": conflicts,
        "warnings": warnings,
        "gates": gates,
        "reply": "\n".join(lines),
    }


def archive_home_photo(
    db: SimingDB,
    src_path: Path,
    room: str,
    tag: str = "现场留证",
    note: str = "",
) -> dict[str, Any]:
    """将隐蔽工程水电走向图、开箱照或设备铭牌照按房间加密权限 (`0600`) 归档入 `home_assets/photos/<房间>/`。"""
    db.init_db()
    if not src_path.is_file():
        return {"ok": False, "error": f"未找到待归档图片：{src_path}"}

    clean_room = re.sub(r"[^\w\u4e00-\u9fff-]+", "", (room or "未分房间").strip())[:20] or "未分房间"
    clean_tag = re.sub(r"[^\w\u4e00-\u9fff-]+", "_", (tag or "现场留证").strip())[:32] or "现场留证"
    ext = src_path.suffix.lower() or ".jpg"
    raw_bytes = src_path.read_bytes()
    sha10 = hashlib.sha256(raw_bytes).hexdigest()[:10]
    stamp = now_local().strftime("%Y%m%d-%H%M%S")

    room_dir = _assets_root(db) / "photos" / clean_room
    room_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(room_dir, 0o700)
    dest_path = room_dir / f"{stamp}-{clean_tag}-{sha10[:6]}{ext}"

    fd = os.open(str(dest_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, raw_bytes)
    finally:
        os.close(fd)

    mf = _load_manifest(db)
    entry = {
        "id": f"IMG-{sha10}",
        "room": clean_room,
        "tag": clean_tag,
        "note": note.strip()[:240],
        "path": str(dest_path),
        "created_at": iso_now(),
    }
    mf["photos"] = [p for p in mf["photos"] if p.get("id") != entry["id"]]
    mf["photos"].append(entry)
    _save_manifest(db, mf)
    db.record_human_use("movein_pm", "archive_photo", ref_id=entry["id"])

    return {
        "ok": True,
        "entry": entry,
        "summary": (
            f"📸 已归档新房留证照片 [`{entry['id']}`]\n"
            f"• 房间：**{clean_room}** ｜ 标签：**{clean_tag}**" + (f" ｜ 备注：{note}" if note else "") + "\n"
            f"• 路径：`{dest_path}`（日后随时问 `新房照片 {clean_room}` 秒调原图）"
        ),
    }


def list_home_photos(db: SimingDB, keyword: str = "") -> dict[str, Any]:
    mf = _load_manifest(db)
    photos = list(reversed(mf.get("photos") or []))
    kw = (keyword or "").strip().lower()
    if kw:
        photos = [
            p
            for p in photos
            if kw in f"{p.get('room', '')} {p.get('tag', '')} {p.get('note', '')}".lower()
        ]
    if not photos:
        return {
            "ok": True,
            "photos": [],
            "reply": f"📷 暂无匹配的装修/家电归档照片{f'（筛选：{keyword}）' if keyword else ''}。在 QQ 带图发 `存 主卧 水电` 或 `存 铭牌 林内` 即可自动建档！",
        }
    lines = [f"📷 **新房隐蔽工程与铭牌照片库**（共匹配 {len(photos)} 张）"]
    for p in photos[:12]:
        n_s = f" · {p['note']}" if p.get("note") else ""
        lines.append(f"• [{p['room']} · **{p['tag']}**] `{p['path']}`（{str(p.get('created_at', ''))[:10]}{n_s}）")
    return {"ok": True, "photos": photos, "reply": "\n".join(lines)}


def save_appliance_note(db: SimingDB, target_kw: str, note_text: str) -> dict[str, Any]:
    """为某台家电补充铭牌序列号、官方报修电话或说明书排障页码备忘。"""
    kw = (target_kw or "").strip()
    nt = (note_text or "").strip()
    if not kw or not nt:
        return {"ok": False, "error": "请提供设备名称与售后备注内容"}
    mf = _load_manifest(db)
    notes_map = mf.setdefault("appliance_notes", {})
    existing = list(notes_map.get(kw) or [])
    existing.append({"note": nt[:300], "at": iso_now()[:10]})
    notes_map[kw] = existing[-8:]
    _save_manifest(db, mf)
    return {"ok": True, "summary": f"📘 已写入「{kw}」售后护照备忘：{nt[:120]}"}


def _infer_warranty_desc(name: str) -> str:
    for pat, desc in WARRANTY_BASELINES:
        if pat.search(name):
            return desc
    return "整机 1–3 年（以平台电子发票与官方三包凭证为准）"


def query_appliance_passport(
    db: SimingDB,
    keyword: str = "",
    items_json_path: Optional[Path] = None,
    price_orders_path: Optional[Path] = None,
) -> dict[str, Any]:
    """查询单台或全部新房家电售后护照（订单号、实付价、服务单、三包期、前置规格与关联铭牌照）。"""
    i_path = items_json_path or (Path.home() / "buy" / "track" / "items.json")
    p_path = price_orders_path or (Path.home() / ".hermes/scripts/buy-price-watch/orders.json")
    if not i_path.is_file():
        return {"ok": False, "reply": f"❌ 未找到家电台账真源：{i_path}"}

    doc = json.loads(i_path.read_text(encoding="utf-8"))
    paid_by_order: dict[str, Any] = {}
    if p_path.is_file():
        try:
            p_doc = json.loads(p_path.read_text(encoding="utf-8"))
            rows_p = p_doc.get("orders") if isinstance(p_doc, dict) else p_doc
            for r in rows_p or []:
                if isinstance(r, dict) and r.get("order_id"):
                    paid_by_order[str(r["order_id"])] = r.get("paid_price") or r.get("paid")
        except Exception:
            pass

    mf = _load_manifest(db)
    photos = mf.get("photos") or []
    app_notes = mf.get("appliance_notes") or {}

    kw = re.sub(r"^(?:售后|保修|护照|说明书|家电护照)\s*", "", (keyword or "").strip()).strip().lower()
    matched: list[dict[str, Any]] = []

    for it in doc.get("items") or []:
        name = str(it.get("name") or "")
        room = str(it.get("room") or "未填房间")
        cat = str(it.get("cat") or "")
        hay = f"{name} {room} {cat} {it.get('id', '')}".lower()
        if kw and kw not in hay:
            continue
        oid = str(it.get("order") or "")
        svcs = [str(s) for s in (it.get("svc") or []) if s]
        paid = paid_by_order.get(oid) or it.get("price")
        rel_photos = [
            p["path"]
            for p in photos
            if any(tok and tok in f"{p.get('room','')} {p.get('tag','')} {p.get('note','')}".lower() for tok in (name[:3].lower(), room.lower()))
        ]
        custom_notes: list[str] = []
        for nk, nlist in app_notes.items():
            if nk.lower() in hay or name[:3].lower() in nk.lower():
                custom_notes.extend(f"{x['note']} ({x['at']})" for x in (nlist or []))

        matched.append(
            {
                "id": str(it.get("id") or ""),
                "name": name,
                "room": room,
                "platform": str(it.get("platform") or "京东"),
                "order": oid,
                "paid": paid,
                "svc": svcs,
                "warranty": _infer_warranty_desc(name),
                "install_note": str(it.get("installNote") or ""),
                "photos": rel_photos[:3],
                "custom_notes": custom_notes,
            }
        )

    if not matched:
        return {"ok": False, "reply": f"未在 15 单家电台账中找到匹配「{keyword}」的设备（可试：`售后 林内`、`售后 洗烘`、`售后 电视`）。"}

    db.record_human_use("movein_pm", "passport_query", ref_id=kw or "all")
    if not kw and len(matched) > 5:
        lines = [f"🛡️ **新房 15 单家电售后护照总览**（共 {len(matched)} 件 · 查单件详卡发 `售后 <设备名>`）"]
        for m in matched:
            svc_s = f"服务单 {','.join(m['svc'])}" if m["svc"] else "联系平台订单客服"
            lines.append(f"• **{m['name']}**（{m['room']}）— 单号 `{m['order'] or '-'}` · {svc_s} · {m['warranty'].split('·')[0].strip()}")
        return {"ok": True, "items": matched, "reply": "\n".join(lines)}

    lines = [f"🛡️ **家电售后与排障护照**（匹配 {len(matched)} 件）"]
    for m in matched[:5]:
        paid_s = f" · 实付 ¥{m['paid']}" if m["paid"] else ""
        svc_s = "、".join(f"`{s}`" for s in m["svc"]) if m["svc"] else "订单页直连品牌/京东客服"
        lines.append(f"\n### 🔹 {m['name']}（房间：{m['room']}）")
        lines.append(f"• 购买凭证：{m['platform']} 订单号 `{m['order'] or '-'}`{paid_s} ｜ 服务单：{svc_s}")
        lines.append(f"• 保修基线：{m['warranty']}")
        if m["install_note"]:
            lines.append(f"• 规格与安装要点：{m['install_note']}")
        if m["custom_notes"]:
            lines.append("• 排障/铭牌备忘：" + "；".join(m["custom_notes"]))
        if m["photos"]:
            lines.append("• 关联现场/铭牌图：" + "、".join(f"`{p}`" for p in m["photos"]))
    lines.append("\n💡 补充序列号/客服电话或说明书页码可发：`登记售后 <设备名> <备忘内容>`")
    return {"ok": True, "items": matched, "reply": "\n".join(lines)}
