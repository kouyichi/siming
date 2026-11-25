"""美团外卖投机推理与下单预操作引擎（meituan_speculate）：
- 在展示位置实时推荐的同时，后台通过独立进程 + 有界并行线程池（默认 max_workers=3）对 Top N 候选店铺并行拉取美团 H5 实时菜单：
  1. 熟店优先：对照 `shop_order_items` 真实历史常点菜品组合，在实时菜单中核验在售与现价，保留单人合理份量；
  2. 新店推演：从实时菜单剔除多人套餐/售罄/餐具/凑单陷阱，按单人主食适配度、月售、招牌标签与一键直加（`+`）稳定性推演最优菜品组合，不足起送价自动补配主食/小菜；
  3. 预操作就绪：缓存直达店铺菜单页 `menu_url`（含 `poi_id_str` 与同步坐标）、精确菜品清单与预估总价至 `meta['meituan_speculative_batch']`（20 分钟有效），**投机阶段严格只读、绝不改购物车、绝不下单付款**；
- 当用户在 QQ 明确回复「确认第N家 / 就点第N家」（或先看「第N家」确认卡再回「确认 / apply Axx」）时，直接跳到预解析 `menu_url`，通过 CDP 真实鼠标点击将预推菜品逐项加入购物车并校验实时车内金额，停在付款前交由用户一键结算。
"""

from __future__ import annotations

import json
import math
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from siming.cdp import LocalWsCdpClient, detect_compliance_gate, resolve_chromium_cdp_base
from siming.config import load_policy
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local, parse_iso

SPECULATIVE_META_KEY = "meituan_speculative_batch"
SPECULATIVE_TTL_MINUTES = 20
COUPONS_CACHE_META_KEY = "meituan_account_coupons_cache"
COUPONS_CACHE_TTL_MINUTES = 45

MEITUAN_H5_COUPONLIST = "https://h5.waimai.meituan.com/waimai/mindex/couponlist"
MEITUAN_H5_POICOUPONLIST = "https://h5.waimai.meituan.com/waimai/mindex/poicouponlist"

_NON_FOOD_COUPON_RE = re.compile(
    r"(闪购|便利店|服饰|暖衣|数码|日百|松鼠|大药房|医药|买药|健康守护|买菜|小象|超市|鲜花|水果专享|果切专享|宠物|母婴|美妆|酒店|机票|火车票|KTV|洗浴|按摩|医美|丽人)"
)
_COUPON_EXPIRY_RE = re.compile(r"有效期至\s*(\d{4}\.\d{2}\.\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)")
_COUPON_THRESHOLD_RE = re.compile(r"满\s*(\d+(?:\.\d+)?)\s*可用")
_COUPON_TIME_WIN_RE = re.compile(r"(\d{2}:\d{2})\s*[-~至]\s*(\d{2}:\d{2})")
_STORE_MANJIAN_RE = re.compile(r"(?<!返)(?<!返满)(?:满)?\s*(\d{2,3})\s*减\s*(\d+(?:\.\d+)?)")
_STORE_CLAIM_COUPON_RE = re.compile(r"领\s*(\d+(?:\.\d+)?)\s*元券")
_STORE_REBATE_RE = re.compile(r"(返满\d+减\d+(?:商家代金券)?|返\d+(?:\.\d+)?元券)")
_STORE_DISCOUNT_TAG_RE = re.compile(r"(\d+(?:\.\d+)?折起)")

_UTENSIL_RE = re.compile(
    r"(餐具|筷勺|筷子|勺子|打包袋|塑料袋|纸巾|湿巾|手套|环保袋|餐盒费|不要餐具|需要餐具|无需餐具|备注"
    r"|点击右上角|收藏领券|关注领券|好评返现|收藏店铺|活动说明|勿拍|温馨提示|下单必看|联系门店)"
)
_MULTI_PERSON_RE = re.compile(r"([2-9]\s*[-~至到]\s*[3-9]\s*人|[2-9]\s*人份|[2-9]\s*人餐|双人|三人|四人|多人套餐|大团聚|家庭餐|聚会餐|团餐|分享装)")
_SOLD_OUT_RE = re.compile(r"(售罄|已抢光|今日售完|暂不可售|已下架|补货中)")
_SOLO_MEAL_BOOST_RE = re.compile(
    r"(盖码饭|盖浇饭|拌饭|打抛饭|卤肉饭|炒饭|烩饭|套餐|单人|1人份|必点|招牌|牛肉饭|鸡排饭|面|粉|米线|馄饨|饺子|汉堡|堡|粥|煲|春饼|小炒|冒菜)",
    re.IGNORECASE,
)
_SIDE_OR_STAPLE_RE = re.compile(r"(米饭|白饭|五常|馒头|花卷|春饼|荷包蛋|煎蛋|卤蛋|薯条|小菜|拌菜|青菜|酸奶|豆浆|可乐|雪碧|凉茶|汤|例汤)")
_PURE_SIDE_PRIMARY_PENALTY_RE = re.compile(r"^(?:米饭|白米饭|五常米饭|煎蛋|荷包蛋|卤蛋|加蛋|加面|加粉|加饭|可乐|雪碧|矿泉水|酸奶|原味酸奶|小料|蘸料|纸巾|餐具)")
_DRINK_ITEM_RE = re.compile(r"(美式|拿铁|咖啡|摩卡|卡布|澳白|奶茶|果茶|柠檬茶|轻乳茶|抹茶|酸奶|气泡|冰萃|芝士|杨枝甘露|可可)", re.IGNORECASE)
_MONTHLY_SALES_RE = re.compile(r"月售\s*(\d+)")
_PRICE_ALL_RE = re.compile(r"[¥￥]\s*(\d+(?:\.\d{1,2})?)")


def _norm_dish(name: str) -> str:
    s = (name or "").strip().lower()
    # 剥离常见装饰括号与 emoji/符号以便历史菜名匹配实时菜单
    s = re.sub(r"【[^】]*】|\[[^\]]*\]", "", s)
    s = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", s)
    return s


def _norm_shop(name: str) -> str:
    s = (name or "").strip().lower()
    s = s.replace("（", "(").replace("）", ")").replace("·", "").replace("•", "").replace("●", "")
    return re.sub(r"\s+", "", s)


def _brand_stem(name: str) -> str:
    raw = (name or "").strip().lower().replace("（", "(").replace("）", ")")
    raw = re.sub(r"\([^)]*\)$", "", raw).strip()
    parts = [p.strip() for p in re.split(r"[·•●\-—/|]", raw) if p.strip()]
    base = parts[0] if parts and len(parts[0]) >= 2 else "".join(parts)
    return re.sub(r"\s+", "", base)


