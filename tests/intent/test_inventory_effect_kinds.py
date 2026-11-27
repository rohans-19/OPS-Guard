"""S3-P4: the inventory reader reads the effect model's extra kinds, Secrets as metadata only."""

import asyncio

import httpx
import pytest

from opsguard.impact.state import ClusterState
from opsguard.intent.inventory import METADATA_ONLY, KubernetesInventory


def test_reads_requested_kinds_and_strips_secret_values():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen[request.url.path] = request.headers.get("accept", "")
        if request.url.path.endswith("/secrets"):
            # A server that ignored the Accept header still must not leak values.
            item = {"metadata": {"name": "db", "namespace": "shop", "annotations": {"x": "y"}},
                    "data": {"password": "c2VjcmV0"}}
        elif request.url.path.endswith("/poddisruptionbudgets"):
            item = {"metadata": {"name": "pdb", "namespace": "shop"}, "spec": {"minAvailable": 1}}
        else:
            return httpx.Response(200, json={"items": []})
        return httpx.Response(200, json={"items": [item]})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api")
    reader = KubernetesInventory("shop", client, effect_kinds=("poddisruptionbudgets", "secrets"))
    snap = asyncio.run(reader.inventory(timeout_seconds=5))
    assert snap.poddisruptionbudgets[0]["spec"] == {"minAvailable": 1}
    assert snap.secrets == ({"metadata": {"name": "db", "namespace": "shop"}},)
    assert seen["/api/v1/namespaces/shop/secrets"] == METADATA_ONLY
    assert seen["/apis/policy/v1/namespaces/shop/poddisruptionbudgets"] != METADATA_ONLY
    assert snap.horizontalpodautoscalers is None  # not requested, so unknown
    state = ClusterState.from_snapshot(snap)
    assert state.knows("PodDisruptionBudget") and not state.knows("HorizontalPodAutoscaler")


def test_rejects_unknown_or_base_kinds():
    client = httpx.AsyncClient(base_url="https://api")
    with pytest.raises(ValueError):
        KubernetesInventory("shop", client, effect_kinds=("pods",))
    with pytest.raises(ValueError):
        KubernetesInventory("shop", client, effect_kinds=("widgets",))
