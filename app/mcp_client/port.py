"""Port interface for MCP client adapters."""

from __future__ import annotations

from typing import Any, Protocol

from mcp.types import CallToolResult, ReadResourceResult, Resource, Tool


class McpClientPort(Protocol):
    """Narrow MCP boundary used by future graph nodes and agents."""

    async def list_tools(self) -> list[Tool]:
        """Return tools exposed by the connected server."""
        ...

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
        """Invoke a tool by name."""
        ...

    async def read_resource(self, uri: str) -> ReadResourceResult:
        """Read a resource by URI."""
        ...

    async def list_resources(self) -> list[Resource]:
        """Return resources exposed by the connected server."""
        ...
