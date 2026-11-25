"""S2-P4: three-step decision, envelope reason codes, and episode state."""

import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from opsguard.benchmark.restart_lab import MemoryExecutionLab
from opsguard.contracts import RestartAction, ScaleAction, SetSelectorAction
from opsguard.impact import assessment
from opsguard.impact.assessment import EnvelopeDecider, EpisodeState, decide_with_impact
from opsguard.impact.decision import RiskSettings
from opsguard.impact.effects import ImpactEstimate
from opsguard.impact.graph import ResourceRef
from opsguard.intent.inventory import InventorySnapshot
from opsguard.intent.models import (ArgumentBounds, ImpactEnvelope, IntentSpecV2, ReplicaBounds, TargetHint,
                                    sign_intent)
from opsguard.kube.executor import MutationExecutor
from opsguard.kube.gateway import ExecutionGateway
from opsguard.kube.operations import LabBounds

NS = "shop"


def deployment(name, pool, replicas):
    return {"metadata": {"namespace": NS, "name": name, "uid": name + "-uid", "resourceVersion": "1"},
            "spec": {"replicas": replicas, "selector": {"matchLabels": {"app": "checkout", "pool": pool}},
                     "template": {"metadata": {"labels": {"app": "checkout", "pool": pool}},
                                  "spec": {"containers": [{"name": "app", "image": "shop/app:v1"}]}}}}


def service(selector):
    return {"metadata": {"namespace": NS, "name": "checkout", "uid": "checkout-svc", "resourceVersion": "1"},
            "spec": {"selector": dict(selector)}}


def snapshot(deps, svc):
    return InventorySnapshot(namespace=NS, deployments=tuple(deepcopy(deps)), services=(deepcopy(svc),))


def scaled(dep, replicas):
    out = deepcopy(dep)
    out["spec"]["replicas"] = replicas
    return out


def restarted(dep):
    out = deepcopy(dep)
    out["spec"]["template"]["metadata"]["annotations"] = {"kubectl.kubernetes.io/restartedAt": "now"}
    return out


def reselected(svc, selector):
    out = deepcopy(svc)
    out["spec"]["selector"] = dict(selector)
    return out


SCALE = ScaleAction(verb="scale", resource="deployment", namespace=NS, name="checkout-canary", arguments={"replicas": 0})
RESTART = RestartAction(verb="restart", resource="deployment", namespace=NS, name="checkout", arguments={})
SELECT = SetSelectorAction(verb="set_selector", resource="service", namespace=NS, name="checkout",
                           arguments={"selector": {"app": "checkout", "pool": "canary"}})


@pytest.fixture
def lab():
    """checkout (3 replicas) and checkout-canary (2) both behind Service checkout."""
    return {"stable": deployment("checkout", "stable", 3), "canary": deployment("checkout-canary", "canary", 2),
            "svc": service({"app": "checkout"})}


def decider(envelope=ImpactEnvelope()):
    made = EnvelopeDecider(envelope)
    made.episode = EpisodeState("t")
    return made


def step(made, request, current, preview):
    """Decide, and on ALLOW record the write as the gateway would."""
    result = made.decide(request, current, preview)
    if result["status"] == "ALLOW":
        made.episode.record(request, preview)
    return result


def test_scale_restart_selector_sequence_is_blocked_at_the_selector_step(lab):
    made = decider()
    start = snapshot([lab["stable"], lab["canary"]], lab["svc"])
    made.episode.begin(start)
    one = step(made, SCALE, start, scaled(lab["canary"], 0))
    after_one = snapshot([lab["stable"], scaled(lab["canary"], 0)], lab["svc"])
    two = step(made, RESTART, after_one, restarted(lab["stable"]))
    after_two = snapshot([restarted(lab["stable"]), scaled(lab["canary"], 0)], lab["svc"])
    three = step(made, SELECT, after_two, reselected(lab["svc"], SELECT.arguments.selector))
    assert [one["status"], two["status"]] == ["ALLOW", "ALLOW"]
    assert (three["status"], three["reason"]) == ("BLOCK", "ENVELOPE_CUMULATIVE")
    assert three["predicted_services"] == ("checkout",)
    assert made.audit[-1]["step"] == "envelope" and made.audit[-1]["approved_steps"] == 2
    assert len(made.episode.approved) == 2  # The blocked step is never recorded.


