"""W2-2 (`homelab_ops`)：家庭内网（RB5009 / 光猫桥接 / NAS / Sure）巡检与三地加密灾备编排。"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from siming import db as _db_mod
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, parse_iso
from siming.modules import is_module_enabled
from siming.push import push_message

DISK_TEMP_WARN_C = 50.0
BACKUP_MAX_STALE_HOURS = 48.0


def _coerce_now(now: Optional[datetime | str] = None) -> datetime:
    if now is None:
        return _db_mod.now_local()
    if isinstance(now, str):
        return parse_iso(now)
    if now.tzinfo is None:
        return now.replace(tzinfo=SHANGHAI_TZ)
    return now.astimezone(SHANGHAI_TZ)


def record_homelab_node(
    db: SimingDB,
    node_id: str,
    kind: str,
    name: str,
    status: str,
    metrics: Optional[dict[str, Any]] = None,
    detail: str = "",
    checked_at: Optional[str] = None,
) -> dict[str, Any]:
    """登记或刷新家庭实验室节点（router / nas / modem / vps / backup）状态与指标。"""
    db.init_db()
    ts = checked_at or iso_now()
    norm_status = (status or "UNKNOWN").strip().upper()
    if norm_status not in ("OK", "WARN", "ERROR", "UNKNOWN"):
        norm_status = "ERROR" if norm_status in ("DOWN", "OFFLINE", "FAIL", "FAILED") else "UNKNOWN"
    metrics_obj = dict(metrics or {})

    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO homelab_nodes (node_id, kind, name, status, metrics_json, last_checked_at, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_id) DO UPDATE SET
                kind = excluded.kind,
                name = excluded.name,
                status = excluded.status,
                metrics_json = excluded.metrics_json,
                last_checked_at = excluded.last_checked_at,
                detail = excluded.detail
            """,
            (
                node_id.strip(),
                kind.strip().lower(),
                name.strip(),
                norm_status,
                json.dumps(metrics_obj, ensure_ascii=False, sort_keys=True),
                ts,
                detail.strip(),
            ),
        )
        db.write_audit(
            "A0",
            "homelab_node_record",
            {"node_id": node_id, "kind": kind, "status": norm_status},
            target_id=node_id,
            detail=detail[:200],
            conn=conn,
        )

    return {
        "ok": True,
        "node_id": node_id.strip(),
        "kind": kind.strip().lower(),
        "name": name.strip(),
        "status": norm_status,
        "metrics": metrics_obj,
        "last_checked_at": ts,
        "detail": detail.strip(),
    }


def _safe_int(val: Any, default: int = 0) -> int:
    try:
        return int(float(val))
    except (TypeError, ValueError):
        return default


def _safe_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _extract_disk_issues(node_name: str, node_id: str, metrics: dict[str, Any]) -> list[dict[str, Any]]:
    alerts: list[dict[str, Any]] = []

    bad_sectors = _safe_int(metrics.get("smart_bad_sectors") or metrics.get("bad_sectors") or metrics.get("reallocated_sectors") or 0)
    if bad_sectors > 0:
        alerts.append(
            {
                "node_id": node_id,
                "node_name": node_name,
                "code": "smart_bad_sectors",
                "severity": "critical",
                "message": f"{node_name} 检测到 SMART 坏道 {bad_sectors} 个，请立即安排更换硬盘并核验 RAID 同步。",
            }
        )

    disk_temp = _safe_float(metrics.get("disk_temp_c"))
    if disk_temp is not None and disk_temp >= DISK_TEMP_WARN_C:
        alerts.append(
            {
                "node_id": node_id,
                "node_name": node_name,
                "code": "disk_overheat",
                "severity": "critical" if disk_temp >= 58.0 else "warn",
                "message": f"{node_name} 硬盘温度 {disk_temp:.1f}°C（≥ {DISK_TEMP_WARN_C:.0f}°C 阈值），请检查 NAS 风扇与风道。",
            }
        )

    for disk in metrics.get("disks") or []:
        if not isinstance(disk, dict):
            continue
        d_label = str(disk.get("slot") or disk.get("device") or "disk")
        d_bad = _safe_int(disk.get("bad_sectors") or disk.get("reallocated_sectors") or 0)
        if d_bad > 0:
            alerts.append(
                {
                    "node_id": node_id,
                    "node_name": node_name,
                    "code": "smart_bad_sectors",
                    "severity": "critical",
                    "message": f"{node_name} 盘位 {d_label} SMART 坏道 {d_bad} 个！",
                }
            )
        d_temp = _safe_float(disk.get("temp_c"))
        if d_temp is not None and d_temp >= DISK_TEMP_WARN_C:
            alerts.append(
                {
                    "node_id": node_id,
                    "node_name": node_name,
                    "code": "disk_overheat",
                    "severity": "critical" if d_temp >= 58.0 else "warn",
                    "message": f"{node_name} 盘位 {d_label} 温度 {d_temp:.1f}°C（≥ {DISK_TEMP_WARN_C:.0f}°C）",
                }
            )

    raid_status = str(metrics.get("raid_status") or "").upper()
    if metrics.get("raid_degraded") or raid_status in ("DEGRADED", "FAILED", "ERROR"):
        alerts.append(
            {
                "node_id": node_id,
                "node_name": node_name,
                "code": "raid_degraded",
                "severity": "critical",
                "message": f"{node_name} RAID 阵列降级/异常（status={raid_status or 'DEGRADED'}）！",
            }
        )

    sure_status = str(metrics.get("sure_status") or "").upper()
    if metrics.get("sure_healthy") is False or sure_status in ("DOWN", "UNHEALTHY", "ERROR"):
        alerts.append(
            {
                "node_id": node_id,
                "node_name": node_name,
                "code": "sure_container_unhealthy",
                "severity": "warn",
                "message": f"{node_name} Sure 容器健康检查异常（{sure_status or 'unhealthy'}），请检查 docker compose logs sure-web。",
            }
        )
    return alerts


