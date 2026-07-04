"""Provider registry: config-driven registration of providers by name.

The built-in `stub` provider is always registered and remains the default.
Real providers (Anthropic/OpenAI/OpenAI-compatible/Ollama) are added by
`/connect` (see reidcli.provider.store) or by env-var auto-registration for
Anthropic, but never auto-promoted to default — the user picks with `/use`.
"""
from __future__ import annotations

from reidcli.config.models import Config
from reidcli.diagnostics.logger import get_logger
from reidcli.provider.anthropic import AnthropicProvider
from reidcli.provider.base import BaseProvider
from reidcli.provider.stub import StubProvider

log = get_logger("reidcli.provider")


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, BaseProvider] = {}

    def register(self, name: str, provider: BaseProvider) -> None:
        self._providers[name] = provider
        log.debug("registered provider: %s", name)

    def unregister(self, name: str) -> bool:
        if name in self._providers:
            del self._providers[name]
            return True
        return False

    def get(self, name: str) -> BaseProvider:
        if name not in self._providers:
            raise KeyError(f"provider '{name}' not registered")
        return self._providers[name]

    def has(self, name: str) -> bool:
        return name in self._providers

    def names(self) -> list[str]:
        return list(self._providers)


def default_registry(config: Config) -> ProviderRegistry:
    """Build the default registry. Stub is always registered and stays the
    default; Anthropic and OpenAI auto-register under their own names when the
    matching env vars are set (available via `/use`), but never override the
    default. Every other configured provider is built from its `kind` so a
    subagent can request it by name. Providers persisted by `/connect` are
    layered on top by `reidcli.provider.store.load_into`.
    """
    # Lazy import: store.py imports ProviderRegistry from this module, so a
    # top-level import here would be circular.
    from reidcli.provider.store import ProviderRecord, build_provider

    reg = ProviderRegistry()
    reg.register("stub", StubProvider())

    anthropic = AnthropicProvider.from_env()
    if anthropic is not None:
        reg.register("anthropic", anthropic)
        log.debug("auto-registered anthropic provider from env vars")

    from reidcli.provider.openai import OpenAIProvider  # noqa: PLC0415
    openai = OpenAIProvider.from_env()
    if openai is not None:
        reg.register("openai", openai)
        log.debug("auto-registered openai provider from env vars")

    for name, pc in config.providers.items():
        if name == "stub" or reg.has(name):
            continue
        kind = (pc.kind or name).strip()
        try:
            provider = build_provider(ProviderRecord(
                name=name,
                kind=kind,
                base_url=pc.base_url or "",
                api_key=pc.api_key.get_secret_value() if pc.api_key else "",
                default_model=pc.default_model,
            ))
        except (ValueError, TypeError):
            log.warning("provider '%s' (kind=%s) configured but could not be built; skipping", name, kind)
            continue
        reg.register(name, provider)
        log.debug("registered configured provider: %s (kind=%s)", name, kind)
    return reg
