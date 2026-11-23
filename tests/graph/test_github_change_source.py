"""Fixture vs GitHub PR through the same MCP interface (M2 exit criterion), offline."""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import pytest
from app.agents.observable import runtime_signals
from app.core.domain_pack import load_domain_pack
from app.deterministic.contract_diff import parse_oasdiff_output
from app.deterministic.evaluate import (
    build_signals_from_fixture,
    list_change_fixtures,
    load_change_fixture_manifest,
)
from app.deterministic.models import DeterministicSignals
from app.deterministic.policy_gate import apply_policy_gate
from app.deterministic.risk_engine import compute_risk_score
from app.graph.change_signals import signals_via_mcp
from app.mcp_client.errors import ChangeSourceError
from app.mcp_client.port import McpClientPort
from app.mcp_client.source_binding import (
    CHANGE_SCOPED_TOOLS,
    SourceBoundMcpClient,
    bind_change_source,
)
from app.model_gateway.llm import StaticStructuredLLM
from pydantic import BaseModel
from tests.contract.conftest import mcp_session
from tests.contract.github_fakes import SCRIPTED_OASDIFF_ERR, FakeSignalTools, github_offline
from tests.graph.conftest import RunGraph, static_llm_responses

DOMAINS_ROOT = Path(__file__).resolve().parents[2] / "domains"
HC = "healthcare-claims"

# Fields that legitimately differ between a fixture and its PR: the id, and how the
# contract verdict was obtained (recorded fixture signal vs. a computed result).
SOURCE_SPECIFIC = {"change_id", "contract_diff"}


def _decide(signals: DeterministicSignals, pack_id: str) -> tuple[str, int, str | None]:
    pack = load_domain_pack(pack_id)
    gate = apply_policy_gate(signals, pack, compute_risk_score(signals, pack))
    return gate.decision.value, gate.risk_score, gate.override_applied


async def _signals(change_id: str, source: str, pack_id: str = HC) -> Any:
    async with mcp_session() as adapter:
        client = bind_change_source(adapter, source)  # type: ignore[arg-type]
        return await signals_via_mcp(client, change_id=change_id, domain_pack_id=pack_id)


ALL_FIXTURES = [
    (pack_dir.name, fixture.name)
    for pack_dir in sorted(DOMAINS_ROOT.iterdir())
    if pack_dir.is_dir()
    for fixture in list_change_fixtures(pack_dir)
]


@pytest.mark.parametrize(("pack_id", "change_id"), ALL_FIXTURES)
async def test_mcp_signals_equal_disk_signals_for_every_fixture(
    pack_id: str, change_id: str
) -> None:
    """The MCP composition used for live sources reproduces the fixture path exactly."""
    pack = load_domain_pack(pack_id)
    fixture_dir = DOMAINS_ROOT / pack_id / "fixtures" / "changes" / change_id
    manifest = load_change_fixture_manifest(fixture_dir)
    disk = runtime_signals(
        build_signals_from_fixture(fixture_dir, manifest, pack, fixture_only=True)
    )
    via_mcp = await _signals(change_id, "fixture", pack_id)
    assert via_mcp.signals == disk
    assert via_mcp.gaps == []


async def test_pr1_matches_fixture_h001_signals_and_decision() -> None:
    fixture = await _signals("CHG-H001", "fixture")
    with github_offline():
        github = await _signals("PR-1", "github")
    assert github.gaps == []
    assert github.changed_files == fixture.changed_files
    assert github.signals.model_dump(exclude=SOURCE_SPECIFIC) == fixture.signals.model_dump(
        exclude=SOURCE_SPECIFIC
    )
    assert github.signals.contract_diff.tool == "not_applicable"
    assert github.signals.contract_diff.err_count == fixture.signals.contract_diff.err_count == 0
    assert _decide(github.signals, HC) == _decide(fixture.signals, HC) == ("PASS", 10, None)


