"""Prepare an identity/version-bound replacement of one Service's selector map."""

from collections.abc import Mapping

from opsguard.contracts import ActionRequest, SelectorMap, SetSelectorAction, WireModel
from opsguard.intent.resolver import DeploymentIdentity

SelectorKey = tuple[tuple[str, str], ...]


def selector_key(selector: Mapping[str, str]) -> SelectorKey:
    """Order-independent, hashable form of a selector map for lab bounds."""
    return tuple(sorted(selector.items()))


class PreparedSelector(WireModel):
    # DeploymentIdentity is the shared namespace/name/UID/version identity. It
    # carries no Deployment-specific field, so a Service uses it unchanged.
    target: DeploymentIdentity
    selector: SelectorMap

    @property
    def path(self) -> str:
        return f"/api/v1/namespaces/{self.target.namespace}/services/{self.target.name}"

    def body(self) -> list[dict]:
        # JSON Patch "add" on an object member replaces the whole map, and also
        # works when the Service currently has no selector at all.
        return [
            {"op": "test", "path": "/metadata/uid", "value": self.target.uid},
            {"op": "test", "path": "/metadata/resourceVersion", "value": self.target.resource_version},
            {"op": "add", "path": "/spec/selector", "value": dict(sorted(self.selector.items()))},
        ]


def prepare_set_selector(
    request: SetSelectorAction | ActionRequest | Mapping, *, service: dict,
    lab_selectors: Mapping[tuple[str, str], frozenset[SelectorKey]],
) -> PreparedSelector:
    """Bind the request to an independently read Service and the common lab bounds.

    Lab bounds are the synthetic values every defence condition may reach. Task
    authorization is a separate, narrower decision made by the gateway policy.
    """
    action = SetSelectorAction.model_validate(request.model_dump() if isinstance(request, ActionRequest) else request)
    metadata = service["metadata"]
    target = DeploymentIdentity(namespace=metadata["namespace"], name=metadata["name"],
                                uid=metadata["uid"], resource_version=metadata["resourceVersion"])
    if (action.namespace, action.name) != (target.namespace, target.name) or metadata.get("deletionTimestamp"):
        raise ValueError("selector target differs from the live Service")
    spec = service.get("spec")
    if not isinstance(spec, dict) or spec.get("type") == "ExternalName":
        raise ValueError("Service does not route by selector")
    allowed = lab_selectors.get((action.namespace, action.name), frozenset())
    if selector_key(action.arguments.selector) not in allowed:
        raise ValueError("selector target or value is outside lab bounds")
    return PreparedSelector(target=target, selector=action.arguments.selector)
