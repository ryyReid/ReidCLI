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


class BaseProvider(ABC):
    name: str = "base"

    @abstractmethod
    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> ProviderResponse:
        """Run one model turn. Returns text and/or tool calls."""
