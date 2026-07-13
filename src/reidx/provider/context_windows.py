"""Context-window size for the status bar / auto-compact.

Resolution order (first hit wins):

  1. Live cache from the provider's `/models` payload (`context_length`,
     `max_model_len`, nested `meta`, etc.)
  2. Well-known model-id fragment table (most APIs omit context on list)
  3. Session-stored window (set when we last resolved for that model)
  4. Size tags embedded in the model id only (`…[1m]`, `…-32k…`)
  5. DEFAULT_CONTEXT_WINDOW (128k) as a last-resort meter scale

Call `refresh_context_from_provider(provider, model_id)` on `/use` and
`/model` so the cache stays current. Live API values always beat the table.
"""
from __future__ import annotations

import re
from typing import Any

# Only used when the provider tells us nothing and the id has no size hint.
DEFAULT_CONTEXT_WINDOW = 128_000

# model_id (normalized) -> tokens discovered from APIs this process
_LIVE: dict[str, int] = {}

# Keys we recognize anywhere in a model object (top-level or nested).
# Prefer longer / more specific names first in extract (order matters there).
_CTX_KEY_FRAGMENTS = (
    "context_length",
    "context_window",
    "context_size",
    "context_tokens",
    "max_model_len",
    "max_sequence_length",
    "max_input_tokens",
    "max_position_embeddings",
    "input_token_limit",
    "input_tokens_limit",
    "n_ctx_train",
    "n_ctx",
    "num_ctx",
    "n_positions",
    "max_context_length",
    "max_context",
)

# Nested bags common on OpenRouter / vLLM / Ollama / NIM-style payloads.
_NEST_KEYS = (
    "top_provider",
    "meta",
    "metadata",
    "architecture",
    "limits",
    "parameters",
    "config",
    "model_info",
    "info",
    "details",
    "capabilities",
    "settings",
)

