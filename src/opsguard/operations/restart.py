"""Prepare one conditional Deployment restart with a code-owned marker."""

from uuid import uuid4

from opsguard.contracts import RestartAction, WireModel
from opsguard.intent.resolver import DeploymentIdentity

RESTART_ANNOTATION = "opsguard.dev/restarted-at"


class PreparedRestart(WireModel):
    target: DeploymentIdentity
    marker: str
    annotations_present: bool

    @property
    def path(self) -> str:
        return f"/apis/apps/v1/namespaces/{self.target.namespace}/deployments/{self.target.name}"

    def body(self) -> list[dict]:
        patches = [
            {"op": "test", "path": "/metadata/uid", "value": self.target.uid},
            {"op": "test", "path": "/metadata/resourceVersion", "value": self.target.resource_version},
        ]
        if self.annotations_present:
            patches.append({"op": "add", "path": "/spec/template/metadata/annotations/opsguard.dev~1restarted-at",
                            "value": self.marker})
        else:
            patches.append({"op": "add", "path": "/spec/template/metadata/annotations",
                            "value": {RESTART_ANNOTATION: self.marker}})
        return patches


def prepare_restart(request, *, deployment: dict, allowed_targets: frozenset[tuple[str, str]]) -> PreparedRestart:
    action = RestartAction.model_validate(request.model_dump() if hasattr(request, "model_dump") else request)
    metadata = deployment["metadata"]
    target = DeploymentIdentity(namespace=metadata["namespace"], name=metadata["name"],
                                uid=metadata["uid"], resource_version=metadata["resourceVersion"])
    identity = (action.namespace, action.name)
    if identity not in allowed_targets or identity != (target.namespace, target.name) or metadata.get("deletionTimestamp"):
        raise ValueError("restart target is outside lab bounds or no longer live")
    template_metadata = deployment["spec"]["template"]["metadata"]
    annotations = template_metadata.get("annotations")
    if annotations is not None and not isinstance(annotations, dict):
        raise ValueError("invalid template annotations")
    return PreparedRestart(target=target, marker=uuid4().hex, annotations_present=annotations is not None)
