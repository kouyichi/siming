"""Wave 1 (W1-2 · compute_assets)：多平台 AI 算力资产总控、烧钱熔断器、404 自动降级路由与 Sure 成本同步。

覆盖平台：DeepSeek / Aliyun / Kimi / Nous / RedPill / Claude / Codex / Magpie。
核心护栏：
1. 日消耗 >= 2x 均耗 -> 自动置 `CIRCUIT_OPEN` 并推荐降级备用模型；
2. 低余额预警（CNY < ¥200 / USD < $30）与节省计划到期倒计时（默认 14 天）；
3. 404 / 429 / 5xx 或熔断触发时一键路由至健康模型；
4. 按 `work_cw08` vs `personal` 归属月度汇总同步至 Sure。
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from siming import db as _db_mod
from siming.db import SimingDB, iso_now, now_local, parse_iso
from siming.modules import is_module_enabled

USD_TO_CNY_RATE = 7.25

_FX_TO_CNY: dict[str, float] = {
    "CNY": 1.0,
    "RMB": 1.0,
    "USD": USD_TO_CNY_RATE,
    "JPY": 0.048,
    "EUR": 7.85,
    "GBP": 9.15,
    "SGD": 5.40,
    "HKD": 0.93,
}


def _fx_rate(currency: str) -> float:
    return float(_FX_TO_CNY.get((currency or "CNY").strip().upper(), 1.0))

DEFAULT_FALLBACK_MODELS: dict[str, tuple[str, str]] = {
    "claude": ("deepseek", "deepseek-chat"),
    "codex": ("deepseek", "deepseek-reasoner"),
    "nous": ("deepseek", "deepseek-chat"),
    "redpill": ("aliyun", "qwen-max"),
    "deepseek": ("aliyun", "qwen-plus"),
    "aliyun": ("kimi", "kimi-k2"),
    "kimi": ("deepseek", "deepseek-chat"),
    "magpie": ("deepseek", "deepseek-chat"),
}

PROVIDER_DEFAULT_MODELS: dict[str, str] = {
    "deepseek": "deepseek-chat",
    "aliyun": "qwen-max",
    "kimi": "kimi-k2",
    "nous": "hermes-4-70b",
    "redpill": "claude-sonnet-4",
    "claude": "claude-sonnet-4",
    "codex": "gpt-5-codex",
    "magpie": "magpie-pro",
}


def _slugify(val: str) -> str:
    s = re.sub(r"[^a-zA-Z0-9_-]+", "-", (val or "").strip().lower()).strip("-")
    return s[:32] or "default"


def _make_asset_id(provider: str, account_label: str) -> str:
    p = _slugify(provider)
    lbl = _slugify(account_label)
    return f"CA-{p}-{lbl}"


def _parse_expiry_dt(val: str) -> Optional[datetime]:
    s = (val or "").strip()
    if not s:
        return None
    try:
        if len(s) == 10:
            return parse_iso(f"{s}T23:59:59+08:00")
        return parse_iso(s)
    except Exception:
        return None


def upsert_compute_asset(
    db: SimingDB,
    provider: str,
    account_label: str,
    balance: float,
    currency: str = "CNY",
    daily_spend: float = 0.0,
    avg_daily_spend: float = 0.0,
    plan_name: str = "",
    plan_expires_at: str = "",
    billing_scope: str = "personal",
    fallback_model: str = "",
    meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """登记或刷新单个 AI 算力平台账户余额、日烧钱速率、节省计划到期日与公私归属。"""
    db.init_db()
    prov = (provider or "").strip().lower()
    if not prov:
        return {"ok": False, "error": "provider 不能为空"}

    cur = (currency or "CNY").strip().upper()
    scope = (billing_scope or "personal").strip()
    if scope not in ("work_cw08", "personal"):
        scope = "work_cw08" if "cw08" in scope.lower() or "work" in scope.lower() else "personal"

    asset_id = _make_asset_id(prov, account_label)
    bal = round(float(balance), 4)
    d_spend = round(max(0.0, float(daily_spend)), 4)
    avg_spend = round(max(0.0, float(avg_daily_spend)), 4)
    fb = (fallback_model or "").strip() or DEFAULT_FALLBACK_MODELS.get(prov, ("deepseek", "deepseek-chat"))[1]
    ts = iso_now()

    # 初评状态（精细熔断由 evaluate_compute_guardrails 执行）
    status = "OK"
    if avg_spend > 0 and d_spend >= avg_spend * 2.0:
        status = "BURN_SPIKE"
    elif (cur == "CNY" and bal < 200.0) or (cur == "USD" and bal < 30.0):
        status = "LOW_BALANCE"

    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO compute_assets (
                asset_id, provider, account_label, currency, balance,
                daily_spend, avg_daily_spend, plan_name, plan_expires_at,
                billing_scope, fallback_model, status, meta_json, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(asset_id) DO UPDATE SET
                provider = excluded.provider,
                account_label = excluded.account_label,
                currency = excluded.currency,
                balance = excluded.balance,
                daily_spend = excluded.daily_spend,
                avg_daily_spend = excluded.avg_daily_spend,
                plan_name = excluded.plan_name,
                plan_expires_at = excluded.plan_expires_at,
                billing_scope = excluded.billing_scope,
                fallback_model = excluded.fallback_model,
                status = excluded.status,
                meta_json = excluded.meta_json,
                updated_at = excluded.updated_at
            """,
            (
                asset_id,
                prov,
                (account_label or "").strip(),
                cur,
                bal,
                d_spend,
                avg_spend,
                (plan_name or "").strip(),
                (plan_expires_at or "").strip(),
                scope,
                fb,
                status,
                json.dumps(meta or {}, ensure_ascii=False),
                ts,
            ),
        )

    return {
        "ok": True,
        "asset_id": asset_id,
        "provider": prov,
        "account_label": (account_label or "").strip(),
        "currency": cur,
        "balance": bal,
        "daily_spend": d_spend,
        "avg_daily_spend": avg_spend,
        "plan_name": (plan_name or "").strip(),
        "plan_expires_at": (plan_expires_at or "").strip(),
        "billing_scope": scope,
        "fallback_model": fb,
        "status": status,
        "updated_at": ts,
    }


