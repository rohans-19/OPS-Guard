"""Estimate effects from Kubernetes inventory and a server-side dry-run."""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Optional

import networkx as nx

from opsguard.impact.graph import ResourceRef, affected_services, build_dependency_graph


Object = Mapping[str, object]
Inventory = Mapping[ResourceRef, Object]
Backends = Mapping[ResourceRef, set[ResourceRef]]
_WIRE_KINDS = {"workload": "Deployment", "service": "Service", "configmap": "ConfigMap"}
_TARGET_KINDS = {
    "scale": "workload",
    "restart": "workload",
    "set_image": "workload",
    "set_selector": "service",
    "set_config": "configmap",
}


@dataclass(frozen=True)
class ConfigEffect:
    """Trusted fixture knowledge of the consumers and behavior of one key."""

    configmap: ResourceRef
    key: str
    consumers: tuple[ResourceRef, ...]
    degraded_values: frozenset[str]
    healthy_values: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.configmap.kind != "configmap" or not self.key or not self.consumers:
            raise ValueError("a ConfigMap effect needs a ConfigMap, key, and consumers")
        if any(
            consumer.kind != "workload" or consumer.namespace != self.configmap.namespace
            for consumer in self.consumers
        ):
            raise ValueError("ConfigMap consumers must be workloads in its namespace")
        if self.healthy_values & self.degraded_values:
            raise ValueError("ConfigMap effect values cannot be both healthy and degraded")


@dataclass(frozen=True)
class ImpactEstimate:
    reachable_services: tuple[ResourceRef, ...]
    degraded_services: tuple[ResourceRef, ...]
    uncertain_services: tuple[ResourceRef, ...]
    service_count: int


def _field(obj: Object, name: str) -> Object:
    value = obj.get(name)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"Kubernetes {name} must be an object")
    return value


def _objects(obj: Object, name: str) -> Iterable[Object]:
    value = obj.get(name)
    if value is None:
        return ()
    if not isinstance(value, (list, tuple)) or any(not isinstance(item, Mapping) for item in value):
        raise ValueError(f"Kubernetes {name} must be a list of objects")
    return value


def _ref(obj: Object, kind: str, *, allow_scale: bool = False) -> ResourceRef:
    accepted = (None, _WIRE_KINDS[kind], "Scale") if allow_scale else (None, _WIRE_KINDS[kind])
    if obj.get("kind") not in accepted:
        raise ValueError(f"expected Kubernetes {_WIRE_KINDS[kind]}, got {obj.get('kind')!r}")
    metadata = _field(obj, "metadata")
    return ResourceRef(kind, metadata.get("namespace"), metadata.get("name"))


def _index(items: Iterable[Object], kind: str) -> dict[ResourceRef, Object]:
    indexed = {}
    for item in items:
        ref = _ref(item, kind)
        if ref in indexed:
            raise ValueError(f"duplicate inventory resource: {ref!r}")
        indexed[ref] = item
    return indexed


def _template(deployment: Object) -> Object:
    return _field(_field(deployment, "spec"), "template")


def _replicas(deployment: Object) -> int:
    default = None if deployment.get("kind") == "Scale" else 1
    count = _field(deployment, "spec").get("replicas", default)
    if type(count) is not int or count < 0:
        raise ValueError("Deployment replica count must be a nonnegative integer")
    return count


def _selector(service: Object) -> Object:
    spec = _field(service, "spec")
    return {} if spec.get("type") == "ExternalName" else _field(spec, "selector")


def _backends(deployments: Inventory, services: Inventory) -> dict[ResourceRef, set[ResourceRef]]:
    labels = {
        ref: _field(_field(_template(obj), "metadata"), "labels")
        for ref, obj in deployments.items()
    }
    result = {}
    for ref, service in services.items():
        selector = _selector(service)
        result[ref] = {
            workload for workload, pod_labels in labels.items()
            if selector and workload.namespace == ref.namespace
            and all(pod_labels.get(key) == value for key, value in selector.items())
        }
    return result


