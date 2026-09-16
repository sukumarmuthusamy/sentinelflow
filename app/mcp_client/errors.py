"""MCP client error mapping."""

from __future__ import annotations

from dataclasses import dataclass

from mcp.shared.exceptions import MCPError


@dataclass
class McpToolError(Exception):
    """Business or validation failure returned by an MCP tool."""

    tool_name: str
    message: str

    def __str__(self) -> str:
        return f"{self.tool_name}: {self.message}"


@dataclass
class McpProtocolError(Exception):
    """Protocol-level MCP failure."""

    message: str
    code: int | None = None

    def __str__(self) -> str:
        if self.code is None:
            return self.message
        return f"MCP protocol error {self.code}: {self.message}"


def map_protocol_error(exc: MCPError) -> McpProtocolError:
    """Convert SDK MCPError into the adapter's protocol error type."""
    return McpProtocolError(message=str(exc), code=getattr(exc, "code", None))
