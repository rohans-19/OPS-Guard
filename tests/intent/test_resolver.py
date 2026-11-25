"""The Stage 2 target-resolution algorithm, one test per situation in the guide's table."""

from dataclasses import replace

import pytest

from opsguard.intent.inventory import InventorySnapshot
from opsguard.intent.resolver import DeploymentIdentity, check_binding, resolve

NS = "shop"


def deployment(name, *, labels=None, namespace=NS, uid=None, replicas=1, version="1"):
    return {"metadata": {"namespace": namespace, "name": name, "uid": uid or f"{namespace}-{name}-uid",
                         "resourceVersion": version},
            "spec": {"replicas": replicas, "template": {"metadata": {"labels": labels or {"app": name}},
                                                        "spec": {"containers": [{"name": "app", "image": "app:v1"}]}}}}


def statefulset(name, *, labels=None, namespace=NS):
    obj = deployment(name, labels=labels, namespace=namespace, uid=f"{namespace}-{name}-sts-uid")
    return obj


def replicaset(dep):
    meta = dep["metadata"]
    return {"metadata": {"namespace": meta["namespace"], "name": meta["name"] + "-rs", "uid": meta["uid"] + "-rs",
                         "ownerReferences": [{"kind": "Deployment", "name": meta["name"], "uid": meta["uid"],
                                              "controller": True}]},
            "spec": {"template": dep["spec"]["template"]}}


def pod(owner, owner_kind, *, suffix="0", labels=None):
    meta = owner["metadata"]
    labels = labels or owner["spec"]["template"]["metadata"]["labels"]
    return {"metadata": {"namespace": meta["namespace"], "name": f"{meta['name']}-{suffix}",
                         "uid": f"{meta['uid']}-pod-{suffix}", "labels": dict(labels),
                         "ownerReferences": [{"kind": owner_kind, "name": meta["name"], "uid": meta["uid"],
                                              "controller": True}]},
            "status": {"phase": "Running"}}


def service(name, selector, *, namespace=NS):
    return {"metadata": {"namespace": namespace, "name": name, "uid": f"{namespace}-{name}-svc", "resourceVersion": "1"},
            "spec": {"selector": selector}}


def endpoints(svc, pods, *, ready=True):
    meta = svc["metadata"]
    return {"metadata": {"namespace": meta["namespace"], "name": meta["name"] + "-slice",
                         "labels": {"kubernetes.io/service-name": meta["name"]},
                         "ownerReferences": [{"kind": "Service", "name": meta["name"], "uid": meta["uid"],
                                              "controller": True}]},
            "endpoints": [{"conditions": {"ready": ready},
                           "targetRef": {"kind": "Pod", "namespace": p["metadata"]["namespace"],
                                         "name": p["metadata"]["name"], "uid": p["metadata"]["uid"]}} for p in pods]}


def snapshot(*, deployments=(), statefulsets=(), services=(), pods=(), slices=(), replicasets=None):
    return InventorySnapshot(
        namespace=NS, deployments=tuple(deployments), statefulsets=tuple(statefulsets), services=tuple(services),
        pods=tuple(pods), endpointslices=tuple(slices),
        replicasets=tuple(replicasets if replicasets is not None else [replicaset(d) for d in deployments]))


def one_service_one_deployment():
    dep = deployment("payment")
    p = pod(replicaset(dep), "ReplicaSet")
    svc = service("payment", {"app": "payment"})
    return snapshot(deployments=[dep], services=[svc], pods=[p], slices=[endpoints(svc, [p])])


def test_service_with_pods_owned_by_one_deployment_binds_that_deployment():
    result = resolve(NS, "Service", "payment", one_service_one_deployment())
    assert (result.status, result.target_kind, result.binding) == ("PASS", "Deployment", "endpoint_owner_chain")
    assert result.target.name == "payment" and result.target.uid == "shop-payment-uid"


def test_service_whose_endpoints_come_from_two_deployments_escalates():
    deps = [deployment("payment-a", labels={"app": "payment", "v": "a"}),
            deployment("payment-b", labels={"app": "payment", "v": "b"})]
    pods = [pod(replicaset(d), "ReplicaSet") for d in deps]
    svc = service("payment", {"app": "payment"})
    result = resolve(NS, "Service", "payment", snapshot(deployments=deps, services=[svc], pods=pods,
                                                        slices=[endpoints(svc, pods)]))
    assert (result.status, result.reason, result.target) == ("ESCALATE", "TARGET_AMBIGUOUS", None)


def test_deployment_and_statefulset_with_the_same_bare_name_escalate():
    state = snapshot(deployments=[deployment("payment")], statefulsets=[statefulset("payment")])
    assert (resolve(NS, None, "payment", state).status, resolve(NS, None, "payment", state).reason) == (
        "ESCALATE", "TARGET_AMBIGUOUS")
    # A kind hint removes the ambiguity.
    assert resolve(NS, "StatefulSet", "payment", state).target_kind == "StatefulSet"
    assert resolve(NS, "Deployment", "payment", state).target_kind == "Deployment"


