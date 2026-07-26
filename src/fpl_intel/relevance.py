"""FPL relevance and club-impact enrichment for confirmed transfers."""

from collections import defaultdict
from datetime import datetime
import re
import unicodedata


def _token(value):
    text = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def _parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _direction(movement_type):
    return {
        "transfer-in": "in",
        "loan-in": "in",
        "transfer-out": "out",
        "loan-out": "out",
        "end-of-loan": "out",
        "player-released": "released",
    }.get(movement_type, "unknown")


def enrich_transfers(transfers, bootstrap, generated_at):
    """Add deterministic relevance, movement, freshness, and FPL-match fields."""
    full_names = {}
    web_names = defaultdict(list)
    for player in bootstrap.get("elements", []):
        full = _token(f"{player.get('first_name', '')} {player.get('second_name', '')}")
        if full:
            full_names[full] = player
        web = _token(player.get("web_name", ""))
        if web:
            web_names[web].append(player)

    generated = _parse_time(generated_at)
    enriched = []
    for transfer in transfers:
        row = dict(transfer)
        direction = _direction(row.get("movement_type"))
        match = full_names.get(_token(row.get("player")))
        if match is None:
            candidates = web_names.get(_token(row.get("player")), [])
            match = candidates[0] if len(candidates) == 1 else None

        if match is not None:
            relevance = "high"
        elif direction == "in":
            relevance = "medium"
        else:
            relevance = "low"

        announced = _parse_time(row["announced_at"])
        age_days = max(0, (generated.date() - announced.date()).days)
        freshness = "new_7d" if age_days <= 7 else "recent_14d" if age_days <= 14 else "older"
        club = row.get("premier_league_club")
        if not club:
            club = row.get("to_club") if direction == "in" else row.get("from_club")

        row.update(
            {
                "movement_direction": direction,
                "fpl_relevance": relevance,
                "freshness": freshness,
                "age_days": age_days,
                "premier_league_club": club,
                "matched_fpl_element_id": match.get("id") if match else None,
                "fpl_reconciliation_status": (
                    "matched_current_fpl"
                    if match
                    else row.get("fpl_reconciliation_status", "pending_new_season_fpl")
                ),
            }
        )
        enriched.append(row)
    return enriched


def summarize_clubs(transfers):
    """Create compact, sortable summaries for Premier League clubs."""
    clubs = {}
    for transfer in transfers:
        club = transfer.get("premier_league_club") or "Unknown"
        summary = clubs.setdefault(
            club,
            {"club": club, "arrivals": 0, "departures": 0, "relevant_moves": 0, "latest_at": ""},
        )
        if transfer.get("movement_direction") == "in":
            summary["arrivals"] += 1
        elif transfer.get("movement_direction") in {"out", "released"}:
            summary["departures"] += 1
        if transfer.get("fpl_relevance") in {"high", "medium"}:
            summary["relevant_moves"] += 1
        summary["latest_at"] = max(summary["latest_at"], transfer.get("announced_at", ""))
    return sorted(clubs.values(), key=lambda item: (-item["relevant_moves"], item["club"]))
