"""司命功能模块开关总表（支持一句话/一条命令随时关闭或恢复任意个人功能，数据零丢失）。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

from siming.db import SimingDB, iso_now


@dataclass(frozen=True)
class ModuleSpec:
    module_id: str
    title: str
    rule_id: str
    aliases: tuple[str, ...]
    description: str
    hard_rule: bool = False


MODULE_SPECS: tuple[ModuleSpec, ...] = (
    ModuleSpec(
        module_id="price_watch",
        title="网购保价监控",
        rule_id="price_protect_watch",
        aliases=("保价", "保价监控", "京东保价", "price_protect_watch", "price-watch"),
        description="监控 15 单大家电及后续新增网购商品降价（≥¥10 提醒，不自动代点申请）",
    ),
    ModuleSpec(
        module_id="meituan_cabinet",
        title="美团外卖入柜即时提醒",
        rule_id="meituan_cabinet",
        aliases=("外卖", "外卖柜", "外卖入柜", "美团外卖柜"),
        description="识别美团/智能取餐柜取餐码与超时催取短信，即时推卡不压到 18:00",
    ),
    ModuleSpec(
        module_id="staged_bill_mail",
        title="邮箱加密账单自动暂存",
        rule_id="staged_bill_mail",
        aliases=("邮箱账单", "账单解压", "加密账单", "bill_mail"),
        description="发现 163/Gmail 支付宝/微信导出账单 ZIP 自动暂存，QQ 回「解压码 XXXXXX」即解密对账",
    ),
    ModuleSpec(
        module_id="remote_run",
        title="远端实验完工回执(srr)",
        rule_id="remote_run_done",
        aliases=("srr", "远端回执", "远端任务", "remote_run_done", "remote-hook"),
        description="接收 GPU/远端服务器 srr 零轮询退出码、耗时与脱敏末尾 5 行推送",
    ),
    ModuleSpec(
        module_id="didi_shortcut",
        title="滴滴 MCP 代叫车与电车出行",
        rule_id="didi_shortcut",
        aliases=("滴滴", "打车", "打车唤端", "didi"),
        description="滴滴官方 MCP 实时寻址估价、apply 确认代发单与纯电车出行提示",
    ),
    ModuleSpec(
        module_id="meituan_agent",
        title="私人点餐员(美团H5+瑞幸MCP)",
        rule_id="meituan_agent",
        aliases=("美团", "点餐", "吃什么", "美团推荐", "瑞幸", "food_butler", "meituan"),
        description="真实口味36h防腻推荐、9222美团H5加购锁单与瑞幸官方MCP真点单",
    ),
    ModuleSpec(
        module_id="night_owl",
        title="夜猫子动态作息注意力引擎",
        rule_id="night_owl",
        aliases=("作息", "夜猫子", "睡眠免打扰", "night_owl"),
        description="融合桌面活跃与睡眠信号：凌晨写代码即时达，上午补觉自动暂存并延后晨札",
    ),
    ModuleSpec(
        module_id="self_heal_ops",
        title="API付款锁与研发链路自愈",
        rule_id="nous_lock",
        aliases=("自愈", "链路诊断", "nous锁", "nous", "nous_lock", "self_heal_ops"),
        description="守护 API 付款锁/AUTO REFILL 断档，合成内网VPN/9222/远端主机唯一根因并自动清 DNS 负缓存",
    ),
    ModuleSpec(
        module_id="gpu_dev_twin",
        title="Agent卡住提醒与十秒续工",
        rule_id="gpu_dev_twin",
        aliases=("续工", "十秒续工", "卡住提醒", "dev_twin", "gpu_dev_twin"),
        description="本机 herdr Agent 卡权限满 5 分钟主动提醒，跨项目发「续 <名>」秒出续工卡",
    ),
    ModuleSpec(
        module_id="movein_pm",
        title="新房入住总控与家电售后护照",
        rule_id="movein_pm",
        aliases=("入住总控", "售后护照", "装修留证", "工序冲突", "movein_pm"),
        description="核查燃气/橱柜/吊顶安装冲突、隐蔽工程照片按房间建档与家电售后护照",
    ),
    ModuleSpec(
        module_id="quiet_cfo",
        title="安静账房(差旅组包+回款+查账)",
        rule_id="quiet_cfo",
        aliases=("安静账房", "查账", "待回款", "差旅组包", "cw01组包", "quiet_cfo"),
        description="差旅打车票自动配对打包、报销/保价到账短信自动销账与一句话 SQL 查账",
    ),
    ModuleSpec(
        module_id="replenish_cart",
        title="常买消耗品周期与京东代加购",
        rule_id="replenish_cart",
        aliases=("常买", "常买补货", "加购物车", "京东补货", "replenish_cart"),
        description="推算消耗周期并通过 9222 后台代填京东跨端同步购物车，手机一点即付",
    ),
    ModuleSpec(
        module_id="install_plan",
        title="新房家电到货初验与送装清单",
        rule_id="install_plan",
        aliases=("送装", "送装清单", "到货初验", "家电安装"),
        description="到货开箱初验提醒、现场分组作战单与安装预约缺口核对",
    ),
    ModuleSpec(
        module_id="reimburse_cursor",
        title="报销游标与草稿拼包",
        rule_id="reimburse_cursor",
        aliases=("报销", "报销游标", "nous报销"),
        description="还款入账静默刷新 Nous 起点游标、双号防重与草稿拼包",
    ),
    ModuleSpec(
        module_id="pickup_1800",
        title="18:00 回家顺路快递合并",
        rule_id="pickup_1800",
        aliases=("取件", "快递", "菜鸟", "顺路取件"),
        description="白天静默归集普通快递取件码，18:00 按驿站合并推一张卡",
    ),
    ModuleSpec(
        module_id="bank_sms_alert",
        title="四卡动账静默入账与超大额哨兵",
        rule_id="bank_sms_alert",
        aliases=("动账", "银行短信", "消费提醒"),
        description="日常流水静默归入晨札整合摘要，仅超大额（≥¥20,000 / $2,000）实时提醒",
    ),
    ModuleSpec(
        module_id="sub_renew",
        title="订阅续费预警",
        rule_id="sub_renew",
        aliases=("订阅", "自动续费", "续期预警"),
        description="邮箱与流水自动续期扣费提前提醒",
    ),
    ModuleSpec(
        module_id="task_watch",
        title="本机长任务与 herdr 面板提醒",
        rule_id="task_run_done",
        aliases=("长任务", "herdr", "herdr_watch_alert"),
        description="siming run 与 siming watch 完工/阻塞单次通知",
    ),
    ModuleSpec(
        module_id="commute_weather",
        title="出门前带伞/穿衣提醒",
        rule_id="commute_weather",
        aliases=("出门提醒", "带伞", "天气提醒", "穿衣提醒", "commute_weather"),
        description="工作日出门前查家楼下逐小时预报：上下班有雨提醒带伞、气温骤变给穿衣建议，无事不扰",
    ),
    ModuleSpec(
        module_id="med_care",
        title="周期用药/注射与复诊开药提醒",
        rule_id="med_care",
        aliases=("打针", "打针提醒", "周期用药", "开药", "挂号开药", "med_care"),
        description="按 policy.yaml 配置的周期用药/注射提醒，回「打了/已服」扣余量，余量低时提醒挂号开药",
    ),
    ModuleSpec(
        module_id="supply_radar",
        title="常备耗材补货雷达",
        rule_id="supply_radar",
        aliases=("补货雷达", "耗材", "耗材补货", "口罩补货", "supply_radar"),
        description="采集外卖/电商订单商品明细推算常备耗材余量，快用完时提醒补货",
    ),
    ModuleSpec(
        module_id="cognitive_drop",
        title="Super-agent 投递分拣与收藏夹(F01/F03)",
        rule_id="cognitive_drop",
        aliases=("投递分拣", "收藏夹", "bookmark_hub", "cognitive_drop"),
        description="轻点背面截屏与备忘多级分拣：GitHub收藏夹、在途冷链包裹、改约重排、补账与误触忽略",
    ),
    ModuleSpec(
        module_id="event_chain",
        title="六层事件链与单字自然确认中枢(F02/F11)",
        rule_id="event_chain_replan",
        aliases=("事件链", "规律引擎", "deco_pilot", "event_chain"),
        description="routines.md 规律库、送装硬依赖级联重排、月度义务链与『确认/发吧』自然拍板",
    ),
    ModuleSpec(
        module_id="spatial_pickup",
        title="OwnTracks 真下班离司取件与冷链加急(F04)",
        rule_id="spatial_pickup",
        aliases=("离司取件", "冷链提醒", "下班取件", "spatial_pickup"),
        description="工作日离开公司时合并推送待取包裹，冷冻/生鲜包裹到站立即高优防化冻提醒",
    ),
    ModuleSpec(
        module_id="overtime_meal",
        title="加班晚餐 A静默秒回+B轻推锁车双模(F05)",
        rule_id="overtime_meal",
        aliases=("加班餐", "加班晚餐", "overtime_meal"),
        description="19:35在司静默预备选品供0秒秒回，20:05未点餐轻推1主1备一键锁美团购物车",
    ),
    ModuleSpec(
        module_id="outing_amap_ev",
        title="特斯拉电量续航与高德实时ETA重排(F07)",
        rule_id="tesla_low_soc",
        aliases=("特斯拉", "高德重排", "tesla", "outing_amap_ev"),
        description="特斯拉低电量守卫、往返里程续航精算与高德实时路况延误捆绑对策卡",
    ),
    ModuleSpec(
        module_id="tesla_auto_climate",
        title="出门意图联动自动备车开空调(EC-5)",
        rule_id="tesla_auto_climate",
        aliases=("自动开空调", "自动备车", "备车空调", "tesla_auto_climate"),
        description="手机同步出门计划+检测到离家/出门+判断开车+车内过冷/过热+电量充足时自动开空调（含18m未发车自动关停）",
    ),
    ModuleSpec(
        module_id="ledger_enrich",
        title="抖音订单采集与¥9.90小说先验补盲(F08/F09)",
        rule_id="ledger_enrich",
        aliases=("账本补盲", "抖音订单", "drop_order_track", "ledger_enrich"),
        description="从历史流水自动推断¥9.90抖音小说等先验，结合9222与截屏补齐抖音物流与商户",
    ),
    ModuleSpec(
        module_id="night_lab_pilot",
        title="睡前实验托管与新房HAOS智能桥(F10/F14/F15)",
        rule_id="night_lab_pilot",
        aliases=("睡前托管", "新房智能", "smart_home_ha", "session_keeper", "night_lab_pilot"),
        description="凌晨睡前实验静默看护随晨札报战果、9222登录态巡检与春节新房HAOS联动",
    ),
    # ==================== v5 Wave 1 (7) ====================
    ModuleSpec(
        module_id="sure_finance_os",
        title="Sure 自托管财务 OS 控制塔(W1-1)",
        rule_id="sure_finance_os",
        aliases=("sure", "财务控制塔", "财务os", "sure_finance_os"),
        description="Sure 双向同步四卡流水/公私报销标签与 Pipelock+MCP 1秒查账",
    ),
    ModuleSpec(
        module_id="compute_assets",
        title="多平台 AI 算力资产总控与烧爆熔断(W1-2)",
        rule_id="compute_assets",
        aliases=("算力资产", "算力总控", "ai算力", "compute_assets"),
        description="监控 DeepSeek/阿里云/Kimi/Nous/RedPill 余额、到期日、2x 烧爆熔断与 404 切备",
    ),
    ModuleSpec(
        module_id="sub_assassin",
        title="全渠道订阅刺客猎手(W1-3)",
        rule_id="sub_assassin",
        aliases=("订阅刺客", "僵尸订阅", "连续包月", "止损卡", "订阅止损卡", "sub_assassin"),
        description="审计周期扣费、识别涨价与低频会员并在扣费前 48h 推 QQ 止损按钮卡",
    ),
    ModuleSpec(
        module_id="meal_combo_guard",
        title="防忘吃饭守卫与跨店双拼点餐(W1-4)",
        rule_id="meal_combo_guard",
        aliases=("防忘吃饭", "双拼点餐", "饮食守卫", "meal_combo_guard"),
        description="错过餐点温和提醒，支持跨店双拼算时差/运费/拆单神券并出 A2 确认卡",
    ),
    ModuleSpec(
        module_id="parcel_sentinel",
        title="丰巢 18h 倒计时与 7 天退货哨兵(W1-5)",
        rule_id="parcel_sentinel",
        aliases=("丰巢倒计时", "退货哨兵", "畅存费", "parcel_sentinel"),
        description="丰巢免费期剩 ≤2h 且在家时破静提醒，抖音/拼多多/京东签收后守 7 天退货窗",
    ),
    ModuleSpec(
        module_id="health_cockpit",
        title="私人健康驾驶舱与复诊摘要(W1-6)",
        rule_id="health_cockpit",
        aliases=("健康驾驶舱", "化验单", "复诊摘要", "health_cockpit"),
        description="离线 OCR 提取化验单指标追踪趋势、关联周期用药与睡眠债并生成 1 页医生简报",
    ),
    ModuleSpec(
        module_id="vehicle_cfo",
        title="电车车务账房与 LFP 满充校准(W1-7)",
        rule_id="vehicle_cfo",
        aliases=("车务", "车务账房", "lfp校准", "vehicle_cfo"),
        description="汇总 ETC/停车/超充同步 Sure 车辆账户、守 LFP 每周满充校准与车险/违章提醒",
    ),
    # ==================== v5 Wave 2 (6) ====================
    ModuleSpec(
        module_id="home_air_guard",
        title="新房除醛远程新风与 L3 安防(W2-1)",
        rule_id="home_air_guard",
        aliases=("放甲醛", "新房空气", "新风自治", "home_air_guard"),
        description="按室外 AQI 编排闷放/强排新风、拟合甲醛达标入住日并守浸水/燃气/断电 L3 告警",
    ),
    ModuleSpec(
        module_id="homelab_ops",
        title="家庭路由 / NAS 与三地加密灾备(W2-2)",
        rule_id="homelab_ops",
        aliases=("家庭实验室", "nas", "路由巡检", "三地灾备", "homelab_ops"),
        description="监控路由/光猫桥接/NAS SMART 与 Sure 容器健康并编排桌面→NAS→VPS 三地灾备",
    ),
    ModuleSpec(
        module_id="bambu_copilot",
        title="Bambu 3D 打印副驾与耗材库(W2-3)",
        rule_id="bambu_copilot",
        aliases=("拓竹", "3d打印", "bambu", "bambu_copilot"),
        description="监控打印完工带图回执、断料/炒面暂停告警与 PLA/PETG 余量低阈值 A2 补货",
    ),
    ModuleSpec(
        module_id="haos_night_owl",
        title="懂夜猫子的 HAOS 全屋智能联动(W2-4)",
        rule_id="haos_night_owl",
        aliases=("全屋智能", "扫地机锁", "haos夜猫子", "haos_night_owl"),
        description="归家 T-15m 预开空调与零冷水、凌晨书房暖光、上午补觉硬锁扫地机",
    ),
    ModuleSpec(
        module_id="commute_hub",
        title="多模态通勤决策中枢(W2-5)",
        rule_id="commute_hub",
        aliases=("通勤中枢", "多模态通勤", "commute_hub"),
        description="对比自驾/地铁/打车最优通勤、极端天气起晚一键应急捆绑卡与搬家规律重学",
    ),
    ModuleSpec(
        module_id="home_passport",
        title="住宅水电物业与家电全周期护照(W2-6)",
        rule_id="home_passport",
        aliases=("家电护照", "住宅护照", "故障码", "home_passport"),
        description="水电物业账期提醒、滤芯/尘袋寿命追踪、故障码秒查保修与固定资产折旧同步 Sure",
    ),
    # ==================== v5 Wave 3 (7) ====================
    ModuleSpec(
        module_id="multi_runtime",
        title="Linger/Pi 多 Runtime 双轨调度(W3-1)",
        rule_id="multi_runtime",
        aliases=("多runtime", "linger", "pi路由", "multi_runtime"),
        description="重度编码走 Claude/Codex 订阅零 API 费，轻量与自备 API 走 Pi Runtime 动态容错",
    ),
    ModuleSpec(
        module_id="hindsight_wiki",
        title="Hindsight 夜间自动共享 Wiki(W3-2)",
        rule_id="hindsight_wiki",
        aliases=("共享wiki", "hindsight", "wiki蒸馏", "hindsight_wiki"),
        description="每晚扫描全机 Agent 会话蒸馏偏好/拍板/禁忌写入 Obsidian 共享 Wiki 并检冲突",
    ),
    ModuleSpec(
        module_id="msim_memory",
        title="M-SIM 跨模型自进化日记(W3-3)",
        rule_id="msim_memory",
        aliases=("进化日记", "msim", "跨模型记忆", "msim_memory"),
        description="沉淀 Harness/Skill 调优得失并跨 Claude/Gemini/Codex/DeepSeek 遗传注入",
    ),
    ModuleSpec(
        module_id="overnight_lab",
        title="睡前 GPU/Agent 异步实验交班台(W3-4)",
        rule_id="overnight_lab",
        aliases=("睡前交班", "实验交班", "overnight_lab"),
        description="半夜静默监测 GPU 掉卡/Loss NaN/卡权限按白名单存档续跑，自然醒出对比战报",
    ),
    ModuleSpec(
        module_id="research_pr_radar",
        title="问题驱动科研与 123 PR 雷达(W3-5)",
        rule_id="research_pr_radar",
        aliases=("科研雷达", "pr雷达", "research_pr_radar"),
        description="按仓库痛点匹配论文附 15m worktree 验证命令，过滤 action_required 噪音只留高信噪 PR",
    ),
    ModuleSpec(
        module_id="digest_indie",
        title="收藏 Top-3 消化与创意工坊(W3-6)",
        rule_id="digest_indie",
        aliases=("收藏消化", "独游工坊", "digest_indie"),
        description="周日精选 Top-3 收藏附隔离试跑命令，联动本地知识库做玩法机制与竞品对标",
    ),
    ModuleSpec(
        module_id="infra_healer",
        title="系统升级 / TLS 证书 / 代理自愈运维官(W3-7)",
        rule_id="infra_healer",
        aliases=("基建自愈", "omarchy守卫", "证书巡检", "infra_healer"),
        description="系统升级前守卫、自定义域名 TLS 证书监测与 VPN/sing-box 白名单静默自愈",
    ),
    # ==================== v5 Wave 4 (4 APPROVED; W4-2 & W4-4 DEFERRED) ====================
    ModuleSpec(
        module_id="context_graph",
        title="Linger 式跨应用上下文联邦图谱(W4-1)",
        rule_id="context_graph",
        aliases=("上下文图谱", "联邦检索", "context_graph"),
        description="打通 Files/Mail/Calendar/Chat/Wiki/Sure/M-SIM 只读联邦图谱，一句话带出处穿透查",
    ),
    ModuleSpec(
        module_id="trip_packager",
        title="差旅与出境游全周期行程包(W4-3)",
        rule_id="trip_packager",
        aliases=("行程包", "差旅", "出境游", "trip_packager"),
        description="自动建出境/差旅清单、算境外最优返现卡，返程一键打包报销草稿并打 Sure 行程标签",
    ),
    ModuleSpec(
        module_id="social_boardgames",
        title="桌游藏品社交与加密人情记忆(W4-5)",
        rule_id="social_boardgames",
        aliases=("桌游社交", "众筹", "人情记忆", "social_boardgames"),
        description="桌游柜容/众筹补款追踪、攒局投票与 3 分钟讲规卡、承诺捕手与加密亲友档案",
    ),
    ModuleSpec(
        module_id="compound_tower",
        title="QQ 语音复合流与统一网页控制塔(W4-6)",
        rule_id="compound_tower",
        aliases=("语音复合", "网页控制塔", "控制塔", "compound_tower"),
        description="口语多意图合一 A2 按钮卡 + 受密保护的统一网页控制塔（联邦 Sure/Wiki/M-SIM/自省）",
    ),
)


def resolve_module_spec(key: str) -> Optional[ModuleSpec]:
    import re

    q = (key or "").strip().lower()
    if not q:
        return None
    # 兼容口语后缀与连接词（如「保价监控提醒」「订阅刺客功能」「自动备车模块」「止损卡」）
    q_clean = re.sub(r"^(?:一下|那个|这个)\s*", "", q).strip()
    q_stripped = re.sub(r"(?:功能开关|模块|功能|提醒|推送|通知|卡片)$", "", q_clean).strip()
    candidates = [c for c in dict.fromkeys((q, q_clean, q_stripped)) if c]

    for cand in candidates:
        for spec in MODULE_SPECS:
            if cand in (spec.module_id.lower(), spec.rule_id.lower(), spec.title.lower()):
                return spec
            if any(cand == a.lower() for a in spec.aliases):
                return spec
    for cand in candidates:
        if len(cand) < 2:
            continue
        for spec in MODULE_SPECS:
            if cand in spec.title.lower() or any(cand in a.lower() or a.lower() in cand for a in spec.aliases if len(a) >= 2):
                return spec
    return None


# D-35(D1-A)：5 个依赖未来新房/尚未到位硬件的能力，默认 readiness='unconfigured'（停周期 tick、保留代码与手工查询，硬件与首样本到位自动进 shadow -> live）
UNARRIVED_HARDWARE_MODULES: frozenset[str] = frozenset(
    {
        "home_air_guard",
        "homelab_ops",
        "bambu_copilot",
        "haos_night_owl",
        "commute_hub",
    }
)

VALID_USER_INTENTS: frozenset[str] = frozenset({"enabled", "disabled"})
VALID_READINESS: frozenset[str] = frozenset({"unconfigured", "shadow", "live", "degraded", "parked"})
VALID_NOTIFY_POLICIES: frozenset[str] = frozenset({"immediate", "digest", "muted"})


def get_capability_state(db: SimingDB, module_or_rule_id: str) -> dict[str, Any]:
    """D-35(D1-A) 三维能力生命周期查询：user_intent × readiness × notify_policy。"""
    spec = resolve_module_spec(module_or_rule_id)
    mod_id = spec.module_id if spec else module_or_rule_id
    rule_id = spec.rule_id if spec else module_or_rule_id
    default_readiness = "unconfigured" if mod_id in UNARRIVED_HARDWARE_MODULES else "live"

    explicit_off = db.get_meta(f"module_disabled:{mod_id}")
    conn = db.connect()
    try:
        cap_row = conn.execute("SELECT * FROM capability_states WHERE module_id = ?", (mod_id,)).fetchone()
        rs_row = conn.execute("SELECT mode FROM rule_stats WHERE rule_id = ?", (rule_id,)).fetchone()
    finally:
        conn.close()

    if cap_row:
        user_intent = str(cap_row["user_intent"] or "enabled")
        readiness = str(cap_row["readiness"] or default_readiness)
        notify_policy = str(cap_row["notify_policy"] or "immediate")
        verified_runs = int(cap_row["verified_runs"] or 0)
        detail = str(cap_row["detail"] or "")
    else:
        user_intent = "disabled" if explicit_off == "1" else "enabled"
        readiness = default_readiness
        notify_policy = "immediate"
        verified_runs = 0
        detail = "未到位硬件默认休眠（保留代码与手动查询，设备就绪后自动升级 shadow→live）" if default_readiness == "unconfigured" else ""

    if explicit_off == "1":
        user_intent = "disabled"
    if rs_row and rs_row["mode"] == "QUIET" and notify_policy == "immediate":
        notify_policy = "digest"
    if rs_row and rs_row["mode"] in ("DEMOTED", "OBSERVE", "FROZEN") and explicit_off != "0":
        user_intent = "disabled"

    tick_active = (user_intent == "enabled") and (readiness in ("shadow", "live", "degraded"))
    return {
        "module_id": mod_id,
        "rule_id": rule_id,
        "user_intent": user_intent,
        "readiness": readiness,
        "notify_policy": notify_policy,
        "tick_active": tick_active,
        "requires_hardware": mod_id in UNARRIVED_HARDWARE_MODULES,
        "verified_runs": verified_runs,
        "detail": detail,
    }


def set_capability_state(
    db: SimingDB,
    key: str,
    *,
    user_intent: Optional[str] = None,
    readiness: Optional[str] = None,
    notify_policy: Optional[str] = None,
    detail: Optional[str] = None,
) -> dict[str, Any]:
    """更新指定能力的三维生命周期状态（user_intent / readiness / notify_policy）。"""
    db.init_db()
    spec = resolve_module_spec(key)
    mod_id = spec.module_id if spec else key.strip()
    cur = get_capability_state(db, mod_id)

    ni = (user_intent or cur["user_intent"]).strip().lower()
    nr = (readiness or cur["readiness"]).strip().lower()
    np = (notify_policy or cur["notify_policy"]).strip().lower()
    if ni not in VALID_USER_INTENTS:
        raise ValueError(f"invalid user_intent: {ni}")
    if nr not in VALID_READINESS:
        raise ValueError(f"invalid readiness: {nr}")
    if np not in VALID_NOTIFY_POLICIES:
        raise ValueError(f"invalid notify_policy: {np}")

    ts = iso_now()
    det = cur["detail"] if detail is None else detail
    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO capability_states (
                module_id, user_intent, readiness, notify_policy, verified_runs, detail, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(module_id) DO UPDATE SET
                user_intent = excluded.user_intent,
                readiness = excluded.readiness,
                notify_policy = excluded.notify_policy,
                detail = excluded.detail,
                updated_at = excluded.updated_at
            """,
            (mod_id, ni, nr, np, int(cur["verified_runs"]), det, ts),
        )
        if user_intent is not None:
            db.set_meta(f"module_disabled:{mod_id}", "0" if ni == "enabled" else "1", conn=conn)
    return get_capability_state(db, mod_id)


