"""2026-11-23 网购保价失效修复回归测试：
1. `buy-price-protect` (1f669ff55e60) 已纳入 cron_harvest.HARVEST_JOBS，deliver=local 不再丢消息；
2. 国补/券后订单：对比页面标价 jdPrice 时用下单页面价(buyPrice)/首见价(firstSeen)而非国补后实付 paid；
3. 若页面抓到券后到手价(promoPrice/finalPrice)，同步对比实付 paid；
4. 官方价保页只读探测发现登录过期（plogin.m.jd.com）时不再假绿，标 AUTH_EXPIRED 并每日推一次告警；
5. 官方价保页只读探测发现可申请退差（含不在 orders.json 的新单）时准确推 QQ；
6. buy_track 上游登录态过期（jd:empty()）时每日推一次 QQ 提醒。
"""

from __future__ import annotations

import json
from pathlib import Path

from siming.collectors.buy_track import collect_buy_track
from siming.cron_harvest import HARVEST_JOBS
from siming.db import SimingDB
from siming.price_watch import collect_price_watch, list_price_watches


def _queued_effects(db: SimingDB, rule_id: str) -> list[dict]:
    conn = db.connect()
    try:
        rows = conn.execute("SELECT payload_json FROM effects WHERE kind='send_qq'").fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        p = json.loads(r["payload_json"])
        if p.get("rule_id") == rule_id:
            out.append(p)
    return out


def test_buy_price_protect_cron_is_harvested() -> None:
    assert "1f669ff55e60" in HARVEST_JOBS
    assert HARVEST_JOBS["1f669ff55e60"]["name"] == "buy-price-protect"


