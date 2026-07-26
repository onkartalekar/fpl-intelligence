"""Collect confirmed moves from the official Premier League transfer centre."""

import json
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from .transfers import OFFICIAL_CLUB_DOMAINS, normalize_transfer


API_ROOT = "https://api.premierleague.com/content/premierleague/playlist/en"
MASTER_PLAYLIST_ID = 4658365
_MOVEMENT_TYPES = {
    "transfer-in",
    "transfer-out",
    "loan-in",
    "loan-out",
    "player-released",
    "end-of-loan",
}


def _counterpart(description):
    text = (description or "").strip()
    return text.lstrip("- ").strip() or "Not stated"


def parse_team_playlist(team_name, playlist):
    records = []
    for item in playlist.get("items", []):
        promo = item.get("response") or {}
        tags = {tag.get("label") for tag in promo.get("tags", [])}
        movements = sorted(tags & _MOVEMENT_TYPES)
        if not movements:
            continue
        movement = movements[0]
        links = promo.get("links") or []
        source_url = next((link.get("promoUrl") for link in links if link.get("promoUrl")), None)
        if not source_url:
            continue
        source_domain = (urlparse(source_url).hostname or "").lower()
        if source_domain.startswith("www."):
            source_domain = source_domain[4:]
        if source_domain == "premierleague.com" or source_domain.endswith(".premierleague.com"):
            source_type = "official_premier_league"
            official_club_domain = None
        elif source_domain in OFFICIAL_CLUB_DOMAINS:
            source_type = "official_club"
            official_club_domain = source_domain
        else:
            continue
        other = _counterpart(promo.get("description"))
        if movement in {"transfer-in", "loan-in", "end-of-loan"}:
            from_club, to_club = other, team_name
        elif movement == "player-released":
            from_club, to_club = team_name, other if other != "Not stated" else "Released"
        else:
            from_club, to_club = team_name, other
        records.append(
            normalize_transfer(
                {
                    "player": promo.get("title"),
                    "from_club": from_club,
                    "to_club": to_club,
                    "announced_at": promo.get("date"),
                    "source_url": source_url,
                    "source_type": source_type,
                    "official_club_domain": official_club_domain,
                    "movement_type": movement,
                    "premier_league_club": team_name,
                    "premier_league_item_id": promo.get("id"),
                }
            )
        )
    return records


def _fetch_json(url, timeout=30):
    request = Request(url, headers={"User-Agent": "FPL Intelligence local dashboard"})
    with urlopen(request, timeout=timeout) as response:
        return json.load(response)


def fetch_confirmed_transfers(timeout=30):
    master = _fetch_json(f"{API_ROOT}/{MASTER_PLAYLIST_ID}?detail=DETAILED", timeout)
    records = []
    for item in master.get("items", []):
        team_playlist = item.get("response") or {}
        playlist_id = team_playlist.get("id") or item.get("id")
        title = team_playlist.get("title", "")
        marker = " - Transfer Centre - "
        if not playlist_id or marker not in title:
            continue
        team_name = title.split(marker, 1)[1]
        detailed = _fetch_json(
            f"{API_ROOT}/{playlist_id}?pageSize=100&detail=DETAILED", timeout
        )
        records.extend(parse_team_playlist(team_name, detailed))
    records.sort(key=lambda row: (row.get("announced_at") or "", row["player"]), reverse=True)
    return records
