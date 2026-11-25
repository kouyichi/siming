"""QQ Bot 原生内嵌键盘（Inline Keyboard）确认框 / 选择框引擎。

为司命的 A2 审批卡、美团推荐与投机方案选择、事件链重排、日常照护/保价/取件操作与主动求证
生成标准 QQ Bot API v2 `keyboard` 载荷，并提供确定性按钮回调执行器与带安全降级的 REST 发送器。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Optional

from siming.db import SimingDB, now_local, parse_iso

API_BASE = "https://api.sgroup.qq.com"
TOKEN_URL = "https://bots.qq.com/app/getAppAccessToken"
MAX_ROWS = 5
MAX_BUTTONS_PER_ROW = 5
MAX_LABEL_CHARS = 18

# 动作类型 -> 确认/取消按钮文案
_APPROVAL_ACTION_LABELS: dict[str, tuple[str, str, str, str]] = {
    # action: (confirm_label, confirm_visited, cancel_label, cancel_visited)
    "meituan_submit_pay": ("💳 确认支付", "已确认支付", "🗑️ 取消订单", "已取消"),
    "meituan_lock_cart": ("🛒 确认加购", "已确认加购", "✗ 取消", "已取消"),
    "meituan_combo_order": ("🍱 确认双拼", "已确认双拼", "✗ 取消", "已取消"),
    "didi_create_order": ("🚕 确认叫车", "已确认叫车", "✗ 取消", "已取消"),
    "luckin_confirm_order": ("☕ 确认下单", "已确认下单", "✗ 取消", "已取消"),
    "jd_add_cart": ("🛒 确认加车", "已确认加车", "✗ 取消", "已取消"),
    "jd_replenish_spool": ("🧵 确认补耗材", "已确认补货", "✗ 取消", "已取消"),
    "git_apply_worktree": ("✅ 确认合入", "已确认合入", "✗ 作废", "已作废"),
    "append_block_with_backup": ("✅ 确认写入", "已确认写入", "✗ 作废", "已作废"),
    "shift_expectation": ("✅ 确认重排", "已确认重排", "✗ 取消", "已取消"),
    "compound_voice_batch": ("✅ 全部执行", "已全部执行", "✗ 取消", "已取消"),
    "commute_rescue_bundle": ("🚨 一键应急", "已触发应急", "✗ 取消", "已取消"),
    "overnight_next_step": ("🧪 确认推进", "已确认推进", "✗ 暂缓", "已暂缓"),
}

_APPROVAL_SHORT_NAMES: dict[str, str] = {
    "meituan_submit_pay": "美团支付",
    "meituan_lock_cart": "美团加购",
    "meituan_combo_order": "双拼点餐",
    "didi_create_order": "滴滴叫车",
    "luckin_confirm_order": "瑞幸点单",
    "jd_add_cart": "京东加车",
    "jd_replenish_spool": "耗材补货",
    "git_apply_worktree": "代码合入",
    "append_block_with_backup": "写入配置",
    "shift_expectation": "重排日程",
    "compound_voice_batch": "复合指令",
    "commute_rescue_bundle": "通勤应急",
    "overnight_next_step": "实验推进",
}

# 允许经按钮触发的确定性司命指令白名单（严禁透传任意自由命令）
_ALLOWED_CMD_RE = re.compile(
    r"^(?:第[1-5]家|确认第[1-5]家|复购第[1-5]单|历史菜单|看看方案|全文|今天|取消订单|送装清单|外箱初验|保价|打车状态|外卖推荐|晚餐推荐|待办|备车|关空调"
    r"|记下\s+W\d{2,6}|忽略\s+W\d{2,6}|恢复推送\s+[A-Za-z0-9_:\-.]{1,48})$"
)
_ALLOWED_CASE_ACTS = frozenset(
    {"打了", "跳过", "已约", "已开药", "已保价", "不看了", "取了", "拿了", "外箱完好", "已办", "取消", "少推"}
)
_TOKEN_RE = re.compile(r"\b(?:apply\s+)?(A\d{2,6})\b", re.IGNORECASE)
_APPLY_TOKEN_RE = re.compile(r"\bapply\s+(A\d{2,6})\b", re.IGNORECASE)
_BUNDLE_RE = re.compile(r"\b(BND-[A-Z0-9]{4,16})\b")
_SHOP_LINE_RE = re.compile(r"^\s*([1-5])\.\s*(?:[✅⏳🟡🛡️⚠️]\s*)?\*\*(.+?)\*\*", re.MULTILINE)


_SAFE_GROUP_ID_RE = re.compile(r"^[A-Za-z0-9_]{1,12}$")


def _trim_label(text: str, limit: int = MAX_LABEL_CHARS) -> str:
    clean = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(clean) <= limit:
        return clean
    return clean[: max(1, limit - 1)].rstrip() + "…"


def _normalize_group_id(group_id: str) -> str:
    """规范化 QQ Bot `rows.buttons.group_id`（严守平台短标识与 `[A-Za-z0-9_]` 字符集限制，防 400 整卡降级）。"""
    raw = str(group_id or "").strip()
    if not raw:
        return "default"
    if _SAFE_GROUP_ID_RE.fullmatch(raw):
        return raw
    return "g_" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


def _sanitize_keyboard_payload(keyboard: dict[str, Any]) -> dict[str, Any]:
    """发往 QQ Bot API 前统一净化每个按钮的 `group_id`，防止手写字典触发 400 无效参数。"""
    content = keyboard.get("content")
    if not isinstance(content, dict) or not isinstance(content.get("rows"), list):
        return keyboard
    out_rows: list[dict[str, Any]] = []
    for row in content["rows"]:
        if not isinstance(row, dict) or not isinstance(row.get("buttons"), list):
            continue
        out_btns: list[dict[str, Any]] = []
        for btn in row["buttons"]:
            if not isinstance(btn, dict):
                continue
            b = dict(btn)
            if "group_id" in b:
                b["group_id"] = _normalize_group_id(str(b.get("group_id") or ""))
            out_btns.append(b)
        if out_btns:
            out_rows.append({**row, "buttons": out_btns})
    return {**keyboard, "content": {**content, "rows": out_rows}}


def make_button(
    btn_id: str,
    label: str,
    data: str,
    *,
    visited_label: str = "",
    style: int = 1,
    group_id: str = "default",
    click_limit: int = 1,
) -> dict[str, Any]:
    """构造单个 QQ Bot InlineKeyboard 回调按钮（action.type=1 -> INTERACTION_CREATE）。"""
    lbl = _trim_label(label)
    vlbl = _trim_label(visited_label or f"已点: {lbl}")
    return {
        "id": str(btn_id)[:32],
        "render_data": {
            "label": lbl,
            "visited_label": vlbl,
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


def make_keyboard(rows: list[list[dict[str, Any]]]) -> Optional[dict[str, Any]]:
    """按 QQ Bot API 限制（最多 5 行、每行最多 5 按钮）封装 `InlineKeyboard` 字典。"""
    cleaned_rows: list[dict[str, Any]] = []
    for row in rows[:MAX_ROWS]:
        btns = [b for b in row[:MAX_BUTTONS_PER_ROW] if isinstance(b, dict)]
        if btns:
            cleaned_rows.append({"buttons": btns})
    if not cleaned_rows:
        return None
    return {"content": {"rows": cleaned_rows}}


def build_approval_card_keyboard(
    token: str,
    action: str = "",
    bundle_id: str = "",
) -> Optional[dict[str, Any]]:
    """为单张 A2 审批卡或整包捆绑审批卡构造 [确认] [取消] 按钮行。"""
    tok = (token or "").strip().upper()
    bid = (bundle_id or "").strip().upper()
    if not tok and not bid:
        return None
    c_lbl, c_vis, x_lbl, x_vis = _APPROVAL_ACTION_LABELS.get(
        action, ("✅ 确认执行", "已确认", "✗ 取消", "已取消")
    )
    if bid:
        gid = f"sm_bnd_{bid[:16]}"
        confirm_btn = make_button(
            "confirm",
            c_lbl,
            f"siming:apply_bundle:{bid}",
            visited_label=c_vis,
            style=1,
            group_id=gid,
        )
        cancel_btn = make_button(
            "cancel",
            x_lbl,
            f"siming:cancel_bundle:{bid}",
            visited_label=x_vis,
            style=0,
            group_id=gid,
        )
    else:
        gid = f"sm_app_{tok}"
        confirm_btn = make_button(
            "confirm",
            f"{c_lbl} {tok}",
            f"siming:apply:{tok}",
            visited_label=c_vis,
            style=1,
            group_id=gid,
        )
        cancel_btn = make_button(
            "cancel",
            f"{x_lbl} {tok}",
            f"siming:cancel:{tok}",
            visited_label=x_vis,
            style=0,
            group_id=gid,
        )
    return make_keyboard([[confirm_btn, cancel_btn]])


def build_multi_approval_keyboard(rows: list[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """同时存在多张待确认审批卡时，逐卡提供点选执行按钮，彻底消除误触歧义。"""
    if not rows:
        return None
    gid = f"sm_multi_{int(time.time()) % 100000}"
    btn_rows: list[list[dict[str, Any]]] = []
    for idx, r in enumerate(rows[:4]):
        tok = str(r.get("token") or "").strip().upper()
        act = str(r.get("action") or "")
        short = _APPROVAL_SHORT_NAMES.get(act, act or "执行")
        if not tok:
            continue
        btn_rows.append(
            [
                make_button(
                    f"app_{idx}",
                    f"✅ {tok} · {short}",
                    f"siming:apply:{tok}",
                    visited_label=f"已选 {tok}",
                    style=1,
                    group_id=gid,
                ),
                make_button(
                    f"can_{idx}",
                    f"✗ 取消 {tok}",
                    f"siming:cancel:{tok}",
                    visited_label=f"已取消 {tok}",
                    style=0,
                    group_id=gid,
                ),
            ]
        )
    if not btn_rows:
        return None
    btn_rows.append(
        [make_button("noop", "暂不处理", "siming:noop", visited_label="已暂缓", style=0, group_id=gid)]
    )
    return make_keyboard(btn_rows)


def build_meituan_recommend_keyboard(
    shops: list[tuple[int, str]],
    *,
    direct_confirm: bool = False,
) -> Optional[dict[str, Any]]:
    """为美团推荐列表 / 投机推演预案生成可点选的门店按钮。

    - `direct_confirm=False`（推荐初选）：点门店 -> `第N家`（展示该店详情/确认卡，防误下单）+ `看看方案`
    - `direct_confirm=True`（推演预案已列全菜品实价）：点门店 -> `确认第N家`（直达加购并出付款确认卡）
    """
    if not shops:
        return None
    gid = f"sm_mt_{int(time.time()) % 100000}"
    cmd_prefix = "确认第" if direct_confirm else "第"
    label_prefix = "✅ " if direct_confirm else ""
    buttons: list[dict[str, Any]] = []
    for idx, name in shops[:5]:
        short_name = re.sub(r"[（(].*?[)）]", "", str(name)).strip() or str(name)
        btn_lbl = _trim_label(f"{label_prefix}{idx}.{short_name}", limit=13)
        buttons.append(
            make_button(
                f"mt_{idx}",
                btn_lbl,
                f"siming:cmd:{cmd_prefix}{idx}家",
                visited_label=f"已选第{idx}家",
                style=1,
                group_id=gid,
            )
        )
    rows: list[list[dict[str, Any]]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])
    if not direct_confirm:
        rows.append(
            [
                make_button(
                    "plans",
                    "📋 查看各家推演方案",
                    "siming:cmd:看看方案",
                    visited_label="正在查看方案",
                    style=0,
                    group_id=gid,
                ),
                make_button(
                    "hist",
                    "🧾 历史常点菜单",
                    "siming:cmd:历史菜单",
                    visited_label="正在查历史单",
                    style=0,
                    group_id=gid,
                ),
            ]
        )
    return make_keyboard(rows)


def build_meituan_history_keyboard(shops: list[tuple[int, str]]) -> Optional[dict[str, Any]]:
    """为美团历史常点整单列表生成一键复购按钮（点选即原样加购并签发付款许可确认卡）。"""
    if not shops:
        return None
    gid = f"sm_mth_{int(time.time()) % 100000}"
    buttons: list[dict[str, Any]] = []
    for idx, name in shops[:5]:
        short_name = re.sub(r"[（(].*?[)）]", "", str(name)).strip() or str(name)
        btn_lbl = _trim_label(f"🔁 {idx}.{short_name}", limit=13)
        buttons.append(
            make_button(
                f"mth_{idx}",
                btn_lbl,
                f"siming:cmd:复购第{idx}单",
                visited_label=f"正在备第{idx}单",
                style=1,
                group_id=gid,
            )
        )
    rows: list[list[dict[str, Any]]] = []
    for i in range(0, len(buttons), 2):
        rows.append(buttons[i : i + 2])
    return make_keyboard(rows)


def build_case_action_keyboard(
    target_id: str,
    *,
    category: str = "",
    rule_id: str = "",
    sub_meta: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """为明确需要人工确认/操作反馈的司命单据或主动求证构建快捷确认按钮。"""
    tid = (target_id or "").strip()
    cat = (category or "").strip().lower()
    rid = (rule_id or "").strip().lower()
    gid = f"sm_act_{(tid or rid or 'item')[:18]}"

    if cat == "med_injection" or "inject" in rid:
        if not tid:
            return None
        return make_keyboard(
            [
                [
                    make_button("done", "💉 这针已打", f"siming:case_act:{tid}:打了", visited_label="已登记打针", style=1, group_id=gid),
                    make_button("skip", "⏭️ 本周跳过", f"siming:case_act:{tid}:跳过", visited_label="本周已跳过", style=0, group_id=gid),
                ]
            ]
        )
    if cat == "med_refill" or "refill" in rid:
        if not tid:
            return None
        return make_keyboard(
            [
                [
                    make_button("book", "📅 已约挂号", f"siming:case_act:{tid}:已约", visited_label="已登记挂号", style=1, group_id=gid),
                    make_button("done", "💊 已开药", f"siming:case_act:{tid}:已开药", visited_label="已登记开药", style=1, group_id=gid),
                ]
            ]
        )
    if cat == "price_protect" or rid == "price_protect_watch":
        if not tid:
            return None
        return make_keyboard(
            [
                [
                    make_button("claim", "💰 已申请保价", f"siming:case_act:{tid}:已保价", visited_label="已登记保价", style=1, group_id=gid),
                    make_button("mute", "🔕 不看了", f"siming:case_act:{tid}:不看了", visited_label="已静音", style=0, group_id=gid),
                ]
            ]
        )
    if rid in ("pickup_1800", "spatial_pickup") or (sub_meta and sub_meta.get("batch_case_ids")):
        all_ids = [
            str(x).strip().upper()
            for x in ((sub_meta or {}).get("all_case_ids") or (sub_meta or {}).get("batch_case_ids") or [])
            if re.fullmatch(r"S\d{2,6}", str(x).strip().upper())
        ]
        # 去重保序
        uniq_ids = list(dict.fromkeys(all_ids))
        eff_tid = tid if re.fullmatch(r"[SW]\d{2,6}", tid) else (uniq_ids[0] if uniq_ids else "")
        if eff_tid:
            btn_rows: list[list[dict[str, Any]]] = []
            if len(uniq_ids) >= 2:
                per_btns = [
                    make_button(
                        f"done_{idx}",
                        f"✅ 已取 {cid}",
                        f"siming:case_act:{cid}:已办",
                        visited_label=f"已销 {cid}",
                        style=0,
                        group_id=f"{gid}_{cid}",
                    )
                    for idx, cid in enumerate(uniq_ids[:4])
                ]
                btn_rows.append(per_btns)
            btn_rows.append(
                [
                    make_button(
                        "pick",
                        "📦 全部已取" if len(uniq_ids) >= 2 else f"📦 已取 {eff_tid}",
                        f"siming:case_act:{eff_tid}:取了",
                        visited_label="已销账",
                        style=1,
                        group_id=gid,
                    )
                ]
            )
            return make_keyboard(btn_rows)
    if rid == "commitment_drop" and tid.startswith("W"):
        btns: list[dict[str, Any]] = []
        if (sub_meta or {}).get("needs_destination"):
            btns.append(
                make_button(
                    "trip_local",
                    "🏠 就在家/线上",
                    f"siming:trip_local:{tid}",
                    visited_label="已标在家/线上",
                    style=1,
                    group_id=gid,
                )
            )
        btns.append(
            make_button(
                "cancel_wait",
                f"✗ 取消提醒 {tid}",
                f"siming:case_act:{tid}:取消",
                visited_label=f"已取消 {tid}",
                style=0,
                group_id=gid,
            )
        )
        return make_keyboard([btns])
    if rid == "curiosity_ask" or tid.startswith("Q-"):
        qid = tid or "latest"
        return make_keyboard(
            [
                [
                    make_button("ignore", "⏭️ 临时路过（忽略）", f"siming:curiosity_ignore:{qid}", visited_label="已忽略", style=0, group_id=gid),
                ]
            ]
        )
    if rid == "overtime_meal":
        return make_keyboard(
            [
                [
                    make_button("meal_rec", "🍱 看看晚餐推荐", "siming:cmd:晚餐推荐", visited_label="正在推荐晚餐", style=1, group_id=gid),
                    make_button("meal_plans", "📋 查看推演方案", "siming:cmd:看看方案", visited_label="正在查看方案", style=0, group_id=gid),
                ]
            ]
        )
    if cat == "food_pickup" or rid == "meituan_cabinet":
        if tid and re.fullmatch(r"S\d{2,6}", tid):
            return make_keyboard(
                [
                    [
                        make_button("take", f"🍱 已取餐 {tid}", f"siming:case_act:{tid}:拿了", visited_label=f"已销 {tid}", style=1, group_id=gid),
                    ]
                ]
            )
    if cat == "appliance" or rid == "buy_track_watch":
        btns: list[dict[str, Any]] = [
            make_button("inst_list", "📋 12.10 送装清单", "siming:cmd:送装清单", visited_label="已查送装清单", style=1, group_id=gid),
            make_button("arr_chk", "📦 外箱初验单", "siming:cmd:外箱初验", visited_label="已查初验单", style=0, group_id=gid),
        ]
        if tid and re.fullmatch(r"S\d{2,6}", tid):
            btns.append(
                make_button("done", f"✅ 已办 {tid}", f"siming:case_act:{tid}:已办", visited_label=f"已销 {tid}", style=0, group_id=gid)
            )
        return make_keyboard([btns])
    if rid == "tesla_low_soc_skip" or tid.startswith("low_soc_skip:"):
        return make_keyboard(
            [
                [
                    make_button("precond_on", "🔥 电量够，立即备车", "siming:cmd:备车", visited_label="正在开启备车", style=1, group_id=gid),
                    make_button("precond_skip", "⏭️ 暂不开空调", "siming:noop", visited_label="已跳过", style=0, group_id=gid),
                ]
            ]
        )
    if rid == "tesla_climate_started":
        return make_keyboard(
            [
                [
                    make_button("precond_off", "🛑 关闭空调", "siming:cmd:关空调", visited_label="已关闭空调", style=0, group_id=gid),
                ]
            ]
        )
    if rid == "morning_brief" or tid.startswith("brief:"):
        return make_keyboard(
            [
                [
                    make_button("full", "📄 展开晨札全文", "siming:cmd:全文", visited_label="已展开全文", style=1, group_id=gid),
                    make_button("todo", "📌 待办快捷销账", "siming:cmd:待办", visited_label="已查待办", style=0, group_id=gid),
                ]
            ]
        )
    if rid in ("sub_assassin", "sub_stoploss") or tid.startswith("SUB-"):
        sub_id = tid or str((sub_meta or {}).get("sub_id") or "")
        if sub_id:
            return make_keyboard(
                [
                    [
                        make_button("sub_keep", "✅ 保留", f"siming:sub_act:{sub_id}:KEEP", visited_label="已保留", style=0, group_id=gid),
                        make_button("sub_can", "✂️ 已去取消", f"siming:sub_act:{sub_id}:CANCELLED", visited_label="已取消", style=1, group_id=gid),
                        make_button("sub_zmb", "🧟 记为僵尸", f"siming:sub_act:{sub_id}:ZOMBIE", visited_label="已标僵尸", style=0, group_id=gid),
                    ],
                    [
                        make_button("sub_stop", "⏹️ 已停扣（未在进行）", f"siming:sub_ongoing:{sub_id}:0", visited_label="已标停扣", style=0, group_id=gid),
                    ],
                ]
            )
    if rid in ("meal_combo_guard", "meal_guard"):
        return make_keyboard(
            [
                [
                    make_button("m_rec", "🍱 看看外卖推荐", "siming:cmd:外卖推荐", visited_label="正在推荐", style=1, group_id=gid),
                    make_button("m_plan", "📋 查看推演方案", "siming:cmd:看看方案", visited_label="正在查看", style=0, group_id=gid),
                ]
            ]
        )
    return None


def _lookup_pending_approvals_in_text(db: SimingDB, text: str) -> list[dict[str, Any]]:
    """只读检索正文里提及且当前仍有效（PENDING 且未过期）的司命审批单。"""
    tokens = sorted({m.upper() for m in _APPLY_TOKEN_RE.findall(text or "")})
    bundles = sorted({m.upper() for m in _BUNDLE_RE.findall(text or "")})
    # 多卡消歧场景可能写作 `A01`(action) / `A02`(action) 而不带 apply 前缀
    if not tokens and ("待确认卡" in (text or "") or "确认卡" in (text or "")):
        tokens = sorted({m.upper() for m in _TOKEN_RE.findall(text or "")})
    if not tokens and not bundles:
        return []
    now = now_local()
    conn = db.connect()
    try:
        rows: list[dict[str, Any]] = []
        if tokens:
            ph = ",".join("?" for _ in tokens)
            for r in conn.execute(
                f"SELECT token, bundle_id, action, target_id, status, expires_at FROM approvals WHERE token IN ({ph}) AND status='PENDING'",
                tuple(tokens),
            ).fetchall():
                if parse_iso(str(r["expires_at"])) >= now:
                    rows.append(dict(r))
        if bundles and not rows:
            ph = ",".join("?" for _ in bundles)
            for r in conn.execute(
                f"SELECT token, bundle_id, action, target_id, status, expires_at FROM approvals WHERE bundle_id IN ({ph}) AND status='PENDING'",
                tuple(bundles),
            ).fetchall():
                if parse_iso(str(r["expires_at"])) >= now:
                    rows.append(dict(r))
        return rows
    except Exception:
        return []
    finally:
        conn.close()


def infer_keyboard_for_message(
    db: SimingDB,
    text: str,
    *,
    ref_id: str = "",
    rule_id: str = "",
    sub_meta: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """根据消息正文与数据库实态自动推断匹配的 QQ 原生确认框 / 选择框；普通纯通知返回 None。"""
    body = (text or "").strip()
    if not body:
        return None

    # 1. 优先匹配有效 PENDING A2 审批卡（单卡 / 捆绑包 / 多卡消歧）
    pending_apps = _lookup_pending_approvals_in_text(db, body)
    if pending_apps:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in pending_apps:
            bid = str(row.get("bundle_id") or "").strip().upper()
            gkey = f"bundle:{bid}" if bid else f"tok:{row['token']}"
            groups.setdefault(gkey, []).append(row)
        if len(groups) > 1:
            return build_multi_approval_keyboard([grp[0] for grp in groups.values()])
        first = next(iter(groups.values()))[0]
        return build_approval_card_keyboard(
            token=str(first["token"]),
            action=str(first.get("action") or ""),
            bundle_id=str(first.get("bundle_id") or ""),
        )

    # 2. 美团投机推演预案列表（已列出具体菜品与价格 -> 直接选择门店加购）
    if "美团投机推演菜品与券后价预案" in body:
        shops = [(int(m.group(1)), m.group(2).strip()) for m in _SHOP_LINE_RE.finditer(body)]
        if shops:
            return build_meituan_recommend_keyboard(shops, direct_confirm=True)

    # 2.5 美团历史常点整单列表 -> 一键原样复购（自动加购并出付款确认卡）
    if "美团历史常点整单" in body:
        shops = [(int(m.group(1)), m.group(2).strip()) for m in _SHOP_LINE_RE.finditer(body)]
        if shops:
            return build_meituan_history_keyboard(shops)

    # 3. 美团附近推荐列表 -> 选择门店查看确认卡或看推演方案
    if "看看方案" in body and ("确认第1家" in body or "第1家" in body):
        shops = [(int(m.group(1)), m.group(2).strip()) for m in _SHOP_LINE_RE.finditer(body)]
        if shops:
            return build_meituan_recommend_keyboard(shops, direct_confirm=False)

    # 3.5 日用品补货到期提醒卡 `[RPL:rpl_xxx]` -> 自动挂接一键直接买 / 已买 / 延后 / 暂停按钮
    m_rpl = re.search(r"\[`?RPL:([A-Za-z0-9_-]{3,40})`?\]", body)
    if m_rpl:
        try:
            from siming.replenish_cart import _find_item, _load_replenish_items, build_replenish_reminder_keyboard

            it_rpl = _find_item(_load_replenish_items(db), m_rpl.group(1))
            if it_rpl:
                return build_replenish_reminder_keyboard(it_rpl)
        except Exception:
            pass

    # 4. 明确需要操作确认的待办/提醒（打针/开药/保价/顺路取件/外卖柜/家电/主动求证/晨札全文）
    cat = ""
    tid = ref_id.strip()
    # 兼容 S43:nudge1 / S43:2026-11-19 这类带后缀的 ref_id，提取真实单号 Sxx
    m_base_id = re.match(r"^([SW]\d{2,6})(?::.+)?$", tid, re.IGNORECASE)
    if m_base_id:
        tid = m_base_id.group(1).upper()
    if not tid or not re.fullmatch(r"(?:[SW]\d{2,6}|Q-[0-9a-fA-F]{6,12}|brief:.+)", tid):
        batch_first = ""
        if sub_meta:
            for cand in (sub_meta.get("batch_case_ids") or []) + (sub_meta.get("all_case_ids") or []):
                c_up = str(cand).strip().upper()
                if re.fullmatch(r"[SW]\d{2,6}", c_up):
                    batch_first = c_up
                    break
        if batch_first:
            tid = batch_first
        else:
            m_id = re.search(r"\[(S\d{2,6}|Q-[0-9a-fA-F]{6,12})\]", body)
            if m_id:
                tid = m_id.group(1)
    if tid.startswith("S"):
        conn = db.connect()
        try:
            row = conn.execute("SELECT category, status FROM cases WHERE case_id=?", (tid,)).fetchone()
            if row:
                if str(row["status"] or "") in ("完成", "取消"):
                    return None
                cat = str(row["category"] or "")
        except Exception:
            cat = ""
        finally:
            conn.close()
    if not rule_id and "司命晨札" in body and "全文" in body:
        rule_id = "morning_brief"
    if not rule_id and "临时路过回" in body and "记住地点" in body:
        rule_id = "curiosity_ask"
    if not rule_id and "已从截图识别约定" in body:
        rule_id = "commitment_drop"
        if not tid:
            m_wid = re.search(r"\[(W\d{2,6})\]", body)
            if m_wid:
                tid = m_wid.group(1)
    if "自动备车已跳过省电" in body:
        rule_id = "tesla_low_soc_skip"
    elif "Model Y" in body and "关空调" in body and ("已开启" in body or "已按指令开启" in body):
        rule_id = "tesla_climate_started"
    eff_meta = dict(sub_meta or {})
    if rule_id == "commitment_drop" and "这次去哪里" in body and "needs_destination" not in eff_meta:
        eff_meta["needs_destination"] = True
    return build_case_action_keyboard(tid, category=cat, rule_id=rule_id, sub_meta=eff_meta)


def handle_qq_button_action(db: SimingDB, button_data: str) -> dict[str, Any]:
    """纯代码确定性执行 QQ `siming:*` 按钮回调（零 LLM 参与），返回 `{ok, reply, keyboard}`。"""
    from siming.approval import (
        cancel_approval,
        cancel_approval_bundle,
        resolve_and_apply_natural_confirm,
        verify_and_apply_approval,
    )
    from siming.curiosity import dismiss_curiosity_question
    from siming.router import _handle_subscribed_reply, handle_inbound_qq

    db.init_db()
    raw = (button_data or "").strip()
    if not raw.startswith("siming:"):
        return {"ok": False, "reply": "⚠️ 未知司命按钮指令。", "keyboard": None}

    parts = raw.split(":", 2)
    kind = parts[1] if len(parts) >= 2 else ""
    payload = parts[2] if len(parts) >= 3 else ""
    if kind and kind != "noop":
        db.record_human_use("qq_button", kind, ref_id=payload[:40])

    if kind == "noop":
        return {"ok": True, "reply": "👍 已取消，未执行任何操作。", "keyboard": None}

    if kind == "probe_ok":
        return {
            "ok": True,
            "reply": "✅ 收到按钮点击！QQ 原生确认框与确定性回调通道已全链路畅通。",
            "keyboard": None,
        }

    if kind == "apply":
        tok = payload.strip().upper()
        if not re.fullmatch(r"A\d{2,6}", tok):
            return {"ok": False, "reply": f"⚠️ 非法审批口令格式：{tok}", "keyboard": None}
        res = verify_and_apply_approval(db, tok)
        if res.get("ok"):
            reply = f"✅ [{tok}] {res.get('summary') or '已执行'}"
            kb = infer_keyboard_for_message(db, reply)
            return {"ok": True, "reply": reply, "keyboard": kb}
        return {"ok": False, "reply": f"❌ {res.get('error') or '执行失败'}", "keyboard": None}

    if kind == "cancel":
        tok = payload.strip().upper()
        if not re.fullmatch(r"A\d{2,6}", tok):
            return {"ok": False, "reply": f"⚠️ 非法审批口令格式：{tok}", "keyboard": None}
        c_res = cancel_approval(db, tok)
        return {"ok": bool(c_res.get("ok")), "reply": str(c_res.get("reply") or ""), "keyboard": None}

    if kind == "apply_bundle":
        bid = payload.strip().upper()
        if not re.fullmatch(r"BND-[A-Z0-9]{4,16}", bid):
            return {"ok": False, "reply": f"⚠️ 非法捆绑单号：{bid}", "keyboard": None}
        nat = resolve_and_apply_natural_confirm(db, "确认", quoted_ref=bid)
        if nat.get("matched"):
            reply = str(nat.get("reply") or "")
            return {"ok": bool(nat.get("ok")), "reply": reply, "keyboard": infer_keyboard_for_message(db, reply)}
        return {"ok": False, "reply": f"⚠️ 捆绑确认包 {bid} 已过期或已处理。", "keyboard": None}

    if kind == "cancel_bundle":
        bid = payload.strip().upper()
        if not re.fullmatch(r"BND-[A-Z0-9]{4,16}", bid):
            return {"ok": False, "reply": f"⚠️ 非法捆绑单号：{bid}", "keyboard": None}
        c_res = cancel_approval_bundle(db, bid)
        return {"ok": bool(c_res.get("ok")), "reply": str(c_res.get("reply") or ""), "keyboard": None}

    if kind == "cmd":
        cmd = payload.strip()
        if not _ALLOWED_CMD_RE.fullmatch(cmd):
            return {"ok": False, "reply": "⚠️ 该按钮命令不在安全白名单内，已拦截。", "keyboard": None}
        inb = handle_inbound_qq(db, cmd)
        if inb.get("handled") and inb.get("reply"):
            reply = str(inb["reply"])
            kb = inb.get("keyboard") or infer_keyboard_for_message(db, reply)
            return {"ok": True, "reply": reply, "keyboard": kb}
        return {"ok": False, "reply": f"⚠️ 当前上下文不支持执行「{cmd}」（可能已过期）。", "keyboard": None}

    if kind == "case_act":
        act_parts = payload.split(":", 1)
        if len(act_parts) != 2:
            return {"ok": False, "reply": "⚠️ 按钮参数不完整。", "keyboard": None}
        tid, act = act_parts[0].strip().upper(), act_parts[1].strip()
        if act not in _ALLOWED_CASE_ACTS or not re.fullmatch(r"[SW]\d{2,6}", tid):
            return {"ok": False, "reply": "⚠️ 该单据动作不在白名单内，已拦截。", "keyboard": None}
        tk = "case" if tid.startswith("S") else "wait"
        sub_meta: dict[str, Any] = {}
        # 顺路取件整批销账：补齐同一批未结 pickup 列表
        if act == "取了" and tk == "case":
            conn = db.connect()
            try:
                sub = conn.execute(
                    "SELECT metadata_json FROM subscriptions WHERE target_id=? AND state='ACTIVE' ORDER BY created_at DESC LIMIT 1",
                    (tid,),
                ).fetchone()
                if sub and sub["metadata_json"]:
                    sub_meta = json.loads(sub["metadata_json"])
            except Exception:
                sub_meta = {}
            finally:
                conn.close()
        sub_res = _handle_subscribed_reply(db, tk, tid, act, sub_meta=sub_meta)
        if sub_res.get("handled"):
            return {"ok": True, "reply": str(sub_res.get("reply") or f"[{tid}] 已处理。"), "keyboard": None}
        return {"ok": False, "reply": f"⚠️ [{tid}] 当前状态无法执行「{act}」。", "keyboard": None}

    if kind == "curiosity_ignore":
        qid = payload.strip()
        msg = dismiss_curiosity_question(db, qid="" if qid == "latest" else qid)
        return {"ok": True, "reply": msg, "keyboard": None}

    if kind == "trip_local":
        wid = payload.strip().upper()
        if not re.fullmatch(r"W\d{2,6}", wid):
            return {"ok": False, "reply": f"⚠️ 非法提醒编号：{wid}", "keyboard": None}
        from siming.outing_chain import bind_commitment_destination

        res = bind_commitment_destination(db, "在家", wait_id=wid)
        return {"ok": bool(res.get("ok")), "reply": str(res.get("reply") or ""), "keyboard": None}

    if kind == "meal_ack":
        parts_m = payload.split(":", 1)
        day_s = parts_m[0].strip() if len(parts_m) == 2 else ""
        win = parts_m[1].strip() if len(parts_m) == 2 else payload.strip()
        if win and win not in ("lunch", "dinner"):
            return {"ok": False, "reply": "⚠️ 非法餐窗参数。", "keyboard": None}
        from siming.meal_guard import record_meal_ack

        m_res = record_meal_ack(db, window=win, day_override=day_s, note="qq_button")
        return {"ok": bool(m_res.get("ok")), "reply": str(m_res.get("reply") or ""), "keyboard": None}

    if kind in ("sub_act", "sub_status"):
        parts_s = payload.split(":", 1)
        if len(parts_s) != 2:
            return {"ok": False, "reply": "⚠️ 订阅操作参数不完整。", "keyboard": None}
        sid, st = parts_s[0].strip(), parts_s[1].strip().upper()
        if st not in ("KEEP", "ZOMBIE", "CANCELLED", "ACTIVE"):
            return {"ok": False, "reply": "⚠️ 非法订阅状态。", "keyboard": None}
        from siming.sub_hunter import mark_subscription_status

        res = mark_subscription_status(db, sid, st, note="qq_button")
        if res.get("ok"):
            return {"ok": True, "reply": f"✅ 订阅 `{sid}` 已标记为 `{st}`。", "keyboard": None}
        return {"ok": False, "reply": f"⚠️ {res.get('error') or '标记失败'}", "keyboard": None}

    if kind == "sub_ongoing":
        parts_s = payload.split(":", 1)
        if len(parts_s) != 2:
            return {"ok": False, "reply": "⚠️ 订阅在途参数不完整。", "keyboard": None}
        sid, flag_s = parts_s[0].strip(), parts_s[1].strip().lower()
        if flag_s not in ("1", "0", "true", "false"):
            return {"ok": False, "reply": "⚠️ 非法订阅在途标志。", "keyboard": None}
        ongoing_val = flag_s in ("1", "true")
        from siming.sub_hunter import mark_subscription_status

        res = mark_subscription_status(db, sid, status=None, note="qq_button", is_ongoing=ongoing_val)
        if res.get("ok"):
            label = "进行中" if ongoing_val else "已停扣"
            return {"ok": True, "reply": f"✅ 订阅 `{sid}` 已标记为「{label}」。", "keyboard": None}
        return {"ok": False, "reply": f"⚠️ {res.get('error') or '标记失败'}", "keyboard": None}

    if kind == "replenish_buy":
        iid = payload.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,48}", iid):
            return {"ok": False, "reply": f"⚠️ 非法日用品编号：{iid}", "keyboard": None}
        from siming.replenish_cart import stage_replenish_buy

        res = stage_replenish_buy(db, iid)
        reply = str(res.get("reply") or res.get("error") or "备单完成")
        kb = infer_keyboard_for_message(db, reply)
        return {"ok": bool(res.get("ok")), "reply": reply, "keyboard": kb}

    if kind == "replenish_bought":
        iid = payload.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,48}", iid):
            return {"ok": False, "reply": f"⚠️ 非法日用品编号：{iid}", "keyboard": None}
        from siming.replenish_cart import mark_replenish_bought

        res = mark_replenish_bought(db, iid)
        if res.get("ok"):
            return {"ok": True, "reply": str(res.get("summary") or "✅ 已重置补货周期。"), "keyboard": None}
        return {"ok": False, "reply": f"⚠️ {res.get('error') or '更新失败'}", "keyboard": None}

    if kind == "replenish_snooze":
        parts_r = payload.split(":", 1)
        iid = parts_r[0].strip()
        try:
            s_days = max(1, min(180, int(parts_r[1].strip()))) if len(parts_r) == 2 else 14
        except ValueError:
            s_days = 14
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,48}", iid):
            return {"ok": False, "reply": f"⚠️ 非法日用品编号：{iid}", "keyboard": None}
        from siming.replenish_cart import snooze_replenish_item

        res = snooze_replenish_item(db, iid, days=s_days)
        if res.get("ok"):
            return {"ok": True, "reply": str(res.get("summary") or f"⏳ 已延后 {s_days} 天。"), "keyboard": None}
        return {"ok": False, "reply": f"⚠️ {res.get('error') or '延后失败'}", "keyboard": None}

    if kind == "replenish_pause":
        iid = payload.strip()
        if not re.fullmatch(r"[A-Za-z0-9_-]{3,48}", iid):
            return {"ok": False, "reply": f"⚠️ 非法日用品编号：{iid}", "keyboard": None}
        from siming.replenish_cart import toggle_replenish_item

        res = toggle_replenish_item(db, iid, enabled=False)
        if res.get("ok"):
            return {"ok": True, "reply": str(res.get("summary") or "⏸️ 已暂停该日用品提醒。"), "keyboard": None}
        return {"ok": False, "reply": f"⚠️ {res.get('error') or '暂停失败'}", "keyboard": None}

    return {"ok": False, "reply": f"⚠️ 未识别的司命按钮类型：{kind}", "keyboard": None}


def _load_qq_credentials() -> tuple[str, str]:
    """从环境或 `~/.hermes/.env` 只读获取 QQ_APP_ID / QQ_CLIENT_SECRET（绝不打印明文）。"""
    app_id = os.environ.get("QQ_APP_ID", "").strip()
    secret = os.environ.get("QQ_CLIENT_SECRET", "").strip()
    if app_id and secret:
        return app_id, secret
    env_file = Path.home() / ".hermes" / ".env"
    if env_file.is_file():
        try:
            for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = line.strip()
                if not s or s.startswith("#") or "=" not in s:
                    continue
                k, _, v = s.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k == "QQ_APP_ID" and not app_id:
                    app_id = v
                elif k == "QQ_CLIENT_SECRET" and not secret:
                    secret = v
        except OSError:
            pass
    return app_id, secret


def _http_post_json(url: str, body: dict[str, Any], headers: dict[str, str], timeout: float = 15.0) -> tuple[int, dict[str, Any], str]:
    data_bytes = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data_bytes, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw else {}
            except Exception:
                parsed = {}
            return resp.status, (parsed if isinstance(parsed, dict) else {}), raw
    except urllib.error.HTTPError as exc:
        raw = ""
        try:
            raw = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        try:
            parsed = json.loads(raw) if raw else {}
        except Exception:
            parsed = {}
        return exc.code, (parsed if isinstance(parsed, dict) else {}), raw[:240]
    except Exception as exc:
        return 0, {}, str(exc)[:240]


def _get_qq_access_token(app_id: str, secret: str, cache_dir: Optional[Path] = None) -> tuple[str, str]:
    """获取或复用缓存的 QQ Bot access_token，缓存落盘在 `$TMPDIR/siming`（0600 权限）。"""
    tmp_root = cache_dir or (Path(os.environ.get("TMPDIR") or (Path.home() / ".cache" / "agent-tmp")) / "siming")
    tmp_root.mkdir(parents=True, exist_ok=True)
    cache_file = tmp_root / f"qq_token_{hashlib.sha256(app_id.encode()).hexdigest()[:12]}.json"
    now = time.time()
    if cache_file.is_file():
        try:
            cached = json.loads(cache_file.read_text(encoding="utf-8"))
            if cached.get("access_token") and float(cached.get("expires_at", 0)) > now + 60:
                return str(cached["access_token"]), ""
        except Exception:
            pass

    status, data, raw = _http_post_json(
        TOKEN_URL,
        {"appId": str(app_id), "clientSecret": str(secret)},
        {"Content-Type": "application/json"},
        timeout=12.0,
    )
    if status != 200 or not data.get("access_token"):
        return "", f"token request failed [{status}]: {data.get('message') or raw[:160]}"
    token = str(data["access_token"])
    expires_in = int(data.get("expires_in", 7200) or 7200)
    try:
        cache_file.write_text(
            json.dumps({"access_token": token, "expires_at": now + expires_in}),
            encoding="utf-8",
        )
        os.chmod(cache_file, 0o600)
    except OSError:
        pass
    return token, ""


def send_qq_rest_with_keyboard(
    text: str,
    qq_target: str,
    keyboard: dict[str, Any],
    *,
    reply_to: Optional[str] = None,
    cache_dir: Optional[Path] = None,
) -> tuple[bool, str, str]:
    """直连 QQ Bot REST API 发送带内嵌键盘的 Markdown 消息（`msg_type=2`）。

    返回 `(ok, msg_id, error)`；调用方失败时自动降级到普通文本外发，绝不丢消息。
    """
    if os.environ.get("SIMING_DRY_RUN_SEND") == "1":
        fake_id = "dryrun-kb-" + hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]
        return True, fake_id, ""

    target = (qq_target or "").strip()
    if target.lower().startswith("qqbot:"):
        chat_id = target.split(":", 1)[1].strip()
    else:
        chat_id = target
    if not chat_id:
        return False, "", "empty qq chat_id"

    app_id, secret = _load_qq_credentials()
    if not app_id or not secret:
        return False, "", "QQ_APP_ID / QQ_CLIENT_SECRET not configured"

    token, tok_err = _get_qq_access_token(app_id, secret, cache_dir=cache_dir)
    if not token:
        return False, "", tok_err

    msg_seq = (int(time.time()) ^ int(uuid.uuid4().hex[:4], 16)) % 65536
    safe_keyboard = _sanitize_keyboard_payload(keyboard)
    body: dict[str, Any] = {
        "markdown": {"content": (text or "")[:4000]},
        "msg_type": 2,
        "msg_seq": msg_seq,
        "keyboard": safe_keyboard,
    }
    if reply_to:
        body["msg_id"] = reply_to

    headers = {
        "Authorization": f"QQBot {token}",
        "Content-Type": "application/json",
    }
    status, data, raw = _http_post_json(
        f"{API_BASE}/v2/users/{chat_id}/messages",
        body,
        headers,
        timeout=15.0,
    )
    if status in (200, 201):
        return True, str(data.get("id") or ""), ""
    return False, "", f"QQBot keyboard send [{status}]: {data.get('message') or raw[:200]}"
