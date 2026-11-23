"""Job event contracts: the boundary M5-B's SSE endpoint will stream.

Two families share one envelope:

* **Lifecycle events** (``job.*``) are emitted in M5-A. The job store writes them
  durably in the same transaction as the status change they describe, so the event
  log can never disagree with the job's status. ``seq`` is per job, gap-free and
  starts at 1. It is the natural SSE ``id`` for ``Last-Event-ID`` replay.
* **Graph events** (``node.*``, ``agent.token``) are *defined but not emitted* in
  M5-A. They need the runner to switch from ``ainvoke`` to ``astream``, which is
  M5-B. ``EMITTED_EVENT_TYPES`` lists what is actually produced today, and a test
  pins it, so a client cannot mistake a contract for a working feature.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class JobEventType(StrEnum):
    """Event names on the job stream."""

    JOB_QUEUED = "job.queued"
    JOB_RUNNING = "job.running"
    JOB_WAITING_FOR_APPROVAL = "job.waiting_for_approval"
    JOB_APPROVAL_SUBMITTED = "job.approval_submitted"
    JOB_COMPLETED = "job.completed"
    JOB_FAILED = "job.failed"

    # M5-B contracts, not emitted yet.
    NODE_STARTED = "node.started"
    NODE_COMPLETED = "node.completed"
    AGENT_TOKEN = "agent.token"


LIFECYCLE_EVENT_TYPES: frozenset[JobEventType] = frozenset(
    {
        JobEventType.JOB_QUEUED,
        JobEventType.JOB_RUNNING,
        JobEventType.JOB_WAITING_FOR_APPROVAL,
        JobEventType.JOB_APPROVAL_SUBMITTED,
        JobEventType.JOB_COMPLETED,
        JobEventType.JOB_FAILED,
    },
)

# What M5-A actually emits. M5-B extends this only once node and token events are
# demonstrated end to end.
EMITTED_EVENT_TYPES: frozenset[JobEventType] = LIFECYCLE_EVENT_TYPES


class JobEvent(BaseModel):
    """One event on a job's stream."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    seq: int = Field(ge=1, description="Per-job, gap-free, starts at 1 (SSE id)")
    type: JobEventType
    at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class NodeEventData(BaseModel):
    """Payload of ``node.started`` / ``node.completed`` (M5-B).

    ``node`` is the LangGraph node name. ``agent`` is set for the fan-out node
    ``run_parallel_agent``, whose branches share one node name. ``task_id`` is
    LangGraph's task id, which tells parallel branches apart.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: str
    agent: str | None = None
    task_id: str | None = None
    # Only on node.completed: the state keys the node wrote. Values are never sent;
    # retrieved chunk text and raw tool payloads stay server-side.
    updated_keys: list[str] = Field(default_factory=list)


class AgentTokenData(BaseModel):
    """Payload of ``agent.token`` (M5-B).

    The gateway asks for structured output, so a streamed "token" is a fragment of
    the JSON the model is producing (tool-call arguments or JSON content), not prose.
    Token events are live-only: they are not persisted and cannot be replayed.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: str
    agent: str | None = None
    task_id: str | None = None
    text: str
