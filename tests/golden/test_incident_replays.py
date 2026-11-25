"""Wave 0 (W0-4) 黄金故障回放集：10 条真实 5 天日志与 2026-11-21 现网事故端到端回归测试。"""

from __future__ import annotations

import json
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from siming.approval import create_approval, expire_stale_approvals
from siming.cn_calendar import try_parse_explicit_datetime
from siming.collectors.buy_track import collect_buy_track
from siming.collectors.omamail_bridge import _try_ingest_subscription_renewal, collect_omamail
from siming.daily_care import (
    _in_injection_window,
    _injection_log,
    get_pen_state,
    run_injection_guard,
)
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, logical_today
from siming.enrich import enrich_appliance_arrival, lookup_deco_snippets
from siming.install_plan import (
    classify_arrival_status,
    format_arrival_inspection_checklist,
    is_order_cancelled,
    is_order_truly_signed,
)
from siming.meituan_pay import _run_hermes_confidential_payment_handoff
from siming.morning import build_sunday_health_audit
from siming.push import flush_debounced_kuma_alerts, push_external
from siming.remote import drop_item
from siming.router import close_case, handle_inbound_qq, send_qq_message
from siming.session_scanner import META_SKIP_RE, scan_agent_sessions


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI_TZ)


def test_incident_01_qq_inline_quoted_message_closes_food_pickup_and_tracks_rule_stats(
    sandbox_db: SimingDB,
) -> None:
    """事故 1（11-20 S48 美团外卖柜）：QQBot 内联 `[Quoted message]:\\n...\\n\\n拿了` 必须确定性销账且更新订阅与 rule_stats。"""
    ts = iso_now()
    with sandbox_db.transaction() as conn:
        cid = sandbox_db.next_seq_id(conn, "S")
        conn.execute(
            """
            INSERT INTO cases (case_id, title, next_step, done_rule, status, strength, category, rule_id, context_json, created_at, updated_at)
            VALUES (?, '美团外卖柜：示例小区A（取餐码 0435）', '下楼取餐', '回拿了', '待处理', 'NORMAL', 'food_pickup', 'meituan_cabinet', '{}', ?, ?)
            """,
            (cid, ts, ts),
        )
    send_qq_message(
        sandbox_db,
        f"🍱 [{cid}] 美团外卖已存入外卖柜（取餐码 0435），取完引用本条回「拿了」。",
        ref_id=cid,
        rule_id="meituan_cabinet",
        bypass_quiet=True,
    )

    raw_qq = (
        f"[Quoted message]:\n"
        f"🍱 [{cid}] 美团外卖已存入外卖柜（取餐码 0435），取完引用本条回「拿了」。\n\n"
        f"拿了"
    )
    res = handle_inbound_qq(sandbox_db, raw_qq)
    assert res["handled"] is True
    assert cid in str(res.get("reply") or "")

    conn = sandbox_db.connect()
    try:
        c_row = conn.execute("SELECT status FROM cases WHERE case_id=?", (cid,)).fetchone()
        sub_row = conn.execute("SELECT state, ended_at FROM subscriptions WHERE target_id=?", (cid,)).fetchone()
        rs_row = conn.execute("SELECT acted_14d FROM rule_stats WHERE rule_id='meituan_cabinet'").fetchone()
        assert c_row["status"] == "完成"
        assert sub_row is not None and sub_row["state"] == "ENDED" and sub_row["ended_at"] is not None
        assert rs_row is not None and int(rs_row["acted_14d"]) >= 1
    finally:
        conn.close()


