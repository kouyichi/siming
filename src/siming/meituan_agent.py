"""美团外卖位置同步实时检索、个人口味画像融合推荐与本机 9222 持久浏览器 H5 点餐加购锁单执行器（food_butler）：
- 实时位置优先铁律：
  1. 先读取手机实时定位（GPS / Wi-Fi / 已知锚点，WGS-84 自动转 GCJ-02）同步至本机 127.0.0.1:9222 主 Chromium 的美团外卖 H5（优先匹配 `addstore` 350m 内已存收货地址，户外/陌生位置直接写坐标）；
  2. 只从美团 H5 **当前位置实时在售可配送商家**（首页推荐或实时关键词搜索）中筛选并打分，再叠加个人历史点餐偏好 + 36h 防腻 + 时段过滤（正餐不混药房/蛋糕/纯咖啡，且绝不推荐当前位置送不到的异地历史老店）；
  3. 仅当 9222 离线或测试离线模式时才退回历史账本参考，并明确向用户说明未能实时核验当前位置配送范围；
- 合规铁律：强制后台标签（background=True，零 Hyprland 焦点抢占），遇到美团登录或滑块风控验证立即停手原样保留现场交由本人处理，绝不代解绕过验证码。
"""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.parse
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from siming.approval import create_approval
from siming.cdp import (
    LocalWsCdpClient,
    detect_compliance_gate,
    has_domain_session_cookies,
    list_chromium_tabs,
    resolve_chromium_cdp_base,
)
from siming.config import load_policy
from siming.context_loc import (
    _haversine_m,
    resolve_anchor_coords_for_alias,
    resolve_current_location_context,
)
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local

MEITUAN_H5_HOME = "https://h5.waimai.meituan.com/waimai/mindex/home"
MEITUAN_H5_SEARCH_TMPL = (
    "https://h5.waimai.meituan.com/waimai/mindex/searchresults"
    "?queryType=12002&keyword={keyword}&entranceId=0&qwTypeId=11002&mode=search"
)

_NON_MEAL_MERCHANT_RE = re.compile(
    r"(先骑后付|单车|充电宝|超市|便利店?|数码|专卖店|办公|文具|等多件|小象|买菜|买药|药房|大药房|医药|快药|百货|鲜花|花店|五金|建材|打车|跑腿|会员|省钱卡|神会员|抖音App|直播|顺丰|陆运|京东快递|宠物|母婴|美妆|成人|情趣|水果捞?店?$|百果园|鲜丰水果|烟酒|酒水急送|洗护|五金日杂)",
    re.IGNORECASE,
)
_DRINK_DESSERT_RE = re.compile(
    r"(咖啡|瑞幸|luckin|库迪|星巴克|Manner|皮爷|Peet|挪瓦|NOWWA|拉瓦萨|LAVAZZA|幸运咖|肯悦咖啡|奶茶|茶饮|茶百道|霸王茶姬|沪上阿姨|喜茶|奈雪|古茗|蜜雪冰城|一点点|CoCo|茉莉奶白|爷爷不泡茶|手作酸奶|酸奶|生日蛋糕|蛋糕|味多美|好利来|甜品|烘焙|面包|冰淇淋|冰奶|糖水铺|果汁)",
    re.IGNORECASE,
)
_LATE_NIGHT_SKIP_RE = re.compile(
    r"(生日蛋糕|蛋糕|味多美|好利来|咖啡|瑞幸|luckin|库迪|星巴克|Manner|酸奶|烘焙|面包)",
    re.IGNORECASE,
)
_FOOD_PLATFORM_HINT_RE = re.compile(
    r"(美团|三快|饿了么|拉扎斯|淘宝闪购|外卖|餐饮|麦当劳|肯德基|星巴克|瑞幸|Manner|螺蛳粉|肉夹馍|饺子|盖码饭|打抛饭|食府|小吃|早点|火锅|烧烤|面馆|塔可|TACO|寿司|东北|奈雪|老乡鸡|汉堡|FIVE\s*GUYS)",
    re.IGNORECASE,
)
_CLOSED_SHOP_RE = re.compile(r"(休息中|已打烊|暂不接单|停止营业|暂停营业|今日已打烊|明日.{0,8}起送)")
_PREORDER_SHOP_RE = re.compile(r"(接受预订|预订中|预约配送|预订单)")
_RANK_BADGE_RE = re.compile(r"(好评榜|人气榜|热销榜|复购榜|必点榜|回头客榜)")
_PAGE_ERROR_RE = re.compile(r"(参数错误|错误码[:：]|页面加载失败|网络异常.*刷新)")

_HOME_RATING_RE = re.compile(r"^(\d(?:\.\d)?)分$")
_SEARCH_RATING_RE = re.compile(r"^(\d\.\d)$")
_SALES_INLINE_RE = re.compile(r"^月售\s*(\d+\+?)$")
_PER_CAPITA_RE = re.compile(r"^人均\s*[¥￥]?\s*(\d+(?:\.\d+)?)$")
_MIN_ORDER_INLINE_RE = re.compile(r"^起送\s*[¥￥]?\s*(\d+(?:\.\d+)?)$")
_DELIVERY_FEE_INLINE_RE = re.compile(r"^配送\s*(?:约)?\s*(免配送费|[¥￥]?\s*\d+(?:\.\d+)?)$")
_ETA_RE = re.compile(r"^(\d{1,3})\s*分钟$")
_DIST_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(km|m)$", re.IGNORECASE)
_PRICE_ONLY_RE = re.compile(r"^[¥￥]?\s*(\d+(?:\.\d{1,2})?)$")

_UI_NOISE_LINES = {
    "点击授权获取精确定位信息",
    "开启定位才能看到附近的商家哦~",
    "去开启",
    "搜索",
    "搜索页",
    "异国料理",
    "小吃馆",
    "家常菜",
    "晚餐优选",
    "午餐优选",
    "早餐优选",
    "夜宵优选",
    "点评高分",
    "优惠商家",
    "满减优惠",
    "品牌商家",
    "综合排序",
    "销量最高",
    "距离最近",
    "速度最快",
    "评分最高",
    "正在加载...",
    "没有更多了",
    "首页",
    "订单",
    "我的",
    "美团专送",
    "刷新试试",
    "参数错误",
}


