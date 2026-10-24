import json

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico import storage
from pico.features.memory import file_freshness


def build(tmp_path, outputs=()):
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    return Pico(FakeModelClient(outputs), WorkspaceContext.build(tmp_path), SessionStore(tmp_path / ".pico" / "sessions"), approval_policy="auto")


def restore(agent, outputs=()):
    return Pico.from_session(FakeModelClient(outputs), WorkspaceContext.build(agent.root), agent.session_store, agent.session["id"], approval_policy="auto")


def test_failed_session_replace_preserves_previous_json_and_removes_temp(tmp_path, monkeypatch):
    store = SessionStore(tmp_path)
    store.save({"id": "session", "value": "old"})
    def fail(*_):
        raise OSError("replace failed")
    monkeypatch.setattr(storage.os, "replace", fail)
    with pytest.raises(OSError, match="replace failed"):
        store.save({"id": "session", "value": "new"})
    assert store.load("session")["value"] == "old"
    assert not list(tmp_path.glob("*.tmp"))


def test_invalid_session_is_reported_without_overwriting_source(tmp_path):
    store = SessionStore(tmp_path)
    store.path("broken").write_text('{"truncated":', encoding="utf-8")
    with pytest.raises(ValueError, match="Invalid session JSON"):
        store.load("broken")
    assert store.path("broken").read_text() == '{"truncated":'


def test_interrupt_during_model_call_saves_phase_checkpoint_and_report(tmp_path):
    agent = build(tmp_path)
    def interrupt(*_, **__):
        raise KeyboardInterrupt
    agent.model_client.complete = interrupt
    with pytest.raises(KeyboardInterrupt):
        agent.ask("Inspect sample.txt")
    state = agent.current_task_state
    assert state.status == "stopped"
    assert state.phase == "interrupted"
    assert state.failure_phase == "waiting_model"
    assert agent.current_checkpoint()["current_blocker"] == "interrupted"
    assert agent.run_store.load_report(state.run_id)["stop_reason"] == "interrupted"
    assert restore(agent).current_checkpoint()["checkpoint_id"] == state.checkpoint_id


def test_old_file_observation_blocks_write_until_reread(tmp_path):
    agent = build(tmp_path)
    agent.execute_tool("read_file", {"path": "sample.txt"})
    (tmp_path / "sample.txt").write_text("external\n", encoding="utf-8")
    result = agent.execute_tool("write_file", {"path": "sample.txt", "content": "overwrite"})
    assert result.metadata["tool_error_code"] == "recovery_guard"
    assert (tmp_path / "sample.txt").read_text() == "external\n"
    agent.execute_tool("read_file", {"path": "sample.txt"})
    assert agent.execute_tool("write_file", {"path": "sample.txt", "content": "updated"}).metadata["tool_status"] == "ok"


def test_expected_hash_is_returned_by_read_and_enforced_on_write(tmp_path):
    agent = build(tmp_path)
    version = file_freshness("sample.txt", tmp_path)
    assert version in agent.execute_tool("read_file", {"path": "sample.txt"}).content
    bad = agent.execute_tool("write_file", {"path": "sample.txt", "content": "new", "expected_hash": "0" * 64})
    assert bad.metadata["tool_status"] == "rejected"
    good = agent.execute_tool("write_file", {"path": "sample.txt", "content": "new", "expected_hash": version})
    assert good.metadata["tool_status"] == "ok"


def test_completed_patch_is_reconciled_after_interruption_and_not_repeated(tmp_path):
    args = {"path": "sample.txt", "old_text": "alpha", "new_text": "beta"}
    tool = '<tool>' + json.dumps({"name": "patch_file", "args": args}) + '</tool>'
    agent = build(tmp_path, [tool])
    runner = agent.tools["patch_file"]["run"]
    def patch_and_interrupt(parameters):
        runner(parameters)
        raise KeyboardInterrupt
    agent.tools["patch_file"]["run"] = patch_and_interrupt
    with pytest.raises(KeyboardInterrupt):
        agent.ask("Replace alpha with beta")
    assert next(iter(agent.session["actions"].values()))["status"] == "unknown"
    resumed = restore(agent)
    action = next(iter(resumed.session["actions"].values()))
    assert action["status"] == "reconciled"
    def forbidden(_):
        pytest.fail("reconciled file operation must not run again")
    resumed.tools["patch_file"]["run"] = forbidden
    result = resumed.execute_tool("patch_file", args)
    assert result.metadata["recovered_action_id"] == action["action_id"]
    assert (tmp_path / "sample.txt").read_text() == "beta\n"


def test_unknown_shell_outcome_requires_review_but_allows_inspection(tmp_path):
    agent = build(tmp_path, ['<tool>{"name":"run_shell","args":{"command":"test-command"}}</tool>'])
    def interrupt(_):
        raise KeyboardInterrupt
    agent.tools["run_shell"]["run"] = interrupt
    with pytest.raises(KeyboardInterrupt):
        agent.ask("Run the command")
    resumed = restore(agent)
    resumed.tools["run_shell"]["run"] = lambda _: "exit_code: 0\nstdout: done"
    assert resumed.execute_tool("read_file", {"path": "sample.txt"}).metadata["tool_status"] == "ok"
    assert resumed.execute_tool("run_shell", {"command": "test-command"}).metadata["tool_error_code"] == "recovery_guard"
    action_id = next(iter(resumed.session["actions"]))
    resumed.acknowledge_action(action_id)
    assert resumed.execute_tool("run_shell", {"command": "test-command"}).metadata["tool_status"] == "ok"


def test_runtime_failure_is_recorded_with_its_phase(tmp_path, monkeypatch):
    agent = build(tmp_path)
    def fail(_):
        raise ValueError("context failure")
    monkeypatch.setattr(agent, "_build_prompt_and_metadata", fail)
    with pytest.raises(ValueError, match="context failure"):
        agent.ask("Inspect")
    report = agent.run_store.load_report(agent.current_task_state.run_id)
    assert report["status"] == "failed"
    assert report["failure_phase"] == "building_context"


def test_one_shot_cli_interrupt_returns_130_and_saves_state(tmp_path, monkeypatch):
    from pico import cli
    agent = build(tmp_path)
    def interrupt(*_, **__):
        raise KeyboardInterrupt
    agent.model_client.complete = interrupt
    monkeypatch.setattr(cli, "build_agent", lambda _: agent)
    assert cli.main(["Inspect sample.txt"]) == 130
    assert agent.run_store.load_report(agent.current_task_state.run_id)["stop_reason"] == "interrupted"


def test_stale_write_is_rejected_before_human_approval(tmp_path, monkeypatch):
    agent = build(tmp_path)
    agent.execute_tool("read_file", {"path": "sample.txt"})
    (tmp_path / "sample.txt").write_text("external", encoding="utf-8")
    agent.approval_policy = "ask"
    monkeypatch.setattr("builtins.input", lambda _: pytest.fail("stale actions should fail before asking"))
    assert agent.execute_tool("write_file", {"path": "sample.txt", "content": "overwrite"}).metadata["tool_error_code"] == "recovery_guard"
