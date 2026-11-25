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
    db.init_db()
    conn = db.connect()
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
    db.init_db()
    conn = db.connect()
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
    return r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>司命 Siming v5 · 统一网页控制塔</title>
<style>
  :root {
    --bg: #0b1017;
    --bg-soft: #101722;
    --card: #141d2b;
    --card-hover: #1a2536;
    --border: #243247;
    --text: #e6edf7;
    --muted: #8ea4c0;
    --accent: #58a6ff;
    --accent-soft: rgba(88, 166, 255, 0.14);
    --ok: #34d399;
    --ok-soft: rgba(52, 211, 153, 0.14);
    --warn: #f59e0b;
    --warn-soft: rgba(245, 158, 11, 0.15);
    --err: #f87171;
    --err-soft: rgba(248, 113, 113, 0.15);
    --purple: #a78bfa;
    --purple-soft: rgba(167, 139, 250, 0.15);
    --teal: #2dd4bf;
    --shadow: 0 8px 24px rgba(0, 0, 0, 0.28);
  }
  body.light {
    --bg: #f3f6fb;
    --bg-soft: #eaf0f8;
    --card: #ffffff;
    --card-hover: #f8fbff;
    --border: #d8e2ef;
    --text: #101928;
    --muted: #5a6c84;
    --accent: #0969da;
    --accent-soft: rgba(9, 105, 218, 0.1);
    --ok: #15803d;
    --ok-soft: rgba(21, 128, 61, 0.1);
    --warn: #b45309;
    --warn-soft: rgba(180, 83, 9, 0.12);
    --err: #dc2626;
    --err-soft: rgba(220, 38, 38, 0.1);
    --purple: #7c3aed;
    --purple-soft: rgba(124, 58, 237, 0.1);
    --teal: #0f766e;
    --shadow: 0 4px 16px rgba(15, 23, 42, 0.06);
  }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: system-ui, -apple-system, "Segoe UI", "PingFang SC", "Noto Sans CJK SC", sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
  }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }

  header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    padding: 12px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--card);
    position: sticky;
    top: 0;
    z-index: 30;
    box-shadow: var(--shadow);
  }
  .brand { display: flex; align-items: center; gap: 10px; }
  .brand-logo {
    width: 32px; height: 32px; border-radius: 10px;
    background: linear-gradient(135deg, #2563eb, #06b6d4);
    display: flex; align-items: center; justify-content: center;
    font-size: 16px; color: #fff; font-weight: 700;
  }
  .brand h1 { margin: 0; font-size: 16px; font-weight: 700; letter-spacing: 0.2px; }
  .brand-sub { font-size: 11px; color: var(--muted); }
  .header-actions { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
  .status-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: var(--ok); margin-right: 5px; }

  nav.tabs {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding: 10px 24px;
    border-bottom: 1px solid var(--border);
    background: var(--bg-soft);
  }
  .tab {
    border: 1px solid var(--border);
    background: var(--card);
    color: var(--muted);
    padding: 7px 13px;
    border-radius: 9px;
    cursor: pointer;
    font-size: 13px;
    font-weight: 500;
    transition: all 0.15s ease;
    display: inline-flex;
    align-items: center;
    gap: 6px;
  }
  .tab:hover { color: var(--text); border-color: var(--accent); background: var(--card-hover); }
  .tab.active { background: var(--accent); color: #fff; border-color: var(--accent); box-shadow: 0 2px 10px rgba(88,166,255,0.28); }
  .tab-count {
    font-size: 11px; padding: 1px 6px; border-radius: 999px;
    background: rgba(255,255,255,0.12);
  }
  .tab:not(.active) .tab-count { background: var(--bg-soft); color: var(--muted); }

  main { max-width: 1360px; margin: 0 auto; padding: 20px 24px 60px; }
  .panel { display: none; animation: fadeIn 0.18s ease; }
  .panel.active { display: block; }
  @keyframes fadeIn { from { opacity: 0; transform: translateY(3px); } to { opacity: 1; transform: none; } }

  .kpi-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 14px; margin-bottom: 18px; }
  .grid-2 { display: grid; grid-template-columns: repeat(auto-fit, minmax(420px, 1fr)); gap: 16px; margin-bottom: 18px; }
  .grid-3 { display: grid; grid-template-columns: repeat(auto-fit, minmax(310px, 1fr)); gap: 14px; margin-bottom: 18px; }

  .card {
    background: var(--card);
    border: 1px solid var(--border);
    border-radius: 14px;
    padding: 16px 18px;
    box-shadow: var(--shadow);
  }
  .kpi-card { position: relative; overflow: hidden; cursor: pointer; transition: transform 0.15s ease, border-color 0.15s ease; }
  .kpi-card:hover { transform: translateY(-2px); border-color: var(--accent); }
  .kpi-bar { position: absolute; top: 0; left: 0; right: 0; height: 3px; background: var(--accent); }
  .kpi-bar.ok { background: var(--ok); }
  .kpi-bar.warn { background: var(--warn); }
  .kpi-bar.purple { background: var(--purple); }
  .kpi-bar.teal { background: var(--teal); }
  .kpi-label { font-size: 12px; color: var(--muted); font-weight: 500; display: flex; justify-content: space-between; align-items: center; }
  .kpi-value { font-size: 26px; font-weight: 700; margin: 6px 0 2px; letter-spacing: -0.5px; }
  .kpi-sub { font-size: 12px; color: var(--muted); }

  .section-head {
    display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;
    margin: 0 0 12px;
  }
  .section-title { font-size: 15px; font-weight: 700; display: flex; align-items: center; gap: 7px; }
  .muted { color: var(--muted); font-size: 12px; }
  .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }

  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .spacer { flex: 1; }

  button.btn, a.btn {
    border: 1px solid var(--border);
    background: var(--bg-soft);
    color: var(--text);
    padding: 7px 13px;
    border-radius: 8px;
    cursor: pointer;
    font-size: 12.5px;
    font-weight: 500;
    transition: all 0.15s ease;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    text-decoration: none;
  }
  button.btn:hover, a.btn:hover { border-color: var(--accent); background: var(--card-hover); text-decoration: none; }
  button.btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
  button.btn.primary:hover { filter: brightness(1.08); }
  button.btn.ok { background: var(--ok-soft); border-color: var(--ok); color: var(--ok); }
  button.btn.warn { background: var(--warn-soft); border-color: var(--warn); color: var(--warn); }
  button.btn.err { background: var(--err-soft); border-color: var(--err); color: var(--err); }
  button.btn.sm { padding: 4px 9px; font-size: 11.5px; border-radius: 6px; }
  button.btn.selected { box-shadow: 0 0 0 2px var(--accent); font-weight: 700; }
  button.btn:disabled { opacity: 0.5; cursor: not-allowed; }
  tr.sub-row-stopped { opacity: 0.66; transition: opacity 0.15s ease; }
  tr.sub-row-stopped:hover { opacity: 1; }
  .subs-cost-strip {
    display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px;
    padding: 11px 14px; margin-bottom: 12px; border-radius: 10px;
    background: var(--bg-soft); border: 1px solid var(--border);
    border-left: 3px solid var(--purple);
  }
  .subs-cost-main { display: flex; align-items: baseline; flex-wrap: wrap; gap: 10px; }
  .subs-cost-val { font-size: 20px; font-weight: 700; color: var(--text); letter-spacing: -0.3px; }
  tr.sub-total-row td {
    background: var(--bg-soft); border-top: 2px solid var(--border);
    font-weight: 600;
  }
  .ios-switch.sm { width: 38px; height: 22px; }
  .ios-switch.sm .ios-slider:before { height: 16px; width: 16px; left: 3px; bottom: 3px; }
  .ios-switch.sm input:checked + .ios-slider:before { transform: translateX(16px); }

  .badge {
    display: inline-flex; align-items: center; gap: 4px;
    padding: 2px 8px; border-radius: 999px;
    font-size: 11px; font-weight: 600;
    border: 1px solid var(--border);
    background: var(--bg-soft); color: var(--muted);
    white-space: nowrap;
  }
  .badge.ok { background: var(--ok-soft); border-color: rgba(52,211,153,0.35); color: var(--ok); }
  .badge.warn { background: var(--warn-soft); border-color: rgba(245,158,11,0.35); color: var(--warn); }
  .badge.err { background: var(--err-soft); border-color: rgba(248,113,113,0.35); color: var(--err); }
  .badge.accent { background: var(--accent-soft); border-color: rgba(88,166,255,0.35); color: var(--accent); }
  .badge.purple { background: var(--purple-soft); border-color: rgba(167,139,250,0.35); color: var(--purple); }

  input[type="text"], input[type="search"], textarea, select {
    width: 100%;
    background: var(--bg);
    color: var(--text);
    border: 1px solid var(--border);
    border-radius: 9px;
    padding: 8px 11px;
    font-size: 13px;
    outline: none;
    transition: border-color 0.15s;
  }
  input:focus, textarea:focus, select:focus { border-color: var(--accent); }
  textarea { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; line-height: 1.55; }

  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th { text-align: left; font-size: 11.5px; color: var(--muted); font-weight: 600; padding: 8px 10px; border-bottom: 1px solid var(--border); white-space: nowrap; }
  td { padding: 9px 10px; border-bottom: 1px solid rgba(148,163,184,0.12); vertical-align: middle; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(88,166,255,0.04); }
  .table-wrap { overflow-x: auto; }

  .item-list { display: flex; flex-direction: column; gap: 10px; }
  .item-row {
    display: flex; justify-content: space-between; align-items: flex-start; gap: 12px;
    padding: 11px 13px; border-radius: 10px;
    border: 1px solid var(--border); background: var(--bg-soft);
  }
  .item-row:hover { border-color: rgba(88,166,255,0.45); }
  .item-main { flex: 1; min-width: 0; }
  .item-title { font-weight: 600; font-size: 13.5px; margin-bottom: 3px; word-break: break-word; }
  .item-actions { display: flex; gap: 6px; flex-shrink: 0; flex-wrap: wrap; }

  /* iOS-style toggle switch */
  .ios-switch {
    position: relative; display: inline-block; width: 46px; height: 26px; flex-shrink: 0;
  }
  .ios-switch input { opacity: 0; width: 0; height: 0; }
  .ios-slider {
    position: absolute; cursor: pointer; inset: 0;
    background: #475569; border-radius: 999px; transition: 0.2s ease;
  }
  .ios-slider:before {
    position: absolute; content: ""; height: 20px; width: 20px; left: 3px; bottom: 3px;
    background: #fff; border-radius: 50%; transition: 0.2s ease;
    box-shadow: 0 1px 4px rgba(0,0,0,0.35);
  }
  .ios-switch input:checked + .ios-slider { background: var(--ok); }
  .ios-switch input:checked + .ios-slider:before { transform: translateX(20px); }
  .ios-switch input:disabled + .ios-slider { opacity: 0.5; cursor: wait; }

  .wave-block { margin-bottom: 20px; }
  .wave-header {
    display: flex; justify-content: space-between; align-items: center;
    padding: 8px 12px; border-radius: 10px; background: var(--bg-soft);
    border: 1px solid var(--border); margin-bottom: 10px; font-weight: 700; font-size: 13.5px;
  }
  .mod-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(330px, 1fr)); gap: 12px; }
  .mod-card {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 13px 15px; display: flex; flex-direction: column; gap: 8px;
    transition: border-color 0.15s, opacity 0.15s;
  }
  .mod-card.off { opacity: 0.72; border-style: dashed; }
  .mod-card:hover { border-color: var(--accent); }
  .mod-top { display: flex; justify-content: space-between; align-items: flex-start; gap: 10px; }
  .mod-title { font-weight: 700; font-size: 13.5px; }
  .mod-desc { font-size: 12px; color: var(--muted); line-height: 1.45; }
  .mod-meta { display: flex; gap: 6px; flex-wrap: wrap; align-items: center; margin-top: auto; }

  /* Progress bars */
  .prog-track { width: 100%; height: 8px; border-radius: 999px; background: var(--bg-soft); border: 1px solid var(--border); overflow: hidden; }
  .prog-track.exhausted { border-color: rgba(248,113,113,0.65); background: rgba(248,113,113,0.16); }
  .prog-fill { height: 100%; border-radius: 999px; background: var(--accent); transition: width 0.25s ease; }
  .prog-fill.ok { background: var(--ok); }
  .prog-fill.warn { background: var(--warn); }
  .prog-fill.err { background: var(--err); }

  /* Graph workspace */
  .graph-layout { display: grid; grid-template-columns: minmax(0, 1fr) 360px; gap: 14px; margin-bottom: 18px; }
  @media (max-width: 980px) { .graph-layout { grid-template-columns: 1fr; } .grid-2 { grid-template-columns: 1fr; } }
  .graph-canvas-wrap {
    position: relative; background: var(--bg); border: 1px solid var(--border);
    border-radius: 12px; overflow: hidden; height: 500px;
  }
  svg#kg-svg { width: 100%; height: 100%; display: block; cursor: grab; user-select: none; }
  svg#kg-svg:active { cursor: grabbing; }
  .graph-legend {
    display: flex; flex-wrap: wrap; gap: 6px; margin-top: 10px;
  }
  .app-pill {
    display: inline-flex; align-items: center; gap: 6px; padding: 4px 10px;
    border-radius: 999px; font-size: 12px; cursor: pointer;
    border: 1px solid var(--border); background: var(--bg-soft); color: var(--text);
  }
  .app-pill.active { border-color: var(--accent); background: var(--accent-soft); font-weight: 600; }
  .app-dot { width: 9px; height: 9px; border-radius: 50%; display: inline-block; }
  .inspector {
    background: var(--card); border: 1px solid var(--border); border-radius: 12px;
    padding: 15px; height: 500px; overflow-y: auto; display: flex; flex-direction: column; gap: 10px;
  }
  .inspector-body {
    white-space: pre-wrap; font-size: 12.5px; background: var(--bg-soft);
    border: 1px solid var(--border); border-radius: 8px; padding: 10px; line-height: 1.55;
    max-height: 190px; overflow-y: auto; word-break: break-word;
  }
  .neighbor-chip {
    display: flex; justify-content: space-between; align-items: center; gap: 8px;
    padding: 6px 9px; border-radius: 8px; border: 1px solid var(--border);
    background: var(--bg-soft); cursor: pointer; font-size: 12px;
  }
  .neighbor-chip:hover { border-color: var(--accent); }

  /* Wiki 2-col */
  .wiki-layout { display: grid; grid-template-columns: 300px minmax(0, 1fr); gap: 16px; }
  @media (max-width: 900px) { .wiki-layout { grid-template-columns: 1fr; } }
  .wiki-item {
    padding: 10px 12px; border-radius: 10px; border: 1px solid var(--border);
    background: var(--bg-soft); cursor: pointer; transition: border-color 0.15s;
  }
  .wiki-item:hover, .wiki-item.active { border-color: var(--accent); background: var(--accent-soft); }

  /* Learned section */
  .learned-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(310px, 1fr)); gap: 12px; }
  .learned-card {
    background: var(--bg-soft); border: 1px solid var(--border); border-radius: 11px;
    padding: 12px 14px; display: flex; flex-direction: column; gap: 6px;
  }
  .learned-card.clickable { cursor: pointer; }
  .learned-card.clickable:hover { border-color: var(--accent); }

  /* Empty state */
  .empty { text-align: center; padding: 26px 14px; color: var(--muted); font-size: 13px; border: 1px dashed var(--border); border-radius: 10px; }

  /* Raw details */
  details.raw-json { margin-top: 18px; }
  details.raw-json summary { cursor: pointer; color: var(--muted); font-size: 12px; padding: 6px 0; }
  details.raw-json pre {
    background: var(--bg); border: 1px solid var(--border); border-radius: 10px;
    padding: 12px; overflow-x: auto; font-size: 11.5px; max-height: 360px;
  }

  /* Toast */
  #toast {
    position: fixed; right: 20px; bottom: 20px; z-index: 99;
    max-width: 380px; padding: 11px 16px; border-radius: 10px;
    background: var(--card); border: 1px solid var(--accent); color: var(--text);
    box-shadow: 0 10px 30px rgba(0,0,0,0.4); font-size: 13px;
    display: none; animation: fadeIn 0.2s ease;
  }
  #toast.ok { border-color: var(--ok); }
  #toast.err { border-color: var(--err); }

  /* Sure-style Finance Charts */
  .sure-banner {
    display: flex; justify-content: space-between; align-items: center; gap: 12px; flex-wrap: wrap;
    padding: 13px 16px; border-radius: 12px; border: 1px solid var(--border);
    background: linear-gradient(135deg, rgba(88,166,255,0.08), rgba(52,211,153,0.06));
    margin-bottom: 16px;
  }
  .chart-svg-wrap { position: relative; width: 100%; overflow-x: auto; }
  .chart-svg { width: 100%; height: auto; display: block; }
  .chart-tip {
    font-size: 12px; color: var(--muted); min-height: 20px; margin-top: 6px;
    padding: 4px 8px; border-radius: 7px; background: var(--bg-soft); border: 1px solid var(--border);
  }
  .donut-layout { display: grid; grid-template-columns: 200px minmax(0, 1fr); gap: 16px; align-items: center; }
  @media (max-width: 720px) { .donut-layout { grid-template-columns: 1fr; } }
  .legend-row {
    display: flex; align-items: center; justify-content: space-between; gap: 8px;
    padding: 5px 8px; border-radius: 7px; font-size: 12.5px;
  }
  .legend-row:hover { background: var(--bg-soft); }
  .legend-dot { width: 10px; height: 10px; border-radius: 3px; flex-shrink: 0; display: inline-block; }
  .merch-row { margin-bottom: 9px; }
  .merch-head { display: flex; justify-content: space-between; font-size: 12.5px; margin-bottom: 3px; gap: 8px; }
  .range-pills { display: inline-flex; gap: 4px; background: var(--bg-soft); padding: 3px; border-radius: 9px; border: 1px solid var(--border); }
  .range-pills .btn.sm { border: none; background: transparent; padding: 4px 10px; }
  .range-pills .btn.sm.active { background: var(--accent); color: #fff; }

  /* Wiki Conflict Review Queue */
  .tab-count.warn-pill { background: var(--warn); color: #111827; font-weight: 700; }
  .conflict-digest-bar {
    display: flex; gap: 8px; flex-wrap: wrap; align-items: center;
    margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--border);
    font-size: 12.5px;
  }
  .conflict-card {
    background: var(--card); border: 1px solid var(--border); border-left: 4px solid var(--warn);
    border-radius: 12px; padding: 16px 18px; margin-bottom: 14px;
  }
  .conflict-card.kind-policy_conflict { border-left-color: #ef4444; }
  .conflict-card.kind-rule_proposal { border-left-color: #a855f7; }
  .conflict-card.kind-contradiction { border-left-color: var(--err); }
  .conflict-card.resolved { border-left-color: var(--ok); opacity: 0.92; }
  .conflict-card.filtered { border-left-color: #64748b; opacity: 0.88; }
  .cf-question {
    font-size: 15.5px; font-weight: 700; line-height: 1.45; margin: 10px 0 6px;
    color: var(--fg);
  }
  .cf-point {
    font-size: 13px; line-height: 1.55; padding: 8px 12px; border-radius: 8px;
    background: var(--bg-soft); border-left: 3px solid var(--accent); margin-bottom: 10px;
  }
  .cf-rec-box {
    font-size: 12.5px; line-height: 1.5; padding: 8px 12px; border-radius: 8px;
    background: rgba(56, 189, 248, 0.08); border: 1px solid rgba(56, 189, 248, 0.25);
    margin: 0 0 12px; color: var(--fg);
  }
  .cf-rec-box.rec-manual {
    background: rgba(245, 158, 11, 0.09); border-color: rgba(245, 158, 11, 0.32);
  }
  .conflict-sides { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; margin: 10px 0 12px; }
  @media (max-width: 820px) { .conflict-sides { grid-template-columns: 1fr; } }
  .conflict-side {
    background: var(--bg-soft); border: 1px solid var(--border); border-radius: 10px;
    padding: 11px 13px; display: flex; flex-direction: column; gap: 6px;
  }
  .conflict-side.side-old { border-top: 3px solid #94a3b8; }
  .conflict-side.side-new { border-top: 3px solid var(--accent); }
  .conflict-side.winner { border-color: var(--ok); box-shadow: 0 0 0 1px var(--ok) inset; }
  .conflict-side.loser { opacity: 0.58; text-decoration: line-through; }
  .conflict-stmt { font-size: 13.5px; line-height: 1.55; word-break: break-word; white-space: pre-wrap; }
  .cf-evidence {
    margin-top: 10px; border-top: 1px dashed var(--border); padding-top: 8px;
  }
  .cf-evidence > summary {
    cursor: pointer; font-size: 12px; color: var(--muted); user-select: none;
  }
  .cf-evidence > summary:hover { color: var(--fg); }
  .cf-raw-text {
    font-size: 12.5px; line-height: 1.6; word-break: break-word; white-space: pre-wrap;
    background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
    padding: 10px 12px; max-height: 360px; overflow-y: auto;
  }
  .conflict-actions { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
  .conflict-actions .btn.rec-hit { box-shadow: 0 0 0 2px var(--ok); font-weight: 700; }
  .conflict-note-input { flex: 1; min-width: 170px; max-width: 300px; padding: 6px 10px; font-size: 12.5px; }

  /* Compute Resource Monitor */
  .tab-count.err-pill { background: var(--err); color: #fff; font-weight: 700; }
  .compute-toolbar {
    display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap;
    padding: 13px 16px; border-radius: 12px; border: 1px solid var(--border);
    background: linear-gradient(135deg, rgba(88,166,255,0.09), rgba(167,139,250,0.07));
    margin-bottom: 16px;
  }
  .svc-pill {
    display: inline-flex; align-items: center; gap: 6px; padding: 3px 9px;
    border-radius: 999px; font-size: 11.5px; font-weight: 600;
    border: 1px solid var(--border); background: var(--card);
  }
  .svc-dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; background: var(--muted); }
  .svc-dot.on { background: var(--ok); box-shadow: 0 0 6px rgba(52,211,153,0.7); }
  .svc-dot.off { background: var(--err); }
  .compute-alert-strip {
    display: flex; flex-direction: column; gap: 8px; margin-bottom: 16px;
  }
  .compute-alert-item {
    display: flex; justify-content: space-between; align-items: center; gap: 10px; flex-wrap: wrap;
    padding: 9px 13px; border-radius: 10px; border: 1px solid var(--border);
    border-left: 4px solid var(--warn); background: var(--card); font-size: 12.5px;
  }
  .compute-alert-item.critical { border-left-color: var(--err); background: rgba(248,113,113,0.07); }
  .pool-card {
    background: var(--bg-soft); border: 1px solid var(--border); border-radius: 12px;
    padding: 14px 15px; display: flex; flex-direction: column; gap: 8px;
    transition: border-color 0.15s ease;
  }
  .pool-card:hover { border-color: var(--accent); }
  .pool-card.st-critical, .pool-card.st-circuit { border-color: rgba(248,113,113,0.55); }
  .pool-card.st-warn { border-color: rgba(245,158,11,0.45); }
  .pool-balance { font-size: 22px; font-weight: 700; letter-spacing: -0.4px; }
  .win-row { display: flex; flex-direction: column; gap: 4px; }
  .win-head { display: flex; justify-content: space-between; align-items: center; gap: 8px; font-size: 12px; }
  .hw-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 12px; margin-bottom: 14px; }
  .hw-box {
    background: var(--bg-soft); border: 1px solid var(--border); border-radius: 11px;
    padding: 13px 14px; display: flex; flex-direction: column; gap: 7px;
  }
  .hw-big { font-size: 23px; font-weight: 700; letter-spacing: -0.4px; }
  .compute-form-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(175px, 1fr)); gap: 10px; margin-bottom: 12px; }
  .compute-form-field label { display: block; font-size: 11.5px; color: var(--muted); margin-bottom: 4px; font-weight: 600; }
  input[type="number"] {
    width: 100%; background: var(--bg); color: var(--text); border: 1px solid var(--border);
    border-radius: 9px; padding: 8px 11px; font-size: 13px; outline: none;
  }
  input[type="number"]:focus { border-color: var(--accent); }
  .auto-refresh-lbl { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--muted); cursor: pointer; user-select: none; }

  /* Ops & Schedule Radar (Plan A) */
  .ops-ext-strip {
    display: flex; flex-wrap: wrap; gap: 8px; align-items: center;
    padding: 11px 14px; border-radius: 12px; border: 1px solid var(--border);
    background: var(--card); margin-bottom: 16px;
  }
  .ops-ext-link {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 6px 11px; border-radius: 9px; border: 1px solid var(--border);
    background: var(--bg-soft); color: var(--text); font-size: 12.5px; font-weight: 500;
    text-decoration: none; transition: border-color 0.15s, background 0.15s;
  }
  .ops-ext-link:hover { border-color: var(--accent); background: var(--card-hover); text-decoration: none; }
  .ops-host-pills { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 6px; }
  .ops-preview-pre {
    background: var(--bg); border: 1px solid var(--border); border-radius: 10px;
    padding: 12px; overflow: auto; font-size: 12px; line-height: 1.55;
    max-height: 360px; white-space: pre-wrap; word-break: break-word; margin: 0;
  }
  .ops-dep-row {
    display: flex; justify-content: space-between; align-items: center; gap: 10px;
    padding: 8px 11px; border-radius: 9px; border: 1px solid var(--border);
    background: var(--bg-soft); font-size: 12.5px;
  }
</style>
</head>
<body>
<header>
  <div class="brand">
    <div class="brand-logo">⚡</div>
    <div>
      <h1>司命 Siming v5 · 统一网页控制塔</h1>
      <div class="brand-sub"><span class="status-dot"></span><span id="ts">正在连接控制塔…</span></div>
    </div>
  </div>
  <div class="header-actions">
    <a class="btn" href="v5-guide.html">📖 v5 教程</a>
    <button class="btn" onclick="toggleTheme()">🌗 主题切换</button>
    <button class="btn primary" onclick="refreshAll()">🔄 刷新全部</button>
  </div>
</header>

<nav class="tabs" id="tabs">
  <button class="tab active" data-tab="overview">📊 总览 Overview</button>
  <button class="tab" data-tab="compute">⚡ 算力监控 <span class="tab-count" id="tc-compute">-</span></button>
  <button class="tab" data-tab="modules">🎛️ 模块管理 <span class="tab-count" id="tc-modules">-</span></button>
  <button class="tab" data-tab="ops">⚙️ 运维与调度 <span class="tab-count" id="tc-ops">-</span></button>
  <button class="tab" data-tab="graph">🕸️ 知识图谱 &amp; 学到了什么 <span class="tab-count" id="tc-graph">-</span></button>
  <button class="tab" data-tab="finance">💰 财务、算力与订阅</button>
  <button class="tab" data-tab="replenish">🛒 日用品补货 <span class="tab-count" id="tc-replenish">-</span></button>
  <button class="tab" data-tab="smarthome">🏠 新房与实验室</button>
  <button class="tab" data-tab="wiki">📚 共享 Wiki <span class="tab-count" id="tc-wiki">-</span></button>
  <button class="tab" data-tab="conflicts">⚖️ 冲突审核 <span class="tab-count" id="tc-conflicts">0</span></button>
  <button class="tab" data-tab="msim">🧠 M-SIM 进化榜</button>
  <button class="tab" data-tab="routines">📝 Routines 规律库</button>
</nav>

<main>
  <!-- 1. OVERVIEW -->
  <section id="panel-overview" class="panel active">
    <div id="overview-kpis" class="kpi-grid"></div>
    <div class="card" style="margin-bottom:16px">
      <div class="section-head" style="margin-bottom:0">
        <div class="section-title">⚡ 快捷运维操作</div>
        <div class="row">
          <button class="btn primary" onclick="refreshComputeLive()">⚡ 同步实时算力</button>
          <button class="btn" onclick="runGraphReindex()">🔄 刷新图谱索引</button>
          <button class="btn ok" onclick="runWikiDistill()">📚 立即蒸馏共享 Wiki</button>
          <button class="btn warn" onclick="runSubsRescan()">✂️ 重扫订阅刺客</button>
        </div>
      </div>
    </div>
    <div id="overview-admin" class="grid-2"></div>
    <div id="overview-extra"></div>
    <details class="raw-json"><summary>调试：查看原始 JSON 快照</summary><pre id="overview-raw"></pre></details>
  </section>

  <!-- 1b. COMPUTE RESOURCE MONITOR -->
  <section id="panel-compute" class="panel">
    <div class="compute-toolbar">
      <div>
        <div class="row" style="gap:8px;margin-bottom:4px">
          <strong style="font-size:15px">⚡ 算力资源全景监控</strong>
          <span class="badge accent" id="compute-cache-badge">加载中</span>
          <span id="compute-services-bar" class="row" style="gap:6px"></span>
        </div>
        <div class="muted" id="compute-sub-ts">实时汇聚 Magpie / ai-usagebar / ai-quota / OmniRoute / compute_assets 与本机 CPU·内存·GPU</div>
      </div>
      <div class="row" style="gap:8px">
        <label class="auto-refresh-lbl">
          <input type="checkbox" id="compute-auto-cb" onchange="toggleComputeAutoRefresh(this.checked)">
          ⏱️ 15s 自动刷新
        </label>
        <button class="btn sm" onclick="toggleComputeAssetForm()">➕ 登记/更新账户</button>
        <button class="btn sm warn" onclick="runComputeGuardrails()">🛡️ 护栏巡检</button>
        <button class="btn sm primary" id="btn-compute-refresh" onclick="refreshComputeLive()">🔄 刷新并同步实时算力</button>
      </div>
    </div>

    <div id="compute-kpis" class="kpi-grid"></div>
    <div id="compute-alerts" class="compute-alert-strip"></div>

    <div class="card" id="compute-asset-form-card" style="margin-bottom:16px; display:none">
      <div class="section-head">
        <div class="section-title">➕ 手动登记 / 更新 AI 算力资产（写入 compute_assets 并联动熔断护栏）</div>
        <button class="btn sm" onclick="toggleComputeAssetForm()">收起</button>
      </div>
      <form id="compute-asset-form" onsubmit="submitComputeAssetForm(event)">
        <div class="compute-form-grid">
          <div class="compute-form-field">
            <label for="ca-provider">提供商 (provider)</label>
            <input type="text" id="ca-provider" placeholder="deepseek / nous / aliyun / kimi / redpill" required>
          </div>
          <div class="compute-form-field">
            <label for="ca-label">账号标签</label>
            <input type="text" id="ca-label" placeholder="main / lab-cw08" value="main">
          </div>
          <div class="compute-form-field">
            <label for="ca-balance">当前余额</label>
            <input type="number" id="ca-balance" step="0.01" placeholder="500.00" required>
          </div>
          <div class="compute-form-field">
            <label for="ca-currency">币种</label>
            <select id="ca-currency">
              <option value="CNY">CNY (¥)</option>
              <option value="USD">USD ($)</option>
            </select>
          </div>
          <div class="compute-form-field">
            <label for="ca-daily">今日消耗</label>
            <input type="number" id="ca-daily" step="0.01" value="0">
          </div>
          <div class="compute-form-field">
            <label for="ca-avg">日均消耗</label>
            <input type="number" id="ca-avg" step="0.01" value="0">
          </div>
          <div class="compute-form-field">
            <label for="ca-scope">费用归属</label>
            <select id="ca-scope">
              <option value="personal">个人 (personal)</option>
              <option value="work_cw08">公费 (work_cw08)</option>
            </select>
          </div>
          <div class="compute-form-field">
            <label for="ca-plan">套餐 / 节省计划名</label>
            <input type="text" id="ca-plan" placeholder="可选，如 百炼 Coding Plan">
          </div>
          <div class="compute-form-field">
            <label for="ca-expires">套餐到期日</label>
            <input type="text" id="ca-expires" placeholder="YYYY-MM-DD（可选）">
          </div>
          <div class="compute-form-field">
            <label for="ca-fallback">降级备用模型</label>
            <input type="text" id="ca-fallback" placeholder="可选，如 deepseek-chat">
          </div>
        </div>
        <div class="row">
          <button type="submit" class="btn primary">💾 保存并立即评估护栏</button>
          <span class="muted">提交后自动写 audit 审计日志并刷新财务页算力资产卡</span>
        </div>
      </form>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="section-head">
        <div>
          <div class="section-title">🎟️ 实时 AI 算力配额、余额池与熔断控制</div>
          <div class="muted" id="compute-pools-meta">覆盖订阅窗口（5h / 7d / 30d）、Credits 池与 API 余额；支持一键熔断/恢复多 Runtime 路由</div>
        </div>
        <div class="range-pills" id="compute-filter-pills">
          <button class="btn sm active" data-cf="all" onclick="setComputeFilter('all')">全部</button>
          <button class="btn sm" data-cf="subscription" onclick="setComputeFilter('subscription')">🎟️ 订阅配额</button>
          <button class="btn sm" data-cf="balance" onclick="setComputeFilter('balance')">💰 API 余额</button>
          <button class="btn sm" data-cf="alert" onclick="setComputeFilter('alert')">⚠️ 仅告警</button>
        </div>
      </div>
      <div id="compute-pools" class="grid-3" style="margin-bottom:0"></div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="section-head">
        <div>
          <div class="section-title">🖥️ 本机计算资源与硬件遥测</div>
          <div class="muted" id="compute-host-meta">直读 Linux /proc 与 /sys 传感器（CPU / 内存 / GPU / 显存 / 磁盘 / 温度 / Top 进程）</div>
        </div>
        <span class="badge mono" id="compute-host-uptime"></span>
      </div>
      <div id="compute-hardware" class="hw-grid"></div>
      <div class="grid-2" style="margin-bottom:0">
        <div>
          <div class="section-title" style="font-size:13.5px;margin-bottom:8px">🔥 本机高负载进程 (Top CPU)</div>
          <div id="compute-procs" class="table-wrap"></div>
        </div>
        <div>
          <div class="section-title" style="font-size:13.5px;margin-bottom:8px">🧪 远端 GPU 交班与长任务监听 (gpu-server / task_runs)</div>
          <div id="compute-remote"></div>
        </div>
      </div>
    </div>

    <div class="grid-2" style="margin-bottom:16px">
      <div class="card">
        <div class="section-head">
          <div>
            <div class="section-title">📈 近 24 小时 AI 网关调用与 Token 趋势</div>
            <div class="muted" id="compute-traffic-meta">聚合 Magpie usage.jsonl + OmniRoute + Siming usage</div>
          </div>
        </div>
        <div class="chart-svg-wrap" id="compute-hourly-svg"></div>
        <div class="chart-tip" id="compute-hourly-tip">悬停柱子查看该小时调用次数、Token 吞吐与错误数</div>
        <div style="margin-top:12px">
          <div class="section-title" style="font-size:13.5px;margin-bottom:8px">🤖 近 7 天活跃模型与厂商分布</div>
          <div id="compute-models-table" class="table-wrap"></div>
        </div>
      </div>
      <div class="card">
        <div class="section-head">
          <div>
            <div class="section-title">💵 账本 AI 算力月度支出（CNY / USD 严格隔离）</div>
            <div class="muted" id="compute-spend-meta">来自 siming.db ledger_txns（排除划转与 VOID，按真实币种分列）</div>
          </div>
        </div>
        <div id="compute-spend-body"></div>
      </div>
    </div>

    <div class="card">
      <div class="section-head">
        <div>
          <div class="section-title">🧭 Linger/Pi 多 Runtime 调度底座 &amp; 熔断路由</div>
          <div class="muted" id="compute-runtime-pick">按 ai-quota 余量与熔断状态自动路由重型编码与轻量分拣任务</div>
        </div>
      </div>
      <div id="compute-runtimes" class="grid-3" style="margin-bottom:0"></div>
    </div>
  </section>

  <!-- 2. MODULES -->
  <section id="panel-modules" class="panel">
    <div class="card" style="margin-bottom:16px">
      <div class="row">
        <div class="section-title" id="mod-summary-title">🎛️ 模块管理</div>
        <div class="spacer"></div>
        <input type="search" id="mod-search" placeholder="搜索模块名 / ID / 别名 / 描述…" style="max-width:280px" oninput="renderModulesView()">
        <div class="row" style="gap:6px">
          <button class="btn sm primary" id="mf-all" onclick="setModFilter('all')">全部</button>
          <button class="btn sm" id="mf-on" onclick="setModFilter('on')">🟢 已开启</button>
          <button class="btn sm" id="mf-off" onclick="setModFilter('off')">⚪ 已关闭</button>
        </div>
      </div>
      <div id="mod-audit-bar" class="row" style="margin-top:12px"></div>
    </div>
    <div id="modules-waves"></div>
    <div class="card" style="margin-top:18px">
      <div class="section-title" style="margin-bottom:10px">📡 数据源健康巡检</div>
      <div id="sources-grid" class="learned-grid"></div>
    </div>
  </section>

  <!-- 2b. OPS & SCHEDULE RADAR (PLAN A) -->
  <section id="panel-ops" class="panel">
    <div class="compute-toolbar">
      <div>
        <div class="row" style="gap:8px;margin-bottom:4px">
          <strong style="font-size:15px">⚙️ 司命自运维与统一调度总表</strong>
          <span class="badge accent" id="ops-status-badge">加载中</span>
          <span class="badge" id="ops-pause-badge" style="display:none"></span>
        </div>
        <div class="muted" id="ops-sub-ts">聚合司命 4 核心 Timer / 推送熔断 (Push Guard) / 链路自检 + 三合一 Cron 只读雷达（Systemd · Hermes · Waits）</div>
        <div class="ops-host-pills" id="ops-host-pills"></div>
      </div>
      <div class="row" style="gap:7px;flex-wrap:wrap">
        <select id="ops-pause-dur" style="width:auto;min-width:96px;padding:6px 9px;font-size:12px" title="暂停后台自动 worker 时长（提醒仍正常发）">
          <option value="1h">暂停 1 小时</option>
          <option value="2h" selected>暂停 2 小时</option>
          <option value="6h">暂停 6 小时</option>
          <option value="24h">暂停 24 小时</option>
        </select>
        <button class="btn sm warn" id="btn-ops-pause" onclick="setSimingPauseAction('pause')">⏸️ 暂停 Worker</button>
        <button class="btn sm ok" id="btn-ops-resume" style="display:none" onclick="setSimingPauseAction('resume')">▶️ 恢复后台</button>
        <button class="btn sm" id="btn-ops-diag" onclick="runOpsDiagnoseAction(false)">🩺 链路体检</button>
        <button class="btn sm warn" id="btn-ops-heal" onclick="runOpsDiagnoseAction(true)" title="仅允许白名单清 sing-box DNS 负缓存，严禁执行任意命令">🩹 体检并清 DNS 缓存</button>
        <button class="btn sm primary" onclick="refreshOpsOnly()">🔄 刷新运维</button>
      </div>
    </div>

    <div class="ops-ext-strip" id="ops-ext-tools">
      <span class="muted" style="font-weight:600">🔗 专业监控面板：</span>
      <span class="muted">加载中…</span>
    </div>

    <div id="ops-issues" class="compute-alert-strip"></div>
    <div id="ops-kpis" class="kpi-grid"></div>

    <div class="grid-2" style="margin-bottom:16px">
      <div class="card">
        <div class="section-head">
          <div>
            <div class="section-title">⏱️ 司命核心 4 Timer 运行态</div>
            <div class="muted">tick / edge-sync / watchdog / backup 最近执行与耗时</div>
          </div>
        </div>
        <div id="ops-core-timers" class="item-list"></div>
        <div style="margin-top:14px">
          <div class="section-title" style="font-size:13.5px;margin-bottom:8px">📦 Effects 队列异常 / 运行中</div>
          <div id="ops-effects-issues"></div>
        </div>
      </div>

      <div class="card">
        <div class="section-head">
          <div>
            <div class="section-title">🩺 核心依赖链与离线备份状态</div>
            <div class="muted">CDP 9222 / sing-box 7890 / 81 边端 / iPhone 心跳 / SQLite 备份</div>
          </div>
        </div>
        <div id="ops-deps" class="item-list" style="margin-bottom:12px"></div>
        <div id="ops-diagnose-box"></div>
      </div>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="section-head">
        <div>
          <div class="section-title">🧯 推送熔断与隐式降级（Push Guard · D-33）</div>
          <div class="muted" id="ops-pg-meta">查看处于 QUIET 静默降级或熔断冷却中的推送规则，支持一键恢复即时推送（免进终端）</div>
        </div>
      </div>
      <div id="ops-push-guard"></div>
    </div>

    <div class="card">
      <div class="section-head">
        <div>
          <div class="section-title">🗓️ 三合一定时任务只读总表（Systemd Timers · Hermes Cron · Siming Waits）</div>
          <div class="muted" id="ops-sched-meta">严格只读视图：一眼看清谁在跑、挂没挂、谁在直推 QQ（修改系统定时器仍请在终端操作）</div>
        </div>
        <input type="search" id="ops-sched-search" placeholder="搜索任务名 / 描述 / 脚本 / 规则…" style="max-width:250px" oninput="renderOpsSchedules()">
      </div>
      <div class="graph-legend" id="ops-sched-filters" style="margin-top:0;margin-bottom:12px"></div>
      <div id="ops-schedules-wrap" class="table-wrap"></div>

      <div class="card" id="ops-preview-card" style="margin-top:14px;background:var(--bg-soft);display:none">
        <div class="section-head" style="margin-bottom:8px">
          <div>
            <div class="section-title" style="font-size:13.5px" id="ops-preview-title">📄 最近输出 / 日志预览（只读 · 已脱敏）</div>
            <div class="muted mono" id="ops-preview-sub"></div>
          </div>
          <button class="btn sm" onclick="document.getElementById('ops-preview-card').style.display='none'">收起</button>
        </div>
        <pre id="ops-preview-body" class="ops-preview-pre"></pre>
      </div>
    </div>
  </section>

  <!-- 3. GRAPH & LEARNED -->
  <section id="panel-graph" class="panel">
    <div class="card" style="margin-bottom:16px">
      <div class="section-head">
        <div class="section-title">🕸️ 跨应用知识图谱可视化 <span class="badge accent" id="kg-stats-badge">加载中</span></div>
        <div class="row">
          <button class="btn sm" onclick="zoomGraph(1.2)">＋ 放大</button>
          <button class="btn sm" onclick="zoomGraph(0.82)">－ 缩小</button>
          <button class="btn sm" onclick="resetGraphView()">⟲ 重置视图</button>
          <button class="btn sm primary" onclick="runGraphReindex()">🔄 重建图谱</button>
        </div>
      </div>
      <div class="row" style="margin-bottom:10px; flex-wrap:nowrap">
        <input type="text" id="graph-q" style="flex:1; width:auto" placeholder="跨应用联邦检索，例如：去年东京机票刷哪张卡 报销了没有 订的哪家酒店" onkeydown="if(event.key==='Enter')searchGraph()">
        <button class="btn primary" onclick="searchGraph()" style="flex-shrink:0">🔎 联邦检索</button>
      </div>
      <div class="graph-legend" id="kg-filters"></div>
    </div>

    <div class="graph-layout">
      <div class="graph-canvas-wrap">
        <svg id="kg-svg" viewBox="0 0 900 500">
          <g id="kg-viewport">
            <g id="kg-edges"></g>
            <g id="kg-nodes"></g>
          </g>
        </svg>
      </div>
      <div class="inspector" id="kg-inspector">
        <div class="section-title">🔍 节点详情检查器</div>
        <div class="empty">点击左侧图谱任意节点，查看完整内容、来源出处（provenance）与跨应用关联边。</div>
      </div>
    </div>

    <div class="card" id="graph-result-card" style="margin-bottom:18px; display:none">
      <div class="section-head">
        <div class="section-title">🔎 联邦检索结果</div>
        <button class="btn sm" onclick="document.getElementById('graph-result-card').style.display='none'">收起</button>
      </div>
      <div id="graph-result"></div>
    </div>

    <div class="card">
      <div class="section-head">
        <div class="section-title">🎓 司命学到了什么（已沉淀知识全景）</div>
        <div class="row" id="learned-counts-bar"></div>
      </div>
      <div id="learned-body"></div>
    </div>
  </section>

  <!-- 4. FINANCE -->
  <section id="panel-finance" class="panel">
    <div id="finance-kpis" class="kpi-grid"></div>
    <div id="finance-sure-banner" class="sure-banner"></div>
    <div class="card" style="margin-bottom:16px">
      <div class="section-head">
        <div>
          <div class="section-title">📈 Sure 风格财务图表（CNY 主币种 · 剔除划转 · 退款抵减净支出）</div>
          <div class="muted" id="finance-charts-sub">数据直接聚合自本机 siming.db，支持深浅色主题与区间切换</div>
        </div>
        <div class="range-pills" id="finance-range-pills">
          <button class="btn sm" data-range="current_month" onclick="setFinanceRange('current_month')">本月</button>
          <button class="btn sm active" data-range="last_3m" onclick="setFinanceRange('last_3m')">近 3 个月</button>
          <button class="btn sm" data-range="last_12m" onclick="setFinanceRange('last_12m')">近 12 个月</button>
        </div>
      </div>
      <div class="grid-2" style="margin-bottom:14px">
        <div class="card" style="background:var(--bg);margin-bottom:0">
          <div class="section-head" style="margin-bottom:8px">
            <div class="section-title" style="font-size:13.5px">💹 累计净现金流趋势（近 12 个月 CNY）</div>
            <span class="muted mono" id="chart-netflow-badge"></span>
          </div>
          <div class="chart-svg-wrap" id="chart-netflow-svg"></div>
          <div class="chart-tip" id="chart-netflow-tip">悬停数据点查看当月收入、净支出与累计净流</div>
        </div>
        <div class="card" style="background:var(--bg);margin-bottom:0">
          <div class="section-head" style="margin-bottom:8px">
            <div class="section-title" style="font-size:13.5px">📊 月度收入 vs 净支出（近 12 个月 CNY）</div>
            <span class="muted" style="font-size:11.5px"><span style="color:#34d399">■ 收入</span> · <span style="color:#f87171">■ 净支出</span></span>
          </div>
          <div class="chart-svg-wrap" id="chart-bar-svg"></div>
          <div class="chart-tip" id="chart-bar-tip">悬停柱子查看当月收入、消费、退款与净支出明细</div>
        </div>
      </div>
      <div class="grid-2" style="margin-bottom:14px">
        <div class="card" style="background:var(--bg);margin-bottom:0">
          <div class="section-head" style="margin-bottom:8px">
            <div class="section-title" style="font-size:13.5px">🍩 支出分类占比（CNY）</div>
            <span class="muted mono" id="chart-donut-range-lbl"></span>
          </div>
          <div id="chart-donut-wrap"></div>
        </div>
        <div class="card" style="background:var(--bg);margin-bottom:0">
          <div class="section-head" style="margin-bottom:8px">
            <div class="section-title" style="font-size:13.5px">🏪 Top 10 消费商户（CNY 净支出）</div>
            <span class="muted mono" id="chart-merch-range-lbl"></span>
          </div>
          <div id="chart-merchants-wrap"></div>
        </div>
      </div>
      <div class="card" style="background:var(--bg);margin-bottom:0" id="finance-usd-card"></div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <div class="section-head">
        <div>
          <div class="section-title">⚡ AI 算力资产总控</div>
          <div class="muted">余额 / 日耗 / 套餐到期状态（实时配额、Token 流量与硬件监控见「算力监控」页）</div>
        </div>
        <div class="row" style="gap:6px">
          <button class="btn sm" onclick="refreshComputeLive()">🔄 同步实时算力</button>
          <button class="btn sm primary" onclick="switchTab('compute')">⚡ 打开算力监控 →</button>
        </div>
      </div>
      <div id="finance-compute" class="grid-3" style="margin-bottom:0"></div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <div class="section-head">
        <div>
          <div class="section-title">🗡️ 订阅刺客审计清单</div>
          <div class="muted">区分「是否还在进行（自动按账期判断 / 可手动切换）」与「审计标签（正常 / 保留 / 僵尸 / 已取消）」</div>
        </div>
        <button class="btn sm warn" onclick="runSubsRescan()">✂️ 重扫订阅刺客</button>
      </div>
      <div id="finance-subs-summary"></div>
      <div id="finance-subs-filters" class="row" style="gap:6px;margin-bottom:10px"></div>
      <div id="finance-subs" class="table-wrap"></div>
    </div>
    <div class="card">
      <div class="section-head">
        <div class="section-title">🧾 最近账本流水</div>
        <span class="muted" id="finance-txn-meta"></span>
      </div>
      <div id="finance-txns" class="table-wrap"></div>
    </div>
  </section>

  <!-- 5. SMARTHOME -->
  <section id="panel-smarthome" class="panel">
    <div id="smarthome-top" class="grid-2"></div>
    <div class="card" style="margin-bottom:16px">
      <div class="section-title" style="margin-bottom:12px">🖥️ Homelab 节点健康状态</div>
      <div id="sh-homelab" class="grid-3" style="margin-bottom:0"></div>
    </div>
    <div class="card" style="margin-bottom:16px">
      <div class="section-title" style="margin-bottom:12px">🧵 Bambu 3D 耗材与打印任务</div>
      <div id="sh-bambu"></div>
    </div>
    <div class="grid-2">
      <div class="card">
        <div class="section-title" style="margin-bottom:10px">💧 水电物业账期</div>
        <div id="sh-utilities"></div>
      </div>
      <div class="card">
        <div class="section-title" style="margin-bottom:10px">🔧 家电耗材到期清单</div>
        <div id="sh-consumables"></div>
      </div>
    </div>
  </section>

  <!-- 5b. DAILY REPLENISH -->
  <section id="panel-replenish" class="panel">
    <div class="compute-toolbar">
      <div>
        <div class="row" style="gap:8px;margin-bottom:4px">
          <strong style="font-size:15px">🛒 日用品周期补货台账（家 / 公司）</strong>
          <span class="badge accent" id="rpl-mod-badge">加载中</span>
        </div>
        <div class="muted" id="rpl-sub-ts">基于美团订单与支付宝/微信流水推算消耗周期 · 到期自动弹 QQ 按钮确认 · 点确认走美团闪购直买或京东跨端加车</div>
      </div>
      <div class="row" style="gap:8px;flex-wrap:wrap">
        <button class="btn sm" onclick="toggleReplenishForm(true)">➕ 新增 / 编辑日用品</button>
        <button class="btn sm" onclick="reinferReplenish()">🔍 重算消费记录周期</button>
        <button class="btn sm primary" onclick="checkDueReplenish()">🔔 巡检到期并推 QQ</button>
      </div>
    </div>

    <div id="rpl-kpis" class="kpi-grid"></div>

    <div class="card" id="rpl-form-card" style="margin-bottom:16px; display:none">
      <div class="section-head">
        <div class="section-title" id="rpl-form-title">➕ 新增 / 编辑日用品补货周期</div>
        <button class="btn sm" onclick="toggleReplenishForm(false)">收起</button>
      </div>
      <form id="rpl-form" onsubmit="submitReplenishForm(event)">
        <input type="hidden" id="rpl-id" value="">
        <div class="compute-form-grid">
          <div class="compute-form-field">
            <label for="rpl-name">商品名称</label>
            <input type="text" id="rpl-name" placeholder="如 抽纸（家） / 加湿器滤芯" required>
          </div>
          <div class="compute-form-field">
            <label for="rpl-category">分类</label>
            <select id="rpl-category">
              <option value="清洁洗护">清洁洗护</option>
              <option value="纸品湿巾">纸品湿巾</option>
              <option value="饮用水">饮用水</option>
              <option value="家电滤芯">家电滤芯</option>
              <option value="垃圾收纳">垃圾收纳</option>
              <option value="个护日用">个护日用</option>
              <option value="照护医疗">照护医疗</option>
              <option value="日用消耗">日用消耗</option>
            </select>
          </div>
          <div class="compute-form-field">
            <label for="rpl-location">配送地点</label>
            <select id="rpl-location">
              <option value="家">🏠 家（示例小区A/示例小区B）</option>
              <option value="公司">🏢 公司（示例写字楼）</option>
            </select>
          </div>
          <div class="compute-form-field">
            <label for="rpl-channel">购买渠道</label>
            <select id="rpl-channel">
              <option value="meituan">🛵 美团闪购直买（点确认自动下单付款）</option>
              <option value="jd">📦 京东跨端同步购物车</option>
              <option value="taobao">🧡 淘宝 / 天猫（到期提醒复购）</option>
              <option value="sams">🏬 山姆会员商店（到期提醒囤货）</option>
              <option value="hema">🦛 盒马鲜生（到期提醒复购）</option>
            </select>
          </div>
          <div class="compute-form-field">
            <label for="rpl-cycle">补货周期（天）</label>
            <input type="number" id="rpl-cycle" min="3" max="730" value="45" required>
          </div>
          <div class="compute-form-field">
            <label for="rpl-lead">提前提醒（天）</label>
            <input type="number" id="rpl-lead" min="1" max="30" value="5">
          </div>
          <div class="compute-form-field">
            <label for="rpl-qty">默认份数</label>
            <input type="number" id="rpl-qty" min="1" max="99" value="1">
          </div>
          <div class="compute-form-field">
            <label for="rpl-price">参考单价（¥）</label>
            <input type="number" id="rpl-price" step="0.01" placeholder="可选，如 22">
          </div>
          <div class="compute-form-field">
            <label for="rpl-last">上次购买日</label>
            <input type="text" id="rpl-last" placeholder="YYYY-MM-DD（留空按今天）">
          </div>
          <div class="compute-form-field">
            <label for="rpl-shop">美团优先商超</label>
            <input type="text" id="rpl-shop" placeholder="如 即购超市 / 小象超市 / 乐购达">
          </div>
          <div class="compute-form-field">
            <label for="rpl-query">美团搜索关键词</label>
            <input type="text" id="rpl-query" placeholder="如 维达抽纸 6包">
          </div>
          <div class="compute-form-field">
            <label for="rpl-sku">京东链接 / SKU</label>
            <input type="text" id="rpl-sku" placeholder="京东渠道必填，如 100012345678">
          </div>
          <div class="compute-form-field">
            <label for="rpl-tier">分级</label>
            <select id="rpl-tier">
              <option value="core">⭐ 核心日用</option>
              <option value="recommended">✨ 消费挖掘常买</option>
              <option value="movein">🏡 新房入住后启用</option>
            </select>
          </div>
          <div class="compute-form-field">
            <label for="rpl-spec">规格说明</label>
            <input type="text" id="rpl-spec" placeholder="如 130抽×6包/提 S码">
          </div>
          <div class="compute-form-field" style="grid-column: span 2">
            <label for="rpl-notes">备注</label>
            <input type="text" id="rpl-notes" placeholder="如 型号说明、囤货装周期提示等">
          </div>
        </div>
        <div class="row" style="gap:10px">
          <button type="submit" class="btn primary">💾 保存日用品配置</button>
          <button type="button" class="btn" onclick="resetReplenishForm()">🧹 清空新建</button>
          <span class="muted">保存后立即同步控制塔与 QQ 到期巡检，并写入审计日志</span>
        </div>
      </form>
    </div>

    <div class="card" style="margin-bottom:16px">
      <div class="section-head">
        <div class="range-pills" id="rpl-filter-pills">
          <button class="btn sm active" data-rf="all" onclick="setReplenishFilter('all')">全部</button>
          <button class="btn sm" data-rf="due" onclick="setReplenishFilter('due')">🔴 待补/临期</button>
          <button class="btn sm" data-rf="calibrate" onclick="setReplenishFilter('calibrate')">🟡 待校准</button>
          <button class="btn sm" data-rf="core" onclick="setReplenishFilter('core')">⭐ 核心清单</button>
          <button class="btn sm" data-rf="home" onclick="setReplenishFilter('home')">🏠 家</button>
          <button class="btn sm" data-rf="office" onclick="setReplenishFilter('office')">🏢 公司</button>
          <button class="btn sm" data-rf="paused" onclick="setReplenishFilter('paused')">⚪ 已暂停</button>
        </div>
        <input type="search" id="rpl-search" placeholder="搜索品名 / 规格 / 门店 / 依据…" style="max-width:250px" oninput="renderReplenish()">
      </div>
      <div id="rpl-table-wrap" class="table-wrap"></div>
    </div>

    <div class="card">
      <div class="section-head">
        <div>
          <div class="section-title">🩺 照护耗材实测雷达（联动 daily_care）</div>
          <div class="muted">口罩 / 注射笔针头 / 酒精棉片按包装规格自动扣减余量，在 QQ 发「口罩还剩 N」可直接校准</div>
        </div>
      </div>
      <div id="rpl-care-supplies" class="grid-3" style="margin-bottom:0"></div>
    </div>
  </section>

  <!-- 6. WIKI -->
  <section id="panel-wiki" class="panel">
    <div class="wiki-layout">
      <div class="card">
        <div class="section-head">
          <div class="section-title">📚 页面目录</div>
          <div class="row" style="gap:5px">
            <button class="btn sm" onclick="newWikiPage()">＋ 新建</button>
            <button class="btn sm ok" onclick="runWikiDistill()">✨ 蒸馏</button>
          </div>
        </div>
        <input type="search" id="wiki-filter" placeholder="筛选 Wiki 页面…" style="margin-bottom:10px" oninput="renderWikiSidebar()">
        <div id="wiki-list" class="item-list"></div>
      </div>
      <div class="card">
        <div class="section-head">
          <div class="section-title">✍️ Hindsight Wiki 编辑器</div>
          <button class="btn primary" onclick="saveWiki()">💾 保存 Wiki（自动备份）</button>
        </div>
        <div class="row" style="margin-bottom:10px">
          <input type="text" id="wiki-slug" placeholder="页面 slug (如 tokyo-trip-notes)" style="max-width:240px">
          <input type="text" id="wiki-title" placeholder="页面标题" style="flex:1">
          <input type="text" id="wiki-category" placeholder="分类 (如 general)" style="max-width:150px">
        </div>
        <textarea id="wiki-content" rows="16" placeholder="Markdown 内容…"></textarea>
        <div class="muted" id="wiki-status" style="margin-top:8px"></div>
        <div id="wiki-conflicts" style="margin-top:12px"></div>
      </div>
    </div>
  </section>

  <!-- 6b. WIKI CONFLICTS REVIEW QUEUE -->
  <section id="panel-conflicts" class="panel">
    <div class="card" style="margin-bottom:16px">
      <div class="section-head">
        <div>
          <div class="section-title">⚖️ 冲突与待拍板审核台</div>
          <div class="muted" id="conflicts-summary">已先过滤误报并为你提炼每条的「裁决问题 + 核心分歧 + 建议」，不用通读全文即可直接拍板；需要核对时可随时展开完整未截断原文。</div>
        </div>
        <div class="row" style="gap:6px">
          <button class="btn sm primary" id="btn-cf-summarize" onclick="summarizeConflictsAction()">🧠 AI 重提炼裁决点</button>
          <button class="btn sm ok" onclick="runWikiDistill()">✨ 重新蒸馏并刷新</button>
        </div>
      </div>
      <div class="row" style="gap:8px; flex-wrap:wrap">
        <div class="range-pills" id="conflict-subtabs">
          <button class="btn sm active" data-st="pending" onclick="setConflictSubTab('pending')">待裁决 (<span id="cf-cnt-pending">0</span>)</button>
          <button class="btn sm" data-st="deferred" onclick="setConflictSubTab('deferred')">稍后再看 (<span id="cf-cnt-deferred">0</span>)</button>
          <button class="btn sm" data-st="resolved" onclick="setConflictSubTab('resolved')">已裁决 (<span id="cf-cnt-resolved">0</span>)</button>
          <button class="btn sm" data-st="filtered" onclick="setConflictSubTab('filtered')">已筛除误报 (<span id="cf-cnt-filtered">0</span>)</button>
        </div>
        <input type="search" id="conflict-search" placeholder="搜索裁决问题 / 领域 / 原文 / 出处…" style="max-width:250px" oninput="renderConflictsView()">
        <select id="conflict-kind" onchange="renderConflictsView()" style="width:auto;min-width:130px">
          <option value="all">全部类型</option>
          <option value="policy_conflict">🔴 核心原则待拍板</option>
          <option value="rule_proposal">📝 行为规则提案</option>
          <option value="contradiction">⚡ 记忆变更与矛盾</option>
          <option value="staleness">🕰️ 陈旧待核对</option>
        </select>
        <select id="conflict-rec" onchange="renderConflictsView()" style="width:auto;min-width:125px">
          <option value="all">全部建议</option>
          <option value="manual">⚠️ 需本人拍板</option>
          <option value="keep_new">💡 建议采用新说法</option>
          <option value="keep_old">💡 建议沿用旧说法</option>
          <option value="both_valid">🤝 可能不冲突</option>
        </select>
        <select id="conflict-domain" onchange="renderConflictsView()" style="width:auto;min-width:125px">
          <option value="">全部领域</option>
        </select>
        <select id="conflict-page" onchange="renderConflictsView()" style="width:auto;min-width:120px">
          <option value="">全部 Wiki 页</option>
          <option value="preferences">preferences</option>
          <option value="decisions">decisions</option>
          <option value="taboos">taboos</option>
          <option value="verified-fixes">verified-fixes</option>
        </select>
      </div>
      <div id="conflicts-digest-bar" class="conflict-digest-bar"></div>
    </div>
    <div id="conflicts-list"></div>
  </section>

  <!-- 7. MSIM -->
  <section id="panel-msim" class="panel">
    <div class="card" style="margin-bottom:16px">
      <div class="section-title" style="margin-bottom:12px">🏆 M-SIM 跨模型进化排行榜</div>
      <div id="msim-leaderboard"></div>
    </div>
    <div class="card">
      <div class="section-title" style="margin-bottom:12px">🧬 跨模型调优经验卡片</div>
      <div id="msim-entries" class="learned-grid"></div>
    </div>
  </section>

  <!-- 8. ROUTINES -->
  <section id="panel-routines" class="panel">
    <div class="card">
      <div class="section-head">
        <div>
          <div class="section-title">📝 routines.md 规律库编辑器</div>
          <div class="muted" id="routines-meta">保存前自动在 tmp_root/tower-backups/ 下创建 .bak 备份并写入审计日志</div>
        </div>
        <button class="btn primary" onclick="saveRoutines()">💾 保存 routines.md</button>
      </div>
      <textarea id="routines-editor" rows="20" placeholder="# Siming Routines..."></textarea>
      <div class="muted" id="routines-status" style="margin-top:8px"></div>
    </div>
  </section>
</main>

<div id="toast"></div>

<script>
const STATE = {
  overview: null,
  admin: null,
  compute: null,
  computeFilter: 'all',
  computeAutoTimer: null,
  computeWarmRetried: false,
  modules: null,
  modFilter: 'all',
  ops: null,
  opsFilter: 'all',
  finance: null,
  financeCharts: null,
  financeRange: 'last_3m',
  subFilter: 'all',
  replenish: null,
  replenishFilter: 'all',
  smarthome: null,
  wiki: null,
  conflicts: null,
  conflictSubTab: 'pending',
  selectedWikiSlug: '',
  msim: null,
  learned: null,
  graph: null,
  graphApp: 'all',
  selectedNodeId: '',
  graphZoom: 1,
  graphPan: { x: 0, y: 0 },
  graphNodesPos: {},
};

const APP_META = {
  files:    { label: 'Files 笔记',  color: '#38bdf8' },
  mail:     { label: 'Mail 邮件',   color: '#fb923c' },
  calendar: { label: 'Calendar 行程', color: '#a78bfa' },
  chat:     { label: 'Chat/Cases',  color: '#34d399' },
  wiki:     { label: 'Wiki 维基',   color: '#facc15' },
  sure:     { label: 'Sure 账本',   color: '#f472b6' },
  msim:     { label: 'M-SIM 进化',  color: '#60a5fa' },
  learned:  { label: 'Learned 学到', color: '#2dd4bf' },
};

function esc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function showToast(msg, kind='ok') {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.className = kind;
  el.style.display = 'block';
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.style.display = 'none'; }, 3500);
}

