"""QQ Bot 原生内嵌键盘确认框 / 选择框插件扩展（不侵入 hermes-agent 源码树，升级零冲突）。

经 Hermes 官方 `ctx.register_platform_handler("qqbot", install_qq_interactive)` 挂载到活体
`QQAdapter` 实例，补齐以下全部交互确认能力：
1. `QQAdapter.send_clarify` + `retire_clarify_card`：把 Hermes `clarify` 工具问题渲染为 QQ 原生可点选确认框/选择框；
2. `QQAdapter.send_slash_confirm`：把斜杠命令三选一确认渲染为 `[允许一次] [始终允许] [取消]` 按钮；
3. `QQAdapter.send_choice_picker`：把 `/reasoning`、`/fast` 等平铺选择渲染为原生按钮；
4. `QQAdapter.send` 审批卡自动挂载：凡单条 QQ 回复内含司命当前有效 `PENDING` 的 `apply Axx` / `BND-xxx`，自动挂上确认/取消按钮；
5. `INTERACTION_CREATE` 统一路由：严格鉴权后分发 `clarify:*`、`sc:*`、`cp:*`、`siming:*`（纯代码确定性执行，零 LLM 介入），其余透传原 `_default_interaction_dispatch`（保留 `approve:*` 与 `update_prompt:*`）。
"""

from __future__ import annotations

import asyncio
import collections
import hashlib
import itertools
import json
import logging
import os
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

logger = logging.getLogger("siming_bridge.qq_interactive")

_MAX_STATE_ENTRIES = 128
_FRESH_MSG_ID_WINDOW_SEC = 280.0
_NEGATIVE_CHOICE_RE = re.compile(
    r"(?:取消|拒绝|放弃|不用|不要|不点|算了|否|跳过|暂不|先不|cancel|deny|reject|skip|^no$)",
    re.IGNORECASE,
)
_APPLY_TOKEN_RE = re.compile(r"\bapply\s+(A\d{2,6})\b", re.IGNORECASE)
_BUNDLE_RE = re.compile(r"\b(BND-[A-Z0-9]{4,16})\b")
_RECOMMENDED_SUFFIX_RE = re.compile(r"\s*\(Recommended\)\s*$", re.IGNORECASE)

_APPROVAL_ACTION_LABELS: dict[str, tuple[str, str, str, str]] = {
    "meituan_submit_pay": ("💳 确认支付", "已确认支付", "🗑️ 取消订单", "已取消"),
    "meituan_lock_cart": ("🛒 确认加购", "已确认加购", "✗ 取消", "已取消"),
    "didi_create_order": ("🚕 确认叫车", "已确认叫车", "✗ 取消", "已取消"),
    "luckin_confirm_order": ("☕ 确认下单", "已确认下单", "✗ 取消", "已取消"),
    "jd_add_cart": ("🛒 确认加车", "已确认加车", "✗ 取消", "已取消"),
    "git_apply_worktree": ("✅ 确认合入", "已确认合入", "✗ 作废", "已作废"),
    "append_block_with_backup": ("✅ 确认写入", "已确认写入", "✗ 作废", "已作废"),
    "shift_expectation": ("✅ 确认重排", "已确认重排", "✗ 取消", "已取消"),
}

_ACTIVE_QQ_ADAPTER: Any = None
_LAST_MSG_TS: dict[str, tuple[str, float]] = {}
_SEEN_INTERACTION_IDS: "collections.OrderedDict[str, float]" = collections.OrderedDict()
_RUN_SIMING_CLI_FN: Optional[Callable[..., Awaitable[tuple[int, str]]]] = None


