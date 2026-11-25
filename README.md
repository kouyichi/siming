# 司命（Siming）

**本地优先、确定性驱动的跨 Agent 个人助理后台与 Personal OS。**

Siming 将两类原本割裂的个人信息流统一收口到本机 SQLite（WAL）中协同治理：

1. **跨编码 Agent 工作现场**：无论你在 Claude Code、Codex、OpenCode、Antigravity 还是 Hermes 里随口交代的「等 PR 合并后删掉补丁」「等训练跑完接着改」「等月底报销时再修」，统一记入 `wait_source` 并绑定权限委任状（`Mandate`）；条件一到自动提醒、在 `herdr` 后台恢复原对话，或在独立 `git worktree` 中完成修改并跑通验证测试。
2. **个人数字生活与财务事务**：统一整合多邮箱账单/发票、微信/支付宝月度导出、自托管 Sure 财务 OS、快递取件码、网购保价、中国法定调休限行、日常照护、电车出行、Home Assistant 智能家居与 Homelab 灾备，并经由**全机唯一出口 `router` + 推送熔断器 + 夜猫子注意力引擎**精准触达，平时零打扰、有实质进展再出手。

---

## 核心设计理念

- **本地优先与零公网暴露**：模型循环、数据库（`siming.db`）、账本明细与所有密钥（`0600`）全部留在本机；可选公网边缘节点（`edge/siming_edge.py`）仅做加密收信暂存与断电死人开关，本机主动 SSH 拉取，**无需向公网开放任何本机端口**。
- **确定性优先，绝不滥用 LLM**：能用规则、SQL、类型化探针解决的绝不调模型；所有外部状态变迁写入 SQLite 事务与 `effects` 队列（After-Commit 投递与退避重试）。
- **硬权限分级（A1 / A2 / A3）与纯代码审批**：
  - `A1`：只读查询、本地归档、白名单 DNS 清理（自动执行并记审计日志）；
  - `A2`：外部动作或消费预备（如改文件、加购/下单确认、发车确认），签发带 TTL 的一次性口令 `apply Axx`（支持聊天原生按钮），**由纯代码 CAS 校验，严禁任何 AI Agent 代用户确认**；
  - `A3`：转账、删库、绕过人机风控等高危操作，默认硬禁止。
- **低噪音、懂作息、防刷屏**：
  - **夜猫子注意力引擎（Night-Owl）**：融合桌面活跃与手机充断电/亮屏信号，凌晨写代码时高优消息照常到达，上午补觉自动压住晨札直到自然醒；
  - **Push Guard 推送熔断**：全机统一经 `siming push` 发消息，单规则每小时 6 条 / 全机每小时 20 条硬熔断，14 天零处理率规则自动转静默汇入晨札。
- **53 个模块化能力一键开关**：所有垂直生活与 Homelab 插件均支持 `siming module off <name>` 随时关闭，历史数据与手动查询完全保留。

---

## 总体架构

```text
[iPhone / OwnTracks] (可选)                                    [可选：公网 Edge VPS]
  短信 / 位置 / 睡眠信号 / 截屏 OCR / 每日心跳 ── HTTPS POST ──▶ siming-edge（加密暂存 7 天）
                                                                 ├─ 断电预排发件箱
                                                                 └─ siming-deadman（15m 失联告警）
                                                                        ▲
[本机工作现场] Claude Code / Codex / OpenCode / Hermes                  │ SSH 定期拉游标 + 发心跳
  ├─ 显式登记：siming defer / siming run / siming drop                  ▼ （本机零公网入站端口）
  └─ 2m 只读增量扫描会话输入（仅提议 PROPOSED，确认后生效）──▶ [本机 Siming · SQLite WAL]
                                                                 ├─ collectors（无模型权限 / 无外发权）
                                                                 ├─ evald（90s 合批 + 六层事件链）
                                                                 ├─ worker（Mandate 重验 + worktree / herdr）
                                                                 ├─ watchdog（2m 纯脚本巡检 + 端口防呆）
                                                                 ├─ router（唯一消息出口 + Push Guard + A2 审批）
                                                                 └─ control_tower（127.0.0.1:8788 网页控制塔）
```

详细进程边界、表结构与事件链机制见 **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)**。

