import json

import pytest

from opsguard.impact.decision import GuardDecision, RiskSettings, decide_risk
from opsguard.impact.graph import ResourceRef


@pytest.mark.parametrize(
    ("blast_radius", "irreversibility", "expected"),
    [
        (0.25, 0, GuardDecision("ALLOW", "risk_low", 0.125, ())),
        (0.75, 0, GuardDecision("ESCALATE", "risk_elevated", 0.375, ())),
        (1, 1, GuardDecision("BLOCK", "risk_high", 0.75, ())),
    ],
)
def test_risk_thresholds(blast_radius, irreversibility, expected):
    assert decide_risk(
        policy_outcome="PASS",
        provenance_risk=0,
        blast_radius=blast_radius,
        irreversibility=irreversibility,
    ) == expected


@pytest.mark.parametrize(
    ("policy_outcome", "expected"),
    [
        ("BLOCK", GuardDecision("BLOCK", "policy_violation", None, ())),
        ("AMBIGUOUS", GuardDecision("ESCALATE", "ambiguous_target", None, ())),
    ],
)
def test_policy_outcome_takes_precedence_over_low_risk(policy_outcome, expected):
    assert decide_risk(
        policy_outcome=policy_outcome,
        provenance_risk=0,
        blast_radius=0,
        irreversibility=0,
    ) == expected


def test_audit_record_contains_stable_service_order_and_score():
    checkout = ResourceRef("service", "shop", "checkout")
    storefront = ResourceRef("service", "shop", "storefront")
    result = decide_risk(
        policy_outcome="PASS",
        provenance_risk=0.5,
        blast_radius=0.5,
        irreversibility=0.5,
        predicted_affected_services=[storefront, checkout, checkout],
    )

    line = result.to_json_line()
    assert line.endswith("\n")
    assert json.loads(line) == {
        "decision": "ESCALATE",
        "reason_code": "risk_elevated",
        "risk_score": 0.5,
        "predicted_affected_services": [
            {"kind": "service", "namespace": "shop", "name": "checkout"},
            {"kind": "service", "namespace": "shop", "name": "storefront"},
        ],
    }


def test_invalid_risk_input_fails_explicitly():
    with pytest.raises(ValueError, match="blast_radius"):
        decide_risk(policy_outcome="PASS", provenance_risk=0, blast_radius=float("nan"), irreversibility=0)


def test_thresholds_can_be_calibrated_but_must_be_ordered():
    with pytest.raises(ValueError, match="thresholds"):
        RiskSettings(escalate_at=0.8, block_at=0.7)

    decision = decide_risk(
        policy_outcome="PASS",
        provenance_risk=0,
        blast_radius=0.5,
        irreversibility=0,
        settings=RiskSettings(escalate_at=0.2, block_at=0.6),
    )
    assert decision.decision == "ESCALATE"


@pytest.mark.parametrize(
    ("escalate_at", "block_at", "expected"),
    [(0.07, 0.5, "ESCALATE"), (0.04, 0.07, "BLOCK")],
)
def test_decimal_threshold_does_not_round_down_to_lower_risk(escalate_at, block_at, expected):
    settings = RiskSettings(
        provenance_weight=0.1,
        blast_radius_weight=0.4,
        irreversibility_weight=0.5,
        escalate_at=escalate_at,
        block_at=block_at,
    )
    result = decide_risk(
        policy_outcome="PASS",
        provenance_risk=0.7,
        blast_radius=0,
        irreversibility=0,
        settings=settings,
    )
    assert result.decision == expected
