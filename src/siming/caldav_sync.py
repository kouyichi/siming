"""P3 (#4 · D3-B) 双向 iCloud/CalDAV 日历同步与只读 ICS 投影（caldav_sync.py）：
1. CalendarObject(account, calendar, href, uid, recurrence_id, etag, sequence, tzid, start, end, all_day, status, origin_ref)；
2. 增量 sync-collection token + ETag 兜底；同步游标与对象变更同一 SQLite 事务提交；
3. 条件版本写回（If-Match: <etag> / If-None-Match: *），遇 412 冲突自动拉取远端最新版返回结构化差异，绝不盲目覆盖；
4. 用 X-SIMING-ORIGIN-REF / origin_ref 彻底阻断「司命写入 -> 日历回步 -> 再建一条提醒」回声循环；
5. 支持 RRULE 窗口展开、EXDATE 排除、RECURRENCE-ID 单次例外、全天日期、跨时区，以及「会议推迟自动重算出发提醒、保留手动改过的提醒」。
"""

from __future__ import annotations

import base64
import json
import re
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import httpx

from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local, parse_iso

DAV_NS = "DAV:"
CALDAV_NS = "urn:ietf:params:xml:ns:caldav"
CS_NS = "http://calendarserver.org/ns/"


def _unfold_ics(raw: str) -> list[str]:
    text = (raw or "").replace("\r\n", "\n").replace("\r", "\n")
    lines: list[str] = []
    for ln in text.split("\n"):
        if ln.startswith((" ", "\t")) and lines:
            lines[-1] += ln[1:]
        else:
            lines.append(ln)
    return lines


def _parse_ics_dt(val: str, tzid: str = "") -> tuple[str, bool, str]:
    """解析 ICS 日期/时刻，返回 (iso_str, all_day, effective_tzid)。"""
    clean = (val or "").strip()
    if re.fullmatch(r"\d{8}", clean):
        d = datetime.strptime(clean, "%Y%m%d").date()
        return d.isoformat(), True, tzid or "Asia/Shanghai"
    if clean.endswith("Z"):
        dt = datetime.strptime(clean, "%Y%m%dT%H%M%SZ").replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(SHANGHAI_TZ).isoformat(timespec="seconds"), False, "UTC"
    tz = SHANGHAI_TZ
    eff_tz = tzid or "Asia/Shanghai"
    if tzid:
        try:
            tz = ZoneInfo(tzid)
        except Exception:
            tz = SHANGHAI_TZ
            eff_tz = "Asia/Shanghai"
    dt = datetime.strptime(clean[:15], "%Y%m%dT%H%M%S").replace(tzinfo=tz)
    return dt.astimezone(SHANGHAI_TZ).isoformat(timespec="seconds"), False, eff_tz


