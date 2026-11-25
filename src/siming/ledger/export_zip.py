"""微信 / 支付宝 / 抖音 / 美团 / 滴滴导出账单与行程单（ZIP/CSV/XLSX/PDF）临时解压解析器：
- 严格在 $TMPDIR/siming/ 内阅后即焚，零落盘残留；
- 纯内存流式 pyzipper 解密（同时支持 PKZIP ZipCrypto 与 WinZip AES-256），彻底根除 bsdtar --passphrase 的 ps 命令行泄露；
- 强防压缩炸弹（成员数上限 + 单文件与累计实际解压字节流式硬熔断）；
- 美团订单与滴滴行程单优先在 ±35m 内富化已有微信/支付宝/银行卡流水（补全餐厅菜品与起终点路线），严防重复双算支出。
"""

from __future__ import annotations

import csv
import io
import os
import re
import secrets
import shutil
import zipfile
from pathlib import Path
from typing import Any, Optional

import openpyxl
import pyzipper

from siming.db import SimingDB
from siming.ledger.reconcile import (
    enrich_or_record_platform_order,
    match_or_record_wallet_row,
)
from siming.ledger.reimburse import parse_didi_itinerary_text
from siming.ledger.sms_parser import classify_txn_kind, yuan_to_cents

MAX_ZIP_MEMBERS = 40
MAX_SINGLE_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_UNCOMPRESSED_BYTES = 120 * 1024 * 1024

CARD_LAST4_IN_METHOD_RE = re.compile(r"\((\d{4})\)|（(\d{4})）|尾号(\d{4})")
PDF_TXN_LINE_RE = re.compile(
    r"(20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?)\s+(.+?)\s+([+-]?[¥￥]?[0-9]{1,7}(?:,[0-9]{3})*\.[0-9]{2})(?!\d)"
)


class ZipBombError(ValueError):
    """压缩包成员数或解压体积超出安全上限。"""


def _scrub_pwd(msg: str, password: Optional[str]) -> str:
    s = msg or ""
    if password and len(password) >= 2:
        s = s.replace(password, "***")
    return s[:200]


def _parse_rows_from_csv_bytes(raw_bytes: bytes) -> list[list[str]]:
    text = ""
    for enc in ("utf-8-sig", "gb18030", "utf-8"):
        try:
            text = raw_bytes.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if not text:
        text = raw_bytes.decode("utf-8", errors="replace")
    reader = csv.reader(io.StringIO(text))
    return [[c.strip() for c in row] for row in reader if any(c.strip() for c in row)]


def _parse_rows_from_xlsx(path: Path) -> list[list[str]]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.active
        out: list[list[str]] = []
        for row in ws.iter_rows(values_only=True):
            vals = [str(v).strip() if v is not None else "" for v in row]
            if any(vals):
                out.append(vals)
        return out
    finally:
        wb.close()


def _detect_table_platform(rows: list[list[str]], filename: str) -> str:
    fn = filename.lower()
    head_blob = "\n".join("|".join(r) for r in rows[:18])
    if any(k in fn or k in head_blob for k in ("didi", "滴滴", "高德打车", "行程报销单", "上车时间", "小桔")):
        return "didi"
    if any(k in fn or k in head_blob for k in ("meituan", "美团", "美团月付", "北京三快")):
        return "meituan"
    if any(k in fn or k in head_blob for k in ("douyin", "抖音", "合众易宝", "抖音支付", "抖音月付")):
        return "douyin"
    if any(k in fn or k in head_blob for k in ("alipay", "支付宝")):
        return "alipay"
    return "wechat"


