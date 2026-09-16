"""Contract tests for sentinelflow-context MCP resources."""

from __future__ import annotations

import json

import pytest
from app.mcp_client import McpProtocolError, SentinelFlowMcpAdapter

from tests.contract.conftest import mcp_session


class TestResources:
    async def test_service_catalog_current(self) -> None:
        async with mcp_session() as adapter:
            result = await adapter.read_resource("service-catalog://current")
            payload = json.loads(SentinelFlowMcpAdapter.resource_text(result))
            assert "services" in payload

    async def test_release_policy_current(self) -> None:
        async with mcp_session() as adapter:
            result = await adapter.read_resource("release-policy://current")
            payload = json.loads(SentinelFlowMcpAdapter.resource_text(result))
            assert "policy" in payload

    async def test_dependency_graph_current(self) -> None:
        async with mcp_session() as adapter:
            result = await adapter.read_resource("dependency-graph://current")
            payload = json.loads(SentinelFlowMcpAdapter.resource_text(result))
            assert "nodes" in payload

    async def test_invalid_resource_uri(self) -> None:
        async with mcp_session() as adapter:
            with pytest.raises(McpProtocolError):
                await adapter.read_resource("service-catalog://unknown")


class TestMcpInspectorDiscovery:
    """Programmatic MCP Inspector-style discovery checks."""

    EXPECTED_TOOLS = {
        "get_change_package",
        "get_dependency_graph",
        "diff_contracts",
        "list_tests",
        "run_tests",
        "run_security_scan",
        "record_gate_decision",
    }

    EXPECTED_RESOURCES = {
        "service-catalog://current",
        "release-policy://current",
        "dependency-graph://current",
    }

    async def test_all_tools_and_resources_discoverable(self) -> None:
        async with mcp_session() as adapter:
            tools = await adapter.list_tools()
            tool_names = {tool.name for tool in tools}
            assert self.EXPECTED_TOOLS.issubset(tool_names)

            resources = await adapter.list_resources()
            resource_uris = {str(item.uri) for item in resources}
            assert self.EXPECTED_RESOURCES.issubset(resource_uris)
