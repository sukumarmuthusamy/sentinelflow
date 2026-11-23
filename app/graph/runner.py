"""Run the analysis graph with checkpointing and budgets."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from app.core.domain_pack import load_domain_pack
from app.core.settings import Settings, get_settings
from app.graph.budget import AnalysisBudget
from app.graph.build import build_analysis_graph
from app.graph.context import AnalysisGraphContext
from app.graph.state import AnalysisRequest
from app.graph.typed_state import GraphState
from app.mcp_client.port import McpClientPort
from app.model_gateway.llm import StructuredLLM
from app.rag.models import RerankerMode, VectorStoreBackend
from app.rag.retrieval import RetrievalService


@asynccontextmanager
async def _checkpointer(path: Path) -> AsyncIterator[AsyncSqliteSaver]:
    path.parent.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(path)) as saver:
        await saver.setup()
        yield saver


def _initial_state(request: AnalysisRequest, analysis_id: str | None = None) -> dict[str, object]:
    return {
        "analysis_id": analysis_id or "",
        "request": request.model_dump(mode="json"),
        "domain_pack_id": request.domain_pack_id,
        "deterministic_signals": None,
        "changed_files": [],
        "plan": None,
        "change_impact": None,
        "evidence": None,
        "regression_plan": None,
        "security_findings": None,
        "verification": None,
        "risk_assessment": None,
        "retrieved_evidence_ids": [],
        "retrieved_chunks": None,
        "evidence_gaps": [],
        "repair_count": 0,
        "approval_status": "not_requested",
        "trace_metrics": {},
        "final_report": None,
        "errors": [],
        "parallel_completions": [],
        "rejected_evidence_ids": [],
        "verification_metrics": None,
        "degraded_to_review": False,
        "record_decision_written": False,
    }


async def build_analysis_context(
    request: AnalysisRequest,
    *,
    mcp: McpClientPort,
    llm: StructuredLLM,
    settings: Settings | None = None,
    enable_retrieval: bool = True,
) -> AnalysisGraphContext:
    cfg = settings or get_settings()
    pack = load_domain_pack(request.domain_pack_id, domains_root=cfg.domains_root)
    retrieval: RetrievalService | None = None
    if enable_retrieval:
        backend = VectorStoreBackend(cfg.vector_store_backend)
        retrieval = RetrievalService(
            domain_pack_id=request.domain_pack_id,
            backend=backend,
            reranker_mode=RerankerMode.NONE,
            settings=cfg,
        )
        await retrieval.initialize()
    return AnalysisGraphContext(
        domain_pack=pack,
        mcp=mcp,
        llm=llm,
        budget=AnalysisBudget.from_settings(cfg),
        retrieval=retrieval,
        agent_model=cfg.agent_llm_model,
        triage_model=cfg.triage_llm_model,
        agent_temperature=cfg.agent_llm_temperature,
    )


def restore_counters(context: AnalysisGraphContext, values: dict[str, Any]) -> None:
    """Seed a resumed run's trace and budget from the checkpointed state.

    The context is rebuilt on resume, so without this every post-interrupt node
    writes zeroed trace metrics over the real ones and the call budget restarts.
    """
    trace = values.get("trace_metrics") or {}
    for name in (
        "agent_steps",
        "llm_calls",
        "mcp_tool_calls",
        "retrieval_queries",
        "input_tokens",
        "output_tokens",
        "citations_rejected",
    ):
        setattr(context.trace, name, int(trace.get(name) or 0))
    context.budget.llm_calls = context.trace.llm_calls
    context.budget.input_tokens = context.trace.input_tokens
    context.budget.output_tokens = context.trace.output_tokens


async def run_analysis(
    request: AnalysisRequest,
    *,
    mcp: McpClientPort,
    llm: StructuredLLM,
    thread_id: str,
    analysis_id: str | None = None,
    settings: Settings | None = None,
    enable_retrieval: bool = True,
) -> dict[str, Any]:
    """Execute the graph until completion or interrupt."""
    cfg = settings or get_settings()
    context = await build_analysis_context(
        request,
        mcp=mcp,
        llm=llm,
        settings=cfg,
        enable_retrieval=enable_retrieval,
    )
    config: RunnableConfig = {
        "configurable": {
            "thread_id": thread_id,
            "analysis_context": context,
        },
        "recursion_limit": cfg.graph_recursion_limit,
    }
    try:
        async with _checkpointer(cfg.graph_checkpoint_path) as checkpointer:
            compiled = build_analysis_graph(cfg).compile(checkpointer=checkpointer)
            state_in = cast(GraphState, _initial_state(request, analysis_id))
            result: dict[str, Any] = await compiled.ainvoke(
                state_in,
                config=config,
                durability="sync",
            )
            return result
    finally:
        if context.retrieval is not None:
            await context.retrieval.close()


async def resume_analysis(
    *,
    mcp: McpClientPort,
    llm: StructuredLLM,
    thread_id: str,
    request: AnalysisRequest,
    resume_payload: dict[str, Any],
    settings: Settings | None = None,
    enable_retrieval: bool = True,
) -> dict[str, Any]:
    """Resume after human approval interrupt."""
    cfg = settings or get_settings()
    context = await build_analysis_context(
        request,
        mcp=mcp,
        llm=llm,
        settings=cfg,
        enable_retrieval=enable_retrieval,
    )
    config: RunnableConfig = {
        "configurable": {
            "thread_id": thread_id,
            "analysis_context": context,
        },
        "recursion_limit": cfg.graph_recursion_limit,
    }
    try:
        async with _checkpointer(cfg.graph_checkpoint_path) as checkpointer:
            compiled = build_analysis_graph(cfg).compile(checkpointer=checkpointer)
            snapshot = await compiled.aget_state(config)
            restore_counters(context, snapshot.values)
            return await compiled.ainvoke(
                Command(resume=resume_payload),
                config=config,
                durability="sync",
            )
    finally:
        if context.retrieval is not None:
            await context.retrieval.close()
