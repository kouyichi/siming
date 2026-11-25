"""W2-3 (`bambu_copilot`)：拓竹 Bambu Lab 3D 打印车间副驾（完工带图推送、异常急停告警、耗材余量/干燥箱湿度监控与低库存 A2 京东补货卡）。"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from siming.approval import create_approval
from siming.db import SimingDB, iso_now
from siming.modules import is_module_enabled
from siming.push import push_message

DRYBOX_HUMIDITY_WARN_PCT = 40.0

ANOMALY_LABELS: dict[str, str] = {
    "spaghetti": "炒面/拉丝失控",
    "runout": "耗材断料/用尽",
    "bed_temp": "热床温度异常",
    "nozzle_clog": "喷嘴堵塞/挤出异常",
    "power_loss": "打印中断/掉电",
}


def upsert_spool(
    db: SimingDB,
    spool_id: str,
    material: str = "PLA",
    color: str = "白色",
    brand: str = "Bambu",
    initial_grams: float = 1000.0,
    remaining_grams: float = 1000.0,
    drybox_humidity_pct: float = 25.0,
    low_threshold_grams: float = 180.0,
    jd_url: str = "",
) -> dict[str, Any]:
    """登记或更新 3D 打印耗材卷库存与干燥箱湿度。"""
    db.init_db()
    ts = iso_now()
    sid = spool_id.strip()
    rem = max(0.0, float(remaining_grams))
    init_g = max(rem, float(initial_grams))
    hum = float(drybox_humidity_pct)
    low_th = max(0.0, float(low_threshold_grams))

    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO bambu_spools (
                spool_id, material, color, brand, initial_grams, remaining_grams,
                drybox_humidity_pct, low_threshold_grams, jd_url, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(spool_id) DO UPDATE SET
                material = excluded.material,
                color = excluded.color,
                brand = excluded.brand,
                initial_grams = excluded.initial_grams,
                remaining_grams = excluded.remaining_grams,
                drybox_humidity_pct = excluded.drybox_humidity_pct,
                low_threshold_grams = excluded.low_threshold_grams,
                jd_url = excluded.jd_url,
                updated_at = excluded.updated_at
            """,
            (sid, material.strip().upper(), color.strip(), brand.strip(), init_g, rem, hum, low_th, jd_url.strip(), ts),
        )

    return {
        "ok": True,
        "spool_id": sid,
        "material": material.strip().upper(),
        "color": color.strip(),
        "brand": brand.strip(),
        "initial_grams": init_g,
        "remaining_grams": rem,
        "drybox_humidity_pct": hum,
        "low_threshold_grams": low_th,
        "jd_url": jd_url.strip(),
        "low_stock": rem <= low_th,
        "humidity_alert": hum > DRYBOX_HUMIDITY_WARN_PCT,
        "updated_at": ts,
    }


