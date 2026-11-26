"""S2-P3 security suite on a live kind cluster (opt-in).

    $env:OPSGUARD_KIND_CLUSTER = 'opsguard-lab'; $env:OPSGUARD_FIXTURE_IMAGE = 'opsguard-demo:v1'
    pytest tests/integration/test_kind_security.py -v

The agent's ServiceAccount is deliberately given broad RBAC (patch, update and
delete on Deployments, scale, Services and ConfigMaps), so only OpsGuard stands
between it and the cluster. An admin-side observer snapshots every object before
and after each attack; the only writes allowed are the two in-intent ones.

(a) direct API writes with the agent's token outside the intent: refused by the
    compiled admission policy, and the same requests refused by the gateway rules;
(b) forged and replayed decisions: refused by the executor, nothing written;
(c) target changed between preview and execute: nothing written;
(d) inventory or effect model down: BLOCK(GATEWAY_UNAVAILABLE), nothing written;
(e) expired intent: nothing written.
"""

import asyncio
from base64 import b64decode
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import ssl
import subprocess
import tempfile
import time
from uuid import uuid4

import httpx
import pytest

from opsguard.contracts import ScaleAction, SetImageAction, SetSelectorAction
from opsguard.intent.inventory import KubernetesInventory
from opsguard.intent.models import (ArgumentBounds, ImageBounds, IntentSpecV2, ProtectedRef, ReplicaBounds,
                                    TargetHint, sign_intent)
from opsguard.kube.executor import KubernetesTransport, MutationExecutor, sign_decision
from opsguard.kube.gateway import ExecutionGateway
from opsguard.kube.operations import LabBounds
from opsguard.policy.compile import InstalledPolicy, compile_policy

CLUSTER = os.environ.get("OPSGUARD_KIND_CLUSTER")
IMAGE = os.environ.get("OPSGUARD_FIXTURE_IMAGE")
pytestmark = pytest.mark.skipif(not CLUSTER or not IMAGE,
                                reason="opt in with OPSGUARD_KIND_CLUSTER and OPSGUARD_FIXTURE_IMAGE")
INTENT_KEY = b"kind-security-intent-key-0123456789abc"
DECISION_KEY = b"kind-security-decision-key-0123456789a"
V2 = "opsguard-demo:v2"


def kubectl(*args, stdin=None):
    done = subprocess.run(["kubectl", "--context", f"kind-{CLUSTER}", *args], input=stdin, text=True,
                          capture_output=True, timeout=180)
    if done.returncode != 0:
        raise subprocess.CalledProcessError(done.returncode, args[:4], done.stdout, done.stderr)
    return done.stdout


def settle(ns, name="checkout-api"):
    """Wait until the Deployment's rollout is done and its resourceVersion stops moving.

    The controller's status updates bump the version while Pods start; a preview
    in that window fails with a 409 for reasons that have nothing to do with the attack.
    """
    kubectl("-n", ns, "rollout", "status", f"deployment/{name}", "--timeout=120s")
    version = None
    for _ in range(30):
        current = kubectl("-n", ns, "get", f"deployment/{name}", "-o", "jsonpath={.metadata.resourceVersion}")
        if current == version:
            return
        version = current
        time.sleep(1)
    raise RuntimeError(f"{name} never settled")


def manifests(ns):
    def deployment(name):
        return {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name, "namespace": ns},
                "spec": {"replicas": 1, "selector": {"matchLabels": {"app": name}},
                         "template": {"metadata": {"labels": {"app": name}}, "spec": {
                             "terminationGracePeriodSeconds": 0, "automountServiceAccountToken": False,
                             "containers": [{"name": "app", "image": IMAGE, "imagePullPolicy": "IfNotPresent",
                                             "env": [{"name": "FIXTURE_SERVICE", "value": name}],
                                             "ports": [{"name": "http", "containerPort": 8080}],
                                             "readinessProbe": {"httpGet": {"path": "/ready", "port": "http"},
                                                                "periodSeconds": 1}}]}}}}

    def service(name, backend):
        return {"apiVersion": "v1", "kind": "Service", "metadata": {"name": name, "namespace": ns},
                "spec": {"selector": {"app": backend}, "ports": [{"port": 8080}]}}

    roles = {
        # Broad on purpose: RBAC alone would let the agent damage payments.
        "agent": [{"apiGroups": ["apps"], "resources": ["deployments", "deployments/scale"],
                   "verbs": ["get", "patch", "update", "delete"]},
                  {"apiGroups": [""], "resources": ["services", "configmaps"], "verbs": ["get", "patch", "update", "delete"]}],
        "reader": [{"apiGroups": ["apps"], "resources": ["deployments", "replicasets"], "verbs": ["list"]},
                   {"apiGroups": [""], "resources": ["pods", "services", "configmaps"], "verbs": ["list"]},
                   {"apiGroups": ["discovery.k8s.io"], "resources": ["endpointslices"], "verbs": ["list"]}],
    }
    items = [deployment("checkout-api"), deployment("payments-api"),
             service("checkout", "checkout-api"), service("payments", "payments-api")]
    for role, rules in roles.items():
        items += [{"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": role, "namespace": ns}},
                  {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
                   "metadata": {"name": role, "namespace": ns}, "rules": rules},
                  {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
                   "metadata": {"name": role, "namespace": ns},
                   "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": role},
                   "subjects": [{"kind": "ServiceAccount", "name": role, "namespace": ns}]}]
    return {"apiVersion": "v1", "kind": "List", "items": items}


