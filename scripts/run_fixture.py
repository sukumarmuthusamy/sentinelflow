#!/usr/bin/env python3
"""Run a domain-pack change fixture through the deterministic pipeline."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from app.deterministic.evaluate import (
    evaluate_change_fixture,
    load_change_fixture_manifest,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOMAINS_ROOT = PROJECT_ROOT / "domains"


CONDITION_LABELS = {
    "oasdiff_err_on_critical_service": "breaking contract on critical service",
    "verified_secret_in_diff": "verified secret exposure",
    "critical_claim_unsupported_after_repair": "critical claim unsupported after repair",
    "evidence_coverage_below_floor": "evidence coverage below floor",
    "dependency_vuln_at_or_above_ceiling": "dependency vulnerability at or above ceiling",
}


def _override_label(condition: str | None) -> str:
    if condition and condition in CONDITION_LABELS:
        return CONDITION_LABELS[condition]
    return (condition or "unknown override").replace("_", " ")


def main() -> int:
    parser = argparse.ArgumentParser(description="Evaluate a SentinelFlow change fixture.")
    parser.add_argument("pack_id", help="Domain pack id, e.g. healthcare-claims")
    parser.add_argument("fixture_id", help="Fixture id, e.g. CHG-H002")
    args = parser.parse_args()

    fixture_dir = DOMAINS_ROOT / args.pack_id / "fixtures" / "changes" / args.fixture_id
    if not fixture_dir.is_dir():
        print(f"Fixture not found: {fixture_dir}", file=sys.stderr)
        return 1

    manifest = load_change_fixture_manifest(fixture_dir)
    result = evaluate_change_fixture(
        fixture_dir,
        pack_id=args.pack_id,
        domains_root=DOMAINS_ROOT,
        fixture_only=True,
    )
    norms = result.gate.norms

    print(f"Fixture: {manifest.change_id}")
    print(f"Purpose: {manifest.description}")
    print()
    print("Normalized terms:")
    print(f"  blast_radius_norm:          {norms.blast_radius_norm:.3f}")
    print(f"  critical_service_norm:      {norms.critical_service_norm:.3f}")
    print(f"  contract_break_norm:        {norms.contract_break_norm:.3f}")
    print(f"  security_severity_norm:     {norms.security_severity_norm:.3f}")
    print(f"  test_gap_norm:              {norms.test_gap_norm:.3f}")
    print(f"  incident_similarity_norm:   {norms.incident_similarity_norm:.3f}")
    print(f"  evidence_confidence_norm:   {norms.evidence_confidence_norm:.3f}")
    print()
    print(f"Risk score: {result.risk_score}")
    print(f"Score-only verdict: {result.gate.score_decision.value}")
    print(f"Policy verdict: {result.decision.value}")

    if result.override_applied:
        label = _override_label(result.override_condition)
        print(f"OVERRIDE FIRED: {label} -> {result.decision.value}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
