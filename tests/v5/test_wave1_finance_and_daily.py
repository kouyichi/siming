"""Siming v5 Wave 1 (W1-1 ~ W1-7) 综合单元测试与集成测试。

覆盖：
- W1-1: Sure 财务 OS 控制塔双向同步桥 + Pipelock A2 护栏 + Compose/Pipelock 部署校验 + 1秒自然语言查账
- W1-2: AI 算力资产总控（2x 烧钱熔断 CIRCUIT_OPEN、低余额、套餐到期、404 自动降级路由、Sure 公私成本同步）
- W1-3: 订阅刺客猎手（周期推断、涨价 >5% 识别、僵尸候选、48h 止损卡与按钮、状态标记）
- W1-4: 饮食节律守卫（夜猫子睡眠尊重、近5h进食去扰、单窗防重）+ 跨店双拼点餐协调与 A2 捆绑审批
- W1-5: 丰巢 18h 防超期倒计时（仅 <=120m 且在家/未知触发、单件去重）+ 7天无理由退货与运费险哨兵
- W1-6: 私人健康驾驶舱（化验单 OCR 生化指标抽取与高低异常标注、周期用药/体重/睡眠债联动、常用医院一页医生简报）
- W1-7: 车务与补能全链路（ETC/停车/超充归集与公私分账、Model Y LFP 每周100%校准与冬季能效、车险/12123违章、同步至 Sure 车辆账户）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from siming.attention import record_explicit_sleep_state
from siming.compute_assets import (
    evaluate_compute_guardrails,
    format_compute_dashboard,
    recommend_healthy_model,
    sync_compute_costs_to_sure,
    upsert_compute_asset,
)
from siming.db import SHANGHAI_TZ, SimingDB, iso_now
from siming.health_cockpit import (
    generate_doctor_brief,
    get_health_trends,
    ingest_health_text,
    parse_lab_ocr_text,
    record_health_metric,
)
from siming.meal_guard import (
    evaluate_meal_regularity,
    format_combo_plan,
    plan_dual_shop_combo,
    stage_dual_shop_combo_approval,
)
from siming.parcel_sentinel import (
    evaluate_hivebox_countdown,
    evaluate_return_window_sentinel,
    format_sentinel_report,
    run_parcel_sentinel,
)
from siming.sub_hunter import (
    emit_subscription_stoploss_alerts,
    find_upcoming_charges,
    format_subscription_audit,
    mark_subscription_status,
    scan_subscriptions,
)
from siming.sure_bridge import (
    ask_sure_natural,
    format_sure_summary,
    get_sure_status,
    pipelock_authorize,
    sync_fixed_asset_to_sure,
    sync_ledger_to_sure,
    upsert_sure_rule,
    list_sure_rules,
)
from siming.vehicle_cfo import (
    evaluate_lfp_calibration_and_insurance,
    format_vehicle_report,
    harvest_vehicle_costs_from_ledger,
    record_vehicle_event,
    sync_vehicle_to_sure,
)


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI_TZ)


def _insert_txn(
    db: SimingDB,
    txn_id: str,
    occurred_at: str,
    amount_cents: int,
    merchant: str,
    account_alias: str = "建行1004",
    card_last4: str = "1004",
    kind: str = "消费",
    notes: str = "",
    currency: str = "CNY",
) -> None:
    ts = iso_now()
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO ledger_txns (
                txn_id, occurred_at, amount_cents, currency, account_alias,
                card_last4, kind, status, merchant, notes, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'CONFIRMED', ?, ?, ?, ?)
            """,
            (txn_id, occurred_at, amount_cents, currency, account_alias, card_last4, kind, merchant, notes, ts, ts),
        )


def _outbox_rows(db: SimingDB) -> list[dict]:
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM outbox ORDER BY created_at ASC").fetchall()]
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# W1-1: Sure 财务 OS 控制塔 & Pipelock A2 护栏
# ---------------------------------------------------------------------------


