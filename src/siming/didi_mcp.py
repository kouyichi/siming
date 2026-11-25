"""滴滴官方 MCP Server（https://mcp.didichuxing.com）原生接入与代叫车闭环：
- 登录方式：官方 API / MCP Key（无需脆弱网页爬虫，个人手机号在 https://mcp.didichuxing.com 验证码登录领 Key）；
- 执行链路：maps_textsearch 经纬度解析 -> taxi_estimate 实时车型与价格预估 -> 生成 15m 有效 `apply Axx` 确认口令 -> 本人回「确认」后直接调 taxi_create_order 替你呼叫司机（Beta 账号自动回退官方唤端链接）；
- 支持后续「打车状态」（taxi_query_order）与「取消打车」（taxi_cancel_order）。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

import httpx

from siming.approval import create_approval
from siming.cn_calendar import day_info
from siming.config import load_policy
from siming.crypto import read_secret, write_secret_file
from siming.db import SimingDB, iso_now, now_local
from siming.ledger.reimburse import build_didi_deeplink, resolve_place_alias

DIDI_SECRET_NAME = "didi-mcp-key"
DIDI_PROD_ENDPOINT = "https://mcp.didichuxing.com/mcp-servers"
DIDI_SANDBOX_ENDPOINT = "https://mcp.didichuxing.com/mcp-servers-sandbox"


def save_didi_mcp_key(db: SimingDB, key: str) -> dict[str, Any]:
    clean = (key or "").strip()
    if len(clean) < 8:
        return {"ok": False, "error": "滴滴 MCP Key 长度不合法"}
    target = db.paths.secrets_dir / DIDI_SECRET_NAME
    write_secret_file(target, clean)
    db.write_audit("A1", "save_didi_mcp_key", {"configured": True})
    return {
        "ok": True,
        "summary": "✅ 已安全保存滴滴官方 MCP Key（`0600` 权限）。现在在 QQ 直接发 `打车去<地点>` 即可实时查车价并替你发单叫车！",
    }


def get_didi_mcp_key(db: SimingDB) -> Optional[str]:
    return read_secret(DIDI_SECRET_NAME, db.paths)


class DidiMcpSession:
    """MCP Streamable HTTP (JSON-RPC 2.0) 轻量客户端（兼容 application/json 与 text/event-stream）。"""

    def __init__(self, base_url: str, api_key: str, timeout: float = 15.0) -> None:
        self.url = f"{base_url}?key={api_key}"
        self.timeout = timeout
        self.session_id: str = ""
        self._req_id = 0

    def _parse_mcp_response(self, content_type: str, body_text: str) -> dict[str, Any]:
        ct = (content_type or "").lower()
        if "text/event-stream" in ct or body_text.lstrip().startswith("event:") or body_text.lstrip().startswith("data:"):
            last_json: dict[str, Any] = {}
            for line in body_text.splitlines():
                s = line.strip()
                if s.startswith("data:"):
                    payload = s[5:].strip()
                    if payload.startswith("{"):
                        try:
                            last_json = json.loads(payload)
                        except Exception:
                            continue
            return last_json
        return json.loads(body_text or "{}")

    def rpc(self, method: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        self._req_id += 1
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        payload: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": self._req_id,
            "method": method,
        }
        if params is not None:
            payload["params"] = params

        # 国内滴滴直连优先（不走海外 HTTP 代理）
        with httpx.Client(trust_env=False, timeout=self.timeout) as client:
            resp = client.post(self.url, json=payload, headers=headers)
            sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            resp.raise_for_status()
            parsed = self._parse_mcp_response(resp.headers.get("content-type", ""), resp.text)

        if "error" in parsed:
            err_obj = parsed["error"]
            msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
            raise RuntimeError(f"滴滴 MCP 返回错误: {msg}")
        return parsed.get("result") or {}

    def initialize_and_list_tools(self) -> list[dict[str, Any]]:
        self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "siming-personal-agent", "version": "0.3.0"},
            },
        )
        t_res = self.rpc("tools/list", {})
        return list(t_res.get("tools") or [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        res = self.rpc("tools/call", {"name": name, "arguments": arguments})
        # 提取 MCP ToolResult 的 content 文本并尝试 JSON 解析
        contents = res.get("content") or []
        texts = [str(c.get("text") or "") for c in contents if isinstance(c, dict) and c.get("text")]
        joined = "\n".join(texts).strip()
        if joined.startswith("{") or joined.startswith("["):
            try:
                return json.loads(joined)
            except Exception:
                return joined
        return res.get("structuredContent") or joined


def _extract_first_poi(poi_res: Any, fallback_name: str) -> Optional[dict[str, Any]]:
    """兼容滴滴 maps_textsearch 返回的各种 POI JSON/文本结构，提取首个经纬度与名称。"""
    if isinstance(poi_res, dict):
        pois = poi_res.get("pois") or poi_res.get("data") or poi_res.get("results") or []
        if isinstance(pois, dict):
            pois = pois.get("pois") or pois.get("list") or []
        if isinstance(pois, list) and pois:
            p0 = pois[0]
            if isinstance(p0, dict):
                loc = str(p0.get("location") or "")
                lng = p0.get("lng") or p0.get("longitude")
                lat = p0.get("lat") or p0.get("latitude")
                if (lng is None or lat is None) and "," in loc:
                    parts = loc.split(",")
                    lng, lat = float(parts[0]), float(parts[1])
                if lng is not None and lat is not None:
                    return {
                        "name": str(p0.get("name") or fallback_name),
                        "address": str(p0.get("address") or ""),
                        "lng": float(lng),
                        "lat": float(lat),
                    }
    if isinstance(poi_res, str):
        m_loc = re.search(r"(\d{2,3}\.\d{4,10})\s*,\s*(\d{2}\.\d{4,10})", poi_res)
        if m_loc:
            return {"name": fallback_name, "address": "", "lng": float(m_loc.group(1)), "lat": float(m_loc.group(2))}
    return None


def _build_args_by_schema(schema: dict[str, Any], candidates: dict[str, Any]) -> dict[str, Any]:
    """根据滴滴 MCP 工具实时返回的 JSON Schema 属性名自适应填充参数，防字段命名细微演进导致调用失败。"""
    props = (schema.get("properties") if isinstance(schema, dict) else None) or {}
    if not props:
        return {k: v for k, v in candidates.items() if v is not None}
    out: dict[str, Any] = {}
    for prop_name in props:
        low = prop_name.lower()
        if prop_name in candidates and candidates[prop_name] is not None:
            out[prop_name] = candidates[prop_name]
        elif low in ("keywords", "keyword", "query") and "keywords" in candidates:
            out[prop_name] = candidates["keywords"]
        elif low in ("city", "region") and "city" in candidates:
            out[prop_name] = candidates["city"]
        elif low in ("origin", "from_location", "start_location", "departure") and "origin_coord" in candidates:
            out[prop_name] = candidates["origin_coord"]
        elif low in ("destination", "to_location", "end_location", "arrival") and "dest_coord" in candidates:
            out[prop_name] = candidates["dest_coord"]
        elif low in ("from_lng", "start_lng", "flat_lng", "origin_lng") and "from_lng" in candidates:
            out[prop_name] = candidates["from_lng"]
        elif low in ("from_lat", "start_lat", "flat_lat", "origin_lat") and "from_lat" in candidates:
            out[prop_name] = candidates["from_lat"]
        elif low in ("to_lng", "end_lng", "tlng", "dest_lng") and "to_lng" in candidates:
            out[prop_name] = candidates["to_lng"]
        elif low in ("to_lat", "end_lat", "tlat", "dest_lat") and "to_lat" in candidates:
            out[prop_name] = candidates["to_lat"]
        elif low in ("from_name", "start_name", "origin_name") and "from_name" in candidates:
            out[prop_name] = candidates["from_name"]
        elif low in ("to_name", "end_name", "dest_name", "destination_name") and "to_name" in candidates:
            out[prop_name] = candidates["to_name"]
        elif low in ("trace_id", "traceid") and "trace_id" in candidates:
            out[prop_name] = candidates["trace_id"]
        elif low in ("product_id", "productid", "car_level", "require_level") and "product_id" in candidates:
            out[prop_name] = candidates["product_id"]
        elif low in ("order_id", "orderid", "oid") and "order_id" in candidates:
            out[prop_name] = candidates["order_id"]
    return out


def _extract_estimates(est_res: Any) -> tuple[str, list[dict[str, Any]]]:
    """提取 traceId 与车型报价列表。"""
    trace_id = ""
    quotes: list[dict[str, Any]] = []
    if isinstance(est_res, dict):
        trace_id = str(est_res.get("traceId") or est_res.get("trace_id") or (est_res.get("data") or {}).get("traceId") or "")
        raw_list = (
            est_res.get("prices")
            or est_res.get("estimates")
            or est_res.get("products")
            or (est_res.get("data") or {}).get("prices")
            or (est_res.get("data") or {}).get("products")
            or []
        )
        for item in raw_list if isinstance(raw_list, list) else []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("car_type") or item.get("product_name") or "快车")
            pid = item.get("product_id") or item.get("productId") or item.get("require_level") or ""
            price = item.get("price") or item.get("estimate_price") or item.get("fee") or ""
            # 若滴滴以“分”返回大整数（如 2850 且无小数点），归一显示；若已是元（如 28.5 或 "28.5元"）直接展示
            if isinstance(price, int) and price >= 300:
                price_str = f"¥{price / 100:.1f}"
            elif isinstance(price, (int, float)):
                price_str = f"¥{price}"
            else:
                price_str = str(price) if str(price).startswith("¥") else f"¥{price}"
            quotes.append({"name": name, "product_id": pid, "price_display": price_str, "raw": item})
    return trace_id, quotes


def request_didi_ride(
    db: SimingDB,
    dest_raw: str,
    origin_raw: str = "",
    city: str = "北京",
    sandbox: bool = False,
    mcp_factory: Optional[Callable[[str, str], Any]] = None,
) -> dict[str, Any]:
    """发起滴滴打车准备：支持自动融合手机 Wi-Fi / GPS 实时位置判定起点，已配 MCP Key 时自动估价并生成 `apply Axx` 一键代叫车确认单！"""
    from siming.context_loc import resolve_current_location_context

    db.init_db()
    policy = load_policy(db.paths)
    tail_exempt = bool((policy.get("vehicle") or {}).get("tail_limit_exempt", False))
    cal = day_info(now_local().date(), db.paths)
    cal_hdr = cal.personal_header(tail_limit_exempt=tail_exempt)

    dest_clean = "家" if dest_raw.strip() in ("回家", "家里") else dest_raw.strip()
    dest_name = resolve_place_alias(db, dest_clean)

    loc_note = ""
    preset_orig_gps: Optional[dict[str, Any]] = None
    if origin_raw and origin_raw not in ("auto", "当前位置"):
        origin_name = resolve_place_alias(db, origin_raw)
    else:
        loc_ctx = resolve_current_location_context(db)
        if loc_ctx.get("has_fresh") and str(loc_ctx.get("address") or "") and str(loc_ctx.get("address")) != dest_name:
            origin_name = str(loc_ctx["address"])
            loc_note = f"\n• {loc_ctx['badge']}"
            if loc_ctx.get("lat") is not None and loc_ctx.get("lng") is not None:
                preset_orig_gps = {
                    "name": origin_name,
                    "lng": float(loc_ctx["lng"]),
                    "lat": float(loc_ctx["lat"]),
                }
        else:
            origin_name = resolve_place_alias(db, "家")

    fallback_link = build_didi_deeplink(origin_name, dest_name)

    mcp_key = get_didi_mcp_key(db)
    if not mcp_key:
        msg = (
            f"🚕 滴滴路线已就绪：{origin_name} → {dest_name}（{cal_hdr}）{loc_note}\n"
            f"• 唤起 App 链接：{fallback_link}\n"
            "💡 **想让我直接替你查实时车价并发单叫车**：用手机号打开滴滴官方 MCP 平台 https://mcp.didichuxing.com 领一个个人 Key，在 QQ 发 `滴滴密钥 你的Key`（或 `siming didi --set-key <Key>`）即可开通 API 代叫车！"
        )
        return {"ok": True, "mode": "deeplink_unconfigured", "origin_name": origin_name, "dest_name": dest_name, "reply": msg, "deeplink": fallback_link}

    base_url = DIDI_SANDBOX_ENDPOINT if sandbox else DIDI_PROD_ENDPOINT
    session = mcp_factory(base_url, mcp_key) if mcp_factory else DidiMcpSession(base_url, mcp_key)

    try:
        tools = session.initialize_and_list_tools()
        tool_map = {str(t.get("name")): (t.get("inputSchema") or {}) for t in tools if isinstance(t, dict)}

        search_tool = next((n for n in tool_map if "textsearch" in n or "poi" in n), "")
        est_tool = next((n for n in tool_map if "taxi_estimate" in n or "estimate" in n), "")
        create_tool = next((n for n in tool_map if "taxi_create_order" in n or "create_order" in n), "")
        link_tool = next((n for n in tool_map if "ride_app_link" in n or "app_link" in n), "")

        if not search_tool or not est_tool:
            raise RuntimeError(f"当前 MCP Key 未暴露地图或估价工具（可见工具：{list(tool_map.keys())[:6]}）")

        if preset_orig_gps is not None:
            orig_poi = preset_orig_gps
        else:
            orig_poi_raw = session.call_tool(
                search_tool,
                _build_args_by_schema(tool_map[search_tool], {"keywords": origin_name, "city": city}),
            )
            orig_poi = _extract_first_poi(orig_poi_raw, origin_name)
        dest_poi_raw = session.call_tool(
            search_tool,
            _build_args_by_schema(tool_map[search_tool], {"keywords": dest_name, "city": city}),
        )
        dest_poi = _extract_first_poi(dest_poi_raw, dest_name)
        if not orig_poi or not dest_poi:
            raise RuntimeError(f"未能唯一定位起终点坐标（起点={origin_name}, 终点={dest_name}）")

        cand_vars = {
            "origin_coord": f"{orig_poi['lng']},{orig_poi['lat']}",
            "dest_coord": f"{dest_poi['lng']},{dest_poi['lat']}",
            "from_lng": orig_poi["lng"],
            "from_lat": orig_poi["lat"],
            "to_lng": dest_poi["lng"],
            "to_lat": dest_poi["lat"],
            "from_name": orig_poi["name"],
            "to_name": dest_poi["name"],
            "city": city,
        }
        est_raw = session.call_tool(est_tool, _build_args_by_schema(tool_map[est_tool], cand_vars))
        trace_id, quotes = _extract_estimates(est_raw)

        quote_lines = [f"  {idx}. {q['name']}：预估 **{q['price_display']}**" for idx, q in enumerate(quotes[:4], start=1)]
        if not quote_lines:
            quote_lines = [f"  • 实时返回：{str(est_raw)[:180]}"]

        if create_tool:
            chosen = quotes[0] if quotes else {"name": "快车", "product_id": "", "price_display": "以实计为准"}
            create_vars = {
                **cand_vars,
                "trace_id": trace_id,
                "product_id": chosen["product_id"],
            }
            create_args = _build_args_by_schema(tool_map[create_tool], create_vars)
            ap = create_approval(
                db,
                action="didi_create_order",
                target_id=f"DIDI-{now_local().strftime('%H%M%S')}",
                params={
                    "tool_name": create_tool,
                    "create_args": create_args,
                    "origin_name": orig_poi["name"],
                    "dest_name": dest_poi["name"],
                    "car_name": chosen["name"],
                    "price_display": chosen["price_display"],
                    "sandbox": sandbox,
                },
                tier="A2",
                ttl_minutes=15,
            )
            tok = ap["token"]
            reply = (
                f"🚕 滴滴实时询价已完成：{orig_poi['name']} → {dest_poi['name']}\n"
                + "\n".join(quote_lines)
                + f"\n\n✅ 已锁定首选：**{chosen['name']}（{chosen['price_display']}）**\n"
                f"15 分钟内直接引用本条回「确认」（或发 `apply {tok}`），司命立即经滴滴 API 替你发单叫车！"
            )
            return {
                "ok": True,
                "mode": "pro_ready_to_order",
                "approval_token": tok,
                "quotes": quotes,
                "reply": reply,
            }

        # Beta Key：实时报价成功，附官方深度链接
        app_link = fallback_link
        if link_tool:
            try:
                l_res = session.call_tool(link_tool, _build_args_by_schema(tool_map[link_tool], cand_vars))
                if isinstance(l_res, dict) and l_res.get("url"):
                    app_link = str(l_res["url"])
                elif isinstance(l_res, str) and "http" in l_res:
                    m_u = re.search(r"https?://\S+", l_res)
                    if m_u:
                        app_link = m_u.group(0)
            except Exception:
                pass
        reply = (
            f"🚕 滴滴实时车价：{orig_poi['name']} → {dest_poi['name']}\n"
            + "\n".join(quote_lines)
            + f"\n• 一键呼叫链接（当前 Key 为 Beta 档）：{app_link}"
        )
        return {"ok": True, "mode": "beta_estimate_link", "quotes": quotes, "reply": reply}

    except Exception as exc:
        reply = (
            f"⚠️ 滴滴 MCP 实时询价异常（{str(exc)[:120]}），已自动回退唤端链接：\n"
            f"• 路线：{origin_name} → {dest_name}\n"
            f"• 链接：{fallback_link}"
        )
        return {"ok": False, "mode": "fallback_link", "error": str(exc), "reply": reply}


def execute_didi_create_order(
    params: dict[str, Any],
    db: SimingDB,
    mcp_factory: Optional[Callable[[str, str], Any]] = None,
) -> str:
    """由 approval.py 在用户确认 `apply Axx` 后调用：向滴滴 MCP 发起真实 `taxi_create_order` 呼叫！"""
    mcp_key = get_didi_mcp_key(db)
    if not mcp_key:
        raise RuntimeError("未找到滴滴 MCP Key，无法发单")
    base_url = DIDI_SANDBOX_ENDPOINT if params.get("sandbox") else DIDI_PROD_ENDPOINT
    session = mcp_factory(base_url, mcp_key) if mcp_factory else DidiMcpSession(base_url, mcp_key)
    session.initialize_and_list_tools()
    tool_name = str(params.get("tool_name") or "taxi_create_order")
    res = session.call_tool(tool_name, dict(params.get("create_args") or {}))

    order_id = ""
    if isinstance(res, dict):
        order_id = str(res.get("order_id") or res.get("orderId") or (res.get("data") or {}).get("order_id") or "")
    elif isinstance(res, str):
        m_oid = re.search(r"(?:order_id|订单号)[：:\s\"']*([A-Za-z0-9_-]{6,36})", res)
        if m_oid:
            order_id = m_oid.group(1)

    active_rec = {
        "order_id": order_id,
        "origin": params.get("origin_name"),
        "dest": params.get("dest_name"),
        "car_name": params.get("car_name"),
        "price_display": params.get("price_display"),
        "sandbox": bool(params.get("sandbox")),
        "created_at": iso_now(),
        "raw_response": str(res)[:300],
    }
    db.set_meta("active_didi_order_json", json.dumps(active_rec, ensure_ascii=False))
    db.record_human_use("didi_shortcut", "create_order", ref_id=order_id or "order")
    oid_s = f"（订单号 `{order_id}`）" if order_id else ""
    return (
        f"已通过滴滴官方 API 发起呼叫：{params.get('origin_name')} → {params.get('dest_name')} "
        f"[{params.get('car_name')} {params.get('price_display')}]{oid_s}！\n"
        "随时发 `打车状态` 查司机车牌与位置，或发 `取消打车` 撤单。"
    )


def query_or_cancel_didi_order(
    db: SimingDB,
    cancel: bool = False,
    mcp_factory: Optional[Callable[[str, str], Any]] = None,
) -> dict[str, Any]:
    raw = db.get_meta("active_didi_order_json")
    if not raw:
        return {"ok": False, "reply": "当前没有由司命发起的活跃滴滴订单记录。"}
    rec = json.loads(raw)
    oid = str(rec.get("order_id") or "").strip()
    if not oid:
        return {"ok": False, "reply": f"最近一笔呼叫（{rec.get('origin')}→{rec.get('dest')}）未返回标准订单号，请在滴滴 App 查看。"}

    mcp_key = get_didi_mcp_key(db)
    if not mcp_key:
        return {"ok": False, "reply": "缺少滴滴 MCP Key。"}

    base_url = DIDI_SANDBOX_ENDPOINT if rec.get("sandbox") else DIDI_PROD_ENDPOINT
    session = mcp_factory(base_url, mcp_key) if mcp_factory else DidiMcpSession(base_url, mcp_key)
    tools = session.initialize_and_list_tools()
    tool_map = {str(t.get("name")): (t.get("inputSchema") or {}) for t in tools if isinstance(t, dict)}
    target_kw = "cancel_order" if cancel else "query_order"
    t_name = next((n for n in tool_map if target_kw in n), "")
    if not t_name:
        return {"ok": False, "reply": f"当前滴滴 MCP 未提供 `{target_kw}` 工具。"}

    res = session.call_tool(t_name, _build_args_by_schema(tool_map[t_name], {"order_id": oid}))
    if cancel:
        db.set_meta("active_didi_order_json", "")
        return {"ok": True, "reply": f"🛑 已调用滴滴 API 取消订单 `{oid}`：{str(res)[:180]}"}
    return {"ok": True, "reply": f"🚕 滴滴订单 `{oid}`（{rec.get('origin')}→{rec.get('dest')}）实时状态：\n{str(res)[:320]}"}
