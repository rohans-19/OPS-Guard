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


# ---------------------------------------------------------------------------
# Effect model v1: a typed graph over the Kubernetes kinds in impact/kinds.py
# ---------------------------------------------------------------------------
# Edges run from a dependency to its dependent ("a change to the source can
# affect the target"). Each edge records its type and where it came from:
#   static    read from the API objects (ownerReferences, selectors, references)
#   dynamic   observed at runtime (EndpointSlice membership, Pod readiness)
#   topology  declared in the trusted topology file (application calls, settings)
# Constraint edges (PodDisruptionBudget) are kept for the rules but do not
# propagate impact.

STATIC_EDGE_TYPES = ("owns", "mounts", "env", "uses_sa", "binds", "selects", "selects_template",
                     "slice_of", "backend", "targets", "isolates", "constrains")
DYNAMIC_EDGE_TYPES = ("member",)
TOPOLOGY_EDGE_TYPES = ("calls", "reads_setting")
NON_PROPAGATING = frozenset({"constrains"})


def _volume_refs(pod_spec) -> list[tuple[str, str, str]]:
    """(kind, name, edge type) for every ConfigMap, Secret and PVC a Pod spec references."""
    from opsguard.impact.state import _mapping

    out = []
    for volume in pod_spec.get("volumes") or ():
        if volume.get("configMap"):
            out.append(("ConfigMap", volume["configMap"].get("name"), "mounts"))
        if volume.get("secret"):
            out.append(("Secret", volume["secret"].get("secretName"), "mounts"))
        if volume.get("persistentVolumeClaim"):
            out.append(("PersistentVolumeClaim", volume["persistentVolumeClaim"].get("claimName"), "mounts"))
        for source in _mapping(volume.get("projected"), "projected").get("sources") or ():
            if source.get("configMap"):
                out.append(("ConfigMap", source["configMap"].get("name"), "mounts"))
            if source.get("secret"):
                out.append(("Secret", source["secret"].get("name"), "mounts"))
    for group in ("initContainers", "containers"):
        for container in pod_spec.get(group) or ():
            for source in container.get("envFrom") or ():
                if source.get("configMapRef"):
                    out.append(("ConfigMap", source["configMapRef"].get("name"), "env"))
                if source.get("secretRef"):
                    out.append(("Secret", source["secretRef"].get("name"), "env"))
            for env in container.get("env") or ():
                value_from = env.get("valueFrom") or {}
                if value_from.get("configMapKeyRef"):
                    out.append(("ConfigMap", value_from["configMapKeyRef"].get("name"), "env"))
                if value_from.get("secretKeyRef"):
                    out.append(("Secret", value_from["secretKeyRef"].get("name"), "env"))
    return [(k, n, t) for k, n, t in out if isinstance(n, str) and n]


def secret_refs(pod_spec) -> frozenset[str]:
    return frozenset(name for kind, name, _ in _volume_refs(pod_spec) if kind == "Secret")


def pod_is_ready(pod) -> bool:
    from opsguard.impact.state import meta, status

    if meta(pod).get("deletionTimestamp"):
        return False
    for condition in status(pod).get("conditions") or ():
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False


