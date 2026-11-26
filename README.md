# MixHarness

MixHarness 是一个让 **Gemini** 和 **GPT** 在同一个本地会话里实时协作的跨平台桌面工作台（附带同名 CLI 与本地 Web 端），支持 macOS、Windows 和 Ubuntu。

简单来说：**让快的模型当主力前哨与监工，让重的模型专攻硬核工程。** 平时问答、轻活、写总结由 Gemini 一轮搞定；遇到需要进仓库改代码、跑测试、打包构建的硬骨头，再带好“施工红线”升级派给 GPT，改完后由 Gemini 对照真实 `git diff` 逐项审查把关，没问题再交给你。全程待在同一个窗口里，不用再开两个客户端当“传话筒”。

## 为什么要做它（设计理念）

用过 Gemini（如 Gemini 4 Neon）和 GPT（如 GPT-6.2 Astra）配合干活的人，大概都有同感：
- **Gemini 速度快、性价比高、表达自然、看图与交互能力强**，而且极其听话，中途插话改方向非常丝滑；但遇到跨模块深层排障或大规模重构时，耐力和推理深度不如重型旗舰。
- **GPT 工程硬实力极强**，查源码、改代码、跑复杂测试很稳；但直接面向人输出时经常一股“免责声明 + 机械分点”的官腔，而且写代码时偶尔会“好心办坏事”（私自加硬编码超时、截断、禁网或误删旧逻辑）。

以前的做法要么是“单模型硬扛”，要么是“A 跑完一整轮再硬塞给 B”，既浪费 token 又没人兜底。MixHarness 围绕 **LLM Cascading（级联升级）+ 独立监工闭环** 重新设计了协作方式：

1. **级联快速通道（省 Token、低延迟）**：
   - 收到消息后先由 **Gemini 前哨** 判断：纯问答、概念讨论、文案润色或不需要碰代码的日常请求，Gemini 直接一轮答完，**不惊动 GPT，也不走多余的报告环节**。
2. **按需升级 GPT + 事前立红线**：
   - 真正需要查源码、改代码、跑测试或打包构建时，才升级派单给 **GPT**；
   - 只要涉及修改代码（`develop`），Gemini 会同步注入**施工红线**（禁止私自添加硬编码超时、截断、禁网、静默吞错，禁止误删已有功能或绕过测试）。
3. **Gemini 独立监工（事后查真实 `git diff`）**：
   - GPT 改完代码后，引擎自动提取真实 `git diff` 与测试日志，在**独立干净的上下文**中拉起 Gemini 监工审查——不轻信 GPT 的口头汇报，直接核查每一行改动：
     - **通过（`pass`）**：放行并由 Gemini 整理成人话总结；
     - **打回（`reject`）**：指出具体文件名与问题，自动退回 GPT 返工（最多 2 轮防死循环）；
     - **上报（`escalate`）**：遇到需要权衡的重大取舍，直接高亮出来交给你拍板。
4. **写到缺证据时反向补测 + 按轨道增量复用**：
   - Gemini 写长报告发现缺少实测数据时，可直接调用 `request_verification` 叫 GPT 进项目补测，拿到退出码和日志后接着写；
   - 需求按「开发与研究 / 验证 / 报告」独立记版本，只改报告读者或篇幅时不会让 GPT 把代码和测试全重跑一遍。

## 核心能力与构建方式

底层是一套 Python 任务引擎 + SQLite（WAL）本地状态库，外层提供 PySide6 (Qt) 原生桌面端、交互式 CLI 与本地 Web 服务：

- **双执行后端（本机客户端 / 官方 API）**：
  - **本机客户端模式（默认推荐）**：直接通过官方本地集成协议调用已登录的 `agy`（Antigravity ACP）和 `codex`（Codex App Server），直接复用你现有的客户端订阅额度、模型目录、思考强度和套餐用量查询。Codex 使用临时线程，Antigravity 在 MixHarness 专属目录中续接会话，不污染你原有客户端的历史列表。
  - **官方 API 模式**：填入 `GEMINI_API_KEY`（或 `GOOGLE_API_KEY`）与 `OPENAI_API_KEY`，直连 Gemini Interactions API 与 OpenAI Responses API。