def evaluate_homelab_health(
    db: SimingDB,
    now: Optional[datetime | str] = None,
    notify: bool = False,
) -> dict[str, Any]:
    """全量巡检 RB5009 路由器、光猫、UGREEN NAS（SMART 坏道/高温/RAID/Sure）与 3 地备份时效（>48h）。"""
    db.init_db()
    cur = _coerce_now(now)
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM homelab_nodes ORDER BY kind, node_id").fetchall()
    finally:
        conn.close()

    nodes: list[dict[str, Any]] = []
    alerts: list[dict[str, Any]] = []

    for r in rows:
        try:
            metrics = json.loads(r["metrics_json"] or "{}")
            if not isinstance(metrics, dict):
                metrics = {}
        except (ValueError, TypeError):
            metrics = {}
        node_info = {
            "node_id": r["node_id"],
            "kind": r["kind"],
            "name": r["name"],
            "status": r["status"],
            "metrics": metrics,
            "last_checked_at": r["last_checked_at"],
            "detail": r["detail"],
        }
        nodes.append(node_info)

        if r["status"] == "ERROR" or metrics.get("online") is False:
            alerts.append(
                {
                    "node_id": r["node_id"],
                    "node_name": r["name"],
                    "code": "node_offline",
                    "severity": "critical",
                    "message": f"{r['name']}（{r['kind']}）离线或处于 ERROR 状态：{r['detail'] or '无详细说明'}",
                }
            )
        elif r["status"] == "WARN":
            alerts.append(
                {
                    "node_id": r["node_id"],
                    "node_name": r["name"],
                    "code": "node_warn",
                    "severity": "warn",
                    "message": f"{r['name']}（{r['kind']}）处于 WARN 状态：{r['detail'] or '请核查指标'}",
                }
            )

        if r["kind"] == "nas" or any(k in metrics for k in ("smart_bad_sectors", "bad_sectors", "disk_temp_c", "disks", "raid_status", "sure_healthy")):
            alerts.extend(_extract_disk_issues(r["name"], r["node_id"], metrics))

        backup_ts_raw = metrics.get("last_backup_at") or (r["last_checked_at"] if r["kind"] == "backup" else "")
        if backup_ts_raw:
            try:
                b_dt = parse_iso(str(backup_ts_raw))
                age_hours = (cur - b_dt).total_seconds() / 3600.0
                if age_hours > BACKUP_MAX_STALE_HOURS:
                    alerts.append(
                        {
                            "node_id": r["node_id"],
                            "node_name": r["name"],
                            "code": "backup_stale",
                            "severity": "critical" if age_hours > 96.0 else "warn",
                            "message": f"{r['name']} 最近成功备份距今已 {age_hours:.1f} 小时（超过 {BACKUP_MAX_STALE_HOURS:.0f}h 阈值）。",
                        }
                    )
            except Exception:
                pass

    critical_count = sum(1 for a in alerts if a["severity"] == "critical")
    warn_count = sum(1 for a in alerts if a["severity"] == "warn")
    overall = "ERROR" if critical_count > 0 else ("WARN" if warn_count > 0 else "OK")

    push_result: Optional[dict[str, Any]] = None
    if notify and alerts and is_module_enabled(db, "homelab_ops"):
        lines = [f"🖥️ **家庭内网与三地灾备巡检告警**（状态：`{overall}`）"]
        for a in alerts[:6]:
            icon = "🔴" if a["severity"] == "critical" else "🟠"
            lines.append(f"• {icon} [{a['code']}] {a['message']}")
        push_result = push_message(
            db,
            text="\n".join(lines),
            rule_id="homelab_ops_alert",
            ref_id=f"homelab:{overall}:{len(alerts)}",
            level="now" if critical_count > 0 else "normal",
            dedup_window=timedelta(hours=2),
            hard=(critical_count > 0),
        )

    return {
        "ok": True,
        "overall_status": overall,
        "checked_at": iso_now(cur),
        "node_count": len(nodes),
        "nodes": nodes,
        "alerts": alerts,
        "critical_count": critical_count,
        "warn_count": warn_count,
        "push_result": push_result,
    }


