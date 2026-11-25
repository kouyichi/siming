"""Phase 2 硬验收（S2-D）：报销游标卡触发、Stripe 门户 2xxx ↔ Gmail 1xxx 双号同笔防重、UTC±6s 经济付款身份防重及真实第 11/12 批只读回放。"""

from __future__ import annotations

from pathlib import Path

from siming.db import SimingDB
from siming.ledger.reimburse_audit import (
    DEFAULT_REIMBURSE_RECORDS_ROOT,
    audit_candidate_batch,
    get_reimbursement_cursor_status,
)
from siming.ledger.sms_parser import try_ingest_bank_sms


def test_repayment_sms_triggers_single_reimbursement_cursor_case(sandbox_db: SimingDB) -> None:
    # 准备合成历史记录目录与共享记忆游标卡
    rec_root = sandbox_db.paths.tmp_root / "reimb-records"
    b12 = rec_root / "批次-20261116-CW08-FYYS-202611160001"
    b12.mkdir(parents=True)
    (b12 / "发票明细.csv").write_text(
        "门户收据号,账单号,付款时间UTC,付款时间北京,USD,票夹uuid,附件\n"
        "2675-3693,TESTINV0-0576,2026-11-11 09:41:14,11-11 17:41:14,100.00,u1,a.pdf\n",
        encoding="utf-8",
    )
    (b12 / "README.md").write_text(
        "- 不在本单：门户 2805-0102 / 2903-8722（$200）。\n",
        encoding="utf-8",
    )
    (sandbox_db.paths.shared_memory_dir / "nous-reimbursement-batches.md").write_text(
        "下一批 Nous 从门户 2805-0102（UTC 11-13 20:27）接；11-11 15:15 的 ¥323.94（$48.42）还款还没用。\n",
        encoding="utf-8",
    )
    import os

    # 在沙盒 accounts.toml 登记尾号 1002 为专用还款卡，触发报销游标刷新
    (sandbox_db.paths.config_dir / "accounts.toml").write_text(
        '[[accounts]]\nalias = "还款借记卡"\nbank = "光大银行"\nkind = "debit"\nlast4 = "1002"\nrepay_card = true\nreimburse_cursor = true\nconfigured = true\n',
        encoding="utf-8",
    )
    os.environ["SIMING_REIMBURSE_RECORDS_DIR"] = str(rec_root)
    try:
        r_sms = try_ingest_bank_sms(
            sandbox_db,
            "【光大银行】您尾号1002的账户于11月16日信用卡还款扣款人民币26,800.00元。",
            occurred_at="2026-11-16T20:00:00+08:00",
        )
        assert r_sms["matched"] is True

        conn = sandbox_db.connect()
        c_rows = conn.execute("SELECT * FROM cases WHERE category='reimburse_cursor'").fetchall()
        conn.close()
        assert len(c_rows) == 1
        assert "2805-0102" in c_rows[0]["next_step"]
    finally:
        os.environ.pop("SIMING_REIMBURSE_RECORDS_DIR", None)


def test_real_batches_11_and_12_twin_alias_and_time_window_dedup(sandbox_db: SimingDB) -> None:
    # 使用完全自包含的沙盒批次夹具，不依赖宿主机真实个人文档目录
    rec_root = sandbox_db.paths.tmp_root / "reimburse_batches_synthetic"
    b11 = rec_root / "批次-20261110-CW08-FYYS-202611100001"
    b12 = rec_root / "批次-20261116-CW08-FYYS-202611160001"
    b11.mkdir(parents=True, exist_ok=True)
    b12.mkdir(parents=True, exist_ok=True)

    (b11 / "发票明细.csv").write_text(
        "序号,收据号,门户收据号,账单号,币种,原币金额,付款时间UTC\n"
        "1,1816-1420,,,USD,100.00,2026-11-05 10:00:00\n",
        encoding="utf-8",
    )
    (b12 / "发票明细.csv").write_text(
        "序号,收据号,门户收据号,账单号,币种,原币金额,付款时间UTC\n"
        "1,,2675-3693,,USD,100.00,2026-11-12 10:00:00\n"
        "2,,2675-3694,,USD,100.00,2026-11-12 11:00:00\n"
        "3,,2781-5174,,USD,100.00,2026-11-13 16:56:16\n"
        "4,26000000000000000001,,,CNY,5000.00,2026-11-14 08:00:00\n",
        encoding="utf-8",
    )
    pair_dir = rec_root / "Nous" / "门户收据配对-20261112"
    pair_dir.mkdir(parents=True, exist_ok=True)
    (pair_dir / "配对表.csv").write_text(
        "门户收据号,Gmail收据号,账单号,票夹uuid,金额USD,付款时间UTC(门户),CW08,CW01,状态\n"
        "2675-3693,1496-2036,,u1,100.00,2026-11-12 10:00:00,CW08-FYYS-202611160001,,已提\n"
        "2675-3694,1968-1605,,u2,100.00,2026-11-12 11:00:00,CW08-FYYS-202611160001,,已提\n",
        encoding="utf-8",
    )
    real_mem_md = sandbox_db.paths.shared_memory_dir / "nous-reimbursement-batches.md"
    real_mem_md.write_text(
        "下一批 Nous 从门户 2805-0102（UTC 11-13 20:27）接；\n"
        "- 1496-2036 <-> 2675-3693 (CW08-FYYS-202611160001)\n"
        "- 1968-1605 <-> 2675-3694 (CW08-FYYS-202611160001)\n",
        encoding="utf-8",
    )

    st = get_reimbursement_cursor_status(
        sandbox_db,
        records_root=rec_root,
        shared_memory_md=real_mem_md,
    )
    assert st["latest_batch_id"] >= "CW08-FYYS-202611160001"
    assert bool(st["next_portal_cursor"])

    candidates = [
        {"label": "Gmail残留旧票1", "gmail_no": "1496-2036"},
        {"label": "Gmail残留旧票2", "gmail_no": "1968-1605"},
        {"label": "第11批已用票", "gmail_no": "1816-1420"},
        {"label": "第12批DeepSeek普票", "invoice_no": "26000000000000000001", "currency": "CNY", "amount_cents": 500000},
        {
            "label": "未配对表但撞第12批末尾时间窗(差4秒)",
            "gmail_no": "1999-9999",
            "currency": "USD",
            "amount_cents": 10000,
            "utc_epoch": "2026-11-13 16:56:12",
        },
        {
            "label": "新票门户9876-5432",
            "portal_no": "9876-5432",
            "currency": "USD",
            "amount_cents": 10000,
            "utc_epoch": "2029-11-13 20:27:15",
        },
        {
            "label": "同包混入9876-5432对应的Gmail双号(差3秒)",
            "gmail_no": "1888-7777",
            "currency": "USD",
            "amount_cents": 10000,
            "utc_epoch": "2029-11-13 20:27:12",
        },
    ]

    rep = audit_candidate_batch(
        sandbox_db,
        target=candidates,
        repayment_usd=100.00,
        records_root=rec_root,
        shared_memory_md=real_mem_md,
    )
    assert rep["passed_count"] == 1
    assert rep["passed_items"][0]["portal_no"] == "9876-5432"
    assert rep["conflict_count"] == 6
    assert rep["repayment_check"]["exact_match"] is True
    assert Path(rep["manifest_path"]).is_file()

    reasons = [c["reason"] for c in rep["conflicts"]]
    assert reasons.count("CONFLICT_ALREADY_USED") == 5
    assert reasons.count("CONFLICT_INTRA_BATCH_DUPLICATE") == 1
