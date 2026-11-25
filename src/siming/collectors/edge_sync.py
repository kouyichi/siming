"""与公网 VPS 边缘节点同步：定期 SSH 拉取收信队列游标、推送本机心跳、同步与撤回预排发件箱。"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import date
from typing import Any, Callable, Optional

from siming.cn_calendar import hospital_114_alert_window, rail_12306_alert_window
from siming.collectors.pickup_codes import try_ingest_pickup_code
from siming.config import load_policy
from siming.crypto import contains_otp, store_encrypted_raw
from siming.db import SimingDB, iso_now, now_local, parse_iso
from siming.ledger.bill_ocr_parser import try_ingest_bill_ocr
from siming.ledger.sms_parser import extract_sms_timestamp_and_body, try_ingest_bank_sms

_BARE_SENDER_ONLY_RE = re.compile(r"^[\d+\-\s()]{5,24}$")


def _ssh_edge_call(
    db: SimingDB, sub_args: list[str], stdin_text: Optional[str] = None
) -> dict[str, Any]:
    if os.environ.get("SIMING_DISABLE_SSH_EDGE") == "1":
        return {"ok": True, "disabled": True, "items": []}

    policy = load_policy(db.paths)
    edge_cfg = policy["edge"]
    host = str(edge_cfg["ssh_host"])
    remote_cli = str(edge_cfg["remote_cli"])

    cmd = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, f"{remote_cli} {' '.join(sub_args)}"]
    proc = subprocess.run(
        cmd,
        input=stdin_text,
        capture_output=True,
        text=True,
        timeout=20,
    )
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout or "").strip()[:240]}
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except Exception as exc:
        return {"ok": False, "error": f"invalid edge json: {exc}"}


def _ingest_travel_or_hospital_sms(db: SimingDB, text: str, raw_ref: str) -> bool:
    """识别真正的 12306 候补/购票通知或北京 114/医院挂号确认并幂等建 HARD 单（严防普通含数字或营销短信误触）。"""
    m_date = re.search(r"(20\d{2})[-年](\d{1,2})[-月](\d{1,2})", text)
    if not m_date:
        return False
    try:
        target_d = date(int(m_date.group(1)), int(m_date.group(2)), int(m_date.group(3)))
    except ValueError:
        return False

    is_12306 = ("【铁路12306】" in text or "12306" in text) and bool(
        re.search(r"(车次|检票|候补|发车|开检|乘车|购票|电子客票|起售)", text)
    )
    if is_12306:
        m_st = re.search(r"(北京(?:西|南|北|朝阳|丰台)?|清河)站?", text)
        station = m_st.group(1) if m_st else "北京南"
        win = rail_12306_alert_window(target_d, station, paths=db.paths)
        fp = f"rail_12306:{target_d.isoformat()}:{win['station']}"
        ts = iso_now()
        with db.transaction() as conn:
            ev_id = db.append_event(
                conn,
                source="ios_sms",
                thread_key=fp,
                summary=f"12306 出行提醒：{target_d.isoformat()} {win['station']}",
                raw_ref=raw_ref,
                fingerprint=fp,
            )
            if ev_id is None:
                return True
            cid = db.next_seq_id(conn, "S")
            conn.execute(
                """
                INSERT INTO cases (
                    case_id, title, next_step, due_at, done_rule, status, strength,
                    category, rule_id, evidence_ids, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '已出票或取消出行', '待处理', 'HARD', 'travel', 'rail_12306', ?, ?, ?)
                """,
                (
                    cid,
                    f"12306 出行 {target_d.isoformat()}（{win['station']}）",
                    f"起售日 {win['sale_date']} {win['sale_time']}（提前 15m 预警）",
                    win.get("alert_at") or target_d.isoformat(),
                    json.dumps([ev_id]),
                    ts,
                    ts,
                ),
            )
        return True

    m_hosp = re.search(
        r"(常用医院|协和医院|北医三院|北京大学第三医院|北大人民医院|北京大学人民医院|天坛医院|友谊医院|宣武医院|同仁医院)",
        text,
    )
    is_hosp = (bool(m_hosp) or "【北京114】" in text or "114预约挂号" in text) and bool(
        re.search(r"(挂号|预约成功|已成功预约|就诊|门诊|取号|候诊|放号)", text)
    )
    if is_hosp:
        raw_hosp = m_hosp.group(1) if m_hosp else "北京114医院"
        hosp = {"北京大学第三医院": "北医三院", "北京大学人民医院": "北大人民医院"}.get(raw_hosp, raw_hosp)
        already_booked = bool(re.search(r"(已成功预约|预约成功)", text))
        win = hospital_114_alert_window(target_d, hosp, paths=db.paths)
        fp = f"hospital_114:{target_d.isoformat()}:{hosp}"
        ts = iso_now()
        with db.transaction() as conn:
            ev_id = db.append_event(
                conn,
                source="ios_sms",
                thread_key=fp,
                summary=f"医院就诊/放号提醒：{hosp} {target_d.isoformat()}",
                raw_ref=raw_ref,
                fingerprint=fp,
            )
            if ev_id is None:
                return True
            if already_booked and "内分泌" in text:
                conn.execute(
                    "UPDATE cases SET next_step=?, updated_at=? WHERE category='med_refill' AND status='待处理'",
                    (f"已预约 {target_d.isoformat()} {hosp}内分泌门诊，开完药回「开药了 N支」", ts),
                )
            cid = db.next_seq_id(conn, "S")
            title_h = (
                f"医院就诊提醒：{hosp}（就诊日 {target_d.isoformat()}）"
                if already_booked
                else f"医院放号提醒：{hosp}（就诊日 {target_d.isoformat()}）"
            )
            step_h = (
                f"按预约时段到院缴费取号（{target_d.isoformat()}）"
                if already_booked
                else f"放号时刻 {win['release_date']} {win['release_time']}"
            )
            due_h = target_d.isoformat() if already_booked else (win.get("alert_at") or target_d.isoformat())
            conn.execute(
                """
                INSERT INTO cases (
                    case_id, title, next_step, due_at, done_rule, status, strength,
                    category, rule_id, evidence_ids, created_at, updated_at
                ) VALUES (?, ?, ?, ?, '完成挂号或就诊', '待处理', 'HARD', 'medical', 'hospital_114', ?, ?, ?)
                """,
                (
                    cid,
                    title_h,
                    step_h,
                    due_h,
                    json.dumps([ev_id]),
                    ts,
                    ts,
                ),
            )
        return True
    return False


def _process_single_edge_record(
    db: SimingDB, rec: dict[str, Any], today: date, stats: dict[str, int]
) -> None:
    import hashlib
    from siming.remote import drop_item

    src = str(rec.get("source") or "ios_drop")
    text = str(rec.get("text") or "").strip()
    is_hb = bool(rec.get("heartbeat"))

    if src == "ios_hb" or (is_hb and not text):
        hb_ts = str(rec.get("received_at") or "").strip() or None
        db.mark_source_ok("ios_hb", detail=f"last edge id={rec.get('id')}", ok_at=hb_ts)
        stats["heartbeat"] += 1
        return

    if src == "remote_run_receipt":
        from siming.task_watch import ingest_remote_run_receipt

        r_res = ingest_remote_run_receipt(db, text)
        if r_res.get("ok") and not r_res.get("duplicate"):
            stats["remote_run"] += 1
        return

    if src == "ios_loc":
        from siming.context_loc import ingest_ios_location
        from siming.curiosity import detect_unknown_dwell_and_ask
        from siming.router import flush_morning_brief_if_due
        from siming.spatial_life import check_and_fire_offwork_pickup

        loc_payload = text if text else json.dumps(rec, ensure_ascii=False)
        ingest_ios_location(db, loc_payload, received_at=str(rec.get("received_at") or "").strip() or None)
        if db.get_meta("ios_wakeup_pending_brief") == "1":
            db.set_meta("ios_wakeup_pending_brief", "0")
            flush_morning_brief_if_due(db)
        try:
            check_and_fire_offwork_pickup(db)
            detect_unknown_dwell_and_ask(db)
        except Exception:
            pass
        stats["heartbeat"] += 1
        return

    if src == "ios_tesla":
        from siming.tesla_link import ingest_tesla_state

        ts_rx = str(rec.get("received_at") or "").strip() or None
        t_res = ingest_tesla_state(db, text if text else rec, source="ios_tesla", received_at=ts_rx)
        db.mark_source_ok("ios_tesla", detail=f"soc={t_res.get('soc_pct')}%", ok_at=ts_rx)
        stats["heartbeat"] += 1
        return

    if src in ("ios_sleep", "ios_focus") or text.strip().lower() in (
        "sleep",
        "asleep",
        "awake",
        "wake",
        "alarm_off",
        "charge_on",
        "charge_off",
        "phone_active",
        "app_open",
        "睡眠开启",
        "睡眠结束",
    ):
        from siming.attention import ingest_ios_sleep_signal
        from siming.router import flush_morning_brief_if_due

        s_res = ingest_ios_sleep_signal(
            db,
            text.strip(),
            received_at=str(rec.get("received_at") or "").strip() or None,
        )
        if s_res.get("state") == "AWAKE" and db.get_meta("ios_wakeup_pending_brief") == "1":
            db.set_meta("ios_wakeup_pending_brief", "0")
            flush_morning_brief_if_due(db)
        stats["heartbeat"] += 1
        return

    if contains_otp(text):
        stats["blocked_otp"] += 1
        return

    if src == "ios_bill_ocr":
        ocr_res = try_ingest_bill_ocr(db, text, source="ios_bill_ocr")
        stats["bill_ocr"] += int(ocr_res.get("ingested") or 0)
        return

    if src == "ios_sms_backfill":
        occ_iso, clean_body, msg_date = extract_sms_timestamp_and_body(text)
        body = (clean_body or text).strip()
        non_empty_lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
        if non_empty_lines and all(_BARE_SENDER_ONLY_RE.fullmatch(ln) for ln in non_empty_lines):
            stats["malformed_backfill"] += 1
            return
        if contains_otp(body):
            stats["blocked_otp"] += 1
            return
        raw_ref = store_encrypted_raw(text, db.paths)
        if "【" not in body:
            return
        bk = try_ingest_bank_sms(
            db,
            body,
            source="ios_sms_backfill",
            raw_ref=raw_ref,
            occurred_at=occ_iso,
            silent_backfill=True,
        )
        if bk.get("matched"):
            stats["sms_bank"] += 1
            return
        if msg_date is not None and 0 <= (today - msg_date).days <= 1:
            pk = try_ingest_pickup_code(db, body, source="ios_sms_backfill", raw_ref=raw_ref)
            if pk.get("matched"):
                stats["sms_pickup"] += 1
                return
        m_future = re.search(r"(20\d{2})[-年](\d{1,2})[-月](\d{1,2})", body)
        if m_future:
            try:
                td = date(int(m_future.group(1)), int(m_future.group(2)), int(m_future.group(3)))
                if td >= today and _ingest_travel_or_hospital_sms(db, body, raw_ref):
                    stats["drop"] += 1
                    return
            except ValueError:
                pass
        fp = f"sms_hist:{hashlib.sha256(body.encode('utf-8')).hexdigest()[:28]}"
        with db.transaction() as conn:
            db.append_event(
                conn,
                source="ios_sms_backfill",
                thread_key="sms:history",
                summary=f"存量短信归档：{body[:60]}",
                payload={"body": body[:300]},
                raw_ref=raw_ref,
                occurred_at=occ_iso or iso_now(),
                fingerprint=fp,
            )
        stats["sms_archived"] += 1
        return

    raw_ref = store_encrypted_raw(text, db.paths)
    if src == "ios_sms":
        pk = try_ingest_pickup_code(db, text, source="ios_sms", raw_ref=raw_ref)
        if pk.get("matched"):
            stats["sms_pickup"] += 1
            return
        bk = try_ingest_bank_sms(db, text, source="ios_sms", raw_ref=raw_ref)
        if bk.get("matched"):
            stats["sms_bank"] += 1
            return
        if _ingest_travel_or_hospital_sms(db, text, raw_ref):
            stats["drop"] += 1
            return
        fp = f"sms_auto:{hashlib.sha256(text.encode('utf-8')).hexdigest()[:28]}"
        with db.transaction() as conn:
            db.append_event(
                conn,
                source="ios_sms",
                thread_key="sms:notice",
                summary=f"短信归档：{text[:60]}",
                payload={"body": text[:300]},
                raw_ref=raw_ref,
                fingerprint=fp,
            )
        stats["sms_archived"] += 1
        return

    drop_item(
        db,
        text=text,
        source_channel=src,
        occurred_at=str(rec.get("received_at") or "").strip() or None,
    )
    stats["drop"] += 1


def process_edge_records(db: SimingDB, items: list[dict[str, Any]]) -> dict[str, int]:
    """将 81 拉回的队列记录逐条隔离落库：单条异常转入 deadletter，绝不卡死整批游标。"""
    import hashlib

    stats = {
        "heartbeat": 0,
        "sms_bank": 0,
        "sms_pickup": 0,
        "sms_archived": 0,
        "malformed_backfill": 0,
        "bill_ocr": 0,
        "remote_run": 0,
        "drop": 0,
        "blocked_otp": 0,
        "deadletter": 0,
    }
    today = now_local().date()
    for rec in items:
        try:
            _process_single_edge_record(db, rec, today, stats)
        except Exception as exc:
            stats["deadletter"] += 1
            rec_id = str(rec.get("id") or "")
            raw_dump = json.dumps(rec, ensure_ascii=False, default=str)
            raw_ref = store_encrypted_raw(raw_dump, db.paths)
            fp = f"edge_dlq:{rec_id or hashlib.sha256(raw_dump.encode('utf-8')).hexdigest()[:20]}"
            with db.transaction() as conn:
                db.append_event(
                    conn,
                    source="edge_deadletter",
                    thread_key="edge:deadletter",
                    summary=f"边缘记录解析异常已隔离 (id={rec_id}): {type(exc).__name__}",
                    payload={"edge_id": rec_id, "source": rec.get("source"), "error": str(exc)[:200]},
                    raw_ref=raw_ref,
                    fingerprint=fp,
                )
    return stats


def _sync_track_manual_snapshot_if_live(db: SimingDB) -> None:
    """只读缓存 81:/var/lib/track/manual.json 到本地（仅当真实读取成功时原子替换，绝不用空字典覆盖已有预约缓存！）。"""
    if (
        os.environ.get("SIMING_DISABLE_SSH_EDGE") == "1"
        or os.environ.get("SIMING_TEST_OFFLINE") == "1"
    ):
        return
    policy = load_policy(db.paths)
    host = str(policy["edge"]["ssh_host"])
    try:
        proc = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "sudo -n test ! -f /var/lib/track/manual.json && echo '{}' || sudo -n cat /var/lib/track/manual.json"],
            capture_output=True,
            text=True,
            timeout=8,
        )
        if proc.returncode == 0 and (proc.stdout or "").strip().startswith("{"):
            parsed = json.loads(proc.stdout.strip())
            if isinstance(parsed, dict):
                cache_p = db.paths.data_dir / "track_manual_cache.json"
                tmp_p = db.paths.data_dir / f"track_manual_cache.json.tmp.{os.getpid()}"
                tmp_p.write_text(
                    json.dumps({"fetched_at": iso_now(), "manual": parsed}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                os.replace(tmp_p, cache_p)
    except Exception:
        pass


def sync_edge_node(
    db: SimingDB,
    transport: Optional[Callable[[list[str], Optional[str]], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """每 5m 执行：核验主 tick 真实存活后发本机心跳 + 拉游标增量落库（先落盘再推游标）。"""
    from siming.crypto import redact_sensitive

    db.init_db()
    call = transport or (lambda args, stdin=None: _ssh_edge_call(db, args, stdin))

    # 1. 绑定主 tick 真实存活时间：若从未成功过且超过首次启动 10 分钟宽限、或时间戳损坏/超前/停滞 >10m，停发死人开关心跳！
    now_dt = now_local()
    last_tick_ok = db.get_meta("last_tick_ok_at")
    tick_healthy = False
    if last_tick_ok:
        try:
            tick_age = (now_dt - parse_iso(last_tick_ok)).total_seconds()
            if -60 <= tick_age <= 600:
                tick_healthy = True
        except Exception:
            tick_healthy = False
    else:
        grace_anchor = db.get_meta("edge_sync_boot_grace_at")
        if not grace_anchor:
            grace_anchor = iso_now(now_dt)
            db.set_meta("edge_sync_boot_grace_at", grace_anchor)
        try:
            if 0 <= (now_dt - parse_iso(grace_anchor)).total_seconds() <= 600:
                tick_healthy = True
        except Exception:
            tick_healthy = False

    if tick_healthy:
        hb_res = call(["host-heartbeat", "--ts", last_tick_ok or iso_now()], None)
        if not hb_res.get("ok"):
            db.mark_source_error("edge_vps", "ERROR", str(hb_res.get("error", "heartbeat failed")))
            return {"ok": False, "error": hb_res.get("error")}

    conn = db.connect()
    try:
        row = conn.execute("SELECT cursor FROM sources WHERE id='edge_vps'").fetchone()
        after_id = int(row["cursor"]) if (row and str(row["cursor"]).isdigit()) else 0
    finally:
        conn.close()

    # 2. 按游标拉取增量队列（单次最高 500 条，覆盖存量短信批量回溯）
    pull_res = call(["pull", "--after", str(after_id), "--limit", "500"], None)
    if not pull_res.get("ok"):
        db.mark_source_error("edge_vps", "ERROR", str(pull_res.get("error", "pull failed")))
        return {"ok": False, "error": pull_res.get("error")}

    items: list[dict[str, Any]] = list(pull_res.get("items") or [])
    stats = process_edge_records(db, items)
    max_id = max([after_id, *[int(it["id"]) for it in items if "id" in it]])

    if transport is None:
        _sync_track_manual_snapshot_if_live(db)

    detail_parts = [f"cursor={max_id}, pulled={len(items)}"]
    if not tick_healthy:
        detail_parts.append("WARN:siming-tick_stale_hb_withheld")
    if stats.get("malformed_backfill"):
        detail_parts.append(f"WARN:sender_only_backfill={stats['malformed_backfill']}")

    # 3. 全部本地落盘成功后，才推进游标
    db.mark_source_ok(
        "edge_vps",
        cursor=str(max_id),
        detail=", ".join(detail_parts),
    )
    return {"ok": True, "cursor": max_id, "pulled": len(items), "tick_healthy": tick_healthy, "stats": stats}


def sync_outbox_item_to_edge(db: SimingDB, outbox_id: str) -> dict[str, Any]:
    from siming.crypto import redact_sensitive

    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM outbox WHERE id=?", (outbox_id,)).fetchone()
    finally:
        conn.close()
    if not row or row["status"] != "PRESCHEDULED":
        return {"ok": True, "skipped": True}

    safe_text = redact_sensitive(str(row["text"] or ""), db.paths)
    payload = json.dumps(
        {
            "ref_id": row["ref_id"],
            "text": safe_text,
            "fire_at": row["send_at"],
            "synced_at": iso_now(),
        },
        ensure_ascii=False,
    )
    res = _ssh_edge_call(db, ["outbox-put"], stdin_text=payload)
    if res.get("ok"):
        with db.transaction() as conn:
            conn.execute("UPDATE outbox SET edge_synced=1 WHERE id=?", (outbox_id,))
    return res


def withdraw_outbox_from_edge(db: SimingDB, ref_id: str) -> dict[str, Any]:
    if not ref_id:
        return {"ok": True}
    return _ssh_edge_call(db, ["outbox-withdraw", "--ref-id", ref_id])
