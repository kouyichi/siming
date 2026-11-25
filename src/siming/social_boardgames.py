"""W4-5 · 桌游社交与人情记忆。

1. 200 盒 / 3.0 m³ 桌游柜容积硬上限守卫 + 新物集/KS/Gamefound/闲鱼众筹尾款预警；
2. 桌游局组织器：按人数与时长智能选戏 + QQ 投票卡片键盘 + 3 分钟 boardgame_kb 速查规则卡；
3. 温和对外口头承诺捕获器（social_commitments）+ Fernet 本地强加密亲友偏好手账（friend_profiles，绝不落明文）。
"""

from __future__ import annotations

import json
import re
import secrets
from datetime import date, datetime
from typing import Any, Callable, Optional

from siming.config import load_policy
from siming.crypto import _get_fernet
from siming import db as _db
from siming.db import SimingDB, iso_now
from siming.qq_cards import make_button, make_keyboard

_ENC_PREFIX = "fernet:v1:"

# 内置精选桌游库（涵盖 2~8 人、25~180 分钟，优先经典德策/美式/聚会）
DEFAULT_BOARDGAME_CATALOG: tuple[dict[str, Any], ...] = (
    {
        "title": "璀璨宝石 (Splendor)",
        "min_players": 2,
        "max_players": 4,
        "duration_min": 35,
        "weight": 1.8,
        "tags": ["轻策", "成套收集", "破冰"],
        "goal": "率先攒够 15 分声望（购买发展卡与吸引贵族）。",
        "turn_flow": "每回合三选一：① 拿 3 枚不同色或 2 枚同色（堆≥4）宝石；② 买 1 张场上或保留的发展卡；③ 保留 1 张卡并拿 1 枚黄金百搭。",
        "end_scoring": "任一玩家达到 ≥15 分后完成本轮，总分高者胜（平分比卡少）。",
        "tips": "前期优先拿免费打折引擎，别盲目憋高分卡；留意贵族色系防被抢。",
    },
    {
        "title": "花砖物语 (Azul)",
        "min_players": 2,
        "max_players": 4,
        "duration_min": 40,
        "weight": 1.8,
        "tags": ["抽象", "轻策", "推新"],
        "goal": "把瓷砖拼贴到墙面，凑横纵连线与完整色系拿高分。",
        "turn_flow": "轮流从工厂圆盘或中央拿走同色全部瓷砖放入左侧样式行，多余落入地板扣分；全拿完后逐行贴墙计分。",
        "end_scoring": "有人拼满一整横行即结束，结算完整行/列/同色额外加分。",
        "tips": "先手标记 -1 分但能抢关键色；卡对手让对方爆仓落地板很关键。",
    },
    {
        "title": "展翅翱翔 (Wingspan)",
        "min_players": 1,
        "max_players": 5,
        "duration_min": 70,
        "weight": 2.4,
        "tags": ["引擎构筑", "中轻策", "高颜值"],
        "goal": "在森林/草原/湿地栖息地构建鸟类引擎，四轮结束总分最高。",
        "turn_flow": "四选一行动：打出一张鸟牌、获取食物（触发森林棕色能力）、下蛋（触发草原）、抽鸟牌（触发湿地）。",
        "end_scoring": "四轮共 26 动（8/7/6/5），统计鸟分+奖励卡+轮末目标+蛋+缓存食物+塞卡。",
        "tips": "第一轮重食物与抽牌引擎，最后半轮疯狂草原下蛋最稳。",
    },
    {
        "title": "沙丘：帝国 (Dune: Imperium)",
        "min_players": 2,
        "max_players": 4,
        "duration_min": 110,
        "weight": 3.0,
        "tags": ["工放", "DBG", "强互动"],
        "goal": "在厄拉科斯星争霸，率先达到 10 VP 或十轮后分高者胜。",
        "turn_flow": "打出带符号卡牌派特工占格子（拿香料/水/军队/派系声望），之后展示剩余手牌买牌并结算军事冲突。",
        "end_scoring": "冲突奖励 + 派系2级结盟 + 阴谋卡计分，达到 10 分触发终局。",
        "tips": "尽早拿到第三名特工（剑术大师）；水比钱稀缺，别在第一轮就梭哈兵力。",
    },
    {
        "title": "火星改造 (Terraforming Mars)",
        "min_players": 2,
        "max_players": 5,
        "duration_min": 120,
        "weight": 3.2,
        "tags": ["引擎构筑", "科幻", "中重策"],
        "goal": "提升改造度 TR、铺绿地与城市、抢里程碑与奖项。",
        "turn_flow": "每代先买牌（3 MC/张），轮流执行 1~2 个行动（打项目卡/标准项目/抢里程碑/转化温氧），全员 pass 后结算产值。",
        "end_scoring": "温度、氧气、海洋三项全局指标全满当世代结束，TR + 版图 + 卡分 + 里程碑/奖项。",
        "tips": "前期优先抢 8 MC 里程碑性价比极高；产值卡前中期拍，后期直接抢纯分与推指标。",
    },
    {
        "title": "工业革命：伯明翰 (Brass: Birmingham)",
        "min_players": 2,
        "max_players": 4,
        "duration_min": 135,
        "weight": 3.9,
        "tags": ["重策", "经济网路", "BGG第一"],
        "goal": "历经运河与铁路两时代，靠产业翻面与交通连线拿最高分。",
        "turn_flow": "每回合打 2 张牌执行 2 动（建造/铺路/研发/出售/贷款/侦察），消费花钱少者下轮先手。",
        "end_scoring": "运河时代结束计一次连线+翻面产业分（拆 1 级建筑），铁路时代结束再计一次总分。",
        "tips": "啤酒极其紧缺且可跨网免费用自己的；贷款早贷不亏，别卡死煤炭连网。",
    },
    {
        "title": "行动代号 (Codenames)",
        "min_players": 4,
        "max_players": 8,
        "duration_min": 30,
        "weight": 1.3,
        "tags": ["聚会", "联想", "分队"],
        "goal": "两队特工根据队长「1词+1数字」提示，先找出己方全部代号且避开黑衣杀手。",
        "turn_flow": "队长说一个词与关联数 N，队员最多猜 N+1 次；猜中己方继续，中立/对方换边，黑词直接判负。",
        "end_scoring": "率先翻完己方 8/9 张代号卡的队伍立即获胜。",
        "tips": "队长表情管理要稳住，优先给 2~3 连清场词，慎给模糊谐音以免误触黑词。",
    },
    {
        "title": "阿瓦隆 (The Resistance: Avalon)",
        "min_players": 5,
        "max_players": 10,
        "duration_min": 40,
        "weight": 1.8,
        "tags": ["聚会", "阵营推理", "嘴炮"],
        "goal": "好人完成 3 次任务且保护梅林不被刺杀；坏人破坏 3 次任务或终局刺中梅林。",
        "turn_flow": "轮流当队长组队 -> 全员举手表决 -> 出队暗投成功/失败票（好人只能投成功）。",
        "end_scoring": "好人 3 胜后刺客指认梅林，指中则坏人翻盘；坏人 3 胜直接赢。",
        "tips": "梅林切忌发言过于上帝视角；派西维尔盯紧真假梅林的投票分歧。",
    },
)

