"""Run the M4 graph on the small scenario sample (requires GEMINI_API_KEY + pgvector).

Free-tier discipline: one shared pacer spaces every generation call (parallel agents
included), SDK retries are off so one paced call is one HTTP attempt, and the run
stops at the first daily-quota error instead of burning the next scenario.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.settings import get_settings
from app.deterministic.evaluate import load_change_fixture_manifest, resolve_fixture_directory
from app.graph.evidence_ids import evidence_id
from app.graph.runner import resume_analysis, run_analysis
from app.graph.state import AnalysisRequest
from app.mcp_client import SentinelFlowMcpAdapter
from app.model_gateway.errors import is_daily_quota_exhausted
from app.model_gateway.llm import (
    GeminiStructuredLLM,
    OpenRouterStructuredLLM,
    RoutingStructuredLLM,
    StructuredLLM,
)
from app.model_gateway.pacing import AsyncMinIntervalPacer, min_interval_for_rpm
from mcp_server.sentinelflow_context.server import mcp


class DailyQuotaExhaustedError(Exception):
    """Stop the sample: retrying today will not help."""


def _load_scenarios(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _citation_validity(state: dict[str, Any]) -> tuple[int, list[str]]:
    """Citations surviving in the final state that are not in the retrieved set."""
    allowed = set(state.get("retrieved_evidence_ids") or [])
    invalid: list[str] = []
    total = 0
    evidence = state.get("evidence") or {}
    for item in evidence.get("items") or []:
        citation = item.get("citation") or {}
        total += 1
        eid = evidence_id(str(citation.get("document_id", "")), str(citation.get("section_id", "")))
        if eid not in allowed:
            invalid.append(eid)
    verification = state.get("verification") or {}
    for assessment in verification.get("assertion_assessments") or []:
        for eid in assessment.get("evidence_ids") or []:
            total += 1
            if eid not in allowed:
                invalid.append(str(eid))
    security = state.get("security_findings") or {}
    for finding in security.get("findings") or []:
        for eid in finding.get("evidence_ids") or []:
            total += 1
            if eid not in allowed:
                invalid.append(str(eid))
    return total, invalid


def _expected_decision(request: AnalysisRequest) -> str | None:
    settings = get_settings()
    fixture = resolve_fixture_directory(
        request.change_id,
        pack_id=request.domain_pack_id,
        domains_root=settings.domains_root,
    )
    expected = load_change_fixture_manifest(fixture).expected_decision
    return str(expected.value) if expected is not None else None


async def _run_one(row: dict[str, Any], *, llm: StructuredLLM) -> dict[str, Any]:
    request = AnalysisRequest(
        change_id=str(row["change_id"]),
        domain_pack_id=str(row["domain_pack_id"]),
        record_decision=bool(row.get("record_decision", False)),
    )
    # Unique per run: reusing a thread would merge reducer fields from the prior run.
    thread_id = f"m4-{request.change_id}-{uuid.uuid4().hex[:8]}"
    started = time.monotonic()
    interrupted = False
    async with SentinelFlowMcpAdapter(mcp) as adapter:
        try:
            state = await run_analysis(
                request,
                mcp=adapter,
                llm=llm,
                thread_id=thread_id,
                enable_retrieval=True,
            )
            if state.get("__interrupt__"):
                interrupted = True
                state = await resume_analysis(
                    mcp=adapter,
                    llm=llm,
                    thread_id=thread_id,
                    request=request,
                    resume_payload={"approved": True},
                    enable_retrieval=True,
                )
        except Exception as exc:  # noqa: BLE001 — report, never crash the sample
            if is_daily_quota_exhausted(exc):
                raise DailyQuotaExhaustedError(str(exc)[:300]) from exc
            return {
                "change_id": request.change_id,
                "status": "failed",
                "error": f"{type(exc).__name__}: {str(exc)[:300]}",
                "elapsed_seconds": round(time.monotonic() - started, 1),
            }

    total, invalid = _citation_validity(state)
    metrics = state.get("verification_metrics") or {}
    expected = _expected_decision(request)
    decision = (state.get("risk_assessment") or {}).get("decision")
    return {
        "change_id": request.change_id,
        "domain_pack_id": request.domain_pack_id,
        "status": "completed",
        "decision": decision,
        "expected_decision": expected,
        "decision_matches_fixture": decision == expected if expected else None,
        "plan_agents": (state.get("plan") or {}).get("agents"),
        "plan_category": (state.get("plan") or {}).get("change_category"),
        "agent_status": {
            row["agent"]: row["status"] for row in state.get("parallel_completions") or []
        },
        "citations_in_final_state": total,
        "invalid_citations_after_filter": invalid,
        "invented_ids_before_filter": list(state.get("rejected_evidence_ids") or []),
        "material_assertions": len(metrics.get("assertions") or []),
        "evidence_coverage": metrics.get("evidence_coverage"),
        "llm_reported_evidence_coverage": metrics.get("llm_reported_evidence_coverage"),
        "repair_count": state.get("repair_count"),
        "degraded_to_review": state.get("degraded_to_review"),
        "interrupt_paused_and_resumed": interrupted,
        "record_decision_written": state.get("record_decision_written"),
        "trace_metrics": state.get("trace_metrics"),
        "errors": state.get("errors"),
        "elapsed_seconds": round(time.monotonic() - started, 1),
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Run M4 sample scenarios")
    parser.add_argument(
        "--scenarios",
        type=Path,
        default=Path("evals/graph/sample_scenarios.jsonl"),
    )
    parser.add_argument("--only", action="append", help="change_id to run (repeatable)")
    parser.add_argument("--rpm", type=int, default=5, help="Gemini requests per minute")
    parser.add_argument(
        "--agent-model",
        help="override AGENT_LLM_MODEL; a vendor/model slug routes to OpenRouter",
    )
    parser.add_argument("--triage-model", help="override TRIAGE_LLM_MODEL")
    parser.add_argument(
        "--transient-retries",
        type=int,
        default=3,
        help="retries per call on 503/5xx only (waits 20s, 40s, 60s)",
    )
    parser.add_argument("--out", type=Path, default=Path("evals/graph/m4_sample_results.json"))
    args = parser.parse_args()

    # The graph reads models from Settings; overrides go through the env + cache reset.
    if args.agent_model:
        os.environ["AGENT_LLM_MODEL"] = args.agent_model
    if args.triage_model:
        os.environ["TRIAGE_LLM_MODEL"] = args.triage_model
    get_settings.cache_clear()
    settings = get_settings()

    # One pacer per provider: each has its own RPM limit.
    gemini = GeminiStructuredLLM(
        client_max_retries=0,
        pacer=AsyncMinIntervalPacer(min_interval_for_rpm(args.rpm)),
        transient_retries=args.transient_retries,
    )
    openrouter = None
    if settings.openrouter_api_key is not None:
        openrouter = OpenRouterStructuredLLM(
            client_max_retries=0,
            pacer=AsyncMinIntervalPacer(min_interval_for_rpm(settings.openrouter_rpm)),
            transient_retries=args.transient_retries,
        )
    llm = RoutingStructuredLLM(gemini=gemini, openrouter=openrouter)
    rows = _load_scenarios(args.scenarios)
    if args.only:
        rows = [row for row in rows if row["change_id"] in set(args.only)]

    results: list[dict[str, Any]] = []
    stopped_reason: str | None = None
    for row in rows:
        try:
            result = await _run_one(row, llm=llm)
        except DailyQuotaExhaustedError as exc:
            stopped_reason = f"daily_quota_exhausted at {row['change_id']}: {exc}"
            print(stopped_reason, file=sys.stderr)
            break
        results.append(result)
        print(
            json.dumps(
                {
                    k: result.get(k)
                    for k in (
                        "change_id",
                        "status",
                        "decision",
                        "expected_decision",
                        "invalid_citations_after_filter",
                        "invented_ids_before_filter",
                        "evidence_coverage",
                        "degraded_to_review",
                        "elapsed_seconds",
                        "error",
                    )
                }
            )
        )

    payload = {
        "meta": {
            "run_at_utc": datetime.now(UTC).isoformat(timespec="seconds"),
            "agent_model": settings.agent_llm_model,
            "triage_model": settings.triage_llm_model,
            "agent_temperature": settings.agent_llm_temperature,
            "rpm": args.rpm,
            "stopped_reason": stopped_reason,
        },
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"wrote {args.out}")
    return 1 if stopped_reason else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