def load_shop_historical_dish_combos(
    db: SimingDB,
    shop_name: str,
    limit_orders: int = 8,
) -> dict[str, Any]:
    """从 `shop_order_items` 读取该店铺（或同品牌）最近真实点过的菜品组合（自动剔除餐具与退款单）。"""
    db.init_db()
    target_norm = _norm_shop(shop_name)
    target_brand = _brand_stem(shop_name)
    if not target_norm:
        return {"has_history": False, "last_combo": [], "top_dishes": []}

    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT shop, ordered_at, order_status, order_amount_cents, item_name, qty
            FROM shop_order_items
            WHERE platform = 'meituan'
            ORDER BY ordered_at DESC
            LIMIT 800
            """
        ).fetchall()
    finally:
        conn.close()

    by_order: dict[str, dict[str, Any]] = {}
    dish_freq: dict[str, int] = defaultdict(int)
    dish_display: dict[str, str] = {}

    for r in rows:
        st = str(r["order_status"] or "")
        if any(k in st for k in ("取消", "已退款", "退款成功", "全额退款")):
            continue
        r_shop = str(r["shop"] or "")
        r_norm = _norm_shop(r_shop)
        r_brand = _brand_stem(r_shop)
        exact_match = (r_norm == target_norm)
        brand_match = bool(len(target_brand) >= 2 and r_brand == target_brand)
        if not (exact_match or brand_match):
            continue

        item_name = str(r["item_name"] or "").strip()
        if not item_name or _UTENSIL_RE.search(item_name):
            continue
        qty = max(1, int(r["qty"] or 1))
        occ = str(r["ordered_at"] or "")
        okey = f"{occ}|{r_shop}"
        grp = by_order.setdefault(
            okey,
            {
                "shop": r_shop,
                "exact": exact_match,
                "ordered_at": occ,
                "amount_yuan": round(int(r["order_amount_cents"] or 0) / 100.0, 1),
                "items": [],
            },
        )
        grp["items"].append({"name": item_name, "qty": qty})
        dn = _norm_dish(item_name)
        if dn:
            dish_freq[dn] += 1
            dish_display.setdefault(dn, item_name)

    if not by_order:
        return {"has_history": False, "last_combo": [], "top_dishes": []}

    # 优先最近同分店订单，其次同品牌最近订单
    orders_sorted = sorted(by_order.values(), key=lambda x: (x["exact"], x["ordered_at"]), reverse=True)[:limit_orders]
    best_order = orders_sorted[0]
    top_dishes = [
        {"name": dish_display[dn], "freq": cnt}
        for dn, cnt in sorted(dish_freq.items(), key=lambda kv: kv[1], reverse=True)[:6]
    ]
    preview_str = " + ".join(
        f"{it['name']}" + (f"×{it['qty']}" if it["qty"] > 1 else "")
        for it in best_order["items"][:3]
    )
    return {
        "has_history": True,
        "order_count": len(orders_sorted),
        "last_ordered_at": best_order["ordered_at"],
        "last_amount_yuan": best_order["amount_yuan"],
        "last_combo": best_order["items"][:5],
        "top_dishes": top_dishes,
        "preview": preview_str,
    }


def parse_meituan_menu_cards(
    raw_cards: list[dict[str, Any]],
    cart_bar_text: str = "",
) -> dict[str, Any]:
    """将美团 H5 店铺点菜页提取出的原始 DOM 卡片与底栏解析为结构化菜品列表与起送/配送费。"""
    min_order: Optional[float] = None
    delivery_fee: Optional[float] = None
    m_min = re.search(r"[¥￥]\s*(\d+(?:\.\d+)?)\s*起送", cart_bar_text or "")
    if m_min:
        min_order = float(m_min.group(1))
    m_del = re.search(r"配送费\s*(?:约)?\s*[¥￥]\s*(\d+(?:\.\d+)?)", cart_bar_text or "")
    if m_del:
        delivery_fee = float(m_del.group(1))
    elif "免配送费" in (cart_bar_text or ""):
        delivery_fee = 0.0

    items: list[dict[str, Any]] = []
    seen_norm: set[str] = set()
    for card in raw_cards or []:
        if not isinstance(card, dict):
            continue
        lines = [str(x).strip() for x in (card.get("lines") or []) if str(x).strip()]
        if not lines:
            continue
        name = lines[0]
        if len(name) < 2 or name.startswith(("¥", "￥", "月售")):
            continue
        norm_n = _norm_dish(name)
        if not norm_n or norm_n in seen_norm:
            continue
        joined = "\n".join(lines)

        prices = [float(m.group(1)) for m in _PRICE_ALL_RE.finditer(joined)]
        if not prices:
            continue
        price = prices[0]
        orig_price = prices[1] if len(prices) > 1 and prices[1] >= price else None

        sales = 0
        m_s = _MONTHLY_SALES_RE.search(joined)
        if m_s:
            sales = int(m_s.group(1))

        portion = ""
        for ln in lines[1:4]:
            if re.fullmatch(r"\d+(?:-\d+)?人份", ln):
                portion = ln
                break

        is_multi_person = bool(_MULTI_PERSON_RE.search(name + " " + portion))
        is_sold_out = bool(_SOLD_OUT_RE.search(joined))
        is_utensil = bool(_UTENSIL_RE.search(name))
        is_tiny_promo = bool(
            (price < 3.0 and ("限1份" in joined or "超值购" in name or "限量" in name))
            or (price < 6.0 and any(k in joined for k in ("多点不送", "单点不送", "只限购", "仅限购", "随餐附赠")))
        )
        has_plus = bool(card.get("hasPlus"))

        seen_norm.add(norm_n)
        items.append(
            {
                "name": name[:56],
                "norm": norm_n,
                "price": round(price, 2),
                "original_price": round(orig_price, 2) if orig_price is not None else None,
                "monthly_sales": sales,
                "portion": portion,
                "has_plus": has_plus,
                "is_multi_person": is_multi_person,
                "is_sold_out": is_sold_out,
                "is_utensil": is_utensil,
                "is_tiny_promo": is_tiny_promo,
                "desc": " ".join(lines[1:3])[:80],
            }
        )

    return {
        "min_order": min_order,
        "delivery_fee": delivery_fee,
        "items": items,
    }


def _match_live_dish(hist_name: str, live_items: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    hn = _norm_dish(hist_name)
    if not hn:
        return None
    # 1) 精确归一化命中
    for it in live_items:
        if it["norm"] == hn:
            return it
    # 2) 包含匹配（优先长度最接近且有 direct `+`）
    sub_hits = [it for it in live_items if (len(hn) >= 2 and hn in it["norm"]) or (len(it["norm"]) >= 2 and it["norm"] in hn)]
    if sub_hits:
        sub_hits.sort(key=lambda x: (not x["has_plus"], abs(len(x["norm"]) - len(hn)), -x["monthly_sales"]))
        return sub_hits[0]
    # 3) 核心汉字重叠率 >= 0.65（如 `招牌打抛饭鸡肉` vs `泰式打抛鸡肉饭`）
    h_chars = set(re.findall(r"[\u4e00-\u9fff]", hn))
    if len(h_chars) >= 3:
        best: Optional[tuple[float, dict[str, Any]]] = None
        for it in live_items:
            l_chars = set(re.findall(r"[\u4e00-\u9fff]", it["norm"]))
            if not l_chars:
                continue
            ov = len(h_chars & l_chars) / float(max(1, min(len(h_chars), len(l_chars))))
            if ov >= 0.65 and (best is None or ov > best[0]):
                best = (ov, it)
        if best is not None:
            return best[1]
    return None


def infer_dishes_for_shop(
    shop_name: str,
    menu_data: dict[str, Any],
    history_combo: Optional[dict[str, Any]] = None,
    cur_slot: str = "晚餐",
    max_budget: Optional[float] = None,
    max_yuan: float = 120.0,
    *,
    store_promos: Optional[dict[str, Any]] = None,
    account_coupons: Optional[dict[str, Any]] = None,
    now_dt: Optional[datetime] = None,
) -> dict[str, Any]:
    """根据店铺实时菜单 + 用户个人历史常点组合，推演单人可直接下单的最优菜品清单与预估总价。"""
    raw_items = list(menu_data.get("items") or [])
    min_order = float(menu_data.get("min_order") or 0.0)
    delivery_fee = float(menu_data.get("delivery_fee") or 0.0)
    cap_limit = min(float(max_yuan), float(max_budget) + 8.0) if max_budget is not None else float(max_yuan)

    usable = [
        it
        for it in raw_items
        if not it.get("is_sold_out") and not it.get("is_utensil") and not it.get("is_multi_person")
    ]
    if not usable:
        return {
            "ok": False,
            "reason": "未解析到可用单人菜品",
            "dishes": [],
            "min_order": min_order,
            "delivery_fee": delivery_fee,
            "est_subtotal": 0.0,
            "est_total": 0.0,
        }

    chosen: list[dict[str, Any]] = []
    chosen_norms: set[str] = set()
    plan_source = "live_menu_signature"
    reason_parts: list[str] = []

    def _add_dish(it: dict[str, Any], qty: int = 1) -> None:
        if it["norm"] in chosen_norms:
            return
        chosen_norms.add(it["norm"])
        chosen.append(
            {
                "name": it["name"],
                "qty": max(1, int(qty)),
                "price": round(float(it["price"]), 2),
                "has_plus": bool(it.get("has_plus")),
            }
        )

    # 1) 熟店优先：在实时菜单中核验用户上次/常点的菜品组合
    hist = history_combo or {}
    if hist.get("has_history") and hist.get("last_combo"):
        running_sub = 0.0
        for h_item in hist["last_combo"]:
            matched = _match_live_dish(str(h_item.get("name") or ""), usable)
            if matched is None:
                continue
            qty = min(2, max(1, int(h_item.get("qty") or 1)))
            add_cost = matched["price"] * qty
            if running_sub + add_cost + delivery_fee > cap_limit and chosen:
                continue
            _add_dish(matched, qty=qty)
            running_sub += add_cost
            if len(chosen) >= 3:
                break
        # 若上次组合未命中实时菜单，再尝试该店高频常点单品
        if not chosen and hist.get("top_dishes"):
            for td in hist["top_dishes"]:
                matched = _match_live_dish(str(td.get("name") or ""), usable)
                if matched is not None and matched["price"] + delivery_fee <= cap_limit:
                    _add_dish(matched, qty=1)
                    if len(chosen) >= 2:
                        break
        if chosen:
            chosen.sort(key=lambda d: (bool(_PURE_SIDE_PRIMARY_PENALTY_RE.search(d["name"])), -float(d["price"])))
            plan_source = "history_verified_live"
            last_md = str(hist.get("last_ordered_at") or "")[5:10]
            reason_parts.append(f"沿用常点组合" + (f"(上次{last_md})" if last_md else "") + "并核验实时在售")

    # 2) 新店、历史菜已下架、或历史仅命中配菜/小料（无单价>=12的主菜）：从实时菜单补选热销单人主菜
    has_real_main = any(float(d["price"]) >= (8.0 if cur_slot == "下午茶/咖啡" else 12.0) for d in chosen)
    if not chosen or not has_real_main:
        is_tea_slot = cur_slot == "下午茶/咖啡"
        scored_primary: list[tuple[float, dict[str, Any]]] = []
        for it in usable:
            if it["norm"] in chosen_norms or it.get("is_tiny_promo"):
                continue
            p = float(it["price"])
            if p + sum(d["price"] * d["qty"] for d in chosen) + delivery_fee > cap_limit:
                continue
            nm = str(it["name"])
            sc = math.log1p(max(0, int(it.get("monthly_sales") or 0))) * 1.4
            if it.get("has_plus"):
                sc += 2.6
            if it.get("portion") == "1人份":
                sc += 1.5
            if is_tea_slot:
                if _DRINK_ITEM_RE.search(nm):
                    sc += 4.5
                if 10.0 <= p <= 45.0:
                    sc += 1.5
            else:
                if _SOLO_MEAL_BOOST_RE.search(nm):
                    sc += 3.8
                if _PURE_SIDE_PRIMARY_PENALTY_RE.search(nm):
                    sc -= 6.0
                if 18.0 <= p <= 68.0:
                    sc += 2.2
                elif p < 12.0:
                    sc -= 3.5
                elif p > 88.0:
                    sc -= 2.0
            scored_primary.append((sc, it))

        scored_primary.sort(key=lambda x: (x[0], x[1]["monthly_sales"]), reverse=True)
        if scored_primary:
            top_dish = scored_primary[0][1]
            had_hist_sides = bool(chosen)
            _add_dish(top_dish, qty=1)
            chosen.sort(key=lambda d: (bool(_PURE_SIDE_PRIMARY_PENALTY_RE.search(d["name"])), -float(d["price"])))
            sales_s = f"月售{top_dish['monthly_sales']}" if top_dish.get("monthly_sales") else "门店主推"
            if had_hist_sides:
                reason_parts.append(f"补配单人热销主菜({sales_s})")
            else:
                reason_parts.append(f"单人热销主菜({sales_s})")

    # 3) 起送价自动补齐：若已选菜品金额低于起送价，补 1 份最合适的主食/小菜/饮品
    subtotal = round(sum(d["price"] * d["qty"] for d in chosen), 2)
    if chosen and min_order > 0 and subtotal < min_order:
        gap = min_order - subtotal
        remaining_cap = cap_limit - delivery_fee - subtotal
        topup_cands: list[tuple[float, dict[str, Any]]] = []
        for it in usable:
            if it["norm"] in chosen_norms or it.get("is_tiny_promo"):
                continue
            p = float(it["price"])
            if p <= 0 or p > remaining_cap:
                continue
            sc = 0.0
            if p >= gap:
                sc += 4.0 - min(3.0, (p - gap) / 15.0)
            else:
                sc += 1.5
            if it.get("has_plus"):
                sc += 2.0
            if _SIDE_OR_STAPLE_RE.search(it["name"]):
                sc += 2.5
            sc += math.log1p(max(0, int(it.get("monthly_sales") or 0))) * 0.5
            topup_cands.append((sc, it))
        topup_cands.sort(key=lambda x: x[0], reverse=True)
        for _sc, cand in topup_cands[:2]:
            _add_dish(cand, qty=1)
            subtotal = round(sum(d["price"] * d["qty"] for d in chosen), 2)
            if subtotal >= min_order:
                reason_parts.append(f"补配满足¥{min_order:.0f}起送")
                break

    # 4) 最优红包智能凑单：若加一份平价小菜/主食/饮品能跨入更高档大额神券且券后总实付严格更低（倒省钱），自动顺手凑单！
    value_topup_tip = ""
    coupon_auto_topup: Optional[dict[str, Any]] = None
    if chosen and (account_coupons or store_promos):
        from siming.meituan_coupons import find_cash_saving_topup, infer_order_category

        remaining_usable = [
            it
            for it in usable
            if it["norm"] not in chosen_norms
            and not it.get("is_tiny_promo")
            and float(it.get("price") or 0.0) <= 25.0
        ]
        # 优先拿主食/配菜/饮品做凑单候选，避免凑进一份完整大菜吃不完
        preferred_topups = [it for it in remaining_usable if _SIDE_OR_STAPLE_RE.search(str(it.get("name") or "")) or float(it.get("price") or 0.0) <= 15.0]
        topup_pool = preferred_topups or remaining_usable
        if topup_pool:
            o_cat = infer_order_category(shop_name, item_names=[str(d.get("name") or "") for d in chosen])
            topup_eval = find_cash_saving_topup(
                shop_name=shop_name,
                current_subtotal=subtotal,
                delivery_fee=delivery_fee,
                candidate_items=topup_pool,
                store_promos=store_promos,
                account_coupons=account_coupons,
                max_total_cap=cap_limit,
                order_category=o_cat,
                now_dt=now_dt,
            )
            auto_t = topup_eval.get("auto_topup")
            if auto_t is not None and auto_t.get("item"):
                _add_dish(auto_t["item"], qty=1)
                subtotal = round(sum(d["price"] * d["qty"] for d in chosen), 2)
                coupon_auto_topup = {
                    "name": auto_t["name"],
                    "price": auto_t["price"],
                    "net_cash_saved": auto_t["net_cash_saved"],
                    "applied_coupons": auto_t["applied_coupons"],
                }
                reason_parts.append(f"顺手凑「{auto_t['name']}」解锁大额券倒省¥{auto_t['net_cash_saved']:g}")
            elif topup_eval.get("value_topup_tip"):
                value_topup_tip = str(topup_eval["value_topup_tip"])

    est_total = round(subtotal + delivery_fee, 2)
    all_direct = bool(chosen) and all(d.get("has_plus") for d in chosen)
    if all_direct:
        reason_parts.append("支持一键直加")

    return {
        "ok": bool(chosen),
        "dishes": chosen,
        "min_order": round(min_order, 2),
        "delivery_fee": round(delivery_fee, 2),
        "est_subtotal": subtotal,
        "est_total": est_total,
        "meets_min_order": (subtotal >= min_order) if min_order > 0 else True,
        "over_cap": est_total > float(max_yuan),
        "plan_source": plan_source,
        "reason": " · ".join(reason_parts) or "实时菜单匹配",
        "coupon_auto_topup": coupon_auto_topup,
        "value_topup_tip": value_topup_tip,
    }


def _setup_mobile_page(p_ws: LocalWsCdpClient) -> None:
    for method, params in (
        ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
        ("Page.enable", {}),
        ("Page.setWebLifecycleState", {"state": "active"}),
        ("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True}),
    ):
        try:
            p_ws.call(method, params)
        except Exception:
            pass


def _cdp_click_xy(p_ws: LocalWsCdpClient, x: float, y: float) -> None:
    p_ws.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    p_ws.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
    p_ws.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})


def _eval_val(p_ws: LocalWsCdpClient, expr: str) -> Any:
    r = p_ws.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    return (r.get("result") or {}).get("value")


def _parse_coupon_expiry_dt(exp_str: str) -> Optional[datetime]:
    s = (exp_str or "").strip()
    if not s:
        return None
    for fmt, end_of_day in (
        ("%Y.%m.%d %H:%M:%S", False),
        ("%Y.%m.%d %H:%M", False),
        ("%Y.%m.%d", True),
    ):
        try:
            dt = datetime.strptime(s, fmt).replace(tzinfo=SHANGHAI_TZ)
            if end_of_day:
                dt = dt.replace(hour=23, minute=59, second=59)
            return dt
        except ValueError:
            continue
    return None


def _within_time_window(win: Optional[list[str]], now_dt: datetime) -> bool:
    if not win or len(win) != 2:
        return True
    hhmm = now_dt.strftime("%H:%M")
    start_s, end_s = str(win[0]), str(win[1])
    if start_s <= end_s:
        return start_s <= hhmm <= end_s
    return hhmm >= start_s or hhmm <= end_s


def parse_meituan_all_account_redpackets(
    text: str,
    now_dt: Optional[datetime] = None,
) -> dict[str, list[dict[str, Any]]]:
    """全量解析美团 H5「美团红包」(`mindex/couponlist`) 页面文本，分品类归类餐饮外卖神券与商超/闪购/医药补货神券。"""
    from siming.meituan_coupons import classify_coupon_category

    now_cur = now_dt or now_local()
    lines = [ln.strip() for ln in (text or "").replace("\r", "").split("\n") if ln.strip()]
    if not lines:
        return {"platform_coupons": [], "flash_coupons": []}

    food_coupons: list[dict[str, Any]] = []
    flash_coupons: list[dict[str, Any]] = []
    seen_food: set[tuple[str, float, float, str]] = set()
    seen_flash: set[tuple[str, float, float, str]] = set()
    n = len(lines)
    i = 0
    while i < n:
        # 锚点：`标题` / `¥` / `金额`
        if i + 2 < n and lines[i + 1] in ("¥", "￥") and re.fullmatch(r"\d+(?:\.\d{1,2})?", lines[i + 2]):
            title = lines[i]
            if title in ("美团红包", "使用兑换码添加红包") or re.fullmatch(r"\d+张券", title):
                i += 1
                continue
            amount = float(lines[i + 2])
            j = i + 3
            block_lines: list[str] = []
            while j < n and lines[j] != "去使用":
                if j + 2 < n and lines[j + 1] in ("¥", "￥") and re.fullmatch(r"\d+(?:\.\d{1,2})?", lines[j + 2]):
                    break
                block_lines.append(lines[j])
                j += 1
            block_txt = " ".join(block_lines)

            exp_s = ""
            m_exp = _COUPON_EXPIRY_RE.search(block_txt)
            if m_exp:
                exp_s = m_exp.group(1).strip()
            exp_dt = _parse_coupon_expiry_dt(exp_s)
            if exp_dt is not None and exp_dt < now_cur:
                i = j + 1
                continue

            threshold = 0.0
            m_th = _COUPON_THRESHOLD_RE.search(block_txt)
            if m_th:
                threshold = float(m_th.group(1))
            elif "无门槛" in block_txt:
                threshold = 0.0

            m_win = _COUPON_TIME_WIN_RE.search(block_txt)
            time_win = [m_win.group(1), m_win.group(2)] if m_win else None

            cat = classify_coupon_category(title, block_txt)
            if amount > 0 and cat != "other":
                key = (title, amount, threshold, exp_s)
                entry = {
                    "kind": "platform",
                    "category": cat,
                    "is_universal": cat == "universal",
                    "title": title,
                    "amount": round(amount, 2),
                    "threshold": round(threshold, 2),
                    "expires_at": exp_s,
                    "time_window": time_win,
                    "label": f"{title} " + (f"满{threshold:g}减{amount:g}" if threshold > 0 else f"无门槛减{amount:g}"),
                    "short_label": f"满{threshold:g}减{amount:g}" if threshold > 0 else f"无门槛减{amount:g}",
                    "rule": block_txt[:160],
                }
                if cat in ("food", "universal") and not _NON_FOOD_COUPON_RE.search(title):
                    if key not in seen_food:
                        seen_food.add(key)
                        food_coupons.append(entry)
                if cat in ("supermarket", "pharmacy", "fruit", "universal"):
                    if key not in seen_flash:
                        seen_flash.add(key)
                        flash_coupons.append(entry)
            i = j + 1
        else:
            i += 1

    food_coupons.sort(key=lambda x: (-x["amount"], -x["threshold"]))
    flash_coupons.sort(key=lambda x: (-x["amount"], -x["threshold"]))
    return {"platform_coupons": food_coupons, "flash_coupons": flash_coupons}


def parse_meituan_account_redpackets(
    text: str,
    now_dt: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """解析美团 H5「美团红包」(`mindex/couponlist`) 页面文本，筛选未过期且可用于餐饮外卖的平台神券/红包。"""
    return parse_meituan_all_account_redpackets(text, now_dt=now_dt)["platform_coupons"]


def parse_meituan_store_vouchers(
    text: str,
    now_dt: Optional[datetime] = None,
    *,
    include_retail: bool = False,
) -> list[dict[str, Any]]:
    """解析美团 H5「商家代金券」(`mindex/poicouponlist`) 页面文本，提取未过期且可与平台神券叠加的门店券。"""
    now_cur = now_dt or now_local()
    lines = [ln.strip() for ln in (text or "").replace("\r", "").split("\n") if ln.strip()]
    if not lines:
        return []

    vouchers: list[dict[str, Any]] = []
    seen: set[tuple[str, float, float]] = set()
    n = len(lines)
    i = 0
    while i < n:
        if i + 2 < n and lines[i + 1] in ("¥", "￥") and re.fullmatch(r"\d+(?:\.\d{1,2})?", lines[i + 2]):
            shop = lines[i]
            if shop in ("商家代金券",):
                i += 1
                continue
            amount = float(lines[i + 2])
            j = i + 3
            block_lines: list[str] = []
            while j < n:
                if j + 2 < n and lines[j + 1] in ("¥", "￥") and re.fullmatch(r"\d+(?:\.\d{1,2})?", lines[j + 2]):
                    break
                block_lines.append(lines[j])
                j += 1
            block_txt = " ".join(block_lines)

            exp_s = ""
            m_exp = _COUPON_EXPIRY_RE.search(block_txt)
            if m_exp:
                exp_s = m_exp.group(1).strip()
            exp_dt = _parse_coupon_expiry_dt(exp_s)
            if exp_dt is not None and exp_dt < now_cur:
                i = j
                continue

            if not include_retail and _NON_FOOD_COUPON_RE.search(shop):
                i = j
                continue

            threshold = 0.0
            m_th = _COUPON_THRESHOLD_RE.search(block_txt)
            if m_th:
                threshold = float(m_th.group(1))
            key = (_norm_shop(shop), amount, threshold)
            if amount > 0 and key not in seen:
                seen.add(key)
                vouchers.append(
                    {
                        "kind": "store_voucher",
                        "shop": shop,
                        "norm_shop": _norm_shop(shop),
                        "brand": _brand_stem(shop),
                        "amount": round(amount, 2),
                        "threshold": round(threshold, 2),
                        "expires_at": exp_s,
                        "stackable": "可与其他" in block_txt,
                        "label": f"商家券 满{threshold:g}减{amount:g}" if threshold > 0 else f"商家券 减{amount:g}",
                    }
                )
            i = j
        else:
            i += 1

    vouchers.sort(key=lambda x: (-x["amount"], -x["threshold"]))
    return vouchers


def parse_store_promos(promo_text: str) -> dict[str, Any]:
    """解析门店头部优惠条 / 展开抽屉文本中的满减阶梯、店内可领券与返券信息。"""
    txt = (promo_text or "").strip()
    if not txt:
        return {"manjian": [], "claimable_coupons": [], "rebates": [], "discount_tag": ""}

    # 先剥离「返满XX减YY」以免把返券误算成当单立减
    rebates = list(dict.fromkeys(_STORE_REBATE_RE.findall(txt)))
    clean_for_mj = _STORE_REBATE_RE.sub(" ", txt)

    manjian: list[dict[str, float]] = []
    seen_mj: set[tuple[float, float]] = set()
    for m in _STORE_MANJIAN_RE.finditer(clean_for_mj):
        th, disc = float(m.group(1)), float(m.group(2))
        if th > disc > 0 and (th, disc) not in seen_mj:
            seen_mj.add((th, disc))
            manjian.append({"threshold": th, "discount": disc})
    manjian.sort(key=lambda x: (-x["discount"], x["threshold"]))

    claimable: list[dict[str, Any]] = []
    seen_cl: set[float] = set()
    for m in _STORE_CLAIM_COUPON_RE.finditer(txt):
        amt = float(m.group(1))
        if amt > 0 and amt not in seen_cl:
            seen_cl.add(amt)
            claimable.append({"amount": amt, "label": f"店内可领¥{amt:g}券"})
    # 捕获头部独立样式 `¥\n3\n领`
    for m in re.finditer(r"[¥￥]\s*\n?\s*(\d+(?:\.\d+)?)\s*\n?\s*领", txt):
        amt = float(m.group(1))
        if amt > 0 and amt not in seen_cl:
            seen_cl.add(amt)
            claimable.append({"amount": amt, "label": f"店内可领¥{amt:g}券"})
    claimable.sort(key=lambda x: -x["amount"])

    m_disc = _STORE_DISCOUNT_TAG_RE.search(txt)
    return {
        "manjian": manjian,
        "claimable_coupons": claimable,
        "rebates": rebates,
        "discount_tag": m_disc.group(1) if m_disc else "",
    }


def load_cached_account_coupons(
    db: SimingDB,
    max_age_min: float = COUPONS_CACHE_TTL_MINUTES,
    *,
    now_dt: Optional[datetime] = None,
    require_flash: bool = False,
) -> Optional[dict[str, Any]]:
    raw = db.get_meta(COUPONS_CACHE_META_KEY) or ""
    if not raw:
        return None
    try:
        obj = json.loads(raw)
        if not isinstance(obj, dict):
            return None
        if require_flash and "flash_coupons" not in obj:
            return None
        ft = str(obj.get("fetched_at") or "")
        ref_now = now_dt or now_local()
        if ft and (ref_now - parse_iso(ft)).total_seconds() > max_age_min * 60.0:
            return None
        return obj
    except Exception:
        return None


def fetch_meituan_account_coupons(
    db: SimingDB,
    port: int = 9222,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """只读读取美团账号当前可用外卖神券 (`couponlist`) 与商家代金券 (`poicouponlist`)，带 20 分钟缓存。"""
    if not force_refresh:
        cached = load_cached_account_coupons(db)
        if cached is not None:
            return {"ok": True, "from_cache": True, **cached}
    if os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return {"ok": False, "reason": "offline", "platform_coupons": [], "store_vouchers": []}

    base = resolve_chromium_cdp_base(port=port)
    if not base or not base.get("ws_browser"):
        return {"ok": False, "reason": "cdp_unavailable", "platform_coupons": [], "store_vouchers": []}

    tid = ""
    keep_open = False
    try:
        with LocalWsCdpClient(base["ws_browser"], timeout=6.0) as b_ws:
            tid = str(b_ws.call("Target.createTarget", {"url": "about:blank", "background": True}).get("targetId") or "")
        if not tid:
            return {"ok": False, "reason": "create_target_failed", "platform_coupons": [], "store_vouchers": []}

        page_ws = base["ws_browser"].split("/devtools/")[0] + f"/devtools/page/{tid}"
        with LocalWsCdpClient(page_ws, timeout=20.0) as p_ws:
            _setup_mobile_page(p_ws)

            # 1) 美团红包 / 外卖神券
            p_ws.call("Page.navigate", {"url": MEITUAN_H5_COUPONLIST})
            time.sleep(2.4)
            for _ in range(2):
                p_ws.call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": 200, "y": 500, "deltaX": 0, "deltaY": 2400})
                time.sleep(0.7)
            hb_meta = _eval_val(
                p_ws,
                "JSON.stringify({u: location.href, t: document.title||'', x: document.body?.innerText||''})",
            )
            hb_obj = json.loads(hb_meta) if isinstance(hb_meta, str) and hb_meta.startswith("{") else {}
            gate = detect_compliance_gate(str(hb_obj.get("u") or ""), str(hb_obj.get("t") or ""), str(hb_obj.get("x") or ""))
            if gate is not None:
                keep_open = True
                return {"ok": False, "gated": True, "gate": gate, "platform_coupons": [], "store_vouchers": []}
            all_rp = parse_meituan_all_account_redpackets(str(hb_obj.get("x") or ""))
            plat_coupons = all_rp["platform_coupons"]
            flash_coupons = all_rp["flash_coupons"]

            # 2) 商家代金券（同时保留餐饮券与商超/医药券）
            p_ws.call("Page.navigate", {"url": MEITUAN_H5_POICOUPONLIST})
            time.sleep(2.2)
            p_ws.call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": 200, "y": 500, "deltaX": 0, "deltaY": 2000})
            time.sleep(0.6)
            sv_txt = str(_eval_val(p_ws, "document.body?.innerText || ''") or "")
            store_vouchers = parse_meituan_store_vouchers(sv_txt, include_retail=False)
            retail_store_vouchers = parse_meituan_store_vouchers(sv_txt, include_retail=True)

        payload = {
            "fetched_at": iso_now(),
            "platform_coupons": plat_coupons,
            "flash_coupons": flash_coupons,
            "store_vouchers": store_vouchers,
            "retail_store_vouchers": retail_store_vouchers,
        }
        db.set_meta(COUPONS_CACHE_META_KEY, json.dumps(payload, ensure_ascii=False))
        return {"ok": True, "from_cache": False, **payload}
    except Exception as exc:
        return {"ok": False, "reason": "cdp_error", "error": str(exc)[:180], "platform_coupons": [], "store_vouchers": []}
    finally:
        if tid and not keep_open:
            try:
                with LocalWsCdpClient(base["ws_browser"], timeout=4.0) as b_ws:
                    b_ws.call("Target.closeTarget", {"targetId": tid})
            except Exception:
                pass


def format_account_coupons_brief(coupons_obj: Optional[dict[str, Any]], limit: int = 6) -> str:
    if not coupons_obj:
        return ""
    plats = list(coupons_obj.get("platform_coupons") or [])
    if not plats:
        return ""
    uniq: list[str] = []
    for c in plats:
        sl = str(c.get("short_label") or "")
        if sl and sl not in uniq:
            uniq.append(sl)
        if len(uniq) >= limit:
            break
    return " / ".join(uniq)


def compute_coupon_pricing(
    shop_name: str,
    subtotal: float,
    delivery_fee: float = 0.0,
    store_promos: Optional[dict[str, Any]] = None,
    account_coupons: Optional[dict[str, Any]] = None,
    now_dt: Optional[datetime] = None,
    *,
    packing_fee: float = 0.0,
    order_category: str = "food",
) -> dict[str, Any]:
    """按美团真实计价顺序计算「门店满减 -> 商家代金券(区分可叠加/互斥) -> 平台最优神券(含打包费门槛、省无门槛券、临期优先)」后的预估到手价与凑单提示。"""
    from siming.meituan_coupons import classify_coupon_category, is_coupon_applicable_to_category, is_coupon_applicable_to_shop

    now_cur = now_dt or now_local()
    sub = max(0.0, round(float(subtotal), 2))
    deliv = max(0.0, round(float(delivery_fee), 2))
    pack = max(0.0, round(float(packing_fee), 2))
    raw_total = round(sub + pack + deliv, 2)

    promos = store_promos or {}
    acc = account_coupons or {}

    # 1) 门店满减（按餐品现价算门槛）
    mj_off = 0.0
    mj_label = ""
    mj_tier = next(
        (t for t in sorted(promos.get("manjian") or [], key=lambda x: (-float(x["discount"]), float(x["threshold"]))) if sub >= float(t["threshold"])),
        None,
    )
    if mj_tier is not None:
        mj_off = round(float(mj_tier["discount"]), 2)
        mj_label = f"门店满{float(mj_tier['threshold']):g}减{mj_off:g}"
    after_mj = max(0.0, round(sub - mj_off, 2))

    # 2) 筛选当前品类有效平台神券（餐饮看 platform_coupons，闪购/商超/医药/水果合并 flash_coupons + universal）
    raw_plats: list[dict[str, Any]] = []
    if order_category == "food":
        raw_plats = list(acc.get("platform_coupons") or [])
    else:
        seen_p: set[tuple[str, float, float, str]] = set()
        for src_key in ("flash_coupons", "platform_coupons"):
            for c in acc.get(src_key) or []:
                if not isinstance(c, dict):
                    continue
                sig = (str(c.get("title") or ""), float(c.get("amount") or 0.0), float(c.get("threshold") or 0.0), str(c.get("expires_at") or ""))
                if sig in seen_p:
                    continue
                seen_p.add(sig)
                raw_plats.append(c)

    valid_plats: list[dict[str, Any]] = []
    for c in raw_plats:
        if not isinstance(c, dict):
            continue
        if not _within_time_window(c.get("time_window"), now_cur):
            continue
        exp_dt = _parse_coupon_expiry_dt(str(c.get("expires_at") or ""))
        if exp_dt is not None and exp_dt < now_cur:
            continue
        c_cat = str(c.get("category") or classify_coupon_category(str(c.get("title") or ""), str(c.get("rule") or "")))
        if not is_coupon_applicable_to_category(c_cat, order_category):
            continue
        if not is_coupon_applicable_to_shop(c, shop_name):
            continue
        valid_plats.append({**c, "_exp_ts": exp_dt.timestamp() if exp_dt is not None else 9e12, "_is_univ": c_cat == "universal"})

    def _pick_best_platform(qualify_amt: float) -> tuple[float, Optional[dict[str, Any]]]:
        usable_p = [c for c in valid_plats if qualify_amt >= float(c.get("threshold") or 0.0)]
        if not usable_p:
            return 0.0, None
        # 最优策略：1.立减最大 2.门槛最高(留无门槛券给小单) 3.临期优先 4.专享优先于通用
        usable_p.sort(
            key=lambda x: (
                -float(x["amount"]),
                -float(x.get("threshold") or 0.0),
                float(x.get("_exp_ts") or 9e12),
                1 if x.get("_is_univ") else 0,
            )
        )
        best = usable_p[0]
        return round(min(qualify_amt, float(best["amount"])), 2), best

    # 3) 账户已领商家代金券（匹配同店/同品牌；严格区分可叠加 vs 不可叠加，全局求最大省钱组合！）
    s_norm = _norm_shop(shop_name)
    s_brand = _brand_stem(shop_name)
    voucher_pool = list(acc.get("store_vouchers") or [])
    if order_category != "food" and acc.get("retail_store_vouchers"):
        voucher_pool = list(acc.get("retail_store_vouchers") or []) + voucher_pool

    matched_stackable: list[dict[str, Any]] = []
    matched_exclusive: list[dict[str, Any]] = []
    for v in voucher_pool:
        if not isinstance(v, dict):
            continue
        vn = str(v.get("norm_shop") or _norm_shop(str(v.get("shop") or "")))
        vb = str(v.get("brand") or _brand_stem(str(v.get("shop") or "")))
        if (vn and (vn == s_norm or vn in s_norm or s_norm in vn)) or (len(s_brand) >= 2 and vb == s_brand):
            if after_mj >= float(v.get("threshold") or 0.0):
                if v.get("stackable", True):
                    matched_stackable.append(v)
                else:
                    matched_exclusive.append(v)

    # 方案 A：可叠加商家券 + 平台神券（注意：若商家券压低门槛导致无法触发更高档平台神券，自动比较「仅用平台神券」与「商家券+神券」取最优！）
    best_sv_off = 0.0
    best_sv_obj: Optional[dict[str, Any]] = None
    best_plat_off = 0.0
    best_plat_obj: Optional[dict[str, Any]] = None

    # 候选 1：仅用平台神券（不扣商家券，保住大额神券门槛）
    p_only_off, p_only_obj = _pick_best_platform(round(after_mj + pack, 2))
    best_plat_off, best_plat_obj = p_only_off, p_only_obj

    # 候选 2：叠加商家券 + 剩余金额匹配平台神券
    if matched_stackable:
        for sv in sorted(matched_stackable, key=lambda x: (-float(x["amount"]), -float(x.get("threshold") or 0.0)))[:3]:
            sv_a = round(min(after_mj, float(sv["amount"])), 2)
            rem_qual = max(0.0, round(after_mj - sv_a + pack, 2))
            p_a, p_o = _pick_best_platform(rem_qual)
            if sv_a + p_a > best_sv_off + best_plat_off + 1e-6:
                best_sv_off, best_sv_obj = sv_a, sv
                best_plat_off, best_plat_obj = p_a, p_o

    # 候选 3：不可叠加商家券单独使用（仅当它比平台神券更省钱时才选用）
    if matched_exclusive:
        matched_exclusive.sort(key=lambda x: (-float(x["amount"]), -float(x.get("threshold") or 0.0)))
        ex_sv = matched_exclusive[0]
        ex_a = round(min(after_mj, float(ex_sv["amount"])), 2)
        if ex_a > best_sv_off + best_plat_off + 1e-6:
            best_sv_off, best_sv_obj = ex_a, ex_sv
            best_plat_off, best_plat_obj = 0.0, None

    applied: list[str] = []
    if mj_label:
        applied.append(mj_label)
    if best_sv_obj is not None and best_sv_off > 0:
        applied.append(str(best_sv_obj.get("label") or f"商家券减{best_sv_off:g}"))
    if best_plat_obj is not None and best_plat_off > 0:
        applied.append(str(best_plat_obj.get("label") or f"外卖神券减{best_plat_off:g}"))

    sv_off = best_sv_off
    plat_off = best_plat_off
    qualify_for_near = max(0.0, round(after_mj - sv_off + pack, 2))

    # 4) 凑单提示：若差 <= ¥15 就能用上一档更划算的大额神券（新增立减 > 差额的一半），主动提醒（倒省钱重点高亮！）
    near_tip = ""
    best_near_gain = 0.0
    for c in valid_plats:
        th = float(c.get("threshold") or 0.0)
        amt = float(c.get("amount") or 0.0)
        gap = round(th - qualify_for_near, 2)
        extra_save = round(amt - plat_off, 2)
        if 0.0 < gap <= 15.0 and extra_save >= max(2.0, gap * 0.55):
            gain_score = extra_save - gap * 0.5
            if gain_score > best_near_gain:
                best_near_gain = gain_score
                sl = c.get("short_label") or f"满{th:g}减{amt:g}"
                net_save = round(extra_save - gap, 2)
                if net_save >= 0.5:
                    near_tip = f"差¥{gap:g}可换{sl}（倒省¥{net_save:g}！）"
                else:
                    near_tip = f"差¥{gap:g}可换{sl}"

    claimable_notes = [str(x.get("label") or "") for x in (promos.get("claimable_coupons") or []) if x.get("label")][:2]

    total_disc = round(min(sub + pack, mj_off + sv_off + plat_off), 2)
    after_coupon_total = round(max(0.0, sub + pack - total_disc) + deliv, 2)

    return {
        "raw_subtotal": sub,
        "packing_fee": pack,
        "delivery_fee": deliv,
        "raw_total": raw_total,
        "store_manjian_off": mj_off,
        "store_voucher_off": sv_off,
        "platform_coupon_off": plat_off,
        "total_discount": total_disc,
        "after_coupon_total": after_coupon_total,
        "applied_coupons": applied,
        "claimable_notes": claimable_notes,
        "near_coupon_tip": near_tip,
        "order_category": order_category,
    }


def _extract_menu_dom_snapshot(p_ws: LocalWsCdpClient) -> dict[str, Any]:
    raw = _eval_val(
        p_ws,
        """(() => {
            const url = location.href;
            let cards = [];
            const mindexBtns = Array.from(document.querySelectorAll('.sqt-menu-add-buttons')).filter(btn => {
                const r = btn.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            });
            if (mindexBtns.length > 0) {
                cards = mindexBtns.slice(0, 42).map((btn, idx) => {
                    let card = btn;
                    for (let i = 0; i < 8 && card.parentElement; i++) {
                        card = card.parentElement;
                        const t = (card.innerText || '').trim();
                        if (t.includes('¥') && (t.includes('月售') || t.split('\\n').length >= 3)) break;
                    }
                    const plus = btn.querySelector('[class*="plus"]');
                    return {
                        idx,
                        hasPlus: Boolean(plus),
                        lines: (card.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean)
                    };
                });
            } else {
                const subCards = Array.from(document.querySelectorAll('[class*="food-card-wrap"]')).filter(c => {
                    const r = c.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                });
                cards = subCards.slice(0, 42).map((c, idx) => {
                    const plus = c.querySelector('.index-flashstepper-add_box');
                    const rawLines = (c.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean);
                    const h = (c.querySelector('[class*="food-item-header"]')?.innerText || '').trim();
                    const lines = h && rawLines[0] !== h ? [h, ...rawLines.filter(x => x !== h)] : rawLines;
                    return {
                        idx,
                        hasPlus: Boolean(plus),
                        lines
                    };
                });
            }
            const cartBar = (
                document.querySelector('[class*="cartContainer"]')?.innerText ||
                document.querySelector('[class*="bottom-cart"], [class*="cart-bar"], [class*="shopcart"]')?.innerText ||
                ''
            ).trim();
            return JSON.stringify({
                url,
                title: document.title || '',
                cartBar,
                promoTags: (document.querySelector('#sqt-openh5-menuTags')?.innerText || '').trim(),
                bodyHead: (document.body?.innerText || '').slice(0, 1200),
                cards
            });
        })()""",
    )
    if isinstance(raw, str) and raw.startswith("{"):
        try:
            return json.loads(raw)
        except Exception:
            return {}
    return {}


def inspect_meituan_shop_menu(
    db: SimingDB,
    shop_name: str,
    loc_target: Optional[dict[str, Any]] = None,
    port: int = 9222,
    menu_url: str = "",
    history_dishes: Optional[list[str]] = None,
) -> dict[str, Any]:
    """在主 Chromium 9222 后台标签只读打开目标店铺菜单页，解析实时在售菜品、价格、起送/配送费与直达 `menu_url`。"""
    if os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return {"ok": False, "reason": "offline"}

    base = resolve_chromium_cdp_base(port=port)
    if not base or not base.get("ws_browser"):
        return {"ok": False, "reason": "cdp_unavailable"}

    from siming.meituan_agent import (
        MEITUAN_H5_SEARCH_TMPL,
        set_cdp_geolocation_for_target,
        sync_meituan_h5_location,
    )

    tid = ""
    keep_open = False
    try:
        with LocalWsCdpClient(base["ws_browser"], timeout=6.0) as b_ws:
            tid = str(b_ws.call("Target.createTarget", {"url": "about:blank", "background": True}).get("targetId") or "")
        if not tid:
            return {"ok": False, "reason": "create_target_failed"}

        page_ws = base["ws_browser"].split("/devtools/")[0] + f"/devtools/page/{tid}"
        with LocalWsCdpClient(page_ws, timeout=22.0) as p_ws:
            _setup_mobile_page(p_ws)
            set_cdp_geolocation_for_target(p_ws, loc_target or {})

            if menu_url and ("mindex/menu" in menu_url or "sub-trade/restaurant" in menu_url):
                p_ws.call("Page.navigate", {"url": menu_url})
                time.sleep(2.2)
                if loc_target and "h5.waimai.meituan.com" in menu_url:
                    s_res = sync_meituan_h5_location(p_ws, loc_target)
                    if s_res.get("need_reload"):
                        p_ws.call("Page.navigate", {"url": menu_url})
                        time.sleep(1.8)
            else:
                # 先在当前已同步定位的美团搜索页搜该店并点进真实门店菜单页
                s_url = MEITUAN_H5_SEARCH_TMPL.format(keyword=urllib.parse.quote(shop_name))
                p_ws.call("Page.navigate", {"url": s_url})
                time.sleep(1.4)
                if loc_target:
                    s_res = sync_meituan_h5_location(p_ws, loc_target)
                    if s_res.get("need_reload"):
                        p_ws.call("Page.navigate", {"url": s_url})
                        time.sleep(1.4)
                deadline_s = time.monotonic() + 6.5
                clicked_shop = ""
                while time.monotonic() < deadline_s:
                    time.sleep(0.5)
                    meta_s = _eval_val(
                        p_ws,
                        "JSON.stringify({u: location.href, t: document.title||'', x: (document.body?.innerText||'').slice(0,1500)})",
                    )
                    ms = json.loads(meta_s) if isinstance(meta_s, str) and meta_s.startswith("{") else {}
                    gate = detect_compliance_gate(str(ms.get("u") or ""), str(ms.get("t") or ""), str(ms.get("x") or ""))
                    if gate is not None:
                        keep_open = True
                        return {"ok": False, "gated": True, "gate": gate, "target_id": tid}
                    if str(ms.get("x") or "").count("起送") < 1:
                        continue
                    pos_raw = _eval_val(
                        p_ws,
                        f"""(() => {{
                            const wantFull = {json.dumps(_norm_shop(shop_name))};
                            const wantBrand = {json.dumps(_brand_stem(shop_name))};
                            const norm = s => (s||'').toLowerCase().replace(/（/g,'(').replace(/）/g,')').replace(/[·•●\\s]+/g,'');
                            const cards = Array.from(document.querySelectorAll('[class*="shopCard"]')).filter(c => c.getBoundingClientRect().width > 0);
                            let chosen = null;
                            if (cards.length) {{
                                chosen =
                                    cards.find(c => norm((c.innerText||'').trim().split('\\n')[0]) === wantFull) ||
                                    cards.find(c => wantFull && norm(c.innerText).includes(wantFull)) ||
                                    cards.find(c => wantBrand.length >= 2 && norm(c.innerText).includes(wantBrand));
                            }}
                            if (!chosen) {{
                                const cands = Array.from(document.querySelectorAll('div,a,span,p,li')).filter(e => {{
                                    const t = (e.innerText || '').trim();
                                    const r = e.getBoundingClientRect();
                                    if (r.top < 75 || r.width < 20 || r.height < 10 || t.length < 2 || t.length > 52) return false;
                                    if (e.children.length > 2) return false;
                                    const nt = norm(t);
                                    return nt === wantFull || nt.includes(wantFull) || (wantBrand.length >= 2 && nt.includes(wantBrand));
                                }});
                                cands.sort((a, b) => {{
                                    const na = norm(a.innerText), nb = norm(b.innerText);
                                    const ea = na === wantFull ? 0 : 1, eb = nb === wantFull ? 0 : 1;
                                    return ea - eb || a.getBoundingClientRect().top - b.getBoundingClientRect().top;
                                }});
                                if (cands.length) chosen = cands[0];
                            }}
                            if (!chosen) return '';
                            chosen.scrollIntoView({{block: 'center'}});
                            const r = chosen.getBoundingClientRect();
                            const title = ((chosen.innerText||'').trim().split('\\n')[0] || '').trim();
                            return JSON.stringify({{name: title, x: r.left + Math.min(r.width/2, 120), y: r.top + Math.min(r.height/2, 36)}});
                        }})()""",
                    )
                    if isinstance(pos_raw, str) and pos_raw.startswith("{"):
                        pos_obj = json.loads(pos_raw)
                        clicked_shop = str(pos_obj.get("name") or "")
                        _cdp_click_xy(p_ws, float(pos_obj["x"]), float(pos_obj["y"]))
                        break

                if not clicked_shop:
                    return {"ok": False, "reason": "shop_not_found_in_search", "error": f"当前定位未找到「{shop_name}」门店入口"}

            # 等待进入门店菜单页（兼容 mindex/menu 与 sub-trade/restaurant）并加载菜品控件
            deadline_m = time.monotonic() + (12.0 if "sub-trade/restaurant" in (menu_url or "") else 8.5)
            reloaded_subtrade = bool(menu_url and "sub-trade/restaurant" in menu_url)
            snap: dict[str, Any] = {}
            while time.monotonic() < deadline_m:
                time.sleep(0.55)
                _setup_mobile_page(p_ws)
                snap = _extract_menu_dom_snapshot(p_ws)
                gate = detect_compliance_gate(
                    str(snap.get("url") or ""),
                    str(snap.get("title") or ""),
                    str(snap.get("bodyHead") or ""),
                )
                if gate is not None:
                    keep_open = True
                    return {"ok": False, "gated": True, "gate": gate, "target_id": tid}
                snap_u = str(snap.get("url") or "")
                if "sub-trade/restaurant" in snap_u and not reloaded_subtrade and not snap.get("cards"):
                    reloaded_subtrade = True
                    p_ws.call("Page.navigate", {"url": snap_u})
                    deadline_m = time.monotonic() + 12.5
                    time.sleep(3.5)
                    continue
                if ("mindex/menu" in snap_u or "sub-trade/restaurant" in snap_u) and len(snap.get("cards") or []) >= 1:
                    break

            snap_u_final = str(snap.get("url") or "")
            if ("mindex/menu" not in snap_u_final and "sub-trade/restaurant" not in snap_u_final) or not snap.get("cards"):
                return {"ok": False, "reason": "menu_load_timeout", "url": snap_u_final}

            # 展开门店头部优惠详情（只读不领券），读取满减阶梯与店内可领券
            promo_drawer_txt = str(snap.get("promoTags") or "")
            try:
                _eval_val(p_ws, "document.querySelector('#sqt-openh5-menuTagsMore')?.click()")
                time.sleep(0.6)
                expanded_txt = str(
                    _eval_val(
                        p_ws,
                        "((document.querySelector('#sqt-openh5-menuTags')?.innerText || '') + '\\n' + (document.body?.innerText || '').slice(0, 420))",
                    )
                    or ""
                )
                if expanded_txt.strip():
                    promo_drawer_txt = expanded_txt
                # 收起抽屉以免遮挡后续滚动
                _eval_val(p_ws, "document.querySelector('#sqt-openh5-menuTagsMore')?.click()")
                time.sleep(0.25)
            except Exception:
                pass
            store_promos = parse_store_promos(promo_drawer_txt)

            # 轻滚一次加载更多招牌/主食菜品
            p_ws.call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": 260, "y": 500, "deltaX": 0, "deltaY": 1600})
            time.sleep(0.8)
            snap2 = _extract_menu_dom_snapshot(p_ws)
            merged_cards = list(snap.get("cards") or []) + list(snap2.get("cards") or [])

            # 熟店懒加载菜单补扫：若用户历史常点菜未出现在首屏分类（如南京大牌档），快速点击左侧分类补全以免回落推荐新菜
            want_hist = [str(d).strip() for d in (history_dishes or []) if str(d).strip()]
            if want_hist and "mindex/menu" in snap_u_final:
                cur_parsed = parse_meituan_menu_cards(merged_cards)
                missing_hist = [hd for hd in want_hist if _match_live_dish(hd, cur_parsed["items"]) is None]
                if missing_hist:
                    cats_raw = _eval_val(
                        p_ws,
                        r"""JSON.stringify(
                            Array.from(document.querySelectorAll('[class*="link-"]'))
                                .filter(el => {
                                    const r = el.getBoundingClientRect();
                                    const t = (el.innerText || '').trim().split('\n')[0].trim();
                                    return r.left < 120 && r.width > 20 && r.height > 15 && t && !/折扣|必选|团餐|饮品|零售/.test(t);
                                })
                                .map(el => Array.from(el.classList || []).find(c => /^link-\d+$/.test(c)) || '')
                                .filter(Boolean)
                        )""",
                    )
                    cat_classes = json.loads(cats_raw) if isinstance(cats_raw, str) and cats_raw.startswith("[") else []
                    for c_cls in cat_classes[:8]:
                        _eval_val(p_ws, f"document.querySelector('.{c_cls}')?.click()")
                        time.sleep(0.55)
                        snap_c = _extract_menu_dom_snapshot(p_ws)
                        if snap_c.get("cards"):
                            merged_cards.extend(snap_c["cards"])
                            cur_parsed = parse_meituan_menu_cards(merged_cards)
                            missing_hist = [hd for hd in want_hist if _match_live_dish(hd, cur_parsed["items"]) is None]
                            if not missing_hist:
                                break

            parsed = parse_meituan_menu_cards(merged_cards, cart_bar_text=str(snap2.get("cartBar") or snap.get("cartBar") or ""))
            return {
                "ok": True,
                "gated": False,
                "live_shop_name": str(snap2.get("title") or snap.get("title") or shop_name).strip(),
                "menu_url": str(snap2.get("url") or snap.get("url") or ""),
                "store_promos": store_promos,
                **parsed,
            }
    except Exception as exc:
        return {"ok": False, "reason": "cdp_error", "error": str(exc)[:200]}
    finally:
        if tid and not keep_open:
            try:
                with LocalWsCdpClient(base["ws_browser"], timeout=4.0) as b_ws:
                    b_ws.call("Target.closeTarget", {"targetId": tid})
            except Exception:
                pass


def create_speculative_batch(
    db: SimingDB,
    picks: list[dict[str, Any]],
    delivery_poi: str,
    cur_slot: str,
    max_budget: Optional[float],
    loc_target: dict[str, Any],
) -> dict[str, Any]:
    """初始化本轮推荐的投机推演批次（0ms 立即注入熟店历史常点菜预览，供后台并行实测补全）。"""
    now = now_local()
    batch_id = f"SPEC-{now.strftime('%H%M%S')}-{secrets.token_hex(2)}"
    expires = now + timedelta(minutes=SPECULATIVE_TTL_MINUTES)

    entries: list[dict[str, Any]] = []
    for idx, p in enumerate(picks[:5], start=1):
        shop = str(p.get("shop") or "").strip()
        hist = load_shop_historical_dish_combos(db, shop)
        entries.append(
            {
                "index": idx,
                "shop": shop,
                "live_shop_name": shop,
                "status": "PLANNING",
                "menu_url": "",
                "delivery_poi": delivery_poi,
                "rating": p.get("rating"),
                "eta_min": p.get("eta_min"),
                "distance_str": p.get("distance_str") or "",
                "avg_yuan": float(p.get("avg_yuan") or 38.0),
                "min_order": float(p.get("min_order") or 0.0),
                "delivery_fee": 0.0,
                "history_preview": str(hist.get("preview") or ""),
                "history_combo": hist,
                "dishes": [],
                "est_subtotal": 0.0,
                "est_total": float(p.get("avg_yuan") or 38.0),
                "plan_source": "history_hint" if hist.get("has_history") else "pending_live",
                "reason": "后台正在并行拉取实时菜单推演菜品...",
                "error": "",
            }
        )

    batch = {
        "batch_id": batch_id,
        "status": "RUNNING",
        "slot": cur_slot,
        "max_budget": max_budget,
        "delivery_poi": delivery_poi,
        "loc_target": {
            "alias": loc_target.get("alias") or "",
            "label": loc_target.get("label") or "",
            "gcj_lat": loc_target.get("gcj_lat"),
            "gcj_lng": loc_target.get("gcj_lng"),
            "mt_lat": loc_target.get("mt_lat"),
            "mt_lng": loc_target.get("mt_lng"),
        },
        "created_at": iso_now(now),
        "expires_at": iso_now(expires),
        "updated_at": iso_now(now),
        "entries": entries,
    }
    db.set_meta(SPECULATIVE_META_KEY, json.dumps(batch, ensure_ascii=False))
    return batch


def load_active_speculative_batch(db: SimingDB) -> Optional[dict[str, Any]]:
    raw = db.get_meta(SPECULATIVE_META_KEY) or ""
    if not raw:
        return None
    try:
        batch = json.loads(raw)
        if not isinstance(batch, dict):
            return None
        exp = str(batch.get("expires_at") or "")
        if exp and parse_iso(exp) < now_local():
            return None
        return batch
    except Exception:
        return None


def spawn_speculative_preplan_background(db: SimingDB, batch_id: str) -> bool:
    """派生后台独立进程异步执行并行投机推演，0ms 不阻塞当前 QQ 推荐回复。"""
    if os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return False
    siming_bin = str(Path.home() / ".local" / "bin" / "siming")
    cmd = (
        [siming_bin, "meituan", "--speculate-batch", batch_id]
        if Path(siming_bin).is_file()
        else [sys.executable, "-m", "siming.cli", "meituan", "--speculate-batch", batch_id]
    )
    if os.environ.get("INVOCATION_ID") and shutil.which("systemd-run"):
        cmd = ["systemd-run", "--user", "--scope", "--quiet", "--", *cmd]
    log_path = db.paths.tmp_root / "meituan-speculate.log"
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("ab") as lf:
            subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=lf,
                stderr=lf,
                start_new_session=True,
                close_fds=True,
            )
        return True
    except Exception:
        return False


def _save_batch_entry_update(db: SimingDB, batch_id: str, idx: int, patch: dict[str, Any], lock: threading.Lock) -> None:
    with lock:
        cur = load_active_speculative_batch(db)
        if not cur or cur.get("batch_id") != batch_id:
            return
        for ent in cur.get("entries") or []:
            if int(ent.get("index") or 0) == idx:
                ent.update(patch)
                break
        statuses = [str(e.get("status") or "") for e in (cur.get("entries") or [])]
        if all(s != "PLANNING" for s in statuses):
            cur["status"] = "READY"
        cur["updated_at"] = iso_now()
        db.set_meta(SPECULATIVE_META_KEY, json.dumps(cur, ensure_ascii=False))


def run_speculative_meal_preplan(
    db: SimingDB,
    batch_id: str = "",
    inspect_shop_fn: Optional[Callable[[str, dict[str, Any]], dict[str, Any]]] = None,
    fetch_coupons_fn: Optional[Callable[[], dict[str, Any]]] = None,
    max_workers: int = 3,
    only_index: Optional[int] = None,
) -> dict[str, Any]:
    """对推荐批次内的所有候选店铺并行执行实时菜单抓取、账户优惠券/门店满减核验与菜品推演（严格只读，绝不提前加购或付款）。"""
    batch = load_active_speculative_batch(db)
    if not batch:
        return {"ok": False, "error": "当前无有效期内的推荐批次"}
    if batch_id and batch.get("batch_id") != batch_id:
        return {"ok": False, "error": "推荐批次已更新，跳过旧批次"}

    active_bid = str(batch["batch_id"])
    cur_slot = str(batch.get("slot") or "晚餐")
    max_budget = batch.get("max_budget")
    loc_target = dict(batch.get("loc_target") or {})
    policy = load_policy(db.paths)
    max_yuan = float((policy.get("spending_caps") or {}).get("meituan_single_order_yuan", 120.0))

    targets = [
        dict(e)
        for e in (batch.get("entries") or [])
        if (only_index is None or int(e.get("index") or 0) == only_index)
        and str(e.get("status") or "") in ("PLANNING", "ERROR", "")
    ]
    if not targets:
        return {"ok": True, "batch_id": active_bid, "ready_count": sum(1 for e in batch.get("entries", []) if e.get("status") == "READY")}

    lock = threading.Lock()
    # 先快速检查已有缓存，若无则与店铺菜单并行抓取账号外卖神券 + 商家代金券
    cached_coupons = load_cached_account_coupons(db) if fetch_coupons_fn is None else None
    coupons_holder: dict[str, Any] = dict(cached_coupons) if cached_coupons else {"platform_coupons": [], "store_vouchers": []}

    def _load_coupons() -> dict[str, Any]:
        if fetch_coupons_fn is not None:
            return fetch_coupons_fn() or {"platform_coupons": [], "store_vouchers": []}
        if cached_coupons is not None:
            return cached_coupons
        return fetch_meituan_account_coupons(db, force_refresh=False)

    def _plan_one(ent: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        idx = int(ent["index"])
        shop = str(ent["shop"])
        hist_combo_ent = ent.get("history_combo") or {}
        hist_dish_names = [
            str(it.get("name") or "").strip()
            for it in (hist_combo_ent.get("last_combo") or [])
            if isinstance(it, dict) and str(it.get("name") or "").strip()
        ]
        menu_res = (
            inspect_shop_fn(shop, loc_target)
            if inspect_shop_fn is not None
            else inspect_meituan_shop_menu(db, shop, loc_target=loc_target, history_dishes=hist_dish_names)
        )
        if menu_res.get("gated"):
            return idx, {
                "status": "GATED",
                "error": str((menu_res.get("gate") or {}).get("reason") or "需美团登录或安全验证"),
                "reason": "遇美团安全验证门禁，已合规停手",
            }
        if not menu_res.get("ok"):
            hist = ent.get("history_combo") or {}
            if hist.get("has_history") and hist.get("last_combo"):
                return idx, {
                    "status": "HISTORY_ONLY",
                    "dishes": [{"name": it["name"], "qty": it["qty"], "price": 0.0, "has_plus": False} for it in hist["last_combo"][:3]],
                    "est_total": float(hist.get("last_amount_yuan") or ent.get("avg_yuan") or 38.0),
                    "plan_source": "history_fallback",
                    "reason": f"实时菜单拉取未就绪({menu_res.get('reason') or 'timeout'})，暂按历史常点预备",
                    "error": str(menu_res.get("error") or menu_res.get("reason") or ""),
                }
            return idx, {
                "status": "FAILED",
                "error": str(menu_res.get("error") or menu_res.get("reason") or "menu_unavailable"),
                "reason": "实时菜单暂未就绪",
            }

        # 优先等账号优惠券就绪（已缓存/测试注入时 0ms；实时拉取与首个菜单抓取并发完成），让菜品推演直接纳入最优凑单省钱策略
        cur_coupons = coupons_holder
        try:
            c_ready = coupon_fut.result(timeout=12.0)
            if isinstance(c_ready, dict):
                cur_coupons = c_ready
        except Exception:
            pass

        inferred = infer_dishes_for_shop(
            shop_name=shop,
            menu_data=menu_res,
            history_combo=ent.get("history_combo"),
            cur_slot=cur_slot,
            max_budget=float(max_budget) if max_budget is not None else None,
            max_yuan=max_yuan,
            store_promos=menu_res.get("store_promos") or {},
            account_coupons=cur_coupons,
        )
        if not inferred.get("ok"):
            return idx, {
                "status": "FAILED",
                "menu_url": str(menu_res.get("menu_url") or ""),
                "live_shop_name": str(menu_res.get("live_shop_name") or shop),
                "error": str(inferred.get("reason") or "no_usable_dishes"),
                "reason": str(inferred.get("reason") or "暂无可自动推荐菜品"),
            }

        return idx, {
            "status": "READY",
            "live_shop_name": str(menu_res.get("live_shop_name") or shop),
            "menu_url": str(menu_res.get("menu_url") or ""),
            "store_promos": dict(menu_res.get("store_promos") or {}),
            "dishes": inferred["dishes"],
            "min_order": inferred["min_order"],
            "delivery_fee": inferred["delivery_fee"],
            "est_subtotal": inferred["est_subtotal"],
            "est_total": inferred["est_total"],
            "meets_min_order": inferred["meets_min_order"],
            "over_cap": inferred["over_cap"],
            "plan_source": inferred["plan_source"],
            "reason": inferred["reason"],
            "coupon_auto_topup": inferred.get("coupon_auto_topup"),
            "value_topup_tip": inferred.get("value_topup_tip") or "",
            "error": "",
        }

    workers = max(2 if (targets and cached_coupons is None) else 1, min(max(2, int(max_workers)), len(targets) + 1))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        coupon_fut = pool.submit(_load_coupons)
        shop_futs = [pool.submit(_plan_one, ent) for ent in targets]
        for fut in as_completed(shop_futs):
            try:
                idx, patch = fut.result()
                _save_batch_entry_update(db, active_bid, idx, patch, lock)
            except Exception:
                pass
        try:
            c_res = coupon_fut.result()
            if isinstance(c_res, dict):
                coupons_holder = c_res
        except Exception:
            pass

    # 将账户外卖神券 + 商家代金券 + 门店满减统一结算到每家 READY 店铺，得到券后预估到手价与凑单提示
    final_batch = load_active_speculative_batch(db) or batch
    if final_batch.get("batch_id") == active_bid:
        final_batch["account_coupons_brief"] = format_account_coupons_brief(coupons_holder)
        for ent in final_batch.get("entries") or []:
            if ent.get("status") == "READY":
                cp = compute_coupon_pricing(
                    shop_name=str(ent.get("live_shop_name") or ent.get("shop") or ""),
                    subtotal=float(ent.get("est_subtotal") or 0.0),
                    delivery_fee=float(ent.get("delivery_fee") or 0.0),
                    store_promos=ent.get("store_promos") or {},
                    account_coupons=coupons_holder,
                )
                if ent.get("value_topup_tip") and not cp.get("near_coupon_tip"):
                    cp["near_coupon_tip"] = str(ent["value_topup_tip"])
                elif ent.get("value_topup_tip"):
                    cp["near_coupon_tip"] = str(ent["value_topup_tip"])
                ent["coupon_pricing"] = cp
                ent["coupon_discount"] = cp["total_discount"]
                ent["est_after_coupon"] = cp["after_coupon_total"]
                ent["over_cap"] = float(cp["after_coupon_total"]) > max_yuan
        if all(str(e.get("status") or "") != "PLANNING" for e in (final_batch.get("entries") or [])):
            final_batch["status"] = "READY"
        final_batch["updated_at"] = iso_now()
        db.set_meta(SPECULATIVE_META_KEY, json.dumps(final_batch, ensure_ascii=False))

    ready_cnt = sum(1 for e in (final_batch.get("entries") or []) if e.get("status") == "READY")
    return {
        "ok": True,
        "batch_id": active_bid,
        "ready_count": ready_cnt,
        "total": len(final_batch.get("entries") or []),
        "account_coupons_count": len(coupons_holder.get("platform_coupons") or []),
    }


def find_speculative_entry(
    db: SimingDB,
    choice: str,
    wait_if_planning_sec: float = 0.0,
) -> Optional[dict[str, Any]]:
    """按编号（`1..5` / `第1家` / `第二个`）或店名查找当前有效投机预案；若仍在收尾可短等其变为 READY。"""
    q = (choice or "").strip()
    m_idx = re.match(r"^(?:第\s*([1-5一二三四五])\s*(?:家|个|号)?|([1-5])\s*(?:家|个|号)?)$", q)
    cn_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
    if m_idx:
        tok_idx = m_idx.group(1) or m_idx.group(2) or "1"
        want_idx = cn_map.get(tok_idx) or int(tok_idx)
    else:
        want_idx = None

    def _lookup() -> Optional[dict[str, Any]]:
        batch = load_active_speculative_batch(db)
        if not batch:
            return None
        entries = batch.get("entries") or []
        if want_idx is not None:
            for e in entries:
                if int(e.get("index") or 0) == want_idx:
                    return dict(e)
            return None
        qn = _norm_shop(q)
        qb = _brand_stem(q)
        if not qn:
            return None
        for e in entries:
            sn = _norm_shop(str(e.get("shop") or ""))
            sb = _brand_stem(str(e.get("shop") or ""))
            if qn in sn or sn in qn or (len(qb) >= 2 and qb == sb):
                return dict(e)
        return None

    ent = _lookup()
    if ent is None or ent.get("status") != "PLANNING" or wait_if_planning_sec <= 0:
        return ent

    deadline = time.monotonic() + wait_if_planning_sec
    while time.monotonic() < deadline:
        time.sleep(0.4)
        ent2 = _lookup()
        if ent2 is None or ent2.get("status") != "PLANNING":
            return ent2
    return _lookup()


def format_dishes_brief(dishes: list[dict[str, Any]]) -> str:
    parts: list[str] = []
    for d in dishes or []:
        nm = str(d.get("name") or "")
        qty = int(d.get("qty") or 1)
        pr = d.get("price")
        q_s = f"×{qty}" if qty > 1 else ""
        p_s = f"(¥{float(pr):.1f})" if pr and float(pr) > 0 else ""
        parts.append(f"{nm}{q_s}{p_s}")
    return " + ".join(parts)


def format_coupon_price_line(
    est_subtotal: float,
    delivery_fee: float,
    est_total: float,
    coupon_pricing: Optional[dict[str, Any]] = None,
) -> str:
    """格式化原价 + 券后预估到手价 + 优惠明细 + 店内可领券/凑单提示。"""
    cp = coupon_pricing or {}
    deliv_s = f" + 配送¥{delivery_fee:g}" if delivery_fee > 0 else ""
    raw_part = f"餐品¥{est_subtotal:g}{deliv_s} = ¥{est_total:.1f}"
    disc = float(cp.get("total_discount") or 0.0)
    after_tot = float(cp.get("after_coupon_total") if cp.get("after_coupon_total") is not None else est_total)

    extras: list[str] = []
    if disc > 0:
        applied_s = " + ".join(str(x) for x in (cp.get("applied_coupons") or []) if x)
        price_str = f"{raw_part} → 🎫 券后预估 **¥{after_tot:.1f}**（省¥{disc:g}" + (f"：{applied_s}" if applied_s else "") + "）"
    else:
        price_str = f"{raw_part}（≈ **¥{est_total:.1f}**）"

    for cl in cp.get("claimable_notes") or []:
        extras.append(str(cl))
    if cp.get("near_coupon_tip"):
        extras.append(f"💡{cp['near_coupon_tip']}")
    if extras:
        price_str += " · " + " · ".join(extras)
    return price_str


def format_speculative_plans_overview(db: SimingDB) -> str:
    """格式化展示本轮 5 家店的并行投机推演结果（含菜品、原价、券后预估价、优惠券明细与凑单提示）。"""
    batch = load_active_speculative_batch(db)
    if not batch:
        return "目前没有进行中或 20 分钟内有效的美团投机推演方案，先发一句「我要点晚饭了，推荐点」即可生成。"

    entries = batch.get("entries") or []
    ready_cnt = sum(1 for e in entries if e.get("status") == "READY")
    planning_cnt = sum(1 for e in entries if e.get("status") == "PLANNING")
    poi = str(batch.get("delivery_poi") or "当前位置")

    state_tag = f"已全部就绪 {ready_cnt}/{len(entries)}" if planning_cnt == 0 else f"已就绪 {ready_cnt}/{len(entries)}（剩余 {planning_cnt} 家推演中）"
    lines = [f"⚡ **美团投机推演菜品与券后价预案**（定位：**{poi}** · {state_tag}）"]
    coupons_brief = str(batch.get("account_coupons_brief") or "").strip()
    if coupons_brief:
        lines.append(f"• 🎫 账户当前可用外卖券：`{coupons_brief}`")

    for e in entries:
        idx = e["index"]
        shop = e["shop"]
        st = str(e.get("status") or "")
        if st == "READY":
            dish_s = format_dishes_brief(e.get("dishes") or [])
            price_s = format_coupon_price_line(
                est_subtotal=float(e.get("est_subtotal") or 0.0),
                delivery_fee=float(e.get("delivery_fee") or 0.0),
                est_total=float(e.get("est_total") or 0.0),
                coupon_pricing=e.get("coupon_pricing"),
            )
            cap_warn = " ⚠️超上限" if e.get("over_cap") else ""
            lines.append(
                f"{idx}. ✅ **{shop}**{cap_warn}\n"
                f"   • 预推菜品：{dish_s}（{e.get('reason')}）\n"
                f"   • 价格估算：{price_s}"
            )
        elif st == "PLANNING":
            hint_s = f"（历史常点：{e['history_preview']}，实时菜单与券后价核验中…）" if e.get("history_preview") else "（后台并行拉取实时菜单与优惠券中…）"
            lines.append(f"{idx}. ⏳ **{shop}** {hint_s}")
        elif st == "HISTORY_ONLY":
            dish_s = format_dishes_brief(e.get("dishes") or [])
            lines.append(f"{idx}. 🟡 **{shop}** — 暂按常点预备：{dish_s}（约 ¥{float(e.get('est_total') or 0):.0f}）")
        elif st == "GATED":
            lines.append(f"{idx}. 🛡️ **{shop}** — 遇美团验证门禁已合规停手")
        else:
            lines.append(f"{idx}. ⚠️ **{shop}** — {e.get('reason') or '实时菜单未就绪'}")

    lines.append("👉 直接回 `确认第1家`（或 `就点第2家`）立刻按预推菜品在后台自动加购；回 `第1家` 可先看单店确认卡。")
    return "\n".join(lines)


def execute_speculative_cart_add(
    db: SimingDB,
    plan: dict[str, Any],
    port: int = 9222,
    cdp_exec_fn: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """用户确认后，直达预解析 `menu_url`，用真实 CDP 鼠标点击把预推菜品逐项加入美团 H5 购物车并回读车内实价（严格停在付款前）。"""
    if cdp_exec_fn is not None:
        return cdp_exec_fn(plan)
    if os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return {"ok": False, "reason": "offline"}

    menu_url = str(plan.get("menu_url") or "").strip()
    dishes = list(plan.get("dishes") or [])
    if not menu_url or ("mindex/menu" not in menu_url and "sub-trade/restaurant" not in menu_url) or not dishes:
        return {"ok": False, "reason": "no_preplanned_menu"}

    base = resolve_chromium_cdp_base(port=port)
    if not base or not base.get("ws_browser"):
        return {"ok": False, "reason": "cdp_unavailable"}

    from siming.meituan_agent import (
        resolve_meituan_delivery_target,
        set_cdp_geolocation_for_target,
        sync_meituan_h5_location,
    )
    from siming.meituan_cart_live import (
        CARD_SEL,
        SEARCH_CARD_SEL,
        _clear_mindex_cart_if_unrelated,
        _inner_search_dish,
        _mindex_set_dish_qty,
        _prune_subtrade_unrelated_cart,
        _set_dish_qty,
    )

    qs_m = urllib.parse.parse_qs(urllib.parse.urlsplit(menu_url).query)
    poi_id = (qs_m.get("poi_id_str") or [""])[0]
    is_subtrade = "sub-trade/restaurant" in menu_url
    loc_target = resolve_meituan_delivery_target(db, location_override=str(plan.get("loc_override") or ""))

    tid = ""
    try:
        with LocalWsCdpClient(base["ws_browser"], timeout=6.0) as b_ws:
            tid = str(b_ws.call("Target.createTarget", {"url": "about:blank", "background": True}).get("targetId") or "")
        if not tid:
            return {"ok": False, "reason": "create_target_failed"}

        page_ws = base["ws_browser"].split("/devtools/")[0] + f"/devtools/page/{tid}"
        with LocalWsCdpClient(page_ws, timeout=22.0) as p_ws:
            _setup_mobile_page(p_ws)
            set_cdp_geolocation_for_target(p_ws, loc_target)
            p_ws.call("Page.navigate", {"url": menu_url})

            if not is_subtrade and loc_target.get("has_location"):
                time.sleep(1.5)
                s_res = sync_meituan_h5_location(p_ws, loc_target)
                if s_res.get("need_reload"):
                    p_ws.call("Page.navigate", {"url": menu_url})

            # 等待菜单渲染
            deadline = time.monotonic() + (13.0 if is_subtrade else 8.5)
            while time.monotonic() < deadline:
                time.sleep(0.5)
                _setup_mobile_page(p_ws)
                snap = _extract_menu_dom_snapshot(p_ws)
                gate = detect_compliance_gate(
                    str(snap.get("url") or menu_url),
                    str(snap.get("title") or ""),
                    str(snap.get("bodyHead") or ""),
                )
                if gate is not None:
                    return {"ok": False, "gated": True, "gate": gate, "target_id": tid, "kept_tab_open": True}
                if len(snap.get("cards") or []) >= 1:
                    break

            added: list[str] = []
            failed: list[str] = []

            if is_subtrade:
                if poi_id:
                    _prune_subtrade_unrelated_cart(p_ws, poi_id, dishes)
                used_inner_sub = False
                for dish in dishes:
                    d_name = str(dish.get("name") or "").strip()
                    d_qty = max(1, int(dish.get("qty") or 1))
                    if not d_name:
                        continue
                    try:
                        try:
                            _set_dish_qty(p_ws, d_name, d_qty, CARD_SEL, poi_id=poi_id)
                        except Exception:
                            if not poi_id:
                                raise
                            _inner_search_dish(p_ws, poi_id, d_name)
                            used_inner_sub = True
                            _set_dish_qty(p_ws, d_name, d_qty, SEARCH_CARD_SEL, poi_id=poi_id)
                        added.append(f"{d_name}×{d_qty}" if d_qty > 1 else d_name)
                    except Exception:
                        failed.append(d_name)
                if used_inner_sub:
                    p_ws.call("Page.navigate", {"url": menu_url})
                    time.sleep(4.0)
                    _setup_mobile_page(p_ws)
            else:
                if poi_id:
                    _clear_mindex_cart_if_unrelated(p_ws, poi_id, dishes)
                used_menusearch = False
                for dish in dishes:
                    d_name = str(dish.get("name") or "").strip()
                    d_qty = max(1, int(dish.get("qty") or 1))
                    d_norm = _norm_dish(d_name)
                    if not d_norm:
                        continue

                    def _find_dish_btn() -> Optional[dict[str, Any]]:
                        raw_pos = _eval_val(
                            p_ws,
                            f"""(() => {{
                                const want = {json.dumps(d_norm)};
                                const norm = s => (s||'').toLowerCase().replace(/【[^】]*】|\\[[^\\]]*\\]/g,'').replace(/[^a-z0-9\\u4e00-\\u9fff]+/g,'');
                                const btns = Array.from(document.querySelectorAll('.sqt-menu-add-buttons')).filter(b => {{
                                    const br = b.getBoundingClientRect();
                                    return br.width > 0 && br.height > 0;
                                }});
                                for (const btn of btns) {{
                                    let card = btn;
                                    for (let i = 0; i < 8 && card.parentElement; i++) {{
                                        card = card.parentElement;
                                        const t = (card.innerText || '').trim();
                                        if (t.includes('¥') && (t.includes('月售') || t.split('\\n').length >= 3)) break;
                                    }}
                                    const firstLine = ((card.innerText || '').split('\\n').map(s => s.trim()).filter(Boolean)[0]) || '';
                                    const nf = norm(firstLine);
                                    if (!nf) continue;
                                    if (nf === want || nf.includes(want) || want.includes(nf)) {{
                                        const plus = btn.querySelector('[class*="plus"]');
                                        const tgt = plus || btn;
                                        tgt.scrollIntoView({{block: 'center'}});
                                        const r = tgt.getBoundingClientRect();
                                        return JSON.stringify({{hasPlus: Boolean(plus), x: r.left + r.width/2, y: r.top + r.height/2, matched: firstLine}});
                                    }}
                                }}
                                return '';
                            }})()""",
                        )
                        if isinstance(raw_pos, str) and raw_pos.startswith("{"):
                            return json.loads(raw_pos)
                        return None

                    btn_info = _find_dish_btn() if not used_menusearch else None
                    if btn_info is None and not used_menusearch:
                        p_ws.call("Input.dispatchMouseEvent", {"type": "mouseWheel", "x": 260, "y": 500, "deltaX": 0, "deltaY": 1600})
                        time.sleep(0.8)
                        btn_info = _find_dish_btn()

                    if btn_info is None:
                        # 门店默认分类未展示该菜品时，自动走店内搜索 `mindex/menusearch` 精确加购
                        try:
                            info_ms, used_ms = _mindex_set_dish_qty(p_ws, menu_url, poi_id, d_name, d_qty)
                            used_menusearch = used_menusearch or used_ms
                            ok_q = int(info_ms.get("qty") or d_qty)
                            added.append(f"{d_name}×{ok_q}" if ok_q > 1 else d_name)
                        except Exception:
                            failed.append(d_name)
                        continue

                    ok_clicks = 0
                    for _ in range(d_qty):
                        _cdp_click_xy(p_ws, float(btn_info["x"]), float(btn_info["y"]))
                        time.sleep(0.75)
                        if btn_info.get("hasPlus"):
                            ok_clicks += 1
                        else:
                            modal_btn = _eval_val(
                                p_ws,
                                """(() => {
                                    const cands = Array.from(document.querySelectorAll('div,button,span')).filter(e => {
                                        const t = (e.innerText || '').trim();
                                        const r = e.getBoundingClientRect();
                                        return ['加入购物车', '选好了'].includes(t) && r.width > 30 && r.height > 15 && e.children.length <= 1;
                                    });
                                    if (!cands.length) return '';
                                    const b = cands[cands.length - 1];
                                    const r = b.getBoundingClientRect();
                                    return JSON.stringify({x: r.left + r.width/2, y: r.top + r.height/2});
                                })()""",
                            )
                            if isinstance(modal_btn, str) and modal_btn.startswith("{"):
                                mb = json.loads(modal_btn)
                                _cdp_click_xy(p_ws, float(mb["x"]), float(mb["y"]))
                                time.sleep(0.85)
                                ok_clicks += 1
                    if ok_clicks > 0:
                        added.append(f"{d_name}×{ok_clicks}" if ok_clicks > 1 else d_name)
                    else:
                        failed.append(d_name)

                if used_menusearch:
                    p_ws.call("Page.navigate", {"url": menu_url})
                    time.sleep(2.4)
                    _setup_mobile_page(p_ws)

            time.sleep(0.9)
            cart_raw = _eval_val(
                p_ws,
                """JSON.stringify({
                    url: location.href,
                    title: document.title || '',
                    cartText: (
                        document.querySelector('[class*="cartContainer"]')?.innerText ||
                        document.querySelector('[class*="bottom-cart"], [class*="cart-bar"], [class*="shopcart"]')?.innerText ||
                        ''
                    ).trim(),
                    count: (() => {
                        const m = document.querySelector('[class*="cartCornerMark"], [class*="cornerMark"]');
                        return m ? (parseInt((m.innerText || '').trim(), 10) || 0) : 0;
                    })(),
                    payable: (document.querySelector('[class*="payableAmount"]')?.innerText || '').trim(),
                    delivery: (document.querySelector('[class*="deliveryFee"]')?.innerText || '').trim(),
                    checkout: (
                        Array.from(document.querySelectorAll('[class*="goToPreview"]')).find(e => e.getBoundingClientRect().width > 0)?.innerText ||
                        Array.from(document.querySelectorAll('mt-view, mt-text, div, span, button')).find(e => (e.innerText || '').trim() === '去结算' && e.getBoundingClientRect().width > 0)?.innerText ||
                        ''
                    ).trim()
                })""",
            )
            cart_obj = json.loads(cart_raw) if isinstance(cart_raw, str) and cart_raw.startswith("{") else {}
            cart_text = str(cart_obj.get("cartText") or "").replace("\n", " · ")
            cart_count = int(cart_obj.get("count") or 0)
            checkout_txt = str(cart_obj.get("checkout") or "")
            checkout_ready = ("去结算" in checkout_txt) or ("去结算" in cart_text)

            return {
                "ok": bool(added),
                "added_to_cart": bool(added) and (is_subtrade or cart_count > 0 or checkout_ready or bool(cart_obj.get("payable"))),
                "gated": False,
                "store_kind": "subtrade" if is_subtrade else "mindex",
                "poi_id_str": poi_id,
                "shop": str(cart_obj.get("title") or plan.get("shop") or ""),
                "added_dishes": added,
                "failed_dishes": failed,
                "cart_count": cart_count,
                "payable_text": str(cart_obj.get("payable") or ""),
                "delivery_text": str(cart_obj.get("delivery") or ""),
                "checkout_status": checkout_txt or ("去结算" if checkout_ready else ""),
                "cart_summary": cart_text,
                "checkout_ready": checkout_ready,
                "delivery_poi": str(plan.get("delivery_poi") or ""),
                "menu_url": menu_url,
                "target_id": tid,
                "kept_tab_open": True,
            }
    except Exception as exc:
        return {"ok": False, "reason": "cdp_error", "error": str(exc)[:200], "target_id": tid}


def confirm_and_execute_speculative_pick(
    db: SimingDB,
    choice_raw: str,
    wait_ready_sec: float = 5.0,
    cdp_exec_fn: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
    inspect_shop_fn: Optional[Callable[[str, dict[str, Any]], dict[str, Any]]] = None,
    preview_fn: Optional[Callable[[SimingDB, str, str], dict[str, Any]]] = None,
    location_override: str = "",
) -> dict[str, Any]:
    """用户明确表达「确认第N家 / 就点第N家」后，按预推菜品后台加购并直达结算预览签发付款许可确认卡（零扣款；离线/单测回落加购审计）。"""
    from siming.approval import create_approval, verify_and_apply_approval
    from siming.meituan_agent import execute_meituan_lock_cart

    batch = load_active_speculative_batch(db)
    if not batch:
        return {
            "ok": False,
            "reply": "⚠️ 当前没有 20 分钟内有效的推荐批次，请先发一句「我要点晚饭了，推荐点」或直接 `点外卖 <店名> <菜品>`。",
        }

    ent = find_speculative_entry(db, choice_raw, wait_if_planning_sec=wait_ready_sec)
    if ent is None:
        return {
            "ok": False,
            "reply": f"⚠️ 在本轮推荐里未找到 `{choice_raw}`（可回 `看看方案` 查看 1~{len(batch.get('entries') or [])} 号店铺）。",
        }

    # 若后台进程尚未跑到这一家，当场同步补推这一家（只推用户点名的 1 家）
    if str(ent.get("status") or "") == "PLANNING":
        run_speculative_meal_preplan(
            db,
            batch_id=str(batch["batch_id"]),
            inspect_shop_fn=inspect_shop_fn,
            max_workers=1,
            only_index=int(ent["index"]),
        )
        ent = find_speculative_entry(db, str(ent["index"]), wait_if_planning_sec=0.0) or ent

    if str(ent.get("status") or "") == "GATED":
        return {
            "ok": False,
            "gated": True,
            "reply": f"🛡️ **「{ent['shop']}」** 在预检时触发美团验证门禁（{ent.get('error')}），请先在桌面 Chromium 美团标签完成验证再确认。",
        }

    policy = load_policy(db.paths)
    max_yuan = float((policy.get("spending_caps") or {}).get("meituan_single_order_yuan", 120.0))
    cp = ent.get("coupon_pricing") or {}
    est_raw_total = float(ent.get("est_total") or ent.get("avg_yuan") or 0.0)
    est_pay_total = float(ent.get("est_after_coupon") if ent.get("est_after_coupon") is not None else est_raw_total)
    if est_pay_total > max_yuan:
        return {
            "ok": False,
            "over_cap": True,
            "reply": f"🛑 **「{ent['shop']}」** 预推方案券后约 **¥{est_pay_total:.1f}**，超过美团单笔安全上限（¥{max_yuan:.0f}），已拦截。",
        }

    shop = str(ent.get("live_shop_name") or ent.get("shop") or "").strip()
    dishes = list(ent.get("dishes") or [])
    dish_brief = format_dishes_brief(dishes) or str(ent.get("history_preview") or "店内招牌套餐")
    deeplink = f"imeituan://www.meituan.com/waimai/search?query={urllib.parse.quote(str(ent.get('shop') or shop))}"

    params = {
        "shop": shop,
        "item_spec": dish_brief,
        "dishes": dishes,
        "menu_url": str(ent.get("menu_url") or ""),
        "delivery_poi": str(ent.get("delivery_poi") or batch.get("delivery_poi") or ""),
        "loc_override": location_override,
        "est_yuan": est_pay_total,
        "est_raw_total": est_raw_total,
        "coupon_pricing": cp,
        "speculative_batch_id": str(batch["batch_id"]),
        "confirmed_via": "qq_direct_confirm",
        "deeplink": deeplink,
    }

    # 在线实时模式（或测试注入 preview_fn）：零扣款一步完成「加购 → mindex/preview 核验真实实付 → 签发单张付款许可确认卡」
    if preview_fn is not None or (cdp_exec_fn is None and os.environ.get("SIMING_TEST_OFFLINE") != "1"):
        from siming.meituan_pay import stage_meituan_payment_preview

        cart_res = execute_speculative_cart_add(db, params, cdp_exec_fn=cdp_exec_fn)
        if cart_res.get("gated"):
            gate = cart_res.get("gate") or {}
            return {
                "ok": False,
                "gated": True,
                "reply": (
                    f"🛡️ **美团进店加购触发合规门禁**（门店：**{shop}**）\n"
                    f"• 原因：{gate.get('reason') or '需登录或滑块验证'}\n"
                    f"• 司命已原样保留桌面 Chromium 后台标签页（绝不绕过验证），完成后重发即可；或手机唤端：`{deeplink}`"
                ),
            }
        if cart_res.get("ok") and cart_res.get("added_to_cart") and (cart_res.get("target_id") or preview_fn is not None):
            pay_stage = stage_meituan_payment_preview(
                db,
                cart_res=cart_res,
                shop=shop,
                item_spec=dish_brief,
                deeplink=deeplink,
                preview_fn=preview_fn,
                location_override=location_override,
            )
            if pay_stage.get("ok") or pay_stage.get("gated") or pay_stage.get("over_cap"):
                return {
                    **pay_stage,
                    "dishes": dishes,
                    "est_after_coupon": est_pay_total,
                    "coupon_pricing": cp,
                }

    app = create_approval(
        db,
        action="meituan_lock_cart",
        target_id=f"MT-{re.sub(r'[^A-Za-z0-9\u4e00-\u9fff]+', '', shop)[:14] or 'ORDER'}",
        params=params,
        ttl_minutes=15,
    )

    extra_exec = None
    if cdp_exec_fn is not None:
        extra_exec = {
            "meituan_lock_cart": lambda p, d: execute_meituan_lock_cart(
                p,
                d,
                cdp_cart_exec_fn=cdp_exec_fn,
            )
        }
    applied = verify_and_apply_approval(db, app["token"], extra_executors=extra_exec)
    if not applied.get("ok"):
        return {"ok": False, "reply": f"❌ [{app['token']}] 执行失败：{applied.get('error')}"}

    coupon_note = ""
    if cp and float(cp.get("total_discount") or 0) > 0:
        applied_s = " + ".join(str(x) for x in (cp.get("applied_coupons") or []) if x)
        coupon_note = f"\n🎫 结算页勾选优惠券（{applied_s}）后预计实付约 **¥{est_pay_total:.1f}**（原价 ¥{est_raw_total:.1f}，立省 ¥{float(cp['total_discount']):g}）"
        if cp.get("claimable_notes"):
            coupon_note += "；" + "、".join(cp["claimable_notes"])

    return {
        "ok": True,
        "approval_token": app["token"],
        "shop": shop,
        "dishes": dishes,
        "est_after_coupon": est_pay_total,
        "coupon_pricing": cp,
        "reply": f"✅ [{app['token']}] 已按你的确认执行预操作！\n{applied['summary']}{coupon_note}",
    }
