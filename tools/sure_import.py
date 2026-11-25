#!/usr/bin/env python3
"""将本机 siming.db 的 ledger_txns 幂等导入自托管 Sure（we-promise/sure）财务 OS。

特点：
- 对 ~/.local/share/siming/siming.db 严格只读（SQLite mode=ro）；
- 按 (清洗后账户名, 币种) 自动拆分账户（严格隔离 CNY / USD 等多币种，识别信用卡 vs 储蓄卡/零钱）；
- 排除 VOID/CANCELLED 流水；消费/退款/收入/划转按 Sure 口径映射；
- 幂等防重：每笔流水在 notes 中写入 `[siming:<txn_id>]` 指纹，重复执行不产生重复流水；
- 支持三种模式：
  1. `--dry-run`：仅统计将导入的账户、分类与流水数量，不写任何目标；
  2. `--export-csv <dir>`：按账户导出 Sure 可直接上传的 UTF-8 CSV；
  3. `--apply`（默认）：经 `docker exec -i siming-sure-web bundle exec rails runner -` 幂等创建用户/账户/分类并写入全部流水（含首次随机强密码 0600 落盘 `~/.config/siming/secrets/sure-login.txt`）。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import secrets
import sqlite3
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

DEFAULT_DB = Path.home() / ".local/share/siming/siming.db"
DEFAULT_SECRETS_DIR = Path.home() / ".config/siming/secrets"
DEFAULT_CONTAINER = "siming-sure-web"
DEFAULT_EMAIL = os.environ.get("SURE_EMAIL", "admin@siming.local")

_CATEGORY_RULES: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    (
        "餐饮外卖",
        "#f97316",
        re.compile(
            r"(美团|饿了么|瑞幸|luckin|星巴克|麦当劳|肯德基|喜茶|霸王茶姬|库迪|奶茶|咖啡|餐饮|餐厅|火锅|烧烤|米粉|拉面|面馆|瓦罐|饺子|包子|果切|水果|海底捞|西贝|便利蜂|罗森|全家|7-?11|面包|蛋糕|烘焙|甜品|食堂|小吃|肉夹馍)",
            re.IGNORECASE,
        ),
    ),
    (
        "AI 算力",
        "#6366f1",
        re.compile(
            r"(nous|deepseek|aliyun|阿里云|dashscope|kimi|月之暗面|claude|anthropic|openai|chatgpt|redpill|codex|magpie|cursor|github|autodl|算力|gpu|api\s*充值|openrouter)",
            re.IGNORECASE,
        ),
    ),
    (
        "出行车务",
        "#38bdf8",
        re.compile(
            r"(滴滴|didi|打车|花小猪|曹操出行|高德|地铁|公交|一卡通|12306|铁路|航空|机票|机场|携程|去哪儿|飞猪|酒店|民宿|tesla|特斯拉|超充|特来电|星星充电|国家电网|小桔充电|充电|etc|速通卡|高速|停车|泊车|车库|车险|中石化|中石油|加油|洗车)",
            re.IGNORECASE,
        ),
    ),
    (
        "家居装修",
        "#14b8a6",
        re.compile(
            r"(装修|建材|宜家|ikea|西门子|海尔|美的|格力|老板|林内|科沃斯|凯度|极米|米家|小米|绿联|拓竹|家电|物业|水费|电费|燃气|供暖|宽带|暖气|厨卫|灯具)",
            re.IGNORECASE,
        ),
    ),
    (
        "网购商超",
        "#ec4899",
        re.compile(
            r"(京东|淘宝|天猫|拼多多|抖音电商|抖音商城|山姆|盒马|超市|物美|永辉|大润发|沃尔玛|屈臣氏|名创优品|无印良品|得物|闲鱼|唯品会|叮咚|多点|丰巢|菜鸟|顺丰|快递|邮政)",
            re.IGNORECASE,
        ),
    ),
    (
        "订阅数字",
        "#a855f7",
        re.compile(
            r"(apple|icloud|app\s*store|itunes|game\s*pass|xbox|steam|迅雷|点众|快看|番茄小说|七猫|起点|阅文|百度网盘|百度文库|夸克|阿里云盘|网易云|qq音乐|spotify|youtube|netflix|会员|续费|连续包月|wps|飞书|剪映|知乎|微信读书|得到|樊登)",
            re.IGNORECASE,
        ),
    ),
    (
        "医疗健康",
        "#10b981",
        re.compile(
            r"(医院|门诊|挂号|诊所|药房|药店|大药房|体检|口腔|牙科|医保|美团买药|叮当快药|医疗)",
            re.IGNORECASE,
        ),
    ),
    (
        "娱乐桌游",
        "#eab308",
        re.compile(
            r"(桌游|集石|摩点|新物集|剧本杀|电影|影院|演出|门票|游戏|任天堂|switch|playstation|psn|bilibili|哔哩哔哩|b站|漫画|小说|短剧)",
            re.IGNORECASE,
        ),
    ),
)

_CREDIT_RE = re.compile(r"(信用卡|花呗|白条|credit)", re.IGNORECASE)


def _clean_account_name(raw_alias: str, currency: str) -> str:
    base = (raw_alias or "").split("&", 1)[0].strip()
    if not base or base in ("/", "-", "未知"):
        base = "其他未标注账户"
    # 合并全半角括号差异（如 招商银行借记卡（0001） vs 招商银行储蓄卡(0001)）
    base = base.replace("（", "(").replace("）", ")")
    base = re.sub(r"借记卡\(", "储蓄卡(", base)
    return f"{base} [{currency}]" if currency != "CNY" else base


def _classify(merchant: str, notes: str, kind: str) -> str:
    if kind == "收入":
        return "收入"
    if kind == "划转":
        return "划转还款"
    hay = f"{merchant or ''} {notes or ''}"
    for name, _, pat in _CATEGORY_RULES:
        if pat.search(hay):
            return name
    return "其他消费"


def load_siming_ledger(db_path: Path) -> dict[str, Any]:
    if not db_path.is_file():
        raise FileNotFoundError(f"找不到账本库：{db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            """
            SELECT txn_id, occurred_at, amount_cents, currency, account_alias, card_last4,
                   kind, status, merchant, notes
            FROM ledger_txns
            WHERE COALESCE(UPPER(status), '') NOT IN ('VOID_DUPLICATE', 'VOID', 'CANCELLED')
            ORDER BY occurred_at ASC
            """
        ).fetchall()
    finally:
        conn.close()

    accounts: dict[tuple[str, str], dict[str, Any]] = {}
    categories: dict[str, dict[str, str]] = {}
    color_map = {n: c for n, c, _ in _CATEGORY_RULES}
    color_map.update({"收入": "#34d399", "划转还款": "#64748b", "其他消费": "#94a3b8"})

    txns_out: list[dict[str, Any]] = []
    skipped_transfers = 0

    for r in rows:
        txn_id = str(r["txn_id"])
        date_s = str(r["occurred_at"] or "")[:10]
        if len(date_s) != 10:
            continue
        amt_yuan = round(int(r["amount_cents"] or 0) / 100.0, 2)
        if amt_yuan <= 0:
            continue
        ccy = str(r["currency"] or "CNY").strip().upper()
        if ccy == "RMB":
            ccy = "CNY"
        kind = str(r["kind"] or "消费").strip()
        merchant = str(r["merchant"] or "").strip() or (kind if kind in ("收入", "划转") else "未标注商户")
        notes = str(r["notes"] or "").strip()
        acc_name = _clean_account_name(str(r["account_alias"] or ""), ccy)
        acc_type = "credit_card" if _CREDIT_RE.search(acc_name) else "depository"

        ak = (acc_name, ccy)
        if ak not in accounts:
            accounts[ak] = {
                "name": acc_name,
                "currency": ccy,
                "account_type": acc_type,
                "count": 0,
            }
        accounts[ak]["count"] += 1

        cat_name = _classify(merchant, notes, kind)
        classification = "income" if kind == "收入" else "expense"
        if cat_name not in categories:
            categories[cat_name] = {
                "name": cat_name,
                "color": color_map.get(cat_name, "#94a3b8"),
                "classification": classification,
            }

        # Sure (Maybe fork) 记账符号约定：消费(outflow)为正数，收入/退款(inflow)为负数
        if kind == "消费":
            sure_amount = amt_yuan
            sure_kind = "standard"
        elif kind in ("收入", "退款"):
            sure_amount = -amt_yuan
            sure_kind = "standard"
        elif kind == "划转":
            skipped_transfers += 1
            continue  # 单边划转避免扭曲收支统计；后续可在 Sure 内配对双边转账
        else:
            sure_amount = amt_yuan
            sure_kind = "standard"

        marker = f"[siming:{txn_id}]"
        full_notes = f"{notes} {marker}".strip() if notes else marker
        txns_out.append(
            {
                "txn_id": txn_id,
                "date": date_s,
                "name": merchant[:120],
                "amount": sure_amount,
                "currency": ccy,
                "account_name": acc_name,
                "category": cat_name,
                "kind": sure_kind,
                "siming_kind": kind,
                "notes": full_notes[:400],
            }
        )

    return {
        "accounts": list(accounts.values()),
        "categories": list(categories.values()),
        "transactions": txns_out,
        "skipped_transfers": skipped_transfers,
    }


def export_csv_per_account(bundle: dict[str, Any], out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    by_acc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for t in bundle["transactions"]:
        by_acc[t["account_name"]].append(t)
    written: list[Path] = []
    for acc_name, items in sorted(by_acc.items()):
        safe = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", acc_name).strip("_") or "account"
        p = out_dir / f"{safe}.csv"
        with p.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["date", "name", "amount", "currency", "category", "notes"])
            for t in items:
                w.writerow([t["date"], t["name"], f"{t['amount']:.2f}", t["currency"], t["category"], t["notes"]])
        written.append(p)
    return written


def _ensure_login_secret(secrets_dir: Path, email: str) -> tuple[str, Path]:
    secrets_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(secrets_dir, 0o700)
    cred_path = secrets_dir / "sure-login.txt"
    if cred_path.is_file():
        for ln in cred_path.read_text(encoding="utf-8").splitlines():
            if ln.startswith("PASSWORD="):
                return ln.split("=", 1)[1].strip(), cred_path
    pwd = secrets.token_urlsafe(18)
    fd = os.open(str(cred_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"URL=http://127.0.0.1:3032\nEMAIL={email}\nPASSWORD={pwd}\n")
    return pwd, cred_path


_RAILS_IMPORTER_RUBY = r"""
require "json"