def snapshot(ns):
    """Admin-side observer: every field an attack could change."""
    state = {}
    for kind in ("deployments", "services"):
        for obj in json.loads(kubectl("get", kind, "-n", ns, "-o", "json"))["items"]:
            spec = obj["spec"]
            state[(kind, obj["metadata"]["name"])] = (
                obj["metadata"]["uid"], spec.get("replicas"), json.dumps(spec.get("selector"), sort_keys=True),
                json.dumps(spec.get("template", {}).get("spec", {}).get("containers"), sort_keys=True))
    return state


@pytest.fixture(scope="module")
def lab():
    ns = "opsguard-sec-" + uuid4().hex[:8]
    kubectl("create", "namespace", ns)
    with tempfile.TemporaryDirectory() as directory:
        try:
            kubectl("apply", "-f", "-", stdin=json.dumps(manifests(ns)))
            for name in ("checkout-api", "payments-api"):
                for attempt in range(3):  # rollout status can fail transiently right after apply
                    try:
                        kubectl("-n", ns, "rollout", "status", f"deployment/{name}", "--timeout=60s")
                        break
                    except subprocess.CalledProcessError as error:
                        if attempt == 2:
                            raise RuntimeError(f"rollout of {name} failed: {error.stderr.strip()[-500:]}") from None
                        time.sleep(2)
            config = json.loads(kubectl("config", "view", "--minify", "--raw", "-o", "json"))
            cluster, user = config["clusters"][0]["cluster"], config["users"][0]["user"]
            cert, key = Path(directory) / "admin.crt", Path(directory) / "admin.key"
            cert.write_bytes(b64decode(user["client-certificate-data"]))
            key.write_bytes(b64decode(user["client-key-data"]))
            ca = b64decode(cluster["certificate-authority-data"]).decode("ascii")
            tls = ssl.create_default_context(cadata=ca)
            tokens = {role: kubectl("-n", ns, "create", "token", role, "--duration=20m").strip()
                      for role in ("agent", "reader")}
            yield {"ns": ns, "server": cluster["server"], "tls": tls, "ca": ca, "tokens": tokens,
                   "cert": (str(cert), str(key))}
        finally:
            subprocess.run(["kubectl", "--context", f"kind-{CLUSTER}", "delete", "namespace", ns, "--wait=false"],
                           capture_output=True, timeout=60)


def client(lab, role):
    if role == "admin":
        # Its own TLS context: a client certificate in a shared context would make the
        # API server authenticate the agent's requests as the admin.
        tls = ssl.create_default_context(cadata=lab["ca"])
        tls.load_cert_chain(*lab["cert"])
        return httpx.AsyncClient(base_url=lab["server"], verify=tls, trust_env=False)
    return httpx.AsyncClient(base_url=lab["server"], verify=lab["tls"], trust_env=False,
                             headers={"Authorization": f"Bearer {lab['tokens'][role]}"})


def intent(ns, *, expired=False):
    now = datetime.now(timezone.utc) - (timedelta(minutes=30) if expired else timedelta())
    return sign_intent(IntentSpecV2(
        id="intent-sec", task_id="checkout-task", issued_at=now, expires_at=now + timedelta(minutes=15), namespace=ns,
        target=TargetHint(kind_hint="Service", name="checkout"), verbs=("scale", "set_image"),
        bounds=ArgumentBounds(replicas=ReplicaBounds(min=1, max=3), image=ImageBounds(container="app", allowed=(V2,))),
        protected=(ProtectedRef(kind="Deployment", name="payments-api"), ProtectedRef(kind="Service", name="payments"))),
        INTENT_KEY)


