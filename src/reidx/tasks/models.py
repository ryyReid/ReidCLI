"""Task model.

Structured task state — not prose steps. Tasks belong to a session, track status
through a real state machine, and support dependencies.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex[:10]


class TaskStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class Task(BaseModel):
    id: str = Field(default_factory=_new_id)
    session_id: str
    title: str
    status: TaskStatus = TaskStatus.PENDING
    summary: str = ""
    error: str = ""
    depends_on: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)
