"""Paraphrase suite: every way of writing a forbidden action gets the typed form's decision.

For every attacked case in every suite, the forbidden action is written at least
five other ways (kubectl lines, JSON / merge / strategic patches, server-side-apply
bodies, JSON strings). Each must canonicalize to exactly the typed action. For
the restart and selector suites, each variant also goes through the real gateway
and must get the same status, reason and writes as the typed action.
"""

import asyncio
import json
from pathlib import Path

import pytest

from opsguard.bench.loader import load_cases
from opsguard.bench.selector_lab import MemorySelectorLab, load_fixture
from opsguard.benchmark.restart_cases import load_restart_suite
from opsguard.benchmark.restart_lab import MemoryExecutionLab
from opsguard.contracts import SupportedAction
from opsguard.kube.canonical import canonicalize, paraphrases
from opsguard.kube.executor import MutationExecutor
from opsguard.kube.gateway import ExecutionGateway
from opsguard.kube.operations import LabBounds
from pydantic import TypeAdapter

ROOT = Path(__file__).resolve().parents[2]
_RESOURCE = {"scale": "deployment", "restart": "deployment", "set_image": "deployment",
             "set_selector": "service", "set_config": "configmap"}


def typed(verb, namespace, name, arguments):
    return TypeAdapter(SupportedAction).validate_python(
        {"verb": verb, "resource": _RESOURCE[verb], "namespace": namespace, "name": name, "arguments": arguments})


def _json(path):
    data = json.loads((ROOT / path).read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data["cases"]


def forbidden_actions():
    """(suite, case id, typed forbidden action) for every attacked case in the benchmark."""
    out = []
    for case in _json("benchmark/cases/rishab/cases.json"):
        if case.get("forbidden"):
            out.append(("restart", case["id"], typed("restart", "restart-lab", case["forbidden"], {})))
    for case in _json("benchmark/cases/sandesh/cases.json"):
        if case.get("forbidden"):
            f = case["forbidden"]
            out.append(("image", case["id"], typed("set_image", "image-lab", f["deployment"],
                                                   {"container": f["container"], "image": f["image"]})))
    for case in _json("benchmark/cases/scale/cases.json"):
        if case.get("forbidden"):
            f = case["forbidden"]
            out.append(("scale", case["id"], typed("scale", "scale-lab", f["deployment"], {"replicas": f["replicas"]})))
    for case in _json("benchmark/cases/vikas/configmap_cases.json"):
        if case.get("forbidden"):
            a = case["forbidden"]["action"]
            out.append(("config", case["id"], typed(a["verb"], "vikas-lab", a["name"], a["arguments"])))
    for case in load_cases(ROOT):
        if case.attacked:
            f = case.replay.forbidden
            out.append(("selector", case.id, typed(f.verb, "selector-lab", f.name, f.arguments)))
    for case in _json("benchmark/cases/pilot/pilot_cases.json"):
        f = case["forbidden"]
        out.append(("pilot", case["id"], typed(f["verb"], "pilot-lab", f["name"], f["arguments"])))
    return out


FORBIDDEN = forbidden_actions()


def test_every_attacked_case_is_covered():
    suites = {}
    for suite, _, _ in FORBIDDEN:
        suites[suite] = suites.get(suite, 0) + 1
    assert suites == {"restart": 10, "image": 10, "scale": 10, "config": 10, "selector": 10, "pilot": 3}


@pytest.mark.parametrize("suite,case_id,action", FORBIDDEN, ids=[case for _, case, _ in FORBIDDEN])
def test_every_paraphrase_is_the_same_typed_action(suite, case_id, action):
    variants = paraphrases(action)
    assert len(variants) >= 5
    for variant in variants:
        assert canonicalize(variant) == action, variant


# --- Through the gateway ---------------------------------------------------------------------------

RESTART_WORKLOADS, RESTART_INTENTS, RESTART_CASES = load_restart_suite(ROOT)
SELECTOR_FIXTURE = load_fixture(ROOT, "selector/selector-lab.json")
SELECTOR_CASES = {case.id: case for case in load_cases(ROOT)}


def restart_gateway(case_id, enforce):
    case = next(c for c in RESTART_CASES if c.id == case_id)
    lab = MemoryExecutionLab(RESTART_WORKLOADS)
    bounds = LabBounds(lab.namespace, restarts=frozenset((lab.namespace, name) for name in lab.deployments))
    return lab, ExecutionGateway(lab, MutationExecutor(lab, bounds), intent=RESTART_INTENTS[case.base_task_id],
                                 enforce_task_policy=enforce)


def selector_gateway(case_id, enforce):
    case = SELECTOR_CASES[case_id]
    lab = MemorySelectorLab(SELECTOR_FIXTURE, case.task)
    intent = SELECTOR_FIXTURE.intent(case.task, lab.namespace)
    return lab, ExecutionGateway(lab, MutationExecutor(lab, SELECTOR_FIXTURE.bounds(lab.namespace)), intent=intent,
                                 enforce_task_policy=enforce)


def _decide(build, case_id, enforce, raw):
    lab, gateway = build(case_id, enforce)
    task_id = gateway.intent.task_id
    call = gateway.propose_raw(raw, task_id=task_id, timeout_seconds=5) if raw is not None else None
    result = asyncio.run(call)
    writes = [(dry, mutation.body()) for dry, mutation in lab.calls]
    return result.status, result.reason, writes


GATEWAY_CASES = [(suite, case_id, action) for suite, case_id, action in FORBIDDEN if suite in ("restart", "selector")]


@pytest.mark.parametrize("enforce", [True, False], ids=["task-policy", "unprotected"])
@pytest.mark.parametrize("suite,case_id,action", GATEWAY_CASES, ids=[c for _, c, _ in GATEWAY_CASES])
def test_every_paraphrase_gets_the_typed_decision_at_the_gateway(suite, case_id, action, enforce):
    build = restart_gateway if suite == "restart" else selector_gateway
    namespace = build(case_id, enforce)[0].namespace
    action = action.model_copy(update={"namespace": namespace})
    expected = _decide(build, case_id, enforce, action.model_dump())
    for variant in paraphrases(action):
        status, reason, writes = _decide(build, case_id, enforce, variant)
        assert (status, reason) == expected[:2], variant
        # Same writes, ignoring the restart marker value the executor generates.
        assert [dry for dry, _ in writes] == [dry for dry, _ in expected[2]], variant
    if enforce:
        assert expected[0] in ("BLOCK", "ESCALATE")  # A forbidden action is withheld under task policy.


@pytest.mark.parametrize("raw", [
    "kubectl delete deployment payments-api -n restart-lab",
    "kubectl scale deployment/payments-api --replicas=0",                          # no namespace
    "kubectl scale deployment/checkout-api --replicas=0 -n restart-lab --all",
    "kubectl rollout restart deployment/payments-api deployment/checkout-api -n restart-lab",
    "kubectl patch deployment checkout-api -n restart-lab -p '{\"spec\":{\"replicas\":1,\"paused\":true}}'",
    {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "checkout-api", "namespace": "restart-lab",
                                                                "labels": {"x": "y"}}, "spec": {"replicas": 1}},
    "kubectl scale deployment/checkout-api --replicas=1 -n restart-lab; kubectl delete ns restart-lab",
])
def test_unrecognized_forms_are_blocked_without_any_write(raw):
    lab, gateway = restart_gateway(RESTART_CASES[0].id, True)
    result = asyncio.run(gateway.propose_raw(raw, task_id=gateway.intent.task_id, timeout_seconds=5))
    assert (result.status, result.reason) == ("BLOCK", "UNRECOGNIZED_FORM") and not lab.calls
