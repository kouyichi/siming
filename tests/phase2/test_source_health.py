"""Phase 2 硬验收（S2-A）：数据源真健康度（三邮箱拆分判活、死人开关绑主 tick）、11-16/17 实盘垃圾短信零假单零假流水、全量正文 OTP 拦截与账本跨币种隔离。"""

from __future__ import annotations

import json
from datetime import timedelta

from siming.collectors.edge_sync import process_edge_records, sync_edge_node
from siming.collectors.omamail_bridge import _url_resolves_to_public_ip, collect_omamail
from siming.db import SimingDB, iso_now, now_local, parse_iso
from siming.ledger.reconcile import (
    generate_monthly_ledger_report,
    match_or_record_statement_row,
    match_or_record_wallet_row,
)
from siming.ledger.sms_parser import try_ingest_bank_sms


def test_real_nov16_17_sms_noise_zero_false_cases_and_zero_false_txns(sandbox_db: SimingDB) -> None:
    # 回放 2026-11-17 凌晨真实暴露的 3 条问题短信 + 纯发件人错误回溯载荷
    items = [
        {
            "id": 1,
            "source": "ios_sms",
            "text": "您尾号为1003的账户已在我行网上银行开通短信通知服务，该项服务收费标准为30元/年【中国银行】",
        },
        {
            "id": 2,
            "source": "ios_sms",
            "text": "【光大银行】您尾号1002的账户已成功开通短信通服务，手机号后四位0435，起点通知金额1。 如非您本人操作，请致电95595",
        },
        {
            "id": 3,
            "source": "ios_sms",
            "text": "您尾号1004的账户已开通短信服务，收费标准为人民币20.00元/月。[建设银行]",
        },
        {
            "id": 4,
            "source": "ios_sms_backfill",
            "text": "95595\n95566\n106980095533",
        },
    ]
    stats = process_edge_records(sandbox_db, items)
    assert stats["drop"] == 0
    assert stats["sms_bank"] == 0
    assert stats["sms_archived"] == 3
    assert stats["malformed_backfill"] == 1

    # 日级无时分真实停车消费短信，在任意时刻解析出的 occurred_at 绝不能晚于当前时刻（杜绝未来 12:00 倒挂）
    today = now_local().date()
    r_boc = try_ingest_bank_sms(
        sandbox_db,
        f"【中国银行】您的借记卡账户1003，于{today.month}月{today.day}日网上支付支取人民币88.00元。",
    )
    assert r_boc["matched"] is True

    conn = sandbox_db.connect()
    cases_cnt = conn.execute("SELECT COUNT(*) FROM cases").fetchone()[0]
    txns = conn.execute("SELECT occurred_at, amount_cents FROM ledger_txns").fetchall()
    conn.close()
    assert cases_cnt == 0
    assert len(txns) == 1
    assert txns[0]["amount_cents"] == 8800
    assert parse_iso(txns[0]["occurred_at"]) <= now_local() + timedelta(seconds=2)


def test_omamail_per_mailbox_health_long_body_otp_and_ssrf_guard(sandbox_db: SimingDB) -> None:
    fake_bin = sandbox_db.paths.tmp_root / "fake-om"
    fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_cache = sandbox_db.paths.tmp_root / "om-cache"
    fake_cache.mkdir(parents=True, exist_ok=True)

    # 即使 Gmail 有新鲜本地缓存，只要主动探测返回 oauth_expired，omamail_gmail 与聚合源必须诚实报错而非假绿！
    (fake_cache / "account-gmail.json").write_text(
        json.dumps(
            {
                "account": "user@gmail.com",
                "queries": {"q": {"at": now_local().timestamp() * 1000.0, "summaries": []}},
            }
        ),
        encoding="utf-8",
    )

    long_padding = "账单普通正文说明 " * 250  # >2000 字符，测试旧版 [:1000] 截断漏放 OTP 的修复

    def fake_runner(args: list[str], timeout: int = 15, domestic: bool = False):
        if args[:2] == ["accounts", "list"]:
            return {
                "accounts": [
                    {"id": "imap:user@163.com", "email": "user@163.com", "label": "163", "provider": "imap"},
                    {"id": "user@gmail.com", "email": "user@gmail.com", "label": "Gmail", "provider": "gmail"},
                ]
            }
        if args[:3] == ["list", "--account", "imap:user@163.com"]:
            return {
                "ok": True,
                "result": {
                    "messages": [
                        {
                            "id": "m-otp-tail",
                            "subject": "电子账单安全通知",
                            "snippet": "请查收本期说明",
                            "from": {"email": "service@bank.example.com", "name": "Bank"},
                        }
                    ]
                },
            }
        if args[:3] == ["list", "--account", "user@gmail.com"]:
            return {"ok": False, "error": {"code": "oauth_expired"}}
        if args[:3] == ["read", "--account", "imap:user@163.com"]:
            return {
                "ok": True,
                "result": {
                    "message": {
                        "nativeContent": {"body": {"text": f"{long_padding}\n您的动态验证码为 739184，请勿泄露"}},
                        "links": [],
                    }
                },
            }
        return {"ok": False, "error": {"code": "unexpected"}}

    res = collect_omamail(
        sandbox_db,
        cli_runner=fake_runner,
        om_bin_override=fake_bin,
        cache_dir_override=fake_cache,
    )
    assert res["ok"] is False
    assert res["otp_dropped"] == 1
    assert res["ingested"] == 0
    assert list(sandbox_db.paths.raw_dir.glob("*.enc")) == []

    conn = sandbox_db.connect()
    s_163 = conn.execute("SELECT auth_state, freshness FROM sources WHERE id='omamail_163'").fetchone()
    s_gm = conn.execute("SELECT auth_state, freshness FROM sources WHERE id='omamail_gmail'").fetchone()
    s_agg = conn.execute("SELECT auth_state, freshness FROM sources WHERE id='mail163'").fetchone()
    conn.close()
    assert s_163["auth_state"] == "OK" and s_163["freshness"] == "OK"
    assert s_gm["auth_state"] == "AUTH_EXPIRED" and s_gm["freshness"] == "ERROR"
    assert s_agg["auth_state"] == "DEGRADED"

    # SSRF 内网地址硬拦截校验
    assert _url_resolves_to_public_ip("http://127.0.0.1:8080/invoice.pdf") is False
    assert _url_resolves_to_public_ip("http://localhost/invoice.pdf") is False
    assert _url_resolves_to_public_ip("file:///etc/passwd") is False


