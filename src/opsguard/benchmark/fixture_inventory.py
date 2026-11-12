"""Build ownership and routing objects for fake Kubernetes backends only."""

from copy import deepcopy

from opsguard.intent.inventory import InventorySnapshot


def fixture_inventory(namespace: str, deployments: dict[str, dict], services: dict[str, str]) -> InventorySnapshot:
    pods, replicasets, service_objects, slices = [], [], [], []
    for name, deployment in deployments.items():
        uid = deployment["metadata"]["uid"]
        replicasets.append({"metadata": {"namespace": namespace, "name": name + "-rs", "uid": uid + "-rs",
            "ownerReferences": [{"kind": "Deployment", "name": name, "uid": uid, "controller": True}]}})
        if deployment["spec"].get("replicas", 1) > 0:
            pods.append({"metadata": {"namespace": namespace, "name": name + "-pod", "uid": uid + "-pod",
                "labels": deployment["spec"]["template"]["metadata"]["labels"],
                "ownerReferences": [{"kind": "ReplicaSet", "name": name + "-rs", "uid": uid + "-rs", "controller": True}]},
                "status": {"phase": "Running"}})
    for service, name in services.items():
        deployment = deployments[name]
        service_uid = service + "-service"
        service_objects.append({"metadata": {"namespace": namespace, "name": service, "uid": service_uid},
            "spec": {"selector": deployment["spec"]["template"]["metadata"]["labels"]}})
        slices.append({"metadata": {"namespace": namespace, "name": service + "-slice",
            "labels": {"kubernetes.io/service-name": service},
            "ownerReferences": [{"kind": "Service", "name": service, "uid": service_uid, "controller": True}]},
            "endpoints": [{"conditions": {"ready": deployment.get("status", {}).get("readyReplicas", 1) > 0}, "targetRef": {
                "kind": "Pod", "namespace": namespace, "name": name + "-pod",
                "uid": deployment["metadata"]["uid"] + "-pod"}}]
                if deployment["spec"].get("replicas", 1) > 0 else []})
    return InventorySnapshot(namespace=namespace, deployments=tuple(deepcopy(list(deployments.values()))),
        services=tuple(deepcopy(service_objects)), pods=tuple(deepcopy(pods)),
        replicasets=tuple(deepcopy(replicasets)), endpointslices=tuple(deepcopy(slices)))
