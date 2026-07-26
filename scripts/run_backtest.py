#!/usr/bin/env python3
"""Run the current projection backtest and write a versioned report.

The 2022-23 through 2024-25 seasons are coefficient-fit data, so their
metrics are in-sample diagnostics. The 2025-26 season is reported separately
as a post-tuning check. It is not described as an untouched independent
validation set because results from that season have been inspected during
iterative model development.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fpl_intel.backtest import build_backtest_report, load_season
from fpl_intel.coefficients import load_coefficients
from fpl_intel.fpl_data import save_json


MODEL_VERSION = str(load_coefficients()["model_version"])
FIT_SEASONS = ["2022-23", "2023-24", "2024-25"]
HELD_OUT_SEASONS = ["2025-26"]


def _print_summary(label, summary):
    if summary["count"] == 0:
        print(f"  {label}: no comparisons")
        return
    print(
        f"  {label}: n={summary['count']} mae={summary['mae']} bias={summary['bias']} "
        f"rmse={summary['rmse']} range_coverage={summary['range_coverage']}"
    )


def main():
    history_root = ROOT / "data" / "history"
    missing = [
        season for season in FIT_SEASONS + HELD_OUT_SEASONS
        if not (history_root / season).exists()
    ]
    if missing:
        raise SystemExit(
            f"Missing history for {missing} -- run scripts/fetch_history.py first."
        )

    fit_seasons = [load_season(history_root / season, label=season) for season in FIT_SEASONS]
    held_out_seasons = [load_season(history_root / season, label=season) for season in HELD_OUT_SEASONS]

    print(f"Running backtest over {FIT_SEASONS} ...")
    report = build_backtest_report(fit_seasons, model_version=MODEL_VERSION)

    print(f"Running reviewed post-tuning check over {HELD_OUT_SEASONS} ...")
    held_out_report = build_backtest_report(held_out_seasons, model_version=MODEL_VERSION)

    report["fit_evaluation_role"] = "in_sample_diagnostic"
    report["post_tuning_seasons"] = HELD_OUT_SEASONS
    report["post_tuning_evaluation_role"] = "reviewed_post_tuning_check_not_independent_validation"
    report["post_tuning_summary"] = held_out_report["summary"]
    report["post_tuning_by_horizon"] = held_out_report["by_horizon"]
    report["post_tuning_by_position"] = held_out_report["by_position"]
    report.pop("comparisons")  # raw rows are reproducible on demand; too large to keep in the committed baseline

    output_path = ROOT / "data" / f"backtest-baseline-v{MODEL_VERSION}.json"
    save_json(output_path, report)

    print()
    print(f"Fit seasons {FIT_SEASONS}: {report['completed_comparisons']} comparisons")
    _print_summary("overall", report["summary"])
    for horizon, summary in report["by_horizon"].items():
        _print_summary(f"horizon {horizon}", summary)
    for position, summary in report["by_position"].items():
        _print_summary(f"position {position}", summary)
    print()
    print(f"Post-tuning check {HELD_OUT_SEASONS} (reviewed, not independent validation):")
    _print_summary("overall", report["post_tuning_summary"])
    for horizon, summary in report["post_tuning_by_horizon"].items():
        _print_summary(f"horizon {horizon}", summary)
    print()
    print(f"Saved baseline report to {output_path}")


if __name__ == "__main__":
    main()
