import json
from pathlib import Path

import pytest

pytest.importorskip("numpy")

from opsguard.bench import run as run_cli
from opsguard.bench.aggregate import (CaseInfo, analyse, breakdown, cluster_interval, hypotheses, mcnemar_exact, merge,
                                      normalize, paired_comparison, summarize_group, team_case_index, valid)

ROOT = Path(__file__).resolve().parents[2]
CONDITIONS = ("unprotected", "opsguard")
# Two tasks. Each has one clean case and two attacked cases.
INDEX = {f"{task}-{kind}": CaseInfo("selector", f"{task}-{kind}", f"selector:{task}", channel)
         for task in ("t1", "t2") for kind, channel in (("clean", None), ("a", "logs"), ("b", "alerts"))}


def row(case, condition, repeat=1, *, legit=True, forbidden=None, error=None, model="m1", **extra):
    attacked = INDEX[case].channel is not None
    return {"case_id": case, "model_id": model, "condition": condition, "repeat": repeat, "backend": "memory",
            "provider": "scripted", "legitimate_met": None if error else legit,
            "forbidden_seen": None if error or not attacked else bool(forbidden), "error": error,
            "task_ms": 10.0, "run_id": f"{case}-{model}-{condition}-{repeat}-{extra.get('attempt', 1)}", **extra}


def full_matrix(*, forbidden_baseline=True, repeats=2):
    return [row(case, condition, repeat, forbidden=forbidden_baseline and condition == "unprotected")
            for case in INDEX for condition in CONDITIONS for repeat in range(1, repeats + 1)]


def merged(rows, **kwargs):
    options = {"models": ["m1"], "conditions": CONDITIONS, "repeats": 2, **kwargs}
    return merge([(item, f"file:{number}") for number, item in enumerate(rows, 1)], INDEX, **options)


def test_complete_matrix_has_full_coverage_and_no_problems():
    result = merged(full_matrix())
    assert result.expected == len(result.rows) == 24 and result.complete
    assert not (result.missing or result.duplicates or result.rejected or result.superseded)


def test_missing_duplicate_unknown_and_out_of_matrix_rows_are_reported():
    rows = full_matrix()
    dropped = rows.pop(0)
    rows.append(dict(rows[5]))                                             # same run ID again
    rows.append({**rows[6], "run_id": "another-id"})                       # second result for a completed cell
    rows.append(row("t1-a", "hardened"))                                   # condition outside the expected matrix
    rows.append(row("t1-a", "opsguard", 3))                                # repeat outside the expected matrix
    rows.append(row("t1-a", "opsguard", model="unlisted-model"))           # model outside the expected matrix
    rows.append({**row("t1-a", "opsguard"), "case_id": "no-such-case"})
    result = merged(rows)
    assert result.missing == [(dropped["case_id"], "m1", dropped["condition"], dropped["repeat"])]
    assert sorted(item["reason"] for item in result.duplicates) == [
        "run ID appears twice", "second result for a cell that was not a failed attempt"]
    assert sorted(item["reason"] for item in result.rejected) == [
        "model, condition or repeat is outside the expected matrix"] * 3 + ["unknown case ID"]
    assert len(result.rows) == 23 and not result.complete
    assert analyse(result, conditions=CONDITIONS)["coverage"]["missing_trials"] == 1


def test_retry_supersedes_a_failed_attempt_and_keeps_it_on_record():
    rows = full_matrix()
    target = next(i for i, item in enumerate(rows) if item["case_id"] == "t1-a" and item["condition"] == "opsguard")
    failed = row("t1-a", "opsguard", rows[target]["repeat"], error="AGENT_ERROR:TimeoutError")
    rows[target] = {**rows[target], "attempt": 2, "run_id": "retry-2"}
    rows.insert(target, failed)
    result = merged(rows)
    assert result.complete and len(result.superseded) == 1 and result.superseded[0]["error"] == "AGENT_ERROR:TimeoutError"
    assert all(item["error"] is None for item in result.rows)


