"""瑞幸咖啡官方 MCP Server（https://gwmcp.lkcoffee.com/order/user/mcp）原生接入：
- 你历史外卖下单频次第 1 名的常点品牌（18 单），走官方 AI 开放平台（https://open.lkcoffee.com）MCP Token，无需打开任何网页；
- 流程：一句话点单 (`quickOrder` / `reorder`) -> 价格上限与门店规格预览 -> 生成 15m 有效 `apply Axx` 确认卡 -> 本人确认后调 `confirmOrder` 出支付码/取餐码；
- 支持 `瑞幸状态` (`orderStatus`) 查询当前制作进度与取餐码。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Optional

import httpx

from siming.approval import create_approval
from siming.config import load_policy
from siming.crypto import read_secret, write_secret_file
from siming.db import SimingDB, iso_now

LUCKIN_SECRET_NAME = "luckin-mcp-token"
LUCKIN_MCP_ENDPOINT = "https://gwmcp.lkcoffee.com/order/user/mcp"


def save_luckin_mcp_token(db: SimingDB, token: str) -> dict[str, Any]:
    clean = (token or "").strip()
    if clean.lower().startswith("bearer "):
        clean = clean[7:].strip()
    if len(clean) < 12:
        return {"ok": False, "error": "瑞幸 MCP Token 长度不合法（请在 https://open.lkcoffee.com 获取）"}
    target = db.paths.secrets_dir / LUCKIN_SECRET_NAME
    write_secret_file(target, clean)
    db.write_audit("A1", "save_luckin_mcp_token", {"configured": True})
    return {
        "ok": True,
        "summary": "✅ 已安全保存瑞幸官方 MCP Token（`0600` 权限）。现在在 QQ 直接说 `来杯瑞幸 大杯冰美式不加糖` 或 `瑞幸再来一单` 即可生成确认卡真点单！",
    }


def get_luckin_mcp_token(db: SimingDB) -> Optional[str]:
    return read_secret(LUCKIN_SECRET_NAME, db.paths)


class LuckinMcpSession:
    """瑞幸咖啡官方 Streamable-HTTP MCP 客户端（Bearer Token 鉴权，纯 Python 零 npx 依赖）。"""

    def __init__(self, token: str, endpoint: str = LUCKIN_MCP_ENDPOINT, timeout: float = 15.0) -> None:
        self.url = endpoint
        self.token = token
        self.timeout = timeout
        self.session_id = ""
        self._req_id = 0

    def _parse_response(self, content_type: str, body_text: str) -> dict[str, Any]:
        ct = (content_type or "").lower()
        if "text/event-stream" in ct or body_text.lstrip().startswith(("event:", "data:")):
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
            "Authorization": f"Bearer {self.token}",
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

        with httpx.Client(trust_env=False, timeout=self.timeout) as client:
            resp = client.post(self.url, json=payload, headers=headers)
            sid = resp.headers.get("mcp-session-id") or resp.headers.get("Mcp-Session-Id")
            if sid:
                self.session_id = sid
            resp.raise_for_status()
            parsed = self._parse_response(resp.headers.get("content-type", ""), resp.text)

        if "error" in parsed:
            err_obj = parsed["error"]
            msg = err_obj.get("message") if isinstance(err_obj, dict) else str(err_obj)
            raise RuntimeError(f"瑞幸 MCP 返回错误: {msg}")
        return parsed.get("result") or {}

    def initialize_and_list_tools(self) -> list[dict[str, Any]]:
        self.rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "siming-personal-agent", "version": "0.4.0"},
            },
        )
        t_res = self.rpc("tools/list", {})
        return list(t_res.get("tools") or [])

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        res = self.rpc("tools/call", {"name": name, "arguments": arguments})
        contents = res.get("content") or []
        texts = [str(c.get("text") or "") for c in contents if isinstance(c, dict) and c.get("text")]
        joined = "\n".join(texts).strip()
        if joined.startswith(("{", "[")):
            try:
                return json.loads(joined)
            except Exception:
                return joined
        return res.get("structuredContent") or joined


def _find_tool(tools: list[dict[str, Any]], candidates: tuple[str, ...]) -> Optional[dict[str, Any]]:
    by_low = {str(t.get("name") or "").lower(): t for t in tools if isinstance(t, dict)}
    for c in candidates:
        if c.lower() in by_low:
            return by_low[c.lower()]
    for t in tools:
        t_low = str(t.get("name") or "").lower()
        if any(c.lower() in t_low for c in candidates):
            return t
    return None


def _build_adaptive_args(schema: dict[str, Any], prompt_text: str, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
    out: dict[str, Any] = {}
    if extra:
        for k, v in extra.items():
            if not props or k in props:
                out[k] = v
    if prompt_text:
        for key in ("query", "prompt", "text", "input", "productName", "keyword", "orderRequirement"):
            if key in props and key not in out:
                out[key] = prompt_text
                break
        if not out and props:
            first_key = next(iter(props.keys()))
            out[first_key] = prompt_text
        elif not props and not out:
            out["query"] = prompt_text
    return out


def _extract_draft_summary(raw_res: Any, fallback_spec: str) -> dict[str, Any]:
    draft_id = ""
    shop_name = ""
    item_desc = fallback_spec
    price_yuan: Optional[float] = None
    pay_url = ""

    if isinstance(raw_res, dict):
        data = raw_res.get("data") if isinstance(raw_res.get("data"), dict) else raw_res
        for k in ("draftId", "draft_id", "orderDraftId", "previewId", "orderId", "id"):
            if data.get(k):
                draft_id = str(data[k])
                break
        for k in ("shopName", "storeName", "deptName", "shop_name"):
            if data.get(k):
                shop_name = str(data[k])
                break
        for k in ("productSummary", "itemsDesc", "productName", "title", "summary"):
            if data.get(k):
                item_desc = str(data[k])
                break
        for k in ("payAmount", "totalAmount", "amount", "price", "realPayAmount"):
            val = data.get(k)
            if val is not None:
                try:
                    fval = float(str(val).replace("¥", "").replace("元", "").strip())
                    # 若返回单位是分（如 1600 > 250 且无小数点）
                    if fval >= 300 and "." not in str(val):
                        fval = round(fval / 100.0, 2)
                    price_yuan = fval
                    break
                except ValueError:
                    pass
        for k in ("payUrl", "paymentUrl", "qrCodeUrl", "url"):
            if data.get(k):
                pay_url = str(data[k])
                break
    else:
        txt = str(raw_res or "")
        m_id = re.search(r"(?:draftId|draft_id|草稿单号|预览单号)\s*[:：=]\s*([A-Za-z0-9_-]{4,64})", txt, re.IGNORECASE)
        if m_id:
            draft_id = m_id.group(1)
        m_pr = re.search(r"(?:实付|合计|价格|应付|¥|￥)\s*([0-9]+(?:\.[0-9]{1,2})?)", txt)
        if m_pr:
            try:
                price_yuan = float(m_pr.group(1))
            except ValueError:
                pass
        m_shop = re.search(r"(?:门店|店铺)\s*[:：]\s*([^\n，,]{3,36})", txt)
        if m_shop:
            shop_name = m_shop.group(1).strip()

    return {
        "draft_id": draft_id,
        "shop_name": shop_name,
        "item_desc": item_desc,
        "price_yuan": price_yuan,
        "pay_url": pay_url,
        "raw_preview": json.dumps(raw_res, ensure_ascii=False)[:500] if isinstance(raw_res, (dict, list)) else str(raw_res)[:500],
    }


def stage_luckin_order(
    db: SimingDB,
    spec_text: str,
    session_factory: Optional[Callable[[str], Any]] = None,
) -> dict[str, Any]:
    """调用瑞幸官方 MCP 创建点餐草稿并生成 15 分钟有效 `apply Axx` 确认卡。"""
    db.init_db()
    clean_spec = re.sub(r"^(?:帮我|替我)?(?:点一杯|来一杯|来杯|点|下单)?(?:瑞幸(?:咖啡)?)\s*", "", (spec_text or "").strip()).strip()
    is_reorder = "再来一单" in (spec_text or "") or clean_spec in ("常点的", "老样子", "")
    if not clean_spec:
        clean_spec = "常点咖啡（再来一单）"

    token = get_luckin_mcp_token(db)
    if not token:
        reply = (
            f"☕ **瑞幸咖啡智能点单准备**（需求：{clean_spec}）\n"
            "• 瑞幸是你外卖账本历史下单 **第 1 名** 的常点店，官方已提供纯 API 点餐通道（无需开网页）。\n"
            "• **一步开通**：浏览器打开 `https://open.lkcoffee.com` 绑定手机号复制专属 MCP Token，然后在终端运行：\n"
            "  `siming luckin --set-key`（或直接在 QQ 发 `瑞幸密钥 <你的Token>`，网关自动拦截加密不进日志）\n"
            "• 配好后说一句 `来杯瑞幸 大杯冰美式不加糖` 或 `瑞幸再来一单`，即自动选店用券并弹 `apply Axx` 确认下单！"
        )
        return {"ok": True, "configured": False, "reply": reply}

    policy = load_policy(db.paths)
    caps = policy.get("spending_caps") or {}
    max_yuan = float(caps.get("luckin_single_order_yuan", 65.0))
    from siming.context_loc import resolve_current_location_context

    loc_ctx = resolve_current_location_context(db)
    loc_suffix = f"（附近位置：{loc_ctx['address']}）" if loc_ctx.get("has_fresh") and loc_ctx.get("address") else ""

    make_sess = session_factory or (lambda tk: LuckinMcpSession(tk))
    try:
        sess = make_sess(token)
        tools = sess.initialize_and_list_tools()
        target_tool = (
            _find_tool(tools, ("reorder", "quickOrder", "quick_order"))
            if is_reorder
            else _find_tool(tools, ("quickOrder", "quick_order", "createDraft", "order"))
        )
        if not target_tool:
            raise RuntimeError("瑞幸 MCP 未返回可用的点单工具（quickOrder / reorder）")

        t_name = str(target_tool["name"])
        prompt_with_loc = "" if (is_reorder and "reorder" in t_name.lower()) else f"{clean_spec}{loc_suffix}"
        t_args = _build_adaptive_args(target_tool.get("inputSchema") or {}, prompt_with_loc)
        raw_res = sess.call_tool(t_name, t_args)
        parsed = _extract_draft_summary(raw_res, clean_spec)
    except Exception as exc:
        return {"ok": False, "configured": True, "reply": f"❌ 瑞幸官方 MCP 询价建单失败：{exc}"}

    if parsed["price_yuan"] is not None and parsed["price_yuan"] > max_yuan:
        return {
            "ok": False,
            "configured": True,
            "over_cap": True,
            "reply": (
                f"🛑 瑞幸草稿单预估实付 **¥{parsed['price_yuan']:.2f}** 超过单笔安全上限（¥{max_yuan:.0f}），已拦截未建确认令。\n"
                "如需点多人拼单可在 `~/.config/siming/policy.yaml` 调高 `spending_caps.luckin_single_order_yuan`。"
            ),
        }

    confirm_tool = _find_tool(tools, ("confirmOrder", "confirm_order", "submitOrder"))
    confirm_tool_name = str(confirm_tool["name"]) if confirm_tool else "confirmOrder"
    confirm_schema = (confirm_tool.get("inputSchema") or {}) if confirm_tool else {}

    app_params = {
        "draft_id": parsed["draft_id"],
        "confirm_tool": confirm_tool_name,
        "confirm_schema": confirm_schema,
        "spec": parsed["item_desc"],
        "shop_name": parsed["shop_name"],
        "price_yuan": parsed["price_yuan"],
    }
    app = create_approval(
        db,
        action="luckin_confirm_order",
        target_id=f"LUCKIN-{parsed['draft_id'] or 'DRAFT'}"[:32],
        params=app_params,
        ttl_minutes=15,
    )
    db.record_human_use("meituan_agent", "luckin_stage", ref_id=app["token"])

    price_str = f"¥{parsed['price_yuan']:.2f}" if parsed["price_yuan"] is not None else "以瑞幸券后价为准"
    shop_str = parsed["shop_name"] or "常用自提/外送门店"
    lines = [
        f"☕ **瑞幸官方 MCP 点单确认卡** [`{app['token']}`]",
        f"• 门店：**{shop_str}**",
        f"• 饮品：**{parsed['item_desc']}**",
        f"• 预估实付：**{price_str}**（单笔上限 ¥{max_yuan:.0f} · 15 分钟内有效）",
    ]
    if parsed["draft_id"]:
        lines.append(f"• 草稿单号：`{parsed['draft_id']}`")
    lines.append(f"👉 确认下单请直接发 `apply {app['token']}`（或引用本条回 `确认`）。")
    return {
        "ok": True,
        "configured": True,
        "approval_token": app["token"],
        "draft": parsed,
        "reply": "\n".join(lines),
    }


def execute_luckin_confirm_order(
    params: dict[str, Any],
    db: SimingDB,
    session_factory: Optional[Callable[[str], Any]] = None,
) -> str:
    """纯代码审批通过后调用瑞幸 MCP `confirmOrder` 落单，返回付款链接或取餐码。"""
    token = get_luckin_mcp_token(db)
    if not token:
        raise RuntimeError("未找到瑞幸 MCP Token，无法提交订单")
    draft_id = str(params.get("draft_id") or "").strip()
    tool_name = str(params.get("confirm_tool") or "confirmOrder")
    schema = params.get("confirm_schema") or {}

    props = (schema.get("properties") or {}) if isinstance(schema, dict) else {}
    args: dict[str, Any] = {}
    if draft_id:
        placed = False
        for k in ("draftId", "draft_id", "orderDraftId", "previewId", "orderId", "id"):
            if k in props:
                args[k] = draft_id
                placed = True
                break
        if not placed:
            args["draftId"] = draft_id

    make_sess = session_factory or (lambda tk: LuckinMcpSession(tk))
    sess = make_sess(token)
    sess.initialize_and_list_tools()
    res = sess.call_tool(tool_name, args)

    res_txt = json.dumps(res, ensure_ascii=False) if isinstance(res, (dict, list)) else str(res)
    db.set_meta("last_luckin_order_at", iso_now())
    db.set_meta("last_luckin_order_raw", res_txt[:600])

    pickup_code = ""
    pay_link = ""
    if isinstance(res, dict):
        d = res.get("data") if isinstance(res.get("data"), dict) else res
        pickup_code = str(d.get("pickupCode") or d.get("takeCode") or d.get("code") or "")
        pay_link = str(d.get("payUrl") or d.get("paymentUrl") or d.get("qrCodeUrl") or "")
    if not pay_link:
        m_u = re.search(r"(https?://[^\s\"'<>]+)", res_txt)
        if m_u:
            pay_link = m_u.group(1)

    parts = [f"瑞幸订单已确认提交（{params.get('shop_name') or '门店'} · {params.get('spec') or '咖啡'}）"]
    if pickup_code:
        parts.append(f"取餐码：`{pickup_code}`")
    if pay_link:
        parts.append(f"待支付链接/二维码：{pay_link}")
    elif not pickup_code:
        parts.append(f"回执：{res_txt[:220]}")
    parts.append("后续可发 `瑞幸状态` 查看出杯进度与取餐码。")
    return " ｜ ".join(parts)


def query_luckin_order_status(
    db: SimingDB,
    session_factory: Optional[Callable[[str], Any]] = None,
) -> dict[str, Any]:
    token = get_luckin_mcp_token(db)
    if not token:
        return {"ok": False, "reply": "尚未配置瑞幸官方 MCP Token（运行 `siming luckin --set-key` 即可配置）。"}
    make_sess = session_factory or (lambda tk: LuckinMcpSession(tk))
    try:
        sess = make_sess(token)
        tools = sess.initialize_and_list_tools()
        st_tool = _find_tool(tools, ("orderStatus", "order_status", "queryOrder", "getOrder"))
        if not st_tool:
            return {"ok": False, "reply": "瑞幸 MCP 当前未暴露订单状态查询工具。"}
        res = sess.call_tool(str(st_tool["name"]), {})
        txt = json.dumps(res, ensure_ascii=False) if isinstance(res, (dict, list)) else str(res)
        return {"ok": True, "reply": f"☕ **瑞幸当前订单状态**：\n{txt[:500]}"}
    except Exception as exc:
        return {"ok": False, "reply": f"❌ 查询瑞幸订单状态失败：{exc}"}
