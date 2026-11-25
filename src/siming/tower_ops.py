"""司命控制塔 · 运维与调度面板聚合引擎（方案 A：司命自运维 + 三合一 Cron/Timer 只读总表 + 专业工具外链）。

设计边界与安全铁律：
1. 不重造 Beszel / Uptime Kuma / Backrest / Cockpit：只提供轻量本机资源摘要条 + 专业监控工具直达外链；
2. 三合一调度总表（systemd user timers + Hermes cron jobs + 司命 waits）严格只读，高亮「直推 QQ」与「失败/超时」任务；
3. 日志/产物预览严格走白名单与路径边界（systemd 仅限实际发现的 user timer 激活单元，Hermes 仅限 jobs.json 中存在的 job_id）并全程脱敏；
4. 仅开放 3 个司命专属安全写操作（恢复 push_guard 规则、暂停/恢复后台 worker、触发只读链路诊断或白名单 DNS 清缓存），严禁任意 shell / 任意 systemd 服务启停 / Web 端编辑 crontab。
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from siming import db as _db_mod
from siming.cn_calendar import parse_duration_seconds
from siming.config import load_policy
from siming.cron_harvest import DEFAULT_JOBS_JSON, DEFAULT_OUTPUT_ROOT, HARVEST_JOBS, parse_cron_output
from siming.crypto import redact_sensitive
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, parse_iso
from siming.doctor import run_doctor
from siming.infra_healer import read_local_resource_snapshot
from siming.push_guard import guard_status, restore_rule
from siming.self_heal import diagnose_and_heal_dev_links
from siming.watchdog import check_cdp_9222_status

_HERMES_JOB_ID_RE = re.compile(r"^[A-Za-z0-9_-]{4,64}$")
_SYSTEMD_UNIT_RE = re.compile(r"^[A-Za-z0-9@_.-]{3,128}\.service$")
_SIMING_CORE_TIMERS = (
    "siming-tick.timer",
    "siming-edge-sync.timer",
    "siming-watchdog.timer",
    "siming-backup.timer",
)
_DIRECT_QQ_UNIT_HINTS = ("-notify", "qq-status", "qq_status", "qqbot")

DEFAULT_EXTERNAL_TOOLS: tuple[dict[str, str], ...] = (
    {
        "id": "beszel",
        "name": "Beszel",
        "role": "本机资源与容器指标",
        "url": "http://127.0.0.1:8090",
        "icon": "📊",
    },
    {
        "id": "uptime_kuma",
        "name": "Uptime Kuma",
        "role": "服务探活与宕机告警",
        "url": "http://127.0.0.1:3001",
        "icon": "🟢",
    },
    {
        "id": "backrest",
        "name": "Backrest",
        "role": "Restic 加密备份管理",
        "url": "http://127.0.0.1:9898",
        "icon": "💾",
    },
    {
        "id": "hermes_dashboard",
        "name": "Hermes Dashboard",
        "role": "Hermes Cron 与看板",
        "url": "http://127.0.0.1:9119",
        "icon": "🤖",
    },
    {
        "id": "homepage",
        "name": "Homepage",
        "role": "本机个人服务总入口",
        "url": "http://127.0.0.1:3000",
        "icon": "🏠",
    },
)


def _live_ops_allowed() -> bool:
    if os.environ.get("SIMING_TOWER_LIVE_OPS") == "1":
        return True
    return os.environ.get("SIMING_TEST_OFFLINE") != "1"


def _tcp_alive(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def _usec_to_iso(usec: Any) -> str:
    if usec is None:
        return ""
    try:
        val = int(usec)
    except (TypeError, ValueError):
        return ""
    if val <= 0:
        return ""
    try:
        dt = datetime.fromtimestamp(val / 1_000_000.0, tz=SHANGHAI_TZ)
        return iso_now(dt)
    except (OSError, OverflowError, ValueError):
        return ""


def _fmt_interval_hint(sec: float) -> str:
    if sec <= 0:
        return "-"
    if sec < 90:
        return f"约每 {int(round(sec))}s"
    if sec < 5400:
        return f"约每 {int(round(sec / 60.0))}m"
    if sec < 129600:
        return f"约每 {round(sec / 3600.0, 1):g}h"
    return f"约每 {round(sec / 86400.0, 1):g}d"


def _default_cmd_runner(cmd: list[str], timeout: float = 4.0) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return int(proc.returncode), proc.stdout or "", proc.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 124, "", str(exc)


def collect_host_summary() -> dict[str, Any]:
    """轻量本机健康摘要条（零依赖直读 /proc 与根分区，不做重型时序采集）。"""
    snap = read_local_resource_snapshot()
    pacman_lock = Path("/var/lib/pacman/db.lck").exists()
    mem_pct = snap.get("mem_used_pct")
    disk_pct = snap.get("disk_root_used_pct")
    disk_free_gb = snap.get("disk_root_free_gb")

    alerts: list[str] = []
    if isinstance(mem_pct, (int, float)) and mem_pct >= 85.0:
        alerts.append(f"内存水位 {mem_pct:.1f}%")
    if isinstance(disk_pct, (int, float)) and disk_pct >= 90.0:
        alerts.append(f"根分区已用 {disk_pct:.1f}%")
    elif isinstance(disk_free_gb, (int, float)) and disk_free_gb < 15.0:
        alerts.append(f"根分区仅余 {disk_free_gb:.1f} GB")
    if pacman_lock:
        alerts.append("检测到 pacman 锁文件 (/var/lib/pacman/db.lck)")

    if (isinstance(mem_pct, (int, float)) and mem_pct >= 95.0) or (
        isinstance(disk_pct, (int, float)) and disk_pct >= 96.0
    ):
        status = "CRITICAL"
    elif alerts:
        status = "WARN"
    else:
        status = "OK"

    return {
        "hostname": socket.gethostname(),
        "status": status,
        "alerts": alerts,
        "pacman_lock_present": pacman_lock,
        **snap,
    }


def collect_external_tools(
    db: SimingDB,
    *,
    live: Optional[bool] = None,
    tcp_probe_fn: Optional[Callable[[str, int], bool]] = None,
) -> list[dict[str, Any]]:
    """构建本机专业监控工具快捷外链列表，并附轻量本机端口在线状态。"""
    allow_live = _live_ops_allowed() if live is None else bool(live)
    policy = load_policy(db.paths)
    overrides = policy.get("tower_links") if isinstance(policy.get("tower_links"), dict) else {}
    probe = tcp_probe_fn or (lambda h, p: _tcp_alive(h, p, timeout=0.22))

    tools: list[dict[str, Any]] = []
    for base in DEFAULT_EXTERNAL_TOOLS:
        tid = base["id"]
        custom_url = str(overrides.get(tid) or "").strip()
        url = custom_url or base["url"]
        parsed = urlsplit(url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        reachable: Optional[bool] = None
        if allow_live and host in ("127.0.0.1", "localhost", "::1"):
            reachable = bool(probe(host, port))
        tools.append(
            {
                "id": tid,
                "name": base["name"],
                "role": base["role"],
                "icon": base["icon"],
                "url": url,
                "reachable": reachable,
            }
        )
    return tools


def _parse_systemctl_show_blocks(raw: str) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for block in re.split(r"\n\s*\n", raw.strip()):
        if not block.strip():
            continue
        props: dict[str, str] = {}
        for ln in block.splitlines():
            if "=" in ln:
                k, v = ln.split("=", 1)
                props[k.strip()] = v.strip()
        uid = props.get("Id")
        if uid:
            out[uid] = props
    return out


def collect_systemd_timers(
    *,
    live: Optional[bool] = None,
    cmd_runner: Optional[Callable[[list[str], float], tuple[int, str, str]]] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """只读读取当前用户的 systemd user timers 及对应激活 service 的最近执行结果与耗时。"""
    allow_live = _live_ops_allowed() if live is None else bool(live)
    if cmd_runner is None and not allow_live:
        return [], {"available": False, "reason": "offline", "total": 0}

    run_cmd = cmd_runner or _default_cmd_runner
    rc, out, err = run_cmd(["systemctl", "--user", "list-timers", "--all", "--output=json", "--no-pager"], 4.0)
    if rc != 0 or not out.strip():
        return [], {"available": False, "reason": (err or f"rc={rc}")[:160], "total": 0}

    try:
        raw_timers = json.loads(out)
        if not isinstance(raw_timers, list):
            raw_timers = []
    except ValueError as exc:
        return [], {"available": False, "reason": f"json_parse_error: {exc}", "total": 0}

    service_names: list[str] = []
    seen_svcs: set[str] = set()
    for t in raw_timers:
        if not isinstance(t, dict):
            continue
        act = str(t.get("activates") or "").strip()
        if act and _SYSTEMD_UNIT_RE.fullmatch(act) and act not in seen_svcs:
            seen_svcs.add(act)
            service_names.append(act)

    svc_props: dict[str, dict[str, str]] = {}
    if service_names:
        props_arg = (
            "Id,Description,ActiveState,SubState,Result,ExecMainStatus,"
            "ExecMainStartTimestamp,ExecMainExitTimestamp,"
            "ExecMainStartTimestampMonotonic,ExecMainExitTimestampMonotonic"
        )
        rc2, out2, _ = run_cmd(
            ["systemctl", "--user", "show", *service_names, f"--property={props_arg}", "--no-pager"],
            5.0,
        )
        if rc2 == 0 and out2.strip():
            svc_props = _parse_systemctl_show_blocks(out2)

    items: list[dict[str, Any]] = []
    for t in raw_timers:
        if not isinstance(t, dict):
            continue
        unit = str(t.get("unit") or "").strip()
        if not unit:
            continue
        activates = str(t.get("activates") or "").strip()
        props = svc_props.get(activates, {})

        next_iso = _usec_to_iso(t.get("next"))
        last_iso = _usec_to_iso(t.get("last"))
        enabled = bool(next_iso)
        desc = str(props.get("Description") or activates or unit).strip()
        active_state = str(props.get("ActiveState") or "").strip().lower()
        result = str(props.get("Result") or "").strip().lower()
        try:
            exit_status = int(props.get("ExecMainStatus") or 0)
        except ValueError:
            exit_status = 0

        dur_sec: Optional[float] = None
        try:
            sm = int(props.get("ExecMainStartTimestampMonotonic") or 0)
            em = int(props.get("ExecMainExitTimestampMonotonic") or 0)
            if sm > 0 and em >= sm:
                dur_sec = round((em - sm) / 1_000_000.0, 2)
        except ValueError:
            dur_sec = None

        schedule_label = "-"
        try:
            n_us = int(t.get("next") or 0)
            l_us = int(t.get("last") or 0)
            if n_us > 0 and l_us > 0 and n_us > l_us:
                schedule_label = _fmt_interval_hint((n_us - l_us) / 1_000_000.0)
            elif not enabled:
                schedule_label = "未排期 / 一次性"
        except (TypeError, ValueError):
            pass

        last_error = ""
        if active_state in ("active", "activating"):
            status = "running"
        elif result == "timeout":
            status = "timeout"
            last_error = f"result=timeout (exit={exit_status})"
        elif active_state == "failed" or (result and result != "success") or exit_status != 0:
            status = "failed"
            last_error = f"result={result or active_state}, exit={exit_status}"
        elif last_iso or result == "success":
            status = "ok"
        else:
            status = "idle"

        is_siming = unit.startswith("siming-")
        hay_qq = f"{unit} {activates} {desc}".lower()
        if is_siming:
            push_channel = "siming_core"
        elif any(h in hay_qq for h in _DIRECT_QQ_UNIT_HINTS) or ("qq" in hay_qq and "hermes" in hay_qq):
            push_channel = "direct_qq"
        else:
            push_channel = "local"

        items.append(
            {
                "id": f"systemd:{unit}",
                "raw_id": activates or unit,
                "source": "systemd",
                "name": unit,
                "target": activates,
                "description": desc,
                "category": "siming" if is_siming else "host",
                "is_siming": is_siming,
                "enabled": enabled,
                "schedule": schedule_label,
                "last_run_at": last_iso,
                "next_run_at": next_iso,
                "duration_sec": dur_sec,
                "last_status": status,
                "exit_code": exit_status,
                "result": result or active_state or "unknown",
                "push_channel": push_channel,
                "push_detail": "司命核心节拍" if is_siming else ("脚本直推 QQ" if push_channel == "direct_qq" else "本机执行"),
                "last_error": last_error,
                "has_log_preview": bool(activates and _SYSTEMD_UNIT_RE.fullmatch(activates)),
            }
        )

    items.sort(key=lambda x: (0 if x["last_status"] in ("failed", "timeout") else (1 if x["is_siming"] else 2), x["name"]))
    return items, {"available": True, "reason": "", "total": len(items)}


def collect_hermes_crons(
    db: SimingDB,
    *,
    jobs_json: Optional[Path] = None,
    output_root: Optional[Path] = None,
    live: Optional[bool] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """只读读取 ~/.hermes/cron/jobs.json，并关联 siming cron_harvest 收割策略与产物游标。"""
    allow_live = _live_ops_allowed() if live is None else bool(live)
    if jobs_json is None and not allow_live:
        return [], {"available": False, "reason": "offline", "total": 0}

    jpath = jobs_json or DEFAULT_JOBS_JSON
    oroot = output_root or DEFAULT_OUTPUT_ROOT
    if not jpath.is_file():
        return [], {"available": False, "reason": "jobs_json_missing", "total": 0}

    try:
        raw = json.loads(jpath.read_text(encoding="utf-8"))
        jobs = raw.get("jobs", []) if isinstance(raw, dict) else (raw if isinstance(raw, list) else [])
    except (OSError, ValueError) as exc:
        return [], {"available": False, "reason": str(exc)[:160], "total": 0}

    items: list[dict[str, Any]] = []
    for j in jobs:
        if not isinstance(j, dict):
            continue
        jid = str(j.get("id") or "").strip()
        if not jid:
            continue
        name = str(j.get("name") or jid).strip()
        enabled = bool(j.get("enabled", True)) and str(j.get("state") or "").lower() != "paused"
        deliver = str(j.get("deliver") or "local").strip()
        sched_obj = j.get("schedule") if isinstance(j.get("schedule"), dict) else {}
        sched_disp = str(j.get("schedule_display") or sched_obj.get("display") or sched_obj.get("expr") or "-").strip()
        script = str(j.get("script") or "").strip()
        prompt_first = ""
        if not script and j.get("prompt"):
            for ln in str(j.get("prompt")).splitlines():
                s = ln.strip()
                if s:
                    prompt_first = redact_sensitive(s)[:90]
                    break
        desc = f"script: {script}" if script else (prompt_first or "Hermes Cron 任务")

        harvest_spec = HARVEST_JOBS.get(jid)
        cursor_val = db.get_meta(f"cron_harvest:{jid}") or ""
        out_dir = oroot / jid
        has_output = False
        latest_output_file = ""
        if out_dir.is_dir():
            try:
                md_files = sorted(p.name for p in out_dir.glob("*.md"))
                if md_files:
                    has_output = True
                    latest_output_file = md_files[-1]
            except OSError:
                pass

        if not enabled:
            push_channel = "disabled"
            push_detail = "已停用"
        elif deliver == "local" and harvest_spec:
            push_channel = "siming_harvest"
            lvl = str(harvest_spec.get("level") or "normal")
            push_detail = f"司命收割 → rule:{harvest_spec['rule']} ({lvl})"
        elif deliver.lower().startswith("qqbot"):
            push_channel = "direct_qq"
            push_detail = "Hermes 直推 QQ（绕过司命）"
        elif deliver.lower().startswith("feishu"):
            push_channel = "direct_feishu"
            push_detail = "Hermes 直推飞书"
        else:
            push_channel = "local"
            push_detail = f"本地静默 ({deliver or 'local'})"

        raw_st = str(j.get("last_status") or "").strip().lower()
        fail_streak = int(j.get("failure_streak") or 0)
        last_err = str(j.get("last_error") or j.get("last_delivery_error") or "").strip()
        if not enabled:
            status = "paused"
        elif fail_streak > 0 or last_err or raw_st in ("error", "failed", "timeout"):
            status = "failed"
        elif raw_st in ("ok", "success"):
            status = "ok"
        else:
            status = "idle"

        items.append(
            {
                "id": f"hermes:{jid}",
                "raw_id": jid,
                "source": "hermes",
                "name": name,
                "target": script or "agent-prompt",
                "description": desc,
                "category": "siming" if harvest_spec else "hermes",
                "is_siming": bool(harvest_spec),
                "enabled": enabled,
                "schedule": sched_disp,
                "last_run_at": str(j.get("last_run_at") or ""),
                "next_run_at": str(j.get("next_run_at") or ""),
                "duration_sec": None,
                "last_status": status,
                "failure_streak": fail_streak,
                "deliver": deliver,
                "push_channel": push_channel,
                "push_detail": push_detail,
                "harvest_rule": harvest_spec["rule"] if harvest_spec else "",
                "harvest_level": harvest_spec.get("level", "") if harvest_spec else "",
                "harvest_cursor": cursor_val,
                "latest_output_file": latest_output_file,
                "last_error": redact_sensitive(last_err)[:220] if last_err else "",
                "has_log_preview": has_output,
            }
        )

    prio = {"failed": 0, "ok": 1, "idle": 2, "paused": 3}
    items.sort(
        key=lambda x: (
            prio.get(x["last_status"], 9),
            0 if x["push_channel"] == "direct_qq" else (1 if x["push_channel"] == "siming_harvest" else 2),
            x["name"],
        )
    )
    return items, {"available": True, "reason": "", "total": len(items), "jobs_path": str(jpath)}


def collect_siming_waits_schedule(db: SimingDB) -> list[dict[str, Any]]:
    """收集司命活跃挂起与待确认提议（WAITING / PROPOSED）纳入统一调度总表。"""
    db.init_db()
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT wait_id, status, strength, trigger_kind, trigger_expr, next_fire_at,
                   expires_at, action_mode, quote, mandate_json, updated_at
            FROM waits
            WHERE status IN ('WAITING', 'PROPOSED')
            ORDER BY CASE status WHEN 'WAITING' THEN 0 ELSE 1 END,
                     COALESCE(next_fire_at, expires_at) ASC
            LIMIT 80
            """
        ).fetchall()
    finally:
        conn.close()

    items: list[dict[str, Any]] = []
    for r in rows:
        try:
            mandate = json.loads(r["mandate_json"] or "{}")
            if not isinstance(mandate, dict):
                mandate = {}
        except ValueError:
            mandate = {}
        wid = str(r["wait_id"])
        st = str(r["status"]).upper()
        task = str(mandate.get("task") or r["quote"] or r["trigger_expr"] or wid).strip()
        mode = str(r["action_mode"] or "remind")
        tkind = str(r["trigger_kind"] or "time")
        texpr = str(r["trigger_expr"] or "")
        items.append(
            {
                "id": f"wait:{wid}",
                "raw_id": wid,
                "source": "siming_wait",
                "name": f"[{wid}] {task[:60]}",
                "target": f"mode={mode} · {r['strength']}",
                "description": f"{tkind}: {texpr}" + (f" · 过期 {str(r['expires_at'])[:10]}" if r["expires_at"] else ""),
                "category": "siming",
                "is_siming": True,
                "enabled": st == "WAITING",
                "schedule": texpr or tkind,
                "last_run_at": str(r["updated_at"] or ""),
                "next_run_at": str(r["next_fire_at"] or r["expires_at"] or ""),
                "duration_sec": None,
                "last_status": "waiting" if st == "WAITING" else "proposed",
                "push_channel": "siming_router",
                "push_detail": "司命 Router（确认后触发）" if st == "PROPOSED" else f"司命 Router ({mode})",
                "last_error": "",
                "has_log_preview": False,
            }
        )
    return items