def _referenced_configmaps(deployment: Object) -> set[str]:
    pod = _field(_template(deployment), "spec")
    references = []
    for volume in _objects(pod, "volumes"):
        references.append(_field(volume, "configMap"))
        for source in _objects(_field(volume, "projected"), "sources"):
            references.append(_field(source, "configMap"))
    for field in ("containers", "initContainers"):
        for container in _objects(pod, field):
            for source in _objects(container, "envFrom"):
                references.append(_field(source, "configMapRef"))
            for env in _objects(container, "env"):
                references.append(_field(_field(env, "valueFrom"), "configMapKeyRef"))
    names = set()
    for reference in references:
        name = reference.get("name")
        if isinstance(name, str) and name:
            names.add(name)
    return names


def _ordered(services: Iterable[ResourceRef]) -> tuple[ResourceRef, ...]:
    return tuple(sorted(services, key=lambda ref: (ref.namespace, ref.name)))


def _changed_config_values(before: Object, preview: Object) -> dict[str, Optional[str]]:
    old_data, new_data = _field(before, "data"), _field(preview, "data")
    for data in (old_data, new_data):
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in data.items()):
            raise ValueError("ConfigMap data must contain string keys and values")
    if _field(before, "binaryData") != _field(preview, "binaryData"):
        raise ValueError("set_config preview changed binaryData")
    return {
        key: new_data.get(key)
        for key in old_data.keys() | new_data.keys()
        if old_data.get(key) != new_data.get(key)
    }


def _config_effects(
    graph: nx.DiGraph,
    target: ResourceRef,
    changed: Mapping[str, Optional[str]],
    rules: Iterable[ConfigEffect],
    backends: Backends,
    reachable: Iterable[ResourceRef],
) -> tuple[set[ResourceRef], set[ResourceRef]]:
    rules = tuple(rule for rule in rules if rule.configmap == target)
    declared = {workload for rule in rules for workload in rule.consumers}
    degraded = set()
    uncertain = {
        service
        for consumer in set(graph.successors(target)) - declared
        for service in affected_services(graph, consumer)
    }
    for key, value in changed.items():
        matched = [rule for rule in rules if rule.key == key]
        if not matched:
            uncertain.update(reachable)
        for rule in matched:
            if value in rule.healthy_values:
                continue
            for workload in rule.consumers:
                uncertain.update(affected_services(graph, workload))
                if value in rule.degraded_values:
                    degraded.update(ref for ref, workloads in backends.items() if workload in workloads)
    return degraded, uncertain


def _active_managed_backends(services: Inventory, backends: Backends, replicas: Mapping[ResourceRef, int]) -> Backends:
    # Selectorless Services can use manually managed endpoints.
    return {
        service: {workload for workload in workloads if replicas[workload] > 0}
        for service, workloads in backends.items()
        if _selector(services[service])
    }


def _services_losing_backends(
    reachable: Iterable[ResourceRef],
    old_active: Backends,
    new_active: Backends,
    recreate_target: Optional[ResourceRef],
) -> set[ResourceRef]:
    degraded = set()
    for service in reachable:
        if service not in old_active or service not in new_active:
            continue
        previous, current = old_active[service], new_active[service]
        if (previous and not current) or (recreate_target is not None and previous == current == {recreate_target}):
            degraded.add(service)
    return degraded


