# workbuddy2api

把 **WorkBuddy / CodeBuddy（腾讯代码助手）** 的桌面端登录态，转成你本机 / 局域网可直接使用的 **OpenAI / Anthropic 兼容 API**，并提供一个 **多账号代理共享 + 自动化运营平台**。

## 它是什么

`workbuddy2api` 不负责登录、不模拟桌面端、不替你执行工具。对外它提供两类能力：

### 一、API 反代网关

1. 读取本机登录态并注入完整的鉴权头（含设备风控头 `X-Device-Token`）
2. 在 OpenAI / Anthropic 协议与腾讯后端协议之间转换
3. 对 Codex CLI 这类长上下文 agent 请求做后端友好的压缩投影

### 二、多账号自动化运营平台

号池管理之外，还内置了一整套**纯协议实现的自动化能力**——不需要桌面端开机、不需要 CLI 常驻、不占用本地登录态：

| 能力 | 说明 |
|------|------|
| **每日签到领积分** | 活动期间自动为号池内每个账号领取每日签到奖励，已领自动跳过，活动下线自动停手保护账号 |
| **成长计划任务自动化** | 拉取任务列表 → 自动参与 → 自动完成 → **自动领取积分**，全流程闭环 |
| **猫猫旅行** | 同意协议 → 首次领养（+300）→ 派猫 → 到站领奖，批量执行 |
| **定时调度** | 内置调度器，签到 / 刷余额 / 同步模型列表 / 拉取成长任务列表均可按间隔自动跑 |

