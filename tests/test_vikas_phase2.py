import json
import os
import runpy
import time
from pathlib import Path
from shutil import which
from subprocess import run
from types import SimpleNamespace
from uuid import uuid4

import pytest

from opsguard.benchmark.vikas_cases import expected_state_met, load_cases, namespaced_action
from opsguard.evidence.configmap import ConfigMapEvidenceSource, inject_configmap_evidence, read_configmap_evidence
from opsguard.impact.assessment import decide_with_impact
from opsguard.impact.effects import ConfigEffect, ImpactEstimate, predict_impact
from opsguard.impact.graph import ResourceRef
from opsguard.operations.set_config import prepare_set_config


ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = ROOT / "benchmark/fixtures/vikas"
CASES = load_cases(ROOT / "benchmark/cases/vikas/configmap_cases.json")
SERVICE_CONFIG = {"checkout": "shared-features", "storefront": "shared-features", "decoy": "decoy-features"}


def _trusted_fixture(namespace):
    effects = []
    bounds = {}
    for rule in json.loads((FIXTURE_DIR / "impact_topology.json").read_text(encoding="utf-8")):
        configmap = ResourceRef("configmap", namespace, rule["configmap"])
        consumers = tuple(ResourceRef("workload", namespace, name) for name in rule["workloads"])
        effects.append(ConfigEffect(
            configmap, rule["key"], consumers, frozenset({rule["degraded"]}), frozenset({rule["healthy"]}),
        ))
        bounds[namespace, configmap.name, rule["key"]] = frozenset({rule["healthy"], rule["degraded"]})
    return effects, bounds


def _assess(policy_outcome, impact=None):
    return decide_with_impact(
        policy_outcome=policy_outcome,
        provenance_risk=0,
        irreversibility=0,
        impact=impact,
    )


def _estimate(before, preview, inventory, effects):
    return predict_impact(
        verb="set_config",
        before=before,
        preview=preview,
        config_effects=effects,
        **inventory,
    )


@pytest.fixture(scope="module")
def fixture_response():
    return runpy.run_path(str(FIXTURE_DIR / "app.py"))["response_for_path"]


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_local_case_replay(case, tmp_path, fixture_response):
    namespace = "vikas-local"
    effects, bounds = _trusted_fixture(namespace)
    data_dirs = {name: tmp_path / name for name in case["initial"]}
    for directory in data_dirs.values():
        directory.mkdir()

    def stage_initial():
        for name, data in case["initial"].items():
            for key, value in data.items():
                (data_dirs[name] / key).write_text(value, encoding="utf-8")

    def read_configmap(_, name):
        return {path.name: path.read_text(encoding="utf-8") for path in data_dirs[name].iterdir()}

    def probe_http(_, service, path):
        return fixture_response(service, data_dirs[SERVICE_CONFIG[service]], path)[0]

    def observed(outcome):
        return expected_state_met(
            outcome["expected"], namespace=namespace, read_configmap=read_configmap, probe_http=probe_http,
        )

    def apply(mutation):
        for key, value in mutation.patch_body()["data"].items():
            (data_dirs[mutation.name] / key).write_text(value, encoding="utf-8")

    def apply_if_allowed(mutation, decision):
        if decision.decision == "ALLOW":
            apply(mutation)

    def resource(name):
        return {"name": name, "namespace": namespace}

    deployments = []
    services = []
    for service in SERVICE_CONFIG:
        labels = {"app": service}
        deployments.append({
            "metadata": resource(f"{service}-api"),
            "spec": {"replicas": 1, "template": {"metadata": {"labels": labels}}},
        })
        services.append({"metadata": resource(service), "spec": {"selector": labels}})

    def preview_impact(mutation):
        maps = [
            {"kind": "ConfigMap", "metadata": resource(name), "data": read_configmap(namespace, name)}
            for name in data_dirs
        ]
        before = next(configmap for configmap in maps if configmap["metadata"]["name"] == mutation.name)
        preview = {**before, "data": {**before["data"], **mutation.patch_body()["data"]}}
        inventory = {"configmaps": maps, "deployments": deployments, "services": services}
        return _estimate(before, preview, inventory, effects)

    stage_initial()
    assert not observed(case["legitimate"])
    if "forbidden" in case:
        assert not observed(case["forbidden"])
        attack = prepare_set_config(namespaced_action(case["forbidden"], namespace), lab_values=bounds)
        decision = _assess("BLOCK")
        assert decision.decision == "BLOCK"
        apply_if_allowed(attack, decision)
        assert read_configmap(namespace, attack.name) == case["initial"][attack.name]

        # Controlled replay measures the withheld operation against the independent oracle.
        predicted = preview_impact(attack)
        assert {service.name for service in predicted.degraded_services} == {
            probe["service"] for probe in case["forbidden"]["expected"]["probes"] if probe["status"] == 503
        }
        apply(attack)
        assert observed(case["forbidden"])
        stage_initial()

    mutation = prepare_set_config(namespaced_action(case["legitimate"], namespace), lab_values=bounds)
    impact = preview_impact(mutation)
    assert {service.name for service in impact.reachable_services} == {
        service for service, configmap in SERVICE_CONFIG.items() if configmap == mutation.name
    }
    assert not impact.degraded_services
    decision = _assess("PASS", impact)
    assert decision.decision == "ALLOW"
    assert read_configmap(namespace, mutation.name) == case["initial"][mutation.name]
    apply_if_allowed(mutation, decision)
    assert observed(case["legitimate"])
    if "forbidden" in case:
        assert not observed(case["forbidden"])


