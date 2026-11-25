"""美团外卖 & 闪购最优红包/代金券/配送券省钱策略与结算页选券引擎（meituan_coupons）。

统一覆盖两大核心场景的省钱闭环：
1. **点饭（餐饮外卖 `mindex/menu` → `mindex/preview`）**：
   - 账户神券/红包、商家代金券、门店满减全量解析与真实门槛计价（含打包费、区分可否叠加）；
   - 投机推演自动凑单省钱：若加一份平价配菜/饮品能跨入更高档大额神券且**券后实付严格更低（倒省钱）**，自动凑单；否则给出精准到具体菜品的凑单建议；
   - 结算预览页 `machpro/red_packet` 遍历全部可用券，按「立减最大 → 门槛最高（省下无门槛小券给小单）→ 临近过期优先 → 专享券优先于通用券」择优勾选，严格剔除付费买券包/省钱卡陷阱，并叠加核验商家代金券与配送券（防互斥涨价回滚）。
2. **补货（商超/医药闪购 `sub-trade/restaurant` → `package/preview/preview`）**：
   - 同步解析账户内的**闪购/超市/便利店/医药/水果神券 + 吃喝玩乐通用券 + 商家代金券**（不再把商超补货神券当垃圾过滤）；
   - 补货提醒卡与备单确认卡自动测算：① 当前单可用最优神券；② 同地点同商超临期日用品**合单省运费 + 跨档凑大额券**；③ 消耗品多买 1 件是否倒省钱或显著降低单价；
   - 修复 `package/preview/preview` 商超结算页此前不选券/漏认全角 `￥`/误点遮罩关闭抽屉的问题，双架构统一走最优三券择优闭环。
"""

from __future__ import annotations

import itertools
import json
import re
import time
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from siming.cdp import LocalWsCdpClient
from siming.db import SHANGHAI_TZ, SimingDB, now_local

_PAID_UPSELL_RE = re.compile(
    r"(购买|去购买|立即购买|立即抢购|开通|去开通|立即开通|省钱卡|神券包|膨胀神券包|会员卡|"
    r"付费|开卡|首月|连续包月|续费|元购|元买|花\s*\d+(?:\.\d+)?\s*元|抢购\s*\d+张|"
    r"[¥￥]\s*\d+(?:\.\d+)?\s*(?:买|购|抢|开通))"
)
_BENIGN_SCOPE_UNAVAIL_RE = re.compile(
    r"(?:部分商品|特价商品|折扣商品|活动商品|爆品|部分门店|部分商家|部分品类|部分活动|"
    r"到店自取|自取订单|自取|拼好饭|团购|酒水|饮品|以下商品|指定商品以外)不可(?:用|使用|享)"
)
_UNAVAILABLE_CARD_RE = re.compile(
    r"(本单不可用|本单不可使用|暂不可用|不可使用|不满足|未满\s*[¥￥]?\s*\d|已过期|已抢光|已领完|"
    r"未到使用时间|不在使用时段|去凑单|凑单可用|还差\s*[¥￥]?\s*\d|差\s*[¥￥]?\s*\d+(?:\.\d+)?\s*元?可用)"
)
_EXPANDABLE_COUPON_RE = re.compile(r"(可膨胀|去膨胀|最高膨至\s*[¥￥]?\s*(\d+(?:\.\d+)?)\s*元?)")
_GAP_YUAN_RE = re.compile(r"(?:还差|再买|差)\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*元?(?:可用|可减|立减)?")
_THRESHOLD_RE = re.compile(r"满\s*(\d+(?:\.\d{1,2})?)\s*元?(?:可用|减|使用)?")
_EXPIRE_DT_RE = re.compile(r"有效期至\s*(\d{4}[.\-/]\d{2}[.\-/]\d{2}(?:\s+\d{2}:\d{2}(?::\d{2})?)?)")
_EXPIRE_HOURS_RE = re.compile(r"(?:仅剩|剩余|剩)\s*(\d+)\s*小时")
_EXPIRE_TODAY_RE = re.compile(r"(今日|今天|当日).{0,8}(?:到期|过期|失效|23:59)")
_EXPIRE_DAYS_RE = re.compile(r"(?:仅剩|剩余|剩)\s*(\d+)\s*天")

_UNIVERSAL_COUPON_RE = re.compile(r"(吃喝玩乐|全场通用|通用神券|全平台)")
_SUPERMARKET_COUPON_RE = re.compile(
    r"(闪购|便利店|超市|小象|日百|百货|松鼠|买菜|商超|零售|即购|乐购达|酒水|日用)"
)
_PHARMACY_COUPON_RE = re.compile(r"(医药|大药房|买药|药房|药店|健康守护|医疗|器械|叮当)")
_FRUIT_COUPON_RE = re.compile(r"(水果|果切|鲜果|生鲜)")
_OTHER_NON_ORDER_COUPON_RE = re.compile(
    r"(服饰|暖衣|数码|宠物|母婴|美妆|酒店|机票|火车票|KTV|洗浴|按摩|医美|丽人|鲜花|电影|门票|玩乐团购)"
)

_SHOP_PHARMACY_RE = re.compile(r"(药房|大药房|药店|快药|医疗|器械|京亿康|养生堂|叮当|海王星辰|高济)")
_SHOP_FRUIT_RE = re.compile(r"(果切|水果|鲜果|示例果切|百果园|鲜丰)")
_SHOP_SUPERMARKET_RE = re.compile(
    r"(超市|便利店|小象|即购|乐购达|屈臣氏|名创|五金|闪购|商超|永辉|物美|盒马|沃尔玛|7-?11|罗森|全家|便利蜂|京东便利)"
)

_CARD_AMOUNT_PATS = (
    re.compile(r"-\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)"),
    re.compile(r"[¥￥]\s*\n?\s*(\d+(?:\.\d{1,2})?)"),
    re.compile(r"(?:减|立减|抵扣|省)\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)\s*元?"),
    re.compile(r"(\d+(?:\.\d{1,2})?)\s*元(?:红包|神券|代金券|优惠券|配送券|券)"),
)


def classify_coupon_category(title: str, rule: str = "", shop: str = "") -> str:
    """将美团红包/神券分类为 food / supermarket / pharmacy / fruit / universal / other。"""
    t = (title or "").strip()
    r = (rule or "").strip()
    s = (shop or "").strip()
    hay_title = f"{t} {s}".strip()

    # 1. 明显非外卖/非日用品补货品类（服饰、酒店、KTV、医美等）优先排除
    if _OTHER_NON_ORDER_COUPON_RE.search(hay_title):
        return "other"
    if re.search(r"(服饰商家|数码商家|美妆商家|母婴商家|宠物商家|鲜花商家)", r):
        return "other"

    # 2. 医药/买药
    if _PHARMACY_COUPON_RE.search(hay_title) or re.search(r"(医药|大药房|买药|药店)", r):
        return "pharmacy"

    # 3. 商超/闪购/便利店/小象
    if _SUPERMARKET_COUPON_RE.search(hay_title) or re.search(r"(闪购.{0,8}商家|便利店.{0,8}商家|超市.{0,8}商家|松鼠便利|小象)", r):
        return "supermarket"

    # 4. 水果/果切专享
    if _FRUIT_COUPON_RE.search(hay_title) and not _UNIVERSAL_COUPON_RE.search(hay_title):
        return "fruit"

    # 5. 吃喝玩乐 / 全场通用神券（同时可用于餐饮与闪购/水果神券商家）
    if _UNIVERSAL_COUPON_RE.search(t):
        return "universal"

    # 6. 默认归入餐饮外卖神券
    return "food"


def infer_order_category(
    shop_name: str = "",
    *,
    store_kind: str = "",
    item_names: Optional[list[str]] = None,
) -> str:
    """根据门店名、架构类型与商品名推断订单所属券品类（food / supermarket / pharmacy / fruit）。"""
    s = (shop_name or "").strip()
    items_hay = " ".join(str(x) for x in (item_names or []) if x)
    if _SHOP_PHARMACY_RE.search(s):
        return "pharmacy"
    if _SHOP_FRUIT_RE.search(s):
        return "fruit"
    if _SHOP_SUPERMARKET_RE.search(s):
        return "supermarket"
    if store_kind == "subtrade":
        if re.search(r"(口罩|针头|酒精|棉片|药|布洛芬|感冒|米诺地尔)", items_hay):
            return "pharmacy"
        if re.search(r"(西瓜|果切|草莓|芒果|车厘子|水果)", items_hay):
            return "fruit"
        return "supermarket"
    return "food"


def is_coupon_applicable_to_category(coupon_category: str, order_category: str) -> bool:
    """判断某类平台券是否可用于指定订单品类。"""
    cc = (coupon_category or "food").strip().lower()
    oc = (order_category or "food").strip().lower()
    if cc == "other":
        return False
    if cc == "universal":
        return True
    if oc == "food":
        return cc == "food"
    if oc == "supermarket":
        return cc == "supermarket"
    if oc == "pharmacy":
        return cc in ("pharmacy", "supermarket")
    if oc == "fruit":
        return cc in ("fruit", "supermarket", "food")
    return cc == oc


