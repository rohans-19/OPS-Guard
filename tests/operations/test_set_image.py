import asyncio
import json

import httpx
import pytest

from opsguard.benchmark.image_lab import MemoryImageLab, MemoryImageObserver
from opsguard.contracts import SetImageAction
from opsguard.intent.image_gateway import KubernetesImageBackend
from opsguard.operations.set_image import ImageConflict, prepare_set_image, send_image_mutation


def fixture():
    lab = MemoryImageLab({"checkout-api": {"service": "checkout",
        "containers": {"app": "fixture:old", "sidecar": "fixture:sidecar"},
        "healthy_images": ["fixture:old", "fixture:new", "fixture:sidecar"]}})
    action = SetImageAction(verb="set_image", resource="deployment", namespace=lab.namespace, name="checkout-api",
                            arguments={"container": "app", "image": "fixture:new"})
    bounds = {(lab.namespace, "checkout-api", "app"): frozenset({"fixture:new"})}
    return lab, action, bounds


def test_preview_does_not_write_and_commit_preserves_sidecar():
    lab, action, bounds = fixture()
    mutation = prepare_set_image(action, deployment=lab.deployments["checkout-api"], lab_images=bounds)
    observer = MemoryImageObserver(lab)
    async def scenario():
        await lab.replace_image(mutation, dry_run=True, timeout_seconds=1)
        assert observer.image("checkout-api", "app") == "fixture:old"
        await lab.replace_image(mutation, dry_run=False, timeout_seconds=1)
        assert observer.image("checkout-api", "app") == "fixture:new"
        assert observer.image("checkout-api", "sidecar") == "fixture:sidecar"
        assert lab.mutations[0][1] == lab.mutations[1][1]
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["uid", "resourceVersion", "container"])
def test_changed_identity_version_or_container_position_blocks_commit(change):
    lab, action, bounds = fixture()
    mutation = prepare_set_image(action, deployment=lab.deployments["checkout-api"], lab_images=bounds)
    if change == "container":
        lab.deployments["checkout-api"]["spec"]["template"]["spec"]["containers"].reverse()
    else:
        lab.deployments["checkout-api"]["metadata"][change] = "changed"
    with pytest.raises(ImageConflict):
        asyncio.run(lab.replace_image(mutation, dry_run=False, timeout_seconds=1))
    assert not lab.mutations


def test_http_preview_and_commit_use_exact_patch_and_preconditions():
    lab, action, bounds = fixture()
    mutation = prepare_set_image(action, deployment=lab.deployments["checkout-api"], lab_images=bounds)
    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={}) if len(requests) == 1 else httpx.Response(409)
    async def scenario():
        async with httpx.AsyncClient(base_url="https://127.0.0.1", transport=httpx.MockTransport(respond)) as client:
            await send_image_mutation(client, mutation, dry_run=True, timeout_seconds=1)
            with pytest.raises(ImageConflict):
                await send_image_mutation(client, mutation, dry_run=False, timeout_seconds=1)
    asyncio.run(scenario())
    preview, commit = requests
    assert preview.method == commit.method == "PATCH" and preview.content == commit.content
    assert preview.headers["content-type"] == "application/json-patch+json"
    assert preview.url.params["dryRun"] == "All" and not commit.url.params
    operations = json.loads(commit.content)
    assert [item["op"] for item in operations] == ["test", "test", "test", "replace"]
    assert operations[-1]["path"] == "/spec/template/spec/containers/0/image"


def test_typed_image_and_lab_bounds_reject_extra_keys_and_unapproved_values():
    lab, action, bounds = fixture()
    raw = action.model_dump()
    raw["arguments"]["patch"] = {"replicas": 0}
    with pytest.raises(ValueError):
        SetImageAction.model_validate(raw)
    with pytest.raises(ValueError):
        prepare_set_image(action, deployment=lab.deployments["checkout-api"], lab_images={})
    raw = action.model_dump()
    raw["arguments"]["image"] = "image:tag; command"
    with pytest.raises(ValueError):
        SetImageAction.model_validate(raw)


def test_live_backend_separates_reader_and_writer_and_rejects_another_namespace():
    lab, action, bounds = fixture()
    prepared = prepare_set_image(action, deployment=lab.deployments["checkout-api"], lab_images=bounds)
    reads, writes = [], []
    def read(request):
        reads.append(request)
        assert request.method == "GET"
        return httpx.Response(200, json={"items": []})
    def write(request):
        writes.append(request)
        assert request.method == "PATCH"
        return httpx.Response(200, json={})
    async def scenario():
        async with httpx.AsyncClient(base_url="https://127.0.0.1", transport=httpx.MockTransport(read)) as reader:
            async with httpx.AsyncClient(base_url="https://127.0.0.1", transport=httpx.MockTransport(write)) as writer:
                backend = KubernetesImageBackend(lab.namespace, reader, writer)
                await backend.inventory(timeout_seconds=1)
                await backend.replace_image(prepared, dry_run=True, timeout_seconds=1)
                wrong = KubernetesImageBackend("other", reader, writer)
                with pytest.raises(ValueError):
                    await wrong.replace_image(prepared, dry_run=False, timeout_seconds=1)
    asyncio.run(scenario())
    assert len(reads) == 5 and len(writes) == 1
