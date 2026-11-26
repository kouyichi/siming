"""Hermes Gateway 桥接插件（siming-bridge）：
1. 在 pre_gateway_dispatch 缓存消息的引用回复元数据与 QQ 最新 msg_id 时间戳，不跳过鉴权；
2. 在 post_gateway_admission（已通过本人鉴权后）优先走纯代码 apply 口令、引用回复直达、查全机、受控派单，
   并对包含确认框/选择框 keyboard 的司命回复直接经 QQAdapter 发送原生内嵌键盘；
   - 对 apply / 派单 等敏感确定性口令采用 fail-closed：超时或异常时绝不透传给底层对话模型；
   - 普通自然语言未命中时 fail-open 交还常规 Hermes Agent；
3. 经 register_platform_handler("qqbot", ...) 挂载原生 QQ 确认框/选择框能力（clarify / slash_confirm / choice_picker / siming:* 按钮回调 / 审批卡自动挂按钮）；
4. 经 register_system_prompt_section 注入 QQ 确认框使用规范；
5. 注册 /siming 斜杠命令作为严格的只读速查入口（禁止经 /siming 执行 apply / dispatch / pause 等写动作）。
"""

from __future__ import annotations

import asyncio
import collections
import importlib.util
import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Optional

SIMING_BIN = str(Path.home() / ".local" / "bin" / "siming")
_EVENT_META_CACHE: "collections.OrderedDict[str, dict[str, Any]]" = collections.OrderedDict()
_MAX_CACHE = 256
_BACKGROUND_TASKS: "set[asyncio.Task[Any]]" = set()
# Hermes Gateway 对 post_gateway_admission 钩子默认限时 30s；超过 8s 仍未返回的司命长任务自动转入后台异步回推，永不触发 30s 钩子超时漏回 LLM！
_HOOK_SYNC_WAIT_SEC = 8.0
_SENSITIVE_DETERMINISTIC_RE = re.compile(
    r"^(?:/?apply\s+A\d{2,6}\b|(?:(?:帮我|请)?(?:确认支付|确认付款|确认下单|去支付|立即支付|确认并支付)(?:\s+A\d{2,6})?(?:吧)?)$|(?:支付|付款|取消|放弃|作废)\s+A\d{2,6}$|(?:取消支付|取消付款|放弃下单|取消点单)$|派单\s+|(?:密码|解压码)\s*[：:]?\s*[A-Za-z0-9]{4,16}$|(?:滴滴密钥|滴滴key|瑞幸密钥|瑞幸token|luckin[_\-\s]*key|绑定手机号|授权手机号)\s*[：:]?\s*\S+$)",
    re.IGNORECASE,
)
_LONG_RUNNING_INBOUND_RE = re.compile(
    r"(?:(?:点外卖|美团下单|点餐|美团点|在美团点|用美团点|下单外卖|点个外卖|叫个外卖|叫外卖|再来一单|复购|原样下单|原样再来一单|原样点|再点一次|再买一次|按上次|照上次|老样子|(?:点|来|要|吃|下单)?(?:上一次|上次|上一个|上一单)|双拼点餐|跨店双拼|双拼外卖|双拼|两家一起点)"
    r"|(?:(?:昨天的?|前天的?|上次的?|老样子).{0,20}(?:加上|再加|多加|加一个|加个|加一份|加份|去掉|不要|减掉|大牌档|大排档|老乡鸡|火锅|米粉|炒饭))"
    r"|(?:(?:点|来|买|叫|下单)(?:[一二两三四五\d]?)(?:杯|份|碗|个|盒|桶|袋|套|串|只))"
    r"|(?:(?:确认|看看|选|点|就点|就吃|就选|那就|直接点|下单|复购)?第?\s*[1-5一二三四五]\s*(?:家|个|号|单))"
    r"|(?:美团推荐|外卖推荐|晚餐推荐|午餐推荐|夜宵推荐|吃什么|吃啥|吃点什么|吃点啥|点什么|点啥|今晚吃|历史菜单|常点菜单|之前买过|以前买过|以前点过|之前点过|以前吃过|之前吃过|不要新的|不想试新)"
    r"|推荐(?:点|个|下|一下|几家|几个)?(?:今天|今晚|晚上|中午|早上)?(?:饭|吃的|好吃的|吃啥|吃什么|外卖|晚饭|晚餐|午饭|午餐|夜宵|店)|还没吃饭|不知道吃啥|没想好吃)",
    re.IGNORECASE,
)
_VOICE_PREFIX_RE = re.compile(r"^\s*\[Voice\]\s*", re.IGNORECASE)
_STMT_TRAILING_PUNCT_RE = re.compile(r"[。！!~～\s]+$")
_QQ_INLINE_QUOTE_RE = re.compile(
    r"^\s*(?:\[Quoted message\]|\[引用消息\]|【引用消息】)\s*[:：]?\s*\r?\n([\s\S]+)$",
    re.IGNORECASE,
)


