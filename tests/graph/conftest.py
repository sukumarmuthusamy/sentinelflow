"""Fixtures for graph integration tests."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import pytest
from app.agents.contracts import (
    ChangeImpact,
    EvidenceBundle,
    RegressionPlan,
    SecurityFindings,
    VerificationReport,
)
from app.graph.state import AnalysisPlan, AnalysisRequest
from app.model_gateway.llm import StaticStructuredLLM
from mcp_server.sentinelflow_context.settings import clear_mcp_server_settings_cache
from pydantic import BaseModel
from tests.contract.conftest import mcp_session
from tests.graph.fakes import DEFAULT_CHUNKS, FakeRetriever


@pytest.fixture
def audit_log_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated audit log for graph write tests."""
    path = tmp_path / "gate_decisions.jsonl"
    monkeypatch.setenv("SENTINELFLOW_AUDIT_LOG_PATH", str(path))
    clear_mcp_server_settings_cache()
    yield path
    clear_mcp_server_settings_cache()


_DEFAULT = object()

RunGraph = Callable[..., Awaitable[dict[str, Any]]]


@pytest.fixture
def run_graph(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, audit_log_path: Path) -> RunGraph:
    """Run the full graph offline: static LLM, in-memory MCP, fake retrieval.

    ``retriever=None`` runs with retrieval unavailable; omit it for the default fake.
    ``wrap_mcp`` lets a test substitute a failing MCP client around the real one.
    """
    _ = audit_log_path

    async def _run(
        change_id: str,
        llm: StaticStructuredLLM,
        *,
        pack_id: str = "healthcare-claims",
        env: dict[str, str] | None = None,
        retriever: Any = _DEFAULT,
        wrap_mcp: Callable[[Any], Any] | None = None,
        record_decision: bool = False,
    ) -> dict[str, Any]:
        from app.core.settings import get_settings
        from app.graph.runner import run_analysis

        monkeypatch.setenv("GRAPH_CHECKPOINT_PATH", str(tmp_path / f"{change_id}.sqlite"))
        for key, value in (env or {}).items():
            monkeypatch.setenv(key, value)
        get_settings.cache_clear()
        settings = get_settings()
        request = AnalysisRequest(
            change_id=change_id,
            domain_pack_id=pack_id,
            record_decision=record_decision,
        )
        chosen = FakeRetriever(DEFAULT_CHUNKS) if retriever is _DEFAULT else retriever
        try:
            async with mcp_session() as mcp:
                client = wrap_mcp(mcp) if wrap_mcp is not None else mcp
                return await run_analysis(
                    request,
                    mcp=client,
                    llm=llm,
                    thread_id=f"t-{change_id}-{tmp_path.name}",
                    settings=settings,
                    enable_retrieval=False,
                    retriever=chosen,
                )
        finally:
            get_settings.cache_clear()

    return _run


def static_llm_responses() -> dict[type[BaseModel], BaseModel]:
    """Minimal schema-valid payloads for a documentation change fixture."""
    return {
        AnalysisPlan: AnalysisPlan(
            change_category="documentation",
            agents=[
                "change_analyst",
                "evidence_investigator",
                "test_strategist",
                "security_auditor",
                "evidence_verifier",
            ],
            rationale="documentation-only",
        ),
        ChangeImpact: ChangeImpact(
            change_id="CHG-H001",
            impacted_services=["svc-analytics"],
            summary="doc-only",
        ),
        EvidenceBundle: EvidenceBundle(coverage=0.0, items=[]),
        RegressionPlan: RegressionPlan(change_id="CHG-H001", recommended_tests=[]),
        SecurityFindings: SecurityFindings(),
        VerificationReport: VerificationReport(
            unsupported_assertion_rate=0.0,
            evidence_coverage=0.9,
            repair_recommended=False,
        ),
    }
