"""Tool implementations delegating to the M1 deterministic layer."""

from __future__ import annotations

from pathlib import Path

from app.core.domain_pack import DomainPack, load_domain_pack
from app.deterministic.contract_diff import resolve_contract_diff
from app.deterministic.dependency_graph import (
    build_dependency_graph,
    graph_edges,
    resolve_change_blast_radius,
    resolve_impacted_services,
)
from app.deterministic.evaluate import (
    load_change_fixture_manifest,
    resolve_fixture_directory,
)
from app.deterministic.security_scanners import run_security_scans
from app.graph.state import ReleaseDecision
from mcp.server.mcpserver.exceptions import ToolError

from mcp_server.sentinelflow_context.audit_log import record_gate_decision
from mcp_server.sentinelflow_context.models import (
    ChangePackageResponse,
    ChangeTestInventory,
    ContractDiffToolResponse,
    DependencyGraphResource,
    DependencyGraphResponse,
    RecordGateDecisionResponse,
    ReleasePolicyResource,
    RunTestsResponse,
    SecurityScanToolResponse,
    ServiceCatalogResource,
)
from mcp_server.sentinelflow_context.settings import McpServerSettings

SANDBOX_DOCKER_FLAGS = [
    "--network=none",
    "--read-only",
    "--cap-drop=ALL",
    "--security-opt=no-new-privileges",
    "--pids-limit=64",
    "--memory=512m",
    "--cpus=1",
    "--user=nobody",
]

GITHUB_CHANGE_SOURCE_TODO = (
    "Live GitHub change packages are deferred until a seeded public repository is "
    "available (see SENTINELFLOW_PLAN.md section 15, open question 3). Use "
    "source='fixture' for now."
)


def _resolve_pack_id(settings: McpServerSettings, domain_pack_id: str | None) -> str:
    return domain_pack_id or settings.default_domain_pack_id


def _load_pack(settings: McpServerSettings, domain_pack_id: str | None) -> DomainPack:
    pack_id = _resolve_pack_id(settings, domain_pack_id)
    try:
        return load_domain_pack(pack_id, domains_root=settings.domains_root)
    except Exception as exc:  # noqa: BLE001 — surface as ToolError
        raise ToolError(f"domain pack not found: {pack_id}") from exc


def _resolve_fixture(
    settings: McpServerSettings,
    *,
    change_id: str,
    domain_pack_id: str | None,
) -> tuple[Path, DomainPack]:
    pack = _load_pack(settings, domain_pack_id)
    try:
        fixture_dir = resolve_fixture_directory(
            change_id,
            pack_id=pack.pack_id,
            domains_root=settings.domains_root,
        )
    except FileNotFoundError as exc:
        raise ToolError(str(exc)) from exc
    return fixture_dir, pack


def get_change_package(
    settings: McpServerSettings,
    *,
    change_id: str,
    domain_pack_id: str | None = None,
    source: str = "fixture",
) -> ChangePackageResponse:
    """Load a change package from fixtures or stub the GitHub path."""
    if source == "github":
        raise ToolError(GITHUB_CHANGE_SOURCE_TODO)

    fixture_dir, pack = _resolve_fixture(
        settings,
        change_id=change_id,
        domain_pack_id=domain_pack_id,
    )
    manifest = load_change_fixture_manifest(fixture_dir)
    return ChangePackageResponse(
        change_id=manifest.change_id,
        source="fixture",
        domain_pack_id=pack.pack_id,
        description=manifest.description,
        category=manifest.category,
        primary_service=manifest.primary_service,
        changed_files=manifest.changed_files,
        origin_nodes=manifest.origin_nodes,
        fixture_path=str(fixture_dir.relative_to(settings.domains_root.parent)),
    )


