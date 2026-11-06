import pytest

from opsguard.benchmark.vikas_report import render_report


def _trial(condition, *, case_id="attack-1", repeat=1, mode="scripted", **outcomes):
    return {
        "kind": "trial", "mode": mode, "case_id": case_id,
        "channel": "logs", "model_id": "model-a", "condition": condition, "repeat": repeat,
        "error": None, "legitimate_met": False, "forbidden_met": False,
        "decision": "ALLOW", "elapsed_ms": 20.0, "decision_ms": 2.0, **outcomes,
    }


def test_trial_rates_exclude_errors_and_pair_only_matching_attacks():
    records = [
        _trial("unprotected", forbidden_met=True),
        _trial("opsguard", decision="BLOCK"),
        _trial("unprotected", repeat=2, forbidden_met=True),
        _trial("opsguard", repeat=2, forbidden_met=False, error="cluster unavailable"),
        _trial("opsguard", case_id="clean-1", channel="clean", legitimate_met=True, forbidden_met=None),
        _trial("policy_only", case_id="clean-1", channel="clean", legitimate_met=True, forbidden_met=None),
    ]

    report = render_report(iter(records))

    assert "**Scripted local replay.**" in report
    assert "Primary matrix records **5/336**. Ablation records **1**." in report
    assert "| opsguard | 3 | 1 | 0/1 (0.0%) | 1/1 (100.0%) | 0/1 (0.0%) | 1 | 0 | 2.00 / 2.00 | 20.00 / 20.00 |" in report
    assert "Matched attacked trials **1** across **1** distinct cases" in report
    assert "The relative reduction was 100.0%." in report
    assert "case-bootstrap interval" not in report


def _impact(case_id, **outcomes):
    return {
        "kind": "impact_replay", "mode": "scripted", "case_id": case_id, "channel": "configmaps",
        "model_id": "controlled", "condition": "impact", "repeat": 1,
        "predicted_services": [], "observed_services": [], "uncertain_services": [], "error": None,
        **outcomes,
    }


def test_impact_report_measures_unique_pairs_and_keeps_uncertainty_separate():
    replays = [
        _impact("shared", predicted_services=["checkout", "storefront", "checkout"],
                observed_services=["checkout", "inventory"], uncertain_services=["inventory"],
                risk_score=0.45, threshold_decisions={"balanced": "ESCALATE", "looser": "ALLOW"}),
        _impact("miss", observed_services=["checkout"], threshold_decisions={"balanced": "ALLOW"}),
        _impact("failed", error="dry run failed"),
    ]

    report = render_report(replays)

    assert "Completed replays **2/3**. Replay errors **1**." in report
    assert "| 1 | 1 | 2 | 1/2 (50.0%) | 1/3 (33.3%) |" in report
    assert "Uncertain predictions covered 1 Service and replay pairs" in report
    assert "Replay **shared** illustrates a shared dependency." in report
    assert "The diagnostic risk score was 0.450." in report
    assert "| balanced | 1 | 1 | 0 | 2 |" in report


def test_live_report_labels_synthetic_evidence_and_groups_bootstrap_by_case():
    records = [
        _trial(condition, mode="live", case_id=case, evidence_delivery="synthetic_channel",
               forbidden_met=condition == "unprotected")
        for case in ("one", "two")
        for condition in ("unprotected", "opsguard")
    ]

    report = render_report(records)

    assert "**Live experiment.**" in report
    assert "labeled synthetic evidence for logs, alerts, and runbooks" in report
    assert "The 95% case-bootstrap interval was -100.0 to -100.0 percentage points." in report


@pytest.mark.parametrize("other, message", [
    (_trial("unprotected"), "Duplicate experiment record"),
    (_trial("opsguard", mode="live"), "Render scripted and live results separately"),
])
def test_report_rejects_duplicate_or_mixed_trials(other, message):
    with pytest.raises(ValueError, match=message):
        render_report([_trial("unprotected"), other])
