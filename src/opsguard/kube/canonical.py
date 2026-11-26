"""Map raw Kubernetes write forms to exactly one typed action, or reject them.

The agent should call typed tools. When a raw form reaches the gateway instead
(a kubectl command line, a JSON or merge patch, a server-side-apply body), it is
mapped to the one typed action it expresses, and the gateway decides on that
typed action only. Anything else is ``UNRECOGNIZED_FORM``:

* a verb with no typed operation (delete, create, edit, exec, label, ...);
* more than one object, or more than one change, in one form;
* any field outside the typed action (an extra container field, a second
  ConfigMap key, a label, an unknown kubectl flag);
* a missing namespace: the kubeconfig default must not choose the target.

A merge or strategic patch of ``spec.selector`` maps to ``set_selector``, which
replaces the whole selector map. The typed action is what gets decided and
written, so the gateway never applies the raw merge semantics.
"""

from __future__ import annotations

import json
import shlex
from typing import Literal

from pydantic import TypeAdapter, ValidationError

from opsguard.contracts import ActionRequest, SupportedAction, WireModel

RESTART_ANNOTATION = "kubectl.kubernetes.io/restartedAt"
_KINDS = {
    "deployment": "deployment", "deployments": "deployment", "deploy": "deployment",
    "deployment.apps": "deployment", "deployments.apps": "deployment",
    "service": "service", "services": "service", "svc": "service",
    "configmap": "configmap", "configmaps": "configmap", "cm": "configmap",
}
_MANIFEST_KINDS = {("apps/v1", "Deployment"): "deployment", ("v1", "Service"): "service",
                   ("v1", "ConfigMap"): "configmap"}


class Rejection(WireModel):
    status: Literal["BLOCK"] = "BLOCK"
    reason: Literal["UNRECOGNIZED_FORM"] = "UNRECOGNIZED_FORM"
    detail: str


class _Reject(Exception):
    pass


def _fail(detail: str):
    raise _Reject(detail)


def _only(obj, allowed: set[str], where: str) -> dict:
    if not isinstance(obj, dict):
        _fail(f"{where} must be an object")
    extra = set(obj) - allowed
    if extra:
        _fail(f"{where} has fields outside the typed action: {sorted(extra)}")
    return obj


def _action(verb, resource, namespace, name, arguments) -> ActionRequest:
    if not isinstance(namespace, str) or not namespace:
        _fail("the namespace must be explicit")
    try:
        return TypeAdapter(SupportedAction).validate_python(
            {"verb": verb, "resource": resource, "namespace": namespace, "name": name, "arguments": arguments})
    except ValidationError as error:
        _fail(f"not a valid {verb}: {error.errors()[0]['msg']}")


# ---------------------------------------------------------------------------
# Patch and manifest bodies
# ---------------------------------------------------------------------------

def _deployment_change(spec: dict, where: str):
    """(verb, arguments) for a Deployment spec fragment with exactly one change."""
    spec = _only(spec, {"replicas", "template"}, where)
    if len(spec) != 1:
        _fail(f"{where} must change exactly one thing")
    if "replicas" in spec:
        return "scale", {"replicas": spec["replicas"]}
    template = _only(spec["template"], {"metadata", "spec"}, where + ".template")
    if set(template) == {"metadata"}:
        metadata = _only(template["metadata"], {"annotations"}, where + ".template.metadata")
        annotations = _only(metadata.get("annotations"), {RESTART_ANNOTATION}, where + ".annotations")
        if set(annotations) != {RESTART_ANNOTATION} or not isinstance(annotations[RESTART_ANNOTATION], str):
            _fail("a template annotation other than the restart marker")
        return "restart", {}
    if set(template) == {"spec"}:
        pod = _only(template["spec"], {"containers"}, where + ".template.spec")
        containers = pod.get("containers")
        if not isinstance(containers, list) or len(containers) != 1:
            _fail("exactly one container must change")
        container = _only(containers[0], {"name", "image"}, where + ".containers[0]")
        if set(container) != {"name", "image"}:
            _fail("a container change needs exactly a name and an image")
        return "set_image", {"container": container["name"], "image": container["image"]}
    _fail(f"{where}.template must change exactly one thing")


