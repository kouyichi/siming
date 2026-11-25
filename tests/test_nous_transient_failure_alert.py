"""2026-11-23 事故回放：Nous「Auto-refill Payment Failed」后 6 分钟自动充值成功，两封邮件同一轮入库（UTC `Z` 时间戳），
哨兵判健康后一声不吭。现在必须单独告知一次扣款失败（普通级），且不重复。"""

from __future__ import annotations

from siming.db import SimingDB
from siming.self_heal import evaluate_nous_billing_sentinel


def _mail(db: SimingDB, n: int, subject: str, occ: str) -> None:
    with db.transaction() as conn:
        db.append_event(
            conn,
            source="mail163",
            thread_key=f"mail:gmail:{n}",
            summary=subject,
            payload={"sender": "noreply@nousresearch.com"},
            occurred_at=occ,
            fingerprint=f"omamail:gmail:nous:{n}",
        )


def _pending_texts(db: SimingDB) -> list[str]:
    import json

    conn = db.connect()
    try:
        rows = conn.execute("SELECT payload_json FROM effects WHERE kind='send_qq'").fetchall()
    finally:
        conn.close()
    return [json.loads(r["payload_json"]).get("text", "") for r in rows]


def test_transient_payment_failure_is_reported_once_even_if_recovered(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-23T04:30:00+08:00")
    _mail(sandbox_db, 1, "[Gmail] Nous Research - Auto-refill Successful", "2026-11-22T17:15:04.000Z")
    _mail(sandbox_db, 2, "[Gmail] Nous Research - Auto-refill Payment Failed", "2026-11-22T18:15:09.000Z")
    _mail(sandbox_db, 3, "[Gmail] Nous Research - Auto-refill Successful", "2026-11-22T18:20:04.000Z")
    _mail(sandbox_db, 4, "[Gmail] Your Nous Research Inc. receipt [#1202-9932]", "2026-11-22T18:21:20.000Z")

    r = evaluate_nous_billing_sentinel(sandbox_db, notify=True)
    assert r["locked"] is False
    assert len(r["transient_alerted"]) == 1
    texts = [t for t in _pending_texts(sandbox_db) if "扣款失败" in t]
    assert len(texts) == 1
    assert "11-23 02:15" in texts[0] and "5 分钟后自动充值已成功" in texts[0]

    # 再跑一轮不重复提醒
    r2 = evaluate_nous_billing_sentinel(sandbox_db, notify=True)
    assert r2["transient_alerted"] == []
    assert len([t for t in _pending_texts(sandbox_db) if "扣款失败" in t]) == 1


def test_unrecovered_failure_still_raises_lock_card(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-23T04:30:00+08:00")
    _mail(sandbox_db, 1, "[Gmail] Your Nous Research Inc. receipt [#1]", "2026-11-23T01:00:00+08:00")
    # 混合时区格式：失败邮件是 UTC Z，字符串比较会误判为更早
    _mail(sandbox_db, 2, "[Gmail] Nous Research - Auto-refill Payment Failed", "2026-11-22T18:15:09.000Z")
    r = evaluate_nous_billing_sentinel(sandbox_db, notify=True)
    assert r["locked"] is True
    assert r["transient_alerted"] == []
    assert any("付款锁" in t for t in _pending_texts(sandbox_db))


def test_old_failures_are_not_backfilled(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-23T04:30:00+08:00")
    _mail(sandbox_db, 1, "[Gmail] Nous Research - Auto-refill Payment Failed", "2026-11-20T01:00:00+08:00")
    _mail(sandbox_db, 2, "[Gmail] Nous Research - Auto-refill Successful", "2026-11-20T01:05:00+08:00")
    r = evaluate_nous_billing_sentinel(sandbox_db, notify=True)
    assert r["transient_alerted"] == []