_BRAND_LOCKED_COUPON_PATTERNS: tuple[tuple[str, re.Pattern[str], re.Pattern[str]], ...] = (
    ("松鼠便利", re.compile(r"(松鼠神券|松鼠便利)"), re.compile(r"松鼠")),
    ("屈臣氏", re.compile(r"屈臣氏"), re.compile(r"屈臣氏")),
    ("小象超市", re.compile(r"小象"), re.compile(r"小象")),
    ("名创优品", re.compile(r"名创"), re.compile(r"名创")),
    ("叮当快药", re.compile(r"叮当"), re.compile(r"叮当")),
    ("乐购达", re.compile(r"乐购达"), re.compile(r"乐购达")),
    ("罗森", re.compile(r"罗森"), re.compile(r"罗森")),
    ("全家", re.compile(r"全家"), re.compile(r"全家")),
    ("便利蜂", re.compile(r"便利蜂"), re.compile(r"便利蜂")),
    ("物美", re.compile(r"物美"), re.compile(r"物美")),
    ("永辉", re.compile(r"永辉"), re.compile(r"永辉")),
    ("沃尔玛", re.compile(r"沃尔玛"), re.compile(r"沃尔玛")),
)


def is_coupon_applicable_to_shop(coupon: dict[str, Any], shop_name: str) -> bool:
    """过滤品牌专属闪购券（如「闪购松鼠神券」「闪购屈臣氏专属神券」），严防串算到其他商超（如即购超市）。"""
    title = str(coupon.get("title") or "")
    rule = str(coupon.get("rule") or "")
    hay = f"{title} {rule}"
    sn = (shop_name or "").strip()
    for _brand, lock_re, shop_re in _BRAND_LOCKED_COUPON_PATTERNS:
        if lock_re.search(hay):
            return bool(sn and shop_re.search(sn))
    return True


def is_paid_coupon_upsell(text: str) -> bool:
    """识别美团选券页夹杂的付费购买神券包/开通省钱卡/加价膨胀诱饵，严禁误点。"""
    return bool(_PAID_UPSELL_RE.search(text or ""))


def _parse_expiry_ts(text: str, now_dt: Optional[datetime] = None) -> float:
    """将券面有效期解析为可比较的 Unix 时间戳（无法解析时返回极大值）。"""
    now_cur = now_dt or now_local()
    s = (text or "").strip()
    if not s:
        return 9e12
    m_dt = _EXPIRE_DT_RE.search(s)
    if m_dt:
        raw = m_dt.group(1).strip().replace("-", ".").replace("/", ".")
        for fmt, end_of_day in (
            ("%Y.%m.%d %H:%M:%S", False),
            ("%Y.%m.%d %H:%M", False),
            ("%Y.%m.%d", True),
        ):
            try:
                dt = datetime.strptime(raw, fmt).replace(tzinfo=SHANGHAI_TZ)
                if end_of_day:
                    dt = dt.replace(hour=23, minute=59, second=59)
                return dt.timestamp()
            except ValueError:
                continue
    m_h = _EXPIRE_HOURS_RE.search(s)
    if m_h:
        return (now_cur + timedelta(hours=int(m_h.group(1)))).timestamp()
    if _EXPIRE_TODAY_RE.search(s):
        return now_cur.replace(hour=23, minute=59, second=59).timestamp()
    m_d = _EXPIRE_DAYS_RE.search(s)
    if m_d:
        return (now_cur + timedelta(days=int(m_d.group(1)))).timestamp()
    return 9e12


