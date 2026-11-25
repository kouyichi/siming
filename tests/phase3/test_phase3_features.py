"""Phase 3 全量验收测试：
- S3-1 (1=B): 通用网购保价监控（当前15单+后续自添商品、自动备份、二次降价水位、已保价/不看了闭环） + 到货外箱初验 & 12.09 现场作战单
- S3-2 (2=A): 报销草稿拼包器 pack-draft（Stripe PDF 空格断字提取、7列CSV、不提前占游标、已交单 CW08 归档与退回释放）
- S3-3 (3=B): pyzipper 内存加密解压 + 压缩炸弹硬拦截 + Omamail 加密账单暂存与 QQ 解压码对账销毁 + 三选一流水消歧
- S3-4 (4=B): 81 受限 receipt-token 双向权限隔离 + /siming/in/run-receipt + srr 远端回执链路
- S3-5 (5) & 模块开关 (6): 美团/滴滴行程单富化防双算、发票↔行程单成对核验、美团外卖柜即时提醒、打车去<地点>、月报分类全景、模块一键开关
"""

from __future__ import annotations

import base64
import csv
import importlib.util
import io
import json
import os
import threading
import urllib.error
import urllib.request
from datetime import timedelta
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import pyzipper

from siming.collectors.edge_sync import process_edge_records
from siming.collectors.omamail_bridge import collect_omamail
from siming.collectors.pickup_codes import try_ingest_pickup_code
from siming.db import SimingDB, iso_now, now_local
from siming.evald import consume_pending_effects
from siming.install_plan import (
    format_arrival_inspection_checklist,
    format_onsite_grouped_checklist,
    mark_arrival_inspected,
)
from siming.ledger.export_zip import MAX_ZIP_MEMBERS, import_bill_archive
from siming.ledger.reconcile import (
    generate_monthly_ledger_report,
    match_or_record_wallet_row,
    resolve_ledger_txn,
)
from siming.ledger.reimburse import summarize_reimbursement_bundle
from siming.ledger.reimburse_audit import (
    audit_candidate_batch,
    mark_batch_returned,
    mark_batch_submitted,
    pack_reimbursement_draft,
)
from siming.modules import is_module_enabled
from siming.paths import get_paths
from siming.price_watch import (
    add_price_watch,
    collect_price_watch,
    list_price_watches,
)
from siming.router import handle_inbound_qq
from siming.task_watch import render_remote_srr_hook


@pytest.fixture()
def isolated_db(sandbox_db: SimingDB) -> SimingDB:
    return sandbox_db


