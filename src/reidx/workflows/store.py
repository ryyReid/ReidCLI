"""Workflow store: global (not per-session) persistence to workflows.json.

Layout under storage_root: workflows.json — {"workflows": [Workflow, ...]}.
"""
from __future__ import annotations

import json
from pathlib import Path

from reidx.diagnostics.logger import get_logger
from reidx.workflows.models import Workflow

log = get_logger("reidx.workflows")


class WorkflowStore:
    def __init__(self, storage_root: Path) -> None:
        self.path = storage_root / "workflows.json"
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> dict[str, Workflow]:
        if not self.path.exists():
            return {}
        text = self.path.read_text(encoding="utf-8").strip()
        if not text:
            return {}
        data: dict[str, Workflow] = {}
        for raw in json.loads(text).get("workflows", []):
            wf = Workflow.model_validate(raw)
            data[wf.name] = wf
        return data

    def _write(self, workflows: dict[str, Workflow]) -> None:
        payload = {"workflows": [wf.model_dump(mode="json") for wf in workflows.values()]}
        self.path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")

    def save(self, workflow: Workflow) -> Workflow:
        workflows = self._read()
        workflows[workflow.name] = workflow
        self._write(workflows)
        log.debug("saved workflow %s (%d steps)", workflow.name, len(workflow.steps))
        return workflow

    def get(self, name: str) -> Workflow | None:
        return self._read().get(name)

    def list(self) -> list[Workflow]:
        return sorted(self._read().values(), key=lambda w: w.name)

    def delete(self, name: str) -> bool:
        workflows = self._read()
        if name not in workflows:
            return False
        del workflows[name]
        self._write(workflows)
        return True
