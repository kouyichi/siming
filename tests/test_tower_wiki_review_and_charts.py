"""2026-11-23 Tower Wiki 冲突审核台 + Sure 风格财务图表端到端测试。"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

from siming.control_tower import create_tower_server, render_tower_html
from siming.db import SimingDB, iso_now
from siming.hindsight_wiki import (
    build_conflict_decision_brief,
    detect_fact_conflicts,
    distill_hindsight_wiki,
    enrich_conflicts_with_llm,
    extract_facts_from_utterances,
    list_wiki_conflicts,
    reopen_wiki_conflict,
    resolve_wiki_conflict,
)


@contextmanager
def _tower(db: SimingDB, token: str = "test-tower-secret-99") -> Iterator[tuple[str, dict[str, str]]]:
    srv = create_tower_server(db, host="127.0.0.1", port=0, token=token)
    port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        yield f"http://127.0.0.1:{port}", {"X-Siming-Token": token}
    finally:
        srv.shutdown()
        srv.server_close()
        t.join(timeout=2)


def _req(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    raw_body: bytes | None = None,
) -> tuple[int, dict[str, Any]]:
    hdrs = dict(headers or {})
    data = raw_body
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(base_url + path, data=data, headers=hdrs, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_tower_html_includes_conflict_review_and_sure_charts_with_valid_js(tmp_path: Path) -> None:
    html = render_tower_html()
    assert 'data-tab="conflicts"' in html
    assert 'id="tc-conflicts"' in html
    assert 'id="panel-conflicts"' in html
    assert 'id="finance-sure-banner"' in html
    assert 'id="chart-netflow-svg"' in html
    assert 'id="chart-bar-svg"' in html
    assert 'id="chart-donut-wrap"' in html
    assert 'id="chart-merchants-wrap"' in html
    assert 'id="finance-usd-card"' in html
    assert "/api/wiki/conflicts" in html
    assert "/api/finance/charts" in html
    # 验证内联 <script> 语法合法（node --check）
    m = re.search(r"<script>([\s\S]*?)</script>", html)
    assert m is not None
    js_file = tmp_path / "tower_check.js"
    js_file.write_text(m.group(1), encoding="utf-8")
    proc = subprocess.run(["node", "--check", str(js_file)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr


def test_wiki_conflict_review_keep_new_both_valid_defer_and_persists_across_distill(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    frozen_clock("2026-11-23T04:50:00+08:00")
    work_dir = sandbox_db.paths.tmp_root / "wiki_test"
    work_dir.mkdir(parents=True, exist_ok=True)
    sess = work_dir / "sess.jsonl"
    wiki_dir = work_dir / "wiki"
    # 制造两组明确冲突：1) package_manager 选型冲突 (pixi vs uv)；2) theme_ui 选型冲突 (dark vs light)
    lines = [
        json.dumps({"role": "user", "content": "我们决定采用 pixi 作为包管理工具", "timestamp": "2026-11-01T10:00:00+08:00"}, ensure_ascii=False),
        json.dumps({"role": "user", "content": "我们决定采用 uv 作为包管理工具", "timestamp": "2026-11-20T10:00:00+08:00"}, ensure_ascii=False),
        json.dumps({"role": "user", "content": "偏好默认用深色模式主题", "timestamp": "2026-11-02T10:00:00+08:00"}, ensure_ascii=False),
        json.dumps({"role": "user", "content": "偏好默认用浅色模式主题", "timestamp": "2026-11-21T10:00:00+08:00"}, ensure_ascii=False),
    ]
    sess.write_text("\n".join(lines) + "\n", encoding="utf-8")

    r1 = distill_hindsight_wiki(sandbox_db, session_files=[sess], wiki_dir=wiki_dir, include_db_sources=False, include_local_sessions=False)
    assert len(r1["conflicts"]) >= 2

    q0 = list_wiki_conflicts(sandbox_db)
    assert q0["counts"]["pending"] >= 2
    by_topic = {c["topic"]: c for c in q0["pending"]}
    assert "package_manager" in by_topic and "theme_ui" in by_topic
    cf_pkg = by_topic["package_manager"]
    cf_theme = by_topic["theme_ui"]

    with _tower(sandbox_db) as (base_url, hdrs):
        # CSRF：拒绝非 application/json 与跨站 Origin
        with pytest.raises(urllib.error.HTTPError) as e_ct:
            _req(base_url, f"/api/wiki/conflicts/{cf_pkg['conflict_id']}/resolve", method="POST", headers={**hdrs, "Content-Type": "text/plain"}, raw_body=b"{}")
        assert e_ct.value.code == 415

        with pytest.raises(urllib.error.HTTPError) as e_org:
            _req(
                base_url,
                f"/api/wiki/conflicts/{cf_pkg['conflict_id']}/resolve",
                method="POST",
                headers={**hdrs, "Origin": "https://evil.example.com"},
                payload={"decision": "keep_new"},
            )
        assert e_org.value.code == 403

        # 1) 对 package_manager 裁决「保留新的」(keep_new -> 保留 uv，淘汰 pixi)
        st1, res1 = _req(
            base_url,
            f"/api/wiki/conflicts/{cf_pkg['conflict_id']}/resolve",
            method="POST",
            headers=hdrs,
            payload={"decision": "keep_new", "note": "统一迁到 uv"},
        )
        assert st1 == 200 and res1["ok"] is True
        assert res1["winner_fact_id"] == cf_pkg["incoming"]["fact_id"]
        assert res1["loser_fact_id"] == cf_pkg["existing"]["fact_id"]

        # 2) 对 theme_ui 先点「稍后」(defer)，移出 pending 进 deferred
        st2, res2 = _req(
            base_url,
            f"/api/wiki/conflicts/{cf_theme['conflict_id']}/resolve",
            method="POST",
            headers=hdrs,
            payload={"decision": "defer", "snooze_days": 7},
        )
        assert st2 == 200 and res2["counts"]["deferred"] == 1
        assert res2["counts"]["pending"] == 0

        # 3) 对 theme_ui 改为「两者都对（不冲突）」(both_valid)
        st3, res3 = _req(
            base_url,
            f"/api/wiki/conflicts/{cf_theme['conflict_id']}/resolve",
            method="POST",
            headers=hdrs,
            payload={"decision": "both_valid", "note": "白浅夜深"},
        )
        assert st3 == 200 and res3["counts"]["pending"] == 0 and res3["counts"]["resolved"] == 2

        # 4) 重新蒸馏：被淘汰的 pixi 事实与 both_valid 冲突对绝不再复活！
        r2 = distill_hindsight_wiki(sandbox_db, session_files=[sess], wiki_dir=wiki_dir, include_db_sources=False, include_local_sessions=False)
        assert r2["conflicts"] == []
        _, q_after = _req(base_url, "/api/wiki/conflicts", headers=hdrs)
        assert q_after["counts"]["pending"] == 0
        assert q_after["counts"]["resolved"] == 2

        # 5) 撤销裁决 (reopen) 后再蒸馏会重新检出
        st4, res4 = _req(base_url, f"/api/wiki/conflicts/{cf_theme['conflict_id']}/reopen", method="POST", headers=hdrs, payload={})
        assert st4 == 200 and res4["reopened"] is True
        r3 = distill_hindsight_wiki(sandbox_db, session_files=[sess], wiki_dir=wiki_dir, include_db_sources=False, include_local_sessions=False)
        assert any(c["conflict_id"] == cf_theme["conflict_id"] for c in r3["conflicts"])


def test_finance_charts_excludes_transfers_nets_refunds_and_isolates_usd(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    frozen_clock("2026-11-23T04:50:00+08:00")
    ts = iso_now()
    txns = [
        # CNY 收入 ¥20,000
        ("TX-INC1", "2026-11-05T10:00:00+08:00", 2000000, "CNY", "建行工资卡", "1004", "收入", "POSTED", "工资入账", ""),
        # CNY 消费：美团 ¥100，京东 ¥5,000，退款 ¥1,000（净支出应为 4,100）
        ("TX-C1", "2026-11-10T12:00:00+08:00", 10000, "CNY", "建设银行储蓄卡(1004)", "1004", "消费", "POSTED", "美团外卖·示例粉面", ""),
        ("TX-C2", "2026-11-11T12:00:00+08:00", 500000, "CNY", "建设银行储蓄卡(1004)", "1004", "消费", "POSTED", "京东自营·小米电视", ""),
        ("TX-R1", "2026-11-12T12:00:00+08:00", 100000, "CNY", "建设银行储蓄卡(1004)", "1004", "退款", "POSTED", "京东自营·小米电视", ""),
        # CNY 划转 ¥8,000：必须排除，既不算支出也不算收入
        ("TX-TR1", "2026-11-15T12:00:00+08:00", 800000, "CNY", "光大还款卡", "1002", "划转", "POSTED", "信用卡还款", ""),
        # VOID 重复流水：必须排除
        ("TX-VOID", "2026-11-16T12:00:00+08:00", 999900, "CNY", "建行", "1004", "消费", "VOID_DUPLICATE", "重复流水", ""),
        # USD 消费 $200（必须隔离在 usd 字段，绝不混入 CNY！）
        ("TX-USD1", "2026-11-18T12:00:00+08:00", 20000, "USD", "光大信用卡", "1001", "消费", "POSTED", "Nous Research", ""),
    ]
    with sandbox_db.transaction() as conn:
        for row in txns:
            conn.execute(
                """
                INSERT INTO ledger_txns (
                    txn_id, occurred_at, amount_cents, currency, account_alias, card_last4,
                    kind, status, merchant, notes, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (*row, ts, ts),
            )

    with _tower(sandbox_db) as (base_url, hdrs):
        st, fc = _req(base_url, "/api/finance/charts", headers=hdrs)
        assert st == 200 and fc["ok"] is True
        s12 = fc["cny_summary_12m"]
        assert s12["income"] == 20000.0
        assert s12["refunds"] == 1000.0
        assert s12["net_spend"] == 4100.0
        assert s12["net_cashflow"] == 15900.0

        # 划转和 USD 不得混入 CNY 分类
        cur_cats = fc["categories"]["current_month"]
        assert cur_cats["total"] == 4100.0
        cat_map = {c["name"]: c["amount"] for c in cur_cats["items"]}
        assert cat_map["餐饮外卖"] == 100.0
        assert cat_map["家居装修"] == 4000.0

        # USD 严格独立
        assert fc["usd"]["total_all_spend"] == 200.0
        assert fc["usd"]["spend_12m"] == 200.0
        assert "sure_status" in fc and "local_url" in fc["sure_status"]


