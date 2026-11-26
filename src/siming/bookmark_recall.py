"""P3 (#6) 收藏夹无损入库、后台实体富化、三链决策召回与 DecisionUse 反馈闭环（bookmark_recall.py）。

职责：
1. 无损采集：保留全部抖音/截屏/分享收藏（先存 unclassified，含 platform_item_id/canonical_url），彻底解决入库前按技术关键词误删普通餐厅/食谱的问题；
2. 实体富化：后台提取门店、菜品、商品型号、开源仓库与关联项目；
3. 三条真实消费链：retrieve_candidates(purpose='food'|'shopping'|'dev', context, constraints) 返回带出处的候选列表（收藏是兴趣证据，绝不构成购买授权）；
4. 反馈闭环：DecisionUse 记录收藏在决策中的采纳/拒绝；用户点「只是收藏不想买」保留原收藏，仅在该用途下排除。
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
from typing import Any, Optional

from siming.db import SimingDB, iso_now, now_local

_FOOD_HINT_RE = re.compile(
    r"(?:餐厅|探店|美食|好吃|菜谱|食谱|外卖|火锅|烤肉|面馆|咖啡|奶茶|拉面|烧烤|日料|川菜|湘菜|粤菜|早茶|甜品|大排档|大牌档|店名|必吃)",
    re.IGNORECASE,
)
_SHOP_HINT_RE = re.compile(
    r"(?:评测|好物|开箱|种草|选购|对比|型号|滤芯|扫地机|吸尘器|传感器|zigbee|路由器|NAS|显卡|椅子|台灯|咖啡机|洗烘|冰箱|空调|电视)",
    re.IGNORECASE,
)
_DEV_HINT_RE = re.compile(
    r"(?:github\.com/|arxiv\.org/|开源|仓库|框架|Agent|LLM|Python|Rust|TypeScript|Docker|SQLite|CLI|SDK|论文|源码)",
    re.IGNORECASE,
)
_URL_RE = re.compile(r"https?://[^\s<>\"'）)]+", re.IGNORECASE)


def extract_all_bookmarks_from_douyin_favorites(raw_text: str) -> list[dict[str, str]]:
    """无损切分抖音「我的收藏」页面块：不按技术关键词预过滤，保留餐厅/食谱/好物/开源全部收藏。"""
    blocks = [b.strip() for b in re.split(r"\n\s*(?:\d+(?:\.\d+)?万?|\d{2,6})\s*\n", raw_text or "") if b.strip()]
    results: list[dict[str, str]] = []
    seen: set[str] = set()
    for blk in blocks:
        clean = re.sub(r"\s+", " ", blk).strip()
        if len(clean) < 8 or len(clean) > 800:
            continue
        url_m = _URL_RE.search(clean)
        canonical_url = url_m.group(0).rstrip(".,;:)") if url_m else ""
        item_id = hashlib.sha1((canonical_url or clean[:80]).encode("utf-8")).hexdigest()[:16]
        if item_id in seen:
            continue
        seen.add(item_id)
        title = clean.split(" ")[0][:48]
        results.append(
            {
                "platform": "douyin",
                "platform_item_id": item_id,
                "canonical_url": canonical_url,
                "title": f"抖音收藏 · {title}",
                "body": clean,
                "classification_state": "unclassified",
            }
        )
    return results


def infer_bookmark_entities(title: str, summary: str, url: str = "") -> tuple[str, dict[str, Any], list[str]]:
    """从收藏文本与 URL 轻量提取用途类别与结构化实体（不丢原文本）。"""
    blob = f"{title} {summary} {url}"
    tags: list[str] = []
    entities: dict[str, Any] = {}
    if _DEV_HINT_RE.search(blob):
        purpose = "dev"
        tags.append("dev")
        m_repo = re.search(r"github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)", blob, re.IGNORECASE)
        if m_repo:
            entities["repo"] = m_repo.group(1)
    elif _FOOD_HINT_RE.search(blob):
        purpose = "food"
        tags.append("food")
        entities["venue_or_dish"] = title.replace("抖音收藏 · ", "").strip()
    elif _SHOP_HINT_RE.search(blob):
        purpose = "shopping"
        tags.append("shopping")
        entities["product"] = title.replace("抖音收藏 · ", "").strip()
    else:
        purpose = "article"
        tags.append("general")
    return purpose, entities, tags


def save_lossless_bookmark(
    db: SimingDB,
    *,
    title: str,
    body: str,
    platform: str = "douyin",
    platform_item_id: str = "",
    canonical_url: str = "",
    source_event_id: str = "",
    classify_now: bool = False,
) -> dict[str, Any]:
    """无损入库单条收藏（允许先存 unclassified，按平台 ID 或 URL 幂等去重，不再按标题误删）。"""
    db.init_db()
    ts = iso_now()
    pid = platform_item_id.strip() or hashlib.sha1((canonical_url or f"{platform}:{title}:{body[:80]}").encode("utf-8")).hexdigest()[:16]
    with db.transaction() as conn:
        existing = conn.execute(
            "SELECT bookmark_id, title FROM bookmarks WHERE platform=? AND platform_item_id=? AND platform_item_id != ''",
            (platform, pid),
        ).fetchone()
        if existing:
            return {"ok": True, "duplicate": True, "bookmark_id": str(existing["bookmark_id"])}

        bid = db.next_seq_id(conn, "B")
        if classify_now:
            purpose, ent, tags = infer_bookmark_entities(title, body, canonical_url)
            kind = "github" if purpose == "dev" else ("food" if purpose == "food" else ("product" if purpose == "shopping" else "article"))
            cls_state = "classified"
        else:
            ent, tags, kind, cls_state = {}, [], "unclassified", "unclassified"

        conn.execute(
            """
            INSERT INTO bookmarks (
                bookmark_id, kind, title, url, repo_slug, summary,
                related_projects_json, tags_json, source_event_id, created_at,
                platform, platform_item_id, canonical_url, entity_json, classification_state
            ) VALUES (?, ?, ?, ?, ?, ?, '[]', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                bid,
                kind,
                title[:120],
                canonical_url,
                str(ent.get("repo") or ""),
                body[:600],
                json.dumps(tags, ensure_ascii=False),
                source_event_id,
                ts,
                platform,
                pid,
                canonical_url,
                json.dumps(ent, ensure_ascii=False),
                cls_state,
            ),
        )
    return {"ok": True, "duplicate": False, "bookmark_id": bid, "classification_state": cls_state}


