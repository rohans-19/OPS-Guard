import pytest

from opsguard.impact.graph import ResourceRef, affected_services, build_dependency_graph


def test_shared_config_reaches_both_services_in_stable_order():
    config = ResourceRef("configmap", "shop", "flags")
    checkout = ResourceRef("workload", "shop", "checkout-api")
    payments = ResourceRef("workload", "shop", "payments-api")
    checkout_service = ResourceRef("service", "shop", "checkout")
    payments_service = ResourceRef("service", "shop", "payments")
    other_checkout = ResourceRef("service", "other", "checkout")
    graph = build_dependency_graph(
        [config, checkout, payments, checkout_service, payments_service, other_checkout],
        [
            (config, payments),
            (payments, payments_service),
            (config, checkout),
            (checkout, checkout_service),
        ],
    )

    assert affected_services(graph, config) == (checkout_service, payments_service)
    assert affected_services(graph, checkout) == (checkout_service,)


def test_direct_service_change_reaches_consumers_even_in_a_cycle():
    upstream = ResourceRef("service", "shop", "payments")
    downstream = ResourceRef("service", "shop", "checkout")
    graph = build_dependency_graph(
        [upstream, downstream],
        [(upstream, downstream), (downstream, upstream)],
    )

    assert affected_services(graph, upstream) == (downstream, upstream)


def test_known_isolated_resource_has_no_affected_services():
    config = ResourceRef("configmap", "shop", "flags")
    graph = build_dependency_graph([config], [])

    assert affected_services(graph, config) == ()


def test_unknown_changed_resource_is_an_error():
    graph = build_dependency_graph([], [])

    with pytest.raises(ValueError, match="absent from the dependency graph"):
        affected_services(graph, ResourceRef("configmap", "shop", "missing"))


def test_dependency_edges_cannot_implicitly_add_unknown_resources():
    config = ResourceRef("configmap", "shop", "flags")
    workload = ResourceRef("workload", "shop", "checkout-api")

    with pytest.raises(ValueError, match="Unknown resource in dependency edge"):
        build_dependency_graph([config], [(config, workload)])
