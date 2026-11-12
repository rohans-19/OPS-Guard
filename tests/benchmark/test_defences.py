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


def test_full_impact_condition_preserves_uncertain_restart_escalation():
    root=Path(__file__).resolve().parents[2]
    workloads,intents,cases=load_restart_suite(root)
    case=cases[0]
    row=asyncio.run(run_trial(root=root,workloads=workloads,intent=intents[case.base_task_id],case=case,condition="opsguard"))
    assert row["error"] is None and not row["legitimate_met"]
    assert any(event["status"]=="ESCALATE" for event in row["timings"])
    assert row["impact_audit"] and not any(item.get("execution",{}).get("status")=="APPLIED" for item in row["executor_audit"])
