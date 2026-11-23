ASSIGNMENTS = {"engineering": "GPT · 开发与研究", "verification": "GPT · 验证", "report": "Gemini · 报告"}
SANDBOXES = {"readOnly": "只读", "workspaceWrite": "可写工作区", "dangerFullAccess": "完全访问",
             "externalSandbox": "外部沙箱"}


def plan_details(meta, messages):
    lines = []
    for seq in meta.get("source_messages", ()):
        if seq in messages:
            lines.append(("用户原话", messages[seq]))
    if meta.get("followup"):
        lines.append(("目标的下一步", meta["followup"]))
    if meta.get("interpretation"):
        lines.append(("Gemini 的理解", meta["interpretation"]))
    brief = meta.get("brief", {})
    if "assigned" not in meta:
        return lines + [(owner, brief[kind]) for kind, owner in ASSIGNMENTS.items() if brief.get(kind)]
    for task in meta["assigned"]:
        kind = task["kind"]
        title = ASSIGNMENTS[kind]
        if task.get("read_only"):
            title = "GPT · 只读调研" if kind == "engineering" else "GPT · 只读核对"
        elif task.get("after_gpt"):
            title += "（等 GPT 返回后撰写）"
        lines.append((title + " · " + task["model"], brief[kind]))
    for kind in meta["changed"]:
        if not brief[kind]:
            lines.append((ASSIGNMENTS[kind], "已取消"))
    if not meta["assigned"] and not meta["changed"]:
        lines.append(("任务分配", "本轮沿用已有分工。"))
    return lines


def report_inputs(tasks):
    reports = [t["basis"] for t in tasks if t["kind"] == "report" and t["status"] not in ("failed", "obsolete", "stopped")]
    engineering = {basis["engineering"] for basis in reports}
    verification = {(basis["engineering"], basis["verification"]) for basis in reports}
    inputs = {}
    for task in tasks:
        if task.get("direct"):
            continue
        kind, basis = task["kind"], task["basis"]
        if ((kind == "engineering" and basis["engineering"] in engineering) or
                (kind == "verification" and (basis["engineering"], basis["verification"]) in verification)):
            inputs[task["id"]] = kind
    return inputs


def report_input_kind(meta, inputs):
    if not meta.get("needs_input"):
        return inputs.get(meta.get("job")) or meta.get("report_input")


def describe_events(events, tasks, approvals):
    messages, outcomes, failures, reads = {}, {}, {}, {}
    decisions = {a["id"]: a for a in approvals}
    failed = {t["id"] for t in tasks if t["status"] == "failed"}
    inputs = report_inputs(tasks)
    for task in tasks:
        if task["id"] in inputs:
            task["intermediate"] = True
    for event in events:
        kind, meta = event["kind"], event["meta"]
        jid = meta.get("job")
        if kind == "message" and event["actor"] == "openai" and report_input_kind(meta, inputs):
            event["intermediate"] = True
        if meta.get("files_read"):
            reads.setdefault(jid, {}).update(dict.fromkeys(meta["files_read"]))
        if event["actor"] == "user":
            body = event["body"].strip() or "、".join(a["name"] for a in event.get("attachments", ()))
            messages[event["seq"]] = body[:500] + ("…" if len(body) > 500 else "")
        elif kind == "requirements":
            event["trace"] = plan_details(meta, messages)
        elif kind == "progress" and meta.get("approval") in decisions:
            action = decisions[meta["approval"]]
            event["approval_label"] = action["source"] + (" · 操作已批准" if action["decision"] == "allow" else " · 操作已拒绝")
            if not meta.get("source"):
                event["body"] += " · " + action["source"] + " · " + action["title"]
        if kind == "failure":
            failures[jid] = event
            diagnostic = meta.get("diagnostic") or {}
            if "无法识别本地客户端权限声明" in event["body"] and event["actor"] == "openai":
                event["notice"] = "Codex 返回权限声明错误，这次 GPT 调用没有成功完成。"
            if diagnostic:
                event["trace"] = [("Codex 返回阶段", diagnostic["stage"]),
                                  ("线程权限", SANDBOXES.get(diagnostic["sandbox"], diagnostic["sandbox"]))]
                if diagnostic.get("code"):
                    event["trace"].append(("错误类型", diagnostic["code"]))
        scope = meta.get("kind")
        if jid in reads and (kind == "requirements" or kind == "message" and event["actor"] != "user"):
            event.setdefault("trace", []).append(("本次任务实际读取", "、".join(reads[jid])))
        if scope in ("engineering", "verification") and kind in ("message", "failure"):
            outcomes[jid] = (scope, meta.get("basis", {}), kind)
        if scope == "report" and kind == "message":
            basis = meta.get("basis", {})
            statuses = [(scope, result) for scope, versions, result in outcomes.values()
                        if all(basis.get(k) == v for k, v in versions.items())]
            if statuses:
                event.setdefault("trace", []).append(("GPT 任务记录", "、".join(dict.fromkeys(
                    ("开发与研究" if scope == "engineering" else "验证") +
                    ("已返回结果" if result == "message" else "未成功完成") for scope, result in statuses))))
    for jid in failed:
        if jid in failures:
            failures[jid]["retryable"] = True
