"""Wave 1 (W1-3 · sub_hunter)：订阅刺客猎手——自动识别周期扣费、涨价刺客、僵尸订阅与 48h 止损卡。

能力：
1. 扫描 `ledger_txns` 与邮箱续费 `events`/`cases`，严格过滤外卖/打车/咖啡/商超/停车/快递/单次网购等日常消费，
   通过 Tier 1（显式订阅/会员关键词 + 5~400 天合理周期）与 Tier 2（未知商户 ≥3 次、金额波动 ≤±12%、标准账期且间隔 CV ≤0.25）双层规则精准识别真订阅；
2. 推断扣费周期（7d / 14d / 30d / 90d / 365d）、识别悄悄涨价（> 5%）、标出低利用率僵尸候选，并自动同步生成 Sure 周期规则；
3. 提供 `purge_false_positive_subscriptions(db)` 清理历史误报订阅与对应的 `sure_rules` 自动周期规则（保留用户显式标记的 `KEEP`/`CANCELLED`）；
4. 扣费前 48 小时或新发现涨价时推送 QQ 止损卡（含 `保留 / 去取消 / 记为僵尸` 快捷按钮）；
5. 支持一键标记 `KEEP / ZOMBIE / CANCELLED / ACTIVE`。
"""

from __future__ import annotations

import hashlib
import json
import re
import statistics
from datetime import datetime, timedelta
from typing import Any, Optional

from siming import db as _db_mod
from siming.db import SimingDB, iso_now, now_local, parse_iso
from siming.modules import is_module_enabled
from siming.qq_cards import make_button, make_keyboard
from siming.sure_bridge import upsert_sure_rule

_SUB_FX_TO_CNY: dict[str, float] = {
    "CNY": 1.0,
    "RMB": 1.0,
    "USD": 7.25,
    "JPY": 0.048,
    "EUR": 7.85,
    "GBP": 9.15,
    "SGD": 5.40,
    "HKD": 0.93,
    "AUD": 4.75,
}

_ANNUAL_HINT_RE = re.compile(
    r"(包年|年费|年卡|年度会员|年会员|连续包年|88\s*vip|京东\s*plus|jd\s*plus|annual|yearly|365天)",
    re.IGNORECASE,
)

# 剥离历史金额先验推断备注，避免外卖/日常消费因 notes 含 `[历史推断: Apple 订阅服务]` 被误归为订阅
_PRIOR_NOTE_RE = re.compile(r"\[历史推断:[^\]]*\]")

# 商超/门店名中自带"会员"二字但不代表订阅扣费的常见误导词
_STORE_MEMBER_NOISE_RE = re.compile(r"(山姆会员商店|山姆会员店|会员商店|会员店|会员甄选|会员专享价|会员日)")

# 明确表示订阅/会员续费商品的覆盖词（允许从通用平台如 美团神会员 / 盒马88VIP / 京东PLUS / 小桔充电会员自动续费 中放行）
_EXPLICIT_SUB_OVERRIDE_RE = re.compile(
    r"(连续包月|连续包年|连续包季|连续包周|自动续费|自动续期|包月|包年|包季|包周|"
    r"月度会员|年度会员|超级会员|大会员|专属会员|神会员|美团会员|美团神会员|充电会员|"
    r"88\s*vip|jd\s*plus|京东\s*plus|plus\s*会员|会员自动续费|会员续费|会员年费|会员费|"
    r"\d+\s*天会员|svip|vip\s*月卡|vip\s*年卡|体育\s*vip|大师会员|"
    r"subscription|auto[- ]?renew|premium\s*connectivity|高级车载娱乐)",
    re.IGNORECASE,
)

# 全面日常非订阅消费排除黑名单
_NON_SUB_DENYLIST_RE = re.compile(
    r"("
    # 外卖与即时零售
    r"美团|美团外卖|美团平台|美团闪购|美团买菜|meituan|饿了么|eleme|小象超市|外卖柜|取餐柜|外卖|"
    # 出行打车与公共交通
    r"滴滴|didi|打车|高德打车|花小猪|曹操出行|首汽约车|t3出行|哈啰|青桔|摩拜|单车骑行|"
    r"一卡通|地铁|公交|12306|铁路|国航|东航|南航|海航|航空|机票|机场|酒店|民宿|携程|去哪儿|飞猪|"
    # 咖啡茶饮
    r"瑞幸|luckin|星巴克|starbucks|霸王茶姬|库迪|cotti|喜茶|蜜雪|奈雪|茶百道|古茗|一点点|沪上阿姨|"
    r"奶茶|咖啡|coffee|cafe|manner|tims|"
    # 便利店与商超生鲜
    r"便利店|便利蜂|7-?11|罗森|lawson|全家|familymart|物美|永辉|超市|盒马|山姆|samsclub|"
    r"叮咚|菜场|菜市场|果蔬|生鲜|沃尔玛|大润发|屈臣氏|名创优品|无印良品|宜家|ikea|多点|"
    # 餐饮小吃零食
    r"餐厅|餐饮|饭店|大排档|食堂|小吃|烧烤|小串|烤肉|火锅|面馆|拉面|米粉|螺蛳粉|酸辣粉|米线|"
    r"瓦罐|饺子|包子|煎饼|肉夹馍|酸奶|果切|水果|鲜果|面包|蛋糕|烘焙|甜品|肯德基|kfc|麦当劳|mcdonald|"
    r"汉堡|必胜客|披萨|沙拉|轻食|麻辣烫|烤鱼|清真菜|川菜|湘菜|粤菜|烤鸭|老乡鸡|西贝|海底捞|"
    r"眉州东坡|南京大牌档|素坤泰|示例粉面|示例粉面|示例瓦罐汤|粉面|"
    # 停车高速加油充电车务
    r"停车|泊车|etcp|驿停车|捷停车|车库|高速|etc|速通卡|通行费|加油|中石化|中石油|壳牌|"
    r"超充|特斯拉|tesla|充电|特来电|星星充电|国家电网|小桔充电|洗车|"
    # 快递物流与柜机
    r"快递|丰巢|菜鸟|顺丰|中通|圆通|韵达|申通|极兔|德邦|京东物流|ems|邮政|寄件|"
    # 转账金融与水电物业
    r"转账|红包|还款|信用卡|花呗|借呗|白条|理财|基金|股票|证券|保险|公积金|"
    r"水费|电费|燃气费|物业费|供暖|静态码|二维码收款|扫码付|先乘车后付款|先乘后付|先充后付|"
    # 医疗药房
    r"医院|门诊|挂号|诊所|药房|药店|大药房|体检|口腔|牙科|"
    # 综合电商单次零售 & 单次购买
    r"淘宝|天猫|拼多多|京东商城|京东平台|京东自营|京东\s*·|抖音电商|抖音商城|抖音红包|"
    r"得物|闲鱼|唯品会|小米商城|小米\*\*店|苏宁|当当|订单编号|便利蜂购物|steam\s*purchase|单场"
    r")",
    re.IGNORECASE,
)

# Tier 1：显式订阅/会员服务关键词
_TIER1_KEYWORD_RE = re.compile(
    r"("
    r"会员|vip|svip|连续包月|连续包年|连续包季|连续包周|包月|包年|包季|包周|续费|自动续期|订阅|subscription|auto[- ]?renew|"
    r"icloud|apple\.com/bill|apple\s*服务|apple\s*music|apple\s*one|app\s*store|itunes|"
    r"点众|快看|番茄小说|七猫|起点读书|阅文|繁花剧场|常读小说|"
    r"game\s*pass|xbox|playstation|ps\s*plus|nintendo\s*switch\s*online|steam|"
    r"microsoft\s*365|pc\s*版\s*1\s*[个個]月|"
    r"迅雷|xunlei|百度网盘|百度文库|夸克网盘|阿里网盘|阿里云盘|"
    r"腾讯视频|爱奇艺|优酷|芒果tv|哔哩哔哩|bilibili|b站大会员|"
    r"网易云音乐|qq音乐|腾讯音乐|spotify|youtube\s*premium|youtube|netflix|disney\+|"
    r"chatgpt|openai|claude|anthropic|cursor|copilot|github\s*(?:pro|copilot)|"
    r"notion|1password|jetbrains|kimi\s*vip|codeplan|m\s*plan|"
    r"樊登|得到|微信读书|知乎盐选|知乎\s*·\s*连续包月|wps|飞书|剪映|"
    r"抖音.*(?:小说|短剧|vip|连续包月)|和多号|watt\s*svip|88vip|京东\s*plus|jd\s*plus|美团神会员"
    r")",
    re.IGNORECASE,
)

