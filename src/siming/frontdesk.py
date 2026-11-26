"""P1 FrontDesk 轻前台、回复权单一归属（owner + reply_epoch）、最小共享 Thread 事实与可恢复 handback（frontdesk.py）。

职责边界（严格落实 D-35 双脑协作契约）：
1. 保留完整 Hermes 聊天与通用技能 + 保留完整司命 PA 功能；
2. FrontDesk 按 (channel, account, conversation, message_id) 幂等收件，原子分配唯一 owner ('hermes' | 'siming') 与 reply_epoch；
3. 持久化 (channel, account, conversation, platform_msg_id) -> (owner, thread_id, card_id, approval_token, quote_version)，
   引用普通聊天回到 Hermes，引用司命卡精准回到原 Thread（14 天续接，过期报价强制重核价）；
4. 双脑仅共享最小 Thread 工作事实（thread_id, goal, entity_refs, facts_with_source, pending_question, active_run, last_verified_effect, revision），
   绝不互灌全量聊天记录，彻底废除全局单槽 last_meituan_picks 串台；
5. 支持复合句多子意图拆分（如「解释下这个框架，等周五提醒我试试」由 Hermes 编排一次回复并内嵌司命 Wxx 编号，绝不隐式授权付款）；
6. 支持可恢复 handback(thread, from, to, reason, context_rev, reply_epoch) 根治 8s 硬接管失忆。
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import timedelta
from typing import Any, Callable, Optional

from siming.db import SimingDB, iso_now, now_local, parse_iso

_STEER_RE = re.compile(
    r"^(?:先别合入|别合入|不要合入|只跑测试|改为|改成|插话|中途改|补充要求|顺便改|不要辣|改微辣|别加米饭|换成)",
    re.IGNORECASE,
)
_CANCEL_RUN_RE = re.compile(
    r"^(?:停一下|停手|停止|全部停|全停|别跑了|停掉|中止任务|取消运行)(?:\s+([A-Za-z0-9_-]+))?$",
    re.IGNORECASE,
)
_HYBRID_EXPLAIN_AND_REMIND_RE = re.compile(
    r"^(?P<chat>.{2,160}?)[,，;；。\s]+(?:顺便|另外|再|然后|并|等|记得)?(?P<remind>(?:等|在)?(?:明天|后天|周[一二三四五六日天]|星期[一二三四五六日天]|\d+点|\d+分钟后|\d+小时后).{0,40}?(?:提醒我|叫我|记一下).{1,80})$",
    re.IGNORECASE,
)
_MULTI_AGENT_EDIT_REVIEW_RE = re.compile(
    r"(?:让|叫)\s*(codex|claude(?:\s*code)?)\s*(?:来|去)?(?:改|写|修|实现).{0,40}?(?:让|叫)?\s*(claude(?:\s*code)?|codex)\s*(?:来|去)?(?:审|review|检查|评审)",
    re.IGNORECASE,
)
_RESUME_SESSION_RE = re.compile(
    r"(?:回到|恢复|续接)\s*(agy|opencode|claude|codex|herdr)\s*(?:刚才|上次|之前)?(?:的)?(?:现场|会话|窗口)|^herdr\s+resume\b",
    re.IGNORECASE,
)
_SENSITIVE_ISOLATED_RE = re.compile(
    r"(?:密码|支付密码|PIN|验证码|OTP|解压码|\bapply\s+A\d{2,6}\b|确认支付|确认付款|化验单|血糖|替尔泊肽|银行卡|私钥|token)",
    re.IGNORECASE,
)
_CASUAL_CHAT_ONLY_RE = re.compile(
    r"^(?:今天累死了|累死了|随便聊聊|聊聊天|陪我聊聊|好无聊|心情不好|讲个笑话|你好|在吗|哈喽|hi|hello)[，,。！!~～\s]*(?:随便聊聊|聊聊)?$",
    re.IGNORECASE,
)


def make_envelope_key(channel: str, account: str, conversation: str, message_id: str) -> str:
    return f"{channel.strip()}:{account.strip()}:{conversation.strip()}:{message_id.strip()}"


def assign_message_owner(
    db: SimingDB,
    *,
    channel: str,
    account: str,
    conversation: str,
    message_id: str,
    owner: str,
    thread_id: str = "",
    route_tier: str = "tier0",
    route_reason: str = "",
) -> dict[str, Any]:
    """原子分配一条入站消息的唯一回复 owner 与单调 reply_epoch；重复投递幂等返回原归属。"""
    db.init_db()
    env_key = make_envelope_key(channel, account, conversation, message_id or f"anon-{secrets.token_hex(4)}")
    ts = iso_now()
    with db.transaction() as conn:
        existing = conn.execute(
            "SELECT * FROM message_ownership WHERE envelope_key = ?",
            (env_key,),
        ).fetchone()
        if existing:
            return {**dict(existing), "created": False}

        row_ep = conn.execute(
            "SELECT COALESCE(MAX(reply_epoch), 0) AS max_ep FROM message_ownership WHERE channel=? AND conversation=?",
            (channel, conversation),
        ).fetchone()
        next_epoch = int((row_ep["max_ep"] if row_ep else 0) or 0) + 1
        conn.execute(
            """
            INSERT INTO message_ownership (
                envelope_key, channel, account, conversation, message_id,
                owner, reply_epoch, thread_id, route_tier, route_reason,
                status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'ACTIVE', ?, ?)
            """,
            (
                env_key,
                channel,
                account,
                conversation,
                message_id,
                owner,
                next_epoch,
                thread_id,
                route_tier,
                route_reason,
                ts,
                ts,
            ),
        )
        row = conn.execute("SELECT * FROM message_ownership WHERE envelope_key = ?", (env_key,)).fetchone()
        return {**dict(row), "created": True}


def get_message_owner(db: SimingDB, *, channel: str, account: str, conversation: str, message_id: str) -> Optional[dict[str, Any]]:
    env_key = make_envelope_key(channel, account, conversation, message_id)
    conn = db.connect_readonly()
    try:
        row = conn.execute("SELECT * FROM message_ownership WHERE envelope_key = ?", (env_key,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def bind_message_ref(
    db: SimingDB,
    *,
    channel: str,
    account: str,
    conversation: str,
    platform_msg_id: str,
    owner: str = "siming",
    thread_id: str = "",
    card_id: str = "",
    approval_token: str = "",
    quote_version: int = 1,
    meta: Optional[dict[str, Any]] = None,
    ttl_days: int = 14,
) -> dict[str, Any]:
    """持久化平台消息 ID 到 Thread / Card / QuoteVersion 的可信映射，替代全局「最近一次」与盲目引用劫持。"""
    db.init_db()
    ref_key = make_envelope_key(channel, account, conversation, platform_msg_id)
    ts = iso_now()
    exp = iso_now(now_local() + timedelta(days=max(1, int(ttl_days))))
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO message_refs (
                ref_key, channel, account, conversation, platform_msg_id,
                owner, thread_id, card_id, approval_token, quote_version,
                meta_json, created_at, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(ref_key) DO UPDATE SET
                owner = excluded.owner,
                thread_id = excluded.thread_id,
                card_id = excluded.card_id,
                approval_token = excluded.approval_token,
                quote_version = excluded.quote_version,
                meta_json = excluded.meta_json,
                expires_at = excluded.expires_at
            """,
            (
                ref_key,
                channel,
                account,
                conversation,
                platform_msg_id,
                owner,
                thread_id,
                card_id,
                approval_token.strip().upper(),
                int(quote_version),
                json.dumps(meta or {}, ensure_ascii=False),
                ts,
                exp,
            ),
        )
        row = conn.execute("SELECT * FROM message_refs WHERE ref_key = ?", (ref_key,)).fetchone()
        return dict(row)


