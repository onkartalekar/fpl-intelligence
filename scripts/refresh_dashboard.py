#!/usr/bin/env python3
"""Refresh official FPL and Premier League transfer data, then rebuild dashboard.html."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fpl_intel.sources.pl_transfers import fetch_confirmed_transfers
from fpl_intel.refresh import RefreshAlreadyRunning, _refresh_project_unlocked, project_refresh_lock

_BUSY_EXIT_CODE = 75


def main():
    try:
        with project_refresh_lock(ROOT):
            source_errors = {}
            try:
                transfers = fetch_confirmed_transfers()
            except Exception as error:
                transfers = None
                source_errors["transfers"] = str(error)
            state = _refresh_project_unlocked(
                ROOT,
                official_transfer_records=transfers,
                source_errors=source_errors,
            )
    except RefreshAlreadyRunning:
        print("A dashboard refresh is already running.", file=sys.stderr)
        return _BUSY_EXIT_CODE
    print(f"Dashboard refreshed: {ROOT / 'dashboard.html'}")
    print(f"Confirmed official movements: {len(state['transfers'])}")
    print(f"FPL feed status: {state['fpl']['season_status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
