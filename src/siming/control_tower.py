"""W4-6 (Part B) · 司命统一网页控制塔与管理控制台（零额外依赖 Python stdlib ThreadingHTTPServer + 单页应用 SPA）。

安全边界：
- 默认仅监听 `127.0.0.1:8788`，经由本机 Caddy 反代对外暴露（`edge.example.com:8443/siming/tower/`）；
- 强制鉴权（HTTP Basic / Bearer / X-Siming-Token，采用 `hmac.compare_digest` 恒定时间比对）；
- 所有管理写操作（模块开关、待办销账/取消、挂起取消、审批作废、订阅状态标记/重扫、图谱重建、Wiki 蒸馏与编辑、Routines 编辑）均强制鉴权并落盘 `audit` 审计表；
- 绝不在网页端自动执行 A2 审批（仅允许安全作废 `POST /api/approvals/<token>/cancel`）；
- 严格不含已暂缓的 W4-2 与 W4-4。
"""

from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional

from siming import db as _db
from siming.compute_assets import evaluate_compute_guardrails, upsert_compute_asset
from siming.compute_monitor import build_compute_monitor_payload, set_provider_circuit
from siming.config import load_policy
from siming.context_graph import get_graph_stats, index_context_graph, query_context_graph
from siming.crypto import read_secret, write_secret
from siming.db import SimingDB, iso_now
from siming.event_chain import routines_md_path
from siming.hindsight_wiki import (
    distill_hindsight_wiki,
    list_wiki_conflicts,
    reopen_wiki_conflict,
    resolve_wiki_conflict,
    restore_filtered_conflict,
    summarize_wiki_conflicts,
)
from siming.modules import list_modules_status, set_module_enabled
from siming.replenish_cart import (
    build_replenish_tower_payload,
    evaluate_replenish_due,
    mark_replenish_bought,
    remove_replenish_item,
    seed_daily_replenish_catalog,
    send_replenish_due_reminder,
    snooze_replenish_item,
    toggle_replenish_item,
    upsert_replenish_item,
)
from siming.social_boardgames import check_upcoming_crowdfund_balances
from siming.tower_ops import (
    build_ops_tower_payload,
    ops_restore_push_rule,
    ops_run_diagnose,
    ops_set_pause,
    read_schedule_output_preview,
)

_WIKI_SLUG_RE = re.compile(r"^[A-Za-z0-9_-]{1,80}$")
_WAVE_TITLE_RE = re.compile(r"\(W([1-4])-\d+\)")
_CLOSED_CASE_STATUSES = ("完成", "取消", "已关闭", "已取消", "已归档", "已收藏", "CLOSED", "DONE")

_WAVE_LABELS: dict[int, str] = {
    0: "Wave 0 · 核心与日常",
    1: "Wave 1 · 财务与生活",
    2: "Wave 2 · 新房与实验室",
    3: "Wave 3 · 知识与研发进化",
    4: "Wave 4 · 个人 OS 终态",
}

_WAVE_FALLBACK_IDS: dict[str, int] = {
    "sure_finance_os": 1,
    "compute_assets": 1,
    "sub_assassin": 1,
    "meal_combo_guard": 1,
    "parcel_sentinel": 1,
    "health_cockpit": 1,
    "vehicle_cfo": 1,
    "home_air_guard": 2,
    "homelab_ops": 2,
    "bambu_copilot": 2,
    "haos_night_owl": 2,
    "commute_hub": 2,
    "home_passport": 2,
    "multi_runtime": 3,
    "hindsight_wiki": 3,
    "msim_memory": 3,
    "overnight_lab": 3,
    "research_pr_radar": 3,
    "digest_indie": 3,
    "infra_healer": 3,
    "context_graph": 4,
    "trip_packager": 4,
    "social_boardgames": 4,
    "compound_tower": 4,
}


def _resolve_tower_token(db: SimingDB, explicit_token: str = "") -> str:
    if explicit_token and explicit_token.strip():
        return explicit_token.strip()
    env_tok = os.environ.get("SIMING_TOWER_TOKEN", "").strip()
    if env_tok:
        return env_tok
    sec = read_secret("tower-token", db.paths)
    if sec:
        return sec
    generated = secrets.token_urlsafe(24)
    write_secret("tower-token", generated, db.paths)
    return generated


def _verify_auth_header(headers: Any, expected_token: str) -> bool:
    if not expected_token:
        return False
    x_tok = (headers.get("X-Siming-Token") or "").strip()
    if x_tok and hmac.compare_digest(x_tok, expected_token):
        return True

    auth = (headers.get("Authorization") or "").strip()
    if not auth:
        return False
    if auth.lower().startswith("bearer "):
        cand = auth[7:].strip()
        return hmac.compare_digest(cand, expected_token)
    if auth.lower().startswith("basic "):
        b64_part = auth[6:].strip()
        try:
            decoded = base64.b64decode(b64_part).decode("utf-8", errors="strict")
        except Exception:
            return False
        if ":" in decoded:
            _, pwd = decoded.split(":", 1)
            return hmac.compare_digest(pwd, expected_token) or hmac.compare_digest(decoded, expected_token)
        return hmac.compare_digest(decoded, expected_token)
    return False


def _safe_json(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (dict, list)):
        return raw
    try:
        return json.loads(str(raw)) if str(raw) else default
    except (ValueError, TypeError):
        return default


def _module_wave(module_id: str, title: str) -> int:
    m = _WAVE_TITLE_RE.search(title or "")
    if m:
        return int(m.group(1))
    return _WAVE_FALLBACK_IDS.get(module_id, 0)


_FX_TO_CNY: dict[str, float] = {
    "CNY": 1.0,
    "RMB": 1.0,
    "USD": 7.25,
    "JPY": 0.048,
    "EUR": 7.85,
    "GBP": 9.15,
    "SGD": 5.40,
    "HKD": 0.93,
    "KRW": 0.0052,
    "THB": 0.21,
    "AUD": 4.75,
}


_ANNUAL_CYCLES_MAP: dict[int, float] = {7: 52.0, 14: 26.0, 30: 12.0, 90: 4.0, 365: 1.0}


def _calc_sub_cost_cny(sub: dict[str, Any]) -> tuple[float, float]:
    amt_yuan = float(sub.get("amount_cents") or 0) / 100.0
    cad = max(1, int(sub.get("cadence_days") or 30))
    ccy = str(sub.get("currency") or "CNY").strip().upper()
    fx = float(_FX_TO_CNY.get(ccy, 1.0))
    cycles_per_year = _ANNUAL_CYCLES_MAP.get(cad, 365.0 / cad)
    annual_cny = amt_yuan * fx * cycles_per_year
    monthly_cny = annual_cny / 12.0
    return monthly_cny, annual_cny


def _compute_sub_summary(subscriptions: list[dict[str, Any]]) -> dict[str, Any]:
    active_cnt = 0
    keep_cnt = 0
    zombie_cnt = 0
    zombie_total_cnt = 0
    stopped_cnt = 0
    cancelled_cnt = 0
    annual_cost = 0.0
    zombie_annual_cost = 0.0
    stopped_annual_cost = 0.0
    for s in subscriptions:
        st = str(s.get("status") or "ACTIVE").upper()
        is_ongoing = bool(s.get("is_ongoing", st != "CANCELLED"))
        _row_monthly, row_annual = _calc_sub_cost_cny(s)
        if st == "ZOMBIE":
            zombie_total_cnt += 1
        if st == "CANCELLED":
            cancelled_cnt += 1
            continue
        if not is_ongoing:
            stopped_cnt += 1
            stopped_annual_cost += row_annual
            continue
        if st == "ACTIVE":
            active_cnt += 1
        elif st == "KEEP":
            keep_cnt += 1
        elif st == "ZOMBIE":
            zombie_cnt += 1
            zombie_annual_cost += row_annual
        annual_cost += row_annual
    monthly_cost = annual_cost / 12.0
    zombie_monthly_cost = zombie_annual_cost / 12.0
    clean_monthly_cost = (annual_cost - zombie_annual_cost) / 12.0
    stopped_monthly_cost = stopped_annual_cost / 12.0
    ongoing_cnt = active_cnt + keep_cnt + zombie_cnt
    return {
        "total": len(subscriptions),
        "ongoing_count": ongoing_cnt,
        "active_count": ongoing_cnt,
        "clean_active_count": active_cnt + keep_cnt,
        "ongoing_zombie_count": zombie_cnt,
        "zombie_count": zombie_cnt,
        "zombie_total_count": zombie_total_cnt,
        "stopped_count": stopped_cnt,
        "cancelled_count": cancelled_cnt,
        "monthly_cost_cny": round(monthly_cost, 2),
        "annual_cost_cny": round(annual_cost, 2),
        "zombie_monthly_cost_cny": round(zombie_monthly_cost, 2),
        "clean_monthly_cost_cny": round(clean_monthly_cost, 2),
        "stopped_monthly_cost_cny": round(stopped_monthly_cost, 2),
    }


def _finance_payload(db: SimingDB) -> dict[str, Any]:
    from siming.sub_hunter import enrich_subscription_row

    db.init_db()
    conn = db.connect()
    try:
        active_txns = conn.execute(
            """
            SELECT amount_cents, currency, kind FROM ledger_txns
            WHERE COALESCE(UPPER(status), '') NOT IN ('VOID_DUPLICATE', 'VOID', 'CANCELLED')
            """
        ).fetchall()
        txn_cnt = len(active_txns)
        total_spend_cny = 0.0
        for tr in active_txns:
            if str(tr["kind"] or "") == "消费":
                ccy = str(tr["currency"] or "CNY").strip().upper()
                rate = float(_FX_TO_CNY.get(ccy, 1.0))
                total_spend_cny += (int(tr["amount_cents"] or 0) / 100.0) * rate
        sure_synced = int(conn.execute("SELECT COUNT(*) FROM sure_sync_map WHERE status='SYNCED'").fetchone()[0])
        sure_queued = int(conn.execute("SELECT COUNT(*) FROM sure_sync_map WHERE status='QUEUED'").fetchone()[0])
        sure_rules = [dict(r) for r in conn.execute("SELECT * FROM sure_rules ORDER BY created_at DESC LIMIT 50").fetchall()]
        compute_assets = [dict(r) for r in conn.execute("SELECT * FROM compute_assets ORDER BY updated_at DESC").fetchall()]
        raw_subs = [dict(r) for r in conn.execute("SELECT * FROM subscription_audit").fetchall()]
        recent_txns = [
            dict(r)
            for r in conn.execute(
                "SELECT txn_id, occurred_at, amount_cents, currency, account_alias, card_last4, kind, status, merchant "
                "FROM ledger_txns ORDER BY occurred_at DESC LIMIT 30"
            ).fetchall()
        ]
    finally:
        conn.close()
    cur_dt = _db.now_local()
    subscriptions = [enrich_subscription_row(r, now=cur_dt) for r in raw_subs]
    for s in subscriptions:
        m_cny, a_cny = _calc_sub_cost_cny(s)
        s["monthly_cost_cny"] = round(m_cny, 2)
        s["annual_cost_cny"] = round(a_cny, 2)
    status_rank = {"ZOMBIE": 0, "ACTIVE": 1, "KEEP": 2, "CANCELLED": 3}
    subscriptions.sort(
        key=lambda s: (
            0 if (s["is_ongoing"] and s["status"] != "CANCELLED") else (1 if s["status"] != "CANCELLED" else 2),
            status_rank.get(str(s["status"]), 9),
            str(s.get("next_expected_at") or "") if s["is_ongoing"] else "",
            "" if s["is_ongoing"] else str(s.get("last_charged_at") or ""),
        )
    )
    # 停扣/历史订阅按最近扣费倒序排列，更易复盘
    ongoing_part = [s for s in subscriptions if s["is_ongoing"] and s["status"] != "CANCELLED"]
    other_part = [s for s in subscriptions if not (s["is_ongoing"] and s["status"] != "CANCELLED")]
    other_part.sort(
        key=lambda s: (
            0 if s["status"] != "CANCELLED" else 1,
            str(s.get("last_charged_at") or ""),
        ),
        reverse=False,
    )
    # 细分：已停扣按 last_charged_at 倒序，已取消按 updated_at 倒序
    stopped_part = sorted(
        [s for s in other_part if s["status"] != "CANCELLED"],
        key=lambda s: str(s.get("last_charged_at") or ""),
        reverse=True,
    )
    cancelled_part = sorted(
        [s for s in other_part if s["status"] == "CANCELLED"],
        key=lambda s: str(s.get("updated_at") or ""),
        reverse=True,
    )
    subscriptions = ongoing_part + stopped_part + cancelled_part
    sub_summary = _compute_sub_summary(subscriptions)
    return {
        "ok": True,
        "sure": {
            "ledger_txn_count": txn_cnt,
            "total_spend_cny_yuan": round(total_spend_cny, 2),
            "synced_count": sure_synced,
            "queued_count": sure_queued,
            "rules_count": len(sure_rules),
            "rules": sure_rules,
        },
        "sub_summary": sub_summary,
        "compute_assets": compute_assets,
        "subscriptions": subscriptions,
        "recent_txns": recent_txns,
    }


