"""On-disk persistence for user-added providers (`/connect`).

File: `<storage_root>/providers.json`, chmod 600 on POSIX so API keys aren't
world-readable. Format:

    {"providers": [
        {"name": "local-llama", "kind": "openai-compatible",
         "base_url": "http://localhost:8080", "api_key": "", "default_model": "..."},
        ...
    ]}

The registry rebuilds each provider on load via `build_provider(kind, ...)`.
Nothing in here auto-changes `config.default_provider` — stub stays default;
switching is explicit via `/use`.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

from reidx.diagnostics.logger import get_logger
from reidx.provider.anthropic import AnthropicProvider
from reidx.provider.base import BaseProvider
from reidx.provider.models import (
    denormalize_model_id,
    normalize_model_id,
    validate_model_against_provider,
)
from reidx.provider.ollama import OllamaProvider
from reidx.provider.openai import OpenAICompatibleProvider, OpenAIProvider
from reidx.provider.registry import ProviderRegistry

log = get_logger("reidx.provider.store")

SUPPORTED_KINDS = ("anthropic", "openai", "openai-compatible", "ollama")


@dataclass
class ProviderRecord:
    name: str
    kind: str
    base_url: str = ""
    api_key: str = ""
    default_model: str = ""
    auth_method: str = "bearer"


def build_provider(record: ProviderRecord) -> BaseProvider:
    kind = record.kind
    if kind == "anthropic":
        return AnthropicProvider(
            api_key=record.api_key,
            base_url=record.base_url or "https://api.anthropic.com",
            default_model=record.default_model,
        )
    if kind == "openai":
        return OpenAIProvider(
            api_key=record.api_key,
            base_url=record.base_url,
            default_model=record.default_model,
        )
    if kind == "openai-compatible":
        return OpenAICompatibleProvider(
            api_key=record.api_key,
            base_url=record.base_url,
            default_model=record.default_model,
            auth_method=record.auth_method,
        )
    if kind == "ollama":
        return OllamaProvider(
            base_url=record.base_url,
            default_model=record.default_model,
            api_key=record.api_key,
        )
    raise ValueError(f"unsupported provider kind: {kind}")


class ProviderStore:
    def __init__(self, storage_root: Path) -> None:
        self.path = Path(storage_root) / "providers.json"

    def _read(self) -> list[ProviderRecord]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("failed to read providers.json; treating as empty")
            return []
        out: list[ProviderRecord] = []
        for entry in data.get("providers", []):
            try:
                out.append(ProviderRecord(**entry))
            except TypeError:
                log.warning("skipping malformed provider entry: %s", entry)
        return out

    def _write(self, records: list[ProviderRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps({"providers": [asdict(r) for r in records]}, indent=2),
            encoding="utf-8",
        )
        # Best-effort key protection on POSIX; a no-op on Windows.
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def list(self) -> list[ProviderRecord]:
        return self._read()

    def get(self, name: str) -> ProviderRecord | None:
        for r in self._read():
            if r.name == name:
                return r
        return None

    def save(self, record: ProviderRecord) -> None:
        records = [r for r in self._read() if r.name != record.name]
        records.append(record)
        self._write(records)

    def delete(self, name: str) -> bool:
        records = self._read()
        remaining = [r for r in records if r.name != name]
        if len(remaining) == len(records):
            return False
        self._write(remaining)
        return True


def validate_provider(
    record: ProviderRecord,
    skip_verify: bool = False,
) -> tuple[bool, str]:
    try:
        provider = build_provider(record)
    except (ValueError, TypeError) as exc:
        return False, str(exc)
    _keyless_kinds = ("ollama", "openai-compatible")
    if not record.api_key and record.kind not in _keyless_kinds:
        return False, "API key required for this provider kind"
    if not record.api_key:
        return True, "no key required"
    if skip_verify:
        return True, "verification skipped"
    try:
        models = provider.fetch_models()
    except RuntimeError as exc:
        msg = str(exc)
        if msg.startswith("HTTP 401") or msg.startswith("HTTP 403"):
            return False, f"authentication failed ({msg})"
        if msg.startswith("HTTP 404"):
            return False, f"endpoint not found - check base URL ({msg})"
        if msg.startswith("HTTP "):
            return False, f"provider error: {msg}"
        return False, f"{msg} (use --skip-verify to save anyway)"
    except Exception as exc:
        return False, f"unexpected error: {exc} (use --skip-verify to save anyway)"
    
    if models and record.default_model:
        normalized = normalize_model_id(record.default_model, provider_name=record.name)
        if normalized.is_valid:
            is_valid, msg = validate_model_against_provider(normalized, models)
            if not is_valid:
                return False, f"model validation failed: {msg}"
            # Update the record with the normalized model ID
            record.default_model = denormalize_model_id(normalized)
    
    if models:
        return True, f"ok ({len(models)} models available)"
    return True, "connected (no models endpoint or empty list)"


def load_into(registry: ProviderRegistry, storage_root: Path) -> list[str]:
    added: list[str] = []
    store = ProviderStore(storage_root)
    for record in store.list():
        try:
            registry.register(record.name, build_provider(record))
            added.append(record.name)
        except (ValueError, TypeError):
            log.exception("skipping provider %s (kind=%s): failed to build", record.name, record.kind)
    return added


def load_from_database(registry: ProviderRegistry, storage_root: Path) -> list[str]:
    from reidx.provider.models import denormalize_model_id, normalize_model_id
    from reidx.provider_manager.database import ProviderDatabase

    added: list[str] = []
    db = ProviderDatabase(storage_root)
    for sp in db.list_providers():
        if registry.has(sp.name):
            continue
        api_key = sp.decrypted_api_key()
        record = ProviderRecord(
            name=sp.name,
            kind=sp.kind,
            base_url=sp.base_url,
            api_key=api_key,
            default_model=sp.default_model,
            auth_method=sp.auth_method,
        )
        try:
            provider = build_provider(record)
        except (ValueError, TypeError):
            log.warning("skipping provider %s (kind=%s): failed to build", sp.name, sp.kind)
            continue
        if not record.default_model:
            try:
                models = provider.fetch_models()
            except Exception:
                models = []
            if models:
                normalized = normalize_model_id(models[0], provider_name=sp.name)
                if normalized.is_valid:
                    model = denormalize_model_id(normalized)
                    provider.default_model = model
                    sp.default_model = model
                    db.save_provider(sp)
                    log.info("auto-fetched model for %s: %s", sp.name, model)
        elif record.default_model:
            # Normalize the existing model
            normalized = normalize_model_id(record.default_model, provider_name=sp.name)
            if normalized.is_valid:
                model = denormalize_model_id(normalized)
                record.default_model = model
                provider.default_model = model
                sp.default_model = model
                db.save_provider(sp)
        registry.register(sp.name, provider)
        added.append(sp.name)
    return added