def test_subsidy_order_uses_page_baseline_and_first_seen(
    sandbox_db: SimingDB, tmp_path: Path, frozen_clock
) -> None:
    frozen_clock("2026-11-23T04:50:00+08:00")
    wdir = tmp_path / "pw"
    wdir.mkdir()
    (wdir / "orders.json").write_text(
        json.dumps(
            {
                "min_diff": 10,
                "auto_claim": False,
                "orders": [
                    {"name": "小米电视 S Pro 85", "order": "9000000000000004", "sku": "100211053573", "date": "2026-11-03", "paid": 5574.05},
                    {"name": "格力云佳Pro #1", "order": "9000000000000005", "sku": "100212513162", "date": "2026-11-09", "paid": 2390.49, "baseline": "first_seen"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (wdir / "state.json").write_text(
        json.dumps(
            {
                "9000000000000004": {"buyPrice": 6587, "fact": 5574.05, "ppDays": 30, "last": {"at": "2026-11-23T03:25:00+08:00", "jdPrice": 6234}},
                "9000000000000005": {"firstSeen": 2999, "ppDays": 30, "last": {"at": "2026-11-23T03:25:00+08:00", "jdPrice": 2899}},
            }
        ),
        encoding="utf-8",
    )

    rep = list_price_watches(sandbox_db, watch_dir=wdir, auto_json_path=tmp_path / "no_auto.json")
    assert rep["claimable_count"] == 2
    by_ord = {it["order"]: it for it in rep["items"]}
    assert by_ord["9000000000000004"]["diff_cents"] == 35300
    assert by_ord["9000000000000005"]["diff_cents"] == 10000


def test_official_probe_auth_expired_alerts_once_and_marks_source_error(
    sandbox_db: SimingDB, tmp_path: Path, frozen_clock
) -> None:
    frozen_clock("2026-11-23T04:50:00+08:00")
    wdir = tmp_path / "pw2"
    wdir.mkdir()
    (wdir / "orders.json").write_text(json.dumps({"min_diff": 10, "orders": []}), encoding="utf-8")
    (wdir / "state.json").write_text("{}", encoding="utf-8")

    def _gated_login() -> dict:
        return {
            "ok": False,
            "gated": True,
            "gate": {
                "gate_kind": "login_required",
                "reason": "页面需要本人登录授权",
                "url": "https://plogin.m.jd.com/login/login?appid=461",
            },
        }

    res1 = collect_price_watch(sandbox_db, watch_dir=wdir, auto_json_path=tmp_path / "none.json", official_cdp_probe=_gated_login)
    assert res1["ok"] is False and res1["auth_expired"] is True
    conn = sandbox_db.connect()
    src = conn.execute("SELECT auth_state, freshness FROM sources WHERE id='price_watch'").fetchone()
    conn.close()
    assert src["auth_state"] == "AUTH_EXPIRED" and src["freshness"] == "ERROR"
    assert len(_queued_effects(sandbox_db, "price_protect_auth")) == 1

    collect_price_watch(sandbox_db, watch_dir=wdir, auto_json_path=tmp_path / "none.json", official_cdp_probe=_gated_login)
    assert len(_queued_effects(sandbox_db, "price_protect_auth")) == 1


def test_official_probe_detects_claimable_order_even_outside_orders_json(
    sandbox_db: SimingDB, tmp_path: Path, frozen_clock
) -> None:
    frozen_clock("2026-11-23T04:50:00+08:00")
    wdir = tmp_path / "pw3"
    wdir.mkdir()
    (wdir / "orders.json").write_text(json.dumps({"min_diff": 10, "orders": []}), encoding="utf-8")
    (wdir / "state.json").write_text("{}", encoding="utf-8")

    def _logged_in_with_refund() -> dict:
        return {
            "ok": True,
            "value": json.dumps(
                {
                    "url": "https://msitepp-fm.jd.com/rest/priceprophone/priceProPhoneMenu?defaultViewTab=0",
                    "title": "价格保护",
                    "text": "价格保护\n索尼 PS5 手柄 午夜黑\n订单号 36889900112233\n可申请价保 预计可退 ¥45.50\n历史记录\n格力空调 价保成功 3.96元",
                },
                ensure_ascii=False,
            ),
        }

    res = collect_price_watch(sandbox_db, watch_dir=wdir, auto_json_path=tmp_path / "none.json", official_cdp_probe=_logged_in_with_refund)
    assert res["ok"] is True and res["claimable"] == 1 and res["alerted"] == 1
    alerts = _queued_effects(sandbox_db, "price_protect_watch")
    assert len(alerts) == 1 and "36889900112233" in alerts[0]["text"] and "45.50" in alerts[0]["text"]


def test_buy_track_auth_expired_alerts_once_per_day(
    sandbox_db: SimingDB, tmp_path: Path, frozen_clock
) -> None:
    frozen_clock("2026-11-23T04:50:00+08:00")
    auto_p = tmp_path / "auto.json"
    auto_p.write_text(
        json.dumps({"sources": {"jd": {"last": {"status": "auth"}, "why": "需重新登录"}, "tb": {"last": {"status": "ok"}}}, "items": []}),
        encoding="utf-8",
    )
    r1 = collect_buy_track(sandbox_db, auto_json_path=auto_p)
    assert r1["ok"] is False and r1["auth_expired"] is True
    assert len(_queued_effects(sandbox_db, "buy_track_auth")) == 1
    collect_buy_track(sandbox_db, auto_json_path=auto_p)
    assert len(_queued_effects(sandbox_db, "buy_track_auth")) == 1


def test_bogus_tiny_promo_price_is_rejected(
    sandbox_db: SimingDB, tmp_path: Path, frozen_clock
) -> None:
    """防回归：PLUS 1元 / 补贴 180元 等低于页面标价 45% 的异常 promoPrice 绝不触发误报，真实券后价正常触发。"""
    frozen_clock("2026-11-23T10:15:00+08:00")
    wdir = tmp_path / "pw_promo"
    wdir.mkdir()
    (wdir / "orders.json").write_text(
        json.dumps(
            {
                "min_diff": 10,
                "auto_claim": False,
                "orders": [
                    {"name": "格力云佳Pro #1", "order": "9000000000000005", "sku": "100212513162", "date": "2026-11-09", "paid": 2390.49, "baseline": "first_seen"},
                    {"name": "小米电视 S Pro 85", "order": "9000000000000004", "sku": "100211053573", "date": "2026-11-03", "paid": 5574.05},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    (wdir / "state.json").write_text(
        json.dumps(
            {
                "9000000000000005": {"firstSeen": 2999, "ppDays": 30, "last": {"at": "2026-11-23T09:26:00+08:00", "jdPrice": 2999, "promoPrice": 1}},
                "9000000000000004": {"buyPrice": 6587, "fact": 5574.05, "ppDays": 30, "last": {"at": "2026-11-23T09:26:00+08:00", "jdPrice": 7299, "promoPrice": 180}},
            }
        ),
        encoding="utf-8",
    )
    rep = list_price_watches(sandbox_db, watch_dir=wdir, auto_json_path=tmp_path / "no_auto.json")
    assert rep["claimable_count"] == 0
    for it in rep["items"]:
        assert it["promo_cents"] is None

    # 真实券后到手价（5400，占页面价 7299 的 74%）仍能正常命中并低于实付 5574.05
    (wdir / "state.json").write_text(
        json.dumps(
            {
                "9000000000000005": {"firstSeen": 2999, "ppDays": 30, "last": {"at": "2026-11-23T09:26:00+08:00", "jdPrice": 2999}},
                "9000000000000004": {"buyPrice": 6587, "fact": 5574.05, "ppDays": 30, "last": {"at": "2026-11-23T09:26:00+08:00", "jdPrice": 7299, "promoPrice": 5400}},
            }
        ),
        encoding="utf-8",
    )
    rep2 = list_price_watches(sandbox_db, watch_dir=wdir, auto_json_path=tmp_path / "no_auto.json")
    assert rep2["claimable_count"] == 1
    assert rep2["claimable_items"][0]["diff_cents"] == 17405


def test_cron_harvest_dedups_price_drop_when_price_watch_enabled(
    sandbox_db: SimingDB, monkeypatch
) -> None:
    import siming.router as router
    from siming.cron_harvest import harvest_cron_outputs

    monkeypatch.setattr(router, "_in_quiet_hours", lambda policy, db=None: False)
    tmp = sandbox_db.paths.tmp_root / "cron_pw_dedup"
    out_dir = tmp / "output" / "1f669ff55e60"
    out_dir.mkdir(parents=True, exist_ok=True)
    jobs_json = tmp / "jobs.json"
    jobs_json.write_text(json.dumps({"jobs": [{"id": "1f669ff55e60", "deliver": "local"}]}), encoding="utf-8")

    # 首次只记游标
    (out_dir / "2026-11-23_06-25-00.md").write_text("**Status:** silent\n", encoding="utf-8")
    harvest_cron_outputs(sandbox_db, output_root=tmp / "output", jobs_json=jobs_json)

    # 写入纯降价提醒 -> price_watch 已开启时跳过，避免双推！
    drop_md = "# Cron Job: buy-price-protect\n\n---\n\n🔻 小米电视：页面价 ¥6234\n仅提醒，不自动申请。\n"
    (out_dir / "2026-11-23_07-25-00.md").write_text(drop_md, encoding="utf-8")
    res_drop = harvest_cron_outputs(sandbox_db, output_root=tmp / "output", jobs_json=jobs_json)
    assert res_drop["pushed"] == []

    # 写入冷却预警 -> 必须正常转发！
    cool_md = "# Cron Job: buy-price-protect\n\n---\n\n⏸ 京东要求验证；已停止全部京东自动访问\n"
    (out_dir / "2026-11-23_08-25-00.md").write_text(cool_md, encoding="utf-8")
    res_cool = harvest_cron_outputs(sandbox_db, output_root=tmp / "output", jobs_json=jobs_json)
    assert len(res_cool["pushed"]) == 1


def test_price_watch_auth_expired_auto_relogin_recovers_without_waiting(
    sandbox_db: SimingDB, tmp_path: Path, frozen_clock
) -> None:
    frozen_clock("2026-11-23T10:20:00+08:00")
    wdir = tmp_path / "pw_auto_ok"
    wdir.mkdir()
    (wdir / "orders.json").write_text(json.dumps({"min_diff": 10, "orders": []}), encoding="utf-8")
    (wdir / "state.json").write_text("{}", encoding="utf-8")

    state = {"logged_in": False, "relogin_calls": 0}

    def _probe() -> dict:
        if not state["logged_in"]:
            return {
                "ok": False,
                "gated": True,
                "gate": {"gate_kind": "login_required", "reason": "跳转登录", "url": "https://plogin.m.jd.com/login/login"},
            }
        return {
            "ok": True,
            "value": json.dumps({"url": "https://msitepp-fm.jd.com/rest/priceprophone/priceProPhoneMenu", "title": "价格保护", "text": "暂无差价"}),
        }

    def _relogin(db: SimingDB, plat: str) -> dict:
        state["relogin_calls"] += 1
        assert plat == "jd"
        state["logged_in"] = True
        return {"ok": True, "brand": "京东", "reply": "✅ 已自动填入验证码登录"}

    res = collect_price_watch(
        sandbox_db,
        watch_dir=wdir,
        auto_json_path=tmp_path / "none.json",
        official_cdp_probe=_probe,
        relogin_runner=_relogin,
    )
    assert state["relogin_calls"] == 1
    assert res["ok"] is True and res["auth_expired"] is False
    conn = sandbox_db.connect()
    src = conn.execute("SELECT auth_state, freshness FROM sources WHERE id='price_watch'").fetchone()
    conn.close()
    assert src["auth_state"] == "OK" and src["freshness"] == "OK"
    msgs = _queued_effects(sandbox_db, "price_protect_auth")
    assert len(msgs) == 1 and "续登成功" in msgs[0]["text"]


def test_price_watch_auth_expired_slider_gate_hands_off_and_respects_cooldown(
    sandbox_db: SimingDB, tmp_path: Path, frozen_clock
) -> None:
    frozen_clock("2026-11-23T10:25:00+08:00")
    wdir = tmp_path / "pw_auto_gate"
    wdir.mkdir()
    (wdir / "orders.json").write_text(json.dumps({"min_diff": 10, "orders": []}), encoding="utf-8")
    (wdir / "state.json").write_text("{}", encoding="utf-8")

    calls = {"n": 0}

    def _probe() -> dict:
        return {
            "ok": False,
            "gated": True,
            "gate": {"gate_kind": "login_required", "reason": "跳转登录", "url": "https://plogin.m.jd.com/login/login"},
        }

    def _relogin_gated(db: SimingDB, plat: str) -> dict:
        calls["n"] += 1
        return {"ok": False, "gated": True, "reply": "触发滑块"}

    r1 = collect_price_watch(
        sandbox_db, watch_dir=wdir, auto_json_path=tmp_path / "none.json", official_cdp_probe=_probe, relogin_runner=_relogin_gated
    )
    assert r1["ok"] is False and r1["auth_expired"] is True and calls["n"] == 1
    msgs = _queued_effects(sandbox_db, "price_protect_auth")
    assert len(msgs) == 1 and "滑块验证" in msgs[0]["text"] and "填验证码 京东" in msgs[0]["text"]

    # 冷却期内第二次 tick 绝不重复发短信触发登录
    collect_price_watch(
        sandbox_db, watch_dir=wdir, auto_json_path=tmp_path / "none.json", official_cdp_probe=_probe, relogin_runner=_relogin_gated
    )
    assert calls["n"] == 1


def test_buy_track_auto_relogin_and_fresh_probe_kicks_sync(
    sandbox_db: SimingDB, tmp_path: Path, frozen_clock
) -> None:
    frozen_clock("2026-11-23T10:30:00+08:00")
    auto_p = tmp_path / "auto_bt.json"
    auto_p.write_text(
        json.dumps({"sources": {"jd": {"last": {"status": "auth"}, "why": "需重新登录"}, "tb": {"last": {"status": "ok"}}}, "items": []}),
        encoding="utf-8",
    )
    kicks: list[bool] = []
    relogins: list[str] = []

    res1 = collect_buy_track(
        sandbox_db,
        auto_json_path=auto_p,
        relogin_runner=lambda db, p: (relogins.append(p), {"ok": True, "reply": "ok"})[1],
        sync_kicker=lambda db, rec: (kicks.append(rec), True)[1],
    )
    assert relogins == ["jd"] and kicks == [True] and res1["kicked_refresh"] is True

    # 当官方价保页刚确认京东已在线但 auto.json 仍是旧 empty 快照 -> 直接踢 buy-track 刷新，不重发短信也不骚扰用户
    auto_empty = tmp_path / "auto_empty.json"
    auto_empty.write_text(
        json.dumps({"sources": {"jd": {"last": {"status": "empty"}, "why": ""}, "tb": {"last": {"status": "ok"}}}, "items": []}),
        encoding="utf-8",
    )
    sandbox_db.set_meta(
        "price_watch_official_snapshot",
        json.dumps({"ok": True, "auth_expired": False, "claimable": [], "checked_at": "2026-11-23T10:28:00+08:00"}),
    )
    res2 = collect_buy_track(
        sandbox_db,
        auto_json_path=auto_empty,
        relogin_runner=lambda db, p: (relogins.append("should_not_run"), {"ok": True})[1],
        sync_kicker=lambda db, rec: (kicks.append(rec), True)[1],
    )
    assert relogins == ["jd"] and len(kicks) == 2 and res2["kicked_refresh"] is True
