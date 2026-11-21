# MixHarness

MixHarness 是 Ubuntu、Windows 和 macOS 共用的一款 Qt 桌面应用。Gemini 和 GPT 读取同一个本地会话。Gemini 负责需求和报告，GPT 负责工程、研究和验证。任务运行时可以继续发消息，也可以在输入框底部的两栏独立选择模型。左侧栏管理会话，右上角打开任务与成果，左下角切换黑白主题。

## 使用本机客户端

点击输入框右下角的「API」或「本机客户端」，可以在当前会话中切换执行方式。切到本机客户端时，左栏读取 Antigravity 的模型和推理档位，右栏读取 Codex 的可见模型。等待 API 密钥的消息会自动继续执行。已经发出的模型请求可以完成当前一轮，后续调用使用新选择，并读取已有消息、草稿和验证结果。每个会话分别记住两种方式使用的模型。

本机需要已登录的 **agy** 和 **codex**。应用会查找系统 PATH、Antigravity 的用户安装目录和 macOS 客户端内置的 Codex 程序。也可以使用环境变量 **MIXHARNESS_ANTIGRAVITY** 和 **MIXHARNESS_CODEX** 指定可执行文件路径。[Antigravity ACP](https://antigravity.google/docs/cli/acp/) · [Codex App Server](https://developers.openai.com/codex/app-server/)

点击输入框里的模型名称可以切换模型，点击旁边的思考档位可以单独调整该栏。Gemini 菜单将同一个模型的 Low、Medium、High 等档位折叠在一项内。在本机客户端模式中，Orchestrator 的每个型号各占一项。在 API 模式中，「搜索全部模型」可以查找目录里的其他型号或输入模型 ID。Codex 显示当前客户端为该模型提供的档位。两栏选择按会话和执行方式分别保存。已发出的请求完成当前一轮，下一次调用使用该任务最新的档位。

在任一模型菜单底部点击「查看套餐用量」，可以切换 Antigravity 和 Codex、查看剩余额度与本地时间显示的重置时间，并手动刷新。Antigravity 使用官方 CLI 状态栏提供的账户配额，查询时启动空闲客户端并在取得数据后关闭。Codex 使用 App Server 的账户限额接口。「API 用量」打开相应服务商的 API 账单页面。[Antigravity 状态栏字段](https://antigravity.google/docs/cli/statusline) · [Codex App Server](https://developers.openai.com/codex/app-server/)

侧栏的会话来源菜单可以选择「全部来源」、Codex 或 Antigravity。点击客户端会话直接查看原始消息，可刷新和继续分页。主会话的需求、任务和证据保存在 MixHarness。Codex 使用内存线程，Antigravity 在主会话专属的数据目录中续接同一条会话。客户端原有的历史目录用于浏览。

### 操作权限与 Codex 速度

输入框下方的权限选择按会话保存，默认使用「请求批准」。任务需要批准时，点击输入框上方的操作名称可查看命令、路径或待写入的内容，并选择允许、拒绝或稍后处理。套餐用量等弹窗可用关闭按钮或 Esc 退出。模型分组和项目操作的子菜单还提供「返回上一级」。

| MixHarness | Antigravity | Codex 与共享工具的行为 |
| --- | --- | --- |
| 请求批准 | Default | Codex 启用工作区沙箱并将审批交给用户。共享工具写文件和运行项目命令前会显示请求。 |
| 帮我批准 | Auto | Codex 使用自动审批。共享工具可按文件版本写入项目文件，并可使用应用自带的 Python 独立执行标准语法检查。使用项目解释器或运行其他验证命令时会请求批准。 |
| 完全访问 | Turbo | 在当前会话中启用客户端的完全访问权限，并自动执行共享工具操作。 |

在本机客户端模式中，Codex 速度菜单按当前账户和模型显示。MixHarness 只对 Pro 200 及以上个人套餐显示客户端实际提供的 Fast 或 Ultrafast。切换 GPT 型号时恢复标准速度，发起加速请求前会再次读取账户资格。API 调用使用 API 账户计费。[Codex 套餐与处理速度](https://help.openai.com/en/articles/20001275-chatgpt-work-and-codex)

## 命令行

在项目目录运行 **mixharness** 即可进入交互式命令行。首次发送消息时使用当前目录创建或打开项目。**mixharness chat --standalone** 使用独立工作目录，**mixharness chat 会话编号** 继续已有会话。桌面发行包内的程序名为 **mixharness-cli**，Windows 为 **mixharness-cli.exe**，macOS 的位置为 **MixHarness.app/Contents/MacOS/mixharness-cli**。从源码也可以运行 **python start.py cli**。

交互中直接输入需求。任务运行期间可继续输入，新消息会进入同一个工作流。Tab 补全指令，Esc 再按 Enter 可输入换行，Ctrl+C 清除当前输入，Ctrl+D 或 **/exit** 退出。输入 **/help** 查看当前可用的指令。桌面输入框也接受斜杠指令。

| 用途 | 常用指令 |
| --- | --- |
| 会话和项目 | **/new**、**/resume**、**/history 关键词**、**/rename 名称**、**/project 目录**、**/add-dir 目录** |
| 模型与权限 | **/model gemini**、**/effort openai high**、**/permissions auto**、**/backend clients**、**/usage codex** |
| 开发与研究 | **/init** 维护项目说明，**/plan 要求** 制定方案，**/review 关注点** 审查项目，**/diff** 查看工作区和暂存区改动 |
| 任务与证据 | **/status**、**/approvals**、**/approve 编号**、**/deny 编号**、**/stop**、**/retry 任务编号**、**/export 文件路径** |
| 配置与上下文 | **/settings**、**/skills**、**/context**、**/compact**、**/tools**、**/mcp antigravity** |

**/fork 名称** 将当前消息与需求复制到同一工作目录下的新会话，原会话保留任务和验证成果。新分支使用「请求批准」与标准速度。脚本可以用 **mixharness command "/status" --session 会话编号** 调用指令，加上 **--json** 可以取得会话编号和结构化返回值。

~~~sh
mixharness models --source antigravity
mixharness models --source codex
mixharness chats --source all
mixharness connections
mixharness usage --source antigravity
mixharness usage --source codex
mixharness project-create "报告工具" --folder .
~~~

将下面的 PROJECT_ID 换成上一步返回的项目编号，SESSION_ID 换成主会话编号。外部历史使用相应来源列表返回的编号。命令以 JSON 返回数据，分页时将 **next** 传给 **--cursor**。

~~~sh
mixharness run --backend clients --project-id PROJECT_ID "检查项目的 Python 语法，核实后撰写说明"
mixharness send SESSION_ID "将报告读者改为开发者"
mixharness status SESSION_ID
mixharness send SESSION_ID --gemini-effort low --openai-effort high "用当前项目的验证结果更新报告"
mixharness history SESSION_ID
mixharness history --source codex CODEX_CHAT_ID
mixharness history --source antigravity ANTIGRAVITY_CHAT_ID
~~~

**run** 和 **send** 默认等待任务结束。遇到待批准操作时会返回审批记录。单独启动的客户端正在等待原生审批时，终端会显示可在另一终端执行的批准指令。**--no-wait** 只提交消息。桌面端打开时，命令会连接同一会话库的运行中引擎。独立使用时，可用 **mixharness serve** 持续处理队列，或用 **mixharness wait SESSION_ID** 执行待处理任务。**--library 目录** 选择配置与数据目录，放在子命令之前。

### 技能与上下文窗口

在桌面「连接设置 → 技能与上下文窗口」中可以添加技能目录、勾选技能和修改共享输入预算。应用发现项目的 **.agents/skills**、两家客户端常用的个人技能目录以及手动添加的目录。启用后，两边模型的下一轮任务都会收到对应 **SKILL.md** 的正文。项目内的技能选择保存在该项目，个人技能和上下文设置保存在当前会话库。[Antigravity 技能说明](https://antigravity.google/docs/skills)

~~~sh
mixharness settings --list-skills
mixharness settings --skill-dir "./skills" --enable-skill report-writing
mixharness settings --context-window 64000
~~~

**report-writing** 需替换为实际技能名称。交互中可用 **/skills on 名称**、**/skills off 名称**、**/context 64000** 或 **/context auto**。手动预算范围为 4,096 至 2,000,000 token。MixHarness 用随应用提供的 o200k_base 词表估算共享输入，保留当前需求、启用的技能和工具调用编号，按预算压缩历史和较长的工具结果。**/compact** 会在本会话保存历史摘要，原消息仍可查看或导出。自定义窗口也会传给 Codex 的模型上下文配置。Antigravity 管理其原生会话历史，各服务商的模型上限仍然适用。

## 打开桌面应用

- **macOS** 打开 dist 中的 **MixHarness.app**。也可以将应用拖入「应用程序」。
- **Windows** 解压对应的桌面发行包，在 MixHarness 文件夹里双击 **MixHarness.exe**，并将同目录的 _internal 文件夹与程序一起保留。
- **Ubuntu** 解压对应的 tar.gz，在 MixHarness 文件夹中运行 **./MixHarness**。若系统尚未安装 Qt 所需的图形库，请运行 `sudo apt install libegl1 libgl1 libxcb-cursor0 libxcb-icccm4 libxcb-keysyms1 libxcb-shape0 libxkbcommon-x11-0`。

发行包包含 Python、Qt 和两家服务商的 SDK。直接打开应用即可使用。首次启动使用当前用户的应用数据目录。「连接设置」中可查看配置文件的完整路径。要继续已有会话，选择「文件 → 打开已有会话库」，然后选择包含 **.mixharness** 的原项目目录。

## 从源码启动和打包

源码要求 Python 3.11 或更高版本。在项目根目录运行以下命令。

~~~sh
python start.py
~~~

Ubuntu 和 macOS 可使用 **python3 start.py**，Windows 可以使用 **py -3.12 start.py**。首次启动会在项目内创建虚拟环境并安装依赖，随后打开桌面窗口，使用本项目的配置文件与会话库。需要调用本地 HTTP 接口时，运行 **python start.py --web --port 8765**。

在目标操作系统上，使用同一 Python 环境生成桌面发行包。构建产物位于 **dist**。

~~~sh
python -m pip install -e ".[build]"
python packaging/build_desktop.py
python packaging/check_desktop.py
~~~

## API 密钥与项目目录

在 API 模式下，输入框下方分别显示 Gemini 和 OpenAI 的密钥配置状态。任务缺少密钥时，输入框上方显示等待原因。点击提示或左下角「连接设置」可以查看生效来源并填写密钥。应用优先读取启动进程的非空环境变量，再读取当前会话库配置目录的 **.env**。支持 **GEMINI_API_KEY**、**GOOGLE_API_KEY** 和 **OPENAI_API_KEY**。保存或从外部编辑配置文件后，运行中的引擎会重新读取并继续等待密钥的任务。模型目录请求中的认证错误可在对应模型选择器中查看。[Google AI Studio 密钥](https://aistudio.google.com/apikey) · [OpenAI Platform 密钥](https://platform.openai.com/api-keys)

点击侧栏「项目」右侧的「＋ 新建项目」，填写项目名称，按需添加一个或多个来源文件夹。留空时应用会为项目创建专属工作目录。已有代码目录也可以通过「文件 → 打开项目」直接打开。点击项目行右侧的写字图标即可在这个项目新建会话。首次发送消息后，会话才会出现在历史列表中。切换项目和重新启动都会保存未发送的草稿。「最近独立会话」旁的写字图标可以开始项目以外的会话。可在 .env 中用 **MIXHARNESS_WORKSPACE** 指定初次启动的默认代码目录。

项目行的「⋯」可以置顶、编辑或从侧栏移除项目。「编辑项目」支持修改名称、添加和移除来源文件夹，以及选择主要文件夹。会话行的「⋯」和右键菜单提供置顶、重命名、归档和删除。左下角「归档管理」可以恢复会话和已移除的项目。删除需要确认，会移除该会话的消息、草稿、任务、工具调用和成果。已开始的本地验证结束后，应用会清理该会话的验证快照和日志，项目源文件继续保存在工作目录。按 **⌘K** 或 **Ctrl+K** 可以按项目名称、会话标题或消息正文搜索。

Gemini 和 OpenAI 两栏分别显示可搜索的模型列表。填写密钥后，应用在启动、保存连接设置或点击「刷新目录」时向对应服务商读取账户目录。尚未取得账户目录时，选择器明确显示来自官方文档的常用模型参考列表。调用权限、余额以及模型对文本和函数调用的支持决定任务是否能够运行。模型选择按会话保存，也支持手动输入 ID。刷新目录会保留当前选择，已开始的工具调用仍使用原模型续接。[Gemini 模型列表接口](https://aistudio.google.com/api-reference/models/list-models) · [OpenAI 模型列表接口](https://developers.openai.com/api/reference/resources/models/methods/list)

## 会话和任务

- 后端按顺序保存用户消息、需求版本、任务、工具调用编号和成果。开发、验证和写作分别记录输入版本。修改读者或篇幅可以复用工程结果，更改验证条件会产生新的验证任务。桌面窗口直接从任务引擎显示进度和草稿，重新启动后可继续已保存的任务。
- Gemini 使用 request_verification 写明章节、问题和所需证据。后端保存 Gemini 的交互与调用编号，创建 GPT 子任务。验证结束后，后端使用原编号回传 function_result，继续撰写报告。失败原因也会回传。「任务」可查看进度并重试，「成果」可查看和下载报告、章节和验证日志。
- GPT-6 系列使用 OpenAI Responses WebSocket 接收生成期间的补充要求，其他 OpenAI 模型使用 Responses HTTP 接口。Gemini 使用 Interactions API。所选模型需支持相应的接口和工具调用。
- 工程任务可读取和编辑当前项目的文本文件。编辑使用读取时的 SHA256 检查版本。验证命令在源文件快照的独立副本中运行，保存命令、退出码、日志和快照摘要。验证期间源文件变化会触发重新检查。

## 注册项目的验证命令

内置 **python-tests** 和 **python-compile**，分别运行 unittest 发现和 Python 语法编译。各来源文件夹可以在自己的 **mixharness.toml** 中注册测试或基准命令。应用读取 .env 同目录的通用命令，再应用各来源文件夹的配置。多目录项目的验证会复制全部来源文件夹，并在选定目录运行。

~~~toml
[checks]
project-tests = ["@python", "-m", "unittest", "discover", "-s", "tests", "-v"]
~~~

**@python** 在设置了 **MIXHARNESS_PYTHON** 时使用指定的解释器，否则优先使用项目的 **.venv**。桌面发行包可以用内置解释器执行 unittest 与 compileall。依赖其他第三方库的项目应在项目环境中安装依赖。验证命令也可以直接使用本机工具，运行目录是项目快照的副本。

## 数据与测试

会话数据库、项目目录、快照和运行日志保存在 **.mixharness**。停止服务后可复制整个目录备份。中间构建和测试文件位于 **.work**。

在已安装本项目的 Python 环境中运行 **python -m unittest discover -s tests -v**。测试通过 Qt 控件发送消息、变更模型和恢复会话，并检查 SQLite、项目文件和实际子进程。三平台构建、发行包启动检查和归档配置位于 **.github/workflows/test.yml**。

配置两把密钥后，**python -m tests.live_workflow** 会实际调用两个模型，要求 Gemini 向 GPT 请求检查本项目源文件的语法，并核对原始进程退出码、工具调用编号和最终报告。该命令使用两家账户的 API 额度，记录保存在 **.work/live-check**。

安装并登录两个本机客户端后，**python -m tests.live_clients** 会使用客户端额度执行一次报告验证，再修改报告要求，检查续写时使用的会话编号和原生历史列表。验证项目和真实日志保存在 **.work**。
