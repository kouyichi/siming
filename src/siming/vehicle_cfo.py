"""Wave 1 (W1-7 · vehicle_cfo)：车务与补能全链路 CFO（Tesla Model Y RWD LFP 专属）。

能力：
1. 自动从 `ledger_txns` 归集 ETC、停车费、特斯拉超充/公共充电、车险与 12123 违章记录入 `vehicle_records`，自动区分为公（`work`）与私人（`personal`）停车票据；
2. 追踪 Model Y 后驱标续 LFP（60kWh 宁德时代磷酸铁锂）每周 100% 满电校准 BMS 提醒 + 冬季能耗效率（vs 4.35 / 3.40 km/% 基准）；
3. 追踪车险续保倒计时（≤30 天预警）与 12123 未处理违章；
4. 一键同步车务支出至 Sure「车辆账户」。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from siming import db as _db_mod
from siming.db import SimingDB, iso_now, now_local, parse_iso
from siming.event_chain import get_situation
from siming.modules import is_module_enabled

VALID_VEHICLE_KINDS = frozenset({"charge", "etc", "parking", "insurance", "violation", "lfp_cal"})

_CHARGE_RE = re.compile(r"(特斯拉超充|tesla\s*supercharg|超充|特来电|星星充电|国家电网充电|蔚来充电|小桔充电|极氪充电|充电站|充电费)", re.IGNORECASE)
_ETC_RE = re.compile(r"(\betc\b|速通卡|京通etc|高速通行|高速公路|粤通卡|苏通卡)", re.IGNORECASE)
_PARKING_RE = re.compile(r"(停车|泊车|车库|捷停车|etcp|停简单|蜜蜂停车|科拓停车|立方停车)", re.IGNORECASE)
_INSURANCE_RE = re.compile(r"(车险|交强险|商业险|车船税|平安车险|人保车险|太平洋车险)", re.IGNORECASE)
_VIOLATION_RE = re.compile(r"(12123|违章|交通违法|交警罚款)", re.IGNORECASE)
_WORK_SCOPE_RE = re.compile(r"(cw01|工作|公司|出差|报销|示例写字楼|公务|客户)", re.IGNORECASE)

MODEL_Y_NOMINAL_KM_PER_PCT = 4.35
MODEL_Y_WINTER_KM_PER_PCT = 3.40

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


def _to_cny_cents(amount_cents: int, currency: str) -> tuple[int, float]:
    curr = (currency or "CNY").strip().upper()
    rate = float(_FX_TO_CNY.get(curr, 1.0))
    return int(round(int(amount_cents or 0) * rate)), rate


def _classify_vehicle_txn(merchant: str, notes: str) -> Optional[str]:
    hay = f"{merchant or ''} {notes or ''}"
    if _CHARGE_RE.search(hay):
        return "charge"
    if _ETC_RE.search(hay):
        return "etc"
    if _PARKING_RE.search(hay):
        return "parking"
    if _INSURANCE_RE.search(hay):
        return "insurance"
    if _VIOLATION_RE.search(hay):
        return "violation"
    return None


def _classify_scope(merchant: str, notes: str) -> str:
    hay = f"{merchant or ''} {notes or ''}"
    return "work" if _WORK_SCOPE_RE.search(hay) else "personal"


def harvest_vehicle_costs_from_ledger(db: SimingDB, days: int = 365) -> dict[str, Any]:
    """从 `ledger_txns` 幂等归集 ETC / 停车 / 超充 / 车险 / 违章流水至 `vehicle_records`，并区分公私归属。"""
    db.init_db()
    cutoff = iso_now(_db_mod.now_local() - timedelta(days=max(1, int(days))))
    ts = iso_now()

    conn = db.connect()
    try:
        txns = [
            dict(r)
            for r in conn.execute(
                """
                SELECT txn_id, occurred_at, amount_cents, currency, merchant, notes, account_alias, kind, status
                FROM ledger_txns
                WHERE occurred_at >= ?
                  AND amount_cents > 0
                  AND kind NOT IN ('收入', '退款', '还款', '转入')
                  AND COALESCE(UPPER(status), '') NOT IN ('VOID_DUPLICATE', 'VOID', 'CANCELLED')
                ORDER BY occurred_at ASC
                """,
                (cutoff,),
            ).fetchall()
        ]
        existing_ids = {
            str(r["rec_id"]) for r in conn.execute("SELECT rec_id FROM vehicle_records").fetchall()
        }
    finally:
        conn.close()

    harvested = 0
    skipped = 0
    by_kind: dict[str, int] = {}
    work_cents = 0
    personal_cents = 0

    with db.transaction() as wconn:
        for t in txns:
            v_kind = _classify_vehicle_txn(str(t.get("merchant") or ""), str(t.get("notes") or ""))
            if not v_kind:
                continue
            rec_id = f"VR-LED-{t['txn_id']}"
            orig_curr = str(t.get("currency") or "CNY").strip().upper()
            orig_cents = int(t["amount_cents"])
            amt_c, fx_rate = _to_cny_cents(orig_cents, orig_curr)
            scope = _classify_scope(str(t.get("merchant") or ""), str(t.get("notes") or ""))

            if scope == "work":
                work_cents += amt_c
            else:
                personal_cents += amt_c
            by_kind[v_kind] = by_kind.get(v_kind, 0) + 1

            if rec_id in existing_ids:
                skipped += 1
                continue

            detail = {
                "source_txn_id": t["txn_id"],
                "account_alias": t["account_alias"],
                "notes": t["notes"],
                "orig_currency": orig_curr,
                "orig_amount_cents": orig_cents,
                "fx_rate": fx_rate,
            }
            wconn.execute(
                """
                INSERT INTO vehicle_records (
                    rec_id, kind, occurred_at, amount_cents, scope,
                    provider, title, detail_json, sure_synced, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    rec_id,
                    v_kind,
                    str(t["occurred_at"]),
                    amt_c,
                    scope,
                    str(t.get("merchant") or "")[:48],
                    f"{t.get('merchant') or v_kind}"[:80],
                    json.dumps(detail, ensure_ascii=False),
                    ts,
                ),
            )
            harvested += 1

    return {
        "ok": True,
        "harvested": harvested,
        "skipped": skipped,
        "by_kind": by_kind,
        "work_yuan": round(work_cents / 100.0, 2),
        "personal_yuan": round(personal_cents / 100.0, 2),
    }


