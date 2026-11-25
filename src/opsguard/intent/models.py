"""Trusted operator configuration, passed separately from model proposals."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import json
import os
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, model_validator

from opsguard.contracts import DnsLabel, ImageReference, ObjectName, SelectorMap, Verb, WireModel


class LogicalTarget(WireModel):
    # "any" is a bare name with no kind: the resolver considers every
    # Deployment, StatefulSet and Service with that name and escalates on more than one.
    resource: Literal["service", "deployment", "configmap", "statefulset", "any"]
    name: ObjectName
    uid: str | None = Field(default=None, min_length=1, max_length=256)


class ScalePermission(WireModel):
    verb: Literal["scale"] = "scale"
    min_replicas: int = Field(ge=0, le=2**31 - 1)
    max_replicas: int = Field(ge=0, le=2**31 - 1)

    @model_validator(mode="after")
    def ordered_bounds(self) -> ScalePermission:
        if self.min_replicas > self.max_replicas:
            raise ValueError("replica bounds are reversed")
        return self


class ImagePermission(WireModel):
    verb: Literal["set_image"] = "set_image"
    container: DnsLabel
    images: tuple[ImageReference, ...] = Field(min_length=1, max_length=20)


class RestartPermission(WireModel):
    verb: Literal["restart"] = "restart"


class SelectorPermission(WireModel):
    """The operator-confirmed replacement selector and the backend it must select.

    The gateway re-derives the Deployments matched by this selector from live
    inventory and requires exactly this backend before any write.
    """

    verb: Literal["set_selector"] = "set_selector"
    selector: SelectorMap
    backend: ObjectName


class ConfigPermission(WireModel):
    verb: Literal["set_config"] = "set_config"
    key: str = Field(min_length=1, max_length=253)
    values: tuple[str, ...] = Field(min_length=1, max_length=20)
    diagnostic_deployment: ObjectName


Permission = Annotated[ScalePermission | ImagePermission | RestartPermission | SelectorPermission | ConfigPermission,
                       Field(discriminator="verb")]


class TaskIntent(WireModel):
    """Build only from an operator-confirmed task, never evidence or oracle labels.

    The caller authenticates the operator and stores this configuration. A model
    receives a task ID, not an API for creating or widening this object.
    """

    task_id: DnsLabel
    namespace: DnsLabel
    target: LogicalTarget
    permissions: tuple[Permission, ...] = Field(min_length=1, max_length=5)
    protected_deployments: tuple[ObjectName, ...] = ()
    protected_services: tuple[ObjectName, ...] = ()
    allow_selector_fallback: bool = False

    @model_validator(mode="after")
    def unique_permissions(self) -> TaskIntent:
        has_config = any(isinstance(permission, ConfigPermission) for permission in self.permissions)
        if has_config != (self.target.resource == "configmap") or (has_config and len(self.permissions) != 1):
            raise ValueError("a ConfigMap task requires exactly one ConfigPermission and a ConfigMap target")
        if len({permission.verb for permission in self.permissions}) != len(self.permissions):
            raise ValueError("one permission per operation is required")
        if any(isinstance(permission, SelectorPermission) for permission in self.permissions) and (
                self.target.resource != "service" or self.target.name in self.protected_services):
            raise ValueError("a selector permission needs an unprotected Service target")
        return self


# ---------------------------------------------------------------------------
# IntentSpec v2: the signed, operator-confirmed task scope (Stage 2, S2-P2)
# ---------------------------------------------------------------------------

INTENT_KEY_ENV = "OPSGUARD_INTENT_KEY"
MAX_INTENT_LIFETIME = timedelta(hours=24)
CLOCK_SKEW = timedelta(seconds=30)
_MIN_KEY_BYTES = 32
_KIND_TO_RESOURCE = {"Service": "service", "Deployment": "deployment", "StatefulSet": "statefulset",
                     "ConfigMap": "configmap", None: "any"}


class TargetHint(WireModel):
    """The operator's name for the target. The resolver turns it into one live object."""

    kind_hint: Literal["Service", "Deployment", "StatefulSet", "ConfigMap"] | None = None
    name: ObjectName
    uid: str | None = Field(default=None, min_length=1, max_length=256)


