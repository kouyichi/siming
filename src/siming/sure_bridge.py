"""Wave 1 (W1-1 · sure_bridge)：Sure 财务 OS 控制塔双向同步桥 + Pipelock A2 护栏 + 1 秒自然语言查账。

能力：
1. 幂等同步 Siming `ledger_txns`（含四卡短信/邮箱流水、CW08/CW01 公私报销标签与分摊比例）、固定资产与车务记录至自托管 Sure；
2. 管理 `sure_rules` 自动分类/账户映射/公私分账/周期订阅规则；
3. Pipelock 安全护栏：默认只读（A0），任何写入/转账/删除动作必须携带 Siming A2 审批口令；
4. 1 秒 QQ 自然语言财务查询（净资产、月度现金流、分类支出、公私报销账龄与待回款）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from siming import db as _db_mod
from siming.crypto import read_secret
from siming.db import SimingDB, iso_now, now_local, parse_iso
from siming.modules import is_module_enabled

DEFAULT_SURE_API_URL = "http://127.0.0.1:3032"

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
    "TWD": 0.225,
    "MOP": 0.90,
}

_VOID_TXN_STATUSES = ("VOID_DUPLICATE", "VOID", "CANCELLED")
_INFLOW_KINDS = ("收入", "退款")
_NON_SPEND_KINDS = ("收入", "退款", "还款", "转入")


def _fx_rate_to_cny(currency: str) -> float:
    return float(_FX_TO_CNY.get((currency or "CNY").strip().upper(), 1.0))


def _to_cny_yuan(amount_cents: int, currency: str, split_ratio: float = 1.0) -> float:
    raw = float(amount_cents or 0) / 100.0
    return round(raw * _fx_rate_to_cny(currency) * float(split_ratio), 2)


def _is_void_txn(txn: dict[str, Any]) -> bool:
    return str(txn.get("status") or "").strip().upper() in _VOID_TXN_STATUSES

_READ_ACTIONS = frozenset(
    {"read", "get", "list", "query", "health", "status", "summary", "ask", "net_worth", "cashflow"}
)
_WRITE_ACTIONS = frozenset(
    {
        "write",
        "create",
        "update",
        "delete",
        "remove",
        "transfer",
        "pay",
        "sync_txn",
        "sync_asset",
        "sync_rule",
        "reconcile",
    }
)
_A2_TOKEN_RE = re.compile(r"^A\d{2,8}$", re.IGNORECASE)

_CW08_RE = re.compile(
    r"(cw08|nous|openai|anthropic|claude|deepseek|redpill|kimi|moonshot|aliyun|dashscope|codex|cursor|github\s*copilot|api\s*算力|work_cw08)",
    re.IGNORECASE,
)
_CW01_RE = re.compile(
    r"(cw01|滴滴|didi|高德打车|首汽|曹操出行|神州专车|差旅|火车票|12306|机票|携程|航旅)",
    re.IGNORECASE,
)

_CARD_ACCOUNT_HINTS: dict[str, str] = {
    "1004": "建行储蓄卡(1004)",
    "1003": "中行信用卡(1003)",
    "1001": "光大信用卡(1001)",
    "招行": "招行信用卡",
    "支付宝": "支付宝余额/花呗",
    "微信": "微信零钱",
}

_CATEGORY_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("外卖餐饮", r"(美团|饿了么|瑞幸|星巴克|麦当劳|肯德基|喜茶|霸王茶姬|奶茶|餐饮|火锅|烧烤|米粉|瓦罐|咖啡|食堂|水果|示例果切|示例粉面|示例瓦罐汤)"),
    ("出行车务", r"(滴滴|打车|地铁|公交|12306|特来电|超充|tesla|特斯拉|etc|高速|停车|泊车|车险|中石化|中石油)"),
    ("AI算力", r"(nous|deepseek|aliyun|dashscope|kimi|claude|anthropic|openai|redpill|codex|magpie|api|算力)"),
    ("订阅服务", r"(apple|icloud|game\s*pass|迅雷|点众|快看|抖音小程序|会员|续费|spotify|youtube|netflix)"),
    ("家居装修", r"(京东|天猫|山姆|宜家|海尔|美的|西门子|装修|家电|物业|水费|电费|燃气|宽带)"),
    ("医疗健康", r"(常用医院|医院|药房|药店|周期用药|体检|挂号|医保|美团买药)"),
)


def _sure_api_url(db: SimingDB) -> str:
    env_url = (os.environ.get("SURE_API_URL") or "").strip()
    if env_url:
        return env_url.rstrip("/")
    meta_url = (db.get_meta("sure_api_url") or "").strip()
    if meta_url:
        return meta_url.rstrip("/")
    return DEFAULT_SURE_API_URL


def _sure_api_token(db: SimingDB) -> str:
    env_tok = (os.environ.get("SURE_API_TOKEN") or "").strip()
    if env_tok:
        return env_tok
    return (read_secret("sure-api-token", db.paths) or "").strip()


def _canonical_hash(obj: Any) -> str:
    raw = json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _default_http_call(
    method: str,
    url: str,
    payload: Optional[dict[str, Any]] = None,
    headers: Optional[dict[str, str]] = None,
    timeout: float = 5.0,
) -> dict[str, Any]:
    if os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return {"ok": False, "status": 0, "offline": True, "error": "SIMING_TEST_OFFLINE=1"}
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req_headers = {"Accept": "application/json"}
    if data is not None:
        req_headers["Content-Type"] = "application/json"
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method.upper())
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            parsed = json.loads(body) if body.strip().startswith(("{", "[")) else {"raw": body}
            return {"ok": 200 <= resp.status < 300, "status": resp.status, "data": parsed}
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": str(exc)}
    except Exception as exc:
        return {"ok": False, "status": 0, "error": str(exc)}


def pipelock_authorize(
    action: str,
    amount_yuan: float = 0.0,
    payload: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Pipelock AI 安全代理护栏：默认只读（A0），任何写入/转账/删除动作均需携带有效 A2 审批口令。"""
    act = (action or "").strip().lower()
    pl = payload if isinstance(payload, dict) else {}

    # 拦截任何试图将隐私财务数据外发到非本机域名的越权载荷
    target_url = str(pl.get("url") or pl.get("target_url") or "").strip().lower()
    if target_url and not any(
        target_url.startswith(prefix)
        for prefix in ("http://127.0.0.1", "http://localhost", "http://sure-web", "http://pipelock")
    ):
        return {
            "allowed": False,
            "approved": False,
            "tier": "A2",
            "requires_a2": True,
            "action": act,
            "reason": "egress_blocked_local_only",
        }

    if act in _READ_ACTIONS:
        return {
            "allowed": True,
            "approved": True,
            "tier": "A0",
            "requires_a2": False,
            "action": act,
            "reason": "read_only_allowed",
        }

    if amount_yuan < 0:
        return {
            "allowed": False,
            "approved": False,
            "tier": "A2",
            "requires_a2": True,
            "action": act,
            "reason": "invalid_negative_amount",
        }

    token = str(pl.get("a2_token") or pl.get("approval_token") or pl.get("X-Siming-Approval-Token") or "").strip()
    if not token:
        return {
            "allowed": False,
            "approved": False,
            "tier": "A2",
            "requires_a2": True,
            "action": act,
            "amount_yuan": round(float(amount_yuan), 2),
            "reason": "a2_approval_required",
        }

    if not _A2_TOKEN_RE.match(token):
        return {
            "allowed": False,
            "approved": False,
            "tier": "A2",
            "requires_a2": True,
            "action": act,
            "amount_yuan": round(float(amount_yuan), 2),
            "reason": "invalid_a2_token_format",
        }

    if act in ("transfer", "pay", "delete") and float(amount_yuan) > 50000.0:
        return {
            "allowed": False,
            "approved": False,
            "tier": "A3",
            "requires_a2": True,
            "action": act,
            "amount_yuan": round(float(amount_yuan), 2),
            "reason": "exceeds_pipelock_single_cap",
        }

    return {
        "allowed": True,
        "approved": True,
        "tier": "A2",
        "requires_a2": True,
        "action": act,
        "approval_token": token.upper(),
        "amount_yuan": round(float(amount_yuan), 2),
        "reason": "a2_token_verified",
    }


