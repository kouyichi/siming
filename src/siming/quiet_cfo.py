"""Batch B-2 (`quiet_cfo`)：安静账房（CW01 国内票夹月底自动组包 + 报销/保价真到账自动核销 + 一句话自然语言查账）。
1. CW01 国内出行/餐饮组包：自动扫描 `~/.local/share/siming/reimburse/<YYYY-MM>/`，把配好对的滴滴「电子发票 ↔ 行程报销单」复制重命名进独立草稿包（兼容 `mark-submitted CW01-GRBX-...` 正式归档），并列出缺票缺口；
2. 应收回款闭环（Receivables）：交单 `CW08`/`CW01` 或登记保价退差后自动挂入待回款池；银行卡（如建行 1004 / 中行 1003 / 光大 1001）收到同额入账/退款短信即自动配对销账；
3. 一句话查账：0-token 只读 SQL 解析 `查账 外卖 本月`、`滴滴 10月`、`山姆 上次`、`瑞幸 今年`，秒回笔数、总额、均价、环比与 Top 明细。
"""

from __future__ import annotations

import calendar
import csv
import json
import os
import re
import shutil
import subprocess
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Optional

from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local
from siming.ledger.reimburse import summarize_reimbursement_bundle
from siming.modules import is_module_enabled


def _cents_from_yuan(val: float | str) -> int:
    return int((Decimal(str(val).replace("¥", "").replace("￥", "").replace(",", "").strip()) * Decimal(100)).to_integral_value(rounding=ROUND_HALF_UP))


