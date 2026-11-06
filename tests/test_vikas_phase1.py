import json
from pathlib import Path

import pytest

from opsguard.impact.decision import decide_risk
from opsguard.impact.graph import ResourceRef, affected_services, build_dependency_graph
from opsguard.operations.set_config import prepare_set_config


CASE_PATH = Path(__file__).resolve().parents[1] / "benchmark/cases/vikas/feature_flag_clean.json"


@pytest.mark.parametrize(
    ("policy_outcome", "expected_decision"),
    [("PASS", "ALLOW"), ("BLOCK", "BLOCK"), ("AMBIGUOUS", "ESCALATE")],
)
def test_feature_flag_replay_with_fakes(policy_outcome, expected_decision):
    case = json.loads(CASE_PATH.read_text(encoding="utf-8"))
    action = case["action"]
    configmap = ResourceRef("configmap", action["namespace"], action["name"])
    resources = [configmap]
    edges = []
    for consumer in case["consumers"]:
        workload = ResourceRef("workload", configmap.namespace, consumer["workload"])
        service = ResourceRef("service", configmap.namespace, consumer["service"])
        resources.extend((workload, service))
        edges.extend(((configmap, workload), (workload, service)))

    graph = build_dependency_graph(resources, edges)
    reachable = affected_services(graph, configmap)
    assert [service.name for service in reachable] == case["expected_downstream_services"]

    mutation = prepare_set_config(
        action,
        lab_values={
            (configmap.namespace, configmap.name, action["arguments"]["key"]): set(case["lab_allowed_values"]),
        },
    )
    stored_data = dict(case["initial_data"])
    preview_data = {**stored_data, **mutation.patch_body()["data"]}
    assert stored_data == case["initial_data"]
    assert preview_data == case["expected_data"]

    # The fixture simulates a benign, reversible change with two affected Services.
    decision = decide_risk(
        policy_outcome=policy_outcome,
        provenance_risk=0,
        blast_radius=0.5,
        irreversibility=0,
        predicted_affected_services=reachable,
    )
    if decision.decision == "ALLOW":
        stored_data.update(mutation.patch_body()["data"])

    assert decision.decision == expected_decision
    assert decision.predicted_affected_services == reachable
    if expected_decision == "ALLOW":
        assert stored_data == case["expected_data"]
    else:
        assert stored_data == case["initial_data"]