payload = JSON.parse(STDIN.read)
email = payload.fetch("email")
password = payload.fetch("password")

# 1. 查找或创建 Family 与 User（幂等）
user = User.find_by(email: email)
if user
  family = user.family
else
  family = Family.first || Family.create!(
    name: "Siming Personal",
    currency: "CNY",
    locale: "zh-CN",
    timezone: "Asia/Shanghai",
    date_format: "%Y-%m-%d"
  )
  user = User.new(
    email: email,
    family: family,
    first_name: "Siming",
    last_name: "Owner",
    role: "admin",
    password: password,
    password_confirmation: password
  )
  user.onboarded_at = Time.current if user.respond_to?(:onboarded_at=)
  user.save!
end

# 2. 创建或复用 Categories
cat_map = {}
family.categories.each { |c| cat_map[c.name] = c }
payload.fetch("categories").each do |c|
  next if cat_map[c["name"]]
  attrs = { name: c["name"], color: c["color"] }
  attrs[:classification] = c["classification"] if Category.column_names.include?("classification")
  cat_map[c["name"]] = family.categories.create!(attrs)
end

# 3. 创建或复用 Accounts
acc_map = {}
family.accounts.each { |a| acc_map[[a.name, a.currency]] = a }
payload.fetch("accounts").each do |a|
  key = [a["name"], a["currency"]]
  next if acc_map[key]
  accountable = if a["account_type"] == "credit_card" && defined?(CreditCard)
                  CreditCard.new
                else
                  Depository.new
                end
  acc_map[key] = family.accounts.create!(
    name: a["name"],
    currency: a["currency"],
    balance: 0,
    accountable: accountable
  )
