"""Unit tests for dependency graph construction and blast radius."""

from __future__ import annotations

from pathlib import Path

import networkx as nx
from app.core.domain_pack import is_non_code_path, load_domain_pack
from app.deterministic.dependency_graph import (
    build_dependency_graph,
    compute_blast_radius,
    resolve_change_blast_radius,
    resolve_impacted_services,
    seed_graph_from_domain,
)

DOMAINS_ROOT = Path(__file__).resolve().parents[2] / "domains"


def test_seed_graph_preserves_provenance() -> None:
    pack = load_domain_pack("healthcare-claims", domains_root=DOMAINS_ROOT)
    graph = seed_graph_from_domain(pack.dependencies)
    assert graph.edges["svc-core-api", "svc-notification"]["provenance"] == "seed"


def test_heuristic_edges_do_not_upgrade_resolved() -> None:
    from app.deterministic.dependency_graph import _add_edge

    pack = load_domain_pack("healthcare-claims", domains_root=DOMAINS_ROOT)
    graph = build_dependency_graph(domain_pack=pack)
    _add_edge(graph, source="a", target="b", kind="imports", provenance="resolved")
    _add_edge(graph, source="a", target="b", kind="imports", provenance="heuristic")
    assert graph.edges["a", "b"]["provenance"] == "resolved"


def test_reverse_bfs_respects_depth_cap() -> None:
    graph: nx.DiGraph = nx.DiGraph()
    graph.add_edge("a", "b", provenance="seed")
    graph.add_edge("b", "c", provenance="seed")
    graph.add_edge("c", "d", provenance="seed")

    blast = compute_blast_radius(graph, ["d"], max_depth=1)
    node_ids = {node.node_id for node in blast.affected_nodes}
    assert "c" in node_ids
    assert "a" not in node_ids


def test_non_code_paths_skip_graph_edges() -> None:
    pack = load_domain_pack("healthcare-claims", domains_root=DOMAINS_ROOT)
    rules = pack.domain.non_code_paths
    assert is_non_code_path("analytics_docs/readme.py", rules)
    assert is_non_code_path("docs/guide.md", rules)
    assert not is_non_code_path("svc_core/handlers.py", rules)


def test_non_code_origin_isolated_to_owner_service() -> None:
    pack = load_domain_pack("healthcare-claims", domains_root=DOMAINS_ROOT)
    fixture_repo = (
        DOMAINS_ROOT
        / "healthcare-claims"
        / "fixtures"
        / "changes"
        / "CHG-H001"
        / "repo"
    )
    module_map = {"analytics_docs/readme.py": "svc-analytics"}
    graph = build_dependency_graph(
        domain_pack=pack,
        repo_root=fixture_repo,
        module_service_map=module_map,
    )
    origin = "analytics_docs/readme.py"
    blast = resolve_change_blast_radius(
        graph,
        [origin],
        max_depth=3,
        domain_pack=pack,
    )
    assert blast.affected_nodes == []
    impacted = resolve_impacted_services(
        graph,
        blast,
        domain_pack=pack,
        module_service_map=module_map,
        primary_service="svc-analytics",
        origin_nodes=[origin],
    )
    assert impacted == ["svc-analytics"]
    assert "svc-core-api" not in impacted
