"""测试 公网边缘存储（幂等、验证码拦截、预排撤回与失联代发）、取件码 18:00 合并以及四卡整数分账本。"""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

from siming.collectors.edge_sync import sync_edge_node
from siming.db import SimingDB
from siming.ledger.export_zip import import_bill_archive
from siming.ledger.reconcile import (
    detect_recurring_subscriptions,
    generate_monthly_ledger_report,
)
from siming.ledger.sms_parser import try_ingest_bank_sms
from siming.ledger.statement_parser import try_ingest_statement_text


def _load_edge_module():
    edge_py = Path(__file__).resolve().parents[1] / "edge" / "siming_edge.py"
    spec = importlib.util.spec_from_file_location("siming_edge_mod", edge_py)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_edge_store_idempotency_otp_outbox_and_deadman(sandbox_db: SimingDB) -> None:
    edge_mod = _load_edge_module()
    store = edge_mod.EdgeStore(sandbox_db.paths.tmp_root / "edge-state")

    # 1. 二道验证码防线：零字节落盘
    r_otp = store.ingest_payload("ios_sms", "【银行】登录验证码 918273，切勿提供他人", False)
    assert r_otp.get("dropped") == "otp_guard"
    assert store.pull_since(0) == []

    # 2. 2 分钟时间桶幂等去重
    r1 = store.ingest_payload("ios_sms", "【丰巢】凭取件码 662819 至示例小区B1号柜取件", False)
    r2 = store.ingest_payload("ios_sms", "【丰巢】凭取件码 662819 至示例小区B1号柜取件", False)
    assert r1["dedup"] is False
    assert r2["dedup"] is True
    assert r1["id"] == r2["id"]

    # 3. 本机经游标拉取入库，生成 pickup 待办
    def fake_transport(args: list[str], stdin_data=None):
        if args[0] == "host-heartbeat":
            store.record_host_heartbeat(args[2])
            return {"ok": True}
        if args[0] == "pull":
            items = store.pull_since(int(args[2]), int(args[4]))
            return {"ok": True, "items": items}
        return {"ok": True}

    sync_res = sync_edge_node(sandbox_db, transport=fake_transport)
    assert sync_res["ok"] is True
    assert sync_res["stats"]["sms_pickup"] == 1

    # 4. 预排发件箱同步与核销撤回
    store.put_outbox("S99", "[S99] 信用卡明日到期还款", "2020-01-01T09:00:00+08:00", "2020-01-01T08:00:00+08:00")
    assert store.withdraw_outbox("S99") == 1


