"""Workflow model: a named, reusable sequence of steps.

Each step is either a slash command (e.g. "/model gpt-5-5") or a plain prompt
submitted as a user turn — exactly what you'd type into the box, run in
order. Workflows are global (not per-session or per-workspace): saved once,
runnable from any session.
"""
from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class Workflow(BaseModel):
    name: str
    description: str = ""
    steps: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
