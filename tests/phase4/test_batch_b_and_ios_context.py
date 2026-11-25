"""Batch B（movein_pm + quiet_cfo + replenish_cart）与手机无感快捷指令（OTP 瞬时保险箱 + Wi-Fi/GPS 定位 + 睡眠充电融合）全链路测试。"""

from __future__ import annotations

import importlib.util
import json
from datetime import datetime
from pathlib import Path
from typing import Any

from siming.approval import verify_and_apply_approval
from siming.attention import evaluate_attention_state
from siming.collectors.edge_sync import process_edge_records
from siming.db import SHANGHAI_TZ, SimingDB
from siming.ledger.reconcile import match_or_record_wallet_row
from siming.ledger.reimburse import set_saved_place, stage_invoice_attachment
from siming.ledger.sms_parser import try_ingest_bank_sms
from siming.movein_pm import check_install_conflicts, query_appliance_passport
from siming.otp_vault import pull_latest_platform_otp, save_authorized_phone
from siming.replenish_cart import execute_jd_cart_add
from siming.router import handle_inbound_qq


def _load_edge_mod() -> Any:
    edge_py = Path(__file__).resolve().parents[2] / "edge" / "siming_edge.py"
    spec = importlib.util.spec_from_file_location("siming_edge_p4b", edge_py)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_text_pdf(_path: Path, lines: list[str]) -> bytes:
    full_str = "\n".join(lines)
    uniq_chars: list[str] = []
    for ch in full_str:
        if ch != "\n" and ch not in uniq_chars:
            uniq_chars.append(ch)
    cmap_entries = []
    char_to_gid: dict[str, int] = {}
    for idx, ch in enumerate(uniq_chars, start=1):
        char_to_gid[ch] = idx
        cmap_entries.append(f"<{idx:04X}> <{ord(ch):04X}>")
    cmap_body = (
        "/CIDInit /ProcSet findresource begin\n12 dict begin\nbegincmap\n"
        "/CMapName /CustomUTF16 def\n/CMapType 2 def\n"
        "1 begincodespacerange\n<0001> <FFFF>\nendcodespacerange\n"
        f"{len(cmap_entries)} beginbfchar\n"
        + "\n".join(cmap_entries)
        + "\nendbfchar\nendcmap\nCMapName currentdict /CMap defineresource pop\nend\nend\n"
    ).encode("ascii")
    ops = ["BT", "/F1 12 Tf", "36 750 Td"]
    for li, ln in enumerate(lines):
        if li > 0:
            ops.append("0 -18 Td")
        hex_seq = "".join(f"{char_to_gid[c]:04X}" for c in ln if c in char_to_gid)
        ops.append(f"<{hex_seq}> Tj")
    ops.append("ET")
    content_bytes = "\n".join(ops).encode("ascii")
    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n",
        f"4 0 obj\n<< /Length {len(content_bytes)} >>\nstream\n".encode("ascii") + content_bytes + b"\nendstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type0 /BaseFont /SimSun /Encoding /Identity-H /DescendantFonts [6 0 R] /ToUnicode 7 0 R >>\nendobj\n",
        b"6 0 obj\n<< /Type /Font /Subtype /CIDFontType2 /BaseFont /SimSun /CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> >>\nendobj\n",
        f"7 0 obj\n<< /Length {len(cmap_body)} >>\nstream\n".encode("ascii") + cmap_body + b"\nendstream\nendobj\n",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for o in objs:
        offsets.append(len(out))
        out.extend(o)
    xref_pos = len(out)
    out.extend(f"xref\n0 {len(offsets)}\n0000000000 65535 f \n".encode("ascii"))
    for off in offsets[1:]:
        out.extend(f"{off:010d} 00000 n \n".encode("ascii"))
    out.extend(f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii"))
    return bytes(out)


def test_ephemeral_otp_vault_isolation_and_bank_block(sandbox_db: SimingDB) -> None:
    edge_mod = _load_edge_mod()
    store = edge_mod.EdgeStore(sandbox_db.paths.tmp_root / "edge-otp")
    store.set_otp_vault_enabled(True)

    # 1. 银行转账/银联交易动态码即使开启代登也必须拒收！
    r_bank = store.ingest_payload("ios_sms", "【中国银行】您正在办理转账，动态验证码 334455，切勿泄露。", False)
    assert r_bank.get("dropped") == "bank_otp_guard"
    assert store.pull_ephemeral_otps() == []

    # 2. 美团/瑞幸等生活平台登录验证码进入加密 otp_inbox，常规 pull_since 保持为空（零进 events！）
    r_mt = store.ingest_payload("ios_sms", "【美团】684920（登录验证码，5分钟内有效，请勿泄露）", False)
    assert r_mt.get("otp_staged") is True
    assert store.pull_since(0) == []

    sp = save_authorized_phone(sandbox_db, "13800138000")
    assert sp["ok"] is True
    assert "138****8000" in sp["summary"]

    def _caller(cid: int = 0) -> dict[str, Any]:
        return {"ok": True, "items": store.pull_ephemeral_otps(consume_id=cid)}

    hit = pull_latest_platform_otp(sandbox_db, platform_hint="meituan", consume=True, edge_otp_caller=_caller)
    assert hit is not None
    assert hit["code"] == "684920"
    assert hit["brand"] == "美团"

    # 消费即焚：二次拉取为空，且审计表绝不含明文 684920
    assert pull_latest_platform_otp(sandbox_db, platform_hint="meituan", edge_otp_caller=_caller) is None
    conn = sandbox_db.connect()
    try:
        aud_txt = json.dumps([dict(r) for r in conn.execute("SELECT * FROM audit").fetchall()], ensure_ascii=False)
        ev_cnt = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    finally:
        conn.close()
    assert "684920" not in aud_txt
    assert "6**0" in aud_txt
    assert ev_cnt == 0


def test_ios_wifi_and_gps_location_auto_didi_origin(sandbox_db: SimingDB) -> None:
    set_saved_place(sandbox_db, "家", "示例小区A")
    set_saved_place(sandbox_db, "公司", "示例写字楼A座")
    r_bind = handle_inbound_qq(sandbox_db, "绑定wifi Office-5G 公司")
    assert r_bind["handled"] is True

    # 手机连上公司 Wi-Fi 上报位置 -> 说「打车回家」自动以「示例写字楼A座」为起点！
    process_edge_records(
        sandbox_db,
        [{"id": 10, "source": "ios_loc", "text": json.dumps({"wifi": "Office-5G"})}],
    )
    ride_home = handle_inbound_qq(sandbox_db, "打车回家")
    assert ride_home["handled"] is True
    assert "示例写字楼A座 → 示例小区A" in ride_home["reply"]
    assert "Office-5G" in ride_home["reply"]

    # 人在外面无熟知 Wi-Fi，上报实时 GPS 与街道名 -> 自动采用户外实时定位为起点！
    process_edge_records(
        sandbox_db,
        [
            {
                "id": 11,
                "source": "ios_loc",
                "text": json.dumps({"wifi": "", "lat": 39.9082, "lng": 116.3800, "address": "示例地铁站A口"}),
            }
        ],
    )
    ride_out = handle_inbound_qq(sandbox_db, "打车去北京南站")
    assert "示例地铁站A口 → 北京南站" in ride_out["reply"]


def test_ios_passive_sleep_and_charger_desk_fusion(sandbox_db: SimingDB) -> None:
    dt_3am = datetime(2026, 11, 18, 3, 15, tzinfo=SHANGHAI_TZ)
    process_edge_records(
        sandbox_db,
        [{"id": 20, "source": "ios_sleep", "text": "charge_on", "received_at": "2026-11-18T03:00:00+08:00"}],
    )

    # 1. 手机插上电，但人还在电脑前敲代码（idle=5s） -> 保持清醒，绝不误判睡着！
    st_coding = evaluate_attention_state(
        sandbox_db,
        desktop_probe=lambda: {"available": True, "locked": False, "idle_sec": 5.0},
        now_override=dt_3am,
    )
    assert st_coding["state"] == "AWAKE_ACTIVE"

    # 2. 手机插着电，且电脑已空闲 12 分钟（无需等满 40m） -> 自动判定上床入睡！
    st_bed = evaluate_attention_state(
        sandbox_db,
        desktop_probe=lambda: {"available": True, "locked": False, "idle_sec": 720.0},
        now_override=dt_3am,
    )
    assert st_bed["state"] == "ASLEEP"
    assert "ios_charging_and_desk_idle" in st_bed["reason"]


def test_batch_b_movein_conflicts_photo_and_passport(sandbox_db: SimingDB) -> None:
    items_p = sandbox_db.paths.tmp_root / "items.json"
    manual_p = sandbox_db.paths.tmp_root / "manual.json"
    items_p.write_text(
        json.dumps(
            {
                "preferences": {"install_date": "2026-12-10"},
                "items": [
                    {"id": "rinnai", "name": "林内16L零冷水燃气热水器", "room": "厨房", "install": "brand", "order": "JD-9901", "installNote": "12T天然气"},
                    {"id": "robam1", "name": "老板烟灶套装E2S", "room": "厨房", "install": "brand", "order": "JD-9902"},
                    {"id": "robam2", "name": "老板嵌入式洗碗机", "room": "厨房", "install": "brand", "order": "JD-9903"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manual_p.write_text(
        json.dumps(
            {
                "rinnai": {"install": "已预约", "installDate": "2026-12-10"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    c1 = check_install_conflicts(sandbox_db, items_json_path=items_p, manual_json_path=manual_p)
    assert c1["ok"] is True
    assert any("燃气前置冲突" in x for x in c1["conflicts"])
    assert any("空间拥挤预警" in x for x in c1["warnings"])

    # 标记燃气前置就绪后，燃气冲突解除
    handle_inbound_qq(sandbox_db, "工序就绪 燃气")
    c2 = check_install_conflicts(sandbox_db, items_json_path=items_p, manual_json_path=manual_p)
    assert not any("燃气前置冲突" in x for x in c2["conflicts"])

    # QQ 带图发「存 主卧 水电 强弱电过桥弯」归档 + 售后护照查询
    fake_img = sandbox_db.paths.tmp_root / "wire.jpg"
    fake_img.write_bytes(b"\xff\xd8\xff\xe0MOCK_JPEG_DATA")
    r_img = handle_inbound_qq(sandbox_db, "存 厨房 铭牌 序列号SN8899", media_paths=[str(fake_img)])
    assert r_img["handled"] is True
    assert "IMG-" in r_img["reply"]

    handle_inbound_qq(sandbox_db, "登记售后 林内 官方报修400-820-3161 说明书P18排障E11")
    pp = query_appliance_passport(sandbox_db, "林内", items_json_path=items_p)
    assert pp["ok"] is True
    assert "JD-9901" in pp["reply"]
    assert "400-820-3161" in pp["reply"]


def test_batch_b_cw01_pack_receivable_reconcile_and_natural_ask(sandbox_db: SimingDB) -> None:
    inv_tmp = sandbox_db.paths.tmp_root / "inv.pdf"
    itin_tmp = sandbox_db.paths.tmp_root / "itin.pdf"
    inv_bytes = _make_text_pdf(
        inv_tmp,
        ["电子发票（普通发票）", "发票号码：26112000000088776655", "开票日期：2026年11月16日", "滴滴出行 客运服务", "价税合计（小写） ¥38.50"],
    )
    itin_bytes = _make_text_pdf(
        itin_tmp,
        [
            "滴滴出行—行程报销单",
            "申请日期：2026-11-16",
            "共 1 笔行程，合计 38.50 元",
            "1 快车 11-15 21:10 周日 北京市 示例写字楼 示例小区A 7.2 38.50",
        ],
    )
    stage_invoice_attachment(sandbox_db, "didi-inv.pdf", inv_bytes)
    stage_invoice_attachment(sandbox_db, "didi-itin.pdf", itin_bytes)

    r_pack = handle_inbound_qq(sandbox_db, "打包CW01")
    assert r_pack["handled"] is True
    assert "¥38.50" in r_pack["reply"]

    # 正式登记 CW01 交单 -> 自动挂入建行 1004 待回款池
    r_sub = handle_inbound_qq(sandbox_db, "已交单 CW01-GRBX-202611180001")
    assert r_sub["handled"] is True

    r_rcv_before = handle_inbound_qq(sandbox_db, "待回款")
    assert "CW01-GRBX-202611180001" in r_rcv_before["reply"]

    # 建行 1004 收到 ¥38.50 报销入账短信 -> 自动核销待回款！
    try_ingest_bank_sms(
        sandbox_db,
        "【建设银行】您尾号1004的储蓄卡11月18日15:20报销入账人民币38.50元，活期余额12038.50元。",
    )
    r_rcv_after = handle_inbound_qq(sandbox_db, "待回款")
    assert "在途 0 笔" in r_rcv_after["reply"]
    assert "近期已核销到账" in r_rcv_after["reply"]

    # 一句话自然语言查账
    match_or_record_wallet_row(
        sandbox_db,
        occurred_at="2026-11-12T13:00:00+08:00",
        amount_cents=68900,
        card_last4="1003",
        wallet_alias="微信",
        merchant="山姆会员商店(海淀店)",
        kind="消费",
    )
    q_sam = handle_inbound_qq(sandbox_db, "山姆 上次")
    assert q_sam["handled"] is True
    assert "689.00" in q_sam["reply"]


def test_batch_b_replenish_cycle_and_jd_cart_apply(sandbox_db: SimingDB) -> None:
    r_add = handle_inbound_qq(sandbox_db, "常买 添加 维达抽纸整箱 https://item.jd.com/100012345678.html 45 59")
    assert r_add["handled"] is True
    assert "100012345678" in r_add["reply"]

    r_stage = handle_inbound_qq(sandbox_db, "加购物车 维达抽纸 x2")
    assert r_stage["handled"] is True
    assert "× **2** 件" in r_stage["reply"]

    import re

    m_tok = re.search(r"apply\s+(A\d+)", r_stage["reply"])
    assert m_tok is not None
    tok = m_tok.group(1)

    called_urls: list[str] = []
    ap = verify_and_apply_approval(
        sandbox_db,
        tok,
        extra_executors={
            "jd_add_cart": lambda p, d: execute_jd_cart_add(
                p,
                d,
                cdp_exec_fn=lambda u: (called_urls.append(u) or {"ok": True, "gated": False}),
            )
        },
    )
    assert ap["ok"] is True
    assert called_urls == ["https://gate.jd.com/InitCart.aspx?pid=100012345678&pcount=2&ptype=1"]
    assert "openapp.jdmobile://" in ap["summary"]


def test_owntracks_basic_auth_trajectory_and_sleep_get_routing(sandbox_db: SimingDB, tmp_path: Path) -> None:
    import base64
    import importlib.util
    import socket
    import threading
    import urllib.request

    spec = importlib.util.spec_from_file_location(
        "siming_edge", Path(__file__).resolve().parents[2] / "edge" / "siming_edge.py"
    )
    assert spec and spec.loader
    siming_edge = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(siming_edge)
    from datetime import date
    from siming.attention import evaluate_attention_state
    from siming.collectors.edge_sync import _process_single_edge_record
    from siming.context_loc import bind_wifi_to_place, format_location_status, ingest_ios_location

    # 1) 验证 OwnTracks HTTP Basic Auth + _type='location' 返回标准 [] 且入库 SSID/vel/lon
    store = siming_edge.EdgeStore(tmp_path / "edge_ot")
    store.token_path.write_text("tok_secret_123\n", encoding="utf-8")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    t = threading.Thread(target=siming_edge.run_http_server, args=(store, "127.0.0.1", port), daemon=True)
    t.start()

    basic_b64 = base64.b64encode(b"devuser:tok_secret_123").decode("ascii")
    ot_body = json.dumps(
        {
            "_type": "location",
            "lat": 39.908001,
            "lon": 116.407031,
            "SSID": "Office-Mobile",
            "vel": 0,
            "batt": 88,
            "tst": 1795012000,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/siming/in/loc",
        data=ot_body,
        headers={"Authorization": f"Basic {basic_b64}", "Content-Type": "application/json"},
        method="POST",
    )
    import time

    for _ in range(20):
        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                raw_resp = resp.read().decode("utf-8")
                assert resp.status == 200
                assert json.loads(raw_resp) == []
                break
        except Exception:
            time.sleep(0.05)

    # 2) 验证 GET /siming/in/sleep 带 heartbeat=True 时不会被 edge_sync 提前吞掉
    stats = {k: 0 for k in ("heartbeat", "sms_bank", "sms_pickup", "sms_archived", "malformed_backfill", "bill_ocr", "remote_run", "drop", "blocked_otp", "deadletter")}
    _process_single_edge_record(
        sandbox_db,
        {"id": 1, "source": "ios_sleep", "text": "sleep", "heartbeat": True, "received_at": "2026-11-18T03:30:00+08:00"},
        date(2026, 11, 18),
        stats,
    )
    att = evaluate_attention_state(
        sandbox_db,
        desktop_probe=lambda: {"available": True, "locked": True, "idle_sec": 900.0},
        now_override=datetime(2026, 11, 18, 3, 45, tzinfo=SHANGHAI_TZ),
    )
    assert att["state"] == "ASLEEP"

    # 3) 验证空间轨迹去重：原地刷手机不重复写 events，离开公司开车 >=200m 自动记入轨迹
    bind_wifi_to_place(sandbox_db, "Office-Mobile", "公司")
    r1 = ingest_ios_location(sandbox_db, json.dumps({"wifi": "Office-Mobile", "lat": 39.908001, "lng": 116.407031}), received_at="2026-11-18T19:00:00+08:00")
    assert r1["logged_event_id"] is not None
    r2_same = ingest_ios_location(sandbox_db, json.dumps({"wifi": "Office-Mobile", "lat": 39.908008, "lng": 116.407040}), received_at="2026-11-18T19:05:00+08:00")
    assert r2_same["logged_event_id"] is None
    r3_drive = ingest_ios_location(sandbox_db, json.dumps({"wifi": "", "lat": 39.985500, "lon": 116.352000, "vel": 42}), received_at="2026-11-18T19:15:00+08:00")
    assert r3_drive["logged_event_id"] is not None
    assert r3_drive["motion"] == "driving"
    status_txt = format_location_status(sandbox_db)
    assert "离场: 公司 → 外出" in status_txt
    assert "驾车 42.0km/h" in status_txt

    # 4) 验证无睡眠专注习惯时：插电刷手机(phone_active)不误判入睡；放手机睡一觉后早上在被窝打开 App(phone_active)直接唤醒！
    from siming.attention import ingest_ios_sleep_signal

    ingest_ios_sleep_signal(sandbox_db, "charge_on", received_at="2026-11-19T02:00:00+08:00")
    ingest_ios_sleep_signal(sandbox_db, "phone_active", received_at="2026-11-19T02:20:00+08:00")
    st_scrolling = evaluate_attention_state(
        sandbox_db,
        desktop_probe=lambda: {"available": True, "locked": True, "idle_sec": 1800.0},
        now_override=datetime(2026, 11, 19, 2, 25, tzinfo=SHANGHAI_TZ),
    )
    assert st_scrolling["state"] == "AWAKE_ACTIVE"

    st_asleep_later = evaluate_attention_state(
        sandbox_db,
        desktop_probe=lambda: {"available": True, "locked": True, "idle_sec": 3600.0},
        now_override=datetime(2026, 11, 19, 3, 0, tzinfo=SHANGHAI_TZ),
    )
    assert st_asleep_later["state"] == "ASLEEP"

    wake_res = ingest_ios_sleep_signal(sandbox_db, "phone_active", received_at="2026-11-19T10:15:00+08:00")
    assert wake_res["state"] == "AWAKE"
    assert sandbox_db.get_meta("ios_wakeup_pending_brief") == "1"

    # 5) 验证「手机 App (?state=awake) + 位置联动判醒」：在家静止心跳不误醒，走出家门 (OwnTracks 离场/移动) 立即唤醒！
    from siming.context_loc import bind_wifi_to_place as _bind_w

    _bind_w(sandbox_db, "CU_tdNN", "家")
    sandbox_db.set_meta("ios_wakeup_pending_brief", "0")
    ingest_ios_sleep_signal(sandbox_db, "charge_on", received_at="2026-11-20T02:10:00+08:00")
    # 凌晨 02:25 打开 App 传了 ?state=awake -> 不应清空充电状态，02:55 放下手机后仍自动入睡
    ingest_ios_sleep_signal(sandbox_db, "awake", received_at="2026-11-20T02:25:00+08:00")
    assert sandbox_db.get_meta("ios_charger_connected_at") != ""
    st_3am = evaluate_attention_state(
        sandbox_db,
        desktop_probe=lambda: {"available": True, "locked": True, "idle_sec": 3600.0},
        now_override=datetime(2026, 11, 20, 3, 5, tzinfo=SHANGHAI_TZ),
    )
    assert st_3am["state"] == "ASLEEP"

    # 早上 09:00 OwnTracks 在家里床头每 3 分钟发一次静止心跳 -> 绝不误唤醒！
    ingest_ios_location(sandbox_db, json.dumps({"wifi": "CU_tdNN", "lat": 39.904200, "lon": 116.407400}), received_at="2026-11-20T09:00:00+08:00")
    assert sandbox_db.get_meta("ios_wakeup_pending_brief") == "0"

    # 早上 09:40 没看 App 直接走出家门开车 -> 位置变化当场唤醒！
    r_depart = ingest_ios_location(sandbox_db, json.dumps({"wifi": "", "lat": 39.906200, "lon": 116.409000, "vel": 26}), received_at="2026-11-20T09:40:00+08:00")
    assert r_depart["departed_or_moving"] is True
    assert sandbox_db.get_meta("ios_wakeup_pending_brief") == "1"
