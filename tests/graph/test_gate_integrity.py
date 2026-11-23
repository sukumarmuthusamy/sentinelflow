"""End-to-end graph checks that LLM output cannot weaken or self-score the gate."""

from __future__ import annotations

import pytest
from app.agents.contracts import ChangeImpact, RiskHypothesis, VerificationReport
from app.model_gateway.llm import StaticStructuredLLM
from tests.graph.conftest import RunGraph, static_llm_responses


@pytest.mark.asyncio
async def test_budget_exhaustion_degrades_to_review(run_graph: RunGraph) -> None:
    state = await run_graph(
        "CHG-H001",
        StaticStructuredLLM(static_llm_responses()),
        env={"ANALYSIS_MAX_LLM_CALLS": "1"},
    )
    assert state["degraded_to_review"] is True
    assert state["risk_assessment"]["decision"] == "REVIEW"
    assert any("max_llm_calls" in error for error in state["errors"])


@pytest.mark.asyncio
async def test_degradation_never_weakens_deterministic_block(run_graph: RunGraph) -> None:
    state = await run_graph(
        "CHG-H003",
        StaticStructuredLLM(static_llm_responses()),
        env={"ANALYSIS_MAX_LLM_CALLS": "1"},
    )
    assert state["degraded_to_review"] is True
    assert state["risk_assessment"]["decision"] == "BLOCK"


@pytest.mark.asyncio
async def test_token_budget_degrades(run_graph: RunGraph) -> None:
    llm = StaticStructuredLLM(static_llm_responses(), input_tokens=1_000, output_tokens=1)
    state = await run_graph(
        "CHG-H001",
        llm,
        env={"ANALYSIS_MAX_INPUT_TOKENS": "1000"},
    )
    assert state["degraded_to_review"] is True
    assert llm.calls == ["AnalysisPlan"]


@pytest.mark.asyncio
async def test_token_usage_is_accounted(run_graph: RunGraph) -> None:
    llm = StaticStructuredLLM(static_llm_responses(), input_tokens=100, output_tokens=20)
    state = await run_graph("CHG-H001", llm)
    trace = state["trace_metrics"]
    assert trace["llm_calls"] == len(llm.calls) >= 5
    assert trace["input_tokens"] == 100 * trace["llm_calls"]
    assert trace["output_tokens"] == 20 * trace["llm_calls"]


@pytest.mark.asyncio
async def test_verifier_self_reported_coverage_is_ignored(run_graph: RunGraph) -> None:
    responses = static_llm_responses()
    responses[ChangeImpact] = ChangeImpact(
        change_id="CHG-H001",
        risk_hypotheses=[
            RiskHypothesis(id="H1", statement="docs edit hides a code path", severity="high"),
        ],
    )
    # The verifier claims full coverage but never assesses H1.
    responses[VerificationReport] = VerificationReport(
        evidence_coverage=1.0,
        unsupported_assertion_rate=0.0,
    )
    state = await run_graph("CHG-H001", StaticStructuredLLM(responses))

    metrics = state["verification_metrics"]
    assert metrics["llm_reported_evidence_coverage"] == 1.0
    assert metrics["evidence_coverage"] == 0.0
    assert metrics["unsupported_ids"] == ["hyp:H1"]
    assert metrics["critical_unsupported"] is True
    assert state["repair_count"] == 1
    assert state["risk_assessment"]["decision"] == "REVIEW"
    assert state["final_report"]["evidence_coverage"] == 0.0


@pytest.mark.asyncio
async def test_clean_doc_change_passes_with_nothing_to_verify(run_graph: RunGraph) -> None:
    state = await run_graph("CHG-H001", StaticStructuredLLM(static_llm_responses()))
    assert state["risk_assessment"]["decision"] == "PASS"
    assert state["verification_metrics"]["evidence_coverage"] is None
    assert state["rejected_evidence_ids"] == []


@pytest.mark.asyncio
async def test_missing_retrieval_never_passes_silently(run_graph: RunGraph) -> None:
    llm = StaticStructuredLLM(static_llm_responses())
    state = await run_graph("CHG-H001", llm, retriever=None)
    assert state["degraded_to_review"] is True
    assert state["risk_assessment"]["decision"] == "REVIEW"
    assert any("retrieval service unavailable" in error for error in state["errors"])
    # Degraded after fan-out: the verifier is skipped, not run on missing evidence.
    assert "VerificationReport" not in llm.calls
