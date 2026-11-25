"""美团外卖 H5 真实加购 + 结算预览（双架构全兼容 trusted input 版）。

同时完整支持美团外卖两套独立 H5 门店/结算体系：
1. 餐饮外卖体系（绝大多数正餐/粉面/快餐/咖啡门店）：
   - 门店页：`h5.waimai.meituan.com/waimai/mindex/menu?poi_id_str=...`
   - 店内搜索：`h5.waimai.meituan.com/waimai/mindex/menusearch?...`
   - 菜品卡片：`[class*="spu_"]`（仅取可见节点防 SPA 隐藏路由干扰），控件 `.sqt-menu-add-buttons [class*="plus_"]` / `[class*="minus_"]` / `[class*="count_"]`
   - 购物车存储：`localStorage.cached_cart_data[poi_id_str].cartData[*].bills`
   - 结算预览：`h5.waimai.meituan.com/waimai/mindex/preview?placeholder=1&redirectfrom=1`
   - 预览选址：`mindex/addresslist`（默认排除「外卖柜」，点击左侧区域避开右侧「删除」）
   - 预览选券：`machpro/red_packet`（勾选首张可用券圆标 + 点击底部 `packet-confirm-btn`，保底自动回退预览页绝不滞留）
2. 商超/水果/医药闪购体系（如 示例果切·果切、百康药房等）：
   - 门店页：`cactivityapi-sc.waimai.meituan.com/h5/sub-trade/restaurant/restaurant?...`
   - 店内搜索：`cactivityapi-sc.waimai.meituan.com/h5/package/shop-inner-search/shop-inner-search?...`
   - 菜品卡片：`[class*="food-card-wrap"]` / `[class*="food-item-search"]`，控件 `.index-flashstepper-add_box` / `.index-flashstepper-but-box`
   - 购物车存储：`localStorage.localCartData[poi_id_str]` + `localStorage.previewData`
   - 结算预览：`cactivityapi-sc.waimai.meituan.com/h5/package/preview/preview?...operateType=cart-submit`

硬约束（实测固化，严禁回退）：
- 强制后台标签（background=True，零焦点抢占）+ 每个 CDP 连接重设 `setFocusEmulationEnabled` + `390x844` 移动视口 + `Page.setWebLifecycleState('active')` + `Emulation.setGeolocationOverride`。
- 全部交互采用 CDP trusted mouse input（`Input.dispatchMouseEvent`）。
- 本模块严格停在结算预览页（核验菜品名、数量、收货地址、实付合计四要素齐全），绝不点击「提交订单」或任何支付控件。
"""

from __future__ import annotations

import json
import math
import re
import time
import urllib.parse
from typing import Any, Optional

from siming.cdp import LocalWsCdpClient, detect_compliance_gate, resolve_chromium_cdp_base
from siming.db import SimingDB
from siming.meituan_agent import (
    MEITUAN_H5_SEARCH_TMPL,
    _match_best_addstore_entry,
    _strip_branch_display,
    fetch_live_meituan_candidates,
    resolve_meituan_delivery_target,
    set_cdp_geolocation_for_target,
    sync_meituan_h5_location,
)

RESTAURANT_MARKER = "sub-trade/restaurant"
MINDEX_MENU_MARKER = "mindex/menu"
MINDEX_PREVIEW_URL = "https://h5.waimai.meituan.com/waimai/mindex/preview?placeholder=1&redirectfrom=1"
PREVIEW_URL_TMPL = (
    "https://cactivityapi-sc.waimai.meituan.com/h5/package/preview/preview"
    "?hash_id=undefined&pageType=0&poi_id=-100&poi_id_str={poi_id_str}"
    "&from=undefined&act_page_code=&buzType=0&is_di_tui="
    "&drugPreviewExtend={{}}&operateType=cart-submit"
)
ADD_SEL = ".index-flashstepper-add_box"
MINUS_SEL = ".index-flashstepper-but-box"
COUNT_SEL = ".index-flashstepper-count"
CARD_SEL = '[class*="food-card-wrap"]'

INNER_SEARCH_URL = (
    "https://cactivityapi-sc.waimai.meituan.com/h5/package/shop-inner-search/shop-inner-search"
    "?poi_id=-100&poi_id_str={poi_id_str}&source_BU=&buz_type={buz_type}"
)
SEARCH_CARD_SEL = '[class*="food-item-search"]'

_CART_BADGES = {
    "美团快送",
    "美团专送",
    "商家自配",
    "美团跑腿",
    "企鹅跑腿",
    "准时宝",
    "放心吃",
    "支持自取",
    "神券商家",
    "品牌",
    "拼好饭",
    "美团配送",
    "到店自取",
}
_UNIT_RE = re.compile(r"^单价[:：]\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)")
_QTY_RE = re.compile(r"^数量[:：]\s*(\d{1,3})")
_YUAN_RE = re.compile(r"[¥￥]\s*(\d+(?:\.\d{1,2})?)")
_CLOSED_STORE_BANNER_RE = re.compile(r"(本店休息中|商家已休息|门店休息中|商家已打烊|本店已打烊|暂不接单)")


class CartStageError(RuntimeError):
    """带阶段的加购失败，便于上层如实回报失败原因。"""

    def __init__(self, stage: str, message: str, **extra: Any) -> None:
        super().__init__(message)
        self.stage = stage
        self.extra = extra


def _norm(s: str) -> str:
    return re.sub(r"[\s\-—()（）·•,，。:：/+＋|｜]+", "", (s or "").lower())


def _core_norm(s: str) -> str:
    cleaned = re.sub(r"【[^】]*】|\[[^\]]*\]", "", s or "")
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", cleaned.lower())


_SPEC_COUNT_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*(只|片|支|颗|粒|条|贴|抽|g|kg|克|千克|ml|l|毫升)",
    re.IGNORECASE,
)


def _extract_spec_pack(s: str) -> Optional[tuple[float, str]]:
    """提取商品名中的显式规格量（如 50只 / 10片 / 500g），用于同款不同规格排序优先。"""
    m = _SPEC_COUNT_RE.search(s or "")
    if not m:
        return None
    val = float(m.group(1))
    unit = m.group(2).lower()
    if unit in ("只", "片", "支", "颗", "粒", "条", "贴", "抽"):
        return val, "cnt"
    if unit in ("kg", "千克"):
        return val * 1000.0, "g"
    if unit in ("g", "克"):
        return val, "g"
    if unit == "l":
        return val * 1000.0, "ml"
    return val, "ml"


_PACK_BOTTLE_RE = re.compile(r"(?:[*×xX]\s*|\b)(\d{1,3})\s*(?:瓶|听|罐|盒|包|卷|袋|提|桶)", re.IGNORECASE)


def _extract_pack_count(s: str) -> Optional[int]:
    m = _PACK_BOTTLE_RE.search(s or "")
    if m:
        val = int(m.group(1))
        if val >= 2:
            return val
    if any(k in (s or "") for k in ("整箱", "整提", "整盒")):
        return 12
    return None


def _apply_spec_pack_bias(base_score: float, candidate_title: str, want_dish: str) -> float:
    if base_score < 55.0:
        return base_score
    score = base_score
    wp = _extract_spec_pack(want_dish)
    cp = _extract_spec_pack(candidate_title)
    if wp and cp and wp[1] == cp[1]:
        w_val, c_val = wp[0], cp[0]
        if w_val > 0 and c_val > 0:
            if abs(w_val - c_val) / max(w_val, c_val) <= 0.05:
                score = min(100.0, score + 10.0)
            else:
                # 显式指定容量/重量（如 1.5L vs 2L / 550ml）不匹配时施加强惩罚，严防买错规格！
                ratio = max(w_val, c_val) / min(w_val, c_val)
                if wp[1] in ("ml", "g"):
                    penalty = min(24.0, 14.0 + math.log(ratio) * 14.0)
                    score = max(0.0, score - penalty)
                else:
                    penalty = min(8.0, math.log(ratio) * 4.0)
                    score = max(62.0, score - penalty) if score >= 68.0 else max(0.0, score - penalty)

    # 整箱/多瓶装偏好：明确要 12瓶/6瓶/8瓶 时优先匹配多瓶装，避免误命中单瓶装
    w_pack = _extract_pack_count(want_dish)
    c_pack = _extract_pack_count(candidate_title)
    if w_pack is not None:
        if c_pack is not None:
            if c_pack == w_pack:
                score = min(100.0, score + 4.0)
            else:
                score = min(100.0, score + 1.5)
        elif re.search(r"(?:1瓶|/瓶|单瓶)", candidate_title):
            score = max(0.0, score - 12.0)
    return score


_COLLOQUIAL_DISH_FILLER_RE = re.compile(
    r"(?:之前那个|上次那个|以前那个|那个|那道|那份|那碗|那杯|这个|这道|这份|的)"
)
_COLLOQUIAL_CAT_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("凉菜", ("凉菜", "冷菜", "冷味", "烧卤", "凉拌", "冷盘")),
    ("冷菜", ("凉菜", "冷菜", "冷味", "烧卤", "凉拌", "冷盘")),
    ("冷盘", ("凉菜", "冷菜", "冷味", "烧卤", "凉拌", "冷盘")),
    ("主食", ("主食", "米饭", "面", "粥")),
    ("小吃", ("小吃", "小食", "点心")),
    ("点心", ("小吃", "小食", "点心")),
    ("饮品", ("饮品", "饮料", "茶", "酒水")),
    ("饮料", ("饮品", "饮料", "茶", "酒水")),
)
_COLLOQUIAL_SYNONYMS: tuple[tuple[str, ...], ...] = (
    ("芫荽", "香菜", "芫"),
    ("西红柿", "番茄"),
    ("土豆", "马铃薯", "洋芋"),
    ("羊肚", "百叶", "毛肚"),
    ("鸡爪", "凤爪"),
)
_COMMON_CULINARY_STOP_CHARS = frozenset(
    "菜肉饭面汤粉大小辣香鲜甜酸炒拌蒸煮烧烤炸煎炖烩老新招牌精选特色秘制家常风味口感升级标准人份个的了凉冷热小素荤配味汁油盐糖醋葱姜蒜椒"
)


def _strip_colloquial_dish_fillers(want_dish: str) -> tuple[str, tuple[str, ...]]:
    """剥离口语菜品指代词并提取品类提示（如「芫荽那个凉菜」-> `('芫荽', ('凉菜','冷菜','冷味','烧卤',...))`）。"""
    raw = (want_dish or "").strip()
    if not raw:
        return "", ()
    cat_hints: tuple[str, ...] = ()
    for suffix, hints in _COLLOQUIAL_CAT_HINTS:
        if suffix in raw:
            cat_hints = hints
            break
    cleaned = _COLLOQUIAL_DISH_FILLER_RE.sub("", raw).strip()
    for suffix, _hints in _COLLOQUIAL_CAT_HINTS:
        if cleaned.endswith(suffix) and len(cleaned) - len(suffix) >= 2:
            cleaned = cleaned[: -len(suffix)].strip()
            break
    if cleaned.endswith("菜") and len(cleaned) >= 3 and not cleaned.endswith(("白菜", "青菜", "生菜", "油菜", "菠菜", "芹菜", "香菜", "泡菜", "榨菜", "梅菜", "酸菜", "雪菜", "紫菜", "海带菜", "冒菜", "川菜")):
        cleaned = cleaned[:-1].strip()
    return cleaned or raw, cat_hints


def _dish_match_score(
    candidate_title: str,
    want_dish: str,
    *,
    card_lines: Optional[list[str]] = None,
    category_name: str = "",
) -> float:
    """统一模糊匹配菜品名（使「滑蛋牛肉」命中「滑蛋炒牛肉」、「芫荽那个凉菜」命中「御膳芫炝羊肚」，同时拦截低相关误配）。"""
    tf = _norm(candidate_title)
    wf = _norm(want_dish)
    if not tf or not wf:
        return 0.0
    if tf == wf:
        return 100.0
    tc = _core_norm(candidate_title) or tf
    wc = _core_norm(want_dish) or wf
    if tc == wc:
        return 98.0

    def _title_base(target_tc: str, target_tf: str, q_wc: str, q_wf: str) -> float:
        if not q_wc:
            return 0.0
        b = 0.0
        if q_wf in target_tf or (len(q_wc) >= 2 and q_wc in target_tc):
            b = 90.0 - min(12.0, abs(len(target_tc) - len(q_wc)) * 0.5)
        elif len(target_tc) >= 2 and target_tc in q_wc:
            b = 84.0 - min(12.0, abs(len(target_tc) - len(q_wc)) * 0.5)
        else:
            # 保序子序列匹配（如 滑蛋牛肉 -> 滑蛋炒牛肉、麒麟西瓜500g -> 鲜果切麒麟西瓜500g）
            if len(q_wc) >= 3:
                pos = 0
                ok_subseq = True
                for ch in q_wc:
                    idx = target_tc.find(ch, pos)
                    if idx < 0:
                        ok_subseq = False
                        break
                    pos = idx + 1
                if ok_subseq:
                    b = 82.0 - min(15.0, max(0, len(target_tc) - len(q_wc)) * 1.4)

            # 高字符覆盖 + 至少 1 个双字词命中
            if b <= 0.0 and len(q_wc) >= 3:
                hits = sum(1 for ch in q_wc if ch in target_tc)
                ratio = hits / float(len(q_wc))
                bigrams = [q_wc[i : i + 2] for i in range(len(q_wc) - 1)]
                bg_hits = sum(1 for bg in bigrams if bg in target_tc)
                if ratio >= 0.75 and bg_hits >= 1:
                    b = 64.0 + ratio * 10.0
        return b

    base = _title_base(tc, tf, wc, wf)

    cleaned_q, cat_hints = _strip_colloquial_dish_fillers(want_dish)
    if base < 62.0 and cleaned_q and cleaned_q != want_dish:
        cq_f = _norm(cleaned_q)
        cq_c = _core_norm(cleaned_q) or cq_f
        base = max(base, _title_base(tc, tf, cq_c, cq_f))

    # 口语食材/描述兜底（如「芫荽那个凉菜」命中卡片介绍含「芫荽」或「烧卤冷味」分类下的「御膳芫炝羊肚」）
    if base < 62.0 and cleaned_q:
        cq_c = _core_norm(cleaned_q) or _norm(cleaned_q)
        syn_terms: list[str] = [cq_c]
        for grp in _COLLOQUIAL_SYNONYMS:
            if any(t in cq_c for t in grp):
                for t in grp:
                    if t not in syn_terms:
                        syn_terms.append(t)
        desc_text = _norm(" ".join(str(x) for x in (card_lines or [])[1:]))
        cat_match = bool(cat_hints and any(h in (category_name or "") or h in desc_text for h in cat_hints))

        # 1) 同义词命中标题（如 香菜 <-> 芫荽）
        for syn in syn_terms[1:]:
            if len(syn) >= 2 and syn in tc:
                base = max(base, 80.0 + (4.0 if cat_match else 0.0))

        # 2) 核心食材词/同义词命中卡片介绍文本（card.lines）
        if base < 62.0 and desc_text:
            for term in syn_terms:
                if len(term) >= 2 and term in desc_text:
                    base = max(base, 72.0 + (6.0 if cat_match else 0.0))
                    break

        # 3) 稀有食材特征字命中标题 + 品类或介绍佐证（如 芫荽 -> 御膳芫炝羊肚）
        if base < 62.0:
            rare_chars = [
                ch for ch in cq_c
                if "\u4e00" <= ch <= "\u9fff" and ch not in _COMMON_CULINARY_STOP_CHARS
            ]
            for syn in syn_terms[1:]:
                if len(syn) == 1 and "\u4e00" <= syn <= "\u9fff" and syn not in _COMMON_CULINARY_STOP_CHARS and syn not in rare_chars:
                    rare_chars.append(syn)
            matched_rare = [ch for ch in rare_chars if ch in tc]
            if matched_rare and (cat_match or any(len(t) >= 2 and t in desc_text for t in syn_terms)):
                base = max(base, 74.0 if cat_match else 68.0)

    return _apply_spec_pack_bias(base, candidate_title, want_dish)


def _is_mindex_menu_url(url: str) -> bool:
    u = (url or "").lower()
    return (MINDEX_MENU_MARKER in u) and ("mindex/menusearch" not in u)


def _is_restaurant_url(url: str) -> bool:
    u = url or ""
    return (RESTAURANT_MARKER in u) or _is_mindex_menu_url(u)


def _store_kind_from_url(url: str) -> str:
    return "mindex" if _is_mindex_menu_url(url) else "subtrade"


def _setup_page(p_ws: LocalWsCdpClient, loc_target: Optional[dict[str, Any]] = None) -> None:
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
        set_cdp_geolocation_for_target(p_ws, loc_target)


