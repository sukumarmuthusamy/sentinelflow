"""Shared evidence state: deduplicating reducer, bounded verifier view, citation filters."""

from __future__ import annotations

from app.agents.contracts import SecurityFinding
from app.graph.evidence_context import (
    build_verifier_evidence,
    chunk_records,
    merge_unique_chunks,
)
from app.graph.evidence_ids import filter_security_findings

from tests.graph.fakes import make_chunk

A = chunk_records([make_chunk("doc-a", "s1", "alpha text")], source="evidence_investigator")
B = chunk_records([make_chunk("doc-b", "s1", "beta text")], source="security_auditor")
A_AGAIN = chunk_records([make_chunk("doc-a", "s1", "alpha text")], source="repair")


def test_reducer_unions_branches_without_loss() -> None:
    merged = merge_unique_chunks(A, B)
    assert [record["evidence_id"] for record in merged] == ["doc-a::s1", "doc-b::s1"]


def test_reducer_is_independent_of_branch_order() -> None:
    assert merge_unique_chunks([], A + B) == merge_unique_chunks([], B + A)
    assert merge_unique_chunks(A, B) == merge_unique_chunks(B, A)


def test_reducer_dedupes_and_keeps_first_writer() -> None:
    merged = merge_unique_chunks(A, A_AGAIN)
    assert len(merged) == 1
    assert merged[0]["source"] == "evidence_investigator"


def test_verifier_view_is_bounded_and_records_withheld() -> None:
    records = chunk_records(
        [make_chunk(f"doc-{i:02d}", "s1", "x" * 100) for i in range(10)],
        source="evidence_investigator",
    )
    view = build_verifier_evidence(
        records,
        priority_ids={"doc-09::s1"},
        max_chunks=3,
        max_chunk_chars=50,
        max_total_chars=10_000,
    )
    # Specialist-cited evidence is shown first; the rest fill the remaining slots.
    assert [chunk.evidence_id for chunk in view.shown] == ["doc-09::s1", "doc-00::s1", "doc-01::s1"]
    assert all(len(chunk.content) == 50 for chunk in view.shown)
    assert len(view.withheld_ids) == 7
    assert view.shown_ids.isdisjoint(view.withheld_ids)


def test_verifier_view_total_character_cap() -> None:
    records = chunk_records(
        [make_chunk(f"doc-{i}", "s1", "y" * 400) for i in range(5)],
        source="repair",
    )
    view = build_verifier_evidence(records, priority_ids=set(), max_total_chars=1_000)
    assert len(view.shown) == 2
    assert sum(len(chunk.content) for chunk in view.shown) <= 1_000


def test_invalidly_cited_security_finding_is_kept() -> None:
    finding = SecurityFinding(
        id="S1",
        category="secret_exposure",
        severity="critical",
        title="t",
        description="d",
        evidence_ids=["ghost::1", "doc-a::s1"],
    )
    only_invalid = finding.model_copy(update={"id": "S2", "evidence_ids": ["ghost::2"]})
    kept, rejected = filter_security_findings([finding, only_invalid], {"doc-a::s1"})
    assert [f.id for f in kept] == ["S1", "S2"]
    assert kept[0].evidence_ids == ["doc-a::s1"]
    assert kept[1].evidence_ids == []
    assert rejected == ["ghost::1", "ghost::2"]
