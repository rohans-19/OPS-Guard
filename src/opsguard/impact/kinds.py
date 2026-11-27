"""The Kubernetes kinds the effect model reads, and how to read them.

Stage 3 concepts list the kinds whose static and dynamic edges the effect
model follows. PodDisruptionBudget is added because the scale-down rule
compares remaining capacity with it. CRDs and operator resources are out of
scope: only their ownerReferences are followed, and that is a stated limitation.

Every kind is namespaced. The gateway's reader identity needs ``list`` (and
``get``) on these kinds only; ``reader_rules()`` builds that Role. Secrets are
read as metadata only (``PartialObjectMetadataList``), because the effect
model needs their names and owners, never their values.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KindInfo:
    kind: str           # Kubernetes kind, e.g. "Deployment"
    group: str          # API group, "" for core
    version: str
    plural: str         # resource name in the URL and in RBAC rules
    field: str          # InventorySnapshot field
    category: str       # workload, controller, network, config, storage, autoscaling, identity, policy
    metadata_only: bool = False

    @property
    def api_prefix(self) -> str:
        return f"/api/{self.version}" if not self.group else f"/apis/{self.group}/{self.version}"


KINDS: tuple[KindInfo, ...] = (
    KindInfo("Deployment", "apps", "v1", "deployments", "deployments", "workload"),
    KindInfo("StatefulSet", "apps", "v1", "statefulsets", "statefulsets", "workload"),
    KindInfo("DaemonSet", "apps", "v1", "daemonsets", "daemonsets", "workload"),
    KindInfo("Job", "batch", "v1", "jobs", "jobs", "workload"),
    KindInfo("ReplicaSet", "apps", "v1", "replicasets", "replicasets", "controller"),
    KindInfo("Pod", "", "v1", "pods", "pods", "controller"),
    KindInfo("Service", "", "v1", "services", "services", "network"),
    KindInfo("EndpointSlice", "discovery.k8s.io", "v1", "endpointslices", "endpointslices", "network"),
    KindInfo("Ingress", "networking.k8s.io", "v1", "ingresses", "ingresses", "network"),
    KindInfo("NetworkPolicy", "networking.k8s.io", "v1", "networkpolicies", "networkpolicies", "network"),
    KindInfo("ConfigMap", "", "v1", "configmaps", "configmaps", "config"),
    KindInfo("Secret", "", "v1", "secrets", "secrets", "config", metadata_only=True),
    KindInfo("PersistentVolumeClaim", "", "v1", "persistentvolumeclaims", "persistentvolumeclaims", "storage"),
    KindInfo("HorizontalPodAutoscaler", "autoscaling", "v2", "horizontalpodautoscalers",
             "horizontalpodautoscalers", "autoscaling"),
    KindInfo("ServiceAccount", "", "v1", "serviceaccounts", "serviceaccounts", "identity"),
    KindInfo("RoleBinding", "rbac.authorization.k8s.io", "v1", "rolebindings", "rolebindings", "identity"),
    KindInfo("PodDisruptionBudget", "policy", "v1", "poddisruptionbudgets", "poddisruptionbudgets", "policy"),
)

BY_KIND: dict[str, KindInfo] = {info.kind: info for info in KINDS}
BY_FIELD: dict[str, KindInfo] = {info.field: info for info in KINDS}
WORKLOAD_KINDS: frozenset[str] = frozenset(info.kind for info in KINDS if info.category == "workload")
# Kinds every lab already reads; the rest are opt-in until each lab grants them.
BASE_FIELDS: tuple[str, ...] = ("deployments", "services", "pods", "replicasets", "endpointslices")
# Objects whose changes the validation harness ignores: heartbeats and mirrors, not effects.
IGNORED_KINDS: frozenset[str] = frozenset({"Event", "Lease", "Endpoints", "ControllerRevision"})


def reader_rules(*, include_secrets: bool = True) -> list[dict]:
    """RBAC rules granting list and get on the effect model's kinds, and nothing else."""
    groups: dict[str, list[str]] = {}
    for info in KINDS:
        if info.kind == "Secret" and not include_secrets:
            continue
        groups.setdefault(info.group, []).append(info.plural)
    return [{"apiGroups": [group], "resources": sorted(plurals), "verbs": ["get", "list"]}
            for group, plurals in sorted(groups.items())]
