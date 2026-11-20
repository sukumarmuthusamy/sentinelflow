"""Deterministic verification metrics computed from validated citations.

The Evidence Verifier (an LLM) judges each material assertion. It does not get to
report its own coverage numbers into the gate: coverage, the unsupported-assertion
rate, and the critical-unsupported flag are computed here, in code, from its
per-assertion judgments after invalid evidence IDs have been stripped.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from app.agents.contracts import (
    ChangeImpact,
    RiskSeverity,
    SecurityFindings,
    VerificationReport,
)
from app.deterministic.models import DeterministicSignals

AssertionSource = Literal["change_analyst", "security_auditor"]

CRITICAL_SEVERITIES: frozenset[RiskSeverity] = frozenset(
    {RiskSeverity.CRITICAL, RiskSeverity.HIGH},
)


class MaterialAssertion(BaseModel):
    """One specialist assertion the Evidence Verifier must assess by ID."""

    assertion_id: str
    text: str
    severity: RiskSeverity
    source: AssertionSource


class VerificationMetrics(BaseModel):
    """Gate inputs derived deterministically from the verifier's judgments."""

    assertions: list[MaterialAssertion] = Field(default_factory=list)
    supported_ids: list[str] = Field(default_factory=list)
    unsupported_ids: list[str] = Field(default_factory=list)
    evidence_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    unsupported_assertion_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    critical_unsupported: bool = False
    llm_reported_evidence_coverage: float | None = None
    llm_reported_unsupported_rate: float | None = None


def material_assertions(
    change_impact: ChangeImpact | None,
    security_findings: SecurityFindings | None,
) -> list[MaterialAssertion]:
    """Collect specialist assertions with namespaced, collision-free IDs."""
    assertions: list[MaterialAssertion] = []
    if change_impact is not None:
        for hypothesis in change_impact.risk_hypotheses:
            assertions.append(
                MaterialAssertion(
                    assertion_id=f"hyp:{hypothesis.id}",
                    text=hypothesis.statement,
                    severity=hypothesis.severity,
                    source="change_analyst",
                ),
            )
    if security_findings is not None:
        for finding in security_findings.findings:
            assertions.append(
                MaterialAssertion(
                    assertion_id=f"sec:{finding.id}",
                    text=f"{finding.title}: {finding.description}",
                    severity=finding.severity,
                    source="security_auditor",
                ),
            )
    return assertions


def signals_indicate_risk(signals: DeterministicSignals | None) -> bool:
    """True when deterministic tools found something an agent should have addressed."""
    if signals is None:
        return False
    return (
        signals.contract_diff.err_count > 0
        or signals.contract_diff.warn_count > 0
        or bool(signals.security.findings)
    )


def compute_verification_metrics(
    assertions: list[MaterialAssertion],
    report: VerificationReport | None,
    *,
    signals: DeterministicSignals | None = None,
) -> VerificationMetrics:
    """Score each assertion from the (citation-filtered) verifier report.

    Rules:
    - An assertion is supported only if the verifier assessed it by its exact ID as
      ``supported`` with at least one surviving evidence ID, and no assessment of the
      same ID is ``contradicted`` or ``injection_suspected``.
    - An assertion the verifier skipped counts as unsupported.
    - With no assertions, coverage is ``None`` (nothing to verify) unless the
      deterministic signals show risk, in which case coverage is ``0.0``: a contract
      break or scanner finding that no agent addressed is weak evidence, not a pass.
    """
    llm_coverage = report.evidence_coverage if report is not None else None
    llm_rate = report.unsupported_assertion_rate if report is not None else None

    if not assertions:
        risky = signals_indicate_risk(signals)
        return VerificationMetrics(
            evidence_coverage=0.0 if risky else None,
            unsupported_assertion_rate=None,
            llm_reported_evidence_coverage=llm_coverage,
            llm_reported_unsupported_rate=llm_rate,
        )

    by_id: dict[str, list[tuple[str, int]]] = {}
    for assessment in report.assertion_assessments if report is not None else []:
        by_id.setdefault(assessment.assertion_id, []).append(
            (assessment.status, len(assessment.evidence_ids)),
        )

    supported: list[str] = []
    unsupported: list[str] = []
    critical_unsupported = False
    for assertion in assertions:
        judgments = by_id.get(assertion.assertion_id, [])
        disqualified = any(
            status in {"contradicted", "injection_suspected"} for status, _ in judgments
        )
        backed = any(status == "supported" and count > 0 for status, count in judgments)
        if backed and not disqualified:
            supported.append(assertion.assertion_id)
        else:
            unsupported.append(assertion.assertion_id)
            if assertion.severity in CRITICAL_SEVERITIES:
                critical_unsupported = True

    total = len(assertions)
    return VerificationMetrics(
        assertions=assertions,
        supported_ids=supported,
        unsupported_ids=unsupported,
        evidence_coverage=len(supported) / total,
        unsupported_assertion_rate=len(unsupported) / total,
        critical_unsupported=critical_unsupported,
        llm_reported_evidence_coverage=llm_coverage,
        llm_reported_unsupported_rate=llm_rate,
    )
