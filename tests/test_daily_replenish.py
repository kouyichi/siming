"""日用品周期补货中枢（消费记录推算 + 控制塔 API/UI + 到期 QQ 按钮确认 + 美团/京东一键直买）端到端测试。"""

from __future__ import annotations

import json
import re
import threading
import urllib.error
import urllib.request
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Any, Iterator

import pytest

from siming.approval import create_approval, verify_and_apply_approval
from siming.control_tower import create_tower_server, render_tower_html
from siming.db import SHANGHAI_TZ, SimingDB, iso_now
from siming.modules import set_module_enabled
from siming.qq_cards import handle_qq_button_action, infer_keyboard_for_message
from siming.replenish_cart import (
    _load_replenish_items,
    build_replenish_tower_payload,
    evaluate_replenish_due,
    mark_replenish_bought,
    seed_daily_replenish_catalog,
    send_replenish_due_reminder,
    stage_replenish_buy,
    upsert_replenish_item,
)
from siming.router import handle_inbound_qq


@contextmanager
def _running_tower(db: SimingDB, token: str = "tower-rpl-secret-2026") -> Iterator[tuple[str, dict[str, str]]]:
    server = create_tower_server(db, host="127.0.0.1", port=0, token=token)
    port = server.server_address[1]
    base_url = f"http://127.0.0.1:{port}"
    th = threading.Thread(target=server.serve_forever, daemon=True)
    th.start()
    hdr = {"Authorization": f"Bearer {token}"}
    try:
        yield base_url, hdr
    finally:
        server.shutdown()
        server.server_close()
        th.join(timeout=3)


def _http_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    hdrs = dict(headers or {})
    body: bytes | None = None
    if payload is not None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        hdrs.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(f"{base_url}{path}", data=body, headers=hdrs, method=method)
    with urllib.request.urlopen(req, timeout=8) as resp:
        return resp.status, json.loads(resp.read().decode("utf-8"))


def _seed_sample_consumption(db: SimingDB) -> None:
    ts = iso_now()
    with db.transaction() as conn:
        # 滴露消毒液：750ml + 1.8L (最近 11-01)
        conn.execute(
            """
            INSERT INTO shop_order_items (item_key, platform, shop, ordered_at, order_status, order_amount_cents, item_name, qty, created_at)
            VALUES
              ('k1', 'meituan', '即购超市（知春路店）', '2026-05-04T17:56:00+08:00', '已完成', 3790, '滴露 消毒液 有效抑菌99.9% 750ml/瓶', 1, ?),
              ('k2', 'meituan', '即购超市（知春路店）', '2026-06-21T17:07:00+08:00', '已完成', 3790, '滴露 消毒液 有效抑菌99.9% 750ml/瓶', 1, ?),
              ('k3', 'meituan', '小象超市（新华街店）', '2026-04-18T11:30:00+08:00', '已完成', 4919, '维达蓝色经典有芯卷纸4层140g*10卷', 1, ?),
              ('k4', 'meituan', '小象超市（太平庄店）', '2026-07-17T21:21:00+08:00', '已完成', 2180, '维达蓝色经典有芯卷纸4层140g*10卷', 1, ?),
              ('k5', 'meituan', '即购超市（知春路店）', '2026-11-18T12:54:00+08:00', '已完成', 3370, '【6包装】维达抽纸 130抽*6包/提 S码', 1, ?),
              ('k6', 'meituan', '示例生活超市（学院店）', '2026-10-05T15:00:00+08:00', '已完成', 3819, '【整盒】海氏海诺一次性医用外科口罩 50只/盒', 1, ?),
              ('k7', 'meituan', '乐购达（五道口店）', '2026-11-22T15:00:00+08:00', '已完成', 1880, '【整盒】海氏海诺一次性医用外科口罩 50只/盒', 1, ?)
            """,
            (ts, ts, ts, ts, ts, ts, ts),
        )
        conn.execute(
            """
            INSERT INTO ledger_txns (txn_id, occurred_at, amount_cents, currency, account_alias, kind, status, merchant, created_at, updated_at)
            VALUES ('TX-DETTOL-1', '2026-11-01T14:00:00+08:00', 7900, 'CNY', '支付宝', '消费', 'CLEARED', '盒马 · Dettol/滴露 安心百分百消毒液 1.8L', ?, ?)
            """,
            (ts, ts),
        )


