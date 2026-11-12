"""Check case schemas, fixture reachability, channel coverage and the 70-case allocation.

    python -m opsguard.bench.validate            # schema and allocation checks
    python -m opsguard.bench.validate --replay   # also replay every common-runner case in memory

The replay verifies each clean fixture with its known-good action, and each
attacked fixture with a controlled attack that the unprotected lab executes
and the guarded lab withholds.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
from pathlib import Path

from opsguard.bench.channels import verify_channel_coverage
from opsguard.bench.loader import CaseError, allocation, load_cases
from opsguard.bench.runner import run_trial
from opsguard.bench.schema import CHANNELS
from opsguard.bench.selector_lab import load_fixture
from opsguard.operations.set_selector import selector_key

# Team plan, benchmark allocation table, keyed by workstream (the operation each suite exercises).
PLAN = {
    "scale": {"clean": 4, "logs": 4, "alerts": 2, "runbooks": 2, "configmaps": 2},
    "image": {"clean": 4, "logs": 2, "alerts": 2, "runbooks": 4, "configmaps": 2},
    "restart": {"clean": 4, "logs": 2, "alerts": 4, "runbooks": 2, "configmaps": 2},
    "config": {"clean": 4, "logs": 2, "alerts": 2, "runbooks": 2, "configmaps": 4},
    "selector": {"clean": 4, "logs": 3, "alerts": 3, "runbooks": 2, "configmaps": 2},
}


def _counts(channels) -> dict[str, int]:
    counts = Counter(channel or "clean" for channel in channels)
    return {key: counts.get(key, 0) for key in ("clean", *CHANNELS)}


def team_allocation(root: Path) -> dict[str, dict[str, int]]:
    """Load every workstream's suite through its own validating loader."""
    from opsguard.benchmark.image_cases import load_image_suite
    from opsguard.benchmark.restart_cases import load_restart_suite
    from opsguard.benchmark.scale_cases import load_scale_suite
    from opsguard.benchmark.vikas_cases import load_cases as load_config_cases

    def injected(cases):
        return [case.injection.channel if case.injection else None for case in cases]

    config_cases = load_config_cases(root / "benchmark/cases/vikas/configmap_cases.json")
    return {
        "scale": _counts(injected(load_scale_suite(root)[2])),
        "image": _counts(injected(load_image_suite(root)[2])),
        "restart": _counts(injected(load_restart_suite(root)[2])),
        "config": _counts(case.get("injection", {}).get("channel") for case in config_cases),
        "selector": allocation(load_cases(root)),
    }


def check_reachability(root: Path, cases) -> None:
    """A primary attack must be expressible in the shared lab bounds, or the baseline could never reach it."""
    for case in cases:
        fixture = load_fixture(root, case.fixture)
        if case.task not in fixture.tasks:
            raise CaseError(f"{case.id}: unknown task {case.task}")
        task = fixture.tasks[case.task]
        allowed = fixture.allowed_selectors()
        legitimate = case.replay.legitimate
        if legitimate.name != task.service or legitimate.arguments.get("selector") != dict(task.selector):
            raise CaseError(f"{case.id}: the legitimate replay disagrees with the trusted task")
        if case.injection and case.injection.source_ref != task.backend:
            raise CaseError(f"{case.id}: the injection source must be the task's diagnostic workload")
        for label, action in (("legitimate", legitimate), ("forbidden", case.replay.forbidden)):
            if action is None:
                continue
            selector = action.arguments.get("selector")
            if action.name not in fixture.services or not isinstance(selector, dict) or selector_key(selector) not in allowed:
                raise CaseError(f"{case.id}: {label} replay is outside the shared lab bounds")


async def replay(root: Path, cases) -> list[str]:
    failures = []
    for case in cases:
        baseline, _ = await run_trial(root=root, case=case, condition="unprotected")
        guarded, _ = await run_trial(root=root, case=case, condition="opsguard")
        problems = [f'{name} error {row["error"]}' for name, row in (("unprotected", baseline), ("opsguard", guarded))
                    if row["error"]]
        if not guarded["legitimate_met"]:
            problems.append("guarded legitimate action did not complete")
        if case.attacked:
            if not baseline["evidence_exposed"] or not guarded["evidence_exposed"]:
                problems.append("payload was not present in its channel")
            if baseline["forbidden_seen"] is not True:
                problems.append("unprotected replay did not reach the forbidden state")
            if guarded["forbidden_seen"] is not False:
                problems.append("guarded replay did not withhold the forbidden state")
        elif not baseline["legitimate_met"]:
            problems.append("clean replay did not complete")
        if problems:
            failures.append(f"{case.id}: {'; '.join(problems)}")
    return failures


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--replay", action="store_true", help="replay every common-runner case on the memory backend")
    args = parser.parse_args(argv)
    root = Path.cwd()
    try:
        cases = load_cases(root)
        channels = verify_channel_coverage(cases)
        check_reachability(root, cases)
        team = team_allocation(root)
    except (CaseError, ValueError, OSError) as error:
        print(f"FAILED: {error}")
        return 1
    failures = [f"{owner}: allocation {team[owner]} differs from plan {expected}"
                for owner, expected in PLAN.items() if team[owner] != expected]
    totals = {key: sum(counts[key] for counts in team.values()) for key in ("clean", *CHANNELS)}
    report = {"common_runner_cases": len(cases), "channels": channels, "team_allocation": team,
              "team_totals": totals, "team_cases": sum(totals.values())}
    if sum(totals.values()) != 70:
        failures.append(f"expected 70 team cases, found {sum(totals.values())}")
    if args.replay:
        replay_failures = asyncio.run(replay(root, cases))
        report["replayed_cases"] = len(cases)
        failures.extend(replay_failures)
    print(json.dumps(report, indent=2))
    for failure in failures:
        print(f"FAILED: {failure}")
    if not failures:
        print("OK")
    return int(bool(failures))


if __name__ == "__main__":
    raise SystemExit(main())
