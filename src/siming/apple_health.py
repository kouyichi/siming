"""P3 (#4B) Apple Health 正规健康事实源（apple_health.py）：
1. HealthObservation(source, source_sample_id, obs_type, start_at, end_at, timezone, value_num, unit, device, revision, deleted_at, consent_scope)；
2. 同时支持 iPhone 快捷指令每日聚合窗口（按窗口替换修订）与原生 HealthKit UUID 增量及删除墓碑；
3. 处理跨午夜睡眠、手机/手表重叠去重（Apple Watch 优先于 iPhone）、迟到更正重算；
4. 撤权/未戴表/超 48h 未同步一律诚实返回 status='unknown'，严禁误推用户未睡、未运动或调药建议。
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Any, Optional

from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local, parse_iso

TARGET_SLEEP_HOURS = 7.5


def _merge_sleep_intervals(intervals: list[dict[str, Any]]) -> list[tuple[datetime, datetime]]:
    """合并手机与手表重叠的睡眠区间（避免 Watch + iPhone 重复叠加睡眠时长），支持跨午夜区间。"""
    parsed: list[tuple[int, datetime, datetime]] = []
    for it in intervals:
        s_raw = str(it.get("start_at") or "")
        e_raw = str(it.get("end_at") or "")
        if not s_raw or not e_raw:
            continue
        s_dt = parse_iso(s_raw)
        e_dt = parse_iso(e_raw)
        if e_dt <= s_dt:
            continue
        dev = str(it.get("device") or "").lower()
        prio = 0 if "watch" in dev else 1
        parsed.append((prio, s_dt, e_dt))

    if not parsed:
        return []
    # 按开始时间排序并做区间并集，避免任何双计
    parsed.sort(key=lambda x: (x[1], x[0]))
    merged: list[tuple[datetime, datetime]] = []
    cur_s, cur_e = parsed[0][1], parsed[0][2]
    for _, s_dt, e_dt in parsed[1:]:
        if s_dt <= cur_e:
            if e_dt > cur_e:
                cur_e = e_dt
        else:
            merged.append((cur_s, cur_e))
            cur_s, cur_e = s_dt, e_dt
    merged.append((cur_s, cur_e))
    return merged


def ingest_health_observations(
    db: SimingDB,
    observations: list[dict[str, Any]],
    *,
    source: str = "apple_health_shortcut",
    consent_scope: str = "daily_summary",
) -> dict[str, Any]:
    """幂等写入 Apple Health 观测事实（支持快捷指令窗口修订、HealthKit UUID 增量、deleted_at 删除与睡眠去重重算）。"""
    db.init_db()
    ts = iso_now()
    ingested = 0
    deleted = 0
    touched_sleep_days: set[str] = set()
    latest_weight: Optional[tuple[str, float]] = None

    with db.transaction() as conn:
        for raw in observations:
            if not isinstance(raw, dict):
                continue
            obs_type = str(raw.get("obs_type") or raw.get("type") or "").strip().lower()
            start_at = str(raw.get("start_at") or raw.get("measured_at") or "").strip()
            if not obs_type or not start_at:
                continue
            end_at = str(raw.get("end_at") or "").strip()
            sample_id = str(raw.get("source_sample_id") or raw.get("uuid") or "").strip()
            tz = str(raw.get("timezone") or "Asia/Shanghai").strip()
            device = str(raw.get("device") or "").strip()
            rev = max(1, int(raw.get("revision") or 1))
            del_at = str(raw.get("deleted_at") or "").strip() or None
            unit = str(raw.get("unit") or "").strip()
            val = float(raw.get("value_num") if raw.get("value_num") is not None else (raw.get("value") or 0.0))

            if sample_id:
                obs_key = f"{source}::{sample_id}"
            else:
                win_key = start_at[:10] if not end_at else f"{start_at[:16]}..{end_at[:16]}"
                obs_key = f"{source}::{obs_type}::{win_key}::{device or 'agg'}"

            prev = conn.execute("SELECT revision FROM health_observations WHERE obs_key = ?", (obs_key,)).fetchone()
            if prev and int(prev["revision"] or 1) > rev and del_at is None:
                continue

            conn.execute(
                """
                INSERT INTO health_observations (
                    obs_key, source, source_sample_id, obs_type, start_at, end_at, timezone,
                    value_num, unit, device, revision, deleted_at, consent_scope, meta_json, ingested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(obs_key) DO UPDATE SET
                    start_at=excluded.start_at,
                    end_at=excluded.end_at,
                    timezone=excluded.timezone,
                    value_num=excluded.value_num,
                    unit=excluded.unit,
                    device=excluded.device,
                    revision=excluded.revision,
                    deleted_at=excluded.deleted_at,
                    consent_scope=excluded.consent_scope,
                    meta_json=excluded.meta_json,
                    ingested_at=excluded.ingested_at
                """,
                (
                    obs_key,
                    source,
                    sample_id,
                    obs_type,
                    start_at,
                    end_at,
                    tz,
                    val,
                    unit,
                    device,
                    rev,
                    del_at,
                    consent_scope,
                    json.dumps(raw.get("meta") or {}, ensure_ascii=False),
                    ts,
                ),
            )
            if del_at:
                deleted += 1
            else:
                ingested += 1
            if obs_type in ("sleep_interval", "sleep_duration_h"):
                # 跨午夜睡眠归属到醒来自然日（end_at 或 start_at）
                day_ref = (end_at or start_at)[:10]
                touched_sleep_days.add(day_ref)
            elif obs_type == "weight_kg" and del_at is None:
                latest_weight = (start_at, val)

        # 按天重算受影响日期的净睡眠时长与睡眠债（去重手机/手表重叠），同步到 health_metrics
        recomputed_sleep: dict[str, dict[str, float]] = {}
        for day_s in sorted(touched_sleep_days):
            d_obj = datetime.strptime(day_s, "%Y-%m-%d").replace(tzinfo=SHANGHAI_TZ)
            win_lo = iso_now(d_obj - timedelta(hours=12))
            win_hi = iso_now(d_obj + timedelta(hours=24))
            rows = [
                dict(r)
                for r in conn.execute(
                    """
                    SELECT * FROM health_observations
                    WHERE obs_type IN ('sleep_interval', 'sleep_duration_h')
                      AND deleted_at IS NULL
                      AND start_at >= ? AND start_at <= ?
                    """,
                    (win_lo, win_hi),
                ).fetchall()
            ]
            # 仅取归属当天（醒来日在 day_s）的记录
            day_intervals = [r for r in rows if r["obs_type"] == "sleep_interval" and (r["end_at"] or r["start_at"])[:10] == day_s]
            if day_intervals:
                merged = _merge_sleep_intervals(day_intervals)
                total_h = round(sum((e - s).total_seconds() for s, e in merged) / 3600.0, 2)
            else:
                dur_rows = [r for r in rows if r["obs_type"] == "sleep_duration_h" and (r["end_at"] or r["start_at"])[:10] == day_s]
                total_h = round(max((float(r["value_num"]) for r in dur_rows), default=0.0), 2)

            debt_h = round(max(0.0, TARGET_SLEEP_HOURS - total_h), 2) if total_h > 0 else 0.0
            recomputed_sleep[day_s] = {"sleep_hours": total_h, "sleep_debt_h": debt_h}
            if total_h > 0:
                mid = f"HM-ah-sleep-{day_s}"
                flag = "HIGH" if debt_h > 2.0 else "NORMAL"
                conn.execute(
                    """
                    INSERT INTO health_metrics (
                        metric_id, measured_at, metric_type, value_num, unit, ref_range, flag, source_event_id, notes, created_at
                    ) VALUES (?, ?, 'sleep_debt_h', ?, 'h', '0-2.0', ?, ?, ?, ?)
                    ON CONFLICT(metric_id) DO UPDATE SET
                        value_num=excluded.value_num, flag=excluded.flag, notes=excluded.notes
                    """,
                    (mid, f"{day_s}T09:00:00+08:00", debt_h, flag, source, f"sleep={total_h}h ({source})", ts),
                )

        if latest_weight is not None:
            w_at, w_val = latest_weight
            mid_w = f"HM-ah-weight-{w_at[:10]}"
            conn.execute(
                """
                INSERT INTO health_metrics (
                    metric_id, measured_at, metric_type, value_num, unit, ref_range, flag, source_event_id, notes, created_at
                ) VALUES (?, ?, 'weight_kg', ?, 'kg', '', 'NORMAL', ?, ?, ?)
                ON CONFLICT(metric_id) DO UPDATE SET value_num=excluded.value_num, measured_at=excluded.measured_at
                """,
                (mid_w, w_at, w_val, source, f"Apple Health ({source})", ts),
            )

        # 更新 Situation 真源（health:latest）
        conn.execute(
            """
            INSERT INTO situation (key, value_json, as_of, source, confidence)
            VALUES ('health:latest', ?, ?, ?, 0.95)
            ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json, as_of=excluded.as_of, source=excluded.source
            """,
            (
                json.dumps({"sleep_by_day": recomputed_sleep, "latest_weight_kg": latest_weight[1] if latest_weight else None}, ensure_ascii=False),
                ts,
                source,
            ),
        )

    db.mark_source_ok("apple_health", detail=f"ingested={ingested} deleted={deleted}")
    return {
        "ok": True,
        "ingested": ingested,
        "deleted": deleted,
        "recomputed_sleep": recomputed_sleep,
        "latest_weight_kg": latest_weight[1] if latest_weight else None,
    }


def get_health_readiness_summary(
    db: SimingDB,
    *,
    now: Optional[datetime] = None,
    max_stale_hours: int = 48,
) -> dict[str, Any]:
    """诚实返回 Apple Health 当前状态：未授权/未戴表/超 48h 未同步一律返回 status='unknown'，绝不臆断健康结论。"""
    cur = now or now_local()
    conn = db.connect_readonly()
    try:
        row = conn.execute(
            "SELECT MAX(ingested_at) AS last_at, COUNT(*) AS cnt FROM health_observations WHERE deleted_at IS NULL"
        ).fetchone()
    finally:
        conn.close()

    last_at = str(row["last_at"] or "") if row else ""
    cnt = int(row["cnt"] or 0) if row else 0
    if not last_at or cnt == 0:
        return {
            "ok": True,
            "status": "unknown",
            "reason": "no_observations_or_consent_revoked",
            "inferred_missed_sleep": False,
            "medication_advice": None,
        }
    age_h = (cur - parse_iso(last_at)).total_seconds() / 3600.0
    if age_h > max_stale_hours:
        return {
            "ok": True,
            "status": "unknown",
            "reason": "stale_or_unworn",
            "age_hours": round(age_h, 1),
            "last_ingested_at": last_at,
            "inferred_missed_sleep": False,
            "medication_advice": None,
        }
    return {
        "ok": True,
        "status": "fresh",
        "age_hours": round(age_h, 1),
        "last_ingested_at": last_at,
        "active_observations": cnt,
    }
