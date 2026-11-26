"""无状态事件分拣与同事务 effects 异步消费（evald + afterCommit runner）。"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any

from siming.collectors.edge_sync import sync_outbox_item_to_edge, withdraw_outbox_from_edge
from siming.config import load_policy
from siming.db import SimingDB, iso_now, now_local, parse_iso
from siming.router import (
    flush_morning_brief_if_due,
    flush_pickup_window_if_due,
    send_qq_message,
)
from siming.waits import evaluate_waits
from siming.worker import run_worker_for_wait


def process_new_events(
    db: SimingDB, coalesce_sec: int = 0
) -> dict[str, Any]:
    """扫描未处理 events：按 thread_key 聚合（支持 90s 防抖或立即冲刷），触发 event/intent waits。"""
    db.init_db()
    cutoff = iso_now(now_local() - timedelta(seconds=max(0, coalesce_sec)))
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT * FROM events
            WHERE processed_at IS NULL AND ingested_at <= ?
            ORDER BY occurred_at ASC
            LIMIT 200
            """,
            (cutoff,),
        ).fetchall()
        events = [dict(r) for r in rows]
    finally:
        conn.close()

    if not events:
        return {"processed_events": 0, "fired_waits": []}

    intent_texts = [
        f"{ev['summary']} {ev['payload_json']}"
        for ev in events
        if ev["source"] in ("qq_drop", "desktop_drop", "ios_drop")
    ]
    w_res = evaluate_waits(db, recent_events=events, intent_texts=intent_texts or None)

    ts = iso_now()
    with db.transaction() as conn:
        for ev in events:
            conn.execute(
                "UPDATE events SET processed_at = ? WHERE event_id = ?",
                (ts, ev["event_id"]),
            )
    return {"processed_events": len(events), "fired_waits": w_res["fired"]}


def _should_detach_worker(db: SimingDB, wait_id: str) -> bool:
    """长耗时 stage / run / resume 任务默认由独立 systemd-run 瞬态单元执行，防阻塞 tick 或被 45s/240s 超时强杀。"""
    if (
        os.environ.get("SIMING_INLINE_WORKER") == "1"
        or os.environ.get("SIMING_DRY_RUN_SEND") == "1"
        or os.environ.get("SIMING_TEST_OFFLINE") == "1"
    ):
        return False
    if not shutil.which("systemd-run"):
        return False
    siming_bin = Path.home() / ".local" / "bin" / "siming"
    if not siming_bin.is_file():
        return False
    conn = db.connect()
    try:
        row = conn.execute("SELECT action_mode FROM waits WHERE wait_id=?", (wait_id.strip().upper(),)).fetchone()
        mode = str(row["action_mode"]) if row else "remind"
    finally:
        conn.close()
    return mode in ("stage", "run", "resume")


def _spawn_detached_worker_unit(
    db: SimingDB, eff_id: str, unit_name: str, expected_gen: int = 0
) -> tuple[bool, str, str]:
    policy = load_policy(db.paths)
    timeout_sec = int(policy["guardrails"].get("worker_timeout_sec", 1800)) + 120
    siming_bin = str(Path.home() / ".local" / "bin" / "siming")
    cmd = [
        "systemd-run",
        "--user",
        "--collect",
        f"--unit={unit_name}",
        f"--property=RuntimeMaxSec={timeout_sec}",
        f"--setenv=TMPDIR={db.paths.tmp_root.parent}",
        f"--setenv=HTTP_PROXY={os.environ.get('HTTP_PROXY', 'http://127.0.0.1:7890')}",
        f"--setenv=HTTPS_PROXY={os.environ.get('HTTPS_PROXY', 'http://127.0.0.1:7890')}",
        f"--setenv=NO_PROXY={os.environ.get('NO_PROXY', 'localhost,127.0.0.1,::1')}",
        siming_bin,
        "run-effect-worker",
        eff_id,
    ]
    if expected_gen > 0:
        cmd.extend(["--expected-gen", str(expected_gen)])
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    if proc.returncode != 0:
        return False, "", (proc.stderr or proc.stdout).strip()[:240]
    return True, unit_name, ""


