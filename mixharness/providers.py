import json
from dataclasses import dataclass, field
from typing import Literal

from google import genai
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from .attachments import image_parts
from .catalog import MODELS
from .store import pack


class Plan(BaseModel):
    engineering: str | None
    verification: str | None
    report: str | None
    reply: str
    interpretation: str = Field(default="", description="给用户的一两句话：交付什么，有哪些前提还需确认。")
    engineering_mode: Literal["inspect", "develop"] | None = Field(default=None, description="只读源码调研用 inspect，需要编辑或运行项目验证用 develop。")
    verification_mode: Literal["inspect", "run_check"] | None = Field(default=None, description="只读核对资料用 inspect，实际运行验证用 run_check。")
    report_after_gpt: bool = Field(default=False, description="报告的内容依赖 GPT 本轮提供的事实或验证结果时为 true。")
    rerun: list[Literal["engineering", "verification", "report"]] = Field(default_factory=list)


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ReadFile(Arguments):
    path: str
    start_line: int = Field(default=1, ge=1)


class EditFile(Arguments):
    path: str
    expected_sha256: str
    content: str


class RunCheck(Arguments):
    name: str
    folder: str | None = Field(default=None, description="多目录项目中指定要运行验证的目录名称，省略时使用主目录。")


class ReadArtifact(Arguments):
    id: str
    offset: int = Field(default=0, ge=0)


class Verify(Arguments):
    section: str = Field(min_length=1)
    question: str = Field(min_length=1)
    evidence_needed: str = Field(min_length=1)
    method: Literal["inspect", "run_check"] = Field(default="run_check", description="查阅源码、配置或文字资料用 inspect，运行测试或命令用 run_check。")


class Section(Arguments):
    title: str = Field(min_length=1)
    markdown: str = Field(min_length=1)


class WritingTask(Arguments):
    title: str = Field(min_length=1)
    instructions: str = Field(min_length=1)


class GoalAssessment(Arguments):
    status: Literal["complete", "continue", "blocked"]
    evidence: str = Field(min_length=1, description="核对依据，受阻时写明还需要用户提供什么。")
    next_step: str = Field(default="", description="继续时填写可执行的下一步。")


class Clarification(Arguments):
    question: str = Field(min_length=1)


FUNCTIONS = {
    "assess_goal": (GoalAssessment, "根据本轮实际交付与验证记录更新目标进度。"),
    "ask_clarification": (Clarification, "向用户提出制定计划前必须回答的问题。"),
    "list_files": (Arguments, "列出项目文件与已注册的验证命令。"),
    "read_file": (ReadFile, "读取 UTF-8 文件内容与 SHA256 摘要。"),
    "edit_file": (EditFile, "编辑 UTF-8 文件，传入读取时的摘要与完整新内容（新建文件摘要传空字符串）。"),
    "run_check": (RunCheck, "在项目快照中运行验证命令，记录退出码、日志与文件版本。"),
    "read_artifact": (ReadArtifact, "按编号读取本会话的报告、日志或章节，offset 为字符偏移。"),
    "request_verification": (Verify, "把需要实测或核实的问题交给 GPT 验证，拿到结果后继续写作。"),
    "save_section": (Section, "保存已完成的报告章节。"),
    "draft_section": (WritingTask, "并行起草已有足够资料的独立章节。"),
}
TOOLS = {
    "side": ["list_files", "read_file", "read_artifact"],
    "proposal": ["list_files", "read_file", "read_artifact", "ask_clarification"],
    "goal_check": ["list_files", "read_file", "read_artifact", "assess_goal"],
    "engineering": ["list_files", "read_file", "edit_file", "run_check", "read_artifact"],
    "verification": ["list_files", "read_file", "run_check", "read_artifact"],
    "report": ["read_artifact", "save_section", "draft_section", "request_verification"],
    "section": ["read_artifact", "save_section", "request_verification"],
}

