"""Input validation for MCP tools (raises ToolError on business failures)."""

from __future__ import annotations

import re

from mcp.server.mcpserver.exceptions import ToolError

CHANGE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
PACK_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
ANALYSIS_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$")
HEX_HASH_PATTERN = re.compile(r"^[A-Fa-f0-9]{32,128}$")

MAX_TEST_IDS = 64
MAX_TEST_ID_LENGTH = 256


def validate_change_id(change_id: str) -> str:
    """Validate a change identifier and reject path traversal."""
    if not change_id or len(change_id) > 128:
        raise ToolError("change_id must be 1-128 characters")
    if any(token in change_id for token in ("..", "/", "\\", "\x00")):
        raise ToolError("change_id must not contain path separators or traversal sequences")
    if not CHANGE_ID_PATTERN.match(change_id):
        raise ToolError("change_id contains invalid characters")
    return change_id


def validate_domain_pack_id(domain_pack_id: str) -> str:
    """Validate a domain pack identifier."""
    if not domain_pack_id or len(domain_pack_id) > 128:
        raise ToolError("domain_pack_id must be 1-128 characters")
    if not PACK_ID_PATTERN.match(domain_pack_id):
        raise ToolError("domain_pack_id contains invalid characters")
    return domain_pack_id


def validate_analysis_id(analysis_id: str) -> str:
    """Validate an analysis identifier."""
    if not analysis_id or len(analysis_id) > 256:
        raise ToolError("analysis_id must be 1-256 characters")
    if not ANALYSIS_ID_PATTERN.match(analysis_id):
        raise ToolError("analysis_id contains invalid characters")
    return analysis_id


def validate_idempotency_key(idempotency_key: str) -> str:
    """Validate an idempotency key for audit writes."""
    if not idempotency_key or len(idempotency_key) > 256:
        raise ToolError("idempotency_key must be 1-256 characters")
    if not ANALYSIS_ID_PATTERN.match(idempotency_key):
        raise ToolError("idempotency_key contains invalid characters")
    return idempotency_key


def validate_evidence_hash(evidence_hash: str) -> str:
    """Validate a hex-encoded evidence hash."""
    if not evidence_hash or len(evidence_hash) > 128:
        raise ToolError("evidence_hash must be 32-128 hex characters")
    if not HEX_HASH_PATTERN.match(evidence_hash):
        raise ToolError("evidence_hash must be hexadecimal")
    return evidence_hash.lower()


def validate_decision(decision: str) -> str:
    """Validate a release decision string."""
    normalized = decision.upper()
    if normalized not in {"PASS", "REVIEW", "BLOCK"}:
        raise ToolError("decision must be PASS, REVIEW, or BLOCK")
    return normalized


def validate_source(source: str) -> str:
    """Validate a change-package source selector."""
    normalized = source.lower()
    if normalized not in {"fixture", "github"}:
        raise ToolError("source must be 'fixture' or 'github'")
    return normalized


def validate_test_ids(test_ids: list[str]) -> list[str]:
    """Validate requested test identifiers."""
    if not test_ids:
        raise ToolError("test_ids must contain at least one test id")
    if len(test_ids) > MAX_TEST_IDS:
        raise ToolError(f"test_ids exceeds maximum of {MAX_TEST_IDS}")
    validated: list[str] = []
    for test_id in test_ids:
        if not test_id or len(test_id) > MAX_TEST_ID_LENGTH:
            raise ToolError("each test_id must be 1-256 characters")
        if any(token in test_id for token in ("..", "/", "\\", "\x00")):
            raise ToolError("test_id must not contain path separators")
        validated.append(test_id)
    return validated
