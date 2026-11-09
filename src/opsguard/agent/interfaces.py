"""Ports used by the agent; adapters hold all I/O and credentials."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, Sequence

from pydantic import Field, TypeAdapter

from opsguard.contracts import JsonValue, ReadRequest, ScaleAction, WireModel


class ReadStep(WireModel):
    kind: Literal["read"]
    request: ReadRequest


class ActionStep(WireModel):
    kind: Literal["action"]
    request: ScaleAction


class FinishStep(WireModel):
    kind: Literal["finish"]
    summary: str = Field(min_length=1, max_length=1_024)


Step = TypeAdapter(Annotated[ReadStep | ActionStep | FinishStep, Field(discriminator="kind")])


@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: str


@dataclass(frozen=True)
class ModelReply:
    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None


class ModelAdapter(Protocol):
    model_id: str

    async def complete(
        self, messages: Sequence[Message], *, schema: dict, timeout_seconds: float,
    ) -> ModelReply: ...


class GatewayResult(WireModel):
    status: Literal["OK", "ALLOW", "BLOCK", "ESCALATE", "ERROR"]
    reason: str = Field(min_length=1, max_length=120)
    decision_id: str | None = Field(default=None, max_length=120)
    data: dict[str, JsonValue] = Field(default_factory=dict)


class Gateway(Protocol):
    async def read(
        self, request: ReadRequest, *, task_id: str, timeout_seconds: float,
    ) -> GatewayResult: ...

    async def propose(
        self, request: ScaleAction, *, task_id: str, timeout_seconds: float,
    ) -> GatewayResult: ...
