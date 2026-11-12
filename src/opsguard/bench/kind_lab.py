"""Opt-in selector fixture on a named local kind cluster, one fresh namespace per trial.

Setup, reader, executor and oracle use separate short-lived ServiceAccount
tokens. The executor may patch only the fixture Services. The oracle may only
read state and reach Services through the API proxy. Deleting the namespace on
exit is the cleanup.
"""

from __future__ import annotations

from base64 import b64decode
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
import ipaddress
import json
from pathlib import Path
import re
from shutil import which
import ssl
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.parse import urlsplit
from uuid import uuid4

import httpx

from opsguard.bench.predicates import DeploymentView, Observation, ServiceView
from opsguard.bench.selector_lab import EVIDENCE_CONFIGMAP, WORKLOAD_LABEL, SelectorFixture
from opsguard.benchmark.kind_scale_lab import _command
from opsguard.intent.inventory import KubernetesInventory
from opsguard.intent.resolver import deployment_for_pod
from opsguard.kube.executor import KubernetesTransport

APP_CONFIGMAP = "selector-app"
PROBES_PER_SERVICE = 3


@dataclass
class LabHandles:
    namespace: str
    inventory: object
    transport: object
    oracle: object
    reader_client: httpx.AsyncClient | None = None


def selector_manifest(namespace: str, fixture: SelectorFixture, task_id: str, app: str, image: str, *,
                      log_target: str = "", log_line: str = "", configmap_text: str = "No incident override.") -> dict:
    """Namespaced objects for one trial. Inspect this to review the lab's RBAC boundary."""
    items: list[dict] = [
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": APP_CONFIGMAP}, "data": {"app.py": app}},
        {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": EVIDENCE_CONFIGMAP},
         "data": {"observation": configmap_text}},
    ]
    for name, dep in fixture.deployments.items():
        items.append({
            "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name},
            "spec": {"replicas": dep.replicas, "selector": {"matchLabels": {WORKLOAD_LABEL: name}},
                     "template": {"metadata": {"labels": {WORKLOAD_LABEL: name, **dep.labels}}, "spec": {
                         "automountServiceAccountToken": False, "terminationGracePeriodSeconds": 1,
                         "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                             "seccompProfile": {"type": "RuntimeDefault"}},
                         "containers": [{
                             "name": "app", "image": image, "command": ["python", "-u", "/app/app.py"],
                             "env": [{"name": "FIXTURE_BACKEND", "value": name},
                                     {"name": "FIXTURE_LOG_LINE", "value": log_line if name == log_target else ""}],
                             "ports": [{"name": "http", "containerPort": 8080}],
                             "securityContext": {"allowPrivilegeEscalation": False, "readOnlyRootFilesystem": True,
                                                 "capabilities": {"drop": ["ALL"]}},
                             "readinessProbe": {"httpGet": {"path": "/ready", "port": "http"}, "periodSeconds": 2},
                             "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                                           "limits": {"cpu": "200m", "memory": "64Mi"}},
                             "volumeMounts": [{"name": "app", "mountPath": "/app", "readOnly": True}],
                         }], "volumes": [{"name": "app", "configMap": {"name": APP_CONFIGMAP}}]}}},
        })
    for name, selector in fixture.initial_selectors(task_id).items():
        spec = {"ports": [{"name": "http", "port": 8080, "targetPort": "http"}]}
        if selector:
            spec["selector"] = selector
        items.append({"apiVersion": "v1", "kind": "Service", "metadata": {"name": name}, "spec": spec})
    services = sorted(fixture.services)
    state = [{"apiGroups": ["apps"], "resources": ["deployments", "replicasets"], "verbs": ["list"]},
             {"apiGroups": [""], "resources": ["pods", "services"], "verbs": ["list"]},
             {"apiGroups": ["discovery.k8s.io"], "resources": ["endpointslices"], "verbs": ["list"]}]
    roles = {
        "setup": [{"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["create"]},
                  {"apiGroups": [""], "resources": ["services", "configmaps"], "verbs": ["create"]}],
        "reader": [*state, {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]},
                   {"apiGroups": [""], "resources": ["configmaps"], "resourceNames": [EVIDENCE_CONFIGMAP], "verbs": ["get"]}],
        "executor": [{"apiGroups": [""], "resources": ["services"], "verbs": ["patch"], "resourceNames": services}],
        "observer": [*state, {"apiGroups": [""], "resources": ["services/proxy"], "verbs": ["get"]}],
    }
    for role, rules in roles.items():
        items.extend([
            {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": role},
             "automountServiceAccountToken": False},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role", "metadata": {"name": role}, "rules": rules},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding", "metadata": {"name": role},
             "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": role},
             "subjects": [{"kind": "ServiceAccount", "name": role, "namespace": namespace}]},
        ])
    for item in items:
        item["metadata"]["namespace"] = namespace
    return {"apiVersion": "v1", "kind": "List", "items": items}