def test_incident_02_qq_inline_quoted_message_executes_approval_and_expires_stale(
    sandbox_db: SimingDB,
) -> None:
    """事故 2（11-19 apply 漏 LLM + 13 张过期 PENDING 僵尸卡）：内联引用回「确认」确定性命中审批，过期卡被自动标 EXPIRED。"""
    # 1. 种入一张已过期的 PENDING 审批卡，验证 expire_stale_approvals 自动清理
    stale = create_approval(
        sandbox_db,
        action="shift_expectation",
        target_id="EXP-OLD",
        params={"expectation_id": "EXP-OLD"},
        ttl_minutes=5,
    )
    with sandbox_db.transaction() as conn:
        conn.execute(
            "UPDATE approvals SET expires_at='2020-01-01T00:00:00+08:00' WHERE token=?",
            (stale["token"],),
        )
    expired_n = expire_stale_approvals(sandbox_db, now=_dt("2026-11-21 15:00"))
    assert expired_n == 1

    # 2. 有效审批卡通过 QQBot 内联引用回「取消」确定性执行（不透传 LLM）
    live_app = create_approval(
        sandbox_db,
        action="meituan_lock_cart",
        target_id="MT-1",
        params={"shop": "肯德基"},
        ttl_minutes=20,
    )
    tok = live_app["token"]
    quoted_cancel = (
        f"[Quoted message]:\n"
        f"🛒 美团加购确认卡：回复 apply {tok} 确认，或回取消。\n\n"
        f"取消"
    )
    res = handle_inbound_qq(sandbox_db, quoted_cancel)
    assert res["handled"] is True

    conn = sandbox_db.connect()
    try:
        st_old = conn.execute("SELECT status FROM approvals WHERE token=?", (stale["token"],)).fetchone()["status"]
        st_live = conn.execute("SELECT status FROM approvals WHERE token=?", (tok,)).fetchone()["status"]
        assert st_old == "EXPIRED"
        assert st_live == "INVALIDATED"
    finally:
        conn.close()


def test_incident_03_med_injection_close_case_syncs_care_log_no_repush(
    sandbox_db: SimingDB,
) -> None:
    """事故 3（11-19 01:02 `siming done S43` 1 分钟后重推）：close_case 必须原子同步 care_injection_log 并扣减针数。"""
    sandbox_db.set_meta("care_pen", json.dumps({"doses_left": 3, "pen_size": 4}))
    t0 = _dt("2026-11-19 00:55")  # 周四凌晨 00:55（对应周三 11-18 那针）
    r1 = run_injection_guard(sandbox_db, now=t0)
    cid = r1.get("created")
    assert cid and cid.startswith("S")

    # 模拟命令行 `siming done Sxx` 直接调用 close_case
    closed = close_case(sandbox_db, cid, note="manual_done")
    assert closed.get("ok") is True

    # 验证单真源已同步到 care_injection_log 且剩余针数从 3 -> 2
    log = _injection_log(sandbox_db)
    assert any(x.get("slot") == "2026-11-18" and not x.get("skipped") for x in log)
    assert get_pen_state(sandbox_db)["doses_left"] == 2

    # 1 分钟后再跑 run_injection_guard：必须跳过，绝不重建新单、绝不重推！
    r2 = run_injection_guard(sandbox_db, now=_dt("2026-11-19 00:56"))
    assert r2.get("skipped") == "already_logged"
    assert "created" not in r2


def test_incident_04_night_owl_logical_day_and_session_scanner_meta_skip(
    sandbox_db: SimingDB,
) -> None:
    """事故 4（夜猫子 00:00-04:59 逻辑日漂移 + session_scanner 误抓背景介绍/路线图段落）。"""
    late_night = _dt("2026-11-19 00:52")  # 周四凌晨 00:52 -> 逻辑周三 11-18
    assert logical_today(late_night) == date(2026, 11, 18)

    # 凌晨 00:52 说「明早9点」，应落在当天早上 2026-11-19 09:00，而不是跳过一整天落到 11-20！
    parsed = try_parse_explicit_datetime("明早9点提醒我看下", ref_dt=late_night, paths=sandbox_db.paths)
    assert parsed is not None
    dt_target, _ = parsed
    assert (dt_target.year, dt_target.month, dt_target.day, dt_target.hour) == (2026, 11, 19, 9)
    assert _in_injection_window(sandbox_db, now=late_night) is True

    # session_scanner 绝不把规划/背景描述/评审段落误建为 inferred wait
    assert META_SKIP_RE.search("给你一些我日常的状态，方便你后续规划") is not None
    assert META_SKIP_RE.search("观察这几天siming的运行和错误记录，深入review后规划更新到网页") is not None

    sess_file = sandbox_db.paths.tmp_root / "claude_sess.jsonl"
    sess_file.write_text("", encoding="utf-8")
    # 首次运行 bootstrap
    scan_agent_sessions(sandbox_db, session_roots={"claude": str(sess_file)})
    long_bg = (
        "我目前正在梳理整个个人助理系统的多波次演进方案，包含很多架构细节与历史背景介绍，"
        "平时每周三晚上回到家洗澡之后会整理一下待办和清单，等以后把所有子模块都梳理完毕再统一看整体节奏，"
        "目前主要是先把各个模块之间的接口边界与数据流向画清楚，方便后续一并评估迭代优先级和资源投入，"
        "同时也要兼顾多端设备之间的低延迟状态同步、隐私脱敏红线、以及网页控制台与轻量消息通道的分工协作边界。"
    )
    assert len(long_bg) > 180 and "提醒我" not in long_bg
    with sess_file.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "user", "message": {"role": "user", "content": long_bg}}, ensure_ascii=False) + "\n")
    r_scan = scan_agent_sessions(sandbox_db, session_roots={"claude": str(sess_file)})
    assert r_scan["new_waits"] == []


