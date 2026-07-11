"""Policy engine: evaluates actions against the current permission mode and config.

This is a first-pass real policy layer — not just hardcoded guardrails. It classifies
risk, consults the active mode, and returns an explicit decision (ALLOW/DENY/PROMPT).
The caller (runtime/UI) is responsible for resolving PROMPT into ALLOW/DENY via user
input. Custom allowlists/denylists are honored for shell commands and path access.
"""
from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from reidx.policy.models import ActionKind, PermissionDecision, PermissionMode, RiskLevel

if TYPE_CHECKING:
    from reidx.config.models import Config

# Default risk classification per action kind.
_RISK_BY_KIND: dict[ActionKind, RiskLevel] = {
    ActionKind.FILE_READ: RiskLevel.LOW,
    ActionKind.FILE_WRITE: RiskLevel.MEDIUM,
    ActionKind.FILE_DELETE: RiskLevel.HIGH,
    ActionKind.SHELL_EXEC: RiskLevel.HIGH,
    ActionKind.TOOL_CALL: RiskLevel.MEDIUM,
    ActionKind.NETWORK: RiskLevel.HIGH,
}

# Commands that are always blocked regardless of mode.
_DEFAULT_BLOCKED = {"rm", "rmdir", "del", "format", "shutdown", "reboot", "mkfs"}


class PolicyEngine:
    def __init__(self, config: Config) -> None:
        self.config = config
        self.mode: PermissionMode = config.policy.default_mode
        self.blocked_commands = set(config.policy.blocked_commands) | _DEFAULT_BLOCKED
        self.allowed_commands = set(config.policy.allowed_commands)

    def set_mode(self, mode: PermissionMode) -> None:
        self.mode = mode

    def classify(self, kind: ActionKind) -> RiskLevel:
        return _RISK_BY_KIND.get(kind, RiskLevel.MEDIUM)

    def evaluate(self, kind: ActionKind, risk: RiskLevel | None = None) -> PermissionDecision:
        risk = risk or self.classify(kind)
        if self.mode is PermissionMode.STRICT:
            if kind in (ActionKind.FILE_READ, ActionKind.TOOL_CALL) and risk is RiskLevel.LOW:
                return PermissionDecision.ALLOW
            return PermissionDecision.PROMPT if risk is RiskLevel.MEDIUM else PermissionDecision.DENY
        if self.mode is PermissionMode.BALANCED:
            if risk is RiskLevel.LOW:
                return PermissionDecision.ALLOW
            if risk is RiskLevel.MEDIUM:
                return PermissionDecision.PROMPT
            return PermissionDecision.PROMPT  # high -> prompt (user can allow)
        if self.mode is PermissionMode.AUTONOMOUS:
            if risk in (RiskLevel.LOW, RiskLevel.MEDIUM):
                return PermissionDecision.ALLOW
            return PermissionDecision.PROMPT
        # CUSTOM: only explicit allowlist permits; everything else prompts.
        return PermissionDecision.PROMPT

    def check_path(self, path: Path, write: bool) -> PermissionDecision:
        workspace = (self.config.workspace_root or Path.cwd()).resolve()
        try:
            resolved = path.resolve()
        except (OSError, RuntimeError):
            return PermissionDecision.DENY

        writable_roots = {workspace, *[r.resolve() for r in self.config.policy.additional_writable_roots]}
        read_only = {r.resolve() for r in self.config.policy.read_only_paths}

        # Explicit read-only denylist still hard-denies — that list is the
        # user's "never touch this" declaration, not a soft boundary.
        if any(resolved == ro or ro in resolved.parents for ro in read_only):
            return PermissionDecision.DENY

        inside_writable = any(resolved == root or root in resolved.parents for root in writable_roots)
        kind = ActionKind.FILE_WRITE if write else ActionKind.FILE_READ
        if inside_writable:
            return self.evaluate(kind, self.classify(kind))
        # Outside the workspace: ask the user (yes/no) rather than hard-deny.
        # Confinement stays the default (writable roots still frame what's
        # "normal"), but a single approval prompt lets one-off cross-project
        # reads / writes through without editing the config.
        return PermissionDecision.PROMPT

    def check_command(self, command: str) -> PermissionDecision:
        tokens = command.strip().split()
        if not tokens:
            return PermissionDecision.DENY
        head = tokens[0]
        # Deny explicit blocked commands and dangerous shell metacharacter patterns.
        if head in self.blocked_commands:
            return PermissionDecision.DENY
        if any(tok in self.blocked_commands for tok in tokens):
            return PermissionDecision.DENY
        if self.allowed_commands and head in self.allowed_commands:
            return PermissionDecision.ALLOW
        return self.evaluate(ActionKind.SHELL_EXEC, RiskLevel.HIGH)
