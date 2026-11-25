"""W3-3 · M-SIM 跨模型自进化记忆（Self-Improvement Memory）。

受 Google + PSU M-SIM 启发：
- 将每次实验/调优/重构的结构化演化轨迹（问题域、来源模型、改动理由、基线与实测分、是否通过、评测语境）
  持久化到 SQLite `msim_entries` 表，并支持导出 Obsidian 共享记忆 Markdown 索引。
- 当切换模型家族（如 Claude Opus -> Gemini / Codex / DeepSeek / RedPill）时，自动按问题域提取
  Top-K 有效策略与高危回退反模式，拼装为跨模型知识迁移提示块（transfer prompt）。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from siming.crypto import redact_sensitive
from siming.db import SimingDB, iso_now
from siming.paths import SimingPaths, _reject_slash_tmp

_MSIM_EXTRA_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "[REDACTED-KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}\b"), "[REDACTED-JWT]"),
    (
        re.compile(
            r"((?:支付密码|交易密码|取款密码|银行卡密码|密保|卡密|PIN码?|payment\s*password)[\s:：为是=]*)\d{4,8}\b",
            re.IGNORECASE,
        ),
        r"\1[REDACTED-PWD]",
    ),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[REDACTED-PHONE]"),
)


def _sanitize_msim_text(text: str, paths: Optional[SimingPaths] = None) -> str:
    if not text:
        return ""
    out = redact_sensitive(text, paths=paths)
    for pat, repl in _MSIM_EXTRA_SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out

_TOKEN_RE = re.compile(r"[a-zA-Z0-9_]+|[\u4e00-\u9fff]{2,}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")
_MSIM_CN_STOP = frozenset({"我们", "使用", "进行", "可以", "通过", "已经", "失败", "成功", "功能", "修复"})


def _tokenize(text: str) -> set[str]:
    raw = text or ""
    toks = {tok.lower() for tok in _TOKEN_RE.findall(raw) if len(tok.strip()) >= 2}
    for run in _CJK_RUN_RE.findall(raw):
        for i in range(len(run) - 1):
            bg = run[i : i + 2]
            if bg not in _MSIM_CN_STOP and not any(ch in "的了是在有和与用点选" for ch in bg):
                toks.add(bg)
    return toks


def _row_to_entry(row: Any) -> dict[str, Any]:
    tags_raw = str(row["tags_json"] or "[]")
    try:
        tags = json.loads(tags_raw)
        if not isinstance(tags, list):
            tags = []
    except Exception:
        tags = []
    measured = float(row["measured_score"] or 0.0)
    baseline = float(row["baseline_score"] or 0.0)
    return {
        "entry_id": str(row["entry_id"]),
        "problem_family": str(row["problem_family"]),
        "target_artifact": str(row["target_artifact"] or ""),
        "source_model": str(row["source_model"]),
        "change_rationale": str(row["change_rationale"]),
        "expected_outcome": str(row["expected_outcome"] or ""),
        "measured_score": measured,
        "baseline_score": baseline,
        "delta": round(measured - baseline, 4),
        "passed": bool(int(row["passed"])),
        "eval_context": str(row["eval_context"] or ""),
        "tags": [str(t) for t in tags],
        "created_at": str(row["created_at"]),
    }


def record_msim_entry(
    db: SimingDB,
    problem_family: str,
    source_model: str,
    change_rationale: str,
    expected_outcome: str = "",
    measured_score: float = 1.0,
    baseline_score: float = 0.0,
    passed: bool = True,
    eval_context: str = "",
    target_artifact: str = "",
    tags: Optional[list[str]] = None,
) -> dict[str, Any]:
    """写入一条 M-SIM 跨模型进化日记记录到 `msim_entries`。"""
    db.init_db()
    fam = (problem_family or "general").strip()
    model = (source_model or "unknown").strip()
    rationale = _sanitize_msim_text((change_rationale or "").strip(), paths=db.paths)
    if not rationale:
        raise ValueError("change_rationale 不能为空")
    clean_expected = _sanitize_msim_text((expected_outcome or "").strip(), paths=db.paths)
    clean_eval_ctx = _sanitize_msim_text((eval_context or "").strip(), paths=db.paths)

    clean_tags = [str(t).strip() for t in (tags or []) if str(t).strip()]
    ts = iso_now()
    measured = float(measured_score)
    baseline = float(baseline_score)
    delta = round(measured - baseline, 4)

    with db.transaction() as conn:
        entry_id = db.next_seq_id(conn, "MSIM")
        conn.execute(
            """
            INSERT INTO msim_entries (
                entry_id, problem_family, target_artifact, source_model,
                change_rationale, expected_outcome, measured_score, baseline_score,
                passed, eval_context, tags_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                entry_id,
                fam,
                (target_artifact or "").strip(),
                model,
                rationale,
                clean_expected,
                measured,
                baseline,
                1 if passed else 0,
                clean_eval_ctx,
                json.dumps(clean_tags, ensure_ascii=False),
                ts,
            ),
        )

    return {
        "entry_id": entry_id,
        "problem_family": fam,
        "target_artifact": (target_artifact or "").strip(),
        "source_model": model,
        "change_rationale": rationale,
        "expected_outcome": clean_expected,
        "measured_score": measured,
        "baseline_score": baseline,
        "delta": delta,
        "passed": bool(passed),
        "eval_context": clean_eval_ctx,
        "tags": clean_tags,
        "created_at": ts,
    }


