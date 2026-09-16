"""Orchestrate deterministic evaluation for a change fixture."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, Field

from app.core.domain_pack import DomainPack, load_domain_pack
from app.deterministic.contract_diff import resolve_contract_diff
from app.deterministic.dependency_graph import (
    build_dependency_graph,
    resolve_change_blast_radius,
    resolve_impacted_services,
)
from app.deterministic.models import DeterministicSignals, PolicyGateResult
from app.deterministic.policy_gate import apply_policy_gate
from app.deterministic.risk_engine import compute_risk_score
from app.deterministic.security_scanners import run_security_scans
from app.graph.state import ReleaseDecision


class ChangeFixtureManifest(BaseModel):
    """Manifest describing a synthetic change fixture."""

    change_id: str
    description: str
    category: str
    primary_service: str
    origin_nodes: list[str] = Field(default_factory=list)
    module_service_map: dict[str, str] = Field(default_factory=dict)
    changed_files: list[str] = Field(default_factory=list)
    required_tests: list[str] = Field(default_factory=list)
    available_tests: list[str] = Field(default_factory=list)
    evidence_coverage: float | None = None
    critical_claims_unsupported: bool = False
    repair_attempted: bool = False
    blast_radius_max_depth: int = Field(default=5, ge=0)
    contract_base: str | None = None
    contract_revision: str | None = None
    lockfile: str | None = None
    expected_decision: ReleaseDecision | None = None


class DeterministicEvaluationResult(BaseModel):
    """Full deterministic evaluation output for one fixture."""

    change_id: str
    signals: DeterministicSignals
    risk_score: int
    decision: ReleaseDecision
    override_applied: str | None = None
    override_condition: str | None = None
    gate: PolicyGateResult


def resolve_fixture_directory(
    change_id: str,
    *,
    pack_id: str,
    domains_root: Path,
) -> Path:
    """Resolve a fixture directory from pack and change identifiers."""
    fixture_dir = domains_root / pack_id / "fixtures" / "changes" / change_id
    if not fixture_dir.is_dir() or not (fixture_dir / "manifest.yaml").is_file():
        msg = f"Change fixture not found: {pack_id}/{change_id}"
        raise FileNotFoundError(msg)
    return fixture_dir


def load_change_fixture_manifest(fixture_dir: Path) -> ChangeFixtureManifest:
    """Load and validate a fixture manifest.yaml."""
    manifest_path = fixture_dir / "manifest.yaml"
    with manifest_path.open(encoding="utf-8") as handle:
        payload = yaml.safe_load(handle)
    return ChangeFixtureManifest.model_validate(payload)


def build_signals_from_fixture(
    fixture_dir: Path,
    manifest: ChangeFixtureManifest,
    domain_pack: DomainPack,
    *,
    fixture_only: bool = False,
) -> DeterministicSignals:
    """Collect deterministic signals for a fixture without LLM calls."""
    repo_root = fixture_dir / "repo"
    graph = build_dependency_graph(
        domain_pack=domain_pack,
        repo_root=repo_root if repo_root.is_dir() else None,
        module_service_map=manifest.module_service_map,
    )

    if manifest.origin_nodes:
        origins = manifest.origin_nodes
    elif manifest.primary_service:
        origins = [manifest.primary_service]
    else:
        origins = []
    blast_radius = resolve_change_blast_radius(
        graph,
        origins,
        max_depth=manifest.blast_radius_max_depth,
        domain_pack=domain_pack,
    )
    impacted = resolve_impacted_services(
        graph,
        blast_radius,
        domain_pack=domain_pack,
        module_service_map=manifest.module_service_map,
        primary_service=manifest.primary_service,
        origin_nodes=origins,
    )

    contract_base = (
        fixture_dir / manifest.contract_base if manifest.contract_base else None
    )
    contract_revision = (
        fixture_dir / manifest.contract_revision if manifest.contract_revision else None
    )
    contract_fixture = fixture_dir / "signals" / "contract_diff.json"
    if fixture_only and contract_fixture.is_file():
        from app.deterministic.contract_diff import load_contract_diff_fixture

        contract_diff = load_contract_diff_fixture(contract_fixture)
    else:
        contract_diff = resolve_contract_diff(
            base_spec=contract_base,
            revision_spec=contract_revision,
            fixture_signals=contract_fixture,
        )

    lockfile = fixture_dir / manifest.lockfile if manifest.lockfile else None
    security_fixture = fixture_dir / "signals" / "security_scan.json"
    if fixture_only and security_fixture.is_file():
        from app.deterministic.security_scanners import load_security_fixture

        security = load_security_fixture(security_fixture)
    else:
        security = run_security_scans(
            repo_root=repo_root if repo_root.is_dir() else fixture_dir,
            lockfile=lockfile,
            fixture_signals=security_fixture,
        )

    return DeterministicSignals(
        change_id=manifest.change_id,
        primary_service=manifest.primary_service,
        impacted_services=impacted,
        blast_radius=blast_radius,
        contract_diff=contract_diff,
        security=security,
        required_tests=manifest.required_tests,
        available_tests=manifest.available_tests,
        evidence_coverage=manifest.evidence_coverage,
        critical_claims_unsupported=manifest.critical_claims_unsupported,
        repair_attempted=manifest.repair_attempted,
    )


def evaluate_change_fixture(
    fixture_dir: Path,
    *,
    domain_pack: DomainPack | None = None,
    pack_id: str | None = None,
    domains_root: Path | None = None,
    fixture_only: bool = False,
) -> DeterministicEvaluationResult:
    """Evaluate one fixture directory end-to-end with zero LLM calls."""
    manifest = load_change_fixture_manifest(fixture_dir)
    pack = domain_pack
    if pack is None:
        resolved_pack_id = pack_id or fixture_dir.parents[2].name
        pack = load_domain_pack(resolved_pack_id, domains_root=domains_root)

    signals = build_signals_from_fixture(
        fixture_dir,
        manifest,
        pack,
        fixture_only=fixture_only,
    )
    risk = compute_risk_score(signals, pack)
    gate = apply_policy_gate(signals, pack, risk)

    return DeterministicEvaluationResult(
        change_id=manifest.change_id,
        signals=signals,
        risk_score=gate.risk_score,
        decision=gate.decision,
        override_applied=gate.override_applied,
        override_condition=gate.override_condition,
        gate=gate,
    )


def list_change_fixtures(pack_root: Path) -> list[Path]:
    """List fixture directories under a domain pack."""
    fixtures_root = pack_root / "fixtures" / "changes"
    if not fixtures_root.is_dir():
        return []
    return sorted(
        path
        for path in fixtures_root.iterdir()
        if path.is_dir() and (path / "manifest.yaml").is_file()
    )
