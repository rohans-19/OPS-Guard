"""Compiler: each rule type produces the expected CEL, and one source drives both enforcement points."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from opsguard.intent.models import (ArgumentBounds, ConfigBounds, ImageBounds, IntentSpecV2, ProtectedRef,
                                    ReplicaBounds, SelectorBounds, TargetHint)
from opsguard.policy.cel import parse
from opsguard.policy.compile import RESOURCE_GUARDS, InstalledPolicy, build_rules, compile_policy

NOW = datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)


def spec(**changes):
    fields = dict(id="intent-0007", task_id="t", issued_at=NOW, expires_at=NOW + timedelta(minutes=15),
                  namespace="shop", target=TargetHint(kind_hint="Service", name="checkout"),
                  verbs=("scale", "set_image"),
                  bounds=ArgumentBounds(replicas=ReplicaBounds(min=2, max=4),
                                        image=ImageBounds(container="app", allowed=("shop/app:v2",))),
                  protected=(ProtectedRef(kind="Deployment", name="payments-api"),
                             ProtectedRef(kind="Service", name="payments")))
    fields.update(changes)
    return IntentSpecV2(**fields)


def rules(intent=None, workload="checkout-api"):
    return {rule.id: rule for rule in build_rules(intent or spec(), workload=workload)}


def test_each_rule_type_produces_the_expected_cel():
    r = rules()
    assert r["updates-only"].check == "request.operation == 'UPDATE'"
    assert r["target-deployment"].check == "object.metadata.name in ['checkout-api']"
    assert r["target-service"].check == "object.metadata.name in []"
    assert r["protected-deployment"].check == "!(object.metadata.name in ['payments-api'])"
    assert r["protected-service"].check == "!(object.metadata.name in ['payments'])"
    assert r["replicas"].check == "object.spec.replicas >= 2 && object.spec.replicas <= 4"
    assert "(c.name == 'app' && c.image in ['shop/app:v2'])" in r["images"].check
    assert "kubectl.kubernetes.io/restartedAt" in r["restart"].check  # restart not allowed
    assert r["selector"].check == ("(has(object.spec.selector) ? object.spec.selector : {}) == "
                                   "(has(oldObject.spec.selector) ? oldObject.spec.selector : {})")
    assert r["configmap-data"].check == "(has(object.data) ? object.data : {}) == (has(oldObject.data) ? oldObject.data : {})"
    assert r["replicas"].expression.startswith(f"!(({RESOURCE_GUARDS['deployments']}) || ")


def test_selector_config_and_restart_variants():
    selector = rules(spec(verbs=("set_selector",), bounds=ArgumentBounds(
        selector=SelectorBounds(selector={"app": "checkout-api"}, backend="checkout-api"))), workload=None)
    assert "== {'app': 'checkout-api'} ||" in selector["selector"].check
    assert selector["target-service"].check == "object.metadata.name in ['checkout']"
    assert selector["replicas"].check == "object.spec.replicas == oldObject.spec.replicas"
    config = rules(spec(target=TargetHint(kind_hint="ConfigMap", name="app-settings"), verbs=("set_config",),
                        bounds=ArgumentBounds(config=ConfigBounds(key="mode", values=("on", "it's"),
                                                                  diagnostic_deployment="checkout-api"))), workload=None)
    assert "k == 'mode' ? object.data[k] in ['it\\'s', 'on']" in config["configmap-data"].check
    restart = rules(spec(verbs=("restart",), bounds=ArgumentBounds()))
    assert "restart" not in restart and "images" in restart


def test_workload_verbs_need_a_resolved_deployment():
    with pytest.raises(ValueError):
        build_rules(spec(), workload=None)
    named = spec(target=TargetHint(kind_hint="Deployment", name="checkout-api"))
    assert rules(named, workload=None)["target-deployment"].check == "object.metadata.name in ['checkout-api']"


def test_every_compiled_rule_is_inside_the_subset():
    for rule in build_rules(spec(), workload="checkout-api"):
        parse(rule.expression)


def test_one_policy_per_resource_targets_only_the_agent_in_the_intent_namespace():
    compiled = compile_policy(spec(), workload="checkout-api")
    expected = {"deployments": {"updates-only", "target-deployment", "protected-deployment", "replicas", "images",
                                "restart"},
                "deployments/scale": {"updates-only", "target-deployment", "protected-deployment", "replicas"},
                "services": {"updates-only", "target-service", "protected-service", "selector"},
                "configmaps": {"updates-only", "target-configmap", "configmap-data"}}
    by_expression = {rule.expression: rule.id for rule in compiled.gateway.rules}
    assert len(compiled.policies) == len(compiled.bindings) == 4
    for policy, binding in zip(compiled.policies, compiled.bindings):
        spec_ = policy["spec"]
        (match,) = spec_["matchConstraints"]["resourceRules"]
        (resource,) = match["resources"]
        assert spec_["failurePolicy"] == "Fail" and set(match["operations"]) == {"CREATE", "UPDATE", "DELETE"}
        assert spec_["matchConditions"] == [{"name": "agent-only", "expression":
                                             "request.userInfo.username == 'system:serviceaccount:shop:agent'"}]
        # The same strings the gateway evaluates, and only this resource's rules.
        assert {by_expression[v["expression"]] for v in spec_["validations"]} == expected[resource]
        assert binding["spec"]["policyName"] == policy["metadata"]["name"]
        assert binding["spec"]["validationActions"] == ["Deny"]
        assert binding["spec"]["matchResources"] == {
            "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "shop"}}}
    assert '"kind": "List"' in compiled.vap_yaml and '"kind": "List"' in compiled.binding_yaml


def deployment(name="checkout-api", replicas=2, image="shop/app:v1", annotations=None):
    return {"metadata": {"name": name}, "spec": {"replicas": replicas, "template": {
        "metadata": {"annotations": annotations} if annotations else {},
        "spec": {"containers": [{"name": "app", "image": image}, {"name": "side", "image": "side:1"}]}}}}


def scale(name, replicas):
    return {"metadata": {"name": name}, "spec": {"replicas": replicas}}


@pytest.mark.parametrize("resource,sub,name,obj,old,violated", [
    ("deployments", "scale", "checkout-api", scale("checkout-api", 3), scale("checkout-api", 2), set()),
    ("deployments", "scale", "checkout-api", scale("checkout-api", 5), scale("checkout-api", 2), {"replicas"}),
    ("deployments", "scale", "payments-api", scale("payments-api", 2), scale("payments-api", 1),
     {"target-deployment", "protected-deployment"}),
    ("deployments", "", "checkout-api", deployment(image="shop/app:v2"), deployment(), set()),
    ("deployments", "", "checkout-api", deployment(image="evil:1"), deployment(), {"images"}),
    ("deployments", "", "checkout-api", deployment(annotations={"kubectl.kubernetes.io/restartedAt": "x"}),
     deployment(), {"restart"}),
    ("services", "", "checkout", {"metadata": {"name": "checkout"}, "spec": {"selector": {"app": "payments-api"}}},
     {"metadata": {"name": "checkout"}, "spec": {"selector": {"app": "checkout-api"}}},
     {"target-service", "selector"}),
    ("configmaps", "", "app-settings", {"metadata": {"name": "app-settings"}, "data": {"mode": "off"}},
     {"metadata": {"name": "app-settings"}, "data": {"mode": "on"}}, {"target-configmap", "configmap-data"}),
])
def test_gateway_rules_accept_in_intent_requests_and_name_each_violation(resource, sub, name, obj, old, violated):
    gateway = compile_policy(spec(), workload="checkout-api").gateway
    found = gateway.check(operation="UPDATE", resource=resource, sub_resource=sub, name=name, obj=obj, old=old)
    assert {v.rule for v in found} == violated


def test_deletes_creates_and_unevaluable_objects_are_violations():
    gateway = compile_policy(spec(), workload="checkout-api").gateway
    delete = gateway.check(operation="DELETE", resource="deployments", sub_resource="", name="checkout-api",
                           obj=None, old=deployment())
    assert "updates-only" in {v.rule for v in delete}
    broken = gateway.check(operation="UPDATE", resource="deployments", sub_resource="", name="checkout-api",
                           obj={"metadata": {"name": "checkout-api"}}, old=deployment())
    assert {"replicas", "images"} <= {v.rule for v in broken}
    assert any("evaluation error" in v.message for v in broken)


def test_installer_posts_policies_then_bindings_and_removes_bindings_first():
    calls = []

    def handler(request):
        calls.append((request.method, request.url.path.split("/")[-2] if request.method == "DELETE"
                      else request.url.path.rsplit("/", 1)[-1]))
        return httpx.Response(201 if request.method == "POST" else 200, json={})

    compiled = compile_policy(spec(expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
                                   issued_at=datetime.now(timezone.utc)), workload="checkout-api")

    async def run():
        async with httpx.AsyncClient(base_url="https://lab.invalid", transport=httpx.MockTransport(handler)) as admin:
            async with InstalledPolicy(compiled, admin) as installed:
                assert not installed.removed
            assert installed.removed

    asyncio.run(run())
    policies, bindings = "validatingadmissionpolicies", "validatingadmissionpolicybindings"
    assert calls == [("POST", policies)] * 4 + [("POST", bindings)] * 4 + [("DELETE", bindings)] * 4 +         [("DELETE", policies)] * 4


def test_a_failed_install_removes_what_it_created():
    calls = []

    def handler(request):
        calls.append(request.method)
        if request.method == "POST" and len([c for c in calls if c == "POST"]) == 6:
            return httpx.Response(409, json={})
        return httpx.Response(201 if request.method == "POST" else 200, json={})

    compiled = compile_policy(spec(), workload="checkout-api")

    async def run():
        async with httpx.AsyncClient(base_url="https://lab.invalid", transport=httpx.MockTransport(handler)) as admin:
            with pytest.raises(httpx.HTTPStatusError):
                async with InstalledPolicy(compiled, admin):
                    pass

    asyncio.run(run())
    assert calls.count("DELETE") == 5  # Four policies and the one binding that was created.


def test_installer_removes_the_policy_at_expiry():
    deleted = []

    def handler(request):
        if request.method == "DELETE":
            deleted.append(request.url.path)
        return httpx.Response(201 if request.method == "POST" else 200, json={})

    now = datetime.now(timezone.utc)
    compiled = compile_policy(spec(issued_at=now - timedelta(minutes=14, seconds=59.9), expires_at=now + timedelta(seconds=0.1)),
                              workload="checkout-api")

    async def run():
        async with httpx.AsyncClient(base_url="https://lab.invalid", transport=httpx.MockTransport(handler)) as admin:
            async with InstalledPolicy(compiled, admin) as installed:
                await asyncio.sleep(0.5)
                assert installed.removed and len(deleted) == 8

    asyncio.run(run())