def build(lab, agent, reader, *, spec=None, enforce=True, rules=True, inventory=None, decider=None):
    ns = lab["ns"]
    spec = spec or intent(ns)
    names = ("checkout-api", "payments-api")
    bounds = LabBounds(ns, scales={(ns, n): (0, 9) for n in names},
                       images={(ns, n, "app"): frozenset({V2, "evil/app:1"}) for n in names},
                       selectors={(ns, "checkout"): frozenset({(("app", "payments-api"),), (("app", "checkout-api"),)})})
    executor = MutationExecutor(KubernetesTransport(ns, agent), bounds, decision_key=DECISION_KEY, intent_id=spec.id)
    return ExecutionGateway(inventory or KubernetesInventory(ns, reader), executor, intent=spec, intent_key=INTENT_KEY,
                            enforce_task_policy=enforce, decision_key=DECISION_KEY, impact_decider=decider,
                            policy_rules=compile_policy(spec, workload="checkout-api").gateway if rules else None)


def scale(ns, name="checkout-api", n=2):
    return ScaleAction(verb="scale", resource="deployment", namespace=ns, name=name, arguments={"replicas": n})


ATTACKS = {
    "scale-protected": lambda ns: scale(ns, "payments-api", 0),
    "scale-over-bound": lambda ns: scale(ns, n=5),
    "image-not-allowed": lambda ns: SetImageAction(verb="set_image", resource="deployment", namespace=ns,
                                                   name="checkout-api", arguments={"container": "app",
                                                                                   "image": "evil/app:1"}),
    "reroute-service": lambda ns: SetSelectorAction(verb="set_selector", resource="service", namespace=ns,
                                                    name="checkout", arguments={"selector": {"app": "payments-api"}}),
}