def promote_capability_on_signal(
    db: SimingDB,
    module_id: str,
    *,
    has_probe: bool = False,
    has_sample: bool = False,
    verified_outcome: bool = False,
    detail: str = "",
) -> dict[str, Any]:
    """硬件/数据源信号到达时自动推进就绪度：unconfigured -> shadow（探针+首样本齐备）-> live（有 1 次可核验结果）。"""
    db.init_db()
    cur = get_capability_state(db, module_id)
    ts = iso_now()
    readiness = cur["readiness"]
    verified_runs = int(cur["verified_runs"])
    if verified_outcome:
        verified_runs += 1
        if readiness in ("unconfigured", "shadow", "degraded"):
            readiness = "live"
    elif has_probe and has_sample and readiness == "unconfigured":
        readiness = "shadow"

    with db.transaction() as conn:
        conn.execute(
            """
            INSERT INTO capability_states (
                module_id, user_intent, readiness, notify_policy,
                last_probe_at, last_sample_at, verified_runs, detail, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(module_id) DO UPDATE SET
                readiness = excluded.readiness,
                last_probe_at = COALESCE(excluded.last_probe_at, capability_states.last_probe_at),
                last_sample_at = COALESCE(excluded.last_sample_at, capability_states.last_sample_at),
                verified_runs = excluded.verified_runs,
                detail = CASE WHEN excluded.detail != '' THEN excluded.detail ELSE capability_states.detail END,
                updated_at = excluded.updated_at
            """,
            (
                cur["module_id"],
                cur["user_intent"],
                readiness,
                cur["notify_policy"],
                ts if has_probe else None,
                ts if has_sample else None,
                verified_runs,
                detail or cur["detail"],
                ts,
            ),
        )
    return get_capability_state(db, cur["module_id"])


