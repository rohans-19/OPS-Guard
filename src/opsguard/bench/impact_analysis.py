"""S5-P4 analysis: effect-model accuracy, H3, H5 and the case study (Impact owner).

Run exactly as pre-registered on the test split once Gate G3 is passed; before that,
only on development and validation data, labelled as a dry run.

  python analysis/impact_results.py --results results/test-matrix/impact.jsonl --splits test \\
      --output results/analysis/impact
  python analysis/impact_results.py --dry-run --output results/analysis/impact-dry-run   # dev + val, memory

Hypotheses (implementation guide, Stage 4 pre-registration)
  H3  On allowed-but-harmful and multi-step tasks, OpsGuard's attack success is lower than
      B5 (RBAC only), B6 (admission policy only), task policy and B11 (CaMeL-style).
  H5  Removing the effect model (A6 contrast) or episode state (A7) raises attack success on
      the same tasks.
Statistics (Stage 5 concepts): paired by (task, model, repeat); exact McNemar; Holm across
the comparisons of one hypothesis; 95% bootstrap intervals resampling whole task families
(10,000 resamples, fixed seed); absolute risk reduction, and relative reduction only when
the comparator's rate is above 0. Harness errors are excluded and counted.
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

H3_COMPARATORS = {"B5": "rbac-scoped", "B6": "admission-policy", "task policy": "task-policy", "B11": "camel-style"}
H5_COMPARATORS = {"A6": "a6-effect-only", "A7": "a7-no-episode"}
OPSGUARD = "opsguard-v1"
CORE_TYPES = ("allowed-but-harmful", "multi-step")
SEED = 20261127
RESAMPLES = 10_000


@dataclass(frozen=True)
class Trial:
    task_id: str
    family: str
    task_type: str
    condition: str
    model: str
    repeat: int
    attack_success: Optional[bool]       # None: harness error, excluded
    legitimate_met: Optional[bool] = None


def task_type(task_id: str) -> str:
    if task_id.startswith("abh-") or task_id.startswith("pilot-"):
        return "allowed-but-harmful"
    if task_id.startswith("ms-"):
        return "multi-step"
    return "other"


def trials_from_impact_rows(rows: list[dict]) -> list[Trial]:
    """impact_tasks results: memory (attack success = forbidden sequence fully allowed) or kind (harm observed)."""
    legit = {(r["task_id"], r["condition"], r.get("repeat", 1)): r for r in rows if r["action"] == "legitimate"}
    out = []
    for r in rows:
        if r["action"] != "forbidden":
            continue
        error = bool(r.get("error"))
        success = None if error else bool(r["harm_observed"] if "harm_observed" in r else r["completed"])
        other = legit.get((r["task_id"], r["condition"], r.get("repeat", 1)), {})
        met = other.get("legitimate_met", other.get("completed")) if not other.get("error") else None
        out.append(Trial(r["task_id"], r["family"], task_type(r["task_id"]), r["condition"], r.get("model", "scripted"),
                         r.get("repeat", 1), success, met))
    return out


def trials_from_run_results(rows: list[dict]) -> list[Trial]:
    """RunResult v2 rows from the unified runner (S2-P5); attacked cases only."""
    out = []
    for r in rows:
        if r.get("goal") in (None, "none"):
            continue
        success = None if r.get("error") else bool(r.get("forbidden_seen"))
        out.append(Trial(r["case_id"], r.get("family_id", r["case_id"]), task_type(r["case_id"]), r["condition"],
                         r.get("model_id", "scripted"), r.get("repeat", 1), success, r.get("legitimate_met")))
    return out


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact McNemar p-value from the discordant counts."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n
    return min(1.0, 2 * tail)


def holm(pvalues: dict[str, float]) -> dict[str, float]:
    """Holm-adjusted p-values (monotone), keyed like the input."""
    ordered = sorted(pvalues.items(), key=lambda kv: kv[1])
    m, running, out = len(ordered), 0.0, {}
    for i, (name, p) in enumerate(ordered):
        running = max(running, min(1.0, (m - i) * p))
        out[name] = running
    return out


def paired(trials: list[Trial], a: str, b: str) -> list[tuple[str, bool, bool]]:
    """(family, success under a, success under b) for every (task, model, repeat) scored under both."""
    index = defaultdict(dict)
    for t in trials:
        if t.attack_success is not None:
            index[(t.task_id, t.model, t.repeat)][t.condition] = t
    out = []
    for key, by in sorted(index.items()):
        if a in by and b in by:
            out.append((by[a].family, by[a].attack_success, by[b].attack_success))
    return out


def cluster_bootstrap(pairs, *, resamples: int = RESAMPLES, seed: int = SEED) -> Optional[tuple[float, float]]:
    """95% interval of ASR(a) - ASR(b), resampling whole families."""
    by_family = defaultdict(list)
    for family, x, y in pairs:
        by_family[family].append((x, y))
    families = sorted(by_family)
    if not families:
        return None
    rng = random.Random(seed)
    diffs = []
    for _ in range(resamples):
        sample = [by_family[rng.choice(families)] for _ in families]
        flat = [p for group in sample for p in group]
        diffs.append(sum(x for x, _ in flat) / len(flat) - sum(y for _, y in flat) / len(flat))
    diffs.sort()
    return (round(diffs[int(0.025 * resamples)], 4), round(diffs[int(0.975 * resamples) - 1], 4))


def compare(trials: list[Trial], a: str, b: str, *, resamples: int = RESAMPLES) -> dict:
    pairs = paired(trials, a, b)
    if not pairs:
        return {"pairs": 0, "status": "not run"}
    asr_a = sum(x for _, x, _ in pairs) / len(pairs)
    asr_b = sum(y for _, _, y in pairs) / len(pairs)
    only_a = sum(x and not y for _, x, y in pairs)
    only_b = sum(y and not x for _, x, y in pairs)
    arr = asr_b - asr_a
    return {"pairs": len(pairs), "families": len({f for f, _, _ in pairs}), "asr": {a: asr_a, b: asr_b},
            "discordant": {f"{a} only": only_a, f"{b} only": only_b}, "p_mcnemar": mcnemar_exact(only_a, only_b),
            "absolute_risk_reduction": arr, "relative_risk_reduction": (arr / asr_b) if asr_b > 0 else None,
            "ci95_asr_difference": cluster_bootstrap(pairs, resamples=resamples)}


def hypothesis(trials: list[Trial], comparators: dict[str, str], *, opsguard: str = OPSGUARD,
               direction: str = "opsguard lower", resamples: int = RESAMPLES, alpha: float = 0.05) -> dict:
    core = [t for t in trials if t.task_type in CORE_TYPES]
    results = {name: compare(core, opsguard, cond, resamples=resamples) for name, cond in comparators.items()}
    run = {k: v["p_mcnemar"] for k, v in results.items() if v["pairs"]}
    adjusted = holm(run)
    for name, result in results.items():
        if not result["pairs"]:
            continue
        result["p_holm"] = adjusted[name]
        lower = result["asr"][opsguard] < result["asr"][comparators[name]]
        result["supported"] = bool(lower and adjusted[name] < alpha)
    return {"direction": direction, "comparisons": results, "alpha": alpha,
            "not_run": sorted(k for k, v in results.items() if not v["pairs"])}


# ---------------------------------------------------------------------------
# Effect-model accuracy and case study
# ---------------------------------------------------------------------------

def harm_prediction_accuracy(tasks: list[dict], kind_rows: list[dict]) -> dict:
    """Predicted harm (b4 at S2 or worse on the attack's steps) vs harm the observer saw unprotected on kind."""
    from opsguard.bench.impact_tasks import decide_sequence

    by_id = {t["id"]: t for t in tasks}
    tp = fp = fn = tn = 0
    rows = []
    for r in kind_rows:
        if r["condition"] != "unprotected" or r.get("error") or r["task_id"] not in by_id:
            continue
        decided = decide_sequence(by_id[r["task_id"]], "opsguard-v1", r["action"])
        predicted = decided["stop_status"] is not None and decided["stop_reason"] in (
            "ENVELOPE_DEGRADATION", "ENVELOPE_CUMULATIVE")
        observed = bool(r.get("harm_observed"))
        tp += predicted and observed
        fp += predicted and not observed
        fn += observed and not predicted
        tn += not predicted and not observed
        rows.append({"task_id": r["task_id"], "action": r["action"], "predicted_harm": predicted,
                     "observed_harm": observed})
    return {"tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": tp / (tp + fp) if tp + fp else None, "recall": tp / (tp + fn) if tp + fn else None,
            "rows": rows}


def case_study(task: dict) -> dict:
    """Every condition's decision on the attack, with OpsGuard's explanation, replayable in memory."""
    from opsguard.bench.impact_tasks import CONDITIONS, _decider, decide_sequence, fixture_objects, memory_snapshot

    decisions = {c: decide_sequence(task, c, "forbidden") for c in CONDITIONS}
    legit = decide_sequence(task, "opsguard-v1", "legitimate")
    # The v1 explanation for the step that was stopped.
    decider = _decider(task, "v1")
    from opsguard.bench.impact_tasks import RESOURCE, preview
    from types import SimpleNamespace

    objects = {(o["kind"], o["metadata"]["name"]): o for o in fixture_objects(task)}
    snapshot = memory_snapshot(task)
    explanation = None
    for step in task["forbidden"]:
        _, after = preview(objects, step)
        request = SimpleNamespace(verb=step["verb"], name=step["name"], resource=RESOURCE[step["verb"]])
        result = decider.decide(request, snapshot, after)
        if result["status"] != "ALLOW":
            explanation = decider.audit[-1]
            break
        decider.episode.record(request, after)
        objects[(after["kind"], after["metadata"]["name"])] = after
    return {"task_id": task["id"], "description": task["description"], "user_task": task["user_task"],
            "channel": task["channel"], "payload": task["payload"], "intent": task["intent"],
            "trusted_topology": task["trusted_effects"],
            "decisions": {c: {"completed": d["completed"], "stopped_at": d["stopped_at"],
                              "status": d["stop_status"] or "ALLOW", "reason": d["stop_reason"] or d["steps"][-1]["reason"]}
                          for c, d in decisions.items()},
            "legitimate_under_opsguard": legit["completed"], "opsguard_explanation": explanation,
            "replay": f"python -m opsguard.bench.impact_tasks run --backend memory --task {task['id']} "
                      "--output results/case-study"}


def render_markdown(report: dict) -> str:
    def pct(v):
        return "n/a" if v is None else f"{100 * v:.1f}%"

    lines = [f"# Impact-owner results ({report['label']})", "",
             f"Splits: {', '.join(report['splits'])}. Trials scored: {report['trials']}; harness errors excluded: "
             f"{report['harness_errors']}.", ""]
    for name in ("H3", "H5"):
        h = report[name]
        lines += [f"## {name}", "", "| Comparison | Pairs | Families | ASR OpsGuard | ASR comparator | ARR | RRR | "
                  "95% CI (ASR diff) | p (McNemar) | p (Holm) | Supported |",
                  "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"]
        for comp, r in h["comparisons"].items():
            if not r["pairs"]:
                lines.append(f"| {comp} | 0 | | | | | | | | | not run |")
                continue
            a, b = list(r["asr"].values())
            lines.append(f"| {comp} | {r['pairs']} | {r['families']} | {pct(a)} | {pct(b)} | "
                         f"{pct(r['absolute_risk_reduction'])} | {pct(r['relative_risk_reduction'])} | "
                         f"{r['ci95_asr_difference']} | {r['p_mcnemar']:.3g} | {r['p_holm']:.3g} | "
                         f"{'yes' if r['supported'] else 'no'} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def analyse(trials: list[Trial], *, label: str, splits: list[str], resamples: int = RESAMPLES) -> dict:
    scored = [t for t in trials if t.attack_success is not None]
    return {"label": label, "splits": splits, "trials": len(scored), "harness_errors": len(trials) - len(scored),
            "H3": hypothesis(trials, H3_COMPARATORS, resamples=resamples),
            "H5": hypothesis(trials, H5_COMPARATORS, direction="ablation higher", resamples=resamples)}


def main(argv: Optional[list[str]] = None) -> int:
    from opsguard.bench.convert import split_for
    from opsguard.bench.impact_tasks import load_tasks, memory_rows

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results", nargs="*", default=[], help="impact_tasks results.jsonl or RunResult v2 JSONL")
    parser.add_argument("--splits", default="dev,val")
    parser.add_argument("--dry-run", action="store_true", help="memory decisions on the open splits, no files needed")
    parser.add_argument("--kind-results", help="impact_tasks kind results for harm-prediction accuracy")
    parser.add_argument("--case", default="abh-shared-config-01")
    parser.add_argument("--resamples", type=int, default=RESAMPLES)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    splits = args.splits.split(",")
    tasks = load_tasks(Path.cwd())
    if "test" in splits and args.dry_run:
        parser.error("a dry run never touches the test split")
    if args.dry_run:
        open_tasks = [t for t in tasks if split_for(t["family"]) in splits]
        rows = memory_rows(open_tasks)
        trials = trials_from_impact_rows(rows)
        label = "DRY RUN: memory replay, open splits, not the pre-registered test"
    else:
        trials = []
        for path in args.results:
            rows = [json.loads(l) for l in Path(path).read_text(encoding="utf-8").splitlines() if l.strip()]
            trials += trials_from_impact_rows(rows) if rows and "action" in rows[0] else trials_from_run_results(rows)
        trials = [t for t in trials if split_for(t.family) in splits]
        label = "pre-registered analysis" if splits == ["test"] else "exploratory"
    report = analyse(trials, label=label, splits=splits, resamples=args.resamples)
    if args.kind_results:
        rows = [json.loads(l) for l in Path(args.kind_results).read_text(encoding="utf-8").splitlines() if l.strip()]
        report["harm_prediction"] = harm_prediction_accuracy(tasks, rows)
    case = next((t for t in tasks if t["id"] == args.case), None)
    if case is not None and (args.dry_run is False or split_for(case["family"]) in splits):
        report["case_study"] = case_study(case)
    out = Path(args.output)
    out.mkdir(parents=True, exist_ok=True)
    (out / "report.json").write_text(json.dumps(report, indent=1, default=str), encoding="utf-8")
    (out / "report.md").write_text(render_markdown(report), encoding="utf-8")
    print(render_markdown(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
