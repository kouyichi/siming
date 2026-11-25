"""订阅刺客「是否还在进行 (is_ongoing)」标志、自动停扣推断、手动覆盖与 Control Tower API/UI 回归测试。"""

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
from siming.qq_cards import handle_qq_button_action
from siming.sub_hunter import (
    emit_subscription_stoploss_alerts,
    find_upcoming_charges,
    format_subscription_audit,
    mark_subscription_ongoing,
    mark_subscription_status,
    scan_subscriptions,
)
from siming.sure_bridge import list_sure_rules


def _insert_txn(
    db: SimingDB,
    txn_id: str,
    occurred_at: str,
    amount_cents: int,
    merchant: str,
    notes: str = "",
    currency: str = "CNY",
) -> None:
    ts = iso_now()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ledger_txns (
                txn_id, occurred_at, amount_cents, currency, account_alias,
                card_last4, kind, status, merchant, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, '招行', '8848', '消费', 'POSTED', ?, ?, ?, ?)
            """,
            (txn_id, occurred_at, amount_cents, currency, merchant, notes, ts, ts),
        )


@contextmanager
def _tower(db: SimingDB, token: str = "test-sub-ongoing-token") -> Iterator[tuple[str, dict[str, str]]]:
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
) -> tuple[int, dict[str, Any]]:
    hdrs = dict(headers or {})
    data = None
    if payload is not None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(base_url + path, data=data, headers=hdrs, method=method)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(req, timeout=5) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def test_subscription_ongoing_auto_inference_excludes_lapsed_from_cost_and_alerts(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    now = frozen_clock("2026-11-23T10:00:00+08:00")

    # 1) 进行中 · 月付正常：iCloud+（2 天前刚扣费）
    _insert_txn(sandbox_db, "OG-IC1", "2026-10-21T10:00:00+08:00", 600, "iCloud 由云上贵州运营")
    _insert_txn(sandbox_db, "OG-IC2", "2026-11-21T10:00:00+08:00", 600, "iCloud 由云上贵州运营")

    # 2) 进行中 · 僵尸候选：迅雷（8 天前扣费，且涨价）
    _insert_txn(sandbox_db, "OG-XL1", "2026-10-15T10:00:00+08:00", 1900, "深圳市迅雷网络技术有限公司 · 超级会员")
    _insert_txn(sandbox_db, "OG-XL2", "2026-11-15T10:00:00+08:00", 2500, "深圳市迅雷网络技术有限公司 · 超级会员")

    # 3) 已停扣 · 月付历史：小桔充电会员（21 个月前已停止扣费，且当年有涨价）
    _insert_txn(sandbox_db, "ST-XJ1", "2025-01-15T08:00:00+08:00", 990, "小桔新能源 · 小桔充电会员自动续费")
    _insert_txn(sandbox_db, "ST-XJ2", "2025-02-15T08:00:00+08:00", 1500, "小桔新能源 · 小桔充电会员自动续费")

    # 4) 已停扣 · 周付历史：繁花剧场（3 个多月前停扣）
    _insert_txn(sandbox_db, "ST-FH1", "2026-08-05T10:00:00+08:00", 2900, "繁花剧场 · 繁花剧场-7天会员")
    _insert_txn(sandbox_db, "ST-FH2", "2026-08-12T10:00:00+08:00", 2900, "繁花剧场 · 繁花剧场-7天会员")

    scan = scan_subscriptions(sandbox_db, now=now)
    assert scan["ok"] is True
    assert scan["ongoing_count"] == 2
    assert scan["stopped_count"] == 2

    by_name = {s["normalized_name"]: s for s in scan["subscriptions"]}
    assert by_name["iCloud+"]["is_ongoing"] is True
    assert by_name["iCloud+"]["ongoing_source"] == "AUTO"
    assert by_name["迅雷"]["is_ongoing"] is True
    assert by_name["迅雷"]["status"] == "ZOMBIE"

    # 已停扣订阅：is_ongoing=False，且 next_expected_at 不得被假滚推到未来
    assert by_name["小桔充电会员"]["is_ongoing"] is False
    assert by_name["小桔充电会员"]["ongoing_label"] == "已停扣"
    assert by_name["小桔充电会员"]["next_expected_at"].startswith("2025-03-")

    assert by_name["繁花剧场"]["is_ongoing"] is False
    assert by_name["繁花剧场"]["next_expected_at"].startswith("2026-08-19")

    # 停扣订阅绝不进入 price_hikes / zombies / 48h 止损卡 / Sure 活跃周期规则
    assert all(h["normalized_name"] != "小桔充电会员" for h in scan["price_hikes"])
    assert any(h["normalized_name"] == "迅雷" for h in scan["price_hikes"])
    assert all(z["normalized_name"] not in ("小桔充电会员", "繁花剧场") for z in scan["zombies"])
    assert any(z["normalized_name"] == "迅雷" for z in scan["zombies"])

    alerts = emit_subscription_stoploss_alerts(sandbox_db, now=now)
    assert all(a["normalized_name"] not in ("小桔充电会员", "繁花剧场") for a in alerts["alerts"])
    assert any(a["normalized_name"] == "迅雷" for a in alerts["alerts"])

    rule_names = {r["name"] for r in list_sure_rules(sandbox_db)}
    assert "订阅:iCloud+" in rule_names
    assert "订阅:迅雷" in rule_names
    assert "订阅:小桔充电会员" not in rule_names
    assert "订阅:繁花剧场" not in rule_names

    # 年度在途成本仅计算进行中订阅：iCloud+ (6*12=72) + 迅雷 (25*12=300) = ¥372.00/年
    audit_txt = format_subscription_audit(sandbox_db, now=now)
    assert "约 ¥372.00/年" in audit_txt
    assert "▶️[进行中]" in audit_txt
    assert "⏹️[已停扣]" in audit_txt


def test_subscription_manual_ongoing_override_persists_and_auto_reactivates_on_new_charge(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    now = frozen_clock("2026-11-23T10:00:00+08:00")
    _insert_txn(sandbox_db, "M-IC1", "2026-10-21T10:00:00+08:00", 600, "iCloud 由云上贵州运营")
    _insert_txn(sandbox_db, "M-IC2", "2026-11-21T10:00:00+08:00", 600, "iCloud 由云上贵州运营")
    _insert_txn(sandbox_db, "M-XJ1", "2025-01-15T08:00:00+08:00", 990, "小桔新能源 · 小桔充电会员自动续费")
    _insert_txn(sandbox_db, "M-XJ2", "2025-02-15T08:00:00+08:00", 990, "小桔新能源 · 小桔充电会员自动续费")

    scan_subscriptions(sandbox_db, now=now)

    # 1) 手动把 iCloud+ 标为已停扣，不改变其 ACTIVE 标签
    r1 = mark_subscription_ongoing(sandbox_db, "iCloud+", False, note="已切家庭共享")
    assert r1["ok"] is True
    assert r1["status"] == "ACTIVE"
    assert r1["is_ongoing"] is False
    assert r1["ongoing_source"] == "MANUAL"

    # 2) 手动把历史停扣的小桔充电会员标为进行中（例如换卡继续付费）
    r2 = mark_subscription_ongoing(sandbox_db, "小桔充电会员", True)
    assert r2["ok"] is True
    assert r2["is_ongoing"] is True
    assert r2["ongoing_source"] == "MANUAL"
    assert r2["next_expected_at"] >= "2026-11-23"

    # 3) 重扫严格保留用户手动决策
    rescan = scan_subscriptions(sandbox_db, now=now)
    by_name = {s["normalized_name"]: s for s in rescan["subscriptions"]}
    assert by_name["iCloud+"]["is_ongoing"] is False
    assert by_name["iCloud+"]["ongoing_source"] == "MANUAL"
    assert by_name["小桔充电会员"]["is_ongoing"] is True
    assert by_name["小桔充电会员"]["ongoing_source"] == "MANUAL"

    # 4) 切换状态 ACTIVE -> KEEP 不覆盖手动已停扣标志
    r_keep = mark_subscription_status(sandbox_db, "iCloud+", "KEEP")
    assert r_keep["ok"] is True and r_keep["status"] == "KEEP" and r_keep["is_ongoing"] is False

    # 5) 拒绝 CANCELLED + is_ongoing=True 矛盾组合
    bad = mark_subscription_status(sandbox_db, "iCloud+", "CANCELLED", is_ongoing=True)
    assert bad["ok"] is False

    # 6) 标记 CANCELLED 后仅切换 is_ongoing=True -> 自动恢复为 ACTIVE + 进行中
    mark_subscription_status(sandbox_db, "iCloud+", "CANCELLED")
    revived = mark_subscription_ongoing(sandbox_db, "iCloud+", True)
    assert revived["ok"] is True and revived["status"] == "ACTIVE" and revived["is_ongoing"] is True

    # 7) 手动标为已停扣后，若未来真的又扣费了 -> 重扫自动恢复为进行中
    mark_subscription_ongoing(sandbox_db, "iCloud+", False)
    later = frozen_clock("2026-12-22T10:00:00+08:00")
    _insert_txn(sandbox_db, "M-IC3", "2026-12-21T10:00:00+08:00", 600, "iCloud 由云上贵州运营")
    rescan2 = scan_subscriptions(sandbox_db, now=later)
    ic_after = next(s for s in rescan2["subscriptions"] if s["normalized_name"] == "iCloud+")
    assert ic_after["is_ongoing"] is True
    assert ic_after["ongoing_source"] == "AUTO"


def test_tower_api_and_qq_callbacks_support_ongoing_flag_and_all_status_options(
    sandbox_db: SimingDB, frozen_clock, tmp_path: Path
) -> None:
    now = frozen_clock("2026-11-23T10:00:00+08:00")
    _insert_txn(sandbox_db, "T-IC1", "2026-10-21T10:00:00+08:00", 600, "iCloud 由云上贵州运营")
    _insert_txn(sandbox_db, "T-IC2", "2026-11-21T10:00:00+08:00", 600, "iCloud 由云上贵州运营")
    _insert_txn(sandbox_db, "T-XJ1", "2025-01-15T08:00:00+08:00", 990, "小桔新能源 · 小桔充电会员自动续费")
    _insert_txn(sandbox_db, "T-XJ2", "2025-02-15T08:00:00+08:00", 990, "小桔新能源 · 小桔充电会员自动续费")
    scan = scan_subscriptions(sandbox_db, now=now)
    ic_id = next(s["sub_id"] for s in scan["subscriptions"] if s["normalized_name"] == "iCloud+")
    xj_id = next(s["sub_id"] for s in scan["subscriptions"] if s["normalized_name"] == "小桔充电会员")

    # 验证 HTML 包含独立「是否还在进行」开关、每月订阅总花费汇总区、筛选胶囊、全部 4 个状态按钮，且内联 JS 语法合法
    html = render_tower_html()
    assert "是否还在进行" in html
    assert "每月订阅总花费" in html
    assert "finance-subs-summary" in html
    assert "subMonthlyCostCny" in html
    assert "toggleSubOngoing" in html
    assert "setSubFilter" in html
    assert "markSubStatus(this.dataset.subId,'ACTIVE')" in html
    assert "markSubStatus(this.dataset.subId,'KEEP')" in html
    assert "markSubStatus(this.dataset.subId,'ZOMBIE')" in html
    assert "markSubStatus(this.dataset.subId,'CANCELLED')" in html

    m = re.search(r"<script>([\s\S]*?)</script>", html)
    assert m is not None
    js_file = tmp_path / "tower_sub_check.js"
    js_file.write_text(m.group(1), encoding="utf-8")
    proc = subprocess.run(["node", "--check", str(js_file)], capture_output=True, text=True)
    assert proc.returncode == 0, proc.stderr

    with _tower(sandbox_db) as (base_url, hdrs):
        st, fin = _req(base_url, "/api/finance", headers=hdrs)
        assert st == 200 and fin["ok"] is True
        ss = fin["sub_summary"]
        assert ss["total"] == 2
        assert ss["ongoing_count"] == 1
        assert ss["stopped_count"] == 1
        assert ss["monthly_cost_cny"] == 6.0
        assert ss["annual_cost_cny"] == 72.0
        assert ss["stopped_monthly_cost_cny"] == 9.9

        # 进行中排在停扣之前，且每条订阅自带折合月花费
        assert fin["subscriptions"][0]["sub_id"] == ic_id
        assert fin["subscriptions"][0]["is_ongoing"] is True
        assert fin["subscriptions"][0]["monthly_cost_cny"] == 6.0
        assert fin["subscriptions"][1]["sub_id"] == xj_id
        assert fin["subscriptions"][1]["is_ongoing"] is False
        assert fin["subscriptions"][1]["monthly_cost_cny"] == 9.9

        # /api/finance/charts recurring 仅含进行中订阅
        st_ch, ch = _req(base_url, "/api/finance/charts", headers=hdrs)
        assert st_ch == 200
        assert [r["sub_id"] for r in ch["recurring"]] == [ic_id]

        # /api/overview 同步反映进行中 / 已停扣计数
        st_ov, ov = _req(base_url, "/api/overview", headers=hdrs)
        assert st_ov == 200
        assert ov["wave1_finance"]["ongoing_subscriptions"] == 1
        assert ov["wave1_finance"]["stopped_subscriptions"] == 1

        # 单独通过 API 切换 is_ongoing=False，保留原状态
        st_tog, res_tog = _req(
            base_url,
            f"/api/subs/{ic_id}/status",
            method="POST",
            headers=hdrs,
            payload={"is_ongoing": False},
        )
        assert st_tog == 200 and res_tog["ok"] is True
        assert res_tog["is_ongoing"] is False and res_tog["status"] == "ACTIVE"

        # 确认财务汇总立即更新为 0 项进行中
        _, fin2 = _req(base_url, "/api/finance", headers=hdrs)
        assert fin2["sub_summary"]["ongoing_count"] == 0
        assert fin2["sub_summary"]["stopped_count"] == 2
        assert fin2["sub_summary"]["annual_cost_cny"] == 0.0

        # 拒绝非法 is_ongoing / 矛盾状态
        with pytest.raises(urllib.error.HTTPError) as exc_bad:
            _req(
                base_url,
                f"/api/subs/{ic_id}/status",
                method="POST",
                headers=hdrs,
                payload={"status": "CANCELLED", "is_ongoing": True},
            )
        assert exc_bad.value.code == 400

    # QQ 按钮回调：sub_ongoing & sub_status 均可工作
    q1 = handle_qq_button_action(sandbox_db, f"siming:sub_ongoing:{ic_id}:1")
    assert q1["ok"] is True and "进行中" in q1["reply"]
    q2 = handle_qq_button_action(sandbox_db, f"siming:sub_status:{ic_id}:KEEP")
    assert q2["ok"] is True and "KEEP" in q2["reply"]
