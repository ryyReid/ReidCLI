"""File tools: read, write, patch, list, find, grep.

All file tools confine access to the workspace root plus any additional writable
roots via _safe_path(). Path traversal outside the workspace is denied. patch_file
is exact single-match string replacement — honest scaffolding; structured edits
and diff generation are TODO (see roadmap Phase 5).
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from reidx.policy.models import PermissionDecision, RiskLevel
from reidx.tools.base import BaseTool, ToolContext, ToolDefinition, ToolResult

_READ_PARAMS = {
    "type": "object",
    "properties": {"path": {"type": "string", "description": "Path relative to workspace root."}},
    "required": ["path"],
}
_WRITE_PARAMS = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "content": {"type": "string"},
    },
    "required": ["path", "content"],
}
_PATCH_PARAMS = {
    "type": "object",
    "properties": {
        "path": {"type": "string"},
        "find": {"type": "string", "description": "Exact substring to locate (single match)."},
        "replace": {"type": "string"},
    },
    "required": ["path", "find", "replace"],
}
_LIST_PARAMS = {
    "type": "object",
    "properties": {"path": {"type": "string", "description": "Subdirectory relative to root. Defaults to root."}},
}
_FIND_PARAMS = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "glob pattern, e.g. '**/*.py'"},
        "path": {"type": "string"},
    },
    "required": ["pattern"],
}
_GREP_PARAMS = {
    "type": "object",
    "properties": {
        "pattern": {"type": "string", "description": "regex pattern"},
        "path": {"type": "string"},
    },
    "required": ["pattern"],
}


def _resolve(path_str: str, ctx: ToolContext) -> Path:
    p = Path(path_str)
    if not p.is_absolute():
        p = ctx.workspace_root / p
    return p


def _safe_read(path: Path, ctx: ToolContext) -> ToolResult | None:
    decision = ctx.policy.check_path(path, write=False)
    if decision is PermissionDecision.DENY:
        return ToolResult.fail(f"path outside workspace: {path}")
    if decision is PermissionDecision.PROMPT:
        if ctx.resolve_decision(f"Allow reading {path}?") is PermissionDecision.DENY:
            return ToolResult.fail(f"read denied by user: {path}")
    return None


def _safe_write(path: Path, ctx: ToolContext) -> ToolResult | None:
    decision = ctx.policy.check_path(path, write=True)
    if decision is PermissionDecision.DENY:
        return ToolResult.fail(f"write outside workspace: {path}")
    if decision is PermissionDecision.PROMPT:
        if ctx.resolve_decision(f"Allow writing {path}?") is PermissionDecision.DENY:
            return ToolResult.fail(f"write denied by user: {path}")
    return None


class ReadFileTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="read_file", description="Read a file's text content.", parameters=_READ_PARAMS, risk=RiskLevel.LOW)

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(str(args.get("path", "")), ctx)
        blocked = _safe_read(path, ctx)
        if blocked:
            return blocked
        if not path.exists() or not path.is_file():
            return ToolResult.fail(f"not a file: {path}")
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            return ToolResult.fail(f"read error: {exc}")
        return ToolResult.ok_(text, path=str(path), bytes=len(text))


class WriteFileTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="write_file", description="Create or overwrite a file.", parameters=_WRITE_PARAMS, risk=RiskLevel.MEDIUM)

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(str(args.get("path", "")), ctx)
        blocked = _safe_write(path, ctx)
        if blocked:
            return blocked
        content = str(args.get("content", ""))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except OSError as exc:
            return ToolResult.fail(f"write error: {exc}")
        return ToolResult.ok_(f"wrote {len(content)} bytes to {path}", path=str(path))


class PatchFileTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(
            name="patch_file",
            description="Replace one exact substring occurrence in a file.",
            parameters=_PATCH_PARAMS,
            risk=RiskLevel.MEDIUM,
        )

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        path = _resolve(str(args.get("path", "")), ctx)
        blocked = _safe_write(path, ctx)
        if blocked:
            return blocked
        find = str(args.get("find", ""))
        replace = str(args.get("replace", ""))
        if not find:
            return ToolResult.fail("find string is empty")
        if not path.exists():
            return ToolResult.fail(f"not a file: {path}")
        text = path.read_text(encoding="utf-8", errors="replace")
        count = text.count(find)
        if count == 0:
            return ToolResult.fail("find string not present")
        if count > 1:
            return ToolResult.fail(f"find string matches {count} times; patch requires a unique match (TODO: structured edits)")
        new_text = text.replace(find, replace, 1)
        path.write_text(new_text, encoding="utf-8")
        return ToolResult.ok_("patched", path=str(path))


class ListDirTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="list_dir", description="List entries in a directory.", parameters=_LIST_PARAMS, risk=RiskLevel.LOW)

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        rel = str(args.get("path", ""))
        path = _resolve(rel, ctx) if rel else ctx.workspace_root
        blocked = _safe_read(path, ctx)
        if blocked:
            return blocked
        if not path.is_dir():
            return ToolResult.fail(f"not a directory: {path}")
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in path.iterdir())
        return ToolResult.ok_("\n".join(entries), count=len(entries))


class FindFilesTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="find_files", description="Find files matching a glob pattern.", parameters=_FIND_PARAMS, risk=RiskLevel.LOW)

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(args.get("pattern", "*"))
        base = _resolve(str(args.get("path", "")), ctx) if args.get("path") else ctx.workspace_root
        blocked = _safe_read(base, ctx)
        if blocked:
            return blocked
        matches = sorted(str(p.relative_to(ctx.workspace_root)) for p in base.glob(pattern) if p.is_file())
        return ToolResult.ok_("\n".join(matches), count=len(matches))


class GrepFilesTool(BaseTool):
    @property
    def definition(self) -> ToolDefinition:
        return ToolDefinition(name="grep_files", description="Search file contents with a regex.", parameters=_GREP_PARAMS, risk=RiskLevel.LOW)

    def execute(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        pattern = str(args.get("pattern", ""))
        base = _resolve(str(args.get("path", "")), ctx) if args.get("path") else ctx.workspace_root
        blocked = _safe_read(base, ctx)
        if blocked:
            return blocked
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            return ToolResult.fail(f"bad regex: {exc}")
        hits: list[str] = []
        for p in base.rglob("*"):
            if not p.is_file():
                continue
            try:
                for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{p.relative_to(ctx.workspace_root)}:{i}: {line.strip()}")
            except OSError:
                continue
            if len(hits) >= 200:
                break
        return ToolResult.ok_("\n".join(hits), count=len(hits))


def register_file_tools(registry) -> None:  # type: ignore[no-untyped-def]
    for tool in (ReadFileTool(), WriteFileTool(), PatchFileTool(), ListDirTool(), FindFilesTool(), GrepFilesTool()):
        registry.register(tool)
