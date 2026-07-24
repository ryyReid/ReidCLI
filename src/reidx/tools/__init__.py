from reidx.tools.base import BaseTool, ToolContext, ToolDefinition, ToolResult
from reidx.tools.file_tools import (
    FindFilesTool,
    GrepFilesTool,
    ListDirTool,
    PatchFileTool,
    ReadFileTool,
    WriteFileTool,
    register_file_tools,
)
from reidx.tools.provider_tools import (
    ConnectProviderTool,
    register_provider_tools,
)
from reidx.tools.registry import ToolRegistry
from reidx.tools.session_tools import SetContextWindowTool, register_session_tools
from reidx.tools.shell_tool import RunCommandTool, register_shell_tool
from reidx.tools.spawn_agent import SpawnAgentTool, register_spawn_agent
from reidx.tools.web_tools import WebSearchTool, register_web_tools

__all__ = [
    "BaseTool",
    "ConnectProviderTool",
    "FindFilesTool",
    "GrepFilesTool",
    "ListDirTool",
    "PatchFileTool",
    "ReadFileTool",
    "RunCommandTool",
    "SetContextWindowTool",
    "SpawnAgentTool",
    "ToolContext",
    "ToolDefinition",
    "ToolRegistry",
    "ToolResult",
    "WebSearchTool",
    "WriteFileTool",
    "register_file_tools",
    "register_provider_tools",
    "register_session_tools",
    "register_shell_tool",
    "register_spawn_agent",
    "register_web_tools",
]


def default_registry() -> ToolRegistry:
    reg = ToolRegistry()
    register_file_tools(reg)
    register_shell_tool(reg)
    register_web_tools(reg)
    register_spawn_agent(reg)
    register_session_tools(reg)
    register_provider_tools(reg)
    return reg
