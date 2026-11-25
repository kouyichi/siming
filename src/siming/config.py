"""配置文件加载与默认模板初始化（~/.config/siming/）。"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path
from typing import Any, Optional

import yaml

from siming.crypto import ensure_edge_token
from siming.paths import SimingPaths, get_paths

DEFAULT_POLICY: dict[str, Any] = {
    "policy_version": 1,
    "timezone": "Asia/Shanghai",
    "quiet_hours": {
        "start": "23:30",
        "end": "07:30",
    },
    "windows": {
        "morning_brief": "08:15",
        "pickup_batch": "18:00",
        "night_batch_run": "01:30",
    },
    "channels": {
        "qq_target": "",
        "feishu_fallback": "",
        "hermes_bin": str(Path.home() / ".local" / "bin" / "hermes"),
    },
    "edge": {
        "ssh_host": "",
        "public_in_url": "",
        "remote_cli": "sudo -n -u siming /usr/bin/python3 /opt/siming-edge/siming_edge.py",
        "pull_interval_sec": 300,
    },
    "coding_plan": {
        "ai_quota_url": "http://127.0.0.1:18101/api/ai-quota",
        "min_remaining_percent": 10.0,
        "claude_bin": str(Path.home() / ".local" / "bin" / "claude"),
        "codex_bin": "codex",
        "herdr_bin": "/usr/bin/herdr",
    },
    "guardrails": {
        "worker_timeout_sec": 1800,
        "max_done_check_attempts": 2,
        "approval_ttl_minutes": 30,
        "default_wait_ttl_days": 14,
        "max_wait_ttl_days": 30,
        "max_explicit_wait_ttl_days": 400,
        "ended_subscription_resume_days": 14,
        "rule_max_fires_per_hour": 10,
        "reboot_suppress_gap_minutes": 30,
        "boardgame_cabinet_boxes_limit": 200,
        "boardgame_cabinet_volume_m3_limit": 3.0,
    },
    "whitelisted_repos": {
        "siming": str(Path.home() / "Work" / "siming"),
    },
    "knowledge_roots": [
        str(Path.home() / "Documents" / "Obsidian Vault"),
        str(Path.home() / "Work" / "siming" / "docs"),
    ],
    "watched_prs": [],
    "lunar_birthdays": [],
    "places": {
        "家": "示例小区A",
        "公司": "示例写字楼",
    },
    "pickup_sites": {
        "home_tokens": ["示例小区A", "阳光花园"],
        "alt_home_tokens": ["示例小区B", "翠竹苑"],
        "office_tokens": ["示例写字楼", "科技园大厦"],
    },
    "vehicle": {
        "kind": "ev",
        "model": "Electric Vehicle (LFP 60kWh)",
        "km_per_pct": 4.35,
        "charging_habit": "周边超级充电站 / 外出目的地顺路补电（LFP 电池建议定期满充 100% 校准 BMS）",
        "auto_climate_min_soc_pct": 25.0,
        "auto_climate_cold_c": 12.0,
        "auto_climate_hot_c": 29.0,
        "tail_limit_exempt": True,
    },
    "tesla": {
        "home_garage_coords": [],
        "virtual_key_pairing_url": "",
    },
    "ledger_alerts": {
        "super_large_cny_yuan": 20000,
        "super_large_fx_usd": 2000,
        "realtime_reimburse_cursor_push": False,
    },
    "night_owl": {
        "active_grace_minutes": 35,
        "sleep_idle_minutes": 40,
        "sleep_window_start": "00:30",
        "sleep_window_end": "12:00",
        "latest_morning_brief": "11:45",
        "explicit_sleep_ttl_hours": 11.0,
    },
    "spending_caps": {
        "luckin_single_order_yuan": 65.0,
        "meituan_single_order_yuan": 120.0,
        "jd_replenish_yuan": 500.0,
    },
    "meituan_pay": {
        "hermes_handoff": {
            "enabled": False,
            "provider": "",
            "model": "",
            "instruction_file": "",
            "max_turns": 20,
            "timeout_sec": 150,
        },
    },
    "qq_nlu": {
        "llm_fallback": False,
    },
    "compute_monitor": {
        "redpill_billing_url": "",
    },
    "self_heal": {
        "nous_billing_url": "",
        "intranet_probe_host": "",
        "remote_host_keywords": ["gpu", "remote"],
    },
    "tower": {
        "allowed_hosts": ["127.0.0.1", "localhost", "::1"],
    },
    "github": {
        "my_login": "",
    },
}

DEFAULT_SOURCES_TOML = """# 司命数据源健康配置（五项检查：进程/登录态/读取/静默时长/通知可达）

