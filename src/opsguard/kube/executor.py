"""One-use preview/execute protocol for registered, immutable operations."""

import asyncio
from dataclasses import dataclass
import math
from time import monotonic
from typing import Literal, Protocol

import httpx
from pydantic import Field

from opsguard.contracts import WireModel
from opsguard.kube.operations import LabBounds, OperationRegistry, PreparedMutation, default_registry


class MutationTransport(Protocol):
    async def send(self, mutation: PreparedMutation, *, dry_run: bool, timeout_seconds: float) -> dict: ...


class KubernetesTransport:
    def __init__(self, namespace: str, client: httpx.AsyncClient):
        self.namespace, self.client = namespace, client

    async def send(self, mutation, *, dry_run, timeout_seconds):
        if mutation.target.namespace != self.namespace:
            raise ValueError("mutation outside executor namespace")
        response = await self.client.request(mutation.method, mutation.path,
            params={"dryRun": "All"} if dry_run else None,
            headers={"Content-Type": mutation.content_type}, content=mutation.body_json.encode("utf-8"),
            timeout=timeout_seconds, follow_redirects=False)
        response.raise_for_status()
        obj = response.json()
        if not isinstance(obj, dict):
            raise ValueError("invalid Kubernetes response")
        return obj


class ExecutionResult(WireModel):
    status: Literal["PREVIEWED", "APPLIED", "BLOCK", "ESCALATE", "ERROR"]
    reason: str
    elapsed_ms: float = 0
    write_outcome_unknown: bool = False
    requires_reevaluation: bool = False
    request_sent: bool = False
    object: dict = Field(default_factory=dict)


@dataclass
class _Ticket:
    mutation: PreparedMutation
    expires: float
    state: str = "prepared"


class MutationExecutor:
    """The gateway owns authorization. No caller can bypass preview or replay a ticket.

    Unknown writes latch this executor closed. Reconciliation is an explicit caller
    responsibility; create a fresh task/executor only after observing cluster state.
    """

    def __init__(self, transport: MutationTransport, bounds: LabBounds, *, registry: OperationRegistry | None = None,
                 preparation_ttl: float = 120):
        if not math.isfinite(preparation_ttl) or preparation_ttl <= 0:
            raise ValueError("preparation lifetime must be positive and finite")
        self.transport, self.bounds = transport, bounds
        self.registry = registry or default_registry()
        self.preparation_ttl = preparation_ttl
        self._tickets: dict[str, _Ticket] = {}
        self.write_outcome_unknown = False

    def prepare(self, request, *, deployment: dict) -> PreparedMutation:
        if self.write_outcome_unknown:
            raise ValueError("reconcile the unknown write before preparing another action")
        self._tickets = {key: ticket for key, ticket in self._tickets.items() if ticket.expires > monotonic()}
        if len(self._tickets) >= 100:
            raise ValueError("too many outstanding preparations")
        mutation = self.registry.prepare(request, deployment, self.bounds)
        self._tickets[mutation.preparation_id] = _Ticket(mutation, monotonic() + self.preparation_ttl)
        return mutation

    def discard(self, mutation: PreparedMutation) -> None:
        self._tickets.pop(mutation.preparation_id, None)

    def _claim(self, mutation, expected, next_state):
        ticket = self._tickets.get(mutation.preparation_id)
        if self.write_outcome_unknown or ticket is None or ticket.mutation != mutation or ticket.state != expected:
            return False
        if monotonic() >= ticket.expires:
            self.discard(mutation)
            return False
        ticket.state = next_state  # claim before awaiting; concurrent calls cannot double-write
        return True

    async def preview(self, mutation: PreparedMutation, *, timeout_seconds: float) -> ExecutionResult:
        if not self._claim(mutation, "prepared", "previewing"):
            return ExecutionResult(status="BLOCK", reason="INVALID_PREPARATION")
        try:
            result = await self._send(mutation, dry_run=True, timeout_seconds=timeout_seconds)
        except BaseException:
            self.discard(mutation)
            raise
        if result.status == "PREVIEWED":
            ticket = self._tickets.get(mutation.preparation_id)
            if ticket:
                ticket.state = "previewed"
        else:
            self.discard(mutation)
        return result

    async def execute(self, mutation: PreparedMutation, *, decision: str, timeout_seconds: float) -> ExecutionResult:
        if decision != "ALLOW":
            self.discard(mutation)
            return ExecutionResult(status="ESCALATE" if decision == "ESCALATE" else "BLOCK", reason="WRITE_WITHHELD")
        if not self._claim(mutation, "previewed", "executing"):
            return ExecutionResult(status="BLOCK", reason="PREVIEW_REQUIRED_OR_EXPIRED")
        self.discard(mutation)
        try:
            result = await self._send(mutation, dry_run=False, timeout_seconds=timeout_seconds)
        except asyncio.CancelledError:
            self.write_outcome_unknown = True
            raise
        self.write_outcome_unknown |= result.write_outcome_unknown
        return result

    async def _send(self, mutation, *, dry_run, timeout_seconds):
        started = monotonic()
        sent = False
        def result(status, reason, **kwargs):
            return ExecutionResult(status=status, reason=reason, elapsed_ms=round((monotonic()-started)*1000, 3), request_sent=sent, **kwargs)
        if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
            return result("ERROR", "DEADLINE_BEFORE_REQUEST")
        try:
            sent = True
            obj = await asyncio.wait_for(self.transport.send(mutation, dry_run=dry_run,
                                                            timeout_seconds=timeout_seconds), timeout_seconds)
            return result("PREVIEWED" if dry_run else "APPLIED", "DRY_RUN_ACCEPTED" if dry_run else "WRITE_ACCEPTED", object=obj)
        except httpx.HTTPStatusError as error:
            code = error.response.status_code
            if code in (401, 403):
                return result("ERROR", "API_AUTHORIZATION_ERROR")
            if code in (404, 409):
                return result("BLOCK", "TARGET_NOT_FOUND" if code == 404 else "STALE_TARGET", requires_reevaluation=True)
            if code in (400, 422):
                return result("BLOCK", "API_VALIDATION_ERROR", requires_reevaluation=code == 422)
            unknown = not dry_run and (code >= 500 or code == 408)
            return result("ERROR", "WRITE_OUTCOME_UNKNOWN" if unknown else "API_ERROR", write_outcome_unknown=unknown)
        except (TimeoutError, httpx.TimeoutException):
            return result("ERROR", "PREVIEW_TIMEOUT" if dry_run else "WRITE_OUTCOME_UNKNOWN", write_outcome_unknown=not dry_run)
        except Exception:
            return result("ERROR", "PREVIEW_FAILED" if dry_run else "WRITE_OUTCOME_UNKNOWN", write_outcome_unknown=not dry_run)
