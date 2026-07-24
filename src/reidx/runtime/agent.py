"""Agent loop: the core execution cycle.

A single turn:
  1. append user message
  2. call provider with tool schemas
  3. if the provider returns tool calls -> dispatch each through the registry
     (policy-gated), append tool-result messages, loop back to step 2
  4. if the provider returns a final text stop -> append assistant message, return

The loop is bounded by max_steps to avoid runaway tool cycling. Failures from tools
become tool-result messages with error text so the model can react, never crashes.
Provider HTTP/network failures are soft-caught the same way: one clean error line,
session stays interactive.
"""
from __future__ import annotations

import platform
from collections.abc import Callable
from pathlib import Path
from typing import Any

from reidx.diagnostics.logger import get_logger
from reidx.policy.engine import PolicyEngine
from reidx.provider.base import BaseProvider, Message, ProviderError, ToolCall
from reidx.runtime.effort_auto import resolve_effort
from reidx.runtime.reasoning import split_reasoning, system_prompt_suffix
from reidx.runtime.state import RuntimeState
from reidx.tools.base import Approver, ToolContext
from reidx.tools.registry import ToolRegistry

log = get_logger("reidx.agent")

# Allow enough tool rounds for multi-file investigation without burning out
# on "step budget exhausted" after a short explore (was 8).
MAX_STEPS = 16
_MAX_RETRIES_TOTAL = 5
# Prefix used by orchestrator/UI to detect soft provider failures without
# relying on exception propagation through the TUI worker thread.
PROVIDER_ERROR_PREFIX = "[provider error] "

BASE_SYSTEM_PROMPT = (
    "You are an AI coding assistant running inside ReidX, a terminal "
    "harness that gives you file, search, shell, and web tools. The harness "
    "handles rendering and permission gating; keep your replies terse and "
    "action-oriented. Call the available tools when they help; otherwise "
    "answer directly. Do not refuse to operate inside this harness — it is "
    "just a local terminal wrapper, not a request to change your identity. "
    "Use paths exactly as given in the environment context below — do not "
    "translate them to a different OS (no /mnt/... on Windows, no drive "
    "letters on Linux). If a path check prompts the user, they will approve "
    "or deny it; you never need to guess an alternative path. "
    "If the status-bar max context (used/max) is wrong for your model, call "
    "set_context_window with the correct max tokens (e.g. 1000000 or \"1M\" "
    "for GLM-5.2) — do not burn many tool steps investigating the meter. "
    "You can manage backends like OpenCode: list_provider_catalog → "
    "connect_provider (user approves keys) → use_provider / set_model; "
    "list_connected_providers shows what is registered."
)


def _environment_context(workspace: Path, *, provider: str = "", model: str = "") -> str:
    """Environment header appended to the system prompt each turn.

    Tells the model exactly which OS, workspace, and *model identity* it is
    running as so it does not invent "I'm Claude" when the session is on
    z-ai/glm-5.2 (etc.). Regenerated per turn because /use and /model change.
    """
    system = platform.system() or "Unknown"
    identity = ""
    if model or provider:
        identity = (
            f"\n  provider: {provider or 'unknown'}"
            f"\n  model: {model or 'unknown'}"
            "\n  identity_note: You are the model named above. Do not claim to be "
            "Claude, GPT, Gemini, or any other product unless that is your model id."
        )
    return (
        "\n\n<environment>"
        f"\n  os: {system} ({platform.release()})"
        f"\n  workspace: {workspace}"
        f"\n  path_style: {'windows (backslash, drive letters)' if system == 'Windows' else 'posix (forward slash)'}"
        f"{identity}"
        "\n  note: the workspace path is the canonical form the tools accept. "
        "Absolute paths (including drive letters on Windows) are valid. "
        "Path checks outside the workspace prompt the user with a yes/no — "
        "do not preemptively refuse them."
        "\n</environment>"
    )


