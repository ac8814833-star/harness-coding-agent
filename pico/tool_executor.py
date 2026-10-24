"""Structured tool execution for the agent runtime."""

from dataclasses import dataclass
import re
import uuid
from .approvals import ApprovalManager
from . import recovery as recoverylib

from .workspace import clip


@dataclass(frozen=True)
class ToolExecutionResult:
    content: str
    metadata: dict


def _metadata(
    tool_status,
    tool_error_code="",
    security_event_type="",
    risk_level="low",
    read_only=True,
    affected_paths=None,
    workspace_changed=False,
    workspace_fingerprint="",
    diff_summary=None,
):
    result = {
        "tool_status": tool_status,
        "tool_error_code": tool_error_code,
        "security_event_type": security_event_type,
        "risk_level": risk_level,
        "read_only": read_only,
        "affected_paths": list(affected_paths or []),
        "workspace_changed": bool(workspace_changed),
        "diff_summary": list(diff_summary or []),
    }
    if workspace_fingerprint:
        result["workspace_fingerprint"] = workspace_fingerprint
    return result


class ToolExecutor:
    def __init__(self, agent):
        self.agent = agent

    def execute(self, name, args):
        agent = self.agent
        previous_call_id = agent.current_call_id
        agent.current_call_id = "call_" + uuid.uuid4().hex[:12]
        call_id = agent.current_call_id
        state = agent.current_task_state
        agent.last_approval_id = getattr(agent, "_approval_to_resume", "")
        if state is not None:
            state.last_call_id = call_id
        try:
            agent.set_phase("validating_tool")
            agent.trace_tool_event("tool_requested", {"name": name, "args": args})
            fingerprint = agent.tool_input_fingerprint(name, args)
            result = self._execute(name, args)
            recoverylib.finish_action(agent, result)
            ApprovalManager(agent).finish(result)
            metadata = {**result.metadata, "call_id": call_id, "input_fingerprint": fingerprint, "approval_id": agent.last_approval_id}
            if name == "read_file" and metadata["tool_status"] == "ok":
                match = re.search(r"(?m)^# sha256: ([a-f0-9]{64})$", result.content)
                if match:
                    metadata["file_version"] = match.group(1)
            agent.record_tool_progress(name, args, metadata, result.content)
            agent.record_process_note_for_tool(name, metadata)
            agent.trace_tool_event("tool_finished", {"name": name, **metadata})
            return ToolExecutionResult(result.content, metadata)
        finally:
            agent.current_call_id = previous_call_id

    def _execute(self, name, args):
        agent = self.agent
        if agent.allowed_tools is not None and name not in agent.allowed_tools:
            return ToolExecutionResult(
                content=f"error: tool '{name}' is not allowed in this run",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="tool_not_allowed",
                    risk_level="high",
                    read_only=False,
                ),
            )

        tool = agent.tools.get(name)
        if tool is None:
            return ToolExecutionResult(
                content=f"error: unknown tool '{name}'",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="unknown_tool",
                    risk_level="high",
                    read_only=False,
                ),
            )

        recovered = recoverylib.recovered_action(agent, name, args)
        if recovered:
            return ToolExecutionResult(recovered["result"], {**_metadata("ok", read_only=True), "recovered_action_id": recovered["action_id"]})

        try:
            agent.validate_tool(name, args)
        except Exception as exc:
            example = agent.tool_example(name)
            message = f"error: invalid arguments for {name}: {exc}"
            if example:
                message += f"\nexample: {example}"
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            return ToolExecutionResult(
                content=message,
                metadata=_metadata(
                    "rejected",
                    tool_error_code="invalid_arguments",
                    security_event_type=security_event_type,
                    risk_level="high" if tool["risky"] else "low",
                    read_only=not tool["risky"],
                ),
            )

        guard_error = recoverylib.write_guard(agent, name, args)
        if guard_error:
            agent.session_path = agent.session_store.save(agent.session)
            return ToolExecutionResult(f"error: {guard_error}", _metadata("rejected", tool_error_code="recovery_guard", risk_level="high", read_only=False))

        if agent.repeated_tool_call(name, args):
            return ToolExecutionResult(
                content=f"error: repeated identical tool call for {name}; choose a different tool or return a final answer",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="repeated_identical_call",
                    risk_level="high" if tool["risky"] else "low",
                    read_only=not tool["risky"],
                ),
            )

        if tool["risky"]:
            agent.set_phase("waiting_approval")
        if tool["risky"] and not agent.approve(name, args):
            return ToolExecutionResult(
                content=f"error: approval denied for {name}",
                metadata=_metadata(
                    "rejected",
                    tool_error_code="approval_denied",
                    security_event_type="read_only_block" if agent.read_only else "approval_denied",
                    risk_level="high",
                    read_only=False,
                ),
            )

        agent.set_phase("executing_tool")
        guard_error = recoverylib.write_guard(agent, name, args)
        if guard_error:
            agent.session_path = agent.session_store.save(agent.session)
            return ToolExecutionResult(f"error: {guard_error}", _metadata("rejected", tool_error_code="recovery_guard", risk_level="high", read_only=False))
        # A human may have waited while a file changed; validate again at execution.
        if tool["risky"]:
            try:
                agent.validate_tool(name, args)
            except Exception as exc:
                return ToolExecutionResult(f"error: invalid arguments for {name}: {exc}", _metadata("rejected", tool_error_code="invalid_arguments", risk_level="high", read_only=False))
        before_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else {}
        after_snapshot = before_snapshot
        if tool["risky"]:
            recoverylib.begin_action(agent, name, args, before_snapshot)
        try:
            content = clip(tool["run"](args))
            after_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            tool_status = "ok"
            tool_error_code = ""
            if name == "run_shell":
                match = re.search(r"exit_code:\s*(-?\d+)", content)
                exit_code = int(match.group(1)) if match else 0
                if exit_code != 0 and workspace_changed:
                    tool_status = "partial_success"
                    tool_error_code = "tool_partial_success"
                elif exit_code != 0:
                    tool_status = "error"
                    tool_error_code = "tool_failed"
            agent.update_memory_after_tool(name, args, content)
            metadata = _metadata(
                tool_status,
                tool_error_code=tool_error_code,
                risk_level="high" if tool["risky"] else "low",
                read_only=not tool["risky"],
                affected_paths=affected_paths,
                workspace_changed=workspace_changed,
                workspace_fingerprint=agent.workspace.fingerprint(),
                diff_summary=diff_summary,
            )
            return ToolExecutionResult(content=content, metadata=metadata)
        except Exception as exc:
            after_snapshot = agent.capture_workspace_snapshot() if tool["risky"] else before_snapshot
            affected_paths, diff_summary = agent.diff_workspace_snapshots(before_snapshot, after_snapshot)
            workspace_changed = bool(affected_paths)
            security_event_type = "path_escape" if "path escapes workspace" in str(exc) else ""
            metadata = _metadata(
                "partial_success" if workspace_changed else "error",
                tool_error_code="tool_partial_success" if workspace_changed else "tool_failed",
                security_event_type=security_event_type,
                risk_level="high" if tool["risky"] else "low",
                read_only=not tool["risky"],
                affected_paths=affected_paths,
                workspace_changed=workspace_changed,
                workspace_fingerprint=agent.workspace.fingerprint(),
                diff_summary=diff_summary,
            )
            return ToolExecutionResult(content=f"error: tool {name} failed: {exc}", metadata=metadata)
