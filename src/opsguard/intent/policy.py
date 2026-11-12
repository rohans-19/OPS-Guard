"""Deterministic authorization. A PASS still requires preview and effect checks."""

from typing import Literal

from pydantic import TypeAdapter

from opsguard.contracts import ActionRequest, ScaleAction, SetImageAction, SetSelectorAction, SupportedAction, WireModel
from opsguard.intent.evidence import EvidenceRegistry
from opsguard.intent.inventory import InventorySnapshot
from opsguard.intent.models import ImagePermission, ScalePermission, SelectorPermission, TaskIntent
from opsguard.intent.resolver import TargetResolution, resolve_service, resolve_target, selector_backends


class FieldBinding(WireModel):
    field: str
    authority: Literal["trusted_task", "live_inventory"]


class PolicyResult(WireModel):
    status: Literal["PASS", "BLOCK", "ESCALATE"]
    reason: str
    resolution: TargetResolution | None = None
    bindings: tuple[FieldBinding, ...] = ()


def evaluate_policy(
    request: ActionRequest | dict, *, intent: TaskIntent, task_id: str,
    inventory: InventorySnapshot, evidence: EvidenceRegistry | None = None,
    observation_ids: tuple[str, ...] = (),
) -> PolicyResult:
    def block(reason):
        return PolicyResult(status="BLOCK", reason=reason)

    if task_id != intent.task_id:
        return block("TASK_ID_MISMATCH")
    try:
        action = TypeAdapter(SupportedAction).validate_python(
            request.model_dump() if isinstance(request, ActionRequest) else request,
        )
    except ValueError:
        return block("UNSUPPORTED_OR_INVALID_ACTION")
    if action.namespace != intent.namespace:
        return block("NAMESPACE_MISMATCH")
    protected = intent.protected_services if action.resource == "service" else intent.protected_deployments
    if action.name in protected:
        return block("PROTECTED_TARGET")
    if observation_ids and (evidence is None or not evidence.contains(task_id, observation_ids)):
        return block("UNKNOWN_OBSERVATION")
    permission = next((rule for rule in intent.permissions if rule.verb == action.verb), None)
    if permission is None:
        return block("OPERATION_NOT_AUTHORIZED")
    if isinstance(action, ScaleAction):
        if not isinstance(permission, ScalePermission) or not (
            permission.min_replicas <= action.arguments.replicas <= permission.max_replicas
        ):
            return block("ARGUMENT_NOT_AUTHORIZED")
    elif isinstance(action, SetImageAction):
        if not isinstance(permission, ImagePermission) or action.arguments.container != permission.container:
            return block("CONTAINER_NOT_AUTHORIZED")
        if action.arguments.image not in permission.images:
            return block("IMAGE_NOT_AUTHORIZED")

    if isinstance(action, SetSelectorAction):
        return _selector_policy(action, intent, permission, inventory)

    resolution = resolve_target(intent, inventory)
    if resolution.status != "PASS":
        return PolicyResult(status=resolution.status, reason=resolution.reason, resolution=resolution)
    if action.name != resolution.target.name:
        return PolicyResult(status="BLOCK", reason="TASK_SCOPE_MISMATCH", resolution=resolution)
    # Model-provided observation references are diagnostics only. Evidence never
    # becomes an authority, even when its text repeats an authorized value.
    fields = ("task_id", "namespace", "verb", *[f"arguments.{name}" for name in type(action.arguments).model_fields])
    return PolicyResult(
        status="PASS", reason="TASK_AUTHORIZED", resolution=resolution,
        bindings=tuple(FieldBinding(field=field, authority="trusted_task") for field in fields)
        + (FieldBinding(field="name", authority="live_inventory"),),
    )


def _selector_policy(action: SetSelectorAction, intent: TaskIntent, permission, inventory) -> PolicyResult:
    """Authorize one whole-selector replacement on the task's own Service."""
    if not isinstance(permission, SelectorPermission):
        return PolicyResult(status="BLOCK", reason="OPERATION_NOT_AUTHORIZED")
    if intent.target.resource != "service" or action.name != intent.target.name:
        return PolicyResult(status="BLOCK", reason="TASK_SCOPE_MISMATCH")
    if action.arguments.selector != permission.selector:
        return PolicyResult(status="BLOCK", reason="SELECTOR_NOT_AUTHORIZED")
    resolution = resolve_service(intent, inventory)
    if resolution.status != "PASS":
        return PolicyResult(status=resolution.status, reason=resolution.reason, resolution=resolution)
    try:
        backends = selector_backends(inventory, permission.selector)
    except (KeyError, TypeError, ValueError, AttributeError):
        return PolicyResult(status="ESCALATE", reason="INVENTORY_UNVERIFIED", resolution=resolution)
    # The trusted selector must select the trusted backend and nothing else in
    # live inventory. A missing or extra match is ambiguous, so a human decides.
    if backends != (permission.backend,):
        return PolicyResult(status="ESCALATE", reason="SELECTOR_BACKEND_UNVERIFIED", resolution=resolution)
    fields = ("task_id", "namespace", "verb", "name", "arguments.selector")
    return PolicyResult(
        status="PASS", reason="TASK_AUTHORIZED", resolution=resolution,
        bindings=tuple(FieldBinding(field=field, authority="trusted_task") for field in fields)
        + (FieldBinding(field="selector_backend", authority="live_inventory"),),
    )