- **长会话省 Token 与防截断机制**：
  - Antigravity 协作会话采用**增量事件同步（`antigravity_seen`）**，不再每轮重灌整段历史，长会话大幅降低输入 token 消耗；
  - 序列化上下文时将**当前最新提问、待处理需求、施工红线与草稿置前**，大段历史记录置后，并在客户端自动窗口模式下按安全预算自动折叠早期历史，避免长会话在尾部被截断导致最新消息丢失。
- **真实项目构建、网络放开与安全隔离**：
  - Codex 沙箱在保留只读/工作区文件保护的同时默认放开网络访问（`networkAccess: true`），`gpt-dot` 系列模型不再被强制锁死在只读模式，按你选择的权限正常执行代码修改、编译打包与测试；
  - 提供受审批保护的 `run_command`（在真实项目目录执行打包、构建或测试命令）与 `run_check`（在 `.mixharness/snapshots` 独立快照中跑已注册检查），均支持随时中止且不设短命硬编码超时；
  - 编辑文件前核对 SHA256，防止覆盖你手动改过的源码；左下角提供三档权限（**请求批准** / **帮我批准** / **完全访问**），并支持单击闪电切换 GPT 的 **Fast / Ultrafast** 档位与 **Daybreak** 模式。
- **多模态、共享窗口与桌面操作（Computer Use）**：
  - 支持直接粘贴、拖拽或上传图片（PNG / JPEG / WebP）与视频（MP4 / MOV / WebM，桌面端与 Web 端均支持内置播放与预览），以及引用 MixHarness / Codex / Antigravity 历史会话；
  - 支持将单个应用窗口或整个桌面共享给当前会话，按权限审批后可执行截图观察、控件读取与界面操作。
- **丰富的协作与直聊模式**：
  - **双模型协作 / 单模型直聊 / GPT-dot 快捷入口**：随时切换让 Gemini 或 GPT 单独回复，全部留在同一条会话记录里；
  - **计划模式（`/plan`）**：先只读调研并提出步骤与验收条件，确认后再点「执行当前计划」；
  - **目标模式（`/goal`）**：持续核对交付依据并自动推进下一步，直到达成目标或明确阻塞点；
  - **侧边问答（`/btw`）**：基于当前背景开一个独立只读问答，不打断主线任务；
  - **右侧快速导航与工作树分支**：点进会话自动定位到最新消息，右侧圆点支持快速跳转任意一轮对话；Git 项目支持 `/fork --worktree` 开独立工作树并行开发。

## 快速上手

### 1. 打开桌面应用

- **macOS**：直接打开 `MixHarness.app`（可拖进「应用程序」或 `~/Applications`）。
- **Windows**：解压发行包后运行 `MixHarness.exe`（保留同目录 `_internal`）。
- **Ubuntu**：解压 tar.gz 后运行 `./MixHarness`。若缺少 Qt 图形库，先安装依赖：
  ```sh
  sudo apt install libegl1 libgl1 libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 libxkbcommon-x11-0
  ```

发行包自带 Python、Qt 与两家官方 SDK，开箱即用。想切换历史会话库，选菜单栏「文件 → 打开会话库…」选中包含 `.mixharness` 的目录即可。

### 2. 从源码启动

需要 Python 3.11+，在仓库根目录安装依赖并运行：

```sh
python -m pip install -e .
python start.py
```

- 仅使用交互命令行：`python start.py cli`
- 启动本地网页服务：`python start.py --web --port 8765`

### 3. 连接方式设置

- **本机客户端（默认推荐）**：确保电脑已登录 `agy` 和 `codex`。若装在非默认路径，可通过 `MIXHARNESS_ANTIGRAVITY` 和 `MIXHARNESS_CODEX` 指定。
- **官方 API**：在界面左下角「设置」中填写 `GEMINI_API_KEY` 与 `OPENAI_API_KEY`（保存在当前会话库 `.env`，优先读取环境变量）。

### 4. 常用快捷指令

