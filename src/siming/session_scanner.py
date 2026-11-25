"""每 2 分钟只读扫描各编码 Agent 新增用户输入（0 token 正则初筛 + 锁死 inferred/remind）。
Phase 2 加固：
1. 严格读取至最后一个完整换行符（`rfind(b"\\n")`），杜绝 512KiB 截断或半行落盘丢消息；
2. 先完成 DB 写入再保存文件偏移游标（先落盘后推游标）；
3. 过滤 Claude `isSidechain` 子代理内部文本与 Hermes 定时任务提示词；
4. 仅在存在明确时间表达式或意图条件时建 inferred 挂起，取消 24h 即时打扰推送（统一收归晨札待确认）。
"""

from __future__ import annotations

import glob
import hashlib
import json
import re
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

from siming.cn_calendar import try_parse_explicit_datetime
from siming.crypto import contains_otp, redact_sensitive
from siming.db import SimingDB, iso_now, now_local
from siming.waits import add_wait, evaluate_waits, expire_stale_proposals

# 延后与提醒触发词初筛正则：未命中 0 token 直接跳过
DEFER_PREFILTER_RE = re.compile(
    r"(?:"
    r"等(?![，,。、；;：:！!？?\s等]|我拍板|你回复|会儿看)"  # 列举尾「补货等，…之后」「等等」「等我拍板」不是延后
    r".{1,28}(?:再说|再看|再改|再修|再跑|再做|再聊|再弄|再处理|时候|之后|以后|完|好|合并|到货|签收)"
    r"|(?:下周|月底|月初|明早|明天|后天|今晚|下午|晚上|周末|\d{1,2}月\d{1,2}[日号]?|过几天|回头).{0,18}(?:提醒我|再说|再看|再改|再处理|记得)"
    r"|(?<!需要你)提醒我(?!的[，,。、\s需]).{1,30}"
    r"|(?:先不管|先放着).{0,20}等.{1,20}"
    r")"
)

# 排除正在讨论司命自身设计的元话语、日常背景介绍、规划评审、命令行片段、排错纠偏或后台定时任务注入词
META_SKIP_RE = re.compile(
    r"(siming\s+defer|<CONTEXT_SUMMARY>|# Previous Session Summary|<USER_REQUEST>\s*$|测试样例说明"
    r"|You are running as a scheduled cron job|独立架构评审任务"
    r"|(?:你应该在|本该在|不应该在).{1,36}提醒我|为什么.{0,12}(?:现在)?发提醒|纠正你的错误"
    r"|(?:没|没有|未|怎么没|为什么没).{0,8}提醒我|提醒我.{0,12}(?:为什么|修复|怎么没)"
    r"|给你一些我日常的状态|观察这几天.{0,12}siming|深入\s*review|下一阶段\s*feature|波次=\{|取舍=\["
    r"|规划.{0,12}(?:更新到网页|放到网页|让我审阅)|合并成\s*wave\s*0)"
    , re.IGNORECASE,
)


def _extract_user_texts_from_jsonl_line(agent: str, line: str) -> list[str]:
    line = line.strip()
    if not line:
        return []
    try:
        obj = json.loads(line)
    except Exception:
        return []
    if not isinstance(obj, dict):
        return []

    out: list[str] = []
    if agent == "claude":
        # 严防把 Claude 子代理（isSidechain=True）或工具回执当成本人发言
        if (
            obj.get("type") == "user"
            and not obj.get("isMeta")
            and not obj.get("isSidechain")
            and obj.get("userType") in ("external", None)
        ):
            msg = obj.get("message")
            if isinstance(msg, dict) and msg.get("role") in ("user", None):
                content = msg.get("content")
                if isinstance(content, str):
                    out.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            out.append(str(block.get("text", "")))
    elif agent == "codex":
        payload = obj.get("payload")
        if isinstance(payload, dict):
            if obj.get("type") == "event_msg" and payload.get("type") == "user_message":
                txt = str(payload.get("message") or "")
                if txt:
                    out.append(txt)
    elif agent == "agy":
        if obj.get("source") == "USER_EXPLICIT" and obj.get("type") == "USER_INPUT":
            raw = str(obj.get("content") or "")
            if "<CONTEXT_SUMMARY>" in raw:
                return []
            m = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", raw, re.DOTALL)
            out.append(m.group(1).strip() if m else raw.strip())
    elif agent in ("hermes", "opencode"):
        if obj.get("role") == "user":
            content = obj.get("content")
            if isinstance(content, str):
                out.append(content)
            elif isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        out.append(str(block.get("text", "")))
    return [t.strip() for t in out if t and t.strip()]


