"""``stream_graph`` is ``ainvoke`` with observation attached: same run, same result."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from app.core.settings import Settings
from app.graph.runner import read_checkpoint, resume_analysis, run_analysis
from app.graph.state import AnalysisRequest
from app.graph.streaming import (
    INTERRUPT_KEY,
    GraphEvent,
    GraphStreamTranslator,
    NodeFinished,
    NodeStarted,
    OutputFragment,
)
from app.model_gateway.llm import STRUCTURED_OUTPUT_METADATA_KEY, StaticStructuredLLM
from langchain_core.messages import AIMessage, AIMessageChunk
from tests.contract.conftest import mcp_session
from tests.graph.conftest import static_llm_responses
from tests.graph.fakes import DEFAULT_CHUNKS, FakeRetriever

PLAIN = AnalysisRequest(change_id="CHG-H001", domain_pack_id="healthcare-claims")
GATED = AnalysisRequest(
    change_id="CHG-H001",
    domain_pack_id="healthcare-claims",
    record_decision=True,
)


def _settings(tmp_path: Path) -> Settings:
    return Settings(  # type: ignore[call-arg]
        _env_file=None,
        graph_checkpoint_path=tmp_path / "checkpoints.sqlite",
        gemini_api_key=None,
        openrouter_api_key=None,
    )


async def _run(
    request: AnalysisRequest,
    settings: Settings,
    thread_id: str,
    *,
    events: list[GraphEvent] | None,
) -> dict[str, Any]:
    async def collect(event: GraphEvent) -> None:
        assert events is not None
        events.append(event)

    async with mcp_session() as mcp:
        return await run_analysis(
            request,
            mcp=mcp,
            llm=StaticStructuredLLM(static_llm_responses(), input_tokens=5, output_tokens=3),
            thread_id=thread_id,
            analysis_id="analysis-equivalence",
            settings=settings,
            enable_retrieval=False,
            retriever=FakeRetriever(DEFAULT_CHUNKS),
            on_event=collect if events is not None else None,
            stream_tokens=True,
        )


def _comparable(state: dict[str, Any]) -> dict[str, Any]:
    values = {key: value for key, value in state.items() if key != INTERRUPT_KEY}
    interrupts = [item.value for item in state.get(INTERRUPT_KEY, [])]
    return {**values, INTERRUPT_KEY: interrupts}


@pytest.mark.parametrize("request_", [PLAIN, GATED], ids=["completes", "interrupts"])
async def test_streamed_run_returns_what_ainvoke_returns(
    tmp_path: Path,
    request_: AnalysisRequest,
) -> None:
    settings = _settings(tmp_path)
    events: list[GraphEvent] = []
    invoked = await _run(request_, settings, "thread-invoke", events=None)
    streamed = await _run(request_, settings, "thread-stream", events=events)

    assert _comparable(streamed) == _comparable(invoked)
    assert bool(streamed.get(INTERRUPT_KEY)) is (request_ is GATED)
    # The stored checkpoint agrees with the returned state.
    stored = await read_checkpoint(thread_id="thread-stream", settings=settings)
    assert _comparable(stored.state) == _comparable(streamed)
    assert stored.interrupted is (request_ is GATED)
    assert stored.finished is (request_ is PLAIN)
    # Every started task finished, exactly once; a static LLM streams nothing.
    started = [event.task_id for event in events if isinstance(event, NodeStarted)]
    finished = [event.task_id for event in events if isinstance(event, NodeFinished)]
    assert sorted(started) == sorted(finished)
    assert len(set(started)) == len(started)
    assert not [event for event in events if isinstance(event, OutputFragment)]


async def test_streamed_resume_preserves_counters_and_finishes(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    paused = await _run(GATED, settings, "thread-gate", events=[])
    events: list[GraphEvent] = []

    async def collect(event: GraphEvent) -> None:
        events.append(event)

    async with mcp_session() as mcp:
        resumed = await resume_analysis(
            mcp=mcp,
            llm=StaticStructuredLLM(static_llm_responses()),
            thread_id="thread-gate",
            request=GATED,
            resume_payload={"approved": True},
            settings=settings,
            enable_retrieval=False,
            retriever=FakeRetriever(DEFAULT_CHUNKS),
            on_event=collect,
        )
    assert INTERRUPT_KEY not in resumed
    assert resumed["risk_assessment"] == paused["risk_assessment"]
    for name in ("llm_calls", "input_tokens", "output_tokens"):
        assert resumed["trace_metrics"][name] == paused["trace_metrics"][name]
    stored = await read_checkpoint(thread_id="thread-gate", settings=settings)
    assert stored.finished
    names = [event.node for event in events if isinstance(event, NodeStarted)]
    assert names[0] == "human_approval"
    assert "record_gate_decision" in names


# -- translator --------------------------------------------------------------------------

TAG = {STRUCTURED_OUTPUT_METADATA_KEY: "ChangeImpact"}


def _message(chunk: Any, metadata: dict[str, Any]) -> dict[str, Any]:
    return {"type": "messages", "ns": (), "data": (chunk, metadata)}


def test_translator_keeps_only_names_and_written_key_names() -> None:
    translator = GraphStreamTranslator()
    start = {
        "type": "tasks",
        "ns": (),
        "data": {
            "id": "t-1",
            "name": "run_parallel_agent",
            "input": {"agent_name": "security_auditor", "retrieved_chunks": ["raw text"]},
            "triggers": ("branch",),
        },
    }
    result = {
        "type": "tasks",
        "ns": (),
        "data": {
            "id": "t-1",
            "name": "run_parallel_agent",
            "error": None,
            "result": {"security_findings": {"secret": "value"}, "not_a_state_key": 1},
            "interrupts": [],
        },
    }
    assert translator.translate(start) == [
        NodeStarted(node="run_parallel_agent", task_id="t-1", agent="security_auditor"),
    ]
    assert translator.translate(result) == [
        NodeFinished(
            node="run_parallel_agent",
            task_id="t-1",
            agent="security_auditor",
            status="ok",
            updated_keys=("security_findings",),
        ),
    ]


def test_translator_reports_errors_and_interrupts_without_their_content() -> None:
    translator = GraphStreamTranslator()
    failed = translator.translate(
        {
            "type": "tasks",
            "ns": (),
            "data": {"id": "t", "name": "risk_engine", "error": "boom /etc", "result": None},
        },
    )
    paused = translator.translate(
        {
            "type": "tasks",
            "ns": (),
            "data": {
                "id": "u",
                "name": "human_approval",
                "error": None,
                "result": {},
                "interrupts": [{"value": {"report": "..."}}],
            },
        },
    )
    assert isinstance(failed[0], NodeFinished) and failed[0].status == "error"
    assert isinstance(paused[0], NodeFinished) and paused[0].status == "interrupted"
    assert "boom" not in repr(failed) and "report" not in repr(paused)


def test_translator_forwards_only_streamed_tagged_answer_text() -> None:
    translator = GraphStreamTranslator()
    meta = {**TAG, "langgraph_checkpoint_ns": "change_analyst:t-9", "langgraph_node": "x"}
    content = AIMessageChunk(
        content=[{"type": "thinking", "thinking": "hidden"}, {"type": "text", "text": '{"a"'}],
        id="m-1",
    )
    tool = AIMessageChunk(
        content="",
        tool_call_chunks=[{"name": None, "args": ':"b"}', "id": None, "index": 0}],
        id="m-1",
    )
    events = translator.translate(_message(content, meta)) + translator.translate(
        _message(tool, meta),
    )
    assert [(e.channel, e.text, e.task_id, e.schema) for e in events] == [  # type: ignore[union-attr]
        ("content", '{"a"', "t-9", "ChangeImpact"),
        ("tool_call_arguments", ':"b"}', "t-9", "ChangeImpact"),
    ]
    # A finished message (a model that did not stream) is never replayed as tokens,
    # and model calls the gateway did not tag are never forwarded.
    assert translator.translate(_message(AIMessage(content='{"a":1}'), meta)) == []
    untagged = {"langgraph_checkpoint_ns": "x:t"}
    assert translator.translate(_message(AIMessageChunk(content="x"), untagged)) == []
    # Subgraph parts are ignored rather than misattributed.
    assert translator.translate({**_message(content, meta), "ns": ("sub:1",)}) == []
