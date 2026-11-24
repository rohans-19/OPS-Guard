import asyncio
from pathlib import Path

from opsguard.agent.models import ScriptedModel
from opsguard.benchmark.defences import ModelJudge
from opsguard.benchmark.restart_cases import load_restart_suite
from opsguard.benchmark.restart_trial import run_trial
from opsguard.contracts import RestartAction


def test_judge_is_a_separate_model_verdict_without_policy_substitution():
    judge=ModelJudge(ScriptedModel(['{"allow":false,"reason":"Wrong target"}']),user_task="Restart checkout",evidence=[])
    action=RestartAction(verb="restart",resource="deployment",namespace="shop",name="payments",arguments={})
    result=asyncio.run(judge(action,None,None,None))
    assert result.status=="BLOCK" and result.reason=="JUDGE_DENY"


def test_full_impact_condition_completes_a_rolling_restart():
    # A rolling restart keeps the old replica serving until its replacement is
    # ready, so the effect model predicts no outage and OpsGuard allows it.
    root=Path(__file__).resolve().parents[2]
    workloads,intents,cases=load_restart_suite(root)
    case=cases[0]
    row=asyncio.run(run_trial(root=root,workloads=workloads,intent=intents[case.base_task_id],case=case,condition="opsguard"))
    assert row["error"] is None and row["legitimate_met"] and row["safe_completion"]
    assert not any(event.get("status")=="ESCALATE" for event in row["timings"])
    assert row["impact_audit"] and any(item.get("execution",{}).get("status")=="APPLIED" for item in row["executor_audit"])


def test_full_impact_condition_blocks_every_attacked_restart():
    root=Path(__file__).resolve().parents[2]
    workloads,intents,cases=load_restart_suite(root)
    for case in cases:
        row=asyncio.run(run_trial(root=root,workloads=workloads,intent=intents[case.base_task_id],case=case,condition="opsguard"))
        assert row["error"] is None
        assert not row["forbidden_seen"], case.case_id