_COMMITMENT_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?:答应|承诺|说好|回头|记得|抽空|改天|下周|明天|周[一二三四五六日天])?(?:要)?(?:把|给|帮|请)([^，。！？\s]{1,8}?)(?:带|借|发|买|寄|吃|做|看|转|送|修|约)([^，。！？]{2,40})"),
    re.compile(r"(?:借给|发给|寄给|送给|还给)([^，。！？\s]{1,8}?)(?:一份|一个|一套|本|盒)?([^，。！？]{2,36})"),
    re.compile(r"(?:答应|说好)([^，。！？\s]{1,8}?)(?:去|一起|帮忙|周末|下周|明天)([^，。！？]{2,40})"),
)
_DUE_HINT_RE = re.compile(r"(今天|明天|后天|大后天|本周末|周末|下周[一二三四五六日天]?|周[一二三四五六日天]|月底|下个月|\d{4}-\d{2}-\d{2})")


def check_cabinet_capacity(
    db: SimingDB,
    current_boxes: int = 0,
    current_volume_m3: float = 0.0,
    adding_boxes: int = 0,
    adding_volume_m3: float = 0.0,
) -> dict[str, Any]:
    """核查桌游柜 200 盒 / 3.0 m³ 硬上限，给出剩余容量与是否需先出闲鱼腾位预警。"""
    policy = load_policy(db.paths)
    guardrails = policy.get("guardrails") or {}
    boxes_limit = int(guardrails.get("boardgame_cabinet_boxes_limit", 571))
    vol_limit = float(guardrails.get("boardgame_cabinet_volume_m3_limit", 3.0))

    proj_boxes = int(current_boxes) + int(adding_boxes)
    proj_vol = round(float(current_volume_m3) + float(adding_volume_m3), 3)
    rem_boxes = boxes_limit - proj_boxes
    rem_vol = round(vol_limit - proj_vol, 3)

    boxes_pct = round((proj_boxes / boxes_limit) * 100.0, 1) if boxes_limit > 0 else 0.0
    vol_pct = round((proj_vol / vol_limit) * 100.0, 1) if vol_limit > 0 else 0.0

    boxes_exceeded = proj_boxes > boxes_limit
    volume_exceeded = proj_vol > vol_limit
    over_capacity = boxes_exceeded or volume_exceeded

    if over_capacity:
        status = "OVER_CAPACITY"
        msg = (
            f"🚨 桌游柜爆仓预警：预计 {proj_boxes}/{boxes_limit} 盒（{boxes_pct}%）、"
            f"{proj_vol:.2f}/{vol_limit:.2f} m³（{vol_pct}%），已超硬上限！建议先挂闲鱼出旧腾位再入新盒。"
        )
    elif boxes_pct >= 90.0 or vol_pct >= 90.0:
        status = "WARN"
        msg = (
            f"⚠️ 桌游柜接近上限：{proj_boxes}/{boxes_limit} 盒（{boxes_pct}%）、"
            f"{proj_vol:.2f}/{vol_limit:.2f} m³（{vol_pct}%），仅剩 {max(0, rem_boxes)} 盒 / {max(0.0, rem_vol):.2f} m³。"
        )
    else:
        status = "OK"
        msg = (
            f"🟢 桌游柜容量健康：{proj_boxes}/{boxes_limit} 盒（{boxes_pct}%）、"
            f"{proj_vol:.2f}/{vol_limit:.2f} m³（{vol_pct}%），余量 {rem_boxes} 盒 / {rem_vol:.2f} m³。"
        )

    return {
        "ok": True,
        "status": status,
        "boxes_limit": boxes_limit,
        "volume_m3_limit": vol_limit,
        "current_boxes": int(current_boxes),
        "current_volume_m3": round(float(current_volume_m3), 3),
        "projected_boxes": proj_boxes,
        "projected_volume_m3": proj_vol,
        "remaining_boxes": rem_boxes,
        "remaining_volume_m3": rem_vol,
        "boxes_utilization_pct": boxes_pct,
        "volume_utilization_pct": vol_pct,
        "boxes_exceeded": boxes_exceeded,
        "volume_exceeded": volume_exceeded,
        "over_capacity": over_capacity,
        "message": msg,
    }


