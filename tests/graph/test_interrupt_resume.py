"""Graph interrupt/resume and record_gate_decision idempotency."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.graph.state import AnalysisRequest
from app.model_gateway.llm import StaticStructuredLLM
from tests.contract.conftest import mcp_session
from tests.graph.conftest import static_llm_responses


@pytest.mark.asyncio
async def test_interrupt_resume_record_gate_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    audit_log_path: Path,
) -> None:
    monkeypatch.setenv("SENTINELFLOW_AUDIT_LOG_PATH", str(audit_log_path))
    from mcp_server.sentinelflow_context.settings import clear_mcp_server_settings_cache

    clear_mcp_server_settings_cache()

    llm = StaticStructuredLLM(static_llm_responses())
    request = AnalysisRequest(
        change_id="CHG-H001",
        domain_pack_id="healthcare-claims",
        record_decision=True,
    )
    thread_id = "test-thread-interrupt"

    async with mcp_session() as mcp:
        from app.core.settings import get_settings
        from app.graph.runner import resume_analysis, run_analysis

        get_settings.cache_clear()
        monkeypatch.setenv("GRAPH_CHECKPOINT_PATH", str(tmp_path / "cp.sqlite"))
        monkeypatch.setenv("ANALYSIS_MAX_LLM_CALLS", "50")
        settings = get_settings()

        paused = await run_analysis(
            request,
            mcp=mcp,
            llm=llm,
            thread_id=thread_id,
            settings=settings,
            enable_retrieval=False,
        )
        assert paused.get("__interrupt__"), "expected interrupt before external write"

        resumed = await resume_analysis(
            mcp=mcp,
            llm=llm,
            thread_id=thread_id,
            request=request,
            resume_payload={"approved": True},
            settings=settings,
            enable_retrieval=False,
        )
        assert resumed.get("record_decision_written") is True
        # Resume rebuilds the context; trace counters must carry over, not reset.
        before = paused["trace_metrics"]
        after = resumed["trace_metrics"]
        assert before["llm_calls"] > 0
        assert after["llm_calls"] == before["llm_calls"]
        assert after["mcp_tool_calls"] >= before["mcp_tool_calls"]
        lines = audit_log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
