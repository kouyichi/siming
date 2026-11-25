"""W4-6 (Part A) · QQ 语音复合指令流（多意图拆分 + 实体消歧 + 单张统一 A2 确认卡 + 确定性顺序执行）。

例如用户口述：「销掉牛板筋、点份示例粉面送到家、设下周三抢号」
-> 拆分为 3 个原子意图：
   1. close_case (匹配待办 ID)
   2. meituan_order (示例粉面 -> 家)
   3. defer_remind (下周三 · 抢号)
-> 封装进单张 A2 审批卡 (`create_approval(action="compound_voice_batch", ...)`) + QQ 原生按钮（✅ 全部执行 / ❌ 取消）。
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from siming.approval import create_approval
from siming import db as _db
from siming.db import SimingDB, iso_now
from siming.qq_cards import make_button, make_keyboard
from siming.social_boardgames import add_commitment, extract_commitment_from_text

# 分句分隔符：顿号、逗号、分号，以及口语连接词（然后/顺便/并且/另外/还有/再帮我/以及/同时）
_SPLIT_RE = re.compile(r"[、，,;；\n]+|\s*(?:然后|顺便|并且|另外|还有|再帮我|再给我|以及|同时)\s*")

_CLOSE_CASE_RE = re.compile(r"^(?:帮我|替我|给我)?(?:销掉|消掉|核销|关闭|划掉|结单|完成|搞定|销一下|消一下)\s*(.+)$")
_CLOSE_CASE_BA_RE = re.compile(r"^(?:帮我|替我|给我)?把\s*(.+?)\s*(?:销掉|消掉|核销|划掉|关闭|标记完成)$")
_MEITUAN_RE = re.compile(
    r"^(?:帮我|替我|给我)?(?:点份|点个|点杯|点一份|点一杯|点一碗|来份|来个|来杯|来一份|来一杯|买份|买个|买杯|外卖点|美团点|点外卖|下个单|下单|叫份|叫个)\s*(.+)$"
)
_DEFER_REMIND_RE = re.compile(
    r"^(?:帮我|替我|给我)?(?:设个?|定个?|加个?|记一下|提醒我?|闹钟|别忘了)\s*(.+)$"
)
_TIME_FIRST_REMIND_RE = re.compile(
    r"^(?:帮我|替我|给我)?((?:今天|明天|后天|大后天|今晚|明早|明晚|下周[一二三四五六日天]|(?:这周|本周|周|星期)[一二三四五六日天]|\d{1,2}月\d{1,2}[日号]|半个?小时后|\d+\s*(?:分钟|个?小时|天)后).{0,16}?)(?:提醒我?|叫我|设个?闹钟|定个?闹钟|闹钟|别忘了)\s*(.+)$"
)
_DIDI_CLAUSE_RE = re.compile(
    r"^(?:帮我|替我|给我)?(?:从([^\s打叫滴]{1,16}?))?(?:打车|叫车|打个车|叫辆车|打滴滴|打个滴滴|叫滴滴)(?:(回家|回公司)|(?:去|到)\s*(.{2,24}))$"
)
_CASUAL_CLOSE_TARGET_RE = re.compile(
    r"(?:念头|想法|打算|主意|计划|心事|情绪|烦恼|焦虑|压力|疲惫|倦意|梦|算了|再说|今天的工作)"
)
_LEDGER_QUERY_RE = re.compile(r"(查账|花了多少|消费统计|本月账单|查一下.*账|支出汇总)")
_CARE_ACTION_RE = re.compile(r"^(?:已经)?(打了|打针了|已打针|已开药|已约|跳过这针|本周跳过|吃药了)(.*)$")

_WEEKDAY_MAP = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}
_CN_HOUR_MAP = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6,
    "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12,
}

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

_CLOCK_TIME_RE = re.compile(
    r"(?:(上午|早上|清晨|中午|下午|晚上|傍晚)\s*)?(1\d|2[0-3]|\d|十一|十二|[零一二两三四五六七八九十])\s*(?:[点时:：])\s*(?:(半)|(\d{1,2})\s*分?)?"
)


def _parse_hour_token(tok: str) -> int:
    s = (tok or "").strip()
    if s.isdigit():
        return int(s)
    return _CN_HOUR_MAP.get(s, 9)


def _extract_clock_time(expr: str, default_hour: int = 9) -> tuple[int, int, str]:
    m = _CLOCK_TIME_RE.search(expr or "")
    if not m:
        return default_hour, 0, ""
    period, h_raw, half, min_raw = m.group(1) or "", _parse_hour_token(m.group(2)), m.group(3), m.group(4)
    hour = h_raw
    if period in ("下午", "晚上", "傍晚") and 1 <= hour <= 11:
        hour += 12
    elif period == "中午" and 1 <= hour <= 5:
        hour += 12
    hour = max(0, min(23, hour))
    minute = 30 if half else (max(0, min(59, int(min_raw))) if min_raw else 0)
    return hour, minute, m.group(0)


def _resolve_relative_time(expr: str, base: Optional[datetime] = None) -> tuple[str, str]:
    """解析中文口语时间提示（含具体钟点，如「明天9点」「下周三下午3点半」），返回 (iso_time, matched_phrase)。"""
    cur = base or _db.now_local()
    s = expr or ""
    hour, minute, clock_phrase = _extract_clock_time(s, default_hour=9)

    def _combine(day_phrase: str) -> str:
        if day_phrase and clock_phrase and clock_phrase not in day_phrase:
            return f"{day_phrase}{clock_phrase}" if f"{day_phrase}{clock_phrase}" in s else day_phrase
        return day_phrase or clock_phrase

    m_next_wk = re.search(r"下周([一二三四五六日天])", s)
    if m_next_wk:
        target_wd = _WEEKDAY_MAP[m_next_wk.group(1)]
        days_to_next_mon = 7 - cur.weekday()
        target_date = cur.date() + timedelta(days=days_to_next_mon + target_wd)
        fire_dt = datetime(target_date.year, target_date.month, target_date.day, hour, minute, 0, tzinfo=cur.tzinfo)
        return (iso_now(fire_dt), _combine(m_next_wk.group(0)))

    m_this_wk = re.search(r"(?:这周|本周|周)([一二三四五六日天])", s)
    if m_this_wk:
        target_wd = _WEEKDAY_MAP[m_this_wk.group(1)]
        delta = (target_wd - cur.weekday()) % 7
        if delta == 0:
            delta = 7
        target_date = cur.date() + timedelta(days=delta)
        fire_dt = datetime(target_date.year, target_date.month, target_date.day, hour, minute, 0, tzinfo=cur.tzinfo)
        return (iso_now(fire_dt), _combine(m_this_wk.group(0)))

    if "今天" in s:
        d = cur.date()
        fire_dt = datetime(d.year, d.month, d.day, hour, minute, 0, tzinfo=cur.tzinfo)
        if fire_dt <= cur and not clock_phrase:
            fire_dt = fire_dt + timedelta(days=1)
        return (iso_now(fire_dt), _combine("今天"))
    if "明天" in s:
        d = cur.date() + timedelta(days=1)
        return (iso_now(datetime(d.year, d.month, d.day, hour, minute, 0, tzinfo=cur.tzinfo)), _combine("明天"))
    if "后天" in s:
        d = cur.date() + timedelta(days=2)
        return (iso_now(datetime(d.year, d.month, d.day, hour, minute, 0, tzinfo=cur.tzinfo)), _combine("后天"))
    if "今晚" in s:
        d = cur.date()
        ev_h, ev_m, _ = _extract_clock_time(s, default_hour=20)
        if ev_h < 12 and clock_phrase and not any(p in clock_phrase for p in ("上午", "早上", "清晨")):
            ev_h += 12
        return (iso_now(datetime(d.year, d.month, d.day, ev_h, ev_m, 0, tzinfo=cur.tzinfo)), _combine("今晚"))

    m_days = re.search(r"(\d+)\s*天后", s)
    if m_days:
        d = cur.date() + timedelta(days=max(1, int(m_days.group(1))))
        return (iso_now(datetime(d.year, d.month, d.day, hour, minute, 0, tzinfo=cur.tzinfo)), _combine(m_days.group(0)))

    if "半小时后" in s or "半个小时后" in s:
        fire_dt = cur + timedelta(minutes=30)
        return (iso_now(fire_dt), "半个小时后" if "半个小时后" in s else "半小时后")

    m_rel_hm = re.search(r"(\d+)\s*(分钟|个?小时)后", s)
    if m_rel_hm:
        amt = max(1, int(m_rel_hm.group(1)))
        fire_dt = cur + (timedelta(minutes=amt) if "分" in m_rel_hm.group(2) else timedelta(hours=amt))
        return (iso_now(fire_dt), m_rel_hm.group(0))

    m_md = re.search(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", s)
    if m_md:
        mo, da = int(m_md.group(1)), int(m_md.group(2))
        for yr in (cur.year, cur.year + 1):
            try:
                fire_dt = datetime(yr, mo, da, hour, minute, 0, tzinfo=cur.tzinfo)
            except ValueError:
                continue
            if fire_dt.date() >= cur.date() or yr == cur.year + 1:
                return (iso_now(fire_dt), _combine(m_md.group(0)))

    # 默认次日 09:00（若给出明确钟点则使用该钟点）
    d = cur.date() + timedelta(days=1)
    return (iso_now(datetime(d.year, d.month, d.day, hour, minute, 0, tzinfo=cur.tzinfo)), clock_phrase)


def _match_open_cases(db: SimingDB, target: str) -> list[dict[str, Any]]:
    q = (target or "").strip().lower()
    if not q:
        return []
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT case_id, title, status, category
            FROM cases
            WHERE status NOT IN ('完成', '取消', '已关闭', '已取消', '已归档', 'CLOSED', 'DONE', 'CANCELLED')
            ORDER BY updated_at DESC
            """
        ).fetchall()
    finally:
        conn.close()
    matches: list[dict[str, Any]] = []
    for r in rows:
        title = str(r["title"] or "")
        if q in title.lower() or q == str(r["case_id"]).lower():
            matches.append(dict(r))
    return matches