def _eval_value(p_ws: LocalWsCdpClient, expr: str) -> Any:
    res = p_ws.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    if res.get("exceptionDetails"):
        return None
    return (res.get("result") or {}).get("value")


def _eval_obj(p_ws: LocalWsCdpClient, expr: str) -> dict[str, Any]:
    val = _eval_value(p_ws, expr)
    if isinstance(val, str) and val.startswith("{"):
        try:
            obj = json.loads(val)
            return obj if isinstance(obj, dict) else {}
        except Exception:
            return {}
    return {}


def _body_text(p_ws: LocalWsCdpClient) -> str:
    val = _eval_value(p_ws, "(document.body && document.body.innerText) || ''")
    return str(val or "")


def _page_url(p_ws: LocalWsCdpClient) -> str:
    return str(_eval_value(p_ws, "location.href") or "")


def _click_xy(p_ws: LocalWsCdpClient, x: float, y: float) -> None:
    p_ws.call("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": float(x), "y": float(y)})
    p_ws.call(
        "Input.dispatchMouseEvent",
        {"type": "mousePressed", "x": float(x), "y": float(y), "button": "left", "clickCount": 1},
    )
    p_ws.call(
        "Input.dispatchMouseEvent",
        {"type": "mouseReleased", "x": float(x), "y": float(y), "button": "left", "clickCount": 1},
    )


_CLICK_JS = r"""
(() => {
  const sel = SEL;
  const idx = IDX;
  const nodes = Array.from(document.querySelectorAll(sel)).filter(n => {
    const r = n.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  if (!nodes.length) return JSON.stringify({ok:false, reason:'not_found', n:0});
  const el = nodes[Math.min(idx, nodes.length - 1)];
  try { el.scrollIntoView({block:'center', inline:'center'}); } catch (e) {}
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return JSON.stringify({ok:false, reason:'zero_size', n:nodes.length});
  const x = r.x + r.width / 2;
  const y = r.y + r.height / 2;
  if (x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) {
    return JSON.stringify({ok:false, reason:'out_of_viewport', n:nodes.length, x, y, w:window.innerWidth, h:window.innerHeight});
  }
  return JSON.stringify({ok:true, n:nodes.length, x, y, text:(el.innerText||'').trim().slice(0,100), cls:String(el.className||'').slice(0,120)});
})()
"""


def _click_selector(p_ws: LocalWsCdpClient, selector: str, index: int = 0) -> dict[str, Any]:
    expr = _CLICK_JS.replace("SEL", json.dumps(selector, ensure_ascii=False)).replace("IDX", str(index))
    info = _eval_obj(p_ws, expr)
    if info.get("ok"):
        _click_xy(p_ws, float(info["x"]), float(info["y"]))
    return info


_TEXT_CLICK_JS = r"""
(() => {
  const want = WANT;
  const norm = s => (s || '').replace(/[\s\-—()（）·•,，。:：/]+/g, '').toLowerCase();
  const wn = norm(want);
  const cands = [];
  for (const el of document.querySelectorAll('mt-view, mt-text, webc-view, webc-text, div, span, p, button, a')) {
    const t = (el.innerText || '').trim();
    if (!t || t.length > 40) continue;
    const r = el.getBoundingClientRect();
    if (r.width <= 0 || r.height <= 0) continue;
    if (norm(t) === wn) cands.push({el, score: 2});
    else if (norm(t).includes(wn)) cands.push({el, score: 1});
  }
  if (!cands.length) return JSON.stringify({ok:false, reason:'not_found'});
  const depth = el => { let d=0,c=el; while(c&&c.parentElement){d++;c=c.parentElement;} return d; };
  cands.sort((a,b) => (b.score - a.score) || (depth(b.el) - depth(a.el)));
  const el = cands[0].el;
  try { el.scrollIntoView({block:'center', inline:'center'}); } catch(e) {}
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return JSON.stringify({ok:false, reason:'zero_size'});
  return JSON.stringify({ok:true, x:r.x+r.width/2, y:r.y+r.height/2, text:(el.innerText||'').trim().slice(0,80), cls:String(el.className||'').slice(0,120)});
})()
"""


def _click_text(p_ws: LocalWsCdpClient, text: str) -> dict[str, Any]:
    info = _eval_obj(p_ws, _TEXT_CLICK_JS.replace("WANT", json.dumps(text, ensure_ascii=False)))
    if info.get("ok"):
        _click_xy(p_ws, float(info["x"]), float(info["y"]))
    return info


def _wait(seconds: float) -> None:
    time.sleep(max(0.0, seconds))


def _ensure_active(p_ws: LocalWsCdpClient) -> None:
    try:
        p_ws.call("Page.setWebLifecycleState", {"state": "active"})
    except Exception:
        pass


def _check_gate(p_ws: LocalWsCdpClient, stage: str) -> None:
    url = _page_url(p_ws)
    text = _body_text(p_ws)[:2000]
    title = str(_eval_value(p_ws, "document.title || ''") or "")
    gate = detect_compliance_gate(url, title, text)
    if gate is not None:
        raise CartStageError(stage, f"触发合规门禁：{gate.get('reason') or '需登录或滑块验证'}", gate=gate, gated=True)


# ---------------------------------------------------------------------------
# 账单解析：适配 cactivityapi H5 package/preview 的 innerText 版式
# ---------------------------------------------------------------------------


def parse_cart_preview_bill(text: str) -> dict[str, Any]:
    """解析店铺页直达的 `package/preview` 提交页文本（与 mindex/preview 版式不同）。"""
    lines = [ln.strip() for ln in (text or "").replace("\r", "").splitlines() if ln.strip()]
    address = ""
    shop = ""
    eta_text = ""
    coupon_text = ""
    store_voucher_text = ""
    delivery_coupon_text = ""
    store_manjian_yuan: Optional[float] = None
    discount_yuan: Optional[float] = None
    packing_fee: Optional[float] = None
    delivery_fee: Optional[float] = None
    final_payable: Optional[float] = None
    items: list[dict[str, Any]] = []

    first_item_name_idx: Optional[int] = None

    for i, ln in enumerate(lines):
        if ln in ("外卖配送", "到店自取") and i + 1 < len(lines):
            cand = lines[i + 1]
            if cand in ("外卖配送", "到店自取", "选择收货地址", "配送方式已变更，请重新核对信息"):
                continue
            address = cand
            break
    if not address:
        for i, ln in enumerate(lines[:14]):
            if ln == "选择收货地址" and i >= 1 and lines[i - 1] not in ("外卖配送", "到店自取"):
                address = ""
                break

    for i, ln in enumerate(lines):
        if ("大约" in ln and "送达" in ln) or ln.startswith("预计"):
            eta_text = ln
            break

    for i, ln in enumerate(lines):
        m = _UNIT_RE.match(ln)
        if not m:
            continue
        unit = float(m.group(1))
        name = ""
        spec_parts: list[str] = []
        j = i - 1
        while j >= 0:
            cand = lines[j]
            if cand in _CART_BADGES or cand in ("选择收货地址",) or _YUAN_RE.fullmatch(cand):
                j -= 1
                continue
            if re.match(r"^(规格|已选|口味|加料)[:：]", cand):
                spec_parts.insert(0, cand)
                j -= 1
                continue
            name = cand
            break
        qty = 1
        for k in range(i, min(i + 8, len(lines))):
            mq = _QTY_RE.match(lines[k])
            if mq:
                qty = int(mq.group(1))
                break
            mx = re.match(r"^[xX×]\s*(\d{1,3})$", lines[k])
            if mx:
                qty = int(mx.group(1))
                break
        items.append(
            {
                "name": name,
                "price_yuan": unit,
                "spec": " ".join(spec_parts).strip(),
                "qty": qty,
                "line_total_yuan": round(unit * qty, 2),
            }
        )
        if first_item_name_idx is None and name:
            first_item_name_idx = j

    # 兼容医药/闪购 `package/preview` 另一版式（无「单价:¥X」，采用 `商品名 / [规格] / x数量 / ￥实付小计 / [￥原价]`）
    if not items:
        for i, ln in enumerate(lines):
            mx = re.fullmatch(r"[xX×]\s*(\d{1,3})", ln)
            if not mx:
                continue
            qty = int(mx.group(1))
            # 价格必须紧跟在 `xN` 下一行（区分于餐饮版式的「品名 / ¥价 / xN」，绝不跨行错抓下一道菜价格）
            line_total: Optional[float] = None
            if i + 1 < len(lines):
                cand_p = lines[i + 1]
                mp = _YUAN_RE.fullmatch(cand_p)
                if mp:
                    line_total = float(mp.group(1))
                elif cand_p in ("¥", "￥") and i + 2 < len(lines) and re.fullmatch(r"\d+(?:\.\d{1,2})?", lines[i + 2]):
                    line_total = float(lines[i + 2])
            if line_total is None:
                continue
            name = ""
            spec_parts = []
            j = i - 1
            while j >= 0:
                cand = lines[j]
                if cand in _CART_BADGES or cand in ("选择收货地址",) or _YUAN_RE.fullmatch(cand) or re.fullmatch(r"[xX×]\s*\d{1,3}", cand):
                    j -= 1
                    continue
                if not name and re.fullmatch(r"[\d.*×xX]+\s*(?:只|片|支|盒|包|袋|瓶|ml|g|kg|克|毫升)(?:/\S+)?", cand, re.IGNORECASE):
                    spec_parts.insert(0, cand)
                    j -= 1
                    continue
                if not name and re.match(r"^(规格|已选|口味|加料)[:：]", cand):
                    spec_parts.insert(0, cand)
                    j -= 1
                    continue
                name = cand
                break
            if not name:
                continue
            unit = round(line_total / max(1, qty), 2)
            items.append(
                {
                    "name": name,
                    "price_yuan": unit,
                    "spec": " ".join(spec_parts).strip(),
                    "qty": qty,
                    "line_total_yuan": round(line_total, 2),
                }
            )
            if first_item_name_idx is None:
                first_item_name_idx = j

    for i, ln in enumerate(lines):
        if ln == "打包费":
            for k in range(i + 1, min(i + 4, len(lines))):
                ms = _YUAN_RE.findall(lines[k])
                if ms:
                    packing_fee = float(ms[-1])
                    break
            if packing_fee is None and i + 1 < len(lines):
                m2 = re.search(r"打包费\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)", lines[i] + lines[i + 1])
                if m2:
                    packing_fee = float(m2.group(1))
        elif ln == "配送费":
            nums: list[float] = []
            for k in range(i + 1, min(i + 4, len(lines))):
                if lines[k] in ("店铺满减", "美团红包", "商家代金券", "优惠活动", "小计", "合计"):
                    break
                if "免配送费" in lines[k]:
                    nums = [0.0]
                    break
                nums.extend(float(x) for x in _YUAN_RE.findall(lines[k]))
            if nums:
                delivery_fee = nums[-1]
        elif ln in ("美团红包", "神券红包", "美团神券", "美团红包/神券"):
            for k in range(i + 1, min(i + 4, len(lines))):
                cand_c = lines[k].strip()
                if cand_c in ("商家代金券", "配送券", "优惠活动", "优惠规则", "优惠说明", "小计", "合计"):
                    break
                if cand_c in ("神券", "天天神券", "可膨胀"):
                    continue
                coupon_text = cand_c
                md = re.search(r"-\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)", coupon_text)
                if md:
                    discount_yuan = max(discount_yuan or 0.0, float(md.group(1)))
                break
        elif ln in ("商家代金券", "店铺代金券"):
            if i + 1 < len(lines) and lines[i + 1].strip() not in ("配送券", "美团红包", "优惠活动", "优惠规则", "小计", "合计"):
                store_voucher_text = lines[i + 1].strip()
        elif ln in ("配送券", "配送费券"):
            if i + 1 < len(lines) and lines[i + 1].strip() not in ("商家代金券", "美团红包", "优惠活动", "优惠规则", "小计", "合计"):
                delivery_coupon_text = lines[i + 1].strip()
        elif ln == "店铺满减":
            if i + 1 < len(lines):
                mmj = re.search(r"-\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)", lines[i + 1])
                if mmj:
                    store_manjian_yuan = float(mmj.group(1))
        elif "已优惠" in ln:
            ms = _YUAN_RE.findall(ln)
            if ms:
                discount_yuan = max(discount_yuan or 0.0, float(ms[-1]))

    m_total = re.search(r"合计\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)", text or "")
    if m_total:
        final_payable = float(m_total.group(1))
    else:
        m_sub = re.search(r"小计\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)", text or "")
        if m_sub:
            final_payable = float(m_sub.group(1))

    if first_item_name_idx is not None and first_item_name_idx >= 1:
        for j in range(first_item_name_idx - 1, -1, -1):
            cand = lines[j]
            if not cand or cand in _CART_BADGES or cand in ("选择收货地址", "外卖配送", "到店自取") or _YUAN_RE.fullmatch(cand):
                continue
            if cand == address or ("大约" in cand and "送达" in cand):
                continue
            if _QTY_RE.match(cand):
                continue
            shop = cand
            break

    return {
        "delivery_address": address,
        "recipient_masked": "",
        "eta_text": eta_text,
        "shop": shop,
        "items": items,
        "packing_fee_yuan": packing_fee,
        "delivery_fee_yuan": delivery_fee,
        "coupon_text": coupon_text,
        "store_voucher_text": store_voucher_text,
        "delivery_coupon_text": delivery_coupon_text,
        "store_manjian_yuan": store_manjian_yuan,
        "discount_yuan": discount_yuan,
        "final_payable_yuan": final_payable,
        "has_submit_btn": "提交订单" in (text or ""),
    }


# ---------------------------------------------------------------------------
# 通用搜索与进店（自动识别 mindex/menu 与 sub-trade/restaurant）
# ---------------------------------------------------------------------------


def _search_tab(
    db: SimingDB,
    keyword: str,
    loc_target: dict[str, Any],
    existing_target_id: str,
    port: int,
) -> tuple[str, dict[str, Any], str]:
    """确保有一个已按目标定位加载好的搜索标签，返回 (tid, fetch_result, page_ws)。"""
    base = resolve_chromium_cdp_base(port=port)
    if not base or not base.get("ws_browser"):
        raise CartStageError("cdp", "本机主 Chromium (9222) 未就绪")

    tid = existing_target_id.strip()
    sr: dict[str, Any] = {"ok": True, "delivery_poi": ""}
    if tid:
        try:
            probe_ws = base["ws_browser"].split("/devtools/")[0] + f"/devtools/page/{tid}"
            with LocalWsCdpClient(probe_ws, timeout=8.0) as p:
                _setup_page(p, loc_target)
                url = _page_url(p)
            if not url:
                tid = ""
        except Exception:
            tid = ""

    if not tid:
        sr = fetch_live_meituan_candidates(
            db,
            loc_target=loc_target,
            keyword=keyword,
            port=port,
            scroll_rounds=1,
            close_when_clean=False,
        )
        if sr.get("gated"):
            raise CartStageError("search", str((sr.get("gate") or {}).get("reason") or "搜索页触发风控"), gate=sr.get("gate"), gated=True)
        if not sr.get("ok") or not sr.get("target_id"):
            raise CartStageError("search", str(sr.get("error") or sr.get("reason") or "美团搜索页未就绪"))
        tid = str(sr["target_id"])

    page_ws = base["ws_browser"].split("/devtools/")[0] + f"/devtools/page/{tid}"
    return tid, sr, page_ws


_SHOP_CLICK_JS = r"""
(() => {
  const want = WANT;
  const norm = s => (s || '').replace(/[\s\-—()（）·•,，。:：/]+/g, '').toLowerCase();
  const wn = norm(want);
  const cards = Array.from(document.querySelectorAll('[class*="shopCard"], [class*="poilist-item"]')).filter(c => {
    const r = c.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  const nameOf = c => {
    const n = c.querySelector('[class*="name_"], [class*="info1name"]');
    return ((n ? n.innerText : (c.innerText || '').split('\n')[0]) || '').trim();
  };
  if (!cards.length) return JSON.stringify({ok:false, reason:'no_cards'});
  let idx = cards.findIndex(c => nameOf(c) === want || (wn && norm(nameOf(c)) === wn));
  if (idx < 0) idx = cards.findIndex(c => wn && norm(nameOf(c)).includes(wn));
  if (idx < 0) idx = cards.findIndex(c => wn && (wn.includes(norm(nameOf(c))) || (wn.length >= 4 && norm(nameOf(c)).includes(wn.slice(0, 6)))));
  if (idx < 0) return JSON.stringify({ok:false, reason:'shop_not_found', names: cards.map(nameOf).slice(0, 12)});
  const card = cards[idx];
  const el = card.querySelector('[class*="name_"], [class*="info1name"]') || card;
  try { el.scrollIntoView({block:'center', inline:'center'}); } catch(e) {}
  const r = el.getBoundingClientRect();
  return JSON.stringify({ok:true, idx, name:nameOf(card), x:r.x+r.width/2, y:r.y+r.height/2, n:cards.length});
})()
"""


