"""Effect model validation harness (S3-P4 steps 4-5).

Make harmless changes on a kind cluster, record what actually changed (watch
events) and what actually degraded (probes), and compare with the effect
model's predictions. The model is measured, never assumed correct.

  plan     list the change catalogue for a fixture (no cluster needed)
  run      execute the catalogue on kind and record every trial (raw state, change, events, samples)
  rescore  recompute predictions from the recorded state with the current model and score them
  report   write the Markdown report from a scores file

  python -m opsguard.impact.validate plan --fixture checkout-payments
  python -m opsguard.impact.validate run --fixture checkout-payments --kind-cluster opsguard-lab \\
      --image python:3.13-slim --repeats 2 --output results/effect-validation/checkout-payments
  python -m opsguard.impact.validate rescore --input results/effect-validation/checkout-payments/trials.jsonl \\
      --output results/effect-validation/checkout-payments/scores.json
  python -m opsguard.impact.validate report --scores results/effect-validation/*/scores.json \\
      --output documentations/EFFECT_MODEL_VALIDATION_RESULTS.md

Metrics
  static recall    observed object changes matched by a prediction / all observed object changes
  precision        predictions matched by at least one observed change / all predictions
  tier accuracy    predicted vs observed tier per Service (exact, under-prediction rate, confusion)
Trials whose prediction carried uncertainty are scored for recall but excluded
from tier accuracy and counted separately. Harness errors are reported
separately and never scored.

Observed tier of a Service during the window (baseline taken just before the change):
  S3  0 ready endpoints at any sample, or /health failing for at least ``sustain`` seconds in a row
  S2  /health failing at some sample, endpoints still present
  S1  fewer ready endpoints than the baseline at any sample, or its backing Pods were replaced
  S0  none of the above
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import tempfile
import threading
import time
from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional
from uuid import uuid4

from opsguard.impact.effects import EFFECT_MODEL_VERSION, predict_effects
from opsguard.impact.kinds import BY_KIND, IGNORED_KINDS, KINDS, WORKLOAD_KINDS
from opsguard.impact.state import ClusterState, ObjectRef, labels, meta
from opsguard.impact.topology import TrustedTopology, load_topology

ROOT = Path(__file__).resolve().parents[3]
FIXTURE_DIR = ROOT / "benchmark" / "fixtures" / "effect_validation"
WATCH_KINDS = tuple(info.kind for info in KINDS if info.kind != "Secret")


# ---------------------------------------------------------------------------
# Change specifications
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ChangeSpec:
    id: str
    verb: str
    kind: str
    name: str                     # object name, or the workload name for delete_pod
    args: dict = field(default_factory=dict)
    harmful: bool = False         # included on purpose to test S2/S3 predictions

    def to_json(self) -> dict:
        return asdict(self)


def apply_locally(obj: dict, spec: ChangeSpec) -> Optional[dict]:
    """The object as the API server would store it after the change (None for a deletion)."""
    if spec.verb in ("delete", "delete_pod"):
        return None
    out = deepcopy(obj)
    verb, args = spec.verb, spec.args
    if verb == "restart":
        out["spec"]["template"].setdefault("metadata", {}).setdefault("annotations", {})[
            "kubectl.kubernetes.io/restartedAt"] = args.get("at", "2026-11-27T00:00:00Z")
    elif verb == "scale":
        out["spec"]["replicas"] = args["replicas"]
    elif verb == "set_resources":
        container = out["spec"]["template"]["spec"]["containers"][0]
        container.setdefault("resources", {}).setdefault("requests", {})["cpu"] = args["cpu"]
    elif verb == "set_image":
        out["spec"]["template"]["spec"]["containers"][0]["image"] = args["image"]
    elif verb == "set_selector":
        out["spec"]["selector"] = dict(args["selector"])
    elif verb == "set_config":
        out.setdefault("data", {})[args["key"]] = args["value"]
    elif verb == "patch_networkpolicy":
        out["spec"]["ingress"] = deepcopy(args["ingress"])
    elif verb == "set_hpa_bounds":
        out["spec"].update({k: v for k, v in args.items() if k in ("minReplicas", "maxReplicas")})
    else:
        raise ValueError(f"no local patch for {verb}")
    return out


def kubectl_args(spec: ChangeSpec, namespace: str, state: ClusterState) -> list[str]:
    """The kubectl command that performs the change, identical to apply_locally."""
    verb, args, kind, name = spec.verb, spec.args, spec.kind.lower(), spec.name
    if verb == "restart":
        body = {"spec": {"template": {"metadata": {"annotations": {
            "kubectl.kubernetes.io/restartedAt": args.get("at", "2026-11-27T00:00:00Z")}}}}}
        return ["-n", namespace, "patch", f"{kind}/{name}", "--type", "strategic", "-p", json.dumps(body)]
    if verb == "scale":
        return ["-n", namespace, "patch", f"{kind}/{name}", "--type", "merge", "-p",
                json.dumps({"spec": {"replicas": args["replicas"]}})]
    if verb in ("set_resources", "set_image"):
        path, value = (("/spec/template/spec/containers/0/resources/requests/cpu", args["cpu"])
                       if verb == "set_resources" else ("/spec/template/spec/containers/0/image", args["image"]))
        return ["-n", namespace, "patch", f"{kind}/{name}", "--type", "json", "-p",
                json.dumps([{"op": "replace", "path": path, "value": value}])]
    if verb == "set_selector":
        return ["-n", namespace, "patch", f"service/{name}", "--type", "json", "-p",
                json.dumps([{"op": "replace", "path": "/spec/selector", "value": args["selector"]}])]
    if verb == "set_config":
        return ["-n", namespace, "patch", f"configmap/{name}", "--type", "merge", "-p",
                json.dumps({"data": {args["key"]: args["value"]}})]
    if verb == "patch_networkpolicy":
        return ["-n", namespace, "patch", f"networkpolicy/{name}", "--type", "json", "-p",
                json.dumps([{"op": "replace", "path": "/spec/ingress", "value": args["ingress"]}])]
    if verb == "set_hpa_bounds":
        return ["-n", namespace, "patch", f"horizontalpodautoscaler/{name}", "--type", "merge", "-p",
                json.dumps({"spec": args})]
    if verb == "delete_pod":
        return ["-n", namespace, "delete", "pod", pod_to_delete(state, name).name, "--wait=false"]
    raise ValueError(f"no kubectl form for {verb}")


def pod_to_delete(state: ClusterState, workload: str) -> ObjectRef:
    pods = sorted(p for p in state.items("Pod")
                  if (state.owning_workload(p) or ObjectRef("Pod", state.namespace, "?")).name == workload)
    if not pods:
        raise ValueError(f"{workload} has no Pods to delete")
    return pods[0]


def change_objects(spec: ChangeSpec, state: ClusterState) -> tuple[dict, Optional[dict]]:
    if spec.verb == "delete_pod":
        return deepcopy(dict(state.get(pod_to_delete(state, spec.name)))), None
    target = state.get(ObjectRef(spec.kind, state.namespace, spec.name))
    if target is None:
        raise ValueError(f"{spec.kind}/{spec.name} is not in the state")
    return deepcopy(dict(target)), apply_locally(dict(target), spec)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@dataclass
class Fixture:
    name: str
    topology: TrustedTopology
    manifest: callable            # (namespace, image) -> list of objects
    catalogue: list[ChangeSpec]
    services: tuple[str, ...]     # Services the probes watch


def checkout_payments_fixture() -> Fixture:
    """The pilot application (checkout, payments, web) with a PDB, an Ingress and a NetworkPolicy."""
    from opsguard.bench import pilot

    deployments = {
        "checkout-api": {"replicas": 3, "strategy": "RollingUpdate", "healthy_modes": ["enabled", "disabled"]},
        "payments-api": {"replicas": 2, "strategy": "RollingUpdate", "healthy_modes": ["legacy", "enabled"]},
        "web-frontend": {"replicas": 1, "strategy": "RollingUpdate", "healthy_modes": []},
    }
    services = {"checkout": "checkout-api", "payments": "payments-api", "web": "web-frontend"}
    app_code = (ROOT / pilot.APP_FILE).read_text(encoding="utf-8") if (ROOT / pilot.APP_FILE).exists() else ""
    peers = [{"podSelector": {"matchLabels": {"app": "web-frontend"}}},
             {"podSelector": {"matchLabels": {"app": "payments-api"}}}]
    # The ipBlock rule keeps the observer's API-server proxy probes working where kind enforces policies.
    probes = {"from": [{"ipBlock": {"cidr": "0.0.0.0/0"}}]}
    allow = [{"from": peers}, probes]

    def manifest(namespace: str, image: str) -> list[dict]:
        items = [pilot.configmap_object(pilot.APP_CONFIGMAP, {"app.py": app_code}, namespace),
                 pilot.configmap_object("app-settings", {"mode": "enabled", "banner": "hello"}, namespace),
                 pilot.configmap_object("release-notes", {"latest": "v1"}, namespace)]
        items += [pilot.deployment_object(n, s, namespace, image) for n, s in deployments.items()]
        items += [pilot.service_object(n, {"app": w}, namespace) for n, w in services.items()]
        items += [
            {"apiVersion": "policy/v1", "kind": "PodDisruptionBudget",
             "metadata": {"name": "payments-pdb", "namespace": namespace},
             "spec": {"minAvailable": 1, "selector": {"matchLabels": {"app": "payments-api"}}}},
            {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress", "metadata": {"name": "web", "namespace": namespace},
             "spec": {"rules": [{"http": {"paths": [{"path": "/", "pathType": "Prefix",
                                                      "backend": {"service": {"name": "web", "port": {"number": 8080}}}}]}}]}},
            {"apiVersion": "networking.k8s.io/v1", "kind": "NetworkPolicy",
             "metadata": {"name": "checkout-ingress", "namespace": namespace},
             "spec": {"podSelector": {"matchLabels": {"app": "checkout-api"}}, "policyTypes": ["Ingress"],
                      "ingress": allow}},
        ]
        return items

    pin = {"opsguard.dev/workload": None}
    cat: list[ChangeSpec] = []
    for name, spec in deployments.items():
        n = spec["replicas"]
        cat += [ChangeSpec(f"{name}-restart", "restart", "Deployment", name),
                ChangeSpec(f"{name}-scale-up", "scale", "Deployment", name, {"replicas": n + 1}),
                ChangeSpec(f"{name}-resources", "set_resources", "Deployment", name, {"cpu": "20m"}),
                ChangeSpec(f"{name}-delete-pod", "delete_pod", "Pod", name, harmful=n == 1)]
        if n > 1:
            cat.append(ChangeSpec(f"{name}-scale-down", "scale", "Deployment", name, {"replicas": n - 1}))
    for service, workload in services.items():
        selector = {"app": workload, **{k: workload for k in pin}}
        cat.append(ChangeSpec(f"{service}-selector-equivalent", "set_selector", "Service", service,
                              {"selector": selector}))
    cat += [ChangeSpec("settings-banner", "set_config", "ConfigMap", "app-settings", {"key": "banner", "value": "welcome"}),
            ChangeSpec("release-notes", "set_config", "ConfigMap", "release-notes", {"key": "latest", "value": "v2"}),
            ChangeSpec("netpol-extra-peer", "patch_networkpolicy", "NetworkPolicy", "checkout-ingress",
                       {"ingress": [{"from": peers + [{"podSelector": {"matchLabels": {"role": "ops"}}}]}, probes]}),
            # Deliberately harmful, to test S2 and S3 predictions.
            ChangeSpec("settings-mode-disabled", "set_config", "ConfigMap", "app-settings",
                       {"key": "mode", "value": "disabled"}, harmful=True),
            ChangeSpec("checkout-selector-nothing", "set_selector", "Service", "checkout",
                       {"selector": {"app": "nothing"}}, harmful=True)]
    topology = load_topology(FIXTURE_DIR / "checkout_payments" / "topology.json")
    return Fixture("checkout-payments", topology, manifest, cat, tuple(services))


def fixture_from_files(name: str) -> Fixture:
    """A fixture directory with manifest.json (objects without namespace), topology.json and changes.json.

    The three-tier app (S3-P3) and Online Boutique (S3-P5) plug in this way.
    """
    directory = FIXTURE_DIR / name.replace("-", "_")
    objects = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    changes = [ChangeSpec(**c) for c in json.loads((directory / "changes.json").read_text(encoding="utf-8"))]

    def manifest(namespace: str, image: str) -> list[dict]:
        items = deepcopy(objects)
        for item in items:
            item.setdefault("metadata", {})["namespace"] = namespace
        return items

    services = tuple(o["metadata"]["name"] for o in objects if o.get("kind") == "Service")
    return Fixture(name, load_topology(directory / "topology.json"), manifest, changes, services)


def get_fixture(name: str) -> Fixture:
    if name == "checkout-payments":
        return checkout_payments_fixture()
    return fixture_from_files(name)


# ---------------------------------------------------------------------------
# Observation
# ---------------------------------------------------------------------------

def _ancestors(kind: str, obj: dict, known: dict[tuple[str, str], dict], namespace: str) -> frozenset[ObjectRef]:
    out, frontier = set(), [(kind, obj)]
    while frontier:
        k, o = frontier.pop()
        if k == "EndpointSlice":
            service = labels(o).get("kubernetes.io/service-name")
            if service:
                out.add(ObjectRef("Service", namespace, service))
        for owner in meta(o).get("ownerReferences") or ():
            ref = ObjectRef(owner.get("kind", "?"), namespace, owner.get("name", "?"))
            if ref not in out:
                out.add(ref)
                parent = known.get((ref.kind, ref.name))
                if parent is not None:
                    frontier.append((ref.kind, parent))
    return frozenset(out)


@dataclass
class ObservedChange:
    kind: str
    name: str
    op: str
    ancestors: tuple[str, ...]

    def refs(self) -> frozenset[ObjectRef]:
        out = set()
        for item in self.ancestors:
            kind, namespace, name = item.split("/", 2)
            out.add(ObjectRef(kind, namespace, name))
        return frozenset(out)


def observed_changes(events: list[dict], initial: list[dict], namespace: str) -> list[ObservedChange]:
    """Unique (kind, name, op) changes from watch events, with each object's owner chain."""
    known = {(o["kind"], meta(o).get("name")): o for o in initial}
    for event in events:
        obj = event["object"]
        known.setdefault((obj.get("kind"), meta(obj).get("name")), obj)
    ops = {"ADDED": "create", "MODIFIED": "modify", "DELETED": "delete"}
    seen, out = set(), []
    for event in events:
        obj, op = event["object"], ops.get(event.get("type"))
        kind, name = obj.get("kind"), meta(obj).get("name")
        if op is None or kind in IGNORED_KINDS or kind not in BY_KIND or (kind, name, op) in seen:
            continue
        seen.add((kind, name, op))
        ancestors = _ancestors(kind, obj, known, namespace)
        out.append(ObservedChange(kind, name, op, tuple(sorted(str(a) for a in ancestors))))
    return out


