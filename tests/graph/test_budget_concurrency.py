"""The LLM call budget holds under parallel fan-out and across resume.

Branches run concurrently, so a check-then-record budget lets every branch pass the
check before any of them records its call. These tests use an LLM that yields
inside ``generate`` so all branches are in flight together, which is exactly the
interleaving that overshot the limit.
"""

from __future__ import annotations

import pytest
from tests.graph.conftest import RunGraph, static_llm_responses
from tests.graph.fakes import GatedLLM


@pytest.mark.asyncio
async def test_parallel_branches_cannot_exceed_max_llm_calls(run_graph: RunGraph) -> None:
    """Regression: max_llm_calls=2 with four parallel agents made five calls."""
    llm = GatedLLM(static_llm_responses(), release_at=4)
    state = await run_graph(
        "CHG-H001",
        llm,
        env={"ANALYSIS_MAX_LLM_CALLS": "2", "ANALYSIS_MAX_PARALLEL_AGENTS": "4"},
    )
    # One triage call plus exactly one specialist; the other branches are refused.
    assert len(llm.calls) == 2
    assert state["trace_metrics"]["llm_calls"] == 2
    assert state["degraded_to_review"] is True
    assert state["risk_assessment"]["decision"] == "REVIEW"
    statuses = sorted(row["status"] for row in state["parallel_completions"])
    assert statuses.count("ok") == 1
    assert statuses.count("budget_exhausted") == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("max_calls", [1, 2, 3, 4, 5])
async def test_llm_calls_never_exceed_limit_at_any_width(
    run_graph: RunGraph,
    max_calls: int,
) -> None:
    # Hold every call until all four branches are inside generate (or refused).
    llm = GatedLLM(static_llm_responses(), release_at=4, timeout=0.2)
    state = await run_graph(
        "CHG-H001",
        llm,
        env={"ANALYSIS_MAX_LLM_CALLS": str(max_calls), "ANALYSIS_MAX_PARALLEL_AGENTS": "4"},
    )
    assert len(llm.calls) <= max_calls
    assert state["trace_metrics"]["llm_calls"] <= max_calls
    assert state["risk_assessment"]["decision"] != "PASS"


@pytest.mark.asyncio
async def test_concurrent_budget_exhaustion_preserves_block(run_graph: RunGraph) -> None:
    llm = GatedLLM(static_llm_responses(), release_at=4, timeout=0.2)
    state = await run_graph(
        "CHG-E003",
        llm,
        pack_id="ecommerce-payments",
        env={"ANALYSIS_MAX_LLM_CALLS": "2", "ANALYSIS_MAX_PARALLEL_AGENTS": "4"},
    )
    assert len(llm.calls) <= 2
    assert state["degraded_to_review"] is True
    assert state["risk_assessment"]["decision"] == "BLOCK"
    assert state["risk_assessment"]["override_applied"] == "dependency-vuln-ceiling"


@pytest.mark.asyncio
async def test_exact_capacity_runs_every_branch(run_graph: RunGraph) -> None:
    """Control: a budget that fits the full plan is not refused under concurrency."""
    llm = GatedLLM(static_llm_responses(), release_at=4)
    state = await run_graph(
        "CHG-H001",
        llm,
        env={"ANALYSIS_MAX_LLM_CALLS": "6", "ANALYSIS_MAX_PARALLEL_AGENTS": "4"},
    )
    assert llm.peak == 4  # all four specialists were in flight together
    assert len(llm.calls) == 6  # triage + 4 specialists + verifier
    assert state["degraded_to_review"] is False
