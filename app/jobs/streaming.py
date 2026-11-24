"""Job progress streaming: event fan-out, graph event sink, SSE framing (ADR-0003).

Data flow for one job::

    LangGraph astream ──► GraphEventSink ──► store.append_events (node.*, durable)
                               │                    │ listener, after commit
                               │                    ▼
                               └──(agent.token)──► JobEventBus ──► Subscription ──► SSE
    service transitions (job.*) ──► store ──────────┘                 (bounded)

Rules this module enforces:

* **One execution.** Subscribers only read. Nothing here starts, resumes or retries
  a job, and nothing a subscriber does (connect, lag, disconnect) can reach the run.
* **The graph never waits on a client.** The sink publishes with ``put_nowait`` into
  bounded per-subscriber queues. A full queue marks that subscriber *lagged*: it
  then drops new items until it has drained, re-reads the durable events it missed
  from the store, and is told how many live events it lost (``stream.gap``).
* **Durable events are exactly the store's log**, delivered in ``seq`` order with no
  duplicates: anything at or below the last ``seq`` sent is skipped, and a jump is
  filled from the store.
* **Live fragments never precede what they follow.** A fragment whose ``after_seq``
  is older than a durable event already sent is not delivered.
* **Sink failures are contained.** An error while recording progress is logged by
  type and dropped; it cannot fail, retry or alter the analysis or its audit write.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

from app.core.errors import ErrorCode, SentinelFlowError
from app.core.settings import Settings
from app.graph.streaming import GraphEvent, NodeFinished, NodeStarted, OutputFragment
from app.jobs.events import (
    TERMINAL_EVENT_TYPES,
    AgentTokenData,
    JobEvent,
    JobEventType,
    LiveJobEvent,
    NodeEventData,
    StreamGapData,
)
from app.jobs.models import TERMINAL_STATUSES
from app.jobs.redaction import (
    FragmentRedactor,
    StreamRedactor,
    configured_secret_values,
    strip_controls,
)
from app.persistence.job_store import SqliteJobStore, utc_now

logger = logging.getLogger(__name__)

StreamItem = JobEvent | LiveJobEvent


# -- fan-out -----------------------------------------------------------------------


class Subscription:
    """One SSE client's bounded view of one job. Loop-thread only."""

    def __init__(self, bus: JobEventBus, job_id: str, maxsize: int) -> None:
        self.job_id = job_id
        self._bus = bus
        self._queue: asyncio.Queue[StreamItem] = asyncio.Queue(maxsize=maxsize)
        self.lagged = False
        # Bumped on every drop, so a recovery can tell whether more was lost meanwhile.
        self.drop_epoch = 0
        self._dropped_live = 0
        self.closed = False

    def offer(self, item: StreamItem) -> None:
        if self.closed:
            return
        if not self.lagged:
            try:
                self._queue.put_nowait(item)
                return
            except asyncio.QueueFull:
                self.lagged = True
        self.drop_epoch += 1
        if isinstance(item, LiveJobEvent):
            self._dropped_live += 1

    def take_dropped_live(self) -> int:
        dropped, self._dropped_live = self._dropped_live, 0
        return dropped

    def empty(self) -> bool:
        return self._queue.empty()

    async def get(self, timeout: float) -> StreamItem | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return None

    def close(self) -> None:
        if not self.closed:
            self.closed = True
            self._bus.remove(self)


class JobEventBus:
    """In-process fan-out of job events to live subscribers (one API process)."""

    def __init__(self, *, queue_size: int, max_subscribers_per_job: int) -> None:
        self._queue_size = queue_size
        self._max = max_subscribers_per_job
        self._subscribers: dict[str, set[Subscription]] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def subscriber_count(self, job_id: str) -> int:
        return len(self._subscribers.get(job_id, ()))

    def subscribe(self, job_id: str) -> Subscription:
        current = self._subscribers.setdefault(job_id, set())
        if len(current) >= self._max:
            raise SentinelFlowError(
                "Too many open event streams for this job",
                code=ErrorCode.TOO_MANY_SUBSCRIBERS,
                details={"job_id": job_id, "max_subscribers": self._max},
            )
        subscription = Subscription(self, job_id, self._queue_size)
        current.add(subscription)
        return subscription

    def remove(self, subscription: Subscription) -> None:
        current = self._subscribers.get(subscription.job_id)
        if current is None:
            return
        current.discard(subscription)
        if not current:
            del self._subscribers[subscription.job_id]

    def publish(self, item: StreamItem) -> None:
        """Loop thread only. Never blocks and never raises into the caller."""
        for subscription in list(self._subscribers.get(item.job_id, ())):
            subscription.offer(item)

    def publish_committed(self, events: Sequence[JobEvent]) -> None:
        """Store listener: callable from any thread, after the write committed.

        Always hops through ``call_soon_threadsafe``: callbacks run in FIFO order,
        and this one is queued before the store call's own result is delivered, so a
        writer that awaited the store sees its events published before it resumes.
        """
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._publish_many, list(events))
        except RuntimeError:
            return  # loop shutting down: there is nobody left to stream to

    def _publish_many(self, events: list[JobEvent]) -> None:
        for event in events:
            self.publish(event)


