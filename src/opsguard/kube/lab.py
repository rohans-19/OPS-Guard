"""Disposable kind lab with separate setup, reader, executor and observer identities."""

from base64 import b64decode
from contextlib import AsyncExitStack, asynccontextmanager
import ipaddress
import json
from pathlib import Path
import re
from shutil import which
import ssl
from tempfile import TemporaryDirectory
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from opsguard.benchmark.kind_scale_lab import _command, _fixture
from opsguard.benchmark.scale_cases import Workload
from opsguard.intent.inventory import KubernetesInventory
from opsguard.intent.resolver import deployment_for_pod
from opsguard.kube.executor import KubernetesTransport
from opsguard.operations.restart import RESTART_ANNOTATION


def lab_manifest(namespace, workloads, app, image, *, log_target="", log_line="", configmap_text="No incident override."):
    parsed = {name: Workload(service=f["service"], initial_replicas=f["replicas"]) for name, f in workloads.items()}
    manifest = _fixture(namespace, parsed, app, log_target, log_line, image)
    manifest["items"].append({"apiVersion":"v1","kind":"ConfigMap","metadata":{"name":"opsguard-evidence"},
                              "data":{"observation":configmap_text}})
    names = sorted(workloads)
    for item in manifest["items"]:
        item["metadata"]["namespace"] = namespace
        if item["kind"] == "Deployment":
            item["spec"]["template"]["spec"]["terminationGracePeriodSeconds"] = 1
        if item["kind"] == "Role" and item["metadata"]["name"] == "reader":
            item["rules"].append({"apiGroups":[""],"resources":["configmaps"],"resourceNames":["opsguard-evidence"],"verbs":["get"]})
        if item["kind"] == "Role" and item["metadata"]["name"] == "executor":
            item["rules"].append({"apiGroups": ["apps"], "resources": ["deployments"],
                                  "verbs": ["patch"], "resourceNames": names})
        if item["kind"] == "Role" and item["metadata"]["name"] == "observer":
            item["rules"].extend([
                {"apiGroups": ["apps"], "resources": ["deployments", "replicasets"], "verbs": ["list"]},
                {"apiGroups": [""], "resources": ["pods", "services"], "verbs": ["list"]},
                {"apiGroups": ["discovery.k8s.io"], "resources": ["endpointslices"], "verbs": ["list"]},
            ])
    manifest["items"].extend([
        {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": "setup", "namespace": namespace},
         "automountServiceAccountToken": False},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": "setup", "namespace": namespace},
         "rules": [{"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["create"]},
                   {"apiGroups": [""], "resources": ["services", "configmaps"], "verbs": ["create"]}]},
        {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": "setup", "namespace": namespace},
         "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": "setup"},
         "subjects": [{"kind": "ServiceAccount", "name": "setup", "namespace": namespace}]},
    ])
    return manifest


class KubernetesRestartObserver:
    def __init__(self, namespace, client, workloads):
        self.namespace, self.client, self.workloads = namespace, client, workloads

    async def snapshot(self, *, timeout_seconds):
        from time import monotonic
        deadline = monotonic() + timeout_seconds
        snapshot = await KubernetesInventory(self.namespace, self.client).inventory(timeout_seconds=timeout_seconds)
        pods = {name: [] for name in self.workloads}
        for pod in snapshot.pods:
            if pod["metadata"].get("deletionTimestamp"):
                continue
            try:
                dep = deployment_for_pod(snapshot, pod)
            except (KeyError, ValueError, TypeError):
                continue
            if dep["metadata"]["name"] in pods:
                pods[dep["metadata"]["name"]].append(pod["metadata"]["uid"])
        values = {}
        for dep in snapshot.deployments:
            meta, spec, status = dep["metadata"], dep["spec"], dep.get("status", {})
            name = meta["name"]
            if name not in self.workloads:
                continue
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("observer deadline")
            service = self.workloads[name]["service"]
            response = await self.client.get(f"/api/v1/namespaces/{self.namespace}/services/{service}:http/proxy/ready", timeout=remaining)
            if response.status_code in (401, 403):
                response.raise_for_status()
            replicas = spec["replicas"]
            values[name] = {"uid": meta["uid"], "generation": meta["generation"],
                "marker": spec["template"]["metadata"].get("annotations", {}).get(RESTART_ANNOTATION),
                "pod_uids": sorted(pods[name]),
                "ready": replicas > 0 and status.get("observedGeneration", 0) >= meta["generation"]
                    and status.get("readyReplicas", 0) == replicas and status.get("updatedReplicas", 0) == replicas,
                "http_ok": response.is_success and response.text.strip() == service}
        if set(values) != set(self.workloads):
            raise ValueError("observer is missing fixture deployments")
        return values


