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


def load_into(registry: ProviderRegistry, storage_root: Path) -> list[str]:
    """Register every persisted provider into `registry`. Returns the list of
    names successfully registered. Skips (with a warning) any record whose
    kind is unknown or fails to construct — the app must not crash on a
    single bad entry."""
    added: list[str] = []
    store = ProviderStore(storage_root)
    for record in store.list():
        try:
            registry.register(record.name, build_provider(record))
            added.append(record.name)
        except (ValueError, TypeError):
            log.exception("skipping provider %s (kind=%s): failed to build", record.name, record.kind)
    return added
