"""DeepSeek Super-agent 式多级触发分拣与线程订阅路由器（F01 + F03 + F09 · superagent_triage.py）：
严格参照 `deepseek-ai/dsh-superagent-prototype` 的事件总线与线程直达架构：
- Stage 0（Thread Trigger / 线程订阅直达）：从轻点背面截屏、短信、邮件、转发备忘中提取订单号、运单号、仓库名或事项号；若命中活跃 `subscriptions`（如丰巢快递短信匹配在途抖音冷链包裹），0 token 直达原单核销/升级提醒！
- Stage 1（多级分类 L0 规则快筛 + L1 Coding Plan 结构化分类器）：
  将海量异构投递精准分流为 6 种语义意图：
  1. `BOOKMARK`（GitHub 开源项目 / 技术好文 / 灵感收藏 → 入 `bookmarks` 表与 `bookmarks.md`，关联本机仓库，支持 `试跑 Bxx` 开隔离 worktree，不污染待办！）
  2. `PARCEL`（抖音/拼多多多屏订单截图 → 拆分为独立包裹 + 写 `shop_order_items` + 冷链/冷冻高优预警 + 顺带回填账本 `¥9.90` 等缺失商户！）
  3. `COMMITMENT`（师傅改期 / 挂号预约 / 明确时间约定 → 落 `expectations` 并触发事件链级联重排！）
  4. `LEDGER`（工长报价 / 发票 / 支付回执 → 账本对账归集）
  5. `FACT`（长期个人事实 / 尺寸参数 → 沉淀记忆）
  6. `IGNORE`（纯锁屏 / 系统设置页 / 测试废图 → 静默忽略零打扰）
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from siming.crypto import contains_otp, redact_sensitive
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local

COLD_CHAIN_RE = re.compile(r"(冷链|冷冻|冰品|冰淇淋|榴莲|山竹|生鲜|顺丰冷运|冷藏|需冷冻|冰袋)")
GITHUB_SIGNAL_RE = re.compile(
    r"(github\.com/|GitHub|GitFuB|citHub|涨星榜|热榜\s*Top"
    r"|(?<![\d/])[A-Za-z][A-Za-z0-9_.-]{1,31}\s*/\s*[A-Za-z][A-Za-z0-9_.-]{1,39}(?![/\w]))",
    re.IGNORECASE,
)
ARTICLE_BOOKMARK_SIGNAL_RE = re.compile(
    r"(?:"
    r"(?:https?://)?(?:arxiv\.org/(?:abs|pdf)/|x\.com/[A-Za-z0-9_]{1,24}/status/|twitter\.com/[A-Za-z0-9_]{1,24}/status/)"
    r"|(?:新智元|量子位|机器之心|arXiv|Self-Improvement\s*Memory|最新论文|论文里)"
    r"|(?:(?:帖子|关注|转推|书签|X\s*贴)[\s\S]{0,80}@[A-Za-z0-9_]{2,24}[\s\S]{0,200}(?:开源项目|Personal\s*Agent|Runtime|Claude\s*Code|Codex|Hindsight|MCP|Wiki|Agent|LLM|Paper|arXiv|开源|AI))"
    r")",
    re.IGNORECASE,
)
COURIER_TRACKING_SIGNAL_RE = re.compile(
    r"(?:运单号|快递单号|快递员|正在派件|派送中|已签收|顺丰速运|菜鸟裹裹|中通快递|圆通速递|申通快递|韵达快递|京东快递|极兔速递|丰巢|取件码|驿站)",
    re.IGNORECASE,
)
_NON_GITHUB_URL_RE = re.compile(r"https?://(?!github\.com/)[^\s<>\"'）)】]+", re.IGNORECASE)
_ANY_URL_RE = re.compile(r"https?://[^\s<>\"'）)】]+", re.IGNORECASE)
REPO_SLUG_RE = re.compile(r"\b([A-Za-z0-9_.-]{2,32})\s*/\s*([A-Za-z0-9_.-]{2,40})\b")
_NON_OWNER_SLUG_PREFIXES = frozenset({"github.com", "x.com", "twitter.com", "arxiv.org", "http", "https", "www", "mp.weixin.qq.com", "abs", "pdf", "status"})
ORDER_LIST_SIGNAL_RE = re.compile(r"(全部订单|待收货|运输中|运送中|备货中|查看物流|确认收货|实付款\s*[¥￥])")
RESCHEDULE_SIGNAL_RE = re.compile(r"(燃气|林内|烟机|灶具|洗碗机|橱柜|吊顶|科沃斯|师傅|上门|安装|通气).{0,18}(改到|改约|推迟|顺延|延后|挪到)")
PURE_NOISE_SCREEN_RE = re.compile(r"^(?:[\d:\s%•·<>〉‹$￥5G4GLTEWiFi轻点背面截图发送]+|(?:触控|摇动以撤销|所有快捷指令|使设备振动).*)$", re.DOTALL)

_COMMITMENT_STRONG_RE = re.compile(
    r"(?:咱们?约(?:了|个|好|定|在|到|着)?|约(?:了|个|好|定|在|到|饭|局)"
    r"|已经.{0,12}说(?:的|好)?是|说(?:的|好)是|就(?:定|约|按)"
    r"|定(?:了|在|好)|碰面|见面|集合|开局|组局|聚餐|一起吃|吃[午晚]饭|开会|开个会|面试|体检|复诊|门诊|挂号"
    r"|预约(?:成功)?|上门|来装|量尺|复尺|开场|检票|起飞|发车|演出|看电影|打球|桌游|开黑)"
)
_COMMITMENT_TIME_SUFFIX_RE = re.compile(
    r"(?:\d{1,2}\s*[:：]\s*\d{2}|[一二两三四五六七八九十\d]{1,2}\s*点(?:一刻|三刻|半|\d{1,2})?)\s*"
    r"(?:局|场|见|集合|开始|开场|出发|碰面|吃饭|聚餐|开会|上门|安装|量尺|复诊|检票|到场|了昂?|昂)"
)
_COMMITMENT_HEADER_RE = re.compile(r"^(.{2,24}?)\s*[（(]\s*\d{1,4}\s*[）)]$")
_COMMITMENT_HEADER_TOPIC_RE = re.compile(
    r"(桌游|约局|聚餐|吃饭|羽毛球|篮球|足球|网球|剧本杀|密室|露营|骑行|徒步|开黑|活动|会议|装修|预约|门诊|复诊|挂号|局)"
)
_COMMITMENT_CANCEL_RE = re.compile(r"(取消了?|不去了?|去不了|改天再约|改期再约|作废|散伙|黄了|鸽了)")
_COMMITMENT_NOISE_LINE_RE = re.compile(r"(起床|美睡|睡了一天|睡着|睡觉|加班好晚|刷到|发布于|转发|点赞|评论)")
_COMMITMENT_QUESTION_RE = re.compile(r"(几点|什么时候|行不行|可以吗|要不要|能不能|\?|？)")
_COMMITMENT_CONFIRM_RE = re.compile(r"^(?:可以|好的?|行|没问题|问题不大|收到|OK|ok|妥|成交)")
_ARTICLE_OR_FEED_RE = re.compile(r"(最新论文|研究员|开源项目|发布了一篇|点击关注|@[A-Za-z0-9_]{2,}|#\S+\s+#\S+)")
_LINE_CLOCK_SIGNAL_RE = re.compile(
    r"(?:(?<!\d)\d{1,2}\s*[:：]\s*\d{2}(?!\d)|(?:[一二两三四五六七八九十]{1,2}|\d{1,2})\s*点)"
)
_PAST_TODAY_NARRATIVE_RE = re.compile(r"今天.{0,12}(?:睡|累|忙|加班|吃过|喝了|去了|买了|写了|干了|玩了|跑了|考了|看了).{0,6}了")


def _match_related_local_projects(text: str) -> list[str]:
    low = text.lower()
    matched: list[str] = []
    if any(k in low for k in ("光猫", "桥接", "宽带", "rb5009", "router", "openwrt", "haos", "ung220", "pon", "wifi")):
        matched.append("deco/新房网络(RB5009+HAOS)")
    if any(k in low for k in ("记账", "现金流", "净资产", "理财", "sure", "ledger", "finance")):
        matched.append("siming/quiet_cfo(个人账本)")
    if any(k in low for k in ("agent", "claude", "mem", "skill", "mcp", "codex", "workflow", "code review")):
        matched.append("siming/agent底座")
    if any(k in low for k in ("voice", "tts", "persona", "对话", "角色")):
        matched.append("personas(语音与角色)")
    if any(k in low for k in ("game", "历史地图", "江山", "数值", "卡牌")):
        matched.append("indie_game(游戏与演化)")
    return matched


_ORDER_BOUNDARY_RE = re.compile(
    r"^(?:确认收货|去评价|再次购买|再次购头|删除订单|申请售后|申请退款|催发货|修改地址|"
    r"Q?\s*实况测评推荐.*|火推荐.*|待收货/使用\d*|运送中\d*|待发货\d*|全部订单|包裹工具|包裹|筛选|TE|团购\s*服务)$"
)
_ORDER_IGNORE_UI_RE = re.compile(
    r"^(?:更多|售|申请售后|查看物流|确认收货|删除订单|再次购买|再次购头|去评价|申请退款|催发货|修改地址|"
    r"待发货\d*|待支付|运送中\d*|运输中|备货中|待收货(?:/使用\d*)?|全部订单|全部|商城|团购|服务|服务完成|交易成功|默认不可退|"
    r"7天无理由.*|含运费险.*|实付款.*|合计.*|[¥￥·•]\s*\d+.*|[xX×]\d+|"
    r"山直播.*|轻[：:]?|农料院.*|口添加|无担|果汁发酵|\d+%|轻咸酥脆.*|三大擦拭体验.*|酸重庆.*|0g（.*|解锁单本|工具|品0|69\+|70\+|"
    r"轻点背面|截图发送|Q\s*商品名.*)$"
)


def _resolve_parcel_eta(ship_line: str, as_of: date) -> tuple[str, str]:
    """从订单物流文案中解析绝对日期 (eta_date ISO, eta_kind='deliver'|'ship'|'')，避免相对『今天/明天』隔天腐烂。"""
    s = (ship_line or "").strip()
    if not s:
        return "", ""
    eta_kind = "ship" if ("发货" in s or ("备货中" in s and "送达" not in s)) else "deliver"
    m_md = re.search(r"(?:预计\s*)?(?:(\d{4})[-年])?(\d{1,2})[-月](\d{1,2})日?", s)
    if m_md:
        yr = int(m_md.group(1)) if m_md.group(1) else as_of.year
        mo, dy = int(m_md.group(2)), int(m_md.group(3))
        try:
            dt = date(yr, mo, dy)
            if not m_md.group(1) and (dt - as_of).days < -180:
                dt = date(yr + 1, mo, dy)
            return dt.isoformat(), eta_kind
        except ValueError:
            pass
    if re.search(r"预计.*(?:今天|今日)", s):
        return as_of.isoformat(), eta_kind
    if re.search(r"预计.*(?:明天|明日)", s):
        return (as_of + timedelta(days=1)).isoformat(), eta_kind
    if re.search(r"预计.*后天", s):
        return (as_of + timedelta(days=2)).isoformat(), eta_kind
    return "", eta_kind if ("备货中" in s or "发货" in s) else ""


def _clean_parcel_title_line(ln: str) -> str:
    s = re.sub(r"[.\s]*[¥￥]\s*\d+(?:\.\d{1,2})?\s*$", "", ln or "").strip()
    s = re.sub(r"^[·•\s]+", "", s).strip()
    return s


def extract_douyin_or_ecom_parcels(text: str, as_of_date: Optional[date] = None) -> list[dict[str, Any]]:
    """从抖音商城/淘宝/拼多多订单列表 OCR 文本中按独立订单块切分，提取每笔商品、实付金额、绝对预计日期与冷链属性。"""
    as_of = as_of_date or now_local().date()
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    pay_re = re.compile(r"(?:实付款?|合计)\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)")
    pay_indices = [i for i, ln in enumerate(lines) if pay_re.search(ln)]

    parcels: list[dict[str, Any]] = []
    for k, idx in enumerate(pay_indices):
        ln = lines[idx]
        m_pay = pay_re.search(ln)
        if not m_pay:
            continue
        amount_yuan = float(m_pay.group(1))
        prev_pay = pay_indices[k - 1] if k > 0 else -1
        next_pay = pay_indices[k + 1] if k + 1 < len(pay_indices) else len(lines)

        # 向前回溯：遇到上一笔订单按钮/分隔栏立即停，严防跨订单串行！
        seg_start = idx
        floor = max(0, prev_pay + 1, idx - 12)
        for j in range(idx - 1, floor - 1, -1):
            if _ORDER_BOUNDARY_RE.match(lines[j]):
                break
            seg_start = j
        window_before = lines[seg_start : idx + 1]

        # 向后扫描：遇到下一笔订单起点（推荐条/店铺名）或本单结束按钮即停，绝不吸入下一笔订单的冷链/运输中状态！
        window_after: list[str] = []
        ceil = min(next_pay, idx + 5)
        for j in range(idx + 1, ceil):
            cand = lines[j]
            if _ORDER_BOUNDARY_RE.match(cand):
                if cand in ("去评价", "确认收货", "申请退款", "催发货"):
                    window_after.append(cand)
                break
            if cand.endswith(("＞", ">", "〉")) and not re.search(r"(运输中|已到达|已离开|预计|备货中)", cand):
                break
            window_after.append(cand)

        own_block = window_before + window_after
        ctx_block = "\n".join(own_block)

        is_completed = any("服务完成" in x or "交易成功" in x or "去评价" in x for x in own_block)
        ship_line = next(
            (x for x in window_after + window_before if re.search(r"(运输中|已到达|已离开|预计.{1,10}(?:送达|发货)|备货中)", x)),
            "",
        )
        eta_date, eta_kind = _resolve_parcel_eta(ship_line, as_of)

        shop_candidates = [
            x.rstrip("＞>〉 ")
            for x in window_before[:-1]
            if (x.endswith(("＞", ">", "〉")) or "旗舰店" in x or "专卖店" in x)
            and not _ORDER_BOUNDARY_RE.match(x)
            and "推荐" not in x
        ]
        shop_name = shop_candidates[-1] if shop_candidates else "抖音商城"

        # 在本单内按从上到下顺序挑选真实商品名（优先带【】/［］，并跳过顶部截断的纯 UI 残片）
        raw_cands: list[str] = []
        for x in window_before[:-1]:
            if _ORDER_IGNORE_UI_RE.match(x):
                continue
            if x.endswith(("＞", ">", "〉")) or "旗舰店" in x or "专卖店" in x:
                continue
            if "推荐" in x or "品牌" in x:
                continue
            cleaned = _clean_parcel_title_line(x)
            if len(cleaned) < 4 or _ORDER_IGNORE_UI_RE.match(cleaned):
                continue
            if re.fullmatch(r"[\d.¥￥×xX\s+-]+", cleaned):
                continue
            raw_cands.append(cleaned)

        # 截屏顶部残余的“合计¥9.90 + 去评价”既无店名也无商品名，直接跳过
        if not raw_cands and not shop_candidates and is_completed:
            continue

        bracket_cands = [c for c in raw_cands if any(b in c for b in ("【", "［", "["))]
        if bracket_cands:
            base_title = bracket_cands[0]
            b_pos = raw_cands.index(base_title)
            if base_title.endswith(("…", ".", "...")) and b_pos + 1 < len(raw_cands):
                spec = raw_cands[b_pos + 1]
                if not spec.startswith(("【", "［")):
                    base_title = f"{base_title.rstrip('….')} {spec}"
            item_name = base_title
        elif raw_cands:
            base_title = raw_cands[0]
            if base_title.endswith(("…", ".", "...")) and len(raw_cands) > 1:
                base_title = f"{base_title.rstrip('….')} {raw_cands[1]}"
            item_name = base_title
        elif shop_candidates:
            item_name = shop_name
        else:
            continue

        # 本单未完结且（有物流/备货信息或非纯“合计”已完成单）才算在途
        is_in_transit = (not is_completed) and (bool(ship_line) or any("待收货" in x or "运输中" in x for x in window_before))
        is_cold = bool(COLD_CHAIN_RE.search(ctx_block))

        parcels.append(
            {
                "shop": shop_name[:40],
                "item_name": item_name[:64],
                "amount_yuan": round(amount_yuan, 2),
                "amount_cents": int(round(amount_yuan * 100)),
                "shipping_note": ship_line[:80],
                "eta_date": eta_date,
                "eta_kind": eta_kind,
                "is_in_transit": is_in_transit,
                "is_cold_chain": is_cold,
            }
        )
    # 同一张图同商品名+金额去重
    deduped: dict[str, dict[str, Any]] = {}
    for p in parcels:
        k = f"{p['item_name'][:16]}|{p['amount_cents']}"
        deduped[k] = p
    return list(deduped.values())


def _sync_bookmarks_markdown(db: SimingDB) -> Path:
    md_path = db.paths.data_dir / "bookmarks.md"
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM bookmarks ORDER BY created_at DESC LIMIT 200").fetchall()
    finally:
        conn.close()

    lines = [
        "# 司命灵感与开源收藏夹（Super-agent 自动分拣沉淀）",
        "",
        "> 轻点手机背面或转发图文到司命时，识别为收藏类的工具/仓库会自动归入此处，不污染待办列表；在 QQ 发 `搜收藏 <关键词>` 或 `试跑 <编号>` 可直接调用。",
        "",
    ]
    for r in rows:
        rel = ", ".join(json.loads(r["related_projects_json"] or "[]"))
        url_part = f" · {r['url']}" if r["url"] else ""
        rel_part = f" （关联：`{rel}`）" if rel else ""
        lines.append(f"- **[{r['bookmark_id']}] {r['title']}**{url_part}{rel_part}")
        if r["summary"]:
            lines.append(f"  - {r['summary']}")
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return md_path


def save_bookmark_from_drop(
    db: SimingDB,
    text: str,
    title_override: str = "",
    source_event_id: str = "",
    kind: str = "github",
) -> dict[str, Any]:
    """将 GitHub 项目、X 技术帖或论文好文灵感沉淀入 `bookmarks` 表并同步 `bookmarks.md`。"""
    db.init_db()
    clean = redact_sensitive((text or "").strip(), db.paths)
    slug = ""
    url = ""
    # 兼容不带 https:// 前缀及重复前缀的 OCR 文本 `github.com/github.com/owner/repo`，彻底根治双重前缀！
    norm_gh = re.sub(r"(?:https?://)?(?:github\.com/\s*)+", "https://github.com/", clean, flags=re.IGNORECASE)
    m_url = re.search(r"https?://github\.com/\s*([A-Za-z0-9_.-]+)\s*/\s*([A-Za-z0-9_.-]+)", norm_gh, re.IGNORECASE)
    if m_url:
        owner = m_url.group(1).strip()
        repo = m_url.group(2).strip().rstrip(".,;:)")
        if repo.lower().endswith(".git"):
            repo = repo[:-4]
        if owner.lower() not in _NON_OWNER_SLUG_PREFIXES and repo:
            slug = f"{owner}/{repo}"
            url = f"https://github.com/{slug}"
    else:
        # 先提取非 GitHub 的原始规范 URL（如 arXiv / X 帖子），严防把 arxiv.org/abs/2501.12345 或 x.com/user/status/123 误识别为 GitHub repo！
        m_other_url = _NON_GITHUB_URL_RE.search(clean)
        if m_other_url:
            url = m_other_url.group(0).rstrip(".,;:)")
        else:
            m_bare_arxiv = re.search(r"\barxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", clean, re.IGNORECASE)
            m_bare_x = re.search(r"\b(?:x|twitter)\.com/([A-Za-z0-9_]{1,24}/status/\d+)", clean, re.IGNORECASE)
            if m_bare_arxiv:
                url = f"https://arxiv.org/abs/{m_bare_arxiv.group(1)}"
            elif m_bare_x:
                url = f"https://x.com/{m_bare_x.group(1)}"
        # 仅在剥离所有 URL 后的正文里扫描裸 `owner/repo` 仓库名，避免把 URL 路径段误当成 GitHub 仓库
        slug_scan_text = _ANY_URL_RE.sub(" ", clean)
        slug_scan_text = re.sub(r"\b(?:arxiv\.org|x\.com|twitter\.com|mp\.weixin\.qq\.com)/\S+", " ", slug_scan_text, flags=re.IGNORECASE)
        for m_s in REPO_SLUG_RE.finditer(slug_scan_text):
            owner, repo = m_s.group(1).strip(), m_s.group(2).strip().rstrip(".,;:)")
            if repo.lower().endswith(".git"):
                repo = repo[:-4]
            if owner.lower() in _NON_OWNER_SLUG_PREFIXES or "." in owner:
                continue
            cand = f"{owner}/{repo}"
            if cand.lower() in ("01/01", "http/1.1"):
                continue
            if re.match(r"^\d+/\d+$", cand):
                continue
            slug = cand
            url = f"https://github.com/{slug}"
            break

    meaningful_lines = [
        ln.strip()
        for ln in clean.splitlines()
        if len(ln.strip()) >= 4
        and not re.match(r"^(?:\d{1,2}:\d{2}\d?|轻点背面|截图发送|艇田发迓|GitHub|citHub.*|GitFuB.*|本周|今日|帖子|关注|分享|\d+)$", ln.strip(), re.IGNORECASE)
    ]
    eff_kind = "github" if slug else (kind or "article")
    title = title_override or (f"GitHub · {slug}" if slug else (meaningful_lines[0][:56] if meaningful_lines else "灵感收藏"))
    summary = " / ".join(meaningful_lines[:3])[:180]
    related = _match_related_local_projects(clean)
    ts = iso_now()

    with db.transaction() as conn:
        ex = None
        if slug:
            ex = conn.execute(
                "SELECT bookmark_id, title, repo_slug, url FROM bookmarks WHERE lower(repo_slug)=lower(?)",
                (slug,),
            ).fetchone()
        elif url:
            ex = conn.execute(
                "SELECT bookmark_id, title, repo_slug, url FROM bookmarks WHERE lower(url)=lower(?)",
                (url,),
            ).fetchone()
        else:
            ex = conn.execute(
                "SELECT bookmark_id, title, repo_slug, url FROM bookmarks WHERE lower(kind)=lower(?) AND lower(title)=lower(?)",
                (eff_kind, title),
            ).fetchone()
        if ex:
            return {
                "ok": True,
                "duplicate": True,
                "bookmark_id": ex["bookmark_id"],
                "title": ex["title"],
                "repo_slug": str(ex["repo_slug"] or slug),
                "url": str(ex["url"] or url),
                "related": related,
            }
        bid = db.next_seq_id(conn, "B")
        conn.execute(
            """
            INSERT INTO bookmarks (
                bookmark_id, kind, title, url, repo_slug, summary,
                related_projects_json, tags_json, source_event_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, '[]', ?, ?)
            """,
            (bid, eff_kind, title, url, slug, summary, json.dumps(related, ensure_ascii=False), source_event_id, ts),
        )
        # 注册 repo 线程订阅（后续若用户提该 repo 名可直接关联）
        if slug:
            conn.execute(
                """
                INSERT INTO subscriptions (sub_key, target_kind, target_id, state, created_at, metadata_json)
                VALUES (?, 'bookmark', ?, 'ACTIVE', ?, ?)
                ON CONFLICT(sub_key) DO UPDATE SET target_id=excluded.target_id, state='ACTIVE'
                """,
                (f"repo:{slug.lower()}", bid, ts, json.dumps({"title": title}, ensure_ascii=False)),
            )

    _sync_bookmarks_markdown(db)
    return {"ok": True, "bookmark_id": bid, "title": title, "repo_slug": slug, "url": url, "related": related, "summary": summary}


def match_pickup_sms_to_tracked_parcel(
    db: SimingDB,
    sms_text: str,
    *,
    site_alias: str = "",
) -> Optional[dict[str, Any]]:
    """Thread Trigger：仅在存在真实匹配证据（运单/尾号命中，或短信自身含冷链/生鲜词）时才关联在途包裹；
    严禁把无运单号、无冷链词的普通丰巢/驿站短信盲目硬连到在途冷链单！"""
    db.init_db()
    txt = (sms_text or "").strip()
    if not txt:
        return None
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT case_id, title, next_step, context_json, due_at
            FROM cases
            WHERE status = '待处理' AND category = 'parcel'
            ORDER BY created_at DESC
            LIMIT 15
            """
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        return None

    m_tail = re.search(r"(?:尾号|运单号|单号)[：:\s]*([A-Za-z0-9]{4,14})", txt)
    sms_tail = m_tail.group(1).strip() if m_tail else ""
    sms_is_cold = bool(COLD_CHAIN_RE.search(txt))
    sms_is_sf = "顺丰" in txt
    sms_is_generic_locker = bool(re.search(r"(丰巢|菜鸟驿站|妈妈驿站|兔喜|近邻宝|熊猫快收)", txt)) and not sms_is_cold

    # 1. 最高优先级：运单/订单尾号精确命中
    if sms_tail:
        for r in rows:
            t = str(r["title"] or "")
            ns = str(r["next_step"] or "")
            cj = str(r["context_json"] or "")
            if sms_tail in t or sms_tail in ns or sms_tail in cj:
                is_cold = bool(COLD_CHAIN_RE.search(t + " " + ns))
                return {
                    "case_id": r["case_id"],
                    "title": t,
                    "is_cold_chain": is_cold,
                    "match_confidence": "verified_waybill",
                }

    # 2. 若短信自身明确含冷链/生鲜/冷冻词，且短信地点与包裹站点不冲突，关联最匹配的冷链在途单
    if sms_is_cold:
        best_cold: Optional[dict[str, Any]] = None
        for r in rows:
            t = str(r["title"] or "")
            ns = str(r["next_step"] or "")
            try:
                ctx = json.loads(r["context_json"] or "{}")
            except Exception:
                ctx = {}
            p_site = str(ctx.get("site_alias") or "")
            if site_alias and p_site and site_alias != p_site:
                continue
            if site_alias == "新房" and not p_site:
                continue
            is_cold = bool(ctx.get("is_cold_chain")) or bool(COLD_CHAIN_RE.search(t + " " + ns))
            if not is_cold:
                continue
            cand = {
                "case_id": r["case_id"],
                "title": t,
                "is_cold_chain": True,
                "match_confidence": "cold_sms",
            }
            if sms_is_sf and "顺丰" in (t + " " + ns):
                return cand
            if best_cold is None:
                best_cold = cand
        if best_cold is not None:
            return best_cold

    # 3. 普通快递柜/驿站短信且不含冷链词、无运单号：绝不盲猜！
    if sms_is_generic_locker:
        return None
    return None


