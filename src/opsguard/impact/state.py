"""A read-only view of namespaced cluster state for the effect model.

``ClusterState`` holds wire-format objects by kind. A kind that was never read
is *unknown*, which is different from *read and empty*: a rule that depends on
an unknown kind reports uncertainty instead of assuming nothing is there.

States are immutable in use: ``apply`` returns a new state with one previewed
object in place, so the episode can replay approved changes on its start
snapshot without touching live data.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Optional

from opsguard.impact.kinds import BY_FIELD, BY_KIND, WORKLOAD_KINDS

Object = Mapping[str, object]


@dataclass(frozen=True, order=True)
class ObjectRef:
    kind: str
    namespace: str
    name: str

    def __post_init__(self) -> None:
        if not self.kind or not self.namespace or not self.name:
            raise ValueError("an object reference needs a kind, namespace and name")

    def __str__(self) -> str:
        return f"{self.kind}/{self.namespace}/{self.name}"


def _mapping(value: object, what: str) -> Mapping:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{what} must be an object")
    return value


def meta(obj: Object) -> Mapping:
    return _mapping(obj.get("metadata"), "metadata")


def spec(obj: Object) -> Mapping:
    return _mapping(obj.get("spec"), "spec")


def status(obj: Object) -> Mapping:
    return _mapping(obj.get("status"), "status")


def ref_of(obj: Object, kind: Optional[str] = None) -> ObjectRef:
    kind = kind or obj.get("kind")
    if not isinstance(kind, str) or kind not in BY_KIND:
        raise ValueError(f"unsupported or missing kind: {kind!r}")
    if obj.get("kind") not in (None, kind):
        raise ValueError(f"expected {kind}, got {obj.get('kind')!r}")
    m = meta(obj)
    return ObjectRef(kind, m.get("namespace"), m.get("name"))


def labels(obj: Object) -> Mapping:
    return _mapping(meta(obj).get("labels"), "labels")


def pod_template(obj: Object) -> Mapping:
    return _mapping(spec(obj).get("template"), "template")


def selector_matches(selector: Mapping, pod_labels: Mapping) -> bool:
    """Equality-based selector, or a LabelSelector with matchLabels and matchExpressions."""
    if not selector:
        return False
    if "matchLabels" in selector or "matchExpressions" in selector:
        match = _mapping(selector.get("matchLabels"), "matchLabels")
        if any(pod_labels.get(k) != v for k, v in match.items()):
            return False
        for expression in selector.get("matchExpressions") or ():
            key, operator = expression.get("key"), expression.get("operator")
            values = set(expression.get("values") or ())
            present = key in pod_labels
            if operator == "In" and not (present and pod_labels[key] in values):
                return False
            if operator == "NotIn" and present and pod_labels[key] in values:
                return False
            if operator == "Exists" and not present:
                return False
            if operator == "DoesNotExist" and present:
                return False
            if operator not in ("In", "NotIn", "Exists", "DoesNotExist"):
                raise ValueError(f"unsupported selector operator: {operator!r}")
        return bool(match) or bool(selector.get("matchExpressions"))
    return all(pod_labels.get(k) == v for k, v in selector.items())


@dataclass(frozen=True)
class ClusterState:
    namespace: str
    objects: Mapping[str, Mapping[ObjectRef, Object]] = field(default_factory=dict)
    read: frozenset[str] = frozenset()
    # Workloads changed inside the episode: their Pods in the snapshot are stale,
    # so capacity comes from the spec instead.
    stale: frozenset[ObjectRef] = frozenset()

    # -- construction -----------------------------------------------------------
    @classmethod
    def from_objects(cls, namespace: str, items: Iterable[Object], *, read: Optional[Iterable[str]] = None):
        """Build from manifests. ``read`` lists kinds known to be complete; default: the kinds present."""
        pools: dict[str, dict[ObjectRef, Object]] = {}
        for item in items:
            if item.get("kind") not in BY_KIND:
                continue
            obj = deepcopy(dict(item))
            obj.setdefault("metadata", {}).setdefault("namespace", namespace)
            ref = ref_of(obj)
            if ref.namespace != namespace:
                raise ValueError(f"{ref} is outside namespace {namespace}")
            pool = pools.setdefault(ref.kind, {})
            if ref in pool:
                raise ValueError(f"duplicate object: {ref}")
            pool[ref] = obj
        known = frozenset(read) if read is not None else frozenset(pools)
        unknown = set(known) - set(BY_KIND)
        if unknown:
            raise ValueError(f"unknown kinds: {sorted(unknown)}")
        return cls(namespace, {kind: dict(pool) for kind, pool in pools.items()}, known)

    @classmethod
    def from_snapshot(cls, snapshot) -> ClusterState:
        """From an ``InventorySnapshot``. Fields that are None were not read."""
        items, read = [], set()
        for name, info in BY_FIELD.items():
            value = getattr(snapshot, name, None)
            if value is None:
                continue
            read.add(info.kind)
            for obj in value:
                items.append({**obj, "kind": obj.get("kind") or info.kind})
        return cls.from_objects(snapshot.namespace, items, read=read)

    # -- access -------------------------------------------------------------------
    def knows(self, kind: str) -> bool:
        return kind in self.read

    def items(self, kind: str) -> dict[ObjectRef, Object]:
        return dict(self.objects.get(kind, {}))

    def get(self, ref: ObjectRef) -> Optional[Object]:
        return self.objects.get(ref.kind, {}).get(ref)

    def workloads(self) -> dict[ObjectRef, Object]:
        out: dict[ObjectRef, Object] = {}
        for kind in sorted(WORKLOAD_KINDS):
            out.update(self.items(kind))
        return out

    def owner_refs(self, obj: Object) -> list[ObjectRef]:
        ns = meta(obj).get("namespace")
        refs = []
        for owner in meta(obj).get("ownerReferences") or ():
            kind, name = owner.get("kind"), owner.get("name")
            if kind in BY_KIND and name:
                refs.append(ObjectRef(kind, ns, name))
        return refs

    def owning_workload(self, ref: ObjectRef) -> Optional[ObjectRef]:
        """Follow ownerReferences from a Pod or ReplicaSet to its root workload, if it is in the state."""
        seen = set()
        current = ref
        while current not in seen:
            seen.add(current)
            if current.kind in WORKLOAD_KINDS:
                return current
            obj = self.get(current)
            if obj is None:
                return None
            owners = self.owner_refs(obj)
            if not owners:
                return None
            current = owners[0]
        return None

    # -- change -------------------------------------------------------------------
    def apply(self, obj: Object, *, delete: bool = False) -> ClusterState:
        """This state with one object replaced (or removed). A Scale body merges its replica count."""
        kind = obj.get("kind")
        if kind == "Scale":
            m = meta(obj)
            target = next((r for r in self.workloads() if r.name == m.get("name")), None)
            if target is None:
                raise ValueError("a Scale body needs its workload in the state")
            merged = deepcopy(dict(self.get(target)))
            merged.setdefault("spec", {})["replicas"] = spec(obj).get("replicas")
            obj = merged
        ref = ref_of(obj)
        pools = {k: dict(v) for k, v in self.objects.items()}
        pool = pools.setdefault(ref.kind, {})
        if delete:
            pool.pop(ref, None)
        else:
            pool[ref] = deepcopy(dict(obj))
        stale = self.stale | ({ref} if ref.kind in WORKLOAD_KINDS else set())
        return ClusterState(self.namespace, pools, self.read | {ref.kind}, stale)