@pytest.mark.parametrize("request_,preview", [
    (SCALE, lambda lab: scaled(lab["canary"], 0)),
    (RESTART, lambda lab: restarted(lab["stable"])),
    (SELECT, lambda lab: reselected(lab["svc"], SELECT.arguments.selector)),
])
def test_each_step_alone_is_allowed(lab, request_, preview):
    made = decider()
    start = snapshot([lab["stable"], lab["canary"]], lab["svc"])
    result = made.decide(request_, start, preview(lab))
    assert (result["status"], made.audit[-1]["step"]) == ("ALLOW", "graded")


def test_an_action_that_breaks_the_envelope_alone_gets_its_own_reason(lab):
    made = decider()
    start = snapshot([lab["stable"], lab["canary"]], lab["svc"])
    empty = reselected(lab["svc"], {"app": "nothing"})
    result = made.decide(SELECT, start, empty)
    assert (result["status"], result["reason"]) == ("ESCALATE", "ENVELOPE_DEGRADATION")


def test_services_limit_counts_reachable_services(lab):
    other = deepcopy(lab["svc"])
    other["metadata"].update(name="checkout-internal", uid="internal-svc")
    start = InventorySnapshot(namespace=NS, deployments=(lab["stable"], lab["canary"]),
                              services=(lab["svc"], other))
    made = decider()
    result = made.decide(RESTART, start, restarted(lab["stable"]))
    assert (result["status"], result["reason"]) == ("ESCALATE", "ENVELOPE_SERVICES")
    wide = decider(ImpactEnvelope(max_services_affected=2))
    assert wide.decide(RESTART, start, restarted(lab["stable"]))["status"] == "ALLOW"


def test_cumulative_services_limit(lab):
    """Two actions on different Services fit alone but not together under a limit of 1."""
    second = deployment("search", "search", 1)
    second["spec"]["template"]["metadata"]["labels"] = {"app": "search"}
    search_svc = {"metadata": {"namespace": NS, "name": "search", "uid": "search-svc", "resourceVersion": "1"},
                  "spec": {"selector": {"app": "search"}}}
    start = InventorySnapshot(namespace=NS, deployments=(lab["stable"], second), services=(lab["svc"], search_svc))
    made = decider()
    made.episode.begin(start)
    assert step(made, RESTART, start, restarted(lab["stable"]))["status"] == "ALLOW"
    other = RestartAction(verb="restart", resource="deployment", namespace=NS, name="search", arguments={})
    result = made.decide(other, start, restarted(second))
    assert (result["status"], result["reason"]) == ("BLOCK", "ENVELOPE_CUMULATIVE")


def test_rollout_uncertainty_and_missing_objects_are_unknown(lab):
    start = snapshot([lab["stable"], lab["canary"]], lab["svc"])
    recreate = restarted(lab["stable"])
    recreate["spec"]["strategy"] = {"type": "RollingUpdate", "rollingUpdate": {"maxUnavailable": 0, "maxSurge": 0}}
    assert decider().decide(RESTART, start, recreate)["reason"] == "IMPACT_UNKNOWN"
    ghost = RestartAction(verb="restart", resource="deployment", namespace=NS, name="ghost", arguments={})
    assert decider().decide(ghost, start, restarted(lab["stable"]))["reason"] == "IMPACT_UNKNOWN"


def test_irreversible_and_deleting_verbs_hit_their_envelope_codes(lab, monkeypatch):
    start = snapshot([lab["stable"], lab["canary"]], lab["svc"])
    monkeypatch.setattr(assessment, "IRREVERSIBLE_VERBS", frozenset({"restart"}))
    assert decider().decide(RESTART, start, restarted(lab["stable"]))["reason"] == "ENVELOPE_IRREVERSIBLE"
    assert decider(ImpactEnvelope(require_reversible=False)).decide(
        RESTART, start, restarted(lab["stable"]))["reason"] != "ENVELOPE_IRREVERSIBLE"
    monkeypatch.setattr(assessment, "IRREVERSIBLE_VERBS", frozenset())
    monkeypatch.setattr(assessment, "DELETING_VERBS", frozenset({"restart"}))
    assert decider().decide(RESTART, start, restarted(lab["stable"]))["reason"] == "ENVELOPE_DELETION"


def test_decider_refuses_to_run_without_an_episode(lab):
    with pytest.raises(ValueError):
        EnvelopeDecider(ImpactEnvelope()).decide(SCALE, snapshot([lab["canary"]], lab["svc"]), lab["canary"])


