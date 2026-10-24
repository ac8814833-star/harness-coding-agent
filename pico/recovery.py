"""File freshness gates and reconciliation of interrupted local tool actions."""

import hashlib
import os

from .features.memory import file_freshness
from .workspace import clip, now


def recovery_state(agent):
    state = agent.session.setdefault("recovery", {})
    state.setdefault("stale_paths", [])
    state.setdefault("observed_files", {})
    return state


def observe_file(agent, path):
    relative = agent.path(path).relative_to(agent.root).as_posix()
    state = recovery_state(agent)
    state["observed_files"][relative] = file_freshness(relative, agent.root)
    state["stale_paths"] = [item for item in state["stale_paths"] if item != relative]


def write_guard(agent, name, args):
    if name not in {"write_file", "patch_file", "run_shell"}:
        return ""
    if any(item["status"] in {"executing", "unknown"} for item in agent.session.get("actions", {}).values()):
        return "An interrupted action has an unknown outcome; inspect /actions and acknowledge it before another write."
    if name == "run_shell":
        if recovery_state(agent)["stale_paths"]:
            return "Re-read stale key files before running a shell command."
        return ""
    path = agent.path(args["path"]).relative_to(agent.root).as_posix()
    state = recovery_state(agent)
    current = file_freshness(path, agent.root)
    expected = args.get("expected_hash")
    observed = state["observed_files"]
    if expected is not None and expected != current:
        return f"File version mismatch for {path}; re-read the file before modifying it."
    if path in observed and observed[path] != current:
        if path not in state["stale_paths"]:
            state["stale_paths"].append(path)
    if path in state["stale_paths"]:
        return f"Stale file {path}; re-read it before modifying it."
    return ""


def begin_action(agent, name, args, before):
    expected = {}
    if name in {"write_file", "patch_file"}:
        path = agent.path(args["path"])
        content = args.get("content")
        if name == "patch_file":
            content = path.read_text(encoding="utf-8").replace(args["old_text"], args["new_text"], 1)
        # Path.write_text translates newlines on Windows.
        digest = hashlib.sha256(content.replace("\n", os.linesep).encode("utf-8")).hexdigest()
        expected[path.relative_to(agent.root).as_posix()] = digest
    record = {
        "action_id": agent.current_call_id, "run_id": agent.current_task_state.run_id if agent.current_task_state else "",
        "name": name, "args": dict(args), "status": "executing", "started_at": now(),
        "before": before, "expected_after": expected, "binding_root": str(agent.root),
        "approval_id": agent.last_approval_id,
    }
    agent.session.setdefault("actions", {})[agent.current_call_id] = record
    agent.session_path = agent.session_store.save(agent.session)
    agent.trace_tool_event("action_started", {"action_id": record["action_id"], "name": name})


def finish_action(agent, result):
    record = agent.session.get("actions", {}).get(agent.current_call_id)
    if record is None:
        return
    record.update(status={"ok": "succeeded", "partial_success": "partial_success"}.get(result.metadata["tool_status"], "failed"), finished_at=now(), result=agent.redact_text(clip(result.content, 500)))
    record["affected_paths"] = list(result.metadata.get("affected_paths", []))
    agent.session_path = agent.session_store.save(agent.session)


def reconcile_actions(agent):
    """Confirm file postconditions; never replay an uncertain shell action."""
    for record in agent.session.get("actions", {}).values():
        if record["status"] not in {"executing", "unknown"}:
            continue
        expected = record.get("expected_after", {})
        if record.get("binding_root") != str(agent.root):
            record["status"] = "unknown"
        elif expected and all(file_freshness(path, agent.root) == version for path, version in expected.items()):
            record["status"] = "reconciled"
            record["result"] = "Recovered completed file change; its expected content is present."
            approval = agent.session.get("approvals", {}).get(record.get("approval_id"))
            if approval and approval["status"] == "approved":
                approval.update(status="executed", finished_at=now())
        else:
            record["status"] = "unknown"
        record["reconciled_at"] = now()


def recovered_action(agent, name, args):
    for record in reversed(list(agent.session.get("actions", {}).values())):
        if record["status"] != "reconciled" or record["name"] != name or record["args"] != args:
            continue
        if record.get("binding_root") == str(agent.root) and all(file_freshness(path, agent.root) == version for path, version in record["expected_after"].items()):
            return record
    return None
