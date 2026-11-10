"""Validate independently supplied image tasks, fixture state and scoring cases."""

from pathlib import Path

from pydantic import Field, TypeAdapter

from opsguard.benchmark.scale_cases import Injection
from opsguard.contracts import DnsLabel, ImageReference, ObjectName, WireModel
from opsguard.intent.models import ImagePermission, TaskIntent


class ImageOutcome(WireModel):
    deployment: ObjectName
    container: DnsLabel
    image: ImageReference


class ImageCase(WireModel):
    id: DnsLabel
    base_task_id: DnsLabel
    legitimate: ImageOutcome
    injection: Injection | None = None
    forbidden: ImageOutcome | None = None


class ImageWorkload(WireModel):
    service: DnsLabel
    containers: dict[DnsLabel, ImageReference] = Field(min_length=1)
    healthy_images: tuple[ImageReference, ...] = Field(min_length=1)
    lab_images: tuple[ImageReference, ...] = Field(min_length=1)


def load_image_suite(root: Path):
    folder = root / "benchmark/cases/sandesh"
    workloads = TypeAdapter(dict[ObjectName, ImageWorkload]).validate_json((folder / "workloads.json").read_bytes())
    intents = TypeAdapter(dict[DnsLabel, TaskIntent]).validate_json((folder / "intents.json").read_bytes())
    cases = TypeAdapter(tuple[ImageCase, ...]).validate_json((folder / "cases.json").read_bytes())
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("duplicate image case ID")
    if len({workload.service for workload in workloads.values()}) != len(workloads):
        raise ValueError("fixture Services must have unique backends")
    for name, task in intents.items():
        if task.task_id != name or task.namespace != "image-lab":
            raise ValueError("trusted image task identity differs from the fixture")
    for case in cases:
        task = intents[case.base_task_id]
        outcome = case.legitimate
        workload = workloads[outcome.deployment]
        target = workload.service if task.target.resource == "service" else outcome.deployment
        if target != task.target.name or len(task.permissions) != 1:
            raise ValueError("independent intent and outcome disagree")
        permission = task.permissions[0]
        if (not isinstance(permission, ImagePermission) or permission.container != outcome.container
                or permission.images != (outcome.image,)):
            raise ValueError("independent image bounds and outcome disagree")
        if (case.injection is None) != (case.forbidden is None):
            raise ValueError("attack needs both injection and forbidden predicate")
        if case.forbidden == outcome:
            raise ValueError("forbidden outcome must differ from the legitimate outcome")
        for expected in (outcome, case.forbidden):
            if expected is None:
                continue
            fixture = workloads[expected.deployment]
            if expected.container not in fixture.containers or expected.image not in fixture.lab_images:
                raise ValueError("scored image change must be expressible through the common lab tools")
            if fixture.containers[expected.container] == expected.image:
                raise ValueError("outcome already holds before the trial")
    return {name: workload.model_dump(mode="json") for name, workload in workloads.items()}, intents, cases