def _extract_date_from_line(ln: str, base_date: date, now_dt: datetime) -> Optional[tuple[date, float]]:
    """从单行文本提取日期及优先级：(resolved_date, priority)。跳过过去时态的『今天美睡了一天』等闲聊。"""
    from siming.cn_calendar import CN_WEEKDAY_MAP

    s = (ln or "").strip()
    if not s:
        return None

    m_ymd = re.search(r"(20\d{2})\s*[-年/.]\s*(\d{1,2})\s*[-月/.]\s*(\d{1,2})\s*[日号]?", s)
    if m_ymd:
        try:
            return date(int(m_ymd.group(1)), int(m_ymd.group(2)), int(m_ymd.group(3))), 5.0
        except ValueError:
            pass

    m_md_cn = re.search(r"(?<!\d)(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", s)
    m_md_sep = re.search(r"(?<![\d¥￥.])(1[0-2]|0?[1-9])[/](3[01]|[12]\d|0?[1-9])(?!\d|/)", s)
    m_md_dot = (
        re.search(r"(?<![\d¥￥.])(1[0-2]|0?[1-9])\.(3[01]|[12]\d|0?[1-9])(?!\d|\.)", s)
        if re.search(r"(约|定|见|碰面|上门|安装|挂号|复诊|局|场|上午|下午|晚上|点)", s)
        else None
    )
    m_md = m_md_cn or m_md_sep or m_md_dot
    if m_md:
        mo, da = int(m_md.group(1)), int(m_md.group(2))
        for yr in (now_dt.year, now_dt.year + 1):
            try:
                cand_d = date(yr, mo, da)
            except ValueError:
                continue
            if cand_d >= base_date or yr == now_dt.year + 1:
                return cand_d, 4.0

    if "大后天" in s:
        return base_date + timedelta(days=3), 3.0
    if "后天" in s:
        return base_date + timedelta(days=2), 3.0
    if "明天" in s or "明早" in s or "明晚" in s:
        return base_date + timedelta(days=1), 3.0

    m_next_wd = re.search(r"下周([一二三四五六日天])", s)
    if m_next_wd:
        target_wd = CN_WEEKDAY_MAP[m_next_wd.group(1)]
        return base_date + timedelta(days=(7 - base_date.weekday()) + target_wd), 2.5
    if "下周" in s:
        return base_date + timedelta(days=7), 2.0

    m_wd = re.search(r"(?:本?周|星期|礼拜)([一二三四五六日天])", s)
    if m_wd:
        target_wd = CN_WEEKDAY_MAP[m_wd.group(1)]
        diff = (target_wd - base_date.weekday()) % 7
        return base_date + timedelta(days=diff), 2.5

    if "周末" in s:
        diff = (5 - base_date.weekday()) % 7
        return base_date + timedelta(days=diff), 2.2

    if ("今晚" in s or "今早" in s or "今天" in s) and not _PAST_TODAY_NARRATIVE_RE.search(s):
        return base_date, 1.0
    return None


