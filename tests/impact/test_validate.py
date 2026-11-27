"""S3-P4: the effect model validation harness (scoring, tiers, event parsing, rescore)."""

from copy import deepcopy

import pytest

from opsguard.impact.kinds import KINDS
from opsguard.impact.state import ClusterState
from opsguard.impact import validate
from opsguard.impact.validate import (ChangeSpec, ObservedChange, aggregate, apply_locally, change_objects,
                                      get_fixture, kubectl_args, observed_changes, observed_tiers, report_markdown,
                                      rescore_records, score_trial, wilson)

NS = "val"


def simulated_items(fixture, ready=True):
    """The fixture's manifest plus one ReplicaSet and ready Pods per Deployment, as kind would show them."""
    items = fixture.manifest(NS, "img")
    extra = []
    for obj in items:
        if obj["kind"] != "Deployment":
            continue
        name = obj["metadata"]["name"]
        rs = {"kind": "ReplicaSet", "metadata": {"name": f"{name}-1", "namespace": NS,
                                                 "ownerReferences": [{"kind": "Deployment", "name": name}]},
              "spec": {"template": deepcopy(obj["spec"]["template"])}}
        extra.append(rs)
        for i in range(obj["spec"]["replicas"]):
            extra.append({"kind": "Pod", "metadata": {"name": f"{name}-1-{i}", "namespace": NS,
                                                      "labels": dict(obj["spec"]["template"]["metadata"]["labels"]),
                                                      "ownerReferences": [{"kind": "ReplicaSet", "name": f"{name}-1"}]},
                          "spec": deepcopy(obj["spec"]["template"]["spec"]),
                          "status": {"conditions": [{"type": "Ready", "status": "True" if ready else "False"}]}})
    return items + extra


READ = [k.kind for k in KINDS if k.kind != "Secret"]


@pytest.fixture(scope="module")
def fixture():
    return get_fixture("checkout-payments")


def test_catalogue_has_benign_and_harmful_changes(fixture):
    assert len(fixture.catalogue) == 22 and sum(c.harmful for c in fixture.catalogue) == 3
    assert len({c.id for c in fixture.catalogue}) == len(fixture.catalogue)
    assert fixture.topology.name == "checkout-payments"


def test_every_change_has_a_local_patch_and_a_kubectl_form(fixture):
    st = ClusterState.from_objects(NS, simulated_items(fixture), read=READ)
    for spec in fixture.catalogue:
        before, after = change_objects(spec, st)
        command = kubectl_args(spec, NS, st)
        assert command[:2] == ["-n", NS]
        if spec.verb == "delete_pod":
            assert after is None and command[2] == "delete"
        else:
            assert after != before and command[2] == "patch"


def test_apply_locally_matches_the_patch_semantics():
    dep = {"kind": "Deployment", "metadata": {"name": "a"}, "spec": {"replicas": 2, "template": {
        "spec": {"containers": [{"name": "app", "image": "x"}]}}}}
    out = apply_locally(dep, ChangeSpec("r", "set_resources", "Deployment", "a", {"cpu": "20m"}))
    assert out["spec"]["template"]["spec"]["containers"][0]["resources"] == {"requests": {"cpu": "20m"}}
    assert apply_locally(dep, ChangeSpec("s", "scale", "Deployment", "a", {"replicas": 5}))["spec"]["replicas"] == 5
    with pytest.raises(ValueError):
        apply_locally(dep, ChangeSpec("x", "explode", "Deployment", "a"))


def test_observed_changes_dedupe_ignore_and_follow_owners():
    initial = [{"kind": "Deployment", "metadata": {"name": "a"}},
               {"kind": "ReplicaSet", "metadata": {"name": "a-2", "ownerReferences": [{"kind": "Deployment", "name": "a"}]}}]
    pod = {"kind": "Pod", "metadata": {"name": "a-2-x", "ownerReferences": [{"kind": "ReplicaSet", "name": "a-2"}]}}
    slice_ = {"kind": "EndpointSlice", "metadata": {"name": "svc-1", "labels": {"kubernetes.io/service-name": "svc"}}}
    events = [{"type": "ADDED", "object": pod}, {"type": "MODIFIED", "object": pod}, {"type": "MODIFIED", "object": pod},
              {"type": "MODIFIED", "object": slice_}, {"type": "ADDED", "object": {"kind": "Event", "metadata": {"name": "e"}}}]
    changes = observed_changes(events, initial, NS)
    assert [(c.kind, c.op) for c in changes] == [("Pod", "create"), ("Pod", "modify"), ("EndpointSlice", "modify")]
    assert set(changes[0].ancestors) == {f"ReplicaSet/{NS}/a-2", f"Deployment/{NS}/a"}
    assert changes[2].ancestors == (f"Service/{NS}/svc",)


