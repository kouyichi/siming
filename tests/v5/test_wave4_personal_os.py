"""Siming v5 Wave 4 个人 OS 终态测试套件（仅覆盖 4 项已批准特性：W4-1、W4-3、W4-5、W4-6）。

严守隔离边界：使用 sandbox_db 与 frozen_clock，绝不触碰 /tmp 或真实 /home/devuser 用户文件。
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

from siming.compound_voice import (
    execute_compound_intents,
    parse_compound_utterance,
    stage_compound_approval,
)
from siming.context_graph import (
    format_graph_answer,
    get_graph_stats,
    index_context_graph,
    query_context_graph,
)
from siming.control_tower import build_tower_snapshot, create_tower_server, render_tower_html
from siming.db import SimingDB, iso_now
from siming.social_boardgames import (
    add_commitment,
    check_cabinet_capacity,
    check_upcoming_crowdfund_balances,
    close_commitment,
    extract_commitment_from_text,
    format_social_summary,
    get_friend_profile,
    plan_game_night,
    upsert_crowdfund,
    upsert_friend_profile,
)
from siming.trip_packager import (
    create_trip,
    detect_trips_from_events,
    format_trip_summary,
    pack_trip_cw01_draft,
    recommend_travel_card,
)


def _seed_tokyo_multi_app_data(db: SimingDB) -> Path:
    """在沙盒库与沙盒文档目录写入跨 7 个应用的东京差旅/出境数据。"""
    notes_dir = db.paths.shared_memory_dir / "travel"
    notes_dir.mkdir(parents=True, exist_ok=True)
    note_file = notes_dir / "2025-tokyo-trip.md"
    note_file.write_text(
        "# 2025 东京行程复盘\n\n"
        "- 日期：2025-11-12 ~ 2025-11-16 东京\n"
        "- 酒店：入住新宿格拉斯丽酒店（Hotel Gracery Shinjuku），携程预付。\n"
        "- 报销进度：CW01 已提交并到账核销。\n"
        "#东京 #出境游\n",
        encoding="utf-8",
    )

    with db.transaction() as conn:
        # mail: 携程机票 & 酒店确认邮件
        db.append_event(
            conn,
            source="mail163",
            thread_key="ctrip-tokyo-2025",
            summary="携程机票出票确认：2025-11-12 北京-东京 ANA NH962 金额 ¥3850，酒店：新宿格拉斯丽酒店",
            payload={
                "subject": "携程出票确认：2025-11-12 东京往返机票与新宿格拉斯丽酒店",
                "destination": "东京",
                "start_date": "2025-11-12",
                "end_date": "2025-11-16",
                "merchant": "携程",
                "hotel": "新宿格拉斯丽酒店",
            },
            occurred_at="2025-11-05T10:20:00+08:00",
            fingerprint="fp-tokyo-mail-1",
        )
        # sure: 账本机票消费流水（中行全币种卡 1003）+ Sure sync map
        conn.execute(
            """
            INSERT INTO ledger_txns (
                txn_id, occurred_at, amount_cents, currency, account_alias, card_last4,
                kind, status, merchant, channel_order_id, notes, created_at, updated_at
            ) VALUES (?, ?, ?, 'CNY', '中行全币种', '1003', '消费', 'POSTED', '携程东京机票', 'CT-9981', '东京往返机票 CW01已报销', ?, ?)
            """,
            ("TXN-TK-01", "2025-11-05T10:15:00+08:00", 385000, iso_now(), iso_now()),
        )
        conn.execute(
            """
            INSERT INTO sure_sync_map (local_kind, local_id, sure_id, sure_account, payload_hash, last_synced_at, status)
            VALUES ('txn', 'TXN-TK-01', 'trip:东京:2025-11', '中行1003', 'h1', ?, 'SYNCED')
            """,
            (iso_now(),),
        )
        # chat: 报销完成 case
        conn.execute(
            """
            INSERT INTO cases (
                case_id, title, next_step, due_at, status, category, rule_id,
                context_json, created_at, updated_at, closed_at
            ) VALUES ('C88', '2025东京机票与酒店CW01报销', '已到账核销', '2025-11-25', '已关闭', 'reimburse', 'quiet_cfo',
                      '{"card_last4":"1003","reimbursed":true}', ?, ?, ?)
            """,
            (iso_now(), iso_now(), iso_now()),
        )
        # wiki: 东京差旅攻略维基
        conn.execute(
            """
            INSERT INTO wiki_pages (slug, title, category, content_md, sources_json, conflicts_json, updated_at, last_distilled_at)
            VALUES ('tokyo-travel-playbook', '东京出境差旅与刷卡攻略', 'travel',
                    '2025年11月东京入住新宿格拉斯丽酒店，机票刷中行尾号1003，CW01已报销。', '["mail163"]', '[]', ?, ?)
            """,
            (iso_now(), iso_now()),
        )
        # msim: 进化日志
        conn.execute(
            """
            INSERT INTO msim_entries (
                entry_id, problem_family, target_artifact, source_model, change_rationale,
                expected_outcome, measured_score, baseline_score, passed, eval_context, tags_json, created_at
            ) VALUES ('MS-01', 'context_graph', 'src/siming/context_graph.py', 'claude-opus',
                      '加入1-hop跨应用图边扩散', '提升多跳问答召回', 0.94, 0.78, 1, 'pytest', '["graph","tokyo"]', ?)
            """,
            (iso_now(),),
        )
    return notes_dir


# ==================== W4-1: 跨应用上下文图谱 ====================


def test_w4_1_context_graph_index_and_tokyo_multi_app_query(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-20T14:00:00+08:00")
    notes_dir = _seed_tokyo_multi_app_data(sandbox_db)

    idx_res = index_context_graph(sandbox_db, file_roots=[notes_dir], max_files=50)
    assert idx_res["ok"] is True
    assert idx_res["nodes_indexed"] >= 6
    assert idx_res["edges_created"] >= 3
    for expected_app in ("files", "mail", "chat", "wiki", "sure", "msim"):
        assert idx_res["by_app"].get(expected_app, 0) >= 1

    stats = get_graph_stats(sandbox_db)
    assert stats["total_nodes"] == idx_res["nodes_indexed"]
    assert stats["total_edges"] == idx_res["edges_created"]

    q = "去年东京机票刷哪张卡 报销了没有 订的哪家酒店"
    ans = query_context_graph(sandbox_db, q, limit=8)
    assert ans["ok"] is True
    assert "2025" in ans["year_hints"]
    assert ans["total_hits"] >= 3
    # 必须覆盖跨应用（如 sure / mail / files / chat / wiki 中的至少 3 个）
    assert len(ans["apps_matched"]) >= 3

    combined_reply = format_graph_answer(ans)
    assert "1003" in combined_reply
    assert "新宿格拉斯丽" in combined_reply
    assert "报销" in combined_reply
    # 每条命中都有精确 provenance 与关联图边
    assert all(h["provenance"] for h in ans["hits"])
    assert any(len(h["linked_nodes"]) >= 1 for h in ans["hits"])


# ==================== W4-3: 差旅与出境游行程包 ====================


def test_w4_3_trip_lifecycle_detect_card_and_cw01_pack(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-20T14:00:00+08:00")

    # 1. 创建出境东京行程 & 国内上海行程
    jp_trip = create_trip(sandbox_db, destination="东京", start_date="2026-12-01", end_date="2026-12-05", country="JP")
    assert jp_trip["ok"] is True
    assert jp_trip["kind"] == "international"
    assert jp_trip["sure_tag"] == "trip:东京:2026-12"
    chk_titles = " ".join(c["title"] for c in jp_trip["checklist"])
    assert "护照" in chk_titles and "eSIM" in chk_titles and "Suica" in chk_titles and "T-24h" in chk_titles

    cn_trip = create_trip(sandbox_db, destination="上海", start_date="2026-11-10", end_date="2026-11-12", country="CN")
    assert cn_trip["kind"] == "domestic"
    assert cn_trip["status"] == "RETURNED"

    # 2. 从航旅邮件事件自动识别行程并合并酒店预订
    with sandbox_db.transaction() as conn:
        db_ev1 = sandbox_db.append_event(
            conn,
            source="mail163",
            thread_key="sg-trip",
            summary="携程机票出票：2026-12-20 至 2026-12-24 北京飞新加坡 航班 SQ807",
            payload={"provider": "携程", "order_no": "CT20261220"},
            occurred_at="2026-11-18T09:00:00+08:00",
            fingerprint="fp-sg-flight",
        )
        db_ev2 = sandbox_db.append_event(
            conn,
            source="mail163",
            thread_key="sg-hotel",
            summary="新加坡希尔顿酒店预订确认 2026-12-20 至 2026-12-24",
            payload={"provider": "Hilton", "order_no": "HL8899"},
            occurred_at="2026-11-18T10:00:00+08:00",
            fingerprint="fp-sg-hotel",
        )
        assert db_ev1 and db_ev2

    detected = detect_trips_from_events(sandbox_db)
    assert len(detected) == 2
    assert detected[0]["destination"] == "新加坡"
    assert detected[0]["kind"] == "international"
    assert detected[1]["action"] == "merged"
    assert len(detected[1]["bookings"]) == 2

    # 3. 刷卡返现与外汇建议
    card_jpy = recommend_travel_card(currency="JPY", amount_foreign=20000.0, category="dining")
    assert card_jpy["ok"] is True
    assert card_jpy["fx_fee_pct"] == 0.0
    assert card_jpy["estimated_savings_vs_cny_card"] > 0
    assert "DCC" in card_jpy["dcc_warning"]

    card_cny = recommend_travel_card(currency="CNY", amount_foreign=1200.0, category="flight")
    assert card_cny["ok"] is True
    assert card_cny["fx_rate_to_cny"] == 1.0

    # 4. 返程 CW01 报销草稿打包 + Sure 标签绑定
    with sandbox_db.transaction() as conn:
        for tid, occ, cents, cur, acct, l4, merch in [
            ("T-SH-1", "2026-11-10T08:30:00+08:00", 55300, "CNY", "建行", "1004", "12306高铁北京-上海"),
            ("T-SH-2", "2026-11-10T14:20:00+08:00", 6800, "CNY", "中行", "1003", "滴滴出行"),
            ("T-SH-3", "2026-11-11T12:00:00+08:00", 138000, "CNY", "建行", "1004", "上海亚朵酒店"),
            ("T-OUT", "2026-11-15T12:00:00+08:00", 9900, "CNY", "中行", "1003", "滴滴出行"),
        ]:
            conn.execute(
                """
                INSERT INTO ledger_txns (
                    txn_id, occurred_at, amount_cents, currency, account_alias, card_last4,
                    kind, status, merchant, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, '消费', 'POSTED', ?, ?, ?)
                """,
                (tid, occ, cents, cur, acct, l4, merch, iso_now(), iso_now()),
            )

    pack = pack_trip_cw01_draft(sandbox_db, cn_trip["trip_id"])
    assert pack["ok"] is True
    assert pack["expense_count"] == 3
    assert pack["total_cny_yuan"] == pytest.approx(553.0 + 68.0 + 1380.0, abs=0.01)
    assert pack["sure_tag"] == "trip:上海:2026-11"
    assert not pack["manifest_path"].startswith("/tmp")
    assert Path(pack["manifest_path"]).is_file()
    assert Path(pack["csv_path"]).is_file()

    # 核查 sure_sync_map 已绑定 trip_tag
    conn = sandbox_db.connect()
    try:
        tag_rows = conn.execute(
            "SELECT * FROM sure_sync_map WHERE local_kind = 'trip_tag' AND sure_id = ?",
            ("trip:上海:2026-11",),
        ).fetchall()
        assert len(tag_rows) == 3
    finally:
        conn.close()

    summary_txt = format_trip_summary(sandbox_db, cn_trip["trip_id"])
    assert "上海" in summary_txt and "trip:上海:2026-11" in summary_txt


# ==================== W4-5: 桌游社交与人情记忆 ====================


def test_w4_5_boardgame_cabinet_crowdfund_gamenight_and_encrypted_friends(sandbox_db: SimingDB, frozen_clock) -> None:
    now_dt = frozen_clock("2026-11-20T14:00:00+08:00")

    # 1. 200 盒 / 3.0 m³ 桌游柜容量上限
    ok_cap = check_cabinet_capacity(sandbox_db, current_boxes=160, current_volume_m3=2.2, adding_boxes=2, adding_volume_m3=0.03)
    assert ok_cap["boxes_limit"] == 200
    assert ok_cap["volume_m3_limit"] == pytest.approx(3.0)
    assert ok_cap["over_capacity"] is False
    assert ok_cap["status"] == "OK"

    over_cap = check_cabinet_capacity(sandbox_db, current_boxes=199, current_volume_m3=2.96, adding_boxes=3, adding_volume_m3=0.10)
    assert over_cap["over_capacity"] is True
    assert over_cap["boxes_exceeded"] is True
    assert over_cap["volume_exceeded"] is True
    assert over_cap["status"] == "OVER_CAPACITY"

    # 2. 众筹尾款预警
    upsert_crowdfund(
        sandbox_db,
        title="工业革命：匹兹堡 豪华版",
        platform="新物集",
        pledge_yuan=199.0,
        balance_due_yuan=488.0,
        balance_due_at="2026-11-24",
        status="BALANCE_DUE",
    )
    upsert_crowdfund(
        sandbox_db,
        title="已到货旧桌游",
        platform="KS",
        pledge_yuan=300.0,
        balance_due_yuan=200.0,
        balance_due_at="2026-11-21",
        status="ARRIVED",
    )
    alerts = check_upcoming_crowdfund_balances(sandbox_db, now=now_dt, warn_days=7)
    assert len(alerts) == 1
    assert alerts[0]["title"] == "工业革命：匹兹堡 豪华版"
    assert alerts[0]["days_left"] == 4

    # 3. 加密亲友手账（验证落盘密文绝不含明文）
    secret_note = "花生严重过敏，喝无糖乌龙茶，去年借过《火星改造》扩展包"
    fp_res = upsert_friend_profile(
        sandbox_db,
        name="老张",
        notes=secret_note,
        birthday="08-15",
        dietary_tags=["无花生", "少辣"],
        favorite_games=["沙丘：帝国", "展翅翱翔"],
    )
    assert fp_res["ok"] is True
    assert fp_res["encrypted"] is True

    # 检查数据库原始列绝不含明文
    conn = sandbox_db.connect()
    try:
        raw_blob = conn.execute(
            "SELECT encrypted_notes_blob FROM friend_profiles WHERE name = '老张'"
        ).fetchone()["encrypted_notes_blob"]
    finally:
        conn.close()
    assert raw_blob.startswith("fernet:v1:")
    assert "花生" not in raw_blob
    assert "火星改造" not in raw_blob

    # 解密读取还原
    loaded = get_friend_profile(sandbox_db, "老张")
    assert loaded is not None
    assert loaded["notes"] == secret_note
    assert "无花生" in loaded["dietary_tags"]

    # 4. 桌游局组织（选戏 + QQ 投票卡键盘 + 3 分钟速查规则）
    gn = plan_game_night(sandbox_db, players=4, duration_min=100)
    assert gn["ok"] is True
    assert len(gn["recommendations"]) >= 2
    assert gn["poll_card"]["keyboard"] is not None
    rows = gn["poll_card"]["keyboard"]["content"]["rows"]
    assert len(rows[0]["buttons"]) == len(gn["recommendations"])
    assert "boardgame_kb" in gn["quick_rules_sheet"]

    # 自定义 rules_fn 注入
    gn_custom = plan_game_night(
        sandbox_db,
        players=4,
        duration_min=90,
        rules_fn=lambda name: f"CUSTOM-3MIN-RULES:{name}",
    )
    assert gn_custom["quick_rules_sheet"].startswith("CUSTOM-3MIN-RULES:")

    # 5. 对外口头承诺提取、登记与关闭
    ext = extract_commitment_from_text("回头把《火星改造》借给老张")
    assert ext is not None
    assert ext["person"] == "老张"
    cm = add_commitment(sandbox_db, person=ext["person"], promise=ext["promise"], due_at="2026-11-28", source_quote=ext["source_quote"])
    assert cm["status"] == "OPEN"

    # 摘要不得泄露加密私密备注
    summary = format_social_summary(sandbox_db, now=now_dt)
    assert "工业革命：匹兹堡" in summary
    assert "老张" in summary
    assert "花生严重过敏" not in summary

    closed = close_commitment(sandbox_db, cm["commit_id"])
    assert closed["ok"] is True
    assert closed["status"] == "DONE"


# ==================== W4-6 (Part A): QQ 语音复合指令流 ====================


def test_w4_6_compound_voice_three_intents_approval_and_execution(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-20T14:00:00+08:00")  # 周五

    # 先在 cases 表插入一条待办「牛板筋」
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO cases (case_id, title, status, category, created_at, updated_at)
            VALUES ('S12', '取快递：牛板筋零食包裹', '待处理', 'pickup', ?, ?)
            """,
            (iso_now(), iso_now()),
        )

    utterance = "销掉牛板筋、点份示例粉面送到家、设下周三抢号"
    parsed = parse_compound_utterance(sandbox_db, utterance)
    assert parsed["ok"] is True
    assert parsed["is_compound"] is True
    assert parsed["intent_count"] == 3

    kinds = [it["kind"] for it in parsed["intents"]]
    assert kinds == ["close_case", "meituan_order", "defer_remind"]
    assert parsed["intents"][0]["resolved_target_id"] == "S12"
    assert parsed["intents"][1]["params"]["query"] == "示例粉面"
    assert parsed["intents"][1]["params"]["address_alias"] == "家"
    assert parsed["intents"][2]["params"]["task"] == "抢号"
    assert parsed["intents"][2]["params"]["fire_at"].startswith("2026-11-25")

    # 封装为单张统一 A2 审批卡 + QQ 按钮键盘
    staged = stage_compound_approval(sandbox_db, utterance)
    assert staged["ok"] is True
    token = staged["token"]
    assert token.startswith("A")
    assert staged["approval"]["action"] == "compound_voice_batch"
    btns = staged["keyboard"]["content"]["rows"][0]["buttons"]
    assert btns[0]["render_data"]["label"] == "✅ 全部执行"
    assert btns[0]["action"]["data"] == f"siming:apply:{token}"
    assert btns[1]["render_data"]["label"] == "❌ 取消"
    assert btns[1]["action"]["data"] == f"siming:cancel:{token}"

    # 确定性顺序执行（验证 DB 状态真实更新）
    mt_calls: list[dict] = []

    def _mock_meituan(db: SimingDB, intent: dict) -> dict:
        mt_calls.append(intent["params"])
        return {"ok": True, "kind": "meituan_order", "detail": f"模拟锁单 {intent['params']['query']}"}

    exec_res = execute_compound_intents(
        sandbox_db,
        parsed["intents"],
        executors={"meituan_order": _mock_meituan},
    )
    assert exec_res["ok"] is True
    assert exec_res["succeeded"] == 3
    assert exec_res["failed"] == 0
    assert len(mt_calls) == 1 and mt_calls[0]["query"] == "示例粉面"

    conn = sandbox_db.connect()
    try:
        c12 = conn.execute("SELECT status FROM cases WHERE case_id = 'S12'").fetchone()
        assert c12["status"] == "完成"
        w_row = conn.execute(
            "SELECT * FROM waits WHERE session_ref = 'compound_voice' AND quote LIKE '%抢号%'"
        ).fetchone()
        assert w_row is not None
        assert w_row["next_fire_at"].startswith("2026-11-25")
    finally:
        conn.close()


