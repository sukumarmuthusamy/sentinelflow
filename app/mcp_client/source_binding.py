"""Bind every change-scoped MCP tool call in one analysis to a single change source.

Agents and graph nodes call the same tools with the same arguments in fixture and
GitHub mode. The runner wraps the MCP client once per analysis, and this wrapper
sets ``source`` on each change-scoped call. A run therefore cannot mix sources, and
agent code never chooses where change data comes from.
"""

from __future__ import annotations

from typing import Any, Literal, TypeVar

from mcp.types import CallToolResult, ReadResourceResult, Resource, Tool
from pydantic import BaseModel

from app.mcp_client.port import McpClientPort

ChangeSource = Literal["fixture", "github"]

# Tools whose data depends on where the change came from.
CHANGE_SCOPED_TOOLS: frozenset[str] = frozenset(
    {
        "get_change_package",
        "get_dependency_graph",
        "diff_contracts",
        "list_tests",
        "run_tests",
        "run_security_scan",
    },
)

T = TypeVar("T", bound=BaseModel)


class SourceBoundMcpClient:
    """McpClientPort decorator that pins ``source`` on change-scoped tool calls."""

    def __init__(self, inner: McpClientPort, *, source: ChangeSource) -> None:
        self._inner = inner
        self.source: ChangeSource = source

    def _bind(self, name: str, arguments: dict[str, Any] | None) -> dict[str, Any]:
        bound = dict(arguments or {})
        if name in CHANGE_SCOPED_TOOLS:
            bound["source"] = self.source
        return bound

    async def list_tools(self) -> list[Tool]:
        return await self._inner.list_tools()

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> CallToolResult:
        return await self._inner.call_tool(name, self._bind(name, arguments))

    async def read_resource(self, uri: str) -> ReadResourceResult:
        return await self._inner.read_resource(uri)

    async def list_resources(self) -> list[Resource]:
        return await self._inner.list_resources()

    async def call_tool_typed(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        response_model: type[T],
    ) -> T:
        return await self._inner.call_tool_typed(
            name,
            self._bind(name, arguments),
            response_model=response_model,
        )


def bind_change_source(mcp: McpClientPort, source: ChangeSource) -> McpClientPort:
    """Fixture runs keep the client untouched; other sources get the binding wrapper."""
    if source == "fixture":
        return mcp
    return SourceBoundMcpClient(mcp, source=source)
