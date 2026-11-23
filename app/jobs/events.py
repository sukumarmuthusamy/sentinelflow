"""Job event contracts streamed by ``GET /v1/analyses/{job_id}/events`` (ADR-0003).

Two delivery classes share the stream:

* **Durable events** carry a ``seq`` and are written to the job store.
  * Lifecycle events (``job.*``) are written in the same transaction as the status
    change they describe, so the event log never disagrees with the job's status.
  * Node events (``node.started`` / ``node.completed``) are written while the job is
    ``running``, as LangGraph reports each task.
  ``seq`` is per job, gap-free and starts at 1. It is the SSE ``id``, so
  ``Last-Event-ID`` replays exactly the durable events after it.
* **Live events** (``agent.token``, ``stream.gap``) have no ``seq`` and no SSE ``id``,
  so they never move a client's ``Last-Event-ID``. They are not persisted and cannot
  be replayed. Each carries ``after_seq``, the durable event it follows.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class JobEventType(StrEnum):
    """Event names on the job stream."""

    JOB_QUEUED = "job.queued"
    JOB_RUNNING = "job.running"
    JOB_WAITING_FOR_APPROVAL = "job.waiting_for_approval"
    JOB_APPROVAL_SUBMITTED = "job.approval_submitted"
    JOB_COMPLETED = "job.completed"
    JOB_FAILED = "job.failed"

    NODE_STARTED = "node.started"
    NODE_COMPLETED = "node.completed"

    # Live-only.
    AGENT_TOKEN = "agent.token"
    STREAM_GAP = "stream.gap"


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
NODE_EVENT_TYPES: frozenset[JobEventType] = frozenset(
    {JobEventType.NODE_STARTED, JobEventType.NODE_COMPLETED},
)
DURABLE_EVENT_TYPES: frozenset[JobEventType] = LIFECYCLE_EVENT_TYPES | NODE_EVENT_TYPES
LIVE_EVENT_TYPES: frozenset[JobEventType] = frozenset(
    {JobEventType.AGENT_TOKEN, JobEventType.STREAM_GAP},
)
# The stream ends after one of these.
TERMINAL_EVENT_TYPES: frozenset[JobEventType] = frozenset(
    {JobEventType.JOB_COMPLETED, JobEventType.JOB_FAILED},
)

# Everything the service produces. ``agent.token`` is produced only when token
# streaming is enabled and the provider really streams (ADR-0003).
EMITTED_EVENT_TYPES: frozenset[JobEventType] = DURABLE_EVENT_TYPES | LIVE_EVENT_TYPES


class JobEvent(BaseModel):
    """A durable event: persisted, ordered by ``seq``, replayable."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    seq: int = Field(ge=1, description="Per-job, gap-free, starts at 1 (SSE id)")
    type: JobEventType
    at: datetime
    data: dict[str, Any] = Field(default_factory=dict)


class LiveJobEvent(BaseModel):
    """A live-only event: not persisted, no ``seq``, sent without an SSE ``id``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    job_id: str
    type: JobEventType
    at: datetime
    after_seq: int = Field(ge=0, description="seq of the last durable event before this one")
    data: dict[str, Any] = Field(default_factory=dict)


class NodeEventData(BaseModel):
    """Payload of ``node.started`` / ``node.completed``.

    ``node`` is the LangGraph node name and ``task_id`` LangGraph's task id, which
    tells the ``run_parallel_agent`` branches apart. ``agent`` is the agent the node
    runs for. ``run`` counts executions of the job (1 = first run, 2 = resume).
    Values are never sent: only the *names* of state keys a node wrote.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: str
    task_id: str
    agent: str | None = None
    run: int = Field(ge=1)
    # node.completed only.
    status: Literal["ok", "error", "interrupted"] | None = None
    updated_keys: list[str] = Field(default_factory=list)
    fragments_streamed: int | None = Field(default=None, ge=0)
    fragments_dropped: int | None = Field(default=None, ge=0)


class AgentTokenData(BaseModel):
    """Payload of ``agent.token``: a provisional piece of structured model output.

    The gateway asks every model for schema-shaped JSON, so fragments are pieces of
    JSON, not prose (``format``). They are *unverified*: the final output may fail
    schema validation, be repaired, or be discarded by the evidence verifier, and the
    authoritative result is the decision endpoint. Concatenating the fragments of one
    ``(task_id, response)`` in ``index`` order gives that response's streamed text,
    after control-character stripping and credential redaction.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    node: str
    task_id: str
    agent: str | None = None
    run: int = Field(ge=1)
    index: int = Field(ge=1, description="Per-run order of fragments, gap-free as emitted")
    response: int = Field(ge=1, description="Which model response of this task (1, 2, ...)")
    schema_name: str
    channel: Literal["content", "tool_call_arguments"]
    format: Literal["structured_json"] = "structured_json"
    provisional: Literal[True] = True
    verified: Literal[False] = False
    text: str


class StreamGapData(BaseModel):
    """Payload of ``stream.gap``: this subscriber fell behind and lost live events.

    Durable events missed meanwhile are re-read from the store, so only live
    fragments are lost.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    reason: Literal["slow_client"] = "slow_client"
    dropped_live_events: int = Field(ge=0)
