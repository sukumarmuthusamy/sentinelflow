"""Runtime context passed to LangGraph nodes."""

from __future__ import annotations

from dataclasses import dataclass, field

from app.core.domain_pack import DomainPack
from app.graph.budget import AnalysisBudget, TraceAccumulator
from app.mcp_client.port import McpClientPort
from app.model_gateway.llm import StructuredLLM
from app.rag.retrieval import RetrievalService


@dataclass
class AnalysisGraphContext:
    """Dependencies shared across graph nodes."""

    domain_pack: DomainPack
    mcp: McpClientPort
    llm: StructuredLLM
    budget: AnalysisBudget
    trace: TraceAccumulator = field(default_factory=TraceAccumulator)
    retrieval: RetrievalService | None = None
    agent_model: str = ""
    triage_model: str = ""
    agent_temperature: float = 0.2
