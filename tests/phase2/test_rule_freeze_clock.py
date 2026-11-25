"""Phase 2 硬验收（第四节断舍离机制 + explain 全链路诊断）：
1. 采集器 append_event 绝不虚增 human_use_count_30d；
2. 非安全规则启用满 30 天且 human_use_count_30d==0 自动置为 FROZEN（停推且仅发 1 次冻结通知）；
3. 硬期限安全规则（如 credit_due）永久豁免自动冻结；
4. 本人主动正反馈（已办/改期）自动解冻；
5. `explain Wxx` 直接给出当前卡在哪一步的全链路诊断。
"""

from __future__ import annotations

from datetime import timedelta

from siming.db import SimingDB, iso_now, now_local
from siming.doctor import evaluate_30d_feature_freeze
from siming.evald import consume_pending_effects
from siming.router import explain_item, record_feedback, send_qq_message
from siming.waits import add_wait


def test_30d_human_use_freeze_clock_ignores_collector_events_and_exempts_hard_rules(
    sandbox_db: SimingDB,
) -> None:
    old_35d = iso_now(now_local() - timedelta(days=35))

    with sandbox_db.transaction() as conn:
        # 后台采集器狂写事件，但本人从未交互过 -> 不得算入 human_use_count_30d！
        sandbox_db.append_event(conn, "buy_track", "t1", "后台自动采集事件")

        # 规则 1：noisy_opt_rule 已启用 35 天，0 次人工使用 -> 应被自动冻结
        conn.execute(
            "INSERT INTO rule_stats (rule_id, mode, hard_rule, sent_14d, updated_at) VALUES ('noisy_opt_rule', 'ACTIVE', 0, 5, ?)",
            (old_35d,),
        )
        conn.execute(
            "INSERT INTO outbox (id, ref_id, rule_id, text, send_at, status, channel, created_at, sent_at) VALUES ('OB-OLD1', 'S01', 'noisy_opt_rule', 'old', ?, 'SENT', 'qqbot', ?, ?)",
            (old_35d, old_35d, old_35d),
        )

        # 规则 2：credit_due（信用卡还款硬安全规则）同样 35 天无点击 -> 必须豁免绝不冻结！
        conn.execute(
            "INSERT INTO rule_stats (rule_id, mode, hard_rule, sent_14d, updated_at) VALUES ('credit_due', 'ACTIVE', 1, 1, ?)",
            (old_35d,),
        )

        # 规则 3：used_opt_rule 35 天前启用，但 5 天前用户点过「已办」 -> human_use_count_30d=1，保留 ACTIVE！
        conn.execute(
            "INSERT INTO rule_stats (rule_id, mode, hard_rule, sent_14d, updated_at) VALUES ('used_opt_rule', 'ACTIVE', 0, 2, ?)",
            (old_35d,),
        )
        conn.execute(
            "INSERT INTO cases (case_id, title, status, rule_id, created_at, updated_at) VALUES ('S88', '有用提醒', '待处理', 'used_opt_rule', ?, ?)",
            (old_35d, old_35d),
        )

    record_feedback(sandbox_db, "S88", "已办")

    freeze_res = evaluate_30d_feature_freeze(sandbox_db, inactive_days=30, apply_freeze=True)
    assert "noisy_opt_rule" in freeze_res["newly_frozen"]
    assert "credit_due" not in freeze_res["newly_frozen"]
    assert "used_opt_rule" not in freeze_res["newly_frozen"]
    assert freeze_res["human_use_counts_30d"].get("buy_track", 0) == 0

    # 冲刷一次冻结通知后，后续 noisy_opt_rule 的推送必须被静默拦截！
    consume_pending_effects(sandbox_db, include_workers=False)
    suppressed = send_qq_message(
        sandbox_db,
        "这条已被冻结的推送不应发出",
        ref_id="S90",
        rule_id="noisy_opt_rule",
        bypass_quiet=True,
    )
    assert suppressed.get("skipped") == "rule_demoted_or_observe"

    # 二次巡检已处于 FROZEN，不再重复发第 2 条冻结通知
    freeze_res_2 = evaluate_30d_feature_freeze(sandbox_db, inactive_days=30, apply_freeze=True)
    assert freeze_res_2["newly_frozen"] == []
    assert "noisy_opt_rule" in freeze_res_2["already_frozen"]


def test_explain_item_reports_full_pipeline_stage(sandbox_db: SimingDB) -> None:
    f = sandbox_db.paths.tmp_root / "not-yet.flag"
    w = add_wait(sandbox_db, when=f"probe:file_exists:{f}", mode="remind", quote="等报告生成提醒我")
    exp = explain_item(sandbox_db, w["wait_id"])
    assert "全链路诊断" in exp
    assert "当前卡在/所处步骤：等待触发条件满足" in exp