def _split_qq_quoted_message(text: str, reply_to_text: str = "") -> tuple[str, str]:
    """拆解 QQBot C2C 内联引用文本 `[Quoted message]:\\n<原文>\\n\\n<回复>`，修复引用销账与审批漏 LLM 缺陷。"""
    raw = (text or "").strip()
    existing = (reply_to_text or "").strip()
    if not raw:
        return "", existing
    m = _QQ_INLINE_QUOTE_RE.match(raw)
    if not m:
        return raw, existing
    body = m.group(1).strip()
    blank_splits = list(re.finditer(r"\r?\n[ \t]*\r?\n", body))
    if blank_splits:
        last_sep = blank_splits[-1]
        q_part = body[: last_sep.start()].strip()
        r_part = body[last_sep.end() :].strip()
        if q_part and r_part:
            eff_q = f"{existing}\n{q_part}".strip() if (existing and q_part not in existing) else (existing or q_part)
            return r_part, eff_q
    lines = [ln.rstrip() for ln in body.splitlines() if ln.strip()]
    if len(lines) >= 2 and len(lines[-1].strip()) <= 64:
        q_part = "\n".join(lines[:-1]).strip()
        r_part = lines[-1].strip()
        eff_q = f"{existing}\n{q_part}".strip() if (existing and q_part not in existing) else (existing or q_part)
        return r_part, eff_q
    return raw, existing

# /siming 斜杠命令严格白名单：仅放行纯只读查询子命令
_SLASH_READONLY_SPECS = {
    "today": {"flags": {"--full"}},
    "ask": {"max_args": 1, "flags": set()},
    "cases": {"flags": {"--all"}},
    "explain": {"max_args": 1, "flags": set()},
    "doctor": {"flags": {"--no-edge", "--json"}},
    "install-check": {"flags": {"--json", "--arrival", "--onsite"}},
}


def _load_qq_interactive() -> Any:
    try:
        from . import qq_interactive as mod  # type: ignore[import-not-found]

        return mod
    except ImportError:
        mod_path = Path(__file__).resolve().parent / "qq_interactive.py"
        if not mod_path.is_file():
            return None
        spec = importlib.util.spec_from_file_location("siming_bridge_qq_interactive", mod_path)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        return None


_QQ_INTERACTIVE = _load_qq_interactive()


def _load_jev_gate() -> Any:
    """加载纯标准库 `src/siming/jev_gate.py`，与司命主进程共享同一套 JEV 决策提示与校准阈值。"""
    candidates = [
        Path(__file__).resolve().parents[2] / "src" / "siming" / "jev_gate.py",
        Path.home() / "Work" / "siming" / "src" / "siming" / "jev_gate.py",
    ]
    for p in candidates:
        if p.is_file():
            try:
                spec = importlib.util.spec_from_file_location("siming_bridge_jev_gate", p)
                if spec and spec.loader:
                    mod = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(mod)
                    return mod
            except Exception:
                continue
    return None


_JEV_GATE = _load_jev_gate()