def predict_impact(
    *,
    verb: str,
    deployments: Iterable[Object],
    services: Iterable[Object],
    configmaps: Iterable[Object],
    before: Object,
    preview: Object,
    config_effects: Iterable[ConfigEffect] = (),
    trusted_edges: Iterable[tuple[ResourceRef, ResourceRef]] = (),
) -> ImpactEstimate:
    """Compare wire-format objects against a snapshot of the synthetic inventory.

    Deployment templates approximate managed backends. The result cannot prove
    actual readiness, external routing, or application health after a rollout.
    ConfigMap behavior for recognized keys comes from trusted fixture rules.
    """
    kind = _TARGET_KINDS.get(verb)
    if kind is None:
        raise ValueError(f"unsupported impact verb: {verb!r}")
    target = _ref(before, kind, allow_scale=verb == "scale")
    if target != _ref(preview, kind, allow_scale=verb == "scale"):
        raise ValueError("preview target does not match the original object")

    deployment_index = _index(deployments, "workload")
    service_index = _index(services, "service")
    configmap_index = _index(configmaps, "configmap")
    inventory = {**deployment_index, **service_index, **configmap_index}
    if target not in inventory:
        raise ValueError("preview target is absent from the inventory")
    uids = {_field(obj, "metadata").get("uid") for obj in (inventory[target], before, preview)}
    if len(uids - {None}) > 1:
        raise ValueError("preview target UID does not match the original object")

    old_deployments, new_deployments = deployment_index, deployment_index
    old_services, new_services = service_index, service_index
    if kind == "workload":
        if verb != "scale" and not (_template(before) and _template(preview)):
            raise ValueError(f"{verb} requires the Deployment templates before and after the preview")
        # A Scale response includes the replica count but omits the Pod template.
        old_deployments = {**deployment_index, target: before} if _template(before) else deployment_index
        new_deployments = {**old_deployments, target: preview} if _template(preview) else old_deployments
    elif kind == "service":
        old_services = {**service_index, target: before}
        new_services = {**service_index, target: preview}

    old_backends = _backends(old_deployments, old_services)
    new_backends = _backends(new_deployments, new_services)
    config_effects = tuple(config_effects)
    edges = list(trusted_edges)
    for service in service_index:
        edges.extend((workload, service) for workload in old_backends[service] | new_backends[service])
    for workload, deployment in old_deployments.items():
        names = _referenced_configmaps(deployment) | _referenced_configmaps(new_deployments[workload])
        for name in names:
            configmap = ResourceRef("configmap", workload.namespace, name)
            if configmap in configmap_index:
                edges.append((configmap, workload))
    for rule in config_effects:
        edges.extend((rule.configmap, workload) for workload in rule.consumers)
    graph = build_dependency_graph(inventory, edges)
    reachable = affected_services(graph, target)

    if kind == "configmap":
        changed_values = _changed_config_values(before, preview)
        if not changed_values:
            return ImpactEstimate((), (), (), len(service_index))
        degraded, uncertain = _config_effects(graph, target, changed_values, config_effects, new_backends, reachable)
    else:
        old_replicas = {ref: _replicas(obj) for ref, obj in old_deployments.items()}
        new_replicas = {ref: _replicas(obj) for ref, obj in new_deployments.items()}
        if kind == "workload":
            old_replicas[target], new_replicas[target] = _replicas(before), _replicas(preview)
        if verb == "scale":
            changed = (
                old_replicas[target] != new_replicas[target]
                or _template(old_deployments[target]) != _template(new_deployments[target])
            )
            risky = new_replicas[target] < old_replicas[target] or old_backends != new_backends
        elif verb == "set_selector":
            changed = _selector(before) != _selector(preview)
            risky = old_backends[target] != new_backends[target] or not (_selector(before) and _selector(preview))
        else:
            changed = _template(before) != _template(preview) or old_replicas[target] != new_replicas[target]
            risky = True  # Image and restart rollouts depend on runtime readiness.
        if not changed:
            return ImpactEstimate((), (), (), len(service_index))

        strategy = _field(_field(preview, "spec"), "strategy")
        recreate = verb in ("restart", "set_image") and strategy.get("type") == "Recreate"
        degraded = _services_losing_backends(
            reachable,
            _active_managed_backends(old_services, old_backends, old_replicas),
            _active_managed_backends(new_services, new_backends, new_replicas),
            target if recreate else None,
        )
        uncertain = set(reachable) if risky else set()
        for service in degraded:
            uncertain.update(affected_services(graph, service))

    return ImpactEstimate(reachable, _ordered(degraded), _ordered(uncertain - degraded), len(service_index))
