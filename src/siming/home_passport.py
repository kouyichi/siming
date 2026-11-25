"""W2-6 (`home_passport`)：家电与住宅全周期护照（水电气网物业缴费周期守卫、15 件家电固定资产折旧估值同步 Sure、耗材寿命追踪、故障码秒查保修与排障）。"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta
from typing import Any, Optional

from siming import db as _db_mod
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, logical_today, parse_iso
from siming.modules import is_module_enabled
from siming.push import push_message

UTILITY_LABELS: dict[str, str] = {
    "water": "水费",
    "electric": "电费",
    "gas": "燃气费",
    "broadband": "宽带费",
    "property": "物业费",
}

DEFAULT_APPLIANCE_CATALOG: list[dict[str, Any]] = [
    {"id": "app-01", "name": "格力 客厅柜机空调", "brand": "gree", "purchase_price": 7800.0, "purchase_date": "2026-11-11", "useful_life_years": 10},
    {"id": "app-02", "name": "格力 主卧挂机空调", "brand": "gree", "purchase_price": 3600.0, "purchase_date": "2026-11-11", "useful_life_years": 10},
    {"id": "app-03", "name": "格力 次卧挂机空调", "brand": "gree", "purchase_price": 3200.0, "purchase_date": "2026-11-11", "useful_life_years": 10},
    {"id": "app-04", "name": "林内 R32F 零冷水燃气热水器", "brand": "rinnai", "purchase_price": 5600.0, "purchase_date": "2026-12-12", "useful_life_years": 8},
    {"id": "app-05", "name": "老板 双子星 E2S 烟灶套装", "brand": "robam", "purchase_price": 6200.0, "purchase_date": "2026-11-11", "useful_life_years": 8},
    {"id": "app-06", "name": "美的 晶焰万向 S67Pro 洗碗机", "brand": "midea", "purchase_price": 4900.0, "purchase_date": "2026-11-11", "useful_life_years": 8},
    {"id": "app-07", "name": "海尔 零嵌十字门冰箱", "brand": "haier", "purchase_price": 6800.0, "purchase_date": "2026-11-11", "useful_life_years": 10},
    {"id": "app-08", "name": "海尔 洗烘一体套装", "brand": "haier", "purchase_price": 7400.0, "purchase_date": "2026-11-11", "useful_life_years": 8},
    {"id": "app-09", "name": "科沃斯 T90S Pro 上下水扫拖机器人", "brand": "ecovacs", "purchase_price": 4600.0, "purchase_date": "2026-11-11", "useful_life_years": 5},
    {"id": "app-10", "name": "小米 85 英寸 MiniLED 电视", "brand": "xiaomi", "purchase_price": 5500.0, "purchase_date": "2026-11-11", "useful_life_years": 8},
    {"id": "app-11", "name": "小米 智能电动晾衣机", "brand": "xiaomi", "purchase_price": 1299.0, "purchase_date": "2026-11-11", "useful_life_years": 6},
    {"id": "app-12", "name": "极米 激光投影与幕布", "brand": "xgimi", "purchase_price": 6400.0, "purchase_date": "2026-11-11", "useful_life_years": 6},
    {"id": "app-13", "name": "小米 厨下反渗透净水器", "brand": "xiaomi", "purchase_price": 1999.0, "purchase_date": "2026-11-11", "useful_life_years": 6},
    {"id": "app-14", "name": "新风除醛净化主机", "brand": "xiaomi", "purchase_price": 3400.0, "purchase_date": "2026-11-11", "useful_life_years": 8},
    {"id": "app-15", "name": "嵌入式微蒸烤一体机", "brand": "robam", "purchase_price": 4200.0, "purchase_date": "2026-11-11", "useful_life_years": 8},
]

BRAND_HOTLINES: dict[str, dict[str, Any]] = {
    "gree": {"display": "格力 (Gree)", "hotline": "400-836-5315", "default_warranty_years": 6, "order_ref": "ORD-HOME-GREE-AC"},
    "rinnai": {"display": "林内 (Rinnai)", "hotline": "400-699-0606", "default_warranty_years": 3, "order_ref": "ORD-HOME-RINNAI-WH"},
    "robam": {"display": "老板电器 (Robam)", "hotline": "9510-5855", "default_warranty_years": 5, "order_ref": "ORD-HOME-ROBAM-HOOD"},
    "haier": {"display": "海尔 (Haier)", "hotline": "400-699-9999", "default_warranty_years": 3, "order_ref": "ORD-HOME-HAIER-APP"},
    "ecovacs": {"display": "科沃斯 (Ecovacs)", "hotline": "400-886-8888", "default_warranty_years": 2, "order_ref": "ORD-HOME-ECOVACS-VAC"},
    "xiaomi": {"display": "小米 (Xiaomi)", "hotline": "400-100-5678", "default_warranty_years": 2, "order_ref": "ORD-HOME-XIAOMI-SMART"},
    "midea": {"display": "美的 (Midea)", "hotline": "400-889-9315", "default_warranty_years": 3, "order_ref": "ORD-HOME-MIDEA-DW"},
}

BRAND_ALIASES: dict[str, str] = {
    "gree": "gree",
    "格力": "gree",
    "空调": "gree",
    "rinnai": "rinnai",
    "林内": "rinnai",
    "热水器": "rinnai",
    "r32f": "rinnai",
    "robam": "robam",
    "老板": "robam",
    "烟灶": "robam",
    "油烟机": "robam",
    "e2s": "robam",
    "haier": "haier",
    "海尔": "haier",
    "洗衣机": "haier",
    "洗烘": "haier",
    "冰箱": "haier",
    "ecovacs": "ecovacs",
    "科沃斯": "ecovacs",
    "扫地机": "ecovacs",
    "t90s": "ecovacs",
    "xiaomi": "xiaomi",
    "小米": "xiaomi",
    "电视": "xiaomi",
    "晾衣机": "xiaomi",
    "净水器": "xiaomi",
    "midea": "midea",
    "美的": "midea",
    "洗碗机": "midea",
    "s67pro": "midea",
}

FAULT_CODE_KB: dict[str, dict[str, dict[str, Any]]] = {
    "gree": {
        "E1": {
            "meaning": "系统高压保护（室外机散热不良或冷媒压力过高）",
            "severity": "high",
            "steps": ["立即停机断电 10 分钟复位", "检查室外机散热翅片与出风口是否被百叶/杂物遮挡", "若复现请联系格力官方售后检压清洗"],
        },
        "E4": {
            "meaning": "压缩机排气温度过高保护",
            "severity": "high",
            "steps": ["关闭空调并等待 15 分钟降温", "清洗室内机回风滤网确认风量正常", "持续报错说明可能缺氟或四通阀故障，需售后上门检漏"],
        },
        "E6": {
            "meaning": "室内外机通讯故障",
            "severity": "medium",
            "steps": ["检查空调专用空开是否跳闸或接触不良", "断电 3 分钟重新上电观察", "排查内外机连接信号线与外机主板"],
        },
        "F0": {
            "meaning": "制冷剂泄漏/系统缺氟保护",
            "severity": "high",
            "steps": ["停止运行防止压缩机过热受损", "联系格力官方保压查漏并补加环保冷媒"],
        },
        "H1": {
            "meaning": "冬季制热自动除霜（正常工况，非故障）",
            "severity": "info",
            "steps": ["耐心等待 5–10 分钟除霜完毕即可自动恢复制热"],
        },
    },
    "rinnai": {
        "E1": {
            "meaning": "初次点火失败（对应林内代码 11，常见于燃气阀未开或表内余额不足）",
            "severity": "high",
            "steps": ["确认燃气灶能否正常打火（排查入户总阀/燃气表余额与电池）", "关闭热水龙头等待 30 秒后重新放水点火", "仍报错请联系林内售后核查点火针与燃气比例阀"],
        },
        "11": {
            "meaning": "初次点火失败（燃气未通或水压过低）",
            "severity": "high",
            "steps": ["检查燃气表阀门与电池电量", "重启电源开关并开启最大热水量试机", "持续报 11 请拨打林内热线 400-699-0606"],
        },
        "12": {
            "meaning": "中途熄火（燃气压力波动或烟道排烟不畅）",
            "severity": "high",
            "steps": ["检查排烟管室外防风帽是否被异物堵塞", "确认厨房燃气表规格满足 G4 大流量需求", "联系林内售后检测火焰感应针"],
        },
        "F2": {
            "meaning": "出水温度传感器开路/短路异常（对应代码 32）",
            "severity": "medium",
            "steps": ["断电 2 分钟后重启热水器", "若代码依旧闪烁，预约林内官方更换 NTC 温度传感器"],
        },
    },
    "robam": {
        "E1": {
            "meaning": "燃气灶点火失败或热电偶熄火保护",
            "severity": "medium",
            "steps": ["擦干火盖与热电偶感应针油污/水渍", "检查灶底 1 号电池电量", "若持续点不着火请报修老板售后"],
        },
        "E4": {
            "meaning": "防干烧温度传感器保护触发",
            "severity": "medium",
            "steps": ["移开空烧锅具等待炉头冷却", "确认锅底平整贴合中间测温探头"],
        },
        "F2": {
            "meaning": "油烟机风机电机或主控板通讯异常",
            "severity": "high",
            "steps": ["拔掉烟机电源插头静置 3 分钟后重启", "清理止逆阀检查是否卡死", "联系老板电器 9510-5855 上门检修"],
        },
    },
    "haier": {
        "E1": {
            "meaning": "排水超时（排水泵滤网堵塞或排水管弯折）",
            "severity": "medium",
            "steps": ["打开右下角排污口拧下过滤器清理毛絮硬币", "检查排水管高度是否超过 1 米或被挤压"],
        },
        "E4": {
            "meaning": "进水超时（进水阀未开、水压过低或进水滤网堵塞）",
            "severity": "medium",
            "steps": ["确认洗衣机角阀处于全开状态", "拆下进水管接头清洗不锈钢过滤网"],
        },
        "F2": {
            "meaning": "水位传感器或烘干温控传感器异常",
            "severity": "high",
            "steps": ["断电重启执行单脱水自检", "若仍报 F2 请联系海尔售后检测水位传感器与线束"],
        },
    },
    "ecovacs": {
        "C0": {
            "meaning": "上下水模块供水/排水异常或污水箱密封未扣紧",
            "severity": "medium",
            "steps": ["检查自动上下水三通角阀是否打开、减压阀压力正常", "确认基站污水盒密封圈压实无漏气，清理清洗盘滤网"],
        },
        "E1": {
            "meaning": "边刷/主滚刷或驱动轮缠绕异物卡死",
            "severity": "low",
            "steps": ["翻转机身清理滚刷两端的毛发与线缆", "手动拨动驱动轮确认回弹顺畅后按继续清扫"],
        },
        "E4": {
            "meaning": "激光雷达 LDS 旋转受阻或被遮挡",
            "severity": "medium",
            "steps": ["轻吹雷达罩缝隙灰尘并轻拨确认无卡顿", "关机静置 1 分钟重启，仍报错联系科沃斯售后"],
        },
    },
    "xiaomi": {
        "E1": {
            "meaning": "晾衣机/新风电机遇阻或过载保护",
            "severity": "medium",
            "steps": ["取下超重衣物或检查导轨异物，断电 1 分钟复位"],
        },
        "E4": {
            "meaning": "净水器/新风传感器或滤芯芯片通讯异常",
            "severity": "low",
            "steps": ["重新插拔对应滤芯确认到位锁紧", "长按复位键 5 秒并在米家 App 刷新设备状态"],
        },
        "F2": {
            "meaning": "电源板或背光驱动自检保护",
            "severity": "high",
            "steps": ["拔掉电源线等待 3 分钟重新通电", "无法点亮则凭电子发票联系小米售后上门"],
        },
    },
    "midea": {
        "E1": {
            "meaning": "洗碗机进水超时",
            "severity": "medium",
            "steps": ["确认厨下进水角阀打开且水压正常", "检查进水管是否折弯"],
        },
        "E4": {
            "meaning": "洗碗机底部防漏水浮球触发（溢流保护）",
            "severity": "high",
            "steps": ["立即关闭厨下进水角阀并断电", "检查门封与排水管接头渗漏，联系美的售后排空底盘水"],
        },
    },
}


def _coerce_now(now: Optional[datetime | str] = None) -> datetime:
    if now is None:
        return _db_mod.now_local()
    if isinstance(now, str):
        return parse_iso(now)
    if now.tzinfo is None:
        return now.replace(tzinfo=SHANGHAI_TZ)
    return now.astimezone(SHANGHAI_TZ)


def _coerce_date(val: Optional[date | datetime | str], fallback: Optional[date] = None) -> date:
    if val is None or (isinstance(val, str) and not val.strip()):
        return fallback or logical_today()
    if isinstance(val, datetime):
        return val.date()
    if isinstance(val, date):
        return val
    return parse_iso(str(val).strip()).date()


def upsert_utility(
    db: SimingDB,
    kind: str,
    account_no: str = "",
    cycle_days: int = 30,
    last_paid_at: str = "",
    next_due_at: str = "",
    amount_yuan: float = 0.0,
    notes: str = "",
) -> dict[str, Any]:
    """登记或更新住宅水/电/燃气/宽带/物业缴费周期台账。"""
    db.init_db()
    norm_kind = (kind or "electric").strip().lower()
    acct = account_no.strip()
    cyc = max(1, int(cycle_days))
    due_str = next_due_at.strip()
    if not due_str and last_paid_at.strip():
        due_str = (_coerce_date(last_paid_at) + timedelta(days=cyc)).isoformat()
    amount_cents = int(round(float(amount_yuan) * 100))
    util_id = f"util:{norm_kind}:{acct or 'default'}"
    ts = iso_now()

    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO home_utilities (
                util_id, kind, account_no, cycle_days, last_paid_at, next_due_at,
                amount_cents, notes, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(util_id) DO UPDATE SET
                kind = excluded.kind,
                account_no = excluded.account_no,
                cycle_days = excluded.cycle_days,
                last_paid_at = excluded.last_paid_at,
                next_due_at = excluded.next_due_at,
                amount_cents = excluded.amount_cents,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (util_id, norm_kind, acct, cyc, last_paid_at.strip(), due_str, amount_cents, notes.strip(), ts),
        )

    return {
        "ok": True,
        "util_id": util_id,
        "kind": norm_kind,
        "kind_label": UTILITY_LABELS.get(norm_kind, norm_kind),
        "account_no": acct,
        "cycle_days": cyc,
        "last_paid_at": last_paid_at.strip(),
        "next_due_at": due_str,
        "amount_yuan": round(amount_cents / 100.0, 2),
        "amount_cents": amount_cents,
        "notes": notes.strip(),
        "updated_at": ts,
    }


def check_due_utilities(
    db: SimingDB,
    now: Optional[datetime | str] = None,
    warn_days: int = 5,
    notify: bool = False,
) -> dict[str, Any]:
    """巡检水电气网物业缴费到期状态（逾期 / 即将到期 / 正常）。"""
    db.init_db()
    cur = _coerce_now(now)
    today = cur.date()

    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM home_utilities ORDER BY next_due_at ASC").fetchall()
    finally:
        conn.close()

    overdue: list[dict[str, Any]] = []
    due_soon: list[dict[str, Any]] = []
    upcoming: list[dict[str, Any]] = []

    for r in rows:
        item = dict(r)
        item["kind_label"] = UTILITY_LABELS.get(item["kind"], item["kind"])
        item["amount_yuan"] = round(int(item["amount_cents"]) / 100.0, 2)
        if not item["next_due_at"]:
            item["days_until_due"] = None
            upcoming.append(item)
            continue
        due_d = _coerce_date(item["next_due_at"])
        delta = (due_d - today).days
        item["days_until_due"] = delta
        if delta < 0:
            overdue.append(item)
        elif delta <= int(warn_days):
            due_soon.append(item)
        else:
            upcoming.append(item)

    push_result: Optional[dict[str, Any]] = None
    if notify and (overdue or due_soon) and is_module_enabled(db, "home_passport"):
        ref_id = f"utility_due:{today.isoformat()}"
        conn_chk = db.connect()
        try:
            existing_ob = conn_chk.execute(
                """
                SELECT id FROM outbox
                WHERE rule_id = 'home_utility_due' AND ref_id = ? AND status IN ('SENT', 'MORNING_QUEUE')
                LIMIT 1
                """,
                (ref_id,),
            ).fetchone()
        finally:
            conn_chk.close()
        if existing_ob:
            push_result = {
                "ok": True,
                "delivered": False,
                "skipped": "duplicate",
                "outbox_id": existing_ob["id"],
                "level": "normal",
                "rule_id": "home_utility_due",
            }
        else:
            lines = [f"🧾 **示例小区B住宅账单缴费提醒**（截至 {today.isoformat()}）"]
            for u in overdue:
                lines.append(f"• 🔴 已逾期 {abs(u['days_until_due'])} 天：**{u['kind_label']}**（户号 `{u['account_no'] or '-'}` · 约 ¥{u['amount_yuan']:.2f}）")
            for u in due_soon:
                lines.append(f"• 🟠 {u['days_until_due']} 天后到期：**{u['kind_label']}**（{u['next_due_at']} · 约 ¥{u['amount_yuan']:.2f}）")
            push_result = push_message(
                db,
                text="\n".join(lines),
                rule_id="home_utility_due",
                ref_id=ref_id,
                level="normal",
                dedup_window=timedelta(hours=18),
            )

    return {
        "ok": True,
        "as_of": today.isoformat(),
        "total": len(rows),
        "overdue": overdue,
        "due_soon": due_soon,
        "upcoming": upcoming,
        "alerts_count": len(overdue) + len(due_soon),
        "push_result": push_result,
    }


def upsert_consumable(
    db: SimingDB,
    appliance_id: str,
    appliance_name: str,
    consumable_name: str,
    lifespan_days: int = 90,
    installed_at: Optional[date | datetime | str] = None,
    jd_url: str = "",
) -> dict[str, Any]:
    """登记或更新家电耗材（净水滤芯/扫地机尘袋/新风 HEPA/洗碗机盐等）安装日期与换新日。"""
    db.init_db()
    inst_d = _coerce_date(installed_at)
    life = max(1, int(lifespan_days))
    next_d = inst_d + timedelta(days=life)
    slug = hashlib.sha1(f"{appliance_id.strip()}:{consumable_name.strip()}".encode("utf-8")).hexdigest()[:10]
    cons_id = f"cons-{slug}"
    ts = iso_now()

    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO appliance_consumables (
                cons_id, appliance_id, appliance_name, consumable_name,
                lifespan_days, installed_at, next_replace_at, jd_url, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(cons_id) DO UPDATE SET
                appliance_name = excluded.appliance_name,
                lifespan_days = excluded.lifespan_days,
                installed_at = excluded.installed_at,
                next_replace_at = excluded.next_replace_at,
                jd_url = excluded.jd_url,
                updated_at = excluded.updated_at
            """,
            (
                cons_id,
                appliance_id.strip(),
                appliance_name.strip(),
                consumable_name.strip(),
                life,
                inst_d.isoformat(),
                next_d.isoformat(),
                jd_url.strip(),
                ts,
            ),
        )

    return {
        "ok": True,
        "cons_id": cons_id,
        "appliance_id": appliance_id.strip(),
        "appliance_name": appliance_name.strip(),
        "consumable_name": consumable_name.strip(),
        "lifespan_days": life,
        "installed_at": inst_d.isoformat(),
        "next_replace_at": next_d.isoformat(),
        "jd_url": jd_url.strip(),
        "updated_at": ts,
    }


