#!/usr/bin/env python3
"""Benchmark build_transfer_decisions/build_draft_decisions at realistic player-pool scale.

Issue #176: every number produced while investigating and fixing that issue (including PR #177's
"~45% faster" headline) came from tests/test_transfer_decisions.py's 28-player unit-test fixture --
far smaller than a real ~570-600 player FPL pool. Checking against real local data
(data/fpl-bootstrap-latest.json, gitignored, live-fetched) turned up a materially different,
larger absolute cost (14.05s before #177, 8.77s after -- vs. 4.79s/2.65s on the tiny fixture) and
showed the underlying claim that this cost is "not data-volume-driven" was wrong.

That real data isn't committed (gitignored, and only ever exists locally after a live refresh), so
it can't be the basis of a repeatable benchmark -- CI and a fresh clone would have nothing to run
against. This script generates a synthetic pool instead, sized and shaped to match real FPL's
actual distribution (573 players: 64 GKP / 187 DEF / 253 MID / 69 FWD across 20 teams, per a real
bootstrap snapshot inspected directly), so it runs identically anywhere -- no live data, no
network, no dependency on what happens to be cached locally.

Deterministic (formula-based player stats, no randomness) so re-runs are comparable to each other
and to numbers quoted in issue writeups. Stdlib-only, matching this repo's dependency policy.
"""

from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fpl_intel.recommendations import build_gw_recommendations
from fpl_intel.transfer_decisions import build_draft_decisions, build_transfer_decisions

# Real FPL's actual position split, inspected directly from a real bootstrap snapshot (573
# players) -- not an arbitrary round number, so sort/candidate-list costs that scale with
# per-position pool size are realistic too, not just the total player count.
_POSITION_COUNTS = {1: 64, 2: 187, 3: 253, 4: 69}  # GKP, DEF, MID, FWD
_TEAM_COUNT = 20


def _build_bootstrap():
    teams = [
        {"id": index, "name": f"Club {index}", "short_name": f"C{index}"}
        for index in range(1, _TEAM_COUNT + 1)
    ]
    element_types = [
        {"id": 1, "singular_name": "Goalkeeper", "singular_name_short": "GKP", "squad_select": 2, "squad_min_play": 1, "squad_max_play": 1},
        {"id": 2, "singular_name": "Defender", "singular_name_short": "DEF", "squad_select": 5, "squad_min_play": 3, "squad_max_play": 5},
        {"id": 3, "singular_name": "Midfielder", "singular_name_short": "MID", "squad_select": 5, "squad_min_play": 2, "squad_max_play": 5},
        {"id": 4, "singular_name": "Forward", "singular_name_short": "FWD", "squad_select": 3, "squad_min_play": 1, "squad_max_play": 3},
    ]
    players = []
    player_id = 1
    for position, count in _POSITION_COUNTS.items():
        for index in range(count):
            players.append({
                "id": player_id,
                "web_name": f"P{player_id}",
                "first_name": "Player",
                "second_name": str(player_id),
                "team": (index % _TEAM_COUNT) + 1,
                "element_type": position,
                "now_cost": 40 + position * 5 + (index % 8) * 3,
                "minutes": 1800 + (index % 20) * 60,
                "starts": 15 + (index % 20),
                "total_points": 30 + (index % 40) * 3,
                "ep_next": str(1.5 + (index % 6) * 0.5),
                "status": "a",
                "news": "",
                "selected_by_percent": str(1.0 + (index % 30)),
                "can_select": True,
                "removed": False,
            })
            player_id += 1
    return {
        "events": [
            {
                "id": event, "name": f"Gameweek {event}",
                "deadline_time": f"2026-09-{event:02d}T17:30:00Z",
                "finished": event == 1, "is_current": event == 1, "is_next": event == 2,
            }
            for event in range(1, 7)
        ],
        "teams": teams,
        "element_types": element_types,
        "elements": players,
        "game_settings": {
            "squad_squadsize": 15, "squad_team_limit": 3, "squad_total_spend": 1000,
            "max_extra_free_transfers": 4, "transfers_cap": 20,
        },
        "chips": [
            {"id": 1, "name": "wildcard", "number": 1, "start_event": 2, "stop_event": 19, "chip_type": "transfer"},
            {"id": 2, "name": "freehit", "number": 1, "start_event": 2, "stop_event": 19, "chip_type": "transfer"},
            {"id": 3, "name": "bboost", "number": 1, "start_event": 1, "stop_event": 19, "chip_type": "team"},
            {"id": 4, "name": "3xc", "number": 1, "start_event": 1, "stop_event": 19, "chip_type": "team"},
        ],
    }


