"""OpenAI Chat Completions provider.

Speaks the OpenAI Chat Completions API (POST /v1/chat/completions). The same
wire format is spoken by llama.cpp's server (`/v1/chat/completions`) and by
LM Studio's local server - see `OpenAICompatibleProvider` below for the
local-endpoint variant that skips the auth header when no key is set.

Tool schemas ReidX produces (`ToolBase.schema()`) are already OpenAI-style,
so tool passing is a straight forward.

`base_url` may be either a bare origin (`https://api.openai.com`) or an
already-versioned API root (`https://api.x.ai/v1`,
`https://api.groq.com/openai/v1`). Normalization appends `/v1` only when
needed so catalog entries never hit `/v1/v1/chat/completions`.
"""
from __future__ import annotations

import os
import re
from typing import Any

from reidx.provider._http import MODELS_TIMEOUT_SECONDS, get_json, iter_sse_json, post_json
from reidx.provider.base import BaseProvider, Message, ProviderResponse, ToolCall, Usage
from reidx.provider.context_windows import ingest_models_payload

# Matches an API root that already includes a version segment, so we must
# not append another `/v1`. Covers:
#   .../v1, .../v4, .../v1beta/openai, .../openai/v1
_VERSIONED_API_ROOT = re.compile(
    r"/(?:v\d+[a-z]*(?:/openai)?|openai/v\d+)$",
    re.IGNORECASE,
)


def normalize_openai_base_url(base_url: str, default: str = "https://api.openai.com") -> str:
    """Return the API root used for `/chat/completions` and `/models`.

    Bare origins get `/v1` appended. Already-versioned roots are left alone.
    """
    base = (base_url or default or "").rstrip("/")
    if not base:
        base = default.rstrip("/")
    if _VERSIONED_API_ROOT.search(base):
        return base
    return f"{base}/v1"


