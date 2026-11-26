"""P4 共享受控 MCP JSON-RPC 传输层（mcp_transport.py）：消除 didi_mcp / luckin_mcp 中重复的 rpc / initialize / call_tool 样板。"""

from __future__ import annotations

import json
import urllib.request
from typing import Any, Callable, Optional


def mcp_rpc_request(
    endpoint: str,
    method: str,
    params: Optional[dict[str, Any]] = None,
    *,
    headers: Optional[dict[str, str]] = None,
    req_id: int = 1,
    timeout: float = 20.0,
    opener: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    body = json.dumps(
        {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params or {}},
        ensure_ascii=False,
    ).encode("utf-8")
    req_headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(endpoint, data=body, headers=req_headers, method="POST")
    open_fn = opener or urllib.request.urlopen
    with open_fn(req, timeout=timeout) as resp:
        raw = resp.read().decode("utf-8", errors="replace").strip()
    if raw.startswith("data:"):
        for line in reversed(raw.splitlines()):
            if line.startswith("data:"):
                raw = line[5:].strip()
                break
    return json.loads(raw) if raw else {}


def mcp_initialize(
    endpoint: str,
    *,
    headers: Optional[dict[str, str]] = None,
    client_name: str = "siming",
    timeout: float = 15.0,
    opener: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    return mcp_rpc_request(
        endpoint,
        "initialize",
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": client_name, "version": "6.2"},
        },
        headers=headers,
        req_id=1,
        timeout=timeout,
        opener=opener,
    )


def mcp_call_tool(
    endpoint: str,
    tool_name: str,
    arguments: Optional[dict[str, Any]] = None,
    *,
    headers: Optional[dict[str, str]] = None,
    timeout: float = 25.0,
    opener: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    return mcp_rpc_request(
        endpoint,
        "tools/call",
        {"name": tool_name, "arguments": arguments or {}},
        headers=headers,
        req_id=2,
        timeout=timeout,
        opener=opener,
    )
