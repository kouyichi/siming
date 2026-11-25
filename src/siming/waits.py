"""wait_source：统一管理 time / event / probe / intent 四类挂起触发器与 ACTIVE_WAITS.md 导出。"""

from __future__ import annotations

import ipaddress
import json
import os
import re
import socket
import sqlite3
import subprocess
import urllib.parse
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

import httpx

from siming.cn_calendar import compile_time_trigger, parse_duration_seconds
from siming.config import load_policy
from siming.crypto import redact_sensitive
from siming.db import SimingDB, iso_now, now_local, parse_iso
from siming.mandate import Mandate, revalidate_mandate

VALID_TRIGGER_KINDS = ("time", "event", "probe", "intent")
_SHELL_META_RE = re.compile(r"[;&|`$<>\\\n\r]")


def validate_and_parse_probe(expr: str) -> tuple[str, dict[str, Any]]:
    """将 probe 表达式解析为安全类型化探针，严禁裸 /bin/bash -c 执行任意命令。"""
    s = (expr or "").strip()
    if not s:
        raise ValueError("probe 表达式不能为空")

    # 兼容历史 SKILL 示例：`gh pr view 418 --repo owner/repo ... MERGED`
    m_legacy_gh = re.fullmatch(
        r"gh\s+pr\s+view\s+(\d+)\s+--repo\s+([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\s+--json\s+state\s+-q\s+\.state\s*\|\s*grep\s+-qx\s+MERGED",
        s,
    )
    if m_legacy_gh:
        return ("gh_pr_merged", {"repo": m_legacy_gh.group(2), "number": int(m_legacy_gh.group(1))})

    if _SHELL_META_RE.search(s):
        raise ValueError(
            f"安全拦截：禁止在 probe 中使用 shell 元字符或裸命令 ({s[:60]})。"
            " 请使用类型化探针：file_exists:<path> / file_missing:<path> / pid_gone:<pid> / "
            "port_open:<port> / gh_pr_merged:<owner/repo>#<n> / gh_pr_checks:<owner/repo>#<n> / url_200:<https://...>"
        )

    # 1. 类型化前缀
    if s.startswith("file_exists:"):
        p = s[len("file_exists:") :].strip()
        if not p:
            raise ValueError("file_exists: 缺少路径")
        return ("file_exists", {"path": p})
    if s.startswith("file_missing:"):
        p = s[len("file_missing:") :].strip()
        if not p:
            raise ValueError("file_missing: 缺少路径")
        return ("file_missing", {"path": p})
    if s.startswith("pid_gone:"):
        pid_s = s[len("pid_gone:") :].strip()
        if not pid_s.isdigit():
            raise ValueError("pid_gone: 需要纯数字 PID")
        return ("pid_gone", {"pid": int(pid_s)})
    if s.startswith("port_open:"):
        port_s = s[len("port_open:") :].strip()
        if not port_s.isdigit() or not (1 <= int(port_s) <= 65535):
            raise ValueError("port_open: 端口范围需在 1..65535")
        return ("port_open", {"port": int(port_s)})
    if s.startswith("gh_pr_merged:"):
        spec = s[len("gh_pr_merged:") :].strip()
        m = re.fullmatch(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)", spec)
        if not m:
            raise ValueError("gh_pr_merged 格式应为 owner/repo#123")
        return ("gh_pr_merged", {"repo": m.group(1), "number": int(m.group(2))})
    if s.startswith("gh_pr_checks:"):
        spec = s[len("gh_pr_checks:") :].strip()
        m = re.fullmatch(r"([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#(\d+)", spec)
        if not m:
            raise ValueError("gh_pr_checks 格式应为 owner/repo#123")
        return ("gh_pr_checks", {"repo": m.group(1), "number": int(m.group(2))})
    if s.startswith("url_200:"):
        u = s[len("url_200:") :].strip()
        parsed = urllib.parse.urlsplit(u)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("url_200: 仅允许 https:// 公网域名")
        return ("url_200", {"url": u, "hostname": parsed.hostname})
    if s.startswith("herdr:"):
        pane = s[len("herdr:") :].strip()
        if not re.fullmatch(r"[A-Za-z0-9_:-]{1,32}", pane):
            raise ValueError("herdr: 非法 pane 标识")
        return ("herdr", {"pane": pane})

    # 2. 受限安全兼容格式：`test -f <path>` / `test -e <path>` / `test ! -d /proc/<pid>` / `test ! -f <path>`
    m_test = re.fullmatch(r"test\s+(!\s+)?-(f|e|d)\s+([^\s]+)", s)
    if m_test:
        neg = bool(m_test.group(1))
        flag = m_test.group(2)
        target = m_test.group(3)
        if neg and flag == "d" and re.fullmatch(r"/proc/\d+", target):
            return ("pid_gone", {"pid": int(target.split("/")[-1])})
        if neg:
            return ("file_missing", {"path": target})
        return ("file_exists", {"path": target, "flag": flag})

    raise ValueError(
        f"不支持非白名单裸命令探针 ({s[:60]})；请改用 file_exists: / pid_gone: / port_open: / gh_pr_merged: / url_200: / herdr:"
    )