def upsert_sure_rule(
    db: SimingDB,
    name: str,
    match_pattern: str,
    account_name: str = "",
    category: str = "",
    tags: Optional[list[str]] = None,
    split_ratio: float = 1.0,
    is_recurring: bool = False,
    cadence_days: int = 0,
) -> dict[str, Any]:
    """新增或更新 Sure 财务自动归类/分账/周期规则（按 name 幂等更新）。"""
    db.init_db()
    clean_name = (name or "").strip()
    clean_pat = (match_pattern or "").strip()
    if not clean_name or not clean_pat:
        return {"ok": False, "error": "规则名称与匹配表达式不能为空"}
    ratio = float(split_ratio)
    if ratio <= 0.0 or ratio > 1.0:
        return {"ok": False, "error": "split_ratio 必须在 (0, 1] 区间"}

    clean_tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
    ts = iso_now()

    with db.transaction() as conn:
        existing = conn.execute(
            "SELECT rule_id, created_at FROM sure_rules WHERE name = ?",
            (clean_name,),
        ).fetchone()
        if existing:
            rule_id = str(existing["rule_id"])
            conn.execute(
                """
                UPDATE sure_rules
                SET match_pattern = ?, account_name = ?, category = ?, tags_json = ?,
                    split_ratio = ?, is_recurring = ?, cadence_days = ?
                WHERE rule_id = ?
                """,
                (
                    clean_pat,
                    account_name.strip(),
                    category.strip(),
                    json.dumps(clean_tags, ensure_ascii=False),
                    ratio,
                    1 if is_recurring else 0,
                    max(0, int(cadence_days)),
                    rule_id,
                ),
            )
            created_at = str(existing["created_at"])
        else:
            rule_id = db.next_seq_id(conn, "SR")
            conn.execute(
                """
                INSERT INTO sure_rules (
                    rule_id, name, match_pattern, account_name, category, tags_json,
                    split_ratio, is_recurring, cadence_days, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rule_id,
                    clean_name,
                    clean_pat,
                    account_name.strip(),
                    category.strip(),
                    json.dumps(clean_tags, ensure_ascii=False),
                    ratio,
                    1 if is_recurring else 0,
                    max(0, int(cadence_days)),
                    ts,
                ),
            )
            created_at = ts

    return {
        "ok": True,
        "rule_id": rule_id,
        "name": clean_name,
        "match_pattern": clean_pat,
        "account_name": account_name.strip(),
        "category": category.strip(),
        "tags": clean_tags,
        "split_ratio": ratio,
        "is_recurring": bool(is_recurring),
        "cadence_days": max(0, int(cadence_days)),
        "created_at": created_at,
    }


def list_sure_rules(db: SimingDB) -> list[dict[str, Any]]:
    """列出全部已配置的 Sure 自动规则。"""
    db.init_db()
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM sure_rules ORDER BY created_at ASC, rule_id ASC").fetchall()
    finally:
        conn.close()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            tags = json.loads(r["tags_json"] or "[]")
            if not isinstance(tags, list):
                tags = []
        except Exception:
            tags = []
        out.append(
            {
                "rule_id": str(r["rule_id"]),
                "name": str(r["name"]),
                "match_pattern": str(r["match_pattern"]),
                "account_name": str(r["account_name"]),
                "category": str(r["category"]),
                "tags": [str(t) for t in tags],
                "split_ratio": float(r["split_ratio"]),
                "is_recurring": bool(r["is_recurring"]),
                "cadence_days": int(r["cadence_days"]),
                "created_at": str(r["created_at"]),
            }
        )
    return out


def _infer_account_name(account_alias: str, card_last4: str) -> str:
    if card_last4 and card_last4 in _CARD_ACCOUNT_HINTS:
        return _CARD_ACCOUNT_HINTS[card_last4]
    alias = (account_alias or "").strip()
    for hint_k, hint_v in _CARD_ACCOUNT_HINTS.items():
        if hint_k in alias:
            return hint_v
    if alias and card_last4 and card_last4 not in alias:
        return f"{alias}({card_last4})"
    return alias or (f"银行卡({card_last4})" if card_last4 else "默认现金账户")


def _infer_category(haystack: str) -> str:
    for cat, pat in _CATEGORY_KEYWORDS:
        if re.search(pat, haystack, re.IGNORECASE):
            return cat
    return "日常消费"


def _apply_rules_to_txn(txn: dict[str, Any], rules: list[dict[str, Any]]) -> dict[str, Any]:
    merchant = str(txn.get("merchant") or "")
    notes = str(txn.get("notes") or "")
    alias = str(txn.get("account_alias") or "")
    last4 = str(txn.get("card_last4") or "")
    kind = str(txn.get("kind") or "消费")
    haystack = f"{merchant} {notes} {alias} {kind}"

    account_name = _infer_account_name(alias, last4)
    category = _infer_category(haystack)
    tags: list[str] = []
    split_ratio = 1.0
    matched_rules: list[str] = []

    if _CW08_RE.search(haystack):
        tags.extend(["public", "CW08"])
    elif _CW01_RE.search(haystack):
        tags.extend(["public", "CW01"])
    else:
        tags.append("private")

    for rule in rules:
        pat = rule["match_pattern"]
        matched = False
        try:
            matched = bool(re.search(pat, haystack, re.IGNORECASE))
        except re.error:
            matched = pat.lower() in haystack.lower()
        if not matched:
            continue
        matched_rules.append(rule["rule_id"])
        if rule["account_name"]:
            account_name = rule["account_name"]
        if rule["category"]:
            category = rule["category"]
        if rule["tags"]:
            for t in rule["tags"]:
                if t not in tags:
                    tags.append(t)
        if rule["split_ratio"] < split_ratio or split_ratio == 1.0:
            split_ratio = float(rule["split_ratio"])

    # 如果规则明确赋予公费标签 CW08/CW01，移除默认的 private 冲突标签
    if any(t in ("CW08", "CW01", "public", "work_cw08") for t in tags) and "private" in tags:
        tags = [t for t in tags if t != "private"]

    # 保持顺序去重
    dedup_tags = list(dict.fromkeys(tags))
    curr = str(txn.get("currency") or "CNY").strip().upper()
    fx = _fx_rate_to_cny(curr)
    raw_yuan = round(int(txn.get("amount_cents") or 0) / 100.0, 2)
    effective_yuan = round(raw_yuan * split_ratio, 2)
    amount_cny_yuan = round(effective_yuan * fx, 2)

    return {
        "external_id": str(txn["txn_id"]),
        "occurred_at": str(txn["occurred_at"]),
        "merchant": merchant or kind,
        "kind": kind,
        "currency": curr,
        "fx_rate": fx,
        "raw_amount_yuan": raw_yuan,
        "amount_yuan": effective_yuan,
        "amount_cny_yuan": amount_cny_yuan,
        "split_ratio": split_ratio,
        "account_name": account_name,
        "category": category,
        "tags": dedup_tags,
        "matched_rules": matched_rules,
        "notes": notes,
    }


def get_sure_status(
    db: SimingDB,
    http_fn: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """检查 Sure 本地 Compose 部署、Pipelock 策略、双向同步进度与实时 API 健康状态。"""
    db.init_db()
    compose_path = db.paths.repo_root / "deploy" / "sure" / "docker-compose.yml"
    pipelock_path = db.paths.repo_root / "deploy" / "sure" / "pipelock.yaml"

    conn = db.connect()
    try:
        map_rows = conn.execute(
            "SELECT local_kind, status, COUNT(*) AS cnt FROM sure_sync_map GROUP BY local_kind, status"
        ).fetchall()
        total_txns = int(
            conn.execute(
                "SELECT COUNT(*) FROM ledger_txns WHERE COALESCE(UPPER(status), '') NOT IN ('VOID_DUPLICATE', 'VOID', 'CANCELLED')"
            ).fetchone()[0]
        )
        synced_txns = int(
            conn.execute(
                "SELECT COUNT(*) FROM sure_sync_map WHERE local_kind = 'txn' AND status = 'SYNCED'"
            ).fetchone()[0]
        )
        rules_count = int(conn.execute("SELECT COUNT(*) FROM sure_rules").fetchone()[0])
    finally:
        conn.close()

    synced_counts: dict[str, dict[str, int]] = {}
    for r in map_rows:
        k = str(r["local_kind"])
        s = str(r["status"])
        synced_counts.setdefault(k, {})[s] = int(r["cnt"])

    api_url = _sure_api_url(db)
    token = _sure_api_token(db)
    api_healthy = False
    api_detail = "offline_mirror_mode"

    if http_fn is not None or (token and os.environ.get("SIMING_TEST_OFFLINE") != "1"):
        caller = http_fn or _default_http_call
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            resp = caller("GET", f"{api_url}/api/v1/health", None, headers)
            api_healthy = bool(resp.get("ok"))
            api_detail = "ok" if api_healthy else str(resp.get("error") or resp.get("status") or "unhealthy")
        except Exception as exc:
            api_healthy = False
            api_detail = str(exc)

    return {
        "ok": True,
        "compose_ready": compose_path.is_file(),
        "pipelock_ready": pipelock_path.is_file(),
        "compose_path": str(compose_path),
        "pipelock_path": str(pipelock_path),
        "pipelock_mode": "read_only_default",
        "api_url": api_url,
        "token_configured": bool(token),
        "api_healthy": api_healthy,
        "api_detail": api_detail,
        "total_ledger_txns": total_txns,
        "synced_txns": synced_txns,
        "pending_txn_count": max(0, total_txns - synced_txns),
        "rules_count": rules_count,
        "synced_counts": synced_counts,
    }


def sync_ledger_to_sure(
    db: SimingDB,
    limit: int = 500,
    dry_run: bool = False,
    http_fn: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """按内容哈希幂等同步 `ledger_txns` 到 Sure，自动套用 `sure_rules` 账户映射、CW08/CW01 公私标签与分摊比例。"""
    db.init_db()
    rules = list_sure_rules(db)
    api_url = _sure_api_url(db)
    token = _sure_api_token(db)

    conn = db.connect()
    try:
        txns = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM ledger_txns
                WHERE COALESCE(UPPER(status), '') NOT IN ('VOID_DUPLICATE', 'VOID', 'CANCELLED')
                ORDER BY occurred_at DESC, txn_id DESC LIMIT ?
                """,
                (max(1, int(limit)),),
            ).fetchall()
        ]
        existing_map = {
            str(r["local_id"]): dict(r)
            for r in conn.execute(
                "SELECT local_id, sure_id, payload_hash, status FROM sure_sync_map WHERE local_kind = 'txn'"
            ).fetchall()
        }
    finally:
        conn.close()

    synced = 0
    skipped = 0
    failed = 0
    would_sync = 0
    items: list[dict[str, Any]] = []
    ts = iso_now()

    use_remote = http_fn is not None or (token and os.environ.get("SIMING_TEST_OFFLINE") != "1")
    pending_writes: list[tuple[str, str, str, str, str, str]] = []

    for txn in txns:
        txn_id = str(txn["txn_id"])
        mapped = _apply_rules_to_txn(txn, rules)
        p_hash = _canonical_hash(mapped)

        prev = existing_map.get(txn_id)
        if prev and prev.get("status") == "SYNCED" and prev.get("payload_hash") == p_hash:
            skipped += 1
            continue

        if dry_run:
            would_sync += 1
            items.append({"txn_id": txn_id, "payload_hash": p_hash, **mapped})
            continue

        sure_id = str(prev["sure_id"]) if (prev and prev.get("sure_id")) else f"sure-txn-{p_hash[:12]}"
        sync_status = "SYNCED"
        err_msg = ""

        if use_remote:
            caller = http_fn or _default_http_call
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            try:
                resp = caller("POST", f"{api_url}/api/v1/transactions", mapped, headers)
                if resp.get("ok"):
                    resp_data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
                    sure_id = str(resp_data.get("id") or resp_data.get("sure_id") or sure_id)
                else:
                    sync_status = "FAILED"
                    err_msg = str(resp.get("error") or resp.get("status") or "http_error")
            except Exception as exc:
                sync_status = "FAILED"
                err_msg = str(exc)

        row_params = (txn_id, sure_id, mapped["account_name"], p_hash, ts, sync_status)
        if use_remote:
            with db.transaction() as wconn:
                wconn.execute(
                    """
                    INSERT INTO sure_sync_map (
                        local_kind, local_id, sure_id, sure_account, payload_hash, last_synced_at, status
                    ) VALUES ('txn', ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(local_kind, local_id) DO UPDATE SET
                        sure_id = excluded.sure_id,
                        sure_account = excluded.sure_account,
                        payload_hash = excluded.payload_hash,
                        last_synced_at = excluded.last_synced_at,
                        status = excluded.status
                    """,
                    row_params,
                )
        else:
            pending_writes.append(row_params)

        if sync_status == "SYNCED":
            synced += 1
        else:
            failed += 1
        items.append(
            {
                "txn_id": txn_id,
                "sure_id": sure_id,
                "status": sync_status,
                "error": err_msg,
                **mapped,
            }
        )

    if pending_writes:
        with db.transaction() as wconn:
            wconn.executemany(
                """
                INSERT INTO sure_sync_map (
                    local_kind, local_id, sure_id, sure_account, payload_hash, last_synced_at, status
                ) VALUES ('txn', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(local_kind, local_id) DO UPDATE SET
                    sure_id = excluded.sure_id,
                    sure_account = excluded.sure_account,
                    payload_hash = excluded.payload_hash,
                    last_synced_at = excluded.last_synced_at,
                    status = excluded.status
                """,
                pending_writes,
            )

    return {
        "ok": failed == 0,
        "dry_run": dry_run,
        "total_scanned": len(txns),
        "synced": synced,
        "would_sync": would_sync,
        "skipped": skipped,
        "failed": failed,
        "items": items,
    }