def _subprocess_env() -> dict[str, str]:
    home = Path.home()
    return {
        "HOME": str(home),
        "TMPDIR": str(home / ".cache" / "agent-tmp"),
        "PATH": f"{home / '.local/bin'}:{home / '.local/share/mise/command-wrappers/bin'}:/usr/local/bin:/usr/bin:/bin",
        "LANG": os.environ.get("LANG", "zh_CN.UTF-8"),
        "HTTP_PROXY": os.environ.get("HTTP_PROXY", "http://127.0.0.1:7890"),
        "HTTPS_PROXY": os.environ.get("HTTPS_PROXY", "http://127.0.0.1:7890"),
        "NO_PROXY": os.environ.get("NO_PROXY", "localhost,127.0.0.1,::1"),
    }


async def _run_siming_cli(
    args: list[str],
    timeout_sec: float = 25.0,
    stdin_bytes: Optional[bytes] = None,
) -> tuple[int, str]:
    if not Path(SIMING_BIN).is_file():
        return 1, ""
    proc = await asyncio.create_subprocess_exec(
        SIMING_BIN,
        *args,
        env=_subprocess_env(),
        stdin=asyncio.subprocess.PIPE if stdin_bytes is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(input=stdin_bytes), timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "司命命令执行超时"
    out = (stdout or stderr or b"").decode("utf-8", errors="replace").strip()
    return proc.returncode or 0, out


def _validate_readonly_slash_args(tokens: list[str]) -> Optional[list[str]]:
    if not tokens:
        return ["today"]
    sub = tokens[0].lower()
    rest = tokens[1:]
    if sub in ("今天", "晨札"):
        return ["today"]
    if sub in ("全文", "晨札全文"):
        return ["today", "--full"]
    if sub == "defer":
        if rest and rest[0] == "list" and all(a in ("--all",) for a in rest[1:]):
            return ["defer", *rest]
        return None
    if sub == "ledger":
        if rest == ["cursor"] or rest == ["report"] or rest == ["reimburse"]:
            return ["ledger", *rest]
        if len(rest) == 3 and rest[0] == "report" and rest[1] == "--month" and re.fullmatch(r"20\d{2}-\d{2}", rest[2]):
            return ["ledger", *rest]
        return None
    if sub == "price-watch":
        if not rest or rest == ["list"]:
            return ["price-watch", "list"]
        return None
    if sub == "module":
        if not rest or rest == ["ls"]:
            return ["module", "ls"]
        return None
    if sub == "resume":
        if len(rest) <= 1 and not any(t.startswith("-") for t in rest):
            return ["resume", *rest]
        return None
    if sub == "ops-check":
        if not rest or rest == ["--no-heal"]:
            return ["ops-check", "--no-heal"]
        return None
    spec = _SLASH_READONLY_SPECS.get(sub)
    if spec is None:
        return None
    if sub == "ask":
        if any(t.startswith("-") for t in rest):
            return None
        query_text = " ".join(rest).strip()
        return ["ask", query_text] if query_text else None
    allowed_flags = spec.get("flags", set())
    max_pos = int(spec.get("max_args", 0))
    pos_args: list[str] = []
    flag_args: list[str] = []
    for tok in rest:
        if tok.startswith("-"):
            if tok not in allowed_flags:
                return None
            flag_args.append(tok)
        else:
            pos_args.append(tok)
    if len(pos_args) > max_pos:
        return None
    return [sub, *flag_args, *pos_args]


def _on_pre_gateway_dispatch(**kwargs: Any) -> None:
    """只读记下即将进入鉴权的 MessageEvent 引用上下文与 QQ msg_id 时间戳，返回 None 不干预鉴权链路。"""
    event = kwargs.get("event")
    if event is None:
        return None
    msg_id = getattr(event, "message_id", None)
    if not msg_id:
        return None
    source = getattr(event, "source", None)
    if source is not None and _QQ_INTERACTIVE is not None:
        plat = getattr(getattr(source, "platform", None), "value", "") or str(getattr(source, "platform", "") or "")
        chat_id = str(getattr(source, "chat_id", "") or "")
        if plat == "qqbot" and chat_id:
            _QQ_INTERACTIVE.record_qq_inbound_msg(chat_id, str(msg_id))
    _EVENT_META_CACHE[str(msg_id)] = {
        "reply_to_id": str(getattr(event, "reply_to_message_id", "") or ""),
        "reply_to_text": str(getattr(event, "reply_to_text", "") or ""),
        "media_urls": list(getattr(event, "media_urls", []) or []),
    }
    while len(_EVENT_META_CACHE) > _MAX_CACHE:
        _EVENT_META_CACHE.popitem(last=False)
    return None


async def _deliver_inbound_result_to_qq(
    adapter: Any,
    chat_id: str,
    msg_id: str,
    rc: int,
    out: str,
    is_sensitive_cmd: bool,
) -> None:
    if _QQ_INTERACTIVE is None:
        return
    if rc != 0 or not out:
        msg = (
            f"⚠️ 司命确定性指令执行异常或超时（rc={rc}），已按安全策略拦截，未转交对话模型执行，请稍后重试。"
            if is_sensitive_cmd
            else f"⚠️ 司命后台执行异常或超时（rc={rc}），请稍后重试。"
        )
        await _QQ_INTERACTIVE._send_plain_no_auto_kb(adapter, chat_id, msg)
        return
    try:
        parsed = json.loads(out)
    except Exception:
        await _QQ_INTERACTIVE._send_plain_no_auto_kb(adapter, chat_id, "⚠️ 司命返回格式异常，已安全拦截。")
        return
    if isinstance(parsed, dict) and parsed.get("handled") and parsed.get("reply"):
        reply_text = str(parsed["reply"])
        kb = parsed.get("keyboard")
        if isinstance(kb, dict) and kb:
            try:
                res = await _QQ_INTERACTIVE.send_keyboard_safe(
                    adapter,
                    chat_id,
                    reply_text,
                    kb,
                    reply_to=msg_id or None,
                )
                if getattr(res, "success", False):
                    return
            except Exception:
                pass
        await _QQ_INTERACTIVE._send_plain_no_auto_kb(adapter, chat_id, reply_text)
        return
    if is_sensitive_cmd:
        await _QQ_INTERACTIVE._send_plain_no_auto_kb(
            adapter, chat_id, "⚠️ 该敏感口令未匹配当前待处理状态，已拦截且未转交对话模型。"
        )
        return
    # P1 修复 8s 接管失忆：若后台异步任务最终返回 handled=False，明确交还并告知用户，绝不黑洞吞没！
    await _QQ_INTERACTIVE._send_plain_no_auto_kb(
        adapter,
        chat_id,
        "↩️ 司命检查后确认该消息属于普通对话/未命中专项单据，已交还 Hermes（如仍需继续讨论可直接回复，不会丢失上下文）。",
    )


async def _await_and_deliver_bg(
    cli_task: "asyncio.Task[tuple[int, str]]",
    adapter: Any,
    chat_id: str,
    msg_id: str,
    is_sensitive_cmd: bool,
) -> None:
    try:
        rc, out = await cli_task
        await _deliver_inbound_result_to_qq(adapter, chat_id, msg_id, rc, out, is_sensitive_cmd)
    except Exception:
        if _QQ_INTERACTIVE is not None:
            try:
                await _QQ_INTERACTIVE._send_plain_no_auto_kb(
                    adapter, chat_id, "⚠️ 司命后台任务推送异常，请重新发起指令。"
                )
            except Exception:
                pass


async def _on_post_gateway_admission(**kwargs: Any) -> Optional[dict[str, Any]]:
    """鉴权通过后尝试交由司命确定性路由处理；对敏感口令执行 fail-closed 拦截，带按钮卡直出 QQ 原生确认框。"""
    platform = str(kwargs.get("platform") or "")
    if platform not in ("qqbot", "feishu", "telegram"):
        return None
    text = str(kwargs.get("text") or "").strip()
    msg_id = str(kwargs.get("message_id") or "")
    meta = _EVENT_META_CACHE.pop(msg_id, {}) if msg_id else {}

    reply_to_id = str(meta.get("reply_to_id") or "")
    reply_to_text = str(meta.get("reply_to_text") or "")
    media_urls: list[str] = list(meta.get("media_urls") or [])

    if not text and not media_urls:
        return None

    clean_text, effective_reply_to_text = _split_qq_quoted_message(text, reply_to_text)
    voice_stripped = _VOICE_PREFIX_RE.sub("", clean_text).strip()
    # 剥离语音转写末尾陈述标点（保留 ?/？ 防止问句误触发支付/审批）
    norm_text = _STMT_TRAILING_PUNCT_RE.sub("", voice_stripped).strip()

    norm_core = re.sub(r"(?:吧|呀|啊|哈|咯|啦|一下)$", "", norm_text).strip()
    is_quote_approval = bool(
        (reply_to_id or (effective_reply_to_text and re.search(r"\b(?:apply\s+)?(A\d{2,6}|BND-[A-Z0-9]+)\b", effective_reply_to_text, re.IGNORECASE)))
        and norm_core.lower() in ("apply", "确认", "合入", "同意", "支付", "付款", "取消", "放弃", "作废", "不点了", "不要了", "算了")
    )
    is_quote_bill_pwd = bool(
        (reply_to_id or (effective_reply_to_text and ("加密账单" in effective_reply_to_text or "解压码" in effective_reply_to_text)))
        and re.fullmatch(r"[A-Za-z0-9]{4,16}", norm_text)
    )
    is_sensitive_cmd = bool(_SENSITIVE_DETERMINISTIC_RE.search(norm_text)) or is_quote_approval or is_quote_bill_pwd
    local_media = [str(m) for m in media_urls[:3] if m and not str(m).startswith(("http://", "https://"))]

    # JEV (`typesafe/jev-1.13`) 前置过滤：
    # - 敏感确定性口令（apply/确认支付/派单/解压码/引用审批）严格 Tier-0 纯代码直通，永不依赖外部 API；
    # - 其余自然语言先由 JEV 过滤一遍是否应由司命接手：高置信 hermes_chat 直接交还通用对话（省去拉起 siming 子进程且零误劫持）；
    # - 若判定为 siming_meituan 则自动抬升 300s 长任务超时，并将 jev_decision 透传给 siming inbound 避免重复请求。
    jev_decision: Optional[dict[str, Any]] = None
    if not is_sensitive_cmd and _JEV_GATE is not None and clean_text:
        try:
            jev_decision = await asyncio.to_thread(
                _JEV_GATE.evaluate_siming_takeover,
                clean_text,
                reply_to_id=reply_to_id,
                reply_to_text=effective_reply_to_text,
                media=local_media,
            )
        except Exception:
            jev_decision = None
        if isinstance(jev_decision, dict) and jev_decision.get("should_siming_handle") is False:
            return None

    is_long_cmd = bool(_LONG_RUNNING_INBOUND_RE.search(norm_text)) or bool(
        isinstance(jev_decision, dict) and jev_decision.get("is_meituan")
    )

    # 改用 stdin JSON 传递入站载荷：彻底阻断 `ps aux` 暴露解压码/消息正文，并透传消息信封供 FrontDesk 持久化 owner/reply_epoch
    source = kwargs.get("source") if isinstance(kwargs.get("source"), dict) else {}
    chat_id = str(source.get("chat_id") or "")
    payload_obj: dict[str, Any] = {
        "text": clean_text,
        "message_id": msg_id,
        "channel": platform,
        "conversation": chat_id,
        "reply_to_id": reply_to_id,
        "reply_to_text": effective_reply_to_text,
        "media": local_media,
    }
    if isinstance(jev_decision, dict):
        payload_obj["jev_decision"] = jev_decision
    stdin_payload = json.dumps(payload_obj, ensure_ascii=False).encode("utf-8")

    inbound_timeout = 300.0 if (is_sensitive_cmd or is_long_cmd) else 60.0
    adapter = _QQ_INTERACTIVE.get_active_qq_adapter() if (platform == "qqbot" and _QQ_INTERACTIVE is not None) else None
    can_bg_qq = bool(platform == "qqbot" and adapter is not None and chat_id and _QQ_INTERACTIVE is not None)

    cli_task = asyncio.create_task(
        _run_siming_cli(["inbound", "--stdin-json"], timeout_sec=inbound_timeout, stdin_bytes=stdin_payload)
    )
    if can_bg_qq:
        try:
            rc, out = await asyncio.wait_for(asyncio.shield(cli_task), timeout=_HOOK_SYNC_WAIT_SEC)
        except asyncio.TimeoutError:
            # P1 修复：按实际命令类型给出诚实受理回执（不再把广义敏感口令错印成「正在提交美团订单」）
            is_pay_hint = bool(re.search(r"(?:确认支付|确认付款|去支付|立即支付|美团|点餐|外卖)", norm_text))
            if is_sensitive_cmd and is_pay_hint:
                progress_txt = "⏳ 已收到付款确认指令，正在后台提交订单并核验三态收据，完成后立即推送回执，请稍候…"
            elif is_sensitive_cmd:
                progress_txt = "⏳ 已收到司命确认/控制指令，正在后台执行并核验收据，完成后立即推送回执，请稍候…"
            elif is_long_cmd:
                progress_txt = "⏳ 正在后台为你实时加购并核验优惠，稍后直接弹出付款确认按钮，请稍候…"
            else:
                progress_txt = "⏳ 司命正在后台处理中，完成后立即推送结果（如不属司命单据将明确交还 Hermes），请稍候…"
            try:
                await _QQ_INTERACTIVE._send_plain_no_auto_kb(adapter, chat_id, progress_txt)
            except Exception:
                pass
            bg_task = asyncio.create_task(_await_and_deliver_bg(cli_task, adapter, chat_id, msg_id, is_sensitive_cmd))
            _BACKGROUND_TASKS.add(bg_task)
            bg_task.add_done_callback(_BACKGROUND_TASKS.discard)
            return {"action": "handled"}
    else:
        rc, out = await cli_task

    if rc != 0 or not out:
        if is_sensitive_cmd:
            return {
                "action": "handled",
                "reply": f"⚠️ 司命确定性指令执行异常或超时（rc={rc}），已按安全策略拦截，未转交对话模型执行，请稍后重试。",
            }
        return None
    try:
        parsed = json.loads(out)
    except Exception:
        if is_sensitive_cmd:
            return {"action": "handled", "reply": "⚠️ 司命返回格式异常，已安全拦截敏感口令。"}
        return None
    if isinstance(parsed, dict) and parsed.get("handled") and parsed.get("reply"):
        reply_text = str(parsed["reply"])
        kb = parsed.get("keyboard")
        if can_bg_qq and isinstance(kb, dict) and kb:
            try:
                res = await _QQ_INTERACTIVE.send_keyboard_safe(
                    adapter,
                    chat_id,
                    reply_text,
                    kb,
                    reply_to=msg_id or None,
                )
                if getattr(res, "success", False):
                    return {"action": "handled"}
            except Exception:
                pass
        return {"action": "handled", "reply": reply_text}
    if is_sensitive_cmd:
        return {"action": "handled", "reply": "⚠️ 该敏感口令未匹配当前待处理状态，已拦截且未转交对话模型。"}
    return None


async def _handle_slash_siming(raw_args: str) -> str:
    arg_str = raw_args.strip()
    try:
        tokens = shlex.split(arg_str) if arg_str else []
    except ValueError:
        tokens = arg_str.split()
    safe_args = _validate_readonly_slash_args(tokens)
    if safe_args is None:
        return (
            "🔒 `/siming` 仅开放只读查询子命令：`today [--full]`、`ask <问题>`、`cases [--all]`、"
            "`defer list`、`explain [编号]`、`install-check`、`ledger report|cursor`、`doctor`。\n"
            "审批合入请在对话中点击确认框按钮或发 `apply Axx`，修改单据请点按钮或对消息引用回复。"
        )
    _rc, out = await _run_siming_cli(safe_args, timeout_sec=25.0)
    return out or "暂无记录。"


def _qq_interactive_prompt_section(session_info: Any) -> str:
    plat = str((session_info or {}).get("platform") or "").strip().lower()
    if plat != "qqbot":
        return ""
    return (
        "## QQ 交互确认与司命兜底路由规范（必须遵守）\n"
        "- 当前会话在 QQ 中，已启用原生可点击确认框/选择框按钮。\n"
        "- 后续**所有需要用户人工明确确认**（是/否、确认执行、确认参数、确认提交/修改/下单等）"
        "或**人工做选择**（多选一、选方案、选商品/门店、选时间/策略等）的场景，"
        "**必须优先调用 `clarify` 工具**并把具体选项放入 `choices`（最多 4 项，推荐项放第 1 个），"
        "不要只输出纯文本让用户手动输入回复；`clarify` 会在 QQ 里渲染为原生可点击按钮卡片。\n"
        "- 若涉及司命（siming）A2 审批卡，直接输出含 `apply Axx` 编号的完整卡面即可，QQ 会自动挂载「确认 / 取消」原生按钮；绝不替用户执行 `siming apply`。\n"
        "- **美团点餐与自动支付闭环铁律（最高优先级）**：司命已内置完整的美团点餐+自动补必选品+自动勾最优红包+合规自动支付全闭环（`siming meituan --order \"<店名> <菜品>\" [--loc <地点>]`、`siming meituan --reorder \"<店名>\" [--add \"<加菜>\"] [--exclude \"<减菜>\"] [--loc <地点>]`、`siming meituan --history`、`siming meituan 晚餐`）。"
        "凡遇点餐、复购、复购加菜/减菜，**一律直接调用 `~/.local/bin/siming meituan ...`** 并原样把含 `apply Axx` 的完整确认卡输出到 QQ（`siming-bridge` 会自动挂载 `[💳 确认支付 Axx]` 按钮；用户点击后由司命纯代码提交订单并经 RedPill 机密支付通道自动完成收银台付款与实单核验）。"
        "**严禁绕开司命手搓 CDP 脚本或调用 `agy` 代点美团外卖**（会漏掉最优红包、失去支付按钮并破坏自动支付闭环），**也绝不要向用户声称「没有自动支付流程」**（仅在遇滑块/验证码/风控时才交接美团 App 待付款）。\n"
        "- 当用户以灵活口语描述本机个人助理需求（点餐/复购、叫车、查账/报销/回款、待办销账、延后提醒、日常照护打针/耗材、特斯拉备车/电量、新房送装/甲醛/售后护照、保价、模块开关、算力/订阅/NAS/拓竹/本机检索）而未命中快捷确定性直达时，"
        "优先直接调用 `~/.local/bin/siming` 对应子命令完成查询或签发 `apply Axx` 确认卡（如 `siming meituan --history/--reorder/--order`、`siming didi`、`siming ledger ask/cursor/receivables`、`siming defer add`、`siming done Sxx`、`siming care`、`siming tesla`、`siming install-check`、`siming movein`、`siming price-watch`、`siming module on|off|ls`、`siming ask`、`siming today [--full]`、`siming cases`、`siming sure/compute/subs/home-air/homelab/print3d/health/car/trip/social/graph`），"
        "切勿要求用户背诵固定口令格式，也不要绕开司命手搓脚本。"
    )


def _wire_qq_platform(native: Any, adapter: Any) -> None:
    if _QQ_INTERACTIVE is not None:
        _QQ_INTERACTIVE.install_qq_interactive(native, adapter, run_siming_cli=_run_siming_cli)


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", _on_pre_gateway_dispatch)
    ctx.register_hook("post_gateway_admission", _on_post_gateway_admission)
    if hasattr(ctx, "register_platform_handler"):
        ctx.register_platform_handler("qqbot", _wire_qq_platform)
    if hasattr(ctx, "register_system_prompt_section"):
        ctx.register_system_prompt_section("qq-interactive-confirm", _qq_interactive_prompt_section)
    ctx.register_command(
        "siming",
        handler=_handle_slash_siming,
        description="司命个人助理只读速查入口（today / ask / cases / defer list / explain / install-check / doctor）",
        args_hint="<只读子命令> [参数]",
    )