@dataclass
class _ResponseBuffer:
    task_id: str
    node: str
    agent: str | None
    schema: str
    channel: str
    response: int
    redactor: StreamRedactor


@dataclass
class _TaskCounters:
    streamed: int = 0
    dropped: int = 0
    responses: dict[str, int] = field(default_factory=dict)


# -- graph sink ----------------------------------------------------------------------


class GraphEventSink:
    """Turns one run's graph events into durable node events and live fragments."""

    def __init__(
        self,
        *,
        store: SqliteJobStore,
        bus: JobEventBus,
        job_id: str,
        run: int,
        last_seq: int,
        settings: Settings,
    ) -> None:
        self._store = store
        self._bus = bus
        self._job_id = job_id
        self._run = run
        self._last_seq = last_seq
        self._max_chars = settings.api_stream_fragment_max_chars
        self._max_fragments = settings.api_stream_max_fragments_per_run
        self._redaction = FragmentRedactor(configured_secret_values(settings))
        self._index = 0
        self._tasks: dict[str, _TaskCounters] = {}
        self._buffers: dict[tuple[str, str, str], _ResponseBuffer] = {}
        self._open = True

    @property
    def fragments_emitted(self) -> int:
        return self._index

    async def __call__(self, event: GraphEvent) -> None:
        try:
            if isinstance(event, NodeStarted):
                await self._durable(
                    JobEventType.NODE_STARTED,
                    NodeEventData(
                        node=event.node,
                        task_id=event.task_id,
                        agent=event.agent,
                        run=self._run,
                    ),
                )
            elif isinstance(event, NodeFinished):
                self._flush(event.task_id)
                counters = self._tasks.get(event.task_id, _TaskCounters())
                await self._durable(
                    JobEventType.NODE_COMPLETED,
                    NodeEventData(
                        node=event.node,
                        task_id=event.task_id,
                        agent=event.agent,
                        run=self._run,
                        status=event.status,
                        updated_keys=list(event.updated_keys),
                        fragments_streamed=counters.streamed,
                        fragments_dropped=counters.dropped,
                    ),
                )
            elif isinstance(event, OutputFragment):
                self._fragment(event)
        except Exception as exc:  # noqa: BLE001 — observability must not fail the run
            logger.warning("job %s: progress event dropped (%s)", self._job_id, type(exc).__name__)

    def close(self) -> None:
        """End of run: flush held fragments of tasks that never reported a result."""
        try:
            for task_id in {key[0] for key in self._buffers}:
                self._flush(task_id)
        except Exception as exc:  # noqa: BLE001
            logger.warning("job %s: progress flush failed (%s)", self._job_id, type(exc).__name__)
        self._open = False

    async def _durable(self, event_type: JobEventType, data: NodeEventData) -> None:
        if not self._open:
            return
        written = await asyncio.to_thread(
            self._store.append_events,
            self._job_id,
            [(event_type, data.model_dump(mode="json"))],
        )
        if written is None:
            # The job left ``running`` (failed or interrupted): stop recording.
            self._open = False
            return
        self._last_seq = written[-1].seq

    def _fragment(self, fragment: OutputFragment) -> None:
        if not self._open:
            return
        counters = self._tasks.setdefault(fragment.task_id, _TaskCounters())
        response = counters.responses.setdefault(
            fragment.message_id,
            len(counters.responses) + 1,
        )
        key = (fragment.task_id, fragment.message_id, fragment.channel)
        buffer = self._buffers.get(key)
        if buffer is None:
            buffer = _ResponseBuffer(
                task_id=fragment.task_id,
                node=fragment.node,
                agent=fragment.agent,
                schema=fragment.schema,
                channel=fragment.channel,
                response=response,
                redactor=self._redaction.stream(),
            )
            self._buffers[key] = buffer
        safe = buffer.redactor.feed(strip_controls(fragment.text))
        self._emit(buffer, safe, counters)

    def _flush(self, task_id: str) -> None:
        for key in [key for key in self._buffers if key[0] == task_id]:
            buffer = self._buffers.pop(key)
            safe = buffer.redactor.finish()
            self._emit(buffer, safe, self._tasks.setdefault(task_id, _TaskCounters()))

    def _emit(self, buffer: _ResponseBuffer, text: str, counters: _TaskCounters) -> None:
        for start in range(0, len(text), self._max_chars):
            piece = text[start : start + self._max_chars]
            if self._index >= self._max_fragments:
                counters.dropped += 1
                continue
            self._index += 1
            counters.streamed += 1
            data = AgentTokenData(
                node=buffer.node,
                task_id=buffer.task_id,
                agent=buffer.agent,
                run=self._run,
                index=self._index,
                response=buffer.response,
                schema_name=buffer.schema,
                channel="content" if buffer.channel == "content" else "tool_call_arguments",
                text=piece,
            )
            self._bus.publish(
                LiveJobEvent(
                    job_id=self._job_id,
                    type=JobEventType.AGENT_TOKEN,
                    at=utc_now(),
                    after_seq=self._last_seq,
                    data=data.model_dump(mode="json"),
                ),
            )