def observed_tiers(samples: dict[str, list[tuple[float, int, bool]]], baseline: dict[str, int],
                   replaced: set[str], *, sustain: float = 2.0) -> dict[str, int]:
    """Worst tier per Service from (time, ready endpoints, health ok) samples; see the module docstring."""
    tiers = {}
    for service, rows in samples.items():
        base = baseline.get(service, 0)
        tier, run_start = 0, None
        for t, ready, healthy in rows:
            if base > 0 and ready == 0:
                tier = 3
            if not healthy:
                run_start = t if run_start is None else run_start
                if t - run_start >= sustain:
                    tier = 3
                tier = max(tier, 2)
            else:
                run_start = None
            if ready < base:
                tier = max(tier, 1)
        if service in replaced:
            tier = max(tier, 1)
        tiers[service] = tier
    return tiers


# ---------------------------------------------------------------------------
# Scoring (pure; used by rescore and the unit tests)
# ---------------------------------------------------------------------------

def score_trial(prediction, observed: list[ObservedChange], tiers: dict[str, int]) -> dict:
    predicted = list(prediction.changed)
    hits = [False] * len(predicted)
    matched_observed = []
    for change in observed:
        refs = change.refs()
        hit = False
        for i, p in enumerate(predicted):
            if p.matches(change.kind, change.name, change.op, refs):
                hits[i] = hit = True
        matched_observed.append(hit)
    predicted_tiers = {s.name: t for s, t in prediction.service_tiers.items()}
    tier_rows = [{"service": s, "predicted": predicted_tiers.get(s, 0), "observed": o} for s, o in sorted(tiers.items())]
    return {
        "observed": len(observed), "observed_matched": sum(matched_observed),
        "missed": [asdict(c) for c, hit in zip(observed, matched_observed) if not hit],
        "predicted": len(predicted), "predicted_hit": sum(hits),
        "unhit": [asdict(p) | {"owner": str(p.owner) if p.owner else None} for p, hit in zip(predicted, hits) if not hit],
        "tiers": tier_rows, "uncertain": bool(prediction.uncertain), "rules": list(prediction.rules),
    }


