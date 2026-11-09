"""Prepare a Kubernetes Scale replacement without using credentials."""

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from opsguard.contracts import ActionRequest, DnsLabel, ObjectName, ScaleAction, WireModel

Version = Annotated[str, StringConstraints(min_length=1, max_length=256)]


class ScaleTarget(WireModel):
    namespace: DnsLabel
    name: ObjectName
    uid: Version
    resource_version: Version


class PreparedScale(WireModel):
    method: Literal["PUT"] = "PUT"
    target: ScaleTarget
    replicas: int = Field(ge=0, le=2**31 - 1)

    @property
    def path(self) -> str:
        return f"/apis/apps/v1/namespaces/{self.target.namespace}/deployments/{self.target.name}/scale"

    def body(self) -> dict:
        return {
            "apiVersion": "autoscaling/v1",
            "kind": "Scale",
            "metadata": {
                "name": self.target.name, "namespace": self.target.namespace,
                "uid": self.target.uid, "resourceVersion": self.target.resource_version,
            },
            "spec": {"replicas": self.replicas},
        }


def prepare_scale(
    request: ScaleAction | ActionRequest | Mapping, *,
    target: ScaleTarget, lab_bounds: Mapping[tuple[str, str], tuple[int, int]],
) -> PreparedScale:
    """Bind the request to independently read identity and common lab bounds."""
    action = ScaleAction.model_validate(
        request.model_dump() if isinstance(request, ActionRequest) else request
    )
    identity = (action.namespace, action.name)
    if identity != (target.namespace, target.name):
        raise ValueError("resolved target differs from proposed target")
    bounds = lab_bounds.get(identity)
    if bounds is None or not bounds[0] <= action.arguments.replicas <= bounds[1]:
        raise ValueError("scale target or replica count is outside lab bounds")
    return PreparedScale(target=target, replicas=action.arguments.replicas)