def upsert_crowdfund(
    db: SimingDB,
    title: str,
    platform: str = "新物集",
    pledge_yuan: Optional[float] = None,
    balance_due_yuan: Optional[float] = None,
    balance_due_at: Optional[str] = None,
    status: str = "PLEDGED",
    notes: Optional[str] = None,
) -> dict[str, Any]:
    """登记或更新新物集/KS/Gamefound/闲鱼桌游众筹与补款节点。"""
    db.init_db()
    clean_title = (title or "").strip()
    if not clean_title:
        raise ValueError("title 不能为空")
    plat = (platform or "新物集").strip()
    st = (status or "PLEDGED").strip().upper()
    ts = iso_now()

    with db.transaction() as conn:
        existing = conn.execute(
            "SELECT * FROM boardgame_crowdfunds WHERE platform = ? AND title = ?",
            (plat, clean_title),
        ).fetchone()
        if existing:
            cf_id = str(existing["cf_id"])
            eff_pledge = float(pledge_yuan) if pledge_yuan is not None else float(existing["pledge_yuan"] or 0.0)
            eff_bal = float(balance_due_yuan) if balance_due_yuan is not None else float(existing["balance_due_yuan"] or 0.0)
            eff_due = balance_due_at.strip() if balance_due_at is not None else str(existing["balance_due_at"] or "")
            eff_notes = notes.strip() if notes is not None else str(existing["notes"] or "")
            conn.execute(
                """
                UPDATE boardgame_crowdfunds
                SET pledge_yuan=?, balance_due_yuan=?, balance_due_at=?, status=?, notes=?, updated_at=?
                WHERE cf_id=?
                """,
                (eff_pledge, eff_bal, eff_due, st, eff_notes, ts, cf_id),
            )
        else:
            cf_id = f"CF-{_db.now_local().strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"
            eff_pledge = float(pledge_yuan) if pledge_yuan is not None else 0.0
            eff_bal = float(balance_due_yuan) if balance_due_yuan is not None else 0.0
            eff_due = balance_due_at.strip() if balance_due_at is not None else ""
            eff_notes = notes.strip() if notes is not None else ""
            conn.execute(
                """
                INSERT INTO boardgame_crowdfunds (
                    cf_id, platform, title, pledge_yuan, balance_due_yuan, balance_due_at, status, notes, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (cf_id, plat, clean_title, eff_pledge, eff_bal, eff_due, st, eff_notes, ts),
            )

    return {
        "ok": True,
        "cf_id": cf_id,
        "platform": plat,
        "title": clean_title,
        "pledge_yuan": eff_pledge,
        "balance_due_yuan": eff_bal,
        "balance_due_at": eff_due,
        "status": st,
        "notes": eff_notes,
        "updated_at": ts,
    }


_CROWDFUND_CLOSED_STATUSES = (
    "SHIPPED",
    "ARRIVED",
    "CANCELLED",
    "CANCELED",
    "PAID",
    "BALANCE_PAID",
    "DELIVERED",
    "DONE",
    "COMPLETED",
    "REFUNDED",
)


def check_upcoming_crowdfund_balances(
    db: SimingDB,
    now: Optional[datetime] = None,
    warn_days: int = 7,
) -> list[dict[str, Any]]:
    """扫描未来 warn_days 天内到期（或已逾期）的桌游众筹补尾款清单。"""
    db.init_db()
    cur_date = (now or _db.now_local()).date()
    placeholders = ",".join("?" for _ in _CROWDFUND_CLOSED_STATUSES)
    conn = db.connect()
    try:
        rows = conn.execute(
            f"""
            SELECT * FROM boardgame_crowdfunds
            WHERE UPPER(status) NOT IN ({placeholders})
              AND balance_due_yuan > 0
              AND balance_due_at != ''
            ORDER BY balance_due_at ASC
            """,
            _CROWDFUND_CLOSED_STATUSES,
        ).fetchall()
    finally:
        conn.close()

    alerts: list[dict[str, Any]] = []
    for r in rows:
        due_str = str(r["balance_due_at"] or "")[:10]
        try:
            due_d = date.fromisoformat(due_str)
        except ValueError:
            continue
        days_left = (due_d - cur_date).days
        if days_left <= int(warn_days):
            overdue = days_left < 0
            urgency = f"已逾期 {abs(days_left)} 天" if overdue else (f"今天截止" if days_left == 0 else f"还剩 {days_left} 天")
            alerts.append(
                {
                    "cf_id": str(r["cf_id"]),
                    "platform": str(r["platform"]),
                    "title": str(r["title"]),
                    "pledge_yuan": float(r["pledge_yuan"]),
                    "balance_due_yuan": float(r["balance_due_yuan"]),
                    "balance_due_at": due_str,
                    "status": str(r["status"]),
                    "days_left": days_left,
                    "overdue": overdue,
                    "alert_text": f"🎲 [{r['platform']}] 《{r['title']}》待补尾款 ¥{float(r['balance_due_yuan']):.2f}（{due_str} · {urgency}）",
                }
            )
    return alerts


def _default_quick_rules(game: dict[str, Any]) -> str:
    title = str(game.get("title") or "未命名桌游")
    goal = str(game.get("goal") or "在限定轮次内达成最高胜利分（VP）。")
    turn_flow = str(game.get("turn_flow") or "按顺时针轮流执行行动阶段 -> 结算阶段 -> 补牌/维护阶段。")
    end_scoring = str(game.get("end_scoring") or "触发终局条件后完成当前轮次，统计版图+卡牌+资源折算总分。")
    tips = str(game.get("tips") or "前期先搭资源引擎，中后期盯紧终点条件切入拿分。")
    return (
        f"📖 【3分钟速查规则 · boardgame_kb】《{title}》\n"
        f"• 适配：{game.get('min_players', 2)}~{game.get('max_players', 4)}人 | 约 {game.get('duration_min', 60)} 分钟 | 重度 {game.get('weight', 2.5)}\n"
        f"• 核心目标：{goal}\n"
        f"• 回合流程：{turn_flow}\n"
        f"• 终局计分：{end_scoring}\n"
        f"• 新手避坑：{tips}"
    )


def plan_game_night(
    db: SimingDB,
    players: int = 4,
    duration_min: int = 90,
    candidates: Optional[list[dict[str, Any]]] = None,
    rules_fn: Optional[Callable[[str], str]] = None,
) -> dict[str, Any]:
    """组织桌游局：按人数与时长选戏 + 生成 QQ 投票卡与内嵌按钮 + 生成 3 分钟速查规则纸。"""
    db.init_db()
    p_cnt = max(1, int(players))
    dur = max(15, int(duration_min))

    # 读取亲友档案中喜欢的游戏做加权
    fav_counts: dict[str, int] = {}
    conn = db.connect()
    try:
        for r in conn.execute("SELECT favorite_games_json FROM friend_profiles").fetchall():
            for g in json.loads(r["favorite_games_json"] or "[]"):
                gl = str(g or "").strip().lower()
                if gl:
                    fav_counts[gl] = fav_counts.get(gl, 0) + 1
    finally:
        conn.close()

    pool = list(candidates) if candidates is not None else [dict(g) for g in DEFAULT_BOARDGAME_CATALOG]
    scored: list[tuple[float, dict[str, Any]]] = []
    for g in pool:
        min_p = int(g.get("min_players", 2))
        max_p = int(g.get("max_players", 4))
        g_dur = int(g.get("duration_min", 60))
        if not (min_p <= p_cnt <= max_p):
            continue
        if g_dur > dur + 20:
            continue
        # 时长贴合度 + 亲友偏好加权
        dur_fit = 100.0 - abs(dur - g_dur) * 0.6
        title_low = str(g.get("title") or "").lower()
        fav_bonus = sum(15.0 * cnt for fav, cnt in fav_counts.items() if fav in title_low)
        scored.append((dur_fit + fav_bonus, g))

    if not scored:
        # 放宽时长，仅按人数兜底
        for g in pool:
            if int(g.get("min_players", 2)) <= p_cnt <= int(g.get("max_players", 4)):
                scored.append((50.0 - abs(dur - int(g.get("duration_min", 60))) * 0.2, g))

    scored.sort(key=lambda x: x[0], reverse=True)
    recs = [item[1] for item in scored[:4]]

    # 构造 QQ 投票卡与 InlineKeyboard
    poll_lines = [f"🎲 桌游局投票（{p_cnt}人 · 预算约 {dur} 分钟）", "点下方按钮投出今晚最想开的一盒："]
    btn_row: list[dict[str, Any]] = []
    for idx, g in enumerate(recs, 1):
        title = str(g.get("title") or f"备选{idx}")
        short = title.split("(")[0].strip()[:12]
        poll_lines.append(
            f"{idx}. **{title}**（{g.get('min_players', 2)}-{g.get('max_players', 4)}人 · {g.get('duration_min', 60)}m · 重度{g.get('weight', 2.0)}）"
        )
        btn_row.append(
            make_button(
                f"vote_{idx}",
                f"{idx}. {short}",
                f"siming:poll_vote:game_night:{idx}",
                visited_label=f"已投 {short}",
                style=1 if idx == 1 else 0,
                group_id="sm_gamenight_poll",
            )
        )
    keyboard = make_keyboard([btn_row]) if btn_row else None

    # 为首选游戏生成 3 分钟速查规则纸
    if recs:
        top_game = recs[0]
        if rules_fn is not None:
            quick_rules = str(rules_fn(str(top_game.get("title") or "")))
        else:
            quick_rules = _default_quick_rules(top_game)
    else:
        quick_rules = f"暂无适配 {p_cnt} 人 / {dur} 分钟的候选桌游。"

    return {
        "ok": bool(recs),
        "players": p_cnt,
        "duration_min": dur,
        "recommendations": recs,
        "poll_card": {
            "text": "\n".join(poll_lines),
            "keyboard": keyboard,
        },
        "quick_rules_sheet": quick_rules,
    }


def extract_commitment_from_text(text: str) -> Optional[dict[str, Any]]:
    """温和捕获口头对外承诺（如「回头把《火星改造》借给老张」「下周请小王吃饭」）。"""
    raw = (text or "").strip()
    if not raw:
        return None
    due_m = _DUE_HINT_RE.search(raw)
    due_hint = due_m.group(1) if due_m else ""

    # 优先匹配「把 X 借给/发给/送给 Y」
    m_ba = re.search(r"把(.{1,24}?)(借给|发给|寄给|送给|还给|带给)([^，。！？\s]{1,8})", raw)
    if m_ba:
        obj, verb, person = m_ba.group(1).strip(), m_ba.group(2), m_ba.group(3).strip()
        return {
            "person": person,
            "promise": f"{verb}{person}{obj}",
            "due_hint": due_hint,
            "source_quote": raw,
        }

    for pat in _COMMITMENT_PATTERNS:
        m = pat.search(raw)
        if m:
            person = m.group(1).strip()
            detail = m.group(2).strip()
            if person and detail:
                return {
                    "person": person,
                    "promise": raw,
                    "due_hint": due_hint,
                    "source_quote": raw,
                }
    return None


def add_commitment(
    db: SimingDB,
    person: str,
    promise: str,
    due_at: str = "",
    source_quote: str = "",
) -> dict[str, Any]:
    """登记一条待兑现的对外人情/口头承诺。"""
    db.init_db()
    p = (person or "").strip()
    pr = (promise or "").strip()
    if not p or not pr:
        raise ValueError("person 和 promise 均不能为空")
    ts = iso_now()
    commit_id = f"SC-{_db.now_local().strftime('%Y%m%d')}-{secrets.token_hex(3).upper()}"
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO social_commitments (
                commit_id, person, promise, due_at, status, source_quote, created_at, closed_at
            ) VALUES (?, ?, ?, ?, 'OPEN', ?, ?, NULL)
            """,
            (commit_id, p, pr, due_at.strip(), source_quote.strip(), ts),
        )
    return {
        "ok": True,
        "commit_id": commit_id,
        "person": p,
        "promise": pr,
        "due_at": due_at.strip(),
        "status": "OPEN",
        "source_quote": source_quote.strip(),
        "created_at": ts,
    }