def enrich_unclassified_bookmarks(db: SimingDB, limit: int = 100) -> dict[str, Any]:
    """后台异步为 unclassified 收藏提取门店/菜品/型号/仓库实体（只增不删，来源未知保留未知）。"""
    db.init_db()
    enriched = 0
    with db.transaction() as conn:
        rows = conn.execute(
            "SELECT * FROM bookmarks WHERE classification_state = 'unclassified' OR kind = 'unclassified' LIMIT ?",
            (int(limit),),
        ).fetchall()
        for r in rows:
            d = dict(r)
            purpose, ent, tags = infer_bookmark_entities(
                str(d.get("title") or ""), str(d.get("summary") or ""), str(d.get("canonical_url") or d.get("url") or "")
            )
            kind = "github" if purpose == "dev" else ("food" if purpose == "food" else ("product" if purpose == "shopping" else "article"))
            conn.execute(
                """
                UPDATE bookmarks
                SET kind=?, repo_slug=CASE WHEN repo_slug='' THEN ? ELSE repo_slug END,
                    tags_json=?, entity_json=?, classification_state='classified'
                WHERE bookmark_id=?
                """,
                (
                    kind,
                    str(ent.get("repo") or ""),
                    json.dumps(tags, ensure_ascii=False),
                    json.dumps(ent, ensure_ascii=False),
                    d["bookmark_id"],
                ),
            )
            enriched += 1
    return {"ok": True, "enriched": enriched}


def record_decision_use(
    db: SimingDB,
    *,
    bookmark_id: str,
    purpose: str,
    ticket_id: str = "",
    rank: int = 1,
    shown_reason: str = "",
    chosen: bool = False,
    rejected_reason: str = "",
    outcome: str = "",
) -> dict[str, Any]:
    """记录收藏在点餐/采买/研发决策中的实际效果；rejected_reason='just_saved_not_buying' 时保留原收藏仅修正该用途。"""
    db.init_db()
    uid = f"DU-{now_local().strftime('%Y%m%d%H%M%S')}-{secrets.token_hex(3)}"
    ts = iso_now()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO bookmark_decision_uses (
                use_id, bookmark_id, ticket_id, purpose, rank,
                shown_reason, chosen, rejected_reason, outcome, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (uid, bookmark_id, ticket_id, purpose, int(rank), shown_reason[:240], 1 if chosen else 0, rejected_reason[:120], outcome[:120], ts),
        )
    return {"ok": True, "use_id": uid, "bookmark_id": bookmark_id, "purpose": purpose, "chosen": chosen, "rejected_reason": rejected_reason}


