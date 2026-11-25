"""W3-5 · 精准科研与 PR 雷达（Research & Smart PR Triage Radar）。

1. 问题驱动的科研/开源雷达：将仓库痛点绑定到 `research_pain_points`，按关键词与相关度匹配
   arXiv 论文与 GitHub Release，并为每条命中自动附带「15 分钟隔离 worktree 最小验证命令」；
2. 123+ Open PR 智能降噪分诊：彻底过滤无操作价值的 `action_required` CI 噪音，仅提炼高信号 PR
   （已批准可合并 `ready_to_merge`、维护者 `@mention`、活跃 PR `merge_conflict`），压缩为 1-3 张行动卡。
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from siming.db import SimingDB, iso_now

_SLUG_CLEAN_RE = re.compile(r"[^a-zA-Z0-9_-]+")


def _safe_slug(text: str, max_len: int = 28) -> str:
    slug = _SLUG_CLEAN_RE.sub("-", (text or "item").strip().lower()).strip("-")
    return (slug[:max_len] or "verify").strip("-")


def add_pain_point(
    db: SimingDB,
    repo: str,
    topic: str,
    keywords: list[str],
    verify_cmd_template: str = "",
) -> dict[str, Any]:
    """登记仓库研发痛点到 `research_pain_points`，并生成隔离 worktree 验证模板。"""
    db.init_db()
    clean_repo = (repo or "").strip()
    clean_topic = (topic or "").strip()
    if not clean_repo or not clean_topic:
        raise ValueError("repo 和 topic 不能为空")

    clean_kws = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    tpl = (verify_cmd_template or "").strip() or (
        "git worktree add {worktree_dir}/verify-{slug} HEAD && "
        "cd {worktree_dir}/verify-{slug} && "
        "pytest -q -k '{topic_slug}'"
    )
    ts = iso_now()

    with db.transaction() as conn:
        pp_id = db.next_seq_id(conn, "PP")
        conn.execute(
            """
            INSERT INTO research_pain_points (
                pp_id, repo, topic, keywords_json, verify_cmd_template, active, created_at
            ) VALUES (?, ?, ?, ?, ?, 1, ?)
            """,
            (
                pp_id,
                clean_repo,
                clean_topic,
                json.dumps(clean_kws, ensure_ascii=False),
                tpl,
                ts,
            ),
        )

    return {
        "ok": True,
        "pp_id": pp_id,
        "repo": clean_repo,
        "topic": clean_topic,
        "keywords": clean_kws,
        "verify_cmd_template": tpl,
        "active": True,
        "created_at": ts,
    }


def match_research_items(
    db: SimingDB,
    items: list[dict[str, Any]],
    min_score: float = 0.2,
) -> list[dict[str, Any]]:
    """按痛点关键词匹配 arXiv 论文 / GitHub Release，并生成 15 分钟隔离 worktree 最小验证命令。"""
    db.init_db()
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM research_pain_points WHERE active = 1 ORDER BY created_at DESC"
        ).fetchall()
    finally:
        conn.close()

    pain_points = [
        {
            "pp_id": str(r["pp_id"]),
            "repo": str(r["repo"]),
            "topic": str(r["topic"]),
            "keywords": json.loads(str(r["keywords_json"] or "[]")),
            "verify_cmd_template": str(r["verify_cmd_template"] or ""),
        }
        for r in rows
    ]
    if not pain_points or not items:
        return []

    worktrees_dir = str(db.paths.worktrees_dir)
    matches: list[dict[str, Any]] = []

    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "")
        summary = str(item.get("summary") or item.get("abstract") or "")
        raw_tags = item.get("tags")
        if isinstance(raw_tags, str):
            tags = [raw_tags]
        elif isinstance(raw_tags, (list, tuple, set)):
            tags = [str(t) for t in raw_tags if t is not None]
        else:
            tags = []
        url = str(item.get("url") or "")
        item_id = str(item.get("id") or _safe_slug(title, 16))
        hay_low = f"{title} {summary} {' '.join(tags)}".lower()

        best_pp: Optional[dict[str, Any]] = None
        best_score = 0.0
        best_kws: list[str] = []

        for pp in pain_points:
            kws = [str(k).strip() for k in pp["keywords"] if str(k).strip()]
            matched = [k for k in kws if k.lower() in hay_low]
            topic_hit = pp["topic"].lower() in hay_low
            repo_hit = pp["repo"].lower() in hay_low

            denom = max(1, len(kws))
            score = (len(matched) / denom) * 0.8 + (0.2 if topic_hit else 0.0) + (0.15 if repo_hit else 0.0)
            if len(matched) >= 2:
                score += 0.15
            score = round(min(1.0, score), 3)

            if score > best_score and (matched or topic_hit):
                best_score = score
                best_pp = pp
                best_kws = matched

        if best_pp is not None and best_score >= min_score:
            slug = _safe_slug(f"{best_pp['repo']}-{item_id}", 32)
            topic_slug = _safe_slug(best_pp["topic"], 24)
            tpl = best_pp["verify_cmd_template"] or (
                "git worktree add {worktree_dir}/verify-{slug} HEAD && "
                "cd {worktree_dir}/verify-{slug} && pytest -q"
            )
            fmt_vars = {
                "worktree_dir": worktrees_dir,
                "repo": best_pp["repo"],
                "slug": slug,
                "topic": best_pp["topic"],
                "topic_slug": topic_slug,
                "item_id": item_id,
                "url": url,
            }
            try:
                verify_cmd = tpl.format(**fmt_vars)
            except (KeyError, ValueError, IndexError):
                verify_cmd = (
                    f"git worktree add {worktrees_dir}/verify-{slug} HEAD && "
                    f"cd {worktrees_dir}/verify-{slug} && pytest -q"
                )
            matches.append(
                {
                    "item_id": item_id,
                    "kind": str(item.get("kind") or "arxiv"),
                    "title": title,
                    "url": url,
                    "summary": summary,
                    "pain_point": best_pp,
                    "repo": best_pp["repo"],
                    "topic": best_pp["topic"],
                    "matched_keywords": best_kws,
                    "relevance_score": best_score,
                    "verify_cmd": verify_cmd,
                }
            )

    matches.sort(key=lambda m: (m["relevance_score"], len(m["matched_keywords"])), reverse=True)
    return matches


def triage_open_prs(
    prs: list[dict[str, Any]],
    my_login: str = "devuser",
    max_cards: int = 3,
) -> dict[str, Any]:
    """对 Open PR 进行高信噪比分诊：屏蔽 `action_required` CI 噪音，压缩为 1-3 张行动卡。"""
    login = (my_login or "devuser").strip().lower()
    mention_pat = re.compile(rf"@{re.escape(login)}\b", re.IGNORECASE)

    actionable: list[dict[str, Any]] = []
    silenced_noise: list[dict[str, Any]] = []
    buckets: dict[str, list[dict[str, Any]]] = {
        "ready_to_merge": [],
        "maintainer_mention": [],
        "merge_conflict": [],
    }

    for pr in prs or []:
        if not isinstance(pr, dict):
            continue
        repo = str(pr.get("repo") or "unknown/repo")
        num = pr.get("number") if pr.get("number") is not None else (pr.get("id") or "")
        title = str(pr.get("title") or "")
        url = str(pr.get("url") or "")
        state = str(pr.get("state") or "open").strip().lower()
        is_closed = bool(pr.get("merged")) or state not in ("", "open")
        is_draft = bool(pr.get("draft"))
        review_decision = str(pr.get("review_decision") or "").upper()
        approved = bool(pr.get("approved")) or review_decision == "APPROVED"
        mergeable = str(pr.get("mergeable") or "").upper()
        has_conflict = bool(pr.get("has_conflict")) or mergeable == "CONFLICTING"

        ci_status = str(
            pr.get("ci_status") or pr.get("conclusion") or pr.get("check_status") or ""
        ).lower()
        is_action_required = ci_status == "action_required" or bool(pr.get("action_required_only"))

        comments = pr.get("comments") or []
        latest_comment = str(pr.get("latest_comment") or "")
        comment_author_role = str(pr.get("latest_comment_role") or "").upper()
        maintainer_mention = bool(pr.get("maintainer_mentioned"))
        if not maintainer_mention and latest_comment and mention_pat.search(latest_comment):
            if comment_author_role in ("MAINTAINER", "OWNER", "MEMBER", "COLLABORATOR", ""):
                maintainer_mention = True
        if not maintainer_mention and isinstance(comments, list):
            for c in comments:
                if not isinstance(c, dict):
                    continue
                body = str(c.get("body") or "")
                role = str(c.get("author_association") or c.get("role") or "").upper()
                is_bot = bool(c.get("is_bot")) or str(c.get("author") or "").lower().endswith("[bot]")
                if not is_bot and mention_pat.search(body) and role in ("MAINTAINER", "OWNER", "MEMBER", "COLLABORATOR", ""):
                    maintainer_mention = True
                    break

        entry_base = {
            "repo": repo,
            "number": num,
            "title": title,
            "url": url,
            "ci_status": ci_status,
        }

        if is_closed:
            silenced_noise.append({**entry_base, "silence_reason": "closed_or_merged"})
            continue

        if is_draft:
            silenced_noise.append({**entry_base, "silence_reason": "draft_pr"})
            continue

        # 严格优先级：ready_to_merge > maintainer_mention > merge_conflict
        if approved and not has_conflict and ci_status not in ("failure", "failed"):
            item = {
                **entry_base,
                "signal": "ready_to_merge",
                "priority": 1,
                "action_hint": "已获批准且无冲突，可直接合入或催促维护者 merge。",
            }
            actionable.append(item)
            buckets["ready_to_merge"].append(item)
        elif maintainer_mention:
            item = {
                **entry_base,
                "signal": "maintainer_mention",
                "priority": 2,
                "action_hint": f"维护者已 @{my_login} 回复，需尽快跟进修改或回应。",
            }
            actionable.append(item)
            buckets["maintainer_mention"].append(item)
        elif has_conflict:
            item = {
                **entry_base,
                "signal": "merge_conflict",
                "priority": 3,
                "action_hint": "活跃 PR 出现合并冲突，需 rebase 解决。",
            }
            actionable.append(item)
            buckets["merge_conflict"].append(item)
        elif is_action_required:
            silenced_noise.append({**entry_base, "silence_reason": "ci_action_required_noise"})
        else:
            silenced_noise.append({**entry_base, "silence_reason": "waiting_no_actionable_signal"})

    actionable.sort(key=lambda x: (x["priority"], str(x["repo"]), str(x["number"])))

    # 压缩为最多 max_cards (默认 3) 张行动卡
    cards: list[dict[str, Any]] = []
    for signal_key, badge, heading in (
        ("ready_to_merge", "✅", "可合入 PR"),
        ("maintainer_mention", "💬", "维护者 @ 提醒"),
        ("merge_conflict", "🔀", "冲突待 Rebase"),
    ):
        group = buckets[signal_key]
        if not group:
            continue
        top_items = group[:3]
        lines = [f"{badge} **{heading}**（{len(group)} 条）"]
        for g in top_items:
            lines.append(f"• `{g['repo']}#{g['number']}` {g['title']} — {g['action_hint']}")
        if len(group) > 3:
            lines.append(f"• …其余 {len(group) - 3} 条已折叠")
        cards.append(
            {
                "signal": signal_key,
                "title": f"{badge} {heading} ({len(group)})",
                "count": len(group),
                "items": top_items,
                "body": "\n".join(lines),
            }
        )

    return {
        "total": len(prs or []),
        "actionable_count": len(actionable),
        "silenced_noise_count": len(silenced_noise),
        "actionable": actionable,
        "silenced_noise": silenced_noise,
        "by_bucket": buckets,
        "cards": cards[: max(1, int(max_cards))],
    }


def format_radar_digest(
    db: SimingDB,
    prs: Optional[list[dict[str, Any]]] = None,
    papers: Optional[list[dict[str, Any]]] = None,
    my_login: str = "devuser",
) -> str:
    """生成科研论文/Release 与 Open PR 降噪合辑简报。"""
    db.init_db()
    lines = ["📡 **精准科研与 PR 雷达简报**"]

    if prs is not None:
        triage = triage_open_prs(prs, my_login=my_login)
        lines.append(
            f"\n### 🔀 PR 智能分诊（共 {triage['total']} 条 · 高信号 `{triage['actionable_count']}` · 已静默降噪 `{triage['silenced_noise_count']}`）"
        )
        if triage["cards"]:
            for card in triage["cards"]:
                lines.append(card["body"])
        else:
            lines.append("• 当前无高信号待处理 PR（`action_required` 等噪音已自动屏蔽）。")

    if papers is not None:
        matched = match_research_items(db, papers)
        lines.append(f"\n### 🧪 痛点驱动科研/开源命中（{len(matched)} 条）")
        if matched:
            for m in matched[:5]:
                lines.append(
                    f"• **[{m['repo']} · {m['topic']}]** {m['title']} (score `{m['relevance_score']:.2f}`, hits: {', '.join(m['matched_keywords'])})"
                )
                lines.append(f"  - 15m 隔离验证：`{m['verify_cmd']}`")
        else:
            lines.append("• 暂无命中当前登记仓库痛点的新论文或 Release。")

    return "\n".join(lines)
