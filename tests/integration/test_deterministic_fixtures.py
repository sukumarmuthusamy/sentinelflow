"""Integration tests for M1 deterministic fixture evaluation."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.core.domain_pack import list_domain_pack_ids, load_domain_pack
from app.deterministic.evaluate import (
    evaluate_change_fixture,
    list_change_fixtures,
    load_change_fixture_manifest,
)
from app.graph.state import ReleaseDecision

DOMAINS_ROOT = Path(__file__).resolve().parents[2] / "domains"


@pytest.mark.parametrize("pack_id", list_domain_pack_ids(domains_root=DOMAINS_ROOT))
def test_each_fixture_produces_score_and_verdict(pack_id: str) -> None:
    pack_root = DOMAINS_ROOT / pack_id
    fixtures = list_change_fixtures(pack_root)
    assert len(fixtures) >= 3

    for fixture_dir in fixtures:
        manifest = load_change_fixture_manifest(fixture_dir)
        result = evaluate_change_fixture(
            fixture_dir,
            domain_pack=load_domain_pack(pack_id, domains_root=DOMAINS_ROOT),
            fixture_only=True,
        )

        assert result.change_id == manifest.change_id
        assert 0 <= result.risk_score <= 100
        assert result.decision in ReleaseDecision
        assert result.gate.norms.blast_radius_norm <= 1.0

        if manifest.expected_decision is not None:
            assert result.decision == manifest.expected_decision


def test_override_rules_fire_for_seeded_scenarios() -> None:
    healthcare_block = evaluate_change_fixture(
        DOMAINS_ROOT / "healthcare-claims" / "fixtures" / "changes" / "CHG-H002",
        fixture_only=True,
    )
    assert healthcare_block.decision == ReleaseDecision.BLOCK
    assert healthcare_block.override_condition == "oasdiff_err_on_critical_service"

    ecommerce_review = evaluate_change_fixture(
        DOMAINS_ROOT / "ecommerce-payments" / "fixtures" / "changes" / "CHG-E004",
        fixture_only=True,
    )
    assert ecommerce_review.decision == ReleaseDecision.REVIEW
    assert ecommerce_review.override_condition == "critical_claim_unsupported_after_repair"