class ReplicaBounds(WireModel):
    min: int = Field(ge=0, le=2**31 - 1)
    max: int = Field(ge=0, le=2**31 - 1)

    @model_validator(mode="after")
    def ordered(self) -> ReplicaBounds:
        if self.min > self.max:
            raise ValueError("replica bounds are reversed")
        return self


class ImageBounds(WireModel):
    container: DnsLabel
    allowed: tuple[ImageReference, ...] = Field(min_length=1, max_length=20)


class ConfigBounds(WireModel):
    key: str = Field(min_length=1, max_length=253)
    values: tuple[str, ...] = Field(min_length=1, max_length=20)
    diagnostic_deployment: ObjectName


class SelectorBounds(WireModel):
    selector: SelectorMap
    backend: ObjectName


class ArgumentBounds(WireModel):
    replicas: ReplicaBounds | None = None
    image: ImageBounds | None = None
    config: ConfigBounds | None = None
    selector: SelectorBounds | None = None


class ProtectedRef(WireModel):
    """A resource no action may touch. namespace None means the intent's namespace."""

    kind: Literal["Deployment", "StatefulSet", "Service", "ConfigMap", "any"] = "any"
    namespace: DnsLabel | None = None
    name: ObjectName | Literal["*"]


class Postcondition(WireModel):
    """Checked after the action by the post-action verifier (Stage 4)."""

    check: Literal["ready_replicas_at_least", "http_status"]
    object: ObjectName
    value: int = Field(ge=0, le=2**31 - 1)
    description: str = Field(default="", max_length=200)


class ImpactEnvelope(WireModel):
    """Blast-radius limits for the effect check (S2-P4). Unknown counts as a breach."""

    max_services_affected: int = Field(default=1, ge=0, le=100)
    max_namespaces: int = Field(default=1, ge=1, le=10)
    allow_deletion: bool = False
    allow_privilege_change: bool = False
    allow_secret_change: bool = False
    require_reversible: bool = True


_BOUNDS_BY_VERB = {"scale": "replicas", "set_image": "image", "set_config": "config", "set_selector": "selector"}


class IntentSpecV2(WireModel):
    """What the operator approved, signed by the gateway's lab key.

    Fields come from the operator's request and confirmed object names, never
    from evidence. ``drafted_fields`` lists fields a drafter model proposed and
    the operator then confirmed, so audits can tell them apart.
    """

    id: DnsLabel
    version: Literal["v2"] = "v2"
    task_id: DnsLabel
    issued_at: Annotated[AwareDatetime, Field(strict=False)]
    expires_at: Annotated[AwareDatetime, Field(strict=False)]
    namespace: DnsLabel
    target: TargetHint
    verbs: tuple[Verb, ...] = Field(min_length=1, max_length=5)
    bounds: ArgumentBounds = ArgumentBounds()
    protected: tuple[ProtectedRef, ...] = ()
    postconditions: tuple[Postcondition, ...] = ()
    envelope: ImpactEnvelope = ImpactEnvelope()
    risk_class: Literal["low", "medium", "high"] = "low"
    approval_mode: Literal["allow_inside_envelope", "always_escalate"] = "allow_inside_envelope"
    summary: str = Field(default="", max_length=500)
    drafted_fields: tuple[str, ...] = ()
    signature: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def consistent(self) -> IntentSpecV2:
        if self.expires_at <= self.issued_at:
            raise ValueError("intent expires before it is issued")
        if self.expires_at - self.issued_at > MAX_INTENT_LIFETIME:
            raise ValueError("intent lifetime exceeds the lab maximum")
        if len(set(self.verbs)) != len(self.verbs):
            raise ValueError("duplicate verb")
        for verb, field in _BOUNDS_BY_VERB.items():
            present = getattr(self.bounds, field) is not None
            if present != (verb in self.verbs):
                raise ValueError(f"bounds.{field} must be set exactly when {verb} is allowed")
        for ref in self.protected:
            if ref.namespace in (None, self.namespace) and ref.name == "*":
                raise ValueError("a wildcard cannot protect the intent's own namespace")
            if ref.namespace in (None, self.namespace) and ref.name == self.target.name and (
                    ref.kind in ("any", self.target.kind_hint)):
                raise ValueError("the intent target is protected")
        self.to_task_intent()  # The legacy view must validate too.
        return self

    def to_task_intent(self) -> TaskIntent:
        """The equivalent v1 TaskIntent, so existing policy and runners keep working."""
        permissions = []
        for verb in self.verbs:
            if verb == "scale":
                permissions.append(ScalePermission(min_replicas=self.bounds.replicas.min,
                                                   max_replicas=self.bounds.replicas.max))
            elif verb == "set_image":
                permissions.append(ImagePermission(container=self.bounds.image.container,
                                                   images=self.bounds.image.allowed))
            elif verb == "restart":
                permissions.append(RestartPermission())
            elif verb == "set_selector":
                permissions.append(SelectorPermission(selector=self.bounds.selector.selector,
                                                      backend=self.bounds.selector.backend))
            else:
                permissions.append(ConfigPermission(key=self.bounds.config.key, values=self.bounds.config.values,
                                                    diagnostic_deployment=self.bounds.config.diagnostic_deployment))
        local = [ref for ref in self.protected if ref.namespace in (None, self.namespace)]
        return TaskIntent(
            task_id=self.task_id, namespace=self.namespace,
            target=LogicalTarget(resource=_KIND_TO_RESOURCE[self.target.kind_hint], name=self.target.name,
                                 uid=self.target.uid),
            permissions=tuple(permissions),
            protected_deployments=tuple(sorted({ref.name for ref in local if ref.kind in ("any", "Deployment", "StatefulSet")})),
            protected_services=tuple(sorted({ref.name for ref in local if ref.kind in ("any", "Service")})),
            allow_selector_fallback=True,
        )

    def canonical_bytes(self) -> bytes:
        """Canonical JSON of every field except the signature."""
        body = self.model_dump(mode="json", exclude={"signature"})
        return json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class IntentVerification(WireModel):
    status: Literal["VALID", "INTENT_INVALID", "INTENT_EXPIRED"]
    reason: str


