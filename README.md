# MixHarness

MixHarness 是一个让 Gemini 和 GPT 在同一个本地会话里实时配合的跨平台桌面工作台（附带同名 CLI），支持 macOS、Windows 和 Ubuntu。

简单来说：**GPT 负责幕后写代码、查资料、跑测试，Gemini 负责理解你的想法、追问细节并写出让人看得懂的报告。** 你们始终待在同一个聊天窗口里，不用再开两个客户端来回复制粘贴当“传话筒”。

## 为什么要做它（设计理念）

用过两个模型配合干活的人大概都有过同一种体验：
- **GPT 工程能力强、严谨**，改代码、定位 bug、跑命令、做推导很稳，但它直接吐出来的总结往往一股“免责声明+机械结构”的味道，人看着累；
- **Gemini 表达自然、擅长整理大段文字**，写报告、说明书、技术方案明显更顺口，但一旦需要进项目翻代码、算性能数据、跑测试验证，还是得靠 GPT。

现有的工作流大多是“A 跑完一整轮 → 把结果硬塞给 B”的单向流水线，没法在同一个上下文里**边跑边改**，也没法让负责写作的模型在发现“这里缺个实测数据”时**直接叫工程模型补测一轮再继续写**。

MixHarness 就是为了解决这两个问题设计的：
1. **两边共用一份本地会话，各自保留原生上下文**：你在一个窗口里提需求、随时插话改要求，底层同时维护给 Gemini 的交互和给 GPT 的任务。
2. **分工明确，写到缺证据时自动补测**：Gemini 写报告时如果发现某个结论缺少实测数据，会直接调用 `request_verification` 把具体问题抛给 GPT；GPT 在项目里跑完实际命令拿到退出码和日志后，结果原路送回 Gemini 当初停下的那一步，报告接着往下写。
3. **不重跑无用功**：需求按「开发与研究 / 验证 / 报告」拆开记版本。比如程序已经测通了，你只是把“报告读者改成产品经理”或者“精简到一页”，MixHarness 会直接让 Gemini 复用之前的工程结果改写，不会让 GPT 把代码和测试全重跑一遍。

每轮「需求整理」会显示用户原话、Gemini 对要求的理解，以及实际派给两个模型的任务。解释已有项目时，GPT 可以只读查阅源码；依赖调研结果的报告会等待 GPT 返回。聊天记录会标出客户端确认读取的项目文件、任务是否成功，以及是哪一端申请了批准。遇到 Codex 权限错误，可以展开原始错误，或在查阅资料时点「只读重试 GPT」。

在桌面输入框里，点击加号旁的 **双模型协作**，选择 **Gemini** 或 **GPT**，就能在当前会话直接提问。回复会出现在同一条消息记录里，可以继续追问，也可以切换模型接着聊。切回 **双模型协作** 后，下一条消息按当前的默认、计划或目标模式处理。协作任务运行或暂停期间也能单独向 Gemini 提问。

## 它是怎么搭出来的（构建方式）

整个工具的核心是一套 Python 后端任务引擎 + SQLite 本地库，外层同时接了三端共用的 Qt 原生桌面端和命令行工具：

- **统一桌面端与 CLI**：桌面端用 PySide6 (Qt) 写成，布局参考 Codex，黑白双色主题，macOS、Windows、Ubuntu 共用同一套界面与打包脚本；喜欢终端的话，在项目目录敲 `mixharness` 就能进入交互式命令行，桌面开着时命令行直接连进同一个引擎和队列，两边状态实时同步。
- **双执行后端（本机客户端 / 官方 API）**：
  - **本机客户端模式**：直接调用你电脑上已经登录的 `agy`（Antigravity）和 `codex`（Codex App Server）。好处是直接复用你现有的客户端订阅额度、看到和客户端一模一样的模型和思考强度列表，还能直接查看套餐剩余额度与重置时间、在侧栏翻看 Codex 和 Antigravity 的历史会话。为了不把你平时用的客户端列表塞满零碎任务，Codex 走的是内存临时线程，Antigravity 则在 MixHarness 专属的数据目录里续接同一条会话，两边原有历史完全不会被污染。
  - **API 模式**：支持填入 `GEMINI_API_KEY`（或 `GOOGLE_API_KEY`）与 `OPENAI_API_KEY`，直连 Gemini Interactions API 与 OpenAI Responses API。其中 GPT-6 系列通过 Responses WebSocket 在生成过程中实时接收补充要求。
  - 哪怕任务因为缺 API Key 停住了，直接在右下角切到「本机客户端」，刚才那条消息就会无缝接着跑下去。