_SUB_ALIASES: tuple[tuple[str, str, bool], ...] = (
    # (normalized_name, regex, zombie_prone)
    ("iCloud+", r"(icloud|apple\s*icloud)", False),
    ("Apple 服务", r"(apple\.com/bill|app\s*store|apple\s*服务|itunes|apple\s*music|apple\s*one)", False),
    ("点众小说", r"(点众|快看小说|番茄小说|七猫|阅文|起点读书)", True),
    ("快看", r"(快看漫画|快看)", True),
    ("PC Game Pass", r"(game\s*pass|xbox\s*game\s*pass|microsoft\s*pc\s*game|pgpass|microsoft\s*payments.*pc\s*版\s*1\s*[个個]月)", True),
    ("Microsoft 365", r"(microsoft\s*365)", False),
    ("迅雷", r"(迅雷|xunlei)", True),
    ("抖音小说/短剧VIP", r"(抖音.*(?:小说|短剧|连续包月|vip|会员)|字节跳动小程序)", True),
    ("百度网盘", r"(百度网盘|baidu\s*netdisk)", True),
    ("百度文库VIP", r"(百度文库\s*vip|百度文库.*连续包月)", True),
    ("夸克网盘", r"(夸克网盘|quark\s*网盘)", True),
    ("阿里云盘", r"(阿里云盘|阿里网盘)", True),
    ("网易云音乐", r"(网易云音乐|cloudmusic)", False),
    ("QQ音乐", r"(qq音乐|腾讯音乐)", False),
    ("B站大会员", r"(哔哩哔哩|bilibili|b站大会员)", False),
    ("爱奇艺/优酷/腾讯视频", r"(爱奇艺|优酷|腾讯视频|芒果tv)", True),
    ("YouTube Premium", r"(youtube\s*premium|google\s*youtube)", False),
    ("Spotify", r"(spotify)", False),
    ("Netflix", r"(netflix)", False),
    ("GitHub Copilot", r"(github\s*copilot|github\s*pro)", False),
    ("ChatGPT Plus", r"(chatgpt|openai\s*plus)", False),
    ("Claude Pro", r"(claude\s*(?:pro|max|4)|anthropic\s*pro|克劳德.*(?:订阅|续费|会员))", False),
    ("Cursor", r"(cursor\s*(?:pro|subscription|ai))", False),
    ("Notion", r"(notion\s*(?:plus|pro|ai|subscription))", False),
    ("1Password", r"(1password)", False),
    ("JetBrains", r"(jetbrains)", False),
    ("Kimi VIP", r"(kimi\s*vip|月之暗面.*vip)", False),
    ("MiniMax 会员", r"(稀宇科技.*(?:会员|codeplan|m\s*plan))", False),
    ("知乎盐选", r"(知乎盐选|知乎\s*·\s*连续包月|知乎\s*·\s*在广告平台投放落地页使用)", True),
    ("常读小说", r"(常读小说)", True),
    ("微信读书", r"(微信读书)", False),
    ("得到", r"(得到\s*(?:会员|vip|包月|包年))", False),
    ("樊登读书", r"(樊登)", True),
    ("WPS会员", r"(wps\s*(?:会员|超级会员|vip|连续包))", False),
    ("飞书", r"(飞书\s*(?:会员|专业版|订阅|续费))", False),
    ("剪映", r"(剪映\s*(?:会员|vip|连续包))", False),
    ("繁花剧场", r"(繁花剧场)", True),
    ("Watt Toolkit SVIP", r"(watt\s*toolkit|watt\s*svip)", True),
    ("中国移动和多号", r"(和多号)", False),
    ("Grab Subscription", r"(grabrewardsubscription|grab.*subscription)", True),
    ("京东PLUS", r"(京东\s*plus|jd\s*plus)", False),
    ("88VIP", r"(88\s*vip)", False),
    ("美团神会员", r"(美团神会员|美团会员)", False),
    ("小桔充电会员", r"(小桔.*会员)", True),
    ("网易邮箱大师会员", r"(网易邮箱.*会员)", True),
    ("央视体育VIP", r"(央视网.*体育\s*vip)", True),
)

_RENEWAL_EVENT_RE = re.compile(
    r"(续费|自动扣费|自动续期|连续包月|连续包年|subscription|renewal|auto[- ]?renew|billing|会员到期|扣款预告)",
    re.IGNORECASE,
)
_AMOUNT_RE = re.compile(r"(?:¥|￥|RMB|CNY|\$|USD)\s*(\d+(?:\.\d{1,2})?)|(\d+(?:\.\d{1,2})?)\s*元", re.IGNORECASE)
_CADENCE_DAYS_CANDIDATES = (7, 14, 30, 90, 365)
# Tier 2 标准账期容忍窗口: (target_days, tolerance_days)
_STANDARD_CADENCE_WINDOWS: tuple[tuple[int, float], ...] = (
    (7, 2.0),
    (14, 2.0),
    (30, 4.0),
    (90, 7.0),
    (365, 10.0),
)


def _strip_prior_notes(notes: str) -> str:
    return _PRIOR_NOTE_RE.sub("", notes or "").strip()


def _strip_store_member_noise(text: str) -> str:
    return _STORE_MEMBER_NOISE_RE.sub("", text or "")


def is_excluded_non_subscription(merchant: str, notes: str = "") -> bool:
    """判断商户/备注是否属于日常非订阅消费黑名单（外卖、打车、咖啡、商超、停车、快递、单次网购等）。"""
    m_raw = (merchant or "").strip()
    n_clean = _strip_prior_notes(notes)
    if not m_raw and not n_clean:
        return True
    if m_raw == "未知订阅":
        return True

    m_no_store = _strip_store_member_noise(m_raw)
    if m_raw:
        # 优先在原始商户名上做显式会员商品覆盖判断（如「美团神会员」「盒马X 88VIP专属会员」「小桔充电会员自动续费」）
        if _EXPLICIT_SUB_OVERRIDE_RE.search(m_no_store):
            # 但单次电商订单（如「超市**店 · 会员甄选...」）或单场体育赛事不算订阅
            if re.search(r"(单场|订单编号|代购|等多件)", m_raw):
                return True
            return False
        if _NON_SUB_DENYLIST_RE.search(m_raw):
            return True

    combined = f"{m_raw} {n_clean}".strip()
    combined_no_store = _strip_store_member_noise(combined)
    if _EXPLICIT_SUB_OVERRIDE_RE.search(combined_no_store):
        if re.search(r"(单场|订单编号|代购|等多件)", combined):
            return True
        return False
    if _NON_SUB_DENYLIST_RE.search(combined):
        return True
    return False


def is_explicit_subscription_merchant(merchant: str, notes: str = "") -> bool:
    """判断是否命中 Tier 1 显式订阅/会员关键词（已剥离先验备注与商超门店名误导词）。"""
    if is_excluded_non_subscription(merchant, notes):
        return False
    hay = _strip_store_member_noise(f"{merchant or ''} {_strip_prior_notes(notes)}".strip())
    if not hay:
        return False
    return bool(_TIER1_KEYWORD_RE.search(hay))


