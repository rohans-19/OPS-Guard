import asyncio

import httpx
import pytest

from opsguard.benchmark.kind_scale_lab import KindScaleLab
from opsguard.benchmark.scale_lab import ScaleConflict
from opsguard.contracts import ActionRequest, ScaleAction
from opsguard.operations.scale import ScaleTarget, prepare_scale


def request(**updates):
    return ActionRequest.model_validate({
        "verb": "scale", "resource": "deployment", "name": "checkout-api", "namespace": "shop",
        "arguments": {"replicas": 3}, **updates,
    })


TARGET = ScaleTarget(namespace="shop", name="checkout-api", uid="uid-1", resource_version="42")
BOUNDS = {("shop", "checkout-api"): (0, 4)}


def test_scale_rejects_wrong_resource_target_and_bounds():
    with pytest.raises(ValueError):
        request(resource="configmap")
    with pytest.raises(ValueError, match="resolved target"):
        prepare_scale(request(name="payments-api"), target=TARGET, lab_bounds=BOUNDS)
    with pytest.raises(ValueError, match="outside lab bounds"):
        prepare_scale(request(arguments={"replicas": 5}), target=TARGET, lab_bounds=BOUNDS)
    with pytest.raises(ValueError):
        prepare_scale(request(arguments={"replicas": "3"}), target=TARGET, lab_bounds=BOUNDS)


def test_preview_and_write_use_same_prepared_scale_body():
    prepared = prepare_scale(ScaleAction.model_validate(request().model_dump()), target=TARGET, lab_bounds=BOUNDS)
    requests = []

    def respond(http_request):
        requests.append(http_request)
        return httpx.Response(200 if len(requests) == 1 else 409)

    async def scenario():
        async with httpx.AsyncClient(base_url="https://127.0.0.1", transport=httpx.MockTransport(respond)) as client:
            lab = KindScaleLab("shop", client, client)
            await lab.replace_scale(prepared, dry_run=True, timeout_seconds=1)
            with pytest.raises(ScaleConflict):
                await lab.replace_scale(prepared, dry_run=False, timeout_seconds=1)

    asyncio.run(scenario())
    preview, commit = requests
    assert preview.method == commit.method == "PUT"
    assert preview.url.path == commit.url.path == "/apis/apps/v1/namespaces/shop/deployments/checkout-api/scale"
    assert preview.content == commit.content
    assert preview.url.params["dryRun"] == "All" and not commit.url.params
    assert prepared.body()["metadata"]["resourceVersion"] == "42"
    assert prepared.body()["metadata"]["uid"] == "uid-1"
