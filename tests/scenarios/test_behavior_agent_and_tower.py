"""Siming v5 Behavioral UX & Cross-Module Linkage Scenario Suite.

Focuses on human common-sense expectations across:
1. iOS Drop / Screenshot Triage (`superagent_triage.py`)
2. Hindsight Wiki Contradiction Detection & Incremental Freshness (`hindsight_wiki.py` + Tower)
3. M-SIM Cross-Model Learning Transfer (`msim_memory.py` + `worker.py`)
4. Multi-Runtime Quota & Circuit-Open Switching (`multi_runtime.py` + `worker.py`)
5. Control Tower Management Console Real UX (`control_tower.py` + `evald.py`)
6. Overnight Lab Natural-Wake Workflow (`overnight_lab.py`)
7. Weekly Bookmark Digest Ranking Realism (`digest_indie.py` + `superagent_triage.py`)
"""

from __future__ import annotations

import json
import shlex
import threading
import urllib.request
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from siming.control_tower import create_tower_server, render_tower_html
from siming.db import SHANGHAI_TZ, SimingDB, iso_now
from siming.digest_indie import build_isolated_trial_cmd, build_weekly_digest_card
from siming.evald import run_tick
from siming.hindsight_wiki import (
    detect_fact_conflicts,
    distill_hindsight_wiki,
    extract_facts_from_utterances,
)
from siming.mandate import Mandate
from siming.msim_memory import build_msim_transfer_prompt, record_msim_entry
from siming.multi_runtime import select_runtime
from siming.overnight_lab import (
    build_morning_handoff_report,
    inspect_handoff_log,
    patrol_overnight_handoffs,
    register_overnight_handoff,
)
from siming.superagent_triage import save_bookmark_from_drop, triage_inbound_drop
from siming.worker import build_worker_knowledge_context, select_worker_runtime


@contextmanager
def _running_tower(db: SimingDB, token: str = "behav-tower-token-2026") -> Iterator[tuple[str, dict[str, str]]]:
    server = create_tower_server(db, host="127.0.0.1", port=0, token=token)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    hdrs = {"Authorization": f"Bearer {token}"}
    try:
        yield base_url, hdrs
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=3)


