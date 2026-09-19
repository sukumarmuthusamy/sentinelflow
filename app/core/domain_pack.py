"""Load and validate domain packs from YAML configuration."""

from __future__ import annotations

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, ValidationInfo, field_validator

from app.core.errors import ErrorCode, SentinelFlowError

CriticalityTier = Literal["critical", "high", "medium", "low"]
DecisionOutcome = Literal["PASS", "REVIEW", "BLOCK"]
AgentName = Literal[
    "change_analyst",
    "evidence_investigator",
    "test_strategist",
    "security_auditor",
    "evidence_verifier",
]
ChangeCategory = Literal[
    "api_contract",
    "auth_security",
    "data_schema",
    "dependency",
    "configuration",
    "documentation",
    "unknown",
]


class ServiceDefinition(BaseModel):
    """A service entry in the domain catalog."""

    name: str
    criticality: CriticalityTier
    data_classification: str
    owner: str
    description: str | None = None


class NonCodePathRules(BaseModel):
    """Paths excluded from code-dependency graph traversal (domain-pack configurable)."""

    extensions: list[str] = Field(
        default_factory=lambda: [".md", ".txt", ".rst", ".adoc"],
    )
    path_segments: list[str] = Field(
        default_factory=lambda: ["docs"],
    )


def is_non_code_path(path: str, rules: NonCodePathRules) -> bool:
    """Return True when a file path should not enter the code dependency graph."""
    normalized = path.replace("\\", "/").strip("/").lower()
    if not normalized:
        return False

    for ext in rules.extensions:
        suffix = ext.lower() if ext.startswith(".") else f".{ext.lower()}"
        if normalized.endswith(suffix):
            return True

    parts = normalized.split("/")
    excluded_segments = {segment.lower() for segment in rules.path_segments}
    return bool(excluded_segments.intersection(parts))


def is_non_code_module(module_id: str, rules: NonCodePathRules) -> bool:
    """Return True for module ids (file paths or dotted import names) that are non-code."""
    path_like = module_id.replace(".", "/")
    if is_non_code_path(path_like, rules):
        return True
    return is_non_code_path(f"{path_like}.py", rules)


class DomainConfig(BaseModel):
    """Content of domain.yaml."""

    pack_id: str
    display_name: str
    description: str
    services: list[ServiceDefinition] = Field(min_length=1)
    data_classification_labels: list[str] = Field(min_length=1)
    non_code_paths: NonCodePathRules = Field(default_factory=NonCodePathRules)

    @field_validator("pack_id")
    @classmethod
    def validate_pack_id(cls, value: str) -> str:
        if not value.replace("-", "").replace("_", "").isalnum():
            msg = "pack_id must be alphanumeric with hyphens or underscores"
            raise ValueError(msg)
        return value


class DependencyNode(BaseModel):
    """A node in the seed dependency graph."""

    id: str
    type: Literal["service", "module", "endpoint"] = "service"


class DependencyEdge(BaseModel):
    """A directed edge in the seed dependency graph."""

    source: str
    target: str
    kind: Literal["imports", "calls", "deploys_with", "publishes_to"] = "deploys_with"
    provenance: Literal["seed", "static_analysis", "heuristic"] = "seed"


class DependenciesConfig(BaseModel):
    """Content of dependencies.yaml."""

    nodes: list[DependencyNode] = Field(min_length=1)
    edges: list[DependencyEdge] = Field(default_factory=list)


class DecisionThresholds(BaseModel):
    """Numeric score thresholds for PASS / REVIEW / BLOCK."""

    pass_max: int = Field(ge=0, le=100)
    review_max: int = Field(ge=0, le=100)

    @field_validator("review_max")
    @classmethod
    def review_must_exceed_pass(cls, value: int, info: ValidationInfo) -> int:
        pass_max = info.data.get("pass_max")
        if isinstance(pass_max, int) and value <= pass_max:
            msg = "review_max must be greater than pass_max"
            raise ValueError(msg)
        return value


class RiskWeights(BaseModel):
    """Weight coefficients for the deterministic risk engine."""

    blast_radius: float = Field(ge=0)
    critical_service: float = Field(ge=0)
    contract_break: float = Field(ge=0)
    security_severity: float = Field(ge=0)
    test_gap: float = Field(ge=0)
    incident_similarity: float = Field(ge=0)
    evidence_confidence_penalty: float = Field(ge=0)


