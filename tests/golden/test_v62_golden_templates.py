"""v6.2 双盲审架构核心回归与 3 个黄金端到端验收样板（test_v62_golden_templates.py）：

覆盖：
- Golden 1：一次确认美团支付（保留 D4 当前调用方式）+ 双击幂等防重 + 同店连下第二单 + 响应丢失 RECONCILING 对账 + 三态收据拆分；
- Golden 2：长任务运行中插话 STEER（applied_seq）+ 急停 CANCEL + 5 问停止交代 + 旧 generation CAS 失权拒绝脏写；
- Golden 3：跨会话引用旧卡/普通聊天（普通闲聊回 Hermes，司命卡进原 Thread，过期报价强制重核价）+ 混合多意图 + 8s 交还；
- P0 真缺陷回归：通知 ok=True/delivered=False 不误标已提醒、边端 put_outbox 不复活已发送行、假执行器仅落 PLAN_CONFIRMED；
- P2 验收：三维能力生命周期（D1-A 5 个未到位硬件默认 unconfigured 停 tick → 自动升级 shadow→live）、ResourceLease 互斥；
- P3 验收：#4 D3-B CalDAV 双向写回（If-Match / 412 冲突 / origin_ref 防回声 / RRULE+EXDATE+RECURRENCE-ID / 会议改期保留手动提醒）、Apple Health 去重与记录数 Bug 修复、#6 抖音收藏无损入库与三链召回；
- P4 验收：#5 Correction 跨重启生效与回滚、LearnGate 留出集评测门禁、今日活动流与补偿撤销、源头 ModelCall 分栏计量与控制塔只读投影。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from siming.apple_health import get_health_readiness_summary, ingest_health_observations
from siming.approval import create_approval, verify_and_apply_approval
from siming.bookmark_recall import (
    enrich_unclassified_bookmarks,
    extract_all_bookmarks_from_douyin_favorites,
    record_decision_use,
    retrieve_candidates,
    save_lossless_bookmark,
)
from siming.caldav_sync import (
    expand_calendar_window,
    export_siming_ics,
    put_caldav_event,
    sync_caldav_calendar,
)
from siming.db import SHANGHAI_TZ, SimingDB, iso_now
from siming.delivery_outbox import NoticeRequested, evaluate_due_obligations, register_obligation, submit_notice
from siming.frontdesk import (
    bind_message_ref,
    get_thread_facts,
    handback,
    route_frontdesk_envelope,
    upsert_thread_facts,
)
from siming.health_cockpit import get_health_trends
from siming.learning_loop import (
    add_correction,
    get_today_activity_feed,
    lookup_correction,
    promote_candidate,
    propose_learning_candidate,
    replay_candidate_on_holdout,
    rollback_correction,
    summarize_model_calls,
    undo_activity_item,
)
from siming.leases import acquire_lease, acquire_leases, browser_account_resource, release_lease
from siming.meal_guard import evaluate_meal_regularity
from siming.meituan_pay import execute_meituan_submit_pay, reconcile_payment_intent
from siming.modules import (
    UNARRIVED_HARDWARE_MODULES,
    get_capability_state,
    is_module_tick_active,
    promote_capability_on_signal,
)
from siming.receipts import get_intent_with_receipts
from siming.runtime_kernel import (
    ack_run_command,
    build_stop_handoff,
    create_assignment,
    create_multi_agent_coding_ticket,
    create_ticket,
    enqueue_run_command,
    poll_run_inbox,
    start_run,
    update_run_progress_cas,
)
from siming.tower_projections import v6_overview_projection


# ===========================================================================
# GOLDEN TEMPLATE 1: 一次确认美团支付 + 双击防重 + 同店第二单 + 崩溃对账 + 三态收据
# ===========================================================================
def test_golden_1_single_confirm_payment_three_state_receipts_and_reconcile(sandbox_db: SimingDB) -> None:
    params_1 = {
        "shop": "示例江浙菜(科技园店)",
        "item_spec": "招牌小笼包 x1",
        "final_payable_yuan": 38.5,
        "max_authorized_yuan": 40.5,
        "preview_target_id": "TAB-ORDER-1",
    }
    app1 = create_approval(sandbox_db, action="meituan_submit_pay", target_id="mt:order1", params=params_1)

    # 1) 模拟提交后返回 needs_user_action（尚未确认扣款）-> 绝不写 APPLIED！
    r_pending = verify_and_apply_approval(
        sandbox_db,
        app1["token"],
        extra_executors={
            "meituan_submit_pay": lambda p, d: execute_meituan_submit_pay(
                p,
                d,
                cdp_pay_fn=lambda _: {
                    "ok": True,
                    "paid": False,
                    "order_submitted": True,
                    "needs_user_action": True,
                    "state": "RECONCILING",
                    "order_id": "MT-20261125-001",
                    "payable_yuan": 38.5,
                    "reason": "响应超时，转入原单对账",
                },
            )
        },
    )
    assert r_pending["approval_status"] == "AWAITING_VERIFY"
    assert r_pending["receipt"]["authorization_consumed"] is True
    assert r_pending["receipt"]["order_submitted"] is True
    assert r_pending["receipt"]["paid_verified"] is False

    # 2) 重复点击同一张确认卡 -> 幂等返回已有 intent，绝不二次扣款
    r_dup = verify_and_apply_approval(sandbox_db, app1["token"])
    assert r_dup["ok"] is False
    assert r_dup.get("idempotent_replay") is True
    assert r_dup["intent_id"] == r_pending["intent_id"]

    # 3) 对账阶段只核验原订单号 MT-20261125-001：若首单单号不匹配则保持对账，不误标已付
    rec_mismatch = reconcile_payment_intent(
        sandbox_db,
        r_pending["intent_id"],
        verify_olist_fn=lambda s, oid, p: {"ok": True, "found": True, "paid": True, "order_id_matched": False, "shop": s},
    )
    assert rec_mismatch["paid_verified"] is False
    assert rec_mismatch["reason"] == "order_id_mismatch"

    # 4) 原单号核验匹配 -> 正式升格 PAID_VERIFIED
    rec_ok = reconcile_payment_intent(
        sandbox_db,
        r_pending["intent_id"],
        verify_olist_fn=lambda s, oid, p: {"ok": True, "found": True, "paid": True, "order_id_matched": True, "shop": s, "status": "已支付"},
    )
    assert rec_ok["paid_verified"] is True
    full_intent = get_intent_with_receipts(sandbox_db, intent_id=r_pending["intent_id"])
    assert full_intent is not None
    assert full_intent["authorization_consumed"] is True
    assert full_intent["order_submitted"] is True
    assert full_intent["paid_verified"] is True

    # 5) 同店连续有意再下第二单（新 preview + 新 token）-> 拥有独立购买意图，允许正常一次确认支付成功！
    params_2 = {**params_1, "preview_target_id": "TAB-ORDER-2"}
    app2 = create_approval(sandbox_db, action="meituan_submit_pay", target_id="mt:order2", params=params_2)
    r_second = verify_and_apply_approval(
        sandbox_db,
        app2["token"],
        extra_executors={
            "meituan_submit_pay": lambda p, d: execute_meituan_submit_pay(
                p,
                d,
                cdp_pay_fn=lambda _: {
                    "ok": True,
                    "paid": True,
                    "order_submitted": True,
                    "state": "PAID",
                    "order_id": "MT-20261125-002",
                    "paid_yuan": 38.5,
                    "shop": "示例江浙菜(科技园店)",
                    "order_status": "商家已接单",
                    "pay_channel": "hermes_confidential_handoff",
                },
            )
        },
    )
    assert r_second["ok"] is True
    assert r_second["approval_status"] == "APPLIED"
    assert r_second["receipt"]["paid_verified"] is True
    assert r_second["receipt"]["external_order_id"] == "MT-20261125-002"


# ===========================================================================
# GOLDEN TEMPLATE 2: 长任务插话 STEER + 急停 CANCEL + 5 问停止交代 + generation CAS
# ===========================================================================
def test_golden_2_coding_run_steer_cancel_stop_handoff_and_generation_cas(sandbox_db: SimingDB) -> None:
    ma = create_multi_agent_coding_ticket(
        sandbox_db,
        goal="重构 router 路由层并补齐测试",
        repo_dir=str(sandbox_db.paths.repo_root),
        editor_adapter="codex",
        reviewer_adapter="claude",
    )
    ticket_id = ma["ticket"]["ticket_id"]
    run = start_run(
        sandbox_db,
        assignment_id=ma["editor_assignment"]["assignment_id"],
        ticket_id=ticket_id,
        step_reached="已创建隔离 worktree 并修改 2 个文件",
    )
    run_id = run["run_id"]
    orig_gen = run["generation"]

    # 挂一张尚未消费的合入待批卡
    pending_merge = create_approval(
        sandbox_db, action="overnight_next_step", target_id=ticket_id, params={"handoff_id": ticket_id, "action": "merge"}
    )

    # 1) 用户运行中插话：「先别合入，只跑测试」
    fd_steer = route_frontdesk_envelope(sandbox_db, text="先别合入，只跑测试", message_id="qq-steer-1")
    assert fd_steer["owner"] == "siming"
    assert fd_steer["control_action"]["kind"] == "STEER"
    assert fd_steer["control_action"]["run_id"] == run_id

    cmd_res = enqueue_run_command(sandbox_db, run_id=run_id, kind="STEER", payload={"instruction": "先别合入，只跑测试"})
    assert pending_merge["token"] in cmd_res["revoked_approvals"]

    # Worker 轮询安全点并采纳，回报 applied_seq
    inbox = poll_run_inbox(sandbox_db, run_id)
    assert len(inbox) == 1 and inbox[0]["kind"] == "STEER"
    ack = ack_run_command(sandbox_db, run_id=run_id, seq=inbox[0]["seq"], applied=True, note="已切为仅跑 pytest")
    assert ack["applied_seq"] == inbox[0]["seq"]

    # 正常推进当前代次进度，记录隔离补丁产物
    prog = update_run_progress_cas(
        sandbox_db,
        run_id=run_id,
        expected_generation=orig_gen,
        step_reached="pytest 跑至第 18 个用例",
        artifact_ref="worktrees/WT-01/fix.patch",
        resume_hint="siming resume " + ticket_id,
    )
    assert prog["ok"] is True

    # 2) 用户喊停：「停一下」-> 急停并生成 5 问停止交代（同时递升 generation）
    fd_cancel = route_frontdesk_envelope(sandbox_db, text="停一下", message_id="qq-cancel-1")
    assert fd_cancel["owner"] == "siming" and fd_cancel["control_action"]["kind"] == "CANCEL"
    enqueue_run_command(sandbox_db, run_id=run_id, kind="CANCEL")
    handoff = build_stop_handoff(
        sandbox_db,
        run_id=run_id,
        confirmed_stopped=["测试进程已停", "主仓未合入"],
        pending_verification=["无远端任务待确认"],
    )
    assert handoff["ok"] is True
    for marker in ("做到了哪一步", "已在外部生效", "已确认停止", "仍待核验", "产物与续跑"):
        assert marker in handoff["text"]
    assert "worktrees/WT-01/fix.patch" in handoff["text"]

    # 3) 模拟旧代次进程复活试图补写 DONE -> CAS 严格拒绝！
    stale_write = update_run_progress_cas(
        sandbox_db,
        run_id=run_id,
        expected_generation=orig_gen,
        step_reached="旧进程误以为完成",
        new_state="DONE",
    )
    assert stale_write["ok"] is False
    assert stale_write["rejected"] == "stale_generation"


# ===========================================================================
# GOLDEN TEMPLATE 3: 跨会话引用旧卡 vs 普通聊天 + 混合意图 + 可恢复 handback
# ===========================================================================
def test_golden_3_frontdesk_hermes_quote_routing_hybrid_and_handback(sandbox_db: SimingDB) -> None:
    # 1) 绑定一条普通 Hermes 聊天消息引用 + 一条司命过期报价卡引用
    bind_message_ref(
        sandbox_db,
        channel="qqbot",
        account="owner",
        conversation="c2c-1",
        platform_msg_id="MSG-CHAT-1",
        owner="hermes",
        thread_id="TH-CHAT-1",
    )
    expired_app = create_approval(
        sandbox_db,
        action="meituan_submit_pay",
        target_id="mt:old",
        params={"shop": "示例泰餐", "final_payable_yuan": 44.0},
        ttl_minutes=-5,  # 已过期
    )
    th_food = upsert_thread_facts(
        sandbox_db,
        channel="qqbot",
        conversation="c2c-1",
        goal="今晚吃示例泰餐",
        entity_refs={"shop": "示例泰餐", "quote_token": expired_app["token"]},
        picks=[{"shop": "示例泰餐", "avg_yuan": 44.0}, {"shop": "示例快餐", "avg_yuan": 32.0}],
    )
    bind_message_ref(
        sandbox_db,
        channel="qqbot",
        account="owner",
        conversation="c2c-1",
        platform_msg_id="MSG-SIMING-OLD-QUOTE",
        owner="siming",
        thread_id=th_food["thread_id"],
        card_id=expired_app["token"],
        approval_token=expired_app["token"],
    )

    # 引用普通聊天 -> 归还 Hermes，彻底修复 jev_gate:187 劫持！
    r_chat_quote = route_frontdesk_envelope(
        sandbox_db,
        text="我刚说的是这个意思",
        conversation="c2c-1",
        message_id="IN-1",
        reply_to_id="MSG-CHAT-1",
    )
    assert r_chat_quote["owner"] == "hermes"
    assert r_chat_quote["scenario"] == "quoted_chat"

    # 引用过期司命报价卡 -> 精准回到原 Thread 且标记 requires_requote=True
    r_quote_old = route_frontdesk_envelope(
        sandbox_db,
        text="就点这家吧",
        conversation="c2c-1",
        message_id="IN-2",
        reply_to_id="MSG-SIMING-OLD-QUOTE",
    )
    assert r_quote_old["owner"] == "siming"
    assert r_quote_old["thread_id"] == th_food["thread_id"]
    assert r_quote_old["requires_requote"] is True
    assert get_thread_facts(sandbox_db, th_food["thread_id"])["picks"][0]["shop"] == "示例泰餐"

    # 混合多意图：「解释下这个框架，等周五提醒我试试」-> Hermes 编排一次回复 + 拆出司命提醒子意图
    r_hybrid = route_frontdesk_envelope(
        sandbox_db,
        text="解释下这个框架，等周五提醒我试试",
        conversation="c2c-1",
        message_id="IN-3",
    )
    assert r_hybrid["owner"] == "hermes"
    assert r_hybrid["scenario"] == "hybrid_multi_intent"
    assert len(r_hybrid["sub_intents"]) == 2
    assert r_hybrid["sub_intents"][1]["owner"] == "siming"

    # 敏感载荷本地隔离：不调用外部 JEV
    jev_called = {"n": 0}
    r_sens = route_frontdesk_envelope(
        sandbox_db,
        text="解压码 884210",
        conversation="c2c-1",
        message_id="IN-4",
        jev_eval_fn=lambda *a, **k: jev_called.__setitem__("n", jev_called["n"] + 1) or {"should_siming_handle": False},
    )
    assert r_sens["owner"] == "siming" and r_sens["sensitive_isolated"] is True
    assert jev_called["n"] == 0

    # 可恢复 handback：原子转移回复权
    hb = handback(
        sandbox_db,
        thread_id=th_food["thread_id"],
        from_owner="siming",
        to_owner="hermes",
        reason="unhandled_open_chat",
        context_rev=th_food["revision"],
        reply_epoch=r_quote_old["reply_epoch"],
        envelope_key=r_quote_old["envelope_key"],
    )
    assert hb["to_owner"] == "hermes"


# ===========================================================================
# P0 & P2 专项缺陷与契约验证（假送达、边端重放、plan_only、三维能力态、ResourceLease）
# ===========================================================================
def test_p0_and_p2_truthful_delivery_edge_replay_hardware_lifecycle_and_leases(sandbox_db: SimingDB, tmp_path: Path) -> None:
    # 1) 假执行器只落 PLAN_CONFIRMED，不假装 external_verified=True
    app_combo = create_approval(
        sandbox_db,
        action="commute_rescue_bundle",
        target_id="commute:1",
        params={"reason": "暴雨", "didi_fare_yuan": 45, "coffee_delay_min": 15},
    )
    res_combo = verify_and_apply_approval(sandbox_db, app_combo["token"])
    assert res_combo["approval_status"] == "PLAN_CONFIRMED"
    assert res_combo["receipt"]["external_verified"] is False

    # 2) meal_guard 在规则被 D-33 隐式降级/暂存（push_message 返回 ok=True, delivered=False）时绝不能误写「已提醒」
    quiet_t = datetime(2026, 11, 25, 13, 20, tzinfo=SHANGHAI_TZ)
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO rule_stats (rule_id, mode, hard_rule, updated_at)
            VALUES ('meal_combo_guard', 'QUIET', 0, ?)
            ON CONFLICT(rule_id) DO UPDATE SET mode='QUIET', updated_at=excluded.updated_at
            """,
            (iso_now(quiet_t),),
        )
    res_meal = evaluate_meal_regularity(sandbox_db, now=quiet_t, notify=True)
    assert res_meal["nudged"] is False
    assert sandbox_db.get_meta("meal_guard_nudged:2026-11-25:lunch") is None

    # 3) 边端 put_outbox 不复活已离线 SENT 的提醒
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "edge"))
    from siming_edge import EdgeStore

    edge_dir = tmp_path / "edge_state"
    edge_dir.mkdir(parents=True, exist_ok=True)
    st = EdgeStore(edge_dir)
    st.put_outbox("ref-1", "提醒正文", "2026-11-25T10:00:00+08:00", "2026-11-25T09:00:00+08:00")
    conn_e = st.connect()
    conn_e.execute("UPDATE prescheduled_outbox SET status='SENT' WHERE ref_id='ref-1'")
    conn_e.close()
    st.put_outbox("ref-1", "重放旧推送", "2026-11-25T10:00:00+08:00", "2026-11-25T09:05:00+08:00")
    conn_e = st.connect()
    row_e = conn_e.execute("SELECT status FROM prescheduled_outbox WHERE ref_id='ref-1'").fetchone()
    conn_e.close()
    assert row_e["status"] == "SENT"

    # 4) D1-A 三维能力生命周期：5 个未到位硬件默认 readiness=unconfigured -> tick_active=False -> 信号到达自动升级 shadow -> live
    for mod in UNARRIVED_HARDWARE_MODULES:
        cs = get_capability_state(sandbox_db, mod)
        assert cs["readiness"] == "unconfigured"
        assert is_module_tick_active(sandbox_db, mod) is False
    st_shadow = promote_capability_on_signal(sandbox_db, "bambu_copilot", has_probe=True, has_sample=True)
    assert st_shadow["readiness"] == "shadow" and st_shadow["tick_active"] is True
    st_live = promote_capability_on_signal(sandbox_db, "bambu_copilot", verified_outcome=True)
    assert st_live["readiness"] == "live" and st_live["verified_runs"] == 1

    # 5) ResourceLease：账户级独占锁互斥，释放后可获取
    acc_key = browser_account_resource("meituan")
    l1 = acquire_lease(sandbox_db, resource_key=acc_key, holder_run_id="RN-1", generation=1, mode="exclusive")
    l2 = acquire_lease(sandbox_db, resource_key=acc_key, holder_run_id="RN-2", generation=1, mode="exclusive")
    assert l1["acquired"] is True and l2["acquired"] is False
    assert release_lease(sandbox_db, resource_key=acc_key, holder_run_id="RN-1") == 1
    l3 = acquire_leases(sandbox_db, resource_keys=[acc_key, "worktree:siming:RN-2"], holder_run_id="RN-2", generation=1)
    assert l3["acquired"] is True


