"""Stdio entrypoint for the sentinelflow-context MCP server."""

from __future__ import annotations

import asyncio

from mcp_server.sentinelflow_context.server import mcp


def main() -> None:
    """Run the MCP server over stdio transport."""
    asyncio.run(mcp.run_stdio_async())


if __name__ == "__main__":
    main()
