"""Task-scoped image gateway, with execution supplied through a narrow backend."""

from typing import Protocol
from uuid import uuid4

from opsguard.agent.interfaces import GatewayResult
from opsguard.contracts import DeploymentRead, RunbookRead, SetImageAction
from opsguard.evidence.runbooks import RunbookReader
from opsguard.intent.evidence import EvidenceRegistry
from opsguard.intent.inventory import InventoryReader, KubernetesInventory
from opsguard.intent.models import TaskIntent
from opsguard.intent.policy import evaluate_policy
from opsguard.intent.resolver import resolve_target
from opsguard.operations.set_image import ImageConflict, PreparedImage, prepare_set_image, send_image_mutation


class ImageBackend(InventoryReader, Protocol):
    async def replace_image(self, mutation: PreparedImage, *, dry_run: bool, timeout_seconds: float) -> None: ...


class KubernetesImageBackend(KubernetesInventory):
    """Use separate caller-configured reader and executor clients in a named namespace."""

    def __init__(self, namespace, reader, executor):
        super().__init__(namespace, reader)
        self.executor = executor

    async def replace_image(self, mutation: PreparedImage, *, dry_run: bool, timeout_seconds: float) -> None:
        if mutation.target.namespace != self.namespace:
            raise ValueError("mutation is outside the configured namespace")
        await send_image_mutation(self.executor, mutation, dry_run=dry_run, timeout_seconds=timeout_seconds)


class ImageGateway:
    """Policy authorization only; full impact-aware OpsGuard is a later integration."""

    def __init__(self, backend: ImageBackend, *, intent: TaskIntent, lab_images,
                 runbooks: RunbookReader, enforce_task_policy: bool = True):
        self.backend, self.intent = backend, intent
        self.lab_images = dict(lab_images)
        self.runbooks = runbooks
        self.enforce_task_policy = enforce_task_policy
        self.evidence = EvidenceRegistry(intent.task_id)

    async def read(self, request, *, task_id: str, timeout_seconds: float) -> GatewayResult:
        if task_id != self.intent.task_id or request.namespace != self.intent.namespace:
            return GatewayResult(status="BLOCK", reason="READ_OUT_OF_SCOPE")
        inventory = await self.backend.inventory(timeout_seconds=timeout_seconds)
        resolved = resolve_target(self.intent, inventory)
        if resolved.status != "PASS" or request.name != resolved.target.name:
            return GatewayResult(status="BLOCK", reason="READ_TARGET_UNVERIFIED")
        if isinstance(request, DeploymentRead):
            dep = next(obj for obj in inventory.deployments if obj["metadata"]["name"] == request.name
                       and obj["metadata"]["namespace"] == self.intent.namespace)
            return GatewayResult(status="OK", reason="DEPLOYMENT_STATE", data={
                "name": request.name, "uid": resolved.target.uid,
                "images": {item["name"]: item["image"] for item in dep["spec"]["template"]["spec"]["containers"]},
                "generation": dep["metadata"].get("generation"), "status": dep.get("status", {}),
            })
        if isinstance(request, RunbookRead):
            try:
                evidence = self.runbooks.read(request)
            except PermissionError:
                return GatewayResult(status="BLOCK", reason="RUNBOOK_OUT_OF_SCOPE")
            self.evidence.add(evidence)
            return GatewayResult(status="OK", reason="RUNBOOK_EVIDENCE", data={"observation": evidence.model_dump()})
        return GatewayResult(status="BLOCK", reason="UNSUPPORTED_READ")

    async def propose(self, request, *, task_id: str, timeout_seconds: float,
                      observation_ids: tuple[str, ...] = ()) -> GatewayResult:
        decision_id = uuid4().hex

        def result(status, reason, **data):
            return GatewayResult(status=status, reason=reason, decision_id=decision_id, data=data)

        if not isinstance(request, SetImageAction):
            return result("BLOCK", "UNSUPPORTED_OPERATION")
        if task_id != self.intent.task_id or request.namespace != self.intent.namespace:
            return result("BLOCK", "OUTSIDE_LAB_BOUNDS")
        if not self.evidence.contains(task_id, observation_ids):
            return result("BLOCK", "UNKNOWN_OBSERVATION")
        inventory = await self.backend.inventory(timeout_seconds=timeout_seconds)
        policy = None
        if self.enforce_task_policy:
            policy = evaluate_policy(request, intent=self.intent, task_id=task_id, inventory=inventory,
                                     evidence=self.evidence, observation_ids=observation_ids)
            if policy.status != "PASS":
                return result(policy.status, policy.reason, policy=policy.model_dump(mode="json"))
        deployments = [obj for obj in inventory.deployments if obj["metadata"]["name"] == request.name
                       and obj["metadata"]["namespace"] == request.namespace]
        if len(deployments) != 1:
            return result("BLOCK", "TARGET_NOT_FOUND")
        try:
            prepared = prepare_set_image(request, deployment=deployments[0], lab_images=self.lab_images)
        except ValueError:
            return result("BLOCK", "OUTSIDE_LAB_BOUNDS")
        if policy and prepared.target != policy.resolution.target:
            return result("BLOCK", "STALE_TARGET")
        details = {"name": request.name, "container": prepared.container, "image": prepared.image,
                   **({"policy": policy.model_dump(mode="json")} if policy else {})}
        current = deployments[0]["spec"]["template"]["spec"]["containers"][prepared.container_index]
        if current["image"] == prepared.image:
            return result("ALLOW", "ALREADY_AT_DESIRED_IMAGE", **details)
        try:
            await self.backend.replace_image(prepared, dry_run=True, timeout_seconds=timeout_seconds)
            await self.backend.replace_image(prepared, dry_run=False, timeout_seconds=timeout_seconds)
        except ImageConflict:
            return result("BLOCK", "STALE_TARGET")
        return result("ALLOW", "IMAGE_REQUEST_ACCEPTED", **details)
