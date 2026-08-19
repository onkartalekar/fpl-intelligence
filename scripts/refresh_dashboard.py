#!/usr/bin/env python3
"""Refresh official FPL and Premier League transfer data into `data/`.

This writes data only. It does **not** render the standalone `dashboard.html` file, despite
having said so until issue #229: since #120 the server renders the dashboard fresh from
`dashboard-state.json` on every request, and `publish_generation` stopped publishing an HTML
snapshot that nothing read. `scripts/rebuild_dashboard.py` is the only thing that writes
`dashboard.html` now, so refreshing the data and re-rendering that file are two commands:

    python3 scripts/refresh_dashboard.py    # fetch live data -> data/
    python3 scripts/rebuild_dashboard.py    # render data/ -> dashboard.html

Nobody using the local server needs the second one -- it exists for the standalone file, which
is opened straight off disk with no server behind it.
"""

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
    # Issue #229: this used to name `dashboard.html`, a file this script has not written since
    # #120 -- so on a machine where that file was days stale (or had never existed at all) the
    # line read as positive confirmation it was current. Name what was actually written.
    print(f"Data refreshed: {ROOT / 'data' / 'dashboard-state.json'}")
    print(f"Confirmed official movements: {len(state['transfers'])}")
    print(f"FPL feed status: {state['fpl']['season_status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