const api = async (path, opts={}) => {
  const cur = new URL(window.location.href);
  cur.search = '';
  cur.hash = '';
  let base = cur.toString();
  if (base.endsWith('/index.html')) base = base.slice(0, -'index.html'.length);
  if (!base.endsWith('/')) base += '/';
  const url = new URL(String(path || '').replace(/^\/+/, ''), base).toString();
  const res = await fetch(url, opts);
  const text = await res.text();
  let data = {};
  try { data = text ? JSON.parse(text) : {}; } catch (_) { data = { ok: false, error: text }; }
  if (!res.ok) throw new Error(data.error || data.reply || ('HTTP ' + res.status));
  return data;
};

function toggleTheme() {
  document.body.classList.toggle('light');
  localStorage.setItem('siming_theme', document.body.classList.contains('light') ? 'light' : 'dark');
}
if (localStorage.getItem('siming_theme') === 'light') document.body.classList.add('light');

function switchTab(tabName) {
  const btn = document.querySelector('#tabs .tab[data-tab="' + tabName + '"]');
  const panel = document.getElementById('panel-' + tabName);
  if (!btn || !panel) return;
  document.querySelectorAll('#tabs .tab').forEach(b => b.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  panel.classList.add('active');
  if (window.location.hash !== '#' + tabName) {
    history.replaceState(null, '', '#' + tabName);
  }
  if (tabName === 'graph' && STATE.graph) renderKnowledgeGraph();
}

document.querySelectorAll('#tabs .tab').forEach(btn => {
  btn.addEventListener('click', () => switchTab(btn.dataset.tab));
});

function initTabFromUrl() {
  const qs = new URLSearchParams(window.location.search);
  const t = qs.get('tab') || (window.location.hash || '').replace(/^#/, '');
  if (t) switchTab(t);
}

/* ==================== OVERVIEW ==================== */
function renderOverview() {
  const ov = STATE.overview;
  const ad = STATE.admin;
  if (!ov) return;
  document.getElementById('ts').textContent = '更新于 ' + (ov.generated_at || '').replace('T', ' ').slice(0, 19);
  document.getElementById('tc-modules').textContent = ov.modules.enabled + '/' + ov.modules.total;
  const cg = ov.wave4_personal_os.context_graph || {};
  document.getElementById('tc-graph').textContent = (cg.total_nodes || 0) + 'N';
  document.getElementById('tc-wiki').textContent = ov.wave3_knowledge.wiki_pages || 0;

  const kpis = [
    {
      bar: '', label: '📋 开启待办 / 挂起等待', tab: 'overview',
      val: `${ov.wave0_core.open_cases} / ${ov.wave0_core.active_waits}`,
      sub: `待确认 A2 审批：${ov.wave0_core.pending_approvals} 项`
    },
    {
      bar: 'ok', label: '🎛️ 已启用功能模块', tab: 'modules',
      val: `${ov.modules.enabled} / ${ov.modules.total}`,
      sub: '点击管理所有 Wave 0–4 模块开关'
    },
    {
      bar: 'teal', label: '🎓 司命已学知识', tab: 'graph',
      val: `${ov.wave3_knowledge.learned_total ?? (ov.wave3_knowledge.wiki_pages + ov.wave3_knowledge.msim_entries)}`,
      sub: `Wiki ${ov.wave3_knowledge.wiki_pages} · 假设 ${ov.wave3_knowledge.hypotheses || 0} · M-SIM ${ov.wave3_knowledge.msim_entries} · 收藏 ${ov.wave3_knowledge.bookmarks || 0}`
    },
    {
      bar: 'warn', label: '🗡️ 进行中订阅 / 每月总花费', tab: 'finance',
      val: `${ov.wave1_finance.ongoing_subscriptions ?? ov.wave1_finance.active_subscriptions ?? ov.wave1_finance.subscriptions} 项 · ¥${Number(ov.wave1_finance.monthly_sub_cost_cny || 0).toFixed(0)}/月`,
      sub: `月均 ¥${Number(ov.wave1_finance.monthly_sub_cost_cny || 0).toFixed(2)}/月 · 年化 ¥${Number(ov.wave1_finance.annual_sub_cost_cny || 0).toFixed(0)}/年 · 已停扣 ${ov.wave1_finance.stopped_subscriptions || 0} 项`
    },
    {
      bar: 'purple', label: '🕸️ 跨应用图谱节点 / 边', tab: 'graph',
      val: `${cg.total_nodes || 0} / ${cg.total_edges || 0}`,
      sub: '点击探索交互式联邦知识图谱'
    },
    {
      bar: 'ok', label: '🖥️ Homelab / 行程包', tab: 'smarthome',
      val: `${ov.wave2_smarthome.homelab_ok}/${ov.wave2_smarthome.homelab_total} OK`,
      sub: `行程 ${(ov.wave4_personal_os.trips || []).length} · 众筹尾款预警 ${(ov.wave4_personal_os.crowdfund_alerts || []).length}`
    },
    (() => {
      const cs = (STATE.compute && STATE.compute.summary) || {};
      const poolCnt = cs.pool_count ?? ov.wave1_finance.compute_assets ?? 0;
      const critCnt = Number(cs.critical_count || 0);
      const alertCnt = Number(cs.alert_count || 0);
      const cpuStr = cs.host_cpu_pct != null ? `CPU ${Number(cs.host_cpu_pct).toFixed(0)}%` : 'CPU -';
      const memStr = cs.host_mem_pct != null ? `RAM ${Number(cs.host_mem_pct).toFixed(0)}%` : 'RAM -';
      const balParts = [];
      if (Number(cs.total_balance_cny || 0) > 0) balParts.push('¥' + Number(cs.total_balance_cny).toFixed(0));
      if (Number(cs.total_balance_usd || 0) > 0) balParts.push('$' + Number(cs.total_balance_usd).toFixed(2));
      return {
        bar: critCnt > 0 ? 'warn' : 'teal',
        label: '⚡ 算力资源监控',
        tab: 'compute',
        val: balParts.length ? balParts.join(' · ') : `${poolCnt} 算力池`,
        sub: `${poolCnt} 池 · ${alertCnt > 0 ? alertCnt + ' 告警' : '状态健康'} · ${cpuStr} / ${memStr}`,
      };
    })(),
    (() => {
      const os = (STATE.ops && STATE.ops.summary) || {};
      const st = String(os.overall_status || 'OK');
      const failN = Number(os.timers_failed || 0) + Number(os.crons_failed || 0);
      const pgN = Number(os.push_guard_quiet || 0) + Number(os.push_guard_cooling || 0);
      return {
        bar: st === 'CRITICAL' ? 'warn' : (st === 'WARN' ? 'warn' : 'ok'),
        label: '⚙️ 司命运维与定时调度',
        tab: 'ops',
        val: `${os.schedules_total || 0} 调度 · ${failN > 0 ? failN + ' 异常' : (os.siming_paused ? '已暂停' : '运行中')}`,
        sub: `24h 发 ${os.outbox_sent_24h || 0} / 暂扣 ${os.outbox_held_24h || 0} · 熔断/静默 ${pgN} · 直推QQ ${os.direct_qq_count || 0}`,
      };
    })(),
  ];
  document.getElementById('overview-kpis').innerHTML = kpis.map(k => `
    <div class="card kpi-card" data-tab="${esc(k.tab)}" onclick="switchTab(this.dataset.tab)">
      <div class="kpi-bar ${esc(k.bar)}"></div>
      <div class="kpi-label"><span>${esc(k.label)}</span></div>
      <div class="kpi-value">${esc(k.val)}</div>
      <div class="kpi-sub">${esc(k.sub)}</div>
    </div>`).join('');

  // Live Admin Panel
  const cases = (ad && ad.cases) || [];
  const waits = (ad && ad.waits) || [];
  const approvals = (ad && ad.approvals) || [];

  const casesHtml = cases.length ? `<div class="item-list">${cases.map(c => `
    <div class="item-row">
      <div class="item-main">
        <div class="item-title"><span class="badge accent mono">${esc(c.case_id)}</span> ${esc(c.title)}</div>
        <div class="muted">状态: ${esc(c.status)} · 分类: ${esc(c.category)} ${c.due_at ? '· 截止 ' + esc(String(c.due_at).slice(0,16)) : ''} ${c.next_step ? '· ' + esc(c.next_step) : ''}</div>
      </div>
      <div class="item-actions">
        <button class="btn sm ok" data-case-id="${esc(c.case_id)}" onclick="closeCaseAction(this.dataset.caseId, false)">✅ 销账</button>
        <button class="btn sm err" data-case-id="${esc(c.case_id)}" onclick="closeCaseAction(this.dataset.caseId, true)">✗ 取消</button>
      </div>
    </div>`).join('')}</div>` : '<div class="empty">🎉 当前无开启中的待办事项</div>';

  const waitsHtml = waits.length ? `<div class="item-list">${waits.map(w => `
    <div class="item-row">
      <div class="item-main">
        <div class="item-title"><span class="badge purple mono">${esc(w.wait_id)}</span> ${esc(w.task || w.quote)}</div>
        <div class="muted">触发: ${esc(w.trigger_kind)} (${esc(w.trigger_expr)}) ${w.next_fire_at ? '· 下次 ' + esc(String(w.next_fire_at).slice(0,16).replace('T',' ')) : ''}</div>
      </div>
      <div class="item-actions">
        <button class="btn sm err" data-wait-id="${esc(w.wait_id)}" onclick="cancelWaitAction(this.dataset.waitId)">🚫 取消</button>
      </div>
    </div>`).join('')}</div>` : '<div class="empty">当前无活跃挂起项</div>';

  const appHtml = approvals.length ? `<div class="item-list">${approvals.map(a => `
    <div class="item-row">
      <div class="item-main">
        <div class="item-title"><span class="badge warn mono">${esc(a.token)}</span> ${esc(a.action)} <span class="muted">→ ${esc(a.target_id)}</span></div>
        <div class="muted">等级 ${esc(a.tier)} · 过期 ${esc(String(a.expires_at || '').slice(0,16).replace('T',' '))} · 安全边界：A2 仅在 QQ 确认执行</div>
      </div>
      <div class="item-actions">
        <button class="btn sm err" data-token="${esc(a.token)}" onclick="cancelApprovalAction(this.dataset.token)">🗑️ 作废</button>
      </div>
    </div>`).join('')}</div>` : '<div class="empty">当前无待确认的 A2 审批卡</div>';

  document.getElementById('overview-admin').innerHTML = `
    <div class="card">
      <div class="section-head"><div class="section-title">📋 开启中的待办 (${cases.length})</div><span class="muted">单真源联动 router.close_case</span></div>
      ${casesHtml}
    </div>
    <div class="card">
      <div class="section-head"><div class="section-title">⏳ 活跃挂起等待 (${waits.length})</div><span class="muted">waits.cancel_wait</span></div>
      ${waitsHtml}
      <div class="section-head" style="margin-top:18px"><div class="section-title">🛡️ 待确认 A2 审批 (${approvals.length})</div><span class="badge warn">网页端仅支持作废</span></div>
      ${appHtml}
    </div>`;

  // Trips & crowdfunds strip
  const trips = ov.wave4_personal_os.trips || [];
  const cfs = ov.wave4_personal_os.crowdfund_alerts || [];
  if (trips.length || cfs.length) {
    document.getElementById('overview-extra').innerHTML = `
      <div class="grid-2">
        <div class="card">
          <div class="section-title" style="margin-bottom:10px">✈️ 行程包 (${trips.length})</div>
          ${trips.length ? '<div class="item-list">' + trips.slice(0,5).map(t => `
            <div class="item-row"><div class="item-main">
              <div class="item-title">${esc(t.destination)} (${esc(t.country)}) · ${esc(t.start_date)} ~ ${esc(t.end_date)}</div>
              <div class="muted">${esc(t.kind)} · 状态 ${esc(t.status)} ${t.sure_tag ? '· 标签 ' + esc(t.sure_tag) : ''}</div>
            </div><span class="badge accent">${esc(t.status)}</span></div>`).join('') + '</div>' : '<div class="empty">暂无行程</div>'}
        </div>
        <div class="card">
          <div class="section-title" style="margin-bottom:10px">🎲 桌游众筹尾款预警 (${cfs.length})</div>
          ${cfs.length ? '<div class="item-list">' + cfs.map(cf => `
            <div class="item-row"><div class="item-main">
              <div class="item-title">${esc(cf.title)} (${esc(cf.platform)})</div>
              <div class="muted">尾款 ¥${esc(cf.balance_due_yuan)} · 截止 ${esc(cf.balance_due_at)}</div>
            </div><span class="badge warn">剩 ${esc(cf.days_left)} 天</span></div>`).join('') + '</div>' : '<div class="empty">未来 14 天无众筹尾款到期</div>'}
        </div>
      </div>`;
  } else {
    document.getElementById('overview-extra').innerHTML = '';
  }
  document.getElementById('overview-raw').textContent = JSON.stringify(ov, null, 2);
}

/* ==================== MODULES ==================== */
function setModFilter(f) {
  STATE.modFilter = f;
  ['all','on','off'].forEach(k => {
    const b = document.getElementById('mf-' + k);
    if (b) b.className = 'btn sm' + (k === f ? ' primary' : '');
  });
  renderModulesView();
}

function renderModulesView() {
  const md = STATE.modules;
  if (!md) return;
  const q = (document.getElementById('mod-search').value || '').trim().toLowerCase();
  const f = STATE.modFilter;
  const mods = (md.modules || []).filter(m => {
    if (f === 'on' && !m.enabled) return false;
    if (f === 'off' && m.enabled) return false;
    if (!q) return true;
    const hay = [m.module_id, m.title, m.rule_id, m.description, ...(m.aliases || [])].join(' ').toLowerCase();
    return hay.includes(q);
  });

  document.getElementById('mod-summary-title').textContent = `🎛️ 模块管理（已启用 ${md.enabled_count} / 总计 ${md.total_count}）`;
  const sa = md.sunday_audit || {};
  document.getElementById('mod-audit-bar').innerHTML = `
    <span class="badge accent">🤖 LLM 调用 ${sa.llm_calls || 0} 次</span>
    <span class="badge">Tokens In ${(sa.tokens_in || 0).toLocaleString()} / Out ${(sa.tokens_out || 0).toLocaleString()}</span>
    <span class="badge ok">💰 累计费用 ¥${Number(sa.cost_cny || 0).toFixed(4)}</span>
    <span class="muted">切换右侧开关即时生效（保留历史数据与手动查询）</span>`;

  const waves = new Map();
  mods.forEach(m => {
    const w = m.wave ?? 0;
    if (!waves.has(w)) waves.set(w, { label: m.wave_label || ('Wave ' + w), items: [] });
    waves.get(w).items.push(m);
  });

  const sortedWaves = Array.from(waves.entries()).sort((a,b) => a[0] - b[0]);
  if (!sortedWaves.length) {
    document.getElementById('modules-waves').innerHTML = '<div class="empty">没有匹配当前筛选条件的模块</div>';
  } else {
    document.getElementById('modules-waves').innerHTML = sortedWaves.map(([w, grp]) => {
      const onCnt = grp.items.filter(i => i.enabled).length;
      return `
        <div class="wave-block">
          <div class="wave-header">
            <span>${esc(grp.label)}</span>
            <span class="badge ${onCnt === grp.items.length ? 'ok' : 'accent'}">${onCnt} / ${grp.items.length} 已开启</span>
          </div>
          <div class="mod-grid">
            ${grp.items.map(m => `
              <div class="mod-card ${m.enabled ? '' : 'off'}" id="mod-card-${esc(m.module_id)}">
                <div class="mod-top">
                  <div>
                    <div class="mod-title">${esc(m.title)}</div>
                    <div class="mono muted">${esc(m.module_id)}</div>
                  </div>
                  <label class="ios-switch" title="切换 ${esc(m.title)}">
                    <input type="checkbox" ${m.enabled ? 'checked' : ''} data-module-id="${esc(m.module_id)}" onchange="toggleModule(this.dataset.moduleId, this)">
                    <span class="ios-slider"></span>
                  </label>
                </div>
                <div class="mod-desc">${esc(m.description)}</div>
                <div class="mod-meta">
                  <span class="badge ${m.enabled ? 'ok' : ''}">${m.enabled ? '🟢 开启' : '⚪ 已关'} · ${esc(m.mode)}</span>
                  <span class="badge">14d 推送 ${Number(m.sent_14d || 0)}</span>
                  <span class="badge">最近用 ${esc(m.last_used_at || '-')}</span>
                </div>
              </div>`).join('')}
          </div>
        </div>`;
    }).join('');
  }

  const sources = sa.sources || [];
  document.getElementById('sources-grid').innerHTML = sources.length ? sources.map(s => {
    const cls = s.freshness === 'OK' ? 'ok' : (s.freshness === 'ERROR' ? 'err' : (s.freshness === 'UNCONFIGURED' ? '' : 'warn'));
    return `
      <div class="learned-card">
        <div class="row" style="justify-content:space-between">
          <strong class="mono">${esc(s.id)}</strong>
          <span class="badge ${cls}">${esc(s.freshness)} / ${esc(s.auth_state)}</span>
        </div>
        <div class="muted">${esc(s.detail || s.kind)}</div>
        <div class="muted mono">last_ok: ${esc(s.last_ok_at ? String(s.last_ok_at).slice(0,16).replace('T',' ') : '-')}</div>
      </div>`;
  }).join('') : '<div class="empty">暂无数据源记录</div>';
}

async function toggleModule(moduleId, checkbox) {
  const target = !!checkbox.checked;
  checkbox.disabled = true;
  try {
    const res = await api('/api/modules/' + encodeURIComponent(moduleId) + '/toggle', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ enabled: target })
    });
    showToast(res.summary || (`模块 ${moduleId} 已` + (target ? '开启' : '关闭')), 'ok');
    STATE.modules = await api('/api/modules');
    STATE.overview = await api('/api/overview');
    renderModulesView();
    renderOverview();
  } catch (e) {
    checkbox.checked = !target;
    showToast('切换模块失败：' + e.message, 'err');
  } finally {
    checkbox.disabled = false;
  }
}

