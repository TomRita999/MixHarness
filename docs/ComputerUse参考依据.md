# Computer Use 本机源码核查

2026 年 11 月 26 日，只读检查了本机安装包、Codex 插件缓存和 Antigravity CLI。版本取自应用的 `Contents/Info.plist` 和包内的 `package.json`，原生文件类型由 `file` 确认。以下结论限于这些安装目录。

## Codex 与 ChatGPT

本机的 Codex CLI 位于 `/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app`。ChatGPT 版本为 `26.1120.41243`。下表的起始目录均为 `/Applications/ChatGPT.app/Contents/Resources`。

| 文件 | 可读内容 |
| --- | --- |
| `plugins/openai-bundled/plugins/computer-use/skills/computer-use/SKILL.md` | 操作说明、接口签名和示例。 |
| `plugins/openai-bundled/plugins/unified-computer-use/.mcp.json` | `cua_repl` 的启动配置和工具名称。 |
| `cua_node/lib/node_modules/@oai/cua/dist/lib/js/oai_js_cua/src/tinysky_alt/bind_mac_app.js` | `@oai/cua` 0.2.8 分发的 JavaScript 函数体。`getAXState` 和 `getScreenshot` 调用应用状态接口，`click` 将控件编号或坐标转换为请求参数，`drag` 传递起点和终点。 |
| `cua_node/lib/node_modules/@oai/sky/dist/project/cua/sky_js/src/targets/mac/native-pipe.js` | `@oai/sky` 0.7.26 分发的 JavaScript 通信实现。 |
| `native/sky.node` 与 `cua_node/lib/node_modules/@oai/sky/Codex Computer Use.app/Contents/MacOS/SkyComputerUseService` | ARM64 Mach-O 模块和可执行程序。 |

## Antigravity

本机 `/Applications/Antigravity.app` 版本为 `2.35.1`。以下字节偏移从 `Contents/Resources/app.asar` 的文件起点计算，可以定位到该版本实际分发的 JavaScript 定义。

| 定义 | 字节偏移 | 可读内容 |
| --- | ---: | --- |
| `probeHelperOnce` | 4379633 | 启动辅助程序的权限探测命令，检查其返回的辅助功能和屏幕录制授权状态。 |
| `desktopHelperPath` | 4406041 | 解析安装包内 `bin/agy-desktop-helper` 的路径。 |
| `MacComputerSurfaceDriver` | 4422469 | `capture` 提交截图请求并解码返回图像，`mouse`、`key`、`text` 和 `focusApp` 分别传递鼠标动作、快捷键、文本和目标应用。 |
| `startDesktopHost` | 4728723 | 创建 `DesktopHelper` 和 `MacComputerSurfaceDriver`，连接桌面操作与辅助程序。 |

`Contents/Resources/bin/agy-desktop-helper`、`agy-desktop-script`、`agy-desktop-rtc` 及 `~/.gemini/bin/agy` 均为 ARM64 Mach-O 可执行程序。两套安装包提供了可以参考的 JavaScript 调用和状态处理代码。在检查的 Computer Use 包及辅助程序目录中，未找到与这些原生二进制对应的工程源码。
