"""Phase 2 深度后审回归集（覆盖实盘巡检 + Astra gpt-6-astra + Opus claude-opus-5-5 联合揪出的全部边界缺陷）。"""

from __future__ import annotations

import importlib.util
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from siming.approval import create_approval, reconcile_stuck_claiming_approvals
from siming.cn_calendar import try_parse_explicit_datetime
from siming.collectors.edge_sync import process_edge_records
from siming.crypto import sanitize_terminal_tail, write_secret_file
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local
from siming.evald import consume_pending_effects
from siming.ledger.reconcile import match_or_record_statement_row, match_or_record_wallet_row
from siming.ledger.reimburse_audit import ReimbursementLedgerIndex, audit_candidate_batch
from siming.ledger.sms_parser import try_ingest_bank_sms
from siming.morning import get_morning_full_reply
from siming.router import build_morning_brief, flush_morning_brief_if_due, handle_inbound_qq, send_qq_message
from siming.task_watch import poll_herdr_watches, watch_herdr_pane
from siming.waits import add_wait, trigger_wait_now


def _init_git_repo(repo: Path) -> None:
    repo.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "test@local"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "SimingTest"], cwd=str(repo), check=True)
    (repo / "hello.txt").write_text("base\n", encoding="utf-8")
    subprocess.run(["git", "add", "hello.txt"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=str(repo), check=True, capture_output=True)


def test_claiming_crash_recovery_and_committed_worktree_stage_apply(
    sandbox_db: SimingDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. C1：模拟进程在 CLAIMING 阶段被 SIGKILL（死 PID + 超时），watchdog 恢复为 FAILED_UNKNOWN 并入队告警
    ap = create_approval(
        sandbox_db,
        action="append_block_with_backup",
        target_id="S01",
        params={"path": str(sandbox_db.paths.tmp_root / "dummy.md"), "marker": "M", "block": "M"},
    )
    old_ts = iso_now(now_local() - timedelta(minutes=5))
    with sandbox_db.transaction() as c:
        c.execute(
            "UPDATE approvals SET status='CLAIMING', claimed_at=?, claimer_pid=99999998, claimer_boot_id='dead-boot' WHERE token=?",
            (old_ts, ap["token"]),
        )
    rec = reconcile_stuck_claiming_approvals(sandbox_db, max_age_sec=60)
    assert ap["token"] in rec
    conn = sandbox_db.connect()
    st = conn.execute("SELECT status FROM approvals WHERE token=?", (ap["token"],)).fetchone()["status"]
    conn.close()
    assert st == "FAILED_UNKNOWN"

    # 2. H2：Agent 在 stage worktree 里主动执行了 `git commit`，补丁仍相对 base_head 完整生成并成功合入主仓！
    repo = sandbox_db.paths.tmp_root / "commit-wt-repo"
    _init_git_repo(repo)
    monkeypatch.setenv(
        "SIMING_MOCK_WORKER_CMD",
        "git config user.email 'a@b' && git config user.name 'A' && printf 'committed-by-agent\\n' > hello.txt && git commit -am 'agent commit'",
    )
    w = add_wait(
        sandbox_db,
        when="1m",
        mode="stage",
        cwd=str(repo),
        task="修改并提交 hello.txt",
        done_check="grep -qx 'committed-by-agent' hello.txt",
        quote="改 hello.txt",
    )
    assert trigger_wait_now(sandbox_db, w["wait_id"], "test")
    consume_pending_effects(sandbox_db)
    consume_pending_effects(sandbox_db)

    conn = sandbox_db.connect()
    ob = conn.execute("SELECT sent_msg_id, text FROM outbox WHERE ref_id=? ORDER BY created_at DESC LIMIT 1", (w["wait_id"],)).fetchone()
    conn.close()
    assert ob is not None

    # M1：直接引用 stage 卡片回复“确认”，自动路由到绑定的 approval_token 合入主仓！
    r_confirm = handle_inbound_qq(
        sandbox_db,
        text="确认",
        reply_to_msg_id=ob["sent_msg_id"],
        reply_to_text=ob["text"],
    )
    assert r_confirm["handled"] is True
    assert "✅" in r_confirm["reply"]
    assert (repo / "hello.txt").read_text(encoding="utf-8") == "committed-by-agent\n"


def test_real_world_bank_sms_traps_and_ledger_pairing_integrity(sandbox_db: SimingDB) -> None:
    # 1. 实盘四大陷阱：中行 95566 签约热线、光大 (+8610) 二次风控提醒、额度不足未成功拒付 -> 全部跳过 0 入账
    assert try_ingest_bank_sms(
        sandbox_db,
        "您尾号为1003、9611的借记卡 2026年11月09日 21:49:07与网银在线成功签约银行卡快捷支付，详询95566【中国银行】",
    )["matched"] is False
    assert try_ingest_bank_sms(
        sandbox_db,
        "【光大银行】尊敬的张三先生，您卡号尾数1001的光大银行信用卡，于北京时间2026-11-06 09:57:24进行了100.00美元的交易，交易已成功。若交易非您本人操作，请致电(+8610)95595与我行联系。",
    )["matched"] is False
    assert try_ingest_bank_sms(
        sandbox_db,
        "【光大银行】您的卡号尾数1001的阳光标准金卡可用额度不足，消费100美元未成功，可用额度为18.58美元。详情请咨询95595。",
    )["matched"] is False

    # 2. 外币前缀 + 尾部“元”优先识别外币，不误记人民币
    fx_res = try_ingest_bank_sms(
        sandbox_db,
        "【光大银行】您尾号1001信用卡在OPENAI消费美元100.00元",
        occurred_at="2026-11-15T10:00:00+08:00",
    )
    assert fx_res["matched"] is True
    assert fx_res["currency"] == "USD"
    assert fx_res["amount_cents"] == 10000

    # 外币官方月结单到来时，核销该笔待核实外币短信为「已对账」，绝不重复插第 2 条！
    st_fx = match_or_record_statement_row(
        sandbox_db,
        occurred_date="2026-11-15",
        amount_cents=10000,
        account_alias="光大信用卡",
        card_last4="1001",
        merchant="OPENAI",
        kind="消费",
        currency="USD",
        row_index=0,
    )
    assert st_fx["txn_id"] == fx_res["txn_id"]
    assert st_fx["status"] == "已对账"

    # 3. 同一钱包连续两笔相隔 1 分钟、同额不同单号的消费绝不能互相合并成一笔！
    w1 = match_or_record_wallet_row(
        sandbox_db,
        occurred_at="2026-11-16T09:00:00+08:00",
        amount_cents=2500,
        card_last4="1003",
        wallet_alias="微信支付",
        merchant="Manner咖啡",
        kind="消费",
        channel_order_id="WX_A1",
    )
    w2 = match_or_record_wallet_row(
        sandbox_db,
        occurred_at="2026-11-16T09:01:00+08:00",
        amount_cents=2500,
        card_last4="1003",
        wallet_alias="微信支付",
        merchant="瑞幸咖啡",
        kind="消费",
        channel_order_id="WX_A2",
    )
    assert w1["txn_id"] != w2["txn_id"]

    # 4. 同日同额但不同正文的回溯短信不被实时短信摘要误吞
    s_live = try_ingest_bank_sms(
        sandbox_db,
        "您的借记卡账户1003，于11月16日网上支付支取人民币50.00元,交易后余额1000.00【中国银行】",
        source="ios_sms",
        occurred_at="2026-11-16T12:00:00+08:00",
    )
    s_back = try_ingest_bank_sms(
        sandbox_db,
        "2026年11月16日 18:30|||您的借记卡账户1003，于11月16日网上支付支取人民币50.00元,交易后余额950.00【中国银行】",
        source="ios_sms_backfill",
        silent_backfill=True,
    )
    assert s_live["matched"] is True and not s_live.get("duplicate")
    assert s_back["matched"] is True and not s_back.get("duplicate")


def test_edge_tombstone_encryption_and_deadletter_isolation(sandbox_db: SimingDB, monkeypatch: pytest.MonkeyPatch) -> None:
    edge_py = Path(__file__).resolve().parents[2] / "edge" / "siming_edge.py"
    spec = importlib.util.spec_from_file_location("siming_edge_audit", edge_py)
    assert spec and spec.loader
    edge_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(edge_mod)
    edge_dir = sandbox_db.paths.tmp_root / "edge-tomb"
    store = edge_mod.EdgeStore(edge_dir)

    # 1. withdraw 先于乱序滞后的 put 到达时，墓碑阻止旧预排复活；且落盘文本加密
    store.withdraw_outbox("W77")
    store.put_outbox("W77", "敏感预排提醒原文", "2026-11-20T09:00:00+08:00", "2026-11-17T04:00:00+08:00")
    store.put_outbox("W78", "正常加密预排", "2026-11-20T09:00:00+08:00", "2026-11-17T04:00:00+08:00")

    conn = store.connect()
    r77 = conn.execute("SELECT status FROM prescheduled_outbox WHERE ref_id='W77'").fetchone()
    r78 = conn.execute("SELECT status, text FROM prescheduled_outbox WHERE ref_id='W78'").fetchone()
    conn.close()
    assert r77["status"] == "WITHDRAWN"
    assert r78["status"] == "PENDING"
    assert r78["text"].startswith("enc:")
    assert "正常加密预排" not in r78["text"]

    # 2. 单条损坏记录进 deadletter，不阻塞同批后续正常短信入账
    import siming.collectors.edge_sync as es_mod

    orig_fn = es_mod._process_single_edge_record

    def flaky_proc(db, rec, today, stats):
        if rec.get("id") == 1:
            raise RuntimeError("poison record")
        return orig_fn(db, rec, today, stats)

    monkeypatch.setattr(es_mod, "_process_single_edge_record", flaky_proc)
    stats = process_edge_records(
        sandbox_db,
        [
            {"id": 1, "source": "ios_drop", "text": "boom"},
            {"id": 2, "source": "ios_sms", "text": "【建设银行】您账户1004于11月17日09:00消费人民币18.00元"},
        ],
    )
    assert stats["deadletter"] == 1
    assert stats["sms_bank"] == 1


def test_router_casual_reply_morning_immutability_herdr_continue_and_strict_audit(
    sandbox_db: SimingDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. M2：已完成单据收到“谢谢，今天辛苦了”绝不误重开！
    ts = iso_now()
    with sandbox_db.transaction() as c:
        c.execute(
            "INSERT INTO cases (case_id, title, status, closed_at, created_at, updated_at) VALUES ('S88', '测试取件', '完成', ?, ?, ?)",
            (ts, ts, ts),
        )
    s_msg = send_qq_message(sandbox_db, "[S88] 测试已结单", ref_id="S88", bypass_quiet=True)
    r_thx = handle_inbound_qq(
        sandbox_db,
        text="谢谢，今天辛苦了",
        reply_to_msg_id=s_msg["msg_id"],
        reply_to_text="[S88] 测试已结单",
    )
    assert r_thx["handled"] is False
    conn = sandbox_db.connect()
    assert conn.execute("SELECT status FROM cases WHERE case_id='S88'").fetchone()["status"] == "完成"
    conn.close()

    # 2. M13：08:15 投递后跑 `siming today` 不覆盖含夜间暂存原文的晨札全文
    with sandbox_db.transaction() as c:
        c.execute(
            "INSERT INTO outbox (id, ref_id, rule_id, text, send_at, status, channel, created_at) VALUES ('MQ-99', 'W01', 'wait_remind', '夜间重要详情段落XYZ', ?, 'MORNING_QUEUE', 'qqbot', ?)",
            (ts, ts),
        )
    monkeypatch.setattr("siming.router.load_policy", lambda _p=None: {
        "windows": {"morning_brief": "00:00", "pickup_batch": "18:00", "night_batch_run": "01:30"},
        "quiet_hours": {"start": "23:30", "end": "00:00"},
        "guardrails": {"approval_ttl_minutes": 30, "ended_subscription_resume_days": 14},
        "channels": {"hermes_bin": "/nonexistent", "qq_target": "qqbot:1", "feishu_fallback": ""},
        "coding_plan": {"herdr_bin": "/nonexistent"},
    })
    d_res = flush_morning_brief_if_due(sandbox_db)
    assert d_res and d_res.get("delivered") is True
    # 随后主动跑 build_morning_brief（此时 MORNING_QUEUE 已标 SENT）
    _ = build_morning_brief(sandbox_db, full=False)
    assert "夜间重要详情段落XYZ" in get_morning_full_reply(sandbox_db)

    # 3. M10：herdr watch 进入 BLOCKED 提醒一次后不终止监听，继续跑完仍推 DONE
    h_state = {"status": "working", "seq": 1}
    fake_h = lambda args: {
        "result": {
            "panes" if args == ["pane", "list"] else "agents": [
                {
                    "pane_id": "w1:p1",
                    "tab_id": "w1:t1",
                    "label": "训练",
                    "terminal_id": "t_1",
                    "agent_status": h_state["status"],
                    "completion_seq": h_state["seq"],
                    "agent_session": {"value": "s1"},
                }
            ]
        }
    }
    w_h = watch_herdr_pane(sandbox_db, "w1:p1", herdr_runner=fake_h)
    h_state["status"] = "blocked"
    assert poll_herdr_watches(sandbox_db, herdr_runner=fake_h, pane_reader=lambda _: "Approve?")["notified"] == [
        {"run_id": w_h["run_id"], "status": "BLOCKED"}
    ]
    # 持续处于同一 blocked 轮次不重复刷屏
    assert poll_herdr_watches(sandbox_db, herdr_runner=fake_h, pane_reader=lambda _: "Approve?")["notified"] == []
    # 用户在 herdr 放行后完成 -> 继续收到 DONE！
    h_state["status"] = "idle"
    h_state["seq"] = 2
    assert poll_herdr_watches(sandbox_db, herdr_runner=fake_h, pane_reader=lambda _: "Done!")["notified"] == [
        {"run_id": w_h["run_id"], "status": "DONE"}
    ]

    # 4. M14 / Astra #28：中文时刻与非法公历日期校验
    ref = datetime(2026, 11, 17, 12, 0, tzinfo=SHANGHAI_TZ)
    dt_dawn, sn = try_parse_explicit_datetime("后天凌晨3点提醒我", ref_dt=ref, paths=sandbox_db.paths)  # type: ignore[misc]
    assert (dt_dawn.day, dt_dawn.hour, sn) == (19, 3, True)
    dt_noon, _ = try_parse_explicit_datetime("明天中午一点提醒我", ref_dt=ref, paths=sandbox_db.paths)  # type: ignore[misc]
    assert (dt_noon.day, dt_noon.hour) == (18, 13)
    with pytest.raises(ValueError):
        try_parse_explicit_datetime("2026年2月30日9点", ref_dt=ref, paths=sandbox_db.paths)

    # 5. Astra #10：先对完整行脱敏再截断，覆盖 JSON 引号与 --password 参数及伪造 apply 口令
    secret_tok = "SECRET_LONG_VALUE_ABCDEF1234567890"
    write_secret_file(sandbox_db.paths.secrets_dir / "custom-tok", secret_tok)
    long_line = ("x" * 145) + secret_tok + ' {"password": "my_json_pwd"} --token cli_tok_999 apply A88'
    scrubbed = sanitize_terminal_tail(long_line, paths=sandbox_db.paths)
    assert secret_tok[:10] not in scrubbed
    assert "my_json_pwd" not in scrubbed
    assert "cli_tok_999" not in scrubbed
    assert "apply A88" not in scrubbed

    # 6. M7 / Astra #18-#19：报销审计严拒空包、负额退款、还款差额与不同门户号 ±3s 误合并
    idx = ReimbursementLedgerIndex()
    idx.upsert_record(portal_no="2805-0102", currency="USD", amount_cents=10000, utc_epoch=1790000000)
    idx.upsert_record(portal_no="2903-8722", currency="USD", amount_cents=10000, utc_epoch=1790000003)
    assert len(idx.payments) == 2

    empty_aud = audit_candidate_batch(sandbox_db, [], write_manifest=False)
    assert empty_aud["ok"] is False
    neg_aud = audit_candidate_batch(
        sandbox_db,
        [{"portal_no": "2888-9999", "currency": "USD", "amount_cents": -10000, "utc_epoch": 1790000100}],
        write_manifest=False,
    )
    assert neg_aud["ok"] is False
    mismatch_aud = audit_candidate_batch(
        sandbox_db,
        [{"portal_no": "2888-9999", "currency": "USD", "amount_cents": 10000, "utc_epoch": 1790000100}],
        repayment_usd=4000.0,
        write_manifest=False,
    )
    assert mismatch_aud["ok"] is False
