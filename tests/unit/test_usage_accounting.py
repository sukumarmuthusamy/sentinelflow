"""Token accounting when a provider's usage report is missing or malformed (review M4)."""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from app.core.settings import Settings
from app.graph.runner import run_analysis
from app.graph.state import AnalysisPlan, AnalysisRequest
from app.graph.streaming import GraphEvent
from app.model_gateway.llm import CHARS_PER_TOKEN_ESTIMATE, account_usage
from langchain_core.messages import AIMessage

from tests.contract.conftest import mcp_session
from tests.graph.conftest import static_llm_responses
from tests.graph.fakes import DEFAULT_CHUNKS, FakeRetriever
from tests.graph.streaming_fakes import FakeStreamingGatewayLLM

PROMPT_CHARS = 4_000
REQUEST = AnalysisRequest(change_id="CHG-H001", domain_pack_id="healthcare-claims")


def _raw(usage: Any, content: str = '{"a": 1}') -> Any:
    return SimpleNamespace(usage_metadata=usage, content=content, tool_calls=[])


def test_valid_usage_is_used_as_reported() -> None:
    account = account_usage(
        _raw({"input_tokens": 120, "output_tokens": 30}),
        prompt_chars=PROMPT_CHARS,
    )
    assert (account.input_tokens, account.output_tokens, account.estimated) == (120, 30, False)


@pytest.mark.parametrize(
    "usage",
    [
        None,
        {},
        {"input_tokens": "bad", "output_tokens": 5},
        {"input_tokens": None, "output_tokens": None},
        {"input_tokens": -4, "output_tokens": 9},
        {"input_tokens": True, "output_tokens": 9},
        {"input_tokens": 1.5, "output_tokens": 9},
        {"input_tokens": float("nan"), "output_tokens": 9},
        {"input_tokens": 0, "output_tokens": 0},
        "not-a-dict",
    ],
    ids=[
        "absent",
        "empty",
        "non-numeric",
        "null",
        "negative",
        "bool",
        "fractional",
        "nan",
        "zero",
        "wrong-type",
    ],
)
def test_unknown_usage_is_never_zero_and_never_raises(usage: Any) -> None:
    account = account_usage(_raw(usage), prompt_chars=PROMPT_CHARS)
    assert account.estimated
    assert account.input_tokens >= math.ceil(PROMPT_CHARS / CHARS_PER_TOKEN_ESTIMATE)
    assert account.output_tokens >= 1


def test_a_partly_valid_report_keeps_the_valid_field() -> None:
    account = account_usage(_raw({"input_tokens": 900}), prompt_chars=PROMPT_CHARS)
    assert account.input_tokens == 900
    assert account.output_tokens >= 1
    assert account.estimated


def test_output_estimate_counts_tool_call_arguments() -> None:
    raw = AIMessage(
        content="",
        tool_calls=[{"name": "X", "args": {"summary": "s" * 300}, "id": "c1"}],
    )
    account = account_usage(raw, prompt_chars=10)
    assert account.output_tokens >= 300 // CHARS_PER_TOKEN_ESTIMATE


async def test_malformed_usage_after_a_billed_call_does_not_raise() -> None:
    """Review M4c: ``int("bad")`` used to raise after the provider had answered."""
    llm = FakeStreamingGatewayLLM(
        static_llm_responses(),
        method="json_schema",
        disable_streaming=True,
        raw_usage_override={"input_tokens": "bad", "output_tokens": 5},
    )
    result = await llm.generate(
        model="fake",
        temperature=0.0,
        system_prompt="s" * 3_000,
        user_prompt="u" * 1_000,
        schema=AnalysisPlan,
    )
    assert result.value.change_category == "documentation"
    assert result.input_tokens >= 4_000 // CHARS_PER_TOKEN_ESTIMATE
    assert result.output_tokens >= 1
    assert llm.model.generated_calls == ["AnalysisPlan"]  # answered once, not retried


@pytest.mark.parametrize("stream", [True, False], ids=["streamed", "not_streamed"])
async def test_missing_usage_cannot_bypass_token_budgets(tmp_path: Path, stream: bool) -> None:
    """Review M4d: six calls with no usage report used to settle as 0 tokens."""
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        graph_checkpoint_path=tmp_path / "checkpoints.sqlite",
        gemini_api_key=None,
        openrouter_api_key=None,
        analysis_max_input_tokens=1_000,
        analysis_max_output_tokens=1_000,
    )
    llm = FakeStreamingGatewayLLM(
        static_llm_responses(),
        settings=settings,
        report_usage=False,
        disable_streaming=not stream,
    )

    async def ignore(event: GraphEvent) -> None:
        _ = event

    async with mcp_session() as mcp:
        state = await run_analysis(
            REQUEST,
            mcp=mcp,
            llm=llm,
            thread_id="budget",
            analysis_id="budget",
            settings=settings,
            enable_retrieval=False,
            retriever=FakeRetriever(DEFAULT_CHUNKS),
            on_event=ignore,
            stream_tokens=stream,
        )
    trace = state["trace_metrics"]
    assert trace["input_tokens"] >= 1_000  # unknown usage counted, not zeroed
    assert trace["llm_calls"] < 6  # the token budget stopped further calls
    assert state["degraded_to_review"] is True
    calls = llm.model.streamed_calls if stream else llm.model.generated_calls
    assert len(calls) == trace["llm_calls"]
