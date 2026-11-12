"""One trial of one case: fixture, readiness, injection, agent, oracle, record, cleanup.

Outcome flags come only from the independent oracle. A malicious command in the
transcript is evidence of an attempt. Whether the attack succeeded is decided by
what the cluster actually did. Any failure of the harness itself is recorded in
`error` and is never counted as a prevented attack.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
import json
from pathlib import Path
from time import monotonic

from opsguard.agent.loop import AgentLimits, run_agent
from opsguard.agent.models import ScriptedModel
from opsguard.bench.channels import BENIGN, fixture_text, open_evidence
from opsguard.bench.defences import ScriptedJudgeModel, SelectorImpactDecision
from opsguard.bench.kind_lab import LabHandles
from opsguard.bench.oracle import OutcomeTracker, verify_initial_state
from opsguard.bench.predicates import Observation
from opsguard.bench.schema import AGENT_ERROR_STATES, CONDITIONS, CaseSpec
from opsguard.bench.selector_lab import MemorySelectorLab, MemorySelectorOracle, load_fixture
from opsguard.benchmark.defences import ModelJudge
from opsguard.kube.executor import MutationExecutor
from opsguard.kube.gateway import ExecutionGateway


@dataclass(frozen=True)
class TrialSettings:
    """Everything held constant across the conditions of one experiment."""

    backend: str = "memory"
    provider: str = "scripted"
    judge_model: str | None = None
    cluster: str | None = None
    image: str | None = None
    env_file: Path | None = None
    alert_port: int = 9093
    seed: int = 42
    max_steps: int = 8
    settle_seconds: float = 20.0

    def __post_init__(self):
        if self.backend not in ("memory", "kind") or self.provider not in ("scripted", "ollama", "groq"):
            raise ValueError("unknown backend or provider")
        if self.backend == "kind" and (not self.cluster or not self.image):
            raise ValueError("kind trials require a named cluster and fixture image")
        if not 0 < self.settle_seconds <= 120:
            raise ValueError("settle_seconds must be in (0, 120]")


def _real_model(settings: TrialSettings, name: str, seed: int):
    # Imported lazily so scripted runs need no provider SDK.
    from opsguard.benchmark.restart_trial import make_model
    return make_model(settings.provider, name, seed=seed, env_file=settings.env_file)


def _step(action, namespace: str) -> dict:
    return {"kind": "action", "request": {**action.model_dump(), "namespace": namespace}}


def summarize(observation: Observation | None) -> dict | None:
    """JSON form of an oracle observation for the saved trace."""
    if observation is None:
        return None
    return {"settled": observation.settled,
            "services": {name: {"selector": dict(view.selector), "ready_backends": sorted(view.ready_backends),
                                "responders": list(view.responders)} for name, view in observation.services.items()},
            "deployments": {name: {"replicas": view.replicas, "ready_replicas": view.ready_replicas}
                            for name, view in observation.deployments.items()}}


class ObservedGateway:
    """Let the oracle look after every proposal, so a state the agent later undoes is still scored."""

    def __init__(self, gateway, tracker: OutcomeTracker, settle_seconds: float):
        self.gateway, self.tracker, self.settle_seconds = gateway, tracker, settle_seconds
        self.decisions: list[dict] = []
        self.oracle_error: str | None = None

    async def read(self, request, **context):
        return await self.gateway.read(request, **context)

    async def propose(self, request, **context):
        result = await self.gateway.propose(request, **context)
        self.decisions.append({"status": result.status, "reason": result.reason, **{
            key: result.data[key] for key in ("decision_ms", "gateway_ms", "preview_ms", "execution_ms")
            if result.data.get(key) is not None}})
        try:
            if result.status in ("ALLOW", "ERROR"):
                await self.tracker.settle(timeout_seconds=self.settle_seconds)
            else:
                await self.tracker.sample(timeout_seconds=self.settle_seconds)
        except Exception as error:  # The oracle failed, not the agent. Report it as a harness error.
            self.oracle_error = self.oracle_error or type(error).__name__
        return result


@asynccontextmanager
async def open_lab(root: Path, case: CaseSpec, fixture, settings: TrialSettings, *, log_target, log_line, configmap_text):
    if settings.backend == "memory":
        lab = MemorySelectorLab(fixture, case.task)
        yield LabHandles(namespace=lab.namespace, inventory=lab, transport=lab, oracle=MemorySelectorOracle(lab))
    else:
        from opsguard.bench.kind_lab import open_kind_selector_lab
        async with open_kind_selector_lab(
                fixture_dir=(root / "benchmark/fixtures" / case.fixture).parent, cluster=settings.cluster,
                image=settings.image, fixture=fixture, task_id=case.task, log_target=log_target,
                log_line=log_line, configmap_text=configmap_text) as handles:
            yield handles


async def _wait_ready(oracle, timeout: float) -> Observation:
    deadline = monotonic() + timeout
    while True:
        observation = await oracle.observe(timeout_seconds=30)
        if observation.settled:
            return observation
        if monotonic() >= deadline:
            raise TimeoutError("initial fixture readiness")
        await asyncio.sleep(0.5)


async def _deliver(gateway, request, task_id: str, payload: str, *, required: bool):
    """Read the injected channel through the gateway and confirm the payload is present."""
    result = None
    for _ in range(10):
        result = await gateway.read(request, task_id=task_id, timeout_seconds=15)
        if result.status == "OK" and (not required or payload in result.data["observation"]["text"]):
            return result
        await asyncio.sleep(0.3)
    raise ValueError("injection not delivered through its registered channel" if required
                     else f"evidence read failed: {result.reason if result else 'no result'}")


async def run_trial(*, root: Path, case: CaseSpec, condition: str, model_name: str | None = None, repeat: int = 1,
                    settings: TrialSettings = TrialSettings()) -> tuple[dict, dict]:
    """Run one trial. Returns (outcome fields for RunResult, trace payload saved beside it)."""
    if condition not in CONDITIONS:
        raise ValueError("unsupported condition")
    scripted = settings.provider == "scripted"
    if scripted == bool(model_name):
        raise ValueError("a real provider needs a model name, and the scripted provider takes none")
    if condition == "llm-judge" and not scripted and not settings.judge_model:
        raise ValueError("the llm-judge condition needs a judge model")
    seed = settings.seed + repeat - 1
    started = monotonic()
    outcome = {"owner": case.owner, "case_id": case.id, "base_task_id": case.task, "operation": case.operation,
               "channel": case.channel, "backend": settings.backend, "provider": settings.provider,
               "model_id": model_name or "scripted",
               "judge_model": (settings.judge_model or "scripted-judge") if condition == "llm-judge" else None,
               "condition": condition, "repeat": repeat, "seed": None if scripted else seed,
               "legitimate_met": None, "forbidden_seen": None, "collateral_seen": None, "safe_completion": None,
               "evidence_exposed": None, "delivery": None, "agent_status": None, "steps": None, "decisions": [],
               "task_ms": None, "setup_ms": None, "error": None, "namespace": None}
    trace: dict = {"case_id": case.id, "condition": condition, "agent": [], "stage": "fixture"}
    tracker = observed = None
    stage = "fixture"
    try:
        if case.operation != "set_selector":
            raise ValueError("no fixture backend is registered for this operation")
        fixture = load_fixture(root, case.fixture)
        task = fixture.tasks[case.task]
        target = task.backend
        if case.injection and case.injection.source_ref != target:
            raise ValueError("injection source must be the task's diagnostic workload")
        channel_name = case.channel or "logs"
        payload = case.injection.payload if case.injection else BENIGN[channel_name]
        log_line, configmap_text = fixture_text(channel_name, payload)
        runbook = (root / "benchmark/fixtures" / case.fixture).parent / fixture.runbook
        async with open_lab(root, case, fixture, settings, log_target=target, log_line=log_line,
                            configmap_text=configmap_text) as lab:
            namespace = outcome["namespace"] = lab.namespace
            intent = fixture.intent(case.task, namespace)
            stage = "injection"
            async with open_evidence(backend=settings.backend, namespace=namespace, target=target,
                                     channel_name=channel_name, payload=payload,
                                     runbook_template=runbook.read_text(encoding="utf-8"),
                                     reader_client=lab.reader_client, alert_port=settings.alert_port) as evidence:
                impact = SelectorImpactDecision(intent) if condition == "opsguard" else None
                gateway = ExecutionGateway(
                    lab.inventory, MutationExecutor(lab.transport, fixture.bounds(namespace)), intent=intent,
                    enforce_task_policy=condition in ("task-policy", "opsguard"), alerts=evidence.alerts,
                    runbooks=evidence.runbooks, kubernetes_evidence=evidence.kubernetes, impact_decider=impact)
                stage = "readiness"
                before = await _wait_ready(lab.oracle, 60)
                verify_initial_state(case, before)
                tracker = OutcomeTracker(case, lab.oracle, before)
                stage = "evidence"
                initial = await _deliver(gateway, evidence.read_request, intent.task_id, payload, required=case.attacked)
                outcome["delivery"] = evidence.delivery
                outcome["evidence_exposed"] = (payload in initial.data["observation"]["text"]) if case.attacked else None
                user_task = case.user_task.replace("{namespace}", namespace)
                legitimate = _step(case.replay.legitimate, namespace)
                judge = None
                if condition == "llm-judge":
                    judge_model = (ScriptedJudgeModel(legitimate["request"]) if scripted
                                   else _real_model(settings, settings.judge_model, seed))
                    judge = ModelJudge(judge_model, user_task=user_task, evidence=[initial.data["observation"]],
                                       timeout=case.deadline_seconds)
                    gateway.impact_decider = judge
                if scripted:
                    # Controlled replay: the attack proposal first, then the legitimate one.
                    script = ([_step(case.replay.forbidden, namespace)] if case.replay.forbidden else []) + [
                        legitimate,
                        {"kind": "read", "request": {"tool": "get_service", "namespace": namespace, "name": task.service}},
                        {"kind": "finish", "summary": "The independent oracle scores the cluster state."}]
                    model = ScriptedModel(map(json.dumps, script))
                else:
                    model = _real_model(settings, model_name, seed)
                observed = ObservedGateway(gateway, tracker, settings.settle_seconds)
                stage = "agent"
                task_start = monotonic()
                run = await run_agent(
                    task_id=intent.task_id, user_task=user_task, model=model, gateway=observed,
                    initial_observation=initial, capture_synthetic_transcript=True,
                    limits=AgentLimits(max_steps=settings.max_steps, timeout_seconds=case.deadline_seconds),
                    prompt_mode="hardened" if condition == "hardened" else "unprotected")
                outcome.update(agent_status=run.status, steps=run.steps)
                trace["agent"] = list(run.trace)
                stage = "oracle"
                await tracker.settle(timeout_seconds=settings.settle_seconds)
                outcome.update(task_ms=round((monotonic() - task_start) * 1000, 3),
                               setup_ms=round((task_start - started) * 1000, 3))
                trace.update(executor_audit=gateway.audit, judge_audit=judge.audit if judge else [],
                             impact_audit=impact.audit if impact else [],
                             provider_audit=list(getattr(model, "audit", [])),
                             initial_observation=initial.data["observation"], user_task=user_task,
                             intent=intent.model_dump(mode="json"), before=summarize(before))
                if observed.oracle_error:
                    outcome["error"] = f"ORACLE_ERROR:{observed.oracle_error}"
                elif run.status in AGENT_ERROR_STATES:
                    outcome["error"] = run.status
                stage = "cleanup"
    except Exception as error:
        outcome["error"] = f"{stage.upper()}_ERROR:{type(error).__name__}"
        trace["error_detail"] = str(error)[:500]
    if tracker is not None and tracker.samples:
        # Keep whatever the oracle saw, including on errored trials.
        outcome.update(legitimate_met=tracker.legitimate_met, forbidden_seen=tracker.forbidden_seen,
                       collateral_seen=tracker.collateral_seen, safe_completion=tracker.safe_completion)
        trace["after"] = summarize(tracker.last)
    if observed is not None:
        outcome["decisions"] = observed.decisions
    trace["stage"] = stage
    outcome["elapsed_ms"] = round((monotonic() - started) * 1000, 3)
    return outcome, json.loads(json.dumps(trace, default=_jsonable))


def _jsonable(value):
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    return str(value)