# ==================== W4-6 (Part B): 司命统一网页控制塔 ====================


def test_w4_6_control_tower_http_auth_endpoints_and_routines_roundtrip(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-20T14:00:00+08:00")
    notes_dir = _seed_tokyo_multi_app_data(sandbox_db)
    index_context_graph(sandbox_db, file_roots=[notes_dir])

    # 补充一条算力资产 & 智能家居状态
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO compute_assets (asset_id, provider, account_label, currency, balance, status, updated_at)
            VALUES ('CA-1', 'deepseek', 'main', 'CNY', 128.5, 'OK', ?)
            """,
            (iso_now(),),
        )
        conn.execute(
            """
            INSERT INTO homelab_nodes (node_id, kind, name, status, last_checked_at, detail)
            VALUES ('HL-1', 'nas', 'Synology-Main', 'OK', ?, 'healthy')
            """,
            (iso_now(),),
        )

    # 验证快照与 HTML 渲染不包含已暂缓的 W4-2/W4-4
    snap = build_tower_snapshot(sandbox_db)
    assert snap["ok"] is True
    assert snap["wave3_knowledge"]["msim_entries"] >= 1
    assert snap["wave4_personal_os"]["context_graph"]["total_nodes"] >= 6
    html = render_tower_html()
    assert "统一网页控制塔" in html
    assert "个税抵扣" not in html
    assert "证件死线" not in html

    token = "test-secret-tower-token-987654"
    server = create_tower_server(sandbox_db, host="127.0.0.1", port=0, token=token)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()

    def _req(path: str, *, method: str = "GET", headers: dict[str, str] | None = None, body: bytes | None = None):
        req = urllib.request.Request(f"{base_url}{path}", data=body, headers=headers or {}, method=method)
        return urllib.request.urlopen(req, timeout=5)

    try:
        # 1. 无鉴权与错误 token -> 401
        with pytest.raises(urllib.error.HTTPError) as exc_unauth:
            _req("/api/overview")
        assert exc_unauth.value.code == 401

        with pytest.raises(urllib.error.HTTPError) as exc_bad:
            _req("/api/overview", headers={"Authorization": "Bearer wrong-token"})
        assert exc_bad.value.code == 401

        bearer_hdr = {"Authorization": f"Bearer {token}"}
        basic_b64 = base64.b64encode(f"siming:{token}".encode("utf-8")).decode("ascii")
        basic_hdr = {"Authorization": f"Basic {basic_b64}"}

        # 2. GET / (HTTP Basic 鉴权)
        with _req("/", headers=basic_hdr) as resp:
            assert resp.status == 200
            page = resp.read().decode("utf-8")
            assert "<!DOCTYPE html>" in page and "routines-editor" in page

        # 3. GET /api/overview, /api/finance, /api/smarthome, /api/msim, /api/modules
        for ep in ("/api/overview", "/api/finance", "/api/smarthome", "/api/msim", "/api/modules"):
            with _req(ep, headers=bearer_hdr) as resp:
                assert resp.status == 200
                data = json.loads(resp.read().decode("utf-8"))
                assert data["ok"] is True
                if ep == "/api/finance":
                    assert len(data["compute_assets"]) == 1
                elif ep == "/api/msim":
                    assert data["count"] == 1
                    assert data["leaderboard"][0]["source_model"] == "claude-opus"

        # 4. GET /api/graph?q=...
        q_enc = urllib.parse.quote("去年东京机票刷哪张卡 订的哪家酒店")
        with _req(f"/api/graph?q={q_enc}", headers=bearer_hdr) as resp:
            assert resp.status == 200
            gdata = json.loads(resp.read().decode("utf-8"))
            assert gdata["ok"] is True
            assert gdata["total_hits"] >= 2
            assert "1003" in gdata["reply"]

        # 5. POST /api/routines -> GET /api/routines 往返与备份校验
        new_routines = "# Siming Routines\n- weekday_morning: 08:15\n- night_owl_cutoff: 05:00\n"
        post_hdr = {**bearer_hdr, "Content-Type": "application/json"}
        with _req(
            "/api/routines",
            method="POST",
            headers=post_hdr,
            body=json.dumps({"content": new_routines}).encode("utf-8"),
        ) as resp:
            assert resp.status == 200
            saved = json.loads(resp.read().decode("utf-8"))
            assert saved["ok"] is True
            assert not saved["backup_path"].startswith("/tmp")
            assert Path(saved["backup_path"]).is_file()

        with _req("/api/routines", headers=bearer_hdr) as resp:
            assert resp.status == 200
            fetched = json.loads(resp.read().decode("utf-8"))
            assert fetched["content"] == new_routines

        # 6. POST /api/wiki/<slug> -> GET /api/wiki 往返
        with _req(
            "/api/wiki/wave4-verified",
            method="POST",
            headers=post_hdr,
            body=json.dumps({"title": "Wave 4 验收", "content_md": "全部 4 项功能就绪"}).encode("utf-8"),
        ) as resp:
            assert resp.status == 200
            wk_saved = json.loads(resp.read().decode("utf-8"))
            assert wk_saved["ok"] is True and wk_saved["slug"] == "wave4-verified"

        with _req("/api/wiki", headers=bearer_hdr) as resp:
            wk_list = json.loads(resp.read().decode("utf-8"))
            assert any(p["slug"] == "wave4-verified" for p in wk_list["pages"])
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=3)


def test_w4_6_systemd_tower_unit_file_valid() -> None:
    unit = Path(__file__).resolve().parents[2] / "systemd" / "siming-tower.service"
    assert unit.is_file()
    content = unit.read_text(encoding="utf-8")
    assert "siming.control_tower" in content
    assert ("127.0.0.1" in content) or ("127.0.0.1" in content)
    assert ("8798" in content) or ("8788" in content)
    assert "TMPDIR=%h/.cache/agent-tmp" in content
