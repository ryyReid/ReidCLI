"""Subagent runtime: lifecycle tracking + event bus for spawned child agents.

The `spawn_agent` tool constructs child `Agent`s (each with its own scoped
`PolicyEngine` and tool allowlist) and reports their lifecycle here. The TUI
subscribes to the manager so the "active agents" panel below the status line
knows what to render — one row per live subagent, auto-hidden when none.

Threading: tool calls run in an executor thread (see ui/app.py) so
SubagentManager mutates from worker threads. Every mutation grabs a lock and
listeners are called with a snapshot copy so the UI can iterate without
holding it.
"""
from __future__ import annotations

import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Literal

SubagentStatus = Literal["running", "done", "error"]


@dataclass(frozen=True)
class SubagentSnapshot:
    id: str
    name: str
    status: SubagentStatus
    started_at: float
    last_action: str = ""
    finished_at: float | None = None
    error: str = ""

    @property
    def elapsed_seconds(self) -> float:
        end = self.finished_at if self.finished_at is not None else time.monotonic()
        return end - self.started_at


@dataclass
class _Row:
    id: str
    name: str
    status: SubagentStatus
    started_at: float
    last_action: str = ""
    finished_at: float | None = None
    error: str = ""
    # Small display grace period after finish so completed agents flash
    # briefly before disappearing (see prune_finished).
    linger_until: float = field(default=0.0)


Listener = Callable[[list[SubagentSnapshot]], None]

LINGER_SECONDS = 2.0


class SubagentManager:
    def __init__(self) -> None:
        self._rows: dict[str, _Row] = {}
        self._lock = threading.Lock()
        self._listeners: list[Listener] = []

    # --- subscribe --------------------------------------------------------

    def subscribe(self, listener: Listener) -> Callable[[], None]:
        with self._lock:
            self._listeners.append(listener)

        def _unsub() -> None:
            with self._lock:
                if listener in self._listeners:
                    self._listeners.remove(listener)
        return _unsub

    def _notify(self) -> None:
        # Snapshot outside the lock so listener callbacks don't reenter it.
        snap = self.snapshot()
        for listener in list(self._listeners):
            try:
                listener(snap)
            except Exception:  # noqa: BLE001 - a broken listener must not kill agents
                pass

    # --- lifecycle --------------------------------------------------------

    def start(self, name: str) -> str:
        agent_id = uuid.uuid4().hex[:8]
        with self._lock:
            self._rows[agent_id] = _Row(
                id=agent_id, name=name, status="running", started_at=time.monotonic()
            )
        self._notify()
        return agent_id

    def update(self, agent_id: str, *, last_action: str) -> None:
        with self._lock:
            row = self._rows.get(agent_id)
            if row is None or row.status != "running":
                return
            row.last_action = last_action
        self._notify()

    def finish(self, agent_id: str, *, status: SubagentStatus = "done", error: str = "") -> None:
        with self._lock:
            row = self._rows.get(agent_id)
            if row is None:
                return
            row.status = status
            row.finished_at = time.monotonic()
            row.error = error
            row.linger_until = row.finished_at + LINGER_SECONDS
        self._notify()

    # --- read -------------------------------------------------------------

    def snapshot(self) -> list[SubagentSnapshot]:
        with self._lock:
            return [
                SubagentSnapshot(
                    id=r.id, name=r.name, status=r.status, started_at=r.started_at,
                    last_action=r.last_action, finished_at=r.finished_at, error=r.error,
                )
                for r in self._rows.values()
            ]

    def visible_rows(self) -> list[SubagentSnapshot]:
        """Rows to actually render — running + recently-finished (linger)."""
        now = time.monotonic()
        with self._lock:
            keep: list[_Row] = []
            for row in self._rows.values():
                if row.status == "running" or row.linger_until > now:
                    keep.append(row)
        return [
            SubagentSnapshot(
                id=r.id, name=r.name, status=r.status, started_at=r.started_at,
                last_action=r.last_action, finished_at=r.finished_at, error=r.error,
            )
            for r in keep
        ]

    def prune_finished(self) -> bool:
        """Drop finished rows past their linger window. Returns True if any
        row was removed (so the TUI knows to invalidate and re-render). Called
        from the spinner tick."""
        now = time.monotonic()
        removed = False
        with self._lock:
            for aid, row in list(self._rows.items()):
                if row.status != "running" and row.linger_until <= now:
                    del self._rows[aid]
                    removed = True
        if removed:
            self._notify()
        return removed

    def any_active(self) -> bool:
        with self._lock:
            return any(r.status == "running" for r in self._rows.values())

    # Kept for tests / debugging.
    def _clear(self) -> None:
        with self._lock:
            self._rows.clear()


# For type hints elsewhere without a heavy import.
__all__ = ["SubagentManager", "SubagentSnapshot", "LINGER_SECONDS"]

# Discourage silent replace() calls on the frozen dataclass elsewhere.
_ = replace  # noqa: F841
