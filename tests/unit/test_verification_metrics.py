"""Coverage and unsupported rate are computed in code, not self-reported by the LLM."""

from __future__ import annotations

import pytest
from app.agents.contracts import (
    AssertionAssessment,
    ChangeImpact,
    RiskHypothesis,
    RiskSeverity,
    SecurityFinding,
    SecurityFindings,
    VerificationReport,
)
from app.deterministic.models import (
    ContractDiffResult,
    DeterministicSignals,
)
from app.graph.verification import compute_verification_metrics, material_assertions


def _impact(*severities: RiskSeverity) -> ChangeImpact:
    return ChangeImpact(
        change_id="CHG-T",
        risk_hypotheses=[
            RiskHypothesis(id=f"H{i}", statement=f"hypothesis {i}", severity=severity)
            for i, severity in enumerate(severities, start=1)
        ],
    )


def _assess(assertion_id: str, status: str, evidence: int = 1) -> AssertionAssessment:
    return AssertionAssessment(
        assertion_id=assertion_id,
        assertion_text="x",
        status=status,  # type: ignore[arg-type]
        evidence_ids=[f"doc::s{i}" for i in range(evidence)],
    )


def test_assertions_are_namespaced_per_agent() -> None:
    security = SecurityFindings(
        findings=[
            SecurityFinding(
                id="H1",
                category="other",
                severity=RiskSeverity.LOW,
                title="t",
                description="d",
            ),
        ],
    )
    ids = [a.assertion_id for a in material_assertions(_impact(RiskSeverity.LOW), security)]
    assert ids == ["hyp:H1", "sec:H1"]


def test_self_reported_coverage_is_ignored() -> None:
    assertions = material_assertions(_impact(RiskSeverity.LOW, RiskSeverity.LOW), None)
    report = VerificationReport(
        assertion_assessments=[_assess("hyp:H1", "supported")],
        evidence_coverage=1.0,
        unsupported_assertion_rate=0.0,
    )
    metrics = compute_verification_metrics(assertions, report)
    assert metrics.evidence_coverage == pytest.approx(0.5)
    assert metrics.unsupported_assertion_rate == pytest.approx(0.5)
    assert metrics.unsupported_ids == ["hyp:H2"]
    assert metrics.llm_reported_evidence_coverage == 1.0


def test_supported_without_surviving_evidence_is_unsupported() -> None:
    assertions = material_assertions(_impact(RiskSeverity.LOW), None)
    report = VerificationReport(assertion_assessments=[_assess("hyp:H1", "supported", 0)])
    metrics = compute_verification_metrics(assertions, report)
    assert metrics.evidence_coverage == 0.0


def test_contradiction_disqualifies_support() -> None:
    assertions = material_assertions(_impact(RiskSeverity.LOW), None)
    report = VerificationReport(
        assertion_assessments=[
            _assess("hyp:H1", "supported"),
            _assess("hyp:H1", "contradicted"),
        ],
    )
    assert compute_verification_metrics(assertions, report).supported_ids == []


def test_critical_flag_only_for_high_or_critical_unsupported() -> None:
    assertions = material_assertions(_impact(RiskSeverity.HIGH, RiskSeverity.LOW), None)
    only_low_missing = VerificationReport(assertion_assessments=[_assess("hyp:H1", "supported")])
    assert not compute_verification_metrics(assertions, only_low_missing).critical_unsupported
    high_missing = VerificationReport(assertion_assessments=[_assess("hyp:H2", "supported")])
    assert compute_verification_metrics(assertions, high_missing).critical_unsupported


def test_no_assertions_and_clean_signals_means_nothing_to_verify() -> None:
    signals = DeterministicSignals(change_id="CHG-T", primary_service="svc-a")
    metrics = compute_verification_metrics([], VerificationReport(), signals=signals)
    assert metrics.evidence_coverage is None


def test_no_assertions_despite_risky_signals_is_zero_coverage() -> None:
    signals = DeterministicSignals(
        change_id="CHG-T",
        primary_service="svc-a",
        contract_diff=ContractDiffResult(err_count=1, breaking=True),
    )
    metrics = compute_verification_metrics([], VerificationReport(), signals=signals)
    assert metrics.evidence_coverage == 0.0
