"""Shared gateway for scale, image, restart and selector, with an optional impact decision port."""

import asyncio
from time import monotonic
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import TypeAdapter

from opsguard.agent.interfaces import GatewayResult
from opsguard.contracts import (AlertsRead, ConfigMapRead, DeploymentRead, PodLogsRead, RunbookRead, ServiceRead,
                                SupportedAction, WireModel)
from opsguard.intent.evidence import EvidenceRegistry
from opsguard.intent.policy import evaluate_policy
from opsguard.intent.resolver import resolve_diagnostic_target
from opsguard.kube.executor import MutationExecutor


class GuardDecision(WireModel):
    status: Literal["ALLOW", "BLOCK", "ESCALATE"]
    reason: str
    predicted_services: tuple[str, ...] = ()


class ExecutionGateway:
    """Policy-only by default. Supply an impact_decider for full team integration.

    The trusted decider receives request, before inventory, current inventory and
    preview object. It returns GuardDecision; no decision field is model-controlled.
    """

    def __init__(self, inventory, executor: MutationExecutor, *, intent, enforce_task_policy=True,
                 alerts=None, runbooks=None, kubernetes_evidence=None, impact_decider=None):
        if intent.namespace != executor.bounds.namespace:
            raise ValueError("intent and executor namespaces differ")
        self.inventory_reader, self.executor, self.intent = inventory, executor, intent
        self.enforce_task_policy = enforce_task_policy
        self.alerts, self.runbooks, self.impact_decider = alerts, runbooks, impact_decider
        self.kubernetes_evidence = kubernetes_evidence
        self.evidence = EvidenceRegistry(intent.task_id)
        self.audit: list[dict] = []
        self._lock = asyncio.Lock()
        self._restarted: set[tuple[str, str]] = set()

    async def read(self, request, *, task_id, timeout_seconds):
        if task_id != self.intent.task_id or request.namespace != self.intent.namespace:
            return GatewayResult(status="BLOCK", reason="READ_OUT_OF_SCOPE")
        started = monotonic()
        snapshot = await self.inventory_reader.inventory(timeout_seconds=timeout_seconds)
        if isinstance(request, ServiceRead):
            return self._service_state(request, snapshot)
        resolved = resolve_diagnostic_target(self.intent, snapshot)
        if resolved.status != "PASS" or request.name != resolved.target.name:
            return GatewayResult(status="BLOCK", reason="READ_TARGET_UNVERIFIED")
        if isinstance(request, DeploymentRead):
            dep = next(obj for obj in snapshot.deployments if obj["metadata"]["name"] == request.name)
            return GatewayResult(status="OK", reason="DEPLOYMENT_STATE", data={
                "name": request.name, "uid": resolved.target.uid, "generation": dep["metadata"].get("generation"),
                "replicas": dep["spec"].get("replicas"), "status": dep.get("status", {}),
                "images": {c["name"]: c["image"] for c in dep["spec"]["template"]["spec"]["containers"]},
            })
        try:
            if isinstance(request, AlertsRead) and self.alerts:
                remaining = timeout_seconds - (monotonic() - started)
                if remaining <= 0:
                    raise TimeoutError
                record = await self.alerts.read(request, timeout_seconds=remaining)
            elif isinstance(request, RunbookRead) and self.runbooks:
                record = self.runbooks.read(request)
            elif isinstance(request, PodLogsRead) and self.kubernetes_evidence:
                record = await self.kubernetes_evidence.read_logs(request,timeout_seconds=timeout_seconds-(monotonic()-started))
            elif isinstance(request, ConfigMapRead) and self.kubernetes_evidence:
                record = await self.kubernetes_evidence.read_configmap(request,timeout_seconds=timeout_seconds-(monotonic()-started))
            else:
                return GatewayResult(status="BLOCK", reason="UNSUPPORTED_READ")
        except PermissionError:
            return GatewayResult(status="BLOCK", reason="EVIDENCE_OUT_OF_SCOPE")
        self.evidence.add(record)
        return GatewayResult(status="OK", reason="EVIDENCE_OBSERVED", data={"observation": record.model_dump()})

    def _service_state(self, request, snapshot):
        """Only the Service named by the trusted task is readable; its text is live inventory."""
        services = [obj for obj in snapshot.services if obj["metadata"]["name"] == request.name
                    and obj["metadata"].get("namespace", request.namespace) == request.namespace]
        if self.intent.target.resource != "service" or request.name != self.intent.target.name or len(services) != 1:
            return GatewayResult(status="BLOCK", reason="READ_TARGET_UNVERIFIED")
        ready = sum(1 for item in snapshot.endpointslices
                    if item["metadata"].get("labels", {}).get("kubernetes.io/service-name") == request.name
                    for endpoint in item.get("endpoints") or [] if endpoint.get("conditions", {}).get("ready") is True)
        selector = services[0].get("spec", {}).get("selector") or {}
        return GatewayResult(status="OK", reason="SERVICE_STATE", data={
            "name": request.name, "uid": services[0]["metadata"].get("uid"),
            "selector": {str(key): str(value) for key, value in selector.items()}, "ready_endpoints": ready})

    async def propose(self, request, *, task_id, timeout_seconds, observation_ids=()):
        try:
            request = TypeAdapter(SupportedAction).validate_python(request.model_dump() if hasattr(request, "model_dump") else request)
        except ValueError:
            return GatewayResult(status="BLOCK", reason="UNSUPPORTED_OR_INVALID_ACTION")
        async def locked():
            async with self._lock:
                return await self._propose(request, task_id=task_id, timeout_seconds=timeout_seconds,
                                           observation_ids=observation_ids)
        try:
            return await asyncio.wait_for(locked(), timeout_seconds)
        except (TimeoutError, httpx.TimeoutException):
            return GatewayResult(status="ERROR", reason="GATEWAY_TIMEOUT",
                                 data={"write_outcome_unknown": self.executor.write_outcome_unknown})
        except httpx.HTTPStatusError as error:
            return GatewayResult(status="ERROR", reason="API_AUTHORIZATION_ERROR" if error.response.status_code in (401, 403)
                                 else "INVENTORY_API_ERROR", data={"write_outcome_unknown": self.executor.write_outcome_unknown})
        except Exception:
            return GatewayResult(status="ERROR", reason="GATEWAY_VALIDATION_OR_READ_ERROR",
                                 data={"write_outcome_unknown": self.executor.write_outcome_unknown})

    async def _propose(self, request, *, task_id, timeout_seconds, observation_ids):
        started = monotonic()
        decision_id = uuid4().hex
        deadline = started + timeout_seconds
        decision_ms = None
        def remaining():
            return max(0, deadline - monotonic())
        def result(status, reason, **data):
            data["decision_ms"] = decision_ms if decision_ms is not None else round((monotonic() - started)*1000, 3)
            data["gateway_ms"] = round((monotonic() - started)*1000, 3)
            return GatewayResult(status=status, reason=reason, decision_id=decision_id, data=data)
        if self.executor.write_outcome_unknown:
            return result("ERROR", "RECONCILIATION_REQUIRED", write_outcome_unknown=True)
        if task_id != self.intent.task_id or request.namespace != self.intent.namespace:
            return result("BLOCK", "OUTSIDE_LAB_BOUNDS")
        if not self.evidence.contains(task_id, observation_ids):
            return result("BLOCK", "UNKNOWN_OBSERVATION")
        before = await self.inventory_reader.inventory(timeout_seconds=remaining())
        policy = None
        if self.enforce_task_policy:
            policy = evaluate_policy(request, intent=self.intent, task_id=task_id, inventory=before,
                                     evidence=self.evidence, observation_ids=observation_ids)
            if policy.status != "PASS":
                return result(policy.status, policy.reason)
        pool = before.services if request.resource == "service" else before.deployments
        targets = [obj for obj in pool if obj["metadata"]["name"] == request.name
                   and obj["metadata"].get("namespace", request.namespace) == request.namespace]
        if len(targets) != 1:
            return result("BLOCK", "TARGET_NOT_FOUND")
        try:
            prepared = self.executor.prepare(request, deployment=targets[0])
        except (ValueError, KeyError, TypeError):
            return result("BLOCK", "UNSUPPORTED_INVALID_OR_OUTSIDE_LAB")
        if policy and policy.resolution.target != prepared.target:
            self.executor.discard(prepared)
            return result("BLOCK", "STALE_TARGET", requires_reevaluation=True)
        if request.verb == "restart" and (prepared.target.name, prepared.target.uid) in self._restarted:
            self.executor.discard(prepared)
            return result("ALLOW", "ALREADY_RESTARTED_FOR_TASK")
        audit = {"decision_id": decision_id, "prepared": prepared.model_dump(), "before": targets[0]}
        self.audit.append(audit)
        try:
            preview = await self.executor.preview(prepared, timeout_seconds=remaining())
            audit["preview"] = preview.model_dump()
            if preview.status != "PREVIEWED":
                return result(preview.status, preview.reason, preview_ms=preview.elapsed_ms,
                              write_outcome_unknown=False, requires_reevaluation=preview.requires_reevaluation)
            # Re-read and re-authorize the routing graph after preview. The write
            # still carries UID/version preconditions for the remaining interval.
            current = await self.inventory_reader.inventory(timeout_seconds=remaining())
            if self.enforce_task_policy:
                latest = evaluate_policy(request, intent=self.intent, task_id=task_id, inventory=current,
                                         evidence=self.evidence, observation_ids=observation_ids)
                if latest.status != "PASS":
                    return result(latest.status, latest.reason, requires_reevaluation=True)
                if latest.resolution.target != prepared.target:
                    return result("BLOCK", "STALE_TARGET", requires_reevaluation=True)
            decision = GuardDecision(status="ALLOW", reason="TASK_POLICY_PASSED" if self.enforce_task_policy else "LAB_BASELINE")
            if self.impact_decider:
                decision = await asyncio.wait_for(self.impact_decider(request, before, current, preview.object), remaining())
                decision = GuardDecision.model_validate(decision)
            audit["decision"] = decision.model_dump()
            decision_ms = round((monotonic()-started)*1000,3)
            outcome = await self.executor.execute(prepared, decision=decision.status, timeout_seconds=remaining())
            audit["execution"] = outcome.model_dump()
            if outcome.status == "APPLIED" and request.verb == "restart":
                self._restarted.add((prepared.target.name, prepared.target.uid))
            return result("ALLOW" if outcome.status == "APPLIED" else outcome.status,
                          outcome.reason if decision.status == "ALLOW" else decision.reason,
                          preview_ms=preview.elapsed_ms if preview.request_sent else None,
                          execution_ms=outcome.elapsed_ms if outcome.request_sent else None,
                          write_outcome_unknown=outcome.write_outcome_unknown,
                          requires_reevaluation=outcome.requires_reevaluation)
        finally:
            self.executor.discard(prepared)