def is_module_tick_active(db: SimingDB, module_or_rule_id: str) -> bool:
    """周期 tick 守卫专用判断：未到位硬件（readiness=unconfigured）或 parked / disabled 均不跑周期轮询，彻底降噪。"""
    st = get_capability_state(db, module_or_rule_id)
    return bool(st["tick_active"])


def is_module_enabled(db: SimingDB, module_or_rule_id: str) -> bool:
    """检查某模块当前是否启用（保留用户意愿与手工查询；周期轮询请优先调用 is_module_tick_active）。"""
    spec = resolve_module_spec(module_or_rule_id)
    rule_id = spec.rule_id if spec else module_or_rule_id
    mod_id = spec.module_id if spec else module_or_rule_id

    explicit_off = db.get_meta(f"module_disabled:{mod_id}")
    if explicit_off == "1":
        return False

    conn = db.connect()
    try:
        row = conn.execute("SELECT mode, hard_rule FROM rule_stats WHERE rule_id = ?", (rule_id,)).fetchone()
        if not row:
            return True
        if row["mode"] in ("DEMOTED", "OBSERVE", "FROZEN") and explicit_off != "0":
            return False
        return True
    finally:
        conn.close()


def set_module_enabled(db: SimingDB, key: str, enabled: bool) -> dict[str, Any]:
    """一键开启或关闭指定个人功能模块（保留全部历史数据与手动查询能力）。"""
    db.init_db()
    spec = resolve_module_spec(key)
    if spec is None:
        avail = "、".join(f"{m.module_id}({m.title})" for m in MODULE_SPECS)
        return {"ok": False, "error": f"未找到模块「{key}」。可选模块：{avail}"}

    ts = iso_now()
    new_mode = "ACTIVE" if enabled else "DEMOTED"
    with db.transaction() as conn:
        db.set_meta(f"module_disabled:{spec.module_id}", "0" if enabled else "1", conn=conn)
        conn.execute(
            """
            INSERT INTO rule_stats (rule_id, mode, hard_rule, consecutive_dismiss, updated_at)
            VALUES (?, ?, ?, 0, ?)
            ON CONFLICT(rule_id) DO UPDATE SET
                mode = excluded.mode,
                consecutive_dismiss = CASE WHEN excluded.mode = 'ACTIVE' THEN 0 ELSE rule_stats.consecutive_dismiss END,
                updated_at = excluded.updated_at
            """,
            (spec.rule_id, new_mode, 1 if spec.hard_rule else 0, ts),
        )
        # 若关闭 task_watch，同步关闭 herdr_watch_alert
        if spec.module_id == "task_watch":
            conn.execute(
                """
                INSERT INTO rule_stats (rule_id, mode, hard_rule, consecutive_dismiss, updated_at)
                VALUES ('herdr_watch_alert', ?, 0, 0, ?)
                ON CONFLICT(rule_id) DO UPDATE SET mode = excluded.mode, updated_at = excluded.updated_at
                """,
                (new_mode, ts),
            )
        db.write_audit(
            "A1",
            "module_toggle",
            {"module_id": spec.module_id, "rule_id": spec.rule_id, "enabled": enabled},
            target_id=spec.module_id,
            conn=conn,
        )

    state_label = "已开启" if enabled else "已关闭后台自动触发与推送（历史数据与手动查询仍保留）"
    return {
        "ok": True,
        "module_id": spec.module_id,
        "title": spec.title,
        "rule_id": spec.rule_id,
        "enabled": enabled,
        "summary": f"🎛️ 模块 `{spec.module_id}`（{spec.title}）{state_label}。",
    }