def parse_coupon_card_info(
    text: str,
    *,
    dom_idx: int = 0,
    x: float = 0.0,
    y: float = 0.0,
    card_x: float = 0.0,
    card_y: float = 0.0,
    selected: bool = False,
    now_dt: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """将结算页/选券页单张券卡片的可见文本解析为结构化候选信息。"""
    raw = (text or "").strip()
    if not raw:
        return None
    if is_paid_coupon_upsell(raw):
        return {
            "dom_idx": dom_idx,
            "amount_yuan": 0.0,
            "threshold_yuan": 0.0,
            "usable": False,
            "is_paid_upsell": True,
            "is_unavailable": True,
            "raw_head": raw.replace("\n", " ")[:100],
        }

    # 先剥离「部分商品不可用」「到店自取不可用」等常规说明文案，严防把有效通用神券（如吃喝玩乐¥5）误判为不可用！
    raw_for_unavail = _BENIGN_SCOPE_UNAVAIL_RE.sub(" ", raw)
    is_unavail = bool(_UNAVAILABLE_CARD_RE.search(raw_for_unavail))
    gap_yuan: Optional[float] = None
    m_gap = _GAP_YUAN_RE.search(raw)
    if m_gap:
        gap_yuan = round(float(m_gap.group(1)), 2)
        is_unavail = True

    m_exp_b = _EXPANDABLE_COUPON_RE.search(raw)
    is_expandable = bool(m_exp_b)
    expand_max_yuan = float(m_exp_b.group(2)) if (m_exp_b and m_exp_b.group(2)) else None

    threshold = 0.0
    m_th = _THRESHOLD_RE.search(raw)
    if m_th:
        threshold = round(float(m_th.group(1)), 2)
    elif "无门槛" in raw:
        threshold = 0.0

    # 提取券面金额：先屏蔽「满XX」「还差XX」「有效期」数字，再按优先级匹配金额
    cleaned_for_amt = _GAP_YUAN_RE.sub(" ", raw)
    cleaned_for_amt = _THRESHOLD_RE.sub(" ", cleaned_for_amt)
    amount: Optional[float] = None
    for pat in _CARD_AMOUNT_PATS:
        m_a = pat.search(cleaned_for_amt)
        if m_a:
            val = float(m_a.group(1))
            if 0.0 < val <= 300.0:
                amount = round(val, 2)
                break
    # 兼容首行直接裸数字（如 "30\n满76可用\n外卖大额神券"）
    if amount is None:
        lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
        for ln in lines[:4]:
            if re.fullmatch(r"\d+(?:\.\d{1,2})?", ln):
                val = float(ln)
                if 0.0 < val <= 300.0 and (threshold <= 0 or val < threshold):
                    amount = round(val, 2)
                    break

    if amount is None or amount <= 0:
        return None

    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    title = ""
    for ln in lines:
        if ln in ("¥", "￥", "无门槛", "可用", "去使用", "已选") or re.fullmatch(r"[¥￥]?\d+(?:\.\d{1,2})?", ln):
            continue
        if ln.startswith(("满", "有效期", "限", "本单", "还差")):
            continue
        title = ln[:48]
        break

    cat = classify_coupon_category(title, raw)
    exp_ts = _parse_expiry_ts(raw, now_dt=now_dt)
    label = (
        f"{title or '红包'} 满{threshold:g}减{amount:g}"
        if threshold > 0
        else f"{title or '红包'} 无门槛减{amount:g}"
    )

    return {
        "dom_idx": dom_idx,
        "title": title or "美团红包",
        "label": label,
        "short_label": f"满{threshold:g}减{amount:g}" if threshold > 0 else f"无门槛减{amount:g}",
        "amount_yuan": amount,
        "threshold_yuan": threshold,
        "category": cat,
        "is_universal": cat == "universal",
        "expires_ts": exp_ts,
        "usable": not is_unavail,
        "is_paid_upsell": False,
        "is_unavailable": is_unavail,
        "gap_yuan": gap_yuan,
        "is_expandable": is_expandable,
        "expand_max_yuan": expand_max_yuan,
        "selected": bool(selected),
        "x": float(x),
        "y": float(y),
        "card_x": float(card_x or x),
        "card_y": float(card_y or y),
        "raw_head": raw.replace("\n", " ")[:120],
    }


def select_optimal_coupon_candidate(
    candidates: list[dict[str, Any]],
    *,
    order_subtotal: Optional[float] = None,
    now_dt: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """从结算页/选券页的全部候选券中选出数学最优的一张可用券。

    择优次序：
    1. 严格剔除付费买券包/省钱卡与不可用券；
    2. 本单立减金额 **最大**（核心省钱目标）；
    3. 同等立减下，优先消耗 **门槛更高** 的券（把无门槛/低门槛小券省下来留给小单）；
    4. 同等立减与门槛下，优先消耗 **更早到期** 的券（防过期浪费）；
    5. 同等条件下，优先消耗 **专享/限定券**，保留「吃喝玩乐/全场通用」券。
    """
    usable: list[dict[str, Any]] = []
    for c in candidates or []:
        if not isinstance(c, dict):
            continue
        if not c.get("usable", True) or c.get("is_paid_upsell") or c.get("is_unavailable"):
            continue
        amt = float(c.get("amount_yuan") or c.get("amount") or 0.0)
        if amt <= 0:
            continue
        th = float(c.get("threshold_yuan") if c.get("threshold_yuan") is not None else (c.get("threshold") or 0.0))
        if order_subtotal is not None and order_subtotal > 0 and th > order_subtotal + 0.01:
            continue
        exp_ts = float(c.get("expires_ts") or _parse_expiry_ts(str(c.get("expires_at") or ""), now_dt=now_dt))
        usable.append(
            {
                **c,
                "amount_yuan": round(amt, 2),
                "threshold_yuan": round(th, 2),
                "expires_ts": exp_ts,
            }
        )
    if not usable:
        return None

    usable.sort(
        key=lambda c: (
            -float(c["amount_yuan"]),
            -float(c["threshold_yuan"]),
            float(c["expires_ts"]),
            1 if c.get("is_universal") else 0,
            int(c.get("dom_idx") or 0),
        )
    )
    return usable[0]


def extract_near_threshold_tip_from_cards(
    candidates: list[dict[str, Any]],
    current_discount: float = 0.0,
) -> str:
    """从选券页不可用分区（如「还差¥4可用满52减18」）提取真实可执行的凑单省钱提示。"""
    best_score = 0.0
    best_tip = ""
    for c in candidates or []:
        if not isinstance(c, dict) or c.get("is_paid_upsell") or c.get("usable"):
            continue
        gap = c.get("gap_yuan")
        amt = float(c.get("amount_yuan") or 0.0)
        th = float(c.get("threshold_yuan") or 0.0)
        if gap is None or gap <= 0 or gap > 18.0 or amt <= current_discount:
            continue
        extra_save = round(amt - current_discount, 2)
        if extra_save < max(2.0, float(gap) * 0.5):
            continue
        net_save = round(extra_save - float(gap), 2)
        score = extra_save - float(gap) * 0.45
        if score > best_score:
            best_score = score
            lbl = str(c.get("short_label") or (f"满{th:g}减{amt:g}" if th > 0 else f"减{amt:g}"))
            if net_save > 0:
                best_tip = f"再凑¥{gap:g}可解锁「{lbl}」，多省¥{extra_save:g}（净倒省¥{net_save:g}！）"
            else:
                best_tip = f"差¥{gap:g}可换「{lbl}」（多省¥{extra_save:g}）"
    return best_tip


# ---------------------------------------------------------------------------
# 凑单 / 补货省钱策略规划（纯计算，点饭与补货共用）
# ---------------------------------------------------------------------------


def find_cash_saving_topup(
    *,
    shop_name: str,
    current_subtotal: float,
    delivery_fee: float = 0.0,
    packing_fee: float = 0.0,
    candidate_items: list[dict[str, Any]],
    store_promos: Optional[dict[str, Any]] = None,
    account_coupons: Optional[dict[str, Any]] = None,
    max_total_cap: float = 120.0,
    order_category: str = "food",
    now_dt: Optional[datetime] = None,
) -> dict[str, Any]:
    """测算在当前购物车基础上加购 1 件候选配菜/商品是否能跨入更高档红包并实现「倒省钱」或「高性价比凑单」。

    返回：
    - `auto_topup`: 仅当加购后**券后总实付严格更低**（`new_after < base_after`，即白嫖商品还倒省现金）时非空；
    - `value_topup_tip`: 未达到严格倒省钱但新增立减 ≥ 加购价 55% 时的具体凑单建议（带商品名与实算差价）。
    """
    from siming.meituan_speculate import compute_coupon_pricing

    base_cp = compute_coupon_pricing(
        shop_name=shop_name,
        subtotal=current_subtotal,
        delivery_fee=delivery_fee,
        packing_fee=packing_fee,
        store_promos=store_promos,
        account_coupons=account_coupons,
        order_category=order_category,
        now_dt=now_dt,
    )
    base_after = float(base_cp["after_coupon_total"])
    base_disc = float(base_cp["total_discount"])

    best_auto: Optional[dict[str, Any]] = None
    best_value: Optional[dict[str, Any]] = None

    for it in candidate_items or []:
        if not isinstance(it, dict):
            continue
        price = float(it.get("price") or 0.0)
        name = str(it.get("name") or "").strip()
        if not name or price <= 0.0 or price > 35.0:
            continue
        if it.get("is_sold_out") or it.get("is_utensil") or it.get("is_multi_person") or it.get("is_tiny_promo"):
            continue
        new_sub = round(current_subtotal + price, 2)
        new_cp = compute_coupon_pricing(
            shop_name=shop_name,
            subtotal=new_sub,
            delivery_fee=delivery_fee,
            packing_fee=packing_fee,
            store_promos=store_promos,
            account_coupons=account_coupons,
            order_category=order_category,
            now_dt=now_dt,
        )
        new_after = float(new_cp["after_coupon_total"])
        new_disc = float(new_cp["total_discount"])
        extra_disc = round(new_disc - base_disc, 2)
        if extra_disc <= 0.5 or new_after > max_total_cap:
            continue

        net_cash_saved = round(base_after - new_after, 2)
        # 1. 严格倒省钱（new_after < base_after）：加了这个品之后总付款反而更少！
        if net_cash_saved >= 0.5:
            cand_rec = {
                "item": it,
                "name": name,
                "price": round(price, 2),
                "before_after_total": base_after,
                "new_after_total": new_after,
                "net_cash_saved": net_cash_saved,
                "extra_discount": extra_disc,
                "new_coupon_pricing": new_cp,
                "applied_coupons": list(new_cp.get("applied_coupons") or []),
            }
            if best_auto is None or (
                cand_rec["net_cash_saved"],
                bool(it.get("has_plus")),
                -cand_rec["price"],
            ) > (
                best_auto["net_cash_saved"],
                bool(best_auto["item"].get("has_plus")),
                -best_auto["price"],
            ):
                best_auto = cand_rec
        # 2. 高性价比凑单（新增立减 >= 商品价格 55% 且净多花 <= ¥8）
        elif extra_disc >= max(2.0, price * 0.55) and (new_after - base_after) <= 8.0:
            net_extra_cost = round(new_after - base_after, 2)
            ratio = extra_disc / max(0.1, price)
            cand_val = {
                "item": it,
                "name": name,
                "price": round(price, 2),
                "net_extra_cost": net_extra_cost,
                "extra_discount": extra_disc,
                "new_after_total": new_after,
                "applied_coupons": list(new_cp.get("applied_coupons") or []),
                "score": ratio * 10.0 + extra_disc - net_extra_cost,
            }
            if best_value is None or cand_val["score"] > best_value["score"]:
                best_value = cand_val

    value_tip = ""
    if best_value is not None:
        cp_lbl = " + ".join(best_value["applied_coupons"]) or "大额神券"
        value_tip = (
            f"加「{best_value['name']} ¥{best_value['price']:g}」可解锁 {cp_lbl}"
            f"（多省¥{best_value['extra_discount']:g}，实付仅多花¥{best_value['net_extra_cost']:g}）"
        )

    return {
        "baseline_pricing": base_cp,
        "auto_topup": best_auto,
        "value_topup": best_value,
        "value_topup_tip": value_tip,
    }


def plan_replenish_coupon_strategy(
    db: SimingDB,
    item: dict[str, Any],
    all_items: Optional[list[dict[str, Any]]] = None,
    *,
    qty: Optional[int] = None,
    account_coupons: Optional[dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """为美团日用品补货测算最优省钱红包策略：单买最优券 vs 多件加购凑券 vs 同店临期合单省运费+凑大额券。"""
    from siming.meituan_speculate import compute_coupon_pricing, load_cached_account_coupons

    if str(item.get("channel") or "meituan") != "meituan":
        return {"ok": False, "summary_lines": [], "best_strategy": "none"}

    cur = now or now_local()
    today = cur.date()
    eff_qty = max(1, int(qty if qty is not None else item.get("qty") or 1))
    unit_price = float(item.get("target_yuan") or 0.0)
    shop = str(item.get("meituan_shop") or "").strip()
    loc = str(item.get("location") or "家").strip()
    item_name = str(item.get("name") or "").strip()
    order_cat = infer_order_category(shop, store_kind="subtrade", item_names=[item_name, str(item.get("meituan_query") or "")])

    acc = account_coupons if account_coupons is not None else (load_cached_account_coupons(db, max_age_min=720.0, now_dt=cur) or {})
    est_deliv = 3.0  # 商超/闪购典型配送+打包参考，用于对比合单省运费
    base_sub = round(unit_price * eff_qty, 2)

    base_cp = (
        compute_coupon_pricing(
            shop_name=shop or item_name,
            subtotal=base_sub,
            delivery_fee=est_deliv,
            account_coupons=acc,
            order_category=order_cat,
            now_dt=cur,
        )
        if base_sub > 0
        else {
            "raw_subtotal": 0.0,
            "total_discount": 0.0,
            "after_coupon_total": 0.0,
            "applied_coupons": [],
            "near_coupon_tip": "",
        }
    )

    # 策略 A：消耗品多买 1 件（qty+1）测算是否跨档倒省钱或单价大幅下降
    qty_bump_rec: Optional[dict[str, Any]] = None
    if unit_price > 0 and eff_qty <= 3:
        bump_qty = eff_qty + 1
        bump_sub = round(unit_price * bump_qty, 2)
        bump_cp = compute_coupon_pricing(
            shop_name=shop or item_name,
            subtotal=bump_sub,
            delivery_fee=est_deliv,
            account_coupons=acc,
            order_category=order_cat,
            now_dt=cur,
        )
        base_after = float(base_cp["after_coupon_total"])
        bump_after = float(bump_cp["after_coupon_total"])
        extra_disc = round(float(bump_cp["total_discount"]) - float(base_cp["total_discount"]), 2)
        unit_before = round(base_after / eff_qty, 2)
        unit_after = round(bump_after / bump_qty, 2)
        unit_drop_pct = round((unit_before - unit_after) / max(0.01, unit_before) * 100.0, 1)
        if bump_after < base_after:
            qty_bump_rec = {
                "recommended_qty": bump_qty,
                "strictly_cheaper": True,
                "cash_saved_yuan": round(base_after - bump_after, 2),
                "extra_discount_yuan": extra_disc,
                "after_coupon_total": bump_after,
                "unit_before": unit_before,
                "unit_after": unit_after,
                "unit_drop_pct": unit_drop_pct,
                "applied_coupons": list(bump_cp.get("applied_coupons") or []),
            }
        elif extra_disc >= max(4.0, unit_price * 0.35) and unit_drop_pct >= 12.0:
            qty_bump_rec = {
                "recommended_qty": bump_qty,
                "strictly_cheaper": False,
                "cash_saved_yuan": 0.0,
                "extra_discount_yuan": extra_disc,
                "after_coupon_total": bump_after,
                "unit_before": unit_before,
                "unit_after": unit_after,
                "unit_drop_pct": unit_drop_pct,
                "applied_coupons": list(bump_cp.get("applied_coupons") or []),
            }

    # 策略 B：同地点同门店（或同为超市渠道）临期/将到期日用品合单（省 1 笔运费 + 叠加凑大额券）
    bundle_rec: Optional[dict[str, Any]] = None
    if all_items and unit_price > 0:
        from datetime import date

        shop_key = re.sub(r"[\s（()）·•]+", "", shop).lower()
        peers: list[tuple[int, dict[str, Any]]] = []
        for other in all_items:
            if other.get("item_id") == item.get("item_id"):
                continue
            if not other.get("enabled", True) or str(other.get("channel") or "") != "meituan":
                continue
            if str(other.get("location") or "家") != loc:
                continue
            o_price = float(other.get("target_yuan") or 0.0)
            if o_price <= 0:
                continue
            o_shop = str(other.get("meituan_shop") or "").strip()
            o_shop_key = re.sub(r"[\s（()）·•]+", "", o_shop).lower()
            same_shop = bool(shop_key and o_shop_key and (shop_key in o_shop_key or o_shop_key in shop_key))
            if not same_shop:
                continue
            try:
                due_d = date.fromisoformat(str(other.get("next_due") or ""))
                days_left = (due_d - today).days
            except ValueError:
                days_left = 999
            # 到期或 30 天内将到期，或待校准的同店消耗品都可作为合单候选
            if days_left <= 30 or bool(other.get("needs_calibration")):
                peers.append((days_left, other))

        peers.sort(key=lambda x: x[0])
        best_bundle_gain = 0.0
        for d_left, peer in peers[:4]:
            p_qty = max(1, int(peer.get("qty") or 1))
            p_sub = round(float(peer.get("target_yuan") or 0.0) * p_qty, 2)
            comb_sub = round(base_sub + p_sub, 2)
            comb_cp = compute_coupon_pricing(
                shop_name=shop or item_name,
                subtotal=comb_sub,
                delivery_fee=est_deliv,
                account_coupons=acc,
                order_category=order_cat,
                now_dt=cur,
            )
            peer_cp = compute_coupon_pricing(
                shop_name=shop or str(peer.get("name") or ""),
                subtotal=p_sub,
                delivery_fee=est_deliv,
                account_coupons=acc,
                order_category=order_cat,
                now_dt=cur,
            )
            separate_total = round(float(base_cp["after_coupon_total"]) + float(peer_cp["after_coupon_total"]), 2)
            combined_total = float(comb_cp["after_coupon_total"])
            total_saved_vs_sep = round(separate_total - combined_total, 2)
            extra_coupon_vs_single = round(float(comb_cp["total_discount"]) - float(base_cp["total_discount"]), 2)
            if total_saved_vs_sep >= 3.0 and (total_saved_vs_sep > best_bundle_gain):
                best_bundle_gain = total_saved_vs_sep
                bundle_rec = {
                    "peer_item_id": str(peer["item_id"]),
                    "peer_name": str(peer["name"]),
                    "peer_qty": p_qty,
                    "peer_days_left": d_left,
                    "peer_subtotal": p_sub,
                    "combined_subtotal": comb_sub,
                    "combined_after_total": combined_total,
                    "saved_vs_separate": total_saved_vs_sep,
                    "extra_coupon_yuan": extra_coupon_vs_single,
                    "applied_coupons": list(comb_cp.get("applied_coupons") or []),
                }

    summary_lines: list[str] = []
    if float(base_cp.get("total_discount") or 0.0) > 0:
        app_s = " + ".join(str(x) for x in (base_cp.get("applied_coupons") or []) if x)
        summary_lines.append(
            f"当前单可用「{app_s}」，预计立省 **¥{float(base_cp['total_discount']):g}**（券后约 ¥{float(base_cp['after_coupon_total']):.1f}）"
        )
    if bundle_rec is not None:
        b_cp = " + ".join(bundle_rec["applied_coupons"]) or "大额满减"
        d_tag = "已到期" if bundle_rec["peer_days_left"] <= 0 else f"余{bundle_rec['peer_days_left']}天"
        summary_lines.append(
            f"💡 **同店合单更省**：与同店「{bundle_rec['peer_name']}」（{d_tag}）合单满 ¥{bundle_rec['combined_subtotal']:g}，"
            f"可用「{b_cp}」+ 省 1 笔运费，比分买共省 **¥{bundle_rec['saved_vs_separate']:g}**"
        )
    if qty_bump_rec is not None:
        q_cp = " + ".join(qty_bump_rec["applied_coupons"]) or "更高档神券"
        if qty_bump_rec["strictly_cheaper"]:
            summary_lines.append(
                f"🔥 **加购×{qty_bump_rec['recommended_qty']}倒省钱**：触发「{q_cp}」，买 {qty_bump_rec['recommended_qty']} 件总实付反而比买 {eff_qty} 件还少 **¥{qty_bump_rec['cash_saved_yuan']:g}**（发 `补货 {item_name} x{qty_bump_rec['recommended_qty']}`）"
            )
        else:
            summary_lines.append(
                f"💡 **囤货降单价**：买 ×{qty_bump_rec['recommended_qty']} 可解锁「{q_cp}」（多省¥{qty_bump_rec['extra_discount_yuan']:g}，单件均价 ¥{qty_bump_rec['unit_before']:g}→¥{qty_bump_rec['unit_after']:g}，降 {qty_bump_rec['unit_drop_pct']:g}%；发 `补货 {item_name} x{qty_bump_rec['recommended_qty']}`）"
            )
    elif base_cp.get("near_coupon_tip") and bundle_rec is None:
        summary_lines.append(f"💡 凑单提示：{base_cp['near_coupon_tip']}")

    best_strategy = "direct"
    if qty_bump_rec and qty_bump_rec["strictly_cheaper"]:
        best_strategy = "qty_bump_cheaper"
    elif bundle_rec is not None:
        best_strategy = "bundle_same_shop"
    elif qty_bump_rec is not None:
        best_strategy = "qty_bump_unit_save"

    return {
        "ok": True,
        "order_category": order_cat,
        "best_strategy": best_strategy,
        "base_pricing": base_cp,
        "qty_bump": qty_bump_rec,
        "bundle": bundle_rec,
        "summary_lines": summary_lines,
    }


def optimize_multi_leg_coupons(
    legs: list[dict[str, Any]],
    account_coupons: Optional[dict[str, Any]] = None,
    *,
    now_dt: Optional[datetime] = None,
) -> list[dict[str, Any]]:
    """跨店双拼/多单点餐的全局最优 1 对 1 分配账户红包（避免同一张单次神券被重复计算，全局最大化总省钱额）。"""
    if not legs:
        return []
    acc = account_coupons or {}
    all_plats: list[dict[str, Any]] = []
    seen_sig: set[tuple[str, float, float, str]] = set()
    for c in list(acc.get("platform_coupons") or []) + list(acc.get("flash_coupons") or []):
        if not isinstance(c, dict):
            continue
        sig = (
            str(c.get("title") or ""),
            float(c.get("amount") or 0.0),
            float(c.get("threshold") or 0.0),
            str(c.get("expires_at") or ""),
        )
        # 注意：同面额多张相同券若来自不同列表去重，但同列表重复张允许保留
        if sig in seen_sig and c in (acc.get("flash_coupons") or []):
            continue
        seen_sig.add(sig)
        all_plats.append(c)

    if not all_plats:
        return [
            {
                "leg_index": idx,
                "coupon_yuan": float(l.get("coupon_yuan") or 0.0),
                "coupon_label": f"默认满减/神券 -¥{float(l.get('coupon_yuan') or 0.0):g}",
            }
            for idx, l in enumerate(legs)
        ]

    n_legs = len(legs)
    # 为每一单筛选出 Top 6 可用候选券（含 None 表示本单不用平台券）
    per_leg_cands: list[list[Optional[int]]] = []
    for leg in legs:
        price = float(leg.get("item_price") or 0.0)
        cat = infer_order_category(str(leg.get("shop_name") or ""), item_names=[str(leg.get("item_name") or "")])
        elig: list[tuple[float, float, int]] = []
        for ci, c in enumerate(all_plats):
            c_cat = str(c.get("category") or classify_coupon_category(str(c.get("title") or ""), str(c.get("rule") or "")))
            if not is_coupon_applicable_to_category(c_cat, cat):
                continue
            if not is_coupon_applicable_to_shop(c, str(leg.get("shop_name") or "")):
                continue
            th = float(c.get("threshold") or 0.0)
            amt = float(c.get("amount") or 0.0)
            if amt > 0 and price >= th:
                elig.append((min(price, amt), th, ci))
        elig.sort(key=lambda x: (-x[0], -x[1]))
        per_leg_cands.append([x[2] for x in elig[:6]] + [None])

    best_total = -1.0
    best_th_sum = -1.0
    best_assign: tuple[Optional[int], ...] = tuple(None for _ in range(n_legs))

    for combo in itertools.product(*per_leg_cands):
        used = [x for x in combo if x is not None]
        if len(used) != len(set(used)):
            continue
        tot = 0.0
        th_sum = 0.0
        for li, ci in enumerate(combo):
            if ci is not None:
                c = all_plats[ci]
                tot += min(float(legs[li].get("item_price") or 0.0), float(c.get("amount") or 0.0))
                th_sum += float(c.get("threshold") or 0.0)
        if tot > best_total or (abs(tot - best_total) < 1e-6 and th_sum > best_th_sum):
            best_total = tot
            best_th_sum = th_sum
            best_assign = combo

    out: list[dict[str, Any]] = []
    for li, ci in enumerate(best_assign):
        if ci is not None:
            c = all_plats[ci]
            amt = round(min(float(legs[li].get("item_price") or 0.0), float(c.get("amount") or 0.0)), 2)
            out.append(
                {
                    "leg_index": li,
                    "coupon_yuan": amt,
                    "coupon_label": str(c.get("label") or c.get("short_label") or f"神券 -¥{amt:g}"),
                }
            )
        else:
            out.append({"leg_index": li, "coupon_yuan": 0.0, "coupon_label": "暂无可用平台神券"})
    return out


# ---------------------------------------------------------------------------
# 结算预览页实时 CDP 最优三券择优（美团红包 + 商家代金券 + 配送券，双架构全兼容）
# ---------------------------------------------------------------------------


def _wait(sec: float) -> None:
    time.sleep(max(0.0, sec))


def _ensure_active(p_ws: LocalWsCdpClient) -> None:
    try:
        p_ws.call("Page.setWebLifecycleState", {"state": "active"})
    except Exception:
        pass


def _setup_mobile(p_ws: LocalWsCdpClient, loc_target: Optional[dict[str, Any]] = None) -> None:
    for method, params in (
        ("Page.enable", {}),
        ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
        ("Page.setWebLifecycleState", {"state": "active"}),
        ("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True}),
    ):
        try:
            p_ws.call(method, params)
        except Exception:
            pass
    if loc_target:
        try:
            from siming.meituan_agent import set_cdp_geolocation_for_target

            set_cdp_geolocation_for_target(p_ws, loc_target)
        except Exception:
            pass


def _eval_val(p_ws: LocalWsCdpClient, expr: str) -> Any:
    res = p_ws.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    if res.get("exceptionDetails"):
        return None
    return (res.get("result") or {}).get("value")


def _eval_obj(p_ws: LocalWsCdpClient, expr: str) -> dict[str, Any]:
    val = _eval_val(p_ws, expr)
    if isinstance(val, str) and val.startswith("{"):
        try:
            obj = json.loads(val)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def _page_url(p_ws: LocalWsCdpClient) -> str:
    return str(_eval_val(p_ws, "location.href") or "")


def _body_text(p_ws: LocalWsCdpClient) -> str:
    return str(_eval_val(p_ws, "(document.body && document.body.innerText) || ''") or "")


def _click_xy(p_ws: LocalWsCdpClient, x: float, y: float) -> None:
    p_ws.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": float(x), "y": float(y)})
    p_ws.call("Input.dispatchMouseEvent", {"type": "mousePressed", "x": float(x), "y": float(y), "button": "left", "clickCount": 1})
    p_ws.call("Input.dispatchMouseEvent", {"type": "mouseReleased", "x": float(x), "y": float(y), "button": "left", "clickCount": 1})


def _touch_tap_xy(p_ws: LocalWsCdpClient, x: float, y: float) -> None:
    try:
        p_ws.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
    except Exception:
        pass
    pt = [{"x": round(float(x), 1), "y": round(float(y), 1), "radiusX": 6, "radiusY": 6, "force": 1.0, "id": 1}]
    p_ws.call("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": pt})
    _wait(0.05)
    p_ws.call("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def _is_preview_url(url: str) -> bool:
    u = (url or "").lower()
    return any(p in u for p in ("mindex/preview", "package/preview/preview", "/preview/preview"))


def _parse_bill_for_url(url: str, text: str) -> dict[str, Any]:
    if "package/preview" in (url or "").lower():
        from siming.meituan_cart_live import parse_cart_preview_bill

        return parse_cart_preview_bill(text)
    from siming.meituan_pay import parse_meituan_preview_bill

    return parse_meituan_preview_bill(text)


def _find_preview_row_pos(p_ws: LocalWsCdpClient, row_kind: str) -> dict[str, Any]:
    """精准定位结算预览页中的「美团红包 / 商家代金券 / 配送券」单行入口（严防误点外层整块容器导致点偏）。"""
    return _eval_obj(
        p_ws,
        r"""(() => {
            const kind = KIND;
            const patterns = {
                redpacket: {
                    labels: ['美团红包', '神券红包', '美团神券', '美团红包/神券'],
                    statusHints: ['未选红包', '最高', '张可用', '可用', '-¥', '-￥', '已减'],
                },
                store_voucher: {
                    labels: ['商家代金券', '店铺代金券', '商家券'],
                    statusHints: ['张可用', '可用', '未选', '-¥', '-￥'],
                },
                delivery_coupon: {
                    labels: ['配送券', '配送费券', '减配送费券'],
                    statusHints: ['张可用', '可用', '未选', '-¥', '-￥'],
                },
            }[kind] || {labels: ['美团红包'], statusHints: ['可用']};

            const isBlocked = t => /暂无可用|无可用|不可用/.test(t) && !/-\s*[¥￥]\s*\d/.test(t) && !/\d+\s*张可用/.test(t) && !/最高\s*\d/.test(t);

            // 1) 兼容全部自定义组件标签（mt-view/webc-view/wm-view/div 等），精准定位目标优惠单行或状态叶子
            const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'META', 'LINK', 'HEAD', 'TITLE', 'HTML', 'BODY', 'NOSCRIPT']);
            const cands = [];
            for (const el of document.querySelectorAll('*')) {
                if (SKIP_TAGS.has(el.tagName)) continue;
                const r = el.getBoundingClientRect();
                if (r.width < 36 || r.height < 14 || r.height > 96 || r.width > 420) continue;
                const t = (el.innerText || '').trim();
                if (!t || t.length > 90) continue;
                const hasLabel = patterns.labels.some(lb => t.includes(lb));
                const hasStatus = patterns.statusHints.some(sh => t.includes(sh));
                if (hasLabel && !isBlocked(t)) {
                    const area = r.width * r.height;
                    cands.push({el, score: (hasStatus ? 100 : 60) - area / 1200.0, text: t, r});
                } else if (kind === 'redpacket' && t.includes('未选红包') && t.includes('可用') && el.children.length <= 2) {
                    const area = r.width * r.height;
                    cands.push({el, score: 135 - area / 1200.0, text: t, r});
                }
            }
            if (!cands.length) return JSON.stringify({ok: false, reason: 'row_not_found'});
            cands.sort((a, b) => b.score - a.score);
            const best = cands[0].el;
            try { best.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
            const r = best.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return JSON.stringify({ok: false, reason: 'zero_size'});
            // 点击行偏右位置（箭头/状态区）避免点到左侧「?」说明图标！
            const clickX = r.width >= 200 ? (r.left + r.width * 0.78) : (r.left + r.width / 2);
            const clickY = r.top + r.height / 2;
            return JSON.stringify({
                ok: true,
                x: clickX,
                y: clickY,
                text: (best.innerText || '').trim().slice(0, 80),
            });
        })()""".replace("KIND", json.dumps(row_kind)),
    )


def _scan_mindex_redpacket_cards(p_ws: LocalWsCdpClient) -> dict[str, Any]:
    """扫描 `machpro/red_packet`（以及商家代金券/配送券独立页）全部可见可用与不可用券卡片。"""
    return _eval_obj(
        p_ws,
        r"""(() => {
            const unavail = Array.from(document.querySelectorAll('*')).find(el => {
                const t = (el.innerText || '').trim();
                const r = el.getBoundingClientRect();
                return r.height > 8 && r.height < 60 && (
                    t.startsWith('不可使用红包') || t.startsWith('不可用红包')
                    || t.startsWith('不可用优惠券') || t.startsWith('不可使用代金券')
                    || t.startsWith('本单不可用原因') || t.startsWith('以下红包不可用')
                );
            });
            const limitY = unavail ? unavail.getBoundingClientRect().top : 99999;

            let cells = Array.from(document.querySelectorAll(
                'webc-view.cell-wrapper, [class*="cell-wrapper"], [class*="coupon-item"], [class*="packet-item"], [class*="voucher-item"]'
            )).filter(c => {
                const r = c.getBoundingClientRect();
                return r.width >= 200 && r.height >= 45 && r.height <= 260;
            });
            // 去重：过滤掉包含子卡片的外层容器
            cells = cells.filter(c => !cells.some(o => o !== c && c.contains(o)));

            if (!cells.length) {
                const fallback = Array.from(document.querySelectorAll('webc-view, div')).filter(c => {
                    const r = c.getBoundingClientRect();
                    const t = (c.innerText || '').trim();
                    return r.width >= 240 && r.height >= 55 && r.height <= 210 && r.top >= 30
                        && (t.includes('¥') || t.includes('￥') || /\d+\s*元/.test(t))
                        && (t.includes('满') || t.includes('无门槛') || t.includes('神券') || t.includes('红包') || t.includes('代金券'));
                });
                cells = fallback.filter(c => !fallback.some(o => o !== c && c.contains(o)));
            }

            const cards = cells.map((cell, domIdx) => {
                const cr = cell.getBoundingClientRect();
                const aboveDivider = cr.top < limitY - 8;
                const imgs = Array.from(cell.querySelectorAll('webc-image, img, [class*="radio"], [class*="check"], [class*="select"]')).filter(e => {
                    const r = e.getBoundingClientRect();
                    return r.left >= cr.left + cr.width * 0.72 && r.width >= 14 && r.width <= 44 && r.height >= 14 && r.height <= 44;
                });
                const tgt = imgs[0] || cell.querySelector('webc-view.coupon-wrapper') || cell;
                const tr = tgt.getBoundingClientRect();
                const clsAll = (String(cell.className || '') + ' ' + String((tgt && tgt.className) || '')).toLowerCase();
                const selected = /selected|checked|active/.test(clsAll) || (cell.innerText || '').includes('已选');
                return {
                    domIdx,
                    aboveDivider,
                    top: Math.round(cr.top),
                    text: (cell.innerText || '').trim().slice(0, 260),
                    x: tr.left + tr.width / 2,
                    y: tr.top + tr.height / 2,
                    cardX: cr.left + cr.width * 0.5,
                    cardY: cr.top + cr.height * 0.5,
                    selected,
                };
            });

            const bottomBar = (
                document.querySelector('webc-view.packet-confirm-bar')?.innerText
                || document.querySelector('[class*="confirm"]')?.parentElement?.innerText
                || ''
            ).trim().slice(0, 120);

            return JSON.stringify({
                ok: cards.length > 0,
                limitY,
                bottomBar,
                cards,
            });
        })()""",
    )


def _scan_subtrade_drawer_cards(p_ws: LocalWsCdpClient) -> dict[str, Any]:
    """扫描 `package/preview/preview` 商超/医药优惠券抽屉内全部可用与不可用券卡片（兼容半角 ¥ 与全角 ￥，并检测抽屉动画是否落稳）。"""
    return _eval_obj(
        p_ws,
        r"""(() => {
            const vh = window.innerHeight || 844;
            const lb = document.querySelector('.loading-ui-loading-backdrop');
            const lbVisible = Boolean(lb && getComputedStyle(lb).display !== 'none' && lb.getBoundingClientRect().height > 0);
            const unavails = Array.from(document.querySelectorAll('*')).filter(el => {
                const t = (el.innerText || '').trim();
                const r = el.getBoundingClientRect();
                return r.height > 8 && r.height < 60 && r.width > 40 && (
                    t.startsWith('不可用优惠券') || t.startsWith('不可用红包')
                    || t.startsWith('不可使用红包') || t.startsWith('本单不可用原因')
                );
            });
            const unavail = unavails.length ? unavails[unavails.length - 1] : null;
            const limitY = unavail ? unavail.getBoundingClientRect().top : (vh + 300);
            const availHdrs = Array.from(document.querySelectorAll('*')).filter(el => {
                const t = (el.innerText || '').trim();
                const r = el.getBoundingClientRect();
                return r.height > 8 && r.height < 60 && r.width > 40 && r.top >= 20 && r.top < vh - 60 && (
                    t.startsWith('可用优惠券') || t.startsWith('可用红包')
                    || t.startsWith('本单可用') || t.startsWith('选择美团红包') || t.startsWith('选择红包')
                );
            });
            const availHdr = availHdrs.length ? availHdrs[availHdrs.length - 1] : null;
            const minY = availHdr ? availHdr.getBoundingClientRect().bottom : 110;

            const SKIP_TAGS = new Set(['SCRIPT', 'STYLE', 'META', 'LINK', 'HEAD', 'TITLE', 'HTML', 'BODY', 'NOSCRIPT']);
            let rawCards = Array.from(document.querySelectorAll('*')).filter(el => {
                if (SKIP_TAGS.has(el.tagName)) return false;
                const r = el.getBoundingClientRect();
                const t = (el.innerText || '').trim();
                return r.top >= minY - 12 && r.bottom <= vh + 260
                    && r.width >= 230 && r.height >= 44 && r.height <= 220
                    && (t.includes('¥') || t.includes('￥') || /\d+\s*元/.test(t))
                    && (t.includes('满') || t.includes('无门槛') || t.includes('红包') || t.includes('神券') || t.includes('代金券') || t.includes('配送') || t.includes('有效期') || t.includes('限'));
            });
            // 剔除包含子卡片的外层容器，避免误把整个列表当成第 1 张券！
            rawCards = rawCards.filter(c => !rawCards.some(o => o !== c && c.contains(o)));
            rawCards.sort((a, b) => a.getBoundingClientRect().top - b.getBoundingClientRect().top);

            const cards = rawCards.map((c, domIdx) => {
                const r = c.getBoundingClientRect();
                const aboveDivider = r.top < limitY - 6;
                const inViewport = r.top >= 20 && r.top < vh - 36 && r.bottom <= vh + 10;
                const checkEl = c.querySelector('[class*="check"], [class*="radio"], [class*="select"], img, mt-image, webc-image');
                const cr = checkEl ? checkEl.getBoundingClientRect() : null;
                const useCheck = Boolean(cr && cr.left >= r.left + r.width * 0.68 && cr.width >= 12 && cr.width <= 48);
                const x = useCheck ? (cr.left + cr.width / 2) : (r.left + r.width - 26);
                const y = useCheck ? (cr.top + cr.height / 2) : (r.top + r.height / 2);
                const clsAll = String(c.className || '').toLowerCase();
                const selected = /selected|checked|active/.test(clsAll) || (c.innerText || '').includes('已选');
                return {
                    domIdx,
                    aboveDivider,
                    inViewport,
                    top: Math.round(r.top),
                    text: (c.innerText || '').trim().slice(0, 260),
                    x,
                    y,
                    cardX: r.left + r.width * 0.5,
                    cardY: r.top + r.height * 0.5,
                    selected,
                };
            });

            const settled = Boolean(!lbVisible && cards.length > 0 && cards.some(c => c.inViewport));
            return JSON.stringify({
                ok: cards.length > 0,
                settled,
                lbVisible,
                minY,
                limitY,
                cards,
            });
        })()""",
    )


def _find_confirm_btn_pos(p_ws: LocalWsCdpClient) -> dict[str, Any]:
    return _eval_obj(
        p_ws,
        r"""(() => {
            const el = document.querySelector('webc-view.packet-confirm-btn')
                || document.querySelector('webc-view.packet-confirm-rightpart')
                || Array.from(document.querySelectorAll('webc-view, mt-view, button, div, span')).filter(e => {
                    const t = (e.innerText || '').trim();
                    const r = e.getBoundingClientRect();
                    return /^(?:确认|确定|确认使用|使用红包|选好了)$/.test(t) && r.top > 560 && r.width >= 40 && r.height >= 22;
                }).pop();
            if (!el) return JSON.stringify({ok: false});
            const r = el.getBoundingClientRect();
            return JSON.stringify({ok: r.width > 0 && r.height > 0, x: r.left + r.width / 2, y: r.top + r.height / 2, text: (el.innerText || '').trim()});
        })()""",
    )


def _extract_selected_discount_in_bar(text: str) -> Optional[float]:
    for pat in (
        r"(?:已选\d+张[^\d]{0,8})?(?:共减|可减|已减|已优惠|立减)\s*[¥￥]?\s*(\d+(?:\.\d{1,2})?)",
        r"-\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)",
    ):
        m = re.search(pat, text or "")
        if m:
            return round(float(m.group(1)), 2)
    return None


def _click_card_and_verify_mindex(
    p_ws: LocalWsCdpClient,
    best_card: dict[str, Any],
) -> bool:
    """在 `machpro/red_packet` 点击目标最优券并核验底部栏/选中态，支持 mouse -> touch -> DOM click 三级兜底。"""
    dom_idx = int(best_card["dom_idx"])
    want_amt = float(best_card["amount_yuan"])

    def _measure_target() -> dict[str, Any]:
        return _eval_obj(
            p_ws,
            r"""(() => {
                let cells = Array.from(document.querySelectorAll(
                    'webc-view.cell-wrapper, [class*="cell-wrapper"], [class*="coupon-item"], [class*="packet-item"], [class*="voucher-item"]'
                )).filter(c => {
                    const r = c.getBoundingClientRect();
                    return r.width >= 200 && r.height >= 45 && r.height <= 260;
                });
                cells = cells.filter(c => !cells.some(o => o !== c && c.contains(o)));
                const cell = cells[IDX];
                if (!cell) return JSON.stringify({ok: false});
                try { cell.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
                const cr = cell.getBoundingClientRect();
                const imgs = Array.from(cell.querySelectorAll('webc-image, img, [class*="radio"], [class*="check"], [class*="select"]')).filter(e => {
                    const r = e.getBoundingClientRect();
                    return r.left >= cr.left + cr.width * 0.72 && r.width >= 14 && r.width <= 44 && r.height >= 14 && r.height <= 44;
                });
                const tgt = imgs[0] || cell.querySelector('webc-view.coupon-wrapper') || cell;
                const tr = tgt.getBoundingClientRect();
                return JSON.stringify({
                    ok: tr.width > 0 && tr.height > 0,
                    x: tr.left + tr.width / 2,
                    y: tr.top + tr.height / 2,
                    cardX: cr.left + cr.width * 0.5,
                    cardY: cr.top + cr.height * 0.5,
                });
            })()""".replace("IDX", str(dom_idx)),
        )

    def _is_selected_now() -> bool:
        if _is_preview_url(_page_url(p_ws)):
            return True
        txt = _body_text(p_ws)
        tail = txt[-360:]
        bar_disc = _extract_selected_discount_in_bar(tail)
        if bar_disc is not None and abs(bar_disc - want_amt) <= 0.15:
            return True
        return False

    # 若最优券本来就已被勾选，无需再点（避免点一下反而取消勾选！）
    if best_card.get("selected") and _is_selected_now():
        return True

    pos = _measure_target()
    x = float(pos.get("x") or best_card["x"])
    y = float(pos.get("y") or best_card["y"])
    cx = float(pos.get("cardX") or best_card["card_x"] or x)
    cy = float(pos.get("cardY") or best_card["card_y"] or y)

    # 1) Trusted mouse click on radio
    _click_xy(p_ws, x, y)
    _wait(0.65)
    if _is_selected_now():
        return True

    # 2) Touch tap on radio (MachPro webc-view often listens only to touch!)
    _touch_tap_xy(p_ws, x, y)
    _wait(0.65)
    if _is_selected_now():
        return True

    # 3) DOM click on radio / card + touch tap on card center
    _eval_val(
        p_ws,
        r"""(() => {
            let cells = Array.from(document.querySelectorAll(
                'webc-view.cell-wrapper, [class*="cell-wrapper"], [class*="coupon-item"], [class*="packet-item"], [class*="voucher-item"]'
            )).filter(c => {
                const r = c.getBoundingClientRect();
                return r.width >= 200 && r.height >= 45 && r.height <= 260;
            });
            cells = cells.filter(c => !cells.some(o => o !== c && c.contains(o)));
            const cell = cells[IDX];
            if (!cell) return false;
            const cr = cell.getBoundingClientRect();
            const img = Array.from(cell.querySelectorAll('webc-image, img, [class*="radio"], [class*="check"]')).find(e => e.getBoundingClientRect().left >= cr.left + cr.width * 0.72);
            if (img) img.click();
            else cell.click();
            return true;
        })()""".replace("IDX", str(dom_idx)),
    )
    _wait(0.55)
    if _is_selected_now():
        return True

    if abs(cx - x) > 12 or abs(cy - y) > 12:
        _touch_tap_xy(p_ws, cx, cy)
        _wait(0.55)
    return _is_selected_now()


def _optimize_single_row(
    p_ws: LocalWsCdpClient,
    *,
    row_kind: str,
    is_subtrade: bool,
    loc_target: Optional[dict[str, Any]],
    saved_preview_url: str,
) -> dict[str, Any]:
    """打开并择优勾选单一优惠行（redpacket / store_voucher / delivery_coupon），保底回到预览页。"""
    before_url = _page_url(p_ws) or saved_preview_url
    before_txt = _body_text(p_ws)
    before_bill = _parse_bill_for_url(before_url, before_txt)
    before_total = before_bill.get("final_payable_yuan")

    row_pos = _find_preview_row_pos(p_ws, row_kind)
    if not row_pos.get("ok"):
        return {
            "ok": False,
            "applied": False,
            "reason": "row_not_found",
            "before_payable": before_total,
            "after_payable": before_total,
        }

    # 点击目标优惠行打开选券页或抽屉
    _click_xy(p_ws, float(row_pos["x"]), float(row_pos["y"]))

    opened_subpage = False
    opened_drawer = False
    wait_dl = time.monotonic() + (3.6 if is_subtrade else 4.5)
    retried_open = False
    while time.monotonic() < wait_dl:
        _wait(0.45)
        _ensure_active(p_ws)
        cur_u = _page_url(p_ws)
        if any(k in cur_u.lower() for k in ("red_packet", "coupon", "voucher", "poicoupon")):
            opened_subpage = True
            break
        cur_t = _body_text(p_ws)
        if any(
            k in cur_t
            for k in ("可用优惠券", "可用红包", "不可用优惠券", "不可使用红包", "选择美团红包", "不使用红包", "不使用代金券", "确认使用")
        ):
            opened_drawer = True
            break
        if not retried_open and time.monotonic() > wait_dl - 2.0:
            retried_open = True
            _touch_tap_xy(p_ws, float(row_pos["x"]), float(row_pos["y"]))

    if not opened_subpage and not opened_drawer:
        return {
            "ok": False,
            "applied": False,
            "reason": "coupon_picker_not_opened",
            "before_payable": before_total,
            "after_payable": before_total,
        }

    # 扫描全部候选券卡片（支持 MachPro 独立页与商超抽屉双模式；商超抽屉等待滑入动画与 loading 遮罩结束！）
    scan_res: dict[str, Any] = {"ok": False, "cards": []}
    scan_dl = time.monotonic() + (4.8 if opened_drawer else 3.8)
    while time.monotonic() < scan_dl:
        _ensure_active(p_ws)
        scan_res = _scan_mindex_redpacket_cards(p_ws) if opened_subpage else _scan_subtrade_drawer_cards(p_ws)
        if scan_res.get("ok"):
            if opened_subpage or scan_res.get("settled", True):
                break
        _wait(0.45)

    parsed_cards: list[dict[str, Any]] = []
    for raw_c in scan_res.get("cards") or []:
        info = parse_coupon_card_info(
            str(raw_c.get("text") or ""),
            dom_idx=int(raw_c.get("domIdx") or 0),
            x=float(raw_c.get("x") or 0.0),
            y=float(raw_c.get("y") or 0.0),
            card_x=float(raw_c.get("cardX") or 0.0),
            card_y=float(raw_c.get("cardY") or 0.0),
            selected=bool(raw_c.get("selected")),
        )
        if info is None:
            continue
        if not raw_c.get("aboveDivider", True):
            info["usable"] = False
            info["is_unavailable"] = True
        parsed_cards.append(info)

    best_card = select_optimal_coupon_candidate(parsed_cards)
    near_tip = extract_near_threshold_tip_from_cards(
        parsed_cards,
        current_discount=float(best_card["amount_yuan"]) if best_card else 0.0,
    )
    expandable_hint = ""
    if best_card and best_card.get("is_expandable"):
        emax = best_card.get("expand_max_yuan")
        expandable_hint = (
            f"当前「{best_card['short_label']}」支持膨胀（最高膨至 ¥{emax:g}），可在美团 App 膨胀后再付更省！"
            if emax
            else f"当前「{best_card['short_label']}」支持免费膨胀，可在美团 App 膨胀后再付更省！"
        )

    if best_card is not None:
        if opened_subpage:
            _click_card_and_verify_mindex(p_ws, best_card)
            if not _is_preview_url(_page_url(p_ws)):
                cf = _find_confirm_btn_pos(p_ws)
                if cf.get("ok"):
                    _click_xy(p_ws, float(cf["x"]), float(cf["y"]))
                    for attempt_cf in range(3):
                        w_ret = time.monotonic() + (1.2 if attempt_cf < 2 else 2.0)
                        while time.monotonic() < w_ret:
                            _wait(0.35)
                            if _is_preview_url(_page_url(p_ws)):
                                break
                        if _is_preview_url(_page_url(p_ws)):
                            break
                        if attempt_cf == 0:
                            _eval_val(
                                p_ws,
                                "(() => { const el = document.querySelector('webc-view.packet-confirm-btn') || document.querySelector('webc-view.packet-confirm-rightpart'); if (el) { el.click(); return true; } return false; })()",
                            )
                        elif attempt_cf == 1:
                            _touch_tap_xy(p_ws, float(cf["x"]), float(cf["y"]))
        else:
            # 商超抽屉：确认坐标在视口内后点击最优券勾选框，并支持 mouse -> confirm -> touch tap -> 卡片中心 四级兜底！
            def _drawer_still_open() -> bool:
                t_now = _body_text(p_ws)
                return any(k in t_now for k in ("可用优惠券", "可用红包", "选择美团红包", "不使用红包"))

            if not best_card.get("selected"):
                bx, by = float(best_card["x"]), float(best_card["y"])
                cx, cy = float(best_card.get("card_x") or bx), float(best_card.get("card_y") or by)
                # 1) trusted mouse click on checkbox/radio
                if 10.0 <= by <= 834.0:
                    _click_xy(p_ws, bx, by)
                    _wait(1.0)
                if _drawer_still_open():
                    cf = _find_confirm_btn_pos(p_ws)
                    if cf.get("ok"):
                        _click_xy(p_ws, float(cf["x"]), float(cf["y"]))
                        _wait(1.2)
                # 2) 若抽屉仍未关闭或价格未变化，补 touch tap 在 radio 与卡片中心
                if _drawer_still_open():
                    if 10.0 <= by <= 834.0:
                        _touch_tap_xy(p_ws, bx, by)
                        _wait(0.9)
                    if _drawer_still_open() and 10.0 <= cy <= 834.0 and (abs(cx - bx) > 10 or abs(cy - by) > 10):
                        _click_xy(p_ws, cx, cy)
                        _wait(0.9)
                    if _drawer_still_open():
                        cf2 = _find_confirm_btn_pos(p_ws)
                        if cf2.get("ok"):
                            _click_xy(p_ws, float(cf2["x"]), float(cf2["y"]))
                            _wait(1.1)
                            if _drawer_still_open():
                                _touch_tap_xy(p_ws, float(cf2["x"]), float(cf2["y"]))
                                _wait(1.0)
            else:
                cf = _find_confirm_btn_pos(p_ws)
                if cf.get("ok"):
                    _click_xy(p_ws, float(cf["x"]), float(cf["y"]))
                    _wait(1.0)

    # 保底防滞留：若仍在子页则回退/直航回预览页；若商超抽屉仍未关闭再点顶部空白收起
    if opened_subpage and not _is_preview_url(_page_url(p_ws)):
        _eval_val(p_ws, "history.back()")
        for _ in range(8):
            _wait(0.4)
            if _is_preview_url(_page_url(p_ws)):
                break
        if not _is_preview_url(_page_url(p_ws)):
            p_ws.call("Page.navigate", {"url": saved_preview_url})
            _wait(3.6)
            _setup_mobile(p_ws, loc_target)
    elif opened_drawer:
        cur_t2 = _body_text(p_ws)
        if any(k in cur_t2 for k in ("可用优惠券", "可用红包", "选择美团红包")):
            cf2 = _find_confirm_btn_pos(p_ws)
            if cf2.get("ok"):
                _click_xy(p_ws, float(cf2["x"]), float(cf2["y"]))
                _wait(1.0)
            else:
                _click_xy(p_ws, 195.0, 80.0)
                _wait(0.8)

    _ensure_active(p_ws)
    _wait(0.6)
    after_url = _page_url(p_ws) or saved_preview_url
    after_txt = _body_text(p_ws)
    after_bill = _parse_bill_for_url(after_url, after_txt)
    after_total = after_bill.get("final_payable_yuan")

    saved = 0.0
    if before_total is not None and after_total is not None:
        saved = round(float(before_total) - float(after_total), 2)

    return {
        "ok": True,
        "applied": saved > 0.005,
        "saved_yuan": saved,
        "before_payable": before_total,
        "after_payable": after_total,
        "chosen": best_card,
        "cards_count": len(parsed_cards),
        "usable_count": sum(1 for c in parsed_cards if c.get("usable")),
        "picker_mode": "subpage" if opened_subpage else "drawer",
        "near_threshold_tip": near_tip,
        "expandable_hint": expandable_hint,
        "after_bill": after_bill,
    }


def _row_needs_optimization(row_text: str, *, check_suboptimal: bool = False) -> bool:
    t = (row_text or "").strip()
    if not t:
        return False
    if any(k in t for k in ("暂无可用", "无可用", "不可用")) and "张可用" not in t and "最高" not in t:
        return False
    if "未选" in t and "可用" in t:
        return True
    if re.search(r"\d+\s*张可用", t) or ("最高" in t and "可用" in t):
        return True
    if check_suboptimal and re.search(r"-\s*[¥￥]\s*\d", t) and "已选最大优惠" not in t:
        return True
    return False


def apply_optimal_preview_coupons(
    p_ws: LocalWsCdpClient,
    *,
    store_kind: str = "",
    loc_target: Optional[dict[str, Any]] = None,
    check_suboptimal_selected: bool = True,
) -> dict[str, Any]:
    """结算预览页统一最优三券择优（美团红包 + 商家代金券 + 配送券），双架构全兼容且防互斥涨价回滚。"""
    saved_preview_url = _page_url(p_ws)
    is_subtrade = (store_kind == "subtrade") or ("package/preview" in saved_preview_url.lower())
    before_txt = _body_text(p_ws)
    before_bill = _parse_bill_for_url(saved_preview_url, before_txt)
    before_total = before_bill.get("final_payable_yuan")

    result: dict[str, Any] = {
        "ok": True,
        "applied": False,
        "before_payable_yuan": before_total,
        "after_payable_yuan": before_total,
        "saved_yuan": 0.0,
        "red_packet_chosen": None,
        "store_voucher_chosen": None,
        "delivery_coupon_chosen": None,
        "near_threshold_tip": "",
        "expandable_hint": "",
        "warning": "",
        "debug": {},
    }

    try:
        # 1) 美团红包（含未选红包 / N张可用 / 已选但未标「已选最大优惠」时复核是否有更大额神券）
        rp_txt = str(before_bill.get("coupon_text") or "")
        need_rp = _row_needs_optimization(rp_txt, check_suboptimal=check_suboptimal_selected) or (
            "未选红包" in before_txt and "可用" in before_txt
        )
        if need_rp:
            rp_res = _optimize_single_row(
                p_ws,
                row_kind="redpacket",
                is_subtrade=is_subtrade,
                loc_target=loc_target,
                saved_preview_url=saved_preview_url,
            )
            result["debug"]["rp_first"] = {
                "ok": rp_res.get("ok"),
                "reason": rp_res.get("reason", ""),
                "picker_mode": rp_res.get("picker_mode", ""),
                "cards_count": rp_res.get("cards_count", 0),
                "usable_count": rp_res.get("usable_count", 0),
                "saved_yuan": rp_res.get("saved_yuan", 0.0),
            }
            if rp_res.get("chosen"):
                result["red_packet_chosen"] = rp_res["chosen"]
            if rp_res.get("near_threshold_tip"):
                result["near_threshold_tip"] = str(rp_res["near_threshold_tip"])
            if rp_res.get("expandable_hint"):
                result["expandable_hint"] = str(rp_res["expandable_hint"])
            # 若仍有「未选红包，最高X元可用」残留，自动重试一次（抵御首轮动画/懒加载未就绪）
            cur_bill_1 = rp_res.get("after_bill") or _parse_bill_for_url(_page_url(p_ws) or saved_preview_url, _body_text(p_ws))
            if "未选红包" in str(cur_bill_1.get("coupon_text") or "") and "可用" in str(cur_bill_1.get("coupon_text") or ""):
                _wait(0.8)
                rp_retry = _optimize_single_row(
                    p_ws,
                    row_kind="redpacket",
                    is_subtrade=is_subtrade,
                    loc_target=loc_target,
                    saved_preview_url=saved_preview_url,
                )
                result["debug"]["rp_retry"] = {
                    "ok": rp_retry.get("ok"),
                    "reason": rp_retry.get("reason", ""),
                    "picker_mode": rp_retry.get("picker_mode", ""),
                    "cards_count": rp_retry.get("cards_count", 0),
                    "usable_count": rp_retry.get("usable_count", 0),
                    "saved_yuan": rp_retry.get("saved_yuan", 0.0),
                }
                if rp_retry.get("chosen"):
                    result["red_packet_chosen"] = rp_retry["chosen"]
                if rp_retry.get("near_threshold_tip") and not result["near_threshold_tip"]:
                    result["near_threshold_tip"] = str(rp_retry["near_threshold_tip"])
                if rp_retry.get("expandable_hint") and not result["expandable_hint"]:
                    result["expandable_hint"] = str(rp_retry["expandable_hint"])

        # 2) 商家代金券（如页面显示「1张可用」，尝试叠加；若与平台大额神券互斥导致总价上升，立即回滚重选美团红包！）
        mid_txt = _body_text(p_ws)
        mid_bill = _parse_bill_for_url(_page_url(p_ws) or saved_preview_url, mid_txt)
        mid_total = mid_bill.get("final_payable_yuan")
        sv_txt = str(mid_bill.get("store_voucher_text") or "")
        if _row_needs_optimization(sv_txt, check_suboptimal=False):
            sv_res = _optimize_single_row(
                p_ws,
                row_kind="store_voucher",
                is_subtrade=is_subtrade,
                loc_target=loc_target,
                saved_preview_url=saved_preview_url,
            )
            sv_after = sv_res.get("after_payable")
            if mid_total is not None and sv_after is not None and float(sv_after) > float(mid_total) + 0.01:
                # 互斥涨价守卫：商家券顶掉了更大的平台红包 -> 立即重选平台红包恢复更低实付！
                _optimize_single_row(
                    p_ws,
                    row_kind="redpacket",
                    is_subtrade=is_subtrade,
                    loc_target=loc_target,
                    saved_preview_url=saved_preview_url,
                )
            elif sv_res.get("applied") and sv_res.get("chosen"):
                result["store_voucher_chosen"] = sv_res["chosen"]

        # 3) 配送券（如页面显示「1张可用」，尝试叠加减配送费）
        mid2_txt = _body_text(p_ws)
        mid2_bill = _parse_bill_for_url(_page_url(p_ws) or saved_preview_url, mid2_txt)
        mid2_total = mid2_bill.get("final_payable_yuan")
        dv_txt = str(mid2_bill.get("delivery_coupon_text") or "")
        if _row_needs_optimization(dv_txt, check_suboptimal=False):
            dv_res = _optimize_single_row(
                p_ws,
                row_kind="delivery_coupon",
                is_subtrade=is_subtrade,
                loc_target=loc_target,
                saved_preview_url=saved_preview_url,
            )
            if dv_res.get("applied") and dv_res.get("chosen"):
                result["delivery_coupon_chosen"] = dv_res["chosen"]
            elif mid2_total is not None and dv_res.get("after_payable") is not None and float(dv_res["after_payable"]) > float(mid2_total) + 0.01:
                _optimize_single_row(
                    p_ws,
                    row_kind="redpacket",
                    is_subtrade=is_subtrade,
                    loc_target=loc_target,
                    saved_preview_url=saved_preview_url,
                )

        # 最终复核账单与防漏券告警
        final_txt = _body_text(p_ws)
        final_bill = _parse_bill_for_url(_page_url(p_ws) or saved_preview_url, final_txt)
        final_total = final_bill.get("final_payable_yuan")
        result["after_payable_yuan"] = final_total
        if before_total is not None and final_total is not None:
            saved_total = round(float(before_total) - float(final_total), 2)
            result["saved_yuan"] = max(0.0, saved_total)
            result["applied"] = saved_total > 0.005

        final_rp_txt = str(final_bill.get("coupon_text") or "")
        if "未选红包" in final_rp_txt and "可用" in final_rp_txt:
            result["warning"] = f"结算页仍显示「{final_rp_txt}」未能自动勾选，付款前建议在美团 App 核对勾选以免多花钱！"
        return result
    except Exception as exc:
        if saved_preview_url and not _is_preview_url(_page_url(p_ws)):
            try:
                p_ws.call("Page.navigate", {"url": saved_preview_url})
                _wait(3.2)
                _setup_mobile(p_ws, loc_target)
            except Exception:
                pass
        result["ok"] = False
        result["error"] = str(exc)[:180]
        return result
