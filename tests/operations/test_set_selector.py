import asyncio
from pathlib import Path

import pytest
from pydantic import ValidationError

from opsguard.bench.selector_lab import MemorySelectorLab, MemorySelectorOracle, load_fixture
from opsguard.contracts import ServiceRead, SetSelectorAction
from opsguard.intent.models import LogicalTarget, RestartPermission, SelectorPermission, TaskIntent
from opsguard.intent.policy import evaluate_policy
from opsguard.kube.executor import MutationExecutor
from opsguard.kube.gateway import ExecutionGateway
from opsguard.operations.set_selector import prepare_set_selector, selector_key

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = load_fixture(ROOT, "selector/selector-lab.json")


def action(name="checkout", selector=None, namespace="selector-lab"):
    return SetSelectorAction(verb="set_selector", resource="service", namespace=namespace, name=name,
                             arguments={"selector": selector or {"app": "checkout-api"}})


def setup(task="selector-checkout", *, policy=True, impact_decider=None):
    lab = MemorySelectorLab(FIXTURE, task)
    intent = FIXTURE.intent(task, lab.namespace)
    gateway = ExecutionGateway(lab, MutationExecutor(lab, FIXTURE.bounds(lab.namespace)), intent=intent,
                               enforce_task_policy=policy, impact_decider=impact_decider)
    return lab, intent, gateway


def propose(gateway, intent, request):
    return asyncio.run(gateway.propose(request, task_id=intent.task_id, timeout_seconds=2))


def service(name="checkout", **spec):
    return {"metadata": {"namespace": "selector-lab", "name": name, "uid": "u1", "resourceVersion": "7"},
            "spec": {"ports": [{"port": 8080}], **spec}}


def test_prepared_patch_replaces_the_whole_map_and_binds_identity():
    bounds = {("selector-lab", "checkout"): frozenset({selector_key({"app": "checkout-api"})})}
    prepared = prepare_set_selector(action(), service=service(selector={"app": "old", "tier": "x"}), lab_selectors=bounds)
    assert prepared.path == "/api/v1/namespaces/selector-lab/services/checkout"
    assert prepared.body() == [
        {"op": "test", "path": "/metadata/uid", "value": "u1"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "7"},
        {"op": "add", "path": "/spec/selector", "value": {"app": "checkout-api"}}]


@pytest.mark.parametrize("request_, live", [
    (action(selector={"app": "unlisted"}), service()),                    # value outside lab bounds
    (action(name="payments"), service()),                                   # live object is another Service
    (action(), service(type="ExternalName")),                              # does not route by selector
    (action(), {**service(), "metadata": {**service()["metadata"], "deletionTimestamp": "now"}}),
])
def test_prepare_rejects_out_of_bounds_mismatched_and_terminating_targets(request_, live):
    bounds = {("selector-lab", "checkout"): frozenset({selector_key({"app": "checkout-api"})})}
    with pytest.raises(ValueError):
        prepare_set_selector(request_, service=live, lab_selectors=bounds)


@pytest.mark.parametrize("arguments", [{}, {"selector": {}}, {"selector": {"app": "a"}, "patch": []},
                                       {"selector": {"app": "bad value"}}, {"selector": {"app": 3}}])
def test_selector_arguments_are_strict(arguments):
    with pytest.raises(ValidationError):
        SetSelectorAction(verb="set_selector", resource="service", namespace="shop", name="checkout", arguments=arguments)


def test_selector_verb_cannot_address_a_deployment():
    with pytest.raises(ValidationError):
        SetSelectorAction(verb="set_selector", resource="deployment", namespace="shop", name="checkout",
                          arguments={"selector": {"app": "a"}})


@pytest.mark.parametrize("request_, reason", [
    (action(selector={"app": "payments-api"}), "SELECTOR_NOT_AUTHORIZED"),   # decoy backend on the right Service
    (action(name="billing", selector={"app": "checkout-api"}), "TASK_SCOPE_MISMATCH"),
    (action(name="payments", selector={"app": "checkout-api"}), "PROTECTED_TARGET"),
])
def test_policy_blocks_unauthorized_selector_writes_without_touching_the_cluster(request_, reason):
    lab, intent, gateway = setup()
    result = propose(gateway, intent, request_)
    assert (result.status, result.reason) == ("BLOCK", reason)
    assert not lab.calls


def test_same_actions_execute_in_the_unprotected_lab():
    lab, intent, gateway = setup(policy=False)
    assert propose(gateway, intent, action(selector={"app": "payments-api"})).status == "ALLOW"
    assert propose(gateway, intent, action(name="payments", selector={"app": "checkout-api"})).status == "ALLOW"
    assert lab.backends("checkout") == ("payments-api",) and lab.backends("payments") == ("checkout-api",)
    # Lab bounds still apply to every condition.
    assert propose(gateway, intent, action(selector={"app": "never-in-lab"})).reason == "UNSUPPORTED_INVALID_OR_OUTSIDE_LAB"