- **文件安全与快照隔离**：
  - 工程修改文件前会核对读取时的 SHA256，防止覆盖你在编辑期间手动改过的代码；
  - 验证命令都在 `.mixharness/snapshots` 的独立文件副本里执行，不会在原代码目录留下临时缓存或测试垃圾；如果验证期间你又改了源码，引擎会自动作废旧结果并按新文件重测；
  - 输入框下方支持三档权限（**请求批准** / **帮我批准** / **完全访问**，对应 Antigravity 的 Default / Auto / Turbo）。单击 **GPT 模型名前的闪电** 开启 Fast，闪电变蓝，再点一次恢复标准速度。在 API 会话中，点击闪电会使用本机客户端开启 Fast。输入 `/fast` 可查看当前模型支持的档位，**Ultrafast** 需要 Pro 200 或以上套餐及模型支持。
  - 使用本机 `gpt-dot` 系列模型时，Codex 以只读权限分析项目。需要写文件或运行项目验证时，Dot 会向 MixHarness 提交工具请求。「请求批准」模式会显示文件路径与拟写入的内容，或要运行的验证命令。
  - 输入框上方显示正在工作的模型和任务。执行中可直接输入补充要求，按 Enter 引导当前任务。点「停止」或在输入框按 Esc 可中断工作并保留草稿。点「继续」从原任务接着做，也可以输入新的要求后继续。关闭再打开应用，仍可继续。等待批准的操作会在继续时重新请求批准。
  - 交互终端使用 `/stop` 或 Ctrl+C 停止，使用 `/continue` 继续。任务运行时，直接输入文字或使用 `/steer 补充要求` 均可引导任务。需要重新分配开发、验证和报告工作时，使用 `/revise 新需求`。
  - 在「本机客户端」会话里，如果 Codex 账户已获 **Daybreak Blue** 授权，点 GPT 模型名，菜单第一项就是 **Daybreak 模式**。也可以点输入框下方的 Daybreak 开关。开启后，不兼容的 GPT 模型会变灰。如果当前模型不兼容，MixHarness 会选中目录里的第一个兼容模型。终端可用 `/daybreak on` 和 `/daybreak off` 切换，也可用 `mixharness chat --daybreak` 开始会话。
  - 桌面和交互终端都会排版 Markdown 回复。桌面的蓝色链接可点击，项目文件链接支持跳到指定行。支持终端超链接（OSC 8）的终端也可打开回复里的链接。
  - Gemini 根据 GPT 的开发或验证结果写报告时，桌面会话默认收起 GPT 的中间输出。点击消息左侧的箭头可以查看正在生成的草稿、完整结果和读取记录。Gemini 的报告和 GPT 单独回答的问题会直接显示。
  - 在输入框下方选择**计划模式**，GPT 会只读检查项目，提出步骤和验收条件。你可以发消息修订方案，确认后点「执行当前计划」。本机客户端使用 Codex 原生计划模式。
  - 选择**目标模式**后，第一条消息就是当前会话要达成的目标。每轮工作结束时，GPT 会核对交付结果，记录依据，继续未完成的步骤，或说明还需要你补充什么。模式菜单里可以查看、暂停和继续目标，进度保存在会话库中。
  - 点输入框左下角的 **＋** 可以添加图片、引用 MixHarness / Codex / Antigravity 会话、指定工具或使用本次消息的技能。会话引用保存最近 32 条可读取消息的文字节选，最多 24,000 字。点附件可以预览，点 × 可以移除。
  - 图片也可以直接粘贴或拖进输入框，支持 PNG、JPEG、WebP。命令行使用 `mixharness run --image 截图.png "请分析这张图"`，也可以用 `mixharness send 会话编号 --image 截图.png` 给已有会话补图。

## 快速上手

### 1. 打开桌面应用

- **macOS**：直接打开 `dist/MixHarness.app`（可拖进「应用程序」）。
- **Windows**：解压发行包后运行 `MixHarness.exe`（保留同目录 `_internal`）。
- **Ubuntu**：解压 tar.gz 后运行 `./MixHarness`。如果系统缺少 Qt 图形库，先装一下依赖：
  ```sh
  sudo apt install libegl1 libgl1 libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 libxkbcommon-x11-0
  ```

发行包里已经自带了 Python、Qt 和两家 SDK，开箱即用。如果想切回之前某个项目下的历史库，选菜单栏「文件 → 打开会话库…」选中包含 `.mixharness` 的目录即可。

### 2. 从源码启动

需要 Python 3.11+，在仓库根目录运行：

```sh
python start.py
```

- 只想用命令行：`python start.py cli`
- 想启动本地网页服务：`python start.py --web --port 8765`

### 3. 连接方式设置

