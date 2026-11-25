"""美团外卖 H5 订单商品明细采集（本机主 Chromium 9222 后台标签，只读）。

为什么需要：银行/钱包流水里只有「美团 · 京亿康医疗器械」这种商户名，看不到买了什么、买了几盒；
补货雷达（口罩 / 注射笔针头 / 酒精棉片）必须知道商品名与数量，才能按「包装规格 × 份数 − 日均消耗」推算余量。

合规边界（与 cdp.py 一致）：
- 只在后台新开标签（background=True），不抢 Hyprland 焦点、不弹窗；读完即关；
- 只读页面文字，不调用带签名的私有接口、不点击任何按钮；
- 命中登录 / 滑块 / 风控页立刻停手：关闭临时标签并记 source 错误，由用户自己在浏览器里处理，绝不尝试绕过。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local

OLIST_URL = "https://h5.waimai.meituan.com/waimai/mindex/olist"
_TS_RE = re.compile(r"^20\d\d-\d\d-\d\d \d\d:\d\d$")
_QTY_RE = re.compile(r"^[xX×]\s*(\d{1,4})$")
_STATUS_RE = re.compile(r"^(?:已[\u4e00-\u9fa5]{1,5}|退款[\u4e00-\u9fa5]{0,4}|部分退款|配送中|待[\u4e00-\u9fa5]{1,4}|商家已接单|骑手[\u4e00-\u9fa5]{0,6}|订单[\u4e00-\u9fa5]{0,6})$")
_CANCELLED_RE = re.compile(r"(取消|已退款|退款成功|全额退款)")


def parse_olist_text(txt: str) -> list[dict[str, Any]]:
    """把美团外卖 H5「订单」页 innerText 解析为订单列表。

    页面每单的文字结构（2026-11 实测）：
        店名 / 商品名 / x数量 / 商品名 / x数量 / ... / 2026-11-11 23:11 / 实付¥32.7 / 已完成 / 删除 / 再来一单
    店名不能靠「按钮词表」判断（按钮会变），这里用结构锚点：第一个「后面紧跟 xN 的行」是首个商品，其前一行即店名。
    """
    lines = [ln.strip() for ln in (txt or "").replace("\r", "").split("\n")]
    lines = [ln for ln in lines if ln]
    orders: list[dict[str, Any]] = []
    seg: list[str] = []
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("实付¥") and seg and _TS_RE.match(seg[-1]):
            ts = seg[-1]
            body = seg[:-1]
            status = ""
            if i + 1 < len(lines) and _STATUS_RE.match(lines[i + 1]):
                status = lines[i + 1]
                i += 1
            first_item = next((k for k in range(len(body) - 1) if _QTY_RE.match(body[k + 1])), None)
            if first_item is not None:
                shop = body[first_item - 1] if first_item > 0 else ""
                items: list[tuple[str, int]] = []
                k = first_item
                while k < len(body):
                    if k + 1 < len(body) and _QTY_RE.match(body[k + 1]):
                        items.append((body[k], int(_QTY_RE.match(body[k + 1]).group(1))))  # type: ignore[union-attr]
                        k += 2
                    else:
                        k += 1
                try:
                    amount = float(ln.replace("实付¥", "").strip() or 0)
                except ValueError:
                    amount = 0.0
                orders.append(
                    {
                        "shop": shop,
                        "ordered_at": ts,
                        "amount": amount,
                        "status": status,
                        "cancelled": bool(_CANCELLED_RE.search(status)),
                        "items": items,
                    }
                )
            seg = []
        else:
            seg.append(ln)
        i += 1
    return orders


def _iso_from_local(ts: str) -> str:
    dt = datetime.strptime(ts, "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI_TZ)
    return dt.isoformat(timespec="seconds")


def store_orders(db: SimingDB, orders: list[dict[str, Any]], platform: str = "meituan") -> dict[str, int]:
    """幂等写入 shop_order_items；同一单状态变化（如后来退款）会刷新 order_status。"""
    db.init_db()
    inserted = updated = 0
    now_s = iso_now()
    with db.transaction() as conn:
        for o in orders:
            ordered_iso = _iso_from_local(o["ordered_at"])
            for name, qty in o["items"]:
                key_src = f"{platform}|{o['ordered_at']}|{o['shop']}|{o['amount']:.2f}|{name}"
                item_key = hashlib.sha1(key_src.encode("utf-8")).hexdigest()[:20]
                cur = conn.execute("SELECT order_status FROM shop_order_items WHERE item_key=?", (item_key,)).fetchone()
                if cur is None:
                    conn.execute(
                        """
                        INSERT INTO shop_order_items (
                            item_key, platform, shop, ordered_at, order_status, order_amount_cents,
                            item_name, qty, created_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (item_key, platform, o["shop"], ordered_iso, o["status"], int(round(o["amount"] * 100)), name, int(qty), now_s),
                    )
                    inserted += 1
                elif o["status"] and str(cur["order_status"]) != o["status"]:
                    conn.execute("UPDATE shop_order_items SET order_status=? WHERE item_key=?", (o["status"], item_key))
                    updated += 1
    return {"inserted": inserted, "updated": updated}


_SCROLL_STEP_JS = "document.body ? document.body.innerText : ''"


