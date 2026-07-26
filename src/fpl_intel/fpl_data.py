"""Official FPL data collection and season readiness checks."""

from datetime import datetime
import json
import os
from pathlib import Path
import tempfile
from urllib.request import Request, urlopen


BOOTSTRAP_URL = "https://fantasy.premierleague.com/api/bootstrap-static/"
FIXTURES_URL = "https://fantasy.premierleague.com/api/fixtures/"
EVENT_LIVE_URL = "https://fantasy.premierleague.com/api/event/{event}/live/"


def summarize_bootstrap(payload, expected_first_deadline_year):
    events = payload.get("events", [])
    first_deadline_year = None
    if events and events[0].get("deadline_time"):
        first_deadline_year = datetime.fromisoformat(
            events[0]["deadline_time"].replace("Z", "+00:00")
        ).year
    ready = first_deadline_year == expected_first_deadline_year
    next_event = None
    if ready:
        next_event = next((event for event in events if event.get("is_next")), None)
        if next_event is None:
            next_event = next((event for event in events if not event.get("finished", False)), None)
    season_phase = "feed_pending"
    if ready:
        season_phase = "preseason" if next_event and next_event.get("id") == 1 else "in_season"
    return {
        "season_status": "target_season_ready" if ready else "prior_season_data",
        "season_phase": season_phase,
        "ready_for_2026_27": ready,
        "first_deadline_year": first_deadline_year,
        "next_event_id": next_event.get("id") if next_event else None,
        "next_event_name": next_event.get("name") if next_event else None,
        "next_deadline": next_event.get("deadline_time") if next_event else None,
        "event_count": len(events),
        "player_count": len(payload.get("elements", [])),
        "team_count": len(payload.get("teams", [])),
    }


def fetch_bootstrap(timeout=30):
    request = Request(BOOTSTRAP_URL, headers={"User-Agent": "FPL Intelligence local dashboard"})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def fetch_fixtures(timeout=30, opener=urlopen):
    request = Request(FIXTURES_URL, headers={"User-Agent": "FPL Intelligence local dashboard"})
    with opener(request, timeout=timeout) as response:
        return json.load(response)


def fetch_event_live(event, timeout=30, opener=urlopen):
    request = Request(
        EVENT_LIVE_URL.format(event=int(event)),
        headers={"User-Agent": "FPL Intelligence local dashboard"},
    )
    with opener(request, timeout=timeout) as response:
        return json.load(response)


def atomic_write_text(path, text):
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=destination.parent,
            prefix=f".{destination.name}.", suffix=".tmp", delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(text)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path and temporary_path.exists():
            temporary_path.unlink()


def save_json(path, value):
    atomic_write_text(path, json.dumps(value, indent=2, ensure_ascii=False))