def parse_ics_vevents(ics_text: str) -> list[dict[str, Any]]:
    """纯标准库解析 VCALENDAR 中的全部 VEVENT（支持主事件、RECURRENCE-ID 例外、EXDATE、RRULE 与 X-SIMING-ORIGIN-REF）。"""
    lines = _unfold_ics(ics_text)
    events: list[dict[str, Any]] = []
    cur: Optional[dict[str, Any]] = None
    in_alarm = False

    for ln in lines:
        up = ln.strip().upper()
        if up == "BEGIN:VALARM":
            in_alarm = True
            continue
        if up == "END:VALARM":
            in_alarm = False
            continue
        if in_alarm:
            continue
        if up == "BEGIN:VEVENT":
            cur = {
                "uid": "",
                "recurrence_id": "",
                "sequence": 0,
                "tzid": "Asia/Shanghai",
                "start_at": "",
                "end_at": "",
                "all_day": False,
                "summary": "",
                "location": "",
                "description": "",
                "rrule": "",
                "exdates": [],
                "status": "CONFIRMED",
                "origin_ref": "",
            }
            continue
        if up == "END:VEVENT" and cur is not None:
            if cur["uid"] and cur["start_at"]:
                events.append(cur)
            cur = None
            continue
        if cur is None or ":" not in ln:
            continue

        head, val = ln.split(":", 1)
        parts = head.split(";")
        prop = parts[0].strip().upper()
        params: dict[str, str] = {}
        for p in parts[1:]:
            if "=" in p:
                pk, pv = p.split("=", 1)
                params[pk.strip().upper()] = pv.strip().strip('"')

        val_unesc = val.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").strip()
        if prop == "UID":
            cur["uid"] = val_unesc
        elif prop == "SUMMARY":
            cur["summary"] = val_unesc
        elif prop == "LOCATION":
            cur["location"] = val_unesc
        elif prop == "DESCRIPTION":
            cur["description"] = val_unesc
        elif prop == "STATUS":
            cur["status"] = val_unesc.upper()
        elif prop == "SEQUENCE":
            try:
                cur["sequence"] = int(val_unesc)
            except ValueError:
                cur["sequence"] = 0
        elif prop == "RRULE":
            cur["rrule"] = val_unesc
        elif prop == "X-SIMING-ORIGIN-REF":
            cur["origin_ref"] = val_unesc
        elif prop == "DTSTART":
            iso_v, all_day, tz = _parse_ics_dt(val_unesc, params.get("TZID", ""))
            cur["start_at"] = iso_v
            cur["all_day"] = all_day or (params.get("VALUE", "").upper() == "DATE")
            cur["tzid"] = tz
        elif prop == "DTEND":
            iso_v, _, _ = _parse_ics_dt(val_unesc, params.get("TZID", ""))
            cur["end_at"] = iso_v
        elif prop == "RECURRENCE-ID":
            iso_v, _, _ = _parse_ics_dt(val_unesc, params.get("TZID", ""))
            cur["recurrence_id"] = iso_v
        elif prop == "EXDATE":
            for item in val_unesc.split(","):
                item_s = item.strip()
                if item_s:
                    iso_v, _, _ = _parse_ics_dt(item_s, params.get("TZID", ""))
                    cur["exdates"].append(iso_v)
    return events


def _ics_escape(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def render_ics_vevent(
    *,
    uid: str,
    summary: str,
    start_at: str,
    end_at: str = "",
    all_day: bool = False,
    tzid: str = "Asia/Shanghai",
    location: str = "",
    description: str = "",
    rrule: str = "",
    origin_ref: str = "",
    sequence: int = 0,
    status: str = "CONFIRMED",
) -> str:
    """生成符合 RFC 5545 的单事件 VCALENDAR 文本，内嵌 X-SIMING-ORIGIN-REF 防回声标记。"""
    stamp = now_local().astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Siming Personal Agent//CalDAV v6.2//CN",
        "BEGIN:VEVENT",
        f"UID:{uid}",
        f"DTSTAMP:{stamp}",
        f"SEQUENCE:{int(sequence)}",
        f"STATUS:{status}",
        f"SUMMARY:{_ics_escape(summary)}",
    ]
    if all_day:
        d_s = start_at[:10].replace("-", "")
        lines.append(f"DTSTART;VALUE=DATE:{d_s}")
        if end_at:
            lines.append(f"DTEND;VALUE=DATE:{end_at[:10].replace('-', '')}")
    else:
        dt_s = parse_iso(start_at).astimezone(ZoneInfo(tzid) if tzid else SHANGHAI_TZ)
        lines.append(f"DTSTART;TZID={tzid}:{dt_s.strftime('%Y%m%dT%H%M%S')}")
        if end_at:
            dt_e = parse_iso(end_at).astimezone(ZoneInfo(tzid) if tzid else SHANGHAI_TZ)
        else:
            dt_e = dt_s + timedelta(hours=1)
        lines.append(f"DTEND;TZID={tzid}:{dt_e.strftime('%Y%m%dT%H%M%S')}")
    if location:
        lines.append(f"LOCATION:{_ics_escape(location)}")
    if description:
        lines.append(f"DESCRIPTION:{_ics_escape(description)}")
    if rrule:
        lines.append(f"RRULE:{rrule}")
    if origin_ref:
        lines.append(f"X-SIMING-ORIGIN-REF:{origin_ref}")
    lines.extend(["END:VEVENT", "END:VCALENDAR"])
    return "\r\n".join(lines) + "\r\n"


