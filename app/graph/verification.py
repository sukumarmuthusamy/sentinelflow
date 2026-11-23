"""Deterministic verification metrics computed from validated citations.

The Evidence Verifier (an LLM) judges each material assertion. It does not get to
report its own coverage numbers into the gate: coverage, the unsupported-assertion
rate, and the critical-unsupported flag are computed here, in code, from its
per-assertion judgments after invalid evidence IDs have been stripped.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.contracts import (
    AssertionAssessment,
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
    # Evidence the specialist itself cited for this assertion (security findings only).
    cited_evidence_ids: list[str] = Field(default_factory=list)


class VerificationMetrics(BaseModel):
    """Gate inputs derived deterministically from the verifier's judgments."""

    assertions: list[MaterialAssertion] = Field(default_factory=list)
    supported_ids: list[str] = Field(default_factory=list)
    unsupported_ids: list[str] = Field(default_factory=list)
    evidence_coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    unsupported_assertion_rate: float | None = Field(default=None, ge=0.0, le=1.0)
    critical_unsupported: bool = False
    # "supported" judgments demoted because the source text did not back them.
    grounding_rejections: list[str] = Field(default_factory=list)
    # The verifier reported injection (flags or an injection_suspected assessment).
    injection_flagged: bool = False
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
                    cited_evidence_ids=list(finding.evidence_ids),
                ),
            )
    return assertions


MIN_QUOTE_CHARS = 20
_NEGATIONS = frozenset({"not", "no", "never", "none", "nor", "without", "cannot"})
_STOPWORDS = frozenset(
    {
        "this",
        "that",
        "with",
        "from",
        "into",
        "will",
        "would",
        "could",
        "should",
        "have",
        "been",
        "were",
        "they",
        "them",
        "their",
        "there",
        "than",
        "then",
        "also",
        "only",
        "when",
        "which",
        "while",
        "about",
        "after",
        "before",
        "change",
        "changes",
        "does",
        "must",
        "more",
        "most",
        "such",
        "other",
        "some",
        "what",
    },
)
_TOKEN = re.compile(r"[a-z0-9][a-z0-9_\-]*(?:'t)?")


def _normalize(text: str) -> str:
    return " ".join(text.lower().split())


def _tokens(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


def _negated(tokens: list[str]) -> bool:
    return any(token in _NEGATIONS or token.endswith("n't") for token in tokens)


def _salient(tokens: list[str]) -> set[str]:
    return {token for token in tokens if len(token) >= 4 and token not in _STOPWORDS}


def grounding_failure(
    assertion_text: str,
    quote: str,
    cited_contents: list[str],
) -> str | None:
    """Why a "supported" judgment is not backed by source text, or None when it is.

    Deterministic checks on the verifier's own supporting quote:
    1. it is long enough to mean something;
    2. it appears verbatim (case/whitespace-normalized) in a cited, shown chunk;
    3. it shares at least one salient term with the assertion (topical);
    4. its negation polarity matches the assertion's (a quote saying "is not
       consumed" cannot back "is consumed").
    These do not prove semantic support; they stop an ID, an off-topic excerpt or a
    contradicting excerpt from counting as support on the verifier's say-so.
    """
    normalized_quote = _normalize(quote)
    if len(normalized_quote) < MIN_QUOTE_CHARS:
        return "no supporting quote"
    if not any(normalized_quote in _normalize(content) for content in cited_contents):
        return "quote not found in cited evidence"
    quote_tokens = _tokens(normalized_quote)
    assertion_tokens = _tokens(assertion_text)
    if not _salient(quote_tokens) & _salient(assertion_tokens):
        return "quote shares no terms with the assertion"
    if _negated(quote_tokens) != _negated(assertion_tokens):
        return "quote polarity contradicts the assertion"
    return None


def ground_report(
    report: VerificationReport,
    assertions: list[MaterialAssertion],
    shown_content: dict[str, str],
) -> tuple[VerificationReport, list[str]]:
    """Demote "supported" judgments that the shown source text does not back.

    ``report`` must already be citation-filtered to the shown evidence IDs.
    """
    texts = {assertion.assertion_id: assertion.text for assertion in assertions}
    rejections: list[str] = []
    grounded: list[AssertionAssessment] = []
    for assessment in report.assertion_assessments:
        if assessment.status != "supported":
            grounded.append(assessment)
            continue
        text = texts.get(assessment.assertion_id)
        if text is None:
            reason: str | None = "unknown assertion id"
        else:
            contents = [
                shown_content[eid] for eid in assessment.evidence_ids if eid in shown_content
            ]
            reason = (
                grounding_failure(text, assessment.supporting_quote, contents)
                if contents
                else "no shown evidence cited"
            )
        if reason is None:
            grounded.append(assessment)
            continue
        rejections.append(f"{assessment.assertion_id}: {reason}")
        grounded.append(
            assessment.model_copy(
                update={
                    "status": "unsupported",
                    "notes": f"{assessment.notes} [grounding: {reason}]".strip(),
                },
            ),
        )
    return report.model_copy(update={"assertion_assessments": grounded}), rejections


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
    grounding_rejections: list[str] | None = None,
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
    # The verifier's injection report can only escalate (REVIEW floor), never decide.
    injection_flagged = report is not None and (
        bool(report.injection_flags)
        or any(item.status == "injection_suspected" for item in report.assertion_assessments)
    )
    rejections = list(grounding_rejections or [])

    if not assertions:
        risky = signals_indicate_risk(signals)
        return VerificationMetrics(
            evidence_coverage=0.0 if risky else None,
            unsupported_assertion_rate=None,
            grounding_rejections=rejections,
            injection_flagged=injection_flagged,
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
        grounding_rejections=rejections,
        injection_flagged=injection_flagged,
        llm_reported_evidence_coverage=llm_coverage,
        llm_reported_unsupported_rate=llm_rate,
    )
