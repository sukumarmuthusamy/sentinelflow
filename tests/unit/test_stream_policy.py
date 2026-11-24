"""SSE framing and subscriber back-pressure (redaction: test_stream_redaction.py)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest
from app.core.errors import ErrorCode, SentinelFlowError
from app.jobs.events import JobEvent, JobEventType, LiveJobEvent
from app.jobs.streaming import JobEventBus, sse_frame

AT = datetime(2026, 11, 23, tzinfo=UTC)


def test_sse_frames_give_ids_to_durable_events_only() -> None:
    durable = JobEvent(job_id="job-1", seq=7, type=JobEventType.NODE_STARTED, at=AT, data={})
    live = LiveJobEvent(
        job_id="job-1",
        type=JobEventType.AGENT_TOKEN,
        at=AT,
        after_seq=7,
        data={"text": 'line1\nline2 "quoted"'},
    )
    durable_frame = sse_frame(durable).decode()
    live_frame = sse_frame(live).decode()
    assert durable_frame.startswith("id: 7\nevent: node.started\ndata: {")
    assert live_frame.startswith("event: agent.token\ndata: {")
    assert "id:" not in live_frame
    # One data line: embedded newlines are JSON-escaped, so a frame cannot be split.
    assert live_frame.count("\n") == 3 and live_frame.endswith("\n\n")


async def test_full_subscriber_drops_instead_of_blocking() -> None:
    bus = JobEventBus(queue_size=4, max_subscribers_per_job=2)
    bus.bind_loop(asyncio.get_running_loop())
    sub = bus.subscribe("job-1")
    for index in range(10):
        bus.publish(
            LiveJobEvent(
                job_id="job-1",
                type=JobEventType.AGENT_TOKEN,
                at=AT,
                after_seq=0,
                data={"index": index},
            ),
        )
    assert sub.lagged
    assert sub.take_dropped_live() == 6
    received = [await sub.get(0.1) for _ in range(4)]
    assert [item.data["index"] for item in received if item is not None] == [0, 1, 2, 3]
    assert await sub.get(0.01) is None  # empty: waits with a timeout, never spins


async def test_store_listener_publishes_on_the_loop_from_any_thread() -> None:
    bus = JobEventBus(queue_size=8, max_subscribers_per_job=2)
    bus.bind_loop(asyncio.get_running_loop())
    sub = bus.subscribe("job-1")
    event = JobEvent(job_id="job-1", seq=1, type=JobEventType.JOB_QUEUED, at=AT, data={})
    await asyncio.to_thread(bus.publish_committed, [event])
    # Published before the awaiting writer resumed (FIFO call_soon_threadsafe).
    assert not sub.empty()
    assert await sub.get(0.1) == event


def test_subscriber_cap_and_release() -> None:
    bus = JobEventBus(queue_size=4, max_subscribers_per_job=1)
    first = bus.subscribe("job-1")
    with pytest.raises(SentinelFlowError) as info:
        bus.subscribe("job-1")
    assert info.value.code is ErrorCode.TOO_MANY_SUBSCRIBERS
    first.close()
    first.close()  # idempotent
    assert bus.subscriber_count("job-1") == 0
    bus.subscribe("job-1").close()
