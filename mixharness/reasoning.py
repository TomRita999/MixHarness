import re


LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
LEVEL_NAMES = {"none": "关闭", "minimal": "Minimal", "low": "Low", "medium": "Medium", "high": "High",
               "xhigh": "XHigh", "max": "Max", "ultra": "Ultra"}
STANDARD = ("low", "medium", "high")
EXTENDED = (*STANDARD, "xhigh")
GEMINI_LEVELS = {
    "gemini-4-hydrogen": EXTENDED, "gemini-4-neon": EXTENDED,
    "gemini-4-argon": STANDARD, "gemini-4-carbon": STANDARD, "gemini-3.8-flash": STANDARD,
    "gemini-3.6-flash": ("minimal", *STANDARD), "gemini-3.5-flash": ("minimal", *STANDARD),
    "gemini-3.5-flash-lite": ("minimal", *STANDARD), "gemini-3.1-pro-preview": STANDARD,
    "gemini-3.1-flash-lite": ("minimal", *STANDARD), "gemini-3.1-flash-lite-image": ("minimal", "high"),
    "gemini-3-flash-preview": ("minimal", *STANDARD), "gemini-3-pro-preview": ("low", "high"),
    "gemini-2.5-pro": STANDARD, "gemini-2.5-flash": STANDARD, "gemini-2.5-flash-lite": STANDARD,
}


def api_levels(provider, model):
    model = re.sub(r"-\d{4}-\d{2}-\d{2}$", "", model)
    if provider == "gemini":
        return GEMINI_LEVELS.get(model, ())
    if model in ("gpt-6.2-sol", "gpt-6.2-astra", "gpt-6.1-sol", "gpt-6-astra"):
        return (*EXTENDED, "max")
    if model in ("gpt-6.1-terra", "gpt-6-sol", "gpt-6-luna", "gpt-5.6", "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna"):
        return ("none", *EXTENDED, "max")
    if model in ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini", "gpt-5.4-nano", "gpt-5.2"):
        return ("none", *EXTENDED)
    if model in ("gpt-5.5-pro", "gpt-5.4-pro", "gpt-5.2-pro"):
        return ("medium", "high", "xhigh")
    if model in ("gpt-5.3-codex", "gpt-5.2-codex"):
        return EXTENDED
    if model == "gpt-5.1":
        return ("none", *STANDARD)
    if model in ("gpt-5", "gpt-5-mini", "gpt-5-nano"):
        return ("minimal", *STANDARD)
    if model == "gpt-5-pro":
        return ("high",)
    if model in ("o3", "o4-mini", "o3-mini"):
        return STANDARD
    return ()


def antigravity_groups(labels):
    groups = {}
    for model, label in labels.items():
        base, separator, level = model.rpartition("-")
        title, _, suffix = label.rpartition(" (")
        if separator and level in LEVELS and suffix.casefold() == level + ")":
            group = groups.setdefault(base, {"id": base, "name": title, "choices": {}, "default": ""})
            group["choices"][level] = model
            if not group["default"] or level == "medium":
                group["default"] = level
        else:
            groups[model] = {"id": model, "name": label, "choices": {"": model}, "default": ""}
    for group in groups.values():
        group["choices"] = dict(sorted(group["choices"].items(), key=lambda pair: LEVELS.index(pair[0]) if pair[0] else -1))
    return list(groups.values())


def model_group(model, name, levels, default=""):
    return {"id": model, "name": name, "choices": {level: model for level in levels}, "default": default}


def api_group(provider, model):
    levels = api_levels(provider, model)
    name = model
    if provider == "gemini" and levels:
        name = "Gemini " + model.removeprefix("gemini-").replace("-", " ").title()
    return model_group(model, name, levels)


def group_for(groups, model):
    return next((g for g in groups if model == g["id"] or model in g["choices"].values()), None)


def model_available(group, daybreak):
    programs = (group or {}).get("access_programs")
    if not programs:
        return not daybreak
    return ("daybreakBlue" if daybreak else "standard") in programs


def select_daybreak_model(groups, current, enabled):
    group = group_for(groups, current)
    if group and model_available(group, enabled):
        return current
    for candidate in groups:
        if model_available(candidate, enabled):
            return candidate["id"]
    if not enabled and not groups:
        return current
    raise ValueError("当前 Codex 账户没有可用的 Daybreak Blue 模型" if enabled else "当前账户没有可用的标准模型")


def resolve_choice(group, model, effort):
    choices = group["choices"]
    if effort and effort not in choices:
        raise ValueError(group["name"] + " 不支持思考强度 " + effort)
    if model != group["id"] or any(value != group["id"] for value in choices.values()):
        selected = effort or next((k for k, v in choices.items() if v == model), group["default"])
        return choices[selected], selected
    return model, effort or ""