def test_unknown_impact_escalates_instead_of_becoming_an_allow():
    service = ResourceRef("service", "shop", "checkout")
    estimate = ImpactEstimate((service,), (), (service,), 10)

    result = _assess("PASS", estimate)

    assert result.decision == "ESCALATE"
    assert result.reason_code == "impact_uncertain"
    assert result.predicted_affected_services == ()
    assert _assess("PASS").reason_code == "impact_unavailable"
    assert _assess("BLOCK", estimate).reason_code == "policy_violation"


@pytest.fixture
def kind_lab(tmp_path):
    # A named kind cluster supplies a dedicated kubeconfig for this test.
    cluster = os.environ.get("OPSGUARD_KIND_CLUSTER")
    if not cluster:
        pytest.skip("set OPSGUARD_KIND_CLUSTER to opt into the disposable kind smoke tests")
    if not which("kind") or not which("kubectl"):
        pytest.fail("kind and kubectl are required for the opt-in smoke tests")
    kubeconfig = tmp_path / "kind-kubeconfig"
    exported = run(["kind", "get", "kubeconfig", "--name", cluster], check=True, capture_output=True, timeout=60)
    namespace = f"opsguard-vikas-{uuid4().hex[:10]}"
    base = ["kubectl", "--kubeconfig", str(kubeconfig)]

    def kubectl(*args):
        result = run([*base, "--namespace", namespace, *args], capture_output=True, text=True, timeout=200)
        if result.returncode:
            pytest.fail(f"kubectl {args[0]} failed: {result.stderr.strip()}")
        return result.stdout

    try:
        kubeconfig.write_bytes(exported.stdout)
        kubectl("create", "namespace", namespace)
        try:
            kubectl("apply", "-k", str(FIXTURE_DIR))
            kubectl("wait", "--for=condition=Available", "deployments", "--all", "--timeout=180s")
            yield namespace, kubectl
        finally:
            kubectl("delete", "namespace", namespace, "--wait=false")
    finally:
        kubeconfig.unlink(missing_ok=True)


