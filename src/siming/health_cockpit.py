"""Wave 1 (W1-6 · health_cockpit)：私人健康驾驶舱——化验单 OCR 指标抽取、周期用药/体重/睡眠债关联与常用医院一页医生简报。

支持抽取指标与标准临床参考范围：
- 空腹血糖 (glucose, 3.9–6.1 mmol/L)
- 糖化血红蛋白 (hba1c, 4.0–6.0 %)
- 总胆固醇 (tc, 2.8–5.2 mmol/L)
- 甘油三酯 (tg, 0.4–1.7 mmol/L)
- 低密度脂蛋白 (ldl, 0.0–3.4 mmol/L)
- 高密度脂蛋白 (hdl, 1.0–2.0 mmol/L)
- 谷丙转氨酶 (alt, 0–40 U/L)
- 谷草转氨酶 (ast, 0–40 U/L)
- 尿酸 (uric_acid, 208–428 umol/L)
- 肌酐 (creatinine, 57–97 umol/L)
- 体重 (weight_kg, kg；自动识别「斤」并换算 /2)
- 睡眠债 (sleep_debt_h, 0–2.0 h)
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Optional

from siming.db import SimingDB, iso_now, now_local, parse_iso

BIOMARKER_SPECS: dict[str, dict[str, Any]] = {
    "glucose": {
        "label": "空腹血糖",
        "unit": "mmol/L",
        "low": 3.9,
        "high": 6.1,
        "ref_range": "3.9-6.1 mmol/L",
        "pattern": r"(?:空腹血糖|葡萄糖|血糖|\bGLU\b|\bFBG\b|glucose)",
    },
    "hba1c": {
        "label": "糖化血红蛋白",
        "unit": "%",
        "low": 4.0,
        "high": 6.0,
        "ref_range": "4.0-6.0 %",
        "pattern": r"(?:糖化血红蛋白|HbA1c|HBA1C|A1C)",
    },
    "tc": {
        "label": "总胆固醇",
        "unit": "mmol/L",
        "low": 2.8,
        "high": 5.2,
        "ref_range": "2.8-5.2 mmol/L",
        "pattern": r"(?:总胆固醇|\bTC\b|\bCHOL\b)",
    },
    "tg": {
        "label": "甘油三酯",
        "unit": "mmol/L",
        "low": 0.4,
        "high": 1.7,
        "ref_range": "0.4-1.7 mmol/L",
        "pattern": r"(?:甘油三酯|三酰甘油|\bTG\b)",
    },
    "ldl": {
        "label": "低密度脂蛋白(LDL-C)",
        "unit": "mmol/L",
        "low": 0.0,
        "high": 3.4,
        "ref_range": "0.0-3.4 mmol/L",
        "pattern": r"(?:低密度脂蛋白胆固醇|低密度脂蛋白|LDL-C|\bLDL\b)",
    },
    "hdl": {
        "label": "高密度脂蛋白(HDL-C)",
        "unit": "mmol/L",
        "low": 1.0,
        "high": 2.0,
        "ref_range": "1.0-2.0 mmol/L",
        "pattern": r"(?:高密度脂蛋白胆固醇|高密度脂蛋白|HDL-C|\bHDL\b)",
    },
    "alt": {
        "label": "谷丙转氨酶(ALT)",
        "unit": "U/L",
        "low": 0.0,
        "high": 40.0,
        "ref_range": "0-40 U/L",
        "pattern": r"(?:谷丙转氨酶|丙氨酸氨基转移酶|\bALT\b)",
    },
    "ast": {
        "label": "谷草转氨酶(AST)",
        "unit": "U/L",
        "low": 0.0,
        "high": 40.0,
        "ref_range": "0-40 U/L",
        "pattern": r"(?:谷草转氨酶|天门冬氨酸氨基转移酶|\bAST\b)",
    },
    "uric_acid": {
        "label": "尿酸",
        "unit": "umol/L",
        "low": 208.0,
        "high": 428.0,
        "ref_range": "208-428 umol/L",
        "pattern": r"(?:血?尿酸|uric[_\s]*acid|\bUA\b)",
    },
    "creatinine": {
        "label": "肌酐",
        "unit": "umol/L",
        "low": 57.0,
        "high": 97.0,
        "ref_range": "57-97 umol/L",
        "pattern": r"(?:血?肌酐|creatinine|\bCREA\b|\bCr\b)",
    },
    "weight_kg": {
        "label": "体重",
        "unit": "kg",
        "low": None,
        "high": None,
        "ref_range": "",
        "pattern": r"(?:体重|weight_kg|weight)",
    },
    "sleep_debt_h": {
        "label": "睡眠债",
        "unit": "h",
        "low": 0.0,
        "high": 2.0,
        "ref_range": "0-2.0 h",
        "pattern": r"(?:睡眠债|熬夜欠睡|sleep_debt_h|sleep\s*debt)",
    },
}

_METRIC_ALIASES: dict[str, str] = {
    "空腹血糖": "glucose",
    "血糖": "glucose",
    "glu": "glucose",
    "糖化血红蛋白": "hba1c",
    "a1c": "hba1c",
    "总胆固醇": "tc",
    "chol": "tc",
    "甘油三酯": "tg",
    "ldl-c": "ldl",
    "低密度脂蛋白": "ldl",
    "hdl-c": "hdl",
    "高密度脂蛋白": "hdl",
    "谷丙转氨酶": "alt",
    "谷草转氨酶": "ast",
    "尿酸": "uric_acid",
    "ua": "uric_acid",
    "肌酐": "creatinine",
    "cr": "creatinine",
    "体重": "weight_kg",
    "weight": "weight_kg",
    "睡眠债": "sleep_debt_h",
}


def _normalize_metric_type(raw_type: str) -> str:
    key = (raw_type or "").strip().lower()
    if key in BIOMARKER_SPECS:
        return key
    if key in _METRIC_ALIASES:
        return _METRIC_ALIASES[key]
    return key or "unknown"


def _evaluate_flag(metric_type: str, value_num: float) -> tuple[str, str, str]:
    spec = BIOMARKER_SPECS.get(metric_type)
    if not spec:
        return "NORMAL", "", ""
    low = spec.get("low")
    high = spec.get("high")
    flag = "NORMAL"
    if high is not None and value_num > float(high):
        flag = "HIGH"
    elif low is not None and value_num < float(low):
        flag = "LOW"
    return flag, str(spec.get("unit") or ""), str(spec.get("ref_range") or "")


_PLAUSIBLE_BOUNDS: dict[str, tuple[float, float]] = {
    "glucose": (0.5, 60.0),
    "hba1c": (2.0, 25.0),
    "tc": (0.5, 30.0),
    "tg": (0.1, 50.0),
    "ldl": (0.1, 20.0),
    "hdl": (0.1, 10.0),
    "alt": (0.0, 5000.0),
    "ast": (0.0, 5000.0),
    "uric_acid": (30.0, 2000.0),
    "creatinine": (10.0, 2000.0),
    "weight_kg": (20.0, 300.0),
    "sleep_debt_h": (0.0, 48.0),
}

_DATE_NOISE_RE = re.compile(r"(?<!\d)(?:19|20)\d{2}[-/.年]\d{1,2}[-/.月]\d{1,2}日?(?!\d)")
_TIME_NOISE_RE = re.compile(r"(?<!\d)\d{1,2}:\d{2}(?::\d{2})?(?!\d)")
_PHONE_NOISE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_ID_NOISE_RE = re.compile(r"(?<!\d)\d{8,}(?!\d)")
_REF_CLAUSE_RE = re.compile(
    r"(?:参考(?:范围|区间|值)|正常(?:范围|值)|ref(?:erence)?(?:\s*range)?)[ \t:：=]*"
    r"(?:[<>≤≥][ \t]*)?\d+(?:\.\d+)?(?:[ \t]*[-~～—–][ \t]*\d+(?:\.\d+)?)?"
    r"(?:[ \t]*(?:mmol/L|umol/L|μmol/L|µmol/L|IU/L|U/L|%))?",
    re.IGNORECASE,
)
_PAREN_RANGE_RE = re.compile(
    r"[\(（\[][ \t]*(?:[<>≤≥][ \t]*)?\d+(?:\.\d+)?[ \t]*[-~～—–][ \t]*\d+(?:\.\d+)?"
    r"(?:[ \t]*(?:mmol/L|umol/L|μmol/L|µmol/L|IU/L|U/L|%))?[ \t]*[\)）\]]",
    re.IGNORECASE,
)
_BARE_RANGE_RE = re.compile(r"(?<!\d)\d+(?:\.\d+)?[ \t]*[-~～—–][ \t]*\d+(?:\.\d+)?(?!\d)")
_CJK_INNER_SPACE_RE = re.compile(r"(?<=[\u4e00-\u9fff])[ \t\u3000]+(?=[\u4e00-\u9fff])")


def _clean_ocr_line(line: str) -> str:
    """清除化验单 OCR 单行中的日期、手机号、长编号与参考范围，避免误把参考值或日期当成检测值。"""
    s = (line or "").replace("\u3000", " ").replace("µ", "μ")
    s = _DATE_NOISE_RE.sub(" ", s)
    s = _TIME_NOISE_RE.sub(" ", s)
    s = _PHONE_NOISE_RE.sub(" ", s)
    s = _ID_NOISE_RE.sub(" ", s)
    s = _REF_CLAUSE_RE.sub(" ", s)
    s = _PAREN_RANGE_RE.sub(" ", s)
    s = _BARE_RANGE_RE.sub(" ", s)
    # 修复 OCR 拆字空格（如「空腹 血糖」「谷丙 转氨酶」），但保留「参考 范围」之后已清理的空白
    s = _CJK_INNER_SPACE_RE.sub("", s)
    return s.strip()


def _normalize_unit(raw_unit: str, mtype: str, default_unit: str) -> str:
    if mtype == "weight_kg":
        return "kg"
    if not raw_unit:
        return default_unit
    u = raw_unit.strip().replace("μ", "u").replace("µ", "u")
    low = u.lower()
    if low == "umol/l":
        return "umol/L"
    if low == "mmol/l":
        return "mmol/L"
    if low in ("u/l", "iu/l"):
        return "U/L"
    if low in ("h", "小时"):
        return "h"
    return u


def parse_lab_ocr_text(text: str) -> list[dict[str, Any]]:
    """从化验单/体检报告 OCR 文本中抽取结构化生化指标与体重，并自动判定 HIGH / LOW / NORMAL。"""
    src = text or ""
    if not src.strip():
        return []

    cleaned_lines = [_clean_ocr_line(ln) for ln in src.splitlines()]
    cleaned_lines = [ln for ln in cleaned_lines if ln]

    results: list[dict[str, Any]] = []
    seen: set[str] = set()

    flag_chars = r"(?:[↑↓▲▼]|(?<![A-Za-z])[HL](?![A-Za-z])|偏高|偏低)"
    unit_group = r"(mmol/L|umol/L|μmol/L|µmol/L|IU/L|U/L|%|kg|公斤|斤|h|小时)"

    for mtype, spec in BIOMARKER_SPECS.items():
        if mtype in seen:
            continue
        # 严禁跨行匹配：仅使用水平空白，支持指标名后括号缩写、前置或后置 ↑/↓/H/L 箭头及多变体单位
        pat = re.compile(
            rf"{spec['pattern']}"
            rf"(?:[ \t]*[\(（][^\)）]{{1,20}}[\)）])?"
            rf"[ \t:：=]*(?:{flag_chars}[ \t]*)?"
            rf"([0-9]+(?:\.[0-9]+)?)"
            rf"(?:[ \t]*{flag_chars})?"
            rf"[ \t]*{unit_group}?",
            re.IGNORECASE,
        )

        best_candidate: Optional[tuple[int, float, str]] = None
        min_v, max_v = _PLAUSIBLE_BOUNDS.get(mtype, (0.0, 1e9))

        for ln in cleaned_lines:
            for m in pat.finditer(ln):
                try:
                    raw_val = float(m.group(1))
                except ValueError:
                    continue
                raw_unit = (m.group(2) or "").strip()
                val = raw_val
                if mtype == "weight_kg" and raw_unit == "斤":
                    val = round(raw_val / 2.0, 2)
                if not (min_v <= val <= max_v):
                    continue
                # 优先选择携带明确单位的匹配行（抗重复/噪声行干扰）
                score = 2 if raw_unit else 1
                if best_candidate is None or score > best_candidate[0]:
                    best_candidate = (score, val, raw_unit)
                    if score == 2:
                        break
            if best_candidate is not None and best_candidate[0] == 2:
                break

        if best_candidate is None:
            continue

        _, val, raw_unit = best_candidate
        flag, default_unit, ref_range = _evaluate_flag(mtype, val)
        unit = _normalize_unit(raw_unit, mtype, default_unit)

        seen.add(mtype)
        results.append(
            {
                "metric_type": mtype,
                "label": spec["label"],
                "value_num": round(val, 2),
                "unit": unit,
                "ref_range": ref_range,
                "flag": flag,
            }
        )
    return results


def record_health_metric(
    db: SimingDB,
    metric_type: str,
    value_num: float,
    unit: str = "",
    measured_at: Optional[str] = None,
    notes: str = "",
    source_event_id: str = "",
) -> dict[str, Any]:
    """写入单条健康/化验指标至 `health_metrics` 并自动评估异常标记。"""
    db.init_db()
    mtype = _normalize_metric_type(metric_type)
    val = round(float(value_num), 2)
    flag, default_unit, ref_range = _evaluate_flag(mtype, val)
    final_unit = (unit or "").strip() or default_unit
    ts = iso_now()
    meas = (measured_at or ts).strip()
    metric_id = f"HM-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}"

    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO health_metrics (
                metric_id, measured_at, metric_type, value_num, unit,
                ref_range, flag, source_event_id, notes, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (metric_id, meas, mtype, val, final_unit, ref_range, flag, source_event_id, notes.strip(), ts),
        )

    return {
        "ok": True,
        "metric_id": metric_id,
        "measured_at": meas,
        "metric_type": mtype,
        "label": (BIOMARKER_SPECS.get(mtype) or {}).get("label", mtype),
        "value_num": val,
        "unit": final_unit,
        "ref_range": ref_range,
        "flag": flag,
        "notes": notes.strip(),
    }


def ingest_health_text(
    db: SimingDB,
    text: str,
    measured_at: Optional[str] = None,
    source_event_id: str = "",
) -> dict[str, Any]:
    """从化验单 OCR 文本一键解析并落库全部指标。"""
    db.init_db()
    parsed = parse_lab_ocr_text(text)
    if not parsed:
        return {"ok": False, "count": 0, "abnormal_count": 0, "metrics": [], "error": "未识别到可解析的健康化验指标"}

    saved: list[dict[str, Any]] = []
    for item in parsed:
        rec = record_health_metric(
            db,
            metric_type=item["metric_type"],
            value_num=item["value_num"],
            unit=item["unit"],
            measured_at=measured_at,
            source_event_id=source_event_id,
        )
        saved.append(rec)

    abnormal = [m for m in saved if m["flag"] in ("HIGH", "LOW")]
    return {
        "ok": True,
        "count": len(saved),
        "abnormal_count": len(abnormal),
        "abnormal": abnormal,
        "metrics": saved,
    }


def get_health_trends(
    db: SimingDB,
    days: int = 90,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """汇总近 N 天生化指标、体重轨迹、周期用药注射记录（联动 `daily_care`）与熬夜睡眠债。"""
    db.init_db()
    cur = now or now_local()
    cutoff_iso = iso_now(cur - timedelta(days=max(1, int(days))))

    conn = db.connect()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM health_metrics WHERE measured_at >= ? ORDER BY measured_at ASC",
                (cutoff_iso,),
            ).fetchall()
        ]
    finally:
        conn.close()

    by_type: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_type.setdefault(str(r["metric_type"]), []).append(r)

    latest_by_type: dict[str, dict[str, Any]] = {}
    abnormal_latest: list[dict[str, Any]] = []
    for mtype, series in by_type.items():
        first = series[0]
        last = series[-1]
        delta = round(float(last["value_num"]) - float(first["value_num"]), 2)
        entry = {
            "metric_type": mtype,
            "label": (BIOMARKER_SPECS.get(mtype) or {}).get("label", mtype),
            "count": len(series),
            "first_value": float(first["value_num"]),
            "latest_value": float(last["value_num"]),
            "delta": delta,
            "unit": str(last["unit"] or ""),
            "ref_range": str(last["ref_range"] or ""),
            "flag": str(last["flag"] or "NORMAL"),
            "latest_at": str(last["measured_at"]),
        }
        latest_by_type[mtype] = entry
        if entry["flag"] in ("HIGH", "LOW"):
            abnormal_latest.append(entry)

    # 读取 daily_care 周期用药注射记录与余针状态
    raw_inj = db.get_meta("care_injection_log") or "[]"
    raw_pen = db.get_meta("care_pen") or "{}"
    try:
        inj_log = [x for x in json.loads(raw_inj) if isinstance(x, dict)]
    except Exception:
        inj_log = []
    try:
        pen_state = json.loads(raw_pen) if isinstance(json.loads(raw_pen), dict) else {}
    except Exception:
        pen_state = {}

    cutoff_day = (cur - timedelta(days=max(1, int(days)))).date().isoformat()
    recent_injs = [x for x in inj_log if str(x.get("slot") or x.get("at") or "") >= cutoff_day]

    # 体重轨迹
    weight_series = by_type.get("weight_kg") or []
    weight_trend = {
        "count": len(weight_series),
        "start_kg": float(weight_series[0]["value_num"]) if weight_series else None,
        "latest_kg": float(weight_series[-1]["value_num"]) if weight_series else None,
        "delta_kg": round(float(weight_series[-1]["value_num"]) - float(weight_series[0]["value_num"]), 2)
        if len(weight_series) >= 2
        else 0.0,
    }

    # 睡眠债关联统计
    sleep_series = by_type.get("sleep_debt_h") or []
    sleep_debt_avg = (
        round(sum(float(x["value_num"]) for x in sleep_series) / len(sleep_series), 2) if sleep_series else 0.0
    )
    high_sleep_debt_days = sum(1 for x in sleep_series if float(x["value_num"]) > 2.0)

    return {
        "ok": True,
        "days": days,
        "as_of": iso_now(cur),
        "total_records": len(rows),
        "latest_by_type": latest_by_type,
        "abnormal_latest": abnormal_latest,
        "weight_trend": weight_trend,
        "periodic_med": {
            "injections_in_window": len(recent_injs),
            "doses_left": pen_state.get("doses_left"),
            "last_visit": pen_state.get("last_visit") or "",
            "recent_slots": [str(x.get("slot") or "") for x in recent_injs[-6:]],
        },
        "sleep_debt": {
            "records": len(sleep_series),
            "avg_debt_h": sleep_debt_avg,
            "consecutive_high_days": high_sleep_debt_days,
        },
    }


def generate_doctor_brief(
    db: SimingDB,
    hospital: str = "常用医院",
    days: int = 90,
    now: Optional[datetime] = None,
) -> str:
    """生成面向医生（默认常用医院）的 1 页结构化就诊简报。"""
    cur = now or now_local()
    tr = get_health_trends(db, days=days, now=cur)
    tirz = tr["periodic_med"]
    wt = tr["weight_trend"]
    sd = tr["sleep_debt"]

    lines = [
        f"# 🏥 {hospital} 就诊一页健康简报",
        f"> 生成时间：{cur.strftime('%Y-%m-%d %H:%M')} ｜ 回溯窗口：近 {days} 天 ｜ 指标记录：{tr['total_records']} 条",
        "",
        "## 1. 周期用药用药与体重轨迹",
        f"- 近 {days} 天注射次数：**{tirz['injections_in_window']} 针**（最近针次：{', '.join(tirz['recent_slots']) or '暂无登记'}）",
        f"- 当前余药：**{tirz['doses_left'] if tirz['doses_left'] is not None else '未登记'} 针**",
    ]
    if wt["count"] >= 2:
        sign = "+" if wt["delta_kg"] > 0 else ""
        lines.append(
            f"- 体重变化：**{wt['start_kg']:.1f} kg → {wt['latest_kg']:.1f} kg**（净变化 **{sign}{wt['delta_kg']:.1f} kg**，共 {wt['count']} 次记录）"
        )
    elif wt["latest_kg"] is not None:
        lines.append(f"- 最新体重：**{wt['latest_kg']:.1f} kg**")
    else:
        lines.append("- 体重记录：暂无")

    lines.extend(["", "## 2. 化验指标重点异常（需医生关注）"])
    abn = [x for x in tr["abnormal_latest"] if x["metric_type"] != "sleep_debt_h"]
    if abn:
        for m in abn:
            arrow = "↑偏高" if m["flag"] == "HIGH" else "↓偏低"
            lines.append(
                f"- ⚠️ **{m['label']}**：**{m['latest_value']} {m['unit']}**（{arrow}，参考 `{m['ref_range']}`，检测日 `{m['latest_at'][:10]}`）"
            )
    else:
        lines.append("- ✅ 近期核心生化指标均在标准参考范围内")

    lines.extend(["", "## 3. 其它核心化验指标一览"])
    normals = [
        m
        for k, m in tr["latest_by_type"].items()
        if k not in ("weight_kg", "sleep_debt_h") and m["flag"] == "NORMAL"
    ]
    if normals:
        for m in normals:
            lines.append(
                f"- {m['label']}：`{m['latest_value']} {m['unit']}`（参考 `{m['ref_range']}`，{m['latest_at'][:10]}）"
            )
    else:
        lines.append("- 暂无其他正常项化验记录")

    lines.extend(["", "## 4. 作息与睡眠债关联提示"])
    if sd["records"] > 0:
        lines.append(
            f"- 近 {days} 天记录睡眠债 {sd['records']} 次，均值 **{sd['avg_debt_h']:.1f} h**（>2h 熬夜天数：{sd['consecutive_high_days']} 天），请注意作息对空腹血糖/肝酶的影响。"
        )
    else:
        lines.append("- 暂无显式睡眠债异常记录（夜猫子作息，抽血前请注意保证空腹 8–12h）。")

    lines.extend(
        [
            "",
            "## 5. 本次就诊待沟通清单",
            "- 结合当前体重降幅与代谢指标，评估周期用药维持剂量或调量方案；",
            "- 开具下一周期多剂量笔处方及复查化验单。",
        ]
    )
    return "\n".join(lines)
