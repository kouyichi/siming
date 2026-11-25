"""中国实际日历计算：国务院调休补班、北京尾号限行、12306/114 放号点、农历生日与自然时间解析。"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Optional

import httpx
from lunar_python import Lunar, LunarYear

from siming.config import load_calendar_rules, load_policy
from siming.db import SHANGHAI_TZ, iso_now, logical_today, now_local
from siming.paths import SimingPaths, get_paths

WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
CN_WEEKDAY_MAP = {
    "一": 0,
    "二": 1,
    "三": 2,
    "四": 3,
    "五": 4,
    "六": 5,
    "日": 6,
    "天": 6,
}


@dataclass(frozen=True)
class DayCalendarInfo:
    day: date
    weekday_cn: str
    is_workday: bool
    is_rest_day: bool
    is_makeup_workday: bool
    holiday_name: str
    beijing_tail_limit: str

    @property
    def kind_label(self) -> str:
        if self.is_makeup_workday:
            return f"补班工作日（{self.holiday_name}）" if self.holiday_name else "补班工作日"
        if self.is_rest_day and self.holiday_name:
            return f"休·{self.holiday_name}"
        return "工作日" if self.is_workday else "休息日"

    @property
    def brief_header(self) -> str:
        tail = f"限行 {self.beijing_tail_limit}" if self.beijing_tail_limit not in ("不限行", "需核验") else self.beijing_tail_limit
        return f"{self.day.strftime('%m-%d')} {self.weekday_cn} · {self.kind_label} · {tail}"

    def personal_header(self, tail_limit_exempt: bool = False) -> str:
        if tail_limit_exempt:
            return f"{self.day.strftime('%m-%d')} {self.weekday_cn} · {self.kind_label} · 电车不限行"
        return self.brief_header


def day_info(target: date, paths: Optional[SimingPaths] = None) -> DayCalendarInfo:
    rules = load_calendar_rules(paths)
    ds = target.isoformat()
    wd = target.weekday()  # 0=Mon .. 6=Sun
    wd_cn = WEEKDAY_CN[wd]

    special = (rules.get("days") or {}).get(ds)
    if special is not None:
        is_off = bool(special.get("isOffDay", False))
        h_name = str(special.get("name", ""))
        is_rest = is_off
        is_work = not is_off
        is_makeup = (not is_off) and (wd >= 5)
    else:
        h_name = ""
        is_work = wd < 5
        is_rest = wd >= 5
        is_makeup = False

    # 北京尾号限行铁律：法定节假日与「调休补班的周六日」均不限行；仅正常周一至周五限行
    if wd >= 5 or is_rest:
        tail = "不限行"
    else:
        tail = "需核验"
        for rot in rules.get("beijing_tail_rotations") or []:
            if rot["start"] <= ds <= rot["end"]:
                tail = (rot.get("weekday_tails") or {}).get(str(wd + 1), "需核验")
                break

    return DayCalendarInfo(
        day=target,
        weekday_cn=wd_cn,
        is_workday=is_work,
        is_rest_day=is_rest,
        is_makeup_workday=is_makeup,
        holiday_name=h_name,
        beijing_tail_limit=tail,
    )


def upcoming_non_makeup_rest_days(
    start: Optional[date] = None,
    horizon_days: int = 14,
    paths: Optional[SimingPaths] = None,
) -> list[DayCalendarInfo]:
    base = start or now_local().date()
    out: list[DayCalendarInfo] = []
    for offset in range(0, horizon_days + 1):
        info = day_info(base + timedelta(days=offset), paths)
        if info.is_rest_day and not info.is_makeup_workday:
            out.append(info)
    return out


def upcoming_makeup_workdays(
    start: Optional[date] = None,
    horizon_days: int = 14,
    paths: Optional[SimingPaths] = None,
) -> list[DayCalendarInfo]:
    base = start or now_local().date()
    out: list[DayCalendarInfo] = []
    for offset in range(0, horizon_days + 1):
        info = day_info(base + timedelta(days=offset), paths)
        if info.is_makeup_workday:
            out.append(info)
    return out


def subtract_workdays(
    target: date, n_workdays: int, paths: Optional[SimingPaths] = None
) -> date:
    cur = target
    remaining = max(0, n_workdays)
    while remaining > 0:
        cur -= timedelta(days=1)
        if day_info(cur, paths).is_workday:
            remaining -= 1
    return cur


def rail_12306_alert_window(
    depart_date: date, station: str, paths: Optional[SimingPaths] = None
) -> dict[str, Any]:
    rules = load_calendar_rules(paths)
    stations: dict[str, str] = rules.get("rail_12306_stations") or {}
    clean_station = station.removesuffix("站").strip()
    sale_hhmm = stations.get(clean_station)
    sale_day = depart_date - timedelta(days=14)
    if not sale_hhmm:
        return {
            "known": False,
            "station": station,
            "sale_date": sale_day.isoformat(),
            "sale_time": "需核验",
            "alert_at": None,
        }
    hh, mm = (int(x) for x in sale_hhmm.split(":"))
    sale_dt = datetime.combine(sale_day, time(hh, mm), tzinfo=SHANGHAI_TZ)
    alert_dt = sale_dt - timedelta(minutes=15)
    return {
        "known": True,
        "station": clean_station + "站",
        "sale_date": sale_day.isoformat(),
        "sale_time": sale_hhmm,
        "sale_at": iso_now(sale_dt),
        "alert_at": iso_now(alert_dt),
    }


_HOSPITAL_ALIASES = {
    "北京大学第三医院": "北医三院",
    "北京大学人民医院": "北大人民医院",
}


def hospital_114_alert_window(
    visit_date: date,
    hospital: str,
    advance_days: int = 7,
    paths: Optional[SimingPaths] = None,
) -> dict[str, Any]:
    rules = load_calendar_rules(paths)
    slots: dict[str, str] = rules.get("hospital_114_slots") or {}
    norm_hosp = _HOSPITAL_ALIASES.get(hospital, hospital)
    for raw_k, canon in _HOSPITAL_ALIASES.items():
        if raw_k in norm_hosp:
            norm_hosp = canon
            break
    release_hhmm = None
    for name, val in slots.items():
        if name in norm_hosp or norm_hosp in name:
            release_hhmm = val
            break
    release_day = visit_date - timedelta(days=advance_days)
    if not release_hhmm:
        return {
            "known": False,
            "hospital": hospital,
            "release_date": release_day.isoformat(),
            "release_time": "需核验",
            "alert_at": None,
        }
    hh, mm = (int(x) for x in release_hhmm.split(":"))
    rel_dt = datetime.combine(release_day, time(hh, mm), tzinfo=SHANGHAI_TZ)
    alert_dt = rel_dt - timedelta(minutes=10)
    return {
        "known": True,
        "hospital": hospital,
        "release_date": release_day.isoformat(),
        "release_time": release_hhmm,
        "alert_at": iso_now(alert_dt),
    }


def next_lunar_birthday(
    lunar_month: int,
    lunar_day: int,
    is_leap_month: bool = False,
    ref_date: Optional[date] = None,
) -> dict[str, Any]:
    base = ref_date or now_local().date()
    for yr in (base.year, base.year + 1, base.year + 2):
        ly = LunarYear.fromYear(yr)
        leap_m = ly.getLeapMonth()
        use_month = -abs(lunar_month) if (is_leap_month and leap_m == abs(lunar_month)) else abs(lunar_month)
        lm = ly.getMonth(use_month)
        if lm is None:
            continue
        actual_day = min(lunar_day, lm.getDayCount())
        lunar_obj = Lunar.fromYmd(yr, use_month, actual_day)
        solar = lunar_obj.getSolar()
        solar_date = date(solar.getYear(), solar.getMonth(), solar.getDay())
        if solar_date >= base:
            remind_date = solar_date - timedelta(days=3)
            return {
                "solar_date": solar_date.isoformat(),
                "remind_date": remind_date.isoformat(),
                "lunar_label": lunar_obj.toString(),
                "used_leap": use_month < 0,
            }
    raise ValueError(f"无法换算农历生日: {lunar_month}月{lunar_day}日")


def tax_settlement_window(ref_date: Optional[date] = None) -> dict[str, str]:
    base = ref_date or now_local().date()
    yr = base.year if base <= date(base.year, 6, 30) else base.year + 1
    return {
        "year": str(yr),
        "prep_remind_at": f"{yr}-02-25T09:00:00+08:00",
        "window_start": f"{yr}-03-01",
        "window_end": f"{yr}-06-30",
    }


def sync_holiday_cn(
    years: Optional[list[int]] = None, paths: Optional[SimingPaths] = None
) -> dict[str, Any]:
    """从 NateScarlet/holiday-cn 同步国务院放假与调休补班表到 calendar_rules.json。"""
    p = paths or get_paths()
    rules = load_calendar_rules(p)
    cur_year = now_local().year
    target_years = years or [cur_year, cur_year + 1]
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("HTTP_PROXY")
    updated_days = dict(rules.get("days") or {})
    fetched_years: list[int] = []

    for yr in target_years:
        url = f"https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/{yr}.json"
        try:
            with httpx.Client(timeout=10.0, proxy=proxy, follow_redirects=True) as client:
                resp = client.get(url)
                if resp.status_code != 200:
                    continue
                payload = resp.json()
        except Exception:
            continue
        for item in payload.get("days") or []:
            d_str = str(item.get("date", "")).strip()
            if d_str:
                updated_days[d_str] = {
                    "name": str(item.get("name", "")),
                    "isOffDay": bool(item.get("isOffDay", False)),
                }
        fetched_years.append(yr)

    if fetched_years:
        rules["days"] = updated_days
        rules["version"] = f"holiday-cn-{','.join(map(str, fetched_years))}"
        rules["fetched_at"] = iso_now()
        (p.config_dir / "calendar_rules.json").write_text(
            json.dumps(rules, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return {
        "ok": True,
        "fetched_years": fetched_years,
        "days_count": len(updated_days),
        "version": rules.get("version", ""),
    }


_REL_DURATION_RE = re.compile(
    r"^(?:in\s+)?(\d+)\s*(s|sec|秒|m|min|分钟|分|h|hr|小时|个?小时|d|day|天)后?$",
    re.IGNORECASE,
)
_REL_INLINE_RE = re.compile(
    r"(?:(半)\s*个?小时后|(\d+|[一二两三四五六七八九十]{1,2})\s*(秒|分钟|分|个?小时|天)后)"
)
_CN_NUM = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
}


def parse_duration_seconds(expr: str) -> int:
    s = expr.strip().lower()
    m = re.fullmatch(r"(\d+)\s*(s|m|h|d)", s)
    if m:
        val, unit = int(m.group(1)), m.group(2)
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        return val * mult
    return int(s)


# 「下班后」是时刻；「上下班 / 上班下班」是名词（通勤），不能被解析成 18:30（W07 误挂起根因）
_OFF_WORK_RE = re.compile(r"(?<!上)下班")

_STANDALONE_CHAT_CLOCK_RE = re.compile(r"^\d{1,2}\s*[:：]\s*\d{2}\d?$")
_PAST_CHAT_DIVIDER_RE = re.compile(r"^(?:昨天|前天)\s+\d{1,2}\s*[:：]\s*\d{2}$")
_IOS_STATUSBAR_CLOCK_RE = re.compile(
    r"^\d{1,2}:\d{2}\d?\s+(?:5G|4G|LTE|Wi-?Fi|SOS|VPN|\d{1,3}%).*$",
    re.IGNORECASE,
)
_EXPLICIT_AM_RE = re.compile(r"(凌晨|半夜|早上|今早|明早|清晨|上午|早起|起床|\bam\b)", re.IGNORECASE)
_EXPLICIT_PM_RE = re.compile(r"(下午|傍晚|晚上|今晚|明晚|夜里|\bpm\b)", re.IGNORECASE)
_SOCIAL_DAYTIME_CUE_RE = re.compile(
    r"(约|局|见面|碰面|集合|聚餐|吃饭|桌游|开会|上门|安装|送货|量尺|复尺|复诊|门诊|挂号|体检|面试|电影|演出|活动|场次|到家|出发|过来|过去)"
)
_CN_DIGIT_MAP = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}


def strip_chat_divider_timestamps(raw: str) -> str:
    """从多行截屏/聊天记录中剥离独立行聊天时间戳（如 `20:14`、`昨天 23:05`）与 iOS 状态栏时间，防止误当约定时刻。"""
    if "\n" not in (raw or ""):
        return raw
    lines = [ln.strip() for ln in raw.splitlines() if ln.strip()]
    if len(lines) < 2:
        return raw
    kept: list[str] = []
    removed_any = False
    for idx, ln in enumerate(lines):
        if _STANDALONE_CHAT_CLOCK_RE.fullmatch(ln) or _PAST_CHAT_DIVIDER_RE.fullmatch(ln):
            removed_any = True
            continue
        if idx == 0 and _IOS_STATUSBAR_CLOCK_RE.fullmatch(ln):
            removed_any = True
            continue
        kept.append(ln)
    if not removed_any or not kept:
        return raw
    candidate = "\n".join(kept)
    # 2 行短备忘（如『明天的会\n16:00』）且正文无其它时间线索时，保留第 2 行时刻
    if len(lines) == 2 and not re.search(
        r"(?:\d{1,2}\s*[:：]\s*\d{2}|[一二两三四五六七八九十\d]{1,2}\s*点|上午|下午|中午|晚上|早上|凌晨|半夜|下班)",
        candidate,
    ):
        return raw
    return candidate


def _parse_cn_minute(m_tok: Optional[str]) -> int:
    if not m_tok:
        return 0
    tok = m_tok.strip().rstrip("分")
    if tok == "半":
        return 30
    if tok == "一刻":
        return 15
    if tok == "三刻":
        return 45
    if tok.isdigit():
        return min(59, int(tok))
    if tok.startswith("零") and len(tok) == 2 and tok[1] in _CN_DIGIT_MAP:
        return _CN_DIGIT_MAP[tok[1]]
    if tok == "十":
        return 10
    if tok.startswith("十") and len(tok) == 2 and tok[1] in _CN_DIGIT_MAP:
        return 10 + _CN_DIGIT_MAP[tok[1]]
    if len(tok) >= 2 and tok[0] in _CN_DIGIT_MAP and tok[1] == "十":
        tens = _CN_DIGIT_MAP[tok[0]] * 10
        ones = _CN_DIGIT_MAP.get(tok[2], 0) if len(tok) >= 3 else 0
        return min(59, tens + ones)
    return 0


def _extract_time_of_day(
    raw: str,
    default_hm: tuple[int, int] = (9, 0),
    *,
    ref_dt: Optional[datetime] = None,
    has_future_day: bool = False,
) -> tuple[int, int, bool]:
    """提取文本中的时刻，返回 (hour, minute, matched_explicit_time)。"""
    cleaned_raw = strip_chat_divider_timestamps(raw)
    hour, minute = default_hm
    matched = False
    if "半夜" in cleaned_raw or "凌晨" in cleaned_raw:
        hour, minute, matched = 1, 30, True
    elif _OFF_WORK_RE.search(cleaned_raw):
        hour, minute, matched = 18, 30, True
    elif "明早" in cleaned_raw or "今早" in cleaned_raw or "早上" in cleaned_raw:
        hour, minute, matched = 8, 30, True
    elif "上午" in cleaned_raw:
        hour, minute, matched = 9, 30, True
    elif "中午" in cleaned_raw:
        hour, minute, matched = 12, 30, True
    elif "下午" in cleaned_raw:
        hour, minute, matched = 15, 0, True
    elif "晚上" in cleaned_raw or "今晚" in cleaned_raw or "明晚" in cleaned_raw:
        hour, minute, matched = 20, 0, True

    has_explicit_am = bool(_EXPLICIT_AM_RE.search(cleaned_raw))
    has_explicit_pm = bool(_EXPLICIT_PM_RE.search(cleaned_raw))

    # 匹配 HH:MM 或 中文点钟（排除 12.10 这类日期小数点）
    m_colon = re.search(r"(?<!\d)(\d{1,2})\s*[:：]\s*(\d{2})(?!\d)", cleaned_raw)
    if m_colon:
        h_val, m_val = int(m_colon.group(1)), int(m_colon.group(2))
        if 0 <= h_val <= 23 and 0 <= m_val <= 59:
            hour, minute, matched = h_val, m_val, True
            if has_explicit_pm and hour < 12:
                hour += 12
            elif "中午" in cleaned_raw and 1 <= hour <= 4:
                hour += 12
            return hour, minute, matched

    # 排除中文程度副词与条目次序误匹配为钟点（如「好看一点」「快一点」「第一点」「两点建议」「功能点」）
    cleaned_hm_raw = re.sub(
        r"(?:第\s*[一二两三四五六七八九十\d]+\s*点"
        r"|(?:看|好|快|慢|早|晚|多|少|大|小|近|远|简|全|准|细|深|差|有|略|稍微?|更|再|这|那|哪|一丁|半)\s*一点"
        r"|一点(?:儿|点|都|也|小|不|没)"
        r"|[一二两三四五六七八九十\d]+\s*点\s*(?:意见|建议|问题|原因|说明|要求|结论|想法|补充|注意|原则|特征))",
        " ",
        cleaned_raw,
    )
    m_hm = re.search(
        r"(\d{1,2}|[一二两三四五六七八九十]{1,2})\s*点\s*"
        r"(一刻|三刻|半|\d{1,2}|(?:零[一二三四五六七八九]|十[一二三四五六七八九]?|[二三四五]十[一二三四五六七八九]?)分?)?",
        cleaned_hm_raw,
    )
    if m_hm:
        h_tok = m_hm.group(1)
        h_val = int(h_tok) if h_tok.isdigit() else _CN_NUM.get(h_tok, hour)
        if 0 <= h_val <= 23:
            hour = h_val
            matched = True
            if has_explicit_pm and hour < 12:
                hour += 12
            elif "中午" in cleaned_raw and 1 <= hour <= 4:
                hour += 12
            elif 1 <= hour <= 6 and not has_explicit_am:
                # 中文口语『约个四点局 / 说的是四点了 / 明天4点 / 4点开会』未标凌晨/早上时默认指下午 13:00-18:00；
                # 仅在深夜 00:00-05:59 且无未来日期、无社交约定语境且目标小时大于当前小时（如 00:30 说『2点睡』）时保留凌晨。
                social_cue = bool(_SOCIAL_DAYTIME_CUE_RE.search(cleaned_raw))
                midnight_soon = (
                    ref_dt is not None
                    and ref_dt.hour < 6
                    and not has_future_day
                    and not social_cue
                    and hour > ref_dt.hour
                )
                if not midnight_soon:
                    hour += 12
            minute = _parse_cn_minute(m_hm.group(2))
    return hour, minute, matched


def try_parse_explicit_datetime(
    expr: str,
    ref_dt: Optional[datetime] = None,
    paths: Optional[SimingPaths] = None,
) -> Optional[tuple[datetime, bool]]:
    """尝试从自然语言或标准时间串中提取明确时间；若不含任何日期/时间线索则返回 None（绝不硬编明早 9 点）。"""
    now = (ref_dt or now_local()).astimezone(SHANGHAI_TZ)
    raw = expr.removeprefix("time:").strip()
    if not raw:
        return None
    policy = load_policy(paths)

    # 1. 标准 ISO（纯 YYYY-MM-DD 走公历日期分支默认 09:00，带时刻才按精确 ISO 解析）
    if not re.fullmatch(r"20\d{2}-\d{2}-\d{2}", raw):
        for candidate in (raw, raw.replace(" ", "T")):
            try:
                dt = datetime.fromisoformat(candidate)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=SHANGHAI_TZ)
                return dt.astimezone(SHANGHAI_TZ), False
            except ValueError:
                pass

    # 2. 纯相对时长或句内相对时长（如 2m / 30分钟后 / 2小时后 / 3天后）
    m_rel = _REL_DURATION_RE.match(raw)
    if m_rel:
        amount = int(m_rel.group(1))
        unit = m_rel.group(2).lower()
        if unit in ("s", "sec", "秒"):
            return now + timedelta(seconds=amount), False
        if unit in ("m", "min", "分钟", "分"):
            return now + timedelta(minutes=amount), False
        if unit in ("h", "hr", "小时", "个小时"):
            return now + timedelta(hours=amount), False
        return now + timedelta(days=amount), False

    m_inl = _REL_INLINE_RE.search(raw)
    if m_inl:
        if m_inl.group(1) == "半":
            return now + timedelta(minutes=30), False
        amt_raw = m_inl.group(2)
        if amt_raw.isdigit():
            amount = int(amt_raw)
        elif amt_raw in _CN_NUM:
            amount = _CN_NUM[amt_raw]
        elif amt_raw.startswith("十") and len(amt_raw) == 2 and amt_raw[1] in _CN_NUM:
            amount = 10 + _CN_NUM[amt_raw[1]]
        elif len(amt_raw) == 2 and amt_raw[0] in _CN_NUM and amt_raw[1] == "十":
            amount = _CN_NUM[amt_raw[0]] * 10
        else:
            amount = 0
        unit = m_inl.group(3)
        if amount > 0:
            if "秒" in unit:
                return now + timedelta(seconds=amount), False
            if "分" in unit:
                return now + timedelta(minutes=amount), False
            if "小时" in unit:
                return now + timedelta(hours=amount), False
            return now + timedelta(days=amount), False

    # 3. 显式公历日期：YYYY-MM-DD / YYYY年MM月DD日 / MM月DD日(号) / MM.DD（需带上下文防把版本号当日期）
    target_date: Optional[date] = None
    m_ymd = re.search(r"(20\d{2})\s*[-年/.]\s*(\d{1,2})\s*[-月/.]\s*(\d{1,2})\s*[日号]?", raw)
    if m_ymd:
        try:
            target_date = date(int(m_ymd.group(1)), int(m_ymd.group(2)), int(m_ymd.group(3)))
        except ValueError as exc:
            raise ValueError(f"非法公历日期: {raw[:40]}") from exc
    else:
        m_md_cn = re.search(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", raw)
        m_md_dot = re.search(
            r"(?:约了?|改到?|改期|定在|改|推迟到|安排在)?\s*(?<!\d)(1[0-2]|0?[1-9])\.(3[01]|[12]\d|0?[1-9])(?!\d|\.)",
            raw,
        )
        mo_da: Optional[tuple[int, int]] = None
        if m_md_cn:
            mo_da = (int(m_md_cn.group(1)), int(m_md_cn.group(2)))
        elif m_md_dot and re.search(r"(约|改|安装|送货|上门|提醒|号|日|上午|下午|晚上|之前|前)", raw):
            mo_da = (int(m_md_dot.group(1)), int(m_md_dot.group(2)))
        if mo_da:
            mo, da = mo_da
            for yr in (now.year, now.year + 1):
                try:
                    cand_d = date(yr, mo, da)
                except ValueError:
                    continue
                if cand_d >= now.date() or yr == now.year + 1:
                    target_date = cand_d
                    break
            if target_date is None:
                raise ValueError(f"非法月日日期: {mo}月{da}日")

    is_night_kw = ("半夜" in raw) or ("凌晨" in raw)
    nh_cfg, nm_cfg = (int(x) for x in str(policy["windows"]["night_batch_run"]).split(":"))
    default_hm = (nh_cfg, nm_cfg) if is_night_kw else (9, 0)

    if target_date is not None:
        hour, minute, _ = _extract_time_of_day(
            raw,
            default_hm=default_hm,
            ref_dt=now,
            has_future_day=(target_date > now.date()),
        )
        return datetime.combine(target_date, time(hour, minute), tzinfo=SHANGHAI_TZ), is_night_kw

    # 4. 个税汇算与指定月份月底
    if "个税" in raw or "汇算" in raw:
        win = tax_settlement_window(now.date())
        return datetime.fromisoformat(win["prep_remind_at"]), False

    m_me = re.search(r"(?:(1[0-2]|0?[1-9])\s*月)?月底", raw)
    if m_me:
        target_mo = int(m_me.group(1)) if m_me.group(1) else now.month
        target_yr = now.year if target_mo >= now.month else now.year + 1
        nxt_mo_first = date(target_yr + 1, 1, 1) if target_mo == 12 else date(target_yr, target_mo + 1, 1)
        last_day = nxt_mo_first - timedelta(days=1)
        if last_day < now.date() and m_me.group(1):
            nxt_mo_first = date(target_yr + 2, 1, 1) if target_mo == 12 else date(target_yr + 1, target_mo + 1, 1)
            last_day = nxt_mo_first - timedelta(days=1)
        return datetime.combine(last_day, time(10, 0), tzinfo=SHANGHAI_TZ), False

    # 5. 中文口语相对日 + 时刻（夜猫子 00:00~04:59 采用 05:00 切日逻辑日；未来日优先于闲聊里的“今天”）
    cleaned_date_raw = strip_chat_divider_timestamps(raw)
    base_date = logical_today(now)
    base_wd = base_date.weekday()
    day_offset: Optional[int] = None
    same_weekday_named = False
    if "大后天" in cleaned_date_raw:
        day_offset = 3
    elif "后天" in cleaned_date_raw:
        day_offset = 2
    elif "明天" in cleaned_date_raw or "明早" in cleaned_date_raw or "明晚" in cleaned_date_raw:
        day_offset = 1
    elif "下周" in cleaned_date_raw:
        m_wd = re.search(r"下周([一二三四五六日天])", cleaned_date_raw)
        if m_wd:
            target_wd = CN_WEEKDAY_MAP[m_wd.group(1)]
            day_offset = (7 - base_wd) + target_wd
        else:
            day_offset = 7
    elif re.search(r"(?:本?周|星期|礼拜)([一二三四五六日天])", cleaned_date_raw):
        m_wd = re.search(r"(?:本?周|星期|礼拜)([一二三四五六日天])", cleaned_date_raw)
        assert m_wd is not None
        target_wd = CN_WEEKDAY_MAP[m_wd.group(1)]
        diff = (target_wd - base_wd) % 7
        day_offset = diff
        same_weekday_named = diff == 0 and ("本周" not in cleaned_date_raw) and ("今天" not in cleaned_date_raw)
    elif "周末" in cleaned_date_raw:
        diff = (5 - base_wd) % 7
        day_offset = diff if diff > 0 else 0
        same_weekday_named = diff == 0 and ("今天" not in cleaned_date_raw)
    elif "今天" in cleaned_date_raw or "今晚" in cleaned_date_raw or "今早" in cleaned_date_raw:
        day_offset = 0

    hour, minute, has_time = _extract_time_of_day(
        cleaned_date_raw,
        default_hm=default_hm,
        ref_dt=now,
        has_future_day=(day_offset is not None and day_offset > 0),
    )
    if day_offset is None and not has_time and not is_night_kw:
        return None

    eff_offset = day_offset if day_offset is not None else 0
    # “今晚半夜”在 20:00 说时实际指跨过 00:00 的次日 01:30；在 00:00~04:59 逻辑日已是昨天，+1 正好是此刻之后
    if is_night_kw and ("今晚" in raw or day_offset is None) and hour < 6:
        eff_offset = max(eff_offset, 1)

    candidate_dt = datetime.combine(
        base_date + timedelta(days=eff_offset), time(hour, minute), tzinfo=SHANGHAI_TZ
    )
    if candidate_dt <= now and day_offset in (None, 0):
        candidate_dt += timedelta(days=7 if same_weekday_named else 1)
    elif candidate_dt <= now and base_date != now.date() and day_offset == 1 and hour < 5:
        candidate_dt += timedelta(days=1)

    if _OFF_WORK_RE.search(raw) and not day_info(candidate_dt.date(), paths).is_workday:
        probe_d = candidate_dt.date()
        for _ in range(10):
            probe_d += timedelta(days=1)
            if day_info(probe_d, paths).is_workday:
                candidate_dt = datetime.combine(probe_d, time(hour, minute), tzinfo=SHANGHAI_TZ)
                break

    return candidate_dt, is_night_kw


def compile_time_trigger(
    expr: str,
    ref_dt: Optional[datetime] = None,
    paths: Optional[SimingPaths] = None,
) -> tuple[datetime, bool]:
    """把时间表达式编译为东八区精确 datetime，并返回 silent_night 标记（如半夜后台跑任务）。"""
    parsed = try_parse_explicit_datetime(expr, ref_dt=ref_dt, paths=paths)
    if parsed is not None:
        return parsed
    now = (ref_dt or now_local()).astimezone(SHANGHAI_TZ)
    fallback = datetime.combine(now.date(), time(9, 0), tzinfo=SHANGHAI_TZ)
    if fallback <= now:
        fallback += timedelta(days=1)
    return fallback, False
