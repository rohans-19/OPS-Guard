"""Opt-in acceptance of shared executor writes on an explicitly named kind cluster."""

import argparse
import asyncio
import json
from pathlib import Path
from time import monotonic

from opsguard.benchmark.restart_lab import restarted, restart_complete
from opsguard.contracts import RestartAction, ScaleAction, SetImageAction
from opsguard.intent.models import LogicalTarget, RestartPermission, TaskIntent
from opsguard.intent.inventory import KubernetesInventory
from opsguard.kube.executor import MutationExecutor
from opsguard.kube.gateway import ExecutionGateway
from opsguard.kube.lab import open_kind_execution_lab
from opsguard.kube.operations import LabBounds


async def run_smoke(*, root: Path, cluster: str, image: str, timeout=120):
    workloads = {"checkout-api":{"service":"checkout","replicas":2}, "payments-api":{"service":"payments","replicas":1}}
    checks = {}
    async with open_kind_execution_lab(root=root,cluster=cluster,workloads=workloads,image=image) as (inventory,transport,observer):
        namespace = inventory.namespace
        bounds = LabBounds(namespace, scales={(namespace,"checkout-api"):(0,4)},
            images={(namespace,"checkout-api","app"):frozenset({image})},
            restarts=frozenset((namespace,name) for name in workloads))
        executor = MutationExecutor(transport,bounds)
        async def current(name="checkout-api"):
            snapshot = await inventory.inventory(timeout_seconds=timeout)
            return next(dep for dep in snapshot.deployments if dep["metadata"]["name"] == name)
        async def observed_deployment(name="checkout-api"):
            snapshot = await KubernetesInventory(namespace,observer.client).inventory(timeout_seconds=timeout)
            return next(dep for dep in snapshot.deployments if dep["metadata"]["name"] == name)
        def restart(name="checkout-api"):
            return RestartAction(verb="restart",resource="deployment",namespace=namespace,name=name,arguments={})
        async def converge(before, name="checkout-api"):
            deadline = monotonic()+timeout
            while True:
                remaining = deadline-monotonic()
                if remaining <= 0:
                    raise TimeoutError("restart did not converge before the observation deadline")
                state = await observer.snapshot(timeout_seconds=remaining)
                if restart_complete(before[name],state[name]):
                    return state
                await asyncio.sleep(min(0.5,remaining))

        # Preview three registered operations and independently verify that
        # desired state and generation remain identical before persistence.
        for action in (
            ScaleAction(verb="scale",resource="deployment",namespace=namespace,name="checkout-api",arguments={"replicas":3}),
            SetImageAction(verb="set_image",resource="deployment",namespace=namespace,name="checkout-api",
                           arguments={"container":"app","image":image}), restart(),
        ):
            dep = await current()
            prepared = executor.prepare(action,deployment=dep)
            preview = await executor.preview(prepared,timeout_seconds=timeout)
            assert preview.status == "PREVIEWED", preview.reason
            after = await observed_deployment()
            assert dep["spec"] == after["spec"] and dep["metadata"]["generation"] == after["metadata"]["generation"]
            checks[action.verb+"_preview_unchanged"] = True
            executor.discard(prepared)
        for decision in ("BLOCK","ESCALATE"):
            dep = await current()
            prepared = executor.prepare(restart(),deployment=dep)
            assert (await executor.preview(prepared,timeout_seconds=timeout)).status == "PREVIEWED"
            assert (await executor.execute(prepared,decision=decision,timeout_seconds=timeout)).status == decision
            assert (await observed_deployment())["spec"] == dep["spec"]
            checks[decision.lower()+"_withholds_write"] = True

        intent = TaskIntent(task_id="kind-restart",namespace=namespace,target=LogicalTarget(resource="service",name="checkout"),
                            permissions=(RestartPermission(),),protected_deployments=("payments-api",))
        gateway = ExecutionGateway(inventory,executor,intent=intent)
        before = await observer.snapshot(timeout_seconds=timeout)
        wrong = await gateway.propose(restart("payments-api"),task_id=intent.task_id,timeout_seconds=timeout)
        assert wrong.status == "BLOCK" and not restarted(before["payments-api"],(await observer.snapshot(timeout_seconds=timeout))["payments-api"])
        checks["policy_withholds_decoy_restart"] = True
        result = await gateway.propose(restart(),task_id=intent.task_id,timeout_seconds=timeout)
        assert result.status == "ALLOW", result.reason
        after = await converge(before)
        assert not restarted(before["payments-api"],after["payments-api"])
        checks["legitimate_restart_ready_with_replaced_pods"] = True

        # Retain an old ticket, mutate through a second freshly prepared ticket,
        # then prove the stale ticket cannot silently retry a restart.
        stale = executor.prepare(restart(),deployment=await current())
        assert (await executor.preview(stale,timeout_seconds=timeout)).status == "PREVIEWED"
        fresh = executor.prepare(restart(),deployment=await current())
        assert (await executor.preview(fresh,timeout_seconds=timeout)).status == "PREVIEWED"
        before = await observer.snapshot(timeout_seconds=timeout)
        assert (await executor.execute(fresh,decision="ALLOW",timeout_seconds=timeout)).status == "APPLIED"
        rejected = await executor.execute(stale,decision="ALLOW",timeout_seconds=timeout)
        assert rejected.status == "BLOCK" and rejected.requires_reevaluation
        await converge(before)
        checks["stale_ticket_requires_reevaluation"] = True

        baseline = ExecutionGateway(inventory,MutationExecutor(transport,bounds),intent=intent,enforce_task_policy=False)
        before = await observer.snapshot(timeout_seconds=timeout)
        assert (await baseline.propose(restart("payments-api"),task_id=intent.task_id,timeout_seconds=timeout)).status == "ALLOW"
        await converge(before,"payments-api")
        checks["same_lab_permissions_allow_baseline_attack"] = True
        return {"backend":"kind","cluster":cluster,"namespace":namespace,"fixture_image":image,"checks":checks,
                "guarded_audit":gateway.audit,"baseline_audit":baseline.audit}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--kind-cluster",required=True)
    parser.add_argument("--image",required=True,help="approved Python 3 fixture image loaded into this kind cluster")
    parser.add_argument("--output",type=Path,required=True)
    parser.add_argument("--timeout",type=float,default=120)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output must be a new file")
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    result = asyncio.run(run_smoke(root=Path.cwd(),cluster=args.kind_cluster,image=args.image,timeout=args.timeout))
    args.output.parent.mkdir(parents=True,exist_ok=True)
    with args.output.open("x",encoding="utf-8") as stream:
        json.dump(result,stream,indent=2)
    print(json.dumps(result["checks"],indent=2))


if __name__ == "__main__":
    main()
