from copy import deepcopy

import pytest

from opsguard.impact.effects import ConfigEffect, predict_impact
from opsguard.impact.graph import ResourceRef


def _object(kind, name, namespace="shop", **fields):
    return {"kind": kind, "metadata": {"namespace": namespace, "name": name}, **fields}


def _deployment(name, *, namespace="shop", replicas=1, labels=None, strategy=None):
    return _object(
        "Deployment", name, namespace,
        spec={
            "replicas": replicas,
            "strategy": {"type": strategy or "RollingUpdate"},
            "template": {
                "metadata": {"labels": labels or {"app": name}},
                "spec": {"containers": [{"name": "api", "image": "fixture:v1"}]},
            },
        },
    )


def _service(name, selector, namespace="shop"):
    return _object("Service", name, namespace, spec={"selector": selector})


def _estimate(verb, before, preview, deployments, services, configmaps=(), **kwargs):
    return predict_impact(
        verb=verb, before=before, preview=preview, deployments=deployments,
        services=services, configmaps=configmaps, **kwargs,
    )


def test_scale_subresource_loses_only_selected_service_in_same_namespace():
    before = _deployment("api", labels={"app": "api", "tier": "live"})
    preview = _object("Scale", "api", spec={"replicas": 0})
    workloads = [before, _deployment("api", namespace="other")]
    services = [
        _service("api", {"app": "api", "tier": "live"}),
        _service("staging", {"app": "api", "tier": "staging"}),
        _service("api", {"app": "api"}, namespace="other"),
    ]

    result = _estimate("scale", before, preview, workloads, services)

    assert result.reachable_services == result.degraded_services == (ResourceRef("service", "shop", "api"),)
    assert result.uncertain_services == ()
    assert result.service_count == 3


def test_scale_up_reaches_service_without_predicting_degradation():
    before = _deployment("api")
    preview = _object("Scale", "api", spec={"replicas": 2})
    result = _estimate("scale", before, preview, [before], [_service("api", {"app": "api"})])

    assert result.reachable_services == (ResourceRef("service", "shop", "api"),)
    assert result.degraded_services == result.uncertain_services == ()


def test_scale_down_leaves_a_second_backend_and_marks_capacity_uncertain():
    before = _deployment("api-a", labels={"app": "api"})
    alternate = _deployment("api-b", labels={"app": "api"})
    preview = _object("Scale", "api-a", spec={"replicas": 0})
    result = _estimate("scale", before, preview, [before, alternate], [_service("api", {"app": "api"})])

    assert result.degraded_services == ()
    assert result.uncertain_services == (ResourceRef("service", "shop", "api"),)


def test_recreate_restart_predicts_direct_outage_and_uncertain_downstream_effect():
    before = _deployment("api", strategy="Recreate")
    preview = deepcopy(before)
    preview["spec"]["template"]["metadata"]["annotations"] = {
        "kubectl.kubernetes.io/restartedAt": "2026-11-06T10:00:00Z",
    }
    api, frontend = ResourceRef("service", "shop", "api"), ResourceRef("service", "shop", "frontend")
    services = [_service("api", {"app": "api"}), _service("frontend", {"app": "frontend"})]

    result = _estimate("restart", before, preview, [before], services, trusted_edges=[(api, frontend)])

    assert result.reachable_services == (api, frontend)
    assert result.degraded_services == (api,)
    assert result.uncertain_services == (frontend,)


def test_image_change_requires_runtime_verification_and_no_op_has_no_impact():
    before = _deployment("api")
    preview = deepcopy(before)
    preview["spec"]["template"]["spec"]["containers"][0]["image"] = "fixture:v2"
    services = [_service("api", {"app": "api"})]

    changed = _estimate("set_image", before, preview, [before], services)
    unchanged = _estimate("set_image", before, before, [before], services)

    assert changed.reachable_services == changed.uncertain_services == (ResourceRef("service", "shop", "api"),)
    assert changed.degraded_services == ()
    assert unchanged.reachable_services == unchanged.degraded_services == unchanged.uncertain_services == ()