def search_msim_entries(
    db: SimingDB,
    problem_family: str = "",
    query: str = "",
    limit: int = 5,
) -> list[dict[str, Any]]:
    """按 problem_family 与可选查询词检索历史进化日记（综合相关性、分数收益与时效排序）。"""
    db.init_db()
    fam = (problem_family or "").strip().lower()
    q_tokens = _tokenize(query)

    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM msim_entries ORDER BY created_at DESC LIMIT 500"
        ).fetchall()
    finally:
        conn.close()

    scored: list[tuple[float, dict[str, Any]]] = []
    for idx, row in enumerate(rows):
        entry = _row_to_entry(row)
        entry_fam = entry["problem_family"].lower()
        if fam and fam != entry_fam and fam not in entry_fam:
            continue

        hay = " ".join(
            [
                entry["problem_family"],
                entry["target_artifact"],
                entry["source_model"],
                entry["change_rationale"],
                entry["expected_outcome"],
                entry["eval_context"],
                " ".join(entry["tags"]),
            ]
        )
        hay_lower = hay.lower()
        hay_tokens = _tokenize(hay)

        score = 0.0
        if fam:
            score += 3.0 if entry_fam == fam else 1.5
        if q_tokens:
            overlap = len(q_tokens & hay_tokens)
            substr_hits = sum(1 for tok in q_tokens if tok in hay_lower)
            if overlap == 0 and substr_hits == 0:
                continue
            score += overlap * 2.0 + substr_hits * 0.8
        # 优先突出显著正向改进或显著回退（高信息量样本）
        score += min(3.0, abs(entry["delta"]) * 2.0)
        # 轻度时效偏置
        score += max(0.0, 0.5 - idx * 0.01)
        entry["relevance_score"] = round(score, 3)
        scored.append((score, entry))

    scored.sort(key=lambda pair: (pair[0], pair[1]["created_at"]), reverse=True)
    return [item for _, item in scored[: max(1, int(limit))]]


