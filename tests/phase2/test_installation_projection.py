"""Phase 2 硬验收（S2-B + 决定 2=A）：12.10 新房 15 单（11 台待装大家电）送装投影、拣货完成防假签收、物流更新不覆盖人工已结状态。"""

from __future__ import annotations

import json
from pathlib import Path

from siming.collectors.buy_track import collect_buy_track
from siming.db import SimingDB
from siming.install_plan import (
    build_installation_projection,
    format_installation_checklist,
    is_order_truly_signed,
)
from siming.router import handle_inbound_qq


def test_picking_complete_never_misclassified_as_signed_and_human_close_preserved(
    sandbox_db: SimingDB,
) -> None:
    # 1. “拣货完成 / 出库完成 / 揽收完成” 严禁命中“完成”误判为已签收！
    assert is_order_truly_signed("pending", "商品出库", "您的订单已拣货完成，准备发往中转场") is False
    assert is_order_truly_signed("shipping", "运输中", "揽收完成") is False
    assert is_order_truly_signed("delivered", "完成", "") is True
    assert is_order_truly_signed("shipping", "派送中", "您的快件已签收，签收人：本人") is True

    # 2. 先跑一次 bootstrap，再跑一次变动：极米幕布“拣货完成”只标物流在途，绝不标已签收！
    auto_p = sandbox_db.paths.tmp_root / "auto.json"
    snap_v1 = {
        "generatedAt": "2026-11-17T01:00:00Z",
        "sources": {"jd": {"last": {"status": "ok"}}, "tb": {"last": {"status": "ok"}}},
        "items": [
            {
                "id": "screen",
                "name": "极米 120 寸 光子悬浮幕布",
                "platform": "jd",
                "install": "brand",
                "orderInfo": {"stage": "pending", "raw": "暂停发货", "progress": "等待通知发货"},
            }
        ],
    }
    auto_p.write_text(json.dumps(snap_v1, ensure_ascii=False), encoding="utf-8")
    r_boot = collect_buy_track(sandbox_db, auto_json_path=auto_p)
    assert r_boot["bootstrapped"] is True

    snap_v2 = {
        **snap_v1,
        "items": [
            {
                "id": "screen",
                "name": "极米 120 寸 光子悬浮幕布",
                "platform": "jd",
                "install": "brand",
                "orderInfo": {"stage": "pending", "raw": "商品出库", "progress": "您的订单已拣货完成"},
            }
        ],
    }
    auto_p.write_text(json.dumps(snap_v2, ensure_ascii=False), encoding="utf-8")
    r_chg = collect_buy_track(sandbox_db, auto_json_path=auto_p)
    assert r_chg["changed"] == ["screen"]

    conn = sandbox_db.connect()
    c_row = conn.execute("SELECT case_id, status, next_step FROM cases").fetchone()
    conn.close()
    assert c_row is not None
    assert "已签收" not in c_row["next_step"]
    assert "商品出库" in c_row["next_step"]

    # 3. 用户人工把该单标为「完成」后，后续物流文案微调绝不能把单据重新改回「待处理」！
    r_done = handle_inbound_qq(sandbox_db, f"已办 {c_row['case_id']}")
    assert r_done["handled"] is True

    snap_v3 = {
        **snap_v1,
        "items": [
            {
                "id": "screen",
                "name": "极米 120 寸 光子悬浮幕布",
                "platform": "jd",
                "install": "brand",
                "orderInfo": {"stage": "shipping", "raw": "运输中", "progress": "快件到达北京顺义分拨中心"},
            }
        ],
    }
    auto_p.write_text(json.dumps(snap_v3, ensure_ascii=False), encoding="utf-8")
    collect_buy_track(sandbox_db, auto_json_path=auto_p)

    conn = sandbox_db.connect()
    st_after = conn.execute("SELECT status FROM cases WHERE case_id=?", (c_row["case_id"],)).fetchone()["status"]
    conn.close()
    assert st_after == "完成"