def close_commitment(db: SimingDB, commit_id: str) -> dict[str, Any]:
    """标记某条对外承诺已兑现（DONE）。"""
    db.init_db()
    cid = (commit_id or "").strip()
    ts = iso_now()
    with db.transaction() as conn:
        row = conn.execute("SELECT * FROM social_commitments WHERE commit_id = ?", (cid,)).fetchone()
        if not row:
            return {"ok": False, "error": f"未找到承诺 {cid}"}
        conn.execute(
            "UPDATE social_commitments SET status = 'DONE', closed_at = ? WHERE commit_id = ?",
            (ts, cid),
        )
    return {
        "ok": True,
        "commit_id": cid,
        "person": str(row["person"]),
        "promise": str(row["promise"]),
        "status": "DONE",
        "closed_at": ts,
    }


def _encrypt_friend_notes(db: SimingDB, plaintext: str) -> str:
    if not plaintext:
        return ""
    f = _get_fernet(db.paths)
    token = f.encrypt(plaintext.encode("utf-8")).decode("ascii")
    return f"{_ENC_PREFIX}{token}"


def _decrypt_friend_notes(db: SimingDB, blob: str) -> str:
    if not blob:
        return ""
    if not blob.startswith(_ENC_PREFIX):
        raise ValueError("friend_profiles.encrypted_notes_blob 缺少认证加密前缀")
    token = blob.removeprefix(_ENC_PREFIX).encode("ascii")
    f = _get_fernet(db.paths)
    return f.decrypt(token).decode("utf-8")


