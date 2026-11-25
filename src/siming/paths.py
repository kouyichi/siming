"""路径与目录边界控制（强制禁写 /tmp）。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _reject_slash_tmp(path: Path) -> Path:
    resolved_str = str(path)
    if resolved_str == "/tmp" or resolved_str.startswith("/tmp/"):
        raise RuntimeError(
            f"禁止使用 /tmp 路径 ({resolved_str})：本机 /tmp 为 tmpfs，写满会拖挂 Hyprland。"
            " 请设置 TMPDIR=~/.cache/agent-tmp"
        )
    return path


@dataclass(frozen=True)
class SimingPaths:
    repo_root: Path
    config_dir: Path
    secrets_dir: Path
    data_dir: Path
    raw_dir: Path
    backup_dir: Path
    tmp_root: Path
    worktrees_dir: Path
    reimburse_dir: Path
    shared_memory_dir: Path
    shared_inbox_dir: Path
    db_path: Path

    @property
    def runs_dir(self) -> Path:
        return self.tmp_root / "runs"

    def ensure_dirs(self) -> None:
        _reject_slash_tmp(self.tmp_root)
        self.config_dir.mkdir(parents=True, exist_ok=True)
        self.secrets_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.secrets_dir, 0o700)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.data_dir, 0o700)
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.raw_dir, 0o700)
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.backup_dir, 0o700)
        self.tmp_root.mkdir(parents=True, exist_ok=True)
        os.chmod(self.tmp_root, 0o700)
        self.worktrees_dir.mkdir(parents=True, exist_ok=True)
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.runs_dir, 0o700)
        self.reimburse_dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.reimburse_dir, 0o700)
        self.shared_memory_dir.mkdir(parents=True, exist_ok=True)
        self.shared_inbox_dir.mkdir(parents=True, exist_ok=True)


def get_paths() -> SimingPaths:
    home = Path.home()
    repo_root = Path(
        os.environ.get("SIMING_REPO_ROOT") or Path(__file__).resolve().parents[2]
    )
    config_dir = Path(
        os.environ.get("SIMING_CONFIG_DIR") or (home / ".config" / "siming")
    )
    data_dir = Path(
        os.environ.get("SIMING_DATA_DIR") or (home / ".local" / "share" / "siming")
    )

    env_tmp = os.environ.get("SIMING_TMP_ROOT")
    if env_tmp:
        tmp_root = _reject_slash_tmp(Path(env_tmp))
    else:
        base_tmp = os.environ.get("TMPDIR", "").strip()
        if not base_tmp or base_tmp == "/tmp" or base_tmp.startswith("/tmp/"):
            base_tmp = str(home / ".cache" / "agent-tmp")
        tmp_root = _reject_slash_tmp(Path(base_tmp) / "siming")

    shared_memory_dir = Path(
        os.environ.get("SIMING_SHARED_MEMORY_DIR")
        or (home / "Documents" / "Obsidian Vault" / "60-共享记忆")
    )

    return SimingPaths(
        repo_root=repo_root,
        config_dir=config_dir,
        secrets_dir=config_dir / "secrets",
        data_dir=data_dir,
        raw_dir=data_dir / "raw",
        backup_dir=data_dir / "backups",
        tmp_root=tmp_root,
        worktrees_dir=tmp_root / "worktrees",
        reimburse_dir=data_dir / "reimburse",
        shared_memory_dir=shared_memory_dir,
        shared_inbox_dir=shared_memory_dir / "inbox",
        db_path=data_dir / "siming.db",
    )