def _normalize_merchant(raw_merchant: str, notes: str = "") -> tuple[str, bool]:
    clean_notes = _strip_prior_notes(notes)
    hay = f"{raw_merchant or ''} {clean_notes}".strip()
    for norm, pat, zombie_prone in _SUB_ALIASES:
        if re.search(pat, hay, re.IGNORECASE):
            return norm, zombie_prone
    cleaned = re.sub(r"[\(（\[【].*?[\)）\]】]", "", raw_merchant or "").strip()
    cleaned = re.sub(r"\s*[-_#]?\d{4,}$", "", cleaned).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned[:40] or "未知订阅", False


def _snap_cadence(intervals_days: list[float], hint_text: str = "") -> int:
    low = (hint_text or "").lower()
    if _ANNUAL_HINT_RE.search(low):
        return 365
    if any(k in low for k in ("包季", "季卡", "quarterly", "90天")):
        return 90
    if any(k in low for k in ("包周", "周卡", "weekly", "7天")):
        return 7
    if any(k in low for k in ("双周", "14天", "biweekly")):
        return 14
    if not intervals_days:
        return 30
    med = float(statistics.median(intervals_days))
    best = min(_CADENCE_DAYS_CANDIDATES, key=lambda c: abs(c - med))
    if abs(best - med) <= max(4.0, best * 0.25):
        return best
    return max(1, int(round(med)))


def _match_standard_cadence(median_days: float) -> Optional[int]:
    for target, tol in _STANDARD_CADENCE_WINDOWS:
        if abs(median_days - float(target)) <= tol:
            return target
    return None


def _compute_unique_day_intervals(recs: list[dict[str, Any]]) -> list[float]:
    """按唯一扣费日期计算相邻间隔天数（避免同日重复/刷卡导致伪间隔，同时不再丢弃 <3 天高频消费而误拟合 7 天周期）。"""
    dts: list[datetime] = []
    for r in recs:
        try:
            dts.append(parse_iso(str(r["occurred_at"])))
        except Exception:
            pass
    if len(dts) < 2:
        return []
    # 同一天多笔合并为当天最晚时间戳，保留真实的跨日频率
    by_day: dict[str, datetime] = {}
    for dt in sorted(dts):
        by_day[dt.date().isoformat()] = dt
    ordered = [by_day[k] for k in sorted(by_day.keys())]
    if len(ordered) < 2:
        return []
    return [(ordered[i] - ordered[i - 1]).total_seconds() / 86400.0 for i in range(1, len(ordered))]


def _qualifies_as_subscription(
    *,
    norm: str,
    recs: list[dict[str, Any]],
    intervals: list[float],
    explicit_renewal: bool,
    is_tier1: bool,
    min_occurrences: int,
    hint_text: str = "",
) -> tuple[bool, Optional[int], str]:
    """双层精准订阅判定：
    - Tier 1（显式订阅/会员关键词）：显式续费事件（1+ 次）、单次显式包年/年度会员（如 88VIP / 京东PLUS），
      或 >= max(2, min_occurrences) 次且中位间隔落在 5..400 天；
    - Tier 2（未知商户无显式关键词）：未命中黑名单且 >= max(3, min_occurrences) 次、金额波动 <= ±12%、
      中位间隔命中标准账期（7±2 / 14±2 / 30±4 / 90±7 / 365±10 天）且间隔变异系数 CV <= 0.25。
    """
    if not recs or norm == "未知订阅":
        return False, None, "empty_or_unknown"

    if explicit_renewal:
        if intervals:
            med = float(statistics.median(intervals))
            if med < 3.0 or med > 420.0:
                return False, None, "implausible_renewal_cadence"
        return True, None, "explicit_renewal"

    if is_tier1:
        if len(recs) == 1 and _ANNUAL_HINT_RE.search(f"{norm} {hint_text}"):
            return True, 365, "tier1_explicit_annual"
        if len(recs) < max(2, int(min_occurrences)):
            return False, None, "tier1_too_few_occurrences"
        if not intervals:
            return False, None, "tier1_no_intervals"
        med = float(statistics.median(intervals))
        if not (5.0 <= med <= 400.0):
            return False, None, "tier1_implausible_cadence"
        return True, None, "tier1_explicit_keyword"

    # Tier 2：未知商户严格统计门控
    if len(recs) < max(3, int(min_occurrences)):
        return False, None, "tier2_too_few_occurrences"
    if len(intervals) < 2:
        return False, None, "tier2_too_few_intervals"

    amounts = [int(r.get("amount_cents") or 0) for r in recs if int(r.get("amount_cents") or 0) > 0]
    if len(amounts) < 3:
        return False, None, "tier2_insufficient_amounts"
    med_amt = float(statistics.median(amounts))
    if med_amt <= 0:
        return False, None, "tier2_zero_amount"
    if (max(amounts) - min(amounts)) > 0.12 * med_amt:
        return False, None, "tier2_amount_variance_too_high"

    med_iv = float(statistics.median(intervals))
    snapped = _match_standard_cadence(med_iv)
    if snapped is None:
        return False, None, "tier2_non_standard_cadence"

    mean_iv = float(statistics.mean(intervals))
    if mean_iv <= 0:
        return False, None, "tier2_bad_mean_interval"
    cv = float(statistics.stdev(intervals)) / mean_iv
    if cv > 0.25:
        return False, None, "tier2_interval_cv_too_high"

    return True, snapped, "tier2_regular_cadence"


def _make_sub_id(normalized_name: str) -> str:
    digest = hashlib.sha1(normalized_name.lower().encode("utf-8")).hexdigest()[:10]
    return f"SUB-{digest}"


def _extract_event_amount_cents(text: str, payload: dict[str, Any]) -> tuple[int, str]:
    if payload.get("amount_cents") is not None:
        return int(payload["amount_cents"]), str(payload.get("currency") or "CNY").upper()
    if payload.get("amount_yuan") is not None:
        return int(round(float(payload["amount_yuan"]) * 100)), str(payload.get("currency") or "CNY").upper()
    m = _AMOUNT_RE.search(text or "")
    if m:
        raw_num = m.group(1) or m.group(2)
        curr = "USD" if ("$" in m.group(0) or "usd" in m.group(0).lower()) else "CNY"
        return int(round(float(raw_num) * 100)), curr
    return 0, "CNY"


