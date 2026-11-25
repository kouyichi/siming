#!/usr/bin/env python3
"""一键完成本机 Omamail + Hermes + 司命 Gmail OAuth 重授权（监听 127.0.0.1:9482，与 Omamail 桌面 9481 互不冲突）。"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import httpx

SCOPES = [
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.send",
    "https://www.googleapis.com/auth/calendar.events",
    "https://www.googleapis.com/auth/calendar.calendarlist.readonly",
]
PORT = 9482
CALLBACK_PATH = "/oauth2callback"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def main() -> int:
    home = Path.home()
    acc_path = home / ".config/omamail/accounts.json"
    accs = json.loads(acc_path.read_text(encoding="utf-8")).get("accounts") or []
    g_acc = next((a for a in accs if a.get("provider") == "gmail"), None)
    if not g_acc:
        sys.stderr.write("未在 ~/.config/omamail/accounts.json 找到 Gmail 账号\n")
        return 1

    account_id = str(g_acc.get("id") or g_acc.get("email"))
    email_addr = str(g_acc.get("email") or account_id)
    client_id = str(g_acc.get("clientId") or "")
    client_secret = str(g_acc.get("clientSecret") or "")

    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode("ascii")).digest())
    state = _b64url(secrets.token_bytes(18))
    redirect_uri = f"http://127.0.0.1:{PORT}{CALLBACK_PATH}"

    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": " ".join(SCOPES),
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "state": state,
            "access_type": "offline",
            "include_granted_scopes": "true",
            "prompt": "consent",
            "login_hint": email_addr,
        }
    )

    url_file = home / ".cache/agent-tmp/siming/gmail-reauth-url.txt"
    url_file.parent.mkdir(parents=True, exist_ok=True)
    url_file.write_text(auth_url + "\n", encoding="utf-8")
    os.chmod(url_file, 0o600)
    print(auth_url, flush=True)

    done = {"finished": False}

    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: object) -> None:
            return

        def _reply_html(self, code: int, title: str, msg: str) -> None:
            doc = (
                f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>{title}</title>"
                "<style>body{font:15px/1.6 -apple-system,'PingFang SC',sans-serif;background:#f6f5f2;color:#111;padding:40px 18px}"
                ".card{max-width:520px;margin:0 auto;background:#fff;border:1px solid #e2e0d9;border-radius:10px;padding:22px}</style>"
                f"</head><body><div class='card'><h2>{title}</h2><p>{msg}</p></div></body></html>"
            ).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(doc)))
            self.end_headers()
            self.wfile.write(doc)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            if parsed.path != CALLBACK_PATH:
                self._reply_html(404, "路径不匹配", "请从授权链接进入。")
                return
            qs = urllib.parse.parse_qs(parsed.query)
            if (qs.get("state") or [""])[0] != state:
                self._reply_html(400, "State 校验失败", "请使用最新生成的授权链接。")
                return
            if "error" in qs:
                self._reply_html(400, "授权已取消", str(qs["error"][0]))
                done["finished"] = True
                return
            auth_code = (qs.get("code") or [""])[0]
            if not auth_code:
                self._reply_html(400, "缺少授权码", "回调未携带 code。")
                return

            try:
                with httpx.Client(proxy="http://127.0.0.1:7890", timeout=15.0) as client:
                    tr = client.post(
                        "https://oauth2.googleapis.com/token",
                        data={
                            "client_id": client_id,
                            "client_secret": client_secret,
                            "code": auth_code,
                            "code_verifier": verifier,
                            "grant_type": "authorization_code",
                            "redirect_uri": redirect_uri,
                        },
                    )
                    tok_data = tr.json()
            except Exception as exc:
                self._reply_html(500, "换取 Token 网络失败", f"请确认 127.0.0.1:7890 代理开启：{exc}")
                return

            refresh_tok = str(tok_data.get("refresh_token") or "")
            access_tok = str(tok_data.get("access_token") or "")
            if not refresh_tok:
                self._reply_html(400, "未拿到 Refresh Token", str(tok_data.get("error_description") or tok_data.get("error") or "unknown"))
                return

            # 1. 写入 Omamail GNOME Keyring
            om_bin = home / ".local/share/omamail/bin/omamail"
            subprocess.run(
                [str(om_bin), "call", "credentials.put", "--json"],
                input=json.dumps(
                    {
                        "kind": "google-refresh-token",
                        "accountId": account_id,
                        "clientId": client_id,
                        "secret": refresh_tok,
                    }
                ),
                capture_output=True,
                text=True,
                timeout=10,
            )

            # 2. 同步更新 ~/.hermes/google_token.json（先备份，保持 0600）
            hermes_tok = home / ".hermes/google_token.json"
            if hermes_tok.is_file():
                ts_tag = datetime.now().strftime("%Y%m%d")
                bak = home / f".hermes/google_token.json.bak-{ts_tag}"
                if not bak.exists():
                    shutil.copy2(hermes_tok, bak)
                try:
                    cur = json.loads(hermes_tok.read_text(encoding="utf-8"))
                except Exception:
                    cur = {}
                cur.update(
                    {
                        "token": access_tok,
                        "refresh_token": refresh_tok,
                        "token_uri": "https://oauth2.googleapis.com/token",
                        "client_id": client_id,
                        "client_secret": client_secret,
                        "scopes": str(tok_data.get("scope") or " ".join(SCOPES)).split(),
                        "expiry": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    }
                )
                fd = os.open(str(hermes_tok), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                try:
                    os.write(fd, (json.dumps(cur, indent=2) + "\n").encode("utf-8"))
                finally:
                    os.close(fd)

            # 3. 重启 omamail serve 让桌面端当场加载新 Token，并触发一次司命邮箱同步
            subprocess.run(["pkill", "-x", "omamail"], capture_output=True)
            subprocess.Popen(
                [str(home / ".local/bin/siming"), "mail-sync"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            self._reply_html(
                200,
                "Gmail 重新登录成功",
                f"账号 <code>{email_addr}</code> 的新授权已同步写入 Omamail 密钥环、Hermes 与司命后台，现在可以直接关闭此标签页。",
            )
            done["finished"] = True

    server = HTTPServer(("127.0.0.1", PORT), CallbackHandler)
    server.timeout = 10.0
    deadline = datetime.now().timestamp() + 900  # 15 分钟自动退出，不常驻占端口
    try:
        while not done["finished"] and datetime.now().timestamp() < deadline:
            server.handle_request()
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
