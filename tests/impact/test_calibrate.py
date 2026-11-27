"""S4-P4: threshold calibration, sensitivity and the thresholds file."""

import json
from pathlib import Path

import pytest

from opsguard.impact import calibrate
from opsguard.impact.calibrate import (GRID, all_tasks, choose, derive_records, load_records, metrics, renormalised,
                                       score, settings_for)
from opsguard.impact.decision import RiskSettings, load_thresholds

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def scored(monkeypatch_module=None):
    import os
    cwd = os.getcwd()
    os.chdir(ROOT)
    try:
        tasks = all_tasks(ROOT)
        records = [r for r in derive_records(list(tasks.values())) if r["split"] in ("dev", "val")]
        return [score(r, tasks[r["task_id"]]) for r in records]
    finally:
        os.chdir(cwd)


def test_derived_labels_follow_task_design():
    tasks = all_tasks(ROOT)
    records = derive_records([tasks["ms-reroute-then-drain-01"]])
    labels = [(len(r["context"]), r["proposal"]["verb"], r["label"]) for r in records]
    assert labels == [(0, "scale", "safe"), (0, "set_selector", "safe"), (0, "scale", "safe"), (1, "scale", "unsafe")]
    assert all(r["label_source"] == "derived-from-task-design" for r in records)


def test_committed_labelled_file_matches_the_derivation():
    tasks = all_tasks(ROOT)
    committed = [json.loads(line) for line in (ROOT / calibrate.DERIVED).read_text().splitlines()]
    assert committed == derive_records(list(tasks.values()))


def test_test_split_is_never_loaded(tmp_path):
    path = tmp_path / "labelled.jsonl"
    path.write_text("\n".join(json.dumps({"split": s, "id": s}) for s in ("dev", "test", "val", "test")))
    kept, skipped = load_records(path)
    assert [r["split"] for r in kept] == ["dev", "val"] and skipped == 2


def test_full_decision_separates_safe_and_unsafe_on_development_and_validation(scored):
    for split in ("dev", "val"):
        m = metrics([s for s in scored if s["split"] == split], RiskSettings())
        assert m["unsafe_approval_rate"] == 0 and m["false_block_rate"] == 0


def test_graded_score_alone_cannot_separate(scored):
    dev = [s for s in scored if s["split"] == "dev"]
    rows = [metrics(dev, settings_for(t), envelope=False) for t in GRID]
    assert all(r["unsafe_approval_rate"] > 0.5 or r["false_block_rate"] > 0.5 for r in rows)


def test_choose_keeps_the_default_when_data_cannot_tell_thresholds_apart(scored):
    dev = [s for s in scored if s["split"] == "dev"]
    chosen, m = choose(dev, 0.05)
    assert chosen.escalate_at == RiskSettings().escalate_at and m["identifiable"]


def test_choose_respects_the_ceiling():
    unsafe = {"id": "u", "label": "unsafe", "split": "dev", "family": "f", "breach": False, "breach_status": None,
              "features": {"provenance_risk": 0.0, "blast_radius": 1 / 3, "irreversibility": 0.0}}
    safe = {**unsafe, "id": "s", "label": "safe", "features": {"provenance_risk": 0.0, "blast_radius": 0.0,
                                                                "irreversibility": 0.0}}
    chosen, m = choose([unsafe, safe], 0.0)
    assert chosen.escalate_at <= 0.5 / 3 + 1e-9 and m["unsafe_approved"] == 0 and m["safe_stopped"] == 0
    with pytest.raises(ValueError):
        choose([{**unsafe, "features": {**unsafe["features"], "blast_radius": 0.0}}], 0.0)


def test_renormalised_weights_sum_to_one():
    for weight in calibrate.WEIGHTS:
        for factor in (0.5, 1.5):
            values = renormalised(RiskSettings(), weight, factor)
            RiskSettings(**values)
            assert abs(sum(values.values()) - 1) < 1e-12


def test_committed_thresholds_file_loads():
    settings = load_thresholds(ROOT / "config/thresholds.yaml")
    assert 0 < settings.escalate_at < settings.block_at <= 1
    assert "provisional" in (ROOT / "config/thresholds.yaml").read_text()
