"""FPL relevance and club-impact enrichment for confirmed transfers."""

from collections import defaultdict
from datetime import datetime
import re
import unicodedata

from .transfers import canonical_club


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
    # Different clubs' own transfer-centre write-ups don't always spell an
    # opposing club's name the same way (e.g. "Brighton" vs. "Brighton &
    # Hove Albion") -- normalize from_club/to_club to the bootstrap feed's
    # own team name whenever they resolve to a real current club, so every
    # downstream consumer (summarize_clubs, the dashboard's club filter)
    # sees one consistent name per club instead of splitting the same club
    # across multiple spellings.
    #
    # Issue #232: premier_league_club gets the same treatment. It carries the
    # PL transfer-centre playlist's own title ("Tottenham Hotspur", "AFC
    # Bournemouth", "Nottingham Forest"), which is a third vocabulary again --
    # while the dashboard's club dropdown is built from summarize_clubs, i.e.
    # from these already-normalized from_club/to_club names ("Spurs",
    # "Bournemouth", "Nott'm Forest"). Six clubs' dropdown values therefore
    # matched no premier_league_club at all, so that arm of the filter's club
    # predicate silently contributed nothing for them. It cost no rows only
    # because those records happen to carry the short name on from_club or
    # to_club as well -- luck, not design.
    team_name_by_canonical = {
        canonical_club(team.get("name")): team.get("name")
        for team in bootstrap.get("teams", [])
        if team.get("name")
    }
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
        for side in ("from_club", "to_club", "premier_league_club"):
            row[side] = team_name_by_canonical.get(canonical_club(row.get(side)), row.get(side))
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


def summarize_clubs(transfers, bootstrap):
    """Create compact, sortable summaries for Premier League clubs.

    Counts a move's arrival against its destination club and its departure
    against its origin club independently, using from_club/to_club rather
    than the single premier_league_club/movement_direction pairing a move
    happens to have kept after refresh.py's cross-source dedup. A move
    reported by both the selling and buying club's own transfer-centre
    feeds is merged into one record there, and that merge keeps only one
    side's attribution (whichever raw record was processed last) -- so
    relying on it here would silently undercount the club that lost the
    attribution (see #35). from_club/to_club survive the merge on every
    record regardless of which side "won," so both clubs get credit.
    """
    club_names = {
        canonical_club(team.get("name")): team.get("id")
        for team in bootstrap.get("teams", [])
        if team.get("name")
    }
    clubs = {}

    def _entry(club):
        return clubs.setdefault(
            club, {"club": club, "arrivals": 0, "departures": 0, "relevant_moves": 0, "latest_at": ""}
        )

    for transfer in transfers:
        relevant = transfer.get("fpl_relevance") in {"high", "medium"}
        announced_at = transfer.get("announced_at", "")
        from_club = transfer.get("from_club")
        to_club = transfer.get("to_club")
        touched_a_pl_club = False
        if from_club and canonical_club(from_club) in club_names:
            entry = _entry(from_club)
            entry["departures"] += 1
            entry["relevant_moves"] += relevant
            entry["latest_at"] = max(entry["latest_at"], announced_at)
            touched_a_pl_club = True
        if to_club and canonical_club(to_club) in club_names:
            entry = _entry(to_club)
            entry["arrivals"] += 1
            entry["relevant_moves"] += relevant
            entry["latest_at"] = max(entry["latest_at"], announced_at)
            touched_a_pl_club = True
        if not touched_a_pl_club:
            # Neither side resolved to a current PL club (e.g. a lower-
            # league loan move) -- fall back to the reporting club so the
            # move still shows up somewhere rather than being dropped.
            entry = _entry(transfer.get("premier_league_club") or "Unknown")
            entry["relevant_moves"] += relevant
            entry["latest_at"] = max(entry["latest_at"], announced_at)

    return sorted(clubs.values(), key=lambda item: (-item["relevant_moves"], item["club"]))
