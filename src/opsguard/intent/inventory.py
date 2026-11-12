"""Read namespaced inventory with a caller-supplied read-only Kubernetes client."""

from dataclasses import dataclass
from time import monotonic
from typing import Protocol

import httpx


@dataclass(frozen=True)
class InventorySnapshot:
    namespace: str
    deployments: tuple[dict, ...] = ()
    services: tuple[dict, ...] = ()
    pods: tuple[dict, ...] = ()
    replicasets: tuple[dict, ...] = ()
    endpointslices: tuple[dict, ...] = ()


class InventoryReader(Protocol):
    async def inventory(self, *, timeout_seconds: float) -> InventorySnapshot: ...


class KubernetesInventory:
    def __init__(self, namespace: str, reader: httpx.AsyncClient):
        # Validate before interpolating an API path.
        from pydantic import TypeAdapter
        from opsguard.contracts import DnsLabel

        self.namespace = TypeAdapter(DnsLabel).validate_python(namespace)
        self.reader = reader

    async def inventory(self, *, timeout_seconds: float) -> InventorySnapshot:
        deadline = monotonic() + timeout_seconds
        collected = {}
        for field, api in (
            ("deployments", "/apis/apps/v1"), ("services", "/api/v1"),
            ("pods", "/api/v1"), ("replicasets", "/apis/apps/v1"),
            ("endpointslices", "/apis/discovery.k8s.io/v1"),
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
            collected[field] = tuple(items)
        return InventorySnapshot(namespace=self.namespace, **collected)
