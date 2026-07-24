from __future__ import annotations

import json
import os
import time
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path

from reidx.diagnostics.logger import get_logger
from reidx.provider_manager import keychain

log = get_logger("reidx.provider_manager.database")

SCHEMA_VERSION = 2
DB_FILENAME = "providers.db"
LEGACY_FILENAME = "providers.json"


@dataclass
class StoredKey:
    id: str
    label: str
    encrypted_key: str

    def decrypt(self) -> str:
        return keychain.decrypt(self.encrypted_key)


@dataclass
class OAuthTokens:
    access_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    expires_at: float = 0
    scope: str = ""
    token_type: str = "Bearer"

    def is_expired(self, buffer_seconds: int = 300) -> bool:
        return self.expires_at - time.time() < buffer_seconds

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
            "token_type": self.token_type,
        }

    @classmethod
    def from_dict(cls, data: dict) -> OAuthTokens:
        return cls(
            access_token=data.get("access_token", ""),
            refresh_token=data.get("refresh_token", ""),
            id_token=data.get("id_token", ""),
            expires_at=data.get("expires_at", 0),
            scope=data.get("scope", ""),
            token_type=data.get("token_type", "Bearer"),
        )

    def encrypt(self) -> str:
        return keychain.encrypt(str(self.to_dict()))

    @classmethod
    def decrypt(cls, encrypted: str) -> OAuthTokens:
        try:
            data = eval(keychain.decrypt(encrypted))
            return cls.from_dict(data)
        except Exception:
            return cls()


@dataclass
class StoredProvider:
    name: str
    kind: str
    base_url: str = ""
    default_model: str = ""
    auth_method: str = "bearer"
    extra_headers: dict[str, str] = field(default_factory=dict)
    catalog_id: str | None = None
    keys: list[StoredKey] = field(default_factory=list)
    active_key_id: str | None = None
    oauth_tokens: OAuthTokens | None = None

    def active_key(self) -> StoredKey | None:
        if not self.active_key_id:
            return self.keys[0] if self.keys else None
        for k in self.keys:
            if k.id == self.active_key_id:
                return k
        return self.keys[0] if self.keys else None

    def decrypted_api_key(self) -> str:
        k = self.active_key()
        return k.decrypt() if k else ""

    def decrypted_oauth_access_token(self) -> str:
        if self.oauth_tokens and not self.oauth_tokens.is_expired():
            return self.oauth_tokens.access_token
        return ""

    def has_valid_oauth(self) -> bool:
        return self.oauth_tokens is not None and not self.oauth_tokens.is_expired()