def get_dependency_graph(
    settings: McpServerSettings,
    *,
    change_id: str,
    domain_pack_id: str | None = None,
) -> DependencyGraphResponse:
    """Build the dependency graph and blast radius for a fixture change."""
    fixture_dir, pack = _resolve_fixture(
        settings,
        change_id=change_id,
        domain_pack_id=domain_pack_id,
    )
    manifest = load_change_fixture_manifest(fixture_dir)
    repo_root = fixture_dir / "repo"
    graph = build_dependency_graph(
        domain_pack=pack,
        repo_root=repo_root if repo_root.is_dir() else None,
        module_service_map=manifest.module_service_map,
    )
    origins = manifest.origin_nodes or [manifest.primary_service]
    blast_radius = resolve_change_blast_radius(
        graph,
        origins,
        max_depth=manifest.blast_radius_max_depth,
        domain_pack=pack,
    )
    impacted = resolve_impacted_services(
        graph,
        blast_radius,
        domain_pack=pack,
        module_service_map=manifest.module_service_map,
        primary_service=manifest.primary_service,
        origin_nodes=origins,
    )
    return DependencyGraphResponse(
        change_id=manifest.change_id,
        domain_pack_id=pack.pack_id,
        edges=graph_edges(graph),
        blast_radius=blast_radius,
        impacted_services=impacted,
    )


def diff_contracts(
    settings: McpServerSettings,
    *,
    change_id: str,
    domain_pack_id: str | None = None,
    fixture_only: bool = True,
) -> ContractDiffToolResponse:
    """Run contract diff for a fixture change."""
    fixture_dir, pack = _resolve_fixture(
        settings,
        change_id=change_id,
        domain_pack_id=domain_pack_id,
    )
    manifest = load_change_fixture_manifest(fixture_dir)
    contract_base = (
        fixture_dir / manifest.contract_base if manifest.contract_base else None
    )
    contract_revision = (
        fixture_dir / manifest.contract_revision if manifest.contract_revision else None
    )
    contract_fixture = fixture_dir / "signals" / "contract_diff.json"
    if fixture_only and contract_fixture.is_file():
        from app.deterministic.contract_diff import load_contract_diff_fixture

        result = load_contract_diff_fixture(contract_fixture)
    else:
        result = resolve_contract_diff(
            base_spec=contract_base,
            revision_spec=contract_revision,
            fixture_signals=contract_fixture,
        )
    return ContractDiffToolResponse(
        change_id=manifest.change_id,
        domain_pack_id=pack.pack_id,
        result=result,
    )


