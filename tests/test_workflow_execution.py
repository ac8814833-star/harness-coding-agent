import json

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.task_state import TaskState
from pico.tool_schema import validate_parameters


def build(tmp_path, outputs=()):
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    return Pico(FakeModelClient(outputs), WorkspaceContext.build(tmp_path), SessionStore(tmp_path / ".pico" / "sessions"), approval_policy="auto")


@pytest.mark.parametrize("name,args", [
    ("read_file", {"path": "sample.txt", "start": True}),
    ("read_file", {"path": "sample.txt", "start": "1"}),
    ("read_file", {"path": "sample.txt", "start": 1.2}),
    ("read_file", {"path": "sample.txt", "extra": 1}),
    ("write_file", {"path": "new.txt", "content": 123}),
    ("run_shell", {"command": "echo hello", "timeout": 121}),
    ("delegate", {"task": "inspect", "max_steps": 0}),
])
def test_schema_rejects_invalid_input_without_executing(tmp_path, name, args):
    agent = build(tmp_path)
    result = agent.execute_tool(name, args)
    assert result.metadata["tool_error_code"] == "invalid_arguments"
    assert not (tmp_path / "new.txt").exists()


def test_required_fields_empty_content_and_enum_contract(tmp_path):
    agent = build(tmp_path)
    assert agent.execute_tool("write_file", {"path": "new.txt"}).metadata["tool_status"] == "rejected"
    assert agent.execute_tool("write_file", {"path": "new.txt", "content": ""}).metadata["tool_status"] == "ok"
    with pytest.raises(ValueError, match="one of"):
        validate_parameters({"properties": {"mode": {"type": "string", "enum": ["safe"]}}}, {"mode": "unsafe"})


def test_xml_numeric_attributes_are_normalized_before_validation(tmp_path):
    agent = build(tmp_path)
    kind, payload = agent.parse('<tool name="read_file" path="sample.txt" start="1" end="1"></tool>')
    assert kind == "tool"
    assert agent.execute_tool(payload["name"], payload["args"]).metadata["tool_status"] == "ok"


def test_trace_correlates_rejected_and_successful_calls_and_phases(tmp_path):
    agent = build(tmp_path, [
        '<tool>{"name":"read_file","args":{"path":"sample.txt","start":false}}</tool>',
        '<tool>{"name":"read_file","args":{"path":"sample.txt"}}</tool>',
        '<final>Done.</final>',
    ])
    agent.ask("Inspect sample.txt")
    events = [json.loads(line) for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()]
    requests = [e for e in events if e["event"] == "tool_requested"]
    finishes = [e for e in events if e["event"] == "tool_finished"]
    assert len({e["call_id"] for e in requests}) == 2
    assert [e["call_id"] for e in requests] == [e["call_id"] for e in finishes]
    assert [e["tool_status"] for e in finishes] == ["rejected", "ok"]
    phases = {e["phase"] for e in events if e["event"] == "phase_changed"}
    assert {"building_context", "waiting_model", "validating_tool", "executing_tool", "recording"} <= phases
    assert agent.current_task_state.phase == "finished"


def test_repeated_read_is_allowed_after_file_content_changes(tmp_path):
    agent = build(tmp_path)
    args = {"path": "sample.txt"}
    for _ in range(2):
        result = agent.execute_tool("read_file", args)
        agent.record({"role": "tool", "name": "read_file", "args": args, "content": result.content, "input_fingerprint": result.metadata["input_fingerprint"]})
    assert agent.execute_tool("read_file", args).metadata["tool_error_code"] == "repeated_identical_call"
    (tmp_path / "sample.txt").write_text("beta\n", encoding="utf-8")
    result = agent.execute_tool("read_file", args)
    assert result.metadata["tool_status"] == "ok"
    assert "beta" in result.content


def test_phase_failure_and_legacy_state_loading():
    state = TaskState.from_dict({"run_id": "old", "task_id": "task", "user_request": "request"})
    state.set_phase("waiting_model").stop_model_error("offline")
    assert state.phase == "failed"
    assert state.failure_phase == "waiting_model"
    assert TaskState.from_dict(state.to_dict()).failure_phase == "waiting_model"
    with pytest.raises(ValueError):
        state.set_phase("unknown")


def test_fixed_python_verifier_uses_active_interpreter_and_preserves_script():
    import subprocess
    import sys
    from pico.evaluation.evaluator import verifier_invocation

    command, shell = verifier_invocation('python3 -c "print(123)"')
    assert command == [sys.executable, "-c", "print(123)"]
    assert shell is False
    assert subprocess.run(command, capture_output=True, text=True).stdout.strip() == "123"
    assert verifier_invocation("echo hello && echo world") == ("echo hello && echo world", True)