end

# 4. 读取已导入的 [siming:<txn_id>] 集合，保证绝对幂等
existing_ids = {}
family.entries.where("notes LIKE ?", "%[siming:%").pluck(:notes).each do |n|
  next unless n
  n.scan(/\[siming:([^\]]+)\]/).flatten.each { |tid| existing_ids[tid] = true }
end

imported = 0
skipped = 0
errors = []

payload.fetch("transactions").each do |t|
  if existing_ids[t["txn_id"]]
    skipped += 1
    next
  end
  acc = acc_map[[t["account_name"], t["currency"]]]
  next unless acc
  cat = cat_map[t["category"]]
  begin
    tx_attrs = { category: cat }
    tx_attrs[:kind] = t["kind"] if defined?(Transaction) && Transaction.column_names.include?("kind")
    entry_attrs = {
      name: t["name"],
      date: Date.parse(t["date"]),
      amount: t["amount"],
      currency: t["currency"],
      notes: t["notes"],
      entryable: Transaction.new(tx_attrs)
    }
    acc.entries.create!(entry_attrs)
    existing_ids[t["txn_id"]] = true
    imported += 1
  rescue => e
    errors << "#{t['txn_id']}: #{e.class}: #{e.message}" if errors.length < 10
  end
end

# 5. 触发一次账户余额同步（若支持）
family.accounts.each do |a|
  a.sync_later if a.respond_to?(:sync_later)
