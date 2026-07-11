"""Shell tool: controlled command execution.

Gated by the policy engine (command allowlist/denylist + mode). Uses subprocess with
a timeout, captures stdout/stderr, and returns a structured ToolResult. Shell is
treated as HIGH risk by default — in balanced/strict modes the user is prompted.
"""
from __future__ import annotations

import subprocess
from typing import Any

from reidx.policy.models import PermissionDecision, RiskLevel
from reidx.tools.base import BaseTool, ToolContext, ToolDefinition, ToolResult

_PARAMS = {
    "type": "object",
    "properties": {
        "command": {"type": "string", "description": "Shell command to execute."},
        "cwd": {"type": "string", "description": "Working directory. Defaults to workspace root."},
    },
    "required": ["command"],
}


class RunCommandTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="run_command",
            description="Run a shell command with policy approval and timeout.",
            parameters=_PARAMS,
            risk=RiskLevel.HIGH,
        )

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        command = str(args.get("command", "")).strip()
        if not command:
            return ToolResult.fail("empty command")
        decision = ctx.policy.check_command(command)
        if decision is PermissionDecision.DENY:
            return ToolResult.fail(f"command blocked by policy: {command}")
        if decision is PermissionDecision.PROMPT:
            if ctx.resolve_decision(f"Run command? `{command}`") is PermissionDecision.DENY:
                return ToolResult.fail("command denied by user")

        cwd = str(args.get("cwd", "")) or str(ctx.workspace_root)
        timeout = ctx.policy.config.policy.shell_timeout_seconds
        try:
            proc = subprocess.run(
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            return ToolResult.fail(f"command timed out after {timeout}s")
        except OSError as exc:
            return ToolResult.fail(f"failed to spawn: {exc}")
        out = proc.stdout
        if proc.stderr:
            out += ("\n--- stderr ---\n" + proc.stderr) if out else proc.stderr
        ok = proc.returncode == 0
        return ToolResult(
            ok=ok,
            output=out.rstrip(),
            error="" if ok else f"exit code {proc.returncode}",
            data={"exit_code": proc.returncode},
        )


def register_shell_tool(registry) -> None:  # type: ignore[no-untyped-def]
    registry.register(RunCommandTool())
