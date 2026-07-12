"""Config loader: merge global + project + env into a validated Config.

Precedence (low -> high): defaults < global (~/.reidx/config.json)
< project (./.reidx/config.json) < environment overrides.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from reidx.config.models import Config, default_config
from reidx.config.settings import apply_settings_env, read_reidx_block
from reidx.config.storage import storage_root
from reidx.diagnostics.logger import get_logger

log = get_logger("reidx.config")

GLOBAL_DIR = storage_root()
PROJECT_DIR = Path(".reidx")
CONFIG_FILENAME = "config.json"


def _read_json(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("failed to read config %s: %s", path, exc)
        return {}


def _env_overrides() -> dict:
    overrides: dict = {}
    provider = os.environ.get("REIDX_PROVIDER")
    if provider:
        overrides["default_provider"] = provider
    workspace = os.environ.get("REIDX_WORKSPACE")
    if workspace:
        overrides["workspace_root"] = workspace
    storage = os.environ.get("REIDX_STORAGE")
    if storage:
        overrides["storage_root"] = storage
    mode = os.environ.get("REIDX_PERMISSION_MODE")
    if mode:
        overrides.setdefault("policy", {})["default_mode"] = mode
    log_level = os.environ.get("REIDX_LOG_LEVEL")
    if log_level:
        overrides["log_level"] = log_level
    return overrides


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


class ConfigLoader:
    """Loads and persists config across global + project scopes."""

    def __init__(self, global_dir: Path = GLOBAL_DIR, project_dir: Path = PROJECT_DIR) -> None:
        self.global_dir = global_dir
        self.project_dir = project_dir

    def load(self) -> Config:
        # Apply the Reidchat settings file's env block (ANTHROPIC_* creds) before
        # anything reads the environment, so the provider routes through Reidchat.
        apply_settings_env()
        data = default_config().model_dump(mode="json")
        data = _deep_merge(data, _read_json(self.global_dir / CONFIG_FILENAME))
        data = _deep_merge(data, _read_json(self.project_dir / CONFIG_FILENAME))
        # Claude-Code-shaped settings.json (`reidx` block) — sits above
        # .reidx/config.json so the project's baked-in settings file wins
        # over an older on-disk config; env vars still win over both.
        data = _deep_merge(data, read_reidx_block())
        data = _deep_merge(data, _env_overrides())

        cfg = Config.model_validate(data)
        if cfg.storage_root is None:
            cfg.storage_root = self.global_dir
        if cfg.workspace_root is None:
            cfg.workspace_root = Path.cwd().resolve()
        return cfg

    def save_global(self, cfg: Config) -> None:
        self.global_dir.mkdir(parents=True, exist_ok=True)
        path = self.global_dir / CONFIG_FILENAME
        path.write_text(
            cfg.model_dump_json(indent=2, exclude_none=True, mode="json"),
            encoding="utf-8",
        )

    def save_project(self, cfg: Config) -> None:
        self.project_dir.mkdir(parents=True, exist_ok=True)
        path = self.project_dir / CONFIG_FILENAME
        path.write_text(
            cfg.model_dump_json(indent=2, exclude_none=True, mode="json"),
            encoding="utf-8",
        )
