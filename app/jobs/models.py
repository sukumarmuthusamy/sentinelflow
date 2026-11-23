"""Job records and the deterministic lifecycle transition table (ADR-0003)."""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import ErrorCode


class JobKind(StrEnum):
    """What a job runs."""

    ANALYSIS = "analysis"
    EVALUATION = "evaluation"


class JobStatus(StrEnum):
    """Lifecycle state of a job.

    ``waiting_for_approval`` maps to the graph's ``interrupt()`` before
    ``record_gate_decision``. A conservative REVIEW is a *completed* analysis: the
    decision is the result, not a failure.
    """

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_FOR_APPROVAL = "waiting_for_approval"
    COMPLETED = "completed"
    FAILED = "failed"


TERMINAL_STATUSES: frozenset[JobStatus] = frozenset({JobStatus.COMPLETED, JobStatus.FAILED})

# Statuses that hold (or are about to hold) an execution slot; capped by
# ``api_max_active_jobs``. A job waiting for a human holds nothing.
ACTIVE_STATUSES: frozenset[JobStatus] = frozenset({JobStatus.QUEUED, JobStatus.RUNNING})

# The only legal moves. Every write goes through a compare-and-set on the current
# status, so a transition that is not listed here, or whose precondition no longer
# holds because another request won the race, changes nothing.
ALLOWED_TRANSITIONS: dict[JobStatus, frozenset[JobStatus]] = {
    # queued -> failed: interrupted before start (shutdown) or start-time failure.
    JobStatus.QUEUED: frozenset({JobStatus.RUNNING, JobStatus.FAILED}),
    JobStatus.RUNNING: frozenset(
        {JobStatus.WAITING_FOR_APPROVAL, JobStatus.COMPLETED, JobStatus.FAILED},
    ),
    # An approval re-queues the job; the resume then runs like any queued job.
    JobStatus.WAITING_FOR_APPROVAL: frozenset({JobStatus.QUEUED}),
    JobStatus.COMPLETED: frozenset(),
    JobStatus.FAILED: frozenset(),
}


def is_allowed_transition(current: JobStatus, target: JobStatus) -> bool:
    """True when ``current -> target`` is a legal lifecycle move."""
    return target in ALLOWED_TRANSITIONS[current]


class ApprovalRecord(BaseModel):
    """A human decision on the gated write, as accepted by the API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    approved: bool
    reviewer: str
    comment: str | None = None
    submitted_at: datetime


class JobError(BaseModel):
    """Sanitized failure: a code and a fixed message, never an exception string."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: ErrorCode
    message: str


class JobRecord(BaseModel):
    """One row of the job store."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    kind: JobKind
    status: JobStatus
    request: dict[str, Any]
    request_fingerprint: str
    thread_id: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    # Graph executions started for this job: 1 after the first run, 2 after a resume.
    run_count: int = Field(default=0, ge=0)
    approval: ApprovalRecord | None = None
    result: dict[str, Any] | None = None
    error: JobError | None = None

    @property
    def is_resume_pending(self) -> bool:
        """A queued analysis that already has a result and an approval: run the resume."""
        return (
            self.kind is JobKind.ANALYSIS
            and self.status is JobStatus.QUEUED
            and self.approval is not None
            and self.result is not None
        )
