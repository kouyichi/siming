"""P0 动作意图与三态收据引擎（receipts.py）：
将「授权已消费 (authorization_consumed)」「动作已提交/执行中 (order_submitted/running)」「外部结果已核验 (paid_verified/external_verified)」
彻底拆分为独立状态与持久化收据，杜绝“返回中文字符串即当成业务已完成”的虚假完成。
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Optional

from siming.db import SimingDB, iso_now, now_local

# 明确标记仅确认方案、未发生真实外部履约的动作（绝不允许落成虚假 external_verified=True）
PLAN_ONLY_ACTIONS: frozenset[str] = frozenset(
    {
        "meituan_combo_order",
        "commute_rescue_bundle",
        "overnight_next_step",
        "overnight_handoff_next_step",
    }
)


@dataclass
class ActionResult:
    """动作执行器类型化返回值，取代裸字符串。"""

    outcome: str  # verified | paid_verified | submitted_pending | needs_user_action | plan_only | failed | unknown
    summary: str
    external_order_id: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)
    authorization_consumed: bool = True
    order_submitted: bool = False
    external_verified: bool = False
    paid_verified: bool = False
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "outcome": self.outcome,
            "summary": self.summary,
            "external_order_id": self.external_order_id,
            "evidence": self.evidence,
            "authorization_consumed": self.authorization_consumed,
            "order_submitted": self.order_submitted,
            "external_verified": self.external_verified,
            "paid_verified": self.paid_verified,
            "error": self.error,
        }


class ReceiptSummary(str):
    """兼容旧版 str 返回值的类型化摘要：既是普通 str，又携带完整 ActionResult 供审批网关与收据表读取。"""

    action_result: ActionResult

    def __new__(cls, text: str, action_result: ActionResult) -> "ReceiptSummary":
        obj = super().__new__(cls, text)
        obj.action_result = action_result
        return obj


def coerce_action_result(action: str, raw: Any) -> ActionResult:
    """将执行器返回值（ActionResult / ReceiptSummary / dict / 兼容旧 str）归约为诚实的 ActionResult。"""
    if isinstance(raw, ActionResult):
        return raw
    if isinstance(raw, ReceiptSummary) and hasattr(raw, "action_result"):
        return raw.action_result

    if isinstance(raw, dict):
        summary = str(raw.get("summary") or raw.get("reply") or "")
        ext_id = str(raw.get("external_order_id") or raw.get("order_id") or "")
        ev = raw.get("evidence") if isinstance(raw.get("evidence"), dict) else {}
        outcome = str(raw.get("outcome") or "").strip().lower()
        if not outcome:
            if raw.get("paid_verified") or raw.get("paid"):
                outcome = "paid_verified"
            elif raw.get("needs_user_action") or raw.get("gated"):
                outcome = "needs_user_action"
            elif raw.get("plan_only") or action in PLAN_ONLY_ACTIONS:
                outcome = "plan_only"
            elif raw.get("ok") is False:
                outcome = "failed"
            elif raw.get("order_submitted") and not raw.get("external_verified"):
                outcome = "submitted_pending"
            else:
                outcome = "verified"
        return ActionResult(
            outcome=outcome,
            summary=summary,
            external_order_id=ext_id,
            evidence=ev,
            authorization_consumed=bool(raw.get("authorization_consumed", True)),
            order_submitted=bool(raw.get("order_submitted", outcome in ("paid_verified", "submitted_pending", "needs_user_action"))),
            external_verified=bool(raw.get("external_verified", outcome in ("verified", "paid_verified"))),
            paid_verified=bool(raw.get("paid_verified", outcome == "paid_verified")),
            error=str(raw.get("error") or ""),
        )

    txt = str(raw or "")
    if action in PLAN_ONLY_ACTIONS:
        return ActionResult(
            outcome="plan_only",
            summary=txt,
            authorization_consumed=True,
            order_submitted=False,
            external_verified=False,
            paid_verified=False,
            evidence={"mode": "plan_only", "note": "仅确认方案/草稿，未触发真实外部履约"},
        )
    if action == "meituan_submit_pay":
        if "等待你完成最终支付验证" in txt or "触发美团安全门禁" in txt or "未扣款" in txt:
            return ActionResult(
                outcome="needs_user_action",
                summary=txt,
                authorization_consumed=True,
                order_submitted=("未扣款" not in txt and "安全门禁" not in txt),
                external_verified=False,
                paid_verified=False,
                evidence={"reason": "needs_user_action"},
            )
        if "支付成功" in txt:
            return ActionResult(
                outcome="paid_verified",
                summary=txt,
                authorization_consumed=True,
                order_submitted=True,
                external_verified=True,
                paid_verified=True,
            )
        return ActionResult(
            outcome="submitted_pending",
            summary=txt,
            authorization_consumed=True,
            order_submitted=True,
            external_verified=False,
            paid_verified=False,
        )
    return ActionResult(
        outcome="verified",
        summary=txt,
        authorization_consumed=True,
        order_submitted=True,
        external_verified=True,
        paid_verified=False,
    )


def build_intent_idempotency_key(
    action: str,
    approval_token: str = "",
    target_id: str = "",
    params: Optional[dict[str, Any]] = None,
) -> str:
    """绑定本次购买/动作意图的事务幂等键：
    - 有 approval_token 时绑定 token（同一张确认卡重复点击永远命中同一 Intent；同店重新生成新确认卡则是独立新 Intent）；
    - 无 token 时绑定 preview_target_id/quote_version/params 摘要。
    """
    tok = (approval_token or "").strip().upper()
    if tok:
        return f"intent:{action}:{tok}"
    p = params or {}
    seed = {
        "action": action,
        "target_id": target_id,
        "preview_target_id": p.get("preview_target_id") or "",
        "quote_version": p.get("quote_version") or "",
        "shop": p.get("shop") or "",
        "item_spec": p.get("item_spec") or "",
        "final_payable_yuan": p.get("final_payable_yuan"),
    }
    digest = hashlib.sha256(json.dumps(seed, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:24]
    return f"intent:{action}:{digest}"


def create_or_get_intent(
    db: SimingDB,
    *,
    action: str,
    idempotency_key: str,
    approval_token: str = "",
    target_id: str = "",
    amount_cents: int = 0,
    max_authorized_cents: int = 0,
    quote: Optional[dict[str, Any]] = None,
    initial_state: str = "AUTHORIZED",
    conn: Optional[sqlite3.Connection] = None,
) -> tuple[dict[str, Any], bool]:
    """幂等创建或读取 ActionIntent，返回 (intent_dict, created_new)。"""
    ts = iso_now()

    def _run(c: sqlite3.Connection) -> tuple[dict[str, Any], bool]:
        existing = c.execute(
            "SELECT * FROM action_intents WHERE idempotency_key = ?",
            (idempotency_key,),
        ).fetchone()
        if existing:
            return dict(existing), False

        intent_id = f"INT-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}"
        auth_ts = ts if initial_state in ("AUTHORIZED", "SUBMITTING", "PAYING") else None
        c.execute(
            """
            INSERT INTO action_intents (
                intent_id, approval_token, action, target_id, idempotency_key, state,
                auth_consumed_at, amount_cents, max_authorized_cents, quote_json,
                evidence_json, summary, error, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '', '', ?, ?)
            """,
            (
                intent_id,
                approval_token.strip().upper(),
                action,
                target_id,
                idempotency_key,
                initial_state,
                auth_ts,
                max(0, int(amount_cents)),
                max(0, int(max_authorized_cents)),
                json.dumps(quote or {}, ensure_ascii=False, sort_keys=True),
                ts,
                ts,
            ),
        )
        row = c.execute("SELECT * FROM action_intents WHERE intent_id = ?", (intent_id,)).fetchone()
        return dict(row), True

    if conn is not None:
        return _run(conn)
    db.init_db()
    with db.transaction() as c:
        return _run(c)


def record_receipt_stage(
    db: SimingDB,
    *,
    intent_id: str,
    stage: str,
    approval_token: str = "",
    ok: bool = True,
    external_ref: str = "",
    evidence: Optional[dict[str, Any]] = None,
    summary: str = "",
    new_intent_state: Optional[str] = None,
    error: str = "",
    conn: Optional[sqlite3.Connection] = None,
) -> dict[str, Any]:
    """追加一张不可变阶段收据，并同步推进 action_intents 的对应时间戳与状态。"""
    ts = iso_now()
    rec_id = f"RCP-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}"
    ev_json = json.dumps(evidence or {}, ensure_ascii=False, sort_keys=True, default=str)

    def _run(c: sqlite3.Connection) -> dict[str, Any]:
        c.execute(
            """
            INSERT INTO action_receipts (
                receipt_id, intent_id, approval_token, stage, ok, external_ref,
                evidence_json, summary, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                rec_id,
                intent_id,
                approval_token.strip().upper(),
                stage,
                1 if ok else 0,
                external_ref,
                ev_json,
                summary[:600],
                ts,
            ),
        )
        updates: list[str] = ["updated_at = ?"]
        vals: list[Any] = [ts]
        if new_intent_state:
            updates.append("state = ?")
            vals.append(new_intent_state)
        if stage == "authorization_consumed":
            updates.append("auth_consumed_at = COALESCE(auth_consumed_at, ?)")
            vals.append(ts)
        elif stage in ("order_submitted", "submitting", "paying"):
            updates.append("submitted_at = COALESCE(submitted_at, ?)")
            vals.append(ts)
        elif stage in ("paid_verified", "external_verified"):
            updates.append("verified_at = COALESCE(verified_at, ?)")
            vals.append(ts)
        if external_ref:
            updates.append("external_order_id = CASE WHEN external_order_id='' THEN ? ELSE external_order_id END")
            vals.append(external_ref)
        if summary:
            updates.append("summary = ?")
            vals.append(summary[:600])
        if error:
            updates.append("error = ?")
            vals.append(error[:400])
        if evidence:
            updates.append("evidence_json = ?")
            vals.append(ev_json)
        vals.append(intent_id)
        c.execute(f"UPDATE action_intents SET {', '.join(updates)} WHERE intent_id = ?", tuple(vals))
        return {
            "receipt_id": rec_id,
            "intent_id": intent_id,
            "stage": stage,
            "ok": ok,
            "external_ref": external_ref,
            "created_at": ts,
        }

    if conn is not None:
        return _run(conn)
    db.init_db()
    with db.transaction() as c:
        return _run(c)


