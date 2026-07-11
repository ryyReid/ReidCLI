"""Session store: structured per-session persistence.

Layout under storage_root:
    sessions/
      <session-id>/
        meta.json        # Session record
        transcript.jsonl # one Message per line (restorable into state.messages)
        tasks.json       # task state for the session
        events.jsonl     # runtime action log (turn summaries, lifecycle events)
"""
from __future__ import annotations

from pathlib import Path

from reidx.diagnostics.logger import EventLog, get_logger
from reidx.provider.base import Message
from reidx.session.models import Session, SessionStatus

log = get_logger("reidx.session")

_MAX_RESUME_MESSAGES = 100


class SessionStore:
    def __init__(self, storage_root: Path) -> None:
        self.root = storage_root / "sessions"
        self.root.mkdir(parents=True, exist_ok=True)

    def _dir(self, session_id: str) -> Path:
        return self.root / session_id

    def create(self, session: Session) -> Session:
        d = self._dir(session.id)
        d.mkdir(parents=True, exist_ok=True)
        self._write_meta(session)
        log.debug("created session %s in %s", session.id, session.workspace)
        return session

    def _write_meta(self, session: Session) -> None:
        session.touch()
        (self._dir(session.id) / "meta.json").write_text(
            session.model_dump_json(indent=2), encoding="utf-8"
        )

    def get(self, session_id: str) -> Session | None:
        path = self._dir(session_id) / "meta.json"
        if not path.exists():
            return None
        return Session.model_validate_json(path.read_text(encoding="utf-8"))

    def update(self, session: Session) -> None:
        if not self._dir(session.id).exists():
            raise FileNotFoundError(f"session {session.id} not found")
        self._write_meta(session)

    def list(self) -> list[Session]:
        sessions: list[Session] = []
        for child in sorted(self.root.iterdir()):
            meta = child / "meta.json"
            if meta.exists():
                sessions.append(Session.model_validate_json(meta.read_text(encoding="utf-8")))
        return sessions

    def set_status(self, session_id: str, status: SessionStatus) -> Session | None:
        s = self.get(session_id)
        if s is None:
            return None
        s.status = status
        self.update(s)
        return s

    def session_dir(self, session_id: str) -> Path:
        return self._dir(session_id)

    def event_log(self, session_id: str) -> EventLog:
        return EventLog(self._dir(session_id) / "events.jsonl")

    def transcript_log(self, session_id: str) -> EventLog:
        return EventLog(self._dir(session_id) / "transcript.jsonl")

    def append_message(self, session_id: str, message: Message) -> None:
        """Append one Message to transcript.jsonl for restorable resume."""
        path = self._dir(session_id) / "transcript.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        line = message.model_dump_json()
        with path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def read_messages(self, session_id: str, limit: int = _MAX_RESUME_MESSAGES) -> list[Message]:
        """Read transcript.jsonl back into Message objects (most recent `limit`)."""
        path = self._dir(session_id) / "transcript.jsonl"
        if not path.exists():
            return []
        lines = path.read_text(encoding="utf-8").splitlines()
        messages: list[Message] = []
        for line in lines[-limit:]:
            try:
                messages.append(Message.model_validate_json(line))
            except Exception:  # noqa: BLE001 - skip corrupt lines
                continue
        return messages