def _normalize_row_iso_ts(ts_raw: str) -> Optional[str]:
    cleaned = (ts_raw or "").strip().replace("/", "-").replace("年", "-").replace("月", "-").replace("日", "")
    m_full = re.match(r"^(20\d{2})-(\d{1,2})-(\d{1,2})(?:\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?)?", cleaned)
    if m_full:
        return (
            f"{int(m_full.group(1)):04d}-{int(m_full.group(2)):02d}-{int(m_full.group(3)):02d}"
            f"T{int(m_full.group(4) or 12):02d}:{int(m_full.group(5) or 0):02d}:{int(m_full.group(6) or 0):02d}+08:00"
        )
    # 兼容滴滴行程表无年份的 "11-12 21:35"
    m_short = re.match(r"^(\d{1,2})-(\d{1,2})\s+(\d{1,2}):(\d{1,2})(?::(\d{1,2}))?", cleaned)
    if m_short:
        from siming.db import now_local

        yr = now_local().year
        return (
            f"{yr:04d}-{int(m_short.group(1)):02d}-{int(m_short.group(2)):02d}"
            f"T{int(m_short.group(3)):02d}:{int(m_short.group(4)):02d}:{int(m_short.group(5) or 0):02d}+08:00"
        )
    return None


def _ingest_table_rows(db: SimingDB, rows: list[list[str]], platform_hint: str) -> dict[str, int]:
    header_idx = -1
    col_map: dict[str, int] = {}
    time_headers = ("交易时间", "付款时间", "交易创建时间", "创建时间", "下单时间", "上车时间", "出发时间", "入账时间")
    amt_headers = ("金额(元)", "金额（元）", "实付金额", "支付金额", "交易金额", "发生额", "总价", "金额")

    for idx, r in enumerate(rows):
        joined = "|".join(r)
        if any(th in joined for th in time_headers) and any(ah in joined for ah in amt_headers):
            header_idx = idx
            for c_i, name in enumerate(r):
                col_map[name.strip()] = c_i
            break

    if header_idx < 0:
        return {"ingested": 0, "matched": 0, "ambiguous": 0}

    def _col(row: list[str], *candidates: str) -> str:
        for cand in candidates:
            for k, ci in col_map.items():
                if cand in k and ci < len(row):
                    return row[ci].strip().strip("'\t ")
        return ""

    ingested = 0
    matched = 0
    ambiguous = 0

    for row in rows[header_idx + 1 :]:
        ts_raw = _col(row, "交易时间", "付款时间", "交易创建时间", "创建时间", "下单时间", "上车时间", "出发时间", "入账时间")
        iso_ts = _normalize_row_iso_ts(ts_raw)
        if not iso_ts:
            continue

        amt_raw = _col(row, "金额(元)", "金额（元）", "实付金额", "支付金额", "交易金额", "发生额", "总价", "金额")
        if not amt_raw:
            continue
        try:
            cents = yuan_to_cents(amt_raw)
        except Exception:
            continue
        if cents == 0:
            continue

        status_str = _col(row, "当前状态", "交易状态", "订单状态")
        if any(s in status_str for s in ("交易关闭", "未付款", "等待付款", "已取消", "解冻")):
            continue

        pay_method = _col(row, "收/付款方式", "支付方式", "支付渠道")
        m_l4 = CARD_LAST4_IN_METHOD_RE.search(pay_method)
        last4 = next((g for g in m_l4.groups() if g), "") if m_l4 else ""
        order_id = _col(row, "交易单号", "交易订单号", "美团订单号", "抖音支付单号", "平台交易单号", "订单号", "流水号")
        direction = _col(row, "收/支", "收支类型", "资金方向")

        # 滴滴出行表格：提取车型 + 起点→终点，富化已有支付流水防双算
        if platform_hint == "didi":
            car_type = _col(row, "车型", "产品类型", "服务类型") or "打车"
            origin = _col(row, "起点", "出发地", "上车地点")
            dest = _col(row, "终点", "目的地", "下车地点")
            route_desc = f"{origin}→{dest}" if (origin and dest) else (origin or dest or _col(row, "商户名称", "商品说明"))
            detail_mer = f"滴滴{car_type} · {route_desc}".strip(" ·")[:80]
            kind = "退款" if ("退款" in status_str or amt_raw.strip().startswith("+")) else "消费"
            res = enrich_or_record_platform_order(
                db=db,
                occurred_at=iso_ts,
                amount_cents=cents,
                platform="didi",
                detail_merchant=detail_mer or "滴滴出行",
                kind=kind,
                channel_order_id=order_id,
                card_last4=last4,
            )
            if not res.get("duplicate"):
                ingested += 1
            if res.get("status") == "已对账":
                matched += 1
            elif res.get("status") == "待核实":
                ambiguous += 1
            continue

        # 美团账单表格：提取店铺 + 菜品/订单标题（支持美团月付），富化已有支付流水防双算
        if platform_hint == "meituan":
            shop = _col(row, "商户名称", "店铺名称", "交易对方")
            item_title = _col(row, "商品名称", "订单标题", "商品说明", "交易说明")
            parts = [x for x in (shop, item_title) if x and x != "/"]
            raw_mer = " · ".join(parts)
            if not raw_mer.startswith("美团"):
                detail_mer = f"美团 · {raw_mer}" if raw_mer else "美团消费"
            else:
                detail_mer = raw_mer
            kind = "退款" if ("退款" in status_str or direction == "收入" or amt_raw.strip().startswith("+")) else "消费"
            res = enrich_or_record_platform_order(
                db=db,
                occurred_at=iso_ts,
                amount_cents=cents,
                platform="meituan",
                detail_merchant=detail_mer[:80],
                kind=kind,
                channel_order_id=order_id,
                card_last4=last4,
            )
            if not res.get("duplicate"):
                ingested += 1
            if res.get("status") == "已对账":
                matched += 1
            elif res.get("status") == "待核实":
                ambiguous += 1
            continue

        counterparty = _col(row, "交易对方", "商户名称", "店铺名称")
        goods = _col(row, "商品说明", "商品名称", "交易说明", "摘要", "商品")
        merchant_full = " · ".join(x for x in (counterparty, goods) if x and x != "/")[:80]
        kind = classify_txn_kind(f"{direction} {merchant_full} {status_str} {pay_method}")
        if ("退款" in status_str or amt_raw.strip().startswith("+")) and direction in ("收入", "/", "不计收支", "退回", ""):
            kind = "退款"
        elif direction == "不计收支" and kind == "消费":
            kind = "划转"

        if "抖音月付" in pay_method:
            wallet_alias = "抖音月付"
        elif "美团月付" in pay_method:
            wallet_alias = "美团月付"
        elif "零钱" in pay_method or "余额" in pay_method:
            if platform_hint == "douyin":
                wallet_alias = "抖音零钱"
            elif platform_hint == "alipay":
                wallet_alias = "支付宝余额"
            else:
                wallet_alias = "微信零钱"
        elif "花呗" in pay_method:
            wallet_alias = "支付宝花呗"
        else:
            default_map = {"wechat": "微信账单", "alipay": "支付宝账单", "douyin": "抖音账单"}
            wallet_alias = pay_method or default_map.get(platform_hint, platform_hint)

        res = match_or_record_wallet_row(
            db=db,
            occurred_at=iso_ts,
            amount_cents=cents,
            card_last4=last4,
            wallet_alias=wallet_alias,
            merchant=merchant_full or wallet_alias,
            kind=kind,
            channel_order_id=order_id,
            source=f"export_{platform_hint}",
        )
        if not res.get("duplicate"):
            ingested += 1
        if res.get("status") == "已对账":
            matched += 1
        elif res.get("status") == "待核实":
            ambiguous += 1

    return {"ingested": ingested, "matched": matched, "ambiguous": ambiguous}