PROMPTS = {
    "side": "你在独立的侧边聊天中。将继承的对话当作背景，只回答用户最后提出的问题。需要时只读查看项目，直接用中文回答。",
    "proposal": "你正在计划模式中。只读检查项目，和用户讨论可执行的步骤、验收条件和必要的取舍。"
                "先了解项目再写计划。遇到决定方案的关键信息缺口时用 ask_clarification 提问。用中文给出计划，等待用户选择执行。",
    "goal_check": "核对共享会话里的持续目标与最新补充。检查当前结果、工程记录和相关验证。"
                  "所有条件均有实际依据时调用 assess_goal 标为 complete，并写明依据。"
                  "还有可做的工作时标为 continue 并写明具体下一步。确实需要外部信息或用户决定时标为 blocked 并说明需求。"
                  "每次核对以 assess_goal 结束。",
    "plan": """解析共享会话中的最新用户消息，按当前需求输出变更：
engineering 为 GPT 负责的开发或研究任务，verification 为需要单独执行的验证条件，report 为 Gemini 负责的写作或汇总要求。
未变化的字段返回 null；有修改时返回完整的新要求；明确取消时返回空字符串。
首次提问按需填写对应字段；需要输出说明、报告或解释工程结果时填写 report。
interpretation 面向用户，简述你理解的交付要求和待确认的前提。把推断标为推断，不要预告已经取得的结果。
安排或重跑 engineering 时填写 engineering_mode。解释已有项目、查阅源码和总结用 inspect，修改代码或运行项目验证用 develop。
安排 verification 时用 verification_mode 区分只读核对和实际运行。若需要根据 GPT 调研或验证的结果写报告，report_after_gpt 填 true。
若用户让写作者在缺数据时自行发起验证，将该要求放入 report，verification 保持不变；仅调整读者或篇幅时只更新 report。
纯状态询问在 reply 中简短作答，各任务字段返回 null。
用户要求重跑或已手动改过文件时，将对应任务类型填入 rerun。若本次任务含 followup，安排完成该目标步骤所需的工作，已有要求需要再执行时填写 rerun。reply 可留空。""",
    "engineering": """你负责会话中的工程开发与技术研究。用工具先读文件再修改，运行验证命令检查改动。
研究与推导需写明前提和依据。完成后用简洁自然的中文说明改动内容与验证结果，避免套话和防御性免责声明。""",
    "verification": """你负责针对具体问题做验证。检查项目快照、运行验证命令或核对资料来源，如实给出方法、结果与结论。
如果缺少测试条件或数据，直接说明缺什么，不要堆砌防御性套话。""",
    "report": """你负责面向读者撰写内容，使用自然、通俗易懂的中文和 Markdown，避免机器味和防御性套话。
长文可用 save_section 保存章节，等待验证期间可用 draft_section 并行撰写其他章节。
先给出回答，再用简短段落解释依据。用真正的 Markdown 标题、列表、表格和代码块，避免字符画、装饰分隔线和整篇套代码围栏。
引用工程或验证结论时核对任务状态与实际成果。GPT 失败时说明缺失的依据，分别标明自己查到的文件和仍未核实的推断。
如果开发与研究已经在处理同一个问题，等待其结果。需要额外查阅资料时，request_verification 的 method 填 inspect。
遇到需要实测或核实的问题时，调用 request_verification 写明章节、问题与所需数据，拿到结果后再下结论。
同一问题若已有验证结果或失败记录，直接使用现有结论，不要重复请求。日常对话直接自然回复。""",
    "section": """按当前读者要求完成指定章节，只输出该章节正文，文风自然简洁。需要核实数据时调用 request_verification。""",
}


def instructions(job):
    text = PROMPTS[job["kind"]]
    if job.get("payload", {}).get("read_only"):
        text += "\n本次只读核对已有资料。写明所查文件和支持结论的配置项、函数名或行号。"
    return text


def tool_names(kind, payload=None):
    if kind in ("engineering", "verification") and (payload or {}).get("read_only"):
        return TOOLS["side"]
    return TOOLS[kind]


def declarations(kind, payload=None):
    return [{"type": "function", "name": name, "description": FUNCTIONS[name][1],
             "parameters": FUNCTIONS[name][0].model_json_schema()} for name in tool_names(kind, payload)]


@dataclass
class Turn:
    cursor: str
    text: str
    calls: list = field(default_factory=list)
    sources: list = field(default_factory=list)


