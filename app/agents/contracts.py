"""Typed Pydantic contracts for specialist agent outputs."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field


class RiskSeverity(StrEnum):
    """Severity levels for risk hypotheses and findings."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class RiskHypothesis(BaseModel):
    """A ranked risk hypothesis emitted by the Change Analyst."""

    id: str
    statement: str
    severity: RiskSeverity
    impacted_services: list[str] = Field(default_factory=list)
    related_contracts: list[str] = Field(default_factory=list)
    deterministic_signals: list[str] = Field(
        default_factory=list,
        description="References to deterministic tool outputs supporting this hypothesis",
    )


class BlastRadiusSummary(BaseModel):
    """Deterministic blast-radius signal interpreted by the Change Analyst."""

    origin_nodes: list[str] = Field(default_factory=list)
    affected_nodes: list[str] = Field(default_factory=list)
    max_depth: int = Field(ge=0)
    critical_services_touched: list[str] = Field(default_factory=list)


class ChangeImpact(BaseModel):
    """Output of the Change Analyst agent."""

    change_id: str
    impacted_services: list[str] = Field(default_factory=list)
    affected_contracts: list[str] = Field(default_factory=list)
    data_flows: list[str] = Field(default_factory=list)
    blast_radius: BlastRadiusSummary | None = None
    risk_hypotheses: list[RiskHypothesis] = Field(default_factory=list)
    summary: str = ""


class EvidenceCitation(BaseModel):
    """A stable citation to a knowledge-base chunk."""

    document_id: str
    section_id: str
    document_type: str
    service: str | None = None
    excerpt: str = ""
    relevance_score: float = Field(ge=0.0, le=1.0)


class EvidenceItem(BaseModel):
    """Evidence supporting or contradicting a hypothesis."""

    hypothesis_id: str
    stance: Literal["support", "contradict", "neutral"]
    citation: EvidenceCitation
    rationale: str = ""


class EvidenceBundle(BaseModel):
    """Output of the Evidence Investigator agent."""

    items: list[EvidenceItem] = Field(default_factory=list)
    coverage: float = Field(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="Fraction of hypotheses with evidence",
    )
    unsupported_hypothesis_ids: list[str] = Field(default_factory=list)
    retrieval_queries: list[str] = Field(default_factory=list)


class RecommendedTest(BaseModel):
    """A prioritized test recommendation."""

    test_id: str
    name: str
    test_type: Literal["unit", "contract", "api", "integration", "e2e", "regression"]
    priority: Literal["P0", "P1", "P2", "P3"]
    components: list[str] = Field(default_factory=list)
    rationale: str = ""


class RegressionPlan(BaseModel):
    """Output of the Test Strategist agent."""

    change_id: str
    recommended_tests: list[RecommendedTest] = Field(default_factory=list)
    coverage_gaps: list[str] = Field(default_factory=list)
    dry_run_only: bool = True
    execution_requested: bool = False


class SecurityFinding(BaseModel):
    """A single security finding."""

    id: str
    category: Literal[
        "secret_exposure",
        "auth_change",
        "dependency_vulnerability",
        "input_validation",
        "injection_risk",
        "policy_violation",
        "other",
    ]
    severity: RiskSeverity
    title: str
    description: str
    affected_services: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    scanner_source: Literal["gitleaks", "semgrep", "osv-scanner", "manual", "unknown"] = "unknown"


class SecurityFindings(BaseModel):
    """Output of the Security Auditor agent."""

    findings: list[SecurityFinding] = Field(default_factory=list)
    max_severity: RiskSeverity = RiskSeverity.INFO
    requires_security_review: bool = False
    summary: str = ""


class AssertionAssessment(BaseModel):
    """Assessment of a material assertion against available evidence."""

    assertion_id: str
    assertion_text: str
    status: Literal["supported", "unsupported", "contradicted", "injection_suspected"]
    evidence_ids: list[str] = Field(default_factory=list)
    notes: str = ""


class AgentDisagreement(BaseModel):
    """A detected contradiction between specialist outputs."""

    topic: str
    agents: list[str]
    description: str


class VerificationReport(BaseModel):
    """Output of the Evidence Verifier agent."""

    assertion_assessments: list[AssertionAssessment] = Field(default_factory=list)
    disagreements: list[AgentDisagreement] = Field(default_factory=list)
    unsupported_assertion_rate: float = Field(ge=0.0, le=1.0, default=0.0)
    evidence_coverage: float = Field(ge=0.0, le=1.0, default=0.0)
    injection_flags: list[str] = Field(default_factory=list)
    repair_recommended: bool = False
