from typing import NamedTuple


class Command(NamedTuple):
    title: str
    description: str
    arguments: str = ""

    def usage(self):
        return "　".join(filter(None, (self.arguments, self.description)))


# The order here is also the order shown when the composer contains just "/".
COMMANDS = {
    "mcp": Command("MCP 服务", "查看客户端已配置的 MCP 服务与启用状态", "[codex|antigravity]"),
    "review": Command("代码审查", "让 GPT 审查当前项目的改动", "[关注点]"),
    "usage": Command("使用情况和计费", "查看套餐额度、重置时间和 API 用量", "[codex|antigravity|openai|gemini]"),
    "btw": Command("侧边聊天", "带着当前对话的上下文，单独问 GPT 一个问题", "[问题]"),
    "share": Command("分享聊天", "预览并分享当前对话的文字快照"),
    "fork": Command("创建聊天分支", "在当前工作区或新的 Git 工作树中继续", "[名称 | --worktree 目录]"),
    "init": Command("初始化项目说明", "阅读项目并创建或更新 AGENTS.md"),
    "compact": Command("压缩上下文", "精简模型读取的历史，保留完整聊天记录"),
    "feedback": Command("反馈", "向 MixHarness 项目反馈问题或建议", "[反馈内容]"),
    "archive": Command("归档聊天", "将当前会话移到归档管理"),
    "fast": Command("快速模式", "查看或切换 Codex Fast 加速档位", "[off|fast|ultrafast]"),
    "daybreak": Command("Daybreak", "切换当前账户授权的 Daybreak Blue 模型", "[on|off]"),
    "plan": Command("计划模式", "制定或修订只读计划", "[要求]"),
    "goal": Command("目标模式", "设定持续目标，查看进度、暂停或继续", "[目标|status|pause|resume|clear]"),
    "implement": Command("执行计划", "开始实施当前已制定的计划"),
    "mode": Command("切换模式", "选择默认、计划或目标模式", "[default|plan|goal]"),
    "new": Command("新建会话", "在当前项目开始新对话"),
    "resume": Command("继续会话", "选择或打开已有聊天", "[会话编号]"),
    "history": Command("搜索会话", "按标题或关键词查找历史聊天", "[关键词]"),
    "model": Command("选择模型", "查看或切换 GPT 和 Gemini 模型", "[gemini|openai] [模型 ID]"),
    "effort": Command("思考强度", "选择模型的思考档位", "gemini|openai [档位|default]"),
    "permissions": Command("操作权限", "设置操作是否需要批准", "[ask|auto|full]"),
    "backend": Command("执行方式", "选择本机客户端或官方 API", "[clients|api]"),
    "diff": Command("查看改动", "查看工作区和暂存区的 Git 差异"),
    "skills": Command("技能", "查看技能、启用技能或添加技能目录", "[on 名称|off 名称|add 目录|remove 目录]"),
    "tools": Command("可用工具", "查看工具与项目验证命令"),
    "context": Command("上下文窗口", "查看用量或设置上下文大小", "[auto|token 数]"),
    "status": Command("会话状态", "查看模型、权限和任务进度"),
    "rename": Command("重命名", "修改当前会话名称", "名称"),
    "export": Command("导出聊天", "将对话保存为 Markdown 文件", "[文件路径]"),
    "projects": Command("项目列表", "查看本地项目"),
    "project": Command("切换项目", "选择项目编号或文件夹", "项目编号或目录"),
    "add-dir": Command("添加目录", "把文件夹加入当前项目", "文件夹"),
    "approvals": Command("待批准操作", "查看会话中等待批准的请求"),
    "approve": Command("批准操作", "允许指定的待批准请求", "审批编号"),
    "deny": Command("拒绝操作", "拒绝指定的待批准请求", "审批编号"),
    "stop": Command("停止任务", "停止当前会话的任务"),
    "retry": Command("重试任务", "重新执行选定的失败任务", "任务编号"),
    "settings": Command("技能与上下文设置", "查看或配置当前项目的智能体设置"),
    "help": Command("指令帮助", "查看快捷指令和参数"),
    "exit": Command("退出", "关闭当前交互界面"),
}
ALIASES = {
    "": "help", "?": "help", "quit": "exit", "clear": "new", "chats": "history",
    "models": "model", "config": "settings", "approvals-mode": "permissions", "sandbox": "permissions",
    "stats": "usage", "billing": "usage", "stop-agent": "stop", "side": "btw", "sidechat": "btw",
    "branch": "fork", "speed": "fast",
    "侧边": "btw", "用量": "usage", "审查": "review", "归档": "archive", "快速": "fast", "压缩": "compact",
}


def command_name(word):
    word = word.casefold()
    if word in COMMANDS or word in ALIASES:
        return ALIASES.get(word, word)
    return next((name for name, spec in COMMANDS.items() if spec.title.casefold() == word), word)


def matches(query):
    query = query.casefold()
    if not query:
        return list(COMMANDS)
    ranked = []
    for name, spec in COMMANDS.items():
        names = [name, *(alias for alias, target in ALIASES.items() if alias and target == name)]
        if query in names or query == spec.title.casefold():
            rank = 0
        elif any(n.startswith(query) for n in names) or query in spec.title.casefold():
            rank = 1
        elif query in name or query in spec.description.casefold():
            rank = 2
        else:
            continue
        ranked.append((rank, name))
    return [name for _, name in sorted(ranked, key=lambda pair: pair[0])]
