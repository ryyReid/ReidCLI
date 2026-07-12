"""Model normalization, validation, and discovery utilities."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

from reidx.diagnostics.logger import get_logger

log = get_logger("reidx.provider.models")

KNOWN_PROVIDER_PREFIXES = {
    "openrouter",
    "anthropic",
    "openai",
    "google",
    "cohere",
    "mistral",
    "meta",
    "meta-llama",
    "microsoft",
    "nvidia",
    "deepseek",
    "qwen",
    "yi",
    "zai",
    "xai",
    "perplexity",
    "together",
    "fireworks",
    "groq",
    "cerebras",
    "deepinfra",
    "sambanova",
    "novita",
    "featherless",
    "chutes",
    "aimlapi",
    "hyperbolic",
    "moonshot",
    "volcengine",
    "hunyuan",
    "upstage",
    "predibase",
    "gravity",
    "infermatic",
    "baseten",
    "anyscale",
    "lambda",
    "kluster",
    "monsterapi",
    "cloudflare-ai",
    "replicate",
    "watsonx",
    "aleph-alpha",
    "ai21",
    "databricks",
    "siliconflow",
    "nebius",
    "lepton",
    "voyage-ai",
    "jan",
    "llamacpp",
}

VALID_VARIANTS = {":free", ":thinking", ":nitro", ":extended", ":online", ":alpha", ":beta"}
VALID_VARIANTS_LOWER = {v.lower() for v in VALID_VARIANTS}

UI_DISPLAY_PATTERN = re.compile(r"\s*\[via\s+([^\]]+)\]", re.IGNORECASE)
PROVIDER_DASH_PATTERN = re.compile(r"^([A-Za-z0-9\-]+)\s+-\s+(.+)$")


@dataclass(frozen=True)
class NormalizedModel:
    provider: str
    model_id: str
    full_id: str
    variant: Optional[str]
    base_model: str
    is_valid: bool


def normalize_model_id(raw: str, provider_name: Optional[str] = None) -> NormalizedModel:
    original = raw.strip()

    # Remove UI display text like "[via openrouter]"
    cleaned = UI_DISPLAY_PATTERN.sub("", original).strip()

    # Remove "ProviderName - " prefix
    dash_match = PROVIDER_DASH_PATTERN.match(cleaned)
    if dash_match:
        cleaned = dash_match.group(2).strip()

    parts = cleaned.split("/")

    if not parts or not parts[0]:
        return NormalizedModel(
            provider=provider_name.lower() if provider_name else "",
            model_id=cleaned,
            full_id=cleaned,
            variant=None,
            base_model=cleaned,
            is_valid=False,
        )

    first_part_lower = parts[0].lower()
    provider = ""
    model_parts = parts

    if provider_name and provider_name.lower() in KNOWN_PROVIDER_PREFIXES:
        provider = provider_name.lower()
        if first_part_lower == provider:
            model_parts = parts[1:]
    elif first_part_lower in KNOWN_PROVIDER_PREFIXES:
        provider = first_part_lower
        model_parts = parts[1:]

    if not model_parts:
        return NormalizedModel(
            provider=provider,
            model_id="",
            full_id=cleaned,
            variant=None,
            base_model="",
            is_valid=False,
        )

    model_id = "/".join(model_parts)

    variant = None
    base_model = model_id
    for v in VALID_VARIANTS:
        if model_id.lower().endswith(v.lower()):
            variant = v[1:]
            base_model = model_id[: -len(v)]
            break

    if provider:
        full_id = f"{provider}/{model_id}"
    else:
        full_id = model_id

    is_valid = bool(provider and base_model and "/" in base_model)

    return NormalizedModel(
        provider=provider,
        model_id=model_id,
        full_id=full_id,
        variant=variant,
        base_model=base_model,
        is_valid=is_valid,
    )


def denormalize_model_id(normalized: NormalizedModel, include_provider: bool = True) -> str:
    if include_provider and normalized.provider:
        return normalized.full_id
    return normalized.model_id


async def fetch_provider_models(provider) -> list[str]:
    import asyncio
    try:
        loop = asyncio.get_event_loop()
        models = await loop.run_in_executor(None, provider.fetch_models)
        return sorted(models) if models else []
    except Exception as exc:
        log.warning("failed to fetch models from provider: %s", exc)
        return []


def validate_model_against_provider(
    normalized: NormalizedModel,
    available_models: list[str],
) -> tuple[bool, str]:
    if not normalized.is_valid:
        return False, f"Invalid model format: {normalized.full_id}"

    if not available_models:
        return True, "No model list available from provider (skipping validation)"

    if normalized.full_id in available_models:
        return True, "Model found in provider catalog"

    if normalized.model_id in available_models:
        return True, "Model found in provider catalog (without provider prefix)"

    if normalized.base_model in available_models:
        return True, f"Base model found (variant '{normalized.variant}' may not be listed separately)"

    base_with_provider = f"{normalized.provider}/{normalized.base_model}"
    if base_with_provider in available_models:
        return True, f"Base model found with provider prefix (variant '{normalized.variant}' may not be listed separately)"

    for avail in available_models:
        if normalized.base_model in avail or avail in normalized.base_model:
            return True, f"Similar model found: {avail}"

    return False, f"Model '{normalized.full_id}' not found in provider catalog ({len(available_models)} models available)"


class ModelCache:
    def __init__(self) -> None:
        self._cache: dict[str, tuple[list[str], float]] = {}
        self._ttl = 3600

    def get(self, provider_name: str) -> Optional[list[str]]:
        import time
        if provider_name in self._cache:
            models, timestamp = self._cache[provider_name]
            if time.time() - timestamp < self._ttl:
                return models
            else:
                del self._cache[provider_name]
        return None

    def set(self, provider_name: str, models: list[str]) -> None:
        import time
        self._cache[provider_name] = (models, time.time())

    def invalidate(self, provider_name: str) -> None:
        self._cache.pop(provider_name, None)


_model_cache = ModelCache()


def get_model_cache() -> ModelCache:
    return _model_cache