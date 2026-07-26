"""Normalize official FPL players and fixtures for dashboard use."""


def _number(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def build_player_catalog(bootstrap):
    """Return compact, display-ready player records from bootstrap-static."""
    team_by_id = {team.get("id"): team for team in bootstrap.get("teams", [])}
    position_by_id = {
        position.get("id"): position for position in bootstrap.get("element_types", [])
    }
    players = []
    for player in bootstrap.get("elements", []):
        team = team_by_id.get(player.get("team"), {})
        position = position_by_id.get(player.get("element_type"), {})
        players.append(
            {
                "id": player.get("id"),
                "name": player.get("web_name"),
                "full_name": " ".join(
                    part for part in [player.get("first_name"), player.get("second_name")] if part
                ),
                "club": team.get("name"),
                "club_short": team.get("short_name"),
                "position": position.get("singular_name"),
                "position_short": position.get("singular_name_short"),
                "price": _number(player.get("now_cost")) / 10,
                "ownership": _number(player.get("selected_by_percent")),
                "status": player.get("status"),
                "news": player.get("news") or "",
                "form": _number(player.get("form")),
                "total_points": player.get("total_points") or 0,
                "minutes": player.get("minutes") or 0,
                "starts": player.get("starts") or 0,
            }
        )
    return players


def build_fixture_catalog(fixtures, bootstrap):
    """Return compact fixtures with official club names and difficulty ratings."""
    team_by_id = {team.get("id"): team for team in bootstrap.get("teams", [])}
    catalog = []
    for fixture in fixtures:
        home = team_by_id.get(fixture.get("team_h"), {})
        away = team_by_id.get(fixture.get("team_a"), {})
        catalog.append(
            {
                "id": fixture.get("id"),
                "event": fixture.get("event"),
                "kickoff_time": fixture.get("kickoff_time"),
                "home_team_id": fixture.get("team_h"),
                "home_team": home.get("name"),
                "home_short": home.get("short_name"),
                "away_team_id": fixture.get("team_a"),
                "away_team": away.get("name"),
                "away_short": away.get("short_name"),
                "home_difficulty": fixture.get("team_h_difficulty"),
                "away_difficulty": fixture.get("team_a_difficulty"),
                "finished": bool(fixture.get("finished")),
                "started": bool(fixture.get("started")),
                "home_score": fixture.get("team_h_score"),
                "away_score": fixture.get("team_a_score"),
            }
        )
    return catalog
