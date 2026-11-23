"""Policy gate with hard override rules from domain pack policy.yaml."""

from __future__ import annotations

from pydantic import BaseModel

from app.core.domain_pack import CriticalityTier, DomainPack, PolicyOverride
from app.deterministic.models import (
    DeterministicSignals,
    PolicyGateResult,
    RiskEngineResult,
    Severity,
)
from app.deterministic.risk_engine import score_to_decision
from app.graph.state import ReleaseDecision

_DECISION_RANK: dict[ReleaseDecision, int] = {
    ReleaseDecision.PASS: 0,
    ReleaseDecision.REVIEW: 1,
    ReleaseDecision.BLOCK: 2,
}


def decision_rank(decision: ReleaseDecision) -> int:
    """Safety precedence: PASS < REVIEW < BLOCK."""
    return _DECISION_RANK[decision]


def strictest_decision(*decisions: ReleaseDecision) -> ReleaseDecision:
    """The most restrictive of the given decisions (a gate can only escalate)."""
    if not decisions:
        msg = "strictest_decision requires at least one decision"
        raise ValueError(msg)
    return max(decisions, key=decision_rank)


class PolicyEvaluationContext(BaseModel):
    """Inputs required to evaluate policy overrides."""

    signals: DeterministicSignals
    risk: RiskEngineResult
    primary_service_criticality: CriticalityTier | None = None
    dependency_vuln_ceiling: CriticalityTier = "high"


def _service_criticality(domain_pack: DomainPack, service_name: str) -> CriticalityTier | None:
    for service in domain_pack.domain.services:
        if service.name == service_name:
            return service.criticality
    return None


def _severity_at_or_above(actual: str, ceiling: CriticalityTier) -> bool:
    return Severity.from_string(actual).rank() >= Severity.from_string(ceiling).rank()


def evaluate_override(
    override: PolicyOverride,
    *,
    context: PolicyEvaluationContext,
    domain_pack: DomainPack,
) -> bool:
    """Return True when an override condition is satisfied."""
    signals = context.signals
    condition = override.condition

    if condition == "oasdiff_err_on_critical_service":
        criticality = context.primary_service_criticality or _service_criticality(
            domain_pack,
            signals.primary_service,
        )
        return signals.contract_diff.err_count > 0 and criticality == "critical"

    if condition == "verified_secret_in_diff":
        return signals.security.verified_secret_exposure

    if condition == "critical_claim_unsupported_after_repair":
        return signals.repair_attempted and signals.critical_claims_unsupported

    if condition == "evidence_coverage_below_floor":
        if signals.evidence_coverage is None:
            return False
        return signals.evidence_coverage < domain_pack.policy.evidence_floor

    if condition == "dependency_vuln_at_or_above_ceiling":
        return _severity_at_or_above(signals.security.max_severity, context.dependency_vuln_ceiling)

    return False


def apply_policy_gate(
    signals: DeterministicSignals,
    domain_pack: DomainPack,
    risk: RiskEngineResult,
    *,
    dependency_vuln_ceiling: CriticalityTier | None = None,
) -> PolicyGateResult:
    """Apply numeric thresholds plus configured hard overrides."""
    thresholds = domain_pack.policy.decision_thresholds
    score_decision = ReleaseDecision(
        score_to_decision(
            risk.risk_score,
            pass_max=thresholds.pass_max,
            review_max=thresholds.review_max,
        )
    )

    context = PolicyEvaluationContext(
        signals=signals,
        risk=risk,
        primary_service_criticality=_service_criticality(domain_pack, signals.primary_service),
        dependency_vuln_ceiling=(
            dependency_vuln_ceiling or domain_pack.policy.dependency_vuln_ceiling
        ),
    )

    # Every override is evaluated; the strictest outcome wins. Equal-severity ties
    # break by override id, so the result never depends on policy.yaml ordering.
    matched = sorted(
        (
            override
            for override in domain_pack.policy.overrides
            if evaluate_override(override, context=context, domain_pack=domain_pack)
        ),
        key=lambda override: (-decision_rank(ReleaseDecision(override.outcome)), override.id),
    )
    decision = strictest_decision(
        score_decision,
        *(ReleaseDecision(override.outcome) for override in matched),
    )
    deciding = next(
        (
            override
            for override in matched
            if decision_rank(ReleaseDecision(override.outcome)) >= decision_rank(score_decision)
        ),
        None,
    )
    return PolicyGateResult(
        decision=decision,
        risk_score=risk.risk_score,
        override_applied=deciding.id if deciding is not None else None,
        override_condition=deciding.condition if deciding is not None else None,
        overrides_matched=[override.id for override in matched],
        score_decision=score_decision,
        norms=risk.norms,
    )
