"""Session JSON persistence."""

import json
from pathlib import Path
from .storage import write_json_atomic


class SessionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, session_id):
        return self.root / f"{session_id}.json"

    def save(self, session):
        path = self.path(session["id"])
        write_json_atomic(path, session)
        return path

    def load(self, session_id):
        try:
            return json.loads(self.path(session_id).read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid session JSON: {session_id}") from exc

    def latest(self):
        files = sorted(self.root.glob("*.json"), key=lambda path: path.stat().st_mtime)
        return files[-1].stem if files else None