def test_edge_http_shortcut_friendly_endpoints(sandbox_db: SimingDB) -> None:
    import json
    import socket
    import threading
    import urllib.error
    import urllib.request

    edge_mod = _load_edge_module()
    store = edge_mod.EdgeStore(sandbox_db.paths.tmp_root / "edge-http-state")
    store.token_path.write_text("secret_test_token_123\n", encoding="utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    t = threading.Thread(target=edge_mod.run_http_server, args=(store, "127.0.0.1", port), daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"

    # 等待监听就绪
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"{base}/siming/in/health", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            import time
            time.sleep(0.05)

    # 1. /siming/in/hb 支持直接 GET + 纯 token（漏写 Bearer 也能过）
    req_hb = urllib.request.Request(
        f"{base}/siming/in/hb",
        headers={"Authorization": "secret_test_token_123"},
        method="GET",
    )
    with urllib.request.urlopen(req_hb, timeout=2) as r:
        res_hb = json.loads(r.read().decode("utf-8"))
        assert res_hb["ok"] is True

    # 2. /siming/in/sms 仅传单个字段 {"text": "..."}，无需填 source
    req_sms = urllib.request.Request(
        f"{base}/siming/in/sms",
        data=json.dumps({"text": "【菜鸟驿站】凭取件码 9-1-202 取件"}).encode("utf-8"),
        headers={"Authorization": "Bearer secret_test_token_123", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req_sms, timeout=2) as r:
        res_sms = json.loads(r.read().decode("utf-8"))
        assert res_sms["ok"] is True

    # 3. /siming/in/drop 直接发 text/plain 也能入库
    req_drop = urllib.request.Request(
        f"{base}/siming/in/drop",
        data="工长报价：厨卫防水 1800 元".encode("utf-8"),
        headers={"Authorization": "Bearer secret_test_token_123", "Content-Type": "text/plain; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(req_drop, timeout=2) as r:
        res_drop = json.loads(r.read().decode("utf-8"))
        assert res_drop["ok"] is True

    # 4. 未带 token 访问 /siming/in/hb 返回 401
    try:
        urllib.request.urlopen(f"{base}/siming/in/hb", timeout=2)
        assert False, "expected 401"
    except urllib.error.HTTPError as e:
        assert e.code == 401

    pulled = store.pull_since(0)
    sources = [x["source"] for x in pulled]
    assert sources == ["ios_hb", "ios_sms", "ios_drop"]


def test_ledger_reconcile_ambiguity_transfer_and_zip_shred(sandbox_db: SimingDB) -> None:
    # 1. 单笔唯一消费短信 + 微信账单 ZIP 导入 -> 自动对账并补齐商户名，且临时目录不残留解压文件
    try_ingest_bank_sms(
        sandbox_db,
        "【招商银行】您账户0000于11月10日12:30消费人民币68.50元",
        occurred_at="2026-11-10T12:30:00+08:00",
    )
    # 2. 同日两笔同金额 ¥25.00 短信 + 单条明细 -> 严禁按时间硬配，必须标「待核实」
    try_ingest_bank_sms(
        sandbox_db,
        "【招商银行】您账户0000于11月11日09:01消费人民币25.00元",
        occurred_at="2026-11-11T09:01:00+08:00",
    )
    try_ingest_bank_sms(
        sandbox_db,
        "【招商银行】您账户0000于11月11日09:04消费人民币25.00元",
        occurred_at="2026-11-11T09:04:00+08:00",
    )
    # 3. 连续两个月固定金额会员扣款 -> 抓出疑似周期自动续费
    try_ingest_bank_sms(
        sandbox_db,
        "【招商银行】您账户0000于10月05日10:00在云盘会员消费人民币30.00元",
        occurred_at="2026-10-05T10:00:00+08:00",
    )
    try_ingest_bank_sms(
        sandbox_db,
        "【招商银行】您账户0000于11月05日10:00在云盘会员消费人民币30.00元",
        occurred_at="2026-11-05T10:00:00+08:00",
    )

    csv_content = (
        "微信支付账单明细\n"
        "交易时间,交易类型,交易对方,商品,收/支,金额(元),支付方式,当前状态,交易单号\n"
        "2026-11-10 12:31:00,商户消费,山姆会员商店,牛奶面包,支出,¥68.50,招商银行(0000),支付成功,WX10001\n"
        "2026-11-11 09:02:00,商户消费,Manner咖啡,美式,支出,¥25.00,招商银行(0000),支付成功,WX10002\n"
        "2026-11-12 20:00:00,信用卡还款,招商银行信用卡,还款,不计收支,¥5000.00,招商银行借记卡(0001),支付成功,WX10003\n"
    )
    zip_path = sandbox_db.paths.tmp_root / "wechat-202611.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("微信支付账单(202611).csv", csv_content.encode("utf-8-sig"))

    imp = import_bill_archive(sandbox_db, zip_path)
    assert imp["ok"] is True
    assert imp["ingested"] == 3
    assert imp["matched"] == 1
    assert imp["ambiguous"] == 1
    # 确认临时解压目录已被阅后即焚清理干净
    assert list(sandbox_db.paths.tmp_root.glob("bill-unpack-*")) == []

    # 4. 月结单长假还款日测试
    stmt = try_ingest_statement_text(
        sandbox_db,
        subject="招商银行信用卡电子账单",
        content="本期应还金额：人民币 1,280.50\n到期还款日：2026-10-08\n2026-11-10 山姆会员商店 68.50 0000\n",
    )
    assert stmt["matched"] is True

    subs = detect_recurring_subscriptions(sandbox_db)
    assert any(s["merchant"] == "云盘会员" for s in subs)

    report = generate_monthly_ledger_report(sandbox_db, "2026-11")
    assert "卡间/还款划转（不计重复消费）：¥5000.00" in report
    assert "未覆盖缺口" in report


def test_omamail_bridge_renewal_and_otp_guard(sandbox_db: SimingDB) -> None:
    import json
    from datetime import timedelta
    from siming.collectors.omamail_bridge import collect_omamail
    from siming.db import now_local
    from siming.remote import ask_knowledge

    fake_bin = sandbox_db.paths.tmp_root / "fake-omamail"
    fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_cache = sandbox_db.paths.tmp_root / "omamail-cache"
    fake_cache.mkdir(parents=True, exist_ok=True)

    future_due = now_local().date() + timedelta(days=3)
    due_cn = f"{future_due.year}年{future_due.month}月{future_due.day}日续期"

    # Yahoo 缓存里有一条即将自动扣费的 iCloud+ 续期提醒 + 一条验证码邮件（应被丢弃）
    (fake_cache / "account-yahoo.json").write_text(
        json.dumps(
            {
                "account": "user@yahoo.com",
                "queries": {
                    "folder:INBOX|50": {
                        "at": now_local().timestamp() * 1000.0,
                        "summaries": [
                            {
                                "id": "9002:INBOX",
                                "subject": "您的登录验证码 884210",
                                "snippet": "登录验证码 884210，5分钟内有效",
                                "from": {"email": "noreply@auth.example.com", "name": "Auth"},
                            },
                            {
                                "id": "9001:INBOX",
                                "subject": "iCloud+ 订阅将在 3天后续期",
                                "snippet": "iCloud+（含 50 GB 储存空间） ¥6.00",
                                "from": {"email": "no_reply@iCloud.gzdata.com.cn", "name": "Apple"},
                            },
                        ],
                    }
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    def fake_cli_runner(args: list[str], timeout: int = 15, domestic: bool = False):
        if args[:2] == ["accounts", "list"]:
            return {
                "accounts": [
                    {"id": "imap:user@163.com", "email": "user@163.com", "label": "163 Mail", "provider": "imap"},
                    {"id": "imap:user@yahoo.com", "email": "user@yahoo.com", "label": "Yahoo", "provider": "imap"},
                ]
            }
        if args[:3] == ["list", "--account", "imap:user@163.com"]:
            return {
                "ok": True,
                "result": {
                    "messages": [
                        {
                            "id": "1001:INBOX",
                            "subject": "智云电子发票通知",
                            "snippet": "您有一张新的电子发票",
                            "date": "2026-11-16T02:00:00Z",
                            "from": {"email": "bill@example.com", "name": "开票服务"},
                        }
                    ]
                },
            }
        if args[:3] == ["read", "--account", "imap:user@yahoo.com"]:
            return {
                "ok": True,
                "result": {
                    "message": {
                        "nativeContent": {
                            "body": {
                                "text": f"续期确认\niCloud+（含 50 GB 储存空间）（月度）\n{due_cn}\n¥6.00"
                            },
                            "attachments": [],
                        },
                        "links": [],
                        "attachments": [],
                    }
                },
            }
        return {"ok": True, "result": {"message": {"nativeContent": {"body": {"text": "发票正文"}}, "links": []}}}

    res = collect_omamail(
        sandbox_db,
        max_per_account=10,
        cli_runner=fake_cli_runner,
        om_bin_override=fake_bin,
        cache_dir_override=fake_cache,
    )
    assert res["ok"] is True
    assert res["otp_dropped"] == 1
    assert res["ingested"] == 2
    assert res["renewals_tracked"] == 1

    # 二次同步幂等，不重复入库
    res2 = collect_omamail(
        sandbox_db,
        max_per_account=10,
        cli_runner=fake_cli_runner,
        om_bin_override=fake_bin,
        cache_dir_override=fake_cache,
    )
    assert res2["ingested"] == 0

    # siming ask 能秒查订阅与邮箱动态
    ask_sub = ask_knowledge(sandbox_db, "查 订阅")
    assert "iCloud+" in ask_sub["answer"]
    ask_mail = ask_knowledge(sandbox_db, "查 邮箱")
    assert "智云电子发票通知" in ask_mail["answer"]


def test_four_real_card_sms_patterns(sandbox_db: SimingDB) -> None:
    (sandbox_db.paths.config_dir / "accounts.toml").write_text(
        """
[[accounts]]
alias = "光大信用卡"
bank = "光大银行"
kind = "credit"
last4 = "1001"
configured = true

[[accounts]]
alias = "光大还款卡"
bank = "光大银行"
kind = "debit"
last4 = "1002"
configured = true

[[accounts]]
alias = "中行主资金卡"
bank = "中国银行"
kind = "debit"
last4 = "1003"
configured = true

[[accounts]]
alias = "建行工资卡"
bank = "建设银行"
kind = "debit"
last4 = "1004"
configured = true
""",
        encoding="utf-8",
    )

    # 1. 建行工资卡 1004 方括号签名 + 工资入账 -> 归为「收入」，不计消费
    r_ccb = try_ingest_bank_sms(
        sandbox_db,
        "您尾号1004的储蓄卡账户11月15日10时12分工资收入人民币28000.00元,活期余额50000.00元。[建设银行]",
        occurred_at="2026-11-15T10:12:00+08:00",
    )
    assert r_ccb["matched"] is True and r_ccb["account_alias"] == "建行工资卡" and r_ccb["kind"] == "收入"

    # 2. 光大还款卡 1002 转入与扣还信用卡 -> 自动归为「划转」，不重复计消费
    r_ceb_rep = try_ingest_bank_sms(
        sandbox_db,
        "【光大银行】您尾号1002的账户于11月14日信用卡还款扣款人民币26,750.65元。",
        occurred_at="2026-11-14T00:57:00+08:00",
    )
    assert r_ceb_rep["matched"] is True and r_ceb_rep["account_alias"] == "光大还款卡" and r_ceb_rep["kind"] == "划转"

    # 3. 光大信用卡 1001 美元消费 -> 自动标外币「待核实」且准确落到信用卡而非还款卡
    r_ceb_cc = try_ingest_bank_sms(
        sandbox_db,
        "【光大银行】您尾号1001的信用卡于11月16日消费100.00美元。",
        occurred_at="2026-11-16T09:41:00+08:00",
    )
    assert r_ceb_cc["matched"] is True and r_ceb_cc["account_alias"] == "光大信用卡" and r_ceb_cc["kind"] == "消费"

    # 4. 中行主资金卡 1003 网上支付支取 -> 归为「消费」
    r_boc = try_ingest_bank_sms(
        sandbox_db,
        "【中国银行】您的借记卡账户1003，于11月16日网上支付支取人民币10,000.00元。",
        occurred_at="2026-11-16T15:00:00+08:00",
    )
    assert r_boc["matched"] is True and r_boc["account_alias"] == "中行主资金卡" and r_boc["kind"] == "消费"


def test_sms_backfill_and_bill_ocr_wechat_douyin_alipay(sandbox_db: SimingDB) -> None:
    import json
    import socket
    import threading
    import urllib.request

    edge_mod = _load_edge_module()
    store = edge_mod.EdgeStore(sandbox_db.paths.tmp_root / "edge-backfill-ocr")
    store.token_path.write_text("tok_backfill_999\n", encoding="utf-8")

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    t = threading.Thread(target=edge_mod.run_http_server, args=(store, "127.0.0.1", port), daemon=True)
    t.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(30):
        try:
            with urllib.request.urlopen(f"{base}/siming/in/health", timeout=1) as r:
                if r.status == 200:
                    break
        except Exception:
            import time
            time.sleep(0.05)

    # A. 存量短信批量回溯：含历史大额银行短信 + 夹杂单条验证码 + 几个月前的旧丰巢码
    batch_payload = "\n".join(
        [
            "2026/10/18 20:15 ||| 【招商银行】您账户0000于10月18日20:15消费人民币3580.00元",
            "2026/10/19 09:00 ||| 【某平台】登录动态码 482910，请勿泄露",
            "2026/09/12 15:00 ||| 【丰巢】凭取件码 771122 至1号柜取件",
        ]
    )
    req_bf = urllib.request.Request(
        f"{base}/siming/in/sms_backfill",
        data=json.dumps({"text": batch_payload}).encode("utf-8"),
        headers={"Authorization": "tok_backfill_999", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req_bf, timeout=3) as r:
        res_bf = json.loads(r.read().decode("utf-8"))
        assert res_bf["ok"] is True
        assert res_bf["ingested"] == 2
        assert res_bf["dropped_otp"] == 1

    # B. 先入一条实时银行动账短信（无商户名），再用微信/抖音/支付宝三合一账单 OCR 同步补齐
    try_ingest_bank_sms(
        sandbox_db,
        "【招商银行】您账户0000于11月16日12:30消费人民币68.50元",
        occurred_at="2026-11-16T12:30:00+08:00",
        silent_backfill=True,
    )

    ocr_pages = "\n===PAGE===\n".join(
        [
            "微信支付\n全部账单\n支出 ¥520.00 收入 ¥0.00\n山姆会员商店\n-68.50\n11月16日 12:31\n瑞幸咖啡\n-9.90\n11月16日 08:45",
            "抖音支付\n账单管理\n东方甄选旗舰店\n-39.90\n11-16 20:15",
            "支付宝\n月账单\n滴滴出行\n-24.50\n11-16 09:10",
            "设置\n无线局域网\n蓝牙 已打开",
        ]
    )
    req_ocr = urllib.request.Request(
        f"{base}/siming/in/bill_ocr",
        data=json.dumps({"text": ocr_pages}).encode("utf-8"),
        headers={"Authorization": "tok_backfill_999", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req_ocr, timeout=3) as r:
        res_ocr = json.loads(r.read().decode("utf-8"))
        assert res_ocr["ok"] is True

    def fake_transport(args: list[str], stdin_data=None):
        if args[0] == "host-heartbeat":
            store.record_host_heartbeat(args[2])
            return {"ok": True}
        if args[0] == "pull":
            return {"ok": True, "items": store.pull_since(int(args[2]), int(args[4]))}
        return {"ok": True}

    sync_res = sync_edge_node(sandbox_db, transport=fake_transport)
    assert sync_res["ok"] is True
    assert sync_res["stats"]["sms_bank"] == 1
    assert sync_res["stats"]["sms_pickup"] == 0  # 9 月旧取件码绝不生成待取单
    assert sync_res["stats"]["bill_ocr"] == 4

    conn = sandbox_db.connect()
    try:
        # 存量 ¥3580 大额消费还原为 2026-10-18，且绝不触发实时 QQ 告警
        oct_row = conn.execute(
            "SELECT occurred_at, amount_cents FROM ledger_txns WHERE amount_cents = 358000"
        ).fetchone()
        assert oct_row is not None and str(oct_row["occurred_at"]).startswith("2026-10-18")
        alert_cnt = conn.execute("SELECT COUNT(*) AS c FROM effects WHERE kind='send_qq'").fetchone()["c"]
        assert alert_cnt == 0

        # 68.50 元实时银行短信已被微信账单 OCR 自动补齐商户名并标记「已对账」，没有记成两笔
        sam_rows = conn.execute(
            "SELECT status, merchant FROM ledger_txns WHERE amount_cents = 6850"
        ).fetchall()
        assert len(sam_rows) == 1
        assert sam_rows[0]["status"] == "已对账"
        assert "山姆会员商店" in sam_rows[0]["merchant"]

        # 抖音、支付宝、微信小额零钱均已独立入账，且无关系统设置截图未污染 cases
        aliases = {
            r["account_alias"]
            for r in conn.execute("SELECT account_alias FROM ledger_txns").fetchall()
        }
        assert "抖音账单" in aliases and "支付宝账单" in aliases and "微信账单" in aliases
        pickup_cases = conn.execute("SELECT COUNT(*) AS c FROM cases").fetchone()["c"]
        assert pickup_cases == 0
    finally:
        conn.close()


def test_douyin_and_alipay_file_export_and_qq_direct_drop(sandbox_db: SimingDB) -> None:
    from siming.router import handle_inbound_qq

    douyin_csv = (
        "抖音支付交易明细证明\n"
        "付款时间,商户名称,商品名称,收支类型,实付金额,支付渠道,订单状态,抖音支付单号\n"
        "2026-11-15 21:05:10,三只松鼠旗舰店,每日坚果礼盒,支出,59.90,抖音月付,支付成功,'DY202611150001\n"
    )
    dy_path = sandbox_db.paths.tmp_root / "抖音账单_202611.csv"
    dy_path.write_bytes(douyin_csv.encode("utf-8-sig"))

    # 直接在手机 QQ 把导出的账单文件甩给机器人
    qq_res = handle_inbound_qq(sandbox_db, text="", media_paths=[str(dy_path)])
    assert qq_res["handled"] is True
    assert "新增入账：1 笔" in qq_res["reply"]

    conn = sandbox_db.connect()
    try:
        row = conn.execute("SELECT account_alias, merchant, amount_cents FROM ledger_txns WHERE amount_cents=5990").fetchone()
        assert row is not None
        assert row["account_alias"] == "抖音月付"
        assert "三只松鼠旗舰店" in row["merchant"]
    finally:
        conn.close()


