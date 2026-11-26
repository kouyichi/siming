"""司命（siming）命令行入口。"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import timedelta
from pathlib import Path
from typing import Optional

from siming.approval import create_approval, verify_and_apply_approval
from siming.cn_calendar import day_info, parse_duration_seconds, sync_holiday_cn
from siming.collectors.edge_sync import sync_edge_node
from siming.db import SimingDB, iso_now, now_local
from siming.doctor import format_doctor_report, run_doctor, run_replay_fixtures
from siming.evald import (
    consume_pending_effects,
    process_new_events,
    run_single_effect_worker,
    run_tick,
)
from siming.install_plan import (
    build_installation_projection,
    format_arrival_inspection_checklist,
    format_installation_checklist,
    format_onsite_grouped_checklist,
    mark_arrival_inspected,
)
from siming.ledger.export_zip import import_bill_archive
from siming.ledger.reconcile import generate_monthly_ledger_report, resolve_ledger_txn
from siming.ledger.reimburse import (
    build_didi_deeplink,
    format_didi_ride_card,
    load_saved_places,
    set_saved_place,
    summarize_reimbursement_bundle,
)
from siming.ledger.reimburse_audit import (
    audit_candidate_batch,
    format_audit_batch_report,
    format_pack_draft_report,
    format_reimbursement_cursor,
    get_reimbursement_cursor_status,
    mark_batch_returned,
    mark_batch_submitted,
    pack_reimbursement_draft,
)
from siming.modules import format_modules_report, list_modules_status, set_module_enabled
from siming.price_watch import (
    add_price_watch,
    collect_price_watch,
    format_price_watch_report,
    list_price_watches,
    remove_price_watch,
)
from siming.remote import ask_knowledge, dispatch_repo_task, drop_item
from siming.router import (
    build_morning_brief,
    explain_item,
    forget_memory,
    handle_inbound_qq,
    record_feedback,
    send_qq_message,
)
from siming.task_watch import render_remote_srr_hook, run_wrapped_command, watch_herdr_pane
from siming.waits import add_wait, cancel_wait, list_waits, trigger_wait_now
from siming.watchdog import run_watchdog

AGENTS_DEFER_RULE_BLOCK = """<!-- siming-defer-rule -->
## 跨会话延后与条件挂起（司命 siming）
- 当用户表达「等某时间 / 某状态 / 某 PR 合并 / 某件事发生后再提醒我或接着改」时，当场调用 `~/.local/bin/siming defer add` 登记挂起项（例如 `siming defer add --when "..." --mode remind|resume|stage|run --cwd "$PWD" --quote "用户原话"`），并在回复末尾附上一行 `[Wxx] 已挂起` 回执。
- 查看当前活跃意图挂起索引可读 `~/Documents/Obsidian Vault/60-共享记忆/ACTIVE_WAITS.md`。"""


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="siming", description="司命（siming）：本机个人助理与跨 Agent 续接命令行")
    sub = p.add_subparsers(dest="cmd")

    # defer
    p_defer = sub.add_parser("defer", help="跨 Agent 挂起任务管理（add / list / fire / cancel）")
    d_sub = p_defer.add_subparsers(dest="defer_cmd")

    p_d_add = d_sub.add_parser("add", help="新增一条显式授权挂起项")
    p_d_add.add_argument("--when", required=True, help="触发条件：时间 / time:... / probe:... / event:... / intent:...")
    p_d_add.add_argument("--mode", default="remind", choices=["remind", "resume", "stage", "run"])
    p_d_add.add_argument("--every", default="30m", help="probe 轮询间隔（默认 30m）")
    p_d_add.add_argument("--ttl", default="14d", help="最长有效期（默认 14d，硬上限 30d）")
    p_d_add.add_argument("--cwd", default="", help="工作目录")
    p_d_add.add_argument("--agent", default="", help="原会话 Agent 名（claude / codex / agy / hermes）")
    p_d_add.add_argument("--session", default="", help="原会话 ID（供 resume 使用）")
    p_d_add.add_argument("--runner", default="", help="后台执行器偏好（codex / claude）")
    p_d_add.add_argument("--task", default="", help="唤醒后要执行的具体任务说明")
    p_d_add.add_argument("--context", default="", help="现场上下文摘要")
    p_d_add.add_argument("--done-check", default="", help="验收命令（退出码必须为 0）")
    p_d_add.add_argument("--quote", default="", help="用户原话")

    p_d_list = d_sub.add_parser("list", help="查看挂起项列表")
    p_d_list.add_argument("--all", action="store_true", help="包含已完成/已取消项")

    p_d_fire = d_sub.add_parser("fire", help="立即手动触发指定挂起项")
    p_d_fire.add_argument("wait_id")

    p_d_cancel = d_sub.add_parser("cancel", help="取消指定挂起项")
    p_d_cancel.add_argument("wait_id")

    # ask
    p_ask = sub.add_parser("ask", help="只读检索全机私有事实（附文件:行号）")
    p_ask.add_argument("query")
    p_ask.add_argument("--llm", action="store_true", help="用 Coding Plan 归纳检索证据")

    # dispatch
    p_disp = sub.add_parser("dispatch", help="向受控仓库派发独立 worktree 编码/调研任务")
    p_disp.add_argument("repo")
    p_disp.add_argument("task")
    p_disp.add_argument("--done-check", default="")
    p_disp.add_argument("--runner", default="")

    # drop
    p_drop = sub.add_parser("drop", help="一秒投递截屏、剪贴板或文本建档（本地先拦验证码）")
    p_drop.add_argument("text", nargs="?", default="")
    p_drop.add_argument("--latest-shot", action="store_true", help="抓取 ~/Pictures 最新截图")
    p_drop.add_argument("--paste", action="store_true", help="读取 Wayland 剪贴板文本")
    p_drop.add_argument("--file", default="", help="指定图片或文本文件路径")
    p_drop.add_argument("--note", default="", help="附加一句备注")

    # today
    p_today = sub.add_parser("today", help="查看今日司命四合一晨札盘点")
    p_today.add_argument("--full", action="store_true", help="输出完整四合一晨札详版")
    p_today.add_argument("--send", action="store_true", help="立即经 router 发送到手机 QQ")

    # install-check (S2-B / S3-1)
    p_inst = sub.add_parser("install-check", help="查看新房家电到货外箱初验、现场作战单与送装核对单")
    p_inst.add_argument("--arrival", action="store_true", help="输出集中到货外箱初验清单（核气源/型号/挂架/六面破损）")
    p_inst.add_argument("--onsite", action="store_true", help="输出按施工梯队与预约状态分组的现场作战单")
    p_inst.add_argument("--mark-arrival", default="", help="登记指定设备或 all 已完成外箱初验")
    p_inst.add_argument("--json", action="store_true")

    # price-watch (S3-1: 1=B)
    p_pw = sub.add_parser("price-watch", help="网购商品通用保价监控（兼容 15 单大家电与后续自添商品）")
    pw_sub = p_pw.add_subparsers(dest="pw_cmd")
    p_pw_ls = pw_sub.add_parser("list", help="列出当前全部保价监控商品与退差状态")
    p_pw_ls.add_argument("--json", action="store_true")
    pw_sub.add_parser("check", help="立即执行一轮保价水位比对并推送新达标项")
    p_pw_add = pw_sub.add_parser("add", help="新增或更新一件保价监控商品")
    p_pw_add.add_argument("sku_or_url", help="京东商品 SKU 编号或商品页链接")
    p_pw_add.add_argument("--name", required=True, help="商品显示名称")
    p_pw_add.add_argument("--paid", required=True, help="实付金额（元）")
    p_pw_add.add_argument("--order", default="", help="可选订单号")
    p_pw_add.add_argument("--date", default="", help="购买日期 YYYY-MM-DD（默认今日）")
    p_pw_add.add_argument("--days", type=int, default=30, help="保价天数（默认 30 天，7 天商品可传 7）")
    p_pw_rm = pw_sub.add_parser("remove", help="按订单号、SKU 或名称移除保价监控项")
    p_pw_rm.add_argument("target")
    p_pw_rm.add_argument("--mute-only", action="store_true", help="仅停止提醒而保留 orders.json 条目")

    # module (个人功能模块一键开关)
    p_mod = sub.add_parser("module", help="查看或一键开启/关闭个人功能模块（保留历史数据）")
    mod_sub = p_mod.add_subparsers(dest="mod_cmd")
    p_mod_ls = mod_sub.add_parser("ls", help="查看全部模块状态与 14d 使用统计")
    p_mod_ls.add_argument("--json", action="store_true")
    p_mod_on = mod_sub.add_parser("on", help="开启指定模块")
    p_mod_on.add_argument("name")
    p_mod_off = mod_sub.add_parser("off", help="关闭指定模块（停止后台自动触发与推送）")
    p_mod_off.add_argument("name")

    # remote-hook (S3-4: 4=B)
    p_rh = sub.add_parser("remote-hook", help="输出远端服务器零轮询完工回执 `srr` 包装函数或一键部署受限子令牌")
    p_rh.add_argument("--host", default="", help="指定主机显示别名（默认取远端 hostname -s）")
    p_rh.add_argument("--install-local", action="store_true", help="同步在本机安装 ~/.local/bin/srr")
    p_rh.add_argument("--deploy", default="", help="通过 SSH 将受限 receipt-token 安全下发至指定远端主机（如 gpu-server）")

    # run & watch (S2-C)
    p_run = sub.add_parser("run", help="零轮询长任务包装器：跑完即推退出码、耗时与脱敏末尾 5 行到手机 QQ")
    p_run.add_argument("--name", default="", help="任务显示名称")
    p_run.add_argument("--cwd", default="", help="工作目录（默认当前目录）")
    p_run.add_argument("--no-qq", action="store_true", help="仅落库与记录日志，不发 QQ")
    p_run.add_argument("--respect-quiet", action="store_true", help="若在安静时段完成则暂存至次日晨札")
    p_run.add_argument("cmd_argv", nargs=argparse.REMAINDER, help="待执行命令（前加 --）")

    p_watch = sub.add_parser("watch", help="监听指定 herdr Agent 面板本轮完成或阻塞提醒")
    p_watch.add_argument("target", nargs="?", default="current", help="herdr pane_id / tab_id 或 current")
    p_watch.add_argument("--label", default="", help="自定义提醒标题")

    # cases / done
    p_cases = sub.add_parser("cases", help="查看当前事项单据（cases）")
    p_cases.add_argument("--all", action="store_true")

    p_done = sub.add_parser("done", help="销账或取消一条事项单据（如 siming done S39）")
    p_done.add_argument("case_id", help="事项编号，如 S39")
    p_done.add_argument("--cancel", action="store_true", help="标记为「取消」而非「完成」")
    p_done.add_argument("--note", default="", help="备注说明")

    # apply
    p_apply = sub.add_parser("apply", help="纯代码校验并执行 A2 审批口令（如 A01）")
    p_apply.add_argument("token")

    # feedback / explain / forget / pause
    p_fb = sub.add_parser("feedback", help="记录提醒反馈（已办 / 改期 / 少推 / 别再提）")
    p_fb.add_argument("target_id")
    p_fb.add_argument("action", choices=["已办", "改期", "少推", "别再提"])
    p_fb.add_argument("--note", default="")

    p_exp = sub.add_parser("explain", help="追问某条提醒或单据的触发依据与原件引用")
    p_exp.add_argument("target_id", nargs="?", default="")

    p_fgt = sub.add_parser("forget", help="软失效某条推断记忆并写入 inbox 标记")
    p_fgt.add_argument("keyword")

    p_pause = sub.add_parser("pause", help="暂停或恢复后台自动 worker（如 4h 或 off）")
    p_pause.add_argument("duration")

    # ziwei 个人参考包
    p_zw = sub.add_parser("ziwei", help="紫微个人参考包：sync 从 ziwei-doushu 拉本命 v4.0 解读与《天纪》原话；natal 查本命 13 维")
    zw_sub = p_zw.add_subparsers(dest="zw_cmd")
    p_zw_sync = zw_sub.add_parser("sync", help="重建 <data_dir>/ziwei/ 参考包（需 gh 与 zstd）")
    p_zw_sync.add_argument("--repo", default="", help="ziwei-doushu 本地仓路径（默认 ~/Work/ziwei-doushu）")
    p_zw_natal = zw_sub.add_parser("natal", help="查看本命 13 维定调或某一维解读")
    p_zw_natal.add_argument("dim", nargs="?", default="", help="维度或宫名，如 财帛 / 事业 / wealth")
    p_zw_natal.add_argument("--full", action="store_true", help="输出该维完整推演")

    # calendar
    p_cal = sub.add_parser("calendar", help="查看中国日历调休/限行或同步最新节假日表")
    p_cal.add_argument("date_str", nargs="?", default="")
    p_cal.add_argument("--sync", action="store_true")

    # ledger & didi
    p_led = sub.add_parser("ledger", help="四卡钱账与发票报销包操作")
    l_sub = p_led.add_subparsers(dest="ledger_cmd")
    p_l_rep = l_sub.add_parser("report", help="输出诚实覆盖范围月报")
    p_l_rep.add_argument("--month", default="")
    p_l_zip = l_sub.add_parser("import-zip", help="在 $TMPDIR 阅后即焚导入微信/支付宝账单")
    p_l_zip.add_argument("path")
    p_l_zip.add_argument("--password", default="")
    p_l_reimb = l_sub.add_parser("reimburse", help="盘点当期持久归集目录发票 PDF 报销包")
    p_l_reimb.add_argument("--month", default="")
    p_l_cur = l_sub.add_parser("cursor", help="查看 Nous 报销游标（2805-0102 起）、未用还款与暂存发票")
    p_l_cur.add_argument("--json", action="store_true")
    p_l_aud = l_sub.add_parser("audit-batch", help="候选报销包双号(1xxx/2xxx)与付款时间(±6s)防重只读审计")
    p_l_aud.add_argument("target", help="候选收据目录、CSV/JSON 文件或逗号分隔收据号")
    p_l_aud.add_argument("--repayment-usd", type=float, default=None, help="目标核对购汇美元总额")
    p_l_aud.add_argument("--repayment-cny", type=float, default=None, help="目标核对购汇人民币总额")
    p_l_aud.add_argument("--json", action="store_true")

    p_l_pack = l_sub.add_parser("pack-draft", help="S3-2：显式选票草稿拼包器（生成 7 列发票明细.csv + 200-DPI PNG + 防重审计，不提前占游标）")
    p_l_pack.add_argument("targets", nargs="+", help="一个或多个候选 PDF 目录或文件")
    p_l_pack.add_argument("--label", default="", help="草稿包备注标签")
    p_l_pack.add_argument("--repayment-usd", type=float, default=None)
    p_l_pack.add_argument("--repayment-cny", type=float, default=None)
    p_l_pack.add_argument("--no-png", action="store_true", help="跳过 pdftoppm 200-DPI PNG 转换")
    p_l_pack.add_argument("--json", action="store_true")

    p_l_subm = l_sub.add_parser("mark-submitted", help="本人交单拿到正式报销单号后归档入 ~/Documents/报销记录/ 并推进游标")
    p_l_subm.add_argument("batch_no", help="如 EXP-WORK-202611180001 或 EXP-TRAVEL-...")
    p_l_subm.add_argument("--draft", default="latest", help="草稿目录或 ID（默认 latest）")

    p_l_ret = l_sub.add_parser("mark-returned", help="将 OA 退回批次标记为 RETURNED_REUSABLE 释放票据占用")
    p_l_ret.add_argument("batch_no")

    p_l_res = l_sub.add_parser("resolve", help="S3-3：对同额歧义或重复流水执行确定性消歧（note / pair / void-dup）")
    p_l_res.add_argument("txn_id", help="如 TX-20261117...")
    p_l_res.add_argument("--action", required=True, choices=["note", "pair", "void-dup"])
    p_l_res.add_argument("--value", default="", help="备注文本或配对目标 TX-ID")

    p_l_cw01 = l_sub.add_parser("pack-cw01", help="Batch B：一键将当月成对滴滴发票↔行程单打包为 CW01 草稿包")
    p_l_cw01.add_argument("--month", default="")
    p_l_cw01.add_argument("--include-dining", action="store_true", help="同时纳入当月餐饮电子发票")

    p_l_ask = l_sub.add_parser("ask", help="一句话 SQL 查账（如 '外卖 本月' / '滴滴 10月' / '山姆 上次'）")
    p_l_ask.add_argument("query", nargs="+")

    p_l_rcv = l_sub.add_parser("receivables", help="查看或登记报销/保价退差待到账追踪")
    p_l_rcv.add_argument("--add", nargs=2, metavar=("TITLE", "YUAN"), help="新增一笔待回款追踪")
    p_l_rcv.add_argument("--card", default="1004", help="预计入账卡尾号（默认 1004）")

    p_didi = sub.add_parser("didi", help="滴滴官方 MCP 实时询价与代叫车（配 Key 后生成 apply Axx 确认发单，未配回退唤端链接）")
    p_didi.add_argument("places", nargs="*", help="传 <终点>（优先取手机定位/Wi-Fi，否则默认「家」）或 <起点> <终点>")
    p_didi.add_argument("--from", dest="from_alias", default="", help="起点名称或预设别名（留空自动取手机实时定位或「家」）")
    p_didi.add_argument("--set", nargs=2, metavar=("ALIAS", "ADDRESS"), help="保存常用地址别名（如 --set 公司 示例写字楼）")
    p_didi.add_argument("--list-places", action="store_true", help="列出当前常用地址预设")
    p_didi.add_argument("--set-key", default="", help="保存滴滴官方 MCP Key（https://mcp.didichuxing.com）")
    p_didi.add_argument("--sandbox", action="store_true", help="使用滴滴 MCP 沙箱调试环境（不产生真实订单）")
    p_didi.add_argument("--status", action="store_true", help="查询当前活跃滴滴订单状态")
    p_didi.add_argument("--cancel", action="store_true", help="取消当前活跃滴滴订单")

    p_mt = sub.add_parser("meituan", help="美团外卖当前位置实时检索推荐、投机菜品推演与本机 9222 H5 加购锁单")
    p_mt.add_argument("hint", nargs="*", help="可选偏好提示（如 晚餐 / 川菜 / 预算40）")
    p_mt.add_argument("--loc", default="", help="可选覆盖配送地点（如 公司 / 家 / 新房；默认自动同步手机实时定位）")
    p_mt.add_argument("--order", default="", help="直接搜店/选品生成付款许可确认卡（如 --order '素坤泰 打抛饭' 或 '第1家'）")
    p_mt.add_argument("--history", action="store_true", help="列出最近买过的 5 份美团历史整单（支持 QQ 原生按钮一键复购）")
    p_mt.add_argument("--reorder", default="", help="按历史整单原样加购并生成付款许可确认卡（如 --reorder '南京大牌档' 或 '复购第1单'）")
    p_mt.add_argument("--add", action="append", default=[], help="配合 --reorder 额外加菜（可重复或用逗号/+分隔，如 --add '芫荽那个凉菜'）")
    p_mt.add_argument("--exclude", action="append", default=[], help="配合 --reorder 剔除旧单中的指定菜品（如 --exclude '米饭'）")
    p_mt.add_argument("--plans", action="store_true", help="查看当前推荐批次的后台并行投机菜品推演方案")
    p_mt.add_argument("--confirm", default="", help="直接确认执行投机推演预操作（如 --confirm 第1家 或店名）")
    p_mt.add_argument("--speculate-batch", default="", help="内部/手动：对指定推荐批次并行拉取菜单并推演菜品预操作")
    p_mt.add_argument("--no-speculate", action="store_true", help="本次推荐不自动派生后台投机推演进程")

    p_lk = sub.add_parser("luckin", help="瑞幸咖啡官方 MCP 一句话真点单与状态查询")
    p_lk.add_argument("spec", nargs="*", help="点餐规格（如 大杯冰美式不加糖 / 再来一单）")
    p_lk.add_argument("--set-key", nargs="?", const="__PROMPT__", default="", help="安全保存瑞幸官方 MCP Token（支持不带参交互式输入防 ps 泄露）")
    p_lk.add_argument("--status", action="store_true", help="查询当前瑞幸订单制作状态与取餐码")

    p_ops = sub.add_parser("ops-check", help="AI 研发链路唯一根因合成诊断、Nous 付款锁巡检与白名单自愈")
    p_ops.add_argument("--no-heal", action="store_true", help="仅只读诊断，不自动刷新 sing-box DNS 缓存")
    p_ops.add_argument("--nous", action="store_true", help="通过 9222 后台标签顺带只读核验 Nous Portal 余额与 AUTO REFILL 开关")
    p_ops.add_argument("--json", action="store_true")

    p_resm = sub.add_parser("resume", help="生成指定项目的跨会话「十秒续工卡」（Git + Handoff + 最近会话恢复命令）")
    p_resm.add_argument("project", nargs="?", default="", help="项目名或别名（如 siming / my-app；留空自动选最近活跃项目）")

    p_att = sub.add_parser("attention", help="查看或切换 Night-Owl 动态作息注意力状态（睡了 / 醒了）")
    p_att.add_argument("mode", nargs="?", choices=["status", "sleep", "awake"], default="status")

    p_loc = sub.add_parser("loc", help="查看或配置手机 Wi-Fi / 空间定位映射（供滴滴起点与美团/瑞幸自动选址）")
    p_loc.add_argument("--map-wifi", nargs=2, metavar=("SSID", "PLACE_ALIAS"), help="绑定 Wi-Fi 名称至地点别名（如 --map-wifi Office-WiFi 公司）")

    p_auth = sub.add_parser("auth-login", help="本人授权手机号与平台短信验证码无感代登（美团 / 滴滴 / 瑞幸 / 京东）")
    p_auth.add_argument("platform", nargs="?", default="", help="目标平台（meituan / didi / luckin / jd）")
    p_auth.add_argument("--set-phone", nargs="?", const="__PROMPT__", default="", help="加密登记本人授权 11 位手机号（0600）")
    p_auth.add_argument("--wait-sec", type=float, default=60.0)

    p_mv = sub.add_parser("movein", help="Batch B：新房入住送装冲突检查、隐蔽工程照片归档与家电售后护照")
    p_mv.add_argument("view", nargs="?", choices=["conflicts", "passport", "photos"], default="conflicts")
    p_mv.add_argument("keyword", nargs="?", default="")
    p_mv.add_argument("--gate-ok", default="", help="标记前置工序就绪（燃气 / 橱柜 / 吊顶 / 上下水 / 挂架）")
    p_mv.add_argument("--photo", default="", help="归档指定照片路径")
    p_mv.add_argument("--room", default="主卧", help="归档照片所属房间")
    p_mv.add_argument("--tag", default="水电留证", help="归档照片标签")

    p_rpl = sub.add_parser("replenish", help="日用品周期推算、到期提醒与美团/京东一键补货")
    rpl_sub = p_rpl.add_subparsers(dest="rpl_cmd")
    rpl_sub.add_parser("ls", help="查看日用品周期清单与待补货倒计时")
    p_rpl_seed = rpl_sub.add_parser("seed", help="根据历史消费记录初始化/重算日用品补货周期")
    p_rpl_seed.add_argument("--overwrite", action="store_true", help="覆盖已有条目的周期与推算起点")
    p_rpl_chk = rpl_sub.add_parser("check", help="检查到期日用品（默认预览，加 --send 推 QQ 确认卡）")
    p_rpl_chk.add_argument("--send", action="store_true", help="真正向 QQ 推送到期交互确认卡")
    p_rpl_buy = rpl_sub.add_parser("buy", help="按商品配置渠道（美团直买 / 京东加车）发起补货确认卡")
    p_rpl_buy.add_argument("target")
    p_rpl_buy.add_argument("--qty", type=int, default=None)
    p_rpl_bought = rpl_sub.add_parser("bought", help="登记日用品已购买并推进下个周期")
    p_rpl_bought.add_argument("target")
    p_rpl_bought.add_argument("--date", default="", help="购买日期 YYYY-MM-DD（默认今天）")
    p_rpl_snz = rpl_sub.add_parser("snooze", help="家里还有余量，延后提醒天数")
    p_rpl_snz.add_argument("target")
    p_rpl_snz.add_argument("--days", type=int, default=14)
    p_rpl_add = rpl_sub.add_parser("add", help="新增或更新常买品")
    p_rpl_add.add_argument("name")
    p_rpl_add.add_argument("sku_or_url", nargs="?", default="")
    p_rpl_add.add_argument("--days", type=int, default=30)
    p_rpl_add.add_argument("--target-yuan", type=float, default=None)
    p_rpl_add.add_argument("--qty", type=int, default=1)
    p_rpl_add.add_argument("--channel", default="", choices=["", "meituan", "jd", "taobao", "sams", "hema"])
    p_rpl_add.add_argument("--location", default="家", choices=["家", "公司"])
    p_rpl_add.add_argument("--shop", default="", help="美团优先商超名")
    p_rpl_add.add_argument("--query", default="", help="美团搜索词")
    p_rpl_add.add_argument("--spec", default="", help="规格说明")
    p_rpl_add.add_argument("--last-bought", default="", help="上次购买日期 YYYY-MM-DD")
    p_rpl_cart = rpl_sub.add_parser("cart", help="生成京东跨端加购物车 15m apply 确认卡")
    p_rpl_cart.add_argument("target")
    p_rpl_cart.add_argument("--qty", type=int, default=1)

    # memory diff
    p_care = sub.add_parser("care", help="日常照护：出门带伞/穿衣、周期用药/打针与开药、常备耗材补货雷达")
    care_sub = p_care.add_subparsers(dest="care_cmd")
    care_sub.add_parser("status", help="查看照护面板（通勤、针数、耗材余量）")
    p_care_w = care_sub.add_parser("weather", help="立即预览今天的出门天气卡（默认不推送）")
    p_care_w.add_argument("--send", action="store_true", help="真的推送到 QQ")
    p_care_h = care_sub.add_parser("harvest", help="立即从美团外卖订单页同步商品明细")
    p_care_h.add_argument("--days", type=int, default=0, help="回溯天数（0=增量）")
    care_sub.add_parser("inject", help="登记本周这针已打")
    care_sub.add_parser("skip", help="本周这针跳过")
    p_care_pen = care_sub.add_parser("pen", help="登记周期注射药笔剩余针数")
    p_care_pen.add_argument("left", type=int)
    p_care_rf = care_sub.add_parser("refill", help="登记开药（支数）")
    p_care_rf.add_argument("pens", type=int, nargs="?", default=1)
    p_care_st = care_sub.add_parser("stock", help="校准耗材余量：mask / needle / alcohol_pad")
    p_care_st.add_argument("item", choices=["mask", "needle", "alcohol_pad"])
    p_care_st.add_argument("count", type=int)

    p_mem = sub.add_parser("memory", help="查看共享记忆近期变更")
    m_sub = p_mem.add_subparsers(dest="mem_cmd")
    p_m_diff = m_sub.add_parser("diff", help="对比近 N 天 60-共享记忆 改动")
    p_m_diff.add_argument("--days", type=int, default=7)

    # cron-ingest & inbound (for Hermes integration)
    p_cron = sub.add_parser("cron-ingest", help="接收外部 cron 结构化输出写入 events（不直推 QQ）")
    p_cron.add_argument("--job", required=True)
    p_cron.add_argument("--text", default="")
    p_cron.add_argument("--notify-on-change", action="store_true")

    p_push = sub.add_parser("push", help="外部脚本统一推送入口：经司命 router 发 QQ（替代脚本里直接 hermes send）")
    p_push.add_argument("--rule", required=True, help="规则名（自动加 ext_ 前缀登记进 rule_stats，可被 feedback 降级/冻结）")
    p_push.add_argument("--ref", default="", help="去重/引用键，如 x:handle:topic；默认 push:<rule>")
    p_push.add_argument("--level", default="normal", help="now=全天穿透 / normal=按作息暂扣 / digest=只进晨札")
    p_push.add_argument("--dedup", default="24h", help="同 ref+正文去重窗口，如 2m / 1h / 24h，0 关闭")
    p_push.add_argument("--hard", action="store_true", help="标记为硬规则：不会被自动降级/冻结（如备份失败）")
    p_push.add_argument("--keyboard-json", default="", help="可选 QQ InlineKeyboard JSON 字符串（确认框/选择框按钮）")
    p_push.add_argument("text", nargs="?", default="-", help="正文；省略或 - 时从 stdin 读取")

    p_inb = sub.add_parser("inbound", help="内部网关桥接：处理 QQ 入站消息并输出 JSON")
    p_inb.add_argument("--stdin-json", action="store_true", help="从 stdin 读取 JSON 入站载荷（防 ps argv 泄露与破折号吞参）")
    p_inb.add_argument("--text", default="")
    p_inb.add_argument("--reply-to-id", default="")
    p_inb.add_argument("--reply-to-text", default="")
    p_inb.add_argument("--media", action="append", default=[])

    p_act = sub.add_parser("inbound-action", help="内部网关桥接：确定性处理 QQ 内嵌键盘 siming:* 按钮回调并输出 JSON")
    p_act.add_argument("--stdin-json", action="store_true", help="从 stdin 读取 JSON 载荷（{\"data\": \"siming:...\"}）")
    p_act.add_argument("--data", default="", help="按钮回调 data（siming:*）")

    # daemon/timer entrypoints
    p_tick = sub.add_parser("tick", help="执行一轮采集、挂起扫描、事件推进与动作消费")
    p_tick.add_argument("--no-collectors", action="store_true")
    p_ew = sub.add_parser("run-effect-worker", help="内部瞬态单元入口：独立执行单条 launch_worker 动作")
    p_ew.add_argument("effect_id")
    p_ew.add_argument("--expected-gen", type=int, default=0, help="期望 attempt_gen 代次（不匹配即拒绝执行）")
    sub.add_parser("watchdog", help="执行一轮纯脚本看门狗巡检")
    sub.add_parser("edge-sync", help="与公网边缘节点同步心跳、收信队列与预排发件箱")
    p_msync = sub.add_parser("mail-sync", help="立即经 Omamail 只读同步已连接邮箱（163 / Yahoo / Gmail）")
    p_msync.add_argument("--limit", type=int, default=30)
    sub.add_parser("gmail-reauth", help="启动 15m 本地回调监听并输出一键重登 Gmail 授权链接（同步更新 Omamail 与 Hermes）")
    sub.add_parser("backup", help="执行 SQLite 在线备份与恢复完整性演练")

    p_doc = sub.add_parser("doctor", help="一键全链路体检")
    p_doc.add_argument("--check-mail", action="store_true", help="立即实测一次 163 IMAP 只读登录")
    p_doc.add_argument("--no-edge", action="store_true")
    p_doc.add_argument("--json", action="store_true")

    p_rep = sub.add_parser("replay", help="在沙盒内回放验证关键防呆场景或近 N 天事件链降噪模拟")
    p_rep.add_argument(
        "--fixture",
        default="all",
        choices=["all", "restart-burst", "mandate-guard", "ambiguous-ledger", "makeup-calendar", "event-chain"],
    )
    p_rep.add_argument("--days", type=int, default=0, help="按真实库近 N 天历史回放事件链降噪与级联模拟（如 --days 30）")
    p_rep.add_argument("--dry-run", action="store_true")

    p_tesla = sub.add_parser("tesla", help="查看特斯拉 Model Y 标续车况（截图/行车估算 + 中国区官方 Fleet API）或评估续航")
    p_tesla.add_argument("--trip", type=float, default=0.0, help="评估单程距离 km 的往返 1.35x 安全电量")
    p_tesla.add_argument("--dest", default="", help="出行目的地（联动高德实时路况与超充检索）")
    p_tesla.add_argument("--arrive-by", default="", help="要求到达时间 HH:MM（如 14:30）")
    p_tesla.add_argument("--set-fleet-token", default="", help="保存特斯拉中国区 Fleet API OAuth Token (0600)")
    p_tesla.add_argument("--auth", nargs=3, metavar=("CLIENT_ID", "CLIENT_SECRET", "DOMAIN"), help="一键注册 developer.tesla.cn Partner 域名并完成 PKCE 授权（自动续期）")

    p_rout = sub.add_parser("routines", help="挖掘并查看司命从历史数据归纳的可改写规律白盒（routines.md）与可观测能力目录")
    p_rout.add_argument("--catalog", action="store_true", help="同时输出 L1 可观测能力目录")

    p_bm = sub.add_parser("bookmarks", help="查看 DeepSeek Super-agent 分拣沉淀的技术灵感收藏夹或触发沙盒试跑")
    p_bm.add_argument("--sandbox", default="", help="在隔离目录试跑指定收藏（如 B01）")
    p_bm.add_argument("--category", default="", help="按分类筛选")

    p_prop = sub.add_parser("propose-agent-rules", help="生成合入 AGENTS.md / SOUL.md 的 A2 审批口令（30m 有效）")
    p_prop.add_argument("--target", choices=["agents", "soul", "both"], default="both")

    from siming.cli_v5 import register_v5_subcommands

    register_v5_subcommands(sub)

    from siming.cli_registry import register_v6_subcommands

    register_v6_subcommands(sub)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.cmd:
        parser.print_help()
        return 0

    db = SimingDB()
    db.init_db()

    if args.cmd == "defer":
        if args.defer_cmd == "add":
            w = add_wait(
                db=db,
                when=args.when,
                mode=args.mode,
                strength="explicit",
                quote=args.quote or args.task,
                task=args.task,
                context=args.context,
                cwd=args.cwd,
                agent=args.agent,
                session_id=args.session,
                runner=args.runner,
                done_check=args.done_check,
                every=args.every,
                ttl=args.ttl,
            )
            consume_pending_effects(db)
            when_disp = w["next_fire_at"] or f"{w['trigger_kind']}:{w['trigger_expr']}"
            print(
                f"[{w['wait_id']}] 已挂起（{w['trigger_kind']} -> {when_disp}，有效期至 {w['expires_at'][:10]}，{w['action_mode']} 模式）"
            )
            return 0
        if args.defer_cmd == "list":
            rows = list_waits(db, status=None if args.all else "WAITING")
            if not rows:
                print("当前无匹配的挂起项。")
                return 0
            for r in rows:
                when_d = r["next_fire_at"] or r["trigger_expr"]
                print(
                    f"[{r['wait_id']}] {r['status']:<9} {r['action_mode']:<6} ({r['strength']}) {r['trigger_kind']}:{when_d} | {r['quote'][:60]}"
                )
            return 0
        if args.defer_cmd == "fire":
            ok = trigger_wait_now(db, args.wait_id, "命令行手动触发")
            if not ok:
                print(f"未找到可触发的 WAITING 挂起项 {args.wait_id}", file=sys.stderr)
                return 1
            consume_pending_effects(db)
            consume_pending_effects(db)
            print(f"[{args.wait_id.upper()}] 已触发并执行。")
            return 0
        if args.defer_cmd == "cancel":
            ok = cancel_wait(db, args.wait_id, "命令行取消")
            consume_pending_effects(db)
            print(f"[{args.wait_id.upper()}] {'已取消' if ok else '未找到或已结束'}。")
            return 0 if ok else 1
        parser.parse_args(["defer", "--help"])
        return 0

    if args.cmd == "ask":
        res = ask_knowledge(db, args.query, use_llm_synthesis=args.llm)
        print(res["answer"])
        return 0

    if args.cmd == "dispatch":
        res = dispatch_repo_task(
            db,
            repo=args.repo,
            task=args.task,
            done_check=args.done_check,
            runner=args.runner,
            run_immediately=True,
        )
        if not res["ok"]:
            print(res["error"], file=sys.stderr)
            return 1
        consume_pending_effects(db)
        consume_pending_effects(db)
        print(f"[{res['wait_id']}] 已在隔离 worktree 完成派单执行（验收：{res['done_check']}）。")
        return 0

    if args.cmd == "drop":
        fp = Path(args.file).expanduser() if args.file else None
        res = drop_item(
            db,
            text=args.text,
            file_path=fp,
            latest_shot=args.latest_shot,
            from_paste=args.paste,
            note=args.note,
        )
        if not res["ok"]:
            print(res["error"], file=sys.stderr)
            return 1
        process_new_events(db)
        consume_pending_effects(db)
        print(res["summary"])
        return 0

    if args.cmd == "today":
        brief = build_morning_brief(db, full=bool(args.full))
        print(brief)
        if args.send:
            s_res = send_qq_message(db, brief, ref_id=f"manual:{now_local().date().isoformat()}", rule_id="morning_brief", bypass_quiet=True)
            print(f"\n[发送状态] ok={s_res.get('ok')} channel={s_res.get('channel', '-')}")
        return 0

    if args.cmd == "install-check":
        if args.mark_arrival:
            m_res = mark_arrival_inspected(db, target_query=args.mark_arrival)
            if not m_res.get("ok"):
                print(m_res.get("error"), file=sys.stderr)
                return 1
            print(m_res["summary"])
            return 0
        db.record_human_use("install_plan", "cli_install_check", ref_id="12.10")
        if args.json:
            print(json.dumps(build_installation_projection(db), ensure_ascii=False, indent=2))
        elif args.arrival:
            print(format_arrival_inspection_checklist(db))
        elif args.onsite:
            print(format_onsite_grouped_checklist(db))
        else:
            print(format_installation_checklist(db, compact=False))
        return 0

    if args.cmd == "price-watch":
        if args.pw_cmd in (None, "list"):
            db.record_human_use("price_protect_watch", "cli_list", ref_id="list")
            if getattr(args, "json", False):
                print(json.dumps(list_price_watches(db), ensure_ascii=False, indent=2))
            else:
                print(format_price_watch_report(db))
            return 0
        if args.pw_cmd == "check":
            res = collect_price_watch(db)
            consume_pending_effects(db, include_workers=False)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if args.pw_cmd == "add":
            res = add_price_watch(
                db,
                sku_or_url=args.sku_or_url,
                name=args.name,
                paid_yuan=args.paid,
                order_id=args.order,
                buy_date=args.date,
                pp_days=args.days,
            )
            if not res.get("ok"):
                print(res.get("error"), file=sys.stderr)
                return 1
            print(res["summary"])
            return 0
        if args.pw_cmd == "remove":
            res = remove_price_watch(db, args.target, mute_only=bool(args.mute_only))
            if not res.get("ok"):
                print(res.get("error"), file=sys.stderr)
                return 1
            print(res["summary"])
            return 0
        return 0

    if args.cmd == "module":
        if args.mod_cmd in (None, "ls"):
            if getattr(args, "json", False):
                print(json.dumps(list_modules_status(db), ensure_ascii=False, indent=2))
            else:
                print(format_modules_report(db))
            return 0
        if args.mod_cmd in ("on", "off"):
            res = set_module_enabled(db, args.name, enabled=(args.mod_cmd == "on"))
            if not res.get("ok"):
                print(res.get("error"), file=sys.stderr)
                return 1
            print(res["summary"])
            return 0
        return 0

    if args.cmd == "remote-hook":
        res = render_remote_srr_hook(
            db,
            host_label=args.host,
            install_local_bin=bool(args.install_local),
            deploy_ssh_host=args.deploy,
        )
        print(res["snippet"])
        if res.get("local_srr_path"):
            print(f"\n# [已安装本机可执行包装器] {res['local_srr_path']}", file=sys.stderr)
        if args.deploy:
            if res.get("deployed_remote"):
                print(f"# [已安全下发受限 receipt-token 至 {args.deploy}:~/.config/siming/receipt-token (0600)]", file=sys.stderr)
            else:
                print(f"# [下发受限令牌失败] {res.get('deploy_error')}", file=sys.stderr)
                return 1
        return 0

    if args.cmd == "run":
        cmd_list = list(args.cmd_argv or [])
        if cmd_list and cmd_list[0] == "--":
            cmd_list = cmd_list[1:]
        if not cmd_list:
            print("错误：请指定要包装运行的命令，例如 `siming run -- pytest -q`", file=sys.stderr)
            return 2
        r_res = run_wrapped_command(
            db,
            cmd_argv=cmd_list,
            name=args.name,
            cwd=args.cwd,
            notify_qq=not args.no_qq,
            respect_quiet=bool(args.respect_quiet),
        )
        return int(r_res.get("exit_code") or 0)

    if args.cmd == "watch":
        w_res = watch_herdr_pane(db, target=args.target, label=args.label)
        if not w_res.get("ok"):
            print(w_res.get("error") or "登记监听失败", file=sys.stderr)
            return 1
        print(
            f"[{w_res['run_id']}] 已绑定 herdr 面板 `{w_res['pane_id']}`（{w_res['name']}，当前={w_res['agent_status']}，seq={w_res['baseline_seq']}）；本轮完成或阻塞时将单次通知手机 QQ。"
        )
        return 0

    if args.cmd == "cases":
        conn = db.connect()
        try:
            if args.all:
                rows = conn.execute("SELECT * FROM cases ORDER BY updated_at DESC LIMIT 40").fetchall()
            else:
                rows = conn.execute("SELECT * FROM cases WHERE status IN ('待处理', '待核实') ORDER BY updated_at DESC").fetchall()
        finally:
            conn.close()
        if not rows:
            print("当前无未结事项。")
            return 0
        for c in rows:
            due = f" (截止 {c['due_at'][:10]})" if c["due_at"] else ""
            print(f"[{c['case_id']}] {c['status']} · {c['title']}{due} — {c['next_step']}")
        return 0

    if args.cmd == "done":
        from siming.router import close_case

        res_d = close_case(db, args.case_id, cancel=bool(args.cancel), note=args.note)
        if not res_d.get("ok"):
            print(res_d.get("error") or "销账失败", file=sys.stderr)
            return 1
        print(res_d["summary"])
        return 0

    if args.cmd == "apply":
        res = verify_and_apply_approval(db, args.token)
        if not res["ok"]:
            print(res["error"], file=sys.stderr)
            return 1
        print(f"[{res['token']}] {res['summary']}")
        return 0

    if args.cmd == "feedback":
        fb = record_feedback(db, args.target_id, args.action, note=args.note)
        print(json.dumps(fb, ensure_ascii=False))
        return 0

    if args.cmd == "explain":
        print(explain_item(db, args.target_id))
        return 0

    if args.cmd == "forget":
        print(forget_memory(db, args.keyword))
        return 0

    if args.cmd == "pause":
        if args.duration.lower() in ("off", "0", "resume", "none"):
            db.set_meta("paused_until", "")
            print("已恢复后台自动执行。")
            return 0
        sec = parse_duration_seconds(args.duration)
        until = iso_now(now_local() + timedelta(seconds=sec))
        db.set_meta("paused_until", until)
        print(f"已暂停后台自动 worker 执行至 {until}（提醒仍正常发出）。")
        return 0

    if args.cmd == "ziwei":
        from siming.ziwei_ref import DEFAULT_REPO, natal_reply, sync_refpack

        if args.zw_cmd == "sync":
            try:
                res = sync_refpack(db, repo=Path(args.repo) if args.repo else DEFAULT_REPO)
            except Exception as exc:
                print(f"同步失败：{exc}", file=sys.stderr)
                return 1
            print(json.dumps(res, ensure_ascii=False, indent=1))
            return 0
        print(natal_reply(db, getattr(args, "dim", "") or "", full=bool(getattr(args, "full", False))))
        return 0

    if args.cmd == "calendar":
        if args.sync:
            s_res = sync_holiday_cn(paths=db.paths)
            print(json.dumps(s_res, ensure_ascii=False))
            return 0
        from datetime import date

        d = date.fromisoformat(args.date_str) if args.date_str else now_local().date()
        info = day_info(d, db.paths)
        print(info.brief_header)
        return 0

    if args.cmd == "ledger":
        if args.ledger_cmd == "report":
            ym = args.month or now_local().strftime("%Y-%m")
            print(generate_monthly_ledger_report(db, ym))
            return 0
        if args.ledger_cmd == "import-zip":
            pwd = args.password or None
            res = import_bill_archive(db, Path(args.path).expanduser(), password=pwd)
            if not res["ok"]:
                print(res["error"], file=sys.stderr)
                return 1
            print(f"导入完成：共解析 {res['ingested']} 笔，已自动对账 {res['matched']} 笔，同额歧义标待核实 {res['ambiguous']} 笔。")
            return 0
        if args.ledger_cmd == "reimburse":
            rep = summarize_reimbursement_bundle(db, args.month)
            print(json.dumps(rep, ensure_ascii=False, indent=2))
            return 0
        if args.ledger_cmd == "cursor":
            db.record_human_use("reimburse_audit", "cli_cursor", ref_id="cursor")
            if args.json:
                print(json.dumps(get_reimbursement_cursor_status(db), ensure_ascii=False, indent=2))
            else:
                print(format_reimbursement_cursor(db))
            return 0
        if args.ledger_cmd == "audit-batch":
            aud = audit_candidate_batch(
                db,
                target=args.target,
                repayment_usd=args.repayment_usd,
                repayment_cny=args.repayment_cny,
            )
            if args.json:
                print(json.dumps(aud, ensure_ascii=False, indent=2))
            else:
                print(format_audit_batch_report(aud))
            return 0 if aud.get("ok") else 1
        if args.ledger_cmd == "pack-draft":
            pk = pack_reimbursement_draft(
                db,
                targets=args.targets,
                label=args.label,
                repayment_usd=args.repayment_usd,
                repayment_cny=args.repayment_cny,
                render_png=not args.no_png,
            )
            if args.json:
                print(json.dumps(pk, ensure_ascii=False, indent=2))
            else:
                print(format_pack_draft_report(pk))
            return 0 if pk.get("ok") else 1
        if args.ledger_cmd == "mark-submitted":
            ms = mark_batch_submitted(db, batch_no=args.batch_no, draft_ref=args.draft)
            if not ms.get("ok"):
                print(ms.get("error"), file=sys.stderr)
                return 1
            print(ms["summary"])
            return 0
        if args.ledger_cmd == "mark-returned":
            mr = mark_batch_returned(db, batch_no=args.batch_no)
            if not mr.get("ok"):
                print(mr.get("error"), file=sys.stderr)
                return 1
            print(mr["summary"])
            return 0
        if args.ledger_cmd == "resolve":
            rs = resolve_ledger_txn(db, txn_id=args.txn_id, action=args.action, value=args.value)
            if not rs.get("ok"):
                print(rs.get("error"), file=sys.stderr)
                return 1
            print(rs["summary"])
            return 0
        if args.ledger_cmd == "pack-cw01":
            from siming.quiet_cfo import pack_cw01_domestic_bundle

            r_cw = pack_cw01_domestic_bundle(db, year_month=args.month, include_dining=bool(args.include_dining))
            if not r_cw.get("ok"):
                print(r_cw.get("error"), file=sys.stderr)
                return 1
            print(r_cw["summary"])
            return 0
        if args.ledger_cmd == "ask":
            from siming.quiet_cfo import query_ledger_natural

            print(query_ledger_natural(db, " ".join(args.query))["reply"])
            return 0
        if args.ledger_cmd == "receivables":
            from siming.quiet_cfo import format_receivables_report, register_receivable

            if args.add:
                rv = register_receivable(db, ref_id="", title=args.add[0], expected_yuan=args.add[1], card_hint=args.card)
                print(rv.get("summary") or rv.get("error", ""))
                return 0 if rv.get("ok") else 1
            print(format_receivables_report(db))
            return 0
        parser.parse_args(["ledger", "--help"])
        return 0

    if args.cmd == "didi":
        from siming.didi_mcp import query_or_cancel_didi_order, request_didi_ride, save_didi_mcp_key

        if args.set_key:
            sk = save_didi_mcp_key(db, args.set_key)
            if not sk.get("ok"):
                print(sk.get("error"), file=sys.stderr)
                return 1
            print(sk["summary"])
            return 0
        if args.status or args.cancel:
            qr = query_or_cancel_didi_order(db, cancel=bool(args.cancel))
            print(qr["reply"])
            return 0 if qr.get("ok") else 1
        if args.set:
            places = set_saved_place(db, args.set[0], args.set[1])
            print(json.dumps(places, ensure_ascii=False, indent=2))
            return 0
        if args.list_places:
            print(json.dumps(load_saved_places(db), ensure_ascii=False, indent=2))
            return 0
        pts = list(args.places or [])
        if len(pts) == 1:
            rr = request_didi_ride(db, dest_raw=pts[0], origin_raw=args.from_alias, sandbox=bool(args.sandbox))
            print(rr["reply"])
            return 0
        if len(pts) >= 2:
            rr = request_didi_ride(db, dest_raw=pts[1], origin_raw=pts[0], sandbox=bool(args.sandbox))
            print(rr["reply"])
            return 0
        parser.parse_args(["didi", "--help"])
        return 0

    if args.cmd == "meituan":
        import re as _re
        from siming.meituan_agent import (
            list_meituan_history_orders,
            recommend_meituan_meals,
            stage_meituan_h5_order,
            stage_meituan_history_reorder,
        )
        from siming.meituan_speculate import (
            confirm_and_execute_speculative_pick,
            format_speculative_plans_overview,
            run_speculative_meal_preplan,
        )

        if args.speculate_batch:
            sp = run_speculative_meal_preplan(db, batch_id=args.speculate_batch)
            print(json.dumps(sp, ensure_ascii=False))
            return 0 if sp.get("ok") else 1
        if args.history:
            hl = list_meituan_history_orders(db, location_override=args.loc)
            print(hl["reply"])
            return 0 if hl.get("ok") else 1
        if args.reorder:
            extra_items: list[dict[str, Any]] = []
            for raw_add in args.add or []:
                for tok in _re.split(r"[,，、+＋]+", str(raw_add or "")):
                    t_clean = tok.strip()
                    if t_clean:
                        extra_items.append({"name": t_clean, "qty": 1})
            exc_items: list[str] = []
            for raw_exc in args.exclude or []:
                for tok in _re.split(r"[,，、+＋]+", str(raw_exc or "")):
                    t_clean = tok.strip()
                    if t_clean:
                        exc_items.append(t_clean)
            ro = stage_meituan_history_reorder(
                db,
                args.reorder,
                location_override=args.loc,
                extra_dishes=extra_items or None,
                exclude_dishes=exc_items or None,
            )
            print(ro["reply"])
            return 0 if ro.get("ok") else 1
        if args.plans:
            print(format_speculative_plans_overview(db))
            return 0
        if args.confirm:
            cf = confirm_and_execute_speculative_pick(db, args.confirm, location_override=args.loc)
            print(cf["reply"])
            return 0 if cf.get("ok") else 1
        if args.order:
            st = stage_meituan_h5_order(db, args.order, location_override=args.loc)
            print(st["reply"])
            return 0 if st.get("ok") else 1
        hint_str = " ".join(args.hint or []).strip()
        if hint_str in ("看看方案", "看方案", "推演方案", "推演结果", "推演好了吗", "外卖方案", "这几家点啥", "点啥菜", "plans"):
            print(format_speculative_plans_overview(db))
            return 0
        m_cf = _re.match(
            r"^(?:确认|就点|就吃|直接点|确认点|按方案点)\s*(第?\s*[1-5一二三四五]\s*(?:家|个|号)?|[^\s,，、。！？]{2,20})$",
            hint_str,
        )
        if m_cf:
            cf = confirm_and_execute_speculative_pick(db, m_cf.group(1), location_override=args.loc)
            print(cf["reply"])
            return 0 if cf.get("ok") else 1
        m_idx_ord = _re.match(r"^(?:看看|选|点|要)?(第\s*[1-5一二三四五]\s*(?:家|个|号)(?:[\s,，、:：]+.+)?)$", hint_str)
        if m_idx_ord:
            st = stage_meituan_h5_order(db, m_idx_ord.group(1), location_override=args.loc)
            print(st["reply"])
            return 0 if st.get("ok") else 1
        rec = recommend_meituan_meals(
            db,
            hint=hint_str,
            location_override=args.loc,
            speculate=not bool(args.no_speculate),
        )
        print(rec["reply"])
        return 0 if rec.get("ok") else 1

    if args.cmd == "luckin":
        import getpass
        from siming.luckin_mcp import query_luckin_order_status, save_luckin_mcp_token, stage_luckin_order

        if args.set_key:
            tok_val = args.set_key
            if tok_val == "__PROMPT__":
                tok_val = getpass.getpass("请粘贴瑞幸官方 MCP Token（输入不回显）：") if sys.stdin.isatty() else sys.stdin.read().strip()
            sk = save_luckin_mcp_token(db, tok_val)
            if not sk.get("ok"):
                print(sk.get("error"), file=sys.stderr)
                return 1
            print(sk["summary"])
            return 0
        if args.status:
            qr = query_luckin_order_status(db)
            print(qr["reply"])
            return 0 if qr.get("ok") else 1
        spec_s = " ".join(args.spec or []).strip()
        res_lk = stage_luckin_order(db, spec_s or "再来一单")
        print(res_lk["reply"])
        return 0 if res_lk.get("ok") else 1

    if args.cmd == "ops-check":
        from siming.self_heal import diagnose_and_heal_dev_links

        diag = diagnose_and_heal_dev_links(db, allow_heal=not args.no_heal, probe_nous_portal=bool(args.nous))
        if args.json:
            print(json.dumps(diag, ensure_ascii=False, indent=2))
        else:
            print(diag["reply"])
        return 0

    if args.cmd == "resume":
        from siming.dev_twin import build_ten_second_resume_card

        rcard = build_ten_second_resume_card(db, project_query=args.project)
        print(rcard["reply"])
        return 0 if rcard.get("ok") else 1

    if args.cmd == "attention":
        from siming.attention import format_attention_status, record_explicit_sleep_state

        if args.mode == "sleep":
            print(record_explicit_sleep_state(db, sleeping=True, source="cli")["summary"])
        elif args.mode == "awake":
            print(record_explicit_sleep_state(db, sleeping=False, source="cli")["summary"])
        else:
            print(format_attention_status(db))
        return 0

    if args.cmd == "loc":
        from siming.context_loc import bind_wifi_to_place, format_location_status

        if args.map_wifi:
            res_w = bind_wifi_to_place(db, args.map_wifi[0], args.map_wifi[1])
            print(res_w.get("summary") or res_w.get("error", ""))
            return 0 if res_w.get("ok") else 1
        print(format_location_status(db))
        return 0

    if args.cmd == "auth-login":
        import getpass
        from siming.otp_vault import run_authorized_sms_login, save_authorized_phone

        if args.set_phone:
            ph = args.set_phone
            if ph == "__PROMPT__":
                ph = getpass.getpass("请输入本人授权登录的 11 位手机号（输入不回显）：") if sys.stdin.isatty() else sys.stdin.read().strip()
            sp = save_authorized_phone(db, ph)
            print(sp.get("summary") or sp.get("error", ""))
            return 0 if sp.get("ok") else 1
        if not args.platform:
            parser.parse_args(["auth-login", "--help"])
            return 0
        lr = run_authorized_sms_login(db, args.platform, wait_sec_otp=args.wait_sec)
        print(lr["reply"])
        return 0 if lr.get("ok") else 1

    if args.cmd == "movein":
        from siming.movein_pm import (
            archive_home_photo,
            check_install_conflicts,
            list_home_photos,
            query_appliance_passport,
            set_prereq_gate,
        )

        if args.gate_ok:
            gr = set_prereq_gate(db, args.gate_ok, ready=True)
            print(gr.get("summary") or gr.get("error", ""))
            return 0 if gr.get("ok") else 1
        if args.photo:
            pr = archive_home_photo(db, Path(args.photo).expanduser(), room=args.room, tag=args.tag)
            print(pr.get("summary") or pr.get("error", ""))
            return 0 if pr.get("ok") else 1
        if args.view == "passport":
            print(query_appliance_passport(db, args.keyword)["reply"])
            return 0
        if args.view == "photos":
            print(list_home_photos(db, args.keyword)["reply"])
            return 0
        print(check_install_conflicts(db)["reply"])
        return 0

    if args.cmd == "replenish":
        from siming.replenish_cart import (
            evaluate_replenish_due,
            format_replenish_report,
            mark_replenish_bought,
            seed_daily_replenish_catalog,
            snooze_replenish_item,
            stage_jd_cart_add,
            stage_replenish_buy,
            upsert_replenish_item,
        )

        if args.rpl_cmd == "seed":
            rs = seed_daily_replenish_catalog(db, overwrite=bool(args.overwrite))
            print(
                f"✅ 日用品周期台账已同步：共 {rs['total']} 项（新增 {rs['added']} · 更新 {rs['updated']} · 保留 {rs['kept']}）"
            )
            print(format_replenish_report(db))
            return 0
        if args.rpl_cmd == "check":
            chk = evaluate_replenish_due(db, notify=bool(args.send), force_window=True)
            print(
                f"🔍 周期巡检完成：到期/临期 {len(chk.get('due') or [])} 项（{', '.join(chk.get('due') or []) or '无'}）"
                f" · 推送 {len(chk.get('pushed') or [])} 条"
            )
            return 0
        if args.rpl_cmd == "buy":
            rb = stage_replenish_buy(db, args.target, qty=args.qty)
            print(rb.get("reply") or rb.get("error", ""))
            return 0 if rb.get("ok") else 1
        if args.rpl_cmd == "bought":
            rm = mark_replenish_bought(db, args.target, bought_date=args.date)
            print(rm.get("summary") or rm.get("error", ""))
            return 0 if rm.get("ok") else 1
        if args.rpl_cmd == "snooze":
            rz = snooze_replenish_item(db, args.target, days=args.days)
            print(rz.get("summary") or rz.get("error", ""))
            return 0 if rz.get("ok") else 1
        if args.rpl_cmd == "add":
            ra = upsert_replenish_item(
                db,
                name=args.name,
                sku_or_url=args.sku_or_url,
                cycle_days=args.days,
                target_yuan=args.target_yuan,
                qty=args.qty,
                last_bought_date=args.last_bought,
                channel=args.channel,
                location=args.location,
                meituan_shop=args.shop,
                meituan_query=args.query,
                spec=args.spec,
            )
            print(ra.get("summary") or ra.get("error", ""))
            return 0 if ra.get("ok") else 1
        if args.rpl_cmd == "cart":
            rc = stage_jd_cart_add(db, args.target, qty=args.qty)
            print(rc["reply"])
            return 0 if rc.get("ok") else 1
        print(format_replenish_report(db))
        return 0

    if args.cmd == "care":
        from siming import daily_care as dc

        cc = args.care_cmd or "status"
        if cc == "weather":
            r = dc.run_weather_guard(db, force=True, dry_run=not args.send)
            if r.get("text"):
                print(r["text"])
                print(f"\n（有需要提醒的内容：{'是' if r.get('would_push') else '否，平时不会推送'}）")
            else:
                print(json.dumps({k: v for k, v in r.items() if k != "analysis"}, ensure_ascii=False))
            return 0 if r.get("ok", True) else 1
        if cc == "harvest":
            from siming.collectors.meituan_orders import collect_meituan_orders

            r = collect_meituan_orders(db, since_days=args.days)
            print(json.dumps(r, ensure_ascii=False))
            return 0 if r.get("ok") else 1
        if cc in ("inject", "skip"):
            print(dc.record_injection(db, skipped=(cc == "skip"))["summary"])
            return 0
        if cc == "pen":
            print(dc.set_doses_left(db, args.left)["summary"])
            return 0
        if cc == "refill":
            print(dc.record_refill(db, args.pens)["summary"])
            return 0
        if cc == "stock":
            print(dc.set_supply_stock(db, args.item, args.count)["summary"])
            return 0
        print(dc.format_care_report(db))
        return 0

    if args.cmd == "memory" and args.mem_cmd == "diff":
        mem_dir = db.paths.shared_memory_dir
        cutoff = now_local().timestamp() - args.days * 86400
        recent = [p for p in sorted(mem_dir.rglob("*.md")) if p.stat().st_mtime >= cutoff]
        if not recent:
            print(f"近 {args.days} 天 60-共享记忆 无文件变更。")
            return 0
        for p in recent:
            print(f"• {p.relative_to(mem_dir)}")
        return 0

    if args.cmd == "push":
        from siming.push import push_external

        body = sys.stdin.read() if args.text == "-" else args.text
        body = (body or "").strip()
        if not body:
            print(json.dumps({"ok": True, "skipped": "empty"}, ensure_ascii=False))
            return 0
        kb_obj = None
        if getattr(args, "keyboard_json", ""):
            try:
                parsed_kb = json.loads(args.keyboard_json)
                if isinstance(parsed_kb, dict):
                    kb_obj = parsed_kb
            except json.JSONDecodeError as exc:
                print(json.dumps({"ok": False, "error": f"invalid --keyboard-json: {exc}"}, ensure_ascii=False))
                return 2
        try:
            res = push_external(
                db,
                body,
                rule_id=args.rule,
                ref_id=args.ref,
                level=args.level,
                dedup_window=timedelta(seconds=parse_duration_seconds(args.dedup)),
                hard=args.hard,
                keyboard=kb_obj,
            )
        except ValueError as exc:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
            return 2
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") and (res.get("delivered") or res.get("queued_for_morning") or res.get("skipped")) else 1

    if args.cmd == "cron-ingest":
        body = args.text or sys.stdin.read().strip()
        if not body:
            print("[SILENT]")
            return 0
        import hashlib

        h = hashlib.sha256(f"{args.job}:{body}".encode("utf-8")).hexdigest()[:28]
        with db.transaction() as conn:
            ev_id = db.append_event(
                conn,
                source="cron_ingest",
                thread_key=f"cron:{args.job}",
                summary=f"[cron:{args.job}] {body.splitlines()[0][:80]}",
                payload={"job": args.job, "body": body[:2000]},
                fingerprint=f"cron:{h}",
            )
            if ev_id and args.notify_on_change:
                db.enqueue_effect(
                    conn,
                    "send_qq",
                    {"ref_id": f"cron:{args.job}", "rule_id": f"cron_{args.job}", "text": body[:1200]},
                )
        consume_pending_effects(db)
        print("[SILENT]")
        return 0

    if args.cmd == "inbound":
        in_text = args.text
        in_rid = args.reply_to_id or None
        in_rtxt = args.reply_to_text or None
        in_media = args.media or None
        in_jev = None
        if args.stdin_json:
            raw_in = sys.stdin.read()
            if raw_in.strip():
                in_obj = json.loads(raw_in)
                in_text = str(in_obj.get("text") or "")
                in_rid = str(in_obj.get("reply_to_id") or "") or None
                in_rtxt = str(in_obj.get("reply_to_text") or "") or None
                in_media = list(in_obj.get("media") or []) or None
                if isinstance(in_obj.get("jev_decision"), dict):
                    in_jev = dict(in_obj["jev_decision"])
        out = handle_inbound_qq(
            db,
            text=in_text,
            reply_to_msg_id=in_rid,
            reply_to_text=in_rtxt,
            media_paths=in_media,
            jev_decision=in_jev,
        )
        print(json.dumps(out, ensure_ascii=False))
        return 0

    if args.cmd == "inbound-action":
        from siming.qq_cards import handle_qq_button_action

        btn_data = args.data
        if args.stdin_json:
            raw_in = sys.stdin.read()
            if raw_in.strip():
                in_obj = json.loads(raw_in)
                btn_data = str(in_obj.get("data") or "")
        out = handle_qq_button_action(db, btn_data)
        print(json.dumps(out, ensure_ascii=False))
        return 0

    if args.cmd == "tick":
        res = run_tick(db, include_collectors=not args.no_collectors)
        print(json.dumps(res, ensure_ascii=False))
        return 0

    if args.cmd == "run-effect-worker":
        res = run_single_effect_worker(db, args.effect_id, expected_gen=int(getattr(args, "expected_gen", 0) or 0))
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if args.cmd == "watchdog":
        res = run_watchdog(db)
        consume_pending_effects(db, include_workers=False)
        print(json.dumps(res, ensure_ascii=False))
        return 0

    if args.cmd == "edge-sync":
        res = sync_edge_node(db)
        # edge-sync 超时上限仅 45s，只冲刷轻量队列与通知，绝不在此拉起 30m worker！
        consume_pending_effects(db, include_workers=False)
        print(json.dumps(res, ensure_ascii=False))
        return 0 if res.get("ok") else 1

    if args.cmd == "mail-sync":
        from siming.collectors.mail163 import collect_mail163

        res = collect_mail163(db, max_fetch=args.limit)
        process_new_events(db, coalesce_sec=0)
        consume_pending_effects(db)
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok") else 1

    if args.cmd == "gmail-reauth":
        script = db.paths.repo_root / "tools" / "gmail_reauth.py"
        return subprocess.call([sys.executable, str(script)])

    if args.cmd == "backup":
        res = db.online_backup_and_verify()
        print(json.dumps(res, ensure_ascii=False))
        return 0

    if args.cmd == "doctor":
        rep = run_doctor(db, check_mail=args.check_mail, check_edge=not args.no_edge)
        if args.json:
            print(json.dumps(rep, ensure_ascii=False, indent=2))
        else:
            print(format_doctor_report(rep))
        return 0 if rep["ok"] else 1

    if args.cmd == "replay":
        if args.days and args.days > 0:
            from siming.event_chain import replay_event_chain_history

            rep_h = replay_event_chain_history(db, days=args.days)
            print(rep_h["reply"])
            return 0
        rep = run_replay_fixtures(db.paths, fixture=args.fixture)
        print(json.dumps(rep, ensure_ascii=False, indent=2))
        return 0 if rep["ok"] else 1

    if args.cmd == "tesla":
        from siming.crypto import write_secret
        from siming.outing_chain import plan_tesla_or_didi_outing
        from siming.tesla_link import (
            check_tesla_range_for_trip,
            format_tesla_status,
            register_and_authorize_tesla_cn_fleet,
        )

        if args.auth:
            ar = register_and_authorize_tesla_cn_fleet(db, args.auth[0], args.auth[1], args.auth[2])
            if not ar.get("ok"):
                print(ar.get("error"), file=sys.stderr)
                return 1
            print(ar["summary"])
            return 0
        if args.set_fleet_token:
            write_secret("tesla-fleet.json", json.dumps({"access_token": args.set_fleet_token.strip()}), db.paths)
            print("已加密保存 `tesla-fleet.json` (0600)。")
            return 0
        if args.dest:
            spec = f"{args.dest} {args.arrive_by}到".strip() if args.arrive_by else args.dest
            ores = plan_tesla_or_didi_outing(db, spec)
            print(ores["reply"])
            return 0 if ores.get("ok") else 1
        if args.trip > 0:
            adv = check_tesla_range_for_trip(db, args.trip, dest_name="目标地点")
            print(adv.get("summary") or "尚未收到车况同步数据。")
            return 0
        print(format_tesla_status(db))
        return 0

    if args.cmd == "routines":
        from siming.event_chain import OBSERVABILITY_CATALOG, format_routines_panel

        print(format_routines_panel(db))
        if args.catalog:
            print("\n=== L1 Observability Catalog ===")
            for k, meta in OBSERVABILITY_CATALOG.items():
                print(f"• {k:<18} [{meta['cost']}/{meta['granularity']}] {meta['desc']}")
        return 0

    if args.cmd == "bookmarks":
        from siming.superagent_triage import format_bookmarks_list, sandbox_try_bookmark

        if args.sandbox:
            sb = sandbox_try_bookmark(db, args.sandbox)
            print(sb["reply"])
            return 0 if sb.get("ok") else 1
        print(format_bookmarks_list(db, query=args.category))
        return 0

    if args.cmd == "propose-agent-rules":
        targets: list[tuple[str, Path]] = []
        if args.target in ("agents", "both"):
            # 注意 ~/.agents/AGENTS.md 是指向 Obsidian Vault 10-用户偏好-AGENTS.md 的符号链接，解析真实路径写入并自动备份
            agents_real = (Path.home() / ".agents" / "AGENTS.md").resolve()
            targets.append(("AGENTS.md", agents_real))
        if args.target in ("soul", "both"):
            targets.append(("SOUL.md", Path.home() / ".hermes" / "SOUL.md"))

        for label, path in targets:
            ap = create_approval(
                db,
                action="append_block_with_backup",
                target_id=f"RULE-{label}",
                params={
                    "path": str(path),
                    "marker": "<!-- siming-defer-rule -->",
                    "block": AGENTS_DEFER_RULE_BLOCK,
                },
                tier="A2",
                ttl_minutes=1440,
            )
            print(f"[{ap['token']}] 已生成 `{path}` 追加预览（24h 内执行 `siming apply {ap['token']}` 即自动备份并写入）")
        return 0

    from siming.cli_v5 import dispatch_v5_command

    v5_rc = dispatch_v5_command(db, args)
    if v5_rc is not None:
        return v5_rc

    from siming.cli_registry import dispatch_v6_command

    v6_rc = dispatch_v6_command(db, args)
    if v6_rc is not None:
        return v6_rc

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