def _obj_key(account: str, calendar: str, uid: str, recurrence_id: str = "") -> str:
    base = f"{account}::{calendar}::{uid}"
    return f"{base}::{recurrence_id}" if recurrence_id else base


def _default_caldav_http(
    method: str,
    url: str,
    *,
    headers: Optional[dict[str, str]] = None,
    content: Optional[bytes] = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    with httpx.Client(timeout=timeout, follow_redirects=True) as client:
        resp = client.request(method, url, headers=headers or {}, content=content)
        return {
            "status_code": resp.status_code,
            "headers": {k.lower(): v for k, v in resp.headers.items()},
            "text": resp.text,
        }


def _auth_headers(username: str, app_password: str) -> dict[str, str]:
    if not username or not app_password:
        return {}
    tok = base64.b64encode(f"{username}:{app_password}".encode("utf-8")).decode("ascii")
    return {"Authorization": f"Basic {tok}"}


def _parse_multistatus_caldav(xml_text: str) -> tuple[str, list[dict[str, Any]]]:
    """解析 CalDAV XML multistatus 响应，提取 sync-token 及每个 (href, etag, status, calendar_data)。"""
    root = ET.fromstring(xml_text)
    sync_token = ""
    st_el = root.find(f".//{{{DAV_NS}}}sync-token")
    if st_el is not None and st_el.text:
        sync_token = st_el.text.strip()

    items: list[dict[str, Any]] = []
    for resp_el in root.findall(f".//{{{DAV_NS}}}response"):
        href_el = resp_el.find(f"{{{DAV_NS}}}href")
        href = (href_el.text or "").strip() if href_el is not None else ""
        status_el = resp_el.find(f"{{{DAV_NS}}}status")
        top_status = (status_el.text or "").strip() if status_el is not None else ""
        etag_el = resp_el.find(f".//{{{DAV_NS}}}getetag")
        etag = (etag_el.text or "").strip().strip('"') if etag_el is not None else ""
        cal_el = resp_el.find(f".//{{{CALDAV_NS}}}calendar-data")
        ics_data = (cal_el.text or "").strip() if cal_el is not None else ""
        deleted = "404" in top_status
        items.append({"href": href, "etag": etag, "ics": ics_data, "deleted": deleted})
    return sync_token, items


def sync_caldav_calendar(
    db: SimingDB,
    *,
    account: str,
    calendar: str,
    caldav_url: str,
    username: str = "",
    app_password: str = "",
    http_fn: Optional[Callable[..., dict[str, Any]]] = None,
) -> dict[str, Any]:
    """增量同步 CalDAV 日历（支持 sync-collection token + ETag 兜底），游标与对象变更同事务提交。
    - 带 origin_ref 的司命自写事件回步时标记 echo_suppressed，绝不重复建提醒；
    - 已有会议时间发生变更时自动重算关联出发提醒（并保留 manually_edited=1 的提醒）。
    """
    db.init_db()
    req_fn = http_fn or _default_caldav_http
    sync_key = f"{account}::{calendar}"
    conn_r = db.connect_readonly()
    try:
        st_row = conn_r.execute("SELECT * FROM calendar_sync_state WHERE sync_key = ?", (sync_key,)).fetchone()
        prev_token = str(st_row["sync_token"]) if st_row else ""
    finally:
        conn_r.close()

    headers = {
        **_auth_headers(username, app_password),
        "Content-Type": "application/xml; charset=utf-8",
        "Depth": "1",
    }
    body = (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<D:sync-collection xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
        f"<D:sync-token>{prev_token}</D:sync-token>"
        "<D:sync-level>1</D:sync-level>"
        "<D:prop><D:getetag/><C:calendar-data/></D:prop>"
        "</D:sync-collection>"
    ).encode("utf-8")

    resp = req_fn("REPORT", caldav_url, headers=headers, content=body)
    code = int(resp.get("status_code") or 0)
    if code not in (200, 207):
        # 回退普通 calendar-query（部分 CalDAV 服务不支持 sync-collection）
        fallback_body = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<C:calendar-query xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">'
            "<D:prop><D:getetag/><C:calendar-data/></D:prop>"
            '<C:filter><C:comp-filter name="VCALENDAR"><C:comp-filter name="VEVENT"/></C:comp-filter></C:filter>'
            "</C:calendar-query>"
        ).encode("utf-8")
        resp = req_fn("REPORT", caldav_url, headers=headers, content=fallback_body)
        code = int(resp.get("status_code") or 0)
        if code not in (200, 207):
            return {"ok": False, "status_code": code, "error": f"caldav_report_failed:{code}"}

    new_token, items = _parse_multistatus_caldav(str(resp.get("text") or ""))
    ts = iso_now()
    upserted = 0
    deleted = 0
    echo_suppressed = 0
    shifted_uids: list[tuple[str, str]] = []

    with db.transaction() as conn:
        for it in items:
            href = it["href"]
            if it["deleted"]:
                cur = conn.execute(
                    "UPDATE calendar_objects SET status='CANCELLED', updated_at=? WHERE account=? AND calendar=? AND href=?",
                    (ts, account, calendar, href),
                )
                deleted += int(cur.rowcount or 0)
                continue
            if not it["ics"]:
                continue
            vevents = parse_ics_vevents(it["ics"])
            for ev in vevents:
                okey = _obj_key(account, calendar, ev["uid"], ev["recurrence_id"])
                prev_ev = conn.execute(
                    "SELECT start_at, origin_ref FROM calendar_objects WHERE obj_key = ?",
                    (okey,),
                ).fetchone()
                is_echo = bool(ev["origin_ref"] and ev["origin_ref"].startswith("siming:"))
                if is_echo:
                    echo_suppressed += 1
                elif prev_ev and str(prev_ev["start_at"]) != ev["start_at"] and not ev["recurrence_id"]:
                    shifted_uids.append((ev["uid"], ev["start_at"]))
                conn.execute(
                    """
                    INSERT INTO calendar_objects (
                        obj_key, account, calendar, href, uid, recurrence_id, etag, sequence,
                        tzid, start_at, end_at, all_day, summary, location, description,
                        rrule, exdates_json, status, origin_ref, raw_ics, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(obj_key) DO UPDATE SET
                        href=excluded.href, etag=excluded.etag, sequence=excluded.sequence,
                        tzid=excluded.tzid, start_at=excluded.start_at, end_at=excluded.end_at,
                        all_day=excluded.all_day, summary=excluded.summary, location=excluded.location,
                        description=excluded.description, rrule=excluded.rrule, exdates_json=excluded.exdates_json,
                        status=excluded.status, origin_ref=excluded.origin_ref, raw_ics=excluded.raw_ics,
                        updated_at=excluded.updated_at
                    """,
                    (
                        okey,
                        account,
                        calendar,
                        href,
                        ev["uid"],
                        ev["recurrence_id"],
                        it["etag"],
                        int(ev["sequence"]),
                        ev["tzid"],
                        ev["start_at"],
                        ev["end_at"],
                        1 if ev["all_day"] else 0,
                        ev["summary"],
                        ev["location"],
                        ev["description"],
                        ev["rrule"],
                        json.dumps(ev["exdates"], ensure_ascii=False),
                        ev["status"],
                        ev["origin_ref"],
                        it["ics"],
                        ts,
                    ),
                )
                upserted += 1

        conn.execute(
            """
            INSERT INTO calendar_sync_state (
                sync_key, account, calendar, caldav_url, sync_token, mode, last_synced_at, last_error, updated_at
            ) VALUES (?, ?, ?, ?, ?, 'caldav_rw', ?, '', ?)
            ON CONFLICT(sync_key) DO UPDATE SET
                caldav_url=excluded.caldav_url,
                sync_token=CASE WHEN excluded.sync_token != '' THEN excluded.sync_token ELSE calendar_sync_state.sync_token END,
                last_synced_at=excluded.last_synced_at,
                last_error='',
                updated_at=excluded.updated_at
            """,
            (sync_key, account, calendar, caldav_url, new_token or prev_token, ts, ts),
        )

    recomputed_reminders: list[dict[str, Any]] = []
    for uid, new_start in shifted_uids:
        recomputed_reminders.extend(recompute_calendar_linked_reminders(db, uid=uid, new_start_at=new_start))

    return {
        "ok": True,
        "sync_token": new_token or prev_token,
        "upserted": upserted,
        "deleted": deleted,
        "echo_suppressed": echo_suppressed,
        "shifted_reminders": recomputed_reminders,
    }


