"""Test Strategist MCP context: run_tests gets allowlisted test_ids and is always a dry run."""

from __future__ import annotations

import pytest
from app.agents.mcp_tools import mcp_test_strategist_context
from tests.contract.conftest import mcp_session


@pytest.mark.asyncio
async def test_execute_tests_passes_allowlisted_ids_as_dry_run() -> None:
    async with mcp_session() as mcp:
        context = await mcp_test_strategist_context(
            mcp,
            change_id="CHG-H002",
            domain_pack_id="healthcare-claims",
            execute_tests=True,
        )
    run = context["run_tests"]
    assert run["test_ids"] == ["test_claims_contract"]
    assert run["dry_run"] is True
    assert run["status"] == "dry_run"


@pytest.mark.asyncio
async def test_no_execution_unless_requested() -> None:
    async with mcp_session() as mcp:
        context = await mcp_test_strategist_context(
            mcp,
            change_id="CHG-H002",
            domain_pack_id="healthcare-claims",
            execute_tests=False,
        )
    assert "run_tests" not in context


@pytest.mark.asyncio
async def test_no_eligible_tests_means_no_run_call() -> None:
    async with mcp_session() as mcp:
        context = await mcp_test_strategist_context(
            mcp,
            change_id="CHG-H001",
            domain_pack_id="healthcare-claims",
            execute_tests=True,
        )
    assert "run_tests" not in context