class KindSelectorOracle:
    """Independent observer: its own token, endpoint membership, and HTTP identity probes."""

    def __init__(self, namespace: str, client: httpx.AsyncClient, fixture: SelectorFixture):
        self.namespace, self.client, self.fixture = namespace, client, fixture

    async def _responders(self, service: str, deadline: float) -> tuple[str, ...]:
        seen = []
        for _ in range(PROBES_PER_SERVICE):
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("oracle deadline")
            response = await self.client.get(
                f"/api/v1/namespaces/{self.namespace}/services/{service}:http/proxy/whoami", timeout=remaining)
            if response.status_code in (401, 403):
                response.raise_for_status()
            if response.is_success:
                seen.append(response.text.strip())
        return tuple(sorted(set(seen)))

    async def observe(self, *, timeout_seconds: float) -> Observation:
        deadline = monotonic() + timeout_seconds
        snapshot = await KubernetesInventory(self.namespace, self.client).inventory(timeout_seconds=timeout_seconds)
        pods = {pod["metadata"]["name"]: pod for pod in snapshot.pods}
        ready_pods: dict[str, set[str]] = {name: set() for name in self.fixture.deployments}
        pod_uids: dict[str, list[str]] = {name: [] for name in self.fixture.deployments}
        owner: dict[str, str] = {}
        for pod in snapshot.pods:
            if pod["metadata"].get("deletionTimestamp"):
                continue
            try:
                name = deployment_for_pod(snapshot, pod)["metadata"]["name"]
            except (KeyError, ValueError, TypeError):
                continue
            if name not in ready_pods:
                continue
            owner[pod["metadata"]["name"]] = name
            pod_uids[name].append(pod["metadata"]["uid"])
            conditions = pod.get("status", {}).get("conditions") or []
            if any(c.get("type") == "Ready" and c.get("status") == "True" for c in conditions):
                ready_pods[name].add(pod["metadata"]["name"])
        endpoints: dict[str, set[str]] = {name: set() for name in self.fixture.services}
        for item in snapshot.endpointslices:
            service = item["metadata"].get("labels", {}).get("kubernetes.io/service-name")
            if service not in endpoints or item["metadata"].get("deletionTimestamp"):
                continue
            for endpoint in item.get("endpoints") or []:
                ref = endpoint.get("targetRef") or {}
                if endpoint.get("conditions", {}).get("ready") is True and ref.get("kind") == "Pod" and ref.get("name") in pods:
                    endpoints[service].add(ref["name"])
        services, settled = {}, True
        for obj in snapshot.services:
            name = obj["metadata"]["name"]
            if name not in endpoints:
                continue
            selector = dict(obj.get("spec", {}).get("selector") or {})
            # What the endpoints controller should converge to for the current selector.
            expected = {pod for backend in self.fixture.deployments for pod in ready_pods[backend]
                        if selector and all(pods[pod]["metadata"].get("labels", {}).get(k) == v for k, v in selector.items())}
            backends = frozenset(owner[pod] for pod in endpoints[name] if pod in owner)
            responders = await self._responders(name, deadline)
            settled &= endpoints[name] == expected and bool(responders) == bool(expected) and set(responders) <= backends
            services[name] = ServiceView(selector=selector, ready_backends=backends, responders=responders)
        deployments = {}
        for dep in snapshot.deployments:
            name, status = dep["metadata"]["name"], dep.get("status", {})
            if name not in self.fixture.deployments:
                continue
            replicas = dep["spec"].get("replicas", 1)
            deployments[name] = DeploymentView(
                replicas=replicas, ready_replicas=status.get("readyReplicas", 0),
                generation=dep["metadata"].get("generation", 1), observed_generation=status.get("observedGeneration", 0),
                images={c["name"]: c["image"] for c in dep["spec"]["template"]["spec"]["containers"]},
                pod_uids=tuple(sorted(pod_uids[name])))
            settled &= status.get("readyReplicas", 0) == replicas and len(ready_pods[name]) == replicas
        if set(services) != set(self.fixture.services) or set(deployments) != set(self.fixture.deployments):
            raise ValueError("oracle is missing fixture objects")
        return Observation(services=services, deployments=deployments, settled=settled)


