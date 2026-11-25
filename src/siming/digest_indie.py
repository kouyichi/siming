"""W3-6 · 收藏消化与独游工坊（Cross-Platform Bookmark Digest & Indie Game Ideation）。

1. 跨平台收藏消化管道：拉取 `bookmarks` 表（iOS 轻点截屏 / GitHub / 技术文章 / 抖音收藏），
   按与主人活跃项目（`siming`, `deco`, `boardgame_kb`, `indie_game`, `personas`）的相关度打分，
   每周日精选 Top-3 消化卡片并附带隔离工作区试跑命令（严禁使用 `/tmp`）；
2. 独立游戏灵感工坊：交叉检索桌游规则语料库（支持自定义目录/列表或内置经典机制库），
   生成机制融合（mechanic mashup）方案与 Steam 竞品雷达简报。
"""

from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
from typing import Any, Optional

from siming.db import SimingDB, logical_today
from siming.paths import _reject_slash_tmp

DEFAULT_ACTIVE_PROJECTS: tuple[str, ...] = (
    "siming",
    "deco",
    "boardgame_kb",
    "indie_game",
    "personas",
)

_PROJECT_DOMAIN_KEYWORDS: dict[str, tuple[str, ...]] = {
    "siming": ("agent", "sqlite", "mcp", "llm", "memory", "wiki", "automation", "runtime", "linger", "pi", "cli", "cron", "司命"),
    "deco": ("装修", "haos", "home assistant", "智能家居", "家电", "新风", "甲醛", "bambu", "3d打印", "homelab", "rb5009"),
    "boardgame_kb": ("桌游", "boardgame", "bgg", "规则", "rag", "deckbuilding", "worker placement", "规则书"),
    "indie_game": ("紫微", "命理", "lunar", "农历", "calendar", "astrology", "独游项目"),
    "personas": ("persona", "roleplay", "benchmark", "eval", "lora", "vllm", "gpu", "训练", "评测", "msim"),
}

_BUILTIN_BOARDGAME_CORPUS: tuple[dict[str, Any], ...] = (
    {
        "name": "Slay the Spire / Dominion",
        "mechanics": ["deckbuilding", "hand_management", "push_your_luck"],
        "excerpt": "通过精简牌库与构筑高协同引擎，在每回合有限抽牌与费用中做最优解。",
    },
    {
        "name": "Dune: Imperium",
        "mechanics": ["deckbuilding", "worker_placement", "area_control"],
        "excerpt": "卡牌同时决定工人派遣格位与战斗揭示效果，创造极强双轨资源张力。",
    },
    {
        "name": "Terraforming Mars / Wingspan",
        "mechanics": ["engine_building", "card_drafting", "tableau_building"],
        "excerpt": "早期投入触发链式产出，中后期围绕里程碑与收官时机切换为刷分模式。",
    },
    {
        "name": "Spirit Island / Root",
        "mechanics": ["asymmetric_powers", "cooperative", "action_programming"],
        "excerpt": "每个阵营拥有截然不同的规则引擎与胜利路径，极高重开价值与机制叙事感。",
    },
    {
        "name": "Brass: Birmingham",
        "mechanics": ["network_economy", "shared_incentives", "hand_management"],
        "excerpt": "对手建成的煤炭/啤酒资源可被己方消费，形成动态供需博弈与时代翻盘。",
    },
    {
        "name": "Azul / Cascadia",
        "mechanics": ["tile_drafting", "pattern_building", "set_collection"],
        "excerpt": "极低上手门槛却具备高深度空间拼图规划与卡位互动。",
    },
)

_STEAM_COMPS_BY_MECHANIC: dict[str, tuple[dict[str, str], ...]] = {
    "deckbuilding": (
        {"title": "Balatro", "angle": "极简规则 + 爆发式数值协同 + 15 分钟一局"},
        {"title": "Slay the Spire", "angle": "路线抉择 + 牌组压缩 + 遗物联动"},
    ),
    "worker_placement": (
        {"title": "Stacklands", "angle": "桌面化拖拽派工 + 合成发现循环"},
        {"title": "Against the Storm", "angle": "Roguelite 城市建造 + 紧迫周期目标"},
    ),
    "engine_building": (
        {"title": "Luck be a Landlord", "angle": "纯符号引擎构建 + 极低美术成本"},
        {"title": "Terra Nil", "angle": "逆向生态引擎 + 阶段性地形重构"},
    ),
    "asymmetric_powers": (
        {"title": "Into the Breach", "angle": "完美信息战棋 + 极高差异化机甲小队"},
        {"title": "FTL: Faster Than Light", "angle": "多子系统异步调度 + 危机应急"},
    ),
    "tile_drafting": (
        {"title": "Dorfromantik", "angle": "禅意六边形拼接 + 轻策略高分挑战"},
        {"title": "Drop Duchy", "angle": "俄罗斯方块落块 + 领地资源构筑"},
    ),
    "network_economy": (
        {"title": "Mini Motorways", "angle": "动态路网拥堵调度 + 极简视觉反馈"},
        {"title": "Slipways", "angle": "无微操星际贸易网 + 30 分钟一局"},
    ),
}

