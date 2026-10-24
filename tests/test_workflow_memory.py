import json

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext
from pico.features.memory import LayeredMemory, file_freshness


def build(tmp_path, outputs=()):
    (tmp_path / "sample.py").write_text('"""Example module."""\ndef handler():\n    return "alpha"\n', encoding="utf-8")
    return Pico(FakeModelClient(outputs), WorkspaceContext.build(tmp_path), SessionStore(tmp_path / ".pico" / "sessions"), approval_policy="auto")


def test_progress_tracks_constraints_observed_steps_and_reported_decisions(tmp_path):
    agent = build(tmp_path, ['<tool>{"name":"read_file","args":{"path":"sample.py"}}</tool>', '<final>Decision: Keep the handler signature.\nNext step: Add a focused test.</final>'])
    agent.ask("Inspect the module\nConstraint: Preserve the public API")
    progress = agent.memory.to_dict()["working"]["progress"]
    assert progress["constraints"] == ["Preserve the public API"]
    assert "read_file: sample.py" in progress["completed"]
    assert "Requested read_file: sample.py" in progress["decisions"]
    assert "Model reported: Keep the handler signature." in progress["decisions"]
    assert progress["next_step"] == "Add a focused test."
    assert progress["evidence"][0]["call_id"]
    saved = agent.session_store.load(agent.session["id"])
    assert saved["memory"]["working"]["progress"] == progress


def test_file_summary_records_source_version_range_and_symbols(tmp_path):
    agent = build(tmp_path)
    agent.execute_tool("read_file", {"path": "sample.py", "start": 1, "end": 3})
    summary = agent.memory.to_dict()["file_summaries"]["sample.py"]
    assert summary["source"] == "sample.py"
    assert summary["line_range"] == [1, 3]
    assert summary["symbols"] == ["handler"]
    assert summary["freshness"] == file_freshness("sample.py", tmp_path)
    assert "sha256" not in summary["summary"]


def test_file_change_invalidates_sourced_notes_but_keeps_general_experience(tmp_path):
    agent = build(tmp_path)
    agent.execute_tool("read_file", {"path": "sample.py"})
    agent.memory.append_note("Inspect diffs after partial success", tags=("process",), source="run_shell", kind="process")
    assert agent.memory.retrieval_candidates("alpha")
    (tmp_path / "sample.py").write_text("value = 'beta'\n", encoding="utf-8")
    agent.invalidate_stale_memory()
    assert not agent.memory.retrieval_candidates("alpha")
    assert agent.memory.retrieval_candidates("partial success")
    notes = agent.memory.to_dict()["episodic_notes"]
    assert any(note.get("status") == "stale" for note in notes)


def test_reread_replaces_stale_fact_in_memory(tmp_path):
    agent = build(tmp_path)
    agent.execute_tool("read_file", {"path": "sample.py"})
    (tmp_path / "sample.py").write_text("value = 'beta'\n", encoding="utf-8")
    agent.execute_tool("read_file", {"path": "sample.py"})
    assert not agent.memory.retrieval_candidates("alpha")
    assert "beta" in agent.memory.retrieval_view("beta")


def test_recent_stale_tool_output_is_hidden_from_prompt_but_preserved_on_disk(tmp_path):
    agent = build(tmp_path)
    result = agent.execute_tool("read_file", {"path": "sample.py"})
    agent.record({"role": "tool", "name": "read_file", "args": {"path": "sample.py"}, "content": result.content, "file_version": result.metadata["file_version"]})
    (tmp_path / "sample.py").write_text("value = 'beta'\n", encoding="utf-8")
    prompt = agent.prompt("Inspect the latest file state")
    assert "alpha" not in prompt
    assert "Stale read result" in prompt
    assert "alpha" in agent.session_store.load(agent.session["id"])["history"][-1]["content"]


