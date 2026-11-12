"""Deterministic executor fixture and independent restart-state observation."""

from copy import deepcopy

import httpx

from opsguard.benchmark.fixture_inventory import fixture_inventory
from opsguard.operations.restart import RESTART_ANNOTATION


def rejected(code: int):
    return httpx.HTTPStatusError("fixture API rejected operation", request=httpx.Request("PATCH", "https://127.0.0.1"),
                                response=httpx.Response(code))


class MemoryExecutionLab:
    namespace = "restart-lab"

    def __init__(self, workloads):
        self.workloads = deepcopy(workloads)
        self.deployments = {}
        self.calls = []
        for name, fixture in workloads.items():
            replicas = fixture.get("replicas", 1)
            self.deployments[name] = {
                "metadata": {"namespace": self.namespace, "name": name, "uid": name + "-uid",
                             "resourceVersion": "1", "generation": 1},
                "spec": {"replicas": replicas, "template": {"metadata": {"labels": {"app": name},
                    "annotations": {"fixture-note": "preserve"}}, "spec": {"containers": [
                        {"name": "app", "image": "opsguard/app:v1"}, {"name": "sidecar", "image": "opsguard/sidecar:v1"}]}}},
                "status": {"observedGeneration": 1, "readyReplicas": replicas, "updatedReplicas": replicas, "availableReplicas": replicas},
            }

    async def inventory(self, *, timeout_seconds):
        return fixture_inventory(self.namespace, self.deployments,
                                 {fixture["service"]: name for name, fixture in self.workloads.items()})

    async def send(self, mutation, *, dry_run, timeout_seconds):
        if mutation.target.namespace != self.namespace or mutation.target.name not in self.deployments:
            raise rejected(404)
        original = self.deployments[mutation.target.name]
        meta = original["metadata"]
        if (meta["uid"], meta["resourceVersion"]) != (mutation.target.uid, mutation.target.resource_version):
            raise rejected(409)
        obj = deepcopy(original)
        body = mutation.body()
        if mutation.operation == "scale":
            obj["spec"]["replicas"] = body["spec"]["replicas"]
        else:
            # Apply the generated JSON Patch atomically to a copy. No policy or
            # case labels enter this fixture API implementation.
            for operation in body:
                segments = [part.replace("~1", "/").replace("~0", "~") for part in operation["path"].split("/")[1:]]
                parent = obj
                try:
                    for segment in segments[:-1]:
                        parent = parent[int(segment)] if isinstance(parent, list) else parent[segment]
                    key = int(segments[-1]) if isinstance(parent, list) else segments[-1]
                    if operation["op"] == "test":
                        if parent[key] != operation["value"]:
                            raise rejected(409)
                    elif operation["op"] in ("add", "replace"):
                        if operation["op"] == "replace" and key not in parent and not isinstance(parent, list):
                            raise rejected(422)
                        parent[key] = deepcopy(operation["value"])
                    else:
                        raise rejected(422)
                except (KeyError, IndexError, TypeError, ValueError):
                    raise rejected(422) from None
        self.calls.append((dry_run, mutation))
        if obj["spec"] != original["spec"]:
            obj["metadata"]["generation"] += 1
            obj["metadata"]["resourceVersion"] = str(int(meta["resourceVersion"]) + 1)
        if not dry_run:
            replicas = obj["spec"]["replicas"]
            obj["status"] = {"observedGeneration": obj["metadata"]["generation"], "readyReplicas": replicas,
                             "updatedReplicas": replicas, "availableReplicas": replicas}
            self.deployments[mutation.target.name] = obj
        return deepcopy(obj)


class MemoryRestartObserver:
    def __init__(self, lab):
        self.lab = lab

    async def snapshot(self, *, timeout_seconds):
        values = {}
        for name, dep in self.lab.deployments.items():
            meta, spec, status = dep["metadata"], dep["spec"], dep["status"]
            values[name] = {"uid": meta["uid"], "generation": meta["generation"],
                "marker": spec["template"]["metadata"].get("annotations", {}).get(RESTART_ANNOTATION),
                "pod_uids": [f'{meta["uid"]}-{meta["generation"]}-{index}' for index in range(spec["replicas"])],
                "ready": spec["replicas"] > 0 and status["readyReplicas"] == spec["replicas"]
                         and status["observedGeneration"] >= meta["generation"],
                "http_ok": spec["replicas"] > 0 and status["readyReplicas"] == spec["replicas"]}
        return values


def restarted(before, after):
    return (before["uid"] == after["uid"] and after["generation"] > before["generation"]
            and after["marker"] is not None and before["marker"] != after["marker"])


def restart_complete(before, after):
    return (restarted(before, after) and bool(after["pod_uids"])
            and set(before["pod_uids"]).isdisjoint(after["pod_uids"]) and after["ready"] and after["http_ok"])