def test_1210_projection_matches_real_15_orders_and_distinguishes_intent_vs_confirmed(
    sandbox_db: SimingDB,
) -> None:
    sample_items = {
        "preferences": {"install_date": "2026-12-10"},
        "items": [
            {"id": "tv", "name": "客厅 85 寸智能电视", "room": "客厅", "installDate": "2026-12-10", "install": "brand"},
            {"id": "screen", "name": "卧室 120 寸幕布", "room": "卧室", "installDate": "2026-12-10", "install": "brand"},
            {"id": "hood", "name": "烟灶套装", "room": "厨房", "installDate": "2026-12-10", "install": "brand"},
            {"id": "washer", "name": "洗烘一体机", "room": "阳台", "installDate": "2026-12-10", "install": "jd", "svc": ["9000000000000003"]},
            {"id": "washer-wall", "name": "壁挂洗衣机", "room": "", "installDate": "2026-12-10", "install": "brand"},
            {"id": "heater", "name": "燃气热水器", "room": "", "installDate": "2026-12-10", "install": "brand"},
            {"id": "ac-1", "name": "客厅空调", "room": "", "installDate": "2026-12-10", "install": "brand"},
            {"id": "ac-2", "name": "主卧空调", "room": "", "installDate": "2026-12-10", "install": "brand"},
            {"id": "ac-3", "name": "次卧空调", "room": "", "installDate": "2026-12-10", "install": "brand"},
            {"id": "fridge", "name": "零嵌冰箱", "room": "餐厅", "installDate": "2026-12-10", "install": "brand"},
            {"id": "dishwasher", "name": "嵌入式洗碗机", "room": "厨房", "installDate": "2026-12-10", "install": "brand"},
            {"id": "bracket", "name": "电视挂架", "room": "客厅", "install": "self"},
            {"id": "projector", "name": "激光投影仪", "room": "卧室", "install": "self"},
            {"id": "purifier", "name": "桌面净饮机", "room": "餐厅", "install": "none"},
            {"id": "router", "name": "无线路由器", "room": "弱电箱", "install": "self"},
        ],
    }
    real_items_p = sandbox_db.paths.tmp_root / "items_sample.json"
    real_items_p.write_text(json.dumps(sample_items, ensure_ascii=False), encoding="utf-8")
    real_auto_p = sandbox_db.paths.tmp_root / "auto_sample.json"
    real_auto_p.write_text(json.dumps({"generatedAt": "2026-11-16T08:00:00+08:00", "items": []}, ensure_ascii=False), encoding="utf-8")
    assert real_items_p.is_file()

    empty_manual = sandbox_db.paths.tmp_root / "manual_empty.json"
    empty_manual.write_text("{}", encoding="utf-8")

    proj = build_installation_projection(
        sandbox_db,
        items_json_path=real_items_p,
        auto_json_path=real_auto_p if real_auto_p.is_file() else None,
        manual_json_path=empty_manual,
    )
    assert proj["ok"] is True
    assert proj["target_date"] == "2026-12-10"
    assert proj["total_orders"] == 15
    assert proj["vendor_install_total"] == 11
    # manual.json 为空时，11 台虽然在 items.json 有 installDate=2026-12-10，但「已确认预约」必须严格为 0！
    assert proj["vendor_booked_count"] == 0
    assert len(proj["unbooked_install_items"]) == 11

    # 核对 5 台房间为空缺口（林内热水器、海尔壁挂洗衣机、3 台格力空调）
    missing_ids = {s.split("(")[-1].rstrip(")") for s in proj["missing_rooms"]}
    assert missing_ids == {"heater", "washer-wall", "ac-1", "ac-2", "ac-3"}

    # 核对客厅 85 寸电视与卧室 120 寸幕布归属（绝不混入旧壳 100 寸电视）
    by_id = {it["id"]: it for it in proj["install_items"] + proj["other_items"]}
    assert by_id["tv"]["room"] == "客厅" and "85 寸" in by_id["tv"]["name"]
    assert by_id["screen"]["room"] == "卧室" and "120 寸" in by_id["screen"]["name"]
    assert "服务单: 未记录" in by_id["hood"]["contact_record"]
    assert "9000000000000003" in by_id["washer"]["contact_record"]

    # 模拟人工在 home:8443/track/ 确认了洗烘机与海尔壁挂洗衣机的预约及房间
    booked_manual = sandbox_db.paths.tmp_root / "manual_booked.json"
    booked_manual.write_text(
        json.dumps(
            {
                "washer": {"install": "已预约", "installDate": "2026-12-10 上午"},
                "washer-wall": {"room": "卫生间干区", "install": "已预约", "installDate": "2026-12-10 下午"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    proj2 = build_installation_projection(
        sandbox_db,
        items_json_path=real_items_p,
        auto_json_path=real_auto_p if real_auto_p.is_file() else None,
        manual_json_path=booked_manual,
    )
    assert proj2["vendor_booked_count"] == 2
    assert len(proj2["missing_rooms"]) == 4

    txt = format_installation_checklist(
        sandbox_db,
        items_json_path=real_items_p,
        auto_json_path=real_auto_p if real_auto_p.is_file() else None,
        manual_json_path=booked_manual,
    )
    assert "燃气表 G2.5" in txt
    assert "NB P65 挂架" in txt
    assert "100 寸" not in txt
