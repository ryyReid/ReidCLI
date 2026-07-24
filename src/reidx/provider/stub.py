"""StubProvider: offline provider so the runtime loop is real and testable.

Returns a canned assistant turn. If the user message asks to "list" files or "read"
a file, it emits a matching tool call so the agent loop executes a real tool
end-to-end. This is honest scaffolding — replace by registering a real provider.
"""
from __future__ import annotations

import uuid
from typing import Any

from reidx.provider.base import BaseProvider, Message, ProviderResponse, ToolCall, Usage


class StubProvider(BaseProvider):
    name = "stub"

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        *,
        on_retry: Any | None = None,
    ) -> ProviderResponse:
        last = next((m for m in reversed(messages) if m.role == "user"), None)
        text = (last.content if last else "").lower()
        tool_names = {t.get("function", {}).get("name") for t in tools or []}

        hint = (
            "\n\n_You're on the offline **stub** (no real model). "
            "Run `/connect` or `/use <provider>` — e.g. `/use nvidia` — to chat for real._"
        )

        # After a tool result comes back, finalize the turn instead of looping.
        last_msg = messages[-1] if messages else None
        if last_msg is not None and last_msg.role == "tool":
            return ProviderResponse(
                text=f"Tool returned: {last_msg.content[:200]}{hint}",
                usage=Usage(prompt_tokens=12, completion_tokens=8),
                stop_reason="stop",
            )

        if ("list" in text or "dir" in text) and "list_dir" in tool_names:
            return ProviderResponse(
                text="I'll list the current directory first.",
                tool_calls=[ToolCall(id=uuid.uuid4().hex[:8], name="list_dir", arguments={})],
                usage=Usage(prompt_tokens=8, completion_tokens=6),
                stop_reason="tool_use",
            )
        if "read" in text and "read_file" in tool_names:
            target = "README.md"
            return ProviderResponse(
                text=f"Reading {target}.",
                tool_calls=[
                    ToolCall(id=uuid.uuid4().hex[:8], name="read_file", arguments={"path": target})
                ],
                usage=Usage(prompt_tokens=10, completion_tokens=4),
                stop_reason="tool_use",
            )

        echo = (
            f"Offline stub echo (not a real model): {last.content if last else ''}{hint}"
        )
        return ProviderResponse(
            text=echo,
            usage=Usage(prompt_tokens=max(1, len(echo) // 4), completion_tokens=8),
            stop_reason="stop",
        )