_SLUG_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _slug(s: str, max_len: int = 28) -> str:
    val = _SLUG_RE.sub("-", (s or "trial").strip().lower()).strip("-")
    return (val[:max_len] or "trial").strip("-")


def build_isolated_trial_cmd(
    bookmark: dict[str, Any],
    tmp_root: Optional[Path | str] = None,
) -> str:
    """为收藏项生成隔离容器/工作区试跑命令（严禁使用 `/tmp`）。"""
    base = (
        Path(tmp_root)
        if tmp_root is not None
        else (Path.home() / ".cache" / "agent-tmp" / "siming")
    )
    _reject_slash_tmp(base)
    trials_dir = base / "trials"

    repo_slug = str(bookmark.get("repo_slug") or "").strip()
    url = str(bookmark.get("url") or "").strip()
    title = str(bookmark.get("title") or "item").strip()
    kind = str(bookmark.get("kind") or "article").strip().lower()

    if not repo_slug and "github.com/" in url:
        m = re.search(r"github\.com/([a-zA-Z0-9_.-]+/[a-zA-Z0-9_.-]+)", url)
        if m:
            repo_slug = m.group(1).removesuffix(".git")

    folder = _slug(repo_slug.replace("/", "-") if repo_slug else title, 32)
    target_dir = trials_dir / folder

    if kind == "github" or repo_slug:
        clone_url = f"https://github.com/{repo_slug}.git" if repo_slug else url
        return (
            f"mkdir -p {trials_dir} && "
            f"git clone --depth 1 {clone_url} {target_dir} && "
            f"cd {target_dir} && TMPDIR={base} ls -la"
        )

    return (
        f"mkdir -p {target_dir} && cd {target_dir} && "
        f"TMPDIR={base} printf '%s\\n' {shlex.quote(title)} {shlex.quote(url)} > notes.md"
    )


def _keyword_matches(kw: str, hay_low: str) -> bool:
    k = kw.lower().strip()
    if not k:
        return False
    # 纯 ASCII 词（如 pi / cli / rag）采用词边界匹配，避免把 pizza / climbing / storage 误判成项目相关！
    if k.isascii():
        return bool(re.search(rf"(?<![a-z0-9]){re.escape(k)}(?![a-z0-9])", hay_low))
    return k in hay_low


def score_and_rank_bookmarks(
    db: SimingDB,
    active_projects: Optional[list[str]] = None,
    limit: int = 3,
) -> list[dict[str, Any]]:
    """拉取 `bookmarks` 表并按主人活跃项目相关度打分，返回 Top-K 消化候选。"""
    db.init_db()
    projects = [p.strip().lower() for p in (active_projects or list(DEFAULT_ACTIVE_PROJECTS)) if p.strip()]

    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM bookmarks ORDER BY created_at DESC LIMIT 200"
        ).fetchall()
    finally:
        conn.close()

    ranked: list[dict[str, Any]] = []
    for idx, r in enumerate(rows):
        title = str(r["title"] or "")
        url = str(r["url"] or "")
        repo_slug = str(r["repo_slug"] or "")
        summary = str(r["summary"] or "")
        kind = str(r["kind"] or "github")
        related_projects = [
            str(x).strip().lower() for x in json.loads(str(r["related_projects_json"] or "[]"))
        ]
        tags = [str(x).strip() for x in json.loads(str(r["tags_json"] or "[]"))]

        hay_low = f"{title} {repo_slug} {summary} {' '.join(tags)} {url}".lower()

        score = 0.0
        matched_projects: list[str] = []
        for proj in projects:
            proj_score = 0.0
            if any(rp == proj or rp.startswith(proj + "/") or rp.startswith(proj + "(") or _keyword_matches(proj, rp) for rp in related_projects):
                proj_score += 3.0
            if _keyword_matches(proj, hay_low):
                proj_score += 2.0
            for kw in _PROJECT_DOMAIN_KEYWORDS.get(proj, ()):
                if _keyword_matches(kw, hay_low):
                    proj_score += 1.0
            if proj_score > 0:
                matched_projects.append(proj)
                score += proj_score

        if kind == "github" or repo_slug:
            score += 0.8
        score += max(0.0, 0.3 - idx * 0.005)

        item = {
            "bookmark_id": str(r["bookmark_id"]),
            "kind": kind,
            "title": title,
            "url": url,
            "repo_slug": repo_slug,
            "summary": summary,
            "tags": tags,
            "related_projects": related_projects,
            "matched_projects": matched_projects,
            "relevance_score": round(score, 3),
            "created_at": str(r["created_at"]),
        }
        item["trial_cmd"] = build_isolated_trial_cmd(item, tmp_root=db.paths.tmp_root)
        ranked.append(item)

    ranked.sort(key=lambda x: (x["relevance_score"], x["created_at"]), reverse=True)
    return ranked[: max(1, int(limit))]


