"""Load and validate CaseSpec files before anything touches a cluster."""

from __future__ import annotations

from collections import Counter
import hashlib
import json
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from opsguard.bench import predicates
from opsguard.bench.schema import CHANNELS, CaseSpec
from opsguard.contracts import SupportedAction, parse_json

CASE_ROOT = Path("benchmark/cases")
FIXTURE_ROOT = Path("benchmark/fixtures")
MAX_CASE_FILE_BYTES = 262_144
# The team plan's allocation for the selector workstream: four clean tasks and ten attacked cases.
OWNER_ALLOCATION = {"selector": {"clean": 4, "logs": 3, "alerts": 3, "runbooks": 2, "configmaps": 2}}


class CaseError(ValueError):
    """A case file failed validation. The message names the case and the reason."""


def _action(action, namespace: str = "validation"):
    return TypeAdapter(SupportedAction).validate_python({**action.model_dump(), "namespace": namespace})


def validate_case(case: CaseSpec, root: Path) -> None:
    def fail(reason):
        raise CaseError(f"{case.id}: {reason}")

    fixture = (root / FIXTURE_ROOT / case.fixture).resolve()
    if not fixture.is_relative_to((root / FIXTURE_ROOT).resolve()) or not fixture.is_file():
        fail(f"fixture path does not exist: {case.fixture}")
    for label, spec in (("legitimate", case.legitimate), ("forbidden", case.forbidden), ("collateral", case.collateral)):
        if spec is None:
            continue
        try:
            predicates.check_arguments(spec.predicate, spec.args)
        except ValueError as error:
            fail(f"{label} {error}")
    if case.injection and case.injection.channel not in CHANNELS:
        fail("unknown channel")
    for label, action in (("legitimate", case.replay.legitimate), ("forbidden", case.replay.forbidden)):
        if action is None:
            continue
        try:
            _action(action)
        except ValidationError:
            fail(f"{label} replay is not expressible through the shared typed tools")
    if set("{}") & set(case.user_task.replace("{namespace}", "")):
        fail("user_task may only use the {namespace} placeholder")
    if case.injection and case.injection.payload in case.user_task:
        fail("the payload must reach the agent through its evidence channel, not the task")


def load_case_file(path: Path, root: Path) -> tuple[CaseSpec, ...]:
    raw = path.read_bytes()
    if len(raw) > MAX_CASE_FILE_BYTES:
        raise CaseError(f"{path.name}: case file exceeds size limit")
    try:
        items = parse_json(raw.decode("utf-8"), max_bytes=MAX_CASE_FILE_BYTES)
    except ValueError as error:
        raise CaseError(f"{path.name}: {error}") from None
    if not isinstance(items, list) or not items:
        raise CaseError(f"{path.name}: expected a nonempty JSON list of cases")
    cases = []
    for index, item in enumerate(items):
        try:
            # JSON mode: lists and numbers arrive as JSON types under strict validation.
            case = CaseSpec.model_validate_json(json.dumps(item))
        except ValidationError as error:
            name = item.get("id", f"#{index}") if isinstance(item, dict) else f"#{index}"
            detail = "; ".join(f'{".".join(map(str, e["loc"]))}: {e["msg"]}' for e in error.errors()[:3])
            raise CaseError(f"{path.name} {name}: {detail}") from None
        validate_case(case, root)
        cases.append(case)
    return tuple(cases)


def load_cases(root: Path, owners: tuple[str, ...] = ("selector",)) -> tuple[CaseSpec, ...]:
    """Load every CaseSpec file for the given owners and enforce suite-wide rules."""
    cases: list[CaseSpec] = []
    for owner in owners:
        folder = root / CASE_ROOT / owner
        files = sorted(folder.glob("cases*.json"))
        if not files:
            raise CaseError(f"{owner}: no case files under {folder.as_posix()}")
        for path in files:
            loaded = load_case_file(path, root)
            if any(case.owner != owner for case in loaded):
                raise CaseError(f"{path.name}: every case must be owned by {owner}")
            cases.extend(loaded)
    duplicates = [name for name, count in Counter(case.id for case in cases).items() if count > 1]
    if duplicates:
        raise CaseError(f"duplicate case IDs: {', '.join(sorted(duplicates))}")
    payloads = Counter(case.injection.payload for case in cases if case.injection)
    if any(count > 1 for count in payloads.values()):
        raise CaseError("each payload has exactly one designated attacked case")
    for owner in owners:
        expected = OWNER_ALLOCATION.get(owner)
        if expected and allocation(case for case in cases if case.owner == owner) != expected:
            raise CaseError(f"{owner}: case allocation differs from the team plan {expected}")
    return tuple(cases)


def allocation(cases) -> dict[str, int]:
    counts = Counter(case.channel or "clean" for case in cases)
    return {key: counts.get(key, 0) for key in ("clean", *CHANNELS)}


def select(cases: tuple[CaseSpec, ...], case_ids: list[str] | None) -> tuple[CaseSpec, ...]:
    if not case_ids:
        return cases
    unknown = set(case_ids) - {case.id for case in cases}
    if unknown:
        raise CaseError(f"unknown case ID: {', '.join(sorted(unknown))}")
    return tuple(case for case in cases if case.id in case_ids)


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
