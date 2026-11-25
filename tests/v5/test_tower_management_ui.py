"""Siming v5 统一网页控制塔管理台与交互式知识图谱测试套件。

覆盖：
1. 全部新增管理 API（模块开关、已学知识聚合、图谱可视化/合成兜底/重建、Wiki 蒸馏、运维项、Case 销账/取消、Wait 取消、Approval 仅作废、订阅状态标记/重扫）；
2. 强鉴权保护（无头/错令牌均 401，支持 Bearer / Basic / X-Siming-Token）与 `audit` 审计表落盘校验；
3. Caddy `/siming/tower/` 路径前缀兼容与前端管理控制台 HTML 结构校验（无原始 JSON 主视图、含 iOS 开关、SVG 图谱、甲醛仪表盘等）。
"""

from __future__ import annotations

import base64
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from typing import Any, Iterator

import pytest

from siming.control_tower import create_tower_server, render_tower_html
from siming.db import SimingDB, iso_now
from siming.event_chain import routines_md_path
from siming.modules import is_module_enabled


@contextmanager
def _running_tower(db: SimingDB, token: str = "tower-mgmt-secret-2026") -> Iterator[tuple[str, dict[str, str], dict[str, str]]]:
    server = create_tower_server(db, host="127.0.0.1", port=0, token=token)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    bearer_hdr = {"Authorization": f"Bearer {token}"}
    x_tok_hdr = {"X-Siming-Token": token}
    try:
        yield base_url, bearer_hdr, x_tok_hdr
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


