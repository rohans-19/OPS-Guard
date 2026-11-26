"""One command line and one result format for every suite (Stage 2, S2-P5).

    python -m opsguard.bench.unified run --output results/unified
    python -m opsguard.bench.unified run --suite scale --condition opsguard --output results/one-suite
    python -m opsguard.bench.unified parity --output results/parity

Every case is loaded through ``opsguard.bench.convert`` as CaseSpec v2. A per-suite
adapter runs the case with that suite's existing in-memory trial code and returns
one RunResult v2. Conditions a suite does not implement are skipped and listed.

``parity`` runs each old per-suite command line unchanged, then compares its
outcomes with the unified runner's, case by case and condition by condition, and
writes a report that names and explains every difference.

The worst severity tier combines the observer's own samples (the common selector
runner samples at a fixed interval during the agent run) with the case's severity
detectors. The other suites' runners sample after each action; moving them onto
the interval sampler is part of Stage 3's unified fixtures.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from time import monotonic

from opsguard.bench.convert import load_all
from opsguard.bench.oracle import worse
from opsguard.bench.schema import SUITES, CaseSpecV2, RunResultV2

UNIFIED_CONDITIONS = ("unprotected", "hardened", "task-policy", "opsguard")
# Unified condition name -> the suite's own name for it, for the conditions each suite implements.
SUITE_CONDITIONS = {
    "selector": {"unprotected": "unprotected", "hardened": "hardened", "task-policy": "task-policy",
                 "opsguard": "opsguard"},
    "scale": {"unprotected": "unprotected", "hardened": "hardened", "task-policy": "task-policy"},
    "image": {"unprotected": "unprotected", "hardened": "hardened", "task-policy": "task-policy"},
    "restart": {"unprotected": "unprotected", "hardened": "hardened", "task-policy": "task-policy",
                "opsguard": "opsguard"},
    "config": {"unprotected": "unprotected", "hardened": "hardened", "task-policy": "policy_only",
               "opsguard": "opsguard"},
    "pilot": {"unprotected": "unprotected", "task-policy": "task-policy", "opsguard": "opsguard"},
}


def decisions_from_trace(trace) -> list[dict]:
    """Every gateway result for an action request, in order, from an agent trace."""
    out, pending = [], None
    for event in trace or ():
        if event.get("event") == "request":
            pending = event.get("kind")
        elif event.get("event") == "gateway" and pending == "action":
            out.append({"status": str(event.get("status")), "reason": str(event.get("reason"))})
            pending = None
    return out


def tokens_from(audit) -> dict:
    inputs = [entry.get("input_tokens") for entry in audit or () if isinstance(entry, dict)]
    outputs = [entry.get("output_tokens") for entry in audit or () if isinstance(entry, dict)]
    return {"input": sum(i for i in inputs if isinstance(i, int)) if any(isinstance(i, int) for i in inputs) else None,
            "output": sum(o for o in outputs if isinstance(o, int)) if any(isinstance(o, int) for o in outputs) else None}


def worst_tier(case: CaseSpecV2, *, observed: str | None, forbidden: bool | None, collateral: bool | None,
               outage: bool = False) -> str:
    tier = observed or "S0"
    fired = {"forbidden_seen": bool(forbidden), "collateral_seen": bool(collateral), "service_outage": outage}
    for detector in case.severity_detectors:
        if fired.get(detector.signal):
            tier = worse(tier, detector.tier)
    return tier


# ---------------------------------------------------------------------------
# Adapters: run one case under one condition with the suite's own trial code
# ---------------------------------------------------------------------------

class Adapters:
    def __init__(self, root: Path, *, sample_interval: float = 1.0, scratch: Path | None = None):
        self.root, self.sample_interval = root, sample_interval
        self.scratch = scratch or Path(tempfile.mkdtemp(prefix="opsguard-unified-"))
        self._loaded: dict[str, object] = {}

    def _suite(self, name, loader):
        if name not in self._loaded:
            self._loaded[name] = loader()
        return self._loaded[name]

    async def selector(self, case, condition):
        from opsguard.bench.loader import load_cases
        from opsguard.bench.runner import TrialSettings, run_trial
        cases = self._suite("selector", lambda: {c.id: c for c in load_cases(self.root)})
        outcome, trace = await run_trial(root=self.root, case=cases[case.id], condition=condition,
                                         settings=TrialSettings(sample_interval=self.sample_interval))
        return {**outcome, "intent_id": trace.get("intent", {}).get("task_id"),
                "tokens": tokens_from(trace.get("provider_audit"))}

    async def scale(self, case, condition):
        from opsguard.benchmark.run_scale import run_case
        from opsguard.benchmark.scale_cases import load_scale_suite
        workloads, intents, cases = self._suite("scale", lambda: load_scale_suite(self.root))
        source = next(c for c in cases if c.id == case.id)
        row = await run_case(root=self.root, case=source, workloads=workloads, intent=intents[source.base_task_id],
                             condition=condition, timeout=30)
        return {**row, "decisions": decisions_from_trace(row.get("trace")), "intent_id": source.base_task_id}

    async def image(self, case, condition):
        from opsguard.benchmark.image_cases import load_image_suite
        from opsguard.benchmark.run_image import run_case
        workloads, intents, cases = self._suite("image", lambda: load_image_suite(self.root))
        source = next(c for c in cases if c.id == case.id)
        row = await run_case(root=self.root, case=source, workloads=workloads, intent=intents[source.base_task_id],
                             condition=condition, timeout=30)
        return {**row, "decisions": decisions_from_trace(row.get("trace")), "intent_id": source.base_task_id}

    async def restart(self, case, condition):
        from opsguard.benchmark.restart_cases import load_restart_suite
        from opsguard.benchmark.run_restart import run_case
        workloads, intents, cases = self._suite("restart", lambda: load_restart_suite(self.root))
        source = next(c for c in cases if c.id == case.id)
        row = await run_case(root=self.root, workloads=workloads, intent=intents[source.base_task_id], case=source,
                             condition=condition, backend="memory")
        return {**row, "decisions": row.get("decisions") or decisions_from_trace(row.get("trace")),
                "intent_id": source.base_task_id}

    async def config(self, case, condition):
        from opsguard.benchmark.vikas_cases import load_cases
        from opsguard.benchmark.vikas_experiment import SCRIPTED_MODELS, freeze_pilot_settings, run_trial
        from opsguard.benchmark.vikas_replay import ScriptedReplay, scripted_response
        corpus = self._suite("config", lambda: {c["id"]: c for c in load_cases(
            self.root / "benchmark/cases/vikas/configmap_cases.json")})
        settings, _ = self._suite("config-settings", freeze_pilot_settings)
        response = self._suite("config-response", lambda: scripted_response(self.root))
        output = self.scratch / "config"
        output.mkdir(parents=True, exist_ok=True)
        row = await asyncio.to_thread(
            run_trial, root=self.root, backend_factory=lambda c: ScriptedReplay(c, response, output), mode="scripted",
            case=corpus[case.id], model_id=SCRIPTED_MODELS[0], condition=condition, repeat=1, settings=settings)
        decision = [{"status": row["decision"], "reason": row["reason_code"]}] if row.get("decision") else []
        return {"legitimate_met": row.get("legitimate_met"), "forbidden_seen": row.get("forbidden_met"),
                "error": row.get("error"), "decisions": decision, "intent_id": case.id, "model_id": row["model_id"]}

    async def pilot(self, case, condition):
        from opsguard.bench.pilot import decide, load_pilot_cases
        cases = self._suite("pilot", lambda: {c["id"]: c for c in load_pilot_cases(self.root)})
        source = cases[case.id]
        legitimate, forbidden = decide(condition, source, source["legitimate"]), decide(condition, source, source["forbidden"])
        # A decision replay: harm is the forbidden action being allowed (measured on kind in S1-P5).
        return {"legitimate_met": legitimate.status == "ALLOW", "forbidden_seen": forbidden.status == "ALLOW",
                "error": None, "intent_id": case.id, "model_id": "decision-replay",
                "decisions": [{"status": forbidden.status, "reason": forbidden.reason},
                              {"status": legitimate.status, "reason": legitimate.reason}]}


def result_id(case_id: str, condition: str, repeat: int) -> str:
    return hashlib.sha256(f"{case_id}|{condition}|{repeat}".encode()).hexdigest()[:24]


async def run_one(adapters: Adapters, case: CaseSpecV2, condition: str, repeat: int = 1) -> RunResultV2:
    suite_condition = SUITE_CONDITIONS[case.suite][condition]
    started = monotonic()
    try:
        raw = await getattr(adapters, case.suite)(case, suite_condition)
    except Exception as error:  # A harness failure is recorded as an error, never as a prevented attack.
        raw = {"error": f"ADAPTER_ERROR:{type(error).__name__}", "legitimate_met": None, "forbidden_seen": None}
    forbidden = raw.get("forbidden_seen") if case.attacked else None
    error = raw.get("error")
    return RunResultV2.model_validate_json(json.dumps({
        "result_id": result_id(case.id, condition, repeat), "case_id": case.id, "suite": case.suite,
        "goal": case.goal, "channel": case.channel, "condition": condition, "suite_condition": suite_condition,
        "backend": "memory", "model_id": raw.get("model_id") or "scripted", "repeat": repeat,
        "intent_id": raw.get("intent_id"), "legitimate_met": raw.get("legitimate_met"), "forbidden_seen": forbidden,
        "collateral_seen": raw.get("collateral_seen"),
        "worst_severity": None if error else worst_tier(case, observed=raw.get("worst_severity"), forbidden=forbidden,
                                                         collateral=raw.get("collateral_seen")),
        "partial_harm": raw.get("partial_harm") or {},
        "decisions": [{"status": str(d.get("status")), "reason": str(d.get("reason"))}
                      for d in raw.get("decisions") or ()],
        "tokens": raw.get("tokens") or {"input": None, "output": None}, "cost_usd": None,
        "samples": raw.get("samples"), "error": error,
        "elapsed_ms": round((monotonic() - started) * 1000, 3),
    }))


async def run_matrix(root: Path, *, suites=SUITES, conditions=UNIFIED_CONDITIONS, case_ids=None,
                     sample_interval: float = 1.0, on_result=None) -> tuple[list[RunResultV2], list[dict]]:
    cases = [case for case in load_all(root, tuple(suites)) if not case_ids or case.id in case_ids]
    adapters = Adapters(root, sample_interval=sample_interval)
    results, skipped = [], []
    for case in cases:
        for condition in conditions:
            if condition not in SUITE_CONDITIONS[case.suite]:
                skipped.append({"case_id": case.id, "suite": case.suite, "condition": condition,
                                "reason": "not implemented by this suite"})
                continue
            result = await run_one(adapters, case, condition)
            results.append(result)
            if on_result:
                on_result(result)
    return results, skipped


# ---------------------------------------------------------------------------
# Parity with the old per-suite runners
# ---------------------------------------------------------------------------

# Differences the report may explain instead of flagging, keyed by (suite, field). None are needed today.
EXPLAINED: dict[tuple[str, str], str] = {}
# Old-runner conditions outside the four common ones, and why the unified matrix leaves them out.
NOT_IN_MATRIX = {
    ("pilot", "rbac-scoped"): "pilot-only comparison condition (S1-P5); RBAC is modelled per pilot case",
    ("pilot", "opsguard-v1.0"): "pilot-only comparison with the v1.0 decision before the S1-P4 fix",
}


def _old_rows(root: Path, out: Path, python: str) -> dict[str, list[dict]]:
    """Run every old command line unchanged, in memory, and load its rows."""
    out.mkdir(parents=True, exist_ok=True)
    runs = {
        "scale": (["-m", "opsguard.benchmark.run_scale", "--output", str(out / "scale.jsonl")], out / "scale.jsonl"),
        "image": (["-m", "opsguard.benchmark.run_image", "--output", str(out / "image.jsonl")], out / "image.jsonl"),
        "restart": (["-m", "opsguard.benchmark.run_restart", "--output", str(out / "restart.jsonl"),
                     *[arg for c in ("unprotected", "hardened", "task-policy", "opsguard") for arg in ("--condition", c)]],
                    out / "restart.jsonl"),
        "config": (["-m", "opsguard.benchmark.vikas_run", "--output", str(out / "config"),
                    *[arg for c in ("unprotected", "hardened", "policy_only", "opsguard") for arg in ("--condition", c)]],
                   out / "config" / "results.jsonl"),
        "selector": (["-m", "opsguard.bench.run", "--backend", "memory", "--output", str(out / "selector"),
                      *[arg for c in ("unprotected", "hardened", "task-policy", "opsguard") for arg in ("--condition", c)]],
                     out / "selector" / "results.jsonl"),
        "pilot": (["-m", "opsguard.bench.pilot", "--backend", "memory", "--output", str(out / "pilot")],
                  out / "pilot" / "results.jsonl"),
    }
    rows = {}
    for suite, (args, path) in runs.items():
        done = subprocess.run([python, *args], cwd=root, capture_output=True, text=True, timeout=1800)
        if not path.exists():
            raise RuntimeError(f"old {suite} runner wrote no results: {done.stderr.strip()[-400:]}")
        rows[suite] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return rows


def _old_outcomes(suite: str, rows: list[dict]) -> dict[tuple[str, str], dict]:
    """(case, suite condition) -> legitimate/forbidden outcome, from an old runner's own rows."""
    out = {}
    if suite == "pilot":
        by = {}
        for row in rows:
            by.setdefault((row["case_id"], row["condition"]), {})[row["action"]] = row["decision"]
        for key, actions in by.items():
            out[key] = {"legitimate_met": actions.get("legitimate") == "ALLOW",
                        "forbidden_seen": actions.get("forbidden") == "ALLOW", "error": None}
        return out
    for row in rows:
        if suite == "config":
            from opsguard.benchmark.vikas_experiment import SCRIPTED_MODELS
            if row.get("kind") != "trial" or row.get("model_id") != SCRIPTED_MODELS[0]:
                continue
            outcome = {"legitimate_met": row.get("legitimate_met"), "forbidden_seen": row.get("forbidden_met"),
                       "error": row.get("error")}
        else:
            outcome = {key: row.get(key) for key in ("legitimate_met", "forbidden_seen", "error")}
        out[(row["case_id"], row["condition"])] = outcome
    return out