[sources.agent_sessions]
kind = "local_log"
max_silence_sec = 900
description = "Claude / Codex / agy / OpenCode / Hermes 用户输入 2m 增量扫描"

[sources.wait_source]
kind = "internal"
max_silence_sec = 900
description = "跨 Agent 挂起触发源（time / event / probe / intent）"

[sources.buy_track]
kind = "local_json"
max_silence_sec = 86400
path = "~/buy/track/data/auto.json"
description = "京东/淘宝大件订单与价保跟踪（可选）"

[sources.price_watch]
kind = "local_json"
max_silence_sec = 86400
path = "~/.hermes/scripts/buy-price-watch/orders.json"
description = "网购商品通用保价监控（降价提醒，不自动代点申请）"

[sources.cn_calendar]
kind = "calendar"
max_silence_sec = 691200
description = "中国节假日调休、城市限行、12306/114 与农历窗口"

[sources.memos_vault]
kind = "local_md"
max_silence_sec = 86400
path = "~/Documents/Obsidian Vault/60-共享记忆"
description = "Obsidian 共享记忆与本地备忘"

[sources.gh_prs]
kind = "probe"
max_silence_sec = 7200
description = "关注 GitHub PR 状态与点名变化"

[sources.edge_vps]
kind = "ssh_edge"
max_silence_sec = 900
description = "公网 VPS 边缘收信队列与预排发件箱同步（可选）"

[sources.ios_hb]
kind = "edge_heartbeat"
max_silence_sec = 93600
description = "iPhone 每日 07:50 快捷指令心跳（未配手机前标 UNCONFIGURED）"

[sources.mail163]
kind = "omamail"
max_silence_sec = 3600
host = "imap.163.com"
port = 993
folders = ["INBOX"]
secret_name = "mail163"
description = "Omamail / IMAP 多邮箱桥接聚合摘要（事件归口，按单箱独立判活）"

[sources.omamail_163]
kind = "omamail_account"
max_silence_sec = 7200
description = "Omamail 163 邮箱（数电发票与国内账单只读）"

[sources.omamail_yahoo]
kind = "omamail_account"
max_silence_sec = 86400
description = "Omamail Yahoo 邮箱（Apple/云订阅续费只读）"

[sources.omamail_gmail]
kind = "omamail_account"
max_silence_sec = 7200
description = "Omamail Gmail 邮箱（Stripe/海外收据 OAuth 只读）"

[sources.open_meteo]
kind = "probe"
max_silence_sec = 691200
description = "Open-Meteo 逐小时天气预报（工作日出门带伞与气温骤变穿衣提醒）"

[sources.meituan_orders]
kind = "cdp_9222"
max_silence_sec = 172800
description = "美团外卖 H5 订单商品明细只读同步（常买消耗品补货雷达）"

[sources.douyin_orders]
kind = "cdp_9222"
max_silence_sec = 604800
description = "抖音商城网页订单只读同步与截屏订单解析（未登录网页版前标 UNCONFIGURED）"

[sources.ios_tesla]
kind = "edge_push"
max_silence_sec = 604800
description = "特斯拉 iPhone 蓝牙断开零唤醒状态同步（首次入站前标 UNCONFIGURED）"
"""

DEFAULT_ACCOUNTS_TOML = """# 多卡账本配置（仅存别名与后 4 位尾号，禁存完整卡号或密码）
# 参考 docs/SETUP_GUIDE.md 按实际卡片修改后即自动生效。

