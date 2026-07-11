"""spawn_agent tool: run a child Agent with an inline system prompt and a
scoped tool allowlist.

Design (see README): child gets its own Agent + PolicyEngine, sharing the
parent's ToolRegistry but wrapped so only the allowlisted tools are visible.
No provider swap by default — the child inherits the parent's provider so
switching to a local model at the top level (via `/use`) applies to child
agents too. Optionally the caller can override provider and/or model.

Lifecycle is reported to `orchestrator.subagents` (a SubagentManager) so the
TUI panel below the status line renders one live row per active child.
"""
from __future__ import annotations

from typing import Any

from reidx.diagnostics.logger import get_logger
from reidx.policy.engine import PolicyEngine
from reidx.provider.base import Message
from reidx.runtime.state import RuntimeState
from reidx.session.models import Session
from reidx.tools.base import BaseTool, ToolContext, ToolDefinition, ToolResult
from reidx.tools.registry import ToolRegistry

log = get_logger("reidx.tools.spawn_agent")

MAX_CHILD_STEPS = 6
DEFAULT_ALLOWED = ("read_file", "list_dir", "find_files", "grep_files")


class SpawnAgentTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        from reidx.policy.models import RiskLevel
        return ToolDefinition(
            name="spawn_agent",
            description=(
                "Run a specialized child agent with an inline system prompt and a "
                "restricted tool set. Blocks until the child returns its final "
                "text. Use for parallel research, focused review, or any task that "
                "shouldn't pollute the main conversation. Child cannot spawn its "
                "own subagents."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "Short label shown in the subagent panel (e.g. 'researcher').",
                    },
                    "system_prompt": {
                        "type": "string",
                        "description": "System prompt that fully specifies the child's role and constraints.",
                    },
                    "task": {
                        "type": "string",
                        "description": "The task/user message for the child to work on.",
                    },
                    "tool_allowlist": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "Names of tools the child may call. Omit for a "
                            "read-only default (read_file, list_dir, find_files, "
                            "grep_files). spawn_agent itself is never available "
                            "to the child."
                        ),
                    },
                    "model": {
                        "type": "string",
                        "description": "Optional model override for the child (defaults to parent's).",
                    },
                    "provider": {
                        "type": "string",
                        "description": (
                            "Optional provider name override (must be registered; "
                            "see /providers)."
                        ),
                    },
                    "max_steps": {
                        "type": "integer",
                        "description": f"Max tool-calling steps for the child (default {MAX_CHILD_STEPS}).",
                    },
                },
                "required": ["name", "system_prompt", "task"],
            },
            risk=RiskLevel.MEDIUM,
        )

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        orch = ctx.extra.get("orchestrator")
        if orch is None:
            return ToolResult.fail("spawn_agent unavailable (no orchestrator in context)")

        name = str(args.get("name") or "subagent").strip() or "subagent"
        system_prompt = str(args.get("system_prompt") or "").strip()
        task = str(args.get("task") or "").strip()
        if not system_prompt or not task:
            return ToolResult.fail("spawn_agent requires both 'system_prompt' and 'task'")

        allowlist = args.get("tool_allowlist") or list(DEFAULT_ALLOWED)
        if not isinstance(allowlist, list):
            return ToolResult.fail("tool_allowlist must be a list of tool names")
        # Prevent recursive spawning — deep chains of nested subagents amplify
        # errors and blow up the panel. One layer only.
        allowlist = [t for t in allowlist if t != "spawn_agent"]

        # Build a filtered ToolRegistry so the child provider only sees the
        # allowlisted schemas *and* the registry rejects unknown-name calls.
        parent_tools: ToolRegistry = orch.tools
        child_tools = ToolRegistry()
        for tool_name in allowlist:
            tool = parent_tools.get(tool_name)
            if tool is None:
                log.debug("skipping unknown tool in allowlist: %s", tool_name)
                continue
            child_tools.register(tool)

        # Provider override (optional). Defaults to parent's active provider.
        provider = orch.provider
        provider_name_override = args.get("provider")
        if provider_name_override:
            if orch.providers is None or not orch.providers.has(provider_name_override):
                return ToolResult.fail(f"provider '{provider_name_override}' is not registered")
            provider = orch.providers.get(provider_name_override)

        # Child policy engine is its own instance so a child in a stricter mode
        # doesn't mutate the parent's mode. Same config, same denylist.
        child_policy = PolicyEngine(orch.config)
        child_policy.set_mode(orch.policy.mode)

        # Lazy import to avoid the runtime→tools→runtime cycle.
        from reidx.runtime.agent import Agent  # noqa: PLC0415

        child_agent = Agent(
            provider,
            child_tools,
            child_policy,
            base_system_prompt=system_prompt,
            context_extras={},  # child cannot see the orchestrator: no nested spawn
        )

        parent_workspace = ctx.workspace_root
        child_session = Session(
            title=f"subagent:{name}",
            workspace=parent_workspace,
            provider=getattr(provider, "name", "unknown"),
            model=args.get("model") or (orch.state.session.model if orch.state else ""),
            permission_mode=orch.policy.mode,
        )
        child_state = RuntimeState(session=child_session)

        max_steps = int(args.get("max_steps") or MAX_CHILD_STEPS)

        subagents = orch.subagents
        agent_id = subagents.start(name)
        subagents.update(agent_id, last_action=f"provider={provider.name} model={child_session.model or 'default'}")

        try:
            final_text, tool_log = child_agent.run_turn(
                child_state,
                task,
                writable_roots=[r.resolve() for r in orch.config.policy.additional_writable_roots],
                approver=ctx.approver,
                max_steps=max_steps,
            )
        except Exception as exc:  # noqa: BLE001 - errors surface as tool-result failures
            log.exception("subagent '%s' crashed", name)
            subagents.finish(agent_id, status="error", error=str(exc))
            return ToolResult.fail(f"subagent '{name}' crashed: {exc}")

        subagents.update(agent_id, last_action=f"finished after {len(tool_log)} tool call(s)")
        subagents.finish(agent_id, status="done")

        # Return a compact summary; the panel already showed live progress.
        header = f"[subagent:{name}] provider={provider.name} tools_used={len(tool_log)}"
        return ToolResult.ok_(output=f"{header}\n\n{final_text}", subagent_id=agent_id, tools=tool_log)


def register_spawn_agent(reg: ToolRegistry) -> None:
    reg.register(SpawnAgentTool())


# Preserve Message import in case future callers want to inspect state.messages.
__all__ = ["SpawnAgentTool", "register_spawn_agent", "Message"]