def test_w1_1_sure_deploy_files_and_pipelock_guardrails(sandbox_db: SimingDB) -> None:
    compose = Path(sandbox_db.paths.repo_root) / "deploy" / "sure" / "docker-compose.yml"
    pipelock = Path(sandbox_db.paths.repo_root) / "deploy" / "sure" / "pipelock.yaml"
    assert compose.is_file()
    assert pipelock.is_file()

    compose_txt = compose.read_text(encoding="utf-8")
    assert "ghcr.io/we-promise/sure:latest" in compose_txt
    assert "postgres:16" in compose_txt
    assert "sidekiq" in compose_txt
    assert "sure-mcp-server" in compose_txt
    assert "pipelock" in compose_txt
    assert "127.0.0.1:3030:3000" in compose_txt
    assert "127.0.0.1:3032:8080" in compose_txt

    pipelock_txt = pipelock.read_text(encoding="utf-8")
    assert "read_only" in pipelock_txt
    assert "X-Siming-Approval-Token" in pipelock_txt

    # Pipelock A0 只读放行，写/转/删强制 A2 审批口令
    r_read = pipelock_authorize("read")
    assert r_read["allowed"] is True and r_read["tier"] == "A0" and r_read["requires_a2"] is False

    r_write_denied = pipelock_authorize("write", amount_yuan=120.0)
    assert r_write_denied["allowed"] is False and r_write_denied["reason"] == "a2_approval_required"

    r_del_denied = pipelock_authorize("delete", amount_yuan=0.0, payload={"a2_token": "BAD"})
    assert r_del_denied["allowed"] is False and r_del_denied["reason"] == "invalid_a2_token_format"

    r_egress_denied = pipelock_authorize("read", payload={"url": "https://evil.example.com/leak"})
    assert r_egress_denied["allowed"] is False and r_egress_denied["reason"] == "egress_blocked_local_only"

    r_write_ok = pipelock_authorize("transfer", amount_yuan=888.0, payload={"a2_token": "A042"})
    assert r_write_ok["allowed"] is True and r_write_ok["tier"] == "A2" and r_write_ok["approval_token"] == "A042"

    r_over_cap = pipelock_authorize("transfer", amount_yuan=99999.0, payload={"a2_token": "A042"})
    assert r_over_cap["allowed"] is False and r_over_cap["reason"] == "exceeds_pipelock_single_cap"


