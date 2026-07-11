"""Goal models.

Goals are durable outcome structures for long-horizon work. They are separate
from Tasks: a task is one execution record, while a goal captures the desired
end state, evidence, decomposition, dependencies, and revision trail.
"""
from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


def _new_id() -> str:
    import uuid

    return uuid.uuid4().hex[:10]


class GoalStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    BLOCKED = "blocked"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class GoalNodeKind(StrEnum):
    GOAL = "goal"
    SUBGOAL = "subgoal"
    MILESTONE = "milestone"
    HABIT = "habit"
    TASK_REF = "task_ref"


class GoalEvidence(BaseModel):
    description: str
    satisfied: bool = False
    note: str = ""
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    satisfied_at: datetime | None = None

    def satisfy(self, note: str = "") -> None:
        self.satisfied = True
        if note:
            self.note = note
        self.satisfied_at = datetime.now(UTC)


class GoalConstraint(BaseModel):
    description: str
    kind: str = "general"


class GoalRevision(BaseModel):
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    note: str


class GoalNode(BaseModel):
    id: str = Field(default_factory=_new_id)
    title: str
    kind: GoalNodeKind = GoalNodeKind.SUBGOAL
    status: GoalStatus = GoalStatus.DRAFT
    parent_id: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    evidence: list[GoalEvidence] = Field(default_factory=list)
    constraints: list[GoalConstraint] = Field(default_factory=list)
    owner: str = "user"
    notes: list[str] = Field(default_factory=list)
    task_ids: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)


class Goal(BaseModel):
    id: str = Field(default_factory=_new_id)
    session_id: str
    title: str
    status: GoalStatus = GoalStatus.DRAFT
    outcome: str = ""
    evidence: list[GoalEvidence] = Field(default_factory=list)
    constraints: list[GoalConstraint] = Field(default_factory=list)
    nodes: list[GoalNode] = Field(default_factory=list)
    active_node_id: str | None = None
    task_ids: list[str] = Field(default_factory=list)
    revisions: list[GoalRevision] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    def touch(self) -> None:
        self.updated_at = datetime.now(UTC)

    def add_revision(self, note: str) -> None:
        if note:
            self.revisions.append(GoalRevision(note=note))
            self.touch()
