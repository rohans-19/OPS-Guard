"""Use a mutation's predicted effects in the task-scoped guard decision."""

from dataclasses import replace
from typing import Optional

from opsguard.impact.decision import GuardDecision, PolicyOutcome, RiskSettings, decide_risk
from opsguard.impact.effects import ImpactEstimate


def decide_with_impact(
    *,
    policy_outcome: PolicyOutcome,
    provenance_risk: float,
    irreversibility: float,
    impact: Optional[ImpactEstimate] = None,
    settings: RiskSettings = RiskSettings(),
    escalate_on_degradation: bool = True,
) -> GuardDecision:
    """Score predicted degradation and withhold writes when the effect is uncertain.

    A predicted Service outage is never allowed automatically, whatever the
    weighted score: an authorized action that takes a Service down still needs
    a person to approve it. ``escalate_on_degradation=False`` reproduces the
    v1.0-reference behaviour, where only the weighted score and uncertainty
    could withhold a write, for comparison runs.
    """
    degraded = impact.degraded_services if impact is not None else ()
    if policy_outcome != "PASS":
        return decide_risk(
            policy_outcome=policy_outcome,
            provenance_risk=provenance_risk,
            blast_radius=0,
            irreversibility=irreversibility,
            predicted_affected_services=degraded,
            settings=settings,
        )
    if impact is None:
        return GuardDecision("ESCALATE", "impact_unavailable", None, ())

    at_risk = set(degraded).union(impact.uncertain_services)
    blast_radius = len(at_risk) / impact.service_count if impact.service_count else 0
    decision = decide_risk(
        policy_outcome=policy_outcome,
        provenance_risk=provenance_risk,
        blast_radius=blast_radius,
        irreversibility=irreversibility,
        predicted_affected_services=degraded,
        settings=settings,
    )
    if decision.decision == "ALLOW" and degraded and escalate_on_degradation:
        return replace(decision, decision="ESCALATE", reason_code="impact_degradation")
    if decision.decision == "ALLOW" and impact.uncertain_services:
        return replace(decision, decision="ESCALATE", reason_code="impact_uncertain")
    return decision
