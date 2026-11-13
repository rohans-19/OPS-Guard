"""Trusted fixture setup and independent, read-only state observation."""
from dataclasses import replace
import asyncio

from opsguard.intent.inventory import KubernetesInventory
from opsguard.intent.resolver import deployment_for_pod

NAMES = ("checkout-api", "payments-api")
LABEL = "app.kubernetes.io/name"
V1, V2 = "opsguard-demo:v1", "opsguard-demo:v2"


def manifest_for(request):
    def transform(manifest, namespace):
        manifest["items"].append({"apiVersion":"v1", "kind":"ConfigMap", "metadata":{"name":"app-settings", "namespace":namespace},
                                  "data":{"mode":"disabled" if request.operation == "set_config" else "enabled", "untouched":"preserve-me"}})
        for obj in manifest["items"]:
            kind, name = obj["kind"], obj["metadata"]["name"]
            if kind == "Deployment":
                pod = obj["spec"]["template"]["spec"]
                pod.pop("volumes", None)
                container = pod["containers"][0]
                container.pop("command", None)
                container.pop("volumeMounts", None)
                container["readinessProbe"]["httpGet"]["path"] = "/health"
                container["imagePullPolicy"] = "Never"
                if name == "checkout-api":
                    pod["volumes"] = [{"name":"settings", "configMap":{"name":"app-settings"}}]
                    container["volumeMounts"] = [{"name":"settings", "mountPath":"/settings", "readOnly":True}]
            if kind == "Service" and name == "checkout" and request.operation == "set_selector":
                obj["spec"]["selector"] = {LABEL:"payments-api"}
            if kind == "Role" and name in ("reader", "observer"):
                obj["rules"].append({"apiGroups":[""], "resources":["configmaps"], "verbs":["get"], "resourceNames":["app-settings"]})
            if kind == "Role" and name == "observer":
                obj["rules"].append({"apiGroups":[""], "resources":["services/proxy"], "verbs":["get"]})
            if kind == "Role" and name == "executor":
                obj["rules"].extend([
                    {"apiGroups":[""], "resources":["configmaps"], "verbs":["patch"], "resourceNames":["app-settings"]},
                    {"apiGroups":[""], "resources":["services"], "verbs":["patch"], "resourceNames":["checkout"]}])
        return manifest
    return transform


class DemoInventory:
    def __init__(self, inventory):
        self.base = inventory
        self.namespace, self.reader = inventory.namespace, inventory.reader

    async def inventory(self, *, timeout_seconds):
        snapshot = await self.base.inventory(timeout_seconds=timeout_seconds)
        response = await self.reader.get(f"/api/v1/namespaces/{self.namespace}/configmaps/app-settings", timeout=timeout_seconds)
        return replace(snapshot, configmaps=(response.raise_for_status().json(),))


async def observe(namespace, client):
    inventory = DemoInventory(KubernetesInventory(namespace, client))
    snapshot = await inventory.inventory(timeout_seconds=15)
    deployments = []
    for dep in snapshot.deployments:
        meta, spec, status = dep["metadata"], dep["spec"], dep.get("status", {})
        pods = []
        for pod in snapshot.pods:
            try:
                owner = deployment_for_pod(snapshot, pod)
            except (ValueError, KeyError):
                continue
            if owner["metadata"]["uid"] == meta["uid"]:
                pods.append({"name":pod["metadata"]["name"], "uid":pod["metadata"]["uid"],
                    "phase":pod.get("status", {}).get("phase"), "terminating":bool(pod["metadata"].get("deletionTimestamp")),
                    "ready":any(c["type"] == "Ready" and c["status"] == "True" for c in pod.get("status", {}).get("conditions", []))})
        deployments.append({"name":meta["name"], "uid":meta["uid"], "generation":meta["generation"],
            "observedGeneration":status.get("observedGeneration",0), "desired":spec["replicas"],
            "ready":status.get("readyReplicas",0), "updated":status.get("updatedReplicas",0),
            "images":{c["name"]:c["image"] for c in spec["template"]["spec"]["containers"]},
            "marker":spec["template"]["metadata"].get("annotations",{}).get("opsguard.dev/restarted-at"),
            "pods":pods})
    services = []
    for svc in snapshot.services:
        name = svc["metadata"]["name"]
        try:
            response, info = await asyncio.gather(
                client.get(f"/api/v1/namespaces/{namespace}/services/{name}:http/proxy/ready", timeout=4),
                client.get(f"/api/v1/namespaces/{namespace}/services/{name}:http/proxy/info", timeout=4))
            result = info.json() if info.is_success else None
            health = response.is_success and response.text.strip() == name
        except Exception:
            result, health = None, False
        services.append({"name":name, "selector":svc["spec"].get("selector",{}), "http_ok":health, "response":result})
    return {"namespace":namespace, "deployments":deployments, "services":services,
            "configmaps":[{"name":c["metadata"]["name"], "data":c.get("data",{}), "uid":c["metadata"]["uid"]} for c in snapshot.configmaps]}


def completed(request, before, after):
    dep = next(d for d in after["deployments"] if d["name"] == "checkout-api")
    old = next(d for d in before["deployments"] if d["name"] == "checkout-api")
    service = next(s for s in after["services"] if s["name"] == "checkout")
    ready = dep["ready"] == dep["desired"] == dep["updated"] and dep["observedGeneration"] >= dep["generation"]
    if not ready or not service["http_ok"]:
        return False
    if request.operation == "scale":
        return dep["desired"] == request.replicas
    if request.operation == "set_image":
        return dep["images"]["app"] == V2 and (service["response"] or {}).get("version") == "v2"
    if request.operation == "restart":
        old_uids = {p["uid"] for p in old["pods"]}
        new_uids = {p["uid"] for p in dep["pods"] if not p["terminating"]}
        return bool(dep["marker"] and dep["marker"] != old["marker"] and new_uids and not old_uids & new_uids)
    if request.operation == "set_selector":
        return service["selector"] == {LABEL:"checkout-api"} and (service["response"] or {}).get("service") == "checkout"
    return after["configmaps"][0]["data"].get("mode") == "enabled" and (service["response"] or {}).get("mode") == "enabled"


def collateral(before, after):
    old = next(d for d in before["deployments"] if d["name"] == "payments-api")
    new = next(d for d in after["deployments"] if d["name"] == "payments-api")
    return any(old[k] != new[k] for k in ("uid", "generation", "desired", "images", "marker"))