def _body_change(resource: str, body: dict, where: str):
    """(verb, arguments) for a merge, strategic or apply body of one resource."""
    if resource == "deployment":
        _only(body, {"spec"}, where)
        return _deployment_change(body.get("spec"), where + ".spec")
    if resource == "service":
        _only(body, {"spec"}, where)
        spec = _only(body.get("spec"), {"selector"}, where + ".spec")
        if "selector" not in spec:
            _fail("a Service change must set the selector")
        return "set_selector", {"selector": spec["selector"]}
    _only(body, {"data"}, where)
    data = _only(body.get("data"), set(body.get("data") or {}), where + ".data")
    if len(data) != 1:
        _fail("exactly one ConfigMap key must change")
    (key, value), = data.items()
    return "set_config", {"key": key, "value": value}


def _json_patch_change(resource: str, operations, where: str):
    """(verb, arguments) for an RFC 6902 patch with exactly one add or replace."""
    if not isinstance(operations, list) or len(operations) != 1:
        _fail("a JSON patch must have exactly one operation")
    op = _only(operations[0], {"op", "path", "value"}, where + "[0]")
    if op.get("op") not in ("add", "replace") or "value" not in op or not isinstance(op.get("path"), str):
        _fail("only one add or replace with a value is recognized")
    path, value = op["path"], op["value"]
    restart = "/spec/template/metadata/annotations/" + RESTART_ANNOTATION.replace("~", "~0").replace("/", "~1")
    if resource == "deployment" and path == "/spec/replicas":
        return "scale", {"replicas": value}
    if resource == "deployment" and path == restart and isinstance(value, str):
        return "restart", {}
    if resource == "deployment" and path == "/spec/template/metadata/annotations":
        annotations = _only(value, {RESTART_ANNOTATION}, "annotations")
        if set(annotations) == {RESTART_ANNOTATION}:
            return "restart", {}
    if resource == "service" and path == "/spec/selector":
        return "set_selector", {"selector": value}
    if resource == "configmap" and path.startswith("/data/") and path.count("/") == 2:
        key = path[len("/data/"):].replace("~1", "/").replace("~0", "~")
        return "set_config", {"key": key, "value": value}
    # A container image by list index cannot be bound to a container name.
    _fail(f"the path {path} is not a typed change for a {resource}")


def _manifest(body) -> ActionRequest:
    """A server-side-apply body: one object, identity plus exactly one change."""
    _only(body, {"apiVersion", "kind", "metadata", "spec", "data"}, "manifest")
    resource = _MANIFEST_KINDS.get((body.get("apiVersion"), body.get("kind")))
    if resource is None:
        _fail("only apps/v1 Deployment, v1 Service and v1 ConfigMap bodies are recognized")
    metadata = _only(body.get("metadata"), {"name", "namespace"}, "manifest.metadata")
    if set(metadata) != {"name", "namespace"}:
        _fail("an apply body needs exactly metadata.name and metadata.namespace")
    rest = {key: value for key, value in body.items() if key in ("spec", "data")}
    verb, arguments = _body_change(resource, rest, "manifest")
    return _action(verb, resource, metadata["namespace"], metadata["name"], arguments)


# ---------------------------------------------------------------------------
# kubectl command lines
# ---------------------------------------------------------------------------

def _split_target(tokens: list[str]) -> tuple[str, str, list[str]]:
    """resource/name or resource name, then the remaining tokens."""
    if not tokens:
        _fail("no target")
    first = tokens[0]
    if "/" in first:
        kind, _, name = first.partition("/")
        rest = tokens[1:]
    else:
        if len(tokens) < 2:
            _fail("no target name")
        kind, name, rest = first, tokens[1], tokens[2:]
    resource = _KINDS.get(kind.lower())
    if resource is None or not name:
        _fail(f"unsupported resource {kind!r}")
    return resource, name, rest


def _flags(tokens: list[str], allowed: set[str]) -> tuple[dict[str, str], list[str]]:
    """Parse --flag=value, --flag value and -n value. Unknown flags are rejected."""
    aliases = {"-n": "--namespace", "-p": "--patch"}
    flags, positional, i = {}, [], 0
    while i < len(tokens):
        token = tokens[i]
        if token.startswith("-"):
            name, eq, value = token.partition("=")
            name = aliases.get(name, name)
            if name not in allowed:
                _fail(f"flag {name} is outside the typed action")
            if not eq:
                if i + 1 >= len(tokens):
                    _fail(f"flag {name} needs a value")
                i += 1
                value = tokens[i]
            if name in flags:
                _fail(f"flag {name} is repeated")
            flags[name] = value
        else:
            positional.append(token)
        i += 1
    return flags, positional


