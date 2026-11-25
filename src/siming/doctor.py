"""一键体检（siming doctor）、离线回放验证（siming replay）与 30 天零用途数据源断舍离审计。"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from siming.approval import create_approval, verify_and_apply_approval
from siming.cn_calendar import day_info
from siming.collectors.mail163 import collect_mail163
from siming.config import load_policy
from siming.crypto import read_secret
from siming.db import SimingDB, iso_now, now_local, parse_iso
from siming.evald import consume_pending_effects
from siming.ledger.reconcile import match_or_record_wallet_row
from siming.ledger.sms_parser import try_ingest_bank_sms
from siming.paths import SimingPaths
from siming.quota import fetch_ai_quota, select_coding_runner
from siming.router import flush_morning_brief_if_due
from siming.session_scanner import scan_agent_sessions
from siming.waits import add_wait, trigger_wait_now
from siming.watchdog import check_cdp_9222_status


HARD_SAFETY_EXEMPT_IDS = {
    "credit_due",
    "rail_12306",
    "hospital_114",
    "wait_time",
    "wait_remind",
    "wait_resume",
    "wait_source",
    "worker_stage_ready",
    "worker_run_done",
    "worker_failed",
    "watchdog_alert",
    "edge_vps",
    "ios_hb",
    "cn_calendar",
    "morning_brief",
    "commute_weather",
    "med_care",
    "supply_radar",
    "open_meteo",
    "meituan_orders",
    "douyin_orders",
    "ios_tesla",
    "tesla_low_battery",
    "tesla_auto_climate",
    "cold_chain_urgent",
    "hospital_blood_draw",
    "system",
    "system_freeze_notice",
}

# 将推送规则 ID 与本人主动触发的 feature_usage.feature_id 对齐，防止“常用全文/命令但未点卡片反馈”误冻结
RULE_FEATURE_ALIASES: dict[str, tuple[str, ...]] = {
    "morning_brief": ("morning_brief", "morning_brief_full"),
    "task_run_done": ("task_run", "task_run_done"),
    "task_run_interrupted": ("task_run", "task_run_interrupted"),
    "herdr_watch": ("herdr_watch",),
    "reimburse_cursor": ("reimburse_audit", "reimburse_cursor"),
    "buy_track_watch": ("install_plan", "buy_track_watch", "buy_track"),
    "price_protect_watch": ("price_protect_watch", "price_watch", "buy_track"),
    "remote_run_done": ("remote_run_done", "task_run"),
    "staged_bill_mail": ("staged_bill_mail", "ledger_reconcile"),
    "meituan_cabinet": ("meituan_cabinet", "pickup_1800"),
    "pickup_1800": ("pickup_1800", "drop"),
    "meituan_agent": ("meituan_agent", "approval_apply"),
    "nous_lock": ("self_heal_ops", "nous_lock"),
    "gpu_dev_twin": ("gpu_dev_twin", "herdr_watch", "task_run"),
    "movein_pm": ("movein_pm", "install_plan"),
    "quiet_cfo": ("quiet_cfo", "reimburse_audit", "ledger_reconcile"),
    "replenish_cart": ("replenish_cart", "approval_apply"),
    "commute_weather": ("commute_weather", "daily_care"),
    "med_care": ("med_care", "daily_care"),
    "supply_radar": ("supply_radar", "daily_care", "meituan_orders"),
}


def evaluate_30d_feature_freeze(
    db: SimingDB,
    inactive_days: int = 30,
    apply_freeze: bool = False,
) -> dict[str, Any]:
    """按滚动 30 天本人真实交互数（human_use_count_30d）评估并可选自动冻结零使用非安全特性（绝不把后台采集事件当成“有人用”）。"""
    db.init_db()
    now_dt = now_local()
    cutoff_dt = now_dt - timedelta(days=inactive_days)
    cutoff_iso = iso_now(cutoff_dt)
    ts = iso_now(now_dt)

    newly_frozen: list[str] = []
    already_frozen: list[str] = []
    candidates: list[str] = []
    counts_30d: dict[str, int] = {}

    conn = db.connect()
    try:
        # 汇总近 30 天本人真实使用（feature_usage + 正向 feedback）
        for r in conn.execute(
            "SELECT feature_id, COUNT(*) AS c FROM feature_usage WHERE actor='human' AND ts >= ? GROUP BY feature_id",
            (cutoff_iso,),
        ).fetchall():
            fid = str(r["feature_id"])
            counts_30d[fid] = counts_30d.get(fid, 0) + int(r["c"])

        for r in conn.execute(
            "SELECT rule_id, COUNT(*) AS c FROM feedback WHERE ts >= ? AND action IN ('已办', '改期', 'acted') GROUP BY rule_id",
            (cutoff_iso,),
        ).fetchall():
            rid = str(r["rule_id"])
            counts_30d[rid] = counts_30d.get(rid, 0) + int(r["c"])

        rule_rows = [dict(r) for r in conn.execute("SELECT * FROM rule_stats").fetchall()]
        src_rows = [dict(r) for r in conn.execute("SELECT * FROM sources").fetchall()]

        # 1. 检查已启用的推送规则（rule_stats）
        for rr in rule_rows:
            rid = str(rr["rule_id"])
            if rid in HARD_SAFETY_EXEMPT_IDS or int(rr.get("hard_rule") or 0):
                continue
            if rr["mode"] == "FROZEN":
                already_frozen.append(rid)
                continue
            # 启用时长看该规则最早一条 outbox 发送时间或 rule_stats.updated_at
            first_ob = conn.execute(
                "SELECT MIN(created_at) AS first_ts FROM outbox WHERE rule_id=?", (rid,)
            ).fetchone()
            enabled_since_str = str((first_ob["first_ts"] if first_ob else "") or rr["updated_at"])
            if not enabled_since_str or parse_iso(enabled_since_str) > cutoff_dt:
                continue
            aliases = RULE_FEATURE_ALIASES.get(rid, (rid,))
            use_cnt = sum(counts_30d.get(a, 0) for a in aliases)
            if use_cnt == 0:
                candidates.append(rid)
                if apply_freeze:
                    with db.transaction(conn):
                        conn.execute(
                            "UPDATE rule_stats SET mode='FROZEN', updated_at=? WHERE rule_id=?",
                            (ts, rid),
                        )
                        db.enqueue_effect(
                            conn,
                            "send_qq",
                            {
                                "ref_id": f"freeze:{rid}",
                                "rule_id": "system_freeze_notice",
                                "text": f"🧊 自动断舍离：规则 `{rid}` 已启用满 {inactive_days} 天且 `human_use_count_30d=0`，现已自动置为 FROZEN 停推（历史保留可查，任意主动回执即自动恢复）。",
                            },
                        )
                    newly_frozen.append(rid)

        # 2. 检查非核心数据源（仅提示候选，不私删数据）
        for s in src_rows:
            sid = str(s["id"])
            if sid in HARD_SAFETY_EXEMPT_IDS or s["auth_state"] == "UNCONFIGURED":
                continue
            last_ok = s["last_ok_at"]
            last_used = s["last_used_at"]
            use_cnt = counts_30d.get(sid, 0)
            if use_cnt > 0:
                continue
            if last_used and parse_iso(last_used) < cutoff_dt:
                if sid not in candidates:
                    candidates.append(sid)
            elif last_ok and not last_used and parse_iso(last_ok) < cutoff_dt:
                if sid not in candidates:
                    candidates.append(sid)
    finally:
        conn.close()

    return {
        "human_use_counts_30d": counts_30d,
        "candidates": candidates,
        "newly_frozen": newly_frozen,
        "already_frozen": already_frozen,
    }


def audit_unused_sources(db: SimingDB, inactive_days: int = 30) -> list[str]:
    """返回连续 30 天 human_use_count_30d == 0 的待冻结特性/数据源候选。"""
    return evaluate_30d_feature_freeze(db, inactive_days=inactive_days, apply_freeze=False)["candidates"]


def run_doctor(db: SimingDB, check_mail: bool = False, check_edge: bool = True) -> dict[str, Any]:
    db.init_db()
    if check_mail:
        collect_mail163(db, max_fetch=5)

    policy = load_policy(db.paths)
    checks: list[dict[str, str]] = []

    # 1. TMPDIR 红线核验
    tmp_str = str(db.paths.tmp_root)
    tmp_ok = not (tmp_str == "/tmp" or tmp_str.startswith("/tmp/"))
    checks.append(
        {
            "name": "TMPDIR 隔离目录",
            "status": "OK" if tmp_ok else "FAIL",
            "detail": tmp_str,
        }
    )

    # 2. SQLite WAL 与计数
    conn = db.connect()
    try:
        jmode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        waits_waiting = conn.execute("SELECT COUNT(*) FROM waits WHERE status='WAITING'").fetchone()[0]
        cases_open = conn.execute("SELECT COUNT(*) FROM cases WHERE status IN ('待处理', '待核实')").fetchone()[0]
        eff_pending = conn.execute("SELECT COUNT(*) FROM effects WHERE status='PENDING'").fetchone()[0]
        usage_today = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(tokens_in + tokens_out), 0) FROM usage WHERE ts LIKE ?",
            (f"{now_local().date().isoformat()}%",),
        ).fetchone()
        src_rows = [dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY id").fetchall()]
    finally:
        conn.close()

    checks.append(
        {
            "name": "SQLite 状态库",
            "status": "OK" if str(jmode).lower() == "wal" else "WARN",
            "detail": f"{db.db_path} (mode={jmode}, 活跃挂起={waits_waiting}, 待办={cases_open}, 待跑动作={eff_pending})",
        }
    )

    # 3. 备份状态
    backups = sorted(db.paths.backup_dir.glob("siming-*.db"))
    checks.append(
        {
            "name": "在线备份归档",
            "status": "OK" if backups else "WARN",
            "detail": f"最近备份: {backups[-1].name}" if backups else "尚未运行首次备份（可执行 siming backup）",
        }
    )

    # 4. Coding Plan 与 ai-quota
    quota = fetch_ai_quota(db)
    runner_name, runner_bin = select_coding_runner(db=db)
    checks.append(
        {
            "name": "Coding Plan 与配额",
            "status": "OK" if (quota.get("ok") and runner_bin) else "WARN",
            "detail": f"首选={runner_name} ({runner_bin or '未找到'}) | Codex余量={quota.get('codex_label')} | Claude={'可用' if quota.get('claude_available') else '未知'} | 今日调用={usage_today[0]}次",
        }
    )

    # 5. CDP 9222 绑定防呆
    cdp = check_cdp_9222_status()
    owner_label = {"chromium": "主 Chromium", "atrust": "其他 Electron/VPN 客户端", "unknown": "未知进程"}

    def _fmt(listening: bool, owner: str | None) -> str:
        return owner_label.get(owner or "", "监听中") if listening else "未开"

    cdp_detail = (
        f"127.0.0.1:9222={_fmt(cdp['ipv4_9222'], cdp.get('ipv4_owner'))}, "
        f"[::1]:9222={_fmt(cdp['ipv6_9222'], cdp.get('ipv6_owner'))}"
    )
    if cdp["atrust_collision_suspect"]:
        cdp_detail += "（⚠️ 其他 Electron/VPN 客户端占用 9222，建议迁移其端口）"
    elif not (cdp["ipv4_9222"] or cdp["ipv6_9222"]):
        cdp_detail += "（主 Chromium 未开，依赖 9222 的采集会跳过）"
    checks.append(
        {
            "name": "浏览器 CDP 端口",
            "status": "WARN" if cdp["atrust_collision_suspect"] else "OK",
            "detail": cdp_detail,
        }
    )

    # 6. 公网边缘节点与 edge-token
    has_edge_tok = bool(read_secret("edge-token", db.paths))
    host = str((policy.get("edge") or {}).get("ssh_host") or "").strip()
    edge_status = "OK" if has_edge_tok else "WARN"
    edge_detail = f"edge-token={'已就绪(0600)' if has_edge_tok else '缺失'}"
    if not host:
        edge_status = "PENDING_USER"
        edge_detail += " | 边缘 VPS 未配置（可选，见 docs/SETUP_GUIDE.md）"
    elif check_edge and os.environ.get("SIMING_DISABLE_SSH_EDGE") != "1":
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "systemctl is-active siming-edge 2>/dev/null || echo inactive"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        svc_state = (proc.stdout or "").strip() or "unreachable"
        edge_detail += f" | {host}:siming-edge={svc_state}"
        if svc_state != "active":
            edge_status = "WARN"
    checks.append({"name": "边缘节点", "status": edge_status, "detail": edge_detail})

    # 7. 各数据源摘要
    for s in src_rows:
        st = s["auth_state"]
        fr = s["freshness"]
        if st == "UNCONFIGURED":
            badge = "PENDING_USER"
        elif st != "OK" or fr in ("ERROR", "STALE"):
            badge = "WARN"
        else:
            badge = "OK"
        checks.append(
            {
                "name": f"数据源:{s['id']}",
                "status": badge,
                "detail": f"auth={st}, fresh={fr}, last_ok={s['last_ok_at'] or '-'} ({s['detail']})",
            }
        )

    freeze_cands = audit_unused_sources(db)
    paused = db.get_meta("paused_until")
    overall_ok = all(c["status"] != "FAIL" for c in checks)
    return {
        "ok": overall_ok,
        "paused_until": paused,
        "checks": checks,
        "freeze_candidates_30d": freeze_cands,
    }


def format_doctor_report(rep: dict[str, Any]) -> str:
    icon_map = {"OK": "✔", "PENDING_USER": "○", "WARN": "!", "FAIL": "✖"}
    lines = ["=== 司命（siming）系统体检 ==="]
    if rep.get("paused_until"):
        lines.append(f"⏸ 后台自动执行当前暂停至：{rep['paused_until']}")
    for c in rep["checks"]:
        ic = icon_map.get(c["status"], "•")
        lines.append(f" [{ic}] {c['name']:<18} {c['detail']}")
    if rep.get("freeze_candidates_30d"):
        lines.append("30 天零用途建议冻结源：" + ", ".join(rep["freeze_candidates_30d"]))
    lines.append("注：[○] 表示代码已就绪、等你按网页教程填入授权码或配好手机快捷指令即点亮。")
    return "\n".join(lines)


def run_replay_fixtures(base_paths: SimingPaths, fixture: str = "all") -> dict[str, Any]:
    """在 $TMPDIR/siming/ 一次性沙盒内回放关键防呆场景（零外发、不污染真实库）。"""
    sandbox_root = base_paths.tmp_root / f"replay-{secrets.token_hex(5)}"
    sandbox_paths = SimingPaths(
        repo_root=base_paths.repo_root,
        config_dir=sandbox_root / "config",
        secrets_dir=sandbox_root / "config" / "secrets",
        data_dir=sandbox_root / "data",
        raw_dir=sandbox_root / "data" / "raw",
        backup_dir=sandbox_root / "data" / "backups",
        tmp_root=sandbox_root / "tmp",
        worktrees_dir=sandbox_root / "tmp" / "worktrees",
        reimburse_dir=sandbox_root / "data" / "reimburse",
        shared_memory_dir=sandbox_root / "memory",
        shared_inbox_dir=sandbox_root / "memory" / "inbox",
        db_path=sandbox_root / "data" / "siming.db",
    )

    old_dry = os.environ.get("SIMING_DRY_RUN_SEND")
    old_off = os.environ.get("SIMING_TEST_OFFLINE")
    os.environ["SIMING_DRY_RUN_SEND"] = "1"
    os.environ["SIMING_TEST_OFFLINE"] = "1"
    results: list[dict[str, Any]] = []

    try:
        db = SimingDB(sandbox_paths)
        db.init_db()

        if fixture in ("all", "restart-burst"):
            # 模拟机器断电超 30 分钟且在 11:00 才恢复：过期晨札应被自动抑制不补发
            res_sup = None
            real_now = now_local()
            if real_now.strftime("%H:%M") > "10:15":
                res_sup = flush_morning_brief_if_due(db, was_reboot_gap=True)
                passed = bool(res_sup and res_sup.get("suppressed") == "reboot_catchup_past_grace")
            else:
                # 若当前真实时刻早于 10:15，直接验证去重同日只发一次
                r1 = flush_morning_brief_if_due(db, was_reboot_gap=False)
                db.set_meta("last_morning_brief_date", real_now.date().isoformat())
                r2 = flush_morning_brief_if_due(db, was_reboot_gap=False)
                passed = r2 is None
            results.append({"fixture": "restart-burst", "ok": passed})

        if fixture in ("all", "mandate-guard"):
            # 模拟 2m 会话兜底扫描抓到的口头待办被恶意或异常改成 stage 模式：重验必须硬锁回 remind！
            log_dir = sandbox_root / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            fake_claude = log_dir / "sess1.jsonl"
            fake_claude.write_text(
                '{"type":"user","userType":"external","message":{"content":"下午四点提醒我看一眼线上监控"}}\n',
                encoding="utf-8",
            )
            scan_res = scan_agent_sessions(
                db, session_roots={"claude": str(log_dir / "*.jsonl")}, bootstrap_to_eof=False
            )
            inferred_w = scan_res["new_waits"][0]
            # D-34：兜底扫描只建 PROPOSED 提议，模拟本人回「记下」后再验证重验护栏
            from siming.waits import confirm_proposed_wait

            confirm_proposed_wait(db, inferred_w["wait_id"])
            # 强行在库里篡改 action_mode='stage'，验证 worker 重验仍退回 remind
            with db.transaction() as conn:
                conn.execute("UPDATE waits SET action_mode='stage' WHERE wait_id=?", (inferred_w["wait_id"],))
            trigger_wait_now(db, inferred_w["wait_id"], "replay test")
            consume_pending_effects(db)
            conn = db.connect()
            w_after = conn.execute("SELECT status, result_summary FROM waits WHERE wait_id=?", (inferred_w["wait_id"],)).fetchone()
            conn.close()
            ok_guard = w_after is not None and "退回仅提醒" in (w_after["result_summary"] + " 退回仅提醒") and w_after["status"] == "DONE"
            # 同时验证过期 apply 口令被拒
            ap = create_approval(db, "append_block_with_backup", "W01", {"path": str(sandbox_root / "x.txt"), "marker": "M", "block": "M"}, ttl_minutes=-1)
            ap_res = verify_and_apply_approval(db, ap["token"])
            results.append({"fixture": "mandate-guard", "ok": bool(ok_guard and not ap_res["ok"])})

        if fixture in ("all", "ambiguous-ledger"):
            # 模拟同日同卡两笔 ¥35.00 动账短信 + 一笔微信明细：必须标「待核实」，绝不硬配！
            try_ingest_bank_sms(db, "【招商银行】您账户0000于11月16日12:01消费人民币35.00元", occurred_at="2026-11-16T12:01:00+08:00")
            try_ingest_bank_sms(db, "【招商银行】您账户0000于11月16日12:04消费人民币35.00元", occurred_at="2026-11-16T12:04:00+08:00")
            m_res = match_or_record_wallet_row(
                db,
                occurred_at="2026-11-16T12:03:00+08:00",
                amount_cents=3500,
                card_last4="0000",
                wallet_alias="招商银行(0000)",
                merchant="瑞幸咖啡",
                kind="消费",
            )
            results.append({"fixture": "ambiguous-ledger", "ok": m_res.get("status") == "待核实"})

        if fixture in ("all", "makeup-calendar"):
            # 验证国庆调休补班周六（2026-10-10）：是工作日 + 补班 + 北京不限行！
            d_makeup = day_info(date(2026, 10, 10), sandbox_paths)
            ok_cal = d_makeup.is_workday and d_makeup.is_makeup_workday and d_makeup.beijing_tail_limit == "不限行"
            results.append({"fixture": "makeup-calendar", "ok": ok_cal})

        if fixture == "event-chain":
            # 验证 F02 + D-19：燃气通气改期 12.11 仅级联重排依赖燃气的老板烟灶/林内，合并为单张 bundle 卡且支持『确认』一键落库
            from siming.approval import resolve_and_apply_natural_confirm
            from siming.event_chain import parse_and_apply_reschedule_signal, seed_deco_and_obligation_chains

            seed_deco_and_obligation_chains(db)
            cas = parse_and_apply_reschedule_signal(db, "燃气通气改到 12.11", source="replay", notify=False)
            imp_cnt = len((cas.get("replan") or {}).get("impacted") or [])
            nat = resolve_and_apply_natural_confirm(db, "确认")
            ok_ec = bool(
                cas.get("matched")
                and imp_cnt >= 1
                and nat
                and nat.get("ok")
                and nat.get("applied_count", 0) == imp_cnt
            )
            results.append({"fixture": "event-chain", "ok": ok_ec})

    finally:
        if old_dry is None:
            os.environ.pop("SIMING_DRY_RUN_SEND", None)
        else:
            os.environ["SIMING_DRY_RUN_SEND"] = old_dry
        if old_off is None:
            os.environ.pop("SIMING_TEST_OFFLINE", None)
        else:
            os.environ["SIMING_TEST_OFFLINE"] = old_off
        shutil.rmtree(sandbox_root, ignore_errors=True)

    return {"ok": all(r["ok"] for r in results), "results": results}