_SHOP_CARD_DISHES_JS = r"""
(() => {
  const idx = IDX;
  const cards = Array.from(document.querySelectorAll('[class*="shopCard"], [class*="poilist-item"]')).filter(c => {
    const r = c.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  const card = cards[idx];
  if (!card) return JSON.stringify({ok:false, dishes:[]});
  const shopNameEl = card.querySelector('[class*="name_"], [class*="info1name"]');
  const shopNameTxt = ((shopNameEl ? shopNameEl.innerText : '') || '').trim();
  const seen = new Set();
  const dishes = [];
  for (const el of card.querySelectorAll('[class*="name_"], [class*="spu"], [class*="food"], div, span')) {
    if (el === shopNameEl) continue;
    if (el.children && el.children.length > 2) continue;
    const t = (el.innerText || '').trim();
    if (!t || t.length < 2 || t.length > 72 || t === shopNameTxt) continue;
    if (/^(月售|起送|配送|¥|￥|\d+(?:\.\d+)?(?:分|分钟|km|m)?$|支持自取|在线咨询|放心吃|准时宝|票$|品牌)/.test(t)) continue;
    if (t.includes('\n')) continue;
    const r = el.getBoundingClientRect();
    if (r.width < 36 || r.height < 12 || r.height > 120) continue;
    if (seen.has(t)) continue;
    seen.add(t);
    dishes.push({text: t, idx});
  }
  return JSON.stringify({ok:true, dishes: dishes.slice(0, 16)});
})()
"""

_CLICK_SHOP_CARD_DISH_JS = r"""
(() => {
  const idx = IDX;
  const targetText = TARGET;
  const cards = Array.from(document.querySelectorAll('[class*="shopCard"], [class*="poilist-item"]')).filter(c => {
    const r = c.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  const card = cards[idx];
  if (!card) return JSON.stringify({ok:false});
  const hits = Array.from(card.querySelectorAll('[class*="name_"], [class*="spu"], [class*="food"], div, span')).filter(el => {
    if (el.children && el.children.length > 2) return false;
    const t = (el.innerText || '').trim();
    const r = el.getBoundingClientRect();
    return t === targetText && r.width >= 36 && r.height >= 12 && r.height <= 120;
  });
  if (!hits.length) return JSON.stringify({ok:false});
  const el = hits[0];
  try { el.scrollIntoView({block:'center', inline:'center'}); } catch(e) {}
  const r = el.getBoundingClientRect();
  if (r.width <= 0 || r.height <= 0) return JSON.stringify({ok:false});
  return JSON.stringify({ok:true, x:r.x+r.width/2, y:r.y+r.height/2, text:targetText});
})()
"""


def _open_restaurant(p_ws: LocalWsCdpClient, shop: str, target_dish: str = "") -> tuple[str, str, str]:
    """在搜索页 trusted-click 目标店铺（若卡片已展示匹配商品则优先点该商品卡以携带 spu_id 置顶，绕开药房店内搜索报错），兼容餐饮 `mindex/menu` 与商超 `sub-trade/restaurant`。"""
    cur_u = _page_url(p_ws)
    if _is_restaurant_url(cur_u):
        qs0 = urllib.parse.parse_qs(urllib.parse.urlsplit(cur_u).query)
        pid0 = (qs0.get("poi_id_str") or [""])[0]
        if pid0:
            return cur_u, pid0, _store_kind_from_url(cur_u)

    info = _eval_obj(p_ws, _SHOP_CLICK_JS.replace("WANT", json.dumps(shop, ensure_ascii=False)))
    if not info.get("ok"):
        names = info.get("names") or []
        raise CartStageError("search_click", f"搜索结果里找不到店铺「{shop}」", visible_shops=names)

    card_idx = int(info.get("idx") or 0)
    url = ""

    # 若指定了目标商品且搜索结果店铺卡片内已有高匹配商品卡，优先点击商品卡以携带 spu_id 直达置顶
    if (target_dish or "").strip():
        dish_list_obj = _eval_obj(p_ws, _SHOP_CARD_DISHES_JS.replace("IDX", str(card_idx)))
        best_tile_txt = ""
        best_tile_sc = 0.0
        for d_it in dish_list_obj.get("dishes") or []:
            t_txt = str((d_it or {}).get("text") or "").strip()
            if not t_txt:
                continue
            sc = _dish_match_score(t_txt, target_dish)
            if sc > best_tile_sc:
                best_tile_sc = sc
                best_tile_txt = t_txt
        if best_tile_txt and best_tile_sc >= 62.0:
            tile_pos = _eval_obj(
                p_ws,
                _CLICK_SHOP_CARD_DISH_JS.replace("IDX", str(card_idx)).replace(
                    "TARGET", json.dumps(best_tile_txt, ensure_ascii=False)
                ),
            )
            if tile_pos.get("ok"):
                _click_xy(p_ws, float(tile_pos["x"]), float(tile_pos["y"]))
                deadline_d = time.monotonic() + 6.0
                while time.monotonic() < deadline_d:
                    _wait(0.5)
                    url = _page_url(p_ws)
                    if _is_restaurant_url(url):
                        break

    if not _is_restaurant_url(url):
        # 回退点击店铺标题进入门店页（重新测量坐标以免前序 scrollIntoView 偏移）
        info2 = _eval_obj(p_ws, _SHOP_CLICK_JS.replace("WANT", json.dumps(shop, ensure_ascii=False)))
        click_src = info2 if info2.get("ok") else info
        _click_xy(p_ws, float(click_src["x"]), float(click_src["y"]))
        deadline = time.monotonic() + 7.0
        while time.monotonic() < deadline:
            _wait(0.5)
            url = _page_url(p_ws)
            if _is_restaurant_url(url):
                break
    if not _is_restaurant_url(url):
        # 重试一次：重新测量卡片中心点
        retry = _eval_obj(
            p_ws,
            "(() => { const cs=Array.from(document.querySelectorAll('[class*=\"shopCard\"], [class*=\"poilist-item\"]')).filter(c=>c.getBoundingClientRect().width>0); const c=cs[IDX]; if(!c) return JSON.stringify({ok:false}); try{c.scrollIntoView({block:'center'});}catch(e){} const r=c.getBoundingClientRect(); return JSON.stringify({ok:true,x:r.x+r.width/2,y:r.y+r.height/2}); })()".replace(
                "IDX", str(card_idx)
            ),
        )
        if retry.get("ok"):
            _click_xy(p_ws, float(retry["x"]), float(retry["y"]))
            deadline = time.monotonic() + 7.0
            while time.monotonic() < deadline:
                _wait(0.5)
                url = _page_url(p_ws)
                if _is_restaurant_url(url):
                    break
    if not _is_restaurant_url(url):
        raise CartStageError("search_click", f"点击店铺「{shop}」后未进入门店页（当前 URL：{url[:72]}）")

    # 商超/医药门店点击后可能先短暂停留 mindex/menu 再跳转至 sub-trade/restaurant，短暂复核防误判为餐饮门店
    if MINDEX_MENU_MARKER in url:
        _wait(1.0)
        _ensure_active(p_ws)
        url_settled = _page_url(p_ws)
        if _is_restaurant_url(url_settled):
            url = url_settled

    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
    poi_id = (qs.get("poi_id_str") or [""])[0]
    if not poi_id:
        raise CartStageError("search_click", "门店页 URL 缺少 poi_id_str")
    return url, poi_id, _store_kind_from_url(url)


def _dish_search_keywords(dish: str) -> list[str]:
    """生成店内搜索关键词序列：剥离规格单位与口语指代词，优先核心食材/品牌词与尾部品类词（如「芫荽那个凉菜」->「芫荽 / 凉菜」）。"""
    s = re.sub(r"^【[^】]*】|^\[[^\]]*\]", "", (dish or "").strip())
    s = re.split(r"[+＋(（]", s)[0]
    s = re.sub(
        r"\d+(?:\.\d+)?\s*(?:g|kg|ml|l|cm|mm|pcs|克|千克|斤|两|份|个|杯|人份|只|片|支|盒|包|袋|瓶|桶|件|颗|粒|条|贴|抽)",
        "",
        s,
        flags=re.IGNORECASE,
    )
    s = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", s)
    cleaned_colloq, _cat_hints = _strip_colloquial_dish_fillers(s)
    c_clean = re.sub(r"[^\u4e00-\u9fffA-Za-z0-9]", "", cleaned_colloq)
    raw_cands: list[str] = []
    if c_clean and c_clean != s:
        raw_cands.append(c_clean[:6])
        if len(c_clean) >= 2:
            raw_cands.append(c_clean[:2])
        # 单字稀有食材兜底（如「芫荽」->「芫」可直搜「御膳芫炝羊肚」）
        for ch in c_clean:
            if "\u4e00" <= ch <= "\u9fff" and ch not in _COMMON_CULINARY_STOP_CHARS:
                raw_cands.append(ch)
                break
    raw_cands.extend(
        [
            s[:6],
            s[:4],
            s[-4:] if len(s) >= 6 else "",
            s[-2:] if len(s) >= 3 else "",
            s[:2] if len(s) >= 3 else "",
        ]
    )
    cands: list[str] = []
    for kw in raw_cands:
        k = kw.strip()
        min_len = 1 if (c_clean and c_clean != s and len(k) == 1 and k not in _COMMON_CULINARY_STOP_CHARS) else 2
        if len(k) >= min_len and k not in cands:
            cands.append(k)
    if not cands and dish:
        cands.append((dish or "").strip()[:6])
    return cands


# ---------------------------------------------------------------------------
# 分支一：商超 / 医药体系 (`sub-trade/restaurant` -> `package/preview/preview`)
# ---------------------------------------------------------------------------


def _wait_menu_ready(p_ws: LocalWsCdpClient, timeout: float = 9.0) -> None:
    _ensure_active(p_ws)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        info = _eval_obj(
            p_ws,
            "JSON.stringify({url:location.href, cards:document.querySelectorAll('[class*=\"food-card-wrap\"]').length, len:((document.body&&document.body.innerText)||'').length})",
        )
        if int(info.get("cards") or 0) > 0 and int(info.get("len") or 0) > 200:
            _check_gate(p_ws, "menu")
            return
        _wait(0.6)
    _check_gate(p_ws, "menu")
    raise CartStageError("menu", "门店页未渲染出菜品列表（可能被后台冻结或页面报错）")


_SUBTRADE_CARDS_JS = r"""
(() => {
  const cardSel = CARDSEL;
  const cards = Array.from(document.querySelectorAll(cardSel)).map((c, domIdx) => ({c, domIdx})).filter(({c}) => {
    const r = c.getBoundingClientRect();
    return r.width > 0 && r.height > 0;
  });
  const out = cards.map(({c, domIdx}) => {
    const h = c.querySelector('[class*="food-item-header"]');
    const title = ((h ? h.innerText : (c.innerText || '').split('\n')[0]) || '').trim();
    const cnt = c.querySelector('.index-flashstepper-count');
    return {
      domIdx,
      title,
      count: cnt ? (parseInt((cnt.innerText || '').trim(), 10) || 0) : 0,
      hasAdd: !!c.querySelector('.index-flashstepper-add_box'),
      hasMinus: !!c.querySelector('.index-flashstepper-but-box'),
      hasSpec: (c.innerText || '').includes('选规格'),
    };
  });
  return JSON.stringify({cards: out});
})()
"""


def _read_dish(p_ws: LocalWsCdpClient, dish: str, card_selector: str = CARD_SEL) -> dict[str, Any]:
    data = _eval_obj(p_ws, _SUBTRADE_CARDS_JS.replace("CARDSEL", json.dumps(card_selector, ensure_ascii=False)))
    cards = data.get("cards") or []
    best_sc = 0.0
    best_card: Optional[dict[str, Any]] = None
    titles: list[str] = []
    for c in cards:
        if not isinstance(c, dict):
            continue
        t = str(c.get("title") or "").strip()
        if t:
            titles.append(t)
        sc = _dish_match_score(t, dish)
        if sc > best_sc:
            best_sc = sc
            best_card = c
    if best_card is None or best_sc < 62.0:
        return {"ok": False, "reason": "dish_not_found", "titles": titles[:20]}
    return {
        "ok": True,
        "idx": int(best_card["domIdx"]),
        "title": str(best_card["title"]),
        "count": int(best_card.get("count") or 0),
        "hasAdd": bool(best_card.get("hasAdd")),
        "hasMinus": bool(best_card.get("hasMinus")),
        "hasSpec": bool(best_card.get("hasSpec")),
    }


def _click_card_control(
    p_ws: LocalWsCdpClient, card_idx: int, selector: str, card_selector: str = CARD_SEL
) -> dict[str, Any]:
    js = r"""
    (() => {
      const cards = Array.from(document.querySelectorAll(CARDSEL));
      const card = cards[IDX];
      if (!card) return JSON.stringify({ok:false, reason:'no_card'});
      const el = card.querySelector(SEL);
      if (!el) return JSON.stringify({ok:false, reason:'no_control'});
      try { el.scrollIntoView({block:'center', inline:'center'}); } catch(e) {}
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return JSON.stringify({ok:false, reason:'zero_size'});
      const x = r.x + r.width/2, y = r.y + r.height/2;
      if (x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) return JSON.stringify({ok:false, reason:'out_of_viewport'});
      return JSON.stringify({ok:true, x, y});
    })()
    """
    expr = (
        js.replace("IDX", str(card_idx))
        .replace("CARDSEL", json.dumps(card_selector, ensure_ascii=False))
        .replace("SEL", json.dumps(selector, ensure_ascii=False))
    )
    info = _eval_obj(p_ws, expr)
    if info.get("ok"):
        _click_xy(p_ws, float(info["x"]), float(info["y"]))
    return info


def _storage_cart_qty(p_ws: LocalWsCdpClient, poi_id: str, dish: str) -> Optional[int]:
    """从 localStorage.localCartData 读商超门店该菜品的真实件数。"""
    raw = _eval_value(p_ws, "localStorage.getItem('localCartData') || '{}'")
    if not isinstance(raw, str) or not raw.strip().startswith("{"):
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    items = obj.get(poi_id) if isinstance(obj, dict) else None
    if not isinstance(items, list):
        return 0
    total = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        name = str(it.get("name") or "")
        if not dish or _dish_match_score(name, dish) >= 62.0:
            total += int(it.get("count") or 0)
    return total


def _prune_subtrade_unrelated_cart(p_ws: LocalWsCdpClient, poi_id: str, target_dishes: list[dict[str, Any]]) -> bool:
    """若商超门店 localCartData 中残留非本单商品，剔除无关项以免混入结算页。"""
    raw = _eval_value(p_ws, "localStorage.getItem('localCartData') || '{}'")
    if not isinstance(raw, str) or not raw.strip().startswith("{"):
        return False
    try:
        obj = json.loads(raw)
    except Exception:
        return False
    items = obj.get(poi_id) if isinstance(obj, dict) else None
    if not isinstance(items, list) or not items:
        return False
    kept = [
        it
        for it in items
        if isinstance(it, dict)
        and any(_dish_match_score(str(it.get("name") or ""), str(d.get("name") or "")) >= 62.0 for d in target_dishes)
    ]
    if len(kept) == len(items):
        return False
    obj[poi_id] = kept
    _eval_value(
        p_ws,
        f"localStorage.setItem('localCartData', {json.dumps(json.dumps(obj, ensure_ascii=False))})",
    )
    return True