class EffectGraph:
    """The dependency graph for one state, with typed edges and their origin."""

    def __init__(self, state, topology=None):
        from opsguard.impact.state import (ObjectRef, labels, meta, pod_template, ref_of, selector_matches,
                                           spec)
        from opsguard.impact.topology import TrustedTopology

        self.state, self.topology = state, topology or TrustedTopology()
        self.graph = nx.MultiDiGraph()
        ns = state.namespace
        for pool in state.objects.values():
            self.graph.add_nodes_from(pool)

        def add(src, dst, kind, origin, **attrs):
            if src == dst:
                return
            # References to objects that were not read, or do not exist, still become nodes:
            # a workload mounting a missing ConfigMap depends on it all the same.
            self.graph.add_edge(src, dst, key=kind, type=kind, source=origin, **attrs)

        for kind, pool in state.objects.items():
            for ref, obj in pool.items():
                for owner in state.owner_refs(obj):
                    add(owner, ref, "owns", "static")

        workloads = state.workloads()
        pods = state.items("Pod")
        for ref, obj in list(workloads.items()) + list(pods.items()):
            pod_spec = spec(pod_template(obj)) if ref.kind != "Pod" else spec(obj)
            for kind, name, edge in _volume_refs(pod_spec):
                add(ObjectRef(kind, ns, name), ref, edge, "static")
            account = pod_spec.get("serviceAccountName") or "default"
            add(ObjectRef("ServiceAccount", ns, account), ref, "uses_sa", "static")

        for ref, binding in state.items("RoleBinding").items():
            for subject in binding.get("subjects") or ():
                if subject.get("kind") == "ServiceAccount" and subject.get("namespace", ns) == ns:
                    add(ref, ObjectRef("ServiceAccount", ns, subject.get("name")), "binds", "static")

        template_labels = {ref: labels(pod_template(obj)) for ref, obj in workloads.items()}
        pod_labels = {ref: labels(obj) for ref, obj in pods.items()}
        for ref, service in state.items("Service").items():
            selector = spec(service).get("selector") or {}
            if spec(service).get("type") == "ExternalName" or not selector:
                continue
            for pod, lab in pod_labels.items():
                if selector_matches(selector, lab):
                    add(pod, ref, "selects", "static")
            for workload, lab in template_labels.items():
                if selector_matches(selector, lab):
                    add(workload, ref, "selects_template", "static")

        for ref, slice_ in state.items("EndpointSlice").items():
            service = labels(slice_).get("kubernetes.io/service-name")
            if service:
                add(ref, ObjectRef("Service", ns, service), "slice_of", "static")
            for endpoint in slice_.get("endpoints") or ():
                target = endpoint.get("targetRef") or {}
                if target.get("kind") == "Pod" and target.get("name"):
                    ready = (endpoint.get("conditions") or {}).get("ready")
                    add(ObjectRef("Pod", ns, target["name"]), ref, "member", "dynamic", ready=ready is not False)

        for ref, ingress in state.items("Ingress").items():
            for name in ingress_backends(ingress):
                add(ObjectRef("Service", ns, name), ref, "backend", "static")

        for ref, hpa in state.items("HorizontalPodAutoscaler").items():
            target = spec(hpa).get("scaleTargetRef") or {}
            if target.get("kind") and target.get("name"):
                add(ref, ObjectRef(target["kind"], ns, target["name"]), "targets", "static")

        for ref, policy in state.items("NetworkPolicy").items():
            selector = spec(policy).get("podSelector")
            if selector is None:
                continue
            everything = not (selector.get("matchLabels") or selector.get("matchExpressions"))
            for pod, lab in pod_labels.items():
                if everything or selector_matches(selector, lab):
                    add(ref, pod, "isolates", "static")
            for workload, lab in template_labels.items():
                if everything or selector_matches(selector, lab):
                    add(ref, workload, "isolates", "static")

        for ref, pdb in state.items("PodDisruptionBudget").items():
            selector = spec(pdb).get("selector") or {}
            for workload, lab in template_labels.items():
                if selector_matches(selector, lab):
                    add(ref, workload, "constrains", "static")

        for call in self.topology.calls:
            add(ObjectRef("Service", ns, call.service), _workload_named(workloads, ns, call.caller), "calls",
                "topology", dependency=call.dependency)
        for setting in self.topology.settings:
            add(ObjectRef("ConfigMap", ns, setting.configmap), _workload_named(workloads, ns, setting.consumer),
                "reads_setting", "topology", setting_key=setting.key)

    # -- queries ------------------------------------------------------------------
    def edges(self, *, type=None, source=None):
        for src, dst, data in self.graph.edges(data=True):
            if (type is None or data["type"] == type) and (source is None or data["source"] == source):
                yield src, dst, data

    def successors(self, ref, *types):
        if ref not in self.graph:
            return set()
        return {dst for _, dst, data in self.graph.out_edges(ref, data=True) if not types or data["type"] in types}

    def predecessors(self, ref, *types):
        if ref not in self.graph:
            return set()
        return {src for src, _, data in self.graph.in_edges(ref, data=True) if not types or data["type"] in types}

    def reachable(self, ref) -> set:
        """Every object a change to ``ref`` can reach through propagating edges, including itself."""
        if ref not in self.graph:
            return {ref}
        seen, frontier = {ref}, [ref]
        while frontier:
            node = frontier.pop()
            for _, dst, data in self.graph.out_edges(node, data=True):
                if data["type"] not in NON_PROPAGATING and dst not in seen:
                    seen.add(dst)
                    frontier.append(dst)
        return seen

    def services_of(self, workload) -> set:
        """Services that select a workload's Pods (live Pods first, then the template)."""
        from opsguard.impact.state import ObjectRef

        services = self.successors(workload, "selects_template")
        for pod in self.pods_of(workload):
            services |= self.successors(pod, "selects")
        return {s for s in services if isinstance(s, ObjectRef) and s.kind == "Service"}

    def pods_of(self, workload) -> set:
        """Pods whose ownerReferences lead to the workload."""
        out = set()
        for pod in self.state.items("Pod"):
            if self.state.owning_workload(pod) == workload:
                out.add(pod)
        return out


def ingress_backends(ingress) -> set[str]:
    from opsguard.impact.state import spec

    names = set()
    default = (spec(ingress).get("defaultBackend") or {}).get("service") or {}
    if default.get("name"):
        names.add(default["name"])
    for rule in spec(ingress).get("rules") or ():
        for path in (rule.get("http") or {}).get("paths") or ():
            service = (path.get("backend") or {}).get("service") or {}
            if service.get("name"):
                names.add(service["name"])
    return names


def _workload_named(workloads, namespace, name):
    from opsguard.impact.state import ObjectRef

    matches = [ref for ref in workloads if ref.name == name]
    if len(matches) > 1:
        raise ValueError(f"topology name {name!r} matches more than one workload")
    return matches[0] if matches else ObjectRef("Deployment", namespace, name)