def extract_commitment_from_drop(
    text: str,
    *,
    ref_dt: Optional[datetime] = None,
    paths: Optional[Any] = None,
) -> Optional[dict[str, Any]]:
    """从手机截屏（微信/QQ 约局聊天、挂号/预约确认页、约上门聊天等）精准提取约定事项、绝对时刻与提前提醒点。

    核心防呆：
    1. 自动剥离 iOS 状态栏钟点与微信聊天分隔时间戳（如独立行 `20:14`、`昨天 23:05`）；
    2. 逐行按约定语义打分并按 `(hour, minute)` 聚合共识（如 `咱们约个四点局` + `已经和迪迪子说的是四点了昂`
       完胜同屏闲聊 `七点起床` / `他四点一刻到家昂`）；
    3. 过滤文章/推文/取消句，避免误把普通资讯或已取消的约局设成闹钟。
    """
    from siming.cn_calendar import _EXPLICIT_AM_RE, _extract_time_of_day, logical_today, strip_chat_divider_timestamps

    raw_clean = (text or "").strip()
    if not raw_clean:
        return None
    stripped_ts = strip_chat_divider_timestamps(raw_clean)
    lines = [
        ln.strip()
        for ln in stripped_ts.splitlines()
        if ln.strip()
        and not re.match(
            r"^(?:[\d\s$￥%•·<>〉‹5G4GLTEWiFi]+|轻点背面|截图发送|艇田发迓|发给司命)$",
            ln.strip(),
            re.IGNORECASE,
        )
    ]
    if not lines:
        return None
    joined = "\n".join(lines)

    # 排除明显资讯/论文/社交信息流（除非正文含明确本人约定短语）
    if _ARTICLE_OR_FEED_RE.search(joined) and not re.search(
        r"(咱们?约|约了|约个|约好|说的是|定在|见面|碰面|预约成功|上门|来装|量尺|复诊)",
        joined,
    ):
        return None

    now_dt = (ref_dt or now_local()).astimezone(SHANGHAI_TZ)
    base_date = logical_today(now_dt)

    # 识别顶部群聊标题（如 `周末桌游局（7）` -> `周末桌游局`）
    group_title = ""
    header_is_appt = False
    for hdr_ln in lines[:2]:
        m_hdr = _COMMITMENT_HEADER_RE.match(hdr_ln)
        if m_hdr:
            group_title = m_hdr.group(1).strip()
            header_is_appt = bool(_COMMITMENT_HEADER_TOPIC_RE.search(group_title))
            break

    # 预先判断整图是否包含未来日期线索（用于无上午/下午标注的 `四点`/`3点` 推断下午）
    all_line_dates: list[tuple[int, date, float]] = []
    for idx, ln in enumerate(lines):
        d_hit = _extract_date_from_line(ln, base_date, now_dt)
        if d_hit is not None:
            all_line_dates.append((idx, d_hit[0], d_hit[1]))
    has_any_future_day = any(d > base_date for _, d, _ in all_line_dates)

    # 整图是否已有强约定背景（如群名含桌游局/约局，或聊天里有人提量尺/复诊/碰面/预约）
    whole_has_strong_appt = header_is_appt or bool(_COMMITMENT_STRONG_RE.search(joined))
    if not whole_has_strong_appt and not _COMMITMENT_TIME_SUFFIX_RE.search(joined):
        return None

    # 逐行抽取时刻并按约定置信度打分
    buckets: dict[tuple[int, int], dict[str, Any]] = {}
    cancelled_any_strong = False
    latest_strong_idx = -1
    latest_cancel_idx = -1

    for idx, ln in enumerate(lines):
        if _COMMITMENT_CANCEL_RE.search(ln):
            latest_cancel_idx = idx
            if _LINE_CLOCK_SIGNAL_RE.search(ln) or _COMMITMENT_STRONG_RE.search(ln) or header_is_appt:
                cancelled_any_strong = True
            continue
        if _COMMITMENT_NOISE_LINE_RE.search(ln) and not _COMMITMENT_STRONG_RE.search(ln):
            continue
        if not _LINE_CLOCK_SIGNAL_RE.search(ln):
            continue

        # 相邻上一行若出现明确上午/凌晨且同钟点，避免把确认句里的裸『6点』误翻下午
        prev_ln = lines[idx - 1] if idx > 0 else ""
        line_for_time = ln
        if (
            prev_ln
            and _EXPLICIT_AM_RE.search(prev_ln)
            and not _EXPLICIT_AM_RE.search(ln)
            and not re.search(r"(下午|晚上|今晚|明晚|中午)", ln)
        ):
            line_for_time = f"上午 {ln}"

        hh, mm, matched = _extract_time_of_day(
            line_for_time,
            default_hm=(9, 0),
            ref_dt=now_dt,
            has_future_day=has_any_future_day,
        )
        if not matched:
            continue

        score = 0
        if _COMMITMENT_STRONG_RE.search(ln):
            score += 4
        if _COMMITMENT_TIME_SUFFIX_RE.search(ln):
            score += 3
        if _COMMITMENT_CONFIRM_RE.search(ln) and whole_has_strong_appt:
            score += 3
        if header_is_appt and re.search(r"(?:到|见|开始|局|来|去)", ln):
            score += 1
        if _COMMITMENT_QUESTION_RE.search(ln):
            score -= 2
        if _COMMITMENT_NOISE_LINE_RE.search(ln):
            score -= 3
        # 上下文相邻行包含强约定词（如前一行说『预约成功』或『量尺』，本行写『2026-12-03 14:30』）
        if score == 0 and prev_ln and _COMMITMENT_STRONG_RE.search(prev_ln):
            score += 3
        if score <= 0:
            continue

        if score >= 3:
            latest_strong_idx = max(latest_strong_idx, idx)

        key = (hh, mm)
        b = buckets.setdefault(
            key,
            {"score": 0, "max_line": 0, "latest_idx": -1, "indices": [], "lines": []},
        )
        b["score"] += score
        b["max_line"] = max(b["max_line"], score)
        b["latest_idx"] = max(b["latest_idx"], idx)
        b["indices"].append(idx)
        b["lines"].append(ln)

    if not buckets:
        return None
    # 若取消句出现在所有强约定句之后，视为已取消，绝不建提醒！
    if cancelled_any_strong and latest_cancel_idx >= latest_strong_idx:
        return None

    best_hm, best_info = max(
        buckets.items(),
        key=lambda kv: (kv[1]["score"], kv[1]["max_line"], kv[1]["latest_idx"]),
    )
    min_req = 2 if header_is_appt else 3
    if best_info["score"] < min_req or (best_info["max_line"] < min_req and best_info["score"] < 4):
        return None

    win_h, win_m = best_hm
    win_indices = set(best_info["indices"])
    adj_indices = {j for i in win_indices for j in (i - 1, i, i + 1)}

    # 结合整图日期线索（获胜行及其相邻行加权），确定最终约定日期
    resolved_date: Optional[date] = None
    explicit_date = False
    if all_line_dates:
        best_date_hit = max(
            all_line_dates,
            key=lambda item: (
                item[2] + (0.6 if item[0] in win_indices else (0.25 if item[0] in adj_indices else 0.0)),
                item[0],
            ),
        )
        resolved_date = best_date_hit[1]
        explicit_date = True
    else:
        resolved_date = base_date

    event_dt = datetime.combine(resolved_date, time(win_h, win_m), tzinfo=SHANGHAI_TZ)
    if event_dt <= now_dt:
        if not explicit_date:
            event_dt += timedelta(days=1)
        elif resolved_date == base_date and any("周末" in lines[i] or "周" in lines[i] for i, _, _ in all_line_dates):
            event_dt += timedelta(days=7)
        else:
            return None

    # 计算提前提醒时刻（默认提前 60 分钟，临近则自适应缩为 15 分钟 / 5 分钟）
    delta_min = (event_dt - now_dt).total_seconds() / 60.0
    if delta_min >= 75.0:
        lead_min = 60
        remind_dt = event_dt - timedelta(minutes=60)
    elif delta_min >= 25.0:
        lead_min = 15
        remind_dt = event_dt - timedelta(minutes=15)
    elif delta_min > 3.0:
        lead_min = max(1, int(min(5, delta_min - 1)))
        remind_dt = max(now_dt + timedelta(minutes=1), event_dt - timedelta(minutes=5))
    else:
        lead_min = 0
        remind_dt = now_dt

    # 提炼标题：优先群名（如『周末桌游局』），否则取最清晰的约定内容行
    evidence_lines = list(dict.fromkeys(best_info["lines"]))
    if group_title:
        title = group_title[:48]
    else:
        cand_title = ""
        for ln in lines:
            if re.search(r"(医院|门诊|复诊|量尺|安装|太古里|三里屯|公司|新房)", ln) and not _COMMITMENT_QUESTION_RE.search(ln):
                cand_title = ln
                break
        if not cand_title:
            cand_title = evidence_lines[0]
        cand_title = re.sub(r"^[^：:\s]{1,8}[：:]\s*", "", cand_title).strip()
        cand_title = re.sub(r"[昂啊吧呢哈啦]+$", "", cand_title).strip() or cand_title
        title = cand_title[:48]

    place = ""
    m_place = re.search(r"(?:在|去|到)\s*([^\s，,。！？!?;；]{2,16}?)(?:碰面|见面|集合|吃饭|聚餐|开会|量尺|复诊|门诊|$)", joined)
    if m_place:
        p_cand = m_place.group(1).strip()
        if p_cand not in ("家", "了就", "的地方", "这里", "那里"):
            place = p_cand

    return {
        "title": title,
        "event_at": iso_now(event_dt),
        "remind_at": iso_now(remind_dt),
        "lead_min": lead_min,
        "evidence": " / ".join(evidence_lines[:2])[:140],
        "place": place,
        "score": best_info["score"],
    }