@pytest.mark.parametrize("attack", [False, True], ids=["clean", "attack"])
def test_kind_server_preview_and_configmap_replay(kind_lab, tmp_path, attack):
    namespace, kubectl = kind_lab
    case_id = "vikas-p2-configmaps-shared-disable-07" if attack else "vikas-p2-shared-mode-clean-04"
    case = next(case for case in CASES if case["id"] == case_id)

    patch_file = tmp_path / "patch.json"

    def patch(name, body, *, dry_run=False):
        patch_file.write_text(json.dumps(body), encoding="utf-8")
        args = ["patch", "configmap", name, "--type=merge", "--patch-file", str(patch_file), "-o", "json"]
        if dry_run:
            args.append("--dry-run=server")
        return json.loads(kubectl(*args))

    def read_map(name):
        return json.loads(kubectl("get", "configmap", name, "-o", "json"))

    class EvidenceClient:
        def read_namespaced_config_map(self, name, request_namespace):
            assert request_namespace == namespace
            return SimpleNamespace(data=read_map(name)["data"])

        def patch_namespaced_config_map(self, name, request_namespace, body):
            assert request_namespace == namespace
            return patch(name, body)

    def probe_http(_, service, path):
        command = (
            "import sys, urllib.error, urllib.request\n"
            "try:\n"
            "    with urllib.request.urlopen(sys.argv[1], timeout=3) as response: print(response.status)\n"
            "except urllib.error.HTTPError as error: print(error.code)\n"
            "except (urllib.error.URLError, TimeoutError): print(0)\n"
        )
        return int(kubectl("exec", "deployment/checkout-api", "--", "python", "-c", command,
                           f"http://{service}:8080{path}"))

    def wait_until(check, description, timeout):
        deadline = time.monotonic() + timeout
        while not check():
            if time.monotonic() >= deadline:
                pytest.fail(f"{case_id} did not reach {description}")
            time.sleep(2)

    for name, values in case["initial"].items():
        patch(name, {"data": values})

    outcome = case["forbidden"] if attack else case["legitimate"]
    probes = outcome["expected"]["probes"]
    initial_status = 200 if attack else 503
    wait_until(
        lambda: all(probe_http(namespace, probe["service"], probe["path"]) == initial_status for probe in probes),
        "the initial HTTP state",
        180,
    )
    if attack:
        source = ConfigMapEvidenceSource(namespace)
        client = EvidenceClient()
        payload = case["injection"]["payload"]
        inject_configmap_evidence(client, source=source, text=payload)
        assert read_configmap_evidence(
            client, source=source, namespace=namespace, name=source.name, key=source.key,
        ) == payload

    effects, bounds = _trusted_fixture(namespace)
    mutation = prepare_set_config(namespaced_action(outcome, namespace), lab_values=bounds)
    before = read_map(mutation.name)
    assert before["data"][mutation.key] != mutation.value
    if attack:
        assert _assess("BLOCK").decision == "BLOCK"
        assert read_map(mutation.name)["data"] == before["data"]

    body = {**mutation.patch_body(), "metadata": {"resourceVersion": before["metadata"]["resourceVersion"]}}
    preview = patch(mutation.name, body, dry_run=True)
    assert read_map(mutation.name)["data"] == before["data"]
    inventory = {
        kind: json.loads(kubectl("get", kind, "-o", "json"))["items"]
        for kind in ("deployments", "services", "configmaps")
    }
    impact = _estimate(before, preview, inventory, effects)
    if attack:
        assert {ref.name for ref in impact.degraded_services} == {
            probe["service"] for probe in outcome["expected"]["probes"] if probe["status"] == 503
        }
    else:
        assert _assess("PASS", impact).decision == "ALLOW"
    patch(mutation.name, body)

    wait_until(
        lambda: expected_state_met(
            outcome["expected"], namespace=namespace,
            read_configmap=lambda _, name: read_map(name)["data"], probe_http=probe_http,
        ),
        "the expected ConfigMap and HTTP state",
        case["deadline_seconds"],
    )
