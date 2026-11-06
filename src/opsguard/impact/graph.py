"""Find Services reachable from a change in trusted topology."""

from collections.abc import Iterable
from dataclasses import dataclass

import networkx as nx


@dataclass(frozen=True)
class ResourceRef:
    kind: str
    namespace: str
    name: str

    def __post_init__(self) -> None:
        if self.kind not in ("workload", "service", "configmap"):
            raise ValueError(f"Unsupported dependency resource kind: {self.kind!r}")
        for field in (self.namespace, self.name):
            if not isinstance(field, str) or not field.strip():
                raise ValueError("Dependency resources require a namespace and name")


def build_dependency_graph(
    resources: Iterable[ResourceRef],
    edges: Iterable[tuple[ResourceRef, ResourceRef]],
) -> nx.DiGraph:
    """Build a graph whose edges run from a resource to its dependents."""
    graph = nx.DiGraph()
    graph.add_nodes_from(resources)
    for dependency, dependent in edges:
        if dependency not in graph or dependent not in graph:
            raise ValueError(f"Unknown resource in dependency edge: {dependency!r} -> {dependent!r}")
        graph.add_edge(dependency, dependent)
    return graph


def affected_services(
    graph: nx.DiGraph,
    changed_resource: ResourceRef,
) -> tuple[ResourceRef, ...]:
    """Return potentially affected Services, including a directly changed Service."""
    if changed_resource not in graph:
        raise ValueError(f"Changed resource is absent from the dependency graph: {changed_resource!r}")
    reachable = nx.descendants(graph, changed_resource)
    reachable.add(changed_resource)
    services = (resource for resource in reachable if resource.kind == "service")
    return tuple(sorted(services, key=lambda resource: (resource.namespace, resource.name)))