def _classify_clause(db: SimingDB, clause: str, index: int) -> Optional[dict[str, Any]]:
    s = clause.strip(" 。！？!?")
    if not s:
        return None

    # 1. close_case（兼容「销掉牛板筋」与「把牛板筋销掉」）
    m_close = _CLOSE_CASE_RE.match(s) or _CLOSE_CASE_BA_RE.match(s)
    if m_close:
        raw_target = m_close.group(1).strip()
        target = re.sub(r"^(?:把|那个|这个|那条|这条|那笔|这笔)\s*", "", raw_target)
        target = re.sub(r"(?:的?(?:待办|单子|提醒|任务|事项))$", "", target).strip()
        matches = _match_open_cases(db, target)
        # 拦截闲聊比喻（如「销掉那个念头吧」「完成今天的工作」），未命中真实待办时不伪造 close_case 审批卡
        if not matches and (
            not target
            or _CASUAL_CLOSE_TARGET_RE.search(raw_target)
            or re.search(r"(?:吧|算了|再说|算啦|算罢)$", raw_target)
        ):
            return None
        resolved_id = matches[0]["case_id"] if len(matches) == 1 else ""
        resolved_title = matches[0]["title"] if len(matches) == 1 else ""
        if resolved_id:
            summary = f"核销待办 `{resolved_id}`（{resolved_title}）"
        elif len(matches) > 1:
            cands = " / ".join(f"`{m['case_id']}`({m['title'][:16]})" for m in matches[:4])
            summary = f"核销待办「{target}」（命中 {len(matches)} 条候选：{cands}，需消歧，不会盲猜关闭）"
        else:
            summary = f"核销待办「{target}」（未匹配到开启待办，将按关键词核查）"
        return {
            "index": index,
            "kind": "close_case",
            "raw_clause": s,
            "target": target,
            "resolved_target_id": resolved_id,
            "resolved_title": resolved_title,
            "ambiguous_candidates": [m["case_id"] for m in matches] if len(matches) > 1 else [],
            "params": {"target": target, "case_id": resolved_id},
            "summary": summary,
        }

    # 2. meituan_order
    m_mt = _MEITUAN_RE.match(s)
    if m_mt or ("送到家" in s or "送到公司" in s):
        body = m_mt.group(1).strip() if m_mt else s
        address = "家"
        if "送到公司" in body or "到公司" in body:
            address = "公司"
            body = re.sub(r"送到公司|到公司", "", body).strip()
        elif "送到家" in body or "到家" in body:
            address = "家"
            body = re.sub(r"送到家|到家", "", body).strip()
        item_name = body.strip(" ，。的") or s
        return {
            "index": index,
            "kind": "meituan_order",
            "raw_clause": s,
            "target": item_name,
            "resolved_target_id": f"meituan:{item_name}:{address}",
            "resolved_title": f"{item_name} -> {address}",
            "params": {"query": item_name, "address_alias": address},
            "summary": f"美团点餐「{item_name}」送到【{address}】",
        }

    # 3. defer_remind（同时兼容「设明天9点提醒我抢号」与「明天下午3点半提醒我开会」）
    m_rem = _DEFER_REMIND_RE.match(s)
    m_time_rem = None if m_rem else _TIME_FIRST_REMIND_RE.match(s)
    if m_rem or m_time_rem:
        if m_rem:
            body = m_rem.group(1).strip()
            fire_at, time_phrase = _resolve_relative_time(body)
            task = body.replace(time_phrase, "", 1).strip() if time_phrase else body
            task = re.sub(r"^(?:的|地)?(?:提醒我?|设个?闹钟|定个?闹钟|闹钟|别忘了)\s*", "", task)
            task = re.sub(r"(?:的)?(?:提醒|闹钟)$", "", task).strip()
            task = task or body
        else:
            time_head = m_time_rem.group(1).strip()
            task_tail = m_time_rem.group(2).strip()
            fire_at, time_phrase = _resolve_relative_time(time_head)
            task = re.sub(r"^(?:去|要)?", "", task_tail).strip() or task_tail
        return {
            "index": index,
            "kind": "defer_remind",
            "raw_clause": s,
            "target": task,
            "resolved_target_id": "",
            "resolved_title": task,
            "params": {"task": task, "fire_at": fire_at, "time_phrase": time_phrase},
            "summary": f"设定提醒「{task}」（触发时间 {fire_at[:16].replace('T', ' ')}）",
        }

    # 4. ledger_query
    if _LEDGER_QUERY_RE.search(s):
        return {
            "index": index,
            "kind": "ledger_query",
            "raw_clause": s,
            "target": s,
            "resolved_target_id": "",
            "resolved_title": s,
            "params": {"query": s},
            "summary": f"只读查账「{s}」",
        }

    # 5. care_action
    m_care = _CARE_ACTION_RE.match(s)
    if m_care:
        act = m_care.group(1)
        return {
            "index": index,
            "kind": "care_action",
            "raw_clause": s,
            "target": act,
            "resolved_target_id": "med_care",
            "resolved_title": act,
            "params": {"action": act, "note": m_care.group(2).strip()},
            "summary": f"记录照护动作「{act}」",
        }

    # 5.5 didi_ride
    m_didi = _DIDI_CLAUSE_RE.match(s)
    if m_didi:
        orig_d = (m_didi.group(1) or "").strip()
        dest_d = ("家" if "家" in m_didi.group(2) else "公司") if m_didi.group(2) else (m_didi.group(3) or "").strip()
        return {
            "index": index,
            "kind": "didi_ride",
            "raw_clause": s,
            "target": dest_d,
            "resolved_target_id": f"didi:{dest_d}",
            "resolved_title": f"打车 -> {dest_d}",
            "params": {"origin": orig_d, "dest": dest_d},
            "summary": f"滴滴叫车前往【{dest_d}】" + (f"（从 {orig_d}）" if orig_d else ""),
        }

    # 6. social_commitment
    extracted = extract_commitment_from_text(s)
    if extracted:
        return {
            "index": index,
            "kind": "social_commitment",
            "raw_clause": s,
            "target": extracted["person"],
            "resolved_target_id": "",
            "resolved_title": extracted["promise"],
            "params": {
                "person": extracted["person"],
                "promise": extracted["promise"],
                "due_at": extracted.get("due_hint", ""),
                "source_quote": s,
            },
            "summary": f"登记对「{extracted['person']}」的承诺：{extracted['promise']}",
        }

    return None


