#!/usr/bin/env python3
"""Run tests/ as parallel subprocesses, mirroring .github/workflows/tests.yml's matrix.

Issue #176/#177: `unittest discover` has no built-in parallelism, and `tests/`'s runtime is
dominated by a handful of modules (test_transfer_decisions.py, test_server.py,
test_send_deadline_reminder.py, test_refresh.py, test_recommendations.py -- see the CI workflow's
comment for the measurements this grouping is sized from). A serial `python3 -m unittest discover
-s tests` pays the full sum of every module's time; this script launches the same four groups
tests.yml already runs in parallel CI jobs as four local subprocesses instead, bounding wall time
by the slowest group.

Stdlib-only (subprocess + concurrent.futures), matching this repo's dependency policy -- no
pytest-xdist or other parallel-test-runner dependency needed for this.

The four GROUPS below must be kept in sync with the `matrix.include` list in
.github/workflows/tests.yml -- there is deliberately no shared source between the YAML and this
script (parsing YAML would need a dependency this repo doesn't otherwise carry), so a change to
one has to be mirrored in the other by hand.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]

# [[run-full-tests]] (.claude/skills/run-full-tests/SKILL.md): plain `python3` on this machine
# resolves to a slow system Python 3.9, which can make a run "look hung" when it isn't -- the
# same interpreter gap that let the socket.timeout/TimeoutError bug (#173) go undetected in the
# first place, since Python 3.10+ (what CI and presumably Railway actually run) never exhibited
# it. Defaulting to sys.executable would silently inherit whatever invoked this script, so a plain
# `python3 scripts/run_tests_parallel.py` would quietly re-hit the slow interpreter. Warn instead
# of guessing at a fix -- there's no portable "correct" interpreter path (CI's is on PATH as
# `python3` already; only this specific local setup needs the workaround), so --python is the
# escape hatch rather than a hardcoded machine-specific default.
_MIN_RECOMMENDED = (3, 10)

GROUPS = {
    "transfer-decisions": [
        "tests.test_transfer_decisions",
    ],
    "server-and-recommendations": [
        # Issue #210: test_server.py itself now covers only DashboardHandler's own cross-cutting
        # plumbing -- the other eight modules below were split out of it, one per
        # server_handlers/*.py feature module it used to test inline.
        "tests.test_server",
        "tests.test_server_contact",
        "tests.test_server_draft_squad",
        "tests.test_server_lookup_opt_out",
        "tests.test_server_profile",
        "tests.test_server_refresh",
        "tests.test_server_release_notes",
        "tests.test_server_reminder",
        "tests.test_server_team_lookup",
        "tests.test_recommendations",
    ],
    "refresh-and-reminders": [
        "tests.test_refresh",
        "tests.test_send_deadline_reminder",
    ],
    "everything-else": [
        "tests.test_archive_team_forecasts", "tests.test_backtest", "tests.test_catalog",
        "tests.test_coefficients", "tests.test_dashboard", "tests.test_deadline_windows",
        # Issue #208: both lightweight, no HTTP server/combinatorial-search involved.
        "tests.test_decision_cache", "tests.test_generation",
        # Issue #197: new, lightweight (no server/network/combinatorial-search).
        "tests.test_email_template",
        "tests.test_fpl_data", "tests.test_live_regression_check", "tests.test_manager_data",
        "tests.test_minutes", "tests.test_ml_minutes", "tests.test_model_performance",
        "tests.test_news_signals", "tests.test_pl_transfers", "tests.test_profiles",
        "tests.test_projection", "tests.test_publish_release_notes", "tests.test_rate_limit",
        "tests.test_refresh_safety", "tests.test_release_notes",
        "tests.test_release_notes_email", "tests.test_release_notes_subscribers", "tests.test_relevance",
        "tests.test_reminder_confirmation", "tests.test_start_dashboard",
        "tests.test_team_strength", "tests.test_transfers", "tests.test_trigger_scheduled_refresh",
    ],
}


def _run_group(python, name, modules):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src")
    start = time.monotonic()
    result = subprocess.run(
        [python, "-m", "unittest", *modules, "-v"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    elapsed = time.monotonic() - start
    return name, result.returncode, elapsed, result.stdout, result.stderr


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--python", default=sys.executable,
        help=(
            "Interpreter to run each group's subprocess with (default: the interpreter running "
            "this script). On this repo's dev machine, plain `python3` resolves to a slow "
            "system Python -- pass --python /Users/onkartalekar/.local/bin/python3.11 (per "
            "[[run-full-tests]]) for an accurate, fast run."
        ),
    )
    args = parser.parse_args()

    try:
        version = subprocess.run(
            [args.python, "-c", "import sys; print('.'.join(map(str, sys.version_info[:2])))"],
            capture_output=True, text=True,
        ).stdout.strip()
    except OSError as error:
        parser.error(f"--python {args.python!r} is not runnable: {error}")
    if tuple(int(part) for part in version.split(".")) < _MIN_RECOMMENDED:
        print(
            f"WARNING: running with Python {version} (via {args.python}). This repo's CI and "
            f"deploy target 3.11+; a run under {'.'.join(map(str, _MIN_RECOMMENDED))}- can be "
            f"meaningfully slower and, per issue #173, can even behave differently. Pass "
            f"--python <path to a 3.11+ interpreter> for a representative run.\n"
        )

    print(f"Running {sum(len(m) for m in GROUPS.values())} test modules across {len(GROUPS)} parallel groups (python: {args.python}, {version})...\n")
    overall_start = time.monotonic()
    results = []
    with ThreadPoolExecutor(max_workers=len(GROUPS)) as pool:
        for name, returncode, elapsed, stdout, stderr in pool.map(
            lambda item: _run_group(args.python, *item), GROUPS.items()
        ):
            results.append((name, returncode, elapsed))
            status = "PASS" if returncode == 0 else "FAIL"
            print(f"=== [{status}] {name} ({elapsed:.1f}s) ===")
            # unittest -v writes its per-test lines and summary to stderr, not stdout.
            print(stderr.strip())
            if stdout.strip():
                print(stdout.strip())
            print()

    overall_elapsed = time.monotonic() - overall_start
    print("=" * 60)
    failed = [name for name, returncode, _ in results if returncode != 0]
    for name, returncode, elapsed in sorted(results, key=lambda row: row[2], reverse=True):
        status = "PASS" if returncode == 0 else "FAIL"
        print(f"  {status}  {name:30s} {elapsed:6.1f}s")
    print(f"\nWall time: {overall_elapsed:.1f}s (slowest group: {max(e for _, _, e in results):.1f}s)")

    if failed:
        print(f"\n{len(failed)} group(s) failed: {', '.join(failed)}")
        sys.exit(1)
    print("\nAll groups passed.")


if __name__ == "__main__":
    main()