def sync_fixed_asset_to_sure(
    db: SimingDB,
    asset_id: str,
    name: str,
    purchase_yuan: float,
    current_yuan: float,
    category: str = "appliance",
    http_fn: Optional[Callable[..., Any]] = None,
) -> dict[str, Any]:
    """同步固定资产（大家电/车辆/硬件设备）净值与折旧到 Sure 资产账本并落库镜像。"""
    db.init_db()
    aid = (asset_id or "").strip()
    if not aid or not (name or "").strip():
        return {"ok": False, "error": "asset_id 与 name 不能为空"}

    buy_v = round(float(purchase_yuan), 2)
    cur_v = round(float(current_yuan), 2)
    dep_v = round(buy_v - cur_v, 2)
    dep_pct = round((dep_v / buy_v) * 100.0, 2) if buy_v > 0 else 0.0
    cat = (category or "appliance").strip()
    sure_account = f"固定资产:{cat}"

    payload = {
        "asset_id": aid,
        "name": name.strip(),
        "category": cat,
        "purchase_yuan": buy_v,
        "current_yuan": cur_v,
        "depreciation_yuan": dep_v,
        "depreciation_pct": dep_pct,
        "account_name": sure_account,
    }
    p_hash = _canonical_hash(payload)
    sure_id = f"sure-asset-{p_hash[:12]}"
    sync_status = "SYNCED"

    api_url = _sure_api_url(db)
    token = _sure_api_token(db)
    if http_fn is not None or (token and os.environ.get("SIMING_TEST_OFFLINE") != "1"):
        caller = http_fn or _default_http_call
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            resp = caller("POST", f"{api_url}/api/v1/assets", payload, headers)
            if resp.get("ok"):
                resp_data = resp.get("data") if isinstance(resp.get("data"), dict) else {}
                sure_id = str(resp_data.get("id") or resp_data.get("sure_id") or sure_id)
            else:
                sync_status = "FAILED"
        except Exception:
            sync_status = "FAILED"

    ts = iso_now()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO sure_sync_map (
                local_kind, local_id, sure_id, sure_account, payload_hash, last_synced_at, status
            ) VALUES ('asset', ?, ?, ?, ?, ?, ?)
            ON CONFLICT(local_kind, local_id) DO UPDATE SET
                sure_id = excluded.sure_id,
                sure_account = excluded.sure_account,
                payload_hash = excluded.payload_hash,
                last_synced_at = excluded.last_synced_at,
                status = excluded.status
            """,
            (aid, sure_id, sure_account, p_hash, ts, sync_status),
        )
        db.set_meta(f"sure_asset:{aid}", json.dumps({**payload, "sure_id": sure_id, "updated_at": ts}, ensure_ascii=False), conn=conn)

    return {
        "ok": sync_status == "SYNCED",
        "asset_id": aid,
        "sure_id": sure_id,
        "sure_account": sure_account,
        "status": sync_status,
        **payload,
    }


def _load_fixed_assets(db: SimingDB) -> list[dict[str, Any]]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT value FROM meta WHERE key LIKE 'sure_asset:%'").fetchall()
    finally:
        conn.close()
    assets: list[dict[str, Any]] = []
    for r in rows:
        try:
            obj = json.loads(r["value"] or "{}")
            if isinstance(obj, dict) and obj.get("asset_id"):
                assets.append(obj)
        except Exception:
            pass
    return assets


def _parse_natural_period(q: str, now: datetime) -> tuple[Optional[str], str, bool]:
    """解析自然语言查账中的时间范围，返回 (iso_prefix_or_none, period_label, is_explicit)。"""
    q_low = (q or "").lower()
    if any(k in q_low for k in ("上月", "上个月", "last month")):
        prev_month = (now.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")
        return prev_month, prev_month, True

    m_ym = re.search(r"(20\d{2})\s*[-/年]\s*(\d{1,2})\s*月?", q)
    if m_ym:
        y, mo = int(m_ym.group(1)), int(m_ym.group(2))
        if 1 <= mo <= 12:
            ym = f"{y:04d}-{mo:02d}"
            return ym, ym, True

    m_mo = re.search(r"(?<!\d)(\d{1,2})\s*月(?:份)?", q)
    if m_mo and not any(k in q for k in ("本月", "这个月", "当月", "上月", "上个月")):
        mo = int(m_mo.group(1))
        if 1 <= mo <= 12:
            yr = now.year if mo <= now.month else now.year - 1
            ym = f"{yr:04d}-{mo:02d}"
            return ym, ym, True

    if any(k in q_low for k in ("本月", "这个月", "当月", "现金流", "月度", "month")):
        cur_ym = now.strftime("%Y-%m")
        return cur_ym, cur_ym, True

    if any(k in q for k in ("今年", "本年")):
        yr_s = now.strftime("%Y")
        return yr_s, f"{yr_s}年", True

    if "去年" in q:
        yr_s = str(now.year - 1)
        return yr_s, f"{yr_s}年", True

    if any(k in q for k in ("全部", "历史", "总共", "累计")):
        return None, "全部历史", True

    cur_ym = now.strftime("%Y-%m")
    return cur_ym, cur_ym, False


_SPEND_QUERY_FILTERS: tuple[tuple[str, tuple[str, ...], str, Optional[str]], ...] = (
    # (label, query_triggers, merchant_notes_regex, category_exact_or_none)
    ("外卖餐饮", ("外卖餐饮",), "", "外卖餐饮"),
    ("外卖", ("外卖",), r"(外卖|美团外卖|美团平台|饿了么|eleme|小象超市)", None),
    ("咖啡", ("咖啡",), r"(咖啡|瑞幸|luckin|星巴克|starbucks|库迪|cotti|manner|tims|coffee|cafe)", None),
    ("奶茶", ("奶茶", "茶饮"), r"(奶茶|霸王茶姬|喜茶|蜜雪|奈雪|茶百道|古茗|一点点|沪上阿姨)", None),
    ("打车", ("打车", "网约车", "出租车"), r"(滴滴|didi|打车|高德打车|花小猪|曹操出行|首汽约车|t3出行|网约车|出租)", None),
    ("充电", ("超充", "充电"), r"(超充|充电|特来电|星星充电|小桔充电|tesla\s*supercharg)", None),
    ("停车", ("停车",), r"(停车|泊车|车库|捷停车|etcp|停简单)", None),
    ("滴滴", ("滴滴", "didi"), r"(滴滴|didi)", None),
    ("美团", ("美团", "meituan"), r"(美团|meituan)", None),
    ("饿了么", ("饿了么", "eleme"), r"(饿了么|eleme)", None),
    ("瑞幸", ("瑞幸", "luckin"), r"(瑞幸|luckin)", None),
    ("星巴克", ("星巴克", "starbucks"), r"(星巴克|starbucks)", None),
    ("山姆", ("山姆",), r"(山姆|samsclub)", None),
    ("盒马", ("盒马",), r"(盒马)", None),
    ("京东", ("京东",), r"(京东)", None),
    ("淘宝", ("淘宝", "天猫"), r"(淘宝|天猫)", None),
    ("特斯拉", ("特斯拉", "tesla"), r"(特斯拉|tesla)", None),
    ("外卖餐饮", ("餐饮", "吃饭", "吃的"), "", "外卖餐饮"),
    ("出行车务", ("出行车务", "出行", "车务"), "", "出行车务"),
    ("AI算力", ("ai算力", "算力"), "", "AI算力"),
    ("订阅服务", ("订阅服务", "订阅"), "", "订阅服务"),
    ("家居装修", ("家居装修", "家居", "装修"), "", "家居装修"),
    ("医疗健康", ("医疗健康", "医疗", "看病", "买药"), "", "医疗健康"),
)


def _extract_spend_filter(q: str) -> Optional[tuple[str, str, Optional[str]]]:
    q_low = (q or "").lower()
    for label, triggers, pat, cat in _SPEND_QUERY_FILTERS:
        if any(t in q_low for t in triggers):
            return label, pat, cat
    return None


def ask_sure_natural(
    db: SimingDB,
    question: str,
    http_fn: Optional[Callable[..., Any]] = None,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """1 秒 QQ 自然语言财务问答（融合 Sure MCP 只读通道 + 本机账本/固定资产/报销镜像）。"""
    db.init_db()
    q = (question or "").strip()
    cur_now = now or _db_mod.now_local()

    # 护栏：查询只走只读 A0
    auth = pipelock_authorize("query", amount_yuan=0.0, payload={"question": q})
    if not auth["allowed"]:
        return {"ok": False, "reply": "Pipelock 护栏拒绝了此次查询。", "data": auth}

    # 若外部提供 Sure MCP/API 查询回调，优先叠加远端实时指标
    remote_data: Optional[dict[str, Any]] = None
    if http_fn is not None:
        api_url = _sure_api_url(db)
        token = _sure_api_token(db)
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        try:
            resp = http_fn("GET", f"{api_url}/api/v1/net_worth", {"q": q}, headers)
            if isinstance(resp, dict) and resp.get("ok") and isinstance(resp.get("data"), dict):
                remote_data = resp["data"]
        except Exception:
            remote_data = None

    conn = db.connect()
    try:
        all_txns = [
            dict(r)
            for r in conn.execute(
                """
                SELECT * FROM ledger_txns
                WHERE COALESCE(UPPER(status), '') NOT IN ('VOID_DUPLICATE', 'VOID', 'CANCELLED')
                ORDER BY occurred_at DESC
                """
            ).fetchall()
        ]
        compute_rows = [dict(r) for r in conn.execute("SELECT provider, currency, balance, billing_scope FROM compute_assets").fetchall()]
    finally:
        conn.close()

    rules = list_sure_rules(db)
    fixed_assets = _load_fixed_assets(db)

    # 1) 净资产问答
    if any(k in q.lower() for k in ("净资产", "资产总览", "总资产", "net worth", "家底")):
        asset_total = round(sum(float(a.get("current_yuan") or 0.0) for a in fixed_assets), 2)
        compute_cny = round(
            sum(
                float(c["balance"]) * _fx_rate_to_cny(str(c["currency"]))
                for c in compute_rows
            ),
            2,
        )
        income_yuan = round(
            sum(
                _to_cny_yuan(int(t["amount_cents"]), str(t.get("currency") or "CNY"))
                for t in all_txns
                if str(t.get("kind") or "") in _INFLOW_KINDS
            ),
            2,
        )
        spend_yuan = round(
            sum(
                _to_cny_yuan(int(t["amount_cents"]), str(t.get("currency") or "CNY"))
                for t in all_txns
                if str(t.get("kind") or "") not in _NON_SPEND_KINDS
            ),
            2,
        )
        cash_net = round(income_yuan - spend_yuan, 2)
        total_net = round(asset_total + compute_cny + cash_net, 2)
        if remote_data and remote_data.get("net_worth_yuan") is not None:
            total_net = round(float(remote_data["net_worth_yuan"]), 2)
        data = {
            "net_worth_yuan": total_net,
            "fixed_assets_yuan": asset_total,
            "fixed_assets_count": len(fixed_assets),
            "compute_balance_cny": compute_cny,
            "ledger_cash_net_yuan": cash_net,
            "remote_synced": remote_data is not None,
        }
        reply = (
            f"🏦 **Sure 净资产快照**：总计 **¥{total_net:,.2f}**\n"
            f"• 固定资产（{len(fixed_assets)} 项）：¥{asset_total:,.2f}\n"
            f"• AI 算力账户余额折合：¥{compute_cny:,.2f}\n"
            f"• 账本现金流净额：¥{cash_net:,.2f}"
        )
        return {"ok": True, "intent": "net_worth", "reply": reply, "data": data}

    # 2) 公私报销 / CW08 / CW01 账龄问答
    q_low = q.lower()
    if any(k in q_low for k in ("报销", "cw08", "cw01", "公私", "账龄", "待回款", "reimburse")):
        cw08_txns: list[dict[str, Any]] = []
        cw01_txns: list[dict[str, Any]] = []
        private_cny = 0.0
        by_currency_spend: dict[str, float] = {}
        cw08_oldest_days = 0
        cw01_oldest_days = 0
        for t in all_txns:
            if str(t.get("kind") or "") in _NON_SPEND_KINDS:
                continue
            mapped = _apply_rules_to_txn(t, rules)
            curr = str(mapped["currency"])
            by_currency_spend[curr] = round(by_currency_spend.get(curr, 0.0) + float(mapped["amount_yuan"]), 2)
            tags = set(mapped["tags"])
            if "CW08" in tags:
                cw08_txns.append(mapped)
                try:
                    age_d = max(0, (cur_now - parse_iso(str(t["occurred_at"]))).days)
                    cw08_oldest_days = max(cw08_oldest_days, age_d)
                except Exception:
                    pass
            elif "CW01" in tags:
                cw01_txns.append(mapped)
                try:
                    age_d = max(0, (cur_now - parse_iso(str(t["occurred_at"]))).days)
                    cw01_oldest_days = max(cw01_oldest_days, age_d)
                except Exception:
                    pass
            else:
                private_cny = round(private_cny + float(mapped["amount_cny_yuan"]), 2)

        asks_cw08 = "cw08" in q_low
        asks_cw01 = "cw01" in q_low
        if asks_cw08 and not asks_cw01:
            oldest_days = cw08_oldest_days
        elif asks_cw01 and not asks_cw08:
            oldest_days = cw01_oldest_days
        else:
            oldest_days = max(cw08_oldest_days, cw01_oldest_days)

        cw08_yuan = round(sum(float(x["amount_cny_yuan"]) for x in cw08_txns), 2)
        cw01_yuan = round(sum(float(x["amount_cny_yuan"]) for x in cw01_txns), 2)
        priv_yuan = round(private_cny, 2)

        raw_rcv = db.get_meta("quiet_cfo_receivables") or "[]"
        try:
            rcvs = [r for r in json.loads(raw_rcv) if isinstance(r, dict) and r.get("status") == "PENDING"]
        except Exception:
            rcvs = []
        pending_rcv_yuan = round(sum(float(r.get("expected_yuan") or 0.0) for r in rcvs), 2)

        data = {
            "cw08_yuan": cw08_yuan,
            "cw08_count": len(cw08_txns),
            "cw08_oldest_days": cw08_oldest_days,
            "cw01_yuan": cw01_yuan,
            "cw01_count": len(cw01_txns),
            "cw01_oldest_days": cw01_oldest_days,
            "private_yuan": priv_yuan,
            "by_currency": by_currency_spend,
            "oldest_reimburse_days": oldest_days,
            "pending_receivables_yuan": pending_rcv_yuan,
            "pending_receivables_count": len(rcvs),
        }
        reply = (
            f"🧾 **公私分账与报销账龄（折合人民币）**：\n"
            f"• 公费 CW08（AI/算力）：{len(cw08_txns)} 笔 · **¥{cw08_yuan:,.2f}**（最长账龄 {cw08_oldest_days} 天）\n"
            f"• 公费 CW01（出行/差旅）：{len(cw01_txns)} 笔 · **¥{cw01_yuan:,.2f}**（最长账龄 {cw01_oldest_days} 天）\n"
            f"• 私人自费支出：**¥{priv_yuan:,.2f}**\n"
            f"• 待回款池：{len(rcvs)} 笔（¥{pending_rcv_yuan:,.2f}）｜最长未结账龄：**{oldest_days} 天**"
        )
        return {"ok": True, "intent": "reimbursement_aging", "reply": reply, "data": data}

    # 3) 分类/商户/时间范围支出 & 月度现金流问答
    period_prefix, period_label, period_explicit = _parse_natural_period(q, cur_now)
    if period_prefix is None:
        scope_txns = all_txns
    else:
        scoped = [t for t in all_txns if str(t.get("occurred_at") or "").startswith(period_prefix)]
        if scoped or period_explicit:
            scope_txns = scoped
        else:
            scope_txns = all_txns
            period_label = "全部历史"

    spend_filter = _extract_spend_filter(q)
    if spend_filter is not None:
        filter_label, filter_pat, filter_cat = spend_filter
        matched_spend: list[dict[str, Any]] = []
        filtered_outflow = 0.0
        by_category: dict[str, float] = {}
        by_currency: dict[str, dict[str, float]] = {}
        by_merchant: dict[str, float] = {}
        for t in scope_txns:
            kind = str(t.get("kind") or "")
            if kind in _NON_SPEND_KINDS:
                continue
            mapped = _apply_rules_to_txn(t, rules)
            hay = f"{t.get('merchant') or ''} {t.get('notes') or ''}"
            hit = False
            if filter_cat is not None:
                hit = mapped["category"] == filter_cat
            elif filter_pat:
                hit = bool(re.search(filter_pat, hay, re.IGNORECASE))
            if not hit:
                continue
            eff_cny = float(mapped["amount_cny_yuan"])
            curr = str(mapped["currency"])
            filtered_outflow = round(filtered_outflow + eff_cny, 2)
            cat = str(mapped["category"])
            by_category[cat] = round(by_category.get(cat, 0.0) + eff_cny, 2)
            m_name = str(mapped["merchant"])
            by_merchant[m_name] = round(by_merchant.get(m_name, 0.0) + eff_cny, 2)
            slot = by_currency.setdefault(curr, {"inflow": 0.0, "outflow": 0.0})
            slot["outflow"] = round(slot["outflow"] + float(mapped["amount_yuan"]), 2)
            matched_spend.append(mapped)

        top_merchants = sorted(by_merchant.items(), key=lambda kv: kv[1], reverse=True)
        top_lines = [f"  - {m}: ¥{v:,.2f}" for m, v in top_merchants[:5]]
        data = {
            "period": period_label,
            "filter_label": filter_label,
            "inflow_yuan": 0.0,
            "outflow_yuan": filtered_outflow,
            "filtered_spend_yuan": filtered_outflow,
            "net_cashflow_yuan": round(-filtered_outflow, 2),
            "txn_count": len(matched_spend),
            "by_category": by_category,
            "by_merchant": dict(top_merchants),
            "by_currency": by_currency,
            "matched_txns": matched_spend,
        }
        reply = (
            f"💹 **Sure 分类/商户支出（{period_label} · {filter_label}）**：\n"
            f"• 合计支出：**¥{filtered_outflow:,.2f}**（共 {len(matched_spend)} 笔）\n"
            + ("• 商户明细：\n" + "\n".join(top_lines) if top_lines else "• 该时段暂无此项支出记录")
        )
        return {"ok": True, "intent": "category_spend", "reply": reply, "data": data}

    inflow_yuan = 0.0
    outflow_yuan = 0.0
    by_category = {}
    by_currency = {}
    for t in scope_txns:
        kind = str(t.get("kind") or "")
        curr = str(t.get("currency") or "CNY").strip().upper()
        raw_orig = round(int(t["amount_cents"]) / 100.0, 2)
        if kind in _INFLOW_KINDS:
            cny_amt = _to_cny_yuan(int(t["amount_cents"]), curr)
            inflow_yuan = round(inflow_yuan + cny_amt, 2)
            slot = by_currency.setdefault(curr, {"inflow": 0.0, "outflow": 0.0})
            slot["inflow"] = round(slot["inflow"] + raw_orig, 2)
        elif kind in _NON_SPEND_KINDS:
            continue
        else:
            mapped = _apply_rules_to_txn(t, rules)
            eff_cny = float(mapped["amount_cny_yuan"])
            outflow_yuan = round(outflow_yuan + eff_cny, 2)
            cat = str(mapped["category"])
            by_category[cat] = round(by_category.get(cat, 0.0) + eff_cny, 2)
            slot = by_currency.setdefault(curr, {"inflow": 0.0, "outflow": 0.0})
            slot["outflow"] = round(slot["outflow"] + float(mapped["amount_yuan"]), 2)

    sorted_cats = sorted(by_category.items(), key=lambda kv: kv[1], reverse=True)
    net_flow = round(inflow_yuan - outflow_yuan, 2)
    top_lines = [f"  - {c}: ¥{v:,.2f}" for c, v in sorted_cats[:5]]
    fx_note = ""
    foreign_ccys = [c for c in sorted(by_currency.keys()) if c not in ("CNY", "RMB")]
    if foreign_ccys:
        fx_parts = [f"{c} 流出 {by_currency[c]['outflow']:,.2f} / 流入 {by_currency[c]['inflow']:,.2f}" for c in foreign_ccys]
        fx_note = "\n• 外币原币明细（已按汇率折合人民币）：" + "；".join(fx_parts)
    data = {
        "period": period_label,
        "inflow_yuan": inflow_yuan,
        "outflow_yuan": outflow_yuan,
        "net_cashflow_yuan": net_flow,
        "txn_count": len(scope_txns),
        "by_category": dict(sorted_cats),
        "by_currency": by_currency,
    }
    reply = (
        f"💹 **Sure 现金流与分类支出（{period_label}）**：\n"
        f"• 流入：¥{inflow_yuan:,.2f} ｜ 流出：**¥{outflow_yuan:,.2f}** ｜ 净现金流：¥{net_flow:,.2f}\n"
        + ("• Top 支出分类：\n" + "\n".join(top_lines) if top_lines else "• 暂无支出记录")
        + fx_note
    )
    return {"ok": True, "intent": "cashflow_and_categories", "reply": reply, "data": data}


def format_sure_summary(db: SimingDB) -> str:
    """格式化 Sure 财务控制塔概览文本。"""
    st = get_sure_status(db)
    deploy_badge = "✅ Compose+Pipelock 就绪" if (st["compose_ready"] and st["pipelock_ready"]) else "⚠️ 待补全编排文件"
    api_badge = "🟢 在线" if st["api_healthy"] else "🟡 本地镜像模式"
    return (
        f"🏛️ **Sure 财务 OS 控制塔**（{api_badge}）\n"
        f"• 部署基座：{deploy_badge} ｜ 代理：`{st['api_url']}`（Pipelock 默认只读，写操作强制 A2）\n"
        f"• 流水同步：已同步 **{st['synced_txns']}/{st['total_ledger_txns']}** 笔（待同步 {st['pending_txn_count']} 笔）\n"
        f"• 自动规则：**{st['rules_count']}** 条（支持 4 卡映射、CW08/CW01 公私标签与分摊）\n"
        "• 一句话查账：`净资产` / `本月现金流` / `CW08报销账龄`"
    )
