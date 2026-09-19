"""Deterministic risk scoring engine (no LLM involvement)."""

from __future__ import annotations

from app.core.domain_pack import CriticalityTier, DomainPack, RiskWeights
from app.deterministic.models import (
    BlastRadiusResult,
    ContractDiffResult,
    DeterministicSignals,
    NormBreakdown,
    RiskEngineResult,
    SecurityScanResult,
)


def clamp_score(value: float) -> int:
    """Clamp a weighted score into the inclusive range [0, 100]."""
    return int(max(0.0, min(100.0, round(value))))


def blast_radius_norm(blast_radius: BlastRadiusResult, *, depth_cap: int = 5) -> float:
    """Depth-weighted reverse reachability normalized to [0, 1].

    Note: a single depth-1 affected node yields 0.8 (1 - 1/depth_cap). Revisit
    sensitivity during M6 weight calibration against the golden set.
    """
    if not blast_radius.affected_nodes:
        return 0.0
    weighted = sum(
        1.0 - (min(node.depth, depth_cap) / depth_cap) for node in blast_radius.affected_nodes
    )
    max_weighted = len(blast_radius.affected_nodes)
    return min(1.0, weighted / max(max_weighted, 1))


def critical_service_norm(impacted_services: list[str], domain_pack: DomainPack) -> float:
    """Map impacted service criticality tiers to [0, 1]."""
    if not impacted_services:
        return 0.0
    tier_scores: dict[CriticalityTier, float] = {
        "critical": 1.0,
        "high": 0.75,
        "medium": 0.5,
        "low": 0.25,
    }
    service_map = {service.name: service.criticality for service in domain_pack.domain.services}
    scores = [
        tier_scores.get(service_map[service], 0.25)
        for service in impacted_services
        if service in service_map
    ]
    if not scores:
        return 0.0
    return min(1.0, sum(scores) / len(scores))


def contract_break_norm(contract_diff: ContractDiffResult) -> float:
    """Normalize oasdiff/buf severity counts to [0, 1]."""
    if contract_diff.err_count > 0:
        return 1.0
    if contract_diff.warn_count > 0:
        return 0.6
    if contract_diff.info_count > 0:
        return 0.2
    return 0.0


def security_severity_norm(security: SecurityScanResult) -> float:
    """Normalize scanner severities and finding volume to [0, 1]."""
    severity_score = {
        "none": 0.0,
        "info": 0.1,
        "low": 0.25,
        "medium": 0.5,
        "high": 0.75,
        "critical": 1.0,
    }
    base = severity_score.get(security.max_severity, 0.0)
    volume_boost = min(0.2, len(security.findings) * 0.05)
    return min(1.0, base + volume_boost)


def regression_test_gap_norm(required_tests: list[str], available_tests: list[str]) -> float:
    """Fraction of required tests absent from the inventory, in [0, 1].

    Implements the ``test_gap_norm`` term from SENTINELFLOW_PLAN.md section 4.
    """
    if not required_tests:
        return 0.0
    available = set(available_tests)
    missing = [test for test in required_tests if test not in available]
    return len(missing) / len(required_tests)


def incident_similarity_norm() -> float:
    """Neutral default until M3 retrieval exists.

    RAG-backed incident similarity will replace this constant in M3.
    """
    return 0.0


def evidence_confidence_norm(*, unsupported_assertion_rate: float = 0.0) -> float:
    """Penalty input for unsupported assertions.

    Returns 0.0 during M1 unless explicit unsupported-assertion data is supplied.
    The Evidence Verifier will populate this in M4.
    """
    return max(0.0, min(1.0, unsupported_assertion_rate))


def compute_norms(signals: DeterministicSignals, domain_pack: DomainPack) -> NormBreakdown:
    """Compute all normalized terms for a signal bundle."""
    unsupported_rate = 1.0 if signals.critical_claims_unsupported else 0.0
    return NormBreakdown(
        blast_radius_norm=blast_radius_norm(signals.blast_radius),
        critical_service_norm=critical_service_norm(signals.impacted_services, domain_pack),
        contract_break_norm=contract_break_norm(signals.contract_diff),
        security_severity_norm=security_severity_norm(signals.security),
        test_gap_norm=regression_test_gap_norm(
            signals.required_tests,
            signals.available_tests,
        ),
        incident_similarity_norm=incident_similarity_norm(),
        evidence_confidence_norm=evidence_confidence_norm(
            unsupported_assertion_rate=unsupported_rate
        ),
    )


def apply_weights(norms: NormBreakdown, weights: RiskWeights) -> tuple[float, dict[str, float]]:
    """Apply pack weights to normalized terms.

    Priors are hard-coded in domain policy.yaml for now. Weight calibration against
    the golden set happens in M6 (see SENTINELFLOW_PLAN.md section 4).
    """
    components = {
        "blast_radius": weights.blast_radius * norms.blast_radius_norm,
        "critical_service": weights.critical_service * norms.critical_service_norm,
        "contract_break": weights.contract_break * norms.contract_break_norm,
        "security_severity": weights.security_severity * norms.security_severity_norm,
        "test_gap": weights.test_gap * norms.test_gap_norm,
        "incident_similarity": weights.incident_similarity * norms.incident_similarity_norm,
        "evidence_confidence_penalty": (
            -weights.evidence_confidence_penalty * norms.evidence_confidence_norm
        ),
    }
    total = sum(components.values())
    return total, components


def score_to_decision(score: int, *, pass_max: int, review_max: int) -> str:
    """Map numeric score to PASS / REVIEW / BLOCK using thresholds."""
    if score <= pass_max:
        return "PASS"
    if score <= review_max:
        return "REVIEW"
    return "BLOCK"


def compute_risk_score(signals: DeterministicSignals, domain_pack: DomainPack) -> RiskEngineResult:
    """Compute the deterministic risk score from typed signals."""
    norms = compute_norms(signals, domain_pack)
    total, components = apply_weights(norms, domain_pack.policy.risk_weights)
    return RiskEngineResult(
        risk_score=clamp_score(total),
        norms=norms,
        weighted_components=components,
    )