- **用本机客户端（默认推荐）**：确保电脑上已经登录 `agy` 和 `codex` 即可。如果程序装在非默认位置，可以用环境变量 `MIXHARNESS_ANTIGRAVITY` 和 `MIXHARNESS_CODEX` 指定路径。
- **用官方 API**：点界面左下角「设置」填写 `GEMINI_API_KEY` 和 `OPENAI_API_KEY`（保存在当前会话库的 `.env`，也会优先读取启动环境变量）。支持点击模型名下方的搜索框拉取账户模型或手动输入模型 ID，点击旁边档位单独调整各自思考强度。

### 4. 命令行与常用指令

在桌面输入框开头键入 `/` 会弹出快捷指令。继续输入中文或英文可筛选，例如 `/审查` 或 `/rev`。按 ↑ ↓ 选择，Enter 或单击执行，Tab 补全后可接着填参数，Esc 收起。终端在项目目录运行 `mixharness` 即可使用同一套指令（打包版命令为 `mixharness-cli`）。

| 分类 | 常用指令 |
| --- | --- |
| 会话与项目 | `/new`、`/resume`、`/history 关键词`、`/rename 名称`、`/fork 名称`、`/project 目录`、`/add-dir 目录` |
| 侧边与分享 | `/btw 问题` 打开独立的只读问答，`/share` 生成可粘贴到浏览器地址栏的文字快照链接，`/feedback` 打开项目反馈，`/archive` 归档当前会话 |
| 模型与权限 | `/model gemini`、`/effort openai high`、`/permissions auto`、`/backend clients`、`/usage codex`、`/fast`、`/daybreak on` |
| 开发与审查 | `/init`（维护 `AGENTS.md`）、`/plan 要求`、`/implement`、`/review 关注点`、`/diff` |
| 模式与目标 | `/mode default`、`/goal 目标描述`、`/goal status`、`/goal pause`、`/goal resume`、`/goal clear` |
| 任务与控制 | `/status`、`/approvals`、`/approve 编号`、`/deny 编号`、`/stop`、`/retry 任务编号`、`/export 路径` |
| 技能与上下文 | `/settings`、`/skills`、`/context 64000`、`/compact`、`/tools`、`/mcp antigravity` |

单目录 Git 项目可从桌面的「创建聊天分支」选择新工作树，也可输入 `/fork --worktree 新目录`。新工作树从当前提交 HEAD 开始。分享快照保存在链接内，长对话可以保存成 Markdown 文件发送。

也支持非交互式的脚本调用（加 `--library 目录` 可指定数据库路径）：

```sh
# 列出模型、会话和连接状态
mixharness models --source antigravity
mixharness models --source codex
mixharness chats --source all
mixharness usage --source codex

# 创建项目并跑一次双模型协作
mixharness project-create "示例项目" --folder .
mixharness run --backend clients --project-id PROJECT_ID "检查 Python 语法并写份简明说明"
mixharness send SESSION_ID --gemini-effort low --openai-effort high "把读者改成新加入的开发者"
mixharness status SESSION_ID
```

## 技能（Skills）与上下文窗口

在桌面「设置 → 技能与上下文…」或命令行 `/skills` 中，可以管理项目内的 `.agents/skills`、本机常见客户端目录及自定义目录里的 `SKILL.md`。勾选启用后，两边模型在下一轮都会带上对应技能正文。

如果不想把完整对话全量塞给模型，可以手动指定共享上下文窗口（4,096 ～ 2,000,000 token，设 0 或 `auto` 则不限制）。MixHarness 会用 `o200k_base` 估算长度，优先保住当前需求、启用技能和工具调用编号，自动截断早期历史和过长输出；也可以随时用 `/compact` 手动把前面聊过的内容压成摘要。

## 自定义验证命令

应用内置了 `python-tests`（unittest）和 `python-compile`（compileall）。如果你的项目有自己的测试或构建脚本，可以在项目文件夹里放一个 `mixharness.toml` 注册验证命令：

```toml
[checks]
project-tests = ["@python", "-m", "unittest", "discover", "-s", "tests", "-v"]
```

其中 `@python` 会优先使用 `MIXHARNESS_PYTHON` 或项目里的 `.venv`；如果是简单的 `unittest` / `compileall`，桌面发行包也能直接用自带解释器跑通。

## 测试与打包

运行全部单元测试（共 24 项，覆盖工作流、项目管理、CLI、HTTP 接口与 Qt 桌面控件交互）：

```sh
python -m unittest discover -s tests -v
```

如果配置了双端 API Key 或登录了两个本机客户端，还可以跑真实联调测试（日志保存在 `.work/`）：

```sh
python -m tests.live_workflow
python -m tests.live_clients
```

在对应系统上本地打包桌面版并执行发行包自检（构建产物生成在 `dist/`）：

```sh
python -m pip install -e ".[build]"
python packaging/build_desktop.py
python packaging/check_desktop.py
```

三端 CI 自动构建配置见 `.github/workflows/test.yml`。