def _find_pending_spool_restock(db: SimingDB, spool_id: str) -> Optional[dict[str, Any]]:
    now_iso = iso_now()
    conn = db.connect()
    try:
        row = conn.execute(
            """
            SELECT token, bundle_id, action, target_id, params_digest, expires_at
            FROM approvals
            WHERE action = 'jd_replenish_spool'
              AND target_id = ?
              AND status = 'PENDING'
              AND (expires_at = '' OR expires_at >= ?)
            ORDER BY created_at DESC LIMIT 1
            """,
            (spool_id, now_iso),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def consume_filament(
    db: SimingDB,
    spool_id: str,
    grams: float,
    drybox_humidity_pct: Optional[float] = None,
    stage_restock: bool = True,
) -> dict[str, Any]:
    """扣减耗材克数，检测低库存阈值与干燥箱湿度（>40%），低于阈值自动生成 A2 京东补货加购审批卡。"""
    db.init_db()
    sid = spool_id.strip()
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM bambu_spools WHERE spool_id = ?", (sid,)).fetchone()
    finally:
        conn.close()

    if not row:
        return {"ok": False, "error": f"未找到耗材卷 `{sid}`"}

    prev_grams = float(row["remaining_grams"])
    used = max(0.0, float(grams))
    new_grams = round(max(0.0, prev_grams - used), 2)
    new_hum = float(drybox_humidity_pct) if drybox_humidity_pct is not None else float(row["drybox_humidity_pct"])
    low_th = float(row["low_threshold_grams"])
    ts = iso_now()

    with db.transaction() as conn:
        conn.execute(
            "UPDATE bambu_spools SET remaining_grams = ?, drybox_humidity_pct = ?, updated_at = ? WHERE spool_id = ?",
            (new_grams, new_hum, ts, sid),
        )

    low_stock = new_grams <= low_th
    humidity_alert = new_hum > DRYBOX_HUMIDITY_WARN_PCT
    alerts: list[str] = []
    if low_stock:
        alerts.append(f"耗材余量不足：{row['brand']} {row['material']} {row['color']} 仅剩 {new_grams:.0f}g（≤ 阈值 {low_th:.0f}g）")
    if humidity_alert:
        alerts.append(f"干燥箱湿度偏高：{new_hum:.1f}%（> {DRYBOX_HUMIDITY_WARN_PCT:.0f}%），建议更换分子筛干燥剂或烘干耗材")

    restock_approval: Optional[dict[str, Any]] = None
    if low_stock and stage_restock:
        existing = _find_pending_spool_restock(db, sid)
        if existing:
            restock_approval = existing
        else:
            restock_approval = create_approval(
                db,
                action="jd_replenish_spool",
                target_id=sid,
                params={
                    "spool_id": sid,
                    "brand": row["brand"],
                    "material": row["material"],
                    "color": row["color"],
                    "remaining_grams": new_grams,
                    "low_threshold_grams": low_th,
                    "replenish_qty": 1,
                    "jd_url": row["jd_url"],
                },
                tier="A2",
            )

    return {
        "ok": True,
        "spool_id": sid,
        "brand": row["brand"],
        "material": row["material"],
        "color": row["color"],
        "previous_grams": prev_grams,
        "consumed_grams": used,
        "remaining_grams": new_grams,
        "low_threshold_grams": low_th,
        "low_stock": low_stock,
        "drybox_humidity_pct": new_hum,
        "humidity_alert": humidity_alert,
        "alerts": alerts,
        "restock_approval": restock_approval,
        "updated_at": ts,
    }


def _describe_anomaly(anomaly: str) -> str:
    raw = (anomaly or "").strip()
    if not raw:
        return ""
    low = raw.lower()
    for key, label in ANOMALY_LABELS.items():
        if key in low:
            return f"{label} ({raw})"
    return raw


def update_print_job(
    db: SimingDB,
    job_id: str,
    name: str,
    status: str,
    progress_pct: float = 0.0,
    spool_id: str = "",
    used_grams: float = 0.0,
    snapshot_path: str = "",
    anomaly: str = "",
    notify: bool = True,
) -> dict[str, Any]:
    """同步Bambu 3D 打印任务状态：完工附摄像头快照推送；炒面/断料/热床异常立即暂停并发送 L3 告警。"""
    db.init_db()
    jid = job_id.strip()
    ts = iso_now()
    norm_status = (status or "RUNNING").strip().upper()
    if norm_status not in ("RUNNING", "PAUSED", "DONE", "FAILED"):
        norm_status = "RUNNING"

    anom_clean = (anomaly or "").strip()
    pause_triggered = False
    if anom_clean and norm_status == "RUNNING":
        norm_status = "PAUSED"
        pause_triggered = True

    conn = db.connect()
    try:
        prev = conn.execute("SELECT status, started_at FROM bambu_jobs WHERE job_id = ?", (jid,)).fetchone()
    finally:
        conn.close()

    prev_status = str(prev["status"]) if prev else ""
    started_at = str(prev["started_at"]) if prev else ts
    finished_at = ts if norm_status in ("DONE", "FAILED") else None
    pct = 100.0 if norm_status == "DONE" else max(0.0, min(100.0, float(progress_pct)))
    detail = _describe_anomaly(anom_clean) if anom_clean else ""

    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO bambu_jobs (
                job_id, name, status, progress_pct, spool_id, used_grams,
                snapshot_path, started_at, finished_at, detail
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO UPDATE SET
                name = excluded.name,
                status = excluded.status,
                progress_pct = excluded.progress_pct,
                spool_id = excluded.spool_id,
                used_grams = excluded.used_grams,
                snapshot_path = excluded.snapshot_path,
                finished_at = excluded.finished_at,
                detail = excluded.detail
            """,
            (jid, name.strip(), norm_status, pct, spool_id.strip(), float(used_grams), snapshot_path.strip(), started_at, finished_at, detail),
        )

    filament_update: Optional[dict[str, Any]] = None
    if norm_status == "DONE" and prev_status != "DONE" and spool_id.strip() and float(used_grams) > 0:
        filament_update = consume_filament(db, spool_id.strip(), float(used_grams), stage_restock=True)

    alert_sent = False
    alert_level = ""
    alert_text = ""
    push_result: Optional[dict[str, Any]] = None
    enabled = is_module_enabled(db, "bambu_copilot")

    if anom_clean or norm_status in ("PAUSED", "FAILED"):
        alert_level = "now"
        reason_s = detail or f"任务进入 {norm_status}"
        snap_s = f"\n• 📷 现场抓拍：`{snapshot_path}`" if snapshot_path else ""
        alert_text = (
            f"🚨 **Bambu 3D 打印异常告警**：`{name}`（{jid}）\n"
            f"• 状态：**{norm_status}**（进度 {pct:.0f}%）｜ 原因：**{reason_s}**{snap_s}\n"
            "• 🛑 已建议/下发暂停指令，请核查打印床与料盘状态。"
        )
        if notify and enabled:
            push_result = push_message(
                db,
                text=alert_text,
                rule_id="bambu_job_anomaly",
                ref_id=f"bambu:anom:{jid}:{norm_status}",
                level="now",
                dedup_window=timedelta(minutes=10),
                hard=True,
            )
            alert_sent = bool(push_result.get("ok"))
    elif norm_status == "DONE":
        alert_level = "normal"
        snap_s = f"\n• 📷 完工快照：`{snapshot_path}`" if snapshot_path else ""
        spool_s = f" · 消耗 `{used_grams:.0f}g`" if used_grams > 0 else ""
        restock_s = ""
        if filament_update and filament_update.get("restock_approval"):
            tok = filament_update["restock_approval"]["token"]
            restock_s = f"\n• 🛒 耗材低库存已预制京东补货卡：`apply {tok}`"
        alert_text = f"✅ **Bambu 3D 打印完工**：`{name}`（{jid}{spool_s}）{snap_s}{restock_s}"
        if notify and enabled:
            push_result = push_message(
                db,
                text=alert_text,
                rule_id="bambu_job_done",
                ref_id=f"bambu:done:{jid}",
                level="normal",
                dedup_window=timedelta(hours=12),
            )
            alert_sent = bool(push_result.get("ok"))

    return {
        "ok": True,
        "job_id": jid,
        "name": name.strip(),
        "status": norm_status,
        "progress_pct": pct,
        "spool_id": spool_id.strip(),
        "used_grams": float(used_grams),
        "snapshot_path": snapshot_path.strip(),
        "anomaly": anom_clean,
        "pause_triggered": pause_triggered,
        "detail": detail,
        "alert_sent": alert_sent,
        "alert_level": alert_level,
        "alert_text": alert_text,
        "push_result": push_result,
        "filament_update": filament_update,
    }


def format_bambu_report(db: SimingDB) -> str:
    """生成 3D 打印车间任务进度与耗材库存简报。"""
    db.init_db()
    conn = db.connect()
    try:
        jobs = [dict(r) for r in conn.execute("SELECT * FROM bambu_jobs ORDER BY started_at DESC LIMIT 5").fetchall()]
        spools = [dict(r) for r in conn.execute("SELECT * FROM bambu_spools ORDER BY remaining_grams ASC").fetchall()]
    finally:
        conn.close()

    lines = ["🖨️ **Bambu 3D 3D 打印车间状态面板**"]
    if not jobs:
        lines.append("• 打印任务：暂无记录")
    else:
        lines.append("• **近期打印任务**：")
        for j in jobs:
            badge = {"RUNNING": "🔵", "PAUSED": "🟠", "DONE": "🟢", "FAILED": "🔴"}.get(j["status"], "⚪")
            extra = f" · {j['detail']}" if j.get("detail") else ""
            snap = f" · 快照 `{j['snapshot_path']}`" if j.get("snapshot_path") else ""
            lines.append(f"  - {badge} `{j['job_id']}` **{j['name']}** — {j['status']} ({j['progress_pct']:.0f}%{extra}{snap})")

    if not spools:
        lines.append("• 耗材库存：暂无登记料盘")
    else:
        lines.append("• **耗材料盘与干燥箱**：")
        for s in spools:
            low_flag = " ⚠️低库存" if float(s["remaining_grams"]) <= float(s["low_threshold_grams"]) else ""
            hum_flag = " 💧高湿" if float(s["drybox_humidity_pct"]) > DRYBOX_HUMIDITY_WARN_PCT else ""
            lines.append(
                f"  - `{s['spool_id']}` {s['brand']} {s['material']} {s['color']}："
                f"余 **{s['remaining_grams']:.0f}g** / {s['initial_grams']:.0f}g · 湿度 {s['drybox_humidity_pct']:.0f}%{low_flag}{hum_flag}"
            )
    return "\n".join(lines)
