from typing import NamedTuple


class Command(NamedTuple):
    title: str
    description: str
    arguments: str = ""

    def usage(self):
        return "　".join(filter(None, (self.arguments, self.description)))


# The order here is also the order shown when the composer contains just "/".
COMMANDS = {
    "mcp": Command("MCP 服务", "查看已配置的 MCP 服务", "[codex|antigravity]"),
    "review": Command("代码审查", "让 GPT 审查当前改动", "[关注点]"),
    "usage": Command("用量", "查看额度与重置时间", "[codex|antigravity|openai|gemini]"),
    "btw": Command("侧边聊天", "带上下文单独问 GPT", "[问题]"),
    "share": Command("分享聊天", "分享对话快照"),
    "fork": Command("创建聊天分支", "在当前目录或新工作树继续", "[名称 | --worktree 目录]"),
    "init": Command("初始化项目说明", "生成或更新 AGENTS.md"),
    "compact": Command("压缩上下文", "精简模型读取的历史"),
    "feedback": Command("反馈", "反馈问题或建议", "[反馈内容]"),
    "archive": Command("归档聊天", "归档当前会话"),
    "fast": Command("快速模式", "切换 Codex 加速档位", "[off|fast|ultrafast]"),
    "daybreak": Command("Daybreak", "切换 Daybreak Blue 模型", "[on|off]"),
    "plan": Command("计划模式", "制定或修订只读计划", "[要求]"),
    "goal": Command("目标模式", "设定并跟进持续目标", "[目标|status|pause|resume|clear]"),
    "implement": Command("执行计划", "开始实施当前计划"),
    "mode": Command("切换模式", "默认、计划或目标", "[default|plan|goal]"),
    "new": Command("新建会话", "在当前项目开始新对话"),
    "resume": Command("继续会话", "打开已有会话", "[会话编号]"),
    "history": Command("搜索会话", "按关键词查找会话", "[关键词]"),
    "model": Command("选择模型", "切换 GPT 或 Gemini 模型", "[gemini|openai] [模型 ID]"),
    "effort": Command("思考强度", "调整思考档位", "gemini|openai [档位|default]"),
    "permissions": Command("操作权限", "设置是否需要批准", "[ask|auto|full]"),
    "backend": Command("执行方式", "选择本机客户端或官方 API", "[clients|api]"),
    "diff": Command("查看改动", "查看 Git 改动"),
    "skills": Command("技能", "管理技能与技能目录", "[on 名称|off 名称|add 目录|remove 目录]"),
    "tools": Command("可用工具", "查看工具与项目验证命令"),
    "context": Command("上下文窗口", "查看或设置窗口大小", "[auto|token 数]"),
    "status": Command("会话状态", "模型、权限与任务"),
    "rename": Command("重命名", "修改会话名称", "名称"),
    "export": Command("导出聊天", "保存为 Markdown", "[文件路径]"),
    "projects": Command("项目列表", "查看本地项目"),
    "project": Command("切换项目", "打开项目或文件夹", "项目编号或目录"),
    "add-dir": Command("添加目录", "加入当前项目", "文件夹"),
    "approvals": Command("待批准操作", "查看待批准请求"),
    "approve": Command("批准操作", "允许请求", "审批编号"),
    "deny": Command("拒绝操作", "拒绝请求", "审批编号"),
    "stop": Command("停止任务", "中断任务并保留进度"),
    "continue": Command("继续任务", "从停止处继续，可以同时补充要求", "[补充要求]"),
    "steer": Command("引导任务", "将补充要求交给当前任务", "补充要求"),
    "revise": Command("修改需求", "让 Gemini 按新需求重新分配工作", "新需求"),
    "retry": Command("重试任务", "重新执行失败任务", "任务编号"),
    "settings": Command("设置", "技能与上下文"),
    "help": Command("指令帮助", "查看全部指令"),
    "exit": Command("退出", "退出命令行"),
}
ALIASES = {
    "": "help", "?": "help", "quit": "exit", "clear": "new", "chats": "history",
    "models": "model", "config": "settings", "approvals-mode": "permissions", "sandbox": "permissions",
    "stats": "usage", "billing": "usage", "stop-agent": "stop", "side": "btw", "sidechat": "btw",
    "branch": "fork", "speed": "fast",
    "继续": "continue", "引导": "steer",
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