def test_incident_05_concurrent_db_init_lock_zero_duplicate_column_error(
    sandbox_db: SimingDB,
) -> None:
    """事故 5（11-19 00:18 并发 `init_db()` 抛 duplicate column name）：8 线程并发初始化零异常。"""
    fresh_dir = sandbox_db.paths.tmp_root / "concurrent_db_test"
    fresh_dir.mkdir(parents=True, exist_ok=True)
    fresh_paths = sandbox_db.paths.__class__(
        repo_root=sandbox_db.paths.repo_root,
        config_dir=fresh_dir / "config",
        secrets_dir=fresh_dir / "config" / "secrets",
        data_dir=fresh_dir / "data",
        raw_dir=fresh_dir / "data" / "raw",
        backup_dir=fresh_dir / "data" / "backups",
        tmp_root=fresh_dir / "tmp",
        worktrees_dir=fresh_dir / "tmp" / "worktrees",
        reimburse_dir=fresh_dir / "data" / "reimburse",
        shared_memory_dir=fresh_dir / "memory",
        shared_inbox_dir=fresh_dir / "memory" / "inbox",
        db_path=fresh_dir / "data" / "siming.db",
    )
    errors: list[Exception] = []

    def _worker() -> None:
        try:
            SimingDB(fresh_paths).init_db()
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)
    assert errors == []

    conn = SimingDB(fresh_paths).connect()
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        assert {"cases", "approvals", "rule_stats", "usage", "bookmarks"}.issubset(tables)
    finally:
        conn.close()


