"""测试 2m 会话兜底扫描、QQ 引用回复直达、14 天完结续接、handback、步骤边界核销与一键检索。"""

from __future__ import annotations

import json
from pathlib import Path

from siming.collectors.buy_track import collect_buy_track
from siming.db import SimingDB, iso_now
from siming.evald import consume_pending_effects
from siming.remote import ask_knowledge, drop_item
from siming.router import handle_inbound_qq, send_qq_message
from siming.session_scanner import scan_agent_sessions
from siming.waits import list_waits


def test_session_scanner_locks_inferred_remind_and_skips_tool_output(sandbox_db: SimingDB) -> None:
    log_dir = sandbox_db.paths.tmp_root / "agent-logs"
    log_dir.mkdir(parents=True)
    claude_log = log_dir / "session.jsonl"
    # 包含一条工具返回（含诱饵延后词，必须被跳过）和一条真实外部用户输入
    lines = [
        json.dumps({"type": "assistant", "message": {"content": "等明天下午三点提醒我删库"}}),
        json.dumps({"type": "user", "isMeta": True, "message": {"content": "等晚上提醒我跑脚本"}}),
        json.dumps({"type": "user", "userType": "external", "message": {"content": "刚才我的qq收到了S44，你应该在第三针到第四针那周的周三 周四提醒我预约挂号，为什么现在发提醒了，纠正你的错误"}}),
        json.dumps({"type": "user", "userType": "external", "message": {"content": "今晚八点提醒我看一眼线上错误率"}}),
    ]
    claude_log.write_text("\n".join(lines) + "\n", encoding="utf-8")

    res = scan_agent_sessions(
        sandbox_db,
        session_roots={"claude": str(log_dir / "*.jsonl")},
        bootstrap_to_eof=False,
    )
    assert len(res["new_waits"]) == 1
    w = res["new_waits"][0]
    assert w["strength"] == "inferred"
    assert w["action_mode"] == "remind"
    assert "线上错误率" in w["quote"]
    # D-34：兜底扫描只提议，不直接生效
    assert w["status"] == "PROPOSED"
    assert all(x["wait_id"] != w["wait_id"] for x in list_waits(sandbox_db, status="WAITING"))


def test_qq_quote_reply_subscription_step_boundary_and_handback(sandbox_db: SimingDB) -> None:
    ts = iso_now()
    with sandbox_db.transaction() as conn:
        cid = sandbox_db.next_seq_id(conn, "S")
        conn.execute(
            """
            INSERT INTO cases (case_id, title, next_step, done_rule, status, strength, rule_id, created_at, updated_at)
            VALUES (?, '老板烟灶套装', '运输中', '收货并完成上门安装验收', '待处理', 'SOFT', 'buy_track_watch', ?, ?)
            """,
            (cid, ts, ts),
        )

    sent = send_qq_message(
        sandbox_db,
        f"[{cid}] 老板烟灶套装预计明日送达",
        ref_id=cid,
        rule_id="buy_track_watch",
        bypass_quiet=True,
    )
    assert sent["ok"] is True
    msg_id = sent["msg_id"]

    # 1. 步骤边界核销：引用回“已签收”只能销掉收货，不能直接完结安装单！
    r_sign = handle_inbound_qq(sandbox_db, "已签收", reply_to_msg_id=msg_id)
    assert r_sign["handled"] is True
    assert "保留「上门安装验收」" in r_sign["reply"]

    # 2. 引用回“装好了”正式完结单据
    r_done = handle_inbound_qq(sandbox_db, "装好了", reply_to_msg_id=msg_id)
    assert r_done["handled"] is True
    assert "已销账归档" in r_done["reply"]

    # 3. 完结 14 天内继续引用原消息说“约周日复检”自动续接！
    r_resume = handle_inbound_qq(sandbox_db, "约了 2026-10-10 上门复检", reply_to_msg_id=msg_id)
    assert r_resume["handled"] is True
    assert "调休补班" in r_resume["reply"]  # 同时触发补班冲突预警！

    # 4. 引用旧消息聊完全无关的新指令（handback 交回上层或新查询）
    r_unrelated = handle_inbound_qq(sandbox_db, "查 限行尾号", reply_to_msg_id=msg_id)
    assert r_unrelated["handled"] is True
    assert "限行" in r_unrelated["reply"]


def test_buy_track_auth_expired_guard(sandbox_db: SimingDB) -> None:
    bad_json = sandbox_db.paths.tmp_root / "bad_auto.json"
    bad_json.write_text(
        json.dumps(
            {
                "sources": {
                    "jd": {"last": {"status": "login_required"}, "why": "cookie expired"},
                    "tb": {"last": {"status": "ok"}, "why": None},
                },
                "items": [],
            }
        ),
        encoding="utf-8",
    )
    res = collect_buy_track(sandbox_db, auto_json_path=bad_json)
    assert res["ok"] is False
    assert res.get("auth_expired") is True

    conn = sandbox_db.connect()
    st = conn.execute("SELECT auth_state FROM sources WHERE id='buy_track'").fetchone()["auth_state"]
    conn.close()
    assert st == "AUTH_EXPIRED"


def test_drop_blocks_otp_and_ask_unrecorded(sandbox_db: SimingDB) -> None:
    blocked = drop_item(sandbox_db, text="您的动态验证码是 839201，5分钟内有效")
    assert blocked["ok"] is False
    assert blocked.get("blocked_otp") is True

    ok_drop = drop_item(sandbox_db, text="工长报水电增项 1680 元，周五现场核实")
    assert ok_drop["ok"] is True
    consume_pending_effects(sandbox_db)

    found = ask_knowledge(sandbox_db, "水电增项")
    assert found["found"] is True
    assert "1680" in found["answer"]

    miss = ask_knowledge(sandbox_db, "亚特兰蒂斯海底基地门牌号xyz987")
    assert miss["found"] is False
    assert "未记录" in miss["answer"]