/* ==================== KNOWLEDGE GRAPH & LEARNED ==================== */
function setGraphAppFilter(app) {
  STATE.graphApp = app;
  renderKnowledgeGraph();
}

function zoomGraph(factor) {
  STATE.graphZoom = Math.max(0.4, Math.min(2.8, STATE.graphZoom * factor));
  applyGraphTransform();
}

function resetGraphView() {
  STATE.graphZoom = 1;
  STATE.graphPan = { x: 0, y: 0 };
  applyGraphTransform();
}

function applyGraphTransform() {
  const vp = document.getElementById('kg-viewport');
  if (!vp) return;
  const cx = 450, cy = 250;
  vp.setAttribute('transform', `translate(${cx + STATE.graphPan.x} ${cy + STATE.graphPan.y}) scale(${STATE.graphZoom}) translate(${-cx} ${-cy})`);
}

function computeGraphLayout(nodes, edges) {
  const W = 900, H = 500, CX = W / 2, CY = H / 2;
  const apps = Array.from(new Set(nodes.map(n => n.app))).sort();
  const appCenters = {};
  apps.forEach((a, idx) => {
    const ang = (idx / Math.max(1, apps.length)) * Math.PI * 2 - Math.PI / 2;
    const rx = apps.length === 1 ? 0 : 295;
    const ry = apps.length === 1 ? 0 : 165;
    appCenters[a] = { x: CX + Math.cos(ang) * rx, y: CY + Math.sin(ang) * ry };
  });

  // Deduplicate parallel edges for stable force layout
  const pairSet = new Set();
  const uniqueEdges = [];
  edges.forEach(e => {
    const k = e.src_id < e.dst_id ? (e.src_id + '|' + e.dst_id) : (e.dst_id + '|' + e.src_id);
    if (!pairSet.has(k)) {
      pairSet.add(k);
      uniqueEdges.push(e);
    }
  });

  const pos = {};
  const byAppNodes = {};
  nodes.forEach(n => {
    (byAppNodes[n.app] = byAppNodes[n.app] || []).push(n);
  });
  Object.entries(byAppNodes).forEach(([app, list]) => {
    const c = appCenters[app] || { x: CX, y: CY };
    const m = list.length;
    list.forEach((n, idx) => {
      if (m === 1) {
        pos[n.node_id] = { x: c.x, y: c.y, vx: 0, vy: 0 };
      } else {
        const ang = (idx / m) * Math.PI * 2 - Math.PI / 2;
        const r = Math.min(115, 36 + Math.sqrt(m) * 24);
        pos[n.node_id] = { x: c.x + Math.cos(ang) * r, y: c.y + Math.sin(ang) * r * 0.78, vx: 0, vy: 0 };
      }
    });
  });

  const N = nodes.length;
  const iters = N > 80 ? 36 : 60;
  const minSep = N > 45 ? 52 : 76;
  for (let it = 0; it < iters; it++) {
    // Repulsion + hard minimum label separation
    for (let i = 0; i < N; i++) {
      const pi = pos[nodes[i].node_id];
      for (let j = i + 1; j < N; j++) {
        const pj = pos[nodes[j].node_id];
        let dx = pi.x - pj.x, dy = pi.y - pj.y;
        let d2 = dx * dx + dy * dy + 4;
        const d = Math.sqrt(d2);
        let f = 5200 / d2;
        if (d < minSep) f += (minSep - d) * 0.22;
        const fx = (dx / d) * f, fy = (dy / d) * f;
        pi.vx += fx; pi.vy += fy;
        pj.vx -= fx; pj.vy -= fy;
      }
    }
    // Gentle spring attraction along unique edges
    uniqueEdges.forEach(e => {
      const pa = pos[e.src_id], pb = pos[e.dst_id];
      if (!pa || !pb) return;
      const dx = pb.x - pa.x, dy = pb.y - pa.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const target = 210;
      const f = (d - target) * 0.004;
      const fx = (dx / d) * f, fy = (dy / d) * f;
      pa.vx += fx; pa.vy += fy;
      pb.vx -= fx; pb.vy -= fy;
    });
    // Anchor gently toward each app's radial cluster sector
    nodes.forEach(n => {
      const p = pos[n.node_id];
      const c = appCenters[n.app] || { x: CX, y: CY };
      p.vx += (c.x - p.x) * 0.055;
      p.vy += (c.y - p.y) * 0.055;
      p.vx *= 0.65; p.vy *= 0.65;
      p.x = Math.max(66, Math.min(W - 66, p.x + p.vx));
      p.y = Math.max(44, Math.min(H - 48, p.y + p.vy));
    });
  }
  return pos;
}