def wgs84_to_gcj02(lat: float, lng: float) -> tuple[float, float]:
    """将 iOS / OwnTracks 上报的 WGS-84 经纬度转为中国国内地图/美团使用的 GCJ-02 火星坐标（误差 ~10m）。"""
    if not (72.004 <= lng <= 137.8347 and 0.8293 <= lat <= 55.8271):
        return round(lat, 6), round(lng, 6)
    a = 6378245.0
    ee = 0.00669342162296594323
    x, y = lng - 105.0, lat - 35.0
    dlat = (
        -100.0
        + 2.0 * x
        + 3.0 * y
        + 0.2 * y * y
        + 0.1 * x * y
        + 0.2 * math.sqrt(abs(x))
        + (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        + (20.0 * math.sin(y * math.pi) + 40.0 * math.sin(y / 3.0 * math.pi)) * 2.0 / 3.0
        + (160.0 * math.sin(y / 12.0 * math.pi) + 320.0 * math.sin(y * math.pi / 30.0)) * 2.0 / 3.0
    )
    dlng = (
        300.0
        + x
        + 2.0 * y
        + 0.1 * x * x
        + 0.1 * x * y
        + 0.1 * math.sqrt(abs(x))
        + (20.0 * math.sin(6.0 * x * math.pi) + 20.0 * math.sin(2.0 * x * math.pi)) * 2.0 / 3.0
        + (20.0 * math.sin(x * math.pi) + 40.0 * math.sin(x / 3.0 * math.pi)) * 2.0 / 3.0
        + (150.0 * math.sin(x / 12.0 * math.pi) + 300.0 * math.sin(x / 30.0 * math.pi)) * 2.0 / 3.0
    )
    radlat = lat / 180.0 * math.pi
    magic = math.sin(radlat)
    magic = 1.0 - ee * magic * magic
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1.0 - ee)) / (magic * sqrtmagic) * math.pi)
    dlng = (dlng * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return round(lat + dlat, 6), round(lng + dlng, 6)


def _normalize_coord_deg(val: Any) -> Optional[float]:
    try:
        f = float(val)
    except (TypeError, ValueError):
        return None
    if abs(f) > 1000.0:
        f = f / 1_000_000.0
    return f


def _clean_restaurant_name(raw_merchant: str) -> str:
    s = (raw_merchant or "").strip()
    s = re.sub(r"^(?:美团平台商户|美团|淘宝闪购|饿了么)\s*·\s*", "", s)
    s = re.sub(r"-美团外卖App-\d+$", "", s)
    s = re.sub(r"-美团外卖App.*$", "", s)
    s = re.sub(r"外卖订单$", "", s)
    s = re.sub(r"消费$", "", s)
    if " · " in s:
        parts = [p.strip() for p in s.split(" · ") if p.strip()]
        if len(parts) >= 2 and parts[0] != parts[1]:
            s = parts[-1]
        elif parts:
            s = parts[0]
    return s.strip()[:48]


def _normalize_shop_name(name: str) -> str:
    s = _clean_restaurant_name(name).lower()
    s = s.replace("（", "(").replace("）", ")").replace("·", "").replace("•", "").replace("●", "")
    return re.sub(r"\s+", "", s)


def _shop_brand_key(name: str) -> str:
    """提取餐厅主品牌前缀（剥离分店括号后缀，用于 36h 防腻跨分店去重与历史偏好匹配）。"""
    s = _normalize_shop_name(name)
    s = re.sub(r"\([^)]*\)$", "", s)
    s = re.split(r"[-—/|]", s)[0]
    return s.strip()


def _meal_slot_for_hour(hour: int) -> str:
    if 5 <= hour < 10:
        return "早餐"
    if 10 <= hour < 15:
        return "午餐"
    if 15 <= hour < 17:
        return "下午茶/咖啡"
    if 17 <= hour < 22:
        return "晚餐"
    return "夜宵"


def _parse_meal_hint(hint: str, now_dt: datetime) -> dict[str, Any]:
    q = (hint or "").strip()
    cur_slot = _meal_slot_for_hour(now_dt.hour)
    if "夜宵" in q or "宵夜" in q:
        cur_slot = "夜宵"
    elif "晚" in q:
        cur_slot = "晚餐"
    elif "午" in q or "中饭" in q:
        cur_slot = "午餐"
    elif "早" in q:
        cur_slot = "早餐"
    elif any(k in q for k in ("咖啡", "奶茶", "下午茶", "甜品", "饮品")):
        cur_slot = "下午茶/咖啡"

    max_budget: Optional[float] = None
    m_bud = re.search(r"(?:预算|不超过|以内)\s*[¥￥]?\s*(\d{1,3})", q) or re.search(r"(\d{2,3})\s*(?:元|块)?(?:以内|以下)", q)
    if m_bud:
        max_budget = float(m_bud.group(1))

    allow_recent_repeat = "再来一单" in q

    kw = re.sub(r"(?:预算|不超过|以内)\s*[¥￥]?\s*\d{1,3}", " ", q)
    kw = re.sub(r"\d{2,3}\s*(?:元|块)?(?:以内|以下)", " ", kw)
    kw = re.sub(
        r"(?:美团推荐|外卖推荐|推荐点|推荐一下|推荐下|推荐几个|推荐几家|帮我推荐|给我推荐|推荐|"
        r"吃点什么|吃点啥|喝点什么|喝点啥|吃什么|吃啥|喝什么|喝啥|点什么|点啥|再来一单|"
        r"不知道吃啥|不知道吃什么|不知道吃|没想好吃啥|没想好吃什么|没想好吃|不想做饭|还没吃饭|还没吃|没吃饭|肚子饿|饿了|"
        r"我要点|我想点|准备点|要点|想点|帮我点|给我点|想吃点|想吃|想喝点|想喝|来一杯|来杯|点一杯|点杯|点个|点份|点顿|点点|"
        r"今晚|今天|晚上|中午|早上|夜里|半夜|现在|马上|待会|一会|快|晚饭|晚餐|午饭|午餐|中饭|早饭|早餐|夜宵|宵夜|下午茶|正餐|外卖|美团|"
        r"附近|周边|周围|这边|这里|能吃啥|有啥吃的|有什么吃的|有啥吃|有什么吃|啥好吃的|啥好吃|有什么好吃|好吃的|吃的|喝的|饭|一点|一下|几个|几家|好呢|随便|啥)",
        " ",
        kw,
    )
    kw = re.sub(r"[，。！？、,.!?:：;；~～()（）\"'“”‘’]+", " ", kw)
    kw = re.sub(r"\b(?:我|要|想|帮我|给我|点|了|吧|呀|呢|啊|的|个|家|份|顿|来|看)\b", " ", kw)
    # 清理残余单字虚词（中文无空格边界；如「吃川菜」->「川菜」、「喝咖啡」->「咖啡」）
    kw = re.sub(r"^[我要想点帮给来和跟看吃喝]+", "", kw.strip())
    kw = re.sub(r"[了的吧呀呢啊点啥]+$", "", kw.strip()).strip()
    kw = re.sub(r"\s+", " ", kw).strip()
    if len(kw) <= 1 and kw not in ("面", "粥", "粉", "肉", "鱼", "虾", "鸡", "鸭", "茶"):
        kw = ""

    return {
        "slot": cur_slot,
        "max_budget": max_budget,
        "keyword": kw,
        "allow_recent_repeat": allow_recent_repeat,
    }


def resolve_meituan_delivery_target(
    db: SimingDB,
    loc_ctx: Optional[dict[str, Any]] = None,
    location_override: str = "",
) -> dict[str, Any]:
    """将当前手机定位（或显式地点覆盖）解析为美团 H5 可直接同步的 GCJ-02 目标坐标与可读标签。"""
    ov = (location_override or "").strip()
    if ov:
        norm_ov = {"家里": "家", "办公室": "公司", "单位": "公司"}.get(ov, ov)
        anc = resolve_anchor_coords_for_alias(db, norm_ov)
        if anc:
            gcj_lat, gcj_lng = wgs84_to_gcj02(float(anc["lat"]), float(anc["lng"]))
            return {
                "has_location": True,
                "is_fallback": False,
                "source": "override",
                "alias": str(anc["alias"]),
                "label": str(anc["label"] or anc["alias"]),
                "match_hint": str(anc["label"] or ov),
                "wgs_lat": float(anc["lat"]),
                "wgs_lng": float(anc["lng"]),
                "gcj_lat": gcj_lat,
                "gcj_lng": gcj_lng,
                "mt_lat": int(round(gcj_lat * 1_000_000)),
                "mt_lng": int(round(gcj_lng * 1_000_000)),
                "badge": f"📍 指定配送位置：**{anc['alias']}**（{anc['label']}）",
            }
        return {
            "has_location": True,
            "is_fallback": False,
            "source": "override_text",
            "alias": norm_ov,
            "label": ov,
            "match_hint": ov,
            "wgs_lat": None,
            "wgs_lng": None,
            "gcj_lat": None,
            "gcj_lng": None,
            "mt_lat": None,
            "mt_lng": None,
            "badge": f"📍 指定配送位置：**{ov}**",
        }

    ctx = loc_ctx if loc_ctx is not None else resolve_current_location_context(db)
    alias = str(ctx.get("place_alias") or "").strip()
    addr = str(ctx.get("address") or "").strip()
    lat = ctx.get("lat")
    lng = ctx.get("lng")

    is_coord_str = bool(re.fullmatch(r"-?\d+\.\d+\s*,\s*-?\d+\.\d+", addr))
    label = ("" if is_coord_str else addr) or alias or "当前位置"

    if lat is not None and lng is not None:
        wgs_lat, wgs_lng = float(lat), float(lng)
        gcj_lat, gcj_lng = wgs84_to_gcj02(wgs_lat, wgs_lng)
        return {
            "has_location": True,
            "is_fallback": False,
            "source": str(ctx.get("source_kind") or "gps"),
            "alias": alias,
            "label": label,
            "match_hint": addr if not is_coord_str else alias,
            "wgs_lat": wgs_lat,
            "wgs_lng": wgs_lng,
            "gcj_lat": gcj_lat,
            "gcj_lng": gcj_lng,
            "mt_lat": int(round(gcj_lat * 1_000_000)),
            "mt_lng": int(round(gcj_lng * 1_000_000)),
            "badge": str(ctx.get("badge") or f"📍 当前定位：{label}"),
        }

    if alias:
        anc = resolve_anchor_coords_for_alias(db, alias)
        if anc:
            wgs_lat, wgs_lng = float(anc["lat"]), float(anc["lng"])
            gcj_lat, gcj_lng = wgs84_to_gcj02(wgs_lat, wgs_lng)
            return {
                "has_location": True,
                "is_fallback": False,
                "source": str(ctx.get("source_kind") or "wifi"),
                "alias": alias,
                "label": addr or str(anc["label"] or alias),
                "match_hint": addr or str(anc["label"] or alias),
                "wgs_lat": wgs_lat,
                "wgs_lng": wgs_lng,
                "gcj_lat": gcj_lat,
                "gcj_lng": gcj_lng,
                "mt_lat": int(round(gcj_lat * 1_000_000)),
                "mt_lng": int(round(gcj_lng * 1_000_000)),
                "badge": str(ctx.get("badge") or f"📍 场景定位：{alias}"),
            }
        return {
            "has_location": True,
            "is_fallback": False,
            "source": str(ctx.get("source_kind") or "wifi"),
            "alias": alias,
            "label": label,
            "match_hint": addr or alias,
            "wgs_lat": None,
            "wgs_lng": None,
            "gcj_lat": None,
            "gcj_lng": None,
            "mt_lat": None,
            "mt_lng": None,
            "badge": str(ctx.get("badge") or f"📍 场景定位：{alias}"),
        }

    # 无新鲜手机定位：尝试回退默认「家」坐标，并显式标记 is_fallback
    home_anc = resolve_anchor_coords_for_alias(db, "家")
    if home_anc:
        wgs_lat, wgs_lng = float(home_anc["lat"]), float(home_anc["lng"])
        gcj_lat, gcj_lng = wgs84_to_gcj02(wgs_lat, wgs_lng)
        return {
            "has_location": False,
            "is_fallback": True,
            "source": "fallback_home",
            "alias": "家",
            "label": str(home_anc["label"] or "家"),
            "match_hint": str(home_anc["label"] or "家"),
            "wgs_lat": wgs_lat,
            "wgs_lng": wgs_lng,
            "gcj_lat": gcj_lat,
            "gcj_lng": gcj_lng,
            "mt_lat": int(round(gcj_lat * 1_000_000)),
            "mt_lng": int(round(gcj_lng * 1_000_000)),
            "badge": f"📍 未收到新鲜手机定位，暂按默认「家（{home_anc['label']}）」查询",
        }
    return {
        "has_location": False,
        "is_fallback": True,
        "source": "none",
        "alias": "",
        "label": "",
        "match_hint": "",
        "wgs_lat": None,
        "wgs_lng": None,
        "gcj_lat": None,
        "gcj_lng": None,
        "mt_lat": None,
        "mt_lng": None,
        "badge": "",
    }


def _match_best_addstore_entry(
    addstore: list[dict[str, Any]],
    loc_target: dict[str, Any],
    max_dist_m: float = 350.0,
) -> Optional[dict[str, Any]]:
    """在美团 `localStorage.addstore` 已存收货地址中寻找与当前位置最匹配的收货点（默认排除外卖柜）。"""
    if not addstore:
        return None
    hint = str(loc_target.get("match_hint") or loc_target.get("label") or "").strip()
    want_cabinet = "外卖柜" in hint

    gcj_lat = loc_target.get("gcj_lat")
    gcj_lng = loc_target.get("gcj_lng")
    if gcj_lat is not None and gcj_lng is not None:
        near: list[tuple[int, float, dict[str, Any]]] = []
        for entry in addstore:
            if not isinstance(entry, dict):
                continue
            a_lat = _normalize_coord_deg(entry.get("lat"))
            a_lng = _normalize_coord_deg(entry.get("lng"))
            if a_lat is None or a_lng is None:
                continue
            dist = _haversine_m(float(gcj_lat), float(gcj_lng), a_lat, a_lng)
            if dist <= max_dist_m:
                poi_s = str(entry.get("poi") or "")
                # 350m 同片区内始终优先真实门牌地址，避免被楼下美团外卖柜抢占
                cabinet_penalty = 0 if want_cabinet else (1 if "外卖柜" in poi_s else 0)
                near.append((cabinet_penalty, dist, entry))
        if near:
            near.sort(key=lambda x: (x[0], x[1] > 120.0, x[1]))
            return near[0][2]
        return None

    if hint and hint not in ("当前位置", "位置"):
        text_hits: list[tuple[int, dict[str, Any]]] = []
        for entry in addstore:
            if not isinstance(entry, dict):
                continue
            poi_s = str(entry.get("poi") or "")
            addr_s = str(entry.get("address") or "")
            if hint in poi_s or hint in addr_s or (poi_s and poi_s in hint):
                cabinet_penalty = 0 if want_cabinet else (1 if "外卖柜" in poi_s else 0)
                text_hits.append((cabinet_penalty, entry))
        if text_hits:
            text_hits.sort(key=lambda x: x[0])
            return text_hits[0][1]
    return None


def parse_meituan_home_shops(text: str) -> list[dict[str, Any]]:
    """解析美团外卖 H5 首页 (`mindex/home`) `document.body.innerText` 中的实时在售附近商家列表。"""
    raw_lines = [ln.strip() for ln in (text or "").replace("\r", "").split("\n")]
    lines = [ln for ln in raw_lines if ln]
    if not lines:
        return []

    # 切掉页面底部导航栏
    for idx in range(len(lines) - 1, max(-1, len(lines) - 8), -1):
        if lines[idx] in ("正在加载...", "没有更多了", "首页"):
            if idx + 1 < len(lines) and lines[idx + 1] in ("首页", "订单"):
                lines = lines[:idx]
                break
            if lines[idx] == "正在加载...":
                lines = lines[:idx]
                break

    starts: list[int] = []
    n = len(lines)
    for i in range(n - 1):
        cand = lines[i]
        if cand in _UI_NOISE_LINES or len(cand) < 2 or len(cand) > 60:
            continue
        if cand.startswith(("“", "\"", "起送", "配送", "人均", "月售", "满", "领", "返", "¥", "￥")):
            continue
        if _HOME_RATING_RE.match(cand) or _ETA_RE.match(cand) or _DIST_RE.match(cand):
            continue
        nxt = lines[i + 1]
        is_shop_head = bool(_HOME_RATING_RE.match(nxt) or nxt == "暂无评分" or _SALES_INLINE_RE.match(nxt))
        if not is_shop_head:
            continue
        window = "\n".join(lines[i + 1 : min(n, i + 9)])
        if any(k in window for k in ("起送", "配送", "分钟", "休息中", "已打烊", "暂不接单")):
            starts.append(i)

    shops: list[dict[str, Any]] = []
    seen_norm: set[str] = set()
    for pos, s_idx in enumerate(starts):
        e_idx = starts[pos + 1] if pos + 1 < len(starts) else n
        block = lines[s_idx:e_idx]
        shop_name = block[0]
        norm = _normalize_shop_name(shop_name)
        if not norm or norm in seen_norm:
            continue
        seen_norm.add(norm)

        rating: Optional[float] = None
        monthly_sales = ""
        per_capita: Optional[float] = None
        min_order: Optional[float] = None
        delivery_fee = ""
        eta_min: Optional[int] = None
        distance_km: Optional[float] = None
        distance_str = ""
        is_frequent = False
        is_favorite = False
        is_high_rated = False
        rank_badge = ""
        review_quote = ""
        is_closed = False
        is_preorder = False
        promo_lines: list[str] = []

        for ln in block[1:]:
            if ln in ("首页", "订单", "我的", "正在加载...", "没有更多了"):
                break
            m = _HOME_RATING_RE.match(ln)
            if m and rating is None:
                rating = float(m.group(1))
                continue
            m = _SALES_INLINE_RE.match(ln)
            if m and not monthly_sales:
                monthly_sales = m.group(1)
                continue
            m = _PER_CAPITA_RE.match(ln)
            if m and per_capita is None:
                per_capita = float(m.group(1))
                continue
            m = _MIN_ORDER_INLINE_RE.match(ln)
            if m and min_order is None:
                min_order = float(m.group(1))
                continue
            m = _DELIVERY_FEE_INLINE_RE.match(ln)
            if m and not delivery_fee:
                delivery_fee = m.group(1).strip()
                continue
            m = _ETA_RE.match(ln)
            if m and eta_min is None:
                eta_min = int(m.group(1))
                continue
            m = _DIST_RE.match(ln)
            if m and distance_km is None:
                val = float(m.group(1))
                unit = m.group(2).lower()
                distance_km = round(val if unit == "km" else val / 1000.0, 3)
                distance_str = ln
                continue
            if ln == "常吃的店":
                is_frequent = True
            elif ln == "已收藏":
                is_favorite = True
            elif "大众点评高分" in ln:
                is_high_rated = True
            if _CLOSED_SHOP_RE.search(ln):
                is_closed = True
            if _PREORDER_SHOP_RE.search(ln):
                is_preorder = True
            if not rank_badge and _RANK_BADGE_RE.search(ln):
                rank_badge = ln
            if not review_quote and ln.startswith("“") and ln.endswith("”"):
                review_quote = ln
            if re.search(r"(?:领\d+(?:\.\d+)?元券|返\d+(?:\.\d+)?元券|\d+减\d+)", ln):
                promo_lines.append(ln)

        shops.append(
            {
                "shop": shop_name,
                "rating": rating,
                "monthly_sales": monthly_sales,
                "per_capita": per_capita,
                "min_order": min_order,
                "delivery_fee": delivery_fee,
                "eta_min": eta_min,
                "distance_km": distance_km,
                "distance_str": distance_str,
                "is_frequent": is_frequent,
                "is_favorite": is_favorite,
                "is_high_rated": is_high_rated,
                "rank_badge": rank_badge,
                "review_quote": review_quote,
                "is_closed": is_closed,
                "is_preorder": is_preorder,
                "promo_lines": promo_lines,
                "dishes": [],
            }
        )
    return shops


def parse_meituan_search_shops(text: str) -> list[dict[str, Any]]:
    """解析美团外卖 H5 搜索结果页 (`mindex/searchresults`) 的实时门店与匹配菜品/价格。"""
    raw_lines = [ln.strip() for ln in (text or "").replace("\r", "").split("\n")]
    lines = [ln for ln in raw_lines if ln]
    if not lines:
        return []

    starts: list[int] = []
    n = len(lines)
    for i in range(n - 1):
        cand = lines[i]
        if cand in _UI_NOISE_LINES or len(cand) < 2 or len(cand) > 64:
            continue
        if cand in ("¥", "￥", "月售", "起送", "配送", "票", "支持自取", "放心吃", "准时宝", "极速退款"):
            continue
        if _SEARCH_RATING_RE.match(cand) or _PRICE_ONLY_RE.match(cand) or _ETA_RE.match(cand) or _DIST_RE.match(cand):
            continue
        # 结构锚点：店名后紧跟 `4.5` + `月售` 或直接紧跟 `月售`
        has_rating_then_sales = (
            i + 2 < n
            and bool(_SEARCH_RATING_RE.match(lines[i + 1]) or _HOME_RATING_RE.match(lines[i + 1]))
            and lines[i + 2].startswith("月售")
        )
        has_direct_sales = lines[i + 1].startswith("月售") and not bool(
            _SEARCH_RATING_RE.match(cand) or _HOME_RATING_RE.match(cand)
        )
        if not (has_rating_then_sales or has_direct_sales):
            continue
        window = "\n".join(lines[i + 1 : min(n, i + 14)])
        if any(k in window for k in ("起送", "配送", "分钟", "休息中", "已打烊")):
            starts.append(i)

    shops: list[dict[str, Any]] = []
    seen_norm: set[str] = set()
    for pos, s_idx in enumerate(starts):
        e_idx = starts[pos + 1] if pos + 1 < len(starts) else n
        block = lines[s_idx:e_idx]
        shop_name = block[0]
        norm = _normalize_shop_name(shop_name)
        if not norm or norm in seen_norm:
            continue
        seen_norm.add(norm)

        rating: Optional[float] = None
        monthly_sales = ""
        min_order: Optional[float] = None
        delivery_fee = ""
        eta_min: Optional[int] = None
        distance_km: Optional[float] = None
        distance_str = ""
        is_frequent = False
        is_favorite = False
        is_high_rated = False
        rank_badge = ""
        is_closed = False
        is_preorder = False
        dishes: list[dict[str, Any]] = []

        k = 1
        nb = len(block)
        meta_done = False
        while k < nb:
            ln = block[k]
            if ln in ("首页", "订单", "我的", "正在加载...", "没有更多了"):
                break
            if rating is None and (_SEARCH_RATING_RE.match(ln) or _HOME_RATING_RE.match(ln)):
                m_r = _SEARCH_RATING_RE.match(ln) or _HOME_RATING_RE.match(ln)
                assert m_r is not None
                rating = float(m_r.group(1))
                k += 1
                continue
            if ln == "月售" and k + 1 < nb:
                monthly_sales = block[k + 1]
                k += 2
                continue
            m_si = _SALES_INLINE_RE.match(ln)
            if m_si and not monthly_sales:
                monthly_sales = m_si.group(1)
                k += 1
                continue
            if ln == "起送":
                j = k + 1
                if j < nb and block[j] in ("¥", "￥"):
                    j += 1
                if j < nb and _PRICE_ONLY_RE.match(block[j]):
                    min_order = float(_PRICE_ONLY_RE.match(block[j]).group(1))  # type: ignore[union-attr]
                    k = j + 1
                    continue
            m_mo = _MIN_ORDER_INLINE_RE.match(ln)
            if m_mo and min_order is None:
                min_order = float(m_mo.group(1))
                k += 1
                continue
            if ln == "配送":
                j = k + 1
                if j < nb and block[j] in ("¥", "￥"):
                    j += 1
                if j < nb and (_PRICE_ONLY_RE.match(block[j]) or "免" in block[j]):
                    delivery_fee = f"¥{block[j]}" if _PRICE_ONLY_RE.match(block[j]) else block[j]
                    k = j + 1
                    continue
            m_df = _DELIVERY_FEE_INLINE_RE.match(ln)
            if m_df and not delivery_fee:
                delivery_fee = m_df.group(1).strip()
                k += 1
                continue
            m_eta = _ETA_RE.match(ln)
            if m_eta and eta_min is None:
                eta_min = int(m_eta.group(1))
                k += 1
                continue
            m_dist = _DIST_RE.match(ln)
            if m_dist and distance_km is None:
                val = float(m_dist.group(1))
                unit = m_dist.group(2).lower()
                distance_km = round(val if unit == "km" else val / 1000.0, 3)
                distance_str = ln
                meta_done = True
                k += 1
                continue

            if ln == "常吃的店":
                is_frequent = True
            elif ln == "已收藏":
                is_favorite = True
            elif "大众点评高分" in ln:
                is_high_rated = True
            if _CLOSED_SHOP_RE.search(ln):
                is_closed = True
            if _PREORDER_SHOP_RE.search(ln):
                is_preorder = True
            if not rank_badge and _RANK_BADGE_RE.search(ln):
                rank_badge = ln

            # 解析店铺卡片下展示的菜品 + 价格（形如 `苹果C美式` / `¥` / `24`）
            if meta_done and k + 2 < nb and block[k + 1] in ("¥", "￥") and _PRICE_ONLY_RE.match(block[k + 2]):
                if not re.match(r"^(?:起送|配送|¥|￥|\d+(?:\.\d+)?)$", ln):
                    price_val = float(_PRICE_ONLY_RE.match(block[k + 2]).group(1))  # type: ignore[union-attr]
                    dishes.append({"name": ln[:42], "price": price_val})
                    k += 3
                    # 跳过紧跟其后的划线原价 (`¥` + `48`)
                    if k + 1 < nb and block[k] in ("¥", "￥") and _PRICE_ONLY_RE.match(block[k + 1]):
                        k += 2
                    continue
            k += 1

        shops.append(
            {
                "shop": shop_name,
                "rating": rating,
                "monthly_sales": monthly_sales,
                "per_capita": None,
                "min_order": min_order,
                "delivery_fee": delivery_fee,
                "eta_min": eta_min,
                "distance_km": distance_km,
                "distance_str": distance_str,
                "is_frequent": is_frequent,
                "is_favorite": is_favorite,
                "is_high_rated": is_high_rated,
                "rank_badge": rank_badge,
                "review_quote": "",
                "is_closed": is_closed,
                "is_preorder": is_preorder,
                "dishes": dishes[:4],
            }
        )
    return shops


def _setup_mobile_h5_session(p_ws: LocalWsCdpClient) -> None:
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


def _eval_json(p_ws: LocalWsCdpClient, expr: str) -> dict[str, Any]:
    res = p_ws.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    val = (res.get("result") or {}).get("value")
    if isinstance(val, str) and val.startswith("{"):
        try:
            return json.loads(val)
        except Exception:
            return {}
    return {}


def _wait_for_meituan_page_ready(
    p_ws: LocalWsCdpClient,
    is_search: bool,
    max_wait_sec: float = 6.5,
) -> dict[str, Any]:
    """轮询等待美团 H5 门店列表渲染完成，遇到风控/登录门禁或报错立即提前返回。"""
    deadline = time.monotonic() + max_wait_sec
    last_snap: dict[str, Any] = {}
    while time.monotonic() < deadline:
        snap = _eval_json(
            p_ws,
            """JSON.stringify({
                url: location.href,
                title: document.title || '',
                ready: document.readyState || '',
                headerAddr: (document.querySelector('.addr_W3eGpu')?.innerText || '').trim(),
                text: (document.body && document.body.innerText) || ''
            })""",
        )
        last_snap = snap
        u = str(snap.get("url") or "")
        t = str(snap.get("title") or "")
        txt = str(snap.get("text") or "")
        gate = detect_compliance_gate(u, t, txt)
        if gate is not None:
            return {"gated": True, "gate": gate, "snap": snap}
        if _PAGE_ERROR_RE.search(txt) and txt.count("起送") == 0:
            return {"gated": False, "page_error": True, "snap": snap}
        shop_signals = txt.count("起送")
        if is_search:
            if shop_signals >= 3 or "暂无相关" in txt or "没有找到" in txt or "换个关键词" in txt:
                return {"gated": False, "snap": snap}
        else:
            if shop_signals >= 5:
                return {"gated": False, "snap": snap}
        time.sleep(0.45)
    return {"gated": False, "snap": last_snap}


def set_cdp_geolocation_for_target(p_ws: LocalWsCdpClient, loc_target: Optional[dict[str, Any]]) -> None:
    """在当前 CDP session 上锁定目标 GCJ-02 经纬度，防止美团 H5 调 navigator.geolocation 把地址拉回桌面所在位置。"""
    if not loc_target:
        return
    g_lat = _normalize_coord_deg(loc_target.get("gcj_lat"))
    g_lng = _normalize_coord_deg(loc_target.get("gcj_lng"))
    if g_lat is not None and g_lng is not None:
        try:
            p_ws.call(
                "Emulation.setGeolocationOverride",
                {"latitude": round(float(g_lat), 6), "longitude": round(float(g_lng), 6), "accuracy": 25.0},
            )
        except Exception:
            pass


def sync_meituan_h5_location(
    p_ws: LocalWsCdpClient,
    loc_target: Optional[dict[str, Any]],
    *,
    init_addstore_raw: Optional[str] = None,
    init_picked_raw: Optional[str] = None,
    init_delivery_raw: Optional[str] = None,
) -> dict[str, Any]:
    """在已打开的美团 H5 标签页中同步十进制坐标与已存收货地址（同步 pickedpoi / geopoi / deliverypoi / param.selectedAddr / AVOID_AUTOGEO）。"""
    loc_t = loc_target or {}
    set_cdp_geolocation_for_target(p_ws, loc_t)

    if init_addstore_raw is None or init_picked_raw is None or init_delivery_raw is None:
        st_meta = _eval_json(
            p_ws,
            """JSON.stringify({
                pickedpoi: localStorage.getItem('pickedpoi') || '',
                deliverypoi: localStorage.getItem('deliverypoi') || '',
                addstore: localStorage.getItem('addstore') || ''
            })""",
        )
        addstore_raw = str(st_meta.get("addstore") or "") if init_addstore_raw is None else init_addstore_raw
        picked_raw = str(st_meta.get("pickedpoi") or "") if init_picked_raw is None else init_picked_raw
        delivery_raw = str(st_meta.get("deliverypoi") or "") if init_delivery_raw is None else init_delivery_raw
    else:
        addstore_raw = init_addstore_raw
        picked_raw = init_picked_raw
        delivery_raw = init_delivery_raw

    addstore_list: list[dict[str, Any]] = []
    try:
        parsed_add = json.loads(addstore_raw or "[]")
        if isinstance(parsed_add, list):
            addstore_list = parsed_add
    except Exception:
        addstore_list = []

    cur_picked: dict[str, Any] = {}
    try:
        parsed_pick = json.loads(picked_raw or "{}")
        if isinstance(parsed_pick, dict):
            cur_picked = parsed_pick
    except Exception:
        cur_picked = {}

    cur_delivery: dict[str, Any] = {}
    try:
        parsed_del = json.loads(delivery_raw or "{}")
        if isinstance(parsed_del, dict):
            cur_delivery = parsed_del
    except Exception:
        cur_delivery = {}

    desired_poi: Optional[dict[str, Any]] = None
    delivery_obj: Optional[dict[str, Any]] = None
    snapped_saved = False
    matched_saved = _match_best_addstore_entry(addstore_list, loc_t) if addstore_list else None
    if matched_saved is not None:
        m_lat_deg = _normalize_coord_deg(matched_saved.get("lat"))
        m_lng_deg = _normalize_coord_deg(matched_saved.get("lng"))
        if m_lat_deg is not None and m_lng_deg is not None:
            lat_f = round(float(m_lat_deg), 6)
            lng_f = round(float(m_lng_deg), 6)
            poi_s = str(matched_saved.get("poi") or loc_t.get("label") or "当前位置")
            addr_s = str(matched_saved.get("address") or "")
            desired_poi = {
                "lat": f"{lat_f:.6f}",
                "lng": f"{lng_f:.6f}",
                "geotype": 2,
                "poi": poi_s,
                "address": addr_s,
            }
            if matched_saved.get("addressId") is not None:
                delivery_obj = {
                    "lat": lat_f,
                    "lng": lng_f,
                    "geotype": 2,
                    "addressId": matched_saved["addressId"],
                    "poi": poi_s,
                    "address": addr_s,
                    "gender": int(matched_saved.get("gender") or 1),
                    "name": str(matched_saved.get("name") or ""),
                    "phone": str(matched_saved.get("phone") or ""),
                    "addressType": int(matched_saved.get("addressType") or 0),
                }
            snapped_saved = True
    elif loc_t.get("gcj_lat") is not None and loc_t.get("gcj_lng") is not None:
        lat_f = round(float(loc_t["gcj_lat"]), 6)
        lng_f = round(float(loc_t["gcj_lng"]), 6)
        desired_poi = {
            "lat": f"{lat_f:.6f}",
            "lng": f"{lng_f:.6f}",
            "geotype": 2,
            "poi": str(loc_t.get("label") or "当前位置"),
            "address": "",
        }
    elif loc_t.get("mt_lat") is not None and loc_t.get("mt_lng") is not None:
        m_lat_deg = _normalize_coord_deg(loc_t["mt_lat"])
        m_lng_deg = _normalize_coord_deg(loc_t["mt_lng"])
        if m_lat_deg is not None and m_lng_deg is not None:
            lat_f = round(float(m_lat_deg), 6)
            lng_f = round(float(m_lng_deg), 6)
            desired_poi = {
                "lat": f"{lat_f:.6f}",
                "lng": f"{lng_f:.6f}",
                "geotype": 2,
                "poi": str(loc_t.get("label") or "当前位置"),
                "address": "",
            }

    need_reload = False
    if desired_poi is not None:
        des_lat = _normalize_coord_deg(desired_poi.get("lat"))
        des_lng = _normalize_coord_deg(desired_poi.get("lng"))
        cur_lat = _normalize_coord_deg(cur_picked.get("lat"))
        cur_lng = _normalize_coord_deg(cur_picked.get("lng"))
        del_lat = _normalize_coord_deg(cur_delivery.get("lat"))
        del_lng = _normalize_coord_deg(cur_delivery.get("lng"))

        # 检查之前是否写入了错误的百万倍整数坐标，或坐标漂移 > 150m，或 deliverypoi 的 addressId 不一致
        raw_pick_lat = cur_picked.get("lat")
        was_microdeg = False
        try:
            if raw_pick_lat is not None and abs(float(raw_pick_lat)) > 1000.0:
                was_microdeg = True
        except (TypeError, ValueError):
            pass

        if was_microdeg or cur_lat is None or cur_lng is None or des_lat is None or des_lng is None:
            need_reload = True
        else:
            drift_m = _haversine_m(cur_lat, cur_lng, des_lat, des_lng)
            if drift_m > 150.0 or (snapped_saved and str(cur_picked.get("poi") or "") != desired_poi["poi"]):
                need_reload = True
            elif delivery_obj is not None and str(cur_delivery.get("addressId") or "") != str(delivery_obj["addressId"]):
                need_reload = True
            elif del_lat is not None and del_lng is not None and _haversine_m(del_lat, del_lng, des_lat, des_lng) > 200.0:
                need_reload = True

        # 始终写入防自动重定位标记与标准化坐标（确保 mindex/preview 不被旧 deliverypoi 拉回公司/外卖柜）
        geo_obj = {
            "lat": round(float(des_lat), 6) if des_lat is not None else 0.0,
            "lng": round(float(des_lng), 6) if des_lng is not None else 0.0,
            "geotype": 2,
            "poi": desired_poi["poi"],
        }
        payload_js = json.dumps(
            {
                "poi": desired_poi,
                "geo": geo_obj,
                "deliv": delivery_obj,
            },
            ensure_ascii=False,
        )
        try:
            p_ws.call(
                "Runtime.evaluate",
                {
                    "expression": f"""(() => {{
                        const d = {payload_js};
                        localStorage.setItem('pickedpoi', JSON.stringify(d.poi));
                        localStorage.setItem('geopoi', JSON.stringify(d.geo));
                        if (d.deliv) {{
                            localStorage.setItem('deliverypoi', JSON.stringify(d.deliv));
                        }} else {{
                            localStorage.removeItem('deliverypoi');
                        }}
                        let param = {{}};
                        try {{ param = JSON.parse(localStorage.getItem('param') || '{{}}'); }} catch (e) {{ param = {{}}; }}
                        param.pickedPOI = d.poi;
                        param.initialLat = d.poi.lat;
                        param.initialLng = d.poi.lng;
                        param.addressName = d.poi.poi;
                        param.address = d.poi.address || '';
                        param.geoPOI = d.geo;
                        param.lat = d.geo.lat;
                        param.lng = d.geo.lng;
                        param.geotype = 2;
                        if (d.deliv) {{
                            param.selectedAddr = d.deliv;
                        }} else {{
                            delete param.selectedAddr;
                        }}
                        localStorage.setItem('param', JSON.stringify(param));
                        try {{
                            sessionStorage.setItem('AVOID_AUTOGEO', 'true');
                            sessionStorage.setItem('RESERVE_DELIVERY_POI', 'true');
                        }} catch (e) {{}}
                    }})()""",
                },
            )
        except Exception:
            pass

    return {
        "need_reload": need_reload,
        "snapped_saved": snapped_saved,
        "matched_saved": matched_saved,
        "desired_poi": desired_poi,
        "delivery_obj": delivery_obj,
        "addstore": addstore_list,
    }


def fetch_live_meituan_candidates(
    db: SimingDB,
    loc_target: dict[str, Any],
    keyword: str = "",
    port: int = 9222,
    scroll_rounds: int = 1,
    close_when_clean: bool = True,
) -> dict[str, Any]:
    """在主 Chromium 9222 后台标签中将美团 H5 切换至当前位置并实时抓取可配送商家（严格遵守零抢焦点与验证码合规红线）。"""
    if os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return {"ok": False, "reason": "offline", "error": "离线测试模式"}

    base = resolve_chromium_cdp_base(port=port)
    if not base or not base.get("ws_browser"):
        return {"ok": False, "reason": "cdp_unavailable", "error": "本机主 Chromium (9222) 未就绪"}

    clean_kw = (keyword or "").strip()
    is_search = bool(clean_kw)
    target_url = (
        MEITUAN_H5_SEARCH_TMPL.format(keyword=urllib.parse.quote(clean_kw))
        if is_search
        else MEITUAN_H5_HOME
    )

    tid = ""
    keep_open = not close_when_clean
    try:
        with LocalWsCdpClient(base["ws_browser"], timeout=6.0) as b_ws:
            tid = str(b_ws.call("Target.createTarget", {"url": "about:blank", "background": True}).get("targetId") or "")
        if not tid:
            return {"ok": False, "reason": "create_target_failed", "error": "无法创建后台标签页"}

        page_ws = base["ws_browser"].split("/devtools/")[0] + f"/devtools/page/{tid}"
        with LocalWsCdpClient(page_ws, timeout=20.0) as p_ws:
            _setup_mobile_h5_session(p_ws)
            set_cdp_geolocation_for_target(p_ws, loc_target)

            # 1) 先进入美团源站（若需搜词可先落首页读/同步定位，再跳搜索页；同城同定位可直接复用）
            p_ws.call("Page.navigate", {"url": target_url})
            time.sleep(1.6)

            # 等 origin 切换到 meituan.com 并检测是否命中登录/滑块门禁
            init_meta = _eval_json(
                p_ws,
                """JSON.stringify({
                    url: location.href,
                    title: document.title || '',
                    text: (document.body && document.body.innerText || '').slice(0, 1800),
                    pickedpoi: localStorage.getItem('pickedpoi') || '',
                    deliverypoi: localStorage.getItem('deliverypoi') || '',
                    addstore: localStorage.getItem('addstore') || ''
                })""",
            )
            gate = detect_compliance_gate(
                str(init_meta.get("url") or target_url),
                str(init_meta.get("title") or ""),
                str(init_meta.get("text") or ""),
            )
            if gate is not None:
                keep_open = True
                return {
                    "ok": False,
                    "gated": True,
                    "gate": gate,
                    "target_id": tid,
                    "kept_tab_open": True,
                }

            # 2) 统一同步十进制坐标与已存地址（含 pickedpoi / geopoi / deliverypoi / param.selectedAddr / AVOID_AUTOGEO）
            sync_info = sync_meituan_h5_location(
                p_ws,
                loc_target,
                init_addstore_raw=str(init_meta.get("addstore") or ""),
                init_picked_raw=str(init_meta.get("pickedpoi") or ""),
                init_delivery_raw=str(init_meta.get("deliverypoi") or ""),
            )
            desired_poi = sync_info.get("desired_poi")
            snapped_saved = bool(sync_info.get("snapped_saved"))
            need_reload = bool(sync_info.get("need_reload"))

            if need_reload and desired_poi is not None:
                p_ws.call("Page.navigate", {"url": target_url})
                time.sleep(1.5)

            ready_res = _wait_for_meituan_page_ready(p_ws, is_search=is_search, max_wait_sec=6.5)
            if ready_res.get("gated"):
                keep_open = True
                return {
                    "ok": False,
                    "gated": True,
                    "gate": ready_res["gate"],
                    "target_id": tid,
                    "kept_tab_open": True,
                }
            if ready_res.get("page_error"):
                err_txt = str((ready_res.get("snap") or {}).get("text") or "")[:140]
                return {"ok": False, "reason": "page_error", "error": f"美团页面报错：{err_txt}"}

            # 3) 适度滚动加载更多附近门店
            for _ in range(max(0, int(scroll_rounds))):
                p_ws.call(
                    "Input.dispatchMouseEvent",
                    {"type": "mouseWheel", "x": 200, "y": 500, "deltaX": 0, "deltaY": 2400},
                )
                time.sleep(0.9)

            final_meta = _eval_json(
                p_ws,
                """JSON.stringify({
                    url: location.href,
                    title: document.title || '',
                    headerAddr: (document.querySelector('.addr_W3eGpu')?.innerText || '').trim(),
                    pickedpoi: localStorage.getItem('pickedpoi') || '',
                    text: (document.body && document.body.innerText) || ''
                })""",
            )
            final_text = str(final_meta.get("text") or "")
            gate2 = detect_compliance_gate(
                str(final_meta.get("url") or target_url),
                str(final_meta.get("title") or ""),
                final_text,
            )
            if gate2 is not None:
                keep_open = True
                return {
                    "ok": False,
                    "gated": True,
                    "gate": gate2,
                    "target_id": tid,
                    "kept_tab_open": True,
                }
            if _PAGE_ERROR_RE.search(final_text) and final_text.count("起送") == 0:
                return {"ok": False, "reason": "page_error", "error": f"美团页面返回错误：{final_text[:120]}"}

            final_picked: dict[str, Any] = {}
            try:
                final_picked = json.loads(str(final_meta.get("pickedpoi") or "{}"))
            except Exception:
                final_picked = {}

            delivery_poi = (
                str((desired_poi or {}).get("poi") or "").strip()
                if snapped_saved
                else (
                    str(final_meta.get("headerAddr") or "").strip()
                    or str(final_picked.get("poi") or "").strip()
                    or str((desired_poi or {}).get("poi") or loc_target.get("label") or "")
                )
            )
            delivery_addr = str((desired_poi or {}).get("address") or final_picked.get("address") or "").strip()
            shops = parse_meituan_search_shops(final_text) if is_search else parse_meituan_home_shops(final_text)
            top_lines = [ln.strip() for ln in final_text.splitlines() if ln.strip()][:16]

            return {
                "ok": True,
                "gated": False,
                "mode": "search" if is_search else "home",
                "keyword": clean_kw,
                "url": str(final_meta.get("url") or target_url),
                "title": str(final_meta.get("title") or ""),
                "delivery_poi": delivery_poi,
                "delivery_address": delivery_addr,
                "snapped_saved": snapped_saved,
                "location_switched": need_reload,
                "shops": shops,
                "top_lines": top_lines,
                "target_id": tid if keep_open else "",
            }
    except Exception as exc:
        return {"ok": False, "reason": "cdp_error", "error": str(exc)[:220]}
    finally:
        if tid and not keep_open:
            try:
                with LocalWsCdpClient(base["ws_browser"], timeout=4.0) as b_ws:
                    b_ws.call("Target.closeTarget", {"targetId": tid})
            except Exception:
                pass


def build_user_dining_profile(db: SimingDB) -> list[dict[str, Any]]:
    """从账本真实外卖消费记录 + 美团已采集订单构建个人常点餐厅画像（自动去重并过滤超市/单车/药房噪音）。"""
    db.init_db()
    conn = db.connect()
    try:
        ledger_rows = conn.execute(
            """
            SELECT merchant, amount_cents, occurred_at
            FROM ledger_txns
            WHERE kind = '消费' AND currency = 'CNY' AND status != 'VOID_DUPLICATE'
              AND amount_cents >= 600 AND amount_cents <= 50000
            ORDER BY occurred_at DESC
            """
        ).fetchall()
        order_rows = conn.execute(
            """
            SELECT shop, ordered_at, MAX(order_amount_cents) AS amount_cents, MAX(order_status) AS order_status
            FROM shop_order_items
            WHERE platform = 'meituan'
              AND order_amount_cents >= 600 AND order_amount_cents <= 50000
            GROUP BY shop, ordered_at
            ORDER BY ordered_at DESC
            """
        ).fetchall()
    finally:
        conn.close()

    by_shop: dict[str, dict[str, Any]] = {}
    seen_order_keys: set[tuple[str, str]] = set()

    def _record(shop_raw: str, amount_cents: int, occ: str, from_orders_table: bool = False) -> None:
        if not from_orders_table and not _FOOD_PLATFORM_HINT_RE.search(shop_raw):
            return
        if _NON_MEAL_MERCHANT_RE.search(shop_raw):
            return
        shop = _clean_restaurant_name(shop_raw)
        if not shop or _NON_MEAL_MERCHANT_RE.search(shop):
            return

        hour = 19
        day_hour_bucket = occ[:13]
        try:
            dt = datetime.fromisoformat(occ.replace("Z", "+00:00")).astimezone(SHANGHAI_TZ)
            hour = dt.hour
            day_hour_bucket = dt.strftime("%Y-%m-%dT%H")
        except Exception:
            pass

        dedup_k = (_shop_brand_key(shop) or _normalize_shop_name(shop), day_hour_bucket)
        if from_orders_table and dedup_k in seen_order_keys:
            return
        seen_order_keys.add(dedup_k)

        slot = _meal_slot_for_hour(hour)
        norm_k = _normalize_shop_name(shop)
        rec = by_shop.setdefault(
            norm_k,
            {
                "shop": shop,
                "norm": norm_k,
                "brand": _shop_brand_key(shop),
                "count": 0,
                "total_cents": 0,
                "last_at": occ,
                "slots": defaultdict(int),
            },
        )
        rec["count"] += 1
        rec["total_cents"] += int(amount_cents)
        if occ > rec["last_at"]:
            rec["last_at"] = occ
            rec["shop"] = shop
        rec["slots"][slot] += 1

    for r in ledger_rows:
        _record(str(r["merchant"] or ""), int(r["amount_cents"]), str(r["occurred_at"] or ""), from_orders_table=False)

    for r in order_rows:
        status_s = str(r["order_status"] or "")
        if any(k in status_s for k in ("取消", "已退款", "退款成功", "全额退款")):
            continue
        _record(str(r["shop"] or ""), int(r["amount_cents"]), str(r["ordered_at"] or ""), from_orders_table=True)

    profile: list[dict[str, Any]] = []
    for rec in by_shop.values():
        avg_yuan = round((rec["total_cents"] / max(1, rec["count"])) / 100.0, 1)
        top_slot = max(rec["slots"].items(), key=lambda kv: kv[1])[0] if rec["slots"] else "正餐"
        profile.append(
            {
                "shop": rec["shop"],
                "norm": rec["norm"],
                "brand": rec["brand"],
                "count": rec["count"],
                "avg_yuan": avg_yuan,
                "last_at": rec["last_at"],
                "top_slot": top_slot,
                "slots": dict(rec["slots"]),
            }
        )
    profile.sort(key=lambda x: (x["count"], x["last_at"]), reverse=True)
    return profile


def _collect_recent_eaten(profile: list[dict[str, Any]], now_dt: datetime, window_hours: float = 36.0) -> tuple[set[str], set[str], list[str]]:
    recent_norms: set[str] = set()
    recent_brands: set[str] = set()
    recent_names: list[str] = []
    for item in profile:
        try:
            last_dt = datetime.fromisoformat(item["last_at"].replace("Z", "+00:00")).astimezone(SHANGHAI_TZ)
            hours_ago = max(0.0, (now_dt - last_dt).total_seconds() / 3600.0)
        except Exception:
            continue
        if hours_ago <= window_hours:
            recent_norms.add(item["norm"])
            if len(item["brand"]) >= 2:
                recent_brands.add(item["brand"])
            if item["shop"] not in recent_names:
                recent_names.append(item["shop"])
    return recent_norms, recent_brands, recent_names


def _match_profile_for_live_shop(
    shop_name: str,
    by_norm: dict[str, dict[str, Any]],
    by_brand: dict[str, dict[str, Any]],
) -> Optional[dict[str, Any]]:
    norm = _normalize_shop_name(shop_name)
    if norm in by_norm:
        return by_norm[norm]
    brand = _shop_brand_key(shop_name)
    if len(brand) >= 2 and brand in by_brand:
        return by_brand[brand]
    return None


def check_meituan_cdp_status(cdp_port: int = 9222, db: Optional[SimingDB] = None) -> dict[str, Any]:
    """只读检查本机主 Chromium (自动排除 aTrust 抢占) 是否在线及是否具备美团 H5 登录态（零开标签、零焦点抢占）。"""
    base_info = resolve_chromium_cdp_base(port=cdp_port)
    if not base_info:
        return {"cdp_online": False, "has_meituan_tab": False, "has_meituan_session": False}
    tabs = list_chromium_tabs(port=cdp_port)
    has_mt = any("meituan.com" in str(t.get("url") or "").lower() for t in tabs)
    has_session = has_domain_session_cookies(
        "meituan.com",
        cookie_names=("token", "token2", "userTicket", "mt_c_token"),
        port=cdp_port,
    )
    if not has_session and db is not None:
        try:
            conn = db.connect()
            try:
                row = conn.execute(
                    "SELECT auth_state, last_ok_at FROM sources WHERE id='meituan_orders'"
                ).fetchone()
            finally:
                conn.close()
            if row and str(row["auth_state"] or "").upper() == "OK" and row["last_ok_at"]:
                age_h = (now_local() - datetime.fromisoformat(str(row["last_ok_at"]))).total_seconds() / 3600.0
                if age_h <= 48.0:
                    has_session = True
        except Exception:
            pass
    return {
        "cdp_online": True,
        "has_meituan_tab": has_mt,
        "has_meituan_session": has_session or has_mt,
        "http_base": base_info["http_base"],
    }


def _rank_live_meituan_shops(
    live_shops: list[dict[str, Any]],
    profile: list[dict[str, Any]],
    now_dt: datetime,
    cur_slot: str,
    kw: str,
    max_budget: Optional[float],
    allow_recent_repeat: bool,
    top_n: int = 5,
    account_coupons: Optional[dict[str, Any]] = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    """在当前位置实时可配送门店集合内，结合个人历史偏好、36h 防腻、评分/榜单与配送速度产出均衡推荐。"""
    recent_norms, recent_brands, _ = _collect_recent_eaten(profile, now_dt, window_hours=36.0)
    by_norm = {p["norm"]: p for p in profile}
    by_brand: dict[str, dict[str, Any]] = {}
    for p in profile:
        if len(p["brand"]) >= 2 and p["brand"] not in by_brand:
            by_brand[p["brand"]] = p

    skipped_recent: list[str] = []
    skipped_brands: set[str] = set()
    candidates: list[dict[str, Any]] = []
    kw_wants_drink = bool(kw and _DRINK_DESSERT_RE.search(kw))
    kw_wants_non_meal = bool(kw and _NON_MEAL_MERCHANT_RE.search(kw))

    for sh in live_shops:
        shop_name = str(sh.get("shop") or "").strip()
        if not shop_name:
            continue
        if sh.get("is_closed"):
            continue
        if not kw_wants_non_meal and _NON_MEAL_MERCHANT_RE.search(shop_name):
            continue
        # 时段品类过滤（未显式搜咖啡/甜品时，正餐过滤纯咖啡/生日蛋糕/酸奶，夜宵过滤蛋糕/咖啡）
        if not kw and cur_slot in ("午餐", "晚餐") and _DRINK_DESSERT_RE.search(shop_name):
            continue
        if not kw and cur_slot == "夜宵" and _LATE_NIGHT_SKIP_RE.search(shop_name):
            continue
        if not kw and cur_slot == "下午茶/咖啡" and not _DRINK_DESSERT_RE.search(shop_name):
            continue
        if kw and not kw_wants_drink and cur_slot in ("午餐", "晚餐") and _DRINK_DESSERT_RE.search(shop_name):
            # 搜正餐词时避免带出纯饮品无关推荐
            if kw.lower() not in shop_name.lower():
                continue

        norm = _normalize_shop_name(shop_name)
        brand = _shop_brand_key(shop_name)
        hist = _match_profile_for_live_shop(shop_name, by_norm, by_brand)

        # 36h 防腻过滤（精确店名或同品牌分店）
        is_recent = (norm in recent_norms) or (len(brand) >= 2 and brand in recent_brands)
        if is_recent and not allow_recent_repeat:
            disp_key = brand or norm
            if disp_key not in skipped_brands:
                skipped_brands.add(disp_key)
                skipped_recent.append(hist["shop"] if hist is not None else shop_name)
            continue

        est_price = sh.get("per_capita")
        if est_price is None and hist is not None:
            est_price = hist["avg_yuan"]
        if est_price is None:
            est_price = sh.get("min_order")

        if max_budget is not None and est_price is not None and float(est_price) > max_budget + 8.0:
            continue

        # 综合评分：个人亲和度（历史订单/美团常吃/已收藏）+ 实时评分/榜单 + 配送时效
        rating = float(sh["rating"]) if sh.get("rating") is not None else 4.3
        eta_min = sh.get("eta_min")
        dist_km = sh.get("distance_km")

        score = (rating - 4.0) * 3.2
        is_familiar = False
        if hist is not None:
            is_familiar = True
            slot_bonus = 2.5 if cur_slot in (hist.get("slots") or {}) else 0.0
            score += min(int(hist["count"]), 8) * 1.1 + slot_bonus
        if sh.get("is_frequent"):
            is_familiar = True
            score += 3.8
        if sh.get("is_favorite"):
            is_familiar = True
            score += 2.2
        if sh.get("rank_badge"):
            score += 1.6
        if sh.get("is_high_rated"):
            score += 0.9
        if eta_min is not None:
            if int(eta_min) <= 30:
                score += 1.2
            elif int(eta_min) <= 40:
                score += 0.5
            elif int(eta_min) >= 52:
                score -= 1.0
        if dist_km is not None:
            if float(dist_km) <= 2.0:
                score += 0.7
            elif float(dist_km) > 5.0:
                score -= 0.6
        if sh.get("is_preorder"):
            score -= 2.5

        # 券后性价比微加权：命中大额神券/商家券/满减且券后实付更优的门店获得适度省钱加分（不破坏熟店与评分主序）
        if est_price is not None and float(est_price) > 0 and (account_coupons or sh.get("promo_lines")):
            try:
                from siming.meituan_speculate import compute_coupon_pricing, parse_store_promos

                c_promos = parse_store_promos("\n".join(sh.get("promo_lines") or []))
                cp_r = compute_coupon_pricing(
                    shop_name=shop_name,
                    subtotal=float(est_price),
                    store_promos=c_promos,
                    account_coupons=account_coupons,
                    now_dt=now_dt,
                )
                save_y = float(cp_r.get("total_discount") or 0.0)
                if save_y > 0:
                    save_ratio = save_y / max(1.0, float(est_price))
                    score += min(1.5, save_ratio * 4.0)
                if float(cp_r.get("store_voucher_off") or 0.0) > 0:
                    score += 0.6
            except Exception:
                pass

        candidates.append(
            {
                "shop": shop_name,
                "brand": brand or norm,
                "score": round(score, 2),
                "is_familiar": is_familiar,
                "live_verified": True,
                "rating": sh.get("rating"),
                "monthly_sales": sh.get("monthly_sales") or "",
                "per_capita": sh.get("per_capita"),
                "min_order": sh.get("min_order"),
                "delivery_fee": sh.get("delivery_fee") or "",
                "eta_min": eta_min,
                "distance_str": sh.get("distance_str") or "",
                "distance_km": dist_km,
                "is_frequent": bool(sh.get("is_frequent")),
                "is_favorite": bool(sh.get("is_favorite")),
                "is_high_rated": bool(sh.get("is_high_rated")),
                "rank_badge": sh.get("rank_badge") or "",
                "promo_lines": list(sh.get("promo_lines") or []),
                "dishes": sh.get("dishes") or [],
                "avg_yuan": float(est_price) if est_price is not None else 38.0,
                "hist_count": int(hist["count"]) if hist is not None else 0,
                "hist_last_at": str(hist["last_at"]) if hist is not None else "",
                "hist_avg_yuan": float(hist["avg_yuan"]) if hist is not None else None,
            }
        )

    candidates.sort(key=lambda x: (x["score"], x.get("rating") or 0.0, -(x.get("eta_min") or 99)), reverse=True)

    # 同品牌只留最优一家（避免两家南京大牌档/两家眉州东坡霸榜）
    deduped: list[dict[str, Any]] = []
    seen_brands: set[str] = set()
    for c in candidates:
        bk = c["brand"]
        if bk in seen_brands:
            continue
        seen_brands.add(bk)
        deduped.append(c)

    # 均衡组合：最多 3 家「当前可送的常吃/收藏熟店」+ 至少 2 家「附近高分新发现」（不足则互相补足）
    familiars = [c for c in deduped if c["is_familiar"]]
    fresh = [c for c in deduped if not c["is_familiar"]]

    max_fav = min(3, top_n - 1) if fresh and top_n >= 4 else top_n
    chosen: list[dict[str, Any]] = []
    chosen.extend(familiars[:max_fav])
    need_more = top_n - len(chosen)
    chosen.extend(fresh[:need_more])
    if len(chosen) < top_n:
        used_names = {c["shop"] for c in chosen}
        for c in deduped:
            if c["shop"] not in used_names:
                chosen.append(c)
                used_names.add(c["shop"])
                if len(chosen) >= top_n:
                    break

    return chosen, skipped_recent


def recommend_meituan_meals(
    db: SimingDB,
    hint: str = "",
    check_cdp: bool = True,
    live_fetch_fn: Optional[Callable[[dict[str, Any], str], dict[str, Any]]] = None,
    location_override: str = "",
    speculate: bool = True,
) -> dict[str, Any]:
    """实时位置驱动的美团外卖智能推荐：
    - 优先把手机实时位置同步到本机 9222 美团 H5，只推荐**当前位置实时在售可配送**的门店；
    - 融合个人历史点餐偏好、36h 防腻、时段过滤与附近高分发现；
    - 若遇风控/登录门禁则合规保留标签交接；若 9222 离线则显式提示退回历史订单参考。
    """
    now = now_local()
    parsed = _parse_meal_hint(hint, now)
    cur_slot = parsed["slot"]
    max_budget: Optional[float] = parsed["max_budget"]
    kw = parsed["keyword"]
    allow_recent_repeat = parsed["allow_recent_repeat"]

    profile = build_user_dining_profile(db)
    loc_ctx = resolve_current_location_context(db)
    loc_target = resolve_meituan_delivery_target(db, loc_ctx=loc_ctx, location_override=location_override)

    # 1) 优先按当前位置实时读取美团 H5 可配送商家
    live_res: dict[str, Any] = {"ok": False, "reason": "skipped"}
    if live_fetch_fn is not None:
        search_kw = kw if kw else ("咖啡 奶茶" if cur_slot == "下午茶/咖啡" else "")
        live_res = live_fetch_fn(loc_target, search_kw)
    elif check_cdp:
        search_kw = kw if kw else ("咖啡 奶茶" if cur_slot == "下午茶/咖啡" else "")
        live_res = fetch_live_meituan_candidates(
            db,
            loc_target=loc_target,
            keyword=search_kw,
            scroll_rounds=1,
        )
        # 若用户给了偏泛化筛选词但搜索结果为空，兜底再拉一次首页附近商家
        if live_res.get("ok") and search_kw and not live_res.get("shops"):
            fallback_home = fetch_live_meituan_candidates(db, loc_target=loc_target, keyword="", scroll_rounds=1)
            if fallback_home.get("ok") and fallback_home.get("shops"):
                live_res = fallback_home

    # 合规红线：遇登录/滑块风控门禁时原样保留标签并诚实停手，绝不拿异地历史订单充当实时可配送推荐
    if live_res.get("gated"):
        gate = live_res.get("gate") or {}
        db.record_human_use("meituan_agent", "recommend_gated", ref_id=cur_slot)
        return {
            "ok": False,
            "gated": True,
            "gate": gate,
            "slot": cur_slot,
            "live_verified": False,
            "picks": [],
            "recent_skipped": [],
            "reply": (
                f"🛡️ **美团 H5 实时检索需要你配合确认一下**（当前时段：**{cur_slot}**）\n"
                f"• 原因：{gate.get('reason') or '需要登录或安全验证'}\n"
                "• 司命已按合规红线原样保留桌面主 Chromium 后台标签页（绝不绕过验证码，也不用未经核验的异地历史订单乱推荐）。\n"
                "• 请切到桌面 Chromium 的美团标签完成登录/滑块，完成后再发一句「晚饭推荐」即可按你当前位置实时看附近能吃啥。"
            ),
        }

    # 2) 实时美团成功路径：严格只从当前位置可配送的实时商家中推荐！
    if live_res.get("ok") and live_res.get("shops"):
        live_shops = list(live_res["shops"])
        account_coupons: Optional[dict[str, Any]] = None
        coupons_brief = ""
        try:
            from siming.meituan_speculate import (
                fetch_meituan_account_coupons,
                format_account_coupons_brief,
                load_cached_account_coupons,
            )

            account_coupons = load_cached_account_coupons(db)
            if account_coupons is None and check_cdp and live_fetch_fn is None:
                fetched_c = fetch_meituan_account_coupons(db, force_refresh=False)
                if fetched_c.get("ok"):
                    account_coupons = fetched_c
            coupons_brief = format_account_coupons_brief(account_coupons)
        except Exception:
            account_coupons = None
            coupons_brief = ""

        picks, recent_eaten_shops = _rank_live_meituan_shops(
            live_shops=live_shops,
            profile=profile,
            now_dt=now,
            cur_slot=cur_slot,
            kw=kw,
            max_budget=max_budget,
            allow_recent_repeat=allow_recent_repeat,
            top_n=5,
            account_coupons=account_coupons,
        )
        delivery_poi = str(live_res.get("delivery_poi") or loc_target.get("label") or "当前位置")
        delivery_addr = str(live_res.get("delivery_address") or "").strip()
        poi_disp = f"{delivery_poi} {delivery_addr}".strip() if delivery_addr and delivery_addr not in delivery_poi else delivery_poi

        spec_batch_id = ""
        spec_spawned = False
        spec_previews: dict[int, str] = {}
        if picks:
            from siming.meituan_speculate import (
                compute_coupon_pricing,
                create_speculative_batch,
                parse_store_promos,
                spawn_speculative_preplan_background,
            )

            db.set_meta(
                "last_meituan_picks",
                json.dumps(
                    [
                        {
                            "shop": p["shop"],
                            "avg_yuan": p["avg_yuan"],
                            "live_verified": True,
                            "delivery_poi": delivery_poi,
                            "poi_id_str": str(p.get("poi_id_str") or ""),
                            "menu_url": str(p.get("menu_url") or ""),
                        }
                        for p in picks
                    ],
                    ensure_ascii=False,
                ),
            )
            spec_batch = create_speculative_batch(
                db,
                picks=picks,
                delivery_poi=poi_disp,
                cur_slot=cur_slot,
                max_budget=max_budget,
                loc_target=loc_target,
            )
            spec_batch_id = str(spec_batch.get("batch_id") or "")
            for ent in spec_batch.get("entries") or []:
                if ent.get("history_preview"):
                    spec_previews[int(ent["index"])] = str(ent["history_preview"])
            if speculate and live_fetch_fn is None:
                spec_spawned = spawn_speculative_preplan_background(db, spec_batch_id)

        db.record_human_use("meituan_agent", "recommend_live", ref_id=cur_slot)

        lines = [
            f"🍱 美团外卖实时推荐（当前时段：**{cur_slot}**"
            + (f" · 筛选：{kw}" if kw else "")
            + (f" · 预算≤¥{int(max_budget)}" if max_budget is not None else "")
            + "）"
        ]
        loc_note = f"（已从手机实时定位同步 · {loc_target['alias']}）" if loc_target.get("alias") and not loc_target.get("is_fallback") else (
            "（已从手机实时 GPS 同步）" if loc_target.get("has_location") else "（手机定位暂无新鲜更新，按当前地址查询）"
        )
        lines.append(f"• 📍 美团配送定位：**{poi_disp}**{loc_note}")
        lines.append(f"• 🟢 已实时拉取该位置可配送在售商家 {len(live_shops)} 家（排除打烊/非餐饮与异地不可送门店）")
        if coupons_brief:
            lines.append(f"• 🎫 账户可用外卖神券：`{coupons_brief}`")
        if recent_eaten_shops:
            lines.append(f"• 已避开近 36h 刚吃过的：{'、'.join(recent_eaten_shops[:3])}（想复购可说「再来一单」）")

        if picks:
            for idx, p in enumerate(picks, start=1):
                meta_parts: list[str] = []
                if p.get("rating") is not None:
                    meta_parts.append(f"{p['rating']:.1f}分")

                # 计算券后参考价（基于门店首页满减/领券 + 账户外卖神券）
                base_meal_yuan = float(p["per_capita"]) if p.get("per_capita") is not None else float(p.get("avg_yuan") or 0.0)
                m_df = re.search(r"(\d+(?:\.\d+)?)", str(p.get("delivery_fee") or ""))
                deliv_num = float(m_df.group(1)) if m_df else 0.0
                card_promos = parse_store_promos("\n".join(p.get("promo_lines") or []))
                cp_quick = (
                    compute_coupon_pricing(
                        shop_name=str(p["shop"]),
                        subtotal=base_meal_yuan,
                        delivery_fee=deliv_num,
                        store_promos=card_promos,
                        account_coupons=account_coupons,
                    )
                    if (base_meal_yuan > 0 and (account_coupons or card_promos.get("manjian") or card_promos.get("claimable_coupons")))
                    else None
                )
                if cp_quick is not None:
                    p["coupon_pricing_est"] = cp_quick

                if p.get("per_capita") is not None:
                    if cp_quick and cp_quick["total_discount"] > 0:
                        est_no_deliv = max(0.0, round(base_meal_yuan - cp_quick["total_discount"], 1))
                        meta_parts.append(f"人均¥{base_meal_yuan:g}→🎫券后约¥{est_no_deliv:g}(省¥{cp_quick['total_discount']:g})")
                    else:
                        meta_parts.append(f"人均¥{float(p['per_capita']):.0f}")
                elif p.get("min_order") is not None:
                    meta_parts.append(f"起送¥{float(p['min_order']):.0f}")
                if p.get("eta_min") is not None:
                    meta_parts.append(f"{p['eta_min']}分钟")
                if p.get("distance_str"):
                    meta_parts.append(str(p["distance_str"]))

                tags: list[str] = []
                if p.get("hist_count", 0) > 0:
                    last_md = p["hist_last_at"][5:10] if len(p.get("hist_last_at", "")) >= 10 else ""
                    tags.append(f"常吃·点过{p['hist_count']}次" + (f"·上次{last_md}" if last_md else ""))
                elif p.get("is_frequent"):
                    tags.append("美团常吃")
                if p.get("is_favorite"):
                    tags.append("已收藏")
                if p.get("rank_badge"):
                    tags.append(str(p["rank_badge"]))
                elif not p.get("is_familiar"):
                    tags.append("附近高分新选择")
                if cp_quick and cp_quick.get("claimable_notes"):
                    tags.append(cp_quick["claimable_notes"][0])
                if p.get("dishes"):
                    d0 = p["dishes"][0]
                    tags.append(f"推荐:{d0['name']} ¥{d0['price']:.0f}")

                meta_str = " · ".join(meta_parts) if meta_parts else f"约¥{p['avg_yuan']:.0f}"
                tag_str = f"（{' · '.join(tags[:3])}）" if tags else ""
                lines.append(f"{idx}. **{p['shop']}** — {meta_str}{tag_str}")
                if idx in spec_previews:
                    lines.append(f"   ↳ 常点菜参考：{spec_previews[idx]}")
            if spec_batch_id:
                lines.append(
                    f"⚡ 后台已并行启动这 {len(picks)} 家的**投机菜品推演与券后价核算**（实时拉菜单、匹配神券/满减、备直达加购操作）。"
                )
                lines.append(
                    "👉 回 `看看方案` 看各家推演好的具体菜品、原价与券后实付；回 `第1家` 看确认卡；或直接回 `确认第1家` / `就点第1家` 立刻自动加购！"
                )
            else:
                lines.append("👉 想直接点哪家可发：`点外卖 第1家` 或 `点外卖 <店名> <菜品>`。")
        else:
            lines.append("• 当前筛选/预算条件下暂无匹配在售门店，可放宽预算或换个关键词再试。")

        return {
            "ok": True,
            "slot": cur_slot,
            "live_verified": True,
            "delivery_poi": delivery_poi,
            "delivery_address": delivery_addr,
            "live_shop_count": len(live_shops),
            "account_coupons_brief": coupons_brief,
            "speculative_batch_id": spec_batch_id,
            "speculative_spawned": spec_spawned,
            "picks": picks,
            "recent_skipped": recent_eaten_shops,
            "cdp": {"cdp_online": True, "has_meituan_tab": True},
            "reply": "\n".join(lines),
        }

    # 3) 离线 / CDP 不可用降级路径（保留单元测试兼容，并在真实回复中诚实标注未做位置核验）
    recent_norms, recent_brands, _ = _collect_recent_eaten(profile, now, window_hours=36.0)
    recent_eaten_shops: list[str] = []
    scored: list[tuple[float, dict[str, Any]]] = []
    for item in profile:
        if not kw and cur_slot in ("午餐", "晚餐") and _DRINK_DESSERT_RE.search(item["shop"]):
            continue
        if max_budget is not None and item["avg_yuan"] > max_budget + 8:
            continue
        if kw and kw.lower() not in item["shop"].lower():
            continue

        is_recent = (item["norm"] in recent_norms) or (len(item["brand"]) >= 2 and item["brand"] in recent_brands)
        if is_recent and not allow_recent_repeat:
            recent_eaten_shops.append(item["shop"])
            continue

        slot_bonus = 3.0 if cur_slot in item["slots"] else 0.0
        score = min(item["count"], 8) * 1.5 + slot_bonus
        scored.append((score, item))

    scored.sort(key=lambda x: (x[0], x[1]["last_at"]), reverse=True)
    picks = [{**x[1], "live_verified": False} for x in scored[:4]]
    if not picks and profile:
        picks = [{**p, "live_verified": False} for p in profile[:3]]

    if picks:
        db.set_meta(
            "last_meituan_picks",
            json.dumps([{"shop": p["shop"], "avg_yuan": p["avg_yuan"], "live_verified": False} for p in picks], ensure_ascii=False),
        )

    cdp = check_meituan_cdp_status(db=db) if check_cdp else {"cdp_online": False, "has_meituan_tab": False, "has_meituan_session": False}
    db.record_human_use("meituan_agent", "recommend_fallback", ref_id=cur_slot)

    lines = [f"🍱 美团/外卖推荐（当前时段：**{cur_slot}**" + (f" · 筛选：{kw}" if kw else "") + "）"]
    if loc_ctx.get("has_fresh"):
        lines.append(f"• {loc_ctx['badge']}")
    err_reason = str(live_res.get("error") or live_res.get("reason") or "未连接本机 9222 浏览器")
    lines.append(f"• ⚠️ **未能实时核验美团当前位置可配送商家**（原因：{err_reason}），以下仅按历史订单参考，可能包含当前位置不可配送的门店！")
    if recent_eaten_shops:
        lines.append(f"• 已自动避开近 36h 刚吃过的：{'、'.join(recent_eaten_shops[:2])}（想复购可说「再来一单」）")

    if picks:
        for idx, p in enumerate(picks, start=1):
            lines.append(
                f"{idx}. **{p['shop']}** — 历史点过 {p['count']} 次 · 均价约 ¥{p['avg_yuan']:.0f}（常在{p['top_slot']}点 · 上次 {p['last_at'][:10]}）"
            )
        lines.append("👉 想直接点哪家可发：`点外卖 第1家` 或 `点外卖 <店名> <菜品>`。")
    else:
        lines.append("• 当前筛选条件下暂无匹配历史常点店铺，可直接说想吃的品类（如 `点外卖 麻辣烫 微辣`）。")

    if not cdp["cdp_online"]:
        lines.append(f"💡 开启实时定位搜店与代加购：保持桌面主 Chromium (CDP 9222) 开启并登录一次 {MEITUAN_H5_HOME}。")

    return {
        "ok": True,
        "slot": cur_slot,
        "live_verified": False,
        "fallback_reason": err_reason,
        "picks": picks,
        "recent_skipped": recent_eaten_shops,
        "cdp": cdp,
        "reply": "\n".join(lines),
    }


_ORDER_SPEC_RULES: tuple[tuple[str, str, bool], ...] = (
    (r"超大杯", "超大杯", True),
    (r"特大杯", "特大杯", True),
    (r"大杯", "大杯", True),
    (r"中杯", "中杯", True),
    (r"不另外加糖|不加糖|无糖", "不另外加糖", True),
    (r"少少甜|三分糖|微甜", "少少甜", True),
    (r"少甜|半糖|五分糖", "少甜", True),
    (r"标准甜|全糖", "标准甜", True),
    (r"加单份浓缩|加浓", "加单份浓缩", True),
    (r"无气泡", "无气泡", True),
    (r"少冰", "少冰", True),
    (r"去冰", "去冰", True),
    (r"全冰", "冰", False),
    (r"常温|热饮", "热", True),
    (r"不辣|免辣", "不辣", True),
    (r"微辣", "微辣", True),
    (r"中辣", "中辣", True),
    (r"特辣|重辣", "特辣", True),
    (r"不要香菜|免香菜", "不要香菜", True),
    (r"不要葱|免葱", "不要葱", True),
)

_GENERIC_ORDER_CATEGORY_WORDS = {
    "咖啡",
    "美式",
    "拿铁",
    "奶茶",
    "果茶",
    "外卖",
    "汉堡",
    "炸鸡",
    "披萨",
    "烧烤",
    "火锅",
    "冒菜",
    "麻辣烫",
    "米线",
    "拉面",
    "盖饭",
    "炒饭",
    "酸奶",
    "蛋糕",
    "甜品",
}


def _strip_branch_display(shop_name: str) -> str:
    s = re.sub(r"[（(][^）)]{1,24}[）)]$", "", (shop_name or "").strip()).strip()
    return s or (shop_name or "").strip()


def _extract_order_spec_prefs(text: str) -> tuple[str, list[str]]:
    """提取规格词（杯型/冰热/糖度/辣度等），在不破坏核心菜名（如「冰美式」）的前提下清理检索词。"""
    s = (text or "").strip()
    if not s:
        return "", []
    prefs: list[str] = []
    for pat, label, strip_ok in _ORDER_SPEC_RULES:
        if re.search(pat, s):
            if label not in prefs:
                prefs.append(label)
            if strip_ok:
                candidate = re.sub(pat, " ", s).strip()
                candidate = re.sub(r"\s+", " ", candidate)
                if len(candidate.replace(" ", "")) >= 2:
                    s = candidate

    if "冰" in s and "少冰" not in prefs and "去冰" not in prefs:
        if "冰" not in prefs:
            prefs.append("冰")
        stripped = re.sub(r"(?:^|\s)冰(?=\S{3,})", " ", s).strip()
        if stripped and len(stripped.replace(" ", "")) >= 3:
            s = stripped
    elif "热" in s and "热" not in prefs:
        prefs.append("热")
        stripped = re.sub(r"(?:^|\s)热(?=\S{3,})", " ", s).strip()
        if stripped and len(stripped.replace(" ", "")) >= 3:
            s = stripped

    return re.sub(r"\s+", " ", s).strip(), prefs


def _extract_order_qty(order_request: str) -> tuple[int, str]:
    """从「店名 菜品 2」这种请求尾部提取数量，返回 (qty, 去掉数量后的请求)。

    只认独立数字词（可带 x/× 前缀或 份/件/个/杯 后缀），避免误伤「600g」「7号楼」「第1家」。
    """
    s = (order_request or "").strip()
    m = re.search(r"(?:^|\s)(?:[xX×*]\s*)?(\d{1,2})\s*(?:份|件|个|杯)?\s*$", s)
    if not m:
        return 1, s
    qty = int(m.group(1))
    if not (1 <= qty <= 10):
        return 1, s
    cleaned = s[: m.start()].strip()
    if len(cleaned.replace(" ", "")) < 2:
        return 1, s
    return qty, cleaned


_SPEC_UNIT_TOKEN_RE = re.compile(
    r"^(?:\d+(?:\.\d+)?\s*(?:g|kg|ml|l|克|千克|斤|两|寸|cm|pcs|片|串|颗|粒|只|根)|(?:\d+\s*人份)|(?:[单双多大小中]份))$",
    re.IGNORECASE,
)
_PURE_QTY_TOKEN_RE = re.compile(r"^(?:[xX×*]\s*)?(\d{1,2})\s*(?:(?<!人)份|件|个|杯|碗|盘|盒|串)?$")
_INLINE_QTY_SUFFIX_RE = re.compile(r"^(.{2,}?)(?:\s*[xX×*]\s*(\d{1,2})|\s+(\d{1,2})\s*(?:(?<!人)份|件|个|杯|碗|盘|盒))$")


def _parse_dish_items(dish_str: str, trailing_qty: int = 1) -> list[dict[str, Any]]:
    """将菜品描述拆分为结构化多菜列表 `[{'name': ..., 'qty': ...}]`，同时保护 `500g`、`麒麟西瓜果切+混合果切` 等单菜规格词不被拆碎。"""
    s = (dish_str or "").strip()
    if not s:
        return []
    # 仅按空白、中英文逗号/顿号/分号，或带空格的 ` + ` 切分（不拆紧邻无空格的 `西瓜+混合果切`）
    raw_parts = [p.strip() for p in re.split(r"(?:[,，、;；/]+|\s+[+＋&和与]\s+|\s+)", s) if p.strip()]
    items: list[dict[str, Any]] = []
    for tok in raw_parts:
        if tok in ("+", "＋", "&", "和", "与", "再来", "加", "配"):
            continue
        m_q = _PURE_QTY_TOKEN_RE.match(tok)
        if m_q and items:
            q_val = int(m_q.group(1))
            if 1 <= q_val <= 10:
                items[-1]["qty"] = q_val
                continue
        if _SPEC_UNIT_TOKEN_RE.match(tok) and items:
            items[-1]["name"] = f"{items[-1]['name']} {tok}".strip()
            continue
        m_inl = _INLINE_QTY_SUFFIX_RE.match(tok)
        if m_inl:
            nm = m_inl.group(1).strip()
            qv = int(m_inl.group(2) or m_inl.group(3) or "1")
            if nm and 1 <= qv <= 10:
                items.append({"name": nm, "qty": qv})
                continue
        items.append({"name": tok, "qty": 1})

    if len(items) == 1 and trailing_qty > 1 and items[0]["qty"] == 1:
        items[0]["qty"] = trailing_qty
    return items


def extract_order_query_parts(order_request: str) -> dict[str, Any]:
    """解析口语或命令式点餐请求，拆出「第N家」引用、店铺提示、多菜品清单、检索词与规格偏好。"""
    raw = (order_request or "").strip()
    raw = re.sub(
        r"^(?:我)?(?:好困|太困了|困了|有点困|好饿|太饿了|饿了|有点饿|嘴馋)[\s，,。！!]*",
        "",
        raw,
    ).strip()
    raw = re.sub(
        r"^(?:帮我|替我|给我)?(?:美团)?(?:点外卖|美团下单|下单外卖|点餐|美团点|想吃|想喝)\s*",
        "",
        raw,
    ).strip()
    raw = re.sub(
        r"^(?:帮我|替我|给我)?(?:美团)?(?:点|来|买|下单)(?:一?杯|一?份|一?碗)?(?=\s*(?:瑞幸|库迪|星巴克|挪瓦|皮爷|Manner|喜茶|霸王茶姬|奈雪))",
        "",
        raw,
        flags=re.IGNORECASE,
    ).strip()
    raw = re.sub(
        r"^(?:帮我|替我|给我)?(?:美团)?(?:点|来|买|下单)(?:一?杯|一?份|一?碗)\s*",
        "",
        raw,
    ).strip()
    raw = re.sub(r"[\s，,。！!]*?(?:确认下单|立即下单|直接下单|帮我下单)(?:吧|呗|哈|啊|呀|[。！!？?])*$", "", raw).strip()
    raw = re.sub(r"(?:啥的|之类的|什么的|吧|呗|哈|啊|呀|[。！!？?])+$", "", raw).strip()

    m_idx = re.match(
        r"^(?:第\s*([1-5一二三四五])\s*(?:家|个|号)?|([1-5])\s*(?:家|个|号)?)(?:$|[\s,，、:：]+(.+))$",
        raw,
    )
    if m_idx:
        cn_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
        tok_idx = m_idx.group(1) or m_idx.group(2) or "1"
        pick_idx_val = cn_map.get(tok_idx) or int(tok_idx)
        rest = (m_idx.group(3) or "").strip()
        rest_clean, spec_prefs = _extract_order_spec_prefs(rest)
        dish_str = rest_clean or rest
        dishes = _parse_dish_items(dish_str)
        first_dish = dishes[0]["name"] if len(dishes) == 1 else dish_str
        return {
            "raw_clean": raw,
            "pick_index": pick_idx_val,
            "shop_hint": "",
            "dish_hint": first_dish if len(dishes) <= 1 else dishes[0]["name"],
            "dishes": dishes,
            "search_kw": first_dish,
            "spec_prefs": spec_prefs,
        }

    cleaned_all, spec_prefs = _extract_order_spec_prefs(raw)
    tokens = [t for t in re.split(r"[\s,，、/]+", cleaned_all) if t]

    shop_hint = ""
    dish_raw = ""
    if len(tokens) >= 2:
        shop_hint = tokens[0]
        rest_tokens = tokens[1:]
        # 兼容「示例粉面广东大排档 鸟巢店 三丝炒米粉」分店名空格拆开写法
        if len(rest_tokens) >= 2 and re.match(r"^[^\d\s]{1,10}(?:店|分店)$", rest_tokens[0]) and "(" not in shop_hint and "（" not in shop_hint:
            shop_hint = f"{shop_hint}({rest_tokens[0]})"
            rest_tokens = rest_tokens[1:]
        # 从原 cleaned_all 截取第一词之后的部分以保留 ` + ` 等分隔上下文
        dish_raw = " ".join(rest_tokens).strip()
    elif len(tokens) == 1:
        tok = tokens[0]
        m_brand = re.match(
            r"^(瑞幸(?:咖啡)?|库迪(?:咖啡)?|星巴克|挪瓦(?:咖啡)?|皮爷(?:咖啡)?|Manner|喜茶|霸王茶姬|奈雪(?:的茶)?|麦当劳|肯德基)(.{2,})$",
            tok,
            re.IGNORECASE,
        )
        if m_brand:
            shop_hint = m_brand.group(1).strip()
            dish_raw = m_brand.group(2).strip()
        else:
            dish_raw = tok

    dishes = _parse_dish_items(dish_raw)
    if len(dishes) == 1:
        dish_hint = str(dishes[0]["name"])
    elif len(dishes) > 1:
        dish_hint = str(dishes[0]["name"])
    else:
        dish_hint = dish_raw

    search_kw = f"{shop_hint} {dish_hint}".strip() if (shop_hint or dish_hint) else raw
    return {
        "raw_clean": raw,
        "pick_index": None,
        "shop_hint": shop_hint,
        "dish_hint": dish_hint,
        "dishes": dishes,
        "search_kw": search_kw,
        "spec_prefs": spec_prefs,
    }


def _score_order_dish_match(dish_name: str, query: str) -> float:
    """对点餐菜品名与需求词做模糊打分（使「苹果美式」高分匹配「苹果C美式」「红苹果气泡冰美式」等）。"""
    d = re.sub(r"\s+", "", (dish_name or "").lower())
    q = re.sub(r"\s+", "", (query or "").lower())
    if not d or not q:
        return 0.0
    if d == q:
        return 10.0
    if q in d:
        return 9.2 - min(1.5, (len(d) - len(q)) * 0.15)
    q_chars = [ch for ch in q if ch.isalnum() or "\u4e00" <= ch <= "\u9fff"]
    if not q_chars:
        return 0.0
    hits = sum(1 for ch in q_chars if ch in d)
    ratio = hits / len(q_chars)
    score = ratio * 5.2
    if len(q_chars) >= 2:
        bigrams = [q[i : i + 2] for i in range(len(q) - 1)]
        bg_hits = sum(1 for bg in bigrams if bg in d)
        score += (bg_hits / len(bigrams)) * 3.5
    # 若所有查询字符均按顺序出现在菜品名中（如 苹果美式 -> 苹果C美式），给予高保真加分
    pos = 0
    for ch in q_chars:
        idx = d.find(ch, pos)
        if idx < 0:
            pos = -1
            break
        pos = idx + 1
    if pos >= 0 and len(q_chars) >= 3:
        score = max(score, 9.2 - min(1.8, (len(d) - len(q)) * 0.2))

    # 容量/重量硬核校验（如 1.5L vs 2L / 550ml）：用户明确指定容量时，严防跨店搜索卡错配成其他容量！
    m_qv = re.search(r"(\d+(?:\.\d+)?)\s*(ml|l|g|kg|克|千克|毫升)", query or "", re.IGNORECASE)
    m_dv = re.search(r"(\d+(?:\.\d+)?)\s*(ml|l|g|kg|克|千克|毫升)", dish_name or "", re.IGNORECASE)
    if m_qv and m_dv:
        def _to_base(val_s: str, unit_s: str) -> tuple[float, str]:
            v = float(val_s)
            u = unit_s.lower()
            if u == "l":
                return v * 1000.0, "ml"
            if u in ("ml", "毫升"):
                return v, "ml"
            if u in ("kg", "千克"):
                return v * 1000.0, "g"
            return v, "g"

        qv, qu = _to_base(m_qv.group(1), m_qv.group(2))
        dv, du = _to_base(m_dv.group(1), m_dv.group(2))
        if qu == du and qv > 0 and dv > 0:
            if abs(qv - dv) / max(qv, dv) <= 0.05:
                score += 2.5
            else:
                score -= 5.5
    return score


def _rank_order_candidates(
    live_shops: list[dict[str, Any]],
    shop_hint: str,
    dish_hint: str,
    profile: list[dict[str, Any]],
    *,
    preferred_shop: str = "",
    prefer_lowest_net_price: bool = False,
    account_coupons: Optional[dict[str, Any]] = None,
    order_category: str = "food",
    order_qty: int = 1,
) -> list[dict[str, Any]]:
    """对美团 H5 实时检索返回的可配送门店进行排序并挑选每家最匹配的在售菜品（支持跨商超券后到手价最优比价）。"""
    by_norm = {p["norm"]: p for p in profile}
    by_brand: dict[str, dict[str, Any]] = {}
    for p in profile:
        if len(p["brand"]) >= 2 and p["brand"] not in by_brand:
            by_brand[p["brand"]] = p

    sh_norm = _normalize_shop_name(shop_hint) if shop_hint else ""
    sh_brand = _shop_brand_key(shop_hint) if shop_hint else ""
    pref_norm = _normalize_shop_name(preferred_shop) if preferred_shop else ""
    pref_brand = _shop_brand_key(preferred_shop) if preferred_shop else ""

    ranked: list[tuple[float, dict[str, Any]]] = []
    for idx, cand in enumerate(live_shops):
        if not isinstance(cand, dict) or cand.get("is_closed"):
            continue
        c_name = str(cand.get("shop") or "").strip()
        if not c_name:
            continue
        c_norm = _normalize_shop_name(c_name)
        c_brand = _shop_brand_key(c_name)

        shop_matched = False
        score = max(0.0, 2.4 - idx * 0.35)

        if sh_norm:
            if sh_norm == c_norm:
                shop_matched = True
                # 用户或历史复购明确指定具体分店名（如「南京大牌档(中关村店)」）时，精确分店决定性压过同品牌其他分店
                score += 26.0
            elif len(sh_brand) >= 2 and (sh_brand == c_brand or sh_brand in c_norm or c_brand in sh_norm):
                shop_matched = True
                score += 9.5
            else:
                score -= 4.0
        elif pref_norm:
            if pref_norm == c_norm or (len(pref_brand) >= 2 and (pref_brand == c_brand or pref_brand in c_norm)):
                score += 1.5  # 同价位下轻度偏好常购商超，但绝不压过更省钱的门店

        prof = _match_profile_for_live_shop(c_name, by_norm, by_brand)
        if prof is not None:
            is_exact_branch = prof.get("norm") == c_norm
            prof_w = 0.35 if prefer_lowest_net_price else 1.0
            score += min(4.2, (2.0 if is_exact_branch else 1.0) + int(prof.get("count") or 0) * 0.15) * prof_w

        dishes = [d for d in (cand.get("dishes") or []) if isinstance(d, dict) and d.get("name")]
        best_dish: Optional[dict[str, Any]] = None
        best_dish_score = 0.0
        if dishes:
            if dish_hint:
                for d in dishes:
                    ds = _score_order_dish_match(str(d.get("name") or ""), dish_hint)
                    if ds > best_dish_score:
                        best_dish_score = ds
                        best_dish = d
                score += best_dish_score
            else:
                best_dish = dishes[0]

        min_order = cand.get("min_order")
        dish_price = best_dish.get("price") if isinstance(best_dish, dict) else None
        meets_min: Optional[bool] = None
        if min_order is not None and dish_price is not None:
            meets_min = (float(dish_price) * max(1, int(order_qty))) >= float(min_order)
            score += 2.8 if meets_min else -2.2

        # 测算该店券后预估到手价（商品价 + 配送费 - 门店满减 - 最优适用红包）
        est_net_yuan: Optional[float] = None
        coupon_disc_est = 0.0
        if dish_price is not None and float(dish_price) > 0:
            raw_deliv = str(cand.get("delivery_fee") or "")
            m_df = re.search(r"(\d+(?:\.\d+)?)", raw_deliv)
            deliv_val = 0.0 if "免" in raw_deliv else (float(m_df.group(1)) if m_df else 0.0)
            sub_val = round(float(dish_price) * max(1, int(order_qty)), 2)
            try:
                from siming.meituan_coupons import infer_order_category
                from siming.meituan_speculate import compute_coupon_pricing, parse_store_promos

                c_promos = parse_store_promos("\n".join(cand.get("promo_lines") or []))
                o_cat = order_category or infer_order_category(c_name, item_names=[str(best_dish.get("name") or "") if best_dish else ""])
                cp_c = compute_coupon_pricing(
                    shop_name=c_name,
                    subtotal=sub_val,
                    delivery_fee=deliv_val,
                    store_promos=c_promos,
                    account_coupons=account_coupons,
                    order_category=o_cat,
                )
                est_net_yuan = float(cp_c["after_coupon_total"])
                coupon_disc_est = float(cp_c["total_discount"])
            except Exception:
                est_net_yuan = round(sub_val + deliv_val, 2)

            if prefer_lowest_net_price and est_net_yuan is not None and best_dish_score >= 4.5:
                # 补货比价模式：券后到手价每省 ¥1 显著加权（+0.85），让更便宜/大券商超胜出！
                score += max(-18.0, min(22.0, (55.0 - est_net_yuan) * 0.85))
                if coupon_disc_est > 0:
                    score += min(4.0, coupon_disc_est * 0.15)

        dist_km = cand.get("distance_km")
        if dist_km is not None:
            if float(dist_km) <= 1.2:
                score += 1.5
            elif float(dist_km) <= 2.2:
                score += 0.6
        eta_min = cand.get("eta_min")
        if eta_min is not None and eta_min <= 32:
            score += 0.6

        ranked.append(
            (
                score,
                {
                    **cand,
                    "shop_matched": shop_matched,
                    "best_dish": best_dish,
                    "best_dish_score": round(best_dish_score, 2),
                    "meets_min_order": meets_min,
                    "est_net_yuan": est_net_yuan,
                    "coupon_discount_est": coupon_disc_est,
                    "rank_score": round(score, 2),
                },
            )
        )

    ranked.sort(key=lambda x: x[0], reverse=True)
    return [x[1] for x in ranked]


def _default_meituan_h5_probe(
    search_kw: str,
    port: int = 9222,
    db: Optional[SimingDB] = None,
    keep_tab: bool = False,
    location_override: str = "",
) -> dict[str, Any]:
    """在主 Chromium 后台同步当前位置并静默搜索美团外卖 H5 门店/菜品（遇登录或滑块验证立即合规停手）。"""
    loc_target: dict[str, Any] = {"has_location": False}
    if db is not None:
        loc_target = resolve_meituan_delivery_target(db, location_override=location_override)
    res = fetch_live_meituan_candidates(
        db=db or SimingDB(),
        loc_target=loc_target,
        keyword=search_kw,
        port=port,
        scroll_rounds=0,
        close_when_clean=not keep_tab,
    )
    if res.get("gated"):
        return res
    if not res.get("ok"):
        return res
    shops = res.get("shops") or []
    prices: list[str] = []
    top_lines: list[str] = []
    if shops:
        s0 = shops[0]
        parts = [s0["shop"]]
        if s0.get("rating") is not None:
            parts.append(f"{s0['rating']:.1f}分")
        if s0.get("eta_min") is not None:
            parts.append(f"{s0['eta_min']}分钟")
        if s0.get("distance_str"):
            parts.append(str(s0["distance_str"]))
        top_lines.append(" · ".join(parts))
        for d in (s0.get("dishes") or [])[:3]:
            top_lines.append(f"{d['name']} ¥{d['price']:.0f}")
            prices.append(f"¥{d['price']}")
        if not prices and s0.get("min_order") is not None:
            prices.append(f"¥{s0['min_order']}")
    else:
        top_lines = list(res.get("top_lines") or [])[:4]
    delivery_poi = str(res.get("delivery_poi") or "").strip()
    delivery_addr = str(res.get("delivery_address") or "").strip()
    full_poi = (
        f"{delivery_poi} {delivery_addr}".strip()
        if (delivery_poi and delivery_addr and delivery_addr not in delivery_poi)
        else delivery_poi
    )
    return {
        "ok": True,
        "gated": False,
        "target_id": str(res.get("target_id") or ""),
        "delivery_poi": full_poi,
        "delivery_address": delivery_addr,
        "shops": shops,
        "data": json.dumps(
            {
                "href": res.get("url") or "",
                "title": res.get("title") or "",
                "delivery_poi": full_poi,
                "top_lines": top_lines,
                "prices": prices,
                "shops": shops[:10],
            },
            ensure_ascii=False,
        ),
    }


def stage_meituan_h5_order(
    db: SimingDB,
    order_request: str,
    cdp_probe_fn: Optional[Callable[[str], dict[str, Any]]] = None,
    auto_preview_pay: bool = True,
    location_override: str = "",
    explicit_shop: str = "",
    explicit_dishes: Optional[list[dict[str, Any]]] = None,
    *,
    preferred_shop: str = "",
    prefer_lowest_net_price: bool = False,
) -> dict[str, Any]:
    """美团统一点餐入口（默认全平台走美团 H5，含瑞幸/库迪等咖啡奶茶与正餐，绝不劫持去要瑞幸官方 Token）：
    - 支持口语点餐（如 `困了 给我点一杯苹果美式啥的`）、`第1家 / 第2家` 引用、指定「店名 菜品」与规格偏好；
    - 在线实时模式下（`auto_preview_pay=True` 且主 Chromium 9222 就绪），一步完成「搜店 → 进店选规格加购 → 进结算预览页 `mindex/preview` 核验真实实付总额与配送地址」，签发 10m 有效 `meituan_submit_pay` 付款许可确认卡（零扣款；仅在用户明确回复 `确认支付` / `apply Axx` 后才真提交并支付）；
    - 遇登录/滑块风控、H5 页面错误或指定门店不在当前配送范围时，诚实拦截且绝不生成虚假确认卡。
    """
    db.init_db()
    live_preview_mode = bool(
        auto_preview_pay
        and cdp_probe_fn is None
        and os.environ.get("SIMING_TEST_OFFLINE") != "1"
    )
    order_qty, order_request_clean = _extract_order_qty(order_request)
    parsed_q = extract_order_query_parts(order_request_clean)
    order_dishes: list[dict[str, Any]] = [dict(d) for d in (parsed_q.get("dishes") or []) if d.get("name")]
    if explicit_dishes:
        order_dishes = [
            {"name": str(d.get("name") or "").strip(), "qty": max(1, int(d.get("qty") or 1))}
            for d in explicit_dishes
            if isinstance(d, dict) and str(d.get("name") or "").strip()
        ]
    if len(order_dishes) == 1:
        if order_qty > 1 and int(order_dishes[0].get("qty") or 1) == 1:
            order_dishes[0]["qty"] = order_qty
        else:
            order_qty = int(order_dishes[0].get("qty") or order_qty)

    picks_cached: list[dict[str, Any]] = []
    try:
        picks_cached = json.loads(db.get_meta("last_meituan_picks") or "[]")
    except Exception:
        picks_cached = []

    profile = build_user_dining_profile(db)
    shop_hint = (explicit_shop or "").strip() or str(parsed_q.get("shop_hint") or "")
    dish_hint = (
        str(order_dishes[0]["name"])
        if explicit_dishes and order_dishes
        else str(parsed_q.get("dish_hint") or "")
    )
    spec_prefs: list[str] = list(parsed_q.get("spec_prefs") or [])
    est_yuan: Optional[float] = None
    backup_choice = ""
    picked_from_cache = False

    pick_index = None if explicit_shop else parsed_q.get("pick_index")
    if pick_index is not None and picks_cached:
        idx0 = int(pick_index) - 1
        if 0 <= idx0 < len(picks_cached):
            shop_hint = str(picks_cached[idx0]["shop"])
            est_yuan = float(picks_cached[idx0].get("avg_yuan") or 0.0) or None
            picked_from_cache = True
            if len(picks_cached) > 1:
                alt = picks_cached[1 if idx0 == 0 else 0]
                backup_choice = f"{alt['shop']}（约 ¥{float(alt.get('avg_yuan') or 35):.0f}）"
    if pick_index is not None and not shop_hint:
        from siming.meituan_speculate import load_active_speculative_batch

        act_b = load_active_speculative_batch(db)
        act_entries = list((act_b or {}).get("entries") or [])
        for ent_b in act_entries:
            if int(ent_b.get("index") or 0) == int(pick_index) and ent_b.get("shop"):
                shop_hint = str(ent_b["shop"])
                est_yuan = float(ent_b.get("avg_yuan") or ent_b.get("est_after_coupon") or 0.0) or None
                picked_from_cache = True
                break

    # 若用户只给了一个词且明确命中历史常点品牌名（如「点外卖 素坤泰」「点外卖 瑞幸」），提前识别为 shop_hint
    if not shop_hint and dish_hint and len(order_dishes) <= 1:
        dh_norm = _normalize_shop_name(dish_hint)
        if dh_norm and dish_hint.strip() not in _GENERIC_ORDER_CATEGORY_WORDS and len(dh_norm) >= 2:
            for p in profile:
                p_norm = p["norm"]
                p_brand = p["brand"]
                if dh_norm == p_norm or dh_norm == p_brand or (len(p_brand) >= 2 and (p_brand.startswith(dh_norm) or p_brand in dh_norm)):
                    shop_hint = p["shop"]
                    dish_hint = ""
                    order_dishes = []
                    est_yuan = float(p["avg_yuan"])
                    break

    # 若命中有预热完毕的投机菜品推演（且用户未显式换菜/未注入 mock probe），0ms 直接输出含精确菜品、券后价与直达入口的确认卡！
    if (pick_index is not None or shop_hint) and not dish_hint and not order_dishes and cdp_probe_fn is None:
        from siming.meituan_speculate import (
            find_speculative_entry,
            format_coupon_price_line,
            format_dishes_brief,
        )

        spec_lookup_key = str(pick_index) if pick_index is not None else shop_hint
        spec_ent = find_speculative_entry(db, spec_lookup_key, wait_if_planning_sec=3.0 if pick_index is not None else 0.5)
        if spec_ent and spec_ent.get("status") == "READY" and spec_ent.get("dishes") and spec_ent.get("menu_url"):
            policy = load_policy(db.paths)
            max_yuan = float((policy.get("spending_caps") or {}).get("meituan_single_order_yuan", 120.0))
            cp = spec_ent.get("coupon_pricing") or {}
            est_raw_total = float(spec_ent.get("est_total") or spec_ent.get("est_subtotal") or est_yuan or 0.0)
            est_pay_total = float(spec_ent.get("est_after_coupon") if spec_ent.get("est_after_coupon") is not None else est_raw_total)
            if est_pay_total > max_yuan:
                return {
                    "ok": False,
                    "over_cap": True,
                    "reply": f"🛑 预推券后总计 **¥{est_pay_total:.1f}** 超过美团单笔安全上限（¥{max_yuan:.0f}），已拦截。",
                }
            m_shop = str(spec_ent.get("live_shop_name") or spec_ent.get("shop") or shop_hint)
            dishes = list(spec_ent["dishes"])
            dish_brief = format_dishes_brief(dishes)
            d_poi = str(spec_ent.get("delivery_poi") or "")
            deliv_fee = float(spec_ent.get("delivery_fee") or 0.0)
            min_ord = float(spec_ent.get("min_order") or 0.0)
            deeplink = f"imeituan://www.meituan.com/waimai/search?query={urllib.parse.quote(str(spec_ent.get('shop') or m_shop))}"
            params = {
                "shop": m_shop,
                "dish_name": str(dishes[0].get("name") or ""),
                "dishes": dishes,
                "menu_url": str(spec_ent["menu_url"]),
                "item_spec": dish_brief,
                "spec_prefs": spec_prefs,
                "est_yuan": est_pay_total,
                "est_raw_total": est_raw_total,
                "coupon_pricing": cp,
                "delivery_fee": f"¥{deliv_fee:g}" if deliv_fee > 0 else "",
                "min_order": min_ord or None,
                "h5_ready": True,
                "delivery_poi": d_poi,
                "loc_override": location_override,
                "speculative_ready": True,
                "deeplink": deeplink,
            }
            # 在线模式直接直达预推门店加购并进入 mindex/preview，一次给出带真实实付总额的付款许可确认卡（零扣款）
            if live_preview_mode:
                from siming.meituan_pay import stage_meituan_payment_preview
                from siming.meituan_speculate import execute_speculative_cart_add

                cart_res = execute_speculative_cart_add(db, params)
                if cart_res.get("gated"):
                    gate = cart_res.get("gate") or {}
                    return {
                        "ok": False,
                        "gated": True,
                        "reply": (
                            f"🛡️ **美团进店加购触发合规门禁**（门店：**{m_shop}**）\n"
                            f"• 原因：{gate.get('reason') or '需登录或滑块验证'}\n"
                            f"• 司命已原样保留桌面 Chromium 后台标签页（绝不绕过验证），处理完成后重发即可；或手机唤端：`{deeplink}`"
                        ),
                    }
                if cart_res.get("ok") and cart_res.get("added_to_cart") and cart_res.get("target_id"):
                    pay_stage = stage_meituan_payment_preview(
                        db,
                        cart_res=cart_res,
                        shop=m_shop,
                        item_spec=dish_brief,
                        deeplink=deeplink,
                        backup_choice=backup_choice,
                        location_override=location_override,
                    )
                    if pay_stage.get("ok") or pay_stage.get("gated") or pay_stage.get("over_cap"):
                        return {**pay_stage, "speculative_hit": True}

            app = create_approval(
                db,
                action="meituan_lock_cart",
                target_id=f"MT-{re.sub(r'[^A-Za-z0-9\u4e00-\u9fff]+', '', m_shop)[:14] or 'ORDER'}",
                params=params,
                ttl_minutes=15,
            )
            db.record_human_use("meituan_agent", "stage_order_speculative", ref_id=app["token"])
            price_line = format_coupon_price_line(
                est_subtotal=float(spec_ent.get("est_subtotal") or 0.0),
                delivery_fee=deliv_fee,
                est_total=est_raw_total,
                coupon_pricing=cp,
            )
            lines = [
                f"⚡ **美团点餐确认卡（命中后台投机预操作）** [`{app['token']}`]",
                f"• 门店：**{m_shop}**" + (f"（📍 {d_poi}）" if d_poi else ""),
                f"• 预推菜品：{dish_brief}（{spec_ent.get('reason')}）",
                f"• 价格估算：{price_line}（单笔上限 ¥{max_yuan:.0f}）",
            ]
            if backup_choice:
                lines.append(f"• 备选参考：{backup_choice}")
            lines.append(f"👉 15 分钟内直接回 `确认`（或 `apply {app['token']}`），即刻直达门店页自动加购以上菜品供你结算！")
            return {
                "ok": True,
                "speculative_hit": True,
                "approval_token": app["token"],
                "shop": m_shop,
                "dishes": dishes,
                "est_yuan": est_pay_total,
                "est_raw_total": est_raw_total,
                "coupon_pricing": cp,
                "h5_ready": True,
                "delivery_poi": d_poi,
                "reply": "\n".join(lines),
            }

    if not shop_hint and not dish_hint:
        rec = recommend_meituan_meals(db)
        if not rec["picks"]:
            return {"ok": False, "reply": "请告诉我你想点的店名或品类，例如：`点外卖 素坤泰 打抛饭` 或 `点外卖 苹果美式`。"}
        top0 = rec["picks"][0]
        shop_hint = str(top0["shop"])
        est_yuan = float(top0["avg_yuan"])
        if len(rec["picks"]) > 1:
            backup_choice = f"{rec['picks'][1]['shop']}（约 ¥{rec['picks'][1]['avg_yuan']:.0f}）"

    if shop_hint and est_yuan is None and not picked_from_cache:
        sh_brand = _shop_brand_key(shop_hint)
        sh_norm = _normalize_shop_name(shop_hint)
        for p in profile:
            if sh_norm == p["norm"] or (len(sh_brand) >= 2 and sh_brand == p["brand"]):
                est_yuan = float(p["avg_yuan"])
                break

    # 检索词：多菜订单以店名为检索词以免全网搜多道菜为空；单菜订单用剥离分店括号后的品牌名 + 菜品词
    search_brand = _strip_branch_display(shop_hint) if shop_hint else ""
    if len(order_dishes) > 1 and shop_hint:
        probe_q = shop_hint
    else:
        probe_q = f"{search_brand} {dish_hint}".strip() if (search_brand or dish_hint) else (shop_hint or parsed_q["raw_clean"])

    policy = load_policy(db.paths)
    caps = policy.get("spending_caps") or {}
    max_yuan = float(caps.get("meituan_single_order_yuan", 120.0))

    probe = (
        cdp_probe_fn(probe_q)
        if cdp_probe_fn is not None
        else _default_meituan_h5_probe(probe_q, db=db, keep_tab=live_preview_mode,
                                        location_override=location_override)
    )
    probe_tid = str(probe.get("target_id") or "") if live_preview_mode else ""

    def _close_probe_tab() -> None:
        if probe_tid:
            from siming.meituan_pay import _close_tab_quietly

            _close_tab_quietly(9222, probe_tid)

    deeplink = f"imeituan://www.meituan.com/waimai/search?query={urllib.parse.quote(probe_q)}"

    if probe.get("gated"):
        gate = probe.get("gate") or {}
        g_kind = gate.get("gate_kind")
        return {
            "ok": False,
            "gated": True,
            "gate_kind": g_kind,
            "reply": (
                f"🛡️ **美团 H5 合规接力提示**（目标：**{probe_q}**）\n"
                f"• 原因：{gate.get('reason')}\n"
                "• 司命已按安全红线原样保留桌面主浏览器后台标签页（绝不硬闯风控验证码防封号）。\n"
                f"• 请在桌面 Chromium 切到美团标签滑一下/扫码登录，完成后回一句 `点外卖 {probe_q}` 即可继续；或在手机直接点唤端：`{deeplink}`"
            ),
        }

    h5_ready = bool(probe.get("ok"))
    if not h5_ready and probe.get("reason") == "page_error":
        _close_probe_tab()
        return {
            "ok": False,
            "page_error": True,
            "reply": f"⚠️ 美团 H5 实时检索返回异常（{probe.get('error') or '参数错误'}），未生成虚假确认卡，请稍后重试或直接在手机美团搜「{probe_q}」。",
        }

    h5_snippet = ""
    delivery_poi = str(probe.get("delivery_poi") or "")
    matched_shop_name = shop_hint or dish_hint
    matched_dish_name = dish_hint
    delivery_fee_str = ""
    min_order_yuan: Optional[float] = None
    eta_min: Optional[int] = None
    distance_str = ""
    meets_min_order: Optional[bool] = None

    if h5_ready:
        d_obj: dict[str, Any] = {}
        try:
            d_obj = json.loads(str(probe.get("data") or "{}")) if isinstance(probe.get("data"), str) else (probe.get("data") or {})
        except Exception:
            d_obj = {}
        delivery_poi = delivery_poi or str(d_obj.get("delivery_poi") or "")
        top_lns = [ln for ln in (d_obj.get("top_lines") or []) if len(ln) >= 2]
        live_found = probe.get("shops")
        if live_found is None:
            live_found = d_obj.get("shops")

        # 拦截藏在 top_lines 里的页面报错，绝不生成虚假确认卡
        if not live_found and top_lns and _PAGE_ERROR_RE.search("\n".join(top_lns)):
            _close_probe_tab()
            return {
                "ok": False,
                "page_error": True,
                "reply": f"⚠️ 美团 H5 实时检索返回异常（{' / '.join(top_lns[:3])}），未生成虚假确认卡，请稍后重试或直接在手机美团搜「{probe_q}」。",
            }

        if isinstance(live_found, list):
            rank_coupons = None
            rank_cat = "food"
            try:
                from siming.meituan_coupons import infer_order_category
                from siming.meituan_speculate import load_cached_account_coupons

                rank_coupons = load_cached_account_coupons(db, max_age_min=720.0)
                rank_cat = infer_order_category(
                    shop_hint or preferred_shop,
                    store_kind="subtrade" if prefer_lowest_net_price else "",
                    item_names=[dish_hint] if dish_hint else None,
                )
            except Exception:
                rank_coupons = None

            ranked = _rank_order_candidates(
                live_found,
                shop_hint=shop_hint,
                dish_hint=dish_hint,
                profile=profile,
                preferred_shop=preferred_shop,
                prefer_lowest_net_price=prefer_lowest_net_price,
                account_coupons=rank_coupons,
                order_category=rank_cat,
                order_qty=order_qty,
            )
            chosen: Optional[dict[str, Any]] = None
            if shop_hint:
                chosen = next((c for c in ranked if c.get("shop_matched")), None)
                # 若「店名+菜品」在全网搜索页被热门同品类门店挤掉，自动回退纯店名再搜一次
                if chosen is None and cdp_probe_fn is None and probe_q != shop_hint:
                    _close_probe_tab()
                    probe2 = _default_meituan_h5_probe(
                        shop_hint,
                        db=db,
                        keep_tab=live_preview_mode,
                        location_override=location_override,
                    )
                    probe_tid = str(probe2.get("target_id") or "") if live_preview_mode else ""
                    if probe2.get("ok") and isinstance(probe2.get("shops"), list):
                        probe_q = shop_hint
                        live_found = probe2["shops"]
                        delivery_poi = str(probe2.get("delivery_poi") or delivery_poi)
                        ranked = _rank_order_candidates(live_found, shop_hint=shop_hint, dish_hint=dish_hint, profile=profile)
                        chosen = next((c for c in ranked if c.get("shop_matched")), None)
            else:
                # 仅给单查询词（如「苹果美式」或外地店名「皖峰饭馆」）：
                # 既支持匹配店名（如某新店），也支持模糊匹配在售菜品（如「苹果美式」→「苹果C美式」）；
                # 若店名与菜品全都不沾边，则诚实按 undeliverable 拦截，绝不张冠李戴！
                q_norm = _normalize_shop_name(dish_hint)
                q_brand = _shop_brand_key(dish_hint)
                chosen = next(
                    (
                        c
                        for c in ranked
                        if q_norm
                        and (
                            q_norm in _normalize_shop_name(str(c.get("shop") or ""))
                            or (len(q_brand) >= 2 and q_brand == _shop_brand_key(str(c.get("shop") or "")))
                        )
                    ),
                    None,
                )
                if chosen is None:
                    is_shop_like = bool(re.search(r"(饭馆|餐厅|酒楼|饭庄|小馆|食堂|食府|茶楼|馆)$", dish_hint))
                    if not is_shop_like:
                        dish_cands = [c for c in ranked if c.get("best_dish") and float(c.get("best_dish_score") or 0.0) >= 4.5]
                        if dish_cands:
                            chosen = dish_cands[0]

            if chosen is not None:
                matched_shop_name = str(chosen.get("shop") or matched_shop_name)
                best_d = chosen.get("best_dish")
                best_d_score = float(chosen.get("best_dish_score") or 0.0)
                # 仅当用户未指定菜品，或搜索卡片展示菜品与用户所点高度匹配（>=6.0）且为单菜订单时才采用卡片菜名；
                # 否则保留用户指定菜名，进店后通过菜单/店内搜索精确命中，绝不拿卡片招牌菜替换用户点的菜！
                if len(order_dishes) <= 1 and isinstance(best_d, dict) and best_d.get("name") and (not dish_hint or best_d_score >= 6.0):
                    matched_dish_name = str(best_d["name"])
                    if best_d.get("price") is not None:
                        est_yuan = float(best_d["price"])
                elif est_yuan is None and chosen.get("per_capita") is not None and len(order_dishes) <= 1:
                    est_yuan = float(chosen["per_capita"])
                # 用户给的是完整菜名（长名/带规格/带【】前缀），但搜索结果只展示了同店其他推荐菜时，
                # 以用户菜名为准交给进店/店内搜索去精确命中，避免张冠李戴。
                hint_norm = dish_hint.replace(" ", "")
                hint_specific = bool(
                    dish_hint
                    and (
                        len(hint_norm) >= 12
                        or "【" in dish_hint
                        or re.search(r"\d+\s*(?:g|克|kg|ml|份|个|杯)", dish_hint)
                    )
                )
                if len(order_dishes) <= 1 and hint_specific and best_d_score < 9.0:
                    matched_dish_name = dish_hint
                if len(order_dishes) == 1 and matched_dish_name:
                    order_dishes[0]["name"] = matched_dish_name
                delivery_fee_str = str(chosen.get("delivery_fee") or "")
                min_order_yuan = chosen.get("min_order")
                eta_min = chosen.get("eta_min")
                distance_str = str(chosen.get("distance_str") or "")
                meets_min_order = chosen.get("meets_min_order")

                snip_parts = [matched_shop_name]
                if chosen.get("rating") is not None:
                    snip_parts.append(f"{float(chosen['rating']):.1f}分")
                if eta_min is not None:
                    snip_parts.append(f"{eta_min}分钟")
                if distance_str:
                    snip_parts.append(distance_str)
                if isinstance(best_d, dict) and best_d.get("name") and best_d.get("price") is not None:
                    snip_parts.append(f"{best_d['name']} ¥{float(best_d['price']):g}")
                h5_snippet = " · ".join(snip_parts)

                if not backup_choice and len(ranked) > 1:
                    top_b = _shop_brand_key(matched_shop_name)
                    alt_c = next(
                        (
                            c
                            for c in ranked
                            if c.get("shop") != matched_shop_name
                            and _shop_brand_key(str(c.get("shop") or "")) != top_b
                            and (shop_hint or float(c.get("best_dish_score") or 0.0) >= 4.5)
                        ),
                        next((c for c in ranked if c.get("shop") != matched_shop_name), None),
                    )
                    if alt_c:
                        ad = alt_c.get("best_dish")
                        ad_s = (
                            f" — {ad['name']} ¥{float(ad['price']):g}"
                            if isinstance(ad, dict) and ad.get("name") and ad.get("price") is not None
                            else ""
                        )
                        net_cmp = ""
                        if prefer_lowest_net_price and chosen.get("est_net_yuan") is not None and alt_c.get("est_net_yuan") is not None:
                            net_cmp = f"券后约¥{float(alt_c['est_net_yuan']):g} vs 已选券后约¥{float(chosen['est_net_yuan']):g}"
                        am = " · ".join(
                            x
                            for x in (
                                net_cmp,
                                f"{alt_c['eta_min']}分钟" if alt_c.get("eta_min") else "",
                                str(alt_c.get("distance_str") or ""),
                            )
                            if x
                        )
                        backup_choice = f"{alt_c['shop']}{ad_s}" + (f"（{am}）" if am else "")
            else:
                _close_probe_tab()
                alts = [str(c.get("shop") or "") for c in live_found[:3] if isinstance(c, dict) and c.get("shop")]
                poi_hint = f"「{delivery_poi}」" if delivery_poi else "当前位置"
                return {
                    "ok": False,
                    "undeliverable": True,
                    "delivery_poi": delivery_poi,
                    "alternatives": alts,
                    "reply": (
                        f"⚠️ 美团在{poi_hint}未搜到可配送的「**{probe_q}**」（可能超出配送范围或暂未营业）。\n"
                        + (f"• 附近可配送的相近门店：{'、'.join(alts)}\n" if alts else "")
                        + "👉 可直接改发 `晚饭推荐` 看附近在售餐厅，或 `点外卖 <上面任一店名>`。"
                    ),
                }
        else:
            # 兼容仅含 top_lines / prices 的旧单元测试 mock
            if top_lns:
                h5_snippet = " / ".join(top_lns[:4])
            if est_yuan is None and d_obj.get("prices"):
                m_p = re.search(r"([0-9]+(?:\.[0-9]{1,2})?)", str(d_obj["prices"][0]))
                if m_p:
                    est_yuan = float(m_p.group(1))

    # 若用户只报了熟店名未指定具体菜品（如 `--order 素坤泰`），在在线模式下回退最近常点组合以便自动加购
    if not order_dishes and matched_dish_name:
        order_dishes = [{"name": matched_dish_name, "qty": order_qty}]
    elif not order_dishes and not matched_dish_name and live_preview_mode and matched_shop_name:
        from siming.meituan_speculate import load_shop_historical_dish_combos

        hist_combo = load_shop_historical_dish_combos(db, matched_shop_name)
        if hist_combo.get("has_history") and hist_combo.get("last_combo"):
            order_dishes = [
                {"name": str(it["name"]), "qty": max(1, int(it.get("qty") or 1))}
                for it in hist_combo["last_combo"][:3]
                if it.get("name")
            ]
            if order_dishes:
                matched_dish_name = str(order_dishes[0]["name"])
                order_qty = int(order_dishes[0]["qty"])

    if est_yuan is not None and est_yuan > max_yuan:
        _close_probe_tab()
        return {
            "ok": False,
            "over_cap": True,
            "reply": f"🛑 预估金额 **¥{est_yuan:.0f}** 超过美团单笔安全上限（¥{max_yuan:.0f}），已拦截。如需放宽可改 `policy.yaml` 的 `spending_caps.meituan_single_order_yuan`。",
        }

    spec_suffix = f"（{' / '.join(spec_prefs)}）" if spec_prefs else ""
    if len(order_dishes) > 1:
        multi_s = " + ".join(
            f"{d['name']}×{d['qty']}" if int(d.get("qty") or 1) > 1 else str(d["name"])
            for d in order_dishes
        )
        item_spec_display = f"{multi_s}{spec_suffix}"
    else:
        item_spec_display = (
            f"{matched_dish_name}{spec_suffix}"
            if matched_dish_name
            else (f"店内招牌/常点套餐{spec_suffix}" if spec_suffix else "店内招牌/常点套餐")
        )
    deeplink = f"imeituan://www.meituan.com/waimai/search?query={urllib.parse.quote(matched_shop_name)}"

    # 若能从推荐缓存或投机批次反查到该门店的 poi_id_str / menu_url，透传给直航逻辑绕开脆弱 search_click
    # 注意：若已锁定具体分店（含括号分店名），仅接受精确同名分店的缓存直航，防同品牌其他分店串店！
    cached_poi_id = ""
    cached_menu_url = ""
    if matched_shop_name:
        norm_ms = _normalize_shop_name(matched_shop_name)
        has_branch_ms = bool(re.search(r"[（(].+[)）]", matched_shop_name))
        for pc in picks_cached:
            pc_norm = _normalize_shop_name(str(pc.get("shop") or ""))
            if not norm_ms or not pc_norm:
                continue
            matched_cache = (norm_ms == pc_norm) if has_branch_ms else (norm_ms in pc_norm or pc_norm in norm_ms)
            if matched_cache:
                cached_poi_id = str(pc.get("poi_id_str") or "")
                cached_menu_url = str(pc.get("menu_url") or "")
                if cached_poi_id or cached_menu_url:
                    break
        if not cached_poi_id and not cached_menu_url:
            try:
                from siming.meituan_speculate import find_speculative_entry

                sp_hit = find_speculative_entry(db, matched_shop_name, wait_if_planning_sec=0.0)
                if sp_hit:
                    sp_norm = _normalize_shop_name(str(sp_hit.get("live_shop_name") or sp_hit.get("shop") or ""))
                    if (not has_branch_ms) or (sp_norm == norm_ms):
                        cached_menu_url = str(sp_hit.get("menu_url") or "")
                        cached_poi_id = str(sp_hit.get("poi_id_str") or "")
            except Exception:
                pass

    params = {
        "shop": matched_shop_name,
        "dish_name": matched_dish_name,
        "dishes": order_dishes,
        "query": probe_q,
        "qty": order_qty,
        "item_spec": item_spec_display,
        "spec_prefs": spec_prefs,
        "est_yuan": est_yuan,
        "delivery_fee": delivery_fee_str,
        "min_order": min_order_yuan,
        "h5_ready": h5_ready,
        "delivery_poi": delivery_poi,
        "loc_override": location_override,
        "deeplink": deeplink,
        "poi_id_str": cached_poi_id,
        "menu_url": cached_menu_url,
    }

    # 在线实时模式：零扣款直接复用搜索标签进店加购并直达结算预览（兼容 mindex/preview 与 package/preview），返回带真实实付金额的付款确认卡
    if live_preview_mode and h5_ready:
        from siming.meituan_pay import stage_meituan_payment_preview

        cart_res = _live_cart_via_trusted_input(
            db,
            matched_shop_name,
            matched_dish_name,
            order_qty,
            search_keyword=probe_q,
            location_override=location_override,
            address_hint=delivery_poi,
            existing_target_id=probe_tid,
            dishes=order_dishes if len(order_dishes) > 1 else None,
            spec_prefs=spec_prefs or None,
            poi_id_str=cached_poi_id,
            direct_menu_url=cached_menu_url,
        )
        if cart_res.get("gated"):
            gate = cart_res.get("gate") or {}
            return {
                "ok": False,
                "gated": True,
                "reply": (
                    f"🛡️ **美团进店加购触发合规门禁**（门店：**{matched_shop_name}**）\n"
                    f"• 原因：{gate.get('reason') or '需登录或滑块验证'}\n"
                    f"• 司命已原样保留桌面 Chromium 后台标签页（绝不绕过验证），处理完成后重发即可；或手机唤端：`{deeplink}`"
                ),
            }
        if cart_res.get("ok") and cart_res.get("added_to_cart") and cart_res.get("preview"):
            pay_stage = stage_meituan_payment_preview(
                db,
                cart_res=cart_res,
                shop=matched_shop_name,
                item_spec=item_spec_display,
                deeplink=deeplink,
                backup_choice=backup_choice,
                preview_fn=lambda *_args: cart_res["preview"],
                location_override=location_override,
            )
            if pay_stage.get("ok") or pay_stage.get("gated") or pay_stage.get("over_cap"):
                return pay_stage
            if pay_stage.get("reason") == "below_min_order":
                alt_line = f"\n• 备选门店：{backup_choice}" if backup_choice else ""
                return {
                    "ok": False,
                    "below_min_order": True,
                    "shop": matched_shop_name,
                    "reply": (
                        f"⚠️ 已在后台将「**{cart_res.get('shop') or matched_shop_name}** — `{cart_res.get('dish') or item_spec_display}`」加入购物车，"
                        f"但暂未满起送价（{pay_stage.get('error') or '差起送价'}），暂时无法进入提交支付页。{alt_line}\n"
                        "👉 可改点满起送门店（如发 `点外卖 <备选店名> <菜品>`），或在桌面/手机美团购物车补凑一件。"
                    ),
                }
            # 预览已成功但金额核验失败：如实回报，绝不伪造确认卡
            return {
                "ok": False,
                "preview_failed": True,
                "reason": str(pay_stage.get("reason") or "preview_failed"),
                "shop": matched_shop_name,
                "reply": (
                    f"⚠️ 已真实加购「**{cart_res.get('shop') or matched_shop_name}** — `{cart_res.get('dish') or item_spec_display}`」，"
                    f"但结算页核验失败：{pay_stage.get('error') or pay_stage.get('reason') or '未读到明确实付金额'}。\n"
                    f"未生成付款确认卡；可手动切换到桌面 Chromium 的美团预览页核对，或手机唤端：`{deeplink}`"
                ),
            }
        # 真实加购/预览失败：如实返回失败阶段与原因，不再生成「已备好点餐页」式确认卡
        stage_label = str(cart_res.get("stage") or cart_res.get("reason") or "live_cart_failed")
        # 已进入预览/地址/核验阶段的失败保留标签页，方便用户直接核对现场
        if stage_label not in ("preview", "address", "verify", "checkout"):
            _close_probe_tab()
        reason = str(cart_res.get("error") or "真实加购未完成")
        return {
            "ok": False,
            "live_cart_failed": True,
            "stage": stage_label,
            "reason": cart_res.get("reason") or stage_label,
            "shop": matched_shop_name,
            "delivery_poi": delivery_poi,
            "reply": (
                f"⚠️ 未能真实加购「**{matched_shop_name}** — `{item_spec_display}`」（失败阶段：`{stage_label}`）。\n"
                f"• 原因：{reason}\n"
                "• 司命没有生成付款确认卡，也没有伪造「已备好点餐页」。\n"
                f"👉 可重试，或直接用手机美团唤端：`{deeplink}`"
            ),
        }
    elif probe_tid:
        _close_probe_tab()

    app = create_approval(
        db,
        action="meituan_lock_cart",
        target_id=f"MT-{re.sub(r'[^A-Za-z0-9\u4e00-\u9fff]+', '', matched_shop_name)[:14] or 'ORDER'}",
        params=params,
        ttl_minutes=15,
    )
    db.record_human_use("meituan_agent", "stage_order", ref_id=app["token"])

    meta_parts: list[str] = [f"餐品约 ¥{est_yuan:g}" if est_yuan is not None else "按店内满减实付为准"]
    if delivery_fee_str:
        meta_parts.append(f"配送 {delivery_fee_str}")
    if min_order_yuan is not None:
        meta_parts.append(f"起送 ¥{min_order_yuan:g}")
    if eta_min is not None:
        meta_parts.append(f"{eta_min}分钟")
    if distance_str:
        meta_parts.append(distance_str)
    meta_parts.append(f"单笔上限 ¥{max_yuan:.0f}")

    lines = [
        f"🍱 **美团外卖点餐确认卡（Action Card）** [`{app['token']}`]",
        f"• 主推方案：**{matched_shop_name}** — `{params['item_spec']}`（{' · '.join(meta_parts)}）",
    ]
    if est_yuan is not None:
        from siming.meituan_coupons import infer_order_category
        from siming.meituan_speculate import compute_coupon_pricing, format_coupon_price_line, load_cached_account_coupons

        cached_acc = load_cached_account_coupons(db, max_age_min=720.0)
        if cached_acc:
            m_df2 = re.search(r"(\d+(?:\.\d+)?)", delivery_fee_str)
            df_val = float(m_df2.group(1)) if m_df2 else 0.0
            o_cat = infer_order_category(matched_shop_name, item_names=[str(d.get("name") or "") for d in order_dishes])
            cp_adhoc = compute_coupon_pricing(
                shop_name=matched_shop_name,
                subtotal=float(est_yuan) * max(1, int(order_qty if len(order_dishes) <= 1 else 1)),
                delivery_fee=df_val,
                account_coupons=cached_acc,
                order_category=o_cat,
            )
            if cp_adhoc["total_discount"] > 0 or cp_adhoc.get("near_coupon_tip"):
                lines.append(
                    "• 🎫 券后估算："
                    + format_coupon_price_line(
                        est_subtotal=cp_adhoc["raw_subtotal"],
                        delivery_fee=df_val,
                        est_total=cp_adhoc["raw_total"],
                        coupon_pricing=cp_adhoc,
                    )
                )
    if delivery_poi:
        lines.append(f"• 📍 配送定位：**{delivery_poi}**")
    if meets_min_order is False and min_order_yuan is not None and est_yuan is not None:
        short = round(min_order_yuan - est_yuan, 2)
        lines.append(f"• ⚠️ 提醒：该单品暂差约 ¥{short:g} 满起送价（¥{min_order_yuan:g}），加购后可在购物车凑单或换选免起送门店。")
    if backup_choice:
        lines.append(f"• 备选参考：{backup_choice}")
    if h5_snippet:
        lines.append(f"• 9222 H5 实时检索：`{h5_snippet[:110]}`")
    elif not h5_ready:
        lines.append(f"• 注：当前主浏览器未就绪（{probe.get('error') or '离线'}），确认后将为你打开结算搜索页并附手机一键唤端链接。")
    lines.append(f"👉 15 分钟内发 `apply {app['token']}`（或引用本条回 `确认`），即刻在 9222 后台进店加购并停在「去结算」前供你一键付款！")

    return {
        "ok": True,
        "approval_token": app["token"],
        "shop": matched_shop_name,
        "dish_name": matched_dish_name,
        "dishes": order_dishes,
        "est_yuan": est_yuan,
        "h5_ready": h5_ready,
        "delivery_poi": delivery_poi,
        "reply": "\n".join(lines),
    }


def _live_cart_via_trusted_input(
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
    dishes: Optional[list[dict[str, Any]]] = None,
    spec_prefs: Optional[list[str]] = None,
    poi_id_str: str = "",
    direct_menu_url: str = "",
) -> dict[str, Any]:
    """真实加购并停在结算预览页（兼容餐饮 mindex/menu 与商超 sub-trade/restaurant，支持多菜加购与 poi_id_str 直航）。

    仅在 9222 主 Chromium 后台标签操作（background=True，零焦点抢占），绝不点「提交订单」。
    """
    from siming.meituan_cart_live import live_cart_via_trusted_input

    extra_kw: dict[str, Any] = {}
    if dishes is not None:
        extra_kw["dishes"] = dishes
    if spec_prefs is not None:
        extra_kw["spec_prefs"] = spec_prefs
    if poi_id_str:
        extra_kw["poi_id_str"] = poi_id_str
    if direct_menu_url:
        extra_kw["direct_menu_url"] = direct_menu_url

    return live_cart_via_trusted_input(
        db,
        shop,
        dish_name,
        qty,
        search_keyword=search_keyword,
        location_override=location_override,
        address_hint=address_hint,
        existing_target_id=existing_target_id,
        port=port,
        **extra_kw,
    )


def _live_stage_meituan_cart(
    params: dict[str, Any],
    db: SimingDB,
    port: int = 9222,
    existing_target_id: str = "",
) -> dict[str, Any]:
    """审批通过后按审批参数真实加购并停在结算预览页。

    地址铁律：只认审批 params 里的 `loc_override`（家/公司/新房）驱动坐标与地址选择，
    绝不再按手机实时定位覆盖；`delivery_poi` 仅作为地址文本提示。
    """
    shop = str(params.get("shop") or "").strip()
    dish_name = str(params.get("dish_name") or "").strip()
    qty = int(params.get("qty") or 1)
    raw_dishes = params.get("dishes")
    dishes_arg = list(raw_dishes) if isinstance(raw_dishes, list) and len(raw_dishes) > 1 else None
    raw_specs = params.get("spec_prefs")
    specs_arg = list(raw_specs) if isinstance(raw_specs, list) and raw_specs else None
    return _live_cart_via_trusted_input(
        db,
        shop,
        dish_name,
        qty,
        search_keyword=str(params.get("query") or "").strip(),
        location_override=str(params.get("loc_override") or "").strip(),
        address_hint=str(params.get("delivery_poi") or ""),
        existing_target_id=existing_target_id,
        port=port,
        dishes=dishes_arg,
        spec_prefs=specs_arg,
        poi_id_str=str(params.get("poi_id_str") or ""),
        direct_menu_url=str(params.get("menu_url") or ""),
    )


def execute_meituan_lock_cart(
    params: dict[str, Any],
    db: SimingDB,
    cdp_stage_fn: Optional[Callable[[str, str], dict[str, Any]]] = None,
    cdp_cart_exec_fn: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
) -> str:
    """审批通过后在 9222 后台同步当前位置并推进美团 H5 至选品/加购结算预览（保留标签页不关闭、绝不代输支付密码）。"""
    shop = str(params.get("shop") or "").strip()
    dish_name = str(params.get("dish_name") or "").strip()
    item_spec = str(params.get("item_spec") or "").strip()
    deeplink = str(params.get("deeplink") or f"imeituan://www.meituan.com/waimai/search?query={urllib.parse.quote(shop)}")

    if cdp_stage_fn is not None:
        st_res = cdp_stage_fn(shop, item_spec)
    elif params.get("menu_url") and params.get("dishes"):
        from siming.meituan_speculate import execute_speculative_cart_add

        st_res = execute_speculative_cart_add(db, params, cdp_exec_fn=cdp_cart_exec_fn)
    else:
        st_res = _live_stage_meituan_cart(params, db)
    db.set_meta("last_meituan_order_at", iso_now())

    if st_res.get("gated"):
        gate_r = (st_res.get("gate") or {}).get("reason") or "需登录或滑块确认"
        return (
            f"已在主浏览器后台为你保留「{shop}」页面（状态：{gate_r}），切过去确认一下即可结算；"
            f"或直接在手机点美团唤端秒下单：{deeplink}"
        )
    if st_res.get("ok") and (st_res.get("added_to_cart") or st_res.get("added_dishes")):
        actual_shop = str(st_res.get("shop") or shop)
        added_list = st_res.get("added_dishes") or []
        actual_dish = " + ".join(added_list) if added_list else str(st_res.get("dish") or dish_name or item_spec)
        # 在线真实加购后，直接用已核验的结算预览结果签发付款许可确认卡（绝不再点「去结算」赌 SPA 过渡）
        if (
            cdp_stage_fn is None
            and cdp_cart_exec_fn is None
            and os.environ.get("SIMING_TEST_OFFLINE") != "1"
            and st_res.get("target_id")
        ):
            from siming.meituan_pay import stage_meituan_payment_preview

            pay_stage = stage_meituan_payment_preview(
                db,
                cart_res=st_res,
                shop=actual_shop,
                item_spec=actual_dish,
                deeplink=deeplink,
                preview_fn=(lambda *_args: st_res["preview"]) if st_res.get("preview") else None,
                location_override=str(params.get("loc_override") or ""),
            )
            if pay_stage.get("ok") or pay_stage.get("gated") or pay_stage.get("over_cap"):
                return str(pay_stage["reply"])
            return (
                f"⚠️ 已真实加购「{actual_shop} · {actual_dish}」并停在结算预览页，但付款确认卡核验失败："
                f"{pay_stage.get('error') or pay_stage.get('reason') or '未读到明确实付金额'}。\n"
                f"未生成付款确认卡；请在桌面 Chromium 核对预览页，或手机唤端：{deeplink}"
            )
        details: list[str] = []
        if st_res.get("cart_count"):
            details.append(f"购物车 {st_res['cart_count']} 件")
        if st_res.get("payable_text"):
            details.append(f"餐品 {st_res['payable_text']}")
        if st_res.get("delivery_text"):
            details.append(str(st_res["delivery_text"]))
        if st_res.get("checkout_status"):
            details.append(f"状态：{st_res['checkout_status']}")
        if st_res.get("delivery_poi"):
            details.append(f"送至：{st_res['delivery_poi']}")
        if st_res.get("selected_specs"):
            details.append(f"规格：{st_res['selected_specs']}")
        if st_res.get("failed_dishes"):
            details.append(f"未命中：{'、'.join(st_res['failed_dishes'])}")
        detail_str = f"（{' · '.join(details)}）" if details else ""
        return (
            f"已在桌面主浏览器后台将「{actual_shop} · {actual_dish}」加入美团购物车{detail_str}，"
            f"标签页已停在结算预览页（零焦点打扰、绝不触碰支付），切过去核对后可一键结算或点手机美团唤端付款：{deeplink}"
        )
    if st_res.get("ok"):
        poi_note = f"（定位：{st_res['delivery_poi']}）" if st_res.get("delivery_poi") else ""
        return (
            f"⚠️ 美团页面已打开「{shop} · {item_spec}」{poi_note}，但未确认商品已加入购物车（零焦点打扰），"
            f"请在桌面 Chromium 核对后再结算；也可直接点手机美团唤端：{deeplink}"
        )
    stage_label = str(st_res.get("stage") or st_res.get("reason") or "live_cart_failed")
    reason = str(st_res.get("error") or "真实加购未完成")
    return (
        f"⚠️ 未能真实加购「{shop} · {item_spec}」（失败阶段：`{stage_label}`）：{reason}\n"
        f"未生成付款确认卡；可重试或直接点手机美团唤端：{deeplink}"
    )


# ---------------------------------------------------------------------------
# 历史整单列表与一键复购（「以前买过的菜单 / 上一个南京大排档 / 再来一单」）
# ---------------------------------------------------------------------------

_HISTORY_NON_MEAL_SHOP_RE = re.compile(
    r"(药房|大药房|快药|便利店|超市|乐购达|屈臣氏|名创|五金|鲜花|猫超|小象|果切|水果|生鲜)"
)
_HISTORY_NOISE_ITEM_RE = re.compile(
    r"(餐具|筷勺|筷子|勺子|打包袋|塑料袋|纸巾|湿巾|手套|环保袋|餐盒费|不要餐具|需要餐具|无需餐具|备注"
    r"|点击右上角|收藏领券|关注领券|好评返现|收藏店铺|活动说明|勿拍|温馨提示|下单必看|联系门店|非产品)"
)
_HISTORY_ORDERS_META_KEY = "last_meituan_history_orders"


def _query_recent_meituan_history_orders(
    db: SimingDB,
    *,
    limit: int = 20,
    meals_only: bool = False,
) -> list[dict[str, Any]]:
    """从 `shop_order_items` 聚合最近真实完成的美团整单（自动剔除退款/取消单与餐具/收藏领券等非菜品条目）。"""
    db.init_db()
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT shop, ordered_at, order_status, order_amount_cents, item_name, qty
            FROM shop_order_items
            WHERE platform = 'meituan'
            ORDER BY ordered_at DESC
            LIMIT 1200
            """
        ).fetchall()
    finally:
        conn.close()

    by_order: dict[str, dict[str, Any]] = {}
    order_seq: list[str] = []
    for r in rows:
        st = str(r["order_status"] or "")
        if any(k in st for k in ("取消", "已退款", "退款成功", "全额退款")):
            continue
        shop = str(r["shop"] or "").strip()
        if not shop:
            continue
        if meals_only and _HISTORY_NON_MEAL_SHOP_RE.search(shop):
            continue
        item_name = str(r["item_name"] or "").strip()
        if not item_name or _HISTORY_NOISE_ITEM_RE.search(item_name):
            continue
        occ = str(r["ordered_at"] or "")
        okey = f"{occ}|{shop}"
        if okey not in by_order:
            by_order[okey] = {
                "shop": shop,
                "brand": _shop_brand_key(shop),
                "norm": _normalize_shop_name(shop),
                "ordered_at": occ,
                "amount_yuan": round(int(r["order_amount_cents"] or 0) / 100.0, 1),
                "dishes": [],
            }
            order_seq.append(okey)
        by_order[okey]["dishes"].append({"name": item_name, "qty": max(1, int(r["qty"] or 1))})

    # 按品牌去重保留每家最近一次真实整单（若想看多家历史常点，避免连续 3 单都是同一家）
    out: list[dict[str, Any]] = []
    seen_brands: set[str] = set()
    for okey in order_seq:
        rec = by_order[okey]
        if not rec["dishes"]:
            continue
        bkey = rec["brand"] or rec["norm"]
        if bkey in seen_brands:
            continue
        seen_brands.add(bkey)
        out.append(rec)
        if len(out) >= limit:
            break
    return out


def list_meituan_history_orders(
    db: SimingDB,
    limit: int = 5,
    location_override: str = "",
) -> dict[str, Any]:
    """列出用户最近买过的 5 份历史美团正餐整单，缓存供 QQ 原生按钮一键复购直出付款确认卡。"""
    db.init_db()
    orders = _query_recent_meituan_history_orders(db, limit=max(1, min(5, int(limit or 5))), meals_only=True)
    if not orders:
        orders = _query_recent_meituan_history_orders(db, limit=max(1, min(5, int(limit or 5))), meals_only=False)
    if not orders:
        return {"ok": False, "orders": [], "reply": "暂无美团历史订单记录，可直接发 `点外卖 <店名> <菜品>` 或 `晚餐推荐`。"}

    loc_target = resolve_meituan_delivery_target(db, location_override=location_override)
    loc_label = str(loc_target.get("alias") or loc_target.get("label") or "当前定位")

    cached_entries: list[dict[str, Any]] = []
    lines = [f"🧾 **美团历史常点整单（原样复购 · 定位：{loc_label}）**"]
    for idx, o in enumerate(orders, start=1):
        dish_s = " + ".join(
            f"{d['name']}×{d['qty']}" if int(d["qty"]) > 1 else d["name"]
            for d in o["dishes"][:4]
        )
        md = str(o["ordered_at"])[5:10]
        amt_s = f"¥{o['amount_yuan']:g}" if o["amount_yuan"] > 0 else "按实付为准"
        lines.append(f"{idx}. **{o['shop']}**（{md} · 上次实付 {amt_s}）\n   ↳ {dish_s}")
        cached_entries.append(
            {
                "index": idx,
                "shop": o["shop"],
                "ordered_at": o["ordered_at"],
                "amount_yuan": o["amount_yuan"],
                "dishes": o["dishes"],
                "loc_override": location_override,
            }
        )

    lines.append("👉 点击下方按钮或回复 `复购第1单` / `再来一单 <店名>`（可加「送到公司/送到家」），司命立即按原单自动加购、匹配最优红包并出付款确认卡！")
    payload = {
        "created_at": iso_now(),
        "expires_at": iso_now(now_local() + timedelta(minutes=20)),
        "loc_override": location_override,
        "entries": cached_entries,
    }
    db.set_meta(_HISTORY_ORDERS_META_KEY, json.dumps(payload, ensure_ascii=False))
    db.record_human_use("meituan_agent", "list_history_orders", ref_id=str(len(cached_entries)))
    return {"ok": True, "orders": cached_entries, "reply": "\n".join(lines)}


def _fuzzy_shop_similarity(query: str, candidate: str) -> float:
    """容错店铺名相似度（使「南京大排档」高分命中「南京大牌档(中关村店)」）。"""
    import difflib

    q_brand = _shop_brand_key(query) or _normalize_shop_name(query)
    c_brand = _shop_brand_key(candidate) or _normalize_shop_name(candidate)
    q_norm = _normalize_shop_name(query)
    c_norm = _normalize_shop_name(candidate)
    if not q_brand or not c_brand:
        return 0.0
    if q_norm == c_norm or q_brand == c_brand:
        return 1.0
    if len(q_brand) >= 2 and (q_brand in c_norm or c_brand in q_norm):
        return 0.92
    if len(q_brand) < 2:
        return 0.0
    seq_r = difflib.SequenceMatcher(None, q_brand, c_brand).ratio()
    # 汉字集合重叠率（容忍单字错别字，如 南京大排档 vs 南京大牌档 -> 4/5 = 0.80）
    q_ch = set(re.findall(r"[\u4e00-\u9fff]", q_brand))
    c_ch = set(re.findall(r"[\u4e00-\u9fff]", c_brand))
    overlap = (len(q_ch & c_ch) / float(max(len(q_ch), len(c_ch)))) if (q_ch and c_ch) else 0.0
    return max(seq_r, overlap)


def match_history_order_for_shop(db: SimingDB, query: str) -> Optional[dict[str, Any]]:
    """按「复购第N单 / 第N单」或店铺名（含错别字容错，如「南京大排档」->「南京大牌档(中关村店)」）匹配最近一笔真实历史整单。"""
    q = (query or "").strip()
    if not q:
        return None

    # 1) 优先匹配最近 20 分钟缓存的历史整单序号
    m_idx = re.fullmatch(r"(?:复购)?第?\s*([1-5一二三四五])\s*(?:单|家|个|号)?", q)
    if m_idx:
        cn_map = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5}
        tok_i = m_idx.group(1)
        want_idx = cn_map.get(tok_i) or int(tok_i)
        raw_cache = db.get_meta(_HISTORY_ORDERS_META_KEY) or ""
        if raw_cache:
            try:
                obj = json.loads(raw_cache)
                exp = str(obj.get("expires_at") or "")
                if not exp or parse_iso(exp) >= now_local():
                    for ent in obj.get("entries") or []:
                        if int(ent.get("index") or 0) == want_idx and ent.get("dishes"):
                            return dict(ent)
            except Exception:
                pass
        # 缓存过期时即时回查最近历史整单按序号命中
        fresh = _query_recent_meituan_history_orders(db, limit=5, meals_only=True)
        if 1 <= want_idx <= len(fresh):
            return dict(fresh[want_idx - 1])
        return None

    # 2) 按店名（含错别字容错）查询历史最近整单
    orders = _query_recent_meituan_history_orders(db, limit=40, meals_only=False)
    best_score = 0.0
    best_order: Optional[dict[str, Any]] = None
    for o in orders:
        sc = _fuzzy_shop_similarity(q, str(o["shop"]))
        if sc > best_score:
            best_score = sc
            best_order = o
    if best_order is not None and best_score >= 0.72:
        return dict(best_order)
    return None


_DISH_COLLOQUIAL_FILLER_RE = re.compile(
    r"(?:之前那个|上次那个|以前那个|那个|那道|那份|那碗|那杯|这个|这道|这份|一下|的)"
)
_DISH_GENERIC_CLASS_SUFFIX_RE = re.compile(
    r"(?:凉菜|冷菜|冷盘|热菜|小菜|素菜|荤菜|配菜|菜品|菜|主食|小吃|小食|点心|甜品|饮品|饮料|汤)$"
)
_CULINARY_SYNONYM_GROUPS: tuple[tuple[str, ...], ...] = (
    ("芫荽", "香菜", "芫"),
    ("西红柿", "番茄"),
    ("土豆", "马铃薯", "洋芋"),
    ("花菜", "菜花", "西兰花"),
    ("羊肚", "百叶", "毛肚"),
    ("鸡爪", "凤爪"),
    ("米饭", "白饭", "大米饭"),
)
_COMMON_DISH_STOP_CHARS = frozenset(
    "菜肉饭面汤粉大小辣香鲜甜酸炒拌蒸煮烧烤炸煎炖烩老新招牌精选特色秘制家常风味口感升级标准人份个的了凉冷热小素荤配味汁油盐糖醋葱姜蒜椒"
)


def _clean_colloquial_dish_hint(raw_dish: str) -> tuple[str, str]:
    """清理口语菜名指代词，返回 `(core_hint, generic_class_suffix)`（如「芫荽那个凉菜」-> `('芫荽', '凉菜')`）。"""
    s = re.sub(r"[\s，,。！!？?]+", "", (raw_dish or "").strip())
    if not s:
        return "", ""
    s_no_fill = _DISH_COLLOQUIAL_FILLER_RE.sub("", s)
    cls_suffix = ""
    m_cls = _DISH_GENERIC_CLASS_SUFFIX_RE.search(s_no_fill)
    if m_cls and m_cls.start() >= 2:
        cls_suffix = m_cls.group(0)
        core = s_no_fill[: m_cls.start()]
    else:
        core = s_no_fill
    return core or s, cls_suffix


def resolve_shop_history_dish_alias(db: SimingDB, shop: str, raw_dish: str) -> str:
    """将同门店口语加菜描述（如「芫荽那个凉菜」「羊肚」）优先解析为该店历史买过的真实菜名（如「御膳芫炝羊肚」）。"""
    q_raw = (raw_dish or "").strip()
    if not q_raw or not shop:
        return q_raw
    core, _cls = _clean_colloquial_dish_hint(q_raw)
    if not core:
        return q_raw

    brand_key = _shop_brand_key(shop) or _normalize_shop_name(shop)
    db.init_db()
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT shop, item_name, order_status
            FROM shop_order_items
            WHERE platform = 'meituan'
            ORDER BY ordered_at DESC
            LIMIT 1200
            """
        ).fetchall()
    finally:
        conn.close()

    hist_dishes: list[str] = []
    seen: set[str] = set()
    for r in rows:
        st = str(r["order_status"] or "")
        if any(k in st for k in ("取消", "已退款", "退款成功", "全额退款")):
            continue
        r_shop = str(r["shop"] or "").strip()
        if not r_shop:
            continue
        r_brand = _shop_brand_key(r_shop) or _normalize_shop_name(r_shop)
        if r_brand != brand_key and _fuzzy_shop_similarity(shop, r_shop) < 0.78:
            continue
        nm = str(r["item_name"] or "").strip()
        if not nm or _HISTORY_NOISE_ITEM_RE.search(nm):
            continue
        if nm not in seen:
            seen.add(nm)
            hist_dishes.append(nm)

    if not hist_dishes:
        return q_raw

    # 展开同义烹饪词（如 芫荽 <-> 香菜 / 芫）
    syn_terms: list[str] = [core]
    for grp in _CULINARY_SYNONYM_GROUPS:
        if any(t in core for t in grp):
            for t in grp:
                if t not in syn_terms:
                    syn_terms.append(t)

    scored: list[tuple[float, str]] = []
    for nm in hist_dishes:
        nm_clean = re.sub(r"（[^）]*）|\([^)]*\)", "", nm).strip() or nm
        if nm == q_raw or nm_clean == core:
            return nm
        sc = 0.0
        if len(core) >= 2 and (core in nm or core in nm_clean):
            sc = max(sc, 92.0 - min(8.0, max(0, len(nm_clean) - len(core)) * 0.4))
        for syn in syn_terms[1:]:
            if len(syn) >= 2 and (syn in nm or syn in nm_clean):
                sc = max(sc, 86.0)
        # 稀有食材特征字命中（如「芫荽」首字「芫」唯一命中「御膳芫炝羊肚」）
        rare_chars = [ch for ch in core if "\u4e00" <= ch <= "\u9fff" and ch not in _COMMON_DISH_STOP_CHARS]
        for syn in syn_terms[1:]:
            if len(syn) == 1 and "\u4e00" <= syn <= "\u9fff" and syn not in _COMMON_DISH_STOP_CHARS and syn not in rare_chars:
                rare_chars.append(syn)
        if sc < 80.0 and rare_chars:
            matched_rare = [ch for ch in rare_chars if ch in nm_clean]
            if matched_rare:
                # 仅在稀有字命中时计分；若多个候选均命中同名稀有字则后续用唯一胜出校验过滤
                sc = max(sc, 70.0 + min(12.0, len(matched_rare) * 6.0))
        if sc > 0:
            scored.append((sc, nm))

    if not scored:
        return q_raw
    scored.sort(key=lambda x: x[0], reverse=True)
    top_sc, top_nm = scored[0]
    second_sc = scored[1][0] if len(scored) > 1 else 0.0
    if top_sc >= 85.0:
        return top_nm
    # 稀有字匹配必须唯一胜出，绝不盲猜歧义菜品
    if top_sc >= 74.0 and (len(scored) == 1 or top_sc - second_sc >= 6.0):
        return top_nm
    return q_raw


def stage_meituan_history_reorder(
    db: SimingDB,
    target: str,
    location_override: str = "",
    cdp_probe_fn: Optional[Callable[[str], dict[str, Any]]] = None,
    auto_preview_pay: bool = True,
    extra_dishes: Optional[list[dict[str, Any]]] = None,
    exclude_dishes: Optional[list[str]] = None,
) -> dict[str, Any]:
    """按历史整单（如 `复购第1单` / `南京大排档`）一键原样或增量改菜加购，并生成 `meituan_submit_pay` 付款确认卡（零扣款）。"""
    db.init_db()
    hit = match_history_order_for_shop(db, target)
    if hit is None:
        return {
            "ok": False,
            "not_found": True,
            "reply": f"⚠️ 在历史美团订单里没找到「**{target}**」，可发 `历史菜单` 查看最近买过的 5 单并点击按钮复购。",
        }
    shop = str(hit["shop"])
    dishes = [
        {"name": str(d["name"]).strip(), "qty": max(1, int(d.get("qty") or 1))}
        for d in (hit.get("dishes") or [])
        if isinstance(d, dict) and str(d.get("name") or "").strip()
    ]

    # 1) 可选剔除用户明确不要的旧菜（如「不要米饭」「去掉鸭肠」）
    excluded_names: list[str] = []
    if exclude_dishes:
        clean_excs = [
            _clean_colloquial_dish_hint(str(x))[0] or str(x).strip()
            for x in exclude_dishes
            if str(x or "").strip()
        ]
        if clean_excs:
            kept: list[dict[str, Any]] = []
            for d in dishes:
                d_nm = str(d["name"])
                if any(ex and (ex in d_nm or d_nm in ex) for ex in clean_excs):
                    excluded_names.append(d_nm)
                else:
                    kept.append(d)
            dishes = kept

    # 2) 可选追加新菜（支持口语指代自动映射回同店历史真实菜名，如「芫荽那个凉菜」->「御膳芫炝羊肚」；同菜自动累加份数）
    resolved_extras: list[dict[str, Any]] = []
    if extra_dishes:
        for raw_ed in extra_dishes:
            if isinstance(raw_ed, str):
                raw_nm, ed_qty = raw_ed.strip(), 1
            elif isinstance(raw_ed, dict):
                raw_nm = str(raw_ed.get("name") or "").strip()
                ed_qty = max(1, int(raw_ed.get("qty") or 1))
            else:
                continue
            if not raw_nm:
                continue
            resolved_nm = resolve_shop_history_dish_alias(db, shop, raw_nm)
            resolved_extras.append({"raw": raw_nm, "resolved": resolved_nm, "qty": ed_qty})
            merged = False
            for d in dishes:
                if d["name"] == resolved_nm or (len(resolved_nm) >= 2 and resolved_nm in d["name"]):
                    d["qty"] = min(10, int(d["qty"]) + ed_qty)
                    merged = True
                    break
            if not merged:
                dishes.append({"name": resolved_nm, "qty": ed_qty})

    if not dishes:
        return {"ok": False, "reply": f"⚠️ 历史订单「**{shop}**」缺少有效菜品明细，请直接发 `点外卖 {shop} <菜名>`。"}
    eff_loc = (location_override or "").strip() or str(hit.get("loc_override") or "").strip()
    dish_str = " + ".join(f"{d['name']}×{d['qty']}" if d["qty"] > 1 else d["name"] for d in dishes)
    res = stage_meituan_h5_order(
        db,
        f"{shop} {dish_str}",
        cdp_probe_fn=cdp_probe_fn,
        auto_preview_pay=auto_preview_pay,
        location_override=eff_loc,
        explicit_shop=shop,
        explicit_dishes=dishes,
    )
    return {
        **res,
        "history_reorder": True,
        "matched_history_shop": shop,
        "matched_history_dishes": dishes,
        "resolved_extra_dishes": resolved_extras,
        "excluded_dishes": excluded_names,
    }