def build_weekly_digest_card(
    db: SimingDB,
    limit: int = 3,
    active_projects: Optional[list[str]] = None,
) -> dict[str, Any]:
    """生成周日收藏消化 Top-3 卡片（含关联项目理由与隔离试跑命令）。"""
    db.init_db()
    top_items = score_and_rank_bookmarks(db, active_projects=active_projects, limit=limit)
    day_s = logical_today().isoformat()

    lines = [f"🗂️ **周日收藏消化 Top-{len(top_items)}**（{day_s}）"]
    if not top_items:
        lines.append("• 当前收藏夹暂无待消化条目。")
    else:
        for i, item in enumerate(top_items, start=1):
            projs = ", ".join(item["matched_projects"]) or "通用灵感"
            lines.append(
                f"{i}. **{item['title']}** (`{item['kind']}` · 关联: `{projs}` · 相关度 `{item['relevance_score']:.2f}`)"
            )
            if item["summary"]:
                lines.append(f"   - 摘要：{item['summary'][:140]}")
            lines.append(f"   - 隔离试跑：`{item['trial_cmd']}`")

    return {
        "ok": True,
        "date": day_s,
        "count": len(top_items),
        "items": top_items,
        "markdown": "\n".join(lines),
    }


def _load_rules_corpus(
    rules_corpus: Optional[list[dict[str, Any]] | Path | str],
) -> list[dict[str, Any]]:
    if isinstance(rules_corpus, list):
        return [dict(x) for x in rules_corpus if isinstance(x, dict)]
    if isinstance(rules_corpus, (str, Path)):
        p = Path(rules_corpus)
        if p.is_dir():
            loaded: list[dict[str, Any]] = []
            for f in sorted(p.iterdir()):
                if not f.is_file() or f.suffix.lower() not in (".md", ".txt", ".json"):
                    continue
                try:
                    raw = f.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    continue
                if f.suffix.lower() == ".json":
                    try:
                        obj = json.loads(raw)
                        if isinstance(obj, dict):
                            loaded.append(obj)
                        elif isinstance(obj, list):
                            loaded.extend(x for x in obj if isinstance(x, dict))
                        continue
                    except Exception:
                        pass
                inferred = _infer_mechanics_from_text(raw)
                loaded.append(
                    {
                        "name": f.stem,
                        "mechanics": inferred or ["custom_rule"],
                        "excerpt": raw.strip().replace("\n", " ")[:180],
                        "source": f.name,
                    }
                )
            if loaded:
                return loaded
    return [dict(x) for x in _BUILTIN_BOARDGAME_CORPUS]


def _infer_mechanics_from_text(text: str) -> list[str]:
    low = (text or "").lower()
    mapping = (
        ("deckbuilding", ("deckbuild", "牌库", "构筑", "肉鸽卡牌", "卡组")),
        ("worker_placement", ("worker placement", "工放", "工人放置", "派工")),
        ("engine_building", ("engine build", "引擎", "流水线", "连携", "自动化")),
        ("asymmetric_powers", ("asymmetric", "非对称", "阵营能力", "异能")),
        ("tile_drafting", ("tile", "拼图", "板块", "六边形", "地形")),
        ("network_economy", ("network", "路网", "经济", "供需", "贸易")),
    )
    found: list[str] = []
    for mech, kws in mapping:
        if any(k in low for k in kws):
            found.append(mech)
    return found


