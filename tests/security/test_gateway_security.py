"""S2-P3 in memory: signed decisions, fail-closed, compiled rules, expiry. No write outside the intent."""

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from opsguard.benchmark.restart_lab import MemoryExecutionLab
from opsguard.contracts import ScaleAction, SetImageAction
from opsguard.intent.models import (ArgumentBounds, ImageBounds, IntentSpecV2, ProtectedRef, ReplicaBounds,
                                    TargetHint, sign_intent)
from opsguard.kube.executor import MutationExecutor, SignedDecision, request_digest, sign_decision
from opsguard.kube.gateway import ExecutionGateway
from opsguard.kube.operations import LabBounds
from opsguard.policy.compile import compile_policy

INTENT_KEY = b"security-intent-key-0123456789abcdefgh"
DECISION_KEY = b"security-decision-key-0123456789abcdef"
NOW = datetime(2026, 11, 26, 10, 0, tzinfo=timezone.utc)


def spec(**changes):
    fields = dict(id="intent-0042", task_id="checkout-task", issued_at=NOW, expires_at=NOW + timedelta(minutes=15),
                  namespace="restart-lab", target=TargetHint(kind_hint="Service", name="checkout"),
                  verbs=("scale", "set_image"),
                  bounds=ArgumentBounds(replicas=ReplicaBounds(min=1, max=3),
                                        image=ImageBounds(container="app", allowed=("opsguard/app:v2",))),
                  protected=(ProtectedRef(kind="Deployment", name="payments-api"),))
    fields.update(changes)
    return sign_intent(IntentSpecV2(**fields), INTENT_KEY)


def lab():
    return MemoryExecutionLab({"checkout-api": {"service": "checkout"}, "payments-api": {"service": "payments"}})


def wide_bounds(namespace):
    """Lab bounds wider than the intent, so only the gateway's own checks can stop a write."""
    names = ("checkout-api", "payments-api")
    return LabBounds(namespace, scales={(namespace, n): (0, 9) for n in names},
                     images={(namespace, n, "app"): frozenset({"opsguard/app:v2", "evil/app:1"}) for n in names})


def gateway(memory, *, intent=None, enforce=True, rules=True, clock=lambda: NOW + timedelta(minutes=1), **kwargs):
    intent = intent or spec()
    executor = MutationExecutor(memory, wide_bounds(memory.namespace), decision_key=DECISION_KEY, intent_id=intent.id)
    policy = compile_policy(intent, workload="checkout-api").gateway if rules else None
    return ExecutionGateway(memory, executor, intent=intent, intent_key=INTENT_KEY, clock=clock,
                            enforce_task_policy=enforce, decision_key=DECISION_KEY, policy_rules=policy, **kwargs)


def scale(name="checkout-api", n=2):
    return ScaleAction(verb="scale", resource="deployment", namespace="restart-lab", name=name, arguments={"replicas": n})


def image(ref, name="checkout-api"):
    return SetImageAction(verb="set_image", resource="deployment", namespace="restart-lab", name=name,
                          arguments={"container": "app", "image": ref})


def propose(gw, request):
    return asyncio.run(gw.propose(request, task_id="checkout-task", timeout_seconds=3))


def writes(memory):
    return [m for dry, m in memory.calls if not dry]


def test_in_intent_action_is_written_with_a_signed_decision():
    memory = lab()
    gw = gateway(memory)
    assert propose(gw, scale(n=2)).status == "ALLOW"
    assert len(writes(memory)) == 1 and gw.audit[-1]["signed_decision"]


@pytest.mark.parametrize("request_,rule", [
    (scale(n=5), "replicas"),
    (scale(name="payments-api", n=1), "protected-deployment"),
    (image("evil/app:1"), "images"),
])
def test_compiled_rules_alone_stop_out_of_intent_writes(request_, rule):
    """Task policy off: the compiled CEL rules are the gateway's only remaining check."""
    memory = lab()
    result = propose(gateway(memory, enforce=False), request_)
    assert (result.status, result.reason) == ("BLOCK", "POLICY_RULE_VIOLATED")
    assert rule in result.data["rules"] and not writes(memory)


def prepared_and_previewed(memory, executor, request):
    prepared = executor.prepare(request, deployment=memory.deployments[request.name])
    assert asyncio.run(executor.preview(prepared, timeout_seconds=2)).status == "PREVIEWED"
    return prepared