def _kubectl(command: str) -> ActionRequest:
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        _fail("the command line cannot be parsed")
    if not tokens or tokens[0] not in ("kubectl", "oc"):
        _fail("not a kubectl command")
    if any(token in ("|", ";", "&&", "||", ">", "<") for token in tokens):
        _fail("pipes and command chains are not recognized")
    verb, rest = (tokens[1] if len(tokens) > 1 else ""), tokens[2:]
    if verb == "scale":
        flags, positional = _flags(rest, {"--namespace", "--replicas"})
        resource, name, extra = _split_target(positional)
        if extra or resource != "deployment" or "--replicas" not in flags:
            _fail("kubectl scale needs exactly one Deployment and --replicas")
        try:
            replicas = int(flags["--replicas"])
        except ValueError:
            _fail("--replicas must be an integer")
        return _action("scale", "deployment", flags.get("--namespace"), name, {"replicas": replicas})
    if verb == "rollout" and rest[:1] == ["restart"]:
        flags, positional = _flags(rest[1:], {"--namespace"})
        resource, name, extra = _split_target(positional)
        if extra or resource != "deployment":
            _fail("kubectl rollout restart needs exactly one Deployment")
        return _action("restart", "deployment", flags.get("--namespace"), name, {})
    if verb == "set" and rest[:1] == ["image"]:
        flags, positional = _flags(rest[1:], {"--namespace"})
        resource, name, extra = _split_target(positional)
        if resource != "deployment" or len(extra) != 1 or "=" not in extra[0]:
            _fail("kubectl set image needs one Deployment and one container=image pair")
        container, _, image = extra[0].partition("=")
        if container == "*":
            _fail("a wildcard container is not one typed change")
        return _action("set_image", "deployment", flags.get("--namespace"), name,
                       {"container": container, "image": image})
    if verb == "patch":
        flags, positional = _flags(rest, {"--namespace", "--patch", "--type"})
        resource, name, extra = _split_target(positional)
        if extra or "--patch" not in flags:
            _fail("kubectl patch needs exactly one object and --patch")
        try:
            patch = json.loads(flags["--patch"])
        except ValueError:
            _fail("the patch is not JSON")
        kind = flags.get("--type", "strategic")
        if kind == "json":
            verb_name, arguments = _json_patch_change(resource, patch, "patch")
        elif kind in ("merge", "strategic"):
            verb_name, arguments = _body_change(resource, patch, "patch")
        else:
            _fail(f"unknown patch type {kind!r}")
        return _action(verb_name, resource, flags.get("--namespace"), name, arguments)
    _fail(f"kubectl {verb} has no typed operation")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _structured(raw: dict) -> ActionRequest:
    form = raw.get("form")
    if form is None:  # Already the typed shape.
        _only(raw, {"verb", "resource", "namespace", "name", "arguments"}, "action")
        return _action(raw.get("verb"), raw.get("resource"), raw.get("namespace"), raw.get("name"),
                       raw.get("arguments"))
    if form == "kubectl":
        _only(raw, {"form", "command"}, "kubectl form")
        if not isinstance(raw.get("command"), str):
            _fail("a kubectl form needs a command string")
        return _kubectl(raw["command"])
    if form == "apply":
        _only(raw, {"form", "manifest"}, "apply form")
        return _manifest(raw.get("manifest"))
    if form == "patch":
        _only(raw, {"form", "resource", "namespace", "name", "patch_type", "patch"}, "patch form")
        resource = _KINDS.get(str(raw.get("resource", "")).lower())
        if resource is None:
            _fail("unsupported resource")
        kind = raw.get("patch_type")
        if kind == "json":
            verb, arguments = _json_patch_change(resource, raw.get("patch"), "patch")
        elif kind in ("merge", "strategic"):
            verb, arguments = _body_change(resource, raw.get("patch"), "patch")
        else:
            _fail("patch_type must be json, merge or strategic")
        return _action(verb, resource, raw.get("namespace"), raw.get("name"), arguments)
    _fail(f"unknown form {form!r}")


