"""Validate and clamp Triage Planner output against domain policy."""

from __future__ import annotations

from app.core.domain_pack import AgentName, ChangeCategory, DomainPack
from app.graph.state import AnalysisPlan

PARALLEL_AGENT_NAMES: frozenset[AgentName] = frozenset(
    {
        "change_analyst",
        "evidence_investigator",
        "test_strategist",
        "security_auditor",
    },
)

AGENT_SORT_KEYS: dict[AgentName, int] = {
    "change_analyst": 0,
    "evidence_investigator": 1,
    "test_strategist": 2,
    "security_auditor": 3,
    "evidence_verifier": 4,
}

VALID_CHANGE_CATEGORIES: frozenset[ChangeCategory] = frozenset(
    {
        "api_contract",
        "auth_security",
        "data_schema",
        "dependency",
        "configuration",
        "documentation",
        "unknown",
    },
)


def _dedupe_agents(agents: list[AgentName]) -> list[AgentName]:
    seen: set[AgentName] = set()
    ordered: list[AgentName] = []
    for agent in agents:
        if agent not in seen:
            seen.add(agent)
            ordered.append(agent)
    return ordered


def default_plan_for_pack(domain_pack: DomainPack) -> AnalysisPlan:
    return AnalysisPlan(
        change_category="unknown",
        agents=_dedupe_agents(list(domain_pack.policy.default_plan)),
        rationale="Policy default plan",
        clamped_to_default=True,
    )


def validate_analysis_plan(
    proposed: AnalysisPlan,
    domain_pack: DomainPack,
) -> AnalysisPlan:
    """Return a policy-valid plan, clamping to default when necessary."""
    allowed = set(domain_pack.policy.allowed_agents)
    category = (
        proposed.change_category
        if proposed.change_category in VALID_CHANGE_CATEGORIES
        else "unknown"
    )
    agents = _dedupe_agents([agent for agent in proposed.agents if agent in allowed])
    clamped = False
    if not agents or len(agents) != len(proposed.agents):
        clamped = True
        agents = _dedupe_agents(list(domain_pack.policy.default_plan))

    if "evidence_verifier" not in agents and "evidence_verifier" in allowed:
        agents.append("evidence_verifier")

    return AnalysisPlan(
        change_category=category,
        agents=agents,
        rationale=proposed.rationale[:500],
        clamped_to_default=clamped or proposed.clamped_to_default,
    )


def parallel_agents_from_plan(plan: AnalysisPlan) -> list[AgentName]:
    """Agents invoked via Send fan-out (Evidence Verifier runs after merge)."""
    return sorted(
        [agent for agent in plan.agents if agent in PARALLEL_AGENT_NAMES],
        key=lambda name: AGENT_SORT_KEYS[name],
    )


def verifier_scheduled(plan: AnalysisPlan) -> bool:
    return "evidence_verifier" in plan.agents