def _live_harvest_text(since_local: str, max_rounds: int = 120, port: int = 9222) -> dict[str, Any]:
    """后台标签打开订单页并用合成滚轮把列表翻到 since_local 之前（后台标签不渲染，必须开 focus emulation）。"""
    from siming.cdp import LocalWsCdpClient, detect_compliance_gate, resolve_chromium_cdp_base

    base = resolve_chromium_cdp_base(port=port)
    if not base or not base.get("ws_browser"):
        return {"ok": False, "reason": "cdp_unavailable"}
    tid = ""
    try:
        with LocalWsCdpClient(base["ws_browser"], timeout=6.0) as b_ws:
            tid = str(b_ws.call("Target.createTarget", {"url": OLIST_URL, "background": True}).get("targetId") or "")
        if not tid:
            return {"ok": False, "reason": "create_target_failed"}
        time.sleep(4.0)
        page_ws = base["ws_browser"].split("/devtools/")[0] + f"/devtools/page/{tid}"
        with LocalWsCdpClient(page_ws, timeout=20.0) as p:
            meta = p.call(
                "Runtime.evaluate",
                {"expression": "JSON.stringify({u: location.href, t: document.title || '', x: (document.body && document.body.innerText || '').slice(0, 1500)})", "returnByValue": True},
            )
            pm = json.loads(str((meta.get("result") or {}).get("value") or "{}"))
            gate = detect_compliance_gate(str(pm.get("u") or ""), str(pm.get("t") or ""), str(pm.get("x") or ""))
            if gate is not None:
                return {"ok": False, "reason": "gated", "gate": gate}
            for method, params in (
                ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
                ("Page.enable", {}),
                ("Page.setWebLifecycleState", {"state": "active"}),
                ("Emulation.setDeviceMetricsOverride", {"width": 414, "height": 896, "deviceScaleFactor": 1, "mobile": True}),
            ):
                try:
                    p.call(method, params)
                except Exception:
                    pass
            txt, last_len, still = "", -1, 0
            for _ in range(max_rounds):
                r = p.call("Runtime.evaluate", {"expression": _SCROLL_STEP_JS, "returnByValue": True})
                txt = str((r.get("result") or {}).get("value") or "")
                stamps = re.findall(r"20\d\d-\d\d-\d\d \d\d:\d\d", txt)
                if stamps and stamps[-1] < since_local:
                    break
                if len(txt) == last_len:
                    still += 1
                    if still >= 6 or "没有更多" in txt[-200:]:
                        break
                else:
                    still, last_len = 0, len(txt)
                p.call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": 200, "y": 500, "deltaX": 0, "deltaY": 3000})
                time.sleep(0.9)
        return {"ok": True, "text": txt}
    except Exception as exc:
        return {"ok": False, "reason": "cdp_error", "error": str(exc)[:200]}
    finally:
        if tid:
            try:
                with LocalWsCdpClient(base["ws_browser"], timeout=4.0) as b_ws:
                    b_ws.call("Target.closeTarget", {"targetId": tid})
            except Exception:
                pass


def collect_meituan_orders(
    db: SimingDB,
    since_days: int = 0,
    fetch_text: Optional[Callable[[str], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """增量采集：默认翻到「已入库最新一单 − 2 天」为止；首次或 since_days>0 时回溯对应天数。"""
    db.init_db()
    if fetch_text is None and os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return {"ok": False, "reason": "offline"}
    conn = db.connect()
    try:
        row = conn.execute("SELECT MAX(ordered_at) AS m FROM shop_order_items WHERE platform='meituan'").fetchone()
    finally:
        conn.close()
    latest = str(row["m"] or "") if row else ""
    if since_days > 0 or not latest:
        since_dt = now_local() - timedelta(days=since_days or 240)
    else:
        since_dt = datetime.fromisoformat(latest) - timedelta(days=2)
    since_local = since_dt.strftime("%Y-%m-%d %H:%M")

    res = (fetch_text or _live_harvest_text)(since_local)
    if not res.get("ok"):
        reason = str(res.get("reason") or "error")
        detail = (res.get("gate") or {}).get("reason") or res.get("error") or reason
        if reason == "cdp_unavailable":
            # 浏览器临时没开：退避 30 分钟后重试，仅当连续超 48h 未同步才把数据源标为 DEGRADED
            db.set_meta("meituan_orders_last_try_at", iso_now(now_local() - timedelta(hours=7, minutes=30)))
            last_ok = db.get_meta("meituan_orders_last_ok_at") or ""
            if not last_ok or (now_local() - datetime.fromisoformat(last_ok)) > timedelta(hours=48):
                db.mark_source_error("meituan_orders", "DEGRADED", str(detail)[:240])
            return {"ok": False, "reason": reason, "detail": detail}
        db.set_meta("meituan_orders_last_try_at", iso_now())
        db.mark_source_error("meituan_orders", "ERROR", str(detail)[:240])
        return {"ok": False, "reason": reason, "detail": detail}
    db.set_meta("meituan_orders_last_try_at", iso_now())
    orders = parse_olist_text(str(res.get("text") or ""))
    stats = store_orders(db, orders)
    db.set_meta("meituan_orders_last_ok_at", iso_now())
    db.mark_source_ok("meituan_orders", detail=f"orders={len(orders)} new_items={stats['inserted']}")
    return {"ok": True, "orders": len(orders), **stats, "since": since_local}
