"""Evidence queries must be specific to each change, not a constant objective string."""

from __future__ import annotations

from app.agents.queries import evidence_queries_from_signals, security_query_from_signals
from app.deterministic.models import (
    ContractChange,
    ContractDiffResult,
    DeterministicSignals,
    SecurityFindingRecord,
    SecurityScanResult,
)

FALLBACK = "Assess release risk"


def _signals(service: str, *, contract: bool = False, secret: bool = False) -> DeterministicSignals:
    changes = [ContractChange(level="ERR", message="removed field id", path="/items")]
    findings = [
        SecurityFindingRecord(id="F1", scanner="fixture", severity="high", title="AWS key"),
    ]
    return DeterministicSignals(
        change_id="CHG-T",
        primary_service=service,
        impacted_services=[service, "svc-downstream"],
        contract_diff=ContractDiffResult(changes=changes if contract else []),
        security=SecurityScanResult(findings=findings if secret else []),
    )


def test_queries_differ_between_changes() -> None:
    first = evidence_queries_from_signals(_signals("svc-a"), fallback=FALLBACK)
    second = evidence_queries_from_signals(_signals("svc-b", contract=True), fallback=FALLBACK)
    assert first != second
    assert FALLBACK not in first


def test_queries_include_contract_and_security_signals() -> None:
    queries = evidence_queries_from_signals(
        _signals("svc-a", contract=True, secret=True),
        fallback=FALLBACK,
    )
    joined = " | ".join(queries)
    assert "removed field id" in joined
    assert "AWS key" in joined
    assert "svc-downstream" in joined
    assert len(queries) <= 6


def test_fallback_only_without_signals() -> None:
    assert evidence_queries_from_signals(None, fallback=FALLBACK) == [FALLBACK]


def test_security_query_uses_scanner_findings() -> None:
    assert "AWS key" in security_query_from_signals(_signals("svc-a", secret=True), fallback="x")
    assert "svc-a" in security_query_from_signals(_signals("svc-a"), fallback="x")
