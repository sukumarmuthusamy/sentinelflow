"""Evaluation suites the API can trigger.

M5-A exposes the suite that runs offline: the M1 deterministic fixture evaluation
(``app.deterministic.evaluate``). It scores every change fixture with the risk engine
and policy gate on recorded signals and compares the result with the fixture's
labelled ``expected_decision``. It makes no LLM, network or container calls.

The graph sample (``scripts/run_m4_sample.py``) and the retrieval matrix
(``evals/retrieval``) need live providers and pgvector. They are not exposed over
HTTP in M5-A; M6 owns the evaluation harness.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from app.core.domain_pack import load_domain_pack
from app.deterministic.evaluate import (
    evaluate_change_fixture,
    list_change_fixtures,
    load_change_fixture_manifest,
)


class EvaluationSuite(StrEnum):
    DETERMINISTIC_FIXTURES = "deterministic_fixtures"


class FixtureEvaluationRow(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    domain_pack_id: str
    change_id: str
    decision: str
    expected_decision: str | None
    matches_expected: bool | None
    risk_score: int
    override_applied: str | None


class EvaluationSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fixtures: int
    labelled: int
    matched: int
    # matched / labelled; None when nothing is labelled (never reported as 1.0).
    decision_accuracy: float | None = Field(default=None, ge=0.0, le=1.0)


class EvaluationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite: EvaluationSuite
    domain_pack_ids: list[str]
    summary: EvaluationSummary
    rows: list[FixtureEvaluationRow]


EvaluationRunner = Callable[[EvaluationSuite, Sequence[str], Path], EvaluationResult]


def run_deterministic_fixture_suite(
    suite: EvaluationSuite,
    pack_ids: Sequence[str],
    domains_root: Path,
) -> EvaluationResult:
    """Blocking; the job service runs it in a worker thread."""
    rows: list[FixtureEvaluationRow] = []
    for pack_id in pack_ids:
        pack = load_domain_pack(pack_id, domains_root=domains_root)
        for fixture_dir in list_change_fixtures(domains_root / pack_id):
            manifest = load_change_fixture_manifest(fixture_dir)
            result = evaluate_change_fixture(fixture_dir, domain_pack=pack, fixture_only=True)
            expected = manifest.expected_decision.value if manifest.expected_decision else None
            rows.append(
                FixtureEvaluationRow(
                    domain_pack_id=pack_id,
                    change_id=result.change_id,
                    decision=result.decision.value,
                    expected_decision=expected,
                    matches_expected=(result.decision.value == expected) if expected else None,
                    risk_score=result.risk_score,
                    override_applied=result.override_applied,
                ),
            )
    labelled = [row for row in rows if row.matches_expected is not None]
    matched = sum(1 for row in labelled if row.matches_expected)
    return EvaluationResult(
        suite=suite,
        domain_pack_ids=list(pack_ids),
        summary=EvaluationSummary(
            fixtures=len(rows),
            labelled=len(labelled),
            matched=matched,
            decision_accuracy=(matched / len(labelled)) if labelled else None,
        ),
        rows=rows,
    )
