"""Tool system contracts.

A tool has a definition (name + description + JSON-schema parameters + risk level)
and an execute() entry point. Tools receive a ToolContext carrying the workspace,
writable roots, the policy engine, and an approver callable used to resolve PROMPT
decisions into ALLOW/DENY. Tool results are structured, never raised as exceptions
when avoidable — failures become ToolResult(ok=False, error=...).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from reidx.policy.engine import PolicyEngine
from reidx.policy.models import PermissionDecision, RiskLevel


class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    risk: RiskLevel = RiskLevel.MEDIUM


class ToolResult(BaseModel):
    ok: bool = True
    output: str = ""
    error: str = ""
    data: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def fail(cls, error: str) -> ToolResult:
        return cls(ok=False, error=error)

    @classmethod
    def ok_(cls, output: str = "", **data: Any) -> ToolResult:
        return cls(ok=True, output=output, data=data)


# Approver signature: (prompt_text) -> bool. Supplied by the UI layer.
Approver = Callable[[str], bool]


@dataclass
class ToolContext:
    workspace_root: Path
    policy: PolicyEngine
    writable_roots: list[Path] = field(default_factory=list)
    approver: Approver | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def resolve_decision(self, prompt_text: str) -> PermissionDecision:
        """Resolve a PROMPT decision via the approver; defaults to DENY if none set."""
        if self.approver is None:
            return PermissionDecision.DENY
        return PermissionDecision.ALLOW if self.approver(prompt_text) else PermissionDecision.DENY


class BaseTool(ABC):
    @property
    @abstractmethod
    def definition(self) -> ToolDefinition:
        ...

    @abstractmethod
    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        ...

    def schema(self) -> dict[str, Any]:
        """OpenAI-style function schema for provider tool passing."""
        d = self.definition
        return {
            "type": "function",
            "function": {
                "name": d.name,
                "description": d.description,
                "parameters": d.parameters or {"type": "object", "properties": {}},
            },
        }
