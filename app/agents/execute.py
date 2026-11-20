"""Run specialist agents with versioned prompts and typed outputs."""

from __future__ import annotations

import json
from typing import Any

from app.agents.contracts import (
    ChangeImpact,
    EvidenceBundle,
    RegressionPlan,
    SecurityFindings,
    VerificationReport,
)
from app.agents.mcp_tools import (
    context_json,
    mcp_change_analyst_context,
    mcp_security_auditor_context,
    mcp_test_strategist_context,
)
from app.agents.prompts import PROMPT_VERSION
from app.agents.prompts import v1 as prompts
from app.graph.budget import AnalysisBudget, TraceAccumulator
from app.graph.evidence_ids import (
    collect_retrieved_evidence_ids,
    evidence_id_from_metadata,
    filter_security_findings,
    validate_bundle_citations,
)
from app.graph.state import AnalysisPlan, AnalysisRequest
from app.mcp_client.port import McpClientPort
from app.model_gateway.llm import StructuredLLM
from app.rag.models import DocumentType, RetrievedChunk
from app.rag.retrieval import RetrievalService


async def run_triage_planner(
    llm: StructuredLLM,
    *,
    budget: AnalysisBudget,
    trace: TraceAccumulator,
    model: str,
    temperature: float,
    change_summary: str,
    domain_pack_id: str,
) -> AnalysisPlan:
    budget.check_llm_call()
    user = (
        f"domain_pack_id={domain_pack_id}\n"
        f"change_summary:\n{change_summary}\n"
        f"prompt_version={PROMPT_VERSION}"
    )
    plan = await llm.generate(
        model=model,
        temperature=temperature,
        system_prompt=prompts.TRIAGE_SYSTEM,
        user_prompt=user,
        schema=AnalysisPlan,
    )
    budget.record_llm_call()
    trace.bump_llm()
    return plan


async def run_change_analyst(
    llm: StructuredLLM,
    mcp: McpClientPort,
    *,
    budget: AnalysisBudget,
    trace: TraceAccumulator,
    model: str,
    temperature: float,
    request: AnalysisRequest,
    signals: dict[str, Any],
) -> ChangeImpact:
    trace.mcp_tool_calls += 3
    context = await mcp_change_analyst_context(
        mcp,
        change_id=request.change_id,
        domain_pack_id=request.domain_pack_id,
    )
    context["deterministic_signals"] = signals
    budget.check_llm_call()
    impact = await llm.generate(
        model=model,
        temperature=temperature,
        system_prompt=prompts.CHANGE_ANALYST_SYSTEM,
        user_prompt=context_json(context),
        schema=ChangeImpact,
    )
    budget.record_llm_call()
    trace.bump_llm()
    return impact.model_copy(update={"change_id": request.change_id})


async def run_evidence_investigator(
    llm: StructuredLLM,
    retrieval: RetrievalService,
    *,
    budget: AnalysisBudget,
    trace: TraceAccumulator,
    model: str,
    temperature: float,
    request: AnalysisRequest,
    hypotheses: list[str],
    allowed_ids: set[str] | None = None,
) -> tuple[EvidenceBundle, list[RetrievedChunk], set[str]]:
    queries = hypotheses[:5] or [request.objective]
    chunks: list[RetrievedChunk] = []
    seen: set[str] = set()
    for query in queries:
        trace.retrieval_queries += 1
        batch = await retrieval.retrieve(query)
        for chunk in batch:
            key = evidence_id_from_metadata(chunk.metadata)
            if key not in seen:
                seen.add(key)
                chunks.append(chunk)
    id_set = collect_retrieved_evidence_ids(chunks)
    if allowed_ids is not None:
        id_set = id_set.intersection(allowed_ids)
        chunks = [
            chunk for chunk in chunks if evidence_id_from_metadata(chunk.metadata) in id_set
        ]
    chunk_payload = [
        {
            "evidence_id": evidence_id_from_metadata(chunk.metadata),
            "content": chunk.content,
            "metadata": chunk.metadata.model_dump(),
        }
        for chunk in chunks
    ]
    budget.check_llm_call()
    bundle = await llm.generate(
        model=model,
        temperature=temperature,
        system_prompt=prompts.EVIDENCE_INVESTIGATOR_SYSTEM,
        user_prompt=json.dumps(
            {"retrieved_chunks": chunk_payload, "hypotheses": hypotheses},
            indent=2,
        ),
        schema=EvidenceBundle,
    )
    budget.record_llm_call()
    trace.bump_llm()
    validated, _rejected = validate_bundle_citations(bundle, id_set)
    return validated, chunks, id_set