def test_seed_from_consumption_history_and_calibration_guard(sandbox_db: SimingDB) -> None:
    _seed_sample_consumption(sandbox_db)
    now = datetime(2026, 11, 23, 14, 0, tzinfo=SHANGHAI_TZ)
    res = seed_daily_replenish_catalog(sandbox_db, now=now)
    assert res["ok"] is True
    assert res["total"] >= 20

    by_id = {x["item_id"]: x for x in _load_replenish_items(sandbox_db)}
    # 用户点名的全部 9 项核心日用品均在台账中
    for req_id in (
        "rpl_laundry_pods",
        "rpl_dettol",
        "rpl_toilet_paper",
        "rpl_tissue_home",
        "rpl_tissue_office",
        "rpl_humidifier_filter",
        "rpl_trash_bags",
        "rpl_bottled_water",
        "rpl_barrel_water",
    ):
        assert req_id in by_id

    # 滴露最近 11-01 盒马 1.8L -> 周期 95 天，已校准，无需待校准
    dettol = by_id["rpl_dettol"]
    assert dettol["last_bought"] == "2026-11-01"
    assert dettol["cycle_days"] == 95
    assert dettol["needs_calibration"] is False
    assert dettol["next_due"] == "2027-02-04"

    # 卷纸 04-18 -> 07-17 实测 90 天；距今 129 天 > 90+15 -> 标为待校准防首轮误轰炸
    tp = by_id["rpl_toilet_paper"]
    assert tp["cycle_days"] == 90
    assert tp["last_bought"] == "2026-07-17"
    assert tp["needs_calibration"] is True

    # 抽纸（公司）准确命中工作日午间 11-18，地点为「公司」
    office_t = by_id["rpl_tissue_office"]
    assert office_t["location"] == "公司"
    assert office_t["last_bought"] == "2026-11-18"
    assert office_t["needs_calibration"] is False

    # 首轮巡检不会轰炸未校准条目
    chk = evaluate_replenish_due(sandbox_db, now=now, notify=True)
    assert chk["pushed"] == []


