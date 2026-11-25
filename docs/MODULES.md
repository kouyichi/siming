# Siming 功能模块全景目录（Modules）

Siming 内置 **53 个可独立开关的功能模块**（定义于 `src/siming/modules.py`）。你可以随时按需关闭任何用不到的垂直模块——关闭后后台自动触发与推送立即停跑，所有历史数据与手动 CLI 查询保持完整可用。

```bash
siming module ls                # 查看全部模块状态与 14 天推送统计
siming module off <module_id>   # 关闭模块
siming module on <module_id>    # 开启模块
```

---

## 1. 核心调度、跨 Agent 与研发运维

| 模块 ID | 名称 | 核心能力 | 主要入口 |
| :--- | :--- | :--- | :--- |
| `night_owl` | 夜猫子动态作息注意力引擎 | 融合桌面活跃与手机睡眠信号动态放行/暂扣推送 | `siming attention` |
| `task_watch` | 本机长任务与 herdr 面板监听 | 零轮询包装执行长命令与监听 Agent 面板阻塞 | `siming run` / `siming watch` |
| `remote_run` | 远端实验完工回执 (`srr`) | 受限子令牌接收远端 GPU/计算服务器退出码与脱敏日志 | `siming remote-hook` / `srr` |
| `gpu_dev_twin` | Agent 卡住提醒与十秒续工 | 检测 herdr 面板卡权限并秒出跨项目 Git+会话续工卡 | `siming resume` |
| `self_heal_ops` | API 付款锁与研发链路自愈 | 守护 API 自动充值锁、合成 VPN/CDP/远端唯一根因与清 DNS 负缓存 | `siming ops-check` |
| `multi_runtime` | 多 Runtime 双轨调度 | 按任务复杂度动态在订阅制编码 Agent 与 API Runtime 间路由 | `siming runtime` |
| `overnight_lab` | 睡前 GPU/Agent 异步交班台 | 夜间静默巡检训练/评测状态，自然醒随晨札汇报对比战报 | `siming handoff` |
| `research_pr_radar` | 问题驱动科研与 PR 雷达 | 过滤 CI 噪音，高信噪比分诊 Open PR 与匹配仓库痛点论文 | `siming radar` |
| `infra_healer` | 系统升级 / TLS / 代理运维官 | 升级前包管理守卫、自定义域名 TLS 到期巡检与白名单自愈 | `siming infra` |
| `night_lab_pilot` | 睡前托管与 HAOS 智能桥 | 夜间任务看护、CDP 登录态巡检与 Home Assistant 状态桥接 | `siming haos` |

---

## 2. 财务、账本、订阅与算力治理

| 模块 ID | 名称 | 核心能力 | 主要入口 |
| :--- | :--- | :--- | :--- |
| `bank_sms_alert` | 多卡动账静默入账与大额哨兵 | 日常动账静默入晨札，仅超阈值大额交易实时提醒 | `siming ledger report` |
| `staged_bill_mail` | 邮箱加密账单自动暂存 | 自动识别邮件中的微信/支付宝加密账单包，回解压码即内存解密对账 | `siming ledger import-zip` |
| `reimburse_cursor` | 报销游标与防重草稿拼包 | 还款入账刷新报销起点、双号/时间窗防重审计与草稿打包 | `siming ledger cursor` / `audit-batch` |
| `quiet_cfo` | 安静账房 | 差旅票据配对打包、待回款短信自动销账与零模型 SQL 查账 | `siming ledger ask` / `receivables` |
| `sure_finance_os` | Sure 自托管财务 OS 控制塔 | 幂等双向同步流水、公私标签与 Pipelock 权限策略 | `siming sure` / `tools/sure_up.sh` |
| `compute_assets` | 多平台 AI 算力资产总控 | 监控多平台余额/到期日、2x 烧爆熔断与 404 自动切备 | `siming compute` |
| `sub_renew` | 订阅续费预警 | 邮箱与流水自动续费扣费提前提醒 | `siming subs upcoming` |
| `sub_assassin` | 全渠道订阅刺客猎手 | 审计周期扣费、涨价与低频会员（支持止损标记与一键关闭） | `siming subs audit` |

---

## 3. 日常生活、消费、物流与健康照护

| 模块 ID | 名称 | 核心能力 | 主要入口 |
| :--- | :--- | :--- | :--- |
| `commute_weather` | 出门前带伞 / 穿衣提醒 | 基于 Open-Meteo 逐小时预报在上下班雨雪/骤冷前提醒 | `siming care status` |
| `med_care` | 周期用药 / 注射与复诊开药 | 按 `policy.yaml` 周期提醒、扣余量与倒推医院挂号时间 | `siming care inject/pen/refill` |
| `supply_radar` | 常备耗材补货雷达 | 从外卖/电商订单推算口罩/日常耗材余量并提前提醒 | `siming care stock` |
| `health_cockpit` | 私人健康驾驶舱与复诊摘要 | 离线 OCR 提取化验单趋势并生成 1 页结构化就诊简报 | `siming health` |
| `price_watch` | 网购保价监控 | 跟踪已购商品价格变动，降价达阈值提醒（不自动代点申请） | `siming price-watch` |
| `replenish_cart` | 常买消耗品周期与京东代加购 | 周期补货预测与 9222 跨端同步加购物车（需 A2 确认） | `siming replenish` |
| `meituan_cabinet` | 外卖入柜即时提醒 | 识别外卖取餐柜取餐码与超时催取短信，高优即时推送 | 自动触发 |
| `pickup_1800` | 18:00 回家顺路快递合并 | 白天静默归集快递取件码，下班按驿站/住址分层合并推送 | 自动触发 |
| `spatial_pickup` | 离司真下班取件与冷链加急 | 结合 OwnTracks 离开公司触发取件清单，冷链生鲜到站立即告警 | 自动触发 |
| `parcel_sentinel` | 丰巢倒计时与 7 天退货哨兵 | 免费期将满且在家时提醒取件，签收后守护 7 天无理由退货窗 | `siming sentinel` |
| `meituan_agent` | 私人点餐员（美团 H5 + 瑞幸 MCP） | 口味防腻推荐、结算预览加购锁单（A2 确认）与瑞幸官方 MCP 点单 | `siming meituan` / `siming luckin` |
| `meal_combo_guard` | 防忘吃饭守卫与跨店双拼点餐 | 错过餐点温和提醒，支持两店双拼核算运费/时差与优惠券 | `siming meal` |
| `overtime_meal` | 加班晚餐双模助手 | 晚间在司静默预备选品，超时未点餐轻推一键确认卡 | 自动触发 |
| `ledger_enrich` | 电商订单采集与小额先验补盲 | 结合网页订单与历史贝叶斯先验自动补全无商户银行流水 | 自动触发 |

