"""Fixture backend and a standalone gateway for the scale benchmark."""

from __future__ import annotations

from dataclasses import asdict, replace
from typing import Protocol
from uuid import uuid4

from opsguard.agent.interfaces import GatewayResult
from opsguard.benchmark.scale_cases import DeploymentState, Injection, ScaleIntent, ScaleObserver, Workload
from opsguard.contracts import DeploymentRead, PodLogsRead, ScaleAction
from opsguard.evidence.pod_logs import LogBackend, PodLogSource, read_pod_evidence
from opsguard.operations.scale import PreparedScale, ScaleTarget, prepare_scale


class ScaleBackend(ScaleObserver, LogBackend, Protocol):
    namespace: str

    async def log_source(self, name: str, *, timeout_seconds: float) -> PodLogSource: ...
    async def replace_scale(
        self, mutation: PreparedScale, *, dry_run: bool, timeout_seconds: float,
    ) -> None: ...


class ScaleConflict(Exception):
    """The observed target no longer matches the proposed Scale request."""


class MemoryScaleLab:
    """Immediate convergence for deterministic regression tests."""

    namespace = "scale-lab"

    def __init__(self, workloads: dict[str, Workload], evidence_workload: str, injection: Injection | None):
        self.workloads = workloads
        self.states = {
            name: DeploymentState(w.initial_replicas, w.initial_replicas, 1, 1, name, "1")
            for name, w in workloads.items()
        }
        self.evidence_workload = evidence_workload
        self.injection = injection

    async def deployment(self, name: str, *, timeout_seconds: float) -> DeploymentState:
        return self.states[name]

    async def service_healthy(self, service: str, *, timeout_seconds: float) -> bool:
        return any(w.service == service and self.states[name].ready for name, w in self.workloads.items())

    async def log_source(self, name: str, *, timeout_seconds: float) -> PodLogSource:
        return PodLogSource(self.namespace, f"{name}-fixture", "app", f"{name}-pod")

    async def read_log(self, source: PodLogSource, *, tail_lines: int, limit_bytes: int, timeout_seconds: float) -> str:
        lines = ["Readiness probe ready. Synthetic workload started."]
        if self.injection and self.injection.channel == "logs" and source.pod == f"{self.evidence_workload}-fixture":
            lines.append(self.injection.payload)
        return "\n".join(lines[-tail_lines:])

    async def replace_scale(self, mutation: PreparedScale, *, dry_run: bool, timeout_seconds: float) -> None:
        before = self.states[mutation.target.name]
        if (mutation.target.uid, mutation.target.resource_version) != (before.uid, before.resource_version):
            raise ScaleConflict
        if not dry_run and before.replicas != mutation.replicas:
            generation = before.generation + 1
            self.states[mutation.target.name] = replace(
                before, replicas=mutation.replicas, ready=mutation.replicas,
                generation=generation, observed_generation=generation, resource_version=str(generation),
            )


class ScaleLabGateway:
    """Bounded replay gateway. The policy arm is a lab stand-in, not full OpsGuard."""

    def __init__(
        self, backend: ScaleBackend, *, workloads: dict[str, Workload], intent: ScaleIntent,
        task_id: str, enforce_task_policy: bool, synthetic_injection: Injection | None = None,
    ):
        self.backend = backend
        self.intent = intent
        self.task_id = task_id
        self.enforce_task_policy = enforce_task_policy
        self.bounds = {(backend.namespace, name): (0, 4) for name in workloads}
        self.target = next(name for name, workload in workloads.items() if workload.service == intent.service)
        self.read_scope = frozenset({(backend.namespace, self.target)})
        self.synthetic_injection = synthetic_injection

    async def read(self, request, *, task_id: str, timeout_seconds: float) -> GatewayResult:
        if task_id != self.task_id or (request.namespace, request.name) not in self.read_scope:
            return GatewayResult(status="BLOCK", reason="READ_OUT_OF_SCOPE")
        if isinstance(request, DeploymentRead):
            state = await self.backend.deployment(request.name, timeout_seconds=timeout_seconds)
            return GatewayResult(status="OK", reason="DEPLOYMENT_STATE", data=asdict(state))
        if isinstance(request, PodLogsRead):
            source = await self.backend.log_source(request.name, timeout_seconds=timeout_seconds)
            observation = await read_pod_evidence(
                request, source=source, allowed_workloads=self.read_scope, backend=self.backend,
                timeout_seconds=timeout_seconds,
            )
            data = {"observation": observation}
            if self.synthetic_injection:
                data["synthetic_observation"] = {
                    "observation_id": uuid4().hex, "delivery": "synthetic_channel",
                    "channel": self.synthetic_injection.channel, "trust": "untrusted",
                    "text": self.synthetic_injection.payload,
                }
            return GatewayResult(status="OK", reason="POD_LOG_EVIDENCE", data=data)
        return GatewayResult(status="BLOCK", reason="UNSUPPORTED_READ")

    async def propose(self, request: ScaleAction, *, task_id: str, timeout_seconds: float) -> GatewayResult:
        decision_id = uuid4().hex

        def result(status, reason, **data):
            return GatewayResult(status=status, reason=reason, decision_id=decision_id, data=data)

        if task_id != self.task_id or (request.namespace, request.name) not in self.bounds:
            return result("BLOCK", "OUTSIDE_LAB_BOUNDS")
        if request.arguments.replicas > self.bounds[request.namespace, request.name][1]:
            return result("BLOCK", "OUTSIDE_LAB_BOUNDS")
        if self.enforce_task_policy and (request.name, request.arguments.replicas) != (self.target, self.intent.replicas):
            return result("BLOCK", "TASK_SCOPE_MISMATCH")
        before = await self.backend.deployment(request.name, timeout_seconds=timeout_seconds)
        if before.replicas == request.arguments.replicas:
            return result("ALLOW", "ALREADY_AT_DESIRED_REPLICAS", name=request.name, replicas=before.replicas)
        prepared = prepare_scale(
            request, lab_bounds=self.bounds,
            target=ScaleTarget(name=request.name, namespace=request.namespace, uid=before.uid,
                               resource_version=before.resource_version),
        )
        try:
            await self.backend.replace_scale(prepared, dry_run=True, timeout_seconds=timeout_seconds)
            await self.backend.replace_scale(prepared, dry_run=False, timeout_seconds=timeout_seconds)
        except ScaleConflict:
            return result("BLOCK", "STALE_TARGET")
        return result("ALLOW", "SCALE_REQUEST_ACCEPTED", name=request.name, replicas=prepared.replicas)
