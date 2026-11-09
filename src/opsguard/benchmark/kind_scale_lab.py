"""Opt-in live fixture on a named local kind cluster, with separate role tokens."""

from __future__ import annotations

import asyncio
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

from opsguard.benchmark.scale_cases import DeploymentState, Injection, Workload
from opsguard.benchmark.scale_lab import ScaleConflict
from opsguard.evidence.pod_logs import PodLogSource
from opsguard.operations.scale import PreparedScale


async def _command(args: list[str], *, payload: bytes | None = None, timeout: float = 30) -> bytes:
    process = await asyncio.create_subprocess_exec(
        *args, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(payload), timeout)
    except (asyncio.CancelledError, TimeoutError):
        if process.returncode is None:
            process.kill()
        await process.communicate()
        raise
    if process.returncode:
        raise RuntimeError(f"{Path(args[0]).name} failed with exit code {process.returncode}; inspect the kind lab")
    return output


def _fixture(namespace: str, workloads: dict[str, Workload], app: str, log_target: str, log_line: str, image: str) -> dict:
    items = [{"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "scale-app"}, "data": {"app.py": app}}]
    for name, workload in workloads.items():
        labels = {"app.kubernetes.io/name": name}
        items.extend([
            {
                "apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": name},
                "spec": {"replicas": workload.initial_replicas, "selector": {"matchLabels": labels},
                         "template": {"metadata": {"labels": labels}, "spec": {
                             "automountServiceAccountToken": False,
                             "securityContext": {"runAsNonRoot": True, "runAsUser": 10001,
                                                 "seccompProfile": {"type": "RuntimeDefault"}},
                             "containers": [{
                                 "name": "app", "image": image, "command": ["python", "-u", "/app/app.py"],
                                 "env": [{"name": "FIXTURE_SERVICE", "value": workload.service},
                                         {"name": "FIXTURE_LOG_LINE", "value": log_line if name == log_target else ""}],
                                 "ports": [{"name": "http", "containerPort": 8080}],
                                 "securityContext": {"allowPrivilegeEscalation": False,
                                                     "readOnlyRootFilesystem": True,
                                                     "capabilities": {"drop": ["ALL"]}},
                                 "readinessProbe": {"httpGet": {"path": "/ready", "port": "http"}, "periodSeconds": 2},
                                 "resources": {"requests": {"cpu": "10m", "memory": "16Mi"},
                                               "limits": {"cpu": "200m", "memory": "64Mi"}},
                                 "volumeMounts": [{"name": "app", "mountPath": "/app", "readOnly": True}],
                             }], "volumes": [{"name": "app", "configMap": {"name": "scale-app"}}],
                         }}},
            },
            {
                "apiVersion": "v1", "kind": "Service", "metadata": {"name": workload.service},
                "spec": {"selector": labels, "ports": [{"name": "http", "port": 8080, "targetPort": "http"}]},
            },
        ])
    dep_read = {"apiGroups": ["apps"], "resources": ["deployments"], "verbs": ["get"],
                "resourceNames": sorted(workloads)}
    for role, rules in {
        "reader": [dep_read, {"apiGroups": [""], "resources": ["pods"], "verbs": ["list"]},
                   {"apiGroups": [""], "resources": ["pods/log"], "verbs": ["get"]}],
        "executor": [{"apiGroups": ["apps"], "resources": ["deployments/scale"], "verbs": ["update"],
                      "resourceNames": sorted(workloads)}],
        "observer": [dep_read, {"apiGroups": [""], "resources": ["services/proxy"], "verbs": ["get"]}],
    }.items():
        items.extend([
            {"apiVersion": "v1", "kind": "ServiceAccount", "metadata": {"name": role},
             "automountServiceAccountToken": False},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "Role",
             "metadata": {"name": role}, "rules": rules},
            {"apiVersion": "rbac.authorization.k8s.io/v1", "kind": "RoleBinding",
             "metadata": {"name": role},
             "roleRef": {"apiGroup": "rbac.authorization.k8s.io", "kind": "Role", "name": role},
             "subjects": [{"kind": "ServiceAccount", "name": role, "namespace": namespace}]},
        ])
    return {"apiVersion": "v1", "kind": "List", "items": items}


class KindScaleObserver:
    def __init__(self, namespace: str, client: httpx.AsyncClient):
        self.namespace = namespace
        self.client = client

    async def deployment(self, name: str, *, timeout_seconds: float) -> DeploymentState:
        response = await self.client.get(f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{name}",
                                         timeout=timeout_seconds)
        obj = response.raise_for_status().json()
        metadata, status = obj["metadata"], obj.get("status", {})
        return DeploymentState(
            obj["spec"]["replicas"], status.get("readyReplicas", 0), metadata["generation"],
            status.get("observedGeneration", 0), metadata["uid"], metadata["resourceVersion"],
        )

    async def service_healthy(self, service: str, *, timeout_seconds: float) -> bool:
        response = await self.client.get(f"/api/v1/namespaces/{self.namespace}/services/{service}/proxy/ready",
                                         timeout=timeout_seconds)
        if response.status_code in (401, 403):
            response.raise_for_status()
        return response.is_success and response.text.strip() == service


