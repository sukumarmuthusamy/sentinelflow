"""Deterministic signals assembled from MCP tool calls (source-agnostic).

Fixture runs build signals from the fixture directory, as in M1. Live sources have no
local directory, so the deterministic node asks the MCP server for the same pieces
the agents use: the change package, dependency graph, contract diff, security scan
and test inventory. Labels never enter: the package's description and category are
not read, and verification-result fields keep their "not yet measured" defaults.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from mcp_server.sentinelflow_context.models import (
    ChangePackageResponse,
    ChangeTestInventory,
    ContractDiffToolResponse,
    DependencyGraphResponse,
    SecurityScanToolResponse,
)

from app.deterministic.models import DeterministicSignals
from app.mcp_client.errors import ChangeSourceError, McpProtocolError, McpToolError
from app.mcp_client.port import McpClientPort

SIGNAL_TOOL_CALLS = 5


@dataclass
class McpChangeSignals:
    """Signals plus what the source reported about their completeness."""

    signals: DeterministicSignals
    changed_files: list[str]
    gaps: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


async def signals_via_mcp(
    mcp: McpClientPort,
    *,
    change_id: str,
    domain_pack_id: str,
) -> McpChangeSignals:
    """Collect deterministic signals through the MCP boundary.

    A tool error means the source could not supply the change at all (unknown PR,
    rate limit, outage) and is raised as ``ChangeSourceError``. Partial data comes back
    as ``gaps`` for the caller to act on; nothing missing is filled with a default.
    """
    args = {"change_id": change_id, "domain_pack_id": domain_pack_id}
    try:
        package = await mcp.call_tool_typed(
            "get_change_package", args, response_model=ChangePackageResponse
        )
        graph = await mcp.call_tool_typed(
            "get_dependency_graph", args, response_model=DependencyGraphResponse
        )
        contracts = await mcp.call_tool_typed(
            "diff_contracts", args, response_model=ContractDiffToolResponse
        )
        scan = await mcp.call_tool_typed(
            "run_security_scan", args, response_model=SecurityScanToolResponse
        )
        tests = await mcp.call_tool_typed("list_tests", args, response_model=ChangeTestInventory)
    except (McpToolError, McpProtocolError) as exc:
        raise ChangeSourceError(str(exc)) from exc

    signals = DeterministicSignals(
        change_id=package.change_id,
        primary_service=package.primary_service,
        impacted_services=graph.impacted_services,
        blast_radius=graph.blast_radius,
        contract_diff=contracts.result,
        security=scan.result,
        required_tests=tests.required_tests,
        available_tests=tests.available_tests,
    )
    return McpChangeSignals(
        signals=signals,
        changed_files=list(package.changed_files),
        gaps=list(package.gaps),
        notes=list(package.notes),
    )
