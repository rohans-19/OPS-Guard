"""Resolve a trusted logical target without using text supplied by the model."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from opsguard.contracts import DnsLabel, ObjectName, WireModel
from opsguard.intent.inventory import InventorySnapshot
from opsguard.intent.models import TaskIntent


class DeploymentIdentity(WireModel):
    namespace: DnsLabel
    name: ObjectName
    uid: str = Field(min_length=1, max_length=256)
    resource_version: str = Field(min_length=1, max_length=256)


class TargetResolution(WireModel):
    status: Literal["PASS", "BLOCK", "ESCALATE"]
    reason: str
    target: DeploymentIdentity | None = None
    binding: Literal["named_object", "endpoint_owner_chain", "selector_fallback"] | None = None


def _index(items: tuple[dict, ...], namespace: str) -> dict[str, dict]:
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


def _controller(obj: dict, kind: str) -> dict:
    owners = [ref for ref in obj["metadata"].get("ownerReferences", []) if ref.get("controller") is True]
    if len(owners) != 1 or owners[0].get("kind") != kind:
        raise ValueError("unverified controller")
    return owners[0]


def _check_ref(ref: dict, obj: dict) -> None:
    metadata = obj["metadata"]
    if (not ref.get("uid") or ref["uid"] != metadata.get("uid")
            or ref.get("name") != metadata.get("name") or metadata.get("deletionTimestamp")):
        raise ValueError("stale or terminating owner")


def deployment_for_pod(snapshot: InventorySnapshot, pod: dict) -> dict:
    """Verify both owner UIDs. Labels alone do not establish ownership."""
    if pod["metadata"].get("namespace") != snapshot.namespace or pod["metadata"].get("deletionTimestamp"):
        raise ValueError("pod is outside the active inventory")
    rs_ref = _controller(pod, "ReplicaSet")
    rs = _index(snapshot.replicasets, snapshot.namespace)[rs_ref["name"]]
    _check_ref(rs_ref, rs)
    dep_ref = _controller(rs, "Deployment")
    dep = _index(snapshot.deployments, snapshot.namespace)[dep_ref["name"]]
    _check_ref(dep_ref, dep)
    return dep


def _matches(selector: dict, labels: dict) -> bool:
    if not isinstance(selector, dict) or not selector or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in selector.items()
    ):
        raise ValueError("Service needs a nonempty selector")
    return all(labels.get(key) == value for key, value in selector.items())


def _identity(dep: dict, namespace: str) -> DeploymentIdentity:
    metadata = dep["metadata"]
    if metadata.get("namespace") != namespace or metadata.get("deletionTimestamp"):
        raise ValueError("Deployment is outside active namespace")
    return DeploymentIdentity(namespace=namespace, name=metadata["name"], uid=metadata["uid"],
                              resource_version=metadata["resourceVersion"])


def resolve_target(intent: TaskIntent, snapshot: InventorySnapshot) -> TargetResolution:
    """Fail closed on incomplete inventory; permit fallback only by trusted opt-in."""
    def stop(status, reason):
        return TargetResolution(status=status, reason=reason)

    if snapshot.namespace != intent.namespace:
        return stop("ESCALATE", "INVENTORY_NAMESPACE_MISMATCH")
    try:
        deployments = _index(snapshot.deployments, intent.namespace)
        if intent.target.resource == "deployment":
            dep = deployments.get(intent.target.name)
            if dep is None:
                return stop("BLOCK", "TARGET_NOT_FOUND")
            identity = _identity(dep, intent.namespace)
            if intent.target.uid and identity.uid != intent.target.uid:
                return stop("BLOCK", "TARGET_RECREATED")
            return TargetResolution(status="PASS", reason="TARGET_RESOLVED", target=identity, binding="named_object")

        service = _index(snapshot.services, intent.namespace).get(intent.target.name)
        if service is None:
            return stop("BLOCK", "TARGET_NOT_FOUND")
        metadata = service["metadata"]
        if not metadata.get("uid") or metadata.get("deletionTimestamp"):
            return stop("ESCALATE", "TARGET_UNAVAILABLE")
        if intent.target.uid and metadata["uid"] != intent.target.uid:
            return stop("BLOCK", "TARGET_RECREATED")
        selector = service["spec"].get("selector", {})
        _matches(selector, {})  # Reject selectorless/unsupported Services.
        pods = _index(snapshot.pods, intent.namespace)
        candidates: dict[str, dict] = {}
        for endpoint_slice in snapshot.endpointslices:
            slice_meta = endpoint_slice["metadata"]
            if (slice_meta.get("namespace") != intent.namespace or
                    slice_meta.get("labels", {}).get("kubernetes.io/service-name") != intent.target.name):
                continue
            if slice_meta.get("deletionTimestamp"):
                return stop("ESCALATE", "ENDPOINTS_UNAVAILABLE")
            _check_ref(_controller(endpoint_slice, "Service"), service)
            for endpoint in endpoint_slice.get("endpoints", []):
                conditions = endpoint.get("conditions", {})
                if conditions.get("terminating") is True or conditions.get("ready") is False:
                    continue
                if conditions.get("ready") is not True:
                    return stop("ESCALATE", "ENDPOINT_READINESS_UNKNOWN")
                ref = endpoint.get("targetRef", {})
                if ref.get("kind") != "Pod" or ref.get("namespace", intent.namespace) != intent.namespace:
                    return stop("ESCALATE", "ENDPOINT_TARGET_UNVERIFIED")
                pod = pods[ref["name"]]
                _check_ref(ref, pod)
                if (pod.get("status", {}).get("phase") != "Running" or
                        not _matches(selector, pod["metadata"].get("labels", {}))):
                    return stop("ESCALATE", "ENDPOINT_TARGET_UNVERIFIED")
                dep = deployment_for_pod(snapshot, pod)
                candidates[dep["metadata"]["uid"]] = dep

        binding = "endpoint_owner_chain"
        if not candidates:
            if not intent.allow_selector_fallback:
                return stop("ESCALATE", "NO_READY_ENDPOINTS")
            binding = "selector_fallback"
            for dep in deployments.values():
                if dep["metadata"].get("deletionTimestamp"):
                    continue
                labels = dep["spec"]["template"]["metadata"].get("labels", {})
                if _matches(selector, labels):
                    candidates[dep["metadata"]["uid"]] = dep
        if len(candidates) != 1:
            return stop("ESCALATE", "AMBIGUOUS_TARGET" if candidates else "TARGET_UNRESOLVED")
        identity = _identity(next(iter(candidates.values())), intent.namespace)
        return TargetResolution(status="PASS", reason="TARGET_RESOLVED", target=identity, binding=binding)
    except (KeyError, TypeError, ValueError, AttributeError):
        return stop("ESCALATE", "INVENTORY_UNVERIFIED")
