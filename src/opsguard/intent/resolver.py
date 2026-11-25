"""Resolve a trusted logical target from live state, never from text supplied by the model.

``resolve`` follows the Stage 2 target-resolution algorithm:

    Service hint      -> ready Pods in the Service's EndpointSlices
                         (fallback: Pods matching the Service selector)
                         -> controller root of each Pod
    Deployment or
    StatefulSet hint  -> the named object
    ConfigMap hint    -> the named object
    bare name         -> every Deployment, StatefulSet and Service with that name

    no candidate      -> BLOCK(TARGET_MISSING)
    two or more       -> ESCALATE(TARGET_AMBIGUOUS); one is never picked
    exactly one       -> bind (kind, namespace, name, uid, resourceVersion)

Before a write, ``check_binding`` compares the bound object with a fresh read:
a new UID is BLOCK(TARGET_REPLACED); a new resourceVersion asks the caller to
re-resolve and re-authorize once.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from opsguard.contracts import DnsLabel, ObjectName, WireModel
from opsguard.intent.inventory import InventorySnapshot
from opsguard.intent.models import ConfigPermission, SelectorPermission, TaskIntent

TargetKind = Literal["Deployment", "StatefulSet", "Service", "ConfigMap"]
KindHint = TargetKind | None
_HINT_BY_RESOURCE: dict[str, KindHint] = {
    "service": "Service", "deployment": "Deployment", "statefulset": "StatefulSet",
    "configmap": "ConfigMap", "any": None,
}


class DeploymentIdentity(WireModel):
    """Namespace, name, UID and version of one bound object (any supported kind)."""

    namespace: DnsLabel
    name: ObjectName
    uid: str = Field(min_length=1, max_length=256)
    resource_version: str = Field(min_length=1, max_length=256)


class TargetResolution(WireModel):
    status: Literal["PASS", "BLOCK", "ESCALATE"]
    reason: str
    target: DeploymentIdentity | None = None
    target_kind: TargetKind | None = None
    binding: Literal["named_object", "endpoint_owner_chain", "selector_fallback"] | None = None


class _IncompleteInventory(Exception):
    """A kind the decision needs was not read, so absence cannot be proven."""


def _index(items: tuple[dict, ...] | None, namespace: str) -> dict[str, dict]:
    if items is None:
        raise _IncompleteInventory
    indexed = {}
    for obj in items:
        metadata = obj["metadata"]
        if metadata.get("namespace") != namespace:
            continue
        name = metadata["name"]
        if name in indexed:
            raise ValueError("duplicate inventory identity")
        indexed[name] = obj
    return indexed


def _controller(obj: dict, kind: str | tuple[str, ...]) -> dict:
    kinds = (kind,) if isinstance(kind, str) else kind
    owners = [ref for ref in obj["metadata"].get("ownerReferences", []) if ref.get("controller") is True]
    if len(owners) != 1 or owners[0].get("kind") not in kinds:
        raise ValueError("unverified controller")
    return owners[0]


def _check_ref(ref: dict, obj: dict) -> None:
    metadata = obj["metadata"]
    if (not ref.get("uid") or ref["uid"] != metadata.get("uid")
            or ref.get("name") != metadata.get("name") or metadata.get("deletionTimestamp")):
        raise ValueError("stale or terminating owner")


def controller_root(snapshot: InventorySnapshot, pod: dict) -> tuple[TargetKind, dict]:
    """Follow verified owner UIDs: Pod -> ReplicaSet -> Deployment, or Pod -> StatefulSet.

    Labels alone never establish ownership, and a Pod is never a target.
    """
    if pod["metadata"].get("namespace") != snapshot.namespace or pod["metadata"].get("deletionTimestamp"):
        raise ValueError("pod is outside the active inventory")
    owner = _controller(pod, ("ReplicaSet", "StatefulSet"))
    if owner["kind"] == "StatefulSet":
        sts = _index(snapshot.statefulsets, snapshot.namespace)[owner["name"]]
        _check_ref(owner, sts)
        return "StatefulSet", sts
    rs = _index(snapshot.replicasets, snapshot.namespace)[owner["name"]]
    _check_ref(owner, rs)
    dep_ref = _controller(rs, "Deployment")
    dep = _index(snapshot.deployments, snapshot.namespace)[dep_ref["name"]]
    _check_ref(dep_ref, dep)
    return "Deployment", dep


def deployment_for_pod(snapshot: InventorySnapshot, pod: dict) -> dict:
    """Verify both owner UIDs. Labels alone do not establish ownership."""
    kind, obj = controller_root(snapshot, pod)
    if kind != "Deployment":
        raise ValueError("pod is not owned by a Deployment")
    return obj


def _matches(selector: dict, labels: dict) -> bool:
    if not isinstance(selector, dict) or not selector or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in selector.items()
    ):
        raise ValueError("Service needs a nonempty selector")
    return all(labels.get(key) == value for key, value in selector.items())


def _identity(obj: dict, namespace: str) -> DeploymentIdentity:
    metadata = obj["metadata"]
    if metadata.get("namespace") != namespace or metadata.get("deletionTimestamp"):
        raise ValueError("object is outside the active namespace")
    return DeploymentIdentity(namespace=namespace, name=metadata["name"], uid=metadata["uid"],
                              resource_version=metadata["resourceVersion"])


def _stop(status, reason) -> TargetResolution:
    return TargetResolution(status=status, reason=reason)


def _bind(kind: TargetKind, obj: dict, namespace: str, binding) -> TargetResolution:
    return TargetResolution(status="PASS", reason="TARGET_RESOLVED", target=_identity(obj, namespace),
                            target_kind=kind, binding=binding)


def _choose(candidates: dict[str, tuple[TargetKind, dict]], namespace: str, binding) -> TargetResolution:
    if not candidates:
        return _stop("BLOCK", "TARGET_MISSING")
    if len(candidates) > 1:
        return _stop("ESCALATE", "TARGET_AMBIGUOUS")
    kind, obj = next(iter(candidates.values()))
    return _bind(kind, obj, namespace, binding)


def _service_candidates(snapshot, namespace, service, *, selector_fallback):
    """Controller roots behind one Service, or a stop result."""
    selector = service["spec"].get("selector", {})
    _matches(selector, {})  # Reject selectorless or unsupported Services.
    pods = _index(snapshot.pods, namespace)
    name = service["metadata"]["name"]
    candidates: dict[str, tuple[TargetKind, dict]] = {}
    for endpoint_slice in snapshot.endpointslices:
        slice_meta = endpoint_slice["metadata"]
        if (slice_meta.get("namespace") != namespace or
                slice_meta.get("labels", {}).get("kubernetes.io/service-name") != name):
            continue
        if slice_meta.get("deletionTimestamp"):
            return _stop("ESCALATE", "ENDPOINTS_UNAVAILABLE")
        _check_ref(_controller(endpoint_slice, "Service"), service)
        # The API sends "endpoints": null, not [], for a slice with no endpoints.
        for endpoint in endpoint_slice.get("endpoints") or []:
            conditions = endpoint.get("conditions", {})
            if conditions.get("terminating") is True or conditions.get("ready") is False:
                continue
            if conditions.get("ready") is not True:
                return _stop("ESCALATE", "ENDPOINT_READINESS_UNKNOWN")
            ref = endpoint.get("targetRef", {})
            if ref.get("kind") != "Pod" or ref.get("namespace", namespace) != namespace:
                return _stop("ESCALATE", "ENDPOINT_TARGET_UNVERIFIED")
            pod = pods[ref["name"]]
            _check_ref(ref, pod)
            if (pod.get("status", {}).get("phase") != "Running" or
                    not _matches(selector, pod["metadata"].get("labels", {}))):
                return _stop("ESCALATE", "ENDPOINT_TARGET_UNVERIFIED")
            kind, root = controller_root(snapshot, pod)
            candidates[root["metadata"]["uid"]] = (kind, root)
    if candidates:
        return candidates, "endpoint_owner_chain"
    if not selector_fallback:
        return _stop("ESCALATE", "NO_READY_ENDPOINTS")
    # Controlled fallback: Pods the selector matches, then their verified owners.
    for pod in pods.values():
        if not pod["metadata"].get("deletionTimestamp") and _matches(selector, pod["metadata"].get("labels", {})):
            kind, root = controller_root(snapshot, pod)
            candidates[root["metadata"]["uid"]] = (kind, root)
    if not candidates:
        # A workload scaled to zero has no Pods. Match the Pod templates instead,
        # so "scale checkout up from 0" can still resolve to exactly one owner.
        workloads = [("Deployment", obj) for obj in _index(snapshot.deployments, namespace).values()]
        if snapshot.statefulsets is not None:
            workloads += [("StatefulSet", obj) for obj in _index(snapshot.statefulsets, namespace).values()]
        for kind, obj in workloads:
            labels = obj["spec"]["template"]["metadata"].get("labels", {})
            if not obj["metadata"].get("deletionTimestamp") and _matches(selector, labels):
                candidates[obj["metadata"]["uid"]] = (kind, obj)
    return candidates, "selector_fallback"


def resolve(namespace: str, kind_hint: KindHint, name: str, snapshot: InventorySnapshot, *,
            uid: str | None = None, selector_fallback: bool = True) -> TargetResolution:
    """The Stage 2 resolution algorithm. Fails closed on incomplete or inconsistent inventory."""
    if snapshot.namespace != namespace:
        return _stop("ESCALATE", "INVENTORY_NAMESPACE_MISMATCH")
    try:
        if kind_hint in ("Deployment", "StatefulSet", "ConfigMap"):
            pool = {"Deployment": snapshot.deployments, "StatefulSet": snapshot.statefulsets,
                    "ConfigMap": snapshot.configmaps}[kind_hint]
            obj = _index(pool, namespace).get(name)
            if obj is None:
                return _stop("BLOCK", "TARGET_MISSING")
            if uid and obj["metadata"].get("uid") != uid:
                return _stop("BLOCK", "TARGET_REPLACED")
            return _bind(kind_hint, obj, namespace, "named_object")

        if kind_hint is None:
            candidates = {}
            for kind, pool in (("Deployment", snapshot.deployments), ("StatefulSet", snapshot.statefulsets),
                               ("Service", snapshot.services)):
                obj = _index(pool, namespace).get(name)
                if obj is not None:
                    candidates[obj["metadata"]["uid"]] = (kind, obj)
            if uid and len(candidates) == 1 and uid not in candidates:
                return _stop("BLOCK", "TARGET_REPLACED")
            return _choose(candidates, namespace, "named_object")

        service = _index(snapshot.services, namespace).get(name)
        if service is None:
            return _stop("BLOCK", "TARGET_MISSING")
        metadata = service["metadata"]
        if not metadata.get("uid") or metadata.get("deletionTimestamp"):
            return _stop("ESCALATE", "TARGET_UNAVAILABLE")
        if uid and metadata["uid"] != uid:
            return _stop("BLOCK", "TARGET_REPLACED")
        found = _service_candidates(snapshot, namespace, service, selector_fallback=selector_fallback)
        if isinstance(found, TargetResolution):
            return found
        candidates, binding = found
        return _choose(candidates, namespace, binding)
    except _IncompleteInventory:
        return _stop("ESCALATE", "INVENTORY_INCOMPLETE")
    except (KeyError, TypeError, ValueError, AttributeError):
        return _stop("ESCALATE", "INVENTORY_UNVERIFIED")


def resolve_target(intent: TaskIntent, snapshot: InventorySnapshot) -> TargetResolution:
    """Resolve a TaskIntent's target. v1 intents keep the selector fallback opt-in."""
    if snapshot.namespace != intent.namespace:
        return _stop("ESCALATE", "INVENTORY_NAMESPACE_MISMATCH")
    return resolve(intent.namespace, _HINT_BY_RESOURCE[intent.target.resource], intent.target.name, snapshot,
                   uid=intent.target.uid, selector_fallback=intent.allow_selector_fallback)


