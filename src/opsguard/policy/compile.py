"""Compile one signed intent into gateway rules and a ValidatingAdmissionPolicy.

Both enforcement points use the same expression strings:

* the gateway evaluates them with ``opsguard.policy.cel`` on the dry-run object
  (``object``) against the live object (``oldObject``);
* the API server evaluates them in ValidatingAdmissionPolicies, bound to the
  intent's namespace and matched only for the agent's ServiceAccount, so a
  write that bypasses the gateway is still refused.

Each rule is wrapped in a resource guard. The API server type-checks every
expression against every kind a policy matches, so there is one policy per
resource (Deployments, the scale subresource, Services, ConfigMaps), each
holding only the rules for that resource. The expression strings are the same
ones the gateway evaluates.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import httpx

from opsguard.intent.models import IntentSpecV2
from opsguard.policy.cel import CelError, evaluate, literal, parse, quote

RESTART = "kubectl.kubernetes.io/restartedAt"
RESOURCE_GUARDS = {
    "deployments": "request.resource.resource == 'deployments' && request.subResource == ''",
    "deployments/scale": "request.resource.resource == 'deployments' && request.subResource == 'scale'",
    "services": "request.resource.resource == 'services' && request.subResource == ''",
    "configmaps": "request.resource.resource == 'configmaps' && request.subResource == ''",
}
_KIND_RESOURCES = {"Deployment": ("deployments", "deployments/scale"), "StatefulSet": (),
                   "Service": ("services",), "ConfigMap": ("configmaps",),
                   "any": ("deployments", "deployments/scale", "services", "configmaps")}


@dataclass(frozen=True)
class Rule:
    id: str
    resources: tuple[str, ...]
    check: str
    message: str

    @property
    def expression(self) -> str:
        """The exact string both enforcement points evaluate."""
        guard = " || ".join(f"({RESOURCE_GUARDS[r]})" for r in self.resources)
        return f"!({guard}) || ({self.check})" if self.resources else self.check


@dataclass(frozen=True)
class Violation:
    rule: str
    message: str


@dataclass(frozen=True)
class GatewayRules:
    rules: tuple[Rule, ...]
    namespace: str
    service_account: str

    @property
    def username(self) -> str:
        return f"system:serviceaccount:{self.namespace}:{self.service_account}"

    def check(self, *, operation: str, resource: str, sub_resource: str, name: str,
              obj: Optional[dict], old: Optional[dict]) -> tuple[Violation, ...]:
        """Evaluate every rule for one request; an evaluation error is a violation (fail closed)."""
        env = {"object": obj, "oldObject": old,
               "request": {"operation": operation, "namespace": self.namespace, "name": name,
                           "resource": {"resource": resource, "group": "apps" if resource == "deployments" else ""},
                           "subResource": sub_resource, "userInfo": {"username": self.username}}}
        violations = []
        for rule in self.rules:
            try:
                passed = evaluate(rule.expression, env)
            except CelError as error:
                violations.append(Violation(rule.id, f"{rule.message} (evaluation error: {error})"))
                continue
            if not passed:
                violations.append(Violation(rule.id, rule.message))
        return tuple(violations)


@dataclass(frozen=True)
class CompiledPolicy:
    gateway: GatewayRules
    policies: tuple[dict, ...]
    bindings: tuple[dict, ...]
    expires_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def vap_yaml(self) -> str:
        # JSON is valid YAML; kubectl and the API accept it.
        return json.dumps({"apiVersion": "v1", "kind": "List", "items": list(self.policies)}, indent=2)

    @property
    def binding_yaml(self) -> str:
        return json.dumps({"apiVersion": "v1", "kind": "List", "items": list(self.bindings)}, indent=2)


def _names(values) -> str:
    return literal(sorted(set(values)))


def _or_empty(path: str) -> str:
    """CEL for oldObject.<path>, or {} when absent (a guard against missing maps)."""
    head, _, last = path.rpartition(".")
    return f"(has({head}.{last}) ? {path} : {{}})"


def build_rules(intent: IntentSpecV2, *, workload: Optional[str] = None) -> tuple[Rule, ...]:
    """The rule list for one intent. ``workload`` is the resolved Deployment for workload verbs."""
    verbs = set(intent.verbs)
    workload_verbs = verbs & {"scale", "restart", "set_image"}
    if workload_verbs and not workload:
        if intent.target.kind_hint == "Deployment":
            workload = intent.target.name
        else:
            raise ValueError("workload verbs need the resolved Deployment name")
    deployments = [workload] if workload_verbs else []
    services = [intent.target.name] if "set_selector" in verbs else []
    configmaps = [intent.target.name] if "set_config" in verbs else []

    rules = [
        Rule("updates-only", (), "request.operation == 'UPDATE'",
             "OpsGuard: the agent may only update existing objects (no create or delete)"),
        Rule("target-deployment", ("deployments", "deployments/scale"),
             f"object.metadata.name in {_names(deployments)}",
             "OpsGuard: only the intent's Deployment may change"),
        Rule("target-service", ("services",), f"object.metadata.name in {_names(services)}",
             "OpsGuard: only the intent's Service may change"),
        Rule("target-configmap", ("configmaps",), f"object.metadata.name in {_names(configmaps)}",
             "OpsGuard: only the intent's ConfigMap may change"),
    ]
    for kind, resources in _KIND_RESOURCES.items():
        names = [ref.name for ref in intent.protected
                 if ref.kind == kind and ref.namespace in (None, intent.namespace) and ref.name != "*"]
        if names and resources:
            rules.append(Rule(f"protected-{kind.lower()}", resources, f"!(object.metadata.name in {_names(names)})",
                              f"OpsGuard: {', '.join(sorted(set(names)))} is protected"))

    if "scale" in verbs:
        low, high = intent.bounds.replicas.min, intent.bounds.replicas.max
        replicas = f"object.spec.replicas >= {low} && object.spec.replicas <= {high}"
        replicas_message = f"OpsGuard: replicas must stay within {low}-{high}"
    else:
        replicas = "object.spec.replicas == oldObject.spec.replicas"
        replicas_message = "OpsGuard: the replica count may not change"
    rules.append(Rule("replicas", ("deployments", "deployments/scale"), replicas, replicas_message))

    unchanged = "oldObject.spec.template.spec.containers.exists(o, o.name == c.name && o.image == c.image)"
    if "set_image" in verbs:
        image = intent.bounds.image
        allowed = f"(c.name == {quote(image.container)} && c.image in {_names(image.allowed)})"
        images = f"object.spec.template.spec.containers.all(c, {unchanged} || {allowed})"
        images_message = f"OpsGuard: container {image.container} may only use {', '.join(image.allowed)}"
    else:
        images = f"object.spec.template.spec.containers.all(c, {unchanged})"
        images_message = "OpsGuard: container images may not change"
    rules.append(Rule("images", ("deployments",), images, images_message))

    if "restart" not in verbs:
        def marker(root: str) -> str:
            annotations = f"{root}.spec.template.metadata.annotations"
            return (f"(has({annotations}) && {quote(RESTART)} in {annotations} "
                    f"? {annotations}[{quote(RESTART)}] : '')")
        rules.append(Rule("restart", ("deployments",), f"{marker('object')} == {marker('oldObject')}",
                          "OpsGuard: a restart is not part of this intent"))

    new_selector, old_selector = _or_empty("object.spec.selector"), _or_empty("oldObject.spec.selector")
    if "set_selector" in verbs:
        selector = f"{new_selector} == {literal(dict(intent.bounds.selector.selector))} || {new_selector} == {old_selector}"
        selector_message = "OpsGuard: the selector may only become the approved selector"
    else:
        selector = f"{new_selector} == {old_selector}"
        selector_message = "OpsGuard: the Service selector may not change"
    rules.append(Rule("selector", ("services",), selector, selector_message))

    new_data, old_data = _or_empty("object.data"), _or_empty("oldObject.data")
    if "set_config" in verbs:
        config = intent.bounds.config
        key = quote(config.key)
        data = (f"{new_data}.all(k, k == {key} ? object.data[k] in {_names(config.values)} "
                f": (has(oldObject.data) && k in oldObject.data && oldObject.data[k] == object.data[k])) "
                f"&& {old_data}.all(k, has(object.data) && k in object.data)")
        data_message = f"OpsGuard: only key {config.key} may change, to {', '.join(config.values)}"
    else:
        data = f"{new_data} == {old_data}"
        data_message = "OpsGuard: ConfigMap data may not change"
    rules.append(Rule("configmap-data", ("configmaps",), data, data_message))

    for rule in rules:
        parse(rule.expression)  # Every compiled rule must be inside the documented subset.
    return tuple(rules)


_MATCH = {"deployments": ("apps", "deployments"), "deployments/scale": ("apps", "deployments/scale"),
          "services": ("", "services"), "configmaps": ("", "configmaps")}


def compile_policy(intent: IntentSpecV2, *, workload: Optional[str] = None,
                   service_account: str = "agent") -> CompiledPolicy:
    """Gateway rules plus one ValidatingAdmissionPolicy and binding per resource, from one intent."""
    rules = build_rules(intent, workload=workload)
    base = f"opsguard-{intent.namespace}-{intent.id}"[:200]
    username = f"system:serviceaccount:{intent.namespace}:{service_account}"
    labels = {"opsguard.io/intent": intent.id}
    policies, bindings = [], []
    for resource, (group, plural) in _MATCH.items():
        name = f"{base}-{resource.replace('/', '-')}"
        policies.append({
            "apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicy",
            "metadata": {"name": name, "labels": labels},
            "spec": {
                "failurePolicy": "Fail",
                "matchConstraints": {"resourceRules": [{"apiGroups": [group], "apiVersions": ["v1"],
                                                        "operations": ["CREATE", "UPDATE", "DELETE"],
                                                        "resources": [plural]}]},
                # Only the agent's identity is constrained; the fixture and observers are not.
                "matchConditions": [{"name": "agent-only",
                                     "expression": f"request.userInfo.username == {quote(username)}"}],
                "validations": [{"expression": rule.expression, "message": rule.message} for rule in rules
                                if not rule.resources or resource in rule.resources],
            },
        })
        bindings.append({
            "apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicyBinding",
            "metadata": {"name": name, "labels": labels},
            "spec": {"policyName": name, "validationActions": ["Deny"],
                     "matchResources": {"namespaceSelector": {"matchLabels": {
                         "kubernetes.io/metadata.name": intent.namespace}}}},
        })
    return CompiledPolicy(GatewayRules(rules, intent.namespace, service_account), tuple(policies), tuple(bindings),
                          intent.expires_at)


# ---------------------------------------------------------------------------
# Installation: at trial start, removed at expiry or cleanup
# ---------------------------------------------------------------------------

_VAP_PATH = "/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicies"
_BINDING_PATH = "/apis/admissionregistration.k8s.io/v1/validatingadmissionpolicybindings"


class InstalledPolicy:
    """Install the policies and bindings with an admin client; remove them at expiry or on exit."""

    def __init__(self, compiled: CompiledPolicy, admin: httpx.AsyncClient):
        self.compiled, self.admin = compiled, admin
        self._expiry: Optional[asyncio.Task] = None
        self.removed = False
        self._created: list[str] = []

    async def __aenter__(self) -> InstalledPolicy:
        try:
            for path, objects in ((_VAP_PATH, self.compiled.policies), (_BINDING_PATH, self.compiled.bindings)):
                for obj in objects:
                    (await self.admin.post(path, json=obj, timeout=30)).raise_for_status()
                    self._created.append(f"{path}/{obj['metadata']['name']}")
        except BaseException:
            await self.remove()
            raise
        delay = (self.compiled.expires_at - datetime.now(timezone.utc)).total_seconds()
        self._expiry = asyncio.create_task(self._remove_after(max(delay, 0)))
        return self

    async def _remove_after(self, delay: float) -> None:
        await asyncio.sleep(delay)
        await self.remove()

    async def remove(self) -> None:
        """Delete bindings before policies, so a policy never applies half-installed."""
        if self.removed:
            return
        self.removed = True
        bindings = [path for path in self._created if path.startswith(_BINDING_PATH)]
        policies = [path for path in self._created if not path.startswith(_BINDING_PATH)]
        for path in bindings + policies:
            response = await self.admin.delete(path, timeout=30)
            if response.status_code not in (200, 202, 404):
                response.raise_for_status()

    async def __aexit__(self, *exc) -> None:
        if self._expiry is not None:
            self._expiry.cancel()
        await self.remove()