def test_w1_1_sure_sync_rules_assets_and_natural_query(sandbox_db: SimingDB, frozen_clock) -> None:
    frozen_clock("2026-11-20T15:00:00+08:00")

    # 1. 创建规则并校验幂等更新
    rule1 = upsert_sure_rule(
        sandbox_db,
        name="山姆家庭分账",
        match_pattern="山姆会员店",
        account_name="中行信用卡(1003)",
        category="家居商超",
        tags=["家庭", "AA分摊"],
        split_ratio=0.5,
    )
    assert rule1["ok"] is True
    rule1_upd = upsert_sure_rule(
        sandbox_db,
        name="山姆家庭分账",
        match_pattern="山姆",
        account_name="中行信用卡(1003)",
        category="家居商超",
        tags=["家庭", "AA分摊"],
        split_ratio=0.5,
    )
    assert rule1_upd["rule_id"] == rule1["rule_id"]
    assert len(list_sure_rules(sandbox_db)) == 1

    # 2. 种入多笔四卡账本流水（含 CW08、CW01、私人分摊、收入）
    _insert_txn(sandbox_db, "TX1", "2026-11-10T10:00:00+08:00", 50000, "Nous API Auto-Refill", "光大1001", "1001", notes="work_cw08")
    _insert_txn(sandbox_db, "TX2", "2026-11-15T22:10:00+08:00", 4500, "滴滴出行", "建行1004", "1004", notes="加班打车 CW01")
    _insert_txn(sandbox_db, "TX3", "2026-11-18T18:30:00+08:00", 40000, "山姆会员商店", "中行1003", "1003")
    _insert_txn(sandbox_db, "TX4", "2026-11-05T09:00:00+08:00", 3000000, "工资入账", "建行1004", "1004", kind="收入")

    # dry_run 不落库
    dry = sync_ledger_to_sure(sandbox_db, dry_run=True)
    assert dry["ok"] is True and dry["would_sync"] == 4 and dry["synced"] == 0

    calls: list[tuple[str, str, dict]] = []

    def _mock_http(method: str, url: str, payload, headers):
        calls.append((method, url, payload or {}))
        if url.endswith("/health"):
            return {"ok": True, "status": 200, "data": {"status": "ok"}}
        return {"ok": True, "status": 201, "data": {"id": f"remote-{len(calls)}"}}

    # 首次全量同步
    res1 = sync_ledger_to_sure(sandbox_db, http_fn=_mock_http)
    assert res1["ok"] is True and res1["synced"] == 4 and res1["skipped"] == 0
    by_id = {it["txn_id"]: it for it in res1["items"]}
    assert "CW08" in by_id["TX1"]["tags"] and "public" in by_id["TX1"]["tags"]
    assert "CW01" in by_id["TX2"]["tags"]
    assert by_id["TX3"]["amount_yuan"] == 200.0  # 400 * 0.5 split_ratio
    assert by_id["TX3"]["account_name"] == "中行信用卡(1003)"

    # 二次幂等同步应全部跳过
    res2 = sync_ledger_to_sure(sandbox_db, http_fn=_mock_http)
    assert res2["ok"] is True and res2["synced"] == 0 and res2["skipped"] == 4

    # 3. 固定资产同步
    asset_res = sync_fixed_asset_to_sure(
        sandbox_db,
        asset_id="APP-WASHER-01",
        name="西门子洗烘套装",
        purchase_yuan=12000.0,
        current_yuan=10200.0,
        category="appliance",
        http_fn=_mock_http,
    )
    assert asset_res["ok"] is True
    assert asset_res["depreciation_yuan"] == 1800.0
    assert asset_res["depreciation_pct"] == 15.0

    # 4. 状态与自然语言 1 秒问答
    st = get_sure_status(sandbox_db, http_fn=_mock_http)
    assert st["compose_ready"] is True and st["pipelock_ready"] is True
    assert st["api_healthy"] is True and st["synced_txns"] == 4 and st["pending_txn_count"] == 0
    assert "Sure 财务 OS 控制塔" in format_sure_summary(sandbox_db)

    q_nw = ask_sure_natural(sandbox_db, "我现在的净资产是多少？")
    assert q_nw["ok"] is True and q_nw["intent"] == "net_worth"
    assert q_nw["data"]["fixed_assets_yuan"] == 10200.0

    q_reimb = ask_sure_natural(sandbox_db, "查一下 CW08 和 CW01 报销账龄")
    assert q_reimb["ok"] is True and q_reimb["intent"] == "reimbursement_aging"
    assert q_reimb["data"]["cw08_yuan"] == 500.0
    assert q_reimb["data"]["cw01_yuan"] == 45.0
    assert q_reimb["data"]["oldest_reimburse_days"] >= 10

    q_cf = ask_sure_natural(sandbox_db, "本月现金流和分类支出")
    assert q_cf["ok"] is True and q_cf["data"]["inflow_yuan"] == 30000.0
    assert q_cf["data"]["outflow_yuan"] == 745.0  # 500 + 45 + 200(split)


# ---------------------------------------------------------------------------
# W1-2: AI 算力资产总控
# ---------------------------------------------------------------------------


