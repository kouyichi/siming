"""不可变委任状（Mandate）构建与唤醒前硬重验。"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Optional

from siming.config import load_policy
from siming.db import SimingDB, now_local, parse_iso

VALID_ACTION_MODES = ("remind", "resume", "stage", "run")


@dataclass(frozen=True)
class Mandate:
    policy_version: int
    strength: str  # "explicit" | "inferred"
    action_mode: str  # "remind" | "resume" | "stage" | "run"
    cwd: str = ""
    agent: str = ""  # "claude" | "codex" | "agy" | "hermes" | ""
    session_id: str = ""
    runner: str = ""  # "codex" | "claude" | ""
    task: str = ""
    context: str = ""
    done_check: str = ""
    silent_night: bool = False
    allowed_tools: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True)

    @classmethod
    def from_json(cls, raw: str) -> "Mandate":
        data = json.loads(raw or "{}")
        allowed = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in allowed}
        filtered.setdefault("policy_version", 1)
        filtered.setdefault("strength", "inferred")
        filtered.setdefault("action_mode", "remind")
        return cls(**filtered)


@dataclass(frozen=True)
class MandateCheckResult:
    allowed: bool
    effective_mode: str
    reason: str = ""


def revalidate_mandate(
    mandate: Mandate,
    wait_strength: str,
    wait_action_mode: str,
    db: SimingDB,
    policy: Optional[dict[str, Any]] = None,
) -> MandateCheckResult:
    """挂起项命中时真正执行前，对 Mandate 做四道硬校验。"""
    pol = policy or load_policy(db.paths)
    active_ver = int(pol.get("policy_version", 1))

    # 1. 策略版本校验：若策略大版本升级作废旧授权，降级为纯提醒
    if int(mandate.policy_version) != active_ver:
        return MandateCheckResult(
            allowed=True,
            effective_mode="remind",
            reason=f"策略版本已变 ({mandate.policy_version} -> {active_ver})，退回仅提醒",
        )

    # 2. 全局暂停开关检查（siming pause）
    paused_until = db.get_meta("paused_until")
    if paused_until:
        try:
            p_dt = parse_iso(paused_until)
            if p_dt > now_local():
                if wait_action_mode in ("resume", "stage", "run"):
                    return MandateCheckResult(
                        allowed=False,
                        effective_mode=wait_action_mode,
                        reason=f"后台执行已暂停至 {paused_until}",
                    )
        except ValueError:
            pass

    # 3. 强度硬锁：非显式登记（如 2 分钟日志兜底推断项）严禁跑 resume/stage/run
    effective_strength = "explicit" if (wait_strength == "explicit" and mandate.strength == "explicit") else "inferred"
    if wait_action_mode != "remind" and effective_strength != "explicit":
        return MandateCheckResult(
            allowed=True,
            effective_mode="remind",
            reason="兜底推断项 (strength=inferred) 禁止自动执行，已锁定为 remind",
        )

    # 4. 目标目录与验收命令检查
    if wait_action_mode in ("resume", "stage", "run"):
        if not mandate.cwd or not Path(mandate.cwd).expanduser().is_dir():
            return MandateCheckResult(
                allowed=True,
                effective_mode="remind",
                reason=f"工作目录不存在 ({mandate.cwd or '未指定'})，退回 remind",
            )
    if wait_action_mode == "stage" and not mandate.done_check.strip():
        return MandateCheckResult(
            allowed=True,
            effective_mode="remind",
            reason="stage 模式缺少 done_check 验证命令，退回 remind",
        )

    return MandateCheckResult(allowed=True, effective_mode=wait_action_mode, reason="ok")
