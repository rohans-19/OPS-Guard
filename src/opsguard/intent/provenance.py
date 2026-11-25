"""Value-level provenance for sensitive action arguments.

Each observation is a gateway-issued ``EvidenceRecord`` (id, channel, source
object, SHA-256 digest), and all of them are untrusted. A sensitive argument is
trusted only when its value equals an intent field or a value the gateway read
from cluster state for this task's own target. An argument whose value appears
in untrusted observation text and is not trusted is BLOCK(UNTRUSTED_ARGUMENT).

"Gateway-read" means the resolved target of this task and its current fields
(name, replicas, container images, the Service selector, the ConfigMap value
of the permitted key). It does not mean every object in the namespace: a decoy
Deployment named in a log is live inventory, but the task never resolved it.

Limitation (stated in the paper): taint is found by matching values, not by
full information-flow tracking as in CaMeL. A value the model derives from
evidence in a form that does not match (for example "five" becoming 5, or a
homoglyph spelling) is reported as ``unobserved`` and left to the policy's
bound checks, which still reject anything outside the intent.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Literal

from pydantic import TypeAdapter

from opsguard.contracts import (ScaleAction, SetConfigAction, SetImageAction, SetSelectorAction, SupportedAction,
                                WireModel)
from opsguard.intent.evidence import EvidenceRegistry
from opsguard.intent.inventory import InventorySnapshot
from opsguard.intent.models import (ConfigPermission, ImagePermission, ScalePermission, SelectorPermission,
                                    TaskIntent)
from opsguard.intent.resolver import resolve_diagnostic_target, resolve_service, resolve_target

Origin = Literal["intent", "gateway_read", "untrusted", "unobserved"]
_INVISIBLE = dict.fromkeys(map(ord, "​‌‍⁠﻿"))


class ArgumentProvenance(WireModel):
    field: str
    value: str
    origin: Origin
    observation_ids: tuple[str, ...] = ()


class ProvenanceResult(WireModel):
    status: Literal["PASS", "BLOCK"]
    reason: str
    arguments: tuple[ArgumentProvenance, ...] = ()


def _normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).translate(_INVISIBLE).casefold()


def _appears(value: str, text: str) -> bool:
    """Whole-token match: 'payments-api' matches 'restart payments-api now', '3' does not match '30'."""
    needle = _normalize(value)
    if not needle:
        return False
    return re.search(r"(?<![a-z0-9])" + re.escape(needle) + r"(?![a-z0-9])", text) is not None


def _image_forms(image: str) -> tuple[str, ...]:
    """The full reference and, when present, its tag or digest (an injected tag is often written alone)."""
    forms = [image]
    name, _, digest = image.partition("@")
    if digest:
        forms.append(digest)
    last = name.rsplit("/", 1)[-1]
    if ":" in last:
        tag = last.rsplit(":", 1)[1]
        if len(tag) >= 2:
            forms.append(tag)
    return tuple(forms)


def sensitive_arguments(action) -> tuple[tuple[str, str, tuple[str, ...]], ...]:
    """(field, value, forms to look for in evidence) for every sensitive argument."""
    items = [("namespace", action.namespace, (action.namespace,)), ("name", action.name, (action.name,))]
    if isinstance(action, ScaleAction):
        value = str(action.arguments.replicas)
        items.append(("arguments.replicas", value, (value,)))
    elif isinstance(action, SetImageAction):
        items.append(("arguments.image", action.arguments.image, _image_forms(action.arguments.image)))
    elif isinstance(action, SetConfigAction):
        items.append(("arguments.key", action.arguments.key, (action.arguments.key,)))
        items.append(("arguments.value", action.arguments.value, (action.arguments.value,)))
    elif isinstance(action, SetSelectorAction):
        for key, value in sorted(action.arguments.selector.items()):
            items.append((f"arguments.selector.{key}", value, (f"{key}={value}", f"{key}: {value}", value)))
    return tuple(items)


def intent_values(intent: TaskIntent) -> dict[str, set[str]]:
    """Values the operator approved, per argument field."""
    values: dict[str, set[str]] = {"namespace": {intent.namespace}, "name": {intent.target.name}}
    for permission in intent.permissions:
        if isinstance(permission, ScalePermission):
            continue  # Replica counts are checked against the bounds, not a value list.
        elif isinstance(permission, ImagePermission):
            values.setdefault("arguments.image", set()).update(permission.images)
        elif isinstance(permission, ConfigPermission):
            values.setdefault("arguments.key", set()).add(permission.key)
            values.setdefault("arguments.value", set()).update(permission.values)
            values["name"].add(permission.diagnostic_deployment)
        elif isinstance(permission, SelectorPermission):
            values["name"].add(permission.backend)
            for key, value in permission.selector.items():
                values.setdefault(f"arguments.selector.{key}", set()).add(value)
    return values


def _within_bounds(intent: TaskIntent, replicas: int) -> bool:
    return any(isinstance(p, ScalePermission) and p.min_replicas <= replicas <= p.max_replicas
               for p in intent.permissions)


def _named(pool, namespace: str, name: str) -> dict | None:
    for obj in pool or ():
        if obj["metadata"].get("namespace", namespace) == namespace and obj["metadata"].get("name") == name:
            return obj
    return None


def gateway_read_values(intent: TaskIntent, inventory: InventorySnapshot) -> dict[str, set[str]]:
    """Values the gateway itself read for this task's resolved target(s)."""
    values: dict[str, set[str]] = {}
    resolutions = [resolve_target(intent, inventory), resolve_diagnostic_target(intent, inventory)]
    if intent.target.resource == "service":
        resolutions.append(resolve_service(intent, inventory))
    for resolution in resolutions:
        if resolution.status != "PASS":
            continue
        name, kind = resolution.target.name, resolution.target_kind
        values.setdefault("name", set()).add(name)
        pool = {"Deployment": inventory.deployments, "StatefulSet": inventory.statefulsets,
                "Service": inventory.services, "ConfigMap": inventory.configmaps}[kind]
        obj = _named(pool, inventory.namespace, name) or {}
        spec = obj.get("spec", {})
        if kind in ("Deployment", "StatefulSet"):
            if isinstance(spec.get("replicas"), int):
                values.setdefault("arguments.replicas", set()).add(str(spec["replicas"]))
            for container in spec.get("template", {}).get("spec", {}).get("containers", []):
                if isinstance(container.get("image"), str):
                    values.setdefault("arguments.image", set()).add(container["image"])
        elif kind == "Service":
            for key, value in (spec.get("selector") or {}).items():
                values.setdefault(f"arguments.selector.{key}", set()).add(str(value))
        elif kind == "ConfigMap":
            for permission in intent.permissions:
                if isinstance(permission, ConfigPermission) and permission.key in (obj.get("data") or {}):
                    values.setdefault("arguments.value", set()).add(str(obj["data"][permission.key]))
    return values