class PolicyOverride(BaseModel):
    """A hard override rule that forces a release decision."""

    id: str
    condition: str
    outcome: DecisionOutcome
    description: str


class PolicyConfig(BaseModel):
    """Content of policy.yaml."""

    evidence_floor: float = Field(ge=0.0, le=1.0)
    dependency_vuln_ceiling: CriticalityTier = "high"
    decision_thresholds: DecisionThresholds
    risk_weights: RiskWeights
    overrides: list[PolicyOverride] = Field(default_factory=list)
    allowed_agents: list[AgentName] = Field(min_length=1)
    default_plan: list[AgentName] = Field(min_length=1)
    category_agent_map: dict[ChangeCategory, list[AgentName]] = Field(default_factory=dict)

    @field_validator("default_plan")
    @classmethod
    def default_plan_subset_of_allowed(
        cls,
        value: list[AgentName],
        info: ValidationInfo,
    ) -> list[AgentName]:
        allowed = info.data.get("allowed_agents", [])
        if isinstance(allowed, list) and any(agent not in allowed for agent in value):
            msg = "default_plan agents must be listed in allowed_agents"
            raise ValueError(msg)
        return value


class DomainPack(BaseModel):
    """Fully loaded domain pack."""

    root: Path
    domain: DomainConfig
    dependencies: DependenciesConfig
    policy: PolicyConfig

    @property
    def pack_id(self) -> str:
        return self.domain.pack_id


def _read_yaml(path: Path) -> object:
    if not path.is_file():
        raise SentinelFlowError(
            f"Missing domain pack file: {path.name}",
            code=ErrorCode.DOMAIN_PACK_NOT_FOUND,
            details={"path": str(path)},
        )
    with path.open(encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def list_domain_pack_ids(*, domains_root: Path | None = None) -> list[str]:
    """Return sorted domain pack identifiers discovered under domains_root."""
    root = domains_root
    if root is None:
        from app.core.settings import get_settings

        root = get_settings().domains_root

    if not root.is_dir():
        return []

    return sorted(
        path.name for path in root.iterdir() if path.is_dir() and (path / "domain.yaml").is_file()
    )


def resolve_default_domain_pack_id(*, domains_root: Path | None = None) -> str:
    """Resolve the configured default pack, falling back to the first discovered pack."""
    from app.core.settings import get_settings

    settings = get_settings()
    if settings.default_domain_pack:
        return settings.default_domain_pack

    discovered = list_domain_pack_ids(domains_root=domains_root or settings.domains_root)
    if not discovered:
        raise SentinelFlowError(
            "No domain packs found",
            code=ErrorCode.DOMAIN_PACK_NOT_FOUND,
            details={"domains_root": str(settings.domains_root)},
        )
    return discovered[0]


def load_domain_pack(pack_id: str, *, domains_root: Path | None = None) -> DomainPack:
    """Load and validate a domain pack by identifier."""
    root = domains_root
    if root is None:
        from app.core.settings import get_settings

        root = get_settings().domains_root

    pack_dir = root / pack_id
    if not pack_dir.is_dir():
        raise SentinelFlowError(
            f"Domain pack not found: {pack_id}",
            code=ErrorCode.DOMAIN_PACK_NOT_FOUND,
            details={"pack_id": pack_id, "path": str(pack_dir)},
        )

    try:
        domain = DomainConfig.model_validate(_read_yaml(pack_dir / "domain.yaml"))
        dependencies = DependenciesConfig.model_validate(_read_yaml(pack_dir / "dependencies.yaml"))
        policy = PolicyConfig.model_validate(_read_yaml(pack_dir / "policy.yaml"))
    except SentinelFlowError:
        raise
    except Exception as exc:  # noqa: BLE001 — surface validation failures uniformly
        raise SentinelFlowError(
            f"Invalid domain pack: {pack_id}",
            code=ErrorCode.DOMAIN_PACK_INVALID,
            details={"pack_id": pack_id, "reason": str(exc)},
        ) from exc

    if domain.pack_id != pack_id:
        raise SentinelFlowError(
            "domain.yaml pack_id does not match directory name",
            code=ErrorCode.DOMAIN_PACK_INVALID,
            details={"expected": pack_id, "actual": domain.pack_id},
        )

    return DomainPack(root=pack_dir, domain=domain, dependencies=dependencies, policy=policy)
