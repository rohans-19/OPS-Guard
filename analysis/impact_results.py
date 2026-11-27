"""S5-P4 analysis entry point: effect-model accuracy, H3, H5 and the case study.

See opsguard.bench.impact_analysis for definitions. Example:
  python analysis/impact_results.py --dry-run --output results/analysis/impact-dry-run
"""

from opsguard.bench.impact_analysis import main

if __name__ == "__main__":
    raise SystemExit(main())
