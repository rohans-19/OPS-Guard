"""Run Vikas's ConfigMap fixtures in disposable namespaces on a named kind cluster."""

import json
import re
import subprocess
import time
from contextlib import ExitStack
from pathlib import Path
from shutil import which
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any, Mapping, Optional
from uuid import uuid4

from opsguard.evidence.configmap import (
    ConfigMapEvidenceSource,
    inject_configmap_evidence,
    read_configmap_evidence,
)
from opsguard.operations.set_config import PreparedConfigMutation


_MOUNTS = {
    "checkout": "shared-features",
    "storefront": "shared-features",
    "decoy": "decoy-features",
}
_PROBE_PATHS = frozenset({"/ready", "/feature", "/health", "/cache", "/metrics"})
_PROBE = """\
import http.client, sys
conn = http.client.HTTPConnection(sys.argv[1], 8080, timeout=3)
try:
    conn.request("GET", sys.argv[2])
    print(conn.getresponse().status)
except (OSError, http.client.HTTPException):
    print(0)
finally:
    conn.close()
"""
_READ_MOUNT = """\
import json, sys
from pathlib import Path
root = Path("/etc/opsguard")
data = {}
for key in json.loads(sys.argv[1]):
    try:
        data[key] = (root / key).read_text(encoding="utf-8")
    except FileNotFoundError:
        pass
print(json.dumps(data))
"""


class KindReplayError(RuntimeError):
    """A lab setup, API operation, or oracle observation failed."""


