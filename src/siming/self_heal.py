"""self_heal_ops：API 付款锁与自动充值断档哨兵 + AI 研发链路唯一根因合成诊断与有限白名单自愈。
真实故障模型对齐：
1. Nous / AI API 付款锁：卡余额不足后先报 `insufficient funds`，几次后触发账户级锁 `Payments are temporarily unavailable` 并关停 `AUTO REFILL`（发 `Auto-refill Payment Disabled` 邮件）；**卡里有钱后开关不会自动重开**，必须「补款 -> 手动 Purchase 一笔 -> 重开 AUTO REFILL」三步恢复；
2. 内网 VPN + sing-box DNS 负缓存：内网地址直连通但走 `127.0.0.1:7890` 代理重置 = 连 VPN 前的 `NXDOMAIN` 留在 sing-box 缓存，白名单安全自愈为 `POST http://127.0.0.1:9090/cache/dns/flush`；
3. 根因合成：若 `policy.yaml` 配置的内网探针主机无法解析且远端主机不可达，合成为唯一根因「内网 VPN 未在线」，绝不同时轰炸多条下游报错；若 `127.0.0.1:9222` 被其他 Electron 客户端抢占，明确指出端口身份并确认 `[::1]:9222` 回退可用。
"""

from __future__ import annotations

import json
import os
import re
import socket
import subprocess
from datetime import timedelta
from typing import Any, Callable, Optional

import httpx

from siming import db as _db_mod
from siming.cdp import inspect_url_in_background_tab
from siming.config import load_policy
from siming.db import SimingDB, iso_now, now_local, parse_iso
from siming.modules import is_module_enabled
from siming.quota import fetch_ai_quota, select_coding_runner
from siming.watchdog import check_cdp_9222_status

NOUS_BILLING_URL = ""

_NOUS_LOCK_OR_FAIL_RE = re.compile(
    r"(auto-refill\s+payment\s+disabled|auto-refill\s+failed|insufficient\s+funds|payments\s+are\s+temporarily\s+unavailable|payment\s+failed|action\s+required.*nous|declined.*nous)",
    re.IGNORECASE,
)
_NOUS_RECOVERY_RE = re.compile(
    r"(auto-refill\s+successful|purchase\s+confirmation|your\s+nous\s+research\s+inc\.\s+receipt)",
    re.IGNORECASE,
)


def _occ_dt(occ: str):
    """邮件 occurred_at 混有 UTC `...Z` 与 `+08:00` 两种格式，统一解析成本地时区再比较（不能直接比字符串）。"""
    try:
        return parse_iso(occ.replace("Z", "+00:00")) if occ else None
    except Exception:
        return None