def test_observed_tiers():
    samples = {
        "full": [(0.0, 2, True), (0.5, 0, True), (1.0, 2, True)],
        "sustained": [(0.0, 1, False), (1.0, 1, False), (2.5, 1, False)],
        "blip": [(0.0, 2, True), (0.5, 2, False), (1.0, 2, True)],
        "dip": [(0.0, 3, True), (0.5, 2, True)],
        "calm": [(0.0, 1, True), (0.5, 1, True)],
        "churn": [(0.0, 2, True)],
    }
    baseline = {"full": 2, "sustained": 1, "blip": 2, "dip": 3, "calm": 1, "churn": 2}
    assert observed_tiers(samples, baseline, {"churn"}) == {"full": 3, "sustained": 3, "blip": 2, "dip": 1,
                                                            "calm": 0, "churn": 1}


def test_score_trial_and_aggregate(fixture):
    st = ClusterState.from_objects(NS, simulated_items(fixture), read=READ)
    spec = next(c for c in fixture.catalogue if c.id == "checkout-api-restart")
    before, after = change_objects(spec, st)
    prediction = validate.predict_effects(verb=spec.verb, state=st, before=before, preview=after,
                                          topology=fixture.topology)
    dep = f"Deployment/{NS}/checkout-api"
    observed = [ObservedChange("Deployment", "checkout-api", "modify", ()),
                ObservedChange("ReplicaSet", "checkout-api-2", "create", (dep,)),
                ObservedChange("Pod", "checkout-api-2-z", "create", (f"ReplicaSet/{NS}/checkout-api-2", dep)),
                ObservedChange("ConfigMap", "surprise", "modify", ())]
    result = score_trial(prediction, observed, {"checkout": 1, "payments": 0, "web": 0})
    assert (result["observed"], result["observed_matched"]) == (4, 3)
    assert result["missed"][0]["name"] == "surprise"
    assert {r["service"]: (r["predicted"], r["observed"]) for r in result["tiers"]}["checkout"] == (1, 1)
    summary = aggregate([result, {"id": "x", "error": "TimeoutError"}])
    assert summary["static_recall"] == 0.75 and summary["harness_errors"] == 1 and summary["tier_accuracy"] == 1.0
    assert summary["missed_by_kind"] == {"ConfigMap": 1}


def test_rescore_uses_the_recorded_state(fixture):
    items = simulated_items(fixture)
    spec = next(c for c in fixture.catalogue if c.id == "checkout-selector-nothing")
    record = {"fixture": "checkout-payments", "namespace": NS, "change": spec.to_json(), "state": items,
              "read_kinds": READ, "error": None,
              "observed": [{"kind": "Service", "name": "checkout", "op": "modify", "ancestors": []},
                           {"kind": "EndpointSlice", "name": "checkout-abc", "op": "modify",
                            "ancestors": [f"Service/{NS}/checkout"]}],
              "observed_tiers": {"checkout": 3, "payments": 0, "web": 0}}
    scored, summary = rescore_records([record], lambda name: fixture.topology)
    assert summary["static_recall"] == 1.0 and summary["tier_accuracy"] == 1.0
    assert summary["confusion"]["S3->S3"] == 1


def test_wilson_interval_and_report():
    low, high = wilson(95, 100)
    assert 0.88 < low < 0.95 < high < 0.99
    text = report_markdown({"a": {"model": "m", "trials": 1, "harness_errors": 0, "uncertain_trials": 0,
                                  "static_recall": 0.96, "recall_ci95": (0.9, 0.99), "precision": 0.8,
                                  "tier_accuracy": 1.0, "tier_under_prediction": 0.0, "tier_within_one": 1.0,
                                  "confusion": {}, "missed_by_kind": {}, "recall_by_rule": {"rollout": 0.96}}})
    assert "not met" in text  # one topology is not three


def test_cli_plan(capsys):
    assert validate.main(["plan", "--fixture", "checkout-payments"]) == 0
    assert '"changes": 22' in capsys.readouterr().out


def test_watch_parses_streamed_pretty_json(tmp_path):
    """kubectl --watch-only --output-watch-events -o json streams indented objects back to back."""
    script = tmp_path / "fake_kubectl.py"
    script.write_text(
        "import json, sys, time\n"
        "for i in range(3):\n"
        "    print(json.dumps({'type': 'MODIFIED', 'object': {'kind': 'Pod', 'metadata': {'name': f'p{i}'}}}, indent=4),"
        " flush=True)\n"
        "time.sleep(30)\n")
    import sys
    watch = validate._Watch([sys.executable, str(script)], NS, "Pod")
    import time
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline and "".join(watch.chunks).count('"type"') < 3:
        time.sleep(0.05)
    events = watch.stop()
    assert [e["object"]["metadata"]["name"] for e in events] == ["p0", "p1", "p2"]
