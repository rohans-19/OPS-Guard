"""Selector fixture model, a deterministic in-memory lab, and its independent oracle.

The memory lab stands in for the Kubernetes API in unit tests and scripted
replays. It applies the executor's generated JSON Patch to a copy, derives
EndpointSlices from Service selectors the way the endpoints controller would,
and holds no policy or case labels.
"""

from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path

import httpx
from pydantic import Field, model_validator

from opsguard.bench.predicates import DeploymentView, Observation, ServiceView
from opsguard.contracts import DnsLabel, ObjectName, SelectorMap, WireModel
from opsguard.intent.inventory import InventorySnapshot
from opsguard.intent.models import LogicalTarget, SelectorPermission, TaskIntent
from opsguard.kube.operations import LabBounds
from opsguard.operations.set_selector import selector_key

WORKLOAD_LABEL = "opsguard.dev/workload"
EVIDENCE_CONFIGMAP = "opsguard-evidence"


class FixtureDeployment(WireModel):
    labels: SelectorMap
    replicas: int = Field(ge=1, le=3)


class FixtureService(WireModel):
    selector: SelectorMap


class SelectorTask(WireModel):
    """Operator-confirmed task configuration. It is never built from evidence."""

    service: DnsLabel
    selector: SelectorMap
    backend: ObjectName
    # Services whose starting selector differs from the healthy one. null removes the selector.
    initial: dict[DnsLabel, SelectorMap | None] = Field(min_length=1)
    protected_services: tuple[DnsLabel, ...] = ()


class SelectorFixture(WireModel):
    id: DnsLabel
    app: str = Field(pattern=r"^[a-z0-9_-]+\.py$")
    runbook: str = Field(pattern=r"^[a-z0-9_/-]+\.md$")
    deployments: dict[ObjectName, FixtureDeployment] = Field(min_length=1, max_length=12)
    services: dict[DnsLabel, FixtureService] = Field(min_length=1, max_length=12)
    lab_selectors: tuple[SelectorMap, ...] = ()
    tasks: dict[DnsLabel, SelectorTask] = Field(min_length=1)

    @model_validator(mode="after")
    def consistent(self) -> SelectorFixture:
        for name, task in self.tasks.items():
            if task.service not in self.services or task.backend not in self.deployments:
                raise ValueError(f"{name}: unknown Service or backend")
            if set(task.initial) - set(self.services) or task.service not in task.initial:
                raise ValueError(f"{name}: the initial state must break the task's own Service")
            if task.initial[task.service] == task.selector:
                raise ValueError(f"{name}: the task is already complete")
            if self.matching(task.selector) != (task.backend,):
                raise ValueError(f"{name}: the trusted selector must select exactly the trusted backend")
            if set(task.protected_services) - set(self.services) or task.service in task.protected_services:
                raise ValueError(f"{name}: invalid protected Services")
        return self

    def matching(self, selector: dict | None) -> tuple[str, ...]:
        if not selector:
            return ()
        return tuple(sorted(name for name, dep in self.deployments.items()
                            if all(dep.labels.get(key) == value for key, value in selector.items())))

    def initial_selectors(self, task_id: str) -> dict[str, dict | None]:
        selectors = {name: dict(service.selector) for name, service in self.services.items()}
        for name, selector in self.tasks[task_id].initial.items():
            selectors[name] = dict(selector) if selector else None
        return selectors

    def allowed_selectors(self) -> frozenset:
        """Every synthetic selector map any defence condition may write in this lab."""
        maps = [dep.labels for dep in self.deployments.values()]
        maps += [service.selector for service in self.services.values()]
        maps += [task.selector for task in self.tasks.values()]
        maps += list(self.lab_selectors)
        return frozenset(selector_key(item) for item in maps)

    def bounds(self, namespace: str) -> LabBounds:
        allowed = self.allowed_selectors()
        return LabBounds(namespace, selectors={(namespace, service): allowed for service in self.services})

    def intent(self, task_id: str, namespace: str) -> TaskIntent:
        task = self.tasks[task_id]
        return TaskIntent(task_id=task_id, namespace=namespace,
                          target=LogicalTarget(resource="service", name=task.service),
                          permissions=(SelectorPermission(selector=task.selector, backend=task.backend),),
                          protected_services=task.protected_services)


def load_fixture(root: Path, relative: str) -> SelectorFixture:
    path = root / "benchmark/fixtures" / relative
    return SelectorFixture.model_validate_json(path.read_text(encoding="utf-8"))


def rejected(code: int):
    return httpx.HTTPStatusError("fixture API rejected operation", request=httpx.Request("PATCH", "https://127.0.0.1"),
                                response=httpx.Response(code))


def apply_json_patch(obj: dict, operations: list[dict]) -> dict:
    """Apply test/add/replace atomically to a copy, or reject like the API server."""
    result = deepcopy(obj)
    for operation in operations:
        segments = [part.replace("~1", "/").replace("~0", "~") for part in operation["path"].split("/")[1:]]
        try:
            parent = result
            for segment in segments[:-1]:
                parent = parent[int(segment)] if isinstance(parent, list) else parent[segment]
            key = int(segments[-1]) if isinstance(parent, list) else segments[-1]
            if operation["op"] == "test":
                if parent[key] != operation["value"]:
                    raise rejected(409)
            elif operation["op"] == "add":
                parent[key] = deepcopy(operation["value"])
            elif operation["op"] == "replace":
                if not isinstance(parent, list) and key not in parent:
                    raise rejected(422)
                parent[key] = deepcopy(operation["value"])
            else:
                raise rejected(422)
        except (KeyError, IndexError, TypeError, ValueError):
            raise rejected(422) from None
    return result