class OpenAIProvider(BaseProvider):
    name = "openai"
    DEFAULT_BASE_URL = "https://api.openai.com"
    DEFAULT_MODEL = "gpt-4o-mini"
    MODELS_PATH = "/models"
    supports_streaming = True

    def __init__(
        self,
        api_key: str,
        base_url: str = "",
        default_model: str = "",
    ) -> None:
        self.api_key = api_key
        self.base_url = normalize_openai_base_url(base_url, self.DEFAULT_BASE_URL)
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
        *,
        on_retry: Any | None = None,
    ) -> ProviderResponse:
        model = model or self.default_model
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._to_openai_messages(messages),
        }
        if tools:
            payload["tools"] = tools
        url = f"{self.base_url}/chat/completions"
        body = post_json(url, payload, self._headers(), on_retry=on_retry)
        return self._parse(body, model)

    def chat_stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        *,
        on_text_delta: Any | None = None,
        on_retry: Any | None = None,
    ) -> ProviderResponse:
        """OpenAI-compatible SSE stream (`stream: true`).

        Accumulates content + tool_call argument fragments, invoking
        `on_text_delta` for each content piece so the TUI can paint live.
        """
        model = model or self.default_model
        payload: dict[str, Any] = {
            "model": model,
            "messages": self._to_openai_messages(messages),
            "stream": True,
            # Some hosts (OpenAI) put usage on the final chunk when requested.
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
        url = f"{self.base_url}/chat/completions"

        text_parts: list[str] = []
        # index -> {id, name, arguments}
        tc_acc: dict[int, dict[str, str]] = {}
        finish = "stop"
        usage = Usage()

        def _consume(events) -> None:  # type: ignore[no-untyped-def]
            nonlocal finish, usage
            for event in events:
                if not isinstance(event, dict):
                    continue
                usage_raw = event.get("usage")
                if isinstance(usage_raw, dict):
                    usage = Usage(
                        prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
                        completion_tokens=int(usage_raw.get("completion_tokens") or 0),
                    )
                choices = event.get("choices") or []
                if not choices:
                    continue
                choice = choices[0] or {}
                fr = choice.get("finish_reason")
                if fr:
                    finish = fr
                delta = choice.get("delta") or {}
                piece = delta.get("content")
                if piece:
                    text_parts.append(piece)
                    if on_text_delta is not None:
                        try:
                            on_text_delta(piece)
                        except Exception:  # noqa: BLE001 - UI callback must not kill stream
                            pass
                for tc in delta.get("tool_calls") or []:
                    if not isinstance(tc, dict):
                        continue
                    idx = int(tc.get("index") or 0)
                    slot = tc_acc.setdefault(idx, {"id": "", "name": "", "arguments": ""})
                    if tc.get("id"):
                        slot["id"] = str(tc["id"])
                    fn = tc.get("function") or {}
                    if isinstance(fn, dict):
                        if fn.get("name"):
                            slot["name"] = str(fn["name"])
                        if fn.get("arguments"):
                            slot["arguments"] += str(fn["arguments"])

        from reidx.provider.base import ProviderError

        stream_error: ProviderError | None = None
        try:
            _consume(iter_sse_json(url, payload, self._headers(), on_retry=on_retry))
        except ProviderError as exc:
            msg = str(exc).lower()
            # Retry once only when stream_options is likely unsupported — not on 401/403.
            retryable = (
                "stream_options" in payload
                and not text_parts
                and not tc_acc
                and (
                    "stream_options" in msg
                    or "unknown" in msg
                    or "unsupported" in msg
                    or "unexpected" in msg
                    or (exc.status_code is not None and 400 <= exc.status_code < 500
                        and exc.status_code not in (401, 403, 404, 429))
                )
            )
            if retryable:
                payload.pop("stream_options", None)
                try:
                    _consume(iter_sse_json(url, payload, self._headers(), on_retry=on_retry))
                except ProviderError as exc2:
                    stream_error = exc2
            else:
                stream_error = exc

        tool_calls: list[ToolCall] = []
        for idx in sorted(tc_acc):
            slot = tc_acc[idx]
            args_raw = slot.get("arguments") or "{}"
            try:
                import json as _json

                args = _json.loads(args_raw) if isinstance(args_raw, str) else args_raw
            except ValueError:
                args = {"_raw": args_raw}
            tool_calls.append(
                ToolCall(
                    id=slot.get("id") or f"call-{idx}",
                    name=slot.get("name") or "",
                    arguments=args if isinstance(args, dict) else {"_raw": args},
                )
            )

        # Mid-stream failure with no content → hard error (agent soft-catches).
        if stream_error is not None and not text_parts and not tool_calls:
            raise stream_error

        text = "".join(text_parts)
        # Partial stream then error: surface partial text + error note (don't pretend success).
        if stream_error is not None:
            note = f"\n\n[stream interrupted: {stream_error}]"
            text = (text + note) if text else f"[stream interrupted: {stream_error}]"
            if not tool_calls:
                # Prefer soft error path when nothing useful was produced.
                if not text_parts:
                    raise stream_error

        return ProviderResponse(
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            stop_reason=("error" if stream_error else (finish or "stop")),
        )

    def fetch_models_detailed(self, *, timeout: int = MODELS_TIMEOUT_SECONDS) -> list[dict]:
        """Raw /models entries (used to cache context windows).

        Uses a short timeout so `/model list` cannot freeze the TUI on slow hosts
        (e.g. NVIDIA NIM catalogs).
        """
        url = f"{self.base_url}{self.MODELS_PATH}"
        body = get_json(url, self._headers(), timeout=timeout)
        data = body.get("data", [])
        if not isinstance(data, list):
            return []
        ingest_models_payload(data)
        return [item for item in data if isinstance(item, dict)]

    def fetch_models(self, *, timeout: int = MODELS_TIMEOUT_SECONDS) -> list[str]:
        models: list[str] = []
        for item in self.fetch_models_detailed(timeout=timeout):
            mid = item.get("id", "")
            if mid:
                models.append(mid)
        return sorted(models)


class OpenAICompatibleProvider(OpenAIProvider):
    """OpenAI-compatible endpoints (llama.cpp server, LM Studio, vLLM, etc.).

    Same wire format as OpenAI; only default base_url differs and the API key
    is optional. Auth method is configurable so providers that use x-api-key
    instead of Bearer are handled correctly. Kept as a subclass so `/providers`
    can show the kind separately in the list.
    """

    name = "openai-compatible"
    DEFAULT_BASE_URL = "http://localhost:8080"
    DEFAULT_MODEL = "local"
    MODELS_PATH = "/models"

    def __init__(
        self,
        api_key: str,
        base_url: str = "",
        default_model: str = "",
        auth_method: str = "bearer",
    ) -> None:
        self.auth_method = auth_method
        super().__init__(api_key=api_key, base_url=base_url, default_model=default_model)

    def _headers(self) -> dict[str, str]:
        h = {}
        if not self.api_key:
            return h
        if self.auth_method == "x-api-key":
            h["x-api-key"] = self.api_key
        elif self.auth_method == "none":
            pass
        else:
            h["authorization"] = f"Bearer {self.api_key}"
        return h


def _json_dump(obj) -> str:  # type: ignore[no-untyped-def]
    import json
    return json.dumps(obj, ensure_ascii=False)