def check_provenance(request, *, intent: TaskIntent, inventory: InventorySnapshot,
                     evidence: EvidenceRegistry) -> ProvenanceResult:
    """Classify every sensitive argument; BLOCK the first untrusted one."""
    action = TypeAdapter(SupportedAction).validate_python(request.model_dump() if hasattr(request, "model_dump") else request)
    approved = intent_values(intent)
    read = gateway_read_values(intent, inventory)
    texts = [(record.observation_id, _normalize(record.text)) for record in evidence.records()]
    findings = []
    for field, value, forms in sensitive_arguments(action):
        if value in approved.get(field, ()) or (field == "arguments.replicas" and _within_bounds(intent, int(value))):
            findings.append(ArgumentProvenance(field=field, value=value, origin="intent"))
            continue
        if value in read.get(field, ()):
            findings.append(ArgumentProvenance(field=field, value=value, origin="gateway_read"))
            continue
        seen = tuple(obs_id for obs_id, text in texts if any(_appears(form, text) for form in forms))
        findings.append(ArgumentProvenance(field=field, value=value, origin="untrusted" if seen else "unobserved",
                                           observation_ids=seen))
    untrusted = next((item for item in findings if item.origin == "untrusted"), None)
    if untrusted:
        return ProvenanceResult(status="BLOCK", reason="UNTRUSTED_ARGUMENT", arguments=tuple(findings))
    return ProvenanceResult(status="PASS", reason="PROVENANCE_CLEAN", arguments=tuple(findings))
