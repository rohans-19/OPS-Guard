"""Regenerate aggregate tables, figures and a Markdown report from saved result files.

    python -m opsguard.bench.report results/selector-memory --output results/report-memory

Inputs are result files or run directories from any owner. No cluster or model
credentials are needed. The expected matrix defaults to the models and repeats
found in the data. Pass --model and --repeats to check a frozen matrix instead.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import pandas as pd

from opsguard.bench.aggregate import BASELINE, analyse, merge, read_rows, team_case_index, valid
from opsguard.bench.schema import CHANNELS, CONDITIONS, PRIMARY_CONDITIONS

# Fixed colour per condition, so a filtered report never repaints the survivors.
# Validated together for colour-vision separation on the light surface. Three are
# below 3:1 contrast, so every bar carries a count label and each figure has a CSV.
CONDITION_COLOURS = {"unprotected": "#2a78d6", "hardened": "#eb6834", "llm-judge": "#1baf7a",
                     "opsguard": "#eda100", "task-policy": "#e87ba4"}
CONDITION_LABELS = {"unprotected": "No defence", "hardened": "Hardened prompt", "llm-judge": "LLM judge",
                    "opsguard": "OpsGuard", "task-policy": "Task policy only"}
SURFACE, INK, MUTED, GRID = "#fcfcfb", "#0b0b0b", "#52514e", "#e4e3df"


def _pct(value) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _ci(interval) -> str:
    return "n/a" if not interval else f"{interval[0] * 100:.1f} to {interval[1] * 100:.1f}%"


def _p(value) -> str:
    if value is None:
        return "n/a"
    return "< 0.0001" if value < 0.0001 else f"{value:.4f}"


def _cell(stat: dict) -> str:
    return f'{stat["count"]}/{stat["n"]} ({_pct(stat["rate"])})'


def table(headers, rows) -> list[str]:
    lines = ["| " + " | ".join(headers) + " |", "| " + " | ".join("---" for _ in headers) + " |"]
    return lines + ["| " + " | ".join(str(value) for value in row) + " |" for row in rows]


def flat(groups: list[dict], keys) -> pd.DataFrame:
    """One row per group with counts kept beside every percentage."""
    records = []
    for group in groups:
        record = {key: group[key] for key in keys}
        record.update(trials=group["trials"], valid_trials=group["valid_trials"], errors=group["errors"])
        for name in ("attack_success", "completion_clean", "completion_attacked", "completion_all"):
            stat = group[name]
            record.update({f"{name}_count": stat["count"], f"{name}_n": stat["n"], f"{name}_rate": stat["rate"],
                           f"{name}_ci95_low": stat["ci95"][0] if stat["ci95"] else None,
                           f"{name}_ci95_high": stat["ci95"][1] if stat["ci95"] else None})
        records.append(record)
    return pd.DataFrame.from_records(records)


def grouped_bars(path: Path, frame: pd.DataFrame, *, category: str, metric: str, title: str, ylabel: str,
                 conditions) -> None:
    """Grouped bars on one zero-based percentage axis: category on x, one fixed colour per condition."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    categories = list(dict.fromkeys(frame[category]))
    present = [c for c in conditions if c in set(frame["condition"])]
    width = min(0.8 / max(len(present), 1), 0.22)
    figure, axis = plt.subplots(figsize=(max(6.4, 1.9 * len(categories) + 2.6), 4.2), dpi=150)
    figure.patch.set_facecolor(SURFACE)
    axis.set_facecolor(SURFACE)
    for index, condition in enumerate(present):
        subset = frame[frame["condition"] == condition].set_index(category)
        for position, name in enumerate(categories):
            if name not in subset.index or not subset.loc[name, f"{metric}_n"]:
                continue
            row = subset.loc[name]
            x = position + (index - (len(present) - 1) / 2) * width
            value = row[f"{metric}_rate"] * 100
            axis.bar(x, value, width=width, color=CONDITION_COLOURS[condition], edgecolor=SURFACE, linewidth=1.5,
                     label=CONDITION_LABELS[condition] if position == categories.index(name) and name == categories[0] else None,
                     zorder=3)
            if pd.notna(row[f"{metric}_ci95_low"]):
                axis.vlines(x, row[f"{metric}_ci95_low"] * 100, row[f"{metric}_ci95_high"] * 100,
                            color=INK, linewidth=1, zorder=4)
            top = row[f"{metric}_ci95_high"] * 100 if pd.notna(row[f"{metric}_ci95_high"]) else value
            axis.text(x, max(value, top) + 2, f'{int(row[f"{metric}_count"])}/{int(row[f"{metric}_n"])}',
                      ha="center", va="bottom", fontsize=7.5, color=INK)
    handles, labels = axis.get_legend_handles_labels()
    seen = dict(zip(labels, handles))
    # Legend order follows the fixed condition order, whichever category drew first.
    ordered = [CONDITION_LABELS[c] for c in present if CONDITION_LABELS[c] in seen]
    missing = [c for c in present if CONDITION_LABELS[c] not in seen]
    from matplotlib.patches import Patch
    legend = [seen[name] for name in ordered] + [Patch(color=CONDITION_COLOURS[c]) for c in missing]
    axis.legend(legend, ordered + [CONDITION_LABELS[c] for c in missing], frameon=False, fontsize=8,
                loc="upper left", bbox_to_anchor=(1.0, 1.0), labelcolor=MUTED)
    axis.set_xticks(range(len(categories)), [str(name) for name in categories], fontsize=8.5, color=INK)
    # Keep bars thin when there are only one or two categories.
    centre, half = (len(categories) - 1) / 2, max(len(categories), 2.5) / 2
    axis.set_xlim(centre - half, centre + half)
    axis.set_ylim(0, 118)
    axis.set_yticks(range(0, 101, 25), [f"{tick}%" for tick in range(0, 101, 25)], fontsize=8, color=MUTED)
    axis.set_ylabel(ylabel, fontsize=8.5, color=MUTED)
    axis.set_title(title, fontsize=10.5, color=INK, loc="left", pad=10)
    axis.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
    axis.tick_params(length=0)
    for side in ("top", "right", "left"):
        axis.spines[side].set_visible(False)
    axis.spines["bottom"].set_color(GRID)
    figure.text(0.01, 0.01, "Labels are successes / scored trials. Whiskers are 95% task-clustered bootstrap intervals.",
                fontsize=7, color=MUTED)
    figure.tight_layout(rect=(0, 0.035, 1, 1))
    figure.savefig(path, facecolor=SURFACE)
    plt.close(figure)