def build_msim_transfer_prompt(
    db: SimingDB,
    problem_family: str,
    target_model: str,
    query: str = "",
    limit: int = 5,
) -> str:
    """为目标模型生成跨模型知识迁移注入块（含历史优胜策略与失败回退警示）。"""
    db.init_db()
    candidates = search_msim_entries(
        db,
        problem_family=problem_family,
        query=query,
        limit=max(limit * 4, 20),
    )
    if not candidates and query:
        # 若带 query 在当前 problem_family 无精确命中，退回到同 problem_family 全部经验
        candidates = search_msim_entries(
            db,
            problem_family=problem_family,
            query="",
            limit=max(limit * 4, 20),
        )
    if not candidates and query and problem_family:
        # 若该 problem_family 尚无记录，按 query 跨问题域兜底召回高度相关的历史进化经验
        candidates = search_msim_entries(
            db,
            problem_family="",
            query=query,
            limit=max(limit * 4, 20),
        )
    if not candidates:
        return ""

    # 若指定了 problem_family 且带 query 检索后缺了优胜策略或失败反模式，则从同 problem_family 补齐另一半，避免目标模型漏掉关键教训
    if problem_family and query:
        has_win = any(e["passed"] and e["delta"] >= 0.0 for e in candidates)
        has_reg = any((not e["passed"]) or e["delta"] < 0.0 for e in candidates)
        if not (has_win and has_reg):
            fam_all = search_msim_entries(
                db,
                problem_family=problem_family,
                query="",
                limit=max(limit * 4, 20),
            )
            seen_ids = {e["entry_id"] for e in candidates}
            for item in fam_all:
                if item["entry_id"] not in seen_ids:
                    candidates.append(item)
                    seen_ids.add(item["entry_id"])

    winners = [e for e in candidates if e["passed"] and e["delta"] >= 0.0]
    winners.sort(key=lambda e: (e["delta"], e["measured_score"]), reverse=True)
    winners = winners[: max(1, int(limit))]

    regressions = [e for e in candidates if (not e["passed"]) or e["delta"] < 0.0]
    regressions.sort(key=lambda e: (e["delta"], e["measured_score"]))
    regressions = regressions[: max(1, int(limit))]

    prior_models = sorted({e["source_model"] for e in candidates})
    lines: list[str] = [
        f"## [M-SIM Cross-Model Evolution Memory] family={problem_family} -> target={target_model}",
        f"Prior contributing models: {', '.join(prior_models)}",
        "",
    ]

    if winners:
        lines.append("### Verified Winning Strategies (Adopt & Build Upon)")
        for e in winners:
            sign = "+" if e["delta"] >= 0 else ""
            artifact_note = f" [{e['target_artifact']}]" if e["target_artifact"] else ""
            ctx_note = f" | eval: {e['eval_context']}" if e["eval_context"] else ""
            lines.append(
                f"- [{e['entry_id']}] ({e['source_model']}){artifact_note} "
                f"score {e['baseline_score']:.3f} -> {e['measured_score']:.3f} ({sign}{e['delta']:.3f}): "
                f"{e['change_rationale']}{ctx_note}"
            )
        lines.append("")

    if regressions:
        lines.append("### Warned Regressions & Anti-Patterns (DO NOT Repeat)")
        for e in regressions:
            sign = "+" if e["delta"] >= 0 else ""
            artifact_note = f" [{e['target_artifact']}]" if e["target_artifact"] else ""
            ctx_note = f" | eval: {e['eval_context']}" if e["eval_context"] else ""
            lines.append(
                f"- ⚠️ [{e['entry_id']}] ({e['source_model']}){artifact_note} "
                f"FAILED ({e['baseline_score']:.3f} -> {e['measured_score']:.3f}, {sign}{e['delta']:.3f}): "
                f"{e['change_rationale']}{ctx_note}"
            )
        lines.append("")

    return "\n".join(lines).strip()


def get_msim_leaderboard(db: SimingDB) -> dict[str, Any]:
    """按 problem_family 与 source_model 聚合改进幅度和通过率排行榜。"""
    db.init_db()
    conn = db.connect()
    try:
        rows = conn.execute("SELECT * FROM msim_entries ORDER BY created_at DESC").fetchall()
    finally:
        conn.close()

    entries = [_row_to_entry(r) for r in rows]
    by_family: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}

    for e in entries:
        fam = e["problem_family"]
        fstat = by_family.setdefault(
            fam,
            {
                "count": 0,
                "passed": 0,
                "failed": 0,
                "delta_sum": 0.0,
                "best_delta": None,
                "best_model": "",
                "best_entry_id": "",
            },
        )
        fstat["count"] += 1
        fstat["passed"] += 1 if e["passed"] else 0
        fstat["failed"] += 0 if e["passed"] else 1
        fstat["delta_sum"] += e["delta"]
        if fstat["best_delta"] is None or e["delta"] > fstat["best_delta"]:
            fstat["best_delta"] = e["delta"]
            fstat["best_model"] = e["source_model"]
            fstat["best_entry_id"] = e["entry_id"]

        mod = e["source_model"]
        mstat = by_model.setdefault(
            mod,
            {
                "count": 0,
                "passed": 0,
                "failed": 0,
                "delta_sum": 0.0,
                "positive_delta_sum": 0.0,
            },
        )
        mstat["count"] += 1
        mstat["passed"] += 1 if e["passed"] else 0
        mstat["failed"] += 0 if e["passed"] else 1
        mstat["delta_sum"] += e["delta"]
        if e["passed"] and e["delta"] > 0:
            mstat["positive_delta_sum"] += e["delta"]

    for stat in by_family.values():
        cnt = max(1, stat["count"])
        stat["avg_delta"] = round(stat.pop("delta_sum") / cnt, 4)
        stat["best_delta"] = round(float(stat["best_delta"] or 0.0), 4)

    for stat in by_model.values():
        cnt = max(1, stat["count"])
        stat["avg_delta"] = round(stat.pop("delta_sum") / cnt, 4)
        stat["total_positive_delta"] = round(stat.pop("positive_delta_sum"), 4)
        stat["pass_rate"] = round(stat["passed"] / cnt, 4)

    passed_total = sum(1 for e in entries if e["passed"])
    return {
        "total_entries": len(entries),
        "passed_count": passed_total,
        "failed_count": len(entries) - passed_total,
        "by_family": by_family,
        "by_model": by_model,
    }