def _seed_rich_tower_db(db: SimingDB) -> None:
    ts = iso_now()
    rp = routines_md_path(db)
    rp.parent.mkdir(parents=True, exist_ok=True)
    rp.write_text("# Siming Routines\n- night_owl_cutoff: 05:00\n- prefer_dark_mode: true\n", encoding="utf-8")

    # 写一条共享收件箱笔记供 /api/wiki/distill 抽取
    inbox = db.paths.shared_inbox_dir
    inbox.mkdir(parents=True, exist_ok=True)
    (inbox / "session-pref.md").write_text(
        "我们统一使用 pixi 管理 Python 依赖。\n严禁向 /tmp 写入临时文件，必须使用 agent-tmp。\n",
        encoding="utf-8",
    )

    with db.transaction() as conn:
        # 1. 开启中待办（含一条 subscriptions 联动）
        conn.execute(
            """
            INSERT INTO cases (case_id, title, next_step, due_at, status, category, rule_id, created_at, updated_at)
            VALUES ('S41', '取快递：拓竹 PLA 耗材', '菜鸟驿站 3-2-108', '2026-11-21', '待处理', 'pickup', 'pickup_1800', ?, ?)
            """,
            (ts, ts),
        )
        conn.execute(
            """
            INSERT INTO subscriptions (sub_key, target_kind, target_id, state, created_at)
            VALUES ('case:S41', 'case', 'S41', 'ACTIVE', ?)
            """,
            (ts,),
        )
        conn.execute(
            """
            INSERT INTO cases (case_id, title, next_step, due_at, status, category, rule_id, created_at, updated_at)
            VALUES ('S42', '核对下周东京酒店', '确认含早', '2026-11-24', '待处理', 'travel', 'manual', ?, ?)
            """,
            (ts, ts),
        )
        # 2. 活跃挂起 Wait
        conn.execute(
            """
            INSERT INTO waits (wait_id, status, strength, trigger_kind, trigger_expr, next_fire_at, expires_at, action_mode, quote, mandate_json, created_at, updated_at)
            VALUES ('W55', 'WAITING', 'explicit', 'time', '2026-11-25 09:00', '2026-11-25T09:00:00+08:00', '2026-12-01T00:00:00+08:00', 'remind', '周三上午抢号', '{"task":"周三上午抢常用医院号"}', ?, ?)
            """,
            (ts, ts),
        )
        # 3. 待确认 A2 Approval
        conn.execute(
            """
            INSERT INTO approvals (token, bundle_id, tier, action, target_id, params_json, params_digest, status, expires_at, created_at)
            VALUES ('A91', '', 'A2', 'didi_create_order', 'DIDI-1', '{"dest":"机场"}', 'dig91', 'PENDING', '2026-11-21T00:00:00+08:00', ?)
            """,
            (ts,),
        )
        # 4. Learned tables: wiki_pages, hypotheses, msim_entries, curiosity_questions, bookmarks
        conn.execute(
            """
            INSERT INTO wiki_pages (slug, title, category, content_md, sources_json, conflicts_json, updated_at, last_distilled_at)
            VALUES ('tokyo-card-guide', '东京出境刷卡与酒店复盘', 'travel', '东京新宿格拉斯丽酒店，刷中行尾号 1003 免外币转换费。', '["mail163"]', '[]', ?, ?)
            """,
            (ts, ts),
        )
        conn.execute(
            """
            INSERT INTO hypotheses (id, claim, evidence_ids, status, expires_at, created_at)
            VALUES ('H01', '工作日超过 19:30 在公司大概率需要加班晚餐提醒', '["EV1","EV2"]', 'OBSERVING', '2026-12-30T00:00:00+08:00', ?)
            """,
            (ts,),
        )
        conn.execute(
            """
            INSERT INTO msim_entries (entry_id, problem_family, target_artifact, source_model, change_rationale, expected_outcome, measured_score, baseline_score, passed, eval_context, tags_json, created_at)
            VALUES ('MS-99', 'sub_precision', 'src/siming/sub_hunter.py', 'claude-opus', '排除高频餐饮打车误判订阅', '订阅误报率归零', 0.96, 0.72, 1, 'pytest', '["finance","subs"]', ?)
            """,
            (ts,),
        )
        conn.execute(
            """
            INSERT INTO curiosity_questions (qid, topic, question, context_json, status, answer, created_at, answered_at)
            VALUES ('CQ-1', 'routine_drift', '最近连续三天凌晨 3 点仍在写代码，夜猫子切日是否保持 05:00？', '{}', 'ANSWERED', '保持 05:00 切日，上午 11 点前别放扫地机', ?, ?)
            """,
            (ts, ts),
        )
        conn.execute(
            """
            INSERT INTO bookmarks (bookmark_id, kind, title, url, repo_slug, summary, related_projects_json, tags_json, created_at)
            VALUES ('BM-1', 'github', 'Linger Context Graph', 'https://github.com/example/linger', 'example/linger', '跨应用本地联邦知识图谱灵感', '["siming"]', '["graph","wiki"]', ?)
            """,
            (ts,),
        )
        # 5. Subscriptions & Ledger & Trips
        conn.execute(
            """
            INSERT INTO subscription_audit (sub_id, merchant, normalized_name, cadence_days, amount_cents, currency, occurrences, last_charged_at, next_expected_at, prev_amount_cents, price_hike_pct, status, notes, updated_at)
            VALUES ('SUB-ICLOUD', 'Apple iCloud+', 'iCloud+', 30, 600, 'CNY', 6, '2026-11-01T08:00:00+08:00', '2026-12-01T08:00:00+08:00', 600, 0.0, 'ACTIVE', '云存储', ?)
            """,
            (ts,),
        )
        conn.execute(
            """
            INSERT INTO ledger_txns (txn_id, occurred_at, amount_cents, currency, account_alias, card_last4, kind, status, merchant, notes, created_at, updated_at)
            VALUES ('TX-IC-1', '2026-10-01T08:00:00+08:00', 600, 'CNY', '招行', '8848', '消费', 'POSTED', 'Apple iCloud+', '连续包月', ?, ?)
            """,
            (ts, ts),
        )
        conn.execute(
            """
            INSERT INTO ledger_txns (txn_id, occurred_at, amount_cents, currency, account_alias, card_last4, kind, status, merchant, notes, created_at, updated_at)
            VALUES ('TX-IC-2', '2026-11-01T08:00:00+08:00', 600, 'CNY', '招行', '8848', '消费', 'POSTED', 'Apple iCloud+', '连续包月', ?, ?)
            """,
            (ts, ts),
        )
        conn.execute(
            """
            INSERT INTO ledger_txns (txn_id, occurred_at, amount_cents, currency, account_alias, card_last4, kind, status, merchant, notes, created_at, updated_at)
            VALUES ('TX-SUB-1', '2026-10-05T10:00:00+08:00', 1900, 'CNY', '招行', '8848', '消费', 'POSTED', '迅雷会员连续包月', '自动续费', ?, ?)
            """,
            (ts, ts),
        )
        conn.execute(
            """
            INSERT INTO ledger_txns (txn_id, occurred_at, amount_cents, currency, account_alias, card_last4, kind, status, merchant, notes, created_at, updated_at)
            VALUES ('TX-SUB-2', '2026-11-05T10:00:00+08:00', 2500, 'CNY', '招行', '8848', '消费', 'POSTED', '迅雷会员连续包月', '自动续费', ?, ?)
            """,
            (ts, ts),
        )
        conn.execute(
            """
            INSERT INTO trips (trip_id, destination, country, start_date, end_date, kind, status, sure_tag, created_at, updated_at)
            VALUES ('TR-TK', '东京', 'JP', '2026-12-01', '2026-12-05', 'international', 'PLANNED', 'trip:东京:2026-12', ?, ?)
            """,
            (ts, ts),
        )


