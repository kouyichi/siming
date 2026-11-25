"""Omamail 本机多邮箱只读桥接采集器（零凭据复制、不改已读状态、自动抓数电发票 PDF 与订阅续期）。"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import subprocess
import time
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional
from zoneinfo import ZoneInfo

import httpx

from siming.crypto import contains_otp, store_encrypted_raw
from siming.db import SimingDB, iso_now, now_local
from siming.ledger.reimburse import stage_invoice_attachment, summarize_reimbursement_bundle
from siming.ledger.statement_parser import try_ingest_statement_text
from siming.modules import is_module_enabled

SHANGHAI_TZ = ZoneInfo("Asia/Shanghai")
DEFAULT_OMAMAIL_BIN = Path.home() / ".local/share/omamail/bin/omamail"
DEFAULT_OMAMAIL_CACHE = Path.home() / ".cache/omamail"
STAGED_BILL_TTL_SECONDS = 1800

INVOICE_KEYWORDS = ("发票", "行程单", "报销单", "电子客票", "水单", "滴滴出行", "美团", "invoice", "receipt")
STATEMENT_KEYWORDS = ("账单", "信用卡", "月结单", "电子对账单", "还款提醒", "statement")
BILL_ARCHIVE_KEYWORDS = ("交易流水", "交易明细", "支付账单", "账单明细", "微信支付", "支付宝", "美团账单", "抖音")
RENEWAL_KEYWORDS = ("订阅", "续期", "续费", "自动续费", "auto-renew", "renewal", "icloud+")
NOUS_BILLING_KEYWORDS = (
    "nous research",
    "nousresearch",
    "auto-refill",
    "insufficient funds",
    "payments are temporarily unavailable",
    "purchase confirmation",
)
TRUSTED_INVOICE_LINK_DOMAINS = (
    "baiwang.com",
    "51fapiao.cn",
    "nuonuo.com",
    "jss.com.cn",
    "piaozone.com",
    "aisino.com",
    "xiaojukeji.com",
    "didiglobal.com",
    "meituan.com",
)

RENEW_EXACT_DATE_RE = re.compile(r"(20\d{2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日\s*(?:续期|续费|自动续费|扣费)")
RENEW_REL_DAYS_RE = re.compile(r"将在\s*(\d{1,2})\s*天后\s*(?:续期|续费)")
PRICE_RE = re.compile(r"(?:[¥￥]\s*[0-9]+(?:\.[0-9]{1,2})?|\$[0-9]+(?:\.[0-9]{1,2})?)")


def _local_proxy_alive(host: str = "127.0.0.1", port: int = 7890) -> bool:
    try:
        with socket.create_connection((host, port), timeout=0.4):
            return True
    except OSError:
        return False


def _build_omamail_env(for_domestic: bool = False) -> dict[str, str]:
    env = dict(os.environ)
    # 严禁触碰桌面 Wayland/X11，确保纯无头 CLI 运行
    env.pop("WAYLAND_DISPLAY", None)
    env.pop("DISPLAY", None)
    env["QT_QPA_PLATFORM"] = "offscreen"
    # 移除可能导致 IMAP 裸连接卡住的 socks5 ALL_PROXY
    for k in ("ALL_PROXY", "all_proxy"):
        env.pop(k, None)
    if for_domestic:
        for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            env.pop(k, None)
    elif _local_proxy_alive():
        env.setdefault("HTTP_PROXY", "http://127.0.0.1:7890")
        env.setdefault("HTTPS_PROXY", "http://127.0.0.1:7890")
        env.setdefault("http_proxy", "http://127.0.0.1:7890")
        env.setdefault("https_proxy", "http://127.0.0.1:7890")
        env["NO_PROXY"] = "localhost,127.0.0.1,::1,.163.com,163.com,.126.com,126.com"
    return env


def resolve_omamail_bin() -> Optional[Path]:
    if os.environ.get("SIMING_DISABLE_LIVE_OMAMAIL") == "1":
        return None
    override = os.environ.get("OMAMAIL_BIN")
    if override is not None:
        p = Path(override)
        return p if p.is_file() else None
    return DEFAULT_OMAMAIL_BIN if DEFAULT_OMAMAIL_BIN.is_file() else None


def resolve_omamail_cache_dir() -> Path:
    override = os.environ.get("OMAMAIL_CACHE_DIR")
    return Path(override) if override else DEFAULT_OMAMAIL_CACHE


def _default_cli_runner(
    om_bin: Path,
    args: list[str],
    timeout: int = 15,
    domestic: bool = False,
    stdin_json: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            [str(om_bin), *args, "--json"],
            input=json.dumps(stdin_json, ensure_ascii=False) if stdin_json is not None else None,
            capture_output=True,
            text=True,
            env=_build_omamail_env(for_domestic=domestic),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": {"code": "timeout"}}
    except OSError as exc:
        return {"ok": False, "error": {"code": "os_error", "message": str(exc)[:180]}}
    raw = (proc.stdout or "").strip()
    if raw.startswith("{"):
        try:
            return json.loads(raw)
        except Exception:
            pass
    return {"ok": False, "error": {"message": (proc.stderr or raw)[:240]}}


def _cleanup_expired_staged_bills(db: SimingDB) -> None:
    s_dir = db.paths.tmp_root / "staged-bills"
    if not s_dir.is_dir():
        return
    cutoff = time.time() - STAGED_BILL_TTL_SECONDS
    for f in s_dir.iterdir():
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
        except OSError:
            continue


def _fetch_omamail_attachment_bytes(
    runner: Callable[..., dict[str, Any]],
    provider: str,
    account_id: str,
    message_id: str,
    attachment_id: str,
    domestic: bool,
    max_bytes: int = 28 * 1024 * 1024,
) -> Optional[bytes]:
    """通过 Omamail JSON-RPC (imap.attachment / gmail.attachment) 无头只读拉取邮件附件，零接触底层凭据。"""
    if not attachment_id:
        return None
    is_gmail = (provider == "gmail") and not account_id.startswith("imap:")
    method = "gmail.attachment" if is_gmail else "imap.attachment"
    params: dict[str, Any] = {
        "accountId": account_id,
        "messageId": message_id,
        "attachmentId": attachment_id,
    }
    if not is_gmail:
        params["requestToken"] = f"siming:{message_id}:{attachment_id}"
    try:
        resp = runner(["call", method], timeout=22, domestic=domestic, stdin_json=params)
    except TypeError:
        # 兼容旧测试桩未声明 stdin_json 参数的情形
        return None
    if not isinstance(resp, dict) or not resp.get("ok"):
        return None
    res_obj = resp.get("result")
    b64 = str((res_obj.get("data") if isinstance(res_obj, dict) else "") or "").strip()
    if not b64:
        return None
    pad = "=" * (-len(b64) % 4)
    try:
        data = base64.urlsafe_b64decode(b64 + pad) if ("-" in b64 or "_" in b64) else base64.b64decode(b64 + pad)
    except Exception:
        return None
    if len(data) > max_bytes:
        return None
    return data


def _format_sender(frm: Any) -> str:
    if isinstance(frm, dict):
        name = str(frm.get("name") or frm.get("display") or "").strip()
        addr = str(frm.get("email") or "").strip()
        if name and addr and name != addr:
            return f"{name} <{addr}>"
        return addr or name
    return str(frm or "").strip()


def _msg_iso_date(item: dict[str, Any]) -> str:
    if item.get("date"):
        return str(item["date"])
    ms = item.get("dateMs")
    if isinstance(ms, (int, float)) and ms > 0:
        return datetime.fromtimestamp(ms / 1000.0, tz=SHANGHAI_TZ).isoformat(timespec="seconds")
    return str(item.get("fullTime") or iso_now())


def _is_actionable_summary(subject: str, snippet: str, sender: str) -> bool:
    hay = f"{subject} {snippet} {sender}".lower()
    if "hermes-agent" in hay and "run failed:" in hay:
        return False
    for kw in (*INVOICE_KEYWORDS, *STATEMENT_KEYWORDS, *BILL_ARCHIVE_KEYWORDS, *RENEWAL_KEYWORDS, *NOUS_BILLING_KEYWORDS):
        if kw in hay:
            return True
    return False


def _url_resolves_to_public_ip(url: str) -> bool:
    """SSRF 防护：发票 PDF 下载仅允许解析为公网全局地址的 http/https 主机，严禁访问本机/内网/保留段。"""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return False
    host = parsed.hostname.strip().lower()
    if host in ("localhost", "localhost.localdomain"):
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except OSError:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip_obj = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if not ip_obj.is_global:
            return False
    return True


def _safe_fetch_pdf_bytes(url: str, max_bytes: int = 15 * 1024 * 1024) -> Optional[bytes]:
    """逐跳固定已验证公网 IP 并发起 GET，杜绝 DNS Rebinding 绕过并限制 PDF 字节上限。"""
    from siming.net_guard import safe_pinned_public_get

    code, data, _ = safe_pinned_public_get(
        url,
        require_https=False,
        max_hops=4,
        timeout_sec=12.0,
        max_bytes=max_bytes,
        status_only=False,
    )
    if code != 200 or not data.startswith(b"%PDF-"):
        return None
    return data


def _download_invoice_links(db: SimingDB, links: list[dict[str, Any]]) -> list[str]:
    """从邮件超链接自动下载国家税务总局数电发票 PDF（将 OFD/XML 自动归一为 PDF）或受信任 https 直链 PDF。"""
    if os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return []
    staged: list[str] = []
    seen_urls: set[str] = set()
    candidates: list[tuple[str, str]] = []

    for lnk in links:
        if not isinstance(lnk, dict):
            continue
        url = str(lnk.get("url") or "").strip()
        txt = str(lnk.get("text") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        parsed_u = urllib.parse.urlsplit(url)
        host_low = (parsed_u.hostname or "").lower()
        if (host_low == "chinatax.gov.cn" or host_low.endswith(".chinatax.gov.cn")) and (
            "exportDzfpwjEwm" in url or "Wjgs=" in url
        ):
            pdf_url = re.sub(r"Wjgs=(?:OFD|XML|O|X)\b", "Wjgs=PDF", url)
            m_fphm = re.search(r"Fphm=(\d{8,24})", pdf_url)
            fname = f"数电发票-{m_fphm.group(1)}.pdf" if m_fphm else "数电发票.pdf"
            candidates.append((fname, pdf_url))
        elif parsed_u.scheme == "https" and (
            url.lower().split("?", 1)[0].endswith(".pdf")
            or ("pdf" in txt.lower() and "wxmpurl.cn" not in host_low)
            or (
                any(host_low == dom or host_low.endswith("." + dom) for dom in TRUSTED_INVOICE_LINK_DOMAINS)
                and any(k in (url + " " + txt).lower() for k in ("pdf", "download", "invoice", "fapiao", "发票", "行程单"))
            )
        ):
            fname = Path(url.split("?", 1)[0]).name
            if not fname.lower().endswith(".pdf"):
                fname = "行程报销单.pdf" if "行程" in txt else "电子发票.pdf"
            candidates.append((fname, url))

    for fname, target_url in candidates[:3]:
        if target_url in seen_urls:
            continue
        seen_urls.add(target_url)
        try:
            pdf_bytes = _safe_fetch_pdf_bytes(target_url)
            if pdf_bytes:
                out_p = stage_invoice_attachment(db, fname, pdf_bytes)
                staged.append(str(out_p))
        except Exception:
            continue
    return staged


def _canonical_renewal_family(service_name: str, price_str: str, context_hint: str = "") -> str:
    low = re.sub(r"[\s（）()·•]+", "", (service_name or "").lower())
    hint_low = re.sub(r"[\s（）()·•]+", "", (context_hint or "").lower())
    if "icloud" in low or "icloud" in hint_low:
        m_gb = re.search(r"(\d+)\s*(?:gb|tb)", low) or re.search(r"(\d+)\s*(?:gb|tb)", hint_low)
        return f"icloud+:{m_gb.group(0) if m_gb else price_str}"
    return f"{low[:24]}:{price_str}"


def _try_ingest_subscription_renewal(
    db: SimingDB,
    subject: str,
    body_text: str,
    msg_date_iso: str,
    event_id: str,
) -> Optional[str]:
    """解析 Apple / 云服务订阅自动续期提醒，按规范化订阅族去重并自动归档已过期的旧周期单据。"""
    combined = f"{subject}\n{body_text}"
    due_d: Optional[date] = None

    m_exact = RENEW_EXACT_DATE_RE.search(combined)
    if m_exact:
        try:
            due_d = date(int(m_exact.group(1)), int(m_exact.group(2)), int(m_exact.group(3)))
        except ValueError:
            due_d = None
    else:
        m_rel = RENEW_REL_DAYS_RE.search(combined)
        if m_rel:
            try:
                base_d = datetime.fromisoformat(msg_date_iso.replace("Z", "+00:00")).astimezone(SHANGHAI_TZ).date()
            except Exception:
                base_d = now_local().date()
            due_d = base_d + timedelta(days=int(m_rel.group(1)))

    today_d = now_local().date()
    if not due_d or due_d < today_d:
        return None

    m_price = PRICE_RE.search(combined)
    price_str = m_price.group(0).replace(" ", "") if m_price else ""

    # 提取订阅产品名（优先找续期日期上一行非空标题，否则用邮件主题）
    service_name = ""
    lines = [ln.strip() for ln in body_text.splitlines() if ln.strip() and not ln.strip().startswith("[image")]
    for idx, ln in enumerate(lines):
        if "续期" in ln and idx >= 1 and ln != "续期确认":
            cand = lines[idx - 1]
            if 2 <= len(cand) <= 48 and cand not in ("续期确认", "iCloud"):
                service_name = cand
                break
    if not service_name:
        service_name = re.sub(r"\s*将在.*$", "", subject).strip()[:40] or "自动续费项目"

    fam_key = _canonical_renewal_family(service_name, price_str, context_hint=combined)
    title = f"订阅续期预警：{service_name}" + (f" ({price_str})" if price_str else "")
    conn = db.connect()
    try:
        exists = conn.execute(
            "SELECT case_id, title, context_json FROM cases WHERE category='subscription_renew' AND due_at=?",
            (due_d.isoformat(),),
        ).fetchall()
        for ex in exists:
            ex_ctx = json.loads(ex["context_json"] or "{}") if ex["context_json"] else {}
            if ex["title"] == title or ex_ctx.get("family") == fam_key or _canonical_renewal_family(str(ex["title"]), price_str) == fam_key:
                return str(ex["case_id"])
    finally:
        conn.close()

    ts = iso_now()
    with db.transaction() as conn:
        # 自动归档同订阅族里已过期（due_at < 今日）的旧月度待处理单（如旧周期 S05 -> 新周期 S50）
        old_rows = conn.execute(
            "SELECT case_id, title, context_json FROM cases WHERE category='subscription_renew' AND status='待处理' AND due_at < ?",
            (today_d.isoformat(),),
        ).fetchall()
        for old_r in old_rows:
            o_ctx = json.loads(old_r["context_json"] or "{}") if old_r["context_json"] else {}
            if o_ctx.get("family") == fam_key or _canonical_renewal_family(str(old_r["title"]), price_str) == fam_key:
                conn.execute(
                    "UPDATE cases SET status='完成', closed_at=?, state_version=state_version+1, updated_at=? WHERE case_id=?",
                    (ts, ts, old_r["case_id"]),
                )
        cid = db.next_seq_id(conn, "S")
        conn.execute(
            """
            INSERT INTO cases (
                case_id, title, next_step, due_at, done_rule, status, strength,
                category, rule_id, evidence_ids, context_json, created_at, updated_at
            ) VALUES (?, ?, ?, ?, '不需要则提前一天取消订阅，保留则回「已办」', '待处理', 'NORMAL', 'subscription_renew', 'sub_renew', ?, ?, ?, ?)
            """,
            (
                cid,
                title,
                f"将于 {due_d.isoformat()} 自动扣费续期（不需要请提前至少 1 天取消）",
                due_d.isoformat(),
                json.dumps([event_id]),
                json.dumps({"family": fam_key}, ensure_ascii=False),
                ts,
                ts,
            ),
        )
    return cid


def _load_cached_account_summaries(cache_dir: Path) -> dict[str, dict[str, Any]]:
    """读取 ~/.cache/omamail/account-*.json 本地缓存摘要（毫秒级，不发网络请求）。"""
    out: dict[str, dict[str, Any]] = {}
    if not cache_dir.is_dir():
        return out
    for p in sorted(cache_dir.glob("account-*.json")):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            continue
        email_addr = str(data.get("account") or "").strip().lower()
        if not email_addr:
            continue
        merged_msgs: list[dict[str, Any]] = []
        latest_at = 0.0
        for qv in (data.get("queries") or {}).values():
            if not isinstance(qv, dict):
                continue
            latest_at = max(latest_at, float(qv.get("at") or 0.0))
            for s in qv.get("summaries") or []:
                if isinstance(s, dict) and s.get("id"):
                    merged_msgs.append(s)
        out[email_addr] = {"at_ms": latest_at, "messages": merged_msgs}
    return out


def collect_omamail(
    db: SimingDB,
    max_per_account: int = 25,
    cli_runner: Optional[Callable[..., dict[str, Any]]] = None,
    om_bin_override: Optional[Path] = None,
    cache_dir_override: Optional[Path] = None,
) -> dict[str, Any]:
    """从 Omamail 只读同步全部已连接邮箱（163 / Yahoo / Gmail），不碰凭据、不改未读状态。"""
    db.init_db()
    _cleanup_expired_staged_bills(db)
    om_bin = om_bin_override or resolve_omamail_bin()
    if not om_bin:
        return {"ok": False, "available": False, "reason": "omamail_not_found"}

    runner = cli_runner or (
        lambda args, timeout=15, domestic=False, stdin_json=None: _default_cli_runner(
            om_bin, args, timeout, domestic, stdin_json
        )
    )
    cache_dir = cache_dir_override or resolve_omamail_cache_dir()

    acc_resp = runner(["accounts", "list"], timeout=10, domestic=True)
    # 兼容直接返回 {"accounts": ...} 或 {"ok": true, "result": {"accounts": ...}}
    acc_payload = acc_resp.get("result", acc_resp) if isinstance(acc_resp, dict) else {}
    accounts = acc_payload.get("accounts") if isinstance(acc_payload, dict) else []
    if not accounts:
        return {"ok": True, "available": True, "unconfigured": True, "accounts": 0}

    cached_map = _load_cached_account_summaries(cache_dir)

    conn = db.connect()
    try:
        seen_fps = {
            str(r["fingerprint"])
            for r in conn.execute(
                "SELECT fingerprint FROM events WHERE fingerprint LIKE 'omamail:%'"
            ).fetchall()
        }
    finally:
        conn.close()

    account_statuses: list[str] = []
    degraded_accounts: list[str] = []
    ingested_total = 0
    invoices_staged = 0
    renewals_tracked = 0
    statements_matched = 0
    otp_dropped = 0

    def _source_id_for_email(addr: str, prov: str) -> str:
        if "@163.com" in addr or "@126.com" in addr:
            return "omamail_163"
        if "@yahoo." in addr:
            return "omamail_yahoo"
        if prov == "gmail" or "@gmail.com" in addr:
            return "omamail_gmail"
        safe_dom = re.sub(r"[^a-z0-9]+", "_", addr.split("@")[-1].split(".")[0]) or "other"
        return f"omamail_{safe_dom}"

    for acc in accounts:
        acc_id = str(acc.get("id") or acc.get("email") or "").strip()
        email_addr = str(acc.get("email") or acc_id).strip().lower()
        label = str(acc.get("label") or email_addr.split("@")[-1])
        provider = str(acc.get("provider") or "")
        per_src_id = _source_id_for_email(email_addr, provider)
        is_domestic = any(d in email_addr for d in ("@163.com", "@126.com", "@yeah.net", "@qq.com"))

        by_id: dict[str, dict[str, Any]] = {}
        live_ok = False
        live_err = ""

        c_entry = cached_map.get(email_addr) or {}
        cache_age_hours = (
            (now_local().timestamp() * 1000.0 - float(c_entry.get("at_ms") or 0.0)) / 3600000.0
            if c_entry.get("at_ms")
            else 9999.0
        )
        backoff_key = f"omamail_backoff:{acc_id}"
        backoff_st: dict[str, Any] = {}
        raw_bo = db.get_meta(backoff_key)
        if raw_bo:
            try:
                parsed_bo = json.loads(raw_bo)
                if isinstance(parsed_bo, dict):
                    backoff_st = parsed_bo
            except Exception:
                backoff_st = {}
        backoff_until_ts = float(backoff_st.get("until_ts") or 0.0)
        in_timeout_backoff = False

        # 1. 国内高速 IMAP（如 163）主动拉实时列表；Gmail 主动探测登录态（超时启用退避 + 新鲜缓存回落，防每日 12 次抖动告警）；Yahoo 优先用桌面守护新鲜缓存
        if is_domestic:
            l_res = runner(["list", "--account", acc_id, "--limit", str(max_per_account)], timeout=14, domestic=True)
            if l_res.get("ok"):
                live_ok = True
                for m in (l_res.get("result") or {}).get("messages") or []:
                    if isinstance(m, dict) and m.get("id"):
                        by_id[str(m["id"])] = m
            else:
                live_err = str((l_res.get("error") or {}).get("code") or "list_failed")
        elif provider == "gmail":
            now_ts = now_local().timestamp()
            if backoff_until_ts > now_ts and cache_age_hours <= 24.0:
                in_timeout_backoff = True
            else:
                l_res = runner(["list", "--account", acc_id, "--limit", "5"], timeout=8, domestic=False)
                if l_res.get("ok"):
                    live_ok = True
                    if backoff_st:
                        db.set_meta(backoff_key, json.dumps({"fails": 0, "until_ts": 0}, ensure_ascii=False))
                    for m in (l_res.get("result") or {}).get("messages") or []:
                        if isinstance(m, dict) and m.get("id"):
                            by_id[str(m["id"])] = m
                else:
                    live_err = str((l_res.get("error") or {}).get("code") or "oauth_expired")
                    if live_err == "timeout":
                        fails = int(backoff_st.get("fails") or 0) + 1
                        cool_sec = min(3600, 900 * fails)
                        db.set_meta(
                            backoff_key,
                            json.dumps({"fails": fails, "until_ts": now_ts + cool_sec, "last_err": "timeout"}, ensure_ascii=False),
                        )
                        # 单次/偶发网络抖动超时且本地缓存新鲜(<24h)：先静默退避用缓存，连续 >=3 次超时才升级异常
                        if fails < 3 and cache_age_hours <= 24.0:
                            in_timeout_backoff = True
                            live_err = ""

        # 2. 合并 ~/.cache/omamail 桌面缓存摘要
        for cm in (c_entry.get("messages") or [])[:max_per_account]:
            mid_s = str(cm.get("id") or "")
            if mid_s and mid_s not in by_id:
                by_id[mid_s] = cm

        # 独立单箱判活：显式认证失效或连续异常绝不用旧缓存掩盖成假绿；单次网络抖动超时启用退避回落缓存
        if live_err:
            err_state = "AUTH_EXPIRED" if any(k in live_err.lower() for k in ("auth", "oauth", "token", "login")) else "ERROR"
            db.mark_source_error(per_src_id, err_state, f"{email_addr}: {live_err}")
            account_statuses.append(f"{label}=异常({live_err})")
            degraded_accounts.append(f"{label}:{live_err}")
        elif live_ok:
            db.mark_source_ok(per_src_id, detail=f"{email_addr}: live ok ({len(by_id)} msgs)")
            account_statuses.append(f"{label}=OK")
        elif in_timeout_backoff and cache_age_hours <= 24.0:
            cache_iso = datetime.fromtimestamp(float(c_entry["at_ms"]) / 1000.0, tz=SHANGHAI_TZ).isoformat(timespec="seconds")
            db.mark_source_ok(per_src_id, detail=f"{email_addr}: timeout backoff cache ({cache_age_hours:.1f}h)", ok_at=cache_iso)
            account_statuses.append(f"{label}=OK(缓存退避)")
        elif cache_age_hours <= 24.0:
            cache_iso = datetime.fromtimestamp(float(c_entry["at_ms"]) / 1000.0, tz=SHANGHAI_TZ).isoformat(timespec="seconds")
            db.mark_source_ok(per_src_id, detail=f"{email_addr}: cache fresh ({cache_age_hours:.1f}h)", ok_at=cache_iso)
            account_statuses.append(f"{label}=OK")
        else:
            db.mark_source_error(per_src_id, "ERROR", f"{email_addr}: cache stale ({cache_age_hours:.0f}h)")
            account_statuses.append(f"{label}=缓存陈旧")
            degraded_accounts.append(f"{label}:stale")

        # 2.5 合并上轮因超时/离线暂挂重试的关键账单/发票邮件摘要，防止新邮件涌入将其挤出最新 25 封窗口！
        retry_meta_key = f"omamail_retry_items:{acc_id}"
        prev_retry_map: dict[str, dict[str, Any]] = {}
        raw_retry = db.get_meta(retry_meta_key)
        if raw_retry:
            try:
                for rm in json.loads(raw_retry):
                    if isinstance(rm, dict) and rm.get("id"):
                        rm_id = str(rm["id"])
                        prev_retry_map[rm_id] = rm
                        by_id.setdefault(rm_id, rm)
            except Exception:
                prev_retry_map = {}
        next_retry_map: dict[str, dict[str, Any]] = {}

        # 按旧到新处理待扫邮件（含暂挂重试项）
        ordered = list(by_id.values())[: max_per_account + len(prev_retry_map)]
        ordered.reverse()

        for item in ordered:
            mid = str(item.get("id") or "")
            if not mid:
                continue
            fp = f"omamail:{acc_id}:{mid}"
            if fp in seen_fps:
                continue

            subj = str(item.get("subject") or "").strip()
            snippet = str(item.get("snippet") or "").strip()
            sender = _format_sender(item.get("from"))
            msg_date = _msg_iso_date(item)

            # 第一道防线：验证码邮件零字节落盘
            if contains_otp(f"{subj} {snippet}"):
                otp_dropped += 1
                continue

            body_text = snippet
            links: list[dict[str, Any]] = []
            att_names: list[str] = []
            att_objs: list[dict[str, Any]] = []

            # 对命中发票/账单/续费关键词的新邮件调用只读 `omamail read`；若暂时超时/掉线则持久化进 retry_map 下轮必补读！
            is_actionable = _is_actionable_summary(subj, snippet, sender)
            can_deep_read = not (provider == "gmail" and not live_ok)
            if is_actionable:
                if not can_deep_read:
                    tries = int(item.get("_retry_cnt") or 0) + 1
                    if tries <= 24:
                        next_retry_map[mid] = {**item, "_retry_cnt": tries}
                    continue
                rd = runner(["read", "--account", acc_id, mid], timeout=14, domestic=is_domestic)
                if rd.get("ok"):
                    msg_obj = (rd.get("result") or {}).get("message") or {}
                    native = msg_obj.get("nativeContent") or {}
                    body_dict = native.get("body") or {}
                    if isinstance(body_dict, dict) and body_dict.get("text"):
                        body_text = str(body_dict["text"])
                    links = list(msg_obj.get("links") or [])
                    seen_att_ids: set[str] = set()
                    for a_it in (msg_obj.get("attachments") or native.get("attachments") or []):
                        if isinstance(a_it, dict) and a_it.get("filename"):
                            aid = str(a_it.get("attachmentId") or a_it.get("filename"))
                            if aid in seen_att_ids:
                                continue
                            seen_att_ids.add(aid)
                            att_names.append(str(a_it["filename"]))
                            att_objs.append(a_it)
                else:
                    err_code = str((rd.get("error") or {}).get("code") or "")
                    tries = int(item.get("_retry_cnt") or 0) + 1
                    if err_code != "not_found" and tries <= 12:
                        next_retry_map[mid] = {**item, "_retry_cnt": tries}
                        continue

            # 全量存储范围（[:20000]）做完整 OTP 二次拦截，杜绝长邮件末尾藏验证码入库
            stored_body = body_text[:20000]
            if contains_otp(f"{subj}\n{stored_body}"):
                otp_dropped += 1
                continue

            staged_pdfs: list[str] = []
            staged_bills: list[str] = []
            hay_low = f"{subj} {snippet} {sender}".lower()

            if any(k in hay_low for k in INVOICE_KEYWORDS) and links:
                staged_pdfs.extend(_download_invoice_links(db, links))

            # 计算邮件年龄（仅对近 48 小时内到达的新账单附件触发解密提示，防止历史旧件打扰）
            msg_age_hours = 0.0
            try:
                m_dt = datetime.fromisoformat(msg_date.replace("Z", "+00:00")).astimezone(SHANGHAI_TZ)
                msg_age_hours = max(0.0, (now_local() - m_dt).total_seconds() / 3600.0)
            except Exception:
                msg_age_hours = 0.0

            # 处理邮件直挂附件：1) 滴滴/美团电子发票与行程报销单 PDF 自动落盘配对；2) 支付宝/微信/美团导出账单包自动暂存
            for a_obj in att_objs[:6]:
                fname = str(a_obj.get("filename") or "").strip()
                att_id = str(a_obj.get("attachmentId") or "").strip()
                fsize = int(a_obj.get("size") or 0)
                ext_low = Path(fname).suffix.lower()
                if not fname or not att_id or fsize > 28 * 1024 * 1024:
                    continue

                is_bill_archive = ext_low in (".zip", ".csv", ".xlsx") and any(
                    k in (hay_low + " " + fname.lower()) for k in (*BILL_ARCHIVE_KEYWORDS, *STATEMENT_KEYWORDS)
                )
                is_invoice_pdf = ext_low == ".pdf" and (
                    any(k in (hay_low + " " + fname.lower()) for k in INVOICE_KEYWORDS)
                    and not is_bill_archive
                )

                if is_invoice_pdf:
                    att_bytes = _fetch_omamail_attachment_bytes(
                        runner, provider, acc_id, mid, att_id, domestic=is_domestic
                    )
                    if att_bytes and att_bytes.startswith(b"%PDF-"):
                        out_pdf = stage_invoice_attachment(db, fname, att_bytes)
                        staged_pdfs.append(str(out_pdf))
                        # 若为滴滴/高德行程报销单 PDF，顺手将其中的行程路线富化到账本同额打车记录
                        if "行程" in fname or "行程" in subj:
                            try:
                                from siming.ledger.export_zip import import_bill_archive

                                import_bill_archive(db, out_pdf, delete_source_archive=False)
                            except Exception:
                                pass

                elif is_bill_archive and msg_age_hours <= 48.0 and is_module_enabled(db, "staged_bill_mail"):
                    att_bytes = _fetch_omamail_attachment_bytes(
                        runner, provider, acc_id, mid, att_id, domestic=is_domestic
                    )
                    if not att_bytes:
                        continue
                    s_dir = db.paths.tmp_root / "staged-bills"
                    s_dir.mkdir(parents=True, exist_ok=True)
                    os.chmod(s_dir, 0o700)
                    sha10 = hashlib.sha256(att_bytes).hexdigest()[:10]
                    safe_fn = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", Path(fname).name)
                    staged_arc = s_dir / f"{sha10}-{safe_fn}"
                    fd_b = os.open(str(staged_arc), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                    try:
                        os.write(fd_b, att_bytes)
                    finally:
                        os.close(fd_b)
                    staged_bills.append(str(staged_arc))

                    from siming.ledger.export_zip import import_bill_archive

                    imp = import_bill_archive(db, staged_arc, password=None, delete_source_archive=True)
                    if imp.get("ok"):
                        with db.transaction() as conn_b:
                            db.enqueue_effect(
                                conn_b,
                                "send_qq",
                                {
                                    "ref_id": f"bill:{sha10}",
                                    "rule_id": "staged_bill_mail",
                                    "text": (
                                        f"✅ 邮箱账单附件 `{safe_fn}` 已自动对账入库（原临时包已销毁）\n"
                                        f"• 新增入账：{imp['ingested']} 笔 · 自动配对：{imp['matched']} 笔 · 待核实：{imp['ambiguous']} 笔"
                                    ),
                                },
                            )
                    elif imp.get("needs_password"):
                        db.set_meta("pending_bill_archive", str(staged_arc))
                        db.set_meta("pending_bill_archive_ts", iso_now())
                        db.set_meta("pending_bill_archive_tries", "0")
                        with db.transaction() as conn_b:
                            db.enqueue_effect(
                                conn_b,
                                "send_qq",
                                {
                                    "ref_id": f"bill:{sha10}",
                                    "rule_id": "staged_bill_mail",
                                    "sub_meta": {"kind": "staged_bill", "archive_path": str(staged_arc)},
                                    "text": (
                                        f"🔐 邮箱收到加密账单附件：`{safe_fn}`（来自 {label}）\n"
                                        "请在 30 分钟内直接回一句 `解压码 XXXXXX`（或引用本条直接回密码），司命即刻解密对账并彻底销毁原包。"
                                    ),
                                },
                            )

            invoices_staged += len(staged_pdfs)
            pairing_hint = ""
            if staged_pdfs and any(k in hay_low for k in ("滴滴", "高德", "行程", "出行")):
                try:
                    ym_msg = msg_date[:7] if len(msg_date) >= 7 else ""
                    b_sum = summarize_reimbursement_bundle(db, year_month=ym_msg)
                    if b_sum["missing_itinerary_invoices"]:
                        pairing_hint = f"当月尚有 {len(b_sum['missing_itinerary_invoices'])} 张打车发票缺行程报销单"
                    elif b_sum["missing_invoice_itineraries"]:
                        pairing_hint = f"当月尚有 {len(b_sum['missing_invoice_itineraries'])} 份行程报销单缺电子发票"
                except Exception:
                    pairing_hint = ""

            raw_ref = store_encrypted_raw(
                f"Account: {email_addr}\nSubject: {subj}\nFrom: {sender}\nDate: {msg_date}\n\n{stored_body}",
                db.paths,
            )

            with db.transaction() as conn:
                ev_id = db.append_event(
                    conn,
                    source="mail163",
                    thread_key=f"mail:{acc_id}:{mid}",
                    summary=f"[{label}] {subj[:80]}",
                    payload={
                        "account": email_addr,
                        "account_id": acc_id,
                        "message_id": mid,
                        "sender": sender,
                        "date": msg_date,
                        "attachments": att_names,
                        "staged_pdfs": staged_pdfs,
                        "staged_bills": staged_bills,
                        "pairing_hint": pairing_hint,
                    },
                    raw_ref=raw_ref,
                    occurred_at=msg_date,
                    fingerprint=fp,
                )
            if ev_id is None:
                seen_fps.add(fp)
                continue

            seen_fps.add(fp)
            ingested_total += 1

            # 账单解析（仅对真正含对账单/信用卡账单特征的主题）
            if any(k in subj.lower() for k in STATEMENT_KEYWORDS):
                st_res = try_ingest_statement_text(db, subject=subj, content=body_text, event_id=ev_id)
                if st_res.get("matched"):
                    statements_matched += 1

            # 订阅自动续期预警解析
            if any(k in f"{subj} {snippet}".lower() for k in RENEWAL_KEYWORDS):
                c_renew = _try_ingest_subscription_renewal(
                    db, subject=subj, body_text=body_text, msg_date_iso=msg_date, event_id=ev_id
                )
                if c_renew:
                    renewals_tracked += 1

        if next_retry_map != prev_retry_map:
            kept = list(next_retry_map.values())[-40:]
            db.set_meta(retry_meta_key, json.dumps(kept, ensure_ascii=False))

    detail_str = (
        f"omamail({', '.join(account_statuses)}) | new={ingested_total}, "
        f"invoices={invoices_staged}, renewals={renewals_tracked}"
    )
    cursor_payload = json.dumps(
        {
            "via": "omamail",
            "accounts": account_statuses,
            "updated_at": iso_now(),
        },
        ensure_ascii=False,
    )
    if degraded_accounts:
        db.mark_source_error(
            "mail163",
            "DEGRADED",
            f"部分邮箱异常({', '.join(degraded_accounts)}) | {detail_str}",
        )
    else:
        db.mark_source_ok(
            "mail163",
            cursor=cursor_payload,
            detail=detail_str,
        )
    return {
        "ok": not degraded_accounts,
        "available": True,
        "via": "omamail",
        "accounts": account_statuses,
        "degraded_accounts": degraded_accounts,
        "ingested": ingested_total,
        "invoices_staged": invoices_staged,
        "renewals_tracked": renewals_tracked,
        "statements_matched": statements_matched,
        "otp_dropped": otp_dropped,
    }