# ---------------------------------------------------------------------------
# Well-known windows — used when /models returns id-only entries (OpenAI,
# Anthropic, many NIM hosts). Longest fragment match wins. Keep specific
# ids above short family prefixes (sorted by length at import).
# Approximate published limits; live API always overrides.
# ---------------------------------------------------------------------------
_KNOWN_WINDOWS: list[tuple[str, int]] = [
    # OpenAI
    ("gpt-4.1-nano", 1_047_576),
    ("gpt-4.1-mini", 1_047_576),
    ("gpt-4.1", 1_047_576),
    ("gpt-4o-mini", 128_000),
    ("gpt-4o", 128_000),
    ("gpt-4-turbo", 128_000),
    ("gpt-4-32k", 32_768),
    ("gpt-4", 8_192),
    ("gpt-3.5-turbo-16k", 16_384),
    ("gpt-3.5-turbo", 16_384),
    ("gpt-5-mini", 400_000),
    ("gpt-5-nano", 400_000),
    ("gpt-5", 400_000),
    ("o4-mini", 200_000),
    ("o3-mini", 200_000),
    ("o3-pro", 200_000),
    ("o3", 200_000),
    ("o1-mini", 128_000),
    ("o1-pro", 200_000),
    ("o1", 200_000),
    # Anthropic (standard 200k; 1M is beta / special tiers)
    ("claude-opus-4", 200_000),
    ("claude-sonnet-4", 200_000),
    ("claude-haiku-4", 200_000),
    ("claude-4-opus", 200_000),
    ("claude-4-sonnet", 200_000),
    ("claude-4-haiku", 200_000),
    ("claude-3-7-sonnet", 200_000),
    ("claude-3-5-sonnet", 200_000),
    ("claude-3-5-haiku", 200_000),
    ("claude-3-opus", 200_000),
    ("claude-3-sonnet", 200_000),
    ("claude-3-haiku", 200_000),
    ("claude-opus", 200_000),
    ("claude-sonnet", 200_000),
    ("claude-haiku", 200_000),
    ("claude", 200_000),
    # Google
    ("gemini-2.5-pro", 1_048_576),
    ("gemini-2.5-flash", 1_048_576),
    ("gemini-2.0-flash", 1_048_576),
    ("gemini-1.5-pro", 2_097_152),
    ("gemini-1.5-flash", 1_048_576),
    ("gemini-pro", 128_000),
    ("gemini-flash", 1_048_576),
    ("gemini", 128_000),
    # xAI
    ("grok-3-mini", 131_072),
    ("grok-3", 131_072),
    ("grok-2", 131_072),
    ("grok", 131_072),
    # DeepSeek
    ("deepseek-v4-pro", 128_000),
    ("deepseek-v4-flash", 128_000),
    ("deepseek-v4", 128_000),
    ("deepseek-v3", 128_000),
    ("deepseek-chat", 128_000),
    ("deepseek-reasoner", 128_000),
    ("deepseek-r1", 128_000),
    ("deepseek", 128_000),
    # Zhipu / GLM (incl. NVIDIA NIM z-ai/*)
    ("glm-5.2", 202_752),
    ("glm-5", 128_000),
    ("glm-4.6", 200_000),
    ("glm-4.5", 128_000),
    ("glm-4", 128_000),
    ("glm-z1", 128_000),
    ("glm", 128_000),
    # Meta Llama (hosted / NIM)
    ("llama-4-maverick", 1_048_576),
    ("llama-4-scout", 10_000_000),
    ("llama-4", 128_000),
    ("llama-3.3-70b", 128_000),
    ("llama-3.3", 128_000),
    ("llama-3.1-405b", 128_000),
    ("llama-3.1-70b", 128_000),
    ("llama-3.1-8b", 128_000),
    ("llama-3.1", 128_000),
    ("llama-3.2", 128_000),
    ("llama-3", 8_192),
    ("llama2", 4_096),
    ("llama", 8_192),
    # Mistral
    ("mistral-large", 128_000),
    ("mistral-small", 32_000),
    ("mistral-nemo", 128_000),
    ("mixtral-8x22b", 64_000),
    ("mixtral", 32_000),
    ("mistral", 32_000),
    ("codestral", 32_000),
    # Qwen
    ("qwen3", 128_000),
    ("qwen2.5-72b", 128_000),
    ("qwen2.5-32b", 128_000),
    ("qwen2.5", 128_000),
    ("qwen2", 128_000),
    ("qwen", 32_000),
    ("qwq", 128_000),
    # Cohere
    ("command-r-plus", 128_000),
    ("command-r", 128_000),
    ("command", 128_000),
    # Microsoft Phi
    ("phi-4", 16_000),
    ("phi-3", 128_000),
    # Local / offline
    ("stub-v0", 8_192),
    ("stub", 8_192),
]

_KNOWN_SORTED = sorted(_KNOWN_WINDOWS, key=lambda t: len(t[0]), reverse=True)


def normalize_model_id(model: str) -> str:
    m = (model or "").strip().lower().replace("_", "-")
    return m.replace(" ", "-")