def _set_dish_qty(
    p_ws: LocalWsCdpClient, dish: str, qty: int, card_selector: str = CARD_SEL, poi_id: str = ""
) -> dict[str, Any]:
    info = _read_dish(p_ws, dish, card_selector)
    if not info.get("ok"):
        titles = info.get("titles") or []
        raise CartStageError(
            "dish",
            f"门店菜单里找不到菜品「{dish}」" + (f"（可见：{'、'.join(t[:24] for t in titles[:6])}）" if titles else ""),
            visible_titles=titles,
        )
    if not info.get("hasAdd") and not info.get("hasMinus"):
        hint = "该菜品需要选规格" if info.get("hasSpec") else "该菜品没有加购控件"
        raise CartStageError("dish", f"{hint}，暂不支持自动设置数量", dish=info.get("title"))

    matched_title = str(info.get("title") or dish)

    def actual_qty(cur_info: dict[str, Any]) -> int:
        dom_cnt = int(cur_info.get("count") or 0)
        if poi_id:
            stored = _storage_cart_qty(p_ws, poi_id, matched_title)
            if stored is not None:
                # 防止 localStorage.localCartData 异步写入滞后导致重复加购
                if stored == 0 and dom_cnt > 0:
                    return dom_cnt
                return stored
        return dom_cnt

    last = actual_qty(info)
    for _ in range(16):
        if last == qty:
            break
        sel = ADD_SEL if last < qty else MINUS_SEL
        clicked = _click_card_control(p_ws, int(info["idx"]), sel, card_selector)
        if not clicked.get("ok"):
            raise CartStageError("qty", f"无法点击数量{'加' if last < qty else '减'}控件（{clicked.get('reason')}）")
        _wait(0.8)
        info = _read_dish(p_ws, matched_title, card_selector)
        if not info.get("ok"):
            raise CartStageError("qty", "设置数量后菜品卡片消失")
        new_count = actual_qty(info)
        if new_count == last:
            _wait(0.7)
            info = _read_dish(p_ws, matched_title, card_selector)
            if not info.get("ok"):
                raise CartStageError("qty", "设置数量后菜品卡片消失")
            new_count = actual_qty(info)
        if new_count == last:
            _click_card_control(p_ws, int(info["idx"]), sel, card_selector)
            _wait(0.8)
            info = _read_dish(p_ws, matched_title, card_selector)
            if not info.get("ok"):
                raise CartStageError("qty", "设置数量后菜品卡片消失")
            new_count = actual_qty(info)
        last = new_count
    if last != qty:
        raise CartStageError("qty", f"购物车数量未能设为 {qty}（当前 {last}）", actual_qty=last)
    return info


def _press_enter(p_ws: LocalWsCdpClient) -> None:
    for tp in ("rawKeyDown", "char", "keyUp"):
        try:
            p_ws.call(
                "Input.dispatchKeyEvent",
                {
                    "type": tp,
                    "key": "Enter",
                    "code": "Enter",
                    "windowsVirtualKeyCode": 13,
                    "nativeVirtualKeyCode": 13,
                    "text": "\r" if tp == "char" else "",
                },
            )
        except Exception:
            pass


def _inner_search_dish(p_ws: LocalWsCdpClient, poi_id: str, dish: str, buz_type: str = "0") -> dict[str, Any]:
    """商超/医药门店菜单分类未渲染目标菜品时，走 `shop-inner-search` 页搜索。"""
    bt = (buz_type or "0").strip() or "0"
    url = INNER_SEARCH_URL.format(
        poi_id_str=urllib.parse.quote(poi_id, safe=""),
        buz_type=urllib.parse.quote(bt, safe=""),
    )
    kws = _dish_search_keywords(dish)
    last_titles: list[str] = []
    api_err_seen = False
    for kw in kws[:4]:
        p_ws.call("Page.navigate", {"url": url})
        _wait(8.5)
        _ensure_active(p_ws)
        _wait(1.4)
        _check_gate(p_ws, "inner_search")

        clicked = _click_selector(p_ws, 'input[type="text"], input[type="search"], input', 0)
        if not clicked.get("ok"):
            raise CartStageError("inner_search", "店内搜索页没有可输入的搜索框")
        p_ws.call("Input.insertText", {"text": kw})
        sent_enter = False
        deadline = time.monotonic() + 6.5
        while time.monotonic() < deadline:
            _wait(0.6)
            info = _read_dish(p_ws, dish, SEARCH_CARD_SEL)
            if info.get("ok"):
                return info
            last_titles = info.get("titles") or last_titles
            if not last_titles and not sent_enter and time.monotonic() > deadline - 4.0:
                body_now = _body_text(p_ws)
                if "Error#1#" in body_now or "访问出错" in body_now:
                    api_err_seen = True
                    break
                sent_enter = True
                _press_enter(p_ws)
        if api_err_seen:
            break
    if api_err_seen:
        raise CartStageError(
            "inner_search",
            f"该门店的 H5 店内搜索接口返回异常（Error#1#），未能检索「{dish}」",
            keyword=kws[0],
            visible_titles=last_titles,
        )
    raise CartStageError(
        "inner_search",
        f"店内搜索「{'/'.join(kws[:4])}」未找到菜品「{dish}」" + (f"（可见：{'、'.join(t[:24] for t in last_titles[:6])}）" if last_titles else ""),
        keyword=kws[0],
        visible_titles=last_titles,
    )


def _prepare_preview_data(
    p_ws: LocalWsCdpClient,
    menu_url: str,
    total_expected_qty: int,
    target_dishes: list[dict[str, Any]],
) -> dict[str, Any]:
    """回到商超门店页点「去结算」，刷新 localStorage.previewData。"""
    if RESTAURANT_MARKER not in _page_url(p_ws):
        p_ws.call("Page.navigate", {"url": menu_url})
        _wait(9.5)
        _ensure_active(p_ws)
        _wait(1.6)
    _check_gate(p_ws, "checkout")

    # 门店页原地加购后底栏「差¥X起送 -> 去结算」可能延迟 0.5–2s 才刷新，短暂轮询防误判未满起送
    clicked: dict[str, Any] = {"ok": False}
    wait_btn_deadline = time.monotonic() + 3.0
    while time.monotonic() < wait_btn_deadline:
        clicked = _click_text(p_ws, "去结算")
        if clicked.get("ok"):
            break
        _wait(0.45)
        _ensure_active(p_ws)
    if not clicked.get("ok"):
        text = _body_text(p_ws)
        if "起送" in text:
            raise CartStageError("checkout", "购物车未满起送价，无法进入结算预览", reason="below_min_order", body_tail=text[-240:])
        raise CartStageError("checkout", "门店页没有可点击的「去结算」按钮", body_tail=text[-240:])

    deadline = time.monotonic() + 6.0
    last_summary: dict[str, Any] = {}
    while time.monotonic() < deadline:
        _wait(0.5)
        raw = _eval_value(p_ws, "localStorage.getItem('previewData') || ''")
        if not isinstance(raw, str) or not raw.strip().startswith("{"):
            continue
        try:
            obj = json.loads(raw)
        except Exception:
            continue
        foods = [f for f in (obj.get("foodlist") or []) if isinstance(f, dict)]
        counts = [int(f.get("count") or 0) for f in foods]
        matched_qty = sum(
            int(f.get("count") or 0)
            for f in foods
            if any(_dish_match_score(str(f.get("name") or ""), str(d.get("name") or "")) >= 62.0 for d in target_dishes)
        )
        last_summary = {"total": obj.get("total"), "counts": counts, "poi_name": obj.get("poi_name")}
        if foods and matched_qty == total_expected_qty and float(obj.get("total") or 0) > 0:
            return obj
    raise CartStageError(
        "checkout",
        f"点击「去结算」后 previewData 数量不符（期望 {total_expected_qty}，实际 {last_summary.get('counts') or '空'}）",
        preview_data=last_summary,
    )


def _wait_preview(p_ws: LocalWsCdpClient, preview_url: str) -> str:
    for attempt in range(2):
        p_ws.call("Page.navigate", {"url": preview_url})
        _wait(7.0)
        _ensure_active(p_ws)
        _wait(1.6)
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            text = _body_text(p_ws)
            if "提交订单" in text and "合计" in text:
                _check_gate(p_ws, "preview")
                return text
            _wait(0.7)
        if attempt == 0:
            _wait(1.5)
    text = _body_text(p_ws)
    _check_gate(p_ws, "preview")
    if "选择收货地址" not in text and "提交订单" not in text:
        raise CartStageError("preview", "结算预览页未渲染（可能购物车为空或接口报错）", preview_url=_page_url(p_ws))
    raise CartStageError("preview", "结算预览页未出现「提交订单 / 合计」", preview_url=_page_url(p_ws))


def _score_address_candidate(addr: str, n_token: str, addr_hint: str, want_cabinet: bool) -> int:
    na = _norm(addr)
    if not na:
        return -100
    is_cabinet = "外卖柜" in addr
    if is_cabinet and not want_cabinet:
        return -10
    if n_token and na == n_token:
        return 40
    if n_token and n_token in na:
        return 30
    if n_token and na.startswith(n_token[: max(4, len(n_token) - 2)]):
        return 20
    if addr_hint:
        nh = _norm(addr_hint)
        if nh and (nh in na or na in nh):
            return 10
    return 0


_SUBTRADE_DRAWER_STATE_JS = r"""
(() => {
  const vh = window.innerHeight || 844;
  const items = Array.from(document.querySelectorAll('.p-address-list-item'));
  const pc = document.querySelector('.p-picker-content');
  const pcRect = pc ? pc.getBoundingClientRect() : null;
  const lb = document.querySelector('.loading-ui-loading-backdrop');
  const lbVisible = Boolean(
    lb && getComputedStyle(lb).display !== 'none' && lb.getBoundingClientRect().height > 0
  );
  const mapped = items.map((it, i) => {
    const r = it.getBoundingClientRect();
    return {
      i,
      addr: ((it.querySelector('.p-address-addr') || {}).innerText || '').trim(),
      top: Math.round(r.top),
      height: Math.round(r.height),
    };
  });
  const anyVisible = mapped.some(it => it.height > 15 && it.top >= -20 && it.top < vh - 30);
  const drawerSettled = Boolean(
    mapped.length > 0
    && anyVisible
    && !lbVisible
    && (!pcRect || (pcRect.height > 80 && pcRect.top < vh - 120))
  );
  return JSON.stringify({
    items: mapped,
    drawerSettled,
    pcTop: pcRect ? Math.round(pcRect.top) : null,
    lbVisible,
    vh,
  });
})()
"""


def _wait_subtrade_address_drawer_ready(
    p_ws: LocalWsCdpClient,
    *,
    timeout: float = 6.0,
    allow_retry_open: bool = True,
) -> list[dict[str, Any]]:
    """等待商超结算页地址抽屉（`.p-picker-content`）彻底滑入视口且无 loading 遮罩，避免点击落到 `y>844` 视口外。"""
    deadline = time.monotonic() + timeout
    retried_open = False
    last_items: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        _wait(0.35)
        _ensure_active(p_ws)
        st = _eval_obj(p_ws, _SUBTRADE_DRAWER_STATE_JS)
        items = st.get("items") if isinstance(st.get("items"), list) else []
        if items:
            last_items = items
        if st.get("drawerSettled"):
            return items
        if allow_retry_open and not retried_open and not items and time.monotonic() > deadline - (timeout - 2.0):
            retried_open = True
            if not _click_selector(p_ws, ".p-user-address", 0).get("ok"):
                _click_selector(p_ws, ".p-choose-address-txt", 0)
    return last_items


def _measure_subtrade_address_item(p_ws: LocalWsCdpClient, item_idx: int) -> dict[str, Any]:
    """滚动到目标地址条目并确认坐标落在视口内且未被遮罩挡住（若抽屉仍在动画中则短暂等待重测）。"""
    js = r"""
    (() => {
      const vh = window.innerHeight || 844;
      const vw = window.innerWidth || 390;
      const items = Array.from(document.querySelectorAll('.p-address-list-item'));
      const el = items[IDX];
      if (!el) return JSON.stringify({ok:false, reason:'no_item'});
      try { el.scrollIntoView({block:'center', inline:'center'}); } catch(e) {}
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return JSON.stringify({ok:false, reason:'zero_size'});
      const x = r.x + Math.min(r.width * 0.45, 160);
      const y = r.y + r.height / 2;
      const inViewport = x >= 4 && x <= vw - 4 && y >= 10 && y <= vh - 10;
      const hit = inViewport ? document.elementFromPoint(x, y) : null;
      const hitOk = Boolean(hit && (el === hit || el.contains(hit)));
      const hitCls = hit ? String(hit.className || '').slice(0, 60) : 'OUT';
      return JSON.stringify({
        ok: inViewport && hitOk,
        inViewport,
        hitOk,
        hitCls,
        x,
        y,
        addr: ((el.querySelector('.p-address-addr') || {}).innerText || '').trim(),
      });
    })()
    """.replace("IDX", str(int(item_idx)))
    last_pos: dict[str, Any] = {}
    for attempt in range(6):
        pos = _eval_obj(p_ws, js)
        last_pos = pos
        if pos.get("ok"):
            return pos
        if pos.get("reason") == "no_item":
            break
        if attempt < 5:
            _wait(0.4)
            _ensure_active(p_ws)
    return last_pos


def _select_subtrade_address(p_ws: LocalWsCdpClient, token: str, addr_hint: str) -> dict[str, Any]:
    body = _body_text(p_ws)
    n_token = _norm(token)
    want_cabinet = "外卖柜" in (token or "")
    cur_bill = parse_cart_preview_bill(body)
    cur_addr0 = str(cur_bill.get("delivery_address") or "").strip()
    if n_token and n_token in _norm(cur_addr0) and (want_cabinet or "外卖柜" not in cur_addr0):
        return {"ok": True, "already_selected": True, "address": cur_addr0}

    clicked = _click_text(p_ws, "选择收货地址")
    if not clicked.get("ok"):
        if not _click_selector(p_ws, ".p-user-address").get("ok"):
            _click_selector(p_ws, ".p-choose-address-txt")
    _wait(0.6)
    items = _wait_subtrade_address_drawer_ready(p_ws, timeout=6.0, allow_retry_open=True)
    if not items:
        body_chk = _body_text(p_ws)
        for sold_kw in ("商品售罄", "部分商品已下线", "已售罄", "已下线"):
            if sold_kw in body_chk:
                raise CartStageError("address", f"预览页商品异常（{sold_kw}），无法选择收货地址")
        raise CartStageError("address", "没有弹出收货地址列表")

    ranked = sorted(
        ((_score_address_candidate(str(it.get("addr") or ""), n_token, addr_hint, want_cabinet), it) for it in items),
        key=lambda x: -x[0],
    )
    best_score, _best = ranked[0]
    if best_score <= 0:
        avail = [str(it.get("addr") or "") for it in items[:8]]
        raise CartStageError("address", f"地址列表里找不到「{token or addr_hint}」", available_addresses=avail)

    n_prefix = n_token[: max(4, min(8, len(n_token)))] if n_token else _norm(addr_hint)[:6]
    candidates = [(sc, it) for sc, it in ranked if sc > 0]
    min_order_rejected: list[str] = []

    for idx_try, (_sc, cand) in enumerate(candidates[:3]):
        if idx_try > 0:
            # 若地址抽屉已收起再重新打开，避免抽屉仍开着时误点遮罩关闭
            cur_st = _eval_obj(p_ws, _SUBTRADE_DRAWER_STATE_JS)
            if not cur_st.get("drawerSettled"):
                if not _click_selector(p_ws, ".p-user-address").get("ok"):
                    _click_text(p_ws, "选择收货地址")
                _wait(0.6)
                _wait_subtrade_address_drawer_ready(p_ws, timeout=5.5, allow_retry_open=False)
        pos = _measure_subtrade_address_item(p_ws, int(cand.get("i") or 0))
        if not pos.get("ok"):
            continue
        chosen_addr = str(pos.get("addr") or "")
        _click_xy(p_ws, float(pos["x"]), float(pos["y"]))
        _wait(2.6)

        body2 = _body_text(p_ws)
        for sold_kw in ("商品售罄", "部分商品已下线"):
            if sold_kw in body2:
                raise CartStageError("address", f"切换地址后商品状态异常（{sold_kw}）")
        if "当前商品不满起送价" in body2 or "不满起送价" in body2:
            min_order_rejected.append(chosen_addr)
            _click_text(p_ws, "不用了")
            _wait(1.0)
            body_after = _body_text(p_ws)
            cur_addr = str(
                _eval_value(p_ws, "(document.querySelector('.p-user-address')||{}).innerText || ''") or ""
            ).strip()
            if cur_addr and n_prefix and n_prefix in _norm(cur_addr) and "提交订单" in body_after:
                return {
                    "ok": True,
                    "already_selected": False,
                    "fallback_used": True,
                    "address": cur_addr,
                    "rejected_min_order": min_order_rejected,
                }
            continue

        cur_bill2 = parse_cart_preview_bill(body2)
        cur_addr2 = str(cur_bill2.get("delivery_address") or "").strip()
        n_chosen = _norm(chosen_addr)
        if n_chosen and n_chosen not in _norm(cur_addr2):
            _wait(1.6)
            body2 = _body_text(p_ws)
            cur_bill2 = parse_cart_preview_bill(body2)
            cur_addr2 = str(cur_bill2.get("delivery_address") or "").strip()
        if (n_chosen and n_chosen in _norm(cur_addr2)) or (n_token and n_token in _norm(cur_addr2)):
            return {
                "ok": True,
                "already_selected": False,
                "fallback_used": idx_try > 0,
                "address": cur_addr2 or chosen_addr or token,
                "rejected_min_order": min_order_rejected,
            }

    if min_order_rejected:
        raise CartStageError(
            "address",
            f"收货地址「{min_order_rejected[0]}」当前商品不满起送价，无法下单",
            rejected_addresses=min_order_rejected,
        )
    raise CartStageError("address", f"点击地址后预览页仍未显示「{token}」")