def test_incident_06_buy_track_enrich_and_install_plan_precision(
    sandbox_db: SimingDB,
) -> None:
    """事故 6（家电串品、dict svc 崩溃、假签收、交易关闭噪音与重复签收双推）。"""
    # 1. 构造隔离的 deco/plans 与 buy/track/items.json
    deco_plans = Path(sandbox_db.paths.tmp_root.parent / "deco" / "plans")
    deco_plans.mkdir(parents=True, exist_ok=True)
    (deco_plans / "baseline.md").write_text(
        "- 卫生间浴霸：米家智能浴霸 Pro，Zigbee 开关预留零线\n"
        "- 客厅旧方案：100寸电视墙预留净宽 2400mm\n"
        "- 卧室空调孔位：格力 1.5匹 空调预留 16A 插座，抽真空至少 15 分钟\n"
        "- 卫生间壁挂洗衣机：海尔 3kg 承重墙体与上下水预留\n",
        encoding="utf-8",
    )
    buy_track_dir = Path(sandbox_db.paths.tmp_root.parent / "buy" / "track")
    (buy_track_dir / "data").mkdir(parents=True, exist_ok=True)
    items_doc = {
        "preferences": {"install_date": "2026-12-10"},
        "items": [
            {"id": "ac-1", "name": "格力 云佳Pro 1.5 匹 #1", "install": "brand", "room": "主卧", "installDate": "2026-12-10", "installNote": "抽真空>=15分钟"},
            {"id": "washer-wall", "name": "海尔 壁挂洗衣机 3kg", "install": "brand", "room": "卫生间干区", "installDate": "2026-12-10"},
            {"id": "heater", "name": "林内 恒逸 R32F", "install": "brand", "room": "厨房", "installDate": "2026-12-10"},
            {"id": "hood", "name": "老板 双子星 E2S 烟灶套装", "install": "brand", "room": "厨房", "installDate": "2026-12-10"},
        ],
    }
    items_path = buy_track_dir / "items.json"
    items_path.write_text(json.dumps(items_doc, ensure_ascii=False), encoding="utf-8")

    # 2. 防串品验证：空调不配浴霸 Zigbee；壁挂洗衣机不配 100寸电视；尊重 2026-12-10 集中安装日
    ac_snips = lookup_deco_snippets(["格力", "1.5匹"], item_id="ac-1", item_name="格力 云佳Pro 1.5 匹 #1")
    assert all("浴霸" not in s and "Zigbee" not in s for s in ac_snips)
    assert any("16A" in s or "2026-12-10" in s for s in ac_snips)

    ww_snips = lookup_deco_snippets(["海尔", "3kg"], item_id="washer-wall", item_name="海尔 壁挂洗衣机 3kg")
    assert all("100寸" not in s and "电视" not in s for s in ww_snips)

    enr = enrich_appliance_arrival(
        "格力 云佳Pro 1.5 匹 #1",
        db=sandbox_db,
        item_id="ac-1",
        install_date="2026-12-10",
    )
    assert "2026-12-10" in enr
    assert "浴霸" not in enr

    # 3. 假签收与取消状态校验
    assert is_order_truly_signed("delivered", "等待收货", "快递员正在派送中，请保持电话畅通") is False
    assert is_order_truly_signed("cancelled", "交易关闭", "") is False
    assert is_order_cancelled("cancelled", "交易关闭") is True
    assert classify_arrival_status({"stage": "cancelled", "raw": "交易关闭"})[0] == "cancelled"

    # 4. buy_track：dict svc 不崩溃 + 交易关闭静默收口 + 重复签收不双推 + 初验单剔除已取消订单
    auto_path = buy_track_dir / "data" / "auto.json"
    snap1 = {
        "generatedAt": "2026-11-20T08:00:00Z",
        "sources": {"jd": {"last": {"status": "ok"}}, "tb": {"last": {"status": "ok"}}},
        "items": [
            {"id": "heater", "name": "林内 恒逸 R32F", "install": "brand", "svc": [{"id": "SVC-1001"}], "orderInfo": {"stage": "pending", "raw": "待发货", "progress": "已下单"}},
            {"id": "hood", "name": "老板 双子星 E2S 烟灶套装", "install": "brand", "svc": [{"orderId": "SVC-2002"}], "orderInfo": {"stage": "shipping", "raw": "派送中", "progress": "正在派送中"}},
        ],
    }
    auto_path.write_text(json.dumps(snap1, ensure_ascii=False), encoding="utf-8")
    collect_buy_track(sandbox_db, auto_json_path=auto_path)

    # 第二步：hood 已送达（首次签收推送 1 次），heater 交易关闭（静默收口，不推送）
    snap2 = {
        **snap1,
        "items": [
            {"id": "heater", "name": "林内 恒逸 R32F", "install": "brand", "svc": [{"id": "SVC-1001"}], "orderInfo": {"stage": "cancelled", "raw": "交易关闭", "progress": "退款关闭"}},
            {"id": "hood", "name": "老板 双子星 E2S 烟灶套装", "install": "brand", "svc": [{"orderId": "SVC-2002"}], "orderInfo": {"stage": "delivered", "raw": "等待收货", "progress": "您的订单已送达，签收人：本人"}},
        ],
    }
    auto_path.write_text(json.dumps(snap2, ensure_ascii=False), encoding="utf-8")
    collect_buy_track(sandbox_db, auto_json_path=auto_path)

    # 第三步：hood 文案变为「完成」（仍为已签收状态）——绝不再推第二次！
    snap3 = {
        **snap1,
        "items": [
            snap2["items"][0],
            {"id": "hood", "name": "老板 双子星 E2S 烟灶套装", "install": "brand", "svc": [{"orderId": "SVC-2002"}], "orderInfo": {"stage": "delivered", "raw": "完成", "progress": "已完成"}},
        ],
    }
    auto_path.write_text(json.dumps(snap3, ensure_ascii=False), encoding="utf-8")
    collect_buy_track(sandbox_db, auto_json_path=auto_path)

    conn = sandbox_db.connect()
    try:
        hood_pushes = conn.execute(
            "SELECT effect_id FROM effects WHERE kind='send_qq' AND payload_json LIKE '%老板 双子星%'"
        ).fetchall()
        heater_pushes = conn.execute(
            "SELECT effect_id FROM effects WHERE kind='send_qq' AND payload_json LIKE '%林内%'"
        ).fetchall()
        assert len(hood_pushes) == 1
        assert len(heater_pushes) == 0
    finally:
        conn.close()

    chk_txt = format_arrival_inspection_checklist(sandbox_db, items_json_path=items_path, auto_json_path=auto_path)
    assert "老板 双子星" in chk_txt
    assert "林内 恒逸" not in chk_txt
    assert "已取消跳过 1 单" in chk_txt