def test_w1_2_compute_guardrails_circuit_breaker_and_fallback(sandbox_db: SimingDB, frozen_clock) -> None:
    now = frozen_clock("2026-11-20T14:00:00+08:00")

    # 正常健康 DeepSeek 账户（个人）
    upsert_compute_asset(
        sandbox_db,
        provider="deepseek",
        account_label="main",
        balance=580.0,
        currency="CNY",
        daily_spend=12.0,
        avg_daily_spend=10.0,
        billing_scope="personal",
    )
    # 烧钱飙升 3.2x -> 应触发 CIRCUIT_OPEN（公费 CW08）
    upsert_compute_asset(
        sandbox_db,
        provider="nous",
        account_label="lab-cw08",
        balance=120.0,
        currency="USD",
        daily_spend=64.0,
        avg_daily_spend=20.0,
        billing_scope="work_cw08",
        fallback_model="deepseek-chat",
    )
    # 低余额 + 节省计划 7 天后到期
    upsert_compute_asset(
        sandbox_db,
        provider="aliyun",
        account_label="coding-plan",
        balance=95.0,
        currency="CNY",
        daily_spend=8.0,
        avg_daily_spend=8.0,
        plan_name="百炼 Coding 节省计划",
        plan_expires_at="2026-11-27",
        billing_scope="personal",
    )

    guard = evaluate_compute_guardrails(sandbox_db, now=now, notify=True)
    assert guard["ok"] is True
    assert len(guard["circuit_open"]) == 1
    assert guard["circuit_open"][0]["provider"] == "nous"
    assert guard["circuit_open"][0]["fallback_model"] == "deepseek-chat"
    assert len(guard["low_balance"]) == 1 and guard["low_balance"][0]["provider"] == "aliyun"
    assert len(guard["expiring"]) == 1 and guard["expiring"][0]["days_left"] == 7
    assert guard["notified"] is True
    assert any("CIRCUIT_OPEN" in r["text"] for r in _outbox_rows(sandbox_db))

    # 熔断平台自动降级路由
    rec_circuit = recommend_healthy_model(sandbox_db, "nous", "hermes-4-70b")
    assert rec_circuit["switched"] is True
    assert rec_circuit["provider"] == "deepseek"
    assert rec_circuit["model"] == "deepseek-chat"
    assert "circuit_open" in rec_circuit["reason"]

    # 404 自动绕行到健康模型
    rec_404 = recommend_healthy_model(sandbox_db, "claude", "claude-3-7-sonnet-404", error_code=404)
    assert rec_404["switched"] is True
    assert rec_404["provider"] == "deepseek"
    assert "http_404" in rec_404["reason"]

    # 健康模型不切换
    rec_ok = recommend_healthy_model(sandbox_db, "deepseek", "deepseek-chat", error_code=200)
    assert rec_ok["switched"] is False

    # 月度公私算力成本同步至 Sure
    sync_res = sync_compute_costs_to_sure(sandbox_db, month="2026-11")
    assert sync_res["ok"] is True
    assert sync_res["work_cw08_cny"] > 0 and sync_res["personal_cny"] > 0
    assert "AI 算力资产总控面板" in format_compute_dashboard(sandbox_db, now=now)


# ---------------------------------------------------------------------------
# W1-3: 订阅刺客猎手
# ---------------------------------------------------------------------------


def test_w1_3_subscription_assassin_hike_zombie_and_48h_stoploss(sandbox_db: SimingDB, frozen_clock) -> None:
    now = frozen_clock("2026-11-20T10:00:00+08:00")

    # 1. Apple 服务：30 天周期 + 涨价 22.2% (¥18 -> ¥22)，且下次扣费在 24h 内（2026-11-21）
    _insert_txn(sandbox_db, "SUB-A1", "2026-09-22T10:00:00+08:00", 1800, "Apple.com/bill iCloud")
    _insert_txn(sandbox_db, "SUB-A2", "2026-10-22T10:00:00+08:00", 2200, "Apple.com/bill iCloud")

    # 2. 点众小说：30 天周期，僵尸候选，下次扣费在 2026-11-21 15:00（29h 后）
    _insert_txn(sandbox_db, "SUB-D1", "2026-09-22T15:00:00+08:00", 1500, "点众小说连续包月")
    _insert_txn(sandbox_db, "SUB-D2", "2026-10-22T15:00:00+08:00", 1500, "点众小说连续包月")

    # 3. PC Game Pass：邮箱续费事件（提前预告 18h 后扣费）
    with sandbox_db.transaction() as conn:
        sandbox_db.append_event(
            conn,
            source="mail",
            thread_key="sub-pgpass",
            summary="Microsoft PC Game Pass 自动续费提醒 ¥49.00",
            payload={
                "merchant": "PC Game Pass",
                "amount_yuan": 49.0,
                "cadence_days": 30,
                "next_expected_at": "2026-11-21T04:00:00+08:00",
            },
            occurred_at="2026-11-18T09:00:00+08:00",
        )

    scan = scan_subscriptions(sandbox_db, now=now, min_occurrences=2)
    assert scan["ok"] is True and scan["detected"] >= 3
    assert any(h["normalized_name"] == "iCloud+" and h["price_hike_pct"] > 20.0 for h in scan["price_hikes"])
    assert any(z["normalized_name"] == "点众小说" for z in scan["zombies"])

    # 验证已同步生成 Sure 周期规则
    rules = list_sure_rules(sandbox_db)
    assert any(r["is_recurring"] and r["cadence_days"] == 30 for r in rules)

    # 48h 扣费预警
    upcoming = find_upcoming_charges(sandbox_db, now=now, horizon_hours=48)
    names = {u["normalized_name"] for u in upcoming}
    assert {"iCloud+", "点众小说", "PC Game Pass"}.issubset(names)

    # 推送 QQ 止损卡（含 保留 / 去取消 / 记为僵尸 按钮）
    emitted = emit_subscription_stoploss_alerts(sandbox_db, now=now)
    assert emitted["ok"] is True and emitted["alert_count"] >= 3
    first_alert = emitted["alerts"][0]
    assert "保留" in first_alert["text"] and "去取消" in first_alert["text"] and "记为僵尸" in first_alert["text"]
    assert first_alert["keyboard"] is not None

    # 标记点众小说为 CANCELLED 后，48h 预警应将其排除
    mk = mark_subscription_status(sandbox_db, "点众小说", "CANCELLED", note="已在微信扣费服务关闭")
    assert mk["ok"] is True and mk["status"] == "CANCELLED"
    upcoming_after = find_upcoming_charges(sandbox_db, now=now, horizon_hours=48)
    assert all(u["normalized_name"] != "点众小说" for u in upcoming_after)
    assert "订阅刺客审计总览" in format_subscription_audit(sandbox_db, now=now)


