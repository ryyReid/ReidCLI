from __future__ import annotations

import os
import sys
from pathlib import Path


def app_data_dir() -> Path:
    if sys.platform == "win32":
        base = os.environ.get("APPDATA")
        if base:
            return Path(base) / "Reid"
    return Path.home() / ".reidx"


def storage_root() -> Path:
    return app_data_dir()


def settings_file() -> Path:
    return app_data_dir() / "settings.json"
