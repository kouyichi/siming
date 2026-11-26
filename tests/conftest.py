"""Pytest 公共夹具：全程隔离在 ~/.cache/agent-tmp/siming-pytest，禁碰 /tmp 与真实 QQ。"""

from __future__ import annotations

import os
import secrets
import shutil
from datetime import datetime
from pathlib import Path
from typing import Callable, Iterator

import pytest

from siming.db import SHANGHAI_TZ, SimingDB
from siming.paths import SimingPaths


@pytest.fixture()
def frozen_clock(monkeypatch: pytest.MonkeyPatch) -> Callable[[str | datetime], datetime]:
    """冻结司命核心模块的 `now_local()`，彻底消除墙钟时间导致的测试抖动（如 23:00 红灯）。"""
    state: dict[str, datetime] = {
        "now": datetime(2026, 11, 20, 14, 0, 0, tzinfo=SHANGHAI_TZ)
    }

    def _fake_now() -> datetime:
        return state["now"]

    def _set(val: str | datetime) -> datetime:
        if isinstance(val, datetime):
            dt = val if val.tzinfo else val.replace(tzinfo=SHANGHAI_TZ)
        else:
            dt = datetime.fromisoformat(val)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=SHANGHAI_TZ)
        state["now"] = dt.astimezone(SHANGHAI_TZ)
        return state["now"]

    target_modules = (
        "siming.db",
        "siming.router",
        "siming.evald",
        "siming.push",
        "siming.approval",
        "siming.cn_calendar",
        "siming.daily_care",
        "siming.morning",
        "siming.session_scanner",
        "siming.push_guard",
        "siming.install_plan",
        "siming.self_heal",
        "siming.collectors.buy_track",
        "siming.collectors.omamail_bridge",
    )
    for mod_name in target_modules:
        try:
            mod = __import__(mod_name, fromlist=["now_local"])
            if hasattr(mod, "now_local"):
                monkeypatch.setattr(mod, "now_local", _fake_now)
        except Exception:
            pass
    return _set


@pytest.fixture()
def sandbox_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[SimingDB]:
    base_cache = Path.home() / ".cache" / "agent-tmp" / "siming-pytest"
    run_dir = base_cache / f"case-{secrets.token_hex(6)}"
    run_dir.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("TMPDIR", str(Path.home() / ".cache" / "agent-tmp"))
    monkeypatch.setenv("SIMING_TEST_OFFLINE", "1")
    monkeypatch.setenv("SIMING_INLINE_WORKER", "1")
    monkeypatch.setenv("SIMING_DRY_RUN_SEND", "1")
    monkeypatch.setenv("SIMING_DISABLE_SSH_EDGE", "1")
    monkeypatch.setenv("SIMING_DISABLE_LLM", "1")
    monkeypatch.setenv("SIMING_DRY_RUN_HERDR", "1")
    monkeypatch.setenv("SIMING_DISABLE_LIVE_OMAMAIL", "1")
    monkeypatch.setenv("SIMING_DECO_ROOT", str(run_dir / "deco"))
    monkeypatch.setenv("SIMING_BUY_ROOT", str(run_dir / "buy"))

    repo_root = Path(__file__).resolve().parents[1]
    paths = SimingPaths(
        repo_root=repo_root,
        config_dir=run_dir / "config",
        secrets_dir=run_dir / "config" / "secrets",
        data_dir=run_dir / "data",
        raw_dir=run_dir / "data" / "raw",
        backup_dir=run_dir / "data" / "backups",
        tmp_root=run_dir / "tmp",
        worktrees_dir=run_dir / "tmp" / "worktrees",
        reimburse_dir=run_dir / "data" / "reimburse",
        shared_memory_dir=run_dir / "memory",
        shared_inbox_dir=run_dir / "memory" / "inbox",
        db_path=run_dir / "data" / "siming.db",
    )
    monkeypatch.setenv("SIMING_CONFIG_DIR", str(paths.config_dir))
    monkeypatch.setenv("SIMING_DATA_DIR", str(paths.data_dir))
    monkeypatch.setenv("SIMING_TMP_ROOT", str(paths.tmp_root))
    monkeypatch.setenv("SIMING_SHARED_MEMORY_DIR", str(paths.shared_memory_dir))
    monkeypatch.setenv("SIMING_REIMBURSE_RECORDS_DIR", str(run_dir / "reimburse_records"))

    db = SimingDB(paths)
    db.init_db()
    try:
        yield db
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
