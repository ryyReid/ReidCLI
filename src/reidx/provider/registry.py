"""Provider registry: config-driven registration of providers by name.

The built-in `stub` provider is always registered and remains the default.
Real providers (Anthropic/OpenAI/OpenAI-compatible/Ollama) are added by
`/connect` (see reidx.provider.store) or by env-var auto-registration for
Anthropic, but never auto-promoted to default — the user picks with `/use`.

`/use nvidia` resolves catalog aliases and display names like "NVIDIA NIM"
via `resolve()` so users don't need the exact stored string.
"""
from __future__ import annotations

from reidx.config.models import Config
from reidx.diagnostics.logger import get_logger
from reidx.provider.anthropic import AnthropicProvider
from reidx.provider.base import BaseProvider
from reidx.provider.stub import StubProvider

log = get_logger("reidx.provider")


def _norm_key(name: str) -> str:
    """Alphanumeric-only lowercase key: 'NVIDIA NIM' / 'nvidia-nim' → 'nvidianim'."""
    return "".join(c for c in (name or "").lower() if c.isalnum())


class ProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, BaseProvider] = {}
        # Extra lookup keys (aliases, catalog ids) → canonical registered name.
        self._aliases: dict[str, str] = {}

    def register(self, name: str, provider: BaseProvider, *, aliases: list[str] | None = None) -> None:
        self._providers[name] = provider
        # Self-alias under normalized form for case/spacing-insensitive lookup.
        self._aliases[_norm_key(name)] = name
        self._aliases[name.lower()] = name
        for alias in aliases or []:
            if not alias:
                continue
            self._aliases[_norm_key(alias)] = name
            self._aliases[alias.lower()] = name
        log.debug("registered provider: %s aliases=%s", name, aliases or [])

    def unregister(self, name: str) -> bool:
        if name not in self._providers:
            # Maybe they passed an alias
            resolved = self.resolve(name)
            if resolved is None:
                return False
            name = resolved
        del self._providers[name]
        self._aliases = {k: v for k, v in self._aliases.items() if v != name}
        return True

    def resolve(self, name: str) -> str | None:
        """Map a user-typed name/alias to the canonical registered name.

        Accepts exact ids, case-insensitive names, normalized forms
        (`nvidianim` for `NVIDIA NIM`), and catalog aliases (`nvidia`).
        """
        if not name or not name.strip():
            return None
        raw = name.strip()
        if raw in self._providers:
            return raw
        low = raw.lower()
        if low in self._aliases:
            return self._aliases[low]
        nk = _norm_key(raw)
        if nk in self._aliases:
            return self._aliases[nk]
        # Unique prefix of registered names (e.g. "nvid" → "NVIDIA NIM")
        matches = [n for n in self._providers if n.lower().startswith(low) or _norm_key(n).startswith(nk)]
        if len(matches) == 1:
            return matches[0]
        return None

    def get(self, name: str) -> BaseProvider:
        resolved = self.resolve(name)
        if resolved is None:
            known = ", ".join(self.names()) or "(none)"
            raise KeyError(f"provider '{name}' is not registered (have: {known})")
        return self._providers[resolved]

    def has(self, name: str) -> bool:
        return self.resolve(name) is not None

    def names(self) -> list[str]:
        return list(self._providers)

    def suggestions(self, query: str, *, limit: int = 5) -> list[str]:
        """Near-matches for error messages when resolve fails."""
        q = _norm_key(query)
        scored: list[tuple[int, str]] = []
        for name in self._providers:
            nn = _norm_key(name)
            if q and q in nn:
                scored.append((0, name))
            elif nn and q and (nn.startswith(q[:3]) if len(q) >= 3 else False):
                scored.append((1, name))
            else:
                scored.append((2, name))
        scored.sort(key=lambda t: (t[0], t[1].lower()))
        return [n for _, n in scored[:limit]]


def default_registry(config: Config) -> ProviderRegistry:
    """Build the default registry. Stub is always registered as a last-resort
    offline fallback. Anthropic/OpenAI auto-register when env vars are set;
    persisted `/connect` providers are layered on by `load_into` / `load_from_database`.
    """
    # Lazy import: store.py imports ProviderRegistry from this module, so a
    # top-level import here would be circular.
    from reidx.provider.store import ProviderRecord, build_provider

    reg = ProviderRegistry()
    reg.register("stub", StubProvider())

    anthropic = AnthropicProvider.from_env()
    if anthropic is not None:
        reg.register("anthropic", anthropic)
        log.debug("auto-registered anthropic provider from env vars")

    from reidx.provider.openai import OpenAIProvider  # noqa: PLC0415
    openai = OpenAIProvider.from_env()
    if openai is not None:
        reg.register("openai", openai)
        log.debug("auto-registered openai provider from env vars")

    for name, pc in config.providers.items():
        if name == "stub" or name in reg.names():
            continue
        kind = (pc.kind or "").strip()
        # Display-only entries (e.g. only default_model remembered for "NVIDIA NIM")
        # must NOT build a fake client — that used to default to localhost:8080 and
        # block load_from_database from registering the real /connect record.
        if kind not in ("anthropic", "openai", "openai-compatible", "ollama"):
            log.debug(
                "skipping settings provider '%s' (kind=%r) — model prefs only; "
                "real endpoint comes from providers.db",
                name,
                kind or name,
            )
            continue
        base = (pc.base_url or "").strip()
        api_key = pc.api_key.get_secret_value() if pc.api_key else ""
        if kind == "openai-compatible" and not base:
            log.debug("skipping incomplete openai-compatible provider '%s' (no base_url)", name)
            continue
        if kind in ("anthropic", "openai") and not api_key:
            log.debug("skipping incomplete provider '%s' (no api_key in settings)", name)
            continue
        try:
            provider = build_provider(ProviderRecord(
                name=name,
                kind=kind,
                base_url=base,
                api_key=api_key,
                default_model=pc.default_model,
                auth_method=pc.auth_method or "bearer",
            ))
        except (ValueError, TypeError):
            log.warning("provider '%s' (kind=%s) configured but could not be built; skipping", name, kind)
            continue
        reg.register(name, provider)
        log.debug("registered configured provider: %s (kind=%s)", name, kind)
    return reg


def pick_startup_provider(registry: ProviderRegistry, preferred: str = "") -> str:
    """Choose the provider to activate on launch.

    Preference order:
      1. `preferred` (settings default_provider) if real and registered
      2. Known cloud/local providers (anthropic, openai, ollama, …)
      3. Any non-stub registered provider (e.g. "NVIDIA NIM" from /connect)
      4. stub (offline only)

    Avoids stranding users on stub-v0 when they already connected a real backend.
    """
    preferred = (preferred or "").strip()
    if preferred and preferred != "stub":
        resolved = registry.resolve(preferred)
        if resolved and resolved != "stub":
            return resolved

    priority = (
        "anthropic",
        "openai",
        "ollama",
        "openrouter",
        "NVIDIA NIM",
        "nvidia",
        "nvidia-nim",
    )
    for name in priority:
        resolved = registry.resolve(name)
        if resolved and resolved != "stub":
            return resolved

    for name in registry.names():
        if name != "stub":
            return name
    return "stub" if registry.has("stub") else (registry.names()[0] if registry.names() else "stub")