def ideate_indie_game(
    topic: str,
    mechanics: Optional[list[str]] = None,
    rules_corpus: Optional[list[dict[str, Any]] | Path | str] = None,
) -> dict[str, Any]:
    """交叉检索桌游规则语料库，生成独游机制融合创意与 Steam 竞品雷达简报。"""
    clean_topic = (topic or "Untitled Indie Prototype").strip()
    corpus = _load_rules_corpus(rules_corpus)

    req_mechs = [m.strip().lower().replace(" ", "_") for m in (mechanics or []) if m.strip()]
    if not req_mechs:
        req_mechs = _infer_mechanics_from_text(clean_topic)
    if not req_mechs:
        req_mechs = ["deckbuilding", "engine_building"]

    referenced: list[dict[str, Any]] = []
    for entry in corpus:
        entry_mechs = [
            str(m).strip().lower().replace(" ", "_") for m in (entry.get("mechanics") or [])
        ]
        overlap = sorted(set(req_mechs) & set(entry_mechs))
        if overlap:
            referenced.append(
                {
                    "name": str(entry.get("name") or "Unknown Game"),
                    "matched_mechanics": overlap,
                    "all_mechanics": entry_mechs,
                    "excerpt": str(entry.get("excerpt") or ""),
                }
            )
    if not referenced and corpus:
        referenced.append(
            {
                "name": str(corpus[0].get("name") or "Reference"),
                "matched_mechanics": req_mechs[:1],
                "all_mechanics": list(corpus[0].get("mechanics") or []),
                "excerpt": str(corpus[0].get("excerpt") or ""),
            }
        )

    steam_comps: list[dict[str, str]] = []
    seen_titles: set[str] = set()
    for m in req_mechs:
        for comp in _STEAM_COMPS_BY_MECHANIC.get(m, ()):
            if comp["title"] not in seen_titles:
                seen_titles.add(comp["title"])
                steam_comps.append({**comp, "mechanic": m})
    if not steam_comps:
        steam_comps.append(
            {"title": "Balatro", "angle": "极简核心循环 + 高复玩构筑深度", "mechanic": req_mechs[0]}
        )

    mashup_str = " × ".join(req_mechs)
    return {
        "ok": True,
        "topic": clean_topic,
        "core_mechanics": req_mechs,
        "referenced_boardgames": referenced[:4],
        "mechanic_mashup": {
            "formula": mashup_str,
            "core_loop": f"围绕「{clean_topic}」构建 {mashup_str} 双轨循环：每回合在短期收益与引擎成长之间做不可逆抉择。",
            "twist": "引入桌游级公开信息博弈 + 20 分钟一局的电子化快速结算，消除物理记账负担。",
        },
        "steam_comp_radar": steam_comps[:4],
        "prototype_plan": [
            "Day 1 (6h): 用纯文本/网格状态机跑通 10 回合核心机制与胜负闭环",
            "Day 2 (6h): 接入 12 张原型卡/板块 + 3 组差异化初始配置做可玩性压测",
        ],
    }


def format_indie_brief(brief: dict[str, Any]) -> str:
    """将独游机制融合创意格式化为 Markdown 简报。"""
    lines = [
        f"🎮 **独游工坊立项简报：{brief.get('topic', '')}**",
        f"• 核心机制融合：`{' × '.join(brief.get('core_mechanics') or [])}`",
    ]
    mashup = brief.get("mechanic_mashup") or {}
    if mashup.get("core_loop"):
        lines.append(f"• 核心循环：{mashup['core_loop']}")
    if mashup.get("twist"):
        lines.append(f"• 差异化反转：{mashup['twist']}")

    refs = brief.get("referenced_boardgames") or []
    if refs:
        lines.append("\n### 🎲 桌游规则库交叉参考")
        for r in refs:
            mechs = ", ".join(r.get("matched_mechanics") or [])
            lines.append(f"- **{r['name']}** (`{mechs}`)：{r.get('excerpt', '')}")

    comps = brief.get("steam_comp_radar") or []
    if comps:
        lines.append("\n### 🧭 Steam 竞品雷达")
        for c in comps:
            lines.append(f"- **{c['title']}** (`{c.get('mechanic', '')}`) — {c.get('angle', '')}")

    plan = brief.get("prototype_plan") or []
    if plan:
        lines.append("\n### ⚡ 48h 最小切片验证")
        for step in plan:
            lines.append(f"- {step}")

    return "\n".join(lines)
