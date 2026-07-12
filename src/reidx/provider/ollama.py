"""Ollama native chat provider (POST /api/chat).

Ollama also exposes an OpenAI-compatible endpoint at /v1, but the native
endpoint is what `ollama serve` recommends and has cleaner tool-call payloads
in current versions. No API key required.
"""
from __future__ import annotations

from typing import Any

from reidx.diagnostics.logger import get_logger
from reidx.provider._http import get_json, post_json
from reidx.provider.base import BaseProvider, Message, ProviderResponse, ToolCall, Usage

log = get_logger("reidx.provider.ollama")


class OllamaProvider(BaseProvider):
    name = "ollama"
    DEFAULT_BASE_URL = "http://localhost:11434"
    DEFAULT_MODEL = "llama3.2"

    def __init__(self, base_url: str = "", default_model: str = "", api_key: str = "") -> None:
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        self.default_model = default_model or self.DEFAULT_MODEL
        self.api_key = api_key  # unused; kept for uniform ProviderRecord shape

    def _to_ollama_tools(self, tools: list[dict] | None) -> list[dict]:
        if not tools:
            return []
        out: list[dict] = []
        for t in tools:
            fn = t.get("function", t)
            out.append({
                "type": "function",
                "function": {
                    "name": fn.get("name", ""),
                    "description": fn.get("description", ""),
                    "parameters": fn.get("parameters", {"type": "object", "properties": {}}),
                },
            })
        return out

    def _to_ollama_messages(self, messages: list[Message]) -> list[dict]:
        out: list[dict] = []
        for m in messages:
            if m.role == "tool":
                out.append({"role": "tool", "content": m.content})
                continue
            if m.role == "assistant" and m.tool_calls:
                out.append({
                    "role": "assistant",
                    "content": m.content or "",
                    "tool_calls": [
                        {"function": {"name": tc.name, "arguments": tc.arguments}}
                        for tc in m.tool_calls
                    ],
                })
                continue
            out.append({"role": m.role, "content": m.content})
        return out

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> ProviderResponse:
        model = model or self.default_model
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._to_ollama_messages(messages),
            "stream": False,
        }
        ol_tools = self._to_ollama_tools(tools)
        if ol_tools:
            payload["tools"] = ol_tools
        try:
            body = post_json(f"{self.base_url}/api/chat", payload, {})
        except RuntimeError:
            log.exception("Ollama request failed")
            raise

        msg = body.get("message", {}) or {}
        text = msg.get("content") or ""
        tool_calls: list[ToolCall] = []
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            args = fn.get("arguments") or {}
            if isinstance(args, str):
                try:
                    import json as _json
                    args = _json.loads(args)
                except ValueError:
                    args = {"_raw": args}
            tool_calls.append(ToolCall(
                id=tc.get("id") or f"ollama-{i}",
                name=fn.get("name", ""),
                arguments=args or {},
            ))
        return ProviderResponse(
            text=text,
            tool_calls=tool_calls,
            usage=Usage(
                prompt_tokens=body.get("prompt_eval_count", 0),
                completion_tokens=body.get("eval_count", 0),
            ),
            stop_reason=body.get("done_reason", "stop"),
        )

    def fetch_models(self) -> list[str]:
        try:
            body = get_json(f"{self.base_url}/api/tags", {})
        except RuntimeError:
            log.debug("failed to fetch models from ollama")
            return []
        models: list[str] = []
        for item in body.get("models", []):
            name = item.get("name", "")
            if name:
                models.append(name)
        return sorted(models)
