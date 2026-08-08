"""FPL relevance and club-impact enrichment for confirmed transfers."""

from collections import defaultdict
from datetime import datetime
import re
import unicodedata


# Some Latin letters have no NFKD decomposition into "base letter + combining
# diacritic" -- they're distinct letterforms, not accented variants of an
# ASCII letter -- so NFKD-then-ascii-encode silently drops them instead of
# transliterating them (e.g. "Nørgaard" loses the "o" entirely and becomes
# "Nrgaard", not "Norgaard"). Substitute these first so the rest of the
# pipeline sees a plausible ASCII form, matching how press/transfer-feed
# reporting typically renders them.
_NON_DECOMPOSABLE_LETTERS = str.maketrans({
    "ø": "o", "Ø": "O",
    "æ": "ae", "Æ": "AE",
    "œ": "oe", "Œ": "OE",
    "ð": "d", "Ð": "D",
    "þ": "th", "Þ": "Th",
    "đ": "d", "Đ": "D",
    "ł": "l", "Ł": "L",
    "ß": "ss",
})


def _token(value):
    text = (value or "").translate(_NON_DECOMPOSABLE_LETTERS)
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
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
    # Press/transfer-feed reporting commonly uses only a player's first
    # surname (e.g. "Bruno Guimaraes"), while FPL's own record often carries
    # additional family/maternal surnames (e.g. "Guimarães Rodriguez
    # Moura") -- neither the full-name nor the web_name strategy above
    # matches that shorter, still-unambiguous form, so index it too.
    primary_surname_names = defaultdict(list)
    for player in bootstrap.get("elements", []):
        first = player.get("first_name", "")
        second = player.get("second_name", "")
        full = _token(f"{first} {second}")
        if full:
            full_names[full] = player
        web = _token(player.get("web_name", ""))
        if web:
            web_names[web].append(player)
        primary_surname = second.split()[0] if second.split() else ""
        if primary_surname and primary_surname != second:
            short = _token(f"{first} {primary_surname}")
            if short:
                primary_surname_names[short].append(player)

    generated = _parse_time(generated_at)
    enriched = []
    for transfer in transfers:
        row = dict(transfer)
        direction = _direction(row.get("movement_type"))
        player_token = _token(row.get("player"))
        match = full_names.get(player_token)
        if match is None:
            candidates = web_names.get(player_token, [])
            match = candidates[0] if len(candidates) == 1 else None
        if match is None:
            candidates = primary_surname_names.get(player_token, [])
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