# -- SSE -----------------------------------------------------------------------------


def sse_frame(item: StreamItem) -> bytes:
    """One SSE event. Only durable events carry ``id:``, so live events never move
    ``Last-Event-ID``. The JSON payload is single-line (newlines are escaped)."""
    lines: list[str] = []
    if isinstance(item, JobEvent):
        lines.append(f"id: {item.seq}")
    lines.append(f"event: {item.type.value}")
    lines.append(f"data: {item.model_dump_json()}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


KEEPALIVE_FRAME = b": keepalive\n\n"


class JobEventStream:
    """Replay-then-follow iterator for one subscription (owns and closes it)."""

    def __init__(
        self,
        *,
        store: SqliteJobStore,
        subscription: Subscription,
        after_seq: int,
        keepalive_seconds: float,
        retry_ms: int,
    ) -> None:
        self._store = store
        self._sub = subscription
        self._job_id = subscription.job_id
        self._last = after_seq
        self._keepalive = keepalive_seconds
        self._retry_ms = retry_ms
        self._done = False

    def close(self) -> None:
        self._sub.close()

    async def frames(self) -> AsyncIterator[bytes]:
        try:
            yield f"retry: {self._retry_ms}\n\n".encode("ascii")
            # Subscribed before this read, so nothing committed later can be missed.
            job = await asyncio.to_thread(self._store.get, self._job_id)
            async for frame in self._backfill():
                yield frame
            if self._done or (job is not None and job.status in TERMINAL_STATUSES):
                return
            while not self._done:
                if self._sub.lagged and self._sub.empty():
                    async for frame in self._recover():
                        yield frame
                    continue
                item = await self._sub.get(self._keepalive)
                if item is None:
                    yield KEEPALIVE_FRAME
                    # Status first, then the log: a status change commits together
                    # with its events, so a job seen terminal here has its terminal
                    # event in the backfill below. The other order could end the
                    # stream on a job that finished between the two reads, without
                    # ever sending its terminal event.
                    job = await asyncio.to_thread(self._store.get, self._job_id)
                    # Cheap safety net for anything a dropped publish may have missed.
                    async for frame in self._backfill():
                        yield frame
                    # Never outlive the job: a terminal job has no more events to wait
                    # for, even if its terminal event was skipped by the cursor.
                    if job is None or job.status in TERMINAL_STATUSES:
                        self._done = True
                    continue
                async for frame in self._deliver(item):
                    yield frame
        finally:
            self.close()

    async def _deliver(self, item: StreamItem) -> AsyncIterator[bytes]:
        if isinstance(item, JobEvent):
            if item.seq <= self._last:
                if item.type in TERMINAL_EVENT_TYPES:
                    self._done = True  # the job is over even if this id was skipped
                return  # already sent (replay overlap)
            if item.seq > self._last + 1:
                async for frame in self._backfill():  # fills the gap, includes item
                    yield frame
                return
            yield self._durable(item)
            return
        if item.after_seq > self._last:
            # It follows a durable event this client has not seen yet: send those first.
            async for frame in self._backfill():
                yield frame
        if item.after_seq == self._last and not self._done:
            yield sse_frame(item)

    def _durable(self, event: JobEvent) -> bytes:
        self._last = event.seq
        if event.type in TERMINAL_EVENT_TYPES:
            self._done = True
        return sse_frame(event)

    async def _backfill(self) -> AsyncIterator[bytes]:
        if self._done:
            return
        events = await asyncio.to_thread(self._store.events, self._job_id, after_seq=self._last)
        for event in events:
            yield self._durable(event)
            if event.type in TERMINAL_EVENT_TYPES:
                return

    async def _recover(self) -> AsyncIterator[bytes]:
        """Drained after falling behind: report lost live events, re-read durable ones.

        The gap is reported first: the drops happened after the last item delivered
        and before anything re-read now, and the re-read may end with the terminal
        event, after which nothing more is sent.
        """
        while True:
            epoch = self._sub.drop_epoch
            lost = self._sub.take_dropped_live()
            if lost:
                yield sse_frame(
                    LiveJobEvent(
                        job_id=self._job_id,
                        type=JobEventType.STREAM_GAP,
                        at=utc_now(),
                        after_seq=self._last,
                        data=StreamGapData(dropped_live_events=lost).model_dump(mode="json"),
                    ),
                )
            async for frame in self._backfill():
                yield frame
            if self._done:
                return
            if self._sub.drop_epoch == epoch:
                # Nothing dropped while re-reading. The check and the reset are one
                # synchronous step, so no item can be published in between.
                self._sub.lagged = False
                return