def test_incident_07_triage_github_url_and_article_bookmark_routing(
    sandbox_db: SimingDB,
) -> None:
    """事故 7（11-21 B19 双 github.com 链接 + S49/S51 资讯截图误建待处理 drop 单）。"""
    r_gh = drop_item(
        sandbox_db,
        text="开源项目推荐 github.com/simplexai-labs/linger — Pi Personal Agent Runtime + Hindsight Wiki",
        source_channel="ios_drop",
    )
    assert r_gh["ok"] is True and r_gh["kind"] == "bookmark"

    r_x = drop_item(
        sandbox_db,
        text="帖子 · AI少年\n关注 @AI_Youth_X\n今天发现一个超强的开源项目 Personal Agent Runtime，配合 Hindsight 自动维护 Wiki 和 MCP 工具。",
        source_channel="ios_drop",
    )
    assert r_x["ok"] is True and r_x["kind"] == "bookmark"

    r_paper = drop_item(
        sandbox_db,
        text="新智元 15:04\n答案，或许藏在谷歌与宾夕法尼亚州立大学深夜联合发布的一篇最新论文里。\nSelf-Improvement Memory (arXiv:2611.18856)",
        source_channel="ios_drop",
    )
    assert r_paper["ok"] is True and r_paper["kind"] == "bookmark"

    conn = sandbox_db.connect()
    try:
        bms = [dict(r) for r in conn.execute("SELECT bookmark_id, kind, url, repo_slug FROM bookmarks ORDER BY bookmark_id").fetchall()]
        open_drops = conn.execute("SELECT case_id FROM cases WHERE category='drop' AND status='待处理'").fetchall()
        assert len(open_drops) == 0
        gh_bm = next(b for b in bms if b["kind"] == "github")
        assert gh_bm["repo_slug"] == "simplexai-labs/linger"
        assert gh_bm["url"] == "https://github.com/simplexai-labs/linger"
        assert "github.com/github.com" not in gh_bm["url"]
        assert len([b for b in bms if b["kind"] == "article"]) == 2
    finally:
        conn.close()


