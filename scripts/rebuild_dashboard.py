#!/usr/bin/env python3
"""Re-render dashboard.html from the last cached state, with no network calls.

Use this after pulling/merging a change to dashboard.py (template, CSS, JS)
to pick it up immediately, without waiting for -- or paying the cost of --
a full scripts/refresh_dashboard.py run against live FPL/PL sources.
"""

from pathlib import Path
import json
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fpl_intel.dashboard import render_dashboard
from fpl_intel.sources.fpl_data import atomic_write_text
from fpl_intel.generation import resolve_artifact


def main():
    state_path = resolve_artifact(ROOT, "dashboard-state.json")
    if not state_path.exists():
        print(
            "No cached dashboard state found -- run scripts/refresh_dashboard.py "
            "first to fetch live data before rebuilding from cache.",
            file=sys.stderr,
        )
        return 1
    state = json.loads(state_path.read_text(encoding="utf-8"))
    atomic_write_text(ROOT / "dashboard.html", render_dashboard(state))
    print(f"Dashboard rebuilt from cached state: {ROOT / 'dashboard.html'}")
    print(f"Cached state generated at: {state.get('generated_at', 'unknown')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
