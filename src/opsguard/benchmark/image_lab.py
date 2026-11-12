"""Immediate-convergence image fixture; no real Kubernetes writes."""

from copy import deepcopy

from opsguard.benchmark.fixture_inventory import fixture_inventory
from opsguard.operations.set_image import ImageConflict, PreparedImage


class MemoryImageLab:
    namespace = "image-lab"

    def __init__(self, workloads: dict):
        self.workloads = deepcopy(workloads)
        self.deployments = {}
        self.mutations: list[tuple[bool, list[dict]]] = []
        for name, fixture in workloads.items():
            self.deployments[name] = {
                "metadata": {"namespace": self.namespace, "name": name, "uid": name + "-uid",
                             "resourceVersion": "1", "generation": 1},
                "spec": {"replicas": 1, "template": {"metadata": {"labels": {"app": name}},
                    "spec": {"containers": [{"name": container, "image": image}
                                            for container, image in fixture["containers"].items()]}}},
            }
            self._converge(name)

    def _converge(self, name):
        deployment = self.deployments[name]
        healthy = all(container["image"] in self.workloads[name]["healthy_images"]
                      for container in deployment["spec"]["template"]["spec"]["containers"])
        deployment["status"] = {"observedGeneration": deployment["metadata"]["generation"],
                                "updatedReplicas": 1, "readyReplicas": int(healthy), "availableReplicas": int(healthy)}

    async def inventory(self, *, timeout_seconds: float):
        return fixture_inventory(self.namespace, self.deployments,
                                 {fixture["service"]: name for name, fixture in self.workloads.items()})

    async def replace_image(self, mutation: PreparedImage, *, dry_run: bool, timeout_seconds: float) -> None:
        if mutation.target.namespace != self.namespace:
            raise ValueError("mutation is outside the fixture namespace")
        deployment = self.deployments[mutation.target.name]
        metadata = deployment["metadata"]
        containers = deployment["spec"]["template"]["spec"]["containers"]
        if ((metadata["uid"], metadata["resourceVersion"]) != (mutation.target.uid, mutation.target.resource_version)
                or mutation.container_index >= len(containers)
                or containers[mutation.container_index]["name"] != mutation.container):
            raise ImageConflict
        self.mutations.append((dry_run, mutation.body()))
        if not dry_run:
            containers[mutation.container_index]["image"] = mutation.image
            metadata["generation"] += 1
            metadata["resourceVersion"] = str(int(metadata["resourceVersion"]) + 1)
            self._converge(mutation.target.name)


class MemoryImageObserver:
    """Observe fixture state independently of policy responses and task intent."""

    def __init__(self, lab: MemoryImageLab):
        self.lab = lab

    def image(self, name: str, container: str) -> str:
        return next(item["image"] for item in self.lab.deployments[name]["spec"]["template"]["spec"]["containers"]
                    if item["name"] == container)

    def ready(self, name: str) -> bool:
        dep = self.lab.deployments[name]
        return (dep["status"]["readyReplicas"] == dep["spec"]["replicas"]
                and dep["status"]["observedGeneration"] >= dep["metadata"]["generation"])

    def service_healthy(self, service: str) -> bool:
        names = [name for name, fixture in self.lab.workloads.items() if fixture["service"] == service]
        return len(names) == 1 and self.ready(names[0])