def _try_apply_subtrade_coupon(p_ws: LocalWsCdpClient) -> bool:
    """在 `package/preview/preview` 自动遍历全部可用优惠券并择优勾选（美团红包 + 商家代金券 + 配送券，绝不阻断下单）。"""
    from siming.meituan_coupons import apply_optimal_preview_coupons

    res = apply_optimal_preview_coupons(p_ws, store_kind="subtrade")
    return bool(res.get("applied"))


# ---------------------------------------------------------------------------
# 分支二：餐饮外卖体系 (`mindex/menu` -> `mindex/menusearch` -> `mindex/preview`)
# ---------------------------------------------------------------------------


def _wait_mindex_menu_ready(p_ws: LocalWsCdpClient, menu_url: str, loc_target: dict[str, Any]) -> str:
    """等待餐饮 H5 `mindex/menu` 渲染出可见菜品控件；若中途二次跳转到商超 `sub-trade/restaurant` 返回 `'subtrade'`。"""
    for attempt in range(2):
        _setup_page(p_ws, loc_target)
        deadline = time.monotonic() + (6.5 if attempt == 0 else 8.5)
        while time.monotonic() < deadline:
            _ensure_active(p_ws)
            info = _eval_obj(
                p_ws,
                r"""JSON.stringify({
                    url: location.href,
                    visBtns: Array.from(document.querySelectorAll('.sqt-menu-add-buttons')).filter(e => e.getBoundingClientRect().width > 0).length,
                    len: ((document.body && document.body.innerText) || '').length
                })""",
            )
            if RESTAURANT_MARKER in str(info.get("url") or ""):
                return "subtrade"
            if int(info.get("visBtns") or 0) >= 1 and int(info.get("len") or 0) > 120:
                _check_gate(p_ws, "menu")
                body_head = _body_text(p_ws)[:400]
                if _CLOSED_STORE_BANNER_RE.search(body_head):
                    raise CartStageError("menu", "该门店当前显示休息中/已打烊，暂不可下单")
                return "mindex"
            _wait(0.55)
        if attempt == 0:
            p_ws.call("Page.navigate", {"url": menu_url})
            _wait(4.5)
    _check_gate(p_ws, "menu")
    raise CartStageError("menu", "餐饮门店页未渲染出可见菜品列表（可能被后台冻结或门店异常）")


def _mindex_cart_counts_map(p_ws: LocalWsCdpClient, poi_id: str) -> Optional[dict[str, int]]:
    """读取 `localStorage.cached_cart_data[poi_id]` 中各菜品真实加购数量。"""
    raw = _eval_value(p_ws, "localStorage.getItem('cached_cart_data') || '{}'")
    if not isinstance(raw, str) or not raw.strip().startswith("{"):
        return None
    try:
        obj = json.loads(raw)
    except Exception:
        return None
    poi_obj = obj.get(poi_id) if isinstance(obj, dict) else None
    if not isinstance(poi_obj, dict):
        return {}
    counts: dict[str, int] = {}
    for grp in poi_obj.get("cartData") or []:
        if not isinstance(grp, dict):
            continue
        for b in grp.get("bills") or []:
            if not isinstance(b, dict):
                continue
            nm = str(b.get("spuName") or b.get("name") or "").strip()
            cnt = int(b.get("count") or 0)
            if nm and cnt > 0:
                counts[nm] = counts.get(nm, 0) + cnt
    return counts


def _mindex_storage_qty(p_ws: LocalWsCdpClient, poi_id: str, dish: str) -> Optional[int]:
    cmap = _mindex_cart_counts_map(p_ws, poi_id)
    if cmap is None:
        return None
    return sum(cnt for nm, cnt in cmap.items() if _dish_match_score(nm, dish) >= 62.0)


def _clear_mindex_cart_if_unrelated(
    p_ws: LocalWsCdpClient,
    poi_id: str,
    target_dishes: list[dict[str, Any]],
    menu_url: str = "",
    loc_target: Optional[dict[str, Any]] = None,
) -> bool:
    """若餐饮门店购物车中残留上次未结算的其他菜品，优先点开底栏清空，未清空时同步修剪 cached_cart_data 并重载门店页。"""
    cmap = _mindex_cart_counts_map(p_ws, poi_id)
    if not cmap:
        return False
    has_unrelated = any(
        not any(_dish_match_score(nm, str(d.get("name") or "")) >= 62.0 for d in target_dishes)
        for nm in cmap
    )
    if not has_unrelated:
        return False

    opened_cart = _click_selector(p_ws, '[aria-label="购物车"]', 0).get("ok")
    if not opened_cart:
        opened_cart = _click_selector(p_ws, '[class*="cartIcon_"], [class*="cartImg_"], [class*="cart_"]', 0).get("ok")
    if opened_cart:
        _wait(0.85)
        if _click_text(p_ws, "清空购物车").get("ok") or _click_text(p_ws, "清空").get("ok"):
            _wait(0.65)
            for confirm_lbl in ("清空", "确定", "确认"):
                if _click_text(p_ws, confirm_lbl).get("ok"):
                    break
            _wait(0.8)

    cmap_after = _mindex_cart_counts_map(p_ws, poi_id) or {}
    still_unrelated = any(
        not any(_dish_match_score(nm, str(d.get("name") or "")) >= 62.0 for d in target_dishes)
        for nm in cmap_after
    )
    if not still_unrelated:
        return True

    # UI 按钮未能清掉残留项时，直接清理本门店 cached_cart_data 中无关条目并刷新门店页，确保账单零污染
    raw = _eval_value(p_ws, "localStorage.getItem('cached_cart_data') || '{}'")
    if isinstance(raw, str) and raw.strip().startswith("{"):
        try:
            obj = json.loads(raw)
            poi_obj = obj.get(poi_id) if isinstance(obj, dict) else None
            if isinstance(poi_obj, dict) and isinstance(poi_obj.get("cartData"), list):
                new_groups = []
                for grp in poi_obj["cartData"]:
                    if not isinstance(grp, dict):
                        continue
                    kept_bills = [
                        b
                        for b in (grp.get("bills") or [])
                        if isinstance(b, dict)
                        and any(
                            _dish_match_score(str(b.get("spuName") or b.get("name") or ""), str(d.get("name") or "")) >= 62.0
                            for d in target_dishes
                        )
                    ]
                    if kept_bills:
                        new_groups.append({**grp, "bills": kept_bills})
                poi_obj["cartData"] = new_groups
                obj[poi_id] = poi_obj
                _eval_value(
                    p_ws,
                    f"localStorage.setItem('cached_cart_data', {json.dumps(json.dumps(obj, ensure_ascii=False))})",
                )
                if menu_url:
                    p_ws.call("Page.navigate", {"url": menu_url})
                    _wait(4.0)
                    _setup_page(p_ws, loc_target)
                    _wait(1.0)
                return True
        except Exception:
            pass
    return False


_MINDEX_VISIBLE_CARDS_JS = r"""
(() => {
  const btns = Array.from(document.querySelectorAll('.sqt-menu-add-buttons'));
  const out = [];
  btns.forEach((btn, btnIdx) => {
    const br = btn.getBoundingClientRect();
    if (br.width <= 0 || br.height <= 0) return;
    let card = btn;
    for (let i = 0; i < 8 && card.parentElement; i++) {
      card = card.parentElement;
      if (card.querySelectorAll('.sqt-menu-add-buttons').length > 1) break;
      const cls = String(card.className || '');
      if (/spu_|food|item/i.test(cls) && (card.innerText || '').includes('¥')) break;
    }
    const lines = (card.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
    const title = lines[0] || '';
    const plus = btn.querySelector('[class*="plus_"]');
    const minus = btn.querySelector('[class*="minus_"]');
    const cntEl = btn.querySelector('[class*="count_"]');
    const pr = plus ? plus.getBoundingClientRect() : null;
    const mr = minus ? minus.getBoundingClientRect() : null;
    out.push({
      btnIdx,
      title,
      lines: lines.slice(0, 6),
      count: cntEl ? (parseInt((cntEl.innerText || '').trim(), 10) || 0) : 0,
      hasPlus: Boolean(pr && pr.width > 0 && pr.height > 0),
      hasMinus: Boolean(mr && mr.width > 0 && mr.height > 0),
      hasSpec: !(pr && pr.width > 0) && ((btn.innerText || '').includes('规格') || br.width >= 36),
    });
  });
  return JSON.stringify({cards: out});
})()
"""


def _mindex_read_dish(p_ws: LocalWsCdpClient, dish: str, *, category_name: str = "") -> dict[str, Any]:
    data = _eval_obj(p_ws, _MINDEX_VISIBLE_CARDS_JS)
    cards = data.get("cards") or []
    best_sc = 0.0
    best_card: Optional[dict[str, Any]] = None
    titles: list[str] = []
    for c in cards:
        if not isinstance(c, dict):
            continue
        t = str(c.get("title") or "").strip()
        lines = [str(x) for x in (c.get("lines") or [])]
        if not t or any(k in "".join(lines) for k in ("售罄", "已抢光", "今日售完", "暂不可售")):
            if t:
                titles.append(f"{t}(售罄)")
            continue
        titles.append(t)
        sc = _dish_match_score(t, dish, card_lines=lines, category_name=category_name)
        if sc > best_sc:
            best_sc = sc
            best_card = c
    if best_card is None or best_sc < 62.0:
        return {"ok": False, "reason": "dish_not_found", "titles": titles[:20]}
    return {
        "ok": True,
        "btnIdx": int(best_card["btnIdx"]),
        "title": str(best_card["title"]),
        "count": int(best_card.get("count") or 0),
        "hasPlus": bool(best_card.get("hasPlus")),
        "hasMinus": bool(best_card.get("hasMinus")),
        "hasSpec": bool(best_card.get("hasSpec")),
    }


def _mindex_click_btn_sub(p_ws: LocalWsCdpClient, btn_idx: int, sub_sel: str = "") -> dict[str, Any]:
    js = r"""
    (() => {
      const btn = document.querySelectorAll('.sqt-menu-add-buttons')[IDX];
      if (!btn) return JSON.stringify({ok:false, reason:'no_btn'});
      const el = SUB ? btn.querySelector(SUB) : btn;
      if (!el) return JSON.stringify({ok:false, reason:'no_sub'});
      try { el.scrollIntoView({block:'center', inline:'center'}); } catch(e) {}
      const r = el.getBoundingClientRect();
      if (r.width <= 0 || r.height <= 0) return JSON.stringify({ok:false, reason:'zero_size'});
      const x = r.x + r.width / 2, y = r.y + r.height / 2;
      if (x < 0 || y < 0 || x > window.innerWidth || y > window.innerHeight) return JSON.stringify({ok:false, reason:'out_of_viewport'});
      return JSON.stringify({ok:true, x, y});
    })()
    """.replace("IDX", str(btn_idx)).replace("SUB", json.dumps(sub_sel))
    info = _eval_obj(p_ws, js)
    if info.get("ok"):
        _click_xy(p_ws, float(info["x"]), float(info["y"]))
    return info


def _mindex_open_menusearch(p_ws: LocalWsCdpClient, menu_url: str, poi_id: str = "") -> None:
    if "mindex/menusearch" in _page_url(p_ws):
        return
    if not _is_mindex_menu_url(_page_url(p_ws)):
        p_ws.call("Page.navigate", {"url": menu_url})
        _wait(4.5)
        _ensure_active(p_ws)
        _wait(1.0)
    # 精准查找当前视口内最顶层的门店搜索入口（防 SPA 底层残留 searchresults DOM 干扰）
    search_pos = _eval_obj(
        p_ws,
        r"""(() => {
            const vw = window.innerWidth || 390;
            const vh = window.innerHeight || 844;
            const cands = Array.from(document.querySelectorAll('[class*="search_"], [class*="Search"]')).filter(el => {
                const r = el.getBoundingClientRect();
                if (r.width < 20 || r.height < 16 || r.top < 0 || r.bottom > vh || r.left < 0 || r.right > vw) return false;
                const t = (el.innerText || '').trim();
                return t === '搜索' || t.includes('菜品') || t.includes('商品') || t.length <= 10;
            });
            cands.sort((a, b) => {
                const ta = (a.innerText || '').trim();
                const tb = (b.innerText || '').trim();
                const sa = (ta === '搜索' || ta.includes('菜品') || ta.includes('商品')) ? 0 : 1;
                const sb = (tb === '搜索' || tb.includes('菜品') || tb.includes('商品')) ? 0 : 1;
                return sa - sb || a.getBoundingClientRect().top - b.getBoundingClientRect().top;
            });
            for (const el of cands) {
                const r = el.getBoundingClientRect();
                const x = r.x + r.width / 2, y = r.y + r.height / 2;
                const hit = document.elementFromPoint(x, y);
                if (hit && (el === hit || el.contains(hit) || hit.contains(el))) {
                    return JSON.stringify({ok: true, x, y, text: (el.innerText || '').trim().slice(0, 20)});
                }
            }
            if (cands.length) {
                const r = cands[0].getBoundingClientRect();
                return JSON.stringify({ok: true, x: r.x + r.width / 2, y: r.y + r.height / 2, text: (cands[0].innerText || '').trim().slice(0, 20)});
            }
            return JSON.stringify({ok: false});
        })()""",
    )
    if search_pos.get("ok"):
        _click_xy(p_ws, float(search_pos["x"]), float(search_pos["y"]))
    else:
        _click_text(p_ws, "搜索")
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        _wait(0.4)
        _ensure_active(p_ws)
        if "mindex/menusearch" in _page_url(p_ws):
            return
    # 兜底 1：JS click 触发 React onClick
    _eval_value(
        p_ws,
        r"""(() => {
            const el = Array.from(document.querySelectorAll('[class*="search_"]')).find(e => {
                const r = e.getBoundingClientRect();
                const t = (e.innerText || '').trim();
                return r.width >= 20 && r.height >= 16 && r.top >= 0 && r.bottom <= (window.innerHeight || 844) && (t === '搜索' || t.includes('菜品') || t.includes('商品'));
            });
            if (el) { el.click(); return true; }
            return false;
        })()""",
    )
    deadline2 = time.monotonic() + 2.5
    while time.monotonic() < deadline2:
        _wait(0.4)
        _ensure_active(p_ws)
        if "mindex/menusearch" in _page_url(p_ws):
            return
    # 兜底 2：直航 mindex/menusearch?poi_id_str=...（与门店共享同源 localStorage.cached_cart_data）
    eff_poi = (poi_id or "").strip()
    if not eff_poi:
        qs_u = urllib.parse.parse_qs(urllib.parse.urlsplit(menu_url or _page_url(p_ws)).query)
        eff_poi = (qs_u.get("poi_id_str") or [""])[0]
    if eff_poi:
        direct_ms = f"https://h5.waimai.meituan.com/waimai/mindex/menusearch?poi_id_str={urllib.parse.quote(eff_poi, safe='')}"
        p_ws.call("Page.navigate", {"url": direct_ms})
        deadline3 = time.monotonic() + 5.5
        while time.monotonic() < deadline3:
            _wait(0.5)
            _ensure_active(p_ws)
            if "mindex/menusearch" in _page_url(p_ws):
                _wait(0.6)
                return
    raise CartStageError("inner_search", "无法打开餐饮门店店内搜索页（mindex/menusearch）")


