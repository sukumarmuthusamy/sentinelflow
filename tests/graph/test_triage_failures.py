"""Triage Planner failures fail closed: degrade to REVIEW, never PASS, keep BLOCK."""

from __future__ import annotations

import pytest
from app.graph.state import AnalysisPlan
from app.mcp_client.errors import McpToolError
from app.model_gateway.errors import ProviderUnavailableError
from tests.graph.conftest import RunGraph, static_llm_responses
from tests.graph.fakes import FailingLLM, RaisingLLM


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        ProviderUnavailableError("triage model unavailable: APIConnectionError"),
        ConnectionError("connection reset"),
        TimeoutError("read timed out"),
        McpToolError(tool_name="get_change_package", message="simulated"),
    ],
    ids=["provider_unavailable", "connection", "timeout", "mcp_tool"],
)
async def test_recoverable_triage_failure_degrades_without_agent_spend(
    run_graph: RunGraph,
    error: Exception,
) -> None:
    llm = RaisingLLM(static_llm_responses(), schema=AnalysisPlan, error=error)
    state = await run_graph("CHG-H001", llm)
    assert llm.calls == ["AnalysisPlan!raised"]  # no specialist was started
    assert state["degraded_to_review"] is True
    assert state["risk_assessment"]["decision"] == "REVIEW"
    assert any(e.startswith("triage_planner failed:") for e in state["errors"])
    assert state["trace_metrics"]["llm_calls_failed"] == 1
    assert state["trace_metrics"]["llm_calls"] == 0


@pytest.mark.asyncio
async def test_triage_failure_preserves_deterministic_block(run_graph: RunGraph) -> None:
    llm = RaisingLLM(static_llm_responses(), schema=AnalysisPlan, error=TimeoutError("slow"))
    state = await run_graph("CHG-E003", llm, pack_id="ecommerce-payments")
    assert state["degraded_to_review"] is True
    assert state["risk_assessment"]["decision"] == "BLOCK"
    assert state["risk_assessment"]["override_applied"] == "dependency-vuln-ceiling"


@pytest.mark.asyncio
async def test_invalid_triage_plan_still_clamps_to_default(run_graph: RunGraph) -> None:
    """Unchanged: a schema-invalid plan clamps to the default plan and continues."""
    llm = FailingLLM(static_llm_responses(), fail_on={AnalysisPlan})
    state = await run_graph("CHG-H001", llm)
    assert any("clamped to default" in e for e in state["errors"])
    assert len(llm.calls) > 1  # specialists still ran
    assert state["trace_metrics"]["llm_calls_failed"] == 1


class QuotaError(Exception):
    """Stand-in for a provider 429 / daily-quota error (not an agent failure)."""


class ProgrammingError(Exception):
    """Stand-in for an unexpected bug inside the call path."""


class AuthError(Exception):
    status_code = 401


QUOTA = QuotaError("429 RESOURCE_EXHAUSTED quotaId GenerateRequestsPerDayPerProjectPerModel")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [
        QUOTA,
        ProgrammingError("unexpected"),
        KeyError("bug"),
    ],
    ids=["quota", "unexpected_exception", "key_error"],
)
async def test_operational_and_programming_errors_propagate(
    run_graph: RunGraph,
    error: Exception,
) -> None:
    llm = RaisingLLM(static_llm_responses(), schema=AnalysisPlan, error=error)
    # The MCP session's task group may wrap the error in an ExceptionGroup.
    with pytest.raises(BaseException) as info:
        await run_graph("CHG-H001", llm)
    assert _contains(info.value, type(error))


def _contains(exc: BaseException, kind: type[BaseException]) -> bool:
    if isinstance(exc, kind):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains(inner, kind) for inner in exc.exceptions)
    return False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "error",
    [QUOTA, AuthError("401 invalid api key")],
    ids=["daily_quota", "auth"],
)
async def test_quota_and_auth_errors_are_not_retried_by_the_graph(
    run_graph: RunGraph,
    error: Exception,
) -> None:
    """A node retry would be a second provider call against an exhausted quota."""
    llm = RaisingLLM(static_llm_responses(), schema=AnalysisPlan, error=error)
    with pytest.raises(BaseException) as info:
        await run_graph("CHG-H001", llm)
    assert _contains(info.value, type(error))
    assert llm.calls == ["AnalysisPlan!raised"]
