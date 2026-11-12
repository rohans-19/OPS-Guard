"""Shared CaseSpec and RunResult contracts for the common benchmark runner.

A case declares its legitimate and forbidden outcomes independently, as named
state predicates. Predicates, replay actions and labels stay in the harness and
are never placed in a model prompt.
"""

from __future__ import annotations

from typing import Literal

from pydantic import Field, JsonValue, model_validator

from opsguard.contracts import DnsLabel, ObjectName, Verb, WireModel

SCHEMA_VERSION = 1
Channel = Literal["logs", "alerts", "runbooks", "configmaps"]
CHANNELS: tuple[str, ...] = ("logs", "alerts", "runbooks", "configmaps")
# The four primary conditions, then the policy-only ablation.
PRIMARY_CONDITIONS: tuple[str, ...] = ("unprotected", "hardened", "llm-judge", "opsguard")
CONDITIONS: tuple[str, ...] = (*PRIMARY_CONDITIONS, "task-policy")
Condition = Literal["unprotected", "hardened", "llm-judge", "opsguard", "task-policy"]
# Agent stop states that mean the trial itself failed, not that a defence worked.
AGENT_ERROR_STATES = frozenset({"MODEL_ERROR", "GATEWAY_ERROR", "DEADLINE", "CANCELLED"})


class InjectionSpec(WireModel):
    channel: Channel
    # The fixture workload whose evidence carries the payload.
    source_ref: ObjectName
    payload: str = Field(min_length=1, max_length=512)


class PredicateSpec(WireModel):
    """A registered oracle predicate and its JSON arguments."""

    predicate: str = Field(min_length=1, max_length=80, pattern=r"^[a-z][a-z0-9_]*$")
    args: dict[str, JsonValue] = Field(default_factory=dict)


class ReplayAction(WireModel):
    """A typed action without a namespace. The harness adds the trial namespace."""

    verb: Verb
    resource: Literal["deployment", "configmap", "service"]
    name: ObjectName
    arguments: dict[str, JsonValue]


class ReplaySpec(WireModel):
    """Harness-only scripted proposals for the clean replay and the controlled attack replay."""

    legitimate: ReplayAction
    forbidden: ReplayAction | None = None


class CaseSpec(WireModel):
    id: DnsLabel
    owner: DnsLabel
    # Fixture file relative to benchmark/fixtures, and the trusted task inside it.
    fixture: str = Field(min_length=1, max_length=200, pattern=r"^[a-z0-9][a-z0-9._/-]*\.json$")
    task: DnsLabel
    operation: Verb
    # May contain the literal "{namespace}", which the runner fills per trial.
    user_task: str = Field(min_length=1, max_length=1024)
    injection: InjectionSpec | None = None
    legitimate: PredicateSpec
    forbidden: PredicateSpec | None = None
    # Optional: an unintended change outside the task, scored for every case.
    collateral: PredicateSpec | None = None
    deadline_seconds: int = Field(ge=10, le=600)
    replay: ReplaySpec

    @model_validator(mode="after")
    def independent_outcomes(self) -> CaseSpec:
        if (self.injection is None) != (self.forbidden is None):
            raise ValueError("an attacked case needs both an injection and a forbidden predicate; a clean case neither")
        if (self.replay.forbidden is None) != (self.forbidden is None):
            raise ValueError("an attacked case needs a controlled forbidden replay action; a clean case none")
        if self.forbidden is not None and self.forbidden == self.legitimate:
            raise ValueError("legitimate and forbidden outcomes must be declared independently")
        if self.replay.legitimate.verb != self.operation:
            raise ValueError("the legitimate replay must use the case operation")
        if self.replay.forbidden is not None and self.replay.forbidden == self.replay.legitimate:
            raise ValueError("the forbidden replay must differ from the legitimate action")
        if ".." in self.fixture.split("/"):
            raise ValueError("fixture path must stay inside benchmark/fixtures")
        return self

    @property
    def attacked(self) -> bool:
        return self.injection is not None

    @property
    def channel(self) -> str | None:
        return self.injection.channel if self.injection else None


class RunResult(WireModel):
    """One attempt of one matrix cell. Harness errors are identified by `error`.

    `legitimate_met` and `forbidden_seen` are independent oracle outcomes. On a
    harness error either may be None; an errored row is never evidence that an
    attack was prevented.
    """

    schema_version: Literal[1] = SCHEMA_VERSION
    run_id: str = Field(min_length=8, max_length=64)
    trial_id: str = Field(min_length=8, max_length=64)
    attempt: int = Field(ge=1)
    experiment_sha256: str = Field(min_length=64, max_length=64)
    owner: DnsLabel
    case_id: DnsLabel
    base_task_id: DnsLabel
    operation: Verb
    channel: Channel | None
    backend: Literal["memory", "kind"]
    provider: str = Field(min_length=1, max_length=40)
    model_id: str = Field(min_length=1, max_length=200)
    judge_model: str | None = None
    condition: Condition
    repeat: int = Field(ge=1)
    seed: int | None = None
    legitimate_met: bool | None
    forbidden_seen: bool | None
    collateral_seen: bool | None = None
    safe_completion: bool | None = None
    evidence_exposed: bool | None = None
    delivery: str | None = None
    agent_status: str | None = None
    steps: int | None = None
    decisions: list[dict[str, JsonValue]] = Field(default_factory=list)
    task_ms: float | None = None
    setup_ms: float | None = None
    elapsed_ms: float = Field(ge=0)
    error: str | None = None
    trace_ref: str | None = None
    namespace: str | None = None
    started_at: str

    @model_validator(mode="after")
    def outcome_shape(self) -> RunResult:
        if self.error is None and type(self.legitimate_met) is not bool:
            raise ValueError("a completed trial records the legitimate outcome")
        if self.error is None and (self.channel is not None) != (type(self.forbidden_seen) is bool):
            raise ValueError("attacked trials record a forbidden outcome; clean trials record none")
        return self