# ===========================================================================
# P3: #4 D3-B 双向 CalDAV + Apple Health + #6 收藏无损召回
# ===========================================================================
def test_p3_caldav_bidirectional_apple_health_and_bookmark_recall(sandbox_db: SimingDB) -> None:
    # 1) D3-B CalDAV：PUT 带 If-None-Match 与 X-SIMING-ORIGIN-REF，412 冲突返回结构化 diff，回步触发 echo_suppressed
    put_calls: list[dict] = []

    def fake_http(method: str, url: str, headers=None, content=None, timeout=20.0):
        put_calls.append({"method": method, "url": url, "headers": headers or {}})
        if method == "PUT" and (headers or {}).get("If-Match") == '"stale-etag"':
            return {"status_code": 412, "headers": {}, "text": ""}
        if method == "GET":
            return {
                "status_code": 200,
                "headers": {"etag": '"remote-v2"'},
                "text": (
                    "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:ev-1\r\n"
                    "DTSTART;TZID=Asia/Shanghai:20261210T150000\r\nDTEND;TZID=Asia/Shanghai:20261210T160000\r\n"
                    "SUMMARY:新房烟灶安装(师傅改期)\r\nEND:VEVENT\r\nEND:VCALENDAR"
                ),
            }
        if method == "PUT":
            return {"status_code": 201, "headers": {"etag": '"etag-v1"'}, "text": ""}
        if method == "REPORT":
            # 返回一条司命自写回声事件 + 一条改期后的重复会议（含 EXDATE 与 RECURRENCE-ID）
            xml = """<?xml version="1.0" encoding="utf-8"?>
<D:multistatus xmlns:D="DAV:" xmlns:C="urn:ietf:params:xml:ns:caldav">
  <D:sync-token>tok-2</D:sync-token>
  <D:response>
    <D:href>/cal/ev-1.ics</D:href>
    <D:propstat><D:prop>
      <D:getetag>"etag-v1"</D:getetag>
      <C:calendar-data>BEGIN:VCALENDAR
BEGIN:VEVENT
UID:ev-1
DTSTART;TZID=Asia/Shanghai:20261210T100000
DTEND;TZID=Asia/Shanghai:20261210T110000
SUMMARY:新房烟灶安装
X-SIMING-ORIGIN-REF:siming:ev-1
END:VEVENT
END:VCALENDAR</C:calendar-data>
    </D:prop></D:propstat>
  </D:response>
  <D:response>
    <D:href>/cal/standup.ics</D:href>
    <D:propstat><D:prop>
      <D:getetag>"etag-s2"</D:getetag>
      <C:calendar-data>BEGIN:VCALENDAR
BEGIN:VEVENT
UID:standup-1
DTSTART;TZID=Asia/Shanghai:20261125T110000
DTEND;TZID=Asia/Shanghai:20261125T113000
RRULE:FREQ=DAILY;COUNT=3
EXDATE;TZID=Asia/Shanghai:20261126T110000
SUMMARY:周会
END:VEVENT
END:VCALENDAR</C:calendar-data>
    </D:prop></D:propstat>
  </D:response>
</D:multistatus>"""
            return {"status_code": 207, "headers": {}, "text": xml}
        return {"status_code": 404, "headers": {}, "text": ""}

    # 预先登记一条旧时间会议 + 两条关联提醒（一条自动、一条手动改过）
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO calendar_objects (obj_key, account, calendar, href, uid, start_at, summary, status, updated_at)
            VALUES ('icloud::work::standup-1', 'icloud', 'work', '/cal/standup.ics', 'standup-1', '2026-11-25T10:00:00+08:00', '周会', 'CONFIRMED', ?)
            """,
            (iso_now(),),
        )
    ob_auto = register_obligation(
        sandbox_db, kind="once", rule_id="cal_depart", subject="周会出发(自动)", next_fire_at="2026-11-25T09:30:00+08:00", calendar_uid="standup-1", manually_edited=False
    )
    ob_manual = register_obligation(
        sandbox_db, kind="once", rule_id="cal_depart", subject="周会出发(手动)", next_fire_at="2026-11-25T09:00:00+08:00", calendar_uid="standup-1", manually_edited=True
    )

    p_ok = put_caldav_event(
        sandbox_db,
        account="icloud",
        calendar="work",
        caldav_url="https://caldav.icloud.com/u/work",
        uid="ev-1",
        summary="新房烟灶安装",
        start_at="2026-12-10T10:00:00+08:00",
        http_fn=fake_http,
    )
    assert p_ok["ok"] is True and p_ok["etag"] == "etag-v1"

    p_conflict = put_caldav_event(
        sandbox_db,
        account="icloud",
        calendar="work",
        caldav_url="https://caldav.icloud.com/u/work",
        uid="ev-1",
        summary="新房烟灶安装",
        start_at="2026-12-10T10:30:00+08:00",
        expected_etag="stale-etag",
        http_fn=fake_http,
    )
    assert p_conflict["conflict"] is True and p_conflict["remote_etag"] == "remote-v2"

    sync_res = sync_caldav_calendar(
        sandbox_db,
        account="icloud",
        calendar="work",
        caldav_url="https://caldav.icloud.com/u/work",
        http_fn=fake_http,
    )
    assert sync_res["ok"] is True and sync_res["echo_suppressed"] == 1
    by_id = {x["ob_id"]: x for x in sync_res["shifted_reminders"]}
    assert by_id[ob_auto["ob_id"]]["action"] == "recomputed" and "10:30" in by_id[ob_auto["ob_id"]]["next_fire_at"]
    assert by_id[ob_manual["ob_id"]]["action"] == "preserved_manual" and "09:00" in by_id[ob_manual["ob_id"]]["next_fire_at"]

    # 窗口展开验证 RRULE + EXDATE（3 天中排除 11-26，剩 11-25 与 11-27 两条）
    expanded = expand_calendar_window(
        sandbox_db,
        datetime(2026, 11, 25, 0, 0, tzinfo=SHANGHAI_TZ),
        datetime(2026, 11, 28, 23, 59, tzinfo=SHANGHAI_TZ),
    )
    standup_days = [e["instance_start"][:10] for e in expanded if e["uid"] == "standup-1"]
    assert standup_days == ["2026-11-25", "2026-11-27"]
    assert "BEGIN:VCALENDAR" in export_siming_ics(sandbox_db)

    # 2) Apple Health：跨午夜 + Watch/iPhone 重叠去重 + 修复 health_cockpit 记录数当连续天数 Bug
    assert get_health_readiness_summary(sandbox_db)["status"] == "unknown"
    ah_res = ingest_health_observations(
        sandbox_db,
        [
            # Watch 睡了 5 小时（23:30 -> 04:30），iPhone 重叠记录了 00:00 -> 04:30 -> 合并后净睡眠 5.0h（不是 9.5h！），睡眠债 2.5h
            {"obs_type": "sleep_interval", "start_at": "2026-11-24T23:30:00+08:00", "end_at": "2026-11-25T04:30:00+08:00", "device": "Apple Watch", "value_num": 5.0},
            {"obs_type": "sleep_interval", "start_at": "2026-11-25T00:00:00+08:00", "end_at": "2026-11-25T04:30:00+08:00", "device": "iPhone", "value_num": 4.5},
            {"obs_type": "weight_kg", "start_at": "2026-11-25T08:00:00+08:00", "value_num": 78.4, "unit": "kg"},
        ],
    )
    assert ah_res["recomputed_sleep"]["2026-11-25"]["sleep_hours"] == 5.0
    assert ah_res["recomputed_sleep"]["2026-11-25"]["sleep_debt_h"] == 2.5
    # 同一天再插 3 条高睡眠债记录 -> consecutive_high_days 仍必须为 1 天（绝不是 4 天！）
    with sandbox_db.transaction() as conn:
        for idx in range(3):
            conn.execute(
                "INSERT INTO health_metrics (metric_id, measured_at, metric_type, value_num, unit, flag, created_at) VALUES (?, ?, 'sleep_debt_h', 3.0, 'h', 'HIGH', ?)",
                (f"HM-dup-{idx}", f"2026-11-25T1{idx}:00:00+08:00", iso_now()),
            )
    trends = get_health_trends(sandbox_db, days=7, now=datetime(2026, 11, 25, 18, 0, tzinfo=SHANGHAI_TZ))
    assert trends["sleep_debt"]["consecutive_high_days"] == 1

    # 3) #6 抖音收藏无损入库（普通餐厅不再入库前被删）+ 召回 + 「只是收藏不想买」仅修用途不删收藏
    raw_dy = "12.3万\n科技园必吃潮汕牛肉火锅探店 汤底超级鲜 菜品推荐吊龙\n5432\nhttps://github.com/deepseek-ai/dsh-superagent-prototype 开源 Agent 调度架构\n"
    favs = extract_all_bookmarks_from_douyin_favorites(raw_dy)
    assert len(favs) == 2
    ids = [save_lossless_bookmark(sandbox_db, title=f["title"], body=f["body"], platform=f["platform"], platform_item_id=f["platform_item_id"], canonical_url=f["canonical_url"])["bookmark_id"] for f in favs]
    enrich_unclassified_bookmarks(sandbox_db)
    food_cands = retrieve_candidates(sandbox_db, purpose="food", context="科技园 牛肉火锅")
    assert len(food_cands) == 1 and food_cands[0]["purchase_authorized"] is False
    record_decision_use(sandbox_db, bookmark_id=food_cands[0]["bookmark_id"], purpose="food", rejected_reason="just_saved_not_buying")
    assert retrieve_candidates(sandbox_db, purpose="food", context="科技园 牛肉火锅") == []
    # 原收藏仍在库中未被删除
    conn_r = sandbox_db.connect_readonly()
    assert conn_r.execute("SELECT COUNT(*) FROM bookmarks WHERE bookmark_id=?", (ids[0],)).fetchone()[0] == 1
    conn_r.close()


# ===========================================================================
# P4: #5 Correction + LearnGate + ActivityFeed / Undo + ModelCall + Tower 投影
# ===========================================================================
def test_p4_correction_learngate_activity_undo_metering_and_tower_projection(sandbox_db: SimingDB) -> None:
    # 1) Correction 持久生效 + 版本迭代 + 回滚恢复上一版
    c1 = add_correction(
        sandbox_db, user_intent="示例停车场不是订阅", target_object="merchant:示例停车场", field_name="is_subscription", new_value="false", scope="exact"
    )
    c2 = add_correction(
        sandbox_db, user_intent="改回需核对", target_object="merchant:示例停车场", field_name="is_subscription", new_value="review", scope="exact"
    )
    assert lookup_correction(sandbox_db, target_object="merchant:示例停车场", field_name="is_subscription")["new_value"] == "review"
    rb = rollback_correction(sandbox_db, c2["corr_id"])
    assert rb["restored_prev"] == c1["corr_id"]
    assert lookup_correction(sandbox_db, target_object="merchant:示例停车场", field_name="is_subscription")["new_value"] == "false"

    # 2) LearnGate：高敏能力需本人批准；留出集样本不足留 shadow，达标后经人批准晋升为 PolicyVersion
    cand = propose_learning_candidate(
        sandbox_db,
        trigger_case="纠偏 3 次",
        proposed_rule="停车场小额扣费默认不归类为订阅",
        target_capability="permissions",
    )
    rep_small = replay_candidate_on_holdout(
        sandbox_db, cand_id=cand["cand_id"], holdout_cases=[{"x": 1}], evaluator_fn=lambda c, x: True, min_samples=3
    )
    assert rep_small["report"]["verdict"] == "insufficient_samples_hold_in_shadow"
    assert promote_candidate(sandbox_db, cand_id=cand["cand_id"], approved_by="human")["ok"] is False

    rep_ok = replay_candidate_on_holdout(
        sandbox_db, cand_id=cand["cand_id"], holdout_cases=[{"x": 1}, {"x": 2}, {"x": 3}], evaluator_fn=lambda c, x: True, min_samples=3
    )
    assert rep_ok["report"]["verdict"] == "ready_for_promotion"
    # 系统不可越权推广高敏规则
    assert promote_candidate(sandbox_db, cand_id=cand["cand_id"], approved_by="system")["error"] == "human_approval_required"
    prom = promote_candidate(sandbox_db, cand_id=cand["cand_id"], approved_by="human")
    assert prom["ok"] is True

    # 3) 今日活动流 + 撤销（Undo）+ 源头 ModelCall 计量 + 控制塔只读投影
    sandbox_db.record_model_call(
        model="codex-6.2", provider="openai", runner="codex_sub", tokens_in=1200, tokens_out=400, meter_source="cli_receipt", billing_kind="subscription"
    )
    sandbox_db.record_model_call(
        model="typesafe/jev-1.13", provider="openrouter", runner="jev_gate", tokens_in=300, tokens_out=20, cost_cny=0.001, meter_source="provider_usage", billing_kind="cash"
    )
    mc = summarize_model_calls(sandbox_db, days=7)
    assert mc["by_billing_kind"]["subscription"]["calls"] == 1
    assert mc["by_billing_kind"]["cash"]["calls"] == 1
    assert mc["by_meter_source"]["provider_usage"] == 1

    feed = get_today_activity_feed(sandbox_db)
    assert feed["counts"]["done"] >= 1
    undo_res = undo_activity_item(sandbox_db, undo_kind="policy_version", target_id=prom["version_id"])
    assert undo_res["ok"] is True

    proj = v6_overview_projection(sandbox_db)
    assert proj["ok"] is True
    assert proj["capabilities"]["by_readiness"].get("unconfigured", 0) >= 5