def record_vehicle_event(
    db: SimingDB,
    kind: str,
    title: str,
    amount_yuan: float = 0.0,
    occurred_at: Optional[str] = None,
    scope: str = "personal",
    provider: str = "",
    detail: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """手动或自动登记一条车务事件（充电/LFP 100% 校准/ETC/停车/车险/12123违章）。"""
    db.init_db()
    k = (kind or "").strip().lower()
    if k not in VALID_VEHICLE_KINDS:
        return {"ok": False, "error": f"不支持的车务类型 {kind!r}，可选 {sorted(VALID_VEHICLE_KINDS)}"}
    sc = "work" if (scope or "").strip().lower() in ("work", "public", "cw01") else "personal"
    ts = iso_now()
    occ = (occurred_at or ts).strip()
    amt_c = int(round(max(0.0, float(amount_yuan)) * 100))
    det = dict(detail or {})
    rec_id = f"VR-{_db_mod.now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}"

    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO vehicle_records (
                rec_id, kind, occurred_at, amount_cents, scope,
                provider, title, detail_json, sure_synced, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (rec_id, k, occ, amt_c, sc, provider.strip(), title.strip(), json.dumps(det, ensure_ascii=False), ts),
        )
        # LFP 满电校准记录（仅当新记录不早于已有记录时更新 meta，防回填历史账单覆盖近期满充）
        if k == "lfp_cal" or (k == "charge" and (det.get("full_charge") or float(det.get("end_soc") or 0) >= 99.0)):
            prev_cal = (db.get_meta("tesla_last_lfp_cal_at") or "").strip()
            should_update_cal = True
            if prev_cal:
                try:
                    should_update_cal = parse_iso(occ) >= parse_iso(prev_cal)
                except Exception:
                    should_update_cal = occ >= prev_cal
            if should_update_cal:
                db.set_meta("tesla_last_lfp_cal_at", occ, conn=conn)
        if k == "insurance" and det.get("expires_at"):
            db.set_meta("vehicle_insurance_expires_at", str(det["expires_at"]), conn=conn)

    return {
        "ok": True,
        "rec_id": rec_id,
        "kind": k,
        "title": title.strip(),
        "amount_yuan": round(amt_c / 100.0, 2),
        "occurred_at": occ,
        "scope": sc,
        "provider": provider.strip(),
        "detail": det,
    }


def evaluate_lfp_calibration_and_insurance(
    db: SimingDB,
    now: Optional[datetime] = None,
    notify: bool = False,
) -> dict[str, Any]:
    """评估 Model Y LFP 每周 100% 校准提醒、冬季能耗追踪、车险续保倒计时与 12123 未处理违章。"""
    db.init_db()
    cur = now or _db_mod.now_local()

    conn = db.connect()
    try:
        rows = [
            dict(r)
            for r in conn.execute("SELECT * FROM vehicle_records ORDER BY occurred_at DESC").fetchall()
        ]
    finally:
        conn.close()

    # 1) LFP 每周 100% 满电校准检查
    last_cal_at = (db.get_meta("tesla_last_lfp_cal_at") or "").strip()
    for r in rows:
        try:
            det = json.loads(r.get("detail_json") or "{}")
            if not isinstance(det, dict):
                det = {}
        except Exception:
            det = {}
        is_full = r["kind"] == "lfp_cal" or (
            r["kind"] == "charge" and (det.get("full_charge") or float(det.get("end_soc") or 0) >= 99.0)
        )
        if is_full:
            cand_at = str(r["occurred_at"])
            if not last_cal_at:
                last_cal_at = cand_at
            else:
                try:
                    if parse_iso(cand_at) > parse_iso(last_cal_at):
                        last_cal_at = cand_at
                except Exception:
                    if cand_at > last_cal_at:
                        last_cal_at = cand_at
            break

    days_since_lfp_cal: Optional[int] = None
    if last_cal_at:
        try:
            days_since_lfp_cal = max(0, (cur.date() - parse_iso(last_cal_at).date()).days)
        except Exception:
            days_since_lfp_cal = None
    lfp_calibration_due = days_since_lfp_cal is None or days_since_lfp_cal >= 7

    # 2) 冬季/实跑能耗效率追踪
    is_winter = cur.month in (11, 12, 1, 2, 3)
    eff_samples: list[float] = []
    for r in rows:
        if r["kind"] != "charge":
            continue
        try:
            det = json.loads(r.get("detail_json") or "{}")
        except Exception:
            det = {}
        km = float(det.get("distance_km") or 0.0)
        kwh = float(det.get("kwh") or 0.0)
        if km > 0 and kwh > 0:
            eff_samples.append(round(km / kwh, 2))
    avg_km_per_kwh = round(sum(eff_samples) / len(eff_samples), 2) if eff_samples else None

    tesla_sit = get_situation(db, "tesla:main")
    tesla_val = tesla_sit["value"] if (tesla_sit and isinstance(tesla_sit.get("value"), dict)) else {}
    est_km_per_pct = MODEL_Y_WINTER_KM_PER_PCT if is_winter else MODEL_Y_NOMINAL_KM_PER_PCT
    if avg_km_per_kwh is not None:
        # 60kWh LFP 电池，每 1% 约 0.6 kWh
        est_km_per_pct = round(avg_km_per_kwh * 0.6, 2)

    # 3) 车险续保倒计时
    ins_exp_s = (db.get_meta("vehicle_insurance_expires_at") or "").strip()
    if not ins_exp_s:
        for r in rows:
            if r["kind"] == "insurance":
                try:
                    det = json.loads(r.get("detail_json") or "{}")
                except Exception:
                    det = {}
                if det.get("expires_at"):
                    ins_exp_s = str(det["expires_at"])
                else:
                    try:
                        ins_exp_s = (parse_iso(str(r["occurred_at"])) + timedelta(days=365)).date().isoformat()
                    except Exception:
                        pass
                break

    days_to_insurance: Optional[int] = None
    if ins_exp_s:
        try:
            exp_dt = parse_iso(f"{ins_exp_s[:10]}T23:59:59+08:00") if len(ins_exp_s) <= 10 else parse_iso(ins_exp_s)
            days_to_insurance = int((exp_dt.date() - cur.date()).days)
        except Exception:
            days_to_insurance = None
    insurance_due_soon = days_to_insurance is not None and days_to_insurance <= 30

    # 4) 12123 未处理违章
    open_violations: list[dict[str, Any]] = []
    for r in rows:
        if r["kind"] != "violation":
            continue
        try:
            det = json.loads(r.get("detail_json") or "{}")
        except Exception:
            det = {}
        if not det.get("paid") and not det.get("resolved"):
            open_violations.append(
                {
                    "rec_id": r["rec_id"],
                    "title": r["title"],
                    "occurred_at": r["occurred_at"],
                    "amount_yuan": round(int(r["amount_cents"] or 0) / 100.0, 2),
                    "points": int(det.get("points") or 0),
                }
            )

    alerts: list[str] = []
    alert_keys: list[str] = []
    if lfp_calibration_due:
        since_s = f"已 {days_since_lfp_cal} 天未满充" if days_since_lfp_cal is not None else "暂无 100% 校准记录"
        alerts.append(f"🔋 Model Y LFP 每周满电校准到期（{since_s}），建议下次顺路超充直接充至 100% 校准 BMS")
        alert_keys.append(f"lfp_cal:{last_cal_at or 'none'}")
    if insurance_due_soon:
        if days_to_insurance is not None and days_to_insurance < 0:
            ins_phrase = f"已过期 **{abs(days_to_insurance)} 天**"
        elif days_to_insurance == 0:
            ins_phrase = "**今天到期**"
        else:
            ins_phrase = f"距到期仅剩 **{days_to_insurance} 天**"
        alerts.append(f"🛡️ 车险续保倒计时：{ins_phrase}（{ins_exp_s[:10]}），请提前比价续保")
        alert_keys.append(f"insurance:{ins_exp_s[:10]}")
    if open_violations:
        alerts.append(f"🚨 12123 待处理违章：**{len(open_violations)}** 条（请核查扣分与罚款）")
        vio_sig = ",".join(sorted(str(v["rec_id"]) for v in open_violations))
        alert_keys.append(f"violations:{vio_sig}")

    notified = False
    if notify and alerts and is_module_enabled(db, "vehicle_cfo"):
        day_key = f"vehicle_cfo_notified:{cur.date().isoformat()}"
        try:
            seen_keys = set(json.loads(db.get_meta(day_key) or "[]"))
        except Exception:
            seen_keys = set()
        fresh_pairs = [(k, msg) for k, msg in zip(alert_keys, alerts) if k not in seen_keys]
        if fresh_pairs:
            from siming.push import push_message

            fresh_msgs = [msg for _, msg in fresh_pairs]
            res = push_message(
                db,
                text="🚗 **Model Y 车务与补能提醒**\n" + "\n".join(f"• {a}" for a in fresh_msgs),
                rule_id="vehicle_cfo",
                ref_id=f"vehicle_cfo:{cur.date().isoformat()}",
                level="normal",
                dedup_window=timedelta(hours=24),
            )
            notified = bool(res.get("ok") and not res.get("skipped"))
            if res.get("ok"):
                seen_keys.update(k for k, _ in fresh_pairs)
                db.set_meta(day_key, json.dumps(sorted(seen_keys), ensure_ascii=False))

    return {
        "ok": True,
        "lfp_calibration_due": lfp_calibration_due,
        "last_lfp_cal_at": last_cal_at,
        "days_since_lfp_cal": days_since_lfp_cal,
        "is_winter": is_winter,
        "avg_km_per_kwh": avg_km_per_kwh,
        "effective_km_per_pct": est_km_per_pct,
        "current_soc_pct": tesla_val.get("soc_pct"),
        "insurance_expires_at": ins_exp_s,
        "days_to_insurance": days_to_insurance,
        "insurance_due_soon": insurance_due_soon,
        "open_violations": open_violations,
        "alerts": alerts,
        "notified": notified,
    }


def sync_vehicle_to_sure(
    db: SimingDB,
    http_fn: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """将尚未同步的 `vehicle_records` 推送至 Sure「车辆账户」并写入 `sure_sync_map`。"""
    db.init_db()
    conn = db.connect()
    try:
        pending = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM vehicle_records WHERE sure_synced = 0 ORDER BY occurred_at ASC"
            ).fetchall()
        ]
    finally:
        conn.close()

    synced = 0
    failed = 0
    total_cents = 0
    work_cents = 0
    personal_cents = 0
    ts = iso_now()

    for rec in pending:
        rec_id = str(rec["rec_id"])
        amt_c = int(rec["amount_cents"] or 0)
        amt_y = round(amt_c / 100.0, 2)
        scope = str(rec["scope"] or "personal")
        payload = {
            "external_id": rec_id,
            "account_name": "车辆账户",
            "category": "出行车务",
            "kind": rec["kind"],
            "title": rec["title"],
            "provider": rec["provider"],
            "occurred_at": rec["occurred_at"],
            "amount_yuan": amt_y,
            "tags": ["车辆", str(rec["kind"]), "CW01" if scope == "work" else "private"],
        }
        p_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        sure_id = f"sure-veh-{p_hash[:12]}"
        status = "SYNCED"

        if http_fn is not None:
            try:
                resp = http_fn("POST", "http://127.0.0.1:3032/api/v1/transactions", payload, {})
                if resp.get("ok"):
                    d = resp.get("data") if isinstance(resp.get("data"), dict) else {}
                    sure_id = str(d.get("id") or d.get("sure_id") or sure_id)
                else:
                    status = "FAILED"
            except Exception:
                status = "FAILED"

        with db.transaction() as wconn:
            wconn.execute(
                """
                INSERT INTO sure_sync_map (
                    local_kind, local_id, sure_id, sure_account, payload_hash, last_synced_at, status
                ) VALUES ('vehicle', ?, ?, '车辆账户', ?, ?, ?)
                ON CONFLICT(local_kind, local_id) DO UPDATE SET
                    sure_id = excluded.sure_id,
                    sure_account = excluded.sure_account,
                    payload_hash = excluded.payload_hash,
                    last_synced_at = excluded.last_synced_at,
                    status = excluded.status
                """,
                (rec_id, sure_id, p_hash, ts, status),
            )
            if status == "SYNCED":
                wconn.execute("UPDATE vehicle_records SET sure_synced = 1 WHERE rec_id = ?", (rec_id,))

        if status == "SYNCED":
            synced += 1
            total_cents += amt_c
            if scope == "work":
                work_cents += amt_c
            else:
                personal_cents += amt_c
        else:
            failed += 1

    return {
        "ok": failed == 0,
        "synced": synced,
        "failed": failed,
        "total_yuan": round(total_cents / 100.0, 2),
        "work_yuan": round(work_cents / 100.0, 2),
        "personal_yuan": round(personal_cents / 100.0, 2),
        "sure_account": "车辆账户",
    }


