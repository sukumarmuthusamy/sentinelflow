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

from app.agents.observable import observable_change_package
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
        # Labels (description, category, fixture path) never reach the agent.
        "change_package": observable_change_package(package),
        "dependency_graph": graph.model_dump(),
        "contract_diff": contracts.model_dump(),
    }


MAX_RUN_TEST_IDS = 64


def runnable_test_ids(inventory: ChangeTestInventory) -> list[str]:
    """Required tests that the inventory also lists as available, in stable order."""
    available = set(inventory.available_tests)
    return [test_id for test_id in inventory.required_tests if test_id in available][
        :MAX_RUN_TEST_IDS
    ]


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
    # Only the required tests the inventory allowlists; always a dry run (live
    # sandboxed execution is M7). No eligible tests means no run_tests call at all.
    test_ids = runnable_test_ids(inventory)
    if execute_tests and test_ids:
        run_result = await mcp.call_tool_typed(
            "run_tests",
            {
                "change_id": change_id,
                "domain_pack_id": domain_pack_id,
                "test_ids": test_ids,
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
