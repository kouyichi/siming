"""Hermes cron 输出收割器：把原先 `deliver=qqbot` 直推 QQ 的 cron 任务改成 `deliver=local` 后，
由 `siming tick` 读取 `~/.hermes/cron/output/<job_id>/*.md` 的新产物，按每个任务的级别经 `siming push` 同一链路分发。

- 脚本本身零改动；回滚 = 把 jobs.json 里该任务的 deliver 改回 qqbot（收割器见 deliver!=local 会自动跳过，避免重复）。
- 首次见到某任务只记游标、不回放历史产物。
- 空输出 / `**Status:** silent` / `no_change` / `[SILENT]` 一律跳过；`script failed` 按 normal 级别报一次。
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Optional

from siming.db import SimingDB

DEFAULT_OUTPUT_ROOT = Path.home() / ".hermes" / "cron" / "output"
DEFAULT_JOBS_JSON = Path.home() / ".hermes" / "cron" / "jobs.json"

# job_id -> 收编策略。level: now / normal / digest；max_lines/max_line_chars 只对 digest 生效（晨札里别刷屏）。
HARVEST_JOBS: dict[str, dict[str, Any]] = {
    "b4824fcdd178": {"name": "digest-my-prs", "rule": "pr_digest", "level": "digest", "max_lines": 12, "max_line_chars": 90},
    "0f0dae92d89f": {"name": "obsidian-weekly", "rule": "vault_weekly", "level": "digest", "max_lines": 8, "max_line_chars": 90},
    "0ae7d9c0167b": {"name": "docker-image-drift", "rule": "docker_drift", "level": "digest", "max_lines": 8, "max_line_chars": 90},
    "e94f9fa2f264": {"name": "check-81-retired", "rule": "check_81_retired", "level": "normal"},
    "095d6171bb72": {"name": "freegames-retry", "rule": "freegames_retry", "level": "normal"},
    "1f669ff55e60": {
        "name": "buy-price-protect",
        "rule": "price_protect_watch",
        "level": "normal",
        "skip_price_drop_when_price_watch_enabled": True,
    },
}

_SILENT_MARKERS = ("**Status:** silent", "**Status:** no_change")


def parse_cron_output(raw: str) -> tuple[str, str]:
    """返回 (kind, body)。kind ∈ {'silent', 'failed', 'ok'}。"""
    if not raw or not raw.strip():
        return "silent", ""
    head = raw[:600]
    if any(m in head for m in _SILENT_MARKERS):
        return "silent", ""
    if "**Status:** script failed" in head:
        body = raw.split("**Status:** script failed", 1)[1].strip()
        return "failed", body
    parts = raw.split("\n---\n", 1)
    body = (parts[1] if len(parts) == 2 else raw).strip()
    if "## Response" in body:
        body = body.split("## Response", 1)[1].strip()
    if not body or body.strip() == "[SILENT]":
        return "silent", ""
    return "ok", body


def compact_for_digest(body: str, max_lines: int, max_line_chars: int) -> str:
    out: list[str] = []
    lines = [ln.rstrip() for ln in body.splitlines() if ln.strip() and not re.fullmatch(r"[━─=\-]{4,}", ln.strip())]
    for ln in lines[:max_lines]:
        out.append(ln if len(ln) <= max_line_chars else ln[: max_line_chars - 1] + "…")
    if len(lines) > max_lines:
        out.append(f"…（另 {len(lines) - max_lines} 行）")
    return "\n".join(out)


def _local_job_ids(jobs_json: Path) -> Optional[set[str]]:
    try:
        data = json.loads(jobs_json.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    jobs = data.get("jobs", []) if isinstance(data, dict) else data
    return {str(j.get("id")) for j in jobs if isinstance(j, dict) and str(j.get("deliver") or "") == "local"}


def harvest_cron_outputs(
    db: SimingDB,
    output_root: Optional[Path] = None,
    jobs_json: Optional[Path] = None,
    jobs: Optional[dict[str, dict[str, Any]]] = None,
) -> dict[str, Any]:
    from siming.push import push_external

    if os.environ.get("SIMING_TEST_OFFLINE") == "1" and output_root is None:
        return {"ok": True, "skipped": "offline"}
    root = output_root or DEFAULT_OUTPUT_ROOT
    table = jobs if jobs is not None else HARVEST_JOBS
    local_ids = _local_job_ids(jobs_json or DEFAULT_JOBS_JSON)

    stats: dict[str, Any] = {"ok": True, "pushed": [], "skipped_not_local": []}
    for job_id, spec in table.items():
        # 仍是 deliver=qqbot 的任务由 Hermes 自己发，收割器不重复推（回滚开关）
        job_dir = root / job_id
        files = sorted(job_dir.glob("*.md")) if job_dir.is_dir() else []
        cursor_key = f"cron_harvest:{job_id}"
        if local_ids is not None and job_id not in local_ids:
            stats["skipped_not_local"].append(spec["name"])
            if files:
                db.set_meta(cursor_key, files[-1].name)  # 回滚期间的产物已由 Hermes 发过，切回 local 时不重放
            continue
        if not files:
            continue
        cursor = db.get_meta(cursor_key)
        if not cursor:
            db.set_meta(cursor_key, files[-1].name)
            continue
        new_files = [f for f in files if f.name > cursor]
        for f in new_files:
            try:
                raw = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            kind, body = parse_cron_output(raw)
            if kind == "silent":
                db.set_meta(cursor_key, f.name)
                continue
            stamp = f"{f.stem[5:10]} {f.stem[11:13]}:{f.stem[14:16]}"
            if kind == "failed":
                text = f"⚠️ cron「{spec['name']}」脚本失败（{stamp}）\n" + compact_for_digest(body, 6, 160)
                res = push_external(db, text, rule_id=spec["rule"] + "_fail", ref_id=f"cron:{job_id}:fail", level="normal")
            else:
                if spec.get("skip_price_drop_when_price_watch_enabled"):
                    from siming.modules import is_module_enabled

                    non_drop_lines = [
                        ln
                        for ln in body.splitlines()
                        if ln.strip()
                        and not ln.strip().startswith("🔻")
                        and not ln.strip().startswith("仅提醒，不自动申请")
                    ]
                    if is_module_enabled(db, "price_watch") and not non_drop_lines:
                        db.set_meta(cursor_key, f.name)
                        continue
                    if is_module_enabled(db, "price_watch") and len(non_drop_lines) < len(
                        [ln for ln in body.splitlines() if ln.strip()]
                    ):
                        body = "\n".join(non_drop_lines)
                level = spec.get("level", "normal")
                text = body
                if level == "digest":
                    text = compact_for_digest(body, int(spec.get("max_lines", 10)), int(spec.get("max_line_chars", 90)))
                    text += f"\n（原文：~/.hermes/cron/output/{job_id}/{f.name}）"
                res = push_external(db, text, rule_id=spec["rule"], ref_id=f"cron:{job_id}", level=level)
            # P0 修复：必须在 push_external 受理成功后再推进游标，防止中途崩溃导致永久漏推
            if res.get("ok"):
                db.set_meta(cursor_key, f.name)
            stats["pushed"].append({"job": spec["name"], "file": f.name, "kind": kind, "result": res})
    db.mark_source_ok("cron_harvest", detail=f"pushed={len(stats['pushed'])}")
    return stats
