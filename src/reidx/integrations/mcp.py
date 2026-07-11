"""Integrations layer.

MCP (Model Context Protocol) integration foundation. This is intentionally a clean
scaffold — config-driven server definitions, lifecycle hooks, and tool discovery
slots. No hardcoded paths: server command/args come from config. Actual stdio
spawning and JSON-RPC negotiation are TODO (see roadmap Phase 6).
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from reidx.diagnostics.logger import get_logger

log = get_logger("reidx.integrations")


class MCPServerConfig(BaseModel):
    name: str
    command: str
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    enabled: bool = True
    timeout_seconds: int = 30


class MCPIntegration:
    """Foundation for an MCP server bridge. Lifecycle methods are stubbed."""

    def __init__(self, config: MCPServerConfig) -> None:
        self.config = config
        self._connected = False

    @property
    def connected(self) -> bool:
        return self._connected

    def connect(self) -> None:
        # TODO: spawn subprocess via config.command + args, perform JSON-RPC initialize.
        log.warning("MCP connect stubbed for '%s' (TODO)", self.config.name)
        self._connected = True

    def disconnect(self) -> None:
        self._connected = False

    def discover_tools(self) -> list[dict]:
        # TODO: call tools/list over the established JSON-RPC channel.
        if not self._connected:
            return []
        return []

    def call_tool(self, name: str, arguments: dict) -> dict:
        # TODO: forward tools/call and return the result.
        raise NotImplementedError("MCP tool calls are not implemented yet (TODO)")


class IntegrationRegistry:
    def __init__(self) -> None:
        self._servers: dict[str, MCPIntegration] = {}

    def register(self, config: MCPServerConfig) -> MCPIntegration:
        integration = MCPIntegration(config)
        self._servers[config.name] = integration
        log.info("registered MCP server: %s", config.name)
        return integration

    def get(self, name: str) -> MCPIntegration | None:
        return self._servers.get(name)

    def list(self) -> list[MCPIntegration]:
        return list(self._servers.values())
