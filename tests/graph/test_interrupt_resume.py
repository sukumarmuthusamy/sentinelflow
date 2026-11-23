"""Graph interrupt/resume, counters across resume, and record_gate_decision exactly-once."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from app.graph.state import AnalysisRequest
from app.model_gateway.llm import StaticStructuredLLM
from tests.contract.conftest import mcp_session
from tests.graph.conftest import static_llm_responses
from tests.graph.fakes import DEFAULT_CHUNKS, FakeRetriever


async def _pause_then_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    thread_id: str,
    resume_payloads: list[dict[str, Any]],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from app.core.settings import get_settings
    from app.graph.runner import resume_analysis, run_analysis
    from mcp_server.sentinelflow_context.settings import clear_mcp_server_settings_cache

    clear_mcp_server_settings_cache()
    llm = StaticStructuredLLM(static_llm_responses())
    request = AnalysisRequest(
        change_id="CHG-H001",
        domain_pack_id="healthcare-claims",
        record_decision=True,
    )
    async with mcp_session() as mcp:
        get_settings.cache_clear()
        monkeypatch.setenv("GRAPH_CHECKPOINT_PATH", str(tmp_path / "cp.sqlite"))
        monkeypatch.setenv("ANALYSIS_MAX_LLM_CALLS", "50")
        settings = get_settings()
        paused = await run_analysis(
            request,
            mcp=mcp,
            llm=llm,
            thread_id=thread_id,
            settings=settings,
            retriever=FakeRetriever(DEFAULT_CHUNKS),
        )
        resumed = [
            await resume_analysis(
                mcp=mcp,
                llm=llm,
                thread_id=thread_id,
                request=request,
                resume_payload=payload,
                settings=settings,
                retriever=FakeRetriever(DEFAULT_CHUNKS),
            )
            for payload in resume_payloads
        ]
    get_settings.cache_clear()
    return paused, resumed


def _audit_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


@pytest.mark.asyncio
async def test_interrupt_resume_record_gate_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    audit_log_path: Path,
) -> None:
    paused, (resumed,) = await _pause_then_resume(
        tmp_path,
        monkeypatch,
        thread_id="test-thread-interrupt",
        resume_payloads=[{"approved": True}],
    )
    assert paused.get("__interrupt__"), "expected interrupt before external write"
    assert resumed.get("record_decision_written") is True
    assert len(_audit_lines(audit_log_path)) == 1
    # Resume rebuilds the context; trace counters must carry over, not reset.
    before = paused["trace_metrics"]
    after = resumed["trace_metrics"]
    assert before["llm_calls"] > 0
    assert after["llm_calls"] == before["llm_calls"]
    assert after["input_tokens"] == before["input_tokens"]
    assert after["mcp_tool_calls"] >= before["mcp_tool_calls"]


@pytest.mark.asyncio
async def test_rejected_approval_writes_nothing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    audit_log_path: Path,
) -> None:
    _, (resumed,) = await _pause_then_resume(
        tmp_path,
        monkeypatch,
        thread_id="test-thread-reject",
        resume_payloads=[{"approved": False}],
    )
    assert resumed["approval_status"] == "rejected"
    assert not resumed.get("record_decision_written")
    assert _audit_lines(audit_log_path) == []


@pytest.mark.asyncio
async def test_second_resume_does_not_write_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    audit_log_path: Path,
) -> None:
    _, resumed = await _pause_then_resume(
        tmp_path,
        monkeypatch,
        thread_id="test-thread-twice",
        resume_payloads=[{"approved": True}, {"approved": True}],
    )
    assert resumed[0]["record_decision_written"] is True
    assert len(_audit_lines(audit_log_path)) == 1


def test_restore_counters_carries_budget_across_resume() -> None:
    from app.graph.budget import AnalysisBudget
    from app.graph.context import AnalysisGraphContext
    from app.graph.runner import restore_counters

    context = AnalysisGraphContext(
        domain_pack=None,  # type: ignore[arg-type]
        mcp=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        budget=AnalysisBudget(
            max_llm_calls=8,
            max_input_tokens=10_000,
            max_output_tokens=10_000,
            max_parallel_agents=4,
        ),
    )
    restore_counters(
        context,
        {
            "trace_metrics": {
                "llm_calls": 8,
                "input_tokens": 900,
                "output_tokens": 50,
                "repair_count": 1,
            }
        },
    )
    assert context.budget.llm_calls == 8
    assert context.budget.input_tokens == 900
    assert context.trace.repair_count == 1
    from app.graph.budget import BudgetExhaustedError

    with pytest.raises(BudgetExhaustedError):
        context.budget.check_llm_call()


def test_restore_counters_carries_failed_calls_and_clears_in_flight() -> None:
    from app.graph.budget import AnalysisBudget, BudgetExhaustedError
    from app.graph.context import AnalysisGraphContext
    from app.graph.runner import restore_counters

    budget = AnalysisBudget(
        max_llm_calls=4,
        max_input_tokens=10_000,
        max_output_tokens=10_000,
        max_parallel_agents=4,
    )
    budget.llm_calls_in_flight = 3  # a stale in-flight count must not survive resume
    context = AnalysisGraphContext(
        domain_pack=None,  # type: ignore[arg-type]
        mcp=None,  # type: ignore[arg-type]
        llm=None,  # type: ignore[arg-type]
        budget=budget,
    )
    restore_counters(context, {"trace_metrics": {"llm_calls": 2, "llm_calls_failed": 1}})
    assert context.trace.llm_calls_failed == 1
    assert (budget.llm_calls, budget.llm_calls_failed, budget.llm_calls_in_flight) == (2, 1, 0)
    budget.reserve_llm_call().complete()  # the fourth and last call
    with pytest.raises(BudgetExhaustedError, match="max_llm_calls"):
        budget.reserve_llm_call()
