"""The Triage Planner sees observable change facts, never the fixture's labels."""

from __future__ import annotations

from pathlib import Path

from app.core.domain_pack import load_domain_pack
from app.deterministic.evaluate import build_signals_from_fixture, load_change_fixture_manifest
from app.graph.nodes import triage_change_summary

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "domains"
    / "healthcare-claims"
    / "fixtures"
    / "changes"
    / "CHG-H002"
)


def test_summary_has_facts_but_no_labels() -> None:
    manifest = load_change_fixture_manifest(FIXTURE)
    pack = load_domain_pack("healthcare-claims", domains_root=FIXTURE.parents[3])
    signals = build_signals_from_fixture(FIXTURE, manifest, pack, fixture_only=True)

    summary = triage_change_summary(signals, manifest.changed_files)

    assert "contract_diff: breaking=True" in summary
    for path in manifest.changed_files:
        assert path in summary
    assert manifest.description not in summary
    assert manifest.category not in summary
    assert str(manifest.expected_decision) not in summary
