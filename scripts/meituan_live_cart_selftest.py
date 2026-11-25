#!/usr/bin/env python3
"""美团 H5 真实加购 + 结算预览自测（真实链路，零扣款）。

用法：
  .pixi/envs/default/bin/python scripts/meituan_live_cart_selftest.py \
    --shop "示例鲜果切" --dish "西瓜果切 500g" --loc 家 --expect-addr "示例小区A"
  .pixi/envs/default/bin/python scripts/meituan_live_cart_selftest.py --with-apply

退出码：0=通过；1=失败。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from siming.approval import create_approval, verify_and_apply_approval  # noqa: E402
from siming.cdp import LocalWsCdpClient, resolve_chromium_cdp_base  # noqa: E402
from siming.db import SimingDB  # noqa: E402
from siming.meituan_agent import stage_meituan_h5_order  # noqa: E402
from siming.meituan_cart_live import _body_text, _setup_page  # noqa: E402

DEFAULT_SHOP = "示例鲜果切"
DEFAULT_DISH = "西瓜果切 500g"
DEFAULT_ADDR_TOKEN = ""


def read_preview_text(tid: str) -> str:
    base = resolve_chromium_cdp_base(port=9222)
    if not base or not base.get("ws_browser"):
        raise RuntimeError("本机主 Chromium 9222 未就绪")
    ws = base["ws_browser"].split("/devtools/")[0] + f"/devtools/page/{tid}"
    with LocalWsCdpClient(ws, timeout=20.0) as p_ws:
        _setup_page(p_ws)
        return _body_text(p_ws)


def parse_total(text: str) -> float:
    ms = re.findall(r"合计\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)", text)
    if not ms:
        raise AssertionError("预览页未找到「合计 ¥X」")
    return float(ms[-1])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--loc", default="家")
    ap.add_argument("--qty", type=int, default=1)
    ap.add_argument("--shop", default=DEFAULT_SHOP)
    ap.add_argument("--dish", default=DEFAULT_DISH)
    ap.add_argument("--expect-addr", default=DEFAULT_ADDR_TOKEN, help="断言预览页应包含的收货地址关键字")
    ap.add_argument("--with-apply", action="store_true", help="额外验证 apply Axx 的 loc_override 铁律")
    args = ap.parse_args()

    db = SimingDB()
    db.init_db()

    print("=" * 72)
    print(f"真实链路：{args.shop} · {args.dish} × {args.qty}（--loc {args.loc}）")
    print("=" * 72)
    res = stage_meituan_h5_order(
        db,
        f"{args.shop} {args.dish} {args.qty}",
        location_override=args.loc,
    )
    if not res.get("ok"):
        print(json.dumps(res, ensure_ascii=False, indent=2))
        print(f"❌ 失败：{res.get('reply') or res.get('error') or res.get('reason')}")
        return 1

    token = str(res.get("approval_token") or "")
    if not token:
        print("❌ 未生成付款许可确认卡")
        return 1
    total = float(res.get("final_payable_yuan") or 0)
    addr = str(res.get("delivery_address") or "")
    print(f"确认卡：{token}  门店：{res.get('shop')}  金额：¥{total:g}")
    print(f"地址：{addr}")
    print(f"商品：{res.get('item_spec')}")

    checks: list[tuple[str, bool, str]] = []
    if args.expect_addr:
        checks.append((f"地址含 {args.expect_addr}", args.expect_addr in addr, addr))
    checks.append(("实付金额 > 0", total > 0.0, f"¥{total:g}"))

    tid = str(res.get("preview_target_id") or "")
    if not tid:
        print("❌ 缺少 preview_target_id，无法核验预览页")
        return 1
    text = read_preview_text(tid)
    preview_total = parse_total(text)
    if args.expect_addr:
        checks.append(("预览页地址正确", args.expect_addr in text, args.expect_addr))
    checks.append(("预览页数量一致", f"数量:{args.qty}" in text, f"数量:{args.qty}"))
    checks.append(("预览页合计与确认卡一致", abs(preview_total - total) < 0.01, f"页面 ¥{preview_total:g} / 卡片 ¥{total:g}"))
    checks.append(("仍停在提交订单之前", "提交订单" in text and "支付成功" not in text and "订单已提交" not in text, "提交订单按钮在，无支付成功字样"))

    m_unit = re.search(r"单价[:：]\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)", text)
    m_qty = re.search(r"数量[:：]\s*(\d+)", text)
    m_pack = re.search(r"打包费\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)", text)
    m_ship = re.findall(r"配送费\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)\s*[¥￥]\s*(\d+(?:\.\d{1,2})?)", text)
    if m_unit and m_qty and m_pack and m_ship:
        calc = float(m_unit.group(1)) * int(m_qty.group(1)) + float(m_pack.group(1)) + float(m_ship[0][1])
        checks.append(("明细自洽（单价×数量+打包+实收配送）", abs(calc - preview_total) < 0.6, f"计算 ¥{calc:g} / 合计 ¥{preview_total:g}"))
    else:
        checks.append(("明细自洽", False, "未能从页面解析出单价/数量/打包/配送"))

    print("-" * 72)
    ok = True
    for name, passed, detail in checks:
        print(f"{'✅' if passed else '❌'} {name}：{detail}")
        ok = ok and passed

    if args.with_apply:
        print("-" * 72)
        print(f"apply 路径：用不一致的 delivery_poi 验证 loc_override={args.loc} 仍然优先")
        params = {
            "shop": str(res.get("shop") or args.shop),
            "dish_name": args.dish,
            "query": f"{args.shop} {args.dish}",
            "qty": args.qty,
            "item_spec": str(res.get("item_spec") or args.dish),
            "delivery_poi": "示例写字楼 10层",
            "loc_override": args.loc,
            "deeplink": f"imeituan://www.meituan.com/waimai/search?query={args.shop}",
        }
        app = create_approval(db, action="meituan_lock_cart", target_id="MT-SELFTEST", params=params, ttl_minutes=10)
        applied = verify_and_apply_approval(db, app["token"])
        apply_ok = bool(applied.get("ok")) and (not args.expect_addr or args.expect_addr in str(applied.get("summary") or ""))
        print(f"{'✅' if apply_ok else '❌'} apply {app['token']} → {applied.get('summary')}")
        ok = ok and apply_ok

    print("-" * 72)
    print(f"{'✅ 自测通过' if ok else '❌ 自测失败'}  预览标签页：{tid}（后台保留，购物车 {args.qty} 件，未提交订单）")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