def parse_compound_utterance(db: SimingDB, text: str) -> dict[str, Any]:
    """将多意图中文自然口语拆分为结构化原子意图列表并做本地实体消歧。"""
    db.init_db()
    raw = (text or "").strip()
    if not raw:
        return {"ok": False, "raw_text": "", "is_compound": False, "intent_count": 0, "intents": []}

    clauses = [c.strip() for c in _SPLIT_RE.split(raw) if c and c.strip()]
    intents: list[dict[str, Any]] = []
    for clause in clauses:
        item = _classify_clause(db, clause, len(intents) + 1)
        if item is not None:
            intents.append(item)

    return {
        "ok": bool(intents),
        "raw_text": raw,
        "is_compound": len(intents) >= 2,
        "intent_count": len(intents),
        "intents": intents,
    }


def stage_compound_approval(db: SimingDB, text: str) -> dict[str, Any]:
    """把复合语音/文本指令打包成单张统一 A2 审批卡与 QQ 原生按钮键盘（✅ 全部执行 / ❌ 取消）。"""
    db.init_db()
    parsed = parse_compound_utterance(db, text)
    if not parsed.get("ok") or not parsed.get("intents"):
        return {
            "ok": False,
            "error": f"未能从「{text}」识别出可执行的复合动作。",
            "parsed": parsed,
        }

    intents = parsed["intents"]
    target_id = "+".join(
        it.get("resolved_target_id") or it["kind"] for it in intents[:5]
    )[:64]
    approval = create_approval(
        db,
        action="compound_voice_batch",
        target_id=target_id,
        params={"raw_text": text, "intents": intents},
        tier="A2",
    )
    token = str(approval["token"])

    lines = [
        f"🎙️ 复合指令统一确认卡（{len(intents)} 项动作 · 口令 `{token}`）",
        f"原话：{text}",
    ]
    for it in intents:
        lines.append(f"{it['index']}. [{it['kind']}] {it['summary']}")
    lines.append(f"回复 `apply {token}` 或点下方按钮一键执行（过期时间 {approval['expires_at'][:16].replace('T', ' ')}）。")
    card_text = "\n".join(lines)

    gid = f"sm_cmp_{token}"
    keyboard = make_keyboard(
        [
            [
                make_button(
                    "confirm_all",
                    "✅ 全部执行",
                    f"siming:apply:{token}",
                    visited_label="已全部执行",
                    style=1,
                    group_id=gid,
                ),
                make_button(
                    "cancel_all",
                    "❌ 取消",
                    f"siming:cancel:{token}",
                    visited_label="已取消",
                    style=0,
                    group_id=gid,
                ),
            ]
        ]
    )

    ambiguous_intents = [it for it in intents if it.get("ambiguous_candidates")]
    return {
        "ok": True,
        "token": token,
        "approval": approval,
        "parsed": parsed,
        "has_ambiguity": bool(ambiguous_intents),
        "ambiguous_intents": ambiguous_intents,
        "card_text": card_text,
        "reply": card_text,
        "keyboard": keyboard,
    }