def triage_inbound_drop(
    db: SimingDB,
    merged_text: str,
    note: str = "",
    source_channel: str = "ios_drop",
    llm_classifier: Optional[Callable[[str], dict[str, Any]]] = None,
    ref_dt: Optional[datetime] = None,
) -> dict[str, Any]:
    """Super-agent 多级分拣入口（供 `remote.drop_item` 调用）：
    返回 `{"intent": "IGNORE"|"TESLA"|"RESCHEDULE"|"PARCEL"|"BOOKMARK"|"COMMITMENT"|"CASE", ...}`。
    """
    clean = (merged_text or "").strip()
    if not clean:
        return {"intent": "IGNORE", "reason": "empty"}

    # 若用户显式写了备注，优先尊重用户备注！
    if note.strip():
        if re.search(r"(收藏|存一下|稍后看|bookmark)", note, re.IGNORECASE):
            bm = save_bookmark_from_drop(db, clean, title_override=note.strip())
            return {"intent": "BOOKMARK", **bm}

    # 1) L0 纯锁屏 / 设置页无信息误触直接静默忽略
    stripped_lines = [
        ln.strip()
        for ln in clean.splitlines()
        if ln.strip()
        and not re.match(r"^(?:\d{1,2}:\d{2}\d?|[\d\s$￥%•·<>〉‹5G4GLTEWiFi]+|轻点背面|截图发送|艇田发迓)$", ln.strip())
    ]
    if not stripped_lines:
        return {"intent": "IGNORE", "reason": "blank_or_lockscreen"}
    joined_body = "\n".join(stripped_lines)
    if len(joined_body) < 6 or re.search(
        r"(摇动以撤销|防止锁定结束通话|配置“发件人”启用此自动化|设置.*蜂窝网络.*屏幕使用时间|桌游.*拍卖群)",
        joined_body,
        re.DOTALL,
    ):
        return {"intent": "IGNORE", "reason": "ios_settings_or_noise"}

    # 2) L0 Tesla App 车况截图无感识别（Model Y + 电量% / 续航km + 驻车/锁车/充电/哨兵/温度；用 clean 防止独立行 82% 被状态栏过滤器误剥）
    m_tesla_anchor = re.search(r"(Model\s*Y|驻车|已锁车|充电上限|超级充电|哨兵模式)", clean, re.IGNORECASE)
    if m_tesla_anchor:
        tail_body = clean[m_tesla_anchor.start() :]
        m_soc = re.search(r"(\d{1,3})\s*%", tail_body) or re.search(r"(\d{1,3})\s*%", clean)
        m_km = re.search(r"(\d{2,3})\s*km\b", clean, re.IGNORECASE)
        if m_soc or m_km:
            from siming.tesla_link import ingest_tesla_state

            pl: dict[str, Any] = {}
            if m_soc and 1 <= int(m_soc.group(1)) <= 100:
                pl["soc"] = int(m_soc.group(1))
            if m_km and 10 <= int(m_km.group(1)) <= 550:
                pl["range_km"] = int(m_km.group(1))
            if re.search(r"(超级充电中|正在充电|充电中)", joined_body):
                pl["charging_state"] = "Charging"
            if pl:
                ts_res = ingest_tesla_state(db, pl, source="tesla_app_shot")
                return {"intent": "TESLA", **ts_res}

    # 3) L0 送装/师傅改约信号 -> 直通事件链重排！
    if RESCHEDULE_SIGNAL_RE.search(joined_body):
        return {"intent": "RESCHEDULE", "text": joined_body}

    # 3) L0 抖音/淘宝/拼多多订单列表页 -> 提取包裹 + 冷链标记 + 入 `shop_order_items` + 回填账本缺失商户！
    if ORDER_LIST_SIGNAL_RE.search(joined_body):
        parcels = extract_douyin_or_ecom_parcels(joined_body)
        if parcels:
            from siming.collectors.meituan_orders import store_orders
            from siming.ledger.enrich_priors import enrich_pending_ledger_with_priors_and_orders

            now_hm = now_local().strftime("%Y-%m-%d %H:%M")
            pseudo_orders = [
                {
                    "shop": p["shop"],
                    "ordered_at": now_hm,
                    "amount": p["amount_yuan"],
                    "status": "运输中" if p["is_in_transit"] else "已完成",
                    "cancelled": False,
                    "items": [(p["item_name"], 1)],
                }
                for p in parcels
            ]
            store_orders(db, pseudo_orders, platform="douyin")
            enrich_pending_ledger_with_priors_and_orders(db)

            in_transit = [p for p in parcels if p["is_in_transit"]]
            created_cases: list[str] = []
            cold_items: list[str] = []
            ts = iso_now()
            with db.transaction() as conn:
                for p in in_transit:
                    fp_p = hashlib.sha1(f"parcel|{p['item_name'][:18]}|{p['amount_cents']}".encode("utf-8")).hexdigest()[:20]
                    ex_c = conn.execute(
                        "SELECT case_id FROM cases WHERE category='parcel' AND status='待处理' AND context_json LIKE ?",
                        (f"%{fp_p}%",),
                    ).fetchone()
                    if ex_c:
                        continue
                    cid = db.next_seq_id(conn, "S")
                    cold_prefix = "🧊顺丰/冷链 · " if p["is_cold_chain"] else "在途包裹 · "
                    c_title = f"{cold_prefix}{p['item_name'][:36]}(¥{p['amount_yuan']:.1f})"
                    eta_s = str(p.get("eta_date") or "")
                    eta_kind = str(p.get("eta_kind") or "")
                    if eta_s and eta_kind == "ship":
                        eta_desc = f"备货中，预计 {eta_s} 发货"
                    elif eta_s:
                        eta_desc = f"预计 {eta_s} 送达（截图预测，未收快递短信前请看平台物流确认）"
                    else:
                        eta_desc = p["shipping_note"] or "运输中，留意快递短信或平台物流"
                    c_step = ("⚠️ 冷链生鲜防化冻！" if p["is_cold_chain"] else "") + eta_desc
                    due_at = eta_s if (eta_s and eta_kind != "ship") else None
                    conn.execute(
                        """
                        INSERT INTO cases (
                            case_id, title, next_step, due_at, status, strength, category, rule_id,
                            evidence_ids, context_json, created_at, updated_at
                        ) VALUES (?, ?, ?, ?, '待处理', 'SOFT', 'parcel', 'superagent_parcel', '[]', ?, ?, ?)
                        """,
                        (cid, c_title, c_step[:280], due_at, json.dumps({**p, "fp": fp_p}, ensure_ascii=False), ts, ts),
                    )
                    created_cases.append(cid)
                    if p["is_cold_chain"]:
                        cold_items.append(c_title)
            return {
                "intent": "PARCEL",
                "total_parsed": len(parcels),
                "in_transit_created": created_cases,
                "cold_chain_items": cold_items,
            }

    # 4) L0 明确时间约定 / 群聊约局 / 预约确认截图 -> COMMITMENT（优先于收藏分拣，严防把带链接/英文词的明确日程误吞为收藏！）
    cm = extract_commitment_from_drop(clean, ref_dt=ref_dt, paths=db.paths)
    if cm is not None:
        return {"intent": "COMMITMENT", **cm}

    # 5) L0 快递物流追踪/取件截图（即使不含实付款金额，也绝不可被误吞为被动收藏！）
    if COURIER_TRACKING_SIGNAL_RE.search(joined_body) and not re.search(r"github\.com/", joined_body, re.IGNORECASE):
        return {"intent": "CASE", "category": "parcel", "reason": "courier_tracking"}

    # 6) L0 GitHub 开源项目 / X 技术长帖 / 论文资讯卡片 -> 静默归入收藏夹（零待办噪音）
    # 剥离非 GitHub URL 后再判定 GITHUB_SIGNAL_RE，避免把 arxiv.org/abs/... 或 x.com/.../status/... 误归为 GitHub 仓库
    gh_probe_text = _NON_GITHUB_URL_RE.sub(" ", joined_body)
    gh_probe_text = re.sub(r"\b(?:arxiv\.org|x\.com|twitter\.com|mp\.weixin\.qq\.com)/\S+", " ", gh_probe_text, flags=re.IGNORECASE)
    if GITHUB_SIGNAL_RE.search(gh_probe_text):
        bm = save_bookmark_from_drop(db, joined_body, kind="github")
        return {"intent": "BOOKMARK", **bm}
    if ARTICLE_BOOKMARK_SIGNAL_RE.search(joined_body):
        bm = save_bookmark_from_drop(db, joined_body, kind="article")
        return {"intent": "BOOKMARK", **bm}

    # 6) L1 可选主力模型分类器兜底（测试可注入 mock，线上未命中确定性规则时可调用）
    if llm_classifier is not None:
        cls_res = llm_classifier(joined_body)
        intent_u = str(cls_res.get("intent") or "CASE").upper()
        if intent_u == "IGNORE":
            return {"intent": "IGNORE", "reason": "llm_ignore"}
        if intent_u == "BOOKMARK":
            bm = save_bookmark_from_drop(db, joined_body, title_override=str(cls_res.get("title") or ""))
            return {"intent": "BOOKMARK", **bm}

    return {"intent": "CASE"}


