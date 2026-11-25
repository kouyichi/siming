"""S3-1（决策 1=B）：网购商品通用保价监控（兼容当前 15 单大家电 + 后续随时新增任意京东/电商商品）。
- 复用 ~/.hermes/scripts/buy-price-watch/{orders.json, state.json}（保留 .account.lock 与 60s 间隔防风控，严守 auto_claim=false 不自动代点申请）
- 同时融合 ~/buy/track/data/auto.json 快照信息
- 由司命统一去重水位、QQ 推送（rule_id='price_protect_watch'）与引用回复「已保价 / 不看了」闭环。
"""

from __future__ import annotations

import json
import os
import re
import shutil
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable, Optional

from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local, parse_iso
from siming.modules import is_module_enabled

DEFAULT_PRICE_WATCH_DIR = Path.home() / ".hermes" / "scripts" / "buy-price-watch"
DEFAULT_AUTO_JSON = Path.home() / "buy" / "track" / "data" / "auto.json"
JD_PRICE_PROTECT_URL = "https://msitepp-fm.jd.com/rest/priceprophone/priceProPhoneMenu?defaultViewTab=0"
OFFICIAL_PROBE_INTERVAL_SEC = 4 * 3600
ACTIVE_STALE_HOURS = 12.0

_JD_SKU_IN_URL_RE = re.compile(r"(?:item\.jd\.com/|item\.m\.jd\.com/product/|sku=)(\d{6,18})")
_DIGITS_ONLY_RE = re.compile(r"^\d{6,22}$")


def _resolve_watch_dir(watch_dir: Optional[Path] = None) -> Path:
    if watch_dir is not None:
        return watch_dir
    override = os.environ.get("SIMING_PRICE_WATCH_DIR")
    if override:
        return Path(override).expanduser()
    return DEFAULT_PRICE_WATCH_DIR


def _to_cents(val: Any) -> Optional[int]:
    if val is None or val == "":
        return None
    s = re.sub(r"^[¥￥$\s]+", "", str(val).strip()).replace(",", "")
    if not s:
        return None
    try:
        return int((Decimal(s) * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP))
    except Exception:
        return None


def extract_sku(sku_or_url: str) -> str:
    raw = (sku_or_url or "").strip()
    m = _JD_SKU_IN_URL_RE.search(raw)
    if m:
        return m.group(1)
    if _DIGITS_ONLY_RE.fullmatch(raw):
        return raw
    m_any = re.search(r"(?<!\d)(\d{8,18})(?!\d)", raw)
    return m_any.group(1) if m_any else ""