def _default_execute_intent(db: SimingDB, intent: dict[str, Any]) -> dict[str, Any]:
    kind = str(intent.get("kind") or "")
    params = dict(intent.get("params") or {})
    ts = iso_now()

    if kind == "close_case":
        cid = str(params.get("case_id") or intent.get("resolved_target_id") or "").strip()
        if not cid:
            matches = _match_open_cases(db, str(params.get("target") or intent.get("target") or ""))
            if len(matches) == 1:
                cid = str(matches[0]["case_id"])
            elif len(matches) > 1:
                return {"ok": False, "kind": kind, "detail": f"命中多条待办（{[m['case_id'] for m in matches]}），需人工指定"}
            else:
                return {"ok": False, "kind": kind, "detail": f"未找到待办「{params.get('target', '')}」"}
        from siming.router import close_case as _router_close_case

        c_res = _router_close_case(db, cid, note="compound_voice")
        if not c_res.get("ok"):
            return {"ok": False, "kind": kind, "detail": str(c_res.get("error") or f"待办 {cid} 关闭失败")}
        with db.transaction() as conn:
            db.write_audit("A2", "compound_close_case", params, target_id=cid, conn=conn)
        return {"ok": True, "kind": kind, "target_id": cid, "detail": f"已核销待办 {cid}（{c_res.get('title', '')}）"}

    if kind == "defer_remind":
        task = str(params.get("task") or intent.get("target") or "提醒事项").strip()
        fire_at = str(params.get("fire_at") or iso_now(_db.now_local() + timedelta(days=1)))
        exp_at = iso_now(_db.now_local() + timedelta(days=30))
        mandate = {"task": task, "source": "compound_voice", "action_mode": "remind"}
        with db.transaction() as conn:
            wid = db.next_seq_id(conn, "W")
            conn.execute(
                """
                INSERT INTO waits (
                    wait_id, status, strength, trigger_kind, trigger_expr,
                    probe_interval_sec, next_fire_at, expires_at, action_mode,
                    session_ref, mandate_json, quote, created_at, updated_at
                ) VALUES (?, 'WAITING', 'explicit', 'time', ?, 1800, ?, ?, 'remind', 'compound_voice', ?, ?, ?, ?)
                """,
                (
                    wid,
                    fire_at,
                    fire_at,
                    exp_at,
                    json.dumps(mandate, ensure_ascii=False),
                    str(intent.get("raw_clause") or task),
                    ts,
                    ts,
                ),
            )
            db.write_audit("A2", "compound_defer_remind", {"wait_id": wid, **params}, target_id=wid, conn=conn)
        return {"ok": True, "kind": kind, "target_id": wid, "detail": f"已设挂起提醒 {wid}（{task} @ {fire_at[:16].replace('T', ' ')}）"}

    if kind == "meituan_order":
        query = str(params.get("query") or intent.get("target") or "").strip()
        addr = str(params.get("address_alias") or "家").strip()
        with db.transaction() as conn:
            ev_id = db.append_event(
                conn,
                source="compound_voice",
                thread_key=f"meituan:{query}",
                summary=f"复合指令排队点餐：{query} -> {addr}",
                payload={"query": query, "address_alias": addr, "mode": "staged_cart"},
            )
            db.write_audit("A2", "compound_meituan_order", params, target_id=query, conn=conn)
        return {"ok": True, "kind": kind, "target_id": ev_id or query, "detail": f"已提交美团点餐任务「{query}」送往【{addr}】"}

    if kind == "ledger_query":
        conn = db.connect()
        try:
            rows = conn.execute(
                """
                SELECT amount_cents, currency, kind FROM ledger_txns
                WHERE COALESCE(UPPER(status), '') NOT IN ('VOID_DUPLICATE', 'VOID', 'CANCELLED')
                """
            ).fetchall()
        finally:
            conn.close()
        spend_cnt = 0
        spend_cny = 0.0
        for r in rows:
            if str(r["kind"] or "") in ("收入", "退款", "还款", "转入"):
                continue
            curr = str(r["currency"] or "CNY").strip().upper()
            rate = float(_FX_TO_CNY.get(curr, 1.0))
            spend_cnt += 1
            spend_cny += (int(r["amount_cents"] or 0) / 100.0) * rate
        total_y = round(spend_cny, 2)
        return {"ok": True, "kind": kind, "detail": f"有效支出 {spend_cnt} 笔，折合累计 ¥{total_y:.2f}"}

    if kind == "care_action":
        act = str(params.get("action") or intent.get("target") or "")
        with db.transaction() as conn:
            db.append_event(
                conn,
                source="compound_voice",
                thread_key="med_care",
                summary=f"照护打卡：{act}",
                payload=params,
            )
            db.write_audit("A2", "compound_care_action", params, target_id="med_care", conn=conn)
        return {"ok": True, "kind": kind, "target_id": "med_care", "detail": f"已记录照护动作「{act}」"}

    if kind == "didi_ride":
        from siming.didi_mcp import request_didi_ride

        r_ride = request_didi_ride(
            db,
            dest_raw=str(params.get("dest") or intent.get("target") or ""),
            origin_raw=str(params.get("origin") or ""),
        )
        return {"ok": True, "kind": kind, "target_id": str(params.get("dest") or ""), "detail": str(r_ride.get("reply") or "已生成叫车预览")}

    if kind == "social_commitment":
        res = add_commitment(
            db,
            person=str(params.get("person") or ""),
            promise=str(params.get("promise") or ""),
            due_at=str(params.get("due_at") or ""),
            source_quote=str(params.get("source_quote") or ""),
        )
        return {"ok": True, "kind": kind, "target_id": res["commit_id"], "detail": f"已登记承诺 {res['commit_id']}"}

    return {"ok": False, "kind": kind, "detail": f"不支持的意图类型：{kind}"}


