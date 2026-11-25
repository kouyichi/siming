"""回归测试 2026-11-20 回家路上快递/丰巢/牛板筋错推与漏推全链路修复。"""

from __future__ import annotations

import json
from datetime import date, datetime

from siming.collectors.pickup_codes import try_ingest_pickup_code
from siming.context_loc import ingest_ios_location
from siming.db import SHANGHAI_TZ, SimingDB, iso_now
from siming.qq_cards import handle_qq_button_action, infer_keyboard_for_message
from siming.remote import drop_item
from siming.router import close_case, flush_pickup_window_if_due, handle_inbound_qq
from siming.spatial_life import check_and_fire_offwork_pickup
from siming.superagent_triage import extract_douyin_or_ecom_parcels, match_pickup_sms_to_tracked_parcel


def _dt(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI_TZ)


def _write_two_home_policy(db: SimingDB) -> None:
    (db.paths.config_dir / "policy.yaml").write_text(
        """quiet_hours:
  start: '23:59'
  end: '00:00'
windows:
  morning_brief: '08:15'
  pickup_batch: '00:00'
daily_care:
  regimes:
  - id: primary_home
    until: '2028-01-01'
    home_label: 示例小区A
    home_lat: 39.904200
    home_lon: 116.407400
    office_label: 公司
    office_lat: 39.908000
    office_lon: 116.407000
  - id: secondary_home
    from: '2028-01-01'
    home_label: 示例小区B
    home_lat: 39.8600
    home_lon: 116.3200
    office_label: 公司
    office_lat: 39.908000
    office_lon: 116.407000
""",
        encoding="utf-8",
    )


def _outbox_rows(db: SimingDB) -> list[dict]:
    conn = db.connect()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM outbox ORDER BY created_at ASC").fetchall()]
    finally:
        conn.close()


