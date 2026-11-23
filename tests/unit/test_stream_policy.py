"""Fragment redaction, control stripping, SSE framing and subscriber back-pressure."""

from __future__ import annotations

import asyncio
import random
from datetime import UTC, datetime

import pytest
from app.core.errors import ErrorCode, SentinelFlowError
from app.jobs.events import JobEvent, JobEventType, LiveJobEvent
from app.jobs.streaming import (
    REDACTED,
    FragmentRedactor,
    JobEventBus,
    sse_frame,
    strip_controls,
)

AT = datetime(2026, 11, 23, tzinfo=UTC)
SECRETS = [
    "AIza" + "Sy" + "C" * 33,
    "sk-or-v1-" + "a" * 48,
    "ghp_" + "Z" * 36,
    "github_pat_" + "9" * 60,
    "Bearer " + "t" * 40,
    "eyJ" + "a" * 20 + "." + "b" * 20 + "." + "c" * 20,
]


def _stream(redactor: FragmentRedactor, text: str, sizes: list[int]) -> str:
    """Feed ``text`` in chunks of the given sizes; return everything emitted."""
    out: list[str] = []
    pending = ""
    position = 0
    for size in sizes:
        pending += text[position : position + size]
        position += size
        safe, pending = redactor.split(pending, final=False)
        out.append(safe)
    pending += text[position:]
    safe, _ = redactor.split(pending, final=True)
    out.append(safe)
    return "".join(out)


@pytest.mark.parametrize("secret", SECRETS)
@pytest.mark.parametrize("seed", range(5))
def test_credentials_split_across_chunks_never_leave(secret: str, seed: int) -> None:
    rng = random.Random(seed)
    text = '{"summary":"' + "x" * rng.randint(0, 300) + secret + " tail" + "y" * 200 + '"}'
    sizes = [rng.randint(1, 12) for _ in range(len(text))]
    emitted = _stream(FragmentRedactor(), text, sizes)
    assert secret not in emitted
    assert secret[-6:] not in emitted  # no partial tail leaks either
    assert REDACTED in emitted
    assert emitted.endswith('"}')


def test_configured_secret_values_are_redacted_whole() -> None:
    value = "plain-config-value-42"
    emitted = _stream(FragmentRedactor([value]), f"a {value} b" + "z" * 200, [3] * 100)
    assert value not in emitted
    assert "a [REDACTED] b" in emitted


def test_private_key_block_is_held_until_it_ends() -> None:
    redactor = FragmentRedactor()
    text = "-----BEGIN PRIVATE KEY-----\n" + "Q" * 400
    safe, held = redactor.split(text, final=False)
    assert safe == ""  # the whole open block is held back
    assert held == text
    closed, _ = redactor.split(text + "\n-----END PRIVATE KEY-----done", final=True)
    assert "Q" not in closed and closed == f"{REDACTED}done"


def test_text_without_secrets_passes_through_unchanged() -> None:
    text = '{"rationale":"documentation-only","agents":["change_analyst"]}' * 5
    assert _stream(FragmentRedactor(), text, [7] * 60) == text


def test_control_and_format_characters_are_stripped() -> None:
    assert strip_controls("a‮b​c\x00d\x1b[31m\te\nf") == "abcd[31m\te\nf"


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
