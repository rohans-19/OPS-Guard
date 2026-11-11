"""Wire contracts shared by the agent and gateway."""

from __future__ import annotations

import json
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, StringConstraints, model_validator

DnsLabel = Annotated[str, StringConstraints(
    min_length=1, max_length=63, pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$"
)]
ObjectName = Annotated[str, StringConstraints(
    min_length=1, max_length=253,
    pattern=r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)*$",
)]
Resource = Literal["deployment", "configmap", "service"]
Verb = Literal["scale", "set_image", "restart", "set_config", "set_selector"]

_RESOURCE_BY_VERB = {
    "scale": "deployment", "set_image": "deployment", "restart": "deployment",
    "set_config": "configmap", "set_selector": "service",
}


class WireModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, revalidate_instances="always")


class ActionRequest(WireModel):
    """Per-verb arguments are validated by the registered operation handler."""

    verb: Verb
    resource: Resource
    namespace: DnsLabel
    name: ObjectName
    arguments: dict[str, JsonValue]

    @model_validator(mode="after")
    def match_operation_to_resource(self) -> ActionRequest:
        if self.resource != _RESOURCE_BY_VERB[self.verb]:
            raise ValueError("verb does not support this resource")
        return self


class ScaleArguments(WireModel):
    replicas: int = Field(ge=0, le=2**31 - 1)


class ScaleAction(ActionRequest):
    verb: Literal["scale"]
    resource: Literal["deployment"]
    arguments: ScaleArguments


ImageReference = Annotated[str, StringConstraints(
    min_length=1, max_length=512, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9._/@:+-]*$",
)]


class ImageArguments(WireModel):
    container: DnsLabel
    image: ImageReference


class SetImageAction(ActionRequest):
    verb: Literal["set_image"]
    resource: Literal["deployment"]
    arguments: ImageArguments


class RestartArguments(WireModel):
    """The caller cannot supply a timestamp, patch, or restart marker."""


class RestartAction(ActionRequest):
    verb: Literal["restart"]
    resource: Literal["deployment"]
    arguments: RestartArguments


SupportedAction = Annotated[ScaleAction | SetImageAction | RestartAction, Field(discriminator="verb")]


class DeploymentRead(WireModel):
    tool: Literal["get_deployment"]
    namespace: DnsLabel
    name: ObjectName


class PodLogsRead(WireModel):
    """The trusted reader resolves this Deployment name to a scoped Pod."""

    tool: Literal["read_pod_logs"]
    namespace: DnsLabel
    name: ObjectName
    tail_lines: int = Field(default=20, ge=1, le=50)


class RunbookRead(WireModel):
    tool: Literal["read_runbook"]
    namespace: DnsLabel
    name: ObjectName
    runbook_id: DnsLabel


class AlertsRead(WireModel):
    tool: Literal["read_alerts"]
    namespace: DnsLabel
    name: ObjectName


class ConfigMapRead(WireModel):
    tool: Literal["read_configmap"]
    namespace: DnsLabel
    name: ObjectName


ReadRequest = Annotated[DeploymentRead | PodLogsRead | RunbookRead | AlertsRead | ConfigMapRead, Field(discriminator="tool")]


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    values: dict[str, object] = {}
    for key, value in pairs:
        if key in values:
            raise ValueError("duplicate JSON key")
        values[key] = value
    return values


def _reject_constant(_constant: str) -> None:
    raise ValueError("non-finite JSON constant")


def parse_json(text: str, *, max_bytes: int = 16_384) -> object:
    """Reject ambiguous or oversized input before typed validation."""
    if not isinstance(text, str) or len(text.encode("utf-8")) > max_bytes:
        raise ValueError("invalid JSON message size")
    return json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
