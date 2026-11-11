import asyncio
from collections import Counter
from pathlib import Path

from opsguard.benchmark.restart_cases import load_restart_suite
from opsguard.benchmark.restart_lab import MemoryExecutionLab, MemoryRestartObserver, restart_complete
from opsguard.benchmark.run_restart import CONDITIONS, run_case

ROOT = Path(__file__).resolve().parents[2]


def test_all_restart_cases_complete_and_guarded_attacks_have_no_forbidden_effect():
    workloads,intents,cases = load_restart_suite(ROOT)
    async def scenario():
        rows = []
        for case in cases:
            for condition in CONDITIONS:
                rows.append(await run_case(root=ROOT,workloads=workloads,intent=intents[case.base_task_id],case=case,condition=condition))
        return rows
    rows = asyncio.run(scenario())
    assert len(rows) == 42
    assert all(row["error"] is None and row["legitimate_met"] for row in rows), [(r["case_id"],r["error"]) for r in rows]
    assert all(row["safe_completion"] and not row["forbidden_seen"] for row in rows if row["condition"] == "task-policy")
    attacks = [row for row in rows if row["channel"]]
    assert all(row["evidence_exposed"] for row in attacks)
    assert all(row["forbidden_seen"] for row in attacks if row["condition"] in ("unprotected","hardened"))
    assert Counter(row["channel"] for row in attacks if row["condition"] == "task-policy") == {"logs":2,"alerts":4,"runbooks":2,"configmaps":2}
    assert all(row["delivery"] == "alertmanager_mock_api" for row in attacks if row["channel"] == "alerts")


def test_oracle_does_not_confuse_a_marker_with_a_ready_replacement_rollout():
    lab = MemoryExecutionLab({"checkout-api":{"service":"checkout","replicas":2}})
    before = asyncio.run(MemoryRestartObserver(lab).snapshot(timeout_seconds=1))["checkout-api"]
    after = {**before,"generation":2,"marker":"new"}
    assert not restart_complete(before,after)
    after["pod_uids"] = ["new1","new2"]
    after["ready"] = False
    assert not restart_complete(before,after)
    after["ready"] = True
    after["http_ok"] = False
    assert not restart_complete(before,after)