def resolve_message_ref(
    db: SimingDB,
    *,
    channel: str,
    account: str,
    conversation: str,
    platform_msg_id: str,
) -> Optional[dict[str, Any]]:
    """按精确平台消息 ID 查找原消息所有方与线程绑定（同时检查 14 天续接窗与报价是否过期）。"""
    if not platform_msg_id:
        return None
    ref_key = make_envelope_key(channel, account, conversation, platform_msg_id)
    conn = db.connect_readonly()
    try:
        row = conn.execute("SELECT * FROM message_refs WHERE ref_key = ?", (ref_key,)).fetchone()
        if not row:
            # 降级按 platform_msg_id 精确匹配（跨 account 别名兼容）
            row = conn.execute(
                "SELECT * FROM message_refs WHERE channel=? AND platform_msg_id=? ORDER BY created_at DESC LIMIT 1",
                (channel, platform_msg_id),
            ).fetchone()
        if not row:
            return None
        d = dict(row)
        d["meta"] = json.loads(d.get("meta_json") or "{}")
        exp = str(d.get("expires_at") or "")
        d["expired"] = bool(exp and parse_iso(exp) < now_local())
        if d.get("approval_token"):
            app = conn.execute(
                "SELECT status, expires_at, action FROM approvals WHERE token = ?",
                (d["approval_token"],),
            ).fetchone()
            if app:
                d["approval_status"] = str(app["status"])
                d["quote_expired"] = bool(app["status"] != "PENDING" or parse_iso(app["expires_at"]) < now_local())
        return d
    finally:
        conn.close()


