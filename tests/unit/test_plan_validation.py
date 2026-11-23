"""Policy validation for triage plans."""

from __future__ import annotations

from app.core.domain_pack import load_domain_pack
from app.core.settings import get_settings
from app.graph.plan_validation import default_plan_for_pack, validate_analysis_plan
from app.graph.state import AnalysisPlan


def test_clamp_disallowed_agent() -> None:
    pack = load_domain_pack(
        "healthcare-claims",
        domains_root=get_settings().domains_root,
    )
    proposed = AnalysisPlan.model_construct(
        change_category="documentation",
        agents=["change_analyst", "not_a_real_agent"],
        rationale="test",
    )
    validated = validate_analysis_plan(proposed, pack)
    assert validated.clamped_to_default
    assert "change_analyst" in validated.agents
    assert "evidence_verifier" in validated.agents


def test_default_plan_is_allowed() -> None:
    pack = load_domain_pack(
        "healthcare-claims",
        domains_root=get_settings().domains_root,
    )
    default = default_plan_for_pack(pack)
    validated = validate_analysis_plan(default, pack)
    assert not validated.clamped_to_default or validated.agents


def test_category_floor_restores_required_agents() -> None:
    pack = load_domain_pack(
        "healthcare-claims",
        domains_root=get_settings().domains_root,
    )
    proposed = AnalysisPlan(
        change_category="api_contract",
        agents=["change_analyst"],
        rationale="planner tried to skip agents",
    )
    validated = validate_analysis_plan(proposed, pack)
    assert set(pack.policy.category_agent_map["api_contract"]) <= set(validated.agents)
    assert not validated.clamped_to_default


def test_long_rationale_truncated_not_rejected() -> None:
    pack = load_domain_pack(
        "healthcare-claims",
        domains_root=get_settings().domains_root,
    )
    proposed = AnalysisPlan(
        change_category="documentation",
        agents=["change_analyst", "evidence_investigator"],
        rationale="x" * 900,
    )
    assert len(validate_analysis_plan(proposed, pack).rationale) == 500