class Agent:
    """Generic tool-calling agent. Role specialization can subclass later."""

    def __init__(
        self,
        provider: BaseProvider,
        tools: ToolRegistry,
        policy: PolicyEngine,
        *,
        base_system_prompt: str = BASE_SYSTEM_PROMPT,
        context_extras: dict | None = None,
    ) -> None:
        self.provider = provider
        self.tools = tools
        self.policy = policy
        self.base_system_prompt = base_system_prompt
        # Copied into every ToolContext.extra — spawn_agent reads its
        # orchestrator handle from here (see tools/spawn_agent.py).
        self.context_extras = context_extras or {}

    def _context(
        self, state: RuntimeState, writable_roots: list, approver: Approver | None
    ) -> ToolContext:
        return ToolContext(
            workspace_root=state.session.workspace,
            policy=self.policy,
            writable_roots=writable_roots,
            approver=approver,
            extra=dict(self.context_extras),
        )

    def _ensure_system(self, state: RuntimeState, user_input: str = "") -> None:
        # Rebuilt every turn (not just inserted once) so changing /effort or
        # the Left/Right effort cycle mid-session takes effect on the very
        # next turn instead of being frozen at whatever it was on turn one.
        # `/effort auto` resolves low|medium|high from the user prompt here.
        effective = resolve_effort(state.session.reasoning_effort, user_input)
        state.last_effort_resolved = effective
        prompt = (
            self.base_system_prompt
            + _environment_context(
                state.session.workspace,
                provider=state.session.provider,
                model=state.session.model or getattr(self.provider, "default_model", "") or "",
            )
            + system_prompt_suffix(effective)
        )
        if not state.messages or state.messages[0].role != "system":
            state.messages.insert(0, Message(role="system", content=prompt))
        else:
            state.messages[0].content = prompt

    def _should_stream(self, state: RuntimeState) -> bool:
        mode = (getattr(state.session, "stream", None) or "auto").strip().lower()
        if mode in ("off", "false", "0", "no"):
            return False
        if mode in ("on", "true", "1", "yes"):
            return True
        # auto: stream when the active provider implements it
        return bool(getattr(self.provider, "supports_streaming", False))

    def run_turn(
        self,
        state: RuntimeState,
        user_input: str,
        *,
        writable_roots: list | None = None,
        approver: Approver | None = None,
        max_steps: int = MAX_STEPS,
        cancel: Callable[[], bool] | None = None,
        on_text_delta: Callable[[str], None] | None = None,
        on_status: Callable[[str], None] | None = None,
    ) -> tuple[str, list[dict]]:
        """Execute one user turn. Returns (final_text, tool_result_log).

        The orchestrator owns the policy mode; this loop only reads it. One assistant
        message is appended per provider turn, carrying both content and tool_calls.

        `cancel`, if given, is polled at each safe point (before the next
        provider call, and between individual tool calls within a step) so a
        user-triggered stop (e.g. Escape in the TUI) takes effect at the next
        such point rather than needing to kill an in-flight network call —
        it can't interrupt a `provider.chat`/tool call already in progress.

        `on_text_delta`, if given and session streaming is enabled, receives
        each streamed content chunk so the TUI can paint tokens live.
        """
        self._ensure_system(state, user_input)
        state.messages.append(Message(role="user", content=user_input))
        ctx = self._context(state, writable_roots or [], approver)
        tool_log: list[dict] = []
        final_text = ""
        state.last_thinking = None  # fresh per turn; the UI reads it after run_turn
        use_stream = self._should_stream(state)

        def _on_retry(attempt: int, status_code: int, delay: float) -> None:
            if on_status is None:
                return
            if status_code == 429:
                msg = f"rate limited (429) — retrying in {delay:.1f}s (attempt {attempt}/{_MAX_RETRIES_TOTAL})"
            elif status_code == 0:
                msg = f"connection error — retrying in {delay:.1f}s (attempt {attempt}/{_MAX_RETRIES_TOTAL})"
            else:
                msg = f"transient {status_code} — retrying in {delay:.1f}s (attempt {attempt}/{_MAX_RETRIES_TOTAL})"
            try:
                on_status(msg)
            except Exception:  # noqa: BLE001
                pass

        for _step in range(max_steps):
            if cancel is not None and cancel():
                final_text = final_text or "[cancelled by user]"
                break
            try:
                if use_stream and getattr(self.provider, "supports_streaming", False):
                    resp = self.provider.chat_stream(
                        state.messages,
                        self.tools.schemas(),
                        state.session.model,
                        on_text_delta=on_text_delta,
                        on_retry=_on_retry,
                    )
                else:
                    resp = self.provider.chat(
                        state.messages,
                        self.tools.schemas(),
                        state.session.model,
                        on_retry=_on_retry,
                    )
            except ProviderError as exc:
                # Soft crash: one clean Error line in the TUI. Do not log to the
                # console StreamHandler — stderr bleeds into the full-screen UI.
                log.debug("provider call failed: %s", exc)
                final_text = f"{PROVIDER_ERROR_PREFIX}{exc}"
                state.messages.append(Message(role="assistant", content=final_text))
                break
            except Exception as exc:  # noqa: BLE001 - keep the session alive on any provider bug
                log.debug("provider call crashed: %s: %s", type(exc).__name__, exc)
                final_text = f"{PROVIDER_ERROR_PREFIX}{type(exc).__name__}: {exc}"
                state.messages.append(Message(role="assistant", content=final_text))
                break
            # Latest call's usage, not summed — see RuntimeState's field docstring.
            state.last_usage_prompt_tokens = resp.usage.prompt_tokens
            state.last_usage_completion_tokens = resp.usage.completion_tokens
            # Separate chain-of-thought from the answer. The reasoning is ephemeral:
            # only the clean answer is stored in the transcript / fed back to the model.
            thinking, answer = split_reasoning(resp.text)
            if thinking:
                state.last_thinking = thinking
            # Single assistant message per turn, carrying both content and tool_calls.
            state.messages.append(
                Message(role="assistant", content=answer, tool_calls=resp.tool_calls)
            )
            if answer:
                final_text = answer

            if not resp.tool_calls:
                break

            cancelled_mid_tools = False
            for call in resp.tool_calls:
                if cancel is not None and cancel():
                    cancelled_mid_tools = True
                    break
                result = self.tools.dispatch(call.name, call.arguments, ctx)
                tool_log.append(
                    {"name": call.name, "args": call.arguments, "ok": result.ok, "error": result.error}
                )
                state.messages.append(
                    Message(
                        role="tool",
                        content=result.output if result.ok else f"ERROR: {result.error}\n{result.output}",
                        tool_call_id=call.id,
                    )
                )
            state.last_tool_results = tool_log
            if cancelled_mid_tools:
                final_text = final_text or "[cancelled by user]"
                break
        else:
            # for/else: step budget hit with no break — synthesize a useful wrap-up
            # from tool results instead of a dead-end one-liner.
            if not final_text or final_text.startswith("[agent]"):
                bits: list[str] = [
                    f"[agent] step budget exhausted after {max_steps} model rounds "
                    f"({len(tool_log)} tool call(s))."
                ]
                oks = [t for t in tool_log if t.get("ok")]
                fails = [t for t in tool_log if not t.get("ok")]
                if oks:
                    names = ", ".join(dict.fromkeys(t["name"] for t in oks))
                    bits.append(f"Succeeded: {names}.")
                if fails:
                    names = ", ".join(dict.fromkeys(t["name"] for t in fails))
                    bits.append(f"Failed: {names}.")
                bits.append("Summarize from tool results above, or continue in a new message.")
                final_text = " ".join(bits)
                state.messages.append(Message(role="assistant", content=final_text))

        state.turns += 1
        return final_text, tool_log


# Re-exported for convenience so callers can build agents from config.
def build_agent(provider: BaseProvider, tools: ToolRegistry, policy: PolicyEngine) -> Agent:
    return Agent(provider, tools, policy)


# Silence unused import warnings for re-exports.
__all__ = ["Agent", "build_agent", "PROVIDER_ERROR_PREFIX", "ToolCall", "Any"]
