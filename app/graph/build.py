"""Construct the SentinelFlow LangGraph workflow."""

from __future__ import annotations

from datetime import timedelta

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from app.core.settings import Settings, get_settings
from app.graph.nodes import (
    budget_guard,
    check_remaining_steps,
    coverage_router,
    degrade_review_node,
    deterministic_signal_layer,
    evidence_verifier_node,
    focused_retrieval_repair,
    gate_router,
    human_approval_node,
    intake_validator,
    merge_findings,
    plan_fan_out,
    record_gate_decision_node,
    report_builder_node,
    risk_engine_node,
    run_parallel_agent,
    triage_planner_node,
)
from app.graph.typed_state import GraphState


def _approval_router(state: GraphState) -> str:
    if state.get("approval_status") == "approved":
        return "record_gate_decision"
    return "end"


def build_analysis_graph(
    settings: Settings | None = None,
) -> StateGraph[GraphState, None, GraphState, GraphState]:
    """Return an uncompiled StateGraph for the M4 analysis workflow."""
    cfg = settings or get_settings()
    graph = StateGraph(GraphState)

    retry = RetryPolicy(max_attempts=2, initial_interval=0.5)
    timeout = timedelta(seconds=cfg.analysis_node_timeout_seconds)

    graph.add_node("intake_validator", intake_validator, retry_policy=retry)
    graph.add_node("deterministic_signals", deterministic_signal_layer, retry_policy=retry)
    graph.add_node("budget_guard", budget_guard)
    graph.add_node("check_remaining_steps", check_remaining_steps)
    graph.add_node(
        "triage_planner",
        triage_planner_node,
        retry_policy=retry,
        timeout=timeout,
    )
    graph.add_node(
        "run_parallel_agent",
        run_parallel_agent,
        retry_policy=retry,
        timeout=timeout,
    )
    graph.add_node("merge_findings", merge_findings, defer=True)
    graph.add_node(
        "evidence_verifier",
        evidence_verifier_node,
        retry_policy=retry,
        timeout=timeout,
    )
    graph.add_node(
        "focused_retrieval_repair",
        focused_retrieval_repair,
        retry_policy=retry,
        timeout=timeout,
    )
    graph.add_node("risk_engine", risk_engine_node, retry_policy=retry)
    graph.add_node("degrade_review", degrade_review_node)
    graph.add_node("report_builder", report_builder_node, retry_policy=retry)
    graph.add_node("human_approval", human_approval_node)
    graph.add_node(
        "record_gate_decision",
        record_gate_decision_node,
        retry_policy=retry,
        timeout=timeout,
    )

    graph.add_edge(START, "intake_validator")
    graph.add_edge("intake_validator", "deterministic_signals")
    graph.add_edge("deterministic_signals", "budget_guard")
    graph.add_edge("budget_guard", "check_remaining_steps")
    # Once degraded, later LLM-calling nodes are skipped: degradation already means
    # "a human must look", so more agent output cannot change that outcome.
    graph.add_conditional_edges(
        "check_remaining_steps",
        gate_router,
        {"continue": "triage_planner", "degrade_review": "degrade_review"},
    )
    graph.add_conditional_edges(
        "triage_planner",
        plan_fan_out,
        ["run_parallel_agent", "degrade_review"],
    )
    graph.add_edge("run_parallel_agent", "merge_findings")
    graph.add_conditional_edges(
        "merge_findings",
        gate_router,
        {"continue": "evidence_verifier", "degrade_review": "degrade_review"},
    )
    graph.add_conditional_edges(
        "evidence_verifier",
        coverage_router,
        {
            "repair": "focused_retrieval_repair",
            "risk_engine": "risk_engine",
            "degrade_review": "degrade_review",
        },
    )
    graph.add_conditional_edges(
        "focused_retrieval_repair",
        gate_router,
        {"continue": "evidence_verifier", "degrade_review": "degrade_review"},
    )
    graph.add_edge("risk_engine", "report_builder")
    graph.add_edge("degrade_review", "report_builder")
    graph.add_edge("report_builder", "human_approval")
    graph.add_conditional_edges(
        "human_approval",
        _approval_router,
        {
            "record_gate_decision": "record_gate_decision",
            "end": END,
        },
    )
    graph.add_edge("record_gate_decision", END)

    return graph