_CHART_CATEGORIES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "餐饮外卖",
        "#f97316",
        re.compile(
            r"(美团|饿了么|瑞幸|luckin|星巴克|麦当劳|肯德基|喜茶|霸王茶姬|库迪|奶茶|咖啡|餐饮|餐厅|火锅|烧烤|米粉|拉面|面馆|瓦罐|饺子|包子|果切|水果|示例果切|示例粉面|示例粉面|示例瓦罐汤|海底捞|西贝|南京大牌档|素坤泰|便利蜂|罗森|全家|7-?11|面包|蛋糕|烘焙|甜品|食堂|小吃|肉夹馍)",
            re.IGNORECASE,
        ),
    ),
    (
        "AI 算力",
        "#6366f1",
        re.compile(
            r"(nous|deepseek|aliyun|阿里云|dashscope|kimi|月之暗面|claude|anthropic|openai|chatgpt|redpill|codex|magpie|cursor|github|autodl|算力|gpu|api\s*充值|openrouter)",
            re.IGNORECASE,
        ),
    ),
    (
        "出行车务",
        "#38bdf8",
        re.compile(
            r"(滴滴|didi|打车|花小猪|曹操出行|高德|地铁|公交|一卡通|12306|铁路|航空|机票|机场|携程|去哪儿|飞猪|酒店|民宿|tesla|特斯拉|超充|特来电|星星充电|国家电网|小桔充电|充电|etc|速通卡|高速|停车|泊车|车库|车险|中石化|中石油|加油|洗车)",
            re.IGNORECASE,
        ),
    ),
    (
        "家居装修",
        "#14b8a6",
        re.compile(
            r"(装修|建材|宜家|ikea|西门子|海尔|美的|格力|老板|林内|科沃斯|凯度|极米|米家|小米|绿联|拓竹|家电|物业|水费|电费|燃气|供暖|宽带|暖气|厨卫|灯具)",
            re.IGNORECASE,
        ),
    ),
    (
        "网购商超",
        "#ec4899",
        re.compile(
            r"(京东|淘宝|天猫|拼多多|抖音电商|抖音商城|山姆|盒马|超市|物美|永辉|大润发|沃尔玛|屈臣氏|名创优品|无印良品|得物|闲鱼|唯品会|叮咚|多点|丰巢|菜鸟|顺丰|快递|邮政)",
            re.IGNORECASE,
        ),
    ),
    (
        "订阅数字",
        "#a855f7",
        re.compile(
            r"(apple|icloud|app\s*store|itunes|game\s*pass|xbox|steam|迅雷|点众|快看|番茄小说|七猫|起点|阅文|百度网盘|百度文库|夸克|阿里云盘|网易云|qq音乐|spotify|youtube|netflix|会员|续费|连续包月|wps|飞书|剪映|知乎|微信读书|得到|樊登)",
            re.IGNORECASE,
        ),
    ),
    (
        "医疗健康",
        "#10b981",
        re.compile(
            r"(医院|常用医院|门诊|挂号|诊所|药房|药店|大药房|体检|周期用药|口腔|牙科|医保|美团买药|叮当快药|京亿康|医疗)",
            re.IGNORECASE,
        ),
    ),
    (
        "娱乐桌游",
        "#eab308",
        re.compile(
            r"(桌游|集石|摩点|新物集|剧本杀|电影|影院|演出|门票|游戏|任天堂|switch|playstation|psn|bilibili|哔哩哔哩|b站|漫画|小说|短剧)",
            re.IGNORECASE,
        ),
    ),
)

_CHART_CAT_COLORS: dict[str, str] = {name: color for name, color, _ in _CHART_CATEGORIES}
_CHART_CAT_COLORS["其他消费"] = "#94a3b8"


def _classify_txn_category(
    merchant: str,
    notes: str,
    compiled_rules: list[tuple[re.Pattern[str], str]],
) -> str:
    hay = f"{merchant or ''} {notes or ''}".strip()
    if not hay:
        return "其他消费"
    for pat, cat in compiled_rules:
        if pat.search(hay):
            return cat
    for cat, _, pat in _CHART_CATEGORIES:
        if pat.search(hay):
            return cat
    return "其他消费"


def _probe_sure_status(db: SimingDB) -> dict[str, Any]:
    """服务端轻量探测本地 Sure 财务 OS 实例是否可达（默认 http://127.0.0.1:3032）。"""
    policy = load_policy(db.paths)
    sure_cfg = policy.get("sure") if isinstance(policy.get("sure"), dict) else {}
    local_url = (
        os.environ.get("SURE_API_URL")
        or str(sure_cfg.get("local_url") or "")
        or (db.get_meta("sure_api_url") or "")
        or "http://127.0.0.1:3032"
    ).strip().rstrip("/")
    public_url = (
        os.environ.get("SIMING_SURE_PUBLIC_URL")
        or str(sure_cfg.get("public_url") or "")
        or (db.get_meta("sure_public_url") or "")
    ).strip().rstrip("/")

    ts = iso_now()
    if os.environ.get("SIMING_TEST_OFFLINE") == "1" and not os.environ.get("SIMING_TOWER_PROBE_SURE"):
        return {
            "running": False,
            "local_url": local_url,
            "public_url": public_url,
            "status_code": 0,
            "checked_at": ts,
            "skipped": "offline",
        }

    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    status_code = 0
    running = False
    for suffix in ("/up", "/"):
        try:
            req = urllib.request.Request(local_url + suffix, method="GET")
            with opener.open(req, timeout=1.5) as resp:
                status_code = int(resp.status)
                if 200 <= status_code < 400:
                    running = True
                    break
        except urllib.error.HTTPError as exc:
            status_code = int(exc.code)
            if 200 <= status_code < 400:
                running = True
                break
        except Exception:
            continue
    return {
        "running": running,
        "local_url": local_url,
        "public_url": public_url,
        "status_code": status_code,
        "checked_at": ts,
    }


def _month_sequence(anchor_ym: str, count: int = 12) -> list[str]:
    try:
        y, m = int(anchor_ym[:4]), int(anchor_ym[5:7])
    except Exception:
        cur = _db.now_local()
        y, m = cur.year, cur.month
    out: list[str] = []
    for _ in range(max(1, count)):
        out.append(f"{y:04d}-{m:02d}")
        m -= 1
        if m <= 0:
            m = 12
            y -= 1
    out.reverse()
    return out