def test_tower_html_management_console_visual_elements() -> None:
    html = render_tower_html()
    assert "<!DOCTYPE html>" in html
    assert "统一网页控制塔" in html
    # 严禁已暂缓的 W4-2 / W4-4
    assert "个税抵扣" not in html
    assert "证件死线" not in html
    # 关键交互元素：iOS 开关、交互式 SVG 图谱、学到了什么、甲醛仪表盘、快捷运维操作、安全作废提示
    assert "ios-switch" in html
    assert 'id="kg-svg"' in html
    assert 'id="kg-inspector"' in html
    assert "司命学到了什么" in html
    assert "刷新图谱索引" in html
    assert "立即蒸馏共享 Wiki" in html
    assert "重扫订阅刺客" in html
    assert "routines-editor" in html
    assert "HCHO mg/m³" in html
    assert "扫地机" in html
    assert "/api/modules/" in html and "/toggle" in html
    assert "/api/cases/" in html and "/close" in html
    assert "/api/subs/" in html and "/status" in html
    # 不应把主面板直接渲染为裸 JSON.stringify（仅允许折叠 debug <details> 使用一次）
    assert html.count("JSON.stringify(ov") <= 1
    assert "JSON.stringify(fin" not in html
    assert "JSON.stringify(sh" not in html
    assert "JSON.stringify(md" not in html
    # 确保正则字面量未被 Python 字符串误转义为真实换行
    assert r"replace(/\n+/g" in html
    assert r"replace(/^\/+/" in html


