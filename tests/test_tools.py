"""Tool registry + file tool tests: dispatch, path safety, unknown tools."""
from __future__ import annotations

from pathlib import Path

from reidx.config.models import PolicyConfig, default_config
from reidx.policy.engine import PolicyEngine
from reidx.policy.models import PermissionMode
from reidx.tools import default_registry
from reidx.tools.base import ToolContext


def _ctx(tmp_path: Path, approver=None) -> ToolContext:  # type: ignore[no-untyped-def]
    cfg = default_config()
    cfg.workspace_root = tmp_path
    cfg.policy = PolicyConfig(default_mode=PermissionMode.AUTONOMOUS)
    return ToolContext(workspace_root=tmp_path, policy=PolicyEngine(cfg), approver=approver)


def test_registry_dispatch_unknown_tool(tmp_path: Path) -> None:
    reg = default_registry()
    result = reg.dispatch("nonexistent", {}, _ctx(tmp_path))
    assert not result.ok
    assert "unknown tool" in result.error


def test_file_tools_confined_to_workspace(tmp_path: Path) -> None:
    reg = default_registry()
    ctx = _ctx(tmp_path)
    # Write inside workspace works.
    r = reg.dispatch("write_file", {"path": "test.txt", "content": "hello"}, ctx)
    assert r.ok
    # Read inside workspace works.
    r = reg.dispatch("read_file", {"path": "test.txt"}, ctx)
    assert r.ok
    assert "hello" in r.output
    # Read outside workspace is denied.
    r = reg.dispatch("read_file", {"path": str(Path("/etc/passwd"))}, ctx)
    assert not r.ok


def test_write_tool_creates_parent_dirs(tmp_path: Path) -> None:
    reg = default_registry()
    r = reg.dispatch("write_file", {"path": "sub/dir/file.txt", "content": "x"}, _ctx(tmp_path))
    assert r.ok
    assert (tmp_path / "sub" / "dir" / "file.txt").exists()


def test_patch_requires_unique_match(tmp_path: Path) -> None:
    reg = default_registry()
    ctx = _ctx(tmp_path)
    reg.dispatch("write_file", {"path": "f.txt", "content": "aaa bbb aaa"}, ctx)
    # Multiple matches -> fail.
    r = reg.dispatch("patch_file", {"path": "f.txt", "find": "aaa", "replace": "ccc"}, ctx)
    assert not r.ok
    # Unique match -> ok.
    r = reg.dispatch("patch_file", {"path": "f.txt", "find": "bbb", "replace": "ccc"}, ctx)
    assert r.ok


def test_patch_refuses_non_utf8_file(tmp_path: Path) -> None:
    reg = default_registry()
    ctx = _ctx(tmp_path)
    (tmp_path / "bin.dat").write_bytes(b"\xff\xfe binary \x00 stuff target \x80")
    r = reg.dispatch("patch_file", {"path": "bin.dat", "find": "target", "replace": "hit"}, ctx)
    assert not r.ok
    assert "UTF-8" in r.error
    assert b"target" in (tmp_path / "bin.dat").read_bytes()


def test_find_files_skips_outside_workspace(tmp_path: Path) -> None:
    reg = default_registry()
    ctx = _ctx(tmp_path)
    (tmp_path / "inside.txt").write_text("hello")
    r = reg.dispatch("find_files", {"pattern": "*.txt"}, ctx)
    assert r.ok
    assert "inside.txt" in r.output


def test_grep_does_not_crash_on_symlink_escape(tmp_path: Path) -> None:
    reg = default_registry()
    ctx = _ctx(tmp_path)
    (tmp_path / "match.txt").write_text("needle here")
    r = reg.dispatch("grep_files", {"pattern": "needle"}, ctx)
    assert r.ok
    assert "match.txt" in r.output

