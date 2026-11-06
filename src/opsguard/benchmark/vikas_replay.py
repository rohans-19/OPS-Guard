"""Scripted lab backend and trusted ConfigMap fixture metadata."""

import json
import runpy
from pathlib import Path
from tempfile import TemporaryDirectory

from opsguard.impact.effects import ConfigEffect
from opsguard.impact.graph import ResourceRef


SERVICE_CONFIG = {"checkout": "shared-features", "storefront": "shared-features", "decoy": "decoy-features"}


def fixture_rules(root, namespace):
    path = root / "benchmark/fixtures/vikas/impact_topology.json"
    effects, bounds = [], {}
    for rule in json.loads(path.read_text(encoding="utf-8")):
        configmap = ResourceRef("configmap", namespace, rule["configmap"])
        consumers = tuple(ResourceRef("workload", namespace, name) for name in rule["workloads"])
        healthy, degraded = frozenset({rule["healthy"]}), frozenset({rule["degraded"]})
        effects.append(ConfigEffect(configmap, rule["key"], consumers, degraded, healthy))
        bounds[namespace, configmap.name, rule["key"]] = healthy | degraded
    return effects, bounds


class ScriptedReplay:
    """Fresh temporary ConfigMap files using the same response logic as the kind fixture."""

    namespace = "vikas-scripted"

    def __init__(self, case, app_response, work_dir):
        self.case = case
        self.app_response = app_response
        self.work_dir = work_dir

    def __enter__(self):
        self.temp = TemporaryDirectory(prefix="replay-", dir=self.work_dir)
        self.path = Path(self.temp.name)
        try:
            for name, values in self.case["initial"].items():
                folder = self.path / name
                folder.mkdir()
                for key, value in values.items():
                    (folder / key).write_text(value, encoding="utf-8")
        except BaseException:
            self.temp.cleanup()
            raise
        return self

    def __exit__(self, *exc):
        self.temp.cleanup()

    def read_configmap(self, namespace, name):
        self._check_namespace(namespace)
        return {path.name: path.read_text(encoding="utf-8") for path in (self.path / name).iterdir()}

    def probe_http(self, namespace, service, path):
        self._check_namespace(namespace)
        return self.app_response(service, self.path / SERVICE_CONFIG[service], path)[0]

    def _check_namespace(self, namespace):
        if namespace != self.namespace:
            raise ValueError("replay namespace mismatch")

    def _configmap(self, name):
        return {
            "kind": "ConfigMap",
            "metadata": {"name": name, "namespace": self.namespace},
            "data": self.read_configmap(self.namespace, name),
        }

    def inventory(self):
        deployments, services = [], []
        for service, configmap in SERVICE_CONFIG.items():
            workload = f"{service}-api"
            labels = {"app": workload}
            deployments.append({
                "metadata": {"name": workload, "namespace": self.namespace},
                "spec": {"replicas": 1, "template": {
                    "metadata": {"labels": labels},
                    "spec": {"volumes": [{"configMap": {"name": configmap}}]},
                }},
            })
            services.append({
                "metadata": {"name": service, "namespace": self.namespace},
                "spec": {"selector": labels},
            })
        return {"deployments": deployments, "services": services,
                "configmaps": [self._configmap(name) for name in self.case["initial"]]}

    def preview(self, mutation):
        self._check_namespace(mutation.namespace)
        before = self._configmap(mutation.name)
        return before, {**before, "data": {**before["data"], **mutation.patch_body()["data"]}}

    project = preview

    def execute(self, mutation):
        self._check_namespace(mutation.namespace)
        for key, value in mutation.patch_body()["data"].items():
            (self.path / mutation.name / key).write_text(value, encoding="utf-8")

    def read_configmap_evidence(self):
        return self.case["injection"]["payload"]


def scripted_response(root):
    return runpy.run_path(str(root / "benchmark/fixtures/vikas/app.py"))["response_for_path"]
