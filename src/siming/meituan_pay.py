"""美团外卖 H5 实付结算预览、红包优选与许可制提交支付引擎（meituan_pay）：
- 严格两阶段「先看实付、获许可再扣款」闭环：
  1. 零扣款结算预览（Phase 1）：在 9222 后台完成加购、必选餐具处理、进入 `mindex/preview`、尝试勾选最优可用美团红包，并直接回读真实「实付合计 + 配送地址 + 预计送达时间」，保留预览标签页，生成 10 分钟有效 `meituan_submit_pay` 付款许可确认卡（此时 ¥0 扣款、零下单）；
  2. 许可后真提交并支付（Phase 2）：**仅当用户明确发出 `确认支付` / `确认付款` / `确认下单` / `apply Axx`（或引用确认卡回 `确认`）**，经纯代码单次 CAS 审批锁后，二次核验实时合计未超单笔上限且波动 ≤ ¥2、配送地址一致，才点击「提交订单」并在美团收银台（优先已开通的极速支付/美团支付免密通道）点击「确认支付」，最后校验支付成功/商家接单回执；
- 安全与合规红线：
  - 绝不允许 AI 模型绕过审批代付，也绝不接受未引用的随口「好/行/1」误触发真实扣款；
  - 遇到登录、滑块风控验证、6 位支付密码键盘、短信二次验证或仅限手机端微信/支付宝扫码拉起时，**立即停手原样保留现场**并把收银台/待付款入口发给用户接力，绝不绕过任何风控或认证。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from siming.approval import create_approval
from siming.cdp import (
    LocalWsCdpClient,
    detect_compliance_gate,
    list_chromium_tabs,
    resolve_chromium_cdp_base,
)
from siming.config import load_policy
from siming.db import SHANGHAI_TZ, SimingDB, iso_now, now_local

MEITUAN_PREVIEW_PATH = "mindex/preview"
MEITUAN_PREVIEW_PATHS = ("mindex/preview", "package/preview/preview", "/preview/preview")
MEITUAN_OLIST_URL = "https://h5.waimai.meituan.com/waimai/mindex/olist"
ACTIVE_PREVIEW_TID_KEY = "meituan_active_preview_tid"
ACTIVE_PREVIEW_MENU_KEY = "meituan_active_preview_menu_url"

_PHONE_11_RE = re.compile(r"(?<!\d)(1[3-9]\d)\d{4}(\d{4})(?!\d)")
_YUAN_NUM_RE = re.compile(r"[¥￥]\s*(\d+(?:\.\d{1,2})?)")

_CAPTCHA_URL_RE = re.compile(r"(verify\.meituan\.com|yoda|captcha|sec\.meituan)", re.IGNORECASE)
_LOGIN_URL_RE = re.compile(r"(passport\.meituan\.com|/login\b|/signin\b)", re.IGNORECASE)
_CAPTCHA_TXT_RE = re.compile(r"(滑动验证|安全验证|请拖动滑块|人机验证|点击完成验证|访问过于频繁)")
_LOGIN_TXT_RE = re.compile(r"(请先登录|扫码登录|账号登录|手机号登录)")
_HARD_2FA_RE = re.compile(r"(短信验证码|获取短信验证码|输入短信验证码|人脸识别|指纹验证|生物验证)")
_PASSWORD_2FA_RE = re.compile(
    r"(请输入密码|请输入支付密码|输入6位支付密码|输入支付密码|验证支付密码|忘记密码|找回密码|短信验证码|获取短信验证码|输入短信验证码|人脸识别|指纹验证|生物验证)"
)
_EXTERNAL_APP_RE = re.compile(
    r"(打开微信|微信扫码|打开支付宝|支付宝扫码|请使用微信|请使用支付宝|跳转微信|跳转支付宝|weixin://|alipays://)",
    re.IGNORECASE,
)
_PAID_SUCCESS_RE = re.compile(
    r"(交易成功|支付成功|付款成功|等待商家接单|商家已接单|骑手正在赶往|骑手已接单|骑手已取餐|商品制作中|商家备餐中|订单已完成)"
)
_UNPAID_MARKER_RE = re.compile(r"(待支付|待付款|订单已提交|等待支付|继续支付|去支付|立即支付|确认支付|确认付款|剩余支付时间|支付失败|支付超时|余额不足|扣款失败)")
_PAY_FAIL_RE = re.compile(r"(支付失败|扣款失败|余额不足|支付超时|银行卡余额不足|交易失败|密码错误)")
# 收银台支付方式列表中「余额」一行的常规副文案（如「可用余额不足，需组合支付」）属于正常展示，绝非扣款失败！
_BENIGN_BALANCE_HINT_RE = re.compile(
    r"(?:可用|账户)?余额不足[，,\s]*(?:需组合支付|可组合支付|暂不可单独支付|无法单独支付|无法使用)"
)
_OLIST_UNPAID_RE = re.compile(r"(待支付|待付款|订单已提交|等待支付|立即支付|去支付|取消|退款)")
_OLIST_PAID_RE = re.compile(r"(商家已接单|等待商家接单|骑手|商品制作中|商家备餐中|配送中|已送达|已完成)")


def _is_meituan_preview_url(url: str) -> bool:
    u = (url or "").lower()
    return any(p in u for p in MEITUAN_PREVIEW_PATHS)
_SUBMIT_ERROR_RE = re.compile(
    r"(商品已售罄|部分商品已售罄|已售罄|已下架|库存不足|不在营业时间|商家已打烊|暂不接单|未满起送|差\s*[¥￥]?\s*\d+.*起送|必选品|必选餐具|订单提交失败|参数错误|请重新下单)"
)
_PREVIEW_BADGE_LINES = {
    "美团快送",
    "美团专送",
    "商家自配",
    "美团跑腿",
    "企鹅跑腿",
    "准时宝",
    "放心吃",
    "支持自取",
    "神券商家",
    "品牌",
    "拼好饭",
    "美团配送",
    "到店自取",
}


def _mask_phones(text: str) -> str:
    return _PHONE_11_RE.sub(r"\1****\2", text or "")


def _setup_mobile_session(p_ws: LocalWsCdpClient) -> None:
    for method, params in (
        ("Page.enable", {}),
        ("Emulation.setFocusEmulationEnabled", {"enabled": True}),
        ("Page.setWebLifecycleState", {"state": "active"}),
        ("Emulation.setDeviceMetricsOverride", {"width": 390, "height": 844, "deviceScaleFactor": 2, "mobile": True}),
    ):
        try:
            p_ws.call(method, params)
        except Exception:
            pass


def _eval_json(p_ws: LocalWsCdpClient, expr: str) -> dict[str, Any]:
    res = p_ws.call("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    val = (res.get("result") or {}).get("value")
    if isinstance(val, str) and val.startswith("{"):
        try:
            return json.loads(val)
        except Exception:
            return {}
    return {}


def _cdp_click_xy(p_ws: LocalWsCdpClient, x: float, y: float) -> None:
    for tp in ("mouseMoved", "mousePressed", "mouseReleased"):
        ev: dict[str, Any] = {"type": tp, "x": float(x), "y": float(y)}
        if tp in ("mousePressed", "mouseReleased"):
            ev["button"] = "left"
            ev["clickCount"] = 1
        p_ws.call("Input.dispatchMouseEvent", ev)


def _cdp_touch_tap(p_ws: LocalWsCdpClient, x: float, y: float, hold_sec: float = 0.07) -> None:
    """派发 CDP 真实触摸点击（touchStart -> touchMove -> touchEnd），用于仅监听 touch 事件的移动端组件（如美团收银台密码键盘）。"""
    pt = {"x": float(x), "y": float(y), "id": 1}
    p_ws.call("Input.dispatchTouchEvent", {"type": "touchStart", "touchPoints": [pt]})
    time.sleep(0.03)
    p_ws.call("Input.dispatchTouchEvent", {"type": "touchMove", "touchPoints": [pt]})
    time.sleep(max(0.02, float(hold_sec) - 0.03))
    p_ws.call("Input.dispatchTouchEvent", {"type": "touchEnd", "touchPoints": []})


def _normalize_addr_token(addr: str) -> str:
    s = re.sub(r"[\s\-—()（）·•,，。]", "", (addr or "").lower())
    return s


def parse_meituan_preview_bill(text: str) -> dict[str, Any]:
    """把美团外卖 H5 `mindex/preview`（提交订单页）innerText 解析为结构化实付账单明细。"""
    lines = [ln.strip() for ln in (text or "").replace("\r", "").splitlines() if ln.strip()]
    delivery_address = ""
    recipient_masked = ""
    eta_text = ""
    shop = ""
    packing_fee: Optional[float] = None
    delivery_fee: Optional[float] = None
    coupon_text = ""
    store_voucher_text = ""
    delivery_coupon_text = ""
    store_manjian_yuan: Optional[float] = None
    discount_yuan: Optional[float] = None
    final_payable: Optional[float] = None
    items: list[dict[str, Any]] = []

    # 1. 收货地址 & 收件人（兼容 `张三\n先生 138...` 与 `张三\n先生\n138...` 两种换行排布）
    for i, ln in enumerate(lines[:14]):
        if _PHONE_11_RE.search(ln) or re.search(r"(?:先生|女士)\s*1\d{2}\*{0,4}\d{4}", ln):
            rec_parts = [ln]
            addr_idx = i - 1
            while addr_idx >= 0 and lines[addr_idx] in ("先生", "女士"):
                rec_parts.insert(0, lines[addr_idx])
                addr_idx -= 1
            if (
                addr_idx >= 0
                and len(lines[addr_idx]) <= 6
                and not any(k in lines[addr_idx] for k in ("配送", "自取", "号", "层", "室", "座", "楼", "小区", "柜", "大厦", "园"))
            ):
                rec_parts.insert(0, lines[addr_idx])
                addr_idx -= 1
            recipient_masked = _mask_phones(" ".join(rec_parts))
            if addr_idx >= 0 and lines[addr_idx] not in ("提交订单", "美团配送", "外卖配送", "到店自取"):
                delivery_address = lines[addr_idx]
            break

    # 2. 送达时间
    for i, ln in enumerate(lines[:20]):
        if "立即送出" in ln or "大约" in ln and "送达" in ln:
            if "立即送出" in ln and i + 1 < len(lines) and "送达" in lines[i + 1]:
                eta_text = f"{ln} {lines[i + 1]}"
            else:
                eta_text = ln
            break

    # 3. 打包费、配送费、红包、优惠、合计
    for i, ln in enumerate(lines):
        if ln == "打包费" and i + 1 < len(lines):
            ms = _YUAN_NUM_RE.findall(lines[i + 1])
            if ms:
                packing_fee = float(ms[-1])
        elif ln == "配送费" and i + 1 < len(lines):
            ship_nums: list[float] = []
            for k in range(i + 1, min(i + 5, len(lines))):
                cand_s = lines[k]
                if cand_s in ("店铺满减", "美团红包", "商家代金券", "优惠规则", "优惠说明", "小计", "合计") or cand_s.startswith(("小计", "合计", "已优惠")):
                    break
                if "免配送费" in cand_s:
                    ship_nums = [0.0]
                    break
                ship_nums.extend(float(x) for x in _YUAN_NUM_RE.findall(cand_s))
            if ship_nums:
                delivery_fee = ship_nums[-1]
        elif ln in ("美团红包", "神券红包", "美团神券", "美团红包/神券") and i + 1 < len(lines):
            for k in range(i + 1, min(i + 4, len(lines))):
                cand_c = lines[k].strip()
                if cand_c in ("商家代金券", "配送券", "优惠活动", "优惠规则", "优惠说明", "小计", "合计"):
                    break
                if cand_c in ("神券", "天天神券", "可膨胀"):
                    continue
                coupon_text = cand_c
                m_disc = re.search(r"-\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)", coupon_text)
                if m_disc:
                    discount_yuan = max(discount_yuan or 0.0, float(m_disc.group(1)))
                break
        elif ln in ("商家代金券", "店铺代金券") and i + 1 < len(lines):
            if lines[i + 1].strip() not in ("配送券", "美团红包", "优惠活动", "优惠规则", "小计", "合计"):
                store_voucher_text = lines[i + 1].strip()
        elif ln in ("配送券", "配送费券") and i + 1 < len(lines):
            if lines[i + 1].strip() not in ("商家代金券", "美团红包", "优惠活动", "优惠规则", "小计", "合计"):
                delivery_coupon_text = lines[i + 1].strip()
        elif ln == "店铺满减" and i + 1 < len(lines):
            mmj = re.search(r"-\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)", lines[i + 1])
            if mmj:
                store_manjian_yuan = float(mmj.group(1))
        elif "已优惠" in ln:
            m_d = _YUAN_NUM_RE.search(ln)
            if m_d:
                discount_yuan = max(discount_yuan or 0.0, float(m_d.group(1)))

    # 4. 最终实付合计（取最后一个 合计¥ / 实付¥，若无则回落 小计¥）
    for pat in (r"(?:合计|实付|待支付)\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)", r"小计\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)"):
        matches = re.findall(pat, text or "")
        if matches:
            final_payable = float(matches[-1])
            break

    # 5. 门店与商品明细（结构锚点：商品名 / ¥现价 / [¥原价] / [规格] / x数量）
    qty_indices = [idx for idx, ln in enumerate(lines) if re.fullmatch(r"[xX×]\s*\d{1,2}", ln)]
    first_item_name_idx: Optional[int] = None
    for q_idx in qty_indices:
        qty = int(re.sub(r"\D", "", lines[q_idx]) or "1")
        price_idxs = [k for k in range(max(0, q_idx - 4), q_idx) if _YUAN_NUM_RE.fullmatch(lines[k])]
        p_idx = price_idxs[0] if price_idxs else None
        if p_idx is not None and p_idx >= 1:
            d_name = lines[p_idx - 1]
            if first_item_name_idx is None:
                first_item_name_idx = p_idx - 1
            d_price = float(_YUAN_NUM_RE.fullmatch(lines[p_idx]).group(1))  # type: ignore[union-attr]
            spec_parts = [lines[k] for k in range(p_idx + 1, q_idx) if not _YUAN_NUM_RE.fullmatch(lines[k])]
            d_spec = " ".join(spec_parts).strip()
            items.append({"name": d_name, "price_yuan": d_price, "spec": d_spec, "qty": qty})

    if first_item_name_idx is not None and first_item_name_idx >= 1:
        for j in range(first_item_name_idx - 1, -1, -1):
            cand = lines[j]
            if (
                not cand
                or cand in _PREVIEW_BADGE_LINES
                or _YUAN_NUM_RE.match(cand)
                or "送达" in cand
                or "立即送出" in cand
                or "时间可能波动" in cand
                or "预估" in cand
                or _PHONE_11_RE.search(cand)
                or re.search(r"(?:先生|女士)\s*1\d{2}\*{0,4}\d{4}", cand)
            ):
                continue
            shop = cand
            break

    parsed = {
        "delivery_address": delivery_address,
        "recipient_masked": recipient_masked,
        "eta_text": eta_text,
        "shop": shop,
        "items": items,
        "packing_fee_yuan": packing_fee,
        "delivery_fee_yuan": delivery_fee,
        "coupon_text": coupon_text,
        "store_voucher_text": store_voucher_text,
        "delivery_coupon_text": delivery_coupon_text,
        "store_manjian_yuan": store_manjian_yuan,
        "discount_yuan": discount_yuan,
        "final_payable_yuan": final_payable,
        "has_submit_btn": "提交订单" in (text or ""),
    }
    # 兼容新版 `package/preview/preview`（单价:¥X / 数量:N / 外卖配送）格式：优先采用其结构化解析结果
    if any(k in (text or "") for k in ("数量:", "数量：", "单价:", "单价：", "外卖配送")):
        try:
            from siming.meituan_cart_live import parse_cart_preview_bill

            cart_bill = parse_cart_preview_bill(text or "")
            for key in (
                "delivery_address",
                "recipient_masked",
                "eta_text",
                "shop",
                "items",
                "packing_fee_yuan",
                "delivery_fee_yuan",
                "coupon_text",
                "store_voucher_text",
                "delivery_coupon_text",
                "store_manjian_yuan",
                "discount_yuan",
                "final_payable_yuan",
            ):
                if cart_bill.get(key) not in (None, "", []):
                    parsed[key] = cart_bill[key]
        except Exception:
            pass
    return parsed


def validate_pre_submit_guardrails(
    live_bill: dict[str, Any],
    params: dict[str, Any],
    max_yuan: float,
    max_drift_yuan: float = 2.0,
) -> dict[str, Any]:
    """点击「提交订单」前的纯代码防呆核验：强校验实付总额、单笔上限、价格波动与收货地址一致。"""
    payable = live_bill.get("final_payable_yuan")
    if payable is None or float(payable) <= 0:
        return {"ok": False, "reason": "missing_payable", "error": "未能从结算页读到明确实付金额，拒绝盲目提交扣款。"}

    payable_f = round(float(payable), 2)
    if payable_f > float(max_yuan):
        return {
            "ok": False,
            "reason": "over_cap",
            "error": f"结算页实时应付 **¥{payable_f:g}** 超过单笔安全上限（¥{max_yuan:.0f}），已中止支付。",
        }

    approved_raw = params.get("final_payable_yuan")
    if approved_raw is None:
        approved_raw = params.get("est_yuan")
    if approved_raw is not None:
        approved_f = round(float(approved_raw), 2)
        if payable_f > approved_f + float(max_drift_yuan):
            return {
                "ok": False,
                "reason": "price_drift",
                "error": (
                    f"结算页实时应付已由确认卡的 **¥{approved_f:g}** 涨至 **¥{payable_f:g}**（涨幅超过 ¥{max_drift_yuan:g}），"
                    "为防止意外多扣款已自动中止，请重新发起确认。"
                ),
            }

    expected_addr = str(params.get("delivery_address") or params.get("delivery_poi") or "").strip()
    live_addr = str(live_bill.get("delivery_address") or "").strip()
    if expected_addr and live_addr:
        exp_head = _normalize_addr_token(expected_addr.split()[0])
        live_norm = _normalize_addr_token(live_addr)
        exp_norm = _normalize_addr_token(expected_addr)
        if exp_head and len(exp_head) >= 2 and (exp_head not in live_norm) and (live_norm not in exp_norm):
            return {
                "ok": False,
                "reason": "address_mismatch",
                "error": f"结算页配送地址（{live_addr}）与确认卡（{expected_addr}）不一致，已中止支付。",
            }

    return {"ok": True, "payable_yuan": payable_f}


def extract_cashier_payable_yuan(text: str) -> Optional[float]:
    """从美团收银台页面文本中精确提取本次待扣款金额（严防把余额/立减/免密额度误当成订单实付）。"""
    raw = text or ""
    # 1. 优先匹配主支付按钮、交易成功提示或明确应付标签
    for pat in (
        r"(?:交易成功|支付成功|付款成功)\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)",
        r"(?:确认支付|立即支付|确认付款|极速支付|免密支付)\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)",
        r"(?:需支付|待支付|应付金额|订单金额|支付金额|实付金额|实付)\s*[：:]?\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)",
    ):
        ms = re.findall(pat, raw)
        if ms:
            return round(float(ms[-1]), 2)

    # 2. 次选：收银台顶部独立金额行（支持 `¥20.34` 单行与 `¥\n20.34` 拆行，跳过余额/立减/额度/优惠/红包/满减行）
    lines = [ln.strip() for ln in raw.replace("\r", "").splitlines() if ln.strip()]
    for idx, ln in enumerate(lines[:14]):
        if any(k in ln for k in ("余额", "立减", "优惠", "额度", "红包", "满", "减", "省", "可用")):
            continue
        m_solo = re.fullmatch(r"[¥￥]\s*(\d+(?:\.\d{1,2})?)", ln)
        if m_solo:
            return round(float(m_solo.group(1)), 2)
        if ln in ("¥", "￥") and idx + 1 < len(lines):
            nxt = lines[idx + 1]
            if re.fullmatch(r"\d+(?:\.\d{1,2})?", nxt):
                return round(float(nxt), 2)
    return None


def classify_cashier_or_order_state(url: str, title: str = "", text: str = "") -> dict[str, Any]:
    """识别点击「提交订单」或收银台「确认支付」后的页面状态（绝不绕过验证码/密码/2FA）。"""
    u = (url or "").strip()
    u_low = u.lower()
    hay = f"{title}\n{text[:2500]}"

    if _CAPTCHA_URL_RE.search(u) or _CAPTCHA_TXT_RE.search(hay):
        return {"state": "CAPTCHA", "reason": "触发美团安全/滑块验证，已按红线停手保留现场"}
    if _LOGIN_URL_RE.search(u) or _LOGIN_TXT_RE.search(hay):
        return {"state": "LOGIN", "reason": "页面要求重新登录，已停手保留现场"}

    # 明确扣款失败/余额不足（优先级高于 PAID 与 CASHIER_READY；先剔除收银台余额行「可用余额不足，需组合支付」等常规提示）
    hay_for_fail = _BENIGN_BALANCE_HINT_RE.sub("", hay)
    m_pf = _PAY_FAIL_RE.search(hay_for_fail)
    if m_pf and not re.search(r"(?:交易成功|支付成功|付款成功)", hay):
        return {"state": "PAY_FAILED", "reason": f"支付未完成：{m_pf.group(0)}"}

    # 明确的收银台完成回执（如 mpay.meituan.com 的「交易成功 ¥20.34」）优先于背景残留 DOM
    if re.search(r"(?:交易成功|支付成功|付款成功)", hay) and not m_pf:
        return {"state": "PAID", "reason": "订单已支付成功"}

    # 支付密码 / 短信验证码 / 生物识别优先于普通收银台，避免误点
    if _PASSWORD_2FA_RE.search(hay):
        return {"state": "PASSWORD_OR_2FA", "reason": "收银台要求支付密码或二次身份验证，已按安全红线停手保留现场"}

    # 外部 App 扫码/拉起
    if _EXTERNAL_APP_RE.search(u) or _EXTERNAL_APP_RE.search(hay):
        # 注意：普通收银台同时列出「美团支付 + 微信支付 + 支付宝」时不应误判为只能外部拉起
        is_cashier_list = ("npay.meituan.com" in u_low or "mpay.meituan.com" in u_low) and any(
            k in hay for k in ("确认支付", "立即支付", "极速支付", "美团支付", "美团月付")
        )
        if not is_cashier_list:
            return {"state": "EXTERNAL_APP", "reason": "当前支付渠道需手机端微信/支付宝完成确认"}

    # 已支付成功 / 商家已接单
    on_order_page = any(k in u_low for k in ("orderdetail", "orderstatus", "paysuccess", "payresult", "trade/result"))
    has_paid_txt = bool(_PAID_SUCCESS_RE.search(hay))
    has_unpaid_txt = bool(_UNPAID_MARKER_RE.search(hay))
    if has_paid_txt and not has_unpaid_txt:
        return {"state": "PAID", "reason": "订单已支付成功"}
    if on_order_page and not has_unpaid_txt and ("下单" in hay or "预计" in hay or "商家" in hay):
        return {"state": "PAID", "reason": "订单已提交并进入制作/接单状态"}

    # 美团收银台待点确认支付
    if (
        any(k in u_low for k in ("npay.meituan.com", "mpay.meituan.com", "/cashier"))
        or ("收银台" in hay and any(k in hay for k in ("确认支付", "立即支付", "确认付款", "极速支付")))
    ):
        return {"state": "CASHIER_READY", "reason": "已抵达美团收银台，可执行授权付款"}

    # 提单失败提示
    m_err = _SUBMIT_ERROR_RE.search(hay)
    if m_err and _is_meituan_preview_url(u_low):
        return {"state": "SUBMIT_ERROR", "reason": f"美团拒绝提单：{m_err.group(0)}"}

    return {"state": "UNKNOWN", "reason": "页面加载或跳转中"}


def _close_tab_quietly(port: int, tid: str) -> None:
    if not tid:
        return
    base = resolve_chromium_cdp_base(port=port)
    if not base or not base.get("ws_browser"):
        return
    try:
        with LocalWsCdpClient(base["ws_browser"], timeout=3.5) as b_ws:
            b_ws.call("Target.closeTarget", {"targetId": tid})
    except Exception:
        pass


def close_stale_preview_tab(db: SimingDB, port: int = 9222, keep_tid: str = "") -> None:
    """清理上一次未结算的旧预览标签页，防止桌面后台堆积多个过期结算页。"""
    old_tid = (db.get_meta(ACTIVE_PREVIEW_TID_KEY) or "").strip()
    if old_tid and old_tid != keep_tid:
        _close_tab_quietly(port, old_tid)
        if not keep_tid:
            db.set_meta(ACTIVE_PREVIEW_TID_KEY, "")


def _clear_cart_on_menu_tab(p_ws: LocalWsCdpClient, menu_url: str) -> bool:
    """回退门店菜单页并清空当前门店购物车，用于取消订单或超上限拦截后的无痕清理。"""
    if not menu_url:
        return False
    try:
        p_ws.call("Page.navigate", {"url": menu_url})
        time.sleep(2.6)
        p_ws.call(
            "Runtime.evaluate",
            {"expression": "document.querySelector('[aria-label=\"购物车\"]')?.click()", "returnByValue": True},
        )
        time.sleep(0.9)
        p_ws.call(
            "Runtime.evaluate",
            {
                "expression": "Array.from(document.querySelectorAll('div,span,button,a')).find(e=>(e.innerText||'').trim()==='清空购物车'&&e.children.length===0)?.click()",
                "returnByValue": True,
            },
        )
        time.sleep(0.7)
        p_ws.call(
            "Runtime.evaluate",
            {
                "expression": "Array.from(document.querySelectorAll('div,span,button,a')).find(e=>/^(清空|确定|确认)$/.test((e.innerText||'').trim())&&e.children.length===0)?.click()",
                "returnByValue": True,
            },
        )
        time.sleep(0.7)
        return True
    except Exception:
        return False


def _try_pick_mandatory_tableware(p_ws: LocalWsCdpClient) -> bool:
    """若门店存在「必选餐具 / 必选品」阻断去结算，委托 `ensure_mindex_required_products` 自动补齐（优先 ¥0 不需要餐具）。"""
    from siming.meituan_cart_live import ensure_mindex_required_products

    return ensure_mindex_required_products(p_ws, force_dom_check=True)


def _try_auto_apply_best_coupon(p_ws: LocalWsCdpClient, before_total: Optional[float]) -> bool:
    """在 `mindex/preview` 或 `package/preview/preview` 统一择优勾选最优美团红包 + 商家代金券 + 配送券，保底自动回退预览页绝不滞留。"""
    from siming.meituan_coupons import apply_optimal_preview_coupons

    res = apply_optimal_preview_coupons(p_ws)
    if res.get("applied"):
        return True
    after_t = res.get("after_payable_yuan")
    if before_total is not None and after_t is not None:
        return float(after_t) < float(before_total)
    return False


def advance_tab_to_preview(
    db: SimingDB,
    tid: str,
    menu_url: str = "",
    port: int = 9222,
    auto_apply_coupon: bool = True,
    location_override: str = "",
    address_hint: str = "",
) -> dict[str, Any]:
    """在已加购的 9222 后台门店标签中点「去结算」进入结算预览页，校验收货地址（防落回公司/外卖柜）、勾选红包并读取真实实付明细（绝不点「提交订单」）。"""
    if os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return {"ok": False, "reason": "offline"}
    base = resolve_chromium_cdp_base(port=port)
    if not base or not base.get("ws_browser") or not tid:
        return {"ok": False, "reason": "cdp_unavailable"}

    from siming.meituan_agent import (
        _match_best_addstore_entry,
        resolve_meituan_delivery_target,
        sync_meituan_h5_location,
    )
    from siming.meituan_cart_live import (
        MINDEX_PREVIEW_URL,
        PREVIEW_URL_TMPL,
        RESTAURANT_MARKER,
        _expand_mindex_preview_items,
        _select_mindex_address,
        _select_subtrade_address,
        ensure_mindex_required_products,
    )

    loc_target = resolve_meituan_delivery_target(db, location_override=location_override)
    close_stale_preview_tab(db, port=port, keep_tid=tid)
    page_ws = base["ws_browser"].split("/devtools/")[0] + f"/devtools/page/{tid}"
    try:
        with LocalWsCdpClient(page_ws, timeout=24.0) as p_ws:
            _setup_mobile_session(p_ws)
            sync_info = sync_meituan_h5_location(p_ws, loc_target)
            matched_addr = sync_info.get("matched_saved") or (
                _match_best_addstore_entry(sync_info.get("addstore") or [], loc_target)
                if sync_info.get("addstore")
                else None
            )
            if matched_addr:
                addr_token = " ".join(
                    x for x in (str(matched_addr.get("poi") or "").strip(), str(matched_addr.get("address") or "").strip()) if x
                )
            else:
                addr_token = address_hint or str(loc_target.get("match_hint") or loc_target.get("label") or "")

            cur_url0 = str(_eval_json(p_ws, "JSON.stringify({url: location.href})").get("url") or "")
            is_subtrade = RESTAURANT_MARKER in cur_url0 or RESTAURANT_MARKER in (menu_url or "")

            def _click_go_to_preview() -> dict[str, Any]:
                pos = _eval_json(
                    p_ws,
                    r"""(() => {
                        const btn = Array.from(document.querySelectorAll('[class*="goToPreview"]')).find(e => e.getBoundingClientRect().width > 0)
                            || Array.from(document.querySelectorAll('mt-view, mt-text, div, span, button')).find(e => (e.innerText || '').trim() === '去结算' && e.getBoundingClientRect().width > 0);
                        if (!btn) return JSON.stringify({clicked: false, reason: 'no_goToPreview'});
                        const t = (btn.innerText || '').trim();
                        if (/差.*起送|起送$/.test(t) && !t.includes('去结算')) {
                            return JSON.stringify({clicked: false, reason: 'below_min_order', text: t});
                        }
                        try { btn.scrollIntoView({block: 'center'}); } catch (e) {}
                        const r = btn.getBoundingClientRect();
                        return JSON.stringify({clicked: true, x: r.x + r.width / 2, y: r.y + r.height / 2, text: t});
                    })()""",
                )
                if pos.get("clicked"):
                    _cdp_click_xy(p_ws, float(pos["x"]), float(pos["y"]))
                return pos

            if not is_subtrade and "mindex/menu" in cur_url0:
                if ensure_mindex_required_products(p_ws, force_dom_check=False):
                    time.sleep(0.8)

            go1 = _click_go_to_preview()
            if not go1.get("clicked") and go1.get("reason") == "below_min_order":
                return {
                    "ok": False,
                    "reason": "below_min_order",
                    "error": f"当前购物车暂不可去结算（{go1.get('text') or '未满起送价'}）",
                    "target_id": tid,
                }

            retried_tableware = False
            retried_js_go = False
            preview_text = ""
            preview_url = ""
            deadline = time.monotonic() + (9.5 if go1.get("clicked") else 0.0)
            while time.monotonic() < deadline:
                time.sleep(0.55)
                snap = _eval_json(
                    p_ws,
                    "JSON.stringify({url: location.href, title: document.title || '', text: (document.body && document.body.innerText || '').trim()})",
                )
                u = str(snap.get("url") or "")
                t = str(snap.get("title") or "")
                txt = str(snap.get("text") or "")

                if _CAPTCHA_URL_RE.search(u) or _CAPTCHA_TXT_RE.search(txt) or _LOGIN_URL_RE.search(u) or _LOGIN_TXT_RE.search(txt):
                    gate = detect_compliance_gate(u, t, txt) or {"gate_kind": "captcha_verification", "reason": "触发美团登录或安全验证"}
                    return {"ok": False, "gated": True, "gate": gate, "target_id": tid, "kept_tab_open": True}

                if _is_meituan_preview_url(u) and "努力加载中" not in txt and ("合计" in txt or "提交订单" in txt):
                    preview_url = u
                    preview_text = txt
                    break

                if not retried_tableware and "mindex/menu" in u and (deadline - time.monotonic()) < 7.2:
                    retried_tableware = True
                    if ensure_mindex_required_products(p_ws, force_dom_check=True):
                        time.sleep(0.8)
                        p_ws.call("Runtime.evaluate", {"expression": "document.querySelector('[class*=\"goToPreview\"]')?.click()", "returnByValue": True})
                        deadline = max(deadline, time.monotonic() + 7.0)
                        continue
                if not retried_js_go and "mindex/menu" in u and (deadline - time.monotonic()) < 6.4:
                    retried_js_go = True
                    p_ws.call("Runtime.evaluate", {"expression": "document.querySelector('[class*=\"goToPreview\"]')?.click()", "returnByValue": True})

            # SPA 过渡未渲染直航兜底
            if not preview_text:
                if is_subtrade:
                    qs_m = urllib.parse.parse_qs(urllib.parse.urlsplit(menu_url or cur_url0).query)
                    pid_s = (qs_m.get("poi_id_str") or [""])[0]
                    fb_url = PREVIEW_URL_TMPL.format(poi_id_str=urllib.parse.quote(pid_s, safe="")) if pid_s else ""
                else:
                    fb_url = MINDEX_PREVIEW_URL
                if fb_url:
                    p_ws.call("Page.navigate", {"url": fb_url})
                    time.sleep(5.5 if is_subtrade else 4.5)
                    _setup_mobile_session(p_ws)
                    snap_fb = _eval_json(
                        p_ws,
                        "JSON.stringify({url: location.href, title: document.title || '', text: (document.body && document.body.innerText || '').trim()})",
                    )
                    u_fb = str(snap_fb.get("url") or "")
                    txt_fb = str(snap_fb.get("text") or "")
                    if _is_meituan_preview_url(u_fb) and ("合计" in txt_fb or "提交订单" in txt_fb):
                        preview_url = u_fb
                        preview_text = txt_fb

            if not preview_text:
                return {
                    "ok": False,
                    "reason": "preview_timeout",
                    "error": "未能进入美团结算预览页（可能该店存在必选项或起送价限制，已保留门店标签页）。",
                    "target_id": tid,
                }

            # 校正收货地址（防止默认落回公司或美团外卖柜）
            if addr_token:
                try:
                    if "package/preview" in preview_url:
                        _select_subtrade_address(p_ws, addr_token, address_hint)
                    else:
                        _select_mindex_address(p_ws, addr_token, address_hint, loc_target)
                    snap_addr = _eval_json(
                        p_ws,
                        "JSON.stringify({url: location.href, text: (document.body && document.body.innerText || '').trim()})",
                    )
                    if _is_meituan_preview_url(str(snap_addr.get("url") or "")):
                        preview_url = str(snap_addr["url"])
                        preview_text = str(snap_addr.get("text") or preview_text)
                except Exception:
                    pass

            if "package/preview" not in preview_url and _expand_mindex_preview_items(p_ws):
                snap_exp = _eval_json(
                    p_ws,
                    "JSON.stringify({url: location.href, text: (document.body && document.body.innerText || '').trim()})",
                )
                if _is_meituan_preview_url(str(snap_exp.get("url") or "")):
                    preview_text = str(snap_exp.get("text") or preview_text)

            bill = parse_meituan_preview_bill(preview_text)
            coupon_opt: dict[str, Any] = {}
            if auto_apply_coupon:
                from siming.meituan_coupons import apply_optimal_preview_coupons

                sk = "subtrade" if "package/preview" in preview_url else "mindex"
                coupon_opt = apply_optimal_preview_coupons(p_ws, store_kind=sk, loc_target=loc_target)
                if "package/preview" not in preview_url:
                    _expand_mindex_preview_items(p_ws)
                snap2 = _eval_json(
                    p_ws,
                    "JSON.stringify({url: location.href, text: (document.body && document.body.innerText || '').trim()})",
                )
                if _is_meituan_preview_url(str(snap2.get("url") or "")):
                    preview_url = str(snap2["url"])
                    preview_text = str(snap2.get("text") or preview_text)
                    bill = parse_meituan_preview_bill(preview_text)

            db.set_meta(ACTIVE_PREVIEW_TID_KEY, tid)
            if menu_url:
                db.set_meta(ACTIVE_PREVIEW_MENU_KEY, menu_url)

            return {
                "ok": True,
                "preview_ready": True,
                "gated": False,
                "preview_target_id": tid,
                "preview_url": preview_url,
                "menu_url": menu_url,
                "bill": bill,
                "final_payable_yuan": bill.get("final_payable_yuan"),
                "delivery_address": bill.get("delivery_address") or "",
                "eta_text": bill.get("eta_text") or "",
                "coupon_text": bill.get("coupon_text") or "",
                "coupon_opt": coupon_opt,
            }
    except Exception as exc:
        return {"ok": False, "reason": "cdp_error", "error": str(exc)[:200], "target_id": tid}


def stage_meituan_payment_preview(
    db: SimingDB,
    cart_res: dict[str, Any],
    shop: str,
    item_spec: str,
    deeplink: str,
    backup_choice: str = "",
    port: int = 9222,
    preview_fn: Optional[Callable[[SimingDB, str, str], dict[str, Any]]] = None,
    location_override: str = "",
) -> dict[str, Any]:
    """将已加购的门店推进到结算预览页核验真实实付总价，并签发 10m 有效 `meituan_submit_pay` 付款许可确认卡。"""
    tid = str(cart_res.get("target_id") or "")
    menu_url = str(cart_res.get("menu_url") or cart_res.get("url") or "")
    loc_ov = str(location_override or cart_res.get("loc_override") or "").strip()

    prev = (
        preview_fn(db, tid, menu_url)
        if preview_fn is not None
        else advance_tab_to_preview(
            db,
            tid,
            menu_url=menu_url,
            port=port,
            location_override=loc_ov,
            address_hint=str(cart_res.get("delivery_poi") or ""),
        )
    )
    if prev.get("gated"):
        gate_r = (prev.get("gate") or {}).get("reason") or "需登录或滑块验证"
        return {
            "ok": False,
            "gated": True,
            "gate": prev.get("gate"),
            "reply": (
                f"🛡️ **美团结算页触发合规门禁**（目标：**{shop}**）\n"
                f"• 原因：{gate_r}\n"
                f"• 司命已原样保留桌面 Chromium 后台标签页（绝不绕过验证），完成验证后重发即可；或手机美团唤端：`{deeplink}`"
            ),
        }

    if not prev.get("ok") or not prev.get("preview_ready"):
        return {
            "ok": False,
            "preview_failed": True,
            "reason": str(prev.get("reason") or "preview_failed"),
            "error": str(prev.get("error") or "未能进入结算预览页"),
        }

    bill = prev.get("bill") or {}
    final_yuan = prev.get("final_payable_yuan")
    if final_yuan is None:
        final_yuan = bill.get("final_payable_yuan")
    if final_yuan is None or float(final_yuan) <= 0:
        return {
            "ok": False,
            "preview_failed": True,
            "reason": "missing_payable",
            "error": "未能从结算预览页读到明确实付总额，为保资金安全未签发付款确认卡。",
        }

    policy = load_policy(db.paths)
    max_yuan = float((policy.get("spending_caps") or {}).get("meituan_single_order_yuan", 120.0))
    if float(final_yuan) > max_yuan:
        # 严格拦截签发自动付款确认卡（零扣款、不提交订单），同时保留购物车现场供用户在手机美团自行核对或调高限额
        return {
            "ok": False,
            "over_cap": True,
            "final_payable_yuan": round(float(final_yuan), 2),
            "delivery_address": str(bill.get("delivery_address") or cart_res.get("delivery_poi") or ""),
            "preview_target_id": str(prev.get("preview_target_id") or tid),
            "reply": (
                f"🛑 结算页真实实付 **¥{float(final_yuan):g}** 超过美团单笔自动支付安全上限（¥{max_yuan:.0f}），已按红线拦截签发代付卡（未提交、未扣款）。\n"
                f"• 已为你保留购物车与后台预览页（配送：**{bill.get('delivery_address') or cart_res.get('delivery_poi') or '见预览页'}**），可直接在手机美团购物车用 App 神券结算：`{deeplink}`\n"
                "• 如需允许司命代付更高金额，可修改 `~/.config/siming/policy.yaml` 的 `spending_caps.meituan_single_order_yuan`。"
            ),
        }

    final_shop = str(bill.get("shop") or cart_res.get("shop") or shop).strip()
    delivery_address = str(bill.get("delivery_address") or cart_res.get("delivery_poi") or "").strip()
    eta_text = str(bill.get("eta_text") or "").strip()
    coupon_text = str(bill.get("coupon_text") or "").strip()
    store_voucher_text = str(bill.get("store_voucher_text") or "").strip()
    delivery_coupon_text = str(bill.get("delivery_coupon_text") or "").strip()
    coupon_opt = prev.get("coupon_opt") or cart_res.get("coupon_opt") or {}
    items_list = list(bill.get("items") or [])
    if items_list:
        items_desc = " + ".join(
            f"{it['name']}" + (f"（{it['spec'][:24]}）" if it.get("spec") else "") + (f"×{it['qty']}" if int(it.get("qty") or 1) > 1 else "")
            for it in items_list[:6]
        )
    else:
        added_list = cart_res.get("added_dishes") or []
        items_desc = " + ".join(added_list) if added_list else str(cart_res.get("dish") or item_spec)

    fee_parts: list[str] = []
    if bill.get("packing_fee_yuan") is not None:
        fee_parts.append(f"打包 ¥{float(bill['packing_fee_yuan']):g}")
    if bill.get("delivery_fee_yuan") is not None:
        fee_parts.append(f"配送 ¥{float(bill['delivery_fee_yuan']):g}")
    if coupon_text and ("-¥" in coupon_text or "-￥" in coupon_text or "已减" in coupon_text or "可用" in coupon_text):
        fee_parts.append(f"红包：{coupon_text}")
    if store_voucher_text and ("-¥" in store_voucher_text or "-￥" in store_voucher_text):
        fee_parts.append(f"商家券：{store_voucher_text}")
    if delivery_coupon_text and ("-¥" in delivery_coupon_text or "-￥" in delivery_coupon_text):
        fee_parts.append(f"配送券：{delivery_coupon_text}")
    if bill.get("discount_yuan") and float(bill["discount_yuan"]) > 0 and not any("红包：" in p and "-¥" in p for p in fee_parts):
        fee_parts.append(f"共省 ¥{float(bill['discount_yuan']):g}")
    fee_note = f"（{' · '.join(fee_parts)}）" if fee_parts else ""

    # 汇总最优红包策略回执、防漏券告警与凑单省钱提示
    coupon_strategy_lines: list[str] = []
    chosen_rp = coupon_opt.get("red_packet_chosen") if isinstance(coupon_opt, dict) else None
    chosen_sv = coupon_opt.get("store_voucher_chosen") if isinstance(coupon_opt, dict) else None
    chosen_dv = coupon_opt.get("delivery_coupon_chosen") if isinstance(coupon_opt, dict) else None
    saved_opt = float(coupon_opt.get("saved_yuan") or 0.0) if isinstance(coupon_opt, dict) else 0.0
    applied_labels: list[str] = []
    if isinstance(chosen_rp, dict) and chosen_rp.get("short_label"):
        applied_labels.append(f"红包 {chosen_rp['short_label']}")
    if isinstance(chosen_sv, dict) and chosen_sv.get("short_label"):
        applied_labels.append(f"商家券 {chosen_sv['short_label']}")
    if isinstance(chosen_dv, dict) and chosen_dv.get("short_label"):
        applied_labels.append(f"配送券 {chosen_dv['short_label']}")
    if applied_labels and saved_opt > 0:
        coupon_strategy_lines.append(f"• 🎫 红包优选：已自动勾选 {' + '.join(applied_labels)}，本单立省 **¥{saved_opt:g}**")
    elif bill.get("discount_yuan") and float(bill["discount_yuan"]) > 0 and ("-¥" in coupon_text or "-￥" in coupon_text):
        coupon_strategy_lines.append(f"• 🎫 红包已生效：美团红包 {coupon_text}（本单共省 ¥{float(bill['discount_yuan']):g}）")

    unapplied_warn = str(coupon_opt.get("warning") or "") if isinstance(coupon_opt, dict) else ""
    if not unapplied_warn and "未选红包" in coupon_text and "可用" in coupon_text:
        unapplied_warn = f"结算页提示「{coupon_text}」暂未能自动勾选，付款前建议核对美团 App 勾选以免多花钱！"
    if unapplied_warn:
        coupon_strategy_lines.append(f"• ⚠️ **红包未生效提醒**：{unapplied_warn}")

    near_tip = str(coupon_opt.get("near_threshold_tip") or "") if isinstance(coupon_opt, dict) else ""
    if not near_tip and items_list:
        try:
            from siming.meituan_coupons import infer_order_category
            from siming.meituan_speculate import compute_coupon_pricing, load_cached_account_coupons

            cached_acc = load_cached_account_coupons(db, max_age_min=720.0)
            if cached_acc:
                items_sub = round(
                    sum(
                        float(it.get("line_total_yuan") if it.get("line_total_yuan") is not None else float(it.get("price_yuan") or 0.0) * int(it.get("qty") or 1))
                        for it in items_list
                    ),
                    2,
                )
                sk_now = str(prev.get("store_kind") or cart_res.get("store_kind") or ("subtrade" if "package/preview" in str(prev.get("preview_url") or "") else "mindex"))
                o_cat = infer_order_category(final_shop, store_kind=sk_now, item_names=[str(it.get("name") or "") for it in items_list])
                cp_chk = compute_coupon_pricing(
                    shop_name=final_shop,
                    subtotal=items_sub,
                    delivery_fee=float(bill.get("delivery_fee_yuan") or 0.0),
                    packing_fee=float(bill.get("packing_fee_yuan") or 0.0),
                    account_coupons=cached_acc,
                    order_category=o_cat,
                )
                if cp_chk.get("near_coupon_tip"):
                    near_tip = str(cp_chk["near_coupon_tip"])
        except Exception:
            pass
    expandable_hint = str(coupon_opt.get("expandable_hint") or "") if isinstance(coupon_opt, dict) else ""
    if expandable_hint:
        coupon_strategy_lines.append(f"• 🎈 神券膨胀提醒：{expandable_hint}")

    if near_tip:
        coupon_strategy_lines.append(f"• 💡 省钱凑单提示：{near_tip}")

    params = {
        "shop": final_shop,
        "item_spec": items_desc,
        "items": items_list,
        "final_payable_yuan": round(float(final_yuan), 2) if final_yuan is not None else None,
        "delivery_address": delivery_address,
        "loc_override": loc_ov,
        "recipient_masked": str(bill.get("recipient_masked") or ""),
        "eta_text": eta_text,
        "coupon_text": coupon_text,
        "store_voucher_text": store_voucher_text,
        "delivery_coupon_text": delivery_coupon_text,
        "discount_yuan": bill.get("discount_yuan"),
        "coupon_opt_saved_yuan": saved_opt,
        "coupon_opt_debug": coupon_opt.get("debug") if isinstance(coupon_opt, dict) else {},
        "preview_target_id": str(prev.get("preview_target_id") or tid),
        "preview_url": str(prev.get("preview_url") or cart_res.get("preview_url") or ""),
        "store_kind": str(prev.get("store_kind") or cart_res.get("store_kind") or ""),
        "menu_url": menu_url,
        "deeplink": deeplink,
        "max_yuan": max_yuan,
    }
    app = create_approval(
        db,
        action="meituan_submit_pay",
        target_id=f"MTPAY-{re.sub(r'[^A-Za-z0-9\u4e00-\u9fff]+', '', final_shop)[:12] or 'ORDER'}",
        params=params,
        ttl_minutes=10,
    )
    db.record_human_use("meituan_agent", "stage_pay_preview", ref_id=app["token"])

    pay_str = f"¥{float(final_yuan):g}" if final_yuan is not None else "见结算页"
    lines = [
        f"💳 **美团外卖付款许可确认卡** [`{app['token']}`]（10 分钟内有效 · 尚未扣款）",
        f"• 门店：**{final_shop}**",
        f"• 商品：`{items_desc}`",
    ]
    if delivery_address:
        eta_suffix = f" · {eta_text}" if eta_text else ""
        lines.append(f"• 📍 配送：**{delivery_address}**{eta_suffix}")
    lines.append(f"• 💰 实付核验：**{pay_str}**{fee_note}（单笔安全上限 ¥{max_yuan:.0f}）")
    lines.extend(coupon_strategy_lines)
    if backup_choice:
        lines.append(f"• 备选门店：{backup_choice}")
    if _is_hermes_handoff_ready(policy):
        h_cfg = _get_hermes_handoff_cfg(policy)
        lines.append(
            f"• 支付方式：已备好美团结算页，获你许可后自动提交订单，并经 Hermes 机密通道（`{h_cfg['provider']}/{h_cfg['model']}`）完成收银台支付与实单核验"
        )
    else:
        lines.append("• 支付方式：已备好美团结算页，获你许可后自动提交并走美团收银台/极速支付付款")
    lines.append(
        f"👉 确认下单并支付请回复 **`确认支付`** 或 **`apply {app['token']}`**（引用本条回 `确认` 也可）；放弃请回 **`取消 {app['token']}`**。"
    )

    return {
        "ok": True,
        "approval_token": app["token"],
        "action": "meituan_submit_pay",
        "shop": final_shop,
        "item_spec": items_desc,
        "final_payable_yuan": params["final_payable_yuan"],
        "delivery_address": delivery_address,
        "preview_target_id": params["preview_target_id"],
        "reply": "\n".join(lines),
    }


def _get_hermes_handoff_cfg(policy: dict[str, Any]) -> dict[str, Any]:
    """读取 `~/.config/siming/policy.yaml` 中美团支付 Hermes 机密通道配置（不读取任何密码文件）。"""
    mt_cfg = policy.get("meituan_pay") if isinstance(policy.get("meituan_pay"), dict) else {}
    raw = mt_cfg.get("hermes_handoff") if isinstance(mt_cfg.get("hermes_handoff"), dict) else {}
    ch = policy.get("channels") if isinstance(policy.get("channels"), dict) else {}
    primary_model = str(raw.get("model") or "phala/deepseek-v4.1-flash-1017-uncensored").strip()
    raw_fb = raw.get("fallback_models")
    fallback_models: list[str] = []
    if isinstance(raw_fb, list):
        for m in raw_fb:
            ms = str(m or "").strip()
            if ms and ms != primary_model and ms not in fallback_models:
                fallback_models.append(ms)
    return {
        "enabled": bool(raw.get("enabled", False)),
        "provider": str(raw.get("provider") or "RedPill").strip(),
        "model": primary_model,
        "fallback_models": fallback_models,
        "instruction_file": str(raw.get("instruction_file") or "").strip(),
        "max_turns": max(4, min(40, int(raw.get("max_turns") or 20))),
        "timeout_sec": max(30, min(300, int(raw.get("timeout_sec") or 150))),
        "hermes_bin": str(ch.get("hermes_bin") or str(Path.home() / ".local" / "bin" / "hermes")).strip(),
    }


def _is_hermes_handoff_ready(policy: dict[str, Any]) -> bool:
    """仅当显式启用且附加指令文件与 hermes 可执行文件均存在时就绪（只判存在性，绝不读文件内容）。"""
    cfg = _get_hermes_handoff_cfg(policy)
    if not cfg["enabled"]:
        return False
    instr = Path(cfg["instruction_file"]).expanduser()
    hbin = Path(cfg["hermes_bin"]).expanduser()
    return bool(instr.is_file() and hbin.is_file())


def _extract_remaining_time(text: str) -> str:
    m = re.search(r"(?:交易剩余时间|剩余支付时间)\s*(\d{1,2}:\d{2})", text or "")
    return m.group(1) if m else ""


def _extract_order_id_from_cashier(url: str, text: str) -> str:
    m_txt = re.search(r"-(\d{14,24})\b", text or "")
    if m_txt:
        return m_txt.group(1)
    qs = urllib.parse.parse_qs(urllib.parse.urlsplit(url or "").query)
    tn = (qs.get("tradeno") or [""])[0].strip()
    return tn


def _read_cashier_state_on_ws(p_ws: LocalWsCdpClient) -> dict[str, Any]:
    snap = _eval_json(
        p_ws,
        r"""(() => {
            const txt = (document.body && document.body.innerText || '').trim();
            const kb = document.querySelector('.keyboard-key');
            const kbRect = kb ? kb.getBoundingClientRect() : null;
            const keypadOpen = Boolean(kbRect && kbRect.width > 100 && kbRect.height > 80);
            const dots = Array.from(document.querySelectorAll('.password-area i'));
            const filledDots = dots.filter(el => el.style.display !== 'none').length;
            return JSON.stringify({
                url: location.href,
                title: document.title || '',
                text: txt.slice(0, 2800),
                keypad_open: keypadOpen,
                filled_dots: filledDots,
                total_dots: dots.length,
            });
        })()""",
    )
    u = str(snap.get("url") or "")
    t = str(snap.get("title") or "")
    txt = str(snap.get("text") or "")
    cls = classify_cashier_or_order_state(u, t, txt)
    return {
        "ok": bool(u),
        "url": u,
        "title": t,
        "text": txt,
        "state": cls["state"],
        "reason": cls["reason"],
        "payable_yuan": extract_cashier_payable_yuan(txt),
        "remaining_time": _extract_remaining_time(txt),
        "keypad_open": bool(snap.get("keypad_open")),
        "filled_dots": int(snap.get("filled_dots") or 0),
        "total_dots": int(snap.get("total_dots") or 0),
        "has_hard_2fa": bool(_HARD_2FA_RE.search(txt)),
        "has_captcha": cls["state"] in ("CAPTCHA", "LOGIN"),
        "paid": cls["state"] == "PAID",
    }


def cdp_cashier_snapshot(target_id: str, port: int = 9222) -> dict[str, Any]:
    """只读采集指定美团收银台标签页实时状态（金额、剩余时间、密码键盘状态与已填圆点数）。"""
    base = resolve_chromium_cdp_base(port=port)
    if not base or not base.get("ws_browser") or not target_id:
        return {"ok": False, "reason": "cdp_unavailable"}
    page_ws = base["ws_browser"].split("/devtools/")[0] + f"/devtools/page/{target_id}"
    try:
        with LocalWsCdpClient(page_ws, timeout=18.0) as p_ws:
            _setup_mobile_session(p_ws)
            return _read_cashier_state_on_ws(p_ws)
    except Exception as exc:
        return {"ok": False, "reason": "cdp_error", "error": str(exc)[:180]}


def cdp_cashier_select_method(target_id: str, keyword: str, port: int = 9222) -> dict[str, Any]:
    """在美团收银台点选指定关键词对应的支付方式行（如 美团月付 / 余额 / 储蓄卡），并返回刷新后快照。"""
    kw = (keyword or "").strip()
    if not kw:
        return {"ok": False, "reason": "empty_keyword"}
    base = resolve_chromium_cdp_base(port=port)
    if not base or not base.get("ws_browser") or not target_id:
        return {"ok": False, "reason": "cdp_unavailable"}
    page_ws = base["ws_browser"].split("/devtools/")[0] + f"/devtools/page/{target_id}"
    try:
        with LocalWsCdpClient(page_ws, timeout=18.0) as p_ws:
            _setup_mobile_session(p_ws)
            sel = _eval_json(
                p_ws,
                r"""(() => {
                    const kw = KW;
                    const primary = Array.from(document.querySelectorAll('.mtpay-listview-item, .pay-type-section')).filter(el => {
                        const t = (el.innerText || '').trim();
                        const r = el.getBoundingClientRect();
                        return r.height >= 26 && r.height <= 165 && r.width >= 140 && t.length <= 120 && t.includes(kw) && !t.includes('确认支付');
                    });
                    const rows = primary.length ? primary : Array.from(document.querySelectorAll('div, li, label')).filter(el => {
                        const t = (el.innerText || '').trim();
                        const r = el.getBoundingClientRect();
                        return r.top >= 60 && r.bottom <= 840 && r.height >= 26 && r.height <= 165 && r.width >= 140 && t.length <= 100 && t.includes(kw) && !t.includes('确认支付');
                    });
                    if (!rows.length) return JSON.stringify({selected: false});
                    rows.sort((a, b) => (a.getBoundingClientRect().height - b.getBoundingClientRect().height));
                    const target = rows[0];
                    try { target.scrollIntoView({block: 'center'}); } catch (e) {}
                    target.click();
                    return JSON.stringify({selected: true, text: (target.innerText || '').trim().slice(0, 60)});
                })()""".replace("KW", json.dumps(kw, ensure_ascii=False)),
            )
            time.sleep(0.7)
            snap = _read_cashier_state_on_ws(p_ws)
            snap["selected"] = bool(sel.get("selected"))
            snap["selected_text"] = str(sel.get("text") or "")
            return snap
    except Exception as exc:
        return {"ok": False, "reason": "cdp_error", "error": str(exc)[:180]}


def cdp_cashier_open_keypad(target_id: str, port: int = 9222, settle_sec: float = 2.5) -> dict[str, Any]:
    """用 JS button.click() 点击收银台「确认支付」（绕过 container-mark 遮罩吃鼠标事件），并等待键盘滑入动画稳定。"""
    base = resolve_chromium_cdp_base(port=port)
    if not base or not base.get("ws_browser") or not target_id:
        return {"ok": False, "reason": "cdp_unavailable"}
    page_ws = base["ws_browser"].split("/devtools/")[0] + f"/devtools/page/{target_id}"
    try:
        with LocalWsCdpClient(page_ws, timeout=22.0) as p_ws:
            _setup_mobile_session(p_ws)
            cur = _read_cashier_state_on_ws(p_ws)
            if cur.get("has_captcha") or cur.get("has_hard_2fa") or cur.get("paid"):
                return cur
            if not cur.get("keypad_open"):
                clicked = _eval_json(
                    p_ws,
                    r"""(() => {
                        const btn = document.querySelector('button.btn-half.btn.load')
                            || Array.from(document.querySelectorAll('button, div, span, a')).filter(el => {
                                const t = (el.innerText || '').replace(/\s+/g, '').trim();
                                const r = el.getBoundingClientRect();
                                return r.height >= 34 && r.width >= 120 && /^(?:确认支付|立即支付|确认付款|免密支付|确认极速支付)(?:[¥￥]?\d+(?:\.\d{1,2})?元?)?$/.test(t);
                            }).sort((a, b) => b.getBoundingClientRect().top - a.getBoundingClientRect().top)[0];
                        if (!btn) return JSON.stringify({clicked: false});
                        btn.click();
                        return JSON.stringify({clicked: true, text: (btn.innerText || '').trim()});
                    })()""",
                )
                if not clicked.get("clicked"):
                    cur["clicked"] = False
                    return cur
                deadline = time.monotonic() + 4.5
                while time.monotonic() < deadline:
                    time.sleep(0.45)
                    cur = _read_cashier_state_on_ws(p_ws)
                    if cur.get("keypad_open") or cur.get("paid") or cur.get("has_captcha") or cur.get("has_hard_2fa"):
                        break
                if cur.get("keypad_open") and settle_sec > 0:
                    time.sleep(float(settle_sec))
                    cur = _read_cashier_state_on_ws(p_ws)
            cur["clicked"] = True
            return cur
    except Exception as exc:
        return {"ok": False, "reason": "cdp_error", "error": str(exc)[:180]}


def cdp_cashier_touch_pin(target_id: str, pin_digits: str, port: int = 9222) -> dict[str, Any]:
    """在美团收银台数字密码键盘上以 CDP 触摸事件逐位录入 6 位密码并等待结果（绝不打印、回显或返回任何密码位）。"""
    pin = str(pin_digits or "").strip()
    if not re.fullmatch(r"\d{6}", pin):
        return {"ok": False, "paid": False, "reason": "invalid_pin_format"}
    base = resolve_chromium_cdp_base(port=port)
    if not base or not base.get("ws_browser") or not target_id:
        return {"ok": False, "paid": False, "reason": "cdp_unavailable"}
    page_ws = base["ws_browser"].split("/devtools/")[0] + f"/devtools/page/{target_id}"
    try:
        with LocalWsCdpClient(page_ws, timeout=30.0) as p_ws:
            _setup_mobile_session(p_ws)
            p_ws.call("Emulation.setTouchEmulationEnabled", {"enabled": True, "maxTouchPoints": 5})

            cur = _read_cashier_state_on_ws(p_ws)
            if cur.get("has_captcha") or cur.get("has_hard_2fa"):
                return {"ok": False, "paid": False, "gated": True, "state": cur["state"], "reason": cur["reason"]}
            if cur.get("paid"):
                return {"ok": True, "paid": True, "state": "PAID", "payable_yuan": cur.get("payable_yuan"), "url": cur.get("url")}
            if not cur.get("keypad_open"):
                return {"ok": False, "paid": False, "reason": "keypad_not_open", "state": cur.get("state")}

            # 若已有残余已填圆点，先用删除键触摸清空
            for _ in range(min(6, int(cur.get("filled_dots") or 0))):
                del_pos = _eval_json(
                    p_ws,
                    r"""(() => {
                        const el = document.querySelector('.keyboard-delete');
                        if (!el) return JSON.stringify({ok: false});
                        const r = el.getBoundingClientRect();
                        if (r.width < 10 || r.height < 10) return JSON.stringify({ok: false});
                        return JSON.stringify({ok: true, x: r.x + r.width / 2, y: r.y + r.height / 2});
                    })()""",
                )
                if not del_pos.get("ok"):
                    break
                _cdp_touch_tap(p_ws, float(del_pos["x"]), float(del_pos["y"]))
                time.sleep(0.22)

            # 逐位录入（绝不在任何变量日志/返回值中出现数字本身）
            for pos_idx, digit in enumerate(pin, start=1):
                key_pos: dict[str, Any] = {}
                wait_until = time.monotonic() + 2.5
                while time.monotonic() < wait_until:
                    key_pos = _eval_json(
                        p_ws,
                        r"""(() => {
                            const d = DIGIT;
                            const el = document.querySelector('.keyboard-key i[data-key="' + d + '"]')
                                || Array.from(document.querySelectorAll('.keyboard-key i')).find(n => (n.innerText || '').trim() === d);
                            if (!el) return JSON.stringify({ok: false, reason: 'key_not_found'});
                            const r = el.getBoundingClientRect();
                            if (r.width < 20 || r.height < 20) return JSON.stringify({ok: false, reason: 'key_zero_rect'});
                            const x = r.x + r.width / 2;
                            const y = r.y + r.height / 2;
                            const hit = document.elementFromPoint(x, y);
                            const hitOk = Boolean(hit && (hit === el || el.contains(hit) || (hit.getAttribute && hit.getAttribute('data-key') === d)));
                            return JSON.stringify({ok: hitOk, x, y, reason: hitOk ? '' : 'animating_or_occluded'});
                        })()""".replace("DIGIT", json.dumps(digit)),
                    )
                    if key_pos.get("ok"):
                        break
                    time.sleep(0.25)

                if not key_pos.get("ok"):
                    return {"ok": False, "paid": False, "reason": f"key_locate_failed_at_pos_{pos_idx}"}

                _cdp_touch_tap(p_ws, float(key_pos["x"]), float(key_pos["y"]))
                time.sleep(0.32)

                if pos_idx < 6:
                    after = _read_cashier_state_on_ws(p_ws)
                    if after.get("filled_dots") == pos_idx - 1:
                        # 仅重试一次防止动画末帧漏触
                        _cdp_touch_tap(p_ws, float(key_pos["x"]), float(key_pos["y"]))
                        time.sleep(0.35)
                        after = _read_cashier_state_on_ws(p_ws)
                    if after.get("filled_dots") != pos_idx:
                        return {
                            "ok": False,
                            "paid": False,
                            "reason": f"dot_count_mismatch_at_pos_{pos_idx}",
                            "filled_dots": after.get("filled_dots"),
                        }

            # 第 6 位输完后圆点会自动清零并提交；轮询等待交易成功/跳转/失败
            poll_deadline = time.monotonic() + 14.0
            last_s = _read_cashier_state_on_ws(p_ws)
            while time.monotonic() < poll_deadline:
                time.sleep(0.8)
                try:
                    last_s = _read_cashier_state_on_ws(p_ws)
                except Exception:
                    continue
                if last_s.get("paid"):
                    return {
                        "ok": True,
                        "paid": True,
                        "state": "PAID",
                        "payable_yuan": last_s.get("payable_yuan"),
                        "url": last_s.get("url"),
                        "reason": last_s.get("reason") or "订单已支付成功",
                    }
                if last_s.get("has_captcha") or last_s.get("has_hard_2fa"):
                    return {
                        "ok": False,
                        "paid": False,
                        "gated": True,
                        "state": last_s.get("state"),
                        "reason": last_s.get("reason"),
                        "url": last_s.get("url"),
                    }
                if last_s.get("state") == "PAY_FAILED":
                    return {
                        "ok": False,
                        "paid": False,
                        "state": "PAY_FAILED",
                        "reason": last_s.get("reason") or "支付失败",
                        "url": last_s.get("url"),
                        "remaining_time": last_s.get("remaining_time") or "",
                    }

            return {
                "ok": True,
                "paid": False,
                "state": last_s.get("state") or "UNKNOWN",
                "reason": last_s.get("reason") or "密码已提交但收银台尚未返回明确完成状态",
                "payable_yuan": last_s.get("payable_yuan"),
                "url": last_s.get("url"),
                "remaining_time": last_s.get("remaining_time") or "",
            }
    except Exception as exc:
        return {"ok": False, "paid": False, "reason": "cdp_error", "error": str(exc)[:180]}


def _olist_status_is_paid(status: str, card_block_text: str = "") -> bool:
    st = (status or "").strip()
    blk = card_block_text or ""
    if _OLIST_UNPAID_RE.search(st):
        return False
    if "立即支付" in blk or "去支付" in blk:
        return False
    return bool(_OLIST_PAID_RE.search(st))


def cdp_verify_olist_paid(shop: str, order_id: str = "", port: int = 9222) -> dict[str, Any]:
    """独立只读核验美团订单列表 `mindex/olist` 最新订单真实状态（绝不覆盖收银台标签页）。"""
    base = resolve_chromium_cdp_base(port=port)
    if not base or not base.get("ws_browser"):
        return {"ok": False, "found": False, "paid": False, "reason": "cdp_unavailable"}

    tabs = list_chromium_tabs(port=port)
    olist_tab = next((t for t in tabs if "mindex/olist" in str(t.get("url") or "")), None)
    temp_tid = ""
    tid = str(olist_tab.get("id") or "") if olist_tab else ""
    try:
        if not tid:
            with LocalWsCdpClient(base["ws_browser"], timeout=10.0) as b_ws:
                created = b_ws.call("Target.createTarget", {"url": "about:blank", "background": True})
                temp_tid = str(created.get("targetId") or "")
                tid = temp_tid
        if not tid:
            return {"ok": False, "found": False, "paid": False, "reason": "no_olist_tab"}

        page_ws = base["ws_browser"].split("/devtools/")[0] + f"/devtools/page/{tid}"
        with LocalWsCdpClient(page_ws, timeout=20.0) as p_ws:
            _setup_mobile_session(p_ws)
            p_ws.call("Page.navigate", {"url": MEITUAN_OLIST_URL})
            time.sleep(2.8)
            _setup_mobile_session(p_ws)
            snap = _eval_json(
                p_ws,
                "JSON.stringify({url: location.href, text: (document.body && document.body.innerText || '').slice(0, 1600), htmlHasOid: OID ? (document.documentElement.innerHTML || '').includes(OID) : false})".replace(
                    "OID", json.dumps(order_id or "")
                ),
            )
            txt = str(snap.get("text") or "")
            from siming.collectors.meituan_orders import parse_olist_text

            orders = parse_olist_text(txt)
            if not orders:
                return {"ok": True, "found": False, "paid": False, "reason": "no_orders"}
            o0 = orders[0]
            o_shop = str(o0.get("shop") or "")
            if shop and (shop[:3] not in o_shop and o_shop[:3] not in shop):
                return {"ok": True, "found": False, "paid": False, "reason": "shop_mismatch", "top_shop": o_shop}
            dt = datetime.strptime(str(o0["ordered_at"]), "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI_TZ)
            age_sec = abs((now_local() - dt).total_seconds())
            if age_sec > 900:
                return {"ok": True, "found": False, "paid": False, "reason": "order_too_old", "ordered_at": str(o0["ordered_at"])}

            # 提取首单区块文本（至下一单或前 240 字）检测是否有「立即支付」按钮
            head_block = txt[:240]
            st_str = str(o0.get("status") or "")
            paid_flag = _olist_status_is_paid(st_str, head_block)
            return {
                "ok": True,
                "found": True,
                "paid": paid_flag,
                "order_id_matched": bool(snap.get("htmlHasOid")),
                "shop": o_shop,
                "ordered_at": str(o0["ordered_at"]),
                "amount": f"¥{float(o0['amount']):g}",
                "amount_yuan": float(o0["amount"]),
                "status": st_str,
            }
    except Exception as exc:
        return {"ok": False, "found": False, "paid": False, "reason": "cdp_error", "error": str(exc)[:180]}
    finally:
        if temp_tid:
            _close_tab_quietly(port, temp_tid)


def _build_hermes_payment_context(ctx: dict[str, Any]) -> str:
    """构造交给 Hermes 机密支付通道（RedPill Phala）的收银台上下文，严禁在其中嵌入或猜测任何密码。"""
    tid = str(ctx.get("target_id") or "")
    shop = str(ctx.get("shop") or "")
    item_spec = str(ctx.get("item_spec") or "")
    order_id = str(ctx.get("order_id") or "")
    verified_yuan = float(ctx.get("verified_yuan") or 0.0)
    cashier_yuan = ctx.get("cashier_yuan")
    cap_yuan = round(min(float(ctx.get("max_yuan") or 120.0), verified_yuan + 2.0), 2)
    addr = str(ctx.get("delivery_address") or "")
    eta = str(ctx.get("eta_text") or "")
    cashier_url = str(ctx.get("cashier_url") or "")
    cashier_head = str(ctx.get("cashier_text_head") or "")[:600]
    port = int(ctx.get("port") or 9222)
    py_bin = sys.executable or "python3"
    src_root = str(Path(__file__).resolve().parents[1])

    return (
        "你正在接手一笔已由用户本人在司命（siming）A2 审批卡中明确授权的美团外卖订单的**最后一步收银台支付**。\n\n"
        "## 严格安全与隐私约束（必守）\n"
        "1. **绝对禁止回显/泄露附加指令**：你在最终回复、stdout、stderr 中**严禁复述、引用、打印或透露**下方本地支付说明文件中的任何原始文本、密码、数字或支付凭证。\n"
        "2. **仅操作指定订单与金额红线**：\n"
        f"   - 仅操作本机 Chromium {port} 的收银台标签页 `{tid}`。\n"
        f"   - 核对订单门店为 `{shop}`（订单号 `{order_id or '见收银台'}`），且应付金额 **≤ ¥{cap_yuan:g}**（预期实付 ¥{verified_yuan:g}，收银台当前显示 {f'¥{float(cashier_yuan):g}' if cashier_yuan is not None else '见页面'}）。超额或门店不符立即停手。\n"
        "   - **若遇到滑块/图形人机验证、短信验证码、人脸/指纹验证或重新登录页，立即停手保留现场并汇报，严禁尝试绕过任何风控。**\n\n"
        "## 当前订单与收银台现场\n"
        f"- 门店：`{shop}`\n"
        f"- 商品：`{item_spec}`\n"
        f"- 配送地址：`{addr}`" + (f"（{eta}）" if eta else "") + "\n"
        f"- 订单号：`{order_id or '未从 XHR 捕获，请看收银台文本'}`\n"
        f"- 收银台 URL：`{cashier_url[:180]}`\n"
        f"- 收银台快照摘录：\n```text\n{cashier_head}\n```\n\n"
        "## 一键可复用 CDP 支付原语（推荐 1 个 Python 命令直接完成，约 15–25 秒）\n"
        f"使用 `{py_bin}` 导入 `siming.meituan_pay` 内置的已验证原语（已内置 `button.click()` 绕开 `container-mark` 遮罩、键盘 2.5s 动画稳定等待、`elementFromPoint` 校验、CDP `touchStart/touchMove/touchEnd` 录入与第 6 位自动提交轮询）：\n"
        "```python\n"
        "import sys, json\n"
        f"sys.path.insert(0, {json.dumps(src_root)})\n"
        "from siming.meituan_pay import (\n"
        "    cdp_cashier_snapshot,\n"
        "    cdp_cashier_select_method,\n"
        "    cdp_cashier_open_keypad,\n"
        "    cdp_cashier_touch_pin,\n"
        "    cdp_verify_olist_paid,\n"
        ")\n"
        f"tid = {json.dumps(tid)}\n"
        f"snap = cdp_cashier_snapshot(tid, port={port})\n"
        "# 1) 若下方说明指定了特定支付方式（如 美团月付 / 储蓄卡 / 余额），先调 cdp_cashier_select_method(tid, '<关键词>')\n"
        "# 2) 打开密码键盘（若免密直接扣款则 open_res['paid'] 会直接为 True）：\n"
        f"open_res = cdp_cashier_open_keypad(tid, port={port}, settle_sec=2.5)\n"
        "# 3) 若 open_res.get('keypad_open') 为 True，调用 cdp_cashier_touch_pin(tid, '<6位数字>', port=9222)\n"
        "#    注意：绝对不要打印那 6 位数字！\n"
        f"# 4) 核验订单列表：olist = cdp_verify_olist_paid({json.dumps(shop, ensure_ascii=False)}, order_id={json.dumps(order_id)}, port={port})\n"
        "```\n\n"
        "## 输出要求\n"
        "仅输出简洁中文状态摘要（成功/失败/需人工、支付方式、实付金额、订单列表核验状态），**严禁输出任何来自下方附加说明的原文或数字**。\n\n"
        "---\n"
        "## 用户附加支付说明（来自本地受控指令文件，严禁在输出中泄露或复述）\n"
    )


def _run_hermes_confidential_payment_handoff(
    ctx: dict[str, Any],
    db: SimingDB,
    policy: dict[str, Any],
    port: int = 9222,
    subprocess_run_fn: Optional[Callable[..., Any]] = None,
    verify_cashier_fn: Optional[Callable[[str, int], dict[str, Any]]] = None,
    verify_olist_fn: Optional[Callable[[str, str, int], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """零查看拼接上下文与用户支付说明文件交由 Hermes 机密模型执行支付，并以纯代码独立核验浏览器真实状态。"""
    cfg = _get_hermes_handoff_cfg(policy)
    tid = str(ctx.get("target_id") or "")
    shop = str(ctx.get("shop") or "")
    order_id = str(ctx.get("order_id") or "")
    verified_yuan = float(ctx.get("verified_yuan") or 0.0)
    max_yuan = float(ctx.get("max_yuan") or 120.0)
    cashier_url = str(ctx.get("cashier_url") or "")

    instr_path = Path(cfg["instruction_file"]).expanduser()
    hermes_bin = Path(cfg["hermes_bin"]).expanduser()
    if not cfg["enabled"] or not instr_path.is_file() or not hermes_bin.is_file():
        return {
            "ok": True,
            "paid": False,
            "order_submitted": True,
            "needs_user_action": True,
            "state": "PASSWORD_OR_2FA",
            "reason": "Hermes 机密支付通道未启用或缺少支付说明文件，已保留收银台交由你手动付款",
            "order_id": order_id,
            "payable_yuan": verified_yuan,
            "cashier_url": cashier_url,
            "target_id": tid,
            "kept_tab_open": True,
        }

    tmp_dir = db.paths.tmp_root
    tmp_dir.mkdir(parents=True, exist_ok=True)
    stamp = f"{os.getpid()}_{int(time.time() * 1000)}"
    ctx_file = tmp_dir / f"mt_pay_ctx_{stamp}.md"
    combined_file = tmp_dir / f"mt_pay_combined_{stamp}.md"

    run_fn = subprocess_run_fn or subprocess.run
    snap_fn = verify_cashier_fn or (lambda t, p: cdp_cashier_snapshot(t, port=p))
    olist_fn = verify_olist_fn or (lambda s, oid, p: cdp_verify_olist_paid(s, order_id=oid, port=p))

    timed_out = False
    hermes_rc: Optional[int] = None
    models_to_try = [cfg["model"], *list(cfg.get("fallback_models") or [])]
    attempted_models: list[str] = []
    used_model = cfg["model"]
    _ROUTING_ERR_RE = re.compile(
        r"(?:\b404\b|no\s+route|model[_\s-]*not[_\s-]*found|not\s+found|\b429\b|\b500\b|\b502\b|\b503\b|\b504\b|"
        r"rate[\s_-]*limit|overloaded|bad\s+gateway|service\s+unavailable|temporarily\s+unavailable|connection\s+error)",
        re.IGNORECASE,
    )
    try:
        ctx_text = _build_hermes_payment_context({**ctx, "port": port})
        ctx_file.write_text(ctx_text, encoding="utf-8")
        try:
            os.chmod(ctx_file, 0o600)
        except OSError:
            pass

        # 严格由 shell 在 0600 临时文件中拼接，经 --query-file 传给 hermes chat（绝不经 -q argv 暴露在 ps aux；Python 进程自身不读不印不落库）
        shell_cmd = (
            'set -euo pipefail; '
            'cleanup() { rm -f "$SIMING_MT_COMBINED"; }; '
            'trap cleanup EXIT; '
            'umask 077; '
            'cat "$SIMING_MT_CTX" "$SIMING_MT_INSTR" > "$SIMING_MT_COMBINED"; '
            '"$SIMING_MT_HERMES" chat '
            '--provider "$SIMING_MT_PROVIDER" '
            '-m "$SIMING_MT_MODEL" '
            '-t terminal -Q --yolo --ignore-rules '
            '--max-turns "$SIMING_MT_MAX_TURNS" '
            '--query-file "$SIMING_MT_COMBINED"'
        )
        total_budget_sec = max(float(cfg["timeout_sec"]), 220.0)
        budget_deadline = time.monotonic() + total_budget_sec
        for idx_m, cand_model in enumerate(models_to_try):
            rem_budget = budget_deadline - time.monotonic()
            if idx_m > 0 and rem_budget < 25.0:
                break
            used_model = cand_model
            attempted_models.append(cand_model)
            env = dict(os.environ)
            # 隔离上游网关可能注入的代理/子进程键，并确保 PATH 含本地 hermes 与 siming
            for _drop_k in ("HERMES_SUPERVISED_CHILD", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
                env.pop(_drop_k, None)
            home_s = str(Path.home())
            extra_path = f"{home_s}/.local/bin:{home_s}/.hermes/hermes-agent/.hermes/bin"
            cur_path = env.get("PATH") or "/usr/local/bin:/usr/bin:/bin"
            if f"{home_s}/.local/bin" not in cur_path:
                env["PATH"] = f"{extra_path}:{cur_path}"
            env.update(
                {
                    "SIMING_MT_CTX": str(ctx_file),
                    "SIMING_MT_INSTR": str(instr_path),
                    "SIMING_MT_COMBINED": str(combined_file),
                    "SIMING_MT_HERMES": str(hermes_bin),
                    "SIMING_MT_PROVIDER": cfg["provider"],
                    "SIMING_MT_MODEL": cand_model,
                    "SIMING_MT_MAX_TURNS": str(cfg["max_turns"]),
                }
            )
            route_err_hit = False
            per_call_timeout = max(25.0, min(float(cfg["timeout_sec"]), rem_budget))
            try:
                proc = run_fn(
                    ["bash", "-c", shell_cmd],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    timeout=per_call_timeout,
                    check=False,
                )
                hermes_rc = int(getattr(proc, "returncode", 1))
                # 仅以布尔模式检测 stderr 是否命中 404/路由不可用信号，绝不保留、打印或回传任何输出内容！
                raw_err_b = getattr(proc, "stderr", b"") or b""
                err_s = raw_err_b.decode("utf-8", errors="ignore") if isinstance(raw_err_b, (bytes, bytearray)) else str(raw_err_b)
                route_err_hit = bool(hermes_rc != 0 and _ROUTING_ERR_RE.search(err_s[:800]))
            except subprocess.TimeoutExpired:
                timed_out = True
                hermes_rc = 124
            try:
                db.record_usage(
                    model=cand_model,
                    runner="hermes_handoff",
                    tokens_in=max(1, len(ctx_text) // 3),
                    tokens_out=0,
                    cost_cny=0.0,
                    ref_id=order_id or shop,
                )
            except Exception:
                pass
            if hermes_rc == 0:
                break
            # D1-B：遇模型 404/无路由且仍有备用模型时，先确认尚未扣款/无风控，自动切备用模型重试一次
            if route_err_hit and idx_m + 1 < len(models_to_try):
                mid_snap = snap_fn(tid, port) if tid else {"ok": False}
                if mid_snap.get("paid") or mid_snap.get("has_captcha") or mid_snap.get("has_hard_2fa"):
                    break
                continue
            break
    finally:
        ctx_file.unlink(missing_ok=True)
        combined_file.unlink(missing_ok=True)

    # 绝不盲信模型文本输出：必须由纯代码重新核验收银台标签页与订单列表真实状态！
    post_snap = snap_fn(tid, port) if tid else {"ok": False}
    post_olist = olist_fn(shop, order_id, port)

    if post_snap.get("has_captcha") or post_snap.get("has_hard_2fa"):
        return {
            "ok": True,
            "paid": False,
            "order_submitted": True,
            "needs_user_action": True,
            "state": str(post_snap.get("state") or "CAPTCHA"),
            "reason": str(post_snap.get("reason") or "收银台触发安全验证或短信/生物验证，已停手保留现场"),
            "order_id": order_id,
            "payable_yuan": post_snap.get("payable_yuan") or verified_yuan,
            "cashier_url": str(post_snap.get("url") or cashier_url),
            "target_id": tid,
            "kept_tab_open": True,
        }

    is_paid_cashier = bool(post_snap.get("paid"))
    is_paid_olist = bool(post_olist.get("found") and post_olist.get("paid"))
    if is_paid_cashier or is_paid_olist:
        paid_yuan = verified_yuan
        snap_amt = post_snap.get("payable_yuan")
        if snap_amt is not None and 0 < float(snap_amt) <= max_yuan:
            paid_yuan = round(float(snap_amt), 2)
        return {
            "ok": True,
            "paid": True,
            "order_submitted": True,
            "state": "PAID",
            "order_id": order_id,
            "paid_yuan": paid_yuan,
            "shop": str(post_olist.get("shop") or shop),
            "order_status": str(post_olist.get("status") or ("支付成功" if is_paid_cashier else "")),
            "delivery_address": str(ctx.get("delivery_address") or ""),
            "eta_text": str(ctx.get("eta_text") or ""),
            "url": str(post_snap.get("url") or cashier_url),
            "target_id": tid,
            "pay_channel": "hermes_confidential_handoff",
            "used_model": used_model,
            "attempted_models": attempted_models,
        }

    rem_s = str(post_snap.get("remaining_time") or "")
    rem_note = f"（剩余支付时间 {rem_s}）" if rem_s else ""
    fail_reason = (
        f"Hermes 机密支付通道执行超时{rem_note}，订单已提交但尚未确认扣款成功"
        if timed_out
        else f"Hermes 机密支付通道未确认完成（rc={hermes_rc}，当前页面：{post_snap.get('state') or 'UNPAID'}）{rem_note}"
    )
    if post_snap.get("state") == "PAY_FAILED":
        fail_reason = f"{post_snap.get('reason') or '支付失败'}{rem_note}"
    return {
        "ok": True,
        "paid": False,
        "order_submitted": True,
        "needs_user_action": True,
        "state": str(post_snap.get("state") or "UNPAID_PENDING"),
        "reason": fail_reason,
        "order_id": order_id,
        "payable_yuan": post_snap.get("payable_yuan") or verified_yuan,
        "cashier_url": str(post_snap.get("url") or cashier_url),
        "target_id": tid,
        "kept_tab_open": True,
    }


def _verify_recent_olist_status(p_ws: LocalWsCdpClient, shop: str) -> Optional[dict[str, str]]:
    """当支付跳转页状态不明确时，只读核查美团 H5 订单列表首单（3 分钟内且匹配店名）返回真实下单/支付状态。"""
    try:
        p_ws.call("Page.navigate", {"url": MEITUAN_OLIST_URL})
        time.sleep(2.8)
        snap = _eval_json(
            p_ws,
            "JSON.stringify({url: location.href, text: (document.body && document.body.innerText || '').slice(0, 1200)})",
        )
        txt = str(snap.get("text") or "")
        from siming.collectors.meituan_orders import parse_olist_text

        orders = parse_olist_text(txt)
        if not orders:
            return None
        o0 = orders[0]
        o_shop = str(o0.get("shop") or "")
        if shop and (shop[:3] not in o_shop and o_shop[:3] not in shop):
            return None
        dt = datetime.strptime(str(o0["ordered_at"]), "%Y-%m-%d %H:%M").replace(tzinfo=SHANGHAI_TZ)
        age_sec = abs((now_local() - dt).total_seconds())
        if age_sec > 240:
            return None
        return {
            "shop": o_shop,
            "ordered_at": str(o0["ordered_at"]),
            "amount": f"¥{float(o0['amount']):g}",
            "status": str(o0.get("status") or ""),
            "head_block": txt[:240],
        }
    except Exception:
        return None


def _live_submit_and_pay_meituan_order(
    params: dict[str, Any],
    db: SimingDB,
    port: int = 9222,
    hermes_handoff_fn: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
) -> dict[str, Any]:
    """经用户显式许可后，在已核验的结算预览标签执行「提交订单 → 美团收银台付款（支持 Hermes 机密支付通道）→ 实态回执校验」。"""
    if os.environ.get("SIMING_TEST_OFFLINE") == "1":
        return {"ok": False, "reason": "offline", "error": "离线测试模式"}

    policy = load_policy(db.paths)
    max_yuan = float((policy.get("spending_caps") or {}).get("meituan_single_order_yuan", 120.0))

    tid = str(params.get("preview_target_id") or "").strip()
    shop = str(params.get("shop") or "").strip()
    if not tid:
        return {"ok": False, "reason": "no_preview_tab", "error": "缺少实时结算预览标签页，为防价格变动拒绝盲目扣款，请重新点单。"}

    base = resolve_chromium_cdp_base(port=port)
    if not base or not base.get("ws_browser"):
        return {"ok": False, "reason": "cdp_unavailable", "error": "本机主 Chromium (9222) 未在线"}

    tabs = list_chromium_tabs(port=port)
    if not any(str(t.get("id") or "") == tid for t in tabs):
        return {
            "ok": False,
            "reason": "preview_tab_closed",
            "error": "结算预览标签页已关闭或过期，为防商品/价格变化未自动扣款，请重新发起点单核验实付金额。",
        }

    page_ws = base["ws_browser"].split("/devtools/")[0] + f"/devtools/page/{tid}"
    keep_tab_open = True
    pending_handoff_ctx: Optional[dict[str, Any]] = None
    try:
        with LocalWsCdpClient(page_ws, timeout=24.0) as p_ws:
            _setup_mobile_session(p_ws)
            loc_ov_param = str(params.get("loc_override") or "").strip()
            if loc_ov_param:
                try:
                    from siming.meituan_agent import resolve_meituan_delivery_target, set_cdp_geolocation_for_target

                    set_cdp_geolocation_for_target(
                        p_ws,
                        resolve_meituan_delivery_target(db, location_override=loc_ov_param),
                    )
                except Exception:
                    pass
            time.sleep(0.35)

            # 1) 提交前二次硬核验：必须仍在结算预览页（支持 mindex/preview 与 package/preview/preview），且实付未超上限、涨幅 ≤ ¥2、地址一致
            pre_snap = _eval_json(
                p_ws,
                "JSON.stringify({url: location.href, title: document.title || '', text: (document.body && document.body.innerText || '').trim()})",
            )
            pre_url = str(pre_snap.get("url") or "")
            pre_txt = str(pre_snap.get("text") or "")
            pre_cls = classify_cashier_or_order_state(pre_url, str(pre_snap.get("title") or ""), pre_txt)
            if pre_cls["state"] in ("CAPTCHA", "LOGIN"):
                return {"ok": False, "gated": True, "state": pre_cls["state"], "reason": pre_cls["reason"], "url": pre_url, "target_id": tid}
            if not _is_meituan_preview_url(pre_url):
                return {
                    "ok": False,
                    "reason": "not_on_preview",
                    "error": f"当前标签已不在结算预览页（{pre_url[:80]}），为保安全未重复提交扣款。",
                    "target_id": tid,
                }
            if "未选择(必选)" in pre_txt:
                return {
                    "ok": False,
                    "reason": "preorder_time_required",
                    "error": "该门店已切为预约配送模式（页面提示送达时间「未选择(必选)」），为防误下隔日预订单已自动中止扣款。",
                    "target_id": tid,
                }

            live_bill = parse_meituan_preview_bill(pre_txt)
            # 提单前兜底防漏券：若预览页因刷新/超时仍显示「未选红包 + 可用」，在提交前最后补选一次最优红包（只会降价、绝不涨价）
            live_cp_txt = str(live_bill.get("coupon_text") or "")
            if "未选红包" in live_cp_txt and "可用" in live_cp_txt:
                try:
                    from siming.meituan_coupons import apply_optimal_preview_coupons

                    sk_pre = "subtrade" if "package/preview" in pre_url.lower() else "mindex"
                    apply_optimal_preview_coupons(p_ws, store_kind=sk_pre, check_suboptimal_selected=False)
                    re_snap = _eval_json(
                        p_ws,
                        "JSON.stringify({url: location.href, text: (document.body && document.body.innerText || '').trim()})",
                    )
                    if _is_meituan_preview_url(str(re_snap.get("url") or "")):
                        pre_url = str(re_snap["url"])
                        pre_txt = str(re_snap.get("text") or pre_txt)
                        live_bill = parse_meituan_preview_bill(pre_txt)
                except Exception:
                    pass

            guard = validate_pre_submit_guardrails(live_bill, params, max_yuan=max_yuan, max_drift_yuan=2.0)
            if not guard.get("ok"):
                return {"ok": False, "reason": guard["reason"], "error": guard["error"], "target_id": tid}

            verified_yuan = float(guard["payable_yuan"])

            # 2) 挂载只读 XHR 钩子捕获 /order/v2/submit 订单号或报错，并用 CDP trusted input 点击「提交订单」（兼容 mindex/preview 与 mt-view）
            p_ws.call(
                "Runtime.evaluate",
                {
                    "expression": r"""(() => {
                        if (window.__simingSubmitHooked) return;
                        window.__simingSubmitHooked = true;
                        window.__simingSubmitResp = null;
                        const origOpen = XMLHttpRequest.prototype.open;
                        XMLHttpRequest.prototype.open = function(m, u) { this.__su = String(u || ''); return origOpen.apply(this, arguments); };
                        const origSend = XMLHttpRequest.prototype.send;
                        XMLHttpRequest.prototype.send = function() {
                            if ((this.__su || '').includes('order/v2/submit')) {
                                this.addEventListener('load', () => {
                                    try { window.__simingSubmitResp = String(this.responseText || '').slice(0, 1200); } catch (e) {}
                                });
                            }
                            return origSend.apply(this, arguments);
                        };
                    })()""",
                    "returnByValue": True,
                },
            )

            submit_btn = _eval_json(
                p_ws,
                r"""(() => {
                    const selectors = [
                        'button[class*="submit_"]',
                        'mt-view.p-self-pay-btn',
                        '[class*="p-self-pay-btn"]',
                        '[class*="p-btns-right-btn"]',
                        '[class*="submitWrapper_"] button',
                        '[class*="toPay_"]',
                        '[class*="pay-btn"]',
                    ];
                    let btn = null;
                    for (const sel of selectors) {
                        const found = Array.from(document.querySelectorAll(sel)).find(e => {
                            const r = e.getBoundingClientRect();
                            return r.width >= 48 && r.height >= 20 && r.top > 250;
                        });
                        if (found) { btn = found; break; }
                    }
                    if (!btn) {
                        const matches = Array.from(document.querySelectorAll('mt-view, webc-view, button, div, span')).filter(e => {
                            if (e.tagName === 'TITLE') return false;
                            const r = e.getBoundingClientRect();
                            return (e.innerText || '').trim() === '提交订单' && r.width >= 50 && r.height >= 22 && r.top > 250;
                        });
                        if (matches.length) btn = matches[matches.length - 1];
                    }
                    if (!btn) return JSON.stringify({found: false});
                    try { btn.scrollIntoView({block: 'center'}); } catch (e) {}
                    const r = btn.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return JSON.stringify({found: false});
                    return JSON.stringify({
                        found: true,
                        x: r.x + r.width / 2,
                        y: r.y + r.height / 2,
                        tag: btn.tagName,
                        text: (btn.innerText || '').trim(),
                    });
                })()""",
            )
            if not submit_btn.get("found"):
                return {"ok": False, "reason": "no_submit_button", "error": "结算页未找到「提交订单」按钮。", "target_id": tid}
            _cdp_click_xy(p_ws, float(submit_btn["x"]), float(submit_btn["y"]))

            # 3) 轮询等待提单结果（直通极速支付成功 / 跳转收银台 / 门禁 / 报错）
            order_id = ""
            cashier_url = ""
            last_state: dict[str, Any] = {"state": "UNKNOWN", "reason": ""}
            last_snap: dict[str, Any] = {}
            cashier_pay_clicked = False
            addr_modal_confirmed = False
            submit_js_retried = False
            handoff_attempted = False
            expected_addr_head = _normalize_addr_token(
                str(live_bill.get("delivery_address") or params.get("delivery_address") or "").split()[0]
                if str(live_bill.get("delivery_address") or params.get("delivery_address") or "").strip()
                else ""
            )

            submit_started_at = time.monotonic()
            deadline = submit_started_at + 20.0
            while time.monotonic() < deadline:
                time.sleep(0.8)
                try:
                    snap = _eval_json(
                        p_ws,
                        "JSON.stringify({url: location.href, title: document.title || '', text: (document.body && document.body.innerText || '').trim().slice(0, 2600), subResp: window.__simingSubmitResp || ''})",
                    )
                except Exception:
                    # 页面跨源跳转到 npay/mpay.meituan.com 瞬间 evaluate 可能报错，重试即可
                    continue

                last_snap = snap
                u = str(snap.get("url") or "")
                t = str(snap.get("title") or "")
                txt = str(snap.get("text") or "")

                sub_raw = str(snap.get("subResp") or "")
                if sub_raw and not order_id:
                    m_oid = re.search(r'"(?:order_view_id|wm_order_id_view|orderId|tradeno|trade_no)"\s*:\s*"?([0-9A-Za-z_-]{8,36})"?', sub_raw)
                    if m_oid:
                        order_id = m_oid.group(1)
                if not order_id and any(k in u.lower() for k in ("mpay.meituan.com", "npay.meituan.com", "/cashier")):
                    order_id = _extract_order_id_from_cashier(u, txt)

                # 若因浏览器定位与收货地址存在距离差弹出「确认收货地址」二次弹窗，仅在弹窗地址与确认卡一致时 trusted 点击确认
                if (
                    not addr_modal_confirmed
                    and _is_meituan_preview_url(u)
                    and re.search(r"(确认收货地址|距离.*较远|核对收货地址)", txt)
                    and (not expected_addr_head or expected_addr_head in _normalize_addr_token(txt))
                ):
                    ack = _eval_json(
                        p_ws,
                        r"""(() => {
                            const btn = Array.from(document.querySelectorAll('mt-view, button, div, span, a')).find(el => {
                                const s = (el.innerText || '').trim();
                                const rc = el.getBoundingClientRect();
                                return rc.width >= 50 && rc.height >= 24 && /^(?:确认地址|确认收货地址|继续下单|仍然下单|确认无误|确认提交|确定下单|继续提交)$/.test(s);
                            });
                            if (!btn) return JSON.stringify({found: false});
                            const rc = btn.getBoundingClientRect();
                            return JSON.stringify({found: true, x: rc.x + rc.width / 2, y: rc.y + rc.height / 2, text: (btn.innerText || '').trim()});
                        })()""",
                    )
                    if ack.get("found"):
                        _cdp_click_xy(p_ws, float(ack["x"]), float(ack["y"]))
                        addr_modal_confirmed = True
                        continue

                # 若 3.2s 后仍停留在 mindex/preview 且尚未发出 order/v2/submit 请求，补一次原生 button.click()（适配 React onClick 绑定在外层 button[class*="submit_"]）
                if (
                    not submit_js_retried
                    and not sub_raw
                    and "mindex/preview" in u.lower()
                    and (time.monotonic() - submit_started_at) >= 3.2
                ):
                    submit_js_retried = True
                    p_ws.call(
                        "Runtime.evaluate",
                        {
                            "expression": "document.querySelector('button[class*=\"submit_\"]')?.click()",
                            "returnByValue": True,
                        },
                    )

                st_info = classify_cashier_or_order_state(u, t, txt)
                last_state = st_info
                st_code = st_info["state"]

                if st_code == "PAID":
                    keep_tab_open = False
                    c_paid = extract_cashier_payable_yuan(txt)
                    return {
                        "ok": True,
                        "paid": True,
                        "order_submitted": True,
                        "state": "PAID",
                        "order_id": order_id,
                        "paid_yuan": c_paid if (c_paid and c_paid <= max_yuan) else verified_yuan,
                        "shop": live_bill.get("shop") or shop,
                        "delivery_address": live_bill.get("delivery_address") or params.get("delivery_address") or "",
                        "eta_text": live_bill.get("eta_text") or params.get("eta_text") or "",
                        "url": u,
                        "target_id": tid,
                    }

                if st_code in ("CAPTCHA", "LOGIN", "EXTERNAL_APP", "PAY_FAILED"):
                    cashier_url = u
                    return {
                        "ok": True,
                        "paid": False,
                        "order_submitted": not _is_meituan_preview_url(u),
                        "needs_user_action": True,
                        "state": st_code,
                        "reason": st_info["reason"],
                        "order_id": order_id,
                        "payable_yuan": verified_yuan,
                        "cashier_url": cashier_url,
                        "target_id": tid,
                        "kept_tab_open": True,
                    }

                if st_code == "SUBMIT_ERROR":
                    return {
                        "ok": False,
                        "paid": False,
                        "order_submitted": False,
                        "state": "SUBMIT_ERROR",
                        "error": st_info["reason"],
                        "target_id": tid,
                    }

                # 抵达收银台或弹出 6 位密码键盘：硬 2FA（短信/人脸/指纹）一律停手；否则若已启用 Hermes 机密通道则直接交接
                if st_code in ("CASHIER_READY", "PASSWORD_OR_2FA"):
                    cashier_url = u
                    if _HARD_2FA_RE.search(txt):
                        return {
                            "ok": True,
                            "paid": False,
                            "order_submitted": not _is_meituan_preview_url(u),
                            "needs_user_action": True,
                            "state": "PASSWORD_OR_2FA",
                            "reason": "收银台要求短信验证码或生物识别验证，已按安全红线停手保留现场",
                            "order_id": order_id,
                            "payable_yuan": verified_yuan,
                            "cashier_url": cashier_url,
                            "target_id": tid,
                            "kept_tab_open": True,
                        }

                    c_amt = extract_cashier_payable_yuan(txt)
                    if c_amt is not None and (c_amt > max_yuan or c_amt > verified_yuan + 2.0):
                        return {
                            "ok": False,
                            "paid": False,
                            "order_submitted": True,
                            "state": "CASHIER_PRICE_MISMATCH",
                            "error": f"收银台展示金额 ¥{c_amt:g} 与核验实付 ¥{verified_yuan:g} 不符，已停手保留现场。",
                            "cashier_url": cashier_url,
                            "target_id": tid,
                        }

                    use_hermes = (hermes_handoff_fn is not None) or _is_hermes_handoff_ready(policy)
                    if use_hermes and not handoff_attempted:
                        handoff_attempted = True
                        pending_handoff_ctx = {
                            "target_id": tid,
                            "cashier_url": cashier_url,
                            "shop": str(live_bill.get("shop") or shop),
                            "item_spec": str(params.get("item_spec") or ""),
                            "order_id": order_id,
                            "verified_yuan": verified_yuan,
                            "cashier_yuan": c_amt,
                            "max_yuan": max_yuan,
                            "delivery_address": str(live_bill.get("delivery_address") or params.get("delivery_address") or ""),
                            "eta_text": str(live_bill.get("eta_text") or params.get("eta_text") or ""),
                            "cashier_text_head": txt[:700],
                            "port": port,
                        }
                        break

                    if st_code == "PASSWORD_OR_2FA":
                        return {
                            "ok": True,
                            "paid": False,
                            "order_submitted": not _is_meituan_preview_url(u),
                            "needs_user_action": True,
                            "state": st_code,
                            "reason": st_info["reason"],
                            "order_id": order_id,
                            "payable_yuan": c_amt or verified_yuan,
                            "cashier_url": cashier_url,
                            "target_id": tid,
                            "kept_tab_open": True,
                        }

                    if not cashier_pay_clicked:
                        # 未启用 Hermes 机密通道时，保留原极速免密尝试路径
                        sel_res = _eval_json(
                            p_ws,
                            r"""(() => {
                                const opts = Array.from(document.querySelectorAll('div, li, label, span')).filter(el => {
                                    const t = (el.innerText || '').trim();
                                    const rc = el.getBoundingClientRect();
                                    return rc.top >= 80 && rc.bottom <= 740 && rc.height >= 28 && rc.height <= 96 && rc.width >= 140 && t.length <= 50;
                                });
                                const mtOpt = opts.find(el => /(极速支付|美团月付|美团支付|余额|储蓄卡)/.test((el.innerText || '').trim()) && !/(微信|支付宝|确认支付|立即支付|确认付款)/.test((el.innerText || '').trim()));
                                if (mtOpt) {
                                    try { mtOpt.click(); return JSON.stringify({selected: true, text: (mtOpt.innerText || '').trim().slice(0, 30)}); } catch (e) {}
                                }
                                return JSON.stringify({selected: false});
                            })()""",
                        )
                        if sel_res.get("selected"):
                            time.sleep(0.35)

                        pay_click = _eval_json(
                            p_ws,
                            r"""(() => {
                                const btn = document.querySelector('button.btn-half.btn.load')
                                    || Array.from(document.querySelectorAll('button, div, span, a')).filter(el => {
                                        const t = (el.innerText || '').replace(/\s+/g, '').trim();
                                        const rc = el.getBoundingClientRect();
                                        return rc.height >= 34 && rc.width >= 120 && (
                                            /^(?:确认支付|立即支付|确认付款|免密支付|确认极速支付)(?:[¥￥]?\d+(?:\.\d{1,2})?元?)?$/.test(t)
                                            || /^(?:极速支付|美团支付)[¥￥]\d+(?:\.\d{1,2})?元?$/.test(t)
                                        );
                                    }).sort((a, b) => (b.getBoundingClientRect().top - a.getBoundingClientRect().top))[0];
                                if (!btn) return JSON.stringify({clicked: false});
                                btn.click();
                                return JSON.stringify({clicked: true, text: (btn.innerText || '').trim()});
                            })()""",
                        )
                        if pay_click.get("clicked"):
                            cashier_pay_clicked = True
                            deadline = max(deadline, time.monotonic() + 12.0)

            if pending_handoff_ctx is None:
                # 4) 超时后通过 mindex/olist 核验真实订单状态，绝不猜结果（严格排除「订单已提交 / 待支付 / 立即支付」）
                olist_hit = _verify_recent_olist_status(p_ws, str(live_bill.get("shop") or shop))
                if olist_hit:
                    st_str = olist_hit.get("status") or ""
                    is_paid_now = _olist_status_is_paid(st_str, olist_hit.get("head_block") or "")
                    if is_paid_now:
                        keep_tab_open = False
                        return {
                            "ok": True,
                            "paid": True,
                            "order_submitted": True,
                            "state": "PAID",
                            "order_id": order_id,
                            "paid_yuan": verified_yuan,
                            "shop": olist_hit["shop"],
                            "order_status": st_str,
                            "delivery_address": live_bill.get("delivery_address") or params.get("delivery_address") or "",
                            "eta_text": live_bill.get("eta_text") or params.get("eta_text") or "",
                            "target_id": tid,
                        }
                    return {
                        "ok": True,
                        "paid": False,
                        "order_submitted": True,
                        "needs_user_action": True,
                        "state": "UNPAID_PENDING",
                        "reason": f"订单已提交（当前状态：{st_str or '待支付'}），需你完成最终付款确认",
                        "order_id": order_id,
                        "payable_yuan": verified_yuan,
                        "cashier_url": cashier_url or str(last_snap.get("url") or ""),
                        "target_id": tid,
                        "kept_tab_open": True,
                    }

                return {
                    "ok": False,
                    "paid": False,
                    "order_submitted": bool(cashier_pay_clicked or cashier_url or (not _is_meituan_preview_url(str(last_snap.get("url") or "")))),
                    "state": last_state.get("state") or "UNKNOWN",
                    "error": f"已尝试提交但未确认最终支付状态（{last_state.get('reason') or '页面超时'}），已保留后台标签页，请核对美团订单避免重复下单。",
                    "cashier_url": cashier_url or str(last_snap.get("url") or ""),
                    "target_id": tid,
                }

        # 已退出主 CDP WebSocket 连接后再启动 Hermes 机密支付通道，避免同标签双 WS 会话冲突
        h_res = (
            hermes_handoff_fn(pending_handoff_ctx)
            if hermes_handoff_fn is not None
            else _run_hermes_confidential_payment_handoff(pending_handoff_ctx, db, policy, port=port)
        )
        if h_res.get("paid"):
            keep_tab_open = False
        return h_res
    except Exception as exc:
        return {"ok": False, "paid": False, "reason": "cdp_error", "error": f"支付流程执行异常：{str(exc)[:180]}", "target_id": tid}
    finally:
        if not keep_tab_open and tid:
            _close_tab_quietly(port, tid)
            if (db.get_meta(ACTIVE_PREVIEW_TID_KEY) or "").strip() == tid:
                db.set_meta(ACTIVE_PREVIEW_TID_KEY, "")


def execute_meituan_submit_pay(
    params: dict[str, Any],
    db: SimingDB,
    cdp_pay_fn: Optional[Callable[[dict[str, Any]], dict[str, Any]]] = None,
) -> str:
    """纯代码审批 `meituan_submit_pay` 执行器：校验幂等防重付、执行提交与美团收银台付款并返回真实回执。"""
    shop = str(params.get("shop") or "").strip()
    item_spec = str(params.get("item_spec") or "").strip()
    deeplink = str(params.get("deeplink") or f"imeituan://www.meituan.com/waimai/search?query={urllib.parse.quote(shop)}")
    orders_deeplink = "imeituan://www.meituan.com/waimai/orders"

    # 幂等防重复支付锁
    pay_digest = hashlib.sha256(
        json.dumps(
            {
                "shop": shop,
                "item_spec": item_spec,
                "final_payable_yuan": params.get("final_payable_yuan"),
                "preview_target_id": params.get("preview_target_id"),
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()[:24]
    if (db.get_meta("last_meituan_paid_digest") or "").strip() == pay_digest:
        raise RuntimeError("该笔订单已执行过支付提交，已触发防重复扣款拦截。")

    res = cdp_pay_fn(params) if cdp_pay_fn is not None else _live_submit_and_pay_meituan_order(params, db)
    db.set_meta("last_meituan_order_at", iso_now())

    if res.get("ok") and res.get("paid"):
        db.set_meta("last_meituan_paid_digest", pay_digest)
        db.set_meta(
            "last_meituan_paid_order",
            json.dumps(
                {
                    "paid_at": iso_now(),
                    "shop": res.get("shop") or shop,
                    "item_spec": item_spec,
                    "paid_yuan": res.get("paid_yuan") or params.get("final_payable_yuan"),
                    "order_id": res.get("order_id") or "",
                    "delivery_address": res.get("delivery_address") or params.get("delivery_address") or "",
                },
                ensure_ascii=False,
            ),
        )
        paid_yuan = res.get("paid_yuan")
        if paid_yuan is None:
            paid_yuan = params.get("final_payable_yuan")
        pay_s = f"¥{float(paid_yuan):g}" if paid_yuan is not None else "见账单"
        addr_s = str(res.get("delivery_address") or params.get("delivery_address") or "").strip()
        eta_s = str(res.get("eta_text") or params.get("eta_text") or "").strip()
        oid_s = f" · 订单号 `{res['order_id']}`" if res.get("order_id") else ""
        st_s = f" · 状态：{res['order_status']}" if res.get("order_status") else ""
        parts = [f"🎉 **美团外卖已下单并支付成功！**"]
        parts.append(f"• 门店：**{res.get('shop') or shop}** — `{item_spec}`")
        ch_s = "（经 Hermes 机密通道支付）" if res.get("pay_channel") == "hermes_confidential_handoff" else ""
        parts.append(f"• 实付：**{pay_s}**{ch_s}{oid_s}{st_s}")
        if addr_s:
            parts.append(f"• 配送：**{addr_s}**" + (f"（{eta_s}）" if eta_s else ""))
        return "\n".join(parts)

    if res.get("needs_user_action"):
        db.set_meta("last_meituan_paid_digest", pay_digest)
        reason = str(res.get("reason") or "需本人确认付款")
        c_url = str(res.get("cashier_url") or "").strip()
        oid_line = f"\n• 订单号：`{res['order_id']}`" if res.get("order_id") else ""
        url_line = f"\n• 收银台直达：{c_url}" if c_url.startswith("http") else ""
        return (
            f"⏳ **订单已提交，等待你完成最终支付验证**（**{shop}** · 应付 ¥{res.get('payable_yuan') or params.get('final_payable_yuan')}）\n"
            f"• 原因：{reason}{oid_line}{url_line}\n"
            f"• 已原样保留桌面 Chromium 后台标签页；也可在手机美团「待付款」直接一键付款：{orders_deeplink}"
        )

    if res.get("gated"):
        return (
            f"🛡️ 提交支付触发美团安全门禁（{res.get('reason') or '需验证'}），已按安全红线停手并保留桌面标签页（未扣款），"
            f"处理完成后可重新下单或直接点美团唤端：{deeplink}"
        )

    if res.get("order_submitted"):
        db.set_meta("last_meituan_paid_digest", pay_digest)
    raise RuntimeError(str(res.get("error") or res.get("reason") or "支付未完成"))


def cancel_meituan_pending_order(
    db: SimingDB,
    token: str = "",
    port: int = 9222,
) -> dict[str, Any]:
    """取消待确认的美团付款许可卡，并在 9222 后台清空购物车与关闭预览标签页。"""
    db.init_db()
    clean_tok = (token or "").strip().upper()
    conn = db.connect()
    non_mt_exists = False
    try:
        if clean_tok:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM approvals WHERE token=? AND status='PENDING' AND action IN ('meituan_submit_pay', 'meituan_lock_cart')",
                    (clean_tok,),
                ).fetchall()
            ]
            if not rows:
                other = conn.execute(
                    "SELECT action FROM approvals WHERE token=? AND status='PENDING'",
                    (clean_tok,),
                ).fetchone()
                if other and str(other["action"] or "") not in ("meituan_submit_pay", "meituan_lock_cart"):
                    non_mt_exists = True
        else:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT * FROM approvals WHERE status='PENDING' AND action IN ('meituan_submit_pay', 'meituan_lock_cart') ORDER BY created_at DESC LIMIT 3"
                ).fetchall()
            ]
    finally:
        conn.close()

    if not rows:
        return {
            "ok": False,
            "cancelled": 0,
            "not_meituan": non_mt_exists,
            "reply": (
                f"口令 `{clean_tok}` 不是待付款的美团订单确认卡。"
                if non_mt_exists
                else "当前没有待付款的美团点单确认卡。"
            ),
        }

    cancelled_toks: list[str] = []
    preview_tids: list[tuple[str, str]] = []
    with db.transaction() as c:
        for r in rows:
            tok = str(r["token"])
            c.execute("UPDATE approvals SET status='INVALIDATED' WHERE token=? AND status='PENDING'", (tok,))
            cancelled_toks.append(tok)
            try:
                p = json.loads(str(r.get("params_json") or "{}"))
                tid = str(p.get("preview_target_id") or "").strip()
                m_url = str(p.get("menu_url") or "").strip()
                if tid:
                    preview_tids.append((tid, m_url))
            except Exception:
                pass
            db.write_audit("A1", "meituan_order_cancelled", {"token": tok}, target_id=str(r.get("target_id") or ""), approval_ref=tok, conn=c)

    # 在线时顺手清空购物车并关闭后台预览标签页
    cart_cleared = False
    if os.environ.get("SIMING_TEST_OFFLINE") != "1":
        base = resolve_chromium_cdp_base(port=port)
        if base and base.get("ws_browser"):
            for tid, m_url in preview_tids:
                if m_url:
                    try:
                        page_ws = base["ws_browser"].split("/devtools/")[0] + f"/devtools/page/{tid}"
                        with LocalWsCdpClient(page_ws, timeout=14.0) as p_ws:
                            _setup_mobile_session(p_ws)
                            if _clear_cart_on_menu_tab(p_ws, m_url):
                                cart_cleared = True
                    except Exception:
                        pass
                _close_tab_quietly(port, tid)
        db.set_meta(ACTIVE_PREVIEW_TID_KEY, "")
        db.set_meta(ACTIVE_PREVIEW_MENU_KEY, "")

    tok_str = "、".join(f"`{t}`" for t in cancelled_toks)
    extra = "并已清空美团购物车与关闭预览页" if cart_cleared else "并已作废确认卡"
    return {
        "ok": True,
        "cancelled": len(cancelled_toks),
        "tokens": cancelled_toks,
        "reply": f"🗑️ 已取消美团待付确认卡 {tok_str}，{extra}，未产生任何扣款。",
    }