def _parse_context_value(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        n = int(value)
        # Guard against tiny nonsense / booleans-as-int
        return n if n >= 1024 else None
    if isinstance(value, str):
        s = value.strip().lower().replace(",", "")
        mult = 1
        if s.endswith("k"):
            mult = 1_000
            s = s[:-1]
        elif s.endswith("m"):
            mult = 1_000_000
            s = s[:-1]
        try:
            n = int(float(s) * mult)
            return n if n >= 1024 else None
        except ValueError:
            return None
    return None


def _key_looks_like_context(key: str) -> bool:
    k = key.lower()
    for frag in _CTX_KEY_FRAGMENTS:
        if frag in k:
            return True
    # max_tokens is usually *output* budget — skip as context window
    if k in ("max_tokens", "max_output_tokens", "max_completion_tokens"):
        return False
    if "context" in k and ("len" in k or "size" in k or "window" in k or "token" in k):
        return True
    if k.endswith("_ctx") or k.endswith("ctx_len"):
        return True
    return False


def extract_context_from_model_obj(item: dict[str, Any], *, _depth: int = 0) -> int | None:
    """Recursively find a context-length field in a /models entry."""
    if not isinstance(item, dict) or _depth > 5:
        return None

    # Prefer explicit well-known keys at this level first.
    for key in _CTX_KEY_FRAGMENTS:
        if key in item:
            n = _parse_context_value(item.get(key))
            if n:
                return n

    for key, val in item.items():
        if isinstance(key, str) and _key_looks_like_context(key):
            n = _parse_context_value(val)
            if n:
                return n

    for nest_key in _NEST_KEYS:
        nested = item.get(nest_key)
        if isinstance(nested, dict):
            n = extract_context_from_model_obj(nested, _depth=_depth + 1)
            if n:
                return n
        # Ollama sometimes nests parameter maps as list of {name, value}
        if isinstance(nested, list) and nest_key in ("parameters", "model_info"):
            for entry in nested:
                if not isinstance(entry, dict):
                    continue
                ek = str(entry.get("name") or entry.get("key") or "").lower()
                if _key_looks_like_context(ek) or ek in _CTX_KEY_FRAGMENTS:
                    n = _parse_context_value(entry.get("value") or entry.get("val"))
                    if n:
                        return n

    # One more pass: any nested dict that might hold the field.
    for val in item.values():
        if isinstance(val, dict):
            n = extract_context_from_model_obj(val, _depth=_depth + 1)
            if n:
                return n
    return None


def known_context_for(model: str) -> int | None:
    """Longest fragment match against the well-known table."""
    m = normalize_model_id(model)
    if not m:
        return None
    bare = m.rsplit("/", 1)[-1]
    best: tuple[int, int] | None = None  # (frag_len, tokens)
    for frag, size in _KNOWN_SORTED:
        if frag in m or frag in bare:
            fl = len(frag)
            if best is None or fl > best[0]:
                best = (fl, size)
            # _KNOWN_SORTED is longest-first; first hit is best for this string
            # but "gpt-4" is also in "gpt-4o" — we need the longest that matches.
            # Continuing is correct.
    return best[1] if best else None


def remember_context(model: str, tokens: int) -> None:
    if not model or tokens < 1024:
        return
    key = normalize_model_id(model)
    _LIVE[key] = int(tokens)
    if "/" in key:
        _LIVE[key.split("/", 1)[1]] = int(tokens)


def ingest_models_payload(items: list[Any]) -> None:
    """Cache context lengths from a /models list; fill known table for bare ids."""
    for item in items or []:
        if not isinstance(item, dict):
            continue
        mid = item.get("id") or item.get("name") or item.get("model")
        if not mid:
            continue
        mid_s = str(mid)
        n = extract_context_from_model_obj(item)
        if n:
            remember_context(mid_s, n)
        else:
            # Most OpenAI-compatible catalogs omit context — seed from table
            # so /model list shows real windows instead of default 128k.
            known = known_context_for(mid_s)
            if known:
                remember_context(mid_s, known)


def clear_live_cache() -> None:
    _LIVE.clear()


def _live_match(model: str) -> int | None:
    m = normalize_model_id(model)
    if not m:
        return None
    if m in _LIVE:
        return _LIVE[m]
    if "/" in m:
        bare = m.rsplit("/", 1)[-1]
        if bare in _LIVE:
            return _LIVE[bare]
    # Exact-ish: live key equals bare name of model or full id
    best: tuple[int, int] | None = None
    for key, size in _LIVE.items():
        if key == m or m.endswith("/" + key) or key.endswith("/" + m.split("/")[-1]):
            if best is None or len(key) > best[0]:
                best = (len(key), size)
    return best[1] if best else None


def _hint_from_model_id(model: str) -> int | None:
    """Only parse size tags in the id itself — not a catalog of product names."""
    m = normalize_model_id(model)
    if not m:
        return None
    bracket_m = re.search(r"\[(\d+(?:\.\d+)?)\s*m\]", m)
    if bracket_m:
        return int(float(bracket_m.group(1)) * 1_000_000)
    bracket_k = re.search(r"\[(\d+(?:\.\d+)?)\s*k\]", m)
    if bracket_k:
        return int(float(bracket_k.group(1)) * 1_000)
    # trailing -32k / -1m / .128k (avoid matching years like 2024)
    found_m = re.findall(r"(?:^|[-/.])(\d{1,3})m(?:$|[-/.\[\]])", m)
    if found_m:
        return int(found_m[-1]) * 1_000_000
    found_k = re.findall(r"(?:^|[-/.])(\d{1,4})k(?:$|[-/.\[\]])", m)
    if found_k:
        return int(found_k[-1]) * 1_000
    return None


def context_window_for(model: str, *, session_window: int = 0) -> int:
    """Best-known window for `model`.

    Priority: live API cache → known table → session → id size tags → default.

    `session_window` is optional; live/known beat a stale session value so the
    footer updates after `/model list` fills the cache (many startups seed
    session with the 128k default before any catalog is loaded).
    """
    if not (model or "").strip():
        if session_window and session_window >= 1024:
            return session_window
        return DEFAULT_CONTEXT_WINDOW

    live = _live_match(model)
    if live:
        return live

    known = known_context_for(model)
    if known:
        return known

    if session_window and session_window >= 1024:
        return session_window

    hint = _hint_from_model_id(model)
    if hint:
        return hint

    return DEFAULT_CONTEXT_WINDOW


def refresh_context_from_provider(
    provider: Any,
    model_id: str,
    *,
    timeout: int = 8,
    network: bool = True,
) -> int:
    """Query the provider for model metadata and cache the context length.

    Never raises. Returns the best number we have after the attempt.

    `network=False` — only use the in-process cache / known table / id hints
    (instant; for `/model foo` so we never hang the TUI on a multi‑MB catalog).
    """
    mid = (model_id or "").strip()
    if not mid:
        return DEFAULT_CONTEXT_WINDOW

    # Instant path: already cached.
    hit = _live_match(mid)
    if hit:
        return hit
    if not network:
        return context_window_for(mid)

    try:
        items: list[Any] = []
        if hasattr(provider, "fetch_models_detailed"):
            try:
                raw = provider.fetch_models_detailed(timeout=timeout)
            except TypeError:
                raw = provider.fetch_models_detailed()
            if isinstance(raw, list):
                items = raw
        elif hasattr(provider, "fetch_models"):
            try:
                provider.fetch_models(timeout=timeout)
            except TypeError:
                provider.fetch_models()

        if items:
            ingest_models_payload(items)
            mid_n = normalize_model_id(mid)
            bare = mid_n.rsplit("/", 1)[-1]
            for item in items:
                if not isinstance(item, dict):
                    continue
                iid = str(item.get("id") or item.get("name") or item.get("model") or "")
                iid_n = normalize_model_id(iid)
                if iid_n == mid_n or iid_n.endswith("/" + bare) or iid_n == bare:
                    n = extract_context_from_model_obj(item)
                    if n:
                        remember_context(mid, n)
                        remember_context(iid, n)
                        return n
            # Matched id in catalog but no context field — known table / cache
            got = context_window_for(mid)
            if got and got != DEFAULT_CONTEXT_WINDOW:
                remember_context(mid, got)
            return got
    except Exception:  # noqa: BLE001
        pass

    return context_window_for(mid)


def fmt_context_window(n: int) -> str:
    if n >= 1_000_000:
        if n % 1_000_000 == 0:
            return f"{n // 1_000_000}M"
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        if n % 1_000 == 0:
            return f"{n // 1_000}k"
        return f"{n / 1000:.1f}k"
    return str(n)