def _discover_session_files(session_roots: Optional[dict[str, str]] = None) -> list[tuple[str, Path]]:
    home = Path.home()
    patterns = session_roots or {
        "claude": str(home / ".claude/projects/**/*.jsonl"),
        "codex": str(home / ".codex/sessions/**/*.jsonl"),
        "agy": str(home / ".gemini/antigravity-cli/brain/*/.system_generated/logs/transcript.jsonl"),
        "hermes": str(home / ".hermes/sessions/**/*.jsonl"),
    }
    cutoff_ts = now_local().timestamp() - 7 * 86400
    found: list[tuple[str, Path]] = []
    for agent, pat in patterns.items():
        for fp in glob.glob(pat, recursive=True):
            p = Path(fp)
            # 严禁扫描 siming 自身隔离 worktree 产生的无头编码代理会话，防止 worker prompt 里的“用户原话”引发自指死循环！
            p_str = str(p)
            if "worktrees" in p_str and "siming" in p_str:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if p.is_file() and st.st_mtime >= cutoff_ts:
                found.append((agent, p))
    return found


def _load_offsets(db: SimingDB) -> tuple[bool, dict[str, int]]:
    raw = db.get_meta("session_scanner_offsets")
    if not raw:
        return False, {}
    try:
        data = json.loads(raw)
        return True, {str(k): int(v) for k, v in data.items()}
    except Exception:
        return False, {}


def _save_offsets(db: SimingDB, offsets: dict[str, int], active_keys: Optional[set[str]] = None) -> None:
    # 超过 600 个文件时只淘汰本轮未出现的陈旧历史文件，绝不挤掉活跃长会话触发从 0 重扫循环！
    if len(offsets) > 600:
        keep = active_keys or set()
        kept_dict: dict[str, int] = {k: v for k, v in offsets.items() if k in keep}
        inactive = [(k, v) for k, v in offsets.items() if k not in keep]
        budget = max(0, 500 - len(kept_dict))
        for k, v in inactive[-budget:]:
            kept_dict[k] = v
        offsets = kept_dict
    db.set_meta("session_scanner_offsets", json.dumps(offsets, ensure_ascii=False))


def _recently_explicitly_deferred(db: SimingDB, text: str) -> bool:
    """剔除前后 3 分钟内已通过 siming defer 显式登记过的同一事项。"""
    cutoff = iso_now(now_local() - timedelta(minutes=3))
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT quote FROM waits WHERE strength='explicit' AND created_at >= ?",
            (cutoff,),
        ).fetchone()
    finally:
        conn.close()
    # 全量遍历近 3 分钟显式项
    conn = db.connect()
    try:
        all_rows = conn.execute(
            "SELECT quote FROM waits WHERE strength='explicit' AND created_at >= ?",
            (cutoff,),
        ).fetchall()
        for r in all_rows:
            q = str(r["quote"] or "").strip()
            if q and (q in text or text in q):
                return True
        return False
    finally:
        conn.close()


def _infer_trigger_from_sentence(sentence: str, db: SimingDB) -> Optional[tuple[str, bool]]:
    """把兜底捕获的口头延后句映射为 when 规则；按分句提取，严禁跨分句把无关形容词（如前句『好看一点』+ 后句『提醒我』）拼成假时间！"""
    clauses = [c.strip() for c in re.split(r"[。！？；;\n]+", sentence) if c.strip()]
    target_clauses = [c for c in clauses if DEFER_PREFILTER_RE.search(c)] or [sentence]

    for clause in target_clauses:
        # 1. 明确时间优先（如“明天下午三点提醒我”、“12月10日提醒我”）
        explicit_dt = try_parse_explicit_datetime(clause, paths=db.paths)
        if explicit_dt is not None:
            dt, _ = explicit_dt
            within_24h = (dt - now_local()) <= timedelta(hours=24)
            return (f"time:{iso_now(dt)}", within_24h)

        # 2. 意图或事件条件（“等报销的时候”、“等洗烘机到货再看”）
        m_intent = re.search(
            r"等(?:我要|需要|下次)?([^\s，。！？,!?]{2,14}?)(?:的时候|时再|到了|到货|签收|合并|跑完|修好)",
            clause,
        )
        if m_intent:
            kw = re.escape(m_intent.group(1).strip())
            if kw:
                return (f"intent:{kw}", False)

    return None


MAX_READ_CHUNK = 512 * 1024

# D-34：兜底扫描只「提议」，不直接建生效挂起。以下文本形态几乎都是给 Agent 的指令/需求/评审，不是口头延后
MAX_PROPOSAL_TEXT_LEN = 160
_NUMBERED_ITEM_RE = re.compile(r"(?:^|[\n；;。，,\s])\s*(?:\d{1,2}|[一二三四五六七八九十])\s*[.、)）]")
_INSTRUCTION_HINT_RE = re.compile(
    r"(review|报告|网页|做个|做一个|实现|重构|规划|方案|feature|架构)",
    re.IGNORECASE,
)


