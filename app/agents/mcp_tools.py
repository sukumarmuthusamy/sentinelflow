"""MCP tool allowlists per agent (section 3 boundaries)."""

from __future__ import annotations

import json
from typing import Any

from mcp_server.sentinelflow_context.models import (
    ChangePackageResponse,
    ChangeTestInventory,
    ContractDiffToolResponse,
    DependencyGraphResponse,
    RunTestsResponse,
    SecurityScanToolResponse,
)

from app.mcp_client.port import McpClientPort


async def mcp_change_analyst_context(
    mcp: McpClientPort,
    *,
    change_id: str,
    domain_pack_id: str,
) -> dict[str, Any]:
    """Change Analyst: get_change_package, get_dependency_graph, diff_contracts only."""
    package = await mcp.call_tool_typed(
        "get_change_package",
        {"change_id": change_id, "domain_pack_id": domain_pack_id},
        response_model=ChangePackageResponse,
    )
    graph = await mcp.call_tool_typed(
        "get_dependency_graph",
        {"change_id": change_id, "domain_pack_id": domain_pack_id},
        response_model=DependencyGraphResponse,
    )
    contracts = await mcp.call_tool_typed(
        "diff_contracts",
        {"change_id": change_id, "domain_pack_id": domain_pack_id},
        response_model=ContractDiffToolResponse,
    )
    return {
        "change_package": package.model_dump(),
        "dependency_graph": graph.model_dump(),
        "contract_diff": contracts.model_dump(),
    }


async def mcp_test_strategist_context(
    mcp: McpClientPort,
    *,
    change_id: str,
    domain_pack_id: str,
    execute_tests: bool,
) -> dict[str, Any]:
    inventory = await mcp.call_tool_typed(
        "list_tests",
        {"change_id": change_id, "domain_pack_id": domain_pack_id},
        response_model=ChangeTestInventory,
    )
    run_result: RunTestsResponse | None = None
    if execute_tests:
        run_result = await mcp.call_tool_typed(
            "run_tests",
            {
                "change_id": change_id,
                "domain_pack_id": domain_pack_id,
                "dry_run": True,
            },
            response_model=RunTestsResponse,
        )
    payload: dict[str, Any] = {"test_inventory": inventory.model_dump()}
    if run_result is not None:
        payload["run_tests"] = run_result.model_dump()
    return payload


async def mcp_security_auditor_context(
    mcp: McpClientPort,
    *,
    change_id: str,
    domain_pack_id: str,
) -> dict[str, Any]:
    scan = await mcp.call_tool_typed(
        "run_security_scan",
        {"change_id": change_id, "domain_pack_id": domain_pack_id},
        response_model=SecurityScanToolResponse,
    )
    graph = await mcp.call_tool_typed(
        "get_dependency_graph",
        {"change_id": change_id, "domain_pack_id": domain_pack_id},
        response_model=DependencyGraphResponse,
    )
    return {
        "security_scan": scan.model_dump(),
        "dependency_graph": graph.model_dump(),
    }


def context_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, default=str)