def upsert_thread_facts(
    db: SimingDB,
    *,
    thread_id: str = "",
    channel: str = "qqbot",
    conversation: str = "",
    goal: str = "",
    entity_refs: Optional[dict[str, Any]] = None,
    facts_with_source: Optional[list[dict[str, Any]]] = None,
    pending_question: Optional[str] = None,
    active_run_id: Optional[str] = None,
    last_verified_effect: Optional[dict[str, Any]] = None,
    picks: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """维护双脑共享的最小工作事实快照（绝不存储或互灌全量聊天记录），每次更新单调递增 revision。"""
    db.init_db()
    ts = iso_now()
    tid = (thread_id or "").strip() or f"TH-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM threads WHERE thread_id = ?", (tid,)).fetchone()
        if not row:
            conn.execute(
                """
                INSERT INTO threads (
                    thread_id, channel, conversation, goal, status,
                    entity_refs_json, facts_with_source_json, pending_question,
                    active_run_id, last_verified_effect_json, picks_json, picks_version,
                    revision, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'ACTIVE', ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)
                """,
                (
                    tid,
                    channel,
                    conversation,
                    goal[:300],
                    json.dumps(entity_refs or {}, ensure_ascii=False),
                    json.dumps(facts_with_source or [], ensure_ascii=False),
                    pending_question or "",
                    active_run_id or "",
                    json.dumps(last_verified_effect or {}, ensure_ascii=False),
                    json.dumps(picks or [], ensure_ascii=False),
                    1 if picks is not None else 0,
                    ts,
                    ts,
                ),
            )
        else:
            cur = dict(row)
            merged_refs = json.loads(cur["entity_refs_json"] or "{}")
            if entity_refs:
                merged_refs.update(entity_refs)
            merged_facts = json.loads(cur["facts_with_source_json"] or "[]")
            if facts_with_source:
                merged_facts = (merged_facts + facts_with_source)[-12:]
            new_goal = goal[:300] if goal else cur["goal"]
            new_q = cur["pending_question"] if pending_question is None else pending_question
            new_run = cur["active_run_id"] if active_run_id is None else active_run_id
            new_eff = (
                json.dumps(last_verified_effect, ensure_ascii=False)
                if last_verified_effect is not None
                else cur["last_verified_effect_json"]
            )
            new_picks = json.dumps(picks, ensure_ascii=False) if picks is not None else cur["picks_json"]
            new_picks_ver = int(cur["picks_version"] or 0) + (1 if picks is not None else 0)
            conn.execute(
                """
                UPDATE threads
                SET goal=?, entity_refs_json=?, facts_with_source_json=?, pending_question=?,
                    active_run_id=?, last_verified_effect_json=?, picks_json=?, picks_version=?,
                    revision=revision+1, updated_at=?
                WHERE thread_id=?
                """,
                (
                    new_goal,
                    json.dumps(merged_refs, ensure_ascii=False),
                    json.dumps(merged_facts, ensure_ascii=False),
                    new_q,
                    new_run,
                    new_eff,
                    new_picks,
                    new_picks_ver,
                    ts,
                    tid,
                ),
            )
        out = dict(conn.execute("SELECT * FROM threads WHERE thread_id = ?", (tid,)).fetchone())
    return _hydrate_thread(out)