---

## 5 分钟快速上手（纯本机最小模式）

> 完整分步配置（含 QQ/飞书、systemd、网页控制塔、Edge VPS、iOS 快捷指令、自托管 Sure 与全部垂直插件）请阅读 **[完整配置教程 docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md)**。

### 1. 克隆并安装依赖

推荐使用 [Pixi](https://pixi.sh/)（或 `uv` / Python `>= 3.12`）：

```bash
git clone https://github.com/kouyichi/siming.git ~/Work/siming
cd ~/Work/siming

# 方式 A：Pixi（推荐）
pixi install

# 方式 B：uv / pip
# uv sync
# 或 python3 -m venv .venv && .venv/bin/pip install -e .
```

### 2. 安装 `siming` 命令入口

```bash
mkdir -p ~/.local/bin
ln -sf ~/Work/siming/tools/siming-bin.sh ~/.local/bin/siming
chmod +x ~/Work/siming/tools/siming-bin.sh
```

> 请确保 `~/.local/bin` 在你的 `PATH` 中，并且建议配置 `TMPDIR=~/.cache/agent-tmp`（Siming 出于安全与防 tmpfs 写满考虑，强制拒绝写入 `/tmp`）。

### 3. 初始化配置并体检

```bash
# 首次运行自动生成 ~/.config/siming/ 与 ~/.local/share/siming/
siming doctor

# （推荐）参考 config.example/ 按需编辑个人配置
cp config.example/policy.example.yaml ~/.config/siming/policy.yaml
cp config.example/accounts.example.toml ~/.config/siming/accounts.toml
```

### 4. 立刻体验核心能力

```bash
siming today                                                    # 预览今日晨札盘点
siming defer add --when "20m" --mode remind --quote "休息喝水"   # 登记一条挂起提醒
siming defer list                                               # 查看活跃挂起项
siming run --name "单元测试" -- pixi run pytest -q               # 包装执行长任务并在结束时回执
siming ask "安全边界"                                            # 带文件:行号检索全机私有知识库
siming module ls                                                # 查看全部 53 个模块开关状态
siming tower serve --host 127.0.0.1 --port 8788                 # 启动本地网页控制塔
```

---

## 文档导航

| 文档 | 内容说明 |
| :--- | :--- |
| **[docs/SETUP_GUIDE.md](docs/SETUP_GUIDE.md)** | **从零到生产完整配置教程**（分 6 个渐进阶段，按需配置） |
| **[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)** | 系统架构、六层事件链、四档跨 Agent 唤醒与安全护栏 |
| **[docs/MODULES.md](docs/MODULES.md)** | 53 个可开关功能模块全景目录与 CLI 速查 |
| **[docs/IOS_SHORTCUTS_GUIDE.md](docs/IOS_SHORTCUTS_GUIDE.md)** | iPhone 快捷指令（短信/位置/睡醒/截屏 OCR）与 OwnTracks 配置 |
| **[docs/DECISIONS.md](docs/DECISIONS.md)** | 架构原则与 A1/A2/A3 安全边界记录 |
| **[config.example/](config.example/)** | `policy.yaml` / `sources.toml` / `accounts.toml` / Docker `.env` 模板 |
| **[integrations/skill/SKILL.md](integrations/skill/SKILL.md)** | 可直接装入 Claude Code / Codex / Hermes 的 Agent Skill |

---

## 目录与数据边界

```text
~/Work/siming/                 源码、SQL Schema、部署模板与文档（进 Git）
~/.config/siming/              你的个人配置（policy.yaml / accounts.toml / sources.toml）
~/.config/siming/secrets/      所有密钥与令牌（权限 0600，绝不进 Git）
~/.local/share/siming/         运行期状态库（siming.db / 加密原件 raw/ / 审计日志 / 备份）
~/.cache/agent-tmp/siming/     临时目录（账单内存解压、git worktrees、草稿包，禁写 /tmp）
```

---

## 运行测试

所有单元测试均为 100% 本地离线沙盒测试（不发外部消息、不访问真实个人目录）：

```bash
SIMING_TEST_OFFLINE=1 SIMING_DISABLE_SSH_EDGE=1 pixi run pytest
```

## License

[MIT](LICENSE)
