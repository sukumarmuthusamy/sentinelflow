"""Deterministic retrieval queries derived from a change's signals.

The Evidence Investigator and Security Auditor run in the same parallel superstep
as the Change Analyst, so they cannot see its hypotheses. Their queries come from
the deterministic signal layer instead, which is computed before fan-out and is
specific to each change.
"""

from __future__ import annotations

from app.deterministic.models import DeterministicSignals

MAX_EVIDENCE_QUERIES = 6


def _dedupe(queries: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for query in queries:
        normalized = " ".join(query.split())
        if normalized and normalized.lower() not in seen:
            seen.add(normalized.lower())
            ordered.append(normalized)
    return ordered[:limit]


def evidence_queries_from_signals(
    signals: DeterministicSignals | None,
    *,
    fallback: str,
    limit: int = MAX_EVIDENCE_QUERIES,
) -> list[str]:
    """Build change-specific evidence queries; fall back only when there are no signals."""
    if signals is None:
        return [fallback]
    service = signals.primary_service
    queries = [f"{service} requirements, ownership and regression tests"]
    for change in signals.contract_diff.changes[:2]:
        location = f" at {change.path}" if change.path else ""
        queries.append(f"{service} API contract change{location}: {change.message}")
    for finding in signals.security.findings[:2]:
        queries.append(f"{service} security finding: {finding.title}")
    downstream = [name for name in signals.impacted_services if name != service]
    if downstream:
        queries.append(f"{service} change impact on {', '.join(downstream[:3])}")
    queries.append(f"prior incidents and runbooks involving {service}")
    return _dedupe(queries, limit) or [fallback]


def security_query_from_signals(signals: DeterministicSignals | None, *, fallback: str) -> str:
    """One security-scoped query grounded in scanner output for this change."""
    if signals is None:
        return fallback
    service = signals.primary_service
    titles = [finding.title for finding in signals.security.findings[:3]]
    if titles:
        return f"{service} security exposure: {'; '.join(titles)}"
    return f"{service} authentication, authorization, secrets and data exposure policy"
