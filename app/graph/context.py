"""Runtime context passed to LangGraph nodes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Protocol

from app.core.domain_pack import DomainPack
from app.graph.budget import AnalysisBudget, TraceAccumulator
from app.mcp_client.port import McpClientPort
from app.model_gateway.llm import StructuredLLM
from app.rag.models import RetrievedChunk


class EvidenceRetriever(Protocol):
    """Retrieval port used by agents (``RetrievalService`` or a test double)."""

    async def retrieve(self, query: str, *, k: int | None = None) -> list[RetrievedChunk]:
        """Return ranked chunks for a query."""
        ...


@dataclass
class AnalysisGraphContext:
    """Dependencies shared across graph nodes."""

    domain_pack: DomainPack
    mcp: McpClientPort
    llm: StructuredLLM
    budget: AnalysisBudget
    trace: TraceAccumulator = field(default_factory=TraceAccumulator)
    retrieval: EvidenceRetriever | None = None
    agent_model: str = ""
    triage_model: str = ""
    agent_temperature: float = 0.2
    # Throttles concurrent specialist agents; excess branches wait instead of failing.
    parallel_limit: asyncio.Semaphore = field(init=False)

    def __post_init__(self) -> None:
        self.parallel_limit = asyncio.Semaphore(max(1, self.budget.max_parallel_agents))
