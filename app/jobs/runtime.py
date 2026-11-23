"""Graph dependencies for API-launched runs, and the executor that drives the graph.

``AnalysisRuntime`` is the injection seam. The default runtime builds the real
in-process MCP client, the routing LLM gateway and (inside ``run_analysis``) the
retrieval service. Tests inject a runtime with a static LLM and a fake retriever.

``GraphExecutor`` is the only module that calls ``run_analysis`` /
``resume_analysis``. M5-B swaps their ``ainvoke`` for ``astream`` here, behind the
same ``start`` / ``resume`` calls, without touching the job service or the routes.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.errors import ErrorCode, SentinelFlowError
from app.core.settings import Settings
from app.graph.context import EvidenceRetriever
from app.graph.runner import resume_analysis, run_analysis
from app.graph.state import AnalysisRequest
from app.mcp_client.port import McpClientPort
from app.model_gateway.llm import (
    GeminiStructuredLLM,
    OpenRouterStructuredLLM,
    RoutingStructuredLLM,
    StructuredLLM,
    is_openrouter_model,
)
from app.model_gateway.pacing import AsyncMinIntervalPacer, min_interval_for_rpm


@dataclass
class AnalysisSession:
    """Dependencies for one graph execution (a first run or a resume)."""

    mcp: McpClientPort
    llm: StructuredLLM
    retriever: EvidenceRetriever | None = None
    enable_retrieval: bool = True


class AnalysisRuntime(Protocol):
    """Where an API-launched analysis gets its MCP client, LLM and retrieval."""

    def check_ready(self, request: AnalysisRequest) -> None:
        """Raise ``PROVIDER_NOT_CONFIGURED`` before a job is created, never during it."""
        ...

    def session(self) -> AbstractAsyncContextManager[AnalysisSession]:
        """Open dependencies for one execution; closed when it ends."""
        ...


def _has_secret(value: object) -> bool:
    getter = getattr(value, "get_secret_value", None)
    return bool(getter is not None and str(getter()).strip())


def missing_provider_settings(settings: Settings) -> list[str]:
    """Names (never values) of the environment variables a live run still needs."""
    missing: set[str] = set()
    for model in (settings.agent_llm_model, settings.triage_llm_model):
        if is_openrouter_model(model):
            if not _has_secret(settings.openrouter_api_key):
                missing.add("OPENROUTER_API_KEY")
        elif not _has_secret(settings.gemini_api_key):
            missing.add("GEMINI_API_KEY")
    # Retrieval embeds queries with Gemini whatever model the agents use.
    if not _has_secret(settings.gemini_api_key):
        missing.add("GEMINI_API_KEY")
    return sorted(missing)


class DefaultAnalysisRuntime:
    """Real dependencies: in-process ``sentinelflow-context`` MCP server, live LLMs.

    The gateway is built once and shared by every run in this process, so its
    per-provider pacers space calls across concurrent jobs too. SDK retries are off,
    so one paced call is one HTTP attempt (the M4 free-tier discipline).
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._llm: StructuredLLM | None = None

    def check_ready(self, request: AnalysisRequest) -> None:
        _ = request
        missing = missing_provider_settings(self._settings)
        if missing:
            raise SentinelFlowError(
                "Model provider configuration is incomplete for a live analysis",
                code=ErrorCode.PROVIDER_NOT_CONFIGURED,
                details={"missing_settings": missing},
            )

    def _gateway(self) -> StructuredLLM:
        if self._llm is None:
            cfg = self._settings
            gemini = GeminiStructuredLLM(
                cfg,
                client_max_retries=0,
                pacer=AsyncMinIntervalPacer(min_interval_for_rpm(cfg.api_gemini_rpm)),
            )
            openrouter = None
            if _has_secret(cfg.openrouter_api_key):
                openrouter = OpenRouterStructuredLLM(
                    cfg,
                    client_max_retries=0,
                    pacer=AsyncMinIntervalPacer(min_interval_for_rpm(cfg.openrouter_rpm)),
                )
            self._llm = RoutingStructuredLLM(gemini=gemini, openrouter=openrouter)
        return self._llm

    @asynccontextmanager
    async def _open(self) -> AsyncIterator[AnalysisSession]:
        from mcp_server.sentinelflow_context.server import mcp as context_server

        from app.mcp_client import SentinelFlowMcpAdapter

        async with SentinelFlowMcpAdapter(context_server) as adapter:
            yield AnalysisSession(mcp=adapter, llm=self._gateway(), enable_retrieval=True)

    def session(self) -> AbstractAsyncContextManager[AnalysisSession]:
        return self._open()


@dataclass(frozen=True)
class GraphOutcome:
    """Final or interrupted graph state after one execution."""

    state: dict[str, Any]

    @property
    def interrupted(self) -> bool:
        return bool(self.state.get("__interrupt__"))


class GraphExecutor:
    """Runs and resumes the M4 graph on a job's own LangGraph thread."""

    def __init__(self, runtime: AnalysisRuntime, settings: Settings) -> None:
        self._runtime = runtime
        self._settings = settings

    async def start(
        self,
        request: AnalysisRequest,
        *,
        thread_id: str,
        analysis_id: str,
    ) -> GraphOutcome:
        async with self._runtime.session() as session:
            state = await run_analysis(
                request,
                mcp=session.mcp,
                llm=session.llm,
                thread_id=thread_id,
                analysis_id=analysis_id,
                settings=self._settings,
                enable_retrieval=session.enable_retrieval,
                retriever=session.retriever,
            )
        return GraphOutcome(state=state)

    async def resume(
        self,
        request: AnalysisRequest,
        *,
        thread_id: str,
        approved: bool,
    ) -> GraphOutcome:
        """Resume the interrupt on the original thread (counters restored by the runner).

        Only ``approved`` reaches the graph. Reviewer identity and comments stay in
        the job record, so nothing a client sends can reach graph state.
        """
        async with self._runtime.session() as session:
            state = await resume_analysis(
                mcp=session.mcp,
                llm=session.llm,
                thread_id=thread_id,
                request=request,
                resume_payload={"approved": approved},
                settings=self._settings,
                enable_retrieval=session.enable_retrieval,
                retriever=session.retriever,
            )
        return GraphOutcome(state=state)
