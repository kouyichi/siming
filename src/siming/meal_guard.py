"""Wave 1 (W1-4 · meal_guard)：「防忘吃饭」饮食节律守卫 + 跨店双拼点餐协调与 A2 捆绑审批。

能力：
1. 饮食节律守卫：在午餐（12:30–15:00）/ 晚餐（19:00–22:00）窗口内，尊重 Night-Owl 清醒/睡眠态，
   检查近 5 小时是否有外卖订单（`shop_order_items`）或餐饮流水（`ledger_txns`），每餐窗最多温柔提醒一次并附 QQ 快捷点餐按钮；
2. 跨店双拼点餐协调：解析 `"示例粉面炒米粉 + 示例果切西瓜"` / `"示例瓦罐汤瓦罐汤 和 瑞幸冰美式"` 等双店需求，
   测算送达时间差（ETA delta）与错峰下单建议、合并配送费与最优神券分拆方案，一键落库 A2 审批卡（`action="meituan_combo_order"`）。
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import datetime, timedelta
from typing import Any, Optional

from siming import db as _db_mod
from siming.approval import create_approval
from siming.attention import evaluate_attention_state
from siming.cn_calendar import day_info
from siming.db import SimingDB, iso_now, logical_today, now_local

_ORIG_NOW_LOCAL = now_local


def _cur_now() -> datetime:
    return now_local() if now_local is not _ORIG_NOW_LOCAL else _db_mod.now_local()
from siming.event_chain import get_situation
from siming.modules import is_module_enabled
from siming.qq_cards import build_approval_card_keyboard, make_button, make_keyboard

_FOOD_MERCHANT_RE = re.compile(
    r"(美团|饿了么|外卖|餐饮|食堂|餐厅|饭店|麦当劳|肯德基|汉堡|披萨|瑞幸|星巴克|奶茶|喜茶|霸王茶姬|"
    r"示例粉面|示例瓦罐汤|瓦罐|米粉|面馆|拉面|烧烤|火锅|麻辣烫|轻食|沙拉|便当|盖饭|示例果切|切果|水果|甜品|烘焙|面包)",
    re.IGNORECASE,
)
_NON_FOOD_SUPPLY_RE = re.compile(
    r"(口罩|针头|酒精棉片|棉片|棉签|小剪刀|体温计|创可贴|垃圾袋|抽纸|卫生纸|洗衣液|洗洁精|电池|医疗器械|药店|大药房)"
)
_EXPLICIT_COMBO_SPLIT_RE = re.compile(r"\s*(?:\+|＋|&|/|、|\s+和\s+|\s+配\s+|\s+加\s+)\s*")
_INLINE_COMBO_SPLIT_RE = re.compile(r"(?<=[\u4e00-\u9fffA-Za-z0-9]{2})(?:和|配)(?=[\u4e00-\u9fffA-Za-z0-9]{2})")
_NON_SHOP_SIDE_RE = re.compile(r"^(?:辣椒|醋|葱|蒜|香菜|配菜|小菜|小料|蘸料|配方|加蛋|煎蛋|荷包蛋|米饭)$")


def _split_combo_query(q: str) -> list[str]:
    """拆分跨店双拼点餐输入：优先显式分隔符（+/和/、等），避免把『和府捞面』『太和板面』『炒米粉配辣椒』误拆成两家店。"""
    explicit = [p.strip() for p in _EXPLICIT_COMBO_SPLIT_RE.split(q) if p.strip()]
    if len(explicit) >= 2:
        return explicit
    inline = [p.strip() for p in _INLINE_COMBO_SPLIT_RE.split(q) if p.strip()]
    if len(inline) >= 2 and all(len(p) >= 2 and not _NON_SHOP_SIDE_RE.fullmatch(p) for p in inline):
        return inline
    return [q] if q else []

# 常用偏好店铺默认画像（当未传入实时 shops_meta 时的兜底估算）
_KNOWN_SHOP_DEFAULTS: tuple[tuple[str, dict[str, Any]], ...] = (
    ("示例粉面", {"shop_name": "示例粉面炒米粉", "eta_min": 32, "delivery_fee": 3.5, "item_price": 29.0, "coupon_yuan": 6.0}),
    ("示例瓦罐汤", {"shop_name": "示例瓦罐汤南昌粉面·瓦罐汤", "eta_min": 35, "delivery_fee": 3.0, "item_price": 34.0, "coupon_yuan": 7.0}),
    ("示例果切", {"shop_name": "示例鲜果切", "eta_min": 24, "delivery_fee": 2.5, "item_price": 26.0, "coupon_yuan": 5.0}),
    ("瑞幸", {"shop_name": "瑞幸咖啡", "eta_min": 18, "delivery_fee": 4.0, "item_price": 15.0, "coupon_yuan": 4.0}),
    ("霸王茶姬", {"shop_name": "霸王茶姬", "eta_min": 22, "delivery_fee": 3.0, "item_price": 18.0, "coupon_yuan": 3.0}),
    ("麦当劳", {"shop_name": "麦当劳", "eta_min": 28, "delivery_fee": 6.0, "item_price": 38.0, "coupon_yuan": 8.0}),
)


def _detect_meal_window(cur: datetime, *, is_workday: bool = True) -> Optional[str]:
    hhmm = cur.strftime("%H:%M")
    if not is_workday:
        # 周末/法定节假日放宽午餐窗（夜猫子补觉），仅在 20:30–22:00 评估晚餐轻提醒
        if "20:30" <= hhmm <= "22:00":
            return "dinner"
        return None
    if "12:30" <= hhmm <= "15:00":
        return "lunch"
    if "19:00" <= hhmm <= "22:00":
        return "dinner"
    return None


def _is_confirmed_awake_for_non_workday(db: SimingDB, att: dict[str, Any]) -> bool:
    """非工作日（周末/节假日）需明确确认清醒态（`sleep:state == awake` 或显式 AWAKE / 真实活跃桌面），避免离线静默默认误扰。"""
    sit_sleep = get_situation(db, "sleep:state")
    if sit_sleep and isinstance(sit_sleep.get("value"), dict):
        st_val = str(sit_sleep["value"].get("state") or "").strip().lower()
        if st_val in ("sleep", "asleep", "sleeping"):
            return False
        if st_val in ("awake", "awake_active", "active"):
            return True

    if att.get("state") == "ASLEEP" or att.get("should_hold_l2"):
        return False
    if (db.get_meta("attention_explicit_state") or "").upper() == "AWAKE":
        return True
    if att.get("state") == "AWAKE_ACTIVE" and att.get("reason") not in ("fallback_static_quiet", "static_quiet_hours"):
        return True
    return False


def _infer_meal_window_for_ack(cur: datetime, hint: str = "") -> str:
    h = (hint or "").strip().lower()
    if h in ("lunch", "午饭", "午餐", "中午"):
        return "lunch"
    if h in ("dinner", "晚饭", "晚餐", "晚上", "今晚"):
        return "dinner"
    return "lunch" if "10:00" <= cur.strftime("%H:%M") < "16:30" else "dinner"


def record_meal_ack(
    db: SimingDB,
    window: str = "",
    *,
    day_override: str = "",
    now: Optional[datetime] = None,
    note: str = "",
) -> dict[str, Any]:
    """记录本人「吃过了 / 今晚不点了」确认，同时静默本餐窗的 meal_guard 与 overtime_meal。"""
    db.init_db()
    cur = now or _cur_now()
    day_s = day_override.strip() if re.fullmatch(r"\d{4}-\d{2}-\d{2}", (day_override or "").strip()) else logical_today(cur).isoformat()
    win = _infer_meal_window_for_ack(cur, window or note)
    ts = iso_now(cur)
    db.set_meta(f"meal_ack:{day_s}:{win}", ts)
    db.set_meta(f"meal_guard_nudged:{day_s}:{win}", ts)
    db.set_meta("last_meal_ack_at", ts)
    db.record_human_use("meal_combo_guard", "meal_ack", ref_id=f"{day_s}:{win}")
    meal_cn = "午饭" if win == "lunch" else "晚饭"
    return {
        "ok": True,
        "day": day_s,
        "window": win,
        "acked_at": ts,
        "reply": f"👌 收到，已记下今天{meal_cn}已安排/不用点，本餐窗不再打扰。",
    }


def is_meal_acked(
    db: SimingDB,
    cur: Optional[datetime] = None,
    *,
    window: str = "",
    lookback_hours: float = 5.0,
) -> bool:
    cur_dt = cur or _cur_now()
    day_s = logical_today(cur_dt).isoformat()
    win = _infer_meal_window_for_ack(cur_dt, window)
    if db.get_meta(f"meal_ack:{day_s}:{win}"):
        return True
    last_ack = db.get_meta("last_meal_ack_at") or ""
    if last_ack:
        try:
            ack_dt = datetime.fromisoformat(last_ack)
            delta_sec = (cur_dt - ack_dt).total_seconds()
            if 0 <= delta_sec <= lookback_hours * 3600.0:
                return True
        except Exception:
            pass
    return False


def find_recent_food_activity(
    db: SimingDB,
    cur: datetime,
    lookback_hours: float = 5.0,
    *,
    since_iso: Optional[str] = None,
) -> list[dict[str, Any]]:
    """统一检索近 N 小时（或指定起止时间）内的真实进食/点餐证据：
    1. shop_order_items（排除取消/退款及口罩/针头等非餐饮日用品）；
    2. ledger_txns（排除作废/收入/退款，命中餐饮商户）；
    3. cases（最近创建的外卖取餐柜 food_pickup 单据，无论待取或已取）；
    4. events（近期外卖入柜/取餐码事件）。
    """
    cutoff_iso = since_iso or iso_now(cur - timedelta(hours=lookback_hours))
    cur_iso = iso_now(cur)
    found: list[dict[str, Any]] = []

    conn = db.connect()
    try:
        order_rows = conn.execute(
            """
            SELECT item_key, platform, shop, ordered_at, order_status, order_amount_cents, item_name
            FROM shop_order_items
            WHERE ordered_at >= ? AND ordered_at <= ?
              AND COALESCE(order_status, '') NOT LIKE '%取消%'
              AND COALESCE(order_status, '') NOT LIKE '%退款%'
            ORDER BY ordered_at DESC
            """,
            (cutoff_iso, cur_iso),
        ).fetchall()
        for r in order_rows:
            hay = f"{r['platform']} {r['shop']} {r['item_name']}"
            shop_item = f"{r['shop']} {r['item_name']}"
            if _NON_FOOD_SUPPLY_RE.search(shop_item) and not _FOOD_MERCHANT_RE.search(shop_item):
                continue
            if str(r["platform"]).lower() in ("meituan", "eleme", "luckin", "美团", "饿了么", "瑞幸") or _FOOD_MERCHANT_RE.search(hay):
                found.append(
                    {
                        "source": "shop_order_items",
                        "at": str(r["ordered_at"]),
                        "label": f"{r['shop'] or r['platform']} · {r['item_name']}",
                        "yuan": round(int(r["order_amount_cents"] or 0) / 100.0, 2),
                    }
                )

        txn_rows = conn.execute(
            """
            SELECT txn_id, occurred_at, amount_cents, merchant, notes, kind, status
            FROM ledger_txns
            WHERE occurred_at >= ? AND occurred_at <= ?
              AND amount_cents > 0
              AND kind NOT IN ('收入', '退款', '还款', '转入')
              AND COALESCE(UPPER(status), '') NOT IN ('VOID_DUPLICATE', 'VOID', 'CANCELLED')
            ORDER BY occurred_at DESC
            """,
            (cutoff_iso, cur_iso),
        ).fetchall()
        for r in txn_rows:
            hay = f"{r['merchant']} {r['notes']} {r['kind']}"
            if _NON_FOOD_SUPPLY_RE.search(hay) and not re.search(r"(外卖|餐饮|食堂|餐厅|饭店|米粉|瓦罐|咖啡|奶茶|汉堡|披萨|面馆|拉面)", hay):
                continue
            if _FOOD_MERCHANT_RE.search(hay):
                found.append(
                    {
                        "source": "ledger_txns",
                        "at": str(r["occurred_at"]),
                        "label": str(r["merchant"] or r["notes"]),
                        "yuan": round(int(r["amount_cents"] or 0) / 100.0, 2),
                    }
                )

        food_case_rows = conn.execute(
            """
            SELECT case_id, title, created_at
            FROM cases
            WHERE category = 'food_pickup'
              AND status != '取消'
              AND created_at >= ? AND created_at <= ?
            ORDER BY created_at DESC
            """,
            (cutoff_iso, cur_iso),
        ).fetchall()
        for r in food_case_rows:
            found.append(
                {
                    "source": "food_pickup_case",
                    "at": str(r["created_at"]),
                    "label": f"[{r['case_id']}] {r['title']}",
                    "yuan": 0.0,
                }
            )

        food_ev_rows = conn.execute(
            """
            SELECT event_id, occurred_at, summary
            FROM events
            WHERE occurred_at >= ? AND occurred_at <= ?
              AND (thread_key LIKE 'food_pickup:%' OR summary LIKE '%外卖柜%' OR summary LIKE '%外卖入柜%' OR summary LIKE '%取餐码%')
            ORDER BY occurred_at DESC
            """,
            (cutoff_iso, cur_iso),
        ).fetchall()
        for r in food_ev_rows:
            found.append(
                {
                    "source": "food_event",
                    "at": str(r["occurred_at"]),
                    "label": str(r["summary"]),
                    "yuan": 0.0,
                }
            )
    finally:
        conn.close()
    return found


_find_recent_food_activity = find_recent_food_activity


def evaluate_meal_regularity(
    db: SimingDB,
    now: Optional[datetime] = None,
    notify: bool = False,
) -> dict[str, Any]:
    """「防忘吃饭」饮食节律守卫：在午/晚餐窗检查是否清醒且近 5h 未进食，每餐窗最多温柔提醒一次。
    - 严格遵守 `meal_combo_guard` 模块开关；
    - 周末与法定节假日放宽午餐窗（不打扰补觉），仅在确认清醒（`sleep:state == awake`）且 20:30 后未见饮食消费时轻柔提醒晚餐，绝不使用“加班”话术。
    """
    db.init_db()
    if not is_module_enabled(db, "meal_combo_guard"):
        return {
            "ok": True,
            "window": None,
            "should_nudge": False,
            "nudged": False,
            "skipped": "module_disabled",
            "reason": "module_disabled",
            "recent_food": [],
        }

    cur = now or _cur_now()
    logical_d = logical_today(cur)
    di = day_info(logical_d, db.paths)
    is_workday = di.is_workday

    window = _detect_meal_window(cur, is_workday=is_workday)
    if not window:
        workday_win = _detect_meal_window(cur, is_workday=True)
        reason = "non_workday_relaxed" if (not is_workday and workday_win is not None) else "outside_meal_window"
        return {
            "ok": True,
            "window": None,
            "is_workday": is_workday,
            "day_kind": di.kind_label,
            "should_nudge": False,
            "nudged": False,
            "reason": reason,
            "recent_food": [],
        }

    att = evaluate_attention_state(db, now_override=cur)
    if att.get("state") == "ASLEEP" or att.get("should_hold_l2"):
        return {
            "ok": True,
            "window": window,
            "is_workday": is_workday,
            "day_kind": di.kind_label,
            "should_nudge": False,
            "nudged": False,
            "reason": "user_asleep",
            "attention_state": att.get("state"),
            "recent_food": [],
        }

    if not is_workday and not _is_confirmed_awake_for_non_workday(db, att):
        return {
            "ok": True,
            "window": window,
            "is_workday": False,
            "day_kind": di.kind_label,
            "should_nudge": False,
            "nudged": False,
            "reason": "non_workday_not_confirmed_awake",
            "attention_state": att.get("state"),
            "recent_food": [],
        }

    if is_meal_acked(db, cur, window=window, lookback_hours=5.0):
        return {
            "ok": True,
            "window": window,
            "is_workday": is_workday,
            "day_kind": di.kind_label,
            "should_nudge": False,
            "nudged": False,
            "reason": "user_acked_meal",
            "attention_state": att.get("state"),
            "recent_food": [],
        }

    recent_food = find_recent_food_activity(db, cur, lookback_hours=5.0)
    if recent_food:
        return {
            "ok": True,
            "window": window,
            "is_workday": is_workday,
            "day_kind": di.kind_label,
            "should_nudge": False,
            "nudged": False,
            "reason": "recent_meal_found",
            "attention_state": att.get("state"),
            "recent_food": recent_food,
        }

    day_s = logical_d.isoformat()
    dedup_key = f"meal_guard_nudged:{day_s}:{window}"
    if db.get_meta(dedup_key):
        return {
            "ok": True,
            "window": window,
            "is_workday": is_workday,
            "day_kind": di.kind_label,
            "should_nudge": False,
            "nudged": False,
            "reason": "already_nudged_this_window",
            "attention_state": att.get("state"),
            "recent_food": [],
        }

    if is_workday:
        meal_cn = "午饭" if window == "lunch" else "晚饭"
        text = (
            f"🍱 **防忘吃饭轻提醒（{meal_cn}）**：已经 `{cur.strftime('%H:%M')}` 了，近 5 小时没看到外卖或餐饮消费记录。\n"
            "• 忙起来也别饿坏胃，点下面按钮一键看推荐或拼双店套餐："
        )
    else:
        day_label = di.holiday_name or "周末"
        text = (
            f"🍽️ **{day_label}晚餐轻提醒**：已经 `{cur.strftime('%H:%M')}` 了，近 5 小时没看到外卖或餐饮消费记录。\n"
            "• 休息日也记得按时吃点热乎的，需要的话点下面按钮看看推荐或双拼："
        )

    kb = make_keyboard(
        [
            [
                make_button("rec", "🍽️ 外卖推荐", "siming:cmd:外卖推荐", style=1, group_id=f"meal_{day_s}_{window}"),
                make_button("combo", "🍉 双拼套餐", "siming:cmd:晚餐推荐", style=0, group_id=f"meal_{day_s}_{window}"),
                make_button("ate", "✅ 吃过了", f"siming:meal_ack:{day_s}:{window}", style=0, group_id=f"meal_{day_s}_{window}"),
            ]
        ]
    )

    nudged = False
    if notify and is_module_enabled(db, "meal_combo_guard"):
        from siming.push import push_message

        res = push_message(
            db,
            text=text,
            rule_id="meal_combo_guard",
            ref_id=f"meal_guard:{day_s}:{window}",
            level="normal",
            dedup_window=timedelta(hours=6),
            keyboard=kb,
            now=cur,
        )
        if res.get("ok"):
            db.set_meta(dedup_key, iso_now(cur))
            nudged = True

    return {
        "ok": True,
        "window": window,
        "is_workday": is_workday,
        "day_kind": di.kind_label,
        "should_nudge": True,
        "nudged": nudged,
        "reason": "missed_meal_window",
        "attention_state": att.get("state"),
        "recent_food": [],
        "message": text,
        "keyboard": kb,
    }


def _resolve_leg_meta(raw_part: str, shops_meta: Optional[Any], idx: int) -> dict[str, Any]:
    part = raw_part.strip()
    meta: dict[str, Any] = {}

    if isinstance(shops_meta, dict):
        if part in shops_meta and isinstance(shops_meta[part], dict):
            meta = dict(shops_meta[part])
        else:
            for k, v in shops_meta.items():
                if isinstance(v, dict) and (k in part or part in k):
                    meta = dict(v)
                    break
    elif isinstance(shops_meta, list):
        if idx < len(shops_meta) and isinstance(shops_meta[idx], dict):
            meta = dict(shops_meta[idx])
        else:
            for m in shops_meta:
                if isinstance(m, dict) and str(m.get("shop_name") or "") and str(m.get("shop_name")) in part:
                    meta = dict(m)
                    break

    if not meta:
        for key, defaults in _KNOWN_SHOP_DEFAULTS:
            if key in part:
                meta = dict(defaults)
                break

    shop_name = str(meta.get("shop_name") or part[:12]).strip()
    item_name = str(meta.get("item_name") or part).strip()
    eta_min = int(meta.get("eta_min") if meta.get("eta_min") is not None else (30 if idx == 0 else 22))
    delivery_fee = round(float(meta.get("delivery_fee") if meta.get("delivery_fee") is not None else 3.5), 2)
    item_price = round(float(meta.get("item_price") if meta.get("item_price") is not None else (32.0 if idx == 0 else 20.0)), 2)
    coupon_yuan = round(float(meta.get("coupon_yuan") if meta.get("coupon_yuan") is not None else (5.0 if item_price >= 25.0 else 3.0)), 2)
    platform = str(meta.get("platform") or ("luckin_mcp" if "瑞幸" in part else "meituan")).strip()

    return {
        "index": idx + 1,
        "raw_part": part,
        "shop_name": shop_name,
        "item_name": item_name,
        "platform": platform,
        "eta_min": max(5, eta_min),
        "delivery_fee": max(0.0, delivery_fee),
        "item_price": max(0.0, item_price),
        "coupon_yuan": max(0.0, min(coupon_yuan, item_price)),
    }


def plan_dual_shop_combo(
    db: SimingDB,
    query: str,
    location: str = "",
    shops_meta: Optional[Any] = None,
) -> dict[str, Any]:
    """解析跨店双拼点餐请求，测算送达时间差、错峰下单间隔、合并配送费与最优双券拆分。"""
    db.init_db()
    q = (query or "").strip()
    parts = _split_combo_query(q)
    if len(parts) < 2:
        return {
            "ok": False,
            "error": f"请使用「+」或「和」分隔两家店/菜品，例如：示例粉面炒米粉 + 示例果切西瓜（收到：{q!r}）",
            "reply": f"⚠️ 请使用「+」或「和」分隔两家店/菜品，例如：`双拼点餐 示例粉面炒米粉 + 示例果切西瓜`（收到单店：{q}）。如需单店下单请直接发 `点外卖 {q}`。",
        }

    legs = [_resolve_leg_meta(parts[i], shops_meta, i) for i in range(min(3, len(parts)))]
    etas = [int(l["eta_min"]) for l in legs]
    max_eta = max(etas)
    min_eta = min(etas)
    eta_delta = max_eta - min_eta

    slowest_leg = max(legs, key=lambda l: l["eta_min"])
    fastest_leg = min(legs, key=lambda l: l["eta_min"])
    stagger_delay = eta_delta if eta_delta >= 8 else 0
    stagger_advice = (
        f"先下【{slowest_leg['shop_name']}】（约 {slowest_leg['eta_min']}m），"
        f"延后 {stagger_delay} 分钟再下【{fastest_leg['shop_name']}】（约 {fastest_leg['eta_min']}m），同步送达口感最佳"
        if stagger_delay > 0
        else f"两店预计送达仅差 {eta_delta} 分钟，可直接同步下单"
    )

    subtotal_items = round(sum(float(l["item_price"]) for l in legs), 2)
    combined_delivery_fee = round(sum(float(l["delivery_fee"]) for l in legs), 2)

    # 最优双券拆分：若未显式注入 shops_meta 券额且账号缓存有实时红包，执行全局 1 对 1 最优分配（防同券复用、最大化总省钱）
    explicit_coupon_provided = isinstance(shops_meta, dict) or (
        isinstance(shops_meta, list) and any(isinstance(m, dict) and m.get("coupon_yuan") is not None for m in shops_meta)
    )
    if not explicit_coupon_provided:
        try:
            from siming.meituan_coupons import optimize_multi_leg_coupons
            from siming.meituan_speculate import load_cached_account_coupons

            cached_acc = load_cached_account_coupons(db, max_age_min=720.0)
            if cached_acc and (cached_acc.get("platform_coupons") or cached_acc.get("flash_coupons")):
                opt_assign = optimize_multi_leg_coupons(legs, cached_acc)
                for a in opt_assign:
                    li = int(a["leg_index"])
                    if 0 <= li < len(legs):
                        legs[li]["coupon_yuan"] = float(a["coupon_yuan"])
                        legs[li]["coupon_label"] = str(a["coupon_label"])
        except Exception:
            pass

    optimal_coupon_split: list[dict[str, Any]] = []
    total_coupon_savings = 0.0
    for leg in legs:
        c_val = float(leg["coupon_yuan"])
        leg_net = round(float(leg["item_price"]) + float(leg["delivery_fee"]) - c_val, 2)
        total_coupon_savings = round(total_coupon_savings + c_val, 2)
        c_lbl = str(leg.get("coupon_label") or f"独立神券/满减 -¥{c_val:.2f}")
        optimal_coupon_split.append(
            {
                "shop_name": leg["shop_name"],
                "item_name": leg["item_name"],
                "coupon_yuan": c_val,
                "coupon_strategy": c_lbl,
                "net_yuan": max(0.0, leg_net),
            }
        )

    single_coupon_naive = max((float(l["coupon_yuan"]) for l in legs), default=0.0)
    extra_split_gain = round(total_coupon_savings - single_coupon_naive, 2)
    estimated_total = round(max(0.0, subtotal_items + combined_delivery_fee - total_coupon_savings), 2)

    loc = (location or "").strip()
    if not loc:
        raw_loc = db.get_meta("latest_ios_location") or ""
        try:
            loc_obj = json.loads(raw_loc) if raw_loc else {}
            loc = str(loc_obj.get("place_alias") or loc_obj.get("address") or "默认收货地址")
        except Exception:
            loc = "默认收货地址"

    return {
        "ok": True,
        "query": q,
        "location": loc,
        "legs": legs,
        "eta_delta_min": eta_delta,
        "max_eta_min": max_eta,
        "stagger_delay_min": stagger_delay,
        "stagger_advice": stagger_advice,
        "subtotal_items_yuan": subtotal_items,
        "combined_delivery_fee": combined_delivery_fee,
        "coupon_savings_yuan": total_coupon_savings,
        "extra_split_gain_yuan": extra_split_gain,
        "optimal_coupon_split": optimal_coupon_split,
        "estimated_total_yuan": estimated_total,
    }


def format_combo_plan(plan: dict[str, Any]) -> str:
    """将双拼点餐方案格式化为清晰可读的 QQ 卡片文本。"""
    if not plan.get("ok"):
        return f"⚠️ 双拼点餐规划失败：{plan.get('error') or '未知错误'}"
    lines = [
        f"🍱 **跨店双拼点餐方案**（送至：{plan.get('location') or '默认地址'}）",
    ]
    for leg in plan.get("legs") or []:
        lines.append(
            f"{leg['index']}. **{leg['shop_name']}** · {leg['item_name']}"
            f"｜约 `{leg['eta_min']}m` ｜餐品 ¥{leg['item_price']:.2f} + 配送 ¥{leg['delivery_fee']:.2f} - 券 ¥{leg['coupon_yuan']:.2f}"
        )
    lines.append(f"• ⏱️ 时差与错峰：ETA 差 **{plan['eta_delta_min']} 分钟** — {plan['stagger_advice']}")
    lines.append(
        f"• 🎟️ 配送与拆券：合并配送费 ¥{plan['combined_delivery_fee']:.2f} ｜双单拆券共省 **¥{plan['coupon_savings_yuan']:.2f}**"
        f"（比单券多省 ¥{plan['extra_split_gain_yuan']:.2f}）"
    )
    lines.append(f"• 💳 预估到手合计：**¥{plan['estimated_total_yuan']:.2f}**")
    return "\n".join(lines)


def stage_dual_shop_combo_approval(
    db: SimingDB,
    query: str,
    location: str = "",
    shops_meta: Optional[Any] = None,
) -> dict[str, Any]:
    """为跨店双拼点餐生成 A2 审批捆绑包（`action='meituan_combo_order'`），严禁未经确认自动下单。"""
    plan = plan_dual_shop_combo(db, query=query, location=location, shops_meta=shops_meta)
    if not plan.get("ok"):
        return plan

    bundle_id = f"BND-{secrets.token_hex(4).upper()}"
    target_id = "+".join(l["shop_name"] for l in plan["legs"])[:60]
    approval = create_approval(
        db,
        action="meituan_combo_order",
        target_id=target_id,
        params={
            "query": plan["query"],
            "location": plan["location"],
            "legs": plan["legs"],
            "eta_delta_min": plan["eta_delta_min"],
            "stagger_delay_min": plan["stagger_delay_min"],
            "combined_delivery_fee": plan["combined_delivery_fee"],
            "coupon_savings_yuan": plan["coupon_savings_yuan"],
            "estimated_total_yuan": plan["estimated_total_yuan"],
        },
        tier="A2",
        bundle_id=bundle_id,
    )
    token = str(approval["token"])
    card_text = (
        format_combo_plan(plan)
        + f"\n• 🔐 A2 审批口令：`apply {token}`（捆绑单 `{bundle_id}`，过期时间 `{approval['expires_at'][:16]}`）"
    )
    keyboard = build_approval_card_keyboard(token, action="meituan_combo_order", bundle_id=bundle_id)

    return {
        "ok": True,
        "token": token,
        "bundle_id": bundle_id,
        "action": "meituan_combo_order",
        "expires_at": approval["expires_at"],
        "plan": plan,
        "card_text": card_text,
        "reply": card_text,
        "keyboard": keyboard,
    }
