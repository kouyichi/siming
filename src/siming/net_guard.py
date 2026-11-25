"""防 DNS Rebinding 与内网 SSRF 的受控公网 HTTP(S) 连接工具：
先解析并校验全部候选 IP 为全局公网地址，再直接用已校验 IP 建连（TLS SNI + Host 头保留原域名），
彻底阻断二次 DNS 解析投毒到 127.0.0.1 / 内网端口的攻击面。
"""

from __future__ import annotations

import http.client
import ipaddress
import socket
import ssl
import urllib.parse
from typing import Optional


def resolve_verified_public_ip(url: str, require_https: bool = False) -> Optional[tuple[str, str, int, str, str]]:
    """校验 URL 并返回 (scheme, hostname, port, verified_public_ip, path_query)；不合规返回 None。"""
    parsed = urllib.parse.urlsplit(url)
    scheme = (parsed.scheme or "").lower()
    if require_https and scheme != "https":
        return None
    if scheme not in ("http", "https") or not parsed.hostname:
        return None
    host = parsed.hostname.strip().lower()
    if host in ("localhost", "localhost.localdomain"):
        return None
    port = parsed.port or (443 if scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return None
    if not infos:
        return None

    chosen_ip = ""
    for info in infos:
        ip_str = str(info[4][0])
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            return None
        if not ip_obj.is_global:
            return None
        if not chosen_ip:
            chosen_ip = ip_str

    path_q = parsed.path or "/"
    if parsed.query:
        path_q = f"{path_q}?{parsed.query}"
    return (scheme, host, port, chosen_ip, path_q)


class _PinnedHTTPConnection(http.client.HTTPConnection):
    def __init__(self, pinned_ip: str, host: str, port: int, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout)
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        self.sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, pinned_ip: str, host: str, port: int, timeout: float) -> None:
        super().__init__(host, port=port, timeout=timeout, context=ssl.create_default_context())
        self._pinned_ip = pinned_ip

    def connect(self) -> None:
        raw_sock = socket.create_connection((self._pinned_ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(raw_sock, server_hostname=self.host)


def safe_pinned_public_get(
    url: str,
    *,
    require_https: bool = False,
    max_hops: int = 4,
    timeout_sec: float = 8.0,
    max_bytes: int = 15 * 1024 * 1024,
    status_only: bool = False,
) -> tuple[int, bytes, str]:
    """逐跳固定已验证公网 IP 发起 GET，返回 (status_code, body_bytes, reason)。"""
    cur_url = url
    for _hop in range(max_hops):
        resolved = resolve_verified_public_ip(cur_url, require_https=require_https)
        if resolved is None:
            return (0, b"", "blocked_non_public_or_invalid_url")
        scheme, host, port, pinned_ip, path_q = resolved
        host_hdr = host if port in (80, 443) else f"{host}:{port}"
        conn: http.client.HTTPConnection = (
            _PinnedHTTPSConnection(pinned_ip, host, port, timeout_sec)
            if scheme == "https"
            else _PinnedHTTPConnection(pinned_ip, host, port, timeout_sec)
        )
        try:
            conn.request(
                "GET",
                path_q,
                headers={"Host": host_hdr, "User-Agent": "siming-local/2.0", "Connection": "close"},
            )
            resp = conn.getresponse()
            code = int(resp.status)
            if code in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location") or ""
                if not loc:
                    return (code, b"", "redirect_missing_location")
                cur_url = urllib.parse.urljoin(cur_url, loc)
                continue
            if status_only:
                return (code, b"", f"http_{code}")
            chunks: list[bytes] = []
            total = 0
            while True:
                buf = resp.read(65536)
                if not buf:
                    break
                total += len(buf)
                if total > max_bytes:
                    return (code, b"", "exceeded_max_bytes")
                chunks.append(buf)
            return (code, b"".join(chunks), "ok")
        except Exception as exc:
            return (0, b"", str(exc)[:160])
        finally:
            conn.close()
    return (0, b"", "too_many_redirects")
