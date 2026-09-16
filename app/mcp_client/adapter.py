"""Typed MCP client adapter over the official SDK Client."""

from __future__ import annotations

from types import TracebackType
from typing import Any, TypeVar, cast

from mcp.client import Client
from mcp.server import MCPServer
from mcp.types import CallToolResult, ReadResourceResult, Resource, TextContent, Tool
from pydantic import BaseModel

from app.mcp_client.errors import McpToolError, map_protocol_error
from app.mcp_client.port import McpClientPort

try:
    from mcp.shared.exceptions import MCPError
except ImportError:  # pragma: no cover
    MCPError = Exception  # type: ignore[misc, assignment]

T = TypeVar("T", bound=BaseModel)


class SentinelFlowMcpAdapter(McpClientPort):
    """Default MCP client adapter (ADR-0001)."""

    def __init__(self, server: MCPServer | Client) -> None:
        if isinstance(server, Client):
            self._client = server
            self._owns_client = False
        else:
            self._client = Client(server)
            self._owns_client = True

    async def __aenter__(self) -> SentinelFlowMcpAdapter:
        if self._owns_client:
            await self._client.__aenter__()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        if self._owns_client:
            await self._client.__aexit__(exc_type, exc, tb)

    async def list_tools(self) -> list[Tool]:
        try:
            result = await self._client.list_tools()
        except MCPError as exc:
            raise map_protocol_error(exc) from exc
        return list(result.tools)

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> CallToolResult:
        try:
            return await self._client.call_tool(name, arguments or {})
        except MCPError as exc:
            raise map_protocol_error(exc) from exc

    async def read_resource(self, uri: str) -> ReadResourceResult:
        try:
            return await self._client.read_resource(uri)
        except MCPError as exc:
            raise map_protocol_error(exc) from exc

    async def list_resources(self) -> list[Resource]:
        try:
            result = await self._client.list_resources()
        except MCPError as exc:
            raise map_protocol_error(exc) from exc
        return list(result.resources)

    async def call_tool_typed(
        self,
        name: str,
        arguments: dict[str, Any] | None,
        *,
        response_model: type[T],
    ) -> T:
        """Call a tool and parse structured output into a Pydantic model."""
        result = await self.call_tool(name, arguments)
        if result.is_error:
            message = "tool call failed"
            if result.content:
                first = result.content[0]
                if isinstance(first, TextContent):
                    message = first.text
            raise McpToolError(tool_name=name, message=message)
        payload = result.structured_content
        if payload is None:
            raise McpToolError(tool_name=name, message="tool returned no structured content")
        return response_model.model_validate(payload)

    @staticmethod
    def resource_text(result: ReadResourceResult) -> str:
        """Extract text from a read_resource result."""
        if not result.contents:
            return ""
        first = result.contents[0]
        text = getattr(first, "text", None)
        return cast(str, text or "")
