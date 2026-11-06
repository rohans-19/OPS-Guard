"""Deterministic decisions for proposals reviewed by the task policy."""

import json
from dataclasses import asdict, dataclass
from math import fsum, isclose
from typing import Iterable, Literal, Optional

from opsguard.impact.graph import ResourceRef


Decision = Literal["ALLOW", "BLOCK", "ESCALATE"]
PolicyOutcome = Literal["PASS", "BLOCK", "AMBIGUOUS"]


def _validate_fraction(name: str, value: float) -> None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
        raise ValueError(f"{name} must be a number between 0 and 1")


def _meets_threshold(score: float, threshold: float) -> bool:
    # Treat floating-point rounding at the boundary conservatively.
    return score >= threshold or isclose(score, threshold, rel_tol=1e-15)


@dataclass(frozen=True)
class RiskSettings:
    """Phase 1 defaults for testing, to be calibrated in the pilot."""

    provenance_weight: float = 0.25
    blast_radius_weight: float = 0.5
    irreversibility_weight: float = 0.25
    escalate_at: float = 0.375
    block_at: float = 0.75

    def __post_init__(self) -> None:
        for name, value in vars(self).items():
            _validate_fraction(name, value)
        weights = (self.provenance_weight, self.blast_radius_weight, self.irreversibility_weight)
        if not isclose(fsum(weights), 1, rel_tol=0, abs_tol=1e-9):
            raise ValueError("risk weights must sum to 1")
        if not 0 < self.escalate_at < self.block_at:
            raise ValueError("thresholds must satisfy 0 < escalate_at < block_at <= 1")


@dataclass(frozen=True)
class GuardDecision:
    decision: Decision
    reason_code: str
    risk_score: Optional[float]
    predicted_affected_services: tuple[ResourceRef, ...]

    def to_json_line(self) -> str:
        return json.dumps(asdict(self), sort_keys=True) + "\n"


def decide_risk(
    *,
    policy_outcome: PolicyOutcome,
    provenance_risk: float,
    blast_radius: float,
    irreversibility: float,
    predicted_affected_services: Iterable[ResourceRef] = (),
    settings: RiskSettings = RiskSettings(),
) -> GuardDecision:
    """Apply policy precedence, then score the supplied impact and risk factors.

    The caller supplies a normalized blast radius from its effect estimate.
    Graph reachability alone does not establish that a Service will degrade.
    """
    services = set(predicted_affected_services)
    if any(ref.kind != "service" for ref in services):
        raise ValueError("predicted affected resources must be Services")
    affected = tuple(sorted(services, key=lambda ref: (ref.namespace, ref.name)))

    if policy_outcome == "BLOCK":
        return GuardDecision("BLOCK", "policy_violation", None, affected)
    if policy_outcome == "AMBIGUOUS":
        return GuardDecision("ESCALATE", "ambiguous_target", None, affected)
    if policy_outcome != "PASS":
        raise ValueError(f"unsupported policy outcome: {policy_outcome!r}")

    _validate_fraction("provenance_risk", provenance_risk)
    _validate_fraction("blast_radius", blast_radius)
    _validate_fraction("irreversibility", irreversibility)

    score = fsum((
        settings.provenance_weight * provenance_risk,
        settings.blast_radius_weight * blast_radius,
        settings.irreversibility_weight * irreversibility,
    ))
    if _meets_threshold(score, settings.block_at):
        return GuardDecision("BLOCK", "risk_high", score, affected)
    if _meets_threshold(score, settings.escalate_at):
        return GuardDecision("ESCALATE", "risk_elevated", score, affected)
    return GuardDecision("ALLOW", "risk_low", score, affected)
