"""Append-only gate decision audit log with idempotency."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from app.graph.state import ReleaseDecision

from mcp_server.sentinelflow_context.models import RecordGateDecisionResponse


def _load_entries(path: Path) -> list[dict[str, object]]:
    if not path.is_file():
        return []
    entries: list[dict[str, object]] = []
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            payload = json.loads(line)
            if isinstance(payload, dict):
                entries.append(payload)
    return entries


def record_gate_decision(
    *,
    audit_log_path: Path,
    analysis_id: str,
    decision: ReleaseDecision,
    evidence_hash: str,
    idempotency_key: str,
) -> RecordGateDecisionResponse:
    """Append a gate decision unless the idempotency key already exists."""
    audit_log_path.parent.mkdir(parents=True, exist_ok=True)
    existing = _load_entries(audit_log_path)
    for entry in existing:
        if entry.get("idempotency_key") == idempotency_key:
            return RecordGateDecisionResponse(
                recorded=False,
                duplicate=True,
                analysis_id=analysis_id,
                decision=decision,
                idempotency_key=idempotency_key,
            )

    record = {
        "idempotency_key": idempotency_key,
        "analysis_id": analysis_id,
        "decision": decision.value,
        "evidence_hash": evidence_hash,
        "recorded_at": datetime.now(tz=UTC).isoformat(),
    }
    with audit_log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record) + "\n")

    return RecordGateDecisionResponse(
        recorded=True,
        duplicate=False,
        analysis_id=analysis_id,
        decision=decision,
        idempotency_key=idempotency_key,
    )