def _notify_transient_nous_failures(
    db: SimingDB,
    fail_events: list[dict[str, Any]],
    ok_events: list[dict[str, Any]],
    notify: bool,
) -> list[str]:
    """2026-11-23 用户纠错：扣款失败后 6 分钟自动充值又成功了，哨兵判「健康」就一声不吭。

    现在每一封新的扣款失败邮件（24h 内、未提醒过）都单独告知一次：
    - 已被后续成功收据覆盖 → 普通级信息（不用操作，但提示查卡额度与近 24h 自动充值次数）；
    - 未恢复的由外层付款锁预警卡负责（urgent），这里不重复发。
    """
    if not fail_events:
        return []
    raw = db.get_meta("nous_fail_alerted_ids") or "[]"
    try:
        alerted: list[str] = [str(x) for x in json.loads(raw)]
    except Exception:
        alerted = []
    now = _db_mod.now_local()
    sent: list[str] = []
    for fe in fail_events:
        fdt = fe.get("dt")
        if fe["event_id"] in alerted or fdt is None or (now - fdt) > timedelta(hours=24):
            continue
        recovered = [o for o in ok_events if o.get("dt") is not None and o["dt"] > fdt]
        if not recovered:
            continue  # 未恢复：交给付款锁预警卡（urgent）
        first_ok = min(recovered, key=lambda o: o["dt"])
        gap_min = max(1, round((first_ok["dt"] - fdt).total_seconds() / 60))
        refills_24h = sum(
            1
            for o in ok_events
            if o.get("dt") is not None and (now - o["dt"]) <= timedelta(hours=24) and "auto-refill" in o["summary"].lower()
        )
        text = (
            f"⚠️ Nous 自动充值扣款失败过一次（{fdt.strftime('%m-%d %H:%M')}：{fe['summary'][:60]}）\n"
            f"• {gap_min} 分钟后自动充值已成功（{first_ok['dt'].strftime('%H:%M')}），账户目前正常，无需操作。\n"
            f"• 近 24 小时自动充值 {refills_24h} 次；连续失败会触发 Nous 付款锁并关掉 AUTO REFILL，建议看一眼绑定信用卡额度。"
        )
        if not (notify and is_module_enabled(db, "self_heal_ops")):
            continue  # 只读诊断（notify=False）不消耗「已提醒」标记
        with db.transaction() as c:
            db.enqueue_effect(
                c,
                "send_qq",
                {"ref_id": f"nousfail:{fe['event_id']}", "rule_id": "nous_payment_fail", "urgent": False, "text": text},
            )
        alerted.append(fe["event_id"])
        sent.append(fe["event_id"])
    if sent:
        db.set_meta("nous_fail_alerted_ids", json.dumps(alerted[-200:]))
    return sent


