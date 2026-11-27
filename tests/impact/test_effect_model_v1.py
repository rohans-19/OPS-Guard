"""S3-P4: effect model v1 - kinds, state, topology, graph edges, controller rules, blast vector."""

import pytest

from opsguard.impact.blast import S0, S1, S2, S3, BlastVector
from opsguard.impact.effects import predict_effects, to_impact_estimate
from opsguard.impact.graph import EffectGraph
from opsguard.impact.kinds import KINDS, WORKLOAD_KINDS, reader_rules
from opsguard.impact.rules import Change, PredictedChange, predict
from opsguard.impact.state import ClusterState, ObjectRef
from opsguard.impact.topology import Call, Setting, TrustedTopology, parse_topology

from v1_fixtures import (NS, configmap, deployment, edited, env_ref, hpa, ingress, mount, netpol, pdb, pod,
                         replicaset, restarted, service, with_pods)

ALL_READ = {info.kind for info in KINDS}


def state(*items, read=ALL_READ):
    flat = []
    for item in items:
        flat.extend(item if isinstance(item, list) else [item])
    return ClusterState.from_objects(NS, flat, read=read)


def ref(kind, name):
    return ObjectRef(kind, NS, name)


def run(verb, st, before, after, topology=None):
    return predict(Change(verb, before, after), st, topology)


# --- kinds, state, topology ------------------------------------------------------

def test_kind_registry_and_reader_rules_are_list_and_get_only():
    assert len(KINDS) == 17 and WORKLOAD_KINDS == {"Deployment", "StatefulSet", "DaemonSet", "Job"}
    rules = reader_rules()
    assert all(rule["verbs"] == ["get", "list"] for rule in rules)
    granted = {r for rule in rules for r in rule["resources"]}
    assert granted == {info.plural for info in KINDS}
    assert "secrets" not in {r for rule in reader_rules(include_secrets=False) for r in rule["resources"]}


def test_unread_kind_is_unknown_not_empty():
    st = ClusterState.from_objects(NS, [deployment("a")])
    assert st.knows("Deployment") and not st.knows("PodDisruptionBudget")
    assert ClusterState.from_objects(NS, [], read={"PodDisruptionBudget"}).knows("PodDisruptionBudget")


def test_state_rejects_other_namespaces_and_duplicates():
    other = edited(deployment("a"), ("metadata", "namespace"), "elsewhere")
    with pytest.raises(ValueError):
        ClusterState.from_objects(NS, [other])
    with pytest.raises(ValueError):
        ClusterState.from_objects(NS, [deployment("a"), deployment("a")])


def test_owner_chain_and_scale_body_merge():
    dep = deployment("a", 2)
    st = state(dep, *with_pods(dep, [True, True]))
    assert st.owning_workload(ref("Pod", "a-abc-0")) == ref("Deployment", "a")
    merged = st.apply({"kind": "Scale", "metadata": {"name": "a", "namespace": NS}, "spec": {"replicas": 5}})
    assert merged.get(ref("Deployment", "a"))["spec"]["replicas"] == 5 and ref("Deployment", "a") in merged.stale


def test_topology_parsing_is_strict():
    topo = parse_topology({"schema": "opsguard-topology/v1", "name": "t",
                           "calls": [{"caller": "payments", "service": "checkout"}],
                           "settings": [{"configmap": "s", "key": "mode", "consumer": "payments",
                                         "healthy": ["on"], "degraded": ["off"], "reload": "on_restart"}],
                           "user_facing": ["checkout"]})
    assert topo.callers_of("checkout")[0].dependency == "hard" and topo.settings[0].reload == "on_restart"
    with pytest.raises(ValueError):
        parse_topology({"schema": "opsguard-topology/v1", "evidence": []})
    with pytest.raises(ValueError):
        parse_topology({"schema": "opsguard-topology/v1",
                        "settings": [{"configmap": "s", "key": "k", "consumer": "c", "healthy": ["x"],
                                      "degraded": ["x"]}]})
    with pytest.raises(ValueError):
        parse_topology({"schema": "something-else"})


# --- graph edges -------------------------------------------------------------------

