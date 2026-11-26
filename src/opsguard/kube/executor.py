"""One-use preview/execute protocol for registered, immutable operations."""

import asyncio
from dataclasses import dataclass
import hashlib
import hmac
import json
import math
from time import monotonic
from typing import Literal, Protocol
from uuid import uuid4

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


class SignedDecision(WireModel):
    """An ALLOW bound to one prepared request and one intent.

    The executor recomputes the request digest and the MAC, and refuses a
    decision it has already used, so a forged or replayed decision never writes.
    """

    status: Literal["ALLOW"] = "ALLOW"
    decision_id: str = Field(min_length=16, max_length=64)
    intent_id: str = Field(min_length=1, max_length=253)
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    mac: str = Field(pattern=r"^[0-9a-f]{64}$")


def request_digest(mutation: PreparedMutation) -> str:
    """SHA-256 of exactly what would be sent: method, path, content type, body and bound identity."""
    material = {"method": mutation.method, "path": mutation.path, "content_type": mutation.content_type,
                "body": mutation.body_json, "target": mutation.target.model_dump()}
    return hashlib.sha256(json.dumps(material, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _mac(key: bytes, decision_id: str, intent_id: str, digest: str) -> str:
    message = json.dumps(["ALLOW", decision_id, intent_id, digest], separators=(",", ":")).encode()
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def sign_decision(key: bytes, mutation: PreparedMutation, intent_id: str) -> SignedDecision:
    digest = request_digest(mutation)
    decision_id = uuid4().hex
    return SignedDecision(decision_id=decision_id, intent_id=intent_id, request_sha256=digest,
                          mac=_mac(key, decision_id, intent_id, digest))


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
                 preparation_ttl: float = 120, decision_key: bytes | None = None, intent_id: str | None = None):
        # With a decision key, a write needs a SignedDecision for exactly this request and intent.
        if decision_key is not None and len(decision_key) < 32:
            raise ValueError("the decision key must be at least 32 bytes")
        self.decision_key, self.intent_id = decision_key, intent_id
        self._used_decisions: set[str] = set()
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

    def _verify(self, mutation: PreparedMutation, decision) -> str | None:
        """None when the decision authorizes this exact write, else the refusal reason."""
        if self.decision_key is None:
            return None if decision == "ALLOW" else "WRITE_WITHHELD"
        if not isinstance(decision, SignedDecision):
            return "DECISION_UNSIGNED"
        expected = _mac(self.decision_key, decision.decision_id, decision.intent_id, decision.request_sha256)
        if not hmac.compare_digest(expected, decision.mac):
            return "DECISION_FORGED"
        if decision.decision_id in self._used_decisions:
            return "DECISION_REPLAYED"
        if not hmac.compare_digest(decision.request_sha256, request_digest(mutation)):
            return "DECISION_REQUEST_MISMATCH"
        if self.intent_id is not None and decision.intent_id != self.intent_id:
            return "DECISION_INTENT_MISMATCH"
        return None

    async def execute(self, mutation: PreparedMutation, *, decision, timeout_seconds: float) -> ExecutionResult:
        if decision in ("BLOCK", "ESCALATE"):
            self.discard(mutation)
            return ExecutionResult(status=decision, reason="WRITE_WITHHELD")
        refusal = self._verify(mutation, decision)
        if refusal is not None:
            self.discard(mutation)
            return ExecutionResult(status="BLOCK", reason=refusal)
        if isinstance(decision, SignedDecision):
            self._used_decisions.add(decision.decision_id)  # Spent even if the write later fails.
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
