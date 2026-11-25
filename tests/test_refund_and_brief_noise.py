"""2026-11-18 用户纠错回归：
1. 中行「网上支付收入人民币3227.58元」是林内热水器退款，曾被误记成消费、误触发报销游标卡并在晨札里当成最大一笔支出；
2. W07（一段关于「学习我的规律」的设计要求）被会话兜底扫描误判为 18:30 提醒，晨札里刷两遍、晚上又推一遍——
   兜底项只应过目一次，「学到的规律」只在真学到时才出现。
"""

from __future__ import annotations

from datetime import timedelta

from siming.cn_calendar import try_parse_explicit_datetime
from siming.db import SimingDB, iso_now, now_local
from siming.ledger.sms_parser import classify_txn_kind, try_ingest_bank_sms
from siming.morning import build_morning_editions
from siming.session_scanner import DEFER_PREFILTER_RE
from siming.waits import add_wait

BOC_REFUND = "您的借记卡账户1003，于11月17日网上支付收入人民币3227.58元,交易后余额400802.84【中国银行】"
W07_TEXT = (
    "不只是触发条件，我会告诉你，你也要学习我每天 每周 每个月的规律 包括上下班 出行游玩 生活用品补货等，"
    "知道了一件事之后要跟什么事，才能在受到影响的时候提出策略，才能在没检测到的时候提醒；要了解行动，还要能检测行动。"
)


def test_boc_online_payment_income_is_not_spend() -> None:
    assert classify_txn_kind(BOC_REFUND, last4="1003") == "收入"
    assert classify_txn_kind("您的借记卡账户1003，于11月17日网上支付支出人民币88.00元【中国银行】", "1003") == "消费"
    assert classify_txn_kind("【中国银行】您尾号1003的借记卡消费人民币3227.58元。", "1003") == "消费"
    # 信用卡「入账金额以账单为准」不能被当成入账
    assert classify_txn_kind("您尾号1001信用卡消费100.00美元，入账金额以账单为准【光大银行】", "1001") == "消费"


def test_boc_refund_sms_no_cursor_case_and_brief_shows_inflow(sandbox_db: SimingDB) -> None:
    res = try_ingest_bank_sms(sandbox_db, BOC_REFUND, occurred_at=now_local().isoformat(timespec="seconds"))
    assert res["matched"] is True and res["kind"] == "收入"

    conn = sandbox_db.connect()
    cursor_cases = conn.execute("SELECT 1 FROM cases WHERE category='reimburse_cursor'").fetchall()
    conn.close()
    assert cursor_cases == []

    concise = build_morning_editions(sandbox_db)["concise"]
    assert "人民币支出" not in concise
    assert "入账/退款 1 笔共 ¥3227.58" in concise


def test_w07_design_statement_is_not_a_deferral() -> None:
    assert DEFER_PREFILTER_RE.search(W07_TEXT) is None
    assert DEFER_PREFILTER_RE.search("等报销的时候再把 Omamail 切 Pro") is not None
    assert DEFER_PREFILTER_RE.search("等洗衣机到货再看") is not None


def test_commute_noun_is_not_off_work_time(sandbox_db: SimingDB) -> None:
    assert try_parse_explicit_datetime("学习我上下班的规律", paths=sandbox_db.paths) is None
    hit = try_parse_explicit_datetime("下班后提醒我取快递", paths=sandbox_db.paths)
    assert hit is not None and (hit[0].hour, hit[0].minute) == (18, 30)


def test_inferred_wait_shown_once_and_learned_line_gated(sandbox_db: SimingDB) -> None:
    today_evening = now_local().replace(hour=23, minute=50, second=0, microsecond=0)
    fresh = add_wait(sandbox_db, when=f"time:{iso_now(today_evening)}", mode="remind", strength="inferred", quote="今晚提醒我看错误率")
    stale = add_wait(sandbox_db, when="intent:报销", mode="remind", strength="inferred", quote="等报销的时候再修脚本")
    conn = sandbox_db.connect()
    with conn:
        conn.execute(
            "UPDATE waits SET created_at=? WHERE wait_id=?",
            (iso_now(now_local() - timedelta(days=2)), stale["wait_id"]),
        )
    conn.close()

    concise = build_morning_editions(sandbox_db)["concise"]
    assert f"[{fresh['wait_id']}]" in concise
    assert concise.count(f"[{fresh['wait_id']}]") == 1  # 不在「今日重点」和「兜底待确认」各刷一遍
    assert f"[{stale['wait_id']}]" not in concise  # 过目过的兜底项不再天天刷
    assert "🧠" not in concise  # 没学到东西就不出现

    conn = sandbox_db.connect()
    with conn:
        conn.execute(
            "INSERT INTO hypotheses (id, claim, evidence_ids, status, expires_at, created_at) VALUES ('H-T1', ?, '[]', 'OBSERVING', ?, ?)",
            ("工作日 12:30 前后常点外卖", iso_now(now_local() + timedelta(days=30)), iso_now()),
        )
    conn.close()
    assert "🧠 近24h新学到 1 条规律：工作日 12:30 前后常点外卖" in build_morning_editions(sandbox_db)["concise"]
