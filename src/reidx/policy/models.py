"""Policy models: permission modes, decisions, risk classification.

Leaf module — no internal reidx dependencies. Referenced by config and policy.engine.
"""
from __future__ import annotations

from enum import StrEnum


class PermissionMode(StrEnum):
    STRICT = "strict"
    BALANCED = "balanced"
    AUTONOMOUS = "autonomous"
    CUSTOM = "custom"


class PermissionDecision(StrEnum):
    ALLOW = "allow"
    DENY = "deny"
    PROMPT = "prompt"


class RiskLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ActionKind(StrEnum):
    FILE_READ = "file_read"
    FILE_WRITE = "file_write"
    FILE_DELETE = "file_delete"
    SHELL_EXEC = "shell_exec"
    TOOL_CALL = "tool_call"
    NETWORK = "network"
