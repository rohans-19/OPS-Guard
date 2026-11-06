"""Reproduce Vikas's ConfigMap experiment and impact report."""

import argparse
import hashlib
import json
import os
from itertools import product
from pathlib import Path
from shutil import which
from subprocess import SubprocessError, run

from opsguard.benchmark.judge import JUDGE_SYSTEM_PROMPT, openai_completion
from opsguard.benchmark.vikas_cases import load_cases
from opsguard.benchmark.vikas_experiment import (
    ABLATIONS, HARDENING, PRIMARY, PROPOSAL_PROMPT, SCRIPTED_MODELS, THRESHOLDS,
    freeze_pilot_settings, run_impact_replay, run_trial,
)
from opsguard.benchmark.vikas_kind import KindReplay
from opsguard.benchmark.vikas_replay import ScriptedReplay, scripted_response
from opsguard.benchmark.vikas_report import render_report


CASE_PATH = Path("benchmark/cases/vikas/configmap_cases.json")
FIXTURE_PATH = Path("benchmark/fixtures/vikas")


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _row_key(row):
    return row["kind"], row["case_id"], row["model_id"], row["condition"], row["repeat"]


def _read_results(path):
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as stream:
        return [json.loads(line) for line in stream if line.strip()]


def _write_report(output, records):
    report = output / "report.md"
    report.write_text(render_report(records), encoding="utf-8")
    return report


