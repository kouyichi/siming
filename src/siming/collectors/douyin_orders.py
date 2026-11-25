"""抖音生态无感联动采集器（Directive #5 + F09 · collectors/douyin_orders.py）：
经实测验证字节跳动已下线电脑网页版 `douyin.com` 个人买家『我的订单』入口（404，实物订单仅在手机 App 内或通过『我的钱包 -> 账单 -> 导出至邮箱』输出），
但本机 `9222` 已登录的抖音网页版（`www.douyin.com/user/self?showTab=favorite_collection`）可直接只读读取主人手机端点的 ⭐ **我的收藏**！
因此本模块采用三路互补：
1. **网页版收藏夹无感同步（`9222` 后台标签零抢焦点）**：自动读取你在手机抖音点的 ⭐ 收藏视频文案（如 GitHub 热榜、`zigbee` 全屋智能、独立游戏），
   自动筛出技术/开源/家装/工具类灵感直入 `bookmarks`（免截屏！）；
2. **手机订单截图解析**：顺丰冷链/实物包裹敲背面截屏时，由 `superagent_triage` 拆单入 `shop_order_items(platform='douyin')` 并自动配平无商户银行流水；
3. **抖音钱包账单 CSV 解析（`parse_douyin_bill_csv`）**：当你在抖音 App 导出账单 CSV 到 163/Gmail 邮箱或本地导入时，逐笔核销 `¥9.90` 小说与商城订单。
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
from typing import Any

from siming.db import SimingDB, iso_now, now_local, parse_iso

DOUYIN_FAV_URL = "https://www.douyin.com/user/self?showTab=favorite_collection"

_TECH_OR_HOME_FAV_RE = re.compile(
    r"(GitHub|开源|AI工具|AI编程|Agent|Claude|DeepSeek|全屋智能|智能家居|zigbee|HomeKit|米家|HAOS|软路由|光猫|NAS|Docker|Steam|独立游戏|理财App)",
    re.IGNORECASE,
)


def extract_tech_bookmarks_from_douyin_favorites(raw_text: str) -> list[dict[str, str]]:
    """从抖音网页版『我的收藏』页面纯文本中切分出技术/开源/全屋智能类收藏视频。"""
    blocks = [b.strip() for b in re.split(r"\n\s*(?:\d+(?:\.\d+)?万?|\d{2,6})\s*\n", raw_text or "") if b.strip()]
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for blk in blocks:
        clean = re.sub(r"\s+", " ", blk).strip()
        if len(clean) < 12 or len(clean) > 600:
            continue
        if not _TECH_OR_HOME_FAV_RE.search(clean):
            continue
        title = clean.split(" ")[0][:46]
        key = clean[:32]
        if key in seen:
            continue
        seen.add(key)
        results.append({"title": f"抖音收藏 · {title}", "body": clean})
    return results


def collect_douyin_orders(db: SimingDB, force: bool = False, port: int = 9222) -> dict[str, Any]:
    """通过本机 `9222` 已登录抖音网页版，后台静默同步 ⭐ 收藏夹中的技术/智能家居灵感至 `bookmarks`（遇未登录或验证码即停）。"""
    db.init_db()
    if os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return {"ok": True, "skipped": "offline_test"}

    now = now_local()
    if not force:
        if not (10 <= now.hour <= 23):
            return {"ok": True, "skipped": "quiet_window"}
        last_s = db.get_meta("last_douyin_harvest_at") or ""
        if last_s:
            try:
                if (now - parse_iso(last_s)).total_seconds() < 6 * 3600:
                    return {"ok": True, "skipped": "interval_6h"}
            except Exception:
                pass

    from siming.cdp import LocalWsCdpClient, detect_compliance_gate, resolve_chromium_cdp_base
    from siming.superagent_triage import save_bookmark_from_drop

    db.set_meta("last_douyin_harvest_at", iso_now(now))
    base = resolve_chromium_cdp_base(port=port)
    if not base or not base.get("ws_browser"):
        return {"ok": False, "reason": "cdp_unavailable"}

    tid = ""
    try:
        with LocalWsCdpClient(base["ws_browser"], timeout=6.0) as b_ws:
            tid = str(b_ws.call("Target.createTarget", {"url": DOUYIN_FAV_URL, "background": True}).get("targetId") or "")
        if not tid:
            return {"ok": False, "reason": "create_target_failed"}
        page_ws = base["ws_browser"].split("/devtools/")[0] + f"/devtools/page/{tid}"
        with LocalWsCdpClient(page_ws, timeout=14.0) as p:
            for method, params in (
                ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
                ("Page.enable", {}),
                ("Page.setWebLifecycleState", {"state": "active"}),
            ):
                try:
                    p.call(method, params)
                except Exception:
                    pass
            time.sleep(4.2)
            ev = p.call(
                "Runtime.evaluate",
                {
                    "expression": "JSON.stringify({u: location.href, t: document.title || '', x: (document.body && document.body.innerText || '').slice(0, 18000)})",
                    "returnByValue": True,
                },
            )
            val = json.loads(((ev.get("result") or {}).get("value")) or "{}")
            gate = detect_compliance_gate(str(val.get("u") or ""), str(val.get("t") or ""), str(val.get("x") or "")[:1500])
            if gate is not None:
                if gate.get("gate_kind") == "login_required":
                    db.mark_source_error("douyin_orders", "UNCONFIGURED", gate.get("reason", "需登录抖音网页版"))
                return {"ok": False, "gated": True, "reason": gate.get("reason")}
            body_txt = str(val.get("x") or "")
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:180]}
    finally:
        if tid:
            try:
                with LocalWsCdpClient(base["ws_browser"], timeout=4.0) as b_ws:
                    b_ws.call("Target.closeTarget", {"targetId": tid})
            except Exception:
                pass

    favs = extract_tech_bookmarks_from_douyin_favorites(body_txt)
    added = 0
    conn = db.connect()
    try:
        existing_titles = {str(r["title"]) for r in conn.execute("SELECT title FROM bookmarks").fetchall()}
    finally:
        conn.close()

    for f in favs:
        if f["title"] in existing_titles:
            continue
        save_bookmark_from_drop(db, f["body"], title_override=f["title"])
        existing_titles.add(f["title"])
        added += 1

    db.mark_source_ok("douyin_orders", detail=f"web_favs_scanned={len(favs)} new_bookmarks={added}")
    return {"ok": True, "favs_matched": len(favs), "bookmarks_added": added}


def ingest_douyin_bill_csv_text(db: SimingDB, csv_text: str) -> dict[str, Any]:
    """导入抖音钱包导出的 CSV 账单明细，自动与银行流水对账。"""
    from siming.ledger.reconcile import match_or_record_wallet_row

    reader = csv.DictReader(io.StringIO((csv_text or "").lstrip("\ufeff")))
    ingested = matched = 0
    for row in reader:
        ts_raw = (row.get("交易时间") or row.get("创建时间") or "").strip()
        amt_raw = (row.get("金额") or row.get("实付金额") or "").replace("¥", "").replace("元", "").strip()
        merch = (row.get("商品说明") or row.get("商户名称") or row.get("交易对方") or "").strip()
        direction = (row.get("收/支") or row.get("交易类型") or "支出").strip()
        if not ts_raw or not amt_raw:
            continue
        try:
            cents = int(round(abs(float(amt_raw)) * 100))
        except ValueError:
            continue
        if cents <= 0:
            continue
        occ_iso = ts_raw.replace("/", "-")
        if len(occ_iso) == 16:
            occ_iso += ":00"
        if "+" not in occ_iso:
            occ_iso = occ_iso.replace(" ", "T") + "+08:00"
        res = match_or_record_wallet_row(
            db,
            occurred_at=occ_iso,
            amount_cents=cents,
            card_last4="",
            wallet_alias="抖音支付",
            merchant=f"抖音 · {merch}"[:48] if merch else "抖音支付",
            kind="退款" if "退" in direction or "收" in direction else "消费",
        )
        ingested += 1
        if res.get("status") == "MATCHED":
            matched += 1
    return {"ok": True, "ingested": ingested, "matched": matched}