def run_single_effect_worker(
    db: SimingDB, effect_id: str, expected_gen: int | None = None
) -> dict[str, Any]:
    """供 systemd-run 瞬态单元调用：派发时绑定 expected_gen，按代次 CAS 认领并执行 launch_worker，随后冲刷通知。"""
    db.init_db()
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM effects WHERE effect_id=?", (effect_id,)).fetchone()
        if not row:
            return {"ok": False, "error": f"effect {effect_id} not found"}
        eff = dict(row)
        if eff["status"] != "RUNNING":
            return {"ok": True, "skipped": eff["status"]}
        cur_gen = int(eff.get("attempt_gen") or 0)
        if expected_gen is not None and int(expected_gen) > 0 and cur_gen != int(expected_gen):
            return {"ok": True, "skipped": f"stale_generation:{expected_gen}!={cur_gen}"}
        gen = cur_gen
        cur = conn.execute(
            "UPDATE effects SET worker_pid=?, started_at=COALESCE(started_at, ?) WHERE effect_id=? AND status='RUNNING' AND attempt_gen=?",
            (os.getpid(), iso_now(), effect_id, gen),
        )
        if cur.rowcount == 0:
            return {"ok": True, "skipped": "generation_changed"}

    payload = json.loads(eff["payload_json"] or "{}")
    was_interrupted = eff.get("last_error") == "interrupted_process_recovered" or int(eff["retry_count"]) > 0
    ok = False
    err = ""
    deferred_sec = 0
    try:
        w_res = run_worker_for_wait(
            db,
            wait_id=str(payload["wait_id"]),
            trigger_reason=str(payload.get("trigger_reason", "")),
            was_interrupted=was_interrupted,
        )
        if w_res.get("deferred"):
            deferred_sec = 600
        else:
            ok = True
    except Exception as exc:
        ok = False
        err = str(exc)

    fin_ts = iso_now()
    with db.transaction() as conn:
        if deferred_sec > 0:
            nxt = iso_now(now_local() + timedelta(seconds=deferred_sec))
            conn.execute(
                "UPDATE effects SET status='PENDING', worker_pid=NULL, unit_name='', next_run_at=?, last_error='paused_deferred' WHERE effect_id=? AND status='RUNNING' AND attempt_gen=?",
                (nxt, effect_id, gen),
            )
        elif ok:
            conn.execute(
                "UPDATE effects SET status='DONE', worker_pid=NULL, unit_name='', completed_at=? WHERE effect_id=? AND status='RUNNING' AND attempt_gen=?",
                (fin_ts, effect_id, gen),
            )
        else:
            retries = int(eff["retry_count"]) + 1
            if retries <= 2:
                nxt = iso_now(now_local() + timedelta(seconds=60 * retries))
                conn.execute(
                    "UPDATE effects SET status='PENDING', retry_count=?, worker_pid=NULL, unit_name='', next_run_at=?, last_error=? WHERE effect_id=? AND status='RUNNING' AND attempt_gen=?",
                    (retries, nxt, err[:300], effect_id, gen),
                )
            else:
                cur_f = conn.execute(
                    "UPDATE effects SET status='FAILED', retry_count=?, worker_pid=NULL, unit_name='', completed_at=?, last_error=? WHERE effect_id=? AND status='RUNNING' AND attempt_gen=?",
                    (retries, fin_ts, err[:300], effect_id, gen),
                )
                if cur_f.rowcount > 0:
                    wid = str(payload.get("wait_id") or effect_id)
                    conn.execute(
                        "UPDATE waits SET status='FAILED', result_summary=?, updated_at=? WHERE wait_id=? AND status='FIRED'",
                        (f"后台执行异常失败：{err[:200]}", fin_ts, wid),
                    )
                    db.enqueue_effect(
                        conn,
                        "send_qq",
                        {
                            "ref_id": wid,
                            "rule_id": "worker_failed",
                            "text": f"⚠️ [{wid}] 后台任务连续执行异常已停手：{err[:220]}",
                        },
                    )

    consume_pending_effects(db, include_workers=False)
    return {"ok": ok, "deferred_sec": deferred_sec, "error": err}


