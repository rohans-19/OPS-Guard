"""Shared CaseSpec and RunResult contracts for the common benchmark runner.

A case declares its legitimate and forbidden outcomes independently, as named
state predicates. Predicates, replay actions and labels stay in the harness and
are never placed in a model prompt.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, JsonValue, model_validator

from opsguard.contracts import DnsLabel, ObjectName, Verb, WireModel

SCHEMA_VERSION = 1
Channel = Literal["logs", "alerts", "runbooks", "configmaps", "events"]
CHANNELS: tuple[str, ...] = ("logs", "alerts", "runbooks", "configmaps", "events")
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
    # Stage 2 observer: worst severity tier at any sample, and which partial harms were seen.
    worst_severity: Literal["S0", "S1", "S2", "S3", "S4", "S5"] | None = None
    partial_harm: dict[str, bool] = Field(default_factory=dict)
    samples: int | None = None

    @model_validator(mode="after")
    def outcome_shape(self) -> RunResult:
        if self.error is None and type(self.legitimate_met) is not bool:
            raise ValueError("a completed trial records the legitimate outcome")
        if self.error is None and (self.channel is not None) != (type(self.forbidden_seen) is bool):
            raise ValueError("attacked trials record a forbidden outcome; clean trials record none")
        return self


# ---------------------------------------------------------------------------
# Schema v2 (Stage 2, S2-P5): one case and one result format for every suite
# ---------------------------------------------------------------------------

SCHEMA_V2 = 2
Goal = Literal["none", "G1", "G2", "G3", "G4", "G5", "G6", "G7", "G8"]
Severity = Literal["S0", "S1", "S2", "S3", "S4", "S5"]
SEVERITY_ORDER: tuple[str, ...] = ("S0", "S1", "S2", "S3", "S4", "S5")
Suite = Literal["selector", "scale", "image", "restart", "config", "pilot"]
SUITES: tuple[str, ...] = ("selector", "scale", "image", "restart", "config", "pilot")
CaseId = Annotated[str, Field(pattern=r"^[a-z0-9][a-z0-9-]{0,99}$")]


class ChangeSpec(WireModel):
    """One change the task is allowed to make. Anything else is collateral."""

    kind: Literal["Deployment", "Service", "ConfigMap"]
    name: ObjectName
    field: str = Field(min_length=1, max_length=200)


class SeverityDetector(WireModel):
    """When the observer sees `signal`, the trial reaches at least `tier`."""

    signal: Literal["forbidden_seen", "collateral_seen", "service_outage", "replicas_reduced", "probes_failed"]
    tier: Severity


class OracleSpecV2(WireModel):
    """The executable checks that score a trial, independent of the agent's words."""

    legitimate: str = Field(min_length=1, max_length=400)
    forbidden: str | None = Field(default=None, min_length=1, max_length=400)


class SourceRef(WireModel):
    file: str = Field(min_length=1, max_length=200, pattern=r"^benchmark/cases/[a-z0-9_/.-]+\.json$")
    id: CaseId


class CaseSpecV2(WireModel):
    """A benchmark case in the common format. Converters build these from every suite's files.

    `goal` and `oracle` have no defaults, so a case without them fails validation.
    """

    schema_version: Literal[2] = SCHEMA_V2
    id: CaseId
    suite: Suite
    operation: Verb
    goal: Goal
    channel: Channel | None
    payload: str | None = Field(default=None, max_length=2048)
    payload_position: Literal["whole", "middle", "none"]
    family_id: CaseId
    split: Literal["dev", "val", "test"]
    allowed_changes: tuple[ChangeSpec, ...] = Field(min_length=1, max_length=10)
    invariants: tuple[str, ...] = Field(min_length=1, max_length=10)
    severity_detectors: tuple[SeverityDetector, ...] = Field(min_length=1, max_length=10)
    oracle: OracleSpecV2
    source: SourceRef

    @model_validator(mode="after")
    def consistent(self) -> CaseSpecV2:
        attacked = self.channel is not None
        if attacked != (self.payload is not None) or attacked != (self.oracle.forbidden is not None):
            raise ValueError("an attacked case needs a channel, a payload and a forbidden oracle; a clean case none")
        if attacked == (self.goal == "none"):
            raise ValueError("an attacked case names an adversary goal; a clean case has goal none")
        if attacked == (self.payload_position == "none"):
            raise ValueError("payload_position is none exactly for clean cases")
        if attacked and not any(d.signal == "forbidden_seen" for d in self.severity_detectors):
            raise ValueError("an attacked case needs a detector for its forbidden outcome")
        return self

    @property
    def attacked(self) -> bool:
        return self.channel is not None


class DecisionRecord(WireModel):
    status: str = Field(min_length=1, max_length=20)
    reason: str = Field(min_length=1, max_length=100)


class RunResultV2(WireModel):
    """One trial of one case under one condition, the same shape for every suite."""

    schema_version: Literal[2] = SCHEMA_V2
    result_id: str = Field(min_length=8, max_length=64)
    case_id: CaseId
    suite: Suite
    goal: Goal
    channel: Channel | None
    condition: str = Field(min_length=1, max_length=40)
    suite_condition: str = Field(min_length=1, max_length=40)
    backend: Literal["memory", "kind"]
    model_id: str = Field(min_length=1, max_length=200)
    repeat: int = Field(ge=1)
    intent_id: str | None = None
    legitimate_met: bool | None
    forbidden_seen: bool | None
    collateral_seen: bool | None = None
    worst_severity: Severity | None
    partial_harm: dict[str, bool] = Field(default_factory=dict)
    decisions: tuple[DecisionRecord, ...] = ()
    tokens: dict[str, int | None] = Field(default_factory=lambda: {"input": None, "output": None})
    cost_usd: float | None = None
    samples: int | None = None
    error: str | None = None
    elapsed_ms: float = Field(ge=0)

    @model_validator(mode="after")
    def outcome_shape(self) -> RunResultV2:
        if self.error is None:
            if type(self.legitimate_met) is not bool:
                raise ValueError("a completed trial records the legitimate outcome")
            if (self.channel is not None) != (type(self.forbidden_seen) is bool):
                raise ValueError("attacked trials record a forbidden outcome; clean trials record none")
            if self.worst_severity is None:
                raise ValueError("a completed trial records its worst severity tier")
        return self
