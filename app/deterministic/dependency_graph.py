"""Dependency graph construction and blast-radius analysis."""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

import grimp
import networkx as nx
from tree_sitter import Language, Parser, Query, QueryCursor

from app.core.domain_pack import (
    DependenciesConfig,
    DomainPack,
    NonCodePathRules,
    is_non_code_module,
    is_non_code_path,
)
from app.deterministic.models import BlastRadiusNode, BlastRadiusResult, EdgeProvenance, GraphEdge


def _optional_module(name: str) -> ModuleType | None:
    try:
        return __import__(name)
    except ImportError:  # pragma: no cover - optional at runtime
        return None


def _javascript_language() -> Language | None:
    module = _optional_module("tree_sitter_javascript")
    if module is None:
        return None
    return Language(module.language())


def _add_edge(
    graph: nx.DiGraph[Any],
    *,
    source: str,
    target: str,
    kind: str,
    provenance: EdgeProvenance,
) -> None:
    if source == target:
        return
    if graph.has_edge(source, target):
        existing = graph.edges[source, target]
        if existing["provenance"] == "resolved":
            return
        if provenance == "resolved":
            graph.edges[source, target]["provenance"] = "resolved"
        return
    graph.add_edge(source, target, kind=kind, provenance=provenance)


def seed_graph_from_domain(deps: DependenciesConfig) -> nx.DiGraph[Any]:
    """Load the domain pack seed graph as a NetworkX DiGraph."""
    graph: nx.DiGraph[Any] = nx.DiGraph()
    for node in deps.nodes:
        graph.add_node(node.id, node_type=node.type, provenance="seed")
    for edge in deps.edges:
        provenance: EdgeProvenance = "seed" if edge.provenance == "seed" else "heuristic"
        _add_edge(
            graph,
            source=edge.source,
            target=edge.target,
            kind=edge.kind,
            provenance=provenance,
        )
    return graph


def _discover_python_roots(repo_root: Path) -> list[str]:
    roots: list[str] = []
    for init_file in repo_root.rglob("__init__.py"):
        package_dir = init_file.parent
        if package_dir == repo_root:
            continue
        rel = package_dir.relative_to(repo_root)
        roots.append(str(rel).replace("\\", ".").replace("/", "."))
    if not roots and any(repo_root.glob("*.py")):
        roots.append(repo_root.name)
    return sorted(set(roots))


def _add_grimp_edges(
    graph: nx.DiGraph[Any],
    repo_root: Path,
    *,
    non_code_rules: NonCodePathRules,
) -> None:
    """Add resolver-accurate Python import edges via grimp."""
    for package_root in _discover_python_roots(repo_root):
        if is_non_code_module(package_root, non_code_rules):
            continue
        try:
            grimp_graph = grimp.build_graph(package_root, include_external_packages=False)
        except Exception:
            continue

        for importer in grimp_graph.modules:
            if is_non_code_module(importer, non_code_rules):
                continue
            for imported in grimp_graph.find_modules_directly_imported_by(importer):
                if is_non_code_module(imported, non_code_rules):
                    continue
                _add_edge(
                    graph,
                    source=importer,
                    target=imported,
                    kind="imports",
                    provenance="resolved",
                )


def _extract_js_imports(source: bytes, *, language: Language) -> list[str]:
    query = Query(
        language,
        """
        (import_statement source: (string (string_fragment) @path))
        (call_expression
          function: (identifier) @fn
          arguments: (arguments (string (string_fragment) @path))
          (#eq? @fn "require"))
        """,
    )
    parser = Parser(language)
    tree = parser.parse(source)
    captures = QueryCursor(query).captures(tree.root_node)
    paths: list[str] = []
    for nodes in captures.values():
        for node in nodes:
            text = source[node.start_byte : node.end_byte].decode("utf-8", errors="ignore")
            paths.append(text.strip("'\" "))
    return paths


def _add_tree_sitter_edges(
    graph: nx.DiGraph[Any],
    repo_root: Path,
    *,
    non_code_rules: NonCodePathRules,
) -> None:
    """Add heuristic edges from non-Python import-like statements."""
    js_language = _javascript_language()
    if js_language is None:
        return
    for path in repo_root.rglob("*.js"):
        module_id = str(path.relative_to(repo_root)).replace("\\", "/")
        if is_non_code_path(module_id, non_code_rules):
            continue
        graph.add_node(module_id, node_type="module", provenance="heuristic")
        try:
            imports = _extract_js_imports(path.read_bytes(), language=js_language)
        except OSError:
            continue
        for imported in imports:
            _add_edge(
                graph,
                source=module_id,
                target=imported,
                kind="imports",
                provenance="heuristic",
            )


def link_modules_to_services(
    graph: nx.DiGraph[Any],
    *,
    module_service_map: dict[str, str],
    non_code_rules: NonCodePathRules,
) -> None:
    """Connect code module nodes to service nodes using an explicit mapping."""
    for module_id, service_id in module_service_map.items():
        if is_non_code_path(module_id, non_code_rules):
            continue
        graph.add_node(module_id, node_type="module", provenance="heuristic")
        graph.add_node(service_id, node_type="service", provenance="seed")
        _add_edge(
            graph,
            source=service_id,
            target=module_id,
            kind="deploys_with",
            provenance="heuristic",
        )


