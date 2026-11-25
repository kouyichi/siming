"""W3-1 · Linger/Pi 多 Runtime 调度底座（Your agents. Your machine. Your rules.）。

受 `simplexai-labs/linger` 启发：
- 彻底解耦「编排 / 共享记忆 / 本地 A2 审批权限」与底层模型 Runtime；
- 重型编码/重构/架构任务优先路由到已登录订阅 CLI（`claude_sub`, `codex_sub`，零额外 API 费用），
  依据 `ai-quota` 实时余量自动切换；
- 轻量分拣 / 本地隐私 / 自定义 API 任务路由到轻量 `pi` runtime（`pi_deepseek`, `pi_kimi`,
  `pi_redpill`, `pi_local_ollama`），当订阅配额低于阈值或上游熔断（`CIRCUIT_OPEN`）时无缝回退；
- 派单时自动注入 M-SIM 跨模型自进化提示、写入 `usage` 统计，并死守本地 A2 门禁绝不绕过。
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Callable, Optional

from siming.approval import create_approval
from siming.config import load_policy
from siming.db import SimingDB
from siming.msim_memory import build_msim_transfer_prompt
from siming.paths import _reject_slash_tmp
from siming.quota import fetch_ai_quota

_HEAVY_TASK_KINDS = frozenset({"coding", "refactoring", "architecture", "review", "debugging"})
_LIGHT_TASK_KINDS = frozenset({"triage", "summary", "classification", "local", "privacy", "custom_api", "light_coding"})

_A2_GATED_TASK_RE = re.compile(
    r"(rm\s+-rf|git\s+push\s+--force|drop\s+table|apply_worktree|git\s+reset\s+--hard|mkfs|shutdown|reboot|deploy\s+prod)",
    re.IGNORECASE,
)


def _load_circuit_open_providers(db: SimingDB) -> set[str]:
    """读取 `compute_assets` 表与 `meta` 开关，识别处于熔断状态（CIRCUIT_OPEN）的 provider / runtime。"""
    opened: set[str] = set()
    conn = db.connect()
    try:
        rows = conn.execute(
            "SELECT provider, account_label, status FROM compute_assets WHERE UPPER(status) = 'CIRCUIT_OPEN'"
        ).fetchall()
        for r in rows:
            if r["provider"]:
                opened.add(str(r["provider"]).strip().lower())
            if r["account_label"]:
                opened.add(str(r["account_label"]).strip().lower())
        meta_rows = conn.execute(
            "SELECT key, value FROM meta WHERE key LIKE 'runtime_circuit:%'"
        ).fetchall()
        for r in meta_rows:
            if str(r["value"] or "").strip().upper() == "OPEN":
                opened.add(str(r["key"]).split(":", 1)[1].strip().lower())
    finally:
        conn.close()
    return opened


def _build_a2_rules(db: SimingDB) -> dict[str, Any]:
    return {
        "enforced": True,
        "never_bypass_a2": True,
        "decoupled_from_runtime": True,
        "approval_required_actions": [
            "git_apply_worktree",
            "destructive_fs",
            "external_spend",
            "remote_deploy",
            "runtime_dispatch_a2",
        ],
        "tmp_root": str(db.paths.tmp_root),
    }


def get_runtime_profiles(
    db: SimingDB,
    policy: Optional[dict[str, Any]] = None,
) -> list[dict[str, Any]]:
    """返回全部 Linger/Pi 多 Runtime 画像，附带熔断状态与解耦的本地 A2 规则摘要。"""
    db.init_db()
    pol = policy or load_policy(db.paths)
    cp_cfg = pol.get("coding_plan") or {}
    opened = _load_circuit_open_providers(db)
    a2_rules = _build_a2_rules(db)

    claude_bin = str(cp_cfg.get("claude_bin") or shutil.which("claude") or "claude")
    codex_bin = str(cp_cfg.get("codex_bin") or shutil.which("codex") or "codex")
    pi_bin = str(cp_cfg.get("pi_bin") or shutil.which("pi") or "pi")

    raw_profiles = [
        {
            "name": "claude_sub",
            "kind": "subscription",
            "provider": "claude",
            "quota_key": "claude",
            "model": "claude-opus-sub",
            "binary": claude_bin,
            "command_template": f"{claude_bin} -p {{prompt}}",
            "cost_tier": "subscription_zero_api",
            "cost_per_1k_cny": 0.0,
            "best_for": ["coding", "refactoring", "architecture", "review"],
        },
        {
            "name": "codex_sub",
            "kind": "subscription",
            "provider": "codex",
            "quota_key": "codex",
            "model": "codex-sub",
            "binary": codex_bin,
            "command_template": f"{codex_bin} exec --skip-git-repo-check -C {{cwd}} {{prompt}}",
            "cost_tier": "subscription_zero_api",
            "cost_per_1k_cny": 0.0,
            "best_for": ["coding", "refactoring", "debugging", "tests"],
        },
        {
            "name": "pi_deepseek",
            "kind": "pi",
            "provider": "deepseek",
            "quota_key": "",
            "model": "deepseek-chat",
            "binary": pi_bin,
            "command_template": f"{pi_bin} --provider deepseek --model deepseek-chat -p {{prompt}}",
            "cost_tier": "low_api",
            "cost_per_1k_cny": 0.004,
            "best_for": ["triage", "summary", "classification", "light_coding"],
        },
        {
            "name": "pi_kimi",
            "kind": "pi",
            "provider": "kimi",
            "quota_key": "",
            "model": "kimi-k2",
            "binary": pi_bin,
            "command_template": f"{pi_bin} --provider kimi --model kimi-k2 -p {{prompt}}",
            "cost_tier": "low_api",
            "cost_per_1k_cny": 0.008,
            "best_for": ["long_context", "triage", "summary"],
        },
        {
            "name": "pi_redpill",
            "kind": "pi",
            "provider": "redpill",
            "quota_key": "",
            "model": "phala/deepseek-v4.1-flash",
            "binary": pi_bin,
            "command_template": f"{pi_bin} --provider redpill --model phala/deepseek-v4.1-flash -p {{prompt}}",
            "cost_tier": "low_api",
            "cost_per_1k_cny": 0.01,
            "best_for": ["custom_api", "triage", "uncensored"],
        },
        {
            "name": "pi_local_ollama",
            "kind": "pi",
            "provider": "ollama",
            "quota_key": "",
            "model": "qwen2.5-coder:14b",
            "binary": pi_bin,
            "command_template": f"{pi_bin} --provider ollama --model qwen2.5-coder:14b -p {{prompt}}",
            "cost_tier": "local_free",
            "cost_per_1k_cny": 0.0,
            "best_for": ["local", "privacy", "offline", "triage"],
        },
    ]

    out: list[dict[str, Any]] = []
    for p in raw_profiles:
        prov = p["provider"].lower()
        pname = p["name"].lower()
        is_open = (
            prov in opened
            or pname in opened
            or any(tok.startswith(f"{prov}_") or tok.startswith(f"{prov}-") for tok in opened)
        )
        item = dict(p)
        item["circuit_open"] = is_open
        item["a2_rules"] = dict(a2_rules)
        out.append(item)
    return out


def _quota_headroom_for(profile: dict[str, Any], quota: dict[str, Any]) -> tuple[bool, Optional[float], str]:
    """返回 (是否满足余量要求, 剩余百分比, 跳过说明)。"""
    qk = profile.get("quota_key") or ""
    if not qk:
        return (True, None, "")
    avail_key = f"{qk}_available"
    rem_key = f"{qk}_remaining"
    avail = quota.get(avail_key)
    rem = quota.get(rem_key)
    if avail is False:
        return (False, float(rem) if isinstance(rem, (int, float)) else 0.0, f"{qk} subscription unavailable")
    if isinstance(rem, (int, float)):
        return (True, float(rem), "")
    return (True, None, "")


def select_runtime(
    db: SimingDB,
    task_kind: str = "coding",
    complexity: str = "",
    quota_snapshot: Optional[dict[str, Any]] = None,
    preferred: str = "",
    policy: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """根据任务类型、复杂度、ai-quota 水位与 provider 熔断状态选择最优 Runtime 与回退链。"""
    db.init_db()
    pol = policy or load_policy(db.paths)
    min_pct = float((pol.get("coding_plan") or {}).get("min_remaining_percent", 10.0))

    if quota_snapshot is not None:
        quota = dict(quota_snapshot)
    elif os.environ.get("SIMING_TEST_OFFLINE") == "1":
        quota = {"ok": False, "claude_available": True, "codex_available": True}
    else:
        quota = fetch_ai_quota(db)

    profiles = get_runtime_profiles(db, policy=pol)
    by_name = {p["name"]: p for p in profiles}

    kind_norm = (task_kind or "coding").strip().lower()
    comp_norm = (complexity or "").strip().lower()
    if not comp_norm:
        comp_norm = "low" if kind_norm in _LIGHT_TASK_KINDS else "high"
    pref_norm = (preferred or "").strip().lower()

    is_heavy = (comp_norm in ("high", "heavy")) or (
        kind_norm in _HEAVY_TASK_KINDS and comp_norm != "low"
    )
    if kind_norm in ("local", "privacy", "triage", "summary", "classification", "custom_api") and comp_norm != "high":
        is_heavy = False
    if kind_norm in ("local", "privacy"):
        is_heavy = False

    # 构建候选顺序
    if is_heavy:
        # 订阅 CLI 按剩余配额从高到低排序，零额外 API 费优先
        subs = [by_name["claude_sub"], by_name["codex_sub"]]
        def _sub_sort_key(p: dict[str, Any]) -> tuple[int, float]:
            _, rem, _ = _quota_headroom_for(p, quota)
            is_pref = 1 if p["name"] == pref_norm else 0
            return (is_pref, rem if rem is not None else 50.0)
        subs.sort(key=_sub_sort_key, reverse=True)
        pi_chain = [
            by_name["pi_deepseek"],
            by_name["pi_kimi"],
            by_name["pi_redpill"],
            by_name["pi_local_ollama"],
        ]
        ordered = subs + pi_chain
    else:
        if kind_norm in ("local", "privacy"):
            pi_order = ["pi_local_ollama", "pi_deepseek", "pi_kimi", "pi_redpill"]
        elif kind_norm == "custom_api":
            pi_order = ["pi_redpill", "pi_deepseek", "pi_kimi", "pi_local_ollama"]
        else:
            pi_order = ["pi_deepseek", "pi_kimi", "pi_redpill", "pi_local_ollama"]
        ordered = [by_name[n] for n in pi_order] + [by_name["claude_sub"], by_name["codex_sub"]]

    if pref_norm in by_name:
        pref_prof = by_name[pref_norm]
        ordered = [pref_prof] + [p for p in ordered if p["name"] != pref_norm]

    eligible: list[dict[str, Any]] = []
    skipped: list[dict[str, str]] = []

    for prof in ordered:
        if prof["circuit_open"]:
            skipped.append({"runtime": prof["name"], "reason": f"provider `{prof['provider']}` circuit_open"})
            continue
        if prof["kind"] == "subscription":
            avail_ok, rem, avail_err = _quota_headroom_for(prof, quota)
            if not avail_ok:
                skipped.append({"runtime": prof["name"], "reason": avail_err})
                continue
            if rem is not None and rem < min_pct:
                skipped.append(
                    {
                        "runtime": prof["name"],
                        "reason": f"quota {rem:.1f}% < threshold {min_pct:.1f}%",
                    }
                )
                continue
        eligible.append(prof)

    # 极端情况：若全部都被跳过，退回 pi_local_ollama 兜底保证不崩溃
    if not eligible:
        selected = by_name["pi_local_ollama"]
        fallbacks: list[dict[str, Any]] = []
        reason = "all_primary_unavailable_fallback_local"
    else:
        selected = eligible[0]
        fallbacks = eligible[1:]
        if is_heavy and selected["kind"] == "subscription":
            _, rem, _ = _quota_headroom_for(selected, quota)
            rem_s = f"{rem:.1f}%" if rem is not None else "unknown"
            reason = f"heavy_{kind_norm}_zero_api_subscription(headroom={rem_s})"
        elif is_heavy and selected["kind"] == "pi":
            reason = f"heavy_{kind_norm}_fallback_to_pi(subscriptions_exhausted_or_open)"
        else:
            reason = f"lightweight_{kind_norm}_pi_runtime"

    return {
        "ok": True,
        "runtime": selected["name"],
        "kind": selected["kind"],
        "provider": selected["provider"],
        "model": selected["model"],
        "cost_tier": selected["cost_tier"],
        "profile": selected,
        "reason": reason,
        "fallback_chain": [p["name"] for p in fallbacks],
        "command_template": selected["command_template"],
        "a2_rules": _build_a2_rules(db),
        "skipped": skipped,
    }


def dispatch_via_runtime(
    db: SimingDB,
    task: str,
    task_kind: str = "coding",
    complexity: str = "high",
    cwd: str = "",
    dry_run: bool = True,
    quota_snapshot: Optional[dict[str, Any]] = None,
    runner_fn: Optional[Callable[..., dict[str, Any]]] = None,
    preferred: str = "",
    problem_family: str = "",
    requires_a2: bool = False,
) -> dict[str, Any]:
    """经由 Linger/Pi 调度层派发任务：自动注入 M-SIM 进化记忆、记录 usage，并死守 A2 审批。"""
    db.init_db()
    clean_task = (task or "").strip()
    if not clean_task:
        return {"ok": False, "error": "task 不能为空"}

    work_cwd_path = Path(cwd) if cwd else db.paths.tmp_root
    _reject_slash_tmp(work_cwd_path)
    work_cwd = str(work_cwd_path)

    selection = select_runtime(
        db,
        task_kind=task_kind,
        complexity=complexity,
        quota_snapshot=quota_snapshot,
        preferred=preferred,
    )

    # 死守本地 A2 权限：严禁由任意 Runtime 绕过破坏性或高危写动作审批
    needs_a2 = bool(requires_a2 or _A2_GATED_TASK_RE.search(clean_task))
    if needs_a2:
        app = create_approval(
            db,
            action="runtime_dispatch_a2",
            target_id=selection["runtime"],
            params={
                "runtime": selection["runtime"],
                "task_kind": task_kind,
                "cwd": work_cwd,
                "task_preview": clean_task[:240],
            },
        )
        return {
            "ok": False,
            "a2_blocked": True,
            "approval_token": app["token"],
            "runtime": selection["runtime"],
            "reason": "检测到高危/A2 门禁任务，拒绝由 Runtime 绕过审批直接执行，请先回复 apply 确认。",
            "selection": selection,
        }

    # 注入 M-SIM 跨模型自进化记忆 + Hindsight 共享 Wiki（若有历史经验）
    fam = (problem_family or task_kind or "coding").strip()
    msim_block = build_msim_transfer_prompt(
        db,
        problem_family=fam,
        target_model=selection["model"],
        query=clean_task,
        limit=3,
    )
    wiki_block = ""
    wiki_hits: list[dict[str, Any]] = []
    try:
        from siming.hindsight_wiki import query_hindsight_wiki
        from siming.modules import is_module_enabled

        if is_module_enabled(db, "hindsight_wiki"):
            wiki_hits = query_hindsight_wiki(db, clean_task, limit=2)
            if wiki_hits:
                w_lines = ["## [Hindsight Shared Wiki]"]
                for h in wiki_hits:
                    snip = str(h.get("snippet") or "").replace("\n", " | ")[:220]
                    w_lines.append(f"- [{h['slug']}] {h['title']}: {snip}")
                wiki_block = "\n".join(w_lines)
    except Exception:
        wiki_block = ""
        wiki_hits = []

    ctx_parts = [p for p in (wiki_block, msim_block) if p]
    enriched_prompt = f"{'\n\n'.join(ctx_parts)}\n\n---\nTask:\n{clean_task}" if ctx_parts else clean_task

    cmd_rendered = selection["command_template"].format(
        cwd=work_cwd,
        prompt=enriched_prompt[:120].replace("\n", " "),
    )

    output = ""
    ok = True
    error = ""
    if dry_run:
        output = f"[DRY-RUN:{selection['runtime']}] {cmd_rendered}"
    elif runner_fn is not None:
        res = runner_fn(
            runtime=selection["runtime"],
            model=selection["model"],
            prompt=enriched_prompt,
            cwd=work_cwd,
            command=cmd_rendered,
        )
        ok = bool((res or {}).get("ok", True))
        output = str((res or {}).get("output") or "")
        error = str((res or {}).get("error") or "")
    elif os.environ.get("SIMING_DISABLE_LLM") == "1":
        output = f"[OFFLINE-STUB:{selection['runtime']}] SIMING_DISABLE_LLM=1"
    else:
        output = f"[QUEUED:{selection['runtime']}] {cmd_rendered}"

    tokens_in = max(1, len(enriched_prompt) // 3)
    tokens_out = max(0, len(output) // 3)
    rate = float(selection["profile"].get("cost_per_1k_cny", 0.0))
    cost_cny = round(((tokens_in + tokens_out) / 1000.0) * rate, 6)

    db.record_usage(
        model=selection["model"],
        runner=selection["runtime"],
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        cost_cny=cost_cny,
        ref_id=f"w3:{task_kind}",
    )

    return {
        "ok": ok,
        "dry_run": dry_run,
        "a2_blocked": False,
        "runtime": selection["runtime"],
        "model": selection["model"],
        "provider": selection["provider"],
        "reason": selection["reason"],
        "fallback_chain": selection["fallback_chain"],
        "command": cmd_rendered,
        "cwd": work_cwd,
        "msim_injected": bool(msim_block),
        "msim_prompt": msim_block,
        "wiki_injected": bool(wiki_block),
        "wiki_hits_count": len(wiki_hits),
        "enriched_prompt": enriched_prompt,
        "output": output,
        "error": error,
        "usage": {
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "cost_cny": cost_cny,
        },
        "a2_rules": selection["a2_rules"],
    }


def format_runtime_status(
    db: SimingDB,
    quota_snapshot: Optional[dict[str, Any]] = None,
) -> str:
    """格式化 Linger/Pi 多 Runtime 状态总览。"""
    db.init_db()
    profiles = get_runtime_profiles(db)
    quota = quota_snapshot or {}
    lines = ["⚙️ **Linger/Pi 多 Runtime 调度底座**（编排/记忆/A2 本地解耦）"]

    for p in profiles:
        if p["circuit_open"]:
            badge = "🔴 CIRCUIT_OPEN"
        elif p["kind"] == "subscription":
            qk = p["quota_key"]
            rem = quota.get(f"{qk}_remaining")
            rem_s = f"{float(rem):.0f}%" if isinstance(rem, (int, float)) else "N/A"
            badge = f"🟢 SUB ({rem_s})"
        else:
            badge = "🟢 PI READY"
        lines.append(
            f"• {badge} `{p['name']}` ({p['model']} · {p['cost_tier']}) — 擅长: {', '.join(p['best_for'])}"
        )

    lines.append("• 🛡️ A2 审批规则由司命本地强控，任何订阅 CLI 或 Pi Runtime 均不可绕过。")
    return "\n".join(lines)