def render(summary: dict, merged, conditions) -> str:
    coverage = summary["coverage"]
    lines = ["# OpsGuard-Bench aggregate results", ""]
    if summary["scripted_only"]:
        lines += ["**These trials use scripted proposals.** They verify the runner, gateway and oracle paths. "
                  "They are not a measurement of how any model responds to injected evidence.", ""]
    lines += [f'Recorded {coverage["recorded_trials"]} of {coverage["expected_trials"]} expected trials: '
              f'{coverage["valid_trials"]} scored, {coverage["recorded_trials"] - coverage["valid_trials"]} with a harness error, '
              f'{coverage["missing_trials"]} missing, {coverage["duplicate_results"]} duplicate and '
              f'{coverage["rejected_results"]} rejected. Coverage is {"complete" if coverage["complete"] else "INCOMPLETE"}.', "",
              "Harness errors are reported separately and are in no rate denominator. "
              "A trial that failed is not counted as a prevented attack.", ""]
    if coverage["mixed_configurations"]:
        lines += ["**Mixed configurations:** " + json.dumps(coverage["mixed_configurations"]), ""]
    lines += ["## Outcomes by defence condition", ""]
    lines += table(["Condition", "Scored / total", "Attack success", "95% CI", "Completion, clean", "Completion, attacked", "Errors"],
                   [[CONDITION_LABELS.get(g["condition"], g["condition"]), f'{g["valid_trials"]}/{g["trials"]}',
                     _cell(g["attack_success"]), _ci(g["attack_success"]["ci95"]), _cell(g["completion_clean"]),
                     _cell(g["completion_attacked"]), g["errors"]] for g in summary["by_condition"]])
    lines += ["", "## Paired comparison with the unprotected baseline", "",
              "Trials are paired by model, case and repeat. The difference is condition minus baseline, "
              "so a negative value is fewer successful attacks.", ""]
    lines += table(["Condition", "Pairs", "Baseline", "Condition", "Absolute difference", "95% CI", "Relative reduction", "95% CI", "Exact McNemar p"],
                   [[CONDITION_LABELS.get(p["condition"], p["condition"]), p["pairs"],
                     f'{p.get("baseline_successes", 0)}/{p["pairs"]}', f'{p.get("condition_successes", 0)}/{p["pairs"]}',
                     "n/a" if "absolute_difference" not in p else f'{p["absolute_difference"] * 100:+.1f} pts',
                     _ci(p.get("absolute_difference_ci95")),
                     "undefined (baseline is zero)" if p.get("relative_reduction") is None and p["pairs"] else _pct(p.get("relative_reduction")),
                     _ci(p.get("relative_reduction_ci95")), _p(p.get("mcnemar_exact_p"))] for p in summary["paired"]])
    hypothesis = summary["hypotheses"]
    reduction, completion = hypothesis["relative_reduction_at_least_50pct"], hypothesis["legitimate_completion_at_least_80pct"]
    verdict = {True: "met", False: "not met", None: "not testable"}
    lines += ["", "## Hypotheses", "",
              f'- At least 50% relative reduction in attack success with OpsGuard: **{verdict[reduction["met"]]}** '
              f'(estimate {_pct(reduction["estimate"])}, 95% CI {_ci(reduction["ci95"])}).'
              + (f' {reduction["note"].capitalize()}.' if reduction.get("note") else ""),
              f'- At least 80% legitimate task completion with OpsGuard: **{verdict[completion["met"]]}** '
              f'({completion["count"]}/{completion["n"]}, {_pct(completion["estimate"])}, 95% CI {_ci(completion["ci95"])}).',
              "", "## By model", ""]
    lines += table(["Model", "Condition", "Attack success", "Completion, clean", "Completion, attacked", "Errors"],
                   [[g["model_id"], CONDITION_LABELS.get(g["condition"], g["condition"]), _cell(g["attack_success"]),
                     _cell(g["completion_clean"]), _cell(g["completion_attacked"]), g["errors"]]
                    for g in summary["by_model_condition"]])
    lines += ["", "## By evidence channel (attacked cases)", ""]
    lines += table(["Channel", "Condition", "Attack success", "95% CI"],
                   [[g["channel"], CONDITION_LABELS.get(g["condition"], g["condition"]), _cell(g["attack_success"]),
                     _ci(g["attack_success"]["ci95"])] for g in summary["by_channel_condition"]])
    lines += ["", "## By owner", ""]
    lines += table(["Owner", "Condition", "Scored / total", "Attack success", "Completion, all"],
                   [[g["owner"], CONDITION_LABELS.get(g["condition"], g["condition"]), f'{g["valid_trials"]}/{g["trials"]}',
                     _cell(g["attack_success"]), _cell(g["completion_all"])] for g in summary["by_owner_condition"]])
    errors = [row for row in merged.rows if not valid(row)]
    if errors or merged.missing or merged.duplicates or merged.rejected:
        lines += ["", "## Coverage problems", ""]
        lines += [f'- Error: {row["case_id"]} / {row["model_id"]} / {row["condition"]} / repeat {row["repeat"]}: {row["error"]}'
                  for row in errors[:50]]
        lines += [f"- Missing: {case} / {model} / {condition} / repeat {repeat}"
                  for case, model, condition, repeat in merged.missing[:50]]
        lines += [f'- Duplicate: {row["case_id"]} / {row["condition"]} / repeat {row["repeat"]} ({row["reason"]}, {row["source"]})'
                  for row in merged.duplicates[:50]]
        lines += [f'- Rejected: {row.get("case_id")} ({row["reason"]}, {row["source"]})' for row in merged.rejected[:50]]
    lines += ["", "## Method", "",
              f'Intervals: {summary["method"]["bootstrap"]}. Repeats and attack variants of one task are resampled together. '
              "An interval is n/a with fewer than two tasks. The exact McNemar p-value treats pairs as independent; "
              "read it with the clustered interval. Figures and CSV tables beside this file come from the same merged rows.", ""]
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("inputs", nargs="+", type=Path, help="result .jsonl files or run directories")
    parser.add_argument("--output", type=Path, required=True, help="new directory for the report")
    parser.add_argument("--model", action="append", help="expected model ID, repeatable (default: models in the data)")
    parser.add_argument("--condition", action="append", choices=CONDITIONS,
                        help="expected condition, repeatable (default: the four primary conditions)")
    parser.add_argument("--repeats", type=int, help="expected repeats per cell (default: highest repeat in the data)")
    parser.add_argument("--owner", action="append", help="restrict to an owner's cases, repeatable")
    parser.add_argument("--backend", choices=("memory", "kind"), help="accept only this fixture backend")
    parser.add_argument("--allow-incomplete", action="store_true", help="exit 0 even when coverage is incomplete")
    args = parser.parse_args(argv)
    if args.output.exists() and any(args.output.iterdir()):
        parser.error("choose a new or empty output directory")
    root = Path.cwd()
    index = team_case_index(root)
    raw = [item for path in args.inputs for item in read_rows(path)]
    trials = [row for row, _ in raw if row.get("kind") in (None, "trial") and row.get("case_id") in index]
    if not trials:
        parser.error("no trial rows for known cases in the inputs")
    conditions = tuple(dict.fromkeys(args.condition or PRIMARY_CONDITIONS))
    selected = [row for row in trials if not args.owner or index[row["case_id"]].owner in args.owner]
    models = list(dict.fromkeys(args.model or sorted({row["model_id"] for row in selected})))
    repeats = args.repeats or max(row["repeat"] for row in selected)
    merged = merge(raw, index, models=models, conditions=conditions, repeats=repeats,
                   owners=set(args.owner) if args.owner else {index[row["case_id"]].owner for row in selected},
                   backend=args.backend)
    summary = analyse(merged, conditions=conditions)
    summary["expected_matrix"] = {"models": models, "conditions": list(conditions), "repeats": repeats,
                                  "baseline": BASELINE, "inputs": [path.as_posix() for path in args.inputs]}

    output = args.output
    (output / "tables").mkdir(parents=True, exist_ok=True)
    (output / "figures").mkdir(exist_ok=True)
    with (output / "merged.jsonl").open("w", encoding="utf-8") as stream:
        for row in merged.rows:
            stream.write(json.dumps(row, allow_nan=False) + "\n")
    (output / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    by_condition = flat(summary["by_condition"], ["condition"])
    by_model = flat(summary["by_model_condition"], ["model_id", "condition"])
    by_channel = flat(summary["by_channel_condition"], ["channel", "condition"])
    by_condition.to_csv(output / "tables/by_condition.csv", index=False)
    by_model.to_csv(output / "tables/by_model_condition.csv", index=False)
    by_channel.to_csv(output / "tables/by_channel_condition.csv", index=False)
    flat(summary["by_owner_condition"], ["owner", "condition"]).to_csv(output / "tables/by_owner_condition.csv", index=False)
    pd.DataFrame.from_records(summary["paired"]).to_csv(output / "tables/paired_vs_unprotected.csv", index=False)
    pd.DataFrame.from_records(summary["paired_by_model"]).to_csv(output / "tables/paired_by_model.csv", index=False)

    if not by_model.empty:
        grouped_bars(output / "figures/attack_success_by_model.png", by_model, category="model_id",
                     metric="attack_success", title="Attack success by model and defence condition",
                     ylabel="Attacked trials where the oracle saw the forbidden state", conditions=CONDITIONS)
        grouped_bars(output / "figures/completion_by_model.png", by_model, category="model_id",
                     metric="completion_all", title="Legitimate task completion by model and defence condition",
                     ylabel="Scored trials where the legitimate outcome held", conditions=CONDITIONS)
    if not by_channel.empty:
        by_channel["channel"] = pd.Categorical(by_channel["channel"], categories=list(CHANNELS), ordered=True)
        grouped_bars(output / "figures/attack_success_by_channel.png", by_channel.sort_values("channel"),
                     category="channel", metric="attack_success", title="Attack success by evidence channel and defence condition",
                     ylabel="Attacked trials where the oracle saw the forbidden state", conditions=CONDITIONS)
    (output / "report.md").write_text(render(summary, merged, conditions), encoding="utf-8")
    print(json.dumps({"recorded": summary["coverage"]["recorded_trials"], "expected": summary["coverage"]["expected_trials"],
                      "complete": summary["coverage"]["complete"], "report": (output / "report.md").as_posix()}))
    return 0 if summary["coverage"]["complete"] or args.allow_incomplete else 1


if __name__ == "__main__":
    raise SystemExit(main())
