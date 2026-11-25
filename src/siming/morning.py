"""S2-A / 审批决定 5=B：司命 08:15 四合一统一晨札与「晨札全文」展开引擎。
1. 收口原 Hermes 4 条早间推送（morning-report / morning-brief / ai-daily-morning / bg-morning，均改为 deliver="local" 落盘保归档）；
2. 废除旧 morning_report.py 机械重复的紫微套话，直读 iztro 流日排盘 JSON，以十二宫通用象义 × 四化给出可代入任何事务的提示，仅在司命已知在办事项恰好对应时附带举例（不与任何单一场景耦合）；
3. 08:15 推送精炼高信噪比晨札（绝不截断夜间 apply Axx 口令、绝不吞第 4+ 条夜间暂存），同时将完整详版存入 meta 供 QQ 回复「全文」随时调阅。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from siming.cn_calendar import day_info
from siming.config import load_policy
from siming.db import SHANGHAI_TZ, SimingDB, now_local
from siming.ziwei_ref import load_natal, natal_hint, pick_tianji_quote

DEFAULT_HERMES_CRON_OUTPUT = Path.home() / ".hermes" / "cron" / "output"
DEFAULT_ZIWEI_SCRIPT = Path.home() / ".hermes" / "scripts" / "ziwei_daily.js"

CRON_JOB_IDS = {
    "report": "affd2f6445fe",  # 天气 + Memos + GitHub Star + AI 用量/额度
    "ops": "51419061eb21",     # 运维巡检 + PR 汇总
    "ai": "52f4fdce0602",      # AI 前沿日报
    "bg": "c0d6e1954cda",      # 桌游众筹晨报 + 规则库日报
}

# 十二宫通用取象：只描述该宫管辖的「人生领域」，不预设任何具体在办事项。
# 司命只看得到自己经手的一小部分事务（送装/账单/报销/编码会话等），用户还有大量司命不知道的事，
# 因此解读以通用象义为主体，仅当已知在办事项恰好落在该领域时才附带「如：xxx」举例，绝不硬凑。
_PALACE_THEMES: dict[str, tuple[str, str]] = {
    "命宫": ("self", "自身状态与亲手拍板的事"),
    "兄弟": ("peers", "同辈、搭档与手足往来"),
    "夫妻": ("partner", "伴侣与一对一的合作关系"),
    "子女": ("output", "产出作品、带人与晚辈"),
    "财帛": ("money", "收支、账目与现金流"),
    "疾厄": ("health", "身体、作息与精力"),
    "迁移": ("outside", "出行、对外场合与外部环境"),
    "仆役": ("social", "朋友、同事与外部人脉"),
    "官禄": ("career", "工作事业与手头主线"),
    "田宅": ("home", "居所、家务与大件资产"),
    "福德": ("leisure", "兴趣爱好与心神留白"),
    "父母": ("docs", "文书证照、上级长辈与流程审批"),
}
_PALACE_ALIASES = {"命": "命宫", "交友": "仆役", "奴仆": "仆役", "事业": "官禄", "疾病": "疾厄"}

# 四化通用行动提示（与宫位领域组合成一句，适用于该领域内用户的任何事务）
_SIHUA_ADVICE: dict[str, str] = {
    "化禄": "较顺、易有进展，适合主动推进一步",
    "化权": "宜拍板定调，把边界与时间敲死",
    "化科": "宜梳理记录、把话讲清楚",
    "化忌": "易反复或生误会，多核对一遍、留字据，别硬推",
}

_ANCHOR_DOMAINS = tuple(dict.fromkeys(dom for dom, _ in _PALACE_THEMES.values()))


def _normalize_palace(palace_raw: Optional[str]) -> str:
    p = str(palace_raw or "命宫").strip()
    if p != "命宫":
        p = p.removesuffix("宫")
    return _PALACE_ALIASES.get(p, p)


def _strip_cron_markdown_header(raw_md: str, is_agent_job: bool = False) -> str:
    if not raw_md:
        return ""
    if is_agent_job:
        idx = raw_md.find("## Response")
        if idx >= 0:
            return raw_md[idx + len("## Response") :].strip()
    parts = raw_md.split("\n---\n", 1)
    body = parts[1] if len(parts) == 2 else raw_md
    return body.strip()


def _read_latest_cron_section(
    output_root: Path,
    job_id: str,
    today_str: str,
    is_agent_job: bool = False,
) -> tuple[str, str, bool]:
    """读取指定 cron job 最新输出，返回 (clean_body, date_tag, is_today)。"""
    job_dir = output_root / job_id
    if not job_dir.is_dir():
        return ("", "", False)
    files = sorted(job_dir.glob("*.md"))
    if not files:
        return ("", "", False)

    today_files = [f for f in files if f.name.startswith(today_str)]
    chosen = today_files[-1] if today_files else files[-1]
    is_today = chosen.name.startswith(today_str)
    # 若非今日文件且已超过 30 小时，视为陈旧不混入今日速览
    if not is_today:
        age_h = (now_local().timestamp() - chosen.stat().st_mtime) / 3600.0
        if age_h > 30.0:
            return ("", chosen.stem[:10], False)

    try:
        raw = chosen.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ("", "", False)
    return (_strip_cron_markdown_header(raw, is_agent_job=is_agent_job), chosen.stem[:16], is_today)


def _extract_weather_and_quota_from_report(report_body: str) -> dict[str, str]:
    """从 morning-report 脚本输出中只提取天气、备忘/Star 变化与昨日用量/配额（彻底丢弃其旧版模板化紫微段）。"""
    if not report_body:
        return {}
    pre_ziwei = report_body.split("🔮 今日紫微运势", 1)[0].strip()
    weather = ""
    quota = ""
    usage = ""
    for block in pre_ziwei.split("\n\n"):
        b = block.strip()
        if b.startswith("🌤️"):
            lines = [ln.strip() for ln in b.splitlines() if ln.strip()]
            weather = " ｜ ".join(lines).replace("🌤️ 北京天气：", "")
        elif b.startswith("📊 昨日 AI 用量"):
            for ln in b.splitlines():
                if ln.strip().startswith("合计："):
                    usage = ln.strip().removeprefix("合计：")
        elif b.startswith("💳 额度剩余"):
            q_lines = [ln.strip().lstrip("· ") for ln in b.splitlines()[1:] if ln.strip()]
            quota = "；".join(q_lines[:2])
    return {"weather": weather, "usage": usage, "quota": quota, "pre_ziwei_full": pre_ziwei}


def _clip_clean(text: str, max_len: int) -> str:
    """在词边界/标点边界优雅截断并加省略号，杜绝出现半截英文单词或未闭合括号。"""
    s = re.sub(r"\s+", " ", (text or "").strip())
    if len(s) <= max_len:
        return s
    cut = s[: max_len - 1]
    # 若末尾落在连续 ASCII 字母数字中间，回退到上一个空格或标点
    m_ascii_tail = re.search(r"[A-Za-z0-9_/-]{1,18}$", cut)
    if m_ascii_tail and m_ascii_tail.start() > max_len // 2:
        cut = cut[: m_ascii_tail.start()].rstrip(" ,;:|·—(")
    for open_br, close_br in (("〔", "〕"), ("（", "）"), ("(", ")"), ("【", "】")):
        if cut.count(open_br) > cut.count(close_br):
            idx = cut.rfind(open_br)
            if idx > max_len // 2:
                cut = cut[:idx].rstrip()
    return cut.rstrip(" ，、；：,;:|·") + "…"


def _extract_ops_highlight(ops_body: str) -> str:
    if not ops_body:
        return ""
    action_line = ""
    cron_line = ""
    pr_line = ""
    for ln in ops_body.splitlines():
        s = ln.strip().lstrip("·• ")
        if s.startswith("需要你处理："):
            action_line = s.removeprefix("需要你处理：").strip()
        elif "cron 体检" in s and not cron_line:
            cron_line = s.split("。")[0]
        elif s.startswith("共 ") and "开放" in s and not pr_line:
            pr_line = "PR " + s.split("、")[0]
    parts = [
        p
        for p in (
            _clip_clean(cron_line, 44),
            _clip_clean(pr_line, 42),
            f"待处理:{_clip_clean(action_line, 38)}" if action_line else "",
        )
        if p
    ]
    return " ｜ ".join(parts[:3]) if parts else _clip_clean(ops_body.splitlines()[0], 76)


def _extract_ai_highlights(ai_body: str, max_items: int = 2) -> list[str]:
    if not ai_body:
        return []
    bullets: list[str] = []
    for ln in ai_body.splitlines():
        s = ln.strip()
        if s.startswith("· ") and not s.startswith("· http"):
            item = s[2:].strip()
            if item and not item.startswith("http"):
                bullets.append(_clip_clean(item, 72))
                if len(bullets) >= max_items:
                    break
    return bullets


def _extract_bg_highlights(bg_body: str) -> str:
    if not bg_body:
        return ""
    tldr_items: list[str] = []
    in_tldr = False
    factory_line = ""
    for ln in bg_body.splitlines():
        s = ln.strip()
        if "TLDR" in s:
            in_tldr = True
            continue
        if in_tldr:
            if s.startswith("━━") or not s:
                if tldr_items:
                    in_tldr = False
                continue
            tldr_items.append(_clip_clean(s, 50))
        if s.startswith("• 已建规则库"):
            factory_line = _clip_clean(s.lstrip("• ").strip().split("|")[0].strip(), 32)
    parts: list[str] = []
    if tldr_items:
        parts.append("；".join(tldr_items[:2]))
    if factory_line:
        parts.append(f"规则库:{factory_line}")
    return " ｜ ".join(parts)


def _default_ziwei_runner(target_date: str) -> Optional[dict[str, Any]]:
    if os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return None
    node_bin = Path("/usr/bin/node")
    if not node_bin.is_file() or not DEFAULT_ZIWEI_SCRIPT.is_file():
        return None
    try:
        proc = subprocess.run(
            [str(node_bin), str(DEFAULT_ZIWEI_SCRIPT), target_date],
            capture_output=True,
            text=True,
            timeout=6,
        )
        if proc.returncode != 0:
            return None
        data = json.loads(proc.stdout or "{}")
        return data if data.get("ok") else None
    except Exception:
        return None


def _collect_real_life_anchors(
    db: SimingDB,
    open_cases: list[dict[str, Any]],
    due_waits: list[dict[str, Any]],
    pending_txns_cnt: int,
    bg_highlight: str,
) -> dict[str, list[str]]:
    """收集司命「已知」的在办事项，按领域归类，供四化解读时举例。

    注意：这只是用户全部事务中司命看得到的一小部分。某领域没有已知事项时就留空，
    由解读退回通用象义，绝不用兜底文案硬凑（例如把所有宫位都扯到装修送装上）。
    """
    anchors: dict[str, list[str]] = {dom: [] for dom in _ANCHOR_DOMAINS}

    # 1. 未结单据：按类别/关键词落到对应领域，未识别的通用单据视为「自己手头的事」
    home_kw = ("洗衣机", "幕布", "烟灶", "电视", "空调", "装修", "家电", "安装", "送装", "橱柜", "燃气", "大件", "送货")
    for c in open_cases:
        cat = str(c.get("category") or "")
        title = str(c.get("title") or "")[:36]
        tag = f"[{c['case_id']}]{title}"
        if cat == "appliance" or any(k in title for k in home_kw):
            anchors["home"].append(tag)
        elif cat in ("credit_bill", "subscription_renew", "price_protect"):
            anchors["money"].append(tag)
        elif cat == "reimburse_cursor":
            anchors["docs"].append(tag)
        elif cat in ("pickup", "food_pickup"):
            anchors["outside"].append(tag)
        else:
            anchors["self"].append(tag)

    # 2. 家电送装投影（只归入居所领域，不再外溢到人际/协作等宫位）
    if os.environ.get("SIMING_TEST_OFFLINE") != "1":
        try:
            from siming.install_plan import build_installation_projection

            proj = build_installation_projection(db)
            if proj.get("ok"):
                if proj.get("signed_waiting_install"):
                    anchors["home"].append(f"已签收待约装（{'、'.join(proj['signed_waiting_install'][:2])}）")
                elif proj.get("days_left") is not None:
                    anchors["home"].append(f"家电送装倒计时 {proj['days_left']} 天")
        except Exception:
            pass

    # 3. 报销游标（文书流程）与待核对流水（账目）
    if os.environ.get("SIMING_TEST_OFFLINE") != "1":
        try:
            from siming.ledger.reimburse_audit import get_reimbursement_cursor_status

            cur_st = get_reimbursement_cursor_status(db)
            sb = cur_st.get("staged_local_invoices") or {}
            if sb.get("count"):
                anchors["docs"].append(f"暂存待报销发票 {sb['count']} 张(¥{sb['total_yuan']:.2f})")
            elif cur_st.get("next_portal_cursor"):
                anchors["docs"].append(f"报销门户游标 `{cur_st['next_portal_cursor']}` 起待归集")
        except Exception:
            pass
    if pending_txns_cnt > 0:
        anchors["money"].append(f"{pending_txns_cnt} 笔待核对银行流水")

    # 4. 今日挂起项（自己定下的提醒）与活跃编码会话（工作）
    for w in due_waits:
        q = str(w.get("quote") or w.get("trigger_expr") or "")[:32]
        anchors["self"].append(f"[{w['wait_id']}]{q}")

    if os.environ.get("SIMING_TEST_OFFLINE") != "1" and os.environ.get("SIMING_DRY_RUN_HERDR") != "1":
        policy = load_policy(db.paths)
        herdr_bin = str(policy["coding_plan"].get("herdr_bin") or "/usr/bin/herdr")
        if Path(herdr_bin).is_file():
            try:
                proc = subprocess.run([herdr_bin, "tab", "list"], capture_output=True, text=True, timeout=2.5)
                if proc.returncode == 0:
                    tabs = (json.loads(proc.stdout).get("result") or {}).get("tabs") or []
                    active_names = [
                        str(t.get("label") or "")
                        for t in tabs
                        if t.get("agent_status") in ("working", "idle") and str(t.get("label") or "") not in ("", "1", "2", "3")
                    ]
                    if active_names:
                        anchors["career"].append("编码会话（" + "、".join(active_names[:3]) + "）")
            except Exception:
                pass

    if bg_highlight:
        anchors["leisure"].append(bg_highlight.split("｜")[0].strip()[:44])
    return anchors


def build_grounded_ziwei(
    zw_data: Optional[dict[str, Any]],
    anchors: dict[str, list[str]],
    natal: Optional[dict[str, Any]] = None,
    quote_picker: Optional[Callable[[str], Optional[str]]] = None,
) -> tuple[str, str]:
    """根据 iztro 流日四化生成 (晨札 2 行精炼版, 全文详批版)。

    主体是宫位通用象义 × 四化行动提示，适用于用户在该领域的任何事务（包括司命不知道的）；
    已知在办事项只作「如：xxx」举例，同一事项全篇只举一次，避免整段被某一件事（如装修）霸屏。
    """
    if not zw_data or not zw_data.get("daily"):
        return ("", "")

    daily = zw_data["daily"]
    ganzhi = str(daily.get("ganzhi") or "")
    lunar_s = str(daily.get("lunarDate") or "")
    ming = daily.get("mingGong") or {}
    ming_palace = _normalize_palace(ming.get("natalPalaceName"))
    ming_stars = "、".join(ming.get("stars") or []) or "空宫借对宫"
    sihua = list(daily.get("siHua") or [])

    used: set[str] = set()

    def _theme(palace: str) -> tuple[str, str]:
        return _PALACE_THEMES.get(palace, ("self", "手头的事"))

    def _example(palace: str) -> str:
        dom, _ = _theme(palace)
        for item in anchors.get(dom) or []:
            if item not in used:
                used.add(item)
                return item
        return ""

    def _find(kind: str) -> Optional[dict[str, Any]]:
        return next((x for x in sihua if x.get("type") == kind), None)

    lu, ji = _find("化禄"), _find("化忌")
    lu_pal = _normalize_palace((lu or {}).get("inNatalPalace"))
    ji_pal = _normalize_palace((ji or {}).get("inNatalPalace") or "迁移")
    lu_star = str((lu or {}).get("star") or "")
    ji_star = str((ji or {}).get("star") or "")

    # 详批先算（决定每条四化举哪个例子），精炼版复用同一套举例，保证两版口径一致
    ming_theme = _theme(ming_palace)[1]
    ming_ex = _example(ming_palace)
    per_item: list[tuple[dict[str, Any], str, str]] = []
    for item in sihua:
        pal = _normalize_palace(item.get("inNatalPalace"))
        per_item.append((item, pal, _example(pal)))

    def _ex_of(kind: str) -> str:
        return next((ex for it, _p, ex in per_item if it.get("type") == kind), "")

    def _with_ex(text: str, ex: str) -> str:
        return f"{text}（如「{ex}」）" if ex else text

    header = f"🔮 流日{ganzhi}（命落{ming_palace}[{ming_stars}] · {lu_star}化禄入{lu_pal} / {ji_star}化忌入{ji_pal}）"
    concise_advice = (
        f" · 顺势：{lu_star}化禄落{lu_pal}，{_with_ex(_theme(lu_pal)[1] + '方面' + _SIHUA_ADVICE['化禄'], _ex_of('化禄'))}；"
        f"留神：{ji_star}化忌入{ji_pal}，{_with_ex(_theme(ji_pal)[1] + '方面' + _SIHUA_ADVICE['化忌'], _ex_of('化忌'))}。"
    )
    concise_block = f"{header}\n{concise_advice}"
    # 本命参考包（ziwei-doushu v4.0 本人 13 维解读）：给化忌所落宫位补一句本命层面的长期提醒
    ji_natal = natal_hint(natal, ji_pal) if ji else None
    if ji_natal and ji_natal["advice_short"]:
        concise_block += f"\n · 本命{ji_natal['dim_name']}提醒：{ji_natal['advice_short']}"

    full_lines = [
        f"🔮 紫微流日实事参详 · {zw_data.get('today', '')}（农历{lunar_s} · 流日{ganzhi}）",
        _with_ex(f"• 流日命宫：落本命【{ming_palace}】（星曜：{ming_stars}）— 今日心思易偏向{ming_theme}", ming_ex),
    ]
    for item, pal, ex in per_item:
        st_type = str(item.get("type") or "")
        star = str(item.get("star") or "")
        theme = _theme(pal)[1]
        advice = _SIHUA_ADVICE.get(st_type, "")
        full_lines.append(_with_ex(f"• {star}{st_type}飞入本命【{pal}】（{theme}）→ {advice}", ex))
        nh = natal_hint(natal, pal)
        if nh and nh["headline"]:
            extra = f"；本命建议：{nh['advice_short']}" if st_type == "化忌" and nh["advice_short"] else ""
            full_lines.append(f"   ↳ 本命{nh['dim_name']}定调：{nh['headline']}{extra}")
    if quote_picker:
        q = quote_picker(ji_star) or quote_picker(lu_star)
        if q:
            full_lines.append(f"• {q}")
    full_lines.append("（宫位按通用象义解读，适用于该领域内你的任何事；括号里只是司命已知事项中恰好对得上的举例。）")
    if natal:
        full_lines.append("（本命定调来自紫微斗数开源解读数据集 v4.0 · CC BY 4.0；回「本命」看 13 维目录。）")

    return (concise_block, "\n".join(full_lines))


def build_sunday_health_audit(
    db: SimingDB,
    now: Optional[datetime] = None,
) -> tuple[str, str]:
    """D2-C 周日自省健康观察卡（仅观测汇总，不自动冻结任何模块）：返回 (精炼一行摘要, 全文详版段)。"""
    cur = now or now_local()
    cutoff_7d = (cur - timedelta(days=7)).isoformat(timespec="seconds")
    cutoff_14d = (cur - timedelta(days=14)).isoformat(timespec="seconds")
    conn = db.connect()
    try:
        rs_rows = conn.execute(
            "SELECT rule_id, sent_14d, acted_14d, dismissed_14d, mode FROM rule_stats ORDER BY sent_14d DESC"
        ).fetchall()
        total_sent = sum(int(r["sent_14d"] or 0) for r in rs_rows)
        total_acted = sum(int(r["acted_14d"] or 0) for r in rs_rows)
        total_dismissed = sum(int(r["dismissed_14d"] or 0) for r in rs_rows)
        noisy_rules = [
            f"{r['rule_id']}({r['sent_14d']}推/0点)"
            for r in rs_rows
            if int(r["sent_14d"] or 0) >= 3 and int(r["acted_14d"] or 0) == 0
        ][:4]

        u_row = conn.execute(
            "SELECT COUNT(*) AS cnt, COALESCE(SUM(tokens_in),0) AS tin, COALESCE(SUM(tokens_out),0) AS tout, COALESCE(SUM(cost_cny),0.0) AS cost FROM usage WHERE ts >= ?",
            (cutoff_7d,),
        ).fetchone()
        usage_cnt = int(u_row["cnt"] or 0) if u_row else 0
        usage_tok = int((u_row["tin"] or 0) + (u_row["tout"] or 0)) if u_row else 0
        usage_cost = float(u_row["cost"] or 0.0) if u_row else 0.0

        bad_src_rows = conn.execute(
            "SELECT id, auth_state, freshness FROM sources WHERE auth_state NOT IN ('OK','UNCONFIGURED') OR freshness IN ('ERROR','STALE')"
        ).fetchall()
    finally:
        conn.close()

    zero_use_mods: list[str] = []
    enabled_cnt = 0
    try:
        from siming.modules import list_modules_status

        for m in list_modules_status(db):
            if m.get("enabled"):
                enabled_cnt += 1
                if int(m.get("uses_14d") or 0) == 0:
                    zero_use_mods.append(str(m.get("id") or ""))
    except Exception:
        pass

    rate_s = f"{(total_acted / total_sent * 100):.0f}%" if total_sent > 0 else "—"
    concise = (
        f"📊 周日健康自省（D2观察）：14天推送 {total_sent} 次 / 点击销账 {total_acted} ({rate_s}) ｜ "
        f"启用 {enabled_cnt} 模块（14天零用 {len(zero_use_mods)} 个） ｜ "
        f"近7天后台模型 {usage_cnt} 次({usage_tok} tok, ¥{usage_cost:.2f})"
    )
    full_lines = [
        "📊 周日司命健康自省观察卡（仅观测统计，不自动停用模块）",
        f"• 推送反馈（14d）：发出 {total_sent} 次 · 有效行动 {total_acted} 次 · 嫌吵/少推 {total_dismissed} 次（接受率 {rate_s}）",
    ]
    if noisy_rules:
        full_lines.append(f"• 高推零点击规则观察：{'、'.join(noisy_rules)}")
    if zero_use_mods:
        full_lines.append(
            f"• 14 天零使用已启模块（共 {len(zero_use_mods)}/{enabled_cnt} 个）：{'、'.join(zero_use_mods[:10])}"
            + ("…" if len(zero_use_mods) > 10 else "")
            + "（如需停用某项，直接发 `关闭 <模块名>`）"
        )
    full_lines.append(
        f"• 近 7 天后台 LLM 用量：{usage_cnt} 次调用 · {usage_tok} tokens · 估算 ¥{usage_cost:.2f}"
    )
    if bad_src_rows:
        full_lines.append(
            "• 异常数据源：" + "、".join(f"{r['id']}({r['auth_state']}/{r['freshness']})" for r in bad_src_rows[:6])
        )
    return concise, "\n".join(full_lines)


def build_morning_editions(
    db: SimingDB,
    cron_output_root: Optional[Path] = None,
    ziwei_runner: Optional[Callable[[str], Optional[dict[str, Any]]]] = None,
) -> dict[str, Any]:
    """同时构建「08:15 精炼晨札（concise）」与「可回复展开的完整详版（full + sections）」。"""
    db.init_db()
    today = now_local().date()
    di = day_info(today, db.paths)
    today_prefix = today.isoformat()
    yesterday_prefix = (today - timedelta(days=1)).isoformat()

    is_offline_test = (
        os.environ.get("SIMING_TEST_OFFLINE") == "1"
        and cron_output_root is None
        and ziwei_runner is None
    )
    out_root = cron_output_root or DEFAULT_HERMES_CRON_OUTPUT

    conn = db.connect()
    try:
        ios_src = conn.execute("SELECT * FROM sources WHERE id='ios_hb'").fetchone()
        ios_warn = ""
        if ios_src and ios_src["auth_state"] != "UNCONFIGURED":
            if not ios_src["last_ok_at"] or ios_src["last_ok_at"][:10] < today_prefix:
                ios_warn = "⚠️ iPhone 07:50 快捷指令心跳今日未达（请看一眼自动化是否被 iOS 关闭）"

        open_cases = [
            dict(r)
            for r in conn.execute(
                "SELECT case_id, title, next_step, due_at, status, category FROM cases WHERE status IN ('待处理', '待核实') ORDER BY CASE WHEN due_at IS NULL THEN 1 ELSE 0 END, due_at ASC"
            ).fetchall()
        ]
        due_waits = [
            dict(r)
            for r in conn.execute(
                "SELECT wait_id, trigger_kind, trigger_expr, next_fire_at, action_mode, quote FROM waits WHERE status='WAITING' AND (next_fire_at LIKE ? OR expires_at LIKE ?) ORDER BY next_fire_at ASC",
                (f"{today_prefix}%", f"{today_prefix}%"),
            ).fetchall()
        ]
        cutoff_24h = (now_local() - timedelta(hours=24)).isoformat(timespec="seconds")
        # 兜底推断的挂起项只在建档后的第一份晨札里请你过目一次，之后不再每天重复刷屏
        inferred_confirm = [
            dict(r)
            for r in conn.execute(
                "SELECT wait_id, trigger_expr, next_fire_at, quote, status FROM waits WHERE status IN ('WAITING', 'PROPOSED') AND strength='inferred' AND created_at >= ? ORDER BY created_at DESC LIMIT 6",
                (cutoff_24h,),
            ).fetchall()
        ]
        # 「学到了什么」只在近 24h 真有新规律/候选推断时才出现；没学到就不空刷
        learned_hyps = [
            dict(r)
            for r in conn.execute(
                "SELECT id, claim, status FROM hypotheses WHERE created_at >= ? AND status != 'FORGOTTEN' ORDER BY created_at DESC LIMIT 5",
                (cutoff_24h,),
            ).fetchall()
        ]
        # 完整取出全部夜间暂存消息，绝不 LIMIT 3 丢弃第 4+ 条！
        mq_rows = [
            dict(r)
            for r in conn.execute(
                "SELECT id, ref_id, rule_id, text, created_at FROM outbox WHERE status='MORNING_QUEUE' ORDER BY created_at ASC"
            ).fetchall()
        ]
        reconciled_cnt = conn.execute(
            "SELECT COUNT(*) FROM ledger_txns WHERE occurred_at LIKE ? AND status='已对账'",
            (f"{yesterday_prefix}%",),
        ).fetchone()[0]
        recent_24h_txns = [
            dict(r)
            for r in conn.execute(
                """
                SELECT txn_id, occurred_at, amount_cents, currency, account_alias, kind, merchant, status
                FROM ledger_txns
                WHERE (occurred_at >= ? OR occurred_at LIKE ?) AND status != 'VOID_DUPLICATE'
                ORDER BY amount_cents DESC
                """,
                (cutoff_24h, f"{yesterday_prefix}%"),
            ).fetchall()
        ]
        pending_total_cnt = conn.execute(
            "SELECT COUNT(*) FROM ledger_txns WHERE status IN ('PENDING', '待核实')"
        ).fetchone()[0]
        pending_txns = [
            dict(r)
            for r in conn.execute(
                "SELECT amount_cents, currency, account_alias, merchant, status FROM ledger_txns WHERE status IN ('PENDING', '待核实') ORDER BY occurred_at DESC LIMIT 5"
            ).fetchall()
        ]
        all_sources = [dict(r) for r in conn.execute("SELECT * FROM sources ORDER BY id").fetchall()]
        bad_sources = [
            s
            for s in all_sources
            if s["auth_state"] not in ("OK", "UNCONFIGURED") or s["freshness"] in ("ERROR", "STALE")
        ]
    finally:
        conn.close()

    # 读取 4 条本地早间 cron 产物与紫微排盘（离线单测未注入 fixture 时自动跳过）
    report_body, ops_body, ai_body, bg_body = "", "", "", ""
    rep_today, ops_date, ai_date, bg_date = False, "", "", ""
    zw_data: Optional[dict[str, Any]] = None
    if not is_offline_test:
        if out_root.is_dir():
            report_body, _, rep_today = _read_latest_cron_section(out_root, CRON_JOB_IDS["report"], today_prefix, is_agent_job=False)
            ops_body, ops_date, ops_today = _read_latest_cron_section(out_root, CRON_JOB_IDS["ops"], today_prefix, is_agent_job=True)
            ai_body, ai_date, ai_today = _read_latest_cron_section(out_root, CRON_JOB_IDS["ai"], today_prefix, is_agent_job=False)
            bg_body, bg_date, bg_today = _read_latest_cron_section(out_root, CRON_JOB_IDS["bg"], today_prefix, is_agent_job=False)
            ops_date = "" if ops_today else ops_date[:10]
            ai_date = "" if ai_today else ai_date[:10]
            bg_date = "" if bg_today else bg_date[:10]
        run_zw = ziwei_runner or _default_ziwei_runner
        zw_data = run_zw(today_prefix)

    rep_meta = _extract_weather_and_quota_from_report(report_body)
    # 严禁把昨日陈旧天气冒充今日天气！
    if not rep_today:
        rep_meta["weather"] = ""
    elif not di.is_workday and rep_meta.get("weather"):
        # 周末/法定节假日绝不向用户播报「工作日通勤 / 上班带伞」文案
        rep_meta["weather"] = re.sub(r"工作日通勤[：:、\s]*|上班带伞[，,、\s]*", "", rep_meta["weather"]).strip(" ｜|，,")
    ops_hl = _extract_ops_highlight(ops_body)
    if ops_hl and ops_date:
        ops_hl = f"({ops_date[5:]}) {ops_hl}"
    ai_hls = _extract_ai_highlights(ai_body, max_items=2)
    if ai_hls and ai_date:
        ai_hls[0] = f"({ai_date[5:]}) {ai_hls[0]}"
    bg_hl = _extract_bg_highlights(bg_body)
    if bg_hl and bg_date:
        bg_hl = f"({bg_date[5:]}) {bg_hl}"

    anchors = _collect_real_life_anchors(db, open_cases, due_waits, pending_total_cnt, bg_hl)
    natal = load_natal(db) if zw_data else None
    zw_concise, zw_full = build_grounded_ziwei(
        zw_data,
        anchors,
        natal=natal,
        quote_picker=(lambda star: pick_tianji_quote(db, star, today_prefix)) if zw_data else None,
    )

    policy = load_policy(db.paths)
    tail_exempt = bool((policy.get("vehicle") or {}).get("tail_limit_exempt", False))
    p_header = di.personal_header(tail_limit_exempt=tail_exempt)

    has_external_digest = bool(rep_meta.get("weather") or ops_hl or ai_hls or bg_hl or zw_concise)
    if (
        not ios_warn
        and not open_cases
        and not due_waits
        and not mq_rows
        and not pending_txns
        and not recent_24h_txns
        and not bad_sources
        and not inferred_confirm
        and not learned_hyps
        and not has_external_digest
    ):
        empty_line = f"{p_header} · 今日无待办与异常动账。"
        return {
            "concise": empty_line,
            "full": empty_line,
            "sections": {"date": today_prefix, "all": empty_line, "siming": empty_line},
            "mq_ids": [],
        }

    # === 组装 08:15 精炼推送版（Concise） ===
    header_line = p_header
    if rep_meta.get("weather"):
        header_line += f" ｜ 🌤️ {rep_meta['weather']}"
    lines: list[str] = [header_line]

    # 首行自检摘要
    configured_cnt = sum(1 for s in all_sources if s["auth_state"] != "UNCONFIGURED")
    ok_cnt = configured_cnt - len(bad_sources)
    if bad_sources:
        bad_desc = "、".join(f"{s['id']}({s['auth_state']}/{s['freshness']})" for s in bad_sources[:4])
        lines.append(f"🩺 自检：{ok_cnt}/{configured_cnt} 正常 ｜ 异常：{bad_desc}")
    elif has_external_digest:
        q_note = f" ｜ {rep_meta['quota']}" if rep_meta.get("quota") else ""
        lines.append(f"🩺 自检：{ok_cnt}/{configured_cnt} 源在线正常{q_note}")

    if ios_warn:
        lines.append(ios_warn)

    if open_cases or due_waits:
        lines.append("📌 今日重点")
        for c in open_cases[:6]:
            step = f" — {c['next_step'][:68]}" if c["next_step"] else ""
            lines.append(f" [{c['case_id']}] {c['title']}{step}")
        if len(open_cases) > 6:
            lines.append(f" … 另有 {len(open_cases) - 6} 条待办（回「全文」查看全部）")
        inferred_ids = {w["wait_id"] for w in inferred_confirm}
        for w in [w for w in due_waits if w["wait_id"] not in inferred_ids][:4]:
            lines.append(f" [{w['wait_id']}] ({w['action_mode']}) {w['quote'] or w['trigger_expr']}")

    # 夜间暂存队列：绝不吞消息、绝不截断 apply Axx 口令！
    if mq_rows:
        lines.append(f"🌙 夜间暂存送达（共 {len(mq_rows)} 条）")
        for m in mq_rows:
            txt = str(m["text"] or "").strip()
            has_apply = bool(re.search(r"\bapply\s+A\d{2,6}\b", txt, re.IGNORECASE))
            if has_apply or len(mq_rows) <= 4:
                indented = "\n   ".join(ln.strip() for ln in txt.splitlines() if ln.strip())
                lines.append(f" • {indented}")
            else:
                first_ln = txt.splitlines()[0][:100]
                lines.append(f" • {first_ln}（完整内容见「晨札全文」）")

    if recent_24h_txns or reconciled_cnt or pending_txns:
        cny_24h_spends = [
            r for r in recent_24h_txns if str(r.get("currency") or "CNY").upper() == "CNY" and r.get("kind") == "消费"
        ]
        fx_24h_spends = [
            r for r in recent_24h_txns if str(r.get("currency") or "CNY").upper() != "CNY" and r.get("kind") == "消费"
        ]
        cny_24h_inflows = [
            r for r in recent_24h_txns if str(r.get("currency") or "CNY").upper() == "CNY" and r.get("kind") in ("收入", "退款")
        ]
        if cny_24h_spends or fx_24h_spends or cny_24h_inflows:
            parts_24h: list[str] = []
            if cny_24h_spends:
                tot_y = sum(int(r["amount_cents"]) for r in cny_24h_spends) / 100.0
                top_r = cny_24h_spends[0]
                top_m = f"·{top_r['merchant'][:12]}" if top_r.get("merchant") else ""
                parts_24h.append(
                    f"人民币支出 {len(cny_24h_spends)} 笔共 ¥{tot_y:.2f}（最大：{top_r['account_alias']}{top_m} ¥{top_r['amount_cents']/100:.2f}）"
                )
            if fx_24h_spends:
                tot_fx = sum(int(r["amount_cents"]) for r in fx_24h_spends) / 100.0
                fx_c = str(fx_24h_spends[0].get("currency") or "USD").upper()
                parts_24h.append(f"{fx_c} 支出 {len(fx_24h_spends)} 笔共 {fx_c} {tot_fx:.2f}")
            if cny_24h_inflows:
                tot_in = sum(int(r["amount_cents"]) for r in cny_24h_inflows) / 100.0
                top_in = cny_24h_inflows[0]
                in_m = f"·{top_in['merchant'][:12]}" if top_in.get("merchant") else ""
                parts_24h.append(
                    f"入账/退款 {len(cny_24h_inflows)} 笔共 ¥{tot_in:.2f}（最大：{top_in['account_alias']}{in_m} {top_in['kind']} ¥{top_in['amount_cents']/100:.2f}）"
                )
            lines.append("💳 近24h动账整合：" + "；".join(parts_24h))
        elif pending_txns:
            lines.append(f"💳 动账：昨日无新增刷卡，历史累计 {pending_total_cnt} 笔待月结单核销")
        else:
            lines.append(f"💳 昨日动账：{reconciled_cnt} 笔均已对清")

    if inferred_confirm:
        # D-34：会话兜底扫描只「提议」（PROPOSED），本人回「记下 Wxx」才生效；其它来源的推断项保持原「过目一次」口径
        proposed = [w for w in inferred_confirm if w.get("status") == "PROPOSED"]
        inferred_live = [w for w in inferred_confirm if w.get("status") != "PROPOSED"]
        if proposed:
            items = "；".join(f"[{w['wait_id']}] {w['quote'][:24]}" for w in proposed[:3])
            lines.append(f"📝 可能想让我记的提醒（未登记）：{items}（要就回「记下 Wxx」，不理 72h 后作废）")
        if inferred_live:
            items = "；".join(f"[{w['wait_id']}] {w['quote'][:24]}" for w in inferred_live[:3])
            lines.append(f"📝 兜底待确认：{items}（不需要回「取消 Wxx」）")

    if learned_hyps:
        claims = "；".join(str(h["claim"])[:40] for h in learned_hyps[:3])
        lines.append(f"🧠 近24h新学到 {len(learned_hyps)} 条规律：{claims}（不对就回「忘掉 关键词」）")

    if zw_concise:
        lines.append(zw_concise)

    lab_rep = ""
    try:
        from siming.night_lab import collect_morning_lab_battle_report

        lab_rep = collect_morning_lab_battle_report(db, consume=True)
        if lab_rep:
            lines.append(lab_rep)
    except Exception:
        pass

    if ops_hl or ai_hls or bg_hl:
        lines.append("🗞️ 早间速览")
        if ops_hl:
            lines.append(f" · 🖥 运维/PR：{ops_hl}")
        if ai_hls:
            lines.append(f" · 🤖 AI前沿：{'；'.join(ai_hls)}")
        if bg_hl:
            lines.append(f" · 🎲 桌游/规则库：{bg_hl}")

    sunday_audit_concise, sunday_audit_full = ("", "")
    if today.weekday() == 6:
        try:
            sunday_audit_concise, sunday_audit_full = build_sunday_health_audit(db)
            if sunday_audit_concise:
                lines.append(sunday_audit_concise)
        except Exception:
            sunday_audit_concise, sunday_audit_full = ("", "")

    if has_external_digest or len(open_cases) > 6 or len(mq_rows) > 4:
        lines.append("↩ 引用回复「全文」（或发「晨札全文 / 全文 ai / 全文 桌游 / 全文 运维 / 全文 紫微」）查看详版")
    else:
        lines.append("直接引用回复说人话即可，或发：已办 Sxx / 查 <词> / apply Axx")

    concise_text = "\n".join(lines)

    # === 组装「晨札全文」各分卷（供 QQ 回复「全文」随时调阅） ===
    siming_full_lines = [
        f"📋 司命全量盘点 · {today_prefix}（{p_header}）",
    ]
    if recent_24h_txns:
        siming_full_lines.append(f"\n【近 24 小时全部卡动账明细（共 {len(recent_24h_txns)} 笔，平时静默归集不逐笔打扰）】")
        for tx in sorted(recent_24h_txns, key=lambda r: str(r["occurred_at"]), reverse=True)[:25]:
            c_u = str(tx.get("currency") or "CNY").upper()
            s_u = "¥" if c_u == "CNY" else ("$" if c_u == "USD" else f"{c_u} ")
            m_u = f" · {tx['merchant']}" if tx.get("merchant") else ""
            siming_full_lines.append(
                f"• {str(tx['occurred_at'])[:16]} {tx['account_alias']} {tx['kind']} {s_u}{tx['amount_cents']/100:.2f}{m_u}"
            )
    if rep_meta.get("pre_ziwei_full"):
        siming_full_lines.append(rep_meta["pre_ziwei_full"])
    if open_cases:
        siming_full_lines.append(f"\n【全部未结待办（共 {len(open_cases)} 条）】")
        for c in open_cases:
            due_s = f" (截止 {c['due_at'][:10]})" if c.get("due_at") else ""
            siming_full_lines.append(f"• [{c['case_id']}] {c['title']}{due_s} — {c['next_step']}")
    if due_waits:
        siming_full_lines.append(f"\n【今日挂起项（共 {len(due_waits)} 条）】")
        for w in due_waits:
            siming_full_lines.append(f"• [{w['wait_id']}] ({w['action_mode']}) {w['quote'] or w['trigger_expr']}")
    if mq_rows:
        siming_full_lines.append(f"\n【夜间暂存完整原文（共 {len(mq_rows)} 条）】")
        for m in mq_rows:
            siming_full_lines.append(f"— ({m['created_at'][11:16]}) {m['text']}")
    if lab_rep:
        siming_full_lines.append(f"\n{lab_rep}")
    if sunday_audit_full:
        siming_full_lines.append(f"\n{sunday_audit_full}")

    sec_siming = "\n".join(siming_full_lines)
    sec_ziwei = zw_full or "今日暂无紫微排盘数据。"
    sec_ops = ops_body or "今日暂无早安运维/PR 详版记录。"
    sec_ai = ai_body or "今日暂无 AI 前沿日报详版记录。"
    sec_bg = bg_body or "今日暂无桌游与规则库日报详版记录。"

    full_parts = [
        f"📖 司命晨札全文 · {today_prefix}",
        "━━━━━━━━━━━━━━",
        sec_siming,
    ]
    if zw_full:
        full_parts.extend(["━━━━━━━━━━━━━━", sec_ziwei])
    # v5 零噪音高优摘要：仅在存在真实异常/到期/凌晨交班成果时才追加，绝不空刷
    v5_alerts: list[str] = []
    try:
        from siming.compute_assets import evaluate_compute_guardrails
        from siming.overnight_lab import build_morning_handoff_report
        from siming.social_boardgames import check_upcoming_crowdfund_balances
        from siming.sub_hunter import find_upcoming_charges

        cg = evaluate_compute_guardrails(db, notify=False)
        if cg.get("alerts"):
            v5_alerts.append(f"⚡ 算力告警 {len(cg['alerts'])} 条")
        up_subs = find_upcoming_charges(db, horizon_hours=48)
        if up_subs:
            v5_alerts.append(f"✂️ 48h内订阅扣费 {len(up_subs)} 项")
        ho = build_morning_handoff_report(db)
        if ho.get("count", 0) > 0:
            v5_alerts.append(f"🧪 凌晨实验交班 {ho['count']} 项待阅")
        cf = check_upcoming_crowdfund_balances(db, warn_days=7)
        if cf:
            v5_alerts.append(f"🎲 7天内桌游补款 {len(cf)} 项")
    except Exception:
        pass
    if v5_alerts:
        v5_line = "🧭 v5 态势：" + " ｜ ".join(v5_alerts)
        concise_text = f"{concise_text}\n{v5_line}"
        full_parts.append(v5_line)

    if ops_body:
        full_parts.extend(["━━━━━━━━━━━━━━", sec_ops])
    if ai_body:
        full_parts.extend(["━━━━━━━━━━━━━━", sec_ai])
    if bg_body:
        full_parts.extend(["━━━━━━━━━━━━━━", sec_bg])
    full_text = "\n\n".join(full_parts)

    sections = {
        "date": today_prefix,
        "all": full_text,
        "siming": sec_siming,
        "ziwei": sec_ziwei,
        "ops": sec_ops,
        "ai": sec_ai,
        "bg": sec_bg,
    }
    return {"concise": concise_text, "full": full_text, "sections": sections, "mq_ids": [m["id"] for m in mq_rows]}


def save_morning_editions_to_meta(
    db: SimingDB,
    editions: dict[str, Any],
    conn=None,
    *,
    overwrite_delivered: bool = True,
) -> None:
    sections = editions.get("sections") or {}
    date_str = str(sections.get("date") or now_local().date().isoformat())
    serialized = json.dumps(sections, ensure_ascii=False)
    # 严防白天主动跑 `siming today` 重建快照时覆盖掉 08:15 投递瞬间含夜间暂存原文的权威全文快照！
    if overwrite_delivered or not db.get_meta(f"morning_brief_full:{date_str}"):
        db.set_meta(f"morning_brief_full:{date_str}", serialized, conn=conn)
        db.set_meta("morning_brief_latest_full", serialized, conn=conn)
    else:
        db.set_meta("morning_brief_preview_full", serialized, conn=conn)


def get_morning_full_reply(
    db: SimingDB, section_hint: str = "", target_date: str = ""
) -> str:
    """响应用户在 QQ 回复「全文 / 晨札全文 / 全文 ai / 全文 桌游 / 全文 运维 / 全文 紫微」。"""
    raw = ""
    if target_date:
        raw = db.get_meta(f"morning_brief_full:{target_date}") or ""
    if not raw:
        today_s = now_local().date().isoformat()
        raw = db.get_meta(f"morning_brief_full:{today_s}") or db.get_meta("morning_brief_latest_full") or ""

    sections: dict[str, str] = {}
    if raw:
        try:
            sections = json.loads(raw)
        except Exception:
            sections = {"all": raw}
    if not sections:
        ed = build_morning_editions(db)
        save_morning_editions_to_meta(db, ed)
        sections = ed["sections"]

    db.record_human_use("morning_brief_full", "read_full", ref_id=section_hint or "all")
    hint = (section_hint or "").strip().lower()
    if hint in ("ai", "前沿", "ai日报", "科技"):
        return str(sections.get("ai") or "暂无 AI 前沿日报。")
    if hint in ("bg", "桌游", "规则库", "众筹"):
        return str(sections.get("bg") or "暂无桌游与规则库日报。")
    if hint in ("ops", "运维", "pr", "系统"):
        return str(sections.get("ops") or "暂无运维与 PR 日报。")
    if hint in ("ziwei", "紫微", "紫薇", "流日", "运势"):
        return str(sections.get("ziwei") or "暂无紫微实事详批。")
    if hint in ("siming", "盘点", "待办"):
        return str(sections.get("siming") or "暂无司命盘点。")

    full_all = str(sections.get("all") or "")
    # QQ 单条消息过长（>3800 字）时，优先返回司命全量+紫微+运维全文，并附一键分卷口令提示防网关截断
    if len(full_all) > 3800:
        head_chunks = [
            f"📖 司命晨札全文 · {sections.get('date', '')}（主卷）",
            str(sections.get("siming") or ""),
            "━━━━━━━━━━━━━━",
            str(sections.get("ziwei") or ""),
            "━━━━━━━━━━━━━━",
            str(sections.get("ops") or ""),
            "━━━━━━━━━━━━━━",
            "📌 因 AI 前沿与桌游日报篇幅较长，可继续发：`全文 ai` 或 `全文 桌游` 查看对应完整长文（命令行 `siming today --full` 可一次看全）。",
        ]
        return "\n\n".join(c for c in head_chunks if c)
    return full_all