def evaluate_compute_guardrails(
    db: SimingDB,
    now: Optional[datetime] = None,
    notify: bool = False,
    spike_ratio: float = 2.0,
    low_balance_cny: float = 200.0,
    low_balance_usd: float = 30.0,
    expiry_warn_days: int = 14,
) -> dict[str, Any]:
    """巡检全部 AI 算力平台：
    - 消耗飙升 (>= spike_ratio × 均耗) -> 自动熔断 `CIRCUIT_OPEN` 并给出降级模型；
    - 余额告急 (< low_balance_cny / low_balance_usd)；
    - 节省计划/套餐到期倒计时 (<= expiry_warn_days 天)。
    """
    db.init_db()
    cur_dt = now or _db_mod.now_local()
    ts = iso_now(cur_dt)

    conn = db.connect()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM compute_assets ORDER BY provider, asset_id").fetchall()]
    finally:
        conn.close()

    alerts: list[dict[str, Any]] = []
    circuit_open: list[dict[str, Any]] = []
    low_balance: list[dict[str, Any]] = []
    expiring: list[dict[str, Any]] = []

    with db.transaction() as wconn:
        for row in rows:
            aid = str(row["asset_id"])
            prov = str(row["provider"])
            lbl = str(row["account_label"] or prov)
            curr = str(row["currency"] or "CNY").upper()
            bal = float(row["balance"] or 0.0)
            d_spend = float(row["daily_spend"] or 0.0)
            avg_spend = float(row["avg_daily_spend"] or 0.0)
            fb = str(row["fallback_model"] or "") or DEFAULT_FALLBACK_MODELS.get(prov, ("deepseek", "deepseek-chat"))[1]

            new_status = "OK"

            # 1) 烧钱速率飙升 -> 立即熔断
            if avg_spend > 0.0 and d_spend >= avg_spend * float(spike_ratio):
                ratio = round(d_spend / avg_spend, 2)
                new_status = "CIRCUIT_OPEN"
                item = {
                    "asset_id": aid,
                    "provider": prov,
                    "account_label": lbl,
                    "kind": "burn_spike",
                    "currency": curr,
                    "daily_spend": d_spend,
                    "avg_daily_spend": avg_spend,
                    "ratio": ratio,
                    "fallback_model": fb,
                    "message": f"🔥 {prov}/{lbl} 日耗 {curr} {d_spend:.2f}（{ratio}x 均耗 {avg_spend:.2f}），已触发 CIRCUIT_OPEN，建议降级至 `{fb}`",
                }
                circuit_open.append(item)
                alerts.append(item)

            # 2) 低余额告警
            low_thresh = float(low_balance_usd) if curr == "USD" else float(low_balance_cny)
            if bal < low_thresh:
                if new_status == "OK":
                    new_status = "LOW_BALANCE"
                item = {
                    "asset_id": aid,
                    "provider": prov,
                    "account_label": lbl,
                    "kind": "low_balance",
                    "currency": curr,
                    "balance": bal,
                    "threshold": low_thresh,
                    "message": f"💸 {prov}/{lbl} 余额告急：{curr} {bal:.2f}（低于阈值 {low_thresh:.0f}）",
                }
                low_balance.append(item)
                alerts.append(item)

            # 3) 节省计划到期倒计时
            exp_dt = _parse_expiry_dt(str(row.get("plan_expires_at") or ""))
            if exp_dt is not None:
                days_left = int((exp_dt.date() - cur_dt.date()).days)
                if days_left <= int(expiry_warn_days):
                    if new_status == "OK":
                        new_status = "EXPIRING"
                    if days_left < 0:
                        exp_phrase = f"已到期 {abs(days_left)} 天"
                    elif days_left == 0:
                        exp_phrase = "今日到期"
                    else:
                        exp_phrase = f"距到期仅剩 {days_left} 天"
                    item = {
                        "asset_id": aid,
                        "provider": prov,
                        "account_label": lbl,
                        "kind": "plan_expiring",
                        "plan_name": str(row.get("plan_name") or "节省计划"),
                        "plan_expires_at": str(row.get("plan_expires_at") or ""),
                        "days_left": days_left,
                        "message": f"⏳ {prov}/{lbl}「{row.get('plan_name') or '套餐'}」{exp_phrase}（{row.get('plan_expires_at')}）",
                    }
                    expiring.append(item)
                    alerts.append(item)

            if new_status != str(row.get("status") or ""):
                wconn.execute(
                    "UPDATE compute_assets SET status = ?, updated_at = ? WHERE asset_id = ?",
                    (new_status, ts, aid),
                )

    notified = False
    if notify and alerts and is_module_enabled(db, "compute_assets"):
        day_key = f"compute_guardrails_notified:{cur_dt.date().isoformat()}"
        try:
            seen_keys = set(json.loads(db.get_meta(day_key) or "[]"))
        except Exception:
            seen_keys = set()
        fresh_alerts: list[dict[str, Any]] = []
        fresh_keys: list[str] = []
        for a in alerts:
            akey = f"{a['asset_id']}:{a['kind']}"
            if akey not in seen_keys:
                fresh_alerts.append(a)
                fresh_keys.append(akey)
        if fresh_alerts:
            from siming.push import push_message

            lines = ["⚡ **AI 算力资产护栏告警**"]
            lines.extend(f"• {a['message']}" for a in fresh_alerts[:8])
            lvl = "now" if any(a["kind"] == "burn_spike" for a in fresh_alerts) else "normal"
            res = push_message(
                db,
                text="\n".join(lines),
                rule_id="compute_guardrails",
                ref_id=f"compute_guardrails:{cur_dt.date().isoformat()}",
                level=lvl,
                dedup_window=timedelta(hours=24),
            )
            notified = bool(res.get("ok") and not res.get("skipped"))
            if res.get("ok"):
                seen_keys.update(fresh_keys)
                db.set_meta(day_key, json.dumps(sorted(seen_keys), ensure_ascii=False))

    return {
        "ok": True,
        "checked": len(rows),
        "alerts": alerts,
        "circuit_open": circuit_open,
        "low_balance": low_balance,
        "expiring": expiring,
        "notified": notified,
    }