async def test_pr7_matches_fixture_h002_decision_with_scripted_oasdiff() -> None:
    """Decision-level equivalence; oasdiff output is scripted, not a real oasdiff run."""
    fixture = await _signals("CHG-H002", "fixture")
    tools = FakeSignalTools(contract=parse_oasdiff_output(SCRIPTED_OASDIFF_ERR))
    with github_offline(tools=tools):
        github = await _signals("PR-7", "github")
    assert github.gaps == []
    assert github.signals.primary_service == fixture.signals.primary_service == "svc-core-api"
    assert github.signals.blast_radius == fixture.signals.blast_radius
    assert github.signals.impacted_services == fixture.signals.impacted_services
    assert github.signals.contract_diff.breaking and fixture.signals.contract_diff.breaking
    github_decision = _decide(github.signals, HC)
    fixture_decision = _decide(fixture.signals, HC)
    assert github_decision[0] == fixture_decision[0] == "BLOCK"
    assert github_decision[2] == fixture_decision[2] == "critical-contract-break"


def _decision(result: dict[str, Any]) -> tuple[str, int]:
    assessment = result["risk_assessment"]
    return assessment["decision"], assessment["risk_score"]


async def test_full_graph_pr1_equals_fixture_h001(run_graph: RunGraph) -> None:
    fixture_llm = StaticStructuredLLM(static_llm_responses())
    fixture = await run_graph("CHG-H001", fixture_llm)
    github_llm = StaticStructuredLLM(static_llm_responses())
    with github_offline():
        github = await run_graph("PR-1", github_llm, source="github")
    assert not github.get("degraded_to_review")
    assert _decision(github) == _decision(fixture)
    assert github["changed_files"] == fixture["changed_files"]
    assert [schema for schema, _, _ in github_llm.prompts] == [
        schema for schema, _, _ in fixture_llm.prompts
    ]
    analyst = next(user for schema, _, user in github_llm.prompts if schema == "ChangeImpact")
    assert '"source": "github"' in analyst
    for _, system, user in github_llm.prompts:
        assert "docs: expand analytics documentation" not in system + user  # PR title
        assert "Synthetic demo PR" not in system + user  # PR body


async def test_incomplete_data_degrades_before_any_llm_call(run_graph: RunGraph) -> None:
    tools = FakeSignalTools()
    tools.scanner_unavailable = True
    llm = StaticStructuredLLM(static_llm_responses())
    with github_offline(tools=tools):
        result = await run_graph("PR-1", llm, source="github")
    assert result["degraded_to_review"] is True
    assert result["risk_assessment"]["decision"] == "REVIEW"
    assert result["risk_assessment"]["override_applied"] == "incomplete_change_data"
    assert any("gitleaks unavailable" in error for error in result["errors"])
    assert llm.prompts == []


async def test_incomplete_data_never_weakens_a_deterministic_block(run_graph: RunGraph) -> None:
    tools = FakeSignalTools(contract=parse_oasdiff_output(SCRIPTED_OASDIFF_ERR))
    tools.scanner_unavailable = True
    llm = StaticStructuredLLM(static_llm_responses())
    with github_offline(tools=tools):
        result = await run_graph("PR-7", llm, source="github")
    assert result["degraded_to_review"] is True
    assert result["risk_assessment"]["decision"] == "BLOCK"


async def test_missing_pr_stops_the_run_without_retrying(run_graph: RunGraph) -> None:
    llm = StaticStructuredLLM(static_llm_responses())
    # The in-process MCP session wraps the error in an ExceptionGroup on its way out.
    with github_offline() as (fake, _), pytest.raises(BaseExceptionGroup) as caught:
        await run_graph("PR-404", llm, source="github")
    assert caught.group_contains(ChangeSourceError, match="not found")
    assert len(fake.requests) == 1
    assert llm.prompts == []


class _RecordingMcp:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool_typed(self, name: str, arguments: Any, *, response_model: Any) -> Any:
        self.calls.append((name, dict(arguments or {})))
        return None


async def test_source_binding_pins_change_tools_only() -> None:
    recorder = _RecordingMcp()
    inner = cast(McpClientPort, recorder)
    assert bind_change_source(inner, "fixture") is inner
    bound = SourceBoundMcpClient(inner, source="github")
    args = {"change_id": "PR-1", "source": "fixture"}
    for tool in sorted(CHANGE_SCOPED_TOOLS):
        await bound.call_tool_typed(tool, args, response_model=BaseModel)
    await bound.call_tool_typed(
        "record_gate_decision", {"analysis_id": "a"}, response_model=BaseModel
    )
    for name, sent in recorder.calls:
        if name in CHANGE_SCOPED_TOOLS:
            assert sent["source"] == "github", name
        else:
            assert "source" not in sent
