"""Batch A 全场景 Personal Agent 单元测试（全程运行在 sandbox_db 隔离目录，零触碰真实库与外部网络）。"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from siming.approval import verify_and_apply_approval
from siming.attention import (
    evaluate_attention_state,
    record_explicit_sleep_state,
    should_defer_morning_brief_for_sleep,
)
from siming.cdp import detect_compliance_gate
from siming.collectors.edge_sync import process_edge_records
from siming.db import SHANGHAI_TZ, SimingDB, iso_now
from siming.dev_twin import build_ten_second_resume_card, scan_stuck_herdr_agents
from siming.ledger.reconcile import match_or_record_wallet_row
from siming.luckin_mcp import (
    execute_luckin_confirm_order,
    save_luckin_mcp_token,
    stage_luckin_order,
)
from siming.meituan_agent import recommend_meituan_meals, stage_meituan_h5_order
from siming.router import handle_inbound_qq
from siming.self_heal import diagnose_and_heal_dev_links, evaluate_nous_billing_sentinel


class _FakeLuckinSession:
    def __init__(self, price_yuan: float = 13.9) -> None:
        self.price_yuan = price_yuan
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def initialize_and_list_tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "quickOrder",
                "inputSchema": {"properties": {"query": {"type": "string"}}},
            },
            {
                "name": "confirmOrder",
                "inputSchema": {"properties": {"draftId": {"type": "string"}}},
            },
            {
                "name": "orderStatus",
                "inputSchema": {"properties": {}},
            },
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        self.calls.append((name, arguments))
        if name == "quickOrder":
            return {
                "draftId": "LK-DRAFT-8801",
                "shopName": "示例写字楼C座店",
                "productSummary": "大杯冰美式（不另外加糖）",
                "payAmount": self.price_yuan,
            }
        if name == "confirmOrder":
            return {
                "orderId": "LK-ORD-9910",
                "pickupCode": "8206",
                "payUrl": "https://pay.lkcoffee.com/mock/9910",
            }
        return {"status": "制作中", "pickupCode": "8206"}


def test_cdp_compliance_gate_never_bypasses_captcha(sandbox_db: SimingDB) -> None:
    gate_slider = detect_compliance_gate(
        "https://verify.meituan.com/v2/web/general_page?action=spiderindefence",
        "安全验证",
        "请拖动滑块完成验证",
    )
    assert gate_slider is not None
    assert gate_slider["gate_kind"] == "captcha_verification"

    gate_login = detect_compliance_gate("https://passport.meituan.com/account/unitivelogin", "登录", "")
    assert gate_login is not None
    assert gate_login["gate_kind"] == "login_required"

    # 当美团 H5 遇到滑块验证时，stage_meituan_h5_order 必须合规停手、不生成 apply 口令
    res = stage_meituan_h5_order(
        sandbox_db,
        "点外卖 素坤泰 泰式打抛饭",
        cdp_probe_fn=lambda _q: {
            "ok": False,
            "gated": True,
            "gate": gate_slider,
            "kept_tab_open": True,
        },
    )
    assert res["ok"] is False
    assert res["gated"] is True
    assert "apply A" not in res["reply"]
    assert "绝不硬闯风控验证码" in res["reply"]


def test_luckin_mcp_stage_cap_and_apply(sandbox_db: SimingDB) -> None:
    # 1. 未配 Token 时返回清晰引导，不报错崩溃
    unconf = stage_luckin_order(sandbox_db, "来杯瑞幸 大杯冰美式")
    assert unconf["ok"] is True
    assert unconf["configured"] is False
    assert "open.lkcoffee.com" in unconf["reply"]

    # 2. 保存 Token（0600 权限）
    sk = save_luckin_mcp_token(sandbox_db, "Bearer test_luckin_secret_token_LUCKIN_MCP_AI")
    assert sk["ok"] is True
    sec_file = sandbox_db.paths.secrets_dir / "luckin-mcp-token"
    assert sec_file.is_file()
    assert (sec_file.stat().st_mode & 0o777) == 0o600

    # 3. 超过单笔安全上限（默认 ¥65）自动拦截
    over = stage_luckin_order(
        sandbox_db,
        "来杯瑞幸 十杯拿铁",
        session_factory=lambda _tk: _FakeLuckinSession(price_yuan=128.0),
    )
    assert over["ok"] is False
    assert over.get("over_cap") is True

    # 4. 正常金额生成 15m apply 确认卡，确认后拿到取餐码 8206
    fake_sess = _FakeLuckinSession(price_yuan=13.9)
    staged = stage_luckin_order(
        sandbox_db,
        "来杯瑞幸 大杯冰美式不加糖",
        session_factory=lambda _tk: fake_sess,
    )
    assert staged["ok"] is True
    tok = staged["approval_token"]
    assert f"apply {tok}" in staged["reply"]

    applied = verify_and_apply_approval(
        sandbox_db,
        tok,
        extra_executors={
            "luckin_confirm_order": lambda p, d: execute_luckin_confirm_order(p, d, session_factory=lambda _tk: fake_sess)
        },
    )
    assert applied["ok"] is True
    assert "8206" in applied["summary"]


def test_meituan_recommend_and_h5_lock_cart_apply(sandbox_db: SimingDB) -> None:
    for d, (shop, cents) in (
        ("06", ("美团 · 老乡鸡(中关村店)-美团外卖App-101", 3400)),
        ("08", ("美团 · 老乡鸡(中关村店)-美团外卖App-102", 3600)),
        ("10", ("美团 · 东北小院(知春路店)-美团外卖App-103", 8200)),
    ):
        match_or_record_wallet_row(
            sandbox_db,
            occurred_at=f"2026-11-{d}T19:30:00+08:00",
            amount_cents=cents,
            card_last4="",
            wallet_alias="微信",
            merchant=shop,
            kind="消费",
        )

    rec = recommend_meituan_meals(sandbox_db, hint="晚餐", check_cdp=False)
    assert rec["ok"] is True
    assert rec["picks"][0]["shop"] == "老乡鸡(中关村店)"

    # 引用「第1家」生成加购确认卡（含主推 + 备选参考）
    staged = stage_meituan_h5_order(
        sandbox_db,
        "点外卖 第1家 农家小炒肉套餐",
        cdp_probe_fn=lambda _q: {
            "ok": True,
            "gated": False,
            "data": json.dumps({"top_lines": ["老乡鸡(中关村店)", "月售2000+", "32分钟送达"], "prices": ["¥35"]}),
        },
    )
    assert staged["ok"] is True
    assert "老乡鸡(中关村店)" in staged["reply"]
    assert "东北小院(知春路店)" in staged["reply"]
    tok = staged["approval_token"]

    applied = verify_and_apply_approval(sandbox_db, tok)
    assert applied["ok"] is True
    assert "imeituan://" in applied["summary"]


def test_night_owl_awake_at_2am_and_asleep_at_9am_brief_deferral(sandbox_db: SimingDB) -> None:
    # 1. 凌晨 02:30 桌面未锁且键鼠刚动过 -> 判定夜猫子清醒写代码，不拦截 L2 通知！
    dt_2am = datetime(2026, 11, 18, 2, 30, tzinfo=SHANGHAI_TZ)
    st_2am = evaluate_attention_state(
        sandbox_db,
        desktop_probe=lambda: {"available": True, "locked": False, "idle_sec": 18.0},
        now_override=dt_2am,
    )
    assert st_2am["state"] == "AWAKE_ACTIVE"
    assert st_2am["should_hold_l2"] is False

    # 2. 上午 09:00 仍在锁屏补觉（空闲 4 小时） -> 判定为 ASLEEP，且延后 08:15 晨札不吵醒用户！
    dt_9am = datetime(2026, 11, 18, 9, 0, tzinfo=SHANGHAI_TZ)
    st_9am = evaluate_attention_state(
        sandbox_db,
        desktop_probe=lambda: {"available": True, "locked": True, "idle_sec": 14400.0},
        now_override=dt_9am,
    )
    assert st_9am["state"] == "ASLEEP"
    assert st_9am["should_hold_l2"] is True
    defer_9am, _ = should_defer_morning_brief_for_sleep(
        sandbox_db,
        desktop_probe=lambda: {"available": True, "locked": True, "idle_sec": 14400.0},
        now_override=dt_9am,
    )
    assert defer_9am is True

    # 3. 到达 11:50 最晚底线时不再无限延后晨札
    dt_1150 = datetime(2026, 11, 18, 11, 50, tzinfo=SHANGHAI_TZ)
    defer_1150, reason_1150 = should_defer_morning_brief_for_sleep(
        sandbox_db,
        desktop_probe=lambda: {"available": True, "locked": True, "idle_sec": 20000.0},
        now_override=dt_1150,
    )
    assert defer_1150 is False
    assert "reached_latest_deadline" in reason_1150

    # 4. iOS 快捷指令发 sleep / QQ 发「早」联动切换
    process_edge_records(sandbox_db, [{"id": 1, "source": "ios_sleep", "text": "sleep"}])
    assert evaluate_attention_state(sandbox_db)["state"] == "ASLEEP"

    wake_reply = handle_inbound_qq(sandbox_db, "早")
    assert wake_reply["handled"] is True
    assert "早" in wake_reply["reply"]
    record_explicit_sleep_state(sandbox_db, sleeping=False, source="test")


def test_self_heal_nous_lock_and_dns_flush_and_root_cause(sandbox_db: SimingDB) -> None:
    # 1. 收到 Auto-refill Payment Disabled 邮件 -> 建 HARD 单并推三步恢复卡；收到新收据 -> 自动销账
    t1 = "2026-11-17T10:00:00+08:00"
    t2 = "2026-11-17T12:00:00+08:00"
    with sandbox_db.transaction() as conn:
        sandbox_db.append_event(
            conn,
            source="mail163",
            thread_key="mail:gmail:1",
            summary="[Gmail] Nous Research - Auto-refill Payment Disabled",
            payload={"sender": "noreply@nousresearch.com"},
            occurred_at=t1,
            fingerprint="omamail:gmail:1",
        )
    s1 = evaluate_nous_billing_sentinel(sandbox_db, notify=True)
    assert s1["locked"] is True
    cid = s1["case_id"]
    assert cid.startswith("S")

    with sandbox_db.transaction() as conn:
        sandbox_db.append_event(
            conn,
            source="mail163",
            thread_key="mail:gmail:2",
            summary="[Gmail] Nous Research - Auto-refill Successful",
            payload={"sender": "noreply@nousresearch.com"},
            occurred_at=t2,
            fingerprint="omamail:gmail:2",
        )
    s2 = evaluate_nous_billing_sentinel(sandbox_db, notify=True)
    assert s2["locked"] is False
    conn = sandbox_db.connect()
    try:
        st_case = conn.execute("SELECT status FROM cases WHERE case_id=?", (cid,)).fetchone()["status"]
    finally:
        conn.close()
    assert st_case == "已完成"

    # 2. OA 直连通而 7890 代理不通 -> 自动调用 sing-box DNS 负缓存清理并恢复
    state = {"flushed": False}

    def _net_probe() -> dict[str, Any]:
        return {
            "proxy_7890_up": True,
            "clash_api_9090_up": True,
            "oa_dns_ok": True,
            "oa_direct_ok": True,
            "oa_proxy_ok": state["flushed"],
            "remote_reachable": True,
            "remote_detail": "herdr:gpu-server=connected",
        }

    def _flush() -> tuple[bool, str]:
        state["flushed"] = True
        return True, "ok"

    diag = diagnose_and_heal_dev_links(
        sandbox_db,
        allow_heal=True,
        net_probe_fn=_net_probe,
        dns_flush_fn=_flush,
        cdp_check_fn=lambda: {"main_chromium_up": True, "atrust_collision_suspect": False},
    )
    assert state["flushed"] is True
    assert diag["ok"] is True
    assert any("清空 sing-box DNS 负缓存" in x for x in diag["healed_actions"])

    # 3. aTrust 离线同时导致 OA 与 gpu-server 不通 -> 合成为唯一根因
    diag_vpn = diagnose_and_heal_dev_links(
        sandbox_db,
        allow_heal=False,
        net_probe_fn=lambda: {
            "proxy_7890_up": True,
            "clash_api_9090_up": True,
            "oa_dns_ok": False,
            "oa_direct_ok": False,
            "oa_proxy_ok": False,
            "remote_reachable": False,
            "remote_detail": "herdr:gpu-server=disconnected",
        },
        cdp_check_fn=lambda: {"main_chromium_up": True, "atrust_collision_suspect": False},
    )
    assert len(diag_vpn["root_causes"]) == 1
    assert "唯一根因：内网 VPN 未在线" in diag_vpn["root_causes"][0]


def test_dev_twin_stuck_agent_5m_dedup_and_resume_card(sandbox_db: SimingDB) -> None:
    t0 = datetime(2026, 11, 17, 21, 0, tzinfo=SHANGHAI_TZ)

    def _fake_herdr(args: list[str]) -> dict[str, Any]:
        if args == ["pane", "list"]:
            return {"result": {"panes": [{"pane_id": "w4:p9X", "cwd": "/home/user/Work/indie_game"}]}}
        if args == ["agent", "list"]:
            return {
                "result": {
                    "agents": [
                        {
                            "pane_id": "w4:p9X",
                            "agent": "claude",
                            "agent_status": "approval_needed",
                            "state_change_seq": 42,
                            "completion_seq": 41,
                            "focused": False,
                            "foreground_cwd": "/home/user/Work/indie_game",
                            "terminal_title_stripped": "修复逆脉数值",
                        }
                    ]
                }
            }
        return {"result": {}}

    # t0 首次发现阻塞：仅记录起点，不立即打扰
    r0 = scan_stuck_herdr_agents(
        sandbox_db,
        herdr_runner=_fake_herdr,
        pane_reader=lambda _p: "Allow Bash(pytest)? [y/n]",
        desktop_probe=lambda: {"available": True, "locked": False, "idle_sec": 120.0},
        now_override=t0,
    )
    assert r0["alerted"] == []

    # t0 + 6 分钟仍停在同一 seq：触发单次提醒
    r1 = scan_stuck_herdr_agents(
        sandbox_db,
        herdr_runner=_fake_herdr,
        pane_reader=lambda _p: "Allow Bash(pytest)? [y/n]",
        desktop_probe=lambda: {"available": True, "locked": False, "idle_sec": 480.0},
        now_override=t0 + timedelta(minutes=6),
    )
    assert r1["alerted"] == ["w4:p9X"]

    # t0 + 8 分钟同一 seq：绝不重复推送第二次！
    r2 = scan_stuck_herdr_agents(
        sandbox_db,
        herdr_runner=_fake_herdr,
        pane_reader=lambda _p: "Allow Bash(pytest)? [y/n]",
        desktop_probe=lambda: {"available": True, "locked": False, "idle_sec": 600.0},
        now_override=t0 + timedelta(minutes=8),
    )
    assert r2["alerted"] == []

    # 验证跨项目「十秒续工卡」
    mock_work = sandbox_db.paths.tmp_root / "mock_work"
    proj = mock_work / "indie_game"
    proj.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=str(proj), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(proj), check=True)
    subprocess.run(["git", "config", "user.name", "Tester"], cwd=str(proj), check=True)
    (proj / "HANDOFF.md").write_text("# Handoff\n\n## 下一步\n- 验证血河逆脉第 3 层反噬倍率\n", encoding="utf-8")
    subprocess.run(["git", "add", "HANDOFF.md"], cwd=str(proj), check=True)
    subprocess.run(["git", "commit", "-m", "init handoff"], cwd=str(proj), check=True, capture_output=True)
    (proj / "sim.ts").write_text("export const x = 1;\n", encoding="utf-8")

    card = build_ten_second_resume_card(sandbox_db, "独游项目", work_root=mock_work, herdr_runner=_fake_herdr)
    assert card["ok"] is True
    assert "sim.ts" in card["reply"]
    assert "验证血河逆脉第 3 层反噬倍率" in card["reply"]
    assert "派单 indie_game：" in card["reply"]