---

## 4. 出行、电车、智能家居与 Homelab

| 模块 ID | 名称 | 核心能力 | 主要入口 |
| :--- | :--- | :--- | :--- |
| `didi_shortcut` | 滴滴 MCP 代叫车与出行提示 | 滴滴官方 MCP 寻址估价 + A2 确认代发单（未配 Key 时降级唤端链接） | `siming didi` |
| `outing_amap_ev` | 电车续航核算与高德 ETA 重排 | 低电量提醒、往返续航精算与实时路况延误捆绑对策卡 | `siming tesla` |
| `tesla_auto_climate` | 出门意图联动自动备车开空调 | 5 重门控（计划+离家+驾车判定+极端温度+电量）自动预热/降温与 18m 安全关停 | 自动触发 |
| `vehicle_cfo` | 电车车务账房与 LFP 满充校准 | 汇总 ETC/停车/充电同步 Sure、LFP 定期满充校准与车险提醒 | `siming car` |
| `commute_hub` | 多模态通勤决策中枢 | 对比自驾/地铁/打车方案、恶劣天气应急卡与搬家规律自动重学 | `siming commute` |
| `haos_night_owl` | 懂夜猫子的 HAOS 全屋联动 | 归家前预开空调/热水、夜间暖光与补觉期硬锁扫地机 | `siming haos` |
| `home_air_guard` | 新房除醛远程新风与 L3 安防 | 结合室外 AQI 编排通风、拟合衰减达标日与浸水/燃气/断电破静告警 | `siming home-air` |
| `homelab_ops` | 家庭路由 / NAS 与三地灾备 | 路由/光猫/NAS SMART 巡检与桌面→NAS→VPS 加密增量灾备清单 | `siming homelab` |
| `bambu_copilot` | Bambu 3D 打印副驾与耗材库 | 打印完工带图通知、异常暂停告警与线材低库存补货 | `siming print3d` |
| `install_plan` | 家电到货初验与送装清单 | 外箱初验清单、现场分组作战单与安装预约缺口只读投影 | `siming install-check` |
| `movein_pm` | 新房入住总控与售后护照 | 前后置施工工序冲突体检、装修隐蔽工程按房间留证与售后护照 | `siming movein` |
| `home_passport` | 住宅水电物业与家电全周期护照 | 账期提醒、滤芯/尘袋寿命追踪、故障码秒查保修与固定资产折旧 | `siming passport` |

---

## 5. 知识库、事件链与 Personal OS

| 模块 ID | 名称 | 核心能力 | 主要入口 |
| :--- | :--- | :--- | :--- |
| `cognitive_drop` | 投递分拣与技术收藏夹 | 截屏/备忘多级分拣：GitHub 收藏夹、在途包裹、改约重排与误触过滤 | `siming drop` / `siming bookmarks` |
| `event_chain` | 六层事件链与自然确认中枢 | `routines.md` 规律库、硬依赖级联重排、月度义务链与单字自然确认 | `siming replay` |
| `hindsight_wiki` | Hindsight 夜间自动共享 Wiki | 跨 Agent 蒸馏长期偏好/决策/红线至 Obsidian 并审阅知识冲突 | `siming wiki` |
| `msim_memory` | M-SIM 跨模型自进化日记 | 沉淀工具/Skill 调优得失并跨模型检索遗传注入 | `siming msim` |
| `digest_indie` | 收藏 Top-3 消化与创意工坊 | 周日精选 Top-3 收藏附隔离试跑命令，联动本地知识库做机制对标 | `siming digest` |
| `context_graph` | 跨应用上下文联邦图谱 | 打通 Files/Mail/Calendar/Wiki/Sure/M-SIM 带出处联邦检索 | `siming graph` |
| `trip_packager` | 差旅与出境游全周期行程包 | 自动建行程清单、境内外刷卡建议与返程报销草稿打包 | `siming trip` |
| `social_boardgames` | 桌游藏品社交与加密人情记忆 | 柜容上限守卫、众筹尾款预警、攒局讲规卡、承诺捕手与加密亲友档案 | `siming social` |
| `compound_tower` | 语音复合流与统一网页控制塔 | 口语多意图合一 A2 按钮卡 + 本地统一网页控制塔 | `siming compound` / `siming tower` |
