"""LangGraph TypedDict state with reducers (M4)."""

from __future__ import annotations

import operator
from typing import Annotated, Any, NotRequired, TypedDict

from langgraph.managed import RemainingSteps
from langgraph.types import Overwrite

from app.graph.plan_validation import AGENT_SORT_KEYS


class ParallelCompletion(TypedDict):
    agent: str
    sort_key: int
    status: str


def merge_parallel_completions(
    existing: list[ParallelCompletion],
    incoming: list[ParallelCompletion],
) -> list[ParallelCompletion]:
    """Merge fan-out branch completions and sort by explicit sort key."""
    by_agent: dict[str, ParallelCompletion] = {item["agent"]: item for item in existing}
    for item in incoming:
        by_agent[item["agent"]] = item
    return sorted(by_agent.values(), key=lambda row: row["sort_key"])


def merge_unique_strings(existing: list[str], incoming: list[str]) -> list[str]:
    merged = list(existing)
    for value in incoming:
        if value not in merged:
            merged.append(value)
    return merged


class GraphState(TypedDict):
    """Shared workflow state for LangGraph."""

    analysis_id: str
    request: dict[str, Any]
    domain_pack_id: str
    deterministic_signals: dict[str, Any] | None
    # Changed file paths only; manifest labels (category, expected decision) never enter state.
    changed_files: list[str]
    plan: dict[str, Any] | None
    change_impact: dict[str, Any] | None
    evidence: dict[str, Any] | None
    regression_plan: dict[str, Any] | None
    security_findings: dict[str, Any] | None
    verification: dict[str, Any] | None
    risk_assessment: dict[str, Any] | None
    retrieved_evidence_ids: Annotated[list[str], merge_unique_strings]
    retrieved_chunks: dict[str, Any] | None
    evidence_gaps: Annotated[list[str], merge_unique_strings]
    repair_count: int
    approval_status: str
    trace_metrics: dict[str, Any]
    final_report: dict[str, Any] | None
    errors: Annotated[list[str], operator.add]
    parallel_completions: Annotated[list[ParallelCompletion], merge_parallel_completions]
    # Citations an agent emitted that were absent from its retrieved set (pre-filter).
    rejected_evidence_ids: Annotated[list[str], operator.add]
    # Deterministic coverage/unsupported metrics (app/graph/verification.py).
    verification_metrics: dict[str, Any] | None
    # OR-reduced: any parallel branch may flip it in the same superstep.
    degraded_to_review: Annotated[bool, operator.or_]
    record_decision_written: bool
    agent_name: NotRequired[str]
    sort_key: NotRequired[int]
    remaining_steps: RemainingSteps


def completion_for_agent(agent: str, *, status: str = "ok") -> ParallelCompletion:
    name = agent if agent in AGENT_SORT_KEYS else None
    sort_key = AGENT_SORT_KEYS[name] if name is not None else 99
    return ParallelCompletion(agent=agent, sort_key=sort_key, status=status)


__all__ = ["GraphState", "Overwrite", "completion_for_agent"]
