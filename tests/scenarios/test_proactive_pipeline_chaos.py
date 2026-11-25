"""Siming v5 主动红队混沌与隐私安全对抗测试套件（Proactive Pipeline & Chaos Red-Team Suite）。

覆盖 5 大主动挖掘场景 + 真库只读防泄漏巡检：
1. Hindsight Wiki / Context Graph / M-SIM 机密脱敏与防污染（API Key / Bearer / JWT / OTP / 支付密码 / 手机号 / YAML frontmatter）
2. 医疗化验单极端脏 OCR 鲁棒解析（参考范围、箭头前置/后置、日期/手机号干扰、µmol/μmol/IU/L、拆字空格、重复行）
3. 新房放甲醛数学与极端边界混沌（0 读数、1 读数、HCHO=0.0 防 log(0) 崩、升温反弹上升、已达标、极值/极慢防溢出、L3 漏水告警 5 分钟去重）
4. 睡前实验交班日志防误杀与安全白名单混沌（ANSI 颜色码、良性触发词零误杀、真实故障 100% 检出、破坏性命令严禁执行）
5. Bambu 3D / Home Passport / PR Radar / Infra Healer / Homelab 边界混沌（耗材防负数与过期审批卡更新、过期保修剩余 0 天、账单告警去重、150 噪杂+已关闭 PR 压测、非白名单自愈阻断审计）
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from siming.bambu_copilot import consume_filament, upsert_spool
from siming.context_graph import index_context_graph, query_context_graph
from siming.crypto import write_secret
from siming.db import SHANGHAI_TZ, SimingDB, iso_now
from siming.health_cockpit import parse_lab_ocr_text
from siming.hindsight_wiki import distill_hindsight_wiki, extract_facts_from_utterances
from siming.home_air_guard import ingest_air_reading, predict_safe_movein_date
from siming.home_passport import check_due_utilities, lookup_fault_code, upsert_utility
from siming.homelab_ops import evaluate_homelab_health, record_homelab_node
from siming.infra_healer import check_proxy_and_dns_drift, run_infra_patrol
from siming.msim_memory import export_msim_markdown, record_msim_entry
from siming.overnight_lab import (
    inspect_handoff_log,
    patrol_overnight_handoffs,
    register_overnight_handoff,
)
from siming.research_pr_radar import add_pain_point, match_research_items, triage_open_prs


# ---------------------------------------------------------------------------
# Hunt 1: Secret Redaction & Data Quality in Hindsight Wiki / Context Graph / M-SIM
# ---------------------------------------------------------------------------
def test_hunt1_secret_redaction_across_wiki_graph_and_msim(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-22T20:00:00+08:00")

    # 写入一个本地 secret 文件，验证 secrets_dir 内的真实凭据也被拦截
    local_tok = "siming-edge-ultra-secret-99887766"
    write_secret("test-redteam-token", local_tok, paths=sandbox_db.paths)

    secrets_corpus = [
        "sk-proj-AbCdEf1234567890XyZ987654",
        "sk-live9988776655443322",
        "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c",
        "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoicWJ5In0.abc123def456",
        "482910",
        "654321",
        "889900",
        "13812345678",
        "18600001234",
        local_tok,
    ]

    sess_dir = sandbox_db.paths.tmp_root / "chaos_sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)

    # 1) 含各类机密的多 Agent 会话 JSONL
    jsonl_file = sess_dir / "claude_secret_leak.jsonl"
    jsonl_file.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "role": "user",
                        "content": "我们决定采用 OpenAI 网关，密钥是 sk-proj-AbCdEf1234567890XyZ987654，备用 sk-live9988776655443322。",
                        "timestamp": "2026-11-22T18:00:00+08:00",
                    }
                ),
                json.dumps(
                    {
                        "role": "user",
                        "content": (
                            "根因是鉴权失效，已修复：请求头加上 "
                            "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c "
                            "且不要泄露 验证码 482910。"
                        ),
                        "timestamp": "2026-11-22T18:05:00+08:00",
                    }
                ),
                json.dumps(
                    {
                        "role": "user",
                        "content": (
                            f"严禁将支付密码 654321、payment password: 889900、手机号 13812345678 "
                            f"或本地令牌 {local_tok} 以及 eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoicWJ5In0.abc123def456 写入共享文档。"
                        ),
                        "timestamp": "2026-11-22T18:10:00+08:00",
                    }
                ),
            ]
        ),
        encoding="utf-8",
    )

    # 2) 含 YAML frontmatter 与 **How to apply:** 脏模板的 Markdown 笔记
    note_file = sess_dir / "codex_frontmatter_note.md"
    note_file.write_text(
        "---\n"
        "name: secret-note\n"
        "description: 决定采用测试前先泄露手机号 18600001234 的错误摘要\n"
        "tags: [siming]\n"
        "---\n"
        "- **How to apply:** 偏好统一使用 pixi 运行测试，绝不提交 sk-proj-AbCdEf1234567890XyZ987654。\n",
        encoding="utf-8",
    )

    # 3) 同时向 DB events / M-SIM 写入含敏感信息的记录
    with sandbox_db.transaction() as conn:
        sandbox_db.append_event(
            conn,
            source="qq_drop",
            thread_key="secret-event",
            summary="决定采用新接口 Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c 联系 13812345678",
            payload={
                "utterance": "偏好本地调试用 sk-proj-AbCdEf1234567890XyZ987654 短信验证码 482910 支付密码 654321",
                "phone": "18600001234",
            },
            occurred_at="2026-11-22T19:00:00+08:00",
            fingerprint="fp-chaos-secret-ev1",
        )

    msim = record_msim_entry(
        sandbox_db,
        problem_family="security_eval",
        source_model="claude-opus",
        change_rationale="Fix auth with Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c and key sk-proj-AbCdEf1234567890XyZ987654",
        expected_outcome="No leak of 13812345678 or 支付密码 654321",
        eval_context="验证码 482910",
    )
    msim_md = export_msim_markdown(sandbox_db).read_text(encoding="utf-8")
    for raw_secret in ("sk-proj-AbCdEf1234567890XyZ987654", "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c", "13812345678", "654321", "482910"):
        assert raw_secret not in msim["change_rationale"]
        assert raw_secret not in msim["expected_outcome"]
        assert raw_secret not in msim["eval_context"]
        assert raw_secret not in msim_md

    # 4) 蒸馏 Hindsight Wiki（含文件 + DB 源）
    wiki_res = distill_hindsight_wiki(
        sandbox_db,
        session_files=[jsonl_file, note_file],
        include_db_sources=True,
        include_local_sessions=False,
    )
    assert wiki_res["ok"] is True
    assert wiki_res["facts_extracted"] >= 3

    # 检验 DB wiki_pages 与 Obsidian 磁盘 .md 零明文机密泄漏、零 frontmatter / 残缺加粗泄漏
    conn = sandbox_db.connect()
    try:
        wiki_rows = conn.execute("SELECT slug, content_md, sources_json, conflicts_json FROM wiki_pages").fetchall()
    finally:
        conn.close()
    assert len(wiki_rows) == 4
    all_wiki_blob = "\n".join(
        f"{r['content_md']}\n{r['sources_json']}\n{r['conflicts_json']}" for r in wiki_rows
    )
    for md_file in (Path(wiki_res["wiki_dir"]).glob("*.md")):
        all_wiki_blob += "\n" + md_file.read_text(encoding="utf-8")

    for raw_secret in secrets_corpus:
        assert raw_secret not in all_wiki_blob, f"Raw secret leaked into Hindsight Wiki: {raw_secret}"
    assert "description: 决定采用测试前先泄露手机号" not in all_wiki_blob
    assert "How to apply:**" not in all_wiki_blob

    # 5) 索引 Context Graph 并检索 -> 验证 context_graph_nodes & edges 零机密泄漏
    graph_res = index_context_graph(sandbox_db, file_roots=[sess_dir], max_files=20)
    assert graph_res["ok"] is True
    assert graph_res["nodes_indexed"] >= 4

    conn = sandbox_db.connect()
    try:
        node_rows = conn.execute("SELECT node_id, title, body, tags_json FROM context_graph_nodes").fetchall()
        edge_rows = conn.execute("SELECT relation FROM context_graph_edges").fetchall()
    finally:
        conn.close()

    all_graph_blob = "\n".join(f"{r['title']}\n{r['body']}\n{r['tags_json']}" for r in node_rows)
    all_graph_blob += "\n" + "\n".join(str(r["relation"]) for r in edge_rows)
    for raw_secret in secrets_corpus:
        assert raw_secret not in all_graph_blob, f"Raw secret leaked into Context Graph: {raw_secret}"

    q_res = query_context_graph(sandbox_db, "OpenAI 网关 pixi")
    for raw_secret in secrets_corpus:
        assert raw_secret not in q_res["reply"]


def test_hunt1_direct_extract_facts_redacts_secrets_and_legacy_purge(sandbox_db: SimingDB) -> None:
    # 直接调用 extract_facts_from_utterances 也必须脱敏
    facts = extract_facts_from_utterances(
        [
            {
                "text": "偏好使用 OpenAI sk-proj-AbCdEf1234567890XyZ987654 支付密码 654321 验证码 482910 电话 13812345678",
                "source": "direct#1",
                "agent": "codex",
            }
        ],
        paths=sandbox_db.paths,
    )
    assert len(facts) == 1
    blob = facts[0]["statement"] + facts[0]["verbatim"]
    for leak in ("sk-proj-AbCdEf1234567890XyZ987654", "654321", "482910", "13812345678"):
        assert leak not in blob

    # 模拟旧版遗留脏数据在 wiki_pages，再次蒸馏必须将其彻底净化
    sandbox_db.init_db()
    dirty_sources = json.dumps(
        [
            {
                "fact_id": "F-dirty1",
                "category": "preference",
                "topic": "description_旧垃圾",
                "polarity": "positive",
                "statement": "description: 这是一条不该入库的 frontmatter",
                "verbatim": "description: 这是一条不该入库的 frontmatter",
                "source": "legacy.md#L3",
                "agent": "unknown",
                "timestamp": iso_now(),
            },
            {
                "fact_id": "F-dirty2",
                "category": "preference",
                "topic": "how_to",
                "polarity": "positive",
                "statement": "How to apply:** 偏好保存前脱敏 sk-proj-AbCdEf1234567890XyZ987654",
                "verbatim": "How to apply:** 偏好保存前脱敏 sk-proj-AbCdEf1234567890XyZ987654",
                "source": "legacy.md#L9",
                "agent": "unknown",
                "timestamp": iso_now(),
            },
        ],
        ensure_ascii=False,
    )
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO wiki_pages (
                slug, title, category, content_md, sources_json, conflicts_json, updated_at, last_distilled_at
            ) VALUES ('preferences', 'Preferences', 'preference', 'dirty', ?, '[]', ?, ?)
            """,
            (dirty_sources, iso_now(), iso_now()),
        )

    distill_hindsight_wiki(sandbox_db, session_files=[], include_db_sources=False, include_local_sessions=False)
    conn = sandbox_db.connect()
    try:
        row = conn.execute("SELECT content_md, sources_json FROM wiki_pages WHERE slug = 'preferences'").fetchone()
    finally:
        conn.close()
    assert "description: 这是一条不该入库的 frontmatter" not in row["content_md"]
    assert "How to apply:**" not in row["content_md"]
    assert "sk-proj-AbCdEf1234567890XyZ987654" not in row["content_md"]
    assert "sk-proj-AbCdEf1234567890XyZ987654" not in row["sources_json"]