def test_read_version_is_bound_to_returned_bytes_not_a_later_file_state(tmp_path):
    agent = build(tmp_path)
    version = file_freshness("sample.py", tmp_path)
    runner = agent.tools["read_file"]["run"]
    def read_then_change(args):
        result = runner(args)
        (tmp_path / "sample.py").write_text("value = 'beta'\n", encoding="utf-8")
        return result
    agent.tools["read_file"]["run"] = read_then_change
    agent.execute_tool("read_file", {"path": "sample.py"})
    assert agent.memory.to_dict()["file_summaries"]["sample.py"]["freshness"] == version
    result = agent.execute_tool("write_file", {"path": "sample.py", "content": "overwrite"})
    assert result.metadata["tool_error_code"] == "recovery_guard"


def test_interrupted_task_keeps_goal_and_observed_progress_on_resume(tmp_path):
    agent = build(tmp_path, ['<tool>{"name":"read_file","args":{"path":"sample.py"}}</tool>'])
    with pytest.raises(RuntimeError, match="ran out"):
        agent.ask("Investigate the original module")
    resumed = Pico.from_session(FakeModelClient(['<final>Done.</final>']), WorkspaceContext.build(tmp_path), agent.session_store, agent.session["id"], approval_policy="auto")
    resumed.ask("Continue")
    progress = resumed.memory.to_dict()["working"]["progress"]
    assert progress["goal"] == "Investigate the original module"
    assert "read_file: sample.py" in progress["completed"]
    assert resumed.current_checkpoint()["current_goal"] == "Investigate the original module"


def test_rejections_create_reusable_process_notes(tmp_path):
    agent = build(tmp_path)
    agent.execute_tool("write_file", {})
    notes = agent.memory.retrieval_candidates("invalid_arguments")
    assert notes and notes[0]["kind"] == "process"
    assert "invalid_arguments" in notes[0]["text"]


def test_legacy_memory_normalizes_without_changing_known_facts():
    memory = LayeredMemory({"task": "legacy", "notes": ["Known fact"]})
    assert memory.to_dict()["working"]["task_summary"] == "legacy"
    assert memory.to_dict()["working"]["progress"]["completed"] == []
    assert "Known fact" in json.dumps(memory.to_dict())


def test_resolving_approval_keeps_original_task_goal_for_continuation(tmp_path, monkeypatch):
    agent = build(tmp_path, ['<tool>{"name":"write_file","args":{"path":"new.txt","content":"new"}}</tool>'])
    agent.approval_policy = "ask"
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        agent.ask("Create the requested project file")
    approval_id = agent.pending_approvals()[0]["approval_id"]
    agent.resolve_approval(approval_id, True)
    agent.model_client = FakeModelClient(['<final>Done.</final>'])
    agent.ask("Continue")
    assert agent.current_checkpoint()["current_goal"] == "Create the requested project file"


def test_file_summary_links_last_observed_change_after_reread(tmp_path):
    agent = build(tmp_path)
    changed = agent.execute_tool("patch_file", {"path": "sample.py", "old_text": "alpha", "new_text": "beta"})
    agent.execute_tool("read_file", {"path": "sample.py"})
    summary = agent.memory.to_dict()["file_summaries"]["sample.py"]
    assert summary["last_change"]["operation"] == "patch_file"
    assert summary["last_change"]["call_id"] == changed.metadata["call_id"]
    assert summary["last_change"]["freshness"] == summary["freshness"]


def test_legacy_checkpoint_goal_is_used_to_seed_structured_progress(tmp_path):
    from pico.checkpoint import CHECKPOINT_SCHEMA_VERSION
    agent = build(tmp_path, ['<final>Done.</final>'])
    agent.session["checkpoints"] = {"current_id": "legacy", "items": {"legacy": {
        "checkpoint_id": "legacy", "schema_version": CHECKPOINT_SCHEMA_VERSION,
        "current_goal": "Original unfinished task", "completed": [], "key_files": [],
        "runtime_identity": agent.current_runtime_identity(),
    }}}
    agent.ask("Continue")
    assert agent.current_checkpoint()["current_goal"] == "Original unfinished task"