function renderKnowledgeGraph() {
  const g = STATE.graph;
  if (!g) return;
  const allNodes = g.nodes || [];
  const allEdges = g.edges || [];
  const st = g.stats || {};

  document.getElementById('kg-stats-badge').textContent =
    `${st.visual_nodes || allNodes.length} 节点 · ${st.visual_edges || allEdges.length} 跨应用边` + (g.is_synthetic ? '（实时库预览·建议点击重建索引）' : '');

  // Filter pills
  const counts = {};
  allNodes.forEach(n => { counts[n.app] = (counts[n.app] || 0) + 1; });
  const appKeys = Object.keys(APP_META).filter(a => counts[a] > 0);
  document.getElementById('kg-filters').innerHTML =
    `<button class="app-pill ${STATE.graphApp === 'all' ? 'active' : ''}" data-app="all" onclick="setGraphAppFilter(this.dataset.app)">全部 (${Number(allNodes.length)})</button>` +
    appKeys.map(a => `
      <button class="app-pill ${STATE.graphApp === a ? 'active' : ''}" data-app="${esc(a)}" onclick="setGraphAppFilter(this.dataset.app)">
        <span class="app-dot" style="background:${APP_META[a].color}"></span>${esc(APP_META[a].label)} (${Number(counts[a] || 0)})
      </button>`).join('');

  const nodes = STATE.graphApp === 'all' ? allNodes : allNodes.filter(n => n.app === STATE.graphApp);
  const visSet = new Set(nodes.map(n => n.node_id));
  const edges = allEdges.filter(e => visSet.has(e.src_id) && visSet.has(e.dst_id));

  const edgeLayer = document.getElementById('kg-edges');
  const nodeLayer = document.getElementById('kg-nodes');
  if (!nodes.length) {
    edgeLayer.innerHTML = '';
    nodeLayer.innerHTML = `<text x="450" y="250" text-anchor="middle" fill="#8ea4c0" font-size="14">图谱暂无节点，点击右上角「🔄 重建图谱」索引 7 大应用域</text>`;
    return;
  }

  const pos = computeGraphLayout(nodes, edges);
  STATE.graphNodesPos = pos;

  const sel = STATE.selectedNodeId;
  const neighborSet = new Set();
  if (sel) {
    edges.forEach(e => {
      if (e.src_id === sel) neighborSet.add(e.dst_id);
      if (e.dst_id === sel) neighborSet.add(e.src_id);
    });
  }

  // Deduplicate visual edges between identical node pairs for crisp rendering
  const edgeMap = new Map();
  edges.forEach(e => {
    const k = e.src_id < e.dst_id ? (e.src_id + '|' + e.dst_id) : (e.dst_id + '|' + e.src_id);
    const prev = edgeMap.get(k);
    if (!prev || (e.weight || 0) > (prev.weight || 0)) {
      edgeMap.set(k, { ...e, count: prev ? prev.count + 1 : 1 });
    } else {
      prev.count += 1;
    }
  });
  const drawEdges = Array.from(edgeMap.values());

  edgeLayer.innerHTML = drawEdges.map(e => {
    const a = pos[e.src_id], b = pos[e.dst_id];
    if (!a || !b) return '';
    const hi = sel && (e.src_id === sel || e.dst_id === sel);
    const stroke = hi ? '#58a6ff' : 'rgba(148,163,184,0.28)';
    const sw = hi ? 2.0 : Math.max(0.8, Math.min(1.8, (e.weight || 1) * 0.65));
    const op = sel ? (hi ? 0.92 : 0.12) : 0.55;
    return `<line x1="${a.x.toFixed(1)}" y1="${a.y.toFixed(1)}" x2="${b.x.toFixed(1)}" y2="${b.y.toFixed(1)}" stroke="${stroke}" stroke-width="${sw}" stroke-opacity="${op}"><title>${esc(e.relation)}${e.count > 1 ? ' (×' + e.count + ')' : ''}</title></line>`;
  }).join('');

  nodeLayer.innerHTML = nodes.map(n => {
    const p = pos[n.node_id];
    const meta = APP_META[n.app] || { color: '#94a3b8', label: n.app };
    const r = Math.max(7, Math.min(18, 7 + Math.sqrt(n.degree || 0) * 2.8));
    const isSel = sel === n.node_id;
    const isNb = neighborSet.has(n.node_id);
    const dim = sel && !isSel && !isNb;
    const label = (n.title || '').length > 14 ? (n.title.slice(0, 14) + '…') : (n.title || '');
    return `
      <g class="kg-node" style="cursor:pointer; opacity:${dim ? 0.28 : 1}" data-node-id="${esc(n.node_id)}" onclick="selectGraphNode(this.dataset.nodeId)">
        ${isSel ? `<circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${(r + 6).toFixed(1)}" fill="none" stroke="#58a6ff" stroke-width="2.5" stroke-dasharray="4 3"/>` : ''}
        <circle cx="${p.x.toFixed(1)}" cy="${p.y.toFixed(1)}" r="${r.toFixed(1)}" fill="${meta.color}" stroke="#0b1017" stroke-width="1.6">
          <title>[${esc(meta.label)}] ${esc(n.title)} (连接 ${Number(n.degree || 0)})</title>
        </circle>
        <text x="${p.x.toFixed(1)}" y="${(p.y + r + 12).toFixed(1)}" text-anchor="middle" fill="var(--text)" font-size="10.5" font-weight="${isSel ? '700' : '500'}">${esc(label)}</text>
      </g>`;
  }).join('');

  applyGraphTransform();
  if (!sel && nodes.length) {
    selectGraphNode(nodes[0].node_id, true);
  }
}

function selectGraphNode(nodeId, silentRedraw=false) {
  STATE.selectedNodeId = nodeId;
  const g = STATE.graph;
  if (!g) return;
  const nodeMap = new Map((g.nodes || []).map(n => [n.node_id, n]));
  const n = nodeMap.get(nodeId);
  if (!n) return;
  if (!silentRedraw) renderKnowledgeGraph();

  const meta = APP_META[n.app] || { color: '#94a3b8', label: n.app };
  const connEdges = (g.edges || []).filter(e => e.src_id === nodeId || e.dst_id === nodeId);
  const neighborsHtml = connEdges.length ? connEdges.slice(0, 16).map(e => {
    const otherId = e.src_id === nodeId ? e.dst_id : e.src_id;
    const other = nodeMap.get(otherId);
    if (!other) return '';
    const om = APP_META[other.app] || { color: '#94a3b8', label: other.app };
    return `
      <div class="neighbor-chip" data-node-id="${esc(other.node_id)}" onclick="selectGraphNode(this.dataset.nodeId)">
        <span style="display:flex;align-items:center;gap:6px;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
          <span class="app-dot" style="background:${om.color};flex-shrink:0"></span>
          <span>${esc(other.title)}</span>
        </span>
        <span class="badge mono">${esc(e.relation)}</span>
      </div>`;
  }).join('') : '<div class="muted">暂无直接关联边</div>';

  document.getElementById('kg-inspector').innerHTML = `
    <div class="row" style="justify-content:space-between">
      <span class="badge" style="border-color:${meta.color};color:${meta.color}">● ${esc(meta.label)}</span>
      <span class="badge mono">${esc(n.entity_type)} · 度 ${n.degree || 0}</span>
    </div>
    <div style="font-size:15px;font-weight:700;word-break:break-word">${esc(n.title)}</div>
    <div class="muted mono">出处: ${esc(n.provenance || '-')}</div>
    <div class="muted mono">时间: ${esc(n.occurred_at ? String(n.occurred_at).slice(0,19).replace('T',' ') : '-')}</div>
    ${(n.tags && n.tags.length) ? '<div class="row" style="gap:5px">' + n.tags.slice(0,8).map(t => `<span class="badge">#${esc(t)}</span>`).join('') + '</div>' : ''}
    <div class="inspector-body">${esc(n.body || '（无详细正文）')}</div>
    <div style="font-weight:700;font-size:12.5px;margin-top:4px">🔗 跨应用关联边 (${connEdges.length})</div>
    <div style="display:flex;flex-direction:column;gap:6px">${neighborsHtml}</div>`;
}

// Pan & wheel zoom on SVG
(function bindSvgPanZoom() {
  const svg = document.getElementById('kg-svg');
  let dragging = false, sx = 0, sy = 0, px0 = 0, py0 = 0;
  svg.addEventListener('mousedown', e => {
    if (e.target.closest('.kg-node')) return;
    dragging = true; sx = e.clientX; sy = e.clientY;
    px0 = STATE.graphPan.x; py0 = STATE.graphPan.y;
  });
  window.addEventListener('mousemove', e => {
    if (!dragging) return;
    STATE.graphPan.x = px0 + (e.clientX - sx);
    STATE.graphPan.y = py0 + (e.clientY - sy);
    applyGraphTransform();
  });
  window.addEventListener('mouseup', () => { dragging = false; });
  svg.addEventListener('wheel', e => {
    e.preventDefault();
    zoomGraph(e.deltaY < 0 ? 1.1 : 0.91);
  }, { passive: false });
})();

function renderLearned() {
  const L = STATE.learned;
  if (!L) return;
  const c = L.counts || {};
  document.getElementById('learned-counts-bar').innerHTML = `
    <span class="badge accent">📚 Wiki ${c.wiki_pages || 0}</span>
    <span class="badge purple">💡 假设 ${c.hypotheses || 0}</span>
    <span class="badge ok">🧬 M-SIM ${c.msim_entries || 0}</span>
    <span class="badge warn">❓ 求证 ${c.curiosity_answered || 0}/${c.curiosity_questions || 0}</span>
    <span class="badge">🔖 收藏 ${c.bookmarks || 0}</span>
    <span class="badge">📝 规律 ${c.routines_lines || 0} 条</span>`;

  const wikiHtml = (L.wiki_pages || []).length ? L.wiki_pages.slice(0, 6).map(p => `
    <div class="learned-card clickable" data-slug="${esc(p.slug)}" onclick="openWikiPage(this.dataset.slug)">
      <div class="row" style="justify-content:space-between">
        <strong>📚 ${esc(p.title)}</strong>
        <div class="row" style="gap:5px">
          ${(p.conflicts && p.conflicts.length) ? `<span class="badge warn">⚠️ ${Number(p.conflicts.length)} 冲突</span>` : ''}
          <span class="badge accent">${esc(p.category)}</span>
        </div>
      </div>
      <div class="muted">${esc((p.preview || '').replace(/\n+/g, ' ').slice(0, 120))}</div>
      <div class="muted mono">点击跳转到 Wiki 编辑器 · ${esc(String(p.updated_at || '').slice(0,10))}</div>
    </div>`).join('') : '<div class="empty">尚无共享 Wiki 页面，点击「立即蒸馏共享 Wiki」生成</div>';

  const hypHtml = (L.hypotheses || []).length ? L.hypotheses.slice(0, 6).map(h => `
    <div class="learned-card">
      <div class="row" style="justify-content:space-between">
        <span class="mono muted">${esc(h.id)}</span>
        <span class="badge ${h.status === 'CONFIRMED' ? 'ok' : 'purple'}">${esc(h.status)}</span>
      </div>
      <div style="font-size:13px;font-weight:600">${esc(h.claim)}</div>
      <div class="muted">证据 ${(h.evidence_ids || []).length} 条 · 过期 ${esc(String(h.expires_at || '').slice(0,10))}</div>
    </div>`).join('') : '<div class="empty">暂无观察中的行为假设</div>';

  const msimHtml = (L.msim_entries || []).length ? L.msim_entries.slice(0, 6).map(m => {
    const d = Number(m.delta || 0);
    return `
      <div class="learned-card">
        <div class="row" style="justify-content:space-between">
          <strong>${esc(m.source_model)} → ${esc(m.problem_family)}</strong>
          <span class="badge ${d >= 0 ? 'ok' : 'err'}">${d >= 0 ? '+' : ''}${d.toFixed(2)}</span>
        </div>
        <div class="muted">${esc(m.change_rationale)}</div>
      </div>`;
  }).join('') : '<div class="empty">暂无 M-SIM 跨模型教训记录</div>';

  const curHtml = (L.curiosity_questions || []).length ? L.curiosity_questions.slice(0, 6).map(q => `
    <div class="learned-card">
      <div class="row" style="justify-content:space-between">
        <span class="badge accent">${esc(q.topic)}</span>
        <span class="badge ${q.status === 'ANSWERED' ? 'ok' : 'warn'}">${esc(q.status)}</span>
      </div>
      <div style="font-size:13px;font-weight:600">Q: ${esc(q.question)}</div>
      <div class="muted">${q.answer ? 'A: ' + esc(q.answer) : '等待主人回复确认'}</div>
    </div>`).join('') : '<div class="empty">暂无主动求证问答</div>';

  const bmHtml = (L.bookmarks || []).length ? L.bookmarks.slice(0, 6).map(b => `
    <div class="learned-card">
      <div class="row" style="justify-content:space-between">
        <strong>${esc(b.title)}</strong>
        <span class="badge">${esc(b.kind)}</span>
      </div>
      <div class="muted">${esc(b.summary || b.repo_slug || b.url)}</div>
      <div class="row" style="gap:5px">${(b.tags || []).slice(0,4).map(t => `<span class="badge">#${esc(t)}</span>`).join('')}</div>
    </div>`).join('') : '<div class="empty">暂无收藏夹条目</div>';

  const rt = L.routines || {};
  document.getElementById('learned-body').innerHTML = `
    <div style="margin-bottom:14px"><div class="section-title" style="margin-bottom:8px;font-size:13.5px">📚 Hindsight 共享 Wiki（点击编辑）</div><div class="learned-grid">${wikiHtml}</div></div>
    <div style="margin-bottom:14px"><div class="section-title" style="margin-bottom:8px;font-size:13.5px">💡 行为观察假设 (Hypotheses)</div><div class="learned-grid">${hypHtml}</div></div>
    <div style="margin-bottom:14px"><div class="section-title" style="margin-bottom:8px;font-size:13.5px">🧬 M-SIM 跨模型调优经验</div><div class="learned-grid">${msimHtml}</div></div>
    <div style="margin-bottom:14px"><div class="section-title" style="margin-bottom:8px;font-size:13.5px">❓ 主动求证问答 (Curiosity Q&amp;As)</div><div class="learned-grid">${curHtml}</div></div>
    <div style="margin-bottom:14px"><div class="section-title" style="margin-bottom:8px;font-size:13.5px">🔖 近期收藏夹精选 (Bookmarks)</div><div class="learned-grid">${bmHtml}</div></div>
    <div class="learned-card">
      <div class="row" style="justify-content:space-between">
        <strong>📝 routines.md 规律库预览 (${rt.rule_count || 0} 条规则)</strong>
        <button class="btn sm" onclick="switchTab('routines')">去编辑 routines.md →</button>
      </div>
      <div class="inspector-body" style="max-height:140px">${esc(rt.preview || '（routines.md 尚未创建）')}</div>
    </div>`;
}

async function searchGraph() {
  const q = document.getElementById('graph-q').value.trim();
  if (!q) { showToast('请输入检索关键词', 'err'); return; }
  try {
    const res = await api('/api/graph?q=' + encodeURIComponent(q));
    const card = document.getElementById('graph-result-card');
    card.style.display = 'block';
    const hits = res.hits || [];
    if (!hits.length) {
      document.getElementById('graph-result').innerHTML = `<div class="empty">${esc(res.reply || '未命中相关图谱节点')}</div>`;
      return;
    }
    document.getElementById('graph-result').innerHTML = `
      <div class="muted" style="margin-bottom:10px">共命中 <strong>${Number(res.total_hits || 0)}</strong> 条 · 覆盖应用域：${(res.apps_matched || []).map(a => `<span class="badge accent">${esc(a)}</span>`).join(' ')}</div>
      <div class="item-list">
        ${hits.map(h => {
          const am = APP_META[h.app] || { color: '#58a6ff', label: h.app };
          return `
            <div class="item-row">
              <div class="item-main">
                <div class="item-title">
                  <span class="badge" style="border-color:${am.color};color:${am.color}">${esc(am.label)}</span>
                  ${esc(h.title)}
                </div>
                <div style="font-size:12.5px;margin:4px 0">${esc(h.snippet || '')}</div>
                <div class="muted mono">出处: ${esc(h.provenance)} · 时间: ${esc(String(h.occurred_at || '').slice(0,16))} · 关联节点: ${(h.linked_nodes || []).length}</div>
              </div>
              <button class="btn sm" data-node-id="${esc(h.node_id)}" onclick="selectGraphNode(this.dataset.nodeId)">定位节点</button>
            </div>`;
        }).join('')}
      </div>`;
  } catch (e) {
    showToast('联邦检索失败：' + e.message, 'err');
  }
}

/* ==================== FINANCE ==================== */
function renderFinance() {
  const fin = STATE.finance;
  if (!fin) return;
  const sure = fin.sure || {};
  const ss = fin.sub_summary || {};
  document.getElementById('finance-kpis').innerHTML = `
    <div class="card kpi-card"><div class="kpi-bar"></div><div class="kpi-label">账本总流水 / 总消费</div><div class="kpi-value">${sure.ledger_txn_count || 0} 笔</div><div class="kpi-sub">累计消费 ¥${Number(sure.total_spend_cny_yuan || 0).toLocaleString()}</div></div>
    <div class="card kpi-card"><div class="kpi-bar ok"></div><div class="kpi-label">Sure 同步状态</div><div class="kpi-value">${sure.synced_count || 0} 已同步</div><div class="kpi-sub">排队 ${sure.queued_count || 0} · 规则 ${sure.rules_count || 0} 条</div></div>
    <div class="card kpi-card"><div class="kpi-bar warn"></div><div class="kpi-label">进行中真实订阅</div><div class="kpi-value">${ss.ongoing_count ?? ss.active_count ?? 0} 项</div><div class="kpi-sub">僵尸进行中 ${ss.ongoing_zombie_count ?? ss.zombie_count ?? 0} · 已停扣 ${ss.stopped_count || 0} · 已取消 ${ss.cancelled_count || 0}</div></div>
    <div class="card kpi-card"><div class="kpi-bar purple"></div><div class="kpi-label">💰 每月订阅总花费（进行中）</div><div class="kpi-value">¥${Number(ss.monthly_cost_cny || 0).toFixed(2)} / 月</div><div class="kpi-sub">折合年化 ¥${Number(ss.annual_cost_cny || 0).toFixed(0)}/年 · 已排除已停扣/已取消</div></div>`;

  const assets = fin.compute_assets || [];
  document.getElementById('finance-compute').innerHTML = assets.length ? assets.map(a => {
    const st = String(a.status || 'OK');
    const stCls = st === 'OK' ? 'ok' : (st === 'LOW_BALANCE' || st === 'EXPIRING' ? 'warn' : 'err');
    const daily = Number(a.daily_spend || 0);
    const avg = Math.max(0.01, Number(a.avg_daily_spend || daily || 1));
    const burnRatio = Math.min(100, Math.round((daily / (avg * 2)) * 100));
    const balBar = Math.min(100, Math.max(8, Math.round((Number(a.balance || 0) / 200) * 100)));
    return `
      <div class="learned-card">
        <div class="row" style="justify-content:space-between">
          <strong>${esc(a.provider)} <span class="muted">${esc(a.account_label || '')}</span></strong>
          <span class="badge ${stCls}">${esc(st)}</span>
        </div>
        <div style="font-size:22px;font-weight:700">${esc(a.currency)} ${Number(a.balance || 0).toFixed(2)}</div>
        <div class="prog-track"><div class="prog-fill ${stCls}" style="width:${balBar}%"></div></div>
        <div class="muted">日耗 ${daily.toFixed(2)} / 均耗 ${Number(a.avg_daily_spend || 0).toFixed(2)} · 烧速 ${burnRatio}%</div>
        <div class="row" style="gap:6px">
          <span class="badge">${esc(a.billing_scope || 'personal')}</span>
          ${a.plan_name ? `<span class="badge accent">${esc(a.plan_name)}</span>` : ''}
          ${a.plan_expires_at ? `<span class="muted mono">到期 ${esc(String(a.plan_expires_at).slice(0,10))}</span>` : ''}
        </div>
      </div>`;
  }).join('') : '<div class="empty">暂无算力资产监控记录</div>';

  renderSubsTable();

  const txns = fin.recent_txns || [];
  document.getElementById('finance-txn-meta').textContent = `最近 ${txns.length} 笔`;
  document.getElementById('finance-txns').innerHTML = txns.length ? `
    <table>
      <thead><tr><th>时间</th><th>商户</th><th>金额</th><th>账户 / 卡尾号</th><th>类型</th><th>状态</th></tr></thead>
      <tbody>${txns.map(t => `
        <tr>
          <td class="mono muted">${esc(String(t.occurred_at || '').slice(0,16).replace('T',' '))}</td>
          <td><strong>${esc(t.merchant || '-')}</strong></td>
          <td class="mono"><strong>${esc(t.currency)} ${(Number(t.amount_cents || 0)/100).toFixed(2)}</strong></td>
          <td>${esc(t.account_alias || '-')} ${t.card_last4 ? `<span class="badge mono">${esc(t.card_last4)}</span>` : ''}</td>
          <td>${esc(t.kind)}</td>
          <td><span class="badge ${t.status === 'POSTED' ? 'ok' : ''}">${esc(t.status)}</span></td>
        </tr>`).join('')}</tbody>
    </table>` : '<div class="empty">暂无消费流水</div>';
  renderFinanceCharts();
}

function setSubFilter(filter) {
  STATE.subFilter = filter;
  renderSubsTable();
}

function subMonthlyCostCny(s) {
  if (s && s.monthly_cost_cny != null && Number.isFinite(Number(s.monthly_cost_cny))) {
    return Number(s.monthly_cost_cny);
  }
  const fxMap = { CNY: 1, RMB: 1, USD: 7.25, HKD: 0.93, EUR: 7.85, GBP: 9.20, JPY: 0.048, SGD: 5.40, AUD: 4.75 };
  const amtYuan = Number((s && s.amount_cents) || 0) / 100;
  const cad = Math.max(1, Number((s && s.cadence_days) || 30));
  const ccy = String((s && s.currency) || 'CNY').trim().toUpperCase();
  const fx = fxMap[ccy] || 1;
  const cyclesMap = { 7: 52, 14: 26, 30: 12, 90: 4, 365: 1 };
  const cycles = cyclesMap[cad] || (365 / cad);
  return (amtYuan * fx * cycles) / 12;
}

function renderSubsTable() {
  const fin = STATE.finance;
  if (!fin) return;
  const ss = fin.sub_summary || {};
  const allSubs = fin.subscriptions || [];
  const ongoingList = allSubs.filter(s => !!s.is_ongoing && String(s.status || 'ACTIVE').toUpperCase() !== 'CANCELLED');
  const ongoingZombieList = ongoingList.filter(s => String(s.status || '').toUpperCase() === 'ZOMBIE');
  const stoppedList = allSubs.filter(s => !s.is_ongoing && String(s.status || 'ACTIVE').toUpperCase() !== 'CANCELLED');
  const cancelledList = allSubs.filter(s => String(s.status || '').toUpperCase() === 'CANCELLED');

  let subs = allSubs;
  if (STATE.subFilter === 'ongoing') subs = ongoingList;
  else if (STATE.subFilter === 'ongoing_zombie') subs = ongoingZombieList;
  else if (STATE.subFilter === 'stopped') subs = stoppedList;
  else if (STATE.subFilter === 'cancelled') subs = cancelledList;

  const ongoingMonthly = ss.monthly_cost_cny != null
    ? Number(ss.monthly_cost_cny)
    : ongoingList.reduce((acc, s) => acc + subMonthlyCostCny(s), 0);
  const ongoingAnnual = ss.annual_cost_cny != null
    ? Number(ss.annual_cost_cny)
    : ongoingMonthly * 12;
  const zombieMonthly = ss.zombie_monthly_cost_cny != null
    ? Number(ss.zombie_monthly_cost_cny)
    : ongoingZombieList.reduce((acc, s) => acc + subMonthlyCostCny(s), 0);
  const stoppedMonthly = ss.stopped_monthly_cost_cny != null
    ? Number(ss.stopped_monthly_cost_cny)
    : stoppedList.reduce((acc, s) => acc + subMonthlyCostCny(s), 0);
  const filteredMonthly = subs.reduce((acc, s) => acc + subMonthlyCostCny(s), 0);
  const filteredAnnual = filteredMonthly * 12;

  const sumEl = document.getElementById('finance-subs-summary');
  if (sumEl) {
    const filterNameMap = {
      all: '全部',
      ongoing: '进行中',
      ongoing_zombie: '僵尸进行中',
      stopped: '已停扣',
      cancelled: '已取消',
    };
    const curFilterName = filterNameMap[STATE.subFilter] || '全部';
    sumEl.innerHTML = `
      <div class="subs-cost-strip">
        <div class="subs-cost-main">
          <span class="badge purple">💰 每月订阅总花费</span>
          <span class="subs-cost-val mono">¥${ongoingMonthly.toFixed(2)} <span class="muted" style="font-size:12px;font-weight:500">/ 月</span></span>
          <span class="muted mono">进行中 ${ss.ongoing_count ?? ongoingList.length} 项 · 折合年化 ¥${ongoingAnnual.toFixed(0)}/年</span>
        </div>
        <div class="row" style="gap:6px">
          ${zombieMonthly > 0 ? `<span class="badge warn mono">🧟 僵尸进行中 ¥${zombieMonthly.toFixed(2)}/月</span>` : ''}
          ${stoppedMonthly > 0 ? `<span class="badge ok mono">⏹️ 已停扣省 ¥${stoppedMonthly.toFixed(2)}/月</span>` : ''}
          ${STATE.subFilter !== 'all' && STATE.subFilter !== 'ongoing'
            ? `<span class="badge accent mono">当前筛选「${esc(curFilterName)}」${subs.length}项 · 折合 ¥${filteredMonthly.toFixed(2)}/月</span>`
            : ''}
        </div>
      </div>`;
  }

  const filterEl = document.getElementById('finance-subs-filters');
  if (filterEl) {
    const pills = [
      { id: 'all', label: `全部 (${allSubs.length})` },
      { id: 'ongoing', label: `▶️ 进行中 (${ongoingList.length})` },
      { id: 'ongoing_zombie', label: `🧟 僵尸进行中 (${ongoingZombieList.length})` },
      { id: 'stopped', label: `⏹️ 已停扣 (${stoppedList.length})` },
      { id: 'cancelled', label: `✂️ 已取消 (${cancelledList.length})` },
    ];
    filterEl.innerHTML = pills.map(p => `
      <button class="app-pill ${STATE.subFilter === p.id ? 'active' : ''}" data-sf="${esc(p.id)}" onclick="setSubFilter(this.dataset.sf)">${esc(p.label)}</button>`).join('');
  }

  const statusMeta = {
    ACTIVE: { cls: 'accent', label: 'ACTIVE 正常' },
    KEEP: { cls: 'ok', label: 'KEEP 保留' },
    ZOMBIE: { cls: 'warn', label: 'ZOMBIE 僵尸' },
    CANCELLED: { cls: '', label: 'CANCELLED 已取消' },
  };

  const isOngoingScope = STATE.subFilter === 'all' || STATE.subFilter === 'ongoing';
  const footerLabel = isOngoingScope
    ? `💰 每月订阅总花费（进行中 ${ss.ongoing_count ?? ongoingList.length} 项）`
    : `当前筛选合计（${subs.length} 项）`;
  const footerMonthly = isOngoingScope ? ongoingMonthly : filteredMonthly;
  const footerAnnual = isOngoingScope ? ongoingAnnual : filteredAnnual;
  const footerNote = isOngoingScope
    ? `按账期折算为月均人民币（已排除已停扣/已取消）${zombieMonthly > 0 ? ` · 含僵尸进行中 ¥${zombieMonthly.toFixed(2)}/月` : ''}`
    : `进行中每月订阅总花费：¥${ongoingMonthly.toFixed(2)} / 月（共 ${ss.ongoing_count ?? ongoingList.length} 项 · 年化 ¥${ongoingAnnual.toFixed(0)}/年）`;

  document.getElementById('finance-subs').innerHTML = subs.length ? `
    <table>
      <thead><tr><th>订阅服务</th><th>金额 / 周期</th><th>上次 / 下次扣费</th><th>是否还在进行</th><th>审计标签与提示</th><th>管理操作</th></tr></thead>
      <tbody>${subs.map(s => {
        const amt = (Number(s.amount_cents || 0) / 100).toFixed(2);
        const rowMonthly = subMonthlyCostCny(s);
        const st = String(s.status || 'ACTIVE').toUpperCase();
        const sm = statusMeta[st] || { cls: 'accent', label: st };
        const isOngoing = !!s.is_ongoing && st !== 'CANCELLED';
        const ongoingSrc = String(s.ongoing_source || 'AUTO').toUpperCase() === 'MANUAL' ? '手动' : '自动';
        const ongoingCls = isOngoing ? 'ok' : (st === 'CANCELLED' ? '' : 'warn');
        const ongoingLbl = isOngoing ? '▶️ 进行中' : (st === 'CANCELLED' ? '✂️ 已取消' : '⏹️ 已停扣');
        const hike = Number(s.price_hike_pct || 0);
        const lastStr = s.last_charged_at ? String(s.last_charged_at).slice(0, 10) : '-';
        const daysAgo = s.days_since_last_charge != null ? `${Number(s.days_since_last_charge)}天前` : '';
        const nextStr = isOngoing ? String(s.next_expected_at || '-').slice(0, 10) : '—（已停扣）';
        return `
          <tr class="${isOngoing ? '' : 'sub-row-stopped'}">
            <td><strong>${esc(s.normalized_name || s.merchant)}</strong><div class="muted mono">${esc(s.sub_id)} · ${Number(s.occurrences || 1)}次</div></td>
            <td>
              <strong>${esc(s.currency)} ${amt}</strong> <span class="muted">/ ${Number(s.cadence_days || 30)}天</span>
              <div class="muted mono" style="font-size:11px">折合 ¥${rowMonthly.toFixed(2)}/月</div>
              ${hike > 5 ? `<div><span class="badge err">⚠️ 涨价 +${hike.toFixed(1)}%</span></div>` : ''}
            </td>
            <td class="mono">
              <div>上次 ${esc(lastStr)} ${daysAgo ? `<span class="muted">(${esc(daysAgo)})</span>` : ''}</div>
              <div class="muted">下次 ${esc(nextStr)}</div>
            </td>
            <td>
              <div class="row" style="gap:7px;flex-wrap:nowrap">
                <label class="ios-switch sm" title="${isOngoing ? '点击标为已停扣' : '点击标为进行中'}">
                  <input type="checkbox" ${isOngoing ? 'checked' : ''} data-sub-id="${esc(s.sub_id)}" onchange="toggleSubOngoing(this.dataset.subId, this.checked, this)">
                  <span class="ios-slider"></span>
                </label>
                <span class="badge ${ongoingCls}">${esc(ongoingLbl)}</span>
                <span class="badge">${esc(ongoingSrc)}</span>
              </div>
              ${s.ongoing_reason ? `<div class="muted" style="margin-top:3px">${esc(s.ongoing_reason)}</div>` : ''}
            </td>
            <td><span class="badge ${sm.cls}">${esc(sm.label)}</span> ${s.notes ? `<div class="muted">${esc(s.notes)}</div>` : ''}</td>
            <td>
              <div class="row" style="gap:5px">
                <button class="btn sm ${st === 'ACTIVE' ? 'selected primary' : ''}" data-sub-id="${esc(s.sub_id)}" onclick="markSubStatus(this.dataset.subId,'ACTIVE')">🟢 正常</button>
                <button class="btn sm ok ${st === 'KEEP' ? 'selected' : ''}" data-sub-id="${esc(s.sub_id)}" onclick="markSubStatus(this.dataset.subId,'KEEP')">📌 保留</button>
                <button class="btn sm warn ${st === 'ZOMBIE' ? 'selected' : ''}" data-sub-id="${esc(s.sub_id)}" onclick="markSubStatus(this.dataset.subId,'ZOMBIE')">🧟 僵尸</button>
                <button class="btn sm err ${st === 'CANCELLED' ? 'selected' : ''}" data-sub-id="${esc(s.sub_id)}" onclick="markSubStatus(this.dataset.subId,'CANCELLED')">✂️ 已取消</button>
              </div>
            </td>
          </tr>`;
      }).join('')}</tbody>
      <tfoot>
        <tr class="sub-total-row">
          <td>${esc(footerLabel)}</td>
          <td class="mono"><strong>¥${footerMonthly.toFixed(2)} / 月</strong><div class="muted" style="font-size:11px;font-weight:400">折合 ¥${footerAnnual.toFixed(0)} / 年</div></td>
          <td colspan="4" class="muted" style="font-weight:400">${esc(footerNote)}</td>
        </tr>
      </tfoot>
    </table>` : '<div class="empty">当前筛选下暂无订阅记录（可切换筛选或点击右上角「重扫订阅刺客」）</div>';
}

function fmtYuan(v) {
  const n = Number(v || 0);
  const abs = Math.abs(n);
  if (abs >= 10000) return (n < 0 ? '-' : '') + '¥' + (abs / 10000).toFixed(2) + '万';
  return (n < 0 ? '-¥' : '¥') + abs.toLocaleString('zh-CN', { minimumFractionDigits: 0, maximumFractionDigits: 0 });
}

function setFinanceRange(rng) {
  STATE.financeRange = rng;
  document.querySelectorAll('#finance-range-pills .btn').forEach(b => {
    b.classList.toggle('active', b.dataset.range === rng);
  });
  renderFinanceCharts();
}

function renderFinanceCharts() {
  const fc = STATE.financeCharts;
  const banner = document.getElementById('finance-sure-banner');
  if (!fc || !banner) return;

  // 1. Sure 财务 OS 状态横幅
  const ss = fc.sure_status || {};
  const sureUrl = ss.public_url || ss.local_url || 'http://127.0.0.1:3032';
  banner.innerHTML = ss.running ? `
    <div>
      <div class="row" style="gap:8px;margin-bottom:3px">
        <strong style="font-size:14px">🏦 Sure 财务 OS（自托管）</strong>
        <span class="badge ok">🟢 运行中 (HTTP ${Number(ss.status_code || 200)})</span>
      </div>
      <div class="muted">本机实例已就绪，可对比下方 Tower 内置图表与 Sure 完整财务面板后择一留用</div>
    </div>
    <div class="row" style="gap:8px">
      <a class="btn primary sm" href="${esc(sureUrl)}" target="_blank" rel="noopener">🚀 打开 Sure 面板 (${esc(sureUrl)})</a>
    </div>` : `
    <div>
      <div class="row" style="gap:8px;margin-bottom:3px">
        <strong style="font-size:14px">🏦 Sure 财务 OS（自托管）</strong>
        <span class="badge">⚪ 未部署 / 未运行</span>
      </div>
      <div class="muted">本机 ${esc(ss.local_url || 'http://127.0.0.1:3032')} 当前不可达；可先直接使用下方 Tower 内置 Sure 风格图表</div>
    </div>
    <span class="muted mono">deploy/sure/docker-compose.yml</span>`;

  const s12 = fc.cny_summary_12m || {};
  const subEl = document.getElementById('finance-charts-sub');
  if (subEl) {
    subEl.textContent = `近 12 个月 CNY：收入 ${fmtYuan(s12.income)} · 净支出 ${fmtYuan(s12.net_spend)}（已抵减退款 ${fmtYuan(s12.refunds)}） · 净现金流 ${fmtYuan(s12.net_cashflow)}`;
  }

  // 2. 累计净现金流趋势折线面积图 (SVG)
  const monthly = fc.monthly_cny || [];
  const netEl = document.getElementById('chart-netflow-svg');
  const netBadge = document.getElementById('chart-netflow-badge');
  if (monthly.length && netEl) {
    const W = 540, H = 210, padL = 54, padR = 16, padT = 18, padB = 30;
    const vals = monthly.map(m => Number(m.cumulative_net || 0));
    let minV = Math.min(0, ...vals), maxV = Math.max(0, ...vals);
    if (maxV - minV < 1) { maxV += 100; minV -= 100; }
    const padSpan = (maxV - minV) * 0.1;
    minV -= padSpan; maxV += padSpan;
    const xAt = i => padL + (monthly.length === 1 ? (W - padL - padR) / 2 : (i / (monthly.length - 1)) * (W - padL - padR));
    const yAt = v => padT + ((maxV - v) / (maxV - minV)) * (H - padT - padB);
    const y0 = yAt(0);
    const pts = monthly.map((m, i) => [xAt(i), yAt(Number(m.cumulative_net || 0))]);
    const lineD = pts.map((p, i) => (i === 0 ? 'M' : 'L') + p[0].toFixed(1) + ' ' + p[1].toFixed(1)).join(' ');
    const areaD = lineD + ` L ${pts[pts.length - 1][0].toFixed(1)} ${y0.toFixed(1)} L ${pts[0][0].toFixed(1)} ${y0.toFixed(1)} Z`;
    const lastNet = vals[vals.length - 1] || 0;
    const lineColor = lastNet >= 0 ? '#34d399' : '#60a5fa';
    if (netBadge) netBadge.textContent = `期末累计 ${fmtYuan(lastNet)}`;
    const yTicks = [maxV, (maxV + minV) / 2, minV];
    netEl.innerHTML = `
      <svg class="chart-svg" viewBox="0 0 ${W} ${H}">
        <defs>
          <linearGradient id="nfGrad" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stop-color="${lineColor}" stop-opacity="0.32"/>
            <stop offset="100%" stop-color="${lineColor}" stop-opacity="0.02"/>
          </linearGradient>
        </defs>
        ${yTicks.map(tv => `<line x1="${padL}" y1="${yAt(tv).toFixed(1)}" x2="${W - padR}" y2="${yAt(tv).toFixed(1)}" stroke="var(--border)" stroke-dasharray="3 3" opacity="0.65"/>
          <text x="${padL - 6}" y="${(yAt(tv) + 4).toFixed(1)}" text-anchor="end" fill="var(--muted)" font-size="10">${esc(fmtYuan(tv))}</text>`).join('')}
        <line x1="${padL}" y1="${y0.toFixed(1)}" x2="${W - padR}" y2="${y0.toFixed(1)}" stroke="var(--muted)" stroke-dasharray="4 3" opacity="0.5"/>
        <path d="${areaD}" fill="url(#nfGrad)"/>
        <path d="${lineD}" fill="none" stroke="${lineColor}" stroke-width="2.4" stroke-linejoin="round"/>
        ${monthly.map((m, i) => {
          const [cx, cy] = pts[i];
          const lbl = m.month.slice(5);
          const tip = `${m.month} ｜ 收入 ${fmtYuan(m.income)} · 净支出 ${fmtYuan(m.net_spend)} · 当月净流 ${fmtYuan(m.net_cashflow)} · 12月累计 ${fmtYuan(m.cumulative_net)}`;
          return `<g>
            <circle cx="${cx.toFixed(1)}" cy="${cy.toFixed(1)}" r="4" fill="${lineColor}" stroke="var(--card)" stroke-width="1.5"
              style="cursor:pointer" data-tip="${esc(tip)}" onmouseenter="document.getElementById('chart-netflow-tip').textContent=this.dataset.tip"/>
            ${(i % 2 === 0 || i === monthly.length - 1) ? `<text x="${cx.toFixed(1)}" y="${H - 8}" text-anchor="middle" fill="var(--muted)" font-size="10">${esc(lbl)}月</text>` : ''}
          </g>`;
        }).join('')}
      </svg>`;
  } else if (netEl) {
    netEl.innerHTML = '<div class="empty">暂无近 12 个月 CNY 流水</div>';
  }

  // 3. 月度收入 vs 净支出柱状图 (SVG)
  const barEl = document.getElementById('chart-bar-svg');
  if (monthly.length && barEl) {
    const W = 540, H = 210, padL = 54, padR = 14, padT = 18, padB = 30;
    const maxV = Math.max(100, ...monthly.map(m => Math.max(Number(m.income || 0), Math.max(0, Number(m.net_spend || 0)))));
    const slotW = (W - padL - padR) / monthly.length;
    const bw = Math.max(5, Math.min(14, (slotW - 6) / 2));
    const baseY = H - padB;
    const hOf = v => Math.max(0, (Math.max(0, Number(v || 0)) / maxV) * (baseY - padT));
    const yTicks = [maxV, maxV / 2, 0];
    barEl.innerHTML = `
      <svg class="chart-svg" viewBox="0 0 ${W} ${H}">
        ${yTicks.map(tv => {
          const y = baseY - hOf(tv);
          return `<line x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" stroke="var(--border)" stroke-dasharray="3 3" opacity="0.65"/>
            <text x="${padL - 6}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="var(--muted)" font-size="10">${esc(fmtYuan(tv))}</text>`;
        }).join('')}
        ${monthly.map((m, i) => {
          const cx = padL + i * slotW + slotW / 2;
          const hInc = hOf(m.income);
          const hSpd = hOf(m.net_spend);
          const tip = `${m.month} ｜ 收入 ${fmtYuan(m.income)} · 消费 ${fmtYuan(m.gross_spend)} · 退款 -${fmtYuan(m.refunds)} · 净支出 ${fmtYuan(m.net_spend)}（${m.count}笔）`;
          return `<g style="cursor:pointer" data-tip="${esc(tip)}" onmouseenter="document.getElementById('chart-bar-tip').textContent=this.dataset.tip">
            <rect x="${(cx - bw - 1).toFixed(1)}" y="${(baseY - hInc).toFixed(1)}" width="${bw.toFixed(1)}" height="${hInc.toFixed(1)}" rx="2" fill="#34d399" opacity="0.9"/>
            <rect x="${(cx + 1).toFixed(1)}" y="${(baseY - hSpd).toFixed(1)}" width="${bw.toFixed(1)}" height="${hSpd.toFixed(1)}" rx="2" fill="#f87171" opacity="0.9"/>
            ${(i % 2 === 0 || i === monthly.length - 1) ? `<text x="${cx.toFixed(1)}" y="${H - 8}" text-anchor="middle" fill="var(--muted)" font-size="10">${esc(m.month.slice(5))}月</text>` : ''}
          </g>`;
        }).join('')}
      </svg>`;
  } else if (barEl) {
    barEl.innerHTML = '<div class="empty">暂无月度收支数据</div>';
  }

  // 4. 支出分类占比环形图 (Donut SVG)
  const rng = STATE.financeRange || 'last_3m';
  const rngName = rng === 'current_month' ? `本月 (${fc.current_month || ''})` : (rng === 'last_3m' ? '近 3 个月' : '近 12 个月');
  const dl = document.getElementById('chart-donut-range-lbl');
  const ml = document.getElementById('chart-merch-range-lbl');
  if (dl) dl.textContent = rngName;
  if (ml) ml.textContent = rngName;

  const catWin = (fc.categories && fc.categories[rng]) || { total: 0, items: [] };
  const donutEl = document.getElementById('chart-donut-wrap');
  if (donutEl) {
    const items = catWin.items || [];
    if (!items.length || Number(catWin.total || 0) <= 0) {
      donutEl.innerHTML = '<div class="empty">该区间暂无 CNY 净支出</div>';
    } else {
      const r = 68, cx = 96, cy = 96, circ = 2 * Math.PI * r;
      let acc = 0;
      const segs = items.map(it => {
        const frac = Math.max(0, Number(it.amount || 0)) / Math.max(1, Number(catWin.total || 1));
        const len = frac * circ;
        const offset = -acc;
        acc += len;
        return `<circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="${esc(it.color || '#60a5fa')}" stroke-width="24"
          stroke-dasharray="${len.toFixed(2)} ${(circ - len).toFixed(2)}" stroke-dashoffset="${offset.toFixed(2)}"
          transform="rotate(-90 ${cx} ${cy})"><title>${esc(it.name)}: ${esc(fmtYuan(it.amount))} (${it.pct}%)</title></circle>`;
      }).join('');
      donutEl.innerHTML = `
        <div class="donut-layout">
          <svg width="192" height="192" viewBox="0 0 192 192" style="margin:0 auto;display:block">
            <circle cx="${cx}" cy="${cy}" r="${r}" fill="none" stroke="var(--border)" stroke-width="24" opacity="0.3"/>
            ${segs}
            <text x="${cx}" y="${cy - 4}" text-anchor="middle" fill="var(--text)" font-size="16" font-weight="700">${esc(fmtYuan(catWin.total))}</text>
            <text x="${cx}" y="${cy + 14}" text-anchor="middle" fill="var(--muted)" font-size="11">净支出合计</text>
          </svg>
          <div>
            ${items.map(it => `
              <div class="legend-row">
                <div class="row" style="gap:7px">
                  <span class="legend-dot" style="background:${esc(it.color || '#60a5fa')}"></span>
                  <span>${esc(it.name)}</span>
                  <span class="muted">(${Number(it.count || 0)}笔)</span>
                </div>
                <div class="mono"><strong>${esc(fmtYuan(it.amount))}</strong> <span class="muted">${Number(it.pct || 0).toFixed(1)}%</span></div>
              </div>`).join('')}
          </div>
        </div>`;
    }
  }

  // 5. Top 10 消费商户条形图
  const merchWrap = document.getElementById('chart-merchants-wrap');
  const merchList = (fc.top_merchants && fc.top_merchants[rng]) || [];
  if (merchWrap) {
    if (!merchList.length) {
      merchWrap.innerHTML = '<div class="empty">该区间暂无商户消费记录</div>';
    } else {
      const topAmt = Math.max(1, Number(merchList[0].amount || 1));
      merchWrap.innerHTML = merchList.map((m, idx) => {
        const pct = Math.max(3, Math.round((Number(m.amount || 0) / topAmt) * 100));
        return `
          <div class="merch-row">
            <div class="merch-head">
              <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap"><strong>#${idx + 1}</strong> ${esc(m.merchant)} <span class="muted">(${Number(m.count || 0)}笔)</span></span>
              <span class="mono"><strong>${esc(fmtYuan(m.amount))}</strong></span>
            </div>
            <div class="prog-track"><div class="prog-fill" style="width:${pct}%"></div></div>
          </div>`;
      }).join('');
    }
  }

  // 6. USD 外币独立账本卡片（严禁与 CNY 混加）
  const usdCard = document.getElementById('finance-usd-card');
  const usd = fc.usd || {};
  const otherFx = fc.other_fx || [];
  if (usdCard) {
    const uMonths = (usd.monthly || []).filter(m => Number(m.spend || 0) > 0 || Number(m.repay_transfer || 0) > 0);
    const uMax = Math.max(1, ...uMonths.map(m => Number(m.spend || 0)));
    usdCard.innerHTML = `
      <div class="section-head" style="margin-bottom:8px">
        <div class="section-title" style="font-size:13.5px">💵 USD 外币独立视图（与 CNY 严格隔离 · 含 Nous / 算力信用卡）</div>
        <div class="row" style="gap:6px">
          <span class="badge accent mono">近12月消费 $${Number(usd.spend_12m || 0).toLocaleString('en-US', {maximumFractionDigits:2})}</span>
          <span class="badge mono">累计消费 $${Number(usd.total_all_spend || 0).toLocaleString('en-US', {maximumFractionDigits:2})}</span>
          ${otherFx.map(f => `<span class="badge mono">${esc(f.currency)} ${Number(f.spend || 0).toFixed(2)} (${f.count}笔)</span>`).join('')}
        </div>
      </div>
      ${uMonths.length ? `<div class="grid-3" style="margin-bottom:0">${uMonths.slice(-6).map(m => {
        const pct = Math.max(4, Math.round((Number(m.spend || 0) / uMax) * 100));
        return `<div class="learned-card" style="padding:8px 10px">
          <div class="row" style="justify-content:space-between"><strong class="mono">${esc(m.month)}</strong><span class="mono">$${Number(m.spend || 0).toLocaleString('en-US', {maximumFractionDigits:2})}</span></div>
          <div class="prog-track"><div class="prog-fill" style="width:${pct}%;background:#6366f1"></div></div>
          <div class="muted">${Number(m.count || 0)} 笔${Number(m.repay_transfer || 0) > 0 ? ' · 还款划转 $' + Number(m.repay_transfer).toLocaleString() : ''}</div>
        </div>`;
      }).join('')}</div>` : '<div class="muted">暂无 USD 消费流水</div>'}`;
  }
}

/* ==================== SMARTHOME ==================== */
function renderSmartHome() {
  const sh = STATE.smarthome;
  if (!sh) return;
  const latestAir = (sh.air_readings || [])[0] || null;
  let airHtml = '<div class="empty">暂无新房空气传感器读数</div>';
  if (latestAir) {
    const hcho = Number(latestAir.hcho_mg_m3 || 0);
    const pct = Math.min(1, hcho / 0.20);
    const dashLen = 157;
    const strokeOffset = (dashLen * (1 - pct)).toFixed(1);
    const hchoColor = hcho <= 0.08 ? '#34d399' : (hcho <= 0.15 ? '#f59e0b' : '#f87171');
    const hchoLabel = hcho <= 0.08 ? '达标安全' : (hcho <= 0.15 ? '建议通风' : '超标强排');
    airHtml = `
      <div class="row" style="align-items:center;gap:20px">
        <svg width="160" height="100" viewBox="0 0 160 100" style="flex-shrink:0">
          <path d="M 20 85 A 60 60 0 0 1 140 85" fill="none" stroke="var(--border)" stroke-width="14" stroke-linecap="round"/>
          <path d="M 20 85 A 60 60 0 0 1 140 85" fill="none" stroke="${hchoColor}" stroke-width="14" stroke-linecap="round" stroke-dasharray="${dashLen}" stroke-dashoffset="${strokeOffset}"/>
          <text x="80" y="68" text-anchor="middle" fill="var(--text)" font-size="20" font-weight="700">${hcho.toFixed(3)}</text>
          <text x="80" y="86" text-anchor="middle" fill="var(--muted)" font-size="11">HCHO mg/m³</text>
        </svg>
        <div style="flex:1">
          <div class="row" style="margin-bottom:6px">
            <strong>${esc(latestAir.room || '客厅')}</strong>
            <span class="badge ${hcho <= 0.08 ? 'ok' : (hcho <= 0.15 ? 'warn' : 'err')}">${hchoLabel}</span>
            <span class="badge accent">新风: ${esc(latestAir.fresh_air_mode || 'auto')}</span>
          </div>
          <div class="muted">温度 ${esc(latestAir.temp_c)}°C · 湿度 ${esc(latestAir.humidity_pct)}% · VOC ${esc(latestAir.voc_ppb)} ppb · 室外 AQI ${esc(latestAir.outdoor_aqi)}</div>
          <div class="row" style="margin-top:8px;gap:6px">
            <span class="badge ${latestAir.water_leak ? 'err' : 'ok'}">${latestAir.water_leak ? '🚨 浸水告警' : '💧 无浸水'}</span>
            <span class="badge ${latestAir.gas_leak ? 'err' : 'ok'}">${latestAir.gas_leak ? '🚨 燃气告警' : '🔥 燃气正常'}</span>
            <span class="badge ${latestAir.power_ok ? 'ok' : 'err'}">${latestAir.power_ok ? '⚡ 供电正常' : '🚨 断电'}</span>
          </div>
          <div class="muted mono" style="margin-top:6px">${esc(String(latestAir.recorded_at || '').slice(0,19).replace('T',' '))}</div>
        </div>
      </div>`;
  }

  const ha = sh.haos_night_owl || {};
  const sleepObj = (ha['sleep:state'] && ha['sleep:state'].value) || {};
  const locObj = (ha['loc:me'] && ha['loc:me'].value) || {};
  const teslaObj = (ha['tesla:main'] && ha['tesla:main'].value) || {};
  const sleepState = String(sleepObj.state || sleepObj.status || 'UNKNOWN');
  const vacuumLocked = /sleep|asleep|rest|nap/i.test(sleepState);
  const haHtml = `
    <div class="item-list">
      <div class="item-row">
        <div class="item-main">
          <div class="item-title">${vacuumLocked ? '🔒 扫地机静音锁已激活（主人睡眠中）' : '🔓 扫地机正常通行'}</div>
          <div class="muted">睡眠态: ${esc(sleepState)} · 更新 ${esc(String((ha['sleep:state'] || {}).as_of || '-').slice(0,16))}</div>
        </div>
        <span class="badge ${vacuumLocked ? 'warn' : 'ok'}">${vacuumLocked ? '已锁定' : '未锁定'}</span>
      </div>
      <div class="item-row">
        <div class="item-main">
          <div class="item-title">📍 实时位置情境</div>
          <div class="muted">${esc(locObj.place_alias || locObj.place || '未知')} · ${esc(String((ha['loc:me'] || {}).as_of || '-').slice(0,16))}</div>
        </div>
        <span class="badge accent">${esc(locObj.place_alias || 'N/A')}</span>
      </div>
      <div class="item-row">
        <div class="item-main">
          <div class="item-title">🚗 Tesla Model Y</div>
          <div class="muted">电量 ${esc(teslaObj.battery_level ?? teslaObj.soc ?? '-')}% · 空调 ${teslaObj.is_climate_on ? '开启' : '关闭'}</div>
        </div>
        <span class="badge ok">${esc(teslaObj.battery_level ?? teslaObj.soc ?? '-')}%</span>
      </div>
    </div>`;

  document.getElementById('smarthome-top').innerHTML = `
    <div class="card"><div class="section-title" style="margin-bottom:10px">🌬️ 新房甲醛与 L3 安防守卫</div>${airHtml}</div>
    <div class="card"><div class="section-title" style="margin-bottom:10px">🌙 HAOS 夜猫子联动与扫地机锁</div>${haHtml}</div>`;

  const nodes = sh.homelab_nodes || [];
  document.getElementById('sh-homelab').innerHTML = nodes.length ? nodes.map(n => {
    const cls = n.status === 'OK' ? 'ok' : (n.status === 'WARN' ? 'warn' : 'err');
    return `
      <div class="learned-card">
        <div class="row" style="justify-content:space-between">
          <strong>${esc(n.name)}</strong>
          <span class="badge ${cls}">${esc(n.status)}</span>
        </div>
        <div class="muted">${esc(n.kind)} · ${esc(n.detail || '运行正常')}</div>
        <div class="muted mono">巡检: ${esc(String(n.last_checked_at || '').slice(0,16).replace('T',' '))}</div>
      </div>`;
  }).join('') : '<div class="empty">暂无 Homelab 节点记录</div>';

  const spools = sh.bambu_spools || [];
  const jobs = sh.bambu_jobs || [];
  const spoolHtml = spools.length ? `<div class="grid-3">${spools.map(sp => {
    const initG = Math.max(1, Number(sp.initial_grams || 1000));
    const remG = Math.max(0, Number(sp.remaining_grams || 0));
    const pct = Math.min(100, Math.round((remG / initG) * 100));
    const low = remG <= Number(sp.low_threshold_grams || 180);
    const hum = Number(sp.drybox_humidity_pct || 0);
    return `
      <div class="learned-card">
        <div class="row" style="justify-content:space-between">
          <strong>${esc(sp.brand)} ${esc(sp.material)} · ${esc(sp.color)}</strong>
          <span class="badge ${hum > 40 ? 'warn' : 'ok'}">💧 ${hum.toFixed(0)}%</span>
        </div>
        <div class="prog-track"><div class="prog-fill ${low ? 'err' : 'ok'}" style="width:${pct}%"></div></div>
        <div class="row" style="justify-content:space-between">
          <span class="muted">余量 <strong>${remG.toFixed(0)}g</strong> / ${initG.toFixed(0)}g (${pct}%)</span>
          ${low ? '<span class="badge err">低余量</span>' : ''}
        </div>
      </div>`;
  }).join('')}</div>` : '<div class="empty" style="margin-bottom:12px">暂无拓竹耗材记录</div>';

  const jobsHtml = jobs.length ? `
    <div class="table-wrap"><table>
      <thead><tr><th>打印任务</th><th>状态</th><th>进度</th><th>耗材</th><th>开始时间</th></tr></thead>
      <tbody>${jobs.map(j => `
        <tr>
          <td><strong>${esc(j.name)}</strong></td>
          <td><span class="badge ${j.status === 'DONE' ? 'ok' : (j.status === 'RUNNING' ? 'accent' : 'warn')}">${esc(j.status)}</span></td>
          <td style="min-width:120px"><div class="prog-track"><div class="prog-fill ok" style="width:${Math.min(100, Number(j.progress_pct||0))}%"></div></div><span class="muted mono">${Number(j.progress_pct||0).toFixed(0)}%</span></td>
          <td class="mono">${Number(j.used_grams||0).toFixed(1)}g</td>
          <td class="mono muted">${esc(String(j.started_at || '').slice(0,16).replace('T',' '))}</td>
        </tr>`).join('')}</tbody>
    </table></div>` : '';
  document.getElementById('sh-bambu').innerHTML = spoolHtml + jobsHtml;

  const utils = sh.utilities || [];
  document.getElementById('sh-utilities').innerHTML = utils.length ? `<div class="item-list">${utils.map(u => `
    <div class="item-row">
      <div class="item-main">
        <div class="item-title">${esc(u.kind)} <span class="muted mono">${esc(u.account_no || '')}</span></div>
        <div class="muted">下次应缴: ${esc(String(u.next_due_at || '-').slice(0,10))} · 周期 ${esc(u.cycle_days)}天 ${u.notes ? '· ' + esc(u.notes) : ''}</div>
      </div>
      <span class="badge accent">¥${(Number(u.amount_cents||0)/100).toFixed(2)}</span>
    </div>`).join('')}</div>` : '<div class="empty">暂无水电物业账期</div>';

  const cons = sh.consumables || [];
  document.getElementById('sh-consumables').innerHTML = cons.length ? `<div class="item-list">${cons.map(c => `
    <div class="item-row">
      <div class="item-main">
        <div class="item-title">${esc(c.appliance_name)} · ${esc(c.consumable_name)}</div>
        <div class="muted">下次更换: ${esc(String(c.next_replace_at || '-').slice(0,10))} · 寿命 ${esc(c.lifespan_days)}天</div>
      </div>
      <span class="badge warn">${esc(String(c.next_replace_at || '-').slice(0,10))}</span>
    </div>`).join('')}</div>` : '<div class="empty">暂无家电耗材记录</div>';
}

/* ==================== WIKI ==================== */
function renderWikiSidebar() {
  const wk = STATE.wiki;
  if (!wk) return;
  const q = (document.getElementById('wiki-filter').value || '').trim().toLowerCase();
  const pages = (wk.pages || []).filter(p => !q || (p.title + ' ' + p.slug + ' ' + p.category).toLowerCase().includes(q));
  document.getElementById('wiki-list').innerHTML = pages.length ? pages.map(p => `
    <div class="wiki-item ${STATE.selectedWikiSlug === p.slug ? 'active' : ''}" data-slug="${esc(p.slug)}" onclick="selectWikiPage(this.dataset.slug)">
      <div class="row" style="justify-content:space-between;margin-bottom:3px">
        <strong style="font-size:13px">${esc(p.title)}</strong>
        ${(p.conflicts && p.conflicts.length) ? `<span class="badge warn">⚠️ ${Number(p.conflicts.length)}</span>` : ''}
      </div>
      <div class="muted mono">${esc(p.slug)} · ${esc(p.category)}</div>
    </div>`).join('') : '<div class="empty">暂无匹配 Wiki 页面</div>';
}

function selectWikiPage(slug) {
  const pages = (STATE.wiki && STATE.wiki.pages) || [];
  const p = pages.find(x => x.slug === slug);
  if (!p) return;
  STATE.selectedWikiSlug = p.slug;
  document.getElementById('wiki-slug').value = p.slug;
  document.getElementById('wiki-title').value = p.title;
  document.getElementById('wiki-category').value = p.category || 'general';
  document.getElementById('wiki-content').value = p.content_md || '';
  document.getElementById('wiki-status').textContent = `最近更新：${String(p.updated_at || '').slice(0,19).replace('T',' ')}`;
  const cf = p.conflicts || [];
  document.getElementById('wiki-conflicts').innerHTML = cf.length ? `
    <div class="card" style="border-color:var(--warn)">
      <div class="section-head" style="margin-bottom:8px">
        <div class="section-title" style="color:var(--warn)">⚠️ 本页关联 ${cf.length} 条待裁决冲突</div>
        <button class="btn sm warn" data-slug="${esc(p.slug)}" onclick="openConflictReviewForPage(this.dataset.slug)">⚖️ 去冲突审核台逐条裁决 →</button>
      </div>
      ${cf.map(c => {
        const b = c.decision_brief || {};
        const lbl = b.domain_label || c.topic_label || c.topic || '冲突';
        const qTxt = b.decision_question || c.reason || '';
        return `<div class="muted" style="margin-bottom:6px">• <strong>[${esc(lbl)}]</strong> ${esc(qTxt)}</div>`;
      }).join('')}
    </div>` : '';
  renderWikiSidebar();
}

function openConflictReviewForPage(slug) {
  const sel = document.getElementById('conflict-page');
  if (sel) sel.value = slug || '';
  STATE.conflictSubTab = 'pending';
  switchTab('conflicts');
  renderConflictsView();
}

/* ==================== WIKI CONFLICTS REVIEW QUEUE ==================== */
function setConflictSubTab(st) {
  STATE.conflictSubTab = st;
  document.querySelectorAll('#conflict-subtabs .btn').forEach(b => {
    b.classList.toggle('active', b.dataset.st === st);
  });
  renderConflictsView();
}

const _DEC_LABELS = {
  keep_new: '✅ 已采用新说法',
  keep_old: '📌 已沿用旧说法',
  both_valid: '🤝 两条都留（不冲突）',
  defer: '⏳ 稍后再看',
};

const _KIND_BADGE = {
  policy_conflict: { cls: 'err', label: '🔴 核心原则待拍板' },
  rule_proposal:   { cls: 'accent', label: '📝 行为规则提案' },
  contradiction:   { cls: 'warn', label: '⚡ 记忆变更与矛盾' },
  staleness:       { cls: 'warn', label: '🕰️ 陈旧待核对' },
};

const _REC_BADGE = {
  keep_new:   { cls: 'ok', label: '💡 建议：采用新说法' },
  keep_old:   { cls: 'accent', label: '💡 建议：沿用旧说法' },
  both_valid: { cls: '', label: '🤝 建议：两条都留' },
  manual:     { cls: 'warn', label: '⚠️ 需你亲自拍板' },
};

function quickFilterConflicts(kind, rec) {
  const kEl = document.getElementById('conflict-kind');
  const rEl = document.getElementById('conflict-rec');
  if (kEl && kind !== null) kEl.value = kind;
  if (rEl && rec !== null) rEl.value = rec;
  STATE.conflictSubTab = 'pending';
  renderConflictsView();
}

function renderConflictsView() {
  const data = STATE.conflicts;
  const listEl = document.getElementById('conflicts-list');
  if (!data || !listEl) return;
  const c = data.counts || { pending: 0, deferred: 0, resolved: 0, filtered: 0 };
  const badge = document.getElementById('tc-conflicts');
  if (badge) {
    badge.textContent = String(c.pending || 0);
    badge.classList.toggle('warn-pill', Number(c.pending || 0) > 0);
  }
  const cp = document.getElementById('cf-cnt-pending');
  const cd = document.getElementById('cf-cnt-deferred');
  const cr = document.getElementById('cf-cnt-resolved');
  const cfCnt = document.getElementById('cf-cnt-filtered');
  if (cp) cp.textContent = String(c.pending || 0);
  if (cd) cd.textContent = String(c.deferred || 0);
  if (cr) cr.textContent = String(c.resolved || 0);
  if (cfCnt) cfCnt.textContent = String(c.filtered || 0);

  // 动态更新领域筛选下拉框
  const domSel = document.getElementById('conflict-domain');
  if (domSel) {
    const prevDom = domSel.value;
    const doms = data.domains || [];
    domSel.innerHTML = '<option value="">全部领域</option>' + doms.map(d => `<option value="${esc(d)}">${esc(d)}</option>`).join('');
    if (doms.includes(prevDom)) domSel.value = prevDom;
  }

  // 顶部待裁决速览条
  const digestEl = document.getElementById('conflicts-digest-bar');
  if (digestEl) {
    const bk = c.by_kind || {};
    const br = c.by_recommendation || {};
    digestEl.innerHTML = `
      <span class="muted">本轮总览：</span>
      <button class="btn sm" onclick="quickFilterConflicts('all','all')">全部待裁决 <strong>${Number(c.pending || 0)}</strong></button>
      ${bk.policy_conflict ? `<button class="btn sm" onclick="quickFilterConflicts('policy_conflict','all')">🔴 核心原则待拍板 <strong>${bk.policy_conflict}</strong></button>` : ''}
      ${bk.rule_proposal ? `<button class="btn sm" onclick="quickFilterConflicts('rule_proposal','all')">📝 行为规则提案 <strong>${bk.rule_proposal}</strong></button>` : ''}
      ${bk.contradiction ? `<button class="btn sm" onclick="quickFilterConflicts('contradiction','all')">⚡ 记忆变更 <strong>${bk.contradiction}</strong></button>` : ''}
      ${br.manual ? `<button class="btn sm warn" onclick="quickFilterConflicts('all','manual')">⚠️ 需你拍板 <strong>${br.manual}</strong></button>` : ''}
      ${br.keep_new ? `<button class="btn sm ok" onclick="quickFilterConflicts('all','keep_new')">💡 建议采用新说法 <strong>${br.keep_new}</strong></button>` : ''}
      ${c.filtered ? `<span class="muted" style="margin-left:auto">已自动筛除 <strong>${c.filtered}</strong> 条非冲突误报（可在「已筛除误报」核查）</span>` : ''}
    `;
  }

  const sub = STATE.conflictSubTab || 'pending';
  document.querySelectorAll('#conflict-subtabs .btn').forEach(b => {
    b.classList.toggle('active', b.dataset.st === sub);
  });

  const q = ((document.getElementById('conflict-search') || {}).value || '').trim().toLowerCase();
  const kFilter = (document.getElementById('conflict-kind') || {}).value || 'all';
  const rFilter = (document.getElementById('conflict-rec') || {}).value || 'all';
  const dFilter = (document.getElementById('conflict-domain') || {}).value || '';
  const pFilter = (document.getElementById('conflict-page') || {}).value || '';

  const rawItems = data[sub] || [];
  const items = rawItems.filter(it => {
    const b = it.decision_brief || {};
    if (kFilter !== 'all' && it.kind !== kFilter) return false;
    if (rFilter !== 'all' && (b.recommendation || 'manual') !== rFilter) return false;
    if (dFilter && (b.domain_label || it.topic_label || '') !== dFilter) return false;
    if (pFilter) {
      const slugs = (it.pages || []).map(p => p.slug);
      if (slugs.length && !slugs.includes(pFilter)) return false;
    }
    if (q) {
      const hay = [
        it.conflict_id, it.topic, it.topic_label, it.reason, it.note,
        b.domain_label, b.decision_question, b.conflict_point, b.old_summary, b.new_summary, b.recommendation_reason,
        (it.existing || {}).verbatim, (it.existing || {}).statement, (it.existing || {}).source, (it.existing || {}).source_label,
        (it.incoming || {}).verbatim, (it.incoming || {}).statement, (it.incoming || {}).source, (it.incoming || {}).source_label,
      ].join(' ').toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  if (!items.length) {
    listEl.innerHTML = sub === 'pending'
      ? '<div class="empty">🎉 当前没有待裁决的冲突或待拍板事项！</div>'
      : '<div class="empty">该分类下暂无记录</div>';
    return;
  }

  listEl.innerHTML = items.map(it => {
    const cid = it.conflict_id || '';
    const ex = it.existing || {};
    const inc = it.incoming || {};
    const b = it.decision_brief || {};
    const isResolved = sub === 'resolved';
    const isFiltered = sub === 'filtered';
    const exCls = isResolved ? (it.decision === 'keep_old' ? 'winner' : (it.decision === 'keep_new' ? 'loser' : '')) : '';
    const incCls = isResolved ? (it.decision === 'keep_new' ? 'winner' : (it.decision === 'keep_old' ? 'loser' : '')) : '';
    const km = _KIND_BADGE[it.kind] || _KIND_BADGE.contradiction;
    const recKey = b.recommendation || 'manual';
    const rm = _REC_BADGE[recKey] || _REC_BADGE.manual;
    const domainLabel = b.domain_label || it.topic_label || '日常与工程约定';
    const question = b.decision_question || it.reason || '请确认下面两条记录以哪条为准';
    const point = b.conflict_point || it.reason || '';
    const oldSum = b.old_summary || ex.statement || ex.verbatim || '（无内容）';
    const newSum = b.new_summary || inc.statement || inc.verbatim || '（无内容）';
    const oldFull = ex.verbatim || ex.statement || '（无内容）';
    const newFull = inc.verbatim || inc.statement || '（无内容）';
    const oldDate = String(ex.timestamp || '').slice(0, 10) || '日期未知';
    const newDate = String(inc.timestamp || '').slice(0, 10) || '日期未知';
    const oldSrc = ex.source_label || ex.source || '-';
    const newSrc = inc.source_label || inc.source || '-';
    const oldBtnTxt = b.old_choice_label ? `📌 沿用旧说法：${b.old_choice_label}` : '📌 沿用旧说法';
    const newBtnTxt = b.new_choice_label ? `✅ 采用新说法：${b.new_choice_label}` : '✅ 采用新说法';
    const pagesHtml = (it.pages || []).map(p => `<button class="btn sm" style="padding:2px 7px;font-size:11px" data-slug="${esc(p.slug)}" onclick="openWikiPage(this.dataset.slug)">📄 ${esc(p.slug)}</button>`).join(' ');
    const srcTag = b.brief_source === 'llm' ? '<span class="badge" style="font-size:11px">✨ AI 提炼</span>' : '<span class="badge" style="font-size:11px">⚡ 规则提炼</span>';

    return `
      <div class="conflict-card kind-${esc(it.kind || 'contradiction')} ${isResolved ? 'resolved' : ''} ${isFiltered ? 'filtered' : ''}" id="cf-card-${esc(cid)}">
        <div class="row" style="justify-content:space-between;gap:8px;flex-wrap:wrap">
          <div class="row" style="gap:6px;flex-wrap:wrap">
            <span class="badge ${km.cls}">${km.label}</span>
            <span class="badge accent">${esc(domainLabel)}</span>
            ${(!isResolved && !isFiltered) ? `<span class="badge ${rm.cls}">${rm.label}</span>` : ''}
            ${srcTag}
          </div>
          <div class="row" style="gap:6px;flex-wrap:wrap">
            ${isResolved ? `<span class="badge ok">${esc(_DEC_LABELS[it.decision] || it.decision)}</span>` : ''}
            ${isFiltered ? `<span class="badge">已判定为非冲突误报</span>` : ''}
            ${sub === 'deferred' && it.snooze_until ? `<span class="badge warn">暂缓至 ${esc(String(it.snooze_until).slice(0,10))}</span>` : ''}
          </div>
        </div>

        <div class="cf-question">❓ ${esc(question)}</div>
        ${point ? `<div class="cf-point"><strong>核心分歧：</strong>${esc(point)}</div>` : ''}

        <div class="conflict-sides">
          <div class="conflict-side side-old ${exCls}">
            <div class="row" style="justify-content:space-between;gap:6px">
              <strong style="font-size:12.5px">📌 旧说法 · ${esc(oldDate)}</strong>
              <span class="muted" style="font-size:11.5px">${esc(oldSrc)}</span>
            </div>
            <div class="conflict-stmt">${esc(oldSum)}</div>
          </div>
          <div class="conflict-side side-new ${incCls}">
            <div class="row" style="justify-content:space-between;gap:6px">
              <strong style="font-size:12.5px;color:var(--accent)">🆕 新说法 · ${esc(newDate)}</strong>
              <span class="muted" style="font-size:11.5px">${esc(newSrc)}</span>
            </div>
            <div class="conflict-stmt">${esc(newSum)}</div>
          </div>
        </div>

        ${b.recommendation_reason ? `
          <div class="cf-rec-box ${recKey === 'manual' ? 'rec-manual' : ''}">
            <strong>${isFiltered ? '🧹 筛除理由：' : (recKey === 'manual' ? '⚠️ 裁决提示：' : '💡 建议理由：')}</strong>${esc(b.recommendation_reason)}
          </div>` : ''}

        ${isResolved ? `
          <div class="row" style="justify-content:space-between;gap:8px;flex-wrap:wrap">
            <div class="muted">裁决时间：${esc(String(it.decided_at || '').slice(0,19).replace('T',' '))}${it.note ? ' · 备注：' + esc(it.note) : ''}</div>
            <button class="btn sm" data-cid="${esc(cid)}" onclick="reopenConflictAction(this.dataset.cid)">↩️ 撤销裁决</button>
          </div>` : (isFiltered ? `
          <div class="row" style="justify-content:space-between;gap:8px;flex-wrap:wrap">
            <div class="muted">如果你认为这两条确实有冲突，可一键恢复到「待裁决」队列</div>
            <button class="btn sm warn" data-cid="${esc(cid)}" onclick="restoreFilteredConflictAction(this.dataset.cid)">↩️ 移回待裁决</button>
          </div>` : `
          <div class="conflict-actions">
            <button class="btn sm ok ${recKey === 'keep_new' ? 'rec-hit' : ''}" data-cid="${esc(cid)}" onclick="resolveConflictAction(this.dataset.cid,'keep_new')">${esc(newBtnTxt)}</button>
            <button class="btn sm primary ${recKey === 'keep_old' ? 'rec-hit' : ''}" data-cid="${esc(cid)}" onclick="resolveConflictAction(this.dataset.cid,'keep_old')">${esc(oldBtnTxt)}</button>
            <button class="btn sm ${recKey === 'both_valid' ? 'rec-hit' : ''}" data-cid="${esc(cid)}" onclick="resolveConflictAction(this.dataset.cid,'both_valid')">🤝 两条都留（实际不冲突）</button>
            <button class="btn sm warn" data-cid="${esc(cid)}" onclick="resolveConflictAction(this.dataset.cid,'defer')">⏳ 暂缓 7 天</button>
            <input type="text" class="conflict-note-input" id="cf-note-${esc(cid)}" placeholder="可选备注（记下你的判断依据）…" value="${esc(it.note || '')}">
          </div>`)}

        <details class="cf-evidence">
          <summary>📄 展开完整原文与出处（旧 ${oldFull.length} 字 / 新 ${newFull.length} 字 · 完整未截断）</summary>
          <div class="conflict-sides" style="margin-top:10px">
            <div>
              <div class="muted mono" style="font-size:11px;margin-bottom:4px">旧原文 · ${esc(ex.source || '-')} · ${esc(String(ex.timestamp || '').slice(0,16).replace('T',' '))}</div>
              <div class="cf-raw-text">${esc(oldFull)}</div>
            </div>
            <div>
              <div class="muted mono" style="font-size:11px;margin-bottom:4px">新原文 · ${esc(inc.source || '-')} · ${esc(String(inc.timestamp || '').slice(0,16).replace('T',' '))}</div>
              <div class="cf-raw-text">${esc(newFull)}</div>
            </div>
          </div>
          <div class="row" style="gap:8px;margin-top:6px;flex-wrap:wrap">
            <span class="muted mono" style="font-size:11px">编号：${esc(cid)} · 内部主题：${esc(it.topic || '-')}</span>
            ${pagesHtml}
          </div>
        </details>
      </div>`;
  }).join('');
}

async function summarizeConflictsAction() {
  const btn = document.getElementById('btn-cf-summarize');
  if (btn) { btn.disabled = true; btn.textContent = '🧠 正在提炼裁决点…'; }
  try {
    const res = await api('/api/wiki/conflicts/summarize', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ force: true })
    });
    const st = res.stats || {};
    showToast(`已完成提炼：AI 提炼 ${st.llm_updated || 0} 条，筛除误报 ${st.filtered_noise || 0} 条`, 'ok');
    const [cf, wk] = await Promise.all([api('/api/wiki/conflicts'), api('/api/wiki')]);
    STATE.conflicts = cf;
    STATE.wiki = wk;
    renderConflictsView();
    renderWikiSidebar();
  } catch (e) {
    showToast('提炼失败：' + e.message, 'err');
  } finally {
    if (btn) { btn.disabled = false; btn.textContent = '🧠 AI 重提炼裁决点'; }
  }
}

async function restoreFilteredConflictAction(cid) {
  try {
    await api('/api/wiki/conflicts/' + encodeURIComponent(cid) + '/restore', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
    showToast(`已将 ${cid} 恢复至「待裁决」队列`, 'ok');
    const [cf, wk] = await Promise.all([api('/api/wiki/conflicts'), api('/api/wiki')]);
    STATE.conflicts = cf;
    STATE.wiki = wk;
    renderConflictsView();
    renderWikiSidebar();
  } catch (e) {
    showToast('恢复失败：' + e.message, 'err');
  }
}

async function resolveConflictAction(cid, decision) {
  const noteEl = document.getElementById('cf-note-' + cid);
  const note = noteEl ? noteEl.value.trim() : '';
  try {
    const res = await api('/api/wiki/conflicts/' + encodeURIComponent(cid) + '/resolve', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ decision, note })
    });
    showToast(`已裁决：${_DEC_LABELS[decision] || decision}`, 'ok');
    const [cf, wk] = await Promise.all([api('/api/wiki/conflicts'), api('/api/wiki')]);
    STATE.conflicts = cf;
    STATE.wiki = wk;
    renderConflictsView();
    renderWikiSidebar();
    if (STATE.selectedWikiSlug) selectWikiPage(STATE.selectedWikiSlug);
  } catch (e) {
    showToast('裁决失败：' + e.message, 'err');
  }
}

async function reopenConflictAction(cid) {
  try {
    await api('/api/wiki/conflicts/' + encodeURIComponent(cid) + '/reopen', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
    showToast(`已撤销裁决 ${cid}，点「重新蒸馏并刷新」可还原到待裁决`, 'ok');
    STATE.conflicts = await api('/api/wiki/conflicts');
    renderConflictsView();
  } catch (e) {
    showToast('撤销失败：' + e.message, 'err');
  }
}

function openWikiPage(slug) {
  switchTab('wiki');
  selectWikiPage(slug);
}

function newWikiPage() {
  STATE.selectedWikiSlug = '';
  document.getElementById('wiki-slug').value = '';
  document.getElementById('wiki-title').value = '';
  document.getElementById('wiki-category').value = 'general';
  document.getElementById('wiki-content').value = '';
  document.getElementById('wiki-conflicts').innerHTML = '';
  document.getElementById('wiki-status').textContent = '新建页面模式：填写 slug、标题与内容后点击保存';
  renderWikiSidebar();
}

/* ==================== MSIM ==================== */
function renderMsim() {
  const ms = STATE.msim;
  if (!ms) return;
  const lb = ms.leaderboard || [];
  document.getElementById('msim-leaderboard').innerHTML = lb.length ? lb.map((r, i) => {
    const pct = Math.max(4, Math.min(100, Math.round(Number(r.avg_score || 0) * 100)));
    const delta = Number(r.avg_delta || 0);
    return `
      <div style="margin-bottom:12px">
        <div class="row" style="justify-content:space-between;margin-bottom:4px">
          <div><strong>#${i + 1} ${esc(r.source_model)}</strong> <span class="muted">(${Number(r.runs || 0)} 次实验 · 通过率 ${Number(r.pass_rate || 0)}%)</span></div>
          <div class="row" style="gap:8px">
            <span class="badge ${delta >= 0 ? 'ok' : 'err'}">Δ ${delta >= 0 ? '+' : ''}${delta.toFixed(3)}</span>
            <strong class="mono">${Number(r.avg_score).toFixed(3)}</strong>
          </div>
        </div>
        <div class="prog-track"><div class="prog-fill ${i === 0 ? 'ok' : ''}" style="width:${pct}%"></div></div>
      </div>`;
  }).join('') : '<div class="empty">暂无 M-SIM 模型排行记录</div>';

  const entries = ms.entries || [];
  document.getElementById('msim-entries').innerHTML = entries.length ? entries.map(e => {
    const delta = Number(e.delta ?? ((e.measured_score || 0) - (e.baseline_score || 0)));
    return `
      <div class="learned-card">
        <div class="row" style="justify-content:space-between">
          <strong>${esc(e.source_model)} → ${esc(e.problem_family)}</strong>
          <span class="badge ${delta >= 0 ? 'ok' : 'err'}">${delta >= 0 ? '+' : ''}${delta.toFixed(3)}</span>
        </div>
        <div class="muted mono">${esc(e.target_artifact || '-')}</div>
        <div style="font-size:13px">${esc(e.change_rationale)}</div>
        ${e.expected_outcome ? `<div class="muted">预期收益: ${esc(e.expected_outcome)}</div>` : ''}
        <div class="row" style="gap:6px;margin-top:auto">
          <span class="badge ${e.passed ? 'ok' : 'err'}">${e.passed ? '✅ PASS' : '❌ FAIL'}</span>
          <span class="badge mono">${Number(e.baseline_score||0).toFixed(2)} → ${Number(e.measured_score||0).toFixed(2)}</span>
          ${(e.tags || []).slice(0,4).map(t => `<span class="badge">#${esc(t)}</span>`).join('')}
        </div>
      </div>`;
  }).join('') : '<div class="empty">暂无 M-SIM 进化日志</div>';
}

/* ==================== ACTIONS ==================== */
async function closeCaseAction(caseId, cancel) {
  try {
    const res = await api('/api/cases/' + encodeURIComponent(caseId) + '/close', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ cancel: !!cancel, note: 'tower' })
    });
    showToast(res.summary || `待办 ${caseId} 已更新`, 'ok');
    await refreshAll();
  } catch (e) { showToast('操作失败：' + e.message, 'err'); }
}

async function cancelWaitAction(waitId) {
  try {
    await api('/api/waits/' + encodeURIComponent(waitId) + '/cancel', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ reason: 'tower_cancel' })
    });
    showToast(`挂起项 ${waitId} 已取消`, 'ok');
    await refreshAll();
  } catch (e) { showToast('取消失败：' + e.message, 'err'); }
}

async function cancelApprovalAction(token) {
  try {
    const res = await api('/api/approvals/' + encodeURIComponent(token) + '/cancel', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
    showToast(res.reply || `审批 ${token} 已作废`, 'ok');
    await refreshAll();
  } catch (e) { showToast('作废失败：' + e.message, 'err'); }
}

async function markSubStatus(subId, status) {
  try {
    const res = await api('/api/subs/' + encodeURIComponent(subId) + '/status', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ status })
    });
    showToast(`订阅 ${res.normalized_name || subId} 已标记为 ${res.status}（${res.ongoing_label || (res.is_ongoing ? '进行中' : '已停扣')}）`, 'ok');
    STATE.finance = await api('/api/finance');
    STATE.overview = await api('/api/overview');
    renderFinance();
    renderOverview();
  } catch (e) { showToast('标记订阅失败：' + e.message, 'err'); }
}

async function toggleSubOngoing(subId, isOngoing, checkbox) {
  if (checkbox) checkbox.disabled = true;
  try {
    const res = await api('/api/subs/' + encodeURIComponent(subId) + '/status', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ is_ongoing: !!isOngoing })
    });
    showToast(`订阅 ${res.normalized_name || subId} 已标为「${res.is_ongoing ? '进行中' : '已停扣'}」`, 'ok');
    STATE.finance = await api('/api/finance');
    STATE.overview = await api('/api/overview');
    renderFinance();
    renderOverview();
  } catch (e) {
    if (checkbox) checkbox.checked = !isOngoing;
    showToast('切换订阅进行状态失败：' + e.message, 'err');
  } finally {
    if (checkbox) checkbox.disabled = false;
  }
}

async function runGraphReindex() {
  showToast('正在重建跨应用图谱索引…', 'ok');
  try {
    const res = await api('/api/graph/reindex', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
    const st = res.stats || {};
    showToast(`图谱重建完成：${st.total_nodes || 0} 节点 / ${st.total_edges || 0} 边`, 'ok');
    await refreshAll();
  } catch (e) { showToast('图谱重建失败：' + e.message, 'err'); }
}

async function runWikiDistill() {
  showToast('正在蒸馏共享 Wiki…', 'ok');
  try {
    const res = await api('/api/wiki/distill', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
    showToast(`共享 Wiki 蒸馏完成：处理 ${res.facts_extracted ?? res.total_facts ?? 0} 条事实`, 'ok');
    await refreshAll();
  } catch (e) { showToast('Wiki 蒸馏失败：' + e.message, 'err'); }
}

async function runSubsRescan() {
  showToast('正在重扫订阅刺客…', 'ok');
  try {
    const res = await api('/api/subs/rescan', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
    const cnt = res.active_count ?? res.detected_count ?? (res.scan && (res.scan.detected ?? res.scan.detected_count)) ?? (res.subscriptions || []).length;
    const purgedCnt = (res.purged && res.purged.purged_subscriptions) || 0;
    showToast(`订阅扫描完成：当前 ${cnt} 项订阅` + (purgedCnt ? `（已清理误报 ${purgedCnt} 项）` : ''), 'ok');
    await refreshAll();
  } catch (e) { showToast('订阅扫描失败：' + e.message, 'err'); }
}

async function saveRoutines() {
  const content = document.getElementById('routines-editor').value;
  try {
    const res = await api('/api/routines', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ content })
    });
    document.getElementById('routines-status').textContent = '✅ 已保存（备份：' + res.backup_path + '）';
    showToast('routines.md 已保存并自动备份', 'ok');
  } catch (e) { showToast('保存失败：' + e.message, 'err'); }
}

async function saveWiki() {
  const slug = document.getElementById('wiki-slug').value.trim();
  const title = document.getElementById('wiki-title').value.trim();
  const category = document.getElementById('wiki-category').value.trim() || 'general';
  const content_md = document.getElementById('wiki-content').value;
  if (!slug) { showToast('请先填写页面 slug', 'err'); return; }
  try {
    const res = await api('/api/wiki/' + encodeURIComponent(slug), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ title, category, content_md })
    });
    STATE.selectedWikiSlug = res.slug;
    document.getElementById('wiki-status').textContent = '✅ 已保存页面 ' + res.slug + '（备份：' + res.backup_path + '）';
    showToast('Wiki 页面已保存：' + res.slug, 'ok');
    await refreshAll();
  } catch (e) { showToast('保存 Wiki 失败：' + e.message, 'err'); }
}

/* ==================== COMPUTE RESOURCE MONITOR ==================== */
function fmtTokens(n) {
  const v = Number(n || 0);
  if (v >= 1e9) return (v / 1e9).toFixed(2) + 'B';
  if (v >= 1e6) return (v / 1e6).toFixed(2) + 'M';
  if (v >= 1e3) return (v / 1e3).toFixed(1) + 'K';
  return String(Math.round(v));
}

function fmtUptime(sec) {
  const s = Math.max(0, Math.floor(Number(sec || 0)));
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `运行 ${d}天 ${h}时 ${m}分`;
  if (h > 0) return `运行 ${h}时 ${m}分`;
  return `运行 ${m}分`;
}

function setComputeFilter(f) {
  STATE.computeFilter = f;
  document.querySelectorAll('#compute-filter-pills .btn').forEach(b => {
    b.classList.toggle('active', b.dataset.cf === f);
  });
  renderCompute();
}

function toggleComputeAssetForm() {
  const card = document.getElementById('compute-asset-form-card');
  if (!card) return;
  card.style.display = card.style.display === 'none' ? 'block' : 'none';
}

function toggleComputeAutoRefresh(enabled) {
  if (STATE.computeAutoTimer) {
    clearInterval(STATE.computeAutoTimer);
    STATE.computeAutoTimer = null;
  }
  if (enabled) {
    STATE.computeAutoTimer = setInterval(async () => {
      const panel = document.getElementById('panel-compute');
      if (!panel || !panel.classList.contains('active')) return;
      try {
        STATE.compute = await api('/api/compute');
        renderCompute();
        renderOverview();
      } catch (_) {}
    }, 15000);
    showToast('已开启算力监控 15 秒自动刷新', 'ok');
  }
}

function renderCompute() {
  const cm = STATE.compute;
  if (!cm) return;
  const s = cm.summary || {};
  const pools = cm.pools || [];
  const alerts = cm.alerts || [];
  const host = cm.host || {};
  const services = cm.services || [];
  const tel = cm.usage_telemetry || {};
  const magU = tel.magpie || {};
  const omniU = tel.omniroute || {};
  const simU = tel.siming || {};
  const spend = cm.ledger_spend || {};
  const rts = cm.runtimes || {};
  const rjobs = cm.remote_jobs || {};

  // Tab badge
  const tc = document.getElementById('tc-compute');
  if (tc) {
    if (Number(s.critical_count || 0) > 0) {
      tc.textContent = String(s.critical_count) + '!';
      tc.classList.add('err-pill');
      tc.classList.remove('warn-pill');
    } else if (Number(s.alert_count || 0) > 0) {
      tc.textContent = String(s.alert_count);
      tc.classList.add('warn-pill');
      tc.classList.remove('err-pill');
    } else {
      tc.textContent = String(s.pool_count || 0);
      tc.classList.remove('warn-pill', 'err-pill');
    }
  }

  // Toolbar status & services
  const cacheMap = { live: '🟢 实时', cached: '🟢 缓存(60s)', warming: '⏳ 后台同步中…', offline: '⚪ 离线模式' };
  const cb = document.getElementById('compute-cache-badge');
  if (cb) {
    cb.textContent = cacheMap[cm.cache_state] || String(cm.cache_state || '-');
    cb.className = 'badge ' + (cm.cache_state === 'live' || cm.cache_state === 'cached' ? 'ok' : (cm.cache_state === 'warming' ? 'warn' : ''));
  }
  const subTs = document.getElementById('compute-sub-ts');
  if (subTs) {
    subTs.textContent = `采集于 ${String(cm.generated_at || '').replace('T', ' ').slice(0, 19)} · 主机 ${host.hostname || '-'} · 台账 ${s.db_asset_count || 0} 项 / 监控池 ${s.pool_count || 0} 个`;
  }
  const svcBar = document.getElementById('compute-services-bar');
  if (svcBar) {
    svcBar.innerHTML = services.map(sv => `
      <span class="svc-pill" title="${esc(sv.endpoint)} · ${esc(sv.detail || '')}">
        <span class="svc-dot ${sv.running ? 'on' : 'off'}"></span>
        <span>${esc(sv.name)}</span>
      </span>`).join('');
  }

  // 1. Top KPIs
  const balCny = Number(s.total_balance_cny || 0);
  const balUsd = Number(s.total_balance_usd || 0);
  const balHeadline = (balCny > 0 || balUsd > 0)
    ? `${balCny > 0 ? '¥' + balCny.toLocaleString('zh-CN', {maximumFractionDigits:2}) : ''}${balCny > 0 && balUsd > 0 ? ' · ' : ''}${balUsd > 0 ? '$' + balUsd.toLocaleString('en-US', {maximumFractionDigits:2}) : ''}`
    : '¥0.00';
  const primaryGpu = (host.gpus || [])[0] || null;
  const kpiCards = [
    {
      bar: 'ok',
      label: '💰 API 实时余额（币种隔离）',
      val: balHeadline,
      sub: `CNY ¥${balCny.toFixed(2)} ｜ USD $${balUsd.toFixed(2)} · 台账 ${s.db_asset_count || 0} 项`,
    },
    {
      bar: Number(s.critical_count || 0) > 0 ? 'warn' : 'purple',
      label: '🎟️ 算力监控池 / 告警',
      val: `${s.pool_count || 0} 池 / ${s.alert_count || 0} 警`,
      sub: `严重 ${s.critical_count || 0} · 熔断 ${s.circuit_open_count || 0} · Nous ${cm.nous_sentinel && cm.nous_sentinel.is_locked ? '🚨付款锁' : '🟢正常'}`,
    },
    {
      bar: '',
      label: '📡 24h AI 调用 / Token 吞吐',
      val: `${Number(s.requests_24h || 0).toLocaleString()} 次`,
      sub: `Token ${fmtTokens(s.tokens_24h || 0)} · 成功率 ${Number(s.success_rate_24h_pct || 100).toFixed(1)}% · 7天 ${Number(magU.requests_7d || 0).toLocaleString()} 次`,
    },
    {
      bar: 'teal',
      label: '🖥️ 本机 CPU / 内存 / GPU',
      val: `${s.host_cpu_pct != null ? Number(s.host_cpu_pct).toFixed(0) + '%' : '-'} / ${s.host_mem_pct != null ? Number(s.host_mem_pct).toFixed(0) + '%' : '-'}`,
      sub: primaryGpu
        ? `GPU ${primaryGpu.util_pct != null ? primaryGpu.util_pct + '%' : '-'} · ${primaryGpu.temp_c != null ? primaryGpu.temp_c + '°C' : '-'} · ${primaryGpu.power_w != null ? primaryGpu.power_w + 'W' : ''}`
        : `CPU ${s.host_cpu_temp_c != null ? s.host_cpu_temp_c + '°C' : '-'} · RAM ${Number(s.host_mem_used_gib || 0).toFixed(1)}/${Number(s.host_mem_total_gib || 0).toFixed(1)} GiB`,
    },
  ];
  document.getElementById('compute-kpis').innerHTML = kpiCards.map(k => `
    <div class="card kpi-card" style="cursor:default">
      <div class="kpi-bar ${esc(k.bar)}"></div>
      <div class="kpi-label"><span>${esc(k.label)}</span></div>
      <div class="kpi-value">${esc(k.val)}</div>
      <div class="kpi-sub">${esc(k.sub)}</div>
    </div>`).join('');

  // 2. Alerts Strip
  const alEl = document.getElementById('compute-alerts');
  if (alEl) {
    alEl.innerHTML = alerts.length ? alerts.map(a => `
      <div class="compute-alert-item ${a.severity === 'critical' ? 'critical' : ''}">
        <div>
          <span class="badge ${a.severity === 'critical' ? 'err' : 'warn'}">${a.severity === 'critical' ? '🚨 严重' : '⚠️ 预警'}</span>
          <strong style="margin-left:6px">${esc(a.title)}</strong>
          ${a.detail ? `<span class="muted" style="margin-left:6px">${esc(a.detail)}</span>` : ''}
        </div>
        <span class="badge mono">${esc(a.category)}</span>
      </div>`).join('') : '';
  }

  // 3. Pools Grid
  const cf = STATE.computeFilter || 'all';
  const filteredPools = pools.filter(p => {
    if (cf === 'subscription') return p.kind === 'subscription' || p.kind === 'hybrid' || (p.windows && p.windows.length > 0);
    if (cf === 'balance') return p.balance != null;
    if (cf === 'alert') return p.status !== 'OK';
    return true;
  });
  const poolsEl = document.getElementById('compute-pools');
  if (poolsEl) {
    poolsEl.innerHTML = filteredPools.length ? filteredPools.map(p => {
      const st = String(p.status || 'OK');
      const stCls = st === 'OK' ? 'ok' : (st === 'WARN' || st === 'LOW_BALANCE' || st === 'EXPIRING' ? 'warn' : (st === 'UNAVAILABLE' ? '' : 'err'));
      const cardCls = st === 'CIRCUIT_OPEN' ? 'st-circuit' : (st === 'CRITICAL' ? 'st-critical' : (st === 'WARN' || st === 'LOW_BALANCE' ? 'st-warn' : ''));
      const fmtPct = v => {
        const n = Math.round(Number(v) * 10) / 10;
        return Number.isInteger(n) ? String(n) : n.toFixed(1);
      };
      const winsHtml = (p.windows || []).map(w => {
        const hasRem = w.remaining_pct != null || w.used_pct != null;
        const used = w.used_pct != null ? Math.max(0, Math.min(100, Number(w.used_pct))) : (w.remaining_pct != null ? Math.max(0, Math.min(100, 100 - Number(w.remaining_pct))) : null);
        const rem = w.remaining_pct != null ? Math.max(0, Math.min(100, Number(w.remaining_pct))) : (used != null ? Math.max(0, Math.min(100, 100 - used)) : null);
        const barCls = w.severity === 'critical' ? 'err' : (w.severity === 'warn' ? 'warn' : 'ok');
        const rawDisp = String(w.display || '').trim();
        const usedText = rawDisp.includes('/') ? rawDisp : (used != null ? fmtPct(used) + '%' : '');
        let valHtml = `<span class="muted">${esc(rawDisp || '未知')}</span>`;
        if (hasRem && rem != null) {
          if (rem <= 0) {
            valHtml = `<strong style="color:var(--err)">余量 0% · 已耗尽</strong>${usedText ? ` <span class="muted">(已用 ${esc(usedText)})</span>` : ''}`;
          } else {
            valHtml = `<strong style="color:${barCls === 'warn' ? 'var(--warn)' : 'var(--ok)'}">余量 ${fmtPct(rem)}%</strong>${usedText ? ` <span class="muted">(已用 ${esc(usedText)})</span>` : ''}`;
          }
        }
        const trackCls = (hasRem && rem != null && rem <= 0) ? 'prog-track exhausted' : 'prog-track';
        const barWidth = (hasRem && rem != null) ? rem : 0;
        return `
          <div class="win-row">
            <div class="win-head">
              <span>${esc(w.name)}</span>
              <span class="mono">${valHtml}</span>
            </div>
            <div class="${trackCls}"><div class="prog-fill ${barCls}" style="width:${barWidth}%"></div></div>
            ${w.detail ? `<div class="muted" style="font-size:11px">${esc(w.detail)}</div>` : ''}
          </div>`;
      }).join('');
      const resetsHtml = p.reset_credits && p.reset_credits.count
        ? `<div class="muted" style="font-size:11.5px">🎫 持有 <strong>${Number(p.reset_credits.count)}</strong> 张满血重置券${(p.reset_credits.next_expires_local || p.reset_credits.next_expires_at) ? ' · 最近到期 ' + esc(String(p.reset_credits.next_expires_local || p.reset_credits.next_expires_at).slice(0, 16)) : ''}</div>`
        : '';
      const notesHtml = (p.extra_notes || []).length
        ? `<div class="muted" style="font-size:11.5px">${(p.extra_notes || []).map(n => esc(n)).join(' · ')}</div>`
        : '';
      const errHtml = p.error ? `<div class="muted" style="font-size:11.5px;color:var(--warn)">${esc(p.error)}</div>` : '';
      const spendHtml = (Number(p.daily_spend || 0) > 0 || Number(p.avg_daily_spend || 0) > 0)
        ? `<div class="muted mono" style="font-size:11.5px">日耗 ${Number(p.daily_spend || 0).toFixed(2)} / 均耗 ${Number(p.avg_daily_spend || 0).toFixed(2)}</div>`
        : '';
      return `
        <div class="pool-card ${cardCls}">
          <div class="row" style="justify-content:space-between;align-items:flex-start">
            <div>
              <strong>${esc(p.display_name || p.provider)}</strong>
              ${p.plan ? `<span class="badge accent" style="margin-left:5px">${esc(p.plan)}</span>` : ''}
              <div class="muted mono" style="font-size:11px">${esc(p.provider)} · ${esc(p.account_label || 'default')} · ${esc(p.source || '')}${p.stale ? ' (缓存)' : ''}</div>
            </div>
            <span class="badge ${stCls}">${esc(st)}</span>
          </div>
          ${p.balance_display ? `<div class="pool-balance mono">${esc(p.balance_display)}</div>` : ''}
          ${winsHtml}
          ${resetsHtml}
          ${spendHtml}
          ${notesHtml}
          ${errHtml}
          <div class="row" style="justify-content:space-between;margin-top:auto;padding-top:4px;border-top:1px solid rgba(148,163,184,0.12)">
            <div class="row" style="gap:5px">
              <span class="badge">${esc(p.billing_scope === 'work_cw08' ? '公费CW08' : '个人')}</span>
              ${p.plan_expires_at ? `<span class="muted mono">到期 ${esc(String(p.plan_expires_at).slice(0,10))}</span>` : ''}
            </div>
            <div class="row" style="gap:5px">
              ${(p.billing_url && String(p.billing_url).startsWith('https://')) ? `<a class="btn sm" href="${esc(p.billing_url)}" target="_blank" rel="noopener noreferrer">🔗 账单页</a>` : ''}
              <button class="btn sm ${p.circuit_open ? 'ok' : 'err'}" data-provider="${esc(p.provider)}" data-open="${p.circuit_open ? '0' : '1'}" onclick="toggleProviderCircuit(this.dataset.provider, this.dataset.open === '1')">
                ${p.circuit_open ? '✅ 恢复路由' : '🔌 熔断'}
              </button>
            </div>
          </div>
        </div>`;
    }).join('') : '<div class="empty">当前筛选条件下暂无算力监控池，可点击右上角「🔄 刷新并同步实时算力」或「➕ 登记/更新账户」</div>';
  }

  // 4. Local Host Hardware Telemetry
  const cpu = host.cpu || {};
  const mem = host.memory || {};
  const gpus = host.gpus || [];
  const disks = host.disks || [];
  const upEl = document.getElementById('compute-host-uptime');
  if (upEl) upEl.textContent = `${host.hostname || 'localhost'} · ${fmtUptime(host.uptime_sec || 0)}`;
  const hwEl = document.getElementById('compute-hardware');
  if (hwEl) {
    const cpuPct = cpu.usage_pct != null ? Number(cpu.usage_pct) : 0;
    const cpuCls = cpuPct >= 85 ? 'err' : (cpuPct >= 65 ? 'warn' : 'ok');
    const memPct = Number(mem.used_pct || 0);
    const memCls = memPct >= 88 ? 'err' : (memPct >= 72 ? 'warn' : 'ok');
    const swPct = Number(mem.swap_used_pct || 0);

    const gpuBoxes = gpus.map(g => {
      const gBusy = g.util_pct != null ? Number(g.util_pct) : 0;
      const gVram = Number(g.vram_used_pct || 0);
      return `
        <div class="hw-box">
          <div class="row" style="justify-content:space-between">
            <strong>🎮 ${esc(g.name || 'GPU')}</strong>
            ${g.temp_c != null ? `<span class="badge ${Number(g.temp_c) >= 80 ? 'err' : 'ok'}">${esc(g.temp_c)}°C</span>` : ''}
          </div>
          <div class="hw-big">${g.util_pct != null ? esc(g.util_pct) + '%' : 'N/A'} <span class="muted" style="font-size:12px;font-weight:500">${g.power_w != null ? esc(g.power_w) + 'W' : ''}${g.sclk_mhz ? ' · ' + esc(g.sclk_mhz) + 'MHz' : ''}</span></div>
          <div class="prog-track"><div class="prog-fill ${gBusy >= 85 ? 'warn' : 'ok'}" style="width:${Math.min(100, gBusy)}%"></div></div>
          <div class="muted mono">VRAM ${Number(g.vram_used_gib || 0).toFixed(2)} / ${Number(g.vram_total_gib || 0).toFixed(2)} GiB (${gVram.toFixed(0)}%)${g.gtt_total_gib ? ' · GTT ' + Number(g.gtt_used_gib || 0).toFixed(2) + '/' + Number(g.gtt_total_gib).toFixed(1) + ' GiB' : ''}</div>
          <div class="prog-track"><div class="prog-fill" style="width:${Math.min(100, gVram)}%"></div></div>
        </div>`;
    }).join('');

    const diskBoxes = disks.map(dk => {
      const dp = Number(dk.used_pct || 0);
      return `
        <div class="hw-box">
          <div class="row" style="justify-content:space-between">
            <strong>💾 磁盘 ${esc(dk.mount)}</strong>
            ${dk.temp_c != null ? `<span class="badge ok">NVMe ${esc(dk.temp_c)}°C</span>` : ''}
          </div>
          <div class="hw-big">${dp.toFixed(1)}%</div>
          <div class="prog-track"><div class="prog-fill ${dp >= 88 ? 'err' : (dp >= 75 ? 'warn' : 'ok')}" style="width:${Math.min(100, dp)}%"></div></div>
          <div class="muted mono">已用 ${Number(dk.used_gib || 0).toFixed(1)} GiB / 总计 ${Number(dk.total_gib || 0).toFixed(1)} GiB · 剩余 ${Number(dk.free_gib || 0).toFixed(1)} GiB</div>
        </div>`;
    }).join('');

    hwEl.innerHTML = `
      <div class="hw-box">
        <div class="row" style="justify-content:space-between">
          <strong>🧠 CPU (${Number(cpu.cores_logical || 0)} 线程)</strong>
          ${cpu.temp_c != null ? `<span class="badge ${Number(cpu.temp_c) >= 85 ? 'err' : (Number(cpu.temp_c) >= 75 ? 'warn' : 'ok')}">${esc(cpu.temp_c)}°C</span>` : ''}
        </div>
        <div class="hw-big">${cpu.usage_pct != null ? cpuPct.toFixed(1) + '%' : '-'}</div>
        <div class="prog-track"><div class="prog-fill ${cpuCls}" style="width:${Math.min(100, cpuPct)}%"></div></div>
        <div class="muted mono">Load ${Number(cpu.load_1m || 0).toFixed(2)} / ${Number(cpu.load_5m || 0).toFixed(2)} / ${Number(cpu.load_15m || 0).toFixed(2)}</div>
        <div class="muted" style="font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="${esc(cpu.model || '')}">${esc(cpu.model || '-')}</div>
      </div>
      <div class="hw-box">
        <div class="row" style="justify-content:space-between">
          <strong>🧩 内存 &amp; Swap</strong>
          <span class="badge ${memCls}">${memPct.toFixed(1)}%</span>
        </div>
        <div class="hw-big">${Number(mem.used_gib || 0).toFixed(1)} <span class="muted" style="font-size:13px">/ ${Number(mem.total_gib || 0).toFixed(1)} GiB</span></div>
        <div class="prog-track"><div class="prog-fill ${memCls}" style="width:${Math.min(100, memPct)}%"></div></div>
        <div class="muted mono">可用 ${Number(mem.available_gib || 0).toFixed(1)} GiB · 缓存 ${Number(mem.cached_gib || 0).toFixed(1)} GiB</div>
        <div class="muted mono">Swap ${Number(mem.swap_used_gib || 0).toFixed(1)} / ${Number(mem.swap_total_gib || 0).toFixed(1)} GiB (${swPct.toFixed(0)}%)</div>
      </div>
      ${gpuBoxes}
      ${diskBoxes}`;
  }

  // Top Processes
  const procs = host.top_processes || [];
  const procsEl = document.getElementById('compute-procs');
  if (procsEl) {
    procsEl.innerHTML = procs.length ? `
      <table>
        <thead><tr><th>PID</th><th>进程</th><th>CPU%</th><th>MEM%</th><th>RSS</th></tr></thead>
        <tbody>${procs.map(pr => `
          <tr>
            <td class="mono muted">${Number(pr.pid || 0)}</td>
            <td title="${esc(pr.cmd || '')}"><strong>${esc(pr.comm)}</strong><div class="muted mono" style="font-size:11px;max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${esc(pr.cmd || '')}</div></td>
            <td class="mono"><strong>${Number(pr.cpu_pct || 0).toFixed(1)}%</strong></td>
            <td class="mono">${Number(pr.mem_pct || 0).toFixed(1)}%</td>
            <td class="mono">${Number(pr.rss_mb || 0).toFixed(0)} MB</td>
          </tr>`).join('')}</tbody>
      </table>` : '<div class="empty">暂无进程遥测</div>';
  }

  // Remote GPU Handoffs & Task Runs
  const handoffs = rjobs.overnight_handoffs || [];
  const truns = rjobs.task_runs || [];
  const remEl = document.getElementById('compute-remote');
  if (remEl) {
    const hoHtml = handoffs.length ? handoffs.map(h => {
      const m = h.metrics || {};
      const mBits = ['step', 'epoch', 'loss', 'val_loss', 'acc', 'eval_score'].filter(k => m[k] != null).map(k => `${k}=${m[k]}`);
      const hCls = h.status === 'DONE' ? 'ok' : (h.status === 'FAILED' ? 'err' : 'accent');
      return `
        <div class="item-row">
          <div class="item-main">
            <div class="item-title"><span class="badge accent mono">${esc(h.handoff_id)}</span> ${esc(h.name)} <span class="muted mono">@ ${esc(h.host)}</span></div>
            <div class="muted mono">${mBits.length ? esc(mBits.join(' · ')) : '等待日志指标'} · 更新 ${esc(String(h.updated_at || '').slice(0,16).replace('T',' '))}</div>
          </div>
          <span class="badge ${hCls}">${esc(h.status)}</span>
        </div>`;
    }).join('') : '';
    const trHtml = truns.slice(0, 6).map(tr => {
      const tCls = tr.status === 'DONE' ? 'ok' : (tr.status === 'RUNNING' ? 'accent' : (tr.status === 'BLOCKED' ? 'warn' : 'err'));
      return `
        <div class="item-row">
          <div class="item-main">
            <div class="item-title"><span class="badge mono">${esc(tr.run_id)}</span> ${esc(tr.name || tr.target_pane || tr.kind)}</div>
            <div class="muted mono">${esc(tr.kind)} · ${esc(String(tr.started_at || '').slice(0,16).replace('T',' '))}${tr.exit_code != null ? ' · exit=' + Number(tr.exit_code) : ''}</div>
          </div>
          <span class="badge ${tCls}">${esc(tr.status)}</span>
        </div>`;
    }).join('');
    remEl.innerHTML = (hoHtml || trHtml)
      ? `<div class="item-list">${hoHtml}${trHtml}</div>`
      : '<div class="empty">当前无在途远端 GPU 交班或长任务（可用 siming handoff add / siming run 登记）</div>';
  }

  // 5. Hourly AI Gateway Traffic Chart + Models Table
  const hourly = magU.hourly_24h || [];
  const hSvg = document.getElementById('compute-hourly-svg');
  const tMeta = document.getElementById('compute-traffic-meta');
  if (tMeta) {
    tMeta.textContent = `Magpie 24h ${Number(magU.requests_24h || 0)} 次 (${fmtTokens(magU.tokens_total_24h || 0)} tok · 均延迟 ${Number(magU.avg_latency_ms_24h || 0)}ms) ｜ OmniRoute 24h ${Number(omniU.requests_24h || 0)} 次 ｜ Siming 内部 ${Number(simU.calls || 0)} 次`;
  }
  if (hSvg) {
    const hasTraffic = hourly.some(h => Number(h.requests || 0) > 0);
    if (!hourly.length || !hasTraffic) {
      hSvg.innerHTML = '<div class="empty">近 24 小时暂无网关调用记录</div>';
    } else {
      const W = 540, H = 175, padL = 42, padR = 12, padT = 16, padB = 26;
      const maxR = Math.max(1, ...hourly.map(h => Number(h.requests || 0)));
      const slotW = (W - padL - padR) / hourly.length;
      const bw = Math.max(6, slotW - 4);
      const baseY = H - padB;
      const hOf = v => Math.max(0, (Number(v || 0) / maxR) * (baseY - padT));
      const yTicks = [maxR, Math.round(maxR / 2), 0];
      hSvg.innerHTML = `
        <svg class="chart-svg" viewBox="0 0 ${W} ${H}">
          ${yTicks.map(tv => {
            const y = baseY - hOf(tv);
            return `<line x1="${padL}" y1="${y.toFixed(1)}" x2="${W - padR}" y2="${y.toFixed(1)}" stroke="var(--border)" stroke-dasharray="3 3" opacity="0.6"/>
              <text x="${padL - 5}" y="${(y + 4).toFixed(1)}" text-anchor="end" fill="var(--muted)" font-size="10">${tv}</text>`;
          }).join('')}
          ${hourly.map((h, i) => {
            const x = padL + i * slotW + (slotW - bw) / 2;
            const bh = hOf(h.requests);
            const hasErr = Number(h.errors || 0) > 0;
            const fill = hasErr ? '#f59e0b' : '#58a6ff';
            const tip = `${h.hour} ｜ 请求 ${Number(h.requests || 0)} 次 · 失败 ${Number(h.errors || 0)} 次 · Token ${fmtTokens(h.tokens || 0)}`;
            return `<g style="cursor:pointer" data-tip="${esc(tip)}" onmouseenter="document.getElementById('compute-hourly-tip').textContent=this.dataset.tip">
              <rect x="${x.toFixed(1)}" y="${(baseY - bh).toFixed(1)}" width="${bw.toFixed(1)}" height="${Math.max(bh, Number(h.requests || 0) > 0 ? 2 : 0).toFixed(1)}" rx="2" fill="${fill}" opacity="0.9"/>
              ${(i % 4 === 0 || i === hourly.length - 1) ? `<text x="${(x + bw / 2).toFixed(1)}" y="${H - 7}" text-anchor="middle" fill="var(--muted)" font-size="9.5">${esc(String(h.hour || '').slice(6, 11))}</text>` : ''}
            </g>`;
          }).join('')}
        </svg>`;
    }
  }

  const models = magU.by_model || [];
  const modEl = document.getElementById('compute-models-table');
  if (modEl) {
    modEl.innerHTML = models.length ? `
      <table>
        <thead><tr><th>模型</th><th>厂商</th><th>24h/7d 次</th><th>Token 总量</th><th>均延迟</th><th>成功率</th></tr></thead>
        <tbody>${models.map(m => `
          <tr>
            <td class="mono"><strong>${esc(m.model)}</strong></td>
            <td><span class="badge">${esc(m.provider)}</span></td>
            <td class="mono">${Number(m.requests_24h || 0)} / ${Number(m.requests_7d || 0)}</td>
            <td class="mono">${fmtTokens(m.tokens_total || 0)}</td>
            <td class="mono muted">${Number(m.avg_latency_ms || 0).toFixed(0)}ms</td>
            <td><span class="badge ${Number(m.success_rate_pct || 0) >= 95 ? 'ok' : (Number(m.success_rate_pct || 0) >= 80 ? 'warn' : 'err')}">${Number(m.success_rate_pct || 0).toFixed(0)}%</span></td>
          </tr>`).join('')}</tbody>
      </table>` : '<div class="empty">近 7 天暂无网关模型调用明细</div>';
  }

  // 6. Monthly AI Compute Ledger Spend (CNY vs USD strictly isolated)
  const spEl = document.getElementById('compute-spend-body');
  if (spEl) {
    const months = spend.monthly || [];
    const maxCny = Math.max(1, ...months.map(m => Number(m.cny || 0)));
    const maxUsd = Math.max(1, ...months.map(m => Number(m.usd || 0)));
    const vUsd = spend.vendors_usd || [];
    const vCny = spend.vendors_cny || [];
    spEl.innerHTML = `
      <div class="row" style="gap:8px;margin-bottom:12px">
        <span class="badge accent mono">当月 (${esc(spend.current_month || '-')}) USD $${Number(spend.current_month_usd || 0).toLocaleString('en-US', {maximumFractionDigits:2})}</span>
        <span class="badge ok mono">当月 CNY ¥${Number(spend.current_month_cny || 0).toLocaleString('zh-CN', {maximumFractionDigits:2})}</span>
        <span class="badge mono">近6月合计 USD $${Number(spend.total_6m_usd || 0).toLocaleString('en-US', {maximumFractionDigits:0})} · CNY ¥${Number(spend.total_6m_cny || 0).toLocaleString('zh-CN', {maximumFractionDigits:0})}</span>
      </div>
      ${months.length ? `<div class="item-list" style="margin-bottom:14px">${months.map(m => {
        const wUsd = Math.round((Number(m.usd || 0) / maxUsd) * 100);
        const wCny = Math.round((Number(m.cny || 0) / maxCny) * 100);
        return `
          <div class="learned-card" style="padding:8px 11px">
            <div class="row" style="justify-content:space-between">
              <strong class="mono">${esc(m.month)}</strong>
              <div class="row mono" style="gap:10px">
                <span style="color:#a78bfa">USD $${Number(m.usd || 0).toLocaleString('en-US', {maximumFractionDigits:2})}</span>
                <span style="color:#34d399">CNY ¥${Number(m.cny || 0).toLocaleString('zh-CN', {maximumFractionDigits:2})}</span>
              </div>
            </div>
            ${Number(m.usd || 0) > 0 ? `<div class="prog-track"><div class="prog-fill" style="width:${Math.max(3, wUsd)}%;background:#8b5cf6"></div></div>` : ''}
            ${Number(m.cny || 0) > 0 ? `<div class="prog-track"><div class="prog-fill ok" style="width:${Math.max(3, wCny)}%"></div></div>` : ''}
          </div>`;
      }).join('')}</div>` : '<div class="empty">账本暂无 AI 算力支出记录</div>'}
      <div class="grid-2" style="margin-bottom:0">
        <div>
          <div class="muted" style="font-weight:700;margin-bottom:6px">💵 USD 厂商汇总（近6月）</div>
          ${vUsd.length ? vUsd.map(v => `<div class="legend-row"><span>${esc(v.vendor)} <span class="muted">(${Number(v.count)}笔)</span></span><strong class="mono">$${Number(v.amount).toLocaleString('en-US', {maximumFractionDigits:2})}</strong></div>`).join('') : '<div class="muted">无 USD 算力支出</div>'}
        </div>
        <div>
          <div class="muted" style="font-weight:700;margin-bottom:6px">💴 CNY 厂商汇总（近6月）</div>
          ${vCny.length ? vCny.map(v => `<div class="legend-row"><span>${esc(v.vendor)} <span class="muted">(${Number(v.count)}笔)</span></span><strong class="mono">¥${Number(v.amount).toLocaleString('zh-CN', {maximumFractionDigits:2})}</strong></div>`).join('') : '<div class="muted">无 CNY 算力支出</div>'}
        </div>
      </div>`;
  }

  // 7. Linger/Pi Multi-Runtime Routing
  const rtProfiles = rts.profiles || [];
  const rtPickEl = document.getElementById('compute-runtime-pick');
  if (rtPickEl && rts.active_heavy && rts.active_light) {
    rtPickEl.textContent = `当前重型编码首选：${rts.active_heavy.runtime} (${rts.active_heavy.model}) ｜ 轻量分拣首选：${rts.active_light.runtime} (${rts.active_light.model})`;
  }
  const rtEl = document.getElementById('compute-runtimes');
  if (rtEl) {
    const heavyName = (rts.active_heavy || {}).runtime || '';
    const lightName = (rts.active_light || {}).runtime || '';
    rtEl.innerHTML = rtProfiles.length ? rtProfiles.map(p => `
      <div class="learned-card">
        <div class="row" style="justify-content:space-between">
          <div>
            <strong class="mono">${esc(p.name)}</strong>
            ${p.name === heavyName ? '<span class="badge accent" style="margin-left:4px">重型首选</span>' : ''}
            ${p.name === lightName ? '<span class="badge purple" style="margin-left:4px">轻量首选</span>' : ''}
          </div>
          <span class="badge ${p.circuit_open ? 'err' : 'ok'}">${p.circuit_open ? '🛑 CIRCUIT_OPEN' : '🟢 READY'}</span>
        </div>
        <div class="muted mono">${esc(p.provider)} · ${esc(p.model)} · ${esc(p.cost_tier)}</div>
        <div class="row" style="justify-content:space-between;margin-top:auto">
          <div class="row" style="gap:4px">${(p.best_for || []).slice(0,3).map(b => `<span class="badge">${esc(b)}</span>`).join('')}</div>
          <button class="btn sm ${p.circuit_open ? 'ok' : 'err'}" data-provider="${esc(p.provider)}" data-open="${p.circuit_open ? '0' : '1'}" onclick="toggleProviderCircuit(this.dataset.provider, this.dataset.open === '1')">
            ${p.circuit_open ? '✅ 恢复' : '🔌 熔断'}
          </button>
        </div>
      </div>`).join('') : '<div class="empty">暂无多 Runtime 画像</div>';
  }
}

async function refreshComputeLive() {
  const btn = document.getElementById('btn-compute-refresh');
  if (btn) btn.disabled = true;
  showToast('正在拉取 Magpie / ai-usagebar / ai-quota 实时算力并同步台账…', 'ok');
  try {
    const res = await api('/api/compute/refresh', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
    STATE.compute = res;
    const [fin, ov] = await Promise.all([api('/api/finance'), api('/api/overview')]);
    STATE.finance = fin;
    STATE.overview = ov;
    renderCompute();
    renderFinance();
    renderOverview();
    const synced = (res.sync_result && res.sync_result.synced) || 0;
    showToast(`算力刷新完成：监控 ${(res.summary || {}).pool_count || 0} 池，已同步 ${synced} 项余额入台账`, 'ok');
  } catch (e) {
    showToast('算力刷新失败：' + e.message, 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function runComputeGuardrails() {
  try {
    const res = await api('/api/compute/guardrails', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
    const cnt = (res.alerts || []).length;
    showToast(`算力护栏巡检完成：检查 ${Number(res.checked || 0)} 项，告警 ${cnt} 条`, cnt > 0 ? 'err' : 'ok');
    STATE.compute = await api('/api/compute');
    renderCompute();
  } catch (e) {
    showToast('护栏巡检失败：' + e.message, 'err');
  }
}

async function toggleProviderCircuit(provider, openState) {
  try {
    const res = await api('/api/compute/circuit/' + encodeURIComponent(provider), {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ open: !!openState })
    });
    showToast(`平台 ${res.provider} 已${res.circuit_open ? '熔断 (CIRCUIT_OPEN)' : '恢复健康路由'}`, 'ok');
    const [cm, fin, ov] = await Promise.all([api('/api/compute'), api('/api/finance'), api('/api/overview')]);
    STATE.compute = cm;
    STATE.finance = fin;
    STATE.overview = ov;
    renderCompute();
    renderFinance();
    renderOverview();
  } catch (e) {
    showToast('熔断切换失败：' + e.message, 'err');
  }
}

async function submitComputeAssetForm(evt) {
  evt.preventDefault();
  const payload = {
    provider: document.getElementById('ca-provider').value.trim(),
    account_label: document.getElementById('ca-label').value.trim() || 'main',
    balance: Number(document.getElementById('ca-balance').value),
    currency: document.getElementById('ca-currency').value,
    daily_spend: Number(document.getElementById('ca-daily').value || 0),
    avg_daily_spend: Number(document.getElementById('ca-avg').value || 0),
    billing_scope: document.getElementById('ca-scope').value,
    plan_name: document.getElementById('ca-plan').value.trim(),
    plan_expires_at: document.getElementById('ca-expires').value.trim(),
    fallback_model: document.getElementById('ca-fallback').value.trim(),
  };
  try {
    const res = await api('/api/compute/assets', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload)
    });
    showToast(`已登记算力资产：${res.provider}/${res.account_label} (${res.currency} ${res.balance})`, 'ok');
    document.getElementById('compute-asset-form-card').style.display = 'none';
    const [cm, fin, ov] = await Promise.all([api('/api/compute'), api('/api/finance'), api('/api/overview')]);
    STATE.compute = cm;
    STATE.finance = fin;
    STATE.overview = ov;
    renderCompute();
    renderFinance();
    renderOverview();
  } catch (e) {
    showToast('登记算力资产失败：' + e.message, 'err');
  }
}

/* ==================== DAILY REPLENISH ==================== */
function setReplenishFilter(f) {
  STATE.replenishFilter = f;
  document.querySelectorAll('#rpl-filter-pills .btn').forEach(b => {
    b.classList.toggle('active', b.dataset.rf === f);
  });
  renderReplenish();
}

function toggleReplenishForm(open) {
  const card = document.getElementById('rpl-form-card');
  if (!card) return;
  const show = (typeof open === 'boolean') ? open : (card.style.display === 'none');
  card.style.display = show ? 'block' : 'none';
}

function resetReplenishForm() {
  document.getElementById('rpl-id').value = '';
  document.getElementById('rpl-form-title').textContent = '➕ 新增日用品补货周期';
  document.getElementById('rpl-name').value = '';
  document.getElementById('rpl-category').value = '清洁洗护';
  document.getElementById('rpl-location').value = '家';
  document.getElementById('rpl-channel').value = 'meituan';
  document.getElementById('rpl-cycle').value = 45;
  document.getElementById('rpl-lead').value = 5;
  document.getElementById('rpl-qty').value = 1;
  document.getElementById('rpl-price').value = '';
  document.getElementById('rpl-last').value = '';
  document.getElementById('rpl-shop').value = '';
  document.getElementById('rpl-query').value = '';
  document.getElementById('rpl-sku').value = '';
  document.getElementById('rpl-tier').value = 'core';
  document.getElementById('rpl-spec').value = '';
  document.getElementById('rpl-notes').value = '';
}

function editReplenishItem(itemId) {
  const items = (STATE.replenish && STATE.replenish.items) || [];
  const it = items.find(x => x.item_id === itemId);
  if (!it) return;
  document.getElementById('rpl-id').value = it.item_id || '';
  document.getElementById('rpl-form-title').textContent = '✏️ 编辑日用品：' + (it.name || itemId);
  document.getElementById('rpl-name').value = it.name || '';
  document.getElementById('rpl-category').value = it.category || '日用消耗';
  document.getElementById('rpl-location').value = it.location || '家';
  document.getElementById('rpl-channel').value = it.channel || 'meituan';
  document.getElementById('rpl-cycle').value = Number(it.cycle_days || 30);
  document.getElementById('rpl-lead').value = Number(it.lead_days || 5);
  document.getElementById('rpl-qty').value = Number(it.qty || 1);
  document.getElementById('rpl-price').value = it.target_yuan != null ? it.target_yuan : '';
  document.getElementById('rpl-last').value = it.last_bought || '';
  document.getElementById('rpl-shop').value = it.meituan_shop || '';
  document.getElementById('rpl-query').value = it.meituan_query || '';
  document.getElementById('rpl-sku').value = it.sku || '';
  document.getElementById('rpl-tier').value = it.tier || 'core';
  document.getElementById('rpl-spec').value = it.spec || '';
  document.getElementById('rpl-notes').value = it.notes || '';
  toggleReplenishForm(true);
  document.getElementById('rpl-form-card').scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function renderReplenish() {
  const rp = STATE.replenish;
  if (!rp) return;
  const s = rp.summary || {};
  const items = rp.items || [];

  const tc = document.getElementById('tc-replenish');
  if (tc) {
    const dueN = Number(s.due_count || 0);
    if (dueN > 0) {
      tc.textContent = String(dueN);
      tc.classList.add('warn-pill');
    } else {
      tc.textContent = String(s.enabled_count || s.total || 0);
      tc.classList.remove('warn-pill');
    }
  }
  const modBadge = document.getElementById('rpl-mod-badge');
  if (modBadge) {
    modBadge.textContent = rp.module_enabled ? '🟢 自动到期提醒已开启' : '⚪ 自动提醒已关闭';
    modBadge.className = 'badge ' + (rp.module_enabled ? 'ok' : 'warn');
  }

  const kpis = [
    {
      bar: 'ok',
      label: '📦 监控日用品（启用 / 总计）',
      val: `${s.enabled_count || 0} / ${s.total || 0}`,
      sub: `⭐核心 ${s.core_count || 0} · 🏠家 ${s.home_count || 0} · 🏢公司 ${s.office_count || 0}`,
    },
    {
      bar: Number(s.due_count || 0) > 0 ? 'warn' : 'teal',
      label: '🔴 待补货 / 临期预警',
      val: `${s.due_count || 0} 项`,
      sub: `已到期 ${s.overdue_count || 0} 项 · 到期自动弹 QQ 确认直买`,
    },
    {
      bar: Number(s.calibrate_count || 0) > 0 ? 'warn' : '',
      label: '🟡 待校准起点',
      val: `${s.calibrate_count || 0} 项`,
      sub: '账本缺明细或较久未见单 · 点「刚买过」即启倒计时',
    },
    {
      bar: 'purple',
      label: '💰 预估月均日用消耗',
      val: `¥${Number(s.monthly_est_yuan || 0).toFixed(0)} / 月`,
      sub: `按已启用项周期折算 · 约 ¥${(Number(s.monthly_est_yuan || 0) * 12).toFixed(0)}/年`,
    },
  ];
  const kpiEl = document.getElementById('rpl-kpis');
  if (kpiEl) {
    kpiEl.innerHTML = kpis.map(k => `
      <div class="card kpi-card" style="cursor:default">
        <div class="kpi-bar ${esc(k.bar)}"></div>
        <div class="kpi-label"><span>${esc(k.label)}</span></div>
        <div class="kpi-value">${esc(k.val)}</div>
        <div class="kpi-sub">${esc(k.sub)}</div>
      </div>`).join('');
  }

  const rf = STATE.replenishFilter || 'all';
  const q = ((document.getElementById('rpl-search') || {}).value || '').trim().toLowerCase();
  const filtered = items.filter(it => {
    if (rf === 'due' && it.status !== 'due' && it.status !== 'soon') return false;
    if (rf === 'calibrate' && it.status !== 'calibrate') return false;
    if (rf === 'core' && it.tier !== 'core') return false;
    if (rf === 'home' && it.location !== '家') return false;
    if (rf === 'office' && it.location !== '公司') return false;
    if (rf === 'paused' && it.status !== 'paused') return false;
    if (q) {
      const chTxt = ({meituan:'美团', jd:'京东', taobao:'淘宝 天猫', sams:'山姆', hema:'盒马'}[it.channel] || it.channel || '');
      const hay = `${it.name} ${it.category} ${it.location} ${chTxt} ${it.spec} ${it.meituan_shop} ${it.meituan_query} ${it.sku} ${it.evidence} ${it.notes}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  const tblWrap = document.getElementById('rpl-table-wrap');
  if (tblWrap) {
    if (!filtered.length) {
      tblWrap.innerHTML = '<div class="empty">当前筛选下暂无日用品条目</div>';
    } else {
      tblWrap.innerHTML = `
        <table>
          <thead>
            <tr>
              <th>日用品 &amp; 场景</th>
              <th>渠道与采购目标</th>
              <th>周期 &amp; 消费推算依据</th>
              <th>倒计时 / 状态</th>
              <th>操作</th>
            </tr>
          </thead>
          <tbody>${filtered.map(it => {
            const stCls = it.status === 'due' ? 'err' : (it.status === 'soon' || it.status === 'calibrate' ? 'warn' : (it.status === 'ok' ? 'ok' : ''));
            const barCls = it.status === 'due' ? 'err' : (it.status === 'soon' ? 'warn' : 'ok');
            const pct = Math.max(0, Math.min(100, Number(it.remaining_pct || 0)));
            const tierBadge = it.tier === 'core' ? '<span class="badge accent">⭐核心</span>' : (it.tier === 'movein' ? '<span class="badge purple">🏡新房</span>' : '<span class="badge">✨常买</span>');
            const locBadge = it.location === '公司' ? '<span class="badge purple">🏢公司</span>' : '<span class="badge">🏠家</span>';
            const chHtml = it.channel === 'meituan'
              ? `<span class="badge ok">🛵 美团直买</span><div class="muted" style="font-size:11.5px;margin-top:3px">${esc(it.meituan_shop || '附近商超')} · ${esc(it.meituan_query || it.name)} ×${Number(it.qty || 1)}</div>`
              : (it.channel === 'jd'
                ? `<span class="badge accent">📦 京东加车</span><div class="muted mono" style="font-size:11.5px;margin-top:3px">${it.sku ? 'SKU ' + esc(it.sku) : '⚠️ 待填 SKU/链接'} ×${Number(it.qty || 1)}</div>`
                : (it.channel === 'taobao'
                  ? `<span class="badge warn">🧡 淘宝复购</span><div class="muted" style="font-size:11.5px;margin-top:3px">淘宝 / 天猫 App 复购 ×${Number(it.qty || 1)}</div>`
                  : (it.channel === 'sams'
                    ? `<span class="badge purple">🏬 山姆囤货</span><div class="muted" style="font-size:11.5px;margin-top:3px">山姆会员商店（App/门店）×${Number(it.qty || 1)}</div>`
                    : `<span class="badge accent">🦛 盒马复购</span><div class="muted" style="font-size:11.5px;margin-top:3px">盒马鲜生 App 复购 ×${Number(it.qty || 1)}</div>`)));
            const qqBtnLbl = (it.channel === 'meituan' || it.channel === 'jd') ? '📲 QQ确认买' : '📲 QQ提醒';
            const confLbl = it.confidence === 'high' ? `实测${Number(it.evidence_count || 0)}笔` : (it.confidence === 'medium' ? '单笔推算' : '经验估算');
            const histTip = (it.history_samples || []).map(h => `${h.date} ${h.source} ¥${h.yuan}`).join('；');
            const progHtml = it.status === 'calibrate'
              ? `<div class="muted" style="font-size:11.5px">上次记录：${esc(it.last_bought || '无明细')}</div><div class="muted" style="font-size:11px;color:var(--warn)">点「刚买过」即启倒计时</div>`
              : (it.status === 'paused'
                ? `<div class="muted" style="font-size:11.5px">已暂停提醒</div>`
                : `<div class="mono" style="font-size:12px"><strong>${Number(it.days_left) < 0 ? '逾期 ' + Math.abs(Number(it.days_left)) + ' 天' : '余 ' + Number(it.days_left) + ' 天'}</strong> <span class="muted">(${esc(it.next_due || '-')})</span></div>
                   <div class="prog-track" style="margin:4px 0;min-width:110px"><div class="prog-fill ${barCls}" style="width:${pct}%"></div></div>
                   <div class="muted mono" style="font-size:11px">上次 ${esc(it.last_bought || '-')}</div>`);
            return `
              <tr>
                <td>
                  <div class="row" style="gap:5px;flex-wrap:wrap;margin-bottom:3px">
                    <strong>${esc(it.name)}</strong> ${locBadge} ${tierBadge}
                    <span class="badge">${esc(it.category)}</span>
                  </div>
                  ${it.spec ? `<div class="muted" style="font-size:11.5px">${esc(it.spec)}</div>` : ''}
                  ${it.notes ? `<div class="muted" style="font-size:11px;color:var(--accent)">💡 ${esc(it.notes)}</div>` : ''}
                </td>
                <td>
                  ${chHtml}
                  ${it.target_yuan ? `<div class="muted mono" style="font-size:11px">参考 ¥${Number(it.target_yuan).toFixed(0)}（约 ¥${Number(it.monthly_est_yuan || 0).toFixed(0)}/月）</div>` : ''}
                </td>
                <td style="max-width:300px">
                  <div class="row" style="gap:5px;margin-bottom:3px">
                    <strong class="mono">每 ${Number(it.cycle_days)} 天</strong>
                    <span class="badge">${esc(confLbl)}</span>
                    <span class="muted" style="font-size:11px">提前 ${Number(it.lead_days)} 天</span>
                  </div>
                  <div class="muted" style="font-size:11.5px;line-height:1.4" title="${esc(histTip)}">${esc(it.evidence || '-')}</div>
                </td>
                <td>
                  <span class="badge ${stCls}" style="margin-bottom:4px">${esc(it.status_label)}</span>
                  ${progHtml}
                </td>
                <td>
                  <div class="row" style="gap:5px;flex-wrap:wrap">
                    <button class="btn sm ok" data-id="${esc(it.item_id)}" onclick="doReplenishAction(this.dataset.id, 'bought')" title="标记今天刚买过，重置下个周期">✅ 刚买过</button>
                    <button class="btn sm primary" data-id="${esc(it.item_id)}" onclick="doReplenishAction(this.dataset.id, 'remind_qq')" title="立刻向 QQ 发送补货提醒/确认卡">${qqBtnLbl}</button>
                    <button class="btn sm" data-id="${esc(it.item_id)}" onclick="doReplenishAction(this.dataset.id, 'snooze', {days:14})">⏳+14天</button>
                    <button class="btn sm" data-id="${esc(it.item_id)}" onclick="editReplenishItem(this.dataset.id)">✏️</button>
                    <button class="btn sm" data-id="${esc(it.item_id)}" data-enabled="${it.enabled ? '0' : '1'}" onclick="doReplenishAction(this.dataset.id, 'toggle', {enabled:this.dataset.enabled === '1'})">${it.enabled ? '⏸️' : '▶️'}</button>
                    <button class="btn sm err" data-id="${esc(it.item_id)}" data-name="${esc(it.name)}" onclick="deleteReplenishItem(this.dataset.id, this.dataset.name)">🗑️</button>
                  </div>
                </td>
              </tr>`;
          }).join('')}</tbody>
        </table>`;
    }
  }

  const careEl = document.getElementById('rpl-care-supplies');
  if (careEl) {
    const cs = rp.daily_care_supplies || [];
    careEl.innerHTML = cs.length ? cs.map(c => {
      const ok = c.status === 'ok';
      const stCls = c.status === 'low' ? 'err' : (ok ? 'ok' : '');
      return `
        <div class="pool-card">
          <div class="row" style="justify-content:space-between">
            <strong>${esc(c.label || c.key)}</strong>
            <span class="badge ${stCls}">${c.status === 'low' ? '🔴 快用完' : (ok ? '🟢 充裕' : '未知')}</span>
          </div>
          ${c.status === 'unknown'
            ? '<div class="muted">暂无记录</div>'
            : `<div class="pool-balance mono">${Number(c.stock || 0).toFixed(0)} <span style="font-size:13px" class="muted">${esc(c.unit || '')} · 够 ${Number(c.days_left || 0).toFixed(0)} 天</span></div>
               <div class="muted mono" style="font-size:11.5px">预计 ${esc(c.run_out || '-')} 用完 · ${esc(c.rate_src || '')} ${Number(c.rate || 0).toFixed(2)}${esc(c.unit || '')}/天</div>`}
        </div>`;
    }).join('') : '<div class="empty">暂无 daily_care 耗材数据</div>';
  }
}

async function submitReplenishForm(ev) {
  ev.preventDefault();
  const payload = {
    item_id: document.getElementById('rpl-id').value.trim(),
    name: document.getElementById('rpl-name').value.trim(),
    category: document.getElementById('rpl-category').value,
    location: document.getElementById('rpl-location').value,
    channel: document.getElementById('rpl-channel').value,
    cycle_days: Number(document.getElementById('rpl-cycle').value || 30),
    lead_days: Number(document.getElementById('rpl-lead').value || 5),
    qty: Number(document.getElementById('rpl-qty').value || 1),
    target_yuan: document.getElementById('rpl-price').value !== '' ? Number(document.getElementById('rpl-price').value) : null,
    last_bought_date: document.getElementById('rpl-last').value.trim(),
    meituan_shop: document.getElementById('rpl-shop').value.trim(),
    meituan_query: document.getElementById('rpl-query').value.trim(),
    sku_or_url: document.getElementById('rpl-sku').value.trim(),
    tier: document.getElementById('rpl-tier').value,
    spec: document.getElementById('rpl-spec').value.trim(),
    notes: document.getElementById('rpl-notes').value.trim(),
  };
  try {
    await api('/api/replenish/items', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload),
    });
    showToast('✅ 已保存日用品：' + payload.name, 'ok');
    toggleReplenishForm(false);
    resetReplenishForm();
    STATE.replenish = await api('/api/replenish');
    STATE.overview = await api('/api/overview');
    renderReplenish();
    renderOverview();
  } catch (e) {
    showToast('保存失败：' + e.message, 'err');
  }
}

