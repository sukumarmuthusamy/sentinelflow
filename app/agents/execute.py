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
from app.agents.prompts import v3 as prompts
from app.graph.budget import AnalysisBudget, TraceAccumulator
from app.graph.context import EvidenceRetriever
from app.graph.evidence_context import VerifierEvidence
from app.graph.evidence_ids import (
    collect_retrieved_evidence_ids,
    evidence_id_from_metadata,
    filter_security_findings,
    filter_verification_report,
    validate_bundle_citations,
)
from app.graph.injection import looks_like_injection
from app.graph.state import AnalysisPlan, AnalysisRequest
from app.graph.verification import MaterialAssertion, ground_report
from app.mcp_client.port import McpClientPort
from app.model_gateway.llm import StructuredLLM, StructuredOutputError
from app.rag.models import DocumentType, RetrievedChunk

SECURITY_DOCUMENT_TYPES: frozenset[DocumentType] = frozenset(
    {DocumentType.INCIDENT, DocumentType.RUNBOOK, DocumentType.RELEASE_POLICY},
)


@dataclass
class RetrievalOutcome:
    """Validated agent output plus the evidence it was allowed to cite."""

    allowed_ids: set[str] = field(default_factory=set)
    chunks: list[RetrievedChunk] = field(default_factory=list)
    rejected_ids: list[str] = field(default_factory=list)
    # Retrieved chunks withheld from every prompt because they contain instruction-like text.
    injection_ids: list[str] = field(default_factory=list)


def screen_chunks(chunks: list[RetrievedChunk]) -> tuple[list[RetrievedChunk], list[str]]:
    """Split retrieved chunks into clean ones and IDs of instruction-like ones."""
    clean: list[RetrievedChunk] = []
    flagged: list[str] = []
    for chunk in chunks:
        if looks_like_injection(chunk.content):
            flagged.append(evidence_id_from_metadata(chunk.metadata))
        else:
            clean.append(chunk)
    return clean, flagged


@dataclass
class VerifierOutcome:
    """Citation-filtered, source-grounded verifier report."""

    report: VerificationReport
    # Cited IDs that were not among the chunks shown to the verifier.
    rejected_ids: list[str]
    # "supported" judgments demoted because the shown source text did not back them.
    grounding_rejections: list[str]


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
    """Single LLM call path: reserve budget, call, settle as completed or failed."""
    reservation = budget.reserve_llm_call()
    try:
        result = await llm.generate(
            model=model,
            temperature=temperature,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema=schema,
        )
    except StructuredOutputError as exc:
        # The provider answered (and billed tokens) but the output was unusable.
        reservation.fail(input_tokens=exc.input_tokens, output_tokens=exc.output_tokens)
        trace.bump_llm_failed(input_tokens=exc.input_tokens, output_tokens=exc.output_tokens)
        raise
    except BaseException:
        # Includes cancellation: the request may have reached the provider, so the
        # reserved call is consumed rather than refunded.
        reservation.fail()
        trace.bump_llm_failed()
        raise
    reservation.complete(input_tokens=result.input_tokens, output_tokens=result.output_tokens)
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
    retrieval: EvidenceRetriever,
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
    chunks, flagged = screen_chunks(chunks)
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
                "untrusted_retrieved_documents": chunk_payload,
            },
            indent=2,
        ),
        schema=EvidenceBundle,
    )
    validated, rejected = validate_bundle_citations(bundle, allowed)
    trace.citations_rejected += len(rejected)
    validated = validated.model_copy(update={"retrieval_queries": list(queries)})
    return validated, RetrievalOutcome(
        allowed_ids=allowed,
        chunks=chunks,
        rejected_ids=rejected,
        injection_ids=flagged,
    )


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
    context = await mcp_test_strategist_context(
        mcp,
        change_id=request.change_id,
        domain_pack_id=request.domain_pack_id,
        execute_tests=request.execute_tests,
    )
    trace.mcp_tool_calls += 2 if "run_tests" in context else 1
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
    retrieval: EvidenceRetriever | None,
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
    security_chunks, flagged = screen_chunks(security_chunks)
    allowed = collect_retrieved_evidence_ids(security_chunks)
    context["untrusted_retrieved_documents"] = [
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
    outcome = RetrievalOutcome(
        allowed_ids=allowed,
        chunks=security_chunks,
        rejected_ids=rejected,
        injection_ids=flagged,
    )
    return findings.model_copy(update={"findings": filtered}), outcome


async def run_evidence_verifier(
    llm: StructuredLLM,
    *,
    budget: AnalysisBudget,
    trace: TraceAccumulator,
    model: str,
    temperature: float,
    assertions: list[MaterialAssertion],
    evidence: VerifierEvidence,
    specialist_payload: dict[str, Any],
) -> VerifierOutcome:
    """Verify assertions against the shown source text, then ground in code.

    The verifier may cite only chunks it was shown. A "supported" judgment survives
    only if its supporting quote is found in a cited chunk (``ground_report``).
    """
    shown_ids = evidence.shown_ids
    user = json.dumps(
        {
            "material_assertions": [
                {
                    "assertion_id": assertion.assertion_id,
                    "text": assertion.text,
                    "severity": assertion.severity,
                    "source": assertion.source,
                    "specialist_cited_evidence_ids": [
                        eid for eid in assertion.cited_evidence_ids if eid in shown_ids
                    ],
                }
                for assertion in assertions
            ],
            "untrusted_retrieved_documents": [
                {
                    "evidence_id": chunk.evidence_id,
                    "document_type": chunk.document_type,
                    "service": chunk.service,
                    "content": chunk.content,
                }
                for chunk in evidence.shown
            ],
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
    filtered, rejected = filter_verification_report(report, shown_ids)
    trace.citations_rejected += len(rejected)
    grounded, grounding_rejections = ground_report(filtered, assertions, evidence.content_by_id())
    return VerifierOutcome(
        report=grounded,
        rejected_ids=rejected,
        grounding_rejections=grounding_rejections,
    )