def _collect_candidate_groups(
    txns: list[dict[str, Any]],
    ev_rows: list[dict[str, Any]],
    case_rows: list[dict[str, Any]],
    ts: str,
) -> dict[str, dict[str, Any]]:
    """将 ledger_txns / events / cases 过滤日常消费黑名单后按归一商户聚合。"""
    groups: dict[str, dict[str, Any]] = {}

    def _add_record(grp: dict[str, Any], rec: dict[str, Any]) -> None:
        dedup_key = (str(rec.get("occurred_at") or "")[:16], int(rec.get("amount_cents") or 0))
        seen: set[tuple[str, int]] = grp["_seen"]
        if dedup_key in seen and not rec.get("next_expected_at"):
            return
        seen.add(dedup_key)
        grp["records"].append(rec)

    for t in txns:
        merchant = str(t.get("merchant") or "").strip()
        notes = str(t.get("notes") or "").strip()
        clean_notes = _strip_prior_notes(notes)
        if not merchant and not clean_notes:
            continue
        if is_excluded_non_subscription(merchant, clean_notes):
            continue
        norm, zombie_prone = _normalize_merchant(merchant, clean_notes)
        if norm == "未知订阅" or is_excluded_non_subscription(norm):
            continue
        tier1 = is_explicit_subscription_merchant(merchant, clean_notes) or is_explicit_subscription_merchant(norm)
        g = groups.setdefault(
            norm,
            {
                "normalized_name": norm,
                "merchant": merchant or norm,
                "zombie_prone": zombie_prone,
                "records": [],
                "explicit_renewal": False,
                "is_tier1": False,
                "hint_text": "",
                "_seen": set(),
            },
        )
        if tier1:
            g["is_tier1"] = True
        if zombie_prone:
            g["zombie_prone"] = True
        _add_record(
            g,
            {
                "occurred_at": str(t["occurred_at"]),
                "amount_cents": int(t["amount_cents"]),
                "currency": str(t.get("currency") or "CNY").upper(),
                "merchant": merchant or norm,
            },
        )
        g["hint_text"] += f" {merchant} {clean_notes}"

    # 融入邮箱/通知续费事件（跳过导出账单流水副本与黑名单日常消费）
    for ev in ev_rows:
        source = str(ev.get("source") or "").lower()
        if source.startswith("export_"):
            continue
        summary = str(ev.get("summary") or "")
        try:
            payload = json.loads(ev.get("payload_json") or "{}")
            if not isinstance(payload, dict):
                payload = {}
        except Exception:
            payload = {}
        hay = f"{summary} {payload.get('merchant') or ''} {payload.get('body') or ''}"
        if not (_RENEWAL_EVENT_RE.search(hay) or payload.get("is_subscription")):
            continue
        raw_m = str(payload.get("merchant") or summary)
        if is_excluded_non_subscription(raw_m, hay):
            continue
        norm, zombie_prone = _normalize_merchant(raw_m, hay)
        if norm == "未知订阅" or is_excluded_non_subscription(norm):
            continue
        amt_c, curr = _extract_event_amount_cents(hay, payload)
        g = groups.setdefault(
            norm,
            {
                "normalized_name": norm,
                "merchant": str(payload.get("merchant") or norm),
                "zombie_prone": zombie_prone,
                "records": [],
                "explicit_renewal": True,
                "is_tier1": True,
                "hint_text": "",
                "_seen": set(),
            },
        )
        g["explicit_renewal"] = True
        g["is_tier1"] = True
        g["hint_text"] += f" {hay}"
        if amt_c > 0 or payload.get("next_expected_at"):
            _add_record(
                g,
                {
                    "occurred_at": str(payload.get("charged_at") or ev["occurred_at"]),
                    "amount_cents": amt_c,
                    "currency": curr,
                    "merchant": str(payload.get("merchant") or norm),
                    "next_expected_at": str(payload.get("next_expected_at") or ""),
                    "cadence_days": int(payload.get("cadence_days") or 0),
                },
            )

    # 融入 sub_renew cases
    for c in case_rows:
        title = str(c.get("title") or "")
        ns = str(c.get("next_step") or "")
        try:
            ctx = json.loads(c.get("context_json") or "{}")
            if not isinstance(ctx, dict):
                ctx = {}
        except Exception:
            ctx = {}
        hay = f"{title} {ns}"
        raw_m = str(ctx.get("merchant") or title)
        if is_excluded_non_subscription(raw_m, hay):
            continue
        norm, zombie_prone = _normalize_merchant(raw_m, hay)
        if norm == "未知订阅" or is_excluded_non_subscription(norm):
            continue
        amt_c, curr = _extract_event_amount_cents(hay, ctx)
        g = groups.setdefault(
            norm,
            {
                "normalized_name": norm,
                "merchant": str(ctx.get("merchant") or norm),
                "zombie_prone": zombie_prone,
                "records": [],
                "explicit_renewal": True,
                "is_tier1": True,
                "hint_text": hay,
                "_seen": set(),
            },
        )
        g["explicit_renewal"] = True
        g["is_tier1"] = True
        if amt_c > 0 or c.get("due_at"):
            _add_record(
                g,
                {
                    "occurred_at": str(c.get("created_at") or ts),
                    "amount_cents": amt_c,
                    "currency": curr,
                    "merchant": str(ctx.get("merchant") or norm),
                    "next_expected_at": str(ctx.get("next_expected_at") or c.get("due_at") or ""),
                    "cadence_days": int(ctx.get("cadence_days") or 0),
                },
            )

    return groups


def infer_subscription_ongoing(
    last_charged_at: str,
    cadence_days: int = 30,
    *,
    status: str = "ACTIVE",
    explicit_next_at: str = "",
    now: Optional[datetime] = None,
) -> tuple[bool, Optional[int], str]:
    """根据最近扣费时间、账期与状态，自动判断订阅当前是否还在进行中。

    规则：
    - `CANCELLED` 恒为未在进行（False）；
    - 若存在显式预告扣费日且未早于 `now - 7d`，判定为进行中（True）；
    - 否则按宽限阈值 `max(cadence_days * 2 + 7, 21)` 天（连续缺失 2 个完整账期 + 7 天账单入账缓冲）判定：
      - 7 天周付：21 天无新扣费视为已停扣；
      - 30 天月付：67 天无新扣费视为已停扣；
      - 90 天季付：187 天无新扣费视为已停扣；
      - 365 天年付：737 天无新扣费视为已停扣。
    """
    cur_dt = now or _db_mod.now_local()
    st = str(status or "ACTIVE").strip().upper()
    cad = max(1, int(cadence_days or 30))

    days_since_last: Optional[int] = None
    last_dt: Optional[datetime] = None
    last_s = str(last_charged_at or "").strip()
    if last_s:
        try:
            last_dt = parse_iso(last_s)
            elapsed_raw = (cur_dt - last_dt).total_seconds() / 86400.0
            days_since_last = max(0, int(elapsed_raw))
        except Exception:
            last_dt = None

    if st == "CANCELLED":
        return False, days_since_last, "已取消"

    exp_s = str(explicit_next_at or "").strip()
    if exp_s:
        try:
            exp_dt = parse_iso(exp_s if "T" in exp_s else f"{exp_s[:10]}T09:00:00+08:00")
            if exp_dt >= cur_dt - timedelta(days=7):
                return True, days_since_last, "存在预告续费日期"
        except Exception:
            pass

    if last_dt is not None:
        elapsed_days = (cur_dt - last_dt).total_seconds() / 86400.0
        lapse_days = max(cad * 2 + (7 if cad <= 90 else 0), 21)
        if elapsed_days <= float(lapse_days):
            if elapsed_days <= float(cad) + 3.0:
                return True, days_since_last, "正常账期内"
            return True, days_since_last, f"宽限期内（距上次扣费 {days_since_last} 天）"
        return False, days_since_last, f"已停扣（{days_since_last} 天未扣费，周期 {cad} 天）"

    return True, None, "默认进行中"