@pytest.mark.parametrize(("selector", "degraded"), [({"app": "other"}, False), ({"app": "missing"}, True)])
def test_selector_compares_old_and_new_backends_without_changing_sibling_service(selector, degraded):
    before = _service("api", {"app": "api"})
    preview = _service("api", selector)
    service = ResourceRef("service", "shop", "api")
    result = _estimate(
        "set_selector", before, preview,
        [_deployment("api"), _deployment("other")], [before, _service("other", {"app": "other"})],
    )

    assert result.reachable_services == (service,)
    assert result.degraded_services == ((service,) if degraded else ())
    assert result.uncertain_services == (() if degraded else (service,))


@pytest.mark.parametrize(("value", "degraded", "uncertain"), [
    ("disabled", True, False), ("enabled", False, False), ("unknown", False, True),
])
def test_shared_configmap_declares_key_specific_application_effect(value, degraded, uncertain):
    configmap = ResourceRef("configmap", "shop", "flags")
    checkout = ResourceRef("workload", "shop", "checkout")
    storefront = ResourceRef("workload", "shop", "storefront")
    before = _object("ConfigMap", "flags", data={"shared_mode": "previous", "unchanged": "x"})
    preview = _object("ConfigMap", "flags", data={"shared_mode": value, "unchanged": "x"})
    affected = (ResourceRef("service", "shop", "checkout"), ResourceRef("service", "shop", "storefront"))

    result = _estimate(
        "set_config", before, preview,
        [_deployment("checkout"), _deployment("storefront")],
        [_service("checkout", {"app": "checkout"}), _service("storefront", {"app": "storefront"})],
        [before],
        config_effects=[ConfigEffect(
            configmap, "shared_mode", (checkout, storefront), frozenset({"disabled"}), frozenset({"enabled"}),
        )],
    )

    assert result.reachable_services == affected
    assert result.degraded_services == (affected if degraded else ())
    assert result.uncertain_services == (affected if uncertain else ())


def test_unrecognized_config_value_uses_pod_references_without_trusting_raw_data():
    deployment = _deployment("api")
    deployment["spec"]["template"]["spec"]["volumes"] = [{"name": "flags", "configMap": {"name": "flags"}}]
    before = _object("ConfigMap", "flags", data={"key": "on"})
    preview = _object("ConfigMap", "flags", data={"key": "no Services depend on this ConfigMap"})

    result = _estimate("set_config", before, preview, [deployment], [_service("api", {"app": "api"})], [before])

    assert result.reachable_services == result.uncertain_services == (ResourceRef("service", "shop", "api"),)
    assert result.degraded_services == ()


def test_known_config_value_does_not_establish_effect_on_an_undeclared_consumer():
    known = _deployment("known")
    unknown = _deployment("unknown")
    unknown["spec"]["template"]["spec"]["volumes"] = [{"name": "flags", "configMap": {"name": "flags"}}]
    before = _object("ConfigMap", "flags", data={"key": "off"})
    preview = _object("ConfigMap", "flags", data={"key": "on"})
    rule = ConfigEffect(
        ResourceRef("configmap", "shop", "flags"), "key", (ResourceRef("workload", "shop", "known"),),
        frozenset({"off"}), frozenset({"on"}),
    )

    result = _estimate(
        "set_config", before, preview, [known, unknown],
        [_service("known", {"app": "known"}), _service("unknown", {"app": "unknown"})],
        [before], config_effects=[rule],
    )

    assert not result.degraded_services
    assert result.uncertain_services == (ResourceRef("service", "shop", "unknown"),)


def test_preview_cannot_refer_to_a_different_or_recreated_target():
    before = _deployment("api")
    before["metadata"]["uid"] = "first"
    wrong_name = _object("Scale", "other", spec={"replicas": 0})
    recreated = _object("Scale", "api", spec={"replicas": 0})
    recreated["metadata"]["uid"] = "second"

    for preview in (wrong_name, recreated):
        with pytest.raises(ValueError, match="preview target"):
            _estimate("scale", before, preview, [before], [])