def _hydrate_thread(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "thread_id": row["thread_id"],
        "channel": row["channel"],
        "conversation": row["conversation"],
        "goal": row["goal"],
        "status": row["status"],
        "entity_refs": json.loads(row.get("entity_refs_json") or "{}"),
        "facts_with_source": json.loads(row.get("facts_with_source_json") or "[]"),
        "pending_question": row.get("pending_question") or "",
        "active_run_id": row.get("active_run_id") or "",
        "last_verified_effect": json.loads(row.get("last_verified_effect_json") or "{}"),
        "picks": json.loads(row.get("picks_json") or "[]"),
        "picks_version": int(row.get("picks_version") or 0),
        "revision": int(row.get("revision") or 1),
        "updated_at": row.get("updated_at") or "",
    }


def get_thread_facts(db: SimingDB, thread_id: str) -> Optional[dict[str, Any]]:
    """供 Hermes 与司命双向读取的最小工作事实视图（只读连接）。"""
    conn = db.connect_readonly()
    try:
        row = conn.execute("SELECT * FROM threads WHERE thread_id = ?", (thread_id,)).fetchone()
        return _hydrate_thread(dict(row)) if row else None
    finally:
        conn.close()


def handback(
    db: SimingDB,
    *,
    thread_id: str,
    from_owner: str,
    to_owner: str,
    reason: str,
    context_rev: int = 1,
    reply_epoch: int = 1,
    envelope_key: str = "",
    summary: str = "",
    open_items: Optional[list[str]] = None,
    next_cursor: str = "",
) -> dict[str, Any]:
    """可恢复交接原语：在同一事务中转移回复权并写待投递记录，防止 8s 接管失忆与重启丢归属。"""
    db.init_db()
    ts = iso_now()
    hb_id = f"HB-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(4)}"
    with db.transaction() as conn:
        if envelope_key:
            conn.execute(
                """
                UPDATE message_ownership
                SET owner = ?, reply_epoch = reply_epoch + 1, status = 'HANDBACK', updated_at = ?
                WHERE envelope_key = ? AND reply_epoch <= ?
                """,
                (to_owner, ts, envelope_key, int(reply_epoch)),
            )
        conn.execute(
            """
            INSERT INTO handbacks (
                handback_id, thread_id, envelope_key, from_owner, to_owner,
                reason, context_rev, reply_epoch, summary, open_items_json,
                next_cursor, delivered, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            (
                hb_id,
                thread_id,
                envelope_key,
                from_owner,
                to_owner,
                reason[:240],
                int(context_rev),
                int(reply_epoch),
                summary[:400],
                json.dumps(open_items or [], ensure_ascii=False),
                next_cursor,
                ts,
            ),
        )
        row = conn.execute("SELECT * FROM handbacks WHERE handback_id = ?", (hb_id,)).fetchone()
        return dict(row)


def is_sensitive_local_only_payload(text: str, media: Optional[list[str]] = None) -> bool:
    """敏感载荷本地隔离判定：审批/PIN/OTP/健康原样本/私人附件严禁发往外部 JEV 分类器。"""
    if media:
        return True
    return bool(_SENSITIVE_ISOLATED_RE.search(text or ""))


def route_frontdesk_envelope(
    db: SimingDB,
    *,
    text: str,
    channel: str = "qqbot",
    account: str = "owner",
    conversation: str = "default",
    message_id: str = "",
    reply_to_id: str = "",
    reply_to_text: str = "",
    media: Optional[list[str]] = None,
    jev_eval_fn: Optional[Callable[..., dict[str, Any]]] = None,
) -> dict[str, Any]:
    """FrontDesk 14 场景统一入站分诊与回复权裁决（绝不吞噬 Hermes 聊天，也不遗漏司命控制与引用）。

    返回：
      {
        "owner": "hermes" | "siming",
        "reply_epoch": int,
        "envelope_key": str,
        "route_tier": "tier0" | "jev" | "light_llm" | "main_llm",
        "scenario": str,
        "thread_id": str,
        "ref": dict | None,
        "sub_intents": list[dict],
        "sensitive_isolated": bool,
        "control_action": dict | None,
      }
    """
    db.init_db()
    clean = (text or "").strip()
    sensitive = is_sensitive_local_only_payload(clean, media=media)
    msg_id = message_id.strip() or f"msg-{secrets.token_hex(5)}"

    # 1. 优先查精确可信引用 (channel, account, conversation, reply_to_id)
    ref = resolve_message_ref(
        db, channel=channel, account=account, conversation=conversation, platform_msg_id=reply_to_id
    ) if reply_to_id else None
    if ref is not None:
        ref_owner = str(ref.get("owner") or "siming")
        tid = str(ref.get("thread_id") or "")
        # 引用普通 Hermes 聊天 -> 原样归还 Hermes（修复 jev_gate:187 一刀切劫持！）
        if ref_owner == "hermes":
            own = assign_message_owner(
                db,
                channel=channel,
                account=account,
                conversation=conversation,
                message_id=msg_id,
                owner="hermes",
                thread_id=tid,
                route_tier="tier0",
                route_reason="quoted_hermes_thread",
            )
            return {
                "owner": "hermes",
                "reply_epoch": own["reply_epoch"],
                "envelope_key": own["envelope_key"],
                "route_tier": "tier0",
                "scenario": "quoted_chat",
                "thread_id": tid,
                "ref": ref,
                "sub_intents": [],
                "sensitive_isolated": sensitive,
                "control_action": None,
            }
        # 引用司命卡/提醒 -> 精准进入原 Thread（若报价已过期则明示需重核价）
        own = assign_message_owner(
            db,
            channel=channel,
            account=account,
            conversation=conversation,
            message_id=msg_id,
            owner="siming",
            thread_id=tid,
            route_tier="tier0",
            route_reason="quoted_siming_ref",
        )
        return {
            "owner": "siming",
            "reply_epoch": own["reply_epoch"],
            "envelope_key": own["envelope_key"],
            "route_tier": "tier0",
            "scenario": "quoted_siming_expired_quote" if ref.get("quote_expired") else "quoted_siming_card",
            "thread_id": tid,
            "ref": ref,
            "requires_requote": bool(ref.get("quote_expired")),
            "sub_intents": [],
            "sensitive_isolated": sensitive,
            "control_action": None,
        }

    # 2. Tier-0 运行中急停（Cancel）与插话（Steer）控制通道（优先于普通对话）
    m_cancel = _CANCEL_RUN_RE.match(clean)
    if m_cancel:
        target_run = (m_cancel.group(1) or "").strip()
        cancel_all = clean.startswith(("全部停", "全停"))
        own = assign_message_owner(
            db,
            channel=channel,
            account=account,
            conversation=conversation,
            message_id=msg_id,
            owner="siming",
            route_tier="tier0",
            route_reason="run_cancel_control",
        )
        return {
            "owner": "siming",
            "reply_epoch": own["reply_epoch"],
            "envelope_key": own["envelope_key"],
            "route_tier": "tier0",
            "scenario": "run_cancel",
            "thread_id": "",
            "ref": None,
            "sub_intents": [],
            "sensitive_isolated": sensitive,
            "control_action": {"kind": "CANCEL", "run_id": target_run, "cancel_all": cancel_all},
        }

    if _STEER_RE.match(clean):
        conn = db.connect_readonly()
        try:
            active_run = conn.execute(
                "SELECT run_id, ticket_id FROM runs WHERE state IN ('RUNNING', 'STEERED') ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        if active_run:
            own = assign_message_owner(
                db,
                channel=channel,
                account=account,
                conversation=conversation,
                message_id=msg_id,
                owner="siming",
                route_tier="tier0",
                route_reason="run_steer_control",
            )
            return {
                "owner": "siming",
                "reply_epoch": own["reply_epoch"],
                "envelope_key": own["envelope_key"],
                "route_tier": "tier0",
                "scenario": "run_steer",
                "thread_id": "",
                "ref": None,
                "sub_intents": [],
                "sensitive_isolated": sensitive,
                "control_action": {"kind": "STEER", "run_id": str(active_run["run_id"]), "instruction": clean},
            }

    # 3. 显式多编码 Agent 协作（如「让 Codex 改，Claude Code 审」）与会话恢复
    m_ma = _MULTI_AGENT_EDIT_REVIEW_RE.search(clean)
    if m_ma:
        own = assign_message_owner(
            db,
            channel=channel,
            account=account,
            conversation=conversation,
            message_id=msg_id,
            owner="siming",
            route_tier="tier0",
            route_reason="multi_agent_coding",
        )
        return {
            "owner": "siming",
            "reply_epoch": own["reply_epoch"],
            "envelope_key": own["envelope_key"],
            "route_tier": "tier0",
            "scenario": "multi_agent_coding",
            "thread_id": "",
            "ref": None,
            "sub_intents": [
                {"role": "editor", "adapter": m_ma.group(1).lower().replace(" ", "")},
                {"role": "reviewer", "adapter": m_ma.group(2).lower().replace(" ", "")},
            ],
            "sensitive_isolated": sensitive,
            "control_action": None,
        }

    if _RESUME_SESSION_RE.search(clean):
        own = assign_message_owner(
            db,
            channel=channel,
            account=account,
            conversation=conversation,
            message_id=msg_id,
            owner="siming",
            route_tier="tier0",
            route_reason="session_resume",
        )
        return {
            "owner": "siming",
            "reply_epoch": own["reply_epoch"],
            "envelope_key": own["envelope_key"],
            "route_tier": "tier0",
            "scenario": "session_resume",
            "thread_id": "",
            "ref": None,
            "sub_intents": [],
            "sensitive_isolated": sensitive,
            "control_action": None,
        }

    # 4. 混合多意图（如「解释下这个框架，等周五提醒我试试」）：Hermes 编排一次回复 + 司命拆出子提醒（绝不隐式授权付款）
    m_hyb = _HYBRID_EXPLAIN_AND_REMIND_RE.match(clean)
    if m_hyb and not re.search(r"(?:确认支付|确认付款|apply\s+A\d+)", clean, re.IGNORECASE):
        own = assign_message_owner(
            db,
            channel=channel,
            account=account,
            conversation=conversation,
            message_id=msg_id,
            owner="hermes",
            route_tier="light_llm",
            route_reason="hybrid_chat_and_remind",
        )
        return {
            "owner": "hermes",
            "reply_epoch": own["reply_epoch"],
            "envelope_key": own["envelope_key"],
            "route_tier": "light_llm",
            "scenario": "hybrid_multi_intent",
            "thread_id": "",
            "ref": None,
            "sub_intents": [
                {"kind": "chat_explain", "owner": "hermes", "text": m_hyb.group("chat").strip()},
                {"kind": "create_wait", "owner": "siming", "text": m_hyb.group("remind").strip()},
            ],
            "sensitive_isolated": sensitive,
            "control_action": None,
        }

    # 5. 纯闲聊快速放行给 Hermes
    if _CASUAL_CHAT_ONLY_RE.match(clean) and not media:
        own = assign_message_owner(
            db,
            channel=channel,
            account=account,
            conversation=conversation,
            message_id=msg_id,
            owner="hermes",
            route_tier="tier0",
            route_reason="casual_chat_pass",
        )
        return {
            "owner": "hermes",
            "reply_epoch": own["reply_epoch"],
            "envelope_key": own["envelope_key"],
            "route_tier": "tier0",
            "scenario": "casual_chat",
            "thread_id": "",
            "ref": None,
            "sub_intents": [],
            "sensitive_isolated": False,
            "control_action": None,
        }

    # 6. Tier-0 本地确定性判断（跳过外网 JEV，严格隔离敏感载荷）
    from siming.jev_gate import evaluate_siming_takeover, is_tier0_siming_bypass

    t0_hit, t0_reason = is_tier0_siming_bypass(
        clean, reply_to_id="", reply_to_text=reply_to_text, media=media
    )
    if t0_hit or sensitive:
        own = assign_message_owner(
            db,
            channel=channel,
            account=account,
            conversation=conversation,
            message_id=msg_id,
            owner="siming",
            route_tier="tier0",
            route_reason=t0_reason or "sensitive_local_only",
        )
        return {
            "owner": "siming",
            "reply_epoch": own["reply_epoch"],
            "envelope_key": own["envelope_key"],
            "route_tier": "tier0",
            "scenario": t0_reason or "sensitive_local",
            "thread_id": "",
            "ref": None,
            "sub_intents": [],
            "sensitive_isolated": sensitive,
            "control_action": None,
        }

    # 7. Tier-1 JEV（700ms 墙钟预算；超时或高置信闲聊归还 Hermes）
    jev_fn = jev_eval_fn or evaluate_siming_takeover
    try:
        jres = jev_fn(clean, reply_to_id="", reply_to_text=reply_to_text, media=None)
    except Exception:
        jres = {"should_siming_handle": False, "reason": "jev_error_fallback_hermes"}

    owner = "siming" if jres.get("should_siming_handle") else "hermes"
    own = assign_message_owner(
        db,
        channel=channel,
        account=account,
        conversation=conversation,
        message_id=msg_id,
        owner=owner,
        route_tier="jev",
        route_reason=str(jres.get("reason") or jres.get("choice") or "jev"),
    )
    return {
        "owner": owner,
        "reply_epoch": own["reply_epoch"],
        "envelope_key": own["envelope_key"],
        "route_tier": "jev",
        "scenario": str(jres.get("choice") or ("siming_task" if owner == "siming" else "hermes_chat")),
        "thread_id": "",
        "ref": None,
        "jev_decision": jres,
        "sub_intents": [],
        "sensitive_isolated": False,
        "control_action": None,
    }


def siming_query_for_hermes(db: SimingDB, query: str) -> dict[str, Any]:
    """供 Hermes 在聊天中按需读取司命个人事实的窄只读接口（带来源与采集时间，不泄全量隐私原文）。"""
    from siming.router import handle_inbound_qq

    res = handle_inbound_qq(db, f"查一下 {query}".strip())
    return {
        "ok": bool(res.get("handled")),
        "query": query,
        "answer": str(res.get("reply") or ""),
        "source": "siming_projection",
        "as_of": iso_now(),
    }