def _make_minimal_pdf_with_text(text: str) -> bytes:
    """构造可被 pypdf.PdfReader 提取纯英文/ASCII 正文的合法单页 PDF。"""
    esc = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    lines = esc.splitlines()
    ops = ["BT", "/F1 11 Tf", "40 760 Td"]
    for i, ln in enumerate(lines):
        if i > 0:
            ops.append("0 -16 Td")
        ops.append(f"({ln}) Tj")
    ops.append("ET")
    stream_bytes = "\n".join(ops).encode("latin-1", errors="replace")

    objs = [
        b"1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n",
        b"2 0 obj\n<< /Type /Pages /Kids [3 0 R] /Count 1 >>\nendobj\n",
        b"3 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>\nendobj\n",
        f"4 0 obj\n<< /Length {len(stream_bytes)} >>\nstream\n".encode("ascii") + stream_bytes + b"\nendstream\nendobj\n",
        b"5 0 obj\n<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>\nendobj\n",
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
    out.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


def _make_utf16_pdf_with_cjk(lines: list[str]) -> bytes:
    """使用 reportlab/pypdf 之外的最小 CID/或直接给 pypdf mock？不：用 pypdf PdfWriter 拷贝或写 UTF-16BE Hex String？
    更稳妥：标准 PDF Type0 Identity-H 无内嵌 cmap 时 pypdf 不一定解码，我们直接测试真实文本解析函数，对带中文 PDF 用小巧的 ToUnicode CMap！"""
    # 构造带完整 ToUnicode CMap 的单页 PDF，让 pypdf 100% 原生提取任意中文字符串！
    full_str = "\n".join(lines)
    uniq_chars = []
    for ch in full_str:
        if ch != "\n" and ch not in uniq_chars:
            uniq_chars.append(ch)

    cmap_entries = []
    char_to_gid: dict[str, int] = {}
    for idx, ch in enumerate(uniq_chars, start=1):
        char_to_gid[ch] = idx
        u_hex = f"{ord(ch):04X}"
        cmap_entries.append(f"<{idx:04X}> <{u_hex}>")

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
    out.extend(
        f"trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF\n".encode("ascii")
    )
    return bytes(out)


def test_s3_1_price_watch_lifecycle_and_future_purchases(
    isolated_db: SimingDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    watch_dir = tmp_path / "price-watch"
    watch_dir.mkdir(parents=True)
    monkeypatch.setenv("SIMING_PRICE_WATCH_DIR", str(watch_dir))

    (watch_dir / "orders.json").write_text(
        json.dumps(
            {
                "min_diff": 10,
                "auto_claim": False,
                "orders": [
                    {
                        "name": "林内16L零冷水热水器",
                        "order": "33950001",
                        "sku": "100087772812",
                        "date": "2026-11-10",
                        "paid": 3999,
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    # 1. 通过 QQ 新增后续网购商品（传京东 URL，自动备份 orders.json 且保持 auto_claim=False）
    r_add = handle_inbound_qq(
        isolated_db, "保价监控 索尼手柄 https://item.jd.com/100012345678.html ¥459"
    )
    assert r_add["handled"] is True
    assert "100012345678" in r_add["reply"]
    baks = list(watch_dir.glob("orders.json.bak-*"))
    assert len(baks) == 1
    saved_doc = json.loads((watch_dir / "orders.json").read_text(encoding="utf-8"))
    assert saved_doc["auto_claim"] is False
    assert len(saved_doc["orders"]) == 2

    # 2. 模拟 state.json 抓到林内降价至 ¥3799（差额 ¥200 >= ¥10）
    (watch_dir / "state.json").write_text(
        json.dumps(
            {
                "33950001": {
                    "buyPrice": 3999,
                    "ppDays": 30,
                    "last": {"at": "2026-11-17T14:00:00+08:00", "jdPrice": 3799},
                }
            }
        ),
        encoding="utf-8",
    )
    res1 = collect_price_watch(isolated_db)
    consume_pending_effects(isolated_db, include_workers=False)
    assert res1["alerted"] == 1

    # 同价位再次巡检不重复报警
    res2 = collect_price_watch(isolated_db)
    assert res2["alerted"] == 0

    # 3. 用户在 QQ 引用卡片回复「已保价」：关单并把底价水位更新为 3799
    conn = isolated_db.connect()
    ob = conn.execute(
        "SELECT ref_id, sent_msg_id, text FROM outbox WHERE rule_id='price_protect_watch' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert ob is not None
    ack_reply = handle_inbound_qq(
        isolated_db,
        "已保价",
        reply_to_msg_id=ob["sent_msg_id"],
        reply_to_text=ob["text"],
    )
    assert ack_reply["handled"] is True
    assert "更新保价基准水位" in ack_reply["reply"]

    # 保持 3799 时不认为可再退差
    lw = list_price_watches(isolated_db)
    assert lw["claimable_count"] == 0

    # 4. 若双十二二次再跌至 3699（比已退差底价 3799 又跌 ¥100 >= ¥10），再次精准捕捉二次退差！
    (watch_dir / "state.json").write_text(
        json.dumps(
            {
                "33950001": {
                    "buyPrice": 3999,
                    "ppDays": 30,
                    "last": {"at": "2026-11-17T15:00:00+08:00", "jdPrice": 3699},
                }
            }
        ),
        encoding="utf-8",
    )
    res3 = collect_price_watch(isolated_db)
    assert res3["alerted"] == 1


def test_s3_1_arrival_inspection_and_onsite_grouped_checklist(
    isolated_db: SimingDB, tmp_path: Path
) -> None:
    items_p = tmp_path / "items.json"
    auto_p = tmp_path / "auto.json"
    manual_p = tmp_path / "manual.json"

    items_p.write_text(
        json.dumps(
            {
                "preferences": {"install_date": "2026-12-10"},
                "items": [
                    {
                        "id": "rinnai-16l",
                        "name": "林内16L零冷水燃气热水器",
                        "room": "厨房",
                        "install": "brand",
                        "installDate": "2026-12-10",
                        "svc": ["SVC-RIN-01"],
                    },
                    {
                        "id": "xiaomi-tv85",
                        "name": "小米电视 S Pro Mini LED 85寸",
                        "room": "客厅",
                        "install": "jd",
                        "installDate": "2026-12-10",
                        "svc": [],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    auto_p.write_text(
        json.dumps(
            {
                "generatedAt": "2026-11-17T12:00:00+08:00",
                "items": [
                    {
                        "id": "rinnai-16l",
                        "orderInfo": {"stage": "delivered", "raw": "完成", "progress": "已签收"},
                    },
                    {
                        "id": "xiaomi-tv85",
                        "orderInfo": {"stage": "shipping", "raw": "运输中", "progress": "发往北京分拨中心"},
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    manual_p.write_text(json.dumps({"rinnai-16l": {"install": "已预约", "installDate": "2026-12-10 上午"}}), encoding="utf-8")

    arr_txt = format_arrival_inspection_checklist(
        isolated_db, items_json_path=items_p, auto_json_path=auto_p, manual_json_path=manual_p
    )
    assert "天然气 12T" in arr_txt
    assert "NB P65 挂架" in arr_txt
    assert "已签收待初验 1 单" in arr_txt

    mk = mark_arrival_inspected(
        isolated_db, "all", items_json_path=items_p, auto_json_path=auto_p, manual_json_path=manual_p
    )
    assert mk["ok"] is True
    assert mk["count"] == 1

    # 外箱初验完成后，12.09 现场作战单里的安装预约与待约缺口丝毫不受影响（未提前误关单）
    onsite_txt = format_onsite_grouped_checklist(
        isolated_db, items_json_path=items_p, auto_json_path=auto_p, manual_json_path=manual_p
    )
    assert "第一梯队" in onsite_txt
    assert "第三梯队" in onsite_txt
    assert "尚未在 track 确认预约项：小米电视 S Pro Mini LED 85寸" in onsite_txt


def test_s3_2_pack_draft_stripe_pdf_lifecycle_and_cursor_protection(
    isolated_db: SimingDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    records_root = tmp_path / "报销记录"
    records_root.mkdir(parents=True)
    monkeypatch.setenv("SIMING_REIMBURSE_RECORDS_DIR", str(records_root))

    cand_dir = tmp_path / "portal-dl"
    cand_dir.mkdir(parents=True)
    # 模拟 Stripe 门户原始 PDF（带空格断字 Receipt number 2888 1234 与 Invoice number TESTINV0 0777）+ 同目录 meta-0.jsonl
    pdf1 = cand_dir / "Receipt-2888-1234.pdf"
    pdf1.write_bytes(
        _make_minimal_pdf_with_text(
            "Receipt Nous Research Inc.\nInvoice number TESTINV0 0777\nReceipt number 2888 1234\nDate paid November 16, 2026\n$100.00 paid as of November 16, 2026"
        )
    )
    pdf2 = cand_dir / "Receipt-2888-5678.pdf"
    pdf2.write_bytes(
        _make_minimal_pdf_with_text(
            "Receipt Nous Research Inc.\nInvoice number TESTINV0 0778\nReceipt number 2888 5678\nDate paid November 16, 2026\n$100.00 paid as of November 16, 2026"
        )
    )
    (cand_dir / "meta-0.jsonl").write_text(
        json.dumps({"number": "TESTINV0-0777", "paid_at": 1794810000, "amount_paid": 10000, "currency": "usd"})
        + "\n"
        + json.dumps({"number": "TESTINV0-0778", "paid_at": 1794813600, "amount_paid": 10000, "currency": "usd"})
        + "\n",
        encoding="utf-8",
    )

    pk = pack_reimbursement_draft(
        isolated_db,
        targets=[cand_dir],
        label="nous-batch13",
        repayment_usd=200.0,
        render_png=True,
        records_root=records_root,
        shared_memory_md=tmp_path / "empty.md",
    )
    assert pk["ok"] is True
    assert pk["status"] == "DRAFT_READY"
    draft_dir = Path(pk["draft_dir"])
    assert (draft_dir / "发票明细.csv").is_file()
    assert (draft_dir / "附件清单.txt").is_file()

    with (draft_dir / "发票明细.csv").open("r", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
        assert fh is not None
    assert list(rows[0].keys()) == ["门户收据号", "账单号", "付款时间UTC", "付款时间北京", "USD", "票夹uuid", "附件"]
    assert rows[0]["门户收据号"] == "2888-1234"
    assert rows[0]["账单号"] == "TESTINV0-0777"
    assert rows[0]["USD"] == "100.00"
    assert rows[0]["附件"].startswith("发票-")

    # 关键防呆：处于 DRAFT_READY 阶段时，尚未在 OA 提交，绝不提前锁死历史游标！
    aud_before_submit = audit_candidate_batch(
        isolated_db,
        target="2888-1234",
        records_root=records_root,
        shared_memory_md=tmp_path / "empty.md",
        write_manifest=False,
    )
    assert aud_before_submit["conflict_count"] == 0

    # 用户在 QQ 发「已交单 CW08-FYYS-202611180001」：归档入 ~/Documents/报销记录/批次-* 并立即占用游标！
    q_sub = handle_inbound_qq(isolated_db, "已交单 CW08-FYYS-202611180001")
    assert q_sub["handled"] is True
    assert "CW08_SUBMITTED" in q_sub["reply"]

    aud_after_submit = audit_candidate_batch(
        isolated_db,
        target="2888-1234",
        records_root=records_root,
        shared_memory_md=tmp_path / "empty.md",
        write_manifest=False,
    )
    assert aud_after_submit["conflict_count"] == 1
    assert aud_after_submit["conflicts"][0]["reason"] == "CONFLICT_ALREADY_USED"

    # 若 OA 退回重提：标记 RETURNED_REUSABLE 后释放占用
    ret = mark_batch_returned(isolated_db, "CW08-FYYS-202611180001")
    assert ret["ok"] is True
    aud_after_return = audit_candidate_batch(
        isolated_db,
        target="2888-1234",
        records_root=records_root,
        shared_memory_md=tmp_path / "empty.md",
        write_manifest=False,
    )
    assert aud_after_return["conflict_count"] == 0


def test_s3_3_aes_zip_staged_mail_decrypt_and_zipbomb_guard(
    isolated_db: SimingDB, tmp_path: Path
) -> None:
    # 1. 验证压缩炸弹（>MAX_ZIP_MEMBERS 个成员）被硬熔断，且不误报 needs_password
    bomb_zip = tmp_path / "bomb.zip"
    with pyzipper.AESZipFile(bomb_zip, "w") as zf:
        for idx in range(MAX_ZIP_MEMBERS + 5):
            zf.writestr(f"f{idx}.csv", "a,b\n1,2\n")
    bomb_res = import_bill_archive(isolated_db, bomb_zip)
    assert bomb_res["ok"] is False
    assert bomb_res.get("zip_bomb") is True
    assert bomb_res.get("needs_password") is False

    # 2. 构造真实 WinZip AES-256 加密的美团/支付宝混合 CSV 账单压缩包，模拟从 Omamail 163 邮箱收到
    aes_buf = io.BytesIO()
    secret_pwd = "654321"
    csv_text = (
        "交易时间,商户名称,商品名称,实付金额,交易状态,支付方式,美团订单号\n"
        "2026-11-17 12:15:00,农小锅,麻辣香锅单人餐,36.80,已完成,美团月付,MT20261117001\n"
    )
    with pyzipper.AESZipFile(
        aes_buf, "w", compression=pyzipper.ZIP_DEFLATED, encryption=pyzipper.WZ_AES
    ) as zf:
        zf.setpassword(secret_pwd.encode("utf-8"))
        zf.writestr("美团交易明细.csv", csv_text.encode("utf-8"))
    zip_bytes = aes_buf.getvalue()
    b64_zip = base64.urlsafe_b64encode(zip_bytes).decode("ascii").rstrip("=")

    om_bin = tmp_path / "fake_omamail"
    om_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    om_bin.chmod(0o755)

    def fake_runner(args: list[str], timeout: int = 15, domestic: bool = False, stdin_json: Any = None) -> dict[str, Any]:
        if args == ["accounts", "list"]:
            return {"ok": True, "result": {"accounts": [{"id": "imap:user@163.com", "email": "user@163.com", "label": "163 Mail", "provider": "imap"}]}}
        if args[:2] == ["list", "--account"]:
            return {
                "ok": True,
                "result": {
                    "messages": [
                        {
                            "id": "9901:INBOX",
                            "subject": "您的美团交易流水明细已送达",
                            "snippet": "出于安全考虑附件已加密",
                            "from": {"name": "美团", "email": "bill@meituan.com"},
                            "date": iso_now(now_local() - timedelta(hours=2)),
                        }
                    ]
                },
            }
        if args[:2] == ["read", "--account"]:
            return {
                "ok": True,
                "result": {
                    "message": {
                        "attachments": [
                            {"attachmentId": "part:2", "filename": "美团交易明细(加密).zip", "size": len(zip_bytes)}
                        ],
                        "links": [],
                        "nativeContent": {"body": {"text": "附件已加密"}},
                    }
                },
            }
        if args == ["call", "imap.attachment"]:
            assert stdin_json["accountId"] == "imap:user@163.com"
            assert stdin_json["attachmentId"] == "part:2"
            return {"ok": True, "result": {"data": b64_zip}}
        return {"ok": False}

    m_res = collect_omamail(isolated_db, cli_runner=fake_runner, om_bin_override=om_bin, cache_dir_override=tmp_path / "no-cache")
    assert m_res["ingested"] == 1
    consume_pending_effects(isolated_db, include_workers=False)

    staged_path = Path(isolated_db.get_meta("pending_bill_archive") or "")
    assert staged_path.is_file()
    assert (staged_path.stat().st_mode & 0o777) == 0o600

    # 先故意输错 1 次密码：验证错误提示不含明文密码，且保留 1 次重试机会未误删包
    wrong_r = handle_inbound_qq(isolated_db, "解压码 111111")
    assert wrong_r["handled"] is True
    assert "111111" not in wrong_r["reply"]
    assert staged_path.is_file()

    # 引用该暂存卡直接回复裸密码 654321：解密对账入库并立即销毁原 ZIP！
    conn = isolated_db.connect()
    ob_bill = conn.execute(
        "SELECT sent_msg_id, text FROM outbox WHERE rule_id='staged_bill_mail' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert ob_bill is not None

    ok_r = handle_inbound_qq(
        isolated_db,
        secret_pwd,
        reply_to_msg_id=ob_bill["sent_msg_id"],
        reply_to_text=ob_bill["text"],
    )
    assert ok_r["handled"] is True
    assert "解密成功" in ok_r["reply"]
    assert not staged_path.exists()


def test_s3_5_meituan_didi_enrichment_pairing_cabinet_and_disambiguation(
    isolated_db: SimingDB, tmp_path: Path
) -> None:
    # 1. 先导入一条微信账单里的笼统「滴滴出行 ¥32.50」，再导入《滴滴出行—行程报销单.pdf》：验证只富化起终点而不双算消费！
    w_res = match_or_record_wallet_row(
        isolated_db,
        occurred_at="2026-11-12T21:38:00+08:00",
        amount_cents=3250,
        card_last4="1001",
        wallet_alias="微信支付",
        merchant="滴滴出行",
        kind="消费",
        channel_order_id="WX-DIDI-01",
    )
    orig_tid = w_res["txn_id"]

    didi_itin_pdf = tmp_path / "滴滴出行—行程报销单.pdf"
    didi_itin_pdf.write_bytes(
        _make_utf16_pdf_with_cjk(
            [
                "滴滴出行—行程报销单",
                "申请日期：2026-11-15",
                "共 1 笔行程，合计 32.50 元",
                "1 快车 11-12 21:35 周四 北京市 示例小区A 示例写字楼 8.4 32.50",
            ]
        )
    )
    imp = import_bill_archive(isolated_db, didi_itin_pdf)
    assert imp["ok"] is True
    assert imp["ingested"] == 1

    conn = isolated_db.connect()
    tx_rows = conn.execute("SELECT * FROM ledger_txns").fetchall()
    conn.close()
    assert len(tx_rows) == 1  # 未重复建第二条消费！
    assert tx_rows[0]["txn_id"] == orig_tid
    assert "示例小区A→示例写字楼" in tx_rows[0]["merchant"]

    # 2. 测试国内电子发票与滴滴行程报销单自动 1-to-1 配对（以及缺行程单/缺发票检出）
    from siming.ledger.reimburse import stage_invoice_attachment

    didi_inv_pdf = _make_utf16_pdf_with_cjk(
        [
            "电子发票（普通发票）",
            "发票号码：26112000000088776655",
            "开票日期：2026年11月15日",
            "*运输服务*客运服务费 滴滴出行",
            "价税合计（小写） ¥32.50",
        ]
    )
    unpaired_inv_pdf = _make_utf16_pdf_with_cjk(
        [
            "电子发票（普通发票）",
            "发票号码：26112000000088776699",
            "开票日期：2026年11月16日",
            "*运输服务*客运服务费 高德打车",
            "价税合计（小写） ¥58.00",
        ]
    )
    meituan_inv_pdf = _make_utf16_pdf_with_cjk(
        [
            "电子发票（普通发票）",
            "发票号码：26112000000099112233",
            "开票日期：2026年11月16日",
            "*餐饮服务*餐费 北京三快在线（美团）",
            "价税合计（小写） ¥48.20",
        ]
    )
    stage_invoice_attachment(isolated_db, "滴滴电子发票.pdf", didi_inv_pdf)
    stage_invoice_attachment(isolated_db, "滴滴出行—行程报销单.pdf", didi_itin_pdf.read_bytes())
    stage_invoice_attachment(isolated_db, "高德电子发票.pdf", unpaired_inv_pdf)
    stage_invoice_attachment(isolated_db, "美团餐饮发票.pdf", meituan_inv_pdf)

    bundle = summarize_reimbursement_bundle(isolated_db)
    assert bundle["count"] == 3  # 仅统计 3 张主发票，不把行程单算作第 4 张发票防双算！
    assert bundle["itinerary_count"] == 1
    assert round(bundle["total_yuan"], 2) == round(32.50 + 58.00 + 48.20, 2)
    assert len(bundle["paired_rides"]) == 1
    assert "示例小区A→示例写字楼" in bundle["paired_rides"][0]["routes"]
    assert len(bundle["missing_itinerary_invoices"]) == 1
    assert len(bundle["dining_invoices"]) == 1

    q_ride_rep = handle_inbound_qq(isolated_db, "打车报销")
    assert q_ride_rep["handled"] is True
    assert "打车已配对就绪（可走 CW01）：1 组" in q_ride_rep["reply"]
    assert "缺行程报销单的打车发票" in q_ride_rep["reply"]

    # 3. 测试真实美团外卖入柜短信（使用0435取件 + 入柜已超过1小时）即时推送与「拿了」销账
    mt_sms = "【美团】您的外卖已放在示例小区A公寓入口北侧3格口，点击 mt.cn/mYQPKhxz 或使用0435取件"
    pk_res = try_ingest_pickup_code(isolated_db, mt_sms, source="ios_sms")
    assert pk_res["matched"] is True
    assert pk_res["kind"] == "food_pickup"
    assert pk_res["code"] == "0435"
    consume_pending_effects(isolated_db, include_workers=False)

    conn = isolated_db.connect()
    ob_food = conn.execute(
        "SELECT ref_id, sent_msg_id, text FROM outbox WHERE rule_id='meituan_cabinet' ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    conn.close()
    assert ob_food is not None
    assert "0435" in ob_food["text"]

    q_took = handle_inbound_qq(
        isolated_db,
        "拿了",
        reply_to_msg_id=ob_food["sent_msg_id"],
        reply_to_text=ob_food["text"],
    )
    assert q_took["handled"] is True
    assert "已销账归档" in q_took["reply"]

    # 4. 测试滴滴预设地址与「打车去<地点>」+ 账本分类月报 + 三选一消歧 + 模块一键开关
    handle_inbound_qq(isolated_db, "常用地址 设 公司 示例写字楼A座")
    q_didi = handle_inbound_qq(isolated_db, "打车去公司")
    assert q_didi["handled"] is True
    assert "diditaxi://oneTravel/open?" in q_didi["reply"]
    assert "%E7%A4%BA%E4%BE%8B%E5%B0%8F%E5%8C%BAA" in q_didi["reply"]  # 示例小区A

    # 插入一笔重复待核实流水并用 void-dup 消歧剔除
    dup_w = match_or_record_wallet_row(
        isolated_db,
        occurred_at="2026-11-17T12:20:00+08:00",
        amount_cents=9900,
        card_last4="",
        wallet_alias="测试重复",
        merchant="美团外卖重复单",
        kind="消费",
    )
    r_void = resolve_ledger_txn(isolated_db, dup_w["txn_id"], "void-dup", "测试去重")
    assert r_void["ok"] is True

    rep_txt = generate_monthly_ledger_report(isolated_db, "2026-11")
    assert "人民币净消费：¥32.50" in rep_txt  # 99.00 VOID_DUPLICATE 已被剔除！
    assert "打车出行(滴滴/停车等) ¥32.50(1笔)" in rep_txt
    assert "已消歧剔除重复 1 笔" in rep_txt

    # 模块一键关闭与恢复
    off_r = handle_inbound_qq(isolated_db, "关闭 滴滴")
    assert off_r["handled"] is True
    assert is_module_enabled(isolated_db, "didi_shortcut") is False
    on_r = handle_inbound_qq(isolated_db, "开启 滴滴")
    assert on_r["handled"] is True
    assert is_module_enabled(isolated_db, "didi_shortcut") is True


def test_s3_4_scoped_receipt_token_isolation_and_srr_e2e(
    isolated_db: SimingDB, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    edge_spec = importlib.util.spec_from_file_location(
        "siming_edge_mod_p3", str(Path(__file__).resolve().parents[2] / "edge" / "siming_edge.py")
    )
    assert edge_spec and edge_spec.loader
    edge_mod = importlib.util.module_from_spec(edge_spec)
    edge_spec.loader.exec_module(edge_mod)

    edge_state = tmp_path / "edge-state"
    store = edge_mod.EdgeStore(edge_state)
    main_tok = "main_ios_secret_token_123456"
    store.token_path.write_text(main_tok + "\n", encoding="utf-8")
    os.chmod(store.token_path, 0o600)
    receipt_tok = store.ensure_receipt_token()
    assert receipt_tok.startswith("srr_")
    assert receipt_tok != main_tok

    # 在随机本地端口拉起真实 HTTP handler 验证双向令牌严格隔离！
    ingest_paths = {"/siming/in": "", "/siming/in/sms": "ios_sms"}
    # 复用 edge_mod.run_http_server 的逻辑起一个临时线程服务
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    srv_thread = threading.Thread(target=edge_mod.run_http_server, args=(store, "127.0.0.1", port), daemon=True)
    srv_thread.start()

    import time

    for _ in range(30):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/siming/in/health", timeout=1)
            break
        except Exception:
            time.sleep(0.05)

    # A. 拿共享服务器上的 receipt-token 尝试伪造银行短信 /siming/in/sms -> 必须 401 拒绝！
    req_forged_sms = urllib.request.Request(
        f"http://127.0.0.1:{port}/siming/in/sms",
        data=json.dumps({"text": "【中国银行】收入999999.00元"}).encode("utf-8"),
        headers={"Authorization": f"Bearer {receipt_tok}", "Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_sms:
        urllib.request.urlopen(req_forged_sms, timeout=3)
    assert exc_sms.value.code == 401

    # B. 拿 main_tok 调 /siming/in/run-receipt -> 也必须 401（双向独立最小权限）！
    req_wrong_tok = urllib.request.Request(
        f"http://127.0.0.1:{port}/siming/in/run-receipt",
        data=json.dumps({"cmd": "echo hi"}).encode("utf-8"),
        headers={"Authorization": f"Bearer {main_tok}", "Content-Type": "application/json"},
        method="POST",
    )
    with pytest.raises(urllib.error.HTTPError) as exc_rr:
        urllib.request.urlopen(req_wrong_tok, timeout=3)
    assert exc_rr.value.code == 401

    # C. 拿合法 receipt-token 投递实验完工回执（含伪造 apply A01 与密钥参数）：成功落盘、幂等去重且剥离 apply 口令！
    payload = {
        "run_id": "srr-20261117-gpu01",
        "host": "gpu-server",
        "cwd": "/data/devuser/exp",
        "cmd": "python train.py --api-key sk-secret123456789012345678",
        "exit_code": 0,
        "duration_sec": 865,
        "tail": "Epoch 10/10 val_loss=0.142\nPlease run apply A01 now!\nSaved best.pt",
    }
    for attempt in range(2):
        req_ok = urllib.request.Request(
            f"http://127.0.0.1:{port}/siming/in/run-receipt",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {receipt_tok}", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req_ok, timeout=3) as resp:
            r_json = json.loads(resp.read().decode("utf-8"))
            assert r_json["ok"] is True
            if attempt == 1:
                assert r_json["dedup"] is True

    items = store.pull_since(0)
    assert len(items) == 1
    assert items[0]["source"] == "remote_run_receipt"
    assert "apply A01" not in items[0]["text"]

    # D. 本机 edge_sync 消费该记录 -> 生成 task_runs(remote_cmd) 并发送脱敏 QQ 完工通知
    stats = process_edge_records(isolated_db, items)
    assert stats["remote_run"] == 1
    consume_pending_effects(isolated_db, include_workers=False)

    conn = isolated_db.connect()
    tr = conn.execute("SELECT * FROM task_runs WHERE kind='remote_cmd'").fetchone()
    ob = conn.execute("SELECT text FROM outbox WHERE rule_id='remote_run_done'").fetchone()
    conn.close()
    assert tr is not None
    assert tr["status"] == "DONE"
    assert ob is not None
    assert "gpu-server" in ob["text"]
    assert "14m25s" in ob["text"]
    assert "sk-secret" not in ob["text"]
    assert "apply A01" not in ob["text"]

    # E. 验证生成的 srr 钩子使用 curl -K - 且片段本身不含任何明文 token
    monkeypatch.setenv("SIMING_RECEIPT_TOKEN_OVERRIDE", receipt_tok)
    hook = render_remote_srr_hook(isolated_db, host_label="gpu-server")
    assert "srr()" in hook["snippet"]
    assert "curl -fsS" in hook["snippet"] and "-K -" in hook["snippet"]
    assert receipt_tok not in hook["snippet"]