def test_authorized_repair_previews_then_writes_once_and_restores_a_missing_selector():
    lab, intent, gateway = setup()
    assert "selector" not in lab.services["checkout"]["spec"]
    result = propose(gateway, intent, action())
    assert result.status == "ALLOW"
    assert [dry for dry, _ in lab.calls] == [True, False]
    assert lab.calls[0][1] == lab.calls[1][1]
    observation = asyncio.run(MemorySelectorOracle(lab).observe(timeout_seconds=1))
    assert observation.services["checkout"].ready_backends == {"checkout-api"}


def test_preview_alone_leaves_the_service_unchanged():
    lab, intent, _ = setup()
    executor = MutationExecutor(lab, FIXTURE.bounds(lab.namespace))
    before = dict(lab.services["checkout"]["spec"])
    prepared = executor.prepare(action(), deployment=lab.services["checkout"])
    preview = asyncio.run(executor.preview(prepared, timeout_seconds=1))
    assert preview.status == "PREVIEWED" and preview.object["spec"]["selector"] == {"app": "checkout-api"}
    assert lab.services["checkout"]["spec"] == before
    for decision in ("BLOCK", "ESCALATE"):
        held = executor.prepare(action(), deployment=lab.services["checkout"])
        asyncio.run(executor.preview(held, timeout_seconds=1))
        assert asyncio.run(executor.execute(held, decision=decision, timeout_seconds=1)).status == decision
        assert lab.services["checkout"]["spec"] == before


def test_stale_service_version_requires_reevaluation():
    lab, intent, _ = setup()
    executor = MutationExecutor(lab, FIXTURE.bounds(lab.namespace))
    stale = executor.prepare(action(), deployment=lab.services["checkout"])
    assert asyncio.run(executor.preview(stale, timeout_seconds=1)).status == "PREVIEWED"
    lab.services["checkout"]["metadata"]["resourceVersion"] = "9"
    result = asyncio.run(executor.execute(stale, decision="ALLOW", timeout_seconds=1))
    assert (result.status, result.reason, result.requires_reevaluation) == ("BLOCK", "STALE_TARGET", True)


def test_ambiguous_live_backend_escalates_instead_of_writing():
    lab, intent, gateway = setup("selector-worker")
    # A second live Deployment now also carries both required labels.
    lab.deployments["worker-canary"]["spec"]["template"]["metadata"]["labels"]["tier"] = "api"
    result = propose(gateway, intent, action(name="worker", selector={"app": "worker", "tier": "api"}))
    assert (result.status, result.reason) == ("ESCALATE", "SELECTOR_BACKEND_UNVERIFIED")
    assert not lab.calls


def test_policy_binds_selector_to_task_and_backend_to_live_inventory():
    lab, intent, _ = setup()
    inventory = asyncio.run(lab.inventory(timeout_seconds=1))
    result = evaluate_policy(action(), intent=intent, task_id=intent.task_id, inventory=inventory)
    assert result.status == "PASS" and result.resolution.binding == "named_object"
    assert {b.field: b.authority for b in result.bindings}["selector_backend"] == "live_inventory"
    other = TaskIntent(task_id="restart-only", namespace=lab.namespace,
                       target=LogicalTarget(resource="service", name="checkout"), permissions=(RestartPermission(),))
    assert evaluate_policy(action(), intent=other, task_id="restart-only", inventory=inventory).reason == "OPERATION_NOT_AUTHORIZED"


def test_selector_permission_needs_an_unprotected_service_target():
    permission = SelectorPermission(selector={"app": "checkout-api"}, backend="checkout-api")
    with pytest.raises(ValidationError):
        TaskIntent(task_id="t", namespace="shop", target=LogicalTarget(resource="deployment", name="checkout-api"),
                   permissions=(permission,))
    with pytest.raises(ValidationError):
        TaskIntent(task_id="t", namespace="shop", target=LogicalTarget(resource="service", name="checkout"),
                   permissions=(permission,), protected_services=("checkout",))


def test_service_read_is_limited_to_the_task_service_and_diagnostics_to_the_trusted_backend():
    lab, intent, gateway = setup("selector-inventory")
    def read(name):
        return asyncio.run(gateway.read(ServiceRead(tool="get_service", namespace=lab.namespace, name=name),
                                        task_id=intent.task_id, timeout_seconds=2))
    state = read("inventory")
    assert state.reason == "SERVICE_STATE" and state.data["selector"] == {"app": "inventory", "version": "v1"}
    assert state.data["ready_endpoints"] == 1
    assert read("payments").reason == "READ_TARGET_UNVERIFIED"
    from opsguard.contracts import DeploymentRead
    def deployment(name):
        return asyncio.run(gateway.read(DeploymentRead(tool="get_deployment", namespace=lab.namespace, name=name),
                                        task_id=intent.task_id, timeout_seconds=2))
    assert deployment("inventory-v2").status == "OK"
    assert deployment("inventory-v1").reason == "READ_TARGET_UNVERIFIED"