async function doReplenishAction(itemId, action, extra={}) {
  try {
    const res = await api('/api/replenish/items/' + encodeURIComponent(itemId) + '/action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ action, ...extra }),
    });
    showToast(res.summary || res.message || '操作已完成', 'ok');
    STATE.replenish = await api('/api/replenish');
    STATE.overview = await api('/api/overview');
    renderReplenish();
    renderOverview();
  } catch (e) {
    showToast('操作失败：' + e.message, 'err');
  }
}

async function deleteReplenishItem(itemId, name) {
  if (!confirm(`确认从补货清单移除「${name}」？`)) return;
  await doReplenishAction(itemId, 'delete');
}

async function reinferReplenish() {
  showToast('正在从美团/账本消费记录重算周期…', 'ok');
  try {
    const res = await api('/api/replenish/reinfer', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({}),
    });
    showToast(`周期重算完成：共 ${res.total || 0} 项（新增 ${res.added || 0} / 更新 ${res.updated || 0}）`, 'ok');
    STATE.replenish = await api('/api/replenish');
    STATE.overview = await api('/api/overview');
    renderReplenish();
    renderOverview();
  } catch (e) {
    showToast('重算失败：' + e.message, 'err');
  }
}

async function checkDueReplenish() {
  showToast('正在巡检到期日用品…', 'ok');
  try {
    const res = await api('/api/replenish/check-due', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ notify: true }),
    });
    const dueN = (res.due || []).length;
    const pushN = (res.pushed || []).length;
    showToast(dueN ? `巡检完成：到期/临期 ${dueN} 项，已推 QQ ${pushN} 条` : '当前全部已校准日用品余量充裕，无需补货', 'ok');
    STATE.replenish = await api('/api/replenish');
    renderReplenish();
  } catch (e) {
    showToast('巡检失败：' + e.message, 'err');
  }
}