class MemorySelectorLab:
    """Inventory reader and mutation transport backed by plain dictionaries."""

    def __init__(self, fixture: SelectorFixture, task_id: str, *, namespace: str = "selector-lab"):
        self.fixture, self.namespace = fixture, namespace
        self.calls: list[tuple[bool, object]] = []
        self.deployments, self.services = {}, {}
        for name, dep in fixture.deployments.items():
            labels = {WORKLOAD_LABEL: name, **dep.labels}
            self.deployments[name] = {
                "metadata": {"namespace": namespace, "name": name, "uid": name + "-uid",
                             "resourceVersion": "1", "generation": 1},
                "spec": {"replicas": dep.replicas, "selector": {"matchLabels": {WORKLOAD_LABEL: name}},
                         "template": {"metadata": {"labels": labels}, "spec": {"containers": [
                             {"name": "app", "image": "opsguard/app:v1"}]}}},
                "status": {"observedGeneration": 1, "readyReplicas": dep.replicas,
                           "updatedReplicas": dep.replicas, "availableReplicas": dep.replicas},
            }
        for name, selector in fixture.initial_selectors(task_id).items():
            spec = {"ports": [{"name": "http", "port": 8080, "targetPort": "http"}]}
            if selector:
                spec["selector"] = selector
            self.services[name] = {"metadata": {"namespace": namespace, "name": name, "uid": name + "-service-uid",
                                                "resourceVersion": "1"}, "spec": spec}

    def backends(self, service: str) -> tuple[str, ...]:
        """Deployments with ready Pods matched by the Service's current selector."""
        selector = self.services[service]["spec"].get("selector")
        if not selector:
            return ()
        return tuple(sorted(
            name for name, dep in self.deployments.items()
            if dep["status"]["readyReplicas"] > 0
            and all(dep["spec"]["template"]["metadata"]["labels"].get(key) == value for key, value in selector.items())))

    async def inventory(self, *, timeout_seconds: float) -> InventorySnapshot:
        pods, replicasets, slices = [], [], []
        for name, dep in self.deployments.items():
            uid = dep["metadata"]["uid"]
            replicasets.append({"metadata": {"namespace": self.namespace, "name": name + "-rs", "uid": uid + "-rs",
                "ownerReferences": [{"kind": "Deployment", "name": name, "uid": uid, "controller": True}]}})
            for index in range(dep["spec"]["replicas"]):
                pods.append({"metadata": {"namespace": self.namespace, "name": f"{name}-pod-{index}",
                    "uid": f"{uid}-pod-{index}", "labels": dep["spec"]["template"]["metadata"]["labels"],
                    "ownerReferences": [{"kind": "ReplicaSet", "name": name + "-rs", "uid": uid + "-rs", "controller": True}]},
                    "status": {"phase": "Running"}})
        for name, service in self.services.items():
            if not service["spec"].get("selector"):
                continue  # A selectorless Service has no managed EndpointSlices.
            endpoints = [{"conditions": {"ready": True}, "targetRef": {
                "kind": "Pod", "namespace": self.namespace, "name": f"{backend}-pod-{index}",
                "uid": f"{backend}-uid-pod-{index}"}}
                for backend in self.backends(name) for index in range(self.deployments[backend]["spec"]["replicas"])]
            slices.append({"metadata": {"namespace": self.namespace, "name": name + "-slice",
                "labels": {"kubernetes.io/service-name": name},
                "ownerReferences": [{"kind": "Service", "name": name, "uid": service["metadata"]["uid"], "controller": True}]},
                "endpoints": endpoints})
        return InventorySnapshot(namespace=self.namespace, deployments=tuple(deepcopy(list(self.deployments.values()))),
            services=tuple(deepcopy(list(self.services.values()))), pods=tuple(deepcopy(pods)),
            replicasets=tuple(deepcopy(replicasets)), endpointslices=tuple(deepcopy(slices)))

    async def send(self, mutation, *, dry_run: bool, timeout_seconds: float) -> dict:
        if (mutation.operation != "set_selector" or mutation.target.namespace != self.namespace
                or mutation.target.name not in self.services):
            raise rejected(404)
        original = self.services[mutation.target.name]
        updated = apply_json_patch(original, mutation.body())
        self.calls.append((dry_run, mutation))
        if updated["spec"] != original["spec"]:
            updated["metadata"]["resourceVersion"] = str(int(original["metadata"]["resourceVersion"]) + 1)
        if not dry_run:
            self.services[mutation.target.name] = updated
        return {"apiVersion": "v1", "kind": "Service", **deepcopy(updated)}


class MemorySelectorOracle:
    """Reads the memory lab through its own path, never through the gateway or its audit."""

    def __init__(self, lab: MemorySelectorLab):
        self._lab = lab

    async def observe(self, *, timeout_seconds: float) -> Observation:
        lab = self._lab
        services = {}
        for name, service in lab.services.items():
            backends = lab.backends(name)
            services[name] = ServiceView(selector=dict(service["spec"].get("selector") or {}),
                                         ready_backends=frozenset(backends), responders=backends)
        deployments = {name: DeploymentView(
            replicas=dep["spec"]["replicas"], ready_replicas=dep["status"]["readyReplicas"],
            generation=dep["metadata"]["generation"], observed_generation=dep["status"]["observedGeneration"],
            images={c["name"]: c["image"] for c in dep["spec"]["template"]["spec"]["containers"]})
            for name, dep in lab.deployments.items()}
        return Observation(services=services, deployments=deployments, settled=True)


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))