def _http_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    hdrs = dict(headers or {})
    body: bytes | None = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(f"{base_url}{path}", data=body, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


# ===========================================================================
# Scenario Group 1: iOS Drop / Screenshot Triage Expectations
# ===========================================================================
def test_behavior_1_ios_drop_bookmarks_dedup_and_action_protection(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-23T10:00:00+08:00")
    ref_dt = datetime(2026, 11, 23, 10, 0, tzinfo=SHANGHAI_TZ)

    # 1) GitHub 仓库截图/链接：规范化 URL（绝无 github.com/github.com/ 双重前缀），不污染待办，重复投递去重
    gh1 = triage_inbound_drop(
        sandbox_db,
        "GitHub\ngithub.com/we-promise/sure\nOpen-source personal finance ledger and cashflow tracker",
        ref_dt=ref_dt,
    )
    assert gh1["intent"] == "BOOKMARK"
    assert gh1["repo_slug"] == "we-promise/sure"
    assert gh1["url"] == "https://github.com/we-promise/sure"
    assert "github.com/github.com/" not in gh1["url"]

    # 重复投递同一个仓库（含双重前缀 OCR 脏文本与 .git 后缀）必须命中去重，而不是建第二条收藏！
    gh2 = triage_inbound_drop(
        sandbox_db,
        "https://github.com/github.com/we-promise/sure.git\n个人记账开源项目",
        ref_dt=ref_dt,
    )
    assert gh2["intent"] == "BOOKMARK"
    assert gh2.get("duplicate") is True
    assert gh2["bookmark_id"] == gh1["bookmark_id"]
    assert gh2["url"] == "https://github.com/we-promise/sure"

    # 2) X 帖子截图与 arXiv 论文截图：必须归为文章收藏并保留真实 URL，绝不能误解析成假 GitHub 仓库！
    x_shot = triage_inbound_drop(
        sandbox_db,
        "帖子\n@karpathy\nhttps://x.com/karpathy/status/1888888888\nBuilding a Personal Agent OS with Hindsight Wiki & MCP",
        ref_dt=ref_dt,
    )
    assert x_shot["intent"] == "BOOKMARK"
    assert x_shot["repo_slug"] == ""
    assert x_shot["url"] == "https://x.com/karpathy/status/1888888888"
    assert "github.com" not in x_shot["url"]

    x_dup = triage_inbound_drop(
        sandbox_db,
        "帖子\n@karpathy\nhttps://x.com/karpathy/status/1888888888\nBuilding a Personal Agent OS with Hindsight Wiki & MCP",
        ref_dt=ref_dt,
    )
    assert x_dup["intent"] == "BOOKMARK"
    assert x_dup.get("duplicate") is True
    assert x_dup["bookmark_id"] == x_shot["bookmark_id"]

    arxiv_shot = triage_inbound_drop(
        sandbox_db,
        "arXiv:2501.12345\nhttps://arxiv.org/abs/2501.12345\nSelf-Improvement Memory for Cross-Model Agents",
        ref_dt=ref_dt,
    )
    assert arxiv_shot["intent"] == "BOOKMARK"
    assert arxiv_shot["repo_slug"] == ""
    assert arxiv_shot["url"] == "https://arxiv.org/abs/2501.12345"
    assert "github.com/abs/" not in arxiv_shot["url"]

    arxiv_dup = triage_inbound_drop(
        sandbox_db,
        "arXiv:2501.12345\nhttps://arxiv.org/abs/2501.12345\nSelf-Improvement Memory for Cross-Model Agents",
        ref_dt=ref_dt,
    )
    assert arxiv_dup.get("duplicate") is True
    assert arxiv_dup["bookmark_id"] == arxiv_shot["bookmark_id"]

    # 以上全部收藏投递对 open cases 零污染
    conn = sandbox_db.connect()
    try:
        open_cases_cnt = int(conn.execute("SELECT COUNT(*) FROM cases WHERE status = '待处理'").fetchone()[0])
        bm_cnt = int(conn.execute("SELECT COUNT(*) FROM bookmarks").fetchone()[0])
    finally:
        conn.close()
    assert open_cases_cnt == 0
    assert bm_cnt == 3

    # 3) 明确行动/复诊约定（含普通约定及提及仓库名的会议约定）绝不可被吞为被动收藏！
    hosp = triage_inbound_drop(sandbox_db, "下周四下午三点常用医院复诊", ref_dt=ref_dt)
    assert hosp["intent"] == "COMMITMENT"
    assert "常用医院" in hosp["title"]
    assert "15:00" in hosp["event_at"]

    tech_meeting = triage_inbound_drop(
        sandbox_db,
        "明天下午四点开会评审 we-promise/sure 接入方案",
        ref_dt=ref_dt,
    )
    assert tech_meeting["intent"] == "COMMITMENT"
    assert "16:00" in tech_meeting["event_at"]

    # 4) 快递物流追踪截图（即使不含实付款金额且含追踪链接）绝不可被吞为被动收藏！
    courier = triage_inbound_drop(
        sandbox_db,
        "顺丰速运\n运单号 SF142857999001\n派送中 预计今天送达 北京海淀营业点\nhttps://m.kuaidi100.com/chaxun/sf142857",
        ref_dt=ref_dt,
    )
    assert courier["intent"] != "BOOKMARK"
    assert courier["intent"] in ("CASE", "PARCEL")


# ===========================================================================
# Scenario Group 2: Hindsight Wiki Contradiction Detection & Incremental Freshness
# ===========================================================================
def test_behavior_2_hindsight_wiki_preference_reversal_and_idempotency(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-23T01:00:00+08:00")
    sess_dir = sandbox_db.paths.tmp_root / "wiki_behav"
    sess_dir.mkdir(parents=True, exist_ok=True)

    # 1) 用户最初表达偏好：优先直航 poi_id_str，不要走搜索页
    f1 = sess_dir / "claude_day1.jsonl"
    f1.write_text(
        json.dumps(
            {
                "role": "user",
                "content": "以后美团下单优先直航 poi_id_str，不要走搜索页",
                "timestamp": "2026-11-20T12:00:00+08:00",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    res1 = distill_hindsight_wiki(
        sandbox_db,
        session_files=[f1],
        now=datetime(2026, 11, 20, 23, 0, tzinfo=SHANGHAI_TZ),
    )
    assert res1["ok"] is True
    assert res1["facts_extracted"] == 1
    assert len(res1["conflicts"]) == 0

    # 2) 稍后用户推翻偏好：先走搜索页比价，不要直接进店 -> 必须检出 contradiction！
    f2 = sess_dir / "codex_day2.jsonl"
    f2.write_text(
        json.dumps(
            {
                "role": "user",
                "content": "以后美团下单还是先走搜索页比价，不要直接进店",
                "timestamp": "2026-11-23T00:30:00+08:00",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    # 先直接验证 detect_fact_conflicts 单元行为
    old_f = extract_facts_from_utterances(
        [{"text": "以后美团下单优先直航 poi_id_str，不要走搜索页", "timestamp": "2026-11-20T12:00:00+08:00"}]
    )
    new_f = extract_facts_from_utterances(
        [{"text": "以后美团下单还是先走搜索页比价，不要直接进店", "timestamp": "2026-11-23T00:30:00+08:00"}]
    )
    assert len(old_f) == 1 and len(new_f) == 1
    direct_conflicts = detect_fact_conflicts(old_f, new_f)
    assert len(direct_conflicts) == 1
    assert direct_conflicts[0]["kind"] == "contradiction"

    # 再端到端蒸馏并验证 wiki_pages.conflicts_json 与 Tower /api/learned 暴露冲突
    res2 = distill_hindsight_wiki(
        sandbox_db,
        session_files=[f2],
        now=datetime(2026, 11, 23, 1, 0, tzinfo=SHANGHAI_TZ),
    )
    assert any(c["kind"] == "contradiction" for c in res2["conflicts"])

    conn = sandbox_db.connect()
    try:
        pref_row = conn.execute("SELECT content_md, conflicts_json FROM wiki_pages WHERE slug = 'preferences'").fetchone()
    finally:
        conn.close()
    assert pref_row is not None
    stored_conflicts = json.loads(pref_row["conflicts_json"])
    assert len(stored_conflicts) == 1
    assert stored_conflicts[0]["kind"] == "contradiction"
    assert "搜索页" in stored_conflicts[0]["existing"]["statement"]
    assert "搜索页" in stored_conflicts[0]["incoming"]["statement"]

    # 通过 Tower /api/learned 对外暴露冲突，用户肉眼可见
    with _running_tower(sandbox_db) as (base_url, hdrs):
        st, learned = _http_json(base_url, "/api/learned", headers=hdrs)
        assert st == 200 and learned["ok"] is True
        pref_page = next(p for p in learned["wiki_pages"] if p["slug"] == "preferences")
        assert len(pref_page["conflicts"]) == 1
        assert pref_page["conflicts"][0]["kind"] == "contradiction"

    # 3) 幂等性校验：在未变数据上连跑两次 distill_hindsight_wiki，绝不重复写同一条 Markdown bullet 或重复冲突！
    res3 = distill_hindsight_wiki(
        sandbox_db,
        session_files=[f1, f2],
        now=datetime(2026, 11, 23, 1, 5, tzinfo=SHANGHAI_TZ),
    )
    pref_md_after = (sandbox_db.paths.shared_memory_dir / "hindsight-wiki" / "preferences.md").read_text(encoding="utf-8")
    assert pref_md_after.count("- 以后美团下单优先直航 poi_id_str，不要走搜索页") == 1
    assert pref_md_after.count("- 以后美团下单还是先走搜索页比价，不要直接进店") == 1
    assert len(res3["conflicts"]) == 1

    # 再跑一次仅含无关新笔记的增量蒸馏，既有未裁决冲突仍应保留而非被静默清空
    f3 = sess_dir / "hermes_day3.jsonl"
    f3.write_text(
        json.dumps(
            {
                "role": "user",
                "content": "偏好在终端使用深色模式显示日志",
                "timestamp": "2026-11-23T01:10:00+08:00",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    res4 = distill_hindsight_wiki(
        sandbox_db,
        session_files=[f3],
        now=datetime(2026, 11, 23, 1, 15, tzinfo=SHANGHAI_TZ),
    )
    assert len(res4["conflicts"]) == 1


# ===========================================================================
# Scenario Group 3: M-SIM Cross-Model Learning Transfer
# ===========================================================================
def test_behavior_3_msim_cross_model_transfer_winner_and_antipattern(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-23T11:00:00+08:00")

    # claude-opus 写入同问题域的一条失败反模式 + 两条优胜策略（检验排名与双向召回）
    record_msim_entry(
        sandbox_db,
        problem_family="meituan_dom",
        source_model="claude-opus",
        change_rationale="用合成 click() 点美团地址失败",
        expected_outcome="切换收货地址弹窗",
        measured_score=0.20,
        baseline_score=0.50,
        passed=False,
        eval_context="meituan-h5-9222",
        target_artifact="src/siming/meituan_cart_live.py",
        tags=["meituan", "address"],
    )
    record_msim_entry(
        sandbox_db,
        problem_family="meituan_dom",
        source_model="claude-opus",
        change_rationale="先等待地址列表 DOM 渲染再尝试滚动选择",
        expected_outcome="提升地址选择稳定性",
        measured_score=0.72,
        baseline_score=0.50,
        passed=True,
        eval_context="meituan-h5-9222",
        target_artifact="src/siming/meituan_cart_live.py",
        tags=["meituan", "address"],
    )
    record_msim_entry(
        sandbox_db,
        problem_family="meituan_dom",
        source_model="claude-opus",
        change_rationale="改用 CDP trusted touch 选地址成功",
        expected_outcome="100% 触发美团地址切换",
        measured_score=0.95,
        baseline_score=0.50,
        passed=True,
        eval_context="meituan-h5-9222",
        target_artifact="src/siming/meituan_cart_live.py",
        tags=["meituan", "cdp", "touch"],
    )

    # 1) 窄关键词查询（只命中 CDP touch）下，build_msim_transfer_prompt 仍必须同时带出反模式 click() 警告！
    narrow_prompt = build_msim_transfer_prompt(
        sandbox_db,
        problem_family="meituan_dom",
        target_model="gemini",
        query="美团选地址 CDP touch",
        limit=5,
    )
    assert "target=gemini" in narrow_prompt
    assert "Verified Winning Strategies" in narrow_prompt
    assert "Warned Regressions & Anti-Patterns" in narrow_prompt
    assert "改用 CDP trusted touch 选地址成功" in narrow_prompt
    assert "用合成 click() 点美团地址失败" in narrow_prompt
    # 最优策略 (0.95) 必须排在次优策略 (0.72) 之前
    assert narrow_prompt.index("改用 CDP trusted touch 选地址成功") < narrow_prompt.index("先等待地址列表 DOM 渲染再尝试滚动选择")

    # 2) 通过 worker.build_worker_knowledge_context 为 gemini 组装任务上下文
    kb = build_worker_knowledge_context(
        sandbox_db,
        task="修复美团点餐选地址购物车交互",
        target_model="gemini",
        mode="stage",
    )
    assert kb["problem_family"] == "meituan_dom"
    assert kb["msim_injected"] is True
    assert "改用 CDP trusted touch 选地址成功" in kb["block"]
    assert "用合成 click() 点美团地址失败" in kb["block"]


# ===========================================================================
# Scenario Group 4: Multi-Runtime Quota & Circuit-Open Switching
# ===========================================================================
def test_behavior_4_multi_runtime_quota_and_circuit_open_switching(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-23T12:00:00+08:00")

    # 1) claude_sub 配额耗尽 (5% < 10%) 而 codex_sub 余量 85% -> 重型编码必须自动切换到 codex_sub
    heavy_sel = select_runtime(
        sandbox_db,
        task_kind="coding",
        complexity="high",
        preferred="claude_sub",
        quota_snapshot={
            "claude_available": True,
            "claude_remaining": 5.0,
            "codex_available": True,
            "codex_remaining": 85.0,
        },
    )
    assert heavy_sel["runtime"] == "codex_sub"
    assert any(s["runtime"] == "claude_sub" for s in heavy_sel["skipped"])

    # worker.select_worker_runtime 同步联动切换
    mandate = Mandate(
        policy_version=1,
        strength="explicit",
        action_mode="stage",
        cwd=str(sandbox_db.paths.tmp_root),
        task="Refactor worker",
        runner="claude",
    )
    w_sel = select_worker_runtime(
        sandbox_db,
        mandate,
        mode="stage",
        quota_snapshot={
            "claude_available": True,
            "claude_remaining": 5.0,
            "codex_available": True,
            "codex_remaining": 85.0,
        },
    )
    assert w_sel["runtime"] == "codex_sub"
    assert w_sel["preferred_runner"] == "codex"

    # 2) deepseek 在 compute_assets 处于 CIRCUIT_OPEN -> 轻量分拣（即使不显式传 complexity）必须跳过 pi_deepseek 走健康 Pi 回退
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO compute_assets (asset_id, provider, account_label, status, updated_at)
            VALUES ('CA-ds-open', 'deepseek_api', 'deepseek-main', 'circuit_open', ?)
            """,
            (iso_now(),),
        )
    triage_default = select_runtime(sandbox_db, task_kind="triage")
    assert triage_default["kind"] == "pi"
    assert triage_default["runtime"] == "pi_kimi"
    assert any(s["runtime"] == "pi_deepseek" for s in triage_default["skipped"])

    triage_low = select_runtime(sandbox_db, task_kind="triage", complexity="low")
    assert triage_low["runtime"] == "pi_kimi"

    # 3) 双订阅 CLI 均耗尽 -> 优雅降级到健康 Pi Runtime，不崩溃且 worker 不再把耗尽的 claude 传成 preferred_runner
    both_exhausted = select_runtime(
        sandbox_db,
        task_kind="coding",
        complexity="high",
        quota_snapshot={
            "claude_available": True,
            "claude_remaining": 3.0,
            "codex_available": True,
            "codex_remaining": 4.0,
        },
    )
    assert both_exhausted["ok"] is True
    assert both_exhausted["kind"] == "pi"
    assert both_exhausted["runtime"] == "pi_kimi"

    w_exhausted = select_worker_runtime(
        sandbox_db,
        mandate,
        mode="stage",
        quota_snapshot={
            "claude_available": True,
            "claude_remaining": 3.0,
            "codex_available": True,
            "codex_remaining": 4.0,
        },
    )
    assert w_exhausted["runtime"] == "pi_kimi"
    assert w_exhausted["preferred_runner"] == ""

    # 4) 极端情况：所有外部 provider 均熔断 -> 兜底回落 pi_local_ollama 不崩溃
    with sandbox_db.transaction() as conn:
        for idx, prov in enumerate(("kimi", "redpill", "ollama"), start=1):
            conn.execute(
                """
                INSERT INTO compute_assets (asset_id, provider, account_label, status, updated_at)
                VALUES (?, ?, ?, 'CIRCUIT_OPEN', ?)
                """,
                (f"CA-all-{idx}", prov, prov, iso_now()),
            )
    extreme = select_runtime(
        sandbox_db,
        task_kind="triage",
        quota_snapshot={"claude_available": False, "codex_available": False},
    )
    assert extreme["ok"] is True
    assert extreme["runtime"] == "pi_local_ollama"


# ===========================================================================
# Scenario Group 5: Control Tower Management Console Real UX Expectations
# ===========================================================================
def test_behavior_5a_tower_visual_graph_balance_and_zero_dangling_edges(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-23T13:00:00+08:00")
    ts = iso_now()

    # 构造高度倾斜的真实生产分布：1500 sure + 600 chat + 4 wiki + 30 learned + 5 msim + 3 calendar
    with sandbox_db.transaction() as conn:
        nodes_rows: list[tuple[Any, ...]] = []
        for i in range(1500):
            nodes_rows.append((f"cg:sure:TX{i:04d}", "sure", "ledger_txn", f"美团消费 #{i}", "消费记录", f"ledger:{i}", f"2026-11-{(i % 28) + 1:02d}", "[]", ts))
        for i in range(600):
            nodes_rows.append((f"cg:chat:EV{i:04d}", "chat", "event", f"微信消息 #{i}", "聊天记录", f"events:{i}", f"2026-11-{(i % 28) + 1:02d}", "[]", ts))
        for i in range(30):
            nodes_rows.append((f"cg:learned:L{i:02d}", "learned", "hypothesis", f"已学偏好 #{i}", "美团下单规律", f"hyp:{i}", ts, '["learned"]', ts))
        for i in range(5):
            nodes_rows.append((f"cg:msim:M{i:02d}", "msim", "msim_entry", f"M-SIM 经验 #{i}", "CDP touch", f"msim:{i}", ts, '["msim"]', ts))
        for i in range(4):
            nodes_rows.append((f"cg:wiki:W{i:02d}", "wiki", "wiki_page", f"Wiki 页面 #{i}", "偏好维基", f"wiki:{i}", ts, '["wiki"]', ts))
        for i in range(3):
            nodes_rows.append((f"cg:cal:C{i:02d}", "calendar", "trip", f"行程 #{i}", "东京出差", f"trip:{i}", ts, '["trip"]', ts))

        conn.executemany(
            """
            INSERT INTO context_graph_nodes (node_id, app, entity_type, title, body, provenance, occurred_at, tags_json, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            nodes_rows,
        )

        # 写入 2,000 条高权重 sure/chat 边（如果后端用全局 LIMIT 1200 会彻底顶掉 wiki<->learned 边！）
        edge_rows: list[tuple[Any, ...]] = []
        for i in range(1499):
            edge_rows.append((f"cg:sure:TX{i:04d}", f"cg:sure:TX{i + 1:04d}", "shared_merchant:美团", 2.5, ts))
        for i in range(550):
            edge_rows.append((f"cg:chat:EV{i:04d}", f"cg:sure:TX{i:04d}", "shared_date:2026-11", 2.0, ts))
        # 再写入低一些权重的跨知识域边（wiki <-> learned、msim <-> wiki）
        edge_rows.append(("cg:wiki:W00", "cg:learned:L00", "distilled_from", 1.5, ts))
        edge_rows.append(("cg:msim:M00", "cg:wiki:W00", "shared_topic:meituan", 1.4, ts))
        conn.executemany(
            "INSERT OR REPLACE INTO context_graph_edges (src_id, dst_id, relation, weight, created_at) VALUES (?, ?, ?, ?, ?)",
            edge_rows,
        )

    with _running_tower(sandbox_db) as (base_url, hdrs):
        # 测试默认 limit=120 以及较小 limit=40 两种场景
        for lim in (120, 40):
            st, vis = _http_json(base_url, f"/api/graph/visual?limit={lim}", headers=hdrs)
            assert st == 200 and vis["ok"] is True
            nodes = vis["nodes"]
            edges = vis["edges"]
            assert len(nodes) == lim

            by_app: dict[str, int] = {}
            for n in nodes:
                by_app[n["app"]] = by_app.get(n["app"], 0) + 1

            # 每个域都必须获得合理代表性，小域绝不能被 sure/chat 淹没或被字母序饿死！
            assert by_app.get("wiki", 0) == 4, f"wiki starved at limit={lim}: {by_app}"
            assert by_app.get("msim", 0) == 5, f"msim starved at limit={lim}: {by_app}"
            assert by_app.get("calendar", 0) == 3, f"calendar starved at limit={lim}: {by_app}"
            assert by_app.get("learned", 0) >= (25 if lim == 120 else 8), f"learned starved at limit={lim}: {by_app}"
            # sure 和 chat 不能霸占整图超过 50%
            assert by_app.get("sure", 0) <= lim * 0.45
            assert by_app.get("chat", 0) <= lim * 0.45

            # 零悬空边：每条返回边的 src_id 与 dst_id 必须全部在返回 nodes 中！
            node_ids = {n["node_id"] for n in nodes}
            for e in edges:
                assert e["src_id"] in node_ids, f"Dangling src_id: {e}"
                assert e["dst_id"] in node_ids, f"Dangling dst_id: {e}"

            # 跨知识域边（wiki <-> learned、msim <-> wiki）不得被海量 sure 边挤掉！
            edge_pairs = {(e["src_id"], e["dst_id"]) for e in edges} | {(e["dst_id"], e["src_id"]) for e in edges}
            assert ("cg:wiki:W00", "cg:learned:L00") in edge_pairs
            assert ("cg:msim:M00", "cg:wiki:W00") in edge_pairs


def test_behavior_5b_tower_wiki_card_admin_consistency_and_routines_roundtrip(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-23T14:00:00+08:00")
    ts = iso_now()

    with sandbox_db.transaction() as conn:
        # 1) 多行 Wiki 页面（含冲突与字面量测试）
        conn.execute(
            """
            INSERT INTO wiki_pages (slug, title, category, content_md, sources_json, conflicts_json, updated_at, last_distilled_at)
            VALUES (
                'preferences',
                '用户偏好与工程习惯 (Preferences)',
                'preference',
                '# 用户偏好与工程习惯 (Preferences)\n\n- Last distilled: `2026-11-23T14:00:00+08:00`\n- Entries: `1` | Conflicts: `1`\n\n### [F-1] `meituan`\n- 以后美团下单优先直航 poi_id_str\n- 严禁写入 /tmp 目录\n',
                '[]',
                '[{"kind":"contradiction","topic":"meituan","reason":"偏好反转"}]',
                ?, ?
            )
            """,
            (ts, ts),
        )
        # 2) 开启中待办 Case
        conn.execute(
            """
            INSERT INTO cases (case_id, title, next_step, due_at, status, category, rule_id, created_at, updated_at)
            VALUES ('S88', '核对 12.10 林内燃热安装预约', '致电林内客服', '2026-12-09', '待处理', 'deco', 'manual', ?, ?)
            """,
            (ts, ts),
        )
        # 3) 混入误报非订阅（美团外卖）+ 真实连续包月订阅（Apple iCloud+）
        conn.execute(
            """
            INSERT INTO subscription_audit (sub_id, merchant, normalized_name, cadence_days, amount_cents, currency, occurrences, last_charged_at, next_expected_at, prev_amount_cents, price_hike_pct, status, notes, updated_at)
            VALUES
              ('SUB-BAD-MT', '美团外卖', '美团外卖', 7, 3500, 'CNY', 5, '2026-11-20T12:00:00+08:00', '2026-11-27T12:00:00+08:00', 3500, 0.0, 'ACTIVE', '误报餐饮', ?),
              ('SUB-REAL-IC', 'Apple iCloud+', 'iCloud+', 30, 600, 'CNY', 4, '2026-11-01T08:00:00+08:00', '2026-12-01T08:00:00+08:00', 600, 0.0, 'ACTIVE', '连续包月', ?)
            """,
            (ts, ts),
        )
        conn.execute(
            """
            INSERT INTO ledger_txns (txn_id, occurred_at, amount_cents, currency, account_alias, card_last4, kind, status, merchant, notes, created_at, updated_at)
            VALUES
              ('TX-IC1', '2026-10-01T08:00:00+08:00', 600, 'CNY', '招行', '8848', '消费', 'POSTED', 'Apple iCloud+', '连续包月', ?, ?),
              ('TX-IC2', '2026-11-01T08:00:00+08:00', 600, 'CNY', '招行', '8848', '消费', 'POSTED', 'Apple iCloud+', '连续包月', ?, ?)
            """,
            (ts, ts, ts, ts),
        )

    # 1) Wiki 卡片渲染检查：无字面量 \n，且前端 HTML/JS 含冲突角标与正确换行正则
    html = render_tower_html()
    assert r"replace(/\n+/g, ' ')" in html
    assert "冲突" in html

    with _running_tower(sandbox_db) as (base_url, hdrs):
        st, learned = _http_json(base_url, "/api/learned", headers=hdrs)
        assert st == 200
        wp = next(p for p in learned["wiki_pages"] if p["slug"] == "preferences")
        assert "\\n" not in wp["preview"]
        assert "\n" not in wp["preview"]
        assert "以后美团下单优先直航 poi_id_str" in wp["preview"]

        # 2) 切换模块 OFF -> /api/overview 立即减 1，且 evald.run_tick 跳过该模块！
        _, ov_before = _http_json(base_url, "/api/overview", headers=hdrs)
        enabled_before = ov_before["modules"]["enabled"]
        assert ov_before["wave0_core"]["open_cases"] == 1

        st, tog = _http_json(
            base_url,
            "/api/modules/sub_assassin/toggle",
            method="POST",
            headers=hdrs,
            payload={"enabled": False},
        )
        assert st == 200 and tog["ok"] is True and tog["enabled"] is False
        _, ov_after_toggle = _http_json(base_url, "/api/overview", headers=hdrs)
        assert ov_after_toggle["modules"]["enabled"] == enabled_before - 1

        # 同时把 price_watch 也关掉，确认 evald.run_tick 跳过两者
        _http_json(
            base_url,
            "/api/modules/price_watch/toggle",
            method="POST",
            headers=hdrs,
            payload={"enabled": False},
        )
        tick_res = run_tick(sandbox_db, include_collectors=True)
        assert tick_res["ok"] is True
        v5_guards = tick_res["spatial_life"].get("v5_guards") or {}
        assert "sub_assassin" not in v5_guards
        assert tick_res["collectors"]["price_watch"].get("skipped") == "module_disabled"

        # 3) 关闭 Case -> 立即从 /api/admin/items 消失，且 /api/overview open_cases 减 1
        _, admin_before = _http_json(base_url, "/api/admin/items", headers=hdrs)
        assert any(c["case_id"] == "S88" for c in admin_before["cases"])
        st, close_res = _http_json(
            base_url,
            "/api/cases/S88/close",
            method="POST",
            headers=hdrs,
            payload={"cancel": False, "note": "tower_done"},
        )
        assert st == 200 and close_res["ok"] is True
        _, admin_after = _http_json(base_url, "/api/admin/items", headers=hdrs)
        assert not any(c["case_id"] == "S88" for c in admin_after["cases"])
        _, ov_after_close = _http_json(base_url, "/api/overview", headers=hdrs)
        assert ov_after_close["wave0_core"]["open_cases"] == 0

        # 4) 重扫订阅 -> 清除误报『美团外卖』，保留真订阅 iCloud+，且返回真实非零计数（非旧 bug 的 0）
        st, rescan = _http_json(base_url, "/api/subs/rescan", method="POST", headers=hdrs, payload={})
        assert st == 200 and rescan["ok"] is True
        assert rescan["purged"]["purged_subscriptions"] >= 1
        assert "SUB-BAD-MT" in rescan["purged"]["purged_sub_ids"]
        assert rescan["detected_count"] >= 1
        assert rescan["active_count"] >= 1
        names_after = {s["normalized_name"] for s in rescan["subscriptions"]}
        assert "美团外卖" not in names_after
        assert "iCloud+" in names_after

        # 5) Routines UTF-8 中文编辑回环 + .bak 备份必须在 tmp_root 下（严禁 /tmp）
        utf8_text = "# 司命生活规律库\n- 夜猫子逻辑切日：05:00\n- 睡懒觉保护：11:00 前严禁放行科沃斯扫地机 🧹\n"
        st, save1 = _http_json(
            base_url,
            "/api/routines",
            method="POST",
            headers=hdrs,
            payload={"content": utf8_text},
        )
        assert st == 200 and save1["ok"] is True
        bak1 = Path(save1["backup_path"])
        assert bak1.is_file()
        assert bak1.name.endswith(".bak")
        assert str(bak1).startswith(str(sandbox_db.paths.tmp_root))
        assert not str(bak1).startswith("/tmp/")

        # 同秒二次编辑应生成独立备份文件，不覆写第一次备份
        utf8_text_v2 = utf8_text + "- 美团下单偏好：先看评分再下单\n"
        st, save2 = _http_json(
            base_url,
            "/api/routines",
            method="POST",
            headers=hdrs,
            payload={"content": utf8_text_v2},
        )
        assert st == 200 and save2["ok"] is True
        assert save2["backup_path"] != save1["backup_path"]
        assert Path(save2["backup_path"]).read_text(encoding="utf-8") == utf8_text

        st, rt_get = _http_json(base_url, "/api/routines", headers=hdrs)
        assert st == 200 and rt_get["content"] == utf8_text_v2


# ===========================================================================
# Scenario Group 6: Overnight Lab Natural-Wake Workflow
# ===========================================================================
def test_behavior_6_overnight_lab_silent_nan_checkpoint_and_morning_handoff(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-23T01:15:00+08:00")

    # 睡前登记两项 gpu-server 实验交班（危险指令会被白名单剥离）
    h_nan = register_overnight_handoff(
        sandbox_db,
        name="personas-dpo-run4",
        host="gpu-server",
        pane_or_pid="tmux:gpu-server:0.1",
        whitelist=["checkpoint_save", "safe_resume", "rm_rf_data"],
    )
    assert h_nan["whitelist"] == ["checkpoint_save", "safe_resume"]
    assert h_nan["rejected_unsafe_actions"] == ["rm_rf_data"]

    h_rec = register_overnight_handoff(
        sandbox_db,
        name="indie_game-eval-v2",
        host="gpu-server",
        pane_or_pid="tmux:gpu-server:0.2",
    )

    # 凌晨 03:00 巡检：h_nan 出现 loss=nan -> 静默执行白名单 checkpoint_save，零 QQ 吵醒！
    # 同时验证 h_rec 早期掉卡后恢复并正常完工的日志不会重复误判 gpu_drop！
    frozen_clock("2026-11-23T03:00:00+08:00")
    actions_fired: list[tuple[str, str]] = []

    def _reader(h: dict[str, Any]) -> str:
        if h["handoff_id"] == h_nan["handoff_id"]:
            return "step=1400 epoch=2 loss=0.512\nstep=1450 epoch=2 loss=nan val_loss=1.34\n"
        return (
            "step=200 loss=1.42\n"
            "CUDA out of memory\n"
            "Resuming from checkpoint step 200...\n"
            "step=400 loss=0.81\n"
            "step=600 loss=0.55 eval_score=0.91\n"
            "Training complete\n"
        )

    def _actor(h: dict[str, Any], act: str) -> dict[str, Any]:
        actions_fired.append((h["handoff_id"], act))
        return {"ok": True, "action": act}

    # 先单测恢复后日志诊断不会误判旧 gpu_drop
    rec_diag = inspect_handoff_log(_reader(h_rec))
    assert rec_diag["state"] == "completed"
    assert "gpu_drop" not in rec_diag["anomalies"]

    patrol = patrol_overnight_handoffs(
        sandbox_db,
        log_reader_fn=_reader,
        action_fn=_actor,
        now=datetime(2026, 11, 23, 3, 0, tzinfo=SHANGHAI_TZ),
    )
    assert patrol["woke_user"] is False
    assert (h_nan["handoff_id"], "checkpoint_save") in actions_fired
    assert not any(hid == h_rec["handoff_id"] for hid, _ in actions_fired)

    # 确认数据库 effects / outbox 中零夜间 QQ 唤醒消息！
    conn = sandbox_db.connect()
    try:
        qq_effs = int(conn.execute("SELECT COUNT(*) FROM effects WHERE kind = 'send_qq'").fetchone()[0])
        outbox_cnt = int(conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0])
    finally:
        conn.close()
    assert qq_effs == 0
    assert outbox_cnt == 0

    # 早晨 10:30 自然醒 -> 生成清晰对比表 + 下一步 A2 apply 确认卡
    frozen_clock("2026-11-23T10:30:00+08:00")
    report = build_morning_handoff_report(sandbox_db, create_a2_card=True)
    assert report["ok"] is True
    assert report["count"] == 2
    assert "personas-dpo-run4" in report["markdown_table"]
    assert "CHECKPOINTED" in report["markdown_table"]
    assert "indie_game-eval-v2" in report["markdown_table"]
    assert "DONE" in report["markdown_table"]
    assert report["approval_token"].startswith("A")
    assert f"apply {report['approval_token']}" in report["summary"]


# ===========================================================================
# Scenario Group 7: Weekly Bookmark Digest Ranking Realism
# ===========================================================================
def test_behavior_7_weekly_digest_ranking_word_boundary_and_empty_state(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-22T20:00:00+08:00")

    # 1) 空收藏夹表优雅降级，不崩溃
    empty_card = build_weekly_digest_card(sandbox_db, limit=3)
    assert empty_card["ok"] is True
    assert empty_card["count"] == 0
    assert "暂无" in empty_card["markdown"]

    # 2) 灌入 8 条混合收藏：3 条高度关联 siming / deco / boardgame_kb + 5 条通用无关文章（含 pi/cli/rag 子串陷阱！）
    # 其中 deco 条目通过 superagent_triage.save_bookmark_from_drop 写入，验证跨模块 related_projects 联动！
    bm_deco = save_bookmark_from_drop(
        sandbox_db,
        "github.com/demo/haos-rb5009-fresh-air\n新房 RB5009 + HAOS 新风与光猫桥接自动化配置",
        kind="github",
    )
    assert bm_deco["ok"] is True
    assert any("deco" in p for p in bm_deco["related"])

    ts = iso_now()
    with sandbox_db.transaction() as conn:
        extra_rows = [
            (
                "B-SIMING",
                "github",
                "siming-agent-wiki",
                "https://github.com/demo/siming-agent-wiki",
                "demo/siming-agent-wiki",
                "SQLite MCP agent runtime with Hindsight wiki memory",
                json.dumps(["siming/agent底座"], ensure_ascii=False),
                json.dumps(["agent", "wiki", "mcp"]),
            ),
            (
                "B-BOARD",
                "article",
                "BoardGameKB 桌游规则书 RAG 检索优化",
                "https://example.com/boardgame-rag",
                "",
                "针对 BGG 桌游规则书与 deckbuilding 规则的精准检索",
                json.dumps(["boardgame_kb"], ensure_ascii=False),
                json.dumps(["桌游", "rag"]),
            ),
            # 5 条通用无关文章（故意含 pizza->pi / climbing->cli / storage->rag 子串陷阱与单引号标题）
            (
                "B-GEN-1",
                "github",
                "Chef's Homemade Pizza Recipes",
                "https://github.com/food/awesome-pizza-recipes",
                "food/awesome-pizza-recipes",
                "Best homemade pizza dough and rapid photography storage guide",
                "[]",
                '["cooking"]',
            ),
            (
                "B-GEN-2",
                "article",
                "Weekend Climbing & Cycling Trails",
                "https://example.com/climbing-trails",
                "",
                "Outdoor climbing tips and scenic bicycle routes",
                "[]",
                '["outdoor"]',
            ),
            (
                "B-GEN-3",
                "article",
                "Dragon Ball Episode Recap & Opinion",
                "https://example.com/dragon-episode",
                "",
                "Anime episode review and spiritual gardening notes",
                "[]",
                '["anime"]',
            ),
            (
                "B-GEN-4",
                "article",
                "Client Eclipse Color Palette",
                "https://example.com/color-palette",
                "",
                "Minimalist painting and ceramic mug unboxing",
                "[]",
                '["design"]',
            ),
            (
                "B-GEN-5",
                "article",
                "Chef's Secret Pasta Sauce",
                "https://example.com/chefs-pasta",
                "",
                "Simple Italian dinner recipe for busy weeknights",
                "[]",
                '["food"]',
            ),
        ]
        for row in extra_rows:
            conn.execute(
                """
                INSERT INTO bookmarks (
                    bookmark_id, kind, title, url, repo_slug, summary,
                    related_projects_json, tags_json, source_event_id, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?)
                """,
                (*row, ts),
            )

    card = build_weekly_digest_card(sandbox_db, limit=3)
    assert card["ok"] is True
    assert card["count"] == 3
    top_ids = {item["bookmark_id"] for item in card["items"]}
    assert top_ids == {bm_deco["bookmark_id"], "B-SIMING", "B-BOARD"}

    # 所有试跑命令必须使用安全非 /tmp 的隔离目录，且含单引号标题也能生成合法 shell 命令
    for item in card["items"]:
        assert " /tmp/" not in item["trial_cmd"]
        assert str(sandbox_db.paths.tmp_root) in item["trial_cmd"]

    quoted_cmd = build_isolated_trial_cmd(
        {"title": "Chef's Secret Pasta", "url": "https://example.com/chefs-pasta", "kind": "article"},
        tmp_root=sandbox_db.paths.tmp_root,
    )
    assert " /tmp/" not in quoted_cmd
    # 用 shlex.split 校验整条命令无未闭合单引号语法破坏
    assert shlex.split(quoted_cmd)
