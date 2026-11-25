"""主动提醒前的周边信息查齐（deco 尺寸 / 非补班休息日 / 售后）与个人硬规矩冲突检测。"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import date
from pathlib import Path
from typing import Any, Optional

from siming.cn_calendar import (
    day_info,
    upcoming_makeup_workdays,
    upcoming_non_makeup_rest_days,
)
from siming.config import load_policy
from siming.db import SimingDB

BRAND_SERVICE_PHONES = {
    "西门子": "400-889-9999",
    "老板": "9510-5855",
    "林内": "400-699-0606",
    "海尔": "400-699-9999",
    "科沃斯": "400-886-8888",
    "小米": "400-100-5678",
    "米家": "400-100-5678",
    "极米": "400-002-8960",
    "格力": "400-836-5315",
    "凯度": "400-882-6628",
    "绿联": "400-885-3838",
    "拓竹": "400-099-0798",
    "京东": "950618",
}

DIMENSION_HINT_RE = re.compile(
    r"(\d{2,4}\s*(?:mm|CM|cm|毫米|公分)|预留|开孔|插座|水口|台面|净宽|净深|净高|吊装|减压阀|G2\.5|G4|承重|挂架|止逆阀|回水管)"
)

# 精确品类锚点与跨品类黑名单：根治空调配浴霸、洗衣机配100寸电视、电视配烟机止逆阀等串品
_CATEGORY_RULES: tuple[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]], ...] = (
    # (item_triggers, required_anchors, forbidden_unless_anchor_present)
    (("空调", "格力", "云佳"), ("空调", "格力", "云佳", "孔位", "外机", "抽真空", "16A"), ("浴霸", "zigbee", "电视", "烟灶", "止逆阀", "晾衣")),
    (("洗衣机", "洗烘", "壁挂", "海尔", "西门子"), ("洗衣机", "洗烘", "壁挂", "海尔", "西门子", "上下水", "承重墙"), ("电视", "投影", "幕布", "浴霸", "100寸", "85寸")),
    (("电视", "85寸", "Mini LED", "小米 S Pro"), ("电视", "挂架", "P65", "电视墙", "小米", "85"), ("烟灶", "烟机", "止逆阀", "晾衣", "浴霸", "洗衣机")),
    (("烟灶", "烟机", "灶具", "老板", "E2S"), ("烟灶", "烟机", "灶具", "老板", "E2S", "止逆阀", "橱柜开孔", "台面开孔"), ("电视", "投影", "幕布", "浴霸", "晾衣")),
    (("热水器", "林内", "R32F"), ("热水器", "林内", "R32F", "G2.5", "G4", "零冷水", "回水管", "天然气"), ("电视", "投影", "幕布", "浴霸", "空调")),
    (("科沃斯", "扫地", "T90S"), ("科沃斯", "扫地", "T90S", "上下水", "减压阀", "卫生间干区"), ("电视", "空调", "烟灶", "浴霸")),
    (("幕布", "投影", "极米", "RS30"), ("幕布", "投影", "极米", "RS30", "120寸", "吊装"), ("烟灶", "止逆阀", "洗衣机", "浴霸", "热水器")),
    (("晾衣机", "晾衣架", "米家"), ("晾衣", "吊顶", "阳台", "米家"), ("电视", "烟灶", "止逆阀", "浴霸", "洗衣机")),
)


def _resolve_deco_root() -> Path:
    ov = os.environ.get("SIMING_DECO_ROOT")
    if ov is not None:
        return Path(ov)
    return Path.home() / "Work" / "deco"


def _resolve_buy_root() -> Path:
    ov = os.environ.get("SIMING_BUY_ROOT")
    if ov is not None:
        return Path(ov)
    return Path.home() / "buy"


def _load_exact_track_item(
    buy_root: Path, item_id: str = "", item_name: str = ""
) -> tuple[Optional[dict[str, Any]], str]:
    """从 ~/buy/track/items.json 按精确 item_id / 名称读取单件真源与全局集中安装日。"""
    items_file = buy_root / "track" / "items.json"
    if not items_file.is_file():
        return None, ""
    try:
        doc = json.loads(items_file.read_text(encoding="utf-8"))
    except Exception:
        return None, ""
    pref_date = str((doc.get("preferences") or {}).get("install_date") or "").strip()
    target_id = (item_id or "").strip().lower()
    target_name = (item_name or "").strip().lower()
    for it in doc.get("items") or []:
        if not isinstance(it, dict):
            continue
        iid = str(it.get("id") or "").strip().lower()
        nm = str(it.get("name") or "").strip().lower()
        if target_id and iid == target_id:
            return it, pref_date
        if target_name and (nm == target_name or (len(target_name) >= 4 and (target_name in nm or nm in target_name))):
            return it, pref_date
    return None, pref_date


def _select_category_rule(item_name: str) -> Optional[tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]]:
    low = (item_name or "").lower()
    for rule in _CATEGORY_RULES:
        triggers = rule[0]
        if any(t.lower() in low for t in triggers):
            return rule
    return None


def lookup_deco_snippets(
    keywords: list[str],
    db: Optional[SimingDB] = None,
    max_hits: int = 3,
    *,
    item_id: str = "",
    item_name: str = "",
) -> list[str]:
    """按 item_id/精确品类锚点只读检索 ~/buy/track/items.json 与 ~/Work/deco/plans/，严防跨品类串品。"""
    deco_root = _resolve_deco_root()
    buy_root = _resolve_buy_root()

    hits: list[str] = []
    exact_item, _pref_date = _load_exact_track_item(buy_root, item_id=item_id, item_name=item_name)
    if exact_item:
        parts: list[str] = []
        if exact_item.get("room"):
            parts.append(f"房间:{exact_item['room']}")
        if exact_item.get("installDate"):
            parts.append(f"计划安装:{exact_item['installDate']}")
        if exact_item.get("installNote"):
            parts.append(str(exact_item["installNote"])[:90])
        if parts:
            hits.append(f"buy/track/items.json(id={exact_item.get('id')}) — " + " · ".join(parts))

    plans_dir = deco_root / "plans"
    search_roots = [str(p) for p in (plans_dir,) if p.is_dir()]
    if not search_roots and deco_root.is_dir():
        # 测试夹具若直接在 deco_root 下放 md 文件，只扫顶层白名单 md，不扫历史/无关目录
        search_roots = [str(deco_root)]

    cat_rule = _select_category_rule(item_name or " ".join(keywords))
    if cat_rule is not None:
        search_kws = list(cat_rule[1])[:6]
        forbidden = [f.lower() for f in cat_rule[2]]
        required_anchors = [a.lower() for a in cat_rule[1]]
    else:
        # 剔除容易串品的纯规格数字/通用后缀词
        search_kws = [
            k.strip()
            for k in keywords
            if len(k.strip()) >= 2
            and not re.fullmatch(r"[\d.]+(?:匹|kg|寸|L|W|Pro|Mini|LED)?", k.strip(), re.IGNORECASE)
        ][:4]
        forbidden = []
        required_anchors = [k.lower() for k in search_kws]

    if not search_roots or not search_kws:
        return hits[:max_hits]

    pattern = "|".join(re.escape(k) for k in search_kws)
    try:
        proc = subprocess.run(
            [
                "rg",
                "-n",
                "--no-heading",
                "-i",
                "--glob",
                "*.{md,txt}",
                "--glob",
                "!**/v1/**",
                "--glob",
                "!**/archive/**",
                "--max-count",
                "8",
                pattern,
                *search_roots,
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except Exception:
        return hits[:max_hits]

    scored: list[tuple[int, str]] = []
    deco_prefix = str(deco_root.parent).rstrip("/") + "/"
    for raw_line in (proc.stdout or "").splitlines():
        parts = raw_line.split(":", 2)
        if len(parts) < 3:
            continue
        fpath, lineno, content = parts[0], parts[1], parts[2].strip()
        if not content or len(content) > 220:
            continue
        low_c = content.lower()
        # 必须显式命中本品类核心锚点，且不得出现跨品类污染词
        if required_anchors and not any(a in low_c for a in required_anchors):
            continue
        if forbidden and any(fb in low_c for fb in forbidden):
            continue
        home_str = str(Path.home())
        rel = fpath.replace(deco_prefix, "").replace(f"{home_str}/Work/", "").replace(f"{home_str}/", "~/")
        score = 3 if DIMENSION_HINT_RE.search(content) else 1
        if item_name and any(b in content for b in BRAND_SERVICE_PHONES if b in item_name):
            score += 2
        scored.append((score, f"{rel}:{lineno} — {content[:110]}"))

    scored.sort(key=lambda x: -x[0])
    seen = set(hits)
    for _sc, item in scored:
        if len(hits) >= max_hits:
            break
        if item not in seen:
            seen.add(item)
            hits.append(item)
    return hits[:max_hits]


def enrich_appliance_arrival(
    name: str,
    room: str = "",
    install_note: str = "",
    platform: str = "",
    svc_orders: Optional[list[Any]] = None,
    db: Optional[SimingDB] = None,
    *,
    item_id: str = "",
    install_date: str = "",
) -> str:
    """为家电/主材到货约安装拼装精准上下文：item_id 精确档案 + 已定 12.10 安装日（或非补班休息日）+ 售后电话。"""
    buy_root = _resolve_buy_root()
    exact_item, pref_install_date = _load_exact_track_item(buy_root, item_id=item_id, item_name=name)
    eff_install_date = (
        (install_date or "").strip()
        or str((exact_item or {}).get("installDate") or "").strip()
        or pref_install_date
    )
    eff_note = (install_note or "").strip() or str((exact_item or {}).get("installNote") or "").strip()
    eff_room = (room or "").strip() or str((exact_item or {}).get("room") or "").strip()

    tokens = [t for t in re.split(r"[\s/（()）#+-]+", name) if len(t) >= 2]
    deco_hits = lookup_deco_snippets(
        tokens[:4], db=db, max_hits=2, item_id=item_id, item_name=name
    )

    phone = ""
    for brand, num in BRAND_SERVICE_PHONES.items():
        if brand in name or brand in eff_note:
            phone = f"{brand}售后 {num}"
            break
    if not phone and platform == "jd":
        phone = f"京东客服 {BRAND_SERVICE_PHONES['京东']}"

    lines: list[str] = []
    if eff_room:
        lines.append(f"安装房间：{eff_room}")
    if eff_note:
        lines.append(f"安装要点：{eff_note}")
    if deco_hits:
        lines.append("关联档案：" + " | ".join(deco_hits))

    if eff_install_date:
        lines.append(f"计划安装日：{eff_install_date}（已锁定集中送装日，按该日约师傅即可，无需另约近期周末）")
    else:
        rest_days = upcoming_non_makeup_rest_days(
            horizon_days=14, paths=db.paths if db else None
        )
        makeups = upcoming_makeup_workdays(
            horizon_days=14, paths=db.paths if db else None
        )
        rest_str = (
            "、".join(f"{d.day.strftime('%m-%d')}({d.weekday_cn})" for d in rest_days[:4])
            or "近期无休息日"
        )
        makeup_warn = (
            "；避坑："
            + "、".join(f"{m.day.strftime('%m-%d')}({m.weekday_cn}补班)" for m in makeups)
            if makeups
            else ""
        )
        lines.append(f"可约休息日（已排掉补班）：{rest_str}{makeup_warn}")

    svc_strs = [
        str(s.get("id") or s.get("svcId") or s.get("orderId") or s.get("name") or s) if isinstance(s, dict) else str(s)
        for s in (svc_orders or [])
        if s
    ]
    svc_part = f"（服务单 {', '.join(svc_strs)}）" if svc_strs else ""
    if phone or svc_part:
        lines.append(f"联络方式：{phone}{svc_part}".strip())
    return "\n".join(lines)


def detect_hard_rule_conflicts(
    text: str,
    scheduled_date: Optional[date] = None,
    db: Optional[SimingDB] = None,
) -> list[dict[str, str]]:
    """个人硬规矩冲突检测：平时不碎嘴，仅在命中明确硬约束时产出冲突条目。"""
    conflicts: list[dict[str, str]] = []
    paths = db.paths if db else None
    policy = load_policy(paths)
    guard = policy["guardrails"]

    check_dates: list[date] = []
    if scheduled_date:
        check_dates.append(scheduled_date)
    for m in re.finditer(r"(20\d{2})-(\d{1,2})-(\d{1,2})", text):
        try:
            check_dates.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            pass

    if re.search(r"(预约|安装|上门|送货|挂装|量尺|复尺|看房|挂号)", text):
        for d in check_dates:
            info = day_info(d, paths)
            if info.is_makeup_workday:
                conflicts.append(
                    {
                        "rule_id": "conflict_makeup_workday",
                        "message": f"日期冲突：{d.isoformat()}（{info.weekday_cn}）是国务院调休补班工作日（{info.holiday_name}），注意家里是否有人接安装/上门。",
                    }
                )

    if re.search(
        r"(桌游|board\s*game|KS\s*众筹|大盒|All[-\s]*in)", text, re.IGNORECASE
    ) and re.search(r"(下单|买了|新入|收货|到货)", text):
        boxes = guard.get("boardgame_cabinet_boxes_limit", 571)
        vol = guard.get("boardgame_cabinet_volume_m3_limit", 3.0)
        conflicts.append(
            {
                "rule_id": "conflict_boardgame_volume",
                "message": f"柜体容量提醒：deco 桌游柜设计基线为 {boxes} 盒 / {vol}m³，新入大盒建议顺手核对剩余层高与深度。",
            }
        )

    if re.search(r"(涂鸦\s*Wi-?Fi|仅支持云端|专有云网关|必须连外网)", text, re.IGNORECASE):
        conflicts.append(
            {
                "rule_id": "conflict_smart_home_protocol",
                "message": "全屋智能规矩冲突：deco 方案定死「本地 Zigbee / Thread / Matter / ESPHome 优先，断外网可用」，该设备疑似纯云端 Wi-Fi 依赖。",
            }
        )

    if "127.0.0.1:9222" in text:
        conflicts.append(
            {
                "rule_id": "conflict_port_9222",
                "message": "端口防呆：本机 aTrust 曾抢占 IPv4 127.0.0.1:9222（现已迁 9232），采集器连接前请用 /json/version 核对监听者是主 Chromium 而非 aTrust（UA 含 aTrustTray）。",
            }
        )

    return conflicts