def export_msim_markdown(db: SimingDB, out_path: Optional[Path | str] = None) -> Path:
    """导出人类可读的 M-SIM 跨模型进化日记 Markdown 索引（默认写入 Obsidian 共享记忆目录）。"""
    db.init_db()
    target = (
        Path(out_path)
        if out_path is not None
        else (db.paths.shared_memory_dir / "msim-evolution-diary.md")
    )
    _reject_slash_tmp(target)
    target.parent.mkdir(parents=True, exist_ok=True)

    board = get_msim_leaderboard(db)
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT * FROM msim_entries ORDER BY problem_family ASC, created_at DESC"
        ).fetchall()
    finally:
        conn.close()
    entries = [_row_to_entry(r) for r in rows]

    lines: list[str] = [
        "# M-SIM 跨模型自进化日记",
        "",
        f"- 更新时间：`{iso_now()}`",
        f"- 累计记录：`{board['total_entries']}` 条（通过 `{board['passed_count']}` · 失败/回退 `{board['failed_count']}`）",
        "",
        "## 模型进化贡献榜",
        "",
        "| Model | Runs | Pass Rate | Avg Delta | Total Positive Gain |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for model, st in sorted(
        board["by_model"].items(),
        key=lambda kv: kv[1]["total_positive_delta"],
        reverse=True,
    ):
        lines.append(
            f"| `{model}` | {st['count']} | {st['pass_rate'] * 100:.1f}% | "
            f"{st['avg_delta']:+.4f} | {st['total_positive_delta']:+.4f} |"
        )
    if not board["by_model"]:
        lines.append("| (none) | 0 | 0.0% | +0.0000 | +0.0000 |")

    lines.extend(["", "## 问题域演化明细", ""])
    grouped: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        grouped.setdefault(e["problem_family"], []).append(e)

    for fam, items in grouped.items():
        fst = board["by_family"].get(fam, {})
        lines.append(
            f"### `{fam}`（{len(items)} 条 · 最优 {fst.get('best_delta', 0.0):+.4f} by `{fst.get('best_model', '-')}`）"
        )
        lines.append("")
        for e in items:
            badge = "✅" if e["passed"] else "❌"
            tags_s = f" · tags: {', '.join(e['tags'])}" if e["tags"] else ""
            art_s = f" · artifact: `{e['target_artifact']}`" if e["target_artifact"] else ""
            lines.append(
                f"- {badge} **`{e['entry_id']}`** `{e['source_model']}` "
                f"(`{e['baseline_score']:.3f}` → `{e['measured_score']:.3f}`, `{e['delta']:+.4f}`)"
                f"{art_s}{tags_s}"
            )
            lines.append(f"  - Rationale: {_sanitize_msim_text(e['change_rationale'], paths=db.paths)}")
            if e["expected_outcome"]:
                lines.append(f"  - Expected: {_sanitize_msim_text(e['expected_outcome'], paths=db.paths)}")
            if e["eval_context"]:
                lines.append(f"  - Eval: {_sanitize_msim_text(e['eval_context'], paths=db.paths)}")
        lines.append("")

    target.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return target