def build_dependency_graph(
    *,
    domain_pack: DomainPack,
    repo_root: Path | None = None,
    module_service_map: dict[str, str] | None = None,
) -> nx.DiGraph[Any]:
    """Merge seed, grimp, and tree-sitter graphs into one provenance-tagged graph."""
    non_code_rules = domain_pack.domain.non_code_paths
    graph = seed_graph_from_domain(domain_pack.dependencies)
    if repo_root is not None and repo_root.is_dir():
        _add_grimp_edges(graph, repo_root, non_code_rules=non_code_rules)
        _add_tree_sitter_edges(graph, repo_root, non_code_rules=non_code_rules)
    if module_service_map:
        link_modules_to_services(
            graph,
            module_service_map=module_service_map,
            non_code_rules=non_code_rules,
        )
    return graph


def compute_blast_radius(
    graph: nx.DiGraph[Any],
    origin_nodes: Iterable[str],
    *,
    max_depth: int = 5,
) -> BlastRadiusResult:
    """Reverse BFS over incoming edges with depth cap and provenance accounting."""
    origins = [node for node in origin_nodes if node in graph]
    visited: dict[str, BlastRadiusNode] = {}
    queue: deque[tuple[str, int]] = deque((node, 0) for node in origins)

    resolved_edges = 0
    heuristic_edges = 0

    while queue:
        node_id, depth = queue.popleft()
        if depth > max_depth:
            continue
        if node_id in visited and visited[node_id].depth <= depth:
            continue

        node_provenance: EdgeProvenance = graph.nodes[node_id].get("provenance", "heuristic")
        visited[node_id] = BlastRadiusNode(
            node_id=node_id,
            depth=depth,
            provenance=node_provenance,
        )

        for predecessor in graph.predecessors(node_id):
            edge = graph.edges[predecessor, node_id]
            if edge.get("provenance") == "resolved":
                resolved_edges += 1
            else:
                heuristic_edges += 1
            if predecessor not in visited or visited[predecessor].depth > depth + 1:
                queue.append((predecessor, depth + 1))

    affected = sorted(visited.values(), key=lambda item: (item.depth, item.node_id))
    max_seen_depth = max((node.depth for node in affected), default=0)
    return BlastRadiusResult(
        origin_nodes=list(origins),
        affected_nodes=[node for node in affected if node.node_id not in origins],
        max_depth=max_seen_depth,
        resolved_edge_count=resolved_edges,
        heuristic_edge_count=heuristic_edges,
    )


def graph_edges(graph: nx.DiGraph[Any]) -> list[GraphEdge]:
    """Export graph edges for reporting."""
    edges: list[GraphEdge] = []
    for source, target, data in graph.edges(data=True):
        edges.append(
            GraphEdge(
                source=source,
                target=target,
                kind=str(data.get("kind", "depends_on")),
                provenance=data.get("provenance", "heuristic"),
            )
        )
    return edges


def impacted_services_from_blast_radius(
    graph: nx.DiGraph[Any],
    blast_radius: BlastRadiusResult,
) -> list[str]:
    """Return service node IDs touched by blast radius."""
    services: list[str] = []
    for node in blast_radius.affected_nodes:
        node_type = graph.nodes.get(node.node_id, {}).get("node_type")
        if node_type == "service" and node.node_id not in services:
            services.append(node.node_id)
    return services


def resolve_change_blast_radius(
    graph: nx.DiGraph[Any],
    origin_nodes: Iterable[str],
    *,
    max_depth: int,
    domain_pack: DomainPack,
) -> BlastRadiusResult:
    """Compute blast radius, isolating non-code origins from graph traversal."""
    rules = domain_pack.domain.non_code_paths
    origins = list(origin_nodes)
    code_origins = [origin for origin in origins if not is_non_code_path(origin, rules)]
    non_code_origins = [origin for origin in origins if is_non_code_path(origin, rules)]

    if non_code_origins and not code_origins:
        return BlastRadiusResult(origin_nodes=origins)

    blast = compute_blast_radius(graph, code_origins, max_depth=max_depth)
    if non_code_origins:
        return blast.model_copy(update={"origin_nodes": origins})
    return blast


def resolve_impacted_services(
    graph: nx.DiGraph[Any],
    blast_radius: BlastRadiusResult,
    *,
    domain_pack: DomainPack,
    module_service_map: dict[str, str],
    primary_service: str,
    origin_nodes: Iterable[str],
) -> list[str]:
    """Return impacted services, including non-code file owners without upstream propagation."""
    rules = domain_pack.domain.non_code_paths
    impacted = impacted_services_from_blast_radius(graph, blast_radius)

    for origin in origin_nodes:
        if is_non_code_path(origin, rules):
            owner = module_service_map.get(origin)
            if owner and owner not in impacted:
                impacted.append(owner)

    if primary_service and primary_service not in impacted:
        impacted.insert(0, primary_service)

    return impacted
