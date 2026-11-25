"""全机唯一 QQ 出口与入口交互网关（router）：去重即时达、引用回复直达、14d 完结续接、handback、晨札与回家取件窗。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from siming.approval import refresh_approval_ttl_on_delivery, verify_and_apply_approval
from siming.cn_calendar import day_info, try_parse_explicit_datetime
from siming.config import load_policy
from siming.crypto import redact_sensitive
from siming.db import SimingDB, iso_now, now_local, parse_iso
from siming.enrich import detect_hard_rule_conflicts
from siming.waits import add_wait, cancel_wait, list_waits


def _in_quiet_hours(
    policy: dict[str, Any],
    db: Optional[SimingDB] = None,
    now: Optional[datetime] = None,
) -> bool:
    if db is not None:
        from siming.attention import evaluate_attention_state

        return bool(evaluate_attention_state(db, policy=policy, now_override=now)["should_hold_l2"])
    cur = now or now_local()
    now_t = cur.strftime("%H:%M")
    q = policy.get("quiet_hours") or {"start": "23:30", "end": "07:30"}
    start, end = str(q.get("start", "23:30")), str(q.get("end", "07:30"))
    if start > end:
        return now_t >= start or now_t < end
    return start <= now_t < end


_QQ_INLINE_QUOTE_RE = re.compile(
    r"^\s*(?:\[Quoted message\]|\[引用消息\]|【引用消息】)\s*[:：]?\s*\r?\n([\s\S]+)$",
    re.IGNORECASE,
)


def split_qq_quoted_message(
    text: str,
    reply_to_text: Optional[str] = None,
) -> tuple[str, Optional[str]]:
    """拆解 QQBot C2C 内联引用格式（`[Quoted message]:\\n<原消息>\\n\\n<回复正文>`）。

    返回 `(clean_reply, effective_reply_to_text)`，确保引用销账、审批确认与打针回复
    在网关与路由层 100% 确定性命中，绝不因 `reply_to_text` 为空而漏给 LLM。
    """
    raw = (text or "").strip()
    existing_quote = (reply_to_text or "").strip() or None
    if not raw:
        return "", existing_quote
    m = _QQ_INLINE_QUOTE_RE.match(raw)
    if not m:
        return raw, existing_quote
    body = m.group(1).strip()
    # 优先按最后一个空行切分（原消息内部可能有多段换行，用户回复在末尾空行之后）
    blank_splits = list(re.finditer(r"\r?\n[ \t]*\r?\n", body))
    if blank_splits:
        last_sep = blank_splits[-1]
        q_part = body[: last_sep.start()].strip()
        r_part = body[last_sep.end() :].strip()
        if q_part and r_part:
            eff_q = f"{existing_quote}\n{q_part}".strip() if (existing_quote and q_part not in existing_quote) else (existing_quote or q_part)
            return r_part, eff_q
    # 兜底：无空行时若最后一行是短回复，按最后一个换行切分
    lines = [ln.rstrip() for ln in body.splitlines() if ln.strip()]
    if len(lines) >= 2 and len(lines[-1].strip()) <= 64:
        q_part = "\n".join(lines[:-1]).strip()
        r_part = lines[-1].strip()
        eff_q = f"{existing_quote}\n{q_part}".strip() if (existing_quote and q_part not in existing_quote) else (existing_quote or q_part)
        return r_part, eff_q
    return raw, existing_quote


def _rule_allows_push(conn, rule_id: str) -> tuple[bool, bool]:
    """返回 (allowed_to_push, prepend_trial_badge)。"""
    row = conn.execute("SELECT * FROM rule_stats WHERE rule_id = ?", (rule_id,)).fetchone()
    if not row:
        return True, False
    if row["mode"] in ("DEMOTED", "FROZEN") and not int(row["hard_rule"]):
        return False, False
    if row["mode"] == "OBSERVE":
        return False, False
    return True, (row["mode"] == "TRIAL")


def _hermes_send(text: str, target: str, hermes_bin: str) -> tuple[bool, str, str]:
    """调用本机 hermes send --json 发送消息（不经模型），返回 (ok, msg_id, err)。"""
    if os.environ.get("SIMING_DRY_RUN_SEND") == "1":
        fake_id = "dryrun-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
        return True, fake_id, ""

    if not Path(hermes_bin).is_file():
        return False, "", f"hermes binary missing: {hermes_bin}"

    env = dict(os.environ)
    env["TMPDIR"] = str(Path.home() / ".cache" / "agent-tmp")
    proc = subprocess.run(
        [hermes_bin, "send", "--to", target, "--json", text],
        env=env,
        capture_output=True,
        text=True,
        timeout=25,
    )
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        return False, "", (proc.stderr or out)[:240]

    msg_id = ""
    try:
        parsed = json.loads(out)
        if isinstance(parsed, dict):
            msg_id = str(
                parsed.get("message_id")
                or parsed.get("msg_id")
                or parsed.get("id")
                or ""
            )
    except Exception:
        pass
    return True, msg_id, ""


def send_qq_message(
    db: SimingDB,
    text: str,
    ref_id: str = "",
    rule_id: str = "system",
    trial: bool = False,
    urgent: bool = False,
    bypass_quiet: bool = False,
    sub_meta: Optional[dict[str, Any]] = None,
    dedup_window: Optional[timedelta] = None,
    keyboard: Optional[dict[str, Any]] = None,
    _guard_bypass: bool = False,
) -> dict[str, Any]:
    """统一出口：执行去重、降级过滤、推送熔断/隐式降级（push_guard）、安静时段暂存、QQ 原生确认框键盘与 QQ->飞书降级，并建立 msg_id -> 单据订阅。

    dedup_window：同 ref_id+正文的去重窗口，默认 24h；传 timedelta(0) 关闭去重
    （外部脚本自带变化检测、同一裸码会合法重复出现时使用，见 `siming push --dedup`）。
    _guard_bypass：仅供 push_guard 自身发熔断说明使用，其它调用方不要传。
    """
    db.init_db()
    policy = load_policy(db.paths)
    clean_text = redact_sensitive(text.strip(), db.paths)
    if not clean_text:
        return {"ok": False, "skipped": "empty"}

    dedup_hash = hashlib.sha256(f"{ref_id}:{clean_text}".encode("utf-8")).hexdigest()[:32]
    window = timedelta(hours=24) if dedup_window is None else dedup_window
    dedup_cutoff = iso_now(now_local() - window)

    conn = db.connect()
    try:
        dup = None
        if window.total_seconds() > 0:
            dup = conn.execute(
                "SELECT id FROM outbox WHERE dedup_hash = ? AND status IN ('SENT', 'MORNING_QUEUE') AND created_at >= ?",
                (dedup_hash, dedup_cutoff),
            ).fetchone()
        if dup:
            return {"ok": True, "delivered": False, "skipped": "duplicate", "outbox_id": dup["id"]}

        allow_push, rule_is_trial = _rule_allows_push(conn, rule_id)
        if not allow_push and not urgent:
            return {"ok": True, "delivered": False, "skipped": "rule_demoted_or_observe"}

        if (trial or rule_is_trial) and not clean_text.startswith("[试运行]"):
            clean_text = f"[试运行] {clean_text}"

        # D-33 推送熔断与隐式降级：单规则/全局小时上限 + 冷却 + 14 天零处理自动 QUIET（只进晨札汇总）
        if not _guard_bypass:
            from siming.push_guard import GUARD_RULE_ID, guard_outbound

            g_res, g_notice = guard_outbound(
                db,
                conn,
                rule_id=rule_id,
                text=clean_text,
                ref_id=ref_id,
                dedup_hash=dedup_hash,
                urgent=urgent,
                policy=policy,
            )
            if g_res is not None:
                if g_notice:
                    from siming.qq_cards import make_button, make_keyboard

                    g_kb = make_keyboard(
                        [[make_button("restore", "🔔 恢复推送", f"siming:cmd:恢复推送 {rule_id}", visited_label="已恢复", style=0, group_id=f"guard_{rule_id}"[:32])]]
                    )
                    g_res["notice"] = send_qq_message(
                        db,
                        g_notice,
                        ref_id=f"guard:{rule_id}:notice",
                        rule_id=GUARD_RULE_ID,
                        dedup_window=timedelta(hours=6),
                        keyboard=g_kb,
                        _guard_bypass=True,
                    )
                return g_res

        if not bypass_quiet and not urgent and _in_quiet_hours(policy, db=db):
            ob_id = f"OB-Q-{hashlib.sha1((dedup_hash + iso_now()).encode()).hexdigest()[:10]}"
            with db.transaction(conn):
                conn.execute(
                    """
                    INSERT INTO outbox (id, ref_id, rule_id, text, send_at, status, channel, dedup_hash, created_at)
                    VALUES (?, ?, ?, ?, ?, 'MORNING_QUEUE', 'qqbot', ?, ?)
                    """,
                    (ob_id, ref_id, rule_id, clean_text, iso_now(), dedup_hash, iso_now()),
                )
            return {"ok": True, "delivered": False, "queued_for_morning": True, "outbox_id": ob_id}
    finally:
        conn.close()

    hermes_bin = str(policy["channels"]["hermes_bin"])
    qq_target = str(policy["channels"]["qq_target"])
    feishu_target = str(policy["channels"]["feishu_fallback"])

    from siming.qq_cards import infer_keyboard_for_message, send_qq_rest_with_keyboard

    effective_keyboard = keyboard
    if effective_keyboard is None:
        effective_keyboard = infer_keyboard_for_message(
            db,
            clean_text,
            ref_id=ref_id,
            rule_id=rule_id,
            sub_meta=sub_meta,
        )

    ok = False
    msg_id = ""
    err = ""
    kb_err = ""
    sent_with_kb = False
    if effective_keyboard is not None:
        ok, msg_id, err = send_qq_rest_with_keyboard(
            clean_text,
            qq_target,
            effective_keyboard,
            cache_dir=db.paths.tmp_root,
        )
        sent_with_kb = bool(ok)
        if not ok:
            kb_err = err
    if not ok:
        ok, msg_id, err = _hermes_send(clean_text, qq_target, hermes_bin)
    used_channel = "qqbot"
    if not ok and feishu_target:
        ok, msg_id, err = _hermes_send(clean_text, feishu_target, hermes_bin)
        used_channel = "feishu"
        sent_with_kb = False

    ts = iso_now()
    ob_id = f"OB-{now_local().strftime('%Y%m%d%H%M%S')}-{dedup_hash[:6]}"
    if window.total_seconds() <= 0:
        # 关闭去重时同秒同文是合法的（如连续两条同裸码私信），补随机后缀防主键冲突
        ob_id += "-" + os.urandom(3).hex()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO outbox (
                id, ref_id, rule_id, text, send_at, status, channel,
                dedup_hash, sent_msg_id, created_at, sent_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ob_id,
                ref_id,
                rule_id,
                clean_text,
                ts,
                "SENT" if ok else "FAILED",
                used_channel,
                dedup_hash,
                msg_id or None,
                ts,
                ts if ok else None,
            ),
        )
        conn.execute(
            """
            INSERT INTO rule_stats (rule_id, mode, sent_14d, updated_at)
            VALUES (?, ?, 1, ?)
            ON CONFLICT(rule_id) DO UPDATE SET
                sent_14d = rule_stats.sent_14d + 1,
                updated_at = excluded.updated_at
            """,
            (rule_id, "TRIAL" if trial else "ACTIVE", ts),
        )

        # 绑定 QQ msg_id 订阅（支持直接点引用回复免背编号；含晨札全文、stage 审批口令与批量取件）
        meta_obj: dict[str, Any] = {"rule_id": rule_id}
        if sub_meta:
            meta_obj.update(sub_meta)
        m_app_tok = re.search(r"\bapply\s+(A\d{2,6})\b", clean_text, re.IGNORECASE)
        if m_app_tok:
            meta_obj["approval_token"] = m_app_tok.group(1).upper()

        if ok and msg_id:
            if ref_id.startswith("brief:"):
                conn.execute(
                    """
                    INSERT INTO subscriptions (sub_key, target_kind, target_id, state, created_at, metadata_json)
                    VALUES (?, 'morning_brief', ?, 'ACTIVE', ?, ?)
                    ON CONFLICT(sub_key) DO UPDATE SET target_kind='morning_brief', target_id=excluded.target_id, state='ACTIVE', metadata_json=excluded.metadata_json
                    """,
                    (f"qqmsg:{msg_id}", ref_id, ts, json.dumps(meta_obj, ensure_ascii=False)),
                )
            elif ref_id.startswith("bill:"):
                conn.execute(
                    """
                    INSERT INTO subscriptions (sub_key, target_kind, target_id, state, created_at, metadata_json)
                    VALUES (?, 'staged_bill', ?, 'ACTIVE', ?, ?)
                    ON CONFLICT(sub_key) DO UPDATE SET target_kind='staged_bill', target_id=excluded.target_id, state='ACTIVE', metadata_json=excluded.metadata_json
                    """,
                    (f"qqmsg:{msg_id}", ref_id, ts, json.dumps(meta_obj, ensure_ascii=False)),
                )
            elif ref_id.startswith("TX-"):
                conn.execute(
                    """
                    INSERT INTO subscriptions (sub_key, target_kind, target_id, state, created_at, metadata_json)
                    VALUES (?, 'ledger_txn', ?, 'ACTIVE', ?, ?)
                    ON CONFLICT(sub_key) DO UPDATE SET target_kind='ledger_txn', target_id=excluded.target_id, state='ACTIVE', metadata_json=excluded.metadata_json
                    """,
                    (f"qqmsg:{msg_id}", ref_id, ts, json.dumps(meta_obj, ensure_ascii=False)),
                )
            elif ref_id.startswith("rpl:"):
                rpl_iid = (sub_meta or {}).get("item_id") or (ref_id.split(":", 2)[1] if ":" in ref_id else "")
                if rpl_iid:
                    conn.execute(
                        """
                        INSERT INTO subscriptions (sub_key, target_kind, target_id, state, created_at, metadata_json)
                        VALUES (?, 'replenish', ?, 'ACTIVE', ?, ?)
                        ON CONFLICT(sub_key) DO UPDATE SET target_kind='replenish', target_id=excluded.target_id, state='ACTIVE', metadata_json=excluded.metadata_json
                        """,
                        (f"qqmsg:{msg_id}", str(rpl_iid), ts, json.dumps(meta_obj, ensure_ascii=False)),
                    )
            else:
                target_ids = re.findall(r"\b([SW]\d{2,6})\b", f"{ref_id} {clean_text}")
                primary_target = ref_id if re.fullmatch(r"[SW]\d{2,6}", ref_id) else (target_ids[0] if target_ids else "")
                if primary_target:
                    t_kind = "case" if primary_target.startswith("S") else "wait"
                    conn.execute(
                        """
                        INSERT INTO subscriptions (sub_key, target_kind, target_id, state, created_at, metadata_json)
                        VALUES (?, ?, ?, 'ACTIVE', ?, ?)
                        ON CONFLICT(sub_key) DO UPDATE SET target_id=excluded.target_id, state='ACTIVE', metadata_json=excluded.metadata_json
                        """,
                        (
                            f"qqmsg:{msg_id}",
                            t_kind,
                            primary_target,
                            ts,
                            json.dumps(meta_obj, ensure_ascii=False),
                        ),
                    )
    return {
        "ok": ok,
        "delivered": ok,
        "channel": used_channel,
        "msg_id": msg_id,
        "outbox_id": ob_id,
        "error": err,
        "keyboard_error": kb_err,
        "has_keyboard": sent_with_kb,
    }


def record_feedback(
    db: SimingDB, target_id: str, action: str, note: str = ""
) -> dict[str, Any]:
    """记录用户反馈（已办 / 改期 / 少推 / 别再提），按真实 14 天滑动窗口计算接受率，连拒 3 次或 <20% 自动降级非硬规则。"""
    tid = target_id.strip().upper()
    now_dt = now_local()
    ts = iso_now(now_dt)
    cutoff_14d = iso_now(now_dt - timedelta(days=14))
    demoted = False
    rule_id = "manual"

    with db.transaction() as conn:
        if tid.startswith("S"):
            r = conn.execute("SELECT rule_id, strength FROM cases WHERE case_id=?", (tid,)).fetchone()
            if r:
                rule_id = str(r["rule_id"])
                is_hard = r["strength"] == "HARD"
            else:
                is_hard = False
        elif tid.startswith("W"):
            ob_r = conn.execute(
                "SELECT rule_id FROM outbox WHERE ref_id=? ORDER BY created_at DESC LIMIT 1", (tid,)
            ).fetchone()
            rule_id = str(ob_r["rule_id"]) if (ob_r and ob_r["rule_id"]) else "wait_remind"
            is_hard = False
        else:
            rule_id = tid.lower() or "general"
            is_hard = False

        conn.execute(
            "INSERT INTO feedback (target_id, rule_id, action, note, ts) VALUES (?, ?, ?, ?, ?)",
            (tid, rule_id, action, note[:200], ts),
        )

        st = conn.execute("SELECT * FROM rule_stats WHERE rule_id=?", (rule_id,)).fetchone()
        consec = int(st["consecutive_dismiss"]) if st else 0
        hard_flag = 1 if (is_hard or (st and int(st["hard_rule"]))) else 0

        if action in ("少推", "别再提", "dismiss"):
            consec += 1
        elif action in ("已办", "改期", "acted"):
            consec = 0
            db.record_human_use(rule_id, action, ref_id=tid, conn=conn)

        # 按真实 14d 滑动时间窗重算 sent_14d / acted_14d / dismissed_14d
        acted = int(
            conn.execute(
                "SELECT COUNT(*) FROM feedback WHERE rule_id=? AND ts>=? AND action IN ('已办', '改期', 'acted')",
                (rule_id, cutoff_14d),
            ).fetchone()[0]
        )
        dismissed = int(
            conn.execute(
                "SELECT COUNT(*) FROM feedback WHERE rule_id=? AND ts>=? AND action IN ('少推', '别再提', 'dismiss')",
                (rule_id, cutoff_14d),
            ).fetchone()[0]
        )
        sent = int(
            conn.execute(
                "SELECT COUNT(*) FROM outbox WHERE rule_id=? AND status='SENT' AND sent_at>=?",
                (rule_id, cutoff_14d),
            ).fetchone()[0]
        ) or (int(st["sent_14d"]) if st else 1)

        prev_mode = st["mode"] if st else "ACTIVE"
        mode = "ACTIVE" if prev_mode == "FROZEN" and action in ("已办", "改期", "acted") else prev_mode
        total_fb = acted + dismissed
        accept_rate = (acted / max(1, total_fb)) if total_fb >= 5 else 1.0
        if not hard_flag and (action == "别再提" or consec >= 3 or accept_rate < 0.20):
            mode = "DEMOTED"
            demoted = True

        conn.execute(
            """
            INSERT INTO rule_stats (
                rule_id, mode, hard_rule, consecutive_dismiss, sent_14d, acted_14d, dismissed_14d, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(rule_id) DO UPDATE SET
                mode = excluded.mode,
                consecutive_dismiss = excluded.consecutive_dismiss,
                sent_14d = excluded.sent_14d,
                acted_14d = excluded.acted_14d,
                dismissed_14d = excluded.dismissed_14d,
                updated_at = excluded.updated_at
            """,
            (rule_id, mode, hard_flag, consec, sent, acted, dismissed, ts),
        )
    return {"ok": True, "rule_id": rule_id, "demoted": demoted, "mode": mode}


_UNTRUSTED_QUOTE_INJECTION_RE = re.compile(
    r"(忽略(?:之前|以上|前面|所有|全部)?(?:指令|规则|限制|约束|提示)|ignore\s+(?:previous|all|prior)\s+instructions?|system\s+prompt|prompt\s*injection|越权|注入|"
    r"朋友转发|同事转发|群里转发|转发(?:消息|内容|一下|给你|片段|原文)?|摘录|网页(?:内容|原文)|邮件原文|forwarded|snippet|会议室|航班号|工位号)",
    re.IGNORECASE,
)
_TRUSTED_APPROVAL_CARD_FRAMING_RE = re.compile(
    r"(确认卡|付款许可|审批|口令|分钟内|有效期|确认支付|确认下单|发单|合入|一键执行|点下方按钮|改约|去结算|已锁定首选|取消\s+A\d{2,6}|捆绑|加购)",
    re.IGNORECASE,
)


def _extract_trusted_quoted_approval_ref(
    db: SimingDB,
    reply_to_msg_id: Optional[str],
    reply_to_text: Optional[str],
) -> str:
    """仅从可信的司命审批确认卡（已绑定订阅/outbox 记录，或带真实审批卡语义且无转发/提示注入标记的引用原文）提取口令或捆绑号。

    严防外部转发文本、提示词注入（如「忽略规则 apply A05」）或普通聊天（「A05 会议室」）被误当作审批确认引用！
    """
    conn = db.connect()
    try:
        # 1. 若有 QQ reply_to_msg_id，优先核查是否为司命真实发出的订阅或 outbox 消息
        if reply_to_msg_id:
            sub = conn.execute(
                "SELECT state, target_kind, target_id, metadata_json FROM subscriptions WHERE sub_key = ?",
                (f"qqmsg:{reply_to_msg_id}",),
            ).fetchone()
            if sub:
                if sub["state"] == "CANCELLED":
                    return ""
                try:
                    meta = json.loads(sub["metadata_json"] or "{}")
                except Exception:
                    meta = {}
                tok = str(meta.get("approval_token") or "").strip().upper()
                if tok:
                    return tok
                if sub["target_kind"] == "approval" and sub["target_id"]:
                    return str(sub["target_id"]).strip().upper()
            ob = conn.execute(
                "SELECT text FROM outbox WHERE sent_msg_id = ? ORDER BY created_at DESC LIMIT 1",
                (reply_to_msg_id,),
            ).fetchone()
            if ob:
                ob_txt = str(ob["text"] or "")
                m_ob = re.search(r"\b(?:apply\s+)?(A\d{2,6}|BND-[A-Z0-9]{4,16})\b", ob_txt, re.IGNORECASE)
                return m_ob.group(1).upper() if m_ob else ""
            # 提供了非司命发出的 msg_id -> 绝不信任陌生消息正文里的伪造 apply Axx！
            return ""

        q_txt = (reply_to_text or "").strip()
        if not q_txt:
            return ""

        # 2. 拒绝任何带有转发/提示词注入/非审批语境特征的引用正文
        if _UNTRUSTED_QUOTE_INJECTION_RE.search(q_txt):
            return ""

        # 3. 提取候选口令：仅接受 `apply Axx`、`BND-...` 或反引号/方括号包裹的 `Axx`，绝不裸匹配普通词「A05 会议室」
        m_bnd = re.search(r"\b(BND-[A-Z0-9]{4,16})\b", q_txt, re.IGNORECASE)
        m_apply = re.search(r"\bapply\s+(A\d{2,6})\b", q_txt, re.IGNORECASE)
        m_brack = re.search(r"[`\[]\s*(A\d{2,6})\s*[`\]]", q_txt, re.IGNORECASE)
        cand = (
            m_bnd.group(1).upper()
            if m_bnd
            else (m_apply.group(1).upper() if m_apply else (m_brack.group(1).upper() if m_brack else ""))
        )
        if not cand:
            return ""

        # 4. 必须命中司命真实审批卡话术结构，或与司命真实发出过的 outbox 正文匹配
        if _TRUSTED_APPROVAL_CARD_FRAMING_RE.search(q_txt):
            return cand
        ob_hit = conn.execute(
            "SELECT 1 FROM outbox WHERE text = ? OR text LIKE ? LIMIT 1",
            (q_txt, f"%{cand}%"),
        ).fetchone()
        if ob_hit and m_apply:
            return cand
        return ""
    finally:
        conn.close()


def resolve_subscription_for_reply(
    db: SimingDB,
    reply_to_msg_id: Optional[str],
    reply_to_text: Optional[str],
) -> Optional[dict[str, Any]]:
    """根据 QQ 引用回复的 msg_id 或被引原文里的编号，定位原单（含晨札全文展开、批量取件与 14d 完结续接）。"""
    policy = load_policy(db.paths)
    resume_days = int(policy["guardrails"].get("ended_subscription_resume_days", 14))
    cutoff = now_local() - timedelta(days=resume_days)

    conn = db.connect()
    try:
        target_kind = ""
        target_id = ""
        sub_meta: dict[str, Any] = {}
        if reply_to_msg_id:
            sub = conn.execute(
                "SELECT * FROM subscriptions WHERE sub_key = ?",
                (f"qqmsg:{reply_to_msg_id}",),
            ).fetchone()
            if sub:
                if sub["state"] == "CANCELLED":
                    return None
                if sub["ended_at"] and parse_iso(sub["ended_at"]) < cutoff:
                    return None
                target_kind = str(sub["target_kind"])
                target_id = str(sub["target_id"])
                try:
                    sub_meta = json.loads(sub["metadata_json"] or "{}")
                except Exception:
                    sub_meta = {}

        if not target_id and reply_to_text:
            if "引用回复「全文」" in reply_to_text or "司命晨札" in reply_to_text:
                m_bd = re.search(r"(20\d{2}-\d{2}-\d{2})", reply_to_text)
                b_date = m_bd.group(1) if m_bd else now_local().date().isoformat()
                return {"target_kind": "morning_brief", "target_id": f"brief:{b_date}", "meta": {}}
            trusted_app = _extract_trusted_quoted_approval_ref(db, reply_to_msg_id, reply_to_text)
            if trusted_app:
                return {"target_kind": "approval", "target_id": trusted_app, "meta": {"approval_token": trusted_app}}
            if "顺路取件" in reply_to_text or "顺路待取包裹" in reply_to_text or "回家包裹清单" in reply_to_text:
                batch_ids = [m.upper() for m in re.findall(r"\[(S\d{2,6})\]", reply_to_text)]
                if batch_ids:
                    return {"target_kind": "case", "target_id": batch_ids[0], "meta": {"batch_case_ids": batch_ids}}
            if "邮箱收到加密账单附件" in reply_to_text or "已收到加密账单" in reply_to_text:
                return {"target_kind": "staged_bill", "target_id": "bill:pending", "meta": {}}
            if "外箱初验" in reply_to_text:
                return {"target_kind": "arrival_check", "target_id": "arrival:1210", "meta": {}}
            m_tx = re.search(r"\b(TX-\d{14}-[0-9a-fA-F]{4,8})\b", reply_to_text)
            if m_tx:
                return {"target_kind": "ledger_txn", "target_id": m_tx.group(1), "meta": {}}
            m_id = re.search(r"\[([SW]\d{2,6})\]|\b([SW]\d{2,6})\b", reply_to_text)
            if m_id:
                target_id = (m_id.group(1) or m_id.group(2)).upper()
                target_kind = "case" if target_id.startswith("S") else "wait"

        if not target_id:
            return None

        if target_kind in ("morning_brief", "staged_bill", "ledger_txn", "arrival_check"):
            return {"target_kind": target_kind, "target_id": target_id, "meta": sub_meta}

        # 校验完结时间窗口与显式取消标记
        if target_kind == "case":
            c = conn.execute("SELECT status, closed_at FROM cases WHERE case_id=?", (target_id,)).fetchone()
            if not c or c["status"] == "取消":
                return None
            if c["closed_at"] and parse_iso(c["closed_at"]) < cutoff:
                return None
        elif target_kind == "wait":
            w = conn.execute("SELECT status, updated_at FROM waits WHERE wait_id=?", (target_id,)).fetchone()
            if not w or w["status"] == "CANCELLED":
                return None
            if w["status"] in ("DONE", "EXPIRED") and parse_iso(w["updated_at"]) < cutoff:
                return None

        return {"target_kind": target_kind, "target_id": target_id, "meta": sub_meta}
    finally:
        conn.close()


_UNRELATED_NEW_TOPIC_RE = re.compile(
    r"^(?:顺便问下|另外问个|换个话题|查\s+|派单\s+|帮我查)"
)
_EXPLICIT_CASE_PROGRESS_PREFIX_RE = re.compile(
    r"^(?:约了?|已约|预约了?|改期|改到?|定在|推迟到?|安排在|备注[：:\s]|进展[：:\s]|单号[：:\s]|师傅|重开|还没办)"
)
_CASUAL_POLITE_RE = re.compile(
    r"^(?:好的?|好嘞|收到|谢谢|多谢|辛苦|明白|知道了|嗯+|ok|okay)(?:[，,。.！!~～\s]|$)",
    re.IGNORECASE,
)


def _handle_subscribed_reply(
    db: SimingDB,
    target_kind: str,
    target_id: str,
    text: str,
    sub_meta: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """直达处理对已有晨札 / case / wait 的确定性引用回复；兼容 QQ 语音标点，寒暄口头禅绝不误重开已结单据！"""
    from siming.qq_nlu import classify_quoted_reply, normalize_qq_input

    u_q = normalize_qq_input(text)
    t = u_q.raw
    t_stmt = u_q.stmt
    t_q = u_q.query
    if _UNRELATED_NEW_TOPIC_RE.search(t):
        return {"handled": False, "handback": True}

    if target_kind == "morning_brief":
        q_cls = classify_quoted_reply(t, target_kind)
        if q_cls and q_cls.get("action") == "morning_full":
            from siming.morning import get_morning_full_reply

            date_part = target_id.removeprefix("brief:")
            return {
                "handled": True,
                "reply": get_morning_full_reply(db, section_hint=str(q_cls.get("section") or ""), target_date=date_part),
            }
        return {"handled": False, "handback": True}

    if target_kind == "staged_bill":
        m_bpwd = re.fullmatch(r"(?:(?:密码|解压码)\s*[：:]?\s*)?([A-Za-z0-9]{4,16})", t_stmt)
        if m_bpwd:
            from siming.ledger.export_zip import import_bill_archive

            pwd_val = m_bpwd.group(1).strip()
            arc_str = str((sub_meta or {}).get("archive_path") or db.get_meta("pending_bill_archive") or "")
            if not arc_str or not Path(arc_str).is_file():
                return {"handled": True, "reply": "⚠️ 暂存加密账单已超过 30 分钟有效期并自动清理，请重新转发或重发邮件。"}
            arc_p = Path(arc_str)
            imp = import_bill_archive(db, arc_p, password=pwd_val, delete_source_archive=True)
            if imp.get("ok"):
                db.set_meta("pending_bill_archive", "")
                db.set_meta("pending_bill_archive_ts", "")
                db.set_meta("pending_bill_archive_tries", "0")
                return {
                    "handled": True,
                    "reply": (
                        f"✅ 解密成功！`{arc_p.name}` 已对账入库（原加密包已销毁）\n"
                        f"• 新增入账：{imp['ingested']} 笔 · 自动配对：{imp['matched']} 笔 · 待核实：{imp['ambiguous']} 笔"
                    ),
                }
            tries = int(db.get_meta("pending_bill_archive_tries") or "0") + 1
            if tries >= 2:
                arc_p.unlink(missing_ok=True)
                db.set_meta("pending_bill_archive", "")
                db.set_meta("pending_bill_archive_ts", "")
                db.set_meta("pending_bill_archive_tries", "0")
                return {"handled": True, "reply": "❌ 连续两次解压密码错误，为安全起见已销毁暂存包。"}
            db.set_meta("pending_bill_archive_tries", str(tries))
            return {"handled": True, "reply": "❌ 解压密码不正确（还可再试 1 次，直接引用本条回复正确密码）。"}
        return {"handled": False, "handback": True}

    if target_kind == "ledger_txn":
        from siming.ledger.reconcile import resolve_ledger_txn

        if re.fullmatch(r"(?:重复作废|作废重复|重复流水|作废)", t_stmt):
            res_l = resolve_ledger_txn(db, target_id, "void-dup")
            return {"handled": True, "reply": res_l.get("summary") or res_l.get("error", "")}
        m_pair = re.match(r"^配对\s+(TX-[0-9a-zA-Z-]+)$", t_stmt, re.IGNORECASE)
        if m_pair:
            res_l = resolve_ledger_txn(db, target_id, "pair", m_pair.group(1))
            return {"handled": True, "reply": res_l.get("summary") or res_l.get("error", "")}
        m_note = re.match(r"^(?:备注|确认)[：:\s]*(.+)$", t_stmt)
        if m_note:
            res_l = resolve_ledger_txn(db, target_id, "note", m_note.group(1))
            return {"handled": True, "reply": res_l.get("summary") or res_l.get("error", "")}
        return {"handled": False, "handback": True}

    if target_kind == "arrival_check" or re.match(r"^(?:已验外观|外箱完好|外箱都?验好了?)(?:\s+(.+))?$", u_q.core):
        q_arr = classify_quoted_reply(t, "arrival_check")
        if q_arr and q_arr.get("action") == "arrival_mark":
            from siming.install_plan import mark_arrival_inspected

            res_a = mark_arrival_inspected(db, target_query=str(q_arr.get("target") or "all"))
            return {"handled": True, "reply": res_a.get("summary") or f"⚠️ {res_a.get('error')}"}
        if target_kind == "arrival_check":
            return {"handled": False, "handback": True}

    bound_tok = str((sub_meta or {}).get("approval_token") or (target_id if target_kind == "approval" else "")).strip().upper()
    if bound_tok and not u_q.had_question and re.fullmatch(r"(?:apply|确认|发吧|好|好的|行|可以|1|合入|同意)", u_q.core, re.IGNORECASE):
        from siming.approval import resolve_and_apply_natural_confirm

        nat = resolve_and_apply_natural_confirm(db, "确认", quoted_ref=bound_tok)
        if nat.get("matched"):
            prefix = "✅" if nat.get("ok") else "❌"
            return {"handled": True, "reply": f"{prefix} {nat['reply']}"}
        res = verify_and_apply_approval(db, bound_tok)
        prefix = "✅" if res.get("ok") else "❌"
        return {
            "handled": True,
            "reply": f"{prefix} [{bound_tok}] {res.get('summary') or res.get('error')}",
        }

    # 查询 case 类别以便泛化引用意图分流（保价 / 打针 / 开药 / 取件 / 通用销账）
    case_cat = ""
    case_ctx_json = ""
    if target_kind == "case":
        conn_cat = db.connect()
        try:
            c_cat_row = conn_cat.execute("SELECT category, context_json FROM cases WHERE case_id=?", (target_id,)).fetchone()
            if c_cat_row:
                case_cat = str(c_cat_row["category"] or "")
                case_ctx_json = str(c_cat_row["context_json"] or "{}")
        finally:
            conn_cat.close()

    q_intent = classify_quoted_reply(t, target_kind, case_category=case_cat)
    q_act = str(q_intent.get("action") or "") if q_intent else ""

    # 取消 / 停止
    if q_act == "cancel":
        if target_kind == "wait":
            cancel_wait(db, target_id, reason="QQ 引用回复取消")
            return {"handled": True, "reply": f"[{target_id}] 已取消挂起并同步撤回预排提醒。"}
        ts = iso_now()
        with db.transaction() as conn:
            conn.execute(
                "UPDATE cases SET status='取消', closed_at=?, state_version=state_version+1, updated_at=? WHERE case_id=?",
                (ts, ts, target_id),
            )
            conn.execute(
                "UPDATE subscriptions SET state='CANCELLED', ended_at=? WHERE target_id=?",
                (ts, target_id),
            )
            db.enqueue_effect(conn, "edge_withdraw_outbox", {"ref_id": target_id})
        return {"handled": True, "reply": f"[{target_id}] 已取消归档，后续不再提醒。"}

    # 调频降噪
    if q_act == "mute":
        action = str((q_intent or {}).get("feedback_action") or "少推")
        fb = record_feedback(db, target_id, action, note=t_stmt)
        suffix = "（该规则已自动降为仅主动查询显示）" if fb["demoted"] else "（已记录频控偏好）"
        return {"handled": True, "reply": f"[{target_id}] 收到：{action}{suffix}。"}

    # 保价专属引用回复：已保价（更新底价水位继续盯二次降价） / 不看了（移出提醒）
    if target_kind == "case" and case_cat == "price_protect" and q_act in ("price_claimed", "price_mute"):
        from siming.price_watch import ack_price_watch_claimed, remove_price_watch

        ctx_pw = json.loads(case_ctx_json or "{}")
        ord_key = str(ctx_pw.get("order_key") or ctx_pw.get("order") or ctx_pw.get("sku") or "")
        ts = iso_now()
        with db.transaction() as conn:
            conn.execute(
                "UPDATE cases SET status='完成', closed_at=?, state_version=state_version+1, updated_at=? WHERE case_id=?",
                (ts, ts, target_id),
            )
        if q_act == "price_mute":
            if ord_key:
                remove_price_watch(db, ord_key, mute_only=True)
            record_feedback(db, target_id, "少推", note=t_stmt)
            return {"handled": True, "reply": f"[{target_id}] 已将该商品静音，不再发送保价提醒。"}
        if ord_key:
            ack_price_watch_claimed(db, ord_key, claimed_price_cents=ctx_pw.get("current_cents"))
        record_feedback(db, target_id, "已办", note=t_stmt)
        return {
            "handled": True,
            "reply": f"💰 [{target_id}] 已登记退差并更新保价基准水位（后续若再跌 ≥¥10 仍会自动提醒二次价保）。",
        }

    # 周期用药打针 / 开药专属引用回复（防止「已办」只关单不扣针数或被下一节拍重开）
    if target_kind == "case" and q_act == "med_inject":
        from siming.daily_care import record_injection

        return {"handled": True, "reply": record_injection(db, skipped=bool((q_intent or {}).get("skipped")))["summary"]}
    if target_kind == "case" and q_act == "med_booked":
        from siming.daily_care import record_booking

        return {"handled": True, "reply": record_booking(db)["summary"]}
    if target_kind == "case" and q_act == "med_refill":
        from siming.daily_care import _parse_small_cn_int, record_refill

        p_tok = str((q_intent or {}).get("pens_token") or "")
        pens_n = _parse_small_cn_int(p_tok) if p_tok else 1
        return {"handled": True, "reply": record_refill(db, pens_n or 1)["summary"]}

    # 已办核销（支持回家取件窗口合并卡整批销账与外卖入柜「拿了 / 已经拿了 / 刚取回来」销账！）
    if q_act == "done":
        ts = iso_now()
        if target_kind == "case":
            batch_ids = [str(x).upper() for x in ((sub_meta or {}).get("batch_case_ids") or []) if x]
            if len(batch_ids) >= 2 and any(k in t_stmt for k in ("取", "拿", "已办", "搞定", "办完", "办好", "弄好")):
                with db.transaction() as conn:
                    placeholders = ",".join("?" for _ in batch_ids)
                    conn.execute(
                        f"UPDATE cases SET status='完成', closed_at=?, state_version=state_version+1, updated_at=? WHERE case_id IN ({placeholders})",
                        (ts, ts, *batch_ids),
                    )
                    conn.execute(
                        f"UPDATE subscriptions SET state='ENDED', ended_at=? WHERE target_id IN ({placeholders})",
                        (ts, *batch_ids),
                    )
                for cid in batch_ids:
                    record_feedback(db, cid, "已办", note=t_stmt)
                return {
                    "handled": True,
                    "reply": f"📦 已整批销掉 {len(batch_ids)} 条取件码（{', '.join(batch_ids)}）。",
                }

            early_step_only = False
            with db.transaction() as conn:
                row = conn.execute("SELECT title, done_rule FROM cases WHERE case_id=?", (target_id,)).fetchone()
                # 步骤边界保护：若只是说“已签收”而该单还需要安装验收，推进到“待约安装”而非直接完结
                if row and ("签收" in t_stmt) and ("安装" in str(row["done_rule"]) or "安装" in str(row["title"])):
                    conn.execute(
                        "UPDATE cases SET next_step='已签收，待约上门安装与验收', state_version=state_version+1, updated_at=? WHERE case_id=?",
                        (ts, target_id),
                    )
                    early_step_only = True
                else:
                    conn.execute(
                        "UPDATE cases SET status='完成', closed_at=?, state_version=state_version+1, updated_at=? WHERE case_id=?",
                        (ts, ts, target_id),
                    )
                    conn.execute(
                        "UPDATE subscriptions SET state='ENDED', ended_at=? WHERE target_id=?",
                        (ts, target_id),
                    )
                    db.enqueue_effect(conn, "edge_withdraw_outbox", {"ref_id": target_id})
            if early_step_only:
                record_feedback(db, target_id, "改期", note=t_stmt)
                return {
                    "handled": True,
                    "reply": f"[{target_id}] 已核销收货步骤；保留「上门安装验收」待办。",
                }
            record_feedback(db, target_id, "已办", note=t_stmt)
            return {"handled": True, "reply": f"[{target_id}] 已销账归档（14 天内继续引用本条仍可补充续接）。"}
        cancel_wait(db, target_id, reason="用户标记已办")
        record_feedback(db, target_id, "已办", note=t_stmt)
        return {"handled": True, "reply": f"[{target_id}] 已标记完成并停跑探针。"}

    # 引用回复截屏约局提醒卡（wait）直接补充/修改目的地（如引用回「朝阳大悦城」/「孟帅家，望京SOHO」/「就在家」）
    if target_kind == "wait" and 1 <= len(t_stmt) <= 60:
        if not _CASUAL_POLITE_RE.search(t_stmt) and not re.search(r"^(?:为什么|查\s+|派单\s+)", t_stmt):
            conn_w = db.connect()
            try:
                exp_row = conn_w.execute(
                    "SELECT exp_id FROM expectations WHERE source_ref=? AND kind='outing' AND status IN ('ACTIVE','SHIFTED') LIMIT 1",
                    (target_id,),
                ).fetchone()
            finally:
                conn_w.close()
            if exp_row is not None:
                from siming.outing_chain import bind_commitment_destination

                b_res = bind_commitment_destination(db, t_stmt, wait_id=target_id)
                if b_res.get("ok"):
                    return {"handled": True, "reply": str(b_res["reply"])}

    # 确定性进展补充或改期（严守审批决定 3=A：客套寒暄如“谢谢，今天辛苦了”绝不重开已完成单据！）
    if target_kind == "case" and len(t) <= 120:
        has_explicit_prefix = bool(_EXPLICIT_CASE_PROGRESS_PREFIX_RE.search(t))
        is_casual = bool(_CASUAL_POLITE_RE.search(t)) and not re.search(r"(约|改期|改到|定在|推迟|安排|重开)", t)
        if not is_casual:
            try:
                parsed_time = try_parse_explicit_datetime(t, paths=db.paths)
            except ValueError:
                parsed_time = None
            conn_c = db.connect()
            try:
                cur_c = conn_c.execute("SELECT status FROM cases WHERE case_id=?", (target_id,)).fetchone()
                is_already_done = bool(cur_c and cur_c["status"] == "完成")
            finally:
                conn_c.close()

            # 对已完结单据：只有明确带「约了/改期/备注：/重开/还没办」前缀才允许续接重开，单含“今天/下班”绝不误重开！
            allow_mutate = has_explicit_prefix if is_already_done else (parsed_time is not None or has_explicit_prefix)
            if allow_mutate:
                sched_date = parsed_time[0].date() if parsed_time is not None else None
                new_due = iso_now(parsed_time[0]) if parsed_time is not None else None
                conflicts = detect_hard_rule_conflicts(t, scheduled_date=sched_date, db=db)
                ts = iso_now()
                with db.transaction() as conn:
                    conn.execute(
                        """
                        UPDATE cases
                        SET status='待处理', closed_at=NULL, next_step=?,
                            due_at=COALESCE(?, due_at), state_version=state_version+1, updated_at=?
                        WHERE case_id=?
                        """,
                        (t, new_due, ts, target_id),
                    )
                record_feedback(db, target_id, "改期", note=t)
                due_hint = f"（计划时间已更新为 {new_due[:16].replace('T', ' ')}）" if new_due else ""
                reply = f"[{target_id}] 已更新进展：{t}{due_hint}"
                if conflicts:
                    reply += "\n⚠️ " + "\n⚠️ ".join(c["message"] for c in conflicts)
                return {"handled": True, "reply": reply}

    return {"handled": False, "handback": True}


def explain_item(db: SimingDB, target_id: str) -> str:
    """全链路状态追踪解释器：直接回答 Sxx / Wxx「触发依据是什么」+「当前在管线哪一步」。"""
    tid = target_id.strip().upper()
    conn = db.connect()
    try:
        if tid.startswith("S"):
            row = conn.execute("SELECT * FROM cases WHERE case_id=?", (tid,)).fetchone()
            if not row:
                return f"未找到事项 {tid}。"
            evs = json.loads(row["evidence_ids"] or "[]")
            ev_lines: list[str] = []
            for eid in evs[:5]:
                er = conn.execute("SELECT source, occurred_at, summary, raw_ref FROM events WHERE event_id=?", (eid,)).fetchone()
                if er:
                    ev_lines.append(f"• {eid} ({er['source']} @ {er['occurred_at']}): {er['summary']} [{er['raw_ref'] or '无原件'}]")
            ob = conn.execute(
                "SELECT id, status, send_at, sent_at, edge_synced FROM outbox WHERE ref_id=? ORDER BY created_at DESC LIMIT 1",
                (tid,),
            ).fetchone()
            pipe_line = (
                f"最近投递：[{ob['id']}] status={ob['status']} (send_at={ob['send_at']}, sent_at={ob['sent_at'] or '-'}, 81兜底同步={bool(ob['edge_synced'])})"
                if ob
                else "最近投递：尚无外发推送记录"
            )
            return (
                f"[{tid}] 事项追踪与依据说明\n"
                f"标题：{row['title']}\n"
                f"当前所处阶段：{row['status']}（下一步：{row['next_step'] or '无'} · 截止：{row['due_at'] or '未设'}）\n"
                f"规则：{row['rule_id']}（强度 {row['strength']} · v{row['state_version']}）\n"
                f"{pipe_line}\n"
                f"证据链：\n" + ("\n".join(ev_lines) if ev_lines else "• 手工或系统直接创建")
            )
        if tid.startswith("W"):
            w = conn.execute("SELECT * FROM waits WHERE wait_id=?", (tid,)).fetchone()
            if not w:
                return f"未找到挂起项 {tid}。"
            effs = conn.execute(
                "SELECT effect_id, kind, status, retry_count, last_error, unit_name, next_run_at, completed_at FROM effects WHERE payload_json LIKE ? ORDER BY created_at DESC LIMIT 2",
                (f'%"{tid}"%',),
            ).fetchall()
            ap = conn.execute(
                "SELECT token, status, expires_at, applied_at FROM approvals WHERE target_id=? ORDER BY created_at DESC LIMIT 1",
                (tid,),
            ).fetchone()
            ob = conn.execute(
                "SELECT id, status, send_at, sent_at, edge_synced FROM outbox WHERE ref_id=? ORDER BY created_at DESC LIMIT 1",
                (tid,),
            ).fetchone()

            # 计算直观的一句话“当前卡在哪一步”
            stage_diag = f"挂起状态={w['status']}"
            if w["status"] == "WAITING":
                probe_info = f"，上次探测 {w['last_probed_at'] or '尚未探测'}" if w["trigger_kind"] == "probe" else ""
                stage_diag = f"等待触发条件满足（下次检查/触发：{w['next_fire_at'] or w['trigger_expr']}{probe_info}）"
            elif w["status"] == "STAGED" and ap:
                stage_diag = f"代码改动已就绪，正等待本人确认 `apply {ap['token']}`（口令状态={ap['status']}，有效期至 {ap['expires_at']}）"
            elif effs and effs[0]["status"] in ("PENDING", "RUNNING"):
                e0 = effs[0]
                stage_diag = f"后台动作 `{e0['kind']}` 正在 {e0['status']}（重试={e0['retry_count']}，单元={e0['unit_name'] or 'inline'}）"
            elif effs and effs[0]["status"] == "FAILED":
                stage_diag = f"后台动作失败停手：{effs[0]['last_error']}"
            elif w["result_summary"]:
                stage_diag = f"{w['status']} — {w['result_summary'][:120]}"

            lines = [
                f"[{tid}] 挂起项全链路诊断",
                f"当前卡在/所处步骤：{stage_diag}",
                f"原话：{w['quote']}",
                f"触发器：{w['trigger_kind']}:{w['trigger_expr']} ｜ 模式：{w['action_mode']}（{w['strength']}）",
                f"有效期至：{w['expires_at']}",
            ]
            if effs:
                e0 = effs[0]
                lines.append(
                    f"关联后台动作：[{e0['effect_id']}] {e0['kind']} -> {e0['status']} (retry={e0['retry_count']}, err={e0['last_error'] or '无'})"
                )
            if ob:
                lines.append(
                    f"关联通知队列：[{ob['id']}] status={ob['status']} (send_at={ob['send_at']}, sent_at={ob['sent_at'] or '-'}, 81预排={bool(ob['edge_synced'])})"
                )
            return "\n".join(lines)
        # 默认解释最近一条外发消息
        last_ob = conn.execute("SELECT * FROM outbox WHERE status='SENT' ORDER BY sent_at DESC LIMIT 1").fetchone()
        if last_ob:
            return f"最近一条推送 [{last_ob['id']}] 来自规则 `{last_ob['rule_id']}`，关联对象 `{last_ob['ref_id'] or '无'}`，发送于 {last_ob['sent_at']}。"
        return "暂无可解释的提醒记录。"
    finally:
        conn.close()


def forget_memory(db: SimingDB, keyword: str) -> str:
    kw = keyword.strip()
    if not kw:
        return "请指明要忘掉的内容关键词。"
    ts = iso_now()
    invalidated_hyps = 0
    with db.transaction() as conn:
        cur = conn.execute(
            "UPDATE hypotheses SET status='FORGOTTEN' WHERE status='OBSERVING' AND claim LIKE ?",
            (f"%{kw}%",),
        )
        invalidated_hyps = cur.rowcount
        db.write_audit("A1", "memory_forget", {"keyword": kw, "hypotheses": invalidated_hyps}, conn=conn)

    # 若共享记忆卡片命中该词，在 inbox 生成软失效审阅记录（不直接删 git 历史）
    inbox = db.paths.shared_inbox_dir
    inbox.mkdir(parents=True, exist_ok=True)
    note_file = inbox / f"siming-forget-{now_local().strftime('%Y%m%d-%H%M%S')}.md"
    note_file.write_text(
        f"---\ntype: memory_invalidate\ncreated_at: {ts}\nkeyword: {kw}\ninvalid_at: {ts}\n---\n\n"
        f"用户通过司命发出 `忘掉 {kw}`：已清除 `hypotheses` 中 {invalidated_hyps} 条候选推断；若 `60-共享记忆/` 正式卡片含相关旧条目，可按需标 `invalid_at: {ts[:10]}`。\n",
        encoding="utf-8",
    )
    return f"已软失效关于「{kw}」的 {invalidated_hyps} 条行为推断，并在 `60-共享记忆/inbox/{note_file.name}` 记下失效标记。"


def build_morning_brief(db: SimingDB, full: bool = False) -> str:
    """生成 08:15 司命四合一晨札（默认返回精炼版并持久化全文分卷，full=True 返回详版）。"""
    from siming.morning import build_morning_editions, save_morning_editions_to_meta

    editions = build_morning_editions(db)
    save_morning_editions_to_meta(db, editions, overwrite_delivered=False)
    return str(editions["full"] if full else editions["concise"])


def flush_morning_brief_if_due(db: SimingDB, was_reboot_gap: bool = False) -> Optional[dict[str, Any]]:
    """每日 08:15 发送一次四合一晨札；夜间暂存的 apply Axx 口令仅在真实投递成功起刷新 30m 有效期。"""
    from siming.morning import build_morning_editions, save_morning_editions_to_meta

    now = now_local()
    today_str = now.date().isoformat()
    if db.get_meta("last_morning_brief_date") == today_str:
        return None

    policy = load_policy(db.paths)
    target_hhmm = str(policy["windows"].get("morning_brief", "08:15"))
    cur_hhmm = now.strftime("%H:%M")
    if cur_hhmm < target_hhmm:
        return None

    fail_key = f"morning_brief_fails:{today_str}"
    fail_cnt = int(db.get_meta(fail_key) or "0")
    if fail_cnt >= 5:
        return None

    if was_reboot_gap and cur_hhmm > "10:15":
        db.set_meta("last_morning_brief_date", today_str)
        db.write_audit("A0", "morning_brief_suppressed_reboot", {"time": cur_hhmm})
        return {"ok": True, "suppressed": "reboot_catchup_past_grace"}

    from siming.attention import should_defer_morning_brief_for_sleep

    defer_sleep, sleep_reason = should_defer_morning_brief_for_sleep(db, policy=policy, now_override=now)
    if defer_sleep:
        return {"ok": True, "delivered": False, "deferred_for_sleep": True, "reason": sleep_reason}

    editions = build_morning_editions(db)
    brief = str(editions["concise"])
    mq_ids: list[str] = list(editions.get("mq_ids") or [])

    res = send_qq_message(
        db,
        brief,
        ref_id=f"brief:{today_str}",
        rule_id="morning_brief",
        bypass_quiet=True,
        sub_meta={"kind": "morning_brief", "date": today_str},
    )
    if res.get("delivered"):
        save_morning_editions_to_meta(db, editions, overwrite_delivered=True)
        ts = iso_now(now)
        with db.transaction() as conn:
            if mq_ids:
                placeholders = ",".join("?" for _ in mq_ids)
                mq_texts = [
                    str(r["text"])
                    for r in conn.execute(
                        f"SELECT text FROM outbox WHERE id IN ({placeholders})",
                        tuple(mq_ids),
                    ).fetchall()
                ]
                for m_txt in mq_texts:
                    refresh_approval_ttl_on_delivery(db, m_txt, conn=conn)
                conn.execute(
                    f"UPDATE outbox SET status='SENT', sent_at=? WHERE id IN ({placeholders}) AND status='MORNING_QUEUE'",
                    (ts, *mq_ids),
                )
            db.set_meta("last_morning_brief_date", today_str, conn=conn)
    elif not res.get("ok"):
        db.set_meta(fail_key, str(fail_cnt + 1))
    return res


def close_case(
    db: SimingDB,
    case_id: str,
    *,
    cancel: bool = False,
    note: str = "",
) -> dict[str, Any]:
    """确定性销账或取消一条 case（单真源原子更新 cases / subscriptions(ENDED/CANCELLED) / daily_care / edge_withdraw_outbox 与 rule_stats 反馈）。"""
    tid = (case_id or "").strip().upper()
    if not re.fullmatch(r"S\d{2,6}", tid):
        return {"ok": False, "error": f"非法事项编号：{case_id}"}
    ts = iso_now()
    new_status = "取消" if cancel else "完成"
    category = ""
    with db.transaction() as conn:
        row = conn.execute("SELECT case_id, title, status, category FROM cases WHERE case_id=?", (tid,)).fetchone()
        if not row:
            return {"ok": False, "error": f"未找到事项 {tid}"}
        category = str(row["category"] or "")
        conn.execute(
            "UPDATE cases SET status=?, closed_at=?, state_version=state_version+1, updated_at=? WHERE case_id=?",
            (new_status, ts, ts, tid),
        )
        sub_state = "CANCELLED" if cancel else "ENDED"
        conn.execute(
            "UPDATE subscriptions SET state=?, ended_at=? WHERE target_id=?",
            (sub_state, ts, tid),
        )
        db.enqueue_effect(conn, "edge_withdraw_outbox", {"ref_id": tid})

    sync_note = ""
    if category == "med_injection":
        try:
            from siming.daily_care import sync_injection_case_closed

            s_res = sync_injection_case_closed(db, tid, cancel=cancel)
            if s_res.get("synced") and not s_res.get("duplicate"):
                dl = s_res.get("doses_left")
                sync_note = f"（已同步打针日志，剩 {dl} 针）" if isinstance(dl, int) else "（已同步打针日志）"
        except Exception:
            pass

    if not cancel:
        record_feedback(db, tid, "已办", note=note or "cli_done")
    return {
        "ok": True,
        "case_id": tid,
        "title": str(row["title"]),
        "status": new_status,
        "summary": f"[{tid}] 已标记{new_status}：{row['title']}{sync_note}",
    }


def flush_pickup_window_if_due(
    db: SimingDB,
    now: Optional[datetime] = None,
) -> Optional[dict[str, Any]]:
    """18:00 回家窗口兜底：把当天未提醒的快递取件码按完整柜机地址与站点合并推送。
    若 spatial_pickup 已启用且手机实时定位显示仍在公司加班（<23:00），让位给离司实时提醒避免 18:00 提前打扰与二次重复推。"""
    now = now or now_local()
    policy = load_policy(db.paths)
    pickup_hhmm = str(policy["windows"].get("pickup_batch", "18:00"))
    if now.strftime("%H:%M") < pickup_hhmm or _in_quiet_hours(policy, db=db, now=now):
        return None

    from siming.collectors.pickup_codes import classify_pickup_site
    from siming.modules import is_module_enabled

    if is_module_enabled(db, "spatial_pickup"):
        # 若仍在公司未下班，延后交由 spatial_pickup 离司实时触发
        try:
            from siming.context_loc import resolve_current_location_context

            loc_ctx = resolve_current_location_context(db, pull_edge_if_older_sec=0)
            if loc_ctx.get("has_fresh") and str(loc_ctx.get("place_alias") or "") == "公司" and now.hour < 23:
                return None
        except Exception:
            pass

    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT case_id, title, next_step, context_json, created_at
            FROM cases
            WHERE category = 'pickup' AND status = '待处理' AND last_notified_hash IS NULL
            ORDER BY created_at ASC
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None

    # 若今日已触发过离司 spatial_pickup，则过滤掉触发前已推送过的取件单，避免重复打扰
    today_s = now.date().isoformat()
    if is_module_enabled(db, "spatial_pickup") and db.get_meta("last_spatial_pickup_day") == today_s:
        sp_at = db.get_meta("last_spatial_pickup_at") or ""
        if sp_at:
            rows = [r for r in rows if str(r["created_at"] or "") > sp_at]
            if not rows:
                return None

    home_grouped: dict[str, list[str]] = {}
    other_grouped: dict[str, list[str]] = {}
    home_case_ids: list[str] = []
    other_case_ids: list[str] = []
    case_ids: list[str] = []
    for r in rows:
        ctx = json.loads(r["context_json"] or "{}")
        station = str(ctx.get("station") or "快递点")
        location = str(ctx.get("location") or "").strip()
        brand = str(ctx.get("brand") or "").strip()
        display_st = location or station
        if brand and brand not in display_st and not ctx.get("drop_loc"):
            display_st = f"{brand}·{display_st}"
        site_alias = str(ctx.get("site_alias") or "").strip()
        if not site_alias:
            site_alias = classify_pickup_site(db, display_st, f"{r['title']} {r['next_step']}")["site_alias"]

        code = str(ctx.get("code") or r["title"])
        matched_item = str(ctx.get("matched_order_name") or "")
        free_win = str(ctx.get("free_window") or "").strip()
        extra_parts = [p for p in (matched_item, free_win) if p]
        item_note = f"（{' · '.join(extra_parts)}）" if extra_parts else ""
        if ctx.get("drop_loc"):
            carrier = str(ctx.get("carrier") or "快递")
            tail_s = f" 尾号`{ctx['tail_no']}`" if ctx.get("tail_no") else ""
            entry = f"[{r['case_id']}] {carrier}{tail_s}{item_note}"
        else:
            entry = f"[{r['case_id']}] 取件码 `{code}`{item_note}"
        if site_alias == "新房":
            other_grouped.setdefault(display_st, []).append(entry)
            other_case_ids.append(r["case_id"])
        else:
            home_grouped.setdefault(display_st, []).append(entry)
            home_case_ids.append(r["case_id"])
        case_ids.append(r["case_id"])

    msg_lines: list[str] = []
    if home_grouped:
        home_cnt = sum(len(v) for v in home_grouped.values())
        msg_lines.append(f"📦 回家顺路取件（共 {home_cnt} 件）")
        for st, items in home_grouped.items():
            msg_lines.append(f"• {st}：" + "；".join(items))
    if other_grouped:
        other_cnt = sum(len(v) for v in other_grouped.values())
        msg_lines.append(f"🏠 非顺路 · 新房（示例小区B）待取（共 {other_cnt} 件，不在当前住址）")
        for st, items in other_grouped.items():
            msg_lines.append(f"• {st}：" + "；".join(items))
    if home_grouped and other_grouped:
        msg_lines.append("取完回家件回一句「取了」可批销（不会误销新房件）；单件可点下方按钮或回 `已办 S编号`。")
    else:
        msg_lines.append("取完引用回一句「取了」或点下方按钮即可销账。")
    msg = "\n".join(msg_lines)

    res = send_qq_message(
        db,
        msg,
        ref_id=(home_case_ids[0] if home_case_ids else case_ids[0]),
        rule_id="pickup_1800",
        sub_meta={"batch_case_ids": home_case_ids or case_ids, "all_case_ids": case_ids},
    )
    if res.get("delivered"):
        h = hashlib.sha1(msg.encode("utf-8")).hexdigest()[:16]
        with db.transaction() as conn:
            for cid in case_ids:
                conn.execute(
                    "UPDATE cases SET last_notified_hash=? WHERE case_id=?",
                    (h, cid),
                )
    return res


def _extract_order_location_override(text: str) -> tuple[str, str]:
    """从口语点单句式中提取「送到家 / 点回家 / 送到公司 / 送到新房」为 location_override，避免地名污染商品搜索词。"""
    from siming.qq_nlu import extract_location_override

    return extract_location_override(text)


def _extract_natural_meituan_order(raw: str) -> Optional[str]:
    """安全识别口语点餐/点咖啡句式（如「困了 给我点一杯苹果美式啥的」「帮我点个西瓜送到家」），同时严格拦截问句与非餐饮名词。"""
    from siming.qq_nlu import extract_natural_meituan_order

    return extract_natural_meituan_order(raw)


def _format_todo_list_response(db: SimingDB) -> dict[str, Any]:
    conn_td = db.connect()
    try:
        td_rows = conn_td.execute(
            "SELECT case_id, category, title, next_step, due_at FROM cases WHERE status IN ('待处理', '待核实') ORDER BY CASE WHEN category IN ('food_pickup','pickup','parcel','med_injection') THEN 0 ELSE 1 END, updated_at DESC LIMIT 12"
        ).fetchall()
    finally:
        conn_td.close()
    if not td_rows:
        return {"handled": True, "reply": "✅ 当前没有任何未结待办或待取件。"}
    lines_td = [f"📌 当前未结待办（共 {len(td_rows)} 条 · 点下方按钮或发 `已办 Sxx` 即可销账）："]
    quick_ids: list[str] = []
    for r in td_rows:
        due_s = f" (截止 {str(r['due_at'])[:10]})" if r["due_at"] else ""
        lines_td.append(f"• [{r['case_id']}] {r['title']}{due_s} — {str(r['next_step'] or '')[:50]}")
        if r["category"] in ("food_pickup", "pickup", "parcel", "med_injection", "subscription_renew"):
            quick_ids.append(str(r["case_id"]))
    from siming.qq_cards import make_button, make_keyboard

    kb_td = None
    if quick_ids:
        btns = [
            make_button(f"done_{i}", f"✅ 已办 {cid}", f"siming:case_act:{cid}:已办", visited_label=f"已销 {cid}", style=1, group_id=f"sm_td_{cid}")
            for i, cid in enumerate(quick_ids[:4])
        ]
        kb_td = make_keyboard([btns[:2], btns[2:4]] if len(btns) > 2 else [btns])
    return {"handled": True, "reply": "\n".join(lines_td), "keyboard": kb_td}


def _dispatch_preset_view(
    db: SimingDB,
    intent: str,
    raw_query: str = "",
    *,
    args: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """统一执行声明式预置只读视图与安全查询意图（Layer 2 & Layer 3 共享单真源分发器）。"""
    arg_map = args or {}

    if intent == "morning_full":
        from siming.morning import get_morning_full_reply

        return {"handled": True, "reply": get_morning_full_reply(db, section_hint=str(arg_map.get("section") or ""))}
    if intent == "morning_today":
        db.record_human_use("morning_brief", "query_today")
        return {"handled": True, "reply": build_morning_brief(db)}
    if intent == "todo_list":
        return _format_todo_list_response(db)
    if intent == "care_status":
        from siming.daily_care import format_care_report

        db.record_human_use("daily_care", "query_status")
        return {"handled": True, "reply": format_care_report(db)}
    if intent == "install_checklist":
        from siming.install_plan import format_installation_checklist

        db.record_human_use("install_plan", "shortcut", ref_id="12.10")
        return {"handled": True, "reply": format_installation_checklist(db)}
    if intent == "arrival_check":
        from siming.install_plan import format_arrival_inspection_checklist

        db.record_human_use("install_plan", "shortcut_arrival", ref_id="arrival")
        return {"handled": True, "reply": format_arrival_inspection_checklist(db)}
    if intent == "onsite_plan":
        from siming.install_plan import format_onsite_grouped_checklist

        db.record_human_use("install_plan", "shortcut_onsite", ref_id="onsite")
        return {"handled": True, "reply": format_onsite_grouped_checklist(db)}
    if intent == "install_conflicts":
        from siming.movein_pm import check_install_conflicts

        return {"handled": True, "reply": check_install_conflicts(db)["reply"]}
    if intent == "modules_list":
        from siming.modules import format_modules_report

        return {"handled": True, "reply": format_modules_report(db)}
    if intent == "price_watch_list":
        from siming.price_watch import format_price_watch_report

        db.record_human_use("price_protect_watch", "shortcut_list", ref_id="list")
        return {"handled": True, "reply": format_price_watch_report(db)}
    if intent == "reimburse_domestic":
        from siming.ledger.reimburse import summarize_reimbursement_bundle

        rb = summarize_reimbursement_bundle(db)
        r_lines = [
            f"🧾 {rb['year_month']} 国内电子发票与滴滴行程单配对盘点（共 {rb['count']} 张发票 ¥{rb['total_yuan']:.2f} · 行程单 {rb['itinerary_count']} 份）",
            f"• 🚕 打车已配对就绪（可走 CW01）：{len(rb['paired_rides'])} 组",
        ]
        for pr in rb["paired_rides"][:5]:
            rt = f"（{', '.join(pr['routes'][:2])}）" if pr["routes"] else ""
            r_lines.append(f"  - `{pr['invoice_file']}` ↔ `{pr['itinerary_file']}` ¥{pr['amount_yuan']:.2f}{rt}")
        if rb["missing_itinerary_invoices"]:
            r_lines.append(
                "• ⚠️ 缺行程报销单的打车发票："
                + "、".join(f"{x['file']}(¥{x['amount_yuan']:.2f})" for x in rb["missing_itinerary_invoices"][:5])
            )
        if rb["missing_invoice_itineraries"]:
            r_lines.append(
                "• ⚠️ 缺电子发票的行程单："
                + "、".join(f"{x['file']}(¥{x['amount_yuan']:.2f})" for x in rb["missing_invoice_itineraries"][:5])
            )
        if rb["dining_invoices"]:
            r_lines.append(
                f"• 🍜 餐饮外卖（美团等）发票：{len(rb['dining_invoices'])} 张（合计 ¥{sum(x['amount_yuan'] for x in rb['dining_invoices']):.2f}）"
            )
        return {"handled": True, "reply": "\n".join(r_lines)}
    if intent == "reimburse_cursor":
        from siming.ledger.reimburse_audit import format_reimbursement_cursor

        db.record_human_use("reimburse_audit", "shortcut", ref_id="cursor")
        return {"handled": True, "reply": format_reimbursement_cursor(db)}
    if intent == "receivables_list":
        from siming.quiet_cfo import format_receivables_report

        return {"handled": True, "reply": format_receivables_report(db)}
    if intent == "waits_list":
        ws = list_waits(db, status="WAITING")
        if not ws:
            return {"handled": True, "reply": "当前没有待触发的挂起项。"}
        lines = [f"当前挂起项（共 {len(ws)} 条）："]
        for w in ws[:12]:
            lines.append(
                f"• [{w['wait_id']}] {w['trigger_kind']}:{w['trigger_expr']} ({w['action_mode']}) — {w['quote'][:45]}"
            )
        return {"handled": True, "reply": "\n".join(lines)}
    if intent == "didi_status":
        from siming.didi_mcp import query_or_cancel_didi_order

        return {"handled": True, "reply": query_or_cancel_didi_order(db, cancel=False)["reply"]}
    if intent == "luckin_status":
        from siming.luckin_mcp import query_luckin_order_status

        return {"handled": True, "reply": query_luckin_order_status(db)["reply"]}
    if intent == "location_status":
        from siming.context_loc import format_location_status

        return {"handled": True, "reply": format_location_status(db)}
    if intent == "attention_status":
        from siming.attention import format_attention_status

        return {"handled": True, "reply": format_attention_status(db)}
    if intent == "dev_links_check":
        from siming.self_heal import diagnose_and_heal_dev_links

        diag = diagnose_and_heal_dev_links(db, allow_heal=True, probe_nous_portal=("nous" in raw_query.lower()))
        return {"handled": True, "reply": diag["reply"]}
    if intent == "routines_panel":
        from siming.event_chain import format_routines_panel

        return {"handled": True, "reply": format_routines_panel(db)}
    if intent == "tesla_status":
        from siming.tesla_link import format_tesla_status, poll_tesla_cn_fleet_if_configured

        poll_tesla_cn_fleet_if_configured(db, force=True, allow_wake=False)
        return {"handled": True, "reply": format_tesla_status(db)}
    if intent == "novel_spend":
        from siming.ledger.enrich_priors import summarize_novel_and_micro_spend

        return {"handled": True, "reply": summarize_novel_and_micro_spend(db)["summary"]}
    if intent == "platform_sessions":
        from siming.night_lab import inspect_platform_sessions

        return {"handled": True, "reply": inspect_platform_sessions(db)["reply"]}
    if intent == "haos_status":
        from siming.night_lab import query_or_trigger_home_assistant

        return {"handled": True, "reply": query_or_trigger_home_assistant(db)["reply"]}
    if intent == "compute_assets":
        from siming.compute_assets import format_compute_dashboard

        return {"handled": True, "reply": format_compute_dashboard(db)}
    if intent == "sub_audit":
        from siming.sub_hunter import format_subscription_audit, scan_subscriptions

        scan_subscriptions(db)
        return {"handled": True, "reply": format_subscription_audit(db)}
    if intent == "health_cockpit":
        from siming.health_cockpit import generate_doctor_brief

        return {"handled": True, "reply": generate_doctor_brief(db)}
    if intent == "vehicle_cfo":
        from siming.vehicle_cfo import format_vehicle_report

        return {"handled": True, "reply": format_vehicle_report(db)}
    if intent == "home_air":
        from siming.home_air_guard import format_home_air_report

        return {"handled": True, "reply": format_home_air_report(db)}
    if intent == "homelab_ops":
        from siming.homelab_ops import format_homelab_report

        return {"handled": True, "reply": format_homelab_report(db)}
    if intent == "bambu_status":
        from siming.bambu_copilot import format_bambu_report

        return {"handled": True, "reply": format_bambu_report(db)}
    if intent == "home_passport":
        from siming.home_passport import format_home_passport_report

        return {"handled": True, "reply": format_home_passport_report(db)}
    if intent == "msim_diary":
        from siming.msim_memory import get_msim_leaderboard

        lb = get_msim_leaderboard(db)
        return {
            "handled": True,
            "reply": f"🧠 M-SIM 跨模型进化日记：共 {lb.get('total_entries', 0)} 条经验，覆盖 {len(lb.get('by_family') or {})} 个问题族。",
        }
    if intent == "trip_summary":
        from siming.trip_packager import format_trip_summary

        return {"handled": True, "reply": format_trip_summary(db)}
    if intent == "social_summary":
        from siming.social_boardgames import format_social_summary

        return {"handled": True, "reply": format_social_summary(db)}
    if intent == "replenish_list":
        from siming.replenish_cart import format_replenish_report

        return {"handled": True, "reply": format_replenish_report(db)}
    if intent == "speculate_plans":
        from siming.meituan_speculate import format_speculative_plans_overview, load_active_speculative_batch

        strong_plan = any(k in raw_query for k in ("看看方案", "推演", "外卖方案", "点餐方案", "菜品方案"))
        if strong_plan or load_active_speculative_batch(db):
            return {"handled": True, "reply": format_speculative_plans_overview(db)}
        return None
    if intent == "ledger_ask":
        from siming.quiet_cfo import query_ledger_natural

        return {"handled": True, "reply": query_ledger_natural(db, str(arg_map.get("query") or raw_query))["reply"]}
    if intent == "sure_ask":
        from siming.sure_bridge import ask_sure_natural

        return {"handled": True, "reply": str(ask_sure_natural(db, str(arg_map.get("question") or raw_query)).get("reply") or "")}
    if intent == "meituan_history_list":
        from siming.meituan_agent import list_meituan_history_orders
        from siming.modules import is_module_enabled

        if is_module_enabled(db, "meituan_agent"):
            loc_ov = str(arg_map.get("location_override") or "")
            return {"handled": True, "reply": list_meituan_history_orders(db, location_override=loc_ov)["reply"]}
        return None
    if intent == "meituan_recommend":
        from siming.meituan_agent import _parse_meal_hint, recommend_meituan_meals
        from siming.spatial_life import get_prestaged_dinner_fast_reply

        hint_s = str(arg_map.get("hint") or raw_query)
        loc_ov = str(arg_map.get("location_override") or "")
        parsed_mt = _parse_meal_hint(hint_s, now_local())
        if parsed_mt["slot"] == "晚餐" and not parsed_mt["keyword"] and parsed_mt["max_budget"] is None and not loc_ov:
            fast_hit = get_prestaged_dinner_fast_reply(db)
            if fast_hit:
                return {"handled": True, "reply": fast_hit}
        return {"handled": True, "reply": recommend_meituan_meals(db, hint=hint_s, location_override=loc_ov)["reply"]}
    return None


def handle_inbound_qq(
    db: SimingDB,
    text: str,
    reply_to_msg_id: Optional[str] = None,
    reply_to_text: Optional[str] = None,
    media_paths: Optional[list[str]] = None,
    llm_classifier: Optional[Any] = None,
    jev_decision: Optional[dict[str, Any]] = None,
    jev_evaluator: Optional[Any] = None,
) -> dict[str, Any]:
    """处理来自手机 QQ 或网关插件的消息：先经 Tier-0 纯代码安全直通 + JEV 前置过滤，再命中司命确定性/泛化 NLU 路由或引用回复返回应答（附带内嵌键盘确认框）；否则交还常规对话。"""
    res = _handle_inbound_qq_core(
        db,
        text=text,
        reply_to_msg_id=reply_to_msg_id,
        reply_to_text=reply_to_text,
        media_paths=media_paths,
        llm_classifier=llm_classifier,
        jev_decision=jev_decision,
        jev_evaluator=jev_evaluator,
    )
    if isinstance(res, dict) and res.get("handled") and res.get("reply") and not res.get("keyboard"):
        try:
            from siming.qq_cards import infer_keyboard_for_message

            kb = infer_keyboard_for_message(db, str(res["reply"]))
            if kb is not None:
                res["keyboard"] = kb
        except Exception:
            pass
    return res


def _handle_inbound_qq_core(
    db: SimingDB,
    text: str,
    reply_to_msg_id: Optional[str] = None,
    reply_to_text: Optional[str] = None,
    media_paths: Optional[list[str]] = None,
    llm_classifier: Optional[Any] = None,
    jev_decision: Optional[dict[str, Any]] = None,
    jev_evaluator: Optional[Any] = None,
) -> dict[str, Any]:
    from siming.qq_nlu import (
        classify_qq_intent_with_llm,
        match_preset_view,
        normalize_qq_input,
        parse_case_or_wait_action,
        parse_defer_remind,
        parse_didi_command,
        parse_knowledge_ask,
        parse_ledger_natural_query,
        parse_meal_ack,
        parse_meituan_combo,
        parse_meituan_direct_order,
        parse_meituan_history_list,
        parse_meituan_history_reorder,
        parse_meituan_recommend,
        parse_meituan_spec_action,
        parse_misc_commands,
        parse_module_toggle,
        parse_movein_action,
        parse_price_watch_mutation,
        parse_replenish_action,
        parse_sleep_wake,
        parse_sure_query,
        parse_tesla_mutation,
    )
    from siming.remote import ask_knowledge, dispatch_repo_task, drop_item

    db.init_db()
    raw, reply_to_text = split_qq_quoted_message(text, reply_to_text)
    u = normalize_qq_input(raw)
    raw = u.raw
    cmd = u.stmt
    cmd_q = u.query

    # 0. Night-Owl 显式入睡/醒来口令与本人活跃时间戳刷新（泛化支持「先去睡了」「刚起床」「早上好」，仍严拒问句）
    sleep_act = parse_sleep_wake(u)
    if sleep_act == "sleep":
        from siming.attention import record_explicit_sleep_state

        sl_res = record_explicit_sleep_state(db, sleeping=True, source="qq")
        return {"handled": True, "reply": sl_res["summary"]}

    if sleep_act == "awake":
        from siming.attention import record_explicit_sleep_state

        aw_res = record_explicit_sleep_state(db, sleeping=False, source="qq")
        today_s = now_local().date().isoformat()
        if db.get_meta("last_morning_brief_date") != today_s and now_local().hour >= 5:
            from siming.morning import build_morning_editions, save_morning_editions_to_meta

            editions = build_morning_editions(db)
            save_morning_editions_to_meta(db, editions, overwrite_delivered=True)
            mq_ids = list(editions.get("mq_ids") or [])
            ts_now = iso_now()
            with db.transaction() as conn:
                if mq_ids:
                    ph = ",".join("?" for _ in mq_ids)
                    for r_mq in conn.execute(f"SELECT text FROM outbox WHERE id IN ({ph})", tuple(mq_ids)).fetchall():
                        refresh_approval_ttl_on_delivery(db, str(r_mq["text"]), conn=conn)
                    conn.execute(
                        f"UPDATE outbox SET status='SENT', sent_at=? WHERE id IN ({ph}) AND status='MORNING_QUEUE'",
                        (ts_now, *mq_ids),
                    )
                db.set_meta("last_morning_brief_date", today_s, conn=conn)
            return {"handled": True, "reply": f"{aw_res['summary']}\n\n{editions['concise']}"}
        return {"handled": True, "reply": aw_res["summary"]}

    if raw:
        from siming.attention import touch_human_activity

        touch_human_activity(db, source="qq_inbound")

    # 1. 纯代码 apply 口令、付款许可确认与 D-19 自然确认（最高优先级，绝不经任何模型）
    m_apply = re.fullmatch(r"/?apply\s+(A\d{2,6})", cmd, re.IGNORECASE)
    if m_apply:
        tok = m_apply.group(1).upper()
        res = verify_and_apply_approval(db, tok)
        if res["ok"]:
            return {"handled": True, "reply": f"✅ [{tok}] {res['summary']}"}
        return {"handled": True, "reply": f"❌ {res['error']}"}

    m_pay_tok = re.fullmatch(r"(?:确认支付|确认付款|确认下单|支付|付款)\s+(A\d{2,6})", cmd, re.IGNORECASE)
    if m_pay_tok:
        tok = m_pay_tok.group(1).upper()
        conn_chk = db.connect()
        try:
            row_chk = conn_chk.execute("SELECT action FROM approvals WHERE token = ?", (tok,)).fetchone()
        finally:
            conn_chk.close()
        if row_chk and str(row_chk["action"] or "") not in ("meituan_submit_pay", "luckin_confirm_order", "meituan_lock_cart"):
            return {"handled": True, "reply": f"⚠️ 口令 `{tok}` 对应动作 `{row_chk['action']}` 不是外卖/咖啡支付卡，如需执行请发送 `apply {tok}`。"}
        res = verify_and_apply_approval(db, tok)
        if res["ok"]:
            return {"handled": True, "reply": f"✅ [{tok}] {res['summary']}"}
        return {"handled": True, "reply": f"❌ {res['error']}"}

    m_cancel_mt_tok = re.fullmatch(r"(?:取消|放弃|作废)\s+(A\d{2,6})", cmd, re.IGNORECASE)
    if m_cancel_mt_tok:
        from siming.approval import cancel_approval

        c_res = cancel_approval(db, token=m_cancel_mt_tok.group(1).upper())
        return {"handled": True, "reply": c_res["reply"]}

    trusted_quoted_ref = _extract_trusted_quoted_approval_ref(db, reply_to_msg_id, reply_to_text)

    if (
        trusted_quoted_ref
        and not u.had_question
        and re.fullmatch(r"(?:取消|不点了|不要了|放弃|算了|cancel)(?:吧|呀|啊|哈|咯|啦)?", u.core, re.IGNORECASE)
    ):
        if re.fullmatch(r"A\d{2,6}", trusted_quoted_ref):
            from siming.approval import cancel_approval

            c_res = cancel_approval(db, token=trusted_quoted_ref)
            if c_res.get("ok"):
                return {"handled": True, "reply": c_res["reply"]}

    if not u.had_question and u.core in ("取消订单", "放弃下单", "不点了", "取消点单", "取消支付", "取消付款"):
        from siming.meituan_pay import cancel_meituan_pending_order

        c_res = cancel_meituan_pending_order(db)
        if c_res.get("ok") or u.core in ("取消支付", "取消付款", "放弃下单", "取消点单"):
            return {"handled": True, "reply": c_res["reply"]}

    from siming.approval import NATURAL_CONFIRM_RE, PAYMENT_CONFIRM_RE, resolve_and_apply_natural_confirm

    quoted_ref = trusted_quoted_ref
    # 若用户显式引用了一条非司命审批卡（含疑似提示注入/转发 snippet/普通消息），则绝不把随口「好/好的/确认」误转成未引用自然确认去执行库里别的 PENDING 审批！
    untrusted_quote_mention = bool(
        (reply_to_text or "")
        and not quoted_ref
        and (
            _UNTRUSTED_QUOTE_INJECTION_RE.search(reply_to_text or "")
            or re.search(r"\b(?:apply\s+)?A\d{2,6}\b", reply_to_text or "", re.IGNORECASE)
        )
    )
    if not untrusted_quote_mention and not u.had_question:
        confirm_arg: Optional[str] = None
        if PAYMENT_CONFIRM_RE.match(cmd) or NATURAL_CONFIRM_RE.match(cmd):
            confirm_arg = cmd
        elif PAYMENT_CONFIRM_RE.match(u.core):
            confirm_arg = u.core
        elif quoted_ref and u.core.lower() in ("确认", "支付", "付款", "同意", "apply"):
            confirm_arg = u.core
        if confirm_arg is not None:
            nat_res = resolve_and_apply_natural_confirm(db, confirm_arg, quoted_ref=quoted_ref)
            if nat_res.get("matched"):
                return {"handled": True, "reply": nat_res["reply"]}

    # 1.15 引用日用品补货提醒卡 `[RPL:item_id]`（仅校验 subscriptions 或 outbox 真实发出记录，防伪造引用）
    quoted_rpl_id = ""
    if reply_to_msg_id:
        conn_r = db.connect()
        try:
            sub_r = conn_r.execute(
                "SELECT target_id FROM subscriptions WHERE sub_key=? AND target_kind='replenish' AND state='ACTIVE'",
                (f"qqmsg:{reply_to_msg_id}",),
            ).fetchone()
            if sub_r:
                quoted_rpl_id = str(sub_r["target_id"] or "")
        finally:
            conn_r.close()
    if not quoted_rpl_id and reply_to_text:
        m_qr = re.search(r"\[`?RPL:([A-Za-z0-9_-]{3,40})`?\]", reply_to_text)
        if m_qr:
            cand_iid = m_qr.group(1)
            conn_r = db.connect()
            try:
                ob_hit = conn_r.execute(
                    "SELECT 1 FROM outbox WHERE rule_id='replenish_cart' AND ref_id LIKE ? AND status='SENT' ORDER BY created_at DESC LIMIT 1",
                    (f"rpl:{cand_iid}:%",),
                ).fetchone()
                if ob_hit:
                    quoted_rpl_id = cand_iid
            finally:
                conn_r.close()
    if quoted_rpl_id and not u.had_question:
        if re.fullmatch(r"(?:确认|确认买|直接买|买|买吧|补货|下单|好的|好|行|可以)(?:吧|呀|啊|哈|咯|啦)?", u.core, re.IGNORECASE):
            from siming.replenish_cart import stage_replenish_buy

            rb = stage_replenish_buy(db, quoted_rpl_id)
            return {"handled": True, "reply": str(rb.get("reply") or rb.get("error") or "")}
        if re.fullmatch(r"(?:已买|买过了|我自己买过了|刚买|买了)(?:啦|咯|哈|啊)?", u.core):
            from siming.replenish_cart import mark_replenish_bought

            rm = mark_replenish_bought(db, quoted_rpl_id)
            return {"handled": True, "reply": str(rm.get("summary") or rm.get("error") or "")}
        if re.fullmatch(r"(?:还有|还有余量|家里还有|不急|延后|晚点再说)(?:啦|咯|哈|吧)?", u.core):
            from siming.replenish_cart import snooze_replenish_item

            rs = snooze_replenish_item(db, quoted_rpl_id, days=14)
            return {"handled": True, "reply": str(rs.get("summary") or rs.get("error") or "")}
        if re.fullmatch(r"(?:暂停|暂不提醒|别提醒了|不用提醒|取消)(?:啦|咯|哈|吧)?", u.core):
            from siming.replenish_cart import toggle_replenish_item

            rt = toggle_replenish_item(db, quoted_rpl_id, enabled=False)
            return {"handled": True, "reply": str(rt.get("summary") or rt.get("error") or "")}

    # 1.2 日常照护：打针销账（打了 / 跳过）、周期用药剩余针数、开药登记、口罩/针头/棉片余量校准与面板
    try:
        from siming.daily_care import handle_care_inbound

        care_hit = handle_care_inbound(db, raw, reply_to_text=reply_to_text)
    except Exception:
        care_hit = None
    if care_hit:
        return care_hit

    # 1.25 饮食确认短语（「吃过了」「已经吃过晚饭了」「今晚不点了」等明确独立答复）：静默本餐窗 meal_guard 与 overtime_meal，不劫持「晚饭吃完了」或长闲聊句
    meal_ack = parse_meal_ack(u)
    if meal_ack is not None:
        from siming.meal_guard import record_meal_ack

        ack_res = record_meal_ack(db, window=meal_ack["window"], note=cmd)
        return {"handled": True, "reply": str(ack_res.get("reply") or "")}

    # 1.5 微信 / 支付宝 / 抖音导出账单文件直传（仅当显式带「密码/解压码」前缀时提取口令，待解密状态限时 10 分钟且单次尝试即清）
    if media_paths:
        first_media = Path(media_paths[0])
        ext_low = first_media.suffix.lower()
        name_low = first_media.name.lower()
        is_bill_doc = ext_low in (".zip", ".csv", ".xlsx") or (
            ext_low == ".pdf"
            and any(k in name_low or k in raw for k in ("账单", "流水", "明细", "alipay", "支付宝", "微信", "抖音"))
        )
        if is_bill_doc and first_media.is_file():
            from siming.ledger.export_zip import import_bill_archive

            m_pwd = re.search(r"(?:密码|解压码)\s*[：:]?\s*([A-Za-z0-9]{4,16})\s*$", raw)
            pwd = m_pwd.group(1) if m_pwd else ""
            imp = import_bill_archive(db, first_media, password=pwd or None)
            if imp.get("ok"):
                db.set_meta("pending_bill_archive", "")
                db.set_meta("pending_bill_archive_ts", "")
                return {
                    "handled": True,
                    "reply": (
                        f"✅ 账单文件 `{first_media.name}` 已入库（临时解压文件已清理）\n"
                        f"• 新增入账：{imp['ingested']} 笔\n"
                        f"• 自动配对银行短信/月结单：{imp['matched']} 笔\n"
                        f"• 同额歧义待核实：{imp['ambiguous']} 笔"
                    ),
                }
            if imp.get("needs_password"):
                db.set_meta("pending_bill_archive", str(first_media))
                db.set_meta("pending_bill_archive_ts", iso_now())
                db.set_meta("pending_bill_archive_tries", "0")
                return {
                    "handled": True,
                    "reply": f"📂 已收到加密账单 `{first_media.name}`，请在 30 分钟内发 `解压码 XXXXXX`（必须带「解压码」前缀防误吞普通消息），解密后立即销毁临时文件。",
                }
            return {"handled": True, "reply": f"❌ 账单导入失败：{imp.get('error')}"}

    pending_arc = db.get_meta("pending_bill_archive") or ""
    if pending_arc:
        pending_ts_s = db.get_meta("pending_bill_archive_ts") or ""
        expired_arc = True
        if pending_ts_s:
            try:
                expired_arc = (now_local() - parse_iso(pending_ts_s)).total_seconds() > 1800
            except Exception:
                expired_arc = True
        if expired_arc:
            Path(pending_arc).unlink(missing_ok=True) if str(pending_arc).startswith(str(db.paths.tmp_root)) else None
            db.set_meta("pending_bill_archive", "")
            db.set_meta("pending_bill_archive_ts", "")
            db.set_meta("pending_bill_archive_tries", "0")
        else:
            m_arc_pwd = re.fullmatch(r"(?:密码|解压码)\s*[：:]?\s*([A-Za-z0-9]{4,16})", raw)
            if m_arc_pwd:
                from siming.ledger.export_zip import import_bill_archive

                pwd_only = m_arc_pwd.group(1).strip()
                arc_path = Path(pending_arc)
                if arc_path.is_file():
                    imp = import_bill_archive(db, arc_path, password=pwd_only, delete_source_archive=True)
                    if imp.get("ok"):
                        db.set_meta("pending_bill_archive", "")
                        db.set_meta("pending_bill_archive_ts", "")
                        db.set_meta("pending_bill_archive_tries", "0")
                        return {
                            "handled": True,
                            "reply": (
                                f"✅ 解密成功！`{arc_path.name}` 已入库（临时解压文件已清理）\n"
                                f"• 新增入账：{imp['ingested']} 笔 · 自动配对：{imp['matched']} 笔 · 待核实：{imp['ambiguous']} 笔"
                            ),
                        }
                    tries = int(db.get_meta("pending_bill_archive_tries") or "0") + 1
                    if tries >= 2 or imp.get("zip_bomb"):
                        if str(arc_path).startswith(str(db.paths.tmp_root)):
                            arc_path.unlink(missing_ok=True)
                        db.set_meta("pending_bill_archive", "")
                        db.set_meta("pending_bill_archive_ts", "")
                        db.set_meta("pending_bill_archive_tries", "0")
                        return {"handled": True, "reply": f"❌ {imp.get('error') or '解压密码连续错误'}（暂存包已安全清理）。"}
                    db.set_meta("pending_bill_archive_tries", str(tries))
                    return {"handled": True, "reply": "❌ 解压密码不正确（仍保留 1 次重试机会，请发 `解压码 正确密码`）。"}
                db.set_meta("pending_bill_archive", "")
                db.set_meta("pending_bill_archive_ts", "")

    # 2. 媒体图片随手投递与新房按房间留证归档（`存 主卧 水电` / `存 铭牌 林内` 直进 home_assets！）
    if media_paths:
        m_home_img = re.match(r"^(?:存新房|存装修|存)\s+([^\s]+)(?:\s+([^\s]+))?(?:\s+(.+))?$", raw)
        if m_home_img and m_home_img.group(1) not in ("司命",):
            from siming.movein_pm import archive_home_photo

            p_res = archive_home_photo(
                db,
                src_path=Path(media_paths[0]),
                room=m_home_img.group(1),
                tag=m_home_img.group(2) or "现场留证",
                note=m_home_img.group(3) or "",
            )
            return {"handled": True, "reply": p_res.get("summary") or f"❌ {p_res.get('error')}"}

        if re.search(r"^(?:投递|存司命|记一下|发给司命)\b", raw):
            note = re.sub(r"^(?:投递|存司命|记一下|发给司命)\s*", "", raw)
            res = drop_item(
                db,
                file_path=Path(media_paths[0]),
                note=note,
                source_channel="qq_drop",
            )
            return {"handled": True, "reply": res.get("summary") or res.get("error", "投递失败")}

    # 3. 命中已有消息订阅（QQ 引用回复直达，支持晨札全文展开、批量取件与完结 14 天内自动续接）
    if reply_to_msg_id or reply_to_text:
        sub = resolve_subscription_for_reply(db, reply_to_msg_id, reply_to_text)
        if sub is not None:
            sub_res = _handle_subscribed_reply(
                db, sub["target_kind"], sub["target_id"], raw, sub_meta=sub.get("meta")
            )
            if sub_res.get("handled"):
                return sub_res

    # 4. 显式凭据录入与仓级派单（高敏感/强格式前缀，保持严格匹配）
    m_disp = re.match(r"^派单\s+([A-Za-z0-9_.-]+)\s*[：:]\s*(.+)$", raw, re.DOTALL)
    if m_disp:
        repo_name, task_desc = m_disp.group(1).strip(), m_disp.group(2).strip()
        d_res = dispatch_repo_task(db, repo=repo_name, task=task_desc, run_immediately=True)
        if not d_res["ok"]:
            return {"handled": True, "reply": f"❌ {d_res['error']}"}
        return {
            "handled": True,
            "reply": (
                f"🛠️ [{d_res['wait_id']}] 已派单至 `{d_res['repo_dir']}` 独立 git worktree\n"
                f"验收门禁：`{d_res['done_check']}`\n"
                "后台跑完测试后会把 diff 与 apply 口令发回这里。"
            ),
        }

    m_didi_key = re.match(r"^(?:滴滴密钥|滴滴key)\s*[：:]?\s*(\S+)$", cmd, re.IGNORECASE)
    if m_didi_key:
        from siming.didi_mcp import save_didi_mcp_key

        sk = save_didi_mcp_key(db, m_didi_key.group(1))
        return {"handled": True, "reply": sk.get("summary") or f"❌ {sk.get('error')}"}

    m_lk_key = re.match(r"^(?:瑞幸密钥|瑞幸token|luckin[_\-\s]*key)\s*[：:]?\s*(\S+)$", cmd, re.IGNORECASE)
    if m_lk_key:
        from siming.luckin_mcp import save_luckin_mcp_token

        lk_sk = save_luckin_mcp_token(db, m_lk_key.group(1))
        return {"handled": True, "reply": lk_sk.get("summary") or f"❌ {lk_sk.get('error')}"}

    m_bind_phone = re.match(r"^(?:绑定手机号|授权手机号)\s*[：:]?\s*(\+?86[-\s]?)?(1[3-9]\d{9})$", cmd)
    if m_bind_phone:
        from siming.otp_vault import save_authorized_phone

        sp = save_authorized_phone(db, m_bind_phone.group(2))
        return {"handled": True, "reply": sp.get("summary") or f"❌ {sp.get('error')}"}

    # 4.5 JEV (`typesafe/jev-1.13`) 前置路由过滤：
    # 在进入 Layer 2 泛化 NLU 前，先判定该消息是否应由司命接手（Tier-0 审批/状态打卡/引用/密钥已在上方纯代码直通）。
    # 若上游 siming-bridge 已前置跑过 JEV，直接复用传入的 jev_decision，避免重复调用；
    # 仅在高置信 hermes_chat (>= chat_reject_min_prob) 时直接放行给通用对话；异常/离线/中置信一律保守回退本地 NLU。
    effective_jev: Optional[dict[str, Any]] = jev_decision if isinstance(jev_decision, dict) else None
    if effective_jev is None and raw:
        try:
            from siming.jev_gate import evaluate_siming_takeover

            pol_jev = load_policy(db.paths)
            if callable(jev_evaluator):
                effective_jev = jev_evaluator(
                    raw,
                    reply_to_id=reply_to_msg_id or "",
                    reply_to_text=reply_to_text or "",
                    media=media_paths or [],
                    policy=pol_jev,
                )
            else:
                effective_jev = evaluate_siming_takeover(
                    raw,
                    reply_to_id=reply_to_msg_id or "",
                    reply_to_text=reply_to_text or "",
                    media=media_paths or [],
                    policy=pol_jev,
                )
        except Exception:
            effective_jev = None

    if isinstance(effective_jev, dict) and effective_jev.get("should_siming_handle") is False:
        return {"handled": False, "jev": effective_jev}

    # 5. 高频参数化意图与槽位解析（Layer 2B：支持自然词序、礼貌包装、中文数字与无空格变体）
    mod_act = parse_module_toggle(u)
    if mod_act is not None:
        from siming.modules import resolve_module_spec, set_module_enabled

        if resolve_module_spec(mod_act["target"]) is not None or mod_act.get("explicit_module_kw"):
            m_res = set_module_enabled(db, mod_act["target"], enabled=bool(mod_act["enabled"]))
            return {"handled": True, "reply": m_res.get("summary") or f"❌ {m_res.get('error')}"}

    pw_act = parse_price_watch_mutation(u)
    if pw_act is not None:
        if pw_act["action"] == "add":
            from siming.price_watch import add_price_watch

            pw_res = add_price_watch(
                db,
                sku_or_url=pw_act["sku_or_url"],
                name=pw_act["name"],
                paid_yuan=pw_act["paid"],
            )
            return {"handled": True, "reply": pw_res.get("summary") or f"❌ {pw_res.get('error')}"}
        if pw_act["action"] == "remove":
            from siming.price_watch import remove_price_watch

            rm_res = remove_price_watch(db, pw_act["target"])
            return {"handled": True, "reply": rm_res.get("summary") or f"❌ {rm_res.get('error')}"}

    didi_act = parse_didi_command(u)
    if didi_act is not None:
        if didi_act["action"] == "cancel":
            from siming.didi_mcp import query_or_cancel_didi_order

            c_res = query_or_cancel_didi_order(db, cancel=True)
            return {"handled": True, "reply": c_res["reply"]}
        if didi_act["action"] == "ride":
            from siming.didi_mcp import request_didi_ride
            from siming.modules import is_module_enabled

            if is_module_enabled(db, "didi_shortcut"):
                r_ride = request_didi_ride(db, dest_raw=didi_act["dest"], origin_raw=didi_act["origin"])
                return {"handled": True, "reply": r_ride["reply"]}

    mt_hist_list = parse_meituan_history_list(u)
    if mt_hist_list is not None:
        from siming.meituan_agent import list_meituan_history_orders
        from siming.modules import is_module_enabled

        if is_module_enabled(db, "meituan_agent"):
            return {
                "handled": True,
                "reply": list_meituan_history_orders(db, location_override=mt_hist_list["location_override"])["reply"],
            }

    mt_reorder = parse_meituan_history_reorder(u, db)
    if mt_reorder is not None:
        from siming.meituan_agent import stage_meituan_history_reorder
        from siming.modules import is_module_enabled

        if is_module_enabled(db, "meituan_agent"):
            reorder_kw: dict[str, Any] = {"location_override": mt_reorder["location_override"]}
            if mt_reorder.get("extra_dishes"):
                reorder_kw["extra_dishes"] = list(mt_reorder["extra_dishes"])
            if mt_reorder.get("exclude_dishes"):
                reorder_kw["exclude_dishes"] = list(mt_reorder["exclude_dishes"])
            return {
                "handled": True,
                "reply": stage_meituan_history_reorder(
                    db,
                    mt_reorder["target"],
                    **reorder_kw,
                )["reply"],
            }

    mt_combo = parse_meituan_combo(u)
    if mt_combo is not None:
        from siming.meal_guard import stage_dual_shop_combo_approval

        res_c = stage_dual_shop_combo_approval(db, mt_combo["query"])
        reply_c = str(
            res_c.get("reply")
            or res_c.get("card_text")
            or (f"⚠️ {res_c['error']}" if res_c.get("error") else "")
        )
        return {"handled": True, "reply": reply_c, "keyboard": res_c.get("keyboard")}

    mt_rec = parse_meituan_recommend(u)
    if mt_rec is not None:
        from siming.meituan_agent import _parse_meal_hint, recommend_meituan_meals
        from siming.spatial_life import get_prestaged_dinner_fast_reply

        hint_s = mt_rec["hint"]
        loc_ov_rec = mt_rec["location_override"]
        parsed_mt = _parse_meal_hint(hint_s, now_local())
        if parsed_mt["slot"] == "晚餐" and not parsed_mt["keyword"] and parsed_mt["max_budget"] is None and not loc_ov_rec:
            fast_hit = get_prestaged_dinner_fast_reply(db)
            if fast_hit:
                return {"handled": True, "reply": fast_hit}
        rec_res = recommend_meituan_meals(db, hint=hint_s, location_override=loc_ov_rec)
        return {"handled": True, "reply": rec_res["reply"]}

    mt_spec = parse_meituan_spec_action(u, db)
    if mt_spec is not None:
        from siming.modules import is_module_enabled

        if is_module_enabled(db, "meituan_agent"):
            if mt_spec["mode"] in ("confirm_idx", "confirm_name"):
                from siming.meituan_speculate import confirm_and_execute_speculative_pick

                return {"handled": True, "reply": confirm_and_execute_speculative_pick(db, mt_spec["target"])["reply"]}
            if mt_spec["mode"] == "preview":
                from siming.meituan_agent import stage_meituan_h5_order

                return {
                    "handled": True,
                    "reply": stage_meituan_h5_order(
                        db,
                        mt_spec["target"],
                        location_override=mt_spec["location_override"],
                    )["reply"],
                }

    mt_order = parse_meituan_direct_order(u)
    if mt_order is not None:
        from siming.modules import is_module_enabled

        if is_module_enabled(db, "meituan_agent"):
            if mt_order.get("is_luckin"):
                from siming.luckin_mcp import get_luckin_mcp_token, stage_luckin_order
                from siming.meituan_agent import stage_meituan_h5_order

                if get_luckin_mcp_token(db):
                    return {"handled": True, "reply": stage_luckin_order(db, mt_order["query"])["reply"]}
                return {
                    "handled": True,
                    "reply": stage_meituan_h5_order(
                        db,
                        mt_order["query"],
                        location_override=mt_order["location_override"],
                    )["reply"],
                }
            from siming.meituan_agent import stage_meituan_h5_order

            return {
                "handled": True,
                "reply": stage_meituan_h5_order(
                    db,
                    mt_order["query"],
                    location_override=mt_order["location_override"],
                )["reply"],
            }

    sure_q = parse_sure_query(u)
    if sure_q is not None:
        from siming.sure_bridge import ask_sure_natural

        res_s = ask_sure_natural(db, sure_q["question"])
        return {"handled": True, "reply": str(res_s.get("reply") or "")}

    led_q = parse_ledger_natural_query(u)
    if led_q is not None:
        from siming.quiet_cfo import query_ledger_natural

        return {"handled": True, "reply": query_ledger_natural(db, led_q["query"])["reply"]}

    rep_act = parse_replenish_action(u)
    if rep_act is not None:
        if rep_act["action"] == "add":
            from siming.replenish_cart import upsert_replenish_item

            ra = upsert_replenish_item(
                db,
                name=rep_act["name"],
                sku_or_url=rep_act["sku_or_url"],
                cycle_days=int(rep_act["cycle_days"]),
                target_yuan=rep_act["target_yuan"],
            )
            return {"handled": True, "reply": ra.get("summary") or f"❌ {ra.get('error')}"}
        if rep_act["action"] == "remove":
            from siming.replenish_cart import remove_replenish_item

            rr = remove_replenish_item(db, rep_act["target"])
            return {"handled": True, "reply": rr.get("summary") or f"❌ {rr.get('error')}"}
        if rep_act["action"] == "cart":
            from siming.modules import is_module_enabled
            from siming.replenish_cart import stage_jd_cart_add

            if is_module_enabled(db, "replenish_cart"):
                return {"handled": True, "reply": stage_jd_cart_add(db, cmd)["reply"]}
        if rep_act["action"] == "buy":
            from siming.modules import is_module_enabled
            from siming.replenish_cart import stage_replenish_buy

            if is_module_enabled(db, "replenish_cart"):
                return {"handled": True, "reply": stage_replenish_buy(db, rep_act["query"])["reply"]}
        if rep_act["action"] == "bought":
            from siming.replenish_cart import mark_replenish_bought

            rb = mark_replenish_bought(db, rep_act["target"])
            return {"handled": True, "reply": rb.get("summary") or f"❌ {rb.get('error')}"}
        if rep_act["action"] == "snooze":
            from siming.replenish_cart import snooze_replenish_item

            rs = snooze_replenish_item(db, rep_act["target"], days=int(rep_act.get("days") or 14))
            return {"handled": True, "reply": rs.get("summary") or f"❌ {rs.get('error')}"}

    movein_act = parse_movein_action(u)
    if movein_act is not None:
        act_mi = movein_act["action"]
        if act_mi == "arrival_mark":
            from siming.install_plan import mark_arrival_inspected

            r_arr = mark_arrival_inspected(db, target_query=movein_act["target"])
            return {"handled": True, "reply": r_arr.get("summary") or f"⚠️ {r_arr.get('error')}"}
        if act_mi == "gate":
            from siming.movein_pm import set_prereq_gate

            g_res = set_prereq_gate(db, movein_act["gate"], ready=bool(movein_act["ready"]))
            return {"handled": True, "reply": g_res.get("summary") or f"❌ {g_res.get('error')}"}
        if act_mi == "photos":
            from siming.movein_pm import list_home_photos

            return {"handled": True, "reply": list_home_photos(db, movein_act["query"])["reply"]}
        if act_mi == "note":
            from siming.movein_pm import save_appliance_note

            an = save_appliance_note(db, movein_act["target"], movein_act["note"])
            return {"handled": True, "reply": an.get("summary") or f"❌ {an.get('error')}"}
        if act_mi == "passport":
            from siming.movein_pm import query_appliance_passport

            return {"handled": True, "reply": query_appliance_passport(db, movein_act["query"])["reply"]}

    cw_act = parse_case_or_wait_action(u, db)
    if cw_act is not None:
        kind_cw = cw_act["kind"]
        if kind_cw == "wait_prop":
            from siming.waits import confirm_proposed_wait, dismiss_proposed_wait

            wid = cw_act["wait_id"]
            res = confirm_proposed_wait(db, wid) if cw_act["confirm"] else dismiss_proposed_wait(db, wid)
            return {"handled": True, "reply": res["reply"]}
        if kind_cw == "guard_restore":
            from siming.push_guard import restore_rule

            res = restore_rule(db, cw_act["rule_id"])
            return {"handled": True, "reply": res.get("reply") or res.get("error") or "未处理"}
        if kind_cw == "case_act":
            tid = cw_act["target_id"]
            tk = "case" if tid.startswith("S") else "wait"
            return _handle_subscribed_reply(db, tk, tid, cw_act["action"])
        if kind_cw == "disambiguate":
            return {"handled": True, "reply": cw_act["reply"]}
        if kind_cw == "explain":
            return {"handled": True, "reply": explain_item(db, cw_act["target_id"])}
        if kind_cw == "forget":
            return {"handled": True, "reply": forget_memory(db, cw_act["keyword"])}

    misc_act = parse_misc_commands(u)
    if misc_act is not None:
        act_m = misc_act["action"]
        if act_m == "pack_cw01":
            from siming.quiet_cfo import pack_cw01_domestic_bundle

            pk = pack_cw01_domestic_bundle(db)
            return {"handled": True, "reply": pk.get("summary") or f"⚠️ {pk.get('error')}"}
        if act_m == "reimburse_submit":
            from siming.ledger.reimburse_audit import mark_batch_submitted

            sub_res = mark_batch_submitted(db, batch_no=misc_act["batch_no"], draft_ref=misc_act["draft_ref"])
            return {"handled": True, "reply": sub_res.get("summary") or f"❌ {sub_res.get('error')}"}
        if act_m == "ledger_resolve":
            from siming.ledger.reconcile import resolve_ledger_txn

            act_map = {"备注": "note", "配对": "pair", "重复作废": "void-dup"}
            r_tx = resolve_ledger_txn(db, misc_act["tx_id"], act_map[misc_act["op"]], misc_act["val"])
            return {"handled": True, "reply": r_tx.get("summary") or f"❌ {r_tx.get('error')}"}
        if act_m == "receivable_add":
            from siming.quiet_cfo import register_receivable

            kind_r = "price_protect" if misc_act["is_pp"] else "manual"
            rv = register_receivable(
                db,
                ref_id="",
                title=misc_act["title"],
                expected_yuan=misc_act["amount"],
                kind=kind_r,
                card_hint=misc_act["card"] or ("1001" if kind_r == "price_protect" else "1004"),
            )
            return {"handled": True, "reply": rv.get("summary") or f"❌ {rv.get('error')}"}
        if act_m == "saved_place":
            from siming.ledger.reimburse import set_saved_place

            places = set_saved_place(db, misc_act["alias"], misc_act["addr"])
            p_desc = "、".join(f"{k}={v}" for k, v in places.items())
            return {
                "handled": True,
                "reply": f"📍 已保存常用地址 `{misc_act['alias']}` -> `{misc_act['addr']}`（当前预设：{p_desc}）。",
            }
        if act_m == "wifi_bind":
            from siming.context_loc import bind_wifi_to_place

            wb = bind_wifi_to_place(db, misc_act["ssid"], misc_act["alias"])
            return {"handled": True, "reply": wb.get("summary") or f"❌ {wb.get('error')}"}
        if act_m == "otp_login":
            from siming.otp_vault import run_authorized_sms_login

            return {
                "handled": True,
                "reply": run_authorized_sms_login(
                    db,
                    misc_act["platform"],
                    reuse_existing_tab=(misc_act["verb"] == "填验证码"),
                )["reply"],
            }
        if act_m == "resume":
            from siming.dev_twin import build_ten_second_resume_card

            return {"handled": True, "reply": build_ten_second_resume_card(db, project_query=misc_act["project"])["reply"]}
        if act_m == "routine_mutate":
            from siming.event_chain import mutate_routine_status

            act_w = misc_act["verb"]
            new_st = "active" if ("恢复" in act_w or "开启" in act_w) else ("deleted" if "删" in act_w else "paused")
            return {"handled": True, "reply": mutate_routine_status(db, misc_act["rid"], new_status=new_st)}
        if act_m == "bookmarks_list":
            from siming.superagent_triage import format_bookmarks_list

            return {"handled": True, "reply": format_bookmarks_list(db, query=misc_act["query"])}
        if act_m == "bookmark_try":
            from siming.superagent_triage import sandbox_try_bookmark

            return {"handled": True, "reply": sandbox_try_bookmark(db, misc_act["target"])["reply"]}
        if act_m == "sleep_watch":
            from siming.night_lab import register_sleep_handoff_watch

            return {"handled": True, "reply": register_sleep_handoff_watch(db, misc_act["target"])["reply"]}
        if act_m == "boardgame_cabinet":
            from siming.night_lab import evaluate_boardgame_box_capacity

            return {"handled": True, "reply": evaluate_boardgame_box_capacity(db, misc_act["item"])["reply"]}
        if act_m == "graph_query":
            from siming.context_graph import format_graph_answer, query_context_graph

            return {"handled": True, "reply": format_graph_answer(query_context_graph(db, misc_act["query"]))}
        if act_m == "natal":
            from siming.ziwei_ref import natal_reply

            return {"handled": True, "reply": natal_reply(db, misc_act["dim"], full=bool(misc_act["full"]))}
        if act_m == "morning_full_section":
            from siming.morning import get_morning_full_reply

            return {"handled": True, "reply": get_morning_full_reply(db, section_hint=misc_act["section"])}

    # 5.5 主动求证回复、特斯拉控制/出行参谋、送装改期重排、医院挂号闭环
    from siming.curiosity import try_handle_curiosity_reply

    cur_ans = try_handle_curiosity_reply(db, raw)
    if cur_ans:
        return {"handled": True, "reply": cur_ans}

    m_totp = re.match(r"^(?:特斯拉码\s*)?(\d{6})$", cmd)
    if m_totp and (db.get_meta("pending_tesla_totp_tid") or "").strip():
        from siming.tesla_link import submit_tesla_sso_totp

        return {"handled": True, "reply": submit_tesla_sso_totp(db, m_totp.group(1))["reply"]}

    from siming.tesla_link import (
        control_tesla_climate,
        is_low_soc_override_followup,
        parse_explicit_tesla_climate_command,
        start_manual_tesla_precondition,
        sync_mobile_outing_plan,
    )

    ts_mut = parse_tesla_mutation(u)
    if ts_mut is not None:
        if ts_mut["action"] == "wake":
            from siming.tesla_link import format_tesla_status, poll_tesla_cn_fleet_if_configured

            poll_tesla_cn_fleet_if_configured(db, force=True, allow_wake=True)
            return {"handled": True, "reply": format_tesla_status(db)}
        if ts_mut["action"] == "set_soc":
            from siming.tesla_link import ingest_tesla_state

            pl: dict[str, Any] = {"soc": ts_mut["soc"]}
            if ts_mut["range_km"]:
                pl["range_km"] = ts_mut["range_km"]
            ing = ingest_tesla_state(db, pl, source="manual_qq")
            return {"handled": True, "reply": f"⚡ 已更新特斯拉状态：电量 **{ing['soc_pct']}%** · 续航 **{ing['range_km']} km**。"}
        if ts_mut["action"] == "outing_plan":
            from siming.outing_chain import plan_tesla_or_didi_outing

            dest_spec = ts_mut["dest"]
            sync_mobile_outing_plan(db, f"开车去 {dest_spec}", source="manual_qq")
            return {"handled": True, "reply": plan_tesla_or_didi_outing(db, dest_spec)["reply"]}

    explicit_ac = parse_explicit_tesla_climate_command(raw)
    if explicit_ac is not None:
        if explicit_ac["action"] == "on":
            m_res = start_manual_tesla_precondition(
                db,
                target_temp_c=explicit_ac.get("target_temp_c"),
            )
            return {"handled": True, "reply": m_res["reply"]}
        was_guarded = bool(db.get_meta("tesla_auto_climate_guard_json") or "")
        db.set_meta("tesla_auto_climate_guard_json", "")
        db.set_meta("tesla_low_soc_override_ctx", "")
        if explicit_ac.get("soft_cancel"):
            from siming.event_chain import get_situation

            sit_t = get_situation(db, "tesla:main")
            ac_on_now = bool((sit_t["value"] if sit_t else {}).get("is_climate_on"))
            if not ac_on_now and not was_guarded:
                return {"handled": True, "reply": "✅ 已取消本次备车（当前空调未开启，未唤醒车辆）。"}
        c_res = control_tesla_climate(db, turn_on=False)
        if c_res.get("ok"):
            return {"handled": True, "reply": "✅ 已关闭 Model Y 车内空调。"}
        return {"handled": True, "reply": f"⚠️ 关闭空调未成功：{c_res.get('error', '')}"}

    low_soc_followup = is_low_soc_override_followup(db, raw, reply_to_text=reply_to_text)
    if low_soc_followup is not None:
        m_res = start_manual_tesla_precondition(
            db,
            dest_hint=str(low_soc_followup.get("dest") or "外出用车"),
        )
        return {"handled": True, "reply": m_res["reply"]}

    if not any(sep in cmd for sep in ("；", ";", "、")) and not re.search(r"(?:销掉|消掉|核销|提醒我)", cmd):
        outing_sync = sync_mobile_outing_plan(db, raw, source="manual_qq")
        if outing_sync.get("matched"):
            return {"handled": True, "reply": outing_sync["reply"]}

    # 送装前置工序改期同步触发事件链重排（无需等 2m tick）
    from siming.event_chain import parse_and_apply_reschedule_signal

    resched = parse_and_apply_reschedule_signal(db, raw, source="manual_qq", notify=False)
    if resched.get("matched"):
        rep_c = resched["replan"]
        return {
            "handled": True,
            "reply": rep_c.get("card_text")
            or f"✅ 已更新 `{resched['gate_key']}` 至 `{resched['new_ready_at'][:16]}`（下游余量充足无冲突）。",
        }

    # 常用医院复诊挂号告知自动挂入前晚带抽血单义务链
    from siming.spatial_life import ingest_hospital_appointment_and_arm_chain

    hosp = ingest_hospital_appointment_and_arm_chain(db, raw)
    if hosp.get("matched"):
        return {
            "handled": True,
            "reply": f"🏥 已建立常用医院复诊闭环：就诊时间 `{hosp['visit_at'][:16]}`，并已排好前一天 `20:00` 提醒带抽血化验单与核对空腹要求！",
        }

    # 5.8 泛化自然语言提醒/延后建单（兼容「提醒我明天10点开会」「3天后提醒我续费」「半小时后叫我取快递」「等PR合并提醒我」）
    rem_act = parse_defer_remind(u, db)
    if rem_act is not None:
        if rem_act.get("error"):
            return {"handled": True, "reply": f"❌ {rem_act['error']}"}
        w = add_wait(db, when=rem_act["when_arg"], mode="remind", strength="explicit", quote=rem_act["quote"], agent="qqbot")
        return {
            "handled": True,
            "reply": f"[{w['wait_id']}] 已记下：{rem_act['quote']}\n触发条件：{w['next_fire_at'] or (w['trigger_kind'] + ':' + w['trigger_expr'])}（引用回「取消」可撤回）",
        }

    # 6. Layer 2A 声明式预置只读视图目录（36+ 面板统一支持口语包装、词序无关概念帧与高阈值错别字容错）
    view_hit = match_preset_view(u)
    if view_hit is not None:
        dispatched = _dispatch_preset_view(db, view_hit["intent"], cmd_q)
        if dispatched is not None:
            return dispatched

    # 6.5 本机知识只读问答（「查一下次卧窗帘盒多宽」「搜一下工长报价」——放在预置视图之后避免抢走「查一下算力资产/待办」）
    ask_q = parse_knowledge_ask(u)
    if ask_q:
        res = ask_knowledge(db, ask_q, use_llm_synthesis=False)
        return {"handled": True, "reply": res["answer"]}

    # 7. W4-6 语音/口语复合指令流：单句含 2+ 原子动作时打包成一张统一 A2 确认按钮卡
    if any(sep in cmd for sep in ("、", "，", ",", "；", ";", "并且", "顺便", "然后", "另外", "还有", "再", "以及", "同时")):
        try:
            from siming.compound_voice import parse_compound_utterance, stage_compound_approval

            parsed_cmp = parse_compound_utterance(db, cmd)
            if parsed_cmp.get("is_compound"):
                staged_cmp = stage_compound_approval(db, cmd)
                if staged_cmp.get("ok"):
                    return {
                        "handled": True,
                        "reply": str(staged_cmp.get("reply") or staged_cmp.get("card_text") or ""),
                        "keyboard": staged_cmp.get("keyboard"),
                    }
        except Exception:
            pass

    # 8. Layer 3 可插拔安全语义兜底（仅限白名单只读视图与只读查询，严守 D-19 绝不触发 A2 执行/付款）
    sem_hit = classify_qq_intent_with_llm(db, u, llm_classifier=llm_classifier)
    if sem_hit is not None:
        dispatched_sem = _dispatch_preset_view(db, sem_hit["intent"], cmd_q, args=sem_hit.get("args"))
        if dispatched_sem is not None:
            return dispatched_sem

    # 8.5 JEV 高置信美团只读推荐兜底：若 JEV 明确判定为 `siming_meituan` 且 Layer 2 未命中（如全新口语「晚上想吃点热乎的粉」「今天不想做饭了」），
    # 仅在确认非元讨论/吐槽/明确点单动作时触发零扣款只读推荐卡（带原生 QQ 门店按钮），绝不触碰 A2 下单/付款！
    if (
        isinstance(effective_jev, dict)
        and effective_jev.get("verdict") == "siming_meituan"
        and not u.is_meta
        and not u.is_multiline
        and len(raw) <= 48
    ):
        from siming.jev_gate import load_jev_gate_config
        from siming.modules import is_module_enabled

        jev_cfg = load_jev_gate_config(policy=load_policy(db.paths))
        rescue_min = float(jev_cfg.get("meituan_readonly_rescue_min_prob") or 0.78)
        mt_prob = float((effective_jev.get("probabilities") or {}).get("siming_meituan") or 0.0)
        looks_like_explicit_order_or_complaint = bool(
            re.search(
                r"(?:不好吃|难吃|送得好慢|太慢|没成功|失败|退款|取消|投诉|自动支付|密码|收银台|"
                r"加购|下单|复购|再来一单|老样子|加上|再加|去掉|不要)",
                raw,
            )
        )
        if mt_prob >= rescue_min and not looks_like_explicit_order_or_complaint and is_module_enabled(db, "meituan_agent"):
            from siming.meituan_agent import recommend_meituan_meals
            from siming.qq_nlu import extract_location_override

            loc_jev, _ = extract_location_override(raw)
            rec_jev = recommend_meituan_meals(db, hint=raw, location_override=loc_jev)
            if rec_jev.get("reply"):
                return {"handled": True, "reply": rec_jev["reply"], "jev": effective_jev}

    return {"handled": False}