def put_caldav_event(
    db: SimingDB,
    *,
    account: str,
    calendar: str,
    caldav_url: str,
    uid: str,
    summary: str,
    start_at: str,
    end_at: str = "",
    all_day: bool = False,
    location: str = "",
    description: str = "",
    rrule: str = "",
    origin_ref: str = "",
    expected_etag: str = "",
    username: str = "",
    app_password: str = "",
    http_fn: Optional[Callable[..., dict[str, Any]]] = None,
) -> dict[str, Any]:
    """CalDAV 条件版本双向写回：
    - 更新必带 `If-Match: "<etag>"`，新建必带 `If-None-Match: *`；
    - 遇 HTTP 412 冲突立即 GET 远端最新版并返回结构化 diff，严禁盲目覆盖；
    - 自动嵌入 `X-SIMING-ORIGIN-REF` 防止下次同步回声建单。
    """
    db.init_db()
    req_fn = http_fn or _default_caldav_http
    eff_origin = origin_ref.strip() or f"siming:{uid}"
    ics_str = render_ics_vevent(
        uid=uid,
        summary=summary,
        start_at=start_at,
        end_at=end_at,
        all_day=all_day,
        location=location,
        description=description,
        rrule=rrule,
        origin_ref=eff_origin,
    )
    event_url = f"{caldav_url.rstrip('/')}/{uid}.ics"
    headers = {
        **_auth_headers(username, app_password),
        "Content-Type": "text/calendar; charset=utf-8",
    }
    if expected_etag:
        headers["If-Match"] = f'"{expected_etag.strip(chr(34))}"'
    else:
        headers["If-None-Match"] = "*"

    resp = req_fn("PUT", event_url, headers=headers, content=ics_str.encode("utf-8"))
    code = int(resp.get("status_code") or 0)
    if code == 412:
        remote_resp = req_fn("GET", event_url, headers=_auth_headers(username, app_password))
        remote_ics = str(remote_resp.get("text") or "")
        remote_etag = str((remote_resp.get("headers") or {}).get("etag") or "").strip('"')
        remote_evs = parse_ics_vevents(remote_ics)
        return {
            "ok": False,
            "conflict": True,
            "status_code": 412,
            "expected_etag": expected_etag,
            "remote_etag": remote_etag,
            "remote_event": remote_evs[0] if remote_evs else None,
            "local_event": {"uid": uid, "summary": summary, "start_at": start_at, "end_at": end_at},
        }
    if code not in (200, 201, 204):
        return {"ok": False, "status_code": code, "error": f"caldav_put_failed:{code}"}

    new_etag = str((resp.get("headers") or {}).get("etag") or "").strip('"')
    ts = iso_now()
    okey = _obj_key(account, calendar, uid)
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO calendar_objects (
                obj_key, account, calendar, href, uid, recurrence_id, etag, sequence,
                tzid, start_at, end_at, all_day, summary, location, description,
                rrule, exdates_json, status, origin_ref, raw_ics, updated_at
            ) VALUES (?, ?, ?, ?, ?, '', ?, 0, 'Asia/Shanghai', ?, ?, ?, ?, ?, ?, ?, '[]', 'CONFIRMED', ?, ?, ?)
            ON CONFLICT(obj_key) DO UPDATE SET
                etag=excluded.etag, start_at=excluded.start_at, end_at=excluded.end_at,
                all_day=excluded.all_day, summary=excluded.summary, location=excluded.location,
                description=excluded.description, rrule=excluded.rrule, origin_ref=excluded.origin_ref,
                raw_ics=excluded.raw_ics, updated_at=excluded.updated_at
            """,
            (
                okey,
                account,
                calendar,
                event_url,
                uid,
                new_etag,
                start_at,
                end_at,
                1 if all_day else 0,
                summary,
                location,
                description,
                rrule,
                eff_origin,
                ics_str,
                ts,
            ),
        )
    return {"ok": True, "uid": uid, "etag": new_etag, "origin_ref": eff_origin, "href": event_url}


def expand_calendar_window(
    db: SimingDB,
    window_start: datetime,
    window_end: datetime,
    *,
    account: str = "",
) -> list[dict[str, Any]]:
    """按窗口展开日历事件实例：处理 RRULE 重复、EXDATE 排除、RECURRENCE-ID 例外覆盖、全天与取消态。"""
    conn = db.connect_readonly()
    try:
        if account:
            rows = [dict(r) for r in conn.execute("SELECT * FROM calendar_objects WHERE account = ?", (account,)).fetchall()]
        else:
            rows = [dict(r) for r in conn.execute("SELECT * FROM calendar_objects").fetchall()]
    finally:
        conn.close()

    overrides_by_uid: dict[str, dict[str, dict[str, Any]]] = {}
    masters: list[dict[str, Any]] = []
    for r in rows:
        if r.get("recurrence_id"):
            overrides_by_uid.setdefault(str(r["uid"]), {})[str(r["recurrence_id"])] = r
        else:
            masters.append(r)

    instances: list[dict[str, Any]] = []
    for m in masters:
        if m.get("status") == "CANCELLED":
            continue
        uid = str(m["uid"])
        all_day = bool(m.get("all_day"))
        exdates = set(json.loads(m.get("exdates_json") or "[]"))
        ov_map = overrides_by_uid.get(uid, {})

        if all_day:
            d0 = date.fromisoformat(str(m["start_at"])[:10])
            dt0 = datetime(d0.year, d0.month, d0.day, 0, 0, tzinfo=SHANGHAI_TZ)
            dur = timedelta(days=1)
        else:
            dt0 = parse_iso(str(m["start_at"]))
            dt1 = parse_iso(str(m["end_at"])) if m.get("end_at") else (dt0 + timedelta(hours=1))
            dur = max(timedelta(minutes=1), dt1 - dt0)

        rrule = str(m.get("rrule") or "").upper()
        if not rrule:
            if window_start <= dt0 <= window_end:
                instances.append({**m, "instance_start": m["start_at"], "instance_end": m["end_at"], "overridden": False})
            continue

        # 展开常见 RRULE（DAILY / WEEKLY / MONTHLY + INTERVAL + COUNT + UNTIL）
        interval = 1
        max_count = 120
        until_dt: Optional[datetime] = None
        freq = "WEEKLY"
        for part in rrule.split(";"):
            if part.startswith("FREQ="):
                freq = part.split("=", 1)[1]
            elif part.startswith("INTERVAL="):
                interval = max(1, int(part.split("=", 1)[1]))
            elif part.startswith("COUNT="):
                max_count = min(200, max(1, int(part.split("=", 1)[1])))
            elif part.startswith("UNTIL="):
                u_iso, _, _ = _parse_ics_dt(part.split("=", 1)[1])
                until_dt = parse_iso(u_iso) if len(u_iso) > 10 else datetime.fromisoformat(u_iso).replace(tzinfo=SHANGHAI_TZ)

        cur_dt = dt0
        for _ in range(max_count):
            if until_dt is not None and cur_dt > until_dt:
                break
            if cur_dt > window_end:
                break
            occ_key = cur_dt.date().isoformat() if all_day else iso_now(cur_dt)
            if occ_key not in exdates:
                if occ_key in ov_map:
                    ov = ov_map[occ_key]
                    if ov.get("status") != "CANCELLED":
                        ov_dt = parse_iso(ov["start_at"]) if not ov.get("all_day") else datetime.fromisoformat(ov["start_at"][:10]).replace(tzinfo=SHANGHAI_TZ)
                        if window_start <= ov_dt <= window_end:
                            instances.append({**ov, "instance_start": ov["start_at"], "instance_end": ov["end_at"], "overridden": True})
                elif cur_dt >= window_start:
                    instances.append(
                        {
                            **m,
                            "instance_start": occ_key,
                            "instance_end": (cur_dt + dur).date().isoformat() if all_day else iso_now(cur_dt + dur),
                            "overridden": False,
                        }
                    )
            if freq == "DAILY":
                cur_dt = cur_dt + timedelta(days=interval)
            elif freq == "WEEKLY":
                cur_dt = cur_dt + timedelta(days=7 * interval)
            elif freq == "MONTHLY":
                mo = cur_dt.month - 1 + interval
                yr = cur_dt.year + mo // 12
                mo = mo % 12 + 1
                cur_dt = cur_dt.replace(year=yr, month=mo, day=min(cur_dt.day, 28))
            else:
                break

    instances.sort(key=lambda x: str(x.get("instance_start") or ""))
    return instances


def recompute_calendar_linked_reminders(
    db: SimingDB,
    *,
    uid: str,
    new_start_at: str,
    default_lead_min: int = 30,
) -> list[dict[str, Any]]:
    """当 CalDAV 会议推迟/改期时，自动重算关联的出发/准备提醒，但严格保留用户手动改过的提醒（manually_edited=1）。"""
    db.init_db()
    new_dt = parse_iso(new_start_at)
    ts = iso_now()
    updated: list[dict[str, Any]] = []
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM scheduled_obligations WHERE calendar_uid = ? AND status = 'ACTIVE'",
            (uid,),
        ).fetchall()
        for r in rows:
            d = dict(r)
            if int(d.get("manually_edited") or 0) == 1:
                updated.append({"ob_id": d["ob_id"], "action": "preserved_manual", "next_fire_at": d["next_fire_at"]})
                continue
            meta = json.loads(d.get("meta_json") or "{}")
            lead = int(meta.get("lead_minutes") or default_lead_min)
            recomputed_fire = iso_now(new_dt - timedelta(minutes=lead))
            conn.execute(
                "UPDATE scheduled_obligations SET next_fire_at=?, updated_at=? WHERE ob_id=?",
                (recomputed_fire, ts, d["ob_id"]),
            )
            updated.append({"ob_id": d["ob_id"], "action": "recomputed", "next_fire_at": recomputed_fire})
    return updated


def export_siming_ics(db: SimingDB) -> str:
    """将司命活跃硬义务与日程导出为标准只读 .ics（仅含标题与时刻，不含银行卡/隐私原文）。"""
    conn = db.connect_readonly()
    try:
        obs = [dict(r) for r in conn.execute("SELECT * FROM scheduled_obligations WHERE status='ACTIVE' AND next_fire_at != ''").fetchall()]
        cases = [dict(r) for r in conn.execute("SELECT case_id, title, due_at FROM cases WHERE status='待处理' AND due_at IS NOT NULL AND due_at != ''").fetchall()]
    finally:
        conn.close()

    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Siming Personal Agent//Readonly ICS v6.2//CN",
        "X-WR-CALNAME:司命提醒",
        "X-WR-TIMEZONE:Asia/Shanghai",
    ]
    stamp = now_local().astimezone(ZoneInfo("UTC")).strftime("%Y%m%dT%H%M%SZ")
    for ob in obs:
        dt_s = parse_iso(str(ob["next_fire_at"]))
        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:siming-ob-{ob['ob_id']}@siming.local",
                f"DTSTAMP:{stamp}",
                f"DTSTART;TZID=Asia/Shanghai:{dt_s.strftime('%Y%m%dT%H%M%S')}",
                f"DTEND;TZID=Asia/Shanghai:{(dt_s + timedelta(minutes=30)).strftime('%Y%m%dT%H%M%S')}",
                f"SUMMARY:{_ics_escape(str(ob['subject']))}",
                f"X-SIMING-ORIGIN-REF:siming:ob:{ob['ob_id']}",
                "END:VEVENT",
            ]
        )
    for c in cases:
        due = str(c["due_at"])[:10]
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
            lines.extend(
                [
                    "BEGIN:VEVENT",
                    f"UID:siming-case-{c['case_id']}@siming.local",
                    f"DTSTAMP:{stamp}",
                    f"DTSTART;VALUE=DATE:{due.replace('-', '')}",
                    f"SUMMARY:{_ics_escape(str(c['title']))}",
                    f"X-SIMING-ORIGIN-REF:siming:case:{c['case_id']}",
                    "END:VEVENT",
                ]
            )
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"