/* ==================== OPS & SCHEDULE RADAR (PLAN A) ==================== */
function fmtAgeSec(sec) {
  if (sec == null || isNaN(Number(sec))) return '-';
  const s = Math.max(0, Math.floor(Number(sec)));
  if (s < 90) return `${s}秒前`;
  if (s < 3600) return `${Math.floor(s / 60)}分钟前`;
  if (s < 86400) return `${(s / 3600).toFixed(1)}小时前`;
  return `${(s / 86400).toFixed(1)}天前`;
}

function fmtShortTs(iso) {
  if (!iso) return '-';
  return String(iso).slice(0, 16).replace('T', ' ');
}

function setOpsFilter(f) {
  STATE.opsFilter = f;
  renderOpsSchedules();
}

function renderOps() {
  const ops = STATE.ops;
  if (!ops) return;
  const s = ops.summary || {};
  const host = ops.host || {};
  const sh = ops.siming_health || {};
  const beats = sh.beats || {};
  const deps = sh.dependencies || {};
  const pg = ops.push_guard || {};
  const timers = ops.siming_core_timers || [];
  const tools = ops.external_tools || [];
  const issues = ops.issues || [];

  // Tab badge
  const tc = document.getElementById('tc-ops');
  if (tc) {
    const failN = Number(s.timers_failed || 0) + Number(s.crons_failed || 0);
    const guardN = Number(s.push_guard_cooling || 0) + Number(s.push_guard_quiet || 0);
    if (failN > 0 || Number(s.critical_count || 0) > 0) {
      tc.textContent = String(failN || s.critical_count) + '!';
      tc.classList.add('err-pill');
      tc.classList.remove('warn-pill');
    } else if (s.siming_paused || guardN > 0 || Number(s.warn_count || 0) > 0) {
      tc.textContent = String(guardN || s.warn_count || '!');
      tc.classList.add('warn-pill');
      tc.classList.remove('err-pill');
    } else {
      tc.textContent = String(s.schedules_total || 0);
      tc.classList.remove('warn-pill', 'err-pill');
    }
  }

  // Toolbar badges & host lightweight strip
  const stBadge = document.getElementById('ops-status-badge');
  if (stBadge) {
    const st = String(s.overall_status || 'OK');
    stBadge.textContent = st === 'OK' ? '🟢 运行健康' : (st === 'CRITICAL' ? '🔴 存在严重异常' : '🟠 需关注');
    stBadge.className = 'badge ' + (st === 'OK' ? 'ok' : (st === 'CRITICAL' ? 'err' : 'warn'));
  }
  const pBadge = document.getElementById('ops-pause-badge');
  const pBtn = document.getElementById('btn-ops-pause');
  const rBtn = document.getElementById('btn-ops-resume');
  if (pBadge && pBtn && rBtn) {
    if (sh.paused) {
      pBadge.style.display = 'inline-flex';
      pBadge.className = 'badge warn';
      pBadge.textContent = `⏸️ Worker 已暂停至 ${fmtShortTs(sh.paused_until)}（余 ${Number(sh.pause_remaining_min || 0)}m）`;
      rBtn.style.display = 'inline-flex';
    } else {
      pBadge.style.display = 'none';
      rBtn.style.display = 'none';
    }
  }
  const subTs = document.getElementById('ops-sub-ts');
  if (subTs) {
    subTs.textContent = `更新于 ${String(ops.generated_at || '').replace('T', ' ').slice(0, 19)} · 主机 ${host.hostname || '-'} · SQLite ${sh.sqlite_journal_mode || '-'}`;
  }

  const hostEl = document.getElementById('ops-host-pills');
  if (hostEl) {
    const memCls = Number(host.mem_used_pct || 0) >= 85 ? 'warn' : 'ok';
    const diskCls = Number(host.disk_root_used_pct || 0) >= 90 ? 'warn' : 'ok';
    hostEl.innerHTML = `
      <span class="badge ${memCls}">RAM ${host.mem_used_pct != null ? Number(host.mem_used_pct).toFixed(1) + '%' : '-'} (${host.mem_used_gb ?? '-'}/${host.mem_total_gb ?? '-'} GB)</span>
      <span class="badge ${diskCls}">根分区余 ${host.disk_root_free_gb != null ? Number(host.disk_root_free_gb).toFixed(1) + ' GB' : '-'} (${host.disk_root_used_pct ?? '-'}%)</span>
      <span class="badge mono">Load ${host.load_1m ?? '-'} / ${host.load_5m ?? '-'} / ${host.load_15m ?? '-'} (${host.cpu_cores || '-'}C)</span>
      <span class="badge">${host.uptime_sec != null ? fmtUptime(host.uptime_sec) : '运行中'}</span>
      ${host.pacman_lock_present ? '<span class="badge err">🔒 pacman db.lck 存在</span>' : '<span class="badge ok">📦 pacman 无锁</span>'}`;
  }

  // External monitoring deep links
  const extEl = document.getElementById('ops-ext-tools');
  if (extEl) {
    extEl.innerHTML = `<span class="muted" style="font-weight:600">🔗 专业监控面板（不重复造轮子）：</span>` + tools.map(t => {
      const dotCls = t.reachable === true ? 'on' : (t.reachable === false ? 'off' : '');
      const dotStyle = t.reachable === null ? 'background:var(--muted)' : '';
      return `
        <a class="ops-ext-link" href="${esc(t.url)}" target="_blank" rel="noopener" title="${esc(t.role)} · ${esc(t.url)}">
          <span class="svc-dot ${dotCls}" style="${dotStyle}"></span>
          <span>${esc(t.icon || '🔗')} ${esc(t.name)}</span>
          <span class="muted" style="font-size:11px">${esc(t.role)}</span>
        </a>`;
    }).join('');
  }

  // Issues strip
  const issEl = document.getElementById('ops-issues');
  if (issEl) {
    issEl.innerHTML = issues.length ? issues.map(i => `
      <div class="compute-alert-item ${i.severity === 'critical' ? 'critical' : ''}">
        <span class="badge ${i.severity === 'critical' ? 'err' : (i.severity === 'warn' ? 'warn' : 'accent')}">${esc(i.area)}</span>
        <span>${esc(i.message)}</span>
      </div>`).join('') : '';
  }

  // KPIs
  const effC = (sh.effects && sh.effects.counts) || {};
  const ob24 = sh.outbox_24h || {};
  const bkp = sh.backups || {};
  const cdp = deps.cdp_9222 || {};
  const kpiEl = document.getElementById('ops-kpis');
  if (kpiEl) {
    const cards = [
      {
        bar: Number(s.timers_failed || 0) > 0 ? 'warn' : 'ok',
        label: '⏱️ 司命核心节拍 (tick / watchdog)',
        val: beats.last_tick_age_sec != null ? fmtAgeSec(beats.last_tick_age_sec) : (beats.last_tick_ok_at ? fmtShortTs(beats.last_tick_ok_at) : '-'),
        sub: `watchdog ${fmtAgeSec(beats.watchdog_age_sec)} · 边缘同步 ${fmtAgeSec(beats.edge_vps_age_sec)}`,
      },
      {
        bar: (Number(s.push_guard_cooling || 0) > 0 || Number(effC.FAILED || 0) > 0) ? 'warn' : 'teal',
        label: '📣 24h QQ 外发 / 暂扣熔断',
        val: `${Number(ob24.SENT || 0)} 发 / ${Number(s.outbox_held_24h || 0)} 拦`,
        sub: `晨札队列 ${Number(ob24.MORNING_QUEUE || 0)} · Effects 待跑 ${Number(effC.PENDING || 0)} / 失败 ${Number(effC.FAILED || 0)}`,
      },
      {
        bar: (Number(s.timers_failed || 0) + Number(s.crons_failed || 0)) > 0 ? 'warn' : 'purple',
        label: '🗓️ 三合一调度总览',
        val: `${Number(s.schedules_total || 0)} 项`,
        sub: `Systemd ${Number(s.timers_total || 0)} · Hermes ${Number(s.crons_enabled || 0)}/${Number(s.crons_total || 0)} · Waits ${Number(s.waits_total || 0)} · 直推QQ ${Number(s.direct_qq_count || 0)}`,
      },
      {
        bar: cdp.atrust_collision_suspect ? 'warn' : '',
        label: '💾 备份与核心依赖',
        val: bkp.count ? `${Number(bkp.count)} 份备份` : '暂无备份',
        sub: `${bkp.latest_name ? bkp.latest_name + ' (' + bkp.size_mb + 'MB)' : '待执行 siming backup'} · CDP ${cdp.main_chromium_up === true ? '🟢正常' : (cdp.main_chromium_up === false ? '⚪未开' : '离线')}`,
      },
    ];
    kpiEl.innerHTML = cards.map(k => `
      <div class="card kpi-card" style="cursor:default">
        <div class="kpi-bar ${esc(k.bar)}"></div>
        <div class="kpi-label"><span>${esc(k.label)}</span></div>
        <div class="kpi-value">${esc(k.val)}</div>
        <div class="kpi-sub">${esc(k.sub)}</div>
      </div>`).join('');
  }

  // Core 4 Siming Timers
  const coreEl = document.getElementById('ops-core-timers');
  if (coreEl) {
    if (!timers.length) {
      coreEl.innerHTML = '<div class="empty">当前环境未检测到 systemd user timers（或处于离线模式）</div>';
    } else {
      coreEl.innerHTML = timers.map(t => {
        const stCls = t.last_status === 'ok' ? 'ok' : (t.last_status === 'running' ? 'accent' : (t.last_status === 'idle' ? '' : 'err'));
        const durStr = t.duration_sec != null ? `耗时 ${Number(t.duration_sec).toFixed(2)}s` : '';
        return `
          <div class="item-row">
            <div class="item-main">
              <div class="item-title">
                <span class="badge ${stCls}">${esc(t.last_status.toUpperCase())}</span>
                <span class="mono">${esc(t.name)}</span>
                <span class="muted" style="font-weight:400">${esc(t.schedule)}</span>
              </div>
              <div class="muted">${esc(t.description)}</div>
              <div class="muted mono">上次 ${esc(fmtShortTs(t.last_run_at))} ${durStr ? '· ' + esc(durStr) : ''} · 下次 ${esc(fmtShortTs(t.next_run_at))} ${t.last_error ? '· ⚠️ ' + esc(t.last_error) : ''}</div>
            </div>
            <div class="item-actions">
              ${t.has_log_preview ? `<button class="btn sm" data-source="systemd" data-id="${esc(t.raw_id)}" data-name="${esc(t.name)}" onclick="previewScheduleOutput(this.dataset.source, this.dataset.id, this.dataset.name)">📄 日志</button>` : ''}
            </div>
          </div>`;
      }).join('');
    }
  }

  // Effects issues
  const effEl = document.getElementById('ops-effects-issues');
  if (effEl) {
    const badEffs = (sh.effects && sh.effects.recent_issues) || [];
    effEl.innerHTML = badEffs.length ? `<div class="item-list">${badEffs.map(e => `
      <div class="item-row">
        <div class="item-main">
          <div class="item-title"><span class="badge ${e.status === 'FAILED' ? 'err' : 'accent'}">${esc(e.status)}</span> <span class="mono">${esc(e.effect_id)}</span> · ${esc(e.kind)}</div>
          <div class="muted mono">重试 ${Number(e.retry_count || 0)} · ${esc(fmtShortTs(e.updated_at))} ${e.last_error ? '· ' + esc(e.last_error) : ''}</div>
        </div>
      </div>`).join('')}</div>` : `<div class="empty">🎉 当前无失败或卡住的 effects 队列任务（24h 已完成 ${Number(effC.DONE_24H || 0)} 条）</div>`;
  }

  // Core dependencies
  const depsEl = document.getElementById('ops-deps');
  if (depsEl) {
    const depBadge = (v, okLbl='🟢 在线', offLbl='⚪ 离线') => {
      if (v === true) return `<span class="badge ok">${okLbl}</span>`;
      if (v === false) return `<span class="badge warn">${offLbl}</span>`;
      return '<span class="badge">⚪ 未探测</span>';
    };
    const cdpBadge = cdp.atrust_collision_suspect
      ? `<span class="badge warn">⚠️ aTrust 抢占${cdp.main_chromium_up ? ' ([::1]回退可用)' : ''}</span>`
      : depBadge(cdp.main_chromium_up, '🟢 主 Chromium 正常', '⚪ 未开启');
    const srcs = sh.sources || {};
    depsEl.innerHTML = `
      <div class="ops-dep-row"><span>🌐 浏览器 CDP (9222)</span>${cdpBadge}</div>
      <div class="ops-dep-row"><span>🛰️ sing-box 代理 (7890) / Clash API (9090)</span><span>${depBadge(deps.proxy_7890_up, '7890 OK', '7890 DOWN')} ${depBadge(deps.clash_api_9090_up, '9090 OK', '9090 DOWN')}</span></div>
      <div class="ops-dep-row"><span>☁️ 公网边缘同步 &amp; iPhone 心跳</span><span class="mono muted">edge: ${esc(fmtAgeSec(beats.edge_vps_age_sec))} · iOS: ${esc(fmtAgeSec(beats.ios_hb_age_sec))}</span></div>
      <div class="ops-dep-row"><span>📡 数据源巡检 (${Number(srcs.ok || 0)}/${Number(srcs.total || 0)} OK)</span><span class="badge ${(srcs.error || srcs.stale) ? 'warn' : 'ok'}">${(srcs.error || srcs.stale) ? `异常 ${Number(srcs.error || 0)} / 过期 ${Number(srcs.stale || 0)}` : '全部新鲜'}</span></div>
      <div class="ops-dep-row"><span>💾 最近 SQLite 备份</span><span class="mono muted">${bkp.latest_name ? esc(bkp.latest_name) + ' (' + esc(bkp.size_mb) + ' MB · ' + esc(fmtShortTs(bkp.mtime_iso)) + ')' : '尚无备份文件'}</span></div>`;
  }

  // Last Diagnose Box
  const diagEl = document.getElementById('ops-diagnose-box');
  if (diagEl) {
    const ld = sh.last_diagnose;
    if (ld && ld.reply) {
      const rcN = (ld.root_causes || []).length;
      diagEl.innerHTML = `
        <div style="border-top:1px solid var(--border);padding-top:10px">
          <div class="row" style="justify-content:space-between;margin-bottom:6px">
            <strong style="font-size:12.5px">🩺 最近链路体检 (${esc(fmtShortTs(ld.checked_at))})</strong>
            <span class="badge ${rcN === 0 ? 'ok' : 'warn'}">${rcN === 0 ? '全链路健康' : rcN + ' 项待关注'}</span>
          </div>
          <pre class="ops-preview-pre" style="max-height:190px">${esc(ld.reply)}</pre>
        </div>`;
    } else {
      diagEl.innerHTML = '<div class="muted" style="font-size:12px">点击右上角「🩺 链路体检」可实时运行 doctor + aTrust/sing-box/CDP/Nous 根因合成诊断。</div>';
    }
  }

  // Push Guard section
  const pgEl = document.getElementById('ops-push-guard');
  if (pgEl) {
    const rules = pg.rules || [];
    const needAction = rules.filter(r => r.can_restore);
    const heldSamples = pg.recent_held || [];
    let html = '';
    if (needAction.length) {
      html += `<div class="item-list" style="margin-bottom:14px">${needAction.map(r => `
        <div class="item-row" style="border-color:rgba(245,158,11,0.45)">
          <div class="item-main">
            <div class="item-title">
              <span class="badge warn">${r.is_cooling ? '🧯 熔断冷却' : '🔕 QUIET 静默降级'}</span>
              <span class="mono">${esc(r.rule_id)}</span>
            </div>
            <div class="muted">14天推送 ${Number(r.sent_14d)} 次 · 处理 ${Number(r.acted_14d)} 次${r.accept_rate_pct != null ? ' (' + Number(r.accept_rate_pct).toFixed(1) + '%)' : ''} · 近 24h 拦截 ${Number(r.held_24h || 0)} 条${r.cooldown_until ? ' · 冷却至 ' + esc(fmtShortTs(r.cooldown_until)) : ''}</div>
          </div>
          <div class="item-actions">
            <button class="btn sm ok" data-rule-id="${esc(r.rule_id)}" onclick="restorePushRuleAction(this.dataset.ruleId)">🔔 恢复即时推送</button>
          </div>
        </div>`).join('')}</div>`;
    } else {
      html += '<div class="empty" style="margin-bottom:12px">🟢 当前没有被熔断或静默降级的推送规则（全部正常放行）</div>';
    }

    if (rules.length) {
      html += `
        <div class="table-wrap">
          <table>
            <thead><tr><th>规则 ID</th><th>模式</th><th>14d 推送 / 处理</th><th>处理率</th><th>24h 暂扣</th><th>操作</th></tr></thead>
            <tbody>${rules.slice(0, 18).map(r => {
              const mCls = r.is_cooling ? 'err' : (r.mode === 'QUIET' ? 'warn' : (r.mode === 'FROZEN' || r.mode === 'DEMOTED' ? '' : 'ok'));
              return `
                <tr>
                  <td class="mono"><strong>${esc(r.rule_id)}</strong> ${r.hard_rule ? '<span class="badge accent">HARD</span>' : ''}</td>
                  <td><span class="badge ${mCls}">${esc(r.mode)}${r.is_cooling ? ' · 冷却中' : ''}</span></td>
                  <td class="mono">${Number(r.sent_14d)} / ${Number(r.acted_14d)}</td>
                  <td class="mono">${r.accept_rate_pct != null ? Number(r.accept_rate_pct).toFixed(1) + '%' : '-'}</td>
                  <td class="mono">${Number(r.held_24h || 0) > 0 ? '<span class="badge warn">' + Number(r.held_24h) + '</span>' : '0'}</td>
                  <td>${r.can_restore ? `<button class="btn sm ok" data-rule-id="${esc(r.rule_id)}" onclick="restorePushRuleAction(this.dataset.ruleId)">🔔 恢复</button>` : '<span class="muted">-</span>'}</td>
                </tr>`;
            }).join('')}</tbody>
          </table>
        </div>`;
    }
    if (heldSamples.length) {
      html += `
        <details class="raw-json" style="margin-top:10px">
          <summary>查看近 24h 被拦截/静默汇总的原始样本 (${heldSamples.length} 条)</summary>
          <div class="item-list" style="margin-top:8px">${heldSamples.map(h => `
            <div class="item-row">
              <div class="item-main">
                <div class="item-title"><span class="badge warn">${esc(h.status)}</span> <span class="mono">${esc(h.rule_id)}</span> <span class="muted mono">${esc(fmtShortTs(h.created_at))}</span></div>
                <div class="muted">${esc(h.preview)}</div>
              </div>
            </div>`).join('')}</div>
        </details>`;
    }
    pgEl.innerHTML = html;
  }

  renderOpsSchedules();
}

