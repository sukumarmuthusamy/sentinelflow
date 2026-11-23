"""Public projections of graph state, and sanitized failure classification.

Graph state holds retrieved chunk text, raw tool payloads and free-form error strings
that can embed provider messages. None of it is returned as-is. The API sees:

* an ``AnalysisResult``, built field by field from the deterministic risk assessment,
  the cited report, the plan and the trace counters;
* ``DegradationReason`` codes instead of the graph's error strings, so a provider
  outage that degraded an agent, a budget stop and a deterministic data gap stay
  distinguishable without echoing exception text;
* for a failed job, a ``JobError`` with a fixed message per code.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from app.core.errors import ErrorCode, SentinelFlowError
from app.graph.state import ApprovalStatus, ReleaseReport, RiskAssessment, TraceMetrics
from app.jobs.models import JobError
from app.mcp_client.errors import ChangeSourceError, McpProtocolError, McpToolError
from app.model_gateway.errors import (
    ProviderUnavailableError,
    is_auth_error,
    is_daily_quota_exhausted,
    is_provider_unavailable_error,
    is_quota_or_rate_limit_error,
)

DegradationCode = Literal[
    "incomplete_change_data",
    "budget_exhausted",
    "provider_unavailable",
    "structured_output_invalid",
    "mcp_error",
    "retrieval_unavailable",
    "timeout",
    "component_failed",
    "triage_plan_clamped",
    "missing_deterministic_signals",
    "gate_decision_write_failed",
    "other",
]


class DegradationReason(BaseModel):
    """Why a run took the conservative path, without the underlying exception text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: DegradationCode
    component: str | None = None
    # Only for ``incomplete_change_data``: the gap text, which the deterministic layer
    # writes itself (scanner/tool names and repository paths), truncated.
    detail: str | None = None


class PlanSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    change_category: str
    agents: list[str]
    clamped_to_default: bool = False


class ApprovalSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    required: bool
    status: ApprovalStatus
    gate_decision_recorded: bool


class AnalysisResult(BaseModel):
    """The decision and report for one analysis, as stored on the job and returned."""

    model_config = ConfigDict(extra="forbid")

    analysis_id: str
    risk_assessment: RiskAssessment
    report: ReleaseReport
    plan: PlanSummary | None = None
    degraded: bool
    degradation: list[DegradationReason] = Field(default_factory=list)
    approval: ApprovalSummary
    trace: TraceMetrics


_BUDGET = re.compile(r"max_(llm_calls|input_tokens|output_tokens) exceeded")
_COMPONENT_FAILED = re.compile(r"^(?P<component>[a-z_]+) failed: (?P<exc>[A-Za-z_][A-Za-z0-9_]*):")
_COMPONENT = re.compile(r"^(?P<component>[a-z_]+):")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_EXCEPTION_CODES: dict[str, DegradationCode] = {
    "ProviderUnavailableError": "provider_unavailable",
    "StructuredOutputError": "structured_output_invalid",
    "McpToolError": "mcp_error",
    "McpProtocolError": "mcp_error",
    "TimeoutError": "timeout",
    "ConnectionError": "provider_unavailable",
}


def classify_degradation(error: str) -> DegradationReason:
    """Map one graph error string to a code; never return its exception message."""
    text = str(error)
    if text.startswith("incomplete_change_data:"):
        detail = _CONTROL.sub(" ", text.partition(":")[2]).strip()[:200]
        return DegradationReason(code="incomplete_change_data", detail=detail or None)
    if text.startswith("triage plan invalid"):
        return DegradationReason(code="triage_plan_clamped", component="triage_planner")
    if text == "record_gate_decision failed":
        return DegradationReason(code="gate_decision_write_failed", component="record_gate")
    if text.startswith("risk_engine: missing deterministic signals"):
        return DegradationReason(code="missing_deterministic_signals", component="risk_engine")
    failed = _COMPONENT_FAILED.match(text)
    if failed is not None:
        code = _EXCEPTION_CODES.get(failed["exc"], "component_failed")
        return DegradationReason(code=code, component=failed["component"])
    component_match = _COMPONENT.match(text)
    component = component_match["component"] if component_match else None
    if _BUDGET.search(text):
        return DegradationReason(code="budget_exhausted", component=component)
    if "retrieval" in text and "unavailable" in text:
        return DegradationReason(code="retrieval_unavailable", component=component)
    return DegradationReason(code="other", component=component)


def _degradation(state: dict[str, Any]) -> list[DegradationReason]:
    reasons: list[DegradationReason] = []
    for error in state.get("errors") or []:
        reason = classify_degradation(str(error))
        if reason not in reasons:
            reasons.append(reason)
    return reasons