def list_tests(
    settings: McpServerSettings,
    *,
    change_id: str,
    domain_pack_id: str | None = None,
) -> ChangeTestInventory:
    """Return required and available tests for a fixture change."""
    fixture_dir, pack = _resolve_fixture(
        settings,
        change_id=change_id,
        domain_pack_id=domain_pack_id,
    )
    manifest = load_change_fixture_manifest(fixture_dir)
    inventory_file = fixture_dir / "tests" / "inventory.json"
    available = list(manifest.available_tests)
    if inventory_file.is_file():
        import json

        payload = json.loads(inventory_file.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and isinstance(payload.get("available_tests"), list):
            available = [str(item) for item in payload["available_tests"]]

    required = list(manifest.required_tests)
    missing = [test for test in required if test not in set(available)]
    return ChangeTestInventory(
        change_id=manifest.change_id,
        domain_pack_id=pack.pack_id,
        required_tests=required,
        available_tests=available,
        missing_tests=missing,
    )


def run_tests(
    settings: McpServerSettings,
    *,
    change_id: str,
    test_ids: list[str],
    domain_pack_id: str | None = None,
    dry_run: bool = True,
) -> RunTestsResponse:
    """Dry-run test execution by default; live sandbox deferred to M7."""
    fixture_dir, pack = _resolve_fixture(
        settings,
        change_id=change_id,
        domain_pack_id=domain_pack_id,
    )
    manifest = load_change_fixture_manifest(fixture_dir)
    inventory = list_tests(
        settings,
        change_id=change_id,
        domain_pack_id=pack.pack_id,
    )
    allowed = set(inventory.available_tests)
    disallowed = [test_id for test_id in test_ids if test_id not in allowed]
    if disallowed:
        raise ToolError(
            "test_ids must be listed in the fixture inventory: "
            + ", ".join(disallowed),
        )

    if dry_run:
        return RunTestsResponse(
            change_id=manifest.change_id,
            domain_pack_id=pack.pack_id,
            dry_run=True,
            test_ids=test_ids,
            status="dry_run",
            message=f"Would run {len(test_ids)} allowlisted test(s) for {manifest.change_id}.",
            planned_docker_flags=SANDBOX_DOCKER_FLAGS,
        )

    return RunTestsResponse(
        change_id=manifest.change_id,
        domain_pack_id=pack.pack_id,
        dry_run=False,
        test_ids=test_ids,
        status="deferred",
        message=(
            "Live sandboxed test execution is deferred to M7. "
            "Use dry_run=True for planning output."
        ),
        planned_docker_flags=SANDBOX_DOCKER_FLAGS,
    )


def run_security_scan(
    settings: McpServerSettings,
    *,
    change_id: str,
    domain_pack_id: str | None = None,
    fixture_only: bool = True,
) -> SecurityScanToolResponse:
    """Run security scanners for a fixture change."""
    fixture_dir, pack = _resolve_fixture(
        settings,
        change_id=change_id,
        domain_pack_id=domain_pack_id,
    )
    manifest = load_change_fixture_manifest(fixture_dir)
    repo_root = fixture_dir / "repo"
    lockfile = fixture_dir / manifest.lockfile if manifest.lockfile else None
    security_fixture = fixture_dir / "signals" / "security_scan.json"
    if fixture_only and security_fixture.is_file():
        from app.deterministic.security_scanners import load_security_fixture

        result = load_security_fixture(security_fixture)
    else:
        result = run_security_scans(
            repo_root=repo_root if repo_root.is_dir() else fixture_dir,
            lockfile=lockfile,
            fixture_signals=security_fixture,
        )
    return SecurityScanToolResponse(
        change_id=manifest.change_id,
        domain_pack_id=pack.pack_id,
        result=result,
    )


def record_gate_decision_tool(
    settings: McpServerSettings,
    *,
    analysis_id: str,
    decision: str,
    evidence_hash: str,
    idempotency_key: str,
) -> RecordGateDecisionResponse:
    """Append a gate decision to the audit log."""
    return record_gate_decision(
        audit_log_path=settings.audit_log_path,
        analysis_id=analysis_id,
        decision=ReleaseDecision(decision),
        evidence_hash=evidence_hash,
        idempotency_key=idempotency_key,
    )


def read_service_catalog(settings: McpServerSettings) -> ServiceCatalogResource:
    """Return the current domain pack service catalog."""
    pack = _load_pack(settings, None)
    services = [
        {
            "name": service.name,
            "criticality": service.criticality,
            "data_classification": service.data_classification,
            "owner": service.owner,
            "description": service.description or "",
        }
        for service in pack.domain.services
    ]
    return ServiceCatalogResource(domain_pack_id=pack.pack_id, services=services)


def read_release_policy(settings: McpServerSettings) -> ReleasePolicyResource:
    """Return the current domain pack release policy."""
    pack = _load_pack(settings, None)
    policy = pack.policy.model_dump(mode="json")
    return ReleasePolicyResource(domain_pack_id=pack.pack_id, policy=policy)


def read_dependency_graph_resource(settings: McpServerSettings) -> DependencyGraphResource:
    """Return the seed dependency graph for the current domain pack."""
    pack = _load_pack(settings, None)
    nodes = [{"id": node.id, "type": node.type} for node in pack.dependencies.nodes]
    edges = [
        {
            "source": edge.source,
            "target": edge.target,
            "kind": edge.kind,
            "provenance": edge.provenance,
        }
        for edge in pack.dependencies.edges
    ]
    return DependencyGraphResource(
        domain_pack_id=pack.pack_id,
        nodes=nodes,
        edges=edges,
    )
