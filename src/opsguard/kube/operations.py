"""Trusted operation registration. Model inputs never specify API paths or patches."""

from dataclasses import dataclass, field
import json
from types import MappingProxyType
from typing import Callable, Literal, Mapping
from uuid import uuid4

from pydantic import TypeAdapter

from opsguard.contracts import SupportedAction, WireModel
from opsguard.intent.resolver import DeploymentIdentity
from opsguard.operations.restart import prepare_restart
from opsguard.operations.scale import ScaleTarget, prepare_scale
from opsguard.operations.set_image import prepare_set_image
from opsguard.operations.set_selector import SelectorKey, prepare_set_selector


@dataclass(frozen=True)
class LabBounds:
    namespace: str
    scales: Mapping[tuple[str, str], tuple[int, int]] = field(default_factory=dict)
    images: Mapping[tuple[str, str, str], frozenset[str]] = field(default_factory=dict)
    restarts: frozenset[tuple[str, str]] = frozenset()
    # Service -> the whole selector maps (as sorted item tuples) any condition may write.
    selectors: Mapping[tuple[str, str], frozenset[SelectorKey]] = field(default_factory=dict)
    configs: Mapping[tuple[str, str, str], frozenset[str]] = field(default_factory=dict)

    def __post_init__(self):
        from opsguard.contracts import DnsLabel
        TypeAdapter(DnsLabel).validate_python(self.namespace)
        object.__setattr__(self, "scales", MappingProxyType(dict(self.scales)))
        object.__setattr__(self, "images", MappingProxyType({key: frozenset(values) for key, values in self.images.items()}))
        object.__setattr__(self, "restarts", frozenset(self.restarts))
        object.__setattr__(self, "selectors", MappingProxyType({key: frozenset(values) for key, values in self.selectors.items()}))
        object.__setattr__(self, "configs", MappingProxyType({key: frozenset(values) for key, values in self.configs.items()}))
        if any(key[0] != self.namespace for group in (self.scales, self.images, self.restarts, self.selectors, self.configs) for key in group):
            raise ValueError("lab bounds must use the configured namespace")


class PreparedMutation(WireModel):
    preparation_id: str
    operation: Literal["scale", "set_image", "restart", "set_selector", "set_config"]
    target: DeploymentIdentity
    method: Literal["PUT", "PATCH"]
    path: str
    content_type: Literal["application/json", "application/json-patch+json"]
    body_json: str

    def body(self):
        # Return a copy; neither callers nor transports can mutate preparation.
        return json.loads(self.body_json)


def _wrap(operation, prepared, method, content_type):
    return PreparedMutation(preparation_id=uuid4().hex, operation=operation,
        target=DeploymentIdentity.model_validate(prepared.target.model_dump()), method=method,
        path=prepared.path, content_type=content_type,
        body_json=json.dumps(prepared.body(), sort_keys=True, separators=(",", ":"), allow_nan=False))


def _scale(action, deployment, bounds):
    meta = deployment["metadata"]
    target = ScaleTarget(namespace=meta["namespace"], name=meta["name"], uid=meta["uid"], resource_version=meta["resourceVersion"])
    return _wrap("scale", prepare_scale(action, target=target, lab_bounds=bounds.scales), "PUT", "application/json")


def _image(action, deployment, bounds):
    return _wrap("set_image", prepare_set_image(action, deployment=deployment, lab_images=bounds.images),
                 "PATCH", "application/json-patch+json")


def _restart(action, deployment, bounds):
    return _wrap("restart", prepare_restart(action, deployment=deployment, allowed_targets=bounds.restarts),
                 "PATCH", "application/json-patch+json")


def _selector(action, service, bounds):
    return _wrap("set_selector", prepare_set_selector(action, service=service, lab_selectors=bounds.selectors),
                 "PATCH", "application/json-patch+json")


def _config(action, obj, bounds):
    from opsguard.operations.set_config import prepare_set_config
    prepared = prepare_set_config(action.model_dump(), lab_values=bounds.configs)
    meta = obj["metadata"]
    if (prepared.namespace, prepared.name) != (meta["namespace"], meta["name"]):
        raise ValueError("ConfigMap identity mismatch")
    target = DeploymentIdentity(namespace=meta["namespace"], name=meta["name"], uid=meta["uid"], resource_version=meta["resourceVersion"])
    escape = lambda value: value.replace("~", "~0").replace("/", "~1")
    if not isinstance(obj.get("data"), dict):
        raise ValueError("ConfigMap needs a data map")
    body = [{"op":"test", "path":"/metadata/uid", "value":target.uid},
            {"op":"test", "path":"/metadata/resourceVersion", "value":target.resource_version},
            {"op":"add", "path":"/data/" + escape(prepared.key), "value":prepared.value}]
    return PreparedMutation(preparation_id=uuid4().hex, operation="set_config", target=target,
        method="PATCH", path=f"/api/v1/namespaces/{target.namespace}/configmaps/{target.name}",
        content_type="application/json-patch+json", body_json=json.dumps(body))


class OperationRegistry:
    """Register code-owned handlers at application startup, never through a model tool."""

    def __init__(self):
        self._handlers: dict[str, Callable] = {}

    def register(self, verb: str, handler: Callable) -> None:
        if verb in self._handlers:
            raise ValueError("operation already registered")
        self._handlers[verb] = handler

    def prepare(self, request, deployment: dict, bounds: LabBounds) -> PreparedMutation:
        # "deployment" is the independently read live target object. For
        # set_selector that object is the Service.
        action = TypeAdapter(SupportedAction).validate_python(request.model_dump() if hasattr(request, "model_dump") else request)
        if action.namespace != bounds.namespace or deployment["metadata"].get("deletionTimestamp"):
            raise ValueError("target is outside the live lab")
        handler = self._handlers.get(action.verb)
        if handler is None:
            raise ValueError("unsupported operation")
        return handler(action, deployment, bounds)


def default_registry() -> OperationRegistry:
    registry = OperationRegistry()
    for verb, handler in (("scale", _scale), ("set_image", _image), ("restart", _restart),
                          ("set_selector", _selector), ("set_config", _config)):
        registry.register(verb, handler)
    return registry