def _mindex_scan_categories_for_dish(
    p_ws: LocalWsCdpClient,
    menu_url: str,
    dish: str,
    max_categories: int = 16,
) -> dict[str, Any]:
    """懒加载餐饮门店兜底：在同一 `mindex/menu` 标签内依次点选左侧分类（跳过必选品/团餐），命中即停。"""
    if not _is_mindex_menu_url(_page_url(p_ws)):
        p_ws.call("Page.navigate", {"url": menu_url})
        _wait(4.0)
        _ensure_active(p_ws)
        _wait(0.8)
    cat_list = _eval_obj(
        p_ws,
        r"""(() => {
            const cats = Array.from(document.querySelectorAll('[class*="link-"]')).filter(el => {
                const r = el.getBoundingClientRect();
                const t = (el.innerText || '').trim().split('\n')[0].trim();
                return r.left < 120 && r.width > 20 && r.height > 15 && t && !/必选品|必选餐具|团餐/.test(t);
            }).map((el, i) => ({
                cls: Array.from(el.classList || []).find(c => /^link-\d+$/.test(c)) || '',
                name: (el.innerText || '').trim().split('\n')[0].trim(),
                idx: i,
            }));
            return JSON.stringify({cats});
        })()""",
    ).get("cats") or []
    _, cat_hints = _strip_colloquial_dish_fillers(dish)
    if cat_hints and cat_list:
        cat_list = sorted(
            cat_list,
            key=lambda c: (0 if any(h in str(c.get("name") or "") for h in cat_hints) else 1, int(c.get("idx") or 0)),
        )
    last_titles: list[str] = []
    for cat in cat_list[:max_categories]:
        link_cls = str(cat.get("cls") or "")
        cat_name = str(cat.get("name") or "")
        if not link_cls and not cat_name:
            continue
        _eval_value(
            p_ws,
            r"""(() => {
                const cls = LINKCLS;
                const nm = CATNM;
                const el = (cls ? document.querySelector('.' + cls) : null)
                    || Array.from(document.querySelectorAll('[class*="link-"], li, div')).find(e => e.getBoundingClientRect().left < 120 && (e.innerText || '').trim().startsWith(nm));
                if (!el) return false;
                try { el.scrollIntoView({block: 'center'}); } catch (e) {}
                el.click();
                return true;
            })()""".replace("LINKCLS", json.dumps(link_cls)).replace("CATNM", json.dumps(cat_name, ensure_ascii=False)),
        )
        _wait(0.85)
        _ensure_active(p_ws)
        info = _mindex_read_dish(p_ws, dish, category_name=cat_name)
        if info.get("ok"):
            return info
        last_titles = info.get("titles") or last_titles
    return {"ok": False, "reason": "dish_not_found_in_categories", "titles": last_titles}


def _mindex_search_and_find_dish(p_ws: LocalWsCdpClient, menu_url: str, dish: str, poi_id: str = "") -> dict[str, Any]:
    """在 `mindex/menusearch` 搜索目标菜品（支持清空旧词与二字词回退，兼容「滑蛋牛肉 -> 滑蛋炒牛肉」）。"""
    _mindex_open_menusearch(p_ws, menu_url, poi_id=poi_id)
    kws = _dish_search_keywords(dish)
    last_titles: list[str] = []

    for kw in kws[:3]:
        if "mindex/menusearch" not in _page_url(p_ws):
            _mindex_open_menusearch(p_ws, menu_url, poi_id=poi_id)
        # 优先点击搜索框右侧清空图标（若有），再聚焦输入框清空并输入关键词
        _click_selector(p_ws, '[class*="clear_"], [class*="clean_"], [class*="delIcon_"]', 0)
        inp_click = _click_selector(p_ws, 'input[type="search"], [class*="searchInput_"], input', 0)
        if not inp_click.get("ok"):
            # 若展示态隐藏了输入框，点击顶部搜索词条重新激活输入态
            _click_xy(p_ws, 160.0, 22.0)
            _wait(0.5)
            inp_click = _click_selector(p_ws, 'input[type="search"], [class*="searchInput_"], input', 0)
        if not inp_click.get("ok"):
            raise CartStageError("inner_search", "餐饮店内搜索页未找到可输入的搜索框")

        _eval_value(
            p_ws,
            """(() => {
                const inp = Array.from(document.querySelectorAll('input')).find(e => e.getBoundingClientRect().width > 0);
                if (!inp) return false;
                inp.focus();
                try { inp.select(); document.execCommand('delete', false, null); } catch (e) {}
                if (inp.value) {
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value')?.set;
                    if (setter) setter.call(inp, '');
                    inp.dispatchEvent(new Event('input', {bubbles: true}));
                }
                return true;
            })()""",
        )
        p_ws.call("Input.insertText", {"text": kw})
        _wait(0.25)
        sub_click = _click_selector(p_ws, '[class*="submit_"]', 0)
        if not sub_click.get("ok"):
            _click_text(p_ws, "搜索")

        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            _wait(0.55)
            _ensure_active(p_ws)
            info = _mindex_read_dish(p_ws, dish)
            if info.get("ok"):
                return info
            last_titles = info.get("titles") or last_titles

    raise CartStageError(
        "inner_search",
        f"店内搜索「{'/'.join(kws[:3])}」未找到菜品「{dish}」"
        + (f"（店内相近可见：{'、'.join(t[:24] for t in last_titles[:6])}）" if last_titles else ""),
        keyword=kws[0],
        visible_titles=last_titles,
    )


def _mindex_add_via_spec_modal(p_ws: LocalWsCdpClient, btn_idx: int, spec_prefs: Optional[list[str]] = None) -> bool:
    """处理餐饮门店「选规格」弹窗：可选点选杯型/冰热/辣度偏好后点「加入购物车 / 选好了」。"""
    if not _mindex_click_btn_sub(p_ws, btn_idx, "").get("ok"):
        return False
    _wait(0.85)
    for pref in spec_prefs or []:
        if pref:
            _click_text(p_ws, pref)
            _wait(0.25)
    for confirm_txt in ("加入购物车", "选好了"):
        if _click_text(p_ws, confirm_txt).get("ok"):
            _wait(0.85)
            return True
    return False


def _mindex_set_dish_qty(
    p_ws: LocalWsCdpClient,
    menu_url: str,
    poi_id: str,
    dish: str,
    qty: int,
    spec_prefs: Optional[list[str]] = None,
) -> tuple[dict[str, Any], bool]:
    """在餐饮门店页（自动降级 `mindex/menusearch` 与左侧懒加载分类扫描）将单个菜品精确设为目标数量 `qty`。"""
    used_inner = "mindex/menusearch" in _page_url(p_ws)
    info = _mindex_read_dish(p_ws, dish) if not used_inner else {"ok": False}
    if not info.get("ok"):
        try:
            info = _mindex_search_and_find_dish(p_ws, menu_url, dish, poi_id=poi_id)
            used_inner = True
        except CartStageError as ms_exc:
            # 若店内搜索异常或未命中，回退到同一 mindex/menu 标签内遍历左侧懒加载分类查找
            cat_info = _mindex_scan_categories_for_dish(p_ws, menu_url, dish)
            if cat_info.get("ok"):
                info = cat_info
                used_inner = False
            else:
                raise ms_exc

    matched_title = str(info["title"])

    def actual_qty() -> int:
        stored = _mindex_storage_qty(p_ws, poi_id, matched_title)
        if stored is not None:
            return stored
        cur_i = _mindex_read_dish(p_ws, matched_title)
        return int(cur_i.get("count") or 0)

    last = actual_qty()
    for _ in range(16):
        if last == qty:
            break
        cur_info = _mindex_read_dish(p_ws, matched_title)
        if not cur_info.get("ok"):
            raise CartStageError("qty", f"设置数量过程中菜品「{matched_title}」卡片不可见")
        b_idx = int(cur_info["btnIdx"])
        if last < qty:
            if cur_info.get("hasPlus"):
                clicked = _mindex_click_btn_sub(p_ws, b_idx, '[class*="plus_"]')
                if not clicked.get("ok"):
                    raise CartStageError("qty", f"无法点击「{matched_title}」加号控件（{clicked.get('reason')}）")
            elif cur_info.get("hasSpec"):
                if not _mindex_add_via_spec_modal(p_ws, b_idx, spec_prefs=spec_prefs):
                    raise CartStageError("qty", f"菜品「{matched_title}」规格弹窗未能完成加购")
            else:
                raise CartStageError("qty", f"菜品「{matched_title}」无可用加购按钮")
        else:
            if cur_info.get("hasMinus"):
                clicked = _mindex_click_btn_sub(p_ws, b_idx, '[class*="minus_"]')
                if not clicked.get("ok"):
                    raise CartStageError("qty", f"无法点击「{matched_title}」减号控件（{clicked.get('reason')}）")
            else:
                raise CartStageError("qty", f"多规格菜品「{matched_title}」列表无直接减号，请清空重试")

        _wait(0.65)
        new_cnt = actual_qty()
        if new_cnt == last and last < qty and cur_info.get("hasPlus"):
            _wait(0.45)
            _mindex_click_btn_sub(p_ws, b_idx, '[class*="plus_"]')
            _wait(0.65)
            new_cnt = actual_qty()
        last = new_cnt

    if last != qty:
        raise CartStageError("qty", f"菜品「{matched_title}」购物车数量未能设为 {qty}（当前 {last}）", actual_qty=last)
    return {"ok": True, "title": matched_title, "qty": last}, used_inner


_MINDEX_REQUIRED_STATE_JS = r"""
(() => {
  const btn = document.querySelector('[class*="goToPreview"]');
  let comp = null;
  if (btn) {
    const fk = Object.keys(btn).find(k => k.startsWith('__reactInternalInstance') || k.startsWith('__reactFiber'));
    for (let f = fk ? btn[fk] : null, i = 0; i < 25 && f; i++, f = f.return) {
      if (f.stateNode && typeof f.stateNode === 'object' && f.stateNode.props) {
        if (f.stateNode.handleSubmitClick || f.stateNode.props.required_product_info || f.stateNode.props.required_tag_info) {
          comp = f.stateNode;
          break;
        }
      }
    }
  }
  const rpi = comp ? (comp.props.required_product_info || null) : null;
  const rti = comp ? (comp.props.required_tag_info || null) : null;
  const requiredTagId = String((rpi && rpi.required_tag_id) || (rti && rti.required_tag_id) || '');
  const hasRequired = Boolean(
    (rpi && rpi.has_required_product === true)
    || (rti && rti.has_required_tag === false)
  );
  const needsRequired = Boolean(
    (rpi && rpi.required_tag_id && rpi.has_required_product === false)
    || (rti && rti.has_required_tag === true && (!rpi || rpi.has_required_product === false))
  );
  let catLinkCls = '';
  if (comp && requiredTagId) {
    const cats = comp.props.categoryList || comp.props.food_spu_tags || [];
    const idx = cats.findIndex(c => String(c && c.tag) === requiredTagId);
    if (idx >= 0) catLinkCls = 'link-' + idx;
  }
  const sidebarHasReq = Array.from(document.querySelectorAll('[class*="link-"], li, div')).some(el => {
    const r = el.getBoundingClientRect();
    const t = (el.innerText || '').trim();
    return r.left < 120 && r.width > 20 && r.height > 15 && t.length <= 12 && /必选餐具|必选品/.test(t);
  });
  return JSON.stringify({
    hasComp: Boolean(comp),
    needsRequired,
    hasRequired,
    requiredTagId,
    catLinkCls,
    sidebarHasReq,
    tips: (rpi && rpi.tips) || '',
    toast: (rpi && rpi.toast) || '',
  });
})()
"""


def _mindex_pick_required_item_in_dom(p_ws: LocalWsCdpClient, required_tag_id: str = "") -> dict[str, Any]:
    """在已切到「必选品 / 必选餐具」分类的 `mindex/menu` 上挑选最优合法必选项并点击加号：
    优先级：1) ¥0「不需要餐具 / 无需餐具」 -> 2) 其他 ¥0 非活动说明项 -> 3) 最便宜(≤¥2)非活动说明项（如 ¥0.3 需要餐具）。
    严格跳过「勿拍 / 活动说明 / 收藏领券 / 联系门店 / 售罄」类干扰条目。
    """
    pick_info = _eval_obj(
        p_ws,
        r"""(() => {
            const reqTagId = REQTAG;
            const DECOY_RE = /勿拍|活动说明|点击右上角|收藏领券|关注领券|好评返现|收藏店铺|温馨提示|联系门店|团餐定制/;
            const SOLD_RE = /售罄|已抢光|今日售完|暂不可售|已下架/;
            const FREE_NO_UTENSIL_RE = /不需要餐具|无需餐具|不要餐具|免餐具/;

            // 找「必选品 / 必选餐具」右侧分组标题的 DOM 位置（若找不到则取末尾 10 个商品卡片）
            const headings = Array.from(document.querySelectorAll('dt, h3, h4, div, p, span')).filter(el => {
                const t = (el.innerText || '').trim();
                const r = el.getBoundingClientRect();
                return r.left >= 70 && r.width > 30 && r.height >= 12 && r.height <= 60 && /^(?:必选品|必选餐具)$/.test(t);
            });
            const minTop = headings.length ? headings[headings.length - 1].getBoundingClientRect().top - 10 : null;

            const allBtns = Array.from(document.querySelectorAll('.sqt-menu-add-buttons')).filter(b => {
                const r = b.getBoundingClientRect();
                return r.width > 0 && r.height > 0;
            });
            const scoped = [];
            for (const b of allBtns) {
                let card = b;
                for (let i = 0; i < 8 && card.parentElement; i++) {
                    card = card.parentElement;
                    if (card.querySelectorAll('.sqt-menu-add-buttons').length > 1) break;
                    const cls = String(card.className || '');
                    if (/spu_|food|item/i.test(cls) && (card.innerText || '').includes('¥')) break;
                }
                const cr = card.getBoundingClientRect();
                if (minTop !== null && cr.top < minTop) continue;
                const txt = (card.innerText || '').trim();
                const lines = txt.split('\n').map(s => s.trim()).filter(Boolean);
                const title = lines[0] || '';
                if (!title || SOLD_RE.test(txt) || DECOY_RE.test(txt)) continue;
                const mPrice = txt.match(/[¥￥]\s*(\d+(?:\.\d{1,2})?)/);
                const price = mPrice ? parseFloat(mPrice[1]) : 999;
                let rank = 99;
                if (FREE_NO_UTENSIL_RE.test(title) && price <= 0.01) rank = 0;
                else if (FREE_NO_UTENSIL_RE.test(title)) rank = 1;
                else if (price <= 0.01) rank = 2;
                else if (price <= 2.0) rank = 3;
                else continue;
                const plus = b.querySelector('[class*="plus_"], [class*="plus"]') || b;
                scoped.push({rank, price, title, el: plus});
            }
            if (scoped.length) {
                scoped.sort((a, b) => a.rank - b.rank || a.price - b.price);
                const best = scoped[0];
                try { best.el.scrollIntoView({block: 'center', inline: 'center'}); } catch (e) {}
                const r = best.el.getBoundingClientRect();
                const vh = window.innerHeight || 844;
                const inSafeView = r.width > 0 && r.height > 0 && r.top >= 40 && r.bottom <= vh - 76;
                return JSON.stringify({
                    ok: true,
                    via: 'dom',
                    title: best.title,
                    price: best.price,
                    inSafeView,
                    x: r.x + r.width / 2,
                    y: r.y + r.height / 2,
                });
            }

            // 兜底：若必选分类 DOM 仍未渲染但 React categoryList 已含 SPU 数据，直接触发 skuIncrease
            const goBtn = document.querySelector('[class*="goToPreview"]');
            if (goBtn) {
                const fk = Object.keys(goBtn).find(k => k.startsWith('__reactInternalInstance') || k.startsWith('__reactFiber'));
                let comp = null;
                for (let f = fk ? goBtn[fk] : null, i = 0; i < 25 && f; i++, f = f.return) {
                    if (f.stateNode && f.stateNode.props && typeof f.stateNode.props.skuIncrease === 'function') {
                        comp = f.stateNode;
                        break;
                    }
                }
                if (comp) {
                    const cats = comp.props.categoryList || comp.props.food_spu_tags || [];
                    const reqCat = cats.find(c => (reqTagId && String(c.tag) === reqTagId) || /必选/.test(String(c.name || '')));
                    const spus = (reqCat && reqCat.spus) || [];
                    const cands = [];
                    for (const spu of spus) {
                        const nm = String(spu.name || '');
                        const desc = String(spu.description || '');
                        if (!nm || spu.status === 1 || SOLD_RE.test(nm + desc) || DECOY_RE.test(nm + desc)) continue;
                        const skus = spu.skus || [];
                        if (!skus.length) continue;
                        const price = parseFloat(skus[0].price ?? spu.min_price ?? 999);
                        let rank = 99;
                        if (FREE_NO_UTENSIL_RE.test(nm) && price <= 0.01) rank = 0;
                        else if (FREE_NO_UTENSIL_RE.test(nm)) rank = 1;
                        else if (price <= 0.01) rank = 2;
                        else if (price <= 2.0) rank = 3;
                        else continue;
                        cands.push({rank, price, spu, sku: skus[0]});
                    }
                    cands.sort((a, b) => a.rank - b.rank || a.price - b.price);
                    if (cands.length) {
                        const chosen = cands[0];
                        const attrs = (chosen.spu.attrs || []).map(a => a.values && a.values[0] && a.values[0].id).filter(Boolean);
                        comp.props.skuIncrease(chosen.spu.id, chosen.sku.id, attrs, chosen.spu.name);
                        return JSON.stringify({ok: true, via: 'skuIncrease', title: chosen.spu.name, price: chosen.price});
                    }
                }
            }
            return JSON.stringify({ok: false});
        })()""".replace("REQTAG", json.dumps(required_tag_id)),
    )
    if not pick_info.get("ok"):
        return pick_info
    if pick_info.get("via") == "dom":
        if pick_info.get("inSafeView"):
            _click_xy(p_ws, float(pick_info["x"]), float(pick_info["y"]))
        else:
            # 元素靠近底栏被遮挡时直接调 DOM click 触发 React onClick
            _eval_value(
                p_ws,
                r"""(() => {
                    const want = WANT;
                    const btns = Array.from(document.querySelectorAll('.sqt-menu-add-buttons')).filter(b => b.getBoundingClientRect().width > 0);
                    for (let i = btns.length - 1; i >= 0; i--) {
                        let card = btns[i];
                        for (let k = 0; k < 8 && card.parentElement; k++) {
                            card = card.parentElement;
                            if (card.querySelectorAll('.sqt-menu-add-buttons').length > 1) break;
                            if (/spu_|food|item/i.test(String(card.className || '')) && (card.innerText || '').includes('¥')) break;
                        }
                        if ((card.innerText || '').includes(want)) {
                            const plus = btns[i].querySelector('[class*="plus_"], [class*="plus"]') || btns[i];
                            plus.click();
                            return true;
                        }
                    }
                    return false;
                })()""".replace("WANT", json.dumps(str(pick_info.get("title") or ""), ensure_ascii=False)),
            )
        _wait(0.8)
    else:
        _wait(0.8)
    return pick_info