def _hash_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _canonical_manifest_digest(files: list[dict[str, Any]]) -> str:
    canonical = json.dumps(
        sorted(files, key=lambda x: str(x.get("path", ""))),
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_three_site_backup_plan(
    db: SimingDB,
    sources: Optional[list[str | Path]] = None,
) -> dict[str, Any]:
    """构建确定性三地加密增量灾备方案（Desktop -> UGREEN NAS -> 边缘 VPS）与 SHA-256 校验清单。"""
    db.init_db()
    ts = iso_now()

    source_paths: list[Path] = []
    if sources is not None:
        for s in sources:
            p = Path(s)
            if p.is_dir():
                for child in sorted(p.rglob("*")):
                    if child.is_file():
                        source_paths.append(child)
            else:
                source_paths.append(p)
    else:
        if db.db_path.is_file():
            source_paths.append(db.db_path)

    file_entries: list[dict[str, Any]] = []
    for p in sorted(source_paths, key=lambda x: str(x)):
        exists = p.is_file()
        file_entries.append(
            {
                "path": str(p),
                "exists": exists,
                "size_bytes": p.stat().st_size if exists else 0,
                "sha256": _hash_file_sha256(p) if exists else "",
            }
        )

    manifest_sha256 = _canonical_manifest_digest(file_entries)
    plan_id = f"BKP3-{manifest_sha256[:10].upper()}"
    topology = ["desktop", "ugreen_nas", "vps_81"]
    commands = [
        "restic -r sftp:nas-homelab:/volume1/backups/siming-restic backup --tag desktop-to-nas <SOURCES>",
        "restic -r sftp:backup@81:/srv/restic/siming copy --from-repo /volume1/backups/siming-restic",
        "restic -r sftp:backup@81:/srv/restic/siming check --read-data-subset=5%",
    ]

    manifest = {
        "plan_id": plan_id,
        "created_at": ts,
        "topology": topology,
        "encryption": "restic-aes256-ctr-poly1305",
        "files": file_entries,
        "manifest_sha256": manifest_sha256,
    }

    manifest_path = db.paths.tmp_root / f"backup-manifest-{plan_id}.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    db.set_meta(
        "homelab:last_backup_plan",
        json.dumps({"plan_id": plan_id, "created_at": ts, "manifest_sha256": manifest_sha256, "file_count": len(file_entries)}, ensure_ascii=False),
    )

    return {
        "ok": True,
        "plan_id": plan_id,
        "topology": topology,
        "encryption": manifest["encryption"],
        "manifest_path": str(manifest_path),
        "manifest": manifest,
        "commands": commands,
    }


def verify_backup_manifest(db: SimingDB, manifest: dict[str, Any]) -> dict[str, Any]:
    """核验三地备份清单的顶层摘要与每个文件的真实磁盘 SHA-256。"""
    db.init_db()
    files = list(manifest.get("files") or [])
    expected_digest = str(manifest.get("manifest_sha256") or "")
    actual_digest = _canonical_manifest_digest(files)
    digest_ok = bool(expected_digest) and expected_digest == actual_digest

    mismatches: list[dict[str, Any]] = []
    missing_files: list[str] = []
    matched = 0

    for entry in files:
        p = Path(str(entry.get("path") or ""))
        expected_sha = str(entry.get("sha256") or "")
        if not p.is_file():
            missing_files.append(str(p))
            continue
        actual_sha = _hash_file_sha256(p)
        if actual_sha != expected_sha:
            mismatches.append(
                {
                    "path": str(p),
                    "expected_sha256": expected_sha,
                    "actual_sha256": actual_sha,
                }
            )
        else:
            matched += 1

    verified = digest_ok and not mismatches and not missing_files
    db.write_audit(
        "A0",
        "homelab_backup_verify",
        {
            "plan_id": manifest.get("plan_id", ""),
            "verified": verified,
            "checked_files": len(files),
            "mismatches": len(mismatches),
            "missing": len(missing_files),
        },
        target_id=str(manifest.get("plan_id") or "manifest"),
    )
    return {
        "ok": True,
        "verified": verified,
        "manifest_digest_ok": digest_ok,
        "checked_files": len(files),
        "matched_files": matched,
        "mismatches": mismatches,
        "missing_files": missing_files,
    }


