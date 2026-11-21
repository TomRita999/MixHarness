import json
from dataclasses import dataclass, field
from typing import Literal

from google import genai
from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from .catalog import MODELS
from .store import pack


class Plan(BaseModel):
    engineering: str | None
    verification: str | None
    report: str | None
    reply: str
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
    folder: str | None = Field(default=None, description="多目录项目中要运行验证的来源文件夹名称。省略时使用主目录。")


class ReadArtifact(Arguments):
    id: str
    offset: int = Field(default=0, ge=0)


class Verify(Arguments):
    section: str = Field(min_length=1)
    question: str = Field(min_length=1)
    evidence_needed: str = Field(min_length=1)


class Section(Arguments):
    title: str = Field(min_length=1)
    markdown: str = Field(min_length=1)


class WritingTask(Arguments):
    title: str = Field(min_length=1)
    instructions: str = Field(min_length=1)


FUNCTIONS = {
    "list_files": (Arguments, "列出当前项目文件和已注册的验证命令。"),
    "read_file": (ReadFile, "读取 UTF-8 项目文件，返回内容和 SHA256。"),
    "edit_file": (EditFile, "编辑 UTF-8 文件，传入读取时的摘要和完整新内容。新文件的摘要使用空字符串。"),
    "run_check": (RunCheck, "在项目快照上运行已注册的实际命令，记录退出码、日志和源文件版本。"),
    "read_artifact": (ReadArtifact, "按编号读取本会话的报告、证据或章节。offset 为字符偏移。"),
    "request_verification": (Verify, "把缺少证据的具体问题交给 GPT 验证，完成后从此工具调用继续写作。"),
    "save_section": (Section, "保存已写好的报告章节，需求修改或验证等待期间可继续使用。"),
    "draft_section": (WritingTask, "安排独立撰写一个有充足材料的章节，在其他章节等待验证期间并行进行。"),
}
TOOLS = {
    "engineering": ["list_files", "read_file", "edit_file", "run_check", "read_artifact"],
    "verification": ["list_files", "read_file", "run_check", "read_artifact"],
    "report": ["read_artifact", "save_section", "draft_section", "request_verification"],
    "section": ["read_artifact", "save_section", "request_verification"],
}

PROMPTS = {
    "plan": """你负责解析共享会话中的新用户消息。根据当前需求逐项输出变更。
engineering 是 GPT 的开发或理论研究任务，verification 是需要专门检验的条件，report 是 Gemini 的交付要求。
各字段没有变化时严格返回 null；需要修改时返回该字段完整的新要求；明确取消时返回空字符串。
首次任务按实际需要填写字段。需要交付文字或向用户解释工程结果时填写 report。
如果用户要求写作者在发现依据缺失时再申请验证，将这个要求写入 report，保留 verification 原值。
用户只改读者时仅更新 report。纯状态询问用 reply 简短回答，各字段返回 null。
用户明确要求在相同条件下重新执行，或已经手动修改文件时，在 rerun 中填写需要重新执行的任务类型。
reply 可以为空。依据提供的真实任务与结果回答，不声称未执行的操作已经完成。""",
    "engineering": """你是共享会话的工程和研究负责人。使用工具读取真实项目、编辑文件、运行验证。
读取当前文件后再编辑，尊重用户在工作期间的手动修改。研究任务交付可复查的假设、推导及来源。
最终用中文交代实际改变、验证结果、未决问题及证据编号。其他模型的输出是协作资料。""",
    "verification": """你负责独立验证。针对指定问题检查项目快照、执行已注册的实际命令或查阅原始来源。
区分成立、不成立和证据不足。给出方法、观察值、文件或来源链接及适用条件。
没有可用测量时明确指出，不把编译成功、零个测试或未经验证的推测当作实测结果。""",
    "report": """你是面向用户的中文作者。结合最新要求和已有证据完成所需文字，使用 Markdown。
写长文时用 save_section 保存完成的章节。等待验证时，可用 draft_section 安排有依据的独立章节。
技术事实或测量缺少证据时调用 request_verification，
写明章节、具体问题与所需证据。在收到验证结论后继续写作并引用依据。
如已有同一问题的验证或执行失败记录，先使用已有结果及其限制，避免重复请求。
使用当前项目版本的证据。证据不足时说明缺少哪项验证，不能声称结论已获证实。
若收到的是普通对话则直接回答。最终输出完整可阅读的成稿。""",
    "section": """你负责完成本次任务指定的一个章节。读取已有成果、按当前读者要求写作。
只输出该章节正文。需要核实事实时可以请求验证，完成后给出依据。""",
}


def declarations(kind):
    return [{"type": "function", "name": name, "description": FUNCTIONS[name][1],
             "parameters": FUNCTIONS[name][0].model_json_schema()} for name in TOOLS[kind]]


@dataclass
class Turn:
    cursor: str
    text: str
    calls: list = field(default_factory=list)
    sources: list = field(default_factory=list)


async def google_turn(key, job, context, results, preview):
    async with genai.Client(api_key=key, vertexai=False).aio as client:
        options = {"model": job["model"], "system_instruction": PROMPTS[job["kind"]], "store": True,
                   "api_version": "v1", "timeout": 180}
        if job["effort"]:
            options["generation_config"] = {"thinking_level": job["effort"]}
        if job["kind"] == "plan":
            response = await client.interactions.create(
                **options, input=context,
                response_format={"type": "text", "mime_type": "application/json", "schema": Plan.model_json_schema()},
            )
            return Plan.model_validate_json(response.output_text)
        inputs = [{"type": "function_result", "name": call["name"], "call_id": call["cid"],
                   "result": [{"type": "text", "text": pack(call["result"])}]} for call in results]
        inputs.append({"type": "user_input", "content": [{"type": "text", "text": context}]})
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
    inputs.append({"role": "user", "content": context})
    functions = [dict(tool, strict=False) for tool in declarations(job["kind"])]
    options = {"model": job["model"], "input": inputs, "instructions": PROMPTS[job["kind"]],
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
