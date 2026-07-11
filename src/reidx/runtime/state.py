"""Runtime state: in-memory state carried across a turn / session.

Holds the active session, the message transcript (mirrored to disk by the
orchestrator), the active task id, and tool-result history. Permission mode lives
on the session and is kept in sync with the policy engine by the orchestrator.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from reidx.provider.base import Message
from reidx.session.models import Session


@dataclass
class RuntimeState:
    session: Session
    messages: list[Message] = field(default_factory=list)
    active_task_id: str | None = None
    turns: int = 0
    last_tool_results: list[dict] = field(default_factory=list)
    last_thinking: str | None = None  # chain-of-thought from the last turn (ephemeral)
    # Usage from the most recent provider.chat() call (not summed across turns —
    # each call's prompt_tokens already reflects the whole conversation resent
    # so far, so summing would multiply-count it). 0 for providers that don't
    # report usage (e.g. StubProvider); the UI falls back to a char estimate then.
    last_usage_prompt_tokens: int = 0
    last_usage_completion_tokens: int = 0

    @property
    def effective_mode(self):
        return self.session.permission_mode