def test_generic_fengchao_sms_keeps_full_location_classifies_site_and_never_false_cold_alerts(sandbox_db: SimingDB) -> None:
    """复盘 Bug #1 & #2：
    1. 真实丰巢短信『【丰巢】凭取件码08458751至示例小区A大堂门口丰巢柜取件，18小时内免费』必须保留完整柜机地址与免费时长，
       且识别出 示例小区A -> 家（示例小区A/示例小区A）；
    2. 即使库里存在在途冷链单（摘夏棠榴莲山竹冰品），无冷链词/无运单号的普通丰巢短信绝不得盲目匹配并误发冷链急件告警；
    3. 示例小区B/示例小区B丰巢短信必须归类为 新房，绝不得混作示例小区A顺路件！
    """
    _write_two_home_policy(sandbox_db)

    # 先种一条在途冷链单（模拟 S40 摘夏棠榴莲山竹冰品）
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO cases (
                case_id, title, next_step, due_at, status, strength, category, rule_id,
                evidence_ids, context_json, created_at, updated_at
            ) VALUES (
                'S40', '🧊在途冷链 · 摘夏棠榴莲山竹冰品(¥177.0)',
                '⚠️ 冷链生鲜防化冻！预计 2026-11-20 送达', '2026-11-20',
                '待处理', 'SOFT', 'parcel', 'superagent_parcel', '[]',
                '{"is_cold_chain": true, "eta_date": "2026-11-20", "eta_kind": "deliver"}',
                '2026-11-19T00:52:22+08:00', '2026-11-19T00:52:22+08:00'
            )
            """
        )

    # 1) 真实 2026-11-20 11:38 丰巢短信
    sms_home = "【丰巢】凭取件码08458751至示例小区A大堂门口丰巢柜取件，18小时内免费。有疑问联系快递员"
    assert match_pickup_sms_to_tracked_parcel(sandbox_db, sms_home) is None

    res_home = try_ingest_pickup_code(sandbox_db, sms_home, source="ios_sms")
    assert res_home["matched"] is True
    assert res_home["code"] == "08458751"
    assert "示例小区A大堂门口丰巢柜" in res_home["location"]
    assert res_home["site_alias"] == "家"

    # 绝不触发虚假冷链急件告警！
    assert not any("冷链/生鲜包裹到站急件提醒" in r["text"] for r in _outbox_rows(sandbox_db))

    # 2) 示例小区B（新房）丰巢短信
    sms_newhome = "【丰巢】凭取件码662819至示例小区B南门丰巢柜取件，18小时内免费。"
    res_new = try_ingest_pickup_code(sandbox_db, sms_newhome, source="ios_sms")
    assert res_new["matched"] is True
    assert res_new["site_alias"] == "新房"
    assert "示例小区B南门丰巢柜" in res_new["location"]

    conn = sandbox_db.connect()
    try:
        row_h = conn.execute("SELECT title, next_step, context_json FROM cases WHERE case_id=?", (res_home["case_id"],)).fetchone()
        row_n = conn.execute("SELECT title, next_step, context_json FROM cases WHERE case_id=?", (res_new["case_id"],)).fetchone()
    finally:
        conn.close()
    assert "示例小区A大堂门口丰巢柜" in row_h["title"]
    assert "18:00 回家顺路取件" not in row_h["next_step"]
    assert "18小时内免费" in row_h["next_step"]
    assert "【新房】" in row_n["title"]
    assert "示例小区B南门丰巢柜" in row_n["title"]
    assert "非当前住址顺路" in row_n["next_step"]


def test_douyin_screenshot_splits_niubanjin_and_durian_independently_with_absolute_eta(sandbox_db: SimingDB) -> None:
    """复盘 Bug #3 & #5：
    同一张抖音订单截屏里的 桂青源延边牛板筋（常温）和 摘夏棠榴莲山竹冰品（冷链）必须拆成两条独立 case，
    冷链标签绝不串到牛板筋上，且相对日期『预计明天送达』转为绝对日期，隔天不再腐烂！
    """
    real_ocr = "\n".join(
        [
            "00:49",
            "实况测评推荐＞",
            "桂青源官方旗舰店 •品牌",
            "运输中",
            "酸重庆！",
            "【桂青源】延边风味牛板筋.¥34.90",
            "丝状甜辣味*1+藤椒味*1+烧烤味*1",
            "×1",
            "7天无理由退货（一次性包装未破损）",
            "含运费险服务 实付款 ￥24.90",
            "码 运输中°包裹已离开长春市，预计明天送达",
            "申请售后",
            "查看物流",
            "确认收货",
            "售",
            "更多",
            "摘夏案＞",
            "10竹1雞市王總连+4自选",
            "运输中",
            "摘夏棠【32口味自选冰.",
            "1山竹十1猫山王+4自选（（不可自选",
            "山竹榴莲无备注默认为套餐【4）/10...",
            "¥177.00",
            "x1",
            "实付款 ¥177.00",
            "国 运输中°预计明天送达",
            "更多",
            "申请售后",
            "查看物流",
            "确认收货",
            "棱镜球文＞",
            "服务完成",
        ]
    )
    parcels = extract_douyin_or_ecom_parcels(real_ocr, as_of_date=date(2026, 11, 19))
    assert len(parcels) == 2

    beef = next(p for p in parcels if p["amount_cents"] == 2490)
    ice = next(p for p in parcels if p["amount_cents"] == 17700)

    assert "延边风味牛板筋" in beef["item_name"]
    assert beef["is_cold_chain"] is False
    assert beef["eta_date"] == "2026-11-20"
    assert beef["eta_kind"] == "deliver"

    assert "摘夏棠" in ice["item_name"]
    assert ice["is_cold_chain"] is True
    assert ice["eta_date"] == "2026-11-20"
    assert ice["eta_kind"] == "deliver"

    # 经 drop_item 落地必须生成 2 条独立 case（不再合并成一个 S40）
    dr = drop_item(sandbox_db, text=real_ocr, source_channel="ios_drop")
    assert dr["ok"] and dr["kind"] == "parcel"
    assert len(dr["case_ids"]) == 2
    assert len(set(dr["case_ids"])) == 2


def test_offwork_pickup_excludes_stale_yesterday_separates_sites_and_defers_1800_at_office(sandbox_db: SimingDB) -> None:
    """复盘 Bug #4, #6, #7：
    1. 昨日『预计今日(11-19)送达』的 S39 在 11-20 不得再被推送；
    2. 11-20 预计送达的 榴莲山竹冰品 与 延边牛板筋 独立列出，且明确标为『尚未收到取件短信，请看平台物流核实』；
    3. 示例小区A/示例小区A丰巢 显示完整柜机地址，示例小区B丰巢 单独归到『非顺路 · 新房』，不混为顺路件；
    4. 18:00 仍在公司时 pickup_1800 让位给 spatial_pickup，避免 18:00 + 离司二次重复推；
    5. 推送的 QQ 原生按钮 callback 全部合法（真实 Sxx，不再是 offwork-pickup-...），支持单件/整批销账与 close_case。
    """
    _write_two_home_policy(sandbox_db)

    # 种下 3 条 parcel：昨日旧单 S39(11-19)、今日冷链 S40(11-20)、今日常温牛板筋 S41(11-20)、备货单 S42(11-22发货)
    with sandbox_db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO cases (case_id, title, next_step, status, strength, category, rule_id, evidence_ids, context_json, created_at, updated_at)
            VALUES
            ('S39', '抖音在途包裹 · 和丝露发酵果汁 & 香美客苏打饼', '预计今日(11-19)送达，留意驿站取件', '待处理', 'SOFT', 'parcel', 'user_drop', '[]', '{}', '2026-11-19T00:52:22+08:00', '2026-11-19T00:52:22+08:00'),
            ('S40', '🧊在途冷链 · 摘夏棠榴莲山竹冰品(¥177.0)', '⚠️ 冷链！预计 2026-11-20 送达', '待处理', 'SOFT', 'parcel', 'superagent_parcel', '[]', '{"eta_date":"2026-11-20","eta_kind":"deliver","is_cold_chain":true}', '2026-11-19T00:52:22+08:00', '2026-11-19T00:52:22+08:00'),
            ('S41', '在途包裹 · 【桂青源】延边风味牛板筋(¥24.9)', '预计 2026-11-20 送达（常温零食）', '待处理', 'SOFT', 'parcel', 'superagent_parcel', '[]', '{"eta_date":"2026-11-20","eta_kind":"deliver","is_cold_chain":false}', '2026-11-19T00:52:22+08:00', '2026-11-19T00:52:22+08:00'),
            ('S42', '在途包裹 · 小绿盒无尘擦镜纸(¥9.8)', '备货中，预计 2026-11-22 发货', '待处理', 'SOFT', 'parcel', 'superagent_parcel', '[]', '{"eta_date":"2026-11-22","eta_kind":"ship","is_cold_chain":false}', '2026-11-19T00:52:22+08:00', '2026-11-19T00:52:22+08:00')
            """
        )

    r_home = try_ingest_pickup_code(
        sandbox_db,
        "【丰巢】凭取件码08458751至阳光花园大堂门口丰巢柜取件，18小时内免费。",
    )
    r_new = try_ingest_pickup_code(
        sandbox_db,
        "【丰巢】凭取件码662819至示例小区B南门丰巢柜取件，18小时内免费。",
    )

    # 18:00~22:59 仍在公司 -> flush_pickup_window_if_due 应让位给 spatial_pickup（不提前推，不受墙钟时间影响！）
    ingest_ios_location(
        sandbox_db,
        json.dumps({"lat": 39.908000, "lng": 116.407000, "wifi": "Office-Mobile"}),
        received_at=iso_now(),
    )
    assert flush_pickup_window_if_due(sandbox_db, now=_dt("2026-11-20 19:30")) is None

    # 模拟 2026-11-20 18:00 在公司 -> 21:31 离开公司回家
    with sandbox_db.transaction() as conn:
        conn.execute("DELETE FROM events WHERE source='ios_loc'")
    sandbox_db.set_meta("last_logged_ios_loc", "")
    ingest_ios_location(
        sandbox_db,
        json.dumps({"lat": 39.908000, "lng": 116.407000}),
        received_at="2026-11-20T18:00:00+08:00",
    )
    ingest_ios_location(
        sandbox_db,
        json.dumps({"_type": "transition", "event": "leave", "desc": "公司", "lat": 39.906200, "lng": 116.407300, "vel": 5}),
        received_at="2026-11-20T21:31:00+08:00",
    )

    p_res = check_and_fire_offwork_pickup(sandbox_db, now=_dt("2026-11-20 21:32"))
    assert p_res["ok"] and p_res["triggered"] is True
    txt = p_res["text"]

    # 昨日旧单 S39 和未发货 S42 必须被排除！
    assert "S39" not in txt and "和丝露" not in txt
    assert "S42" not in txt and "擦镜纸" not in txt

    # 今日两条独立包裹（牛板筋 + 榴莲山竹）都必须清晰列出，且与已到货丰巢分层展示
    assert "S40" in txt and "摘夏棠" in txt
    assert "S41" in txt and "延边风味牛板筋" in txt
    assert "尚未收到取件短信" in txt

    # 阳光花园丰巢显示完整地址与同院注解；示例小区B丰巢隔离到『非顺路 · 新房』
    assert "阳光花园大堂门口丰巢柜" in txt
    assert "示例小区A同院" in txt
    assert "非顺路提醒 · 新房（示例小区B）" in txt
    assert "示例小区B南门丰巢柜" in txt

    # QQ 按钮回调必须全部是合法 Sxx（绝不出现 offwork-pickup-... 被白名单拦截！）
    kb = infer_keyboard_for_message(
        sandbox_db,
        txt,
        ref_id=r_home["case_id"],
        rule_id="spatial_pickup",
        sub_meta={"batch_case_ids": [r_home["case_id"]], "all_case_ids": [r_home["case_id"], "S40", "S41"]},
    )
    assert kb is not None
    btn_datas = [b["action"]["data"] for row in kb["content"]["rows"] for b in row["buttons"]]
    assert f"siming:case_act:{r_home['case_id']}:已办" in btn_datas
    assert "siming:case_act:S41:已办" in btn_datas

    # 点单个按钮仅销账牛板筋 S41，不影响丰巢和榴莲
    act_res = handle_qq_button_action(sandbox_db, "siming:case_act:S41:已办")
    assert act_res["ok"] is True

    # CLI close_case 销账 S40
    c_res = close_case(sandbox_db, "S40", note="测试销账")
    assert c_res["ok"] is True and c_res["status"] == "完成"

    conn = sandbox_db.connect()
    try:
        st_40 = conn.execute("SELECT status FROM cases WHERE case_id='S40'").fetchone()["status"]
        st_41 = conn.execute("SELECT status FROM cases WHERE case_id='S41'").fetchone()["status"]
        st_home = conn.execute("SELECT status FROM cases WHERE case_id=?", (r_home["case_id"],)).fetchone()["status"]
    finally:
        conn.close()
    assert st_40 == "完成"
    assert st_41 == "完成"
    assert st_home == "待处理"


def test_pickup_sms_expanded_formats_and_otp_safety(sandbox_db: SimingDB) -> None:
    """验证扩展快递短信格式（取件码为/菜鸟/兔喜/圆通送达前台）能入库，营销和登录验证码仍被安全拦截。"""
    _write_two_home_policy(sandbox_db)

    r1 = try_ingest_pickup_code(sandbox_db, "【中通快递】您的快件已到示例小区A南门菜鸟驿站，取件码为 12-3-4567，请及时领取")
    assert r1["matched"] and r1["code"] == "12-3-4567" and r1["site_alias"] == "家"

    r2 = try_ingest_pickup_code(sandbox_db, "【圆通速递】您的包裹已送达示例小区A前台货架，尾号99881，请及时取走")
    assert r2["matched"] and r2["kind"] == "doorstep_pickup" and r2["tail_no"] == "99881" and r2["site_alias"] == "家"

    assert not try_ingest_pickup_code(sandbox_db, "【瑞幸咖啡】您的餐品预计19:05送达，戳链接领券")["matched"]
    assert not try_ingest_pickup_code(sandbox_db, "【银行】您的登录验证码是 654321，切勿泄露")["matched"]