**成长任务是本项目的核心亮点**：逆向出任务进度由**两条通道**驱动——一条是模型请求体里的 `extra_vars.growthEvent`，另一条是**客户端真实业务事件上报**（`POST /v2/report`）。两条通道都用**完全合法的请求**（正常 200 响应、免费模型或零成本事件、真实业务对象 id），因此能完成其中绝大多数任务并顺带把奖励领到手。详见 [第六章](#六成长计划任务growth)。

> 共有 **15 个可自动完成的任务**（单账号满额 1700 积分），覆盖对话、技能、自动化、模型体验、资料库、灵感案例、召唤专家、专家团、换肤、桌面端应用、**设计画布**等类型。触发方式分三类：`growthEvent`、**真实业务事件上报**、以及 **Ardot MCP 工具直调**（`create_canvas`），详见 [6.6](#66-两类触发通道关键区分)。新增的同类任务会被[模式规则](#68-新增任务会自动识别模式匹配)自动识别。
>
> 不做的只剩付费与需真实捐款的：`expert_5_paid`（付费）、`Expert_Philanthropy`（需真实捐款）。

---

## 项目运行截图
<img src="./images/img_1.png">
<img src="./images/img_2.png">
<img src="./images/img_3.png">
<img src="./images/img_4.png">

## 目录

- [一、逆向工程：解包 WorkBuddy 桌面端源码（app_source）](#一逆向工程解包-workbuddy-桌面端源码app_source)
- [二、逆向反代核心（workbuddy2api 网关）](#二逆向反代核心workbuddy2api-网关)
- [三、多账号代理共享平台（admin）](#三多账号代理共享平台admin)
  - [3.7 使用记录：默认近 1 天，可按 Key / 模型筛选](#37-使用记录默认近-1-天可按-key--模型筛选)
  - [3.8 定时任务结果：人话摘要](#38-定时任务结果人话摘要)
- [四、环境安装与项目运行](#四环境安装与项目运行)
- [五、每日签到定时任务（daily_checkin）](#五每日签到定时任务daily_checkin)
- [六、成长计划任务（growth）](#六成长计划任务growth)
  - [6.5 任务分级与实测结论](#65-任务分级与实测结论)
  - [6.6 两类触发通道（关键区分）](#66-两类触发通道关键区分)
  - [6.7 对象 id 一律现拉，绝不编造](#67-对象-id-一律现拉绝不编造)
  - [6.8 新增任务会自动识别（模式匹配）](#68-新增任务会自动识别模式匹配)
- [七、客户端接入](#七客户端接入)
- [八、日志与排障](#八日志与排障)
- [九、项目结构](#九项目结构)
- [十、稳定性与流量治理（限速 / 并发 / 保活）](#十稳定性与流量治理限速--并发--保活)
  - [10.1 选号：三因子加权 + Top-N 抽签](#101-选号三因子加权--top-n-抽签)
  - [10.2 并发：在途租约（把并发摊平到全池）](#102-并发在途租约把并发摊平到全池)
  - [10.3 会话粘性：同一会话固定同一账号](#103-会话粘性同一会话固定同一账号)
  - [10.4 错误分类与账号处置（一张表看懂）](#104-错误分类与账号处置一张表看懂)
    - [10.4.0 上游错误码全表（逆向官方客户端得到）](#1040-上游错误码全表逆向官方客户端得到)
    - [10.4.1 事故复盘：一次 14003 抖动如何变成 10 分钟全站 503](#1041-事故复盘一次-14003-抖动如何变成-10-分钟全站-503)
    - [10.4.2 HTTP 200 不等于成功](#1042-http-200-不等于成功)
  - [10.5 WAF：IP 级 fail-fast](#105-wafip-级-fail-fast)
  - [10.6 轮转退避：指数 + 抖动](#106-轮转退避指数--抖动)
  - [10.7 超时：最要紧的一处修复](#107-超时最要紧的一处修复)
  - [10.8 token 保活](#108-token-保活)
  - [10.9 拟人化请求头](#109-拟人化请求头)
  - [10.10 客户端版本与逆向产物：自动发现 / 自动产出](#1010-客户端版本与逆向产物自动发现--自动产出)
  - [10.11 非流式：一个必须修的协议违约](#1011-非流式一个必须修的协议违约)
  - [10.12 这批改动的验证](#1012-这批改动的验证)
- [十一、免责声明与协议](#十一免责声明与协议)
- [十二、致谢与引用声明（Credits & References）](#十二致谢与引用声明credits--references)

---

## 一、逆向工程：解包 WorkBuddy 桌面端源码（app_source）

本项目在落地反代逻辑、补齐风控头之前，先对 **WorkBuddy 桌面端** 做了逆向分析，目的是拿到「真实接口形态 / 必需请求头 / 活动结束时间等字段」，而不是盲猜。产物是 `app_source/`（解包后的前端 + 主进程源码）。

> `app_source/` 是 **逆向产物，不在本仓库内**（本机解包在 `workbuddy/resources/app_source`），本仓库只收录「解包流程」与「反代实现」。

### 1.1 目标与边界

> 下表的路径以本机为例。**安装位置因人而异**，运行时代码全部自动发现，
> 不依赖这里的路径（见 [10.10](#1010-客户端版本与逆向产物自动发现--自动产出)）。

| 项 | 说明 |
|------|------|
| 安装目录 | `D:\workbuddy` 或 `D:\WorkBuddy`（Windows） |
| 主程序包 | `<安装目录>\resources\app.asar`（Electron 打包，约 287MB） |
| 解包产物 | `<安装目录>\app_source`（cli / main / preload / renderer） |
| 原生模块 | 桌面端安装目录下的 `resources/app.asar.unpacked/native/turing-sdk`（运行时由 `turing_helper.js` 自动发现本机安装位置，可用 `WORKBUDDY_TURING_SDK_DIR` / `WORKBUDDY_INSTALL_DIR` 覆盖） |

解包不是为了修改桌面端，而是为了 **确认接口契约**：

- 每日签到：`POST /v2/billing/meter/checkin-activity-status`、`POST /v2/billing/meter/daily-checkin`
- 活动结束时间：`checkin-activity-status` 响应里的 `data.end_time`（即「下次停止领取」的依据）
- 设备风控头：`X-Device-Token`，由桌面端 Turing Shield SDK 生成，签到 / 对话等敏感请求都带
- 业务码：`1001=今日已领`、`1002=无资格`、`1003=活动已结束`

### 1.2 解包步骤

> **推荐做法：用项目内置的纯 Python 抽取器**，不需要 Node、npm，也不用先装
> `asar` npm 包（服务器上照样能跑）：
>
> ```bash
> python wb_asar.py extract                 # 抽到用户缓存目录
> python wb_asar.py extract --dest D:/wb_source
> python wb_asar.py list --grep canvas      # 只想看某些文件
> python wb_asar.py read /package.json      # 读单个文件（unpacked 自动分流）
> ```
>
> 它会**自动定位** app.asar（不用手填路径），并且正确处理 `unpacked`：
> 数据在 asar 里的定位读，标记 `unpacked` 的去 `app.asar.unpacked/` 读。
> 实测全量抽取 **2242 个文件 / 225MB / 14 秒，0 缺失**。
>
> 下面保留**Node 方案**作为参考（知道官方工具链怎么用也有价值）。

> 前置：本机已装 **Node.js**（含 npm）。asar 工具用 `asar` npm 包。

**（1）安装 asar 工具**（在 WorkBuddy 的 managed node workspace 里装，避免污染全局）：

```bash
cd "C:/Users/Administrator/.workbuddy/binaries/node/workspace"
npm install asar --no-save
```

**（2）全量解包会失败** —— `asar extract` 会去读 `app.asar.unpacked` 里缺失的二进制（如 `node-pty-win32-arm64\...\conpty\OpenConsole.exe`、`ripgrep/arm64-darwin/rg`），报 `ENOENT`。

**（3）改用「按需提取脚本」** `extract_source_files.js`（同 workspace 内），只抽 `main / preload / renderer` 的 `js / cjs / mjs / html / json`，避开 unpacked 原生二进制：

```js
const asar = require('asar');
// 路径按本机实际情况改；运行时逻辑不受影响（自动发现）
const src  = 'D:\\workbuddy\\resources\\app.asar';
const dest = 'D:\\workbuddy\\app_source';
const prefixes = ['main', 'preload', 'renderer'];
const extensions = ['.js', '.cjs', '.mjs', '.html', '.json'];

const files = asar.listPackage(src)
  .map(f => f.startsWith('\\') ? f.slice(1) : f)
  .filter(f => prefixes.includes(f.split('\\')[0])
            && extensions.some(ext => f.endsWith(ext)));

for (const file of files) {
  const out = require('path').join(dest, file);
  require('fs').mkdirSync(require('path').dirname(out), { recursive: true });
  require('fs').writeFileSync(out, asar.extractFile(src, file));
}
```

运行：

```bash
node extract_source_files.js
```

产物结构（`python wb_asar.py extract` 产出相同结构）：

```text
<安装目录>\app_source\        # 或 WORKBUDDY_SOURCE_DIR 指定处
├── cli/          # product.json（含 turingSdk.channelId、版本号等配置）
├── main/         # Electron 主进程：AuthService、server.js、tar.js、index.js（Turing SDK 桥接）
├── preload/      # 预加载脚本（renderer ↔ main IPC 通道）
└── renderer/     # 前端打包代码（assets/*.js、国际化 zh-cn-*.js）
```

> 注意：用 Node 方案时，`cli/product.json`、`cli/dist/codebuddy.js`、
> `native/turing-sdk/` 在官方包里是 **unpacked** 的，`asar extract` 会因为
> 找不齐二进制而中途失败。`wb_asar.py` 对这两类文件分别处理，所以能一次抽全。

### 1.3 关键逆向发现（直接驱动了反代实现）

| 发现 | 位置 | 对反代的意义 |
|------|------|------|
| 设备风控头 `X-Device-Token` | `main/tar.js` `buildHeadersWithTuringToken` / `TURING_SHIELD_ID_HEADER="X-Device-Token"` | 反代必须给签到 / 对话请求注入该头，否则上游风控识别为「非真实客户端」 |
| Turing SDK 桥接 | `resources/app.asar.unpacked/native/turing-sdk/index.cjs`（`configure` + `fetchDeviceToken`） | 复用了同一 SDK 给 Python 网关取 token（见 [2.3](#23-设备风控头提供器)） |
| channelId = `109144` | `app_source/cli/product.json` → `turingSdk.channelId` | `turing_helper.js` 默认 channelId |
| 签到链路 | `main/tar.js` `claimDailyCheckin` → `POST /v2/billing/meter/daily-checkin` | 定时任务直接打该端点（见 [五](#五每日签到定时任务daily_checkin)） |
| RPC 通道 | `main/contract.js` `AUTH_RPC_CHANNELS`：`auth:getCheckinStatus` / `auth:claimDailyCheckin` | 仅桌面端内部用，反代走后端 HTTP 直连，不依赖 IPC |

---

## 二、逆向反代核心（workbuddy2api 网关）

### 2.1 架构

```text
客户端 (OpenAI/Anthropic SDK)
        │  /v1/chat/completions | /v1/responses | /v1/messages
        ▼
converter.py  (FastAPI)
        │  ├─ 注入鉴权头（Authorization / X-User-Id / X-Enterprise-Id / X-Tenant-Id / X-Domain / X-Device-Token）
        │  └─ 协议适配（OpenAI Chat ↔ Responses ↔ Anthropic Messages ↔ 腾讯 /v2/chat/completions）
        ▼
腾讯后端  https://copilot.tencent.com/v2/chat/completions
```

后端 `copilot.tencent.com` 本身走标准 OpenAI `chat/completions` 协议（含原生 `tools` / `tool_calls` / SSE 流式），转换器只在本地 `/v1/*` 与后端 `/v2/*` 之间做路径映射与透传。token 临近过期时自动调 `/v2/plugin/auth/token/refresh` 刷新并回写 `.info` 登录文件。

### 2.2 支持的端点

| 端点 | 说明 | 状态 |
|------|------|------|
| `POST /v1/chat/completions` | OpenAI Chat（流式） | 已支持 |
| `POST /v1/responses` | OpenAI Responses（适配 Codex CLI，默认做投影压缩） | 已支持 |
| `POST /v1/messages` | Anthropic Messages（适配 Claude Code / CC Switch） | 已支持 |
| `GET /v1/models` | 实时拉取后端模型，失败回退内置列表 | 已支持 |
| `GET /v1/balance` | 当前账号积分额度 | 已支持 |
| `GET /health` | 健康检查（含余额摘要） | 已支持 |

### 2.3 设备风控头提供器

反代的全部后端请求（含签到、对话）都会注入 `X-Device-Token`，来源是复用桌面端的 **Turing Shield SDK 原生模块**：

- `turing_helper.js`（项目根，Node）：`require()` 桌面端 `app.asar.unpacked/native/turing-sdk`，`configure(channelId, productName, productVersion)` 后 `fetchDeviceToken()`，向 stdout 输出 `{"token":"v3:..."}`。
- `admin/turing_token.py`（Python）：`subprocess` 调 `turing_helper.js`，进程内缓存 10 分钟，失败返回 `None`（调用方优雅降级，**不影响主流程**）。

可通过环境变量覆盖路径 / channelId：

```bash
WORKBUDDY_TURING_SDK_DIR       # SDK 目录（自动发现本机 WorkBuddy 安装位置；若安装目录特殊可显式指定以覆盖自动发现）
WORKBUDDY_TURING_CHANNEL_ID    # 默认 109144
WORKBUDDY_PRODUCT_NAME         # 默认 WorkBuddy
WORKBUDDY_VERSION              # 默认 2.0.0
```

> 若本机没装桌面端或 SDK 不可用，`get_headers()` 自动降级为不带 `X-Device-Token`，功能仍可跑，但敏感请求更易被风控识别。

### 2.4 三个协议适配器

- `responses_adapter.py` —— OpenAI Responses ↔ Chat 适配
- `anthropic_adapter.py` —— Anthropic Messages ↔ Chat 适配
- `responses_projection.py` —— Codex / agent 请求投影压缩（投影前后消息数 / 字符数 / tool schema 压缩量）
- `desensitize.py` —— 运行时文本压缩与零宽脱敏（去安全风险词，降低腾讯审核拦截率）

---

## 三、多账号代理共享平台（admin）

一个 **sub2 风格的反代理管理大屏**：把多个 WorkBuddy 账号集中管理，在还有额度的账号之间自动切换，并给不同用户发独立 API Key、按 Key 限额，超额直接拒绝；同时内置一整套**号池自动化运营**能力。

### 3.1 能力

**代理共享**

- **批量上传账号**：把桌面端 `.info` 登录文件原文（或数组 / 逐行）批量导入，存进 MySQL
- **账号池自动切换**：每次请求从「启用 + 还有剩余额度」的账号里挑选（默认剩余最多优先，可切 LRU）
- **查余额 / 刷新**：后台随时看每个账号总积分、剩余额度，并触发实时刷新
- **API Key 管理**：后台创建 Key 给别人用，可设每个 Key 的积分上限
- **模型分组**：把模型划进分组，Key 绑定分组后**只能调用组内模型**（越权返回 `403 model_not_in_group`）
- **配额拦截**：Key 已用积分 ≥ 上限时，代理直接返回 `402 {"error":{"message":"积分已耗尽","type":"quota_exceeded"}}`
- **用量记录与统计**：每次调用落 `usage_logs`，可按 Key / 账号 / 模型 / 端点追溯，并出积分与 token 的消耗趋势图表。**默认统计近 1 天**，支持按 Key / 模型 / 端点筛选，筛选后卡片与图表同步变化 —— 见 [3.7](#37-使用记录默认近-1-天可按-key--模型筛选)

**号池自动化运营**（纯协议实现，不需要桌面端开机 / CLI 常驻）

- **每日签到领积分**：自动为号池内每个账号领取每日签到奖励；已领自动跳过，活动下线自动停手保护账号 —— 见 [五](#五每日签到定时任务daily_checkin)
- **成长计划任务自动化**：拉取任务列表 → 自动参与 → 自动完成 → **自动领取积分**，全流程闭环，支持批量一键执行 —— 见 [六](#六成长计划任务growth)
- **猫猫旅行**：同意协议 → 首次领养（+300）→ 派猫 → 到站领奖，支持批量
- **定时调度**：内置调度器，签到 / 刷余额 / 同步模型列表 / 拉取成长任务列表均可按间隔自动跑，后台可增删改查与「立即运行」。**运行结果是人话摘要**（今天签到领了多少积分、哪个账号没成功、原因是什么），不是一坨 JSON —— 见 [3.8](#38-定时任务结果人话摘要)

> 自动化请求全部走**正常业务接口**（正常 200 响应、免费模型、最小输出），不伪造畸形请求，详见 [6.4](#64-为什么不用直接发完成包--直接发事件)。

### 3.2 稳定性设计

> 详细原理、失效模式与官方源码依据见 [第十章](#十稳定性与流量治理限速--并发--保活)。这里只列位置索引。

| 机制 | 实现位置 | 说明 |
|------|----------|------|
| **连接池** | `converter.py` / `admin/backend.py` | `httpx.Limits(max_connections=100, max_keepalive_connections=20)`，减少 TLS 握手 |
| **分级超时** | `admin/routers/proxy.py::_stream_timeout` | connect/read/write/pool 四项分设。**流式绝不设总时长**；read 承担 SSE 静默监控 |
| **账号轮换** | `admin/routers/proxy.py` | 轮换次数可配（`ADMIN_POOL_MAX_ROTATE`）；错误按分类自动换号/换模型 |
| **在途租约** | `admin/pool.py::InFlight` | 单账号在途上限，占满的号不参与选号；`finally` 兜底释放 |
| **会话粘性** | `admin/pool.py::StickyRouter` | 同会话固定同账号（TTL 滚动续期），成功后重绑到实际成功的号 |
| **加权选号** | `admin/pool.py::weighted_pick` | 三因子权重 + Top-5 短名单抽签（`ADMIN_ACCOUNT_SELECT=weighted`） |
| **错误分类** | `_classify_error` | 6004 模型级 / 11102 无此模型 / 11140 / 14017 / 12153 / 429 / 402 / WAF 403 分层判定 |
| **错误处置** | `_apply_account_policy` + `_note_success` | 每类错误各自的恢复依据；冷却取「或门」；**成功即清零连败计数** |
| **熔断 / 降权** | `breaker_until` / `degrade_until` | 连续失败按指数退避熔断；连败降权临时出池 |
| **模型级冷却** | `AccountModelCooldown` 表 | `(账号, 模型)` 粒度：6004 只冷却该模型，账号对别的模型照常可用 |
| **WAF IP 闸** | `admin/pool.py::WafIpGate` | 60s 内 ≥2 个不同账号 403 → 判定出口 IP 被拦，停止轮转 |
| **轮转退避** | `admin/pool.py::backoff_after_ms` | `500ms·2^n` 封顶 8s，再 ±25% 抖动（打散重试聚团） |
| **防撞号** | `admin/pool.py::RecentPick` | 100ms 内存窗口（不再每次选号都 commit 数据库） |
| **token 保活** | `admin/scheduler.py::run_keepalive_tokens` | 每日整点串行节流刷新全部活跃账号，保登录态存活 |
| **状态持久化** | `accounts` 表 + `init_db` 迁移 | 冷却/熔断/连败/禁用原因直接落库，进程重启不丢失 |
| **凭证续期** | `converter.CredentialManager._refresh` | token 临近过期自动刷新，刷新失败在代理层按分类处置 |
| **请求级表格日志** | `_log_chat_row` | 每个 `/v1/chat/completions` 请求出口打印 `seq / TTFB / uid / tokens / latency / error_kind` |

### 3.3 技术栈

- 后端：**FastAPI + SQLAlchemy 2.0 + MySQL 8（pymysql）+ Redis**
- 前端：**纯 HTML + TailwindCSS + FontAwesome**（CDN，无需构建），单页管理后台
- 鉴权：后台 JWT（HS256）；代理 API Key 用 SHA-256 存储，明文仅创建时展示一次

### 3.4 路由总览

| 模块 | 接口 | 说明 |
|------|------|------|
| 登录 | `POST /api/login` | 返回 JWT（放 `X-Admin-Token`） |
| 账号 | `GET/POST /api/accounts` · `POST /api/accounts/batch` | 账号列表 + 汇总 / 新增单个 / 批量导入 |
| 账号 | `POST /api/accounts/{id}/refresh` · `PATCH/DELETE /api/accounts/{id}` | 刷新余额 / 改状态 / 删除 |
| 账号 | `POST /api/accounts/{id}/cat-travel` · `POST /api/accounts/cat-travel/batch` | 猫猫旅行（单个 / 批量） |
| Key | `GET/POST /api/keys` · `PATCH/DELETE /api/keys/{id}` | Key 列表（脱敏）/ 创建 / 改限额 / 改分组 / 停用 / 吊销 |
| 分组 | `GET/POST /api/groups` · `PUT/DELETE /api/groups/{id}` | 模型分组 CRUD（Key 绑定分组后限用组内模型） |
| 统计 | `GET /api/stats/usage?days=&granularity=day\|hour` | 用量统计：总览 / 按模型 / 按端点 / 按 Key / 趋势 |
| 任务 | `GET/POST /api/schedules` · `PATCH/DELETE /api/schedules/{id}` · `POST /api/schedules/{id}/run` | 定时任务 CRUD / 立即运行 |
| 成长任务 | `GET /api/growth/tasks` · `GET /api/growth/accounts/{id}/tasks` | 全量任务列表（不绑账号）/ 单账号任务详情 |
| 成长任务 | `POST /api/growth/accept` · `POST /api/growth/run` · `POST /api/growth/claim` | 参与 / 自动完成 / 领奖 |
| 成长任务 | `GET /api/growth/plans` | 任务分级策略表（哪些能自动完成及依据） |
| 用量 | `GET /api/usage` · `GET /api/logs/usage` | 用量汇总 / 明细 |
| 代理网关 | `POST /v1/chat/completions` · `GET /v1/models` | 带 Key 校验 + 配额 + 记账 |
| 代理网关 | `POST /v1/responses` | OpenAI Responses（适配 Codex CLI，默认做投影压缩） |
| 代理网关 | `POST /v1/messages` · `POST /v1/messages/count_tokens` | Anthropic Messages（适配 Claude Code / CC Switch） |
| 后台页 | `GET /admin` | 管理大屏静态页 |

> 上面三条 `/v1/*` 网关路由**共用同一批 Key、同一套配额与用量记账**，账号都从号池自动挑选；
> 只是入口协议不同（Chat / Responses / Anthropic）。注意 `base_url` 的约定不一样：
> OpenAI 系客户端填 `http://<host>:8790/v1`，Anthropic 系（Claude Code）填 `http://<host>:8790`——
> 两种 SDK 都会自己拼后面的路径。

### 3.5 环境变量（admin）

`ADMIN_DATABASE_URL` · `ADMIN_REDIS_URL` · `ADMIN_BACKEND` · `ADMIN_USERNAME` · `ADMIN_PASSWORD` · `ADMIN_JWT_SECRET`（≥32 字节）· `ADMIN_JWT_EXPIRE_HOURS` · `ADMIN_COST_PER_TOKEN` · `ADMIN_ACCOUNT_SELECT`（`remain` / `lru` / `weighted`）· `ADMIN_PORT` · `ADMIN_CLIENT_AUTH_DIR`

流量治理（详见 [第十章](#十稳定性与流量治理限速--并发--保活)，完整清单见 `.env.example`）：

- `ADMIN_POOL_MAX_IN_FLIGHT`（默认 `3`，`0`=不限）—— 单账号最大在途请求数
- `ADMIN_POOL_MAX_ROTATE`（默认 `3`）—— 单请求最多换号次数
- `ADMIN_SESSION_STICKY`（默认 `1`）· `ADMIN_SESSION_STICKY_TTL`（1800）· `ADMIN_SESSION_STICKY_GC`（300）
- `ADMIN_POOL_SOFT_RATE` / `ADMIN_POOL_SOFT_RATE_MAX`（600 / 7200）—— 429 冷却基数与封顶
- `ADMIN_POOL_REQUEST_RATE` / `ADMIN_POOL_REQUEST_RATE_MAX`（20 / 120）—— **模型繁忙**（`14003`）时该「账号×模型」对的冷却基数与封顶。远小于上一行是有意的：见 [10.4.1](#1041-事故复盘一次-14003-抖动如何变成-10-分钟全站-503)
- `ADMIN_POOL_BREAKER_THRESHOLD` / `_COOLDOWN` / `_COOLDOWN_MAX`（5 / 600 / 21600）—— 熔断
- `ADMIN_POOL_SESSION_DEAD_THRESHOLD`（默认 `3`）—— 连续几次 12153 才禁用账号
- `ADMIN_POOL_MODEL_SOFT_RATE`（600）· `ADMIN_POOL_MODEL_BLOCK`（21600）—— 模型级冷却 TTL
- `ADMIN_STREAM_CONNECT_TIMEOUT`（15）· `ADMIN_STREAM_IDLE_TIMEOUT`（180）· `ADMIN_STREAM_WRITE_TIMEOUT`（60）· `ADMIN_STREAM_POOL_TIMEOUT`（20）
- `ADMIN_KEEPALIVE_ENABLED`（`1`）· `ADMIN_KEEPALIVE_HOURS`（`22`，可逗号分隔多个）· `ADMIN_KEEPALIVE_ACCOUNT_GAP`（`0.8`）

> ⚠️ `ADMIN_STREAM_IDLE_TIMEOUT` 是**静默**上限（有数据就续期），不是响应总时长。
> 不要试图把它改成「总超时」语义 —— 那会掐断长时间推理的活跃流（见 §10.7）。

Anthropic 端点（`/v1/messages`）相关：

- `ADMIN_ANTHROPIC_MODEL_OPUS` / `ADMIN_ANTHROPIC_MODEL_SONNET` / `ADMIN_ANTHROPIC_MODEL_HAIKU` —— Claude 的模型名按这三个档次映射到白名单模型，默认 `deepseek-v4-pro` / `glm-5.2` / `glm-5.3-flash`。**不要设成 `auto`**：本后台的 `auto` 语义是「取第一个启用的模型」，在 20+ 个模型里可能挑到不适合写代码的，甚至图像模型。
- `ADMIN_ANTHROPIC_DESENSITIZE`（默认 `1`）—— harness 脱敏开关，见 §6 说明，**关掉基本发不出去**
- `ADMIN_ANTHROPIC_NO_COMPACT`（默认 `0`）—— 只做零宽脱敏、跳过 harness 压缩

内嵌 `/gw` 网关相关：

- `CONVERTER_API_KEY` —— **必设**。`converter._check_auth()` 是 `if not key: return`，留空等于完全不鉴权，而服务默认监听 `0.0.0.0`
- `CODEBUDDY_AUTH_DIR` —— converter 读取桌面端凭据的目录。**注意与 `ADMIN_CLIENT_AUTH_DIR` 是两个不同的变量**：后者给后台「扫描本机 / 注入本机」用。以 Windows 服务（LocalSystem）方式运行时两者都必须写**绝对路径**，否则 `%LOCALAPPDATA%` 会解析到空目录
- `CONVERTER_DESENSITIZE` · `CONVERTER_LOG`

### 3.6 已知限制

- 账号凭据（`.info` 原文）以明文存于 MySQL，生产环境请加密存储或限制库访问
- 后端未回传 `credits` 时，按 `completion_tokens × COST_PER_TOKEN` 估算扣费（经验值）
- 配额扣减在流式结束后的 `finally` 里提交，高并发下非严格原子（极端竞态可能短暂超额）
- 余额刷新受腾讯后端限流影响（约每日 15:12 UTC+8 重置窗口），刷新失败余额保持不变

### 3.7 使用记录：默认近 1 天，可按 Key / 模型筛选

**默认窗口是「近 1 天 + 按小时」**，不是 14 天。

理由是使用场景：打开「使用记录」时最常问的是「**今天**跑得怎么样 / 刚才那波为什么慢」。
默认 14 天会把当天的异常**稀释**进两周的曲线里 —— 今天积分翻倍，在 14 天的图上
只是一个看不出的小凸起。要长期趋势再手动切 7/30/90 天。

筛选维度：

| 维度 | 参数 | 说明 |
|------|------|------|
| 时间窗 | `days` | 1 / 3 / 7 / 14 / 30 / 90（默认 1） |
| 粒度 | `granularity` | `hour` / `day`（默认 hour） |
| **按 Key** | `key_id` | 某个 Key 今天花了多少 |
| **按模型** | `model` | 某个模型的消耗与请求数 |
| 按端点 | `use_case` | `chat-completion` / `responses` / `messages` |
| 按账号 | `account_id` | 追溯到具体账号 |

**筛选是全局生效的**：卡片、三个分布图、趋势图走的是**同一份 where 条件**
（代码里只构造一次 `filters` 列表，所有查询都过 `_f()`）。这一点是刻意的 ——
分开写条件极易出现「卡片数字和图表对不上」这种自相矛盾的页面。

下拉选项来自 `/api/stats/usage/options`，**只列窗口内真实出现过的** Key / 模型 / 端点。
否则下拉里会塞满从没用过的模型，选中后筛出空结果，用户会以为功能坏了。

两个刻意的边界处理：

- **筛不到结果时给解释**，不是留一片空白：会说明「当前筛选条件下没有记录」
  并给一个「清除筛选」的链接，让用户区分「本来就没量」和「被自己筛没了」。
- **换时间窗后失效的选项自动回落**：原来选的模型在新区间里没了，选择会重置为
  「全部」而不是保留一个永远筛不出东西的隐藏条件。

概览卡片额外给出**失败请求数**（`总请求数（含失败 N）`）：只报成功数会让人
误以为请求没发出去，而失败恰恰是最该被看见的。

### 3.8 定时任务结果：人话摘要

以前「上次结果」是一坨 JSON，得自己去数：

```json
{"task":"daily_checkin","claimed":6,"skipped_already":7,"failed":2,"errors":["acc3:活动已结束","acc9:无领取资格"]}
```

现在直接给你要的答案：

> 今日新领 6 个账号，共 +42 积分，7 个今日已领过，2 个未领成功：
> 177\*\*\*\*5501（活动已结束）；修猫（无领取资格）

实现上分两层：

1. **后端给 `summary`**：每个任务自己写一句人话（`_checkin_summary` /
   `_growth_summary` 等），并附 `detail` 逐账号明细 `{account, ok, credit, reason}`；
2. **前端优先用 `summary`**，展开详情时把 `detail` 渲染成表格（✓/✗ + 账号 + 积分 + 原因），
   原始 JSON 收进折叠区。

**顺带修掉一个真 bug**：原来写库是 `json.dumps(...)[:500]`。
500 字符一截，JSON 就成了非法字符串，前端 `JSON.parse` 直接失败，
于是兜底显示原始文本 —— 这正是「只显示一堆 json 数据」的成因之一。
现在改为 `_dump_result()`：**绝不截断 JSON 结构**，超长时改为逐级丢弃明细数组
（2000 → 50 → 20 → 5 → 0 条）并标记 `detail_truncated`，保证任何情况下都是合法 JSON。
`last_result` 本身就是 TEXT 列，容量不是问题。

> 同一个截断 bug 在**手动「立即运行」**那条路径上也有（`routers/schedules.py`），
> 两条路径现已统一走 `_dump_result()`。手动执行失败也会落库，
> 否则界面还会显示上一次的成功结果，让人以为这次也成功了。

**账号标签做脱敏**：结果里用 `177****5501` 而不是完整手机号。
既够辨认是哪个号（正是「哪个账号没领成功」需要的信息），
又不把完整号码写进会被截图、会随日志流转的地方；没有名字才回落到 `账号#12`。

---

## 四、环境安装与项目运行

### 4.1 前置依赖

| 依赖 | 用途 | 版本 |
|------|------|------|
| Python | 运行 converter / admin | 3.10+（推荐 3.12） |
| Node.js | 设备风控头 `turing_helper.js`（require 桌面端 SDK） | 任意 LTS |
| MySQL | admin 账号池 / 用量库 | 8.x，默认 `root/root`，库名 `workbuddy_admin` |
| Redis | admin Key / 配额缓存 | 默认 6379 |
| WorkBuddy 桌面端 | 提供登录态 `.info` 与 Turing SDK | 已登录 |

> 本项目自带 managed 隔离环境：`C:\Users\Administrator\.workbuddy\binaries\python\envs\default\Scripts\python.exe`（已装全部依赖）。`main.py` 启动时会自动检测到缺包并切换过去。

### 4.2 安装依赖

```bash
# 用仓库自带 venv（推荐）
"C:/Users/Administrator/.workbuddy/binaries/python/envs/default/Scripts/python.exe" -m pip install -r requirements.txt

# 或自建虚拟环境
python3 -m venv .venv && source .venv/bin/activate && pip install -r requirements.txt
```

`requirements.txt`：`fastapi` · `uvicorn[standard]` · `httpx` · `sqlalchemy>=2.0` · `pymysql` · `redis` · `python-multipart` · `PyJWT` · `cryptography`

### 4.3 运行方式（三种）

#### 方式 A：单端口一体化（推荐生产 / 共享）

`python main.py` 一个进程同时拉起：管理后台 + 托管网关 + 内嵌 converter，全部走 **8790** 单端口：

```bash
# 前台常驻
python main.py
# 或指定监听
python main.py --host 0.0.0.0 --port 8790
# 端口被别的程序占用时，强制结束它再启动
python main.py --restart
```

**重启 = 再跑一次，不用先手动杀进程。** `main.py` 在 bind 之前会先看端口：

| 占用者 | 行为 |
|--------|------|
| 端口空闲 | 正常启动 |
| **本项目自己的旧实例**（uvicorn `admin.server:app` / 项目路径 + 入口脚本） | **自动结束旧实例并接管**，等价于一次重启 |
| 别的程序 | 报错退出并打印它的 pid 与命令行，**不动它**；确认要停才加 `--restart` |

为什么要自动接管：实测中宝塔「重启」是 stop → sleep(1) → start，
若旧进程优雅退出略慢于 1 秒，端口就仍被占着；更常见的是停止时只 kill 了
`main.py` 而没带走它派生的 uvicorn 子进程，端口被这个孤儿一直占着，
面板于是显示「启动失败」，pid 文件记的还是已死的进程 —— 之后每次「停止」
都杀不掉真正在跑的 uvicorn，形成死结。主动接管可自愈这种情况。

> 为什么默认**不**杀别的程序：判据一旦放宽就可能误杀无关进程（本机实测过
> 误伤编辑器与终端），误杀的代价不可逆。所以判据很严：要么 uvicorn 目标是
> `admin.server:app`，要么「命令行由 python 解释器执行 + 含项目根目录 + 含本项目入口脚本」。
> 调用本服务的**祖先进程**（终端 / 启动器）永不结束。

**监听端口优先级：命令行 `--port` > `.env` 的 `ADMIN_PORT` > 默认 `8790`。**

原实现写成 `os.getenv("ADMIN_PORT", str(args.port))` —— 环境变量优先，
于是 `.env` 里的 `ADMIN_PORT=8790` 会**静默吞掉** `--port 58634`，
命令行上指定的端口不生效、仍然去起 8790，接管逻辑随即把**线上实例**杀掉。
本机实测这误停了线上服务两次（都是想用 `--port` 起一个测试实例时）。
命令行是操作者的**当次明确意图**，必须压过配置文件里的常驻值。

> 连带教训：任何会「自动接管端口」的脚本都不要拿线上端口做端到端测试。
> `scripts/verify_port_takeover.py` 现在用临时高位端口自建实例，
> 并断言线上端口在测试前后占用者一致。

启动后：

- 管理后台：`http://127.0.0.1:8790/admin`
- 托管网关（带 Key 配额）：`http://127.0.0.1:8790/v1/chat/completions`
- 内嵌网关（桌面登录态 / responses / messages）：`http://127.0.0.1:8790/gw/v1/...`

`Ctrl+C` 优雅关闭；子进程异常退出则整体退出（避免孤儿进程）。启动时会告警弱密钥 / 弱口令，部署请覆盖 `ADMIN_JWT_SECRET` / `ADMIN_PASSWORD`。

一键脚本（本机已配好）：`start_admin.bat`（强密码 + 固定 JWT secret 的一键启动）。

#### 方式 B：仅本机桌面端直连（converter 独立）

适合个人使用，直接吃桌面端实时登录态，额外支持 `/v1/responses`、`/v1/messages`、`/v1/balance`：

```bash
python converter.py --desensitize --log converter.log          # 默认 127.0.0.1:8787
python converter.py --port 9000 --api-key mysecret             # 自定义端口 / 本地鉴权
```

一键脚本：`start_converter.bat`。

#### 方式 C：仅管理后台（admin 独立）

```bash
python -m uvicorn admin.server:app --host 0.0.0.0 --port 8790
```

### 4.4 同步登录态到服务器

`scripts/` 之外，根目录 `sync_auth.py` + `sync_auth.bat`：把本机最新桌面端登录态同步到服务器（依赖 managed python 的 paramiko），双击 `sync_auth.bat` 即可。

### 4.5 Docker

容器拿不到桌面端 auth 文件，需把宿主机登录态目录挂进去。改 `docker-compose.yml` 里的 auth 挂载路径后：

```bash
docker compose up -d --build
```

或单容器：

```bash
docker build -t workbuddy2api .
docker run -d --name workbuddy2api -p 8787:8787 \
  -v ~/Library/Application\ Support/CodeBuddyExtension/Data/Public/auth:/data/auth:ro \
  -e CODEBUDDY_AUTH_DIR=/data/auth \
  workbuddy2api
```

相关环境变量：`CODEBUDDY_AUTH_DIR` · `CODEBUDDY2OPENAI_KEY` · `CODEBUDDY2OPENAI_LOG`。

### 4.6 converter 命令行参数

| 参数 | 默认值 | 说明 |
|------|------|------|
| `--host` | `127.0.0.1` | 监听地址 |
| `--port` | `8787` | 监听端口 |
| `--api-key` | 无 | 给本地客户端加一层鉴权 |
| `--log` | 无 | 记录请求与响应日志 |
| `--desensitize` | 关 | 压缩运行时提示、去掉 tool description、零宽脱敏高风险关键词 |
| `--no-compact` | 关 | 配合 `--desensitize`，保留更完整的原始 system prompt |
| `--skip-check` | 否 | 跳过启动预检 |

---

## 五、每日签到定时任务（daily_checkin）

基于 [一](#一逆向工程解包-workbuddy-桌面端源码app_source) 的逆向结论实现：自动给所有活跃账号领「每日 100 积分」，并自带 **风控保护**。

### 5.1 风控保护

- 全部请求经 `CredentialManager` 注入 `X-Device-Token`（与桌面端一致）
- 任务可配 **「下次停止领取」时间 `stop_after`**：到达后直接跳过，不再发领取请求，避免活动下线后继续请求触发上游风控
- 若某账号领取返回 `EventEnded(1003)`，自动把 `stop_after` 设为今天，后续不再尝试

### 5.2 实现位置

- `admin/backend.py` — `AccountSession.get_checkin_status()` / `claim_daily_checkin()`（用软请求，业务码非 0 不抛异常）
- `admin/scheduler.py` — `run_daily_checkin(db, schedule)`：超 `stop_after` 跳过；遇 `EventEnded` 自动置 `stop_after=今天`
- `admin/models.py` — `Schedule.stop_after` 字段（「下次停止领取」）
- `admin/routers/schedules.py` — `daily_checkin` 接入 `TASK_CHOICES` + `stop_after` 读写
- `admin/db.py` — `init_db()` 补 `schedules.stop_after` 列迁移

### 5.3 配置定时任务

**后台 UI（推荐）**：管理后台「定时任务」页 → 「新建」→ 任务类型选 **每日签到领取积分**，间隔填 `1440`（每天），启用即可。选该类型会出现 **停止领取时间** 输入框（datetime-local），留空=不限制（活动结束会自动停止），填上活动结束时间更安全。

**API 示例**：`stop_after` 建议设为活动结束时间（来自 `checkin-activity-status` 的 `end_time`）：

```bash
curl -X POST http://127.0.0.1:8790/api/schedules \
  -H "X-Admin-Token: <admin_jwt>" \
  -H "Content-Type: application/json" \
  -d '{"name":"每日签到领积分","task":"daily_checkin","interval_minutes":1440,"enabled":1,"stop_after":"2026-09-15T23:59:59"}'
```

**默认即自动签到**：`start_scheduler()` 在后台启动时会 `ensure_daily_checkin(db)`——若实例里没有任何 `daily_checkin` 任务，会自动补一个「每日签到领取积分」（启用、每天）的任务。所以全新部署或已有实例都会自带定时签到配置，无需手动建。已领过当天的账号会被跳过，不会重复领取、不会误发请求。

### 5.4 验证

后台「定时任务」页点该任务的 **执行**，结果列会直接给出人话摘要，例如
「今日已领 6 个账号，共 +42 积分；2 个未领成功：xxx（活动已结束）」——
不需要再去翻 JSON。详见 §7.4。

已验证：活动 `开学季`，`end_time=2026-09-15 23:59:59`；未领账号各领到 100 积分，已领账号自动跳过；今日领完后调度器 `claimed=0, skipped_already=5`（不重复领、不误发请求）；`stop_after` 过期直接跳过。

---

## 六、成长计划任务（growth）

把 WorkBuddy「成长中心」的任务做成可自动完成的模块：拉列表 → 参与 → 自动完成 → 领奖，全流程纯 HTTP，不需要桌面端在线、不需要 CLI、不占用本地登录态。

### 6.1 协议机制（逆向结论）

**进度不是靠独立上报接口驱动的。** 这是整件事最关键的一点，也是最容易走错的方向。

任务进度由**模型请求体里的 `extra_vars.growthEvent`** 驱动。也就是发一个正常的对话请求，在 body 里附带事件声明：

```http
POST /v2/chat/completions
Content-Type: application/json

{
  "model": "hy3",
  "stream": true,
  "max_tokens": 1,
  "messages": [{"role": "user", "content": "hi"}],
  "extra_vars": {
    "growthEvent": "[{\"eventCode\":\"chat_request_send\",\"id\":\"<会话ID>\"}]"
  }
}
```

要点：

- `growthEvent` 是 **JSON 字符串**（不是数组对象），服务端只按 `eventCode` 记账
- 事件声明在**请求体**里，不在 header 里——早期只在 header / 独立上报接口找，方向是错的
- 服务端**不校验模型是否真的被调用**，只认事件名
- 独立上报接口 `POST /v2/report` 虽然返回 200，但**不驱动任务进度**

### 6.2 完整状态机

```text
not_accepted ──accept──► accepted ──触发──► in_progress ──► completed ──claim──► claimed
```

**两个必须遵守的点：**

1. **必须先 `accept`**，否则进度完全不累计。这曾导致大量错误的负面结论——不是方法不成立，是任务没参与。
2. **`completed` ≠ `claimed`**。完成只是达标，奖励要**另外调一次 claim** 才真正到账。

### 6.3 三个接口的形态

| 步骤 | 请求 |
|------|------|
| 拉列表 | `GET /v2/activity/growth/tasks` |
| 参与 | `POST /v2/activity/growth/tasks/accept`，body `{"task_codes":["chat_5", ...]}` |
| 触发 | `POST /v2/chat/completions`，body 带 `extra_vars.growthEvent` |
| 领奖 | `POST /activity/growth/tasks/{task_code}/claim`，空 body |

注意领奖路径与其它 growth 接口**形态不同**：**没有 `/v2` 前缀**，任务码在路径里。参与接口只接受上面这一种 body 形态（复数 + 数组），另外试过的 12 种写法一律 `400 invalid request`。

### 6.4 为什么不用「直接发完成包 / 直接发事件」

这是**已验证不可行**的方案，本模块**没有采用**：

- **直接发完成包、直接上报事件 → 任务不通过。** 已实测：`POST /v2/report` 返回 200 但不驱动进度；伪造完成态也无法让任务真正达标。
- 空 `messages` 等畸形请求虽然有时也能触发计数，但会产生 **HTTP 400 报错**。上游有报错日志审查，这类痕迹容易被发现并封堵。

本模块走的是**免费模型路线**，请求形态与正常对话**完全一致**：

- 用免费 0 倍率模型（`hy3`）+ `max_tokens=1`，**成本为 0**
- 返回 **HTTP 200**，上游日志里看不出任何异常
- 只有在个别任务确实需要特定模型时才换（见 6.5 的「模型体验」）

### 6.5 任务分级与实测结论

分级规则见 `admin/growth_plans.py`，分 **简单 / 简单(多次) / 复杂 / 跳过** 四级。下表是**逐条实测**的结果，不是推测：

**14 个可自动完成的任务**，单账号满额 **1400 积分**：

| 任务 | 分级 | 完成方式 | 实测 |
|------|------|----------|------|
| `chat_5` 对话 5 次 | 简单(多次) | `chat_request_send` × N | ✅ |
| `automation_1` 设置自动化 | 简单 | `automated_task_create_suc` | ✅ |
| `skill_1` 尝鲜技能 | 简单 | `skill_info` | ✅ |
| `Model_chat_GLM5.2` 模型体验 | 简单 | **真实调用 `glm-5.2`** | ✅ |
| `Library_read` 体验资料库 | 简单 | web 域 `web_element_click` | ✅ |
| `playbook_prompt` 探索灵感案例 | 简单 | billing 域 `playbook_prompt_send` | ✅ |
| `expert_5` 召唤 5 次专家 | 简单(多次) | 真实专家 id + `expert_actual_use` | ✅ |
| `template_5` 使用模板 | 简单(多次) | 真实场景 id + 模板事件 | ✅ |
| `Expert_team_use_3` 专家团 | 简单(多次) | `expert_type=team` 过滤后上报 | ✅ |
| `Expert_lighthouse` 轻量云专家 | 简单 | 关键词筛真实专家后上报 | ✅ |
| `Hp_Appearance` 和平精英主题 | 简单 | 真实主题 resourceKey | ✅ |
| `Buddy_App` 发现应用 | 简单 | 桌面指纹 buddyapp 五连事件 | ✅ |
| `Buddy_App_QQ` 企鹅教师助手 | 简单 | 同一组五连事件 | ✅ |
| `RichMeow_Chat` 桌面端对话 | 简单 | 桌面指纹 6 连对话事件链 | ✅ |
| `create_canvas` 设计创意模式 | 简单 | **真实调 Ardot MCP `create_design` 创建画布**，用返回的真实 fileId 上报遥测 | ✅ +300 |

**不做 / 跳过**：

| 任务 | 分级 | 原因 |
|------|------|------|
| `expert_5_paid` 付费召唤专家 | 复杂 | 付费任务，无收益 |
| `Expert_Philanthropy` 公益专家 | 复杂 | 需真实捐款动作，无法代做 |
| `black_cat` 夜猫子折扣 | 跳过 | 奖励为 0 |

> `create_canvas` 从「不做」变为「可自动」的过程见 [6.7](#67-对象-id-一律现拉绝不编造)。
> 关键点是：真实画布 id 完全可以拿到，只是**必须真的去创建画布**，而不是编一个 id。
> 已真机验证：账号 19 上报后成长中心显示 `create_canvas` **1/1、+300 分、已领取**。


**「模型体验」类任务**（`Model_chat_GLM5.2`）的完成条件是 **请求体里的 `model` 必须真的是 `glm-5.2`**，发事件包一律无效。用 `max_tokens=1` 最小输出调用一次即可，倍率 0.79、单次成本极低。

### 6.6 两类触发通道（关键区分）

任务**不是**都靠 `growthEvent`。实测有两类完全不同的通道，混用必然失败：

**通道 A：`growthEvent`（走 chat/completions）**

请求体里带 `extra_vars.growthEvent`，用免费模型 `hy3` 发一次最小请求即可。适用：`chat_5`、`automation_1`、`skill_1`。

**通道 B：真实业务事件（走 `POST /v2/report`）**

上游要的是**带完整业务字段的客户端事件**，且不同域的事件头形状不同，混用不计数：

| 域 | 用途 | 关键请求头 |
|----|------|-----------|
| `billing`（codebuddy.cn） | 常规业务事件 | CLI UA + `Origin`/`Referer` + `X-Domain`=账号域 |
| `chat`（copilot.tencent.com） | 市场/场景接口、桌面事件 | 桌面事件需注入**桌面指纹** |
| `web`（workbuddy.cn） | 浏览器行为 | 浏览器 UA + `Origin`/`Referer` + `x-client-platform: web` |

**桌面指纹**（`Buddy_App` / `RichMeow_Chat` 必需）：`ideName=WorkBuddy`、`extName=workbuddy-desktop`、`ideVersion` / `extVersion` / `commit` / `releaseDate` **均从本机安装包动态读取**（见 [10.10](#1010-客户端版本与逆向产物自动发现--自动产出)），其中 `machineId` / `sessionId` **由 uid 稳定派生**——同一账号每次都是同一台「设备」。频繁换设备反而是异常信号。缺这些头会被判为非桌面端来源，事件不计数。

> 关于任务说明里的「需升级到电脑端 5.5.3 或以上版本」「需下载并使用桌面端」：那是**客户端侧**的提示文案，**服务端只认上报的事件本身**。实测直接上报事件链即可完成，无需真的安装桌面端——这和换肤任务同理（能上报就能完成）。

### 6.7 对象 id 一律现拉，绝不编造

有一类任务要求事件里带**真实存在的对象 id**。这些 id 全部从官方接口现时拉取：

| 任务 | 来源接口 |
|------|----------|
| `expert_5` / `Expert_team_use_3` / `Expert_lighthouse` | `POST /v2/operation-platform/market/expert/list`（团队任务加 `expert_type=team` 过滤；不带该参数时 400 个专家里只有 1 个 team） |
| `template_5` | `GET /console/as/support/scenes`（16 个真实场景） |
| `Hp_Appearance` | `POST /v2/operation-platform/appearance/resources` |
| `create_canvas` | Ardot MCP `create_design`（见下） |

**拉不到就跳过该任务并如实报错，绝不退化成自造 id。** 伪造业务对象一旦被后端核对就会暴露。

#### `create_canvas`：不是不能做，是必须真的去做

早期结论是「事件里要自造 `wb-<ms>` 画布 id，属伪造业务对象，不做」。这个结论**只对了一半**：
真实画布 id 拿得到，只是必须**真的去创建一个画布**。

关键证据来自官方客户端源码（解包产物 `app_source/`，用后台
「设置 → 逆向产物 → 源码检索」或 `wb_asar.py search` 检索到的 ardot 遥测模块）：

1. 画布 id 的真实口径是**纯数字**——`/\bfileId\s+(\d+)\b/`、URL 路径 `/file/(\d+)`。
   所以 `ardot-file-xxxxxxxx` 或 `wb-1789870000000` 在**形状上**就不可能是真实画布 id。
2. 那个 id 来自 **Ardot MCP 工具 `create_design`**（appId `ardot/create_design`），
   而 MCP 工具由客户端 MCP Host 执行，**不是** chat/completions 服务端
   （官方 mcp-app-policy：「Host 不再 bootstrap tools/call」）。
   所以「发一条设计对话，等上游回 fileId」是走不通的——实测模型只会把
   `create_design` 调用**以文本形式**写出来，并明说「无法直接返回真实 fileId」。

因此正确做法是**自己当 MCP 客户端**（`AccountSession.create_ardot_canvas`）：

```text
0. 确保账号已绑定 Ardot（见下「两个坑」，未绑定的账号取不到 token）
1. 用账号凭据换 Ardot token
   GET {账号域}/v2/as/connector/oauth/ardot/accesstoken
   -> {"code":0,"data":{"access_token":"<JWT>","expire_at":...}}
   （依据官方 ardot/access-token.ts）
2. initialize + tools/call create_design  （https://ardot.tencent.com/mcp）
3. 取回真实纯数字 fileId（拿不到就如实失败，绝不补假的）
4. 用这个真实 id 上报 wbx_design_canvas_task_create / _open
```

> 参考项目 `workbuddy2api-panel` 在这里是自造 id（`"ardot-file-" + requestID[-8:]`）。
> 同一个项目在专家任务里却明确写过「自造 id 不计数」，属于自相矛盾的取巧。
> 我们走的是完整正路：**真的建一个画布，报一个真的 id**。

#### 两个必须踩对的坑（否则大部分账号直接失败）

**坑一：Ardot 绑定是「按账号」的，不是全局的。**

实测 15 个真实账号里**只有 5 个**天然已绑定，其余 10 个取 token 会返回：

```json
HTTP 422 {"code":10101,"msg":"access token not found"}
```

`/status` 显示 `not_connected`。**未绑定 = create_canvas 永远做不了**，
而报错信息看起来像是「网关取不到 token」，很容易被误判成代码 bug。

修法是照官方 `ardot-manager.ts` 的「影子账号」流程建绑定：

```text
POST {账号域}/v2/as/connector/oauth/ardot/connect?code=shadow_account_grant
  -> {"code":0,"data":{"access_token":"<JWT>", ...}}   # 绑定并直接给票
```

**坑二：`code` 必须是 query，不能当 JSON body 发。**

官方 `callConnectorOauthApi(method, path, deps, query, body, ...)` 的第 4 个
参数是 **query**。把它当 body 发会得到：

```text
HTTP 302 -> Location: .../agents/callback?code=10001&httpstatus=400
           &msg=authorization+code+empty
```

绑定建不起来。**这个坑很隐蔽**：HTTP 不是 4xx/5xx，而是一个 302 跳转，
如果只看状态码会以为「请求发出去了」。（实测踩过并修正。）

另外还有「半失效态」自愈：服务端记录还在但凭证失效时，`/connect` 返回
`409 user already connect`（同样是 302 回跳），重试多少次都一样，
**必须先 `POST .../revoke` 清掉记录再重绑**。官方的判据刻意放在「取票失败」
之后——`already connect` 本身不代表凭证坏了，记录在且票能取到就该原样放过，
此时 revoke 等于白删一个好绑定。

三点都实现在 `AccountSession.connect_ardot()` / `ensure_ardot_connected()`，
并且成长任务 runner 在执行画布任务前会**主动确保绑定**，所以不会再把
「账号未绑定」暴露成一条莫名报错。

#### 实测结果

| 账号 | 初始绑定 | 画布 id（真实） | 成长中心 |
|------|----------|------------------|----------|
| 19 | 已绑定 | `727757335678209` | 1/1、+300、`claimed` |
| 38（用户报障） | **未绑定** | `727781592533691` | 1/1、+300、`claimed` |
| 其余 13 个 | 10 未绑定 | 各自真实 id | 全部 1/1、`claimed` |

**最终 15/15 账号全部 `claimed`，Ardot 绑定 15/15 `connected`。**

#### 任务状态机：必须先「参与」

`create_canvas` 与其它成长任务一样是**两段式**，缺一段都不计数：

```text
not_accepted --accept--> accepted --完成行为--> completed --claim--> claimed
                  ↑                        ↑                    ↑
            不计进度，做了也白做     进度 1/1 但没领奖      奖励到账
```

- **`accept`**：`POST /v2/activity/growth/tasks/accept`，未参与时进度**不累计**；
- **`claim`**：`POST /v2/activity/growth/tasks/{code}/claim`，`completed` 不会自动到账。
  重复领返回 `already_claimed: true`、`credit: 0`（这是正常的，不是失败）。

Runner 两步都会做：执行前对 `not_accepted` 调 `growth_accept`，跑完后把
`completed` 的逐个 `growth_claim`（见 `admin/routers/growth.py`）。

怀疑某个账号没走完链路时，用后台「成长任务」页的进度与日志排查即可
（每个账号的 `accept` / 完成 / `claim` 状态都在页面上）。

### 6.8 新增任务会自动识别（模式匹配）

`TASK_PLANS` 只登记实测过的具体任务，但上游活动是滚动的：会新增同类任务，也会给同名任务换档位（如 `chat_5` → `chat_10`、`template_5` → `template_3`）。只靠精确匹配的话，新 code 会**静默落到「不做」**——明明能做却不做且不报错，这是最糟的失败方式。

因此 `plan_for()` 是三层决策：

```text
1. 精确表 TASK_PLANS       实测过的具体任务（含特殊形状），优先级最高
2. 模式规则 PATTERN_RULES  按上游命名规律匹配同类新任务与换档位
3. 都不中 -> MANUAL        宁可不做，也不盲发未验证的事件
```

已覆盖的模式：`chat_N` / `template_N` / `expert_N` / `Expert_team_use_N` / `Model_chat_<模型>` / `*appearance*`（换肤）/ `*lighthouse*` / `skill_N` / `automation_N`。

**验证**（用虚构的新 code 实测）：

| 模拟的新任务 | 是否自动命中 |
|--------------|--------------|
| `Hp_Appearance_2` / `Xx_Appearance` / `appearance_theme` | ✅ 命中换肤 |
| `Model_chat_GPT5` / `Model_chat_GLM4.6` | ✅ 命中模型体验 |
| `expert_10` / `expert_20` / `Expert_team_use_5` | ✅ 命中专家类 |
| `skill_5` / `skill_10` | ✅ 命中技能类 |
| `template_3` / `chat_10` / `automation_3` | ✅ 命中对应档位 |
| `SomethingWeird_99`（无规律） | ❌ 保持 MANUAL（符合预期） |

模式匹配**只调用已实测验证过的触发方式**；匹配不出来的一律维持 MANUAL——没人验证过的任务类型，猜错等于往上游灌垃圾事件。

### 6.9 串行执行（重要）

早期实现是「先把所有待办任务批量 `accept`，再逐个触发」，实测会出现 **「已 accept 但首次触发不计数」**——上游的参与状态是**异步落库**的，紧接着发事件会被判定为「未参与」而丢弃。

因此改为**每个任务独立走完整闭环，全程串行**：

```text
accept → 等待落库(_ACCEPT_SETTLE=3s) → 重新读取状态
       → 触发(每次间隔 _EVENT_GAP=1.2s) → 等待(_VERIFY_WAIT=1.5s) → 复查进度
```

账号之间另有 `_ACCOUNT_GAP=2s` 间隔。**请勿对同一账号并行执行**，否则会出现任务不成功。

### 6.10 使用方式

**后台界面**（推荐）：账号池页 → 点某行 🌱 图标打开任务弹窗，可看每个任务的难度 / 状态 / 进度 / 积分，支持单个完成、单个领奖、一键完成全部可自动化任务（完成后自动领奖）。工具栏「批量做任务」对全部 active 账号串行执行。

**接口调用**：

```bash
# 全量任务列表（不绑账号，任意可用登录态即可拉）
curl -H "X-Admin-Token: <jwt>" http://127.0.0.1:8790/api/growth/tasks

# 某账号的任务详情（<id> 为后台账号 ID）
curl -H "X-Admin-Token: <jwt>" http://127.0.0.1:8790/api/growth/accounts/<id>/tasks

# 完成可自动化任务（并自动领奖）
curl -X POST -H "X-Admin-Token: <jwt>" -H "Content-Type: application/json" \
     -d '{"account_ids":[<id>]}' http://127.0.0.1:8790/api/growth/run
curl -X POST -H "X-Admin-Token: <jwt>" -H "Content-Type: application/json" \
     -d '{"account_ids":[<id>]}' http://127.0.0.1:8790/api/growth/claim
```

**定时自动化**：默认自带两个任务，**每天自动把新任务做掉并领奖**。

| 任务 | 间隔 | 说明 |
|------|------|------|
| `refresh_growth_tasks` 每日更新成长任务列表 | 1440 分钟 | 刷新任务定义缓存，让分级与面板跟上上游活动变化 |
| `run_growth_tasks` 每日自动做成长任务 | 1440 分钟 | 遍历号池 → 先刷新列表 → 自动参与 → 触发完成 → **自动领奖** |

**两个任务的先后顺序是硬性要求**：执行必须排在刷新之后**至少 3 分钟**（`_GROWTH_MIN_AFTER_REFRESH = 180` 秒）。上游的任务定义与账号进度之间存在同步延迟，刷新后立刻执行会拿到旧数据，导致新任务识别不到或重复触发。调度器启动时会自动校正二者时间（`ensure_growth_schedules`），即便手工改过也会被顺延回正确区间。

执行任务与你手动点「批量做任务」走的是**同一套代码**（`run_accounts` / `claim_accounts`），因此串行规则与节流完全一致。筛选规则：

- 只做策略表里 `actionable` 的任务（需人工完成的自动跳过）
- `claimed` 的跳过 —— **幂等**，重复跑不会重复领、不会白发请求
- 单次失败只记录、不重试，避免对注定失败的任务反复请求

因此运营上了新任务，**当天就会自动完成并领到积分**，无需人工干预。同类新任务（含换档位）由[模式规则](#68-新增任务会自动识别模式匹配)自动命中。执行结果写入调度记录的 `last_result`：

```json
{"task":"run_growth_tasks","ok":true,"accounts":15,"tasks_done":14,
 "credit":3000,"energy":185,"failed_accounts":[],"elapsed_s":432.2}
```

### 6.11 实测结果

**分三批实测，可自动任务从 4 个逐步扩到 14 个**（15 个活动账号全量）：

```text
第一批  Library_read + playbook_prompt      +2100 积分
第二批  expert_5 / template_5 / Expert_lighthouse
        Expert_team_use_3 / Hp_Appearance    +2600 积分
第三批  Buddy_App / Buddy_App_QQ / RichMeow_Chat
                                             +3000 积分
总计                                         +7700 积分
```

每批跑完后，对应任务的**所有账号状态均为 `claimed`**（15/15）。所有上报均为 HTTP 200 / `code=0`，无一个 400。

单账号满额 **1700 积分**（15 个任务，其中 `Buddy_App_QQ` 为 50、`create_canvas` 为 300）。

> 注：账号标识已完全脱敏，不暴露任何手机号、UID 或昵称。

---

## 七、客户端接入

### Codex CLI（走 `/v1/responses`）

```toml
# ~/.codex/config.toml
[model_providers.workbuddy]
name = "WorkBuddy (via local converter)"
base_url = "http://127.0.0.1:8790/v1"   # 或 8787（独立 converter）
wire_api = "responses"
env_key = "CODEBUDDY2OPENAI_KEY"

[profiles.workbuddy]
model = "glm-5.2"
model_provider = "workbuddy"
```

```bash
export CODEBUDDY2OPENAI_KEY=any-value
codex --profile workbuddy "你的任务描述"
```

### Claude Code / CC Switch（走 `/v1/messages`）

两条路，按「要不要多账号轮换 + Key 配额」来选：

**A. 走共享平台（`8790`）—— 带 Key 校验、配额、用量记账，账号从号池自动挑选**

```json
{
  "workbuddy-admin": {
    "base_url": "http://127.0.0.1:8790",
    "api_key": "后台 API Keys 页创建的那把 sk-...",
    "model": "claude-sonnet-4-5-20250929"
  }
}
```

- **`base_url` 不要带 `/v1`**：Anthropic SDK 会自己拼 `/v1/messages`，填成 `.../v1` 会变成 `/v1/v1/messages`。（对比：OpenAI 系客户端要填 `.../v1`。）
- 模型名可以照抄 Claude 官方的 `claude-sonnet-4-5-*` 这类名字 —— 服务端会按 opus / sonnet / haiku 三档自动映射到白名单里的模型；也可以直接填 `glm-5.2` 这类真实模型名。
- **harness 脱敏默认开启**，无需额外参数。这一步不能省：Claude Code 的 system prompt 里有
  "DoS attacks / exploit development / credential testing" 这类**拒绝作恶的合规声明**，
  不做脱敏会被后端内容审核当成敏感内容整条拒绝，报错是极具误导性的
  `400 {"code":11128,"msg":"Illegal API invocation from an unapproved channel"}`。
  ⚠️ 排查提示：不脱敏时简单的 `"hello"` 请求**能通过**，只有真实 Claude Code 的完整 harness 才会被拦，
  所以**不要用 hello 请求验证这个端点**。

**B. 走本机直连（`8787`，`python converter.py`）—— 只用自己的桌面端登录态，无配额**

```json
{
  "DeepSeek-V4-Pro": {
    "base_url": "http://127.0.0.1:8787/v1/messages",
    "api_key": "",
    "model": "deepseek-v4-pro"
  }
}
```

- 模型名必须填腾讯后端真实模型名；不做自动映射
- 强烈建议开启 `--desensitize`

### 其它 OpenAI 兼容客户端（Cherry Studio / ZCode / LobeChat / NextChat / Open WebUI）

- Base URL：`http://127.0.0.1:8790/v1`（共享平台）或 `:8787/v1`（本机直连）
- API Key：留空，或填启动时 `--api-key` / 后台创建的 `wb-...` Key
- 模型名：`glm-5.2` / `deepseek-v4-pro` / `kimi-k2.7` / `auto` 等

```bash
curl -N http://127.0.0.1:8790/v1/chat/completions \
  -H "X-API-Key: 你的KEY" \
  -H "Content-Type: application/json" \
  -d '{"model":"glm-5.2","stream":true,"messages":[{"role":"user","content":"你好"}]}'
```

---

## 八、日志与排障

### 推荐启动

```bash
python converter.py --desensitize --log converter.log
```

### 日志能看到什么

每次请求带唯一 ID，常见：`REQUEST BODY` · `RESPONSES → CHAT BODY` · `RESPONSES PROJECTION` · `RESPONSE BODY` · `RESPONSE RAW SSE` · `⚠️内容审核拦截`。`RESPONSES PROJECTION` 会给出投影前后消息数 / 字符数 / tool schema 压缩量。

### 常见问题

- **找不到登录文件**：桌面端没登录，或登录目录不在默认路径（macOS `~/Library/Application Support/CodeBuddyExtension/Data/Public/auth`；Windows `%LOCALAPPDATA%\CodeBuddyExtension\Data\Public\auth`；Linux `~/.local/share/CodeBuddyExtension/Data/Public/auth`）。
- **401**：本地 401 = 启用了 `--api-key` 但客户端没带同 key；后端 401 = 腾讯 token 失效，重开桌面端登录。
- **响应慢**：换更快的模型如 `deepseek-v4-flash`。
- **被「敏感内容」拦截**：多为 agent runtime 文本触发（DoS / exploit / credential / sandbox / escalation / 竞争品牌词 / tool description 安全术语）。排查顺序：开 `--log` → 看 `REQUEST BODY` → Codex 看 `RESPONSES PROJECTION` → 开 `--desensitize` → 仍不稳试 `--desensitize --no-compact`。
- **签到 / 对话被风控**：确认本机装了桌面端且 `turing_helper.js` 能取到 token（`X-Device-Token` 已注入）。可 `python -c "from admin.turing_token import get_device_token; print(get_device_token())"` 验证。

---

## 九、项目结构

```text
workbuddy2api/
├── converter.py              # 内嵌网关主入口（FastAPI），挂载到 /gw/v1（main.py 下）
├── main.py                   # 一键单端口启动：管理后台 + 托管网关 + 内嵌网关
├── responses_adapter.py      # OpenAI Responses ↔ Chat 适配
├── responses_projection.py   # Codex / agent 请求投影压缩
├── anthropic_adapter.py      # Anthropic Messages ↔ Chat 适配
├── desensitize.py            # 运行时文本压缩与零宽脱敏
├── wb_install.py             # WorkBuddy 安装位置 / 版本号 / 风控配置自动发现（不写死盘符）
├── wb_asar.py                # 纯标准库的 Electron asar 读取/抽取/检索（不需要 Node/npm）
├── wb_envcheck.py            # 运行环境自检：Python/Node/依赖/桌面端/风控 SDK/MySQL/Redis
├── turing_helper.js          # Node：调用桌面端 Turing Shield SDK 取设备风控 token
├── sync_auth.py / .bat       # 同步本机登录态到服务器（本地用，不进仓库）
├── start_converter.bat       # 本机直连网关一键启动（本地用，不进仓库）
├── start_admin.bat           # 管理后台一键启动（强密码 + 固定 JWT secret；本地用，不进仓库）
├── requirements.txt / Dockerfile / docker-compose.yml
├── scripts/                  # 部署脚本 + 本地诊断脚本（*.py 被 .gitignore 忽略，不进仓库）
│   └── daemon.ps1                       # 部署用守护脚本（**保留在仓库内**）
├── admin/                    # 多账号管理后台（FastAPI + MySQL + Redis）
│   ├── server.py             # FastAPI 入口、登录、静态页挂载、converter 挂 /gw
│   ├── config.py             # 配置（环境变量覆盖）
│   ├── db.py                 # SQLAlchemy 引擎 / 会话 / 建库建表 / 列迁移
│   ├── models.py             # Account / AccountModelCooldown / ApiKey / UsageLog / Schedule ORM
│   ├── security.py           # JWT、Key 哈希、配额拦截
│   ├── backend.py            # 复用 converter.CredentialManager 操作单账号（含签到 / 成长任务 / Ardot 画布）
│   ├── pool.py               # 流量治理原语：在途租约 / 会话粘性 / 防撞号 / WAF IP 闸 / 三因子加权选号
│   ├── growth_plans.py       # 成长任务分级与完成策略表（实测结论沉淀处）
│   ├── scheduler.py          # 轻量定时任务：refresh_balances / sync_models / daily_checkin / refresh_growth_tasks / run_growth_tasks / keepalive_tokens
│   ├── turing_token.py       # Python 侧 X-Device-Token 提供器（subprocess 调 helper）
│   ├── client_profile.py     # 客户端参数档案：UA/版本号/风控头/指纹的探测→保存→生效→同步
│   ├── wb_paths.py           # 客户端路径覆盖（安装目录/产物目录）落库并注入 wb_install
│   ├── jobrunner.py          # 后台任务执行器（拆包等耗时操作异步化 + 进度轮询）
│   ├── routers/              # accounts / app_source / client_profile / groups / growth / keys / proxy / schedules / logs / stats / sync / models
│   └── static/index.html     # 纯 HTML + TailwindCSS + FontAwesome 管理大屏（面板状态写入 ?tab=）
├── tests/                    # 本地回归测试（.gitignore 忽略，不进仓库）
│   ├── test_pool.py          # 号池治理 / 错误分类 / 200 体内错误 / 端口优先级 / 会话键 / 画布 id / SSE 聚合 / 安装发现 / 客户端参数 / 拆包 / 端口接管 / 限流码族 / 链路头族（548 项）
│   ├── test_e2e_db.py        # 真实库端到端：迁移 / 保活任务 / 选号链路（48 项）
│   └── test_gateway_smoke.py # 真实上游端到端冒烟（9 项，会消耗少量积分）
├── wb_install.py             # WorkBuddy 安装位置 / 版本号 / 风控配置自动发现（不写死盘符）
├── wb_asar.py                # 纯标准库 asar 读取/解包/搜索（无需 Node）；逆向产物自动产出
├── wb_envcheck.py            # 环境自检：Python/依赖/Node/桌面端/逆向产物/MySQL/Redis
├── turing_helper.js          # Node：调桌面端 Turing Shield SDK 取设备 token（同样自动发现）
└── README.md

# 逆向产物（不在本仓库；路径因人而异，运行时由 wb_install 自动发现）
<安装目录>\app_source\      # cli / main / preload / renderer 解包源码
<安装目录>\resources\app.asar.unpacked\native\turing-sdk\   # 设备风控原生模块
```

> `scripts/*.py` 与 `tests/` 都被 `.gitignore` 忽略（本地回归工具，不进仓库）。
> 它们在本机检出里仍然可用。

#### 管理大屏的面板状态：地址栏 `?tab=`

面板切换会同步写进地址栏（`?tab=client` / `?tab=logs` …），刷新后停在**当前**面板，
不再跳回「账号」首页；直接把链接发给别人也能打开同一面板。两个实现要点：

- 用 `history.replaceState` 而不是 `pushState` —— 切面板不该往历史栈里堆一长串，
  浏览器「后退」应当离开本页，而不是在面板之间来回跳。同时监听 `popstate`，
  真跨页面前进/后退时同步面板（地址栏被手改后回车也走这条）；
- 面板名走**白名单**（那 10 个 `data-tab`），非法/缺失一律回落 `accounts`；
  默认面板不写进 URL，保持地址栏干净。

面板名 → 懒加载的对应关系保持不变（`groups`/`usage`/`client`/`settings` 各自按需拉数据），
所以从 URL 恢复面板时数据照样会加载，不是只切了个样式。

---

## 十、稳定性与流量治理（限速 / 并发 / 保活）

本章是「把号池跑稳」的部分。原则是**只借该借的**：参考项目的成熟设计拿来加固既有实现，
而不是照抄重写；每条改动都对应一个具体失效模式，并尽量落到官方源码或真机实测上。

### 10.1 选号：三因子加权 + Top-N 抽签

`ADMIN_ACCOUNT_SELECT=weighted` 时启用（默认仍是 `remain`）：

```text
权重 = 1 + 余额/池内最高余额 × 10
        + 快过期积分/余额 × 8
        + min(闲置小时数 × 0.5, 5)
```

取 **Top-5 短名单**再在名单内加权抽签，而不是直接排序取第一 —— 抽签是**概率倾斜**
而非硬排序，能把流量摊开；纯排序会让余额最高的号承担几乎全部流量（热点），
且余额一旦回落就骤停。

> 一个踩过的坑：权重全等时按 uid 字典序截断，会让排序靠后的账号**永远**进不了
> 短名单。实测出现过某号占 79/100 的惊群。因此检出并列时先洗牌再截断。

### 10.2 并发：在途租约（把并发摊平到全池）

粘性路由与加权选号都会**倾向**少数账号；叠加长连接（SSE）后单号容易过载。
上游按账号限速时，单号过载会直接表现为成片 429/5xx，然后这些号被冷却、
流量整体挤到下几个号，形成雪崩。

`admin/pool.py` 的 `InFlight` 给每个账号一个在途计数，占满上限（`ADMIN_POOL_MAX_IN_FLIGHT`，
默认 3）的账号**不参与选号**。要点：

- 上限 `<= 0` 表示不限（计数仍累加，供观测）；
- `release` 幂等（重复释放不会把计数扣成负数）；
- 代理层在 `finally` 里兜底释放 —— 客户端中断流时也必须归还名额，
  否则该账号的在途计数永远减不回去，最终被**永久**排除在选号之外。

### 10.3 会话粘性：同一会话固定同一账号

`ADMIN_SESSION_STICKY=1`（默认开）。两个收益：

1. **上游前缀缓存不碎** —— 换号等于换一份服务端上下文缓存，多轮对话每次都重新计费、也更慢；
2. **拟人** —— 真实用户的一次会话属于同一台设备/同一个账号；一次会话在号池里
   逐轮跳号是很容易被识别的批量特征。

会话键优先级：`metadata.conversation_id` → `metadata.conversationId` → `conversation_id`
→ `conversationId` → `prompt_cache_key`；OpenAI 兼容协议没有这些字段时，用
**首条 user 消息的 sha256** 兜底（会话内历史不断追加而首条恒定 → 同会话恒同键）。

> 刻意**不**把 `metadata.user_id` 当粘性键：它的粒度太粗，会把一个用户的所有并行
> 对话钉到同一个账号上，远粗于上游「对话级」的缓存边界。
> 反过来，请求体带 `user_id` 时**关闭**首条 prompt 兜底，避免同一个问题从另一头发生。

绑定是滚动的（每次命中续期），账号不可用时自动解绑重分配；请求成功后把会话
**重绑到实际成功的账号**，让多轮收敛到「对该会话持续成功」的那个号。

### 10.4 错误分类与账号处置（一张表看懂）

`_classify_error()` 的分层顺序是**语义具体优先**，每层都为防止一种误判：

| 分类 | 触发 | 处置 | 为什么这样处置 |
|------|------|------|----------------|
| `model_block` | 400/404 + `11102` | 该 (账号,模型) 负缓存，指数退避封顶 24h；**换模型** | 官方确定「该后端无此模型」，换号重试无意义，只有换模型有效 |
| `session_dead` | `12153` / **401** / "Offline user session not found" | **连续 3 次**才禁用 | 该错误会被临时触发（上游抖动、并发刷新 token），一次就禁用等于误杀健康号；401 token 过期换号即可恢复 |
| `account_fault` | `11140` / `14015` / `14016` / `14017` | 冷却 30 分钟后换号 | 账号级授权/授权态故障，常带 429 状态码，**必须先于限流判定** |
| `model_rate` | `6004` / `6008` | **只冷却该模型**，然后**换号继续** | 日级额度（TPD/RPD）是**账号级**额度，换号就能继续；「切个模型就能用」的号也不该被整体摘出池子 |
| `soft_rate` | 429 或 `6000`–`6003` / `6005`–`6007` | 有重置时间就精确对齐，否则有界指数退避（封顶 2h） | 秒/分/时级限流是账号级的，换个号立刻可用，所以退避要**有界** |
| `hard_credit` | 402/412 或 `14001`/`14012`/`14013`/`14014`/`14018` 或余额文案 | 冷却到**次日 04:00** | 日额度在凌晨重置，04:00 是重置完成后的安全时点 |
| `upstream_internal` | **HTTP 200 但体内是错误信封**（JSON-RPC `-32603` / `ENOSPC` / `error` 字段） | 15s 短冷却 + 换号重试 + 连败计数 | 见 10.4.1。上游会把内部故障塞进 200 响应，只看状态码会误判成成功 |
| `waf` | 403 且**无业务信封** | 短冷却 + IP 级 fail-fast | 见 11.5 |
| `server` | 5xx / **408 / 425** | 熔断，指数退避封顶 6h | 比原「累计 5 次固定 10 分钟」更贴合：固定时长对持续坏的号太短、对偶发又太长；408/425 是瞬时错误，绝不能算客户端错误 |
| `not_found` | 404 | 60s 短冷却，**不累计** errCount | 防雪崩 |
| `transport`/`client` | 网络抖动 / 其它 4xx | 只记时间 + 连败计数 | 不是账号的错，不叠加权威惩罚 |

分类元组集中定义在 `_RETRYABLE_KINDS` / `_MODEL_SWITCH_KINDS`，五处轮转循环
**共用同一份**（`test_rotation_call_sites_share_one_policy` 静态校验）。
历史教训：每处各复制一份列表，改了一处漏了另一处，就会出现「某条路径报错但不换号」这种极难复现的漂移 bug。

**限流重置时间优先读响应头**（`_reset_at_from_headers`，与官方客户端同款口径）：

| 头 | 形态 | 说明 |
|----|------|------|
| `Retry-After` | 整数**秒** | 最权威，优先；日期形态按官方行为忽略 |
| `anthropic-ratelimit-unified-reset` | epoch 秒 或 HTTP 日期 | 次优先 |
| `x-ratelimit-reset` | epoch 秒 或 HTTP 日期 | 再次 |

头里拿不到才回落到解析响应体文案（`_parse_reset_at`），两者都拿不到才用有界指数退避。
**为什么优先读头**：429 的文案是给人看的、会随上游版本变化；头是机器契约。
只读文案的实现，上游换个措辞就解析不出重置时间，冷却只能靠猜。
另外「解析出的时间已过去」必须当无效 —— 否则冷却立即失效，等于没冷却。

#### 10.4.0 上游错误码全表（逆向官方客户端得到）

下面这张表**不是猜的**，是从官方 WorkBuddy 桌面端 `app.asar` 里的 CLI bundle
（`cli/dist/codebuddy.js`）中提取的权威枚举 `ServerErrorCode`，以及客户端自己的
判定函数 `isTransientRateLimitBusinessCode` / `isCraftDailyQuotaBusinessCode` /
`isQuotaExhaustedError`。

**限流码族 `6000`–`6008`**（这是我们原先漏得最狠的一块 —— 只处理了 `6004`）：

| 码 | 名称 | 维度 | 客户端是否重试 | 本网关处置 |
|----|------|------|----------------|-----------|
| `6000` | CraftRateLimit | 未细化 | 是 | `soft_rate` |
| `6001` | CraftRateTPSLimit | 每秒 token | 是 | `soft_rate` |
| `6002` | CraftRateTPMLimit | 每分钟 token | 是 | `soft_rate` |
| `6003` | CraftRateTPHLimit | 每小时 token | 是 | `soft_rate` |
| `6004` | CraftRateTPDLimit | **每天** token | **否**（日额度） | `model_rate` |
| `6005` | CraftRateRPSLimit | 每秒请求 | 是 | `soft_rate` |
| `6006` | CraftRateRPMLimit | 每分钟请求 | 是 | `soft_rate` |
| `6007` | CraftRateRPHLimit | 每小时请求 | 是 | `soft_rate` |
| `6008` | CraftRateRPDLimit | **每天**请求 | **否**（日额度） | `model_rate` |

客户端把 `{6004, 6008}` 单独拿出来当「日额度、不重试」，其余 `6000`–`6008`
都当瞬时限流重试。本网关比客户端更细一层，因为**我们是号池**：
秒/分/时级是**账号级**额度 → 换号立刻可用 → `soft_rate`；
日级是该**账号×该模型**的日额度 → 换号或换模型都可解 → `model_rate`。

**额度/授权码**：

| 码 | 名称 | 本网关处置 | 为什么 |
|----|------|-----------|--------|
| `14003` | **RateLimitError** | `model_rate`（**只冷该模型**） | 模型繁忙而非账号问题：同一个号换别的模型立刻可用；官方 UI 即「请切换模型或稍后重试」 |
| `14001` | UsageLimitExceeded | `hard_credit` | 个人用量超限，等分钟级不会恢复 |
| `14012` | UsageLimitExceededEnterprise | `hard_credit` | 企业用量超限 |
| `14013` | UsageLimitExceededTencent | `hard_credit` | 腾讯侧用量超限 |
| `14014` | UsageLimitEnterpriseExhausted | `hard_credit` | 企业额度用尽 |
| `14018` | UsageLimitUserExhausted | `hard_credit` | 个人额度用尽 |
| `14015` | UsageLimitLicenseExpired | `account_fault` | 授权到期，非额度问题 |
| `14016` | UsageLimitEnterpriseNotActivated | `account_fault` | 企业未开通，非额度问题 |
| `14017` | UsageLimitUserNotActivated | `account_fault` | 试用未激活，非额度问题 |
| `11140` | — | `account_fault` | 账号级授权风控 |
| `11102` | — | `model_block` | 该后端无此模型（确定性） |
| `11115` | ContextTooLong | 透传客户端 | 上下文超长，换号无意义 |
| `10105` | ConversationLimitExceeded | 透传客户端 | 并发会话数超限 |
| `15001` | WebSearchRateLimit | 透传客户端 | 仅联网搜索受限 |

**关键教训**：这些码**不总是配 `429`** —— 上游会把限流包在 **HTTP 200 或 400** 里返回。
只看状态码的实现会把它落到 `status >= 400 → "client"`（**不可重试**）或 `"transport"`，
于是「限流了却不换号」。所以码判定必须**先于**通用 429 与「其余 4xx」两层
（`test_upstream_error_code_taxonomy` 对全族 9 个码 × 多状态码做了穷举断言）。

#### 10.4.1 事故复盘：一次 14003 抖动如何变成 10 分钟全站 503

**现象**：所有请求返回
`503 {"message":"无可用账号（全部禁用或额度耗尽）","type":"no_account"}`，
但后台看 16 个账号**全是 `active`、余额 106–1809**，没有一个被禁用、没有一个额度耗尽。

**真实因果链**（2026-09-23，从线上库里读出来的）：

| 步 | 事实 |
|----|------|
| 1 | 上游对 `deepseek-v4.1-flash` 返回 `14003 RateLimitError`：`{"code":14003,"msg":"too many requests","displayMsg":{"zh":"请求过于频繁，请稍后重试。"}}` |
| 2 | 旧代码里 `14003` **没有任何专门处理**，落到通用 `"too many requests"` 文案分支 → **账号级** `soft_rate` |
| 3 | `soft_rate` 用 `SOFT_RATE_SECONDS=600`（**10 分钟**）做**账号级**冷却 |
| 4 | `MAX_ROTATE=3`：每个失败的客户端请求会轮转并冷掉最多 **3** 个账号 |
| 5 | 于是**只需 6 个并发请求**就能冷掉 15 个号的整池。实测 16 个账号在 **76 秒**内全部 `err_count=1 / cool_kind=soft_rate`，`cool_until` = 报错时刻 + 600 秒 |
| 6 | `_select_account` 要求「不在任一冷却期内」，整池被冷 → 返回 `None` → **在真正请求上游之前**就 503 |

两个独立的错叠在一起：

* **作用域错了**（主因）：`14003` 是**模型**繁忙，不是一个账号坏了。
  实测同一个号对 `deepseek-v4.1-flash` 报 14003，**换别的模型立刻可用**；
  官方 UI 的文案就是「当前模型请求繁忙，请**切换模型**或稍后重试」。
  把它当账号级处理，等于让一个模型的抖动把整个号池摘空。
* **时长错了**（放大器）：`soft_rate` 的文档写的是「**秒级**有界退避」，
  配置值却是 600 秒 —— 代码与自己的注释差了一个数量级，
  于是几十秒的抖动被放大成 10 分钟停摆。

所以那句「全部禁用或额度耗尽」是**误判**：没有一个号被禁用，也没有一个号额度耗尽。

**三处修复**：

1. **`14003` 改判为 `model_rate`（模型级），账号完全不动**。
   只写「该账号×该模型」的冷却，且走秒级档（`ADMIN_POOL_REQUEST_RATE` / `_MAX`，
   默认 20/120 秒；上游若带 `Retry-After` 也按 `_MAX` 封顶，免得把抖动记成小时级）。
   结果：繁忙模型被短暂绕开，**其它模型立刻可用** —— 这正是 `model_rate` 与
   `soft_rate` 分开存在的意义（6004/6008 的日额度走同一条模型级路径，但保持 600 秒长档）。
2. **整池临时冷却时启用兜底选号**（`_transient_cool_fallback`）：严格过滤为空时，
   不再直接返回 `None`，而是取**冷却最早到期**的号再试一次。只放宽 `cool_until`，
   且只接受 `soft_rate` / `not_found` / `upstream_internal` 这类**临时**状态；
   `hard_credit` / `account_fault` / `session_dead` / `waf` 以及熔断、降权**一律不放行** ——
   对着一个死号反复重试同样是错。并发请求仍会被 100ms 防撞号窗口摊到不同号上。
   这一层是给真正的账号级限流（6000 族）兜底的，避免同类「整池被冷 → 硬 503」。
3. **503 文案如实**（`_no_account_reason`）：区分「全部被临时限流（附恢复秒数）」
   「全部已禁用」「余额都为 0」「熔断/降权/需人工处理」，不再一律嫁祸给「禁用或额度耗尽」。

> **仍然存在的固有风险（未改，作为后续项）**：轮转本身会把一次客户端请求放大成
> `MAX_ROTATE` 次上游调用。若上游的限流最终被证实是**按出口 IP**（而非按账号或按
> 模型）计的，那么换号根本无解，继续轮转只会加重风控。`admin/pool.py` 里已有
> `WafIpGate` 这个「60s 内 N 个**不同账号**命中即判定 IP 级拦截、立刻停止轮转」的
> 先例，把限流也接进同一个闸门是下一步该做的事。
>
> 另外 `SOFT_RATE_SECONDS` 默认仍是 600 秒。对 6000 族里的「每秒钟 / 每分钟请求数」
> （`6001`/`6002`/`6005`/`6006`）来说这个值偏大（文档自称「秒级」），
> 但它同时服务于「每小时」档（`6003`/`6007`），目前不做区分。
> 真要调，应按 `6001/6002/6005/6006/6007` 各自的周期分别设档，而不是整体调小。

#### 10.4.2 HTTP 200 不等于成功

上游会把**内部故障**塞进 HTTP 200 的响应体里返回，例如客户端看到的
`Error Code: 10000`，其底层是：

```json
{"code":-32603,"message":"Internal error",
 "data":{"details":"ENOSPC: no space left on device, write","category":"internal"}}
```

**只看 HTTP 状态码的实现会把这种响应当成成功** —— 于是既不换号、也不重试、
不记任何失败日志，客户端却拿到一个错误体。这正是「接口直接报错了但没有重试切换其它账号」的根因。

三层修复：

1. **分类**：`_classify_error(200, body)` 现在会解析响应体，识别 `error` 字段、
   负 `code`（JSON-RPC）、`category: internal` 与 `ENOSPC` 等标记，归为 `upstream_internal`。
   该判定**放在最后一层**，因此具体业务码（6004 / 11102 / 12153…）优先级更高。
2. **不误判**：判定基于**解析后的结构**而非原始字节 substring，
   所以模型正常回答里出现 "internal error" / "ENOSPC" 这几个字**不会**被当成失败
   （`test_inband_error_detection` 有反向用例）。
3. **提交前探测**：流式响应一旦把字节发给客户端就无法换号了。因此现在先缓冲，
   直到 `_sse_has_content()` 确认真实正文增量（`delta.content` / `reasoning_content` /
   `text_delta`）才提交；只含 `role` 等元信息的帧不算正文。若整条流结束时发现
   体内是错误且**尚未提交**，就换号重试；缓冲上限 `_INBAND_PROBE_MAX`（64 KB）
   保证正常流不会被无限期憋住。
   若错误到达时已经提交过字节（收不回来），则**如实记为失败**而不是记成功，
   并给账号打上冷却 —— 绝不再把失败伪装成 `error_kind="success"`。


两个防累积设计：

- **成功即清零**（`_note_success`）：没有这个，任何长期运行的池子最终都会因零星失败
  把健康号一个个摘出去；
- **冷却取「或门」**：`cool_until` / `breaker_until` / `degrade_until` 任一未到期即不可选。
  原实现共用一个 `cool_until`，后写的短冷却会**覆盖**先写的长冷却，等于提前放行一个坏号。

### 10.5 WAF：IP 级 fail-fast

WAF 403 拦的是**网关出口 IP**，不是账号（实测 3 个账号 1 秒内全 403）。
账号级冷却在这种场景下不够：轮转会把一次客户端请求放大 `MaxRotate` 倍，
同一出口 IP 继续打上游只会加重风控。

判据：60s 滑窗内**不同账号**命中 WAF 403 达到 2 个即判定 IP 级拦截，激活一个窗口。
激活期内新命中不续期（保守，不做主动探测）。**单号反复 403 永不触发** —— 只数不同账号。

### 10.6 轮转退避：指数 + 抖动

`base 500ms × 2^n`，封顶 8s，再施加 **±25%** 抖动。抖动不是装饰：WAF 频控按密度判罚，
齐步走的退避会以固定周期**再次聚团**。

### 10.7 超时：最要紧的一处修复

先纠正一个常见误解：`httpx.AsyncClient(timeout=300)` **不是**总时长 5 分钟，
它把 connect/read/write/pool **各**设为 300s。所以原代码并不会在流式响应中途
掐断活跃的流（read 是「两次读到数据之间」的间隔上限，有数据就重置）。

但它有两个真实缺陷，都在**该快的时候不快**：

1. **connect=300**：一个 TCP 连不上的账号要让我们干等最多 5 分钟才轮到换号逻辑。
   号池里恰恰总有若干连不上的号（被墙/限速/节点故障），**这才是「卡住不动」的主因**；
2. **pool=300**：连接池打满时新请求排队最多 5 分钟。池满本身就是过载信号，
   应当快速失败并把压力交回上层（退避/换号），而不是无限排队把延迟一层层叠起来。

现在拆成四项（`admin/config.py`）：

| 项 | 默认 | 作用 |
|----|------|------|
| connect | 15s | 连不上就快速换号 |
| read | 180s | **静默**上限：有数据就续期，长时间推理只要还在吐字就永不被自己掐断 |
| write | 60s | 上传大 prompt |
| pool | 20s | 池满快速失败，交给上层退避 |

> **必须避免的坑**：绝不给流式响应设**总时长**上限。依据是官方客户端自己的源码 ——
> Node 18+ 默认的 `http.Server.requestTimeout = 300000` 是总时长，会在流**仍然活跃**时
> 到点强行掐断长连接 SSE，客户端只看到 undici `TypeError: terminated` /
> "SSE stream disconnected"。官方把它当必须修的 bug，修法是三重防御把总时长锁到 0。
>
> 这里的 `read` 实际就等价于参考实现的 SSE 空闲监控（有数据续期、静默即取消），
> 且不需要额外起监控任务。因此**非流式端点也用这一套**：上游一律以 `stream: true`
> 返回 SSE，我们在内部聚合，用静默上限而非总时长。

顺带修掉两处 `timeout=None`（完全不设限，连接静默死掉就永久挂住，既不报错也不归还资源）。

### 10.8 token 保活

`keepalive_tokens` 定时任务（默认每晚 22:00，`ADMIN_KEEPALIVE_HOURS` 可配，
`ADMIN_KEEPALIVE_ENABLED=0` 可关）。

为什么需要：上游登录态有绝对有效期。长期没有请求的账号，其 refresh token 会在某天
静默失效 —— 等到真有人来用，才在第一次请求时发现要重登。用户感知就是
「号池里明明有余额的号，用的时候报错」。

要点：**只刷新不调用**（不消耗积分、不产生对话记录）；账号之间按
`ADMIN_KEEPALIVE_ACCOUNT_GAP`（默认 0.8s）**串行节流** —— 批量并发刷新 token 是
很明显的机器特征；`12153` 连续计数达到 3 次才禁用（与 11.4 同源口径）。

#### 10.8.1 官方客户端的刷新节奏（逆向对照）

官方**没有**任何「登录态心跳」接口 —— 这一点值得记下来，避免以后误加一个上游根本
不存在的保活请求。客户端靠的是**低频刷新 + 失败退避**：

| 项 | 官方取值 | 说明 |
|----|---------|------|
| 正常刷新间隔 | **24h** ± 抖动（`86400s + rand(0..10min) - 5min`） | 远长于我们的每夜一次 |
| 短有效期兜底 | 若 `expiresAt` 距今 < 24h → **5min + rand(0..60s)** | token 快过期时提前刷 |
| 最小间隔 | `MIN_REFRESH_DELAY_MS = 15000`；30s 内不重复刷新 | 防抖 |
| 失败重试 | `min(5000·2^(n-1), 60000)` → 5/10/20/40/60s，最多 5 次 | 与我们的退避口径一致 |
| 401/403 | **不重试** | 授权态问题，重试无意义 |

刷新接口：`POST /v2/plugin/auth/token/refresh`，头为官方专门的组合
`X-Refresh-Token` + **`X-Auth-Refresh-Source: plugin`** + `X-Domain`。
（注意上游 auth 路径带 `/v2/plugin/` 前缀，来自 `product.json` 的 `prefixPath: "/plugin"`，
漏掉这一段会 404。）

登录轮询参数（`/v2/plugin/auth/state` → 开浏览器 → 轮询 `/v2/plugin/auth/token?state=`）：
**1000ms 间隔、300s 超时**；轮询期间必须带四个抑制头
`X-No-Authorization` / `X-No-User-Id` / `X-No-Enterprise-Id` / `X-No-Department-Info`（值均为字符串 `"true"`），
否则会被判成「未授权访问受保护接口」。

我们现行的「每晚 22:00 串行刷新」比官方更保守（更晚、更省），方向是对的：
官方 24h 节奏对**真人单客户端**合适，对**号池**则太稀疏（号会静默失效）。

### 10.9 拟人化请求头

出站请求补齐官方客户端形状的头，缺哪个就少一个「这是真人客户端」的证据：

| 头 | 值 / 来源 | 作用 |
|----|-----------|------|
| `User-Agent` | `WorkBuddy/<桌面端版本> WorkBuddy/<同版本> CLI/<CLI版本>`，**版本号来自客户端参数档案** | **原先自报 `codebuddy2openai/2.0`，等于对风控举手**；官方三段式形状取自实测分发包 |
| `X-CodeBuddy-Request` | `1` | 官方客户端所有 API 请求必带的闸门头 |
| `X-IDE-Version` | 同桌面端版本（来自档案） | 用量归属，报旧版本是明显特征 |
| `X-Machine-ID` / `X-Session-ID` | `md5("machine:{uid}")` 稳定派生 | 「每个账号一台固定虚拟设备」：每次随机 = 频繁换设备（异常）；全池共用常量 = 多号同设备（最易被识别）。两者都错 |
| `X-Conversation-*` / `X-Request-ID` / `X-B3-*` | 会话键派生，**轮转循环外只算一次** | 一次 user send 内所有尝试复用同一份 —— 换号重试若换了会话 ID，上游看到的是 N 个并发会话而非一次对话的一次重试 |
| `traceparent` / `b3` / `X-Trace-ID` | 与 `X-B3-TraceId`/`X-B3-SpanId` **同一份**值 | 官方每次模型请求都注入这一整套（`injectOtelSpanHeaders`）。**发得不一致比不发更糟**：同一请求里两个不同 traceId 是自相矛盾的特征 |
| `X-Agent-Intent` | `craft` | 官方模型请求恒带，无 `meta` 时的兜底值 |
| `X-Private-Data` | `false`（默认） | 官方每次模型请求都带的「数据用途」声明：模型优化开启 → `false`，关闭 → `true`。缺失即可识别 |
| `Accept-Language` | `zh-CN` | 缺失会被上游按语言异常误判 |

> **逆向同时确认「本版本不存在的头」**：`X-Moderation-Type` / `X-Expert-Id` /
> `X-Expert-Team-Task` 在 5.5.6 的两个 CLI bundle 里命中数均为 **0**，
> `X-Machine-ID` 也是 0（官方只用 `X-Machine-Id`，且仅用于 `/v2/feedback` 与诊断，
> **不在模型请求上**）。所以这些**不能发** —— 发一个上游从未见过的头，
> 比不发更容易被识别（`test_client_header_shape_matches_official` 有反向断言锁住）。

> **`X-B3-ParentSpanId` 故意不发**：官方只在存在父 span 时携带（`ec && setHeader(...)`），
> 网关发出的都是根请求，带上反而异常。

以上头与桌面指纹**统一由「客户端参数档案」提供**，可在后台查看/修改/同步（见 §10.10.1）。

#### 10.9.1 风控 SDK（Turing）到底发了什么 —— 一个反直觉的结论

逆向官方客户端的结论是：**没有 `X-Turing-*` 头，也没有可在纯 JS 里复刻的签名算法。**

官方接入的是腾讯 T-Sec TuringShield **原生 SDK**（`native/turing-sdk/` →
`turing_sdk.node` → `TuringShieldSDK.dll`）。它对 HTTP 只暴露两个**互斥**的头：

```
X-Device-Token        成功
X-Device-Token-Error  失败
```

device token 由 DLL 采集本机硬件指纹后向 `https://tdid.m.qq.com/tmf` 换取，
是个**不透明字符串**。JS 层只负责透传，所以：

* **纯 Python 网关无法自行生成**这个 token —— 没有可复刻的算法；
* 但官方客户端**自带「未配置就降级」路径**：拿不到 token 时照常发请求，
  只是不带这个头（或带 `X-Device-Token-Error`）。所以我们**省略它不算异常**；
* 参数：`channelId=109144`（来自 `product.json`）、软过期 5 分钟、
  后台异步刷新**绝不阻塞当前请求**、刷新退避 `min(30s·2^(n-1), 60s)`。

顺带纠正两个容易搞混的点：`machineId` 只用于 `X-Machine-Id`（反馈接口 `/v2/feedback`）
和诊断上报，**不在模型请求上**；`qimei36` **只进遥测事件体，不进 HTTP 头**。

### 10.10 客户端版本与逆向产物：自动发现 / 自动产出

版本号曾经是写死的常量（`DESKTOP_VERSION = "5.5.6"`、`CLI_VERSION = "2.137.1"`）。
这有两个**用户侧必然踩到**的问题：

1. **安装盘符不固定** —— 写死 `D:\WorkBuddy`，用户装在 C 盘/绿色版就直接失效；
2. **版本会变** —— 官方一发版，我们的 UA 立刻变成「自报旧版本」，
   既是明显特征，也容易被上游版本闸门拦下。

因此 `wb_install.py`（项目根）统一负责自动发现，Python 与 Node 两侧共用：

**安装目录**（命中即用，判据是存在 `resources/app.asar`，比「目录名像」可靠）：

```text
1. WORKBUDDY_INSTALL_DIR                 显式指定（可指向安装目录 / resources / app.asar）
2. 常见基目录下的 WorkBuddy / workbuddy
   %LOCALAPPDATA% %APPDATA% %ProgramFiles% %ProgramFiles(x86)%
   %ProgramW6432% %USERPROFILE% %HOME%
3. 各盘根目录下的 workbuddy / WorkBuddy   默认盘符 C,D,E,F,G（WORKBUDDY_DRIVES 可改）
```

**版本与配置**（逐项独立回退，缺一项不影响其它项）：

| 取值 | 优先级 |
|------|--------|
| 桌面端版本 | `WORKBUDDY_DESKTOP_VERSION` > `resources/install-manifest.json` 的 `appVersion` > `cli/package.json` 的 `version` > `cli/product.json` 的 `genieVersion` > **`app.asar` 内 `/package.json` 的 `version`** > `WorkBuddy.exe` 版本资源 > 兜底 `5.5.6` |
| CLI 版本 | `WORKBUDDY_CLI_VERSION` > `cli/package.json` 的 `publishConfig.customPackage.version` > `cli/dist/codebuddy.js` 内嵌版本 > 兜底 `2.137.1` |
| 风控 channelId | `WORKBUDDY_TURING_CHANNEL_ID` > `cli/product.json` 的 `config.turingSdk.channelId` > 兜底 `109144` |
| commit / 发布日期 | `WORKBUDDY_COMMIT` / `WORKBUDDY_RELEASE_DATE_MS` > `cli/product.json` 的 `commit` / `date` > 兜底 |

几个实现要点：

- **CLI 版本走官方同款判据**：先看 `cli/package.json` 的 `version`，
  非 `0.0.0`（monorepo 占位）才用；否则看 `publishConfig.customPackage.version`。
  这比读 22MB 的 `dist/codebuddy.js` 快几百倍，两者同源。
- **有 TTL 缓存**（5 分钟）＋进程内单例（首次约 18ms，缓存命中约 0.01ms）。
  **运行期装了新客户端不用重启进程**也会跟上。
- **绝不抛异常**：任何一步失败都退化成兜底值。服务器上通常没装桌面端，
  这时服务必须照常启动，只是版本号不如实测准确。
- **Node 侧同源**：`turing_helper.js` 也读同一份安装包元数据，不再用自己写死的
  `2.0.0`；Python 侧发现后会通过环境变量把安装目录/channelId/版本下发过去。

#### 逆向产物不存在怎么办：自动从原安装位置产出

用户的解包产物**不一定还在**（可能被删、可能从没做过、也可能换了机器）。
更麻烦的是官方包里这些文件是 **unpacked** 的：

| 内部路径 | 数据在哪 |
|----------|----------|
| `/package.json` | **在 asar 数据区**（可纯 Python 读） |
| `/main/index.js` | 在 asar 数据区 |
| `/cli/product.json` | **unpacked**（在旁边的 `app.asar.unpacked/`） |
| `/cli/dist/codebuddy.js` | **unpacked** |
| `/native/turing-sdk/*` | **unpacked** |

所以 `unpacked` 目录一缺，`cli/product.json` 这些就全断了。为此做了两层保障：

**1）版本号不依赖 unpacked。** `wb_install` 会回退到 **`app.asar` 内部的
`/package.json`**（数据区，offset=0），用纯标准库定位读出。实测在「只有
`app.asar`、没有 `unpacked`、也没有 `install-manifest.json`」的目录里，
仍能拿到真实版本号：

```text
安装目录=... | 桌面端=5.5.6(app.asar!/package.json) | CLI=2.137.1(兜底)
```

**2）需要源码时现场产出。** `wb_asar.py` 是**纯标准库**的 Electron asar
读取器/抽取器（asar 是未压缩的拼接文件：JSON 头 + 原始字节，标准库 40 行就能读）：

```bash
python wb_asar.py extract            # 抽到用户缓存目录（默认不在项目里）
python wb_asar.py extract --dest D:/wb_source
python wb_asar.py read /package.json # 读单个文件（unpacked 自动分流到磁盘）
python wb_asar.py list --grep canvas # 列出内部文件
python wb_asar.py search "wbx_design" # 字节检索
```

**这条彻底去掉了对 Node/npm 的依赖**。以前要解包得先装 Node + npm +
`npm i asar`，服务器上经常装不动；现在一个 Python 命令就够。
`Asar.read_file()` 会自动分流：数据在 asar 里就定位读，标记 `unpacked`
就去 `<asar 同级>/app.asar.unpacked/<路径>` 读，调用方不用关心区别。

实测抽取全量源码：**2242 个文件 / 225MB / 14 秒**（含 unpacked，0 缺失）。

产物默认写到用户缓存目录（`%LOCALAPPDATA%\workbuddy2api\app_source` 或
`~/.cache/...`）而**不是项目里** —— 200MB+ 放进仓库会被 git 追着跑。
需要固定位置时用 `WORKBUDDY_SOURCE_DIR` 指定。

`wb_asar.py` 已把 asar 读取/抽取/检索全部收进来（纯标准库），
命令行用法见上一节，后台也有「一键拆包」按钮，都不需要 Node。
#### 后台一键拆包：不用记命令，也不用装 Node

命令行能做，但对「只想看看源码」的人来说门槛仍在（要记住 `wb_asar.py` 的
子命令、要找对输出目录）。而这件事本质上就是点一下，于是收进了后台：

**位置：`/admin` → 设置 → 逆向产物**

| 能力 | 说明 |
|------|------|
| **一键拆包** | 点按钮即从 `app.asar` 解出源码。**纯标准库，不需要 Node/npm** |
| **手动指定安装目录** | 客户端装在非默认盘符/位置时，直接填路径即可 |
| **自动补齐层级** | 填 `D:\WorkBuddy`、`…\resources`、`…\resources\app.asar` **都行** |
| **预检** | 拆包前先告诉你「将写入 2242 个文件 / 225MB」，而不是让你盲点 |
| **实时进度** | 显示 `1234/2242 个文件` + 当前路径（实测约 15 秒） |
| **源码检索** | 在产物里搜关键字（等价于对 `app_source` 做 grep） |
| **删除产物** | 只删本工具产出的目录，防误删 |

**为什么走异步任务**：全量拆包 2242 个文件 / 225MB / 约 15 秒，同步跑会顶到
nginx 的 `proxy_read_timeout`（默认 60s）边缘，界面也是「点了没反应」。
所以 POST 立即返回 `job_id`，前端轮询进度。同一个 job key 只允许一个在跑，
**重复点击不会叠起多个并发拆包**。

**安装目录的手工指定**：填进去的路径不要求「正好是安装根目录」，
`normalize_install_dir()` 会依次试「原样 / 父 / 祖父」找 `resources/app.asar`：

```text
D:\WorkBuddy                          -> D:\WorkBuddy
D:\WorkBuddy\resources                -> D:\WorkBuddy
D:\WorkBuddy\resources\app.asar       -> D:\WorkBuddy
D:\WorkBuddy\resources\随便不存在的名字 -> 报错「路径不存在」（不会瞎猜）
```

最后一条是刻意加的：**输入本身必须存在**才继续判断。否则
`…/resources/随便一个不存在的名字` 会因为父目录恰好含 `app.asar` 而被
「补齐」成一个有效安装目录，让用户以为自己填对了（实测踩过这个坑）。

**路径优先级**（环境变量仍然最高，后台设置次之）：

```text
WORKBUDDY_INSTALL_DIR > 后台「安装目录」设置 > 自动扫描
```

这个顺序是刻意的：环境变量是运维逃生门，容器 / systemd 注入的值不该被后台
操作悄悄盖掉；反过来，后台改完能立即压住自动扫描的结果。后台设置存在
`system_settings` 表里，**保存即生效，无需重启进程**。

**生产环境不需要这个功能**，用 `ADMIN_DEV_TOOLS=0` 可整体关闭：

```bash
ADMIN_DEV_TOOLS=0     # /api/app-source/* 全部 404，后台也不显示该区域
```

服务器上既没有客户端安装包，也不该放 225MB 的源码，关掉最干净。

#### 环境自检：一次性看清缺什么

用户环境差异很大，而失败方式往往很难看懂：没装 Node → 取不到设备 token →
日志只有一句「token 为空」，看不出是缺 Node。所以提供一条命令集中检查：

```bash
python wb_envcheck.py          # 人类可读（按必需/可选/开发分组）
python wb_envcheck.py --json   # 机器可读，便于安装脚本/CI 消费
python wb_envcheck.py --fast   # 跳过取 token 这类慢检查
```

覆盖：Python 版本、8 个必需依赖、5 个可选依赖、Node/npm、桌面端安装、
`app.asar`、`unpacked`、Turing SDK、**实际能否取到设备 token**、
MySQL/Redis 连通性。

设计取向是**只读**（不装包、不改配置、不改代码里的任何路径）＋
**分级**（`required` 缺了起不来 / `optional` 缺了功能降级 / `dev` 只影响逆向取证）。
每项都给出「缺了怎么办」。实测输出：

```text
===== 必需 =====
  [ok]   Python 版本：3.13.14
  [ok]   依赖 fastapi：已安装
  ...
  [ok]   MySQL (127.0.0.1:3306)：可连接

===== 可选（缺失则功能降级） =====
  [ok]   Node.js：v24.21.0
  [ok]   WorkBuddy 桌面端：D:\WorkBuddy｜桌面端=5.5.6(install-manifest.json) | ...
  [ok]   设备风控 token：已获取（1154 字符）
  [ok]   Redis (127.0.0.1:6379)：可连接

===== 开发/逆向取证 =====
  [warn] 逆向产物：未产出（...\app_source）
         -> 需要时可用 WB.ensure_source() 从 app.asar 自动抽取，无需 Node/npm

结论：可以运行（25 项检查，0 失败 / 2 警告）
```

注意它**不检查 passlib/bcrypt**：本项目密码哈希用标准库
`hashlib.pbkdf2_hmac`（见 `admin/security.py`），列进来只会误导用户去装没用的包。

#### 所有路径都走 env，代码里不写死

| 环境变量 | 作用 | 默认 |
|----------|------|------|
| `WORKBUDDY_INSTALL_DIR` | 安装基目录（也接受 `resources/` 或 `app.asar`） | 自动扫描 |
| `WORKBUDDY_ASAR_PATH` | 直接指定 `app.asar`（跳过扫描） | 自动扫描 |
| `WORKBUDDY_DRIVES` | 参与扫描的盘符 | `C,D,E,F,G` |
| `WORKBUDDY_SOURCE_DIR` | 逆向产物输出/查找目录 | 用户缓存目录 |
| `ADMIN_DEV_TOOLS` | 后台「逆向产物」工具开关（生产设 0） | `1` |
| `WORKBUDDY_DESKTOP_VERSION` / `WORKBUDDY_CLI_VERSION` | 强制覆盖版本 | 读安装包 |
| `WORKBUDDY_USER_AGENT` | 强制覆盖整条 UA | 由版本号拼 |
| `WORKBUDDY_IDE_NAME` / `_EXT_NAME` / `_OS` / `_ARCH` / `_OS_VERSION` / `_CPU_CORES` / `_MEMORY_SIZE` | 桌面指纹字段 | 官方默认 |
| `WORKBUDDY_COMMIT` / `WORKBUDDY_RELEASE_DATE_MS` | 安装包 commit / 构建时间 | 读安装包 |
| `WORKBUDDY_TURING_SDK_DIR` | 风控 SDK 目录 | 自动发现 |
| `WORKBUDDY_TURING_CHANNEL_ID` / `_PRODUCT_NAME` / `_DEBUG` | 风控 SDK 配置 | 读安装包 |

自检：

```bash
python -c "from wb_install import WB; print(WB.describe())"
# 安装目录=D:\WorkBuddy | 桌面端=5.5.6(install-manifest.json) | CLI=2.137.1(cli/package.json) | turingChannel=109144(cli/product.json)
#
# 括号里是**取值来源**：显示「兜底」就说明没找到安装包，需要设 WORKBUDDY_INSTALL_DIR
# 或把安装盘符加进 WORKBUDDY_DRIVES。
```

### 10.10.1 客户端参数档案：后台可改、可同步到线上

上面这些参数（UA / 版本号 / 风控头 / 桌面指纹）原先只能「启动时探测一次」，
带来一个**线上实例必然踩到**的问题：

> 线上服务器**没装 WorkBuddy 桌面端**，探测不到任何东西，于是 UA 退化成内置
> 兜底版本号 —— 那是一个「谁也不认识的版本」，等于自报家门。而本地机器明明装得好好的。

现在把它们收敛成一份**档案**（`admin/client_profile.py`），走这条链路：

```text
探测(snapshot) ──→ 保存(saved) ──→ 生效(effective) ──→ 同步到线上(sync)
   本机安装包        system_settings    每个请求实时读      随 /api/sync/push 推送
```

**取值优先级**（`effective()`）：

```text
1. 环境变量            显式运维覆盖，永远最高（WORKBUDDY_*）
2. 现场探测 / 已保存    取决于 source 策略
3. 内置兜底            保证任何情况下都有值，绝不抛异常
```

`source` 有两种模式，因为「本地」与「线上」的最优选择正好相反：

| 模式 | 含义 | 适用 |
|------|------|------|
| `auto`（默认） | 现场探测 > 已保存 > 兜底 | **本地**：装了客户端就用真实值，官方升级后自动跟上 |
| `saved` | 已保存 > 现场探测 > 兜底 | **线上**：探测不到客户端，必须钉住同步过来的值 |

**后台「客户端参数」面板**（`/admin` → 客户端参数）：

- **探测本机客户端** —— 扫描安装包，读出真实版本号 / channelId / commit / 产品身份；
- **直接改任意字段并保存** —— 改完**立即生效，无需重启进程**（缓存 30 秒 TTL，保存时立即失效）；
- **切换取值策略** —— auto ↔ saved；
- **来源排障视图** —— 每项当前取自哪一层（环境变量 / 现场探测 / 已保存 / 内置兜底）；
- **重置** —— 清空保存值，回到纯探测 + 兜底。

**能自动探测出哪些**（`cli/product.json` 是唯一数据源，读不到就不报，绝不用兜底值冒充）：

| 键 | product.json 字段 | 生效位置 |
|----|------------------|----------|
| `desktop_version` | 安装清单 / `genieVersion` | UA、`X-IDE-Version` |
| `cli_version` | `cli/package.json` | UA 第三段 |
| `turing_channel_id` | `config.turingSdk.channelId` | 天御 SDK |
| `commit` / `release_date_ms` | `commit` / `date` | 桌面事件指纹 |
| `product` / `fp_product` | `deploymentType` | 请求头 `X-Product`、遥测 product |
| `application_name` | `applicationName` | UA 前后两段 |
| `ext_name` | `authentication.id` | 指纹 `extName` |

**`X-Product` 不是产品名，是部署形态** —— 这是很容易搞错、且错了就很显眼的一项。
官方客户端的全局 `ProductEndpointHttpInterceptor` 里写死了：

```js
config.headers["X-Product"] ||= configuration?.deploymentType ?? "SaaS";
```

即凡走该拦截器的请求（**模型请求就是走这条**），`X-Product` 报的是
`SaaS` / `CloudHosted` / `SelfHosted` 这类**部署形态**，而不是 `WorkBuddy`。
唯一硬编码 `X-Product: "WorkBuddy"` 的地方是 `stdio-mcp-inspector.js` 里
`/v2/activity/workbuddy/banner` 那个窄接口 —— 拿它当通用值会错。
本项目的 `admin/backend.py` 事件上报路径一直是 `"SaaS"`，与此结论一致。

**取值优先级**（`effective()`）：

**同步到线上**：`/api/sync/push` 会带上 `client_profile`（推送的是 `effective`，
即当下真实生效的完整参数，而不是本地那点增量配置）。

线上 `/api/sync/receive` 收到后**自动把 source 钉成 `saved`**，原因很实在：
线上探测不到客户端，如果还用 `auto`，一旦探测为空就会退化成兜底值，
把刚同步过来的真实参数又盖掉。同步是 `merge=False`（整体替换），
避免与线上旧值混在一起。

> **不传 `machineId` / `sessionId`**：这两个是**账号级**的，必须由各端按自己库里的
> uid 稳定派生。跟着传会让所有账号共用同一台「设备」—— 而那正是最容易被批量识别的特征。

> **已修的一个反向 bug（`_compose` 层优先级写反）**：`out[k] = v` 逐层覆盖，
> 所以优先级高的层必须放在列表**末尾**。历史实现把两个分支都写反了 ——
> `saved` 模式让探测胜出、`auto` 模式让保存胜出，于是「auto 下版本升级后自动跟上」
> 这条承诺根本不成立：只要在后台保存过一次，真实探测值就被那份可能已过期的档案压住。
> `sources()` 一直是按正确优先级写的，所以症状表现为「排障视图说取自现场探测、
> 实际生效的却是已保存值」的自相矛盾。现已修正，并在 `test_pool.py` 里加了
> 四个方向的回归断言（含「`sources()` 与 `effective()` 必须自洽」）。

**一个刻意留下的坑（已加测试钉住）**：`user_agent` **不可保存**。
它 100% 由 `desktop_version` + `cli_version` 派生；若允许独立保存，就会出现
「同步过一次 UA 之后，版本号再变而 UA 不变」的陈旧陷阱 —— 实测踩过：
env 覆盖了版本号，UA 却还在报旧版本。要整体自定义 UA，用环境变量
`WORKBUDDY_USER_AGENT`。

**性能**：`effective()` 在每个请求上被调用（拼上游头、拼桌面指纹）。
档案缓存三层（DB 值 / 探测值 / 合成结果），保存时立即失效：

```text
ua()           1.3 us      原先每请求重扫探测：约 39 us
risk_headers() 2.6 us
fingerprint()  4.3 us
cache miss     5.2 ms      每 30 秒最多一次
```

> **导入期不查库**：模块级常量（`converter.DESKTOP_VERSION` 等）走
> `effective_local()`（兜底 + 探测 + 环境变量），它**刻意不读数据库**。
> 因为 standalone `converter.py` 可能根本没配 MySQL —— 为了算一个 UA 去连库，
> 连不上会白等几秒（实测 4.6s → 1.4s）。运行期发请求才读后台保存值。

**接口**：

| 方法 | 路径 | 作用 |
|------|------|------|
| `GET` | `/api/client-profile` | 生效值 + 已保存值 + 探测值 + 每项来源 |
| `POST` | `/api/client-profile/detect` | 现场探测（只读，不写库） |
| `PUT` | `/api/client-profile` | 保存（merge 语义，可只改一个字段；也可只切 `source`） |
| `POST` | `/api/client-profile/reset` | 清空保存值 |

全部需要管理员鉴权。验证脚本：

```bash
.venv\Scripts\python.exe scripts\verify_client_profile_sync.py
# 覆盖：探测 → 保存即生效 → 来源 → push 载荷 → 线上无客户端仍报真实版本 → env 优先级
```

### 10.11 非流式：一个必须修的协议违约

上游 `/v2/chat/completions` **只支持流式**。为了兼顾 `stream: false` 的调用方，
网关必须自己把上游 SSE 聚合成一个 `chat.completion` 对象。

原先 `admin/routers/proxy.py` 的 `/v1/chat/completions` **没有非流式分支** ——
它无条件 `body["stream"] = True` 并返回 `StreamingResponse`。后果是
**OpenAI SDK 的默认调用（`stream` 缺省即 `false`）100% 失败**：

```text
客户端发 stream:false
  -> 网关返回 Content-Type: text/event-stream + "data: {...}" 行
  -> SDK 拿这个 body 去 json.loads()
  -> JSONDecodeError: Expecting value: line 1 column 1
```

`converter.py` 早已修过同类问题（源码里就有注释说明），但 admin 这条路径漏了。
现在改成：

- `_aggregate_chat_sse()` 把 `delta.content` / `delta.reasoning_content` /
  分片 `tool_calls` / `usage` 合并成标准非流式响应；
- 两条路径**共用同一个号池状态机**（`_stream(aggregate=...)`）——
  租约、粘性、退避、错误分类、记账一份实现，不会随时间漂移；
- 聚合模式的错误也返回 JSON 并**保留上游状态码**（把上游 400 报成 503
  会让调用方去重试一个永远不会成功的请求）；
- 中途断流在两种模式下的语义不同：流式已经吐过字节只能截断，聚合模式
  还没产出任何东西，所以**如实报错**而不是返回空响应。

> 顺带修掉的 `NameError`：`_upstream_extra_headers` / `chat_completions`
> 用到了未定义的变量与未更新的签名。这两个 bug 都是**只在真实请求路径上才触发**
> 的 —— 静态检查与单测都发现不了，是被 §10.10 提到的真机冒烟测试
> （`tests/test_gateway_smoke.py`）抓出来的。这也是为什么必须跑真实 end-to-end。

### 10.12 这批改动的验证

```bash
.venv\Scripts\python.exe tests\test_pool.py            # 548 项，不依赖库/网络
.venv\Scripts\python.exe test_upstream_compat.py       #  74 项，上游协议兼容/思考开关（本地文件，不入仓库）
.venv\Scripts\python.exe tests\test_e2e_db.py          #  48 项，连真实 MySQL（只读 + 幂等迁移）
.venv\Scripts\python.exe tests\test_gateway_smoke.py   #   9 项，真实上游端到端（会消耗少量积分）
.venv\Scripts\python.exe scripts\verify_client_profile_sync.py  # 客户端参数同步链路（DB 只改后还原）
```

> `tests/` 与 `scripts/*.py` 属本地回归工具，已在 `.gitignore` 中，不进仓库；
> 上面的命令在本地检出里照常可跑。

**三层验证缺一不可**，因为每层能抓到的问题不同：

| 层 | 抓到什么 | 抓不到什么 |
|----|----------|------------|
| `test_pool.py` | 纯逻辑：权重/退避/会话键/错误分类/id 提取/SSE 聚合/安装发现/客户端参数优先级与缓存 | 变量作用域、签名不匹配、真实响应形态 |
| `test_e2e_db.py` | 迁移、真实数据下的选号、调度注册 | 请求路径上的 `NameError`、协议违约 |
| `test_gateway_smoke.py` | **只有真跑才暴露的问题** | 长尾上游错误 |

客户端参数这批改动另外做了两组实测：

- **HTTP 层**：四个接口的鉴权 / 400 校验 / 保存即生效 / 策略切换全部走 ASGI 实测；
- **同步闭环**：本地 push 载荷 → 线上 `receive` → **用新会话**读回线上落库值，
  确认线上在「无客户端」环境下报出与本地完全一致的 UA 与版本号。

> 读数时必须**换一个新的 DB 会话**：MySQL 默认 REPEATABLE READ，
> 同一个会话看不到接收端刚提交的行 —— 这不是产品 bug，但会让验证脚本假失败
> （实测踩过一次）。


`test_e2e_db.py` 在真实数据上验证：迁移幂等、保活任务注册在配置整点、
选号链路可用、**模型级冷却不影响同账号的其它模型**、在途占满的号被排除、
以及 `remain` 策略选中余额最大者（这条是回归测试 —— 改成 Python 侧排序时
曾把 `reverse` 配错，变成选余额**最少**的号，这类错误不报错、只静默劣化）。

> 实战教训：`_upstream_extra_headers` 的 `NameError`、`chat_completions` 的
> 未定义变量、以及 §10.10 的非流式协议违约，**全部是冒烟测试抓出来的**，
> 前面两层全绿。任何「只在真实请求路径上触发」的改动都不要只靠单测收工。

---

## 十一、免责声明与协议

本项目仅用于个人学习与研究。与腾讯、WorkBuddy、CodeBuddy、OpenAI、Anthropic 无官方关联。请仅在你合法拥有订阅的前提下使用，并自行承担风险。

协议：[MIT](./LICENSE)

---

## 十二、致谢与引用声明（Credits & References）

本项目的**协议转换与多账号共享**的起步思路，来自社区已有的开源实现。
这里把「参考了什么、参考到什么程度」写清楚，既是对原作者的尊重，
也避免读者误以为这些设计是本项目原创。

### 主要参考项目

| 项目 | 作者 | 许可 | 本项目参考的内容 |
|------|------|------|------------------|
| [HanHan666666/codebuddy2openai](https://github.com/HanHan666666/codebuddy2openai) | HanHan666666 | MIT | 最早期的思路来源：把桌面端登录态转成 OpenAI 兼容接口 |
| [Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api) | Sliverkiss | MIT | 多账号池、错误分类与账号处置、会话粘性、限速/保活等**设计思路** |
| [linguo2625469/workbuddy2api-panel](https://github.com/linguo2625469/workbuddy2api-panel) | linguo2625469 | MIT | 可视化运维面板的信息架构与交互设计思路 |

### 参考的**程度**：思路，不是代码

这一点必须说明白，否则容易引起误解：

- 上述项目是 **Go** 语言实现，本项目是 **Python**；
- 语言与架构都不同，**没有复制、没有移植、没有逐行翻译**任何代码；
- 参考的是**问题清单与设计取向**——比如「要按错误类型分别处置账号」
  「同一会话要固定同一账号」「要主动保活 token」这类**该做什么**的判断；
  具体实现（三因子加权选号、在途租约、`500ms·2ⁿ` 抖动退避、WAF IP 闸门、
  错误码到冷却策略的映射等）都是本项目自己写并实测的。

### 本项目独立完成、并非来自参考项目的部分

- `app.asar` 的纯标准库读取/抽取器（`wb_asar.py`），不依赖 Node/npm；
- 客户端参数档案（UA / 版本号 / 风控头 / 桌面指纹）的**探测→保存→生效→同步**闭环；
- 后台一键拆包与路径自动补齐（`ADMIN_DEV_TOOLS`）；
- `create_canvas` 的真实 id 口径与两段式状态机（`accept` → 完成 → `claim`）；
- 全部测试（`test_pool.py` 548 项 / `test_e2e_db.py` / 网关冒烟 / 实测脚本）。

### 如果引用有误

若你是上述项目的作者，认为此处的署名、许可标注或「参考程度」描述不准确，
请提 Issue 告知，会立即更正或移除相关表述。

### 上游服务

本项目与**腾讯、WorkBuddy、CodeBuddy、OpenAI、Anthropic 均无官方关联**，
不是任何一方的官方客户端或 SDK。请仅在你**合法拥有订阅**的前提下使用。

