"""Siming v5 Wave 3 (W3-1..W3-7) 全量单元测试：
- W3-1 multi_runtime: Linger 订阅 CLI vs Pi 路由、ai-quota 水位降级、CIRCUIT_OPEN 熔断回退、M-SIM 注入、A2 硬门禁
- W3-2 hindsight_wiki: 多 Agent JSONL/MD 蒸馏、四类事实抽取与原文出处、矛盾与过期冲突检测、Wiki 检索
- W3-3 msim_memory: 跨模型进化日记录入、Claude -> Gemini/DeepSeek 迁移提示（含优胜与反模式）、排行榜与 MD 导出
- W3-4 overnight_lab: 睡前交班白名单拦截危险动作、Loss NaN / GPU 掉卡 / 权限卡住静默兜底、自然醒对比表 + A2 apply 卡
- W3-5 research_pr_radar: 123+ Open PR 噪音过滤（屏蔽 action_required）、痛点匹配论文 + 15m 隔离 worktree 验证命令
- W3-6 digest_indie: 跨平台收藏按活跃项目打分选 Top-3、隔离试跑命令（禁 /tmp）、独游桌游规则交叉融合与 Steam 竞品雷达
- W3-7 infra_healer: Omarchy 升级前置守卫、edge.example.com TLS 到期预警、资源水位与 aTrust/sing-box 白名单静默自愈 + A1 审计
"""

from __future__ import annotations

import json
from datetime import datetime

from siming.db import SHANGHAI_TZ, SimingDB, iso_now
from siming.digest_indie import (
    build_isolated_trial_cmd,
    build_weekly_digest_card,
    format_indie_brief,
    ideate_indie_game,
    score_and_rank_bookmarks,
)
from siming.hindsight_wiki import (
    detect_fact_conflicts,
    distill_hindsight_wiki,
    extract_facts_from_utterances,
    format_wiki_status,
    query_hindsight_wiki,
)
from siming.infra_healer import (
    check_proxy_and_dns_drift,
    check_tls_cert_expiry,
    format_infra_report,
    run_infra_patrol,
    run_omarchy_upgrade_guard,
)
from siming.msim_memory import (
    build_msim_transfer_prompt,
    export_msim_markdown,
    get_msim_leaderboard,
    record_msim_entry,
    search_msim_entries,
)
from siming.multi_runtime import (
    dispatch_via_runtime,
    format_runtime_status,
    get_runtime_profiles,
    select_runtime,
)
from siming.overnight_lab import (
    build_morning_handoff_report,
    inspect_handoff_log,
    patrol_overnight_handoffs,
    register_overnight_handoff,
)
from siming.research_pr_radar import (
    add_pain_point,
    format_radar_digest,
    match_research_items,
    triage_open_prs,
)


