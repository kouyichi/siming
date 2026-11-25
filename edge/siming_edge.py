#!/usr/bin/env python3
"""公网 VPS 边缘守护与死人开关（siming-edge）：
- 监听 127.0.0.1:18792 接收 iPhone POST /siming/in（Bearer 鉴权 + 二道验证码拦截 + 2m 幂等去重 + 7 天加密暂存）
- 维护断电兜底预排发件箱（本机核销同步撤回，失联代发自动带 [离线兜底 · 状态截至 HH:MM]）
- 每 5 分钟巡检本机 15 分钟心跳超时（同一故障签名 24h 只报一次，QQ 不通降级飞书）
不调模型、不解析业务、不存账本。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from zoneinfo import ZoneInfo

from cryptography.fernet import Fernet

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
MAX_BODY_BYTES = 1024 * 1024
RETENTION_SECONDS = 7 * 86400
OTP_RETENTION_SECONDS = 300

OTP_GUARD_RE = re.compile(
    r"(验证码|校验码|动态码|动态密码|认证码|登录码|verification\s*code|one[-\s]*time\s*(?:password|code)|\bOTP\b)",
    re.IGNORECASE,
)
OTP_DIGITS_RE = re.compile(r"(?<!\d)\d{4,8}(?!\d)")
# 即使开启平台登录验证码代收，银行转账/信用卡大额交易动态码仍强制拒收不进代登池
BANK_PAYMENT_OTP_RE = re.compile(
    r"(工商银行|农业银行|中国银行|建设银行|招商银行|交通银行|光大银行|中信银行|浦发银行|民生银行|平安银行|兴业银行|邮储银行|银联|转账|汇款|快捷支付|消费验证)"
)


def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI_TZ)


def iso_shanghai(dt: Optional[datetime] = None) -> str:
    return (dt or now_shanghai()).astimezone(SHANGHAI_TZ).isoformat(timespec="seconds")


def is_otp_text(text: str) -> bool:
    if not text:
        return False
    if re.search(r"(取件码|提货码|取货码|货架码|出库码|开柜码|开箱码|取餐码|驿站|丰巢|快递柜|自提柜|外卖柜|外卖已放在|兔喜|妈妈驿站|近邻宝|熊猫快收|菜鸟|家门口)", text) and not re.search(
        r"(验证码|校验码|动态码|登录密码|OTP)", text, re.IGNORECASE
    ):
        return False
    return bool(OTP_GUARD_RE.search(text) and OTP_DIGITS_RE.search(text))


_FAKE_APPLY_RE = re.compile(r"(?i)\b(apply\s+)A\d{2,6}\b")
MAX_RECEIPT_BODY_BYTES = 8192
RECEIPT_RATE_LIMIT_COUNT = 30
RECEIPT_RATE_LIMIT_WINDOW_SEC = 300


class EdgeStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.state_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.state_dir, 0o700)
        self.db_path = self.state_dir / "edge.db"
        self.key_path = self.state_dir / "fernet.key"
        self.token_path = self.state_dir / "edge-token"
        self.receipt_token_path = self.state_dir / "receipt-token"
        self.alert_conf_path = self.state_dir / "alert.json"
        self._init_db()
        self.ensure_receipt_token()

    def ensure_receipt_token(self) -> str:
        """生成或读取与 iPhone edge-token 完全隔离的受限远端完工回执子令牌（0600，仅允许写 remote_run_receipt）。"""
        if not self.receipt_token_path.exists() or not self.receipt_token_path.read_text(encoding="utf-8").strip():
            tok = "srr_" + secrets.token_urlsafe(30)
            fd = os.open(str(self.receipt_token_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, (tok + "\n").encode("utf-8"))
            finally:
                os.close(fd)
        else:
            os.chmod(self.receipt_token_path, 0o600)
        return self.receipt_token_path.read_text(encoding="utf-8").strip()

    def expected_receipt_bearer(self) -> str:
        if not self.receipt_token_path.exists():
            return ""
        return self.receipt_token_path.read_text(encoding="utf-8").strip()

    def _fernet(self) -> Fernet:
        if not self.key_path.exists() or not self.key_path.read_bytes().strip():
            key = Fernet.generate_key()
            fd = os.open(str(self.key_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            try:
                os.write(fd, key + b"\n")
            finally:
                os.close(fd)
        return Fernet(self.key_path.read_bytes().strip())

    def expected_bearer(self) -> str:
        if not self.token_path.exists():
            return ""
        return self.token_path.read_text(encoding="utf-8").strip()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=10.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 5000;")
        return conn

    def _init_db(self) -> None:
        conn = self.connect()
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedup_key TEXT NOT NULL UNIQUE,
                    source TEXT NOT NULL,
                    ciphertext BLOB NOT NULL,
                    created_epoch INTEGER NOT NULL,
                    received_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS prescheduled_outbox (
                    ref_id TEXT PRIMARY KEY,
                    text TEXT NOT NULL,
                    fire_at TEXT NOT NULL,
                    synced_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'PENDING',
                    sent_at TEXT
                );
                CREATE TABLE IF NOT EXISTS deadman_log (
                    signature TEXT PRIMARY KEY,
                    last_sent_epoch INTEGER NOT NULL,
                    detail TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS otp_inbox (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    dedup_key TEXT NOT NULL UNIQUE,
                    ciphertext BLOB NOT NULL,
                    created_epoch INTEGER NOT NULL,
                    expires_epoch INTEGER NOT NULL,
                    consumed INTEGER NOT NULL DEFAULT 0,
                    received_at TEXT NOT NULL
                );
                """
            )
            if self.db_path.exists():
                os.chmod(self.db_path, 0o600)
        finally:
            conn.close()

    def is_otp_vault_enabled(self) -> bool:
        conn = self.connect()
        try:
            row = conn.execute("SELECT value FROM meta WHERE key='allow_platform_otp'").fetchone()
            return bool(row and row["value"] == "1")
        finally:
            conn.close()

    def set_otp_vault_enabled(self, enabled: bool) -> None:
        ts = iso_shanghai()
        conn = self.connect()
        try:
            conn.execute(
                """
                INSERT INTO meta (key, value, updated_at) VALUES ('allow_platform_otp', ?, ?)
                ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                """,
                ("1" if enabled else "0", ts),
            )
        finally:
            conn.close()

    def _stage_ephemeral_otp(self, text: str) -> dict[str, Any]:
        """将本人授权转发的非银行平台登录验证码加密存入 5 分钟瞬时保险箱（绝不进常规 inbox/events）。"""
        if BANK_PAYMENT_OTP_RE.search(text):
            return {"ok": True, "dropped": "bank_otp_guard"}
        now_ep = int(time.time())
        exp_ep = now_ep + OTP_RETENTION_SECONDS
        received_at = iso_shanghai()
        norm = re.sub(r"\s+", " ", text.strip())
        dedup_key = hashlib.sha256(f"otp\n{norm}\n{now_ep // 30}".encode("utf-8")).hexdigest()
        cipher = self._fernet().encrypt(
            json.dumps({"text": text, "received_at": received_at, "expires_epoch": exp_ep}, ensure_ascii=False).encode("utf-8")
        )
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute("DELETE FROM otp_inbox WHERE expires_epoch <= ? OR consumed = 1", (now_ep,))
            ex = conn.execute("SELECT id FROM otp_inbox WHERE dedup_key=?", (dedup_key,)).fetchone()
            if ex:
                conn.execute("COMMIT;")
                return {"ok": True, "otp_staged": True, "dedup": True, "ttl_sec": OTP_RETENTION_SECONDS}
            cur = conn.execute(
                "INSERT INTO otp_inbox (dedup_key, ciphertext, created_epoch, expires_epoch, consumed, received_at) VALUES (?, ?, ?, ?, 0, ?)",
                (dedup_key, cipher, now_ep, exp_ep, received_at),
            )
            oid = int(cur.lastrowid)
            conn.execute("COMMIT;")
            return {"ok": True, "otp_staged": True, "id": oid, "ttl_sec": OTP_RETENTION_SECONDS}
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def pull_ephemeral_otps(self, consume_id: int = 0) -> list[dict[str, Any]]:
        """供本机 SSH 按需瞬时拉取 5 分钟内未过期的平台登录验证码，支持消费即焚。"""
        now_ep = int(time.time())
        f = self._fernet()
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            conn.execute("DELETE FROM otp_inbox WHERE expires_epoch <= ?", (now_ep,))
            if consume_id > 0:
                conn.execute("DELETE FROM otp_inbox WHERE id = ?", (consume_id,))
            rows = conn.execute(
                "SELECT id, ciphertext, created_epoch, expires_epoch FROM otp_inbox WHERE consumed = 0 ORDER BY id DESC LIMIT 10"
            ).fetchall()
            conn.execute("COMMIT;")
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

        out: list[dict[str, Any]] = []
        for r in rows:
            dec = json.loads(f.decrypt(bytes(r["ciphertext"])).decode("utf-8"))
            dec["id"] = int(r["id"])
            dec["created_epoch"] = int(r["created_epoch"])
            dec["expires_epoch"] = int(r["expires_epoch"])
            out.append(dec)
        return out

    def ingest_payload(self, source: str, text: str, heartbeat: bool, msg_id: str = "") -> dict[str, Any]:
        if source == "ios_otp":
            return self._stage_ephemeral_otp(text)
        if is_otp_text(text):
            if source == "ios_sms" and self.is_otp_vault_enabled():
                return self._stage_ephemeral_otp(text)
            return {"ok": True, "dropped": "otp_guard"}

        now_ep = int(time.time())
        # 若调用方提供显式 msg_id 则按 msg_id 幂等；对实时短信缩窄网络重试窗口至 10 秒，严防吞掉 30 秒内两笔真实同额消费！
        window_sec = 10 if source == "ios_sms" else 120
        bucket = msg_id.strip()[:64] if msg_id.strip() else str(now_ep // window_sec)
        norm_text = re.sub(r"\s+", " ", text.strip())
        dedup_key = hashlib.sha256(
            f"{source}\n{int(heartbeat)}\n{norm_text}\n{bucket}".encode("utf-8")
        ).hexdigest()

        received_at = iso_shanghai()
        plain_json = json.dumps(
            {
                "source": source,
                "text": text,
                "heartbeat": heartbeat,
                "received_at": received_at,
            },
            ensure_ascii=False,
        ).encode("utf-8")
        cipher = self._fernet().encrypt(plain_json)

        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            existing = conn.execute(
                "SELECT id FROM inbox WHERE dedup_key = ?", (dedup_key,)
            ).fetchone()
            if existing:
                conn.execute("COMMIT;")
                return {"ok": True, "dedup": True, "id": int(existing["id"])}

            cur = conn.execute(
                """
                INSERT INTO inbox (dedup_key, source, ciphertext, created_epoch, received_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (dedup_key, source, cipher, now_ep, received_at),
            )
            row_id = int(cur.lastrowid)
            if heartbeat or source == "ios_hb":
                conn.execute(
                    """
                    INSERT INTO meta (key, value, updated_at) VALUES ('last_ios_hb', ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (str(now_ep), received_at),
                )
            # 顺手清理超过 7 天的旧密文
            conn.execute(
                "DELETE FROM inbox WHERE created_epoch < ?",
                (now_ep - RETENTION_SECONDS,),
            )
            conn.execute("COMMIT;")
            return {"ok": True, "dedup": False, "id": row_id}
        except Exception:
            conn.execute("ROLLBACK;")
            raise
        finally:
            conn.close()

    def pull_since(self, after_id: int, limit: int = 100) -> list[dict[str, Any]]:
        f = self._fernet()
        now_ep = int(time.time())
        conn = self.connect()
        try:
            conn.execute(
                "DELETE FROM inbox WHERE created_epoch < ?",
                (now_ep - RETENTION_SECONDS,),
            )
            rows = conn.execute(
                "SELECT id, ciphertext FROM inbox WHERE id > ? ORDER BY id ASC LIMIT ?",
                (after_id, limit),
            ).fetchall()
        finally:
            conn.close()

        items: list[dict[str, Any]] = []
        for r in rows:
            dec = json.loads(f.decrypt(bytes(r["ciphertext"])).decode("utf-8"))
            dec["id"] = int(r["id"])
            items.append(dec)
        return items

    def record_host_heartbeat(self, host_ts: str) -> None:
        now_ep = int(time.time())
        ts = iso_shanghai()
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE;")
            for k, v in (("last_host_hb_epoch", str(now_ep)), ("last_host_hb_iso", host_ts or ts)):
                conn.execute(
                    """
                    INSERT INTO meta (key, value, updated_at) VALUES (?, ?, ?)
                    ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
                    """,
                    (k, v, ts),
                )
            conn.execute("COMMIT;")
        finally:
            conn.close()

    def _encrypt_outbox_text(self, text: str) -> str:
        return "enc:" + self._fernet().encrypt(text.encode("utf-8")).decode("ascii")

    def _decrypt_outbox_text(self, stored: str) -> str:
        if stored.startswith("enc:"):
            return self._fernet().decrypt(stored[4:].encode("ascii")).decode("utf-8")
        return stored

    def put_outbox(self, ref_id: str, text: str, fire_at: str, synced_at: str) -> None:
        enc_text = self._encrypt_outbox_text(text)
        conn = self.connect()
        try:
            # 严禁滞后到达的 put 复活已被 WITHDRAWN 墓碑标记的提醒！
            conn.execute(
                """
                INSERT INTO prescheduled_outbox (ref_id, text, fire_at, synced_at, status)
                VALUES (?, ?, ?, ?, 'PENDING')
                ON CONFLICT(ref_id) DO UPDATE SET
                    text = CASE WHEN prescheduled_outbox.status = 'WITHDRAWN' THEN prescheduled_outbox.text ELSE excluded.text END,
                    fire_at = CASE WHEN prescheduled_outbox.status = 'WITHDRAWN' THEN prescheduled_outbox.fire_at ELSE excluded.fire_at END,
                    synced_at = CASE WHEN prescheduled_outbox.status = 'WITHDRAWN' THEN prescheduled_outbox.synced_at ELSE excluded.synced_at END,
                    status = CASE WHEN prescheduled_outbox.status = 'WITHDRAWN' THEN 'WITHDRAWN' ELSE 'PENDING' END
                """,
                (ref_id, enc_text, fire_at, synced_at),
            )
        finally:
            conn.close()

    def withdraw_outbox(self, ref_id: str) -> int:
        ts = iso_shanghai()
        conn = self.connect()
        try:
            # 即使 withdraw 早于在途 put 抵达，也预先写入 WITHDRAWN 墓碑阻止复活
            cur = conn.execute(
                """
                INSERT INTO prescheduled_outbox (ref_id, text, fire_at, synced_at, status)
                VALUES (?, '', '9999-12-31T23:59:59+08:00', ?, 'WITHDRAWN')
                ON CONFLICT(ref_id) DO UPDATE SET status='WITHDRAWN'
                """,
                (ref_id, ts),
            )
            return cur.rowcount
        finally:
            conn.close()

    def _send_webhook_alert(self, message: str) -> tuple[bool, str]:
        """若配置了 /var/lib/siming-edge/alert.json（QQ 官方 Bot 或飞书 Webhook），发送离线兜底消息。"""
        if not self.alert_conf_path.is_file():
            return False, "no_alert_conf"
        try:
            conf = json.loads(self.alert_conf_path.read_text(encoding="utf-8"))
        except Exception as exc:
            return False, str(exc)

        # 1. 尝试 QQ 官方机器人接口（若配了 qq_app_id + qq_client_secret + qq_openid）
        qq_app = str(conf.get("qq_app_id") or "")
        qq_sec = str(conf.get("qq_client_secret") or "")
        qq_openid = str(conf.get("qq_openid") or "")
        if qq_app and qq_sec and qq_openid:
            try:
                tok_req = urllib.request.Request(
                    "https://bots.qq.com/app/getAppAccessToken",
                    data=json.dumps({"appId": qq_app, "clientSecret": qq_sec}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(tok_req, timeout=10) as resp:
                    access_token = json.loads(resp.read().decode("utf-8")).get("access_token")
                if access_token:
                    msg_req = urllib.request.Request(
                        f"https://api.sgroup.qq.com/v2/users/{qq_openid}/messages",
                        data=json.dumps({"content": message, "msg_type": 0}).encode("utf-8"),
                        headers={
                            "Content-Type": "application/json",
                            "Authorization": f"QQBot {access_token}",
                        },
                        method="POST",
                    )
                    with urllib.request.urlopen(msg_req, timeout=10) as m_resp:
                        if 200 <= m_resp.status < 300:
                            return True, "qqbot_direct"
            except Exception:
                pass

        # 2. 降级飞书自定义机器人 Webhook（必须核验响应体 code/StatusCode == 0，防 HTTP 200 业务报错误标已发）
        feishu_hook = str(conf.get("feishu_webhook") or "")
        if feishu_hook:
            try:
                f_req = urllib.request.Request(
                    feishu_hook,
                    data=json.dumps({"msg_type": "text", "content": {"text": message}}).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(f_req, timeout=10) as f_resp:
                    raw_resp = f_resp.read().decode("utf-8", errors="replace").strip()
                    if 200 <= f_resp.status < 300:
                        if raw_resp.startswith("{"):
                            resp_obj = json.loads(raw_resp)
                            f_code = resp_obj.get("code", resp_obj.get("StatusCode", 0))
                            if f_code not in (0, "0", None):
                                return False, f"feishu_biz_err:{f_code}"
                        return True, "feishu_webhook"
            except Exception as exc:
                return False, f"feishu_err:{type(exc).__name__}"
        return False, "no_reachable_channel"

    def run_deadman_check(self, silence_limit_sec: int = 900) -> dict[str, Any]:
        now_ep = int(time.time())
        now_iso = iso_shanghai()
        conn = self.connect()
        try:
            hb_row = conn.execute("SELECT value FROM meta WHERE key='last_host_hb_epoch'").fetchone()
            iso_row = conn.execute("SELECT value FROM meta WHERE key='last_host_hb_iso'").fetchone()
            if not hb_row:
                return {"ok": True, "state": "waiting_first_heartbeat"}

            last_ep = int(hb_row["value"])
            last_iso = str(iso_row["value"]) if iso_row else ""
            last_hhmm = last_iso[11:16] if len(last_iso) >= 16 else "未知"
            silence_sec = now_ep - last_ep

            if silence_sec <= silence_limit_sec:
                return {"ok": True, "state": "host_online", "silence_sec": silence_sec}

            # 本机已失联超 15 分钟：检查 24 小时告警去重
            sig = f"host_offline:{last_iso[:13]}"
            d_row = conn.execute(
                "SELECT last_sent_epoch FROM deadman_log WHERE signature='host_offline'"
            ).fetchone()
            alerted = False
            if not d_row or (now_ep - int(d_row["last_sent_epoch"])) >= 86400:
                alert_msg = (
                    f"[离线兜底 · 状态截至 {last_hhmm}] 本机 siming 已超过 {silence_sec // 60} 分钟未发心跳，可能断电或断网。"
                )
                sent_ok, ch = self._send_webhook_alert(alert_msg)
                if sent_ok:
                    conn.execute(
                        """
                        INSERT INTO deadman_log (signature, last_sent_epoch, detail)
                        VALUES ('host_offline', ?, ?)
                        ON CONFLICT(signature) DO UPDATE SET last_sent_epoch=excluded.last_sent_epoch, detail=excluded.detail
                        """,
                        (now_ep, f"sent={sent_ok}:{ch}:{sig}"),
                    )
                    alerted = True

            # 代发已到期的硬期限预排发件箱条目（仅当实际发送成功才标 SENT_OFFLINE，失败保留 PENDING 待下轮重试）
            due_rows = conn.execute(
                "SELECT * FROM prescheduled_outbox WHERE status='PENDING' AND fire_at <= ?",
                (now_iso,),
            ).fetchall()
            fired_refs: list[str] = []
            failed_refs: list[str] = []
            for r in due_rows:
                plain_txt = self._decrypt_outbox_text(str(r["text"] or ""))
                if not plain_txt:
                    continue
                text_out = f"[离线兜底 · 状态截至 {last_hhmm}] {plain_txt}"
                sent_ok, _ch = self._send_webhook_alert(text_out)
                if sent_ok:
                    conn.execute(
                        "UPDATE prescheduled_outbox SET status='SENT_OFFLINE', sent_at=? WHERE ref_id=?",
                        (now_iso, r["ref_id"]),
                    )
                    fired_refs.append(str(r["ref_id"]))
                else:
                    failed_refs.append(str(r["ref_id"]))

            return {
                "ok": True,
                "state": "host_offline",
                "silence_sec": silence_sec,
                "alerted": alerted,
                "fired_outbox": fired_refs,
                "failed_outbox": failed_refs,
            }
        finally:
            conn.close()


def run_http_server(store: EdgeStore, host: str, port: int) -> None:
    ingest_paths = {
        "/siming/in": "",
        "/siming/in/hb": "ios_hb",
        "/siming/in/sms": "ios_sms",
        "/siming/in/otp": "ios_otp",
        "/siming/in/loc": "ios_loc",
        "/siming/in/sleep": "ios_sleep",
        "/siming/in/tesla": "ios_tesla",
        "/siming/in/sms_backfill": "ios_sms_backfill",
        "/siming/in/bill_ocr": "ios_bill_ocr",
        "/siming/in/drop": "ios_drop",
    }
    receipt_rate_lock = threading.Lock()
    receipt_recent_epochs: list[float] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:
            # 禁打请求体原文与查询参数，只记路径与状态码
            safe_path = self.path.split("?", 1)[0]
            sys.stderr.write(f"[siming-edge] {self.command} {safe_path} -> {fmt % args}\n")

        def _extract_given_token(self) -> str:
            auth_hdr = str(self.headers.get("Authorization") or "").strip()
            m_bearer = re.match(r"^Bearer\s*(.+)$", auth_hdr, re.IGNORECASE)
            if m_bearer:
                return m_bearer.group(1).strip()
            m_basic = re.match(r"^Basic\s+(.+)$", auth_hdr, re.IGNORECASE)
            if m_basic:
                try:
                    decoded = base64.b64decode(m_basic.group(1).strip()).decode("utf-8", errors="replace")
                    if ":" in decoded:
                        return decoded.split(":", 1)[1].strip()
                    return decoded.strip()
                except Exception:
                    return ""
            if auth_hdr and " " not in auth_hdr:
                return auth_hdr
            return str(self.headers.get("X-Siming-Token") or "").strip()

        def _check_bearer(self) -> bool:
            expected = store.expected_bearer()
            given = self._extract_given_token()
            if not expected or not given:
                return False
            return hmac.compare_digest(given.encode("utf-8", "replace"), expected.encode("utf-8", "replace"))

        def _check_receipt_bearer(self) -> bool:
            expected = store.expected_receipt_bearer()
            given = self._extract_given_token()
            if not expected or not given:
                return False
            return hmac.compare_digest(given.encode("utf-8", "replace"), expected.encode("utf-8", "replace"))

        def _send_json(self, code: int, obj: Any, extra_headers: Optional[dict[str, str]] = None) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            if extra_headers:
                for hk, hv in extra_headers.items():
                    self.send_header(hk, hv)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _send_html(self, code: int, html_doc: str, extra_headers: Optional[dict[str, str]] = None) -> None:
            body = html_doc.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            if extra_headers:
                for hk, hv in extra_headers.items():
                    self.send_header(hk, hv)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            parsed_url = urllib.parse.urlsplit(self.path)
            clean_path = parsed_url.path.rstrip("/")
            qs = urllib.parse.parse_qs(parsed_url.query, keep_blank_values=True)
            if clean_path == "/siming/in/health":
                self._send_json(200, {"ok": True, "service": "siming-edge"})
                return

            qs_source = (qs.get("source") or [""])[0].strip()
            qs_hb = (qs.get("heartbeat") or [""])[0].strip().lower() in ("1", "true", "yes", "on")
            if clean_path == "/siming/in/hb" or (clean_path == "/siming/in" and (qs_source == "ios_hb" or qs_hb)):
                if self._check_bearer():
                    res = store.ingest_payload(source="ios_hb", text="", heartbeat=True)
                    self._send_json(200, res)
                    return

            # 免 JSON GET 直达：支持手机快捷指令 1 张卡片上报睡眠态 (/siming/in/sleep?state=sleep|awake) 与位置/Wi-Fi (/siming/in/loc?wifi=...&lat=...&lng=...&addr=...)
            if clean_path == "/siming/in/sleep" and self._check_bearer():
                st_val = (qs.get("state") or qs.get("mode") or ["sleep"])[0].strip() or "sleep"
                res = store.ingest_payload(source="ios_sleep", text=st_val, heartbeat=True)
                self._send_json(200, res)
                return

            if clean_path == "/siming/in/loc" and self._check_bearer():
                loc_obj = {
                    "wifi": (qs.get("wifi") or qs.get("ssid") or [""])[0].strip(),
                    "lat": (qs.get("lat") or [""])[0].strip(),
                    "lng": (qs.get("lng") or qs.get("lon") or [""])[0].strip(),
                    "address": (qs.get("addr") or qs.get("address") or [""])[0].strip(),
                    "scene": (qs.get("scene") or [""])[0].strip(),
                }
                if any(loc_obj.values()):
                    res = store.ingest_payload(source="ios_loc", text=json.dumps(loc_obj, ensure_ascii=False), heartbeat=True)
                    self._send_json(200, res)
                    return

            if clean_path == "/siming/in/tesla" and self._check_bearer():
                ts_obj = {
                    "soc": (qs.get("soc") or qs.get("battery") or qs.get("battery_level") or [""])[0].strip(),
                    "range_km": (qs.get("range_km") or qs.get("range") or [""])[0].strip(),
                    "charging_state": (qs.get("charging") or qs.get("charging_state") or [""])[0].strip(),
                    "plugged_in": (qs.get("plugged") or qs.get("plugged_in") or [""])[0].strip(),
                    "inside_temp_c": (qs.get("temp") or qs.get("inside_temp") or [""])[0].strip(),
                    "locked": (qs.get("locked") or [""])[0].strip(),
                }
                if any(ts_obj.values()):
                    res = store.ingest_payload(source="ios_tesla", text=json.dumps(ts_obj, ensure_ascii=False), heartbeat=True)
                    self._send_json(200, res)
                    return

            if clean_path in ingest_paths:
                accept = str(self.headers.get("Accept") or "")
                if "text/html" in accept:
                    page = (
                        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
                        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
                        "<title>siming-edge 收信接口在线</title>"
                        "<style>body{margin:0;padding:28px 18px;background:#f6f5f2;color:#111110;"
                        "font:15px/1.65 -apple-system,'PingFang SC','Noto Sans CJK SC',sans-serif}"
                        ".box{max-width:560px;margin:0 auto;background:#fcfcfb;border:1px solid #e2e0d9;"
                        "border-radius:10px;padding:20px 22px}h1{font-size:18px;margin:0 0 8px}"
                        ".ok{display:inline-block;background:#e4f2e9;color:#116a37;font-size:12px;"
                        "font-weight:600;padding:2px 8px;border-radius:4px;margin-left:6px}"
                        "p,li{color:#4c4b47}code{background:#efede8;padding:1px 5px;border-radius:4px;"
                        "font-family:ui-monospace,SFMono-Regular,monospace;font-size:13px}"
                        ".btns{display:flex;gap:10px;flex-wrap:wrap;margin-top:16px}"
                        "a.btn{background:#2266b8;color:#fff;text-decoration:none;padding:8px 14px;"
                        "border-radius:6px;font-size:14px}a.sub{background:#efede8;color:#111110}</style></head>"
                        "<body><div class='box'><h1>司命收信网关 /siming/in <span class='ok'>在线正常</span></h1>"
                        "<p>你看到这个页面是因为浏览器发送了未带密钥的 <code>GET</code> 请求，而短信/投递接口仅接收 iPhone 快捷指令发来的 <code>POST</code> 数据（心跳专用地址 <code>/siming/in/hb</code> 带 <code>Authorization</code> 头时支持直接 GET）。</p>"
                        "<ul><li><strong>想看带图例的分步配置教程？</strong>请点下方按钮打开教程页。</li>"
                        "<li><strong>在 iPhone 快捷指令里报错？</strong>请点开「获取 URL 内容」右侧圆圈箭头展开，把<strong>「方法」</strong>从默认的 <code>GET</code> 改为 <code>POST</code>，并填入 <code>Authorization</code> 头部。</li></ul>"
                        "<div class='btns'><a class='btn' href='/siming/tower/'>打开司命控制塔</a></div></div></body></html>"
                    )
                    self._send_html(405, page, {"Allow": "POST"})
                    return
                if clean_path == "/siming/in/hb":
                    self._send_json(401, {"ok": False, "error": "unauthorized"})
                    return
                self._send_json(
                    405,
                    {
                        "ok": False,
                        "service": "siming-edge",
                        "error": "method_not_allowed",
                        "hint": "siming-edge 在线；当前请求为 GET，请在快捷指令「获取 URL 内容 → 展开箭头」中把「方法」改为 POST（配置说明见 docs/IOS_SHORTCUTS_GUIDE.md）。",
                    },
                    {"Allow": "POST"},
                )
                return
            self._send_json(404, {"ok": False, "error": "not_found"})

        def do_POST(self) -> None:
            parsed_url = urllib.parse.urlsplit(self.path)
            clean_path = parsed_url.path.rstrip("/")

            # S3-4：独立受限子令牌端点 /siming/in/run-receipt（仅接受 receipt-token，强制 source=remote_run_receipt，限 8KB 与 30/5m）
            if clean_path == "/siming/in/run-receipt":
                if not self._check_receipt_bearer():
                    self._send_json(401, {"ok": False, "error": "unauthorized_receipt_token"})
                    return
                r_len = int(self.headers.get("Content-Length") or "0")
                if r_len <= 0 or r_len > MAX_RECEIPT_BODY_BYTES:
                    self._send_json(413, {"ok": False, "error": "receipt_payload_size_exceeded"})
                    return
                now_mono = time.monotonic()
                with receipt_rate_lock:
                    cutoff_mono = now_mono - RECEIPT_RATE_LIMIT_WINDOW_SEC
                    receipt_recent_epochs[:] = [t for t in receipt_recent_epochs if t >= cutoff_mono]
                    if len(receipt_recent_epochs) >= RECEIPT_RATE_LIMIT_COUNT:
                        self._send_json(429, {"ok": False, "error": "rate_limit_exceeded"})
                        return
                    receipt_recent_epochs.append(now_mono)

                r_raw = self.rfile.read(r_len).decode("utf-8", errors="replace")
                try:
                    r_data = json.loads(r_raw)
                    if not isinstance(r_data, dict):
                        raise ValueError("not_dict")
                except Exception:
                    self._send_json(400, {"ok": False, "error": "invalid_json"})
                    return

                run_id = re.sub(r"[^A-Za-z0-9_.:-]", "", str(r_data.get("run_id") or ""))[:64]
                host_s = _FAKE_APPLY_RE.sub(r"\1[REDACTED-TOKEN]", str(r_data.get("host") or "remote")[:64])
                cwd_s = _FAKE_APPLY_RE.sub(r"\1[REDACTED-TOKEN]", str(r_data.get("cwd") or "")[:200])
                cmd_s = _FAKE_APPLY_RE.sub(r"\1[REDACTED-TOKEN]", str(r_data.get("cmd") or "")[:240])
                tail_s = _FAKE_APPLY_RE.sub(r"\1[REDACTED-TOKEN]", str(r_data.get("tail") or "")[:2400])
                if is_otp_text(f"{cmd_s}\n{tail_s}"):
                    tail_s = "[tail dropped by otp_guard]"
                try:
                    exit_code_i = int(r_data.get("exit_code", 0))
                except Exception:
                    exit_code_i = 1
                try:
                    dur_sec_f = max(0.0, float(r_data.get("duration_sec", 0.0)))
                except Exception:
                    dur_sec_f = 0.0

                if not cmd_s.strip():
                    self._send_json(400, {"ok": False, "error": "empty_cmd"})
                    return

                safe_receipt_json = json.dumps(
                    {
                        "run_id": run_id,
                        "host": host_s,
                        "cwd": cwd_s,
                        "cmd": cmd_s,
                        "exit_code": exit_code_i,
                        "duration_sec": round(dur_sec_f, 2),
                        "tail": tail_s,
                    },
                    ensure_ascii=False,
                )
                res = store.ingest_payload(
                    source="remote_run_receipt",
                    text=safe_receipt_json,
                    heartbeat=False,
                    msg_id=run_id or f"srr-{int(time.time())}",
                )
                self._send_json(200, res)
                return

            if clean_path not in ingest_paths:
                self._send_json(404, {"ok": False, "error": "not_found"})
                return

            if not self._check_bearer():
                self._send_json(401, {"ok": False, "error": "unauthorized"})
                return

            path_source = ingest_paths[clean_path]
            qs = urllib.parse.parse_qs(parsed_url.query, keep_blank_values=True)
            qs_source = (qs.get("source") or [""])[0].strip()
            qs_hb = (qs.get("heartbeat") or [""])[0].strip().lower() in ("1", "true", "yes", "on")

            length = int(self.headers.get("Content-Length") or "0")
            if length < 0 or length > MAX_BODY_BYTES:
                self._send_json(413, {"ok": False, "error": "invalid_payload_size"})
                return

            if length == 0:
                if path_source == "ios_hb" or qs_source == "ios_hb" or qs_hb:
                    res = store.ingest_payload(source="ios_hb", text="", heartbeat=True)
                    self._send_json(200, res)
                    return
                self._send_json(413, {"ok": False, "error": "invalid_payload_size"})
                return

            raw = self.rfile.read(length)
            raw_text = raw.decode("utf-8", errors="replace")
            ctype = str(self.headers.get("Content-Type") or "").lower()
            data: dict[str, Any] = {}
            if "application/x-www-form-urlencoded" in ctype:
                parsed_qs = urllib.parse.parse_qs(raw_text, keep_blank_values=True)
                data = {k: (v[0] if v else "") for k, v in parsed_qs.items()}
            elif "text/plain" in ctype:
                data = {"text": raw_text}
            else:
                try:
                    parsed_json = json.loads(raw_text)
                    if isinstance(parsed_json, dict):
                        data = parsed_json
                    elif isinstance(parsed_json, str):
                        data = {"text": parsed_json}
                    else:
                        self._send_json(400, {"ok": False, "error": "invalid_json"})
                        return
                except Exception:
                    if path_source in ("ios_sms", "ios_sms_backfill", "ios_bill_ocr", "ios_drop", "ios_hb"):
                        data = {"text": raw_text}
                    else:
                        self._send_json(400, {"ok": False, "error": "invalid_json"})
                        return

            is_owntracks = "_type" in data
            ot_type = str(data.get("_type") or "").strip().lower()
            if is_owntracks and ot_type not in ("location", "transition"):
                # OwnTracks 状态/路点握手包直接记一次心跳并返回标准 [] 响应
                store.ingest_payload(source="ios_hb", text="", heartbeat=True)
                self._send_json(200, [])
                return

            # 若请求路径本身已指定子端点（如 /siming/in/hb 或 /siming/in/sms），严格以路径为准，禁止请求体越权改写 source！
            allowed_sources = {"ios_hb", "ios_sms", "ios_otp", "ios_loc", "ios_sleep", "ios_tesla", "ios_sms_backfill", "ios_bill_ocr", "ios_drop"}
            default_src = "ios_loc" if is_owntracks else "ios_drop"
            raw_source = path_source or str(data.get("source") or qs_source or default_src)[:32]
            source = raw_source if raw_source in allowed_sources else default_src
            msg_id = str(data.get("msg_id") or data.get("idempotency_key") or "")[:64]
            if source == "ios_tesla" and not data.get("text") and any(
                data.get(k) not in (None, "") for k in ("soc", "battery", "battery_level", "range_km", "range", "charging_state", "charging")
            ):
                text = json.dumps(
                    {
                        k: v
                        for k, v in data.items()
                        if k in ("soc", "battery", "battery_level", "range_km", "range", "charging_state", "charging", "plugged_in", "plugged", "inside_temp_c", "temp", "locked", "lat", "lng", "lon", "vin")
                        and v not in (None, "")
                    },
                    ensure_ascii=False,
                )
            elif source == "ios_loc" and not data.get("text") and any(
                data.get(k) is not None and data.get(k) != ""
                for k in ("wifi", "ssid", "SSID", "lat", "lng", "lon", "address", "addr", "scene", "_type")
            ):
                loc_payload: dict[str, Any] = {
                    "wifi": str(data.get("wifi") or data.get("ssid") or data.get("SSID") or "").strip(),
                    "lat": str(data.get("lat") if data.get("lat") is not None else "").strip(),
                    "lng": str(data.get("lng") if data.get("lng") is not None else (data.get("lon") if data.get("lon") is not None else "")).strip(),
                    "address": str(data.get("address") or data.get("addr") or data.get("desc") or "").strip(),
                    "scene": str(data.get("scene") or "").strip(),
                }
                for extra_k in ("vel", "batt", "acc", "conn", "tst", "event", "inregions", "app", "_type"):
                    if data.get(extra_k) not in (None, ""):
                        loc_payload[extra_k] = data.get(extra_k)
                text = json.dumps(loc_payload, ensure_ascii=False)
                if is_owntracks and data.get("tst"):
                    msg_id = msg_id or f"ot-{data.get('tst')}-{loc_payload['lat']}-{loc_payload['lng']}"[:64]
            elif source == "ios_sleep" and not data.get("text") and (data.get("state") or data.get("mode")):
                text = str(data.get("state") or data.get("mode") or "").strip()
            else:
                text = str(
                    data.get("text")
                    or data.get("content")
                    or data.get("message")
                    or data.get("body")
                    or ""
                )
            if not text and path_source in ("ios_sms", "ios_sms_backfill", "ios_bill_ocr", "ios_drop") and len(data) == 1:
                only_val = next(iter(data.values()))
                if isinstance(only_val, str):
                    text = only_val

            raw_hb = data.get("heartbeat", False)
            if isinstance(raw_hb, str):
                hb_flag = raw_hb.strip().lower() in ("1", "true", "yes", "on", "是")
            else:
                hb_flag = bool(raw_hb)
            heartbeat = (source == "ios_hb") or (not path_source and (hb_flag or qs_hb))
            if not heartbeat and not text.strip():
                self._send_json(400, {"ok": False, "error": "empty_text"})
                return

            # 存量短信批量回溯：在边缘节点逐条切分并独立拦截验证码，防单条验证码误杀整批
            if source == "ios_sms_backfill":
                if "===SMS===" in text:
                    chunks = [c.strip() for c in text.split("===SMS===") if c.strip()]
                elif "|||" in text:
                    raw_blocks = re.split(r"\n(?=[^\n]{4,48}\|\|\|)", text.strip())
                    chunks = []
                    for blk in raw_blocks:
                        if "|||" not in blk:
                            continue
                        # 剥除快捷指令「如果」未命中分支透传在块末尾的纯发件人号码行
                        kept_lines: list[str] = []
                        for idx_l, ln in enumerate(blk.splitlines()):
                            s_ln = ln.strip()
                            if idx_l > 0 and (
                                re.fullmatch(r"\+?\d[\d\s()-]{4,24}", s_ln)
                                or ("【" not in s_ln and len(s_ln) <= 14 and not re.search(r"[，。：:元码]", s_ln))
                            ):
                                continue
                            kept_lines.append(ln)
                        joined_blk = "\n".join(kept_lines).strip()
                        if "【" in joined_blk:
                            chunks.append(joined_blk)
                else:
                    chunks = [
                        c.strip()
                        for c in re.split(r"\n(?=(?:[^\n]{0,40}\|\|\|\s*)?【)", text.strip())
                        if c.strip() and "【" in c
                    ]
                ingested_cnt = 0
                dedup_cnt = 0
                otp_cnt = 0
                for chunk in chunks[:1000]:
                    if is_otp_text(chunk):
                        otp_cnt += 1
                        continue
                    r = store.ingest_payload(source="ios_sms_backfill", text=chunk, heartbeat=False)
                    if r.get("dropped") == "otp_guard":
                        otp_cnt += 1
                    elif r.get("dedup"):
                        dedup_cnt += 1
                    else:
                        ingested_cnt += 1
                self._send_json(
                    200,
                    {
                        "ok": True,
                        "mode": "sms_backfill",
                        "total_items": len(chunks[:1000]),
                        "ingested": ingested_cnt,
                        "dedup": dedup_cnt,
                        "dropped_otp": otp_cnt,
                    },
                )
                return

            # 多张账单截图合并上传时，逐页过滤可能混入的验证码截图
            if source == "ios_bill_ocr" and "===PAGE===" in text:
                raw_pages = [p.strip() for p in text.split("===PAGE===") if p.strip()]
                safe_pages = [p for p in raw_pages if not is_otp_text(p)]
                dropped = len(raw_pages) - len(safe_pages)
                if not safe_pages:
                    self._send_json(200, {"ok": True, "dropped": "otp_guard", "dropped_pages": dropped})
                    return
                res = store.ingest_payload(source="ios_bill_ocr", text="\n===PAGE===\n".join(safe_pages), heartbeat=False)
                res["pages"] = len(safe_pages)
                res["dropped_pages"] = dropped
                self._send_json(200, res)
                return

            res = store.ingest_payload(source=source, text=text, heartbeat=heartbeat, msg_id=msg_id)
            if is_owntracks:
                self._send_json(200, [])
                return
            self._send_json(200, res)

    server = ThreadingHTTPServer((host, port), Handler)
    sys.stderr.write(f"[siming-edge] listening on http://{host}:{port}/siming/in\n")
    server.serve_forever()


def main() -> int:
    state_default = os.environ.get("SIMING_EDGE_STATE", "/var/lib/siming-edge")
    p = argparse.ArgumentParser(description="siming-edge daemon & CLI")
    p.add_argument("--state-dir", default=state_default)
    sub = p.add_subparsers(dest="cmd")

    p_srv = sub.add_parser("serve")
    p_srv.add_argument("--host", default="127.0.0.1")
    p_srv.add_argument("--port", type=int, default=int(os.environ.get("SIMING_EDGE_PORT", "18792")))

    p_pull = sub.add_parser("pull")
    p_pull.add_argument("--after", type=int, default=0)
    p_pull.add_argument("--limit", type=int, default=100)

    p_hb = sub.add_parser("host-heartbeat")
    p_hb.add_argument("--ts", default="")

    sub.add_parser("outbox-put")

    p_wd = sub.add_parser("outbox-withdraw")
    p_wd.add_argument("--ref-id", required=True)

    p_dm = sub.add_parser("deadman-check")
    p_dm.add_argument("--silence-sec", type=int, default=900)

    p_rt = sub.add_parser("receipt-token")
    p_rt.add_argument("--rotate", action="store_true")

    p_potp = sub.add_parser("pull-otp")
    p_potp.add_argument("--consume-id", type=int, default=0)

    p_om = sub.add_parser("otp-mode")
    p_om.add_argument("--enable", action="store_true")
    p_om.add_argument("--disable", action="store_true")

    args = p.parse_args()
    store = EdgeStore(Path(args.state_dir))

    if args.cmd in (None, "serve"):
        port = getattr(args, "port", int(os.environ.get("SIMING_EDGE_PORT", "18792")))
        host = getattr(args, "host", "127.0.0.1")
        run_http_server(store, host, port)
        return 0

    if args.cmd == "pull":
        items = store.pull_since(args.after, args.limit)
        print(json.dumps({"ok": True, "items": items}, ensure_ascii=False))
        return 0

    if args.cmd == "host-heartbeat":
        store.record_host_heartbeat(args.ts)
        print(json.dumps({"ok": True, "ts": iso_shanghai()}))
        return 0

    if args.cmd == "outbox-put":
        payload = json.loads(sys.stdin.read())
        store.put_outbox(
            ref_id=str(payload["ref_id"]),
            text=str(payload["text"]),
            fire_at=str(payload["fire_at"]),
            synced_at=str(payload.get("synced_at") or iso_shanghai()),
        )
        print(json.dumps({"ok": True}))
        return 0

    if args.cmd == "outbox-withdraw":
        cnt = store.withdraw_outbox(args.ref_id)
        print(json.dumps({"ok": True, "withdrawn": cnt}))
        return 0

    if args.cmd == "deadman-check":
        res = store.run_deadman_check(silence_limit_sec=args.silence_sec)
        print(json.dumps(res, ensure_ascii=False))
        return 0

    if args.cmd == "receipt-token":
        if getattr(args, "rotate", False) and store.receipt_token_path.exists():
            store.receipt_token_path.unlink(missing_ok=True)
        tok = store.ensure_receipt_token()
        print(json.dumps({"ok": True, "receipt_token": tok}))
        return 0

    if args.cmd == "pull-otp":
        items = store.pull_ephemeral_otps(consume_id=int(getattr(args, "consume_id", 0) or 0))
        print(json.dumps({"ok": True, "items": items}, ensure_ascii=False))
        return 0

    if args.cmd == "otp-mode":
        if getattr(args, "enable", False):
            store.set_otp_vault_enabled(True)
        elif getattr(args, "disable", False):
            store.set_otp_vault_enabled(False)
        print(json.dumps({"ok": True, "allow_platform_otp": store.is_otp_vault_enabled()}))
        return 0

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
