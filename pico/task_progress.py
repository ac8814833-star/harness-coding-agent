"""Structured task facts derived from requests and observed tool outcomes."""

import re

from .workspace import clip


def normalize_progress(value=None):
    value = value if isinstance(value, dict) else {}
    result = {key: clip(str(value.get(key, "")), 500) for key in ("goal", "next_step", "reported_result")}
    for key in ("constraints", "decisions", "completed", "blockers"):
        items = value.get(key, [])
        result[key] = list(dict.fromkeys(clip(str(item), 180) for item in items if str(item).strip()))[-8:] if isinstance(items, list) else []
    evidence = value.get("evidence", [])
    result["evidence"] = evidence[-12:] if isinstance(evidence, list) else []
    return result


def begin(progress, request, continuing=False):
    result = normalize_progress(progress if continuing else None)
    if not continuing or not result["goal"]:
        result["goal"] = clip(request, 500)
    for line in request.splitlines():
        match = re.match(r"(?:Constraint:|约束：)\s*(.+)", line.strip(), re.I)
        if match:
            result["constraints"].append(match.group(1))
    result["next_step"] = "Inspect the task context and choose the next action."
    return normalize_progress(result)


def record(progress, name, args, metadata, content):
    result = normalize_progress(progress)
    target = args.get("path", "workspace") if isinstance(args, dict) else "workspace"
    entry = f"{name}: {target}"
    status = metadata["tool_status"]
    result["decisions"].append(f"Requested {name}: {target}")
    result["evidence"].append({"call_id": metadata.get("call_id", ""), "name": name, "status": status, "source": str(target), "affected_paths": list(metadata.get("affected_paths", []))[:8]})
    if status == "ok":
        result["completed"].append(entry)
        result["blockers"] = [item for item in result["blockers"] if not item.startswith(name + ":")]
        result["next_step"] = f"Use the observed result of {name} to continue."
    else:
        result["blockers"].append(f"{name}: {metadata.get('tool_error_code', status)}; {clip(content, 90)}")
        result["next_step"] = f"Resolve the {name} failure before retrying."
    return normalize_progress(result)


def finish(progress, answer):
    result = normalize_progress(progress)
    result["reported_result"] = clip(answer, 500)
    result["next_step"] = "No next step recorded."
    for line in answer.splitlines():
        match = re.match(r"(?:Decision:|决策：)\s*(.+)", line.strip(), re.I)
        if match:
            result["decisions"].append("Model reported: " + match.group(1))
        match = re.match(r"(?:Next step:|下一步：)\s*(.+)", line.strip(), re.I)
        if match:
            result["next_step"] = match.group(1)
    return normalize_progress(result)
