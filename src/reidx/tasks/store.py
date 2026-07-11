"""Task store: per-session task state persisted to tasks.json."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from reidx.diagnostics.logger import get_logger
from reidx.tasks.models import Task, TaskStatus

log = get_logger("reidx.tasks")


class TaskStore:
    def __init__(self, storage_root: Path, session_id: str) -> None:
        self.path = storage_root / "sessions" / session_id / "tasks.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.session_id = session_id

    def _read(self) -> dict[str, Task]:
        if not self.path.exists():
            return {}
        import json

        text = self.path.read_text(encoding="utf-8").strip()
        if not text:
            return {}

        data: dict[str, Task] = {}
        for raw in json.loads(text).get("tasks", []):
            t = Task.model_validate(raw)
            data[t.id] = t
        return data

    def _write(self, tasks: dict[str, Task]) -> None:
        import json

        payload = {"tasks": [t.model_dump(mode="json") for t in tasks.values()]}
        self.path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def create(
        self,
        title: str,
        depends_on: list[str] | None = None,
        meta: dict[str, Any] | None = None,
    ) -> Task:
        tasks = self._read()
        t = Task(
            session_id=self.session_id,
            title=title,
            depends_on=depends_on or [],
            meta=meta or {},
        )
        tasks[t.id] = t
        self._write(tasks)
        log.debug("created task %s: %s", t.id, title)
        return t

    def get(self, task_id: str) -> Task | None:
        return self._read().get(task_id)

    def list(self) -> list[Task]:
        return list(self._read().values())

    def update_status(self, task_id: str, status: TaskStatus, summary: str = "", error: str = "") -> Task | None:
        tasks = self._read()
        t = tasks.get(task_id)
        if t is None:
            return None
        t.status = status
        if summary:
            t.summary = summary
        if error:
            t.error = error
        t.touch()
        self._write(tasks)
        return t

    def add_note(self, task_id: str, note: str) -> Task | None:
        tasks = self._read()
        t = tasks.get(task_id)
        if t is None:
            return None
        t.notes.append(note)
        t.touch()
        self._write(tasks)
        return t
