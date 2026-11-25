# iPhone 快捷指令与边缘感知配置手册

> 本手册适用于已完成公网边缘网关 `edge/siming_edge.py` 部署的用户，用于将手机短信、Wi-Fi/GPS 位置、无感睡醒信号与离线截屏 OCR 投递回本机司命（Siming）。所有数据经由你自己的 VPS 加密暂存、由本机定时拉取，**不经过任何第三方云**。

## 前置准备：复制边缘密钥

在运行 Siming 的本机终端执行以下命令，获取自动生成的 0600 权限密钥（网关同时兼容纯 Token 与 `Bearer <token>`）：

```bash
cat ~/.config/siming/secrets/edge-token
```

下文中的 `https://edge.example.com:8443` 请替换为你自己 VPS 上配置的 HTTPS 反代域名与端口。

---

## 一、短信入库自动化（含二道验证码隔离）

`siming-edge` 内置安全分流保护：
- **银行 / 银联 / 转账 / 消费动态验证码**：在边缘入口直接硬拦截丢弃，绝不入库；
- **可选平台登录验证码（`allow_platform_otp=1`）**：仅加密写入独立 `otp_inbox`（300 秒过期、本机拉取一次即焚、绝不进入常规事件表）。

### 配置步骤
1. 打开 iPhone **「快捷指令」→「自动化」→「+」→「信息」**；
2. 触发条件设为：**信息包含 `【`**，选择 **「立即运行」**（关闭「运行前确认」）；
3. 添加动作 **「获取 URL 内容」**：
   - **URL**：`https://edge.example.com:8443/siming/in/sms`
   - **方法**：`POST`
   - **头部**：`Authorization` = `<你的 edge-token>`
   - **请求体**：`JSON` → 键 `text`（文本）= 变量 **`信息`**

---

## 二、位置与 Wi-Fi 感知（打车起点 + 外卖定位 + 离司取件）

建议先建 **1 个共用快捷指令 `司命上报位置`**，再让多条自动化复用它：

### 第 1 步：新建快捷指令 `司命上报位置`
依次添加 3 张卡片：
1. **获取网络详细信息**：默认 `获取 无线局域网 的 网络名称`
2. **获取当前位置**：精确度选 **「最近百米」**（省电、响应快）
3. **获取 URL 内容**：
   - **URL**：`https://edge.example.com:8443/siming/in/loc`
   - **方法**：`POST`
   - **头部**：`Authorization` = `<你的 edge-token>`
   - **请求体**：`JSON`
     | 键名 | 值（蓝底变量） |
     | :--- | :--- |
     | `wifi` | `网络名称` |
     | `lat` | `当前位置 → 纬度` |
     | `lng` | `当前位置 → 经度` |
     | `addr` | `当前位置 → 名称` |

### 第 2 步：挂载零打扰自动化
- **加入 / 离开家或公司 Wi-Fi** → `运行快捷指令(司命上报位置)`
- **打开常用高频 App（多选微信、地图、打车、外卖等）** → `运行快捷指令(司命上报位置)`（后台已内置 200 米位移去重，原地刷手机零噪音）
- **CarPlay / 车载蓝牙连接或断开（电车车主推荐）** → `运行快捷指令(司命上报位置)`

### 可选进阶：OwnTracks 锁屏后台轨迹（零第三方云）
1. App Store 安装开源免费的 **OwnTracks**；
2. 打开 `ⓘ → Settings`：
   - **Mode**：`HTTP`
   - **URL**：`https://edge.example.com:8443/siming/in/loc`
   - **Authentication**：开启 → `UserID` 填任意用户名（如 `me`），`Password` 填你的 `edge-token`；
3. 主界面切换为 `Significant` 或 `Move` 模式，授予「始终允许定位」。

### 第 3 步：绑定 Wi-Fi 与地点别名
在终端或 QQ 中执行：
```bash
siming loc --map-wifi "Your-Office-WiFi" 公司
siming loc --map-wifi "Your-Home-WiFi" 家
```

---

## 三、无感「睡着 / 醒着」检测（驱动夜猫子注意力引擎）

每条自动化仅需 1 张 **「获取 URL 内容」** 卡片（保持默认 `GET`，头部填 `Authorization: <你的 edge-token>`，关闭「运行前确认」）：

| # | 自动化触发器 | URL | 融合判定效果 |
| :--- | :--- | :--- | :--- |
| 1 | 充电器 → 已连接 | `https://edge.example.com:8443/siming/in/sleep?state=charge_on` | 深夜插电且桌面/手机连续无操作时自动进入 `ASLEEP`，暂扣晨札 |
| 2 | 充电器 → 已断开 | `https://edge.example.com:8443/siming/in/sleep?state=charge_off` | 早晨拔电直接唤醒推送晨札 |
| 3 | App → 已打开（多选常用 App） | `https://edge.example.com:8443/siming/in/sleep?state=phone_active` | 凌晨刷手机防误判入睡；早晨不拔电看手机也能及时唤醒 |
| 4 | 闹钟 → 停止时（可选） | `https://edge.example.com:8443/siming/in/sleep?state=awake` | 关闹钟即刻判醒 |

---

## 四、每日心跳与侧边操作按钮「截屏发给司命」

1. **每日 07:50 定时心跳**：自动化「特定时间 07:50 每天」→ `GET https://edge.example.com:8443/siming/in/hb`（带 `Authorization` 头）。
2. **截屏离线 OCR 投递**：新建快捷指令 `发给司命`：
   - `截屏` → `从图像中提取文本` → `获取 URL 内容`（`POST https://edge.example.com:8443/siming/in/drop`，`Authorization` 头，JSON 字段 `text` = `图像中的文本`）→ `振动设备`；
   - 在 iOS「设置 → 操作按钮 / 轻点背面」中绑定该快捷指令。

---

## 五、10 秒验收

在手机试跑一次 `司命上报位置`，再在本机执行：

```bash
siming edge-sync && siming loc && siming attention
```

若能正确显示当前 Wi-Fi/经纬度以及注意力状态，即代表全链路贯通。