def enrich_subscription_row(
    row: dict[str, Any],
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """为 `subscription_audit` 行补充规范化的 `is_ongoing` / `ongoing_source` / `ongoing_label` / `ongoing_reason` / `days_since_last_charge`。"""
    cur_dt = now or _db_mod.now_local()
    out = dict(row)
    st = str(out.get("status") or "ACTIVE").strip().upper()
    cad = max(1, int(out.get("cadence_days") or 30))
    last_s = str(out.get("last_charged_at") or "").strip()
    src = str(out.get("ongoing_source") or "AUTO").strip().upper()
    if src not in ("AUTO", "MANUAL"):
        src = "AUTO"
    raw_ongoing = out.get("is_ongoing")

    auto_ongoing, days_since_last, auto_reason = infer_subscription_ongoing(
        last_s,
        cad,
        status=st,
        now=cur_dt,
    )

    if st == "CANCELLED":
        is_ongoing = False
        reason = "已取消"
    elif src == "MANUAL" and raw_ongoing is not None:
        is_ongoing = bool(int(raw_ongoing))
        reason = "已手动标为进行中" if is_ongoing else "已手动标为已停扣"
    elif last_s:
        is_ongoing = auto_ongoing
        reason = auto_reason
    elif raw_ongoing is not None:
        is_ongoing = bool(int(raw_ongoing))
        reason = "进行中" if is_ongoing else "已停扣"
    else:
        is_ongoing = auto_ongoing
        reason = auto_reason

    out["status"] = st
    out["is_ongoing"] = is_ongoing
    out["ongoing_source"] = src
    out["ongoing_label"] = "进行中" if is_ongoing else ("已取消" if st == "CANCELLED" else "已停扣")
    out["ongoing_reason"] = reason
    out["days_since_last_charge"] = days_since_last
    return out


def scan_subscriptions(
    db: SimingDB,
    now: Optional[datetime] = None,
    min_occurrences: int = 2,
) -> dict[str, Any]:
    """扫描 `ledger_txns` 与邮箱续费 `events`/`cases`，精准识别真订阅、周期、涨价与僵尸候选，并同步 Sure 周期规则。"""
    db.init_db()
    cur_dt = now or _db_mod.now_local()
    ts = iso_now(cur_dt)

    conn = db.connect()
    try:
        txns = [
            dict(r)
            for r in conn.execute(
                """
                SELECT txn_id, occurred_at, amount_cents, currency, merchant, notes, kind
                FROM ledger_txns
                WHERE amount_cents > 0
                  AND kind NOT IN ('收入', '退款', '还款', '转入')
                  AND COALESCE(UPPER(status), '') NOT IN ('VOID_DUPLICATE', 'VOID', 'CANCELLED')
                ORDER BY occurred_at ASC
                """
            ).fetchall()
        ]
        ev_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT event_id, source, occurred_at, summary, payload_json FROM events ORDER BY occurred_at ASC"
            ).fetchall()
        ]
        case_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT case_id, title, next_step, due_at, context_json, created_at FROM cases WHERE category IN ('sub_renew', 'subscription', 'bill') OR rule_id = 'sub_renew'"
            ).fetchall()
        ]
        existing_subs = {
            str(r["sub_id"]): dict(r)
            for r in conn.execute("SELECT * FROM subscription_audit").fetchall()
        }
    finally:
        conn.close()

    groups = _collect_candidate_groups(txns, ev_rows, case_rows, ts)

    detected: list[dict[str, Any]] = []
    price_hikes: list[dict[str, Any]] = []
    zombies: list[dict[str, Any]] = []

    for norm, grp in groups.items():
        recs = sorted(grp["records"], key=lambda x: str(x["occurred_at"]))
        if not recs:
            continue

        intervals = _compute_unique_day_intervals(recs)
        ok, snapped_cadence, _tier_reason = _qualifies_as_subscription(
            norm=norm,
            recs=recs,
            intervals=intervals,
            explicit_renewal=bool(grp["explicit_renewal"]),
            is_tier1=bool(grp["is_tier1"]),
            min_occurrences=min_occurrences,
            hint_text=str(grp.get("hint_text") or ""),
        )
        if not ok:
            continue

        explicit_cadence = next((int(r.get("cadence_days") or 0) for r in reversed(recs) if r.get("cadence_days")), 0)
        if explicit_cadence > 0:
            cadence_days = explicit_cadence
        elif snapped_cadence is not None:
            cadence_days = snapped_cadence
        else:
            cadence_days = _snap_cadence(intervals, grp["hint_text"])

        latest = recs[-1]
        latest_amt = int(latest["amount_cents"] or 0)
        prev_amt = int(recs[-2]["amount_cents"]) if len(recs) >= 2 else latest_amt
        hike_pct = round(((latest_amt - prev_amt) / prev_amt) * 100.0, 2) if (prev_amt > 0 and latest_amt > 0) else 0.0

        last_charged_at = str(latest["occurred_at"])
        explicit_next = str(latest.get("next_expected_at") or "").strip()

        sub_id = _make_sub_id(norm)
        prev_row = existing_subs.get(sub_id)
        prev_status = str(prev_row["status"]) if prev_row else ""

        if prev_status in ("KEEP", "CANCELLED", "ZOMBIE"):
            status = prev_status
        elif grp["zombie_prone"] and len(recs) >= 2:
            status = "ZOMBIE"
        else:
            status = "ACTIVE"

        auto_ongoing, days_since_last, auto_reason = infer_subscription_ongoing(
            last_charged_at,
            cadence_days,
            status=status,
            explicit_next_at=explicit_next,
            now=cur_dt,
        )
        prev_source = str(prev_row.get("ongoing_source") or "AUTO").strip().upper() if prev_row else "AUTO"
        if prev_source not in ("AUTO", "MANUAL"):
            prev_source = "AUTO"
        prev_ongoing = bool(int(prev_row.get("is_ongoing", 1))) if prev_row else True
        prev_last_charged = str(prev_row.get("last_charged_at") or "").strip() if prev_row else ""

        if status == "CANCELLED":
            is_ongoing = False
            ongoing_source = prev_source if prev_source == "MANUAL" else "AUTO"
            ongoing_reason = "已取消"
        elif prev_source == "MANUAL":
            # 若用户曾手动标为已停扣，但随后又出现更新的真实扣费，自动恢复为进行中以防漏扣
            if (not prev_ongoing) and last_charged_at and prev_last_charged and last_charged_at > prev_last_charged and auto_ongoing:
                is_ongoing = True
                ongoing_source = "AUTO"
                ongoing_reason = f"检测到新扣费（{last_charged_at[:10]}），已恢复进行中"
            else:
                is_ongoing = prev_ongoing
                ongoing_source = "MANUAL"
                ongoing_reason = "已手动标为进行中" if is_ongoing else "已手动标为已停扣"
        else:
            is_ongoing = auto_ongoing
            ongoing_source = "AUTO"
            ongoing_reason = auto_reason

        if explicit_next:
            norm_exp = explicit_next if "T" in explicit_next else f"{explicit_next[:10]}T09:00:00+08:00"
            try:
                next_dt = parse_iso(norm_exp)
                if cadence_days > 0 and is_ongoing:
                    while next_dt < cur_dt:
                        next_dt += timedelta(days=cadence_days)
                next_expected_at = iso_now(next_dt)
            except Exception:
                next_expected_at = norm_exp
        else:
            try:
                next_dt = parse_iso(last_charged_at) + timedelta(days=cadence_days)
                if cadence_days > 0 and is_ongoing:
                    while next_dt < cur_dt:
                        next_dt += timedelta(days=cadence_days)
                next_expected_at = iso_now(next_dt)
            except Exception:
                next_expected_at = iso_now(cur_dt + timedelta(days=cadence_days))

        notes_parts: list[str] = []
        if hike_pct > 5.0:
            notes_parts.append(f"涨价+{hike_pct:.1f}%({prev_amt/100:.2f}->{latest_amt/100:.2f})")
        if grp["zombie_prone"]:
            notes_parts.append("低利用率僵尸候选")
        prev_note = str(prev_row["notes"]).strip() if (prev_row and prev_row.get("notes")) else ""
        if prev_status in ("KEEP", "CANCELLED", "ZOMBIE") and prev_note:
            notes = prev_note
        else:
            notes = "；".join(notes_parts) or prev_note

        # 同步创建/更新 Sure 周期订阅规则（已取消或已停扣订阅不再写入活跃周期规则）
        if status == "CANCELLED" or not is_ongoing:
            sure_rule_id = str(prev_row["sure_rule_id"] or "") if prev_row else ""
        else:
            rule_res = upsert_sure_rule(
                db,
                name=f"订阅:{norm}",
                match_pattern=re.escape(norm),
                category="订阅服务",
                tags=["订阅", f"周期{cadence_days}天"],
                split_ratio=1.0,
                is_recurring=True,
                cadence_days=cadence_days,
            )
            sure_rule_id = str(rule_res.get("rule_id") or "")

        with db.transaction() as wconn:
            wconn.execute(
                """
                INSERT INTO subscription_audit (
                    sub_id, merchant, normalized_name, cadence_days, amount_cents, currency,
                    occurrences, last_charged_at, next_expected_at, prev_amount_cents,
                    price_hike_pct, status, is_ongoing, ongoing_source, sure_rule_id, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(sub_id) DO UPDATE SET
                    merchant = excluded.merchant,
                    normalized_name = excluded.normalized_name,
                    cadence_days = excluded.cadence_days,
                    amount_cents = excluded.amount_cents,
                    currency = excluded.currency,
                    occurrences = excluded.occurrences,
                    last_charged_at = excluded.last_charged_at,
                    next_expected_at = excluded.next_expected_at,
                    prev_amount_cents = excluded.prev_amount_cents,
                    price_hike_pct = excluded.price_hike_pct,
                    status = excluded.status,
                    is_ongoing = excluded.is_ongoing,
                    ongoing_source = excluded.ongoing_source,
                    sure_rule_id = excluded.sure_rule_id,
                    notes = excluded.notes,
                    updated_at = excluded.updated_at
                """,
                (
                    sub_id,
                    str(latest.get("merchant") or grp["merchant"]),
                    norm,
                    cadence_days,
                    latest_amt,
                    str(latest.get("currency") or "CNY"),
                    len(recs),
                    last_charged_at,
                    next_expected_at,
                    prev_amt,
                    hike_pct,
                    status,
                    1 if is_ongoing else 0,
                    ongoing_source,
                    sure_rule_id,
                    notes,
                    ts,
                ),
            )

        ongoing_label = "进行中" if is_ongoing else ("已取消" if status == "CANCELLED" else "已停扣")
        entry = {
            "sub_id": sub_id,
            "merchant": str(latest.get("merchant") or grp["merchant"]),
            "normalized_name": norm,
            "cadence_days": cadence_days,
            "amount_cents": latest_amt,
            "amount_yuan": round(latest_amt / 100.0, 2),
            "prev_amount_yuan": round(prev_amt / 100.0, 2),
            "currency": str(latest.get("currency") or "CNY"),
            "occurrences": len(recs),
            "last_charged_at": last_charged_at,
            "next_expected_at": next_expected_at,
            "price_hike_pct": hike_pct,
            "status": status,
            "is_ongoing": is_ongoing,
            "ongoing_source": ongoing_source,
            "ongoing_label": ongoing_label,
            "ongoing_reason": ongoing_reason,
            "days_since_last_charge": days_since_last,
            "sure_rule_id": sure_rule_id,
            "notes": notes,
        }
        detected.append(entry)
        if hike_pct > 5.0 and status != "CANCELLED" and is_ongoing:
            price_hikes.append(entry)
        if status == "ZOMBIE" and is_ongoing:
            zombies.append(entry)

    ongoing_count = sum(1 for d in detected if d["is_ongoing"] and d["status"] != "CANCELLED")
    stopped_count = sum(1 for d in detected if (not d["is_ongoing"]) and d["status"] != "CANCELLED")
    return {
        "ok": True,
        "scanned_txns": len(txns),
        "scanned_events": len(ev_rows),
        "detected": len(detected),
        "ongoing_count": ongoing_count,
        "stopped_count": stopped_count,
        "price_hikes": price_hikes,
        "zombies": zombies,
        "subscriptions": detected,
    }