def evaluate_nous_billing_sentinel(
    db: SimingDB,
    notify: bool = True,
    portal_probe: Optional[Callable[[], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """扫描 Gmail/Omamail 事件流（及可选 Portal 只读快照），闭环守护 Nous 付款锁与 AUTO REFILL 状态。"""
    db.init_db()
    conn = db.connect()
    try:
        rows = conn.execute(
            """
            SELECT event_id, summary, payload_json, occurred_at
            FROM events
            WHERE source = 'mail163'
              AND (lower(summary) LIKE '%nous%' OR lower(payload_json) LIKE '%nousresearch%')
            ORDER BY ingested_at DESC
            LIMIT 120
            """
        ).fetchall()
    finally:
        conn.close()

    latest_fail: Optional[dict[str, str]] = None
    latest_ok: Optional[dict[str, str]] = None
    fail_events: list[dict[str, Any]] = []
    ok_events: list[dict[str, Any]] = []

    for r in rows:
        subj = str(r["summary"] or "")
        payload_s = str(r["payload_json"] or "")
        # 跳过 GitHub Actions NousResearch/hermes-agent CI 邮件，只看账单/收据邮件
        if "hermes-agent" in subj.lower() or "run failed:" in subj.lower():
            continue
        hay = f"{subj} {payload_s}"
        occ = str(r["occurred_at"] or "")
        rec = {"event_id": str(r["event_id"]), "summary": subj, "occurred_at": occ, "dt": _occ_dt(occ)}
        if _NOUS_LOCK_OR_FAIL_RE.search(hay):
            fail_events.append(rec)
        elif _NOUS_RECOVERY_RE.search(hay):
            ok_events.append(rec)

    _far = now_local() - timedelta(days=3650)
    fail_events.sort(key=lambda x: x["dt"] or _far, reverse=True)
    ok_events.sort(key=lambda x: x["dt"] or _far, reverse=True)
    if fail_events:
        latest_fail = {k: fail_events[0][k] for k in ("event_id", "summary", "occurred_at")}
    if ok_events:
        latest_ok = {k: ok_events[0][k] for k in ("event_id", "summary", "occurred_at")}
    transient_alerted = _notify_transient_nous_failures(db, fail_events, ok_events, notify)

    portal_info: dict[str, Any] = {}
    if portal_probe is not None:
        try:
            portal_info = portal_probe() or {}
        except Exception as exc:
            portal_info = {"ok": False, "error": str(exc)[:160]}

    mail_locked = bool(
        fail_events
        and (
            not ok_events
            or (fail_events[0]["dt"] is not None and ok_events[0]["dt"] is not None and fail_events[0]["dt"] > ok_events[0]["dt"])
        )
    )
    portal_locked = bool(
        portal_info.get("ok")
        and (
            portal_info.get("payment_unavailable")
            or portal_info.get("auto_refill_enabled") is False
            or (
                isinstance(portal_info.get("balance_usd"), (int, float))
                and float(portal_info["balance_usd"]) < 1.5
                and portal_info.get("auto_refill_enabled") is False
            )
        )
    )
    is_locked_or_disabled = mail_locked or portal_locked

    conn = db.connect()
    try:
        open_case = conn.execute(
            "SELECT case_id, title FROM cases WHERE rule_id='nous_lock' AND status='待处理' ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()

    ts = iso_now()
    case_id = str(open_case["case_id"]) if open_case else ""

    if is_locked_or_disabled:
        trigger_key = (
            (latest_fail or {}).get("event_id")
            or f"portal:{portal_info.get('auto_refill_enabled')}:{portal_info.get('balance_usd')}"
        )
        last_alerted = db.get_meta("nous_lock_last_alerted_key") or ""
        bal_note = (
            f"（Portal 实时余额 ${float(portal_info['balance_usd']):.2f} · AUTO REFILL={'开启' if portal_info.get('auto_refill_enabled') else '已关闭'}）"
            if portal_info.get("ok") and portal_info.get("balance_usd") is not None
            else ""
        )
        billing_url = str((load_policy(db.paths).get("self_heal") or {}).get("nous_billing_url") or NOUS_BILLING_URL or "https://portal.nousresearch.com/").strip()
        card_text = (
            f"🚨 **Nous 付款锁 / 自动充值断档预警**{bal_note}\n"
            f"• 触发线索：{(latest_fail or {}).get('summary') or 'Portal 检测到付款受限或 AUTO REFILL 被关闭'}\n"
            "• ⚠️ 历史铁律：即使卡里充了钱，Nous 账户锁与 `AUTO REFILL` 开关也**不会自己恢复**！\n"
            "• **三步恢复清单**（请勿让脚本代扣款）：\n"
            "  1️⃣ 确认绑定信用卡可用额度充足；\n"
            f"  2️⃣ 打开 `{billing_url}` 手动充值一笔（收到 Purchase confirmation 解锁）；\n"
            "  3️⃣ 在同一账单页亲手把 `AUTO REFILL` 开关重新拨回开启（司命收到新收据会自动核销本单）。"
        )
        if not case_id:
            with db.transaction() as c:
                case_id = db.next_seq_id(c, "S")
                c.execute(
                    """
                    INSERT INTO cases (
                        case_id, title, next_step, due_at, done_rule, status, strength,
                        category, rule_id, evidence_ids, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, '收到新充值收据且重开 AUTO REFILL', '待处理', 'HARD', 'billing_lock', 'nous_lock', ?, ?, ?)
                    """,
                    (
                        case_id,
                        "Nous 付款锁与自动充值断档待恢复",
                        "补足卡额度 -> Portal 手动充一笔 -> 重新打开 AUTO REFILL 开关",
                        now_local().date().isoformat(),
                        json.dumps([trigger_key]),
                        ts,
                        ts,
                    ),
                )
                if notify and is_module_enabled(db, "self_heal_ops") and trigger_key != last_alerted:
                    db.enqueue_effect(
                        c,
                        "send_qq",
                        {
                            "ref_id": case_id,
                            "rule_id": "nous_lock",
                            "urgent": True,
                            "bypass_quiet": False,
                            "text": f"[{case_id}] {card_text}",
                        },
                    )
                    db.set_meta("nous_lock_last_alerted_key", trigger_key, conn=c)
        elif notify and is_module_enabled(db, "self_heal_ops") and trigger_key != last_alerted:
            with db.transaction() as c:
                db.enqueue_effect(
                    c,
                    "send_qq",
                    {
                        "ref_id": case_id,
                        "rule_id": "nous_lock",
                        "urgent": True,
                        "text": f"[{case_id}] {card_text}",
                    },
                )
                db.set_meta("nous_lock_last_alerted_key", trigger_key, conn=c)

        return {
            "healthy": False,
            "locked": True,
            "case_id": case_id,
            "latest_fail": latest_fail,
            "latest_ok": latest_ok,
            "portal": portal_info,
            "transient_alerted": transient_alerted,
            "summary": card_text,
        }

    # 若此前挂着未结的 nous_lock 单据，且现已收到更新的成功收据（且若查了 portal 也确认开关已开），自动销账报喜！
    if case_id and latest_ok and (not portal_info.get("ok") or portal_info.get("auto_refill_enabled") is not False):
        with db.transaction() as c:
            c.execute(
                "UPDATE cases SET status='已完成', closed_at=?, state_version=state_version+1, updated_at=? WHERE case_id=?",
                (ts, ts, case_id),
            )
            if notify and is_module_enabled(db, "self_heal_ops"):
                db.enqueue_effect(
                    c,
                    "send_qq",
                    {
                        "ref_id": case_id,
                        "rule_id": "nous_lock",
                        "text": f"✅ [{case_id}] Nous 付款已确认恢复（最新收据：{latest_ok['summary']}），预警单已自动销账。",
                    },
                )
        case_id = ""

    ok_note = latest_ok["summary"] if latest_ok else "近期无扣款失败/锁号告警"
    if portal_info.get("ok"):
        b_s = f"${float(portal_info['balance_usd']):.2f}" if portal_info.get("balance_usd") is not None else "未知"
        sw_s = "开启" if portal_info.get("auto_refill_enabled") else ("关闭⚠️" if portal_info.get("auto_refill_enabled") is False else "未知")
        ok_note = f"Portal 余额 {b_s} · AUTO REFILL={sw_s}（{ok_note}）"

    return {
        "healthy": True,
        "locked": False,
        "case_id": case_id,
        "latest_fail": latest_fail,
        "latest_ok": latest_ok,
        "portal": portal_info,
        "transient_alerted": transient_alerted,
        "summary": f"🟢 Nous 账单状态正常：{ok_note}",
    }


def probe_nous_portal_cdp(port: int = 9222) -> dict[str, Any]:
    """通过主 Chromium 后台静默标签只读读取 Nous Portal 余额与 AUTO REFILL 开关（绝不点击充值或触发扣费）。"""
    js = r"""
    (() => {
      const txt = (document.body && document.body.innerText) || '';
      const sw = document.querySelector('[role="switch"]');
      const aria = sw ? sw.getAttribute('aria-checked') : null;
      const mBal = txt.match(/\$\s*([0-9]+(?:\.[0-9]{1,2})?)/);
      const unavail = /Payments are temporarily unavailable/i.test(txt);
      return JSON.stringify({
        balance_usd: mBal ? parseFloat(mBal[1]) : null,
        auto_refill_enabled: aria === 'true' ? true : (aria === 'false' ? false : null),
        payment_unavailable: unavail,
        snippet: txt.slice(0, 300)
      });
    })()
    """
    res = inspect_url_in_background_tab(NOUS_BILLING_URL, js, port=port, wait_sec=2.4, close_when_clean=True)
    if not res.get("ok"):
        return {"ok": False, "gated": bool(res.get("gated")), "error": res.get("error") or (res.get("gate") or {}).get("reason", "unavailable")}
    try:
        parsed = json.loads(str(res.get("data") or "{}"))
    except Exception:
        parsed = {}
    return {"ok": True, **parsed}


def _tcp_alive(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _default_net_probe(policy: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """探测本机 7890 代理、可选内网域名 DNS/直连/代理连通性以及 herdr 远端机器可达性。"""
    if os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return {
            "proxy_7890_up": True,
            "clash_api_9090_up": True,
            "oa_dns_ok": True,
            "oa_direct_ok": True,
            "oa_proxy_ok": True,
            "remote_reachable": True,
            "remote_detail": "offline-mock",
        }

    sh_cfg = (policy or {}).get("self_heal") or {}
    probe_host = str(sh_cfg.get("intranet_probe_host") or "").strip()
    remote_kws = tuple(
        str(k).strip().lower()
        for k in (sh_cfg.get("remote_host_keywords") or ("gpu", "remote"))
        if str(k).strip()
    ) or ("gpu", "remote")

    proxy_up = _tcp_alive("127.0.0.1", 7890, timeout=0.5)
    clash_up = _tcp_alive("127.0.0.1", 9090, timeout=0.5)

    oa_dns_ok = True
    oa_direct_ok = True
    oa_proxy_ok = True
    if probe_host:
        oa_dns_ok = False
        oa_direct_ok = False
        oa_proxy_ok = False
        try:
            g_proc = subprocess.run(["getent", "hosts", probe_host], capture_output=True, text=True, timeout=1.5)
            oa_dns_ok = g_proc.returncode == 0 and bool((g_proc.stdout or "").strip())
        except Exception:
            oa_dns_ok = False

        if oa_dns_ok:
            probe_url = probe_host if probe_host.startswith(("http://", "https://")) else f"https://{probe_host}"
            try:
                with httpx.Client(trust_env=False, timeout=2.0, verify=False) as c_dir:
                    r_d = c_dir.get(probe_url, follow_redirects=False)
                    oa_direct_ok = r_d.status_code < 500
            except Exception:
                oa_direct_ok = False

            if proxy_up:
                try:
                    with httpx.Client(proxy="http://127.0.0.1:7890", trust_env=False, timeout=2.2, verify=False) as c_prx:
                        r_p = c_prx.get(probe_url, follow_redirects=False)
                        oa_proxy_ok = r_p.status_code < 500
                except Exception:
                    oa_proxy_ok = False

    remote_reachable: Optional[bool] = None
    remote_detail = "未配置"
    if os.path.isfile("/usr/bin/herdr"):
        try:
            m_proc = subprocess.run(["/usr/bin/herdr", "machine", "list"], capture_output=True, text=True, timeout=2.0)
            out_m = (m_proc.stdout or "").strip()
            target_machine = ""
            if m_proc.returncode == 0 and out_m.startswith("{"):
                m_data = json.loads(out_m)
                for m in (m_data.get("result") or {}).get("machines") or []:
                    if isinstance(m, dict) and any(k in str(m).lower() for k in remote_kws):
                        target_machine = str(m.get("name") or "remote")
                        st = str(m.get("status") or m.get("state") or "").lower()
                        if st:
                            remote_reachable = st in ("connected", "reachable", "online", "ready", "ok")
                            remote_detail = f"herdr:{target_machine}={st}"
                        break
            elif m_proc.returncode == 0 and out_m:
                # 兼容 herdr machine list TSV 输出：<id>\t<name>\t<host>\t<group>\t<enabled>
                for ln in out_m.splitlines():
                    cols = ln.split("\t")
                    if len(cols) >= 2 and any(k in ln.lower() for k in remote_kws):
                        target_machine = cols[1].strip()
                        break

            if target_machine and remote_reachable is None:
                if probe_host and not oa_dns_ok:
                    remote_reachable = False
                    remote_detail = f"herdr:{target_machine}=受内网VPN离线阻断"
                else:
                    try:
                        st_proc = subprocess.run(
                            ["/usr/bin/herdr", "machine", "status", target_machine],
                            capture_output=True,
                            text=True,
                            timeout=2.5,
                        )
                        st_out = ((st_proc.stdout or "") + " " + (st_proc.stderr or "")).strip().lower()
                        remote_reachable = st_proc.returncode == 0 and any(
                            k in st_out for k in ("reachable", "connected", "online", "ready", "ok")
                        )
                        remote_detail = f"herdr:{target_machine}={'reachable' if remote_reachable else 'unreachable'}"
                    except subprocess.TimeoutExpired:
                        remote_reachable = False
                        remote_detail = f"herdr:{target_machine}=timeout"
        except Exception:
            pass

    return {
        "proxy_7890_up": proxy_up,
        "clash_api_9090_up": clash_up,
        "oa_dns_ok": oa_dns_ok,
        "oa_direct_ok": oa_direct_ok,
        "oa_proxy_ok": oa_proxy_ok,
        "remote_reachable": remote_reachable,
        "remote_detail": remote_detail,
    }


def _flush_singbox_dns_cache() -> tuple[bool, str]:
    """白名单自愈动作：调用 Clash API `POST http://127.0.0.1:9090/cache/dns/flush` 清除连 VPN 前残留的 NXDOMAIN 负缓存。"""
    try:
        with httpx.Client(trust_env=False, timeout=2.0) as client:
            resp = client.post("http://127.0.0.1:9090/cache/dns/flush")
            if resp.status_code in (200, 204):
                return True, "POST 127.0.0.1:9090/cache/dns/flush 成功"
            return False, f"HTTP {resp.status_code}"
    except Exception as exc:
        return False, str(exc)[:120]


def diagnose_and_heal_dev_links(
    db: SimingDB,
    allow_heal: bool = True,
    probe_nous_portal: bool = False,
    net_probe_fn: Optional[Callable[[], dict[str, Any]]] = None,
    dns_flush_fn: Optional[Callable[[], tuple[bool, str]]] = None,
    cdp_check_fn: Optional[Callable[[], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """合成诊断 AI 研发与内网全链路（配额余量 + API 付款锁 + 9222 端口身份 + VPN/sing-box DNS + 远端主机），输出唯一根因并执行安全白名单自愈。"""
    db.init_db()
    pol = load_policy(db.paths)
    net = (net_probe_fn or (lambda: _default_net_probe(pol)))()
    healed_actions: list[str] = []

    # 白名单自愈：内网直连正常但走 7890 代理失败 -> 自动刷清 sing-box 旧 NXDOMAIN 负缓存并重验
    if allow_heal and net.get("oa_dns_ok") and net.get("oa_direct_ok") and net.get("proxy_7890_up") and not net.get("oa_proxy_ok"):
        flushed, f_detail = (dns_flush_fn or _flush_singbox_dns_cache)()
        if flushed:
            re_net = (net_probe_fn or (lambda: _default_net_probe(pol)))()
            net.update(re_net)
            healed_msg = (
                "🩹 已自动清空 sing-box DNS 负缓存（`127.0.0.1:9090/cache/dns/flush`），内网地址经 7890 代理已恢复连通"
                if net.get("oa_proxy_ok")
                else f"🩹 已执行 sing-box DNS 缓存清理（{f_detail}），请重刷页面确认"
            )
            healed_actions.append(healed_msg)
            db.write_audit("A1", "self_heal_singbox_dns_flush", {"recovered": bool(net.get("oa_proxy_ok"))})

    cdp = (cdp_check_fn or check_cdp_9222_status)()
    quota = fetch_ai_quota(db)
    preferred_runner, _ = select_coding_runner(db=db)
    nous = evaluate_nous_billing_sentinel(
        db,
        notify=False,
        portal_probe=(lambda: probe_nous_portal_cdp()) if probe_nous_portal else None,
    )

    root_causes: list[str] = []
    if nous["locked"]:
        root_causes.append("Nous 账户触发付款锁或 `AUTO REFILL` 被关闭（需手动充值一笔并重拨开关）")

    if not net.get("proxy_7890_up"):
        root_causes.append("本机 `127.0.0.1:7890` (sing-box) 代理未监听，海外 API 与 Gmail OAuth 将受阻")
    elif not net.get("oa_dns_ok"):
        if net.get("remote_reachable") is False:
            root_causes.append(
                "唯一根因：内网 VPN 未在线（内网探针域名无法解析，连带导致远端主机同步不可达；重登 VPN 即可，无需排查 SSH/herdr）"
            )
        else:
            root_causes.append("内网 VPN 未在线（内网探针域名未解析）")
    elif net.get("oa_direct_ok") and not net.get("oa_proxy_ok"):
        root_causes.append("内网直连通但经 `7890` 代理不通（疑似 sing-box DNS 负缓存或分流异常）")
    elif net.get("remote_reachable") is False:
        root_causes.append(f"内网 VPN 在线但远端 `{net.get('remote_detail')}` 未连接（请核查远端实例状态或端口）")

    if cdp.get("atrust_collision_suspect"):
        if cdp.get("main_chromium_up"):
            root_causes.append("`127.0.0.1:9222` 被其他 Electron/VPN 客户端抢占（司命已自动切至 `[::1]:9222` 连接主 Chromium，不影响使用）")
        else:
            root_causes.append("`9222` 端口被其他 Electron/VPN 客户端抢占且主 Chromium 未开启调试口")
    elif not cdp.get("main_chromium_up"):
        root_causes.append("主桌面 Chromium (`9222`) 当前未运行（保价/美团 H5 自动化暂不可用）")

    codex_s = (
        f"{quota['codex_remaining']:.0f}%"
        if isinstance(quota.get("codex_remaining"), (int, float))
        else str(quota.get("codex_label") or "未知")
    )
    claude_s = (
        f"{quota['claude_remaining']:.0f}%"
        if isinstance(quota.get("claude_remaining"), (int, float))
        else ("可用" if quota.get("claude_available") else "不可用")
    )

    lines = ["🩺 **研发链路根因诊断与 AI 额度一表通**"]
    if healed_actions:
        for h in healed_actions:
            lines.append(f"• {h}")

    if root_causes:
        lines.append("• 🎯 **合成根因结论**：")
        for rc in root_causes:
            lines.append(f"  - ⚠️ {rc}")
    else:
        lines.append("• 🟢 **全链路健康**：代理(7890)、内网 VPN、Chromium(9222) 与 API 账单均无异常。")

    lines.append(f"• 🤖 **AI 额度与路由**：当前首选 `{preferred_runner}` ｜ Codex 余量 **{codex_s}** ｜ Claude **{claude_s}**")
    lines.append(f"• 💳 **Nous 状态**：{nous['summary']}")
    lines.append(
        f"• 🌐 **网络与远端**：7890={'OK' if net.get('proxy_7890_up') else 'DOWN'} · "
        f"内网={'直连+代理双通' if (net.get('oa_direct_ok') and net.get('oa_proxy_ok')) else ('仅直连' if net.get('oa_direct_ok') else ('离线' if not net.get('oa_dns_ok') else '异常'))} · "
        f"远端={net.get('remote_detail')} · "
        f"CDP9222={'主Chromium正常' if (cdp.get('main_chromium_up') and not cdp.get('atrust_collision_suspect')) else ('[::1]回退正常(IPv4被其他客户端占)' if cdp.get('main_chromium_up') else '未开')}"
    )

    db.record_human_use("self_heal_ops", "diagnose", ref_id="ops_check")
    return {
        "ok": len(root_causes) == 0,
        "root_causes": root_causes,
        "healed_actions": healed_actions,
        "quota": quota,
        "nous": nous,
        "net": net,
        "cdp": cdp,
        "reply": "\n".join(lines),
    }
