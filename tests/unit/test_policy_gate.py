"""Policy gate precedence: PASS < REVIEW < BLOCK, independent of policy.yaml order."""

from __future__ import annotations

import itertools
from pathlib import Path

import pytest
from app.core.domain_pack import DomainPack, load_domain_pack
from app.deterministic.evaluate import (
    build_signals_from_fixture,
    evaluate_change_fixture,
    load_change_fixture_manifest,
    resolve_fixture_directory,
)
from app.deterministic.models import DeterministicSignals, PolicyGateResult
from app.deterministic.policy_gate import apply_policy_gate, strictest_decision
from app.deterministic.risk_engine import compute_risk_score
from app.graph.state import ReleaseDecision

DOMAINS_ROOT = Path(__file__).resolve().parents[2] / "domains"


def _pack(pack_id: str) -> DomainPack:
    return load_domain_pack(pack_id, domains_root=DOMAINS_ROOT)


def _signals(pack: DomainPack, change_id: str) -> DeterministicSignals:
    fixture = resolve_fixture_directory(change_id, pack_id=pack.pack_id, domains_root=DOMAINS_ROOT)
    manifest = load_change_fixture_manifest(fixture)
    return build_signals_from_fixture(fixture, manifest, pack, fixture_only=True)


def _gate(
    pack: DomainPack, signals: DeterministicSignals, *, score: int | None = None
) -> PolicyGateResult:
    risk = compute_risk_score(signals, pack)
    if score is not None:
        risk = risk.model_copy(update={"risk_score": score})
    return apply_policy_gate(signals, pack, risk)


WEAK_EVIDENCE = {"evidence_coverage": 0.5}
CRITICAL_UNSUPPORTED = {"repair_attempted": True, "critical_claims_unsupported": True}


class TestBlockIsNeverDowngraded:
    @pytest.mark.parametrize(
        "update",
        [WEAK_EVIDENCE, CRITICAL_UNSUPPORTED, {**WEAK_EVIDENCE, **CRITICAL_UNSUPPORTED}],
        ids=["weak-evidence", "critical-unsupported", "both"],
    )
    def test_dependency_ceiling_block_survives_review_overrides(
        self, update: dict[str, object]
    ) -> None:
        pack = _pack("ecommerce-payments")
        signals = _signals(pack, "CHG-E003").model_copy(update=update)
        gate = _gate(pack, signals)
        assert gate.decision == ReleaseDecision.BLOCK
        assert gate.override_applied == "dependency-vuln-ceiling"
        # The weaker reasons are kept for audit, strictest first.
        assert gate.overrides_matched[0] == "dependency-vuln-ceiling"
        assert set(gate.overrides_matched) > {"dependency-vuln-ceiling"}

    @pytest.mark.parametrize(
        "update", [WEAK_EVIDENCE, CRITICAL_UNSUPPORTED], ids=["weak", "critical"]
    )
    def test_score_block_survives_review_override(self, update: dict[str, object]) -> None:
        pack = _pack("healthcare-claims")
        signals = _signals(pack, "CHG-H001").model_copy(update=update)
        gate = _gate(pack, signals, score=86)
        assert gate.score_decision == ReleaseDecision.BLOCK
        assert gate.decision == ReleaseDecision.BLOCK
        # The score set the decision; the REVIEW override is recorded but did not decide.
        assert gate.override_applied is None
        assert gate.overrides_matched


class TestOrderIndependence:
    def test_every_override_order_gives_the_same_result(self) -> None:
        pack = _pack("ecommerce-payments")
        signals = _signals(pack, "CHG-E003").model_copy(
            update={**WEAK_EVIDENCE, **CRITICAL_UNSUPPORTED}
        )
        overrides = pack.policy.overrides
        results = set()
        for order in itertools.permutations(overrides):
            permuted = pack.model_copy(
                update={"policy": pack.policy.model_copy(update={"overrides": list(order)})},
            )
            gate = _gate(permuted, signals)
            results.add((gate.decision, gate.override_applied, tuple(gate.overrides_matched)))
        assert len(results) == 1
        decision, applied, _ = results.pop()
        assert decision == ReleaseDecision.BLOCK
        assert applied == "dependency-vuln-ceiling"

    def test_two_block_overrides_tie_break_by_id(self) -> None:
        pack = _pack("healthcare-claims")
        signals = _signals(pack, "CHG-H002")
        signals = signals.model_copy(
            update={
                "security": signals.security.model_copy(update={"verified_secret_exposure": True})
            },
        )
        for order in (pack.policy.overrides, list(reversed(pack.policy.overrides))):
            permuted = pack.model_copy(
                update={"policy": pack.policy.model_copy(update={"overrides": list(order)})},
            )
            gate = _gate(permuted, signals)
            assert gate.decision == ReleaseDecision.BLOCK
            assert gate.override_applied == "critical-contract-break"
            assert gate.overrides_matched[:2] == ["critical-contract-break", "secret-exposure"]


class TestExistingBehaviourUnchanged:
    @pytest.mark.parametrize(
        ("pack_id", "change_id", "expected", "override"),
        [
            ("healthcare-claims", "CHG-H001", ReleaseDecision.PASS, None),
            ("healthcare-claims", "CHG-H004", ReleaseDecision.REVIEW, "weak-evidence"),
            (
                "ecommerce-payments",
                "CHG-E004",
                ReleaseDecision.REVIEW,
                "unsupported-critical-claim",
            ),
            ("ecommerce-payments", "CHG-E003", ReleaseDecision.BLOCK, "dependency-vuln-ceiling"),
            ("healthcare-claims", "CHG-H002", ReleaseDecision.BLOCK, "critical-contract-break"),
        ],
    )
    def test_m1_fixture_decisions(
        self,
        pack_id: str,
        change_id: str,
        expected: ReleaseDecision,
        override: str | None,
    ) -> None:
        fixture = resolve_fixture_directory(change_id, pack_id=pack_id, domains_root=DOMAINS_ROOT)
        result = evaluate_change_fixture(
            fixture, pack_id=pack_id, domains_root=DOMAINS_ROOT, fixture_only=True
        )
        assert result.decision == expected
        assert result.override_applied == override

    def test_review_override_still_escalates_a_pass_score(self) -> None:
        pack = _pack("healthcare-claims")
        gate = _gate(pack, _signals(pack, "CHG-H001").model_copy(update=WEAK_EVIDENCE))
        assert gate.score_decision == ReleaseDecision.PASS
        assert gate.decision == ReleaseDecision.REVIEW
        assert gate.override_applied == "weak-evidence"


def test_strictest_decision() -> None:
    assert (
        strictest_decision(ReleaseDecision.PASS, ReleaseDecision.REVIEW) == ReleaseDecision.REVIEW
    )
    assert (
        strictest_decision(ReleaseDecision.BLOCK, ReleaseDecision.REVIEW) == ReleaseDecision.BLOCK
    )
    assert strictest_decision(ReleaseDecision.PASS) == ReleaseDecision.PASS