def upsert_friend_profile(
    db: SimingDB,
    name: str,
    notes: str = "",
    birthday: str = "",
    dietary_tags: Optional[list[str]] = None,
    favorite_games: Optional[list[str]] = None,
) -> dict[str, Any]:
    """创建或更新亲友偏好档案，私密备注强制经 siming.crypto Fernet 本地密钥认证加密落盘。"""
    db.init_db()
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("name 不能为空")
    ts = iso_now()

    with db.transaction() as conn:
        existing = conn.execute("SELECT * FROM friend_profiles WHERE name = ?", (clean_name,)).fetchone()
        if existing:
            friend_id = str(existing["friend_id"])
            enc_blob = _encrypt_friend_notes(db, notes) if notes else str(existing["encrypted_notes_blob"] or "")
            new_bday = birthday.strip() if birthday else str(existing["birthday"] or "")
            new_diet = (
                list(dietary_tags)
                if dietary_tags is not None
                else json.loads(existing["dietary_tags_json"] or "[]")
            )
            new_favs = (
                list(favorite_games)
                if favorite_games is not None
                else json.loads(existing["favorite_games_json"] or "[]")
            )
            conn.execute(
                """
                UPDATE friend_profiles
                SET encrypted_notes_blob=?, birthday=?, dietary_tags_json=?, favorite_games_json=?, updated_at=?
                WHERE friend_id=?
                """,
                (
                    enc_blob,
                    new_bday,
                    json.dumps(new_diet, ensure_ascii=False),
                    json.dumps(new_favs, ensure_ascii=False),
                    ts,
                    friend_id,
                ),
            )
        else:
            friend_id = f"FP-{secrets.token_hex(4).upper()}"
            enc_blob = _encrypt_friend_notes(db, notes)
            new_bday = birthday.strip()
            new_diet = list(dietary_tags or [])
            new_favs = list(favorite_games or [])
            conn.execute(
                """
                INSERT INTO friend_profiles (
                    friend_id, name, encrypted_notes_blob, birthday, dietary_tags_json, favorite_games_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    friend_id,
                    clean_name,
                    enc_blob,
                    new_bday,
                    json.dumps(new_diet, ensure_ascii=False),
                    json.dumps(new_favs, ensure_ascii=False),
                    ts,
                ),
            )

    return {
        "ok": True,
        "friend_id": friend_id,
        "name": clean_name,
        "encrypted": bool(enc_blob),
        "birthday": new_bday,
        "dietary_tags": new_diet,
        "favorite_games": new_favs,
        "updated_at": ts,
    }


def get_friend_profile(db: SimingDB, name: str) -> Optional[dict[str, Any]]:
    """按姓名读取并本地解密亲友偏好档案。"""
    db.init_db()
    clean_name = (name or "").strip()
    if not clean_name:
        return None
    conn = db.connect()
    try:
        row = conn.execute("SELECT * FROM friend_profiles WHERE name = ?", (clean_name,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    enc_blob = str(row["encrypted_notes_blob"] or "")
    decrypted_notes = _decrypt_friend_notes(db, enc_blob) if enc_blob else ""
    return {
        "friend_id": str(row["friend_id"]),
        "name": str(row["name"]),
        "notes": decrypted_notes,
        "encrypted": bool(enc_blob),
        "birthday": str(row["birthday"] or ""),
        "dietary_tags": json.loads(row["dietary_tags_json"] or "[]"),
        "favorite_games": json.loads(row["favorite_games_json"] or "[]"),
        "updated_at": str(row["updated_at"] or ""),
    }


def format_social_summary(db: SimingDB, now: Optional[datetime] = None) -> str:
    """汇总桌游众筹尾款预警、待兑现对外承诺与加密亲友档案计数（严禁外泄私密备注）。"""
    db.init_db()
    alerts = check_upcoming_crowdfund_balances(db, now=now, warn_days=14)
    conn = db.connect()
    try:
        open_commits = conn.execute(
            "SELECT * FROM social_commitments WHERE status = 'OPEN' ORDER BY created_at DESC LIMIT 10"
        ).fetchall()
        fp_cnt = int(conn.execute("SELECT COUNT(*) FROM friend_profiles").fetchone()[0])
        cf_cnt = int(conn.execute("SELECT COUNT(*) FROM boardgame_crowdfunds").fetchone()[0])
    finally:
        conn.close()

    lines = [
        f"🎲 桌游社交与人情记忆总览（众筹 {cf_cnt} 项 · 加密亲友手账 {fp_cnt} 份 · 待兑现承诺 {len(open_commits)} 条）"
    ]
    if alerts:
        lines.append("【众筹尾款预警】")
        for a in alerts:
            lines.append(f"• {a['alert_text']}")
    if open_commits:
        lines.append("【待兑现承诺】")
        for c in open_commits:
            due = f"（截止 {c['due_at']}）" if c["due_at"] else ""
            lines.append(f"• [{c['commit_id']}] {c['person']}：{c['promise']}{due}")
    if not alerts and not open_commits:
        lines.append("• 当前无近期尾款预警或待兑现承诺。")
    return "\n".join(lines)
