"""W4-1 · 跨应用上下文图谱（Linger 风格只读联邦检索图谱）。

统一索引 7 大应用域的只读信息源：
- files: 本地 Markdown / 文本笔记（Obsidian 共享记忆或指定文档根目录）
- mail: 163 / Gmail / Omamail 邮件事件
- calendar: waits 挂起提醒 + expectations 时间线 + trips 行程
- chat: cases 待办与非邮件类事件（QQ / 短信 / 会话摘录）
- wiki: Hindsight Wiki 复盘维基页面
- sure: ledger_txns 账本流水 + sure_sync_map 同步映射
- msim: M-SIM 进化实验记录

通过共享日期、目的地、商户、金额、卡尾号与标签构建跨应用图边（context_graph_edges），
支持中文/英文多跳复杂查询并输出精确溯源引用（app / provenance / occurred_at / snippet / linked_nodes）。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from siming import db as _db
from siming.crypto import redact_sensitive
from siming.db import SimingDB, iso_now
from siming.modules import is_module_enabled
from siming.paths import SimingPaths

_GRAPH_EXTRA_SECRET_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"), "[REDACTED-KEY]"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{6,}\b"), "[REDACTED-JWT]"),
    (
        re.compile(
            r"((?:支付密码|交易密码|取款密码|银行卡密码|密保|卡密|PIN码?|payment\s*password)[\s:：为是=]*)\d{4,8}\b",
            re.IGNORECASE,
        ),
        r"\1[REDACTED-PWD]",
    ),
    (re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "[REDACTED-PHONE]"),
    (
        re.compile(r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----"),
        "[REDACTED-PRIVATE-KEY]",
    ),
)


def _sanitize_graph_text(text: str, paths: Optional[SimingPaths] = None) -> str:
    """严防 API Key / Bearer / JWT / OTP / 支付密码 / 手机号 / 本地 secrets 落入图谱节点或边。"""
    if not text:
        return ""
    out = redact_sensitive(text, paths=paths)
    for pat, repl in _GRAPH_EXTRA_SECRET_PATTERNS:
        out = pat.sub(repl, out)
    return out

VALID_APPS = ("files", "mail", "calendar", "chat", "wiki", "sure", "msim", "learned")

_DATE_RE = re.compile(r"\b(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})日?\b")
_YEAR_MONTH_RE = re.compile(r"\b(20\d{2})[-/.年](\d{1,2})月?\b")
_AMOUNT_RE = re.compile(
    r"(?:¥|￥|RMB|CNY|USD|\$|JPY|日元)\s*([0-9]{2,7}(?:\.[0-9]{1,2})?)|([0-9]{2,7}(?:\.[0-9]{1,2})?)\s*(?:元|块|日元|美元)"
)
_CARD_LAST4_RE = re.compile(r"(?:尾号|卡号|卡|last4|card)[\s:：#]*([0-9]{4})\b", re.IGNORECASE)
_ASCII_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{1,31}")
_CJK_RUN_RE = re.compile(r"[\u4e00-\u9fff]{2,}")

_KNOWN_DESTINATIONS: tuple[str, ...] = (
    "东京", "大阪", "京都", "冲绳", "北海道", "福冈", "名古屋", "日本",
    "新加坡", "首尔", "曼谷", "吉隆坡", "香港", "澳门", "台北",
    "伦敦", "巴黎", "纽约", "旧金山", "洛杉矶", "悉尼", "墨尔本",
    "北京", "上海", "深圳", "广州", "杭州", "成都", "重庆", "南京", "武汉", "西安", "苏州", "厦门", "三亚",
    "tokyo", "osaka", "kyoto", "singapore", "seoul", "bangkok", "london", "paris", "shanghai", "beijing",
)

_KNOWN_MERCHANTS: tuple[str, ...] = (
    "携程", "ctrip", "去哪儿", "飞猪", "12306", "国航", "东航", "南航", "全日空", "ana", "jal",
    "东横", "toyoko", "希尔顿", "hilton", "万豪", "marriott", "凯悦", "hyatt", "亚朵", "全季", "如家", "汉庭",
    "格拉斯丽", "gracery", "新宿", "银座", "涩谷", "羽田", "成田",
    "grab", "suica", "西瓜卡", "滴滴", "美团", "瑞幸", "山姆", "京东", "淘宝", "盒马",
    "中行", "建行", "光大", "招行", "工行", "农行", "交行",
    "cw01", "cw08", "报销", "机票", "酒店", "高铁", "火车", "打车", "外卖", "桌游", "新物集", "特斯拉", "tesla",
    "deepseek", "claude", "codex", "nous", "redpill", "bambu", "拓竹", "sure",
)

_KNOWN_TOPICS: tuple[str, ...] = (
    "siming", "司命", "sqlite", "wal", "worktree", "herdr", "claude", "codex", "deepseek", "kimi",
    "nous", "redpill", "gemini", "linger", "hindsight", "msim", "m-sim", "wiki", "sure", "pipelock",
    "cw08", "cw01", "报销", "美团", "瑞幸", "滴滴", "特斯拉", "tesla", "丰巢", "菜鸟", "快递", "取件",
    "周期用药", "常用医院", "打针", "示例小区B", "甲醛", "新风", "haos", "拓竹", "bambu", "nas",
    "rb5009", "sing-box", "atrust", "omarchy", "桌游", "boardgame_kb", "新物集", "独游", "抖音",
    "小说", "icloud", "apple", "迅雷", "game pass", "光大", "中行", "建行", "招行", "1001", "1003",
    "1004", "示例小区A", "夜猫子", "睡眠", "加班", "装修", "送装", "家电", "12.10", "出境", "差旅",
    "pixi", "pytest", "frozen_clock", "agent-tmp",
)

_CN_STOPWORDS = frozenset({
    "的", "了", "吗", "呢", "啊", "吧", "啊", "是", "在", "有", "没有", "哪", "哪张", "哪家", "哪个",
    "什么", "怎么", "多少", "一下", "帮我", "请问", "查一下", "查询", "看看", "是否", "已经", "之前",
    "那个", "这个", "刷", "订", "买", "花", "给", "和", "跟", "与", "把", "被", "用", "去", "来",
    "去年", "今年", "明年", "上个月", "这个月", "本月", "最近",
})

_DOMAIN_TERMS: tuple[str, ...] = (
    "机票", "航班", "酒店", "住宿", "报销", "发票", "行程单", "信用卡", "刷卡", "尾号", "卡",
    "高铁", "火车", "打车", "外卖", "咖啡", "保价", "快递", "取件", "桌游", "众筹", "尾款",
    "装修", "送装", "家电", "空调", "新风", "甲醛", "充电", "特斯拉", "电费", "水费", "燃气",
    "订阅", "算力", "余额", "账单", "退款", "回款", "实验", "论文", "复盘", "维基",
    *_KNOWN_DESTINATIONS,
    *_KNOWN_MERCHANTS,
)


def _norm_id(prefix: str, raw: str) -> str:
    h = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]
    return f"cg:{prefix}:{h}"


def _extract_entities(text: str, extra_tags: Optional[list[str]] = None, extra_destinations: Optional[set[str]] = None) -> dict[str, set[str]]:
    """从节点文本抽取跨应用关联实体（日期、年月、目的地、商户、金额、卡尾号、标签）。"""
    src = text or ""
    low = src.lower()
    entities: dict[str, set[str]] = defaultdict(set)

    for m in _DATE_RE.finditer(src):
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= d <= 31:
            entities["date"].add(f"{y:04d}-{mo:02d}-{d:02d}")
            entities["month"].add(f"{y:04d}-{mo:02d}")

    for m in _YEAR_MONTH_RE.finditer(src):
        y, mo = int(m.group(1)), int(m.group(2))
        if 1 <= mo <= 12:
            entities["month"].add(f"{y:04d}-{mo:02d}")

    dest_pool = set(_KNOWN_DESTINATIONS)
    if extra_destinations:
        dest_pool.update(d.strip().lower() for d in extra_destinations if d and d.strip())
    for dest in dest_pool:
        if dest in low:
            entities["destination"].add(dest)

    for merch in _KNOWN_MERCHANTS:
        if merch in low:
            entities["merchant"].add(merch)

    for topic in _KNOWN_TOPICS:
        if topic in low:
            entities["topic"].add(topic)

    for m in _AMOUNT_RE.finditer(src):
        raw_num = m.group(1) or m.group(2)
        if not raw_num:
            continue
        try:
            val = float(raw_num)
        except ValueError:
            continue
        if val >= 10.0:
            entities["amount"].add(f"{int(round(val))}")

    for m in _CARD_LAST4_RE.finditer(src):
        entities["card"].add(m.group(1))

    for tag in (extra_tags or []):
        t = str(tag or "").strip().lower()
        if len(t) >= 2:
            entities["tag"].add(t)

    return entities


def _safe_json_loads(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw) if raw else default
    except (ValueError, TypeError):
        return default


def _scan_file_nodes(file_roots: list[Path], max_files: int) -> list[dict[str, Any]]:
    nodes: list[dict[str, Any]] = []
    exts = {".md", ".markdown", ".txt"}
    count = 0
    for root in file_roots:
        if not root.exists() or not root.is_dir():
            continue
        for p in sorted(root.rglob("*")):
            if count >= max_files:
                break
            if not p.is_file() or p.suffix.lower() not in exts:
                continue
            # 严禁读取敏感付款指令或隐藏目录
            if p.name.lower() == "payment_guide.md" or any(part.startswith(".") for part in p.relative_to(root).parts):
                continue
            try:
                content = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if not content.strip():
                continue
            count += 1
            mtime_iso = iso_now(datetime.fromtimestamp(p.stat().st_mtime).astimezone())
            first_heading = ""
            for ln in content.splitlines()[:10]:
                s = ln.strip()
                if s.startswith("#"):
                    first_heading = s.lstrip("#").strip()
                    break
            title = first_heading or p.stem
            tags = [t.lstrip("#") for t in re.findall(r"(?<!\w)#([A-Za-z0-9_\u4e00-\u9fff-]{2,24})", content)][:10]
            nodes.append(
                {
                    "node_id": _norm_id("file", str(p.resolve())),
                    "app": "files",
                    "entity_type": "note",
                    "title": title[:160],
                    "body": content[:4000],
                    "provenance": f"file://{p.resolve()}",
                    "occurred_at": mtime_iso,
                    "tags": tags,
                }
            )
    return nodes


def index_context_graph(
    db: SimingDB,
    file_roots: Optional[list[str | Path]] = None,
    max_files: int = 200,
) -> dict[str, Any]:
    """扫描 7 大应用域（DB 表 + 可选本地文档目录），构建 context_graph_nodes 与跨应用 context_graph_edges。"""
    db.init_db()
    ts = iso_now()

    if file_roots is None:
        roots = [db.paths.shared_memory_dir] if db.paths.shared_memory_dir.is_dir() else []
    else:
        roots = [Path(r) for r in file_roots if str(r).strip()]

    nodes: list[dict[str, Any]] = []
    nodes.extend(_scan_file_nodes(roots, max_files=max(0, int(max_files))))

    extra_destinations: set[str] = set()
    explicit_links: list[tuple[str, str, str, float]] = []

    conn = db.connect()
    try:
        # 1. trips -> calendar 域（同时收集已知目的地用于实体链接）
        for r in conn.execute("SELECT * FROM trips").fetchall():
            dest = str(r["destination"] or "").strip()
            if dest:
                extra_destinations.add(dest)
            bookings = _safe_json_loads(r["bookings_json"], [])
            body_parts = [
                f"行程 {dest} ({r['country']}) {r['start_date']} ~ {r['end_date']} [{r['kind']}/{r['status']}]",
                f"Sure标签: {r['sure_tag']}" if r["sure_tag"] else "",
                f"CW01草稿: {r['cw01_draft_path']}" if r["cw01_draft_path"] else "",
                json.dumps(bookings, ensure_ascii=False) if bookings else "",
            ]
            tags = [t for t in (dest, r["country"], r["kind"], r["sure_tag"]) if t]
            nodes.append(
                {
                    "node_id": f"cg:trip:{r['trip_id']}",
                    "app": "calendar",
                    "entity_type": "trip",
                    "title": f"行程: {dest} ({r['start_date']}~{r['end_date']})",
                    "body": "\n".join(p for p in body_parts if p),
                    "provenance": f"trips:{r['trip_id']}",
                    "occurred_at": str(r["start_date"] or r["created_at"] or ""),
                    "tags": tags,
                }
            )

        # 2. events -> mail 或 chat
        mail_sources = {"mail163", "omamail", "gmail", "mail", "email", "mail_imap"}
        event_node_ids: dict[str, str] = {}
        for r in conn.execute("SELECT * FROM events ORDER BY occurred_at DESC LIMIT 1000").fetchall():
            src = str(r["source"] or "").lower()
            app = "mail" if (src in mail_sources or "mail" in src) else "chat"
            payload = _safe_json_loads(r["payload_json"], {})
            summary = str(r["summary"] or "")
            subject = str(payload.get("subject") or payload.get("title") or summary[:80])
            payload_text = json.dumps(payload, ensure_ascii=False) if payload else ""
            nid = f"cg:{app}:{r['event_id']}"
            event_node_ids[str(r["event_id"])] = nid
            nodes.append(
                {
                    "node_id": nid,
                    "app": app,
                    "entity_type": "email" if app == "mail" else "event",
                    "title": subject[:160],
                    "body": f"{summary}\n{payload_text[:2000]}".strip(),
                    "provenance": f"events:{r['source']}:{r['event_id']}",
                    "occurred_at": str(r["occurred_at"] or ""),
                    "tags": [src, str(r["thread_key"] or "")],
                }
            )

        # 3. waits & expectations -> calendar
        for r in conn.execute("SELECT * FROM waits ORDER BY created_at DESC LIMIT 500").fetchall():
            mandate = _safe_json_loads(r["mandate_json"], {})
            task = str(mandate.get("task") or r["quote"] or r["trigger_expr"])
            nodes.append(
                {
                    "node_id": f"cg:wait:{r['wait_id']}",
                    "app": "calendar",
                    "entity_type": "wait",
                    "title": f"挂起提醒 {r['wait_id']}: {task[:100]}",
                    "body": f"触发={r['trigger_kind']}:{r['trigger_expr']} 状态={r['status']} 原话={r['quote']} 结果={r['result_summary']}",
                    "provenance": f"waits:{r['wait_id']}",
                    "occurred_at": str(r["next_fire_at"] or r["created_at"] or ""),
                    "tags": [str(r["trigger_kind"] or ""), str(r["status"] or "")],
                }
            )

        for r in conn.execute("SELECT * FROM expectations ORDER BY target_at DESC LIMIT 500").fetchall():
            nodes.append(
                {
                    "node_id": f"cg:exp:{r['exp_id']}",
                    "app": "calendar",
                    "entity_type": "expectation",
                    "title": str(r["title"] or r["exp_id"])[:160],
                    "body": f"类型={r['kind']}/{r['chain_type']} 地点={r['place']} 状态={r['status']} 来源={r['source_ref']}",
                    "provenance": f"expectations:{r['exp_id']}",
                    "occurred_at": str(r["target_at"] or r["created_at"] or ""),
                    "tags": [t for t in (r["kind"], r["place"], r["status"]) if t],
                }
            )

        # 4. cases -> chat
        for r in conn.execute("SELECT * FROM cases ORDER BY updated_at DESC LIMIT 500").fetchall():
            ctx = _safe_json_loads(r["context_json"], {})
            nodes.append(
                {
                    "node_id": f"cg:case:{r['case_id']}",
                    "app": "chat",
                    "entity_type": "case",
                    "title": f"[{r['status']}] {r['title']}"[:160],
                    "body": f"下一步={r['next_step']} 分类={r['category']} 截止={r['due_at'] or ''} 上下文={json.dumps(ctx, ensure_ascii=False)[:1200]}",
                    "provenance": f"cases:{r['case_id']}",
                    "occurred_at": str(r["due_at"] or r["created_at"] or ""),
                    "tags": [t for t in (r["category"], r["status"], r["rule_id"]) if t],
                }
            )

        # 5. wiki_pages -> wiki
        wiki_provenance_links: list[tuple[str, str]] = []
        for r in conn.execute("SELECT * FROM wiki_pages ORDER BY updated_at DESC LIMIT 500").fetchall():
            sources = _safe_json_loads(r["sources_json"], [])
            nid = f"cg:wiki:{r['slug']}"
            wiki_tags: list[str] = [str(r["category"] or "wiki")]
            for s in sources[:30]:
                if isinstance(s, dict):
                    tp = str(s.get("topic") or "").strip()
                    src_ref = str(s.get("source") or "").strip()
                    if tp:
                        wiki_tags.append(tp)
                    if src_ref:
                        wiki_provenance_links.append((nid, src_ref))
                elif s:
                    wiki_tags.append(str(s))
            nodes.append(
                {
                    "node_id": nid,
                    "app": "wiki",
                    "entity_type": "wiki_page",
                    "title": str(r["title"] or r["slug"])[:160],
                    "body": str(r["content_md"] or "")[:4000],
                    "provenance": f"wiki_pages:{r['slug']}",
                    "occurred_at": str(r["updated_at"] or ""),
                    "tags": list(dict.fromkeys(wiki_tags))[:24],
                }
            )

        # 6. ledger_txns + sure_sync_map -> sure
        sure_map: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for sr in conn.execute("SELECT * FROM sure_sync_map").fetchall():
            lid = str(sr["local_id"] or "")
            sure_map[lid].append(dict(sr))
            if ":" in lid:
                sure_map[lid.split(":")[-1]].append(dict(sr))

        txn_node_ids: dict[str, str] = {}
        for r in conn.execute("SELECT * FROM ledger_txns ORDER BY occurred_at DESC LIMIT 1500").fetchall():
            tid = str(r["txn_id"])
            nid = f"cg:sure:{tid}"
            txn_node_ids[tid] = nid
            yuan = float(r["amount_cents"] or 0) / 100.0
            sync_rows = sure_map.get(tid, [])
            sync_tags = [str(s.get("sure_id") or "") for s in sync_rows if s.get("sure_id")]
            sure_acct = next((str(s.get("sure_account") or "") for s in sync_rows if s.get("sure_account")), "")
            card_str = f"尾号{r['card_last4']}" if r["card_last4"] else ""
            body = (
                f"商户={r['merchant']} 金额={r['currency']} {yuan:.2f}元 账户={r['account_alias']} {card_str} "
                f"类型={r['kind']} 状态={r['status']} Sure账户={sure_acct} 标签={','.join(sync_tags)} 备注={r['notes']}"
            ).strip()
            tags = [
                t
                for t in (
                    r["account_alias"],
                    r["card_last4"],
                    r["currency"],
                    r["kind"],
                    r["status"],
                    *sync_tags,
                )
                if t
            ]
            nodes.append(
                {
                    "node_id": nid,
                    "app": "sure",
                    "entity_type": "ledger_txn",
                    "title": f"{r['merchant'] or r['account_alias']} {r['currency']} {yuan:.2f} ({r['account_alias']}{(' ' + card_str) if card_str else ''})"[:160],
                    "body": body,
                    "provenance": f"ledger_txns:{tid}",
                    "occurred_at": str(r["occurred_at"] or ""),
                    "tags": tags,
                }
            )

        # 从 links 表获取显式账本<->事件关联
        for lr in conn.execute("SELECT txn_id, event_id, role FROM links").fetchall():
            tn = txn_node_ids.get(str(lr["txn_id"]))
            en = event_node_ids.get(str(lr["event_id"]))
            if tn and en:
                explicit_links.append((tn, en, f"linked:{lr['role']}", 2.0))

        # 7. msim_entries -> msim
        for r in conn.execute("SELECT * FROM msim_entries ORDER BY created_at DESC LIMIT 500").fetchall():
            tags = _safe_json_loads(r["tags_json"], [])
            nodes.append(
                {
                    "node_id": f"cg:msim:{r['entry_id']}",
                    "app": "msim",
                    "entity_type": "msim_entry",
                    "title": f"[{r['problem_family']}] {r['source_model']} ({r['baseline_score']:.2f}->{r['measured_score']:.2f})"[:160],
                    "body": f"目标={r['target_artifact']} 改动={r['change_rationale']} 预期={r['expected_outcome']} 环境={r['eval_context']}",
                    "provenance": f"msim_entries:{r['entry_id']}",
                    "occurred_at": str(r["created_at"] or ""),
                    "tags": [str(r["problem_family"]), str(r["source_model"])] + [str(t) for t in tags],
                }
            )

        # 8. subscription_audit active rows -> sure (entity_type='subscription')
        from siming.sub_hunter import enrich_subscription_row

        for r in conn.execute(
            "SELECT * FROM subscription_audit WHERE status IN ('ACTIVE', 'KEEP', 'ZOMBIE') ORDER BY updated_at DESC LIMIT 300"
        ).fetchall():
            er = enrich_subscription_row(dict(r))
            yuan = float(er["amount_cents"] or 0) / 100.0
            norm_name = str(er["normalized_name"] or er["merchant"] or er["sub_id"])
            nid = f"cg:sub:{er['sub_id']}"
            ongoing_lbl = str(er["ongoing_label"])
            nodes.append(
                {
                    "node_id": nid,
                    "app": "sure",
                    "entity_type": "subscription",
                    "title": f"[订阅:{er['status']}·{ongoing_lbl}] {norm_name} ¥{yuan:.2f}/{er['cadence_days']}天"[:160],
                    "body": (
                        f"商户={er['merchant']} 规范名={norm_name} 金额={er['currency']} {yuan:.2f}元 "
                        f"周期={er['cadence_days']}天 次数={er['occurrences']} 上次扣费={er['last_charged_at']} "
                        f"下次扣费={er['next_expected_at']} 涨价={er['price_hike_pct']}% 状态={er['status']} "
                        f"进行状态={ongoing_lbl}({er['ongoing_reason']}) 备注={er['notes']}"
                    ).strip(),
                    "provenance": f"subscription_audit:{er['sub_id']}",
                    "occurred_at": str(er["last_charged_at"] or er["updated_at"] or ""),
                    "tags": [
                        t
                        for t in (
                            "subscription",
                            str(er["status"]),
                            "ongoing" if er["is_ongoing"] else "stopped",
                            ongoing_lbl,
                            norm_name,
                            str(er["merchant"] or ""),
                        )
                        if t
                    ],
                }
            )

        # 9. learned 域：司命学到的全部长期知识（hypotheses / 已答 curiosity_questions / bookmarks / routines.md）
        learned_ref_to_nid: dict[str, str] = {}

        for r in conn.execute(
            "SELECT * FROM hypotheses WHERE status != 'REJECTED' ORDER BY created_at DESC LIMIT 200"
        ).fetchall():
            nid = f"cg:hyp:{r['id']}"
            learned_ref_to_nid[f"hypotheses:{r['id']}"] = nid
            nodes.append(
                {
                    "node_id": nid,
                    "app": "learned",
                    "entity_type": "hypothesis",
                    "title": f"[假设:{r['status']}] {str(r['claim'] or '')[:120]}",
                    "body": f"假设={r['claim']} 状态={r['status']} 证据={r['evidence_ids']} 过期={r['expires_at']}",
                    "provenance": f"hypotheses:{r['id']}",
                    "occurred_at": str(r["created_at"] or ""),
                    "tags": ["learned", "hypothesis", str(r["status"] or "")],
                }
            )

        for r in conn.execute(
            "SELECT * FROM curiosity_questions WHERE status = 'ANSWERED' AND trim(answer) != '' "
            "ORDER BY COALESCE(answered_at, created_at) DESC LIMIT 200"
        ).fetchall():
            nid = f"cg:cur:{r['qid']}"
            learned_ref_to_nid[f"curiosity:{r['qid']}"] = nid
            nodes.append(
                {
                    "node_id": nid,
                    "app": "learned",
                    "entity_type": "curiosity",
                    "title": f"[求证已答:{r['topic']}] {str(r['answer'] or '')[:110]}",
                    "body": f"主题={r['topic']} 问题={r['question']} 回答={r['answer']} 上下文={r['context_json']}",
                    "provenance": f"curiosity_questions:{r['qid']}",
                    "occurred_at": str(r["answered_at"] or r["created_at"] or ""),
                    "tags": ["learned", "curiosity", str(r["topic"] or "")],
                }
            )

        for r in conn.execute("SELECT * FROM bookmarks ORDER BY created_at DESC LIMIT 300").fetchall():
            nid = f"cg:bm:{r['bookmark_id']}"
            learned_ref_to_nid[f"bookmarks:{r['bookmark_id']}"] = nid
            rel_proj = _safe_json_loads(r["related_projects_json"], [])
            bm_tags = _safe_json_loads(r["tags_json"], [])
            tags = ["learned", "bookmark", str(r["kind"] or "")] + [str(x) for x in rel_proj] + [str(x) for x in bm_tags]
            nodes.append(
                {
                    "node_id": nid,
                    "app": "learned",
                    "entity_type": "bookmark",
                    "title": f"[收藏:{r['kind']}] {str(r['title'] or '')[:120]}",
                    "body": f"摘要={r['summary']} 仓库={r['repo_slug']} 链接={r['url']} 关联项目={','.join(str(x) for x in rel_proj)}",
                    "provenance": f"bookmarks:{r['bookmark_id']}",
                    "occurred_at": str(r["created_at"] or ""),
                    "tags": [t for t in dict.fromkeys(tags) if t],
                }
            )
    finally:
        conn.close()

    # 10. routines.md 分段 -> learned (entity_type='routine')
    try:
        from siming.event_chain import routines_md_path

        r_path = routines_md_path(db)
    except Exception:
        r_path = db.paths.data_dir / "routines.md"
    if r_path.is_file():
        try:
            r_raw = r_path.read_text(encoding="utf-8", errors="ignore")
            r_mtime = iso_now(datetime.fromtimestamp(r_path.stat().st_mtime).astimezone())
            parsed_any = False
            ym = re.search(r"```yaml\s*\n(.*?)```", r_raw, re.DOTALL)
            if ym:
                try:
                    import yaml

                    doc = yaml.safe_load(ym.group(1)) or {}
                    r_ts = str(doc.get("updated_at") or r_mtime)
                    for idx_r, item in enumerate(doc.get("routines") or [], start=1):
                        if not isinstance(item, dict):
                            continue
                        rid = str(item.get("id") or f"R{idx_r}").strip()
                        r_title = str(item.get("title") or rid).strip()
                        nid = f"cg:routine:{rid}"
                        learned_ref_to_nid[f"routines.md:{rid}"] = nid
                        ev_list = item.get("evidence") or []
                        ev_str = ", ".join(str(x) for x in ev_list) if isinstance(ev_list, list) else str(ev_list)
                        r_tags = [
                            "learned",
                            "routine",
                            rid,
                            str(item.get("cadence") or ""),
                            str(item.get("origin") or ""),
                            str(item.get("status") or ""),
                        ]
                        nodes.append(
                            {
                                "node_id": nid,
                                "app": "learned",
                                "entity_type": "routine",
                                "title": f"[规律:{rid}] {r_title[:120]}",
                                "body": (
                                    f"标题={r_title} 周期={item.get('cadence', '')} 窗口={item.get('window', '')} "
                                    f"来源={item.get('origin', '')} 置信度={item.get('confidence', '')} "
                                    f"状态={item.get('status', '')} 证据={ev_str}"
                                ),
                                "provenance": f"routines.md:{rid}",
                                "occurred_at": r_ts,
                                "tags": [t for t in r_tags if t],
                            }
                        )
                        parsed_any = True
                except Exception:
                    parsed_any = False
            if not parsed_any and r_raw.strip():
                # 兜底：若 routines.md 为纯 Markdown 段落（非 YAML），按二级标题/条目切分索引
                sections = re.split(r"(?m)^##+\s+", r_raw)
                for idx_s, sec in enumerate(sections):
                    sec_clean = sec.strip()
                    if not sec_clean or len(sec_clean) < 6:
                        continue
                    first_ln = sec_clean.splitlines()[0].lstrip("# ").strip()[:100]
                    nid = _norm_id("routine", f"{idx_s}:{first_ln}")
                    nodes.append(
                        {
                            "node_id": nid,
                            "app": "learned",
                            "entity_type": "routine",
                            "title": f"[规律] {first_ln}",
                            "body": sec_clean[:1200],
                            "provenance": f"routines.md#sec{idx_s}",
                            "occurred_at": r_mtime,
                            "tags": ["learned", "routine"],
                        }
                    )
        except OSError:
            pass

    # Wiki <-> Learned 显式蒸馏溯源边（distilled_from）
    for wiki_nid, src_ref in wiki_provenance_links:
        target_nid = learned_ref_to_nid.get(src_ref)
        if target_nid:
            explicit_links.append((wiki_nid, target_nid, "distilled_from", 2.2))

    # 统一脱敏所有节点（在实体抽取与落库前执行，杜绝敏感密钥/验证码/支付密码/手机号落入图谱节点或边）
    for n in nodes:
        n["title"] = _sanitize_graph_text(str(n.get("title") or ""), paths=db.paths)[:160]
        n["body"] = _sanitize_graph_text(str(n.get("body") or ""), paths=db.paths)[:4000]
        raw_tags = n.get("tags") or []
        clean_tags: list[str] = []
        for t in raw_tags:
            st = _sanitize_graph_text(str(t or ""), paths=db.paths).strip()
            if st and "[REDACTED" not in st:
                clean_tags.append(st)
        n["tags"] = clean_tags

    # 构建实体倒排索引以生成跨应用图边
    inverted: dict[tuple[str, str], list[tuple[str, str]]] = defaultdict(list)
    node_app: dict[str, str] = {}
    for n in nodes:
        nid = n["node_id"]
        app = n["app"]
        node_app[nid] = app
        full_text = f"{n['title']}\n{n['body']}\n{n['occurred_at']}"
        ents = _extract_entities(full_text, extra_tags=n.get("tags"), extra_destinations=extra_destinations)
        for etype, vals in ents.items():
            for val in vals:
                inverted[(etype, val)].append((nid, app))

    relation_weights = {
        "date": 1.6,
        "destination": 1.8,
        "merchant": 1.4,
        "topic": 1.5,
        "amount": 1.7,
        "card": 1.3,
        "tag": 1.5,
        "month": 0.8,
    }

    edges: dict[tuple[str, str, str], float] = {}
    for src, dst, rel, w in explicit_links:
        a, b = (src, dst) if src < dst else (dst, src)
        edges[(a, b, rel)] = max(edges.get((a, b, rel), 0.0), w)

    high_signal_apps = frozenset({"learned", "wiki", "msim", "files", "calendar"})
    for (etype, val), members in inverted.items():
        if len(members) < 2:
            continue
        # 避免超泛化实体造成边爆炸：超过 40 个节点时，按应用挑选最具代表性的跨应用锚点（保留 learned/wiki/msim/files/calendar + 每应用最多 3 个最新节点）
        if len(members) > 40:
            if etype in ("month", "date", "amount"):
                continue
            by_app_bucket: dict[str, list[tuple[str, str]]] = defaultdict(list)
            for nid_m, app_m in members:
                cap = 5 if app_m in high_signal_apps else 3
                if len(by_app_bucket[app_m]) < cap:
                    by_app_bucket[app_m].append((nid_m, app_m))
            if len(by_app_bucket) < 2:
                continue
            sampled: list[tuple[str, str]] = []
            for bucket in by_app_bucket.values():
                sampled.extend(bucket)
            members = sampled[:24]

        weight = relation_weights.get(etype, 1.0)
        rel = f"shared_{etype}:{val}"
        for i in range(len(members)):
            nid_i, app_i = members[i]
            for j in range(i + 1, len(members)):
                nid_j, app_j = members[j]
                if nid_i == nid_j:
                    continue
                # 优先跨应用联结；同应用仅保留高价值实体（destination/date/amount）
                if app_i == app_j and etype not in ("destination", "date", "amount"):
                    continue
                a, b = (nid_i, nid_j) if nid_i < nid_j else (nid_j, nid_i)
                edges[(a, b, rel)] = max(edges.get((a, b, rel), 0.0), weight)

    by_app: dict[str, int] = defaultdict(int)
    with db.transaction() as c:
        c.execute("DELETE FROM context_graph_edges")
        c.execute("DELETE FROM context_graph_nodes")
        for n in nodes:
            by_app[n["app"]] += 1
            c.execute(
                """
                INSERT INTO context_graph_nodes (
                    node_id, app, entity_type, title, body, provenance, occurred_at, tags_json, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    n["node_id"],
                    n["app"],
                    n["entity_type"],
                    n["title"],
                    n["body"],
                    n["provenance"],
                    n["occurred_at"],
                    json.dumps(n.get("tags") or [], ensure_ascii=False),
                    ts,
                ),
            )
        for (src, dst, rel), w in edges.items():
            c.execute(
                """
                INSERT OR REPLACE INTO context_graph_edges (src_id, dst_id, relation, weight, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (src, dst, rel, float(w), ts),
            )

    return {
        "ok": True,
        "nodes_indexed": len(nodes),
        "edges_created": len(edges),
        "by_app": dict(sorted(by_app.items())),
        "indexed_at": ts,
    }


def _tokenize_query(question: str) -> tuple[list[str], list[str]]:
    """切分中英文检索词，并提取相对年份提示（去年/今年）。返回 (terms, year_hints)。"""
    q = (question or "").strip()
    low = q.lower()
    terms: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        t = tok.strip().lower()
        if len(t) < 2 or t in _CN_STOPWORDS or t in seen:
            return
        seen.add(t)
        terms.append(t)

    # 相对年份
    year_hints: list[str] = []
    cur_year = _db.now_local().year
    if "去年" in q:
        year_hints.append(str(cur_year - 1))
    if "今年" in q:
        year_hints.append(str(cur_year))
    if "前年" in q:
        year_hints.append(str(cur_year - 2))
    for y in re.findall(r"\b(20\d{2})\b", q):
        if y not in year_hints:
            year_hints.append(y)

    # 领域词汇优先提取
    for dt in _DOMAIN_TERMS:
        if dt in low:
            _add(dt)

    # 英文/数字 token
    for tok in _ASCII_TOKEN_RE.findall(q):
        _add(tok)

    # 中文长串切 2-gram 作为召回兜底（跳过已被领域词完整覆盖的停用词组合）
    for run in _CJK_RUN_RE.findall(q):
        if len(run) <= 4 and run not in _CN_STOPWORDS:
            _add(run)
        for i in range(len(run) - 1):
            bg = run[i : i + 2]
            if bg not in _CN_STOPWORDS and not any(ch in "的了哪张家是有没什怎多一帮请查" for ch in bg):
                _add(bg)

    return terms, year_hints


def _make_snippet(body: str, terms: list[str], max_len: int = 160) -> str:
    clean = re.sub(r"\s+", " ", body or "").strip()
    if len(clean) <= max_len:
        return clean
    low = clean.lower()
    best_pos = -1
    for t in terms:
        pos = low.find(t)
        if pos >= 0:
            best_pos = pos
            break
    if best_pos < 0:
        return clean[: max_len - 1] + "…"
    start = max(0, best_pos - 40)
    end = min(len(clean), start + max_len)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(clean) else ""
    return f"{prefix}{clean[start:end]}{suffix}"


def query_context_graph(
    db: SimingDB,
    question: str,
    apps: Optional[list[str]] = None,
    limit: int = 10,
) -> dict[str, Any]:
    """跨应用联邦检索：融合直接词项打分与 1-hop 图谱邻居关联，返回精确溯源引用。"""
    db.init_db()
    if not is_module_enabled(db, "context_graph"):
        return {
            "ok": False,
            "question": question,
            "terms": [],
            "apps_matched": [],
            "hits": [],
            "total_hits": 0,
            "reply": "跨应用上下文图谱模块已关闭。",
        }

    terms, year_hints = _tokenize_query(question)
    app_filter = {a.strip().lower() for a in (apps or []) if a and a.strip()}

    conn = db.connect()
    try:
        all_nodes = [dict(r) for r in conn.execute("SELECT * FROM context_graph_nodes").fetchall()]
        all_edges = [dict(r) for r in conn.execute("SELECT * FROM context_graph_edges").fetchall()]
    finally:
        conn.close()

    node_map: dict[str, dict[str, Any]] = {n["node_id"]: n for n in all_nodes}
    adj: dict[str, list[tuple[str, str, float]]] = defaultdict(list)
    for e in all_edges:
        s, d, rel, w = str(e["src_id"]), str(e["dst_id"]), str(e["relation"]), float(e["weight"] or 1.0)
        adj[s].append((d, rel, w))
        adj[d].append((s, rel, w))

    # 第一阶段：直接词项打分
    direct_scores: dict[str, float] = {}
    matched_terms_map: dict[str, list[str]] = {}
    for n in all_nodes:
        nid = n["node_id"]
        if app_filter and str(n["app"]).lower() not in app_filter:
            continue
        title_low = str(n["title"] or "").lower()
        body_low = str(n["body"] or "").lower()
        tags_list = _safe_json_loads(n["tags_json"], [])
        tags_low = " ".join(str(t).lower() for t in tags_list)
        occ = str(n["occurred_at"] or "")

        score = 0.0
        matched: list[str] = []
        for t in terms:
            hit_here = False
            if t in title_low:
                score += 3.5
                hit_here = True
            if t in tags_low:
                score += 2.5
                hit_here = True
            if t in body_low:
                score += 1.5
                hit_here = True
            if hit_here:
                matched.append(t)

        if matched:
            # 多词命中协同加权
            score += 0.8 * max(0, len(matched) - 1)
            for yh in year_hints:
                if yh in occ or yh in title_low or yh in body_low:
                    score += 2.2
            direct_scores[nid] = score
            matched_terms_map[nid] = matched

    # 第二阶段：1-hop 图谱邻域扩散（把强相关跨应用证据拉入结果）
    combined_scores: dict[str, float] = dict(direct_scores)
    top_seeds = sorted(direct_scores.items(), key=lambda kv: kv[1], reverse=True)[:15]
    for seed_id, seed_score in top_seeds:
        seed_app = node_map[seed_id]["app"]
        for nbr_id, rel, w in adj.get(seed_id, []):
            nbr = node_map.get(nbr_id)
            if not nbr:
                continue
            if app_filter and str(nbr["app"]).lower() not in app_filter:
                continue
            cross_bonus = 1.25 if nbr["app"] != seed_app else 0.7
            hop_boost = min(3.0, (seed_score * 0.22) * w * cross_bonus)
            combined_scores[nbr_id] = combined_scores.get(nbr_id, 0.0) + hop_boost

    ranked_ids = sorted(
        combined_scores.keys(),
        key=lambda nid: (combined_scores[nid], str(node_map[nid].get("occurred_at") or "")),
        reverse=True,
    )[: max(1, int(limit))]

    hits: list[dict[str, Any]] = []
    for nid in ranked_ids:
        n = node_map[nid]
        linked: list[dict[str, Any]] = []
        seen_nbr: set[str] = set()
        for nbr_id, rel, w in sorted(adj.get(nid, []), key=lambda x: x[2], reverse=True):
            if nbr_id in seen_nbr or nbr_id not in node_map:
                continue
            seen_nbr.add(nbr_id)
            nbr = node_map[nbr_id]
            linked.append(
                {
                    "node_id": nbr_id,
                    "app": nbr["app"],
                    "title": nbr["title"],
                    "relation": rel,
                    "provenance": nbr["provenance"],
                    "occurred_at": nbr["occurred_at"],
                }
            )
            if len(linked) >= 5:
                break

        hits.append(
            {
                "node_id": nid,
                "app": n["app"],
                "entity_type": n["entity_type"],
                "title": n["title"],
                "provenance": n["provenance"],
                "occurred_at": n["occurred_at"],
                "snippet": _make_snippet(str(n["body"] or ""), terms),
                "score": round(float(combined_scores[nid]), 2),
                "matched_terms": matched_terms_map.get(nid, []),
                "linked_nodes": linked,
            }
        )

    result: dict[str, Any] = {
        "ok": True,
        "question": question,
        "terms": terms,
        "year_hints": year_hints,
        "apps_matched": sorted({h["app"] for h in hits}),
        "hits": hits,
        "total_hits": len(hits),
    }
    result["reply"] = format_graph_answer(result)
    return result


def get_graph_stats(db: SimingDB) -> dict[str, Any]:
    """返回当前跨应用上下文图谱的节点、边统计与各应用分布。"""
    db.init_db()
    conn = db.connect()
    try:
        total_nodes = int(conn.execute("SELECT COUNT(*) FROM context_graph_nodes").fetchone()[0])
        total_edges = int(conn.execute("SELECT COUNT(*) FROM context_graph_edges").fetchone()[0])
        by_app = {
            str(r["app"]): int(r["cnt"])
            for r in conn.execute(
                "SELECT app, COUNT(*) AS cnt FROM context_graph_nodes GROUP BY app ORDER BY app"
            ).fetchall()
        }
        last_row = conn.execute("SELECT MAX(updated_at) AS ts FROM context_graph_nodes").fetchone()
        last_updated = str(last_row["ts"] or "") if last_row else ""
        rel_counts: dict[str, int] = defaultdict(int)
        for r in conn.execute("SELECT relation FROM context_graph_edges").fetchall():
            prefix = str(r["relation"]).split(":", 1)[0]
            rel_counts[prefix] += 1
    finally:
        conn.close()

    return {
        "ok": True,
        "total_nodes": total_nodes,
        "total_edges": total_edges,
        "by_app": by_app,
        "relations": dict(sorted(rel_counts.items())),
        "last_updated_at": last_updated,
    }


def format_graph_answer(result: dict[str, Any]) -> str:
    """将联邦图谱检索结果格式化为带精确溯源引用与跨应用关联链的可读回答。"""
    q = str(result.get("question") or "").strip()
    hits = list(result.get("hits") or [])
    if not hits:
        return f"🔎 跨应用上下文图谱未检索到与「{q}」匹配的记录（建议先执行一次索引刷新）。"

    apps = ", ".join(result.get("apps_matched") or [])
    lines = [
        f"🕸️ 跨应用上下文图谱检索：「{q}」",
        f"命中 {len(hits)} 条证据，覆盖应用：{apps}",
    ]
    for idx, h in enumerate(hits, 1):
        occ = str(h.get("occurred_at") or "-")[:16].replace("T", " ")
        lines.append(
            f"{idx}. [{h['app']}] {h['title']}（{occ} | 来源 `{h['provenance']}` | 相关度 {h['score']}）"
        )
        if h.get("snippet"):
            lines.append(f"   摘要：{h['snippet']}")
        linked = list(h.get("linked_nodes") or [])
        if linked:
            link_str = "；".join(
                f"[{ln['app']}]{ln['title'][:28]}({ln['relation']})" for ln in linked[:3]
            )
            lines.append(f"   关联图边：{link_str}")
    return "\n".join(lines)