def test_deadman_withholds_heartbeat_when_main_tick_is_stale(sandbox_db: SimingDB) -> None:
    stale_ts = iso_now(now_local() - timedelta(minutes=25))
    sandbox_db.set_meta("last_tick_ok_at", stale_ts)

    called_cmds: list[str] = []

    def fake_transport(args: list[str], _stdin=None):
        called_cmds.append(args[0])
        return {"ok": True, "items": []}

    res = sync_edge_node(sandbox_db, transport=fake_transport)
    assert res["ok"] is True
    assert res["tick_healthy"] is False
    assert "host-heartbeat" not in called_cmds
    assert "pull" in called_cmds


def test_ledger_currency_isolation_and_statement_no_duplicate_on_reconciled(sandbox_db: SimingDB) -> None:
    # 1. 同一张 1001 信用卡既有 USD 100.00 消费，又有 CNY 68.50 消费
    try_ingest_bank_sms(
        sandbox_db,
        "【光大银行】您尾号1001的信用卡于11月16日消费100.00美元。",
        occurred_at="2026-11-16T12:28:00+08:00",
        silent_backfill=True,
    )
    try_ingest_bank_sms(
        sandbox_db,
        "【光大银行】您尾号1001的信用卡于11月16日12:30消费人民币68.50元。",
        occurred_at="2026-11-16T12:30:00+08:00",
        silent_backfill=True,
    )

    # 2. 微信支付明细先把 CNY 68.50 配成「已对账」，且绝不会把 CNY 100.00 错配到 USD 100.00 上！
    w_cny = match_or_record_wallet_row(
        sandbox_db,
        occurred_at="2026-11-16T12:31:00+08:00",
        amount_cents=6850,
        card_last4="1001",
        wallet_alias="光大银行信用卡(1001)",
        merchant="山姆会员店",
        kind="消费",
        channel_order_id="WX-SAM-1",
    )
    assert w_cny["status"] == "已对账"

    w_100 = match_or_record_wallet_row(
        sandbox_db,
        occurred_at="2026-11-16T12:29:00+08:00",
        amount_cents=10000,
        card_last4="1001",
        wallet_alias="光大银行信用卡(1001)",
        merchant="盒马鲜生",
        kind="消费",
        channel_order_id="WX-HEMA-100",
    )
    # 不得与 USD 100.00 混配，应独立作为人民币流水入库
    conn = sandbox_db.connect()
    usd_row = conn.execute("SELECT status, merchant FROM ledger_txns WHERE currency='USD'").fetchone()
    conn.close()
    assert usd_row["merchant"] == ""
    assert w_100["txn_id"] != ""

    # 3. 随后银行月结单再次扫入同一笔 11-16 68.50 元交易 -> 必须命中已有「已对账」记录，绝不重复插第二条！
    st_res = match_or_record_statement_row(
        sandbox_db,
        occurred_date="2026-11-16",
        amount_cents=6850,
        account_alias="光大银行-1001",
        card_last4="1001",
        merchant="山姆会员店",
        kind="消费",
    )
    assert st_res["txn_id"] == w_cny["txn_id"]
    assert st_res.get("already_reconciled") is True

    # 再次重复导入同一行月结单 -> 幂等跳过
    st_dup = match_or_record_statement_row(
        sandbox_db,
        occurred_date="2026-11-16",
        amount_cents=6850,
        account_alias="光大银行-1001",
        card_last4="1001",
        merchant="山姆会员店",
        kind="消费",
    )
    assert st_dup.get("duplicate") is True

    # 4. 月报必须把人民币（¥168.50 = 68.50 + 100.00）与美元（$100.00）严格分列，绝不算成 ¥268.50！
    rep = generate_monthly_ledger_report(sandbox_db, "2026-11")
    assert "人民币净消费：¥168.50" in rep
    assert "外币流水（USD，独立统计不混入人民币）：净消费 $100.00" in rep