def get_modem_bridge_playbook() -> str:
    """输出 `modem-bridge-tool` 光猫改桥接 + MikroTik RB5009 拨号组网实操核对清单。"""
    return "\n".join(
        [
            "🔌 **示例小区B光猫改桥接（`modem-bridge-tool`）+ MikroTik RB5009 核对清单**",
            "1. **备份与提权**：网线直连光猫 LAN1，使用 `modem-bridge-tool` 读取超级管理员密码并备份原厂配置与宽带 LOID / VLAN ID（通常上网 VLAN=3961 或 41）。",
            "2. **切换桥接模式**：登录光猫超管后台 -> 网络 -> 宽带设置 -> 选中 `INTERNET` 连接 -> 模式由 `Route` 改为 `Bridge`，关闭光猫 Wi-Fi 与 DHCP，保留 TR069 仅作远程诊断或按需解绑。",
            "3. **RB5009 PPPoE 拨号**：`ether1` 接光猫千兆/2.5G 口 -> 配置 VLAN 接口与 `pppoe-out1`（填入宽带账号密码，开启 `add-default-route=yes` 与 MTU=1492/1480 钳制 MSS）。",
            "4. **IPv6 与内网隔离**：启用 `ipv6 dhcp-client` 请求 `/56` 或 `/60` 前缀；规划 VLAN（主网 / IoT-HAOS / NAS-存储）并允许 Tailscale 子网路由指向 NAS。",
            "5. **验收回归**：确认公网/Tailscale 连通、`siming` 可达 HAOS (`8123`) 与 Sure (`3000`)，且光猫桥接后无双重 NAT。",
        ]
    )


def format_homelab_report(db: SimingDB) -> str:
    """生成家庭内网与三地灾备巡检简报。"""
    health = evaluate_homelab_health(db, notify=False)
    badge = {"OK": "🟢 全部健康", "WARN": "🟠 存在预警", "ERROR": "🔴 存在故障"}.get(health["overall_status"], "⚪ 未知")
    lines = [f"🖥️ **家庭内网与三地灾备状态面板**（{badge} · 共 {health['node_count']} 节点）"]

    if not health["nodes"]:
        lines.append("• 尚未登记节点（可登记 RB5009 / 光猫 / NAS / 边缘 VPS / 备份任务）。")
    else:
        for n in health["nodes"]:
            icon = {"OK": "🟢", "WARN": "🟠", "ERROR": "🔴"}.get(n["status"], "⚪")
            m = n["metrics"]
            extra_parts: list[str] = []
            if "disk_temp_c" in m:
                extra_parts.append(f"盘温 {m['disk_temp_c']}°C")
            if "smart_bad_sectors" in m:
                extra_parts.append(f"坏道 {m['smart_bad_sectors']}")
            if "raid_status" in m:
                extra_parts.append(f"RAID {m['raid_status']}")
            if "last_backup_at" in m:
                extra_parts.append(f"最近备份 {str(m['last_backup_at'])[:16]}")
            extra = f"（{' · '.join(extra_parts)}）" if extra_parts else ""
            lines.append(f"• {icon} **{n['name']}** [`{n['kind']}`] — {n['status']}{extra}")

    if health["alerts"]:
        lines.append("• ⚠️ **当前告警**：")
        for a in health["alerts"]:
            lines.append(f"  - [{a['severity'].upper()}] {a['message']}")

    last_plan_raw = db.get_meta("homelab:last_backup_plan")
    if last_plan_raw:
        try:
            lp = json.loads(last_plan_raw)
            lines.append(f"• 📦 最近三地备份方案：`{lp['plan_id']}`（{lp['file_count']} 文件 · SHA256 `{lp['manifest_sha256'][:12]}…`）")
        except Exception:
            pass
    return "\n".join(lines)