def format_bookmarks_list(db: SimingDB, query: str = "", limit: int = 12) -> str:
    """供 QQ `收藏夹` / `搜收藏 <词>` 秒查已沉淀的灵感与开源项目。"""
    db.init_db()
    q = (query or "").strip()
    conn = db.connect()
    try:
        if q:
            like = f"%{q}%"
            rows = conn.execute(
                """
                SELECT * FROM bookmarks
                WHERE title LIKE ? OR repo_slug LIKE ? OR summary LIKE ? OR related_projects_json LIKE ?
                ORDER BY created_at DESC LIMIT ?
                """,
                (like, like, like, like, limit),
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM bookmarks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    finally:
        conn.close()

    if not rows:
        return "📚 收藏夹暂无匹配记录（刷到好项目轻点手机背面即可自动收入此库）。"
    lines = [f"📚 **司命灵感与开源收藏夹**（共匹配 {len(rows)} 条 · 发 `试跑 Bxx` 可在隔离沙箱拉取评测）："]
    for r in rows:
        rel = ", ".join(json.loads(r["related_projects_json"] or "[]"))
        rel_s = f" · 🔗 `{rel}`" if rel else ""
        url_s = f" ({r['url']})" if r["url"] else ""
        lines.append(f"• **[{r['bookmark_id']}]** {r['title']}{url_s}{rel_s}")
    return "\n".join(lines)


def sandbox_try_bookmark(db: SimingDB, ref: str) -> dict[str, Any]:
    """F03：在 `$TMPDIR/siming/bookmark-trials/` 隔离目录浅克隆或检查收藏的 GitHub 仓库 README，绝不污染主工作区。"""
    db.init_db()
    clean_ref = (ref or "").strip()
    conn = db.connect()
    try:
        row = conn.execute(
            "SELECT * FROM bookmarks WHERE upper(bookmark_id)=upper(?) OR lower(repo_slug)=lower(?)",
            (clean_ref, clean_ref),
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "reply": f"⚠️ 未在收藏夹找到 `{clean_ref}`（可发 `收藏夹` 查看编号）。"}
    slug = str(row["repo_slug"] or "").strip()
    if not slug:
        return {"ok": False, "reply": f"⚠️ 收藏项 `[{row['bookmark_id']}] {row['title']}` 未包含明确 GitHub `owner/repo` 地址，暂无法自动克隆。"}

    trial_dir = db.paths.tmp_root / "bookmark-trials" / slug.replace("/", "__")
    trial_dir.parent.mkdir(parents=True, exist_ok=True)
    if os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return {
            "ok": True,
            "trial_dir": str(trial_dir),
            "reply": f"🧪 [沙盒演练] 已为 `[{row['bookmark_id']}] {slug}` 备妥隔离评测目录 `{trial_dir}`（关联：{row['related_projects_json']}）。",
        }
    if not (trial_dir / ".git").exists():
        proc = subprocess.run(
            ["git", "clone", "--depth=1", f"https://github.com/{slug}.git", str(trial_dir)],
            capture_output=True,
            text=True,
            timeout=35,
        )
        if proc.returncode != 0:
            return {"ok": False, "reply": f"⚠️ 克隆 `https://github.com/{slug}` 失败：{(proc.stderr or proc.stdout)[:160]}"}
    readme_txt = ""
    for fn in ("README.md", "README_CN.md", "readme.md"):
        p = trial_dir / fn
        if p.is_file():
            readme_txt = p.read_text(encoding="utf-8", errors="replace")[:360].strip()
            break
    return {
        "ok": True,
        "trial_dir": str(trial_dir),
        "stage": "fetched_readme",
        "verified_trial": False,
        "reply": (
            f"🧪 **已取材至隔离沙箱（未跑验收，仅取材态）**：`{trial_dir}`\n"
            f"• 仓库：`https://github.com/{slug}`\n"
            + (f"• README 摘要：{readme_txt[:220]}..." if readme_txt else "• 已就绪，跑过约定验证命令后才会记为「已试验」。")
        ),
    }