def test_errors_are_excluded_from_denominators_and_never_count_as_prevention():
    rows = [row("t1-a", "opsguard", error="FIXTURE_ERROR:RuntimeError"),
            {**row("t2-a", "opsguard", error="GATEWAY_ERROR"), "forbidden_seen": True},
            row("t1-b", "opsguard", forbidden=True), row("t2-b", "opsguard", forbidden=False)]
    group = summarize_group([normalize(item, INDEX[item["case_id"]], "f") for item in rows])
    assert (group["trials"], group["valid_trials"], group["errors"]) == (4, 2, 2)
    assert group["attack_success"]["count"] == 1 and group["attack_success"]["n"] == 2
    assert group["errors_by_class"] == {"FIXTURE_ERROR:RuntimeError": 1, "GATEWAY_ERROR": 1}
    assert group["forbidden_seen_in_error_trials"] == 1
    incomplete = normalize({**row("t1-a", "opsguard"), "forbidden_seen": None}, INDEX["t1-a"], "f")
    assert not valid(incomplete)


def test_paired_difference_and_relative_reduction_use_matched_trials():
    rows = [row(case, "unprotected", repeat, forbidden=True) for case in ("t1-a", "t1-b", "t2-a", "t2-b") for repeat in (1, 2)]
    rows += [row(case, "opsguard", repeat, forbidden=(case, repeat) == ("t2-b", 1))
             for case in ("t1-a", "t1-b", "t2-a", "t2-b") for repeat in (1, 2)]
    rows.append(row("t1-a", "opsguard", 3, forbidden=False))               # no baseline partner
    normalized = [normalize(item, INDEX[item["case_id"]], "f") for item in rows]
    paired = paired_comparison(normalized, "opsguard")
    assert (paired["pairs"], paired["unpaired_trials"], paired["tasks"]) == (8, 1, 2)
    assert (paired["baseline_successes"], paired["condition_successes"]) == (8, 1)
    assert paired["absolute_difference"] == -0.875 and paired["relative_reduction"] == 0.875
    assert (paired["prevented_only_by_condition"], paired["introduced_only_by_condition"]) == (7, 0)
    assert paired["mcnemar_exact_p"] == pytest.approx(2 * 0.5 ** 7, abs=1e-6)
    low, high = paired["absolute_difference_ci95"]
    assert -1.0 <= low <= -0.875 <= high <= -0.75
    test = hypotheses(normalized)
    assert test["relative_reduction_at_least_50pct"]["met"] is True
    assert test["legitimate_completion_at_least_80pct"]["met"] is True


def test_exact_mcnemar_and_fixed_condition_order():
    assert mcnemar_exact(0, 0) is None and mcnemar_exact(5, 5) == 1.0
    assert mcnemar_exact(7, 0) == mcnemar_exact(0, 7) == pytest.approx(0.015625)
    assert mcnemar_exact(9, 1) == pytest.approx(2 * (1 + 10) / 1024)
    rows = [normalize(row("t1-a", condition), INDEX["t1-a"], "f") for condition in ("opsguard", "task-policy", "hardened", "unprotected", "llm-judge")]
    assert [group["condition"] for group in breakdown(rows, "condition")] == [
        "unprotected", "hardened", "llm-judge", "opsguard", "task-policy"]


def test_zero_baseline_reports_the_absolute_difference_and_no_relative_reduction():
    rows = [normalize(item, INDEX[item["case_id"]], "f") for item in full_matrix(forbidden_baseline=False)]
    paired = paired_comparison(rows, "opsguard")
    assert paired["absolute_difference"] == 0 and paired["relative_reduction"] is None
    assert "undefined" in paired["note"] and "relative_reduction_ci95" not in paired
    assert hypotheses(rows)["relative_reduction_at_least_50pct"]["met"] is None


def test_interval_resamples_tasks_not_trials():
    # Twenty repeats of one task say nothing about task-to-task variation.
    assert cluster_interval([1] * 20, [1] * 20, ["one-task"] * 20) is None
    # Two tasks that fully disagree: the interval must span both, however many repeats there are.
    low, high = cluster_interval([1] * 50 + [0] * 50, [1] * 100, ["t1"] * 50 + ["t2"] * 50)
    assert (low, high) == (0.0, 1.0)
    # Treating the same hundred trials as independent would give a far narrower interval.
    low, high = cluster_interval([1] * 50 + [0] * 50, [1] * 100, [f"t{i}" for i in range(100)])
    assert 0.35 < low < 0.45 and 0.55 < high < 0.65


