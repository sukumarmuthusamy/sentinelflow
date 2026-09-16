"""Graph state and planning models for the SentinelFlow workflow."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from app.agents.contracts import (
    ChangeImpact,
    EvidenceBundle,
    RegressionPlan,
    SecurityFindings,
    VerificationReport,
)
from app.core.domain_pack import AgentName, ChangeCategory


class ReleaseDecision(StrEnum):
    """Final release gate recommendation."""

    PASS = "PASS"
    REVIEW = "REVIEW"
    BLOCK = "BLOCK"


class ApprovalStatus(StrEnum):
    """Human approval state for external write actions."""

    NOT_REQUESTED = "not_requested"
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class AnalysisRequest(BaseModel):
    """Input submitted to start an analysis."""

    change_id: str
    domain_pack_id: str
    objective: str = "Assess release risk and generate the minimum sufficient regression plan"
    execute_tests: bool = False
    record_decision: bool = False
    source: Literal["fixture", "github"] = "fixture"


class AnalysisPlan(BaseModel):
    """Output of the Triage Planner node — which agents to invoke."""

    change_category: ChangeCategory
    agents: list[AgentName] = Field(min_length=1)
    rationale: str = Field(
        max_length=500,
        description="Short classification rationale; not a free-form reasoning trace",
    )
    clamped_to_default: bool = False


class RiskAssessment(BaseModel):
    """Deterministic risk score and policy outcome (computed, not LLM-generated)."""

    risk_score: int = Field(ge=0, le=100)
    decision: ReleaseDecision
    confidence: float = Field(ge=0.0, le=1.0)
    override_applied: str | None = None
    component_scores: dict[str, float] = Field(default_factory=dict)


class TraceMetrics(BaseModel):
    """Observability metrics accumulated during an analysis run."""

    agent_steps: int = 0
    llm_calls: int = 0
    mcp_tool_calls: int = 0
    retrieval_queries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    repair_count: int = 0


class CriticalFinding(BaseModel):
    """A cited finding surfaced in the final report."""

    finding: str
    evidence_ids: list[str] = Field(default_factory=list)


class ReleaseReport(BaseModel):
    """Final cited release-readiness report."""

    analysis_id: str
    decision: ReleaseDecision
    risk_score: int = Field(ge=0, le=100)
    confidence: float = Field(ge=0.0, le=1.0)
    impacted_services: list[str] = Field(default_factory=list)
    critical_findings: list[CriticalFinding] = Field(default_factory=list)
    recommended_tests: list[str] = Field(default_factory=list)
    evidence_coverage: float = Field(ge=0.0, le=1.0)
    requires_human_approval: bool = False


def _merge_unique_strings(existing: list[str], incoming: list[str]) -> list[str]:
    merged = list(existing)
    for item in incoming:
        if item not in merged:
            merged.append(item)
    return merged


class AnalysisState(BaseModel):
    """Shared LangGraph state for a single analysis invocation.

    List fields that accumulate parallel branch outputs use reducers when
    converted to a TypedDict graph schema in M4. For M0 we model them on a
    Pydantic object with explicit merge helpers.
    """

    analysis_id: str
    request: AnalysisRequest
    domain_pack_id: str
    plan: AnalysisPlan | None = None
    change_impact: ChangeImpact | None = None
    evidence: EvidenceBundle | None = None
    regression_plan: RegressionPlan | None = None
    security_findings: SecurityFindings | None = None
    verification: VerificationReport | None = None
    risk_assessment: RiskAssessment | None = None
    evidence_gaps: list[str] = Field(default_factory=list)
    retry_count: int = 0
    approval_status: ApprovalStatus = ApprovalStatus.NOT_REQUESTED
    trace_metrics: TraceMetrics = Field(default_factory=TraceMetrics)
    final_report: ReleaseReport | None = None
    errors: list[str] = Field(default_factory=list)

    def merge_evidence_gaps(self, gaps: list[str]) -> None:
        self.evidence_gaps = _merge_unique_strings(self.evidence_gaps, gaps)

    def append_error(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)
