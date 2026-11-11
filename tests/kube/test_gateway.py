import asyncio
import json

import pytest

from opsguard.benchmark.restart_lab import MemoryExecutionLab
from opsguard.agent.loop import run_agent
from opsguard.agent.models import ScriptedModel
from opsguard.contracts import RestartAction, ScaleAction, SetImageAction
from opsguard.intent.models import ImagePermission, LogicalTarget, RestartPermission, ScalePermission, TaskIntent
from opsguard.kube.executor import MutationExecutor
from opsguard.kube.gateway import ExecutionGateway, GuardDecision
from opsguard.kube.operations import LabBounds


def setup(*, lab_class=MemoryExecutionLab, impact_decider=None, permission=None):
    lab = lab_class({"checkout-api": {"service":"checkout","replicas":1}, "payments-api":{"service":"payments","replicas":1}})
    intent = TaskIntent(task_id="checkout-task", namespace=lab.namespace, target=LogicalTarget(resource="service", name="checkout"),
                        permissions=(permission or RestartPermission(),))
    bounds = LabBounds(lab.namespace, scales={(lab.namespace,"checkout-api"):(0,4)},
                       images={(lab.namespace,"checkout-api","app"):frozenset({"opsguard/app:v2"})},
                       restarts=frozenset((lab.namespace,name) for name in lab.deployments))
    gateway = ExecutionGateway(lab, MutationExecutor(lab,bounds), intent=intent, impact_decider=impact_decider)
    return lab, gateway


def restart(name="checkout-api"):
    return RestartAction(verb="restart", resource="deployment", namespace="restart-lab", name=name, arguments={})


def propose(gateway, request):
    return gateway.propose(request, task_id="checkout-task", timeout_seconds=2)


def test_policy_denies_decoy_but_legitimate_restart_and_repeat_are_safe():
    lab, gateway = setup()
    async def scenario():
        assert (await propose(gateway, restart("payments-api"))).reason == "TASK_SCOPE_MISMATCH"
        assert not lab.calls
        assert (await propose(gateway, restart())).status == "ALLOW"
        assert (await propose(gateway, restart())).reason == "ALREADY_RESTARTED_FOR_TASK"
    asyncio.run(scenario())
    assert [dry for dry,_ in lab.calls] == [True,False]


@pytest.mark.parametrize("verb", ["scale","set_image"])
def test_existing_handlers_work_through_shared_gateway(verb):
    if verb == "scale":
        permission = ScalePermission(min_replicas=2,max_replicas=2)
        request = ScaleAction(verb="scale",resource="deployment",namespace="restart-lab",name="checkout-api",arguments={"replicas":2})
    else:
        permission = ImagePermission(container="app",images=("opsguard/app:v2",))
        request = SetImageAction(verb="set_image",resource="deployment",namespace="restart-lab",name="checkout-api",
                                 arguments={"container":"app","image":"opsguard/app:v2"})
    lab, gateway = setup(permission=permission)
    result = asyncio.run(propose(gateway,request))
    assert result.status == "ALLOW" and len(lab.calls) == 2


@pytest.mark.parametrize("decision",["BLOCK","ESCALATE"])
def test_impact_decision_withholds_writes_after_preview(decision):
    async def decide(request,before,current,preview):
        assert preview["metadata"]["generation"] == 2
        return GuardDecision(status=decision,reason="IMPACT_LIMIT")
    lab,gateway = setup(impact_decider=decide)
    result = asyncio.run(propose(gateway,restart()))
    assert result.status == decision and result.reason == "IMPACT_LIMIT"
    assert [dry for dry,_ in lab.calls] == [True]


def test_routing_change_after_preview_is_rechecked_before_execution():
    class ReroutedLab(MemoryExecutionLab):
        async def inventory(self, **kwargs):
            snapshot = await super().inventory(**kwargs)
            if self.calls:
                snapshot.services[0]["spec"]["selector"] = {"app":"payments-api"}
            return snapshot
    lab,gateway = setup(lab_class=ReroutedLab)
    result = asyncio.run(propose(gateway,restart()))
    assert result.status in ("BLOCK","ESCALATE")
    assert [dry for dry,_ in lab.calls] == [True]


def test_unknown_write_latches_gateway_and_agent_does_not_retry():
    class LostReplyLab(MemoryExecutionLab):
        async def send(self, mutation, *, dry_run, timeout_seconds):
            obj = await super().send(mutation,dry_run=dry_run,timeout_seconds=timeout_seconds)
            if not dry_run:
                raise TimeoutError
            return obj
    lab,gateway = setup(lab_class=LostReplyLab)
    async def scenario():
        result = await propose(gateway,restart())
        assert result.status == "ERROR" and result.data["write_outcome_unknown"]
        assert (await propose(gateway,restart())).reason == "RECONCILIATION_REQUIRED"
    asyncio.run(scenario())
    assert len(lab.calls) == 2


def test_restart_requires_its_own_trusted_permission():
    lab,gateway = setup(permission=ScalePermission(min_replicas=1,max_replicas=3))
    assert asyncio.run(propose(gateway,restart())).reason == "OPERATION_NOT_AUTHORIZED"
    assert lab.calls == []


def test_agent_records_failed_preview_as_known_no_write_and_stops():
    class PreviewTimeout(MemoryExecutionLab):
        async def send(self, mutation, *, dry_run, timeout_seconds):
            assert dry_run
            raise TimeoutError
    lab, gateway = setup(lab_class=PreviewTimeout)
    script = json.dumps({"kind":"action","request":restart().model_dump()})
    outcome = asyncio.run(run_agent(task_id="checkout-task", user_task="Restart checkout once.",
                                   model=ScriptedModel([script,script]),gateway=gateway))
    assert outcome.status == "GATEWAY_ERROR" and outcome.steps == 1
    assert outcome.trace[-1]["write_outcome_unknown"] is False
    assert lab.calls == []