def recommend_healthy_model(
    db: SimingDB,
    primary_provider: str,
    primary_model: str,
    error_code: int = 0,
) -> dict[str, Any]:
    """当主模型返回 404/429/5xx 或对应算力平台处于 `CIRCUIT_OPEN`/余额枯竭时，自动路由到健康备用模型。"""
    db.init_db()
    p_prov = (primary_provider or "").strip().lower()
    p_model = (primary_model or "").strip()

    conn = db.connect()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM compute_assets ORDER BY balance DESC").fetchall()]
    finally:
        conn.close()

    by_prov: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_prov.setdefault(str(r["provider"]).lower(), []).append(r)

    primary_rows = by_prov.get(p_prov, [])
    primary_circuit_open = any(str(r["status"]) == "CIRCUIT_OPEN" for r in primary_rows)
    primary_depleted = bool(primary_rows) and all(float(r["balance"] or 0.0) <= 0.0 for r in primary_rows)
    http_unhealthy = int(error_code or 0) in (404, 429, 500, 502, 503, 504)

    if not (http_unhealthy or primary_circuit_open or primary_depleted):
        return {
            "ok": True,
            "switched": False,
            "provider": p_prov,
            "model": p_model,
            "primary_provider": p_prov,
            "primary_model": p_model,
            "reason": "primary_healthy",
        }

    reason_parts: list[str] = []
    if http_unhealthy:
        reason_parts.append(f"http_{int(error_code)}")
    if primary_circuit_open:
        reason_parts.append("circuit_open")
    if primary_depleted:
        reason_parts.append("balance_depleted")
    reason = "+".join(reason_parts)

    # 1) 优先读取该平台显式配置的 fallback_model（若其目标 provider 未熔断）
    def _provider_for_model(model_name: str) -> str:
        low = model_name.lower()
        for prov_key, hints in (
            ("deepseek", ("deepseek",)),
            ("aliyun", ("qwen", "dashscope", "aliyun")),
            ("kimi", ("kimi", "moonshot")),
            ("claude", ("claude", "sonnet", "opus")),
            ("codex", ("gpt", "codex", "o3", "o4")),
            ("nous", ("hermes", "nous")),
            ("redpill", ("redpill",)),
        ):
            if any(h in low for h in hints):
                return prov_key
        return DEFAULT_FALLBACK_MODELS.get(p_prov, ("deepseek", "deepseek-chat"))[0]

    def _is_provider_healthy(prov_key: str) -> bool:
        p_list = by_prov.get(prov_key, [])
        if not p_list:
            return True
        return any(str(r["status"]) != "CIRCUIT_OPEN" and float(r["balance"] or 0.0) > 0.0 for r in p_list)

    for pr in primary_rows:
        cand_model = str(pr.get("fallback_model") or "").strip()
        if cand_model and cand_model != p_model:
            cand_prov = _provider_for_model(cand_model)
            if cand_prov != p_prov and _is_provider_healthy(cand_prov):
                return {
                    "ok": True,
                    "switched": True,
                    "provider": cand_prov,
                    "model": cand_model,
                    "primary_provider": p_prov,
                    "primary_model": p_model,
                    "reason": reason,
                }

    # 2) 从库内其他健康平台里按余额优先挑一个可用主力模型
    for r in rows:
        prov_k = str(r["provider"]).lower()
        if prov_k == p_prov:
            continue
        if str(r["status"]) == "CIRCUIT_OPEN" or float(r["balance"] or 0.0) <= 0.0:
            continue
        cand_model = PROVIDER_DEFAULT_MODELS.get(prov_k, f"{prov_k}-default")
        return {
            "ok": True,
            "switched": True,
            "provider": prov_k,
            "model": cand_model,
            "primary_provider": p_prov,
            "primary_model": p_model,
            "reason": reason,
        }

    # 3) 最终兜底：内置静态降级表
    fb_prov, fb_model = DEFAULT_FALLBACK_MODELS.get(p_prov, ("deepseek", "deepseek-chat"))
    return {
        "ok": True,
        "switched": True,
        "provider": fb_prov,
        "model": fb_model,
        "primary_provider": p_prov,
        "primary_model": p_model,
        "reason": reason,
    }