def _run_manifest(root, args, cases, models, conditions, pilot):
    files = [CASE_PATH, *(FIXTURE_PATH / name for name in
                          ("fixture.yaml", "kustomization.yaml", "app.py", "impact_topology.json"))]
    package = Path(__file__).resolve().parents[1]
    modules = [
        "benchmark/judge.py", "benchmark/vikas_cases.py", "benchmark/vikas_experiment.py",
        "benchmark/vikas_kind.py", "benchmark/vikas_replay.py", "benchmark/vikas_run.py",
        "evidence/configmap.py", "impact/assessment.py", "impact/decision.py", "impact/effects.py",
        "impact/graph.py", "operations/set_config.py",
    ]
    return {
        "schema_version": 1,
        "mode": args.mode,
        "cluster": args.cluster,
        "models": list(models),
        "judge_model": args.judge_model if args.mode == "live" else "scripted-policy-comparison",
        "case_ids": [case["id"] for case in cases],
        "conditions": list(conditions),
        "repeats": args.repeats,
        "pilot": pilot,
        "frozen_risk_preset": args.risk_preset or pilot["selected"],
        "model_seed": None,
        "prompts": {name: _sha256(prompt.encode("utf-8")) for name, prompt in {
            "proposal": PROPOSAL_PROMPT, "hardened_proposal": PROPOSAL_PROMPT + HARDENING,
            "judge": JUDGE_SYSTEM_PROMPT,
        }.items()},
        "fixtures": {path.as_posix(): _sha256((root / path).read_bytes()) for path in files},
        "implementation": {path: _sha256((package / path).read_bytes()) for path in modules},
        "evidence_delivery": {
            "logs": "synthetic_channel", "alerts": "synthetic_channel", "runbooks": "synthetic_channel",
            "configmaps": "configmap" if args.mode == "live" else "synthetic_channel",
        },
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="directory for manifest, results and report")
    parser.add_argument("--mode", choices=("scripted", "live"), default="scripted")
    parser.add_argument("--cluster", help="existing kind cluster name for live mode")
    parser.add_argument("--model", action="append", help="exact provider model ID, repeat for both proposal models")
    parser.add_argument("--judge-model", help="exact model ID for the separate judge")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--case", action="append", help="restrict to a case ID, repeat to select more")
    parser.add_argument("--condition", action="append", choices=PRIMARY + ABLATIONS,
                        help="restrict conditions, repeat to select more")
    parser.add_argument("--impact-only", action="store_true", help="run controlled impact replays without models")
    parser.add_argument("--report-only", action="store_true", help="regenerate the report from existing results")
    parser.add_argument("--risk-preset", choices=tuple(THRESHOLDS), help="freeze an explicit preset for the run")
    args = parser.parse_args(argv)

    if args.report_only:
        output = args.output.resolve()
        records = _read_results(output / "results.jsonl")
        if not records:
            parser.error("--report-only requires existing results.jsonl records")
        print(f"Regenerated {_write_report(output, records)}")
        return 0

    if args.repeats < 1:
        parser.error("--repeats must be positive")
    conditions = () if args.impact_only else tuple(dict.fromkeys(args.condition or PRIMARY + ABLATIONS))
    if args.impact_only and args.condition:
        parser.error("--condition applies to model trials")
    models = () if args.impact_only else SCRIPTED_MODELS
    if args.mode == "live":
        if not args.cluster:
            parser.error("--cluster is required for live mode")
        kind = which("kind")
        if not kind or not which("kubectl"):
            parser.error("kind and kubectl are required for live mode")
        try:
            clusters = run([kind, "get", "clusters"], check=True, capture_output=True, text=True, timeout=30)
        except (OSError, SubprocessError):
            parser.error("could not list kind clusters")
        if args.cluster not in clusters.stdout.splitlines():
            parser.error("the named kind cluster is unavailable")
        if not args.impact_only:
            models = tuple(dict.fromkeys(args.model or ()))
            if not models or ("llm_judge" in conditions and not args.judge_model):
                parser.error("provide --model and, for the judge condition, --judge-model")
            if not os.environ.get("OPENAI_API_KEY"):
                parser.error("set OPENAI_API_KEY locally to run live model trials")
    elif args.cluster or args.model or args.judge_model:
        parser.error("--cluster, --model and --judge-model apply to live mode")

    root = Path.cwd()
    corpus = load_cases(root / CASE_PATH)
    selected = set(args.case or ())
    if selected - {case["id"] for case in corpus}:
        parser.error("unknown --case ID")
    cases = [case for case in corpus if not selected or case["id"] in selected]
    impact_cases = [case for case in cases if "forbidden" in case]
    if args.impact_only and not impact_cases:
        parser.error("controlled impact replays require at least one attacked case")
    probes = sorted({
        (probe["service"], probe["path"])
        for case in corpus for label in ("legitimate", "forbidden") if label in case
        for probe in case[label]["expected"]["probes"]
    })

    settings, pilot = freeze_pilot_settings()
    if args.risk_preset:
        settings = THRESHOLDS[args.risk_preset]
    manifest = _run_manifest(root, args, cases, models, conditions, pilot)
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    manifest_path, results_path = output / "manifest.json", output / "results.jsonl"
    if manifest_path.exists():
        if json.loads(manifest_path.read_text(encoding="utf-8")) != manifest:
            parser.error("output manifest differs from this run, choose a new --output directory")
    elif results_path.exists():
        parser.error("output already contains results with no matching manifest")
    else:
        with manifest_path.open("x", encoding="utf-8") as stream:
            stream.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")

    planned = list(product(cases, models, conditions, range(1, args.repeats + 1)))
    expected_keys = {
        ("trial", case["id"], model, condition, repeat) for case, model, condition, repeat in planned
    } | {("impact_replay", case["id"], "controlled-replay", "controlled", 1) for case in impact_cases}
    records = _read_results(results_path)
    completed = {_row_key(row) for row in records}
    if len(completed) != len(records) or completed - expected_keys or any(row["mode"] != args.mode for row in records):
        parser.error("stored results do not match the run manifest")

    if args.mode == "live":
        def backend_factory(case):
            return KindReplay(root, args.cluster, case)

        completions = {model: openai_completion(model) for model in models}
        judge = openai_completion(args.judge_model) if "llm_judge" in conditions else None
    else:
        response = scripted_response(root)

        def backend_factory(case):
            return ScriptedReplay(case, response, output)

        completions, judge = {}, None

    with results_path.open("a", encoding="utf-8") as stream:
        def save(record):
            stream.write(json.dumps(record, sort_keys=True, allow_nan=False) + "\n")
            stream.flush()
            records.append(record)

        for case, model, condition, repeat in planned:
            if ("trial", case["id"], model, condition, repeat) in completed:
                continue
            save(run_trial(
                root=root, backend_factory=backend_factory, mode=args.mode, case=case, model_id=model,
                condition=condition, repeat=repeat, settings=settings, complete=completions.get(model),
                judge_complete=judge,
            ))
        for case in impact_cases:
            if ("impact_replay", case["id"], "controlled-replay", "controlled", 1) in completed:
                continue
            save(run_impact_replay(
                root=root, backend_factory=backend_factory, mode=args.mode, case=case, probes=probes, settings=settings,
            ))

    _write_report(output, records)
    errors = sum(row["error"] is not None for row in records)
    print(f"{args.mode}: wrote {len(records)} records with {errors} harness errors to {output}")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