def _build_fixtures():
    fixtures = []
    fixture_id = 1
    pairings = [(team, team % _TEAM_COUNT + 1) for team in range(1, _TEAM_COUNT + 1, 2)]
    for event in range(1, 7):
        for home, away in pairings:
            fixtures.append({
                "id": fixture_id, "event": event, "team_h": home, "team_a": away,
                "team_h_difficulty": 2 + (event % 3), "team_a_difficulty": 4 - (event % 3),
            })
            fixture_id += 1
    return fixtures


def _gw2_manager_inputs():
    """A legal GW2 bootstrap/fixtures/manager triple, sized like real FPL, built the same way
    tests/test_transfer_decisions.py's gw2_inputs() does at unit-test scale."""
    bootstrap = _build_bootstrap()
    fixtures = _build_fixtures()
    opening = build_gw_recommendations(bootstrap, fixtures, "2026-08-29T12:00:00-04:00")
    squad = [
        {
            "element_id": player["id"],
            "purchase_price": int(round(player["price"] * 10)),
            "selling_price": int(round(player["price"] * 10)),
            "position": index + 1,
        }
        for index, player in enumerate(opening["recommended_squad"]["players"])
    ]
    manager = {
        "current_event": 1, "bank": 0, "squad_publicly_available": True,
        "squad": squad, "chips_used": [], "public_transfers": [],
    }
    return bootstrap, fixtures, manager


def _draft_inputs():
    """A legal preseason (event 1) bootstrap/fixtures pair plus a legal 15-player draft."""
    bootstrap = _build_bootstrap()
    fixtures = _build_fixtures()
    opening = build_gw_recommendations(bootstrap, fixtures, "2026-07-01T12:00:00-04:00")
    draft_squad_ids = [player["id"] for player in opening["recommended_squad"]["players"]]
    return bootstrap, fixtures, draft_squad_ids


def _time(label, func):
    start = time.perf_counter()
    result = func()
    elapsed = time.perf_counter() - start
    status = result.get("status", "?")
    print(f"  {label:30s} {elapsed:7.3f}s  (status={status})")
    return elapsed


def main():
    if sys.version_info[:2] < (3, 10):
        print(
            f"WARNING: running under Python {sys.version_info.major}.{sys.version_info.minor}. "
            "This repo's CI/deploy target 3.11+; numbers from a slower interpreter aren't "
            "comparable to numbers quoted elsewhere (e.g. issue #176's), and a future run under "
            "3.11+ could look like a regression that's really just a different interpreter. "
            "Re-run with a 3.11+ interpreter (see [[run-full-tests]]) before comparing.\n"
        )

    total_players = sum(_POSITION_COUNTS.values())
    print(f"Synthetic pool: {total_players} players across {_TEAM_COUNT} teams (matches real FPL's actual position split)\n")

    bootstrap, fixtures, manager = _gw2_manager_inputs()
    print("build_transfer_decisions (GW2, real published squad):")
    _time(
        "build_transfer_decisions",
        lambda: build_transfer_decisions(bootstrap, fixtures, manager, generated_at="2026-09-02T12:00:00Z"),
    )

    print("\nbuild_draft_decisions (preseason, declared draft squad):")
    draft_bootstrap, draft_fixtures, draft_squad_ids = _draft_inputs()
    _time(
        "build_draft_decisions",
        lambda: build_draft_decisions(
            draft_bootstrap, draft_fixtures, draft_squad_ids, generated_at="2026-07-05T12:00:00Z"
        ),
    )
    print(
        "\nFor comparison, issue #176's numbers on tests/test_transfer_decisions.py's 28-player "
        "fixture: 4.79s before PR #177's memoization fix, 2.65s after."
    )


if __name__ == "__main__":
    main()