def ensure_mindex_required_products(p_ws: LocalWsCdpClient, *, force_dom_check: bool = False) -> bool:
    """在餐饮门店 `mindex/menu` 点击「去结算」前主动检测并补齐「必选品 / 必选餐具」（如南京大牌档等门店）。

    返回 True 表示确实补充了必选项；返回 False 表示无需补充或未发现必选项。
    """
    st = _eval_obj(p_ws, _MINDEX_REQUIRED_STATE_JS)
    needs = bool(st.get("needsRequired"))
    if not needs:
        if not force_dom_check or not st.get("sidebarHasReq"):
            return False
        if st.get("hasComp") and st.get("hasRequired"):
            return False

    req_tag_id = str(st.get("requiredTagId") or "")
    cat_link_cls = str(st.get("catLinkCls") or "")

    # 切到左侧「必选品 / 必选餐具」分类（它在侧边栏最底部常位于 y>844 视口外，先 scrollIntoView 再 click() 最稳）
    clicked_cat = _eval_value(
        p_ws,
        r"""(() => {
            const cls = LINKCLS;
            const byCls = cls ? document.querySelector('.' + cls) : null;
            const byTxt = Array.from(document.querySelectorAll('[class*="link-"], div, li, span')).find(el => {
                const t = (el.innerText || '').trim().split('\n')[0].trim();
                const rc = el.getBoundingClientRect();
                return rc.left < 120 && rc.width > 20 && rc.height > 15 && t.length <= 12 && /^(?:必选餐具|必选品)$/.test(t);
            });
            const cat = byCls || byTxt;
            if (!cat) return false;
            try { cat.scrollIntoView({block: 'center'}); } catch (e) {}
            cat.click();
            return true;
        })()""".replace("LINKCLS", json.dumps(cat_link_cls)),
    )
    if not clicked_cat:
        return False

    # 必选品分类为懒加载 XHR，轮询等待其菜品渲染完毕（最长 4.5s）
    deadline = time.monotonic() + 4.5
    picked: dict[str, Any] = {"ok": False}
    while time.monotonic() < deadline:
        _wait(0.55)
        _ensure_active(p_ws)
        picked = _mindex_pick_required_item_in_dom(p_ws, required_tag_id=req_tag_id)
        if picked.get("ok"):
            break

    if not picked.get("ok"):
        return False

    # 核验 required_product_info 是否已满足；若首次鼠标点击未落盘则补一次 DOM click
    st_after = _eval_obj(p_ws, _MINDEX_REQUIRED_STATE_JS)
    if st_after.get("needsRequired") and picked.get("via") == "dom":
        _eval_value(
            p_ws,
            r"""(() => {
                const want = WANT;
                const btns = Array.from(document.querySelectorAll('.sqt-menu-add-buttons')).filter(b => b.getBoundingClientRect().width > 0);
                for (let i = btns.length - 1; i >= 0; i--) {
                    let card = btns[i];
                    for (let k = 0; k < 8 && card.parentElement; k++) {
                        card = card.parentElement;
                        if (card.querySelectorAll('.sqt-menu-add-buttons').length > 1) break;
                        if (/spu_|food|item/i.test(String(card.className || '')) && (card.innerText || '').includes('¥')) break;
                    }
                    if ((card.innerText || '').includes(want)) {
                        const plus = btns[i].querySelector('[class*="plus_"], [class*="plus"]') || btns[i];
                        plus.click();
                        return true;
                    }
                }
                return false;
            })()""".replace("WANT", json.dumps(str(picked.get("title") or ""), ensure_ascii=False)),
        )
        _wait(0.8)
    return True


def _mindex_try_pick_mandatory_tableware(p_ws: LocalWsCdpClient) -> bool:
    """兼容旧调用名：委托 `ensure_mindex_required_products`。"""
    return ensure_mindex_required_products(p_ws, force_dom_check=True)


def _expand_mindex_preview_items(p_ws: LocalWsCdpClient) -> bool:
    """在 `mindex/preview` 结算页若存在「展开更多」（4+ 件商品被折叠），自动展开以完整核验所有菜品与金额。"""
    exp = _eval_obj(
        p_ws,
        r"""(() => {
            const el = document.querySelector('[class*="loadBtn_"], [class*="loadMoreP_"]')
                || Array.from(document.querySelectorAll('span, p, div, button')).find(e => (e.innerText || '').trim() === '展开更多' && e.children.length <= 2 && e.getBoundingClientRect().width > 20);
            if (!el) return JSON.stringify({ok: false});
            try { el.scrollIntoView({block: 'center'}); } catch (e) {}
            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return JSON.stringify({ok: false});
            return JSON.stringify({ok: true, x: r.x + r.width / 2, y: r.y + r.height / 2});
        })()""",
    )
    if not exp.get("ok"):
        return False
    _click_xy(p_ws, float(exp["x"]), float(exp["y"]))
    _wait(0.55)
    if "展开更多" in _body_text(p_ws):
        _eval_value(
            p_ws,
            r"""(() => {
                const el = document.querySelector('[class*="loadBtn_"], [class*="loadMoreP_"]');
                if (el) { el.click(); return true; }
                return false;
            })()""",
        )
        _wait(0.5)
    return True


def _mindex_checkout_to_preview(p_ws: LocalWsCdpClient, menu_url: str, loc_target: dict[str, Any]) -> str:
    """在餐饮门店页自动补齐必选品并点「去结算」进入 `mindex/preview`（SPA 未渲染时自动直航 `MINDEX_PREVIEW_URL` 兜底）。"""
    _setup_page(p_ws, loc_target)
    # 若前序在 mindex/menusearch 完成加购，必须回到真正的 mindex/menu（注意 mindex/menu 是 mindex/menusearch 的子串！）
    if not _is_mindex_menu_url(_page_url(p_ws)) or not _eval_obj(
        p_ws,
        "JSON.stringify({ok: Array.from(document.querySelectorAll('[class*=\"goToPreview\"]')).some(e => e.getBoundingClientRect().width > 0)})",
    ).get("ok"):
        p_ws.call("Page.navigate", {"url": menu_url})
        _wait(4.5)
        _setup_page(p_ws, loc_target)
        ready_dl = time.monotonic() + 5.0
        while time.monotonic() < ready_dl:
            _ensure_active(p_ws)
            rdy = _eval_obj(
                p_ws,
                "JSON.stringify({ok: Array.from(document.querySelectorAll('[class*=\"cartContainer\"], [class*=\"goToPreview\"]')).some(e => e.getBoundingClientRect().width > 0)})",
            )
            if rdy.get("ok"):
                break
            _wait(0.45)

    _check_gate(p_ws, "checkout")

    # 主动核验并补齐门店「必选品 / 必选餐具」，避免点击「去结算」被静默拦截
    if ensure_mindex_required_products(p_ws, force_dom_check=False):
        _wait(0.8)

    btn_info = _eval_obj(
        p_ws,
        r"""(() => {
            const btn = Array.from(document.querySelectorAll('[class*="goToPreview"]')).find(e => e.getBoundingClientRect().width > 0);
            if (!btn) {
                const visCart = Array.from(document.querySelectorAll('[class*="cartContainer"]')).find(e => e.getBoundingClientRect().width > 0)
                    || document.querySelector('[class*="cartContainer"]');
                return JSON.stringify({ok: false, reason: 'no_btn', text: ((visCart && visCart.innerText) || '').trim()});
            }
            const t = (btn.innerText || '').trim();
            if (/差.*起送|起送$/.test(t) && !t.includes('去结算')) {
                return JSON.stringify({ok: false, reason: 'below_min_order', text: t});
            }
            try { btn.scrollIntoView({block: 'center'}); } catch (e) {}
            const r = btn.getBoundingClientRect();
            return JSON.stringify({ok: true, x: r.x + r.width/2, y: r.y + r.height/2, text: t});
        })()""",
    )
    if not btn_info.get("ok"):
        if btn_info.get("reason") == "below_min_order":
            raise CartStageError("checkout", f"购物车暂未满起送价（{btn_info.get('text')}）", reason="below_min_order")
        raise CartStageError("checkout", f"餐饮门店底栏无可用「去结算」按钮（{btn_info.get('text') or '空'}）")

    _click_xy(p_ws, float(btn_info["x"]), float(btn_info["y"]))

    retried_tw = False
    retried_js_click = False
    deadline = time.monotonic() + 9.5
    while time.monotonic() < deadline:
        _wait(0.6)
        _ensure_active(p_ws)
        u = _page_url(p_ws)
        txt = _body_text(p_ws)
        _check_gate(p_ws, "preview")
        if "mindex/preview" in u and "努力加载中" not in txt and "提交订单" in txt and ("合计" in txt or "小计" in txt):
            return txt
        elapsed = 9.5 - (deadline - time.monotonic())
        if not retried_tw and _is_mindex_menu_url(u) and elapsed >= 2.0:
            retried_tw = True
            if ensure_mindex_required_products(p_ws, force_dom_check=True):
                _wait(0.8)
                _eval_value(p_ws, "document.querySelector('[class*=\"goToPreview\"]')?.click()")
                deadline = max(deadline, time.monotonic() + 7.0)
                continue
        if not retried_js_click and _is_mindex_menu_url(u) and elapsed >= 2.8:
            retried_js_click = True
            _eval_value(p_ws, "document.querySelector('[class*=\"goToPreview\"]')?.click()")

    # SPA 过渡未渲染兜底：直接导航 mindex/preview
    p_ws.call("Page.navigate", {"url": MINDEX_PREVIEW_URL})
    _wait(4.5)
    _setup_page(p_ws, loc_target)
    deadline2 = time.monotonic() + 6.0
    while time.monotonic() < deadline2:
        _ensure_active(p_ws)
        txt = _body_text(p_ws)
        _check_gate(p_ws, "preview")
        if "提交订单" in txt and ("合计" in txt or "小计" in txt):
            return txt
        _wait(0.65)
    raise CartStageError("preview", "餐饮结算预览页（mindex/preview）未能渲染出完整账单", preview_url=_page_url(p_ws))


def _select_mindex_address(p_ws: LocalWsCdpClient, token: str, addr_hint: str, loc_target: dict[str, Any]) -> dict[str, Any]:
    """在 `mindex/preview` 核验并选择收货地址（进入 `mindex/addresslist`，严格排除外卖柜并避开右侧删除按钮）。"""
    from siming.meituan_pay import parse_meituan_preview_bill

    _setup_page(p_ws, loc_target)
    body = _body_text(p_ws)
    n_token = _norm(token)
    want_cabinet = "外卖柜" in (token or "")
    cur_bill = parse_meituan_preview_bill(body)
    cur_addr = str(cur_bill.get("delivery_address") or "").strip()
    if n_token and n_token in _norm(cur_addr) and (want_cabinet or "外卖柜" not in cur_addr):
        return {"ok": True, "already_selected": True, "address": cur_addr}

    # 点击预览页顶部地址栏进入 mindex/addresslist
    opened = _click_selector(
        p_ws,
        '[class*="address_Pcx2D3"], [class*="addressDetail_"], [class*="addressDetailInfoBold_"]',
        0,
    )
    if not opened.get("ok"):
        _click_text(p_ws, "选择收货地址")

    deadline = time.monotonic() + 6.5
    items: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        _wait(0.5)
        _ensure_active(p_ws)
        raw = _eval_value(
            p_ws,
            r"""JSON.stringify(
                Array.from(document.querySelectorAll('[class*="item_"]:not([class*="outOfRangeItem_"])'))
                    .map((el, domIdx) => {
                        const r = el.getBoundingClientRect();
                        if (r.width < 220 || r.height < 34 || r.height > 150) return null;
                        const addrEl = el.querySelector('[class*="address_"]');
                        const lines = (el.innerText || '').split('\n').map(s => s.trim()).filter(Boolean);
                        const addrTxt = (addrEl ? addrEl.innerText : (lines[0] || '')).replace(/\s*删除\s*$/, '').trim();
                        return {domIdx, addr: addrTxt, full: lines.join(' ')};
                    })
                    .filter(Boolean)
            )""",
        )
        try:
            items = json.loads(raw) if isinstance(raw, str) else []
        except Exception:
            items = []
        if "addresslist" in _page_url(p_ws) and items:
            break

    if not items:
        raise CartStageError("address", "未能打开餐饮收货地址列表（mindex/addresslist）")

    ranked = sorted(
        ((_score_address_candidate(str(it.get("addr") or ""), n_token, addr_hint, want_cabinet), it) for it in items),
        key=lambda x: -x[0],
    )
    best_sc, best_it = ranked[0]
    if best_sc <= 0:
        avail = [str(it.get("addr") or "") for it in items[:8]]
        raise CartStageError("address", f"收货地址列表中未找到可用的「{token or addr_hint}」（可能超出门店配送范围）", available_addresses=avail)

    # 点击目标地址行偏左区域（x = left + min(width*0.38, 130)），绝对避开右侧「删除」按钮！
    click_pos = _eval_obj(
        p_ws,
        r"""(() => {
            const els = Array.from(document.querySelectorAll('[class*="item_"]:not([class*="outOfRangeItem_"])'));
            const el = els[IDX];
            if (!el) return JSON.stringify({ok: false});
            try { el.scrollIntoView({block: 'center'}); } catch (e) {}
            const r = el.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0) return JSON.stringify({ok: false});
            return JSON.stringify({ok: true, x: r.left + Math.min(r.width * 0.38, 130), y: r.top + r.height / 2});
        })()""".replace("IDX", str(int(best_it["domIdx"]))),
    )
    if not click_pos.get("ok"):
        raise CartStageError("address", "无法测量目标收货地址条目坐标")

    _click_xy(p_ws, float(click_pos["x"]), float(click_pos["y"]))
    deadline_back = time.monotonic() + 7.0
    while time.monotonic() < deadline_back:
        _wait(0.55)
        _ensure_active(p_ws)
        if "mindex/preview" in _page_url(p_ws) and "提交订单" in _body_text(p_ws):
            break

    if "mindex/preview" not in _page_url(p_ws):
        p_ws.call("Page.navigate", {"url": MINDEX_PREVIEW_URL})
        _wait(4.0)
        _setup_page(p_ws, loc_target)

    # 等待 0.8s 确认未被页面异步重定位覆盖
    _wait(0.8)
    after_bill = parse_meituan_preview_bill(_body_text(p_ws))
    after_addr = str(after_bill.get("delivery_address") or "").strip()
    chosen_addr = str(best_it.get("addr") or "")
    if (_norm(chosen_addr) and _norm(chosen_addr) in _norm(after_addr)) or (n_token and n_token in _norm(after_addr)):
        if not want_cabinet and "外卖柜" in after_addr:
            raise CartStageError("address", f"预览页地址仍停在外卖柜「{after_addr}」，未切到「{chosen_addr}」")
        return {"ok": True, "already_selected": False, "fallback_used": False, "address": after_addr or chosen_addr}
    raise CartStageError("address", f"选择地址后预览页显示「{after_addr}」，与目标「{chosen_addr or token}」不一致")