def execute_compound_intents(
    db: SimingDB,
    intents: list[dict[str, Any]],
    executors: Optional[dict[str, Callable[[SimingDB, dict[str, Any]], dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """按序确定性执行复合意图清单，逐项回报执行结果。"""
    db.init_db()
    custom = executors or {}
    results: list[dict[str, Any]] = []
    succeeded = 0
    failed = 0

    for idx, intent in enumerate(intents, 1):
        kind = str(intent.get("kind") or "")
        fn = custom.get(kind)
        try:
            step = fn(db, intent) if fn is not None else _default_execute_intent(db, intent)
        except Exception as exc:
            step = {"ok": False, "kind": kind, "detail": f"执行异常: {exc}"}
        step_rec = {"index": idx, "kind": kind, **step}
        results.append(step_rec)
        if step_rec.get("ok"):
            succeeded += 1
        else:
            failed += 1

    lines = [f"⚡ 复合指令执行完成：成功 {succeeded}/{len(intents)}"]
    for r in results:
        icon = "✅" if r.get("ok") else "❌"
        lines.append(f"{icon} {r['index']}. [{r['kind']}] {r.get('detail', '')}")

    return {
        "ok": failed == 0 and len(intents) > 0,
        "total": len(intents),
        "succeeded": succeeded,
        "failed": failed,
        "results": results,
        "summary": "\n".join(lines),
    }
