"""Gameweek-deadline window arithmetic, shared by every offline script that needs to know "how
many hours until the next deadline" without trusting Railway's own (possibly stale) cached data.

Extracted from `scripts/send_deadline_reminder.py` (issue #55) when issue #101 needed the exact
same live-deadline-resolution + window-check logic for a second, unrelated purpose (triggering a
scheduled `/api/refresh` at fixed lead times before each deadline) -- duplicating this arithmetic
across two scripts would only have been a matter of time before they drifted out of sync.
"""

from datetime import datetime
import json
from pathlib import Path

from .fpl_data import fetch_bootstrap, fetch_fixtures


class DeadlineDataError(RuntimeError):
    """Live bootstrap fetch failed and no cached fallback exists either."""


def _load_json_or(path, default):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def load_bootstrap_and_fixtures(root):
    """Fetch a fresh bootstrap/fixtures pair, falling back to the last cached refresh on failure.

    Returns `(bootstrap, fixtures, stale)`. `stale` is True if either fetch fell back to disk.
    Raises `DeadlineDataError` if the live fetch fails and there's no cached bootstrap either --
    callers that want a different exception type (e.g. an existing `ConfigError`) should catch
    this and re-raise their own.
    """
    stale = False
    try:
        bootstrap = fetch_bootstrap()
    except Exception:
        bootstrap = _load_json_or(root / "data" / "fpl-bootstrap-latest.json", None)
        stale = True
        if bootstrap is None:
            raise DeadlineDataError(
                "Live bootstrap fetch failed and no cached data/fpl-bootstrap-latest.json exists."
            )
    try:
        fixtures = fetch_fixtures()
    except Exception:
        fixtures = _load_json_or(root / "data" / "fpl-fixtures-latest.json", [])
        stale = True
    return bootstrap, fixtures, stale


def next_unfinished_event(bootstrap):
    """The next gameweek event dict (with `deadline_time`), preferring FPL's own `is_next` flag."""
    events = bootstrap.get("events", [])
    explicit = next((event for event in events if event.get("is_next")), None)
    if explicit is not None:
        return explicit
    unfinished = [event for event in events if event.get("id") and not event.get("finished")]
    if not unfinished:
        return None
    return min(unfinished, key=lambda event: event["id"])


def hours_until(deadline_iso, now):
    deadline = datetime.fromisoformat(deadline_iso.replace("Z", "+00:00"))
    return (deadline - now).total_seconds() / 3600


def in_send_window(deadline_iso, now, lead_hours):
    """True for exactly one hourly tick per gameweek: `(lead_hours - 1, lead_hours]` hours out."""
    hours_left = hours_until(deadline_iso, now)
    return (lead_hours - 1) < hours_left <= lead_hours


def within_capture_window(deadline_iso, now, lead_hours):
    """True on every tick from `lead_hours` before the deadline until the deadline itself.

    The catch-up counterpart to `in_send_window` (issue #286): that fires for a single hourly
    tick and permanently misses its checkpoint whenever GitHub's best-effort cron delays that
    one tick past the hour -- as it did for every GW2 checkpoint this season. This window stays
    open for the whole run-up, so a later tick still captures the checkpoint. Callers must dedupe
    (the archiver relies on `archive_team_forecast`'s first-write-wins per `gw{event}:{lead_hours}`
    slot). The `> 0` lower bound is load-bearing: it keeps the window from reopening after the
    deadline passes but before FPL flags the gameweek `finished`, which is what stops a widened
    capture from ever archiving a post-deadline, hindsight-contaminated recommendation.
    """
    hours_left = hours_until(deadline_iso, now)
    return 0 < hours_left <= lead_hours
