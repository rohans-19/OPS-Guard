import asyncio
import os
from pathlib import Path

import pytest

from opsguard.bench.loader import load_cases
from opsguard.bench.runner import TrialSettings, run_trial

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.skipif(not os.environ.get("OPSGUARD_KIND_CLUSTER") or not os.environ.get("OPSGUARD_FIXTURE_IMAGE"),
                    reason="opt in with OPSGUARD_KIND_CLUSTER and OPSGUARD_FIXTURE_IMAGE")
def test_selector_smoke_on_named_kind_cluster():
    """Live path: the same spoofed selector changes routing without the guard and is withheld with it."""
    settings = TrialSettings(backend="kind", cluster=os.environ["OPSGUARD_KIND_CLUSTER"],
                             image=os.environ["OPSGUARD_FIXTURE_IMAGE"])
    cases = {case.id: case for case in load_cases(ROOT)}

    def trial(case_id, condition):
        row, trace = asyncio.run(run_trial(root=ROOT, case=cases[case_id], condition=condition, settings=settings))
        assert row["error"] is None, (row["error"], trace.get("error_detail"))
        return row

    clean = trial("selector-checkout-clean-01", "opsguard")
    assert clean["legitimate_met"] is True and clean["collateral_seen"] is False
    # A pod-log case, so no local Alertmanager is needed.
    baseline = trial("selector-logs-decoy-route-01", "unprotected")
    guarded = trial("selector-logs-decoy-route-01", "opsguard")
    assert baseline["evidence_exposed"] is True and baseline["delivery"] == "pod_log"
    assert baseline["forbidden_seen"] is True
    assert guarded["forbidden_seen"] is False and guarded["legitimate_met"] is True
