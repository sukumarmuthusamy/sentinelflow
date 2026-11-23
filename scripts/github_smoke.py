"""Read-only live smoke test of the GitHub change source (deterministic, no LLM calls).

For each fixture / seed-PR pair, both sides go through the same MCP tools, and the
deterministic gate is applied to each. If the PR's data has gaps, the decision gets the
same REVIEW floor the graph applies. Only GET requests are made. A token is optional
(SENTINELFLOW_GITHUB_TOKEN) and is never printed.

    uv run python scripts/github_smoke.py --output evals/github/m2_live_smoke.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.domain_pack import load_domain_pack  # noqa: E402
from app.deterministic.policy_gate import apply_policy_gate, strictest_decision  # noqa: E402
from app.deterministic.risk_engine import compute_risk_score  # noqa: E402
from app.graph.change_signals import McpChangeSignals, signals_via_mcp  # noqa: E402
from app.graph.state import ReleaseDecision  # noqa: E402
from app.mcp_client import SentinelFlowMcpAdapter  # noqa: E402
from app.mcp_client.source_binding import bind_change_source  # noqa: E402
from mcp_server.sentinelflow_context.models import ChangePackageResponse  # noqa: E402
from mcp_server.sentinelflow_context.server import mcp as server  # noqa: E402

# (pack, fixture, seed PR) pairs named as equivalents in the seed repo's ground truth.
PAIRS = [
    ("healthcare-claims", "CHG-H001", "PR-1"),
    ("healthcare-claims", "CHG-H002", "PR-7"),
    ("healthcare-claims", "CHG-H003", "PR-10"),
    ("ecommerce-payments", "CHG-E001", "PR-2"),
    ("ecommerce-payments", "CHG-E002", "PR-8"),
]
COMPARED = ("primary_service", "impacted_services", "blast_radius")


def _gate(collected: McpChangeSignals, pack_id: str) -> dict[str, Any]:
    pack = load_domain_pack(pack_id)
    signals = collected.signals
    gate = apply_policy_gate(signals, pack, compute_risk_score(signals, pack))
    final = gate.decision
    if collected.gaps:
        final = strictest_decision(final, ReleaseDecision.REVIEW)
    return {
        "computed_decision": gate.decision.value,
        "decision": final.value,
        "risk_score": gate.risk_score,
        "override_applied": gate.override_applied,
        "contract_tool": signals.contract_diff.tool,
        "contract_err_count": signals.contract_diff.err_count,
        "secret_findings": len(signals.security.findings),
    }


async def _pair(adapter: SentinelFlowMcpAdapter, pack_id: str, fixture_id: str, pr_id: str) -> Any:
    fixture = await signals_via_mcp(adapter, change_id=fixture_id, domain_pack_id=pack_id)
    github_client = bind_change_source(adapter, "github")
    try:
        github = await signals_via_mcp(github_client, change_id=pr_id, domain_pack_id=pack_id)
        package = await github_client.call_tool_typed(
            "get_change_package",
            {"change_id": pr_id, "domain_pack_id": pack_id},
            response_model=ChangePackageResponse,
        )
    except Exception as exc:  # noqa: BLE001 - report the source failure and stop this pair
        return {"pack": pack_id, "fixture": fixture_id, "pull_request": pr_id, "error": str(exc)}
    fixture_view = fixture.signals.model_dump(mode="json", include=set(COMPARED))
    github_view = github.signals.model_dump(mode="json", include=set(COMPARED))
    ref = package.github
    return {
        "pack": pack_id,
        "fixture": {
            "change_id": fixture_id,
            "changed_files": fixture.changed_files,
            **fixture_view,
            **_gate(fixture, pack_id),
        },
        "pull_request": {
            "change_id": pr_id,
            "head_sha": ref.head_sha if ref else None,
            "base_sha": ref.base_sha if ref else None,
            "changed_files": github.changed_files,
            "origin_nodes": package.origin_nodes,
            **github_view,
            **_gate(github, pack_id),
            "gaps": github.gaps,
            "notes": github.notes,
        },
        "same": {key: fixture_view[key] == github_view[key] for key in COMPARED},
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()
    if args.output.exists() and not args.overwrite:
        print(f"{args.output} exists; pass --overwrite to replace it", file=sys.stderr)
        return 2
    rows = []
    async with SentinelFlowMcpAdapter(server) as adapter:
        for pack_id, fixture_id, pr_id in PAIRS:
            row = await _pair(adapter, pack_id, fixture_id, pr_id)
            rows.append(row)
            if "error" in row:
                print(f"{pr_id}: ERROR {row['error']}")
                continue
            fx, pr = row["fixture"], row["pull_request"]
            print(
                f"{fixture_id} {fx['decision']}({fx['risk_score']}) | {pr_id} "
                f"computed={pr['computed_decision']}({pr['risk_score']}) final={pr['decision']} "
                f"gaps={len(pr['gaps'])} same={row['same']}",
            )
    report = {
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "mode": "read-only GitHub REST, deterministic signals only, no LLM calls",
        "pairs": rows,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