[[accounts]]
alias = "主力信用卡"
bank = "招商银行"
kind = "credit"
last4 = "0000"
sms_threshold_yuan = 0
statement_day = 7
due_day = 25
reimburse_cursor = false
configured = false
verified_through = ""

[[accounts]]
alias = "主力借记卡"
bank = "招商银行"
kind = "debit"
last4 = "0001"
sms_threshold_yuan = 0
repay_card = false
configured = false
verified_through = ""

[[accounts]]
alias = "借记卡B"
bank = "工商银行"
kind = "debit"
last4 = "0002"
sms_threshold_yuan = 300
configured = false
verified_through = ""

[[accounts]]
alias = "借记卡C"
bank = "建设银行"
kind = "debit"
last4 = "0003"
sms_threshold_yuan = 500
configured = false
verified_through = ""
"""

# 内置基线中国法定节假日与北京尾号轮换规则（每周日可在线同步 NateScarlet/holiday-cn 更新）
DEFAULT_CALENDAR_RULES: dict[str, Any] = {
    "version": "2026.11-baseline",
    "source_url": "https://raw.githubusercontent.com/NateScarlet/holiday-cn/master/2026.json",
    "fetched_at": "2026-11-16T00:00:00+08:00",
    # days: date -> {"name": ..., "isOffDay": true (放假休息) / false (周末调休补班)}
    "days": {
        "2026-01-01": {"name": "元旦", "isOffDay": True},
        "2026-01-02": {"name": "元旦", "isOffDay": True},
        "2026-01-03": {"name": "元旦", "isOffDay": True},
        "2026-01-04": {"name": "元旦补班", "isOffDay": False},
        "2026-02-14": {"name": "春节补班", "isOffDay": False},
        "2026-02-15": {"name": "春节", "isOffDay": True},
        "2026-02-16": {"name": "春节", "isOffDay": True},
        "2026-02-17": {"name": "春节", "isOffDay": True},
        "2026-02-18": {"name": "春节", "isOffDay": True},
        "2026-02-19": {"name": "春节", "isOffDay": True},
        "2026-02-20": {"name": "春节", "isOffDay": True},
        "2026-02-21": {"name": "春节", "isOffDay": True},
        "2026-02-22": {"name": "春节", "isOffDay": True},
        "2026-02-23": {"name": "春节", "isOffDay": True},
        "2026-02-28": {"name": "春节补班", "isOffDay": False},
        "2026-04-04": {"name": "清明节", "isOffDay": True},
        "2026-04-05": {"name": "清明节", "isOffDay": True},
        "2026-04-06": {"name": "清明节", "isOffDay": True},
        "2026-04-26": {"name": "劳动节补班", "isOffDay": False},
        "2026-05-01": {"name": "劳动节", "isOffDay": True},
        "2026-05-02": {"name": "劳动节", "isOffDay": True},
        "2026-05-03": {"name": "劳动节", "isOffDay": True},
        "2026-05-04": {"name": "劳动节", "isOffDay": True},
        "2026-05-05": {"name": "劳动节", "isOffDay": True},
        "2026-05-09": {"name": "劳动节补班", "isOffDay": False},
        "2026-06-19": {"name": "端午节", "isOffDay": True},
        "2026-06-20": {"name": "端午节", "isOffDay": True},
        "2026-06-21": {"name": "端午节", "isOffDay": True},
        "2026-09-20": {"name": "中秋国庆补班", "isOffDay": False},
        "2026-09-25": {"name": "中秋节", "isOffDay": True},
        "2026-09-26": {"name": "中秋节", "isOffDay": True},
        "2026-09-27": {"name": "中秋节", "isOffDay": True},
        "2026-10-01": {"name": "国庆节", "isOffDay": True},
        "2026-10-02": {"name": "国庆节", "isOffDay": True},
        "2026-10-03": {"name": "国庆节", "isOffDay": True},
        "2026-10-04": {"name": "国庆节", "isOffDay": True},
        "2026-10-05": {"name": "国庆节", "isOffDay": True},
        "2026-10-06": {"name": "国庆节", "isOffDay": True},
        "2026-10-07": {"name": "国庆节", "isOffDay": True},
        "2026-10-10": {"name": "国庆补班", "isOffDay": False},
    },
    # 北京工作日高峰尾号限行季度轮换（周一至周五对应尾号组合；法定假日及调休补班周末均不限行）
    "beijing_tail_rotations": [
        {
            "start": "2026-03-30",
            "end": "2026-06-28",
            "weekday_tails": {"1": "5/0", "2": "1/6", "3": "2/7", "4": "3/8", "5": "4/9"},
        },
        {
            "start": "2026-06-29",
            "end": "2026-09-27",
            "weekday_tails": {"1": "4/9", "2": "5/0", "3": "1/6", "4": "2/7", "5": "3/8"},
        },
        {
            "start": "2026-09-28",
            "end": "2026-12-27",
            "weekday_tails": {"1": "3/8", "2": "4/9", "3": "5/0", "4": "1/6", "5": "2/7"},
        },
        {
            "start": "2026-12-28",
            "end": "2027-03-28",
            "weekday_tails": {"1": "2/7", "2": "3/8", "3": "4/9", "4": "5/0", "5": "1/6"},
        },
    ],
    # 12306 北京常见车站起售时刻（提前 14 天放票，起售前 15m 提醒）
    "rail_12306_stations": {
        "北京西": "08:00",
        "北京丰台": "08:00",
        "北京": "10:00",
        "北京朝阳": "10:00",
        "北京北": "12:00",
        "清河": "12:00",
        "北京南": "12:30",
    },
    # 北京 114 常见医院放号窗口（提前 10m 提醒）
    "hospital_114_slots": {
        "协和医院": "15:00",
        "北医三院": "07:00",
        "北大人民医院": "08:00",
        "天坛医院": "15:00",
        "友谊医院": "15:00",
        "宣武医院": "07:00",
        "同仁医院": "15:00",
    },
}


def bootstrap_config(paths: Optional[SimingPaths] = None) -> SimingPaths:
    p = paths or get_paths()
    p.ensure_dirs()
    ensure_edge_token(p)

    policy_path = p.config_dir / "policy.yaml"
    if not policy_path.exists():
        policy_path.write_text(
            yaml.safe_dump(DEFAULT_POLICY, allow_unicode=True, sort_keys=False),
            encoding="utf-8",
        )

    sources_path = p.config_dir / "sources.toml"
    if not sources_path.exists():
        sources_path.write_text(DEFAULT_SOURCES_TOML, encoding="utf-8")

    accounts_path = p.config_dir / "accounts.toml"
    if not accounts_path.exists():
        accounts_path.write_text(DEFAULT_ACCOUNTS_TOML, encoding="utf-8")

    cal_path = p.config_dir / "calendar_rules.json"
    if not cal_path.exists():
        cal_path.write_text(
            json.dumps(DEFAULT_CALENDAR_RULES, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return p


def load_policy(paths: Optional[SimingPaths] = None) -> dict[str, Any]:
    p = bootstrap_config(paths)
    loaded = yaml.safe_load((p.config_dir / "policy.yaml").read_text(encoding="utf-8")) or {}
    merged = json.loads(json.dumps(DEFAULT_POLICY))
    for k, v in loaded.items():
        if isinstance(v, dict) and isinstance(merged.get(k), dict):
            merged[k].update(v)
        else:
            merged[k] = v
    return merged


def load_sources_config(paths: Optional[SimingPaths] = None) -> dict[str, dict[str, Any]]:
    p = bootstrap_config(paths)
    data = tomllib.loads((p.config_dir / "sources.toml").read_text(encoding="utf-8"))
    return data.get("sources", {})


def load_accounts_config(paths: Optional[SimingPaths] = None) -> list[dict[str, Any]]:
    p = bootstrap_config(paths)
    data = tomllib.loads((p.config_dir / "accounts.toml").read_text(encoding="utf-8"))
    return list(data.get("accounts", []))


def load_calendar_rules(paths: Optional[SimingPaths] = None) -> dict[str, Any]:
    p = bootstrap_config(paths)
    cal_path: Path = p.config_dir / "calendar_rules.json"
    return json.loads(cal_path.read_text(encoding="utf-8"))
