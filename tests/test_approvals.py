import json

import pytest

from pico import FakeModelClient, Pico, SessionStore, WorkspaceContext


def build(tmp_path, **kwargs):
    (tmp_path / "sample.txt").write_text("alpha\n", encoding="utf-8")
    return Pico(FakeModelClient([]), WorkspaceContext.build(tmp_path), SessionStore(tmp_path / ".pico" / "sessions"), **kwargs)


@pytest.mark.parametrize("policy,read_only,expected", [("auto", False, "executed"), ("never", False, "denied"), ("auto", True, "denied")])
def test_approval_policies_are_recorded_and_enforced(tmp_path, policy, read_only, expected):
    agent = build(tmp_path, approval_policy=policy, read_only=read_only)
    result = agent.execute_tool("write_file", {"path": "new.txt", "content": "new"})
    record = agent.session["approvals"][result.metadata["approval_id"]]
    assert record["status"] == expected
    assert (tmp_path / "new.txt").exists() is (expected == "executed")


def test_approval_preview_and_rejection_preserve_file(tmp_path, monkeypatch, capsys):
    agent = build(tmp_path, approval_policy="ask")
    monkeypatch.setattr("builtins.input", lambda _: "n")
    result = agent.execute_tool("patch_file", {"path": "sample.txt", "old_text": "alpha", "new_text": "beta"})
    record = agent.session["approvals"][result.metadata["approval_id"]]
    assert "-alpha" in record["preview"]["diff"]
    assert "+beta" in record["preview"]["diff"]
    assert "sample.txt" in capsys.readouterr().out
    assert (tmp_path / "sample.txt").read_text() == "alpha\n"
    assert record["status"] == "denied"


def test_edit_during_approval_expires_request(tmp_path, monkeypatch):
    agent = build(tmp_path, approval_policy="ask")
    def answer(_):
        (tmp_path / "sample.txt").write_text("external\n", encoding="utf-8")
        return "y"
    monkeypatch.setattr("builtins.input", answer)
    result = agent.execute_tool("write_file", {"path": "sample.txt", "content": "overwrite"})
    assert result.metadata["tool_status"] == "rejected"
    assert agent.session["approvals"][result.metadata["approval_id"]]["status"] == "expired"
    assert (tmp_path / "sample.txt").read_text() == "external\n"


def test_interrupted_approval_survives_reload_and_executes_once(tmp_path, monkeypatch):
    agent = build(tmp_path, approval_policy="ask")
    def interrupt(_):
        raise KeyboardInterrupt
    monkeypatch.setattr("builtins.input", interrupt)
    with pytest.raises(KeyboardInterrupt):
        agent.execute_tool("write_file", {"path": "new.txt", "content": "approved"})
    pending = agent.pending_approvals()[0]
    assert not (tmp_path / "new.txt").exists()
    restored = Pico.from_session(FakeModelClient([]), WorkspaceContext.build(tmp_path), agent.session_store, agent.session["id"], approval_policy="ask")
    result = restored.resolve_approval(pending["approval_id"], True)
    assert result.metadata["tool_status"] == "ok"
    assert (tmp_path / "new.txt").read_text() == "approved"
    assert not restored.pending_approvals()
    with pytest.raises(ValueError, match="not pending"):
        restored.resolve_approval(pending["approval_id"], True)
    assert restored.run_store.load_report(restored.current_task_state.run_id)["status"] == "completed"


def test_resumed_approval_cannot_overwrite_changed_target(tmp_path, monkeypatch):
    agent = build(tmp_path, approval_policy="ask")
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        agent.execute_tool("write_file", {"path": "sample.txt", "content": "overwrite"})
    pending = agent.pending_approvals()[0]
    (tmp_path / "sample.txt").write_text("changed", encoding="utf-8")
    result = agent.resolve_approval(pending["approval_id"], True)
    assert result.metadata["tool_status"] == "rejected"
    assert (tmp_path / "sample.txt").read_text() == "changed"
    saved = json.loads(agent.session_path.read_text(encoding="utf-8"))
    assert saved["approvals"][pending["approval_id"]]["status"] == "expired"


def test_cli_lists_and_resolves_saved_approval(tmp_path, monkeypatch, capsys):
    from pico import cli

    agent = build(tmp_path, approval_policy="ask")
    monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        agent.execute_tool("write_file", {"path": "new.txt", "content": "from CLI"})
    approval_id = agent.pending_approvals()[0]["approval_id"]
    commands = iter(["/approvals", f"/approve {approval_id}", f"/deny {approval_id}", "/exit"])
    monkeypatch.setattr("builtins.input", lambda _: next(commands))
    monkeypatch.setattr(cli, "build_agent", lambda _: agent)
    assert cli.main([]) == 0
    output = capsys.readouterr()
    assert approval_id in output.out
    assert "not pending" in output.err
    assert (tmp_path / "new.txt").read_text() == "from CLI"
