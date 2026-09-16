"""Unit tests for deterministic risk engine normalization terms."""

from __future__ import annotations

from pathlib import Path

import pytest
from app.core.domain_pack import DomainPack, load_domain_pack
from app.deterministic.models import (
    BlastRadiusNode,
    BlastRadiusResult,
    ContractDiffResult,
    SecurityFindingRecord,
    SecurityScanResult,
)
from app.deterministic.risk_engine import (
    blast_radius_norm,
    contract_break_norm,
    critical_service_norm,
    evidence_confidence_norm,
    incident_similarity_norm,
    regression_test_gap_norm,
    security_severity_norm,
)

DOMAINS_ROOT = Path(__file__).resolve().parents[2] / "domains"


@pytest.fixture
def healthcare_pack() -> DomainPack:
    return load_domain_pack("healthcare-claims", domains_root=DOMAINS_ROOT)


class TestBlastRadiusNorm:
    def test_zero_when_empty(self) -> None:
        assert blast_radius_norm(BlastRadiusResult()) == 0.0

    def test_one_when_single_origin_depth_zero(self) -> None:
        result = BlastRadiusResult(
            affected_nodes=[BlastRadiusNode(node_id="svc-a", depth=0, provenance="seed")]
        )
        assert blast_radius_norm(result) == 1.0

    def test_mid_range_depth_weighting(self) -> None:
        result = BlastRadiusResult(
            affected_nodes=[
                BlastRadiusNode(node_id="a", depth=0, provenance="resolved"),
                BlastRadiusNode(node_id="b", depth=2, provenance="heuristic"),
            ]
        )
        assert blast_radius_norm(result) == pytest.approx(0.8)


class TestCriticalServiceNorm:
    def test_zero_when_no_services(self, healthcare_pack: DomainPack) -> None:
        assert critical_service_norm([], healthcare_pack) == 0.0

    def test_one_for_critical_service(self, healthcare_pack: DomainPack) -> None:
        assert critical_service_norm(["svc-core-api"], healthcare_pack) == 1.0

    def test_mid_for_high_tier(self, healthcare_pack: DomainPack) -> None:
        assert critical_service_norm(["svc-notification"], healthcare_pack) == 0.75


class TestContractBreakNorm:
    def test_zero_when_clean(self) -> None:
        assert contract_break_norm(ContractDiffResult()) == 0.0

    def test_one_when_err(self) -> None:
        assert contract_break_norm(ContractDiffResult(err_count=1)) == 1.0

    def test_mid_when_warn_only(self) -> None:
        assert contract_break_norm(ContractDiffResult(warn_count=2)) == 0.6


class TestSecuritySeverityNorm:
    def test_zero_when_no_findings(self) -> None:
        assert security_severity_norm(SecurityScanResult()) == 0.0

    def test_one_for_critical(self) -> None:
        security = SecurityScanResult(
            max_severity="critical",
            findings=[
                SecurityFindingRecord(
                    id="1",
                    scanner="fixture",
                    severity="critical",
                    title="critical finding",
                )
            ],
        )
        assert security_severity_norm(security) == 1.0

    def test_mid_for_medium(self) -> None:
        security = SecurityScanResult(
            max_severity="medium",
            findings=[
                SecurityFindingRecord(
                    id="1",
                    scanner="fixture",
                    severity="medium",
                    title="medium finding",
                )
            ],
        )
        assert security_severity_norm(security) == pytest.approx(0.55)


class TestRegressionTestGapNorm:
    def test_zero_when_no_required_tests(self) -> None:
        assert regression_test_gap_norm([], ["a"]) == 0.0

    def test_one_when_all_missing(self) -> None:
        assert regression_test_gap_norm(["a", "b"], []) == 1.0

    def test_mid_when_half_missing(self) -> None:
        assert regression_test_gap_norm(["a", "b"], ["a"]) == 0.5


class TestIncidentSimilarityNorm:
    def test_neutral_default(self) -> None:
        assert incident_similarity_norm() == 0.0


class TestEvidenceConfidenceNorm:
    def test_zero_by_default(self) -> None:
        assert evidence_confidence_norm() == 0.0

    def test_one_when_fully_unsupported(self) -> None:
        assert evidence_confidence_norm(unsupported_assertion_rate=1.0) == 1.0

    def test_mid_when_partially_unsupported(self) -> None:
        assert evidence_confidence_norm(unsupported_assertion_rate=0.4) == 0.4