def _looks_like_instruction_not_deferral(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    # 斜杠命令（/swarm、/goal、/plan…）是给 Agent 的指令
    if t.startswith("/"):
        return True
    if len(t) > MAX_PROPOSAL_TEXT_LEN:
        return True
    # 多条编号清单（「1. … 2. …」）是需求列表，不是延后
    if len(_NUMBERED_ITEM_RE.findall(t)) >= 2:
        return True
    if t.count("\n") >= 3:
        return True
    # 提到工程/规划词又没有明确「提醒我」：多半是在派活
    if _INSTRUCTION_HINT_RE.search(t) and "提醒我" not in t:
        return True
    return False


def _supersede_recent_proposals(db: SimingDB, session_ref: str, window_min: int = 30) -> list[str]:
    cutoff = iso_now(now_local() - timedelta(minutes=window_min))
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT wait_id FROM waits WHERE status='PROPOSED' AND session_ref=? AND created_at >= ?",
            (session_ref, cutoff),
        ).fetchall()
        ids = [str(r["wait_id"]) for r in rows]
        for wid in ids:
            conn.execute(
                "UPDATE waits SET status='CANCELLED', result_summary='被同会话新提议合并', updated_at=? WHERE wait_id=?",
                (iso_now(), wid),
            )
    return ids


def _push_proposal_card(db: SimingDB, w: dict[str, Any]) -> dict[str, Any]:
    """24h 内要响的提议：发一条带按钮的轻询问（走 router 熔断与作息暂扣），不点就不生效。"""
    try:
        from siming.push import push_message
        from siming.qq_cards import make_button, make_keyboard

        wid = w["wait_id"]
        when = w.get("next_fire_at") or f"{w.get('trigger_kind')}:{w.get('trigger_expr')}"
        text = (
            f"📝 [{wid}] 你刚才好像说要我提醒：{str(w.get('quote') or '')[:60]}\n"
            f"• 我理解的时间：{when}\n"
            f"• 不点就不登记（72 小时后自动作废）。"
        )
        kb = make_keyboard(
            [
                [
                    make_button("keep", "✅ 记下", f"siming:cmd:记下 {wid}", visited_label="已记下", style=1, group_id=f"prop_{wid}"),
                    make_button("drop", "🙅 不用", f"siming:cmd:忽略 {wid}", visited_label="已忽略", style=0, group_id=f"prop_{wid}"),
                ]
            ]
        )
        return push_message(db, text, rule_id="scan_suggest", ref_id=wid, level="normal", keyboard=kb)
    except Exception as exc:  # 推送失败不影响提议落库（晨札里还会出现）
        return {"ok": False, "error": str(exc)[:200]}


