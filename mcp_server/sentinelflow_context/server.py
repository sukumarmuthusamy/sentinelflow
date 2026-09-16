"""sentinelflow-context MCP server exposing M1 deterministic signals."""

from __future__ import annotations

from mcp.server import MCPServer

from mcp_server.sentinelflow_context import services
from mcp_server.sentinelflow_context.models import (
    ChangePackageResponse,
    ChangeTestInventory,
    ContractDiffToolResponse,
    DependencyGraphResponse,
    RecordGateDecisionResponse,
    RunTestsResponse,
    SecurityScanToolResponse,
)
from mcp_server.sentinelflow_context.settings import get_mcp_server_settings
from mcp_server.sentinelflow_context.validation import (
    validate_analysis_id,
    validate_change_id,
    validate_decision,
    validate_domain_pack_id,
    validate_evidence_hash,
    validate_idempotency_key,
    validate_source,
    validate_test_ids,
)

mcp = MCPServer(
    "sentinelflow-context",
    instructions=(
        "Deterministic engineering context for SentinelFlow: change packages, "
        "dependency graphs, contract diffs, tests, security scans, and gate audit."
    ),
)


@mcp.tool()
def get_change_package(
    change_id: str,
    domain_pack_id: str | None = None,
    source: str = "fixture",
) -> ChangePackageResponse:
    """Load a change package from fixtures or the deferred GitHub integration."""
    settings = get_mcp_server_settings()
    return services.get_change_package(
        settings,
        change_id=validate_change_id(change_id),
        domain_pack_id=validate_domain_pack_id(domain_pack_id) if domain_pack_id else None,
        source=validate_source(source),
    )


@mcp.tool()
def get_dependency_graph(
    change_id: str,
    domain_pack_id: str | None = None,
) -> DependencyGraphResponse:
    """Return the provenance-tagged dependency graph and blast radius for a change."""
    settings = get_mcp_server_settings()
    return services.get_dependency_graph(
        settings,
        change_id=validate_change_id(change_id),
        domain_pack_id=validate_domain_pack_id(domain_pack_id) if domain_pack_id else None,
    )


@mcp.tool()
def diff_contracts(
    change_id: str,
    domain_pack_id: str | None = None,
    fixture_only: bool = True,
) -> ContractDiffToolResponse:
    """Diff OpenAPI contracts for a fixture change via oasdiff or fixture signals."""
    settings = get_mcp_server_settings()
    return services.diff_contracts(
        settings,
        change_id=validate_change_id(change_id),
        domain_pack_id=validate_domain_pack_id(domain_pack_id) if domain_pack_id else None,
        fixture_only=fixture_only,
    )


@mcp.tool()
def list_tests(
    change_id: str,
    domain_pack_id: str | None = None,
) -> ChangeTestInventory:
    """List required and available tests for a fixture change."""
    settings = get_mcp_server_settings()
    return services.list_tests(
        settings,
        change_id=validate_change_id(change_id),
        domain_pack_id=validate_domain_pack_id(domain_pack_id) if domain_pack_id else None,
    )


@mcp.tool()
def run_tests(
    change_id: str,
    test_ids: list[str],
    domain_pack_id: str | None = None,
    dry_run: bool = True,
) -> RunTestsResponse:
    """Plan or defer sandboxed test execution for allowlisted tests."""
    settings = get_mcp_server_settings()
    return services.run_tests(
        settings,
        change_id=validate_change_id(change_id),
        test_ids=validate_test_ids(test_ids),
        domain_pack_id=validate_domain_pack_id(domain_pack_id) if domain_pack_id else None,
        dry_run=dry_run,
    )


@mcp.tool()
def run_security_scan(
    change_id: str,
    domain_pack_id: str | None = None,
    fixture_only: bool = True,
) -> SecurityScanToolResponse:
    """Run gitleaks, semgrep, and osv-scanner wrappers for a fixture change."""
    settings = get_mcp_server_settings()
    return services.run_security_scan(
        settings,
        change_id=validate_change_id(change_id),
        domain_pack_id=validate_domain_pack_id(domain_pack_id) if domain_pack_id else None,
        fixture_only=fixture_only,
    )


@mcp.tool()
def record_gate_decision(
    analysis_id: str,
    decision: str,
    evidence_hash: str,
    idempotency_key: str,
) -> RecordGateDecisionResponse:
    """Append an idempotent gate decision record to the audit log."""
    settings = get_mcp_server_settings()
    return services.record_gate_decision_tool(
        settings,
        analysis_id=validate_analysis_id(analysis_id),
        decision=validate_decision(decision),
        evidence_hash=validate_evidence_hash(evidence_hash),
        idempotency_key=validate_idempotency_key(idempotency_key),
    )


@mcp.resource("service-catalog://current", mime_type="application/json")
def service_catalog_current() -> dict[str, object]:
    """Current domain pack service catalog."""
    settings = get_mcp_server_settings()
    return services.read_service_catalog(settings).model_dump(mode="json")


@mcp.resource("release-policy://current", mime_type="application/json")
def release_policy_current() -> dict[str, object]:
    """Current domain pack release policy."""
    settings = get_mcp_server_settings()
    return services.read_release_policy(settings).model_dump(mode="json")


@mcp.resource("dependency-graph://current", mime_type="application/json")
def dependency_graph_current() -> dict[str, object]:
    """Current domain pack seed dependency graph."""
    settings = get_mcp_server_settings()
    return services.read_dependency_graph_resource(settings).model_dump(mode="json")