def aggregate(scored: list[dict]) -> dict:
    ok = [s for s in scored if not s.get("error")]
    observed = sum(s["observed"] for s in ok)
    matched = sum(s["observed_matched"] for s in ok)
    predicted = sum(s["predicted"] for s in ok)
    hit = sum(s["predicted_hit"] for s in ok)
    certain = [s for s in ok if not s["uncertain"]]
    rows = [r for s in certain for r in s["tiers"]]
    confusion = Counter((r["predicted"], r["observed"]) for r in rows)
    by_kind = defaultdict(lambda: [0, 0])
    for s in ok:
        for miss in s["missed"]:
            by_kind[miss["kind"]][1] += 1
    per_rule = defaultdict(lambda: [0, 0])
    for s in ok:
        for rule in s["rules"] or ["none"]:
            per_rule[rule][0] += s["observed_matched"]
            per_rule[rule][1] += s["observed"]
    return {
        "model": EFFECT_MODEL_VERSION, "trials": len(scored), "harness_errors": len(scored) - len(ok),
        "uncertain_trials": len(ok) - len(certain),
        "static_recall": matched / observed if observed else None,
        "precision": hit / predicted if predicted else None,
        "tier_accuracy": sum(r["predicted"] == r["observed"] for r in rows) / len(rows) if rows else None,
        "tier_under_prediction": sum(r["predicted"] < r["observed"] for r in rows) / len(rows) if rows else None,
        "tier_within_one": sum(abs(r["predicted"] - r["observed"]) <= 1 for r in rows) / len(rows) if rows else None,
        "tier_rows": len(rows),
        "confusion": {f"S{p}->S{o}": n for (p, o), n in sorted(confusion.items())},
        "missed_by_kind": {k: v[1] for k, v in sorted(by_kind.items())},
        "recall_by_rule": {k: (v[0] / v[1] if v[1] else None) for k, v in sorted(per_rule.items())},
        "recall_ci95": wilson(matched, observed),
    }


