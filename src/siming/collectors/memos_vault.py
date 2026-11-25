"""Obsidian 60-共享记忆与本地备忘增量感知采集器。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from siming.db import SimingDB


def collect_memos_vault(db: SimingDB) -> dict[str, Any]:
    db.init_db()
    mem_dir: Path = db.paths.shared_memory_dir
    if not mem_dir.is_dir():
        db.mark_source_error("memos_vault", "ERROR", f"missing {mem_dir}")
        return {"ok": False, "error": f"missing {mem_dir}"}

    conn = db.connect()
    try:
        row = conn.execute("SELECT cursor FROM sources WHERE id='memos_vault'").fetchone()
        prev_mtimes: dict[str, int] = json.loads(row["cursor"]) if (row and row["cursor"]) else {}
    except Exception:
        prev_mtimes = {}
    finally:
        conn.close()

    is_bootstrap = not prev_mtimes
    cur_mtimes: dict[str, int] = {}
    updated_files: list[str] = []

    for md in sorted(mem_dir.glob("*.md")):
        if md.name == "ACTIVE_WAITS.md":
            continue
        mtime = int(md.stat().st_mtime)
        cur_mtimes[md.name] = mtime
        if not is_bootstrap and prev_mtimes.get(md.name) != mtime:
            updated_files.append(md.name)

    if updated_files:
        with db.transaction() as conn:
            for fname in updated_files[:10]:
                db.append_event(
                    conn,
                    source="memos_vault",
                    thread_key=f"memo:{fname}",
                    summary=f"共享记忆卡片更新：{fname}",
                    payload={"file": fname, "mtime": cur_mtimes[fname]},
                    fingerprint=f"memo:{fname}:{cur_mtimes[fname]}",
                )

    db.mark_source_ok(
        "memos_vault",
        cursor=json.dumps(cur_mtimes, ensure_ascii=False),
        detail=f"cards={len(cur_mtimes)}, updated={len(updated_files)}",
        used=bool(updated_files),
    )
    return {"ok": True, "cards": len(cur_mtimes), "updated": updated_files}