def retrieve_candidates(
    db: SimingDB,
    *,
    purpose: str,
    context: str = "",
    constraints: Optional[dict[str, Any]] = None,
    limit: int = 5,
) -> list[dict[str, Any]]:
    """统一决策召回接口：先 SQLite 实体过滤与硬约束（忌口/排除用途/型号），再排序返回带出处的候选。
    注：收藏仅是一条兴趣证据，永远显式返回 purchase_authorized=False。
    """
    clean_purpose = str(purpose or "food").strip().lower()
    cons = constraints or {}
    disliked = [str(x).lower() for x in (cons.get("exclude_keywords") or cons.get("disliked") or []) if x]
    ctx_tokens = [t for t in re.findall(r"[\u4e00-\u9fff]{2,}|[A-Za-z0-9_-]{2,}", (context or "").lower()) if len(t) >= 2]

    conn = db.connect_readonly()
    try:
        rows = [dict(r) for r in conn.execute("SELECT * FROM bookmarks ORDER BY created_at DESC LIMIT 300").fetchall()]
        excluded_ids = {
            str(r["bookmark_id"])
            for r in conn.execute(
                "SELECT DISTINCT bookmark_id FROM bookmark_decision_uses WHERE purpose = ? AND rejected_reason IN ('just_saved_not_buying', 'not_interested')",
                (clean_purpose,),
            ).fetchall()
        }
        chosen_counts = {
            str(r["bookmark_id"]): int(r["c"])
            for r in conn.execute(
                "SELECT bookmark_id, COUNT(*) AS c FROM bookmark_decision_uses WHERE purpose = ? AND chosen = 1 GROUP BY bookmark_id",
                (clean_purpose,),
            ).fetchall()
        }
    finally:
        conn.close()

    scored: list[tuple[float, dict[str, Any]]] = []
    for r in rows:
        bid = str(r["bookmark_id"])
        if bid in excluded_ids:
            continue
        blob = f"{r.get('title', '')} {r.get('summary', '')} {r.get('repo_slug', '')} {r.get('tags_json', '')}".lower()
        # 硬约束过滤（如忌口/排除关键词）
        if any(bad in blob for bad in disliked):
            continue

        kind = str(r.get("kind") or "")
        tags = json.loads(r.get("tags_json") or "[]")
        ent = json.loads(r.get("entity_json") or "{}")
        purpose_match = (
            (clean_purpose == "food" and (kind == "food" or "food" in tags or bool(_FOOD_HINT_RE.search(blob))))
            or (clean_purpose == "shopping" and (kind == "product" or "shopping" in tags or bool(_SHOP_HINT_RE.search(blob))))
            or (clean_purpose == "dev" and (kind == "github" or "dev" in tags or bool(r.get("repo_slug")) or bool(_DEV_HINT_RE.search(blob))))
        )
        if not purpose_match:
            continue

        score = 1.0 + 0.5 * chosen_counts.get(bid, 0)
        matched_terms = [t for t in ctx_tokens if t in blob]
        score += 1.2 * len(matched_terms)
        reason_bits = [f"来自{r.get('platform') or '本地'}收藏 [{bid}]"]
        if matched_terms:
            reason_bits.append(f"命中上下文：{', '.join(matched_terms[:3])}")
        if chosen_counts.get(bid, 0) > 0:
            reason_bits.append(f"历史采纳 {chosen_counts[bid]} 次")

        scored.append(
            (
                score,
                {
                    "bookmark_id": bid,
                    "title": str(r.get("title") or ""),
                    "entity": ent,
                    "url": str(r.get("canonical_url") or r.get("url") or ""),
                    "evidence": {
                        "platform": str(r.get("platform") or "unknown"),
                        "summary_excerpt": str(r.get("summary") or "")[:160],
                        "created_at": str(r.get("created_at") or ""),
                    },
                    "reason": "；".join(reason_bits),
                    "freshness": str(r.get("created_at") or "")[:10],
                    "purchase_authorized": False,
                },
            )
        )

    scored.sort(key=lambda x: x[0], reverse=True)
    return [item for _, item in scored[: max(1, int(limit))]]