def sync_compute_costs_to_sure(
    db: SimingDB,
    month: Optional[str] = None,
    http_fn: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """按 `work_cw08` 与 `personal` 归属汇总月度算力成本，并写入 Sure 同步映射表。"""
    db.init_db()
    ym = (month or _db_mod.now_local().strftime("%Y-%m")).strip()

    conn = db.connect()
    try:
        assets = [dict(r) for r in conn.execute("SELECT * FROM compute_assets").fetchall()]
        usage_cny_row = conn.execute(
            "SELECT COALESCE(SUM(cost_cny), 0.0) AS total FROM usage WHERE ts LIKE ?",
            (f"{ym}%",),
        ).fetchone()
    finally:
        conn.close()

    totals: dict[str, float] = {"work_cw08": 0.0, "personal": 0.0}
    details: dict[str, list[dict[str, Any]]] = {"work_cw08": [], "personal": []}

    for a in assets:
        scope = str(a.get("billing_scope") or "personal")
        if scope not in totals:
            scope = "personal"
        curr = str(a.get("currency") or "CNY").upper()
        rate = _fx_rate(curr)
        daily_base = float(a.get("avg_daily_spend") or 0.0) or float(a.get("daily_spend") or 0.0)
        est_month_cny = round(daily_base * 30.0 * rate, 2)
        totals[scope] = round(totals[scope] + est_month_cny, 2)
        details[scope].append(
            {
                "asset_id": a["asset_id"],
                "provider": a["provider"],
                "account_label": a["account_label"],
                "currency": curr,
                "est_month_cny": est_month_cny,
            }
        )

    usage_extra = round(float(usage_cny_row["total"] or 0.0), 2) if usage_cny_row else 0.0
    if usage_extra > 0:
        totals["personal"] = round(totals["personal"] + usage_extra, 2)

    ts = iso_now()
    synced_records: list[dict[str, Any]] = []
    for scope, amount_cny in totals.items():
        local_id = f"compute:{ym}:{scope}"
        sure_account = "AI算力·CW08公费" if scope == "work_cw08" else "AI算力·个人自费"
        payload = {
            "month": ym,
            "billing_scope": scope,
            "amount_cny": amount_cny,
            "tags": ["AI算力", "CW08", "public"] if scope == "work_cw08" else ["AI算力", "private"],
            "account_name": sure_account,
            "breakdown": details[scope],
        }
        p_hash = hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        sure_id = f"sure-comp-{p_hash[:12]}"
        status = "SYNCED"

        if http_fn is not None:
            try:
                resp = http_fn("POST", "http://127.0.0.1:3032/api/v1/transactions", payload, {})
                if resp.get("ok"):
                    d = resp.get("data") if isinstance(resp.get("data"), dict) else {}
                    sure_id = str(d.get("id") or d.get("sure_id") or sure_id)
                else:
                    status = "FAILED"
            except Exception:
                status = "FAILED"

        with db.transaction() as wconn:
            wconn.execute(
                """
                INSERT INTO sure_sync_map (
                    local_kind, local_id, sure_id, sure_account, payload_hash, last_synced_at, status
                ) VALUES ('compute_monthly', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(local_kind, local_id) DO UPDATE SET
                    sure_id = excluded.sure_id,
                    sure_account = excluded.sure_account,
                    payload_hash = excluded.payload_hash,
                    last_synced_at = excluded.last_synced_at,
                    status = excluded.status
                """,
                (local_id, sure_id, sure_account, p_hash, ts, status),
            )
        synced_records.append(
            {"local_id": local_id, "scope": scope, "amount_cny": amount_cny, "sure_id": sure_id, "status": status}
        )

    return {
        "ok": all(r["status"] == "SYNCED" for r in synced_records),
        "month": ym,
        "work_cw08_cny": totals["work_cw08"],
        "personal_cny": totals["personal"],
        "total_cny": round(totals["work_cw08"] + totals["personal"], 2),
        "synced_records": synced_records,
    }


def format_compute_dashboard(db: SimingDB, now: Optional[datetime] = None) -> str:
    """格式化多平台 AI 算力资产总控面板。"""
    db.init_db()
    cur_dt = now or _db_mod.now_local()
    conn = db.connect()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM compute_assets ORDER BY billing_scope, provider").fetchall()]
    finally:
        conn.close()

    if not rows:
        return "⚡ **AI 算力资产总控**：暂未登记任何 API 算力账户。"

    lines = ["⚡ **AI 算力资产总控面板**"]
    total_cny = 0.0
    for r in rows:
        curr = str(r["currency"] or "CNY").upper()
        bal = float(r["balance"] or 0.0)
        total_cny += bal * _fx_rate(curr)
        st = str(r["status"] or "OK")
        icon = {"OK": "🟢", "LOW_BALANCE": "🟡", "EXPIRING": "⏳", "BURN_SPIKE": "🔥", "CIRCUIT_OPEN": "🛑"}.get(st, "⚪")
        scope_tag = "公费CW08" if r["billing_scope"] == "work_cw08" else "个人"
        exp_str = ""
        exp_dt = _parse_expiry_dt(str(r.get("plan_expires_at") or ""))
        if exp_dt is not None:
            d_left = (exp_dt.date() - cur_dt.date()).days
            exp_str = f" · 套餐剩{d_left}天"
        lines.append(
            f"• {icon} **{r['provider']}**（{r['account_label'] or '默认'} · {scope_tag}）："
            f"余额 `{curr} {bal:.2f}` ｜ 日耗 `{float(r['daily_spend'] or 0.0):.2f}`"
            f"（均 `{float(r['avg_daily_spend'] or 0.0):.2f}`）｜ 状态 `{st}`{exp_str}"
        )
    lines.append(f"• 折合人民币总余额：**¥{total_cny:,.2f}**")
    return "\n".join(lines)
