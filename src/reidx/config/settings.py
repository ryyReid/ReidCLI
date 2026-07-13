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
  3. ~/.reidcli/settings.json          (global user settings — auto-created)

On first launch (and on `npm install` via postinstall) a usable global
settings.json is written under ~/.reidcli so npm-global installs work without
copying a file out of the package tree.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from reidx.config.storage import storage_root
from reidx.diagnostics.logger import get_logger

log = get_logger("reidx.config.settings")

PROJECT_SETTINGS_FILENAME = "settings.json"
LEGACY_SETTINGS_PATH = Path.home() / "Reidchat.json"


def global_settings_path() -> Path:
    """User-level settings: ~/.reidcli/settings.json (or $REIDX_STORAGE)."""
    return storage_root() / "settings.json"


def default_settings_template() -> dict[str, Any]:
    """Fresh settings.json body for new installs (no secrets).

    Empty/placeholder env values are skipped at apply-time so ambient
    ANTHROPIC_API_KEY / OPENAI_API_KEY still work until the user fills these in.
    """
    return {
        "_comment": (
            "ReidX user settings. Edit this file or use /connect and /model in the TUI. "
            "Location: ~/.reidcli/settings.json  |  env vars with empty strings are ignored."
        ),
        "env": {
            "ANTHROPIC_API_KEY": "",
            "ANTHROPIC_BASE_URL": "",
            "ANTHROPIC_MODEL": "",
            "OPENAI_API_KEY": "",
            "OPENAI_BASE_URL": "",
            "OPENAI_MODEL": "",
        },
        "theme": "dark",
        "effortLevel": "medium",
        "reidx": {
            "default_provider": "stub",
            "log_level": "WARNING",
            "policy": {
                "default_mode": "balanced",
                "shell_timeout_seconds": 60,
            },
            "providers": {},
        },
    }


def ensure_user_settings(*, force: bool = False) -> Path:
    """Create ~/.reidcli/settings.json if missing (or always if force=True).

    Safe to call on every startup. Never overwrites a non-empty existing file
    unless force=True. Returns the path written or already present.
    """
    path = global_settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and not force:
        # Treat empty/corrupt files as missing so npm users recover cleanly.
        try:
            raw = path.read_text(encoding="utf-8").strip()
            if raw:
                json.loads(raw)
                return path
        except (OSError, json.JSONDecodeError):
            log.warning("repairing unreadable settings at %s", path)
    body = json.dumps(default_settings_template(), indent=2) + "\n"
    path.write_text(body, encoding="utf-8")
    log.info("wrote default settings → %s", path)
    return path


def _walk_upward_for_project_settings(start: Path) -> Path | None:
    """Walk from `start` toward the filesystem root looking for settings.json.

    Mirrors how git finds `.git` — so launching `reid` from any
    subdirectory of a project still finds that project's baked-in
    settings.json. Stops at the root (path.parent == path).

    Skips the package install tree's own settings when that would surprise
    npm-global users (we still honor a real project file in the cwd tree).
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
      3. ~/.reidcli/settings.json              (auto-created if missing)
      4. ~/Reidchat.json                       (legacy, only if it exists)

    Always returns a path; global settings are created on demand so npm
    installs have something to edit without manual setup.
    """
    override = os.environ.get("REIDCHAT_SETTINGS", "").strip()
    if override:
        return Path(override).expanduser()
    project = _walk_upward_for_project_settings(Path.cwd())
    if project is not None:
        return project
    if LEGACY_SETTINGS_PATH.exists() and not global_settings_path().exists():
        # One-time: prefer legacy only until we seed the new location.
        return LEGACY_SETTINGS_PATH
    return ensure_user_settings()


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
    invalid files are a no-op. Empty-string values are skipped so a fresh
    template does not wipe ambient API keys.
    """
    # Ensure global settings exist before first read (npm / fresh install).
    if path is None:
        ensure_user_settings()
    data = _read_settings(path)
    env = data.get("env")
    if not isinstance(env, dict):
        return {}

    applied: dict[str, str] = {}
    for key, value in env.items():
        if value is None:
            continue
        new = str(value)
        if not new.strip():
            # Placeholder in the default template — leave ambient env alone.
            continue
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


def persist_default_model(model: str, provider_name: str | None = None, path: Path | None = None) -> Path | None:
    """Write the default model into the active settings.json so restarts keep it.

    Updates:
      - env.ANTHROPIC_MODEL (when provider is anthropic / unset)
      - reidx.providers.<name>.default_model

    Creates the global settings file if needed. Returns the path written.
    """
    path = path or settings_path()
    if not path.exists():
        # Prefer writing into the user global file, not a missing override path.
        path = ensure_user_settings()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("failed to read settings for model persist %s: %s", path, exc)
        try:
            data = default_settings_template()
        except Exception:  # noqa: BLE001
            return None

    if not isinstance(data, dict):
        return None

    env = data.get("env")
    if not isinstance(env, dict):
        env = {}
        data["env"] = env
    # Keep env in sync for providers that read ANTHROPIC_MODEL / OPENAI_MODEL.
    if not provider_name or provider_name == "anthropic":
        if model:
            env["ANTHROPIC_MODEL"] = model
        else:
            env.pop("ANTHROPIC_MODEL", None)
    if provider_name == "openai":
        if model:
            env["OPENAI_MODEL"] = model
        else:
            env.pop("OPENAI_MODEL", None)

    reidx = data.get("reidx")
    if not isinstance(reidx, dict):
        reidx = {}
        data["reidx"] = reidx
    providers = reidx.get("providers")
    if not isinstance(providers, dict):
        providers = {}
        reidx["providers"] = providers
    name = provider_name or reidx.get("default_provider") or "anthropic"
    entry = providers.get(name)
    if not isinstance(entry, dict):
        entry = {"name": name}
        providers[name] = entry
    entry["default_model"] = model

    try:
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        log.warning("failed to write settings %s: %s", path, exc)
        return None

    # Live process: keep env matching so subsequent provider rebuilds see it.
    if not provider_name or provider_name == "anthropic":
        if model:
            os.environ["ANTHROPIC_MODEL"] = model
        else:
            os.environ.pop("ANTHROPIC_MODEL", None)
    return path
