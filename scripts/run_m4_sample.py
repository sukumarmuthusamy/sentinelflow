"""Run the M4 graph on the small scenario sample (requires GEMINI_API_KEY)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.graph.evidence_ids import evidence_id
from app.graph.runner import resume_analysis, run_analysis
from app.graph.state import AnalysisRequest
from app.mcp_client import SentinelFlowMcpAdapter
from app.model_gateway.llm import GeminiStructuredLLM
from mcp_server.sentinelflow_context.server import mcp


def _load_scenarios(path: Path) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _citation_validity(state: dict[str, object]) -> tuple[int, int, list[str]]:
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
    return total, len(invalid), invalid


async def _run_one(
    row: dict[str, object],
    *,
    llm: GeminiStructuredLLM,
) -> None:
    request = AnalysisRequest(
        change_id=str(row["change_id"]),
        domain_pack_id=str(row["domain_pack_id"]),
        record_decision=bool(row.get("record_decision", False)),
    )
    thread_id = f"m4-{request.change_id}"
    async with SentinelFlowMcpAdapter(mcp) as adapter:
        try:
            state = await run_analysis(
                request,
                mcp=adapter,
                llm=llm,
                thread_id=thread_id,
                enable_retrieval=True,
            )
        except Exception as exc:  # noqa: BLE001
            print(f"{request.change_id}: failed during run: {exc}", file=sys.stderr)
            return

        if state.get("__interrupt__"):
            state = await resume_analysis(
                mcp=adapter,
                llm=llm,
                thread_id=thread_id,
                request=request,
                resume_payload={"approved": True},
                enable_retrieval=True,
            )

    total, invalid_count, invalid = _citation_validity(state)
    decision = (state.get("risk_assessment") or {}).get("decision")
    print(
        f"{request.change_id} decision={decision} citations={total} invalid={invalid_count}",
    )
    if invalid:
        print(f"  invalid ids: {invalid[:5]}", file=sys.stderr)


async def main() -> None:
    parser = argparse.ArgumentParser(description="Run M4 sample scenarios")
    parser.add_argument(
        "--scenarios",
        type=Path,
        default=Path("evals/graph/sample_scenarios.jsonl"),
    )
    args = parser.parse_args()
    llm = GeminiStructuredLLM()
    for row in _load_scenarios(args.scenarios):
        await _run_one(row, llm=llm)


if __name__ == "__main__":
    asyncio.run(main())
