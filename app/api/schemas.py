"""Versioned (v1) HTTP request and response contracts.

Request models are closed (``extra="forbid"``): an unknown field is a 422, not
ignored. Booleans are strict (``"true"`` or ``1`` is rejected). In particular, an
approval body cannot carry a decision, so a client cannot change the release
decision by retrying or editing an approval.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints

from app.graph.state import AnalysisRequest
from app.jobs.evaluation import EvaluationResult, EvaluationSuite
from app.jobs.models import ApprovalRecord, JobError, JobKind, JobRecord, JobStatus
from app.jobs.results import AnalysisResult

# Same character rules the MCP server enforces (mcp_server validation.py).
ChangeId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$"),
]
PackId = Annotated[
    str,
    StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$"),
]
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$"


class CreateAnalysisRequest(BaseModel):
    """Body of ``POST /v1/analyses``; maps 1:1 onto the graph's ``AnalysisRequest``."""

    model_config = ConfigDict(extra="forbid")

    change_id: ChangeId
    domain_pack_id: PackId
    objective: Annotated[str, StringConstraints(min_length=1, max_length=500)] | None = None
    execute_tests: StrictBool = Field(
        default=False,
        description="Ask the Test Strategist for a run_tests call (always a dry run in M5)",
    )
    record_decision: StrictBool = Field(
        default=False,
        description="Pause for human approval, then record the gate decision in the audit log",
    )
    source: Literal["fixture", "github"] = "fixture"

    def to_graph_request(self) -> AnalysisRequest:
        values = self.model_dump(exclude_none=True)
        return AnalysisRequest(**values)


class ApprovalRequest(BaseModel):
    """Body of ``POST /v1/analyses/{job_id}/approval``."""

    model_config = ConfigDict(extra="forbid")

    approved: StrictBool
    reviewer: Annotated[
        str,
        StringConstraints(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9 ._@-]*$"),
    ]
    comment: Annotated[str, StringConstraints(max_length=1000)] | None = None


class CreateEvaluationRequest(BaseModel):
    """Body of ``POST /v1/evaluations``."""

    model_config = ConfigDict(extra="forbid")

    suite: EvaluationSuite = EvaluationSuite.DETERMINISTIC_FIXTURES
    domain_pack_ids: Annotated[list[PackId], Field(min_length=1, max_length=10)] | None = Field(
        default=None,
        description="Packs to evaluate; omit for every pack",
    )


class JobLinks(BaseModel):
    model_config = ConfigDict(extra="forbid")

    self: str
    result: str | None = None
    approval: str | None = None


class JobView(BaseModel):
    """Status of one job. ``result`` is fetched separately."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    kind: JobKind
    status: JobStatus
    request: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    run_count: int
    result_available: bool
    approval: ApprovalRecord | None
    error: JobError | None
    links: JobLinks

    @classmethod
    def from_record(cls, job: JobRecord) -> JobView:
        if job.kind is JobKind.ANALYSIS:
            base = f"/v1/analyses/{job.job_id}"
            links = JobLinks(
                self=base,
                result=f"{base}/decision",
                approval=f"{base}/approval" if job.request.get("record_decision") else None,
            )
        else:
            base = f"/v1/evaluations/{job.job_id}"
            links = JobLinks(self=base, result=f"{base}/result")
        return cls(
            job_id=job.job_id,
            kind=job.kind,
            status=job.status,
            request=job.request,
            created_at=job.created_at,
            updated_at=job.updated_at,
            started_at=job.started_at,
            finished_at=job.finished_at,
            run_count=job.run_count,
            result_available=job.result is not None,
            approval=job.approval,
            error=job.error,
            links=links,
        )


class DecisionView(BaseModel):
    """Decision and cited report for an analysis.

    Available once the graph has produced a report: while it waits for approval, and
    after it completes. ``status`` is the job's status when the view was read.
    Approval only gates the audit write; the decision shown before approval is the
    decision recorded after it.
    """

    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus
    result: AnalysisResult


class EvaluationResultView(BaseModel):
    model_config = ConfigDict(extra="forbid")

    job_id: str
    status: JobStatus
    result: EvaluationResult