def consume_pending_effects(
    db: SimingDB, limit: int = 30, include_workers: bool = True
) -> dict[str, Any]:
    """消费事务提交后的 effects 队列（send_qq / launch_worker / edge_sync_outbox / edge_withdraw_outbox）。"""
    db.init_db()
    now = now_local()
    ts = iso_now(now)

    conn = db.connect()
    try:
        if include_workers:
            rows = conn.execute(
                """
                SELECT * FROM effects
                WHERE status = 'PENDING' AND next_run_at <= ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (ts, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM effects
                WHERE status = 'PENDING' AND kind != 'launch_worker' AND next_run_at <= ?
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (ts, limit),
            ).fetchall()
        pending = [dict(r) for r in rows]
    finally:
        conn.close()

    done_cnt = 0
    failed_cnt = 0
    detached_cnt = 0

    for eff in pending:
        eff_id = eff["effect_id"]
        kind = eff["kind"]
        payload = json.loads(eff["payload_json"] or "{}")
        was_interrupted = eff.get("last_error") == "interrupted_process_recovered" or int(eff["retry_count"]) > 0

        if kind == "launch_worker" and _should_detach_worker(db, str(payload.get("wait_id", ""))):
            next_gen = int(eff.get("attempt_gen") or 0) + 1
            safe_suffix = "".join(ch if ch.isalnum() else "-" for ch in eff_id.lower())
            unit_name = f"siming-worker-{safe_suffix}-g{next_gen}"
            # 同一事务先写 RUNNING + unit_name + attempt_gen，彻底消除拉起间隙被 watchdog 误判已死重排的竞态！
            with db.transaction() as conn:
                cur = conn.execute(
                    "UPDATE effects SET status='RUNNING', unit_name=?, attempt_gen=?, started_at=? WHERE effect_id=? AND status='PENDING'",
                    (unit_name, next_gen, iso_now(), eff_id),
                )
                if cur.rowcount == 0:
                    continue
            spawn_ok, _, spawn_err = _spawn_detached_worker_unit(db, eff_id, unit_name, expected_gen=next_gen)
            if spawn_ok:
                detached_cnt += 1
                continue
            # 严禁在生产环境回退到 tick 内联长跑（会被 240s systemd 超时强杀）；退避重试并在连续失败后告警
            retries = int(eff["retry_count"]) + 1
            with db.transaction() as conn:
                if retries <= 2:
                    nxt = iso_now(now_local() + timedelta(seconds=60 * retries))
                    conn.execute(
                        "UPDATE effects SET status='PENDING', retry_count=?, unit_name='', next_run_at=?, last_error=? WHERE effect_id=? AND attempt_gen=?",
                        (retries, nxt, f"detach_failed:{spawn_err[:160]}", eff_id, next_gen),
                    )
                else:
                    fin_ts = iso_now()
                    conn.execute(
                        "UPDATE effects SET status='FAILED', retry_count=?, unit_name='', completed_at=?, last_error=? WHERE effect_id=? AND attempt_gen=?",
                        (retries, fin_ts, f"detach_failed:{spawn_err[:160]}", eff_id, next_gen),
                    )
                    wid = str(payload.get("wait_id") or eff_id)
                    conn.execute(
                        "UPDATE waits SET status='FAILED', result_summary=?, updated_at=? WHERE wait_id=? AND status='FIRED'",
                        (f"systemd-run 拉起失败：{spawn_err[:160]}", fin_ts, wid),
                    )
                    db.enqueue_effect(
                        conn,
                        "send_qq",
                        {
                            "ref_id": wid,
                            "rule_id": "worker_failed",
                            "text": f"⚠️ [{wid}] 独立后台单元连续拉起失败已停手：{spawn_err[:180]}",
                        },
                    )
                    failed_cnt += 1
            continue

        next_gen = int(eff.get("attempt_gen") or 0) + 1
        with db.transaction() as conn:
            cur = conn.execute(
                "UPDATE effects SET status='RUNNING', worker_pid=?, attempt_gen=?, started_at=? WHERE effect_id=? AND status='PENDING'",
                (os.getpid(), next_gen, iso_now(), eff_id),
            )
            if cur.rowcount == 0:
                continue

        ok = False
        err = ""
        deferred_sec = 0
        try:
            if kind == "send_qq":
                res = send_qq_message(
                    db,
                    text=str(payload.get("text", "")),
                    ref_id=str(payload.get("ref_id", "")),
                    rule_id=str(payload.get("rule_id", "system")),
                    trial=bool(payload.get("trial", False)),
                    urgent=bool(payload.get("urgent", False)),
                    bypass_quiet=bool(payload.get("bypass_quiet", False)),
                    sub_meta=payload.get("sub_meta") if isinstance(payload.get("sub_meta"), dict) else None,
                )
                ok = bool(res.get("ok"))
                err = str(res.get("error", ""))
            elif kind == "launch_worker":
                w_res = run_worker_for_wait(
                    db,
                    wait_id=str(payload["wait_id"]),
                    trigger_reason=str(payload.get("trigger_reason", "")),
                    was_interrupted=was_interrupted,
                )
                if w_res.get("deferred"):
                    deferred_sec = 600
                else:
                    ok = True
            elif kind == "edge_sync_outbox":
                s_res = sync_outbox_item_to_edge(db, str(payload.get("outbox_id", "")))
                ok = bool(s_res.get("ok"))
                err = str(s_res.get("error", ""))
            elif kind == "edge_withdraw_outbox":
                w_res = withdraw_outbox_from_edge(db, str(payload.get("ref_id", "")))
                ok = bool(w_res.get("ok"))
                err = str(w_res.get("error", ""))
            else:
                ok = False
                err = f"unknown effect kind: {kind}"
        except Exception as exc:
            ok = False
            err = str(exc)

        fin_ts = iso_now()
        with db.transaction() as conn:
            if deferred_sec > 0:
                nxt = iso_now(now_local() + timedelta(seconds=deferred_sec))
                conn.execute(
                    "UPDATE effects SET status='PENDING', worker_pid=NULL, next_run_at=?, last_error='paused_deferred' WHERE effect_id=? AND status='RUNNING' AND attempt_gen=?",
                    (nxt, eff_id, next_gen),
                )
            elif ok:
                cur_done = conn.execute(
                    "UPDATE effects SET status='DONE', worker_pid=NULL, completed_at=? WHERE effect_id=? AND status='RUNNING' AND attempt_gen=?",
                    (fin_ts, eff_id, next_gen),
                )
                if cur_done.rowcount > 0:
                    done_cnt += 1
            else:
                retries = int(eff["retry_count"]) + 1
                if retries <= 3:
                    backoff = 60 * (2 ** (retries - 1))
                    nxt = iso_now(now_local() + timedelta(seconds=backoff))
                    conn.execute(
                        "UPDATE effects SET status='PENDING', retry_count=?, worker_pid=NULL, next_run_at=?, last_error=? WHERE effect_id=? AND status='RUNNING' AND attempt_gen=?",
                        (retries, nxt, err[:300], eff_id, next_gen),
                    )
                else:
                    cur_fail = conn.execute(
                        "UPDATE effects SET status='FAILED', retry_count=?, worker_pid=NULL, completed_at=?, last_error=? WHERE effect_id=? AND status='RUNNING' AND attempt_gen=?",
                        (retries, fin_ts, err[:300], eff_id, next_gen),
                    )
                    if cur_fail.rowcount > 0 and kind == "launch_worker":
                        wid = str(payload.get("wait_id") or eff_id)
                        conn.execute(
                            "UPDATE waits SET status='FAILED', result_summary=?, updated_at=? WHERE wait_id=? AND status='FIRED'",
                            (f"后台执行异常：{err[:200]}", fin_ts, wid),
                        )
                        db.enqueue_effect(
                            conn,
                            "send_qq",
                            {
                                "ref_id": wid,
                                "rule_id": "worker_failed",
                                "text": f"⚠️ [{wid}] 后台任务连续重试后失败停手：{err[:220]}",
                            },
                        )
                failed_cnt += 1

    return {"done": done_cnt, "failed": failed_cnt, "detached": detached_cnt}


def run_tick(db: SimingDB, include_collectors: bool = True) -> dict[str, Any]:
    """每 2 分钟主节拍：开机间隔检测 -> 本地采集器（独立异常隔离）-> 会话与长任务扫描 -> wait_source -> evald -> effects -> 晨札/取件窗。"""
    from siming.collectors.buy_track import collect_buy_track
    from siming.collectors.gh_prs import collect_watched_prs
    from siming.collectors.mail163 import collect_mail163
    from siming.collectors.memos_vault import collect_memos_vault
    from siming.cron_harvest import harvest_cron_outputs
    from siming.install_plan import ensure_arrival_inspection_wait
    from siming.price_watch import collect_price_watch
    from siming.session_scanner import scan_agent_sessions
    from siming.task_watch import poll_herdr_watches, reconcile_interrupted_task_runs

    db.init_db()
    policy = load_policy(db.paths)
    now = now_local()

    # P0 系统安全守卫前置：Tesla 18 分钟未发车自动关空调守卫必须在慢采集器之前运行，绝不被拖死
    p0_tesla_climate: dict[str, Any] = {}
    if include_collectors:
        try:
            from siming.tesla_link import evaluate_departure_auto_climate

            p0_tesla_climate = evaluate_departure_auto_climate(db, departed_now=False, trigger="tick_p0_guard")
        except Exception as exc:
            p0_tesla_climate = {"ok": False, "error": str(exc)[:180]}

    # 自动清理过期 PENDING 审批单 & 冲刷超过 3 分钟仍未恢复的 Uptime Kuma 真实异常
    expired_approvals = 0
    kuma_flushed: dict[str, Any] = {"flushed": [], "count": 0}
    try:
        from siming.approval import expire_stale_approvals

        expired_approvals = expire_stale_approvals(db, now=now)
    except Exception:
        expired_approvals = 0
    try:
        from siming.push import flush_debounced_kuma_alerts

        kuma_flushed = flush_debounced_kuma_alerts(db, now=now)
    except Exception:
        kuma_flushed = {"flushed": [], "count": 0}
    last_tick_str = db.get_meta("last_tick_at")
    reboot_gap_min = int(policy["guardrails"].get("reboot_suppress_gap_minutes", 30))
    was_reboot_gap = False
    if last_tick_str:
        gap = (now - parse_iso(last_tick_str)).total_seconds() / 60.0
        was_reboot_gap = gap > reboot_gap_min
    db.set_meta("last_tick_at", iso_now(now))

    collector_stats: dict[str, Any] = {}
    if include_collectors:
        from siming.modules import is_module_enabled as _is_mod_enabled

        for src_id, fn in (
            ("buy_track", lambda: collect_buy_track(db)),
            ("price_watch", lambda: collect_price_watch(db) if _is_mod_enabled(db, "price_watch") else {"ok": True, "skipped": "module_disabled"}),
            ("memos_vault", lambda: collect_memos_vault(db)),
            ("gh_prs", lambda: collect_watched_prs(db)),
            ("cron_harvest", lambda: harvest_cron_outputs(db)),
        ):
            try:
                collector_stats[src_id] = fn()
            except Exception as exc:
                db.mark_source_error(src_id, "ERROR", str(exc)[:240])
                collector_stats[src_id] = {"ok": False, "error": str(exc)[:240]}

        try:
            ensure_arrival_inspection_wait(db)
        except Exception:
            pass

        try:
            from siming.event_chain import seed_deco_and_obligation_chains

            seed_deco_and_obligation_chains(db)
        except Exception:
            pass

        try:
            from siming.collectors.douyin_orders import collect_douyin_orders

            collector_stats["douyin_orders"] = collect_douyin_orders(db)
        except Exception as exc:
            collector_stats["douyin_orders"] = {"ok": False, "error": str(exc)[:180]}

        try:
            from siming.tesla_link import poll_tesla_cn_fleet_if_configured

            collector_stats["tesla_fleet"] = poll_tesla_cn_fleet_if_configured(db)
        except Exception as exc:
            collector_stats["tesla_fleet"] = {"ok": False, "error": str(exc)[:180]}

        last_enr = db.get_meta("last_ledger_prior_enrich_at")
        if not last_enr or (now - parse_iso(last_enr)).total_seconds() >= 1800:
            try:
                from siming.ledger.enrich_priors import enrich_pending_ledger_with_priors_and_orders

                collector_stats["ledger_enrich"] = enrich_pending_ledger_with_priors_and_orders(db, days=7)
                db.set_meta("last_ledger_prior_enrich_at", iso_now(now))
            except Exception as exc:
                collector_stats["ledger_enrich"] = {"ok": False, "error": str(exc)[:180]}

        last_mail = db.get_meta("last_mail163_poll_at")
        if not last_mail or (now - parse_iso(last_mail)).total_seconds() >= 900:
            try:
                collector_stats["mail163"] = collect_mail163(db)
                db.set_meta("last_mail163_poll_at", iso_now(now))
            except Exception as exc:
                db.mark_source_error("mail163", "ERROR", str(exc)[:240])
                collector_stats["mail163"] = {"ok": False, "error": str(exc)[:240]}
        try:
            from siming.self_heal import evaluate_nous_billing_sentinel

            collector_stats["nous_sentinel"] = evaluate_nous_billing_sentinel(db, notify=True)
        except Exception as exc:
            collector_stats["nous_sentinel"] = {"ok": False, "error": str(exc)[:240]}

        try:
            from siming.gobang_checkin import poll_gobang_checkins

            collector_stats["gobang_checkin"] = poll_gobang_checkins(db, now=now, notify=True)
        except Exception as exc:
            db.mark_source_error("gobang_checkin", "ERROR", str(exc)[:240])
            collector_stats["gobang_checkin"] = {"ok": False, "error": str(exc)[:240]}
        db.mark_source_ok("cn_calendar", detail="local rules active")

    try:
        scan_res = scan_agent_sessions(db)
    except Exception as exc:
        db.mark_source_error("agent_sessions", "ERROR", str(exc)[:240])
        scan_res = {"ok": False, "error": str(exc)[:240]}

    try:
        from siming.dev_twin import scan_stuck_herdr_agents

        task_watch_res = {
            "interrupted": reconcile_interrupted_task_runs(db),
            "herdr_watches": poll_herdr_watches(db),
            "stuck_agents": scan_stuck_herdr_agents(db),
        }
    except Exception as exc:
        task_watch_res = {"error": str(exc)[:240]}

    trip_prep_res: dict[str, Any] = {}
    if include_collectors:
        try:
            from siming.outing_chain import evaluate_upcoming_trip_prep

            trip_prep_res = evaluate_upcoming_trip_prep(db)
        except Exception as exc:
            trip_prep_res = {"ok": False, "error": str(exc)[:240]}

    wait_res = evaluate_waits(db)
    ev_res = process_new_events(db, coalesce_sec=0)
    eff_res = consume_pending_effects(db, include_workers=True)
    # 二次冲刷刚刚入队的 send_qq 通知，不重复拉起新 worker
    eff_res_2 = consume_pending_effects(db, include_workers=False)

    brief_res = flush_morning_brief_if_due(db, was_reboot_gap=was_reboot_gap)
    pickup_res = flush_pickup_window_if_due(db)
    care_res: dict[str, Any] = {}
    spatial_res: dict[str, Any] = {}
    if include_collectors:
        try:
            from siming.daily_care import run_daily_care

            care_res = run_daily_care(db)
        except Exception as exc:
            care_res = {"ok": False, "error": str(exc)[:240]}

        try:
            from siming.spatial_life import check_and_fire_offwork_pickup, evaluate_overtime_meal_ab
            from siming.tesla_link import evaluate_departure_auto_climate

            spatial_res = {
                "trip_prep": trip_prep_res,
                "overtime_meal": evaluate_overtime_meal_ab(db),
                "offwork_pickup": check_and_fire_offwork_pickup(db),
                "tesla_auto_climate": evaluate_departure_auto_climate(db, departed_now=False, trigger="tick"),
            }
            consume_pending_effects(db, include_workers=False)
        except Exception as exc:
            spatial_res = {"ok": False, "error": str(exc)[:240]}

        # 司命 v5（Wave 1–4）节拍守卫：每 30m / 6h 节流巡检，接入 D-35(D1-A) 三维能力生命周期 is_module_tick_active（未到位硬件自动停空转）
        v5_guards: dict[str, Any] = {}
        try:
            from siming.modules import is_module_tick_active as is_module_enabled

            last_v5_30m = db.get_meta("last_v5_30m_guard_at")
            if not last_v5_30m or (now - parse_iso(last_v5_30m)).total_seconds() >= 1800:
                if is_module_enabled(db, "compute_assets"):
                    from siming.compute_assets import evaluate_compute_guardrails
                    try:
                        from siming.compute_monitor import sync_live_compute_assets

                        sync_live_compute_assets(db)
                    except Exception:
                        pass

                    v5_guards["compute"] = evaluate_compute_guardrails(db, now=now, notify=True)
                if is_module_enabled(db, "parcel_sentinel"):
                    from siming.parcel_sentinel import run_parcel_sentinel

                    v5_guards["parcel_sentinel"] = run_parcel_sentinel(db, now=now, notify=True)
                if is_module_enabled(db, "meal_combo_guard"):
                    from siming.meal_guard import evaluate_meal_regularity

                    v5_guards["meal_guard"] = evaluate_meal_regularity(db, now=now, notify=True)
                if is_module_enabled(db, "replenish_cart"):
                    try:
                        from siming.replenish_cart import evaluate_replenish_due

                        v5_guards["replenish_due"] = evaluate_replenish_due(db, now=now, notify=True)
                    except Exception as exc:
                        v5_guards["replenish_due"] = {"ok": False, "error": str(exc)[:180]}
                db.set_meta("last_v5_30m_guard_at", iso_now(now))

            last_v5_6h = db.get_meta("last_v5_6h_guard_at")
            if not last_v5_6h or (now - parse_iso(last_v5_6h)).total_seconds() >= 21600:
                if is_module_enabled(db, "sub_assassin"):
                    from siming.sub_hunter import emit_subscription_stoploss_alerts, scan_subscriptions

                    scan_subscriptions(db, now=now)
                    v5_guards["sub_assassin"] = emit_subscription_stoploss_alerts(db, now=now)
                if is_module_enabled(db, "vehicle_cfo"):
                    from siming.vehicle_cfo import evaluate_lfp_calibration_and_insurance

                    v5_guards["vehicle_cfo"] = evaluate_lfp_calibration_and_insurance(db, now=now, notify=True)
                if is_module_enabled(db, "home_passport"):
                    from siming.home_passport import check_due_utilities

                    v5_guards["home_utilities"] = check_due_utilities(db, now=now, notify=True)
                if is_module_enabled(db, "overnight_lab"):
                    from siming.overnight_lab import patrol_overnight_handoffs

                    v5_guards["overnight_lab"] = patrol_overnight_handoffs(db, now=now)

                # 知识与财务联动管线（各自独立异常隔离 + 受模块开关控制）
                if is_module_enabled(db, "hindsight_wiki"):
                    try:
                        from siming.hindsight_wiki import distill_hindsight_wiki

                        v5_guards["hindsight_wiki"] = distill_hindsight_wiki(
                            db,
                            now=now,
                            include_db_sources=True,
                            include_local_sessions=(os.environ.get("SIMING_TEST_OFFLINE") != "1"),
                        )
                    except Exception as exc:
                        v5_guards["hindsight_wiki"] = {"ok": False, "error": str(exc)[:180]}

                if is_module_enabled(db, "sure_finance_os"):
                    try:
                        from siming.sure_bridge import sync_ledger_to_sure

                        v5_guards["sure_sync"] = sync_ledger_to_sure(db, limit=200)
                    except Exception as exc:
                        v5_guards["sure_sync"] = {"ok": False, "error": str(exc)[:180]}

                if is_module_enabled(db, "context_graph"):
                    try:
                        from siming.context_graph import index_context_graph

                        v5_guards["context_graph"] = index_context_graph(db, max_files=80)
                    except Exception as exc:
                        v5_guards["context_graph"] = {"ok": False, "error": str(exc)[:180]}

                db.set_meta("last_v5_6h_guard_at", iso_now(now))
            if v5_guards:
                consume_pending_effects(db, include_workers=False)
        except Exception as exc:
            v5_guards = {"ok": False, "error": str(exc)[:240]}
        spatial_res["v5_guards"] = v5_guards
    db.set_meta("last_tick_ok_at", iso_now())

    return {
        "ok": True,
        "was_reboot_gap": was_reboot_gap,
        "expired_approvals": expired_approvals,
        "kuma_flushed": kuma_flushed,
        "collectors": collector_stats,
        "session_scan": scan_res,
        "task_watch": task_watch_res,
        "waits": wait_res,
        "events": ev_res,
        "effects": {
            "done": eff_res["done"] + eff_res_2["done"],
            "failed": eff_res["failed"] + eff_res_2["failed"],
            "detached": eff_res.get("detached", 0),
        },
        "morning_brief": brief_res,
        "pickup_window": pickup_res,
        "daily_care": care_res,
        "spatial_life": spatial_res,
    }
