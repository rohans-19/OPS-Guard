"""Read namespaced inventory with a caller-supplied read-only Kubernetes client."""

from dataclasses import dataclass
from time import monotonic
from typing import Protocol

import httpx

METADATA_ONLY = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"


@dataclass(frozen=True)
class InventorySnapshot:
    namespace: str
    deployments: tuple[dict, ...] = ()
    services: tuple[dict, ...] = ()
    pods: tuple[dict, ...] = ()
    replicasets: tuple[dict, ...] = ()
    endpointslices: tuple[dict, ...] = ()
    configmaps: tuple[dict, ...] = ()
    # None means StatefulSets were not read, so the resolver cannot prove one
    # is absent and escalates whenever the answer depends on them.
    statefulsets: tuple[dict, ...] | None = None
    # Effect model v1 (S3-P4) kinds. None means not read, never "none exist".
    daemonsets: tuple[dict, ...] | None = None
    jobs: tuple[dict, ...] | None = None
    ingresses: tuple[dict, ...] | None = None
    networkpolicies: tuple[dict, ...] | None = None
    secrets: tuple[dict, ...] | None = None          # metadata only
    persistentvolumeclaims: tuple[dict, ...] | None = None
    horizontalpodautoscalers: tuple[dict, ...] | None = None
    serviceaccounts: tuple[dict, ...] | None = None
    rolebindings: tuple[dict, ...] | None = None
    poddisruptionbudgets: tuple[dict, ...] | None = None


class InventoryReader(Protocol):
    async def inventory(self, *, timeout_seconds: float) -> InventorySnapshot: ...


class KubernetesInventory:
    def __init__(self, namespace: str, reader: httpx.AsyncClient, *, include_statefulsets: bool = False,
                 effect_kinds: tuple[str, ...] = ()):
        # Validate before interpolating an API path.
        from pydantic import TypeAdapter
        from opsguard.contracts import DnsLabel

        self.namespace = TypeAdapter(DnsLabel).validate_python(namespace)
        self.reader = reader
        # Off by default: the lab Roles grant list on deployments and replicasets only.
        self.include_statefulsets = include_statefulsets
        # Extra InventorySnapshot fields for effect model v1, e.g. ("poddisruptionbudgets", ...).
        # The reader identity needs list on each (opsguard.impact.kinds.reader_rules).
        from opsguard.impact.kinds import BASE_FIELDS, BY_FIELD

        unknown = [f for f in effect_kinds if f not in BY_FIELD or f in BASE_FIELDS]
        if unknown:
            raise ValueError(f"unknown or base inventory kinds: {unknown}")
        self.effect_kinds = tuple(dict.fromkeys(effect_kinds))

    async def inventory(self, *, timeout_seconds: float) -> InventorySnapshot:
        deadline = monotonic() + timeout_seconds
        collected = {}
        for field, api in (
            ("deployments", "/apis/apps/v1"), ("services", "/api/v1"),
            ("pods", "/api/v1"), ("replicasets", "/apis/apps/v1"),
            ("endpointslices", "/apis/discovery.k8s.io/v1"),
            *((("statefulsets", "/apis/apps/v1"),) if self.include_statefulsets else ()),
            *self._effect_paths(),
        ):
            items, continuation, seen = [], "", set()
            while True:
                remaining = deadline - monotonic()
                if remaining <= 0:
                    raise TimeoutError("inventory deadline reached")
                response = await self.reader.get(
                    f"{api}/namespaces/{self.namespace}/{field}",
                    params={"limit": 200, **({"continue": continuation} if continuation else {})},
                    timeout=remaining,
                    **({"headers": {"Accept": METADATA_ONLY}} if field in self._metadata_only else {}),
                )
                data = response.raise_for_status().json()
                batch = data["items"]
                if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
                    raise ValueError("invalid inventory list")
                items.extend(batch)
                if len(items) > 5000:
                    raise ValueError("inventory exceeds lab limit")
                continuation = data.get("metadata", {}).get("continue", "")
                if not continuation:
                    break
                if not isinstance(continuation, str) or continuation in seen:
                    raise ValueError("invalid inventory continuation")
                seen.add(continuation)
            if field in self._metadata_only:
                # Keep names, labels and owners only, even if a server ignored the Accept header.
                items = [{"metadata": {k: v for k, v in (item.get("metadata") or {}).items()
                                       if k in ("name", "namespace", "uid", "labels", "ownerReferences")}}
                         for item in items]
            collected[field] = tuple(items)
        return InventorySnapshot(namespace=self.namespace, **collected)

    @property
    def _metadata_only(self) -> frozenset[str]:
        from opsguard.impact.kinds import BY_FIELD

        return frozenset(f for f in self.effect_kinds if BY_FIELD[f].metadata_only)

    def _effect_paths(self):
        from opsguard.impact.kinds import BY_FIELD

        return tuple((field, BY_FIELD[field].api_prefix) for field in self.effect_kinds
                     if not (field == "statefulsets" and self.include_statefulsets))