def get_intent_with_receipts(db: SimingDB, *, intent_id: str = "", approval_token: str = "") -> Optional[dict[str, Any]]:
    """查询单个 ActionIntent 及其全部三态收据明细（只读）。"""
    conn = db.connect_readonly()
    try:
        if intent_id:
            row = conn.execute("SELECT * FROM action_intents WHERE intent_id = ?", (intent_id,)).fetchone()
        elif approval_token:
            row = conn.execute(
                "SELECT * FROM action_intents WHERE approval_token = ? ORDER BY created_at DESC LIMIT 1",
                (approval_token.strip().upper(),),
            ).fetchone()
        else:
            return None
        if not row:
            return None
        intent = dict(row)
        recs = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM action_receipts WHERE intent_id = ? ORDER BY created_at ASC",
                (intent["intent_id"],),
            ).fetchall()
        ]
        stages = {r["stage"] for r in recs if int(r["ok"] or 0) == 1}
        intent["receipts"] = recs
        intent["authorization_consumed"] = bool(intent.get("auth_consumed_at") or "authorization_consumed" in stages)
        intent["order_submitted"] = bool(intent.get("submitted_at") or "order_submitted" in stages)
        intent["paid_verified"] = bool("paid_verified" in stages or intent.get("state") == "PAID_VERIFIED")
        intent["external_verified"] = bool(
            intent.get("verified_at") or "paid_verified" in stages or "external_verified" in stages
        )
        return intent
    finally:
        conn.close()


