"""Run the common benchmark matrix, or one case, with resumable result IDs.

One case, one condition:

    python -m opsguard.bench.run --case selector-checkout-clean-01 \
        --condition opsguard --output results/one-case

The output directory holds manifest.json (frozen configuration), results.jsonl
(one RunResult per attempt) and traces/ (one synthetic trace per attempt).
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
from importlib.metadata import PackageNotFoundError, version
from itertools import product
import json
from pathlib import Path
import platform
import random
import subprocess

from opsguard.agent.loop import BASE_PROMPT, HARDENING
from opsguard.bench.channels import verify_channel_coverage
from opsguard.bench.loader import CASE_ROOT, FIXTURE_ROOT, CaseError, file_sha256, load_cases, select
from opsguard.bench.runner import TrialSettings, run_trial
from opsguard.bench.schema import CONDITIONS, PRIMARY_CONDITIONS, SCHEMA_VERSION, RunResult


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def trial_id(experiment_sha256: str, case_id: str, model_id: str, condition: str, repeat: int) -> str:
    """Deterministic identity of one matrix cell. It does not depend on which cells a run selects."""
    return digest([experiment_sha256, case_id, model_id, condition, repeat])[:24]


def run_id(trial: str, attempt: int) -> str:
    return trial if attempt == 1 else digest([trial, attempt])[:24]


def planned_ids(manifest: dict) -> dict[str, tuple]:
    selection = manifest["selection"]
    return {trial_id(manifest["experiment_sha256"], *cell): cell for cell in product(
        selection["cases"], selection["models"], selection["conditions"], selection["repeats"])}


def validate_results(rows: list[dict], manifest: dict) -> dict[str, dict]:
    """Accept rows only for this configuration, model, condition and repeat. Return the last attempt per cell."""
    if digest(manifest["experiment"]) != manifest["experiment_sha256"]:
        raise ValueError("manifest experiment digest mismatch")
    planned = planned_ids(manifest)
    if len(planned) != manifest["planned_trials"]:
        raise ValueError("manifest matrix size mismatch")
    seen, latest = set(), {}
    for raw in rows:
        row = RunResult.model_validate_json(json.dumps(raw)).model_dump(mode="json")
        cell = planned.get(row["trial_id"])
        prior = latest.get(row["trial_id"])
        if (row["experiment_sha256"] != manifest["experiment_sha256"]
                or cell != (row["case_id"], row["model_id"], row["condition"], row["repeat"])
                or row["backend"] != manifest["experiment"]["backend"]
                or row["provider"] != manifest["experiment"]["provider"]
                or row["run_id"] in seen or row["run_id"] != run_id(row["trial_id"], row["attempt"])
                or row["attempt"] != (prior["attempt"] + 1 if prior else 1)
                or (prior is not None and prior["error"] is None)):
            raise ValueError(f'result {row["run_id"]} does not match the frozen matrix or attempt order')
        seen.add(row["run_id"])
        latest[row["trial_id"]] = row
    return latest


def read_results(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _git_commit(root: Path) -> str | None:
    try:
        output = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return output.stdout.strip() or None if output.returncode == 0 else None


def _packages(names) -> dict:
    found = {}
    for name in names:
        try:
            found[name] = version(name)
        except PackageNotFoundError:
            found[name] = None
    return found


def build_manifest(root: Path, args, cases, models, conditions, repeats) -> dict:
    fixtures = sorted({case.fixture for case in cases})
    fixture_files = sorted({path for name in fixtures
                            for path in (root / FIXTURE_ROOT / name).parent.rglob("*") if path.is_file()})
    case_files = sorted({path for owner in sorted({case.owner for case in cases})
                         for path in (root / CASE_ROOT / owner).glob("cases*.json")})
    experiment = {
        "schema_version": SCHEMA_VERSION, "backend": args.backend, "provider": args.provider,
        "judge_model": args.judge_model, "seed": args.seed, "image": args.image, "cluster": args.kind_cluster,
        "alertmanager_port": args.alertmanager_port, "max_steps": args.max_steps,
        "settle_seconds": args.settle_seconds, "order": "seeded-shuffle",
        "prompt_sha256": {"base": hashlib.sha256(BASE_PROMPT.encode()).hexdigest(),
                          "hardening": hashlib.sha256(HARDENING.encode()).hexdigest()},
        "case_sha256": {path.relative_to(root).as_posix(): file_sha256(path) for path in case_files},
        "fixture_sha256": {path.relative_to(root).as_posix(): file_sha256(path) for path in fixture_files},
    }
    if args.environment_file:
        experiment["environment"] = json.loads(args.environment_file.read_text(encoding="utf-8"))
    selection = {"cases": [case.id for case in cases], "models": models, "conditions": list(conditions),
                 "repeats": repeats}
    source = {path.relative_to(root).as_posix(): file_sha256(path)
              for path in [*sorted((root / "src/opsguard").rglob("*.py")), root / "pyproject.toml"]}
    return {
        "schema_version": SCHEMA_VERSION, "experiment_sha256": digest(experiment), "experiment": experiment,
        "selection": selection, "planned_trials": len(cases) * len(models) * len(conditions) * len(repeats),
        "created_at": datetime.now(timezone.utc).isoformat(), "git_commit": _git_commit(root),
        "source_sha256": source,
        "runtime": {"python": platform.python_version(), "system": platform.system(), "machine": platform.machine(),
                    "packages": _packages(("httpx", "pydantic", "networkx", *(("groq",) if args.provider == "groq" else ())))},
    }


async def _main(args) -> int:
    root = Path.cwd()
    try:
        cases = load_cases(root)
        verify_channel_coverage(cases)
        cases = select(cases, args.case)
    except CaseError as error:
        raise SystemExit(f"case validation failed: {error}")
    conditions = tuple(dict.fromkeys(args.condition or PRIMARY_CONDITIONS))
    models = list(dict.fromkeys(args.model or [])) if args.provider != "scripted" else ["scripted"]
    if args.provider != "scripted" and not models:
        raise SystemExit("a real provider requires --model")
    if args.provider == "scripted" and args.model:
        raise SystemExit("--model applies to a real provider")
    if "llm-judge" in conditions and args.provider != "scripted" and not args.judge_model:
        raise SystemExit("llm-judge with a real provider requires --judge-model")
    repeats = sorted(set(args.repeat)) if args.repeat else list(range(1, args.repeats + 1))
    try:
        settings = TrialSettings(backend=args.backend, provider=args.provider, judge_model=args.judge_model,
                                 cluster=args.kind_cluster, image=args.image, env_file=args.env_file,
                                 alert_port=args.alertmanager_port, seed=args.seed, max_steps=args.max_steps,
                                 settle_seconds=args.settle_seconds)
    except ValueError as error:
        raise SystemExit(str(error))
    if args.provider == "groq":
        from opsguard.agent.groq_model import groq_key
        groq_key(args.env_file)

    manifest = build_manifest(root, args, cases, models, conditions, repeats)
    output = args.output
    manifest_path, results_path, traces = output / "manifest.json", output / "results.jsonl", output / "traces"
    latest: dict[str, dict] = {}
    if args.resume:
        if not manifest_path.is_file():
            raise SystemExit("nothing to resume: manifest.json is missing")
        prior = json.loads(manifest_path.read_text(encoding="utf-8"))
        if prior["experiment_sha256"] != manifest["experiment_sha256"] or prior["selection"] != manifest["selection"]:
            raise SystemExit("resume configuration differs from the frozen manifest")
        manifest = prior
        if results_path.exists():
            latest = validate_results(read_results(results_path), manifest)
    else:
        if manifest_path.exists() or results_path.exists():
            raise SystemExit("choose a new output directory or use --resume")
        traces.mkdir(parents=True, exist_ok=True)
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    traces.mkdir(parents=True, exist_ok=True)

    by_id = {case.id: case for case in cases}
    trials = list(product(manifest["selection"]["cases"], models, conditions, repeats))
    random.Random(args.seed).shuffle(trials)
    with results_path.open("a", encoding="utf-8") as stream:
        for case_id, model, condition, repeat in trials:
            identity = trial_id(manifest["experiment_sha256"], case_id, model, condition, repeat)
            previous = latest.get(identity)
            if previous and (previous["error"] is None or not args.retry_errors):
                continue
            attempt = previous["attempt"] + 1 if previous else 1
            this_run = run_id(identity, attempt)
            started_at = datetime.now(timezone.utc).isoformat()
            outcome, trace = await run_trial(root=root, case=by_id[case_id], condition=condition,
                                             model_name=None if model == "scripted" else model,
                                             repeat=repeat, settings=settings)
            trace_ref = f"traces/{this_run}.json"
            (output / trace_ref).write_text(json.dumps(trace, indent=1, allow_nan=False), encoding="utf-8")
            row = RunResult(**outcome, run_id=this_run, trial_id=identity, attempt=attempt,
                            experiment_sha256=manifest["experiment_sha256"], trace_ref=trace_ref,
                            started_at=started_at).model_dump(mode="json")
            stream.write(json.dumps(row, allow_nan=False) + "\n")
            stream.flush()
            latest[identity] = row
            print(f'{case_id} {model} {condition} repeat={repeat}: legitimate={row["legitimate_met"]} '
                  f'forbidden={row["forbidden_seen"]} error={row["error"]}', flush=True)
            if row["error"] is not None and args.stop_on_error:
                break
            if args.delay:
                await asyncio.sleep(args.delay)
    errors = sum(row["error"] is not None for row in latest.values())
    missing = manifest["planned_trials"] - len(latest)
    print(json.dumps({"planned": manifest["planned_trials"], "recorded": len(latest), "missing": missing,
                      "errors": errors, "output": output.as_posix()}))
    return int(bool(errors or missing))


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, required=True, help="new directory for manifest, results and traces")
    parser.add_argument("--case", action="append", help="case ID filter, repeatable")
    parser.add_argument("--condition", "--defence", dest="condition", action="append", choices=CONDITIONS,
                        help="defence condition filter, repeatable (default: the four primary conditions)")
    parser.add_argument("--model", action="append", help="exact provider model ID, repeatable")
    parser.add_argument("--repeats", type=int, default=1, help="run repeats 1..N")
    parser.add_argument("--repeat", type=int, action="append", help="run only this repeat index, repeatable")
    parser.add_argument("--backend", choices=("memory", "kind"), default="memory")
    parser.add_argument("--kind-cluster")
    parser.add_argument("--image", help="approved Python 3 fixture image loaded into the kind cluster")
    parser.add_argument("--provider", choices=("scripted", "ollama", "groq"), default="scripted")
    parser.add_argument("--judge-model")
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--max-steps", type=int, default=8)
    parser.add_argument("--settle-seconds", type=float, default=20.0)
    parser.add_argument("--delay", type=float, default=0)
    parser.add_argument("--alertmanager-port", type=int, default=9093)
    parser.add_argument("--resume", action="store_true", help="continue a stopped matrix with identical settings")
    parser.add_argument("--retry-errors", action="store_true",
                        help="with --resume, append a new attempt for failed cells and keep every earlier attempt")
    parser.add_argument("--stop-on-error", action="store_true")
    parser.add_argument("--environment-file", type=Path,
                        help="JSON of non-secret lab versions and image digests to freeze in the manifest")
    args = parser.parse_args(argv)
    if args.retry_errors and not args.resume:
        parser.error("--retry-errors requires --resume")
    if args.repeats < 1 or args.delay < 0 or any(index < 1 for index in args.repeat or ()):
        parser.error("repeats must be positive and delay nonnegative")
    return asyncio.run(_main(args))


if __name__ == "__main__":
    raise SystemExit(main())
