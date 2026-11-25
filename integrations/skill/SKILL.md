---
name: siming
description: 本机个人助理后台「司命（siming）」操作技能。当用户表达「等某时间/某状态/PR 合并/报销时再提醒我或接着改」、跑长任务需要完工通知、监控网购保价、拼报销草稿包、查询订单/账单/知识库、打车点餐或投递截图建档时调用。
---

# 司命（siming）操作技能

`siming` 是本机统一的跨会话状态挂起、全机事实检索、长任务与远端回执、网购保价监控、多卡核账与个人 OS 工具。所有临时目录强制走 `$TMPDIR/siming/`（默认 `~/.cache/agent-tmp/siming`，严禁写 `/tmp`）。

> **安全铁律（A2 审批红线）**：`apply Axx` 口令属于用户本人专属纯代码确认动作，**严禁任何 AI Agent 代用户调用 `siming apply`**。Agent 只负责生成预览或告知口令，必须由用户本人在聊天界面或终端亲手确认。

## 1. 跨 Agent 挂起任务（用户说「等 xxx 时候再说 / 接着改」）

只要用户明确表达延后或等条件满足再提醒/继续执行，**当场执行 `siming defer add` 落库**，并在回复末尾给出一行 `[Wxx] 已挂起` 回执：

```bash
# 仅提醒（remind）
siming defer add --when "2026-11-20 20:00" --mode remind --cwd "$PWD" --quote "周五晚上提醒我看账单"

# 等意图出现时提醒（intent）
siming defer add --when "intent:报销|发票" --mode remind --cwd "$PWD" --quote "等报销的时候再修"

# 类型化安全探针（严禁裸 bash 管道）：支持 gh_pr_merged: / gh_pr_checks: / file_exists: / pid_gone: / port_open: / url_200: / herdr:
siming defer add \
  --when "probe:gh_pr_merged:owner/repo#418" \
  --every 30m --ttl 30d \
  --mode stage --runner codex --cwd "$PWD" \
  --task "删掉临时补丁并跑通测试" \
  --done-check "pixi run pytest" \
  --quote "等 PR 418 合并了帮我删掉临时补丁"

# 等进程退出后，在 herdr 后台不抢焦点新建标签恢复当前会话（resume）
siming defer add \
  --when "probe:pid_gone:12345" \
  --mode resume --agent claude --session "$CLAUDE_SESSION_ID" --cwd "$PWD" \
  --context "接着刚才的方案改下一阶段" \
  --quote "等训练跑完接着刚才说的改"
```

> **提示**：`--ttl` 默认 14 天（显式上限 400 天）。若目标日期在两周之后，请显式传入足够长的 `--ttl`（如 `--ttl 60d`）。

## 2. 本机 / 远端长任务完工回执与面板监听

```bash
siming run --name "全量评测" -- pytest -q             # 本机零轮询包装执行，退出即推退出码、耗时与脱敏末尾 5 行
srr python train.py                                   # 远端服务器通用包装器，经受限子令牌回传完工回执
siming remote-hook --host gpu-server --deploy gpu-server
siming watch gpu:p3T                                  # 监听已在跑的 herdr Agent 面板，完成或卡审批时单次提醒
```

## 3. 统一推送出口（替代直接调聊天机器人）

所有新写的脚本与定时任务发消息一律通过 `siming push` 收口，自动享受去重、夜猫子作息暂扣、Push Guard 熔断与审计：

```bash
siming push --rule my_alert --level now --ref topic:1 "高优先级即时消息"
echo "$BODY" | siming push --rule daily_news --level normal -
siming push --rule drift_check --level digest --ref cron:drift "仅归入晨札的汇总"
siming push --rule backup_fail --level now --hard "硬规则：不被自动降级"
```

## 4. 常用核心与垂直模块速查

```bash
# 检索、单据、派单、投递与体检
siming ask "要查询的关键词"
siming cases [--all]
siming done S01 [--cancel] [--note "已处理"]
siming dispatch siming "补一条单元测试"
siming drop --latest-shot --note "截图备注"
siming today [--full]
siming doctor

# 功能模块一键开关（随时关闭不用的垂直模块，数据与手动查询保留）
siming module ls
siming module off price_watch
siming module on price_watch

# 账本、对账与自托管 Sure OS
siming ledger report [--month YYYY-MM]
siming ledger import-zip <path> [--password P]
siming ledger ask "外卖 本月"
siming ledger receivables [--add 标题 金额]
siming sure [status|sync|ask "<问句>"|rules|add-rule]

# 网购保价、日常照护、出行点餐与定位
siming price-watch [list|add|remove]
siming care [status|weather|harvest|inject|pen|refill|stock]
siming loc [--map-wifi SSID 别名]
siming attention [status|sleep|awake]
siming didi [--set 别名 地址|--set-key|<目的地>]
siming meituan [--loc 地点|--history|--reorder|--order]
siming luckin [--set-key|<饮品>]

# 研发分身、共享 Wiki、联邦图谱与网页控制塔
siming resume [<项目名>]
siming ops-check [--no-heal]
siming runtime [status|select|dispatch]
siming wiki [status|distill|query "<关键词>"]
siming msim [record|search|inject <问题族>|leaderboard]
siming graph [index|query "<跨应用问句>"|stats]
siming tower [serve --host 127.0.0.1 --port 8788|snapshot]
```

## 5. 推送熔断与兜底提议

- **Push Guard 熔断**：单规则每小时上限 6 条（hard/urgent 20 条）、全机每小时 20 条，超出转 `THROTTLED` 汇入晨札。查看状态：`python -m siming.push_guard status`；解除：`python -m siming.push_guard restore <rule>`。
- **会话兜底扫描**：自动扫描推断的延后提醒一律落为 `PROPOSED` 不自动触发，需用户确认「记下 Wxx」才转 `WAITING`（72h 自动过期）。