def _collect_siming_health_and_guard(
    db: SimingDB,
    *,
    live: Optional[bool] = None,
    cdp_check_fn: Optional[Callable[[], dict[str, Any]]] = None,
    tcp_probe_fn: Optional[Callable[[str, int], bool]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """采集司命自身运行态、队列、24h 外发、依赖链健康与 push_guard 熔断/静默清单。"""
    db.init_db()
    allow_live = _live_ops_allowed() if live is None else bool(live)
    now = _db_mod.now_local()
    since_24h = iso_now(now - timedelta(hours=24))
    now_iso = iso_now(now)

    # 1. Pause 状态
    paused_until = (db.get_meta("paused_until") or "").strip()
    is_paused = False
    pause_remaining_min = 0
    if paused_until:
        try:
            p_dt = parse_iso(paused_until)
            if p_dt > now:
                is_paused = True
                pause_remaining_min = max(1, int(round((p_dt - now).total_seconds() / 60.0)))
        except Exception:
            pass

    # 2. 核心节拍与 SQLite 队列/外发/规则统计
    conn = db.connect()
    try:
        jmode = str(conn.execute("PRAGMA journal_mode;").fetchone()[0])

        eff_counts = {"PENDING": 0, "RUNNING": 0, "FAILED": 0, "DONE_24H": 0}
        for r in conn.execute("SELECT status, COUNT(*) AS c FROM effects WHERE status IN ('PENDING','RUNNING','FAILED') GROUP BY status").fetchall():
            eff_counts[str(r["status"]).upper()] = int(r["c"] or 0)
        eff_counts["DONE_24H"] = int(
            conn.execute("SELECT COUNT(*) FROM effects WHERE status='DONE' AND completed_at >= ?", (since_24h,)).fetchone()[0] or 0
        )
        recent_bad_effects = [
            {
                "effect_id": str(r["effect_id"]),
                "kind": str(r["kind"]),
                "status": str(r["status"]),
                "retry_count": int(r["retry_count"] or 0),
                "last_error": redact_sensitive(str(r["last_error"] or ""))[:180],
                "updated_at": str(r["completed_at"] or r["started_at"] or r["created_at"] or ""),
            }
            for r in conn.execute(
                """
                SELECT effect_id, kind, status, retry_count, last_error, created_at, started_at, completed_at
                FROM effects
                WHERE status IN ('FAILED', 'RUNNING')
                ORDER BY COALESCE(completed_at, started_at, created_at) DESC
                LIMIT 8
                """
            ).fetchall()
        ]

        outbox_24h = {"SENT": 0, "MORNING_QUEUE": 0, "THROTTLED": 0, "QUIETED": 0, "FAILED": 0}
        for r in conn.execute(
            "SELECT status, COUNT(*) AS c FROM outbox WHERE created_at >= ? GROUP BY status",
            (since_24h,),
        ).fetchall():
            st_k = str(r["status"] or "").upper()
            if st_k in outbox_24h:
                outbox_24h[st_k] = int(r["c"] or 0)

        top_rules_24h = [
            {
                "rule_id": str(r["rule_id"] or ""),
                "sent_24h": int(r["sent_n"] or 0),
                "held_24h": int(r["held_n"] or 0),
            }
            for r in conn.execute(
                """
                SELECT rule_id,
                       SUM(CASE WHEN status = 'SENT' THEN 1 ELSE 0 END) AS sent_n,
                       SUM(CASE WHEN status IN ('THROTTLED', 'QUIETED') THEN 1 ELSE 0 END) AS held_n
                FROM outbox
                WHERE created_at >= ?
                GROUP BY rule_id
                ORDER BY (sent_n + held_n) DESC
                LIMIT 10
                """,
                (since_24h,),
            ).fetchall()
        ]

        recent_held_samples = [
            {
                "rule_id": str(r["rule_id"] or ""),
                "status": str(r["status"] or ""),
                "ref_id": str(r["ref_id"] or ""),
                "preview": redact_sensitive((str(r["text"] or "").strip().splitlines() or [""])[0])[:110],
                "created_at": str(r["created_at"] or ""),
            }
            for r in conn.execute(
                """
                SELECT rule_id, status, ref_id, text, created_at
                FROM outbox
                WHERE status IN ('THROTTLED', 'QUIETED') AND created_at >= ?
                ORDER BY created_at DESC
                LIMIT 10
                """,
                (since_24h,),
            ).fetchall()
        ]

        rule_rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT rule_id, mode, hard_rule, sent_14d, acted_14d, dismissed_14d, cooldown_until, updated_at
                FROM rule_stats
                ORDER BY
                    CASE mode WHEN 'QUIET' THEN 0 WHEN 'FROZEN' THEN 1 WHEN 'DEMOTED' THEN 2 ELSE 3 END,
                    CASE WHEN cooldown_until IS NOT NULL AND cooldown_until > ? THEN 0 ELSE 1 END,
                    sent_14d DESC
                LIMIT 40
                """,
                (now_iso,),
            ).fetchall()
        ]

        src_rows = [
            dict(r)
            for r in conn.execute("SELECT id, kind, auth_state, freshness, last_ok_at, detail FROM sources ORDER BY id").fetchall()
        ]
    finally:
        conn.close()

    # 3. 备份状态
    backups = sorted(db.paths.backup_dir.glob("siming-*.db")) if db.paths.backup_dir.is_dir() else []
    latest_backup: dict[str, Any] = {"count": len(backups), "latest_name": "", "size_mb": 0.0, "mtime_iso": ""}
    if backups:
        b = backups[-1]
        try:
            st_b = b.stat()
            latest_backup["latest_name"] = b.name
            latest_backup["size_mb"] = round(st_b.st_size / (1024 * 1024), 2)
            latest_backup["mtime_iso"] = iso_now(datetime.fromtimestamp(st_b.st_mtime, tz=SHANGHAI_TZ))
        except OSError:
            pass

    # 4. 核心依赖探测（快速无阻塞）
    probe_tcp = tcp_probe_fn or (lambda h, p: _tcp_alive(h, p, timeout=0.25))
    cdp_info: dict[str, Any]
    if cdp_check_fn is not None:
        cdp_info = dict(cdp_check_fn() or {})
    elif allow_live:
        cdp_info = check_cdp_9222_status()
    else:
        cdp_info = {"main_chromium_up": None, "atrust_collision_suspect": False, "skipped": "offline"}

    deps: dict[str, Any] = {
        "cdp_9222": cdp_info,
        "proxy_7890_up": bool(probe_tcp("127.0.0.1", 7890)) if allow_live else None,
        "clash_api_9090_up": bool(probe_tcp("127.0.0.1", 9090)) if allow_live else None,
        "ai_quota_up": bool(probe_tcp("127.0.0.1", 18101)) if allow_live else None,
        "hermes_dashboard_up": bool(probe_tcp("127.0.0.1", 9119)) if allow_live else None,
    }

    # 5. 数据源健康汇总
    src_by_id = {str(s["id"]): s for s in src_rows}
    src_summary = {
        "total": len(src_rows),
        "ok": sum(1 for s in src_rows if s["auth_state"] == "OK" and s["freshness"] == "OK"),
        "stale": sum(1 for s in src_rows if s["freshness"] == "STALE"),
        "error": sum(1 for s in src_rows if s["freshness"] == "ERROR" or (s["auth_state"] not in ("OK", "UNCONFIGURED"))),
        "unconfigured": sum(1 for s in src_rows if s["auth_state"] == "UNCONFIGURED"),
        "problem_sources": [
            {"id": s["id"], "auth_state": s["auth_state"], "freshness": s["freshness"], "last_ok_at": s["last_ok_at"], "detail": s["detail"]}
            for s in src_rows
            if s["auth_state"] != "UNCONFIGURED" and (s["auth_state"] != "OK" or s["freshness"] in ("STALE", "ERROR"))
        ],
    }

    def _age_sec(ts_s: Optional[str]) -> Optional[int]:
        if not ts_s:
            return None
        try:
            return max(0, int((now - parse_iso(str(ts_s))).total_seconds()))
        except Exception:
            return None

    edge_ok_at = (src_by_id.get("edge_vps") or {}).get("last_ok_at") or ""
    ios_hb_at = (src_by_id.get("ios_hb") or {}).get("last_ok_at") or ""

    # 6. Push Guard 状态整合
    pg = guard_status(db)
    held_by_rule: dict[str, int] = {}
    for h in pg.get("held_24h") or []:
        rid = str(h.get("rule_id") or "")
        held_by_rule[rid] = held_by_rule.get(rid, 0) + int(h.get("n") or 0)

    enriched_rules: list[dict[str, Any]] = []
    for r in rule_rows:
        rid = str(r["rule_id"])
        mode = str(r["mode"] or "ACTIVE").upper()
        cd_until = str(r.get("cooldown_until") or "")
        is_cooling = False
        if cd_until:
            try:
                is_cooling = parse_iso(cd_until) > now
            except Exception:
                is_cooling = False
        sent = int(r.get("sent_14d") or 0)
        acted = int(r.get("acted_14d") or 0)
        rate = round((acted / sent) * 100.0, 1) if sent > 0 else None
        enriched_rules.append(
            {
                "rule_id": rid,
                "mode": mode,
                "hard_rule": bool(r.get("hard_rule")),
                "sent_14d": sent,
                "acted_14d": acted,
                "dismissed_14d": int(r.get("dismissed_14d") or 0),
                "accept_rate_pct": rate,
                "is_cooling": is_cooling,
                "cooldown_until": cd_until if is_cooling else "",
                "held_24h": held_by_rule.get(rid, 0),
                "can_restore": (mode == "QUIET") or is_cooling,
                "updated_at": str(r.get("updated_at") or ""),
            }
        )

    last_diag_raw = db.get_meta("ops_last_diagnose_json") or ""
    last_diagnose: Optional[dict[str, Any]] = None
    if last_diag_raw:
        try:
            parsed_diag = json.loads(last_diag_raw)
            if isinstance(parsed_diag, dict):
                last_diagnose = parsed_diag
        except ValueError:
            pass

    siming_health = {
        "sqlite_journal_mode": jmode,
        "paused": is_paused,
        "paused_until": paused_until if is_paused else "",
        "pause_remaining_min": pause_remaining_min,
        "beats": {
            "last_tick_at": db.get_meta("last_tick_at") or "",
            "last_tick_ok_at": db.get_meta("last_tick_ok_at") or "",
            "last_tick_age_sec": _age_sec(db.get_meta("last_tick_ok_at")),
            "watchdog_last_ok_at": db.get_meta("watchdog_last_ok_at") or "",
            "watchdog_age_sec": _age_sec(db.get_meta("watchdog_last_ok_at")),
            "edge_vps_last_ok_at": edge_ok_at,
            "edge_vps_age_sec": _age_sec(edge_ok_at),
            "ios_hb_last_ok_at": ios_hb_at,
            "ios_hb_age_sec": _age_sec(ios_hb_at),
            "last_v5_30m_guard_at": db.get_meta("last_v5_30m_guard_at") or "",
            "last_v5_6h_guard_at": db.get_meta("last_v5_6h_guard_at") or "",
            "infra_patrol_last_at": db.get_meta("infra_patrol_last_at") or "",
        },
        "effects": {
            "counts": eff_counts,
            "recent_issues": recent_bad_effects,
        },
        "outbox_24h": outbox_24h,
        "top_rules_24h": top_rules_24h,
        "backups": latest_backup,
        "sources": src_summary,
        "dependencies": deps,
        "last_diagnose": last_diagnose,
    }

    push_guard_payload = {
        "quiet": pg.get("quiet") or [],
        "cooling": pg.get("cooling") or [],
        "held_24h": pg.get("held_24h") or [],
        "quiet_count": len(pg.get("quiet") or []),
        "cooling_count": len(pg.get("cooling") or []),
        "held_total_24h": sum(int(h.get("n") or 0) for h in (pg.get("held_24h") or [])),
        "rules": enriched_rules,
        "recent_held": recent_held_samples,
    }
    return siming_health, push_guard_payload


def build_ops_tower_payload(
    db: SimingDB,
    *,
    live: Optional[bool] = None,
    cmd_runner: Optional[Callable[[list[str], float], tuple[int, str, str]]] = None,
    hermes_jobs_json: Optional[Path] = None,
    hermes_output_root: Optional[Path] = None,
    cdp_check_fn: Optional[Callable[[], dict[str, Any]]] = None,
    tcp_probe_fn: Optional[Callable[[str, int], bool]] = None,
) -> dict[str, Any]:
    """构建 Tower「⚙️ 运维与调度」Tab 的全量只读聚合快照。"""
    db.init_db()
    host = collect_host_summary()
    siming_health, push_guard_data = _collect_siming_health_and_guard(
        db,
        live=live,
        cdp_check_fn=cdp_check_fn,
        tcp_probe_fn=tcp_probe_fn,
    )
    sys_timers, sys_meta = collect_systemd_timers(live=live, cmd_runner=cmd_runner)
    hermes_crons, hermes_meta = collect_hermes_crons(
        db,
        jobs_json=hermes_jobs_json,
        output_root=hermes_output_root,
        live=live,
    )
    wait_items = collect_siming_waits_schedule(db)
    external_tools = collect_external_tools(db, live=live, tcp_probe_fn=tcp_probe_fn)

    siming_timers_map = {t["name"]: t for t in sys_timers if t["name"] in _SIMING_CORE_TIMERS}
    siming_core_timers = [siming_timers_map[n] for n in _SIMING_CORE_TIMERS if n in siming_timers_map]
    # 也把所有其他 siming-* timers 纳入 core 展示
    for t in sys_timers:
        if t["is_siming"] and t["name"] not in siming_timers_map:
            siming_core_timers.append(t)

    all_schedules = [*sys_timers, *hermes_crons, *wait_items]

    timers_failed = sum(1 for t in sys_timers if t["last_status"] in ("failed", "timeout"))
    siming_timers_failed = sum(1 for t in sys_timers if t["is_siming"] and t["last_status"] in ("failed", "timeout"))
    host_timers_failed = timers_failed - siming_timers_failed
    crons_failed = sum(1 for c in hermes_crons if c["last_status"] == "failed")
    direct_qq_count = sum(1 for s in all_schedules if s["push_channel"] == "direct_qq" and s["enabled"])
    harvest_count = sum(1 for s in all_schedules if s["push_channel"] == "siming_harvest")

    issues: list[dict[str, str]] = []
    if host["status"] in ("WARN", "CRITICAL"):
        for a in host.get("alerts") or []:
            issues.append({"severity": "critical" if host["status"] == "CRITICAL" else "warn", "area": "host", "message": a})
    if siming_health["paused"]:
        issues.append(
            {
                "severity": "warn",
                "area": "siming",
                "message": f"后台自动 worker 已暂停至 {siming_health['paused_until'][:16].replace('T', ' ')}（提醒仍正常发出）",
            }
        )
    if siming_timers_failed > 0:
        issues.append({"severity": "critical", "area": "siming_timer", "message": f"{siming_timers_failed} 个司命核心 timer 最近执行失败或超时"})
    if host_timers_failed > 0:
        issues.append({"severity": "warn", "area": "systemd", "message": f"{host_timers_failed} 个本机非司命 systemd timer 最近执行异常"})
    if crons_failed > 0:
        issues.append({"severity": "warn", "area": "hermes", "message": f"{crons_failed} 个 Hermes cron 任务状态异常"})
    if siming_health["effects"]["counts"]["FAILED"] > 0:
        issues.append(
            {
                "severity": "warn",
                "area": "effects",
                "message": f"effects 队列存在 {siming_health['effects']['counts']['FAILED']} 条 FAILED 记录",
            }
        )
    if push_guard_data["cooling_count"] > 0:
        issues.append(
            {
                "severity": "warn",
                "area": "push_guard",
                "message": f"{push_guard_data['cooling_count']} 条推送规则正处于熔断冷却期",
            }
        )
    if push_guard_data["quiet_count"] > 0:
        issues.append(
            {
                "severity": "info",
                "area": "push_guard",
                "message": f"{push_guard_data['quiet_count']} 条推送规则已自动静默降级 (QUIET)，仅进晨札汇总",
            }
        )
    cdp = siming_health["dependencies"]["cdp_9222"]
    if cdp.get("atrust_collision_suspect"):
        issues.append({"severity": "warn", "area": "cdp", "message": "9222 端口检测到其他 Electron/VPN 客户端抢占（已检查回退地址）"})
    if siming_health["sources"]["error"] > 0 or siming_health["sources"]["stale"] > 0:
        issues.append(
            {
                "severity": "warn",
                "area": "sources",
                "message": f"数据源异常 {siming_health['sources']['error']} 个 / 过期 {siming_health['sources']['stale']} 个",
            }
        )

    crit_n = sum(1 for i in issues if i["severity"] == "critical")
    warn_n = sum(1 for i in issues if i["severity"] == "warn")
    overall = "CRITICAL" if crit_n > 0 else ("WARN" if warn_n > 0 else "OK")

    return {
        "ok": True,
        "generated_at": iso_now(),
        "summary": {
            "overall_status": overall,
            "issues_count": len(issues),
            "critical_count": crit_n,
            "warn_count": warn_n,
            "siming_paused": siming_health["paused"],
            "timers_total": len(sys_timers),
            "timers_failed": timers_failed,
            "crons_total": len(hermes_crons),
            "crons_enabled": sum(1 for c in hermes_crons if c["enabled"]),
            "crons_failed": crons_failed,
            "waits_total": len(wait_items),
            "schedules_total": len(all_schedules),
            "direct_qq_count": direct_qq_count,
            "siming_harvest_count": harvest_count,
            "push_guard_quiet": push_guard_data["quiet_count"],
            "push_guard_cooling": push_guard_data["cooling_count"],
            "outbox_sent_24h": siming_health["outbox_24h"]["SENT"],
            "outbox_held_24h": siming_health["outbox_24h"]["THROTTLED"] + siming_health["outbox_24h"]["QUIETED"],
            "effects_pending": siming_health["effects"]["counts"]["PENDING"],
            "effects_failed": siming_health["effects"]["counts"]["FAILED"],
        },
        "issues": issues,
        "host": host,
        "siming_health": siming_health,
        "siming_core_timers": siming_core_timers,
        "push_guard": push_guard_data,
        "schedules": {
            "meta": {
                "systemd": sys_meta,
                "hermes": hermes_meta,
            },
            "items": all_schedules,
        },
        "external_tools": external_tools,
    }


def read_schedule_output_preview(
    db: SimingDB,
    source: str,
    item_id: str,
    *,
    jobs_json: Optional[Path] = None,
    output_root: Optional[Path] = None,
    cmd_runner: Optional[Callable[[list[str], float], tuple[int, str, str]]] = None,
    live: Optional[bool] = None,
) -> dict[str, Any]:
    """严格白名单 + 脱敏的只读调度产物/日志预览（严禁任意路径遍历或任意 unit 日志读取）。"""
    src = (source or "").strip().lower()
    raw_id = (item_id or "").strip()

    if src == "hermes":
        if not _HERMES_JOB_ID_RE.fullmatch(raw_id):
            return {"ok": False, "error": "Invalid Hermes job id"}
        jpath = jobs_json or DEFAULT_JOBS_JSON
        oroot = output_root or DEFAULT_OUTPUT_ROOT
        if not jpath.is_file():
            return {"ok": False, "error": "Hermes jobs.json 不存在"}
        try:
            data = json.loads(jpath.read_text(encoding="utf-8"))
            jobs = data.get("jobs", []) if isinstance(data, dict) else data
        except (OSError, ValueError):
            return {"ok": False, "error": "无法读取 Hermes jobs.json"}
        job_obj = next((j for j in jobs if isinstance(j, dict) and str(j.get("id")) == raw_id), None)
        if job_obj is None:
            return {"ok": False, "error": f"未找到 Hermes cron 任务：{raw_id}"}

        job_dir = (oroot / raw_id).resolve()
        try:
            job_dir.relative_to(oroot.resolve())
        except ValueError:
            return {"ok": False, "error": "路径越界已拦截"}
        if not job_dir.is_dir():
            return {
                "ok": True,
                "source": "hermes",
                "id": raw_id,
                "name": str(job_obj.get("name") or raw_id),
                "filename": "",
                "kind": "empty",
                "preview": "（该任务尚无本地输出文件）",
            }
        md_files = sorted(job_dir.glob("*.md"))
        if not md_files:
            return {
                "ok": True,
                "source": "hermes",
                "id": raw_id,
                "name": str(job_obj.get("name") or raw_id),
                "filename": "",
                "kind": "empty",
                "preview": "（输出目录为空）",
            }
        latest = md_files[-1].resolve()
        if latest.parent != job_dir:
            return {"ok": False, "error": "非法文件路径"}
        raw_txt = latest.read_text(encoding="utf-8", errors="replace")[:12000]
        kind, body = parse_cron_output(raw_txt)
        snippet = body if body else raw_txt[:2500]
        return {
            "ok": True,
            "source": "hermes",
            "id": raw_id,
            "name": str(job_obj.get("name") or raw_id),
            "filename": latest.name,
            "kind": kind,
            "file_count": len(md_files),
            "preview": redact_sensitive(snippet)[:4000],
        }

    if src == "systemd":
        if not _SYSTEMD_UNIT_RE.fullmatch(raw_id) or raw_id.startswith("-"):
            return {"ok": False, "error": "Invalid systemd service unit name"}
        timers, meta = collect_systemd_timers(live=live, cmd_runner=cmd_runner)
        allowed_units = {str(t.get("target") or "") for t in timers if t.get("target")}
        if raw_id not in allowed_units:
            return {"ok": False, "error": f"仅允许查看当前用户 timer 关联的 service 日志：{raw_id}"}
        run_cmd = cmd_runner or _default_cmd_runner
        rc, out, err = run_cmd(
            ["journalctl", "--user", "-u", raw_id, "-n", "30", "--no-pager", "--output=short-iso"],
            3.5,
        )
        if rc != 0:
            return {"ok": False, "error": f"读取日志失败：{(err or f'rc={rc}')[:160]}"}
        text = redact_sensitive(out.strip() or "（最近无 journald 输出）")[:4000]
        return {
            "ok": True,
            "source": "systemd",
            "id": raw_id,
            "name": raw_id,
            "filename": f"journalctl --user -u {raw_id} -n 30",
            "kind": "journal",
            "preview": text,
            "meta": meta,
        }

    return {"ok": False, "error": f"Unsupported schedule preview source: {source}"}


def ops_restore_push_rule(db: SimingDB, rule_id: str) -> dict[str, Any]:
    """司命专属安全写操作：解除某条规则的 push_guard 静默降级或熔断冷却。"""
    return restore_rule(db, rule_id)


def ops_set_pause(db: SimingDB, action: str, duration: str = "2h") -> dict[str, Any]:
    """司命专属安全写操作：暂停或恢复司命后台 worker 自动执行（提醒仍正常发出，与 CLI siming pause 同源）。"""
    db.init_db()
    act = (action or "").strip().lower()
    if act in ("resume", "off", "unpause", "0", "none"):
        db.set_meta("paused_until", "")
        return {
            "ok": True,
            "paused": False,
            "paused_until": "",
            "reply": "✅ 已恢复司命后台自动 worker 执行。",
        }
    if act != "pause":
        return {"ok": False, "error": f"Unsupported action: {action} (expected 'pause' or 'resume')"}

    dur_str = (duration or "2h").strip()
    try:
        sec = int(parse_duration_seconds(dur_str))
    except Exception:
        return {"ok": False, "error": f"无法解析暂停时长：{dur_str}（示例：30m / 2h / 12h）"}
    if sec < 300 or sec > 72 * 3600:
        return {"ok": False, "error": "暂停时长须在 5m ~ 72h 之间"}
    until = iso_now(_db_mod.now_local() + timedelta(seconds=sec))
    db.set_meta("paused_until", until)
    return {
        "ok": True,
        "paused": True,
        "paused_until": until,
        "duration": dur_str,
        "reply": f"⏸️ 已暂停司命后台自动 worker 执行至 {until[:16].replace('T', ' ')}（提醒仍正常发出）。",
    }


def ops_run_diagnose(db: SimingDB, *, allow_heal: bool = False) -> dict[str, Any]:
    """司命专属诊断动作：执行 doctor 只读体检 + 研发链路合成诊断（allow_heal=True 仅允许白名单清 sing-box DNS 缓存）。"""
    db.init_db()
    doc = run_doctor(db, check_mail=False, check_edge=False)
    diag = diagnose_and_heal_dev_links(db, allow_heal=bool(allow_heal), probe_nous_portal=False)
    ts = iso_now()
    summary = {
        "ok": True,
        "checked_at": ts,
        "allow_heal": bool(allow_heal),
        "doctor_ok": bool(doc.get("ok")),
        "doctor_checks": doc.get("checks") or [],
        "freeze_candidates_30d": doc.get("freeze_candidates_30d") or [],
        "links_ok": bool(diag.get("ok")),
        "root_causes": diag.get("root_causes") or [],
        "healed_actions": diag.get("healed_actions") or [],
        "cdp": diag.get("cdp") or {},
        "net": diag.get("net") or {},
        "reply": redact_sensitive(str(diag.get("reply") or "")),
    }
    try:
        db.set_meta("ops_last_diagnose_json", json.dumps(summary, ensure_ascii=False)[:16000])
    except Exception:
        pass
    return summary