class DictKeyboard:
    """将标准 QQ InlineKeyboard 字典适配为 `QQAdapter.send_with_keyboard` 期望的 `.to_dict()` 接口。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def to_dict(self) -> dict[str, Any]:
        return self._payload


def get_active_qq_adapter() -> Any:
    return _ACTIVE_QQ_ADAPTER


def record_qq_inbound_msg(chat_id: str, msg_id: str) -> None:
    if chat_id and msg_id:
        _LAST_MSG_TS[str(chat_id)] = (str(msg_id), time.time())


def _fresh_reply_to(adapter: Any, chat_id: str, explicit_reply_to: Optional[str] = None) -> Optional[str]:
    if explicit_reply_to:
        return str(explicit_reply_to)
    rec = _LAST_MSG_TS.get(str(chat_id))
    if rec:
        mid, ts = rec
        if time.time() - ts <= _FRESH_MSG_ID_WINDOW_SEC:
            return mid
    # 回退到 adapter 自身缓存（可能刚启动尚未过 pre_gateway_dispatch）
    last_map = getattr(adapter, "_last_msg_id", None)
    if isinstance(last_map, dict) and chat_id in last_map and not rec:
        return str(last_map[chat_id])
    return None


def _trim_label(text: str, limit: int = 16) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clean) <= limit:
        return clean
    return clean[: max(1, limit - 1)].rstrip() + "…"


_SAFE_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,12}$")


def _normalize_group_id(group_id: str) -> str:
    raw = str(group_id or "").strip()
    if not raw:
        return "default"
    if _SAFE_GROUP_ID_RE.fullmatch(raw):
        return raw
    return "g_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def _make_btn(
    btn_id: str,
    label: str,
    data: str,
    *,
    visited_label: str = "",
    style: int = 1,
    group_id: str = "default",
    click_limit: int = 1,
) -> dict[str, Any]:
    lbl = _trim_label(label, limit=18)
    vis = _trim_label(visited_label or f"已点:{lbl}", limit=18)
    return {
        "id": str(btn_id)[:32],
        "render_data": {
            "label": lbl,
            "visited_label": vis,
            "style": 1 if int(style) == 1 else 0,
        },
        "action": {
            "type": 1,
            "data": str(data)[:128],
            "permission": {"type": 2},
            "click_limit": max(1, int(click_limit)),
        },
        "group_id": _normalize_group_id(group_id),
    }


def _wrap_rows(rows: list[list[dict[str, Any]]]) -> Optional[DictKeyboard]:
    out = [{"buttons": r[:5]} for r in rows[:5] if r]
    if not out:
        return None
    return DictKeyboard({"content": {"rows": out}})


def build_clarify_keyboard(clarify_id: str, choices: list[str]) -> Optional[DictKeyboard]:
    """为 `clarify` 选项构造 QQ 原生可点选按钮（最多 4 选项 + 末行「其他/跳过」，严格 ≤ 5 行）。"""
    if not choices:
        return None
    gid = f"cl_{clarify_id[:16]}"
    choice_btns: list[dict[str, Any]] = []
    max_lbl_len = 0
    for idx, raw_choice in enumerate(choices[:4]):
        text = str(raw_choice or "").strip()
        is_rec = bool(_RECOMMENDED_SUFFIX_RE.search(text))
        bare = _RECOMMENDED_SUFFIX_RE.sub("", text).strip() or text
        prefix = f"⭐ {idx + 1}." if is_rec else f"{idx + 1}."
        label = _trim_label(f"{prefix} {bare}", limit=16)
        max_lbl_len = max(max_lbl_len, len(label))
        style = 0 if _NEGATIVE_CHOICE_RE.search(bare) else 1
        choice_btns.append(
            _make_btn(
                f"c{idx}",
                label,
                f"clarify:{clarify_id}:{idx}",
                visited_label=f"已选 {idx + 1}",
                style=style,
                group_id=gid,
            )
        )

    rows: list[list[dict[str, Any]]] = []
    if len(choice_btns) <= 2 and max_lbl_len <= 12:
        rows.append(choice_btns)
    elif max_lbl_len <= 10:
        for i in range(0, len(choice_btns), 2):
            rows.append(choice_btns[i : i + 2])
    else:
        for btn in choice_btns:
            rows.append([btn])

    rows.append(
        [
            _make_btn(
                "other",
                "✏️ 其他（文字回复）",
                f"clarify:{clarify_id}:other",
                visited_label="请回复文字",
                style=0,
                group_id=gid,
            ),
            _make_btn(
                "skip",
                "⏭️ 跳过",
                f"clarify:{clarify_id}:skip",
                visited_label="已跳过",
                style=0,
                group_id=gid,
            ),
        ]
    )
    return _wrap_rows(rows)


def build_slash_confirm_keyboard(confirm_id: str) -> Optional[DictKeyboard]:
    gid = f"sc_{confirm_id[:16]}"
    return _wrap_rows(
        [
            [
                _make_btn("once", "✅ 允许一次", f"sc:{confirm_id}:once", visited_label="已允许一次", style=1, group_id=gid),
                _make_btn("always", "⭐ 始终允许", f"sc:{confirm_id}:always", visited_label="已始终允许", style=1, group_id=gid),
                _make_btn("cancel", "❌ 取消", f"sc:{confirm_id}:cancel", visited_label="已取消", style=0, group_id=gid),
            ]
        ]
    )


def build_choice_picker_keyboard(picker_id: int, choices: list[dict[str, Any]]) -> Optional[DictKeyboard]:
    gid = f"cp_{picker_id}"
    btns: list[dict[str, Any]] = []
    max_len = 0
    for idx, ch in enumerate(choices[:15]):
        raw_lbl = str(ch.get("label") or ch.get("value") or f"选项 {idx + 1}")
        lbl = f"✓ {raw_lbl}" if ch.get("is_current") else raw_lbl
        trimmed = _trim_label(lbl, limit=14)
        max_len = max(max_len, len(trimmed))
        btns.append(
            _make_btn(
                f"p{idx}",
                trimmed,
                f"cp:{picker_id}:{idx}",
                visited_label=f"已选:{_trim_label(raw_lbl, 8)}",
                style=1 if ch.get("is_current") or idx == 0 else 0,
                group_id=gid,
            )
        )
    if not btns:
        return None
    per_row = 3 if max_len <= 8 else (2 if max_len <= 13 else 1)
    rows = [btns[i : i + per_row] for i in range(0, len(btns), per_row)]
    return _wrap_rows(rows[:5])


def _lookup_pending_siming_approval_keyboard(content: str, db_path: Optional[Path] = None) -> Optional[DictKeyboard]:
    """极轻量只读检查：若 QQ 出站消息含司命当前未过期的 PENDING 审批口令，自动构建确认/取消按钮。"""
    if not content:
        return None
    tokens = sorted({m.upper() for m in _APPLY_TOKEN_RE.findall(content)})
    bundles = sorted({m.upper() for m in _BUNDLE_RE.findall(content)})
    if not tokens and not bundles:
        return None

    siming_db = db_path or (Path.home() / ".local" / "share" / "siming" / "siming.db")
    if not siming_db.is_file():
        return None

    now_local_dt = datetime.now().astimezone()
    rows: list[dict[str, Any]] = []
    try:
        conn = sqlite3.connect(f"file:{siming_db}?mode=ro", uri=True, timeout=0.5)
        conn.row_factory = sqlite3.Row
        try:
            if tokens:
                ph = ",".join("?" for _ in tokens)
                cur = conn.execute(
                    f"SELECT token, bundle_id, action, expires_at FROM approvals WHERE token IN ({ph}) AND status='PENDING'",
                    tuple(tokens),
                )
                for r in cur.fetchall():
                    if datetime.fromisoformat(str(r["expires_at"])) >= now_local_dt:
                        rows.append(dict(r))
            if bundles and not rows:
                ph = ",".join("?" for _ in bundles)
                cur = conn.execute(
                    f"SELECT token, bundle_id, action, expires_at FROM approvals WHERE bundle_id IN ({ph}) AND status='PENDING'",
                    tuple(bundles),
                )
                for r in cur.fetchall():
                    if datetime.fromisoformat(str(r["expires_at"])) >= now_local_dt:
                        rows.append(dict(r))
        finally:
            conn.close()
    except Exception:
        return None

    if not rows:
        return None

    # 聚合为唯一卡或捆绑组
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        bid = str(r.get("bundle_id") or "").strip().upper()
        groups.setdefault(f"b:{bid}" if bid else f"t:{r['token']}", []).append(r)

    if len(groups) > 1:
        gid = f"sm_m_{int(time.time()) % 100000}"
        btn_rows: list[list[dict[str, Any]]] = []
        for idx, grp in enumerate(list(groups.values())[:4]):
            r0 = grp[0]
            tok = str(r0["token"]).upper()
            btn_rows.append(
                [
                    _make_btn(f"a{idx}", f"✅ 执行 {tok}", f"siming:apply:{tok}", visited_label=f"已选 {tok}", style=1, group_id=gid),
                    _make_btn(f"c{idx}", f"✗ 取消 {tok}", f"siming:cancel:{tok}", visited_label=f"已取消 {tok}", style=0, group_id=gid),
                ]
            )
        return _wrap_rows(btn_rows)

    r0 = next(iter(groups.values()))[0]
    tok = str(r0["token"]).upper()
    bid = str(r0.get("bundle_id") or "").strip().upper()
    act = str(r0.get("action") or "")
    c_lbl, c_vis, x_lbl, x_vis = _APPROVAL_ACTION_LABELS.get(
        act, ("✅ 确认执行", "已确认", "✗ 取消", "已取消")
    )
    if bid:
        gid = f"sm_bnd_{bid[:16]}"
        return _wrap_rows(
            [
                [
                    _make_btn("confirm", c_lbl, f"siming:apply_bundle:{bid}", visited_label=c_vis, style=1, group_id=gid),
                    _make_btn("cancel", x_lbl, f"siming:cancel_bundle:{bid}", visited_label=x_vis, style=0, group_id=gid),
                ]
            ]
        )
    gid = f"sm_app_{tok}"
    return _wrap_rows(
        [
            [
                _make_btn("confirm", f"{c_lbl} {tok}", f"siming:apply:{tok}", visited_label=c_vis, style=1, group_id=gid),
                _make_btn("cancel", f"{x_lbl} {tok}", f"siming:cancel:{tok}", visited_label=x_vis, style=0, group_id=gid),
            ]
        ]
    )


async def send_keyboard_safe(
    adapter: Any,
    chat_id: str,
    content: str,
    keyboard: Any,
    *,
    reply_to: Optional[str] = None,
) -> Any:
    """用新鲜 `msg_id` 尝试 `adapter.send_with_keyboard`；若因旧 `msg_id` 过期/超次失败则自动无 `msg_id` 重试一次。"""
    kb_obj = DictKeyboard(keyboard) if isinstance(keyboard, dict) else keyboard
    eff_reply_to = _fresh_reply_to(adapter, chat_id, explicit_reply_to=reply_to)
    res = await adapter.send_with_keyboard(chat_id, content, kb_obj, reply_to=eff_reply_to)
    if getattr(res, "success", False) or not eff_reply_to:
        return res
    err_l = str(getattr(res, "error", "") or "").lower()
    if any(k in err_l for k in ("not connected", "unsupported", "not supported")):
        return res
    logger.debug("[QQInteractive] Retrying send_with_keyboard without stale reply_to (%s): %s", eff_reply_to, err_l[:120])
    return await adapter.send_with_keyboard(chat_id, content, kb_obj, reply_to=None)


def _trim_state_dict(store: dict[Any, Any], max_size: int = _MAX_STATE_ENTRIES) -> None:
    while len(store) > max_size:
        try:
            store.pop(next(iter(store)))
        except Exception:
            break


def _ensure_adapter_state(adapter: Any) -> None:
    if not hasattr(adapter, "_clarify_state"):
        adapter._clarify_state = {}
    if not hasattr(adapter, "_slash_confirm_state"):
        adapter._slash_confirm_state = {}
    if not hasattr(adapter, "_choice_picker_state"):
        adapter._choice_picker_state = {}
    if not hasattr(adapter, "_choice_picker_counter"):
        adapter._choice_picker_counter = itertools.count(1)


async def _qq_send_clarify(
    self: Any,
    chat_id: str,
    question: str,
    choices: Optional[list],
    clarify_id: str,
    session_key: str,
    metadata: Optional[dict[str, Any]] = None,
) -> Any:
    """将 Hermes `clarify` 工具问题渲染为 QQ 原生可点选内嵌键盘确认框 / 选择框。"""
    from gateway.platforms.base import BasePlatformAdapter

    if not choices:
        return await BasePlatformAdapter.send_clarify(
            self,
            chat_id=chat_id,
            question=question,
            choices=choices,
            clarify_id=clarify_id,
            session_key=session_key,
            metadata=metadata,
        )

    _ensure_adapter_state(self)
    choices_list = [str(c) for c in choices]
    is_multi = False
    try:
        from tools.clarify_gateway import get_pending, mark_awaiting_text

        entry = get_pending(clarify_id)
        if entry is not None and getattr(entry, "multi_select", False):
            is_multi = True
            # 多选既支持点按钮单选，也保持文本捕获支持回复如 "1,3"
            mark_awaiting_text(clarify_id)
    except Exception:
        pass

    q_lines = str(question or "").strip().splitlines()
    q_head = q_lines[0].strip() if q_lines else "请确认或选择："
    q_tail = [ln.strip() for ln in q_lines[1:] if ln.strip()]
    lines = [f"❓ **{q_head}**"]
    lines.extend(q_tail)
    lines.append("")
    for idx, ch in enumerate(choices_list, start=1):
        lines.append(f"{idx}. {ch}")
    if is_multi:
        lines.append("")
        lines.append("💡 可直接点击下方按钮选择，或回复多个序号（如 `1,3`）")
    body = "\n".join(lines)

    kb = build_clarify_keyboard(clarify_id, choices_list)
    if kb is None:
        return await BasePlatformAdapter.send_clarify(
            self,
            chat_id=chat_id,
            question=question,
            choices=choices,
            clarify_id=clarify_id,
            session_key=session_key,
            metadata=metadata,
        )

    self._clarify_state[clarify_id] = {
        "session_key": session_key,
        "chat_id": str(chat_id),
        "choices": choices_list,
        "question": str(question),
    }
    _trim_state_dict(self._clarify_state)
    res = await send_keyboard_safe(self, chat_id, body, kb)
    if not getattr(res, "success", False):
        self._clarify_state.pop(clarify_id, None)
    return res


async def _qq_retire_clarify_card(self: Any, clarify_id: str, notice: str) -> None:
    del notice  # QQ Bot API 不支持原位编辑消息，清理状态即可阻止过期点击
    state_map = getattr(self, "_clarify_state", None)
    if isinstance(state_map, dict):
        state_map.pop(clarify_id, None)


async def _qq_send_slash_confirm(
    self: Any,
    chat_id: str,
    title: str,
    message: str,
    session_key: str,
    confirm_id: str,
    metadata: Optional[dict[str, Any]] = None,
) -> Any:
    del metadata
    _ensure_adapter_state(self)
    body = f"🔐 **{title}**\n\n{message}" if title and title not in message else message
    kb = build_slash_confirm_keyboard(confirm_id)
    self._slash_confirm_state[confirm_id] = {
        "session_key": session_key,
        "chat_id": str(chat_id),
    }
    _trim_state_dict(self._slash_confirm_state)
    res = await send_keyboard_safe(self, chat_id, body, kb)
    if not getattr(res, "success", False):
        self._slash_confirm_state.pop(confirm_id, None)
    return res


async def _qq_send_choice_picker(
    self: Any,
    chat_id: str,
    title: str,
    choices: list,
    session_key: str,
    on_choice_selected: Callable[..., Awaitable[Any]],
    metadata: Optional[dict[str, Any]] = None,
) -> Any:
    from gateway.platforms.base import SendResult

    if not choices:
        return SendResult(success=False, error="No choices")
    _ensure_adapter_state(self)
    picker_id = next(self._choice_picker_counter)
    kb = build_choice_picker_keyboard(picker_id, choices)
    if kb is None:
        return SendResult(success=False, error="No choices")

    lines = [str(title or "请选择：").strip()]
    if "\n" not in lines[0]:
        lines.append("")
        for i, ch in enumerate(choices[:15], start=1):
            mark = "✓ " if ch.get("is_current") else ""
            lines.append(f"{i}. {mark}{ch.get('label') or ch.get('value') or ''}")
    body = "\n".join(lines)

    self._choice_picker_state[picker_id] = {
        "session_key": session_key,
        "chat_id": str(chat_id),
        "choices": list(choices),
        "on_choice_selected": on_choice_selected,
        "metadata": metadata,
    }
    _trim_state_dict(self._choice_picker_state)
    res = await send_keyboard_safe(self, chat_id, body, kb)
    if not getattr(res, "success", False):
        self._choice_picker_state.pop(picker_id, None)
    return res


async def _send_plain_no_auto_kb(adapter: Any, chat_id: str, content: str) -> Any:
    return await adapter.send(chat_id, content, metadata={"_skip_auto_keyboard": True})


def _event_chat_id(event: Any) -> str:
    return str(
        getattr(event, "group_openid", "")
        or getattr(event, "channel_id", "")
        or getattr(event, "user_openid", "")
        or getattr(event, "operator_openid", "")
        or ""
    ).strip()


def _is_siming_c2c_authorized(adapter: Any, event: Any) -> tuple[bool, str]:
    """司命 A2 / 动作按钮仅允许本人在私聊 C2C 点击，群聊一律 fail-closed 拒绝。"""
    scene = str(getattr(event, "scene", "") or "").lower()
    chat_type = int(getattr(event, "chat_type", -1) or 0)
    if scene not in ("c2c", "") and chat_type != 2:
        return False, ""
    if scene == "" and chat_type != 2:
        return False, ""
    operator = str(getattr(event, "operator_openid", "") or "").strip()
    user_openid = str(getattr(event, "user_openid", "") or operator).strip()
    if not operator or not user_openid or operator != user_openid:
        return False, ""
    allow_fn = getattr(adapter, "_is_dm_intake_allowed", None)
    if callable(allow_fn) and not allow_fn(operator):
        return False, ""
    return True, user_openid


async def _handle_clarify_interaction(adapter: Any, event: Any, button_data: str) -> None:
    parts = button_data.split(":", 2)
    if len(parts) != 3:
        return
    clarify_id, token = parts[1].strip(), parts[2].strip()
    _ensure_adapter_state(adapter)
    state = adapter._clarify_state.get(clarify_id)

    from tools import clarify_gateway

    entry = clarify_gateway.get_pending(clarify_id)
    session_key = (state or {}).get("session_key") or getattr(entry, "session_key", "")
    chat_id = (state or {}).get("chat_id") or _event_chat_id(event)

    if session_key and not adapter._is_authorized_interaction_for_session(event, session_key):
        logger.warning(
            "[QQInteractive] Rejected unauthorized clarify click id=%s operator=%s",
            clarify_id,
            getattr(event, "operator_openid", ""),
        )
        return

    if entry is None:
        adapter._clarify_state.pop(clarify_id, None)
        if chat_id:
            await _send_plain_no_auto_kb(adapter, chat_id, "⏱️ 该确认框已过期或已处理，请直接发新消息。")
        return

    if token == "other":
        flipped = clarify_gateway.mark_awaiting_text(clarify_id)
        if not flipped:
            adapter._clarify_state.pop(clarify_id, None)
            if chat_id:
                await _send_plain_no_auto_kb(adapter, chat_id, "⏱️ 该确认框已过期，请直接发新消息。")
            return
        if chat_id:
            await _send_plain_no_auto_kb(adapter, chat_id, "✏️ 请直接回复你的具体内容：")
        return

    if token == "skip":
        adapter._clarify_state.pop(clarify_id, None)
        resolved = clarify_gateway.resolve_gateway_clarify(clarify_id, clarify_gateway.SKIPPED)
        if resolved:
            resume = getattr(adapter, "resume_typing_for_chat", None)
            if callable(resume) and chat_id:
                resume(chat_id)
        elif chat_id:
            await _send_plain_no_auto_kb(adapter, chat_id, "⏱️ 该确认框已过期或已处理。")
        return

    if not token.isdigit():
        return
    idx = int(token)
    choices = list(entry.choices or (state or {}).get("choices") or [])
    if not (0 <= idx < len(choices)):
        return
    chosen_text = str(choices[idx])
    adapter._clarify_state.pop(clarify_id, None)
    resolved = clarify_gateway.resolve_gateway_clarify(clarify_id, chosen_text)
    if resolved:
        logger.info("[QQInteractive] Clarify resolved id=%s choice=%r", clarify_id, chosen_text[:60])
        resume = getattr(adapter, "resume_typing_for_chat", None)
        if callable(resume) and chat_id:
            resume(chat_id)
    elif chat_id:
        await _send_plain_no_auto_kb(adapter, chat_id, "⏱️ 该确认框已过期或已处理。")


async def _handle_slash_confirm_interaction(adapter: Any, event: Any, button_data: str) -> None:
    parts = button_data.split(":", 2)
    if len(parts) != 3:
        return
    confirm_id, choice = parts[1].strip(), parts[2].strip()
    if choice not in ("once", "always", "cancel"):
        return
    _ensure_adapter_state(adapter)
    state = adapter._slash_confirm_state.get(confirm_id)
    if not state:
        chat_id = _event_chat_id(event)
        if chat_id:
            await _send_plain_no_auto_kb(adapter, chat_id, "⏱️ 该命令确认框已处理或已过期。")
        return
    session_key = state["session_key"]
    chat_id = state["chat_id"] or _event_chat_id(event)
    if not adapter._is_authorized_interaction_for_session(event, session_key):
        logger.warning(
            "[QQInteractive] Rejected unauthorized slash_confirm click id=%s operator=%s",
            confirm_id,
            getattr(event, "operator_openid", ""),
        )
        return
    adapter._slash_confirm_state.pop(confirm_id, None)
    try:
        from tools import slash_confirm as _sc_mod

        result_text = await _sc_mod.resolve(session_key, confirm_id, choice)
        if result_text and chat_id:
            await _send_plain_no_auto_kb(adapter, chat_id, str(result_text))
    except Exception as exc:
        logger.error("[QQInteractive] slash_confirm resolve failed: %s", exc, exc_info=True)
        if chat_id:
            await _send_plain_no_auto_kb(adapter, chat_id, f"⚠️ 确认执行失败：{exc}")


async def _handle_choice_picker_interaction(adapter: Any, event: Any, button_data: str) -> None:
    parts = button_data.split(":", 2)
    if len(parts) != 3 or not parts[1].isdigit() or not parts[2].isdigit():
        return
    picker_id, idx = int(parts[1]), int(parts[2])
    _ensure_adapter_state(adapter)
    state = adapter._choice_picker_state.get(picker_id)
    if not state:
        return
    session_key = state["session_key"]
    chat_id = state["chat_id"] or _event_chat_id(event)
    if not adapter._is_authorized_interaction_for_session(event, session_key):
        logger.warning(
            "[QQInteractive] Rejected unauthorized choice_picker click id=%s operator=%s",
            picker_id,
            getattr(event, "operator_openid", ""),
        )
        return
    choices = state["choices"]
    if not (0 <= idx < len(choices)):
        return
    adapter._choice_picker_state.pop(picker_id, None)
    val = str(choices[idx].get("value") or choices[idx].get("label") or "")
    try:
        result_text = await state["on_choice_selected"](chat_id, val)
        if result_text and chat_id:
            await _send_plain_no_auto_kb(adapter, chat_id, str(result_text))
    except Exception as exc:
        logger.error("[QQInteractive] choice_picker callback failed: %s", exc, exc_info=True)
        if chat_id:
            await _send_plain_no_auto_kb(adapter, chat_id, f"⚠️ 切换选项失败：{exc}")


async def _handle_siming_interaction(adapter: Any, event: Any, button_data: str) -> None:
    ok_auth, chat_id = _is_siming_c2c_authorized(adapter, event)
    if not ok_auth:
        logger.warning(
            "[QQInteractive] Rejected unauthorized siming button click data=%r scene=%s operator=%s",
            button_data,
            getattr(event, "scene", ""),
            getattr(event, "operator_openid", ""),
        )
        return

    ev_id = str(getattr(event, "id", "") or "")
    if ev_id:
        if ev_id in _SEEN_INTERACTION_IDS:
            logger.debug("[QQInteractive] Duplicate siming interaction %s ignored", ev_id)
            return
        _SEEN_INTERACTION_IDS[ev_id] = time.time()
        while len(_SEEN_INTERACTION_IDS) > 256:
            _SEEN_INTERACTION_IDS.popitem(last=False)

    cli_runner = _RUN_SIMING_CLI_FN
    if cli_runner is None:
        await _send_plain_no_auto_kb(adapter, chat_id, "⚠️ 司命按钮执行器未就绪，请稍后重试。")
        return

    def _peek_pending_approval_action(tok: str) -> str:
        siming_db = Path.home() / ".local" / "share" / "siming" / "siming.db"
        if not siming_db.is_file():
            return ""
        try:
            conn = sqlite3.connect(f"file:{siming_db}?mode=ro", uri=True, timeout=0.5)
            try:
                row = conn.execute(
                    "SELECT action, status FROM approvals WHERE token=?",
                    (tok.upper(),),
                ).fetchone()
                if row and str(row[1] or "") == "PENDING":
                    return str(row[0] or "")
            finally:
                conn.close()
        except Exception:
            pass
        return ""

    is_long_action = button_data.startswith(
        (
            "siming:apply:",
            "siming:apply_bundle:",
            "siming:cmd:确认第",
            "siming:cmd:第",
            "siming:cmd:复购第",
            "siming:replenish_buy:",
        )
    )
    action_timeout = 300.0 if is_long_action else 60.0

    if button_data.startswith("siming:apply:"):
        tok_peek = button_data.split(":", 2)[2].strip().upper()
        if tok_peek and _peek_pending_approval_action(tok_peek) == "meituan_submit_pay":
            try:
                await _send_plain_no_auto_kb(
                    adapter,
                    chat_id,
                    f"⏳ [{tok_peek}] 已收到付款许可，正在提交美团订单并执行支付，请稍候…",
                )
            except Exception:
                pass
    elif button_data.startswith(("siming:cmd:复购第", "siming:cmd:确认第", "siming:cmd:第")):
        try:
            await _send_plain_no_auto_kb(
                adapter,
                chat_id,
                "⏳ 正在为你后台加购并核验美团结算页（自动补齐必选品、核对地址与匹配红包），请稍候…",
            )
        except Exception:
            pass
    elif button_data.startswith("siming:replenish_buy:"):
        try:
            await _send_plain_no_auto_kb(
                adapter,
                chat_id,
                "⏳ 收到补货指令，正在为你后台备单并核验实付价，请稍候…",
            )
        except Exception:
            pass

    stdin_payload = json.dumps({"data": button_data}, ensure_ascii=False).encode("utf-8")
    rc, out = await cli_runner(["inbound-action", "--stdin-json"], timeout_sec=action_timeout, stdin_bytes=stdin_payload)
    if rc != 0 or not out:
        await _send_plain_no_auto_kb(
            adapter,
            chat_id,
            f"⚠️ 司命确定性按钮执行异常或超时（rc={rc}），已按安全策略拦截，未转交对话模型。",
        )
        return

    try:
        parsed = json.loads(out)
    except Exception:
        await _send_plain_no_auto_kb(adapter, chat_id, "⚠️ 司命按钮回调返回格式异常，已安全拦截。")
        return

    reply = str(parsed.get("reply") or "").strip()
    kb = parsed.get("keyboard")
    if not reply:
        return
    if isinstance(kb, dict) and kb:
        res = await send_keyboard_safe(adapter, chat_id, reply, kb)
        if getattr(res, "success", False):
            return
    await _send_plain_no_auto_kb(adapter, chat_id, reply)


def install_qq_interactive(
    native: Any,
    adapter: Any,
    *,
    run_siming_cli: Optional[Callable[..., Awaitable[tuple[int, str]]]] = None,
) -> None:
    """将原生 QQ 确认框 / 选择框能力幂等挂载到 `QQAdapter` 类与当前活体实例。"""
    del native
    global _ACTIVE_QQ_ADAPTER, _RUN_SIMING_CLI_FN
    _ACTIVE_QQ_ADAPTER = adapter
    if run_siming_cli is not None:
        _RUN_SIMING_CLI_FN = run_siming_cli

    from gateway.platforms.base import BasePlatformAdapter

    cls = type(adapter)
    if cls.send_clarify is BasePlatformAdapter.send_clarify:
        cls.send_clarify = _qq_send_clarify
    if getattr(cls, "retire_clarify_card", None) is None:
        cls.retire_clarify_card = _qq_retire_clarify_card
    if cls.send_slash_confirm is BasePlatformAdapter.send_slash_confirm:
        cls.send_slash_confirm = _qq_send_slash_confirm
    if getattr(cls, "send_choice_picker", None) is None:
        cls.send_choice_picker = _qq_send_choice_picker

    # 包装 QQAdapter.send：单块回复含司命未过期 PENDING apply Axx 时自动附带确认/取消按钮
    if not getattr(cls, "_siming_auto_kb_send_wrapped", False):
        orig_send = cls.send

        async def _send_with_auto_approval_kb(
            self: Any,
            chat_id: str,
            content: str,
            reply_to: Optional[str] = None,
            metadata: Optional[dict[str, Any]] = None,
        ) -> Any:
            skip = bool((metadata or {}).get("_skip_auto_keyboard"))
            if (
                not skip
                and content
                and content.strip()
                and self._guess_chat_type(chat_id) != "guild"
                and len(self.format_message(content)) <= getattr(self, "MAX_MESSAGE_LENGTH", 4000)
            ):
                kb = _lookup_pending_siming_approval_keyboard(content)
                if kb is not None:
                    res = await send_keyboard_safe(self, chat_id, content, kb, reply_to=reply_to)
                    if getattr(res, "success", False):
                        return res
                    logger.debug(
                        "[QQInteractive] Auto-keyboard send failed (%s); falling back to plain text",
                        getattr(res, "error", ""),
                    )
            return await orig_send(self, chat_id, content, reply_to=reply_to, metadata=metadata)

        cls.send = _send_with_auto_approval_kb
        cls._siming_auto_kb_send_wrapped = True

    _ensure_adapter_state(adapter)

    if not getattr(adapter, "_siming_interaction_wrapped", False):
        prev_cb = getattr(adapter, "_interaction_callback", None)

        async def _dispatched_interaction(event: Any) -> None:
            data = str(getattr(event, "button_data", "") or "").strip()
            if data.startswith("clarify:"):
                await _handle_clarify_interaction(adapter, event, data)
                return
            if data.startswith("sc:"):
                await _handle_slash_confirm_interaction(adapter, event, data)
                return
            if data.startswith("cp:"):
                await _handle_choice_picker_interaction(adapter, event, data)
                return
            if data.startswith("siming:"):
                await _handle_siming_interaction(adapter, event, data)
                return
            if callable(prev_cb):
                await prev_cb(event)

        adapter.set_interaction_callback(_dispatched_interaction)
        adapter._siming_interaction_wrapped = True

    logger.info("[QQInteractive] Installed QQ interactive keyboards (clarify / slash_confirm / choice_picker / siming) on %s", cls.__name__)