def approval_status_for_outcome(outcome: str) -> str:
    """将 ActionResult.outcome 映射为诚实的 approvals.status，不再把未验证结果一律写 APPLIED。"""
    mapping = {
        "verified": "APPLIED",
        "paid_verified": "APPLIED",
        "plan_only": "PLAN_CONFIRMED",
        "needs_user_action": "AWAITING_VERIFY",
        "submitted_pending": "AWAITING_VERIFY",
        "unknown": "FAILED_UNKNOWN",
        "failed": "FAILED",
    }
    return mapping.get((outcome or "").lower(), "APPLIED")


def format_receipt_status_line(res: ActionResult) -> str:
    """生成可供 QQ 原卡与活动日志直接展示的三态收据摘要行。"""
    a_s = "✅授权已消费" if res.authorization_consumed else "⬜未消费"
    if res.outcome == "plan_only":
        return f"🧾 状态收据：{a_s} · 📋仅方案确认（未触发外部履约）"
    s_s = "✅已提交外部" if res.order_submitted else "⬜未提交"
    if res.paid_verified:
        v_s = f"✅扣款已核验{f'({res.external_order_id})' if res.external_order_id else ''}"
    elif res.external_verified:
        v_s = "✅结果已核验"
    elif res.outcome == "needs_user_action":
        v_s = "⏳待本人验证（尚未确认扣款）"
    elif res.outcome == "submitted_pending":
        v_s = "⏳已提交待核验对账"
    else:
        v_s = "❌未核验"
    return f"🧾 状态收据：{a_s} → {s_s} → {v_s}"