def build_analysis_result(state: dict[str, Any], *, analysis_id: str) -> AnalysisResult:
    """Project final (or interrupted) graph state into the public result."""
    assessment = RiskAssessment.model_validate(state["risk_assessment"])
    report = ReleaseReport.model_validate(state["final_report"])
    raw_plan = state.get("plan")
    plan = (
        PlanSummary(
            change_category=str(raw_plan.get("change_category")),
            agents=[str(agent) for agent in raw_plan.get("agents") or []],
            clamped_to_default=bool(raw_plan.get("clamped_to_default")),
        )
        if raw_plan
        else None
    )
    request = state.get("request") or {}
    required = bool(request.get("record_decision"))
    status_value = state.get("approval_status") or ApprovalStatus.NOT_REQUESTED.value
    status = ApprovalStatus(status_value)
    if required and state.get("__interrupt__"):
        status = ApprovalStatus.PENDING
    return AnalysisResult(
        analysis_id=analysis_id,
        risk_assessment=assessment,
        report=report,
        plan=plan,
        degraded=bool(state.get("degraded_to_review")),
        degradation=_degradation(state),
        approval=ApprovalSummary(
            required=required,
            status=status,
            gate_decision_recorded=bool(state.get("record_decision_written")),
        ),
        trace=TraceMetrics.model_validate(state.get("trace_metrics") or {}),
    )


_FAILURE_MESSAGES: dict[ErrorCode, str] = {
    ErrorCode.PROVIDER_RATE_LIMITED: (
        "The model provider refused the request for quota or rate limits; "
        "the analysis was stopped instead of retrying."
    ),
    ErrorCode.PROVIDER_AUTH_FAILED: "The model provider rejected the configured credentials.",
    ErrorCode.PROVIDER_UNAVAILABLE: "The model provider could not be reached.",
    ErrorCode.MCP_ERROR: "A context tool call failed.",
    ErrorCode.CHANGE_SOURCE_UNAVAILABLE: "The change source could not supply this change.",
    ErrorCode.DOMAIN_PACK_INVALID: "The domain pack failed validation.",
    ErrorCode.DOMAIN_PACK_NOT_FOUND: "The domain pack was not found.",
    ErrorCode.JOB_EXECUTION_FAILED: "The job failed during execution.",
    ErrorCode.JOB_INTERRUPTED: (
        "The API process stopped while this job was running. It was not re-run "
        "automatically, so no provider calls were repeated."
    ),
    ErrorCode.DECISION_INVARIANT_VIOLATED: (
        "The resumed run reported a different decision than the one shown for approval."
    ),
    ErrorCode.GRAPH_INCOMPLETE: (
        "The run ended without reaching the end of the graph or an approval interrupt."
    ),
    ErrorCode.GATE_DECISION_NOT_RECORDED: (
        "The approved gate decision was not confirmed as recorded in the audit log. "
        "The decision is unchanged; the write was not retried."
    ),
}


def job_error(code: ErrorCode) -> JobError:
    return JobError(code=code, message=_FAILURE_MESSAGES.get(code, "The job failed."))


def root_exception(exc: BaseException) -> BaseException:
    """The underlying error inside exception groups.

    A run executes inside the MCP client's ``async with``, whose anyio task group
    wraps anything that escapes it in an ``ExceptionGroup``. Unwrapped, every
    provider quota or auth error would classify as a generic execution failure.
    With several leaves, the first is used.
    """
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def classify_failure(exc: BaseException) -> JobError:
    """Sanitized failure for an exception that escaped graph execution.

    MCP and change-source types are checked first: their ``code`` attributes are
    JSON-RPC codes, not HTTP statuses, and must not be read as provider auth errors.
    """
    exc = root_exception(exc)
    if isinstance(exc, ChangeSourceError):
        return job_error(ErrorCode.CHANGE_SOURCE_UNAVAILABLE)
    if isinstance(exc, McpToolError | McpProtocolError):
        return job_error(ErrorCode.MCP_ERROR)
    if isinstance(exc, SentinelFlowError) and exc.code in {
        ErrorCode.DOMAIN_PACK_INVALID,
        ErrorCode.DOMAIN_PACK_NOT_FOUND,
    }:
        return job_error(exc.code)
    if is_daily_quota_exhausted(exc) or is_quota_or_rate_limit_error(exc):
        return job_error(ErrorCode.PROVIDER_RATE_LIMITED)
    if is_auth_error(exc):
        return job_error(ErrorCode.PROVIDER_AUTH_FAILED)
    if isinstance(exc, ProviderUnavailableError) or is_provider_unavailable_error(exc):
        return job_error(ErrorCode.PROVIDER_UNAVAILABLE)
    return job_error(ErrorCode.JOB_EXECUTION_FAILED)
