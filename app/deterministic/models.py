"""Shared typed models for the deterministic signal layer."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from app.graph.state import ReleaseDecision

EdgeProvenance = Literal["resolved", "heuristic", "seed"]
SeverityLevel = Literal["critical", "high", "medium", "low", "info", "none"]


class Severity(StrEnum):
    """Ordered severity levels for scanner and policy comparisons."""

    NONE = "none"
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"

    @classmethod
    def from_string(cls, value: str) -> Severity:
        normalized = value.lower()
        return cls(normalized) if normalized in cls._value2member_map_ else cls.NONE

    def rank(self) -> int:
        order = {
            Severity.NONE: 0,
            Severity.INFO: 1,
            Severity.LOW: 2,
            Severity.MEDIUM: 3,
            Severity.HIGH: 4,
            Severity.CRITICAL: 5,
        }
        return order[self]


class GraphEdge(BaseModel):
    """A directed dependency edge with explicit provenance."""

    source: str
    target: str
    kind: str = "depends_on"
    provenance: EdgeProvenance


class BlastRadiusNode(BaseModel):
    """One node reached by reverse blast-radius traversal."""

    node_id: str
    depth: int = Field(ge=0)
    provenance: EdgeProvenance


class BlastRadiusResult(BaseModel):
    """Reverse reachability from one or more changed nodes."""

    origin_nodes: list[str] = Field(default_factory=list)
    affected_nodes: list[BlastRadiusNode] = Field(default_factory=list)
    max_depth: int = Field(default=0, ge=0)
    resolved_edge_count: int = Field(default=0, ge=0)
    heuristic_edge_count: int = Field(default=0, ge=0)


class ContractChange(BaseModel):
    """One breaking or potentially breaking contract delta."""

    level: Literal["ERR", "WARN", "INFO"]
    message: str
    path: str | None = None


class ContractDiffResult(BaseModel):
    """Severity-graded OpenAPI/Protobuf compatibility verdict."""

    tool: Literal["oasdiff", "buf", "fixture", "unavailable"] = "unavailable"
    err_count: int = Field(default=0, ge=0)
    warn_count: int = Field(default=0, ge=0)
    info_count: int = Field(default=0, ge=0)
    changes: list[ContractChange] = Field(default_factory=list)
    breaking: bool = False


class SecurityFindingRecord(BaseModel):
    """Normalized security finding from any scanner."""

    id: str
    scanner: Literal["gitleaks", "semgrep", "osv-scanner", "fixture"]
    severity: SeverityLevel
    title: str
    detail: str = ""
    file_path: str | None = None
    rule_id: str | None = None


class SecurityScanResult(BaseModel):
    """Aggregated security scanner output."""

    findings: list[SecurityFindingRecord] = Field(default_factory=list)
    max_severity: SeverityLevel = "none"
    verified_secret_exposure: bool = False


class DeterministicSignals(BaseModel):
    """All non-LLM inputs consumed by the risk engine and policy gate."""

    change_id: str
    primary_service: str
    impacted_services: list[str] = Field(default_factory=list)
    blast_radius: BlastRadiusResult = Field(default_factory=BlastRadiusResult)
    contract_diff: ContractDiffResult = Field(default_factory=ContractDiffResult)
    security: SecurityScanResult = Field(default_factory=SecurityScanResult)
    required_tests: list[str] = Field(default_factory=list)
    available_tests: list[str] = Field(default_factory=list)
    evidence_coverage: float | None = None
    critical_claims_unsupported: bool = False
    repair_attempted: bool = False


class NormBreakdown(BaseModel):
    """Per-term normalized contributions for auditability."""

    blast_radius_norm: float = Field(ge=0.0, le=1.0)
    critical_service_norm: float = Field(ge=0.0, le=1.0)
    contract_break_norm: float = Field(ge=0.0, le=1.0)
    security_severity_norm: float = Field(ge=0.0, le=1.0)
    test_gap_norm: float = Field(ge=0.0, le=1.0)
    incident_similarity_norm: float = Field(ge=0.0, le=1.0)
    evidence_confidence_norm: float = Field(ge=0.0, le=1.0)


class RiskEngineResult(BaseModel):
    """Deterministic risk score before policy overrides."""

    risk_score: int = Field(ge=0, le=100)
    norms: NormBreakdown
    weighted_components: dict[str, float] = Field(default_factory=dict)


class PolicyGateResult(BaseModel):
    """Final deterministic gate outcome."""

    decision: ReleaseDecision
    risk_score: int = Field(ge=0, le=100)
    override_applied: str | None = None
    override_condition: str | None = None
    score_decision: ReleaseDecision
    norms: NormBreakdown