def check_binding(bound: DeploymentIdentity, current: DeploymentIdentity | None) -> Literal[
        "BOUND", "TARGET_MISSING", "TARGET_REPLACED", "TARGET_VERSION_CHANGED"]:
    """Compare a bound object with a fresh read just before the write."""
    if current is None or (current.namespace, current.name) != (bound.namespace, bound.name):
        return "TARGET_MISSING"
    if current.uid != bound.uid:
        return "TARGET_REPLACED"
    if current.resource_version != bound.resource_version:
        return "TARGET_VERSION_CHANGED"
    return "BOUND"


def resolve_service(intent: TaskIntent, snapshot: InventorySnapshot) -> TargetResolution:
    """Bind a Service-level write to the live named Service. Its routing may be broken."""
    if snapshot.namespace != intent.namespace:
        return _stop("ESCALATE", "INVENTORY_NAMESPACE_MISMATCH")
    if intent.target.resource != "service":
        return _stop("BLOCK", "TARGET_NOT_A_SERVICE")
    try:
        service = _index(snapshot.services, intent.namespace).get(intent.target.name)
        if service is None:
            return _stop("BLOCK", "TARGET_MISSING")
        metadata = service["metadata"]
        if not metadata.get("uid") or metadata.get("deletionTimestamp"):
            return _stop("ESCALATE", "TARGET_UNAVAILABLE")
        if intent.target.uid and metadata["uid"] != intent.target.uid:
            return _stop("BLOCK", "TARGET_REPLACED")
        if service.get("spec", {}).get("type") == "ExternalName":
            return _stop("BLOCK", "TARGET_NOT_SELECTABLE")
        return _bind("Service", service, intent.namespace, "named_object")
    except (KeyError, TypeError, ValueError, AttributeError):
        return _stop("ESCALATE", "INVENTORY_UNVERIFIED")


def selector_backends(snapshot: InventorySnapshot, selector: dict) -> tuple[str, ...]:
    """Names of live Deployments whose Pod template a selector would match."""
    names = []
    for name, dep in _index(snapshot.deployments, snapshot.namespace).items():
        if dep["metadata"].get("deletionTimestamp"):
            continue
        if _matches(selector, dep["spec"]["template"]["metadata"].get("labels", {})):
            names.append(name)
    return tuple(sorted(names))


def resolve_diagnostic_target(intent: TaskIntent, snapshot: InventorySnapshot) -> TargetResolution:
    """The workload whose evidence a task may read.

    A selector repair cannot follow the Service's endpoints, because they are
    what is broken. Its trusted permission names the intended backend instead.
    """
    permission = next((rule for rule in intent.permissions if isinstance(rule, (SelectorPermission, ConfigPermission))), None)
    if permission is None:
        return resolve_target(intent, snapshot)
    name = permission.backend if isinstance(permission, SelectorPermission) else permission.diagnostic_deployment
    return resolve(intent.namespace, "Deployment", name, snapshot)