def test_wiki_conflicts_zero_truncation_no_author_false_positives_and_human_briefs(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    frozen_clock("2026-11-23T20:10:00+08:00")
    work_dir = sandbox_db.paths.tmp_root / "wiki_human_test"
    work_dir.mkdir(parents=True, exist_ok=True)
    wiki_dir = work_dir / "wiki"

    # 1) 两条超过 400 字的真实同主题覆写（自动价保授权 -> 同日下午撤回授权）+ 两条带 (claude)/(codex) 署名的无关笔记 + 一条 OCR 垃圾
    long_old_price = (
        "2026-11-11 价保监控安全整改（codex）：用户在 QQ 会话里授权「现价低于实付即自动一键价保、成功失败都通知」，"
        "但前一助手为验收短时间内重复查商品/点价保触发京东验证；修订后增加账户共用锁与冷却，每轮最多两页、间隔不少于60秒、"
        "同SKU缓存4小时，遇验证停24小时，真实H5兼容性仍需冷却后确认。" + ("补充校验细节说明。" * 25)
    )
    long_new_price = (
        "2026-11-11 下午用户撤回自动价保授权（codex）：「先不提自动保价，首先恢复QQ的低价提示，现在是否有可以保价的商品，我自己来」。"
        "orders.json 里 auto_claim=false，正常入口与独立 claim 入口都不提交申请；只保留查价提醒，上文 11-11「现价低于实付即自动一键价保」的授权作废。"
        + ("保留只读轮询与每日低价汇总说明。" * 20)
    )
    assert len(long_old_price) > 350 and len(long_new_price) > 350

    md_appliances = work_dir / "home-appliance-research-202610.md"
    md_appliances.write_text(
        "---\nname: home-appliance-research-202610\nupdated: 2026-11-12\n---\n\n"
        f"{long_old_price}\n\n{long_new_price}\n\n"
        "（2026-11-12 codex 更新）\n",
        encoding="utf-8",
    )
    md_unrelated = work_dir / "reboot-aftermath-20261115.md"
    md_unrelated.write_text(
        "---\nname: reboot-aftermath-20261115\nupdated: 2026-11-15\n---\n\n"
        "`claude-tb-net.service`（system）开机失败：NetworkManager wait-online 是 masked，已修复为显式等待默认路由。\n",
        encoding="utf-8",
    )
    ocr_junk = work_dir / "ocr_dump.jsonl"
    ocr_junk.write_text(
        json.dumps(
            {
                "role": "user",
                "content": "00:49\n70+\nQ 商品名/订单号/快递单号\n全部订单\n待收货/使用5\n默认不可退\n删除订单\n再次购头\n合计¥9.90\n去评价",
                "timestamp": "2026-11-19T00:52:00+08:00",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    res = distill_hindsight_wiki(
        sandbox_db,
        session_files=[md_appliances, md_unrelated, ocr_junk],
        wiki_dir=wiki_dir,
        include_db_sources=False,
        include_local_sessions=False,
        include_governance_sources=False,
        summarize_with_llm=False,
    )
    assert res["ok"] is True
    q = list_wiki_conflicts(sandbox_db, include_resolved=True, include_filtered=True)

    # OCR 购物单垃圾与无关的 claude-tb-net.service 不得产生任何冲突！
    assert q["counts"]["pending"] == 1, f"应仅检出 1 条自动价保覆写冲突，实际：{[(c['topic'], c['reason']) for c in q['pending']]}"
    cf = q["pending"][0]
    assert cf["topic"] == "price_watch"
    # 时间顺序：凌晨授权为旧，下午撤回为新
    assert "授权「现价低于实付" in cf["existing"]["verbatim"]
    assert "撤回自动价保授权" in cf["incoming"]["verbatim"]
    # 绝不截断在 280 字！完整保留 350+ 字原文
    assert len(cf["existing"]["verbatim"]) == len(long_old_price)
    assert len(cf["incoming"]["verbatim"]) == len(long_new_price)
    assert "补充校验细节说明。" in cf["existing"]["verbatim"]
    assert "保留只读轮询与每日低价汇总说明。" in cf["incoming"]["verbatim"]
    # 人话裁决简报必须完整且不含机器黑话
    brief = cf["decision_brief"]
    assert brief["domain_label"] == "网购保价策略"
    assert brief["decision_question"]
    assert brief["conflict_point"]
    assert brief["old_summary"] and brief["new_summary"]
    assert brief["old_choice_label"] and brief["new_choice_label"]
    assert brief["recommendation"] == "keep_new"
    assert "positive vs negative" not in brief["conflict_point"]
    assert "model_routing" not in brief["domain_label"]


def test_wiki_conflicts_governance_sources_and_llm_filter_restore(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    frozen_clock("2026-11-23T20:20:00+08:00")
    # 写入一条 _规则待审.md 提案
    rule_review = sandbox_db.paths.shared_memory_dir / "_规则待审.md"
    rule_review.parent.mkdir(parents=True, exist_ok=True)
    rule_review.write_text(
        "# 规则待审\n\n"
        "## 2026-10-31，来源 codex（aTrust 单实例应用残留在隔离桌面）\n\n"
        "不要在共享用户配置下把单实例应用长期留在隔离桌面；测试结束必须清理界面进程。\n",
        encoding="utf-8",
    )

    # 跑包含治理源的蒸馏（会收集 DECISIONS.md 的 ⚠️ 待重新拍板 + _规则待审.md）
    res = distill_hindsight_wiki(
        sandbox_db,
        include_db_sources=False,
        include_local_sessions=False,
        include_governance_sources=True,
        summarize_with_llm=False,
    )
    assert res["ok"] is True
    q = list_wiki_conflicts(sandbox_db, include_resolved=True, include_filtered=True)
    kinds = {c["kind"] for c in q["pending"]}
    assert "policy_conflict" in kinds
    assert "rule_proposal" in kinds

    # 验证模拟 LLM 将某条普通冲突判定为误报 -> 进入 filtered，且支持 POST /restore 移回待裁决
    fake_conflict = {
        "kind": "contradiction",
        "topic": "dev_cli",
        "reason": "测试候选冲突",
        "existing": {
            "fact_id": "F-fakeold1",
            "statement": "旧测试陈述 A",
            "verbatim": "旧测试陈述 A",
            "source": "a.md#L1",
            "timestamp": "2026-11-01T10:00:00+08:00",
        },
        "incoming": {
            "fact_id": "F-fakenew2",
            "statement": "新测试陈述 B",
            "verbatim": "新测试陈述 B",
            "source": "b.md#L2",
            "timestamp": "2026-11-20T10:00:00+08:00",
        },
        "pages": [{"slug": "preferences", "title": "Preferences", "category": "preference"}],
    }
    fake_conflict["conflict_id"] = "CF-testfilter01"
    stats = enrich_conflicts_with_llm(
        sandbox_db,
        [fake_conflict],
        force=True,
        allow_llm=True,
        llm_caller=lambda batch: (
            "mock-flash",
            [
                {
                    "conflict_id": batch[0]["conflict_id"],
                    "is_real_conflict": False,
                    "domain_label": "本机开发工具",
                    "decision_question": "两条无关",
                    "conflict_point": "无实际冲突",
                    "old_summary": "A",
                    "new_summary": "B",
                    "old_choice_label": "选 A",
                    "new_choice_label": "选 B",
                    "recommendation": "both_valid",
                    "recommendation_reason": "两条讨论不同组件，互不冲突。",
                }
            ],
        ),
    )
    assert stats["llm_updated"] == 1 and stats["filtered_noise"] == 1

    with _tower(sandbox_db) as (base_url, hdrs):
        st, q_api = _req(base_url, "/api/wiki/conflicts", headers=hdrs)
        assert st == 200
        assert any(c["conflict_id"] == "CF-testfilter01" for c in q_api["filtered"])
        # 测试 /restore 把误筛除条目移回待裁决
        st_r, r_res = _req(base_url, "/api/wiki/conflicts/CF-testfilter01/restore", method="POST", headers=hdrs, payload={})
        assert st_r == 200 and r_res["restored"] is True
        st2, q_api2 = _req(base_url, "/api/wiki/conflicts", headers=hdrs)
        assert st2 == 200
        assert any(c["conflict_id"] == "CF-testfilter01" for c in q_api2["pending"])
        # 测试 /summarize 接口（离线环境下走启发式兜底且受 CSRF 保护）
        st_s, s_res = _req(base_url, "/api/wiki/conflicts/summarize", method="POST", headers=hdrs, payload={"force": False})
        assert st_s == 200 and s_res["ok"] is True