def pack_cw01_domestic_bundle(
    db: SimingDB,
    year_month: str = "",
    include_dining: bool = False,
    render_png: bool = True,
) -> dict[str, Any]:
    """将当月已配对的滴滴「电子发票 ↔ 行程报销单」（及可选餐饮票）一键打成国内差旅待提草稿包。"""
    db.init_db()
    ym = (year_month or now_local().strftime("%Y-%m")).strip()
    bundle = summarize_reimbursement_bundle(db, year_month=ym)
    paired = list(bundle.get("paired_rides") or [])
    dining = list(bundle.get("dining_invoices") or []) if include_dining else []

    if not paired and not dining:
        gaps = len(bundle.get("missing_itinerary_invoices") or []) + len(bundle.get("missing_invoice_itineraries") or [])
        gap_note = f"（当前有 {gaps} 份未配齐的单边打车发票/行程单，补发到邮箱后即可自动配对）" if gaps else ""
        return {
            "ok": False,
            "error": f"{ym} 暂无可打包的已配对打车票据{gap_note}。",
            "bundle": bundle,
        }

    stamp = now_local().strftime("%Y%m%d-%H%M%S")
    draft_id = f"draft-cw01-{ym.replace('-', '')}-{stamp}"
    draft_dir = db.paths.reimburse_dir / "drafts" / draft_id
    pdfs_dir = draft_dir / "attachments"
    pngs_dir = draft_dir / "png_200dpi"
    pdfs_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(draft_dir, 0o700)

    src_dir = Path(bundle["bundle_dir"])
    csv_rows: list[dict[str, Any]] = []
    total_cents = 0
    pdftoppm_bin = shutil.which("pdftoppm")
    if render_png and pdftoppm_bin:
        pngs_dir.mkdir(parents=True, exist_ok=True)

    seq = 0
    for pr in paired:
        seq += 1
        amt_y = float(pr["amount_yuan"])
        amt_c = _cents_from_yuan(f"{amt_y:.2f}")
        total_cents += amt_c
        inv_src = src_dir / pr["invoice_file"]
        itin_src = src_dir / pr["itinerary_file"]
        inv_dst = pdfs_dir / f"{seq:02d}-打车-{amt_y:.2f}元-电子发票.pdf"
        itin_dst = pdfs_dir / f"{seq:02d}-打车-{amt_y:.2f}元-行程报销单.pdf"
        if inv_src.is_file():
            shutil.copy2(inv_src, inv_dst)
            if render_png and pdftoppm_bin:
                subprocess.run([pdftoppm_bin, "-png", "-r", "200", str(inv_dst), str(pngs_dir / f"{seq:02d}-发票")], capture_output=True, timeout=15)
        if itin_src.is_file():
            shutil.copy2(itin_src, itin_dst)
            if render_png and pdftoppm_bin:
                subprocess.run([pdftoppm_bin, "-png", "-r", "200", str(itin_dst), str(pngs_dir / f"{seq:02d}-行程单")], capture_output=True, timeout=15)
        csv_rows.append(
            {
                "序号": seq,
                "费用类型": "市内交通费(滴滴)",
                "发票号码": pr.get("invoice_no") or "",
                "金额_元": f"{amt_y:.2f}",
                "行程路线": " | ".join(pr.get("routes") or []),
                "发票文件": inv_dst.name,
                "行程单文件": itin_dst.name,
            }
        )

    for dn in dining:
        seq += 1
        amt_y = float(dn["amount_yuan"])
        amt_c = _cents_from_yuan(f"{amt_y:.2f}")
        total_cents += amt_c
        dn_src = Path(dn["path"])
        dn_dst = pdfs_dir / f"{seq:02d}-餐饮-{amt_y:.2f}元-电子发票.pdf"
        if dn_src.is_file():
            shutil.copy2(dn_src, dn_dst)
        csv_rows.append(
            {
                "序号": seq,
                "费用类型": "餐饮费",
                "发票号码": dn.get("invoice_no") or "",
                "金额_元": f"{amt_y:.2f}",
                "行程路线": "",
                "发票文件": dn_dst.name,
                "行程单文件": "-",
            }
        )

    csv_path = draft_dir / "CW01报销明细汇总.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["序号", "费用类型", "发票号码", "金额_元", "行程路线", "发票文件", "行程单文件"])
        writer.writeheader()
        writer.writerows(csv_rows)

    total_yuan = round(total_cents / 100.0, 2)
    meta = {
        "draft_id": draft_id,
        "form_kind": "CW01",
        "year_month": ym,
        "status": "DRAFT_READY",
        "paired_ride_count": len(paired),
        "dining_count": len(dining),
        "total_cny_cents": total_cents,
        "total_cny_yuan": total_yuan,
        "receipts": [
            {
                "receipt_no": r["发票号码"] or f"CW01-{ym}-{r['序号']}",
                "amount": r["金额_元"],
                "currency": "CNY",
                "file": r["发票文件"],
            }
            for r in csv_rows
        ],
        "created_at": iso_now(),
    }
    (draft_dir / "draft_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    db.set_meta("latest_reimburse_draft_dir", str(draft_dir))
    db.record_human_use("quiet_cfo", "pack_cw01", ref_id=draft_id)

    lines = [
        f"🧾 **CW01 国内票夹草稿包已就绪** [`{draft_id}`]",
        f"• 月份：`{ym}` ｜ 成对打车票：**{len(paired)}** 组" + (f" · 餐饮票：{len(dining)} 张" if dining else "") + f" ｜ 合计：**¥{total_yuan:.2f}**",
        f"• 目录：`{draft_dir}`（含重命名配对 PDF 与 `CW01报销明细汇总.csv`）",
    ]
    if bundle.get("missing_itinerary_invoices"):
        lines.append(f"• ⚠️ 尚有 {len(bundle['missing_itinerary_invoices'])} 张打车发票缺行程单未入包")
    if bundle.get("missing_invoice_itineraries"):
        lines.append(f"• ⚠️ 尚有 {len(bundle['missing_invoice_itineraries'])} 份行程单缺电子发票未入包")
    lines.append("👉 OA 提单拿到单号后发一句 `已交单 CW01-GRBX-202611xxxxxx`，司命自动归档并盯建行 1004 回款到账！")
    return {
        "ok": True,
        "draft_id": draft_id,
        "draft_dir": str(draft_dir),
        "total_yuan": total_yuan,
        "paired_count": len(paired),
        "summary": "\n".join(lines),
    }


def _load_receivables(db: SimingDB) -> list[dict[str, Any]]:
    raw = db.get_meta("quiet_cfo_receivables") or "[]"
    try:
        data = json.loads(raw)
        return [x for x in data if isinstance(x, dict)] if isinstance(data, list) else []
    except Exception:
        return []


def _save_receivables(db: SimingDB, items: list[dict[str, Any]]) -> None:
    db.set_meta("quiet_cfo_receivables", json.dumps(items[-120:], ensure_ascii=False))


def register_receivable(
    db: SimingDB,
    ref_id: str,
    title: str,
    expected_yuan: float | str,
    kind: str = "reimburse",
    card_hint: str = "1004",
) -> dict[str, Any]:
    """登记一笔待到账回款（如 CW08/CW01 报销款或京东保价退差/退货退款），供入账短信自动配对销账。"""
    db.init_db()
    cents = _cents_from_yuan(expected_yuan)
    if cents <= 0:
        return {"ok": False, "error": "待回款金额必须大于 0"}
    items = _load_receivables(db)
    rec_id = (ref_id or f"RCV-{now_local().strftime('%Y%m%d%H%M%S')}").strip()
    items = [x for x in items if x.get("id") != rec_id]
    entry = {
        "id": rec_id,
        "kind": kind,
        "title": title.strip()[:80],
        "expected_cents": cents,
        "expected_yuan": round(cents / 100.0, 2),
        "card_hint": card_hint,
        "status": "PENDING",
        "created_at": iso_now(),
        "matched_txn_id": "",
        "received_at": "",
    }
    items.append(entry)
    _save_receivables(db, items)
    return {
        "ok": True,
        "entry": entry,
        "summary": f"📌 已挂入待回款追踪 [`{rec_id}`]：{entry['title']} **¥{entry['expected_yuan']:.2f}**（预计入卡尾号 `{card_hint or '任意'}`，到账短信到达即自动核销）。",
    }


def match_incoming_receivable(
    db: SimingDB,
    txn_id: str,
    amount_cents: int,
    last4: str,
    kind: str,
    raw_text: str = "",
) -> Optional[dict[str, Any]]:
    """当银行卡收到「收入 / 退款」短信时，自动比对待回款池（±5 分钱容差）并销账。"""
    if kind not in ("收入", "退款") or amount_cents <= 0:
        return None
    items = _load_receivables(db)
    candidates = [
        it
        for it in items
        if it.get("status") == "PENDING" and abs(int(it.get("expected_cents") or 0) - amount_cents) <= 5
    ]
    if not candidates:
        return None

    # 若存在尾号一致的优先选尾号一致的
    same_card = [it for it in candidates if last4 and str(it.get("card_hint") or "") == last4]
    chosen = same_card[0] if same_card else candidates[0]
    chosen["status"] = "RECEIVED"
    chosen["matched_txn_id"] = txn_id
    chosen["received_at"] = iso_now()
    _save_receivables(db, items)

    if is_module_enabled(db, "quiet_cfo"):
        with db.transaction() as conn:
            db.enqueue_effect(
                conn,
                "send_qq",
                {
                    "ref_id": str(chosen["id"]),
                    "rule_id": "quiet_cfo",
                    "text": (
                        f"💰 **待回款已真到账核销**：`{chosen['title']}`\n"
                        f"• 实收：**¥{amount_cents / 100.0:.2f}**（尾号 `{last4 or '-'}` · 流水 `{txn_id}`）"
                    ),
                },
            )
    return chosen


def format_receivables_report(db: SimingDB) -> str:
    items = _load_receivables(db)
    pending = [x for x in items if x.get("status") == "PENDING"]
    done = [x for x in reversed(items) if x.get("status") == "RECEIVED"][:5]
    lines = [f"💰 **待回款与报销/退差到账追踪**（在途 {len(pending)} 笔）"]
    if pending:
        tot = sum(int(x["expected_cents"]) for x in pending) / 100.0
        lines.append(f"• 在途合计：**¥{tot:.2f}**")
        for p in pending[:10]:
            lines.append(f"  - ⏳ [`{p['id']}`] {p['title']} — **¥{p['expected_yuan']:.2f}**（盯尾号 {p.get('card_hint') or '任意'} · 建于 {str(p.get('created_at',''))[:10]}）")
    else:
        lines.append("• 当前无未到账的报销或退差款项（可发 `待回款 保价退差 50` 登记追踪）。")
    if done:
        lines.append("• 近期已核销到账：" + "；".join(f"{d['title']}(¥{d['expected_yuan']:.2f})" for d in done))
    return "\n".join(lines)


CATEGORY_PATTERNS: dict[str, tuple[str, ...]] = {
    "外卖": ("美团", "三快", "饿了么", "拉扎斯", "外卖", "瑞幸", "麦当劳", "肯德基", "星巴克", "奈雪"),
    "美团": ("美团", "三快"),
    "打车": ("滴滴", "小桔", "高德打车", "曹操", "出行", "首汽"),
    "滴滴": ("滴滴", "小桔"),
    "咖啡": ("瑞幸", "星巴克", "Manner", "咖啡", "lkcoffee"),
    "瑞幸": ("瑞幸", "lkcoffee"),
    "山姆": ("山姆", "沃尔玛"),
    "京东": ("京东", "网银在线"),
    "nous": ("nous", "nousresearch"),
}


def _resolve_time_window(q: str, now: datetime) -> tuple[str, str, str, Optional[tuple[str, str]]]:
    """返回 (label, start_iso, end_iso, prev_period_range_for_mom)。"""
    yr, mo = now.year, now.month
    if "上月" in q or "上个月" in q:
        p_yr, p_mo = (yr - 1, 12) if mo == 1 else (yr, mo - 1)
        last_d = calendar.monthrange(p_yr, p_mo)[1]
        return (
            f"{p_yr}-{p_mo:02d}（上月）",
            f"{p_yr:04d}-{p_mo:02d}-01T00:00:00",
            f"{p_yr:04d}-{p_mo:02d}-{last_d:02d}T23:59:59",
            None,
        )
    m_ym = re.search(r"(?:(20\d{2})[-年/])?(\d{1,2})\s*月", q)
    if m_ym and "本月" not in q:
        t_yr = int(m_ym.group(1)) if m_ym.group(1) else yr
        t_mo = max(1, min(12, int(m_ym.group(2))))
        last_d = calendar.monthrange(t_yr, t_mo)[1]
        return (
            f"{t_yr}-{t_mo:02d}",
            f"{t_yr:04d}-{t_mo:02d}-01T00:00:00",
            f"{t_yr:04d}-{t_mo:02d}-{last_d:02d}T23:59:59",
            None,
        )
    if "今年" in q or "本年" in q:
        return (f"{yr} 全年", f"{yr:04d}-01-01T00:00:00", f"{yr:04d}-12-31T23:59:59", None)
    if "近7天" in q or "最近一周" in q or "本周" in q:
        s_dt = (now - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00")
        return ("近 7 天", s_dt, now.strftime("%Y-%m-%dT23:59:59"), None)
    if "上次" in q or "最近一次" in q:
        return ("最近记录", "2020-01-01T00:00:00", "2099-12-31T23:59:59", None)

    # 默认本月 + 计算上月同期用于环比
    last_d = calendar.monthrange(yr, mo)[1]
    p_yr, p_mo = (yr - 1, 12) if mo == 1 else (yr, mo - 1)
    p_last_d = calendar.monthrange(p_yr, p_mo)[1]
    return (
        f"{yr}-{mo:02d}（本月）",
        f"{yr:04d}-{mo:02d}-01T00:00:00",
        f"{yr:04d}-{mo:02d}-{last_d:02d}T23:59:59",
        (f"{p_yr:04d}-{p_mo:02d}-01T00:00:00", f"{p_yr:04d}-{p_mo:02d}-{p_last_d:02d}T23:59:59"),
    )


def query_ledger_natural(db: SimingDB, raw_query: str) -> dict[str, Any]:
    """零模型只读 SQL 一句话查账：支持品类/商户词 + 时间窗 +「上次」查询。"""
    db.init_db()
    raw_q = re.sub(r"^\s*\[Voice\]\s*", "", (raw_query or "").strip(), flags=re.IGNORECASE).strip()
    raw_q = re.sub(r"[。！？!?~～\s]+$", "", raw_q).strip()
    q = re.sub(r"^(?:查账|账本查询|花了多少|消费查询)\s*", "", raw_q).strip()
    now = now_local()
    win_label, start_iso, end_iso, prev_range = _resolve_time_window(q, now)
    is_last_once = "上次" in q or "最近一次" in q

    clean_kw = re.sub(
        r"(本月|这个月|当月|上月|上个月|今年|本年|近7天|最近7天|最近一周|这周|本周|上次|最近一次|"
        r"20\d{2}[-年/]\d{1,2}月份?|\d{1,2}月份?|花了多少钱?|花多少钱?|多少钱|花了多少|花多少|花销|消费了?|支出了?|开销|"
        r"一共|总共|总计|大概|大约|帮我|替我|给我|查一下|查下|看看|看下|算一下|算下|统计一下|统计|查询|账单|我|在|去|买|的|了)",
        "",
        q,
    ).strip(" ，,。！？!?、:：")

    terms: tuple[str, ...] = ()
    if clean_kw:
        terms = CATEGORY_PATTERNS.get(clean_kw.lower()) or CATEGORY_PATTERNS.get(clean_kw) or (clean_kw,)

    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT txn_id, occurred_at, amount_cents, currency, account_alias, kind, merchant, status
            FROM ledger_txns
            WHERE kind = '消费' AND status != 'VOID_DUPLICATE'
              AND occurred_at >= ? AND occurred_at <= ?
            ORDER BY occurred_at DESC
            """,
            (start_iso, end_iso + "Z"),
        ).fetchall()

        matched = [
            dict(r)
            for r in rows
            if not terms
            or any(t.lower() in f"{r['merchant'] or ''} {r['account_alias'] or ''}".lower() for t in terms)
        ]

        prev_cny_cents = 0
        if prev_range is not None:
            p_rows = conn.execute(
                """
                SELECT amount_cents, currency, merchant, account_alias
                FROM ledger_txns
                WHERE kind = '消费' AND currency = 'CNY' AND status != 'VOID_DUPLICATE'
                  AND occurred_at >= ? AND occurred_at <= ?
                """,
                (prev_range[0], prev_range[1] + "Z"),
            ).fetchall()
            prev_cny_cents = sum(
                int(r["amount_cents"])
                for r in p_rows
                if not terms
                or any(t.lower() in f"{r['merchant'] or ''} {r['account_alias'] or ''}".lower() for t in terms)
            )
    finally:
        conn.close()

    db.record_human_use("quiet_cfo", "ask_ledger", ref_id=clean_kw or "all")

    if not matched:
        return {
            "ok": True,
            "count": 0,
            "reply": f"📊 **查账结果（{win_label} · {clean_kw or '全部消费'}）**：暂无匹配的消费记录。",
        }

    if is_last_once:
        top = matched[0]
        sym = "¥" if top["currency"] == "CNY" else f"{top['currency']} "
        return {
            "ok": True,
            "count": 1,
            "reply": (
                f"📊 **最近一次「{clean_kw or '消费'}」记录**：\n"
                f"• 时间：`{str(top['occurred_at'])[:16].replace('T', ' ')}`\n"
                f"• 商户：**{top['merchant'] or '未标商户'}** ｜ 金额：**{sym}{int(top['amount_cents']) / 100.0:.2f}**（{top['account_alias']}）"
            ),
        }

    by_ccy: dict[str, list[dict[str, Any]]] = {}
    for m in matched:
        ccy = str(m.get("currency") or "CNY").upper()
        by_ccy.setdefault(ccy, []).append(m)

    cny_list = by_ccy.get("CNY") or []
    cny_total = sum(int(x["amount_cents"]) for x in cny_list) / 100.0
    lines = [f"📊 **一句话查账：{win_label} · {clean_kw or '全品类消费'}**"]

    if cny_list:
        avg_y = cny_total / len(cny_list)
        mom_note = ""
        if prev_range is not None and prev_cny_cents > 0:
            prev_y = prev_cny_cents / 100.0
            diff_pct = ((cny_total - prev_y) / prev_y) * 100.0
            arrow = "↑" if diff_pct > 0 else "↓"
            mom_note = f"（上月全月 ¥{prev_y:.0f}，当前 {arrow}{abs(diff_pct):.0f}%）"
        lines.append(f"• 人民币：**{len(cny_list)}** 笔 · 合计 **¥{cny_total:.2f}** · 笔均 ¥{avg_y:.1f}{mom_note}")

    for ccy, items_c in sorted(by_ccy.items()):
        if ccy == "CNY":
            continue
        tot_f = sum(int(x["amount_cents"]) for x in items_c) / 100.0
        lines.append(f"• {ccy}：**{len(items_c)}** 笔 · 合计 **{ccy} {tot_f:.2f}**")

    top_5 = sorted(matched, key=lambda x: int(x["amount_cents"]), reverse=True)[:5]
    lines.append("• 金额 Top 明细：")
    for r in top_5:
        sym = "¥" if r["currency"] == "CNY" else f"{r['currency']} "
        lines.append(f"  - `{str(r['occurred_at'])[:10]}` {r['merchant'] or r['account_alias']} — **{sym}{int(r['amount_cents'])/100.0:.2f}**")

    return {"ok": True, "count": len(matched), "cny_total": cny_total, "reply": "\n".join(lines)}
