"""W2-5 (`commute_hub`)：示例小区B（2028-01-01+）60m 多模态通勤决策中枢、暴雨/起晚一键救援捆绑卡与作息规律 `relearning` 切换检测。"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from typing import Any, Optional

from siming import db as _db_mod
from siming.approval import create_approval
from siming.db import SimingDB, iso_now, logical_today, parse_iso
from siming.event_chain import routines_md_path
from siming.modules import is_module_enabled

REGIME_SWITCH_DATE = date(2028, 1, 1)
OLD_REGIME = "primary_home"
NEW_REGIME = "secondary_home"

CROWDING_PENALTY: dict[str, float] = {
    "low": 0.0,
    "medium": 6.0,
    "high": 14.0,
    "extreme": 22.0,
}

MODE_LABELS: dict[str, str] = {
    "ev": "电车自驾（EV）",
    "metro": "地铁通勤",
    "didi": "滴滴打车",
}


def compare_commute_modes(
    db: SimingDB,
    ev_eta_min: float = 48,
    ev_soc_pct: float = 65,
    parking_yuan: float = 40.0,
    metro_eta_min: float = 58,
    metro_crowding: str = "high",
    didi_eta_min: float = 45,
    didi_fare_yuan: float = 68.0,
    rain: bool = False,
    late_min: int = 0,
) -> dict[str, Any]:
    """多模态通勤综合评分：权衡耗时、费用、电量续航、地铁拥挤度、降雨与起晚压力。"""
    db.init_db()
    crowd_key = (metro_crowding or "medium").strip().lower()
    crowd_pen = CROWDING_PENALTY.get(crowd_key, 8.0)
    late = max(0, int(late_min))

    soc_clamped = max(0.0, min(100.0, float(ev_soc_pct)))
    ev_feasible = soc_clamped >= 20.0
    ev_soc_penalty = 0.0 if soc_clamped >= 30.0 else (20.0 if ev_feasible else 80.0)
    ev_rain_penalty = 5.0 if rain else 0.0
    ev_late_penalty = 6.0 if late > 0 else 0.0
    ev_score = round(float(ev_eta_min) + float(parking_yuan) * 0.35 + ev_soc_penalty + ev_rain_penalty + ev_late_penalty, 2)
    ev_notes: list[str] = [f"电量 {soc_clamped:.0f}%", f"停车费 ¥{parking_yuan:.0f}"]
    if not ev_feasible:
        ev_notes.append("⚠️ 电量低于 20% 需先补能")
    elif soc_clamped < 30.0:
        ev_notes.append("电量偏低建议返程顺路补能")
    if rain and ev_feasible:
        ev_notes.append("雨天自驾避雨直达")

    metro_fare = 6.0
    metro_rain_penalty = 15.0 if rain else 0.0
    metro_late_penalty = late * 1.2
    metro_score = round(float(metro_eta_min) + metro_fare * 0.35 + crowd_pen + metro_rain_penalty + metro_late_penalty, 2)
    metro_notes: list[str] = [f"拥挤度 {crowd_key}", f"票价约 ¥{metro_fare:.0f}"]
    if rain:
        metro_notes.append("雨天步行接驳不便")

    didi_rain_adj = -6.0 if rain else 0.0
    didi_late_adj = -min(late, 30) * 0.8
    didi_score = round(float(didi_eta_min) + float(didi_fare_yuan) * 0.35 + didi_rain_adj + didi_late_adj, 2)
    didi_notes: list[str] = [f"预估车费 ¥{didi_fare_yuan:.0f}", "门到门免停泊"]
    if rain:
        didi_notes.append("雨天省心优先")

    candidates = [
        {
            "mode": "ev",
            "label": MODE_LABELS["ev"],
            "eta_min": float(ev_eta_min),
            "cost_yuan": round(float(parking_yuan), 2),
            "soc_pct": float(ev_soc_pct),
            "feasible": ev_feasible,
            "score": ev_score,
            "notes": ev_notes,
        },
        {
            "mode": "metro",
            "label": MODE_LABELS["metro"],
            "eta_min": float(metro_eta_min),
            "cost_yuan": metro_fare,
            "crowding": crowd_key,
            "feasible": True,
            "score": metro_score,
            "notes": metro_notes,
        },
        {
            "mode": "didi",
            "label": MODE_LABELS["didi"],
            "eta_min": float(didi_eta_min),
            "cost_yuan": round(float(didi_fare_yuan), 2),
            "feasible": True,
            "score": didi_score,
            "notes": didi_notes,
        },
    ]
    ranked = sorted(candidates, key=lambda x: (not x["feasible"], x["score"], x["eta_min"]))
    best = ranked[0]

    rescue_reasons: list[str] = []
    if rain and late >= 10:
        rescue_reasons.append(f"暴雨且起晚 {late}m")
    elif late >= 15:
        rescue_reasons.append(f"起晚 {late}m 时间紧张")
    elif rain and not ev_feasible:
        rescue_reasons.append("暴雨且电车电量不足")
    rescue_recommended = bool(rescue_reasons)

    return {
        "ok": True,
        "recommended_mode": best["mode"],
        "recommended_label": best["label"],
        "ranked_modes": ranked,
        "rescue_recommended": rescue_recommended,
        "rescue_reason": "；".join(rescue_reasons),
        "inputs": {
            "ev_eta_min": float(ev_eta_min),
            "ev_soc_pct": float(ev_soc_pct),
            "parking_yuan": float(parking_yuan),
            "metro_eta_min": float(metro_eta_min),
            "metro_crowding": crowd_key,
            "didi_eta_min": float(didi_eta_min),
            "didi_fare_yuan": float(didi_fare_yuan),
            "rain": bool(rain),
            "late_min": late,
        },
    }


def stage_commute_rescue_bundle(
    db: SimingDB,
    reason: str = "暴雨/起晚",
    didi_fare_yuan: float = 68.0,
    coffee_delay_min: int = 20,
    late_eta_str: str = "09:50",
) -> dict[str, Any]:
    """暴雨或起晚时，生成单张捆绑 A2 审批卡（叫滴滴 + 推迟瑞幸咖啡 + 生成晚到报备话术）。"""
    db.init_db()
    if not is_module_enabled(db, "commute_hub"):
        return {
            "ok": False,
            "skipped": "module_disabled",
            "reason": "module_disabled",
            "bundle_id": "",
            "token": "",
            "actions": [],
            "card_text": "",
        }
    ts = iso_now()
    seed = f"{reason}|{didi_fare_yuan}|{coffee_delay_min}|{late_eta_str}|{ts}"
    bundle_id = "BND-COMMUTE-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:8].upper()
    late_draft = f"今天遇{reason}，路上略堵，预计 {late_eta_str} 左右到工位，急事随时留言我车上回复。"

    params = {
        "reason": reason,
        "bundle_actions": ["call_didi", "delay_coffee", "late_notice"],
        "call_didi": {"origin": "示例小区B", "destination": "公司", "estimated_fare_yuan": float(didi_fare_yuan)},
        "delay_coffee": {"delay_min": int(coffee_delay_min)},
        "late_notice": {"eta_str": late_eta_str, "draft_message": late_draft},
    }
    approval = create_approval(
        db,
        action="commute_rescue_bundle",
        target_id=bundle_id,
        params=params,
        tier="A2",
        bundle_id=bundle_id,
    )

    card_text = "\n".join(
        [
            f"🚑 **通勤一键救援捆绑卡**（原因：{reason} · `{bundle_id}`）",
            f"1. 🚕 `call_didi`：示例小区B → 公司（预估 ¥{float(didi_fare_yuan):.0f}）",
            f"2. ☕ `delay_coffee`：瑞幸咖啡顺延 **{int(coffee_delay_min)} 分钟** 制作防放凉",
            f"3. 💬 `late_notice`：晚到报备草稿（预计 `{late_eta_str}` 到）：> {late_draft}",
            f"👉 确认执行请回 `apply {approval['token']}`",
        ]
    )

    return {
        "ok": True,
        "bundle_id": bundle_id,
        "token": approval["token"],
        "approval": approval,
        "actions": ["call_didi", "delay_coffee", "late_notice"],
        "reason": reason,
        "didi_fare_yuan": float(didi_fare_yuan),
        "coffee_delay_min": int(coffee_delay_min),
        "late_eta_str": late_eta_str,
        "late_draft": late_draft,
        "card_text": card_text,
    }


def _coerce_date(today: Optional[date | datetime | str]) -> date:
    if today is None:
        return logical_today(_db_mod.now_local())
    if isinstance(today, datetime):
        return logical_today(today)
    if isinstance(today, date):
        return today
    raw = str(today).strip()
    if "T" in raw or " " in raw or ":" in raw:
        return logical_today(parse_iso(raw))
    return date.fromisoformat(raw[:10])


def check_regime_transition(db: SimingDB, today: Optional[date | datetime | str] = None) -> dict[str, Any]:
    """检测是否跨过 2028-01-01 搬家日：从 `primary_home` 切换至 `secondary_home` 并在 `meta` 与 `routines.md` 标记 `relearning`。"""
    db.init_db()
    cur_date = _coerce_date(today)
    prev_regime = db.get_meta("commute_regime") or OLD_REGIME
    if not is_module_enabled(db, "commute_hub"):
        return {
            "ok": True,
            "skipped": "module_disabled",
            "today": cur_date.isoformat(),
            "switch_date": REGIME_SWITCH_DATE.isoformat(),
            "regime": prev_regime,
            "previous_regime": prev_regime,
            "transitioned": False,
            "relearning": False,
            "days_until_switch": max(0, (REGIME_SWITCH_DATE - cur_date).days),
        }

    crossed = cur_date >= REGIME_SWITCH_DATE

    if not crossed:
        days_left = (REGIME_SWITCH_DATE - cur_date).days
        return {
            "ok": True,
            "today": cur_date.isoformat(),
            "switch_date": REGIME_SWITCH_DATE.isoformat(),
            "regime": OLD_REGIME,
            "previous_regime": prev_regime,
            "transitioned": False,
            "relearning": False,
            "days_until_switch": days_left,
        }

    # 幂等保护：若已切入 `secondary_home`，严禁重复触发切换、覆盖原始触发日期或重刷 A1 审计日志
    if prev_regime == NEW_REGIME:
        raw_relearn = db.get_meta("routines_relearning") or ""
        try:
            relearn_obj = json.loads(raw_relearn) if raw_relearn else {}
        except Exception:
            relearn_obj = {}
        return {
            "ok": True,
            "today": cur_date.isoformat(),
            "switch_date": REGIME_SWITCH_DATE.isoformat(),
            "regime": NEW_REGIME,
            "previous_regime": prev_regime,
            "transitioned": False,
            "already_transitioned": True,
            "relearning": bool(relearn_obj.get("active", True)),
            "days_until_switch": 0,
            "routines_md_path": str(routines_md_path(db)),
            "relearning_meta": relearn_obj,
        }

    relearn_payload = {
        "active": True,
        "status": "relearning",
        "from_regime": OLD_REGIME,
        "to_regime": NEW_REGIME,
        "switch_date": REGIME_SWITCH_DATE.isoformat(),
        "triggered_on": cur_date.isoformat(),
        "updated_at": iso_now(),
    }
    db.set_meta("commute_regime", NEW_REGIME)
    db.set_meta("routines_relearning", json.dumps(relearn_payload, ensure_ascii=False))

    r_path = routines_md_path(db)
    marker = "<!-- commute_regime:secondary_home:relearning -->"
    r_path.parent.mkdir(parents=True, exist_ok=True)
    existing = r_path.read_text(encoding="utf-8") if r_path.exists() else "# Routines\n"
    if marker not in existing:
        sep = "\n" if existing and not existing.endswith("\n") else ""
        r_path.write_text(
            existing
            + sep
            + f"\n{marker}\n## 通勤规律重学（relearning）\n"
            + f"- 切换日期：`{REGIME_SWITCH_DATE.isoformat()}`（`{OLD_REGIME}` → `{NEW_REGIME}`）\n"
            + "- 状态：`relearning`（暂停旧示例小区A步行规律误报，重新学习示例小区B 60m 通勤基线）\n",
            encoding="utf-8",
        )

    db.write_audit(
        "A1",
        "commute_regime_transition",
        relearn_payload,
        target_id=NEW_REGIME,
        detail=f"{OLD_REGIME} -> {NEW_REGIME}",
    )

    return {
        "ok": True,
        "today": cur_date.isoformat(),
        "switch_date": REGIME_SWITCH_DATE.isoformat(),
        "regime": NEW_REGIME,
        "previous_regime": prev_regime,
        "transitioned": True,
        "relearning": True,
        "days_until_switch": 0,
        "routines_md_path": str(r_path),
        "relearning_meta": relearn_payload,
    }


def format_commute_advice(plan: dict[str, Any]) -> str:
    """将 `compare_commute_modes` 结果渲染为简洁中文通勤决策卡。"""
    ranked = plan.get("ranked_modes") or []
    best_label = plan.get("recommended_label") or plan.get("recommended_mode") or "-"
    inp = plan.get("inputs") or {}
    weather_s = "🌧️ 有雨" if inp.get("rain") else "☀️ 无雨"
    late_s = f" · ⏰ 起晚 {inp['late_min']}m" if inp.get("late_min") else ""

    lines = [
        f"🚇 **示例小区B 60m 通勤决策建议**（{weather_s}{late_s}）",
        f"• 🏆 首推方案：**{best_label}**",
    ]
    for idx, item in enumerate(ranked, start=1):
        flag = "✅" if item.get("feasible", True) else "⛔"
        notes = " · ".join(item.get("notes") or [])
        lines.append(
            f"  {idx}. {flag} **{item['label']}**：{item['eta_min']:.0f} 分钟 ｜ ¥{item['cost_yuan']:.0f} ｜ 综合分 {item['score']:.1f}（{notes}）"
        )
    if plan.get("rescue_recommended"):
        lines.append(f"• 🚑 建议触发救援捆绑卡（{plan.get('rescue_reason') or '极端天气/起晚'}）：一键叫车 + 咖啡顺延 + 晚到报备。")
    return "\n".join(lines)