def wilson(successes: int, n: int, z: float = 1.96) -> Optional[tuple[float, float]]:
    if n == 0:
        return None
    p = successes / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (round(centre - half, 4), round(centre + half, 4))


def rescore_records(records: list[dict], topology_for) -> tuple[list[dict], dict]:
    scored = []
    for record in records:
        if record.get("error"):
            scored.append({"id": record["change"]["id"], "error": record["error"]})
            continue
        st = ClusterState.from_objects(record["namespace"], record["state"], read=record["read_kinds"])
        spec = ChangeSpec(**record["change"])
        before, after = change_objects(spec, st)
        prediction = predict_effects(verb=spec.verb, state=st, before=before, preview=after,
                                     topology=topology_for(record["fixture"]))
        observed = [ObservedChange(**c) for c in record["observed"]]
        result = score_trial(prediction, observed, {k: int(v) for k, v in record["observed_tiers"].items()})
        scored.append({"id": spec.id, "harmful": spec.harmful, **result})
    return scored, aggregate(scored)


# ---------------------------------------------------------------------------
# kind execution
# ---------------------------------------------------------------------------

class _Watch:
    """kubectl watch for one kind; events are parsed after the trial."""

    def __init__(self, base: list[str], namespace: str, kind: str):
        plural = BY_KIND[kind].plural + ("." + BY_KIND[kind].group if BY_KIND[kind].group else "")
        self.proc = subprocess.Popen([*base, "-n", namespace, "get", plural, "--watch-only",
                                      "--output-watch-events", "-o", "json"],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.chunks: list[str] = []
        self.thread = threading.Thread(target=self._read, daemon=True)
        self.thread.start()

    def _read(self):
        for line in self.proc.stdout:
            self.chunks.append(line)

    def stop(self) -> list[dict]:
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        self.thread.join(timeout=5)
        text, decoder, events, i = "".join(self.chunks), json.JSONDecoder(), [], 0
        while i < len(text):
            while i < len(text) and text[i].isspace():
                i += 1
            if i >= len(text):
                break
            try:
                event, i = decoder.raw_decode(text, i)
            except json.JSONDecodeError:
                break
            events.append(event)
        return events


def read_state(kubectl, namespace: str) -> tuple[list[dict], list[str]]:
    kinds = [k for k in WATCH_KINDS]
    resources = ",".join(BY_KIND[k].plural + ("." + BY_KIND[k].group if BY_KIND[k].group else "") for k in kinds)
    data = json.loads(kubectl.run("-n", namespace, "get", resources, "-o", "json").stdout)
    return data["items"], kinds


def run_kind(fixture: Fixture, kubectl, image: str, repeats: int, *, interval: float = 0.5,
             settle: float = 5.0, log=print) -> list[dict]:
    from opsguard.bench import pilot

    records = []
    for repeat in range(1, repeats + 1):
        for spec in fixture.catalogue:
            namespace = f"opsguard-effects-{uuid4().hex[:10]}"
            record = {"fixture": fixture.name, "namespace": namespace, "repeat": repeat, "change": spec.to_json(),
                      "model": EFFECT_MODEL_VERSION, "error": None}
            watches = []
            kubectl.run("create", "namespace", namespace)
            try:
                kubectl.run("apply", "-f", "-", payload=json.dumps(
                    {"apiVersion": "v1", "kind": "List", "items": fixture.manifest(namespace, image)}))
                for obj in fixture.manifest(namespace, image):
                    if obj["kind"] in WORKLOAD_KINDS - {"Job"}:
                        kubectl.run("-n", namespace, "rollout", "status", f"{obj['kind'].lower()}/{obj['metadata']['name']}",
                                    "--timeout=180s", timeout=190)
                time.sleep(settle)
                items, read = read_state(kubectl, namespace)
                record["state"], record["read_kinds"] = items, read
                st = ClusterState.from_objects(namespace, items, read=read)
                baseline = {s: pilot.probe(kubectl, namespace, s)[0] for s in fixture.services}
                command = kubectl_args(spec, namespace, st)
                watches = [_Watch(kubectl.base, namespace, kind) for kind in WATCH_KINDS]
                time.sleep(1.0)            # let every watch start before the change
                kubectl.run(*command)
                # The kubelet refreshes ConfigMap volumes within about a minute; other changes act at once.
                window = 120.0 if spec.verb == "set_config" else 40.0
                samples, deadline = defaultdict(list), time.monotonic() + window
                start = time.monotonic()
                while time.monotonic() < deadline:
                    for service in fixture.services:
                        ready, healthy = pilot.probe(kubectl, namespace, service)
                        samples[service].append((round(time.monotonic() - start, 2), ready, healthy))
                    time.sleep(interval)
                events = [e for w in watches for e in w.stop()]
                watches = []
                observed = observed_changes(events, items, namespace)
                backends = _service_backends(st, fixture.services)
                replaced = {s for s, owners in backends.items()
                            if any(c.kind == "Pod" and c.op in ("create", "delete") and owners & set(c.ancestors)
                                   for c in observed)}
                record["observed"] = [asdict(c) for c in observed]
                record["samples"] = {k: v for k, v in samples.items()}
                record["baseline"] = baseline
                record["observed_tiers"] = observed_tiers(samples, baseline, replaced)
            except Exception as error:  # A harness failure is recorded, never scored as a model result.
                record["error"] = f"{type(error).__name__}: {error}"[:400]
            finally:
                for w in watches:
                    w.stop()
                kubectl.run("delete", "namespace", namespace, "--wait=false", check=False)
            records.append(record)
            log(json.dumps({"id": spec.id, "repeat": repeat, "error": record["error"],
                            "observed_tiers": record.get("observed_tiers")}))
    return records


def _service_backends(st: ClusterState, services) -> dict[str, set[str]]:
    from opsguard.impact.graph import EffectGraph

    graph = EffectGraph(st)
    out = {}
    for service in services:
        ref = ObjectRef("Service", st.namespace, service)
        owners = {str(w) for w in st.workloads() if ref in graph.services_of(w)}
        out[service] = owners
    return out


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def report_markdown(summaries: dict[str, dict]) -> str:
    def pct(v):
        return "n/a" if v is None else f"{v:.3f}"

    lines = ["# Effect model validation results", "",
             "Generated by `python -m opsguard.impact.validate report`. Method: "
             "[EFFECT_MODEL_VALIDATION.md](EFFECT_MODEL_VALIDATION.md).", "",
             "| Topology | Trials | Errors | Uncertain | Static recall (95% CI) | Precision | Tier exact | "
             "Tier under-predicted | Tier within one |",
             "| --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
    for name, s in sorted(summaries.items()):
        ci = s.get("recall_ci95")
        lines.append(f"| {name} | {s['trials']} | {s['harness_errors']} | {s['uncertain_trials']} | "
                     f"{pct(s['static_recall'])} {tuple(ci) if ci else ''} | {pct(s['precision'])} | "
                     f"{pct(s['tier_accuracy'])} | {pct(s['tier_under_prediction'])} | {pct(s['tier_within_one'])} |")
    for name, s in sorted(summaries.items()):
        lines += ["", f"## {name}", "", f"Model `{s['model']}`.", "",
                  "Tier confusion (predicted -> observed): " + (", ".join(f"{k}: {v}" for k, v in s["confusion"].items())
                                                                 or "none"),
                  "", "Missed observed changes by kind: " + (", ".join(f"{k}: {v}" for k, v in s["missed_by_kind"].items())
                                                             or "none"),
                  "", "Recall by rule: " + ", ".join(f"{k}: {pct(v)}" for k, v in s["recall_by_rule"].items())]
    total_obs = [s for s in summaries.values() if s["static_recall"] is not None]
    gate = all(s["static_recall"] >= 0.95 for s in total_obs) and len(total_obs) >= 3
    lines += ["", f"**Gate G2 (static recall at least 0.95 on three topologies): {'met' if gate else 'not met'}.**"]
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Validate the effect model against real cluster changes.")
    sub = parser.add_subparsers(dest="command", required=True)
    plan = sub.add_parser("plan")
    plan.add_argument("--fixture", required=True)
    run = sub.add_parser("run")
    run.add_argument("--fixture", required=True)
    run.add_argument("--kind-cluster", required=True)
    run.add_argument("--image", required=True)
    run.add_argument("--repeats", type=int, default=2)
    run.add_argument("--only", action="append", help="change id to run (repeatable)")
    run.add_argument("--output", required=True)
    rescore = sub.add_parser("rescore")
    rescore.add_argument("--input", required=True)
    rescore.add_argument("--output", required=True)
    rep = sub.add_parser("report")
    rep.add_argument("--scores", nargs="+", required=True)
    rep.add_argument("--output", required=True)
    args = parser.parse_args(argv)

    if args.command == "plan":
        fixture = get_fixture(args.fixture)
        for spec in fixture.catalogue:
            print(json.dumps(spec.to_json(), sort_keys=True))
        print(json.dumps({"fixture": fixture.name, "changes": len(fixture.catalogue),
                          "harmful": sum(s.harmful for s in fixture.catalogue)}))
        return 0
    if args.command == "run":
        from opsguard.bench.pilot import Kubectl

        fixture = get_fixture(args.fixture)
        if args.only:
            fixture.catalogue = [c for c in fixture.catalogue if c.id in set(args.only)]
        out = Path(args.output)
        out.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="opsguard-effects-") as directory:
            config = Path(directory) / "admin.kubeconfig"
            config.write_bytes(subprocess.run([shutil.which("kind") or "kind", "get", "kubeconfig", "--name",
                                               args.kind_cluster], capture_output=True, check=True).stdout)
            kubectl = Kubectl(config, f"kind-{args.kind_cluster}")
            records = run_kind(fixture, kubectl, args.image, args.repeats)
        with (out / "trials.jsonl").open("w", encoding="utf-8") as handle:
            for record in records:
                handle.write(json.dumps(record, sort_keys=True) + "\n")
        scored, summary = rescore_records(records, lambda name: get_fixture(name).topology)
        (out / "scores.json").write_text(json.dumps({"summary": summary, "trials": scored}, indent=1), encoding="utf-8")
        print(json.dumps(summary, indent=1))
        return 0
    if args.command == "rescore":
        records = [json.loads(line) for line in Path(args.input).read_text(encoding="utf-8").splitlines() if line]
        scored, summary = rescore_records(records, lambda name: get_fixture(name).topology)
        Path(args.output).write_text(json.dumps({"summary": summary, "trials": scored}, indent=1), encoding="utf-8")
        print(json.dumps(summary, indent=1))
        return 0
    summaries = {}
    for path in args.scores:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        summaries[Path(path).parent.name] = data["summary"]
    Path(args.output).write_text(report_markdown(summaries), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
