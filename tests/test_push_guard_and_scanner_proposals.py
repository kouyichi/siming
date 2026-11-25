"""D-33 推送熔断/隐式降级 与 D-34 会话兜底扫描「只提议不建单」回归测试。

真实事故：
- 2026-11-22 16:07–16:15 `sub_renew` 8 分钟连推 139 条 QQ；14 天 140 推 1 处理仍是 ACTIVE。
- 会话扫描把 `/swarm 1. … 2. …` 指令、一段就医对话切成 W11–W25 十多条碎片挂起。
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import pytest

from siming.db import SimingDB, iso_now
from siming.modules import is_module_enabled
from siming.push_guard import GUARD_RULE_ID, guard_status, restore_rule
from siming.qq_cards import _ALLOWED_CMD_RE
from siming.router import handle_inbound_qq, send_qq_message
from siming.session_scanner import _looks_like_instruction_not_deferral, scan_agent_sessions
from siming.waits import list_waits


@pytest.fixture()
def awake(monkeypatch: pytest.MonkeyPatch, frozen_clock):
    frozen_clock("2026-11-22T16:07:00+08:00")
    import siming.db as _dbm

    monkeypatch.setattr("siming.waits.now_local", _dbm.now_local)  # frozen_clock 已替换 siming.db.now_local
    monkeypatch.setattr("siming.router._in_quiet_hours", lambda *a, **k: False)
    return frozen_clock


def _outbox(db: SimingDB, where: str = "1=1", params: tuple = ()) -> list[dict]:
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute(f"SELECT * FROM outbox WHERE {where} ORDER BY created_at", params).fetchall()]
    finally:
        conn.close()


def test_sub_renew_burst_is_capped_with_single_notice_and_digest_rollup(sandbox_db: SimingDB, awake) -> None:
    results = [
        send_qq_message(sandbox_db, f"🗡️ 订阅刺客止损卡 #{i}", ref_id=f"SUB-{i:04d}", rule_id="sub_renew")
        for i in range(30)
    ]
    delivered = [r for r in results if r.get("delivered")]
    throttled = [r for r in results if r.get("skipped") in ("rate_limited", "rule_cooldown")]
    assert len(delivered) == 6
    assert len(throttled) == 24
    # 熔断说明只发一次，并带「恢复推送」按钮
    notices = _outbox(sandbox_db, "rule_id=? AND status='SENT'", (GUARD_RULE_ID,))
    assert len(notices) == 1 and "sub_renew" in notices[0]["text"]
    # 被拦的消息留痕不丢，晨札只有一条汇总
    assert len(_outbox(sandbox_db, "rule_id='sub_renew' AND status='THROTTLED'")) == 24
    roll = _outbox(sandbox_db, "ref_id='guard:sub_renew' AND status='MORNING_QUEUE'")
    assert len(roll) == 1 and "24 条" in roll[0]["text"]

    # 冷却期过后恢复放行
    awake("2026-11-22T19:30:00+08:00")
    r = send_qq_message(sandbox_db, "🗡️ 新的一张", ref_id="SUB-NEW", rule_id="sub_renew")
    assert r.get("delivered") is True


def test_hard_or_urgent_rules_get_higher_cap(sandbox_db: SimingDB, awake) -> None:
    res = [send_qq_message(sandbox_db, f"🚨 大额异常 {i}", ref_id=f"TX-{i}", rule_id="x_alarm", urgent=True) for i in range(25)]
    assert sum(1 for r in res if r.get("delivered")) == 20


def test_global_hourly_cap_across_rules(sandbox_db: SimingDB, awake) -> None:
    res = [send_qq_message(sandbox_db, f"消息 {i}", ref_id=f"R{i}", rule_id=f"rule_{i % 5}") for i in range(30)]
    assert sum(1 for r in res if r.get("delivered")) == 20


def _seed_sent(db: SimingDB, rule_id: str, n: int, days_ago_start: int = 2) -> None:
    from siming.db import now_local

    base = now_local() - timedelta(days=days_ago_start)
    with db.transaction() as conn:
        conn.execute(
            "INSERT INTO rule_stats (rule_id, mode, updated_at) VALUES (?, 'ACTIVE', ?) ON CONFLICT(rule_id) DO NOTHING",
            (rule_id, iso_now()),
        )
        for i in range(n):
            ts = iso_now(base - timedelta(hours=i))
            conn.execute(
                "INSERT INTO outbox (id, ref_id, rule_id, text, send_at, status, channel, created_at, sent_at) "
                "VALUES (?, ?, ?, ?, ?, 'SENT', 'qqbot', ?, ?)",
                (f"OB-SEED-{rule_id}-{i}", f"{rule_id}:{i}", rule_id, f"旧消息 {i}", ts, ts, ts),
            )


def test_implicit_quiet_after_14d_of_being_ignored_but_module_keeps_running(sandbox_db: SimingDB, awake) -> None:
    _seed_sent(sandbox_db, "buy_track_watch", 26)
    r = send_qq_message(sandbox_db, "📦 家电签收", ref_id="S99", rule_id="buy_track_watch")
    assert r["skipped"] == "rule_implicit_quiet"
    assert "notice" in r
    conn = sandbox_db.connect()
    mode = conn.execute("SELECT mode FROM rule_stats WHERE rule_id='buy_track_watch'").fetchone()["mode"]
    conn.close()
    assert mode == "QUIET"
    # QUIET 只停推送，不停模块后台逻辑（区别于 DEMOTED）
    assert is_module_enabled(sandbox_db, "buy_track_watch") is True
    # 后续消息进晨札汇总
    r2 = send_qq_message(sandbox_db, "📦 又一件签收", ref_id="S100", rule_id="buy_track_watch")
    assert r2["skipped"] == "rule_quiet_digest"
    assert any(q["rule_id"] == "buy_track_watch" for q in guard_status(sandbox_db)["quiet"])

    # QQ 一句话恢复，且宽限期内不再被自动降级
    reply = handle_inbound_qq(sandbox_db, "恢复推送 buy_track_watch")
    assert reply["handled"] and "已恢复" in reply["reply"]
    awake("2026-11-23T16:07:00+08:00")
    r3 = send_qq_message(sandbox_db, "📦 第三件签收", ref_id="S101", rule_id="buy_track_watch")
    assert r3.get("delivered") is True


def test_acted_rules_and_exempt_rules_are_never_implicitly_quieted(sandbox_db: SimingDB, awake) -> None:
    _seed_sent(sandbox_db, "wait_remind", 30)
    assert send_qq_message(sandbox_db, "[W01] 到点提醒", ref_id="W01", rule_id="wait_remind").get("delivered") is True
    _seed_sent(sandbox_db, "meituan_cabinet", 20)
    with sandbox_db.transaction() as conn:
        conn.execute(
            "INSERT INTO feedback (target_id, rule_id, action, note, ts) VALUES ('S22', 'meituan_cabinet', '已办', '', ?)",
            (iso_now(),),
        )
        conn.execute(
            "INSERT INTO feedback (target_id, rule_id, action, note, ts) VALUES ('S23', 'meituan_cabinet', '已办', '', ?)",
            (iso_now(),),
        )
    assert send_qq_message(sandbox_db, "🍱 外卖柜取餐码", ref_id="S24", rule_id="meituan_cabinet").get("delivered") is True


def test_restore_unknown_rule_is_safe(sandbox_db: SimingDB) -> None:
    assert restore_rule(sandbox_db, "nope")["ok"] is False


# ---------------- D-34 会话兜底扫描 ----------------


@pytest.mark.parametrize(
    "text",
    [
        "/swarm 1. tower 的可视化非常差；2. 订阅算进了美团的外卖，完全被误判，明天再看",
        "下一阶段的规划也做一个网页，做得好看一点，流程图规划图啥的，给我看；1. 代码 2. 测试",
        "把review做个网页我来看，同时开始修复，等明天再说",
        "x" * 200 + "明天提醒我",
    ],
)
def test_instruction_like_texts_are_not_proposals(text: str) -> None:
    assert _looks_like_instruction_not_deferral(text) is True


def test_real_deferral_is_still_proposable() -> None:
    assert _looks_like_instruction_not_deferral("今晚八点提醒我看一眼线上错误率") is False
    assert _looks_like_instruction_not_deferral("等报销的时候再修 Gmail") is False


def _write_session(dir_: Path, texts: list[str]) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    f = dir_ / "sess.jsonl"
    with f.open("a", encoding="utf-8") as fh:
        for t in texts:
            fh.write(json.dumps({"type": "user", "userType": "external", "message": {"content": t}}, ensure_ascii=False) + "\n")
    return f


def test_scanner_proposes_merges_and_requires_confirmation(sandbox_db: SimingDB, awake) -> None:
    log_dir = sandbox_db.paths.tmp_root / "agent-logs"
    roots = {"claude": str(log_dir / "*.jsonl")}
    _write_session(log_dir, ["今晚八点提醒我看一眼线上错误率"])
    r1 = scan_agent_sessions(sandbox_db, session_roots=roots, bootstrap_to_eof=False)
    assert len(r1["new_waits"]) == 1 and r1["new_waits"][0]["status"] == "PROPOSED"
    w1 = r1["new_waits"][0]["wait_id"]
    # 24h 内要响 → 发一条带「记下/不用」按钮的轻询问
    assert r1["new_waits"][0]["suggest_push"]["ok"] is True
    asks = _outbox(sandbox_db, "rule_id='scan_suggest' AND status='SENT'")
    assert len(asks) == 1 and w1 in asks[0]["text"]
    assert [w for w in list_waits(sandbox_db, status="WAITING") if w["strength"] == "inferred"] == []

    # 同会话 30 分钟内再提一句：合并为最新一条，不留碎片
    awake("2026-11-22T16:20:00+08:00")
    _write_session(log_dir, ["改成今晚九点提醒我看一眼线上错误率"])
    r2 = scan_agent_sessions(sandbox_db, session_roots=roots, bootstrap_to_eof=False)
    w2 = r2["new_waits"][0]["wait_id"]
    proposed = list_waits(sandbox_db, status="PROPOSED")
    assert [w["wait_id"] for w in proposed] == [w2]

    # 斜杠指令不提议
    _write_session(log_dir, ["/swarm 1. 修 tower 2. 明天提醒我看订阅"])
    assert scan_agent_sessions(sandbox_db, session_roots=roots, bootstrap_to_eof=False)["new_waits"] == []

    # 按钮命令在白名单内；回「记下」才转 WAITING
    assert _ALLOWED_CMD_RE.fullmatch(f"记下 {w2}") and _ALLOWED_CMD_RE.fullmatch(f"忽略 {w2}")
    ok = handle_inbound_qq(sandbox_db, f"记下 {w2}")
    assert ok["handled"] and "已记下" in ok["reply"]
    assert [w["wait_id"] for w in list_waits(sandbox_db, status="WAITING") if w["strength"] == "inferred"] == [w2]


def test_ignored_and_stale_proposals_never_fire(sandbox_db: SimingDB, awake) -> None:
    log_dir = sandbox_db.paths.tmp_root / "agent-logs2"
    roots = {"claude": str(log_dir / "*.jsonl")}
    _write_session(log_dir, ["等报销的时候再修 Gmail"])
    r = scan_agent_sessions(sandbox_db, session_roots=roots, bootstrap_to_eof=False)
    wid = r["new_waits"][0]["wait_id"]
    assert r["new_waits"][0]["status"] == "PROPOSED"
    assert "suggest_push" not in r["new_waits"][0]  # 非 24h 内：只进晨札，不即时打扰
    # 意图词出现也不触发 PROPOSED
    from siming.waits import evaluate_waits

    assert wid not in evaluate_waits(sandbox_db, intent_texts=["我要报销了"])["fired"]
    # 72h 未确认自动过期
    awake("2026-11-25T17:00:00+08:00")
    _write_session(log_dir, ["随便聊聊"])
    scan_agent_sessions(sandbox_db, session_roots=roots, bootstrap_to_eof=False)
    conn = sandbox_db.connect()
    st = conn.execute("SELECT status FROM waits WHERE wait_id=?", (wid,)).fetchone()["status"]
    conn.close()
    assert st == "EXPIRED"
    assert "不是待确认" in handle_inbound_qq(sandbox_db, f"忽略 {wid}")["reply"]
