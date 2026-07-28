"""Collect and summarize public FPL manager data without credentials."""

import json
from urllib.request import Request, urlopen


_API_ROOT = "https://fantasy.premierleague.com/api"


def _fetch_json(url):
    request = Request(url, headers={"User-Agent": "fpl-intelligence/1.0"})
    with urlopen(request, timeout=30) as response:
        return json.load(response)


def collect_public_manager(team_id, fetch_json=None):
    """Fetch the public entry, history, transfers, and available gameweek picks."""
    fetch_json = fetch_json or _fetch_json
    base = f"{_API_ROOT}/entry/{int(team_id)}"
    entry = fetch_json(f"{base}/")
    history = fetch_json(f"{base}/history/")
    transfers = fetch_json(f"{base}/transfers/")
    current_event = entry.get("current_event")
    picks = fetch_json(f"{base}/event/{current_event}/picks/") if current_event else None
    return {
        "entry": entry,
        "history": history,
        "transfers": transfers,
        "picks": picks,
    }


def fetch_manager_event_picks(team_id, event_id, fetch_json=None):
    """Fetch a manager's published picks for one specific past Gameweek."""
    fetch_json = fetch_json or _fetch_json
    return fetch_json(f"{_API_ROOT}/entry/{int(team_id)}/event/{int(event_id)}/picks/")


def summarize_manager(raw, bootstrap):
    """Create dashboard-safe manager state and map public picks to FPL players."""
    entry = raw.get("entry", {})
    picks_payload = raw.get("picks") or {}
    player_by_id = {player.get("id"): player for player in bootstrap.get("elements", [])}
    squad = []
    for pick in picks_payload.get("picks", []):
        player = player_by_id.get(pick.get("element"), {})
        squad.append(
            {
                "element_id": pick.get("element"),
                "name": player.get("web_name") or f"Player {pick.get('element')}",
                "position": pick.get("position"),
                "multiplier": pick.get("multiplier", 0),
                "is_captain": bool(pick.get("is_captain")),
                "is_vice_captain": bool(pick.get("is_vice_captain")),
                "team_id": player.get("team"),
                "element_type": player.get("element_type"),
                "price": player.get("now_cost"),
                "purchase_price": pick.get("purchase_price"),
                "selling_price": pick.get("selling_price"),
            }
        )
    manager_name = " ".join(
        part for part in [entry.get("player_first_name"), entry.get("player_last_name")] if part
    )
    current_event = entry.get("current_event")
    entry_history = picks_payload.get("entry_history") or {}
    return {
        "team_id": entry.get("id"),
        "team_name": entry.get("name"),
        "manager_name": manager_name,
        "connection_status": "connected" if current_event else "registered_preseason",
        "current_event": current_event,
        "started_event": entry.get("started_event"),
        "overall_points": entry.get("summary_overall_points"),
        "overall_rank": entry.get("summary_overall_rank"),
        "bank": entry_history.get("bank", entry.get("last_deadline_bank")),
        "team_value": entry_history.get("value", entry.get("last_deadline_value")),
        "transfers_made": len(raw.get("transfers") or []),
        "public_transfers": list(raw.get("transfers") or []),
        "chips_used": list((raw.get("history") or {}).get("chips", [])),
        "active_chip": picks_payload.get("active_chip"),
        "squad_publicly_available": bool(squad),
        "squad": squad,
    }
