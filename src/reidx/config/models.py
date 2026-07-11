"""Config models: provider, policy, and top-level Config schemas.

Pydantic v2 models. `Config` is the merged, validated runtime configuration.
"""
from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, Field, SecretStr

from reidx.policy.models import PermissionMode


class ProviderConfig(BaseModel):
    name: str
    # Which client to build (anthropic/openai/openai-compatible/ollama). Empty
    # means "same as name" — fine for the built-in kinds whose name matches.
    kind: str = ""
    base_url: str | None = None
    api_key: SecretStr | None = None
    default_model: str = ""


class PolicyConfig(BaseModel):
    default_mode: PermissionMode = PermissionMode.BALANCED
    allowed_commands: list[str] = Field(default_factory=list)
    blocked_commands: list[str] = Field(default_factory=list)
    additional_writable_roots: list[Path] = Field(default_factory=list)
    read_only_paths: list[Path] = Field(default_factory=list)
    shell_timeout_seconds: int = 30


class Config(BaseModel):
    providers: dict[str, ProviderConfig] = Field(default_factory=dict)
    default_provider: str = "stub"
    policy: PolicyConfig = Field(default_factory=PolicyConfig)
    workspace_root: Path | None = None
    storage_root: Path | None = None  # defaults to ~/.reidx when loaded
    log_level: str = "INFO"


def default_config() -> Config:
    """Baseline config with a stub provider so the runtime is exercisable without API keys."""
    return Config(
        providers={
            "stub": ProviderConfig(name="stub", default_model="stub-v0"),
        },
        default_provider="stub",
    )
