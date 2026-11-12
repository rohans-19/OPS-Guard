"""Validate Rishab's confirmed tasks separately from independent case predicates."""

from collections import Counter
import json

from pydantic import Field, TypeAdapter

from opsguard.benchmark.scale_cases import Injection
from opsguard.contracts import DnsLabel, ObjectName, WireModel
from opsguard.intent.models import RestartPermission, TaskIntent


class RestartCase(WireModel):
    id: DnsLabel
    base_task_id: DnsLabel
    legitimate: ObjectName
    forbidden: ObjectName | None = None
    injection: Injection | None = None


class RestartWorkload(WireModel):
    service: DnsLabel
    replicas: int = Field(ge=1, le=2)


def load_restart_suite(root):
    folder = root / "benchmark/cases/rishab"
    parsed = TypeAdapter(dict[ObjectName, RestartWorkload]).validate_json((folder / "workloads.json").read_bytes())
    workloads = {name: fixture.model_dump() for name, fixture in parsed.items()}
    if len({fixture["service"] for fixture in workloads.values()}) != len(workloads):
        raise ValueError("fixture Services must have distinct backends")
    raw_tasks = json.loads((folder / "intents.json").read_text(encoding="utf-8"))
    tasks = [TaskIntent.model_validate_json(json.dumps(raw)) for raw in raw_tasks]
    intents = {task.task_id: task for task in tasks}
    cases = tuple(RestartCase.model_validate(raw) for raw in json.loads((folder / "cases.json").read_text(encoding="utf-8")))
    if len(intents) != 4 or len(tasks) != 4 or len(cases) != 14 or len({case.id for case in cases}) != 14:
        raise ValueError("restart suite requires four tasks and fourteen unique cases")
    if Counter(case.injection.channel for case in cases if case.injection) != {"logs": 2, "alerts": 4, "runbooks": 2, "configmaps": 2}:
        raise ValueError("restart attack channel allocation differs from plan")
    if Counter(case.base_task_id for case in cases if not case.injection) != {task: 1 for task in intents}:
        raise ValueError("each trusted task needs one clean case")
    for case in cases:
        intent = intents[case.base_task_id]
        if intent.namespace != "restart-lab" or len(intent.permissions) != 1 or not isinstance(intent.permissions[0], RestartPermission):
            raise ValueError("invalid trusted restart task")
        matches = [name for name, fixture in workloads.items() if (
            fixture["service"] == intent.target.name if intent.target.resource == "service" else name == intent.target.name)]
        if matches != [case.legitimate] or case.legitimate in intent.protected_deployments:
            raise ValueError("legitimate predicate disagrees with confirmed task")
        if bool(case.injection) != bool(case.forbidden) or (case.forbidden and (
            case.forbidden not in workloads or case.forbidden == case.legitimate)):
            raise ValueError("attack must have a distinct executable forbidden target")
        if any(type(fixture["replicas"]) is not int or not 1 <= fixture["replicas"] <= 2 for fixture in workloads.values()):
            raise ValueError("fixture replicas outside bounds")
    return workloads, intents, cases
