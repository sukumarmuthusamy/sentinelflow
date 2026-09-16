"""Contract tests for sentinelflow-context MCP tools."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from app.mcp_client import McpToolError, SentinelFlowMcpAdapter
from mcp_server.sentinelflow_context.models import (
    ChangePackageResponse,
    ChangeTestInventory,
    ContractDiffToolResponse,
    DependencyGraphResponse,
    RecordGateDecisionResponse,
    RunTestsResponse,
    SecurityScanToolResponse,
)
from mcp_server.sentinelflow_context.validation import MAX_TEST_IDS

from tests.contract.conftest import mcp_session


async def _tool_error(
    adapter: SentinelFlowMcpAdapter,
    name: str,
    arguments: dict[str, object],
) -> str:
    result = await adapter.call_tool(name, arguments)
    assert result.is_error
    assert result.content
    return result.content[0].text


class TestGetChangePackage:
    async def test_valid_fixture(self) -> None:
        async with mcp_session() as adapter:
            payload = await adapter.call_tool_typed(
                "get_change_package",
                {"change_id": "CHG-H001", "domain_pack_id": "healthcare-claims"},
                response_model=ChangePackageResponse,
            )
            assert payload.change_id == "CHG-H001"
            assert payload.source == "fixture"

    async def test_invalid_missing_change(self) -> None:
        async with mcp_session() as adapter:
            message = await _tool_error(
                adapter,
                "get_change_package",
                {"change_id": "DOES-NOT-EXIST", "domain_pack_id": "healthcare-claims"},
            )
            assert "not found" in message.lower()

    async def test_malicious_path_traversal(self) -> None:
        async with mcp_session() as adapter:
            message = await _tool_error(
                adapter,
                "get_change_package",
                {"change_id": "../secrets", "domain_pack_id": "healthcare-claims"},
            )
            assert "path" in message.lower() or "invalid" in message.lower()

    async def test_github_source_stubbed(self) -> None:
        async with mcp_session() as adapter:
            message = await _tool_error(
                adapter,
                "get_change_package",
                {"change_id": "CHG-H001", "source": "github"},
            )
            assert "deferred" in message.lower() or "github" in message.lower()


class TestGetDependencyGraph:
    async def test_valid(self) -> None:
        async with mcp_session() as adapter:
            payload = await adapter.call_tool_typed(
                "get_dependency_graph",
                {"change_id": "CHG-H001", "domain_pack_id": "healthcare-claims"},
                response_model=DependencyGraphResponse,
            )
            assert payload.change_id == "CHG-H001"

    async def test_invalid_pack(self) -> None:
        async with mcp_session() as adapter:
            message = await _tool_error(
                adapter,
                "get_dependency_graph",
                {"change_id": "CHG-H001", "domain_pack_id": "not-a-real-pack"},
            )
            assert "not found" in message.lower()

    async def test_malicious_oversized_change_id(self) -> None:
        async with mcp_session() as adapter:
            message = await _tool_error(
                adapter,
                "get_dependency_graph",
                {"change_id": "A" * 300},
            )
            assert "change_id" in message.lower()


class TestDiffContracts:
    async def test_valid_breaking_change(self) -> None:
        async with mcp_session() as adapter:
            payload = await adapter.call_tool_typed(
                "diff_contracts",
                {"change_id": "CHG-H002", "domain_pack_id": "healthcare-claims"},
                response_model=ContractDiffToolResponse,
            )
            assert payload.result.err_count >= 1

    async def test_invalid_change(self) -> None:
        async with mcp_session() as adapter:
            message = await _tool_error(adapter, "diff_contracts", {"change_id": ""})
            assert "change_id" in message.lower()

    async def test_malicious_path_traversal(self) -> None:
        async with mcp_session() as adapter:
            message = await _tool_error(
                adapter,
                "diff_contracts",
                {"change_id": "../../etc/passwd"},
            )
            assert "path" in message.lower() or "invalid" in message.lower()


class TestListTests:
    async def test_valid(self) -> None:
        async with mcp_session() as adapter:
            payload = await adapter.call_tool_typed(
                "list_tests",
                {"change_id": "CHG-H002", "domain_pack_id": "healthcare-claims"},
                response_model=ChangeTestInventory,
            )
            assert "test_claims_contract" in payload.available_tests

    async def test_invalid_change(self) -> None:
        async with mcp_session() as adapter:
            message = await _tool_error(adapter, "list_tests", {"change_id": "NOPE"})
            assert "not found" in message.lower() or "change_id" in message.lower()

    async def test_malicious_traversal(self) -> None:
        async with mcp_session() as adapter:
            message = await _tool_error(
                adapter,
                "list_tests",
                {"change_id": "..\\windows\\system32"},
            )
            assert "path" in message.lower() or "invalid" in message.lower()


class TestRunTests:
    async def test_valid_dry_run(self) -> None:
        async with mcp_session() as adapter:
            payload = await adapter.call_tool_typed(
                "run_tests",
                {
                    "change_id": "CHG-H002",
                    "domain_pack_id": "healthcare-claims",
                    "test_ids": ["test_claims_contract"],
                    "dry_run": True,
                },
                response_model=RunTestsResponse,
            )
            assert payload.status == "dry_run"
            assert payload.planned_docker_flags

    async def test_invalid_empty_test_ids(self) -> None:
        async with mcp_session() as adapter:
            message = await _tool_error(
                adapter,
                "run_tests",
                {"change_id": "CHG-H002", "test_ids": []},
            )
            assert "test_ids" in message.lower()

    async def test_malicious_unlisted_test_id(self) -> None:
        async with mcp_session() as adapter:
            message = await _tool_error(
                adapter,
                "run_tests",
                {"change_id": "CHG-H002", "test_ids": ["../../etc/passwd"]},
            )
            assert "inventory" in message.lower() or "path" in message.lower()

    async def test_malicious_oversized_test_list(self) -> None:
        async with mcp_session() as adapter:
            message = await _tool_error(
                adapter,
                "run_tests",
                {
                    "change_id": "CHG-H002",
                    "test_ids": [f"test-{index}" for index in range(MAX_TEST_IDS + 1)],
                },
            )
            assert "maximum" in message.lower()


class TestRunSecurityScan:
    async def test_valid(self) -> None:
        async with mcp_session() as adapter:
            payload = await adapter.call_tool_typed(
                "run_security_scan",
                {"change_id": "CHG-H003", "domain_pack_id": "healthcare-claims"},
                response_model=SecurityScanToolResponse,
            )
            assert payload.result.verified_secret_exposure is True

    async def test_invalid_change(self) -> None:
        async with mcp_session() as adapter:
            message = await _tool_error(adapter, "run_security_scan", {"change_id": "UNKNOWN"})
            assert "not found" in message.lower() or "change_id" in message.lower()

    async def test_malicious_null_byte(self) -> None:
        async with mcp_session() as adapter:
            message = await _tool_error(
                adapter,
                "run_security_scan",
                {"change_id": "CHG-H001\x00"},
            )
            assert "path" in message.lower() or "invalid" in message.lower()


class TestRecordGateDecision:
    async def test_valid_write(self, audit_log_path: Path) -> None:
        async with mcp_session() as adapter:
            payload = await adapter.call_tool_typed(
                "record_gate_decision",
                {
                    "analysis_id": "analysis-001",
                    "decision": "PASS",
                    "evidence_hash": "a" * 64,
                    "idempotency_key": "key-001",
                },
                response_model=RecordGateDecisionResponse,
            )
            assert payload.recorded is True
            assert audit_log_path.is_file()

    async def test_invalid_decision(self) -> None:
        async with mcp_session() as adapter:
            message = await _tool_error(
                adapter,
                "record_gate_decision",
                {
                    "analysis_id": "analysis-001",
                    "decision": "MAYBE",
                    "evidence_hash": "a" * 64,
                    "idempotency_key": "key-bad-decision",
                },
            )
            assert "decision" in message.lower()

    async def test_malicious_non_hex_hash(self) -> None:
        async with mcp_session() as adapter:
            message = await _tool_error(
                adapter,
                "record_gate_decision",
                {
                    "analysis_id": "analysis-001",
                    "decision": "PASS",
                    "evidence_hash": "not-a-hash",
                    "idempotency_key": "key-bad-hash",
                },
            )
            assert "evidence_hash" in message.lower()

    async def test_idempotent_duplicate(self, audit_log_path: Path) -> None:
        args = {
            "analysis_id": "analysis-dup",
            "decision": "REVIEW",
            "evidence_hash": "b" * 64,
            "idempotency_key": "dup-key",
        }
        async with mcp_session() as adapter:
            first = await adapter.call_tool_typed(
                "record_gate_decision",
                args,
                response_model=RecordGateDecisionResponse,
            )
            second = await adapter.call_tool_typed(
                "record_gate_decision",
                args,
                response_model=RecordGateDecisionResponse,
            )
        assert first.recorded is True
        assert second.duplicate is True
        lines = audit_log_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["idempotency_key"] == "dup-key"


class TestCallToolTypedErrors:
    async def test_raises_mcp_tool_error(self) -> None:
        async with mcp_session() as adapter:
            with pytest.raises(McpToolError):
                await adapter.call_tool_typed(
                    "get_change_package",
                    {"change_id": "../bad"},
                    response_model=ChangePackageResponse,
                )