def purge_false_positive_subscriptions(db: SimingDB) -> dict[str, Any]:
    """清理 `subscription_audit` 中的非订阅误报行（保留用户显式标记的 `KEEP` / `CANCELLED`），
    并同步删除 `sure_rules` 中由 `sub_hunter` 自动生成的排除商户/无效订阅周期规则。
    """
    db.init_db()
    ts = iso_now()

    conn = db.connect()
    try:
        sub_rows = [dict(r) for r in conn.execute("SELECT * FROM subscription_audit").fetchall()]
        rule_rows = [dict(r) for r in conn.execute("SELECT * FROM sure_rules").fetchall()]
        txns = [
            dict(r)
            for r in conn.execute(
                """
                SELECT txn_id, occurred_at, amount_cents, currency, merchant, notes, kind
                FROM ledger_txns
                WHERE amount_cents > 0 AND kind NOT IN ('收入', '退款')
                ORDER BY occurred_at ASC
                """
            ).fetchall()
        ]
        ev_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT event_id, source, occurred_at, summary, payload_json FROM events ORDER BY occurred_at ASC"
            ).fetchall()
        ]
        case_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT case_id, title, next_step, due_at, context_json, created_at FROM cases WHERE category IN ('sub_renew', 'subscription', 'bill') OR rule_id = 'sub_renew'"
            ).fetchall()
        ]
    finally:
        conn.close()

    has_ledger_or_events = bool(txns or ev_rows or case_rows)
    valid_norms: set[str] = set()
    if has_ledger_or_events:
        groups = _collect_candidate_groups(txns, ev_rows, case_rows, ts)
        for norm, grp in groups.items():
            recs = sorted(grp["records"], key=lambda x: str(x["occurred_at"]))
            intervals = _compute_unique_day_intervals(recs)
            ok, _snapped, _reason = _qualifies_as_subscription(
                norm=norm,
                recs=recs,
                intervals=intervals,
                explicit_renewal=bool(grp["explicit_renewal"]),
                is_tier1=bool(grp["is_tier1"]),
                min_occurrences=2,
                hint_text=str(grp.get("hint_text") or ""),
            )
            if ok:
                valid_norms.add(norm)

    purged_sub_ids: list[str] = []
    purged_rule_ids: set[str] = set()
    remaining_norms: set[str] = set()

    for r in sub_rows:
        sid = str(r["sub_id"])
        status = str(r.get("status") or "").upper()
        merchant = str(r.get("merchant") or "")
        norm = str(r.get("normalized_name") or "")
        occ = int(r.get("occurrences") or 0)
        cad = int(r.get("cadence_days") or 0)
        srid = str(r.get("sure_rule_id") or "")

        # 保留用户显式标记为 KEEP 或 CANCELLED 的记录
        if status in ("KEEP", "CANCELLED"):
            remaining_norms.add(norm)
            continue

        excluded = (
            is_excluded_non_subscription(merchant)
            or is_excluded_non_subscription(norm)
            or norm in ("", "未知订阅")
        )
        if not excluded:
            if has_ledger_or_events:
                # 有账本/事件依据时，必须通过新版 Tier 1/Tier 2 严格校验
                if norm not in valid_norms:
                    excluded = True
            else:
                # 仅审计表独立校验（无流水场景）：非 Tier 1 必须满足 >=3 次且为标准账期
                tier1 = is_explicit_subscription_merchant(merchant) or is_explicit_subscription_merchant(norm)
                if not tier1 and (occ < 3 or cad not in _CADENCE_DAYS_CANDIDATES):
                    excluded = True

        if excluded:
            purged_sub_ids.append(sid)
            if srid:
                purged_rule_ids.add(srid)
        else:
            remaining_norms.add(norm)

    # 扫描 sure_rules 中所有由 sub_hunter 自动生成的周期规则（name='订阅:...' 或 category='订阅服务' & is_recurring=1）
    for rr in rule_rows:
        rid = str(rr["rule_id"])
        rname = str(rr.get("name") or "")
        rcat = str(rr.get("category") or "")
        is_rec = bool(rr.get("is_recurring"))
        if not is_rec:
            continue
        is_sub_hunter_rule = rname.startswith("订阅:") or rcat == "订阅服务"
        if not is_sub_hunter_rule:
            continue
        target_name = rname[3:].strip() if rname.startswith("订阅:") else rname
        if (
            rid in purged_rule_ids
            or is_excluded_non_subscription(target_name)
            or target_name in ("", "未知订阅")
            or (rname.startswith("订阅:") and target_name not in remaining_norms)
        ):
            purged_rule_ids.add(rid)

    with db.transaction() as wconn:
        for sid in purged_sub_ids:
            wconn.execute("DELETE FROM subscription_audit WHERE sub_id = ?", (sid,))
        for rid in purged_rule_ids:
            wconn.execute("DELETE FROM sure_rules WHERE rule_id = ?", (rid,))
        db.write_audit(
            "A1",
            "purge_false_positive_subscriptions",
            {
                "before_count": len(sub_rows),
                "after_count": len(sub_rows) - len(purged_sub_ids),
                "purged_subscriptions": len(purged_sub_ids),
                "purged_sure_rules": len(purged_rule_ids),
            },
            conn=wconn,
        )

    after_count = len(sub_rows) - len(purged_sub_ids)
    return {
        "ok": True,
        "before_count": len(sub_rows),
        "after_count": after_count,
        "purged_subscriptions": len(purged_sub_ids),
        "purged_sub_ids": purged_sub_ids,
        "purged_sure_rules": len(purged_rule_ids),
        "preserved_count": after_count,
    }