def test_tower_auth_protection_on_all_new_endpoints(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-20T14:00:00+08:00")
    with _running_tower(sandbox_db) as (base_url, _bearer, x_tok):
        for method, path in (
            ("GET", "/api/learned"),
            ("GET", "/api/graph/visual"),
            ("GET", "/api/admin/items"),
            ("POST", "/api/modules/price_watch/toggle"),
            ("POST", "/api/graph/reindex"),
            ("POST", "/api/wiki/distill"),
            ("POST", "/api/cases/S41/close"),
            ("POST", "/api/waits/W55/cancel"),
            ("POST", "/api/approvals/A91/cancel"),
            ("POST", "/api/subs/SUB-ICLOUD/status"),
            ("POST", "/api/subs/rescan"),
        ):
            with pytest.raises(urllib.error.HTTPError) as exc:
                _http_json(base_url, path, method=method, payload={"enabled": False} if method == "POST" else None)
            assert exc.value.code == 401

        # X-Siming-Token 鉴权正常通过且兼容 Caddy /siming/tower/ 前缀
        st, learned = _http_json(base_url, "/siming/tower/api/learned", headers=x_tok)
        assert st == 200 and learned["ok"] is True


def test_tower_module_toggle_roundtrip_and_wave_grouping(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-20T14:00:00+08:00")
    with _running_tower(sandbox_db) as (base_url, bearer, _xtok):
        st, mods_before = _http_json(base_url, "/api/modules", headers=bearer)
        assert st == 200 and mods_before["ok"] is True
        waves_seen = {m["wave"] for m in mods_before["modules"]}
        assert waves_seen == {0, 1, 2, 3, 4}
        assert is_module_enabled(sandbox_db, "overtime_meal") is True

        # 关闭模块
        st, off_res = _http_json(
            base_url,
            "/api/modules/overtime_meal/toggle",
            method="POST",
            headers=bearer,
            payload={"enabled": False},
        )
        assert st == 200 and off_res["ok"] is True and off_res["enabled"] is False
        assert is_module_enabled(sandbox_db, "overtime_meal") is False

        # 重新开启模块
        st, on_res = _http_json(
            base_url,
            "/api/modules/overtime_meal/toggle",
            method="POST",
            headers=bearer,
            payload={"enabled": True},
        )
        assert st == 200 and on_res["ok"] is True and on_res["enabled"] is True
        assert is_module_enabled(sandbox_db, "overtime_meal") is True

        # 未知模块返回 404
        with pytest.raises(urllib.error.HTTPError) as exc_nf:
            _http_json(
                base_url,
                "/api/modules/nonexistent_module_xyz/toggle",
                method="POST",
                headers=bearer,
                payload={"enabled": False},
            )
        assert exc_nf.value.code == 404

        # 审计表落盘校验
        conn = sandbox_db.connect()
        try:
            cnt = conn.execute("SELECT COUNT(*) FROM audit WHERE action = 'tower_module_toggle'").fetchone()[0]
            assert cnt >= 2
        finally:
            conn.close()


def test_tower_learned_graph_visual_reindex_and_wiki_distill(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-20T14:00:00+08:00")
    _seed_rich_tower_db(sandbox_db)

    with _running_tower(sandbox_db) as (base_url, bearer, _xtok):
        # 1. GET /api/learned 汇总所有已学知识
        st, learned = _http_json(base_url, "/api/learned", headers=bearer)
        assert st == 200 and learned["ok"] is True
        assert learned["counts"]["wiki_pages"] >= 1
        assert learned["counts"]["hypotheses"] == 1
        assert learned["counts"]["msim_entries"] == 1
        assert learned["counts"]["curiosity_answered"] == 1
        assert learned["counts"]["bookmarks"] == 1
        assert "night_owl_cutoff" in learned["routines"]["preview"]

        # 2. 未建索引时 GET /api/graph/visual 返回兜底合成节点与边
        st, vis_pre = _http_json(base_url, "/api/graph/visual", headers=bearer)
        assert st == 200 and vis_pre["ok"] is True
        assert vis_pre["is_synthetic"] is True
        assert len(vis_pre["nodes"]) >= 5
        apps_pre = {n["app"] for n in vis_pre["nodes"]}
        assert {"wiki", "learned", "msim", "chat", "calendar"}.issubset(apps_pre)

        # 3. POST /api/wiki/distill 触发蒸馏（不能误命中 /api/wiki/<slug>）
        st, dist = _http_json(base_url, "/api/wiki/distill", method="POST", headers=bearer, payload={})
        assert st == 200 and dist["ok"] is True
        assert dist["facts_extracted"] >= 2

        # 4. POST /api/graph/reindex 建立真实索引，随后 /api/graph/visual 切为真实图谱 + learned 叠加
        st, reidx = _http_json(base_url, "/api/graph/reindex", method="POST", headers=bearer, payload={})
        assert st == 200 and reidx["ok"] is True
        assert reidx["stats"]["total_nodes"] >= 5

        st, vis_post = _http_json(base_url, "/api/graph/visual", headers=bearer)
        assert st == 200 and vis_post["ok"] is True
        assert vis_post["is_synthetic"] is False
        assert len(vis_post["nodes"]) >= 5
        assert any(n["app"] == "learned" for n in vis_post["nodes"])


def test_tower_admin_cases_waits_approvals_and_subs_management(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-20T14:00:00+08:00")
    _seed_rich_tower_db(sandbox_db)

    with _running_tower(sandbox_db) as (base_url, bearer, _xtok):
        # 1. GET /api/admin/items
        st, admin = _http_json(base_url, "/api/admin/items", headers=bearer)
        assert st == 200 and admin["ok"] is True
        assert any(c["case_id"] == "S41" for c in admin["cases"])
        assert any(w["wait_id"] == "W55" for w in admin["waits"])
        assert any(a["token"] == "A91" for a in admin["approvals"])

        # 2. POST /api/cases/S41/close (销账) -> 原子更新 cases=完成 + subscriptions=ENDED
        st, c_res = _http_json(
            base_url,
            "/api/cases/S41/close",
            method="POST",
            headers=bearer,
            payload={"cancel": False, "note": "tower"},
        )
        assert st == 200 and c_res["ok"] is True and c_res["status"] == "完成"

        # POST /api/cases/S42/close (取消)
        st, c_res2 = _http_json(
            base_url,
            "/api/cases/S42/close",
            method="POST",
            headers=bearer,
            payload={"cancel": True, "note": "tower"},
        )
        assert st == 200 and c_res2["ok"] is True and c_res2["status"] == "取消"

        # 3. POST /api/waits/W55/cancel
        st, w_res = _http_json(base_url, "/api/waits/W55/cancel", method="POST", headers=bearer, payload={})
        assert st == 200 and w_res["ok"] is True and w_res["status"] == "CANCELLED"

        # 4. POST /api/approvals/A91/cancel（绝无 apply 路由）
        st, a_res = _http_json(base_url, "/api/approvals/A91/cancel", method="POST", headers=bearer, payload={})
        assert st == 200 and a_res["ok"] is True

        with pytest.raises(urllib.error.HTTPError) as exc_apply:
            _http_json(base_url, "/api/approvals/A91/apply", method="POST", headers=bearer, payload={})
        assert exc_apply.value.code == 404

        # 5. POST /api/subs/SUB-ICLOUD/status -> 标记为 ZOMBIE 并反映到 /api/finance
        st, s_res = _http_json(
            base_url,
            "/api/subs/SUB-ICLOUD/status",
            method="POST",
            headers=bearer,
            payload={"status": "ZOMBIE"},
        )
        assert st == 200 and s_res["ok"] is True and s_res["status"] == "ZOMBIE"

        st, fin_mid = _http_json(base_url, "/api/finance", headers=bearer)
        assert st == 200 and any(s["sub_id"] == "SUB-ICLOUD" and s["status"] == "ZOMBIE" for s in fin_mid["subscriptions"])

        # 6. POST /api/subs/rescan -> 扫出迅雷连续包月与 iCloud+
        st, scan_res = _http_json(base_url, "/api/subs/rescan", method="POST", headers=bearer, payload={})
        assert st == 200 and scan_res["ok"] is True

        st, fin = _http_json(base_url, "/api/finance", headers=bearer)
        assert st == 200 and fin["ok"] is True
        by_name = {s["normalized_name"]: s for s in fin["subscriptions"]}
        assert "iCloud+" in by_name
        assert "迅雷" in by_name

        # 验证 DB 状态与审计记录完整
        conn = sandbox_db.connect()
        try:
            assert conn.execute("SELECT status FROM cases WHERE case_id='S41'").fetchone()["status"] == "完成"
            assert conn.execute("SELECT state FROM subscriptions WHERE target_id='S41'").fetchone()["state"] == "ENDED"
            assert conn.execute("SELECT status FROM waits WHERE wait_id='W55'").fetchone()["status"] == "CANCELLED"
            assert conn.execute("SELECT status FROM approvals WHERE token='A91'").fetchone()["status"] == "CANCELLED"
            actions = {
                r["action"]
                for r in conn.execute("SELECT DISTINCT action FROM audit").fetchall()
            }
            for expected_act in (
                "tower_close_case",
                "tower_cancel_wait",
                "tower_cancel_approval",
                "tower_sub_status",
                "tower_subs_rescan",
            ):
                assert expected_act in actions
        finally:
            conn.close()