@asynccontextmanager
async def open_kind_selector_lab(*, fixture_dir: Path, cluster: str, image: str, fixture: SelectorFixture, task_id: str,
                                 log_target: str = "", log_line: str = "",
                                 configmap_text: str = "No incident override."):
    if not cluster or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", cluster):
        raise ValueError("explicit kind cluster name required")
    # No default mutable image is silently pulled. Name the image loaded into the cluster.
    if not image or len(image) > 512 or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/@:+-]*", image):
        raise ValueError("explicit fixture image required")
    kind, kubectl = which("kind"), which("kubectl")
    if not kind or not kubectl:
        raise RuntimeError("kind and kubectl must be on PATH")
    if cluster not in (await _command([kind, "get", "clusters"])).decode().splitlines():
        raise RuntimeError("named kind cluster does not exist")
    kubeconfig = await _command([kind, "get", "kubeconfig", "--name", cluster])
    namespace = "opsguard-selector-" + uuid4().hex[:12]
    with TemporaryDirectory(prefix="opsguard-selector-") as directory:
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
            app = (fixture_dir / fixture.app).read_text(encoding="utf-8")
            manifest = selector_manifest(namespace, fixture, task_id, app, image, log_target=log_target,
                                         log_line=log_line, configmap_text=configmap_text)
            identities = [obj for obj in manifest["items"] if obj["kind"] in ("ServiceAccount", "Role", "RoleBinding")]
            scoped = [*base, "-n", namespace]
            await _command([*scoped, "apply", "-f", "-"],
                           payload=json.dumps({"apiVersion": "v1", "kind": "List", "items": identities}).encode())
            async with AsyncExitStack() as stack:
                clients = {}
                for role in ("setup", "reader", "executor", "observer"):
                    token = (await _command([*scoped, "create", "token", role, "--duration=20m"])).decode().strip()
                    if not token:
                        raise RuntimeError("missing role token")
                    clients[role] = await stack.enter_async_context(httpx.AsyncClient(
                        base_url=server, verify=tls, headers={"Authorization": f"Bearer {token}"},
                        trust_env=False, follow_redirects=False))
                paths = {"Deployment": f"/apis/apps/v1/namespaces/{namespace}/deployments",
                         "Service": f"/api/v1/namespaces/{namespace}/services",
                         "ConfigMap": f"/api/v1/namespaces/{namespace}/configmaps"}
                for obj in manifest["items"]:
                    if obj["kind"] in paths:
                        (await clients["setup"].post(paths[obj["kind"]], json=obj, timeout=30)).raise_for_status()
                for name in fixture.deployments:
                    await _command([*scoped, "rollout", "status", f"deployment/{name}", "--timeout=180s"], timeout=190)
                yield LabHandles(namespace=namespace, inventory=KubernetesInventory(namespace, clients["reader"]),
                                 transport=KubernetesTransport(namespace, clients["executor"]),
                                 oracle=KindSelectorOracle(namespace, clients["observer"], fixture),
                                 reader_client=clients["reader"])
        finally:
            await _command([*base, "delete", "namespace", namespace, "--ignore-not-found", "--wait=false"])