@asynccontextmanager
async def open_kind_execution_lab(*, root: Path, cluster: str, workloads: dict, image: str,
                                  log_target="", log_line="", configmap_text="No incident override.", manifest_transform=None):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", cluster):
        raise ValueError("explicit kind cluster name required")
    # This is an explicit opt-in lab. No default mutable image is silently pulled.
    if not image or len(image) > 512 or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/@:+-]*", image):
        raise ValueError("explicit fixture image required")
    kind, kubectl = which("kind"), which("kubectl")
    if not kind or not kubectl:
        raise RuntimeError("kind and kubectl must be on PATH")
    if cluster not in (await _command([kind, "get", "clusters"])).decode().splitlines():
        raise RuntimeError("named kind cluster does not exist")
    kubeconfig = await _command([kind, "get", "kubeconfig", "--name", cluster])
    namespace = "opsguard-exec-" + uuid4().hex[:12]
    with TemporaryDirectory(prefix="opsguard-exec-") as directory:
        path = Path(directory) / "admin.kubeconfig"
        path.write_bytes(kubeconfig)
        base = [kubectl, "--kubeconfig", str(path), "--context", f"kind-{cluster}"]
        selected = json.loads(await _command([*base, "config", "view", "--minify", "--raw", "-o", "json"]))
        connection = selected["clusters"][0]["cluster"]
        server = connection["server"]
        url = urlsplit(server)
        if url.scheme != "https" or not ipaddress.ip_address(url.hostname or "").is_loopback:
            raise RuntimeError("kind API must be loopback HTTPS")
        tls = ssl.create_default_context(cadata=b64decode(connection["certificate-authority-data"]).decode("ascii"))
        await _command([*base, "create", "namespace", namespace])
        try:
            manifest = lab_manifest(namespace, workloads, (root / "benchmark/fixtures/scale/app.py").read_text(encoding="utf-8"), image,
                                    log_target=log_target,log_line=log_line,configmap_text=configmap_text)
            if manifest_transform is not None:
                manifest = manifest_transform(manifest, namespace)
            identities = [obj for obj in manifest["items"] if obj["kind"] in ("ServiceAccount", "Role", "RoleBinding")]
            scoped = [*base, "-n", namespace]
            await _command([*scoped, "apply", "-f", "-"], payload=json.dumps({"apiVersion": "v1", "kind": "List", "items": identities}).encode())
            async with AsyncExitStack() as stack:
                clients = {}
                for role in ("setup", "reader", "executor", "observer"):
                    token = (await _command([*scoped, "create", "token", role, "--duration=20m"])).decode().strip()
                    if not token:
                        raise RuntimeError("missing role token")
                    clients[role] = await stack.enter_async_context(httpx.AsyncClient(base_url=server, verify=tls,
                        headers={"Authorization": f"Bearer {token}"}, trust_env=False, follow_redirects=False))
                for obj in manifest["items"]:
                    resource = {"Deployment": "/apis/apps/v1/namespaces/{}/deployments",
                                "Service": "/api/v1/namespaces/{}/services", "ConfigMap": "/api/v1/namespaces/{}/configmaps"}.get(obj["kind"])
                    if resource:
                        (await clients["setup"].post(resource.format(namespace), json=obj, timeout=30)).raise_for_status()
                for name in workloads:
                    await _command([*scoped, "rollout", "status", f"deployment/{name}", "--timeout=180s"], timeout=190)
                yield (KubernetesInventory(namespace, clients["reader"]),
                       KubernetesTransport(namespace, clients["executor"]),
                       KubernetesRestartObserver(namespace, clients["observer"], workloads))
        finally:
            await _command([*base, "delete", "namespace", namespace, "--ignore-not-found", "--wait=false"])
