import json
import os
from functools import lru_cache
from pathlib import Path

import frontmatter
import tiktoken
from pydantic import BaseModel, ConfigDict, Field, field_validator


PERMISSIONS = {"ask": "请求批准", "auto": "帮我批准", "full": "完全访问"}
SPEEDS = {"standard": "标准", "fast": "Fast", "ultrafast": "Ultrafast"}


class AgentSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    context_window: int = Field(default=0, ge=0, le=2000000)
    skill_dirs: list[str] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    project_skills: dict[str, list[str]] = Field(default_factory=dict)

    @field_validator("context_window")
    @classmethod
    def valid_window(cls, value):
        if 0 < value < 4096:
            raise ValueError("上下文窗口至少需要 4096 token（填 0 为自动）")
        return value


def skill_catalog(settings, folders):
    home = Path.home()
    roots = [Path(folder) / ".agents/skills" for folder in folders]
    roots.extend(Path(p).expanduser() for p in settings.skill_dirs)
    codex = Path(os.environ.get("CODEX_HOME", home / ".codex")) / "skills"
    roots.extend([home / ".agents/skills", codex, codex / ".system", home / ".gemini/config/skills",
                  home / ".gemini/antigravity-cli/skills", home / ".gemini/antigravity/skills"])
    found = {}
    for root in dict.fromkeys(roots):
        paths = [root / "SKILL.md"] if (root / "SKILL.md").is_file() else sorted(root.glob("*/SKILL.md"))
        for path in paths:
            manifest = frontmatter.load(str(path))
            name = str(manifest.get("name") or path.parent.name)
            if name not in found:
                found[name] = {"name": name, "path": str(path.resolve()), "description": str(manifest.get("description", ""))}
    return found


def selected_skills(settings, folders):
    if not settings.skills:
        return []
    catalog = skill_catalog(settings, folders)
    result = []
    for name in settings.skills:
        if name not in catalog:
            raise ValueError(f"找不到技能 {name}，请检查技能目录设置")
        path = Path(catalog[name]["path"])
        result.append({"名称": name, "目录": str(path.parent), "使用说明": frontmatter.load(str(path)).content})
    return result


@lru_cache(maxsize=1)
def tokenizer():
    os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(Path(__file__).with_name("resources")))
    return tiktoken.get_encoding("o200k_base")


def tokens(text):
    return len(tokenizer().encode_ordinary(text))


def encode_context(context):
    return json.dumps(context, ensure_ascii=False, separators=(",", ":"))


def result_excerpt(result, limit):
    packed = encode_context(result)
    if tokens(packed) <= limit:
        return result
    brief = {k: result[k] for k in ("artifact_id", "source", "sha256", "exit_code", "path", "folder",
                                     "total_chars", "total_lines", "evidence_status") if k in result}
    if result.get("evidence"):
        brief["evidence"] = [{"id": a["id"], "name": a["name"], "exit_code": a["meta"].get("exit_code")}
                             for a in result["evidence"][:6]]
    content = result.get("error") or result.get("text") or result.get("output") or packed
    # 保留输出的开头和结尾，便于同时看到命令说明和最后的退出信息。
    low, high = 0, min(len(content), limit * 5)
    while low < high:
        count = (low + high + 1) // 2
        head = count // 2
        excerpt = content[:head] + " … " + content[len(content) - count + head:]
        if tokens(encode_context(dict(brief, 节选=excerpt))) <= limit:
            low = count
        else:
            high = count - 1
    head = low // 2
    brief["节选"] = content[:head] + " … " + content[len(content) - low + head:]
    if tokens(encode_context(brief)) > limit:
        raise ValueError("上下文窗口过小，无法容纳工具结果，请调大窗口")
    return brief


def compact_context(context, limit):
    packed = encode_context(context)
    if not limit or tokens(packed) <= limit:
        return packed
    # 固定保留当前需求、工具要求和启用的技能，按时间优先保留最近的历史与成果。
    recent = {key: context[key] for key in ("共享记录", "本次任务的工具记录", "可读取的成果", "任务") if key in context}
    kept = {**context, **dict.fromkeys(recent, []), "历史说明": "早期消息已折叠，可按编号读取历史成果。"}
    if tokens(encode_context(kept)) > limit:
        raise ValueError("当前需求、草稿或技能说明超出上下文窗口，请调大窗口或减少技能")
    if "本次任务的工具记录" in recent:
        room = max(128, (limit - tokens(encode_context(kept))) // 2)
        recent["本次任务的工具记录"] = [
            {"name": c["name"], "args": {k: v for k, v in c["args"].items() if k not in ("content", "markdown")},
             "result": result_excerpt(c["result"], room) if c["result"] is not None else None}
            for c in recent["本次任务的工具记录"]]
    for key in ("本次任务的工具记录", "共享记录", "可读取的成果", "任务"):
        if key not in recent:
            continue
        items = recent[key]
        low, high = 0, len(items)
        while low < high:
            count = (low + high + 1) // 2
            kept[key] = items[:count] if key == "可读取的成果" else items[-count:]
            if tokens(encode_context(kept)) <= limit:
                low = count
            else:
                high = count - 1
        kept[key] = items[:low] if key == "可读取的成果" else items[len(items) - low:]
    return encode_context(kept)


def shared_input(context, calls, limit):
    if not limit or not calls:
        return calls, compact_context(context, limit)
    available = limit // (2 * len(calls))
    results = [dict(call, result=result_excerpt(call["result"], available)) for call in calls]
    room = limit - sum(tokens(encode_context(call["result"])) for call in results)
    if room < 1:
        raise ValueError("工具结果超过上下文窗口，请增大窗口")
    return results, compact_context(context, room)


def available_speeds(group):
    return (group or {}).get("speeds", {})


def codex_speeds(account, model):
    result = {}
    tiers = model.get("serviceTiers")
    if tiers is None:
        tiers = [{"id": t} for t in model.get("additionalSpeedTiers", [])]
    for tier in tiers:
        name = (tier.get("name") or tier["id"]).casefold()
        if name == "ultrafast" or tier["id"] == "ultrafast":
            if account.get("type") == "chatgpt" and account.get("planType") in ("pro", "promax"):
                result["ultrafast"] = tier["id"]
        elif name == "fast" or tier["id"] in ("fast", "priority"):
            result["fast"] = tier["id"]
    return result