# ---------------------------------------------------------------------------
# W1-4: 饮食节律守卫 + 跨店双拼点餐协调与 A2 审批
# ---------------------------------------------------------------------------


def test_w1_4_meal_regularity_and_dual_shop_combo_a2(sandbox_db: SimingDB, frozen_clock) -> None:
    # 1) 窗外不打扰
    outside = evaluate_meal_regularity(sandbox_db, now=_dt("2026-11-20 10:30"), notify=True)
    assert outside["should_nudge"] is False and outside["reason"] == "outside_meal_window"

    # 2) 睡眠态不打扰
    frozen_clock("2026-11-20T13:15:00+08:00")
    record_explicit_sleep_state(sandbox_db, sleeping=True, source="qq", at_ts="2026-11-20T12:00:00+08:00")
    asleep = evaluate_meal_regularity(sandbox_db, now=_dt("2026-11-20 13:15"), notify=True)
    assert asleep["should_nudge"] is False and asleep["reason"] == "user_asleep"

    # 3) 清醒且近 5h 无餐饮消费 -> 温柔提醒一次，第二次去重
    record_explicit_sleep_state(sandbox_db, sleeping=False, source="qq", at_ts="2026-11-20T13:10:00+08:00")
    nudge1 = evaluate_meal_regularity(sandbox_db, now=_dt("2026-11-20 13:30"), notify=True)
    assert nudge1["should_nudge"] is True and nudge1["nudged"] is True
    assert nudge1["keyboard"] is not None

    nudge2 = evaluate_meal_regularity(sandbox_db, now=_dt("2026-11-20 14:00"), notify=True)
    assert nudge2["should_nudge"] is False and nudge2["reason"] == "already_nudged_this_window"

    # 4) 晚餐前 2 小时刚点过外卖 -> 不再提醒
    _insert_txn(sandbox_db, "FOOD-1", "2026-11-20T18:10:00+08:00", 3600, "美团外卖·示例瓦罐汤瓦罐汤")
    dinner = evaluate_meal_regularity(sandbox_db, now=_dt("2026-11-20 20:00"), notify=True)
    assert dinner["should_nudge"] is False and dinner["reason"] == "recent_meal_found"

    # 5) 跨店双拼点餐协调 + A2 审批捆绑包
    plan = plan_dual_shop_combo(
        sandbox_db,
        query="示例粉面炒米粉 + 示例果切西瓜",
        location="示例小区A",
        shops_meta={
            "示例粉面炒米粉": {"shop_name": "示例粉面炒米粉", "eta_min": 38, "delivery_fee": 3.5, "item_price": 30.0, "coupon_yuan": 6.0},
            "示例果切西瓜": {"shop_name": "示例鲜果切", "eta_min": 22, "delivery_fee": 2.5, "item_price": 26.0, "coupon_yuan": 5.0},
        },
    )
    assert plan["ok"] is True
    assert plan["eta_delta_min"] == 16
    assert plan["stagger_delay_min"] == 16
    assert plan["combined_delivery_fee"] == 6.0
    assert plan["coupon_savings_yuan"] == 11.0
    assert plan["estimated_total_yuan"] == 51.0
    assert "跨店双拼点餐方案" in format_combo_plan(plan)

    staged = stage_dual_shop_combo_approval(sandbox_db, "示例瓦罐汤瓦罐汤 和 瑞幸冰美式", location="公司")
    assert staged["ok"] is True
    assert staged["action"] == "meituan_combo_order"
    assert staged["token"].startswith("A")
    assert staged["bundle_id"].startswith("BND-")
    assert staged["keyboard"] is not None

    # 验证审批记录已写入 approvals 表
    conn = sandbox_db.connect()
    try:
        row = conn.execute("SELECT action, tier, status, bundle_id FROM approvals WHERE token = ?", (staged["token"],)).fetchone()
    finally:
        conn.close()
    assert row is not None and row["action"] == "meituan_combo_order" and row["tier"] == "A2" and row["status"] == "PENDING"