def _touch_tap_xy(p_ws: LocalWsCdpClient, x: float, y: float) -> None:
    try:
        p_ws.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})
    except Exception:
        pass
    pt = [{"x": round(float(x), 1), "y": round(float(y), 1), "radiusX": 6, "radiusY": 6, "force": 1.0, "id": 1}]
    p_ws.call("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": pt})
    _wait(0.05)
    p_ws.call("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def _try_apply_mindex_coupon(p_ws: LocalWsCdpClient, loc_target: dict[str, Any]) -> bool:
    """在 `mindex/preview` 统一择优勾选最优美团红包 + 商家代金券 + 配送券（防互斥涨价回滚，保底自动返回预览页绝不滞留）。"""
    from siming.meituan_coupons import apply_optimal_preview_coupons

    res = apply_optimal_preview_coupons(p_ws, store_kind="mindex", loc_target=loc_target)
    return bool(res.get("applied"))


# 兼容旧符号调用
_select_address = _select_subtrade_address
_try_apply_preview_coupon = _try_apply_subtrade_coupon


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------


def live_cart_via_trusted_input(
    db: SimingDB,
    shop: str,
    dish_name: str,
    qty: int = 1,
    *,
    search_keyword: str = "",
    location_override: str = "",
    address_hint: str = "",
    existing_target_id: str = "",
    port: int = 9222,
    spec_prefs: Optional[list[str]] = None,
    dishes: Optional[list[dict[str, Any]]] = None,
    poi_id_str: str = "",
    direct_menu_url: str = "",
) -> dict[str, Any]:
    """真实加购并停在结算预览页（自动兼容餐饮 `mindex/menu` 与商超 `sub-trade/restaurant`，支持单菜与多菜组合）。

    返回 itemized 结果；失败时返回 `ok=False` 与真实 `stage/reason`，绝不伪造成功。
    """
    from siming.meituan_pay import parse_meituan_preview_bill

    qty = max(1, int(qty or 1))
    shop = (shop or "").strip()
    dish_name = (dish_name or "").strip()
    if not shop:
        return {"ok": False, "stage": "input", "reason": "missing_shop", "error": "缺少店铺名"}

    target_dishes: list[dict[str, Any]] = []
    if dishes:
        for d in dishes:
            if isinstance(d, dict) and str(d.get("name") or "").strip():
                target_dishes.append({"name": str(d["name"]).strip(), "qty": max(1, int(d.get("qty") or 1))})
    if not target_dishes and dish_name:
        target_dishes = [{"name": dish_name, "qty": qty}]

    loc_override = (location_override or "").strip()
    loc_target = resolve_meituan_delivery_target(db, location_override=loc_override)
    if len(target_dishes) > 1:
        keyword = (search_keyword or "").strip() or shop
    else:
        keyword = (search_keyword or "").strip() or f"{_strip_branch_display(shop)} {dish_name}".strip()

    stage = "start"
    tid = ""
    menu_url = ""
    try:
        stage = "search"
        tid, sr, page_ws = _search_tab(db, keyword, loc_target, existing_target_id, port)
        if not address_hint:
            address_hint = str(sr.get("delivery_poi") or "")

        with LocalWsCdpClient(page_ws, timeout=28.0) as p_ws:
            _setup_page(p_ws, loc_target)
            url = _page_url(p_ws)
            if "searchresults" not in url and not _is_restaurant_url(url):
                stage = "search_load"
                p_ws.call(
                    "Page.navigate",
                    {"url": MEITUAN_H5_SEARCH_TMPL.format(keyword=urllib.parse.quote(keyword))},
                )
                _wait(6.5)
                _ensure_active(p_ws)
                _wait(1.2)
            _check_gate(p_ws, "search")

            # 同步十进制坐标 + deliverypoi + AVOID_AUTOGEO，并锁定期望收货地址匹配词（默认排除外卖柜）
            sync_res = sync_meituan_h5_location(p_ws, loc_target)
            addstore = sync_res.get("addstore") or []
            matched_addr = sync_res.get("matched_saved") or (_match_best_addstore_entry(addstore, loc_target) if addstore else None)
            header_addr = str(_eval_value(p_ws, "(document.querySelector('.addr_W3eGpu')||{}).innerText || ''") or "").strip()
            if matched_addr:
                addr_token = " ".join(
                    x for x in (str(matched_addr.get("poi") or "").strip(), str(matched_addr.get("address") or "").strip()) if x
                )
            else:
                addr_token = address_hint or header_addr or str(loc_target.get("match_hint") or loc_target.get("label") or "")

            stage = "shop_click"
            direct_u = (direct_menu_url or "").strip()
            if not direct_u and (poi_id_str or "").strip():
                direct_u = f"https://h5.waimai.meituan.com/waimai/mindex/menu?poi_id_str={urllib.parse.quote(poi_id_str.strip(), safe='')}"
            if direct_u and not _is_restaurant_url(_page_url(p_ws)):
                try:
                    p_ws.call("Page.navigate", {"url": direct_u})
                    _wait(4.5)
                    _setup_page(p_ws, loc_target)
                    _wait(1.0)
                    _check_gate(p_ws, "shop_click")
                except CartStageError:
                    raise
                except Exception:
                    pass
            first_target_dish = str(target_dishes[0]["name"]) if target_dishes else ""
            menu_url, poi_id, store_kind = _open_restaurant(p_ws, shop, target_dish=first_target_dish)
            if not poi_id:
                raise CartStageError("shop_click", "未能取得 poi_id_str")

            if not target_dishes:
                raise CartStageError("dish", "未指定菜品，无法自动加购")

            used_inner_search = False
            added_titles: list[str] = []
            preview_data: dict[str, Any] = {}

            if store_kind == "mindex":
                # 若导航后页面二次跳转为商超 sub-trade/restaurant，自动切回商超分支
                cur_after_open = _page_url(p_ws)
                if RESTAURANT_MARKER in cur_after_open:
                    menu_url = cur_after_open
                    store_kind = "subtrade"
            if store_kind == "mindex":
                stage = "menu_load"
                if _wait_mindex_menu_ready(p_ws, menu_url, loc_target) == "subtrade":
                    menu_url = _page_url(p_ws)
                    qs_re = urllib.parse.parse_qs(urllib.parse.urlsplit(menu_url).query)
                    poi_id = (qs_re.get("poi_id_str") or [poi_id])[0] or poi_id
                    store_kind = "subtrade"

            if store_kind == "mindex":
                _clear_mindex_cart_if_unrelated(p_ws, poi_id, target_dishes, menu_url=menu_url, loc_target=loc_target)

                stage = "qty"
                for td in target_dishes:
                    res_one, used_in = _mindex_set_dish_qty(
                        p_ws,
                        menu_url=menu_url,
                        poi_id=poi_id,
                        dish=str(td["name"]),
                        qty=int(td["qty"]),
                        spec_prefs=spec_prefs,
                    )
                    if used_in:
                        used_inner_search = True
                    t_s = str(res_one["title"])
                    added_titles.append(f"{t_s}×{td['qty']}" if int(td["qty"]) > 1 else t_s)

                stage = "checkout"
                text = _mindex_checkout_to_preview(p_ws, menu_url, loc_target)

                stage = "address"
                addr_res = _select_mindex_address(p_ws, addr_token, address_hint, loc_target)
                _wait(0.6)
                from siming.meituan_coupons import apply_optimal_preview_coupons

                coupon_opt = apply_optimal_preview_coupons(p_ws, store_kind="mindex", loc_target=loc_target)
                _expand_mindex_preview_items(p_ws)
                text = _body_text(p_ws)

                stage = "verify"
                bill = parse_meituan_preview_bill(text)
            else:
                stage = "menu_load"
                restaurant_url = _page_url(p_ws)
                p_ws.call("Page.navigate", {"url": restaurant_url})
                _wait(9.5)
                _ensure_active(p_ws)
                _wait(1.6)
                _wait_menu_ready(p_ws)

                if _prune_subtrade_unrelated_cart(p_ws, poi_id, target_dishes):
                    p_ws.call("Page.navigate", {"url": restaurant_url})
                    _wait(8.5)
                    _ensure_active(p_ws)
                    _wait(1.4)

                stage = "qty"
                rest_qs = urllib.parse.parse_qs(urllib.parse.urlsplit(restaurant_url).query)
                rest_buz_type = (rest_qs.get("buz_type") or rest_qs.get("buzType") or ["0"])[0] or "0"
                for idx_d, td in enumerate(target_dishes):
                    d_nm = str(td["name"])
                    d_q = int(td["qty"])
                    if idx_d > 0 and RESTAURANT_MARKER not in _page_url(p_ws):
                        p_ws.call("Page.navigate", {"url": restaurant_url})
                        _wait(8.5)
                        _ensure_active(p_ws)
                        _wait(1.4)
                    card_sel = CARD_SEL
                    if not _read_dish(p_ws, d_nm).get("ok"):
                        stage = "inner_search"
                        _inner_search_dish(p_ws, poi_id, d_nm, buz_type=rest_buz_type)
                        card_sel = SEARCH_CARD_SEL
                        used_inner_search = True
                    stage = "qty"
                    set_info = _set_dish_qty(p_ws, d_nm, d_q, card_sel, poi_id=poi_id)
                    t_s = str(set_info.get("title") or d_nm)
                    added_titles.append(f"{t_s}×{d_q}" if d_q > 1 else t_s)

                stage = "checkout"
                total_want_qty = sum(int(d["qty"]) for d in target_dishes)
                preview_data = _prepare_preview_data(p_ws, menu_url, total_want_qty, target_dishes)

                stage = "preview"
                preview_url = PREVIEW_URL_TMPL.format(poi_id_str=urllib.parse.quote(poi_id, safe=""))
                text = _wait_preview(p_ws, preview_url)

                stage = "address"
                addr_res = _select_subtrade_address(p_ws, addr_token, address_hint)
                _wait(1.0)
                from siming.meituan_coupons import apply_optimal_preview_coupons

                coupon_opt = apply_optimal_preview_coupons(p_ws, store_kind="subtrade", loc_target=loc_target)
                text = _body_text(p_ws)

                stage = "verify"
                bill = parse_cart_preview_bill(text)

            if not bill.get("final_payable_yuan"):
                raise CartStageError("verify", "预览页未读到明确合计金额")
            if not bill.get("items"):
                raise CartStageError("verify", "预览页未解析出商品明细")

            # 逐项校验所点全部菜品与数量均真实出现在结算预览页账单中
            bill_items = list(bill["items"])
            total_actual_qty = 0
            for td in target_dishes:
                want_nm = str(td["name"])
                want_q = int(td["qty"])
                matched_bill_items = [
                    it for it in bill_items if _dish_match_score(str(it.get("name") or ""), want_nm) >= 62.0
                ]
                if not matched_bill_items:
                    got_names = [str(it.get("name") or "") for it in bill_items]
                    raise CartStageError(
                        "verify",
                        f"预览页账单缺少所点菜品「{want_nm}」（账单实显：{'、'.join(got_names)}）",
                        bill=bill,
                    )
                got_q = sum(int(it.get("qty") or 0) for it in matched_bill_items)
                if got_q != want_q:
                    raise CartStageError(
                        "verify",
                        f"预览页「{matched_bill_items[0].get('name') or want_nm}」数量不符（期望 {want_q}，实际 {got_q}）",
                        bill=bill,
                    )
                total_actual_qty += got_q

            sold_addr = str(bill.get("delivery_address") or "")
            expected_addr_check = str(addr_res.get("address") or addr_token) if addr_res.get("fallback_used") else addr_token
            if _norm(expected_addr_check) and _norm(expected_addr_check) not in _norm(sold_addr) and _norm(expected_addr_check) not in _norm(text):
                raise CartStageError("verify", f"预览页地址不符（期望含「{expected_addr_check}」，实际「{sold_addr}」）", bill=bill)

            # 联系人手机号尾号防错人守卫：若已登记本人授权手机号且预览页明确露出其他尾号，拒绝误寄他人
            recip_masked = str(bill.get("recipient_masked") or "").strip()
            if recip_masked:
                m_tail4 = re.search(r"(\d{4})\b", recip_masked)
                if m_tail4:
                    try:
                        from siming.otp_vault import get_authorized_phone

                        auth_phone = get_authorized_phone(db) or ""
                        if len(auth_phone) >= 4 and m_tail4.group(1) != auth_phone[-4:]:
                            raise CartStageError(
                                "address",
                                f"预览页联系人尾号不符（页面显示「{recip_masked}」，与本人授权尾号不一致），为防送错人已停手",
                                bill=bill,
                            )
                    except CartStageError:
                        raise
                    except Exception:
                        pass

            # 检查是否落在「未选择(必选)」预订单时段提示
            eta_str = str(bill.get("eta_text") or "")
            if "未选择(必选)" in text and "预" not in eta_str:
                eta_str = (f"{eta_str} · 需选预约送达时间" if eta_str else "门店当前仅接受预订（需选送达时间）")
                bill["eta_text"] = eta_str

            total = float(bill["final_payable_yuan"])
            itemized = []
            items_subtotal = 0.0
            for it in bill_items:
                u_price = float(it.get("price_yuan") or 0.0)
                i_qty = int(it.get("qty") or 1)
                l_tot = float(it.get("line_total_yuan") if it.get("line_total_yuan") is not None else round(u_price * i_qty, 2))
                items_subtotal = round(items_subtotal + l_tot, 2)
                itemized.append(
                    {
                        "name": str(it.get("name") or ""),
                        "unit_price_yuan": u_price,
                        "qty": i_qty,
                        "spec": str(it.get("spec") or ""),
                        "line_total_yuan": l_tot,
                    }
                )
            packing = float(bill.get("packing_fee_yuan") or 0.0)
            delivery = float(bill.get("delivery_fee_yuan") or 0.0)
            discount = float(bill.get("discount_yuan") or 0.0)
            expected_max = round(items_subtotal + packing + delivery + 0.5, 2)
            expected_min = round(items_subtotal + packing + delivery - discount - 1.0, 2)
            total_consistent = expected_min <= total <= expected_max

            preview_result = {
                "ok": True,
                "preview_ready": True,
                "gated": False,
                "store_kind": store_kind,
                "preview_target_id": tid,
                "preview_url": str(_page_url(p_ws)),
                "menu_url": menu_url,
                "bill": bill,
                "final_payable_yuan": total,
                "delivery_address": sold_addr,
                "eta_text": eta_str,
                "coupon_text": str(bill.get("coupon_text") or ""),
                "coupon_opt": coupon_opt,
            }
            dish_summary = " + ".join(added_titles) if len(added_titles) > 1 else (added_titles[0] if added_titles else str(bill_items[0].get("name") or dish_name))
            return {
                "ok": True,
                "gated": False,
                "store_kind": store_kind,
                "added_to_cart": True,
                "shop": str(bill.get("shop") or shop),
                "dish": dish_summary,
                "added_dishes": added_titles,
                "qty": total_actual_qty,
                "cart_count": total_actual_qty,
                "preview_ready": True,
                "preview_target_id": tid,
                "preview_url": preview_result["preview_url"],
                "menu_url": menu_url,
                "poi_id_str": poi_id,
                "target_id": tid,
                "url": preview_result["preview_url"],
                "delivery_poi": sold_addr,
                "delivery_address": sold_addr,
                "address_token": addr_token,
                "address_already_selected": bool(addr_res.get("already_selected")),
                "used_inner_search": used_inner_search,
                "items": bill_items,
                "itemized": itemized,
                "items_subtotal_yuan": items_subtotal,
                "packing_fee_yuan": bill.get("packing_fee_yuan"),
                "delivery_fee_yuan": bill.get("delivery_fee_yuan"),
                "discount_yuan": bill.get("discount_yuan"),
                "final_payable_yuan": total,
                "total_consistent": total_consistent,
                "expected_total_range": [expected_min, expected_max],
                "preview_data_total": preview_data.get("total"),
                "bill": bill,
                "coupon_opt": coupon_opt,
                "preview": preview_result,
                "loc_override": loc_override,
                "delivery_poi_expected": addr_token,
            }
    except CartStageError as exc:
        reason_code = str(exc.extra.get("reason") or exc.stage)
        return {
            "ok": False,
            "gated": bool(exc.extra.get("gated")),
            "gate": exc.extra.get("gate"),
            "stage": exc.stage,
            "reason": reason_code,
            "error": str(exc),
            "target_id": tid,
            "preview_target_id": tid,
            "menu_url": menu_url,
            "loc_override": loc_override,
            **{k: v for k, v in exc.extra.items() if k not in ("gate", "gated", "reason")},
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "gated": False,
            "stage": stage,
            "reason": "cdp_error",
            "error": f"{type(exc).__name__}: {str(exc)[:200]}",
            "target_id": tid,
            "preview_target_id": tid,
            "menu_url": menu_url,
            "loc_override": loc_override,
        }