def test_hunt1_live_db_audit_zero_leaked_secrets() -> None:
    """只读审计真实本机 ~/.local/share/siming/siming.db 的 wiki_pages 与 context_graph_nodes（若存在）。"""
    live_db = Path(os.path.expanduser("~/.local/share/siming/siming.db"))
    if not live_db.is_file():
        pytest.skip("live siming.db not present")

    conn = sqlite3.connect(f"file:{live_db}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        leak_pats = (
            ("sk_key", re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")),
            ("bearer", re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{12,}")),
            ("otp", re.compile(r"(验证码|校验码|动态码|OTP).{0,20}(?<!\d)\d{4,8}(?!\d)", re.I)),
            ("pay_pwd", re.compile(r"(支付密码|交易密码|取款密码|银行卡密码).{0,15}\d{4,8}", re.I)),
            ("phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
        )
        for tbl, cols in (
            ("wiki_pages", ("slug", "content_md", "sources_json")),
            ("context_graph_nodes", ("node_id", "title", "body", "tags_json")),
        ):
            rows = conn.execute(f"SELECT {','.join(cols)} FROM {tbl}").fetchall()
            for r in rows:
                blob = "\n".join(str(r[c] or "") for c in cols[1:])
                for pname, pat in leak_pats:
                    assert not pat.search(blob), f"Live DB {tbl}.{r[cols[0]]} leaked {pname}!"
        # 确认 wiki_pages 无 frontmatter / 残缺加粗
        for r in conn.execute("SELECT slug, content_md FROM wiki_pages").fetchall():
            assert "\n- description:" not in r["content_md"]
            assert "How to apply:**" not in r["content_md"]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Hunt 2: Hostile Lab Report OCR Parsing (health_cockpit.py)
# ---------------------------------------------------------------------------
def test_hunt2_hostile_chinese_lab_report_ocr_parsing() -> None:
    hostile_ocr = """
    常用医院 检验报告单
    患者手机: 13812345678  病历号: 202611220098
    采样日期: 2026-11-22 08:15:30  报告日期: 2026-11-22
    检查项目：空腹血糖
    2026-11-22
    空腹血糖 参考范围 3.9-6.1 mmol/L
    空腹血糖 5.20 mmol/L 参考范围 3.9-6.1
    空腹血糖 5.20 mmol/L 参考范围 3.9-6.1
    尿酸 485 ↑ umol/L (208-428)
    尿酸 ↑ 485 μmol/L (208-428)
    谷丙转氨酶(ALT) 28 U/L 参考值 0-40
    谷草 转氨酶 (AST) 31 IU/L (0~40)
    肌酐(Cr) 82 µmol/L 参考区间 57-97
    甘油三酯 ↑ 2.45 mmol/L (0.4-1.7)
    高密度脂蛋白(HDL-C) ↓ 0.88 mmol/L 1.0-2.0
    糖化血红蛋白(HbA1c) 5.6% (4.0-6.0%)
    体重 164 ↑ 斤
    """

    parsed = parse_lab_ocr_text(hostile_ocr)
    # 严禁重复条目
    metric_types = [p["metric_type"] for p in parsed]
    assert len(metric_types) == len(set(metric_types))

    by_type = {p["metric_type"]: p for p in parsed}

    # 空腹血糖必须是 5.20 (NORMAL)，不能误抓 2026 或参考范围 3.9
    assert by_type["glucose"]["value_num"] == 5.2
    assert by_type["glucose"]["unit"] == "mmol/L"
    assert by_type["glucose"]["flag"] == "NORMAL"

    # 尿酸 485 ↑ umol/L (208-428) -> 485.0 HIGH，绝非 208
    assert by_type["uric_acid"]["value_num"] == 485.0
    assert by_type["uric_acid"]["unit"] == "umol/L"
    assert by_type["uric_acid"]["flag"] == "HIGH"

    # 谷丙转氨酶(ALT) 28 U/L -> 28.0 NORMAL
    assert by_type["alt"]["value_num"] == 28.0
    assert by_type["alt"]["unit"] == "U/L"
    assert by_type["alt"]["flag"] == "NORMAL"

    # OCR 拆字空格 + IU/L 归一化
    assert by_type["ast"]["value_num"] == 31.0
    assert by_type["ast"]["unit"] == "U/L"

    # U+00B5 micro sign µmol/L 归一化为 umol/L
    assert by_type["creatinine"]["value_num"] == 82.0
    assert by_type["creatinine"]["unit"] == "umol/L"
    assert by_type["creatinine"]["flag"] == "NORMAL"

    # 前置箭头 ↑ / ↓ 精准提取
    assert by_type["tg"]["value_num"] == 2.45
    assert by_type["tg"]["flag"] == "HIGH"
    assert by_type["hdl"]["value_num"] == 0.88
    assert by_type["hdl"]["flag"] == "LOW"
    assert by_type["hba1c"]["value_num"] == 5.6

    # 体重 164 ↑ 斤 -> 必须识别 斤 并换算为 82.0 kg（绝不是 164.0 kg）
    assert by_type["weight_kg"]["value_num"] == 82.0
    assert by_type["weight_kg"]["unit"] == "kg"


# ---------------------------------------------------------------------------
# Hunt 3: Formaldehyde Math & Edge-Case Chaos (home_air_guard.py)
# ---------------------------------------------------------------------------
def test_hunt3_formaldehyde_math_chaos_and_l3_leak_dedup(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-22T12:00:00+08:00")

    # (a) 0 readings
    p0 = predict_safe_movein_date(sandbox_db, now="2026-11-22T12:00:00+08:00")
    assert p0["ok"] is False
    assert p0["enough_data"] is False
    assert p0["sample_count"] == 0
    assert p0["trend"] == "no_data"

    # (b) 1 reading above threshold -> insufficient data, no crash
    ingest_air_reading(sandbox_db, hcho_mg_m3=0.22, recorded_at="2026-11-22T10:00:00+08:00", notify=False)
    p1 = predict_safe_movein_date(sandbox_db, now="2026-11-22T12:00:00+08:00")
    assert p1["ok"] is True
    assert p1["enough_data"] is False
    assert p1["predicted_safe_date"] is None
    assert p1["trend"] == "insufficient_data"

    # (c) HCHO = 0.0 -> 绝不崩 math.log(0)，且最新为 0.0 时必须判定 already_safe=True!
    ingest_air_reading(sandbox_db, hcho_mg_m3=0.0, recorded_at="2026-11-23T10:00:00+08:00", notify=False)
    p_zero = predict_safe_movein_date(sandbox_db, now="2026-11-23T12:00:00+08:00")
    assert p_zero["ok"] is True
    assert p_zero["already_safe"] is True
    assert p_zero["current_hcho"] == 0.0
    assert p_zero["days_remaining"] == 0.0

    # (d) HCHO RISING after 0.0 (冬天地暖开启 -> 从 0.0 飙升至 0.14 -> 0.25) -> 不得崩 log(0)，且报告上升而非负天数
    ingest_air_reading(sandbox_db, hcho_mg_m3=0.14, recorded_at="2026-11-25T10:00:00+08:00", notify=False)
    ingest_air_reading(sandbox_db, hcho_mg_m3=0.25, recorded_at="2026-11-27T10:00:00+08:00", notify=False)
    p_rise = predict_safe_movein_date(sandbox_db, now="2026-11-27T12:00:00+08:00")
    assert p_rise["ok"] is True
    assert p_rise["already_safe"] is False
    assert p_rise["trend"] == "rising"
    assert p_rise["days_remaining"] is None
    assert p_rise["predicted_safe_date"] is None
    assert p_rise["on_track_for_movein"] is False
    assert "上升" in p_rise["reason"] or "反弹" in p_rise["reason"]

    # (e) Already safe (0.04 mg/m³)
    ingest_air_reading(sandbox_db, hcho_mg_m3=0.04, recorded_at="2026-12-01T10:00:00+08:00", notify=False)
    p_safe = predict_safe_movein_date(sandbox_db, now="2026-12-01T12:00:00+08:00")
    assert p_safe["ok"] is True
    assert p_safe["already_safe"] is True
    assert p_safe["days_remaining"] == 0.0
    assert p_safe["on_track_for_movein"] is True

    # (f) 极端暴涨 (0.95 mg/m³) 与极慢衰减 (0.95 -> 0.949999) -> 严禁 OverflowError
    with sandbox_db.transaction() as conn:
        conn.execute("DELETE FROM home_air_readings")
    ingest_air_reading(sandbox_db, hcho_mg_m3=0.95, recorded_at="2026-12-01T10:00:00+08:00", notify=False)
    ingest_air_reading(sandbox_db, hcho_mg_m3=0.949998, recorded_at="2026-12-02T10:00:00+08:00", notify=False)
    p_spike = predict_safe_movein_date(sandbox_db, now="2026-12-02T12:00:00+08:00")
    assert p_spike["ok"] is True
    assert p_spike["already_safe"] is False
    assert p_spike["on_track_for_movein"] is False
    assert p_spike["predicted_safe_date"] is None

    # (g) 重复 L3 漏水告警在 5 分钟内必须去重（即使时间戳和温度略有变化！）
    frozen_clock("2026-12-03T03:00:00+08:00")
    r1 = ingest_air_reading(
        sandbox_db, room="厨房", temp_c=19.5, humidity_pct=52.0, hcho_mg_m3=0.06,
        water_leak=True, recorded_at="2026-12-03T03:00:00+08:00", notify=True,
    )
    assert r1["l3_triggered"] is True
    assert r1["push_result"]["ok"] is True
    assert r1["push_result"].get("skipped") != "duplicate"

    frozen_clock("2026-12-03T03:01:30+08:00")
    r2 = ingest_air_reading(
        sandbox_db, room="厨房", temp_c=19.6, humidity_pct=53.0, hcho_mg_m3=0.06,
        water_leak=True, recorded_at="2026-12-03T03:01:30+08:00", notify=True,
    )
    assert r2["l3_triggered"] is True
    assert r2["push_result"]["skipped"] == "duplicate"

    conn = sandbox_db.connect()
    try:
        ob_cnt = conn.execute("SELECT COUNT(*) FROM outbox WHERE rule_id = 'home_air_emergency'").fetchone()[0]
    finally:
        conn.close()
    assert ob_cnt == 1

    # 超过 5 分钟仍漏水 -> 允许再次推送
    frozen_clock("2026-12-03T03:06:00+08:00")
    r3 = ingest_air_reading(
        sandbox_db, room="厨房", temp_c=19.7, humidity_pct=54.0, hcho_mg_m3=0.06,
        water_leak=True, recorded_at="2026-12-03T03:06:00+08:00", notify=True,
    )
    assert r3["push_result"].get("skipped") != "duplicate"


# ---------------------------------------------------------------------------
# Hunt 4: Overnight Lab Log False-Positive & Safety Whitelist Chaos
# ---------------------------------------------------------------------------
def test_hunt4_overnight_lab_ansi_false_positives_and_whitelist(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-22T02:00:00+08:00")

    # 1) 良性日志（含 ANSI 颜色码 + 诱饵子串）-> 零误杀，保持 running
    benign_log = "\n".join(
        [
            "\x1b[32m[INFO] CUDA available: True (RTX 5090 x4)\x1b[0m",
            "\x1b[36m[PERF] nanosecond latency: 12ns | throughput=4200 tok/s\x1b[0m",
            "[CFG] loss_nan_check=enabled | abort_on_loss=nan",
            "[AUDIT] no permission error | not waiting for permission",
            "[HEALTH] no NaN detected | CUDA error count: 0",
            "step=350 epoch=1.5 loss=0.412 val_loss=0.455",
        ]
    )
    benign_diag = inspect_handoff_log(benign_log)
    assert benign_diag["state"] == "running"
    assert benign_diag["anomalies"] == []
    assert benign_diag["recommended_action"] == "none"
    assert benign_diag["metrics"]["step"] == 350
    assert benign_diag["metrics"]["loss"] == 0.412

    # 2) 带 ANSI 颜色码的真实故障 -> 100% 检出，且 tail_line 不含 ANSI 乱码与密钥
    oom_log = "\x1b[31mRuntimeError:\x1b[0m \x1b[1mCUDA out of memory\x1b[0m (token=sk-proj-AbCdEf1234567890XyZ987654)"
    oom_diag = inspect_handoff_log(oom_log)
    assert oom_diag["state"] == "gpu_drop"
    assert oom_diag["recommended_action"] == "safe_resume"
    assert "\x1b[" not in oom_diag["tail_line"]
    assert "sk-proj-AbCdEf1234567890XyZ987654" not in oom_diag["tail_line"]

    nan_log = "\x1b[33mstep=400\x1b[0m \x1b[31mloss\x1b[0m=\x1b[31mnan\x1b[0m"
    nan_diag = inspect_handoff_log(nan_log)
    assert nan_diag["state"] == "loss_nan"
    assert nan_diag["metrics"]["step"] == 400
    assert nan_diag["recommended_action"] == "checkpoint_save"

    perm_log = "\x1b[33mAllow tool call?\x1b[0m \x1b[1m[y/n]\x1b[0m"
    perm_diag = inspect_handoff_log(perm_log)
    assert perm_diag["state"] == "permission_stall"
    assert perm_diag["recommended_action"] == "auto_approve_readonly"

    # 3) 安全白名单强约束：拒绝 rm -rf / shutdown，且即使 DB 被篡改也绝不执行
    h = register_overnight_handoff(
        sandbox_db,
        name="redteam-train",
        host="gpu-server",
        whitelist=["rm -rf /", "shutdown", "checkpoint_save"],
    )
    assert h["whitelist"] == ["checkpoint_save"]
    assert set(h["rejected_unsafe_actions"]) == {"rm -rf /", "shutdown"}

    # 篡改 DB 中的 whitelist_json 注入破坏性命令，验证 patrol 严格二次拦截
    with sandbox_db.transaction() as conn:
        conn.execute(
            "UPDATE overnight_handoffs SET whitelist_json = ? WHERE handoff_id = ?",
            (json.dumps(["rm -rf /", "shutdown"]), h["handoff_id"]),
        )
    called: list[str] = []
    patrol = patrol_overnight_handoffs(
        sandbox_db,
        log_reader_fn=lambda _: nan_log,
        action_fn=lambda _h, act: (called.append(act) or {"ok": True}),
    )
    assert patrol["woke_user"] is False
    assert called == []


# ---------------------------------------------------------------------------
# Hunt 5: Bambu / Home Passport / PR Radar / Infra Healer / Homelab Boundary Chaos
# ---------------------------------------------------------------------------
def test_hunt5_bambu_floor_zero_and_approval_dedup_and_expiry(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-22T15:00:00+08:00")
    upsert_spool(
        sandbox_db,
        spool_id="spool-chaos-01",
        material="PETG",
        color="黑",
        initial_grams=1000.0,
        remaining_grams=120.0,
        low_threshold_grams=180.0,
    )

    # 消耗超过余量 -> 归零而非负数
    c1 = consume_filament(sandbox_db, "spool-chaos-01", grams=500.0, stage_restock=True)
    assert c1["ok"] is True
    assert c1["remaining_grams"] == 0.0
    assert c1["low_stock"] is True
    tok1 = c1["restock_approval"]["token"]
    assert tok1.startswith("A")

    # 反复触发低库存扣减 -> 复用现有有效 PENDING 审批卡，绝不泛滥重复卡
    c2 = consume_filament(sandbox_db, "spool-chaos-01", grams=20.0, stage_restock=True)
    assert c2["remaining_grams"] == 0.0
    assert c2["restock_approval"]["token"] == tok1

    conn = sandbox_db.connect()
    try:
        cnt = conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE action = 'jd_replenish_spool' AND target_id = 'spool-chaos-01' AND status = 'PENDING'"
        ).fetchone()[0]
    finally:
        conn.close()
    assert cnt == 1

    # 若原 PENDING 审批卡已过期（时间快进 48h）-> 不应复用已过期的死口令，而是签发新口令
    frozen_clock("2026-11-24T16:00:00+08:00")
    c3 = consume_filament(sandbox_db, "spool-chaos-01", grams=5.0, stage_restock=True)
    assert c3["restock_approval"]["token"] != tok1


def test_hunt5_home_passport_fault_warranty_and_utility_dedup(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-22T10:00:00+08:00")

    # 1) 小写带空格故障码 " e1 " -> 正确命中
    f_e1 = lookup_fault_code(
        sandbox_db,
        brand_or_appliance="格力空调",
        fault_code=" e1 ",
        purchase_date="2020-01-01",
        warranty_years=3,
        now="2026-11-22T10:00:00+08:00",
    )
    assert f_e1["ok"] is True
    assert f_e1["found"] is True
    assert f_e1["fault_code"] == "E1"
    # 过保必须返回剩余 0 天（绝非负数），且 in_warranty=False
    assert f_e1["in_warranty"] is False
    assert f_e1["warranty_days_remaining"] == 0

    # 2) 未知品牌优雅兜底
    f_unk = lookup_fault_code(sandbox_db, "神秘杂牌烤箱", " x99 ")
    assert f_unk["ok"] is True
    assert f_unk["found"] is False
    assert f_unk["fault_code"] == "X99"

    # 3) 重复巡检到期账单绝不重复推送告警
    upsert_utility(sandbox_db, kind="electric", account_no="ACC-01", next_due_at="2026-11-24", amount_yuan=188.0)
    u1 = check_due_utilities(sandbox_db, now="2026-11-22T10:00:00+08:00", warn_days=5, notify=True)
    assert u1["alerts_count"] == 1
    assert u1["push_result"]["ok"] is True
    assert u1["push_result"].get("skipped") != "duplicate"

    # 即使同日稍晚金额/时间略有变动，也不得再次刷屏
    upsert_utility(sandbox_db, kind="electric", account_no="ACC-01", next_due_at="2026-11-24", amount_yuan=192.0)
    u2 = check_due_utilities(sandbox_db, now="2026-11-22T14:00:00+08:00", warn_days=5, notify=True)
    assert u2["push_result"]["skipped"] == "duplicate"

    conn = sandbox_db.connect()
    try:
        ob_cnt = conn.execute("SELECT COUNT(*) FROM outbox WHERE rule_id = 'home_utility_due'").fetchone()[0]
    finally:
        conn.close()
    assert ob_cnt == 1


def test_hunt5_pr_radar_150_noisy_closed_null_prs_and_template_chaos(sandbox_db: SimingDB) -> None:
    noisy_prs: list[Any] = []
    # 120 条 action_required / 缺字段 / None 值 PR
    for i in range(120):
        noisy_prs.append(
            {
                "repo": None if i % 10 == 0 else "org/noisy",
                "number": None if i % 15 == 0 else i + 1,
                "title": None if i % 7 == 0 else f"chore: bump #{i}",
                "body": None,
                "ci_status": "action_required" if i % 2 == 0 else None,
                "comments": None if i % 3 == 0 else [{"body": None, "author": None}],
            }
        )
    # 27 条已关闭或已合并的 PR（即便带 approved / mention / conflict 也绝不能算高信号待办！）
    for i in range(27):
        noisy_prs.append(
            {
                "repo": "org/closed-repo",
                "number": 500 + i,
                "title": f"old closed pr #{i}",
                "state": "closed" if i % 2 == 0 else "MERGED",
                "merged": i % 3 == 0,
                "approved": True,
                "maintainer_mentioned": True,
                "has_conflict": True,
            }
        )
    # 3 条真实高信号 Open PR
    noisy_prs.extend(
        [
            {"repo": "org/core", "number": 1001, "title": "feat: fast path", "state": "open", "review_decision": "APPROVED", "ci_status": "success"},
            {"repo": "org/core", "number": 1002, "title": "fix: edge case", "state": "open", "latest_comment": "Hey @devuser please rebase", "latest_comment_role": "MAINTAINER"},
            {"repo": "org/core", "number": 1003, "title": "refactor: queue", "state": "open", "mergeable": "CONFLICTING"},
        ]
    )
    assert len(noisy_prs) == 150

    triage = triage_open_prs(noisy_prs, my_login="devuser", max_cards=3)
    assert triage["total"] == 150
    assert triage["actionable_count"] == 3
    assert triage["silenced_noise_count"] == 147
    assert len(triage["cards"]) == 3
    # 验证 27 条 closed/merged PR 全部被归入 closed_or_merged
    closed_silenced = [x for x in triage["silenced_noise"] if x["silence_reason"] == "closed_or_merged"]
    assert len(closed_silenced) == 27

    # 验证异常模板/字符串 tags/None 条目不崩溃
    add_pain_point(
        sandbox_db,
        repo="siming",
        topic="sqlite_wal",
        keywords=["sqlite", "wal"],
        verify_cmd_template="echo '{unknown_placeholder}' && pytest",
    )
    matched = match_research_items(
        sandbox_db,
        [None, {"title": "SQLite WAL tuning", "summary": "fast wal", "tags": "database"}],  # type: ignore[list-item]
    )
    assert len(matched) == 1
    assert "git worktree add" in matched[0]["verify_cmd"]


def test_hunt5_infra_healer_strict_whitelist_and_homelab_malformed_telemetry(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-22T16:00:00+08:00")

    # 1) 字符串形式的 extra_actions 不得拆成单字符，且非白名单动作严格拦截并写审计
    drift_str = check_proxy_and_dns_drift(probe_fn=lambda: {"extra_actions": "reboot_host"})
    assert drift_str["recommended_heals"] == ["reboot_host"]

    heals_called: list[str] = []
    report = run_infra_patrol(
        sandbox_db,
        auto_heal=True,
        probe_fns={
            "omarchy": lambda: {"pacman_lock_present": False, "disk_free_gb": 80.0},
            "tls": lambda h, p: {"ok": True, "days_remaining": 45.0},
            "resources": lambda: {"mem_used_pct": 40.0, "disk81_used_pct": 45.0},
            "proxy_dns": lambda: {
                "direct_dns_ok": True,
                "proxy_dns_ok": False,
                "dns_hijacked": False,
                "singbox_route_drift": False,
                "extra_actions": ["rm -rf /", "shutdown -h now", "disable_firewall"],
            },
        },
        heal_fn=lambda action, _ctx: (heals_called.append(action) or {"ok": False, "error": "simulated_fail"}),
    )
    assert heals_called == ["flush_dns_cache"]
    assert set(report["blocked_non_whitelisted"]) == {"rm -rf /", "shutdown -h now", "disable_firewall"}
    # 白名单自愈失败时，proxy_dns 问题必须仍保留在 alerts 中
    assert any("dns_negative_cache" in a for a in report["alerts"])

    conn = sandbox_db.connect()
    try:
        blocked_audits = [
            r["target_id"]
            for r in conn.execute(
                "SELECT target_id FROM audit WHERE action = 'infra_heal_blocked_non_whitelist'"
            ).fetchall()
        ]
    finally:
        conn.close()
    assert set(blocked_audits) == {"rm -rf /", "shutdown -h now", "disable_firewall"}

    # 2) Homelab 畸形遥测数据不得触发崩溃
    record_homelab_node(
        sandbox_db,
        node_id="nas-chaos",
        kind="nas",
        name="NAS Chaos",
        status="OK",
        metrics={
            "smart_bad_sectors": "not-a-number",
            "disk_temp_c": "",
            "disks": [{"slot": "bay1", "bad_sectors": "bad", "temp_c": "hot"}],
        },
    )
    h_eval = evaluate_homelab_health(sandbox_db, notify=False)
    assert h_eval["ok"] is True
