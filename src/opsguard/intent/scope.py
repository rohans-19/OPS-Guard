"""The operator's scope card: draft, confirm, then sign as an IntentSpec v2.

The drafter is rule-based. It sees only the operator's request text and the
names of objects the operator may refer to, never evidence. Any field it fills
from the request text is listed in ``drafted_fields``, so the card can mark it
and the audit can tell drafted fields from fields the operator typed.

The operator edits and confirms the scope. The signed intent is issued only
when a run starts, because only then is the run's namespace known.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import re
from typing import Literal
from uuid import uuid4

from pydantic import Field, model_validator

from opsguard.contracts import ObjectName, Verb, WireModel
from opsguard.intent.models import (ArgumentBounds, ImpactEnvelope, IntentSpecV2, ProtectedRef, ReplicaBounds,
                                    TargetHint, sign_intent)

_VERB_TEXT = {"scale": "Scale", "restart": "Restart", "set_image": "Update the image of",
              "set_config": "Change", "set_selector": "Repair the selector of"}


class ObjectChoice(WireModel):
    kind: Literal["Service", "Deployment", "StatefulSet", "ConfigMap"]
    name: ObjectName


class ConfirmedScope(WireModel):
    """Everything the operator approves except the run's namespace and timestamps."""

    operation: Verb
    target: TargetHint
    bounds: ArgumentBounds
    protected: tuple[ProtectedRef, ...] = ()
    envelope: ImpactEnvelope = ImpactEnvelope()
    risk_class: Literal["low", "medium", "high"] = "low"
    approval_mode: Literal["allow_inside_envelope", "always_escalate"] = "allow_inside_envelope"
    expires_minutes: int = Field(default=15, ge=1, le=240)
    summary: str = Field(default="", max_length=500)
    drafted_fields: tuple[str, ...] = ()

    @model_validator(mode="after")
    def valid_as_intent(self) -> ConfirmedScope:
        # Build a throwaway intent so every IntentSpecV2 rule applies at confirmation time.
        self.to_intent(namespace="scope-check", task_id="scope-check", intent_id="scope-check",
                       now=datetime(2026, 1, 1, tzinfo=timezone.utc))
        return self

    def to_intent(self, *, namespace: str, task_id: str, intent_id: str | None = None,
                  now: datetime | None = None) -> IntentSpecV2:
        now = now or datetime.now(timezone.utc)
        return IntentSpecV2(
            id=intent_id or "intent-" + uuid4().hex[:12], task_id=task_id, issued_at=now,
            expires_at=now + timedelta(minutes=self.expires_minutes), namespace=namespace, target=self.target,
            verbs=(self.operation,), bounds=self.bounds, protected=self.protected, envelope=self.envelope,
            risk_class=self.risk_class, approval_mode=self.approval_mode, summary=self.summary,
            drafted_fields=self.drafted_fields)


def issue_intent(scope: ConfirmedScope, *, namespace: str, task_id: str, key: bytes,
                 now: datetime | None = None) -> IntentSpecV2:
    """The gateway-issued, signed intent for one run."""
    return sign_intent(scope.to_intent(namespace=namespace, task_id=task_id, now=now), key)


def summarize(scope: ConfirmedScope) -> str:
    target = f"{scope.target.kind_hint or 'object'} {scope.target.name}"
    detail = ""
    if scope.bounds.replicas:
        low, high = scope.bounds.replicas.min, scope.bounds.replicas.max
        detail = f" to {low} replicas" if low == high else f" to {low}-{high} replicas"
    elif scope.bounds.image:
        detail = f" to {', '.join(scope.bounds.image.allowed)}"
    elif scope.bounds.config:
        detail = f" key {scope.bounds.config.key} to {', '.join(scope.bounds.config.values)}"
    elif scope.bounds.selector:
        detail = f" so it routes to {scope.bounds.selector.backend}"
    protected = ", ".join(ref.name for ref in scope.protected) or "nothing"
    return (f"{_VERB_TEXT[scope.operation]} {target}{detail}. Protected: {protected}. "
            f"At most {scope.envelope.max_services_affected} Service affected. "
            f"Expires {scope.expires_minutes} minutes after the run starts.")


def _mentioned(prompt: str, name: str) -> bool:
    return re.search(r"(?<![a-z0-9-])" + re.escape(name) + r"(?![a-z0-9-])", prompt.casefold()) is not None


def draft_scope(*, operation: str, prompt: str, objects: tuple[ObjectChoice, ...], default_target: ObjectChoice,
                bounds: ArgumentBounds, protected: tuple[ProtectedRef, ...] = ()) -> ConfirmedScope:
    """A first draft for the operator to edit. Uses the request text and object names only."""
    drafted = ["summary"]
    target = default_target
    protected_names = {ref.name for ref in protected}
    candidates = [obj for obj in objects if obj.kind == default_target.kind
                  and obj.name not in protected_names and _mentioned(prompt, obj.name)]
    if len(candidates) == 1 and candidates[0] != default_target:
        target = candidates[0]
        drafted.append("target")
    if operation == "scale" and bounds.replicas:
        match = re.search(r"\bto\s+(\d{1,4})\s+replicas?\b", prompt, re.IGNORECASE)
        if match and int(match.group(1)) != bounds.replicas.min:
            count = int(match.group(1))
            bounds = bounds.model_copy(update={"replicas": ReplicaBounds(min=count, max=count)})
            drafted.append("bounds.replicas")
    scope = ConfirmedScope(operation=operation, target=TargetHint(kind_hint=target.kind, name=target.name),
                           bounds=bounds, protected=protected, drafted_fields=tuple(drafted))
    return scope.model_copy(update={"summary": summarize(scope)})