def check_due_consumables(
    db: SimingDB,
    now: Optional[datetime | str] = None,
    warn_days: int = 14,
) -> list[dict[str, Any]]:
    """返回剩余寿命 ≤ `warn_days` 或已过期的家电耗材列表（按紧迫度升序）。"""
    db.init_db()
    today = _coerce_now(now).date()

    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM appliance_consumables ORDER BY next_replace_at ASC").fetchall()
    finally:
        conn.close()

    due_list: list[dict[str, Any]] = []
    for r in rows:
        inst_d = _coerce_date(r["installed_at"])
        next_d = _coerce_date(r["next_replace_at"])
        life = max(1, int(r["lifespan_days"]))
        days_used = max(0, (today - inst_d).days)
        days_remaining = (next_d - today).days
        health_pct = round(max(0.0, min(100.0, (days_remaining / life) * 100.0)), 1)
        if days_remaining <= int(warn_days):
            due_list.append(
                {
                    "cons_id": r["cons_id"],
                    "appliance_id": r["appliance_id"],
                    "appliance_name": r["appliance_name"],
                    "consumable_name": r["consumable_name"],
                    "lifespan_days": life,
                    "installed_at": r["installed_at"],
                    "next_replace_at": r["next_replace_at"],
                    "days_used": days_used,
                    "days_remaining": days_remaining,
                    "health_pct": health_pct,
                    "status": "EXPIRED" if days_remaining <= 0 else "DUE_SOON",
                    "jd_url": r["jd_url"],
                }
            )
    due_list.sort(key=lambda x: x["days_remaining"])
    return due_list


