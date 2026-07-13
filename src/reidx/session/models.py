"""Session model.

A Session is a first-class runtime object: identity, workspace, provider/model state,
permission mode, status, and timestamps. Transcript and task state live alongside
under the session storage directory (see session.store).
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from reidx.policy.models import PermissionMode


def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex[:12]


class SessionStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    ARCHIVED = "archived"


class Session(BaseModel):
    id: str = Field(default_factory=_new_id)
    title: str = ""
    workspace: Path
    provider: str = "stub"
    model: str = "stub-v0"
    reasoning_effort: str = "medium"
    # Context window for the active model, filled from the provider /models API
    # (0 = unknown → status bar falls back to a generic default).
    context_window: int = 0
    # Token streaming into the TUI: auto (on when provider supports) | on | off.
    # Default auto so OpenAI-compatible backends stream without a flag.
    stream: str = "auto"
    permission_mode: PermissionMode = PermissionMode.BALANCED
    status: SessionStatus = SessionStatus.ACTIVE
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)