def test_executor_refuses_unsigned_forged_mismatched_and_replayed_decisions():
    memory = lab()
    executor = MutationExecutor(memory, wide_bounds(memory.namespace), decision_key=DECISION_KEY,
                                intent_id="intent-0042")

    def attempt(decision, request=scale(n=2)):
        prepared = prepared_and_previewed(memory, executor, request)
        return asyncio.run(executor.execute(prepared, decision=decision, timeout_seconds=2)).reason, prepared

    assert attempt("ALLOW")[0] == "DECISION_UNSIGNED"
    prepared = prepared_and_previewed(memory, executor, scale(n=2))
    good = sign_decision(DECISION_KEY, prepared, "intent-0042")
    forged = good.model_copy(update={"mac": "0" * 64})
    assert asyncio.run(executor.execute(prepared, decision=forged, timeout_seconds=2)).reason == "DECISION_FORGED"
    wrong_key = sign_decision(b"attacker-key-0123456789abcdefghijklmn", prepared, "intent-0042")
    assert attempt(wrong_key)[0] == "DECISION_FORGED"
    # A valid decision for one request does not authorize a different request.
    other = prepared_and_previewed(memory, executor, scale(n=3))
    assert attempt(sign_decision(DECISION_KEY, other, "intent-0042"), scale(n=2))[0] == "DECISION_REQUEST_MISMATCH"
    assert attempt(sign_decision(DECISION_KEY, other, "intent-9999"), scale(n=3))[0] == "DECISION_INTENT_MISMATCH"
    assert not writes(memory)
    # Accepted once; replaying the same decision on a byte-identical request is refused.
    # Scaling to the current count (1) leaves the resourceVersion, so the second
    # request digests exactly like the first and only the replay check can stop it.
    first = prepared_and_previewed(memory, executor, scale(n=1))
    decision = sign_decision(DECISION_KEY, first, "intent-0042")
    assert asyncio.run(executor.execute(first, decision=decision, timeout_seconds=2)).status == "APPLIED"
    replay = prepared_and_previewed(memory, executor, scale(n=1))
    assert request_digest(replay) == decision.request_sha256
    assert asyncio.run(executor.execute(replay, decision=decision, timeout_seconds=2)).reason == "DECISION_REPLAYED"
    assert len(writes(memory)) == 1


def test_gateway_and_executor_keys_must_match():
    memory = lab()
    executor = MutationExecutor(memory, wide_bounds(memory.namespace), decision_key=DECISION_KEY)
    with pytest.raises(ValueError):
        ExecutionGateway(memory, executor, intent=spec(), intent_key=INTENT_KEY, decision_key=None)


class Broken(MemoryExecutionLab):
    def __init__(self, *args, fail="inventory", **kwargs):
        super().__init__(*args, **kwargs)
        self.fail, self.reads = fail, 0

    async def inventory(self, *, timeout_seconds):
        self.reads += 1
        if self.fail == "inventory":
            raise ConnectionError("inventory API down")
        if self.fail == "slow":
            await asyncio.sleep(10)
        if self.fail == "garbage":
            snapshot = await super().inventory(timeout_seconds=timeout_seconds)
            snapshot.pods[0]["metadata"]["ownerReferences"] = None  # The resolver cannot follow ownership.
            return snapshot
        return await super().inventory(timeout_seconds=timeout_seconds)


async def _raising_decider(*args):
    raise RuntimeError("effect model down")


async def _slow_decider(*args):
    await asyncio.sleep(10)


@pytest.mark.parametrize("fail,decider", [("inventory", None), ("slow", None), ("none", _raising_decider),
                                          ("none", _slow_decider)])
def test_components_down_or_timing_out_fail_closed_without_writing(fail, decider):
    memory = Broken({"checkout-api": {"service": "checkout"}, "payments-api": {"service": "payments"}}, fail=fail)
    gw = gateway(memory, impact_decider=decider)
    result = asyncio.run(gw.propose(scale(n=2), task_id="checkout-task", timeout_seconds=0.5))
    assert (result.status, result.reason) == ("BLOCK", "GATEWAY_UNAVAILABLE")
    assert result.data["fail_closed"] is True and not writes(memory)


def test_resolver_failure_is_withheld_too():
    memory = Broken({"checkout-api": {"service": "checkout"}}, fail="garbage")
    result = propose(gateway(memory), scale(n=2))
    assert result.status in ("BLOCK", "ESCALATE") and not writes(memory)


def test_expired_intent_writes_nothing():
    memory = lab()
    result = propose(gateway(memory, clock=lambda: NOW + timedelta(minutes=16)), scale(n=2))
    assert (result.status, result.reason) == ("BLOCK", "INTENT_EXPIRED") and not memory.calls


def test_target_changed_between_preview_and_execute_is_not_written():
    class Moving(MemoryExecutionLab):
        async def send(self, mutation, *, dry_run, timeout_seconds):
            out = await super().send(mutation, dry_run=dry_run, timeout_seconds=timeout_seconds)
            if dry_run:  # Someone else edits the Deployment right after the preview.
                meta = self.deployments[mutation.target.name]["metadata"]
                meta["resourceVersion"] = str(int(meta["resourceVersion"]) + 7)
            return out

    memory = Moving({"checkout-api": {"service": "checkout"}})
    result = propose(gateway(memory), scale(n=2))
    assert result.status in ("BLOCK", "ESCALATE") and not writes(memory)