# --- v1.0 equivalence of the single-action decision -----------------------------------------------

def _legacy(*, policy_outcome, provenance_risk, irreversibility, impact=None, settings=RiskSettings(),
            escalate_on_degradation=True):
    """decide_with_impact exactly as it was at v1.0-reference plus the S1-P4 rule."""
    from opsguard.impact.decision import GuardDecision, decide_risk
    degraded = impact.degraded_services if impact is not None else ()
    if policy_outcome != "PASS":
        return decide_risk(policy_outcome=policy_outcome, provenance_risk=provenance_risk, blast_radius=0,
                           irreversibility=irreversibility, predicted_affected_services=degraded, settings=settings)
    if impact is None:
        return GuardDecision("ESCALATE", "impact_unavailable", None, ())
    at_risk = set(degraded).union(impact.uncertain_services)
    blast = len(at_risk) / impact.service_count if impact.service_count else 0
    decision = decide_risk(policy_outcome=policy_outcome, provenance_risk=provenance_risk, blast_radius=blast,
                           irreversibility=irreversibility, predicted_affected_services=degraded, settings=settings)
    if decision.decision == "ALLOW" and degraded and escalate_on_degradation:
        return replace(decision, decision="ESCALATE", reason_code="impact_degradation")
    if decision.decision == "ALLOW" and impact.uncertain_services:
        return replace(decision, decision="ESCALATE", reason_code="impact_uncertain")
    return decision


def _service(name):
    return ResourceRef("service", NS, name)


@pytest.mark.parametrize("policy", ["PASS", "BLOCK", "AMBIGUOUS"])
@pytest.mark.parametrize("degraded,uncertain,count", [
    ((), (), 2), (("a",), (), 2), ((), ("a",), 2), (("a",), ("b",), 2), (("a", "b"), (), 2), ((), ("a", "b"), 4),
    ((), (), 0), (("a",), (), 1),
])
@pytest.mark.parametrize("provenance,irreversibility", [(0, 0), (1, 0), (1, 1), (0.5, 0.5)])
@pytest.mark.parametrize("escalate", [True, False])
@pytest.mark.parametrize("with_impact", [True, False])
def test_split_decision_matches_v1_exactly(policy, degraded, uncertain, count, provenance, irreversibility,
                                           escalate, with_impact):
    impact = ImpactEstimate(tuple(map(_service, degraded + uncertain)), tuple(map(_service, degraded)),
                            tuple(map(_service, uncertain)), count) if with_impact else None
    kwargs = dict(policy_outcome=policy, provenance_risk=provenance, irreversibility=irreversibility,
                  impact=impact, escalate_on_degradation=escalate)
    assert decide_with_impact(**kwargs) == _legacy(**kwargs)


# --- gateway hook --------------------------------------------------------------------------------

KEY = b"episode-test-key-0123456789abcdefghij"
NOW = datetime(2026, 11, 25, 10, 0, tzinfo=timezone.utc)


def test_gateway_attaches_the_episode_and_records_applied_writes():
    lab = MemoryExecutionLab({"checkout-api": {"service": "checkout", "replicas": 2}})
    spec = sign_intent(IntentSpecV2(id="intent-0001", task_id="checkout-task", issued_at=NOW,
                                    expires_at=NOW + timedelta(minutes=15), namespace=lab.namespace,
                                    target=TargetHint(kind_hint="Service", name="checkout"), verbs=("scale",),
                                    bounds=ArgumentBounds(replicas=ReplicaBounds(min=1, max=4))), KEY)
    made = EnvelopeDecider(spec.envelope)
    bounds = LabBounds(lab.namespace, scales={(lab.namespace, "checkout-api"): (0, 4)})
    gateway = ExecutionGateway(lab, MutationExecutor(lab, bounds), intent=spec, intent_key=KEY,
                               impact_decider=made, clock=lambda: NOW + timedelta(minutes=1))
    assert made.episode is gateway.episode

    def scale(n):
        request = ScaleAction(verb="scale", resource="deployment", namespace=lab.namespace, name="checkout-api",
                              arguments={"replicas": n})
        return asyncio.run(gateway.propose(request, task_id="checkout-task", timeout_seconds=2))

    assert scale(3).status == "ALLOW"
    assert [c["verb"] for c in gateway.episode.approved] == ["scale"]
    assert gateway.episode.approved[0]["reachable"]  # The decider's hint reached the record.
    assert made.audit[-1]["step"] == "graded"
