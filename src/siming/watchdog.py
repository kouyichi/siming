"""每 2 分钟纯脚本巡检看门狗（watchdog）：强杀超时 worker、清孤儿 worktree、核查五项数据源健康与 CDP 绑定。"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import urllib.request
from datetime import timedelta
from pathlib import Path
from typing import Any

from siming.approval import reconcile_stuck_claiming_approvals
from siming.config import load_policy
from siming.db import SimingDB, iso_now, now_local, parse_iso, pid_is_siming_process
from siming.quota import fetch_ai_quota


def _identify_cdp_owner(host: str, port: int) -> str:
    """读 /json/version 判断监听者：chromium / atrust / unknown（直连，不走代理）。"""
    url_host = f"[{host}]" if ":" in host else host
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(f"http://{url_host}:{port}/json/version", timeout=1.5) as resp:
            info = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception:
        return "unknown"
    ua = str(info.get("User-Agent", ""))
    browser = str(info.get("Browser", ""))
    if "atrust" in ua.lower() or "electron" in ua.lower():
        return "atrust"
    if browser.startswith(("Chrome/", "HeadlessChrome/")):
        return "chromium"
    return "unknown"


def check_cdp_9222_status() -> dict[str, Any]:
    """核查本机 9222 端口：按 /json/version 识别各地址上的监听者，揪出 aTrust 抢占。

    主 Chromium 默认只绑 127.0.0.1；2026-11-15 曾出现 aTrust 先占 IPv4、Chromium 只剩 [::1]。
    所以不能靠"哪个协议族在听"推断，必须看实际是谁在听。
    """
    ipv6_listening = False
    ipv4_listening = False
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s6:
        s6.settimeout(0.5)
        ipv6_listening = s6.connect_ex(("::1", 9222, 0, 0)) == 0
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s4:
        s4.settimeout(0.5)
        ipv4_listening = s4.connect_ex(("127.0.0.1", 9222)) == 0

    ipv6_owner = _identify_cdp_owner("::1", 9222) if ipv6_listening else None
    ipv4_owner = _identify_cdp_owner("127.0.0.1", 9222) if ipv4_listening else None
    owners = [o for o in (ipv4_owner, ipv6_owner) if o]

    return {
        "ipv6_9222": ipv6_listening,
        "ipv4_9222": ipv4_listening,
        "ipv6_owner": ipv6_owner,
        "ipv4_owner": ipv4_owner,
        "main_chromium_up": "chromium" in owners,
        "atrust_collision_suspect": "atrust" in owners,
    }


def run_watchdog(db: SimingDB) -> dict[str, Any]:
    """纯确定性巡检：零模型调用。"""
    db.init_db()
    policy = load_policy(db.paths)
    timeout_sec = int(policy["guardrails"].get("worker_timeout_sec", 1800))
    now = now_local()
    ts = iso_now(now)

    killed_effects: list[str] = []
    cleaned_worktrees: list[str] = []
    stale_sources: list[str] = []

    conn = db.connect()
    try:
        # 1. 强杀超 30 分钟的卡死 RUNNING worker 进程或瞬态单元
        running = conn.execute(
            "SELECT * FROM effects WHERE status = 'RUNNING'"
        ).fetchall()
        for eff in running:
            started_str = eff["started_at"] or eff["created_at"]
            elapsed = (now - parse_iso(started_str)).total_seconds()
            pid = eff["worker_pid"]
            unit_name = str(eff["unit_name"] or "").strip()
            gen = int(eff["attempt_gen"] or 0)
            payload = json.loads(eff["payload_json"] or "{}")
            alive = False
            if pid:
                alive = pid_is_siming_process(int(pid))
            elif unit_name:
                if elapsed < 15:
                    alive = True
                else:
                    u_chk = subprocess.run(
                        ["systemctl", "--user", "is-active", unit_name],
                        capture_output=True,
                        text=True,
                        timeout=4,
                    )
                    alive = (u_chk.stdout or "").strip() in ("active", "activating")

            if elapsed > timeout_sec:
                if unit_name:
                    subprocess.run(
                        ["systemctl", "--user", "kill", "-s", "SIGKILL", unit_name],
                        capture_output=True,
                        timeout=4,
                    )
                if pid and alive:
                    try:
                        os.kill(int(pid), signal.SIGKILL)
                    except OSError:
                        pass
                with db.transaction(conn):
                    cur_k = conn.execute(
                        "UPDATE effects SET status='FAILED', worker_pid=NULL, unit_name='', last_error=?, completed_at=? WHERE effect_id=? AND status='RUNNING' AND attempt_gen=?",
                        (f"watchdog killed hung worker after {int(elapsed)}s", ts, eff["effect_id"], gen),
                    )
                    if cur_k.rowcount > 0 and eff["kind"] == "launch_worker":
                        wid = str(payload.get("wait_id") or eff["effect_id"])
                        conn.execute(
                            "UPDATE waits SET status='FAILED', result_summary=?, updated_at=? WHERE wait_id=? AND status NOT IN ('CANCELLED', 'DONE')",
                            (f"执行超时({int(elapsed)}s)被看门狗强停", ts, wid),
                        )
                        db.enqueue_effect(
                            conn,
                            "send_qq",
                            {
                                "ref_id": wid,
                                "rule_id": "watchdog_alert",
                                "text": f"⚠️ [{wid}] 后台任务运行超 {int(elapsed // 60)} 分钟被看门狗终止，请检查现场。",
                            },
                        )
                if cur_k.rowcount > 0:
                    killed_effects.append(eff["effect_id"])
            elif not alive:
                # 进程因机器重启或崩溃已不在：按代次 CAS 回退为 PENDING（严防覆盖刚在另一事务标为 DONE 的效果）
                retries = int(eff["retry_count"]) + 1
                new_status = "PENDING" if retries <= 2 else "FAILED"
                with db.transaction(conn):
                    cur_r = conn.execute(
                        """
                        UPDATE effects
                        SET status=?, retry_count=?, worker_pid=NULL, unit_name='',
                            last_error='interrupted_process_recovered',
                            next_run_at=?, completed_at=CASE WHEN ?='FAILED' THEN ? ELSE completed_at END
                        WHERE effect_id=? AND status='RUNNING' AND attempt_gen=?
                        """,
                        (new_status, retries, ts, new_status, ts, eff["effect_id"], gen),
                    )
                    if cur_r.rowcount > 0 and new_status == "FAILED" and eff["kind"] == "launch_worker":
                        wid = str(payload.get("wait_id") or eff["effect_id"])
                        conn.execute(
                            "UPDATE waits SET status='FAILED', result_summary='进程多次意外中断，已停手', updated_at=? WHERE wait_id=? AND status NOT IN ('CANCELLED', 'DONE')",
                            (ts, wid),
                        )
                        db.enqueue_effect(
                            conn,
                            "send_qq",
                            {
                                "ref_id": wid,
                                "rule_id": "watchdog_alert",
                                "text": f"⚠️ [{wid}] 后台任务进程连续意外中断已停手，未盲目继续执行。",
                            },
                        )

        # 2. 清理孤儿 worktree（不在 STAGED/RUNNING/WAITING 中且超过 2 小时的临时目录）
        active_wids = {
            r["wait_id"]
            for r in conn.execute(
                "SELECT wait_id FROM waits WHERE status IN ('WAITING', 'FIRED', 'STAGED')"
            ).fetchall()
        }
        if db.paths.worktrees_dir.is_dir():
            for child in db.paths.worktrees_dir.iterdir():
                if not child.is_dir():
                    continue
                age_sec = now.timestamp() - child.stat().st_mtime
                if child.name not in active_wids and age_sec > 7200:
                    for repo_p in (policy.get("whitelisted_repos") or {}).values():
                        if Path(repo_p).is_dir():
                            subprocess.run(
                                ["git", "worktree", "remove", "--force", str(child)],
                                cwd=repo_p,
                                capture_output=True,
                            )
                    shutil.rmtree(child, ignore_errors=True)
                    cleaned_worktrees.append(child.name)

        # 3. 核查数据源静默期（跳过 UNCONFIGURED 的待用户配置项）
        src_rows = conn.execute("SELECT * FROM sources").fetchall()
        for s in src_rows:
            if s["auth_state"] == "UNCONFIGURED":
                continue
            last_ok = s["last_ok_at"]
            max_sil = int(s["max_silence_sec"] or 3600)
            if last_ok:
                age = (now - parse_iso(last_ok)).total_seconds()
                if age > max_sil:
                    stale_sources.append(s["id"])
                    with db.transaction(conn):
                        conn.execute(
                            "UPDATE sources SET freshness='STALE' WHERE id=?",
                            (s["id"],),
                        )
    finally:
        conn.close()

    try:
        from siming.task_watch import reconcile_interrupted_task_runs

        interrupted_runs = reconcile_interrupted_task_runs(db)
    except Exception:
        interrupted_runs = []

    try:
        recovered_claiming = reconcile_stuck_claiming_approvals(db)
    except Exception:
        recovered_claiming = []

    # 每日执行一次 30 天零真实交互特性冻结巡检
    today_str = now.date().isoformat()
    if db.get_meta("last_freeze_eval_date") != today_str:
        try:
            from siming.doctor import evaluate_30d_feature_freeze

            evaluate_30d_feature_freeze(db, inactive_days=30, apply_freeze=True)
            db.set_meta("last_freeze_eval_date", today_str)
        except Exception:
            pass

    quota = fetch_ai_quota(db)
    cdp = check_cdp_9222_status()
    db.set_meta("watchdog_last_ok_at", ts)

    if killed_effects:
        db.write_audit(
            "A1",
            "watchdog_kill",
            {"killed_effects": killed_effects},
            detail="killed hung >30m workers",
        )

    return {
        "ok": True,
        "ts": ts,
        "killed_effects": killed_effects,
        "recovered_claiming": recovered_claiming,
        "interrupted_runs": interrupted_runs,
        "cleaned_worktrees": cleaned_worktrees,
        "stale_sources": stale_sources,
        "ai_quota_ok": bool(quota.get("ok")),
        "codex_remaining": quota.get("codex_remaining"),
        "cdp": cdp,
    }
