"""Failures degrade to REVIEW, stop further LLM spend, and never weaken a BLOCK."""

from __future__ import annotations

from typing import Any

import pytest
from app.agents.contracts import ChangeImpact, SecurityFindings, VerificationReport
from app.graph.nodes import risk_engine_node
from app.model_gateway.llm import StaticStructuredLLM
from tests.graph.conftest import RunGraph, static_llm_responses
from tests.graph.fakes import ConcurrencyLLM, FailingLLM, FailingToolMcp


@pytest.mark.asyncio
@pytest.mark.parametrize(("change_id", "expected"), [("CHG-H001", "REVIEW"), ("CHG-H003", "BLOCK")])
async def test_specialist_structured_output_failure_degrades(
    run_graph: RunGraph,
    change_id: str,
    expected: str,
) -> None:
    llm = FailingLLM(static_llm_responses(), fail_on={ChangeImpact})
    state = await run_graph(change_id, llm)
    assert state["degraded_to_review"] is True
    assert state["risk_assessment"]["decision"] == expected
    status = {row["agent"]: row["status"] for row in state["parallel_completions"]}
    assert status["change_analyst"] == "failed"
    assert any("change_analyst failed: StructuredOutputError" in e for e in state["errors"])
    # Degraded at fan-in: the verifier is not called on partial output.
    assert "VerificationReport" not in llm.calls


@pytest.mark.asyncio
@pytest.mark.parametrize(("change_id", "expected"), [("CHG-H001", "REVIEW"), ("CHG-H003", "BLOCK")])
async def test_verifier_structured_output_failure_degrades(
    run_graph: RunGraph,
    change_id: str,
    expected: str,
) -> None:
    llm = FailingLLM(static_llm_responses(), fail_on={VerificationReport})
    state = await run_graph(change_id, llm)
    assert state["degraded_to_review"] is True
    assert state["risk_assessment"]["decision"] == expected
    assert any("evidence_verifier failed" in e for e in state["errors"])


@pytest.mark.asyncio
@pytest.mark.parametrize(("change_id", "expected"), [("CHG-H001", "REVIEW"), ("CHG-H003", "BLOCK")])
async def test_mcp_tool_failure_degrades(
    run_graph: RunGraph, change_id: str, expected: str
) -> None:
    llm = StaticStructuredLLM(static_llm_responses())
    state = await run_graph(
        change_id,
        llm,
        wrap_mcp=lambda mcp: FailingToolMcp(mcp, failing_tool="run_security_scan"),
    )
    assert state["degraded_to_review"] is True
    assert state["risk_assessment"]["decision"] == expected
    assert any("security_auditor failed: McpToolError" in e for e in state["errors"])
    assert "SecurityFindings" not in llm.calls


@pytest.mark.asyncio
async def test_budget_exhausted_before_triage_skips_all_agents(run_graph: RunGraph) -> None:
    llm = StaticStructuredLLM(static_llm_responses(), input_tokens=1_000, output_tokens=1)
    state = await run_graph("CHG-H001", llm, env={"ANALYSIS_MAX_INPUT_TOKENS": "1000"})
    assert state["degraded_to_review"] is True
    assert llm.calls == ["AnalysisPlan"]
    # No specialist branch ran (no MCP tools, no retrieval, no failed LLM calls).
    assert state["parallel_completions"] == []
    assert state["risk_assessment"]["decision"] == "REVIEW"


@pytest.mark.asyncio
async def test_budget_degradation_preserves_dependency_block(run_graph: RunGraph) -> None:
    state = await run_graph(
        "CHG-E003",
        StaticStructuredLLM(static_llm_responses()),
        pack_id="ecommerce-payments",
        env={"ANALYSIS_MAX_LLM_CALLS": "1"},
    )
    assert state["degraded_to_review"] is True
    assessment = state["risk_assessment"]
    assert assessment["decision"] == "BLOCK"
    assert assessment["override_applied"] == "dependency-vuln-ceiling"


@pytest.mark.asyncio
async def test_low_remaining_steps_short_circuits_before_any_llm_call(run_graph: RunGraph) -> None:
    llm = StaticStructuredLLM(static_llm_responses())
    state = await run_graph("CHG-H003", llm, env={"GRAPH_RECURSION_LIMIT": "10"})
    assert state["degraded_to_review"] is True
    assert llm.calls == []
    assert state["risk_assessment"]["decision"] == "BLOCK"


@pytest.mark.asyncio
async def test_parallel_limit_throttles_instead_of_crashing(run_graph: RunGraph) -> None:
    llm = ConcurrencyLLM(static_llm_responses())
    state = await run_graph("CHG-H001", llm, env={"ANALYSIS_MAX_PARALLEL_AGENTS": "1"})
    statuses = {row["agent"]: row["status"] for row in state["parallel_completions"]}
    assert len(statuses) == 4
    assert set(statuses.values()) == {"ok"}
    assert llm.peak == 1
    assert state["degraded_to_review"] is False


@pytest.mark.asyncio
async def test_default_path_completes_without_degradation(run_graph: RunGraph) -> None:
    """Control: with every dependency healthy, nothing degrades."""
    responses = static_llm_responses()
    responses[SecurityFindings] = SecurityFindings()
    state = await run_graph("CHG-H001", StaticStructuredLLM(responses))
    assert state["degraded_to_review"] is False
    assert state["risk_assessment"]["decision"] == "PASS"


def test_risk_engine_without_signals_still_decides() -> None:
    from app.graph.context import AnalysisGraphContext

    # risk_engine_node returns before touching the context's fields when signals are absent.
    context = object.__new__(AnalysisGraphContext)
    config: dict[str, Any] = {"configurable": {"analysis_context": context}}
    update = risk_engine_node({"deterministic_signals": None}, config)  # type: ignore[arg-type]
    assert update["risk_assessment"]["decision"] == "REVIEW"
    assert update["degraded_to_review"] is True
