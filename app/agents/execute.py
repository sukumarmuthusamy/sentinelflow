"""Run specialist agents with versioned prompts and typed outputs."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

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
from app.agents.prompts import v2 as prompts
from app.graph.budget import AnalysisBudget, TraceAccumulator
from app.graph.evidence_ids import (
    collect_retrieved_evidence_ids,
    evidence_id_from_metadata,
    filter_security_findings,
    filter_verification_report,
    validate_bundle_citations,
)
from app.graph.state import AnalysisPlan, AnalysisRequest
from app.graph.verification import MaterialAssertion
from app.mcp_client.port import McpClientPort
from app.model_gateway.llm import StructuredLLM
from app.rag.models import DocumentType, RetrievedChunk
from app.rag.retrieval import RetrievalService

SECURITY_DOCUMENT_TYPES: frozenset[DocumentType] = frozenset(
    {DocumentType.INCIDENT, DocumentType.RUNBOOK, DocumentType.RELEASE_POLICY},
)


@dataclass
class RetrievalOutcome:
    """Validated agent output plus the evidence it was allowed to cite."""

    allowed_ids: set[str] = field(default_factory=set)
    chunks: list[RetrievedChunk] = field(default_factory=list)
    rejected_ids: list[str] = field(default_factory=list)


async def _generate[T: BaseModel](
    llm: StructuredLLM,
    *,
    budget: AnalysisBudget,
    trace: TraceAccumulator,
    model: str,
    temperature: float,
    system_prompt: str,
    user_prompt: str,
    schema: type[T],
) -> T:
    """Single LLM call path: budget check, call, token accounting."""
    budget.check_llm_call()
    result = await llm.generate(
        model=model,
        temperature=temperature,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        schema=schema,
    )
    budget.record_llm_call(
        input_tokens=result.input_tokens,
        output_tokens=result.output_tokens,
    )
    trace.bump_llm(input_tokens=result.input_tokens, output_tokens=result.output_tokens)
    return result.value


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
    user = (
        f"domain_pack_id={domain_pack_id}\n"
        f"change_summary:\n{change_summary}\n"
        f"prompt_version={PROMPT_VERSION}"
    )
    return await _generate(
        llm,
        budget=budget,
        trace=trace,
        model=model,
        temperature=temperature,
        system_prompt=prompts.TRIAGE_SYSTEM,
        user_prompt=user,
        schema=AnalysisPlan,
    )


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
    impact = await _generate(
        llm,
        budget=budget,
        trace=trace,
        model=model,
        temperature=temperature,
        system_prompt=prompts.CHANGE_ANALYST_SYSTEM,
        user_prompt=context_json(context),
        schema=ChangeImpact,
    )
    return impact.model_copy(update={"change_id": request.change_id})


async def run_evidence_investigator(
    llm: StructuredLLM,
    retrieval: RetrievalService,
    *,
    budget: AnalysisBudget,
    trace: TraceAccumulator,
    model: str,
    temperature: float,
    queries: list[str],
) -> tuple[EvidenceBundle, RetrievalOutcome]:
    """Retrieve for each query, then cite only what was retrieved in this call."""
    chunks: list[RetrievedChunk] = []
    seen: set[str] = set()
    for query in queries:
        trace.retrieval_queries += 1
        for chunk in await retrieval.retrieve(query):
            key = evidence_id_from_metadata(chunk.metadata)
            if key not in seen:
                seen.add(key)
                chunks.append(chunk)
    allowed = collect_retrieved_evidence_ids(chunks)
    chunk_payload = [
        {
            "evidence_id": evidence_id_from_metadata(chunk.metadata),
            "content": chunk.content,
            "metadata": chunk.metadata.model_dump(),
        }
        for chunk in chunks
    ]
    bundle = await _generate(
        llm,
        budget=budget,
        trace=trace,
        model=model,
        temperature=temperature,
        system_prompt=prompts.EVIDENCE_INVESTIGATOR_SYSTEM,
        user_prompt=json.dumps(
            {
                "focus_queries": {f"q{i}": query for i, query in enumerate(queries)},
                "retrieved_chunks": chunk_payload,
            },
            indent=2,
        ),
        schema=EvidenceBundle,
    )
    validated, rejected = validate_bundle_citations(bundle, allowed)
    trace.citations_rejected += len(rejected)
    validated = validated.model_copy(update={"retrieval_queries": list(queries)})
    return validated, RetrievalOutcome(allowed_ids=allowed, chunks=chunks, rejected_ids=rejected)


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
    plan = await _generate(
        llm,
        budget=budget,
        trace=trace,
        model=model,
        temperature=temperature,
        system_prompt=prompts.TEST_STRATEGIST_SYSTEM,
        user_prompt=context_json(context),
        schema=RegressionPlan,
    )
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
    security_query: str,
) -> tuple[SecurityFindings, RetrievalOutcome]:
    trace.mcp_tool_calls += 2
    context = await mcp_security_auditor_context(
        mcp,
        change_id=request.change_id,
        domain_pack_id=request.domain_pack_id,
    )
    security_chunks: list[RetrievedChunk] = []
    if retrieval is not None:
        trace.retrieval_queries += 1
        for chunk in await retrieval.retrieve(security_query):
            if chunk.metadata.document_type in SECURITY_DOCUMENT_TYPES:
                security_chunks.append(chunk)
    allowed = collect_retrieved_evidence_ids(security_chunks)
    context["security_retrieval"] = [
        {
            "evidence_id": evidence_id_from_metadata(chunk.metadata),
            "content": chunk.content,
        }
        for chunk in security_chunks
    ]
    findings = await _generate(
        llm,
        budget=budget,
        trace=trace,
        model=model,
        temperature=temperature,
        system_prompt=prompts.SECURITY_AUDITOR_SYSTEM,
        user_prompt=context_json(context),
        schema=SecurityFindings,
    )
    filtered, rejected = filter_security_findings(findings.findings, allowed)
    trace.citations_rejected += len(rejected)
    outcome = RetrievalOutcome(allowed_ids=allowed, chunks=security_chunks, rejected_ids=rejected)
    return findings.model_copy(update={"findings": filtered}), outcome


async def run_evidence_verifier(
    llm: StructuredLLM,
    *,
    budget: AnalysisBudget,
    trace: TraceAccumulator,
    model: str,
    temperature: float,
    allowed_ids: set[str],
    assertions: list[MaterialAssertion],
    specialist_payload: dict[str, Any],
) -> tuple[VerificationReport, list[str]]:
    user = json.dumps(
        {
            "material_assertions": [assertion.model_dump() for assertion in assertions],
            "allowed_evidence_ids": sorted(allowed_ids),
            "specialist_outputs": specialist_payload,
        },
        indent=2,
        default=str,
    )
    report = await _generate(
        llm,
        budget=budget,
        trace=trace,
        model=model,
        temperature=temperature,
        system_prompt=prompts.EVIDENCE_VERIFIER_SYSTEM,
        user_prompt=user,
        schema=VerificationReport,
    )
    filtered, rejected = filter_verification_report(report, allowed_ids)
    trace.citations_rejected += len(rejected)
    return filtered, rejected