def _finance_charts_payload(db: SimingDB, months: int = 12) -> dict[str, Any]:
    """为控制塔财务页提供 Sure 风格多维聚合图表数据（严格隔离 CNY/USD，剔除划转，退款抵减净支出）。"""
    db.init_db()
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT occurred_at, amount_cents, currency, account_alias, kind, merchant, notes
            FROM ledger_txns
            WHERE COALESCE(UPPER(status), '') NOT IN ('VOID_DUPLICATE', 'VOID', 'CANCELLED')
            ORDER BY occurred_at ASC
            """
        ).fetchall()
        rule_rows = conn.execute(
            "SELECT match_pattern, category FROM sure_rules WHERE COALESCE(category, '') != ''"
        ).fetchall()
        sub_rows = conn.execute(
            "SELECT * FROM subscription_audit WHERE status != 'CANCELLED' ORDER BY amount_cents DESC LIMIT 50"
        ).fetchall()
    finally:
        conn.close()

    compiled_rules: list[tuple[re.Pattern[str], str]] = []
    for rr in rule_rows:
        pat_s = str(rr["match_pattern"] or "").strip()
        cat_s = str(rr["category"] or "").strip()
        if pat_s and cat_s:
            try:
                compiled_rules.append((re.compile(pat_s, re.IGNORECASE), cat_s))
            except re.error:
                continue

    cur_ym = _db.now_local().strftime("%Y-%m")
    all_months_seen: list[str] = []
    for r in rows:
        ym = str(r["occurred_at"] or "")[:7]
        if len(ym) == 7 and ym[4] == "-":
            all_months_seen.append(ym)
    anchor_ym = max(all_months_seen[-1], cur_ym) if all_months_seen else cur_ym
    # 若最新流水月即当月或上月，锚定在最新有数据月（保证 12m 图表末尾永远有数据）
    if all_months_seen and all_months_seen[-1] < cur_ym:
        anchor_ym = cur_ym
    m_seq = _month_sequence(anchor_ym, count=max(3, min(int(months), 24)))
    m_set = set(m_seq)
    m_cur = all_months_seen[-1] if all_months_seen else cur_ym
    m_3_set = set(_month_sequence(m_cur, count=3))
    m_12_set = set(_month_sequence(m_cur, count=12))

    cny_monthly: dict[str, dict[str, int]] = {
        m: {"income": 0, "gross_spend": 0, "refunds": 0, "transfers": 0, "count": 0} for m in m_seq
    }
    usd_monthly: dict[str, dict[str, int]] = {
        m: {"spend": 0, "repay_transfer": 0, "count": 0} for m in m_seq
    }
    other_fx: dict[str, dict[str, Any]] = {}
    accounts_agg: dict[tuple[str, str], dict[str, Any]] = {}

    cat_windows: dict[str, dict[str, dict[str, int]]] = {
        "current_month": defaultdict(lambda: {"cents": 0, "count": 0}),
        "last_3m": defaultdict(lambda: {"cents": 0, "count": 0}),
        "last_12m": defaultdict(lambda: {"cents": 0, "count": 0}),
    }
    merch_windows: dict[str, dict[str, dict[str, int]]] = {
        "current_month": defaultdict(lambda: {"cents": 0, "count": 0}),
        "last_3m": defaultdict(lambda: {"cents": 0, "count": 0}),
        "last_12m": defaultdict(lambda: {"cents": 0, "count": 0}),
    }
    usd_merchants: dict[str, dict[str, int]] = defaultdict(lambda: {"cents": 0, "count": 0})
    usd_total_all = 0
    usd_transfer_all = 0

    for r in rows:
        ym = str(r["occurred_at"] or "")[:7]
        amt = int(r["amount_cents"] or 0)
        ccy = str(r["currency"] or "CNY").strip().upper()
        if ccy == "RMB":
            ccy = "CNY"
        kind = str(r["kind"] or "消费").strip()
        merchant = str(r["merchant"] or "").strip()
        notes = str(r["notes"] or "").strip()
        alias = str(r["account_alias"] or "").strip() or "未标注账户"
        # 归并带优惠后缀的账户别名（如 `建设银行储蓄卡(1004)&立减金` -> `建设银行储蓄卡(1004)`）
        clean_alias = alias.split("&", 1)[0].strip() or alias

        ak = (clean_alias, ccy)
        acc = accounts_agg.setdefault(ak, {"account": clean_alias, "currency": ccy, "spend_cents": 0, "income_cents": 0, "count": 0})
        acc["count"] += 1
        if kind == "消费":
            acc["spend_cents"] += amt
        elif kind == "退款":
            acc["spend_cents"] -= amt
        elif kind == "收入":
            acc["income_cents"] += amt

        if ccy == "CNY":
            if ym in m_set:
                b = cny_monthly[ym]
                b["count"] += 1
                if kind == "消费":
                    b["gross_spend"] += amt
                elif kind == "退款":
                    b["refunds"] += amt
                elif kind == "收入":
                    b["income"] += amt
                elif kind == "划转":
                    b["transfers"] += amt

            if kind in ("消费", "退款"):
                delta = amt if kind == "消费" else -amt
                cat_name = _classify_txn_category(merchant, notes, compiled_rules)
                m_label = merchant if merchant and merchant not in ("/", "-", "未知", "待补充") else "未标注商户"
                for win_key, win_set in (("current_month", {m_cur}), ("last_3m", m_3_set), ("last_12m", m_12_set)):
                    if ym in win_set:
                        cat_windows[win_key][cat_name]["cents"] += delta
                        if kind == "消费":
                            cat_windows[win_key][cat_name]["count"] += 1
                        merch_windows[win_key][m_label]["cents"] += delta
                        if kind == "消费":
                            merch_windows[win_key][m_label]["count"] += 1
        elif ccy == "USD":
            if kind == "消费":
                usd_total_all += amt
                m_label = merchant or "Nous / USD 账单"
                usd_merchants[m_label]["cents"] += amt
                usd_merchants[m_label]["count"] += 1
            elif kind == "退款":
                usd_total_all -= amt
            elif kind == "划转":
                usd_transfer_all += amt
            if ym in m_set:
                ub = usd_monthly[ym]
                ub["count"] += 1
                if kind == "消费":
                    ub["spend"] += amt
                elif kind == "退款":
                    ub["spend"] -= amt
                elif kind == "划转":
                    ub["repay_transfer"] += amt
        else:
            fx = other_fx.setdefault(ccy, {"currency": ccy, "spend_cents": 0, "count": 0})
            if kind == "消费":
                fx["spend_cents"] += amt
                fx["count"] += 1
            elif kind == "退款":
                fx["spend_cents"] -= amt

    monthly_cny_out: list[dict[str, Any]] = []
    cum_cents = 0
    total_12m_income = 0
    total_12m_net_spend = 0
    total_12m_refunds = 0
    for ym in m_seq:
        b = cny_monthly[ym]
        net_spend = b["gross_spend"] - b["refunds"]
        net_cf = b["income"] - net_spend
        cum_cents += net_cf
        total_12m_income += b["income"]
        total_12m_net_spend += net_spend
        total_12m_refunds += b["refunds"]
        monthly_cny_out.append(
            {
                "month": ym,
                "income": round(b["income"] / 100.0, 2),
                "gross_spend": round(b["gross_spend"] / 100.0, 2),
                "refunds": round(b["refunds"] / 100.0, 2),
                "net_spend": round(net_spend / 100.0, 2),
                "net_cashflow": round(net_cf / 100.0, 2),
                "cumulative_net": round(cum_cents / 100.0, 2),
                "transfers": round(b["transfers"] / 100.0, 2),
                "count": b["count"],
            }
        )

    monthly_usd_out: list[dict[str, Any]] = []
    usd_12m_spend = 0
    for ym in m_seq:
        ub = usd_monthly[ym]
        usd_12m_spend += ub["spend"]
        monthly_usd_out.append(
            {
                "month": ym,
                "spend": round(ub["spend"] / 100.0, 2),
                "repay_transfer": round(ub["repay_transfer"] / 100.0, 2),
                "count": ub["count"],
            }
        )

    def _pack_cats(bucket: dict[str, dict[str, int]]) -> dict[str, Any]:
        pos = [(name, max(0, v["cents"]), v["count"]) for name, v in bucket.items() if v["cents"] > 0]
        pos.sort(key=lambda x: x[1], reverse=True)
        tot = sum(x[1] for x in pos)
        items = [
            {
                "name": name,
                "amount": round(c / 100.0, 2),
                "pct": round((c / tot) * 100.0, 1) if tot > 0 else 0.0,
                "count": cnt,
                "color": _CHART_CAT_COLORS.get(name, "#60a5fa"),
            }
            for name, c, cnt in pos
        ]
        return {"total": round(tot / 100.0, 2), "items": items}

    def _pack_merchants(bucket: dict[str, dict[str, int]]) -> list[dict[str, Any]]:
        pos = [(name, v["cents"], v["count"]) for name, v in bucket.items() if v["cents"] > 0]
        pos.sort(key=lambda x: x[1], reverse=True)
        return [{"merchant": name, "amount": round(c / 100.0, 2), "count": cnt} for name, c, cnt in pos[:10]]

    accounts_out = sorted(accounts_agg.values(), key=lambda x: abs(x["spend_cents"]) + abs(x["income_cents"]), reverse=True)[:12]
    for a in accounts_out:
        a["spend"] = round(a["spend_cents"] / 100.0, 2)
        a["income"] = round(a["income_cents"] / 100.0, 2)

    from siming.sub_hunter import enrich_subscription_row

    cur_dt_charts = _db.now_local()
    recurring_out = []
    for raw_s in sub_rows:
        s = enrich_subscription_row(dict(raw_s), now=cur_dt_charts)
        if not s["is_ongoing"]:
            continue
        recurring_out.append(
            {
                "sub_id": str(s["sub_id"]),
                "name": str(s["normalized_name"] or s["merchant"]),
                "currency": str(s["currency"] or "CNY"),
                "amount": round(int(s["amount_cents"] or 0) / 100.0, 2),
                "cadence_days": int(s["cadence_days"] or 30),
                "monthly_est": round((int(s["amount_cents"] or 0) / 100.0) * (30.0 / max(1, int(s["cadence_days"] or 30))), 2),
                "status": str(s["status"] or "ACTIVE"),
                "is_ongoing": bool(s["is_ongoing"]),
                "ongoing_label": str(s["ongoing_label"]),
                "last_charged_at": str(s.get("last_charged_at") or ""),
                "next_expected_at": str(s["next_expected_at"] or ""),
            }
        )
    recurring_out.sort(key=lambda x: x["monthly_est"], reverse=True)
    recurring_out = recurring_out[:20]

    return {
        "ok": True,
        "current_month": m_cur,
        "cny_summary_12m": {
            "income": round(total_12m_income / 100.0, 2),
            "net_spend": round(total_12m_net_spend / 100.0, 2),
            "refunds": round(total_12m_refunds / 100.0, 2),
            "net_cashflow": round((total_12m_income - total_12m_net_spend) / 100.0, 2),
        },
        "monthly_cny": monthly_cny_out,
        "categories": {k: _pack_cats(v) for k, v in cat_windows.items()},
        "top_merchants": {k: _pack_merchants(v) for k, v in merch_windows.items()},
        "usd": {
            "total_all_spend": round(usd_total_all / 100.0, 2),
            "total_all_repay_transfer": round(usd_transfer_all / 100.0, 2),
            "spend_12m": round(usd_12m_spend / 100.0, 2),
            "monthly": monthly_usd_out,
            "top_merchants": _pack_merchants(usd_merchants)[:6],
        },
        "other_fx": [
            {"currency": v["currency"], "spend": round(v["spend_cents"] / 100.0, 2), "count": v["count"]}
            for v in sorted(other_fx.values(), key=lambda x: x["spend_cents"], reverse=True)
        ],
        "accounts": accounts_out,
        "recurring": recurring_out[:12],
        "sure_status": _probe_sure_status(db),
    }


def _smarthome_payload(db: SimingDB) -> dict[str, Any]:
    db.init_db()
    conn = db.connect()
    try:
        air = [dict(r) for r in conn.execute("SELECT * FROM home_air_readings ORDER BY recorded_at DESC LIMIT 12").fetchall()]
        nodes = [dict(r) for r in conn.execute("SELECT * FROM homelab_nodes ORDER BY name ASC").fetchall()]
        spools = [dict(r) for r in conn.execute("SELECT * FROM bambu_spools ORDER BY remaining_grams ASC").fetchall()]
        jobs = [dict(r) for r in conn.execute("SELECT * FROM bambu_jobs ORDER BY started_at DESC LIMIT 10").fetchall()]
        utilities = [dict(r) for r in conn.execute("SELECT * FROM home_utilities ORDER BY next_due_at ASC").fetchall()]
        consumables = [dict(r) for r in conn.execute("SELECT * FROM appliance_consumables ORDER BY next_replace_at ASC").fetchall()]
        sit_rows = conn.execute(
            "SELECT key, value_json, as_of, source FROM situation WHERE key IN ('sleep:state', 'loc:me', 'tesla:main')"
        ).fetchall()
        haos_night_owl = {
            str(r["key"]): {"value": _safe_json(r["value_json"], {}), "as_of": r["as_of"], "source": r["source"]}
            for r in sit_rows
        }
    finally:
        conn.close()
    return {
        "ok": True,
        "air_readings": air,
        "homelab_nodes": nodes,
        "bambu_spools": spools,
        "bambu_jobs": jobs,
        "utilities": utilities,
        "consumables": consumables,
        "haos_night_owl": haos_night_owl,
    }


def _wiki_payload(db: SimingDB) -> dict[str, Any]:
    conn = db.connect_readonly()
    try:
        pages = []
        for r in conn.execute("SELECT * FROM wiki_pages ORDER BY updated_at DESC").fetchall():
            pages.append(
                {
                    "slug": str(r["slug"]),
                    "title": str(r["title"]),
                    "category": str(r["category"]),
                    "content_md": str(r["content_md"]),
                    "sources": _safe_json(r["sources_json"], []),
                    "conflicts": _safe_json(r["conflicts_json"], []),
                    "updated_at": str(r["updated_at"]),
                    "last_distilled_at": str(r["last_distilled_at"]),
                }
            )
    finally:
        conn.close()
    return {"ok": True, "count": len(pages), "pages": pages}


def _msim_payload(db: SimingDB) -> dict[str, Any]:
    conn = db.connect_readonly()
    try:
        entries = []
        for r in conn.execute("SELECT * FROM msim_entries ORDER BY created_at DESC LIMIT 100").fetchall():
            d = dict(r)
            d["tags"] = _safe_json(d.get("tags_json"), [])
            d["delta"] = round(float(d.get("measured_score") or 0.0) - float(d.get("baseline_score") or 0.0), 4)
            entries.append(d)
        lb_rows = conn.execute(
            """
            SELECT source_model,
                   COUNT(*) AS runs,
                   AVG(measured_score) AS avg_score,
                   AVG(measured_score - baseline_score) AS avg_delta,
                   SUM(CASE WHEN passed = 1 THEN 1 ELSE 0 END) AS pass_cnt
            FROM msim_entries
            GROUP BY source_model
            ORDER BY avg_score DESC
            """
        ).fetchall()
    finally:
        conn.close()
    leaderboard = [
        {
            "source_model": str(r["source_model"]),
            "runs": int(r["runs"]),
            "avg_score": round(float(r["avg_score"] or 0.0), 4),
            "avg_delta": round(float(r["avg_delta"] or 0.0), 4),
            "pass_rate": round((int(r["pass_cnt"] or 0) / max(1, int(r["runs"]))) * 100.0, 1),
        }
        for r in lb_rows
    ]
    return {"ok": True, "count": len(entries), "leaderboard": leaderboard, "entries": entries}


def _modules_payload(db: SimingDB) -> dict[str, Any]:
    db.init_db()
    mods_raw = list_modules_status(db)
    mods: list[dict[str, Any]] = []
    for m in mods_raw:
        w = _module_wave(str(m.get("module_id") or ""), str(m.get("title") or ""))
        item = dict(m)
        item["wave"] = w
        item["wave_label"] = _WAVE_LABELS.get(w, f"Wave {w}")
        mods.append(item)

    conn = db.connect()
    try:
        usage_row = conn.execute(
            "SELECT COUNT(*) AS calls, COALESCE(SUM(tokens_in),0) AS tin, COALESCE(SUM(tokens_out),0) AS tout, COALESCE(SUM(cost_cny),0.0) AS cost FROM usage"
        ).fetchone()
        sources = [
            dict(r)
            for r in conn.execute(
                "SELECT id, kind, auth_state, freshness, last_ok_at, detail FROM sources ORDER BY id"
            ).fetchall()
        ]
    finally:
        conn.close()
    return {
        "ok": True,
        "enabled_count": sum(1 for m in mods if m["enabled"]),
        "total_count": len(mods),
        "modules": mods,
        "sunday_audit": {
            "llm_calls": int(usage_row["calls"] or 0),
            "tokens_in": int(usage_row["tin"] or 0),
            "tokens_out": int(usage_row["tout"] or 0),
            "cost_cny": round(float(usage_row["cost"] or 0.0), 4),
            "sources": sources,
        },
    }


def _learned_payload(db: SimingDB) -> dict[str, Any]:
    """汇总司命学到的全部知识：Wiki / Hypotheses / M-SIM / Curiosity / Bookmarks / Routines。"""
    db.init_db()
    conn = db.connect()
    try:
        wiki_pages = []
        for r in conn.execute("SELECT * FROM wiki_pages ORDER BY updated_at DESC LIMIT 50").fetchall():
            md = str(r["content_md"] or "")
            # 清理多行换行与冗余元信息头，避免卡片预览出现字面量 \n 或无意义头部截断
            clean_md = md.replace("\\n", "\n")
            preview_lines = [
                ln.strip()
                for ln in clean_md.splitlines()
                if ln.strip()
                and not ln.strip().startswith("# ")
                and not ln.strip().startswith("- Last distilled:")
                and not ln.strip().startswith("- Entries:")
            ]
            preview_txt = " ".join(preview_lines)[:320] if preview_lines else re.sub(r"\s+", " ", clean_md).strip()[:320]
            wiki_pages.append(
                {
                    "slug": str(r["slug"]),
                    "title": str(r["title"]),
                    "category": str(r["category"]),
                    "preview": preview_txt,
                    "content_md": md,
                    "sources": _safe_json(r["sources_json"], []),
                    "conflicts": _safe_json(r["conflicts_json"], []),
                    "updated_at": str(r["updated_at"]),
                }
            )
        hypotheses = []
        for r in conn.execute("SELECT * FROM hypotheses ORDER BY created_at DESC LIMIT 50").fetchall():
            hypotheses.append(
                {
                    "id": str(r["id"]),
                    "claim": str(r["claim"]),
                    "evidence_ids": _safe_json(r["evidence_ids"], []),
                    "status": str(r["status"]),
                    "expires_at": str(r["expires_at"]),
                    "created_at": str(r["created_at"]),
                }
            )
        msim_entries = []
        for r in conn.execute("SELECT * FROM msim_entries ORDER BY created_at DESC LIMIT 50").fetchall():
            d = dict(r)
            d["tags"] = _safe_json(d.get("tags_json"), [])
            d["delta"] = round(float(d.get("measured_score") or 0.0) - float(d.get("baseline_score") or 0.0), 4)
            msim_entries.append(d)
        curiosity_questions = []
        for r in conn.execute(
            "SELECT * FROM curiosity_questions ORDER BY COALESCE(answered_at, created_at) DESC LIMIT 50"
        ).fetchall():
            curiosity_questions.append(
                {
                    "qid": str(r["qid"]),
                    "topic": str(r["topic"]),
                    "question": str(r["question"]),
                    "status": str(r["status"]),
                    "answer": str(r["answer"] or ""),
                    "context": _safe_json(r["context_json"], {}),
                    "created_at": str(r["created_at"]),
                    "answered_at": str(r["answered_at"] or ""),
                }
            )
        bookmarks = []
        for r in conn.execute("SELECT * FROM bookmarks ORDER BY created_at DESC LIMIT 30").fetchall():
            bookmarks.append(
                {
                    "bookmark_id": str(r["bookmark_id"]),
                    "kind": str(r["kind"]),
                    "title": str(r["title"]),
                    "url": str(r["url"] or ""),
                    "repo_slug": str(r["repo_slug"] or ""),
                    "summary": str(r["summary"] or ""),
                    "related_projects": _safe_json(r["related_projects_json"], []),
                    "tags": _safe_json(r["tags_json"], []),
                    "created_at": str(r["created_at"]),
                }
            )
        wiki_total = int(conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0])
        hyp_total = int(conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0])
        msim_total = int(conn.execute("SELECT COUNT(*) FROM msim_entries").fetchone()[0])
        cur_total = int(conn.execute("SELECT COUNT(*) FROM curiosity_questions").fetchone()[0])
        cur_ans = int(conn.execute("SELECT COUNT(*) FROM curiosity_questions WHERE status='ANSWERED'").fetchone()[0])
        bm_total = int(conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0])
    finally:
        conn.close()

    rp = routines_md_path(db)
    routines_text = rp.read_text(encoding="utf-8") if rp.exists() else ""
    routines_lines = [ln for ln in routines_text.splitlines() if ln.strip() and not ln.strip().startswith("#")]

    counts = {
        "wiki_pages": wiki_total,
        "hypotheses": hyp_total,
        "msim_entries": msim_total,
        "curiosity_questions": cur_total,
        "curiosity_answered": cur_ans,
        "bookmarks": bm_total,
        "routines_lines": len(routines_lines),
        "total_learned": wiki_total + hyp_total + msim_total + cur_ans + bm_total,
    }
    return {
        "ok": True,
        "generated_at": iso_now(),
        "counts": counts,
        "wiki_pages": wiki_pages,
        "hypotheses": hypotheses,
        "msim_entries": msim_entries,
        "curiosity_questions": curiosity_questions,
        "bookmarks": bookmarks,
        "routines": {
            "path": str(rp),
            "exists": rp.exists(),
            "preview": routines_text[:2000],
            "line_count": len(routines_text.splitlines()),
            "rule_count": len(routines_lines),
        },
    }


def _admin_items_payload(db: SimingDB) -> dict[str, Any]:
    """返回管理台活跃运维项：开启中的 Cases、挂起 Waits、待审批 Approvals（仅允许安全作废）。"""
    db.init_db()
    conn = db.connect()
    try:
        placeholders = ",".join("?" for _ in _CLOSED_CASE_STATUSES)
        cases = [
            dict(r)
            for r in conn.execute(
                f"""
                SELECT case_id, title, next_step, due_at, status, strength, category, rule_id, created_at, updated_at
                FROM cases
                WHERE status NOT IN ({placeholders})
                ORDER BY CASE WHEN due_at IS NULL OR due_at = '' THEN 1 ELSE 0 END, due_at ASC, updated_at DESC
                LIMIT 100
                """,
                _CLOSED_CASE_STATUSES,
            ).fetchall()
        ]
        waits = []
        for r in conn.execute(
            """
            SELECT wait_id, status, strength, trigger_kind, trigger_expr, next_fire_at,
                   expires_at, action_mode, quote, case_id, mandate_json, created_at
            FROM waits
            WHERE status = 'WAITING'
            ORDER BY COALESCE(next_fire_at, expires_at) ASC
            LIMIT 100
            """
        ).fetchall():
            d = dict(r)
            mandate = _safe_json(d.pop("mandate_json", "{}"), {})
            d["task"] = str(mandate.get("task") or d.get("quote") or d.get("trigger_expr") or "")
            waits.append(d)

        approvals = []
        for r in conn.execute(
            """
            SELECT token, bundle_id, tier, action, target_id, params_json, status, expires_at, created_at
            FROM approvals
            WHERE status = 'PENDING'
            ORDER BY created_at DESC
            LIMIT 50
            """
        ).fetchall():
            d = dict(r)
            d["params"] = _safe_json(d.pop("params_json", "{}"), {})
            approvals.append(d)
    finally:
        conn.close()

    return {
        "ok": True,
        "generated_at": iso_now(),
        "counts": {
            "cases": len(cases),
            "waits": len(waits),
            "approvals": len(approvals),
        },
        "cases": cases,
        "waits": waits,
        "approvals": approvals,
    }


def _extract_quick_keywords(text: str) -> set[str]:
    low = (text or "").lower()
    toks = set(re.findall(r"[a-z0-9_-]{3,24}", low))
    cjk = re.findall(r"[\u4e00-\u9fff]{2,6}", text or "")
    for w in cjk:
        if w not in ("已经", "目前", "需要", "可以", "我们", "没有", "一个", "这个", "那个"):
            toks.add(w)
    months = re.findall(r"20\d{2}-\d{2}", low)
    toks.update(months)
    return toks


def _graph_visual_payload(db: SimingDB, limit: int = 120, app_filter: str = "") -> dict[str, Any]:
    """为前端交互式 SVG 图谱返回结构化节点与边（最多 120 个高连接/近期节点；若未建索引则合成真实库内节点兜底）。"""
    db.init_db()
    limit = max(10, min(int(limit or 120), 160))
    app_filter_norm = (app_filter or "").strip().lower()
    stats = get_graph_stats(db)

    conn = db.connect()
    try:
        indexed_cnt = int(conn.execute("SELECT COUNT(*) FROM context_graph_nodes").fetchone()[0])
        is_synthetic = indexed_cnt == 0
        nodes: list[dict[str, Any]] = []
        edges: list[dict[str, Any]] = []

        if not is_synthetic:
            deg_map: dict[str, int] = defaultdict(int)
            for er in conn.execute("SELECT src_id, dst_id FROM context_graph_edges").fetchall():
                deg_map[str(er["src_id"])] += 1
                deg_map[str(er["dst_id"])] += 1

            where = "WHERE app = ?" if app_filter_norm else ""
            params: tuple[Any, ...] = (app_filter_norm,) if where else ()
            raw_nodes = [
                dict(r)
                for r in conn.execute(
                    f"SELECT node_id, app, entity_type, title, body, provenance, occurred_at, tags_json, updated_at FROM context_graph_nodes {where}",
                    params,
                ).fetchall()
            ]
            # 优先高连接度 + 近期时间，并尽量均衡保留各 app 代表节点
            raw_nodes.sort(
                key=lambda n: (
                    deg_map.get(str(n["node_id"]), 0),
                    str(n.get("occurred_at") or ""),
                    str(n.get("updated_at") or ""),
                ),
                reverse=True,
            )
            if not app_filter_norm:
                by_app_buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for n in raw_nodes:
                    by_app_buckets[str(n["app"])].append(n)
                picked: list[dict[str, Any]] = []
                picked_ids: set[str] = set()
                # 公平轮询分配（Round-Robin）：小域（wiki / msim / learned / calendar）优先获得完整代表性，
                # 彻底杜绝 1,500 条 sure 或 600 条 chat 刷屏淹没其他域，且在任意 limit 下均不按字母序饿死 wiki！
                app_names = sorted(by_app_buckets.keys(), key=lambda a: (len(by_app_buckets[a]), a))
                cursors: dict[str, int] = {a: 0 for a in app_names}
                while len(picked) < limit:
                    progressed = False
                    for app_name in app_names:
                        if len(picked) >= limit:
                            break
                        bucket = by_app_buckets[app_name]
                        idx_c = cursors[app_name]
                        while idx_c < len(bucket) and str(bucket[idx_c]["node_id"]) in picked_ids:
                            idx_c += 1
                        if idx_c < len(bucket):
                            cand = bucket[idx_c]
                            picked.append(cand)
                            picked_ids.add(str(cand["node_id"]))
                            cursors[app_name] = idx_c + 1
                            progressed = True
                    if not progressed:
                        break
                selected_raw = picked
            else:
                selected_raw = raw_nodes[:limit]

            for n in selected_raw:
                nid = str(n["node_id"])
                nodes.append(
                    {
                        "node_id": nid,
                        "app": str(n["app"]),
                        "entity_type": str(n["entity_type"]),
                        "title": str(n["title"])[:140],
                        "body": str(n["body"] or "")[:700],
                        "provenance": str(n["provenance"] or ""),
                        "occurred_at": str(n["occurred_at"] or ""),
                        "tags": _safe_json(n.get("tags_json"), []),
                        "degree": deg_map.get(nid, 0),
                    }
                )

            id_set = {n["node_id"] for n in nodes}
            if id_set:
                id_list = sorted(id_set)
                ph = ",".join("?" for _ in id_list)
                # 直接在 SQL 层按返回节点集合精确过滤边，彻底根治海量 sure/chat 边顶掉 wiki/learned 边以及零悬空边！
                vis_edges = conn.execute(
                    f"""
                    SELECT src_id, dst_id, relation, weight
                    FROM context_graph_edges
                    WHERE src_id IN ({ph}) AND dst_id IN ({ph})
                    ORDER BY weight DESC
                    LIMIT 320
                    """,
                    (*id_list, *id_list),
                ).fetchall()
                for er in vis_edges:
                    s_id, d_id = str(er["src_id"]), str(er["dst_id"])
                    if s_id in id_set and d_id in id_set:
                        edges.append(
                            {
                                "src_id": s_id,
                                "dst_id": d_id,
                                "relation": str(er["relation"]),
                                "weight": round(float(er["weight"] or 1.0), 2),
                            }
                        )

            # 仅当图谱本身尚未包含 learned 域节点时，叠加已学知识节点（避免与 context_graph.py 的 learned 节点重复）
            has_learned_nodes = any(n["app"] == "learned" for n in nodes)
            if not has_learned_nodes and (not app_filter_norm or app_filter_norm == "learned") and len(nodes) < limit:
                budget = limit - len(nodes)
                overlay: list[dict[str, Any]] = []
                for r in conn.execute("SELECT * FROM hypotheses ORDER BY created_at DESC LIMIT 12").fetchall():
                    nid = f"learned:hyp:{r['id']}"
                    if nid not in id_set:
                        overlay.append(
                            {
                                "node_id": nid,
                                "app": "learned",
                                "entity_type": "hypothesis",
                                "title": f"假设: {str(r['claim'])[:60]}",
                                "body": f"状态={r['status']} | 论断={r['claim']} | 过期={r['expires_at']}",
                                "provenance": f"hypotheses:{r['id']}",
                                "occurred_at": str(r["created_at"] or ""),
                                "tags": ["hypothesis", str(r["status"])],
                                "degree": 0,
                            }
                        )
                for r in conn.execute(
                    "SELECT * FROM curiosity_questions WHERE status='ANSWERED' ORDER BY answered_at DESC LIMIT 10"
                ).fetchall():
                    nid = f"learned:cur:{r['qid']}"
                    if nid not in id_set:
                        overlay.append(
                            {
                                "node_id": nid,
                                "app": "learned",
                                "entity_type": "curiosity",
                                "title": f"求证已答: {str(r['question'])[:55]}",
                                "body": f"主题={r['topic']}\n问：{r['question']}\n答：{r['answer']}",
                                "provenance": f"curiosity_questions:{r['qid']}",
                                "occurred_at": str(r["answered_at"] or r["created_at"] or ""),
                                "tags": ["curiosity", str(r["topic"])],
                                "degree": 0,
                            }
                        )
                for r in conn.execute("SELECT * FROM bookmarks ORDER BY created_at DESC LIMIT 10").fetchall():
                    nid = f"learned:bm:{r['bookmark_id']}"
                    if nid not in id_set:
                        overlay.append(
                            {
                                "node_id": nid,
                                "app": "learned",
                                "entity_type": "bookmark",
                                "title": f"收藏: {str(r['title'])[:60]}",
                                "body": f"{r['summary']}\nURL: {r['url']}\nRepo: {r['repo_slug']}".strip(),
                                "provenance": f"bookmarks:{r['bookmark_id']}",
                                "occurred_at": str(r["created_at"] or ""),
                                "tags": _safe_json(r["tags_json"], [])[:6] + [str(r["kind"])],
                                "degree": 0,
                            }
                        )
                overlay = overlay[:budget]
                if overlay:
                    # 将 overlay 节点与现有节点基于关键词弱连接
                    existing_kw = {
                        n["node_id"]: _extract_quick_keywords(f"{n['title']} {' '.join(map(str, n['tags']))}")
                        for n in nodes
                    }
                    for ov in overlay:
                        ov_kw = _extract_quick_keywords(f"{ov['title']} {ov['body']} {' '.join(map(str, ov['tags']))}")
                        nodes.append(ov)
                        id_set.add(ov["node_id"])
                        links_added = 0
                        for target_id, t_kw in existing_kw.items():
                            shared = ov_kw & t_kw
                            if shared:
                                tok = sorted(shared)[0]
                                edges.append(
                                    {
                                        "src_id": ov["node_id"],
                                        "dst_id": target_id,
                                        "relation": f"learned:{tok}",
                                        "weight": round(min(2.5, 0.8 + 0.4 * len(shared)), 2),
                                    }
                                )
                                links_added += 1
                                if links_added >= 3:
                                    break
        else:
            # 图谱尚未建索引：从真实业务表构建可视化兜底节点与关系边
            syn_nodes: list[dict[str, Any]] = []
            for r in conn.execute("SELECT * FROM wiki_pages ORDER BY updated_at DESC LIMIT 20").fetchall():
                syn_nodes.append(
                    {
                        "node_id": f"syn:wiki:{r['slug']}",
                        "app": "wiki",
                        "entity_type": "wiki_page",
                        "title": str(r["title"] or r["slug"])[:120],
                        "body": str(r["content_md"] or "")[:600],
                        "provenance": f"wiki_pages:{r['slug']}",
                        "occurred_at": str(r["updated_at"] or ""),
                        "tags": [str(r["category"] or "wiki")],
                        "degree": 0,
                    }
                )
            for r in conn.execute("SELECT * FROM hypotheses ORDER BY created_at DESC LIMIT 15").fetchall():
                syn_nodes.append(
                    {
                        "node_id": f"syn:hyp:{r['id']}",
                        "app": "learned",
                        "entity_type": "hypothesis",
                        "title": f"假设: {str(r['claim'])[:65]}",
                        "body": f"状态={r['status']} | {r['claim']}",
                        "provenance": f"hypotheses:{r['id']}",
                        "occurred_at": str(r["created_at"] or ""),
                        "tags": ["hypothesis", str(r["status"])],
                        "degree": 0,
                    }
                )
            for r in conn.execute("SELECT * FROM msim_entries ORDER BY created_at DESC LIMIT 15").fetchall():
                syn_nodes.append(
                    {
                        "node_id": f"syn:msim:{r['entry_id']}",
                        "app": "msim",
                        "entity_type": "msim_entry",
                        "title": f"[{r['source_model']}] {r['problem_family']}",
                        "body": f"{r['change_rationale']} (score {r['baseline_score']} -> {r['measured_score']})",
                        "provenance": f"msim_entries:{r['entry_id']}",
                        "occurred_at": str(r["created_at"] or ""),
                        "tags": _safe_json(r["tags_json"], []) + [str(r["source_model"])],
                        "degree": 0,
                    }
                )
            for r in conn.execute("SELECT * FROM cases ORDER BY updated_at DESC LIMIT 20").fetchall():
                syn_nodes.append(
                    {
                        "node_id": f"syn:case:{r['case_id']}",
                        "app": "chat",
                        "entity_type": "case",
                        "title": f"[{r['case_id']}] {r['title']}"[:120],
                        "body": f"状态={r['status']} | 分类={r['category']} | 下一步={r['next_step']}",
                        "provenance": f"cases:{r['case_id']}",
                        "occurred_at": str(r["due_at"] or r["updated_at"] or ""),
                        "tags": [t for t in (r["category"], r["status"]) if t],
                        "degree": 0,
                    }
                )
            for r in conn.execute("SELECT * FROM trips ORDER BY start_date DESC LIMIT 12").fetchall():
                syn_nodes.append(
                    {
                        "node_id": f"syn:trip:{r['trip_id']}",
                        "app": "calendar",
                        "entity_type": "trip",
                        "title": f"行程: {r['destination']} ({r['start_date']})",
                        "body": f"{r['destination']} ({r['country']}) {r['start_date']}~{r['end_date']} [{r['status']}] {r['sure_tag']}",
                        "provenance": f"trips:{r['trip_id']}",
                        "occurred_at": str(r["start_date"] or ""),
                        "tags": [t for t in (r["destination"], r["country"], r["sure_tag"]) if t],
                        "degree": 0,
                    }
                )
            for r in conn.execute("SELECT * FROM bookmarks ORDER BY created_at DESC LIMIT 12").fetchall():
                syn_nodes.append(
                    {
                        "node_id": f"syn:bm:{r['bookmark_id']}",
                        "app": "learned",
                        "entity_type": "bookmark",
                        "title": f"收藏: {str(r['title'])[:65]}",
                        "body": f"{r['summary']} {r['url']}".strip(),
                        "provenance": f"bookmarks:{r['bookmark_id']}",
                        "occurred_at": str(r["created_at"] or ""),
                        "tags": _safe_json(r["tags_json"], []) + [str(r["kind"])],
                        "degree": 0,
                    }
                )
            for r in conn.execute("SELECT * FROM curiosity_questions ORDER BY created_at DESC LIMIT 10").fetchall():
                syn_nodes.append(
                    {
                        "node_id": f"syn:cur:{r['qid']}",
                        "app": "learned",
                        "entity_type": "curiosity",
                        "title": f"求证: {str(r['question'])[:60]}",
                        "body": f"状态={r['status']} | 问：{r['question']} | 答：{r['answer']}",
                        "provenance": f"curiosity_questions:{r['qid']}",
                        "occurred_at": str(r["answered_at"] or r["created_at"] or ""),
                        "tags": ["curiosity", str(r["topic"])],
                        "degree": 0,
                    }
                )
            for r in conn.execute(
                "SELECT * FROM ledger_txns WHERE COALESCE(UPPER(status), '') NOT IN ('VOID_DUPLICATE', 'VOID', 'CANCELLED') ORDER BY occurred_at DESC LIMIT 10"
            ).fetchall():
                yuan = float(r["amount_cents"] or 0) / 100.0
                ccy = str(r["currency"] or "CNY").upper()
                amt_lbl = f"¥{yuan:.2f}" if ccy == "CNY" else f"{ccy} {yuan:.2f}"
                syn_nodes.append(
                    {
                        "node_id": f"syn:sure:{r['txn_id']}",
                        "app": "sure",
                        "entity_type": "txn",
                        "title": f"{r['merchant'] or r['account_alias']} {amt_lbl}",
                        "body": f"{r['occurred_at']} | {r['account_alias']} {r['card_last4']} | {r['notes']}",
                        "provenance": f"ledger_txns:{r['txn_id']}",
                        "occurred_at": str(r["occurred_at"] or ""),
                        "tags": [t for t in (r["account_alias"], r["card_last4"]) if t],
                        "degree": 0,
                    }
                )

            if app_filter_norm:
                syn_nodes = [n for n in syn_nodes if n["app"] == app_filter_norm]
            nodes = syn_nodes[:limit]

            # 为兜底节点生成共享关键词跨应用边
            kw_map = {n["node_id"]: _extract_quick_keywords(f"{n['title']} {n['body']} {' '.join(map(str, n['tags']))}") for n in nodes}
            seen_pairs: set[tuple[str, str]] = set()
            for i in range(len(nodes)):
                for j in range(i + 1, len(nodes)):
                    a, b = nodes[i], nodes[j]
                    shared = kw_map[a["node_id"]] & kw_map[b["node_id"]]
                    if shared and (a["app"] != b["app"] or len(shared) >= 2):
                        key = (a["node_id"], b["node_id"])
                        if key not in seen_pairs:
                            seen_pairs.add(key)
                            tok = sorted(shared)[0]
                            edges.append(
                                {
                                    "src_id": a["node_id"],
                                    "dst_id": b["node_id"],
                                    "relation": f"shared:{tok}",
                                    "weight": round(min(3.0, 0.8 + 0.5 * len(shared)), 2),
                                }
                            )
                            if len(edges) >= 180:
                                break
                if len(edges) >= 180:
                    break
    finally:
        conn.close()

    # 重算返回子图内节点度数
    vis_deg: dict[str, int] = defaultdict(int)
    for e in edges:
        vis_deg[e["src_id"]] += 1
        vis_deg[e["dst_id"]] += 1
    by_app_vis: dict[str, int] = defaultdict(int)
    for n in nodes:
        n["degree"] = max(int(n.get("degree") or 0), vis_deg.get(n["node_id"], 0))
        by_app_vis[n["app"]] += 1

    return {
        "ok": True,
        "is_synthetic": is_synthetic,
        "stats": {
            **stats,
            "visual_nodes": len(nodes),
            "visual_edges": len(edges),
            "visual_by_app": dict(by_app_vis),
        },
        "nodes": nodes,
        "edges": edges,
    }


def build_tower_snapshot(db: SimingDB) -> dict[str, Any]:
    """汇总 Waves 0–4 全景快照（严格不含已暂缓的 W4-2/W4-4）。"""
    db.init_db()
    conn = db.connect()
    try:
        placeholders = ",".join("?" for _ in _CLOSED_CASE_STATUSES)
        open_cases = int(
            conn.execute(f"SELECT COUNT(*) FROM cases WHERE status NOT IN ({placeholders})", _CLOSED_CASE_STATUSES).fetchone()[0]
        )
        active_waits = int(conn.execute("SELECT COUNT(*) FROM waits WHERE status = 'WAITING'").fetchone()[0])
        pending_approvals = int(conn.execute("SELECT COUNT(*) FROM approvals WHERE status = 'PENDING'").fetchone()[0])
        trips = [
            dict(r)
            for r in conn.execute(
                "SELECT trip_id, destination, country, start_date, end_date, kind, status, sure_tag, cw01_draft_path FROM trips ORDER BY start_date DESC LIMIT 10"
            ).fetchall()
        ]
        open_commitments = int(conn.execute("SELECT COUNT(*) FROM social_commitments WHERE status = 'OPEN'").fetchone()[0])
        friend_count = int(conn.execute("SELECT COUNT(*) FROM friend_profiles").fetchone()[0])
        wiki_count = int(conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0])
        msim_count = int(conn.execute("SELECT COUNT(*) FROM msim_entries").fetchone()[0])
        hyp_count = int(conn.execute("SELECT COUNT(*) FROM hypotheses").fetchone()[0])
        bm_count = int(conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0])
        cur_ans = int(conn.execute("SELECT COUNT(*) FROM curiosity_questions WHERE status = 'ANSWERED'").fetchone()[0])
        compute_count = int(conn.execute("SELECT COUNT(*) FROM compute_assets").fetchone()[0])
        compute_circuit_open = int(
            conn.execute("SELECT COUNT(*) FROM compute_assets WHERE UPPER(status) = 'CIRCUIT_OPEN'").fetchone()[0]
        )
        active_handoffs = int(
            conn.execute("SELECT COUNT(*) FROM overnight_handoffs WHERE status IN ('WATCHING', 'RESUMED')").fetchone()[0]
        )
        sub_rows = [dict(r) for r in conn.execute("SELECT * FROM subscription_audit").fetchall()]
        homelab_ok = int(conn.execute("SELECT COUNT(*) FROM homelab_nodes WHERE status = 'OK'").fetchone()[0])
        homelab_total = int(conn.execute("SELECT COUNT(*) FROM homelab_nodes").fetchone()[0])
    finally:
        conn.close()

    from siming.sub_hunter import enrich_subscription_row

    cur_dt_ov = _db.now_local()
    enriched_subs = [enrich_subscription_row(r, now=cur_dt_ov) for r in sub_rows]
    graph_stats = get_graph_stats(db)
    crowdfund_alerts = check_upcoming_crowdfund_balances(db, warn_days=14)
    mods = list_modules_status(db)
    sub_summary = _compute_sub_summary(enriched_subs)
    replenish_summary = (build_replenish_tower_payload(db, now=cur_dt_ov).get("summary") or {})

    return {
        "ok": True,
        "generated_at": iso_now(),
        "version": "siming-v5",
        "wave0_core": {
            "open_cases": open_cases,
            "active_waits": active_waits,
            "pending_approvals": pending_approvals,
        },
        "wave1_finance": {
            "compute_assets": compute_count,
            "compute_circuit_open": compute_circuit_open,
            "subscriptions": len(sub_rows),
            "active_subscriptions": sub_summary["active_count"],
            "ongoing_subscriptions": sub_summary["ongoing_count"],
            "stopped_subscriptions": sub_summary["stopped_count"],
            "monthly_sub_cost_cny": sub_summary["monthly_cost_cny"],
            "annual_sub_cost_cny": sub_summary["annual_cost_cny"],
        },
        "compute_monitor": {
            "db_assets": compute_count,
            "circuit_open": compute_circuit_open,
            "active_handoffs": active_handoffs,
        },
        "wave2_smarthome": {
            "homelab_ok": homelab_ok,
            "homelab_total": homelab_total,
        },
        "wave3_knowledge": {
            "wiki_pages": wiki_count,
            "msim_entries": msim_count,
            "hypotheses": hyp_count,
            "bookmarks": bm_count,
            "curiosity_answered": cur_ans,
            "learned_total": wiki_count + msim_count + hyp_count + bm_count + cur_ans,
        },
        "wave4_personal_os": {
            "context_graph": graph_stats,
            "trips": trips,
            "crowdfund_alerts": crowdfund_alerts,
            "open_commitments": open_commitments,
            "encrypted_friend_profiles": friend_count,
        },
        "modules": {
            "enabled": sum(1 for m in mods if m["enabled"]),
            "total": len(mods),
        },
        "replenish": replenish_summary,
    }


def _backup_file_to_tmp(db: SimingDB, name: str, old_content: str) -> str:
    bak_dir = db.paths.tmp_root / "tower-backups"
    bak_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(bak_dir, 0o700)
    stamp = _db.now_local().strftime("%Y%m%d-%H%M%S")
    bak_path = bak_dir / f"{name}.{stamp}.bak"
    if bak_path.exists():
        bak_path = bak_dir / f"{name}.{stamp}-{secrets.token_hex(3)}.bak"
    bak_path.write_text(old_content, encoding="utf-8")
    os.chmod(bak_path, 0o600)
    return str(bak_path)


def render_tower_html() -> str:
    """渲染零外部 CDN 依赖的司命统一网页控制塔与管理控制台单页应用（深浅色主题 + 交互式图谱 + 功能开关）。"""
    static_html = Path(__file__).resolve().parent / "tower_static" / "index.html"
    return static_html.read_text(encoding="utf-8")


def create_tower_server(
    db: SimingDB,
    host: str = "127.0.0.1",
    port: int = 8788,
    token: str = "",
) -> ThreadingHTTPServer:
    """创建带强制认证的轻量级 ThreadingHTTPServer 控制塔实例（支持 port=0 测试绑定）。"""
    db.init_db()
    expected_token = _resolve_tower_token(db, explicit_token=token)

    class TowerHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
            return

        def _send_json(self, payload: dict[str, Any], status: int = HTTPStatus.OK) -> None:
            raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.end_headers()
            self.wfile.write(raw)

        def _send_html(self, html: str, status: int = HTTPStatus.OK) -> None:
            raw = html.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.end_headers()
            self.wfile.write(raw)

        def _require_auth(self) -> bool:
            if _verify_auth_header(self.headers, expected_token):
                return True
            raw = json.dumps({"ok": False, "error": "Unauthorized"}, ensure_ascii=False).encode("utf-8")
            self.send_response(HTTPStatus.UNAUTHORIZED)
            self.send_header("WWW-Authenticate", 'Basic realm="Siming Control Tower", Bearer realm="Siming Control Tower"')
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
            return False

        def _normalize_path(self, path: str) -> str:
            # 兼容 Caddy 反代前缀 /siming/tower/... 与 /siming/...
            for prefix in ("/siming/tower", "/siming"):
                if path == prefix or path == prefix + "/":
                    return "/"
                if path.startswith(prefix + "/"):
                    return path[len(prefix) :]
            return path

        def _read_body_bytes(self, max_bytes: int = 1_048_576) -> bytes:
            try:
                length = int(self.headers.get("Content-Length") or "0")
            except ValueError:
                length = 0
            length = max(0, min(length, max_bytes))
            return self.rfile.read(length) if length > 0 else b""

        def _parse_json_body(self, raw_body: bytes) -> Optional[dict[str, Any]]:
            if not raw_body:
                return {}
            try:
                parsed = json.loads(raw_body.decode("utf-8"))
            except ValueError:
                return None
            return parsed if isinstance(parsed, dict) else None

        def _check_csrf(self, require_json: bool = True) -> bool:
            """CSRF 防护：要求 application/json 且拦截跨站 Origin（兼容同源 Caddy 反代与本机/WG 内网）。"""
            if require_json:
                ctype = (self.headers.get("Content-Type") or "").lower()
                if "application/json" not in ctype:
                    self._send_json(
                        {"ok": False, "error": "Content-Type must be application/json"},
                        status=HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                    )
                    return False
            origin = (self.headers.get("Origin") or "").strip()
            if origin:
                try:
                    o_host = (urllib.parse.urlsplit(origin).hostname or "").lower()
                except Exception:
                    o_host = ""
                req_host_hdr = (self.headers.get("X-Forwarded-Host") or self.headers.get("Host") or "").strip()
                req_host = (req_host_hdr.split(",", 1)[0].split(":", 1)[0] or "").strip().lower()
                allowed_hosts = {"127.0.0.1", "localhost", "::1"}
                try:
                    pol_hosts = (load_policy(db.paths).get("tower") or {}).get("allowed_hosts") or []
                    for h in pol_hosts:
                        hs = str(h or "").strip().lower()
                        if hs:
                            allowed_hosts.add(hs)
                except Exception:
                    pass
                if req_host:
                    allowed_hosts.add(req_host)
                if not o_host or o_host not in allowed_hosts:
                    self._send_json(
                        {"ok": False, "error": f"Cross-origin request blocked: {origin}"},
                        status=HTTPStatus.FORBIDDEN,
                    )
                    return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._require_auth():
                return
            parsed = urllib.parse.urlsplit(self.path)
            route = self._normalize_path(parsed.path)
            qs = urllib.parse.parse_qs(parsed.query)

            if route in ("/", "/index.html"):
                self._send_html(render_tower_html())
                return
            if route == "/v5-guide.html":
                g_path = db.paths.repo_root / "docs" / "SETUP_GUIDE.md"
                if g_path.is_file():
                    body = g_path.read_text(encoding="utf-8")
                    self._send_html(
                        "<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'><title>Siming 配置教程</title>"
                        "<style>body{max-width:920px;margin:24px auto;padding:0 18px;font:14px/1.65 system-ui,sans-serif;background:#0b1020;color:#e5e7eb}"
                        "pre{white-space:pre-wrap;background:#111827;padding:18px;border-radius:10px;border:1px solid #334155}</style></head>"
                        f"<body><p><a href='./' style='color:#60a5fa'>← 返回控制塔</a></p><pre>{html.escape(body)}</pre></body></html>"
                    )
                    return
            if route == "/api/overview":
                self._send_json(build_tower_snapshot(db))
                return
            if route == "/api/compute":
                live_flag = (qs.get("live") or ["0"])[0].strip().lower() in ("1", "true", "yes")
                self._send_json(build_compute_monitor_payload(db, live=live_flag))
                return
            if route == "/api/finance":
                self._send_json(_finance_payload(db))
                return
            if route == "/api/finance/charts":
                try:
                    m_cnt = int((qs.get("months") or ["12"])[0])
                except ValueError:
                    m_cnt = 12
                self._send_json(_finance_charts_payload(db, months=m_cnt))
                return
            if route == "/api/smarthome":
                self._send_json(_smarthome_payload(db))
                return
            if route == "/api/replenish":
                self._send_json(build_replenish_tower_payload(db))
                return
            if route == "/api/wiki/conflicts":
                self._send_json(list_wiki_conflicts(db, include_resolved=True, include_filtered=True))
                return
            if route == "/api/wiki":
                self._send_json(_wiki_payload(db))
                return
            if route == "/api/msim":
                self._send_json(_msim_payload(db))
                return
            if route == "/api/routines":
                rp = routines_md_path(db)
                content = rp.read_text(encoding="utf-8") if rp.exists() else ""
                self._send_json({"ok": True, "path": str(rp), "exists": rp.exists(), "content": content})
                return
            if route == "/api/modules":
                self._send_json(_modules_payload(db))
                return
            if route == "/api/ops":
                self._send_json(build_ops_tower_payload(db))
                return
            if route == "/api/ops/schedule-output":
                src_p = (qs.get("source") or [""])[0].strip()
                id_p = (qs.get("id") or [""])[0].strip()
                res = read_schedule_output_preview(db, src_p, id_p)
                if not res.get("ok"):
                    st = HTTPStatus.NOT_FOUND if "未找到" in str(res.get("error") or "") else HTTPStatus.BAD_REQUEST
                    self._send_json(res, status=st)
                    return
                self._send_json(res)
                return
            if route == "/api/learned":
                self._send_json(_learned_payload(db))
                return
            if route == "/api/admin/items":
                self._send_json(_admin_items_payload(db))
                return
            if route == "/api/graph/visual":
                app_f = (qs.get("app") or [""])[0].strip()
                try:
                    lim = int((qs.get("limit") or ["120"])[0])
                except ValueError:
                    lim = 120
                self._send_json(_graph_visual_payload(db, limit=lim, app_filter=app_f))
                return
            if route == "/api/graph":
                q = (qs.get("q") or [""])[0].strip()
                if not q:
                    self._send_json(get_graph_stats(db))
                    return
                apps_raw = (qs.get("apps") or [""])[0].strip()
                apps = [a.strip() for a in apps_raw.split(",") if a.strip()] if apps_raw else None
                try:
                    limit = int((qs.get("limit") or ["10"])[0])
                except ValueError:
                    limit = 10
                self._send_json(query_context_graph(db, q, apps=apps, limit=limit))
                return

            if route == "/api/v6/overview":
                from siming.tower_projections import v6_overview_projection

                self._send_json(v6_overview_projection(db))
                return
            if route == "/api/v6/activity":
                from siming.learning_loop import get_today_activity_feed

                day_p = (qs.get("day") or [""])[0].strip() or None
                self._send_json(get_today_activity_feed(db, day_prefix=day_p))
                return
            if route == "/api/v6/receipts":
                from siming.tower_projections import receipts_projection

                self._send_json(receipts_projection(db))
                return
            if route == "/api/v6/capabilities":
                from siming.tower_projections import capabilities_projection

                self._send_json(capabilities_projection(db))
                return
            if route == "/api/v6/corrections":
                from siming.tower_projections import corrections_projection

                self._send_json(corrections_projection(db))
                return
            if route == "/api/v6/caldav":
                from siming.tower_projections import caldav_projection

                self._send_json(caldav_projection(db))
                return
            if route == "/api/v6/ics":
                from siming.caldav_sync import export_siming_ics

                raw_ics = export_siming_ics(db).encode("utf-8")
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/calendar; charset=utf-8")
                self.send_header("Content-Length", str(len(raw_ics)))
                self.end_headers()
                self.wfile.write(raw_ics)
                return

            self._send_json({"ok": False, "error": f"Not Found: {route}"}, status=HTTPStatus.NOT_FOUND)

        def do_POST(self) -> None:  # noqa: N802
            if not self._require_auth():
                return
            if not self._check_csrf(require_json=True):
                return
            parsed = urllib.parse.urlsplit(self.path)
            route = self._normalize_path(parsed.path)
            raw_body = self._read_body_bytes()

            # 1. Routines editor
            if route == "/api/routines":
                ctype = (self.headers.get("Content-Type") or "").lower()
                if "application/json" in ctype:
                    data = self._parse_json_body(raw_body)
                    if data is None:
                        self._send_json({"ok": False, "error": "Invalid JSON"}, status=HTTPStatus.BAD_REQUEST)
                        return
                    content = str(data.get("content", ""))
                else:
                    content = raw_body.decode("utf-8", errors="replace")

                rp = routines_md_path(db)
                old_content = rp.read_text(encoding="utf-8") if rp.exists() else ""
                bak_path = _backup_file_to_tmp(db, "routines.md", old_content)
                rp.parent.mkdir(parents=True, exist_ok=True)
                tmp_write = rp.parent / f".{rp.name}.{secrets.token_hex(4)}.tmp"
                tmp_write.write_text(content, encoding="utf-8")
                os.replace(tmp_write, rp)
                os.chmod(rp, 0o600)
                db.write_audit(
                    tier="A2",
                    action="tower_update_routines",
                    params={"bytes": len(content.encode("utf-8")), "backup_path": bak_path},
                    target_id="routines.md",
                )
                self._send_json(
                    {
                        "ok": True,
                        "path": str(rp),
                        "backup_path": bak_path,
                        "bytes": len(content.encode("utf-8")),
                    }
                )
                return

            # 2. Wiki distill (must precede /api/wiki/<slug>)
            if route == "/api/wiki/distill":
                res = distill_hindsight_wiki(db)
                db.write_audit(
                    tier="A1",
                    action="tower_wiki_distill",
                    params={"facts_extracted": res.get("facts_extracted", 0), "pages": len(res.get("pages_written", []))},
                    target_id="hindsight_wiki",
                )
                self._send_json({"ok": True, **res})
                return

            # 2a. Wiki conflict LLM summarize & semantic filter: POST /api/wiki/conflicts/summarize
            if route == "/api/wiki/conflicts/summarize":
                if not self._check_csrf(require_json=True):
                    return
                data = self._parse_json_body(raw_body) or {}
                force = bool(data.get("force", True))
                res = summarize_wiki_conflicts(db, force=force, use_llm=True)
                db.write_audit(
                    tier="A1",
                    action="tower_wiki_conflict_summarize",
                    params={
                        "force": force,
                        "llm_updated": (res.get("stats") or {}).get("llm_updated", 0),
                        "filtered_noise": (res.get("stats") or {}).get("filtered_noise", 0),
                    },
                    target_id="hindsight_wiki",
                )
                self._send_json(res)
                return

            # 2b. Wiki conflict resolve: POST /api/wiki/conflicts/<conflict_id>/resolve
            m_cf_res = re.fullmatch(r"/api/wiki/conflicts/([^/]+)/resolve", route)
            if m_cf_res:
                if not self._check_csrf(require_json=True):
                    return
                cid = urllib.parse.unquote(m_cf_res.group(1)).strip()
                data = self._parse_json_body(raw_body)
                if data is None or not data.get("decision"):
                    self._send_json({"ok": False, "error": "Missing field 'decision'"}, status=HTTPStatus.BAD_REQUEST)
                    return
                dec = str(data.get("decision") or "").strip().lower()
                note = str(data.get("note") or "").strip()
                snooze_days = int(data.get("snooze_days") or 7)
                res = resolve_wiki_conflict(db, cid, dec, note=note, snooze_days=snooze_days)
                db.write_audit(
                    tier="A1",
                    action="tower_wiki_conflict_resolve",
                    params={"conflict_id": cid, "decision": dec, "note": note[:120]},
                    target_id=cid,
                    exit_code=0 if res.get("ok") else 1,
                )
                if not res.get("ok"):
                    st = HTTPStatus.NOT_FOUND if "未找到" in str(res.get("error") or "") else HTTPStatus.BAD_REQUEST
                    self._send_json(res, status=st)
                    return
                self._send_json(res)
                return

            # 2c. Wiki conflict reopen (undo): POST /api/wiki/conflicts/<conflict_id>/reopen
            m_cf_reopen = re.fullmatch(r"/api/wiki/conflicts/([^/]+)/reopen", route)
            if m_cf_reopen:
                if not self._check_csrf(require_json=True):
                    return
                cid = urllib.parse.unquote(m_cf_reopen.group(1)).strip()
                res = reopen_wiki_conflict(db, cid)
                db.write_audit(
                    tier="A1",
                    action="tower_wiki_conflict_reopen",
                    params={"conflict_id": cid},
                    target_id=cid,
                    exit_code=0 if res.get("ok") else 1,
                )
                if not res.get("ok"):
                    self._send_json(res, status=HTTPStatus.NOT_FOUND)
                    return
                self._send_json(res)
                return

            # 2d. Wiki conflict restore from filtered noise: POST /api/wiki/conflicts/<conflict_id>/restore
            m_cf_restore = re.fullmatch(r"/api/wiki/conflicts/([^/]+)/restore", route)
            if m_cf_restore:
                if not self._check_csrf(require_json=True):
                    return
                cid = urllib.parse.unquote(m_cf_restore.group(1)).strip()
                res = restore_filtered_conflict(db, cid)
                db.write_audit(
                    tier="A1",
                    action="tower_wiki_conflict_restore",
                    params={"conflict_id": cid},
                    target_id=cid,
                    exit_code=0 if res.get("ok") else 1,
                )
                if not res.get("ok"):
                    self._send_json(res, status=HTTPStatus.NOT_FOUND)
                    return
                self._send_json(res)
                return

            # 3. Wiki page upsert
            if route.startswith("/api/wiki/"):
                slug = urllib.parse.unquote(route[len("/api/wiki/") :]).strip()
                if not _WIKI_SLUG_RE.fullmatch(slug):
                    self._send_json({"ok": False, "error": "Invalid wiki slug"}, status=HTTPStatus.BAD_REQUEST)
                    return
                data = self._parse_json_body(raw_body)
                if data is None:
                    self._send_json({"ok": False, "error": "Invalid JSON"}, status=HTTPStatus.BAD_REQUEST)
                    return
                title = str(data.get("title") or slug).strip()
                category = str(data.get("category") or "general").strip()
                content_md = str(data.get("content_md") or "")
                ts = iso_now()

                with db.transaction() as conn:
                    old = conn.execute("SELECT content_md FROM wiki_pages WHERE slug = ?", (slug,)).fetchone()
                    bak_path = _backup_file_to_tmp(db, f"wiki-{slug}.md", str(old["content_md"]) if old else "")
                    conn.execute(
                        """
                        INSERT INTO wiki_pages (slug, title, category, content_md, sources_json, conflicts_json, updated_at, last_distilled_at)
                        VALUES (?, ?, ?, ?, '[]', '[]', ?, ?)
                        ON CONFLICT(slug) DO UPDATE SET
                            title = excluded.title,
                            category = excluded.category,
                            content_md = excluded.content_md,
                            updated_at = excluded.updated_at
                        """,
                        (slug, title, category, content_md, ts, ts),
                    )
                    db.write_audit(
                        tier="A2",
                        action="tower_update_wiki",
                        params={"slug": slug, "bytes": len(content_md.encode("utf-8")), "backup_path": bak_path},
                        target_id=slug,
                        conn=conn,
                    )
                self._send_json(
                    {
                        "ok": True,
                        "slug": slug,
                        "title": title,
                        "category": category,
                        "backup_path": bak_path,
                        "updated_at": ts,
                    }
                )
                return

            # 4. Graph reindex
            if route == "/api/graph/reindex":
                idx_res = index_context_graph(db)
                stats = get_graph_stats(db)
                db.write_audit(
                    tier="A1",
                    action="tower_graph_reindex",
                    params={"nodes_indexed": idx_res.get("nodes_indexed", 0), "edges_created": idx_res.get("edges_created", 0)},
                    target_id="context_graph",
                )
                self._send_json({"ok": True, "index_result": idx_res, "stats": stats})
                return

            # 5. Module toggle: POST /api/modules/<module_id>/toggle
            m_mod = re.fullmatch(r"/api/modules/([^/]+)/toggle", route)
            if m_mod:
                module_id = urllib.parse.unquote(m_mod.group(1)).strip()
                data = self._parse_json_body(raw_body)
                if data is None or "enabled" not in data:
                    self._send_json({"ok": False, "error": "Missing boolean field 'enabled'"}, status=HTTPStatus.BAD_REQUEST)
                    return
                raw_en = data.get("enabled")
                if isinstance(raw_en, bool):
                    enabled = raw_en
                elif isinstance(raw_en, (int, float)) and raw_en in (0, 1):
                    enabled = bool(raw_en)
                elif isinstance(raw_en, str) and raw_en.strip().lower() in ("true", "false", "1", "0"):
                    enabled = raw_en.strip().lower() in ("true", "1")
                else:
                    self._send_json({"ok": False, "error": "Invalid 'enabled' value"}, status=HTTPStatus.BAD_REQUEST)
                    return

                res = set_module_enabled(db, module_id, enabled)
                db.write_audit(
                    tier="A1",
                    action="tower_module_toggle",
                    params={"module_id": module_id, "enabled": enabled},
                    target_id=str(res.get("module_id") or module_id),
                    exit_code=0 if res.get("ok") else 1,
                )
                if not res.get("ok"):
                    self._send_json(res, status=HTTPStatus.NOT_FOUND)
                    return
                mods = list_modules_status(db)
                self._send_json(
                    {
                        **res,
                        "enabled_count": sum(1 for x in mods if x["enabled"]),
                        "total_count": len(mods),
                    }
                )
                return

            # 6. Case close: POST /api/cases/<case_id>/close
            m_case = re.fullmatch(r"/api/cases/([^/]+)/close", route)
            if m_case:
                case_id = urllib.parse.unquote(m_case.group(1)).strip()
                data = self._parse_json_body(raw_body)
                if data is None:
                    self._send_json({"ok": False, "error": "Invalid JSON"}, status=HTTPStatus.BAD_REQUEST)
                    return
                cancel = bool(data.get("cancel", False))
                note = str(data.get("note") or "tower").strip() or "tower"
                from siming.router import close_case

                res = close_case(db, case_id, cancel=cancel, note=note)
                db.write_audit(
                    tier="A1",
                    action="tower_close_case",
                    params={"case_id": case_id, "cancel": cancel, "note": note},
                    target_id=case_id.upper(),
                    exit_code=0 if res.get("ok") else 1,
                )
                if not res.get("ok"):
                    st = HTTPStatus.NOT_FOUND if "未找到" in str(res.get("error") or "") else HTTPStatus.BAD_REQUEST
                    self._send_json(res, status=st)
                    return
                self._send_json(res)
                return

            # 7. Wait cancel: POST /api/waits/<wait_id>/cancel
            m_wait = re.fullmatch(r"/api/waits/([^/]+)/cancel", route)
            if m_wait:
                wait_id = urllib.parse.unquote(m_wait.group(1)).strip()
                data = self._parse_json_body(raw_body)
                if data is None:
                    self._send_json({"ok": False, "error": "Invalid JSON"}, status=HTTPStatus.BAD_REQUEST)
                    return
                reason = str(data.get("reason") or "tower_cancel").strip() or "tower_cancel"
                from siming.waits import cancel_wait

                ok = cancel_wait(db, wait_id, reason)
                db.write_audit(
                    tier="A1",
                    action="tower_cancel_wait",
                    params={"wait_id": wait_id, "reason": reason},
                    target_id=wait_id.upper(),
                    exit_code=0 if ok else 1,
                )
                if not ok:
                    self._send_json(
                        {"ok": False, "wait_id": wait_id.upper(), "error": f"挂起项 {wait_id} 不存在或已结束"},
                        status=HTTPStatus.NOT_FOUND,
                    )
                    return
                self._send_json({"ok": True, "wait_id": wait_id.upper(), "status": "CANCELLED", "reason": reason})
                return

            # 8. Approval safe cancel ONLY (never auto-apply A2 from web!): POST /api/approvals/<token>/cancel
            m_app = re.fullmatch(r"/api/approvals/([^/]+)/cancel", route)
            if m_app:
                tok = urllib.parse.unquote(m_app.group(1)).strip()
                from siming.approval import cancel_approval

                res = cancel_approval(db, tok)
                db.write_audit(
                    tier="A1",
                    action="tower_cancel_approval",
                    params={"token": tok},
                    target_id=tok.upper(),
                    exit_code=0 if res.get("ok") else 1,
                )
                if not res.get("ok"):
                    st = HTTPStatus.NOT_FOUND if "未找到" in str(res.get("reply") or "") else HTTPStatus.BAD_REQUEST
                    self._send_json(res, status=st)
                    return
                self._send_json(res)
                return

            # 9. Subscriptions rescan (must precede /api/subs/<sub_id>/status)
            if route == "/api/subs/rescan":
                from siming import sub_hunter

                purge_fn = getattr(sub_hunter, "purge_false_positive_subscriptions", None)
                purged = purge_fn(db) if callable(purge_fn) else None
                scan_res = sub_hunter.scan_subscriptions(db)
                detected_cnt = int(
                    scan_res["detected_count"]
                    if "detected_count" in scan_res
                    else scan_res.get("detected", len(scan_res.get("subscriptions") or []))
                )
                fin_after = _finance_payload(db)
                sub_summary = fin_after.get("sub_summary") or {}
                db.write_audit(
                    tier="A1",
                    action="tower_subs_rescan",
                    params={"purged": purged, "detected_count": detected_cnt, "active_count": sub_summary.get("active_count", 0)},
                    target_id="subscription_audit",
                )
                self._send_json(
                    {
                        "ok": True,
                        "purged": purged,
                        "scan": scan_res,
                        "detected_count": detected_cnt,
                        "active_count": sub_summary.get("active_count", 0),
                        "total_count": sub_summary.get("total", 0),
                        "subscriptions": fin_after.get("subscriptions") or [],
                    }
                )
                return

            # 10. Subscription status / ongoing mark: POST /api/subs/<sub_id>/status
            m_sub = re.fullmatch(r"/api/subs/([^/]+)/status", route)
            if m_sub:
                sub_id = urllib.parse.unquote(m_sub.group(1)).strip()
                data = self._parse_json_body(raw_body)
                if data is None:
                    self._send_json({"ok": False, "error": "Invalid JSON"}, status=HTTPStatus.BAD_REQUEST)
                    return
                status_val = str(data.get("status") or "").strip().upper()
                note = str(data.get("note") or "").strip()
                ongoing_val: Optional[bool] = None
                if "is_ongoing" in data and data.get("is_ongoing") is not None:
                    raw_og = data.get("is_ongoing")
                    if isinstance(raw_og, bool):
                        ongoing_val = raw_og
                    elif isinstance(raw_og, (int, float)) and raw_og in (0, 1):
                        ongoing_val = bool(raw_og)
                    elif isinstance(raw_og, str) and raw_og.strip().lower() in ("true", "false", "1", "0"):
                        ongoing_val = raw_og.strip().lower() in ("true", "1")
                    else:
                        self._send_json({"ok": False, "error": "Invalid is_ongoing boolean"}, status=HTTPStatus.BAD_REQUEST)
                        return
                from siming.sub_hunter import mark_subscription_status

                res = mark_subscription_status(
                    db,
                    sub_id,
                    status=status_val or None,
                    note=note,
                    is_ongoing=ongoing_val,
                )
                db.write_audit(
                    tier="A1",
                    action="tower_sub_status",
                    params={"sub_id": sub_id, "status": status_val, "is_ongoing": ongoing_val, "note": note},
                    target_id=str(res.get("sub_id") or sub_id),
                    exit_code=0 if res.get("ok") else 1,
                )
                if not res.get("ok"):
                    st = HTTPStatus.NOT_FOUND if "未找到" in str(res.get("error") or "") else HTTPStatus.BAD_REQUEST
                    self._send_json(res, status=st)
                    return
                self._send_json(res)
                return

            # 11. Compute resource monitor: refresh & sync live balances
            if route == "/api/compute/refresh":
                if not self._check_csrf(require_json=True):
                    return
                res = build_compute_monitor_payload(db, live=True, sync_assets=True)
                sync_info = res.get("sync_result") or {}
                db.write_audit(
                    tier="A1",
                    action="tower_compute_refresh",
                    params={
                        "pools": (res.get("summary") or {}).get("pool_count", 0),
                        "synced": sync_info.get("synced", 0),
                        "providers": sync_info.get("providers", []),
                    },
                    target_id="compute_monitor",
                )
                self._send_json(res)
                return

            # 12. Compute resource monitor: manual asset upsert
            if route == "/api/compute/assets":
                if not self._check_csrf(require_json=True):
                    return
                data = self._parse_json_body(raw_body)
                if data is None:
                    self._send_json({"ok": False, "error": "Invalid JSON"}, status=HTTPStatus.BAD_REQUEST)
                    return
                prov = str(data.get("provider") or "").strip()
                if not prov:
                    self._send_json({"ok": False, "error": "Missing field 'provider'"}, status=HTTPStatus.BAD_REQUEST)
                    return
                try:
                    bal = float(data.get("balance"))
                    daily = float(data.get("daily_spend") or 0.0)
                    avg_daily = float(data.get("avg_daily_spend") or 0.0)
                except (TypeError, ValueError):
                    self._send_json({"ok": False, "error": "Numeric balance/spend required"}, status=HTTPStatus.BAD_REQUEST)
                    return
                res = upsert_compute_asset(
                    db,
                    provider=prov,
                    account_label=str(data.get("account_label") or "main").strip(),
                    balance=bal,
                    currency=str(data.get("currency") or "CNY").strip().upper(),
                    daily_spend=daily,
                    avg_daily_spend=avg_daily,
                    plan_name=str(data.get("plan_name") or "").strip(),
                    plan_expires_at=str(data.get("plan_expires_at") or "").strip(),
                    billing_scope=str(data.get("billing_scope") or "personal").strip(),
                    fallback_model=str(data.get("fallback_model") or "").strip(),
                )
                guard = evaluate_compute_guardrails(db, notify=False)
                db.write_audit(
                    tier="A1",
                    action="tower_compute_asset_upsert",
                    params={"asset_id": res.get("asset_id"), "provider": prov, "balance": bal, "currency": res.get("currency")},
                    target_id=str(res.get("asset_id") or prov),
                    exit_code=0 if res.get("ok") else 1,
                )
                if not res.get("ok"):
                    self._send_json(res, status=HTTPStatus.BAD_REQUEST)
                    return
                self._send_json({**res, "guardrails": guard})
                return

            # 13. Compute resource monitor: trigger guardrails evaluation
            if route == "/api/compute/guardrails":
                if not self._check_csrf(require_json=True):
                    return
                guard = evaluate_compute_guardrails(db, notify=False)
                db.write_audit(
                    tier="A1",
                    action="tower_compute_guardrails",
                    params={"checked": guard.get("checked", 0), "alerts": len(guard.get("alerts") or [])},
                    target_id="compute_assets",
                )
                self._send_json(guard)
                return

            # 14. Compute resource monitor: circuit breaker toggle POST /api/compute/circuit/<provider>
            m_cir = re.fullmatch(r"/api/compute/circuit/([^/]+)", route)
            if m_cir:
                if not self._check_csrf(require_json=True):
                    return
                prov_raw = urllib.parse.unquote(m_cir.group(1)).strip()
                data = self._parse_json_body(raw_body)
                if data is None or "open" not in data:
                    self._send_json({"ok": False, "error": "Missing boolean field 'open'"}, status=HTTPStatus.BAD_REQUEST)
                    return
                raw_open = data.get("open")
                if isinstance(raw_open, bool):
                    open_val = raw_open
                elif isinstance(raw_open, (int, float)) and raw_open in (0, 1):
                    open_val = bool(raw_open)
                elif isinstance(raw_open, str) and raw_open.strip().lower() in ("true", "false", "1", "0"):
                    open_val = raw_open.strip().lower() in ("true", "1")
                else:
                    self._send_json({"ok": False, "error": "Invalid boolean 'open'"}, status=HTTPStatus.BAD_REQUEST)
                    return
                res = set_provider_circuit(db, prov_raw, open_val)
                db.write_audit(
                    tier="A1",
                    action="tower_compute_circuit_toggle",
                    params={"provider": prov_raw, "open": open_val},
                    target_id=str(res.get("provider") or prov_raw),
                    exit_code=0 if res.get("ok") else 1,
                )
                if not res.get("ok"):
                    self._send_json(res, status=HTTPStatus.BAD_REQUEST)
                    return
                self._send_json(res)
                return

            # 15. Daily Replenish: create/update item
            if route == "/api/replenish/items":
                if not self._check_csrf(require_json=True):
                    return
                data = self._parse_json_body(raw_body)
                if data is None:
                    self._send_json({"ok": False, "error": "Invalid JSON"}, status=HTTPStatus.BAD_REQUEST)
                    return
                name_s = str(data.get("name") or "").strip()
                if not name_s:
                    self._send_json({"ok": False, "error": "Missing field 'name'"}, status=HTTPStatus.BAD_REQUEST)
                    return
                try:
                    cyc_d = int(data.get("cycle_days") or 30)
                    lead_d = int(data["lead_days"]) if data.get("lead_days") not in (None, "") else None
                    qty_n = int(data.get("qty") or 1)
                    ty_val = float(data["target_yuan"]) if data.get("target_yuan") not in (None, "") else None
                except (TypeError, ValueError):
                    self._send_json({"ok": False, "error": "Numeric cycle_days/qty/target_yuan required"}, status=HTTPStatus.BAD_REQUEST)
                    return
                res = upsert_replenish_item(
                    db,
                    name=name_s,
                    sku_or_url=str(data.get("sku_or_url") or data.get("sku") or "").strip(),
                    cycle_days=cyc_d,
                    target_yuan=ty_val,
                    qty=qty_n,
                    last_bought_date=str(data.get("last_bought_date") or data.get("last_bought") or "").strip(),
                    item_id=str(data.get("item_id") or "").strip(),
                    category=str(data.get("category") or "").strip(),
                    location=str(data.get("location") or "家").strip(),
                    spec=str(data.get("spec") or "").strip(),
                    channel=str(data.get("channel") or "meituan").strip(),
                    meituan_shop=str(data.get("meituan_shop") or "").strip(),
                    meituan_query=str(data.get("meituan_query") or "").strip(),
                    lead_days=lead_d,
                    enabled=bool(data["enabled"]) if "enabled" in data and data["enabled"] is not None else None,
                    tier=str(data.get("tier") or "").strip(),
                    notes=str(data.get("notes") or "").strip(),
                )
                db.write_audit(
                    tier="A1",
                    action="tower_replenish_upsert",
                    params={"name": name_s, "cycle_days": cyc_d, "channel": data.get("channel"), "location": data.get("location")},
                    target_id=str((res.get("entry") or {}).get("item_id") or name_s),
                    exit_code=0 if res.get("ok") else 1,
                )
                if not res.get("ok"):
                    self._send_json(res, status=HTTPStatus.BAD_REQUEST)
                    return
                self._send_json(res)
                return

            # 16. Daily Replenish: reinfer cycles from consumption history
            if route == "/api/replenish/reinfer":
                if not self._check_csrf(require_json=True):
                    return
                data = self._parse_json_body(raw_body) or {}
                res = seed_daily_replenish_catalog(db, overwrite=bool(data.get("overwrite", False)))
                db.write_audit(
                    tier="A1",
                    action="tower_replenish_reinfer",
                    params={"total": res.get("total", 0), "added": res.get("added", 0), "updated": res.get("updated", 0)},
                    target_id="replenish_items",
                )
                self._send_json(res)
                return

            # 17. Daily Replenish: evaluate due items and optionally push QQ reminders
            if route == "/api/replenish/check-due":
                if not self._check_csrf(require_json=True):
                    return
                data = self._parse_json_body(raw_body) or {}
                notify_flag = bool(data.get("notify", True))
                res = evaluate_replenish_due(db, notify=notify_flag, force_window=True)
                db.write_audit(
                    tier="A1",
                    action="tower_replenish_check_due",
                    params={"notify": notify_flag, "due": res.get("due", []), "pushed": res.get("pushed", [])},
                    target_id="replenish_items",
                )
                self._send_json(res)
                return

            # 18. Daily Replenish item actions: bought / snooze / toggle / remind_qq / delete (never direct A2 pay from web!)
            m_rpl_act = re.fullmatch(r"/api/replenish/items/([^/]+)/action", route)
            if m_rpl_act:
                if not self._check_csrf(require_json=True):
                    return
                iid = urllib.parse.unquote(m_rpl_act.group(1)).strip()
                data = self._parse_json_body(raw_body)
                if data is None or not data.get("action"):
                    self._send_json({"ok": False, "error": "Missing field 'action'"}, status=HTTPStatus.BAD_REQUEST)
                    return
                act = str(data.get("action") or "").strip().lower()
                if act == "bought":
                    res = mark_replenish_bought(db, iid, bought_date=str(data.get("date") or "").strip())
                elif act == "snooze":
                    try:
                        s_days = int(data.get("days") or 14)
                    except (TypeError, ValueError):
                        s_days = 14
                    res = snooze_replenish_item(db, iid, days=s_days)
                elif act == "toggle":
                    res = toggle_replenish_item(db, iid, enabled=bool(data.get("enabled", True)))
                elif act == "remind_qq":
                    sr = send_replenish_due_reminder(db, iid, bypass_quiet=True)
                    send_r = sr.get("send_result") or {}
                    if sr.get("ok") and send_r.get("has_keyboard"):
                        rpl_sum = f"📲 已向 QQ 发送「{sr.get('name', iid)}」交互补货确认卡，在 QQ 直接点按钮即可下单！"
                    elif sr.get("ok"):
                        kb_warn = f"（按钮降级：{send_r.get('keyboard_error')}）" if send_r.get("keyboard_error") else "（纯文本模式）"
                        rpl_sum = f"📲 已向 QQ 发送「{sr.get('name', iid)}」补货提醒{kb_warn}，可引用回复「确认」下单。"
                    else:
                        rpl_sum = sr.get("error") or "推送失败"
                    res = {
                        **sr,
                        "summary": rpl_sum,
                    }
                elif act == "delete":
                    res = remove_replenish_item(db, iid)
                else:
                    self._send_json({"ok": False, "error": f"Unsupported replenish action: {act}"}, status=HTTPStatus.BAD_REQUEST)
                    return
                db.write_audit(
                    tier="A1",
                    action=f"tower_replenish_{act}",
                    params={"item_id": iid, "action": act},
                    target_id=iid,
                    exit_code=0 if res.get("ok") else 1,
                )
                if not res.get("ok"):
                    st = HTTPStatus.NOT_FOUND if "未找到" in str(res.get("error") or "") else HTTPStatus.BAD_REQUEST
                    self._send_json(res, status=st)
                    return
                self._send_json(res)
                return

            # 19. Ops (Plan A): Push Guard restore rule
            if route == "/api/ops/push-guard/restore":
                data = self._parse_json_body(raw_body)
                if data is None or not str(data.get("rule_id") or "").strip():
                    self._send_json({"ok": False, "error": "Missing field 'rule_id'"}, status=HTTPStatus.BAD_REQUEST)
                    return
                rid = str(data.get("rule_id") or "").strip()
                res = ops_restore_push_rule(db, rid)
                db.write_audit(
                    tier="A1",
                    action="tower_ops_push_guard_restore",
                    params={"rule_id": rid},
                    target_id=rid,
                    exit_code=0 if res.get("ok") else 1,
                )
                if not res.get("ok"):
                    st = HTTPStatus.NOT_FOUND if "没有规则" in str(res.get("error") or "") else HTTPStatus.BAD_REQUEST
                    self._send_json(res, status=st)
                    return
                self._send_json(res)
                return

            # 20. Ops (Plan A): Pause / Resume Siming background worker execution
            if route == "/api/ops/pause":
                data = self._parse_json_body(raw_body)
                if data is None or not str(data.get("action") or "").strip():
                    self._send_json({"ok": False, "error": "Missing field 'action'"}, status=HTTPStatus.BAD_REQUEST)
                    return
                act = str(data.get("action") or "").strip()
                dur = str(data.get("duration") or "2h").strip()
                res = ops_set_pause(db, act, dur)
                db.write_audit(
                    tier="A1",
                    action="tower_ops_pause" if res.get("paused") else "tower_ops_resume",
                    params={"action": act, "duration": dur, "paused_until": res.get("paused_until", "")},
                    target_id="siming_worker",
                    exit_code=0 if res.get("ok") else 1,
                )
                if not res.get("ok"):
                    self._send_json(res, status=HTTPStatus.BAD_REQUEST)
                    return
                self._send_json(res)
                return

            # 21. Ops (Plan A): Run Siming doctor + link diagnose (optional whitelisted DNS cache flush ONLY)
            if route == "/api/ops/diagnose":
                data = self._parse_json_body(raw_body) or {}
                allow_heal = bool(data.get("allow_heal", False))
                res = ops_run_diagnose(db, allow_heal=allow_heal)
                db.write_audit(
                    tier="A1",
                    action="tower_ops_diagnose",
                    params={
                        "allow_heal": allow_heal,
                        "doctor_ok": res.get("doctor_ok"),
                        "links_ok": res.get("links_ok"),
                        "healed_count": len(res.get("healed_actions") or []),
                    },
                    target_id="self_heal_ops",
                )
                self._send_json(res)
                return

            self._send_json({"ok": False, "error": f"Not Found: {route}"}, status=HTTPStatus.NOT_FOUND)

    class _TowerServer(ThreadingHTTPServer):
        allow_reuse_address = True

    server = _TowerServer((host, int(port)), TowerHandler)
    setattr(server, "tower_token", expected_token)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="司命统一网页控制塔 (Siming v5 Control Tower)")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8788, help="监听端口（默认 8788）")
    args = parser.parse_args()

    db = SimingDB()
    server = create_tower_server(db, host=args.host, port=args.port)
    print(f"Siming Control Tower listening on http://{args.host}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
