import asyncio
from collections import Counter
from dataclasses import replace
from pathlib import Path

from opsguard.benchmark.run_scale import CONDITIONS, run_case
from opsguard.benchmark.scale_cases import ScaleOutcome, load_scale_suite, observe_completion
from opsguard.benchmark.scale_lab import MemoryScaleLab, ScaleLabGateway
from opsguard.contracts import PodLogsRead, ScaleAction
from opsguard.evidence.pod_logs import read_pod_evidence


ROOT = Path(__file__).resolve().parents[2]
WORKLOADS, INTENTS, CASES = load_scale_suite(ROOT)


def test_case_allocation_and_controlled_replays():
    attacked = [case for case in CASES if case.injection]
    assert len(CASES) - len(attacked) == 4
    assert Counter(case.injection.channel for case in attacked) == {
        "logs": 4, "alerts": 2, "runbooks": 2, "configmaps": 2,
    }
    assert len({case.injection.payload for case in attacked}) == 10

    async def scenario():
        for case in CASES:
            for condition in CONDITIONS:
                row = await run_case(root=ROOT, case=case, workloads=WORKLOADS, intent=INTENTS[case.base_task_id],
                                     condition=condition, timeout=3)
                assert row["error"] is None and row["legitimate_met"]
                if case.injection:
                    assert row["evidence_exposed"]
                    assert row["forbidden_seen"] == (condition != "task-policy")
                    assert row["safe_completion"] == (condition == "task-policy")
                else:
                    assert row["safe_completion"]

    asyncio.run(scenario())


def test_oracle_requires_current_generation_and_service_health():
    lab = MemoryScaleLab(WORKLOADS, "checkout-api", None)
    state = lab.states["checkout-api"]
    outcome = ScaleOutcome(deployment="checkout-api", replicas=3)
    lab.states["checkout-api"] = replace(state, replicas=3, ready=3, generation=2)
    assert not asyncio.run(observe_completion(lab, outcome, service="checkout", timeout_seconds=1))
    lab.states["checkout-api"] = replace(lab.states["checkout-api"], observed_generation=2)
    assert not asyncio.run(observe_completion(lab, outcome, service="missing", timeout_seconds=1))
    assert asyncio.run(observe_completion(lab, outcome, service="checkout", timeout_seconds=1))


def test_pod_log_scope_enforced_before_backend_read():
    lab = MemoryScaleLab(WORKLOADS, "checkout-api", None)

    async def scenario():
        source = await lab.log_source("checkout-api", timeout_seconds=1)
        request = PodLogsRead(tool="read_pod_logs", namespace=lab.namespace, name="checkout-api")
        try:
            await read_pod_evidence(request, source=source, backend=object(), allowed_workloads=frozenset(),
                                    timeout_seconds=1)
        except PermissionError:
            return
        raise AssertionError("out-of-scope log was not rejected")

    asyncio.run(scenario())


def test_repeated_satisfied_scale_does_not_submit_another_mutation():
    class CountingLab(MemoryScaleLab):
        calls = []

        async def replace_scale(self, mutation, *, dry_run, timeout_seconds):
            self.calls.append(dry_run)
            await super().replace_scale(mutation, dry_run=dry_run, timeout_seconds=timeout_seconds)

    lab = CountingLab(WORKLOADS, "checkout-api", None)
    gateway = ScaleLabGateway(lab, workloads=WORKLOADS, intent=INTENTS["scale-checkout"],
                              task_id="scale-checkout", enforce_task_policy=True)
    action = ScaleAction.model_validate({"verb": "scale", "resource": "deployment", "namespace": lab.namespace,
                                         "name": "checkout-api", "arguments": {"replicas": 3}})

    async def scenario():
        first = await gateway.propose(action, task_id="scale-checkout", timeout_seconds=1)
        second = await gateway.propose(action, task_id="scale-checkout", timeout_seconds=1)
        assert first.reason == "SCALE_REQUEST_ACCEPTED"
        assert second.reason == "ALREADY_AT_DESIRED_REPLICAS"
        assert lab.calls == [True, False]

    asyncio.run(scenario())
