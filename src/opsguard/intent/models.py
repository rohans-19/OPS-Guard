"""Trusted operator configuration, passed separately from model proposals."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, model_validator

from opsguard.contracts import DnsLabel, ImageReference, ObjectName, WireModel


class LogicalTarget(WireModel):
    resource: Literal["service", "deployment"]
    name: ObjectName
    uid: str | None = Field(default=None, min_length=1, max_length=256)


class ScalePermission(WireModel):
    verb: Literal["scale"] = "scale"
    min_replicas: int = Field(ge=0, le=2**31 - 1)
    max_replicas: int = Field(ge=0, le=2**31 - 1)

    @model_validator(mode="after")
    def ordered_bounds(self) -> ScalePermission:
        if self.min_replicas > self.max_replicas:
            raise ValueError("replica bounds are reversed")
        return self


class ImagePermission(WireModel):
    verb: Literal["set_image"] = "set_image"
    container: DnsLabel
    images: tuple[ImageReference, ...] = Field(min_length=1, max_length=20)


class RestartPermission(WireModel):
    verb: Literal["restart"] = "restart"


Permission = Annotated[ScalePermission | ImagePermission | RestartPermission, Field(discriminator="verb")]


class TaskIntent(WireModel):
    """Build only from an operator-confirmed task, never evidence or oracle labels.

    The caller authenticates the operator and stores this configuration. A model
    receives a task ID, not an API for creating or widening this object.
    """

    task_id: DnsLabel
    namespace: DnsLabel
    target: LogicalTarget
    permissions: tuple[Permission, ...] = Field(min_length=1, max_length=3)
    protected_deployments: tuple[ObjectName, ...] = ()
    allow_selector_fallback: bool = False

    @model_validator(mode="after")
    def unique_permissions(self) -> TaskIntent:
        if len({permission.verb for permission in self.permissions}) != len(self.permissions):
            raise ValueError("one permission per operation is required")
        return self