# ---------------------------------------------------------------------------
# W1-5: 丰巢 18h 倒计时 + 7 天无理由退货哨兵
# ---------------------------------------------------------------------------


def test_w1_5_hivebox_18h_countdown_and_7d_return_sentinel(sandbox_db: SimingDB, frozen_clock) -> None:
    now = frozen_clock("2026-11-20T18:30:00+08:00")
    # 16.5 小时前存入丰巢 -> 剩余 90 分钟（0 < 90 <= 120）
    deposited = iso_now(now - timedelta(hours=16, minutes=30))
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO cases (
                case_id, title, next_step, status, strength, category, rule_id,
                context_json, created_at, updated_at
            ) VALUES (?, ?, ?, '待处理', 'NORMAL', 'pickup', 'pickup_1800', ?, ?, ?)
            """,
            (
                "S91",
                "丰巢 · 示例小区A大堂门口丰巢柜",
                "凭取件码 884210 取件，18小时内免费",
                json.dumps({"brand": "丰巢", "code": "884210", "location": "示例小区A大堂门口丰巢柜", "free_window": "18小时内免费", "deposited_at": deposited}, ensure_ascii=False),
                deposited,
                deposited,
            ),
        )
        # 5.5 天前签收的抖音包裹 -> 7 天退货窗仅剩 36h (<=48h)
        deliv_5_5d = iso_now(now - timedelta(days=5, hours=12))
        conn.execute(
            """
            INSERT INTO cases (
                case_id, title, next_step, status, strength, category, rule_id,
                context_json, created_at, updated_at, closed_at
            ) VALUES (?, ?, ?, '完成', 'SOFT', 'parcel', 'superagent_parcel', ?, ?, ?, ?)
            """,
            (
                "S92",
                "抖音订单 · 机械键盘客制化键帽",
                "已签收",
                json.dumps({"platform": "抖音", "delivered_at": deliv_5_5d, "shipping_insurance": True}, ensure_ascii=False),
                deliv_5_5d,
                deliv_5_5d,
                deliv_5_5d,
            ),
        )

    # 人在公司（不在家）-> 不触发丰巢穿透告警
    away = evaluate_hivebox_countdown(sandbox_db, now=now, is_home=False, notify=True)
    assert len(away["tracked"]) == 1
    assert away["tracked"][0]["remaining_min"] == 90.0
    assert len(away["urgent"]) == 0 and len(away["notified"]) == 0

    # 人在家 -> 触发丰巢紧急告警，二次执行严格去重
    home1 = evaluate_hivebox_countdown(sandbox_db, now=now, is_home=True, notify=True)
    assert len(home1["urgent"]) == 1 and home1["notified"] == ["S91"]

    home2 = evaluate_hivebox_countdown(sandbox_db, now=now, is_home=True, notify=True)
    assert len(home2["notified"]) == 0

    # 7 天退货哨兵
    ret1 = evaluate_return_window_sentinel(sandbox_db, now=now, notify=True)
    assert len(ret1["expiring_soon"]) == 1
    assert ret1["expiring_soon"][0]["remaining_hours"] == 36.0
    assert ret1["notified"] == ["S92"]

    full = run_parcel_sentinel(sandbox_db, now=now, notify=False)
    assert full["ok"] is True and full["urgent_count"] == 2
    assert "包裹防超期与退货哨兵" in format_sentinel_report(sandbox_db, now=now)


# ---------------------------------------------------------------------------
# W1-6: 私人健康驾驶舱 & 常用医院一页医生简报
# ---------------------------------------------------------------------------


def test_w1_6_health_ocr_biomarkers_and_puh3_doctor_brief(sandbox_db: SimingDB, frozen_clock) -> None:
    now = frozen_clock("2026-11-20T11:00:00+08:00")

    ocr_sample = """
    常用医院检验科报告单
    空腹血糖(GLU): 6.5 mmol/L ↑
    糖化血红蛋白(HbA1c): 5.8 %
    总胆固醇(TC): 5.5 mmol/L ↑
    甘油三酯(TG): 2.1 mmol/L ↑
    低密度脂蛋白(LDL-C): 3.6 mmol/L ↑
    高密度脂蛋白(HDL-C): 0.92 mmol/L ↓
    谷丙转氨酶(ALT): 48 U/L ↑
    谷草转氨酶(AST): 32 U/L
    尿酸(UA): 465 umol/L ↑
    肌酐(Cr): 78 umol/L
    体重: 164 斤
    """

    parsed = parse_lab_ocr_text(ocr_sample)
    by_type = {p["metric_type"]: p for p in parsed}
    assert by_type["glucose"]["flag"] == "HIGH"
    assert by_type["hba1c"]["flag"] == "NORMAL"
    assert by_type["tg"]["flag"] == "HIGH"
    assert by_type["hdl"]["flag"] == "LOW"
    assert by_type["alt"]["flag"] == "HIGH"
    assert by_type["ast"]["flag"] == "NORMAL"
    assert by_type["uric_acid"]["flag"] == "HIGH"
    assert by_type["creatinine"]["flag"] == "NORMAL"
    assert by_type["weight_kg"]["value_num"] == 82.0  # 164 斤 -> 82.0 kg

    # 先种一条 30 天前的初始体重与熬夜睡眠债、以及 daily_care 周期用药注射日志
    record_health_metric(sandbox_db, "weight_kg", 86.5, measured_at="2026-10-20T08:00:00+08:00")
    record_health_metric(sandbox_db, "sleep_debt_h", 3.5, measured_at="2026-11-18T03:00:00+08:00")
    sandbox_db.set_meta(
        "care_injection_log",
        json.dumps([{"slot": "2026-11-04"}, {"slot": "2026-11-11"}, {"slot": "2026-11-18"}]),
    )
    sandbox_db.set_meta("care_pen", json.dumps({"doses_left": 1, "last_visit": "2026-10-15"}))

    ing = ingest_health_text(sandbox_db, ocr_sample, measured_at="2026-11-19T08:30:00+08:00", source_event_id="EV-LAB-1")
    assert ing["ok"] is True and ing["count"] == 11 and ing["abnormal_count"] >= 6

    trends = get_health_trends(sandbox_db, days=90, now=now)
    assert trends["weight_trend"]["start_kg"] == 86.5
    assert trends["weight_trend"]["latest_kg"] == 82.0
    assert trends["weight_trend"]["delta_kg"] == -4.5
    assert trends["periodic_med"]["injections_in_window"] == 3
    assert trends["periodic_med"]["doses_left"] == 1
    assert trends["sleep_debt"]["consecutive_high_days"] == 1

    brief = generate_doctor_brief(sandbox_db, hospital="常用医院", days=90, now=now)
    assert "常用医院 就诊一页健康简报" in brief
    assert "周期用药" in brief
    assert "86.5 kg → 82.0 kg" in brief
    assert "-4.5 kg" in brief
    assert "空腹血糖" in brief and "偏高" in brief
    assert "睡眠债" in brief


# ---------------------------------------------------------------------------
# W1-7: 车务与补能全链路（Tesla Model Y RWD LFP）
# ---------------------------------------------------------------------------


def test_w1_7_vehicle_cfo_harvest_lfp_insurance_and_sure_sync(sandbox_db: SimingDB, frozen_clock) -> None:
    now = frozen_clock("2026-11-20T16:00:00+08:00")

    # 种入账本中的超充、ETC、私人停车与公务停车
    _insert_txn(sandbox_db, "VEH-1", "2026-11-08T19:00:00+08:00", 6200, "特斯拉超级充电站·大钟寺")
    _insert_txn(sandbox_db, "VEH-2", "2026-11-12T08:40:00+08:00", 3500, "北京速通卡ETC高速通行")
    _insert_txn(sandbox_db, "VEH-3", "2026-11-14T21:00:00+08:00", 1600, "捷停车·商场停车费")
    _insert_txn(sandbox_db, "VEH-4", "2026-11-18T22:30:00+08:00", 2400, "示例写字楼地下车库停车费", notes="加班公务 CW01")

    h1 = harvest_vehicle_costs_from_ledger(sandbox_db, days=365)
    assert h1["ok"] is True and h1["harvested"] == 4
    assert h1["work_yuan"] == 24.0
    assert h1["personal_yuan"] == 113.0  # 62 + 35 + 16

    # 幂等再次归集不重复插入
    h2 = harvest_vehicle_costs_from_ledger(sandbox_db, days=365)
    assert h2["harvested"] == 0 and h2["skipped"] == 4

    # 登记 10 天前 LFP 满电校准（已超 7 天）、冬季充电效率样本、20 天后到期车险与 1 条未处理违章
    record_vehicle_event(
        sandbox_db,
        kind="lfp_cal",
        title="Model Y 100% LFP BMS 校准",
        occurred_at="2026-11-10T20:00:00+08:00",
        detail={"end_soc": 100, "full_charge": True},
    )
    record_vehicle_event(
        sandbox_db,
        kind="charge",
        title="五道口超充补能",
        amount_yuan=48.0,
        occurred_at="2026-11-17T20:00:00+08:00",
        detail={"kwh": 40.0, "distance_km": 220.0, "end_soc": 85},
    )
    record_vehicle_event(
        sandbox_db,
        kind="insurance",
        title="人保 Model Y 商业险+交强险",
        amount_yuan=5600.0,
        occurred_at="2025-12-10T10:00:00+08:00",
        detail={"expires_at": "2026-12-10"},
    )
    record_vehicle_event(
        sandbox_db,
        kind="violation",
        title="12123 违停提醒",
        amount_yuan=200.0,
        occurred_at="2026-11-16T12:00:00+08:00",
        detail={"points": 0, "paid": False},
    )

    ev = evaluate_lfp_calibration_and_insurance(sandbox_db, now=now, notify=True)
    assert ev["ok"] is True
    assert ev["lfp_calibration_due"] is True and ev["days_since_lfp_cal"] == 10
    assert ev["is_winter"] is True
    assert ev["avg_km_per_kwh"] == 5.5
    assert ev["insurance_due_soon"] is True and ev["days_to_insurance"] == 20
    assert len(ev["open_violations"]) == 1
    assert ev["notified"] is True

    # 同步全部车务记录到 Sure「车辆账户」
    pushed: list[dict] = []

    def _mock_sure_post(method: str, url: str, payload, headers):
        pushed.append(payload)
        return {"ok": True, "data": {"id": f"sure-v-{len(pushed)}"}}

    sync_res = sync_vehicle_to_sure(sandbox_db, http_fn=_mock_sure_post)
    assert sync_res["ok"] is True
    assert sync_res["synced"] == 8
    assert sync_res["sure_account"] == "车辆账户"
    assert all(p["account_name"] == "车辆账户" for p in pushed)

    # 二次同步应为 0
    sync_res2 = sync_vehicle_to_sure(sandbox_db, http_fn=_mock_sure_post)
    assert sync_res2["synced"] == 0

    report = format_vehicle_report(sandbox_db, now=now)
    assert "Model Y 车务与补能 CFO 报告" in report
    assert "LFP 100% 校准" in report
    assert "12123 未处理违章" in report
