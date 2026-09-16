"""Typed MCP tool and resource payloads."""

from __future__ import annotations

from typing import Literal

from app.deterministic.models import (
    BlastRadiusResult,
    ContractDiffResult,
    GraphEdge,
    SecurityScanResult,
)
from app.graph.state import ReleaseDecision
from pydantic import BaseModel, Field


class ChangePackageResponse(BaseModel):
    """Fixture-backed change package returned by get_change_package."""

    change_id: str
    source: Literal["fixture", "github"]
    domain_pack_id: str
    description: str
    category: str
    primary_service: str
    changed_files: list[str] = Field(default_factory=list)
    origin_nodes: list[str] = Field(default_factory=list)
    fixture_path: str


class DependencyGraphResponse(BaseModel):
    """Dependency graph and blast-radius snapshot for a change."""

    change_id: str
    domain_pack_id: str
    edges: list[GraphEdge]
    blast_radius: BlastRadiusResult
    impacted_services: list[str]


class ChangeTestInventory(BaseModel):
    """Test inventory for a change fixture."""

    change_id: str
    domain_pack_id: str
    required_tests: list[str] = Field(default_factory=list)
    available_tests: list[str] = Field(default_factory=list)
    missing_tests: list[str] = Field(default_factory=list)


class RunTestsResponse(BaseModel):
    """Dry-run or deferred execution response for run_tests."""

    change_id: str
    domain_pack_id: str
    dry_run: bool
    test_ids: list[str]
    status: Literal["dry_run", "deferred", "rejected"]
    message: str
    planned_docker_flags: list[str] = Field(default_factory=list)


class RecordGateDecisionResponse(BaseModel):
    """Append-only audit log write result."""

    recorded: bool
    duplicate: bool
    analysis_id: str
    decision: ReleaseDecision
    idempotency_key: str


class ServiceCatalogResource(BaseModel):
    """Domain service catalog resource payload."""

    domain_pack_id: str
    services: list[dict[str, str]]


class ReleasePolicyResource(BaseModel):
    """Domain release policy resource payload."""

    domain_pack_id: str
    policy: dict[str, object]


class DependencyGraphResource(BaseModel):
    """Seed dependency graph resource payload."""

    domain_pack_id: str
    nodes: list[dict[str, str]]
    edges: list[dict[str, str]]


class ContractDiffToolResponse(BaseModel):
    """Wrapper for diff_contracts tool output."""

    change_id: str
    domain_pack_id: str
    result: ContractDiffResult


class SecurityScanToolResponse(BaseModel):
    """Wrapper for run_security_scan tool output."""

    change_id: str
    domain_pack_id: str
    result: SecurityScanResult
