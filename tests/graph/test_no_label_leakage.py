"""Benchmark labels and M1 verification stubs must never reach agents or runtime scoring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.agents.observable import (
    OBSERVABLE_PACKAGE_FIELDS,
    VERIFICATION_RESULT_FIELDS,
    agent_signal_view,
    runtime_signals,
)
from app.core.domain_pack import load_domain_pack
from app.deterministic.evaluate import (
    build_signals_from_fixture,
    load_change_fixture_manifest,
    resolve_fixture_directory,
)
from app.model_gateway.llm import StaticStructuredLLM
from tests.graph.conftest import RunGraph, static_llm_responses

DOMAINS_ROOT = Path(__file__).resolve().parents[2] / "domains"

# Fixtures whose manifests carry verification stubs and answer-revealing descriptions.
CASES = [
    ("healthcare-claims", "CHG-H001"),
    ("healthcare-claims", "CHG-H004"),
    ("ecommerce-payments", "CHG-E001"),
    ("ecommerce-payments", "CHG-E004"),
]
BANNED_KEYS = [f'"{field}"' for field in sorted(VERIFICATION_RESULT_FIELDS)] + [
    '"expected_decision"',
    '"fixture_path"',
]


def _manifest(pack_id: str, change_id: str):  # type: ignore[no-untyped-def]
    fixture = resolve_fixture_directory(change_id, pack_id=pack_id, domains_root=DOMAINS_ROOT)
    return fixture, load_change_fixture_manifest(fixture)


@pytest.mark.asyncio
@pytest.mark.parametrize(("pack_id", "change_id"), CASES)
async def test_no_label_or_stub_reaches_any_prompt(
    run_graph: RunGraph,
    pack_id: str,
    change_id: str,
) -> None:
    _, manifest = _manifest(pack_id, change_id)
    llm = StaticStructuredLLM(static_llm_responses())
    await run_graph(change_id, llm, pack_id=pack_id)
    assert llm.prompts
    for schema, system, user in llm.prompts:
        text = system + user
        assert manifest.description not in text, f"{schema} saw the fixture description"
        for key in BANNED_KEYS:
            assert key not in text, f"{schema} prompt contains {key}"
    analyst = next(user for schema, _, user in llm.prompts if schema == "ChangeImpact")
    package = json.loads(analyst)["change_package"]
    assert set(package) <= OBSERVABLE_PACKAGE_FIELDS
    assert "category" not in package and "description" not in package


@pytest.mark.asyncio
async def test_stub_critical_claim_does_not_drive_runtime_scoring(run_graph: RunGraph) -> None:
    """CHG-E004's manifest says "critical claim unsupported after repair"; the graph
    must measure that itself, not inherit it."""
    state = await run_graph(
        "CHG-E004", StaticStructuredLLM(static_llm_responses()), pack_id="ecommerce-payments"
    )
    signals = state["deterministic_signals"]
    # No agent emitted a critical assertion, so none can be unsupported.
    assert signals["critical_claims_unsupported"] is False
    # repair_attempted reflects this run's own repair loop, not the manifest.
    assert signals["repair_attempted"] is (state["repair_count"] > 0)
    assert "unsupported-critical-claim" not in state["risk_assessment"]["overrides_matched"]


@pytest.mark.asyncio
async def test_stub_coverage_does_not_drive_degraded_scoring(run_graph: RunGraph) -> None:
    """CHG-H004's manifest stub coverage is 0.5; a degraded run must not cite it."""
    state = await run_graph(
        "CHG-H004",
        StaticStructuredLLM(static_llm_responses()),
        env={"ANALYSIS_MAX_LLM_CALLS": "1"},
    )
    assert state["degraded_to_review"] is True
    assessment = state["risk_assessment"]
    assert assessment["decision"] == "REVIEW"
    assert "weak-evidence" not in assessment["overrides_matched"]
    assert state["deterministic_signals"]["evidence_coverage"] is None


def test_runtime_signals_clear_stubs_but_keep_observable_facts() -> None:
    pack = load_domain_pack("ecommerce-payments", domains_root=DOMAINS_ROOT)
    fixture, manifest = _manifest("ecommerce-payments", "CHG-E004")
    fixture_signals = build_signals_from_fixture(fixture, manifest, pack, fixture_only=True)
    assert fixture_signals.critical_claims_unsupported is True  # M1 fixture input intact
    live = runtime_signals(fixture_signals)
    assert live.evidence_coverage is None
    assert live.critical_claims_unsupported is False
    assert live.repair_attempted is False
    assert live.contract_diff == fixture_signals.contract_diff
    assert live.security == fixture_signals.security
    view = agent_signal_view(live)
    assert not set(view) & VERIFICATION_RESULT_FIELDS
    assert view["primary_service"] == manifest.primary_service