def test_incident_08_d3_kuma_3m_flap_debounce_and_realtime_x_untouched(
    sandbox_db: SimingDB, monkeypatch
) -> None:
    """事故 8（D3-B：Uptime Kuma 3m 瞬断防抖；真实 >3m 宕机放行；X 监控保持原样实时）。"""
    import siming.router as router

    monkeypatch.setattr(router, "_in_quiet_hours", lambda policy, db=None, now=None: False)

    # 1. 瞬断：T=0 异常 -> 暂存；T+120s 恢复 -> 双吞（0 条 outbox）
    t0 = _dt("2026-11-21 10:00")
    r_down = push_external(sandbox_db, "【主机监控】S-UI：异常", rule_id="uptime_kuma", ref_id="kuma:sui", level="now", now=t0)
    assert r_down["ok"] is True and r_down["debounced"] is True and r_down["delivered"] is False

    r_up = push_external(sandbox_db, "【主机监控】S-UI：恢复", rule_id="uptime_kuma", ref_id="kuma:sui", level="now", now=t0 + timedelta(seconds=120))
    assert r_up["ok"] is True and r_up["flap_suppressed"] is True and r_up["delivered"] is False

    conn = sandbox_db.connect()
    assert conn.execute("SELECT COUNT(*) FROM outbox WHERE rule_id='ext_uptime_kuma'").fetchone()[0] == 0
    conn.close()

    # 2. 真宕机：T=0 异常 -> T+120s 不冲刷 -> T+185s 冲刷发送 -> T+300s 恢复立即放行
    t1 = _dt("2026-11-21 11:00")
    push_external(sandbox_db, "【主机监控】NAS：异常", rule_id="uptime_kuma", ref_id="kuma:nas", level="now", now=t1)
    assert flush_debounced_kuma_alerts(sandbox_db, now=t1 + timedelta(seconds=120))["count"] == 0
    fl = flush_debounced_kuma_alerts(sandbox_db, now=t1 + timedelta(seconds=185))
    assert fl["count"] == 1 and "kuma:nas" in fl["flushed"]

    r_rec = push_external(sandbox_db, "【主机监控】NAS：恢复", rule_id="uptime_kuma", ref_id="kuma:nas", level="now", now=t1 + timedelta(seconds=300))
    assert r_rec["delivered"] is True

    # 3. X 监控严格保持实时不防抖
    r_x = push_external(sandbox_db, "X 私信：1-2", rule_id="x_dm", ref_id="x:dm:1", level="now", now=t1)
    assert r_x["delivered"] is True


