"""User settings auto-seed (npm install + first reid launch)."""
from __future__ import annotations

import json
from pathlib import Path

from reidx.config.settings import (
    apply_settings_env,
    default_settings_template,
    ensure_user_settings,
    global_settings_path,
)


def test_ensure_user_settings_creates_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REIDX_STORAGE", str(tmp_path / "store"))
    path = ensure_user_settings()
    assert path.exists()
    assert path == global_settings_path()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert "reidx" in data
    assert data["reidx"]["default_provider"] == "stub"
    # Second call does not clobber
    path.write_text(json.dumps({"reidx": {"default_provider": "anthropic"}}), encoding="utf-8")
    path2 = ensure_user_settings()
    assert path2 == path
    data2 = json.loads(path2.read_text(encoding="utf-8"))
    assert data2["reidx"]["default_provider"] == "anthropic"


def test_empty_env_placeholders_do_not_wipe_ambient(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("REIDX_STORAGE", str(tmp_path / "store"))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "from-shell")
    path = ensure_user_settings()
    # Template has empty ANTHROPIC_API_KEY — must not overwrite shell key
    applied = apply_settings_env(path)
    assert "ANTHROPIC_API_KEY" not in applied
    import os

    assert os.environ.get("ANTHROPIC_API_KEY") == "from-shell"


def test_default_template_has_reidx_block() -> None:
    t = default_settings_template()
    assert "env" in t
    assert "reidx" in t
    assert t["reidx"]["policy"]["default_mode"] == "balanced"
