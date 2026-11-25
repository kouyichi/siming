"""手机 QQ 与桌面端全机只读检索（<3s 带文件:行号）、受控仓库派单与一秒投递（siming drop）。"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from datetime import timedelta
from pathlib import Path
from typing import Any, Optional

from siming.cn_calendar import day_info
from siming.collectors.pickup_codes import try_ingest_pickup_code
from siming.config import load_accounts_config, load_policy
from siming.crypto import contains_otp, redact_sensitive, store_encrypted_raw
from siming.db import SimingDB, iso_now, now_local, parse_iso
from siming.enrich import detect_hard_rule_conflicts
from siming.ledger.reimburse import summarize_reimbursement_bundle
from siming.quota import fetch_ai_quota, run_coding_plan_prompt
from siming.waits import add_wait, trigger_wait_now

STOPWORDS = {
    "请问",
    "一下",
    "多少",
    "多宽",
    "多高",
    "多深",
    "多大",
    "怎么",
    "什么",
    "哪个",
    "哪张",
    "几号",
    "时候",
    "今天",
    "目前",
    "现在",
    "留了",
    "用了",
    "帮我",
    "查查",
    "查下",
}


ROOM_PREFIXES = ("主卧", "次卧", "儿童房", "书房", "客厅", "餐厅", "厨房", "卫生间", "阳台", "玄关", "走廊")


def _extract_search_terms(query: str) -> list[str]:
    cleaned = re.sub(r"^(?:查一下|查下|查询|查|问下|帮我查)\s*", "", query.strip())
    # 先切掉句尾口语疑问词（如“留了多宽”、“是多少”）
    cleaned = re.sub(r"(?:留了)?(?:多宽|多高|多深|多大|多少|是什么|在哪|怎么摆|几号还款|几号出账)$", "", cleaned).strip()
    tokens: list[str] = []
    for tok in re.findall(r"[A-Za-z0-9_.-]{2,}|[\u4e00-\u9fff]{2,16}", cleaned):
        if tok in STOPWORDS:
            continue
        # 若以房间前缀开头（如“次卧窗帘盒”），拆成“次卧” + “窗帘盒”精确双词，不乱切双字碎片防误召回
        split_done = False
        for rp in ROOM_PREFIXES:
            if tok.startswith(rp) and len(tok) > len(rp) + 1:
                rest = tok[len(rp) :]
                tokens.extend([rp, rest])
                split_done = True
                break
        if not split_done:
            tokens.append(tok)
    return tokens[:6]


def ask_knowledge(
    db: SimingDB, query: str, use_llm_synthesis: bool = False
) -> dict[str, Any]:
    """只读检索全机私有事实（siming.db + ai-quota + herdr + 本地知识库），原样返回带文件:行号结果。"""
    db.init_db()
    q = query.strip()
    policy = load_policy(db.paths)
    structured_hits: list[str] = []

    # 0. 专项投影直达：到货初验 / 12.09 现场作战单 / 12.10 送装清单 / 保价监控 / 报销游标
    if re.search(r"(到货初验|外箱初验|验货清单|开箱初验)", q):
        from siming.install_plan import format_arrival_inspection_checklist

        txt = format_arrival_inspection_checklist(db)
        db.record_human_use("install_plan", "ask_arrival", ref_id="arrival")
        return {"ok": True, "found": True, "answer": txt, "citations": ["~/buy/track/items.json", "~/buy/track/data/auto.json"]}

    if re.search(r"(现场作战单|12\.09|12月9.*(?:安装|作战)|分组安装)", q):
        from siming.install_plan import format_onsite_grouped_checklist

        txt = format_onsite_grouped_checklist(db)
        db.record_human_use("install_plan", "ask_onsite", ref_id="onsite")
        return {"ok": True, "found": True, "answer": txt, "citations": ["~/buy/track/items.json"]}

    if re.search(r"(送装清单|送装核对|12\.10|12月10.*(?:送装|安装)|安装清单|家电预约)", q):
        from siming.install_plan import format_installation_checklist

        checklist = format_installation_checklist(db, compact=False)
        db.record_human_use("install_plan", "ask", ref_id="12.10")
        db.record_human_use("buy_track", "ask", ref_id="12.10")
        return {"ok": True, "found": True, "answer": checklist, "citations": ["~/buy/track/items.json", "~/buy/track/data/auto.json"]}

    if re.search(r"(保价监控|保价列表|京东保价|价保列表|哪些.*保价)", q):
        from siming.price_watch import format_price_watch_report

        pw_txt = format_price_watch_report(db)
        db.record_human_use("price_protect_watch", "ask", ref_id="price_watch")
        return {"ok": True, "found": True, "answer": pw_txt, "citations": ["~/.hermes/scripts/buy-price-watch/orders.json"]}

    if re.search(r"(报销游标|nous游标|下一批报销|未报销收据|报销进度)", q, re.IGNORECASE):
        from siming.ledger.reimburse_audit import format_reimbursement_cursor

        cur_txt = format_reimbursement_cursor(db)
        db.record_human_use("reimburse_audit", "ask", ref_id="cursor")
        return {"ok": True, "found": True, "answer": cur_txt, "citations": ["60-共享记忆/nous-reimbursement-batches.md"]}

    # 1. 配额查询
    if re.search(r"(ai-quota|quota|配额|额度|余量)", q, re.IGNORECASE):
        qt = fetch_ai_quota(db)
        if qt.get("ok"):
            structured_hits.append(
                f"[ai-quota 实时接口 127.0.0.1:18101] Codex 剩余: {qt.get('codex_label')} | Claude 状态: {'可用' if qt.get('claude_available') else '不可用'} ({qt.get('claude_label')})"
            )

    # 2. 时令 / 限行 / 补班查询
    if re.search(r"(限行|尾号|补班|放假|工作日)", q):
        di = day_info(now_local().date(), db.paths)
        structured_hits.append(f"[calendar_rules.json:1] 今日 {di.brief_header}")

    # 3. 银行卡 / 账单日 / 还款日查询
    if re.search(r"(卡|账单|还款|尾号|出账|消费|流水)", q):
        accs = load_accounts_config(db.paths)
        for idx, acc in enumerate(accs, start=1):
            if acc.get("configured"):
                role_s = f" · {acc['role']}" if acc.get("role") else ""
                s_day = int(acc.get("statement_day") or 0)
                d_day = int(acc.get("due_day") or 0)
                cycle_s = (
                    f"：账单日 {s_day} 日，还款日 {d_day} 日"
                    if (acc.get("kind") == "credit" and (s_day or d_day))
                    else ("：账单/还款日按电子账单自动识别" if acc.get("kind") == "credit" else "")
                )
                structured_hits.append(
                    f"[~/.config/siming/accounts.toml:#{idx}] {acc.get('alias')}（{acc.get('bank')} 尾号 {acc.get('last4')}{role_s}）{cycle_s}"
                )
        conn = db.connect()
        try:
            txns = conn.execute(
                "SELECT txn_id, occurred_at, amount_cents, currency, account_alias, kind, status, merchant FROM ledger_txns ORDER BY occurred_at DESC LIMIT 5"
            ).fetchall()
            sym_map = {"CNY": "¥", "USD": "$", "HKD": "HK$", "EUR": "€", "GBP": "£"}
            for t in txns:
                curr = str(t["currency"] or "CNY").upper()
                sym = sym_map.get(curr, f"{curr} ")
                structured_hits.append(
                    f"[siming.db:ledger_txns:{t['txn_id']}] {t['occurred_at'][:10]} {t['account_alias']} {t['kind']} {sym}{t['amount_cents']/100:.2f} {t['merchant']} ({t['status']})"
                )
        finally:
            conn.close()

    # 4. 活跃 herdr 终端会话查询
    if re.search(r"(herdr|终端|pane|跑什么|活跃会话)", q, re.IGNORECASE):
        herdr_bin = str(policy["coding_plan"].get("herdr_bin") or "/usr/bin/herdr")
        if Path(herdr_bin).is_file():
            proc = subprocess.run([herdr_bin, "tab", "list"], capture_output=True, text=True, timeout=3)
            if proc.returncode == 0:
                try:
                    tabs = (json.loads(proc.stdout).get("result") or {}).get("tabs") or []
                    active_labels = [
                        f"{t.get('tab_id')}「{t.get('label')}」({t.get('agent_status')})"
                        for t in tabs
                        if t.get("agent_status") in ("working", "idle")
                    ]
                    if active_labels:
                        structured_hits.append("[herdr tab list] " + "；".join(active_labels[:6]))
                except Exception:
                    pass

    # 5. 本机待办、挂起项与已接入邮箱（Omamail）检索
    if re.search(r"(发票|报销|水单|行程单)", q):
        rb = summarize_reimbursement_bundle(db)
        if rb.get("count", 0) > 0 or rb.get("itinerary_count", 0) > 0:
            files_desc = "、".join(
                f"{it['file']}(¥{it['amount_yuan']:.2f})" if it.get("amount_yuan") else it["file"]
                for it in rb["files"][:5]
            )
            pair_desc = (
                f"；打车成对 {len(rb.get('paired_rides') or [])} 组，缺行程单 {len(rb.get('missing_itinerary_invoices') or [])} 张，缺发票 {len(rb.get('missing_invoice_itineraries') or [])} 份"
                if (rb.get("paired_rides") or rb.get("missing_itinerary_invoices") or rb.get("missing_invoice_itineraries"))
                else ""
            )
            structured_hits.append(
                f"[reimburse/{rb['year_month']}] 当月已自动归集 {rb['count']} 张发票 PDF（合计 ¥{rb['total_yuan']:.2f}{pair_desc}）：{files_desc}"
            )
        try:
            from siming.ledger.reimburse_audit import get_reimbursement_cursor_status

            c_st = get_reimbursement_cursor_status(db)
            if c_st.get("next_portal_cursor"):
                structured_hits.append(
                    f"[报销游标] 最新已提 {c_st.get('latest_batch_id') or '-'}；下一批 Nous 门户起点 `{c_st['next_portal_cursor']}`"
                )
        except Exception:
            pass

    if re.search(r"^(?:查一下|查下|查询|查)?\s*(?:邮箱|邮件|收件箱|omamail)$", q, re.IGNORECASE):
        conn = db.connect()
        try:
            src_m = conn.execute("SELECT detail, last_ok_at FROM sources WHERE id='mail163'").fetchone()
            if src_m:
                structured_hits.append(f"[siming.db:sources:mail163] {src_m['detail']} (last_ok={src_m['last_ok_at']})")
            ev_recent = conn.execute(
                "SELECT event_id, summary, occurred_at, payload_json FROM events WHERE source='mail163' ORDER BY occurred_at DESC, ingested_at DESC LIMIT 6"
            ).fetchall()
            for ev in ev_recent:
                pl = json.loads(ev["payload_json"] or "{}")
                structured_hits.append(
                    f"[omamail:{pl.get('account', 'mail')}:{pl.get('message_id', ev['event_id'])}] {str(pl.get('date') or ev['occurred_at'])[:16]} {ev['summary']}"
                )
        finally:
            conn.close()

    terms = _extract_search_terms(q)
    conn = db.connect()
    try:
        for term in terms[:3]:
            c_rows = conn.execute(
                "SELECT case_id, title, status, next_step FROM cases WHERE title LIKE ? OR next_step LIKE ? LIMIT 4",
                (f"%{term}%", f"%{term}%"),
            ).fetchall()
            for r in c_rows:
                structured_hits.append(
                    f"[siming.db:cases:{r['case_id']}] ({r['status']}) {r['title']} — {r['next_step']}"
                )
            w_rows = conn.execute(
                "SELECT wait_id, status, trigger_kind, trigger_expr, quote FROM waits WHERE quote LIKE ? OR trigger_expr LIKE ? LIMIT 4",
                (f"%{term}%", f"%{term}%"),
            ).fetchall()
            for w in w_rows:
                structured_hits.append(
                    f"[siming.db:waits:{w['wait_id']}] ({w['status']} · {w['trigger_kind']}:{w['trigger_expr']}) {w['quote']}"
                )
            if term not in ("邮箱", "邮件", "omamail"):
                e_rows = conn.execute(
                    "SELECT event_id, summary, payload_json FROM events WHERE source='mail163' AND (summary LIKE ? OR payload_json LIKE ?) ORDER BY ingested_at DESC LIMIT 4",
                    (f"%{term}%", f"%{term}%"),
                ).fetchall()
                for ev in e_rows:
                    pl = json.loads(ev["payload_json"] or "{}")
                    pdf_note = f" | 已存PDF:{len(pl.get('staged_pdfs') or [])}份" if pl.get("staged_pdfs") else ""
                    structured_hits.append(
                        f"[omamail:{pl.get('account', 'mail')}:{pl.get('message_id', ev['event_id'])}] {ev['summary']} ({str(pl.get('date', ''))[:16]}){pdf_note}"
                    )
    finally:
        conn.close()

    # 6. 全机白名单知识目录 rg 行级检索（<2.5s）
    file_hits: list[str] = []
    roots = [r for r in (policy.get("knowledge_roots") or []) if Path(r).exists()]
    if roots and terms:
        regex_pat = "|".join(re.escape(t) for t in terms[:6])
        try:
            rg_proc = subprocess.run(
                [
                    "rg",
                    "-n",
                    "--no-heading",
                    "-i",
                    "--max-filesize",
                    "2M",
                    "--glob",
                    "*.{md,txt,json,toml,yaml,yml,html}",
                    "--glob",
                    "!.git/**",
                    "--glob",
                    "!node_modules/**",
                    "--glob",
                    "!**/v1/**",
                    "--glob",
                    "!**/deco/data/final-content.json",
                    "--glob",
                    "!**/price-*.json",
                    "--glob",
                    "!**/tb-api-*.json",
                    "--max-count",
                    "6",
                    regex_pat,
                    *roots,
                ],
                capture_output=True,
                text=True,
                timeout=2.8,
            )
            scored_lines: list[tuple[int, str]] = []
            for raw in (rg_proc.stdout or "").splitlines():
                parts = raw.split(":", 2)
                if len(parts) < 3:
                    continue
                fpath, lineno, content = parts[0], parts[1], parts[2].strip()
                if not content or len(content) > 260:
                    continue
                # 统计命中的不同关键词数排序，确保最精确的行排最前
                hit_cnt = sum(1 for t in terms if t.lower() in (fpath + " " + content).lower())
                home_str = str(Path.home())
                short_p = fpath.replace(f"{home_str}/Documents/Obsidian Vault/", "Vault/").replace(
                    f"{home_str}/Work/", ""
                ).replace(f"{home_str}/", "~/")
                scored_lines.append((hit_cnt, f"{short_p}:{lineno} — {content[:160]}"))

            scored_lines.sort(key=lambda x: -x[0])
            seen_h: set[str] = set()
            for _cnt, item in scored_lines:
                if item not in seen_h:
                    seen_h.add(item)
                    file_hits.append(item)
                    if len(file_hits) >= 8:
                        break
        except Exception:
            pass

    all_evidence = structured_hits + file_hits
    if not all_evidence:
        ans = "未记录（已检索 siming.db、knowledge_roots、Obsidian Vault 与 accounts.toml）。"
        return {"ok": True, "found": False, "answer": ans, "citations": []}

    db.record_human_use("knowledge_ask", "ask", ref_id=q[:40])
    if any("omamail:" in h or "mail163" in h for h in structured_hits):
        db.record_human_use("mail163", "ask_hit", ref_id=q[:40])
    if any("buy/" in h or "appliance" in h for h in all_evidence):
        db.record_human_use("buy_track", "ask_hit", ref_id=q[:40])

    if use_llm_synthesis:
        ev_block = "\n".join(f"- {line}" for line in all_evidence[:10])
        prompt = (
            "你是本机事实检索器。只准根据下方 `<evidence>` 原样回答用户问题，保留准确数字，每条结论后必须附上 `[文件:行号]` 出处。"
            "若证据不足以回答该具体问题，直接回复「未记录」，严禁猜测。\n\n"
            f"问题：{q}\n\n<evidence>\n{ev_block}\n</evidence>"
        )
        llm = run_coding_plan_prompt(prompt, db=db, read_only=True, timeout_sec=25, ref_id="ask")
        if llm.get("ok") and llm.get("output"):
            return {
                "ok": True,
                "found": True,
                "answer": llm["output"],
                "citations": all_evidence[:8],
            }

    header = f"查到 {len(all_evidence)} 条本机记录："
    body = "\n".join(f"• {line}" for line in all_evidence[:6])
    return {"ok": True, "found": True, "answer": f"{header}\n{body}", "citations": all_evidence[:6]}


def resolve_whitelisted_repo(repo_name_or_path: str, db: SimingDB) -> Optional[Path]:
    """严格仅允许 policy.yaml 中 whitelisted_repos 显式列出的仓库，禁止越界匹配任意目录。"""
    policy = load_policy(db.paths)
    wl: dict[str, str] = policy.get("whitelisted_repos") or {}
    key = repo_name_or_path.strip()
    if key in wl:
        p = Path(wl[key]).expanduser().resolve()
        return p if p.is_dir() else None
    return None


def default_done_check_for_repo(repo_dir: Path) -> str:
    if (repo_dir / "pixi.toml").is_file() and (repo_dir / "tests").is_dir():
        return "pixi run pytest"
    if (repo_dir / "pyproject.toml").is_file() and (repo_dir / "tests").is_dir():
        return "pytest -q"
    return "git diff --check"


def dispatch_repo_task(
    db: SimingDB,
    repo: str,
    task: str,
    done_check: str = "",
    runner: str = "",
    run_immediately: bool = True,
) -> dict[str, Any]:
    """远程或终端受控派单：在白名单仓库的独立 git worktree 跑完测试，发 diff 等 apply。"""
    repo_dir = resolve_whitelisted_repo(repo, db)
    if repo_dir is None:
        return {
            "ok": False,
            "error": f"仓库 `{repo}` 不在受控白名单或 ~/Work 下无此 Git 仓库。",
        }
    chk = done_check.strip() or default_done_check_for_repo(repo_dir)
    w = add_wait(
        db=db,
        when=f"time:{iso_now()}",
        mode="stage",
        strength="explicit",
        quote=f"派单 {repo}：{task}",
        task=task,
        cwd=str(repo_dir),
        runner=runner,
        done_check=chk,
        ttl="7d",
    )
    if run_immediately:
        trigger_wait_now(db, w["wait_id"], trigger_reason=f"远程派单 {repo}")
    return {
        "ok": True,
        "wait_id": w["wait_id"],
        "repo_dir": str(repo_dir),
        "done_check": chk,
    }


def find_latest_screenshot(pictures_dir: Optional[Path] = None) -> Optional[Path]:
    base = pictures_dir or (Path.home() / "Pictures")
    if not base.is_dir():
        return None
    exts = {".png", ".jpg", ".jpeg", ".webp"}
    candidates: list[tuple[float, Path]] = []
    for p in base.rglob("*"):
        if p.is_file() and p.suffix.lower() in exts:
            try:
                candidates.append((p.stat().st_mtime, p))
            except OSError:
                continue
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def _ocr_image_text(image_path: Path) -> str:
    try:
        proc = subprocess.run(
            ["tesseract", str(image_path), "stdout", "-l", "chi_sim+eng"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    except Exception:
        pass
    return ""


_IOS_STATUSBAR_NOISE_RE = re.compile(
    r"^(?:"
    r"\d{1,2}:\d{2}\d?"
    r"|[\d\s$￥%•·<>〉‹«»()（）.+-]+"
    r"|(?:5G|4G|LTE|Wi-?Fi|SOS|VPN|中国移动|中国联通|中国电信)[\d\s%•·]*"
    r"|[三令四五六七八九十$￥]+\s*\d{1,3}"
    r"|轻点背面|截图发送|艇田发迓|发给司命"
    r"|Q\s*商品名.*快递单号.*|全部订单|商城|团购|服务|待支付|待发货\d*|待收货.*|运送中\d*|包裹(?:工具)?|筛选|TE"
    r")$",
    re.IGNORECASE,
)


def _extract_meaningful_drop_title(merged_text: str) -> str:
    """跳过 iPhone 全屏截图顶部状态栏（时间/信号/电量图标乱码），提取真正可读的标题行。"""
    candidates: list[str] = []
    for raw_ln in merged_text.splitlines():
        ln = raw_ln.strip()
        if not ln:
            continue
        if ln.startswith("【备注】"):
            rem = ln.replace("【备注】", "", 1).strip()
            if rem:
                return rem[:64]
            continue
        if _IOS_STATUSBAR_NOISE_RE.match(ln):
            continue
        # 跳过纯 1 个字符的图标误识别（如 OCR 把图标识别成「し」「国」「山」）
        if len(ln) <= 1 or (len(ln) <= 2 and not re.search(r"[\u4e00-\u9fffA-Za-z0-9]{2}", ln)):
            continue
        candidates.append(ln)
        if len(candidates) >= 2:
            break
    if not candidates:
        first_non_empty = next((x.strip() for x in merged_text.splitlines() if x.strip()), "截图投递")
        return first_non_empty[:64]
    if len(candidates) == 1 or len(candidates[0]) >= 18:
        return candidates[0][:64]
    return f"{candidates[0]} · {candidates[1]}"[:64]


def drop_item(
    db: SimingDB,
    text: str = "",
    file_path: Optional[Path] = None,
    latest_shot: bool = False,
    from_paste: bool = False,
    source_channel: str = "desktop_drop",
    note: str = "",
    occurred_at: Optional[str] = None,
) -> dict[str, Any]:
    """统一投递入口（桌面截屏、剪贴板、QQ 转发、iOS 离线提字）：先拦验证码，再入库建档。"""
    db.init_db()
    target_file = file_path
    if latest_shot and target_file is None:
        target_file = find_latest_screenshot()
        if target_file is None:
            return {"ok": False, "error": "~/Pictures 下未找到截图文件。"}

    collected_parts: list[str] = []
    if note.strip():
        collected_parts.append(note.strip())
    if text.strip():
        collected_parts.append(text.strip())

    if from_paste:
        try:
            wl = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, text=True, timeout=3)
            if wl.returncode == 0 and wl.stdout.strip():
                collected_parts.append(wl.stdout.strip())
        except Exception:
            pass

    raw_bytes: Optional[bytes] = None
    if target_file is not None:
        if not target_file.is_file():
            return {"ok": False, "error": f"文件不存在: {target_file}"}
        file_bytes = target_file.read_bytes()
        if target_file.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}:
            ocr_txt = _ocr_image_text(target_file)
            if ocr_txt:
                collected_parts.append(ocr_txt)
                raw_bytes = file_bytes
            else:
                if not collected_parts:
                    return {
                        "ok": False,
                        "error": f"图片 `{target_file.name}` 未识别出可读文字且无备注，为防未经验证码校验的原图落盘已拒收（可附备注说明重试）。",
                    }
                # OCR 为空时仅保存人工备注文本，不盲存未经 OTP 校验的原始图像字节
                raw_bytes = None
                collected_parts.append(f"[图片投递(未存原图字节): {target_file.name}]")
        else:
            raw_bytes = file_bytes
            collected_parts.append(file_bytes.decode("utf-8", errors="replace")[:4000])

    merged_text = "\n".join(collected_parts).strip()
    if not merged_text:
        return {"ok": False, "error": "投递内容为空。"}

    # 铁律：含登录/交易验证码当场拒收，零字节入库
    if contains_otp(merged_text):
        db.write_audit(
            "A0",
            "drop_blocked_otp",
            {"source": source_channel},
            exit_code=0,
            detail="检测到验证码，已本地拦截拒收",
        )
        return {"ok": False, "blocked_otp": True, "error": "检测到验证码内容，已按安全红线拦截，未写入数据库。"}

    raw_ref = store_encrypted_raw(raw_bytes if raw_bytes is not None else merged_text, db.paths)
    fp = f"drop:{hashlib.sha256((source_channel + ':' + merged_text).encode('utf-8')).hexdigest()[:28]}"

    # 若是快递取件码，交给取件码归集器排入 18:00 窗口
    pickup = try_ingest_pickup_code(db, merged_text, source=source_channel, raw_ref=raw_ref)
    if pickup.get("matched"):
        return {
            "ok": True,
            "kind": "pickup_code",
            "case_id": pickup["case_id"],
            "summary": pickup["summary"],
        }

    # DeepSeek Super-agent 多级语义分拣（F01/F03/F09）：收藏夹 / 抖音在途包裹冷链 / 送装改约重排 / 明确约局定时提醒 / 误触静默忽略
    from siming.modules import is_module_enabled

    ref_dt = now_local()
    if occurred_at:
        try:
            ref_dt = parse_iso(occurred_at)
        except Exception:
            ref_dt = now_local()

    if is_module_enabled(db, "cognitive_drop"):
        from siming.superagent_triage import triage_inbound_drop

        tri = triage_inbound_drop(
            db,
            merged_text,
            note=note,
            source_channel=source_channel,
            ref_dt=ref_dt,
        )
        t_intent = tri.get("intent")
        if t_intent == "IGNORE":
            return {"ok": True, "ignored": True, "kind": "ignored_noise", "summary": "已静默忽略无实质内容的屏幕/设置页误触。"}
        if t_intent == "TESLA":
            return {
                "ok": True,
                "kind": "tesla_state",
                "summary": f"🚗 已从 Tesla 截图自动更新 Model Y 标续状态：电量 {tri.get('soc_pct')}% · 表显约 {tri.get('range_km')} km。",
            }
        if t_intent == "BOOKMARK":
            return {
                "ok": True,
                "kind": "bookmark",
                "bookmark_id": tri.get("bookmark_id"),
                "summary": f"📚 已收入灵感收藏夹 `[{tri.get('bookmark_id')}] {tri.get('title')}`（不占待办列表，发 `收藏夹` 可查）。",
            }
        if t_intent == "PARCEL":
            c_ids = ", ".join(tri.get("in_transit_created") or []) or "已去重同步"
            cold_s = f"（⚠️ 含冷链急件：{len(tri.get('cold_chain_items') or [])} 单）" if tri.get("cold_chain_items") else ""
            return {
                "ok": True,
                "kind": "parcel",
                "case_ids": tri.get("in_transit_created"),
                "summary": f"📦 已解析订单截图共 {tri.get('total_parsed')} 笔，在途建档：{c_ids}{cold_s}。",
            }
        if t_intent == "RESCHEDULE":
            from siming.event_chain import parse_and_apply_reschedule_signal

            r_res = parse_and_apply_reschedule_signal(db, merged_text, source=source_channel, notify=True)
            if r_res.get("matched"):
                return {"ok": True, "kind": "reschedule", "summary": r_res["replan"].get("card_text") or "已触发事件链重排"}

        if t_intent == "COMMITMENT":
            from siming.cn_calendar import WEEKDAY_CN
            from siming.event_chain import upsert_expectation
            from siming.outing_chain import (
                bind_commitment_destination,
                register_commitment_destination_prompt,
            )
            from siming.push import push_message
            from siming.waits import add_wait

            cm_title = redact_sensitive(str(tri.get("title") or "约定行程"), db.paths)
            event_at = str(tri["event_at"])
            remind_at = str(tri["remind_at"])
            lead_min = int(tri.get("lead_min") or 0)
            evidence = redact_sensitive(str(tri.get("evidence") or ""), db.paths)
            place = redact_sensitive(str(tri.get("place") or ""), db.paths)
            needs_destination = not bool(place)
            ev_dt = parse_iso(event_at)
            rm_dt = parse_iso(remind_at)
            wd_cn = WEEKDAY_CN[ev_dt.weekday()]

            with db.transaction() as conn:
                ev_id = db.append_event(
                    conn,
                    source=source_channel,
                    thread_key=f"commitment:{hashlib.sha1((cm_title + event_at[:16]).encode('utf-8')).hexdigest()[:10]}",
                    summary=f"约定入档：{cm_title}（{ev_dt.strftime('%m-%d %H:%M')}）",
                    payload={
                        "text": redact_sensitive(merged_text[:1000], db.paths),
                        "commitment": {
                            "title": cm_title,
                            "event_at": event_at,
                            "remind_at": remind_at,
                            "lead_min": lead_min,
                            "evidence": evidence,
                            "place": place,
                        },
                    },
                    raw_ref=raw_ref,
                    occurred_at=iso_now(ref_dt),
                    fingerprint=fp,
                )
            if ev_id is None:
                return {"ok": True, "duplicate": True, "kind": "commitment", "summary": f"重复约定截屏已幂等忽略：{cm_title}"}

            # 线上延迟同步若该约定已彻底过去（非离线测试回放），仅留 events 归档，不发迟到闹钟
            if (
                occurred_at
                and os.environ.get("SIMING_TEST_OFFLINE") != "1"
                and ev_dt < now_local() - timedelta(minutes=5)
            ):
                return {
                    "ok": True,
                    "kind": "commitment_past",
                    "event_id": ev_id,
                    "title": cm_title,
                    "event_at": event_at,
                    "summary": f"📅 已补档过往约定：{cm_title}（{ev_dt.strftime('%m-%d %H:%M')}，已过时未建新闹钟）。",
                }

            # 同时间同标题防重（如轻点两次截了略有滚动差异的同一段聊天）
            existing_wid = ""
            conn_dup = db.connect()
            try:
                dup_w = conn_dup.execute(
                    """
                    SELECT wait_id FROM waits
                    WHERE status='WAITING' AND trigger_kind='time' AND next_fire_at=? AND quote LIKE ?
                    ORDER BY created_at DESC LIMIT 1
                    """,
                    (remind_at, f"%{cm_title[:16]}%"),
                ).fetchone()
                if dup_w:
                    existing_wid = str(dup_w["wait_id"])
            finally:
                conn_dup.close()

            if existing_wid:
                return {
                    "ok": True,
                    "duplicate": True,
                    "kind": "commitment",
                    "wait_id": existing_wid,
                    "event_id": ev_id,
                    "event_at": event_at,
                    "remind_at": remind_at,
                    "title": cm_title,
                    "summary": f"📅 约定「{cm_title}（{ev_dt.strftime('%m-%d %H:%M')}）」已有提醒 [{existing_wid}]，已自动去重。",
                }

            lead_desc = f"提前 {lead_min} 分钟提醒" if lead_min > 0 else "到点提醒"
            wait_quote = f"行程提醒：{cm_title}（{ev_dt.strftime('%m-%d')} {wd_cn} {ev_dt.strftime('%H:%M')}）"
            wait_ctx = f"{lead_desc}；依据：{evidence}" if evidence else lead_desc
            if needs_destination:
                wait_ctx += "；未填地点，回我地点后可按实时路况动态校准动身时刻"
            w = add_wait(
                db,
                when=f"time:{remind_at}",
                mode="remind",
                strength="explicit",
                quote=wait_quote,
                context=wait_ctx,
                agent=source_channel,
            )
            wid = str(w["wait_id"])
            exp_id = upsert_expectation(
                db,
                kind="outing",
                chain_type="obligation",
                title=f"{cm_title}（{ev_dt.strftime('%m-%d %H:%M')}）",
                target_at=event_at,
                place=place,
                visibility="high",
                source_ref=wid,
                counterparty={
                    "title": cm_title,
                    "evidence": evidence,
                    "wait_id": wid,
                    "event_id": ev_id,
                    "needs_destination": needs_destination,
                    "no_travel": False,
                },
                ttl_days=2,
            )
            db.record_human_use("drop", "commitment_drop", ref_id=wid)

            dest_bind_res: Optional[dict[str, Any]] = None
            qid = ""
            if needs_destination:
                qid = (
                    register_commitment_destination_prompt(
                        db,
                        wait_id=wid,
                        exp_id=exp_id,
                        title=cm_title,
                        event_at=event_at,
                    )
                    or ""
                )
            else:
                dest_bind_res = bind_commitment_destination(
                    db,
                    place,
                    wait_id=wid,
                    now=ref_dt,
                )
                if dest_bind_res.get("ok") and dest_bind_res.get("prep_at"):
                    remind_at = str(dest_bind_res["prep_at"])
                    rm_dt = parse_iso(remind_at)

            receipt_lines = [
                f"📅 已从截图识别约定并设好{'保底' if needs_destination else ''}提醒 [{wid}]",
                f"• 事项：{cm_title}（{ev_dt.strftime('%m-%d')} {wd_cn} {ev_dt.strftime('%H:%M')}）",
            ]
            if not needs_destination and dest_bind_res and dest_bind_res.get("ok"):
                receipt_lines.append(
                    f"• 📍 地点：**{dest_bind_res['dest_label']}**（参考车程约 {dest_bind_res['duration_min']} 分钟）"
                )
                receipt_lines.append(
                    f"• ⏰ 准备提醒：{rm_dt.strftime('%m-%d %H:%M')}（临出发前 2.5 小时将按届时实时路况复算校准并联动备车）"
                )
            else:
                receipt_lines.append(
                    f"• ⏰ 保底提醒：{rm_dt.strftime('%m-%d %H:%M')}（{lead_desc}）"
                )
            if evidence:
                receipt_lines.append(f"• 依据：{evidence}")
            if needs_destination:
                receipt_lines.append(
                    "❓ 截屏里没写地点——**这次去哪里？**\n"
                    "直接回「`去 <地点>`」/「`在 <地点>`」或引用本条回地点名，我会在临出行前查实时路况车程，在最该准备出门的时候提醒你！"
                )
            receipt = "\n".join(receipt_lines)

            if source_channel == "ios_drop":
                push_message(
                    db,
                    text=receipt,
                    rule_id="commitment_drop",
                    ref_id=wid,
                    level="normal",
                    sub_meta={
                        "kind": "commitment_drop",
                        "wait_id": wid,
                        "exp_id": exp_id,
                        "event_at": event_at,
                        "needs_destination": needs_destination,
                        "qid": qid,
                    },
                )

            return {
                "ok": True,
                "kind": "commitment",
                "wait_id": wid,
                "exp_id": exp_id,
                "event_id": ev_id,
                "event_at": event_at,
                "remind_at": remind_at,
                "title": cm_title,
                "place": (dest_bind_res or {}).get("dest_label") or place,
                "needs_destination": needs_destination,
                "qid": qid,
                "summary": receipt,
            }

        non_empty_drop_lines = [ln for ln in merged_text.splitlines() if ln.strip()]
        if len(non_empty_drop_lines) <= 2 and len(merged_text) <= 100:
            from siming.tesla_link import sync_mobile_outing_plan

            out_res = sync_mobile_outing_plan(db, merged_text, source=source_channel, now=ref_dt)
            if out_res.get("matched"):
                return {"ok": True, "kind": "outing_plan", "summary": out_res["reply"]}

    title = redact_sensitive(_extract_meaningful_drop_title(merged_text), db.paths)
    conflicts = detect_hard_rule_conflicts(merged_text, db=db)
    ts = iso_now()

    with db.transaction() as conn:
        ev_id = db.append_event(
            conn,
            source=source_channel,
            thread_key=f"drop:{hashlib.sha1(title.encode('utf-8')).hexdigest()[:10]}",
            summary=f"投递入档：{title}",
            payload={"text": redact_sensitive(merged_text[:1000], db.paths), "conflicts": conflicts},
            raw_ref=raw_ref,
            fingerprint=fp,
        )
        if ev_id is None:
            return {"ok": True, "duplicate": True, "summary": f"重复投递已幂等忽略：{title}"}

        case_id = db.next_seq_id(conn, "S")
        next_step = "待核对处理"
        if conflicts:
            next_step += "（注意：" + "；".join(c["message"] for c in conflicts) + "）"
        conn.execute(
            """
            INSERT INTO cases (
                case_id, title, next_step, status, strength, category, rule_id,
                evidence_ids, context_json, created_at, updated_at
            ) VALUES (?, ?, ?, '待处理', 'SOFT', 'drop', 'user_drop', ?, ?, ?, ?)
            """,
            (
                case_id,
                title,
                next_step[:300],
                json.dumps([ev_id]),
                json.dumps({"snippet": redact_sensitive(merged_text[:400], db.paths)}, ensure_ascii=False),
                ts,
                ts,
            ),
        )
        if source_channel in ("desktop_drop", "qq_drop", "ios_drop"):
            db.record_human_use("drop", "drop_item", ref_id=case_id, conn=conn)

    return {
        "ok": True,
        "kind": "case",
        "case_id": case_id,
        "event_id": ev_id,
        "conflicts": conflicts,
        "summary": f"[{case_id}] 已建档：{title}",
    }