function renderOpsSchedules() {
  const ops = STATE.ops;
  if (!ops) return;
  const all = (ops.schedules && ops.schedules.items) || [];
  const f = STATE.opsFilter || 'all';
  const q = ((document.getElementById('ops-sched-search') || {}).value || '').trim().toLowerCase();

  const cnt = {
    all: all.length,
    failed: all.filter(x => x.last_status === 'failed' || x.last_status === 'timeout').length,
    direct_qq: all.filter(x => x.push_channel === 'direct_qq' && x.enabled).length,
    harvest: all.filter(x => x.push_channel === 'siming_harvest').length,
    systemd: all.filter(x => x.source === 'systemd').length,
    hermes: all.filter(x => x.source === 'hermes').length,
    wait: all.filter(x => x.source === 'siming_wait').length,
    siming: all.filter(x => !!x.is_siming).length,
  };

  const pills = [
    { id: 'all', label: `全部 (${cnt.all})` },
    { id: 'failed', label: `❌ 异常/失败 (${cnt.failed})` },
    { id: 'direct_qq', label: `📣 直推 QQ (${cnt.direct_qq})` },
    { id: 'harvest', label: `🌾 司命收割 (${cnt.harvest})` },
    { id: 'systemd', label: `⚙️ Systemd (${cnt.systemd})` },
    { id: 'hermes', label: `🤖 Hermes Cron (${cnt.hermes})` },
    { id: 'wait', label: `⏳ Siming Waits (${cnt.wait})` },
    { id: 'siming', label: `⚡ 仅司命相关 (${cnt.siming})` },
  ];
  const fEl = document.getElementById('ops-sched-filters');
  if (fEl) {
    fEl.innerHTML = pills.map(p => `
      <button class="app-pill ${f === p.id ? 'active' : ''}" data-of="${esc(p.id)}" onclick="setOpsFilter(this.dataset.of)">${esc(p.label)}</button>`).join('');
  }

  const rows = all.filter(x => {
    if (f === 'failed' && x.last_status !== 'failed' && x.last_status !== 'timeout') return false;
    if (f === 'direct_qq' && !(x.push_channel === 'direct_qq' && x.enabled)) return false;
    if (f === 'harvest' && x.push_channel !== 'siming_harvest') return false;
    if (f === 'systemd' && x.source !== 'systemd') return false;
    if (f === 'hermes' && x.source !== 'hermes') return false;
    if (f === 'wait' && x.source !== 'siming_wait') return false;
    if (f === 'siming' && !x.is_siming) return false;
    if (q) {
      const hay = `${x.name} ${x.target} ${x.description} ${x.schedule} ${x.push_detail} ${x.harvest_rule || ''} ${x.last_error || ''}`.toLowerCase();
      if (!hay.includes(q)) return false;
    }
    return true;
  });

  const srcBadge = src => {
    if (src === 'systemd') return '<span class="badge accent">⚙️ Systemd</span>';
    if (src === 'hermes') return '<span class="badge purple">🤖 Hermes</span>';
    return '<span class="badge ok">⏳ Wait</span>';
  };
  const chBadge = x => {
    if (x.push_channel === 'direct_qq') return `<span class="badge warn">📣 ${esc(x.push_detail)}</span>`;
    if (x.push_channel === 'siming_harvest') return `<span class="badge ok">🌾 ${esc(x.push_detail)}</span>`;
    if (x.push_channel === 'siming_core' || x.push_channel === 'siming_router') return `<span class="badge accent">⚡ ${esc(x.push_detail)}</span>`;
    if (x.push_channel === 'disabled') return `<span class="badge">⚪ 已停用</span>`;
    return `<span class="badge">${esc(x.push_detail || '本地')}</span>`;
  };
  const stBadge = x => {
    const s = x.last_status;
    if (s === 'failed' || s === 'timeout') return `<span class="badge err">❌ ${esc(s.toUpperCase())}</span>`;
    if (s === 'running') return '<span class="badge accent">🔄 RUNNING</span>';
    if (s === 'ok' || s === 'waiting') return `<span class="badge ok">🟢 ${esc(s.toUpperCase())}</span>`;
    if (s === 'proposed') return '<span class="badge warn">💬 待确认</span>';
    if (s === 'paused') return '<span class="badge">⏸️ PAUSED</span>';
    return `<span class="badge">${esc(String(s || 'idle').toUpperCase())}</span>`;
  };

  const wrap = document.getElementById('ops-schedules-wrap');
  if (!wrap) return;
  if (!rows.length) {
    wrap.innerHTML = '<div class="empty">当前筛选条件下暂无定时任务</div>';
    return;
  }
  wrap.innerHTML = `
    <table>
      <thead>
        <tr>
          <th>任务与来源</th>
          <th>调度频率</th>
          <th>推送去向</th>
          <th>最近状态</th>
          <th>上次 / 下次触发</th>
          <th>只读查看</th>
        </tr>
      </thead>
      <tbody>${rows.map(x => `
        <tr style="${x.enabled ? '' : 'opacity:0.65'}">
          <td style="max-width:340px">
            <div class="row" style="gap:6px;margin-bottom:3px">
              ${srcBadge(x.source)}
              <strong>${esc(x.name)}</strong>
            </div>
            <div class="muted" style="font-size:11.5px;word-break:break-word">${esc(x.description || x.target || '')}</div>
            ${x.last_error ? `<div class="muted" style="color:var(--err);font-size:11.5px;margin-top:2px">⚠️ ${esc(x.last_error)}</div>` : ''}
          </td>
          <td class="mono">${esc(x.schedule || '-')}</td>
          <td>${chBadge(x)}</td>
          <td>
            ${stBadge(x)}
            ${x.duration_sec != null ? `<div class="muted mono" style="font-size:11px;margin-top:2px">${Number(x.duration_sec).toFixed(2)}s</div>` : ''}
            ${x.failure_streak > 0 ? `<div class="muted mono" style="font-size:11px;color:var(--err)">连败 ${Number(x.failure_streak)} 次</div>` : ''}
          </td>
          <td class="mono" style="font-size:11.5px">
            <div>上: ${esc(fmtShortTs(x.last_run_at))}</div>
            <div class="muted">下: ${esc(fmtShortTs(x.next_run_at))}</div>
          </td>
          <td>
            ${x.has_log_preview
              ? `<button class="btn sm" data-source="${esc(x.source)}" data-id="${esc(x.raw_id)}" data-name="${esc(x.name)}" onclick="previewScheduleOutput(this.dataset.source, this.dataset.id, this.dataset.name)">📄 输出/日志</button>`
              : '<span class="muted">-</span>'}
          </td>
        </tr>`).join('')}</tbody>
    </table>`;
}