def test_same_name_in_two_namespaces_binds_the_one_in_the_intent_namespace():
    state = snapshot(deployments=[deployment("payment", namespace="other"), deployment("payment")])
    result = resolve(NS, "Deployment", "payment", state)
    assert result.status == "PASS" and result.target.namespace == NS and result.target.uid == "shop-payment-uid"


@pytest.mark.parametrize("hint", ["Service", "Deployment", "StatefulSet", None])
def test_no_object_with_the_name_blocks(hint):
    result = resolve(NS, hint, "payment", snapshot(deployments=[deployment("checkout")]))
    assert (result.status, result.reason) == ("BLOCK", "TARGET_MISSING")


def test_deployment_recreated_mid_task_blocks():
    state = one_service_one_deployment()
    bound = resolve(NS, "Deployment", "payment", state).target
    recreated = deployment("payment", uid="new-uid")
    later = replace(state, deployments=(recreated,), replicasets=(replicaset(recreated),))
    assert resolve(NS, "Deployment", "payment", later, uid=bound.uid).reason == "TARGET_REPLACED"
    assert check_binding(bound, resolve(NS, "Deployment", "payment", later).target) == "TARGET_REPLACED"


def test_binding_check_distinguishes_version_change_and_missing_target():
    bound = DeploymentIdentity(namespace=NS, name="payment", uid="u", resource_version="1")
    assert check_binding(bound, bound) == "BOUND"
    assert check_binding(bound, bound.model_copy(update={"resource_version": "2"})) == "TARGET_VERSION_CHANGED"
    assert check_binding(bound, None) == "TARGET_MISSING"


def test_no_ready_endpoints_falls_back_to_pods_matching_the_selector():
    dep = deployment("payment")
    p = pod(replicaset(dep), "ReplicaSet")
    svc = service("payment", {"app": "payment"})
    state = snapshot(deployments=[dep], services=[svc], pods=[p], slices=[endpoints(svc, [p], ready=False)])
    result = resolve(NS, "Service", "payment", state)
    assert (result.status, result.binding, result.target.name) == ("PASS", "selector_fallback", "payment")
    # v1 intents that did not opt in keep their old behaviour.
    assert resolve(NS, "Service", "payment", state, selector_fallback=False).reason == "NO_READY_ENDPOINTS"


def test_workload_scaled_to_zero_resolves_through_its_pod_template():
    dep = deployment("payment", replicas=0)
    svc = service("payment", {"app": "payment"})
    result = resolve(NS, "Service", "payment", snapshot(deployments=[dep], services=[svc]))
    assert (result.status, result.binding) == ("PASS", "selector_fallback")


def test_empty_slice_with_null_endpoints_uses_the_fallback():
    """Found on kind: a Service with no Pods has a slice whose endpoints field is null."""
    dep = deployment("payment", replicas=0)
    svc = service("payment", {"app": "payment"})
    empty = endpoints(svc, [])
    empty["endpoints"] = None
    result = resolve(NS, "Service", "payment", snapshot(deployments=[dep], services=[svc], slices=[empty]))
    assert (result.status, result.binding) == ("PASS", "selector_fallback")


def test_fallback_that_matches_two_workloads_escalates_and_none_blocks():
    deps = [deployment("payment-a", labels={"app": "payment"}), deployment("payment-b", labels={"app": "payment"})]
    svc = service("payment", {"app": "payment"})
    assert resolve(NS, "Service", "payment", snapshot(deployments=deps, services=[svc])).reason == "TARGET_AMBIGUOUS"
    orphan = service("payment", {"app": "nothing"})
    assert resolve(NS, "Service", "payment", snapshot(deployments=deps, services=[orphan])).reason == "TARGET_MISSING"


def test_service_backed_by_a_statefulset_binds_the_statefulset():
    sts = statefulset("ledger")
    p = pod(sts, "StatefulSet")
    svc = service("ledger", {"app": "ledger"})
    state = snapshot(statefulsets=[sts], services=[svc], pods=[p], slices=[endpoints(svc, [p])])
    result = resolve(NS, "Service", "ledger", state)
    assert (result.status, result.target_kind, result.target.name) == ("PASS", "StatefulSet", "ledger")


@pytest.mark.parametrize("hint", ["StatefulSet", None])
def test_unread_statefulsets_fail_closed_when_the_answer_depends_on_them(hint):
    state = replace(snapshot(deployments=[deployment("payment")]), statefulsets=None)
    assert resolve(NS, hint, "payment", state).reason == "INVENTORY_INCOMPLETE"
    assert resolve(NS, "Deployment", "payment", state).status == "PASS"


def test_a_pod_is_never_a_target_and_unverified_owners_escalate():
    dep = deployment("payment")
    p = pod(replicaset(dep), "ReplicaSet")
    p["metadata"]["ownerReferences"] = []  # A bare Pod has no controller root.
    svc = service("payment", {"app": "payment"})
    state = snapshot(deployments=[dep], services=[svc], pods=[p], slices=[endpoints(svc, [p])])
    assert resolve(NS, "Service", "payment", state).reason == "INVENTORY_UNVERIFIED"


def test_bare_name_that_only_names_a_service_binds_the_service():
    svc = service("payment", {"app": "payment"})
    result = resolve(NS, None, "payment", snapshot(services=[svc]))
    assert (result.status, result.target_kind) == ("PASS", "Service")
