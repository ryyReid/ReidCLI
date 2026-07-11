"""OpenAI Chat Completions provider.

Speaks the OpenAI Chat Completions API (POST /v1/chat/completions). The same
wire format is spoken by llama.cpp's server (`/v1/chat/completions`) and by
LM Studio's local server — see `OpenAICompatibleProvider` below for the
local-endpoint variant that skips the auth header when no key is set.

Tool schemas ReidX produces (`ToolBase.schema()`) are already OpenAI-style,
so tool passing is a straight forward.
"""
from __future__ import annotations

import os
from typing import Any

from reidx.diagnostics.logger import get_logger
from reidx.provider._http import post_json
from reidx.provider.base import BaseProvider, Message, ProviderResponse, ToolCall, Usage

log = get_logger("reidx.provider.openai")


class OpenAIProvider(BaseProvider):
    name = "openai"
    DEFAULT_BASE_URL = "https://api.openai.com"
    DEFAULT_MODEL = "gpt-4o-mini"

    def __init__(
        self,
        api_key: str,
        base_url: str = "",
        default_model: str = "",
    ) -> None:
        self.api_key = api_key
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.default_model = default_model or self.DEFAULT_MODEL

    @classmethod
    def from_env(cls) -> OpenAIProvider | None:
        """Build from OPENAI_* env vars. Returns None if no API key is set, so
        it can be auto-registered under `openai` alongside anthropic."""
        key = os.environ.get("OPENAI_API_KEY", "").strip()
        if not key:
            return None
        base = os.environ.get("OPENAI_BASE_URL", "").strip()
        model = os.environ.get("OPENAI_MODEL", "").strip()
        return cls(api_key=key, base_url=base, default_model=model)

    def _to_openai_messages(self, messages: list[Message]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m.role == "tool":
                out.append({
                    "role": "tool",
                    "tool_call_id": m.tool_call_id or "",
                    "content": m.content,
                })
                continue
            if m.role == "assistant" and m.tool_calls:
                out.append({
                    "role": "assistant",
                    "content": m.content or None,
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": _json_dump(tc.arguments)},
                        }
                        for tc in m.tool_calls
                    ],
                })
                continue
            out.append({"role": m.role, "content": m.content})
        return out

    def _headers(self) -> dict[str, str]:
        h = {}
        if self.api_key:
            h["authorization"] = f"Bearer {self.api_key}"
        return h

    def _parse(self, body: dict, model: str) -> ProviderResponse:
        choices = body.get("choices") or [{}]
        msg = (choices[0] or {}).get("message", {})
        text = msg.get("content") or ""
        tool_calls: list[ToolCall] = []
        for tc in msg.get("tool_calls") or []:
            fn = tc.get("function", {})
            args_raw = fn.get("arguments") or "{}"
            try:
                import json as _json
                args = _json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except ValueError:
                args = {"_raw": args_raw}
            tool_calls.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args or {}))
        usage_raw = body.get("usage", {})
        return ProviderResponse(
            text=text,
            tool_calls=tool_calls,
            usage=Usage(
                prompt_tokens=usage_raw.get("prompt_tokens", 0),
                completion_tokens=usage_raw.get("completion_tokens", 0),
            ),
            stop_reason=(choices[0] or {}).get("finish_reason", "stop"),
        )

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> ProviderResponse:
        model = model or self.default_model
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._to_openai_messages(messages),
        }
        if tools:
            payload["tools"] = tools
        url = f"{self.base_url}/v1/chat/completions"
        try:
            body = post_json(url, payload, self._headers())
        except RuntimeError:
            log.exception("OpenAI-compatible request failed")
            raise
        return self._parse(body, model)


class OpenAICompatibleProvider(OpenAIProvider):
    """OpenAI-compatible endpoints (llama.cpp server, LM Studio, vLLM, etc.).

    Same wire format as OpenAI; only default base_url differs and the API key
    is optional. Kept as a subclass so `/providers` can show the kind
    separately in the list.
    """

    name = "openai-compatible"
    DEFAULT_BASE_URL = "http://localhost:8080"
    DEFAULT_MODEL = "local"


def _json_dump(obj) -> str:  # type: ignore[no-untyped-def]
    import json
    return json.dumps(obj, ensure_ascii=False)