def parity(root: Path, output: Path, python: str = sys.executable) -> dict:
    old = _old_rows(root, output / "old", python)
    results, skipped = asyncio.run(run_matrix(root))
    unified = {(r.case_id, r.suite_condition): r for r in results}
    report = {"cases": len({r.case_id for r in results}), "compared": 0, "differences": [], "missing": [],
              "skipped_conditions": skipped}
    for suite, rows in old.items():
        for key, before in _old_outcomes(suite, rows).items():
            after = unified.get(key)
            if after is None:
                report["missing"].append({"suite": suite, "case_id": key[0], "condition": key[1],
                                          "explanation": NOT_IN_MATRIX.get((suite, key[1]), "UNEXPLAINED")})
                continue
            report["compared"] += 1
            for field in ("legitimate_met", "forbidden_seen", "error"):
                old_value = before.get(field)
                new_value = getattr(after, field)
                if field == "forbidden_seen" and after.channel is None:
                    old_value = None  # Clean cases record no forbidden outcome.
                if old_value != new_value:
                    report["differences"].append({
                        "suite": suite, "case_id": key[0], "condition": key[1], "field": field,
                        "old": old_value, "unified": new_value,
                        "explanation": EXPLAINED.get((suite, field), "UNEXPLAINED")})
    report["unexplained"] = sum(d["explanation"] == "UNEXPLAINED" for d in report["differences"] + report["missing"])
    output.mkdir(parents=True, exist_ok=True)
    (output / "parity.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    lines = ["# Unified runner parity", "",
             f"Cases: {report['cases']}. Old-runner cells compared: {report['compared']}. "
             f"Differences: {len(report['differences'])} ({report['unexplained']} unexplained). "
             f"Old cells with no unified row: {len(report['missing'])}.", ""]
    if report["differences"]:
        lines += ["| Suite | Case | Condition | Field | Old | Unified | Explanation |", "| --- | --- | --- | --- | --- | --- | --- |"]
        lines += [f"| {d['suite']} | {d['case_id']} | {d['condition']} | {d['field']} | {d['old']} | {d['unified']} | "
                  f"{d['explanation']} |" for d in report["differences"]]
    if report["missing"]:
        lines += ["", "Old cells the unified matrix does not run:", ""]
        lines += [f"- {m['suite']} {m['case_id']} {m['condition']}: {m['explanation']}" for m in report["missing"]]
    lines += ["", f"Conditions skipped because a suite does not implement them: {len(skipped)}."]
    (output / "PARITY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run cases through the unified runner")
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--suite", action="append", choices=SUITES)
    run.add_argument("--condition", action="append", choices=UNIFIED_CONDITIONS)
    run.add_argument("--case", action="append")
    run.add_argument("--sample-interval", type=float, default=1.0)
    check = sub.add_parser("parity", help="compare with the old per-suite runners")
    check.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    root = Path.cwd()
    if args.command == "parity":
        report = parity(root, args.output)
        print(json.dumps({key: report[key] if not isinstance(report[key], list) else len(report[key])
                          for key in ("cases", "compared", "differences", "unexplained", "missing")}))
        return int(report["unexplained"] > 0)
    args.output.mkdir(parents=True, exist_ok=False)
    stream = (args.output / "results.jsonl").open("x", encoding="utf-8")

    def save(result):
        stream.write(result.model_dump_json() + "\n")
        stream.flush()
        print(f"{result.suite} {result.case_id} {result.condition}: legitimate={result.legitimate_met} "
              f"forbidden={result.forbidden_seen} tier={result.worst_severity} error={result.error}")

    with stream:
        results, skipped = asyncio.run(run_matrix(root, suites=tuple(args.suite or SUITES),
                                                  conditions=tuple(args.condition or UNIFIED_CONDITIONS),
                                                  case_ids=set(args.case or ()), sample_interval=args.sample_interval,
                                                  on_result=save))
    (args.output / "skipped.json").write_text(json.dumps(skipped, indent=2), encoding="utf-8")
    errors = sum(r.error is not None for r in results)
    print(json.dumps({"cases": len({r.case_id for r in results}), "results": len(results), "errors": errors,
                      "skipped": len(skipped), "output": str(args.output)}))
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
