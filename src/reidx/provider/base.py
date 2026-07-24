"""Provider layer: abstract model access from runtime.

Defines the message/response contract and BaseProvider. Real providers
(OpenAI/Anthropic/local) plug in here. A StubProvider is included so the runtime
loop is exercisable end-to-end without API keys.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    role: str  # "system" | "user" | "assistant" | "tool"
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None  # set when role == "tool" (result)


class Usage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ProviderResponse(BaseModel):
    text: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: Usage = Field(default_factory=Usage)
    stop_reason: str = "stop"


class ProviderError(RuntimeError):
    """Raised when a model provider request fails.

    Soft-caught by the agent loop so a bad key, 404 model, or network blip
    becomes an inline error message instead of crashing the TUI session.
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class BaseProvider(ABC):
    name: str = "base"

    # When True, Agent may call chat_stream() for token-by-token TUI updates.
    supports_streaming: bool = False

    @abstractmethod
    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> ProviderResponse:
        """Run one model turn. Returns text and/or tool calls."""

    def chat_stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        *,
        on_text_delta: Any | None = None,
        on_retry: Any | None = None,
    ) -> ProviderResponse:
        """Stream a model turn; call `on_text_delta(str)` for each content chunk.

        Default implementation falls back to non-streaming `chat()` and emits
        the full text once. Providers that set `supports_streaming = True`
        should override with real SSE streaming.
        """
        resp = self.chat(messages, tools, model, on_retry=on_retry)
        if on_text_delta is not None and resp.text:
            on_text_delta(resp.text)
        return resp

    def fetch_models(self) -> list[str]:
        return []