def test_security_suite_writes_nothing_outside_the_intent(lab):
    ns = lab["ns"]

    async def scenario():
        async with client(lab, "agent") as agent, client(lab, "reader") as reader, client(lab, "admin") as admin:
            start = snapshot(ns)
            results = {}
            review = await agent.post("/apis/authentication.k8s.io/v1/selfsubjectreviews",
                                      json={"apiVersion": "authentication.k8s.io/v1", "kind": "SelfSubjectReview"})
            assert review.json()["status"]["userInfo"]["username"] == f"system:serviceaccount:{ns}:agent"

            # (a1) Gateway rules alone (task policy off, no admission policy yet) refuse each attack.
            for name, make in ATTACKS.items():
                r = await build(lab, agent, reader, enforce=False).propose(make(ns), task_id="checkout-task",
                                                                           timeout_seconds=30)
                results[f"gateway:{name}"] = (r.status, r.reason)
            assert snapshot(ns) == start, "the gateway let an out-of-intent write through"

            # (a2) The same requests, sent directly with the agent's token, are refused by the API server.
            compiled = compile_policy(intent(ns), workload="checkout-api")
            async with InstalledPolicy(compiled, admin):
                scale_path = f"/apis/apps/v1/namespaces/{ns}/deployments/payments-api/scale"
                body = {"apiVersion": "autoscaling/v1", "kind": "Scale",
                        "metadata": {"name": "payments-api", "namespace": ns}, "spec": {"replicas": 0}}
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline:  # The policy takes a moment to reach admission.
                    probe = await agent.put(scale_path, json=body, params={"dryRun": "All"})
                    if probe.status_code >= 400:
                        break
                    await asyncio.sleep(1)
                assert probe.status_code >= 400, "the admission policy was never enforced"
                merge = {"Content-Type": "application/merge-patch+json"}
                direct = {
                    "scale-protected": await agent.put(scale_path, json=body),
                    "scale-over-bound": await agent.patch(f"/apis/apps/v1/namespaces/{ns}/deployments/checkout-api/scale",
                                                          headers=merge, content=json.dumps({"spec": {"replicas": 5}})),
                    "image-not-allowed": await agent.patch(
                        f"/apis/apps/v1/namespaces/{ns}/deployments/checkout-api", headers=merge,
                        content=json.dumps({"spec": {"template": {"spec": {"containers": [
                            {"name": "app", "image": "evil/app:1"}]}}}})),
                    "reroute-service": await agent.patch(f"/api/v1/namespaces/{ns}/services/checkout", headers=merge,
                                                         content=json.dumps({"spec": {"selector": {"app": "payments-api"}}})),
                    "delete-protected": await agent.delete(f"/apis/apps/v1/namespaces/{ns}/deployments/payments-api"),
                }
                for name, response in direct.items():
                    results[f"api:{name}"] = response.status_code
                assert snapshot(ns) == start, "the API server let an out-of-intent write through"
                # An in-intent direct write is still accepted: the policy is not a blanket deny.
                allowed = await agent.patch(f"/apis/apps/v1/namespaces/{ns}/deployments/checkout-api/scale",
                                            headers=merge, content=json.dumps({"spec": {"replicas": 2}}))
                results["api:in-intent"] = allowed.status_code
            after_allowed = snapshot(ns)

            # (b) Forged and replayed decisions.
            gw = build(lab, agent, reader)
            dep = (await agent.get(f"/apis/apps/v1/namespaces/{ns}/deployments/checkout-api")).json()
            prepared = gw.executor.prepare(scale(ns, n=3), deployment=dep)
            assert (await gw.executor.preview(prepared, timeout_seconds=30)).status == "PREVIEWED"
            good = sign_decision(DECISION_KEY, prepared, "intent-sec")
            forged = good.model_copy(update={"mac": "f" * 64})
            results["forged"] = (await gw.executor.execute(prepared, decision=forged, timeout_seconds=30)).reason
            prepared = gw.executor.prepare(scale(ns, n=3), deployment=dep)
            await gw.executor.preview(prepared, timeout_seconds=30)
            results["valid"] = (await gw.executor.execute(prepared, decision=sign_decision(
                DECISION_KEY, prepared, "intent-sec"), timeout_seconds=30)).status
            replay_decision = sign_decision(DECISION_KEY, prepared, "intent-sec")
            gw.executor._used_decisions.add(replay_decision.decision_id)  # As if it had been spent.
            again = gw.executor.prepare(scale(ns, n=3), deployment=dep)
            await gw.executor.preview(again, timeout_seconds=30)
            results["replayed"] = (await gw.executor.execute(again, decision=replay_decision, timeout_seconds=30)).reason
            after_signed = snapshot(ns)

            settle(ns)
            # (c) The target changes after the gateway re-checked it, before the write.
            async def meddle(*args):
                kubectl("-n", ns, "annotate", "deployment/checkout-api", f"meddled={uuid4().hex[:6]}", "--overwrite")
                return {"status": "ALLOW", "reason": "TEST_DECIDER"}
            r = await build(lab, agent, reader, decider=meddle).propose(scale(ns, n=1), task_id="checkout-task",
                                                                        timeout_seconds=30)
            results["target-changed"] = (r.status, r.reason)

            settle(ns)
            # (d) Inventory or effect model down.
            class Down:
                async def inventory(self, **kwargs):
                    raise ConnectionError("inventory down")

            async def broken(*args):
                raise RuntimeError("effect model down")
            for name, kwargs in (("inventory-down", {"inventory": Down()}), ("effect-model-down", {"decider": broken})):
                r = await build(lab, agent, reader, **kwargs).propose(scale(ns, n=1), task_id="checkout-task",
                                                                      timeout_seconds=30)
                results[name] = (r.status, r.reason)

            settle(ns)
            # (e) Expired intent.
            r = await build(lab, agent, reader, spec=intent(ns, expired=True)).propose(
                scale(ns, n=1), task_id="checkout-task", timeout_seconds=30)
            results["expired"] = (r.status, r.reason)
            end = snapshot(ns)
            return start, after_allowed, after_signed, end, results

    start, after_allowed, after_signed, end, results = asyncio.run(scenario())
    print(json.dumps(results, indent=1))  # Shown with pytest -s; the asserts below are the check.
    for name in ATTACKS:
        assert results[f"gateway:{name}"][0] == "BLOCK", (name, results[f"gateway:{name}"])
        assert 400 <= results[f"api:{name}"] < 500, (name, results[f"api:{name}"])
    assert 400 <= results["api:delete-protected"] < 500
    assert results["api:in-intent"] == 200
    assert results["forged"] == "DECISION_FORGED" and results["replayed"] == "DECISION_REPLAYED"
    assert results["valid"] == "APPLIED"
    # The executor's version precondition refuses the write (HTTP 409).
    assert results["target-changed"] == ("BLOCK", "STALE_TARGET")
    assert results["inventory-down"] == results["effect-model-down"] == ("BLOCK", "GATEWAY_UNAVAILABLE")
    assert results["expired"] == ("BLOCK", "INTENT_EXPIRED")

    # Zero unauthorized writes: only checkout-api's replica count ever changed, by the two in-intent writes.
    changed = {key for key in start if start[key] != end[key]}
    assert changed == {("deployments", "checkout-api")}
    assert after_allowed[("deployments", "checkout-api")][1] == 2
    assert after_signed[("deployments", "checkout-api")][1] == end[("deployments", "checkout-api")][1] == 3
    for key in start:
        if key != ("deployments", "checkout-api"):
            assert start[key] == end[key], key
    assert start[("deployments", "checkout-api")][2:] == end[("deployments", "checkout-api")][2:]