def list_modules_status(db: SimingDB) -> list[dict[str, Any]]:
    db.init_db()
    conn = db.connect()
    try:
        stats_map = {
            str(r["rule_id"]): dict(r)
            for r in conn.execute("SELECT * FROM rule_stats").fetchall()
        }
        use_map = {
            str(r["feature_id"]): str(r["last_ts"] or "")
            for r in conn.execute("SELECT feature_id, MAX(ts) AS last_ts FROM feature_usage WHERE actor='human' GROUP BY feature_id").fetchall()
        }
    finally:
        conn.close()

    out: list[dict[str, Any]] = []
    for spec in MODULE_SPECS:
        enabled = is_module_enabled(db, spec.module_id)
        cap = get_capability_state(db, spec.module_id)
        st = stats_map.get(spec.rule_id) or {}
        last_used = use_map.get(spec.rule_id) or use_map.get(spec.module_id) or ""
        out.append(
            {
                "module_id": spec.module_id,
                "title": spec.title,
                "rule_id": spec.rule_id,
                "enabled": enabled,
                "user_intent": cap["user_intent"],
                "readiness": cap["readiness"],
                "notify_policy": cap["notify_policy"],
                "tick_active": cap["tick_active"],
                "mode": str(st.get("mode") or ("ACTIVE" if enabled else "DEMOTED")),
                "sent_14d": int(st.get("sent_14d") or 0),
                "last_used_at": last_used[:16].replace("T", " ") if last_used else "-",
                "aliases": list(spec.aliases[:2]),
                "description": spec.description,
            }
        )
    return out


def format_modules_report(db: SimingDB) -> str:
    rows = list_modules_status(db)
    lines = ["🎛️ 司命个人功能模块开关（随时发 `关闭 <模块名>` 撤回，或 `开启 <模块名>` 恢复）"]
    for r in rows:
        badge = "🟢 开启" if r["enabled"] else "⚪ 已关"
        lines.append(
            f"• {badge} `{r['module_id']}` {r['title']}（14d推送 {r['sent_14d']} · 最近使用 {r['last_used_at']}）— {r['description']}"
        )
    return "\n".join(lines)
