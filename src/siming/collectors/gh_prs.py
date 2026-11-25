"""关注的 GitHub PR 状态哈希采集器：仅在状态真变或新增评论时入库提醒，持续 ci_blocked 静默跳过。"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from siming.config import load_policy
from siming.db import SimingDB


def collect_watched_prs(db: SimingDB) -> dict[str, Any]:
    db.init_db()
    policy = load_policy(db.paths)
    watched: list[dict[str, Any]] = list(policy.get("watched_prs") or [])

    if not watched:
        db.mark_source_ok("gh_prs", detail="0 watched PRs configured")
        return {"ok": True, "checked": 0, "changed": 0}

    conn = db.connect()
    try:
        row = conn.execute("SELECT cursor FROM sources WHERE id='gh_prs'").fetchone()
        prev_hashes = json.loads(row["cursor"]) if (row and row["cursor"]) else {}
    except Exception:
        prev_hashes = {}
    finally:
        conn.close()

    env = dict(os.environ)
    env["TMPDIR"] = str(db.paths.tmp_root.parent)
    env.setdefault("HTTP_PROXY", "http://127.0.0.1:7890")
    env.setdefault("HTTPS_PROXY", "http://127.0.0.1:7890")
    env.setdefault("GH_CONFIG_DIR", str(Path.home() / ".config" / "gh"))

    new_hashes = dict(prev_hashes)
    changed = 0

    for item in watched:
        repo = str(item.get("repo") or "").strip()
        num = str(item.get("number") or "").strip()
        if not repo or not num:
            continue
        key = f"{repo}#{num}"
        try:
            proc = subprocess.run(
                [
                    "gh",
                    "pr",
                    "view",
                    num,
                    "--repo",
                    repo,
                    "--json",
                    "state,mergeStateStatus,reviewDecision,comments,title,url",
                ],
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
        except Exception as exc:
            db.mark_source_error("gh_prs", "ERROR", f"{key}: {exc}")
            continue

        if proc.returncode != 0:
            err = (proc.stderr or "").strip()[:120]
            auth_st = "AUTH_EXPIRED" if "auth" in err.lower() or "login" in err.lower() else "ERROR"
            db.mark_source_error("gh_prs", auth_st, f"{key}: {err}")
            return {"ok": False, "error": err}

        info = json.loads(proc.stdout)
        state = str(info.get("state") or "")
        merge_st = str(info.get("mergeStateStatus") or "")
        review = str(info.get("reviewDecision") or "")
        comments_cnt = len(info.get("comments") or [])
        sig = f"{state}|{merge_st}|{review}|{comments_cnt}"

        old_sig = prev_hashes.get(key)
        new_hashes[key] = sig
        if old_sig is None or old_sig == sig:
            continue

        # 持续 BLOCKED 且评论数未增时只记不吵
        old_parts = old_sig.split("|")
        if merge_st == "BLOCKED" and old_parts[1:2] == ["BLOCKED"] and str(comments_cnt) == (old_parts[3] if len(old_parts) > 3 else ""):
            continue

        changed += 1
        title = str(info.get("title") or key)
        with db.transaction() as conn:
            db.append_event(
                conn,
                source="gh_prs",
                thread_key=f"pr:{key}",
                summary=f"PR 状态变化 {key}: {state}/{merge_st}",
                payload={"repo": repo, "number": num, "sig": sig, "title": title},
                fingerprint=f"pr:{key}:{sig}",
            )
            db.enqueue_effect(
                conn,
                "send_qq",
                {
                    "ref_id": key,
                    "rule_id": "gh_pr_watch",
                    "trial": True,
                    "text": f"🔔 关注 PR 状态更新：{key} {title}\n状态：{state} · {merge_st} · review={review or '-'} · 评论 {comments_cnt}\n{info.get('url', '')}",
                },
            )

    db.mark_source_ok(
        "gh_prs",
        cursor=json.dumps(new_hashes),
        detail=f"checked={len(watched)}, changed={changed}",
        used=changed > 0,
    )
    return {"ok": True, "checked": len(watched), "changed": changed}
