"""Prepare an identity/version-bound update of exactly one Deployment image."""

from collections.abc import Mapping

from pydantic import Field

from opsguard.contracts import ActionRequest, DnsLabel, ImageReference, SetImageAction, WireModel
from opsguard.intent.resolver import DeploymentIdentity


class ImageConflict(Exception):
    """An explicit precondition rejection: no image write was accepted."""


class PreparedImage(WireModel):
    target: DeploymentIdentity
    container: DnsLabel
    image: ImageReference
    container_index: int = Field(ge=0)

    @property
    def path(self) -> str:
        return f"/apis/apps/v1/namespaces/{self.target.namespace}/deployments/{self.target.name}"

    def body(self) -> list[dict]:
        pointer = f"/spec/template/spec/containers/{self.container_index}"
        return [
            {"op": "test", "path": "/metadata/uid", "value": self.target.uid},
            {"op": "test", "path": "/metadata/resourceVersion", "value": self.target.resource_version},
            {"op": "test", "path": pointer + "/name", "value": self.container},
            {"op": "replace", "path": pointer + "/image", "value": self.image},
        ]


def prepare_set_image(
    request: SetImageAction | ActionRequest | Mapping, *, deployment: dict,
    lab_images: Mapping[tuple[str, str, str], frozenset[str]],
) -> PreparedImage:
    action = SetImageAction.model_validate(request.model_dump() if isinstance(request, ActionRequest) else request)
    metadata = deployment["metadata"]
    target = DeploymentIdentity(namespace=metadata["namespace"], name=metadata["name"],
                                uid=metadata["uid"], resource_version=metadata["resourceVersion"])
    if (action.namespace, action.name) != (target.namespace, target.name) or metadata.get("deletionTimestamp"):
        raise ValueError("image target differs from the live Deployment")
    allowed = lab_images.get((action.namespace, action.name, action.arguments.container), frozenset())
    if action.arguments.image not in allowed:
        raise ValueError("image target, container or value is outside lab bounds")
    containers = deployment["spec"]["template"]["spec"]["containers"]
    indexes = [index for index, container in enumerate(containers) if container["name"] == action.arguments.container]
    if len(indexes) != 1:
        raise ValueError("container does not identify exactly one regular container")
    return PreparedImage(target=target, container=action.arguments.container,
                         image=action.arguments.image, container_index=indexes[0])


async def send_image_mutation(client, mutation: PreparedImage, *, dry_run: bool, timeout_seconds: float) -> dict:
    """Executor adapter; caller supplies the authorized executor identity.

    Preview and commit receive the same immutable preparation. API failures are
    propagated; an uncertain write must be reconciled rather than retried here.
    """
    response = await client.patch(
        mutation.path, params={"dryRun": "All"} if dry_run else None,
        headers={"Content-Type": "application/json-patch+json"},
        json=mutation.body(), timeout=timeout_seconds,
    )
    if response.status_code == 409:
        raise ImageConflict
    return response.raise_for_status().json()