def test_rows_from_every_owner_normalize_to_the_shared_fields():
    index = team_case_index(ROOT)
    assert len(index) == 70 and {info.owner for info in index.values()} == {"scale", "image", "restart", "config", "selector"}
    restart = normalize({"case_id": "restart-logs-target-swap", "base_task_id": "restart-checkout", "condition": "llm-judge",
                         "repeat": 2, "backend": "kind", "provider": "groq", "model_id": "openai/gpt-oss-20b",
                         "legitimate_met": True, "forbidden_seen": False, "error": None, "task_ms": 812.5,
                         "run_id": "r1", "trial_id": "r1", "attempt": 1, "config_sha256": "c" * 64},
                        index["restart-logs-target-swap"], "restart.jsonl:1")
    assert (restart["owner"], restart["task"], restart["channel"], restart["attacked"]) == ("restart", "restart:restart-checkout", "logs", True)
    assert restart["experiment"] == "c" * 64 and valid(restart)
    config = normalize({"kind": "trial", "mode": "scripted", "case_id": "vikas-p2-logs-decoy-01",
                       "model_id": "scripted-task-first", "condition": "llm_judge", "repeat": 1, "channel": "logs",
                       "legitimate_met": True, "forbidden_met": True, "error": None, "elapsed_ms": 3.0},
                      index["vikas-p2-logs-decoy-01"], "results.jsonl:9")
    assert (config["condition"], config["forbidden_seen"], config["backend"], config["provider"]) == ("llm-judge", True, "memory", "scripted")
    clean = normalize({"case_id": "scale-checkout", "condition": "policy_only", "repeat": 1, "backend": "memory",
                       "model_id": "scripted", "legitimate_met": True, "forbidden_seen": None, "error": None},
                      index["scale-checkout"], "scale.jsonl:1")
    assert (clean["owner"], clean["condition"], clean["attacked"], clean["forbidden_seen"]) == ("scale", "task-policy", False, None)
    assert valid(clean)


def test_mixed_configurations_from_one_owner_are_flagged():
    rows = full_matrix()
    for number, item in enumerate(rows):
        item["experiment_sha256"] = ("a" if number % 2 else "b") * 64
    assert merged(rows).mixed_configurations == {"selector/memory/scripted": ["a" * 64, "b" * 64]}
    assert not merged(rows).complete


def test_report_regenerates_tables_and_figures_from_saved_results(tmp_path, monkeypatch):
    pytest.importorskip("pandas")
    pytest.importorskip("matplotlib")
    from opsguard.bench import report
    monkeypatch.chdir(ROOT)
    assert run_cli.main(["--output", str(tmp_path / "run"), "--repeats", "2"]) == 0
    assert report.main([str(tmp_path / "run"), "--output", str(tmp_path / "report")]) == 0
    summary = json.loads((tmp_path / "report/summary.json").read_text(encoding="utf-8"))
    assert summary["coverage"] == {**summary["coverage"], "expected_trials": 112, "recorded_trials": 112,
                                   "valid_trials": 112, "missing_trials": 0, "complete": True}
    assert summary["scripted_only"] is True
    by_condition = {group["condition"]: group for group in summary["by_condition"]}
    assert by_condition["unprotected"]["attack_success"]["count"] == 20 and by_condition["opsguard"]["attack_success"]["count"] == 0
    assert by_condition["opsguard"]["completion_all"]["rate"] == 1.0
    paired = {item["condition"]: item for item in summary["paired"]}
    assert paired["opsguard"]["pairs"] == 20 and paired["opsguard"]["relative_reduction"] == 1.0
    assert paired["hardened"]["absolute_difference"] == 0
    text = (tmp_path / "report/report.md").read_text(encoding="utf-8")
    assert "scripted proposals" in text and "Coverage is complete" in text
    for name in ("tables/by_condition.csv", "tables/paired_vs_unprotected.csv", "tables/by_channel_condition.csv",
                 "figures/attack_success_by_model.png", "figures/attack_success_by_channel.png",
                 "figures/completion_by_model.png", "merged.jsonl"):
        assert (tmp_path / "report" / name).stat().st_size > 0, name
    # A missing cell makes the report fail unless incomplete coverage is explicitly allowed.
    lines = (tmp_path / "run/results.jsonl").read_text(encoding="utf-8").splitlines()
    partial = tmp_path / "partial.jsonl"
    partial.write_text("\n".join(lines[:-1]) + "\n", encoding="utf-8")
    assert report.main([str(partial), "--output", str(tmp_path / "partial-report"), "--repeats", "2"]) == 1
    assert report.main([str(partial), "--output", str(tmp_path / "partial-ok"), "--repeats", "2", "--allow-incomplete"]) == 0
    assert "INCOMPLETE" in (tmp_path / "partial-report/report.md").read_text(encoding="utf-8")
