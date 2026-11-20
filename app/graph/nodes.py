"""LangGraph node implementations."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any, Literal

from langchain_core.runnables import RunnableConfig
from langgraph.types import Overwrite, Send, interrupt
from mcp_server.sentinelflow_context.models import RecordGateDecisionResponse

from app.agents.contracts import (
    ChangeImpact,
    EvidenceBundle,
    SecurityFindings,
    VerificationReport,
)
from app.agents.execute import (
    run_change_analyst,
    run_evidence_investigator,
    run_evidence_verifier,
    run_security_auditor,
    run_test_strategist,
    run_triage_planner,
)
from app.agents.queries import evidence_queries_from_signals, security_query_from_signals
from app.core.domain_pack import load_domain_pack
from app.deterministic.evaluate import build_signals_from_fixture, load_change_fixture_manifest
from app.deterministic.evaluate import resolve_fixture_directory as resolve_fixture
from app.deterministic.models import DeterministicSignals
from app.deterministic.policy_gate import apply_policy_gate
from app.deterministic.risk_engine import compute_risk_score
from app.graph.budget import BudgetExhaustedError
from app.graph.context import AnalysisGraphContext
from app.graph.plan_validation import (
    default_plan_for_pack,
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
from app.graph.verification import (
    MaterialAssertion,
    VerificationMetrics,
    compute_verification_metrics,
    material_assertions,
)
from app.mcp_client.port import McpClientPort
from app.model_gateway.llm import StructuredOutputError

# Degrade to REVIEW when this few supersteps remain (repair loop plus graph tail).
MIN_REMAINING_STEPS = 6


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


def _metrics(state: GraphState) -> VerificationMetrics | None:
    raw = state.get("verification_metrics")
    return VerificationMetrics.model_validate(raw) if raw else None


def _degraded(reason: str) -> dict[str, Any]:
    return {"degraded_to_review": True, "errors": [reason]}


def _trace_update(ctx: AnalysisGraphContext) -> dict[str, Any]:
    return {
        "trace_metrics": TraceMetrics(
            agent_steps=ctx.trace.agent_steps,
            llm_calls=ctx.trace.llm_calls,
            mcp_tool_calls=ctx.trace.mcp_tool_calls,
            retrieval_queries=ctx.trace.retrieval_queries,
            input_tokens=ctx.trace.input_tokens,
            output_tokens=ctx.trace.output_tokens,
            citations_rejected=ctx.trace.citations_rejected,
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
    try:
        proposed = await run_triage_planner(
            ctx.llm,
            budget=ctx.budget,
            trace=ctx.trace,
            model=ctx.triage_model,
            temperature=0.0,
            change_summary=summary,
            domain_pack_id=request.domain_pack_id,
        )
    except BudgetExhaustedError as exc:
        plan = default_plan_for_pack(ctx.domain_pack)
        return {"plan": plan.model_dump(), **_degraded(exc.reason), **_trace_update(ctx)}
    except StructuredOutputError as exc:
        # Plan section 2: an invalid plan clamps to the default; it is not retried.
        plan = default_plan_for_pack(ctx.domain_pack)
        return {
            "plan": plan.model_dump(),
            "errors": [f"triage plan invalid, clamped to default: {exc}"],
            **_trace_update(ctx),
        }
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
    status = "ok"
    with ctx.budget.parallel_slot():
        ctx.trace.agent_steps += 1
        try:
            updates.update(await _dispatch_agent(agent, ctx, request, signals))
        except BudgetExhaustedError as exc:
            status = "budget_exhausted"
            updates.update(_degraded(f"{agent}: {exc.reason}"))

    updates["parallel_completions"] = [completion_for_agent(agent, status=status)]
    return updates


async def _dispatch_agent(
    agent: str,
    ctx: AnalysisGraphContext,
    request: AnalysisRequest,
    signals: DeterministicSignals | None,
) -> dict[str, Any]:
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
        return {"change_impact": impact.model_dump()}
    if agent == "evidence_investigator":
        if ctx.retrieval is None:
            return {"errors": ["retrieval service unavailable"]}
        # Runs in parallel with the Change Analyst, so it cannot see hypotheses;
        # queries come from this change's deterministic signals instead.
        queries = evidence_queries_from_signals(signals, fallback=request.objective)
        bundle, outcome = await run_evidence_investigator(
            ctx.llm,
            ctx.retrieval,
            budget=ctx.budget,
            trace=ctx.trace,
            model=ctx.agent_model,
            temperature=ctx.agent_temperature,
            queries=queries,
        )
        return {
            "evidence": bundle.model_dump(),
            "retrieved_evidence_ids": sorted(outcome.allowed_ids),
            "retrieved_chunks": [chunk.model_dump() for chunk in outcome.chunks],
            "rejected_evidence_ids": outcome.rejected_ids,
        }
    if agent == "test_strategist":
        plan = await run_test_strategist(
            ctx.llm,
            ctx.mcp,
            budget=ctx.budget,
            trace=ctx.trace,
            model=ctx.agent_model,
            temperature=ctx.agent_temperature,
            request=request,
        )
        return {"regression_plan": plan.model_dump()}
    if agent == "security_auditor":
        findings, outcome = await run_security_auditor(
            ctx.llm,
            ctx.mcp,
            ctx.retrieval,
            budget=ctx.budget,
            trace=ctx.trace,
            model=ctx.agent_model,
            temperature=ctx.agent_temperature,
            request=request,
            security_query=security_query_from_signals(signals, fallback=request.objective),
        )
        # The verifier validates against retrieved_evidence_ids, so the auditor's
        # evidence must land there too or its valid citations get stripped.
        return {
            "security_findings": findings.model_dump(),
            "retrieved_evidence_ids": sorted(outcome.allowed_ids),
            "rejected_evidence_ids": outcome.rejected_ids,
        }
    return {"errors": [f"unknown parallel agent: {agent}"]}


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

    impact_raw = state.get("change_impact")
    security_raw = state.get("security_findings")
    assertions = material_assertions(
        ChangeImpact.model_validate(impact_raw) if impact_raw else None,
        SecurityFindings.model_validate(security_raw) if security_raw else None,
    )
    allowed = set(state.get("retrieved_evidence_ids") or [])
    specialist_payload = {
        "change_impact": impact_raw,
        "evidence": state.get("evidence"),
        "regression_plan": state.get("regression_plan"),
        "security_findings": security_raw,
    }
    report: VerificationReport
    try:
        report, rejected = await run_evidence_verifier(
            ctx.llm,
            budget=ctx.budget,
            trace=ctx.trace,
            model=ctx.agent_model,
            temperature=ctx.agent_temperature,
            allowed_ids=allowed,
            assertions=assertions,
            specialist_payload=specialist_payload,
        )
    except BudgetExhaustedError as exc:
        return {**_degraded(f"evidence_verifier: {exc.reason}"), **_trace_update(ctx)}

    metrics = compute_verification_metrics(assertions, report, signals=_signals(state))
    return {
        "verification": report.model_dump(),
        "verification_metrics": metrics.model_dump(),
        "evidence_gaps": list(metrics.unsupported_ids),
        "rejected_evidence_ids": rejected,
        **_trace_update(ctx),
    }


def coverage_router(
    state: GraphState,
) -> Literal["repair", "risk_engine", "degrade_review"]:
    if state.get("degraded_to_review"):
        return "degrade_review"
    remaining = state.get("remaining_steps")
    if isinstance(remaining, int) and remaining <= MIN_REMAINING_STEPS:
        return "degrade_review"
    metrics = _metrics(state)
    if metrics is None or metrics.evidence_coverage is None:
        return "risk_engine"
    floor = load_domain_pack(state["domain_pack_id"]).policy.evidence_floor
    repair_count = int(state.get("repair_count") or 0)
    if metrics.evidence_coverage < floor and repair_count < 1:
        return "repair"
    return "risk_engine"


def _repair_queries(
    state: GraphState,
    metrics: VerificationMetrics | None,
    request: AnalysisRequest,
) -> list[str]:
    """Query with the text of unsupported assertions, never their opaque IDs."""
    gaps = set(state.get("evidence_gaps") or [])
    texts: list[str] = []
    if metrics is not None:
        by_id: dict[str, MaterialAssertion] = {a.assertion_id: a for a in metrics.assertions}
        texts = [by_id[gap].text for gap in sorted(gaps) if gap in by_id]
    if texts:
        return texts[:5]
    return evidence_queries_from_signals(_signals(state), fallback=request.objective)


async def focused_retrieval_repair(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    request = _request(state)
    repair_count = int(state.get("repair_count") or 0) + 1
    if ctx.retrieval is None:
        return {
            "repair_count": repair_count,
            "evidence_gaps": Overwrite([]),
            "errors": ["retrieval unavailable during repair"],
        }

    bundle_raw = state.get("evidence")
    prior = EvidenceBundle.model_validate(bundle_raw) if bundle_raw else EvidenceBundle()
    queries = _repair_queries(state, _metrics(state), request)
    try:
        repaired, outcome = await run_evidence_investigator(
            ctx.llm,
            ctx.retrieval,
            budget=ctx.budget,
            trace=ctx.trace,
            model=ctx.agent_model,
            temperature=ctx.agent_temperature,
            queries=queries,
        )
    except BudgetExhaustedError as exc:
        return {
            "repair_count": repair_count,
            "evidence_gaps": Overwrite([]),
            **_degraded(f"repair: {exc.reason}"),
            **_trace_update(ctx),
        }
    # Repair exists to find NEW evidence: cite against what this repair retrieved,
    # then union with the earlier set (an intersection would discard new evidence).
    existing_ids = set(state.get("retrieved_evidence_ids") or [])
    merged = repaired.model_copy(
        update={
            "items": prior.items + repaired.items,
            "retrieval_queries": prior.retrieval_queries + repaired.retrieval_queries,
        },
    )
    prior_chunks = list(state.get("retrieved_chunks") or [])
    return {
        "evidence": merged.model_dump(),
        "retrieved_evidence_ids": sorted(existing_ids | outcome.allowed_ids),
        "retrieved_chunks": prior_chunks + [chunk.model_dump() for chunk in outcome.chunks],
        "rejected_evidence_ids": outcome.rejected_ids,
        "repair_count": repair_count,
        "evidence_gaps": Overwrite([]),
        **_trace_update(ctx),
    }


def risk_engine_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    signals = _signals(state)
    if signals is None:
        return {"errors": ["missing deterministic signals"]}

    metrics = _metrics(state)
    repair_count = int(state.get("repair_count") or 0)
    update: dict[str, Any] = {"repair_attempted": repair_count > 0}
    unsupported_rate = 0.0
    if metrics is not None:
        # Computed in code from per-assertion judgments, not the verifier's self-report.
        update["evidence_coverage"] = metrics.evidence_coverage
        update["unsupported_assertion_rate"] = metrics.unsupported_assertion_rate
        update["critical_claims_unsupported"] = metrics.critical_unsupported
        unsupported_rate = metrics.unsupported_assertion_rate or 0.0
    updated = signals.model_copy(update=update)
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


def _degraded_decision(computed: ReleaseDecision) -> ReleaseDecision:
    """Degradation means "a human must look", so it never weakens a deterministic BLOCK."""
    return ReleaseDecision.BLOCK if computed == ReleaseDecision.BLOCK else ReleaseDecision.REVIEW


def degrade_review_node(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    existing = state.get("risk_assessment")
    if existing:
        patched = {
            **existing,
            "decision": _degraded_decision(ReleaseDecision(existing["decision"])).value,
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
        decision=_degraded_decision(ReleaseDecision(gate.decision)),
        confidence=0.5,
        override_applied=gate.override_applied or "budget_or_recursion",
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
        if item.get("status") != "supported" or not item.get("evidence_ids"):
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
    metrics = _metrics(state)
    report = ReleaseReport(
        analysis_id=state["analysis_id"],
        decision=assessment.decision,
        risk_score=assessment.risk_score,
        confidence=assessment.confidence,
        impacted_services=list(impacted),
        critical_findings=findings,
        recommended_tests=tests,
        evidence_coverage=metrics.evidence_coverage if metrics is not None else None,
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
    if isinstance(remaining, int) and remaining <= MIN_REMAINING_STEPS:
        return {"degraded_to_review": True}
    return {}


def budget_guard(state: GraphState, config: RunnableConfig) -> dict[str, Any]:
    ctx = _ctx(config)
    try:
        ctx.budget.check_llm_call()
    except BudgetExhaustedError as exc:
        return _degraded(exc.reason)
    return {}