def test_static_dynamic_and_topology_edges():
    secret_volume = {"name": "tls", "secret": {"secretName": "tls"}}
    pvc_volume = {"name": "data", "persistentVolumeClaim": {"claimName": "data"}}
    dep = deployment("api", 2, sa="api-sa", volumes=[mount("settings"), secret_volume, pvc_volume],
                     env=[env_ref("flags", "beta")])
    rs, p0, p1 = with_pods(dep, [True, False])
    slice_ = {"kind": "EndpointSlice", "metadata": {"name": "api-x", "namespace": NS,
                                                    "labels": {"kubernetes.io/service-name": "api"}},
              "endpoints": [{"targetRef": {"kind": "Pod", "name": p0["metadata"]["name"]}, "conditions": {"ready": True}},
                            {"targetRef": {"kind": "Pod", "name": p1["metadata"]["name"]}, "conditions": {"ready": False}}]}
    binding = {"kind": "RoleBinding", "metadata": {"name": "api-rb", "namespace": NS},
               "subjects": [{"kind": "ServiceAccount", "name": "api-sa"}]}
    st = state(dep, rs, p0, p1, service("api"), slice_, ingress("web", "api"), hpa("api-hpa", "api", 1, 4),
               netpol("api-np", {"app": "api"}, []), pdb("api-pdb", {"app": "api"}, minAvailable=1), binding,
               configmap("settings", {"mode": "on"}), configmap("flags", {"beta": "no"}))
    topo = TrustedTopology(calls=(Call("api", "db"),), settings=(Setting("settings", "mode", "api"),))
    graph = EffectGraph(st, topo)
    edges = {(str(s), str(d), data["type"], data["source"]) for s, d, data in graph.edges()}
    expect = {
        ("Deployment/shop/api", "ReplicaSet/shop/api-abc", "owns", "static"),
        ("ConfigMap/shop/settings", "Deployment/shop/api", "mounts", "static"),
        ("ConfigMap/shop/flags", "Deployment/shop/api", "env", "static"),
        ("Secret/shop/tls", "Deployment/shop/api", "mounts", "static"),
        ("PersistentVolumeClaim/shop/data", "Deployment/shop/api", "mounts", "static"),
        ("ServiceAccount/shop/api-sa", "Deployment/shop/api", "uses_sa", "static"),
        ("RoleBinding/shop/api-rb", "ServiceAccount/shop/api-sa", "binds", "static"),
        ("Deployment/shop/api", "Service/shop/api", "selects_template", "static"),
        ("Pod/shop/api-abc-0", "Service/shop/api", "selects", "static"),
        ("EndpointSlice/shop/api-x", "Service/shop/api", "slice_of", "static"),
        ("Service/shop/api", "Ingress/shop/web", "backend", "static"),
        ("HorizontalPodAutoscaler/shop/api-hpa", "Deployment/shop/api", "targets", "static"),
        ("NetworkPolicy/shop/api-np", "Deployment/shop/api", "isolates", "static"),
        ("PodDisruptionBudget/shop/api-pdb", "Deployment/shop/api", "constrains", "static"),
        ("Pod/shop/api-abc-0", "EndpointSlice/shop/api-x", "member", "dynamic"),
        ("Service/shop/db", "Deployment/shop/api", "calls", "topology"),
        ("ConfigMap/shop/settings", "Deployment/shop/api", "reads_setting", "topology"),
    }
    assert expect <= edges
    member = [d for s, t, d in graph.edges(type="member") if str(s) == "Pod/shop/api-abc-1"]
    assert member and member[0]["ready"] is False
    # Constraint edges do not propagate impact.
    assert ref("Deployment", "api") not in graph.reachable(ref("PodDisruptionBudget", "api-pdb"))
    assert ref("Ingress", "web") in graph.reachable(ref("ConfigMap", "settings"))


# --- rollout -----------------------------------------------------------------------

def test_rollout_three_replicas_default_strategy_is_s1():
    dep = deployment("checkout", 3)
    p = run("restart", state(dep, service("checkout")), dep, restarted(dep))
    assert p.service_tiers[ref("Service", "checkout")] == S1 and p.rules == ("rollout",)
    assert p.blast.b4_tier == S1 and not p.uncertain and p.approximate