def _load_json_safe(path: Path, default: Any) -> Any:
    if not path.is_file():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _atomic_write_orders_with_backup(orders_path: Path, doc: dict[str, Any]) -> Optional[Path]:
    orders_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path: Optional[Path] = None
    if orders_path.is_file():
        stamp = now_local().strftime("%Y%m%d-%H%M%S")
        backup_path = orders_path.with_name(f"{orders_path.name}.bak-{stamp}")
        if not backup_path.exists():
            shutil.copy2(orders_path, backup_path)
            os.chmod(backup_path, 0o600)

    # 强制守护红线：永远禁止开启自动代点申请价保
    doc["auto_claim"] = False
    tmp_p = orders_path.with_name(f".{orders_path.name}.tmp-{os.getpid()}")
    fd = os.open(str(tmp_p), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, (json.dumps(doc, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    finally:
        os.close(fd)
    os.replace(tmp_p, orders_path)
    return backup_path


def _load_watermarks(db: SimingDB) -> dict[str, dict[str, Any]]:
    raw = db.get_meta("price_watch_watermarks_json")
    if not raw:
        return {}
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else {}
    except Exception:
        return {}


def _save_watermarks(db: SimingDB, wm: dict[str, dict[str, Any]], conn: Any = None) -> None:
    db.set_meta("price_watch_watermarks_json", json.dumps(wm, ensure_ascii=False), conn=conn)


def add_price_watch(
    db: SimingDB,
    sku_or_url: str,
    name: str,
    paid_yuan: float | str,
    order_id: str = "",
    buy_date: str = "",
    pp_days: int = 30,
    watch_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """向保价监控池新增或更新一件网购商品（支持直接传京东商品链接或 SKU，自动备份 orders.json）。"""
    db.init_db()
    sku = extract_sku(sku_or_url)
    if not sku:
        return {"ok": False, "error": f"无法从「{sku_or_url}」解析有效商品 SKU（请提供 6~18 位商品编号或京东 item.jd.com 链接）"}

    paid_cents = _to_cents(paid_yuan)
    if paid_cents is None or paid_cents <= 0:
        return {"ok": False, "error": f"实付金额必须大于 0（收到：{paid_yuan}）"}

    clean_name = (name or "").strip() or f"商品-{sku}"
    today_s = now_local().date().isoformat()
    b_date = (buy_date or "").strip() or today_s
    try:
        date.fromisoformat(b_date)
    except ValueError:
        return {"ok": False, "error": f"购买日期格式应为 YYYY-MM-DD（收到：{b_date}）"}

    ord_key = (order_id or "").strip() or f"manual-{sku}"
    w_dir = _resolve_watch_dir(watch_dir)
    orders_path = w_dir / "orders.json"
    doc = _load_json_safe(
        orders_path,
        {"min_diff": 10, "auto_claim": False, "notify_cooldown": True, "orders": []},
    )
    if not isinstance(doc, dict):
        doc = {"min_diff": 10, "auto_claim": False, "notify_cooldown": True, "orders": []}
    orders_list = list(doc.get("orders") or [])

    paid_num: int | float = (
        paid_cents // 100 if paid_cents % 100 == 0 else round(paid_cents / 100.0, 2)
    )
    new_entry: dict[str, Any] = {
        "name": clean_name,
        "order": ord_key,
        "sku": sku,
        "date": b_date,
        "paid": paid_num,
        "pp_days": max(1, min(int(pp_days or 30), 365)),
    }

    updated_existing = False
    for idx, it in enumerate(orders_list):
        if not isinstance(it, dict):
            continue
        if str(it.get("order") or "") == ord_key or (not order_id and str(it.get("sku") or "") == sku):
            orders_list[idx] = {**it, **new_entry}
            updated_existing = True
            break
    if not updated_existing:
        orders_list.append(new_entry)
    doc["orders"] = orders_list
    bak = _atomic_write_orders_with_backup(orders_path, doc)

    # 若此前曾标过 muted，重新加入时自动解除静音
    wm = _load_watermarks(db)
    if ord_key in wm:
        wm[ord_key]["muted"] = False
        _save_watermarks(db, wm)

    db.record_human_use("price_protect_watch", "add_watch", ref_id=ord_key)
    db.write_audit(
        "A1",
        "price_watch_add",
        {"order": ord_key, "sku": sku, "name": clean_name, "paid": paid_num, "backup": str(bak or "")},
        target_id=ord_key,
    )
    return {
        "ok": True,
        "updated": updated_existing,
        "entry": new_entry,
        "backup_path": str(bak or ""),
        "summary": (
            f"🏷️ 已{'更新' if updated_existing else '加入'}保价监控：{clean_name}（SKU `{sku}` · 订单 `{ord_key}`）\n"
            f"• 实付基准：¥{paid_cents / 100:.2f} ｜ 起算日：{b_date}（保价期 {new_entry['pp_days']} 天）\n"
            "• 每小时巡检降价 ≥ ¥10 即发卡提醒（严守安全边界，由本人在 App 一键点申请）。"
        ),
    }


def remove_price_watch(
    db: SimingDB,
    target: str,
    watch_dir: Optional[Path] = None,
    mute_only: bool = False,
) -> dict[str, Any]:
    """按订单号、SKU 或商品名称移除（或静音）一条保价监控项。"""
    db.init_db()
    q = (target or "").strip()
    if not q:
        return {"ok": False, "error": "请指定要移除的订单号、SKU 或商品名称关键词"}

    w_dir = _resolve_watch_dir(watch_dir)
    orders_path = w_dir / "orders.json"
    doc = _load_json_safe(orders_path, {})
    orders_list = list((doc.get("orders") if isinstance(doc, dict) else None) or [])

    matched: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for it in orders_list:
        if not isinstance(it, dict):
            continue
        is_hit = (
            q == str(it.get("order") or "")
            or q == str(it.get("sku") or "")
            or q.lower() in str(it.get("name") or "").lower()
        )
        if is_hit:
            matched.append(it)
            if mute_only:
                kept.append(it)
        else:
            kept.append(it)

    if not matched:
        return {"ok": False, "error": f"未在保价监控池中找到匹配「{q}」的商品"}

    if len(matched) > 1 and not any(q == str(m.get("order")) or q == str(m.get("sku")) for m in matched):
        names = "、".join(f"{m.get('name')}({m.get('sku')})" for m in matched[:5])
        return {"ok": False, "error": f"关键词「{q}」命中多件商品（{names}），请用具体 SKU 或订单号指定"}

    target_item = (
        next((m for m in matched if q in (str(m.get("order")), str(m.get("sku")))), matched[0])
    )
    ord_key = str(target_item.get("order") or target_item.get("sku") or q)
    if not mute_only and isinstance(doc, dict):
        kept_exact = [it for it in orders_list if it is not target_item]
        doc["orders"] = kept_exact
        _atomic_write_orders_with_backup(orders_path, doc)

    wm = _load_watermarks(db)
    wm.setdefault(ord_key, {})["muted"] = True
    _save_watermarks(db, wm)

    db.write_audit(
        "A1",
        "price_watch_remove",
        {"target": ord_key, "name": target_item.get("name"), "mute_only": mute_only},
        target_id=ord_key,
    )
    return {
        "ok": True,
        "order": ord_key,
        "name": str(target_item.get("name") or ord_key),
        "summary": f"已{'停止提醒' if mute_only else '从保价监控池移除'}：{target_item.get('name')}（`{ord_key}`）。",
    }


def ack_price_watch_claimed(db: SimingDB, order_key: str, claimed_price_cents: Optional[int] = None) -> dict[str, Any]:
    """用户回复「已保价」后更新该单基准水位：同一价位不再重报，若后续继续跌破新基准 ≥ min_diff 仍能再抓二次退差！"""
    wm = _load_watermarks(db)
    rec = wm.setdefault(order_key, {})
    floor = claimed_price_cents if claimed_price_cents is not None else rec.get("alerted_floor_cents")
    if floor is not None:
        rec["claimed_floor_cents"] = int(floor)
    rec["claimed_at"] = iso_now()
    _save_watermarks(db, wm)
    db.record_human_use("price_protect_watch", "claimed_ack", ref_id=order_key)
    return {"ok": True, "order": order_key, "new_baseline_cents": floor}


def _load_official_snapshot(db: SimingDB) -> dict[str, Any]:
    raw = db.get_meta("price_watch_official_snapshot")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _parse_official_pp_text(text: str) -> list[dict[str, Any]]:
    """从京东官方价保页只读正文抽取疑似可申请价保的条目（绝不把历史「价保成功/无差价」误判为新可申请）。"""
    items: list[dict[str, Any]] = []
    if not text:
        return items
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    skip_re = re.compile(r"(价保成功|已退|已完成|无差价|暂无差价|不可申请|已过价保|不支持价保|申请记录|价保规则)")
    claim_re = re.compile(r"(一键价保|可申请价保|申请价保|立即价保|预计可退\s*[¥￥]?\s*\d|可退差价\s*[¥￥]?\s*\d)")
    money_re = re.compile(r"(?:预计可退|可退差价|可退|降价)\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)")
    order_re = re.compile(r"\b(\d{10,18})\b")

    seen: set[str] = set()
    for idx, ln in enumerate(lines):
        if skip_re.search(ln):
            continue
        if not claim_re.search(ln):
            continue
        window = " ".join(lines[max(0, idx - 2) : min(len(lines), idx + 3)])
        if skip_re.search(window) and "可申请" not in ln and "预计可退" not in ln:
            continue
        m_ord = order_re.search(window)
        m_amt = money_re.search(window)
        ord_id = m_ord.group(1) if m_ord else ""
        amt_cents = _to_cents(m_amt.group(1)) if m_amt else None
        # 忽略没有具体订单号也没有具体可退金额的通用页面顶部「一键价保」按钮文案
        if not ord_id and not (amt_cents and amt_cents >= 100):
            continue
        title = lines[max(0, idx - 1)][:60] if idx > 0 else ln[:60]
        dedup_k = ord_id or f"{title}:{amt_cents}"
        if dedup_k in seen:
            continue
        seen.add(dedup_k)
        items.append(
            {
                "order": ord_id,
                "name": title,
                "refund_cents": amt_cents,
                "snippet": ln[:100],
            }
        )
    return items


def probe_jd_price_protect_readonly(
    db: SimingDB,
    *,
    force: bool = False,
    cdp_probe: Optional[Callable[[], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """经 9222 后台标签严格只读核查京东官方价保页（绝不点击任何申请按钮）。

    目的：
    1. 及时发现京东移动端登录态过期（跳 plogin.m.jd.com），杜绝像 11-21~23 那样静默失效多日；
    2. 捕捉因促销券/满减/PLUS 价触发的官方可退差价（静态商品页 jdPrice 往往不动）。
    """
    db.init_db()
    cur = now_local()
    ts = iso_now(cur)
    prev = _load_official_snapshot(db)
    if not force and cdp_probe is None:
        if os.environ.get("SIMING_TEST_OFFLINE") == "1":
            return prev or {"ok": True, "skipped": "offline"}
        last_at = str(prev.get("checked_at") or "")
        if last_at:
            try:
                if (cur - parse_iso(last_at)).total_seconds() < OFFICIAL_PROBE_INTERVAL_SEC:
                    return prev
            except Exception:
                pass

    if cdp_probe is not None:
        raw_res = cdp_probe() or {}
    else:
        from siming.cdp import inspect_url_in_background_tab

        js = r"""(() => JSON.stringify({
            url: location.href,
            title: document.title,
            text: (document.body && document.body.innerText || '').slice(0, 8000)
        }))()"""
        raw_res = inspect_url_in_background_tab(
            JD_PRICE_PROTECT_URL,
            js,
            wait_sec=4.5,
            close_when_clean=True,
        )

    if raw_res.get("gated"):
        gate = raw_res.get("gate") or {}
        g_kind = str(gate.get("gate_kind") or "login_required")
        snap = {
            "ok": False,
            "auth_expired": g_kind == "login_required" or "login" in str(gate.get("url") or "").lower(),
            "gate_kind": g_kind,
            "reason": str(gate.get("reason") or g_kind),
            "url": str(gate.get("url") or ""),
            "claimable": [],
            "checked_at": ts,
        }
        db.set_meta("price_watch_official_snapshot", json.dumps(snap, ensure_ascii=False))
        return snap

    if not raw_res.get("ok"):
        snap = {
            "ok": False,
            "auth_expired": False,
            "reason": str(raw_res.get("error") or "probe_failed")[:160],
            "claimable": prev.get("claimable") or [],
            "checked_at": ts,
        }
        db.set_meta("price_watch_official_snapshot", json.dumps(snap, ensure_ascii=False))
        return snap

    val = raw_res.get("value")
    if isinstance(val, str):
        try:
            val = json.loads(val)
        except Exception:
            val = {"text": val}
    page = val if isinstance(val, dict) else {}
    page_url = str(page.get("url") or "")
    page_text = str(page.get("text") or "")
    if re.search(r"plogin\.m\.jd|passport\.jd|扫码登录|请先登录", f"{page_url}\n{page_text[:600]}"):
        snap = {
            "ok": False,
            "auth_expired": True,
            "gate_kind": "login_required",
            "reason": "官方价保页跳转登录",
            "url": page_url,
            "claimable": [],
            "checked_at": ts,
        }
        db.set_meta("price_watch_official_snapshot", json.dumps(snap, ensure_ascii=False))
        return snap

    claimable = _parse_official_pp_text(page_text)
    snap = {
        "ok": True,
        "auth_expired": False,
        "claimable": claimable,
        "checked_at": ts,
    }
    db.set_meta("price_watch_official_snapshot", json.dumps(snap, ensure_ascii=False))
    return snap


def list_price_watches(
    db: SimingDB,
    watch_dir: Optional[Path] = None,
    auto_json_path: Optional[Path] = None,
) -> dict[str, Any]:
    """汇总全部保价监控项（含 15 单大家电与后续自添商品），融合 state.json、auto.json 与官方价保页只读快照。"""
    db.init_db()
    w_dir = _resolve_watch_dir(watch_dir)
    orders_doc = _load_json_safe(w_dir / "orders.json", {})
    state_doc = _load_json_safe(w_dir / "state.json", {})
    auto_doc = _load_json_safe(auto_json_path or DEFAULT_AUTO_JSON, {})
    official_snap = _load_official_snapshot(db)

    if not isinstance(orders_doc, dict):
        orders_doc = {}
    if not isinstance(state_doc, dict):
        state_doc = {}

    auto_by_order: dict[str, dict[str, Any]] = {}
    for a_it in (auto_doc.get("items") if isinstance(auto_doc, dict) else None) or []:
        if isinstance(a_it, dict) and a_it.get("order"):
            auto_by_order[str(a_it["order"])] = a_it

    # 官方只读价保快照（仅使用 24h 内的新鲜结果）
    official_by_order: dict[str, dict[str, Any]] = {}
    official_extra: list[dict[str, Any]] = []
    official_fresh = False
    if official_snap.get("ok") and official_snap.get("checked_at"):
        try:
            if (now_local() - parse_iso(str(official_snap["checked_at"]))).total_seconds() <= 86400:
                official_fresh = True
        except Exception:
            official_fresh = False
    if official_fresh:
        for oit in official_snap.get("claimable") or []:
            if not isinstance(oit, dict):
                continue
            oid = str(oit.get("order") or "").strip()
            if oid:
                official_by_order[oid] = oit
            else:
                official_extra.append(oit)

    wm = _load_watermarks(db)
    min_diff_yuan = float(orders_doc.get("min_diff") or 10.0)
    min_diff_cents = _to_cents(min_diff_yuan) or 1000
    today_d = now_local().date()

    items_out: list[dict[str, Any]] = []
    claimable_items: list[dict[str, Any]] = []
    latest_poll_at = ""
    seen_orders: set[str] = set()

    for raw in orders_doc.get("orders") or []:
        if not isinstance(raw, dict):
            continue
        ord_id = str(raw.get("order") or "").strip()
        sku = str(raw.get("sku") or "").strip()
        key = ord_id or sku
        if not key:
            continue
        if ord_id:
            seen_orders.add(ord_id)

        wm_entry = wm.get(key) or {}
        if wm_entry.get("muted"):
            continue

        st_entry = state_doc.get(ord_id) or state_doc.get(sku) or {}
        if not isinstance(st_entry, dict):
            st_entry = {}
        auto_entry = auto_by_order.get(ord_id) or {}
        off_entry = official_by_order.get(ord_id) or {}

        paid_cents = _to_cents(raw.get("paid") or st_entry.get("fact") or st_entry.get("buyPrice")) or 0
        # 页面价基准：国补/优惠券订单的 paid 远低于页面标价 jdPrice；对比页面标价时必须用下单页面价(buyPrice)或首见价(firstSeen)
        raw_base = raw.get("baseline")
        if str(raw_base or "").strip().lower() == "first_seen":
            page_base_cents = _to_cents(st_entry.get("firstSeen") or st_entry.get("buyPrice")) or paid_cents
        else:
            explicit_base_cents = _to_cents(raw_base)
            buy_page_cents = _to_cents(st_entry.get("buyPrice") or st_entry.get("firstSeen"))
            if explicit_base_cents is not None and explicit_base_cents > 0:
                page_base_cents = explicit_base_cents
            elif buy_page_cents is not None and buy_page_cents > 0:
                page_base_cents = buy_page_cents
            else:
                page_base_cents = paid_cents

        # 若此前已退差并登记底价水位（claimed_floor_cents），同时压低页面基准与实付基准，防止反复重报
        claimed_base_cents = wm_entry.get("claimed_floor_cents")
        effective_base_cents = page_base_cents
        paid_floor_cents = paid_cents
        if isinstance(claimed_base_cents, int) and 0 < claimed_base_cents:
            if effective_base_cents <= 0 or claimed_base_cents < effective_base_cents:
                effective_base_cents = claimed_base_cents
            if paid_floor_cents <= 0 or claimed_base_cents < paid_floor_cents:
                paid_floor_cents = claimed_base_cents

        last_obj = st_entry.get("last") if isinstance(st_entry.get("last"), dict) else {}
        last_at = str(last_obj.get("at") or "")
        if last_at and last_at > latest_poll_at:
            latest_poll_at = last_at

        cur_cents = _to_cents(last_obj.get("jdPrice") or last_obj.get("price"))
        raw_promo_cents = _to_cents(
            last_obj.get("finalPrice") or last_obj.get("promoPrice") or last_obj.get("afterCouponPrice")
        )
        # 防误抓促销文案小数字（如 "PLUS 1元" / "补贴 180元"）：低于页面标价 45% 的 promoPrice 一律忽略
        if raw_promo_cents is not None and cur_cents is not None and cur_cents > 0:
            promo_cents = raw_promo_cents if (int(cur_cents * 0.45) <= raw_promo_cents < cur_cents) else None
        else:
            promo_cents = raw_promo_cents
        auto_pp = auto_entry.get("priceProtect") if isinstance(auto_entry.get("priceProtect"), dict) else {}
        if cur_cents is None and auto_pp.get("currentPrice"):
            cur_cents = _to_cents(auto_pp.get("currentPrice"))

        pp_days = int(raw.get("pp_days") or st_entry.get("ppDays") or 30)
        buy_date_str = str(raw.get("date") or "")
        days_left: Optional[int] = None
        expired = False
        if buy_date_str:
            try:
                b_d = date.fromisoformat(buy_date_str[:10])
                days_left = pp_days - (today_d - b_d).days
                expired = days_left < 0
            except ValueError:
                days_left = None

        page_diff_cents = (effective_base_cents - cur_cents) if (cur_cents is not None and effective_base_cents > 0) else 0
        promo_diff_cents = (paid_floor_cents - promo_cents) if (promo_cents is not None and paid_floor_cents > 0) else 0
        official_refund_cents = int(off_entry.get("refund_cents") or 0) if off_entry else 0
        diff_cents = max(page_diff_cents, promo_diff_cents, official_refund_cents)
        effective_cur_cents = promo_cents if (promo_diff_cents > page_diff_cents and promo_cents is not None) else cur_cents

        auto_claimable_flag = bool(
            off_entry
            or auto_pp.get("claimable")
            or str(auto_pp.get("status") or "") in ("可申请", "可价保")
        )
        is_claimable = (not expired) and (
            (effective_cur_cents is not None and diff_cents >= min_diff_cents)
            or (official_refund_cents >= min_diff_cents)
            or auto_claimable_flag
        )

        if expired:
            status_label = "已过保价期"
        elif off_entry:
            r_s = f"预计可退 ¥{official_refund_cents / 100:.2f}" if official_refund_cents > 0 else "官方价保页显示可申请"
            status_label = f"🔥 {r_s}"
        elif is_claimable:
            status_label = f"🔥 可申请退差 ¥{max(diff_cents, 0) / 100:.2f}" if diff_cents > 0 else "🔥 检测到可申请价保"
        elif effective_cur_cents is not None:
            status_label = "监控中·未降价" if diff_cents <= 0 else f"微降 ¥{diff_cents / 100:.2f} (<¥{min_diff_yuan:.0f})"
        else:
            status_label = "监控中·待抓现价"

        rec = {
            "key": key,
            "order": ord_id,
            "sku": sku,
            "name": str(raw.get("name") or sku),
            "buy_date": buy_date_str,
            "pp_days": pp_days,
            "days_left": days_left,
            "expired": expired,
            "paid_cents": paid_cents,
            "page_base_cents": page_base_cents,
            "effective_base_cents": effective_base_cents,
            "current_cents": effective_cur_cents,
            "promo_cents": promo_cents,
            "diff_cents": diff_cents,
            "official_claimable": bool(off_entry),
            "claimable": is_claimable,
            "status_label": status_label,
            "last_checked_at": last_at,
        }
        items_out.append(rec)
        if is_claimable:
            claimable_items.append(rec)

    # 合并官方价保页新发现但不在 orders.json 的订单（例如近期新买的京东商品）
    for oid, oit in official_by_order.items():
        if oid in seen_orders:
            continue
        wm_entry = wm.get(oid) or {}
        if wm_entry.get("muted"):
            continue
        ref_c = int(oit.get("refund_cents") or 0)
        rec = {
            "key": oid,
            "order": oid,
            "sku": "",
            "name": str(oit.get("name") or f"京东订单 {oid}"),
            "buy_date": "",
            "pp_days": 30,
            "days_left": None,
            "expired": False,
            "paid_cents": 0,
            "page_base_cents": 0,
            "effective_base_cents": 0,
            "current_cents": None,
            "promo_cents": None,
            "diff_cents": ref_c,
            "official_claimable": True,
            "claimable": True,
            "status_label": f"🔥 官方价保页可退 ¥{ref_c / 100:.2f}" if ref_c > 0 else "🔥 官方价保页显示可申请",
            "last_checked_at": str(official_snap.get("checked_at") or ""),
        }
        items_out.append(rec)
        claimable_items.append(rec)

    # 上游冷却/登录健康状态
    risk_until = str(state_doc.get("_riskUntil") or "")
    blocked_reason = str(state_doc.get("_blockedReason") or "")
    upstream_in_cooldown = False
    if risk_until:
        try:
            upstream_in_cooldown = parse_iso(risk_until.replace("Z", "+00:00")) > now_local()
        except Exception:
            upstream_in_cooldown = False

    active_cnt = sum(1 for x in items_out if not x["expired"])
    return {
        "ok": True,
        "watch_dir": str(w_dir),
        "min_diff_yuan": min_diff_yuan,
        "latest_poll_at": latest_poll_at,
        "total": len(items_out),
        "active_count": active_cnt,
        "claimable_count": len(claimable_items),
        "claimable_items": claimable_items,
        "items": items_out,
        "upstream_cooldown": upstream_in_cooldown,
        "upstream_blocked_reason": blocked_reason,
        "upstream_risk_until": risk_until,
        "official_auth_expired": bool(official_snap.get("auth_expired")),
        "official_checked_at": str(official_snap.get("checked_at") or ""),
    }


def collect_price_watch(
    db: SimingDB,
    watch_dir: Optional[Path] = None,
    auto_json_path: Optional[Path] = None,
    official_cdp_probe: Optional[Callable[[], dict[str, Any]]] = None,
    relogin_runner: Optional[Callable[[SimingDB, str], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """由 siming tick 调用：核查官方价保页只读状态 + 本地保价池水位变化；
    登录过期时自动尝试本人授权手机号 + 81 瞬时短信验证码续登（遇滑块即停手交由本人）。
    """
    db.init_db()
    if not is_module_enabled(db, "price_watch"):
        db.mark_source_ok("price_watch", detail="module disabled by user")
        return {"ok": True, "disabled": True, "alerted": 0}

    official_res = probe_jd_price_protect_readonly(db, cdp_probe=official_cdp_probe)
    relogin_res: dict[str, Any] = {}
    if official_res.get("auth_expired"):
        from siming.otp_vault import attempt_auto_platform_relogin

        relogin_res = attempt_auto_platform_relogin(db, "jd", relogin_runner=relogin_runner)
        if relogin_res.get("ok"):
            # 自动短信续登成功：立刻复测官方价保页并顺手触发 buy-track 刷新
            official_res = probe_jd_price_protect_readonly(db, force=True, cdp_probe=official_cdp_probe)
            try:
                from siming.collectors.buy_track import kick_buy_track_sync_if_needed

                kick_buy_track_sync_if_needed(db, user_recovered=True)
            except Exception:
                pass
            today_ok = now_local().date().isoformat()
            if db.get_meta("jd_auto_relogin_ok_day") != today_ok:
                ok_msg = (
                    "✅ 检测到京东登录态过期，司命已自动用你的授权手机号接短信验证码续登成功，并已触发订单与价保刷新（无需手动扫码）。"
                )
                with db.transaction() as conn:
                    db.enqueue_effect(
                        conn,
                        "send_qq",
                        {
                            "ref_id": f"jd_auto_relogin_ok:{today_ok}",
                            "rule_id": "price_protect_auth",
                            "text": ok_msg,
                        },
                    )
                    db.set_meta("jd_auto_relogin_ok_day", today_ok, conn=conn)

    summary = list_price_watches(db, watch_dir=watch_dir, auto_json_path=auto_json_path)
    wm = _load_watermarks(db)
    alerted_cnt = 0
    ts = iso_now()

    for it in summary["claimable_items"]:
        key = it["key"]
        cur_cents = it["current_cents"]
        diff_cents = int(it["diff_cents"] or 0)
        wm_entry = wm.setdefault(key, {})
        prev_alerted = wm_entry.get("alerted_floor_cents")

        # 只有首次达标，或比上次已提醒水位再继续跌至少 ¥1.00 时才发新提醒，彻底杜绝每小时重复轰炸！
        if isinstance(prev_alerted, int):
            if cur_cents is not None and cur_cents > prev_alerted - 100:
                continue
            if cur_cents is None and wm_entry.get("alerted_diff_cents") == diff_cents:
                continue

        cur_str = f"¥{cur_cents / 100:.2f}" if cur_cents is not None else "见官方价保页"
        base_str = f"¥{it['effective_base_cents'] / 100:.2f}" if it["effective_base_cents"] > 0 else "订单实付"
        diff_str = f"¥{diff_cents / 100:.2f}" if diff_cents > 0 else "待确认差额"
        d_left_s = f"剩余 {it['days_left']} 天" if it["days_left"] is not None else f"{it['pp_days']}天价保"
        src_hint = "（京东官方价保页直出）" if it.get("official_claimable") else ""

        with db.transaction() as conn:
            existing = conn.execute(
                "SELECT case_id FROM cases WHERE category='price_protect' AND status IN ('待处理', '待核实') AND context_json LIKE ? LIMIT 1",
                (f'%"{key}"%',),
            ).fetchone()
            ctx_payload = json.dumps(
                {
                    "order_key": key,
                    "order": it["order"],
                    "sku": it["sku"],
                    "current_cents": cur_cents,
                    "effective_base_cents": it["effective_base_cents"],
                    "diff_cents": diff_cents,
                },
                ensure_ascii=False,
            )
            title = f"保价退差提醒：{it['name']} 可退 {diff_str}"
            next_step = f"京东 App -> 我的 -> 客户服务 -> 价格保护（订单 {it['order']} · {d_left_s}）"
            if existing:
                case_id = str(existing["case_id"])
                conn.execute(
                    """
                    UPDATE cases
                    SET title=?, next_step=?, context_json=?, state_version=state_version+1, updated_at=?
                    WHERE case_id=?
                    """,
                    (title[:120], next_step[:240], ctx_payload, ts, case_id),
                )
            else:
                case_id = db.next_seq_id(conn, "S")
                conn.execute(
                    """
                    INSERT INTO cases (
                        case_id, title, next_step, done_rule, status, strength,
                        category, rule_id, context_json, created_at, updated_at
                    ) VALUES (?, ?, ?, '本人点申请退差后回「已保价」或「不看了」', '待处理', 'NORMAL', 'price_protect', 'price_protect_watch', ?, ?, ?)
                    """,
                    (case_id, title[:120], next_step[:240], ctx_payload, ts, ts),
                )

            msg = (
                f"💰 [{case_id}] 保价降价提醒{src_hint}：{it['name']}\n"
                f"• 基准价：{base_str} -> 当前现价：{cur_str}（预计可退 **{diff_str}** · {d_left_s}）\n"
                f"• 订单/SKU：`{it['order']}` / `{it['sku'] or '-'}`\n"
                "• 操作：打开京东 App「我的 -> 客户服务 -> 价格保护」点一键申请；退完引用本条回「已保价」（更新底价继续盯二次降价）或「不看了」。"
            )
            db.enqueue_effect(
                conn,
                "send_qq",
                {
                    "ref_id": case_id,
                    "rule_id": "price_protect_watch",
                    "text": msg,
                },
            )
            wm_entry["alerted_floor_cents"] = cur_cents if cur_cents is not None else 0
            wm_entry["alerted_diff_cents"] = diff_cents
            wm_entry["alerted_at"] = ts
            wm_entry["case_id"] = case_id
            _save_watermarks(db, wm, conn=conn)
            alerted_cnt += 1

    # 健康度校验：京东官方价保页登录过期 / 上游风控冷却 / 活跃单据长时间未刷新 -> 明确标错并每日提醒一次
    health_issues: list[str] = []
    auth_expired = bool(official_res.get("auth_expired")) or bool(summary.get("official_auth_expired"))
    if auth_expired:
        health_issues.append("京东官方价保页跳转登录（移动端未登录，无法读取券后到手价与一键价保可退差价）")
    if summary.get("upstream_cooldown"):
        health_issues.append(
            f"buy-price-watch 上游处于风控/冷却期（原因：{summary.get('upstream_blocked_reason') or 'unknown'}，至 {summary.get('upstream_risk_until')[:16]}）"
        )
    if watch_dir is None and not os.environ.get("SIMING_PRICE_WATCH_DIR") and summary["active_count"] > 0 and summary["latest_poll_at"]:
        try:
            age_h = (now_local() - parse_iso(summary["latest_poll_at"].replace("Z", "+00:00"))).total_seconds() / 3600.0
            if age_h > ACTIVE_STALE_HOURS:
                health_issues.append(f"在保商品已 {age_h:.1f} 小时未刷新价格")
        except Exception:
            pass

    detail = (
        f"watching={summary['active_count']}/{summary['total']}, "
        f"claimable={summary['claimable_count']}, alerted={alerted_cnt}"
    )
    if health_issues:
        err_detail = "; ".join(health_issues)
        db.mark_source_error("price_watch", "AUTH_EXPIRED" if auth_expired else "ERROR", err_detail)
        today_s = now_local().date().isoformat()
        if db.get_meta("price_watch_health_alert_day") != today_s:
            if relogin_res.get("gated"):
                action_hint = (
                    "• 自动续登进展：司命已自动填入你的授权手机号并请求短信，但京东弹出**滑块验证**（已按合规红线停手并保留 Chromium 后台标签页）。\n"
                    "• 下一步：切到主 Chromium 滑一下，收到短信后在 QQ 回 `填验证码 京东` 即可自动完成。"
                )
            elif relogin_res.get("attempted") and not relogin_res.get("ok"):
                action_hint = (
                    f"• 自动续登进展：司命已自动尝试用授权手机号短信登录但未完成（{str(relogin_res.get('reply') or '超时/失败')[:140]}）。\n"
                    "• 下一步：在 QQ 回 `验证码登录 京东`（或滑完滑块后回 `填验证码 京东`）重试，或在主 Chromium 登录京东。"
                )
            else:
                action_hint = (
                    "• 修复建议：在 QQ 回 `验证码登录 京东` 触发自动短信登录，或在主 Chromium 刷新登录态。"
                )
            warn_msg = (
                "🔐 **网购保价监控受阻预警**\n"
                + "\n".join(f"• {h}" for h in health_issues)
                + "\n"
                + action_hint
            )
            with db.transaction() as conn:
                db.enqueue_effect(
                    conn,
                    "send_qq",
                    {
                        "ref_id": f"price_watch_health:{today_s}",
                        "rule_id": "price_protect_auth",
                        "text": warn_msg,
                    },
                )
                db.set_meta("price_watch_health_alert_day", today_s, conn=conn)
    else:
        db.mark_source_ok("price_watch", cursor=summary["latest_poll_at"] or ts, detail=detail)

    return {
        "ok": not health_issues,
        "watching": summary["active_count"],
        "total": summary["total"],
        "claimable": summary["claimable_count"],
        "alerted": alerted_cnt,
        "auth_expired": auth_expired,
        "auto_relogin": relogin_res,
        "health_issues": health_issues,
    }


def format_price_watch_report(
    db: SimingDB,
    watch_dir: Optional[Path] = None,
    auto_json_path: Optional[Path] = None,
) -> str:
    rep = list_price_watches(db, watch_dir=watch_dir, auto_json_path=auto_json_path)
    poll_s = rep["latest_poll_at"][:16].replace("T", " ") if rep["latest_poll_at"] else "待首轮巡检"
    lines = [
        f"🏷️ 网购保价监控池（在保 {rep['active_count']}/{rep['total']} 单 · 阈值 ≥¥{rep['min_diff_yuan']:.0f} · 最近抓价 {poll_s}）",
    ]
    if rep.get("official_auth_expired"):
        lines.append("⚠️ 京东官方价保页登录态已过期（无法读取券后到手价与一键价保可退差价），请在 9222 浏览器重登京东。")
    if rep.get("upstream_cooldown"):
        lines.append(f"⏸ 上游抓价处于冷却期（{rep.get('upstream_blocked_reason') or 'unknown'}，至 {str(rep.get('upstream_risk_until') or '')[:16]}）。")
    if rep["claimable_items"]:
        lines.append("【立即可申请退差】")
        for it in rep["claimable_items"]:
            cur_s = f"¥{it['current_cents']/100:.2f}" if it["current_cents"] is not None else "-"
            lines.append(
                f"• 🔥 {it['name']}（订单 `{it['order']}`）：基准 ¥{it['effective_base_cents']/100:.2f} -> 现价 {cur_s} ｜ {it['status_label']}"
            )

    active_others = [x for x in rep["items"] if not x["expired"] and not x["claimable"]]
    if active_others:
        lines.append("【在保监控中】")
        for it in active_others[:18]:
            cur_s = f"¥{it['current_cents']/100:.2f}" if it["current_cents"] is not None else "待抓"
            d_s = f"剩{it['days_left']}d" if it["days_left"] is not None else f"{it['pp_days']}d"
            paid_note = (
                f" / 实付 ¥{it['paid_cents']/100:.2f}"
                if it.get("paid_cents") and it["paid_cents"] != it["effective_base_cents"]
                else ""
            )
            lines.append(
                f"• {it['name']}（SKU `{it['sku']}` · {d_s}）：基准 ¥{it['effective_base_cents']/100:.2f}{paid_note} ｜ 现价 {cur_s}（{it['status_label']}）"
            )

    expired_cnt = sum(1 for x in rep["items"] if x["expired"])
    if expired_cnt:
        lines.append(f"• 已自然过保：{expired_cnt} 单（自动静默不再巡检提醒）")
    lines.append("提示：后续新买商品可随时发 `保价监控 <商品名> <SKU或链接> <实付价>`（或 `siming price-watch add`）加入。")
    return "\n".join(lines)