def find_upcoming_charges(
    db: SimingDB,
    now: Optional[datetime] = None,
    horizon_hours: int = 48,
) -> list[dict[str, Any]]:
    """查找未来 `horizon_hours` 小时内即将扣费或转正的进行中订阅（自动忽略 `CANCELLED` 与已停扣订阅）。"""
    db.init_db()
    cur_dt = now or _db_mod.now_local()

    conn = db.connect()
    try:
        rows = [
            enrich_subscription_row(dict(r), now=cur_dt)
            for r in conn.execute(
                "SELECT * FROM subscription_audit WHERE status != 'CANCELLED' ORDER BY next_expected_at ASC"
            ).fetchall()
        ]
    finally:
        conn.close()

    upcoming: list[dict[str, Any]] = []
    for r in rows:
        if not r.get("is_ongoing"):
            continue
        nxt_s = str(r.get("next_expected_at") or "").strip()
        if not nxt_s:
            continue
        try:
            nxt_dt = parse_iso(nxt_s)
        except Exception:
            continue
        hours_until = (nxt_dt - cur_dt).total_seconds() / 3600.0
        if 0.0 <= hours_until <= float(horizon_hours):
            upcoming.append(
                {
                    **r,
                    "amount_yuan": round(int(r.get("amount_cents") or 0) / 100.0, 2),
                    "hours_until": round(hours_until, 1),
                }
            )
    return upcoming


