"""LangGraph node implementations."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.types import Overwrite, Send, interrupt
from mcp_server.sentinelflow_context.models import RecordGateDecisionResponse

from app.agents.contracts import ChangeImpact, EvidenceBundle
from app.agents.execute import (
    run_change_analyst,
    run_evidence_investigator,
    run_evidence_verifier,
    run_security_auditor,
    run_test_strategist,
    run_triage_planner,
)
from app.core.domain_pack import load_domain_pack
from app.deterministic.evaluate import build_signals_from_fixture, load_change_fixture_manifest
from app.deterministic.evaluate import resolve_fixture_directory as resolve_fixture
from app.deterministic.models import DeterministicSignals
from app.deterministic.policy_gate import apply_policy_gate
from app.deterministic.risk_engine import compute_risk_score
from app.graph.budget import BudgetExhaustedError
from app.graph.context import AnalysisGraphContext
from app.graph.plan_validation import (
    parallel_agents_from_plan,
    validate_analysis_plan,
    verifier_scheduled,
)
from app.graph.state import (
    AnalysisPlan,
    AnalysisRequest,
    ApprovalStatus,
    CriticalFinding,
    ReleaseDecision,
    ReleaseReport,
    RiskAssessment,
    TraceMetrics,
)
from app.graph.typed_state import GraphState, completion_for_agent
from app.mcp_client.port import McpClientPort


def _ctx(config: RunnableConfig) -> AnalysisGraphContext:
    configurable = config.get("configurable") or {}
    context = configurable.get("analysis_context")
    if not isinstance(context, AnalysisGraphContext):
        msg = "analysis_context missing from RunnableConfig.configurable"
        raise RuntimeError(msg)
    return context


def _request(state: GraphState) -> AnalysisRequest:
    return AnalysisRequest.model_validate(state["request"])


def _signals(state: GraphState) -> DeterministicSignals | None:
    raw = state.get("deterministic_signals")
    if raw is None:
        return None
    return DeterministicSignals.model_validate(raw)


def _trace_update(ctx: AnalysisGraphContext) -> dict[str, Any]:
    return {
        "trace_metrics": TraceMetrics(
            agent_steps=ctx.trace.agent_steps,
            llm_calls=ctx.trace.llm_calls,
            mcp_tool_calls=ctx.trace.mcp_tool_calls,
            retrieval_queries=ctx.trace.retrieval_queries,
            input_tokens=ctx.trace.input_tokens,
            output_tokens=ctx.trace.output_tokens,
        ).model_dump(),
    }


def intake_validator(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    request = _request(state)
    if request.domain_pack_id != ctx.domain_pack.pack_id:
        ctx.domain_pack = load_domain_pack(request.domain_pack_id)
    analysis_id = state.get("analysis_id") or str(uuid.uuid4())
    return {
        "analysis_id": analysis_id,
        "domain_pack_id": request.domain_pack_id,
        "approval_status": ApprovalStatus.NOT_REQUESTED.value,
        "repair_count": state.get("repair_count", 0),
        "record_decision_written": False,
        "degraded_to_review": state.get("degraded_to_review", False),
        "parallel_completions": [],
        "retrieved_evidence_ids": [],
    }


def deterministic_signal_layer(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    request = _request(state)
    fixture_dir = resolve_fixture(
        request.change_id,
        pack_id=request.domain_pack_id,
        domains_root=ctx.domain_pack.root.parent,
    )
    manifest = load_change_fixture_manifest(fixture_dir)
    signals = build_signals_from_fixture(
        fixture_dir,
        manifest,
        ctx.domain_pack,
        fixture_only=True,
    )
    return {"deterministic_signals": signals.model_dump()}


async def triage_planner_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    request = _request(state)
    signals = _signals(state)
    summary = ""
    if signals is not None:
        summary = (
            f"change_id={signals.change_id} primary_service={signals.primary_service} "
            f"impacted={signals.impacted_services}"
        )
    proposed = await run_triage_planner(
        ctx.llm,
        budget=ctx.budget,
        trace=ctx.trace,
        model=ctx.triage_model,
        temperature=0.0,
        change_summary=summary,
        domain_pack_id=request.domain_pack_id,
    )
    plan = validate_analysis_plan(proposed, ctx.domain_pack)
    return {"plan": plan.model_dump(), **_trace_update(ctx)}


def plan_fan_out(state: GraphState) -> list[Send]:
    plan = AnalysisPlan.model_validate(state["plan"])
    sends: list[Send] = []
    for agent in parallel_agents_from_plan(plan):
        sends.append(
            Send(
                "run_parallel_agent",
                {
                    **state,
                    "agent_name": agent,
                    "sort_key": completion_for_agent(agent)["sort_key"],
                },
            ),
        )
    return sends


async def run_parallel_agent(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    agent = state.get("agent_name")
    if not agent:
        return {"errors": ["parallel agent missing agent_name"]}

    request = _request(state)
    signals = _signals(state)
    updates: dict[str, Any] = {}

    with ctx.budget.parallel_slot():
        ctx.trace.agent_steps += 1
        if agent == "change_analyst":
            impact = await run_change_analyst(
                ctx.llm,
                ctx.mcp,
                budget=ctx.budget,
                trace=ctx.trace,
                model=ctx.agent_model,
                temperature=ctx.agent_temperature,
                request=request,
                signals=signals.model_dump() if signals else {},
            )
            updates["change_impact"] = impact.model_dump()
        elif agent == "evidence_investigator":
            if ctx.retrieval is None:
                updates["errors"] = ["retrieval service unavailable"]
            else:
                hypotheses: list[str] = []
                if state.get("change_impact"):
                    impact = ChangeImpact.model_validate(state["change_impact"])
                    hypotheses = [hyp.statement for hyp in impact.risk_hypotheses]
                bundle, chunks, id_set = await run_evidence_investigator(
                    ctx.llm,
                    ctx.retrieval,
                    budget=ctx.budget,
                    trace=ctx.trace,
                    model=ctx.agent_model,
                    temperature=ctx.agent_temperature,
                    request=request,
                    hypotheses=hypotheses,
                )
                updates["evidence"] = bundle.model_dump()
                updates["retrieved_evidence_ids"] = sorted(id_set)
                updates["retrieved_chunks"] = [chunk.model_dump() for chunk in chunks]
        elif agent == "test_strategist":
            plan = await run_test_strategist(
                ctx.llm,
                ctx.mcp,
                budget=ctx.budget,
                trace=ctx.trace,
                model=ctx.agent_model,
                temperature=ctx.agent_temperature,
                request=request,
            )
            updates["regression_plan"] = plan.model_dump()
        elif agent == "security_auditor":
            sec = await run_security_auditor(
                ctx.llm,
                ctx.mcp,
                ctx.retrieval,
                budget=ctx.budget,
                trace=ctx.trace,
                model=ctx.agent_model,
                temperature=ctx.agent_temperature,
                request=request,
            )
            updates["security_findings"] = sec.model_dump()
        else:
            updates["errors"] = [f"unknown parallel agent: {agent}"]

    updates["parallel_completions"] = [completion_for_agent(agent)]
    return updates


async def merge_findings(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    completions = sorted(
        state.get("parallel_completions") or [],
        key=lambda row: row["sort_key"],
    )
    return {"parallel_completions": completions, **_trace_update(ctx)}


async def evidence_verifier_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    plan = AnalysisPlan.model_validate(state["plan"])
    if not verifier_scheduled(plan):
        return {}

    allowed = set(state.get("retrieved_evidence_ids") or [])
    specialist_payload = {
        "change_impact": state.get("change_impact"),
        "evidence": state.get("evidence"),
        "regression_plan": state.get("regression_plan"),
        "security_findings": state.get("security_findings"),
    }
    report = await run_evidence_verifier(
        ctx.llm,
        budget=ctx.budget,
        trace=ctx.trace,
        model=ctx.agent_model,
        temperature=ctx.agent_temperature,
        allowed_ids=allowed,
        specialist_payload=specialist_payload,
    )
    gaps = [
        item.assertion_id
        for item in report.assertion_assessments
        if item.status in {"unsupported", "contradicted"}
    ]
    return {
        "verification": report.model_dump(),
        "evidence_gaps": gaps,
        **_trace_update(ctx),
    }


def coverage_router(
    state: GraphState,
) -> Literal["repair", "risk_engine", "degrade_review"]:
    if state.get("degraded_to_review"):
        return "degrade_review"
    domain = load_domain_pack(state["domain_pack_id"])
    floor = domain.policy.evidence_floor
    verification = state.get("verification") or {}
    coverage = float(verification.get("evidence_coverage") or 0.0)
    repair_count = int(state.get("repair_count") or 0)
    if coverage < floor and repair_count < 1:
        return "repair"
    return "risk_engine"


async def focused_retrieval_repair(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    request = _request(state)
    gaps = list(state.get("evidence_gaps") or [])
    verification = state.get("verification") or {}
    if verification.get("repair_recommended"):
        gaps.append("verification_repair")
    if ctx.retrieval is None:
        return {
            "repair_count": int(state.get("repair_count") or 0) + 1,
            "evidence_gaps": Overwrite([]),
            "errors": ["retrieval unavailable during repair"],
        }

    bundle_raw = state.get("evidence")
    prior = EvidenceBundle.model_validate(bundle_raw) if bundle_raw else EvidenceBundle()
    queries = gaps or prior.unsupported_hypothesis_ids or [request.objective]
    existing_ids = set(state.get("retrieved_evidence_ids") or [])
    repaired, chunks, id_set = await run_evidence_investigator(
        ctx.llm,
        ctx.retrieval,
        budget=ctx.budget,
        trace=ctx.trace,
        model=ctx.agent_model,
        temperature=ctx.agent_temperature,
        request=request,
        hypotheses=queries,
        allowed_ids=existing_ids,
    )
    id_set = existing_ids | id_set
    merged_items = prior.items + repaired.items
    merged = repaired.model_copy(update={"items": merged_items, "coverage": repaired.coverage})
    return {
        "evidence": merged.model_dump(),
        "retrieved_evidence_ids": sorted(id_set),
        "retrieved_chunks": [chunk.model_dump() for chunk in chunks],
        "repair_count": int(state.get("repair_count") or 0) + 1,
        "evidence_gaps": Overwrite([]),
        **_trace_update(ctx),
    }


def risk_engine_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    signals = _signals(state)
    if signals is None:
        return {"errors": ["missing deterministic signals"]}

    verification = state.get("verification") or {}
    unsupported_rate = float(verification.get("unsupported_assertion_rate") or 0.0)
    coverage = verification.get("evidence_coverage")
    repair_count = int(state.get("repair_count") or 0)
    critical_unsupported = unsupported_rate >= 0.5
    updated = signals.model_copy(
        update={
            "evidence_coverage": (
                float(coverage) if coverage is not None else signals.evidence_coverage
            ),
            "critical_claims_unsupported": critical_unsupported,
            "repair_attempted": repair_count > 0,
        },
    )
    risk = compute_risk_score(updated, ctx.domain_pack)
    gate = apply_policy_gate(updated, ctx.domain_pack, risk)
    assessment = RiskAssessment(
        risk_score=gate.risk_score,
        decision=ReleaseDecision(gate.decision),
        confidence=max(0.0, 1.0 - unsupported_rate),
        override_applied=gate.override_applied,
        component_scores=risk.weighted_components,
    )
    return {
        "deterministic_signals": updated.model_dump(),
        "risk_assessment": assessment.model_dump(),
    }


def degrade_review_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    existing = state.get("risk_assessment")
    if existing:
        patched = {
            **existing,
            "decision": ReleaseDecision.REVIEW.value,
            "override_applied": existing.get("override_applied") or "budget_or_recursion",
        }
        return {"risk_assessment": patched, "degraded_to_review": True}

    signals = _signals(state)
    if signals is None:
        assessment = RiskAssessment(
            risk_score=100,
            decision=ReleaseDecision.REVIEW,
            confidence=0.0,
            override_applied="budget_or_recursion",
        )
        return {"risk_assessment": assessment.model_dump(), "degraded_to_review": True}

    risk = compute_risk_score(signals, ctx.domain_pack)
    gate = apply_policy_gate(signals, ctx.domain_pack, risk)
    assessment = RiskAssessment(
        risk_score=gate.risk_score,
        decision=ReleaseDecision.REVIEW,
        confidence=0.5,
        override_applied="budget_or_recursion",
        component_scores=risk.weighted_components,
    )
    return {"risk_assessment": assessment.model_dump(), "degraded_to_review": True}


def report_builder_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    _ = config
    request = _request(state)
    assessment = RiskAssessment.model_validate(state["risk_assessment"])
    verification = state.get("verification") or {}
    findings: list[CriticalFinding] = []
    for item in verification.get("assertion_assessments") or []:
        if item.get("status") != "supported":
            continue
        findings.append(
            CriticalFinding(
                finding=str(item.get("assertion_text", "")),
                evidence_ids=list(item.get("evidence_ids") or []),
            ),
        )
    tests: list[str] = []
    regression = state.get("regression_plan")
    if regression:
        for test in regression.get("recommended_tests") or []:
            tests.append(str(test.get("test_id", test.get("name", ""))))

    signals = _signals(state)
    impacted = signals.impacted_services if signals else []
    report = ReleaseReport(
        analysis_id=state["analysis_id"],
        decision=assessment.decision,
        risk_score=assessment.risk_score,
        confidence=assessment.confidence,
        impacted_services=list(impacted),
        critical_findings=findings,
        recommended_tests=tests,
        evidence_coverage=float(verification.get("evidence_coverage") or 0.0),
        requires_human_approval=request.record_decision,
    )
    return {"final_report": report.model_dump()}


def human_approval_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    _ = config
    request = _request(state)
    if not request.record_decision:
        return {"approval_status": ApprovalStatus.NOT_REQUESTED.value}
    report = state.get("final_report") or {}
    payload = interrupt(
        {
            "action": "approve_record_gate_decision",
            "analysis_id": state["analysis_id"],
            "decision": report.get("decision"),
        },
    )
    approved = bool(payload.get("approved")) if isinstance(payload, dict) else bool(payload)
    status = ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED
    return {"approval_status": status.value}


async def record_gate_decision_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    if state.get("record_decision_written"):
        return {}
    if state.get("approval_status") != ApprovalStatus.APPROVED.value:
        return {}
    assessment = RiskAssessment.model_validate(state["risk_assessment"])
    mcp: McpClientPort = ctx.mcp
    idempotency_key = f"{state['analysis_id']}-gate"
    evidence_material = json.dumps(
        sorted(state.get("retrieved_evidence_ids") or []),
        sort_keys=True,
    )
    evidence_hash = hashlib.sha256(evidence_material.encode("utf-8")).hexdigest()
    result = await mcp.call_tool_typed(
        "record_gate_decision",
        {
            "analysis_id": state["analysis_id"],
            "decision": assessment.decision.value,
            "evidence_hash": evidence_hash,
            "idempotency_key": idempotency_key,
        },
        response_model=RecordGateDecisionResponse,
    )
    ctx.trace.mcp_tool_calls += 1
    if not result.recorded and not result.duplicate:
        return {"errors": ["record_gate_decision failed"]}
    return {"record_decision_written": True, **_trace_update(ctx)}


def check_remaining_steps(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    _ = config
    remaining = state.get("remaining_steps", 999)
    if isinstance(remaining, int) and remaining <= 2:
        return {"degraded_to_review": True}
    return {}


def budget_guard(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    try:
        ctx.budget.check_llm_call()
    except BudgetExhaustedError as exc:
        return {"degraded_to_review": True, "errors": [exc.reason]}
    return {}
