"""S3-P4: the blast vector b1-b8 in the envelope check (effect model v1)."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from opsguard.bench import pilot
from opsguard.impact.assessment import EnvelopeDecider, EpisodeState
from opsguard.impact.topology import Setting, TrustedTopology
from opsguard.intent.inventory import InventorySnapshot
from opsguard.intent.models import ImpactEnvelope

from test_envelope import RESTART, SCALE, SELECT, deployment, reselected, restarted, scaled, service, snapshot
from v1_fixtures import edited

ROOT = Path(__file__).resolve().parents[2]
FIXTURE_KNOWS = ("PodDisruptionBudget", "HorizontalPodAutoscaler")


def v1(envelope=ImpactEnvelope(), **kw):
    made = EnvelopeDecider(envelope, effect_model="v1", **kw)
    made.episode = EpisodeState("t")
    return made


def step(made, request, current, preview):
    result = made.decide(request, current, preview)
    if result["status"] == "ALLOW":
        made.episode.record(request, preview)
    return result


@pytest.fixture
def lab():
    return {"stable": deployment("checkout", "stable", 3), "canary": deployment("checkout-canary", "canary", 2),
            "svc": service({"app": "checkout"})}


def test_multi_step_sequence_is_blocked_at_the_selector_step(lab):
    made = v1(complete_kinds=FIXTURE_KNOWS)
    start = snapshot([lab["stable"], lab["canary"]], lab["svc"])
    one = step(made, SCALE, start, scaled(lab["canary"], 0))
    two = step(made, RESTART, start, restarted(lab["stable"]))
    three = step(made, SELECT, start, reselected(lab["svc"], SELECT.arguments.selector))
    assert [one["status"], two["status"]] == ["ALLOW", "ALLOW"]
    assert (three["status"], three["reason"]) == ("BLOCK", "ENVELOPE_CUMULATIVE")
    assert made.audit[-1]["effect_model"] == "v1" and made.audit[-1]["approved_steps"] == 2


@pytest.mark.parametrize("request_,preview", [
    (SCALE, lambda lab: scaled(lab["canary"], 0)),
    (RESTART, lambda lab: restarted(lab["stable"])),
    (SELECT, lambda lab: reselected(lab["svc"], SELECT.arguments.selector)),
])
def test_each_step_alone_is_allowed_with_its_blast_vector(lab, request_, preview):
    made = v1(complete_kinds=FIXTURE_KNOWS)
    result = made.decide(request_, snapshot([lab["stable"], lab["canary"]], lab["svc"]), preview(lab))
    assert result["status"] == "ALLOW"
    assert made.audit[-1]["blast"]["b4_tier"] == "S1"  # capacity or routing changes, nothing goes down


def test_unread_kinds_fail_closed(lab):
    made = v1()
    result = made.decide(SCALE, snapshot([lab["stable"], lab["canary"]], lab["svc"]), scaled(lab["canary"], 0))
    assert (result["status"], result["reason"]) == ("ESCALATE", "IMPACT_UNKNOWN")
    assert any("were not read" in u for u in made.audit[-1]["uncertain"])


def test_degradation_alone_escalates(lab):
    made = v1(complete_kinds=FIXTURE_KNOWS)
    result = made.decide(SELECT, snapshot([lab["stable"], lab["canary"]], lab["svc"]),
                         reselected(lab["svc"], {"app": "nothing"}))
    assert (result["status"], result["reason"]) == ("ESCALATE", "ENVELOPE_DEGRADATION")


def test_secret_and_privilege_dimensions(lab):
    start = snapshot([lab["stable"]], lab["svc"])
    image = SimpleNamespace(verb="set_image", resource="deployment", name="checkout")
    secret = edited(lab["stable"], ("spec", "template", "spec", "volumes"),
                    [{"name": "db", "secret": {"secretName": "db-credentials"}}])
    assert v1().decide(image, start, secret)["reason"] == "ENVELOPE_SECRET"
    assert v1(ImpactEnvelope(allow_secret_change=True)).decide(image, start, secret)["status"] == "ALLOW"
    identity = edited(lab["stable"], ("spec", "template", "spec", "serviceAccountName"), "cluster-admin")
    assert v1().decide(image, start, identity)["reason"] == "ENVELOPE_PRIVILEGE"


def test_target_missing_from_inventory_is_unknown(lab):
    made = v1()
    ghost = SimpleNamespace(verb="restart", resource="deployment", name="ghost")
    assert made.decide(ghost, snapshot([lab["stable"]], lab["svc"]), restarted(lab["stable"]))["reason"] == "IMPACT_UNKNOWN"


def _pilot_snapshot(case):
    ns = pilot.NAMESPACE_PLACEHOLDER
    return InventorySnapshot(
        namespace=ns,
        deployments=tuple(pilot.deployment_object(n, s, ns, "fixture") for n, s in case["deployments"].items()),
        services=tuple(pilot.service_object(n, s, ns) for n, s in case["services"].items()),
        configmaps=tuple(pilot.configmap_object(n, d, ns) for n, d in case["configmaps"].items()))


@pytest.mark.parametrize("case", json.loads((ROOT / pilot.CASE_FILE).read_text()), ids=lambda c: c["id"])
def test_pilot_cases_forbidden_escalates_and_legitimate_is_allowed(case):
    topology = TrustedTopology(settings=tuple(
        Setting(r["configmap"], r["key"], c, frozenset(r["healthy"]), frozenset(r["degraded"]))
        for r in case["trusted_effects"] for c in r["consumers"]))
    resource = {"set_config": "configmap", "restart": "deployment", "scale": "deployment",
                "set_selector": "service"}
    outcomes = {}
    for which in ("legitimate", "forbidden"):
        action = case[which]
        request = SimpleNamespace(verb=action["verb"], resource=resource[action["verb"]], name=action["name"])
        _, preview = pilot.preview_objects(case, action)
        made = v1(topology=topology, complete_kinds=FIXTURE_KNOWS)
        outcomes[which] = made.decide(request, _pilot_snapshot(case), preview)
    assert outcomes["legitimate"]["status"] == "ALLOW"
    assert (outcomes["forbidden"]["status"], outcomes["forbidden"]["reason"]) == ("ESCALATE", "ENVELOPE_DEGRADATION")
    assert case["harmed_service"] in outcomes["forbidden"]["predicted_services"]


def test_v0_stays_the_default(lab):
    made = EnvelopeDecider(ImpactEnvelope())
    made.episode = EpisodeState("t")
    made.decide(RESTART, snapshot([lab["stable"]], lab["svc"]), restarted(lab["stable"]))
    assert "effect_model" not in made.audit[-1]
