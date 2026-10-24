"""File-backed, local human approval with a version-bound impact preview."""

import difflib
import hashlib
import json
import time
import uuid

from .features.memory import file_freshness
from .workspace import clip, now


class ApprovalManager:
    def __init__(self, agent):
        self.agent = agent
        self.items = agent.session.setdefault("approvals", {})

    def preview(self, name, args):
        agent = self.agent
        preview = {"cwd": str(agent.root), "affected_paths": [], "file_versions": {}}
        if name in {"write_file", "patch_file"}:
            path = agent.path(args["path"])
            relative = path.relative_to(agent.root).as_posix()
            old = path.read_text(encoding="utf-8") if path.exists() else ""
            new = args["content"] if name == "write_file" else old.replace(args["old_text"], args["new_text"], 1)
            preview["affected_paths"] = [relative]
            preview["file_versions"][relative] = file_freshness(relative, agent.root)
            preview["diff"] = clip("".join(difflib.unified_diff(old.splitlines(keepends=True), new.splitlines(keepends=True), fromfile=relative, tofile=relative)), 3000)
        elif name == "run_shell":
            preview["command"] = args["command"]
            preview["timeout"] = args.get("timeout", 20)
            preview["workspace_version"] = hashlib.sha256(json.dumps(agent.capture_workspace_snapshot(), sort_keys=True).encode()).hexdigest()
        return preview

    def binding(self):
        return {"root": str(self.agent.root), "tool_signature": self.agent.tool_signature(), "read_only": self.agent.read_only}

    def valid(self, record):
        if record.get("binding") != self.binding() or record.get("expires_at", 0) < time.time():
            return False
        try:
            current = self.preview(record["name"], record["args"])
        except (OSError, ValueError, KeyError):
            return False
        return all(current.get(key) == record["preview"].get(key) for key in ("file_versions", "workspace_version"))

    def save(self):
        self.agent.session_path = self.agent.session_store.save(self.agent.session)

    def decide(self, record, approved):
        if record["status"] != "pending":
            raise ValueError("approval is no longer pending")
        status = "approved" if approved else "denied"
        if not self.valid(record):
            status = "expired"
        if self.agent.read_only or self.agent.approval_policy == "never":
            status = "denied"
        record.update(status=status, decided_at=now())
        self.save()
        self.agent.trace_tool_event("approval_decided", {"approval_id": record["approval_id"], "status": status})
        return status == "approved"

    def request(self, name, args):
        agent = self.agent
        resumed_id = getattr(agent, "_approval_to_resume", "")
        if resumed_id:
            record = self.items[resumed_id]
            agent.last_approval_id = resumed_id
            if record["name"] != name or record["args"] != args or not self.valid(record):
                record.update(status="expired", decided_at=now())
                self.save()
                return False
            return record["status"] == "approved" and not agent.read_only and agent.approval_policy != "never"

        approval_id = "approval_" + uuid.uuid4().hex[:12]
        record = {
            "approval_id": approval_id, "call_id": agent.current_call_id,
            "run_id": agent.current_task_state.run_id if agent.current_task_state else "",
            "name": name, "args": dict(args), "risk_level": "high", "status": "pending",
            "created_at": now(), "expires_at": time.time() + 1800,
            "binding": self.binding(), "preview": self.preview(name, args),
        }
        self.items[approval_id] = record
        agent.last_approval_id = approval_id
        self.save()  # Save before input: cancellation leaves a recoverable request.
        agent.trace_tool_event("approval_requested", {"approval_id": approval_id, "name": name, "preview": record["preview"]})
        if agent.read_only or agent.approval_policy == "never":
            return self.decide(record, False)
        if agent.approval_policy == "auto":
            return self.decide(record, True)
        print(agent.redact_text(json.dumps(record["preview"], ensure_ascii=False, indent=2)))
        try:
            answer = input(f"approve {name} {json.dumps(args, ensure_ascii=True)}? [y/N] ")
        except EOFError:
            answer = "n"
        return self.decide(record, answer.strip().lower() in {"y", "yes"})

    def finish(self, result):
        approval_id = self.agent.last_approval_id
        if approval_id and approval_id in self.items:
            record = self.items[approval_id]
            if record["status"] == "approved":
                record["status"] = "executed" if result.metadata["tool_status"] == "ok" else "failed"
                record["finished_at"] = now()
                record["tool_error_code"] = result.metadata.get("tool_error_code", "")
                self.save()
