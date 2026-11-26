"""Shared gateway for scale, image, restart and selector, with an optional impact decision port."""

import asyncio
from time import monotonic
from typing import Literal
from uuid import uuid4

import httpx
from pydantic import TypeAdapter

from opsguard.agent.interfaces import GatewayResult
from opsguard.contracts import (AlertsRead, ConfigMapRead, DeploymentRead, EventsRead, PodLogsRead, RunbookRead,
                                ServiceRead, SupportedAction, WireModel)
from opsguard.impact.assessment import EpisodeState
from opsguard.intent.evidence import EvidenceRegistry
from opsguard.intent.models import IntentSpecV2, verify_intent
from opsguard.intent.policy import evaluate_policy
from opsguard.intent.resolver import check_binding, resolve_diagnostic_target
from opsguard.kube.canonical import Rejection, canonicalize
from opsguard.kube.executor import MutationExecutor


_RETRY = object()  # Internal: re-run the whole decision once with fresh state.


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
                 alerts=None, runbooks=None, kubernetes_evidence=None, impact_decider=None, on_event=None,
                 intent_key: bytes | None = None, clock=None, events=None):
        # A signed IntentSpecV2 is verified on every proposal; a v1 TaskIntent is trusted as configured.
        self.signed_intent = intent if isinstance(intent, IntentSpecV2) else None
        if self.signed_intent is not None:
            if intent_key is None:
                raise ValueError("a signed intent needs the lab intent key")
            try:
                intent = self.signed_intent.to_task_intent()
            except (AttributeError, TypeError, ValueError) as error:
                raise ValueError("the signed intent is malformed") from error
        self.intent_key, self.clock = intent_key, clock
        if intent.namespace != executor.bounds.namespace:
            raise ValueError("intent and executor namespaces differ")
        self.inventory_reader, self.executor, self.intent = inventory, executor, intent
        self.enforce_task_policy = enforce_task_policy
        self.alerts, self.runbooks, self.impact_decider = alerts, runbooks, impact_decider
        self.kubernetes_evidence = kubernetes_evidence
        self.events = events
        self.evidence = EvidenceRegistry(intent.task_id)
        # Start snapshot plus every applied change, so a decider can judge the sequence.
        self.episode = EpisodeState(intent.task_id)
        if getattr(impact_decider, "uses_episode", False):
            impact_decider.episode = self.episode
        self.audit: list[dict] = []
        self._lock = asyncio.Lock()
        self._restarted: set[tuple[str, str]] = set()
        self.on_event = on_event or (lambda event: None)

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
            elif isinstance(request, EventsRead) and self.events:
                record = await self.events.read_events(request, timeout_seconds=timeout_seconds-(monotonic()-started))
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

    async def propose_raw(self, raw, *, task_id, timeout_seconds, observation_ids=()):
        """A raw form (kubectl line, patch, apply body) is decided only as its one typed action."""
        typed = canonicalize(raw)
        if isinstance(typed, Rejection):
            self.on_event({"event": "canonical_rejection", "reason": typed.reason, "detail": typed.detail})
            return GatewayResult(status="BLOCK", reason=typed.reason, data={"detail": typed.detail})
        self.on_event({"event": "canonicalized", "action": typed.model_dump(mode="json")})
        return await self.propose(typed, task_id=task_id, timeout_seconds=timeout_seconds,
                                  observation_ids=observation_ids)

    async def propose(self, request, *, task_id, timeout_seconds, observation_ids=()):
        try:
            request = TypeAdapter(SupportedAction).validate_python(request.model_dump() if hasattr(request, "model_dump") else request)
        except ValueError:
            return GatewayResult(status="BLOCK", reason="UNSUPPORTED_OR_INVALID_ACTION")
        async def locked():
            async with self._lock:
                deadline = monotonic() + timeout_seconds
                # A changed resourceVersion is re-resolved and re-authorized once, then escalated.
                for attempt in range(2):
                    outcome = await self._propose(request, task_id=task_id,
                                                  timeout_seconds=max(0, deadline - monotonic()),
                                                  observation_ids=observation_ids, final_attempt=attempt == 1)
                    if outcome is not _RETRY:
                        return outcome
                raise AssertionError("unreachable")
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

    def _intent_check(self):
        if self.signed_intent is None:
            return None
        now = self.clock() if self.clock else None
        return verify_intent(self.signed_intent, self.intent_key, now=now)

    def _binding_outcome(self, result, prepared_target, latest_target, *, final_attempt):
        """BLOCK a replaced target; retry a changed version once, then ESCALATE."""
        state = check_binding(prepared_target, latest_target)
        if state == "TARGET_VERSION_CHANGED":
            return result("ESCALATE", "TARGET_VERSION_CHANGED", requires_reevaluation=True) if final_attempt else _RETRY
        return result("BLOCK", state if state != "BOUND" else "STALE_TARGET", requires_reevaluation=True)

    async def _propose(self, request, *, task_id, timeout_seconds, observation_ids, final_attempt=True):
        started = monotonic()
        decision_id = uuid4().hex
        deadline = started + timeout_seconds
        decision_ms = None
        def remaining():
            return max(0, deadline - monotonic())
        def result(status, reason, **data):
            data["decision_ms"] = decision_ms if decision_ms is not None else round((monotonic() - started)*1000, 3)
            data["gateway_ms"] = round((monotonic() - started)*1000, 3)
            self.on_event({"event":"decision_result", "decision_id":decision_id, "status":status, "reason":reason, **data})
            return GatewayResult(status=status, reason=reason, decision_id=decision_id, data=data)
        verification = self._intent_check()
        if verification is not None:
            self.on_event({"event": "intent_verification", "decision_id": decision_id,
                           "intent_id": self.signed_intent.id, **verification.model_dump()})
            if verification.status != "VALID":
                return result("BLOCK", verification.status, intent_reason=verification.reason)
        if self.executor.write_outcome_unknown:
            return result("ERROR", "RECONCILIATION_REQUIRED", write_outcome_unknown=True)
        if task_id != self.intent.task_id or request.namespace != self.intent.namespace:
            return result("BLOCK", "OUTSIDE_LAB_BOUNDS")
        if not self.evidence.contains(task_id, observation_ids):
            return result("BLOCK", "UNKNOWN_OBSERVATION")
        before = await self.inventory_reader.inventory(timeout_seconds=remaining())
        self.episode.begin(before)
        self.on_event({"event":"inventory", "decision_id":decision_id, "snapshot":__import__('dataclasses').asdict(before)})
        policy = None
        if self.enforce_task_policy:
            policy = evaluate_policy(request, intent=self.intent, task_id=task_id, inventory=before,
                                     evidence=self.evidence, observation_ids=observation_ids,
                                     on_check=lambda check: self.on_event({"event":"policy_check", "decision_id":decision_id, **check}))
            self.on_event({"event":"policy", "decision_id":decision_id, **policy.model_dump(mode="json")})
            if policy.status != "PASS":
                return result(policy.status, policy.reason)
        pool = {"service":before.services, "configmap":before.configmaps, "deployment":before.deployments}[request.resource]
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
            return self._binding_outcome(result, prepared.target, policy.resolution.target, final_attempt=final_attempt)
        if request.verb == "restart" and (prepared.target.name, prepared.target.uid) in self._restarted:
            self.executor.discard(prepared)
            return result("ALLOW", "ALREADY_RESTARTED_FOR_TASK")
        audit = {"decision_id": decision_id, "prepared": prepared.model_dump(), "before": targets[0]}
        self.audit.append(audit)
        self.on_event({"event":"prepared", **audit})
        try:
            preview = await self.executor.preview(prepared, timeout_seconds=remaining())
            audit["preview"] = preview.model_dump()
            self.on_event({"event":"preview", "decision_id":decision_id, **preview.model_dump()})
            if preview.status != "PREVIEWED":
                return result(preview.status, preview.reason, preview_ms=preview.elapsed_ms,
                              write_outcome_unknown=False, requires_reevaluation=preview.requires_reevaluation)
            # Re-read and re-authorize the routing graph after preview. The write
            # still carries UID/version preconditions for the remaining interval.
            current = await self.inventory_reader.inventory(timeout_seconds=remaining())
            if self.enforce_task_policy:
                latest = evaluate_policy(request, intent=self.intent, task_id=task_id, inventory=current,
                                         evidence=self.evidence, observation_ids=observation_ids,
                                         on_check=lambda check: self.on_event({"event":"recheck", "decision_id":decision_id, **check}))
                self.on_event({"event":"policy_recheck", "decision_id":decision_id, **latest.model_dump(mode="json")})
                if latest.status != "PASS":
                    return result(latest.status, latest.reason, requires_reevaluation=True)
                if latest.resolution.target != prepared.target:
                    return self._binding_outcome(result, prepared.target, latest.resolution.target,
                                                 final_attempt=final_attempt)
            decision = GuardDecision(status="ALLOW", reason="TASK_POLICY_PASSED" if self.enforce_task_policy else "LAB_BASELINE")
            if self.impact_decider:
                decision = await asyncio.wait_for(self.impact_decider(request, before, current, preview.object), remaining())
                decision = GuardDecision.model_validate(decision)
            audit["decision"] = decision.model_dump()
            self.on_event({"event":"impact_decision", "decision_id":decision_id, **decision.model_dump()})
            decision_ms = round((monotonic()-started)*1000,3)
            outcome = await self.executor.execute(prepared, decision=decision.status, timeout_seconds=remaining())
            audit["execution"] = outcome.model_dump()
            self.on_event({"event":"execution", "decision_id":decision_id, **outcome.model_dump()})
            if outcome.status == "APPLIED":
                self.episode.record(request, preview.object)
                if request.verb == "restart":
                    self._restarted.add((prepared.target.name, prepared.target.uid))
            return result("ALLOW" if outcome.status == "APPLIED" else outcome.status,
                          outcome.reason if decision.status == "ALLOW" else decision.reason,
                          preview_ms=preview.elapsed_ms if preview.request_sent else None,
                          execution_ms=outcome.elapsed_ms if outcome.request_sent else None,
                          write_outcome_unknown=outcome.write_outcome_unknown,
                          requires_reevaluation=outcome.requires_reevaluation)
        finally:
            self.executor.discard(prepared)
