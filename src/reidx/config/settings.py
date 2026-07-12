"""Claude-Code-style settings file support.

A settings.json holds an `env` block (applied to os.environ before any provider
reads credentials) and an optional `reidx` block (project-baked ReidX
configuration — default_provider, policy, providers, etc.). Same file shape as
Claude Code's settings.json so existing files work as-is; ReidX-specific
config lives under `reidx` so unknown keys (`theme`, `effortLevel`, ...) are
ignored harmlessly.

Path resolution (first hit wins):
  1. $REIDCHAT_SETTINGS                (explicit override)
  2. ./settings.json                   (project-local, when it exists)
  3. ~/.reidx/settings.json            (global default)
  4. ~/Reidchat.json                    (legacy fallback)

Project-local wins over the global file so a project can bake in a different
backend, permission mode, or provider set without editing the global file.
The env block is authoritative for the process (overrides any ambient env)
— this is how ReidX picks up proxy credentials even when the shell's
ANTHROPIC_* vars point somewhere else.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

from reidx.config.storage import app_data_dir
from reidx.diagnostics.logger import get_logger

log = get_logger("reidx.config.settings")

GLOBAL_SETTINGS_PATH = app_data_dir() / "settings.json"
PROJECT_SETTINGS_FILENAME = "settings.json"
LEGACY_SETTINGS_PATH = Path.home() / "Reidchat.json"


def _walk_upward_for_project_settings(start: Path) -> Path | None:
    """Walk from `start` toward the filesystem root looking for settings.json.

    Mirrors how git finds `.git` — so launching `reid` from any
    subdirectory of a project still finds that project's baked-in
    settings.json. Stops at the root (path.parent == path).
    """
    seen: set[Path] = set()
    current = start.resolve()
    while current not in seen:
        seen.add(current)
        candidate = current / PROJECT_SETTINGS_FILENAME
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def settings_path() -> Path:
    """Resolve the settings file path (first hit wins).

    Order:
      1. $REIDCHAT_SETTINGS                    (explicit override)
      2. project settings.json (walk upward from CWD)
      3. ~/.reidx/settings.json              (global default)
      4. ~/Reidchat.json                      (legacy fallback)

    Returns the last fallback even if it doesn't exist, so `doctor` can
    report it as "missing" rather than crashing on a None.
    """
    override = os.environ.get("REIDCHAT_SETTINGS", "").strip()
    if override:
        return Path(override)
    project = _walk_upward_for_project_settings(Path.cwd())
    if project is not None:
        return project
    if GLOBAL_SETTINGS_PATH.exists():
        return GLOBAL_SETTINGS_PATH
    return LEGACY_SETTINGS_PATH


def _read_settings(path: Path | None = None) -> dict:
    path = path or settings_path()
    if not path.exists():
        log.debug("settings file not found: %s", path)
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("failed to read settings %s: %s", path, exc)
        return {}


def apply_settings_env(path: Path | None = None) -> dict[str, str]:
    """Apply a settings file's `env` block to os.environ.

    Returns the mapping of env vars that were actually applied. Missing or
    invalid files are a no-op.
    """
    data = _read_settings(path)
    env = data.get("env")
    if not isinstance(env, dict):
        return {}

    applied: dict[str, str] = {}
    for key, value in env.items():
        if value is None:
            continue
        new = str(value)
        if os.environ.get(key) not in (None, new):
            log.debug("settings override %s (ambient env replaced)", key)
        os.environ[key] = new  # the settings file is authoritative
        applied[key] = new

    if applied:
        log.debug("applied %d env var(s) from settings: %s", len(applied), ", ".join(applied))
    return applied


def read_reidx_block(path: Path | None = None) -> dict:
    """Return the settings file's `reidx` block, or {} if absent.

    Loader merges this into Config below the env-var overrides but above the
    on-disk .reidx/config.json, so `settings.json` is the project's baked-in
    source of truth for anything that isn't an env credential.
    """
    data = _read_settings(path)
    block = data.get("reidx")
    return block if isinstance(block, dict) else {}
