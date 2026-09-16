"""Shared fixtures for MCP contract tests."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from app.mcp_client import SentinelFlowMcpAdapter
from mcp_server.sentinelflow_context.server import mcp
from mcp_server.sentinelflow_context.settings import clear_mcp_server_settings_cache

DOMAINS_ROOT = Path(__file__).resolve().parents[2] / "domains"


@asynccontextmanager
async def mcp_session() -> AsyncIterator[SentinelFlowMcpAdapter]:
    """Open a per-test MCP adapter session without pytest async fixtures."""
    async with SentinelFlowMcpAdapter(mcp) as adapter:
        yield adapter


@pytest.fixture
def audit_log_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated audit log path for record_gate_decision tests."""
    path = tmp_path / "gate_decisions.jsonl"
    monkeypatch.setenv("SENTINELFLOW_AUDIT_LOG_PATH", str(path))
    clear_mcp_server_settings_cache()
    yield path
    clear_mcp_server_settings_cache()