在桌面输入框开头输入 `/` 可弹出中英文筛选菜单；终端在项目目录运行 `mixharness`（打包版为 `mixharness-cli`）共用同一套指令：

| 分类 | 常用指令 |
| --- | --- |
| 会话与项目 | `/new`、`/resume`、`/history 关键词`、`/rename 名称`、`/fork 名称`、`/project 目录`、`/add-dir 目录` |
| 侧边与分享 | `/btw 问题`、`/share`、`/feedback`、`/archive` |
| 模型与权限 | `/model gemini`、`/effort openai high`、`/permissions auto`、`/backend clients`、`/usage codex`、`/fast`、`/daybreak on` |
| 开发与审查 | `/init`（维护 `AGENTS.md`）、`/plan 要求`、`/implement`、`/review 关注点`、`/diff` |
| 模式与目标 | `/mode default`、`/goal 目标描述`、`/goal status`、`/goal pause`、`/goal resume`、`/goal clear` |
| 任务与控制 | `/status`、`/approvals`、`/approve 编号`、`/deny 编号`、`/stop`、`/continue`、`/retry 任务编号`、`/export 路径` |
| 技能与上下文 | `/settings`、`/skills`、`/context 64000`、`/compact`、`/tools`、`/mcp antigravity` |

也支持非交互式脚本调用：

```sh
# 查看模型、历史会话与额度
mixharness models --source antigravity
mixharness models --source codex
mixharness chats --source all
mixharness usage --source codex

# 创建项目并运行双模型协作
mixharness project-create "示例项目" --folder .
mixharness run --backend clients --project-id PROJECT_ID "检查项目代码并写份简明说明"
mixharness send SESSION_ID --gemini-effort low --openai-effort high "把说明精简到一页"
mixharness status SESSION_ID
```

## 技能（Skills）、上下文与自定义命令

- **技能管理**：在「设置 → 技能与上下文…」或 `/skills` 中管理项目 `.agents/skills` 与本机常见客户端目录的 `SKILL.md`，启用后下一轮自动生效。
- **上下文窗口**：默认 `auto`（本机客户端自动启用安全折叠预算），也可手动指定 `4,096 ～ 2,000,000` token 或随时用 `/compact` 压缩历史摘要。
- **自定义检查命令**：在项目根目录放一个 `mixharness.toml` 即可注册项目专属命令（内置 `python-tests` 与 `python-compile`）：

```toml
[checks]
python-tests = ["@python", "-m", "unittest", "discover", "-s", "tests", "-v"]
build-desktop = ["@python", "packaging/build_desktop.py"]
check-desktop = ["@python", "packaging/check_desktop.py"]
```

## 测试与打包

运行全部单元测试：

```sh
python -m unittest discover -s tests -v
```

本地打包桌面版并执行发行包自检（产物生成在 `dist/`）：

```sh
python -m pip install -e ".[build]"
python packaging/build_desktop.py
python packaging/check_desktop.py
```

三端 CI 自动构建配置见 `.github/workflows/test.yml`。

## 友情推荐

如果你对多 Agent 协作和随身 AI 助手也感兴趣，推荐看看朋友开源的项目 **[Reduction](https://github.com/mindtravel/Reduction)** —— 一套把蓝牙耳机、安卓手机、电脑桌面（Windows 桌宠 / WSL / Linux）和可选云中转串起来的智能随身助手：

- **熄屏纯语音交互**：戴着蓝牙耳机说 “Hi Reduction” 就能唤醒记录想法或派发任务，说“完毕”结束录音，不用频繁掏手机。
- **卡片式信息整理**：基于 CARD 卡片协议，把日常事务、图片和文档归类整理成卡片，平时不用盯着屏幕，有空时一次性集中审核；支持私人标注，公开导出时自动过滤隐私内容。
- **跨端多 Agent 调度**：采用 super agent + sub agent 分工，随身端负责理解意图与派发任务，电脑端负责执行工具、读取授权目录、查看电脑上其他编程 Agent 状态以及语音管理 GitHub 动态，任务产出还会经独立核验后才记为完成。
- **轻量开源**：最小配置只需一个 DeepSeek API Key，密钥与数据留在本地设备上，采用 MIT 协议开源。