def scan_agent_sessions(
    db: SimingDB,
    session_roots: Optional[dict[str, str]] = None,
    bootstrap_to_eof: bool = True,
) -> dict[str, Any]:
    """增量读取新增用户输入：触发已有 intent 挂起项，并兜底捕获漏登记的口头延后提醒。"""
    db.init_db()
    initialized, offsets = _load_offsets(db)
    files = _discover_session_files(session_roots)

    active_file_keys = {str(p) for _, p in files}

    if not initialized and bootstrap_to_eof and session_roots is None:
        for _agent, path in files:
            try:
                sz = path.stat().st_size
                if sz == 0:
                    offsets[str(path)] = 0
                    continue
                with path.open("rb") as fh:
                    tail_seek = max(0, sz - 65536)
                    fh.seek(tail_seek)
                    tail_bytes = fh.read()
                nl = tail_bytes.rfind(b"\n")
                offsets[str(path)] = (tail_seek + nl + 1) if nl >= 0 else 0
            except OSError:
                continue
        _save_offsets(db, offsets, active_keys=active_file_keys)
        db.mark_source_ok("agent_sessions", detail=f"bootstrapped {len(offsets)} files")
        return {"bootstrapped": True, "new_waits": [], "intent_fired": []}

    pending_offsets = dict(offsets)
    new_user_utterances: list[tuple[str, Path, str]] = []

    for agent, path in files:
        key = str(path)
        try:
            size = path.stat().st_size
        except OSError:
            continue
        prev = pending_offsets.get(key, 0)
        if size < prev:
            prev = 0
        if size == prev:
            pending_offsets[key] = size
            continue
        try:
            with path.open("rb") as fh:
                fh.seek(prev)
                chunk = fh.read(min(size - prev, MAX_READ_CHUNK))
                if not chunk:
                    continue
                last_nl = chunk.rfind(b"\n")
                if last_nl == -1:
                    if len(chunk) >= MAX_READ_CHUNK:
                        # 单行 >512KiB 巨型日志/附件：向后寻找到该超长行的真实 '\n' 结尾整行跳过，绝不停在半行中间破坏下一行 JSON！
                        skip_pos = prev + len(chunk)
                        found_nl = False
                        for _ in range(64):
                            more = fh.read(MAX_READ_CHUNK)
                            if not more:
                                break
                            nl_pos = more.find(b"\n")
                            if nl_pos >= 0:
                                pending_offsets[key] = skip_pos + nl_pos + 1
                                found_nl = True
                                break
                            skip_pos += len(more)
                        if not found_nl:
                            # 超长行尚未写完结尾换行，暂不动游标
                            pass
                    continue
        except OSError:
            continue

        complete_bytes = chunk[: last_nl + 1]
        pending_offsets[key] = prev + last_nl + 1

        for raw_line in complete_bytes.decode("utf-8", errors="replace").splitlines():
            for txt in _extract_user_texts_from_jsonl_line(agent, raw_line):
                # 先过滤 OTP、超长粘贴段与系统元提示词（含 worker prompt 的【重启恢复保护】/ 用户原话），再做 intent 触发！
                if contains_otp(txt) or len(txt) > 600 or META_SKIP_RE.search(txt):
                    continue
                if "你在隔离工作目录" in txt and "执行用户预授权任务" in txt:
                    continue
                new_user_utterances.append((agent, path, txt))

    intent_fired: list[str] = []
    if new_user_utterances:
        from siming.attention import touch_human_activity

        db.set_meta("last_agent_utterance_at", iso_now())
        touch_human_activity(db, source="agent_utterance")
        eval_res = evaluate_waits(
            db, intent_texts=[u[2] for u in new_user_utterances]
        )
        intent_fired.extend(eval_res["fired"])

    created_waits: list[dict[str, Any]] = []
    expire_stale_proposals(db)
    today_bucket = now_local().date().isoformat()
    for agent, path, text in new_user_utterances:
        if _looks_like_instruction_not_deferral(text):
            continue
        m = DEFER_PREFILTER_RE.search(text)
        if not m:
            continue
        snippet = m.group(0).strip()
        if _recently_explicitly_deferred(db, snippet):
            continue

        inferred_spec = _infer_trigger_from_sentence(text, db)
        if inferred_spec is None:
            continue
        when_spec, within_24h = inferred_spec

        # 指纹绑定 agent + 会话文件 + 日期桶 + 摘要；先确认未去重、建好 wait 后再落指纹，防中途崩溃吞掉 wait！
        fp = (
            "scan:"
            + hashlib.sha256(f"{agent}:{path}:{today_bucket}:{snippet}".encode("utf-8")).hexdigest()[:28]
        )
        conn_chk = db.connect()
        try:
            already = conn_chk.execute("SELECT 1 FROM events WHERE fingerprint=?", (fp,)).fetchone()
        finally:
            conn_chk.close()
        if already:
            continue

        # D-34：只提议不建单。同一会话 30 分钟内的连续提议合并为最新一条（避免一段对话切出 10 多条碎片）
        session_ref = f"{agent}:{path.stem}"
        superseded = _supersede_recent_proposals(db, session_ref)
        w = add_wait(
            db,
            when=when_spec,
            mode="remind",
            strength="inferred",
            quote=text[:160],
            agent=agent,
            session_id=path.stem,
            ttl="14d",
            initial_status="PROPOSED",
        )
        with db.transaction() as conn:
            db.append_event(
                conn,
                source="agent_sessions",
                thread_key=f"scan:{agent}",
                summary=f"[{agent} 兜底提议] {snippet[:80]}",
                payload={
                    "agent": agent,
                    "log": path.name,
                    "wait_id": w["wait_id"],
                    "status": "PROPOSED",
                    "superseded": superseded,
                    "utterance": redact_sensitive(text[:240], db.paths),
                },
                fingerprint=fp,
            )
        w["within_24h"] = within_24h
        w["status"] = "PROPOSED"
        if within_24h:
            w["suggest_push"] = _push_proposal_card(db, w)
        created_waits.append(w)

    # 全部事件与挂起项落盘提交后，再推进文件读取游标！
    _save_offsets(db, pending_offsets, active_keys=active_file_keys)

    db.mark_source_ok(
        "agent_sessions",
        detail=f"scanned={len(files)}, utterances={len(new_user_utterances)}, inferred={len(created_waits)}",
    )
    return {"bootstrapped": False, "new_waits": created_waits, "intent_fired": intent_fired}
