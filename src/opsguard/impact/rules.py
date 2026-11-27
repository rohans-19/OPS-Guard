"""Controller-effect rules: what a typed change does after the API server accepts it.

Server dry-run validates the object; it does not predict what controllers do
next. These rules cover that gap for the changes OpsGuard can make, within the
modelled dependencies. Each rule names itself in the prediction, so every
decision can be explained, and the validation harness measures each rule
against real cluster changes.

  rollout        restart, set_image, rollback, set_resources: capacity dip bounded
                 by maxUnavailable and maxSurge (Recreate: full outage)
  scale          remaining ready replicas against PodDisruptionBudgets and the HPA range
  hpa_override   a manual scale outside the HPA range is undone by the HPA
  selector       endpoint set before and after; 0 ready endpoints is S3
  config         each consumer's reload behaviour and its healthy or degraded values
  delete_pod     the owner recreates the Pod; capacity drops by one meanwhile
  hpa_bounds     new bounds clamp the current replica count
  netpol         declared callers that lose access to a Service
  delete         cascading deletion through ownerReferences
  calls          a Service outage degrades declared callers (trusted topology)

Anything the rules cannot settle is reported as uncertainty with a reason,
never assumed safe.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal, Optional

from opsguard.impact.blast import S0, S1, S2, S3, BlastVector, Reversibility, reversibility_rank
from opsguard.impact.graph import EffectGraph, ingress_backends, pod_is_ready, secret_refs
from opsguard.impact.kinds import WORKLOAD_KINDS
from opsguard.impact.state import (ClusterState, Object, ObjectRef, labels, meta, pod_template, ref_of,
                                   selector_matches, spec, status)
from opsguard.impact.topology import TrustedTopology

TEMPLATE_VERBS = frozenset({"restart", "set_image", "rollback", "set_resources"})
VERBS = TEMPLATE_VERBS | {"scale", "set_selector", "set_config", "delete_pod", "set_hpa_bounds",
                          "patch_networkpolicy", "delete"}
Op = Literal["create", "modify", "delete", "any"]


@dataclass(frozen=True)
class Change:
    """One proposed change: the object before, and the previewed object after (None for a delete)."""

    verb: str
    before: Object
    after: Optional[Object] = None

    def __post_init__(self) -> None:
        if self.verb not in VERBS:
            raise ValueError(f"unsupported change: {self.verb!r}")
        if (self.after is None) != (self.verb in ("delete", "delete_pod")):
            raise ValueError("only deletions have no preview object")

    @property
    def target(self) -> ObjectRef:
        return ref_of(self.before)


@dataclass(frozen=True)
class PredictedChange:
    """An object the model expects to change. ``name`` None means any object of the kind under ``owner``."""

    kind: str
    op: Op
    rule: str
    name: Optional[str] = None
    owner: Optional[ObjectRef] = None

    def matches(self, kind: str, name: str, op: str, ancestors: frozenset) -> bool:
        if kind != self.kind or (self.op != "any" and op != self.op):
            return False
        if self.name is not None:
            return name == self.name
        return self.owner is None or self.owner in ancestors


@dataclass(frozen=True)
class EffectPrediction:
    change: Change
    changed: tuple[PredictedChange, ...]
    service_tiers: Mapping[ObjectRef, int]          # immediate worst tier per Service
    latent_tiers: Mapping[ObjectRef, int]           # harm that waits for the next restart
    endpoint_changes: frozenset[ObjectRef]          # Services whose backend set changes
    affected_workloads: frozenset[ObjectRef]
    uncertain: tuple[tuple[ObjectRef, str], ...]
    rules: tuple[str, ...]
    blast: BlastVector
    approximate: bool = False                       # capacity from specs, not observed Pods
    notes: tuple[str, ...] = ()
    pdb_violations: tuple[ObjectRef, ...] = ()

    @property
    def worst_tier(self) -> int:
        return self.blast.b4_tier

    @property
    def affected_services(self) -> frozenset[ObjectRef]:
        tiers = {s for s, t in self.service_tiers.items() if t > S0}
        latent = {s for s, t in self.latent_tiers.items() if t > S0}
        return frozenset(tiers | latent | self.endpoint_changes)

    def summary(self) -> dict:
        return {
            "verb": self.change.verb, "target": str(self.change.target), "rules": list(self.rules),
            "service_tiers": {str(s): f"S{t}" for s, t in sorted(self.service_tiers.items()) if t},
            "latent_tiers": {str(s): f"S{t}" for s, t in sorted(self.latent_tiers.items()) if t},
            "uncertain": [f"{ref}: {why}" for ref, why in self.uncertain],
            "blast": self.blast.as_dict(), "approximate": self.approximate, "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------

def _int(value, default=None):
    return value if type(value) is int and value >= 0 else default


def replicas_of(obj: Object) -> int:
    value = spec(obj).get("replicas", 1)
    if type(value) is not int or value < 0:
        raise ValueError("replica count must be a nonnegative integer")
    return value


def scaled_value(value, total: int, *, round_up: bool) -> Optional[int]:
    """An int-or-percent field resolved the way the controllers resolve it, or None when invalid."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str) and value.endswith("%") and value[:-1].isdigit():
        scaled = total * int(value[:-1])
        return -(-scaled // 100) if round_up else scaled // 100
    return None


class _Capacity:
    """Ready replicas per workload: observed Pods when they were read, else the spec (approximate)."""

    def __init__(self, state: ClusterState, graph: EffectGraph):
        self.state, self.graph = state, graph
        self.observed = state.knows("Pod")
        self.approximate = False
        self._ready: dict[ObjectRef, int] = {}

    def ready(self, workload: ObjectRef) -> int:
        """Observed Pods first; then the controller's status; then the spec (approximate).

        A workload changed earlier in the episode is stale in the snapshot, so its spec is used.
        Kubernetes omits ``readyReplicas`` when it is 0, so a status the controller has observed
        (``observedGeneration``) without it means 0 ready.
        """
        if workload not in self._ready:
            obj = self.state.get(workload)
            pods = self.graph.pods_of(workload) if self.observed else set()
            st = status(obj) if obj is not None else {}
            if obj is None:
                value = 0
            elif workload in self.state.stale:
                self.approximate = True
                value = _desired(obj)
            elif pods:
                value = sum(1 for p in pods if pod_is_ready(self.state.get(p)))
            elif "observedGeneration" in st:
                field = "numberReady" if obj.get("kind") == "DaemonSet" else "readyReplicas"
                value = _int(st.get(field), 0)
            else:
                self.approximate = True
                value = _desired(obj)
            self._ready[workload] = value
        return self._ready[workload]


def _desired(obj: Object) -> int:
    if obj.get("kind") == "DaemonSet":
        return _int(status(obj).get("numberReady"), 1)
    if obj.get("kind") == "Job":
        return 0
    return replicas_of(obj)


def _backends(state: ClusterState, graph: EffectGraph, service: Object, pods_known: bool) -> set[ObjectRef]:
    """Workloads behind a Service: owners of selected live Pods, plus template matches."""
    selector = spec(service).get("selector") or {}
    if not selector or spec(service).get("type") == "ExternalName":
        return set()
    out = {ref for ref, obj in state.workloads().items() if selector_matches(selector, labels(pod_template(obj)))}
    if pods_known:
        for pod, obj in state.items("Pod").items():
            if selector_matches(selector, labels(obj)):
                owner = state.owning_workload(pod)
                if owner is not None:
                    out.add(owner)
    return out


def _tier_from_capacity(before: int, after: int) -> int:
    if before == 0:
        return S0           # already down: the change does not cause it
    if after <= 0:
        return S3
    return S1 if after < before else S0


# ---------------------------------------------------------------------------
# The model
# ---------------------------------------------------------------------------

class _Builder:
    def __init__(self, change: Change, state: ClusterState, topology: TrustedTopology):
        self.change, self.state, self.topology = change, state, topology
        self.graph = EffectGraph(state, topology)
        self.capacity = _Capacity(state, self.graph)
        self.changed: list[PredictedChange] = []
        self.tiers: dict[ObjectRef, int] = {}
        self.latent: dict[ObjectRef, int] = {}
        self.endpoint_changes: set[ObjectRef] = set()
        self.workloads: set[ObjectRef] = set()
        self.uncertain: list[tuple[ObjectRef, str]] = []
        self.rules: list[str] = []
        self.notes: list[str] = []
        self.pdb: list[ObjectRef] = []
        self.deleted: set[ObjectRef] = set()
        self.data_loss = self.privilege = self.secret = False
        self.reversibility: Reversibility = "reversible"

    # -- helpers ----------------------------------------------------------------
    def rule(self, name: str) -> None:
        if name not in self.rules:
            self.rules.append(name)

    def expect(self, kind, op, rule, *, name=None, owner=None) -> None:
        item = PredictedChange(kind, op, rule, name, owner)
        if item not in self.changed:
            self.changed.append(item)

    def unsure(self, ref: ObjectRef, why: str) -> None:
        if (ref, why) not in self.uncertain:
            self.uncertain.append((ref, why))

    def tier(self, service: ObjectRef, value: int, *, latent=False) -> None:
        pool = self.latent if latent else self.tiers
        pool[service] = max(pool.get(service, S0), value)

    def worse_reversibility(self, value: Reversibility) -> None:
        if reversibility_rank(value) > reversibility_rank(self.reversibility):
            self.reversibility = value

    def services_of(self, workload: ObjectRef) -> set[ObjectRef]:
        return {s for s in self.graph.services_of(workload) if self.state.get(s) is not None}

    def expect_pod_churn(self, workload: ObjectRef, rule: str, *, new_replicaset: bool) -> None:
        self.expect(workload.kind, "modify", rule, name=workload.name)
        if workload.kind == "Deployment":
            self.expect("ReplicaSet", "any", rule, owner=workload)
            if new_replicaset:
                self.expect("ReplicaSet", "create", rule, owner=workload)
        self.expect("Pod", "any", rule, owner=workload)
        for service in self.services_of(workload):
            self.expect("EndpointSlice", "modify", rule, owner=service)

    def service_capacity(self, service: ObjectRef, override: Mapping[ObjectRef, int],
                         state: Optional[ClusterState] = None) -> int:
        state = state or self.state
        obj = state.get(service)
        backends = _backends(state, self.graph, obj, self.capacity.observed) if obj else set()
        return sum(override.get(w, self.capacity.ready(w)) for w in backends)

    def capacity_effect(self, workload: ObjectRef, after: int, rule: str, *, latent=False) -> None:
        for service in self.services_of(workload):
            before = self.service_capacity(service, {})
            now = self.service_capacity(service, {workload: after})
            self.tier(service, _tier_from_capacity(before, now), latent=latent)

    # -- rules --------------------------------------------------------------------
    def rollout(self) -> None:
        target, before, after = self.change.target, self.change.before, self.change.after
        if spec(before).get("template") == spec(after).get("template") and replicas_of(before) == replicas_of(after):
            self.notes.append("template unchanged: no rollout")
            return
        self.rule("rollout")
        self.workloads.add(target)
        ready = self.capacity.ready(target)
        replicas = replicas_of(after)
        if target.kind == "Deployment":
            strategy = spec(after).get("strategy") or {}
            kind = strategy.get("type", "RollingUpdate")
            if kind == "Recreate":
                minimum = 0
            elif kind == "RollingUpdate":
                rolling = strategy.get("rollingUpdate") or {}
                unavailable = scaled_value(rolling.get("maxUnavailable", "25%"), replicas, round_up=False)
                surge = scaled_value(rolling.get("maxSurge", "25%"), replicas, round_up=True)
                if unavailable is None or surge is None or (unavailable == 0 and surge == 0):
                    self.unsure(target, "rolling-update budget the controller would reject")
                    return
                # The controller never removes available Pods below replicas - maxUnavailable;
                # unavailable Pods can go first. A failing new template stalls the rollout.
                minimum = max(min(ready, replicas - unavailable), 0)
            else:
                self.unsure(target, f"unknown Deployment strategy {kind!r}")
                return
            self.expect_pod_churn(target, "rollout", new_replicaset=True)
        elif target.kind == "StatefulSet":
            strategy = (spec(after).get("updateStrategy") or {}).get("type", "RollingUpdate")
            if strategy == "OnDelete":
                self.notes.append("OnDelete StatefulSet: Pods change only when deleted")
                self.expect("StatefulSet", "modify", "rollout", name=target.name)
                return
            minimum = max(ready - 1, 0)          # one Pod at a time, highest ordinal first
            self.expect_pod_churn(target, "rollout", new_replicaset=False)
        elif target.kind == "DaemonSet":
            minimum = max(ready - 1, 0)          # default maxUnavailable 1; node count unknown
            self.expect_pod_churn(target, "rollout", new_replicaset=False)
        else:
            self.unsure(target, f"{target.kind} templates are immutable or not modelled")
            return
        self.capacity_effect(target, minimum, "rollout")
        for service in self.services_of(target):
            if self.service_capacity(service, {}) > 0 and self.tiers.get(service, S0) == S0:
                self.tier(service, S1)    # Pods are replaced: connections reset, capacity briefly lower
        if self.change.verb in ("set_image", "set_resources", "rollback"):
            self.notes.append("new template health is unknown; capacity stays bounded by the rollout budget")
        if (spec(pod_template(after)).get("serviceAccountName") != spec(pod_template(before)).get("serviceAccountName")
                or spec(pod_template(after)).get("automountServiceAccountToken")
                != spec(pod_template(before)).get("automountServiceAccountToken")):
            self.privilege = True
        if secret_refs(spec(pod_template(after))) - secret_refs(spec(pod_template(before))):
            self.secret = True

    def _hpa_for(self, workload: ObjectRef) -> Optional[tuple[ObjectRef, Object]]:
        for ref, hpa in self.state.items("HorizontalPodAutoscaler").items():
            target = spec(hpa).get("scaleTargetRef") or {}
            if target.get("kind") == workload.kind and target.get("name") == workload.name:
                return ref, hpa
        return None

    def _pdb_required(self, workload: ObjectRef, replicas: int) -> list[tuple[ObjectRef, int]]:
        out = []
        obj = self.state.get(workload)
        for ref, pdb in self.state.items("PodDisruptionBudget").items():
            if not selector_matches(spec(pdb).get("selector") or {}, labels(pod_template(obj))):
                continue
            if "minAvailable" in spec(pdb):
                required = scaled_value(spec(pdb)["minAvailable"], replicas, round_up=True)
            elif "maxUnavailable" in spec(pdb):
                unavailable = scaled_value(spec(pdb)["maxUnavailable"], replicas, round_up=True)
                required = None if unavailable is None else max(replicas - unavailable, 0)
            else:
                required = None
            if required is None:
                self.unsure(ref, "PodDisruptionBudget without a valid minAvailable or maxUnavailable")
                continue
            out.append((ref, required))
        return out

    def scale_to(self, workload: ObjectRef, old: int, new: int, rule: str) -> None:
        """Replica change on a workload, with the HPA and PodDisruptionBudget checks."""
        self.rule(rule)
        effective = new
        if self.state.knows("HorizontalPodAutoscaler"):
            hpa = self._hpa_for(workload)
            if hpa is not None:
                ref, obj = hpa
                low = _int(spec(obj).get("minReplicas"), 1)
                high = _int(spec(obj).get("maxReplicas"), new)
                effective = min(max(new, low), high)
                if effective != new:
                    self.rule("hpa_override")
                    self.notes.append(f"{ref.name} keeps replicas in [{low}, {high}]: the scale to {new} "
                                      f"is overridden to {effective}")
                    self.expect("HorizontalPodAutoscaler", "modify", "hpa_override", name=ref.name)
        elif new < old:
            self.unsure(workload, "HorizontalPodAutoscalers were not read")
        if effective == old:
            self.notes.append("replica count unchanged after the HPA")
            if effective != new:
                self.expect(workload.kind, "modify", rule, name=workload.name)
            return
        self.workloads.add(workload)
        self.expect_pod_churn(workload, rule, new_replicaset=False)
        ready = self.capacity.ready(workload)
        after = min(ready, effective)            # scale-down removes unready Pods first
        if effective < old:
            self.capacity_effect(workload, after, rule)
            if self.state.knows("PodDisruptionBudget"):
                for ref, required in self._pdb_required(workload, old):
                    if after < required:
                        self.rule("pdb")
                        self.pdb.append(ref)
                        self.notes.append(f"{after} ready replicas would be below {ref.name}'s {required}")
                        for service in self.services_of(workload):
                            self.tier(service, max(S2, S3 if after == 0 else S2))
            else:
                self.unsure(workload, "PodDisruptionBudgets were not read")

    def scale(self) -> None:
        target = self.change.target
        old = replicas_of(self.state.get(target) or self.change.before)
        self.scale_to(target, old, replicas_of(self.change.after), "scale")

    def selector(self) -> None:
        service, before, after = self.change.target, self.change.before, self.change.after
        self.rule("selector")
        old_sel, new_sel = spec(before).get("selector") or {}, spec(after).get("selector") or {}
        if old_sel == new_sel:
            self.notes.append("selector unchanged")
            return
        if not new_sel:
            self.unsure(service, "a selectorless Service takes manually managed endpoints")
            return
        self.expect("Service", "modify", "selector", name=service.name)
        self.expect("EndpointSlice", "any", "selector", owner=service)
        pods_known = self.capacity.observed
        old_backends = _backends(self.state, self.graph, before, pods_known)
        new_backends = _backends(self.state, self.graph, after, pods_known)
        before_cap = sum(self.capacity.ready(w) for w in old_backends)
        after_cap = sum(self.capacity.ready(w) for w in new_backends)
        if old_backends != new_backends:
            self.endpoint_changes.add(service)
            self.workloads |= old_backends ^ new_backends
        tier = _tier_from_capacity(before_cap, after_cap)
        if tier == S0 and old_backends != new_backends and new_backends:
            tier = S1                            # traffic now reaches different Pods
        self.tier(service, tier)

    def config(self) -> None:
        cm, before, after = self.change.target, self.change.before, self.change.after
        old, new = before.get("data") or {}, after.get("data") or {}
        if (before.get("binaryData") or {}) != (after.get("binaryData") or {}):
            self.unsure(cm, "binaryData changed")
        changed = {k: new.get(k) for k in set(old) | set(new) if old.get(k) != new.get(k)}
        if not changed:
            self.notes.append("data unchanged")
            return
        self.rule("config")
        self.expect("ConfigMap", "modify", "config", name=cm.name)
        consumers = {w for w in self.graph.successors(cm, "mounts", "env", "reads_setting")
                     if w.kind in WORKLOAD_KINDS}
        by_env = {w for w in self.graph.successors(cm, "env") if w.kind in WORKLOAD_KINDS}
        mounted = {w for w in self.graph.successors(cm, "mounts") if w.kind in WORKLOAD_KINDS}
        for key, value in sorted(changed.items(), key=lambda kv: kv[0]):
            settings = self.topology.settings_for(cm.name, key)
            declared = {s.consumer for s in settings}
            for setting in settings:
                workload = next((w for w in consumers if w.name == setting.consumer), None)
                if workload is None or self.state.get(workload) is None:
                    continue
                if value in setting.healthy:
                    continue
                self.workloads.add(workload)
                if value not in setting.degraded:
                    self.unsure(workload, f"{cm.name}/{key}={value!r} is neither a healthy nor a degraded value")
                    continue
                latent = setting.reload == "on_restart" or (workload in by_env and workload not in mounted)
                self._degrade(workload, setting.symptom, latent=latent, rule="config")
            for workload in sorted(consumers):
                if workload.name in declared or self.state.get(workload) is None:
                    continue
                if not self._reads_key(workload, cm.name, key):
                    continue
                self.workloads.add(workload)
                self.unsure(workload, f"no trusted rule says how {workload.name} reacts to {cm.name}/{key}")

    def _reads_key(self, workload: ObjectRef, configmap: str, key: str) -> bool:
        """False only when the workload provably reads other keys of this ConfigMap (configMapKeyRef only)."""
        pod = spec(pod_template(self.state.get(workload)))
        whole = False
        keys = set()
        for volume in pod.get("volumes") or ():
            if (volume.get("configMap") or {}).get("name") == configmap:
                items = volume["configMap"].get("items")
                if not items:
                    whole = True
                keys |= {i.get("key") for i in items or ()}
            for source in (volume.get("projected") or {}).get("sources") or ():
                if (source.get("configMap") or {}).get("name") == configmap:
                    whole = whole or not source["configMap"].get("items")
                    keys |= {i.get("key") for i in source["configMap"].get("items") or ()}
        for group in ("initContainers", "containers"):
            for container in pod.get(group) or ():
                for source in container.get("envFrom") or ():
                    if (source.get("configMapRef") or {}).get("name") == configmap:
                        whole = True
                for env in container.get("env") or ():
                    ref = (env.get("valueFrom") or {}).get("configMapKeyRef") or {}
                    if ref.get("name") == configmap:
                        keys.add(ref.get("key"))
        return whole or key in keys

    def _degrade(self, workload: ObjectRef, symptom: str, *, latent: bool, rule: str) -> None:
        self.rule(rule)
        if not latent and symptom in ("readiness", "crash"):
            self.expect("Pod", "modify", rule, owner=workload)
            for service in self.services_of(workload):
                self.expect("EndpointSlice", "modify", rule, owner=service)
        for service in self.services_of(workload):
            total = self.service_capacity(service, {})
            failing = self.capacity.ready(workload)
            if total == 0:
                continue
            if symptom == "health":
                tier = S3 if failing >= total else S2
            else:
                tier = S3 if failing >= total else S1
            self.tier(service, tier, latent=latent)
        if latent:
            self.notes.append(f"{workload.name} picks up the value at its next restart")

    def delete_pod(self) -> None:
        pod = self.change.target
        self.rule("delete_pod")
        self.expect("Pod", "delete", "delete_pod", name=pod.name)
        owner = self.state.owning_workload(pod)
        if owner is None:
            self.deleted.add(pod)
            self.worse_reversibility("irreversible")
            self.notes.append("a Pod without a controller is not recreated")
            return
        self.workloads.add(owner)
        self.expect("Pod", "create", "delete_pod", owner=owner)
        if owner.kind == "Deployment":
            self.expect("ReplicaSet", "modify", "delete_pod", owner=owner)
        for service in self.services_of(owner):
            self.expect("EndpointSlice", "modify", "delete_pod", owner=service)
        ready = self.capacity.ready(owner)
        was_ready = pod_is_ready(self.change.before) if self.capacity.observed else True
        self.capacity_effect(owner, max(ready - (1 if was_ready else 0), 0), "delete_pod")
        if owner.name in self.topology.stateful_workloads():
            pod_spec = spec(self.change.before)
            if any(v.get("emptyDir") is not None for v in pod_spec.get("volumes") or ()):
                self.data_loss = True

    def hpa_bounds(self) -> None:
        hpa, before, after = self.change.target, self.change.before, self.change.after
        self.rule("hpa_bounds")
        self.expect("HorizontalPodAutoscaler", "modify", "hpa_bounds", name=hpa.name)
        target = spec(after).get("scaleTargetRef") or {}
        workload = ObjectRef(target.get("kind", "Deployment"), hpa.namespace, target.get("name", "?"))
        obj = self.state.get(workload)
        if obj is None:
            self.unsure(workload, "the HPA's target is not in the inventory")
            return
        low, high = _int(spec(after).get("minReplicas"), 1), _int(spec(after).get("maxReplicas"))
        if high is None or low > high:
            self.unsure(hpa, "invalid HPA bounds")
            return
        current = replicas_of(obj)
        clamped = min(max(current, low), high)
        if clamped != current:
            # The new bounds act like a scale; the HPA is the one making it.
            state_without = ClusterState(self.state.namespace,
                                         {k: v for k, v in self.state.objects.items()
                                          if k != "HorizontalPodAutoscaler"},
                                         self.state.read, self.state.stale)
            saved = self.state
            self.state = state_without
            try:
                self.scale_to(workload, current, clamped, "hpa_bounds")
            finally:
                self.state = saved
        elif low < (_int(spec(before).get("minReplicas"), 1)):
            self.notes.append(f"the HPA may later scale {workload.name} down to {low}")
            if self.state.knows("PodDisruptionBudget"):
                for ref, required in self._pdb_required(workload, current):
                    if low < required:
                        for service in self.services_of(workload):
                            self.tier(service, S2, latent=True)

    def netpol(self) -> None:
        policy, before, after = self.change.target, self.change.before, self.change.after
        self.rule("netpol")
        self.expect("NetworkPolicy", "modify", "netpol", name=policy.name)
        types = spec(after).get("policyTypes") or ["Ingress"]
        if "Ingress" not in types:
            self.notes.append("egress-only policy: callers are not modelled for egress")
            self.unsure(policy, "egress rules are not modelled")
            return
        selector = spec(after).get("podSelector") or {}
        everything = not (selector.get("matchLabels") or selector.get("matchExpressions"))
        selected = {w for w, obj in self.state.workloads().items()
                    if everything or selector_matches(selector, labels(pod_template(obj)))}
        for workload in sorted(selected):
            for service in self.services_of(workload):
                callers = self.topology.callers_of(service.name)
                for call in callers:
                    caller = next((w for w in self.state.workloads() if w.name == call.caller), None)
                    if caller is None:
                        continue
                    allowed_before = _peer_allowed(before, self.state.get(caller))
                    allowed_after = _peer_allowed(after, self.state.get(caller))
                    if allowed_before and not allowed_after:
                        self.workloads |= {workload, caller}
                        for caller_service in self.services_of(caller):
                            self.tier(caller_service, S2 if call.dependency == "hard" else S1)
                        self.notes.append(f"{call.caller} can no longer reach {service.name}")
                if any(rule.get("ports") for rule in spec(after).get("ingress") or ()):
                    self.unsure(service, "port-level rules are not modelled")
                if not spec(after).get("ingress"):
                    self.tier(service, S3)       # deny all ingress
                    self.workloads.add(workload)

    def delete(self) -> None:
        target = self.change.target
        self.rule("delete")
        queue, seen = [target], set()
        while queue:
            ref = queue.pop()
            if ref in seen:
                continue
            seen.add(ref)
            self.deleted.add(ref)
            self.expect(ref.kind, "delete", "delete", name=ref.name)
            queue.extend(self.graph.successors(ref, "owns"))
        for ref in sorted(self.deleted):
            if ref.kind in WORKLOAD_KINDS:
                self.workloads.add(ref)
                self.capacity_effect(ref, 0, "delete")
            elif ref.kind == "Pod":
                owner = self.state.owning_workload(ref)
                if owner is not None and owner not in self.deleted:
                    self.capacity_effect(owner, max(self.capacity.ready(owner) - 1, 0), "delete")
            elif ref.kind == "Service":
                if self.service_capacity(ref, {}) > 0:
                    self.tier(ref, S3)
            elif ref.kind in ("ConfigMap", "Secret"):
                for consumer in self.graph.successors(ref, "mounts", "env", "reads_setting"):
                    if consumer.kind in WORKLOAD_KINDS and self.state.get(consumer) is not None:
                        self.workloads.add(consumer)
                        self._degrade(consumer, "crash", latent=True, rule="delete")
            elif ref.kind == "PersistentVolumeClaim":
                self.data_loss = True
            elif ref.kind in ("RoleBinding", "ServiceAccount"):
                self.privilege = True
        if target.kind == "Secret":
            self.secret = True
        owned = bool(self.state.owner_refs(self.change.before))
        self.worse_reversibility("reversible_with_downtime" if owned else "irreversible")

    def propagate_calls(self) -> None:
        """A Service outage degrades declared callers; repeat until nothing changes (bounded)."""
        for _ in range(10):
            grew = False
            for pool, latent in ((self.tiers, False), (self.latent, True)):
                for service, tier in list(pool.items()):
                    if tier < S3:
                        continue
                    for call in self.topology.callers_of(service.name):
                        caller = next((w for w in self.state.workloads() if w.name == call.caller), None)
                        if caller is None:
                            continue
                        effect = S2 if call.dependency == "hard" else S1
                        for caller_service in self.services_of(caller):
                            if pool.get(caller_service, S0) < effect:
                                pool[caller_service] = effect
                                self.workloads.add(caller)
                                self.rule("calls")
                                grew = True
            if not grew:
                return

    # -- result ---------------------------------------------------------------------
    def build(self) -> EffectPrediction:
        verb = self.change.verb
        kind = self.change.target.kind
        expected = {"scale": WORKLOAD_KINDS | {"ReplicaSet"}, "set_selector": {"Service"}, "set_config": {"ConfigMap"},
                    "delete_pod": {"Pod"}, "set_hpa_bounds": {"HorizontalPodAutoscaler"},
                    "patch_networkpolicy": {"NetworkPolicy"}}.get(verb)
        if verb in TEMPLATE_VERBS:
            expected = WORKLOAD_KINDS
        if expected is not None and kind not in expected:
            raise ValueError(f"{verb} cannot target a {kind}")
        if self.change.after is not None and ref_of(self.change.after) != self.change.target:
            raise ValueError("the preview names a different object")
        uids = {meta(o).get("uid") for o in (self.change.before, self.change.after, self.state.get(self.change.target))
                if o is not None}
        if len(uids - {None}) > 1:
            raise ValueError("the preview's UID does not match the object")
        if verb != "delete" and self.state.get(self.change.target) is None:
            raise ValueError(f"{self.change.target} is not in the inventory")
        if kind in ("RoleBinding", "ServiceAccount"):
            self.privilege = True
        if kind == "Secret":
            self.secret = True
        {"scale": self.scale, "set_selector": self.selector, "set_config": self.config,
         "delete_pod": self.delete_pod, "set_hpa_bounds": self.hpa_bounds,
         "patch_networkpolicy": self.netpol, "delete": self.delete}.get(verb, self.rollout)()
        self.propagate_calls()

        services = {**{s: t for s, t in self.tiers.items()}}
        worst = max([S0, *self.tiers.values(), *self.latent.values()])
        affected_services = ({s for s, t in self.tiers.items() if t} | {s for s, t in self.latent.items() if t}
                             | self.endpoint_changes)
        user_facing = set(self.topology.user_facing)
        for ingress in self.state.items("Ingress").values():
            user_facing |= ingress_backends(ingress)
        refs = affected_services | self.workloads | self.deleted | {self.change.target}
        if worst >= S2:
            self.worse_reversibility("reversible_with_downtime")
        if self.data_loss:
            self.worse_reversibility("irreversible")
        blast = BlastVector(
            b1_workloads=len({w for w in self.workloads if w.kind in WORKLOAD_KINDS}),
            b2_namespaces=len({r.namespace for r in refs}) if (affected_services or self.workloads or self.deleted)
            else 0,
            b3_user_facing=len({s for s in affected_services if s.name in user_facing}),
            b4_tier=worst, b5_data_loss=self.data_loss, b6_privilege=self.privilege,
            b7_secret_path=self.secret, b8_reversibility=self.reversibility)
        return EffectPrediction(
            change=self.change, changed=tuple(self.changed), service_tiers=services,
            latent_tiers=dict(self.latent), endpoint_changes=frozenset(self.endpoint_changes),
            affected_workloads=frozenset(self.workloads), uncertain=tuple(self.uncertain),
            rules=tuple(self.rules), blast=blast, approximate=self.capacity.approximate,
            notes=tuple(self.notes), pdb_violations=tuple(self.pdb))


def _peer_allowed(policy: Object, caller: Optional[Object]) -> bool:
    """Whether a same-namespace caller's Pods match any ingress ``from`` peer (ports ignored)."""
    if caller is None:
        return False
    caller_labels = labels(pod_template(caller))
    for rule in spec(policy).get("ingress") or ():
        peers = rule.get("from")
        if not peers:
            return True                          # a rule with no peers allows everyone
        for peer in peers:
            if peer.get("ipBlock") or peer.get("namespaceSelector"):
                continue
            selector = peer.get("podSelector")
            if selector is not None and (not (selector.get("matchLabels") or selector.get("matchExpressions"))
                                         or selector_matches(selector, caller_labels)):
                return True
    return False


def predict(change: Change, state: ClusterState, topology: Optional[TrustedTopology] = None) -> EffectPrediction:
    """Effect model v1: what this change is predicted to do, within the modelled dependencies."""
    return _Builder(change, state, topology or TrustedTopology()).build()