def test_incident_09_omamail_gmail_backoff_and_icloud_renewal_dedup(
    sandbox_db: SimingDB, frozen_clock
) -> None:
    """事故 9（Gmail 偶发超时退避回落缓存 + iCloud+ 续期变体归一与自动收口过期旧周期）。"""
    frozen_clock("2026-11-21T08:00:00+08:00")
    cache_dir = sandbox_db.paths.tmp_root / "omamail_cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    fake_om = sandbox_db.paths.tmp_root / "fake_omamail"
    fake_om.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_om.chmod(0o755)
    now_ms = int(datetime.now(tz=SHANGHAI_TZ).timestamp() * 1000)
    (cache_dir / "account-gmail.json").write_text(
        json.dumps(
            {
                "account": "u@gmail.com",
                "queries": {
                    "inbox": {
                        "at": now_ms - 600_000,  # 10 分钟前新鲜缓存
                        "summaries": [],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    def _timeout_runner(args, timeout=15, domestic=False, stdin_json=None):
        if args[:2] == ["accounts", "list"]:
            return {"ok": True, "result": {"accounts": [{"id": "gmail:1", "provider": "gmail", "email": "u@gmail.com"}]}}
        if args and args[0] == "list":
            return {"ok": False, "error": {"code": "timeout"}}
        return {"ok": True, "result": {}}

    # 第 1 次偶发超时 -> 静默退避用缓存（ok=True，不误报红灯）
    r1 = collect_omamail(sandbox_db, cli_runner=_timeout_runner, om_bin_override=fake_om, cache_dir_override=cache_dir)
    assert r1["ok"] is True
    assert any("缓存退避" in s for s in r1["accounts"])

    # 连续第 3 次超时 -> 升级为真实 ERROR
    sandbox_db.set_meta("omamail_backoff:gmail:1", json.dumps({"fails": 2, "until_ts": 0, "last_err": "timeout"}))
    r3 = collect_omamail(sandbox_db, cli_runner=_timeout_runner, om_bin_override=fake_om, cache_dir_override=cache_dir)
    assert r3["ok"] is False
    assert any("timeout" in s for s in r3["degraded_accounts"])

    # iCloud+ 变体标题归一 + 过期旧周期自动关单：11-17 建 11-20 周期，11-21 新周期到达自动关旧留新
    frozen_clock("2026-11-17T08:00:00+08:00")
    c1 = _try_ingest_subscription_renewal(
        sandbox_db,
        subject="您的 iCloud+ 方案将在 2026 年 11 月 20 日续期",
        body_text="iCloud+（含 50 GB 储存空间）（月度） ¥6.00 将在 2026 年 11 月 20 日续期",
        msg_date_iso="2026-11-17T08:00:00+08:00",
        event_id="EV-IC-1",
    )
    assert c1 is not None
    frozen_clock("2026-11-21T08:00:00+08:00")
    c2 = _try_ingest_subscription_renewal(
        sandbox_db,
        subject="关于包含 50 GB 储存空间的 iCloud+（月度）续期通知",
        body_text="包含 50 GB 储存空间的 iCloud+（月度） ¥6.00 将在 2026 年 12 月 21 日续期",
        msg_date_iso="2026-11-21T08:00:00+08:00",
        event_id="EV-IC-2",
    )
    assert c2 is not None
    conn = sandbox_db.connect()
    try:
        open_ic = conn.execute(
            "SELECT case_id, due_at FROM cases WHERE category='subscription_renew' AND status='待处理'"
        ).fetchall()
        assert len(open_ic) == 1
        assert open_ic[0]["due_at"] == "2026-12-21"
    finally:
        conn.close()


def test_incident_10_d1_hermes_handoff_fallback_model_usage_and_d2_sunday_audit(
    sandbox_db: SimingDB,
) -> None:
    """事故 10（D1-B Hermes 自动代付主模型 404 切备用模型重试 + usage 记账；D2-C 周日自省观察卡）。"""
    secret_marker = "TOP_SECRET_MARKER_NEVER_LEAK_948271"
    instr_f = sandbox_db.paths.tmp_root / "opaque_pay_instr.md"
    instr_f.write_text(f"secret={secret_marker}\n", encoding="utf-8")
    fake_hermes = sandbox_db.paths.tmp_root / "fake_hermes"
    fake_hermes.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_hermes.chmod(0o755)

    policy = {
        "meituan_pay": {
            "hermes_handoff": {
                "enabled": True,
                "provider": "redpill",
                "model": "phala/primary-gone",
                "fallback_models": ["phala/backup-ok"],
                "instruction_file": str(instr_f),
                "hermes_bin": str(fake_hermes),
                "timeout_sec": 30,
                "max_turns": 6,
            }
        }
    }
    seen_models: list[str] = []

    def _fake_run(cmd, env=None, **kwargs):
        m = (env or {}).get("SIMING_MT_MODEL", "")
        seen_models.append(m)
        if m == "phala/primary-gone":
            return SimpleNamespace(returncode=1, stdout=b"", stderr=b"HTTP 404 no route for model")
        return SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")

    res = _run_hermes_confidential_payment_handoff(
        ctx={
            "target_id": "TAB-1",
            "shop": "肯德基",
            "order_id": "MT-999",
            "verified_yuan": 28.5,
            "max_yuan": 120.0,
            "cashier_url": "https://cashier.meituan.com/test",
        },
        db=sandbox_db,
        policy=policy,
        subprocess_run_fn=_fake_run,
        verify_cashier_fn=lambda t, p: {"ok": True, "paid": len(seen_models) >= 2, "payable_yuan": 28.5, "url": "https://cashier.meituan.com/ok"},
        verify_olist_fn=lambda s, oid, p: {"found": True, "paid": True, "shop": s, "status": "商家已接单"},
    )
    assert res["paid"] is True
    assert res["used_model"] == "phala/backup-ok"
    assert res["attempted_models"] == ["phala/primary-gone", "phala/backup-ok"]
    assert secret_marker not in json.dumps(res, ensure_ascii=False)

    # 验证 usage 表落库 + D2-C 周日自省卡汇总（且不自动停用任何模块）
    conn = sandbox_db.connect()
    try:
        u_rows = conn.execute("SELECT model, runner FROM usage WHERE runner='hermes_handoff'").fetchall()
        assert len(u_rows) == 2
    finally:
        conn.close()

    concise, full = build_sunday_health_audit(sandbox_db, now=_dt("2026-11-22 08:15"))
    assert "周日健康自省" in concise
    assert "近7天后台模型 2 次" in concise
    assert "仅观测统计，不自动停用模块" in full
