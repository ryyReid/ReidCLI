from reidcli.tools.base import BaseTool, ToolContext, ToolDefinition, ToolResult
from reidcli.tools.file_tools import (
    FindFilesTool,
    GrepFilesTool,
    ListDirTool,
    PatchFileTool,
    ReadFileTool,
    WriteFileTool,
    register_file_tools,
)
from reidcli.tools.registry import ToolRegistry
from reidcli.tools.shell_tool import RunCommandTool, register_shell_tool
from reidcli.tools.web_tools import WebSearchTool, register_web_tools

__all__ = [
    "BaseTool",
    "FindFilesTool",
    "GrepFilesTool",
    "ListDirTool",
    "PatchFileTool",
    "ReadFileTool",
    "RunCommandTool",
    "ToolContext",
    "ToolDefinition",
    "ToolRegistry",
    "ToolResult",
    "WebSearchTool",
    "WriteFileTool",
    "register_file_tools",
    "register_shell_tool",
    "register_web_tools",
]


def default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_file_tools(reg)
    register_shell_tool(reg)
    register_web_tools(reg)
    return reg