def _ingest_pdf_statement_rows(
    db: SimingDB, pdf_path: Path, password: Optional[str] = None
) -> dict[str, Any]:
    """解析滴滴行程报销单 PDF、美团/抖音支付 PDF 及微信/支付宝电子交易流水证明 PDF（支持密码解密）。"""
    from pypdf import PdfReader

    reader = PdfReader(str(pdf_path))
    if reader.is_encrypted:
        if not password or reader.decrypt(password) == 0:
            raise RuntimeError("PDF 已加密且未提供正确密码")

    full_text = "\n".join((pg.extract_text() or "") for pg in reader.pages)
    plat = _detect_table_platform([[full_text[:2000]]], pdf_path.name)

    # 1. 滴滴/高德《行程报销单》PDF：提取每笔行程上车时间 + 车型 + 起点→终点，富化已有流水
    if plat == "didi" or "行程报销单" in full_text:
        itin = parse_didi_itinerary_text(full_text)
        if itin["trips"]:
            ingested = 0
            matched = 0
            ambiguous = 0
            for tr in itin["trips"]:
                detail_mer = f"滴滴{tr['car_type']} · {tr['route']}"[:80]
                res = enrich_or_record_platform_order(
                    db=db,
                    occurred_at=tr["occurred_at"],
                    amount_cents=tr["amount_cents"],
                    platform="didi",
                    detail_merchant=detail_mer,
                    kind="消费",
                )
                if not res.get("duplicate"):
                    ingested += 1
                if res.get("status") == "已对账":
                    matched += 1
                elif res.get("status") == "待核实":
                    ambiguous += 1
            return {"ingested": ingested, "matched": matched, "ambiguous": ambiguous}

    default_map = {"wechat": "微信账单", "alipay": "支付宝账单", "douyin": "抖音账单", "meituan": "美团账单"}
    default_wallet = default_map.get(plat, "电子流水")

    ingested = 0
    matched = 0
    ambiguous = 0

    # 2. 识别「抖音支付交易凭证」标准多行记录块
    douyin_block_re = re.compile(
        r"^(20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s+(支出|收入|其他)\s+([0-9]+(?:\.[0-9]{2}))\s+(.+)$",
        re.DOTALL,
    )
    chunks = re.split(r"(?=20\d{2}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+)", full_text)
    parsed_any_block = False

    for chunk in chunks:
        cleaned = re.sub(r"\s+\d+\s*/\s*\d+\s*", " ", chunk)
        cleaned = re.sub(r"交易时间\s+收/支/其他[^\n]*", " ", cleaned)
        cleaned = re.sub(r"第\s*\d+\s*页\s*共\s*\d+\s*页[^\n]*", " ", cleaned)
        m_dy = douyin_block_re.match(cleaned.strip())
        if not m_dy:
            continue
        parsed_any_block = True
        ts_raw, direction, amt_raw, rest = m_dy.group(1), m_dy.group(2), m_dy.group(3), m_dy.group(4)
        cents = yuan_to_cents(amt_raw)
        if cents == 0:
            continue

        rest_joined = re.sub(r"([（(][^）)]*?)\s*\n\s*([^）)]*?[）)])", r"\1\2", rest)
        rest_flat = re.sub(r"\s+", " ", rest_joined).strip()

        pay_method = ""
        order_id = ""
        merchant = default_wallet
        m_tail = re.match(r"^(.+?)\s+(\d{16,36})\s+(.+)$", rest_flat)
        if m_tail:
            pay_method = m_tail.group(1).strip()
            order_id = m_tail.group(2).strip()
            mer_raw = m_tail.group(3).strip()
            mer_clean = re.sub(r"\s+[0-9A-Za-z_-]{8,40}(?:\s+\d{1,12})?$", "", mer_raw).strip()
            merchant = (mer_clean or mer_raw)[:60]
        else:
            merchant = rest_flat[:60]

        m_l4 = CARD_LAST4_IN_METHOD_RE.search(pay_method)
        last4 = next((g for g in m_l4.groups() if g), "") if m_l4 else ""
        if "抖音月付" in pay_method:
            wallet_alias = "抖音月付"
        elif "零钱" in pay_method or "余额" in pay_method:
            wallet_alias = "抖音零钱" if plat == "douyin" else default_wallet
        else:
            wallet_alias = pay_method if (pay_method and pay_method != "退款") else default_wallet

        if "退款" in pay_method or (direction == "收入" and "退款" in rest_flat):
            kind = "退款"
        else:
            kind = classify_txn_kind(f"{direction} {pay_method} {merchant}")

        iso_ts = ts_raw.replace(" ", "T") + "+08:00"
        res = match_or_record_wallet_row(
            db=db,
            occurred_at=iso_ts,
            amount_cents=cents,
            card_last4=last4,
            wallet_alias=wallet_alias,
            merchant=merchant,
            kind=kind,
            channel_order_id=order_id,
            source=f"export_{plat}_pdf",
        )
        if not res.get("duplicate"):
            ingested += 1
        if res.get("status") == "已对账":
            matched += 1
        elif res.get("status") == "待核实":
            ambiguous += 1

    if parsed_any_block:
        return {"ingested": ingested, "matched": matched, "ambiguous": ambiguous}

    for ln in full_text.splitlines():
        m = PDF_TXN_LINE_RE.search(ln.strip())
        if not m:
            continue
        ts_raw, mid_desc, amt_raw = m.group(1), m.group(2).strip(), m.group(3)
        cents = yuan_to_cents(amt_raw)
        if cents == 0:
            continue
        iso_ts = ts_raw.replace(" ", "T")
        if len(iso_ts) == 16:
            iso_ts += ":00"
        iso_ts += "+08:00"
        kind = "退款" if ("退款" in mid_desc or amt_raw.startswith("+")) else classify_txn_kind(mid_desc)
        if plat == "meituan":
            res = enrich_or_record_platform_order(
                db=db,
                occurred_at=iso_ts,
                amount_cents=cents,
                platform="meituan",
                detail_merchant=(mid_desc if "美团" in mid_desc else f"美团 · {mid_desc}")[:80],
                kind=kind,
            )
        else:
            m_l4 = CARD_LAST4_IN_METHOD_RE.search(mid_desc)
            last4 = next((g for g in m_l4.groups() if g), "") if m_l4 else ""
            res = match_or_record_wallet_row(
                db=db,
                occurred_at=iso_ts,
                amount_cents=cents,
                card_last4=last4,
                wallet_alias=default_wallet,
                merchant=mid_desc[:60],
                kind=kind,
                source=f"export_{plat}_pdf",
            )
        if not res.get("duplicate"):
            ingested += 1
        if res.get("status") == "已对账":
            matched += 1
        elif res.get("status") == "待核实":
            ambiguous += 1
    return {"ingested": ingested, "matched": matched, "ambiguous": ambiguous}


