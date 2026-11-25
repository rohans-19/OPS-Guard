"""Resolver situations on a live kind cluster (opt-in).

    $env:OPSGUARD_KIND_CLUSTER = 'opsguard-lab'
    $env:OPSGUARD_FIXTURE_IMAGE = 'python:3.13-slim'
    .\\.venv\\Scripts\\python.exe -m pytest tests/integration/test_kind_resolver.py -v

The fixture image must already be loaded into the cluster. Two disposable
namespaces are created and deleted.
"""

import json
import os
import subprocess
from uuid import uuid4

import pytest

from opsguard.intent.inventory import InventorySnapshot
from opsguard.intent.resolver import check_binding, resolve

CLUSTER = os.environ.get("OPSGUARD_KIND_CLUSTER")
IMAGE = os.environ.get("OPSGUARD_FIXTURE_IMAGE")
pytestmark = pytest.mark.skipif(not CLUSTER or not IMAGE,
                                reason="opt in with OPSGUARD_KIND_CLUSTER and OPSGUARD_FIXTURE_IMAGE")


def kubectl(*args, stdin=None):
    return subprocess.run(["kubectl", "--context", f"kind-{CLUSTER}", *args], input=stdin, text=True,
                          capture_output=True, check=True, timeout=180).stdout


def workload(kind, name, namespace, labels, replicas=1):
    spec = {"replicas": replicas, "selector": {"matchLabels": labels},
            "template": {"metadata": {"labels": labels}, "spec": {
                "terminationGracePeriodSeconds": 0,
                "containers": [{"name": "app", "image": IMAGE, "imagePullPolicy": "IfNotPresent",
                                "command": ["python", "-c", "import time; time.sleep(10**9)"]}]}}}
    if kind == "StatefulSet":
        spec["serviceName"] = name + "-hl"
    return {"apiVersion": "apps/v1", "kind": kind, "metadata": {"name": name, "namespace": namespace}, "spec": spec}


def service(name, namespace, selector, headless=False):
    spec = {"selector": selector, "ports": [{"port": 80}]}
    if headless:
        spec["clusterIP"] = "None"
    return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": namespace}, "spec": spec}


def apply(*objects):
    kubectl("apply", "-f", "-", stdin=json.dumps({"apiVersion": "v1", "kind": "List", "items": list(objects)}))


def wait(namespace, *refs):
    for ref in refs:
        kubectl("rollout", "status", ref, "-n", namespace, "--timeout=150s")


def read(namespace):
    def items(kind):
        return tuple(json.loads(kubectl("get", kind, "-n", namespace, "-o", "json"))["items"])
    return InventorySnapshot(namespace=namespace, deployments=items("deployments"), services=items("services"),
                             pods=items("pods"), replicasets=items("replicasets"),
                             endpointslices=items("endpointslices.discovery.k8s.io"),
                             statefulsets=items("statefulsets"))


@pytest.fixture(scope="module")
def lab():
    ns, other = f"resolver-{uuid4().hex[:6]}", f"resolver-other-{uuid4().hex[:6]}"
    for name in (ns, other):
        kubectl("create", "namespace", name)
    try:
        apply(
            workload("Deployment", "payment", ns, {"app": "payment"}), service("payment", ns, {"app": "payment"}),
            workload("Deployment", "split-a", ns, {"app": "split", "v": "a"}),
            workload("Deployment", "split-b", ns, {"app": "split", "v": "b"}), service("split", ns, {"app": "split"}),
            workload("Deployment", "twin", ns, {"app": "twin-d"}),
            workload("StatefulSet", "twin", ns, {"app": "twin-s"}), service("twin-hl", ns, {"app": "twin-s"}, True),
            workload("Deployment", "idle", ns, {"app": "idle"}, replicas=0), service("idle", ns, {"app": "idle"}),
            workload("Deployment", "payment", other, {"app": "payment"}),
        )
        wait(ns, "deployment/payment", "deployment/split-a", "deployment/split-b", "deployment/twin",
             "statefulset/twin")
        wait(other, "deployment/payment")
        yield ns
    finally:
        for name in (ns, other):
            subprocess.run(["kubectl", "--context", f"kind-{CLUSTER}", "delete", "namespace", name, "--wait=false"],
                           capture_output=True, timeout=60)


def test_resolver_situations_on_kind(lab):
    state = read(lab)
    bound = resolve(lab, "Service", "payment", state)
    assert (bound.status, bound.target_kind, bound.target.name, bound.binding) == (
        "PASS", "Deployment", "payment", "endpoint_owner_chain")
    assert bound.target.namespace == lab  # Not the same-named Deployment in the other namespace.
    assert resolve(lab, "Service", "split", state).reason == "TARGET_AMBIGUOUS"
    assert resolve(lab, None, "twin", state).reason == "TARGET_AMBIGUOUS"
    assert resolve(lab, "StatefulSet", "twin", state).target_kind == "StatefulSet"
    assert resolve(lab, None, "absent", state).reason == "TARGET_MISSING"
    idle = resolve(lab, "Service", "idle", state)
    assert (idle.status, idle.binding, idle.target.name) == ("PASS", "selector_fallback", "idle")


def test_recreated_deployment_is_detected_on_kind(lab):
    before = resolve(lab, "Deployment", "payment", read(lab)).target
    manifest = workload("Deployment", "payment", lab, {"app": "payment"})
    kubectl("delete", "deployment", "payment", "-n", lab, "--wait=true")
    apply(manifest)
    wait(lab, "deployment/payment")
    after = read(lab)
    assert resolve(lab, "Deployment", "payment", after, uid=before.uid).reason == "TARGET_REPLACED"
    assert check_binding(before, resolve(lab, "Deployment", "payment", after).target) == "TARGET_REPLACED"
