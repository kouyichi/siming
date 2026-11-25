"""163 账单与发票只读 IMAP 采集器（含网易 IMAP ID 握手、UIDVALIDITY+UID 与附件哈希去重、先落盘后推游标）。"""

from __future__ import annotations

import email
import hashlib
import imaplib
import json
from email.header import decode_header
from email.message import Message
from typing import Any, Optional

from siming.config import load_sources_config
from siming.crypto import read_secret, store_encrypted_raw
from siming.db import SimingDB
from siming.ledger.reimburse import stage_invoice_attachment
from siming.ledger.statement_parser import try_ingest_statement_text

# 注册网易 163 必检的 RFC 2971 IMAP ID 命令（否则 SELECT INBOX 会被拒 Unsafe Login）
if "ID" not in imaplib.Commands:
    imaplib.Commands["ID"] = ("NONAUTH", "AUTH", "SELECTED")


def _parse_mail163_secret(raw_secret: str) -> tuple[str, str]:
    lines = [ln.strip() for ln in raw_secret.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    user, pwd = "", ""
    for ln in lines:
        if "=" in ln:
            k, v = ln.split("=", 1)
            k_low = k.strip().lower()
            if k_low in ("user", "email", "imap_user", "account"):
                user = v.strip().strip('"').strip("'")
            elif k_low in ("pass", "password", "imap_pass", "auth_code", "token"):
                pwd = v.strip().strip('"').strip("'")
    if not user and len(lines) >= 2:
        user, pwd = lines[0], lines[1]
    return user, pwd


def _decode_mime_header(raw_val: Optional[str]) -> str:
    if not raw_val:
        return ""
    out: list[str] = []
    for part, enc in decode_header(raw_val):
        if isinstance(part, bytes):
            out.append(part.decode(enc or "utf-8", errors="replace"))
        else:
            out.append(str(part))
    return "".join(out).strip()


def _extract_text_and_attachments(msg: Message) -> tuple[str, list[tuple[str, bytes]]]:
    texts: list[str] = []
    attachments: list[tuple[str, bytes]] = []
    for part in msg.walk():
        if part.is_multipart():
            continue
        disp = str(part.get("Content-Disposition") or "")
        fname = _decode_mime_header(part.get_filename())
        payload = part.get_payload(decode=True) or b""
        ctype = part.get_content_type()
        if fname or "attachment" in disp.lower():
            attachments.append((fname or "attachment.bin", payload))
        elif ctype in ("text/plain", "text/html") and payload:
            charset = part.get_content_charset() or "utf-8"
            texts.append(payload.decode(charset, errors="replace"))
    return "\n".join(texts), attachments


def ingest_parsed_email(
    db: SimingDB,
    uid_key: str,
    subject: str,
    sender: str,
    date_hdr: str,
    body_text: str,
    attachments: list[tuple[str, bytes]],
) -> dict[str, Any]:
    """纯本地处理单封邮件（无模型权限）：账单入账、发票暂存与事件记录。"""
    raw_ref = store_encrypted_raw(f"Subject: {subject}\nFrom: {sender}\n\n{body_text[:20000]}", db.paths)
    att_hashes = [hashlib.sha256(b).hexdigest()[:16] for _n, b in attachments]
    fp = f"mail163:{uid_key}"

    with db.transaction() as conn:
        ev_id = db.append_event(
            conn,
            source="mail163",
            thread_key=f"mail:{uid_key}",
            summary=f"163邮件：{subject[:80]}",
            payload={"sender": sender, "date": date_hdr, "attachments": [n for n, _b in attachments], "att_hashes": att_hashes},
            raw_ref=raw_ref,
            fingerprint=fp,
        )
        if ev_id is None:
            return {"duplicate": True}

    is_invoice = any(k in subject for k in ("发票", "行程单", "电子客票", "invoice"))
    staged_pdfs: list[str] = []
    if is_invoice:
        for fname, content in attachments:
            if fname.lower().endswith(".pdf") and content:
                out_p = stage_invoice_attachment(db, fname, content)
                staged_pdfs.append(str(out_p))

    stmt_res = {"matched": False}
    if any(k in subject for k in ("账单", "信用卡", "月结单", "电子对账单")):
        stmt_res = try_ingest_statement_text(db, subject=subject, content=body_text, event_id=ev_id)

    return {
        "duplicate": False,
        "event_id": ev_id,
        "is_invoice": is_invoice,
        "staged_pdfs": staged_pdfs,
        "statement": stmt_res,
    }


def collect_mail163(db: SimingDB, max_fetch: int = 30) -> dict[str, Any]:
    """优先走本机 Omamail 只读桥接（163 / Yahoo / Gmail 零凭据复制）；若无 Omamail 则回退直连 IMAP。"""
    from siming.collectors.omamail_bridge import collect_omamail

    db.init_db()
    om_res = collect_omamail(db, max_per_account=max_fetch)
    if om_res.get("available") and not om_res.get("unconfigured"):
        return om_res

    secret = read_secret("mail163", db.paths)
    if not secret:
        db.mark_source_error("mail163", "UNCONFIGURED", "待连接 Omamail 或配置 ~/.config/siming/secrets/mail163")
        return {"ok": True, "unconfigured": True}

    user, pwd = _parse_mail163_secret(secret)
    if not user or not pwd:
        db.mark_source_error("mail163", "AUTH_EXPIRED", "secrets/mail163 格式不完整（需邮箱与授权码）")
        return {"ok": False, "error": "invalid mail163 secret format"}

    cfg = load_sources_config(db.paths).get("mail163") or {}
    host = str(cfg.get("host") or "imap.163.com")
    port = int(cfg.get("port") or 993)
    folders: list[str] = list(cfg.get("folders") or ["INBOX"])

    conn = db.connect()
    try:
        row = conn.execute("SELECT cursor FROM sources WHERE id='mail163'").fetchone()
        cursors: dict[str, str] = json.loads(row["cursor"]) if (row and row["cursor"]) else {}
    except Exception:
        cursors = {}
    finally:
        conn.close()

    ingested_count = 0
    try:
        client = imaplib.IMAP4_SSL(host, port, timeout=20)
        try:
            client.login(user, pwd)
            # 发送网易 163 要求的客户端身份标识
            client._simple_command(
                "ID", '("name" "siming" "version" "0.1.0" "vendor" "siming-local")'
            )
            for folder in folders:
                typ, sel_data = client.select(folder, readonly=True)
                if typ != "OK":
                    continue
                uidvalidity = str(client.response("UIDVALIDITY")[1][0].decode("ascii", errors="ignore") or "0")
                prev_token = cursors.get(folder, "")
                prev_uv, prev_uid = prev_token.split(":", 1) if ":" in prev_token else ("", "0")
                start_uid = (int(prev_uid) + 1) if (prev_uv == uidvalidity and prev_uid.isdigit()) else 1

                # 若首次连接邮箱且邮件很多，只拉最后 max_fetch 封建立游标基线
                typ_s, uid_data = client.uid("search", None, f"UID {start_uid}:*")
                if typ_s != "OK" or not uid_data or not uid_data[0]:
                    cursors[folder] = f"{uidvalidity}:{max(0, start_uid - 1)}"
                    continue
                uids = [u.decode("ascii") for u in uid_data[0].split() if u]
                if start_uid == 1 and len(uids) > max_fetch:
                    uids = uids[-max_fetch:]

                highest_uid = int(prev_uid) if (prev_uv == uidvalidity and prev_uid.isdigit()) else 0
                for u_str in uids:
                    t_f, msg_data = client.uid("fetch", u_str, "(RFC822)")
                    if t_f != "OK" or not msg_data or not isinstance(msg_data[0], tuple):
                        continue
                    msg = email.message_from_bytes(msg_data[0][1])
                    subj = _decode_mime_header(msg.get("Subject"))
                    sender = _decode_mime_header(msg.get("From"))
                    date_hdr = str(msg.get("Date") or "")
                    body, atts = _extract_text_and_attachments(msg)
                    res = ingest_parsed_email(
                        db,
                        uid_key=f"{folder}:{uidvalidity}:{u_str}",
                        subject=subj,
                        sender=sender,
                        date_hdr=date_hdr,
                        body_text=body,
                        attachments=atts,
                    )
                    if not res.get("duplicate"):
                        ingested_count += 1
                    highest_uid = max(highest_uid, int(u_str))

                # 先落盘，再更新内存游标
                cursors[folder] = f"{uidvalidity}:{highest_uid}"
        finally:
            try:
                client.logout()
            except Exception:
                pass
    except imaplib.IMAP4.error as exc:
        db.mark_source_error("mail163", "AUTH_EXPIRED", f"IMAP 登录或读取被拒: {exc}")
        return {"ok": False, "error": str(exc)}
    except Exception as exc:
        db.mark_source_error("mail163", "ERROR", str(exc))
        return {"ok": False, "error": str(exc)}

    db.mark_source_ok(
        "mail163",
        cursor=json.dumps(cursors, ensure_ascii=False),
        detail=f"folders={len(folders)}, new={ingested_count}",
        used=ingested_count > 0,
    )
    return {"ok": True, "ingested": ingested_count, "cursors": cursors}
