"""Fixtures for graph integration tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.agents.contracts import (
    ChangeImpact,
    EvidenceBundle,
    RegressionPlan,
    SecurityFindings,
    VerificationReport,
)
from app.graph.state import AnalysisPlan
from mcp_server.sentinelflow_context.settings import clear_mcp_server_settings_cache
from pydantic import BaseModel


@pytest.fixture
def audit_log_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated audit log for graph write tests."""
    path = tmp_path / "gate_decisions.jsonl"
    monkeypatch.setenv("SENTINELFLOW_AUDIT_LOG_PATH", str(path))
    clear_mcp_server_settings_cache()
    yield path
    clear_mcp_server_settings_cache()


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