def _resolve_brand(brand_or_appliance: str) -> str:
    q = (brand_or_appliance or "").strip().lower()
    if q in BRAND_ALIASES:
        return BRAND_ALIASES[q]
    for alias, canon in BRAND_ALIASES.items():
        if alias in q:
            return canon
    return q or "unknown"


def lookup_fault_code(
    db: SimingDB,
    brand_or_appliance: str,
    fault_code: str,
    purchase_date: Optional[date | datetime | str] = None,
    warranty_years: Optional[int] = 3,
    now: Optional[datetime | str] = None,
) -> dict[str, Any]:
    """秒查家电故障码含义、排障步骤、官方售后热线、发票/订单索引与剩余保修天数。"""
    db.init_db()
    cur = _coerce_now(now)
    today = cur.date()
    brand_key = _resolve_brand(brand_or_appliance)
    code_norm = (fault_code or "").strip().upper()
    brand_meta = BRAND_HOTLINES.get(
        brand_key,
        {"display": brand_or_appliance, "hotline": "请联系购买平台官方售后客服", "default_warranty_years": 3, "order_ref": ""},
    )

    eff_warranty_years = int(warranty_years) if warranty_years is not None else int(brand_meta["default_warranty_years"])
    kb_brand = FAULT_CODE_KB.get(brand_key, {})
    entry = kb_brand.get(code_norm)

    warranty_expires_at: Optional[str] = None
    warranty_days_remaining: Optional[int] = None
    in_warranty: Optional[bool] = None
    p_date_str = ""

    if purchase_date is not None and str(purchase_date).strip():
        p_date = _coerce_date(purchase_date)
        p_date_str = p_date.isoformat()
        try:
            exp_date = p_date.replace(year=p_date.year + eff_warranty_years)
        except ValueError:
            exp_date = p_date + timedelta(days=365 * eff_warranty_years)
        warranty_expires_at = exp_date.isoformat()
        raw_days_remaining = (exp_date - today).days
        in_warranty = raw_days_remaining >= 0
        warranty_days_remaining = max(0, raw_days_remaining)

    order_ref = db.get_meta(f"appliance_order_ref:{brand_key}") or brand_meta.get("order_ref", "")

    if entry:
        return {
            "ok": True,
            "found": True,
            "brand": brand_key,
            "brand_display": brand_meta["display"],
            "fault_code": code_norm,
            "meaning": entry["meaning"],
            "severity": entry["severity"],
            "steps": list(entry["steps"]),
            "hotline": brand_meta["hotline"],
            "order_ref": order_ref,
            "purchase_date": p_date_str,
            "warranty_years": eff_warranty_years,
            "warranty_expires_at": warranty_expires_at,
            "warranty_days_remaining": warranty_days_remaining,
            "in_warranty": in_warranty,
        }

    return {
        "ok": True,
        "found": False,
        "brand": brand_key,
        "brand_display": brand_meta["display"],
        "fault_code": code_norm,
        "meaning": f"未在内置知识库命中 `{brand_meta['display']} {code_norm}`，建议断电 3 分钟复位并拍照留证后联系官方售后。",
        "severity": "unknown",
        "steps": ["拍照记录面板完整故障码与设备铭牌 SN", "断电 3 分钟尝试重启", f"拨打官方售后热线 {brand_meta['hotline']} 报修"],
        "hotline": brand_meta["hotline"],
        "order_ref": order_ref,
        "purchase_date": p_date_str,
        "warranty_years": eff_warranty_years,
        "warranty_expires_at": warranty_expires_at,
        "warranty_days_remaining": warranty_days_remaining,
        "in_warranty": in_warranty,
    }