end

puts JSON.generate({
  ok: errors.empty?,
  user_email: user.email,
  family_id: family.id,
  accounts_total: family.accounts.count,
  categories_total: family.categories.count,
  entries_total: family.entries.count,
  imported: imported,
  skipped_existing: skipped,
  error_sample: errors
})
"""


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="将 siming.db 账本幂等导入本地自托管 Sure 财务 OS")
    ap.add_argument("--db", type=Path, default=DEFAULT_DB, help="siming.db 路径（只读打开）")
    ap.add_argument("--container", default=DEFAULT_CONTAINER, help="Sure web 容器名")
    ap.add_argument("--email", default=DEFAULT_EMAIL, help="Sure 登录邮箱")
    ap.add_argument("--dry-run", action="store_true", help="仅统计将导入的数据，不写 Sure")
    ap.add_argument("--export-csv", type=Path, default=None, help="按账户导出 Sure 可上传 CSV 到指定目录")
    args = ap.parse_args(argv)

    bundle = load_siming_ledger(args.db)
    summary = {
        "accounts": len(bundle["accounts"]),
        "categories": len(bundle["categories"]),
        "transactions": len(bundle["transactions"]),
        "skipped_transfers": bundle["skipped_transfers"],
        "account_list": bundle["accounts"],
    }

    if args.export_csv is not None:
        files = export_csv_per_account(bundle, args.export_csv)
        summary["csv_files"] = [str(p) for p in files]

    if args.dry_run:
        print(json.dumps({"ok": True, "mode": "dry_run", **summary}, ensure_ascii=False, indent=2))
        return 0

    pwd, cred_path = _ensure_login_secret(DEFAULT_SECRETS_DIR, args.email)
    payload = json.dumps({"email": args.email, "password": pwd, **bundle}, ensure_ascii=False)
    proc = subprocess.run(
        ["docker", "exec", "-i", args.container, "bundle", "exec", "rails", "runner", _RAILS_IMPORTER_RUBY],
        input=payload,
        text=True,
        capture_output=True,
        timeout=300,
    )
    if proc.returncode != 0:
        print(json.dumps({"ok": False, "returncode": proc.returncode, "stderr": proc.stderr[-1000:], "stdout": proc.stdout[-500:]}, ensure_ascii=False, indent=2))
        return proc.returncode
    # Rails runner 前面可能有提示行，取最后一个 JSON 行
    out_lines = [ln.strip() for ln in proc.stdout.splitlines() if ln.strip().startswith("{")]
    res = json.loads(out_lines[-1]) if out_lines else {"raw": proc.stdout.strip()}
    res["credentials_file"] = str(cred_path)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
