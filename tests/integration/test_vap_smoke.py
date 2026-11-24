"""Opt-in check that a ValidatingAdmissionPolicy is enforced by the lab's API server.

Stage 2 compiles each intent into gateway rules and an admission policy. This
smoke test proves the second enforcement point works on the local kind cluster
before that compiler exists: a write that breaks the CEL rule is rejected by the
API server itself, and a write that satisfies it is accepted.

Run with:  $env:OPSGUARD_KIND_CLUSTER = 'opsguard-lab'; pytest tests/integration/test_vap_smoke.py
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
from uuid import uuid4

import pytest

CLUSTER = os.environ.get("OPSGUARD_KIND_CLUSTER", "")


def _kubectl(config: Path, *args: str, payload: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([shutil.which("kubectl") or "kubectl", "--kubeconfig", str(config), "--context",
                           f"kind-{CLUSTER}", *args], input=payload, capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(not CLUSTER or not shutil.which("kind") or not shutil.which("kubectl"),
                    reason="opt in with OPSGUARD_KIND_CLUSTER; needs kind and kubectl on PATH")
def test_admission_policy_rejects_a_violating_write_and_accepts_a_valid_one():
    suffix = uuid4().hex[:8]
    namespace, policy = f"opsguard-vap-{suffix}", f"opsguard-replicas-{suffix}"
    objects = {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicy",
         "metadata": {"name": policy},
         "spec": {"failurePolicy": "Fail",
                  "matchConstraints": {"resourceRules": [{"apiGroups": ["apps"], "apiVersions": ["v1"],
                                                          "operations": ["CREATE", "UPDATE"],
                                                          "resources": ["deployments", "deployments/scale"]}]},
                  "validations": [{"expression": "object.spec.replicas <= 4",
                                   "message": "OpsGuard lab bound: at most 4 replicas"}]}},
        {"apiVersion": "admissionregistration.k8s.io/v1", "kind": "ValidatingAdmissionPolicyBinding",
         "metadata": {"name": policy},
         "spec": {"policyName": policy, "validationActions": ["Deny"],
                  "matchResources": {"namespaceSelector": {"matchLabels": {
                      "kubernetes.io/metadata.name": namespace}}}}},
    ]}
    deployment = {"apiVersion": "apps/v1", "kind": "Deployment", "metadata": {"name": "probe", "namespace": namespace},
                  "spec": {"replicas": 0, "selector": {"matchLabels": {"app": "probe"}},
                           "template": {"metadata": {"labels": {"app": "probe"}},
                                        "spec": {"containers": [{"name": "c", "image": "registry.k8s.io/pause:3.10"}]}}}}
    with tempfile.TemporaryDirectory() as directory:
        config = Path(directory) / "admin.kubeconfig"
        config.write_bytes(subprocess.run(["kind", "get", "kubeconfig", "--name", CLUSTER],
                                          capture_output=True, check=True).stdout)
        assert _kubectl(config, "create", "namespace", namespace).returncode == 0
        try:
            applied = _kubectl(config, "apply", "-f", "-", payload=json.dumps(objects))
            assert applied.returncode == 0, applied.stderr
            assert _kubectl(config, "apply", "-f", "-", payload=json.dumps(deployment)).returncode == 0
            # Policies take a moment to reach the admission plugin; poll for enforcement.
            deadline, rejected = time.monotonic() + 30, None
            while time.monotonic() < deadline:
                rejected = _kubectl(config, "-n", namespace, "scale", "deployment/probe", "--replicas=5")
                if rejected.returncode != 0:
                    break
                _kubectl(config, "-n", namespace, "scale", "deployment/probe", "--replicas=0")
                time.sleep(1)
            assert rejected is not None and rejected.returncode != 0, "the API server never enforced the policy"
            assert "at most 4 replicas" in rejected.stderr
            accepted = _kubectl(config, "-n", namespace, "scale", "deployment/probe", "--replicas=3")
            assert accepted.returncode == 0, accepted.stderr
        finally:
            _kubectl(config, "delete", "validatingadmissionpolicybinding", policy, "--ignore-not-found")
            _kubectl(config, "delete", "validatingadmissionpolicy", policy, "--ignore-not-found")
            _kubectl(config, "delete", "namespace", namespace, "--wait=false")