def canonicalize(raw) -> ActionRequest | Rejection:
    """The one typed action a raw form expresses, or Rejection(UNRECOGNIZED_FORM)."""
    try:
        if isinstance(raw, ActionRequest):
            return TypeAdapter(SupportedAction).validate_python(raw.model_dump())
        if isinstance(raw, str):
            text = raw.strip()
            if text.startswith("{"):
                try:
                    raw = json.loads(text)
                except ValueError:
                    _fail("not JSON")
            else:
                return _kubectl(text)
        if not isinstance(raw, dict):
            _fail("unsupported input type")
        if "apiVersion" in raw or "kind" in raw:
            return _manifest(raw)
        return _structured(raw)
    except _Reject as error:
        return Rejection(detail=str(error)[:300])
    except (TypeError, ValueError, KeyError, AttributeError) as error:
        return Rejection(detail=f"malformed: {type(error).__name__}")


# ---------------------------------------------------------------------------
# Paraphrases: the same typed action written other ways (for the security suite)
# ---------------------------------------------------------------------------

def paraphrases(action: ActionRequest) -> list:
    """At least five raw forms that must canonicalize to exactly ``action``."""
    a = TypeAdapter(SupportedAction).validate_python(action.model_dump())
    ns, name, args = a.namespace, a.name, a.arguments.model_dump()
    q = shlex.quote
    if a.verb == "scale":
        n = args["replicas"]
        body = {"spec": {"replicas": n}}
        return [f"kubectl scale deployment/{name} --replicas={n} -n {ns}",
                f"kubectl scale deploy {name} --replicas {n} --namespace={ns}",
                f"kubectl patch deployment {name} -n {ns} --type=merge -p {q(json.dumps(body))}",
                {"form": "patch", "resource": "deployments.apps", "namespace": ns, "name": name, "patch_type": "json",
                 "patch": [{"op": "replace", "path": "/spec/replicas", "value": n}]},
                {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns}, **body},
                json.dumps({"form": "patch", "resource": "deploy", "namespace": ns, "name": name,
                            "patch_type": "strategic", "patch": body})]
    if a.verb == "restart":
        body = {"spec": {"template": {"metadata": {"annotations": {RESTART_ANNOTATION: "2026-11-26T00:00:00Z"}}}}}
        return [f"kubectl rollout restart deployment/{name} -n {ns}",
                f"kubectl rollout restart deploy {name} --namespace {ns}",
                f"kubectl patch deploy/{name} --namespace={ns} -p {q(json.dumps(body))}",
                {"form": "patch", "resource": "deployment", "namespace": ns, "name": name, "patch_type": "json",
                 "patch": [{"op": "add", "path": "/spec/template/metadata/annotations/kubectl.kubernetes.io~1restartedAt",
                            "value": "now"}]},
                {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns}, **body}]
    if a.verb == "set_image":
        container, image = args["container"], args["image"]
        body = {"spec": {"template": {"spec": {"containers": [{"name": container, "image": image}]}}}}
        return [f"kubectl set image deployment/{name} {container}={image} -n {ns}",
                f"kubectl set image deploy {name} {container}={image} --namespace={ns}",
                f"kubectl patch deployment/{name} -n {ns} --type strategic -p {q(json.dumps(body))}",
                {"form": "patch", "resource": "deployment", "namespace": ns, "name": name, "patch_type": "merge",
                 "patch": body},
                {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns}, **body}]
    if a.verb == "set_selector":
        selector = args["selector"]
        body = {"spec": {"selector": selector}}
        return [f"kubectl patch service {name} -n {ns} --type=merge -p {q(json.dumps(body))}",
                f"kubectl patch svc/{name} --namespace {ns} --type json "
                f"-p {q(json.dumps([{'op': 'replace', 'path': '/spec/selector', 'value': selector}]))}",
                {"form": "patch", "resource": "services", "namespace": ns, "name": name, "patch_type": "strategic",
                 "patch": body},
                {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": ns}, **body},
                json.dumps({"form": "kubectl", "command": f"kubectl patch service/{name} -n {ns} "
                                                          f"-p {q(json.dumps(body))}"})]
    key, value = args["key"], args["value"]
    body = {"data": {key: value}}
    escaped = key.replace("~", "~0").replace("/", "~1")
    return [f"kubectl patch configmap {name} -n {ns} --type=merge -p {q(json.dumps(body))}",
            f"kubectl patch cm/{name} --namespace={ns} --type=json "
            f"-p {q(json.dumps([{'op': 'add', 'path': '/data/' + escaped, 'value': value}]))}",
            {"form": "patch", "resource": "configmap", "namespace": ns, "name": name, "patch_type": "merge",
             "patch": body},
            {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": name, "namespace": ns}, **body},
            {"form": "kubectl", "command": f"kubectl patch configmaps {name} -n {ns} -p {q(json.dumps(body))}"}]
