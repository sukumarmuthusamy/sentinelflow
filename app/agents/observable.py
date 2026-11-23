"""What agents may observe about a change: facts, never fixture labels.

Fixture manifests mix two kinds of data. Observable change facts (changed files,
services, scanner and contract-diff output) are legitimate agent input. Benchmark
annotations are not: ``description`` and ``category`` are evaluator notes that
state the expected outcome ("Breaking payment API contract on the critical payment
service"), ``expected_decision`` is the label itself, and ``evidence_coverage``,
``repair_attempted`` and ``critical_claims_unsupported`` are synthetic stand-ins for
results the M1 deterministic tests needed before any verifier existed. At runtime
those last three must be *measured* by the graph, so they are cleared and hidden.
"""

from __future__ import annotations

from typing import Any

from mcp_server.sentinelflow_context.models import ChangePackageResponse

from app.deterministic.models import DeterministicSignals

# Change-package fields an agent may see. Everything else is a fixture annotation.
OBSERVABLE_PACKAGE_FIELDS: frozenset[str] = frozenset(
    {"change_id", "source", "domain_pack_id", "primary_service", "changed_files", "origin_nodes"},
)

# Signal fields that are verification *results*; at runtime the graph computes them.
VERIFICATION_RESULT_FIELDS: frozenset[str] = frozenset(
    {
        "evidence_coverage",
        "unsupported_assertion_rate",
        "critical_claims_unsupported",
        "repair_attempted",
    },
)


def observable_change_package(package: ChangePackageResponse) -> dict[str, Any]:
    return package.model_dump(include=set(OBSERVABLE_PACKAGE_FIELDS))


def runtime_signals(signals: DeterministicSignals) -> DeterministicSignals:
    """Reset verification-result fields: in a live analysis nothing is verified yet."""
    return signals.model_copy(
        update={
            "evidence_coverage": None,
            "unsupported_assertion_rate": None,
            "critical_claims_unsupported": False,
            "repair_attempted": False,
        },
    )


def agent_signal_view(signals: DeterministicSignals | None) -> dict[str, Any]:
    """Deterministic tool outputs for an agent prompt, without verification fields."""
    if signals is None:
        return {}
    return signals.model_dump(mode="json", exclude=set(VERIFICATION_RESULT_FIELDS))