def compute_appliance_asset_valuation(
    db: SimingDB,
    items: Optional[list[dict[str, Any]]] = None,
    as_of: Optional[date | datetime | str] = None,
    sync_to_sure: bool = True,
) -> dict[str, Any]:
    """按直线折旧法计算 15 件大家电固定资产净值，并将资产同步映射写入 `sure_sync_map`。"""
    db.init_db()
    as_of_d = _coerce_date(as_of)
    catalog = list(items) if items is not None else list(DEFAULT_APPLIANCE_CATALOG)
    ts = iso_now()

    valued: list[dict[str, Any]] = []
    total_purchase = 0.0
    total_book = 0.0
    total_depr = 0.0
    annual_depr = 0.0

    for idx, raw in enumerate(catalog, start=1):
        aid = str(raw.get("id") or f"app-{idx:02d}")
        name = str(raw.get("name") or aid)
        price = max(0.0, float(raw.get("purchase_price", raw.get("price", 0.0))))
        p_date = _coerce_date(raw.get("purchase_date"), fallback=as_of_d)
        life_years = max(1.0, float(raw.get("useful_life_years", 8.0)))
        salvage_rate = max(0.0, min(0.5, float(raw.get("salvage_rate", 0.0))))

        salvage_val = price * salvage_rate
        depreciable = max(0.0, price - salvage_val)
        elapsed_days = max(0, (as_of_d - p_date).days)
        ratio = min(1.0, elapsed_days / (life_years * 365.0))
        acc_depr = round(depreciable * ratio, 2)
        book_val = round(max(salvage_val, price - acc_depr), 2)
        item_annual_depr = round(depreciable / life_years, 2)

        total_purchase += price
        total_book += book_val
        total_depr += acc_depr
        annual_depr += item_annual_depr

        valued.append(
            {
                "id": aid,
                "name": name,
                "purchase_price": round(price, 2),
                "purchase_date": p_date.isoformat(),
                "useful_life_years": life_years,
                "elapsed_days": elapsed_days,
                "accumulated_depreciation": acc_depr,
                "book_value": book_val,
                "annual_depreciation": item_annual_depr,
            }
        )

    sure_synced = 0
    if sync_to_sure:
        with db.transaction() as conn:
            for v in valued:
                digest = hashlib.sha256(json.dumps(v, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]
                conn.execute(
                    """
                    INSERT INTO sure_sync_map (local_kind, local_id, sure_id, sure_account, payload_hash, last_synced_at, status)
                    VALUES ('asset', ?, ?, '示例小区B固定资产', ?, ?, 'SYNCED')
                    ON CONFLICT(local_kind, local_id) DO UPDATE SET
                        sure_account = excluded.sure_account,
                        payload_hash = excluded.payload_hash,
                        last_synced_at = excluded.last_synced_at,
                        status = excluded.status
                    """,
                    (v["id"], f"sure-asset-{v['id']}", digest, ts),
                )
                sure_synced += 1

    return {
        "ok": True,
        "as_of": as_of_d.isoformat(),
        "item_count": len(valued),
        "total_purchase_yuan": round(total_purchase, 2),
        "total_book_value_yuan": round(total_book, 2),
        "total_depreciation_yuan": round(total_depr, 2),
        "annual_depreciation_yuan": round(annual_depr, 2),
        "sure_synced_count": sure_synced,
        "items": valued,
    }


def format_home_passport_report(db: SimingDB, now: Optional[datetime | str] = None) -> str:
    """汇总水电气网物业、耗材寿命与大家电固定资产净值，输出住宅全周期护照简报。"""
    cur = _coerce_now(now)
    utils = check_due_utilities(db, now=cur, notify=False)
    due_cons = check_due_consumables(db, now=cur)
    val = compute_appliance_asset_valuation(db, as_of=cur.date(), sync_to_sure=False)

    lines = [f"🏡 **示例小区B家电与住宅全周期护照**（{cur.date().isoformat()}）"]
    lines.append(
        f"• 🧾 住宅账单（共 {utils['total']} 项）：逾期 **{len(utils['overdue'])}** · 5日内到期 **{len(utils['due_soon'])}** · 正常 {len(utils['upcoming'])}"
    )
    for u in (utils["overdue"] + utils["due_soon"])[:4]:
        d_u = int(u["days_until_due"])
        due_phrase = f"已逾期 {abs(d_u)} 天" if d_u < 0 else ("今日到期" if d_u == 0 else f"剩余 {d_u} 天")
        lines.append(f"  - {u['kind_label']}（{u['next_due_at']} · ¥{u['amount_yuan']:.2f} · {due_phrase}）")

    if due_cons:
        lines.append(f"• 🧽 耗材换新预警（{len(due_cons)} 项待换）：")
        for c in due_cons[:5]:
            d_c = int(c["days_remaining"])
            cons_phrase = f"已超期 {abs(d_c)} 天" if d_c < 0 else ("今日到期" if d_c == 0 else f"剩余 {d_c} 天")
            lines.append(
                f"  - **{c['appliance_name']} · {c['consumable_name']}**（{c['status']} · {cons_phrase} / 健康度 {c['health_pct']:.0f}%）"
            )
    else:
        lines.append("• 🧽 耗材寿命：当前无 14 天内到期耗材")

    lines.append(
        f"• 💰 固定资产净值（{val['item_count']} 件大家电）：购入 ¥{val['total_purchase_yuan']:,.2f} → "
        f"当前净值 **¥{val['total_book_value_yuan']:,.2f}**（累计折旧 ¥{val['total_depreciation_yuan']:,.2f} · 年折旧 ¥{val['annual_depreciation_yuan']:,.2f}）"
    )
    return "\n".join(lines)