# ---------------------------------------------------------------------------
# W3-1: Linger/Pi 多 Runtime 调度底座
# ---------------------------------------------------------------------------
def test_w3_1_multi_runtime_routing_fallback_msim_and_a2(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-22T10:00:00+08:00")

    profiles = get_runtime_profiles(sandbox_db)
    names = {p["name"] for p in profiles}
    assert {"claude_sub", "codex_sub", "pi_deepseek", "pi_kimi", "pi_redpill", "pi_local_ollama"} <= names
    assert all(p["a2_rules"]["never_bypass_a2"] for p in profiles)

    # 1) 重型编码任务 -> 按 ai-quota 余量优先选择零 API 费用的订阅 CLI（codex 余量 82% > claude 35%）
    heavy = select_runtime(
        sandbox_db,
        task_kind="coding",
        complexity="high",
        quota_snapshot={
            "claude_available": True,
            "claude_remaining": 35.0,
            "codex_available": True,
            "codex_remaining": 82.0,
        },
    )
    assert heavy["runtime"] == "codex_sub"
    assert heavy["kind"] == "subscription"
    assert "claude_sub" in heavy["fallback_chain"]
    assert heavy["a2_rules"]["enforced"] is True

    # 2) 轻量分拣任务 -> 直接路由到 Pi runtime（不消耗昂贵订阅额度）
    light = select_runtime(sandbox_db, task_kind="triage", complexity="low")
    assert light["runtime"] == "pi_deepseek"
    assert light["kind"] == "pi"

    # 3) 当订阅余量低于阈值且 deepseek 处于 CIRCUIT_OPEN 时 -> 自动跳过并无缝回退到 pi_kimi
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO compute_assets (asset_id, provider, account_label, status, updated_at)
            VALUES ('CA-ds', 'deepseek', 'pi_deepseek', 'CIRCUIT_OPEN', ?)
            """,
            (iso_now(),),
        )
    fallback = select_runtime(
        sandbox_db,
        task_kind="refactoring",
        complexity="high",
        quota_snapshot={
            "claude_available": True,
            "claude_remaining": 4.0,
            "codex_available": True,
            "codex_remaining": 6.5,
        },
    )
    assert fallback["runtime"] == "pi_kimi"
    skipped_runtimes = {s["runtime"] for s in fallback["skipped"]}
    assert {"claude_sub", "codex_sub", "pi_deepseek"} <= skipped_runtimes

    # 4) 派单自动注入 M-SIM 进化经验、记录 usage，且绝不绕过 A2 审批
    record_msim_entry(
        sandbox_db,
        problem_family="coding",
        source_model="claude-opus-sub",
        change_rationale="Use SQLite WAL + BEGIN IMMEDIATE for atomic effect enqueue",
        measured_score=0.95,
        baseline_score=0.70,
        passed=True,
    )
    called: list[dict] = []

    def _fake_runner(**kwargs):
        called.append(kwargs)
        return {"ok": True, "output": "patched cleanly"}

    dispatched = dispatch_via_runtime(
        sandbox_db,
        task="Refactor SQLite WAL enqueue helper",
        task_kind="coding",
        complexity="high",
        dry_run=False,
        quota_snapshot={"claude_available": True, "claude_remaining": 70.0, "codex_available": True, "codex_remaining": 50.0},
        runner_fn=_fake_runner,
    )
    assert dispatched["ok"] is True
    assert dispatched["runtime"] == "claude_sub"
    assert dispatched["msim_injected"] is True
    assert "BEGIN IMMEDIATE" in dispatched["enriched_prompt"]
    assert len(called) == 1

    conn = sandbox_db.connect()
    try:
        usage_rows = conn.execute("SELECT runner, model, tokens_in FROM usage").fetchall()
    finally:
        conn.close()
    assert len(usage_rows) >= 1
    assert usage_rows[-1]["runner"] == "claude_sub"

    # A2 拦截：含破坏性动作的任务必须生成 approval token，严禁直接执行
    blocked = dispatch_via_runtime(
        sandbox_db,
        task="Run git push --force origin main right now",
        task_kind="coding",
        complexity="high",
        dry_run=False,
        runner_fn=_fake_runner,
    )
    assert blocked["ok"] is False
    assert blocked["a2_blocked"] is True
    assert blocked["approval_token"].startswith("A")
    assert len(called) == 1  # runner_fn 未被调用

    status_text = format_runtime_status(
        sandbox_db,
        quota_snapshot={"claude_remaining": 70.0, "codex_remaining": 50.0},
    )
    assert "CIRCUIT_OPEN" in status_text
    assert "claude_sub" in status_text


# ---------------------------------------------------------------------------
# W3-2: Hindsight 自动共享 Wiki
# ---------------------------------------------------------------------------
def test_w3_2_hindsight_wiki_distill_provenance_and_conflicts(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-22T02:30:00+08:00")

    # 1) 抽取四类事实 + 冲突与陈旧度检测单元测试
    utterances = [
        {
            "text": "严禁写入 /tmp 目录，本机 tmpfs 写满会拖挂 Hyprland。",
            "source": "claude-session-1.jsonl#L3",
            "agent": "claude",
            "timestamp": "2026-11-20T10:00:00+08:00",
        },
        {
            "text": "我们决定采用 pixi 作为 Python 环境管理工具。",
            "source": "codex-session-2.jsonl#L8",
            "agent": "codex",
            "timestamp": "2026-06-01T10:00:00+08:00",
        },
        {
            "text": "根因是 sing-box 残留 NXDOMAIN 负缓存，已修复：调用 /cache/dns/flush 实测有效。",
            "source": "hermes-session-3.jsonl#L15",
            "agent": "hermes",
            "timestamp": "2026-11-21T09:00:00+08:00",
        },
        {
            "text": "我偏好在 pytest 里使用 frozen_clock 冻结墙钟时间。",
            "source": "agy-session-4.jsonl#L2",
            "agent": "agy",
            "timestamp": "2026-11-21T11:00:00+08:00",
        },
    ]
    facts = extract_facts_from_utterances(utterances)
    cats = {f["category"] for f in facts}
    assert cats == {"taboo", "decision", "verified_fix", "preference"}
    assert all(f["verbatim"] and f["source"] for f in facts)

    # 检测矛盾（偏好用 pip vs 决定采用 pixi，以及允许/禁用冲突）与陈旧度（>90天）
    incoming_facts = extract_facts_from_utterances(
        [
            {
                "text": "偏好统一使用 uv 来管理 Python 包依赖。",
                "source": "pi-session-5.jsonl#L4",
                "agent": "pi",
                "timestamp": "2026-11-22T01:00:00+08:00",
            },
            {
                "text": "不要用 frozen_clock 冻结测试时间。",
                "source": "pi-session-5.jsonl#L9",
                "agent": "pi",
                "timestamp": "2026-11-22T01:05:00+08:00",
            },
        ]
    )
    conflicts = detect_fact_conflicts(facts, incoming_facts)
    assert any(c["kind"] == "contradiction" and c["topic"] == "package_manager" for c in conflicts)
    assert any(c["kind"] == "contradiction" and c["topic"] == "test_runner" for c in conflicts)

    # 2) 端到端从 JSONL + MD 会话文件蒸馏到 Obsidian 共享 Wiki 与 wiki_pages 表
    sess_dir = sandbox_db.paths.tmp_root / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    claude_jsonl = sess_dir / "claude_20261121.jsonl"
    claude_jsonl.write_text(
        "\n".join(
            [
                json.dumps({"role": "user", "content": "严禁写入 /tmp 目录，请统一使用 ~/.cache/agent-tmp。", "timestamp": "2026-11-21T20:00:00+08:00"}),
                json.dumps({"role": "assistant", "content": "根因是 9222 端口被抢占，已修复：自动回退 [::1]:9222 实测有效。", "timestamp": "2026-11-21T20:05:00+08:00"}),
            ]
        ),
        encoding="utf-8",
    )
    notes_md = sess_dir / "codex_notes.md"
    notes_md.write_text(
        "- 决定采用 SQLite WAL 模式存储司命全部状态。\n"
        "- 偏好优先用 /tmp 存放临时文件。\n",
        encoding="utf-8",
    )

    res = distill_hindsight_wiki(
        sandbox_db,
        session_files=[claude_jsonl, notes_md],
        now=datetime(2026, 11, 22, 2, 30, tzinfo=SHANGHAI_TZ),
    )
    assert res["ok"] is True
    assert res["facts_extracted"] == 4
    assert any(c["kind"] == "contradiction" and c["topic"] == "tmp_directory" for c in res["conflicts"])
    assert (sandbox_db.paths.shared_memory_dir / "hindsight-wiki" / "INDEX.md").is_file()
    assert (sandbox_db.paths.shared_memory_dir / "hindsight-wiki" / "taboos.md").is_file()

    taboo_md = (sandbox_db.paths.shared_memory_dir / "hindsight-wiki" / "taboos.md").read_text(encoding="utf-8")
    assert "claude_20261121.jsonl#L1" in taboo_md
    assert "Conflicts & Staleness" in taboo_md

    hits = query_hindsight_wiki(sandbox_db, "SQLite WAL", limit=3)
    assert len(hits) >= 1
    assert hits[0]["slug"] == "decisions"

    status = format_wiki_status(sandbox_db)
    assert "Hindsight" in status
    assert "冲突" in status


# ---------------------------------------------------------------------------
# W3-3: M-SIM 跨模型自进化日记
# ---------------------------------------------------------------------------
def test_w3_3_msim_cross_model_transfer_and_leaderboard(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-22T11:00:00+08:00")

    e1 = record_msim_entry(
        sandbox_db,
        problem_family="rag_rerank",
        source_model="claude-opus-4",
        change_rationale="Add BM25 + dense hybrid RRF fusion with k=60",
        expected_outcome="Improve Recall@5 on boardgame rules",
        measured_score=0.89,
        baseline_score=0.74,
        passed=True,
        eval_context="boardgame_kb-eval-v2",
        target_artifact="src/rerank.py",
        tags=["rag", "rrf"],
    )
    e2 = record_msim_entry(
        sandbox_db,
        problem_family="rag_rerank",
        source_model="claude-opus-4",
        change_rationale="Drop chunk overlap to 0 to save tokens",
        expected_outcome="Lower latency without recall drop",
        measured_score=0.61,
        baseline_score=0.74,
        passed=False,
        eval_context="boardgame_kb-eval-v2",
        target_artifact="src/chunker.py",
        tags=["rag", "regression"],
    )
    record_msim_entry(
        sandbox_db,
        problem_family="rag_rerank",
        source_model="deepseek-v4",
        change_rationale="Keep 128-token overlap and prepend rule section header",
        measured_score=0.93,
        baseline_score=0.89,
        passed=True,
        eval_context="boardgame_kb-eval-v2",
        tags=["rag", "chunking"],
    )

    assert e1["delta"] == 0.15
    assert e2["delta"] == -0.13

    found = search_msim_entries(sandbox_db, problem_family="rag_rerank", query="overlap", limit=5)
    assert len(found) == 2

    # 跨模型迁移：Claude Opus + DeepSeek -> Gemini 2.5 Pro
    prompt_block = build_msim_transfer_prompt(
        sandbox_db,
        problem_family="rag_rerank",
        target_model="gemini-2.5-pro",
        limit=5,
    )
    assert "target=gemini-2.5-pro" in prompt_block
    assert "claude-opus-4" in prompt_block
    assert "Verified Winning Strategies" in prompt_block
    assert "BM25 + dense hybrid RRF" in prompt_block
    assert "Warned Regressions & Anti-Patterns" in prompt_block
    assert "Drop chunk overlap to 0" in prompt_block

    board = get_msim_leaderboard(sandbox_db)
    assert board["total_entries"] == 3
    assert board["passed_count"] == 2
    assert board["failed_count"] == 1
    assert board["by_family"]["rag_rerank"]["best_model"] == "claude-opus-4"
    assert board["by_family"]["rag_rerank"]["best_delta"] == 0.15
    assert board["by_model"]["deepseek-v4"]["pass_rate"] == 1.0

    md_path = export_msim_markdown(sandbox_db)
    assert md_path.is_file()
    md_text = md_path.read_text(encoding="utf-8")
    assert "rag_rerank" in md_text
    assert "deepseek-v4" in md_text


# ---------------------------------------------------------------------------
# W3-4: 睡前异步实验交班
# ---------------------------------------------------------------------------
def test_w3_4_overnight_lab_silent_remediation_and_morning_report(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-22T01:30:00+08:00")

    # 1) 登记交班单，确认危险动作被自动剔除
    h1 = register_overnight_handoff(
        sandbox_db,
        name="personas-sft-lr3e5",
        host="gpu-server",
        pane_or_pid="tmux:gpu-server:1.0",
        whitelist=["checkpoint_save", "safe_resume", "rm_rf_checkpoints", "git_push_force"],
    )
    assert h1["whitelist"] == ["checkpoint_save", "safe_resume"]
    assert set(h1["rejected_unsafe_actions"]) == {"rm_rf_checkpoints", "git_push_force"}

    h2 = register_overnight_handoff(
        sandbox_db,
        name="indie_game-eval-sweep",
        host="gpu-server",
        pane_or_pid="tmux:gpu-server:2.0",
    )

    # 2) 日志解析能力验证
    nan_diag = inspect_handoff_log("step=1200 epoch=2 loss=0.412\nstep=1250 loss=NaN\n")
    assert nan_diag["state"] == "loss_nan"
    assert nan_diag["metrics"]["step"] == 1250
    assert nan_diag["recommended_action"] == "checkpoint_save"

    gpu_diag = inspect_handoff_log("step=500 loss=0.68\nNCCL timeout / NVRM: Xid 79 GPU has fallen off the bus\n")
    assert gpu_diag["state"] == "gpu_drop"
    assert gpu_diag["recommended_action"] == "safe_resume"

    # 3) 夜间静默巡检：Loss NaN 自动 checkpoint，GPU 掉卡自动 checkpoint + safe_resume，零吵醒
    executed_actions: list[tuple[str, str]] = []

    def _log_reader(h: dict) -> str:
        if h["handoff_id"] == h1["handoff_id"]:
            return "step=2400 epoch=3 loss=nan val_loss=1.21\n"
        return "step=800 acc=84.5% CUDA error: device-side assert triggered\n"

    def _action_runner(h: dict, act: str) -> dict:
        executed_actions.append((h["handoff_id"], act))
        return {"ok": True, "acted": act}

    patrol = patrol_overnight_handoffs(
        sandbox_db,
        log_reader_fn=_log_reader,
        action_fn=_action_runner,
    )
    assert patrol["woke_user"] is False
    assert patrol["checked"] == 2
    assert (h1["handoff_id"], "checkpoint_save") in executed_actions
    assert (h2["handoff_id"], "checkpoint_save") in executed_actions
    assert (h2["handoff_id"], "safe_resume") in executed_actions

    # 4) 自然醒战报对比表格 + A2 apply 口令卡
    frozen_clock("2026-11-22T10:45:00+08:00")
    morning = build_morning_handoff_report(sandbox_db)
    assert morning["ok"] is True
    assert morning["count"] == 2
    assert "personas-sft-lr3e5" in morning["markdown_table"]
    assert "CHECKPOINTED" in morning["markdown_table"]
    assert "RESUMED" in morning["markdown_table"]
    assert morning["approval_token"].startswith("A")
    assert f"apply {morning['approval_token']}" in morning["summary"]


# ---------------------------------------------------------------------------
# W3-5: 精准科研与 PR 雷达
# ---------------------------------------------------------------------------
def test_w3_5_research_pain_point_worktree_and_pr_noise_suppression(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-22T14:00:00+08:00")

    # 1) 登记痛点并匹配 arXiv / GitHub release -> 生成 15 分钟隔离 worktree 验证命令（禁 /tmp）
    pp = add_pain_point(
        sandbox_db,
        repo="personas",
        topic="kv_cache_quantization",
        keywords=["kv cache", "fp8", "vllm", "speculative decoding"],
    )
    assert pp["pp_id"].startswith("PP")

    papers = [
        {
            "id": "arxiv-2611.04201",
            "kind": "arxiv",
            "title": "Sub-4-bit KV Cache Quantization with FP8 Speculative Decoding for vLLM",
            "summary": "Cuts memory bandwidth by 60% with near-zero perplexity loss.",
            "url": "https://arxiv.org/abs/2611.04201",
            "tags": ["llm", "inference"],
        },
        {
            "id": "arxiv-2611.09999",
            "kind": "arxiv",
            "title": "Deep Sea Coral Reef Classification via Sonar",
            "summary": "Marine biology imaging dataset.",
            "url": "https://arxiv.org/abs/2611.09999",
        },
    ]
    matched = match_research_items(sandbox_db, papers)
    assert len(matched) == 1
    assert matched[0]["repo"] == "personas"
    assert "git worktree add" in matched[0]["verify_cmd"]
    assert str(sandbox_db.paths.worktrees_dir) in matched[0]["verify_cmd"]
    assert " /tmp/" not in matched[0]["verify_cmd"]

    # 2) 125 条 Open PR 压测：120 条 action_required 噪音全部屏蔽，只浮出 3 类高信号压缩为 <=3 张卡
    prs = [
        {
            "repo": "org/repo-a",
            "number": i,
            "title": f"chore: automated update #{i}",
            "ci_status": "action_required",
            "review_decision": "REVIEW_REQUIRED",
            "mergeable": "MERGEABLE",
        }
        for i in range(1, 121)
    ]
    prs.extend(
        [
            {
                "repo": "vllm-project/vllm",
                "number": 9001,
                "title": "fix: fp8 kv cache scale sync",
                "review_decision": "APPROVED",
                "ci_status": "success",
                "mergeable": "MERGEABLE",
            },
            {
                "repo": "simplexai-labs/linger",
                "number": 42,
                "title": "feat: add pi runtime adapter",
                "ci_status": "action_required",
                "latest_comment": "Hey @devuser could you split the adapter config?",
                "latest_comment_role": "MAINTAINER",
            },
            {
                "repo": "NousResearch/hermes-agent",
                "number": 318,
                "title": "fix: qqbot retry backoff",
                "mergeable": "CONFLICTING",
                "ci_status": "pending",
            },
            {
                "repo": "org/draft-repo",
                "number": 77,
                "title": "wip: draft experiment",
                "draft": True,
                "mergeable": "CONFLICTING",
            },
        ]
    )
    triage = triage_open_prs(prs, my_login="devuser")
    assert triage["total"] == 124
    assert triage["actionable_count"] == 3
    assert triage["silenced_noise_count"] == 121
    assert len(triage["cards"]) == 3
    assert triage["actionable"][0]["signal"] == "ready_to_merge"
    assert triage["actionable"][1]["signal"] == "maintainer_mention"
    assert triage["actionable"][2]["signal"] == "merge_conflict"

    digest = format_radar_digest(sandbox_db, prs=prs, papers=papers, my_login="devuser")
    assert "已静默降噪 `121`" in digest
    assert "git worktree add" in digest


# ---------------------------------------------------------------------------
# W3-6: 收藏消化与独游工坊
# ---------------------------------------------------------------------------
def test_w3_6_bookmark_weekly_digest_and_indie_workshop(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-22T20:00:00+08:00")
    ts = iso_now()

    with sandbox_db.transaction() as conn:
        bookmarks_seed = [
            (
                "BM-1",
                "github",
                "simplexai-labs/linger",
                "https://github.com/simplexai-labs/linger",
                "simplexai-labs/linger",
                "Multi-runtime agent orchestrator with Hindsight wiki memory",
                json.dumps(["siming"]),
                json.dumps(["agent", "runtime", "wiki"]),
            ),
            (
                "BM-2",
                "github",
                "haos-fresh-air-bridge",
                "https://github.com/demo/haos-fresh-air-bridge",
                "demo/haos-fresh-air-bridge",
                "Home Assistant 新风与甲醛联动自动化 blueprint",
                json.dumps(["deco"]),
                json.dumps(["haos", "智能家居"]),
            ),
            (
                "BM-3",
                "article",
                "BGG Top 100 桌游规则书 RAG 切分实践",
                "https://example.com/bgg-rag",
                "",
                "Boardgame rulebook chunking for boardgame_kb",
                json.dumps(["boardgame_kb"]),
                json.dumps(["桌游", "rag"]),
            ),
            (
                "BM-4",
                "product",
                "抖音网红咖啡杯开箱",
                "https://douyin.com/video/12345",
                "",
                "普通生活好物推荐",
                json.dumps([]),
                json.dumps(["生活"]),
            ),
        ]
        for row in bookmarks_seed:
            conn.execute(
                """
                INSERT INTO bookmarks (
                    bookmark_id, kind, title, url, repo_slug, summary,
                    related_projects_json, tags_json, source_event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?)
                """,
                (*row, ts),
            )

    ranked = score_and_rank_bookmarks(sandbox_db, limit=3)
    assert len(ranked) == 3
    ids = [r["bookmark_id"] for r in ranked]
    assert "BM-4" not in ids
    assert all(" /tmp/" not in r["trial_cmd"] and str(sandbox_db.paths.tmp_root) in r["trial_cmd"] for r in ranked)
    bm1 = next(r for r in ranked if r["bookmark_id"] == "BM-1")
    assert "git clone --depth 1" in build_isolated_trial_cmd(bm1, tmp_root=sandbox_db.paths.tmp_root)

    card = build_weekly_digest_card(sandbox_db, limit=3)
    assert card["count"] == 3
    assert "周日收藏消化 Top-3" in card["markdown"]
    assert "simplexai-labs/linger" in card["markdown"]

    # 独游工坊：使用测试沙箱内的自定义桌游规则语料库（绝不触碰真实 /home/devuser 目录）
    corpus_dir = sandbox_db.paths.tmp_root / "rules_corpus"
    corpus_dir.mkdir(parents=True, exist_ok=True)
    (corpus_dir / "dune_imperium.md").write_text(
        "# Dune: Imperium\n核心机制：deckbuilding 牌库构筑 + worker placement 工人放置双轨联动。",
        encoding="utf-8",
    )
    brief = ideate_indie_game(
        topic="赛博茶馆经营肉鸽",
        mechanics=["deckbuilding", "worker_placement"],
        rules_corpus=corpus_dir,
    )
    assert brief["ok"] is True
    assert brief["referenced_boardgames"][0]["name"] == "dune_imperium"
    assert len(brief["steam_comp_radar"]) >= 2
    formatted = format_indie_brief(brief)
    assert "赛博茶馆经营肉鸽" in formatted
    assert "Steam 竞品雷达" in formatted


# ---------------------------------------------------------------------------
# W3-7: 数字基建自愈运维官
# ---------------------------------------------------------------------------
def test_w3_7_infra_healer_omarchy_tls_proxy_and_whitelist_audit(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-22T15:00:00+08:00")

    # 1) Omarchy 升级守卫：检测 pacman 锁与低磁盘空间并输出快照计划
    om = run_omarchy_upgrade_guard(
        probe_fn=lambda: {
            "pacman_lock_present": True,
            "conflicting_packages": ["hyprland-git vs hyprland"],
            "held_packages": ["linux"],
            "disk_free_gb": 8.2,
        }
    )
    assert om["safe_to_upgrade"] is False
    assert len(om["blockers"]) == 3
    assert any("snapper" in step for step in om["snapshot_plan"])

    # 2) TLS 证书到期检测（剩余 9 天 < 14 天预警）
    tls = check_tls_cert_expiry(
        host="edge.example.com",
        port=8443,
        warn_days=14,
        cert_probe_fn=lambda h, p: {"ok": True, "not_after": "2026-12-01T15:00:00+08:00"},
    )
    assert tls["status"] == "WARN"
    assert tls["needs_renewal"] is True
    assert 8.5 <= tls["days_remaining"] <= 9.5

    # 3) Proxy / DNS 漂移检测与白名单静默自愈（严禁执行非白名单高危动作）
    drift = check_proxy_and_dns_drift(
        probe_fn=lambda: {
            "direct_dns_ok": True,
            "proxy_dns_ok": False,
            "dns_hijacked": True,
            "singbox_route_drift": True,
            "extra_actions": ["reboot_machine"],
        }
    )
    assert drift["healthy"] is False
    assert "flush_dns_cache" in drift["recommended_heals"]
    assert "restart_singbox_dns" in drift["recommended_heals"]

    heals_called: list[str] = []

    def _fake_heal(action: str, ctx: dict) -> dict:
        heals_called.append(action)
        return {"ok": True, "healed": action}

    report = run_infra_patrol(
        sandbox_db,
        auto_heal=True,
        probe_fns={
            "omarchy": lambda: {"pacman_lock_present": False, "disk_free_gb": 65.0},
            "tls": lambda h, p: {"ok": True, "days_remaining": 10.0},
            "resources": lambda: {"mem_used_pct": 88.5, "disk81_used_pct": 72.0},
            "proxy_dns": lambda: {
                "direct_dns_ok": True,
                "proxy_dns_ok": False,
                "dns_hijacked": False,
                "singbox_route_drift": True,
                "extra_actions": ["sudo_rm_var_cache"],
            },
        },
        heal_fn=_fake_heal,
    )
    assert report["overall_status"] == "WARN"
    assert heals_called == ["flush_dns_cache", "restart_singbox_dns"]
    assert report["blocked_non_whitelisted"] == ["sudo_rm_var_cache"]

    # 验证 A1 审计日志完整落盘
    conn = sandbox_db.connect()
    try:
        audit_actions = [
            str(r["action"])
            for r in conn.execute("SELECT action, tier FROM audit ORDER BY id ASC").fetchall()
        ]
    finally:
        conn.close()
    assert "infra_heal:flush_dns_cache" in audit_actions
    assert "infra_heal:restart_singbox_dns" in audit_actions
    assert "infra_heal_blocked_non_whitelist" in audit_actions

    text = format_infra_report(report)
    assert "edge.example.com:8443" in text
    assert "flush_dns_cache" in text
    assert "sudo_rm_var_cache" in text
