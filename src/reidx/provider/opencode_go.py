"""OpenCode Zen router provider.

OpenCode's Zen subscription exposes two different wire protocols behind a
single key and origin:

  - OpenAI-compatible chat/completions — GLM, Kimi, DeepSeek, MiMo
    (`.../zen/v1/chat/completions`)
  - Anthropic Messages — MiniMax, Qwen
    (`.../zen/v1/messages`)

Rather than make the user pick the right one of two near-identical catalog
rows, this provider presents as a single backend and routes each request to
the correct sub-client based on the requested model id. The same key is used
for both (Bearer on the OpenAI path, x-api-key on the Anthropic path).
"""
from __future__ import annotations

import re
from typing import Any

from reidx.provider._http import MODELS_TIMEOUT_SECONDS
from reidx.provider.anthropic import AnthropicProvider
from reidx.provider.base import BaseProvider, Message, ProviderResponse
from reidx.provider.openai import OpenAICompatibleProvider

DEFAULT_BASE_URL = "https://opencode.ai/zen/v1"
DEFAULT_MODEL = "glm-5.2"

# Model-id fragments served by the Anthropic Messages endpoint. Everything
# else goes to the OpenAI-compatible endpoint.
_ANTHROPIC_HINTS = ("qwen", "minimax")


def _is_anthropic_model(model: str) -> bool:
    m = (model or "").lower()
    return any(hint in m for hint in _ANTHROPIC_HINTS)


def _anthropic_base(oai_base: str) -> str:
    """Derive the Anthropic origin from the OpenAI-compatible base URL.

    `AnthropicProvider` appends `/v1/messages`, so we strip a trailing `/v1`
    from the OpenAI root: `.../zen/v1` → `.../zen`.
    """
    base = (oai_base or DEFAULT_BASE_URL).rstrip("/")
    return re.sub(r"/v1$", "", base) or base


class OpenCodeGoProvider(BaseProvider):
    """Single entry that routes to OpenAI-compatible or Anthropic sub-clients."""

    name = "opencode-go"
    supports_streaming = True

    def __init__(
        self,
        api_key: str,
        base_url: str = "",
        default_model: str = "",
    ) -> None:
        self.api_key = api_key
        self.default_model = default_model or DEFAULT_MODEL
        oai_base = base_url or DEFAULT_BASE_URL
        self._openai = OpenAICompatibleProvider(
            api_key=api_key,
            base_url=oai_base,
            default_model=self.default_model,
            auth_method="bearer",
        )
        self._anthropic = AnthropicProvider(
            api_key=api_key,
            base_url=_anthropic_base(oai_base),
            default_model=self.default_model,
        )

    def _route(self, model: str | None) -> BaseProvider:
        model = model or self.default_model
        return self._anthropic if _is_anthropic_model(model) else self._openai

    def chat(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        *,
        on_retry: Any | None = None,
    ) -> ProviderResponse:
        model = model or self.default_model
        return self._route(model).chat(messages, tools, model, on_retry=on_retry)

    def chat_stream(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        *,
        on_text_delta: Any | None = None,
        on_retry: Any | None = None,
    ) -> ProviderResponse:
        model = model or self.default_model
        return self._route(model).chat_stream(
            messages, tools, model, on_text_delta=on_text_delta, on_retry=on_retry
        )

    def fetch_models(self, *, timeout: int = MODELS_TIMEOUT_SECONDS) -> list[str]:
        # Primary (OpenAI) errors propagate so a bad Zen key surfaces as a 401
        # during /connect validation. The Anthropic listing is best-effort —
        # OpenCode Go may not expose /v1/models on that path.
        models: list[str] = list(self._openai.fetch_models(timeout=timeout))
        try:
            models.extend(self._anthropic.fetch_models(timeout=timeout))
        except Exception:  # noqa: BLE001 - secondary listing is optional
            pass
        seen: set[str] = set()
        out: list[str] = []
        for m in models:
            if m not in seen:
                seen.add(m)
                out.append(m)
        return out