def _stream_extract_member(zf: Any, info: Any, out_path: Path, total_written_so_far: int) -> int:
    """流式分块解压单个成员并严格累计真实字节数，防篡改 ZIP 头部绕过炸弹限额。"""
    member_bytes = 0
    fd = os.open(str(out_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with zf.open(info, "r") as src:
            while True:
                chunk = src.read(65536)
                if not chunk:
                    break
                member_bytes += len(chunk)
                if member_bytes > MAX_SINGLE_MEMBER_BYTES:
                    raise ZipBombError(f"单文件解压体积超过 {MAX_SINGLE_MEMBER_BYTES // (1024 * 1024)}MB 安全上限")
                if total_written_so_far + member_bytes > MAX_TOTAL_UNCOMPRESSED_BYTES:
                    raise ZipBombError(f"压缩包累计解压体积超过 {MAX_TOTAL_UNCOMPRESSED_BYTES // (1024 * 1024)}MB 安全上限")
                os.write(fd, chunk)
    finally:
        os.close(fd)
    return member_bytes


def _extract_zip_into_scratch(archive_path: Path, scratch: Path, password: Optional[str]) -> list[Path]:
    """使用纯 Python pyzipper 流式解密（同时兼容传统 ZipCrypto 与支付宝/微信 AES-256），零子进程命令行口令暴露。"""
    allowed_exts = (".csv", ".xlsx", ".pdf")
    extracted: list[Path] = []
    pwd_bytes = password.encode("utf-8") if password else None

    try:
        with pyzipper.AESZipFile(archive_path, "r") as zf:
            if pwd_bytes:
                zf.setpassword(pwd_bytes)
            infos = [i for i in zf.infolist() if not i.is_dir()]
            if len(infos) > MAX_ZIP_MEMBERS:
                raise ZipBombError(f"压缩包文件成员数 ({len(infos)}) 超过安全上限 ({MAX_ZIP_MEMBERS})")

            declared_total = 0
            for info in infos:
                if info.file_size > MAX_SINGLE_MEMBER_BYTES:
                    raise ZipBombError(f"成员声明大小超过 {MAX_SINGLE_MEMBER_BYTES // (1024 * 1024)}MB 安全上限")
                declared_total += max(0, int(info.file_size))
            if declared_total > MAX_TOTAL_UNCOMPRESSED_BYTES:
                raise ZipBombError(f"压缩包声明总大小超过 {MAX_TOTAL_UNCOMPRESSED_BYTES // (1024 * 1024)}MB 安全上限")

            actual_total = 0
            for idx, info in enumerate(infos):
                raw_fn = info.filename
                # 处理部分旧版国内账单 GBK 文件名
                try:
                    raw_fn_fixed = raw_fn.encode("cp437").decode("gb18030")
                    raw_fn = raw_fn_fixed
                except Exception:
                    pass
                safe_name = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", Path(raw_fn).name)
                if not safe_name.lower().endswith(allowed_exts):
                    continue
                out_f = scratch / f"{idx:02d}-{safe_name}"
                actual_total += _stream_extract_member(zf, info, out_f, actual_total)
                extracted.append(out_f)
        return extracted
    except ZipBombError:
        raise
    except (RuntimeError, zipfile.BadZipFile, pyzipper.zipfile.BadZipFile, ValueError) as exc:
        err_s = _scrub_pwd(str(exc), password)
        if any(k in err_s.lower() for k in ("password", "encrypted", "bad password", "requires a password")):
            raise RuntimeError(f"需要正确解压密码: {err_s}") from exc
        raise RuntimeError(err_s) from exc


def import_bill_archive(
    db: SimingDB,
    archive_path: Path,
    password: Optional[str] = None,
    delete_source_archive: bool = False,
) -> dict[str, Any]:
    """在 $TMPDIR/siming/ 建一次性 0700 目录解压微信/支付宝/抖音/美团/滴滴账单，落库后当场彻底删除临时文件。"""
    db.init_db()
    if not archive_path.is_file():
        return {"ok": False, "error": f"文件不存在: {archive_path}"}

    scratch = db.paths.tmp_root / f"bill-unpack-{secrets.token_hex(6)}"
    scratch.mkdir(parents=True, exist_ok=True)
    os.chmod(scratch, 0o700)

    total_ingested = 0
    total_matched = 0
    total_ambiguous = 0

    try:
        if archive_path.suffix.lower() == ".zip":
            target_files = _extract_zip_into_scratch(archive_path, scratch, password)
        else:
            if archive_path.stat().st_size > MAX_SINGLE_MEMBER_BYTES:
                raise ZipBombError("账单原文件体积超过 64MB 安全上限")
            copied = scratch / re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", archive_path.name)
            copied.write_bytes(archive_path.read_bytes())
            os.chmod(copied, 0o600)
            target_files = [copied]

        for tf in target_files:
            ext = tf.suffix.lower()
            if ext == ".pdf":
                st = _ingest_pdf_statement_rows(db, tf, password=password)
            else:
                rows = _parse_rows_from_xlsx(tf) if ext == ".xlsx" else _parse_rows_from_csv_bytes(tf.read_bytes())
                plat = _detect_table_platform(rows, tf.name)
                st = _ingest_table_rows(db, rows, plat)
            total_ingested += st["ingested"]
            total_matched += st["matched"]
            total_ambiguous += st["ambiguous"]

        if delete_source_archive and str(archive_path.resolve()).startswith(str(db.paths.tmp_root.resolve())):
            archive_path.unlink(missing_ok=True)
    except ZipBombError as zb_exc:
        return {
            "ok": False,
            "zip_bomb": True,
            "needs_password": False,
            "error": f"安全熔断拦截：{_scrub_pwd(str(zb_exc), password)}",
        }
    except RuntimeError as exc:
        safe_err = _scrub_pwd(str(exc), password)
        needs_pwd = any(k in safe_err.lower() for k in ("密码", "password", "encrypted"))
        return {
            "ok": False,
            "needs_password": needs_pwd,
            "error": f"解压或解密失败（请核对解压密码）：{safe_err}",
        }
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    db.write_audit(
        "A1",
        "import_bill_archive",
        {"file": archive_path.name, "ingested": total_ingested, "matched": total_matched, "ambiguous": total_ambiguous},
    )
    return {
        "ok": True,
        "ingested": total_ingested,
        "matched": total_matched,
        "ambiguous": total_ambiguous,
    }