def format_vehicle_report(db: SimingDB, now: Optional[datetime] = None) -> str:
    """格式化 Model Y 车务账单、LFP 校准、冬季能效与保险/违章状态报告。"""
    db.init_db()
    cur = now or _db_mod.now_local()
    ev = evaluate_lfp_calibration_and_insurance(db, now=cur, notify=False)

    conn = db.connect()
    try:
        agg_rows = conn.execute(
            "SELECT kind, scope, COUNT(*) AS cnt, COALESCE(SUM(amount_cents), 0) AS cents FROM vehicle_records GROUP BY kind, scope"
        ).fetchall()
    finally:
        conn.close()

    by_kind_yuan: dict[str, float] = {}
    work_y = 0.0
    personal_y = 0.0
    for r in agg_rows:
        y = round(int(r["cents"] or 0) / 100.0, 2)
        k = str(r["kind"])
        by_kind_yuan[k] = round(by_kind_yuan.get(k, 0.0) + y, 2)
        if str(r["scope"]) == "work":
            work_y = round(work_y + y, 2)
        else:
            personal_y = round(personal_y + y, 2)

    lines = [
        "🚗 **Model Y 车务与补能 CFO 报告**",
        f"• 费用汇总：私人 **¥{personal_y:,.2f}** ｜ 公务可报销 **¥{work_y:,.2f}**",
        "• 分项支出："
        + (
            " · ".join(f"{k}=¥{v:.2f}" for k, v in sorted(by_kind_yuan.items()))
            if by_kind_yuan
            else "暂无记录"
        ),
        f"• 🔋 LFP 100% 校准：{'⚠️ 到期需满充' if ev['lfp_calibration_due'] else '✅ 近期已校准'}"
        + (f"（距上次 {ev['days_since_lfp_cal']} 天）" if ev["days_since_lfp_cal"] is not None else ""),
        f"• ❄️ 续航折算：`{ev['effective_km_per_pct']} km/%`"
        + (f"（实测 {ev['avg_km_per_kwh']} km/kWh）" if ev["avg_km_per_kwh"] is not None else "（Model Y 标续默认）"),
    ]
    if ev["days_to_insurance"] is not None:
        lines.append(f"• 🛡️ 车险到期：`{ev['insurance_expires_at'][:10]}`（剩 **{ev['days_to_insurance']} 天**）")
    lines.append(f"• 🚨 12123 未处理违章：**{len(ev['open_violations'])}** 条")
    return "\n".join(lines)