async def run_test_strategist(
    llm: StructuredLLM,
    mcp: McpClientPort,
    *,
    budget: AnalysisBudget,
    trace: TraceAccumulator,
    model: str,
    temperature: float,
    request: AnalysisRequest,
) -> RegressionPlan:
    trace.mcp_tool_calls += 1 if not request.execute_tests else 2
    context = await mcp_test_strategist_context(
        mcp,
        change_id=request.change_id,
        domain_pack_id=request.domain_pack_id,
        execute_tests=request.execute_tests,
    )
    budget.check_llm_call()
    plan = await llm.generate(
        model=model,
        temperature=temperature,
        system_prompt=prompts.TEST_STRATEGIST_SYSTEM,
        user_prompt=context_json(context),
        schema=RegressionPlan,
    )
    budget.record_llm_call()
    trace.bump_llm()
    return plan.model_copy(
        update={
            "change_id": request.change_id,
            "dry_run_only": not request.execute_tests,
            "execution_requested": request.execute_tests,
        },
    )


async def run_security_auditor(
    llm: StructuredLLM,
    mcp: McpClientPort,
    retrieval: RetrievalService | None,
    *,
    budget: AnalysisBudget,
    trace: TraceAccumulator,
    model: str,
    temperature: float,
    request: AnalysisRequest,
) -> SecurityFindings:
    trace.mcp_tool_calls += 2
    context = await mcp_security_auditor_context(
        mcp,
        change_id=request.change_id,
        domain_pack_id=request.domain_pack_id,
    )
    security_chunks: list[RetrievedChunk] = []
    if retrieval is not None:
        trace.retrieval_queries += 1
        raw = await retrieval.retrieve(
            f"security auth exposure vulnerabilities {request.change_id}",
        )
        security_types = {
            DocumentType.INCIDENT,
            DocumentType.RUNBOOK,
            DocumentType.RELEASE_POLICY,
        }
        for chunk in raw:
            if chunk.metadata.document_type in security_types:
                security_chunks.append(chunk)
    allowed = collect_retrieved_evidence_ids(security_chunks)
    context["security_retrieval"] = [
        {
            "evidence_id": evidence_id_from_metadata(chunk.metadata),
            "content": chunk.content,
        }
        for chunk in security_chunks
    ]
    budget.check_llm_call()
    findings = await llm.generate(
        model=model,
        temperature=temperature,
        system_prompt=prompts.SECURITY_AUDITOR_SYSTEM,
        user_prompt=context_json(context),
        schema=SecurityFindings,
    )
    budget.record_llm_call()
    trace.bump_llm()
    filtered, _rejected = filter_security_findings(findings.findings, allowed)
    return findings.model_copy(update={"findings": filtered})


async def run_evidence_verifier(
    llm: StructuredLLM,
    *,
    budget: AnalysisBudget,
    trace: TraceAccumulator,
    model: str,
    temperature: float,
    allowed_ids: set[str],
    specialist_payload: dict[str, Any],
) -> VerificationReport:
    budget.check_llm_call()
    user = json.dumps(
        {
            "allowed_evidence_ids": sorted(allowed_ids),
            "specialist_outputs": specialist_payload,
        },
        indent=2,
        default=str,
    )
    report = await llm.generate(
        model=model,
        temperature=temperature,
        system_prompt=prompts.EVIDENCE_VERIFIER_SYSTEM,
        user_prompt=user,
        schema=VerificationReport,
    )
    budget.record_llm_call()
    trace.bump_llm()
    from app.graph.evidence_ids import filter_verification_report

    filtered, _rejected = filter_verification_report(report, allowed_ids)
    return filtered