class KindReplay:
    def __init__(self, root: Path, cluster: str, case: Mapping[str, Any]):
        if not isinstance(cluster, str) or not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", cluster):
            raise ValueError("supply an explicit kind cluster name")
        self._fixture = Path(root).resolve() / "benchmark" / "fixtures" / "vikas"
        self._cluster = cluster
        self._case = case
        self._namespace = ""
        self._cleanup: Optional[ExitStack] = None
        self._directory: Optional[Path] = None
        self._kubectl_path: Optional[str] = None
        self._prepared: dict[PreparedConfigMutation, dict[str, dict[str, str]]] = {}

    @property
    def namespace(self) -> str:
        return self._namespace

    def __enter__(self) -> "KindReplay":
        if self._directory is not None:
            raise RuntimeError("the replay namespace is already open")
        kind, kubectl = which("kind"), which("kubectl")
        if not kind or not kubectl:
            raise KindReplayError("kind and kubectl are required for live replays")
        if not (self._fixture / "kustomization.yaml").is_file():
            raise KindReplayError(f"fixture not found at {self._fixture}")
        kubeconfig = self._run([kind, "get", "kubeconfig", "--name", self._cluster], "export kind kubeconfig")
        if not kubeconfig.strip():
            raise KindReplayError("kind returned an empty kubeconfig")

        with ExitStack() as stack:
            self._directory = Path(stack.enter_context(TemporaryDirectory(prefix="opsguard-kind-")))
            self._kubectl_path = kubectl
            self._namespace = f"opsguard-vikas-{uuid4().hex[:12]}"
            stack.callback(self._clear)
            (self._directory / "kubeconfig").write_text(kubeconfig, encoding="utf-8")
            self._kubectl("create", "namespace", self.namespace)
            stack.callback(self._kubectl, "delete", "namespace", self.namespace, "--ignore-not-found", "--wait=false")
            self._kubectl("apply", "-k", str(self._fixture))
            self._stage()
            self._cleanup = stack.pop_all()
        return self

    def __exit__(self, exc_type, exc, traceback):
        cleanup, self._cleanup = self._cleanup, None
        if cleanup is not None:
            cleanup.__exit__(exc_type, exc, traceback)

    def _clear(self) -> None:
        self._directory = None
        self._kubectl_path = None
        self._prepared.clear()

    @staticmethod
    def _run(command: list[str], operation: str, timeout: int = 60) -> str:
        try:
            result = subprocess.run(command, capture_output=True, encoding="utf-8", timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as error:
            raise KindReplayError(f"{operation} failed ({type(error).__name__})") from error
        if result.returncode:
            raise KindReplayError(f"{operation} failed: {result.stderr.strip() or result.returncode}")
        return result.stdout

    def _kubectl(self, *args: str, timeout: int = 60) -> str:
        if self._directory is None or self._kubectl_path is None:
            raise KindReplayError("open the kind replay context before accessing the cluster")
        return self._run(
            [self._kubectl_path, "--kubeconfig", str(self._directory / "kubeconfig"),
             "--context", f"kind-{self._cluster}", "--namespace", self.namespace, *args],
            f"kubectl {args[0]}",
            timeout,
        )

    def _require_namespace(self, namespace: str) -> None:
        if namespace != self.namespace:
            raise ValueError("resource is outside the disposable replay namespace")

    def _read(self, name: str) -> dict:
        return json.loads(self._kubectl("get", "configmap", name, "-o", "json"))

    def _patch(self, name: str, body: dict, *, dry_run: bool = False) -> dict:
        if self._directory is None:
            raise KindReplayError("open the kind replay context before patching a ConfigMap")
        patch_file = self._directory / "patch.json"
        patch_file.write_text(json.dumps(body), encoding="utf-8")
        args = ["patch", "configmap", name, "--type=merge", "--patch-file", str(patch_file), "-o", "json"]
        if dry_run:
            args.append("--dry-run=server")
        return json.loads(self._kubectl(*args))

    def _stage(self) -> None:
        initial = self._case["initial"]
        for name, data in initial.items():
            self._patch(name, {"data": data})

        evidence = self._case.get("injection", {})
        if evidence.get("channel") == "configmaps":
            source = ConfigMapEvidenceSource(self.namespace)
            inject_configmap_evidence(_EvidenceClient(self), source=source, text=evidence["payload"])
            if self.read_configmap_evidence() != evidence["payload"]:
                raise KindReplayError("ConfigMap evidence differs from the case payload")

        self._kubectl("wait", "--for=condition=Available", "deployments", "--all", "--timeout=180s", timeout=190)
        if any(self.read_configmap(self.namespace, name) != data for name, data in initial.items()):
            raise KindReplayError("stored ConfigMap data differs from the initial case state")

        deadline = time.monotonic() + 180
        while not self._initial_mounts_ready(initial):
            if time.monotonic() >= deadline:
                raise KindReplayError("the initial ConfigMap mounts and HTTP readiness did not converge")
            time.sleep(2)

    def _initial_mounts_ready(self, initial: Mapping[str, Any]) -> bool:
        for service, configmap in _MOUNTS.items():
            expected = initial[configmap]
            mount = self._kubectl("exec", f"deployment/{service}-api", "--", "python", "-c", _READ_MOUNT,
                                  json.dumps(sorted(expected)), timeout=20)
            if json.loads(mount) != expected or self._probe_status(service, "/ready") != 200:
                return False
        return True

    def inventory(self) -> dict[str, list[dict]]:
        return {
            kind: json.loads(self._kubectl("get", kind, "-o", "json"))["items"]
            for kind in ("deployments", "services", "configmaps")
        }

    def read_configmap(self, namespace: str, name: str) -> Mapping[str, str]:
        self._require_namespace(namespace)
        return self._read(name).get("data") or {}

    def read_configmap_evidence(self) -> str:
        source = ConfigMapEvidenceSource(self.namespace)
        return read_configmap_evidence(
            _EvidenceClient(self), source=source, namespace=source.namespace, name=source.name, key=source.key,
        )

    def _probe_status(self, service: str, path: str) -> int:
        return int(self._kubectl("exec", "deployment/checkout-api", "--", "python", "-c", _PROBE,
                                 service, path, timeout=20))

    def probe_http(self, namespace: str, service: str, path: str) -> int:
        self._require_namespace(namespace)
        if service not in _MOUNTS or path not in _PROBE_PATHS:
            raise ValueError("probe is outside the ConfigMap fixture")
        status = self._probe_status(service, path)
        if not status:
            raise KindReplayError(f"HTTP probe could not reach Service {service}")
        return status

    def _prepare(self, mutation: PreparedConfigMutation) -> tuple[dict, dict[str, dict[str, str]]]:
        if not isinstance(mutation, PreparedConfigMutation):
            raise TypeError("the replay executor requires a prepared ConfigMap mutation")
        self._require_namespace(mutation.namespace)
        if mutation.name not in _MOUNTS.values():
            raise ValueError("mutation target is outside the ConfigMap fixture")
        before = self._read(mutation.name)
        version = before["metadata"]["resourceVersion"]
        return before, {**mutation.patch_body(), "metadata": {"resourceVersion": version}}

    def preview(self, mutation: PreparedConfigMutation) -> tuple[dict, dict]:
        before, body = self._prepare(mutation)
        after = self._patch(mutation.name, body, dry_run=True)
        self._prepared[mutation] = body
        return before, after

    def project(self, mutation: PreparedConfigMutation) -> tuple[dict, dict]:
        """Project the typed patch locally while keeping its version precondition."""
        before, body = self._prepare(mutation)
        after = {**before, "data": {**(before.get("data") or {}), **body["data"]}}
        self._prepared[mutation] = body
        return before, after

    def execute(self, mutation: PreparedConfigMutation) -> None:
        body = self._prepared.get(mutation)
        if body is None:
            _, body = self._prepare(mutation)
        self._patch(mutation.name, body)


class _EvidenceClient:
    def __init__(self, replay: KindReplay):
        self.replay = replay

    def read_namespaced_config_map(self, name: str, namespace: str) -> SimpleNamespace:
        return SimpleNamespace(data=self.replay.read_configmap(namespace, name))

    def patch_namespaced_config_map(self, name: str, namespace: str, body: dict) -> dict:
        self.replay._require_namespace(namespace)
        return self.replay._patch(name, body)
