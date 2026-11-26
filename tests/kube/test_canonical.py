"""Every raw form maps to exactly one typed action, or is rejected with UNRECOGNIZED_FORM."""

import json

import pytest

from opsguard.contracts import (RestartAction, ScaleAction, SetConfigAction, SetImageAction, SetSelectorAction)
from opsguard.kube.canonical import Rejection, canonicalize, paraphrases

NS = "shop"
SCALE = ScaleAction(verb="scale", resource="deployment", namespace=NS, name="checkout-api", arguments={"replicas": 3})
RESTART = RestartAction(verb="restart", resource="deployment", namespace=NS, name="checkout-api", arguments={})
IMAGE = SetImageAction(verb="set_image", resource="deployment", namespace=NS, name="checkout-api",
                       arguments={"container": "app", "image": "shop/app:v2"})
SELECTOR = SetSelectorAction(verb="set_selector", resource="service", namespace=NS, name="checkout",
                             arguments={"selector": {"app": "checkout-api"}})
CONFIG = SetConfigAction(verb="set_config", resource="configmap", namespace=NS, name="app-settings",
                         arguments={"key": "mode", "value": "enabled"})
ALL = [SCALE, RESTART, IMAGE, SELECTOR, CONFIG]


@pytest.mark.parametrize("action", ALL, ids=lambda a: a.verb)
def test_typed_form_and_every_paraphrase_round_trip(action):
    assert canonicalize(action) == action
    assert canonicalize(action.model_dump()) == action
    forms = paraphrases(action)
    kinds = {"kubectl" if isinstance(f, str) and f.startswith("kubectl") else
             "json-string" if isinstance(f, str) else
             "manifest" if "apiVersion" in f else f.get("form") for f in forms}
    assert len(forms) >= 5 and {"kubectl", "manifest"} <= kinds
    for form in forms:
        assert canonicalize(form) == action, form


@pytest.mark.parametrize("raw,expected", [
    ("kubectl scale deployment.apps/checkout-api --replicas=3 --namespace shop", SCALE),
    ("oc scale deploy/checkout-api --replicas 3 -n shop", SCALE),
    ("kubectl patch deploy checkout-api -n shop --type json "
     "-p '[{\"op\":\"replace\",\"path\":\"/spec/replicas\",\"value\":3}]'", SCALE),
    ("kubectl patch deployment checkout-api -n shop --type=json -p "
     "'[{\"op\":\"add\",\"path\":\"/spec/template/metadata/annotations\","
     "\"value\":{\"kubectl.kubernetes.io/restartedAt\":\"t\"}}]'", RESTART),
    ({"form": "patch", "resource": "cm", "namespace": NS, "name": "app-settings", "patch_type": "json",
      "patch": [{"op": "replace", "path": "/data/mode", "value": "enabled"}]}, CONFIG),
    (json.dumps(SELECTOR.model_dump()), SELECTOR),
])
def test_other_spellings(raw, expected):
    assert canonicalize(raw) == expected


@pytest.mark.parametrize("raw", [
    # Verbs with no typed operation.
    "kubectl delete deployment checkout-api -n shop",
    "kubectl apply -f deploy.yaml -n shop",
    "kubectl exec -it checkout-api -n shop -- sh",
    "kubectl label deployment checkout-api team=x -n shop",
    "kubectl create configmap x -n shop",
    "helm upgrade checkout ./chart -n shop",
    # The kubeconfig default must not choose the namespace.
    "kubectl scale deployment/checkout-api --replicas=3",
    {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "checkout-api"}, "spec": {"replicas": 3}},
    # More than one object, or a selection.
    "kubectl scale deployment/a deployment/b --replicas=3 -n shop",
    "kubectl scale deployment --all --replicas=0 -n shop",
    "kubectl scale deployment -l app=x --replicas=0 -n shop",
    # Flags outside the typed action.
    "kubectl scale deployment/checkout-api --replicas=3 -n shop --context prod",
    "kubectl rollout restart deployment/checkout-api -n shop --kubeconfig other",
    "kubectl scale deployment/checkout-api --replicas=3 -n shop -n other",
    # More than one change, or fields outside the typed action.
    "kubectl patch deployment checkout-api -n shop -p '{\"spec\":{\"replicas\":3,\"paused\":true}}'",
    "kubectl patch deployment checkout-api -n shop -p '{\"metadata\":{\"labels\":{\"a\":\"b\"}},\"spec\":{\"replicas\":3}}'",
    "kubectl patch configmap app-settings -n shop -p '{\"data\":{\"mode\":\"enabled\",\"other\":\"x\"}}'",
    "kubectl patch service checkout -n shop -p '{\"spec\":{\"selector\":{\"app\":\"x\"},\"type\":\"NodePort\"}}'",
    "kubectl patch deployment checkout-api -n shop -p "
    "'{\"spec\":{\"template\":{\"spec\":{\"containers\":[{\"name\":\"app\",\"image\":\"x\",\"command\":[\"sh\"]}]}}}}'",
    "kubectl patch deployment checkout-api -n shop -p "
    "'{\"spec\":{\"template\":{\"metadata\":{\"annotations\":{\"other\":\"x\"}}}}}'",
    "kubectl set image deployment/checkout-api *=evil:1 -n shop",
    "kubectl set image deployment/checkout-api app=a:1 sidecar=b:1 -n shop",
    {"form": "patch", "resource": "deployment", "namespace": NS, "name": "checkout-api", "patch_type": "json",
     "patch": [{"op": "replace", "path": "/spec/replicas", "value": 3},
               {"op": "replace", "path": "/spec/paused", "value": True}]},
    {"form": "patch", "resource": "deployment", "namespace": NS, "name": "checkout-api", "patch_type": "json",
     "patch": [{"op": "replace", "path": "/spec/template/spec/containers/0/image", "value": "x"}]},
    {"form": "patch", "resource": "deployment", "namespace": NS, "name": "checkout-api", "patch_type": "json",
     "patch": [{"op": "remove", "path": "/spec/replicas"}]},
    {"apiVersion": "v1", "kind": "Secret", "metadata": {"name": "s", "namespace": NS}, "data": {"k": "dg=="}},
    {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": "r", "namespace": NS}},
    # Typed shape with extra authority, wrong values, or a shell chain.
    {**SCALE.model_dump(), "approved": True},
    {**SCALE.model_dump(), "arguments": {"replicas": -1}},
    "kubectl scale deployment/checkout-api --replicas=3 -n shop && kubectl delete ns shop",
    "kubectl scale deployment/checkout-api --replicas=three -n shop",
    "kubectl patch deployment checkout-api -n shop -p 'not json'",
    "kubectl scale deployment/checkout-api --replicas='3",
    "{not json",
    42,
    None,
])
def test_unrecognized_forms_are_rejected(raw):
    result = canonicalize(raw)
    assert isinstance(result, Rejection) and (result.status, result.reason) == ("BLOCK", "UNRECOGNIZED_FORM")
    assert result.detail