def test_rollout_one_replica_max_unavailable_one_is_s3():
    dep = deployment("checkout", 1, strategy={"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 1}})
    p = run("restart", state(dep, service("checkout")), dep, restarted(dep))
    assert p.service_tiers[ref("Service", "checkout")] == S3


def test_rollout_recreate_is_s3_and_unknown_strategy_is_uncertain():
    rec = deployment("checkout", 2, strategy={"type": "Recreate"})
    assert run("restart", state(rec, service("checkout")), rec, restarted(rec)).blast.b4_tier == S3
    odd = deployment("checkout", 2, strategy={"type": "BlueGreen"})
    p = run("restart", state(odd, service("checkout")), odd, restarted(odd))
    assert p.uncertain and p.uncertain[0][1].startswith("unknown Deployment strategy")
    zero = deployment("checkout", 2, strategy={"rollingUpdate": {"maxUnavailable": 0, "maxSurge": 0}})
    assert run("restart", state(zero, service("checkout")), zero, restarted(zero)).uncertain


def test_rollout_uses_observed_readiness_with_a_fast_budget():
    """Two replicas, one unready, maxUnavailable 100%: the restart may take every Pod at once."""
    dep = deployment("checkout", 2, strategy={"rollingUpdate": {"maxUnavailable": "100%", "maxSurge": 0}})
    st = state(dep, *with_pods(dep, [True, False]), service("checkout"))
    p = run("restart", st, dep, restarted(dep))
    assert p.service_tiers[ref("Service", "checkout")] == S3 and not p.approximate
    safe = deployment("checkout", 2)  # 25%: maxUnavailable rounds to 0, surge 1
    st = state(safe, *with_pods(safe, [True, False]), service("checkout"))
    assert run("restart", st, safe, restarted(safe)).service_tiers[ref("Service", "checkout")] == S1


def test_rollout_predicts_the_objects_that_change():
    dep = deployment("checkout", 2)
    p = run("restart", state(dep, *with_pods(dep, [True, True]), service("checkout")), dep, restarted(dep))
    kinds = {(c.kind, c.op) for c in p.changed}
    assert {("Deployment", "modify"), ("ReplicaSet", "create"), ("Pod", "any"), ("EndpointSlice", "modify")} <= kinds


def test_controller_status_counts_when_no_pods_were_listed():
    dep = deployment("checkout", 2, ready=0)
    p = run("restart", state(dep, service("checkout")), dep, restarted(dep))
    assert p.service_tiers.get(ref("Service", "checkout"), S0) == S0  # already down: not caused by the change


def test_template_change_adding_a_secret_or_identity_sets_b6_b7():
    dep = deployment("api", 2)
    with_secret = edited(dep, ("spec", "template", "spec", "volumes"), [{"name": "s", "secret": {"secretName": "db"}}])
    assert run("set_image", state(dep), dep, with_secret).blast.b7_secret_path
    other_sa = edited(dep, ("spec", "template", "spec", "serviceAccountName"), "admin")
    assert run("set_image", state(dep), dep, other_sa).blast.b6_privilege


# --- scale -----------------------------------------------------------------------------

def test_scale_down_below_pdb_is_s2_and_to_zero_is_s3():
    dep = deployment("checkout", 3)
    st = state(dep, service("checkout"), pdb("checkout-pdb", {"app": "checkout"}, minAvailable=2))
    p = run("scale", st, dep, edited(dep, ("spec", "replicas"), 1))
    assert p.service_tiers[ref("Service", "checkout")] == S2 and p.pdb_violations == (ref("PodDisruptionBudget", "checkout-pdb"),)
    assert run("scale", st, dep, edited(dep, ("spec", "replicas"), 0)).service_tiers[ref("Service", "checkout")] == S3
    assert run("scale", st, dep, edited(dep, ("spec", "replicas"), 2)).service_tiers[ref("Service", "checkout")] == S1


def test_pdb_max_unavailable_percent():
    dep = deployment("checkout", 4)
    st = state(dep, service("checkout"), pdb("p", {"app": "checkout"}, maxUnavailable="25%"))
    assert run("scale", st, dep, edited(dep, ("spec", "replicas"), 2)).pdb_violations
    assert not run("scale", st, dep, edited(dep, ("spec", "replicas"), 3)).pdb_violations


def test_hpa_overrides_a_manual_scale():
    dep = deployment("checkout", 3)
    st = state(dep, service("checkout"), hpa("checkout-hpa", "checkout", 2, 5))
    p = run("scale", st, dep, edited(dep, ("spec", "replicas"), 1))
    assert "hpa_override" in p.rules and p.service_tiers[ref("Service", "checkout")] == S1
    assert any(c.kind == "HorizontalPodAutoscaler" for c in p.changed)


def test_scale_down_with_unread_pdb_or_hpa_is_uncertain_but_scale_up_is_not():
    dep = deployment("checkout", 3)
    st = state(dep, service("checkout"), read={"Deployment", "Service"})
    down = run("scale", st, dep, edited(dep, ("spec", "replicas"), 2))
    assert {why for _, why in down.uncertain} == {"HorizontalPodAutoscalers were not read",
                                                  "PodDisruptionBudgets were not read"}
    assert not run("scale", st, dep, edited(dep, ("spec", "replicas"), 5)).uncertain


# --- selector --------------------------------------------------------------------------

def test_selector_to_nothing_is_s3_and_to_another_workload_is_s1():
    stable, canary = deployment("stable", 2, labels={"app": "c", "pool": "s"}), deployment("canary", 1, labels={"app": "c", "pool": "k"})
    svc = service("checkout", {"app": "c"})
    st = state(stable, canary, svc)
    gone = run("set_selector", st, svc, edited(svc, ("spec", "selector"), {"app": "none"}))
    assert gone.service_tiers[ref("Service", "checkout")] == S3 and ref("Service", "checkout") in gone.endpoint_changes
    moved = run("set_selector", st, svc, edited(svc, ("spec", "selector"), {"app": "c", "pool": "k"}))
    assert moved.service_tiers[ref("Service", "checkout")] == S1 and moved.blast.b1_workloads == 1


def test_selector_to_unready_pods_is_s3():
    good, bad = deployment("v1", 1, labels={"app": "c", "v": "1"}), deployment("v2", 1, labels={"app": "c", "v": "2"})
    svc = service("checkout", {"app": "c", "v": "1"})
    st = state(good, bad, *with_pods(good, [True]), *with_pods(bad, [False]), svc)
    p = run("set_selector", st, svc, edited(svc, ("spec", "selector"), {"app": "c", "v": "2"}))
    assert p.service_tiers[ref("Service", "checkout")] == S3


def test_empty_selector_is_uncertain():
    svc = service("checkout")
    st = state(deployment("checkout"), svc)
    assert run("set_selector", st, svc, edited(svc, ("spec", "selector"), {})).uncertain


# --- config -----------------------------------------------------------------------------

def _shared(reload="live", symptom="health", payments_env=False):
    checkout = deployment("checkout", 1, volumes=[mount("app-settings")])
    payments = (deployment("payments", 1, env=[env_ref("app-settings", "mode")]) if payments_env
                else deployment("payments", 1, volumes=[mount("app-settings")]))
    cm = configmap("app-settings", {"mode": "legacy"})
    topo = TrustedTopology(settings=(
        Setting("app-settings", "mode", "checkout", frozenset({"enabled", "disabled"}), frozenset({"legacy"})),
        Setting("app-settings", "mode", "payments", frozenset({"legacy", "enabled"}), frozenset({"disabled"}),
                reload, symptom)))
    return state(checkout, payments, cm, service("checkout"), service("payments")), cm, topo


def test_config_healthy_and_degraded_values():
    st, cm, topo = _shared()
    ok = run("set_config", st, cm, edited(cm, ("data", "mode"), "enabled"), topo)
    assert ok.blast.b4_tier == S0 and not ok.uncertain
    bad = run("set_config", st, cm, edited(cm, ("data", "mode"), "disabled"), topo)
    assert bad.service_tiers == {ref("Service", "payments"): S3}


def test_config_unknown_value_is_uncertain():
    st, cm, topo = _shared()
    p = run("set_config", st, cm, edited(cm, ("data", "mode"), "turbo"), topo)
    assert {r.name for r, _ in p.uncertain} == {"checkout", "payments"}


def test_config_read_from_env_is_latent_until_restart():
    st, cm, topo = _shared(payments_env=True)
    p = run("set_config", st, cm, edited(cm, ("data", "mode"), "disabled"), topo)
    assert p.service_tiers.get(ref("Service", "payments"), S0) == S0
    assert p.latent_tiers[ref("Service", "payments")] == S3 and p.blast.b4_tier == S3


def test_config_partial_backend_health_failure_is_s2():
    a = deployment("pay-a", 1, labels={"app": "pay", "z": "a"}, volumes=[mount("s")])
    b = deployment("pay-b", 1, labels={"app": "pay", "z": "b"})
    cm = configmap("s", {"mode": "x"})
    topo = TrustedTopology(settings=(Setting("s", "mode", "pay-a", frozenset({"x"}), frozenset({"y"})),))
    p = run("set_config", state(a, b, cm, service("pay", {"app": "pay"})), cm, edited(cm, ("data", "mode"), "y"), topo)
    assert p.service_tiers[ref("Service", "pay")] == S2


def test_config_key_ref_to_another_key_is_not_affected_but_whole_mount_is_uncertain():
    narrow = deployment("narrow", 1, env=[env_ref("flags", "a")])
    whole = deployment("whole", 1, volumes=[mount("flags")])
    cm = configmap("flags", {"a": "1", "b": "1"})
    p = run("set_config", state(narrow, whole, cm), cm, edited(cm, ("data", "b"), "2"))
    assert {r.name for r, _ in p.uncertain} == {"whole"}


def test_config_without_consumers_has_no_effect():
    cm = configmap("evidence", {"note": "x"})
    p = run("set_config", state(deployment("a"), cm), cm, edited(cm, ("data", "note"), "y"))
    assert p.blast == BlastVector() and not p.uncertain and p.changed[0].kind == "ConfigMap"


# --- delete_pod, hpa bounds, network policy, delete, calls ----------------------------------

def test_delete_pod_single_replica_is_s3_and_recreated():
    dep = deployment("checkout", 1)
    rs, p0 = with_pods(dep, [True])
    st = state(dep, rs, p0, service("checkout"))
    p = predict(Change("delete_pod", p0), st)
    assert p.service_tiers[ref("Service", "checkout")] == S3
    assert any(c.kind == "Pod" and c.op == "any" and c.owner == ref("Deployment", "checkout") for c in p.changed)
    assert p.blast.b8_reversibility == "reversible_with_downtime"


def test_delete_bare_pod_is_irreversible():
    bare = pod(replicaset(deployment("x")), 0)
    bare["metadata"]["ownerReferences"] = []
    assert predict(Change("delete_pod", bare), state(bare)).blast.b8_reversibility == "irreversible"


def test_hpa_bounds_clamp_current_replicas():
    dep = deployment("checkout", 4)
    h = hpa("checkout-hpa", "checkout", 1, 6)
    st = state(dep, h, service("checkout"), pdb("p", {"app": "checkout"}, minAvailable=2))
    p = run("set_hpa_bounds", st, h, edited(h, ("spec", "maxReplicas"), 1))
    assert "hpa_bounds" in p.rules and p.service_tiers[ref("Service", "checkout")] == S2 and p.pdb_violations


def test_network_policy_cutting_off_a_declared_caller():
    checkout, payments = deployment("checkout", 1), deployment("payments", 1)
    allow = netpol("checkout-np", {"app": "checkout"}, [{"from": [{"podSelector": {"matchLabels": {"app": "payments"}}}]}])
    topo = TrustedTopology(calls=(Call("payments", "checkout"),))
    st = state(checkout, payments, service("checkout"), service("payments"), allow)
    cut = edited(allow, ("spec", "ingress"), [{"from": [{"podSelector": {"matchLabels": {"app": "nobody"}}}]}])
    p = run("patch_networkpolicy", st, allow, cut, topo)
    assert p.service_tiers[ref("Service", "payments")] == S2
    deny = edited(allow, ("spec", "ingress"), [])
    assert run("patch_networkpolicy", st, allow, deny, topo).service_tiers[ref("Service", "checkout")] == S3


def test_delete_cascades_through_owner_references():
    dep = deployment("checkout", 2)
    rs, p0, p1 = with_pods(dep, [True, True])
    p = predict(Change("delete", dep), state(dep, rs, p0, p1, service("checkout")))
    deleted = {(c.kind, c.name) for c in p.changed if c.op == "delete"}
    assert deleted == {("Deployment", "checkout"), ("ReplicaSet", "checkout-abc"), ("Pod", "checkout-abc-0"),
                       ("Pod", "checkout-abc-1")}
    assert p.service_tiers[ref("Service", "checkout")] == S3 and p.blast.b8_reversibility == "irreversible"


def test_delete_pvc_is_data_loss_and_rolebinding_is_privilege():
    claim = {"kind": "PersistentVolumeClaim", "metadata": {"name": "data", "namespace": NS}}
    assert predict(Change("delete", claim), state(claim)).blast.b5_data_loss
    binding = {"kind": "RoleBinding", "metadata": {"name": "rb", "namespace": NS}, "subjects": []}
    assert predict(Change("delete", binding), state(binding)).blast.b6_privilege


def test_outage_propagates_to_declared_callers():
    checkout, payments = deployment("checkout", 1), deployment("payments", 1)
    svc = service("checkout")
    topo = TrustedTopology(calls=(Call("payments", "checkout", "hard"),), user_facing=frozenset({"payments"}))
    st = state(checkout, payments, svc, service("payments"))
    p = run("set_selector", st, svc, edited(svc, ("spec", "selector"), {"app": "none"}), topo)
    assert p.service_tiers[ref("Service", "payments")] == S2 and "calls" in p.rules
    assert p.blast.b3_user_facing == 1 and p.blast.b1_workloads == 2


def test_user_facing_through_ingress():
    dep = deployment("web", 1)
    p = run("restart", state(dep, service("web"), ingress("edge", "web")), dep, restarted(dep))
    assert p.blast.b3_user_facing == 1


# --- prediction plumbing ------------------------------------------------------------------

def test_predicted_change_matching():
    owner = ref("Deployment", "a")
    any_pod = PredictedChange("Pod", "any", "rollout", owner=owner)
    assert any_pod.matches("Pod", "a-xyz", "create", frozenset({owner}))
    assert not any_pod.matches("Pod", "b-xyz", "create", frozenset({ref("Deployment", "b")}))
    named = PredictedChange("ConfigMap", "modify", "config", name="s")
    assert named.matches("ConfigMap", "s", "modify", frozenset()) and not named.matches("ConfigMap", "s", "delete", frozenset())


def test_change_validation():
    dep = deployment("a")
    with pytest.raises(ValueError):
        Change("explode", dep, dep)
    with pytest.raises(ValueError):
        Change("restart", dep, None)
    with pytest.raises(ValueError):  # a selector change cannot target a Deployment
        predict(Change("set_selector", dep, dep), state(dep))
    with pytest.raises(ValueError):  # UID mismatch
        predict(Change("restart", dep, edited(restarted(dep), ("metadata", "uid"), "other")), state(dep))


def test_scale_body_and_legacy_estimate():
    dep = deployment("checkout", 3)
    st = state(dep, service("checkout"))
    body = {"kind": "Scale", "metadata": {"name": "checkout", "namespace": NS}, "spec": {"replicas": 0}}
    p = predict_effects(verb="scale", state=st, before=dep, preview=body)
    estimate = to_impact_estimate(p, st)
    assert [s.name for s in estimate.degraded_services] == ["checkout"] and estimate.service_count == 1


def test_pdb_counts_pods_across_every_workload_it_selects():
    """One PDB over two Deployments: each scale-down alone fits, the second one after the first does not."""
    a = deployment("checkout-a", 2, labels={"app": "checkout", "pool": "a"})
    b = deployment("checkout-b", 2, labels={"app": "checkout", "pool": "b"})
    budget = pdb("checkout-pdb", {"app": "checkout"}, minAvailable=3)
    st = state(a, b, service("checkout", {"app": "checkout"}), budget)
    assert not run("scale", st, a, edited(a, ("spec", "replicas"), 1)).pdb_violations
    after_a = st.apply(edited(a, ("spec", "replicas"), 1))
    second = run("scale", after_a, b, edited(b, ("spec", "replicas"), 1))
    assert second.pdb_violations and second.service_tiers[ref("Service", "checkout")] == S2
