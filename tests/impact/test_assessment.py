from opsguard.impact.assessment import decide_with_impact
from opsguard.impact.effects import ImpactEstimate
from opsguard.impact.graph import ResourceRef

CHECKOUT = ResourceRef("service", "shop", "checkout")


def _impact(degraded=(), uncertain=(), services=4):
    return ImpactEstimate(reachable_services=(CHECKOUT,), degraded_services=tuple(degraded),
                          uncertain_services=tuple(uncertain), service_count=services)


def test_predicted_outage_is_never_auto_allowed():
    decision = decide_with_impact(policy_outcome="PASS", provenance_risk=0, irreversibility=0,
                                  impact=_impact(degraded=[CHECKOUT]))
    # One degraded Service out of four scores 0.125, far below the escalation threshold.
    assert decision.risk_score < 0.375
    assert (decision.decision, decision.reason_code) == ("ESCALATE", "impact_degradation")
    assert decision.predicted_affected_services == (CHECKOUT,)


def test_reference_behaviour_can_be_requested_for_comparison_runs():
    decision = decide_with_impact(policy_outcome="PASS", provenance_risk=0, irreversibility=0,
                                  impact=_impact(degraded=[CHECKOUT]), escalate_on_degradation=False)
    assert decision.decision == "ALLOW"


def test_no_predicted_effect_still_allows():
    decision = decide_with_impact(policy_outcome="PASS", provenance_risk=0, irreversibility=0, impact=_impact())
    assert decision.decision == "ALLOW"


def test_policy_block_still_wins_over_impact():
    decision = decide_with_impact(policy_outcome="BLOCK", provenance_risk=0, irreversibility=0,
                                  impact=_impact(degraded=[CHECKOUT]))
    assert decision.decision == "BLOCK"