async function refreshOpsOnly() {
  try {
    STATE.ops = await api('/api/ops');
    renderOps();
    renderOverview();
    showToast('运维与调度状态已刷新', 'ok');
  } catch (e) {
    showToast('刷新运维状态失败：' + e.message, 'err');
  }
}

async function restorePushRuleAction(ruleId) {
  try {
    const res = await api('/api/ops/push-guard/restore', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ rule_id: ruleId }),
    });
    showToast(res.reply || `已恢复规则 ${ruleId} 即时推送`, 'ok');
    STATE.ops = await api('/api/ops');
    renderOps();
    renderOverview();
  } catch (e) {
    showToast('恢复推送失败：' + e.message, 'err');
  }
}

async function setSimingPauseAction(action) {
  const dur = (document.getElementById('ops-pause-dur') || {}).value || '2h';
  try {
    const res = await api('/api/ops/pause', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ action, duration: dur }),
    });
    showToast(res.reply || '后台执行状态已更新', 'ok');
    STATE.ops = await api('/api/ops');
    renderOps();
    renderOverview();
  } catch (e) {
    showToast('更新暂停状态失败：' + e.message, 'err');
  }
}

async function runOpsDiagnoseAction(allowHeal) {
  const btn = document.getElementById(allowHeal ? 'btn-ops-heal' : 'btn-ops-diag');
  if (btn) btn.disabled = true;
  showToast(allowHeal ? '正在执行链路体检与白名单 DNS 缓存清理…' : '正在执行只读链路体检…', 'ok');
  try {
    const res = await api('/api/ops/diagnose', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ allow_heal: !!allowHeal }),
    });
    const rcN = (res.root_causes || []).length;
    showToast(rcN === 0 ? '🟢 全链路体检通过' : `⚠️ 检出 ${rcN} 项根因线索，已展示在右侧面板`, rcN === 0 ? 'ok' : 'err');
    STATE.ops = await api('/api/ops');
    renderOps();
    renderOverview();
  } catch (e) {
    showToast('链路体检失败：' + e.message, 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

async function previewScheduleOutput(source, id, name) {
  const card = document.getElementById('ops-preview-card');
  const titleEl = document.getElementById('ops-preview-title');
  const subEl = document.getElementById('ops-preview-sub');
  const bodyEl = document.getElementById('ops-preview-body');
  if (!card || !bodyEl) return;
  card.style.display = 'block';
  titleEl.textContent = `📄 只读预览：${name || id}`;
  subEl.textContent = '正在加载最近产物 / 日志…';
  bodyEl.textContent = '';
  card.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
  try {
    const res = await api('/api/ops/schedule-output?source=' + encodeURIComponent(source) + '&id=' + encodeURIComponent(id));
    subEl.textContent = `${res.source} · ${res.filename || res.id} · status=${res.kind || 'ok'}`;
    bodyEl.textContent = res.preview || '（无内容）';
  } catch (e) {
    subEl.textContent = '加载失败';
    bodyEl.textContent = '错误：' + e.message;
  }
}

async function refreshAll() {
  const [ov, ad, cm, md, ops, fin, fc, rpl, sh, wk, cf, ms, rt, lr, gv] = await Promise.all([
    api('/api/overview'),
    api('/api/admin/items'),
    api('/api/compute'),
    api('/api/modules'),
    api('/api/ops'),
    api('/api/finance'),
    api('/api/finance/charts'),
    api('/api/replenish'),
    api('/api/smarthome'),
    api('/api/wiki'),
    api('/api/wiki/conflicts'),
    api('/api/msim'),
    api('/api/routines'),
    api('/api/learned'),
    api('/api/graph/visual'),
  ]);
  STATE.overview = ov;
  STATE.admin = ad;
  STATE.compute = cm;
  STATE.modules = md;
  STATE.ops = ops;
  STATE.finance = fin;
  STATE.financeCharts = fc;
  STATE.replenish = rpl;
  STATE.smarthome = sh;
  STATE.wiki = wk;
  STATE.conflicts = cf;
  STATE.msim = ms;
  STATE.learned = lr;
  STATE.graph = gv;

  renderOverview();
  renderCompute();
  renderModulesView();
  renderOps();
  renderFinance();
  renderReplenish();
  renderSmartHome();
  renderWikiSidebar();
  renderConflictsView();
  if (!STATE.selectedWikiSlug && wk.pages && wk.pages.length) {
    selectWikiPage(wk.pages[0].slug);
  }
  renderMsim();
  document.getElementById('routines-editor').value = rt.content || '';
  document.getElementById('routines-meta').textContent = `路径: ${rt.path || 'routines.md'} · 保存前自动在 tmp_root/tower-backups/ 落盘 .bak 备份并写审计`;
  renderLearned();
  renderKnowledgeGraph();

  // 冷启动后台热身完成后的单次静默回刷（确保 Nous / Magpie 实时余额自动到位）
  if (cm && cm.cache_state === 'warming' && !STATE.computeWarmRetried) {
    STATE.computeWarmRetried = true;
    setTimeout(async () => {
      try {
        STATE.compute = await api('/api/compute');
        renderCompute();
        renderOverview();
      } catch (_) {}
    }, 4000);
  }
}

initTabFromUrl();
refreshAll().catch(e => {
  document.getElementById('ts').textContent = '连接异常: ' + e.message;
  showToast('加载控制塔数据失败：' + e.message, 'err');
});
</script>
</body>
</html>"""


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