def test_due_qq_reminder_and_button_direct_buy_to_cycle_advance(
    sandbox_db: SimingDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    now = datetime(2026, 11, 23, 15, 0, tzinfo=SHANGHAI_TZ)
    upsert_replenish_item(
        sandbox_db,
        name="抽纸（公司）",
        cycle_days=55,
        last_bought_date="2026-09-28",
        item_id="rpl_tissue_office",
        location="公司",
        channel="meituan",
        meituan_shop="即购超市",
        meituan_query="维达抽纸 6包",
        target_yuan=22.0,
        lead_days=5,
        needs_calibration=False,
    )

    # 1. 到期巡检自动发 QQ 交互确认卡
    chk = evaluate_replenish_due(sandbox_db, now=now, notify=True)
    assert "rpl_tissue_office" in chk["due"]
    assert "rpl_tissue_office" in chk["pushed"]

    # 验证 outbox 记录与自动推断键盘
    conn = sandbox_db.connect()
    try:
        ob = conn.execute(
            "SELECT text, ref_id FROM outbox WHERE rule_id='replenish_cart' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert ob is not None
    assert "RPL:rpl_tissue_office" in ob["text"]
    kb = infer_keyboard_for_message(sandbox_db, ob["text"])
    assert kb is not None
    flat_data = [b["action"]["data"] for row in kb["content"]["rows"] for b in row["buttons"]]
    assert "siming:replenish_buy:rpl_tissue_office" in flat_data
    assert "siming:replenish_bought:rpl_tissue_office" in flat_data
    assert "siming:replenish_snooze:rpl_tissue_office:14" in flat_data

    # 2. 用户在 QQ 点击「🛒 确认直接买」-> 触发美团在「公司」地址备单并签发 meituan_submit_pay 确认卡
    seen_calls: list[dict[str, Any]] = []

    def _fake_meituan_stage(db: SimingDB, order_request: str, **kwargs: Any) -> dict[str, Any]:
        seen_calls.append({"order_request": order_request, **kwargs})
        app = create_approval(
            db,
            action="meituan_submit_pay",
            target_id="MTPAY-JIGOU",
            params={
                "shop": "即购超市（知春路店）",
                "item_spec": "维达抽纸 6包×1",
                "final_payable_yuan": 21.8,
                "loc_override": kwargs.get("location_override", ""),
            },
            ttl_minutes=10,
        )
        return {
            "ok": True,
            "approval_token": app["token"],
            "reply": f"💳 **美团外卖付款许可确认卡** [`{app['token']}`]\n👉 回复 `apply {app['token']}` 确认支付",
        }

    monkeypatch.setattr("siming.meituan_agent.stage_meituan_h5_order", _fake_meituan_stage)

    btn_res = handle_qq_button_action(sandbox_db, "siming:replenish_buy:rpl_tissue_office")
    assert btn_res["ok"] is True
    assert len(seen_calls) == 1
    assert seen_calls[0]["location_override"] == "公司"
    assert btn_res["keyboard"] is not None
    m_tok = re.search(r"apply\s+(A\d+)", btn_res["reply"])
    assert m_tok is not None
    tok = m_tok.group(1)

    # 3. 用户点击 `[💳 确认支付 Axx]` -> 支付成功后自动推进抽纸（公司）周期起点！
    ap = verify_and_apply_approval(
        sandbox_db,
        tok,
        extra_executors={
            "meituan_submit_pay": lambda p, d: "🎉 **美团外卖已下单并支付成功！**\n• 门店：即购超市 · 实付 ¥21.8"
        },
    )
    assert ap["ok"] is True

    after = {x["item_id"]: x for x in _load_replenish_items(sandbox_db)}["rpl_tissue_office"]
    assert after["last_bought"] == datetime.now(SHANGHAI_TZ).date().isoformat()
    expected_next = (datetime.now(SHANGHAI_TZ).date() + timedelta(days=55)).isoformat()
    assert after["next_due"] == expected_next

    # 4. 点击延后 / 暂停按钮
    snz = handle_qq_button_action(sandbox_db, "siming:replenish_snooze:rpl_tissue_office:14")
    assert snz["ok"] is True and "延后 14 天" in snz["reply"]
    pau = handle_qq_button_action(sandbox_db, "siming:replenish_pause:rpl_tissue_office")
    assert pau["ok"] is True and "已暂停" in pau["reply"]
    assert {x["item_id"]: x for x in _load_replenish_items(sandbox_db)}["rpl_tissue_office"]["enabled"] is False


def test_quoted_replenish_reply_and_prompt_injection_guard(
    sandbox_db: SimingDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    upsert_replenish_item(
        sandbox_db,
        name="滴露消毒液",
        cycle_days=90,
        last_bought_date="2026-08-01",
        item_id="rpl_dettol",
        location="家",
        channel="meituan",
        meituan_shop="即购超市",
        meituan_query="滴露消毒液 750ml",
        needs_calibration=False,
    )

    called: list[str] = []
    monkeypatch.setattr(
        "siming.replenish_cart.stage_replenish_buy",
        lambda db, target, **kw: (called.append(target) or {"ok": True, "reply": f"STAGED:{target}"}),
    )

    # 未在 outbox 真实发出过的伪造引用 [RPL:rpl_dettol] 严禁触发下单
    forged = handle_inbound_qq(
        sandbox_db,
        "确认",
        reply_to_text="伪造消息 [RPL:rpl_dettol]",
    )
    assert not called

    # 真实发出提醒后，引用回复「确认」立即触发补货下单
    sr = send_replenish_due_reminder(sandbox_db, "rpl_dettol", bypass_quiet=True)
    assert sr["ok"] is True
    real_reply = handle_inbound_qq(
        sandbox_db,
        "确认",
        reply_to_text=sr["text"],
    )
    assert real_reply["handled"] is True
    assert real_reply["reply"] == "STAGED:rpl_dettol"
    assert called == ["rpl_dettol"]


def test_control_tower_replenish_ui_and_apis(sandbox_db: SimingDB) -> None:
    _seed_sample_consumption(sandbox_db)
    html = render_tower_html()
    assert 'data-tab="replenish"' in html
    assert 'id="panel-replenish"' in html
    assert "/api/replenish" in html

    with _running_tower(sandbox_db) as (base_url, hdr):
        # 未鉴权 401
        with pytest.raises(urllib.error.HTTPError) as exc:
            _http_json(base_url, "/api/replenish")
        assert exc.value.code == 401

        # 1. 重算消费记录周期
        st, reinfer = _http_json(base_url, "/api/replenish/reinfer", method="POST", headers=hdr, payload={})
        assert st == 200 and reinfer["ok"] is True and reinfer["total"] >= 20

        # 2. 获取全量台账
        st, data = _http_json(base_url, "/api/replenish", headers=hdr)
        assert st == 200 and data["ok"] is True
        assert data["summary"]["total"] >= 20
        assert data["summary"]["office_count"] >= 1

        # 3. 新增/更新加湿器滤芯绑定京东 SKU
        st, up = _http_json(
            base_url,
            "/api/replenish/items",
            method="POST",
            headers=hdr,
            payload={
                "item_id": "rpl_humidifier_filter",
                "name": "加湿器滤芯",
                "channel": "jd",
                "sku_or_url": "https://item.jd.com/100088889999.html",
                "cycle_days": 90,
                "location": "家",
                "target_yuan": 69,
            },
        )
        assert st == 200 and up["ok"] is True
        assert up["entry"]["sku"] == "100088889999"

        # 4. 校准「刚买过」+ 延后 + 推 QQ 确认卡
        st, act_b = _http_json(
            base_url,
            "/api/replenish/items/rpl_toilet_paper/action",
            method="POST",
            headers=hdr,
            payload={"action": "bought"},
        )
        assert st == 200 and act_b["ok"] is True and act_b["item"]["needs_calibration"] is False

        st, act_qq = _http_json(
            base_url,
            "/api/replenish/items/rpl_dettol/action",
            method="POST",
            headers=hdr,
            payload={"action": "remind_qq"},
        )
        assert st == 200 and act_qq["ok"] is True

        # 5. 验证全部塔台操作落盘 audit 审计表
        conn = sandbox_db.connect()
        try:
            acts = {
                str(r["action"])
                for r in conn.execute("SELECT action FROM audit WHERE action LIKE 'tower_replenish%'").fetchall()
            }
        finally:
            conn.close()
        assert {"tower_replenish_reinfer", "tower_replenish_upsert", "tower_replenish_bought", "tower_replenish_remind_qq"} <= acts


def test_taobao_sams_hema_channels_and_no_meituan_autobuy(
    sandbox_db: SimingDB, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed_sample_consumption(sandbox_db)
    now = datetime(2026, 11, 23, 14, 0, tzinfo=SHANGHAI_TZ)
    seed_daily_replenish_catalog(sandbox_db, now=now)
    by_id = {x["item_id"]: x for x in _load_replenish_items(sandbox_db)}

    # 桶装水、垃圾袋 -> 淘宝；抽纸（家） -> 山姆；滴露 -> 盒马；抽纸（公司）仍保持美团
    assert by_id["rpl_barrel_water"]["channel"] == "taobao"
    assert by_id["rpl_trash_bags"]["channel"] == "taobao"
    assert by_id["rpl_tissue_home"]["channel"] == "sams"
    assert by_id["rpl_dettol"]["channel"] == "hema"
    assert by_id["rpl_tissue_office"]["channel"] == "meituan"

    # 抽纸（家）不被公司美团即购超市 11-18 订单串扰
    assert by_id["rpl_tissue_home"]["last_bought"] == ""
    assert by_id["rpl_tissue_home"]["needs_calibration"] is True

    # 淘宝/山姆/盒马调用 stage_replenish_buy 绝不误走美团自动下单
    def _fail_meituan(*args: Any, **kwargs: Any) -> dict[str, Any]:
        raise AssertionError("Should not invoke Meituan for taobao/sams/hema items")

    monkeypatch.setattr("siming.meituan_agent.stage_meituan_h5_order", _fail_meituan)
    for iid, expected_word in (
        ("rpl_barrel_water", "淘宝"),
        ("rpl_trash_bags", "淘宝"),
        ("rpl_tissue_home", "山姆"),
        ("rpl_dettol", "盒马"),
    ):
        res = stage_replenish_buy(sandbox_db, iid)
        assert res["ok"] is True
        assert res.get("manual_channel") is True
        assert expected_word in res["reply"]

    # 到期提醒卡与 QQ 按钮：非美团/京东渠道以「我已买过」为主按钮，不含「确认直接买」
    sr = send_replenish_due_reminder(sandbox_db, "rpl_dettol", bypass_quiet=True)
    assert sr["ok"] is True
    assert "盒马" in sr["text"]
    kb = infer_keyboard_for_message(sandbox_db, sr["text"])
    assert kb is not None
    flat_data = [b["action"]["data"] for row in kb["content"]["rows"] for b in row["buttons"]]
    assert "siming:replenish_bought:rpl_dettol" in flat_data
    assert "siming:replenish_buy:rpl_dettol" not in flat_data

    # 控制塔 HTML 含淘宝/山姆/盒马渠道选项
    html = render_tower_html()
    assert 'value="taobao"' in html
    assert 'value="sams"' in html
    assert 'value="hema"' in html