def _parse_when_spec(
    when_spec: str, db: SimingDB
) -> tuple[str, str, Optional[str], bool]:
    """解析 --when 参数，返回 (trigger_kind, trigger_expr, next_fire_at, silent_night)。"""
    raw = when_spec.strip()
    for prefix in VALID_TRIGGER_KINDS:
        if raw.startswith(f"{prefix}:"):
            body = raw[len(prefix) + 1 :].strip()
            if prefix == "time":
                dt, silent = compile_time_trigger(body, paths=db.paths)
                return ("time", body, iso_now(dt), silent)
            if prefix == "probe":
                validate_and_parse_probe(body)
                return ("probe", body, iso_now(now_local()), False)
            return (prefix, body, None, False)

    # 未显式带前缀时，默认按时间表达式编译
    dt, silent = compile_time_trigger(raw, paths=db.paths)
    return ("time", raw, iso_now(dt), silent)


def add_wait(
    db: SimingDB,
    when: str,
    mode: str = "remind",
    strength: str = "explicit",
    quote: str = "",
    task: str = "",
    context: str = "",
    cwd: str = "",
    agent: str = "",
    session_id: str = "",
    runner: str = "",
    done_check: str = "",
    every: str = "30m",
    ttl: str = "14d",
    case_id: Optional[str] = None,
    initial_status: str = "WAITING",
) -> dict[str, Any]:
    """新增一条挂起项（强制带 expires_at，上限 30 天；inferred 强制锁 remind）。

    initial_status='PROPOSED'：仅供会话兜底扫描使用（D-34），建档但不触发，等本人回「记下 Wxx」才转 WAITING。
    """
    db.init_db()
    if initial_status not in ("WAITING", "PROPOSED"):
        raise ValueError(f"非法 initial_status：{initial_status}")
    policy = load_policy(db.paths)
    guard = policy["guardrails"]

    effective_strength = "explicit" if strength == "explicit" else "inferred"
    effective_mode = mode if (effective_strength == "explicit" and mode in ("remind", "resume", "stage", "run")) else "remind"

    max_days_key = "max_explicit_wait_ttl_days" if effective_strength == "explicit" else "max_wait_ttl_days"
    default_max_days = 400 if effective_strength == "explicit" else 30
    max_ttl_sec = int(guard.get(max_days_key, default_max_days)) * 86400
    default_ttl_sec = int(guard.get("default_wait_ttl_days", 14)) * 86400
    ttl_sec = parse_duration_seconds(ttl) if ttl else default_ttl_sec
    ttl_sec = max(60, min(ttl_sec, max_ttl_sec))
    now_dt = now_local()
    expires_dt = now_dt + timedelta(seconds=ttl_sec)

    probe_sec = max(60, parse_duration_seconds(every)) if every else 1800

    trigger_kind, trigger_expr, next_fire_at, silent_night = _parse_when_spec(when, db)
    if next_fire_at and trigger_kind == "time":
        fire_dt = parse_iso(next_fire_at)
        min_exp_dt = fire_dt + timedelta(hours=24)
        hard_cap_dt = now_dt + timedelta(seconds=max_ttl_sec)
        if effective_strength == "explicit" and min_exp_dt > hard_cap_dt:
            raise ValueError(f"目标触发时间 {next_fire_at} 超出显式挂起最大年限 ({default_max_days}d)")
        if expires_dt < min_exp_dt:
            expires_dt = min(min_exp_dt, hard_cap_dt)
    resolved_cwd = str(Path(cwd).expanduser().resolve()) if cwd else ""
    session_ref = f"{agent}:{session_id}" if (agent or session_id) else ""

    mandate = Mandate(
        policy_version=int(policy.get("policy_version", 1)),
        strength=effective_strength,
        action_mode=effective_mode,
        cwd=resolved_cwd,
        agent=agent,
        session_id=session_id,
        runner=runner,
        task=task,
        context=context,
        done_check=done_check,
        silent_night=silent_night,
    )

    ts = iso_now()
    with db.transaction() as conn:
        wait_id = db.next_seq_id(conn, "W")
        conn.execute(
            """
            INSERT INTO waits (
                wait_id, status, strength, trigger_kind, trigger_expr,
                probe_interval_sec, next_fire_at, expires_at, action_mode,
                session_ref, mandate_json, quote, case_id, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                wait_id,
                initial_status,
                effective_strength,
                trigger_kind,
                trigger_expr,
                probe_sec,
                next_fire_at,
                iso_now(expires_dt),
                effective_mode,
                session_ref,
                mandate.to_json(),
                redact_sensitive(quote, db.paths),
                case_id,
                ts,
                ts,
            ),
        )
        # 若为显式硬时间提醒且在未来 >15m，顺手入队同步到 81 预排发件箱兜底（强制先脱敏再入库/外发！）
        if trigger_kind == "time" and next_fire_at and effective_strength == "explicit":
            fire_dt = parse_iso(next_fire_at)
            if fire_dt > now_local() + timedelta(minutes=15) and not silent_night:
                safe_rem = redact_sensitive(quote or task or trigger_expr, db.paths)
                conn.execute(
                    """
                    INSERT INTO outbox (
                        id, ref_id, rule_id, text, send_at, status, channel,
                        hard_deadline, edge_synced, created_at
                    ) VALUES (?, ?, 'wait_time', ?, ?, 'PRESCHEDULED', 'qqbot', 1, 0, ?)
                    """,
                    (
                        f"OB-{wait_id}",
                        wait_id,
                        f"[{wait_id}] 到点提醒：{safe_rem}",
                        next_fire_at,
                        ts,
                    ),
                )
                db.enqueue_effect(
                    conn,
                    "edge_sync_outbox",
                    {"outbox_id": f"OB-{wait_id}", "ref_id": wait_id},
                )

        if effective_strength == "explicit":
            db.record_human_use("wait_source", "defer_add", ref_id=wait_id, conn=conn)
        db.write_audit(
            tier="A1",
            action="wait_add",
            params={
                "wait_id": wait_id,
                "trigger_kind": trigger_kind,
                "trigger_expr": trigger_expr,
                "mode": effective_mode,
                "strength": effective_strength,
            },
            target_id=wait_id,
            conn=conn,
        )

    export_active_waits_md(db)
    return {
        "wait_id": wait_id,
        "trigger_kind": trigger_kind,
        "trigger_expr": trigger_expr,
        "next_fire_at": next_fire_at,
        "expires_at": iso_now(expires_dt),
        "action_mode": effective_mode,
        "strength": effective_strength,
        "probe_interval_sec": probe_sec,
        "quote": redact_sensitive(quote, db.paths),
    }


def list_waits(db: SimingDB, status: Optional[str] = "WAITING") -> list[dict[str, Any]]:
    db.init_db()
    conn = db.connect()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM waits WHERE status = ? ORDER BY created_at ASC",
                (status,),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM waits ORDER BY created_at DESC LIMIT 50").fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _withdraw_all_local_queues_for_wait(conn, wid: str) -> None:
    conn.execute(
        "UPDATE outbox SET status='WITHDRAWN' WHERE ref_id=? AND status IN ('PRESCHEDULED', 'MORNING_QUEUE', 'QUEUED')",
        (wid,),
    )
    conn.execute(
        "UPDATE approvals SET status='INVALIDATED' WHERE target_id=? AND status='PENDING'",
        (wid,),
    )
    # 取消尚未发出的 send_qq / launch_worker 待跑 effect
    pend_effs = conn.execute(
        "SELECT effect_id, payload_json FROM effects WHERE status='PENDING' AND kind IN ('send_qq', 'launch_worker')"
    ).fetchall()
    for er in pend_effs:
        try:
            pj = json.loads(er["payload_json"] or "{}")
        except Exception:
            pj = {}
        if pj.get("ref_id") == wid or pj.get("wait_id") == wid:
            conn.execute("UPDATE effects SET status='CANCELLED' WHERE effect_id=? AND status='PENDING'", (er["effect_id"],))


def _safe_pattern_match(expr: str, text: str) -> bool:
    """安全匹配用户/推断表达式：拦截超长或嵌套量词 ReDoS 表达式，回退为安全分词匹配。"""
    pat = (expr or "").strip()
    if not pat:
        return False
    if len(pat) > 160 or re.search(r"(\+|\*|\})\s*\)\s*(\+|\*|\{)", pat):
        return any(tok.strip() and tok.strip().lower() in text.lower() for tok in pat.split("|"))
    try:
        return bool(re.search(pat, text[:4096], re.IGNORECASE))
    except re.error:
        return any(tok.strip() and tok.strip().lower() in text.lower() for tok in pat.split("|"))


def cancel_wait(db: SimingDB, wait_id: str, reason: str = "用户取消") -> bool:
    wid = wait_id.strip().upper()
    ts = iso_now()
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM waits WHERE wait_id = ?", (wid,)).fetchone()
        if not row or row["status"] in ("CANCELLED", "DONE"):
            return False
        conn.execute(
            "UPDATE waits SET status='CANCELLED', result_summary=?, updated_at=? WHERE wait_id=?",
            (reason, ts, wid),
        )
        conn.execute(
            "UPDATE expectations SET status='CANCELLED', updated_at=? WHERE source_ref=? AND status IN ('ACTIVE', 'SHIFTED')",
            (ts, wid),
        )
        conn.execute(
            "UPDATE curiosity_questions SET status='DISMISSED', answer='提醒已取消', answered_at=? WHERE topic='trip_destination' AND status IN ('PENDING','ASKED') AND context_json LIKE ?",
            (ts, f"%{wid}%"),
        )
        _withdraw_all_local_queues_for_wait(conn, wid)
        db.enqueue_effect(conn, "edge_withdraw_outbox", {"ref_id": wid})
        db.record_human_use("wait_source", "defer_cancel", ref_id=wid, conn=conn)
        db.write_audit("A1", "wait_cancel", {"reason": reason}, target_id=wid, conn=conn)
    export_active_waits_md(db)
    return True


def confirm_proposed_wait(db: SimingDB, wait_id: str) -> dict[str, Any]:
    """D-34：本人回「记下 Wxx」后把兜底提议转为真正挂起（仍锁 remind）。"""
    wid = wait_id.strip().upper()
    ts = iso_now()
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM waits WHERE wait_id = ?", (wid,)).fetchone()
        if not row:
            return {"ok": False, "reply": f"没有找到 {wid}"}
        if row["status"] == "WAITING":
            return {"ok": True, "reply": f"[{wid}] 已经在挂起列表里了"}
        if row["status"] != "PROPOSED":
            return {"ok": False, "reply": f"[{wid}] 当前状态是 {row['status']}，不能再记下（可重新说一遍让我登记）"}
        conn.execute(
            "UPDATE waits SET status='WAITING', result_summary='本人确认记下', updated_at=? WHERE wait_id=?",
            (ts, wid),
        )
        db.record_human_use("wait_source", "proposal_confirm", ref_id=wid, conn=conn)
        db.write_audit("A1", "wait_proposal_confirm", {"wait_id": wid}, target_id=wid, conn=conn)
    export_active_waits_md(db)
    when = row["next_fire_at"] or f"{row['trigger_kind']}:{row['trigger_expr']}"
    return {"ok": True, "reply": f"✅ [{wid}] 已记下：{str(row['quote'] or '')[:40]}（触发：{when}）"}


def dismiss_proposed_wait(db: SimingDB, wait_id: str) -> dict[str, Any]:
    wid = wait_id.strip().upper()
    ts = iso_now()
    with db.transaction() as conn:
        row = conn.execute("SELECT status FROM waits WHERE wait_id = ?", (wid,)).fetchone()
        if not row or row["status"] != "PROPOSED":
            return {"ok": False, "reply": f"[{wid}] 不是待确认的提议，无需忽略"}
        conn.execute(
            "UPDATE waits SET status='CANCELLED', result_summary='本人忽略兜底提议', updated_at=? WHERE wait_id=?",
            (ts, wid),
        )
        db.write_audit("A1", "wait_proposal_dismiss", {"wait_id": wid}, target_id=wid, conn=conn)
    return {"ok": True, "reply": f"👌 [{wid}] 已忽略，不会提醒"}


def expire_stale_proposals(db: SimingDB, max_age_hours: int = 72) -> int:
    """兜底提议 72 小时内没被确认就静默过期（不打扰、不发消息）。"""
    cutoff = iso_now(now_local() - timedelta(hours=max_age_hours))
    with db.transaction() as conn:
        n = conn.execute(
            "UPDATE waits SET status='EXPIRED', result_summary='兜底提议未确认，自动过期', updated_at=? "
            "WHERE status='PROPOSED' AND created_at < ?",
            (iso_now(), cutoff),
        ).rowcount
    return int(n or 0)


def export_active_waits_md(db: SimingDB) -> Path:
    """把当前活跃挂起项精简索引导出到 Obsidian 60-共享记忆/ACTIVE_WAITS.md。"""
    waits = list_waits(db, status="WAITING")
    out_dir = db.paths.shared_memory_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / "ACTIVE_WAITS.md"

    lines = [
        "---",
        "title: 当前挂起任务与意图触发索引（由 siming 自动维护）",
        f"updated: {iso_now()}",
        "---",
        "",
        "# 当前挂起项（ACTIVE_WAITS）",
        "",
        "> 本文件由 `siming` 自动同步，供各编码 Agent 与本人在命中相关意图时快速感知。请勿手改表格，取消请跑 `siming defer cancel <ID>`。",
        "",
    ]
    if not waits:
        lines.append("当前无待触发挂起项。")
    else:
        lines.append("| 编号 | 类型 | 触发条件 | 唤醒模式 | 原话 / 任务摘要 | 过期时间 |")
        lines.append("|---|---|---|---|---|---|")
        for w in waits:
            mandate = Mandate.from_json(w["mandate_json"])
            summary = (w["quote"] or mandate.task or "").replace("|", "\\|").replace("\n", " ")[:60]
            expr = str(w["trigger_expr"]).replace("|", "\\|")[:50]
            nxt = w["next_fire_at"] or expr
            lines.append(
                f"| `{w['wait_id']}` | `{w['trigger_kind']}` | `{nxt}` | `{w['action_mode']}` ({w['strength']}) | {summary} | `{w['expires_at'][:10]}` |"
            )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path


def _check_hourly_breaker(conn: sqlite3.Connection, rule_id: str, max_per_hour: int) -> bool:
    """防死循环熔断：单条规则或探针 1 小时内触发超限则冷却 1 小时。"""
    now = now_local()
    bucket = now.strftime("%Y%m%d%H")
    ts = iso_now(now)
    row = conn.execute("SELECT * FROM rule_stats WHERE rule_id = ?", (rule_id,)).fetchone()
    if row and row["cooldown_until"]:
        if parse_iso(row["cooldown_until"]) > now:
            return False

    fires = (int(row["fires_last_hour"]) + 1) if (row and row["hour_bucket"] == bucket) else 1
    cooldown = iso_now(now + timedelta(hours=1)) if fires > max_per_hour else None
    conn.execute(
        """
        INSERT INTO rule_stats (rule_id, mode, fires_last_hour, hour_bucket, cooldown_until, updated_at)
        VALUES (?, 'ACTIVE', ?, ?, ?, ?)
        ON CONFLICT(rule_id) DO UPDATE SET
            fires_last_hour = excluded.fires_last_hour,
            hour_bucket = excluded.hour_bucket,
            cooldown_until = COALESCE(excluded.cooldown_until, rule_stats.cooldown_until),
            updated_at = excluded.updated_at
        """,
        (rule_id, fires, bucket, cooldown, ts),
    )
    return fires <= max_per_hour


def trigger_wait_now(
    db: SimingDB, wait_id: str, trigger_reason: str = "手动或条件命中"
) -> bool:
    wid = wait_id.strip().upper()
    policy = load_policy(db.paths)
    max_fires = int(policy["guardrails"].get("rule_max_fires_per_hour", 10))
    ts = iso_now()

    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM waits WHERE wait_id = ?", (wid,)).fetchone()
        if not row or row["status"] != "WAITING":
            return False
        if not _check_hourly_breaker(conn, f"wait:{wid}", max_fires):
            return False
        conn.execute(
            "UPDATE waits SET status='FIRED', fired_at=?, result_summary=?, updated_at=? WHERE wait_id=?",
            (ts, trigger_reason, ts, wid),
        )
        conn.execute(
            "UPDATE outbox SET status='WITHDRAWN' WHERE ref_id=? AND status='PRESCHEDULED'",
            (wid,),
        )
        db.enqueue_effect(conn, "edge_withdraw_outbox", {"ref_id": wid})
        db.enqueue_effect(
            conn,
            "launch_worker",
            {"wait_id": wid, "trigger_reason": trigger_reason},
        )
    export_active_waits_md(db)
    return True


def _is_public_hostname(hostname: str) -> bool:
    try:
        infos = socket.getaddrinfo(hostname, 443, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        ip_str = info[4][0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            return False
        if not ip_obj.is_global:
            return False
    return True


def _run_readonly_probe(expr: str, cwd: str, db: SimingDB) -> tuple[bool, str]:
    """执行类型化只读安全探针（零 shell 调用，禁止命令注入与内网 SSRF）。"""
    try:
        p_kind, params = validate_and_parse_probe(expr)
    except ValueError as exc:
        return False, str(exc)

    base_dir = Path(cwd).expanduser() if (cwd and Path(cwd).expanduser().is_dir()) else db.paths.tmp_root

    try:
        if p_kind == "file_exists":
            raw_p = Path(str(params["path"])).expanduser()
            target_p = raw_p if raw_p.is_absolute() else (base_dir / raw_p)
            flag = params.get("flag", "f")
            ok = target_p.is_dir() if flag == "d" else (target_p.exists() if flag == "e" else target_p.is_file())
            return ok, f"file_exists({target_p.name})={ok}"

        if p_kind == "file_missing":
            raw_p = Path(str(params["path"])).expanduser()
            target_p = raw_p if raw_p.is_absolute() else (base_dir / raw_p)
            ok = not target_p.exists()
            return ok, f"file_missing({target_p.name})={ok}"

        if p_kind == "pid_gone":
            pid = int(params["pid"])
            try:
                os.kill(pid, 0)
                return False, f"pid {pid} still alive"
            except ProcessLookupError:
                return True, f"pid {pid} exited"
            except PermissionError:
                return False, f"pid {pid} alive (permission)"

        if p_kind == "port_open":
            port = int(params["port"])
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.settimeout(1.0)
                ok = sock.connect_ex(("127.0.0.1", port)) == 0
            return ok, f"127.0.0.1:{port}={'open' if ok else 'closed'}"

        if p_kind in ("gh_pr_merged", "gh_pr_checks"):
            if os.environ.get("SIMING_TEST_OFFLINE") == "1":
                return False, "offline test mode"
            env = {
                "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
                "HOME": str(Path.home()),
                "TMPDIR": str(db.paths.tmp_root.parent),
                "HTTP_PROXY": os.environ.get("HTTP_PROXY", "http://127.0.0.1:7890"),
                "HTTPS_PROXY": os.environ.get("HTTPS_PROXY", "http://127.0.0.1:7890"),
                "GH_CONFIG_DIR": str(Path.home() / ".config" / "gh"),
            }
            repo = str(params["repo"])
            num = str(params["number"])
            if p_kind == "gh_pr_merged":
                proc = subprocess.run(
                    ["gh", "pr", "view", num, "--repo", repo, "--json", "state"],
                    cwd=str(base_dir),
                    env=env,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                if proc.returncode != 0:
                    return False, (proc.stderr or proc.stdout).strip()[:160]
                state = str(json.loads(proc.stdout or "{}").get("state") or "")
                return state == "MERGED", f"{repo}#{num} state={state}"
            proc = subprocess.run(
                ["gh", "pr", "checks", num, "--repo", repo],
                cwd=str(base_dir),
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            return proc.returncode == 0, (proc.stdout or proc.stderr).strip()[:160]

        if p_kind == "url_200":
            if os.environ.get("SIMING_TEST_OFFLINE") == "1":
                return False, "offline test mode"
            from siming.net_guard import safe_pinned_public_get

            code, _, reason = safe_pinned_public_get(
                str(params["url"]),
                require_https=True,
                max_hops=1,
                timeout_sec=5.0,
                status_only=True,
            )
            if code == 0:
                return False, f"blocked or unreachable: {reason}"
            return code == 200, f"HTTP {code}"

        if p_kind == "herdr":
            from siming.task_watch import probe_herdr_pane_idle

            return probe_herdr_pane_idle(db, str(params["pane"]))

        return False, f"unsupported probe kind: {p_kind}"
    except subprocess.TimeoutExpired:
        return False, "probe timeout"
    except Exception as exc:
        return False, str(exc)[:200]


def evaluate_waits(
    db: SimingDB,
    recent_events: Optional[list[dict[str, Any]]] = None,
    intent_texts: Optional[list[str]] = None,
) -> dict[str, list[str]]:
    """推进所有 WAITING 挂起项：归档过期项（同步撤回 81 预排）、检查 time / event / probe / intent。"""
    db.init_db()
    now = now_local()
    ts = iso_now(now)
    expired_ids: list[str] = []
    fired_ids: list[str] = []

    waits = list_waits(db, status="WAITING")
    for w in waits:
        wid = w["wait_id"]
        if parse_iso(w["expires_at"]) <= now:
            with db.transaction() as conn:
                conn.execute(
                    "UPDATE waits SET status='EXPIRED', result_summary='到期自动归档', updated_at=? WHERE wait_id=? AND status='WAITING'",
                    (ts, wid),
                )
                _withdraw_all_local_queues_for_wait(conn, wid)
                db.enqueue_effect(conn, "edge_withdraw_outbox", {"ref_id": wid})
            expired_ids.append(wid)
            continue

        kind = w["trigger_kind"]
        expr = str(w["trigger_expr"])
        mandate = Mandate.from_json(w["mandate_json"])

        if kind == "time" and w["next_fire_at"]:
            if parse_iso(w["next_fire_at"]) <= now:
                if trigger_wait_now(db, wid, f"已到预定时间 {w['next_fire_at']}"):
                    fired_ids.append(wid)

        elif kind == "probe":
            due_probe = (
                w["next_fire_at"] is None
                or parse_iso(w["next_fire_at"]) <= now
            )
            if due_probe:
                chk = revalidate_mandate(
                    mandate=mandate,
                    wait_strength=w["strength"],
                    wait_action_mode=w["action_mode"],
                    db=db,
                )
                if not chk.allowed:
                    continue
                ok, probe_out = _run_readonly_probe(expr, mandate.cwd, db)
                if ok:
                    if trigger_wait_now(db, wid, f"探针满足 ({probe_out or expr[:40]})"):
                        fired_ids.append(wid)
                else:
                    nxt = iso_now(now + timedelta(seconds=int(w["probe_interval_sec"])))
                    with db.transaction() as conn:
                        conn.execute(
                            "UPDATE waits SET last_probed_at=?, next_fire_at=?, updated_at=? WHERE wait_id=? AND status='WAITING'",
                            (ts, nxt, ts, wid),
                        )

        elif kind == "event" and recent_events:
            w_created = parse_iso(w["created_at"])
            for ev in recent_events:
                ev_ing = ev.get("ingested_at")
                if ev_ing and parse_iso(str(ev_ing)) < w_created:
                    continue
                haystack = f"{ev.get('source', '')} {ev.get('thread_key', '')} {ev.get('summary', '')} {ev.get('payload_json', '')}"
                if _safe_pattern_match(expr, haystack):
                    if trigger_wait_now(db, wid, f"命中内部事件 [{ev.get('event_id')}]: {ev.get('summary', '')[:60]}"):
                        fired_ids.append(wid)
                    break

        elif kind == "intent" and intent_texts:
            for txt in intent_texts:
                if _safe_pattern_match(expr, txt):
                    if trigger_wait_now(db, wid, f"检测到关联意图：「{txt[:50]}」"):
                        fired_ids.append(wid)
                    break

    if expired_ids or fired_ids:
        export_active_waits_md(db)
    db.mark_source_ok(
        "wait_source",
        detail=f"active={len(waits) - len(expired_ids) - len(fired_ids)}, fired={len(fired_ids)}, expired={len(expired_ids)}",
        used=bool(fired_ids),
    )
    return {"fired": fired_ids, "expired": expired_ids}
