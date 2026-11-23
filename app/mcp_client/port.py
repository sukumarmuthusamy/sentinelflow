"""Port interface for MCP client adapters."""

from __future__ import annotations

from typing import Any, Protocol, TypeVar

from mcp.types import CallToolResult, ReadResourceResult, Resource, Tool
from pydantic import BaseModel

T_co = TypeVar("T_co", bound=BaseModel, covariant=True)


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

    async def call_tool_typed(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        response_model: type[T_co],
    ) -> T_co:
        """Invoke a tool and parse structured content into a Pydantic model."""
        ...
