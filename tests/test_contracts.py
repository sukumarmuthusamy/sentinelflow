"""Smoke tests for Pydantic domain contracts."""

from app.agents.contracts import (
    ChangeImpact,
    EvidenceBundle,
    RegressionPlan,
    SecurityFindings,
    VerificationReport,
)
from app.graph.state import AnalysisPlan, AnalysisRequest, AnalysisState


def test_agent_contracts_instantiate_with_minimal_fields() -> None:
    ChangeImpact(change_id="CHG-001")
    EvidenceBundle()
    RegressionPlan(change_id="CHG-001")
    SecurityFindings()
    VerificationReport()


def test_analysis_state_and_plan_models() -> None:
    request = AnalysisRequest(change_id="CHG-001", domain_pack_id="pack-alpha")
    plan = AnalysisPlan(
        change_category="api_contract",
        agents=["change_analyst", "evidence_investigator"],
        rationale="Contract surface changed",
    )
    state = AnalysisState(
        analysis_id="anl_test",
        request=request,
        domain_pack_id="pack-alpha",
        plan=plan,
    )
    assert state.plan is not None
    assert state.plan.change_category == "api_contract"
