"""buy-track 家电主材订单状态与价保变化只读采集器（物流事实与人工预约/完结状态严格分离，严查假签收与假正常登录态）。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from siming.config import load_sources_config
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local, parse_iso
from siming.enrich import enrich_appliance_arrival
from siming.install_plan import is_order_cancelled, is_order_truly_signed

BUY_TRACK_KICK_COOLDOWN_SEC = 1800


def kick_buy_track_sync_if_needed(
    db: SimingDB,
    *,
    user_recovered: bool = False,
    min_interval_sec: int = BUY_TRACK_KICK_COOLDOWN_SEC,
) -> bool:
    """非阻塞触发本机 buy-track-sync 刷新 auto.json（30 分钟节流，防重复拉起）。"""
    if os.environ.get("SIMING_TEST_OFFLINE") == "1" or os.environ.get("SIMING_DRY_RUN_SEND") == "1":
        return False
    cur = now_local()
    last_k = db.get_meta("buy_track_kick_at")
    if last_k and min_interval_sec > 0:
        try:
            if 0 <= (cur - parse_iso(last_k)).total_seconds() < min_interval_sec:
                return False
        except Exception:
            pass

    if user_recovered and shutil.which("systemd-run"):
        node_bin = Path.home() / ".local/share/mise/installs/node/26.8.2/bin/node"
        sync_js = Path.home() / "buy/track/sync.mjs"
        if node_bin.is_file() and sync_js.is_file():
            unit = f"siming-buy-track-recover-{int(cur.timestamp())}"
            proc = subprocess.run(
                [
                    "systemd-run",
                    "--user",
                    "--collect",
                    "--no-block",
                    f"--unit={unit}",
                    f"--working-directory={sync_js.parent}",
                    str(node_bin),
                    str(sync_js),
                    "--user-recovered",
                ],
                capture_output=True,
                text=True,
                timeout=8,
            )
            if proc.returncode == 0:
                db.set_meta("buy_track_kick_at", iso_now(cur))
                return True

    if shutil.which("systemctl"):
        proc = subprocess.run(
            ["systemctl", "--user", "start", "--no-block", "buy-track-sync.service"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if proc.returncode == 0:
            db.set_meta("buy_track_kick_at", iso_now(cur))
            return True
    return False


def _jd_official_probe_is_fresh_ok(db: SimingDB, max_age_sec: int = 6 * 3600) -> bool:
    raw = db.get_meta("price_watch_official_snapshot")
    if not raw:
        return False
    try:
        snap = json.loads(raw)
        if not isinstance(snap, dict) or not snap.get("ok") or snap.get("auth_expired"):
            return False
        checked_at = str(snap.get("checked_at") or "")
        if not checked_at:
            return False
        return (now_local() - parse_iso(checked_at)).total_seconds() <= max_age_sec
    except Exception:
        return False


def _load_items_meta_map(auto_path: Path) -> tuple[dict[str, dict[str, Any]], str]:
    """只读读取相邻 items.json，获取集中安装日（2026-12-10）及各单房间/安装要点。"""
    for cand in (auto_path.parent.parent / "items.json", auto_path.parent / "items.json"):
        if cand.is_file():
            try:
                doc = json.loads(cand.read_text(encoding="utf-8"))
                pref_date = str((doc.get("preferences") or {}).get("install_date") or "").strip()
                by_id = {
                    str(it["id"]): it
                    for it in (doc.get("items") or [])
                    if isinstance(it, dict) and it.get("id")
                }
                return by_id, pref_date
            except Exception:
                continue
    return {}, ""


def collect_buy_track(
    db: SimingDB,
    auto_json_path: Optional[Path] = None,
    relogin_runner: Optional[Callable[[SimingDB, str], dict[str, Any]]] = None,
    sync_kicker: Optional[Callable[[SimingDB, bool], bool]] = None,
) -> dict[str, Any]:
    db.init_db()
    src_cfg = load_sources_config(db.paths).get("buy_track") or {}
    path = auto_json_path or Path(str(src_cfg.get("path") or (Path.home() / "buy" / "track" / "data" / "auto.json"))).expanduser()

    if not path.is_file():
        db.mark_source_error("buy_track", "ERROR", f"missing {path}")
        return {"ok": False, "error": f"missing {path}"}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        db.mark_source_error("buy_track", "ERROR", f"invalid json: {exc}")
        return {"ok": False, "error": str(exc)}

    # 1. 检查上游京东/淘宝抓取登录态，拒绝把掉登录当成“0 条变化”
    upstream = data.get("sources") or {}
    expired_platforms: list[str] = []
    jd_auth_expired = False
    jd_non_ok = False
    for plat in ("jd", "tb"):
        p_info = upstream.get(plat) or {}
        last_status = str((p_info.get("last") or {}).get("status") or "")
        why = str(p_info.get("why") or "")
        why_is_auth = any(k in why.lower() for k in ("login", "auth", "cookie", "未登录", "重新登录", "过期")) or any(
            k in last_status.lower() for k in ("login", "auth", "expired")
        )
        if last_status and last_status != "ok":
            expired_platforms.append(f"{plat}:{last_status}({why})")
            if plat == "jd":
                jd_non_ok = True
                if why_is_auth:
                    jd_auth_expired = True
        elif why_is_auth:
            expired_platforms.append(f"{plat}:{why}")
            if plat == "jd":
                jd_non_ok = True
                jd_auth_expired = True

    if expired_platforms:
        detail = "上游登录态异常: " + ", ".join(expired_platforms)
        kicker = sync_kicker or (lambda d, rec: kick_buy_track_sync_if_needed(d, user_recovered=rec))
        relogin_res: dict[str, Any] = {}
        kicked_refresh = False

        # 若官方价保页刚确认京东已在线（只是 auto.json 还是重登前的旧快照），自动触发 buy-track-sync 立即刷新！
        if jd_non_ok and _jd_official_probe_is_fresh_ok(db):
            kicked_refresh = bool(kicker(db, True))
        elif jd_auth_expired:
            # 京东真正掉登录：自动尝试用本人授权手机号 + 81 瞬时短信验证码续登（绝不傻等扫码！）
            from siming.otp_vault import attempt_auto_platform_relogin

            relogin_res = attempt_auto_platform_relogin(db, "jd", relogin_runner=relogin_runner)
            if relogin_res.get("ok"):
                kicked_refresh = bool(kicker(db, True))
                today_ok = now_local().date().isoformat()
                if db.get_meta("jd_auto_relogin_ok_day") != today_ok:
                    with db.transaction() as conn:
                        db.enqueue_effect(
                            conn,
                            "send_qq",
                            {
                                "ref_id": f"jd_auto_relogin_ok:{today_ok}",
                                "rule_id": "buy_track_auth",
                                "text": "✅ 检测到京东订单同步登录过期，司命已自动用授权手机号接短信验证码续登成功，正在后台刷新订单列表！",
                            },
                        )
                        db.set_meta("jd_auto_relogin_ok_day", today_ok, conn=conn)

        auth_state = "AUTH_EXPIRED" if (jd_auth_expired or any(":auth(" in x or "登录" in x for x in expired_platforms)) else "ERROR"
        db.mark_source_error("buy_track", auth_state, detail)
        # 每日最多推 1 次登录过期告警（自动续登已成功或正在后台刷新时不骚扰用户）
        today_s = now_local().date().isoformat()
        if (not relogin_res.get("ok")) and (not kicked_refresh) and db.get_meta("buy_track_auth_alert_day") != today_s:
            if relogin_res.get("gated"):
                hint = "• 自动续登进展：司命已自动填入授权手机号请求短信，但京东弹出滑块验证（已保留 Chromium 后台标签页）。请滑一下后在 QQ 回 `填验证码 京东` 即可恢复。"
            elif relogin_res.get("attempted"):
                hint = f"• 自动续登进展：已尝试用授权手机号短信登录但未完成（{str(relogin_res.get('reply') or '超时/失败')[:120]}），可在 QQ 回 `验证码登录 京东` 重试。"
            else:
                hint = "• 修复：在 QQ 发 `验证码登录 京东` 自动短信续登，或检查 `buy-track-sync` 日志。"
            msg = (
                f"🔐 京东/淘宝订单同步异常（{detail}）\n"
                "• 影响：订单签收/送装跟踪暂未更新。\n"
                + hint
            )
            with db.transaction() as conn:
                db.enqueue_effect(
                    conn,
                    "send_qq",
                    {
                        "ref_id": f"buy_track_auth:{today_s}",
                        "rule_id": "buy_track_auth",
                        "text": msg,
                    },
                )
                db.set_meta("buy_track_auth_alert_day", today_s, conn=conn)
        return {
            "ok": False,
            "auth_expired": auth_state == "AUTH_EXPIRED",
            "detail": detail,
            "auto_relogin": relogin_res,
            "kicked_refresh": kicked_refresh,
        }

    # 2. 校验快照自身生成时间（generatedAt），防止上游定时抓取静默停跑仍显假绿
    gen_raw = str(data.get("generatedAt") or "")
    snap_ok_iso: Optional[str] = None
    if gen_raw:
        try:
            gen_dt = datetime.fromisoformat(gen_raw.replace("Z", "+00:00")).astimezone(SHANGHAI_TZ)
            snap_ok_iso = gen_dt.isoformat(timespec="seconds")
            age_sec = (now_local() - gen_dt).total_seconds()
            max_sil = int(src_cfg.get("max_silence_sec", 86400))
            if auto_json_path is None and age_sec > max(max_sil, 129600):
                db.mark_source_error("buy_track", "ERROR", f"auto.json 快照已陈旧 ({int(age_sec // 3600)}h 未刷新)")
                return {"ok": False, "stale_snapshot": True, "age_hours": round(age_sec / 3600.0, 1)}
        except Exception:
            snap_ok_iso = None

    conn = db.connect()
    try:
        src_row = conn.execute("SELECT cursor FROM sources WHERE id='buy_track'").fetchone()
        prev_cursor = str(src_row["cursor"] or "") if src_row else ""
    finally:
        conn.close()

    prev_map: dict[str, dict[str, Any]] = {}
    is_bootstrap = not prev_cursor
    if prev_cursor:
        try:
            prev_map = json.loads(prev_cursor)
        except Exception:
            is_bootstrap = True

    items_meta_map, pref_install_date = _load_items_meta_map(path)
    new_map: dict[str, dict[str, Any]] = {}
    changed_items: list[str] = []

    for item in data.get("items") or []:
        iid = str(item.get("id") or "")
        if not iid:
            continue
        meta_item = items_meta_map.get(iid) or {}
        name = str(item.get("name") or meta_item.get("name") or iid)
        oi = item.get("orderInfo") or {}
        stage = str(oi.get("stage") or "")
        raw_state = str(oi.get("raw") or "")
        progress = str(oi.get("progress") or "")
        eta_date = str((oi.get("eta") or {}).get("date") or "")
        pr = item.get("price") or {}
        price_now = pr.get("now")
        price_fact = pr.get("fact")
        install_mode = str(item.get("install") or meta_item.get("install") or "none")
        install_note = str(item.get("installNote") or meta_item.get("installNote") or "")
        install_date = str(item.get("installDate") or meta_item.get("installDate") or pref_install_date or "")
        room = str(item.get("room") or meta_item.get("room") or "")

        snap = {
            "stage": stage,
            "raw": raw_state,
            "eta": eta_date,
            "progress_hash": hashlib.sha1(progress.encode("utf-8")).hexdigest()[:12],
            "price_now": price_now,
        }
        new_map[iid] = snap

        if is_bootstrap:
            continue

        is_new_item = iid not in prev_map
        old = prev_map.get(iid) or {}
        state_changed = is_new_item or (
            old.get("stage") != stage
            or old.get("raw") != raw_state
            or old.get("eta") != eta_date
        )
        cancelled = is_order_cancelled(stage, raw_state, progress)
        price_dropped = (
            (not is_new_item)
            and (not cancelled)
            and isinstance(price_now, (int, float))
            and isinstance(price_fact, (int, float))
            and price_now < price_fact - 1.0
            and old.get("price_now") != price_now
        )

        if not state_changed and not price_dropped:
            continue

        # 已取消/交易关闭订单（如退单林内）：静默关闭已有待办，绝不发到货/安装推送！
        if cancelled:
            ts_c = iso_now()
            with db.transaction() as conn:
                sub_c = conn.execute("SELECT target_id FROM subscriptions WHERE sub_key = ?", (f"buy:{iid}",)).fetchone()
                if sub_c:
                    cid_c = str(sub_c["target_id"])
                    conn.execute(
                        "UPDATE cases SET status='取消', closed_at=?, next_step='订单已取消/交易关闭', state_version=state_version+1, updated_at=? WHERE case_id=? AND status='待处理'",
                        (ts_c, ts_c, cid_c),
                    )
                    conn.execute("UPDATE subscriptions SET state='CANCELLED', ended_at=? WHERE target_id=?", (ts_c, cid_c))
            continue

        changed_items.append(iid)
        ts = iso_now()
        signed = is_order_truly_signed(stage, raw_state, progress)
        needs_install = install_mode not in ("none", "self", "")

        enrich_block = ""
        if signed and needs_install:
            enrich_block = enrich_appliance_arrival(
                name=name,
                room=room,
                install_note=install_note,
                platform=str(item.get("platform") or meta_item.get("platform") or ""),
                svc_orders=list(item.get("svc") or meta_item.get("svc") or []),
                db=db,
                item_id=iid,
                install_date=install_date,
            )

        with db.transaction() as conn:
            fp = f"buy:{iid}:{snap['stage']}:{snap['raw']}:{snap['eta']}:{price_now}"
            ev_id = db.append_event(
                conn,
                source="buy_track",
                thread_key=f"order:{iid}",
                summary=f"订单变动 [{name}]：{raw_state or stage} {progress[:50]}",
                payload={"id": iid, "name": name, "snap": snap, "signed": signed},
                fingerprint=fp,
            )
            if ev_id is None:
                continue

            sub = conn.execute(
                "SELECT target_id FROM subscriptions WHERE sub_key = ?",
                (f"buy:{iid}",),
            ).fetchone()

            if sub:
                case_id = str(sub["target_id"])
            else:
                case_id = db.next_seq_id(conn, "S")
                conn.execute(
                    """
                    INSERT INTO subscriptions (sub_key, target_kind, target_id, state, created_at)
                    VALUES (?, 'case', ?, 'ACTIVE', ?)
                    """,
                    (f"buy:{iid}", case_id, ts),
                )

            if signed and needs_install:
                inst_hint = f"（计划 {install_date}）" if install_date else ""
                logistics_step = f"已签收，待约上门安装与开孔核验{inst_hint}"
                done_rule = "安装验收完成"
                new_status = "待处理"
            elif signed and not needs_install:
                logistics_step = "已签收完结（无需上门安装）"
                done_rule = "签收即完结"
                new_status = "完成"
            else:
                logistics_step = f"物流状态：{raw_state or stage}" + (f"（预计 {eta_date}）" if eta_date else "")
                done_rule = "收货并验收" if not needs_install else "收货并完成上门安装验收"
                new_status = "待处理"

            existing_case = conn.execute(
                "SELECT case_id, status, next_step, context_json FROM cases WHERE case_id=?",
                (case_id,),
            ).fetchone()
            user_closed_or_cancelled = False
            was_already_signed = False
            if existing_case:
                cur_status = str(existing_case["status"])
                cur_step = str(existing_case["next_step"] or "")
                ctx = json.loads(existing_case["context_json"] or "{}")
                was_already_signed = bool(ctx.get("signed"))
                ctx["logistics_state"] = logistics_step
                ctx["signed"] = signed
                # 铁律：严禁物流轮询覆盖用户人工已标记的「完成 / 取消」状态，也不冲掉人工已记的预约时间备注！
                if cur_status in ("完成", "取消"):
                    user_closed_or_cancelled = True
                    conn.execute(
                        "UPDATE cases SET context_json=?, updated_at=? WHERE case_id=?",
                        (json.dumps(ctx, ensure_ascii=False), ts, case_id),
                    )
                else:
                    human_base = re.sub(r"（最新物流：.*$", "", cur_step).strip()
                    has_human_booking_note = any(k in human_base for k in ("约了", "已约", "预约", "改期", "备注"))
                    merged_step = (
                        f"{human_base}（最新物流：{logistics_step}）"[:280]
                        if has_human_booking_note
                        else logistics_step
                    )
                    conn.execute(
                        """
                        UPDATE cases
                        SET next_step=?, status=?, context_json=?, state_version=state_version+1, updated_at=?,
                            closed_at=CASE WHEN ?='完成' THEN ? ELSE NULL END
                        WHERE case_id=?
                        """,
                        (merged_step, new_status, json.dumps(ctx, ensure_ascii=False), ts, new_status, ts, case_id),
                    )
            else:
                conn.execute(
                    """
                    INSERT INTO cases (
                        case_id, title, next_step, due_at, done_rule, status, strength,
                        category, rule_id, evidence_ids, context_json, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'SOFT', 'appliance', 'buy_track_watch', ?, ?, ?, ?)
                    """,
                    (
                        case_id,
                        name,
                        logistics_step,
                        install_date or eta_date or None,
                        done_rule,
                        new_status,
                        json.dumps([ev_id]),
                        json.dumps({"logistics_state": logistics_step, "signed": signed}, ensure_ascii=False),
                        ts,
                        ts,
                    ),
                )

            if user_closed_or_cancelled and not price_dropped:
                continue
            # 同件家电已签收推过一次后（如 等待收货(已送达) -> 完成），绝不再重复推送第二遍签收提醒！
            if signed and was_already_signed and not price_dropped:
                continue

            if price_dropped:
                diff = float(price_fact) - float(price_now)
                msg = (
                    f"[{case_id}] 💰 价保提醒：{name}\n"
                    f"实付基准 ¥{price_fact:.2f} -> 当前页面价 ¥{price_now:.2f}（差价约 ¥{diff:.2f}）\n"
                    "可在手机京东/淘宝点一次官方价保（A3 动作不代点）。"
                )
            elif signed and needs_install:
                msg = f"[{case_id}] 📦 大家电已签收，待约上门安装：{name}\n{enrich_block}"
            else:
                msg = f"[{case_id}] 订单进展更新：{name}\n{logistics_step}\n{progress[:80]}"

            db.enqueue_effect(
                conn,
                "send_qq",
                {"ref_id": case_id, "rule_id": "buy_track_watch", "trial": True, "text": msg},
            )

    db.mark_source_ok(
        "buy_track",
        cursor=json.dumps(new_map, ensure_ascii=False),
        detail=f"tracked={len(new_map)}, changed={len(changed_items)}",
        ok_at=snap_ok_iso,
    )
    return {"ok": True, "bootstrapped": is_bootstrap, "changed": changed_items}