def _check_key(key: bytes) -> bytes:
    if not isinstance(key, bytes) or len(key) < _MIN_KEY_BYTES:
        raise ValueError(f"the intent key must be at least {_MIN_KEY_BYTES} bytes")
    return key


def sign_intent(intent: IntentSpecV2, key: bytes) -> IntentSpecV2:
    digest = hmac.new(_check_key(key), intent.canonical_bytes(), hashlib.sha256).hexdigest()
    return intent.model_copy(update={"signature": digest})


def verify_intent(intent: IntentSpecV2, key: bytes, *, now: datetime | None = None) -> IntentVerification:
    """Fail closed: any doubt about the signature or clock gives a non-VALID status."""
    try:
        if not isinstance(intent, IntentSpecV2):
            return IntentVerification(status="INTENT_INVALID", reason="NOT_AN_INTENT")
        intent = IntentSpecV2.model_validate(intent.model_dump())  # Re-run every validator.
        expected = hmac.new(_check_key(key), intent.canonical_bytes(), hashlib.sha256).hexdigest()
    except ValueError:
        return IntentVerification(status="INTENT_INVALID", reason="INTENT_MALFORMED")
    if intent.signature is None or not hmac.compare_digest(expected, intent.signature):
        return IntentVerification(status="INTENT_INVALID", reason="SIGNATURE_MISMATCH")
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        return IntentVerification(status="INTENT_INVALID", reason="CLOCK_NOT_TIMEZONE_AWARE")
    if intent.issued_at > now + CLOCK_SKEW:
        return IntentVerification(status="INTENT_INVALID", reason="ISSUED_IN_FUTURE")
    if now >= intent.expires_at:
        return IntentVerification(status="INTENT_EXPIRED", reason="INTENT_EXPIRED")
    return IntentVerification(status="VALID", reason="INTENT_VALID")


def load_intent_key(env_file: str | Path = ".env") -> bytes:
    """Read the lab signing key from the environment or an ignored .env file. Never log it."""
    value = os.environ.get(INTENT_KEY_ENV)
    if not value and Path(env_file).is_file():
        for line in Path(env_file).read_text(encoding="utf-8").splitlines():
            name, sep, raw = line.strip().partition("=")
            if sep and name.strip() == INTENT_KEY_ENV:
                value = raw.strip().strip("'\"")
    if not value:
        raise ValueError(f"{INTENT_KEY_ENV} is not set")
    return _check_key(value.encode("utf-8"))
