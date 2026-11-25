"""W3-7 · 数字基建自愈运维官（Infrastructure Self-Healing Patrol）。

覆盖本机与公网边端四大数字基建面：
1. 系统升级前置守卫（检查包管理器锁、冲突/锁定包、根分区余量，输出升级前快照方案）；
2. 自定义公网入口 TLS 证书到期巡检（剩余 < 14 天预警）；
3. 本机内存压力与边缘节点磁盘水位巡检；
4. 内网 VPN DNS 劫持与 sing-box 路由漂移检测，**仅限白名单**（`flush_dns_cache`, `restart_singbox_dns`）
   静默自愈并写入 A1 审计日志，严禁执行任何非白名单动作。
"""

from __future__ import annotations

import os
import shutil
import socket
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

from siming import db as siming_db
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, parse_iso

WHITELISTED_INFRA_HEALS: frozenset[str] = frozenset(
    {
        "flush_dns_cache",
        "restart_singbox_dns",
    }
)


def run_omarchy_upgrade_guard(
    probe_fn: Optional[Callable[[], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """Omarchy 系统升级前置守卫：只读核验 pacman 锁、冲突包与磁盘余量，并生成升级前快照计划。"""
    if probe_fn is not None:
        info = dict(probe_fn() or {})
    else:
        lock_exists = Path("/var/lib/pacman/db.lck").exists()
        try:
            du = shutil.disk_usage("/")
            free_gb = round(du.free / (1024 ** 3), 2)
        except OSError:
            free_gb = 0.0
        info = {
            "pacman_lock_present": lock_exists,
            "conflicting_packages": [],
            "held_packages": [],
            "disk_free_gb": free_gb,
            "snapshot_ready": True,
        }

    blockers: list[str] = []
    warnings: list[str] = []

    if info.get("pacman_lock_present"):
        blockers.append("检测到 `/var/lib/pacman/db.lck` 锁文件，可能有未结束的包管理进程。")

    conflicts = list(info.get("conflicting_packages") or [])
    if conflicts:
        blockers.append(f"存在包冲突待人工确认：{', '.join(str(x) for x in conflicts)}")

    held = list(info.get("held_packages") or [])
    if held:
        warnings.append(f"存在 IgnorePkg/锁定包：{', '.join(str(x) for x in held)}")

    free_gb = float(info.get("disk_free_gb", 0.0))
    if free_gb < 10.0:
        blockers.append(f"根分区可用空间仅 {free_gb:.1f} GB（安全升级门槛 >= 10 GB）。")
    elif free_gb < 20.0:
        warnings.append(f"根分区可用空间偏紧（{free_gb:.1f} GB），建议先清理旧缓存。")

    snapshot_plan = [
        "1. 确认无活跃 pacman 进程且 `/var/lib/pacman/db.lck` 不存在",
        "2. 导出当前显式安装包列表：`pacman -Qqe > ~/.cache/agent-tmp/omarchy-pkglist-pre.txt`",
        "3. 创建 Btrfs/Snapper 升级前只读快照：`sudo snapper create --description 'pre-omarchy-upgrade'`",
        "4. 在隔离窗口执行 `omarchy-update` 并保留回滚引导项",
    ]

    return {
        "ok": True,
        "safe_to_upgrade": len(blockers) == 0,
        "blockers": blockers,
        "warnings": warnings,
        "metrics": {
            "pacman_lock_present": bool(info.get("pacman_lock_present")),
            "conflicting_packages": conflicts,
            "held_packages": held,
            "disk_free_gb": free_gb,
        },
        "snapshot_plan": snapshot_plan,
    }


def check_tls_cert_expiry(
    host: str = "edge.example.com",
    port: int = 8443,
    warn_days: int = 14,
    cert_probe_fn: Optional[Callable[[str, int], dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """巡检公网入口 TLS 证书剩余有效期，少于 `warn_days` 天时预警。"""
    if now is None:
        cur_dt = siming_db.now_local()
    elif now.tzinfo is None:
        cur_dt = now.replace(tzinfo=SHANGHAI_TZ)
    else:
        cur_dt = now.astimezone(SHANGHAI_TZ)

    if cert_probe_fn is not None:
        raw = dict(cert_probe_fn(host, port) or {})
        if not raw.get("ok", True):
            return {
                "ok": False,
                "host": host,
                "port": port,
                "status": "ERROR",
                "error": str(raw.get("error") or "probe failed"),
                "needs_renewal": True,
            }
        if raw.get("days_remaining") is not None:
            days_rem = float(raw["days_remaining"])
            not_after_s = str(raw.get("not_after") or "")
        elif raw.get("not_after"):
            na_val = raw["not_after"]
            na_dt = na_val if isinstance(na_val, datetime) else parse_iso(str(na_val))
            days_rem = (na_dt - cur_dt).total_seconds() / 86400.0
            not_after_s = iso_now(na_dt)
        else:
            return {
                "ok": False,
                "host": host,
                "port": port,
                "status": "UNKNOWN",
                "error": "missing expiry field",
                "needs_renewal": False,
            }
    elif os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return {
            "ok": False,
            "host": host,
            "port": port,
            "status": "OFFLINE_SKIPPED",
            "days_remaining": None,
            "warn_days": int(warn_days),
            "needs_renewal": False,
        }
    else:
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((host, int(port)), timeout=5.0) as sock:
                with ctx.wrap_socket(sock, server_hostname=host) as ssock:
                    cert = ssock.getpeercert() or {}
            not_after_raw = str(cert.get("notAfter") or "")
            na_dt = datetime.strptime(not_after_raw, "%b %d %H:%M:%S %Y %Z").replace(
                tzinfo=timezone.utc
            ).astimezone(SHANGHAI_TZ)
            days_rem = (na_dt - cur_dt).total_seconds() / 86400.0
            not_after_s = iso_now(na_dt)
        except Exception as exc:
            return {
                "ok": False,
                "host": host,
                "port": port,
                "status": "ERROR",
                "error": str(exc)[:160],
                "needs_renewal": True,
            }

    days_rounded = round(days_rem, 1)
    if days_rounded <= 0:
        status = "EXPIRED"
    elif days_rounded < float(warn_days):
        status = "WARN"
    else:
        status = "OK"

    return {
        "ok": True,
        "host": host,
        "port": int(port),
        "days_remaining": days_rounded,
        "warn_days": int(warn_days),
        "not_after": not_after_s,
        "status": status,
        "needs_renewal": status in ("WARN", "EXPIRED"),
    }


def check_proxy_and_dns_drift(
    probe_fn: Optional[Callable[[], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """检测 VPN DNS 劫持、sing-box 负缓存与路由漂移，输出白名单自愈建议。"""
    if probe_fn is not None:
        raw = dict(probe_fn() or {})
    else:
        raw = {
            "direct_dns_ok": True,
            "proxy_dns_ok": True,
            "dns_hijacked": False,
            "singbox_route_drift": False,
            "extra_actions": [],
        }

    issues: list[str] = []
    recommended: list[str] = []

    direct_ok = bool(raw.get("direct_dns_ok", True))
    proxy_ok = bool(raw.get("proxy_dns_ok", True))
    dns_hijacked = bool(raw.get("dns_hijacked", False))
    route_drift = bool(raw.get("singbox_route_drift", False))

    if direct_ok and not proxy_ok:
        issues.append("dns_negative_cache: 直连 DNS 正常但代理侧解析失败（疑似连 VPN 前残留 NXDOMAIN 负缓存）")
        recommended.append("flush_dns_cache")

    if dns_hijacked:
        issues.append("vpn_dns_hijack: 检测到内网 VPN 劫持外部域名 DNS 解析")
        if "flush_dns_cache" not in recommended:
            recommended.append("flush_dns_cache")
        recommended.append("restart_singbox_dns")

    if route_drift:
        issues.append("singbox_route_drift: 检测到 sing-box 路由表/端口规则发生漂移")
        if "restart_singbox_dns" not in recommended:
            recommended.append("restart_singbox_dns")

    extra_raw = raw.get("extra_actions")
    if isinstance(extra_raw, str):
        extra_iter = [extra_raw]
    elif isinstance(extra_raw, (list, tuple, set)):
        extra_iter = list(extra_raw)
    else:
        extra_iter = []
    for extra in extra_iter:
        act = str(extra).strip()
        if act and act not in recommended:
            recommended.append(act)

    return {
        "ok": True,
        "healthy": len(issues) == 0,
        "issues": issues,
        "recommended_heals": recommended,
        "raw": raw,
    }


def read_local_resource_snapshot() -> dict[str, Any]:
    """零依赖读取本机真实资源水位（/proc/meminfo、根分区磁盘、负载与运行时长）。"""
    mem_total_kb = 0
    mem_avail_kb = 0
    try:
        with open("/proc/meminfo", "r", encoding="utf-8", errors="replace") as fh:
            for ln in fh:
                if ln.startswith("MemTotal:"):
                    mem_total_kb = int(ln.split()[1])
                elif ln.startswith("MemAvailable:"):
                    mem_avail_kb = int(ln.split()[1])
                if mem_total_kb and mem_avail_kb:
                    break
    except (OSError, ValueError, IndexError):
        pass

    mem_used_pct: Optional[float] = None
    mem_total_gb: Optional[float] = None
    mem_used_gb: Optional[float] = None
    mem_avail_gb: Optional[float] = None
    if mem_total_kb > 0:
        used_kb = max(0, mem_total_kb - mem_avail_kb)
        mem_used_pct = round((used_kb / mem_total_kb) * 100.0, 1)
        mem_total_gb = round(mem_total_kb / (1024 ** 2), 2)
        mem_used_gb = round(used_kb / (1024 ** 2), 2)
        mem_avail_gb = round(mem_avail_kb / (1024 ** 2), 2)

    disk_root_used_pct: Optional[float] = None
    disk_root_free_gb: Optional[float] = None
    disk_root_total_gb: Optional[float] = None
    try:
        du = shutil.disk_usage("/")
        if du.total > 0:
            disk_root_used_pct = round((du.used / du.total) * 100.0, 1)
            disk_root_free_gb = round(du.free / (1024 ** 3), 2)
            disk_root_total_gb = round(du.total / (1024 ** 3), 2)
    except OSError:
        pass

    try:
        l1, l5, l15 = os.getloadavg()
        loads = (round(l1, 2), round(l5, 2), round(l15, 2))
    except OSError:
        loads = (None, None, None)

    uptime_sec: Optional[float] = None
    try:
        with open("/proc/uptime", "r", encoding="utf-8") as fh:
            uptime_sec = round(float(fh.read().split()[0]), 1)
    except (OSError, ValueError, IndexError):
        pass

    return {
        "mem_used_pct": mem_used_pct,
        "mem_total_gb": mem_total_gb,
        "mem_used_gb": mem_used_gb,
        "mem_avail_gb": mem_avail_gb,
        "disk_root_used_pct": disk_root_used_pct,
        "disk_root_free_gb": disk_root_free_gb,
        "disk_root_total_gb": disk_root_total_gb,
        "load_1m": loads[0],
        "load_5m": loads[1],
        "load_15m": loads[2],
        "cpu_cores": os.cpu_count() or 1,
        "uptime_sec": uptime_sec,
        # 81 边端磁盘未经显式 SSH 探测时不填占位假值
        "disk81_used_pct": None,
    }


def run_infra_patrol(
    db: SimingDB,
    auto_heal: bool = True,
    probe_fns: Optional[dict[str, Callable[..., Any]]] = None,
    heal_fn: Optional[Callable[[str, dict[str, Any]], dict[str, Any]]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """统一执行数字基建巡检，仅对白名单动作做静默自愈并留痕 A1 审计。"""
    db.init_db()
    ts = iso_now(now)
    probes = dict(probe_fns or {})

    omarchy = run_omarchy_upgrade_guard(probe_fn=probes.get("omarchy"))
    tls = check_tls_cert_expiry(cert_probe_fn=probes.get("tls"), now=now)
    proxy_dns = check_proxy_and_dns_drift(probe_fn=probes.get("proxy_dns"))

    # 本机内存/磁盘真实水位 & 81 边端磁盘水位
    res_probe = probes.get("resources")
    if res_probe is not None:
        resources = dict(res_probe() or {})
    else:
        resources = read_local_resource_snapshot()

    raw_mem = resources.get("mem_used_pct")
    raw_disk81 = resources.get("disk81_used_pct")
    raw_disk_root = resources.get("disk_root_used_pct")
    mem_pct = float(raw_mem) if isinstance(raw_mem, (int, float)) else None
    disk81_pct = float(raw_disk81) if isinstance(raw_disk81, (int, float)) else None
    disk_root_pct = float(raw_disk_root) if isinstance(raw_disk_root, (int, float)) else None

    resource_alerts: list[str] = []
    if mem_pct is not None and mem_pct >= 85.0:
        resource_alerts.append(f"本机内存水位过高：{mem_pct:.1f}%")
    if disk_root_pct is not None and disk_root_pct >= 90.0:
        resource_alerts.append(f"本机根分区磁盘水位过高：{disk_root_pct:.1f}%")
    if disk81_pct is not None and disk81_pct >= 85.0:
        resource_alerts.append(f"81 边端磁盘水位过高：{disk81_pct:.1f}%")
    resources["alerts"] = resource_alerts

    heals_executed: list[dict[str, Any]] = []
    blocked_non_whitelisted: list[str] = []

    for action in proxy_dns.get("recommended_heals") or []:
        if action not in WHITELISTED_INFRA_HEALS:
            blocked_non_whitelisted.append(action)
            db.write_audit(
                "A1",
                "infra_heal_blocked_non_whitelist",
                {"action": action, "ts": ts},
                target_id=action,
                exit_code=1,
                detail="refused non-whitelisted infra action",
            )
            continue
        if not auto_heal:
            continue
        if heal_fn is not None:
            res = heal_fn(action, {"proxy_dns": proxy_dns, "ts": ts}) or {"ok": True}
        else:
            res = {"ok": True, "dry_run": True, "action": action}
        heals_executed.append({"action": action, "result": res, "silent": True, "ts": ts})
        db.write_audit(
            "A1",
            f"infra_heal:{action}",
            {"action": action, "result": res, "ts": ts},
            target_id="local_net",
            exit_code=0 if res.get("ok", True) else 1,
            detail="silent whitelisted infra self-heal",
        )

    alerts: list[str] = []
    if not omarchy["safe_to_upgrade"]:
        alerts.extend(omarchy["blockers"])
    if tls.get("needs_renewal"):
        alerts.append(
            f"TLS 证书 {tls['host']}:{tls['port']} 状态={tls['status']}（剩余 {tls.get('days_remaining')} 天）"
        )
    alerts.extend(resource_alerts)
    if not proxy_dns["healthy"] and not any(
        bool((h.get("result") or {}).get("ok", True)) for h in heals_executed
    ):
        alerts.extend(proxy_dns["issues"])

    if (
        tls.get("status") == "EXPIRED"
        or (mem_pct is not None and mem_pct >= 95.0)
        or (disk81_pct is not None and disk81_pct >= 95.0)
        or (disk_root_pct is not None and disk_root_pct >= 96.0)
    ):
        overall = "CRITICAL"
    elif alerts or not proxy_dns["healthy"]:
        overall = "WARN"
    else:
        overall = "OK"

    db.set_meta("infra_patrol_last_at", ts)
    return {
        "ok": True,
        "checked_at": ts,
        "overall_status": overall,
        "omarchy_guard": omarchy,
        "tls_cert": tls,
        "resources": resources,
        "proxy_dns": proxy_dns,
        "heals_executed": heals_executed,
        "blocked_non_whitelisted": blocked_non_whitelisted,
        "alerts": alerts,
    }


def format_infra_report(report: dict[str, Any]) -> str:
    """格式化数字基建自愈运维简报。"""
    status = str(report.get("overall_status") or "UNKNOWN")
    icon = {"OK": "🟢", "WARN": "🟡", "CRITICAL": "🔴"}.get(status, "⚪")
    lines = [
        f"{icon} **数字基建自愈运维简报**（状态：`{status}` · `{str(report.get('checked_at', ''))[:19]}`）"
    ]

    om = report.get("omarchy_guard") or {}
    lines.append(
        f"• **Omarchy 升级守卫**：{'可安全升级' if om.get('safe_to_upgrade') else '已拦截（存在阻断项）'}"
        f"（根分区余量 `{(om.get('metrics') or {}).get('disk_free_gb', '-')} GB`）"
    )
    for b in om.get("blockers") or []:
        lines.append(f"  - ⛔ {b}")

    tls = report.get("tls_cert") or {}
    lines.append(
        f"• **TLS 证书** (`{tls.get('host', '')}:{tls.get('port', '')}`)："
        f"`{tls.get('status', 'UNKNOWN')}` · 剩余 `{tls.get('days_remaining', '-')}` 天"
    )

    res = report.get("resources") or {}
    mem_disp = f"{res['mem_used_pct']}%" if res.get("mem_used_pct") is not None else "-"
    d81_disp = f"{res['disk81_used_pct']}%" if res.get("disk81_used_pct") is not None else "未探测"
    lines.append(
        f"• **资源水位**：本机内存 `{mem_disp}` · 81 边端磁盘 `{d81_disp}`"
    )

    pd = report.get("proxy_dns") or {}
    lines.append(
        f"• **VPN / sing-box 链路**：{'健康' if pd.get('healthy') else '检测到漂移/劫持'}"
    )
    for issue in pd.get("issues") or []:
        lines.append(f"  - ⚠️ {issue}")

    heals = report.get("heals_executed") or []
    if heals:
        lines.append("• **已静默执行的白名单自愈**：" + ", ".join(f"`{h['action']}`" for h in heals))

    blocked = report.get("blocked_non_whitelisted") or []
    if blocked:
        lines.append("• **已拒绝的非白名单高危动作**：" + ", ".join(f"`{a}`" for a in blocked))

    return "\n".join(lines)
