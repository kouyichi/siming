"""本机 Chromium CDP (9222) 纯标准库后台无感桥接与合规验证交接护栏：
1. 自动识别真正的主 Chromium 监听端（127.0.0.1:9222 或 [::1]:9222），严防误连抢占端口的 aTrust Electron；
2. 强制后台标签页（Target.createTarget background=True），永不抢占 Hyprland 焦点、永不弹窗；
3. 合规铁律：一旦检测到滑块验证、短信二次核验、未登录或扫码支付页，立即原样冻结标签页交由用户本人操作，绝不尝试模拟拖拽或绕过验证码。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import socket
import struct
import time
import urllib.parse
from typing import Any, Optional

import httpx

_CAPTCHA_OR_AUTH_URL_RE = re.compile(
    r"(passport\.meituan\.com|verify\.meituan\.com|yoda|captcha|sec\.meituan|/login\b|/signin\b|auth\.nousresearch)",
    re.IGNORECASE,
)
_CAPTCHA_TEXT_RE = re.compile(
    r"(滑动验证|安全验证|请拖动滑块|人机验证|点击完成验证|访问过于频繁|请输入验证码|请先登录|扫码登录|账号登录)"
)
_PAYMENT_GATE_RE = re.compile(r"(微信支付|支付宝付款|收银台|确认支付|扫码付款|立即支付)")


def detect_compliance_gate(url: str, title: str = "", body_text: str = "") -> Optional[dict[str, str]]:
    """检测页面是否处于登录、验证码/滑块风控或收银台支付门禁；命中时必须停手交由本人处理。"""
    u = (url or "").strip()
    hay = f"{title}\n{body_text[:1500]}"
    if _CAPTCHA_OR_AUTH_URL_RE.search(u) or _CAPTCHA_TEXT_RE.search(hay):
        is_login = any(k in (u.lower() + " " + hay) for k in ("passport", "login", "signin", "请先登录", "扫码登录", "账号登录"))
        return {
            "gate_kind": "login_required" if is_login else "captcha_verification",
            "reason": "页面需要本人登录授权" if is_login else "页面触发平台安全验证（滑块/人机核验），已合规停手保留现场",
            "url": u,
        }
    if _PAYMENT_GATE_RE.search(hay):
        return {
            "gate_kind": "payment_checkout",
            "reason": "已抵达收银台付款确认页，等待本人在浏览器或手机端确认支付",
            "url": u,
        }
    return None


def resolve_chromium_cdp_base(port: int = 9222, timeout: float = 1.2) -> Optional[dict[str, str]]:
    """探测本机 9222 端口并返回真正属于主 Chromium（排除 aTrust/Electron）的 HTTP/WS 基础信息。"""
    if os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return None

    for host in ("127.0.0.1", "[::1]"):
        base = f"http://{host}:{port}"
        try:
            with httpx.Client(trust_env=False, timeout=timeout) as client:
                resp = client.get(f"{base}/json/version")
                if resp.status_code != 200:
                    continue
                info = resp.json()
        except Exception:
            continue
        ua = str(info.get("User-Agent") or "").lower()
        browser = str(info.get("Browser") or "")
        if "atrust" in ua or "electron" in ua:
            continue
        if browser.startswith(("Chrome/", "HeadlessChrome/")) or "chrome" in ua:
            return {
                "http_base": base,
                "ws_browser": str(info.get("webSocketDebuggerUrl") or ""),
                "browser": browser,
            }
    return None


def list_chromium_tabs(port: int = 9222) -> list[dict[str, Any]]:
    base_info = resolve_chromium_cdp_base(port=port)
    if not base_info:
        return []
    try:
        with httpx.Client(trust_env=False, timeout=1.5) as client:
            resp = client.get(f"{base_info['http_base']}/json/list")
            if resp.status_code != 200:
                return []
            data = resp.json()
            return [t for t in data if isinstance(t, dict) and t.get("type") == "page"] if isinstance(data, list) else []
    except Exception:
        return []


def has_domain_session_cookies(
    domain_suffix: str,
    cookie_names: tuple[str, ...] = ("token", "userTicket", "mt_c_token"),
    port: int = 9222,
) -> bool:
    """只读通过浏览器级 CDP Storage.getCookies 检查目标域名是否存在有效登录态 Cookie（零开标签、零焦点干扰）。"""
    base_info = resolve_chromium_cdp_base(port=port)
    if not base_info or not base_info.get("ws_browser"):
        return False
    try:
        with LocalWsCdpClient(base_info["ws_browser"], timeout=3.0) as b_ws:
            res = b_ws.call("Storage.getCookies", {})
        now_ts = time.time()
        want = {n.lower() for n in cookie_names}
        suffix = domain_suffix.lower().lstrip(".")
        for c in res.get("cookies") or []:
            if not isinstance(c, dict):
                continue
            dom = str(c.get("domain") or "").lower().lstrip(".")
            if not (dom == suffix or dom.endswith("." + suffix)):
                continue
            name = str(c.get("name") or "").lower()
            val = str(c.get("value") or "").strip()
            exp = float(c.get("expires") or -1)
            if name in want and val and (exp <= 0 or exp > now_ts):
                return True
    except Exception:
        return False
    return False


class LocalWsCdpClient:
    """零第三方依赖的本机回环 RFC 6455 WebSocket CDP 客户端（仅允许连接 127.0.0.1 / ::1）。"""

    def __init__(self, ws_url: str, timeout: float = 6.0) -> None:
        parsed = urllib.parse.urlsplit(ws_url)
        host = (parsed.hostname or "").strip().lower()
        if host not in ("127.0.0.1", "::1", "localhost"):
            raise ValueError(f"CDP WebSocket 仅允许连接本机回环地址，拒绝: {host}")
        self.host = host
        self.port = parsed.port or 9222
        self.path = parsed.path + (f"?{parsed.query}" if parsed.query else "")
        self.timeout = timeout
        self.sock: Optional[socket.socket] = None
        self._buf = bytearray()
        self._msg_id = 0

    def __enter__(self) -> "LocalWsCdpClient":
        family = socket.AF_INET6 if ":" in self.host else socket.AF_INET
        s = socket.socket(family, socket.SOCK_STREAM)
        s.settimeout(self.timeout)
        s.connect((self.host, self.port))
        key_raw = os.urandom(16)
        sec_key = base64.b64encode(key_raw).decode("ascii")
        host_hdr = f"[{self.host}]:{self.port}" if ":" in self.host else f"{self.host}:{self.port}"
        req = (
            f"GET {self.path} HTTP/1.1\r\n"
            f"Host: {host_hdr}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {sec_key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        s.sendall(req.encode("ascii"))
        hdr_buf = bytearray()
        while b"\r\n\r\n" not in hdr_buf:
            chunk = s.recv(4096)
            if not chunk:
                s.close()
                raise RuntimeError("CDP WebSocket 握手意外关闭")
            hdr_buf.extend(chunk)
        idx = hdr_buf.index(b"\r\n\r\n") + 4
        hdr_text = bytes(hdr_buf[:idx]).decode("latin-1", errors="replace")
        self._buf.extend(hdr_buf[idx:])
        if " 101 " not in hdr_text.splitlines()[0]:
            s.close()
            raise RuntimeError(f"CDP WebSocket 握手失败: {hdr_text.splitlines()[0][:120]}")
        expected_accept = base64.b64encode(
            hashlib.sha1((sec_key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
        ).decode("ascii")
        if expected_accept not in hdr_text:
            s.close()
            raise RuntimeError("CDP WebSocket Sec-WebSocket-Accept 校验不匹配")
        self.sock = s
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _read_exact(self, n: int) -> bytes:
        assert self.sock is not None
        while len(self._buf) < n:
            chunk = self.sock.recv(16384)
            if not chunk:
                raise RuntimeError("CDP WebSocket 读取中断")
            self._buf.extend(chunk)
        out = bytes(self._buf[:n])
        del self._buf[:n]
        return out

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        assert self.sock is not None
        mask_key = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | (opcode & 0x0F)])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        header.extend(mask_key)
        masked = bytes(b ^ mask_key[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def _recv_text_message(self) -> str:
        fragments: list[bytes] = []
        while True:
            b0, b1 = self._read_exact(2)
            fin = bool(b0 & 0x80)
            opcode = b0 & 0x0F
            masked = bool(b1 & 0x80)
            length = b1 & 0x7F
            if length == 126:
                length = struct.unpack("!H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", self._read_exact(8))[0]
            mask_key = self._read_exact(4) if masked else b""
            data = self._read_exact(length)
            if masked:
                data = bytes(b ^ mask_key[i % 4] for i, b in enumerate(data))
            if opcode == 0x8:
                raise RuntimeError("CDP WebSocket 对端关闭")
            if opcode == 0x9:
                self._send_frame(0xA, data)
                continue
            if opcode == 0xA:
                continue
            fragments.append(data)
            if fin:
                return b"".join(fragments).decode("utf-8", errors="replace")

    def call(self, method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        self._msg_id += 1
        req_id = self._msg_id
        msg = json.dumps({"id": req_id, "method": method, "params": params or {}}, ensure_ascii=False).encode("utf-8")
        self._send_frame(0x1, msg)
        deadline = time.monotonic() + self.timeout
        while time.monotonic() < deadline:
            raw = self._recv_text_message()
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            if isinstance(obj, dict) and obj.get("id") == req_id:
                if "error" in obj:
                    raise RuntimeError(f"CDP {method} 报错: {obj['error']}")
                return obj.get("result") or {}
        raise TimeoutError(f"CDP {method} 响应超时 ({self.timeout}s)")


def inspect_url_in_background_tab(
    url: str,
    js_expr: str,
    port: int = 9222,
    wait_sec: float = 2.2,
    close_when_clean: bool = True,
) -> dict[str, Any]:
    """在主 Chromium 后台静默开标签加载 URL 并只读执行 JS：
    - 若检测到验证码/登录门禁/支付页，保留后台标签供用户接力并原样返回 gate 信息；
    - 正常读取完毕后按 close_when_clean 自动清理后台临时标签，零焦点干扰。
    """
    base_info = resolve_chromium_cdp_base(port=port)
    if not base_info or not base_info.get("ws_browser"):
        return {"ok": False, "reason": "cdp_unavailable", "error": "本机主 Chromium (9222) 未就绪"}

    target_id = ""
    try:
        with LocalWsCdpClient(base_info["ws_browser"], timeout=6.0) as b_ws:
            created = b_ws.call("Target.createTarget", {"url": url, "background": True})
            target_id = str(created.get("targetId") or "")
        if not target_id:
            return {"ok": False, "reason": "create_target_failed", "error": "无法创建后台标签页"}

        if wait_sec > 0:
            time.sleep(min(wait_sec, 4.5))

        tabs = list_chromium_tabs(port=port)
        tab_info = next((t for t in tabs if str(t.get("id") or "") == target_id), None)
        page_ws_url = str((tab_info or {}).get("webSocketDebuggerUrl") or "")
        if not page_ws_url:
            ws_host_base = base_info["ws_browser"].split("/devtools/")[0]
            page_ws_url = f"{ws_host_base}/devtools/page/{target_id}"

        with LocalWsCdpClient(page_ws_url, timeout=6.0) as p_ws:
            meta_eval = p_ws.call(
                "Runtime.evaluate",
                {
                    "expression": "JSON.stringify({url: location.href, title: document.title || '', text: (document.body && document.body.innerText || '').slice(0, 2500)})",
                    "returnByValue": True,
                },
            )
            meta_str = str(((meta_eval.get("result") or {}).get("value")) or "{}")
            page_meta = json.loads(meta_str) if meta_str.startswith("{") else {}
            gate = detect_compliance_gate(
                str(page_meta.get("url") or url),
                str(page_meta.get("title") or ""),
                str(page_meta.get("text") or ""),
            )
            if gate is not None:
                return {
                    "ok": False,
                    "gated": True,
                    "gate": gate,
                    "target_id": target_id,
                    "kept_tab_open": True,
                }

            val_eval = p_ws.call(
                "Runtime.evaluate",
                {
                    "expression": js_expr,
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
            raw_val = (val_eval.get("result") or {}).get("value")

        if close_when_clean and target_id:
            with LocalWsCdpClient(base_info["ws_browser"], timeout=4.0) as b_ws:
                b_ws.call("Target.closeTarget", {"targetId": target_id})

        return {
            "ok": True,
            "gated": False,
            "url": str(page_meta.get("url") or url),
            "title": str(page_meta.get("title") or ""),
            "data": raw_val,
            "target_id": "" if close_when_clean else target_id,
        }
    except Exception as exc:
        if close_when_clean and target_id:
            try:
                with LocalWsCdpClient(base_info["ws_browser"], timeout=2.5) as b_ws:
                    b_ws.call("Target.closeTarget", {"targetId": target_id})
            except Exception:
                pass
        return {"ok": False, "reason": "cdp_error", "error": str(exc)[:220]}