class ProviderDatabase:
    def __init__(self, storage_root: Path) -> None:
        self.path = Path(storage_root) / DB_FILENAME
        self.legacy_path = Path(storage_root) / LEGACY_FILENAME
        self._migrate_legacy_if_needed()

    def _migrate_legacy_if_needed(self) -> None:
        if self.path.exists() or not self.legacy_path.exists():
            return
        try:
            data = json.loads(self.legacy_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.warning("failed to read legacy providers.json; skipping migration")
            return
        records = data.get("providers", [])
        if not records:
            return
        log.info("migrating %d provider(s) from legacy providers.json", len(records))
        providers: list[dict] = []
        for entry in records:
            name = entry.get("name", "")
            if not name:
                continue
            api_key = entry.get("api_key", "")
            keys: list[dict] = []
            active_key_id = None
            if api_key:
                kid = uuid.uuid4().hex[:12]
                keys.append({
                    "id": kid,
                    "label": "Imported",
                    "encrypted_key": keychain.encrypt(api_key),
                })
                active_key_id = kid
            providers.append({
                "name": name,
                "kind": entry.get("kind", "openai-compatible"),
                "base_url": entry.get("base_url", ""),
                "default_model": entry.get("default_model", ""),
                "auth_method": "bearer",
                "extra_headers": {},
                "catalog_id": None,
                "keys": keys,
                "active_key_id": active_key_id,
            })
        self._write_raw({"version": SCHEMA_VERSION, "providers": providers})
        try:
            os.rename(self.legacy_path, self.legacy_path.with_suffix(".json.bak"))
        except OSError:
            pass
        log.info("migration complete; legacy file backed up")

    def _read_raw(self) -> dict:
        if not self.path.exists():
            return {"version": SCHEMA_VERSION, "providers": []}
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            log.exception("failed to read providers.db; treating as empty")
            return {"version": SCHEMA_VERSION, "providers": []}
        if data.get("version", 0) > SCHEMA_VERSION:
            log.warning("providers.db version %s > supported %s; some features may not work",
                        data.get("version"), SCHEMA_VERSION)
        return data

    def _write_raw(self, data: dict) -> None:
        keychain.secure_write(self.path, json.dumps(data, indent=2))

    def _to_stored(self, entry: dict) -> StoredProvider:
        keys = [StoredKey(
            id=k.get("id", ""),
            label=k.get("label", ""),
            encrypted_key=k.get("encrypted_key", ""),
        ) for k in entry.get("keys", [])]
        
        oauth_tokens = None
        oauth_data = entry.get("oauth_tokens")
        if oauth_data:
            try:
                oauth_tokens = OAuthTokens.from_dict(oauth_data)
            except Exception:
                pass

        return StoredProvider(
            name=entry.get("name", ""),
            kind=entry.get("kind", "openai-compatible"),
            base_url=entry.get("base_url", ""),
            default_model=entry.get("default_model", ""),
            auth_method=entry.get("auth_method", "bearer"),
            extra_headers=entry.get("extra_headers", {}),
            catalog_id=entry.get("catalog_id"),
            keys=keys,
            active_key_id=entry.get("active_key_id"),
            oauth_tokens=oauth_tokens,
        )

    def _to_dict(self, p: StoredProvider) -> dict:
        result = {
            "name": p.name,
            "kind": p.kind,
            "base_url": p.base_url,
            "default_model": p.default_model,
            "auth_method": p.auth_method,
            "extra_headers": p.extra_headers,
            "catalog_id": p.catalog_id,
            "keys": [asdict(k) for k in p.keys],
            "active_key_id": p.active_key_id,
        }
        if p.oauth_tokens:
            result["oauth_tokens"] = p.oauth_tokens.to_dict()
        return result

    def list_providers(self) -> list[StoredProvider]:
        data = self._read_raw()
        return [self._to_stored(e) for e in data.get("providers", [])]

    def get_provider(self, name: str) -> StoredProvider | None:
        for p in self.list_providers():
            if p.name == name:
                return p
        return None

    def save_provider(self, provider: StoredProvider) -> None:
        data = self._read_raw()
        providers = data.get("providers", [])
        providers = [e for e in providers if e.get("name") != provider.name]
        providers.append(self._to_dict(provider))
        data["providers"] = providers
        self._write_raw(data)

    def remove_provider(self, name: str) -> bool:
        data = self._read_raw()
        providers = data.get("providers", [])
        remaining = [e for e in providers if e.get("name") != name]
        if len(remaining) == len(providers):
            return False
        data["providers"] = remaining
        self._write_raw(data)
        return True

    def add_key(self, provider_name: str, label: str, api_key: str) -> StoredKey | None:
        p = self.get_provider(provider_name)
        if p is None:
            return None
        key = StoredKey(
            id=uuid.uuid4().hex[:12],
            label=label,
            encrypted_key=keychain.encrypt(api_key),
        )
        p.keys.append(key)
        if p.active_key_id is None:
            p.active_key_id = key.id
        self.save_provider(p)
        return key

    def remove_key(self, provider_name: str, key_id: str) -> bool:
        p = self.get_provider(provider_name)
        if p is None:
            return False
        before = len(p.keys)
        p.keys = [k for k in p.keys if k.id != key_id]
        if len(p.keys) == before:
            return False
        if p.active_key_id == key_id:
            p.active_key_id = p.keys[0].id if p.keys else None
        self.save_provider(p)
        return True

    def rename_key(self, provider_name: str, key_id: str, new_label: str) -> bool:
        p = self.get_provider(provider_name)
        if p is None:
            return False
        for k in p.keys:
            if k.id == key_id:
                k.label = new_label
                self.save_provider(p)
                return True
        return False

    def set_active_key(self, provider_name: str, key_id: str) -> bool:
        p = self.get_provider(provider_name)
        if p is None:
            return False
        if not any(k.id == key_id for k in p.keys):
            return False
        p.active_key_id = key_id
        self.save_provider(p)
        return True

    def get_decrypted_key(self, provider_name: str) -> str:
        p = self.get_provider(provider_name)
        if p is None:
            return ""
        return p.decrypted_api_key()

    def save_oauth_tokens(self, provider_name: str, tokens: OAuthTokens) -> bool:
        p = self.get_provider(provider_name)
        if p is None:
            return False
        p.oauth_tokens = tokens
        self.save_provider(p)
        return True

    def get_oauth_tokens(self, provider_name: str) -> OAuthTokens | None:
        p = self.get_provider(provider_name)
        if p is None or p.oauth_tokens is None:
            return None
        return p.oauth_tokens
