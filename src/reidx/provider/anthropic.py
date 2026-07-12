"""Anthropic Messages API provider.

Speaks the Anthropic Messages API (POST /v1/messages). Compatible with Anthropic
directly and with Anthropic-compatible proxies (e.g. Reidchat). Uses stdlib
urllib for zero extra dependencies.

Env vars consumed:
  ANTHROPIC_API_KEY   - auth key
  ANTHROPIC_BASE_URL  - API base (defaults to https://api.anthropic.com)
  ANTHROPIC_MODEL     - default model name
"""
from __future__ import annotations

import os
from typing import Any

from reidx.diagnostics.logger import get_logger
from reidx.provider._http import get_json, post_json
from reidx.provider.base import BaseProvider, Message, ProviderResponse, ToolCall, Usage

log = get_logger("reidx.provider.anthropic")

DEFAULT_BASE_URL = "https://api.anthropic.com"
API_VERSION = "2023-06-01"
MAX_TOKENS = 4096


class AnthropicProvider(BaseProvider):
    """Real provider that calls the Anthropic Messages API over HTTP."""

    name = "anthropic"

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        default_model: str = "",
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model

    @classmethod
    def from_env(cls) -> AnthropicProvider | None:
        """Build from ANTHROPIC_* env vars. Returns None if no API key is set."""
        key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
        if not key:
            return None
        base = os.environ.get("ANTHROPIC_BASE_URL", DEFAULT_BASE_URL).strip()
        model = os.environ.get("ANTHROPIC_MODEL", "").strip()
        return cls(api_key=key, base_url=base, default_model=model)

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self.api_key,
            "anthropic-version": API_VERSION,
        }

    def _to_anthropic_messages(self, messages: list[Message]) -> tuple[str | None, list[dict]]:
        """Convert ReidX Messages to Anthropic format. Returns (system, messages)."""
        system: str | None = None
        out: list[dict] = []
        for m in messages:
            if m.role == "system":
                system = m.content
                continue
            if m.role == "tool":
                out.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": m.tool_call_id or "",
                        "content": m.content,
                    }],
                })
                continue
            if m.role == "assistant" and m.tool_calls:
                blocks: list[dict] = []
                if m.content:
                    blocks.append({"type": "text", "text": m.content})
                for tc in m.tool_calls:
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.arguments,
                    })
                out.append({"role": "assistant", "content": blocks})
                continue
            out.append({"role": m.role, "content": m.content})
        return system, out

    def _to_anthropic_tools(self, tools: list[dict] | None) -> list[dict]:
        """Convert OpenAI-style tool schemas to Anthropic tool format."""
        if not tools:
            return []
        result: list[dict] = []
        for t in tools:
            fn = t.get("function", t)
            result.append({
                "name": fn.get("name", ""),
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
            })
        return result

    def _parse_response(self, body: dict, model: str) -> ProviderResponse:
        """Parse Anthropic response into ProviderResponse."""
        content_blocks = body.get("content", [])
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in content_blocks:
            btype = block.get("type", "")
            if btype == "text":
                text_parts.append(block.get("text", ""))
            elif btype == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.get("id", ""),
                    name=block.get("name", ""),
                    arguments=block.get("input", {}),
                ))
        usage_raw = body.get("usage", {})
        return ProviderResponse(
            text="\n".join(text_parts),
            tool_calls=tool_calls,
            usage=Usage(
                prompt_tokens=usage_raw.get("input_tokens", 0),
                completion_tokens=usage_raw.get("output_tokens", 0),
            ),
            stop_reason=body.get("stop_reason", "stop"),
        )

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> ProviderResponse:
        model = model or self.default_model or "claude-sonnet-4-20250514"
        system, anthropic_msgs = self._to_anthropic_messages(messages)
        anthropic_tools = self._to_anthropic_tools(tools)

        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": MAX_TOKENS,
            "messages": anthropic_msgs,
        }
        if system:
            payload["system"] = system
        if anthropic_tools:
            payload["tools"] = anthropic_tools

        url = f"{self.base_url}/v1/messages"
        try:
            body = post_json(url, payload, self._headers())
        except RuntimeError:
            log.exception("Anthropic API request failed")
            raise
        return self._parse_response(body, model)

    def fetch_models(self) -> list[str]:
        url = f"{self.base_url}/v1/models"
        try:
            body = get_json(url, self._headers())
        except RuntimeError:
            log.debug("failed to fetch models from %s", url)
            return []
        models: list[str] = []
        for item in body.get("data", []):
            mid = item.get("id", "")
            if mid:
                models.append(mid)
        return sorted(models)
