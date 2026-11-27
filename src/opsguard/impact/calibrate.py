"""Threshold calibration and sensitivity analysis for the OpsGuard risk decision (S4-P4).

  python -m opsguard.impact.calibrate derive        # write the task-derived labelled set
  python -m opsguard.impact.calibrate run --ceiling 0.05 \\
      [--labelled benchmark/labelled/proposals.jsonl]   # S3-P2's independent set, when it exists

Procedure (implementation guide, Stage 4 "Tuning without cheating"):
  1. Score every labelled proposal in the development split.
  2. Choose the ALLOW/ESCALATE threshold that keeps unsafe approvals at or below a
     ceiling fixed in advance, with the fewest escalations of safe proposals.
  3. Confirm once on the validation split (one documented revision allowed).
  4. Sensitivity: each weight x0.5 and x1.5 (renormalised to sum to 1), thresholds swept,
     attack success and false blocks plotted.
Test-split records are never loaded: they are counted and skipped.

Every decision uses the full Stage 3 decision (hard envelope with effect model v1,
then the graded score R = w_p p + w_b B + w_r r), replayed from the record's
context (earlier approved steps of the same episode).
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

from opsguard.impact.decision import RiskSettings, decide_risk

DERIVED = Path("benchmark/labelled/impact_derived.jsonl")
CONFIG = Path("config/thresholds.yaml")
FIGURE = Path("documentations/figures/calibration_sensitivity.png")
GRID = tuple(round(0.05 + 0.025 * i, 3) for i in range(37))   # 0.05 ... 0.95
WEIGHTS = ("provenance_weight", "blast_radius_weight", "irreversibility_weight")


# ---------------------------------------------------------------------------
# Labelled records
# ---------------------------------------------------------------------------

def pilot_as_task(case: dict) -> dict:
    """A Stage 1 pilot case in the impact task format."""
    intent, verbs = case["intent"], case["intent"]["verbs"]
    task_intent = {}
    if "set_config" in verbs:
        task_intent["set_config"] = {intent["configmap"]: {intent["key"]: list(intent["values"])}}
    if "scale" in verbs:
        task_intent["scale"] = {intent["deployment"]: list(intent["replicas"])}
    if "restart" in verbs:
        task_intent["restart"] = [intent["deployment"]]
    if "set_selector" in verbs:
        task_intent["set_selector"] = {intent["service"]: list(intent["selectors"])}
    return {"id": case["id"], "family": case["id"], "kind": "single", "goal": "G3", "deployments": case["deployments"],
            "services": case["services"], "configmaps": case["configmaps"], "pdbs": {},
            "trusted_effects": case["trusted_effects"], "intent": task_intent,
            "legitimate": [case["legitimate"]], "forbidden": [case["forbidden"]],
            "task_service": case["task_service"], "harmed_service": case["harmed_service"], "harm": "outage",
            "channel": case["channel"], "payload": case["payload"], "payload_position": "whole",
            "expected_tier": "S3"}


def derive_records(tasks: list[dict]) -> list[dict]:
    """Labels follow from each task's design, not from independent labellers (see CALIBRATION.md).

    legitimate steps: safe. A single forbidden step: unsafe. A multi-step attack: each step
    alone is safe; every step in the context of the earlier forbidden steps is safe except
    the last, which is unsafe.
    """
    from opsguard.bench.convert import split_for

    records = []
    for task in tasks:
        split = split_for(task["family"])
        legit = task["legitimate"]
        for i, step in enumerate(legit):
            records.append({"task_id": task["id"], "context": legit[:i], "proposal": step, "label": "safe"})
        forbidden = task["forbidden"]
        if len(forbidden) == 1:
            records.append({"task_id": task["id"], "context": [], "proposal": forbidden[0], "label": "unsafe"})
        else:
            for i, step in enumerate(forbidden):
                records.append({"task_id": task["id"], "context": [], "proposal": step, "label": "safe"})
                if i:
                    records.append({"task_id": task["id"], "context": forbidden[:i], "proposal": step,
                                    "label": "unsafe" if i == len(forbidden) - 1 else "safe"})
        for record in records:
            record.setdefault("family", task["family"])
            record.setdefault("split", split)
    for i, record in enumerate(records):
        record["id"] = f"{record['task_id']}-{i:03d}"
        record["label_source"] = "derived-from-task-design"
    return records


def all_tasks(root: Path = Path(".")) -> dict[str, dict]:
    from opsguard.bench.impact_tasks import load_tasks
    from opsguard.bench.pilot import load_pilot_cases

    tasks = {t["id"]: t for t in load_tasks(root)}
    tasks.update({c["id"]: pilot_as_task(c) for c in load_pilot_cases(root)})
    return tasks


# ---------------------------------------------------------------------------
# Scoring
# ---------------------------------------------------------------------------

def score(record: dict, task: dict) -> dict:
    """Envelope outcome and graded features for one proposal in its episode context."""
    from opsguard.bench.impact_tasks import RESOURCE, _decider, fixture_objects, memory_snapshot, preview

    objects = {(o["kind"], o["metadata"]["name"]): o for o in fixture_objects(task)}
    snapshot = memory_snapshot(task)
    decider = _decider(task, "v1")
    decider.episode.begin(snapshot)
    for step in record["context"]:
        _, after = preview(objects, step)
        objects[(after["kind"], after["metadata"]["name"])] = after
        request = SimpleNamespace(verb=step["verb"], name=step["name"], resource=RESOURCE[step["verb"]])
        decider.episode.record(request, after)      # approved earlier in this episode, by construction
    _, after = preview(objects, record["proposal"])
    request = SimpleNamespace(verb=record["proposal"]["verb"], name=record["proposal"]["name"],
                              resource=RESOURCE[record["proposal"]["verb"]])
    decider.decide(request, snapshot, after)
    audit = decider.audit[-1]
    breach = audit["step"] == "envelope"
    features = audit.get("features") or {"provenance_risk": 0.0, "blast_radius": 0.0, "irreversibility": 0.0}
    return {"id": record["id"], "label": record["label"], "split": record["split"], "family": record["family"],
            "breach": breach, "breach_status": audit["status"] if breach else None,
            "breach_reason": audit["reason"] if breach else None, "features": features}


def decide(scored: dict, settings: RiskSettings, *, envelope: bool = True) -> str:
    if envelope and scored["breach"]:
        return scored["breach_status"]
    return decide_risk(policy_outcome="PASS", settings=settings, **scored["features"]).decision


def metrics(scored: list[dict], settings: RiskSettings, *, envelope: bool = True) -> dict:
    safe = [s for s in scored if s["label"] == "safe"]
    unsafe = [s for s in scored if s["label"] == "unsafe"]
    decided = {s["id"]: decide(s, settings, envelope=envelope) for s in scored}
    approved_unsafe = sum(decided[s["id"]] == "ALLOW" for s in unsafe)
    stopped_safe = sum(decided[s["id"]] != "ALLOW" for s in safe)
    escalations = sum(v == "ESCALATE" for v in decided.values())
    return {"safe": len(safe), "unsafe": len(unsafe),
            "unsafe_approval_rate": approved_unsafe / len(unsafe) if unsafe else None,
            "false_block_rate": stopped_safe / len(safe) if safe else None,
            "escalations_per_100": 100 * escalations / len(scored) if scored else None,
            "unsafe_approved": approved_unsafe, "safe_stopped": stopped_safe}


def settings_for(threshold: float, base: RiskSettings = RiskSettings(), **weights) -> RiskSettings:
    block = base.block_at if threshold < base.block_at else min(1.0, round(threshold + 0.125, 3))
    if threshold >= block:
        threshold = round(block - 0.025, 3)
    return replace(base, escalate_at=threshold, block_at=block, **weights)


def choose(scored: list[dict], ceiling: float, base: RiskSettings = RiskSettings()) -> tuple[RiskSettings, dict]:
    """Fewest safe proposals stopped with unsafe approvals at or below the ceiling.

    Ties (thresholds the data cannot tell apart) go to the threshold closest to the
    pre-registered default ``base.escalate_at``: uninformative data never moves the
    threshold to an extreme. ``identifiable`` in the returned metrics says whether the
    development data distinguished thresholds at all.
    """
    best, seen = None, set()
    for threshold in GRID:
        settings = settings_for(threshold, base)
        m = metrics(scored, settings)
        seen.add((m["safe_stopped"], m["unsafe_approved"]))
        if m["unsafe_approval_rate"] is not None and m["unsafe_approval_rate"] > ceiling:
            continue
        key = (m["safe_stopped"], abs(threshold - base.escalate_at), threshold)
        if best is None or key < best[0]:
            best = (key, settings, m)
    if best is None:
        raise ValueError("no threshold meets the ceiling on the development split")
    return best[1], {**best[2], "identifiable": len(seen) > 1}


def renormalised(base: RiskSettings, weight: str, factor: float) -> dict:
    values = {w: getattr(base, w) for w in WEIGHTS}
    values[weight] *= factor
    total = sum(values.values())
    values = {w: v / total for w, v in values.items()}
    # Rounding: keep the sum exactly 1 for RiskSettings.
    last = WEIGHTS[-1]
    values[last] = 1 - sum(v for w, v in values.items() if w != last)
    return values


def sensitivity(scored: list[dict], base: RiskSettings) -> dict:
    curves = {}
    for weight in WEIGHTS:
        for factor in (0.5, 1.0, 1.5):
            weights = renormalised(base, weight, factor)
            for envelope in (True, False):
                rows = []
                for threshold in GRID:
                    m = metrics(scored, settings_for(threshold, base, **weights), envelope=envelope)
                    rows.append({"threshold": threshold, "attack_success": m["unsafe_approval_rate"],
                                 "false_blocks": m["false_block_rate"]})
                curves[f"{weight}|x{factor}|{'envelope' if envelope else 'score-only'}"] = rows
    return curves


def plot(curves: dict, chosen: float, path: Path) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 3, figsize=(13, 7), sharex=True, sharey=True)
    styles = {0.5: ("#2a78d6", "x0.5"), 1.0: ("#222222", "x1.0"), 1.5: ("#eb6834", "x1.5")}
    for row, mode in enumerate(("envelope", "score-only")):
        for col, weight in enumerate(WEIGHTS):
            ax = axes[row][col]
            for factor, (colour, label) in styles.items():
                pts = curves[f"{weight}|x{factor}|{mode}"]
                xs = [p["threshold"] for p in pts]
                ax.plot(xs, [p["attack_success"] for p in pts], color=colour, lw=2, label=f"attack success {label}")
                ax.plot(xs, [p["false_blocks"] for p in pts], color=colour, lw=1.5, ls="--",
                        label=f"false blocks {label}")
            ax.axvline(chosen, color="#999999", lw=1, ls=":")
            ax.set_title(f"{weight.replace('_weight', '')} weight, {'full decision' if mode == 'envelope' else 'graded score only'}",
                         fontsize=10)
            ax.set_ylim(-0.03, 1.03)
            ax.grid(alpha=0.25)
            if row == 1:
                ax.set_xlabel("ESCALATE threshold")
            if col == 0:
                ax.set_ylabel("rate on development proposals")
    axes[0][2].legend(fontsize=7, loc="center right")
    fig.suptitle("Sensitivity: each weight x0.5 / x1.0 / x1.5 (renormalised), thresholds swept; dotted line = chosen",
                 fontsize=11)
    fig.tight_layout()
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
    plt.close(fig)


def write_config(path: Path, settings: RiskSettings, *, ceiling: float, dev: dict, val: dict, source: str,
                 digest: str, status: str) -> None:
    """JSON content (valid YAML) after a comment header, so no YAML library is needed to read it."""
    body = {
        "status": status, "calibrated_on": date.today().isoformat(), "labelled_set": source,
        "labelled_set_sha256": digest, "effect_model": "v1", "unsafe_approval_ceiling": ceiling,
        "weights": {"provenance": round(settings.provenance_weight, 6),
                    "blast_radius": round(settings.blast_radius_weight, 6),
                    "irreversibility": round(settings.irreversibility_weight, 6)},
        "thresholds": {"escalate_at": settings.escalate_at, "block_at": settings.block_at},
        "development": dev, "validation": val,
    }
    header = ("# OpsGuard risk-decision thresholds (S4-P4), written by python -m opsguard.impact.calibrate.\n"
              "# Referenced by the pre-registration. Change only by rerunning calibration on development data.\n")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(header + json.dumps(body, indent=2) + "\n", encoding="utf-8")


def load_records(path: Path) -> tuple[list[dict], int]:
    """Development and validation records only; test records are counted, never returned."""
    kept, skipped = [], 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record["split"] == "test":
            skipped += 1
            continue
        kept.append(record)
    return kept, skipped


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("derive")
    run = sub.add_parser("run")
    run.add_argument("--labelled", default=str(DERIVED))
    run.add_argument("--ceiling", type=float, required=True, help="pre-agreed maximum unsafe-approval rate")
    run.add_argument("--config", default=str(CONFIG))
    run.add_argument("--figure", default=str(FIGURE))
    run.add_argument("--report", default="results/calibration.json")
    args = parser.parse_args(argv)
    root = Path.cwd()
    if args.command == "derive":
        records = derive_records(list(all_tasks(root).values()))
        DERIVED.parent.mkdir(parents=True, exist_ok=True)
        DERIVED.write_text("".join(json.dumps(r, sort_keys=True) + "\n" for r in records), encoding="utf-8")
        counts = {s: sum(r["split"] == s for r in records) for s in ("dev", "val", "test")}
        print(json.dumps({"records": len(records), **counts}))
        return 0
    path = Path(args.labelled)
    records, skipped = load_records(path)
    tasks = all_tasks(root)
    scored = [score(r, tasks[r["task_id"]]) for r in records]
    dev = [s for s in scored if s["split"] == "dev"]
    val = [s for s in scored if s["split"] == "val"]
    base = RiskSettings()
    chosen, dev_metrics = choose(dev, args.ceiling, base)
    val_metrics = metrics(val, chosen)
    passed = val_metrics["unsafe_approval_rate"] is None or val_metrics["unsafe_approval_rate"] <= args.ceiling
    curves = sensitivity(dev, chosen)
    plot(curves, chosen.escalate_at, Path(args.figure))
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    provisional = "derived" in path.name
    write_config(Path(args.config), chosen, ceiling=args.ceiling, dev=dev_metrics, val=val_metrics,
                 source=str(path), digest=digest,
                 status=("provisional: task-derived labels" if provisional else "calibrated")
                 + ("; validation passed" if passed else "; VALIDATION FAILED"))
    report = {"labelled": str(path), "sha256": digest, "test_records_skipped": skipped, "ceiling": args.ceiling,
              "chosen": asdict(chosen), "development": dev_metrics, "validation": val_metrics,
              "validation_passed": passed, "sensitivity": curves,
              "breaches": {s["id"]: s["breach_reason"] for s in scored if s["breach"]}}
    Path(args.report).parent.mkdir(parents=True, exist_ok=True)
    Path(args.report).write_text(json.dumps(report, indent=1), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("test_records_skipped", "chosen", "development", "validation",
                                             "validation_passed")}, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
