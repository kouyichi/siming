"""本人授权手机号与平台登录短信验证码瞬时保险箱（otp_vault）：
安全与合规四条铁律：
1. 隔离存储：验证码仅存于 81 `otp_inbox`（Fernet 加密、300 秒硬过期、拉取即焚），**永不进入 `events`、`outbox`、`raw/`、QQ 消息或模型上下文日志**；
2. 银行动账/转账隔离：任何涉及银行、银联、转账、信用卡交易的动态码一律在边缘端与本机双重拒收，仅放行生活/工具平台登录码（美团、滴滴、瑞幸、京东等）；
3. 消费即焚与脱敏审计：验证码一旦匹配提取立即通知 81 销毁原记录，审计表只记掩码（如 `12**56`）；
4. 图形验证码合规红线：代填手机号与短信码过程中，若页面弹出滑块/拼图/人机验证码，**立即停手原样保留浏览器后台标签页交由本人滑一下**，绝不绕过或破解人机验证。
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import time
from typing import Any, Callable, Optional

from siming.cdp import (
    LocalWsCdpClient,
    detect_compliance_gate,
    has_domain_session_cookies,
    list_chromium_tabs,
    resolve_chromium_cdp_base,
)
from siming.config import load_policy
from siming.crypto import read_secret, write_secret_file
from siming.db import SimingDB, iso_now, now_local, parse_iso

PHONE_SECRET_NAME = "authorized-phone"
DEFAULT_AUTO_RELOGIN_COOLDOWN_SEC = 6 * 3600

_SLIDER_GATE_RE = re.compile(
    r"(滑动验证|请拖动滑块|向右滑动|完成拼图|智能验证|人机验证|安全验证|点击完成验证|jcap\.m\.jd|jcap|yoda|captcha)",
    re.IGNORECASE,
)

_OTP_EXTRACT_RE = re.compile(
    r"(?:验证码|校验码|动态码|登录码|认证码|code|OTP)[^0-9]{0,14}(?<!\d)(\d{4,8})(?!\d)"
    r"|(?<!\d)(\d{4,8})(?!\d)[^0-9\n]{0,12}(?:验证码|校验码|动态码|登录码)",
    re.IGNORECASE,
)
_BANK_OTP_BLOCK_RE = re.compile(
    r"(工商银行|农业银行|中国银行|建设银行|招商银行|交通银行|光大银行|中信银行|浦发银行|民生银行|平安银行|兴业银行|邮储银行|银联|转账|汇款|快捷支付)"
)

PLATFORM_ALIASES: dict[str, dict[str, Any]] = {
    "meituan": {
        "title": "美团外卖 H5",
        "sms_hints": ("美团", "三快", "大众点评"),
        "login_url": "https://h5.waimai.meituan.com/waimai/mindex/home",
        "login_url_hints": ("passport.meituan.com", "meituan.com"),
        "mobile": True,
        "session_domain": "meituan.com",
        "session_cookies": ("token", "userTicket", "mt_c_token"),
    },
    "didi": {
        "title": "滴滴官方 MCP 平台",
        "sms_hints": ("滴滴", "小桔"),
        "login_url": "https://mcp.didichuxing.com",
        "login_url_hints": ("didichuxing.com",),
        "mobile": False,
        "session_domain": "didichuxing.com",
        "session_cookies": ("token", "ticket"),
    },
    "luckin": {
        "title": "瑞幸开放平台 MCP",
        "sms_hints": ("瑞幸", "luckin"),
        "login_url": "https://open.lkcoffee.com",
        "login_url_hints": ("lkcoffee.com",),
        "mobile": False,
        "session_domain": "lkcoffee.com",
        "session_cookies": ("token",),
    },
    "jd": {
        "title": "京东移动端/价保账号",
        "sms_hints": ("京东", "JD"),
        "login_url": "https://plogin.m.jd.com/login/login?appid=461&returnurl=https%3A%2F%2Fhome.m.jd.com%2FmyJd%2Fnewhome.action",
        "login_url_hints": ("plogin.m.jd.com", "passport.jd.com"),
        "mobile": True,
        "session_domain": "jd.com",
        "session_cookies": ("pt_key", "pt_pin", "thor"),
    },
}


def mask_phone(phone: str) -> str:
    s = re.sub(r"\D", "", phone or "")
    if len(s) == 11:
        return f"{s[:3]}****{s[-4:]}"
    return s[:2] + "****" if len(s) >= 4 else "未配置"


def mask_otp(code: str) -> str:
    c = (code or "").strip()
    if len(c) <= 2:
        return "**"
    return f"{c[0]}**{c[-1]}"


def save_authorized_phone(db: SimingDB, raw_phone: str) -> dict[str, Any]:
    digits = re.sub(r"\D", "", raw_phone or "")
    if digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    if not re.fullmatch(r"1[3-9]\d{9}", digits):
        return {"ok": False, "error": "请输入有效的 11 位中国大陆手机号"}
    write_secret_file(db.paths.secrets_dir / PHONE_SECRET_NAME, digits)
    masked = mask_phone(digits)
    db.write_audit("A1", "save_authorized_phone", {"phone": masked})
    return {
        "ok": True,
        "masked_phone": masked,
        "summary": f"📱 已加密保存本人授权登录手机号 `{masked}`（`0600` 权限）。配合手机短信快捷指令即可授权司命代填短信验证码登录平台！",
    }


def get_authorized_phone(db: SimingDB) -> Optional[str]:
    return read_secret(PHONE_SECRET_NAME, db.paths)


def extract_otp_code_and_brand(sms_text: str) -> Optional[dict[str, str]]:
    """从短信正文提取 4–8 位登录验证码与【签名品牌】（自动拒收银行/银联交易验证码）。"""
    txt = (sms_text or "").strip()
    if not txt or _BANK_OTP_BLOCK_RE.search(txt):
        return None
    m = _OTP_EXTRACT_RE.search(txt)
    if not m:
        return None
    code = m.group(1) or m.group(2) or ""
    if not code:
        return None
    m_brand = re.search(r"【([^】]{1,20})】", txt)
    brand = m_brand.group(1).strip() if m_brand else "未知平台"
    return {"code": code, "brand": brand}


def _default_edge_otp_call(db: SimingDB, consume_id: int = 0) -> dict[str, Any]:
    if os.environ.get("SIMING_DISABLE_SSH_EDGE") == "1" or os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return {"ok": True, "items": []}
    policy = load_policy(db.paths)
    host = str(policy["edge"]["ssh_host"])
    remote_cli = str(policy["edge"]["remote_cli"])
    sub_cmd = f"pull-otp --consume-id {int(consume_id)}" if consume_id > 0 else "pull-otp"
    proc = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, f"{remote_cli} {sub_cmd}"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if proc.returncode != 0:
        return {"ok": False, "error": (proc.stderr or proc.stdout or "").strip()[:180], "items": []}
    try:
        return json.loads(proc.stdout.strip() or "{}")
    except Exception as exc:
        return {"ok": False, "error": str(exc)[:120], "items": []}


def pull_latest_platform_otp(
    db: SimingDB,
    platform_hint: str = "",
    min_created_epoch: int = 0,
    consume: bool = True,
    edge_otp_caller: Optional[Callable[[int], dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    """从 81 瞬时保险箱拉取最新一条匹配的平台验证码，命中后立即远程焚毁。"""
    call = edge_otp_caller or (lambda cid=0: _default_edge_otp_call(db, cid))
    res = call(0)
    if not res.get("ok"):
        return None

    hints: tuple[str, ...] = ()
    p_low = (platform_hint or "").strip().lower()
    if p_low in PLATFORM_ALIASES:
        hints = tuple(PLATFORM_ALIASES[p_low]["sms_hints"])
    elif platform_hint.strip():
        hints = (platform_hint.strip(),)

    for item in res.get("items") or []:
        c_ep = int(item.get("created_epoch") or 0)
        if min_created_epoch and c_ep < min_created_epoch:
            continue
        raw_txt = str(item.get("text") or "")
        parsed = extract_otp_code_and_brand(raw_txt)
        if not parsed:
            continue
        if hints and not any(h.lower() in (raw_txt.lower() + " " + parsed["brand"].lower()) for h in hints):
            continue
        row_id = int(item.get("id") or 0)
        if consume and row_id > 0:
            call(row_id)
        db.write_audit(
            "A1",
            "otp_consumed",
            {
                "platform_hint": platform_hint or "any",
                "brand": parsed["brand"],
                "masked_code": mask_otp(parsed["code"]),
            },
            detail=f"brand={parsed['brand']} code={mask_otp(parsed['code'])}",
        )
        return {
            "id": row_id,
            "code": parsed["code"],
            "brand": parsed["brand"],
            "received_at": str(item.get("received_at") or ""),
        }
    return None


def wait_for_platform_otp(
    db: SimingDB,
    platform_hint: str = "",
    timeout_sec: float = 65.0,
    poll_interval_sec: float = 2.5,
    edge_otp_caller: Optional[Callable[[int], dict[str, Any]]] = None,
) -> Optional[dict[str, Any]]:
    """在触发平台发送短信后，按 2.5 秒间隔轮询 81 瞬时验证码池直至命中或超时。"""
    started_epoch = int(time.time()) - 15
    deadline = time.monotonic() + max(1.0, timeout_sec)
    while time.monotonic() < deadline:
        hit = pull_latest_platform_otp(
            db,
            platform_hint=platform_hint,
            min_created_epoch=started_epoch,
            consume=True,
            edge_otp_caller=edge_otp_caller,
        )
        if hit is not None:
            return hit
        time.sleep(min(poll_interval_sec, max(0.2, deadline - time.monotonic())))
    return None


def resolve_platform_key(raw: str) -> str:
    q = (raw or "").strip().lower()
    if q in ("美团", "美团外卖", "meituan", "mt"):
        return "meituan"
    if q in ("滴滴", "滴滴出行", "didi"):
        return "didi"
    if q in ("瑞幸", "瑞幸咖啡", "luckin"):
        return "luckin"
    if q in ("京东", "jd"):
        return "jd"
    return q


def _close_cdp_tab_safe(ws_browser: str, target_id: str) -> None:
    if not ws_browser or not target_id:
        return
    try:
        with LocalWsCdpClient(ws_browser, timeout=4.0) as b_ws:
            b_ws.call("Target.closeTarget", {"targetId": target_id})
    except Exception:
        pass


def run_authorized_sms_login(
    db: SimingDB,
    platform_raw: str,
    wait_sec_otp: float = 60.0,
    port: int = 9222,
    edge_otp_caller: Optional[Callable[[int], dict[str, Any]]] = None,
    reuse_existing_tab: bool = False,
) -> dict[str, Any]:
    """在主 Chromium 后台打开目标平台登录页，自动填入授权手机号、点击获取短信验证码并从 81 瞬时保险箱取码填入：
    - 若平台弹出滑块/图形验证码，严格遵守红线立即停手并保留后台标签页请用户滑一下；
    - `reuse_existing_tab=True`（由「填验证码 京东」触发）直接复用之前保留的登录标签页，只等新验证码填入提交。
    """
    db.init_db()
    plat = resolve_platform_key(platform_raw)
    spec = PLATFORM_ALIASES.get(plat)
    if not spec:
        return {"ok": False, "reply": f"❌ 暂未内置平台「{platform_raw}」登录入口（支持：美团 / 滴滴 / 瑞幸 / 京东）。"}

    phone = get_authorized_phone(db)
    if not phone:
        return {
            "ok": False,
            "needs_phone": True,
            "reply": (
                f"📱 尚未登记授权登录手机号。请先在终端运行 `siming auth-login --set-phone`（或在 QQ 发 `绑定手机号 138xxxxxxxx`，网关自动加密拦截不留明文），随后即可代登 **{spec['title']}**。"
            ),
        }

    base_info = resolve_chromium_cdp_base(port=port)
    if not base_info or not base_info.get("ws_browser"):
        return {"ok": False, "reply": "❌ 本机主 Chromium (9222) 未在线，请先确保桌面浏览器开启。"}

    target_id = ""
    created_tab = False
    try:
        ws_host_base = base_info["ws_browser"].split("/devtools/")[0]
        page_ws = ""
        if reuse_existing_tab:
            hints = tuple(spec.get("login_url_hints") or ())
            for t in list_chromium_tabs(port=port):
                t_url = str(t.get("url") or "").lower()
                if any(h.lower() in t_url for h in hints):
                    target_id = str(t.get("id") or "")
                    page_ws = str(t.get("webSocketDebuggerUrl") or f"{ws_host_base}/devtools/page/{target_id}")
                    break

        if not page_ws:
            with LocalWsCdpClient(base_info["ws_browser"], timeout=6.0) as b_ws:
                created = b_ws.call("Target.createTarget", {"url": "about:blank", "background": True})
                target_id = str(created.get("targetId") or "")
            created_tab = True
            tabs = list_chromium_tabs(port=port)
            tab_info = next((t for t in tabs if str(t.get("id") or "") == target_id), None)
            page_ws = str((tab_info or {}).get("webSocketDebuggerUrl") or f"{ws_host_base}/devtools/page/{target_id}")

            with LocalWsCdpClient(page_ws, timeout=8.0) as p_ws:
                try:
                    p_ws.call("Emulation.setFocusEmulationEnabled", {"enabled": True})
                except Exception:
                    pass
                if spec.get("mobile"):
                    try:
                        p_ws.call(
                            "Emulation.setDeviceMetricsOverride",
                            {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True},
                        )
                    except Exception:
                        pass
                p_ws.call("Page.navigate", {"url": spec["login_url"]})
            time.sleep(3.5)

            # 京东等登录页若默认停在“账号密码登录”，先切回“短信验证码登录”
            switch_sms_js = """
            (() => {
              const txt = (document.body && document.body.innerText) || '';
              if (/获取验证码|发送验证码|获取短信验证码/.test(txt)) return 'already_sms';
              const els = Array.from(document.querySelectorAll('a, button, span, div, p'));
              const sw = els.find(el => /^(短信验证码登录|手机验证码登录|验证码登录|短信登录|手机号登录)$/.test((el.innerText || '').trim()));
              if (sw) { sw.click(); return 'switched_to_sms'; }
              return 'no_switch';
            })()
            """
            fill_phone_js = f"""
            (() => {{
              const phone = {json.dumps(phone)};
              const txt = (document.body && document.body.innerText) || '';
              const inputs = Array.from(document.querySelectorAll('input')).filter(el => el.type !== 'hidden' && el.type !== 'checkbox');
              const telIn = inputs.find(el => /tel|phone|mobile|手机/i.test((el.type||'')+(el.placeholder||'')+(el.name||'')+(el.id||''))) || inputs[0];
              if (!telIn) return JSON.stringify({{step: 'no_phone_input', url: location.href, title: document.title, text: txt.slice(0, 600)}});
              const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
              telIn.focus();
              setter.call(telIn, phone);
              telIn.dispatchEvent(new Event('input', {{bubbles: true}}));
              telIn.dispatchEvent(new Event('change', {{bubbles: true}}));
              // 勾选用户协议复选框（含京东 .policy_tip-checkbox 等自定义勾选框）
              const cbs = Array.from(document.querySelectorAll('input[type="checkbox"]'));
              cbs.forEach(cb => {{ if (!cb.checked) cb.click(); }});
              const customPolicy = document.querySelector('.policy_tip-checkbox, [class*="policy"][class*="check"], [class*="agree"][class*="check"]');
              if (customPolicy && !/checked|active|selected/i.test(customPolicy.className || '')) {{
                customPolicy.click();
              }}
              const btns = Array.from(document.querySelectorAll('button, a, span, div, p'));
              const sendBtn = btns.find(b => /^(获取验证码|发送验证码|获取短信验证码|Send Code)$/i.test((b.innerText||'').trim()));
              if (sendBtn) sendBtn.click();
              return JSON.stringify({{step: sendBtn ? 'sms_requested' : 'phone_filled_only', url: location.href, title: document.title, text: txt.slice(0, 600)}});
            }})()
            """
            with LocalWsCdpClient(page_ws, timeout=8.0) as p_ws:
                sw_ev = p_ws.call("Runtime.evaluate", {"expression": switch_sms_js, "returnByValue": True})
                if str(((sw_ev.get("result") or {}).get("value")) or "") == "switched_to_sms":
                    time.sleep(0.9)
                ev1 = p_ws.call("Runtime.evaluate", {"expression": fill_phone_js, "returnByValue": True})
                st1 = json.loads(str(((ev1.get("result") or {}).get("value")) or "{}"))
                time.sleep(1.8)
                ev_chk = p_ws.call(
                    "Runtime.evaluate",
                    {
                        "expression": "JSON.stringify({url: location.href, title: document.title, text: (document.body && document.body.innerText || '').slice(0, 1200), frames: Array.from(document.querySelectorAll('iframe')).map(f => f.src || '')})",
                        "returnByValue": True,
                    },
                )
                chk = json.loads(str(((ev_chk.get("result") or {}).get("value")) or "{}"))

            # 合规红线：检测页面文本或 iframe 是否弹出滑块/人机核验，弹出即停手原样保留标签页！
            chk_hay = " ".join(
                [
                    str(chk.get("text") or ""),
                    str(chk.get("url") or ""),
                    " ".join(str(x) for x in (chk.get("frames") or [])),
                ]
            )
            if _SLIDER_GATE_RE.search(chk_hay):
                return {
                    "ok": False,
                    "gated": True,
                    "target_id": target_id,
                    "reply": (
                        f"🛡️ **{spec['title']} 登录触发滑块验证**：司命已替你填好手机号 `{mask_phone(phone)}`，并按合规红线在主浏览器后台保留了该标签页。\n"
                        "请切到浏览器滑一下触发发短信，手机快捷指令转发验证码后发一句 `填验证码 " + plat + "` 即可自动填码完成登录！"
                    ),
                }

            if st1.get("step") == "no_phone_input":
                gate = detect_compliance_gate(str(st1.get("url") or ""), str(st1.get("title") or ""), str(st1.get("text") or ""))
                if gate is None:
                    if created_tab:
                        _close_cdp_tab_safe(base_info["ws_browser"], target_id)
                    return {"ok": True, "already_logged_in": True, "reply": f"🟢 **{spec['title']}** 在主浏览器中已处于登录态，无需重复验证！"}

        otp_hit = wait_for_platform_otp(db, platform_hint=plat, timeout_sec=wait_sec_otp, edge_otp_caller=edge_otp_caller)
        if otp_hit is None:
            return {
                "ok": False,
                "timeout": True,
                "target_id": target_id,
                "reply": (
                    f"⏳ 已在 **{spec['title']}** 填入 `{mask_phone(phone)}` 等待验证码超时（{int(wait_sec_otp)}s 内未从 81 收到新短信验证码）。\n"
                    "请确认手机「司命短信入库」自动化已开启；若刚才需要手动点发送或滑块，收到短信后直接回 `填验证码 " + plat + "` 即可。"
                ),
            }

        fill_otp_js = f"""
        (() => {{
          const code = {json.dumps(otp_hit['code'])};
          const inputs = Array.from(document.querySelectorAll('input')).filter(el => el.type !== 'hidden' && el.type !== 'checkbox');
          const codeIn = inputs.find(el => /code|otp|verify|auth|短信|验证码/i.test((el.placeholder||'')+(el.name||'')+(el.id||''))) || inputs[1] || inputs[0];
          if (!codeIn) return 'no_code_input';
          const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
          codeIn.focus();
          setter.call(codeIn, code);
          codeIn.dispatchEvent(new Event('input', {{bubbles: true}}));
          codeIn.dispatchEvent(new Event('change', {{bubbles: true}}));
          const cbs = Array.from(document.querySelectorAll('input[type="checkbox"]'));
          cbs.forEach(cb => {{ if (!cb.checked) cb.click(); }});
          const btns = Array.from(document.querySelectorAll('button, a, div, span'));
          const loginBtn = btns.find(b => /^(登\\s*录|立即登录|确定|验证并登录|一键登录|Sign in|Login)$/i.test((b.innerText||'').trim()));
          if (loginBtn) loginBtn.click();
          return loginBtn ? 'submitted' : 'otp_filled_no_btn';
        }})()
        """
        with LocalWsCdpClient(page_ws, timeout=8.0) as p_ws:
            ev2 = p_ws.call("Runtime.evaluate", {"expression": fill_otp_js, "returnByValue": True})
            res_step = str(((ev2.get("result") or {}).get("value")) or "")
            time.sleep(2.8)
            ev_post = p_ws.call(
                "Runtime.evaluate",
                {
                    "expression": "JSON.stringify({url: location.href, title: document.title, text: (document.body && document.body.innerText || '').slice(0, 800), frames: Array.from(document.querySelectorAll('iframe')).map(f => f.src || '')})",
                    "returnByValue": True,
                },
            )
            post = json.loads(str(((ev_post.get("result") or {}).get("value")) or "{}"))

        post_hay = " ".join(
            [
                str(post.get("text") or ""),
                str(post.get("url") or ""),
                " ".join(str(x) for x in (post.get("frames") or [])),
            ]
        )
        if _SLIDER_GATE_RE.search(post_hay):
            return {
                "ok": False,
                "gated": True,
                "target_id": target_id,
                "reply": (
                    f"🛡️ **{spec['title']} 提交验证码后触发滑块/二次验证**：已按合规红线停手并保留标签页，请切到 Chromium 完成验证。"
                ),
            }

        post_url = str(post.get("url") or "").lower()
        still_on_login = any(h.lower() in post_url for h in (spec.get("login_url_hints") or ()))
        has_cookie = False
        if spec.get("session_domain") and spec.get("session_cookies"):
            has_cookie = has_domain_session_cookies(
                str(spec["session_domain"]),
                tuple(spec["session_cookies"]),
                port=port,
            )
        verified = (not still_on_login) or has_cookie
        if verified and created_tab:
            _close_cdp_tab_safe(base_info["ws_browser"], target_id)

        return {
            "ok": True,
            "verified": verified,
            "brand": otp_hit["brand"],
            "step": res_step,
            "reply": (
                f"✅ 已接收 `【{otp_hit['brand']}】` 验证码（`{mask_otp(otp_hit['code'])}`，81 瞬时密文已焚毁）并自动填入 **{spec['title']}** 提交登录！"
            ),
        }
    except Exception as exc:
        return {"ok": False, "reply": f"❌ 平台短信代登执行异常：{exc}"}


def attempt_auto_platform_relogin(
    db: SimingDB,
    platform_raw: str = "jd",
    *,
    cooldown_sec: int = DEFAULT_AUTO_RELOGIN_COOLDOWN_SEC,
    wait_sec_otp: float = 65.0,
    relogin_runner: Optional[Callable[[SimingDB, str], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """平台登录态过期时自动尝试用本人授权手机号 + 81 瞬时短信验证码续登（带冷却保护，绝不刷屏发短信；遇滑块即停手交还本人）。"""
    db.init_db()
    plat = resolve_platform_key(platform_raw)
    if plat not in PLATFORM_ALIASES:
        return {"attempted": False, "ok": False, "reason": "unsupported_platform"}
    if not get_authorized_phone(db) and relogin_runner is None:
        return {"attempted": False, "ok": False, "reason": "no_authorized_phone"}
    if relogin_runner is None and (
        os.environ.get("SIMING_TEST_OFFLINE") == "1" or os.environ.get("SIMING_DRY_RUN_SEND") == "1"
    ):
        return {"attempted": False, "ok": False, "reason": "offline"}

    meta_key = f"auto_relogin_at:{plat}"
    cur = now_local()
    last_at = db.get_meta(meta_key)
    if last_at and cooldown_sec > 0:
        try:
            elapsed = (cur - parse_iso(last_at)).total_seconds()
            if 0 <= elapsed < cooldown_sec:
                return {
                    "attempted": False,
                    "ok": False,
                    "reason": "cooldown",
                    "last_attempt_at": last_at,
                }
        except Exception:
            pass

    # 先落冷却时间戳再发起登录，防止异常或重试导致短时间连发短信
    db.set_meta(meta_key, iso_now(cur))
    if relogin_runner is not None:
        res = dict(relogin_runner(db, plat) or {})
    else:
        res = run_authorized_sms_login(db, plat, wait_sec_otp=wait_sec_otp)

    db.write_audit(
        "A1",
        "auto_platform_relogin",
        {
            "platform": plat,
            "ok": bool(res.get("ok")),
            "gated": bool(res.get("gated")),
            "timeout": bool(res.get("timeout")),
            "already_logged_in": bool(res.get("already_logged_in")),
        },
        detail=f"plat={plat} ok={bool(res.get('ok'))} gated={bool(res.get('gated'))}",
    )
    return {"attempted": True, **res}