class KindScaleLab(KindScaleObserver):
    def __init__(self, namespace: str, reader: httpx.AsyncClient, executor: httpx.AsyncClient):
        super().__init__(namespace, reader)
        self.executor = executor

    async def log_source(self, name: str, *, timeout_seconds: float) -> PodLogSource:
        response = await self.client.get(
            f"/api/v1/namespaces/{self.namespace}/pods",
            params={"labelSelector": f"app.kubernetes.io/name={name}"}, timeout=timeout_seconds,
        )
        pods = response.raise_for_status().json()["items"]
        ready = [
            pod["metadata"] for pod in pods if not pod["metadata"].get("deletionTimestamp")
            and pod.get("status", {}).get("phase") == "Running"
        ]
        if not ready:
            raise RuntimeError("no running pod for fixture workload")
        selected = min(ready, key=lambda metadata: metadata["name"])
        return PodLogSource(self.namespace, selected["name"], "app", selected["uid"])

    async def read_log(self, source: PodLogSource, *, tail_lines: int, limit_bytes: int, timeout_seconds: float) -> str:
        response = await self.client.get(
            f"/api/v1/namespaces/{source.namespace}/pods/{source.pod}/log",
            params={"container": source.container, "tailLines": tail_lines, "limitBytes": limit_bytes},
            timeout=timeout_seconds,
        )
        return response.raise_for_status().text

    async def replace_scale(self, mutation: PreparedScale, *, dry_run: bool, timeout_seconds: float) -> None:
        if mutation.target.namespace != self.namespace:
            raise ValueError("scale mutation is outside fixture namespace")
        response = await self.executor.put(
            mutation.path, params={"dryRun": "All"} if dry_run else None,
            json=mutation.body(), timeout=timeout_seconds,
        )
        if response.status_code == 409:
            raise ScaleConflict
        response.raise_for_status()


@asynccontextmanager
async def open_kind_scale_lab(
    *, root: Path, cluster: str, workloads: dict[str, Workload], log_target: str,
    injection: Injection | None, image: str,
):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,40}", cluster):
        raise ValueError("supply an explicit kind cluster name")
    kind, kubectl = which("kind"), which("kubectl")
    if not kind or not kubectl:
        raise RuntimeError("kind and kubectl must be on PATH")
    if cluster not in (await _command([kind, "get", "clusters"])).decode().splitlines():
        raise RuntimeError("named kind cluster does not exist")
    kubeconfig = await _command([kind, "get", "kubeconfig", "--name", cluster])
    namespace = f"opsguard-scale-{uuid4().hex[:12]}"
    with TemporaryDirectory(prefix="opsguard-scale-") as folder:
        config = Path(folder) / "admin.kubeconfig"
        config.write_bytes(kubeconfig)
        base = [kubectl, "--kubeconfig", str(config), "--context", f"kind-{cluster}"]
        selected = json.loads(await _command([*base, "config", "view", "--minify", "--raw", "-o", "json"]))
        connection = selected["clusters"][0]["cluster"]
        server = connection["server"]
        url = urlsplit(server)
        if url.scheme != "https" or not ipaddress.ip_address(url.hostname or "").is_loopback:
            raise RuntimeError("kind API must use a loopback HTTPS address")
        tls = ssl.create_default_context(cadata=b64decode(connection["certificate-authority-data"]).decode("ascii"))
        await _command([*base, "create", "namespace", namespace])
        try:
            app = (root / "benchmark/fixtures/scale/app.py").read_text(encoding="utf-8")
            log_line = injection.payload if injection and injection.channel == "logs" else ""
            fixture = _fixture(namespace, workloads, app, log_target, log_line, image)
            scoped = [*base, "-n", namespace]
            await _command([*scoped, "apply", "-f", "-"], payload=json.dumps(fixture).encode())
            await _command([*scoped, "wait", "--for=condition=Available", "deployment", "--all", "--timeout=180s"],
                           timeout=190)
            async with AsyncExitStack() as stack:
                clients = {}
                for role in ("reader", "executor", "observer"):
                    token = (await _command([*scoped, "create", "token", role, "--duration=20m"])).decode().strip()
                    if not token:
                        raise RuntimeError("kind did not issue a role token")
                    clients[role] = await stack.enter_async_context(httpx.AsyncClient(
                        base_url=server, verify=tls, headers={"Authorization": f"Bearer {token}"},
                        trust_env=False, follow_redirects=False,
                    ))
                yield (KindScaleLab(namespace, clients["reader"], clients["executor"]),
                       KindScaleObserver(namespace, clients["observer"]))
        finally:
            await _command([*base, "delete", "namespace", namespace, "--ignore-not-found", "--wait=false"])
