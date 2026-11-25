# Siming（司命）从零到生产配置教程

本教程采用**渐进式分层配置**设计：你可以用 **5 分钟跑通 Tier 1（纯本机最小模式）**，后续再根据自己的硬件与生活习惯，按需开启聊天推送、网页控制塔、公网边缘节点、自托管财务 OS 或智能家居/电车模块。不需要的模块随时可一键关闭（`siming module off <模块名>`）。

---

## 目录

- [前置要求与目录约定](#前置要求与目录约定)
- [Tier 1：5 分钟最小可用（纯本机 + 跨 Agent 挂起与检索）](#tier-15-分钟最小可用纯本机--跨-agent-挂起与检索)
- [Tier 2：接入聊天软件（Hermes QQ / 飞书 + 原生交互按钮）](#tier-2接入聊天软件hermes-qq--飞书--原生交互按钮)
- [Tier 3：systemd 后台常驻与本地网页控制塔（Control Tower）](#tier-3systemd-后台常驻与本地网页控制塔control-tower)
- [Tier 4（可选）：公网边缘节点 `siming-edge` + iPhone 快捷指令](#tier-4可选公网边缘节点-siming-edge--iphone-快捷指令)
- [Tier 5（可选）：多卡账本、邮箱采集与自托管 Sure 财务 OS](#tier-5可选多卡账本邮箱采集与自托管-sure-财务-os)
- [Tier 6（按需）：垂直生活、电车、智能家居与 Homelab 插件](#tier-6按需垂直生活电车智能家居与-homelab-插件)
- [验收与排障指南](#验收与排障指南)

---

## 前置要求与目录约定

### 系统要求
- **操作系统**：Linux（推荐 Arch / Ubuntu / Debian，完整支持 `systemd --user`；macOS 可手动运行 CLI 与控制塔）
- **Python**：`>= 3.12`（推荐使用 [Pixi](https://pixi.sh/) 或 [uv](https://docs.astral.sh/uv/) 管理环境）
- **可选依赖**：`ripgrep` (`rg`) 用于毫秒级知识库检索；`docker compose` 用于自托管 Sure 财务 OS；`chromium --remote-debugging-port=9222` 用于可选的网页只读采集；`herdr` 用于后台无焦点恢复编码 Agent 标签页。

### 数据边界（非常重要）
Siming 严格分离**代码**与**个人数据/凭据**：

| 路径 | 权限 | 用途 | 是否进 Git |
| :--- | :--- | :--- | :--- |
| `~/Work/siming/` | 常规 | 仓库源码、Schema、文档与模板 | 是 |
| `~/.config/siming/` | 常规 | `policy.yaml`、`sources.toml`、`accounts.toml`、`calendar_rules.json` | 否 |
| `~/.config/siming/secrets/` | `0700` / 文件 `0600` | 所有 Token、IMAP 授权码、MCP Key、加密主密钥 | **严禁** |
| `~/.local/share/siming/` | `0700` | `siming.db`（SQLite WAL）、加密原文 `raw/`、备份 `backups/` | 否 |
| `~/.cache/agent-tmp/siming/` | `0700` | 临时工作目录、隔离 `git worktree`（**强制禁写 `/tmp`**） | 否 |

---

## Tier 1：5 分钟最小可用（纯本机 + 跨 Agent 挂起与检索）

### 第 1 步：拉取代码并安装运行环境

```bash
mkdir -p ~/Work
git clone https://github.com/kouyichi/siming.git ~/Work/siming
cd ~/Work/siming

# 方式 A：使用 Pixi（推荐，自动锁定 Python 3.12 与全部依赖）
pixi install

# 方式 B：使用 uv
# uv sync
```

### 第 2 步：安装全局 `siming` 命令

```bash
mkdir -p ~/.local/bin
chmod +x ~/Work/siming/tools/siming-bin.sh
ln -sf ~/Work/siming/tools/siming-bin.sh ~/.local/bin/siming

# 确认命令可用
which siming && siming --help | head -n 20
```

### 第 3 步：初始化个人配置文件

复制仓库自带的示例模板到 `~/.config/siming/`：

```bash
mkdir -p ~/.config/siming/secrets
chmod 700 ~/.config/siming/secrets

cp ~/Work/siming/config.example/policy.example.yaml ~/.config/siming/policy.yaml
cp ~/Work/siming/config.example/sources.example.toml ~/.config/siming/sources.toml
cp ~/Work/siming/config.example/accounts.example.toml ~/.config/siming/accounts.toml
```

编辑 `~/.config/siming/policy.yaml`，先填好最基础的 4 项：
1. `whitelisted_repos`：允许 `siming dispatch` 远程派单的本地代码仓库映射；
2. `knowledge_roots`：允许 `siming ask` 只读检索的本地笔记/文档目录（如 Obsidian Vault）；
3. `coding_plan`：检查 `claude_bin`、`codex_bin`、`herdr_bin` 路径；
4. `places`：常用地址别名（如 `家`、`公司`）。

### 第 4 步：首次体检与试跑

```bash
siming doctor
siming today
siming defer add --when "10m" --mode remind --cwd "$PWD" --quote "验证司命挂起提醒"
siming defer list
```

### 第 5 步：将 Siming Skill 接入你的编码 Agent

为了让 Claude Code / Codex / Hermes / Antigravity 在你随口说「等 PR 合并再改」「跑完叫我」时自动调用 `siming`：

```bash
# 示例：安装到通用 Agent Skills 目录（按你使用的工具路径调整）
mkdir -p ~/.agents/skills/siming
cp ~/Work/siming/integrations/skill/SKILL.md ~/.agents/skills/siming/SKILL.md
```

并在你的全局 `AGENTS.md` / `CLAUDE.md` 中加入约定（或执行 `siming propose-agent-rules` 生成带审批的预览）：
- 用户表达「等某时间/事件/PR/进程完成后再提醒或继续」时，立刻调用 `siming defer add` 落库；
- 绝不代用户执行 `siming apply Axx`。

---

## Tier 2：接入聊天软件（Hermes QQ / 飞书 + 原生交互按钮）

Siming 将所有外发消息收口到唯一的 `router.py`，默认经由 [Hermes Agent](https://github.com/NousResearch/hermes-agent) 的 `qqbot` / 飞书网关发送，并附带 `integrations/hermes-plugin/` 插件以支持 **QQ 原生可点击确认/选择按钮**。

### 第 1 步：配置 `~/.config/siming/policy.yaml` 的通道目标

```yaml
channels:
  qq_target: "qqbot:<你的 QQ C2C OpenID>"
  feishu_fallback: "feishu:<你的飞书兜底群 Chat ID>"  # 可选
  hermes_bin: "~/.local/bin/hermes"
```

### 第 2 步：安装 Hermes 桥接插件（支持 QQ 按钮与纯代码回执）

```bash
mkdir -p ~/.hermes/plugins
ln -sfn ~/Work/siming/integrations/hermes-plugin ~/.hermes/plugins/siming-bridge
```

重启 Hermes Gateway 后：
- 当 Siming 推送 A2 审批卡（`apply Axx`）、美团方案、照护提醒或晨札时，`siming-bridge` 会自动挂载 QQ 原生按钮；
- 用户在 QQ 点击按钮会直接调用 `siming inbound-action --stdin-json`，**零模型介入、纯代码校核 C2C OpenID 并确定性执行**。

### 第 3 步：测试统一外发与推送熔断

```bash
# 发一条测试消息验证通道连通
siming push --rule setup_test --level now "Siming 通道连通测试成功"

# 查看 Push Guard 熔断状态（单规则 6/h，全机 20/h）
python3 -m siming.push_guard status
```

> **最佳实践**：此后你在本机新写的任何脚本/定时任务需要发通知时，一律调用 `siming push --rule <name> --level now|normal|digest "内容"`，不要直接绕过 `router` 调机器人，避免夜猫子补觉被打断或异常刷屏。

---

## Tier 3：systemd 后台常驻与本地网页控制塔（Control Tower）

仓库 `systemd/` 目录已提供使用 `%h`（自动解析当前用户 `$HOME`）的通用用户级 Unit 模板。

### 第 1 步：安装并启动 `systemd --user` 定时器与服务

> 如果你的代码未放在 `~/Work/siming`，请先修改各 `.service` 中的 `WorkingDirectory` 与 `ExecStart`。

```bash
mkdir -p ~/.config/systemd/user
cp ~/Work/siming/systemd/siming-*.service ~/.config/systemd/user/
cp ~/Work/siming/systemd/siming-*.timer ~/.config/systemd/user/

systemctl --user daemon-reload

# 核心定时器（每 2m 主节拍、每 2m 纯脚本看门狗、每日 04:20 在线备份）
systemctl --user enable --now siming-tick.timer siming-watchdog.timer siming-backup.timer

# 本地网页控制塔（默认仅监听 127.0.0.1:8788）
systemctl --user enable --now siming-tower.service

# （仅当配置了 Tier 4 公网边缘节点时启用）
# systemctl --user enable --now siming-edge-sync.timer
```

验证服务运行状态：

```bash
systemctl --user list-units 'siming*'
```

### 第 2 步：访问本地网页控制塔（Control Tower）

默认监听 `http://127.0.0.1:8788`（仅本机可访问）。首次访问或要开启密码保护时：

```bash
# 可选：为控制塔设置访问密钥（权限 0600）
(umask 077; openssl rand -base64 24 > ~/.config/siming/secrets/tower-token)
```

若需要从手机/外网通过自己的域名安全访问控制塔，建议使用 Tailscale/WireGuard 内网或 Caddy/Nginx + HTTPS + BasicAuth 反向代理到 `127.0.0.1:8788`，并把反代域名加入 `~/.config/siming/policy.yaml` 的 `tower.allowed_hosts`（防跨站 CSRF）：

```yaml
tower:
  allowed_hosts:
    - "127.0.0.1"
    - "localhost"
    - "::1"
    - "edge.example.com"
```

---

## Tier 4（可选）：公网边缘节点 `siming-edge` + iPhone 快捷指令

如果你希望把 **iPhone 短信（快递取件码/银行动账）、Wi-Fi/GPS 定位、睡眠感知、截屏离线 OCR** 投递回本机，并具备 **家里断电/断网 15 分钟死人开关告警**，可以花 15 分钟在一台最低配公网 VPS 上部署 `edge/siming_edge.py`。

### 第 1 步：在公网 VPS 部署 `siming-edge` 与 `siming-deadman`

```bash
# 在 VPS 上创建独立非特权用户与目录
sudo useradd --system --home-dir /var/lib/siming-edge --create-home --shell /usr/sbin/nologin siming
sudo mkdir -p /opt/siming-edge /etc/siming-edge
sudo chmod 700 /etc/siming-edge

# 从本机上传 edge 文件到 VPS（将 edge-vps 替换为你的 ~/.ssh/config 别名）
scp ~/Work/siming/edge/siming_edge.py edge-vps:/tmp/
scp ~/Work/siming/edge/siming-edge.service ~/Work/siming/edge/siming-deadman.* edge-vps:/tmp/
```

在 VPS 上安装并写入与本机一致的 `edge-token`（查看本机密钥：`cat ~/.config/siming/secrets/edge-token`）：

```bash
sudo install -m 0755 /tmp/siming_edge.py /opt/siming-edge/siming_edge.py
sudo install -m 0644 /tmp/siming-edge.service /tmp/siming-deadman.service /tmp/siming-deadman.timer /etc/systemd/system/
rm -f /tmp/siming_edge.py /tmp/siming-edge.service /tmp/siming-deadman.*

# 写入与本机相同的 edge-token（0600，属主 siming）
sudo sh -c 'umask 077; printf "%s\n" "<你的本机edge-token>" > /etc/siming-edge/token'
sudo chown -R siming:siming /etc/siming-edge /var/lib/siming-edge

sudo systemctl daemon-reload
sudo systemctl enable --now siming-edge.service siming-deadman.timer
```

### 第 2 步：在 VPS 配置 HTTPS 反向代理（以 Caddy 为例）

`siming-edge` 默认监听 `127.0.0.1:18792`。在 Caddyfile 中暴露 `/siming/in*`：

```caddyfile
edge.example.com:8443 {
    handle /siming/in* {
        reverse_proxy 127.0.0.1:18792
    }
}
```

### 第 3 步：在本机配置 `policy.yaml` 并启用同步定时器

```yaml
edge:
  ssh_host: "edge-vps"
  public_in_url: "https://edge.example.com:8443/siming/in"
  remote_cli: "sudo -n -u siming /usr/bin/python3 /opt/siming-edge/siming_edge.py"
  pull_interval_sec: 300
```

并在 VPS 的 `/etc/sudoers.d/siming-pull` 中仅授权你的 SSH 账号无密码执行 `siming_edge.py`：

```text
<your-ssh-user> ALL=(siming) NOPASSWD: /usr/bin/python3 /opt/siming-edge/siming_edge.py *
```

回到本机测试并开启 1 分钟同步定时器：

```bash
siming edge-sync
systemctl --user enable --now siming-edge-sync.timer
```

### 第 4 步：配置 iPhone 快捷指令与 OwnTracks

按照 **[docs/IOS_SHORTCUTS_GUIDE.md](docs/IOS_SHORTCUTS_GUIDE.md)** 的图文步骤完成短信、位置、无感睡醒与截屏投递配置（约 5–8 分钟）。

---

## Tier 5（可选）：多卡账本、邮箱采集与自托管 Sure 财务 OS

### 1. 配置多张银行卡（`~/.config/siming/accounts.toml`）

编辑 `~/.config/siming/accounts.toml`，仅填写银行名、自定义别名、后 4 位尾号与账单/还款日（**严禁写入完整卡号或密码**）：
- `repay_card = true`：标记为专用信用卡还款借记卡，转入/自动扣还自动归为「划转」防重复计消费；
- `reimburse_cursor = true`：大额还款或公事消费入账时自动刷新报销游标卡。

### 2. 配置邮箱只读采集（163 IMAP / Omamail）

```bash
# 方式 A：163 邮箱 IMAP 授权码（推荐只读独立文件夹或 INBOX）
(umask 077; printf '{"user":"yourname@163.com","password":"<IMAP授权码>"}\n' > ~/.config/siming/secrets/mail163)

# 方式 B：如果你使用 Omamail 多邮箱客户端（163 / Yahoo / Gmail OAuth），
# Siming 会自动调用 omamail CLI 只读拉取摘要；Gmail 过期时可用 tools/gmail_reauth.py 一键重授权。
```

### 3. 导入历史微信 / 支付宝导出账单

```bash
# 支持单个 .csv / .xlsx / .pdf，或直接导入带密码的加密 .zip（纯内存流式解密，绝不留明文副本）
siming ledger import-zip ~/Downloads/alipay_statement.zip --password "<解压码>"
siming ledger report --month 2026-11
siming ledger ask "外卖 本月"
```

### 4. 一键部署自托管 Sure 财务 OS（Docker）

`deploy/sure/docker-compose.live.yml` 严格只向本机暴露 `127.0.0.1:3032`，Postgres 与 Redis 完全隔离在私网中：

```bash
# 可选：自定义登录邮箱（默认 admin@siming.local）
export SURE_EMAIL="you@example.com"

# 自动生成 0600 密钥、拉起 Sure 容器并幂等导入 siming.db 全部流水
bash ~/Work/siming/tools/sure_up.sh
```

导入完成后，浏览器访问 `http://127.0.0.1:3032`，登录凭证自动保存在 `~/.config/siming/secrets/sure-login.txt`（`0600`）。

---

## Tier 6（按需）：垂直生活、电车、智能家居与 Homelab 插件

以下模块均相互独立，不使用的可以直接 `siming module off <module_id>` 关闭。

### 1. 日常照护（通勤天气 / 周期用药 / 耗材补货雷达）
编辑 `~/.config/siming/policy.yaml` 的 `daily_care` 节点，填入你自己的住址/公司经纬度、出门时间、药品名称/医院与耗材规则：
```bash
siming care status
```

### 2. 网购保价、美团 H5 / 瑞幸 MCP / 滴滴 MCP
- **浏览器只读采集与 H5 加购**：启动桌面主 Chromium 时加上 `--remote-debugging-port=9222` 并登录对应网页版；Siming 会自动识别监听者，防误连其他占端口的 Electron 客户端。
- **滴滴官方 MCP**：运行 `siming didi --set-key` 交互式安全写入 Key（`0600`）；设置常用地址 `siming didi --set 公司 "你的办公地址"`。
- **瑞幸官方 MCP**：运行 `siming luckin --set-key` 交互式写入 Token。
- **安全边界提示**：美团 H5 下单**默认仅停在零扣款结算预览页**，签发 10 分钟有效的 `apply Axx` 确认卡，付款需在官方 App 完成。`meituan_pay.hermes_handoff.enabled` 默认强制为 `false`。

### 3. 电动车 / Tesla Fleet API & 高德实时路况
- **高德实时路况 ETA**：将高德 Web 服务 Key 存入 `~/.config/siming/secrets/amap-key`（未配置时自动用本地路网模型兜底）。
- **Tesla Fleet API（零休眠唤醒）**：在 `policy.yaml` 配置 `tesla.home_garage_coords` 与 `tesla.virtual_key_pairing_url`，运行 `siming tesla --auth` 完成 PKCE 授权；或零配置直接用手机截屏 / `特斯拉电量 80` 录入电量。

### 4. Home Assistant 全屋智能与 NAS 三地灾备
- **Home Assistant**：在 `policy.yaml` 设置 `home_assistant.url`，并将 HA 长效令牌写入 `~/.config/siming/secrets/ha-token`（`0600`），即可启用新风除醛、归家预开空调与上午补觉锁扫地机。
- **NAS Homelab 栈**：参考 `config.example/nas.env.example` 与 `deploy/nas/docker-compose.nas.yml` 部署 Sure + HAOS + Restic 加密灾备。

### 5. 远端 GPU 服务器完工回执（`srr`）
向你的远端训练/计算服务器下发**最小权限子令牌**（仅允许投递完工回执，无法伪造短信或控制指令）：
```bash
siming remote-hook --host gpu-server --deploy gpu-server
```
按提示把打印出的 POSIX `srr()` 函数加入远端 `~/.bashrc` 或 `~/.zshrc`，此后运行 `srr python train.py` 即可零轮询在任务结束时向手机汇报退出码、耗时与脱敏末尾 5 行。

### 6. 模块开关治理
随时查看与启停任意模块：
```bash
siming module ls                    # 列出全部 53 个模块状态、14d 推送次数与最近使用时间
siming module off sub_assassin      # 关闭指定模块后台触发（保留历史数据与手动查询）
siming module on sub_assassin       # 重新开启
```

---

## 验收与排障指南

### 1. 一键全链路自检
```bash
siming doctor                       # 检查数据库、密钥权限、CDP 9222、边缘节点与全部数据源健康度
siming replay --fixture all --dry-run  # 离线回放关键防呆与审批场景（零外发）
SIMING_TEST_OFFLINE=1 SIMING_DISABLE_SSH_EDGE=1 pixi run pytest  # 运行完整单元测试
```

### 2. 常见问题排查
- **报错 `禁止使用 /tmp 路径`**：请设置 `export TMPDIR="$HOME/.cache/agent-tmp"`（`tools/siming-bin.sh` 与 `systemd/*.service` 已自动设置）。
- **某条规则被 Push Guard 静默或限流**：运行 `python3 -m siming.push_guard status` 查看原因，确认修好上游噪声后执行 `python3 -m siming.push_guard restore <rule_id>`。
- **桌面 Chromium 9222 端口被其他 Electron 应用占用**：`siming doctor` 会给出明确告警，Siming 会自动尝试 `[::1]:9222` 回退，或将占用端迁移到其他调试端口。
- **检查凭据权限是否合规**：
  ```bash
  ls -ld ~/.config/siming/secrets && ls -la ~/.config/siming/secrets
  # 目录应为 drwx------ (700)，所有密钥文件应为 -rw------- (600)
  ```
