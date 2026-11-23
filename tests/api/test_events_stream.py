"""``GET /v1/analyses/{id}/events`` over ASGI: framing, ordering, replay, tokens, safety.

Runs the real app, store and graph. Token tests use ``FakeStreamingGatewayLLM``: the
production gateway call path over a chat model that streams its JSON in small
chunks, so every ``agent.token`` here was produced by a real ``astream`` chunk.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import pytest
from app.agents.contracts import ChangeImpact
from app.graph.runner import CheckpointSnapshot
from app.graph.state import AnalysisPlan
from app.jobs.events import JobEventType
from app.jobs.streaming import JobEventStream
from app.persistence.job_store import SqliteJobStore
from pydantic import BaseModel, SecretStr

from tests.api.conftest import BlockingLLM, FakeRuntime, Harness, HarnessFactory, api_settings
from tests.api.sse_client import AsgiSseClient, SseEvent, durable, named
from tests.graph.conftest import static_llm_responses
from tests.graph.fakes import DEFAULT_CHUNKS, FailingToolMcp
from tests.graph.streaming_fakes import FakeStreamingGatewayLLM, payloads_from

PLAIN = {"change_id": "CHG-H001", "domain_pack_id": "healthcare-claims"}
GATED = {**PLAIN, "record_decision": True}
APPROVE = {"approved": True, "reviewer": "release-manager"}
AGENTS = {"change_analyst", "evidence_investigator", "test_strategist", "security_auditor"}


def _streaming(
    tmp_path: Path,
    *,
    method: str | None = None,
    responses: dict[type[BaseModel], BaseModel] | None = None,
    stream_tokens: bool = True,
    settings_overrides: dict[str, Any] | None = None,
    **model_kwargs: Any,
) -> tuple[Any, FakeRuntime, FakeStreamingGatewayLLM]:
    settings = api_settings(tmp_path, **(settings_overrides or {}))
    llm = FakeStreamingGatewayLLM(
        responses or static_llm_responses(),
        method=method,
        settings=settings,
        **model_kwargs,
    )
    return settings, FakeRuntime(llm, stream_tokens=stream_tokens), llm


def _events_path(job_id: str) -> str:
    return f"/v1/analyses/{job_id}/events"


def _sse(h: Harness, job_id: str, **headers: str) -> AsgiSseClient:
    return AsgiSseClient(h.app, _events_path(job_id), headers=headers)


async def _create(h: Harness, body: dict[str, Any] | None = None) -> str:
    created = await h.create(body or PLAIN)
    assert created.status_code == 202, created.text
    job_id: str = created.json()["job_id"]
    return job_id


def _assert_ordered(frames: list[SseEvent], *, first_seq: int = 1) -> None:
    """Durable ids gap-free; tokens sit inside their node and after the right seq."""
    expected = first_seq
    last = first_seq - 1
    open_tasks: set[tuple[int, str]] = set()
    seen_tasks: set[tuple[int, str]] = set()
    for frame in frames:
        if frame.id is not None:
            payload = frame.json
            assert int(frame.id) == payload["seq"] == expected
            assert payload["type"] == frame.event
            expected += 1
            last = payload["seq"]
            data = payload["data"]
            if frame.event == "node.started":
                key = (data["run"], data["task_id"])
                assert key not in open_tasks
                open_tasks.add(key)
                seen_tasks.add(key)
            elif frame.event == "node.completed":
                key = (data["run"], data["task_id"])
                assert key in open_tasks or first_seq > 1
                open_tasks.discard(key)
        elif frame.event == "agent.token":
            payload = frame.json
            assert payload["after_seq"] == last
            key = (payload["data"]["run"], payload["data"]["task_id"])
            assert key in open_tasks, "fragment outside its node"


def _fragments(frames: list[SseEvent]) -> list[dict[str, Any]]:
    return [frame.json["data"] for frame in named(frames, "agent.token")]


# -- framing and headers ------------------------------------------------------------


async def test_completed_job_replays_and_closes_with_sse_framing(harness: HarnessFactory) -> None:
    async with harness() as h:
        status = await h.run_to_rest(PLAIN)
        stored = h.service.store.events(status["job_id"])
        async with _sse(h, status["job_id"]) as sse:
            frames = await sse.events_until_end()
            assert sse.status == 200
            headers = sse.headers
            raw = bytes(sse.body).decode("utf-8")

    assert headers["content-type"].startswith("text/event-stream")
    assert headers["cache-control"] == "no-cache"
    assert headers["x-accel-buffering"] == "no"
    assert headers["x-request-id"]
    assert frames[0].retry is not None and frames[0].event is None
    assert [int(frame.id or 0) for frame in durable(frames)] == [event.seq for event in stored]
    assert frames[-1].event == "job.completed"
    assert all("\r" not in line for line in raw.split("\n"))
    _assert_ordered(frames)
    # Static LLM: no streaming model, so no fragments, and every node reports zero.
    assert named(frames, "agent.token") == []
    for frame in named(frames, "node.completed"):
        assert frame.json["data"]["fragments_streamed"] == 0


async def test_status_view_links_to_the_event_stream(harness: HarnessFactory) -> None:
    async with harness() as h:
        status = await h.run_to_rest(PLAIN)
    assert status["links"]["events"] == _events_path(status["job_id"])


# -- live tokens ----------------------------------------------------------------------


@pytest.mark.parametrize("method", [None, "json_schema"], ids=["tool_calls", "json_content"])
async def test_live_stream_has_real_incremental_fragments_in_order(
    harness: HarnessFactory,
    tmp_path: Path,
    method: str | None,
) -> None:
    settings, runtime, llm = _streaming(
        tmp_path,
        method=method,
        pause_schema="AnalysisPlan",
        pause_after=20,
    )
    async with harness(settings=settings, runtime=runtime) as h:
        job_id = await _create(h)
        async with _sse(h, job_id) as sse:
            # The model is parked mid-response: fragments already on the wire were
            # streamed, not derived from a finished message.
            await asyncio.wait_for(llm.model.paused.wait(), 10)
            early = await sse.wait_for(lambda frame: frame.event == "agent.token")
            assert not llm.model.resume.is_set()
            assert not [
                frame
                for frame in named(early, "node.completed")
                if frame.json["data"]["node"] == "triage_planner"
            ]
            llm.model.resume.set()
            frames = early + await sse.events_until_end()
        await h.idle()
        decision = (await h.client.get(f"/v1/analyses/{job_id}/decision")).json()

    assert frames[-1].event == "job.completed"
    assert decision["result"]["risk_assessment"]["decision"] == "PASS"
    _assert_ordered(frames)
    fragments = _fragments(frames)
    assert [fragment["index"] for fragment in fragments] == list(range(1, len(fragments) + 1))
    channel = "tool_call_arguments" if method is None else "content"
    for fragment in fragments:
        assert fragment["run"] == 1
        assert fragment["channel"] == channel
        assert fragment["format"] == "structured_json"
        assert fragment["provisional"] is True
        assert fragment["verified"] is False
    # Reassembled per task and response, the fragments are exactly what the model
    # streamed for that schema: nothing invented, nothing lost.
    payloads = payloads_from(static_llm_responses())
    texts: dict[tuple[str, int], str] = {}
    schemas: dict[tuple[str, int], str] = {}
    for fragment in fragments:
        key = (fragment["task_id"], fragment["response"])
        texts[key] = texts.get(key, "") + fragment["text"]
        schemas[key] = fragment["schema_name"]
    assert sorted(schemas.values()) == sorted(payloads)
    for key, text in texts.items():
        assert text == payloads[schemas[key]]
    # Each node reports the fragments it streamed.
    per_task: dict[str, int] = {}
    for fragment in fragments:
        per_task[fragment["task_id"]] = per_task.get(fragment["task_id"], 0) + 1
    for frame in named(frames, "node.completed"):
        data = frame.json["data"]
        assert data["fragments_streamed"] == per_task.get(data["task_id"], 0)
        assert data["fragments_dropped"] == 0
    # Token accounting survives the streaming path (usage arrives on the last chunk).
    assert decision["result"]["trace"]["input_tokens"] == 6 * llm.model.input_tokens
    assert sorted(llm.model.streamed_calls) == sorted(payloads)
    assert llm.model.generated_calls == []


async def test_parallel_agents_are_distinguishable(harness: HarnessFactory, tmp_path: Path) -> None:
    settings, runtime, _ = _streaming(tmp_path, delay_seconds=0.001)
    async with harness(settings=settings, runtime=runtime) as h:
        job_id = await _create(h)
        async with _sse(h, job_id) as sse:
            frames = await sse.events_until_end()

    starts = [
        frame.json["data"]
        for frame in named(frames, "node.started")
        if frame.json["data"]["node"] == "run_parallel_agent"
    ]
    assert {start["agent"] for start in starts} == AGENTS
    assert len({start["task_id"] for start in starts}) == 4
    by_task = {start["task_id"]: start["agent"] for start in starts}
    for fragment in _fragments(frames):
        if fragment["node"] == "run_parallel_agent":
            assert fragment["agent"] == by_task[fragment["task_id"]]
    completed = [
        frame.json["data"]
        for frame in named(frames, "node.completed")
        if frame.json["data"]["node"] == "run_parallel_agent"
    ]
    assert {item["task_id"] for item in completed} == set(by_task)
    assert all(item["status"] == "ok" for item in completed)


# -- replay and reconnect -----------------------------------------------------------


async def test_last_event_id_replays_only_later_durable_events(harness: HarnessFactory) -> None:
    async with harness() as h:
        status = await h.run_to_rest(PLAIN)
        job_id = status["job_id"]
        total = len(h.service.store.events(job_id))
        async with _sse(h, job_id, **{"Last-Event-ID": "4"}) as sse:
            later = await sse.events_until_end()
        async with _sse(h, job_id, **{"Last-Event-ID": str(total)}) as sse:
            nothing = await sse.events_until_end()

    assert [int(frame.id or 0) for frame in durable(later)] == list(range(5, total + 1))
    _assert_ordered(later, first_seq=5)
    assert durable(nothing) == []
    assert nothing[0].retry is not None


async def test_reconnect_mid_run_resumes_without_duplicates_or_loss(
    harness: HarnessFactory,
    tmp_path: Path,
) -> None:
    settings, runtime, llm = _streaming(tmp_path, pause_schema="AnalysisPlan", pause_after=20)
    async with harness(settings=settings, runtime=runtime) as h:
        job_id = await _create(h)
        async with _sse(h, job_id) as first:
            await asyncio.wait_for(llm.model.paused.wait(), 10)
            part_one = await first.wait_for(lambda frame: frame.event == "agent.token")
        # The client went away mid-run; the analysis did not.
        assert h.service.bus.subscriber_count(job_id) == 0
        assert (await h.client.get(f"/v1/analyses/{job_id}")).json()["status"] == "running"
        last_id = durable(part_one)[-1].id
        assert last_id is not None
        llm.model.resume.set()
        async with _sse(h, job_id, **{"Last-Event-ID": last_id}) as second:
            part_two = await second.events_until_end()
        await h.idle()
        stored = h.service.store.events(job_id)

    seqs = [int(frame.id or 0) for frame in durable(part_one) + durable(part_two)]
    assert seqs == [event.seq for event in stored]
    assert part_two[-1].event == "job.completed"
    assert runtime.sessions == 1


async def test_disconnect_never_cancels_the_job(harness: HarnessFactory, tmp_path: Path) -> None:
    settings, runtime, llm = _streaming(tmp_path, pause_schema="AnalysisPlan", pause_after=20)
    async with harness(settings=settings, runtime=runtime) as h:
        job_id = await _create(h, GATED)
        sse = _sse(h, job_id)
        await sse.__aenter__()
        await asyncio.wait_for(llm.model.paused.wait(), 10)
        await sse.disconnect()
        assert sse.finished
        assert h.service.bus.subscriber_count(job_id) == 0
        llm.model.resume.set()
        await h.idle()
        status = (await h.client.get(f"/v1/analyses/{job_id}")).json()
    assert status["status"] == "waiting_for_approval"
    assert status["run_count"] == 1
    assert h.audit_lines() == []


async def test_many_subscribers_share_one_execution(
    harness: HarnessFactory, tmp_path: Path
) -> None:
    settings, runtime, llm = _streaming(tmp_path, pause_schema="AnalysisPlan", pause_after=1)
    async with harness(settings=settings, runtime=runtime) as h:
        job_id = await _create(h)
        await asyncio.wait_for(llm.model.paused.wait(), 10)
        clients = [_sse(h, job_id) for _ in range(3)]
        for client in clients:
            await client.__aenter__()
        assert h.service.bus.subscriber_count(job_id) == 3
        llm.model.resume.set()
        streams = await asyncio.gather(*(client.events_until_end() for client in clients))
        for client in clients:
            await client.disconnect()
        await h.idle()

    assert runtime.sessions == 1
    assert len(llm.model.streamed_calls) == 6  # one model call per schema, not per client
    reference = [(frame.id, frame.event, frame.data) for frame in streams[0] if frame.retry is None]
    for frames in streams[1:]:
        assert [(f.id, f.event, f.data) for f in frames if f.retry is None] == reference
    assert _fragments(streams[0])


async def test_slow_client_gets_a_gap_and_never_blocks_the_graph(
    harness: HarnessFactory,
    tmp_path: Path,
) -> None:
    settings, runtime, llm = _streaming(
        tmp_path,
        pause_schema="AnalysisPlan",
        pause_after=1,
        settings_overrides={"api_sse_queue_size": 4, "api_stream_fragment_max_chars": 16},
    )
    async with harness(settings=settings, runtime=runtime) as h:
        job_id = await _create(h)
        await asyncio.wait_for(llm.model.paused.wait(), 10)
        async with _sse(h, job_id) as sse:
            await sse.wait_for(lambda frame: frame.id is not None)
            sse.hold()  # stops reading: the server's sends block
            llm.model.resume.set()
            # The analysis finishes although this client reads nothing.
            await asyncio.wait_for(h.idle(), 20)
            assert (await h.client.get(f"/v1/analyses/{job_id}")).json()["status"] == "completed"
            sse.release()
            frames = await sse.events_until_end()
            seen = bytes(sse.body).decode("utf-8")
        stored = h.service.store.events(job_id)

    gaps = named(frames, "stream.gap")
    assert gaps, "a lagging client must be told it lost live events"
    assert all(gap.json["data"]["dropped_live_events"] > 0 for gap in gaps)
    # Durable events are complete and in order despite the lag (re-read from the store).
    received = [int(event.split("\n", 1)[0]) for event in seen.split("id: ")[1:]]
    assert received == [event.seq for event in stored]
    assert frames[-1].event == "job.completed"


# -- approval ---------------------------------------------------------------------------


async def test_stream_follows_approval_resume_on_the_original_thread(
    harness: HarnessFactory,
    tmp_path: Path,
) -> None:
    settings, runtime, _ = _streaming(
        tmp_path,
        settings_overrides={"api_sse_keepalive_seconds": 0.1},
    )
    async with harness(settings=settings, runtime=runtime) as h:
        job_id = await _create(h, GATED)
        async with _sse(h, job_id) as sse:
            waiting = await sse.wait_for(lambda frame: frame.event == "job.waiting_for_approval")
            before = (await h.client.get(f"/v1/analyses/{job_id}/decision")).json()
            # Waiting is not terminal: the stream stays open with keepalive comments.
            pause = await sse.wait_for(lambda frame: frame.comment == "keepalive")
            assert not sse.finished
            assert (
                await h.client.post(f"/v1/analyses/{job_id}/approval", json=APPROVE)
            ).status_code == 202
            rest = await sse.events_until_end()
        after = (await h.client.get(f"/v1/analyses/{job_id}/decision")).json()
        thread_id = h.service.store.get(job_id).thread_id  # type: ignore[union-attr]

    frames = waiting + pause + rest
    _assert_ordered([frame for frame in frames if frame.comment is None])
    types = [frame.event for frame in durable(frames)]
    lifecycle = [kind for kind in types if kind and kind.startswith("job.")]
    assert lifecycle == [
        "job.queued",
        "job.running",
        "job.waiting_for_approval",
        "job.approval_submitted",
        "job.queued",
        "job.running",
        "job.completed",
    ]
    second_run = [
        frame.json["data"]
        for frame in durable(rest)
        if frame.event and frame.event.startswith("node.")
    ]
    assert second_run and all(item["run"] == 2 for item in second_run)
    assert {item["node"] for item in second_run} >= {"human_approval", "record_gate_decision"}
    first_gate = [
        frame.json["data"]
        for frame in named(waiting, "node.completed")
        if frame.json["data"]["node"] == "human_approval"
    ]
    assert first_gate[0]["status"] == "interrupted"
    # The resume re-entered the paused task on the same thread (same LangGraph task id).
    resumed = [item for item in second_run if item["node"] == "human_approval"]
    assert resumed[0]["task_id"] == first_gate[0]["task_id"]
    assert thread_id == f"analysis-{job_id}"
    assert after["result"]["risk_assessment"] == before["result"]["risk_assessment"]
    assert after["result"]["approval"]["gate_decision_recorded"] is True
    assert len(h.audit_lines()) == 1
    assert runtime.sessions == 2


async def test_rejection_streams_to_completion_without_an_audit_write(
    harness: HarnessFactory,
    tmp_path: Path,
) -> None:
    settings, runtime, _ = _streaming(tmp_path)
    async with harness(settings=settings, runtime=runtime) as h:
        job_id = await _create(h, GATED)
        await h.idle()
        async with _sse(h, job_id, **{"Last-Event-ID": "1"}) as sse:
            await sse.wait_for(lambda frame: frame.event == "job.waiting_for_approval")
            body = {"approved": False, "reviewer": "qa-lead"}
            assert (
                await h.client.post(f"/v1/analyses/{job_id}/approval", json=body)
            ).status_code == 202
            rest = await sse.events_until_end()
        after = (await h.client.get(f"/v1/analyses/{job_id}/decision")).json()
    assert rest[-1].event == "job.completed"
    assert after["result"]["approval"]["status"] == "rejected"
    assert h.audit_lines() == []


async def test_progress_recording_failure_cannot_alter_the_run(
    harness: HarnessFactory,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings, runtime, _ = _streaming(tmp_path)

    def broken(*args: Any, **kwargs: Any) -> Any:
        msg = "event log unavailable"
        raise OSError(msg)

    monkeypatch.setattr(SqliteJobStore, "append_events", broken)
    async with harness(settings=settings, runtime=runtime) as h:
        job_id = await _create(h, GATED)
        await h.idle()
        before = (await h.client.get(f"/v1/analyses/{job_id}/decision")).json()
        assert before["status"] == "waiting_for_approval"
        assert (
            await h.client.post(f"/v1/analyses/{job_id}/approval", json=APPROVE)
        ).status_code == 202
        await h.idle()
        after = (await h.client.get(f"/v1/analyses/{job_id}/decision")).json()
        stored = [event.type for event in h.service.store.events(job_id)]
    assert after["status"] == "completed"
    assert after["result"]["risk_assessment"] == before["result"]["risk_assessment"]
    assert len(h.audit_lines()) == 1
    assert JobEventType.NODE_STARTED not in stored


# -- failure, restart, incomplete --------------------------------------------------------


async def test_failed_job_stream_ends_with_a_sanitized_failure(harness: HarnessFactory) -> None:
    runtime = FakeRuntime(session_error=RuntimeError("INTERNAL-DETAIL-MARKER /srv/secret"))
    async with harness(runtime=runtime) as h:
        job_id = await _create(h)
        async with _sse(h, job_id) as sse:
            frames = await sse.events_until_end()
            raw = bytes(sse.body).decode("utf-8")
    assert frames[-1].event == "job.failed"
    assert frames[-1].json["data"] == {"code": "JOB_EXECUTION_FAILED"}
    assert "INTERNAL-DETAIL-MARKER" not in raw
    assert "Traceback" not in raw


async def test_interrupted_run_replays_after_restart_and_ends(
    harness: HarnessFactory,
    tmp_path: Path,
) -> None:
    settings, runtime, llm = _streaming(tmp_path, pause_schema="AnalysisPlan", pause_after=1)
    async with harness(settings=settings, runtime=runtime) as first:
        job_id = await _create(first)
        await asyncio.wait_for(llm.model.paused.wait(), 10)
    # Shutdown cancelled the run (JOB_INTERRUPTED). A new process replays the log.
    async with (
        harness(settings=settings, runtime=FakeRuntime()) as second,
        _sse(second, job_id) as sse,
    ):
        frames = await sse.events_until_end()
    assert frames[-1].event == "job.failed"
    assert frames[-1].json["data"] == {"code": "JOB_INTERRUPTED"}
    started = [f.json["data"]["node"] for f in named(frames, "node.started")]
    assert "triage_planner" in started
    _assert_ordered(frames)
    assert second.runtime.sessions == 0  # replaying never re-runs anything


async def test_incomplete_checkpoint_is_never_reported_completed(
    harness: HarnessFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def stalled(*, thread_id: str, settings: Any = None) -> CheckpointSnapshot:
        _ = (thread_id, settings)
        return CheckpointSnapshot(state={}, next_nodes=("risk_engine",))

    monkeypatch.setattr("app.jobs.runtime.read_checkpoint", stalled)
    async with harness() as h:
        status = await h.run_to_rest(PLAIN)
        types = [event.type for event in h.service.store.events(status["job_id"])]
    assert status["status"] == "failed"
    assert status["error"]["code"] == "GRAPH_INCOMPLETE"
    assert JobEventType.JOB_COMPLETED not in types


# -- providers that do not stream, and the opt-in --------------------------------------


async def test_non_streaming_provider_yields_no_fabricated_fragments(
    harness: HarnessFactory,
    tmp_path: Path,
) -> None:
    settings, runtime, llm = _streaming(tmp_path, disable_streaming=True)
    async with harness(settings=settings, runtime=runtime) as h:
        job_id = await _create(h)
        async with _sse(h, job_id) as sse:
            frames = await sse.events_until_end()
    assert frames[-1].event == "job.completed"
    assert named(frames, "agent.token") == []
    assert all(f.json["data"]["fragments_streamed"] == 0 for f in named(frames, "node.completed"))
    # The model really answered, just not incrementally; nothing was replayed as tokens.
    assert len(llm.model.generated_calls) == 6
    assert llm.model.streamed_calls == []


async def test_token_streaming_off_keeps_providers_on_non_streaming_calls(
    harness: HarnessFactory,
    tmp_path: Path,
) -> None:
    settings, runtime, llm = _streaming(tmp_path, stream_tokens=False)
    async with harness(settings=settings, runtime=runtime) as h:
        job_id = await _create(h)
        async with _sse(h, job_id) as sse:
            frames = await sse.events_until_end()
    assert named(frames, "node.started")
    assert named(frames, "agent.token") == []
    assert llm.model.streamed_calls == []
    assert len(llm.model.generated_calls) == 6


# -- sanitization -------------------------------------------------------------------------

CONFIGURED_SECRET = "cfg-secret-value-0123456789"
GOOGLE_SHAPED = "AIza" + "Sy" + "B" * 33


async def test_fragments_are_redacted_and_carry_no_internal_data(
    harness: HarnessFactory,
    tmp_path: Path,
) -> None:
    responses = static_llm_responses()
    responses[ChangeImpact] = ChangeImpact(
        change_id="CHG-H001",
        impacted_services=["svc-analytics"],
        summary=f"key {GOOGLE_SHAPED} and {CONFIGURED_SECRET} and ‮bidi​ end",
    )
    responses[AnalysisPlan] = AnalysisPlan.model_validate(
        {
            **responses[AnalysisPlan].model_dump(),
            "rationale": "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\n-----END RSA PRIVATE KEY-----",
        },
    )
    settings, runtime, _ = _streaming(
        tmp_path,
        method="json_schema",
        responses=responses,
        thinking_block="PRIVATE-REASONING-MARKER",
        fragment_size=5,  # every secret is split across several chunks
        settings_overrides={"gemini_api_key": CONFIGURED_SECRET},
    )
    async with harness(settings=settings, runtime=runtime) as h:
        job_id = await _create(h)
        async with _sse(h, job_id) as sse:
            frames = await sse.events_until_end()
            raw = bytes(sse.body).decode("utf-8")
        decision = (await h.client.get(f"/v1/analyses/{job_id}/decision")).json()

    assert frames[-1].event == "job.completed"
    fragments = _fragments(frames)
    streamed = "".join(fragment["text"] for fragment in fragments)
    for forbidden in (
        GOOGLE_SHAPED,
        CONFIGURED_SECRET,
        "MIIEow",
        "PRIVATE-REASONING-MARKER",
        "‮",
        "​",
    ):
        assert forbidden not in raw
    assert "[REDACTED]" in streamed
    assert "bidi" in streamed  # only the control characters were removed
    # Node events name state keys, never their values; no retrieved text, no prompts.
    for chunk in DEFAULT_CHUNKS:
        assert chunk.content not in raw
    assert "You are" not in raw
    allowed = {
        "node",
        "task_id",
        "agent",
        "run",
        "status",
        "updated_keys",
        "fragments_streamed",
        "fragments_dropped",
    }
    for frame in named(frames, "node.started") + named(frames, "node.completed"):
        assert set(frame.json["data"]) <= allowed
    # Redaction is a stream policy only: the analysis itself is unchanged.
    assert decision["result"]["risk_assessment"]["decision"] == "PASS"


async def test_fragment_cap_drops_and_counts(harness: HarnessFactory, tmp_path: Path) -> None:
    settings, runtime, _ = _streaming(
        tmp_path,
        settings_overrides={
            "api_stream_max_fragments_per_run": 3,
            "api_stream_fragment_max_chars": 16,
        },
    )
    async with harness(settings=settings, runtime=runtime) as h:
        job_id = await _create(h)
        async with _sse(h, job_id) as sse:
            frames = await sse.events_until_end()
    fragments = _fragments(frames)
    assert [fragment["index"] for fragment in fragments] == [1, 2, 3]
    assert all(len(fragment["text"]) <= 16 for fragment in fragments)
    dropped = sum(f.json["data"]["fragments_dropped"] for f in named(frames, "node.completed"))
    assert dropped > 0
    assert frames[-1].event == "job.completed"


# -- access and errors ----------------------------------------------------------------------


async def test_unknown_wrong_kind_and_malformed_ids(harness: HarnessFactory) -> None:
    async with harness() as h:
        evaluation = await h.client.post("/v1/evaluations", json={})
        await h.idle()
        missing = await h.client.get(_events_path("job-" + "0" * 32))
        wrong_kind = await h.client.get(_events_path(evaluation.json()["job_id"]))
        malformed = await h.client.get(_events_path("not-a-job"))
        status = await h.run_to_rest(PLAIN)
        bad_cursor = await h.client.get(
            _events_path(status["job_id"]),
            headers={"Last-Event-ID": "abc"},
        )
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "JOB_NOT_FOUND"
    assert wrong_kind.status_code == 404
    assert wrong_kind.json()["error"]["code"] == "JOB_NOT_FOUND"
    assert malformed.status_code == 422
    assert bad_cursor.status_code == 422
    assert bad_cursor.json()["error"]["details"] == {"field": "Last-Event-ID"}


async def test_event_stream_uses_the_api_access_guard(
    harness: HarnessFactory, tmp_path: Path
) -> None:
    key = "stream-test-key-0001"
    async with harness(api_key=SecretStr(key)) as h:
        h.client.headers["Authorization"] = f"Bearer {key}"
        job_id = (await h.run_to_rest(PLAIN))["job_id"]
        anonymous = await h.client.get(_events_path(job_id), headers={"Authorization": ""})
        wrong = await h.client.get(_events_path(job_id), headers={"Authorization": "Bearer nope"})
        async with _sse(h, job_id, Authorization=f"Bearer {key}") as sse:
            frames = await sse.events_until_end()
    assert anonymous.status_code == 401
    assert wrong.status_code == 401
    assert frames[-1].event == "job.completed"

    async with harness() as h:
        job_id = (await h.run_to_rest(PLAIN))["job_id"]
        remote = AsgiSseClient(h.app, _events_path(job_id), client=("203.0.113.9", 4000))
        async with remote:
            body = bytes(remote.body)
        assert remote.status == 403
        assert b"NON_LOCAL_CLIENT" in body


async def test_subscriber_cap_is_enforced_before_streaming(
    harness: HarnessFactory,
    tmp_path: Path,
) -> None:
    settings = api_settings(tmp_path, api_sse_max_subscribers_per_job=1)
    async with harness(settings=settings) as h:
        job_id = (await h.run_to_rest(GATED))["job_id"]  # waiting: streams stay open
        async with _sse(h, job_id) as first:
            await first.wait_for(lambda frame: frame.event == "job.waiting_for_approval")
            refused = await h.client.get(_events_path(job_id))
        reopened = _sse(h, job_id)
        async with reopened:
            await reopened.wait_for(lambda frame: frame.event == "job.waiting_for_approval")
    assert refused.status_code == 429
    assert refused.json()["error"]["code"] == "TOO_MANY_SUBSCRIBERS"
    assert refused.json()["error"]["details"]["max_subscribers"] == 1
    assert h.service.bus.subscriber_count(job_id) == 0


# -- cursor policy and terminal handling (review M2) ------------------------------------


async def test_future_cursor_on_an_active_job_is_refused(harness: HarnessFactory) -> None:
    """Review M2: ``Last-Event-ID: 999`` on a running job used to hang forever."""
    llm = BlockingLLM()
    async with harness(runtime=FakeRuntime(llm)) as h:
        job_id = await _create(h)
        await asyncio.wait_for(llm.entered.wait(), 10)
        last = h.service.store.last_seq(job_id)
        async with _sse(h, job_id, **{"Last-Event-ID": "999"}) as sse:
            status = sse.status
            body = bytes(sse.body).decode("utf-8")
        async with _sse(h, job_id, **{"Last-Event-ID": str(last + 1)}) as sse:
            next_status = sse.status
            next_body = bytes(sse.body).decode("utf-8")
        assert h.service.bus.subscriber_count(job_id) == 0
        llm.release()
        await h.idle()
    assert status == 422
    error = json.loads(body)["error"]
    assert error["code"] == "INVALID_REQUEST"
    # Review L2: the latest committed id survives the details allowlist, so a client
    # can resume from it.
    assert error["details"] == {"field": "Last-Event-ID", "latest_event_id": last}
    assert next_status == 422  # even one past the latest committed event
    assert json.loads(next_body)["error"]["details"]["latest_event_id"] == last


@pytest.mark.parametrize("outcome", ["completed", "failed"])
async def test_cursor_at_latest_event_follows_to_the_terminal_event(
    harness: HarnessFactory,
    tmp_path: Path,
    outcome: str,
) -> None:
    llm = BlockingLLM()
    runtime = FakeRuntime(llm)
    if outcome == "failed":
        runtime = FakeRuntime(
            llm,
            wrap_mcp=lambda mcp: FailingToolMcp(mcp, failing_tool="get_change_package"),
        )
    async with harness(runtime=runtime) as h:
        job_id = await _create(h)
        await asyncio.wait_for(llm.entered.wait(), 10)
        cursor = h.service.store.last_seq(job_id)  # read while events keep committing
        async with _sse(h, job_id, **{"Last-Event-ID": str(cursor)}) as sse:
            llm.release()
            frames = await sse.events_until_end(timeout=20)
        await h.idle()
        stored = h.service.store.events(job_id)
    assert [int(frame.id or 0) for frame in durable(frames)] == [
        event.seq for event in stored if event.seq > cursor
    ]
    assert frames[-1].event in {"job.completed", "job.failed"}
    assert h.service.bus.subscriber_count(job_id) == 0


async def test_cursor_equal_to_the_terminal_event_closes_at_once(harness: HarnessFactory) -> None:
    async with harness() as h:
        job_id = (await h.run_to_rest(PLAIN))["job_id"]
        last = h.service.store.last_seq(job_id)
        async with _sse(h, job_id, **{"Last-Event-ID": str(last)}) as sse:
            frames = await sse.events_until_end(timeout=5)
    assert durable(frames) == []
    assert h.service.bus.subscriber_count(job_id) == 0


async def test_stream_ends_when_the_job_is_terminal_even_if_its_event_was_skipped(
    harness: HarnessFactory,
) -> None:
    """Defence in depth below the route check: a cursor past the terminal event
    (constructed directly) still ends the stream instead of holding the slot."""
    llm = BlockingLLM()
    async with harness(runtime=FakeRuntime(llm), api_sse_keepalive_seconds=0.1) as h:
        job_id = await _create(h)
        await asyncio.wait_for(llm.entered.wait(), 10)
        stream = JobEventStream(
            store=h.service.store,
            subscription=h.service.bus.subscribe(job_id),
            after_seq=999,
            keepalive_seconds=0.1,
            retry_ms=1000,
        )

        async def drain() -> list[bytes]:
            return [frame async for frame in stream.frames()]

        reader = asyncio.create_task(drain())
        llm.release()
        await h.idle()
        frames = await asyncio.wait_for(reader, 10)
        assert h.service.bus.subscriber_count(job_id) == 0
    assert not any(frame.startswith(b"id:") for frame in frames)


async def test_cursor_at_waiting_follows_approval_resume_to_completion(
    harness: HarnessFactory,
) -> None:
    async with harness() as h:
        job_id = (await h.run_to_rest(GATED))["job_id"]
        cursor = h.service.store.last_seq(job_id)
        async with _sse(h, job_id, **{"Last-Event-ID": str(cursor)}) as sse:
            approved = await h.client.post(f"/v1/analyses/{job_id}/approval", json=APPROVE)
            assert approved.status_code == 202
            frames = await sse.events_until_end(timeout=20)
    assert [frame.event for frame in durable(frames)][:3] == [
        "job.approval_submitted",
        "job.queued",
        "job.running",
    ]
    assert frames[-1].event == "job.completed"
    assert int(durable(frames)[0].id or 0) == cursor + 1
