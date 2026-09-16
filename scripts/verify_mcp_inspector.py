#!/usr/bin/env python3
"""Verify sentinelflow-context MCP tools and resources (Inspector-style discovery)."""

from __future__ import annotations

import asyncio
import json
import sys

from app.mcp_client import SentinelFlowMcpAdapter
from mcp_server.sentinelflow_context.models import ChangePackageResponse
from mcp_server.sentinelflow_context.server import mcp

EXPECTED_TOOLS = [
    "get_change_package",
    "get_dependency_graph",
    "diff_contracts",
    "list_tests",
    "run_tests",
    "run_security_scan",
    "record_gate_decision",
]

EXPECTED_RESOURCES = [
    "service-catalog://current",
    "release-policy://current",
    "dependency-graph://current",
]


async def _verify() -> int:
    async with SentinelFlowMcpAdapter(mcp) as adapter:
        tools = await adapter.list_tools()
        tool_names = sorted(tool.name for tool in tools)
        print("Tools discovered:")
        for name in tool_names:
            print(f"  - {name}")

        missing_tools = sorted(set(EXPECTED_TOOLS) - set(tool_names))
        if missing_tools:
            print(f"Missing tools: {missing_tools}", file=sys.stderr)
            return 1

        resources = await adapter.list_resources()
        resource_uris = sorted(str(item.uri) for item in resources)
        print("\nResources discovered:")
        for uri in resource_uris:
            print(f"  - {uri}")

        missing_resources = sorted(set(EXPECTED_RESOURCES) - set(resource_uris))
        if missing_resources:
            print(f"Missing resources: {missing_resources}", file=sys.stderr)
            return 1

        sample = await adapter.call_tool_typed(
            "get_change_package",
            {"change_id": "CHG-H001", "domain_pack_id": "healthcare-claims"},
            response_model=ChangePackageResponse,
        )
        print("\nSample get_change_package:")
        print(f"  change_id={sample.change_id}")
        print(f"  description={sample.description}")

        policy = await adapter.read_resource("release-policy://current")
        policy_payload = json.loads(SentinelFlowMcpAdapter.resource_text(policy))
        print("\nSample release-policy://current keys:", sorted(policy_payload.keys()))

    print("\nMCP Inspector verification passed.")
    return 0


def main() -> int:
    return asyncio.run(_verify())


if __name__ == "__main__":
    raise SystemExit(main())