def emit_subscription_stoploss_alerts(
    db: SimingDB,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """为 48h 内即将扣费或新发现悄悄涨价（>5%）的进行中订阅推送 QQ 止损卡（含 保留 / 去取消 / 记为僵尸 / 已停扣 按钮）。"""
    from siming.push import push_message

    db.init_db()
    cur_dt = now or _db_mod.now_local()
    upcoming = find_upcoming_charges(db, now=cur_dt, horizon_hours=48)

    conn = db.connect()
    try:
        hiked_rows = [
            er
            for r in conn.execute(
                "SELECT * FROM subscription_audit WHERE status != 'CANCELLED' AND price_hike_pct > 5.0"
            ).fetchall()
            if (er := enrich_subscription_row(dict(r), now=cur_dt)).get("is_ongoing")
        ]
    finally:
        conn.close()

    targets: dict[str, dict[str, Any]] = {}
    for item in upcoming:
        targets[str(item["sub_id"])] = {**item, "trigger": "upcoming_48h"}
    for h in hiked_rows:
        sid = str(h["sub_id"])
        if sid in targets:
            targets[sid]["trigger"] = "upcoming_48h+price_hike"
        else:
            targets[sid] = {
                **h,
                "amount_yuan": round(int(h.get("amount_cents") or 0) / 100.0, 2),
                "trigger": "price_hike",
            }

    alerts: list[dict[str, Any]] = []
    enabled = is_module_enabled(db, "sub_renew") and is_module_enabled(db, "sub_assassin")

    for sid, item in targets.items():
        name = str(item["normalized_name"])
        amt_y = float(item["amount_yuan"])
        curr = str(item.get("currency") or "CNY")
        hike = float(item.get("price_hike_pct") or 0.0)
        hrs = item.get("hours_until")
        trigger = str(item["trigger"])

        reasons: list[str] = []
        if hrs is not None:
            reasons.append(f"预计 **{hrs:.1f} 小时内**自动扣费（{str(item['next_expected_at'])[:16]}）")
        if hike > 5.0:
            prev_y = round(int(item.get("prev_amount_cents") or 0) / 100.0, 2)
            reasons.append(f"⚠️ 检测到悄悄涨价 **+{hike:.1f}%**（{prev_y:.2f} → {amt_y:.2f}）")
        if str(item.get("status")) == "ZOMBIE":
            reasons.append("🧟 疑似僵尸订阅")

        text = (
            f"🗡️ **订阅刺客止损卡** [`{sid}`]\n"
            f"• 服务：**{name}**（每 {int(item['cadence_days'])} 天 · `{curr} {amt_y:.2f}` · 进行中）\n"
            f"• 触发：{' ｜ '.join(reasons)}\n"
            "• 快捷操作：**保留** / **去取消** / **记为僵尸** / **已停扣**"
        )
        kb = make_keyboard(
            [
                [
                    make_button("keep", "✅ 保留", f"siming:sub_status:{sid}:KEEP", style=1, group_id=f"sub_{sid}"),
                    make_button("cancel", "✂️ 去取消", f"siming:sub_status:{sid}:CANCELLED", style=0, group_id=f"sub_{sid}"),
                    make_button("zombie", "🧟 记为僵尸", f"siming:sub_status:{sid}:ZOMBIE", style=0, group_id=f"sub_{sid}"),
                ],
                [
                    make_button("stopped", "⏹️ 已停扣（未在进行）", f"siming:sub_ongoing:{sid}:0", style=0, group_id=f"sub_{sid}"),
                ],
            ]
        )

        delivered = False
        if enabled:
            res = push_message(
                db,
                text=text,
                rule_id="sub_renew",
                ref_id=f"sub_stoploss:{sid}:{str(item.get('next_expected_at') or '')[:10]}:{trigger}",
                level="normal",
                dedup_window=timedelta(hours=36),
                keyboard=kb,
            )
            delivered = bool(res.get("ok") and not res.get("skipped"))

        alerts.append(
            {
                "sub_id": sid,
                "normalized_name": name,
                "trigger": trigger,
                "amount_yuan": amt_y,
                "price_hike_pct": hike,
                "hours_until": hrs,
                "text": text,
                "keyboard": kb,
                "delivered": delivered,
            }
        )

    return {"ok": True, "alert_count": len(alerts), "alerts": alerts}


def mark_subscription_status(
    db: SimingDB,
    sub_id_or_name: str,
    status: Optional[str] = None,
    note: str = "",
    is_ongoing: Optional[bool] = None,
) -> dict[str, Any]:
    """将指定订阅标记状态（`KEEP` / `ZOMBIE` / `CANCELLED` / `ACTIVE`）和/或是否还在进行中（`is_ongoing`）。"""
    db.init_db()
    st_raw = (status or "").strip().upper()
    if not st_raw and is_ongoing is None:
        return {"ok": False, "error": "status 与 is_ongoing 至少需提供一项"}
    if st_raw and st_raw not in ("KEEP", "ZOMBIE", "CANCELLED", "ACTIVE"):
        return {"ok": False, "error": f"非法状态 {status!r}，可选 KEEP / ZOMBIE / CANCELLED / ACTIVE"}
    if st_raw == "CANCELLED" and is_ongoing is True:
        return {"ok": False, "error": "已取消 (CANCELLED) 的订阅不能同时标记为进行中"}
    target = (sub_id_or_name or "").strip()
    if not target:
        return {"ok": False, "error": "sub_id_or_name 不能为空"}

    cur_dt = _db_mod.now_local()
    ts = iso_now(cur_dt)
    with db.transaction() as conn:
        row = conn.execute(
            """
            SELECT * FROM subscription_audit
            WHERE sub_id = ? OR LOWER(normalized_name) = LOWER(?) OR LOWER(merchant) = LOWER(?)
            LIMIT 1
            """,
            (target, target, target),
        ).fetchone()
        if not row:
            row = conn.execute(
                "SELECT * FROM subscription_audit WHERE normalized_name LIKE ? OR merchant LIKE ? LIMIT 1",
                (f"%{target}%", f"%{target}%"),
            ).fetchone()
        if not row:
            return {"ok": False, "error": f"未找到匹配订阅: {target}"}

        row_d = dict(row)
        sid = str(row_d["sub_id"])
        prev_st = str(row_d.get("status") or "ACTIVE").strip().upper()
        st = st_raw or prev_st
        # 若原本为 CANCELLED，用户仅点击「标为进行中」，自动恢复状态为 ACTIVE
        if not st_raw and is_ongoing is True and prev_st == "CANCELLED":
            st = "ACTIVE"

        cad = max(1, int(row_d.get("cadence_days") or 30))
        last_charged_at = str(row_d.get("last_charged_at") or "").strip()
        next_expected_at = str(row_d.get("next_expected_at") or "").strip()

        if is_ongoing is not None:
            new_ongoing = bool(is_ongoing)
            new_ongoing_source = "MANUAL"
        elif st == "CANCELLED":
            new_ongoing = False
            new_ongoing_source = "MANUAL"
        elif prev_st == "CANCELLED":
            auto_on, _, _ = infer_subscription_ongoing(last_charged_at, cad, status=st, now=cur_dt)
            new_ongoing = auto_on
            new_ongoing_source = "AUTO"
        else:
            prev_src = str(row_d.get("ongoing_source") or "AUTO").strip().upper()
            if prev_src == "MANUAL":
                new_ongoing = bool(int(row_d.get("is_ongoing", 1)))
                new_ongoing_source = "MANUAL"
            else:
                auto_on, _, _ = infer_subscription_ongoing(last_charged_at, cad, status=st, now=cur_dt)
                new_ongoing = auto_on
                new_ongoing_source = "AUTO"

        # 若标记为进行中但下次扣费日期停留在过去，按周期滚推到下一个预计扣费日
        if new_ongoing and last_charged_at and cad > 0:
            try:
                nxt_dt = parse_iso(next_expected_at) if next_expected_at else (parse_iso(last_charged_at) + timedelta(days=cad))
                if nxt_dt < cur_dt:
                    while nxt_dt < cur_dt:
                        nxt_dt += timedelta(days=cad)
                    next_expected_at = iso_now(nxt_dt)
            except Exception:
                pass

        new_notes = note.strip() if note.strip() else str(row_d.get("notes") or "")
        conn.execute(
            """
            UPDATE subscription_audit
            SET status = ?, is_ongoing = ?, ongoing_source = ?, next_expected_at = ?, notes = ?, updated_at = ?
            WHERE sub_id = ?
            """,
            (st, 1 if new_ongoing else 0, new_ongoing_source, next_expected_at, new_notes, ts, sid),
        )
        updated_row = dict(conn.execute("SELECT * FROM subscription_audit WHERE sub_id = ?", (sid,)).fetchone())

    enriched = enrich_subscription_row(updated_row, now=cur_dt)
    return {
        "ok": True,
        "sub_id": sid,
        "normalized_name": str(enriched["normalized_name"]),
        "status": str(enriched["status"]),
        "is_ongoing": bool(enriched["is_ongoing"]),
        "ongoing_source": str(enriched["ongoing_source"]),
        "ongoing_label": str(enriched["ongoing_label"]),
        "ongoing_reason": str(enriched["ongoing_reason"]),
        "next_expected_at": str(enriched["next_expected_at"]),
        "notes": new_notes,
        "updated_at": ts,
    }


def mark_subscription_ongoing(
    db: SimingDB,
    sub_id_or_name: str,
    is_ongoing: bool,
    note: str = "",
) -> dict[str, Any]:
    """显式切换指定订阅的「是否还在进行中」标志（保留原有的 ACTIVE / KEEP / ZOMBIE 分类标签）。"""
    return mark_subscription_status(db, sub_id_or_name, status=None, note=note, is_ongoing=bool(is_ongoing))


def format_subscription_audit(db: SimingDB, now: Optional[datetime] = None) -> str:
    """格式化订阅刺客审计清单与进行中订阅的月度/年度总成本。"""
    db.init_db()
    cur_dt = now or _db_mod.now_local()
    conn = db.connect()
    try:
        rows = [
            enrich_subscription_row(dict(r), now=cur_dt)
            for r in conn.execute("SELECT * FROM subscription_audit ORDER BY status, next_expected_at").fetchall()
        ]
    finally:
        conn.close()

    if not rows:
        return "🗡️ **订阅刺客审计**：暂未发现周期扣费订阅。"

    annual_cycles_map = {7: 52.0, 14: 26.0, 30: 12.0, 90: 4.0, 365: 1.0}
    annual_total = 0.0
    ongoing_rows = [r for r in rows if r["is_ongoing"] and r["status"] != "CANCELLED"]
    stopped_rows = [r for r in rows if (not r["is_ongoing"]) or r["status"] == "CANCELLED"]

    status_rank = {"ZOMBIE": 0, "ACTIVE": 1, "KEEP": 2, "CANCELLED": 3}
    ongoing_rows.sort(key=lambda r: (status_rank.get(str(r["status"]), 9), str(r.get("next_expected_at") or "")))
    stopped_rows.sort(key=lambda r: (status_rank.get(str(r["status"]), 9), str(r.get("last_charged_at") or "")), reverse=False)

    lines = [f"🗡️ **订阅刺客审计总览**（进行中 {len(ongoing_rows)} 项 · 已停扣/取消 {len(stopped_rows)} 项）"]
    for r in ongoing_rows:
        amt_y = round(int(r["amount_cents"] or 0) / 100.0, 2)
        cad = max(1, int(r["cadence_days"] or 30))
        st = str(r["status"])
        curr = str(r.get("currency") or "CNY").strip().upper()
        fx = float(_SUB_FX_TO_CNY.get(curr, 1.0))
        cycles_per_year = annual_cycles_map.get(cad, 365.0 / cad)
        annual_total += amt_y * fx * cycles_per_year
        badge = {"ACTIVE": "🟢", "KEEP": "📌", "ZOMBIE": "🧟", "CANCELLED": "✂️"}.get(st, "⚪")
        hike_s = f" · ⚠️涨价+{float(r['price_hike_pct']):.1f}%" if float(r.get("price_hike_pct") or 0.0) > 5.0 else ""
        last_s = str(r.get("last_charged_at") or "")[:10] or "-"
        lines.append(
            f"• ▶️[进行中] {badge} **{r['normalized_name']}** [`{r['sub_id']}`]：{r['currency']} {amt_y:.2f} / {cad}天"
            f" · 上次 `{last_s}` · 下次 `{str(r['next_expected_at'])[:10]}` · `{st}`{hike_s}"
        )

    if stopped_rows:
        lines.append("— **已停扣 / 已取消（不计入在途支出）** —")
        for r in stopped_rows:
            amt_y = round(int(r["amount_cents"] or 0) / 100.0, 2)
            cad = max(1, int(r["cadence_days"] or 30))
            st = str(r["status"])
            flag_s = "✂️[已取消]" if st == "CANCELLED" else "⏹️[已停扣]"
            last_s = str(r.get("last_charged_at") or "")[:10] or "-"
            lines.append(
                f"• {flag_s} **{r['normalized_name']}** [`{r['sub_id']}`]：{r['currency']} {amt_y:.2f} / {cad}天"
                f" · 末次 `{last_s}` · `{st}`"
            )

    monthly_total = annual_total / 12.0
    upcoming = find_upcoming_charges(db, now=cur_dt, horizon_hours=48)
    lines.append(f"• 估算在途月度固定订阅支出：**¥{monthly_total:.2f}/月**（约 ¥{annual_total:.2f}/年）")
    if upcoming:
        lines.append(f"• ⏰ 未来 48h 即将扣费：**{len(upcoming)}** 项（{', '.join(u['normalized_name'] for u in upcoming)}）")
    return "\n".join(lines)
