"""App data locations.

Default storage root is always under the user home directory:

    Windows:  C:\\Users\\<you>\\.reidcli
    macOS/Linux:  /home/<you>/.reidcli   (or /Users/<you>/.reidcli)

Override with env `REIDX_STORAGE` if needed. Older installs used
`%APPDATA%\\Reid` (Windows) or `~/.reidx`; those are migrated once into
`.reidcli` on first access so existing sessions/providers aren't lost.
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

# Canonical dir name under the user profile.
STORAGE_DIRNAME = ".reidcli"

# Legacy roots we may still find on disk (pre-.reidcli).
_LEGACY_DIRNAMES = (".reidx",)


def app_data_dir() -> Path:
    """Return `~/.reidcli`, creating it if needed, and migrate legacy data."""
    root = Path.home() / STORAGE_DIRNAME
    _ensure_root(root)
    _migrate_legacy_if_needed(root)
    return root


def storage_root() -> Path:
    """Storage root for sessions, providers, workflows, settings."""
    env = os.environ.get("REIDX_STORAGE", "").strip()
    if env:
        path = Path(env).expanduser().resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path
    return app_data_dir()


def settings_file() -> Path:
    return storage_root() / "settings.json"


def _ensure_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)


def _legacy_candidates() -> list[Path]:
    """Locations used by older Reid/ReidX installs."""
    home = Path.home()
    candidates: list[Path] = [home / name for name in _LEGACY_DIRNAMES]
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "Reid")
        local = os.environ.get("LOCALAPPDATA")
        if local:
            candidates.append(Path(local) / "Reid")
    return candidates


def _dir_has_user_data(path: Path) -> bool:
    if not path.is_dir():
        return False
    markers = (
        "providers.db",
        "providers.json",
        "sessions",
        "workflows.json",
        "config.json",
        "settings.json",
    )
    return any((path / name).exists() for name in markers)


def _migrate_legacy_if_needed(new_root: Path) -> None:
    """Copy data from old roots into ~/.reidcli once.

    Skips if the new root already has user data (don't clobber a live install).
    Only copies files/dirs that are missing at the destination.
    """
    if _dir_has_user_data(new_root):
        return
    for old in _legacy_candidates():
        if old.resolve() == new_root.resolve():
            continue
        if not _dir_has_user_data(old):
            continue
        try:
            for entry in old.iterdir():
                dest = new_root / entry.name
                if dest.exists():
                    continue
                if entry.is_dir():
                    shutil.copytree(entry, dest)
                else:
                    shutil.copy2(entry, dest)
        except OSError:
            # Migration is best-effort; a partial copy still beats losing data.
            continue
        # One legacy source is enough.
        break