async def google_turn(key, job, context, results, preview):
    content = [{"type": "text", "text": context}, *image_parts(job.get("images", []), "gemini")]
    async with genai.Client(api_key=key, vertexai=False).aio as client:
        options = {"model": job["model"], "system_instruction": PROMPTS[job["kind"]], "store": True,
                   "api_version": "v1", "timeout": 180}
        if job["effort"]:
            options["generation_config"] = {"thinking_level": job["effort"]}
        if job["kind"] == "plan":
            response = await client.interactions.create(
                **options, input=content if job.get("images") else context,
                response_format={"type": "text", "mime_type": "application/json", "schema": Plan.model_json_schema()},
            )
            return Plan.model_validate_json(response.output_text)
        inputs = [{"type": "function_result", "name": call["name"], "call_id": call["cid"],
                   "result": [{"type": "text", "text": pack(call["result"])}]} for call in results]
        inputs.append({"type": "user_input", "content": content})
        if job["cursor"]:
            options["previous_interaction_id"] = job["cursor"]
        stream = await client.interactions.create(**options, tools=declarations(job["kind"]), input=inputs, stream=True)
        interaction_id = None
        async with stream:
            async for event in stream:
                if event.event_type == "step.delta" and event.delta.type == "text":
                    preview(event.delta.text)
                elif event.event_type == "interaction.completed":
                    interaction_id = event.interaction.id
                elif event.event_type in ("error", "interaction.failed"):
                    raise RuntimeError(f"Gemini 生成失败：{event}")
        if not interaction_id:
            raise RuntimeError("Gemini 连接在返回完整交互前结束")
        response = await client.interactions.get(interaction_id, api_version="v1")
        if response.status not in ("completed", "requires_action"):
            raise RuntimeError(f"Gemini 返回状态 {response.status}")
        calls = [{"cid": step.id, "name": step.name, "args": step.arguments}
                 for step in response.steps or [] if step.type == "function_call"]
        return Turn(response.id, response.output_text or "", calls)


def openai_result(response):
    calls, sources = [], []
    for item in response.output:
        if item.type == "function_call":
            calls.append({"cid": item.call_id, "name": item.name, "args": json.loads(item.arguments)})
        if item.type == "message":
            for part in item.content:
                if part.type == "output_text":
                    sources.extend({"url": a.url, "title": a.title} for a in part.annotations if a.type == "url_citation")
    return Turn(response.id, response.output_text, calls, sources)


async def openai_turn(key, job, context, results, preview, register_steer):
    inputs = [{"type": "function_call_output", "call_id": c["cid"], "output": pack(c["result"])} for c in results]
    images = image_parts(job.get("images", []), "openai")
    inputs.append({"role": "user", "content": [{"type": "input_text", "text": context}, *images] if images else context})
    functions = [dict(tool, strict=False) for tool in declarations(job["kind"], job.get("payload"))]
    options = {"model": job["model"], "input": inputs, "instructions": instructions(job),
               "store": True, "tools": [*functions, {"type": "web_search"}]}
    if job["effort"]:
        options["reasoning"] = {"effort": job["effort"]}
    if job["cursor"]:
        options["previous_response_id"] = job["cursor"]
    async with AsyncOpenAI(api_key=key, timeout=180, max_retries=0) as client:
        if not job["model"].startswith("gpt-6"):
            response = await client.responses.create(**options)
            if response.status != "completed":
                raise RuntimeError(f"OpenAI 返回状态 {response.status}")
            return openai_result(response)
        async with client.responses.connect() as connection:
            await connection.response.create(**options)
            current = None
            awaiting_successor = set()

            async def steer(message):
                if current:
                    awaiting_successor.add(current)
                    await connection.response.steer(previous_response_id=current, input=message)

            async for event in connection:
                if event.type == "response.created":
                    current = event.response.id
                    register_steer(steer)
                elif event.type == "response.output_text.delta":
                    preview(event.delta)
                elif event.type == "response.completed":
                    response = event.response
                    # 工具请求必须带原调用编号送回，才能继续服务端排队的输入。
                    if response.id not in awaiting_successor or any(x.type == "function_call" for x in response.output):
                        return openai_result(response)
                elif event.type == "response.incomplete":
                    if not (event.response.incomplete_details and event.response.incomplete_details.reason == "steered"):
                        raise RuntimeError(f"OpenAI 生成中断：{event.response.incomplete_details}")
                elif event.type in ("error", "response.failed", "response.steer.failed"):
                    raise RuntimeError(f"OpenAI 请求失败：{event}")
    raise RuntimeError("OpenAI 连接在返回完整结果前结束")
