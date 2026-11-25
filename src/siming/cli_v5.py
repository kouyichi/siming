"""司命 v5（Wave 1–4）统一命令行注册与分发模块。

覆盖已拍板的全部 24 项新场景：
- Wave 1 (7): sure / compute / subs / meal / sentinel / health / car
- Wave 2 (6): home-air / homelab / print3d / haos / commute / passport
- Wave 3 (7): runtime / wiki / msim / handoff / radar / digest / infra
- Wave 4 (4, 暂缓 W4-2 & W4-4): graph / trip / social / compound / tower
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from siming.db import SimingDB


def register_v5_subcommands(sub: argparse._SubParsersAction) -> None:
    """向主 CLI ArgumentParser 注册 v5 Wave 1–4 全部子命令。"""
    # ==================== WAVE 1 ====================
    p_sure = sub.add_parser("sure", help="W1-1：Sure 财务 OS 双向同步、规则、Pipelock 与自然语言查账")
    s_sub = p_sure.add_subparsers(dest="sure_cmd")
    s_sub.add_parser("status", help="查看 Sure 同步状态与 Pipelock 策略")
    p_s_sync = s_sub.add_parser("sync", help="将 ledger_txns 幂等同步至 Sure")
    p_s_sync.add_argument("--limit", type=int, default=500)
    p_s_sync.add_argument("--dry-run", action="store_true")
    p_s_ask = s_sub.add_parser("ask", help="自然语言查账（Sure MCP + 本地账本镜像）")
    p_s_ask.add_argument("question")
    s_sub.add_parser("rules", help="列出全部 Sure 分类/拆单/周期规则")
    p_s_r = s_sub.add_parser("add-rule", help="新增或更新 Sure 规则")
    p_s_r.add_argument("name")
    p_s_r.add_argument("pattern")
    p_s_r.add_argument("--account", default="")
    p_s_r.add_argument("--category", default="")
    p_s_r.add_argument("--tags", default="")
    p_s_r.add_argument("--split", type=float, default=1.0)
    p_s_r.add_argument("--recurring", action="store_true")
    p_s_r.add_argument("--cadence", type=int, default=0)

    p_comp = sub.add_parser("compute", help="W1-2：多平台 AI 算力/API 余额、烧爆熔断与 Sure 入账")
    c_sub = p_comp.add_subparsers(dest="compute_cmd")
    c_sub.add_parser("status", help="查看多平台算力资产总控面板")
    p_c_up = c_sub.add_parser("upsert", help="登记或更新一个 AI 算力账户")
    p_c_up.add_argument("provider")
    p_c_up.add_argument("account_label")
    p_c_up.add_argument("--balance", type=float, required=True)
    p_c_up.add_argument("--currency", default="CNY")
    p_c_up.add_argument("--daily-spend", type=float, default=0.0)
    p_c_up.add_argument("--avg-daily", type=float, default=0.0)
    p_c_up.add_argument("--plan", default="")
    p_c_up.add_argument("--expires", default="")
    p_c_up.add_argument("--scope", choices=["personal", "work_cw08"], default="personal")
    p_c_up.add_argument("--fallback", default="")
    p_c_chk = c_sub.add_parser("check", help="执行余额/烧爆/到期巡检")
    p_c_chk.add_argument("--notify", action="store_true")
    p_c_rt = c_sub.add_parser("route", help="查询健康模型路由与 404 切备建议")
    p_c_rt.add_argument("provider")
    p_c_rt.add_argument("model")
    p_c_rt.add_argument("--error-code", type=int, default=0)
    p_c_ss = c_sub.add_parser("sync-sure", help="按公私标签把月度算力成本同步至 Sure")
    p_c_ss.add_argument("--month", default="")
    c_sub.add_parser("sync-live", help="从本机 Magpie / ai-usagebar / ai-quota 同步实时算力余额至 compute_assets")

    p_subs = sub.add_parser("subs", help="W1-3：全渠道订阅刺客审计、涨价与 48h 止损卡")
    sb_sub = p_subs.add_subparsers(dest="subs_cmd")
    sb_sub.add_parser("audit", help="扫描流水与邮件识别全部周期订阅并输出体检报告")
    p_sb_up = sb_sub.add_parser("upcoming", help="查看未来 N 小时即将扣费的订阅")
    p_sb_up.add_argument("--hours", type=int, default=48)
    sb_sub.add_parser("alerts", help="为 48h 内即将扣费/涨价订阅生成 QQ 止损卡")
    p_sb_mk = sb_sub.add_parser("mark", help="标记订阅状态（KEEP / ZOMBIE / CANCELLED / ACTIVE）与是否进行中")
    p_sb_mk.add_argument("target")
    p_sb_mk.add_argument("status", nargs="?", choices=["KEEP", "ZOMBIE", "CANCELLED", "ACTIVE"], default=None)
    p_sb_mk_grp = p_sb_mk.add_mutually_exclusive_group()
    p_sb_mk_grp.add_argument("--ongoing", action="store_true", help="显式标为进行中")
    p_sb_mk_grp.add_argument("--stopped", action="store_true", help="显式标为已停扣/未在进行")
    p_sb_mk.add_argument("--note", default="")

    p_meal = sub.add_parser("meal", help="W1-4：防忘吃饭守卫与跨店双拼点餐协调")
    m_sub = p_meal.add_subparsers(dest="meal_cmd")
    p_m_g = m_sub.add_parser("guard", help="检查当前餐次是否可能忘吃饭")
    p_m_g.add_argument("--notify", action="store_true")
    p_m_c = m_sub.add_parser("combo", help="规划跨店双拼方案（时差+配送费+拆单神券）")
    p_m_c.add_argument("query")
    p_m_c.add_argument("--loc", default="")
    p_m_s = m_sub.add_parser("stage", help="生成跨店双拼 A2 审批卡")
    p_m_s.add_argument("query")
    p_m_s.add_argument("--loc", default="")

    p_sen = sub.add_parser("sentinel", help="W1-5：丰巢 18h 免费倒计时与 7 天无理由退货哨兵")
    sn_sub = p_sen.add_subparsers(dest="sentinel_cmd")
    sn_sub.add_parser("status", help="查看当前丰巢倒计时与 7 天退货窗状态")
    p_sn_run = sn_sub.add_parser("run", help="执行一次哨兵检查")
    p_sn_run.add_argument("--notify", action="store_true")

    p_hl = sub.add_parser("health", help="W1-6：私人健康驾驶舱（化验单 OCR + 趋势 + 常用医院复诊摘要）")
    h_sub = p_hl.add_subparsers(dest="health_cmd")
    h_sub.add_parser("status", help="查看近 90 天健康指标趋势")
    p_h_in = h_sub.add_parser("ingest", help="从化验单 OCR 文本提取并入库指标")
    p_h_in.add_argument("text")
    p_h_rec = h_sub.add_parser("record", help="手动录入单条健康指标")
    p_h_rec.add_argument("metric_type")
    p_h_rec.add_argument("value", type=float)
    p_h_rec.add_argument("--unit", default="")
    p_h_rec.add_argument("--notes", default="")
    p_h_doc = h_sub.add_parser("doctor-brief", help="生成给医生看的 1 页 3 个月摘要")
    p_h_doc.add_argument("--hospital", default="常用医院")
    p_h_doc.add_argument("--days", type=int, default=90)

    p_car = sub.add_parser("car", help="W1-7：Model Y 车务成本、LFP 校准、车险与 12123 违章")
    car_sub = p_car.add_subparsers(dest="car_cmd")
    car_sub.add_parser("status", help="查看车务全景报告")
    p_car_h = car_sub.add_parser("harvest", help="从账本自动汇总 ETC/停车/超充记录")
    p_car_h.add_argument("--days", type=int, default=365)
    p_car_r = car_sub.add_parser("record", help="录入一条车务/车险/违章/LFP满充记录")
    p_car_r.add_argument("kind", choices=["charge", "etc", "parking", "insurance", "violation", "lfp_cal"])
    p_car_r.add_argument("title")
    p_car_r.add_argument("--amount", type=float, default=0.0)
    p_car_r.add_argument("--scope", choices=["personal", "work"], default="personal")
    p_car_r.add_argument("--provider", default="")
    p_car_chk = car_sub.add_parser("check", help="检查 LFP 每周满充校准与车险到期")
    p_car_chk.add_argument("--notify", action="store_true")
    car_sub.add_parser("sync-sure", help="将车务账目同步到 Sure 车辆账户")

    # ==================== WAVE 2 ====================
    p_air = sub.add_parser("home-air", help="W2-1：示例小区B放甲醛远程新风策略与 L3 浸水/燃气安防")
    a_sub = p_air.add_subparsers(dest="air_cmd")
    a_sub.add_parser("status", help="查看新房空气质量、通风策略与达标日预测")
    p_a_in = a_sub.add_parser("ingest", help="录入一条新房传感器读数")
    p_a_in.add_argument("--room", default="客厅")
    p_a_in.add_argument("--temp", type=float, default=22.0)
    p_a_in.add_argument("--humidity", type=float, default=45.0)
    p_a_in.add_argument("--hcho", type=float, required=True)
    p_a_in.add_argument("--voc", type=float, default=200.0)
    p_a_in.add_argument("--aqi", type=int, default=50)
    p_a_in.add_argument("--water-leak", action="store_true")
    p_a_in.add_argument("--gas-leak", action="store_true")
    p_a_in.add_argument("--power-out", action="store_true")
    a_sub.add_parser("strategy", help="查看当前新风编排建议")
    a_sub.add_parser("predict", help="预测甲醛衰减达标入住日期")

    p_hlb = sub.add_parser("homelab", help="W2-2：RB5009 / 光猫桥接 / NAS 与三地加密灾备")
    hlb_sub = p_hlb.add_subparsers(dest="homelab_cmd")
    hlb_sub.add_parser("status", help="查看家庭实验室各节点健康状态")
    p_hlb_rec = hlb_sub.add_parser("record", help="上报节点状态")
    p_hlb_rec.add_argument("node_id")
    p_hlb_rec.add_argument("kind", choices=["router", "nas", "modem", "vps", "backup"])
    p_hlb_rec.add_argument("name")
    p_hlb_rec.add_argument("status", choices=["OK", "WARN", "ERROR"])
    p_hlb_rec.add_argument("--detail", default="")
    hlb_sub.add_parser("backup-plan", help="生成桌面→NAS→边缘 VPS三地灾备清单")
    hlb_sub.add_parser("modem-playbook", help="查看 modem-bridge-tool 光猫改桥接 + RB5009 操作清单")

    p_p3d = sub.add_parser("print3d", help="W2-3：Bambu 3D 3D 打印监控与 PLA/PETG 耗材库")
    p3_sub = p_p3d.add_subparsers(dest="p3d_cmd")
    p3_sub.add_parser("status", help="查看打印机任务与耗材余量")
    p_p3_sp = p3_sub.add_parser("add-spool", help="登记/更新一卷耗材")
    p_p3_sp.add_argument("spool_id")
    p_p3_sp.add_argument("--material", default="PLA")
    p_p3_sp.add_argument("--color", default="白色")
    p_p3_sp.add_argument("--grams", type=float, default=1000.0)
    p_p3_sp.add_argument("--humidity", type=float, default=25.0)
    p_p3_use = p3_sub.add_parser("use", help="扣减耗材克数并在低余量时出 A2 补货卡")
    p_p3_use.add_argument("spool_id")
    p_p3_use.add_argument("grams", type=float)
    p_p3_job = p3_sub.add_parser("job", help="上报拓竹打印任务状态")
    p_p3_job.add_argument("job_id")
    p_p3_job.add_argument("name")
    p_p3_job.add_argument("status", choices=["RUNNING", "PAUSED", "DONE", "FAILED"])
    p_p3_job.add_argument("--progress", type=float, default=0.0)
    p_p3_job.add_argument("--spool", default="")
    p_p3_job.add_argument("--grams", type=float, default=0.0)
    p_p3_job.add_argument("--anomaly", default="")

    p_ha = sub.add_parser("haos", help="W2-4：懂夜猫子的 HAOS 全屋智能联动与扫地机补觉硬锁")
    ha_sub = p_ha.add_subparsers(dest="haos_cmd")
    ha_sub.add_parser("status", help="查看 HAOS 夜猫子联动状态")
    ha_sub.add_parser("sync", help="同步作息与定位状态至 HAOS")
    ha_sub.add_parser("vacuum", help="检查科沃斯扫地机补觉硬锁状态")

    p_com = sub.add_parser("commute", help="W2-5：示例小区B 60m 多模态通勤决策中枢")
    cm_sub = p_com.add_subparsers(dest="commute_cmd")
    p_cm_c = cm_sub.add_parser("compare", help="对比自驾/地铁/滴滴最优通勤方案")
    p_cm_c.add_argument("--ev-min", type=int, default=48)
    p_cm_c.add_argument("--soc", type=int, default=65)
    p_cm_c.add_argument("--metro-min", type=int, default=58)
    p_cm_c.add_argument("--didi-min", type=int, default=45)
    p_cm_c.add_argument("--didi-fare", type=float, default=68.0)
    p_cm_c.add_argument("--rain", action="store_true")
    p_cm_c.add_argument("--late", type=int, default=0)
    p_cm_r = cm_sub.add_parser("rescue", help="生成极端天气/起晚捆绑应急 A2 卡")
    p_cm_r.add_argument("--reason", default="暴雨/起晚")
    cm_sub.add_parser("regime", help="检查 2028-01-01 搬家通勤切换状态")

    p_pp = sub.add_parser("passport", help="W2-6：水电物业守卫、家电耗材寿命、故障码与固定资产折旧")
    pp_sub = p_pp.add_subparsers(dest="passport_cmd")
    pp_sub.add_parser("status", help="查看住宅与家电全周期护照总览")
    p_pp_u = pp_sub.add_parser("utility", help="登记/更新水电燃气宽带物业账期")
    p_pp_u.add_argument("kind", choices=["water", "electric", "gas", "broadband", "property"])
    p_pp_u.add_argument("--due", required=True)
    p_pp_u.add_argument("--amount", type=float, default=0.0)
    p_pp_c = pp_sub.add_parser("consumable", help="登记/更新家电耗材（滤芯/尘袋/洗碗盐）")
    p_pp_c.add_argument("appliance_id")
    p_pp_c.add_argument("appliance_name")
    p_pp_c.add_argument("consumable_name")
    p_pp_c.add_argument("--days", type=int, default=90)
    p_pp_f = pp_sub.add_parser("fault", help="秒查家电故障码、保修余期与排障指引")
    p_pp_f.add_argument("brand")
    p_pp_f.add_argument("code")
    pp_sub.add_parser("assets", help="计算 15 台家电固定资产折旧并同步 Sure")

    # ==================== WAVE 3 ====================
    p_rt = sub.add_parser("runtime", help="W3-1：Linger/Pi 多 Runtime 双轨路由（订阅账号 + Pi 本地/API）")
    rt_sub = p_rt.add_subparsers(dest="runtime_cmd")
    rt_sub.add_parser("status", help="查看全部可用 Runtime 与配额水位")
    p_rt_sel = rt_sub.add_parser("select", help="按任务类型与复杂度选择最优 Runtime")
    p_rt_sel.add_argument("--kind", default="coding")
    p_rt_sel.add_argument("--complexity", choices=["low", "medium", "high"], default="high")
    p_rt_sel.add_argument("--prefer", default="")
    p_rt_disp = rt_sub.add_parser("dispatch", help="以 Linger/Pi 双轨路由干跑或派发任务")
    p_rt_disp.add_argument("task")
    p_rt_disp.add_argument("--kind", default="coding")
    p_rt_disp.add_argument("--complexity", choices=["low", "medium", "high"], default="high")

    p_wk = sub.add_parser("wiki", help="W3-2：Hindsight 夜间自动共享 Wiki（全机 Agent 共享记忆）")
    wk_sub = p_wk.add_subparsers(dest="wiki_cmd")
    wk_sub.add_parser("status", help="查看共享 Wiki 页面与冲突检测状态")
    wk_sub.add_parser("distill", help="立即执行一次离线会话蒸馏并更新 Obsidian 共享 Wiki")
    p_wk_q = wk_sub.add_parser("query", help="检索 Hindsight 共享 Wiki")
    p_wk_q.add_argument("query")

    p_ms = sub.add_parser("msim", help="W3-3：M-SIM 跨模型 Self-Improvement Memory 进化日记")
    ms_sub = p_ms.add_subparsers(dest="msim_cmd")
    p_ms_rec = ms_sub.add_parser("record", help="记入一条跨模型调优进化记录")
    p_ms_rec.add_argument("family")
    p_ms_rec.add_argument("model")
    p_ms_rec.add_argument("rationale")
    p_ms_rec.add_argument("--expected", default="")
    p_ms_rec.add_argument("--score", type=float, default=1.0)
    p_ms_rec.add_argument("--baseline", type=float, default=0.0)
    p_ms_rec.add_argument("--failed", action="store_true")
    p_ms_rec.add_argument("--artifact", default="")
    p_ms_s = ms_sub.add_parser("search", help="检索某问题族的前世调优记忆")
    p_ms_s.add_argument("--family", default="")
    p_ms_s.add_argument("--query", default="")
    p_ms_inj = ms_sub.add_parser("inject", help="生成给新模型继承的 M-SIM 提示块")
    p_ms_inj.add_argument("family")
    p_ms_inj.add_argument("--target-model", default="gemini")
    ms_sub.add_parser("leaderboard", help="查看按问题族与模型汇总的进化榜单")
    ms_sub.add_parser("export", help="导出 Markdown 进化日记索引")

    p_ho = sub.add_parser("handoff", help="W3-4：凌晨睡前异步实验交班台（GPU/Agent 白名单静默守卫）")
    ho_sub = p_ho.add_subparsers(dest="handoff_cmd")
    p_ho_add = ho_sub.add_parser("add", help="登记一个睡前托管实验")
    p_ho_add.add_argument("name")
    p_ho_add.add_argument("--host", default="gpu-server")
    p_ho_add.add_argument("--pane", default="")
    p_ho_add.add_argument("--log", default="")
    ho_sub.add_parser("patrol", help="巡检当前托管实验日志")
    ho_sub.add_parser("report", help="生成自然醒对比战报")

    p_rd = sub.add_parser("radar", help="W3-5：问题驱动科研雷达与 123 PR 高信噪比分拣")
    rd_sub = p_rd.add_subparsers(dest="radar_cmd")
    p_rd_add = rd_sub.add_parser("add-pain", help="绑定一条活跃仓库痛点")
    p_rd_add.add_argument("repo")
    p_rd_add.add_argument("topic")
    p_rd_add.add_argument("--keywords", required=True, help="逗号分隔关键词")
    rd_sub.add_parser("digest", help="输出科研与 PR 雷达摘要")

    p_dg = sub.add_parser("digest", help="W3-6：跨平台收藏 Top-3 消化与独游创意工坊")
    dg_sub = p_dg.add_subparsers(dest="digest_cmd")
    p_dg_w = dg_sub.add_parser("weekly", help="生成本周 Top-3 收藏消化与隔离试跑命令")
    p_dg_w.add_argument("--limit", type=int, default=3)
    p_dg_i = dg_sub.add_parser("indie", help="联动桌游规则库生成独立游戏机制与 Steam 竞品创意简报")
    p_dg_i.add_argument("topic")
    p_dg_i.add_argument("--mechanics", default="")

    p_inf = sub.add_parser("infra", help="W3-7：Omarchy 升级守卫、TLS 证书与代理/DNS 自愈巡检")
    inf_sub = p_inf.add_subparsers(dest="infra_cmd")
    p_inf_p = inf_sub.add_parser("patrol", help="执行全量数字基建巡检")
    p_inf_p.add_argument("--no-heal", action="store_true")
    inf_sub.add_parser("omarchy", help="执行 Omarchy 升级前冲突与快照守卫检查")
    p_inf_t = inf_sub.add_parser("tls", help="检查 TLS 证书到期时间")
    p_inf_t.add_argument("--host", default="edge.example.com")
    p_inf_t.add_argument("--port", type=int, default=8443)
    inf_sub.add_parser("proxy", help="检查 aTrust DNS 与 sing-box 分流漂移")

    # ==================== WAVE 4 (APPROVED: W4-1, W4-3, W4-5, W4-6; W4-2/W4-4 DEFERRED) ====================
    p_gr = sub.add_parser("graph", help="W4-1：Linger 式跨应用上下文联邦检索图谱")
    gr_sub = p_gr.add_subparsers(dest="graph_cmd")
    gr_sub.add_parser("index", help="重建跨应用（Files/Mail/Calendar/Chat/Wiki/Sure/M-SIM）索引")
    p_gr_q = gr_sub.add_parser("query", help="跨应用联邦检索并附精确出处")
    p_gr_q.add_argument("question")
    p_gr_q.add_argument("--limit", type=int, default=8)
    gr_sub.add_parser("stats", help="查看图谱节点与边统计")

    p_tr = sub.add_parser("trip", help="W4-3：国内/出境差旅全周期行程包（签证/返现卡/CW01）")
    tr_sub = p_tr.add_subparsers(dest="trip_cmd")
    tr_sub.add_parser("list", help="列出全部行程包")
    p_tr_c = tr_sub.add_parser("create", help="创建一段行程包")
    p_tr_c.add_argument("destination")
    p_tr_c.add_argument("start_date")
    p_tr_c.add_argument("end_date")
    p_tr_c.add_argument("--country", default="CN")
    tr_sub.add_parser("detect", help="从邮件/事件自动检测新行程")
    p_tr_cd = tr_sub.add_parser("card", help="查询境外刷卡最优返现卡与汇率建议")
    p_tr_cd.add_argument("--currency", default="JPY")
    p_tr_cd.add_argument("--amount", type=float, default=10000.0)
    p_tr_pk = tr_sub.add_parser("pack-cw01", help="将行程消费打包为 CW01 草稿并打 Sure 标签")
    p_tr_pk.add_argument("trip_id")

    p_soc = sub.add_parser("social", help="W4-5：200 盒桌游柜容/众筹/攒局讲规 + 承诺与加密亲友记忆本")
    sc_sub = p_soc.add_subparsers(dest="social_cmd")
    sc_sub.add_parser("status", help="查看桌游众筹、未闭环承诺与亲友记忆概览")
    p_sc_cab = sc_sub.add_parser("cabinet", help="核查 200 盒 / 3.0m³ 柜容余量")
    p_sc_cab.add_argument("--boxes", type=int, default=0)
    p_sc_cab.add_argument("--vol", type=float, default=0.0)
    p_sc_cab.add_argument("--add-boxes", type=int, default=1)
    p_sc_cab.add_argument("--add-vol", type=float, default=0.012)
    p_sc_cf = sc_sub.add_parser("crowdfund", help="登记桌游众筹补款追踪")
    p_sc_cf.add_argument("title")
    p_sc_cf.add_argument("--platform", default="新物集")
    p_sc_cf.add_argument("--pledge", type=float, default=0.0)
    p_sc_cf.add_argument("--balance", type=float, default=0.0)
    p_sc_cf.add_argument("--due", default="")
    p_sc_gn = sc_sub.add_parser("gamenight", help="生成桌游暖房攒局投票与 3 分钟讲规卡")
    p_sc_gn.add_argument("--players", type=int, default=4)
    p_sc_gn.add_argument("--minutes", type=int, default=90)
    p_sc_cm = sc_sub.add_parser("commit", help="登记一条对外承诺")
    p_sc_cm.add_argument("person")
    p_sc_cm.add_argument("promise")
    p_sc_cm.add_argument("--due", default="")
    p_sc_fr = sc_sub.add_parser("friend", help="查看或加密写入亲友偏好记忆")
    p_sc_fr.add_argument("name")
    p_sc_fr.add_argument("--notes", default="")
    p_sc_fr.add_argument("--birthday", default="")
    p_sc_fr.add_argument("--diet", default="")

    p_cmp = sub.add_parser("compound", help="W4-6：QQ 语音/文本多意图复合指令解析与单卡审批")
    p_cmp.add_argument("utterance")
    p_cmp.add_argument("--stage", action="store_true", help="生成统一 A2 审批卡与 QQ 按钮")

    p_tw = sub.add_parser("tower", help="W4-6：司命统一网页控制塔（联邦 Sure / Wiki / M-SIM / Routines / 模块自省）")
    tw_sub = p_tw.add_subparsers(dest="tower_cmd")
    p_tw_srv = tw_sub.add_parser("serve", help="启动受密码保护的本地控制塔 Web 服务")
    p_tw_srv.add_argument("--host", default="127.0.0.1")
    p_tw_srv.add_argument("--port", type=int, default=8788)
    p_tw_srv.add_argument("--token", default="")
    tw_sub.add_parser("snapshot", help="输出控制塔全量 JSON 快照")


def dispatch_v5_command(db: SimingDB, args: argparse.Namespace) -> Optional[int]:
    """若 args.cmd 属于 v5 子命令则执行并返回退出码，否则返回 None。"""
    cmd = getattr(args, "cmd", "")

    # ==================== WAVE 1 ====================
    if cmd == "sure":
        from siming.sure_bridge import (
            ask_sure_natural,
            format_sure_summary,
            list_sure_rules,
            sync_ledger_to_sure,
            upsert_sure_rule,
        )

        sc = args.sure_cmd or "status"
        if sc == "status":
            print(format_sure_summary(db))
            return 0
        if sc == "sync":
            res = sync_ledger_to_sure(db, limit=args.limit, dry_run=args.dry_run)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if sc == "ask":
            res = ask_sure_natural(db, args.question)
            print(res["reply"])
            return 0 if res.get("ok") else 1
        if sc == "rules":
            print(json.dumps(list_sure_rules(db), ensure_ascii=False, indent=2))
            return 0
        if sc == "add-rule":
            tags = [t.strip() for t in args.tags.split(",") if t.strip()]
            res = upsert_sure_rule(
                db,
                args.name,
                args.pattern,
                account_name=args.account,
                category=args.category,
                tags=tags,
                split_ratio=args.split,
                is_recurring=args.recurring,
                cadence_days=args.cadence,
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

    if cmd == "compute":
        from siming.compute_assets import (
            evaluate_compute_guardrails,
            format_compute_dashboard,
            recommend_healthy_model,
            sync_compute_costs_to_sure,
            upsert_compute_asset,
        )

        cc = args.compute_cmd or "status"
        if cc == "status":
            print(format_compute_dashboard(db))
            return 0
        if cc == "upsert":
            res = upsert_compute_asset(
                db,
                args.provider,
                args.account_label,
                args.balance,
                currency=args.currency,
                daily_spend=args.daily_spend,
                avg_daily_spend=args.avg_daily,
                plan_name=args.plan,
                plan_expires_at=args.expires,
                billing_scope=args.scope,
                fallback_model=args.fallback,
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if cc == "check":
            res = evaluate_compute_guardrails(db, notify=args.notify)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if cc == "route":
            res = recommend_healthy_model(db, args.provider, args.model, error_code=args.error_code)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if cc == "sync-sure":
            res = sync_compute_costs_to_sure(db, month=args.month or None)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if cc == "sync-live":
            from siming.compute_monitor import sync_live_compute_assets

            res = sync_live_compute_assets(db, allow_live=True)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

    if cmd == "subs":
        from siming.sub_hunter import (
            emit_subscription_stoploss_alerts,
            find_upcoming_charges,
            format_subscription_audit,
            mark_subscription_status,
            scan_subscriptions,
        )

        sc = args.subs_cmd or "audit"
        if sc == "audit":
            scan_subscriptions(db)
            print(format_subscription_audit(db))
            return 0
        if sc == "upcoming":
            print(json.dumps(find_upcoming_charges(db, horizon_hours=args.hours), ensure_ascii=False, indent=2))
            return 0
        if sc == "alerts":
            print(json.dumps(emit_subscription_stoploss_alerts(db), ensure_ascii=False, indent=2))
            return 0
        if sc == "mark":
            ongoing_opt = True if getattr(args, "ongoing", False) else (False if getattr(args, "stopped", False) else None)
            res = mark_subscription_status(db, args.target, args.status, note=args.note, is_ongoing=ongoing_opt)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res.get("ok") else 1

    if cmd == "meal":
        from siming.meal_guard import (
            evaluate_meal_regularity,
            format_combo_plan,
            plan_dual_shop_combo,
            stage_dual_shop_combo_approval,
        )

        mc = args.meal_cmd or "guard"
        if mc == "guard":
            print(json.dumps(evaluate_meal_regularity(db, notify=args.notify), ensure_ascii=False, indent=2))
            return 0
        if mc == "combo":
            plan = plan_dual_shop_combo(db, args.query, location=args.loc)
            print(format_combo_plan(plan))
            return 0
        if mc == "stage":
            res = stage_dual_shop_combo_approval(db, args.query, location=args.loc)
            print(res.get("reply") or json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res.get("ok") else 1

    if cmd == "sentinel":
        from siming.parcel_sentinel import format_sentinel_report, run_parcel_sentinel

        sc = args.sentinel_cmd or "status"
        if sc == "status":
            print(format_sentinel_report(db))
            return 0
        if sc == "run":
            print(json.dumps(run_parcel_sentinel(db, notify=args.notify), ensure_ascii=False, indent=2))
            return 0

    if cmd == "health":
        from siming.health_cockpit import (
            generate_doctor_brief,
            get_health_trends,
            ingest_health_text,
            record_health_metric,
        )

        hc = args.health_cmd or "status"
        if hc == "status":
            print(json.dumps(get_health_trends(db), ensure_ascii=False, indent=2))
            return 0
        if hc == "ingest":
            print(json.dumps(ingest_health_text(db, args.text), ensure_ascii=False, indent=2))
            return 0
        if hc == "record":
            print(
                json.dumps(
                    record_health_metric(db, args.metric_type, args.value, unit=args.unit, notes=args.notes),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if hc == "doctor-brief":
            print(generate_doctor_brief(db, hospital=args.hospital, days=args.days))
            return 0

    if cmd == "car":
        from siming.vehicle_cfo import (
            evaluate_lfp_calibration_and_insurance,
            format_vehicle_report,
            harvest_vehicle_costs_from_ledger,
            record_vehicle_event,
            sync_vehicle_to_sure,
        )

        cc = args.car_cmd or "status"
        if cc == "status":
            print(format_vehicle_report(db))
            return 0
        if cc == "harvest":
            print(json.dumps(harvest_vehicle_costs_from_ledger(db, days=args.days), ensure_ascii=False, indent=2))
            return 0
        if cc == "record":
            res = record_vehicle_event(
                db, args.kind, args.title, amount_yuan=args.amount, scope=args.scope, provider=args.provider
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if cc == "check":
            print(json.dumps(evaluate_lfp_calibration_and_insurance(db, notify=args.notify), ensure_ascii=False, indent=2))
            return 0
        if cc == "sync-sure":
            print(json.dumps(sync_vehicle_to_sure(db), ensure_ascii=False, indent=2))
            return 0

    # ==================== WAVE 2 ====================
    if cmd == "home-air":
        from siming.home_air_guard import (
            format_home_air_report,
            ingest_air_reading,
            predict_safe_movein_date,
            recommend_ventilation_strategy,
        )

        ac = args.air_cmd or "status"
        if ac == "status":
            print(format_home_air_report(db))
            return 0
        if ac == "ingest":
            res = ingest_air_reading(
                db,
                room=args.room,
                temp_c=args.temp,
                humidity_pct=args.humidity,
                hcho_mg_m3=args.hcho,
                voc_ppb=args.voc,
                outdoor_aqi=args.aqi,
                water_leak=args.water_leak,
                gas_leak=args.gas_leak,
                power_ok=not args.power_out,
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if ac == "strategy":
            print(json.dumps(recommend_ventilation_strategy(db), ensure_ascii=False, indent=2))
            return 0
        if ac == "predict":
            print(json.dumps(predict_safe_movein_date(db), ensure_ascii=False, indent=2))
            return 0

    if cmd == "homelab":
        from siming.homelab_ops import (
            build_three_site_backup_plan,
            format_homelab_report,
            get_modem_bridge_playbook,
            record_homelab_node,
        )

        hc = args.homelab_cmd or "status"
        if hc == "status":
            print(format_homelab_report(db))
            return 0
        if hc == "record":
            res = record_homelab_node(db, args.node_id, args.kind, args.name, args.status, detail=args.detail)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if hc == "backup-plan":
            print(json.dumps(build_three_site_backup_plan(db), ensure_ascii=False, indent=2))
            return 0
        if hc == "modem-playbook":
            print(get_modem_bridge_playbook())
            return 0

    if cmd == "print3d":
        from siming.bambu_copilot import consume_filament, format_bambu_report, update_print_job, upsert_spool

        pc = args.p3d_cmd or "status"
        if pc == "status":
            print(format_bambu_report(db))
            return 0
        if pc == "add-spool":
            res = upsert_spool(
                db,
                args.spool_id,
                material=args.material,
                color=args.color,
                initial_grams=args.grams,
                remaining_grams=args.grams,
                drybox_humidity_pct=args.humidity,
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if pc == "use":
            print(json.dumps(consume_filament(db, args.spool_id, args.grams), ensure_ascii=False, indent=2))
            return 0
        if pc == "job":
            res = update_print_job(
                db,
                args.job_id,
                args.name,
                args.status,
                progress_pct=args.progress,
                spool_id=args.spool,
                used_grams=args.grams,
                anomaly=args.anomaly,
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

    if cmd == "haos":
        from siming.haos_night_owl import format_haos_report, is_vacuum_locked, sync_night_owl_to_haos

        hc = args.haos_cmd or "status"
        if hc == "status":
            print(format_haos_report(db))
            return 0
        if hc == "sync":
            print(json.dumps(sync_night_owl_to_haos(db), ensure_ascii=False, indent=2))
            return 0
        if hc == "vacuum":
            print(json.dumps(is_vacuum_locked(db), ensure_ascii=False, indent=2))
            return 0

    if cmd == "commute":
        from siming.commute_hub import (
            check_regime_transition,
            compare_commute_modes,
            format_commute_advice,
            stage_commute_rescue_bundle,
        )

        cc = args.commute_cmd or "compare"
        if cc == "compare":
            plan = compare_commute_modes(
                db,
                ev_eta_min=args.ev_min,
                ev_soc_pct=args.soc,
                metro_eta_min=args.metro_min,
                didi_eta_min=args.didi_min,
                didi_fare_yuan=args.didi_fare,
                rain=args.rain,
                late_min=args.late,
            )
            print(format_commute_advice(plan))
            return 0
        if cc == "rescue":
            res = stage_commute_rescue_bundle(db, reason=args.reason)
            print(res.get("reply") or json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if cc == "regime":
            print(json.dumps(check_regime_transition(db), ensure_ascii=False, indent=2))
            return 0

    if cmd == "passport":
        from siming.home_passport import (
            compute_appliance_asset_valuation,
            format_home_passport_report,
            lookup_fault_code,
            upsert_consumable,
            upsert_utility,
        )

        pc = args.passport_cmd or "status"
        if pc == "status":
            print(format_home_passport_report(db))
            return 0
        if pc == "utility":
            print(json.dumps(upsert_utility(db, args.kind, next_due_at=args.due, amount_yuan=args.amount), ensure_ascii=False, indent=2))
            return 0
        if pc == "consumable":
            res = upsert_consumable(db, args.appliance_id, args.appliance_name, args.consumable_name, lifespan_days=args.days)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if pc == "fault":
            print(json.dumps(lookup_fault_code(db, args.brand, args.code), ensure_ascii=False, indent=2))
            return 0
        if pc == "assets":
            print(json.dumps(compute_appliance_asset_valuation(db), ensure_ascii=False, indent=2))
            return 0

    # ==================== WAVE 3 ====================
    if cmd == "runtime":
        from siming.multi_runtime import dispatch_via_runtime, format_runtime_status, select_runtime

        rc = args.runtime_cmd or "status"
        if rc == "status":
            print(format_runtime_status(db))
            return 0
        if rc == "select":
            print(json.dumps(select_runtime(db, task_kind=args.kind, complexity=args.complexity, preferred=args.prefer), ensure_ascii=False, indent=2))
            return 0
        if rc == "dispatch":
            res = dispatch_via_runtime(db, args.task, task_kind=args.kind, complexity=args.complexity, dry_run=True)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0

    if cmd == "wiki":
        from siming.hindsight_wiki import distill_hindsight_wiki, format_wiki_status, query_hindsight_wiki

        wc = args.wiki_cmd or "status"
        if wc == "status":
            print(format_wiki_status(db))
            return 0
        if wc == "distill":
            print(json.dumps(distill_hindsight_wiki(db), ensure_ascii=False, indent=2))
            return 0
        if wc == "query":
            print(json.dumps(query_hindsight_wiki(db, args.query), ensure_ascii=False, indent=2))
            return 0

    if cmd == "msim":
        from siming.msim_memory import (
            build_msim_transfer_prompt,
            export_msim_markdown,
            get_msim_leaderboard,
            record_msim_entry,
            search_msim_entries,
        )

        mc = args.msim_cmd or "leaderboard"
        if mc == "record":
            res = record_msim_entry(
                db,
                args.family,
                args.model,
                args.rationale,
                expected_outcome=args.expected,
                measured_score=args.score,
                baseline_score=args.baseline,
                passed=not args.failed,
                target_artifact=args.artifact,
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if mc == "search":
            print(json.dumps(search_msim_entries(db, problem_family=args.family, query=args.query), ensure_ascii=False, indent=2))
            return 0
        if mc == "inject":
            print(build_msim_transfer_prompt(db, args.family, args.target_model))
            return 0
        if mc == "leaderboard":
            print(json.dumps(get_msim_leaderboard(db), ensure_ascii=False, indent=2))
            return 0
        if mc == "export":
            p = export_msim_markdown(db)
            print(f"✅ 已导出 M-SIM 进化日记索引：{p}")
            return 0

    if cmd == "handoff":
        from siming.overnight_lab import (
            build_morning_handoff_report,
            patrol_overnight_handoffs,
            register_overnight_handoff,
        )

        hc = args.handoff_cmd or "report"
        if hc == "add":
            res = register_overnight_handoff(db, args.name, host=args.host, pane_or_pid=args.pane, log_path=args.log)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if hc == "patrol":
            print(json.dumps(patrol_overnight_handoffs(db), ensure_ascii=False, indent=2))
            return 0
        if hc == "report":
            rep = build_morning_handoff_report(db)
            print(rep.get("report_md") or rep.get("summary") or json.dumps(rep, ensure_ascii=False, indent=2))
            return 0

    if cmd == "radar":
        from siming.research_pr_radar import add_pain_point, format_radar_digest

        rc = args.radar_cmd or "digest"
        if rc == "add-pain":
            kws = [k.strip() for k in args.keywords.split(",") if k.strip()]
            print(json.dumps(add_pain_point(db, args.repo, args.topic, kws), ensure_ascii=False, indent=2))
            return 0
        if rc == "digest":
            print(format_radar_digest(db))
            return 0

    if cmd == "digest":
        from siming.digest_indie import build_weekly_digest_card, format_indie_brief, ideate_indie_game

        dc = args.digest_cmd or "weekly"
        if dc == "weekly":
            card = build_weekly_digest_card(db, limit=args.limit)
            print(card.get("reply") or json.dumps(card, ensure_ascii=False, indent=2))
            return 0
        if dc == "indie":
            mechs = [m.strip() for m in args.mechanics.split(",") if m.strip()] or None
            brief = ideate_indie_game(args.topic, mechanics=mechs)
            print(format_indie_brief(brief))
            return 0

    if cmd == "infra":
        from siming.infra_healer import (
            check_proxy_and_dns_drift,
            check_tls_cert_expiry,
            format_infra_report,
            run_infra_patrol,
            run_omarchy_upgrade_guard,
        )

        ic = args.infra_cmd or "patrol"
        if ic == "patrol":
            rep = run_infra_patrol(db, auto_heal=not args.no_heal)
            print(format_infra_report(rep))
            return 0
        if ic == "omarchy":
            print(json.dumps(run_omarchy_upgrade_guard(), ensure_ascii=False, indent=2))
            return 0
        if ic == "tls":
            print(json.dumps(check_tls_cert_expiry(host=args.host, port=args.port), ensure_ascii=False, indent=2))
            return 0
        if ic == "proxy":
            print(json.dumps(check_proxy_and_dns_drift(), ensure_ascii=False, indent=2))
            return 0

    # ==================== WAVE 4 (APPROVED: W4-1, W4-3, W4-5, W4-6) ====================
    if cmd == "graph":
        from siming.context_graph import format_graph_answer, get_graph_stats, index_context_graph, query_context_graph

        gc = args.graph_cmd or "stats"
        if gc == "index":
            print(json.dumps(index_context_graph(db), ensure_ascii=False, indent=2))
            return 0
        if gc == "query":
            res = query_context_graph(db, args.question, limit=args.limit)
            print(format_graph_answer(res))
            return 0
        if gc == "stats":
            print(json.dumps(get_graph_stats(db), ensure_ascii=False, indent=2))
            return 0

    if cmd == "trip":
        from siming.trip_packager import (
            create_trip,
            detect_trips_from_events,
            format_trip_summary,
            pack_trip_cw01_draft,
            recommend_travel_card,
        )

        tc = args.trip_cmd or "list"
        if tc == "list":
            print(format_trip_summary(db))
            return 0
        if tc == "create":
            res = create_trip(db, args.destination, args.start_date, args.end_date, country=args.country)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if tc == "detect":
            print(json.dumps(detect_trips_from_events(db), ensure_ascii=False, indent=2))
            return 0
        if tc == "card":
            print(json.dumps(recommend_travel_card(currency=args.currency, amount_foreign=args.amount), ensure_ascii=False, indent=2))
            return 0
        if tc == "pack-cw01":
            res = pack_trip_cw01_draft(db, args.trip_id)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res.get("ok") else 1

    if cmd == "social":
        from siming.social_boardgames import (
            add_commitment,
            check_cabinet_capacity,
            format_social_summary,
            get_friend_profile,
            plan_game_night,
            upsert_crowdfund,
            upsert_friend_profile,
        )

        sc = args.social_cmd or "status"
        if sc == "status":
            print(format_social_summary(db))
            return 0
        if sc == "cabinet":
            res = check_cabinet_capacity(
                db,
                current_boxes=args.boxes,
                current_volume_m3=args.vol,
                adding_boxes=args.add_boxes,
                adding_volume_m3=args.add_vol,
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if sc == "crowdfund":
            res = upsert_crowdfund(
                db,
                args.title,
                platform=args.platform,
                pledge_yuan=args.pledge,
                balance_due_yuan=args.balance,
                balance_due_at=args.due,
            )
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if sc == "gamenight":
            res = plan_game_night(db, players=args.players, duration_min=args.minutes)
            print(res.get("poll_text") or json.dumps(res, ensure_ascii=False, indent=2))
            return 0
        if sc == "commit":
            print(json.dumps(add_commitment(db, args.person, args.promise, due_at=args.due), ensure_ascii=False, indent=2))
            return 0
        if sc == "friend":
            if args.notes or args.birthday or args.diet:
                diet = [d.strip() for d in args.diet.split(",") if d.strip()] if args.diet else None
                res = upsert_friend_profile(db, args.name, notes=args.notes, birthday=args.birthday, dietary_tags=diet)
                print(json.dumps(res, ensure_ascii=False, indent=2))
            else:
                prof = get_friend_profile(db, args.name)
                print(json.dumps(prof or {"ok": False, "error": "未找到该亲友档案"}, ensure_ascii=False, indent=2))
            return 0

    if cmd == "compound":
        from siming.compound_voice import parse_compound_utterance, stage_compound_approval

        if args.stage:
            res = stage_compound_approval(db, args.utterance)
            print(res.get("reply") or res.get("card_text") or json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res.get("ok") else 1
        print(json.dumps(parse_compound_utterance(db, args.utterance), ensure_ascii=False, indent=2))
        return 0

    if cmd == "tower":
        from siming.control_tower import build_tower_snapshot, create_tower_server

        tc = args.tower_cmd or "snapshot"
        if tc == "snapshot":
            print(json.dumps(build_tower_snapshot(db), ensure_ascii=False, indent=2))
            return 0
        if tc == "serve":
            srv = create_tower_server(db, host=args.host, port=args.port, token=args.token)
            host, port = srv.server_address[:2]
            print(f"🏛️ 司命 v5 统一网页控制塔已启动：http://{host}:{port}/ （Ctrl+C 退出）", flush=True)
            try:
                srv.serve_forever()
            except KeyboardInterrupt:
                srv.shutdown()
            return 0

    return None
