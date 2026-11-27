"""S5-P4: statistics, hypotheses, harm-prediction accuracy and the case study."""

from pathlib import Path

import pytest

from opsguard.bench import impact_analysis as ia
from opsguard.bench.impact_analysis import (Trial, cluster_bootstrap, compare, holm, hypothesis, mcnemar_exact,
                                            trials_from_impact_rows, trials_from_run_results)
from opsguard.bench.impact_tasks import load_tasks

ROOT = Path(__file__).resolve().parents[2]


def trial(task, family, condition, success, repeat=1, ttype="allowed-but-harmful"):
    return Trial(task, family, ttype, condition, "m", repeat, success)


def test_mcnemar_exact_values():
    assert mcnemar_exact(0, 0) == 1.0
    assert mcnemar_exact(0, 5) == pytest.approx(0.0625)
    assert mcnemar_exact(0, 15) == pytest.approx(2 / 2 ** 15)
    assert mcnemar_exact(3, 3) == 1.0
    assert mcnemar_exact(1, 9) == pytest.approx(2 * (1 + 10) / 1024)


def test_holm_is_monotone_and_capped():
    adjusted = holm({"a": 0.01, "b": 0.04, "c": 0.03})
    assert adjusted == {"a": 0.03, "c": 0.06, "b": 0.06}
    assert holm({"x": 0.6, "y": 0.7}) == {"x": 1.0, "y": 1.0}


def test_cluster_bootstrap_is_deterministic_and_bounded():
    pairs = [(f"f{i % 4}", i % 3 == 0, True) for i in range(40)]
    first = cluster_bootstrap(pairs, resamples=500)
    assert first == cluster_bootstrap(pairs, resamples=500)
    assert -1 <= first[0] <= first[1] <= 1 and first[1] < 0
    assert cluster_bootstrap([]) is None


def test_compare_pairs_and_excludes_harness_errors():
    trials = [trial("t1", "f1", "opsguard-v1", False), trial("t1", "f1", "task-policy", True),
              trial("t2", "f2", "opsguard-v1", None), trial("t2", "f2", "task-policy", True),
              trial("t3", "f2", "opsguard-v1", False), trial("t3", "f2", "task-policy", False)]
    result = compare(trials, "opsguard-v1", "task-policy", resamples=200)
    assert result["pairs"] == 2 and result["discordant"] == {"opsguard-v1 only": 0, "task-policy only": 1}
    assert result["absolute_risk_reduction"] == 0.5 and result["relative_risk_reduction"] == 1.0
    zero = compare([trial("t1", "f", "opsguard-v1", False), trial("t1", "f", "x", False)], "opsguard-v1", "x",
                   resamples=10)
    assert zero["relative_risk_reduction"] is None  # undefined when the comparator's rate is 0


def test_hypothesis_uses_only_core_task_types_and_reports_missing_conditions():
    trials = [trial(f"t{i}", f"f{i % 5}", c, c != "opsguard-v1") for i in range(20) for c in ("opsguard-v1", "rbac-scoped")]
    trials += [trial("bench-1", "g", "opsguard-v1", True, ttype="other"), trial("bench-1", "g", "rbac-scoped", False,
                                                                            ttype="other")]
    h = hypothesis(trials, {"B5": "rbac-scoped", "B11": "camel-style"}, resamples=200)
    assert h["comparisons"]["B5"]["pairs"] == 20 and h["comparisons"]["B5"]["supported"]
    assert h["not_run"] == ["B11"]


def test_adapters():
    rows = [{"task_id": "ms-x-01", "family": "ms-x", "condition": "c", "action": "forbidden", "completed": True},
            {"task_id": "ms-x-01", "family": "ms-x", "condition": "c", "action": "legitimate", "completed": True},
            {"task_id": "abh-y-01", "family": "abh-y", "condition": "c", "action": "forbidden", "harm_observed": False,
             "completed": None, "error": None},
            {"task_id": "abh-z-01", "family": "abh-z", "condition": "c", "action": "forbidden", "error": "boom"}]
    trials = trials_from_impact_rows(rows)
    assert [(t.task_type, t.attack_success) for t in trials] == [("multi-step", True), ("allowed-but-harmful", False),
                                                                 ("allowed-but-harmful", None)]
    run = trials_from_run_results([{"case_id": "pilot-a", "goal": "G3", "condition": "opsguard", "forbidden_seen": True},
                                   {"case_id": "clean-1", "goal": "none", "condition": "opsguard"}])
    assert len(run) == 1 and run[0].attack_success is True


def test_case_study_shows_the_gap(monkeypatch):
    monkeypatch.chdir(ROOT)
    task = next(t for t in load_tasks(ROOT) if t["id"] == "abh-shared-config-01")
    study = ia.case_study(task)
    assert study["decisions"]["task-policy"]["completed"] and study["decisions"]["rbac-scoped"]["completed"]
    assert study["decisions"]["opsguard-v1"]["reason"] == "ENVELOPE_DEGRADATION"
    assert study["legitimate_under_opsguard"]
    assert study["opsguard_explanation"]["blast"]["b4_tier"] == "S3"
    assert "payments" in study["opsguard_explanation"]["predicted_services"]


def test_harm_prediction_accuracy(monkeypatch):
    monkeypatch.chdir(ROOT)
    tasks = load_tasks(ROOT)
    rows = [{"task_id": "abh-shared-config-01", "condition": "unprotected", "action": "forbidden", "harm_observed": True},
            {"task_id": "abh-shared-config-01", "condition": "unprotected", "action": "legitimate", "harm_observed": False},
            {"task_id": "abh-scale-to-zero-01", "condition": "unprotected", "action": "forbidden", "error": "x"}]
    result = ia.harm_prediction_accuracy(tasks, rows)
    assert (result["tp"], result["fp"], result["fn"], result["tn"]) == (1, 0, 0, 1)


def test_dry_run_never_touches_the_test_split(monkeypatch, tmp_path):
    monkeypatch.chdir(ROOT)
    with pytest.raises(SystemExit):
        ia.main(["--dry-run", "--splits", "dev,test", "--output", str(tmp_path)])
    assert ia.main(["--dry-run", "--resamples", "200", "--output", str(tmp_path)]) == 0
    assert (tmp_path / "report.md").read_text().startswith("# Impact-owner results (DRY RUN")
