"""Rolling FPL transfer, roll, and chip decision scenarios."""

from collections import Counter

from .recommendations import (
    _POSITION_CODES,
    _PROFILE_DEFINITIONS,
    _best_xi,
    _event_lineup_schedule,
    _next_event_id,
    _number,
    _optimize_squad,
    _profile_metrics_for_squad,
    _profile_player_score,
    _squad_objective,
    project_players,
)


_RULES_URL = "https://fantasy.premierleague.com/en/help/rules"
_CHIP_LABELS = {
    "wildcard": "Wildcard",
    "freehit": "Free Hit",
    "bboost": "Bench Boost",
    "3xc": "Triple Captain",
}
_CHIP_RULES = {
    "wildcard": "Unlimited permanent transfers without the usual four-point charge for each excess transfer.",
    "freehit": "Unlimited free transfers for one Gameweek; the squad then returns to its prior state. It cannot be played in consecutive Gameweeks.",
    "bboost": "Points scored by all benched players are added to the Gameweek total.",
    "3xc": "The captain scores triple rather than double points for the Gameweek.",
}
# Issue #184: wildcard/freehit compare _central_points on each profile's own scale (issue
# #181's fix made that scale profile-adjusted, where it used to be one shared plain scale).
# balanced's scale never moved -- recommendations.py builds profile_fixture_xp["balanced"]
# from the exact same array as plain fixture_xp -- so its two entries are unchanged from
# before #181. conservative and aggressive did move (confirmed on real squads across a
# downgrade-severity sweep, see plans/issue-184-chip-threshold-recalibration.md): conservative
# freehit's marginal value now runs deeply negative even for an already-strong squad and only
# creeps toward zero as the squad gets much worse, so its threshold has to sit well below zero
# to ever be reachable without being reachable *always*; aggressive wildcard/freehit's marginal
# values now run high even for an already-strong squad, so their thresholds have to rise to
# avoid firing on every squad regardless of quality. These four numbers are a re-tuned
# heuristic, not a backtested one -- the same epistemic status the original constants already
# had (there is no historical decision-level backtest harness in this codebase to validate
# against; see the plan doc). bboost/3xc are untouched: they read _profile_player_score/xp_1,
# a separate code path #181 never changed.
_THRESHOLDS = {
    "conservative": {"wildcard": 22.0, "freehit": -30.0, "bboost": 18.0, "3xc": 9.0},
    "balanced": {"wildcard": 18.0, "freehit": 15.0, "bboost": 16.0, "3xc": 8.0},
    "aggressive": {"wildcard": 20.0, "freehit": 65.0, "bboost": 14.0, "3xc": 7.0},
}
# Issue #256 (part A): _THRESHOLDS above has no notion of season stage -- a chip clears its bar
# in Gameweek 2 on exactly the same terms it would in Gameweek 30, even though playing a chip
# early forecloses using it at a possibly better spot later in the season. Confirmed live on a
# real team (364759, GW2, balanced profile): Wildcard cleared its threshold by only +15.9 and
# Free Hit by only +0.4 -- both barely-qualifying, one gameweek into a 38-gameweek season.
#
# Issue #267 superseded #256's original flat, whole-season cutoff (a single event vs. a shared
# GW10 boundary) with two combined signals, each scoped to the chip candidate's *own* remaining
# half-season window (start_event/stop_event, already computed by _chip_inventory) rather than
# the season as a whole -- #256's flat cutoff had a real, previously-unnoticed gap: it went to
# zero extra caution from GW10 onward even when a chip's own window (e.g. Wildcard/Free Hit's
# first half, GW2-19) still had 9 more gameweeks of runway, and it never reapplied any caution
# when a chip's *second* half-season window opened at GW20 -- structurally identical to a fresh
# season start for that chip, but invisible to a cutoff that only ever measured distance from the
# real season's own GW1. See plans/issue-267-chip-scarcity.md for the full investigation.
_EARLY_SEASON_MAX_EXTRA_MULTIPLIER = 1.0  # threshold's magnitude effectively doubles at max caution


def _chip_window_extra_caution(event, start_event, stop_event):
    """Candidate (2): per-chip-window scarcity. Returns an extra-caution fraction (0..
    _EARLY_SEASON_MAX_EXTRA_MULTIPLIER) based on how much of *this chip's own* remaining
    half-season window is left -- maximum caution at the window's first gameweek, converging
    linearly to exactly zero at its last, and resetting to maximum the moment a new window opens
    (e.g. Wildcard/Free Hit's second half starting at GW20), which a single whole-season cutoff
    can never do. Verified against real per-team data in plans/issue-267-chip-scarcity.md
    (candidate 2's worked table)."""
    if stop_event <= start_event:
        return 0.0
    window_fraction = (event - start_event) / (stop_event - start_event)
    window_fraction = min(1.0, max(0.0, window_fraction))
    return _EARLY_SEASON_MAX_EXTRA_MULTIPLIER * (1.0 - window_fraction)


# Issue #267 (candidate 1b): historically-grounded double/blank-gameweek prior, mined directly
# from data/history/{2022-23,2023-24,2024-25,2025-26}/fixtures.csv -- for each gameweek, the
# fraction of those 4 seasons with a major fixture-congestion double gameweek (2+ fixtures for a
# team) or blank gameweek (0 fixtures for a team). One clear one-off anomaly excluded: 2022-23
# GW7 was a full-round postponement for Queen Elizabeth II's death, not fixture congestion.
# Gameweeks absent from this table had no such event in any of the 4 seasons (weight 0, not
# "unknown") -- the pattern is clean and consistent across all 4 seasons: real fixture-driven
# doubles/blanks essentially never occur before GW19-20 and concentrate heavily from GW25 through
# GW37. Not backtest-validated -- same epistemic status as _THRESHOLDS itself (issue #184's own
# comment above): a reasoned heuristic grounded in real historical data, not a fitted parameter.
# See plans/issue-267-chip-scarcity.md for the full derivation.
_HISTORICAL_DGW_BGW_WEIGHTS = {
    2: 0.25, 7: 0.25, 8: 0.25, 12: 0.25, 15: 0.25, 17: 0.25, 18: 0.25, 19: 0.25,
    20: 0.25, 22: 0.25, 23: 0.25, 24: 0.25, 25: 1.0, 26: 0.5, 27: 0.25, 28: 0.5,
    29: 0.75, 31: 0.25, 32: 0.5, 33: 0.5, 34: 1.0, 35: 0.25, 36: 0.5, 37: 0.5,
}


def _remaining_historical_weight(event, stop_event):
    """Sum of _HISTORICAL_DGW_BGW_WEIGHTS strictly after `event` through `stop_event` -- the
    historical double/blank-gameweek opportunity a chip would still be able to catch if held past
    `event` through the rest of its own window."""
    return sum(weight for gw, weight in _HISTORICAL_DGW_BGW_WEIGHTS.items() if event < gw <= stop_event)


# The largest remaining-weight any real chip window can ever show, at its own opening -- Wildcard/
# Free Hit's second half [20, 38] measured from event=20 itself (matching the shape of
# plans/issue-267-chip-scarcity.md's own worked table, its "largest such sum ever observed across
# a real window" normalizer). Computed from the table above rather than hand-copied, so it always
# stays consistent with it. Used to normalize _remaining_historical_weight onto the same 0..1
# scale _chip_window_extra_caution already uses, so the two "extra caution" signals can be
# combined via max() without one silently dominating the other by scale alone.
_HISTORICAL_MAX_REMAINING_WEIGHT = _remaining_historical_weight(20, 38)


def _historical_opportunity_extra_caution(event, stop_event):
    """Candidate (1b): historically-grounded double/blank-gameweek prior. Returns an extra-caution
    fraction (0.._EARLY_SEASON_MAX_EXTRA_MULTIPLIER) proportional to how much real historical
    DGW/BGW opportunity (_HISTORICAL_DGW_BGW_WEIGHTS) still lies ahead in this chip's own
    remaining window -- unlike _chip_window_extra_caution, this doesn't treat every gameweek of a
    window as equally worth waiting for: a window whose remaining weeks include the GW25-37
    fixture-congestion cluster produces much stronger caution than one that doesn't, even at the
    same window-fraction-remaining. Verified against real per-team data in
    plans/issue-267-chip-scarcity.md (candidate 1b's worked table)."""
    if _HISTORICAL_MAX_REMAINING_WEIGHT <= 0:
        return 0.0
    remaining = _remaining_historical_weight(event, stop_event)
    fraction = min(1.0, remaining / _HISTORICAL_MAX_REMAINING_WEIGHT)
    return _EARLY_SEASON_MAX_EXTRA_MULTIPLIER * fraction


def _chip_scarcity_extra_caution(event, start_event, stop_event):
    """Combines candidates (2) and (1b) via max(), per plans/issue-267-chip-scarcity.md's
    recommendation, rather than summing them -- both are two different views of the same "is it
    wise to wait" question (one from calendar position within the window, one from historical
    fixture-congestion opportunity within it), so summing would double-count agreement between
    them rather than taking the stronger of two independent cautions."""
    return max(
        _chip_window_extra_caution(event, start_event, stop_event),
        _historical_opportunity_extra_caution(event, stop_event),
    )


def _season_stage_effective_threshold(threshold, event, start_event, stop_event):
    """Raises `threshold`'s magnitude the more of this chip's own remaining window's worth of
    scarcity (calendar position and/or real historical DGW/BGW opportunity) still lies ahead,
    always in the direction that makes it *harder* to clear regardless of the base threshold's
    sign.

    A plain `threshold * multiplier` breaks for a negative threshold (conservative's freehit is
    -30.0): multiplying a negative number by something > 1 makes it *more* negative, which is a
    *lower*, easier-to-clear bar -- the opposite of what an early-season penalty should do. Scaling
    by the threshold's own magnitude and adding it back (rather than multiplying the signed value)
    keeps the adjustment in the harder-to-clear direction for both a positive threshold (moves
    further above zero) and a negative one (moves toward zero, e.g. -30.0 -> -3.3 at max caution)
    -- confirmed against both cases in plans/issue-256-chip-timing.md. Converges to exactly
    `threshold` at a window's own last gameweek (both extra-caution signals are 0 there), and
    never exceeds `threshold * 2` in magnitude (both signals are individually capped at
    _EARLY_SEASON_MAX_EXTRA_MULTIPLIER, and max() of two such values is too)."""
    extra = _chip_scarcity_extra_caution(event, start_event, stop_event)
    return threshold + abs(threshold) * extra


# Issue #278: the ordinary multi-transfer path has no equivalent of the above caution -- paid
# transfers regenerate weekly and aren't a scarce resource with their own half-season window the
# way a chip is, so #267's _chip_scarcity_extra_caution doesn't translate directly. The actual risk
# here is different: paying real, permanent points to act on a signal that's still noisy because
# not enough of *this season* has been observed yet. Tied to the same constant that's the real
# source of that noise (projection.py's MID/FWD residual_reliability_denominator=100/cap=0.82,
# not re-imported here to avoid coupling this margin's shape to that module's private constants,
# but matched deliberately) rather than an unrelated arbitrary cutoff -- see
# plans/issue-278-multi-transfer-caution.md for the full derivation and real-data verification.
_MULTI_TRANSFER_EARLY_SEASON_MARGIN = 10.0  # required net_gain_5gw margin at max caution
_MULTI_TRANSFER_MARGIN_RELIABILITY_DENOMINATOR = 100.0  # matches projection.py's MID/FWD value
_MULTI_TRANSFER_MARGIN_RELIABILITY_CAP = 0.82  # matches projection.py's residual_reliability_cap


def _multi_transfer_required_margin(event):
    """How much a 3+-leg multi-transfer override must beat the planner's own roll/single/double
    pick by (`transfer_decisions.py`'s own `best_multi_leg` comparison) before it's accepted,
    early in the season -- 0 once enough of the season has been observed for the underlying
    per-player signal to have stabilized (see this module's docstring above the constants for
    why this decays at the same rate as MID/FWD's own residual-reliability system, rather than an
    unrelated cutoff). One full match per completed gameweek is assumed as the observed-minutes
    estimate; transfers are never recommended before event 2 (see `build_transfer_decisions`), so
    `event - 1` is always >= 1 wherever this is actually called."""
    observed_minutes = max(0, event - 1) * 90
    reliability = (
        min(_MULTI_TRANSFER_MARGIN_RELIABILITY_CAP, observed_minutes / (observed_minutes + _MULTI_TRANSFER_MARGIN_RELIABILITY_DENOMINATOR))
        if observed_minutes
        else 0.0
    )
    extra_caution = 1.0 - reliability / _MULTI_TRANSFER_MARGIN_RELIABILITY_CAP
    return _MULTI_TRANSFER_EARLY_SEASON_MARGIN * extra_caution


def derive_free_transfers(next_event, transfers, chips_used, maximum=5):
    """Infer free transfers at the next deadline from published history.

    Public FPL does not expose unpublished in-Gameweek moves. This therefore
    reconstructs the balance at the latest published deadline only.
    """
    next_event = int(next_event or 1)
    if next_event <= 1:
        return 0
    transfers_by_event = Counter(int(row.get("event") or 0) for row in transfers or [])
    chip_by_event = {
        int(row.get("event") or 0): row.get("name")
        for row in chips_used or []
    }
    available = 1
    for event in range(2, next_event):
        used = transfers_by_event.get(event, 0)
        if chip_by_event.get(event) in {"wildcard", "freehit"}:
            used = 0
        available = min(maximum, max(0, available - used) + 1)
    return available


def _quotas(bootstrap):
    return {
        _POSITION_CODES[int(item["id"])]: int(item.get("squad_select") or 0)
        for item in bootstrap.get("element_types", [])
        if int(item.get("id") or 0) in _POSITION_CODES
    }


def _structure_legal(squad, quotas, club_limit):
    if len(squad) != sum(quotas.values()) or len({row["id"] for row in squad}) != len(squad):
        return False
    positions = Counter(row["position_short"] for row in squad)
    if any(positions.get(position, 0) != count for position, count in quotas.items()):
        return False
    clubs = Counter(row["club"] for row in squad)
    return not clubs or max(clubs.values()) <= club_limit


def _lineup_view(squad, profile):
    score = lambda player: _profile_player_score(player, profile, 1)
    lineup, formation = _best_xi(squad, score)
    lineup_ids = {row["id"] for row in lineup}
    outfield = sorted(
        (row for row in squad if row["id"] not in lineup_ids and row["position_short"] != "GKP"),
        key=score,
        reverse=True,
    )
    goalkeepers = sorted(
        (row for row in squad if row["id"] not in lineup_ids and row["position_short"] == "GKP"),
        key=score,
        reverse=True,
    )
    bench = outfield + goalkeepers
    captaincy = sorted(lineup, key=score, reverse=True)
    captain, vice_captain = captaincy[:2]
    event_points = sum(row["xp_1"] for row in lineup) + captain["xp_1"]
    return {
        "starting_xi": lineup,
        "formation": formation,
        "bench": bench,
        "captain": captain,
        "vice_captain": vice_captain,
        "projected_event_points_including_captain": round(event_points, 1),
    }


def _central_points(squad, horizon, profile="balanced", cache=None):
    return sum(
        row["central_points"] for row in _event_lineup_schedule(squad, profile, horizon, cache=cache)
    )


def _public_squad(manager, projection_by_id):
    squad = []
    for pick in manager.get("squad", []):
        player = projection_by_id.get(pick.get("element_id"))
        if not player:
            continue
        row = dict(player)
        row["purchase_price"] = _number(pick.get("purchase_price"), player["price"] * 10) / 10
        row["selling_price"] = _number(pick.get("selling_price"), player["price"] * 10) / 10
        squad.append(row)
    return squad


def _draft_squad(draft_squad_ids, projection_by_id):
    """Build the enriched squad rows for a self-declared draft (issue #61).

    A draft-adapted `_public_squad`: a declared draft has no purchase history from FPL's API
    (nothing has actually been bought), so both `purchase_price` and `selling_price` are set to
    each player's *current* price -- there is no profit/loss to account for yet.
    """
    squad = []
    for player_id in draft_squad_ids:
        player = projection_by_id.get(player_id)
        if not player:
            continue
        row = dict(player)
        row["purchase_price"] = player["price"]
        row["selling_price"] = player["price"]
        squad.append(row)
    return squad


def validate_draft_squad(bootstrap, draft_squad_ids):
    """Validate a self-declared draft squad's element IDs against real, current FPL data.

    Raises ValueError with a user-facing message if the squad isn't a legal FPL squad (wrong
    player count, duplicate or unknown players, formation/club-limit violation, or over budget)
    -- the same rules a real FPL squad must satisfy. Deliberately cheap: reads raw bootstrap
    elements directly rather than running full projections, since this only needs to check
    legality, not model points (`build_draft_decisions` does that separately, only once the
    squad is already known to be legal).
    """
    quotas = _quotas(bootstrap)
    total = sum(quotas.values())
    if not isinstance(draft_squad_ids, list) or len(draft_squad_ids) != total:
        raise ValueError(f"A draft squad needs exactly {total} players")
    if len(set(draft_squad_ids)) != total:
        raise ValueError("A draft squad cannot include the same player twice")
    elements_by_id = {
        int(row["id"]): row for row in bootstrap.get("elements", []) if row.get("id") is not None
    }
    missing = [player_id for player_id in draft_squad_ids if player_id not in elements_by_id]
    if missing:
        raise ValueError("One or more selected players are not in the current player catalog")
    rows = []
    for player_id in draft_squad_ids:
        element = elements_by_id[player_id]
        position = _POSITION_CODES.get(int(element.get("element_type") or 0))
        if position is None:
            raise ValueError("One or more selected players have an unrecognized position")
        rows.append({
            "position_short": position,
            "club": element.get("team"),
            "price": _number(element.get("now_cost")) / 10,
        })
    positions = Counter(row["position_short"] for row in rows)
    if any(positions.get(position, 0) != count for position, count in quotas.items()):
        quota_text = ", ".join(f"{count} {position}" for position, count in quotas.items())
        raise ValueError(f"A draft squad needs exactly {quota_text}")
    settings = bootstrap.get("game_settings", {})
    club_limit = int(settings.get("squad_team_limit") or 3)
    clubs = Counter(row["club"] for row in rows)
    if clubs and max(clubs.values()) > club_limit:
        raise ValueError(f"No more than {club_limit} players from the same club are allowed")
    budget = _number(settings.get("squad_total_spend"), 1000) / 10
    spend = sum(row["price"] for row in rows)
    if spend > budget + 1e-9:
        raise ValueError(f"Draft squad costs £{spend:.1f}m, over the £{budget:.1f}m budget")


def _move_record(out_player, in_player):
    return {
        "out": {"id": out_player["id"], "name": out_player["name"], "club": out_player["club"], "selling_price": out_player["selling_price"]},
        "in": {"id": in_player["id"], "name": in_player["name"], "club": in_player["club"], "price": in_player["price"]},
    }


def _candidate_moves(squad, eligible, cash, quotas, club_limit, profile, cache=None):
    candidates_by_position = {
        position: sorted(
            (row for row in eligible if row["position_short"] == position),
            key=lambda row: _profile_player_score(row, profile, 5),
            reverse=True,
        )[:45]
        for position in quotas
    }
    owned_ids = {row["id"] for row in squad}
    moves = []
    for index, outgoing in enumerate(squad):
        for incoming in candidates_by_position[outgoing["position_short"]]:
            if incoming["id"] in owned_ids:
                continue
            remaining = cash + outgoing["selling_price"] - incoming["price"]
            if remaining < -1e-9:
                continue
            proposal = list(squad)
            proposal[index] = {**incoming, "purchase_price": incoming["price"], "selling_price": incoming["price"]}
            if not _structure_legal(proposal, quotas, club_limit):
                continue
            moves.append({
                "squad": proposal,
                "cash": round(remaining, 1),
                "transfers": [_move_record(outgoing, incoming)],
                "out_ids": {outgoing["id"]},
            })
    moves.sort(key=lambda row: _squad_objective(row["squad"], profile, cache=cache), reverse=True)
    return moves


def _best_double(single_moves, eligible, quotas, club_limit, profile, cache=None):
    best = None
    best_score = float("-inf")
    candidates_by_position = {
        position: sorted(
            (row for row in eligible if row["position_short"] == position),
            key=lambda row: _profile_player_score(row, profile, 5),
            reverse=True,
        )[:35]
        for position in quotas
    }
    for first in single_moves[:35]:
        squad = first["squad"]
        owned_ids = {row["id"] for row in squad}
        for index, outgoing in enumerate(squad):
            if outgoing["id"] not in first["out_ids"] and outgoing.get("purchase_price") == outgoing["price"] and any(
                transfer["in"]["id"] == outgoing["id"] for transfer in first["transfers"]
            ):
                continue
            if outgoing["id"] in first["out_ids"]:
                continue
            for incoming in candidates_by_position[outgoing["position_short"]]:
                if incoming["id"] in owned_ids:
                    continue
                remaining = first["cash"] + outgoing["selling_price"] - incoming["price"]
                if remaining < -1e-9:
                    continue
                proposal = list(squad)
                proposal[index] = {**incoming, "purchase_price": incoming["price"], "selling_price": incoming["price"]}
                if not _structure_legal(proposal, quotas, club_limit):
                    continue
                score = _squad_objective(proposal, profile, cache=cache)
                if score > best_score:
                    best_score = score
                    best = {
                        "squad": proposal,
                        "cash": round(remaining, 1),
                        "transfers": first["transfers"] + [_move_record(outgoing, incoming)],
                    }
    return best


def _leg_moves(squad, eligible, cash, quotas, club_limit, profile, sold_ids, bought_ids, cache=None, position_pool_size=35):
    """Issue #181: legal one-player swaps from `squad`, ranked only by the cheap per-player score
    already used to build `_candidate_moves`/`_best_double`'s own candidate pools -- deliberately
    *not* scored with the expensive `_squad_objective` here, unlike `_candidate_moves`. This is the
    per-leg expansion step inside `_beam_multi_transfer`'s beam search, which re-ranks the
    *combined* multi-leg squad once a leg is added -- pre-ranking each individual leg by its own
    squad-level score here would be real work thrown away the moment it's combined with the next
    leg, since the combined squad's own objective is what actually gets compared.

    `sold_ids`/`bought_ids` track transfers already made earlier in the same combination, so this
    never proposes undoing one of them (selling a player just bought, or buying back a player just
    sold) -- the same guard `_best_double` already applies for its one earlier leg, generalized to
    however many legs have accumulated so far.
    """
    candidates_by_position = {
        position: sorted(
            (row for row in eligible if row["position_short"] == position),
            key=lambda row: _profile_player_score(row, profile, 5),
            reverse=True,
        )[:position_pool_size]
        for position in quotas
    }
    owned_ids = {row["id"] for row in squad}
    moves = []
    for index, outgoing in enumerate(squad):
        if outgoing["id"] in bought_ids:
            continue
        for incoming in candidates_by_position[outgoing["position_short"]]:
            if incoming["id"] in owned_ids or incoming["id"] in sold_ids:
                continue
            remaining = cash + outgoing["selling_price"] - incoming["price"]
            if remaining < -1e-9:
                continue
            proposal = list(squad)
            proposal[index] = {**incoming, "purchase_price": incoming["price"], "selling_price": incoming["price"]}
            if not _structure_legal(proposal, quotas, club_limit):
                continue
            moves.append({
                "squad": proposal,
                "cash": round(remaining, 1),
                "transfer": _move_record(outgoing, incoming),
            })
    return moves


def _beam_multi_transfer(squad, eligible, cash, quotas, club_limit, profile, max_legs, beam_width=10, cache=None):
    """Issue #181: beam search over transfer legs, generalizing `_candidate_moves`/`_best_double`'s
    single/double-transfer search to up to `max_legs` transfers within one gameweek. Brute-forcing
    this the way `_best_double` brute-forces 2 legs is computationally infeasible past 2 (see
    plans/issue-181-multi-transfer-search.md: ~9.6M combinations for a 3rd leg alone) -- this keeps
    only the top `beam_width` partial combinations alive at each leg count, expanding all of them
    by one more leg before re-truncating, rather than exploring everything.

    Returns `{leg_count: best_candidate_at_that_leg_count}` for every leg count in `1..max_legs`
    that has at least one legal combination -- deliberately parallel to how `roll`/`single`/`double`
    are each already independently scenario-compared today (via `net_gain_5gw`), just with more
    leg counts available to compare across. Each candidate dict has the same
    `{"squad", "cash", "transfers"}` shape `_best_double` already returns.
    """
    if max_legs < 1:
        return {}
    best_by_leg_count = {}
    beam = []
    for move in _leg_moves(squad, eligible, cash, quotas, club_limit, profile, sold_ids=set(), bought_ids=set(), cache=cache):
        beam.append({
            "squad": move["squad"],
            "cash": move["cash"],
            "transfers": [move["transfer"]],
            "sold_ids": {move["transfer"]["out"]["id"]},
            "bought_ids": {move["transfer"]["in"]["id"]},
        })
    if not beam:
        return {}
    beam.sort(key=lambda row: _squad_objective(row["squad"], profile, cache=cache), reverse=True)
    beam = beam[:beam_width]
    best_by_leg_count[1] = beam[0]
    for leg_count in range(2, max_legs + 1):
        expanded = []
        for candidate in beam:
            for move in _leg_moves(
                candidate["squad"], eligible, candidate["cash"], quotas, club_limit, profile,
                sold_ids=candidate["sold_ids"], bought_ids=candidate["bought_ids"], cache=cache,
            ):
                expanded.append({
                    "squad": move["squad"],
                    "cash": move["cash"],
                    "transfers": candidate["transfers"] + [move["transfer"]],
                    "sold_ids": candidate["sold_ids"] | {move["transfer"]["out"]["id"]},
                    "bought_ids": candidate["bought_ids"] | {move["transfer"]["in"]["id"]},
                })
        if not expanded:
            break
        expanded.sort(key=lambda row: _squad_objective(row["squad"], profile, cache=cache), reverse=True)
        beam = expanded[:beam_width]
        best_by_leg_count[leg_count] = beam[0]
    return best_by_leg_count


def _scenario(action, candidate, baseline, profile, free_transfers, maximum_free_transfers, cache=None):
    squad = candidate["squad"]
    transfer_count = len(candidate["transfers"])
    point_cost = max(0, transfer_count - free_transfers) * 4
    gross_gain = _central_points(squad, 5, profile, cache=cache) - _central_points(baseline, 5, profile, cache=cache)
    lineup = _lineup_view(squad, profile)
    free_next = min(maximum_free_transfers, max(0, free_transfers - transfer_count) + 1)
    return {
        "action": action,
        "transfers": candidate["transfers"],
        "transfer_count": transfer_count,
        "point_cost": point_cost,
        "gross_gain_5gw": round(gross_gain, 1),
        "net_gain_5gw": round(gross_gain - point_cost, 1),
        "bank_after": round(candidate["cash"], 1),
        "free_transfers_next_event": free_next,
        "profile_score": round(_squad_objective(squad, profile, cache=cache), 2),
        "squad": squad,
        **lineup,
    }


def _planner_player_score(player, profile, relative_event, horizon=5, cache=None):
    # Issue #176: a player's score for a given (profile, relative_event, horizon) is pure -- it
    # only reads static per-player projection fields that don't change during a single
    # build_transfer_decisions/build_draft_decisions call -- but the beam planner below calls this
    # for the same players over and over across different candidate branches that share most of
    # their squad. Profiled at ~10.9M calls for one recommendation (28-player test fixture); an
    # optional per-call cache, keyed on player id rather than the unhashable player dict, collapses
    # that down to one real computation per (id, profile, relative_event, horizon) combination.
    # `cache=None` (every call site outside this module's planner, e.g. direct test calls) keeps
    # today's uncached behavior exactly, so this is purely additive.
    cache_key = None
    if cache is not None:
        cache_key = (player["id"], profile, relative_event, horizon)
        cached = cache.get(cache_key)
        if cached is not None:
            return cached
    profile_points = (player.get("profile_fixture_xp") or {}).get(profile)
    if profile_points is not None:
        score = sum(profile_points[relative_event:horizon])
        if profile == "aggressive":
            differential = min(0.3, max(0.0, 20.0 - player.get("ownership", 0.0)) * 0.015)
            scenario_minutes = (player.get("expected_minutes_scenarios") or {}).get(
                "aggressive", player.get("expected_minutes", 0.0)
            )
            minutes_penalty = max(0.0, 50.0 - scenario_minutes) * 0.045
            result = score + differential - minutes_penalty
        else:
            result = score
    else:
        fixture_points = player.get("fixture_xp") or []
        central = sum(fixture_points[relative_event:horizon])
        uncertainty = {"high": 0.16, "medium": 0.25, "low": 0.38}.get(player.get("confidence"), 0.38)
        if profile == "conservative":
            result = central * (1 - uncertainty)
        elif profile == "aggressive":
            differential = min(0.3, max(0.0, 20.0 - player.get("ownership", 0.0)) * 0.015)
            minutes_penalty = max(0.0, 50.0 - player.get("expected_minutes", 0.0)) * 0.045
            result = central * (1 + uncertainty) + differential - minutes_penalty
        else:
            result = central
    if cache_key is not None:
        cache[cache_key] = result
    return result


def _planner_event_points(squad, profile, relative_event, cache=None):
    score = lambda player: _planner_player_score(
        player, profile, relative_event, relative_event + 1, cache=cache
    )
    lineup, _ = _best_xi(squad, score)
    captain = max(lineup, key=score)
    return sum(score(player) for player in lineup) + score(captain)


def _planner_remaining_value(squad, profile, relative_event, horizon=5, cache=None):
    return sum(
        _planner_event_points(squad, profile, event_index, cache=cache)
        for event_index in range(relative_event, horizon)
    )


def _planner_single_moves(squad, eligible, cash, quotas, club_limit, profile, relative_event, limit=6, cache=None):
    owned_ids = {player["id"] for player in squad}
    by_position = {}
    for position in quotas:
        by_position[position] = sorted(
            (
                player for player in eligible
                if player["position_short"] == position and player["id"] not in owned_ids
            ),
            key=lambda player: _planner_player_score(player, profile, relative_event, cache=cache),
            reverse=True,
        )[:8]
    outgoing = []
    for position in quotas:
        outgoing.extend(sorted(
            (player for player in squad if player["position_short"] == position),
            key=lambda player: _planner_player_score(player, profile, relative_event, cache=cache),
        )[:2])
    moves = []
    # baseline is loop-invariant (squad/profile/relative_event never change across sold x bought) --
    # hoisted out rather than recomputed on every one of the loop's iterations.
    baseline_value = _planner_remaining_value(squad, profile, relative_event, cache=cache)
    for sold in outgoing:
        for bought in by_position[sold["position_short"]]:
            remaining = cash + sold["selling_price"] - bought["price"]
            if remaining < -1e-9:
                continue
            proposal = [
                ({**bought, "purchase_price": bought["price"], "selling_price": bought["price"]}
                 if player["id"] == sold["id"] else player)
                for player in squad
            ]
            if not _structure_legal(proposal, quotas, club_limit):
                continue
            gain = _planner_remaining_value(proposal, profile, relative_event, cache=cache) - baseline_value
            moves.append({
                "squad": proposal,
                "cash": round(remaining, 1),
                "transfers": [_move_record(sold, bought)],
                "planning_gain": gain,
            })
    moves.sort(key=lambda row: row["planning_gain"], reverse=True)
    return moves[:limit]


def _planner_action_candidates(squad, eligible, cash, quotas, club_limit, profile, relative_event, cache=None):
    roll = {"action": "roll", "squad": squad, "cash": cash, "transfers": []}
    singles = _planner_single_moves(
        squad, eligible, cash, quotas, club_limit, profile, relative_event, cache=cache
    )
    actions = [roll] + [{**move, "action": "single_transfer"} for move in singles]
    doubles = []
    for first in singles[:4]:
        sold_ids = {row["out"]["id"] for row in first["transfers"]}
        bought_ids = {row["in"]["id"] for row in first["transfers"]}
        seconds = _planner_single_moves(
            first["squad"], eligible, first["cash"], quotas, club_limit, profile, relative_event,
            limit=4, cache=cache,
        )
        for second in seconds:
            second_move = second["transfers"][0]
            if second_move["out"]["id"] in bought_ids or second_move["in"]["id"] in sold_ids:
                continue
            doubles.append({
                "action": "double_transfer",
                "squad": second["squad"],
                "cash": second["cash"],
                "transfers": first["transfers"] + second["transfers"],
                "planning_gain": first["planning_gain"] + second["planning_gain"],
            })
    doubles.sort(key=lambda row: row["planning_gain"], reverse=True)
    return actions + doubles[:3]


def _planner_step(node, candidate, profile, event, relative_event, maximum_free_transfers, cache=None):
    transfer_count = len(candidate["transfers"])
    point_cost = max(0, transfer_count - node["free_transfers"]) * 4
    free_next = min(
        maximum_free_transfers,
        max(0, node["free_transfers"] - transfer_count) + 1,
    )
    event_points = _planner_event_points(candidate["squad"], profile, relative_event, cache=cache)
    churn_penalty = {"conservative": 0.45, "balanced": 0.2, "aggressive": 0.0}[profile]
    action_value = event_points - point_cost - churn_penalty * transfer_count
    # Issue #256 (part B2): sum every squad member's own single-event score for this step, not
    # just the starting XI's (unlike event_points above) -- a double gameweek can matter for a
    # bench player too (Bench Boost, Free Hit), and project_players already sums every fixture
    # found for a team in one event (recommendations.py's _fixture_by_team/project_players), so a
    # double/blank gameweek already shows up as a spike/dip here with no extra computation beyond
    # calls _planner_action_candidates already made for this same candidate.
    squad_fixture_richness = sum(
        _planner_player_score(player, profile, relative_event, relative_event + 1, cache=cache)
        for player in candidate["squad"]
    )
    path_row = {
        "event": event,
        "action": candidate["action"],
        "transfers": candidate["transfers"],
        "point_cost": point_cost,
        "free_transfers_before": node["free_transfers"],
        "free_transfers_next_event": free_next,
        "projected_event_points": round(event_points, 2),
        "squad_fixture_richness": round(squad_fixture_richness, 2),
    }
    return {
        "squad": candidate["squad"],
        "cash": candidate["cash"],
        "free_transfers": free_next,
        "cumulative_value": node["cumulative_value"] + action_value,
        "path": node["path"] + [path_row],
    }


def _best_planner_continuation(
    initial_node, eligible, quotas, club_limit, profile, start_event,
    start_relative_event, maximum_free_transfers, horizon=5, beam_width=8, cache=None,
):
    beam = [initial_node]
    for relative_event in range(start_relative_event, horizon):
        event = start_event + relative_event
        expanded = []
        for node in beam:
            for candidate in _planner_action_candidates(
                node["squad"], eligible, node["cash"], quotas, club_limit, profile, relative_event,
                cache=cache,
            ):
                expanded.append(_planner_step(
                    node, candidate, profile, event, relative_event, maximum_free_transfers, cache=cache
                ))
        if not expanded:
            break
        expanded.sort(
            key=lambda node: node["cumulative_value"]
            + 0.12 * _planner_remaining_value(node["squad"], profile, relative_event + 1, cache=cache),
            reverse=True,
        )
        beam = expanded[:beam_width]
    for node in beam:
        terminal_squad = 0.08 * _planner_remaining_value(node["squad"], profile, 0, cache=cache)
        terminal_flexibility = 0.35 * node["free_transfers"] + 0.04 * node["cash"]
        node["plan_value"] = node["cumulative_value"] + terminal_squad + terminal_flexibility
    return max(beam, key=lambda node: node["plan_value"])


# Issue #256 (part B2): how far a planned gameweek's squad_fixture_richness has to stand out from
# the path's own other weeks before it's flagged as a chip-timing signal. Symmetric (spike and dip
# use the same magnitude) for simplicity -- a judgment call, not fitted; matches this plan's own
# worked example (plans/issue-256-chip-timing.md: a squad's own richness running ~28% above its
# other planned weeks from a 3-player double gameweek).
_CHIP_SIGNAL_DEVIATION_FRACTION = 0.25


def _chip_timing_signals(path):
    """Flag any future gameweek in `path` whose squad_fixture_richness stands out from the path's
    own other weeks -- a cheap proxy for a double/blank-gameweek shape, reusing numbers
    _planner_step already computed for the ordinary roll/single/double search (no additional
    _optimize_squad or _planner_player_score calls beyond what that search already makes).

    This is a heads-up only, not a chip verdict: it never says which chip, whether it clears that
    chip's marginal-value threshold, or what the reshuffled squad would look like. The exact chip
    decision is still made only once a gameweek becomes the *immediate* one, by
    _chip_recommendation/_exclusive_chip_scenario, unchanged by this function. Consistent with
    this planner's own disclosed limitation that future prices are held constant (see
    build_multiweek_plan's `assumptions`) -- a precise far-future chip verdict would be unreliable
    this far out anyway; an early-warning flag is the honest level of confidence to offer.

    Returns {event: signal_text} for only the events that stand out; an event with nothing
    unusual is simply absent, not present with a null/false value.
    """
    richness_values = [row["squad_fixture_richness"] for row in path]
    if len(richness_values) < 2:
        return {}
    average = sum(richness_values) / len(richness_values)
    if average <= 0:
        return {}
    signals = {}
    for row in path:
        deviation = (row["squad_fixture_richness"] - average) / average
        if deviation >= _CHIP_SIGNAL_DEVIATION_FRACTION:
            signals[row["event"]] = (
                f"Your squad's combined fixtures look unusually strong for Gameweek {row['event']} "
                "(possible double gameweek) -- reconsider your chip timing before then."
            )
        elif deviation <= -_CHIP_SIGNAL_DEVIATION_FRACTION:
            signals[row["event"]] = (
                f"Your squad's combined fixtures look unusually weak for Gameweek {row['event']} "
                "(possible blank gameweek) -- reconsider your chip timing before then."
            )
    return signals


def _conditional_branches(path, current_event):
    signals = _chip_timing_signals(path)
    branches = []
    for row in path:
        if row["event"] <= current_event:
            continue
        transfers = row["transfers"]
        if transfers:
            move_text = ", ".join(
                f"{move['out']['name']} to {move['in']['name']}" for move in transfers
            )
            condition = (
                f"If the outgoing players remain sellable and the targets remain available, affordable, "
                f"and secure for minutes, reconsider {move_text} before Gameweek {row['event']}."
            )
        else:
            condition = (
                f"If the squad remains available and no stronger information arrives, preserve flexibility "
                f"before Gameweek {row['event']}."
            )
        branches.append({
            "event": row["event"],
            "action": row["action"],
            "transfers": transfers,
            "point_cost": row["point_cost"],
            "free_transfers_before": row["free_transfers_before"],
            "free_transfers_next_event": row["free_transfers_next_event"],
            "condition": condition,
            "commitment": False,
            "chip_signal": signals.get(row["event"]),
        })
    return branches


def build_multiweek_plan(
    initial_scenarios, eligible, quotas, club_limit, profile, event,
    free_transfers, maximum_free_transfers, horizon=5,
):
    """Evaluate five events but recommend only the immediate action."""
    # Issue #176: one cache for this whole planning pass (every initial_scenarios branch plus the
    # roll_option_value comparison below) -- these branches share most of their squad, so scores
    # computed evaluating one branch are frequently reusable evaluating the next. See
    # _planner_player_score's docstring comment for what this eliminates and why it's safe.
    cache = {}
    evaluated = []
    for scenario in initial_scenarios:
        initial_candidate = {
            "action": scenario["action"],
            "squad": scenario["squad"],
            "cash": scenario["bank_after"],
            "transfers": scenario["transfers"],
        }
        root = {
            "squad": scenario["squad"],
            "cash": scenario["bank_after"],
            "free_transfers": free_transfers,
            "cumulative_value": 0.0,
            "path": [],
        }
        first = _planner_step(
            root, initial_candidate, profile, event, 0, maximum_free_transfers, cache=cache
        )
        best = _best_planner_continuation(
            first, eligible, quotas, club_limit, profile, event, 1,
            maximum_free_transfers, horizon=horizon, cache=cache,
        )
        best["post_initial_node"] = first
        best["immediate_scenario"] = scenario
        evaluated.append(best)
    evaluated.sort(key=lambda node: node["plan_value"], reverse=True)
    best = evaluated[0]
    roll = next(node for node in evaluated if node["immediate_scenario"]["action"] == "roll")
    if free_transfers >= maximum_free_transfers:
        roll_option_value = 0.0
    else:
        reduced_initial = dict(roll["post_initial_node"])
        reduced_initial["free_transfers"] = max(0, reduced_initial["free_transfers"] - 1)
        without_extra = _best_planner_continuation(
            reduced_initial, eligible, quotas, club_limit, profile, event, 1,
            maximum_free_transfers, horizon=horizon, cache=cache,
        )
        roll_option_value = max(0.0, roll["plan_value"] - without_extra["plan_value"])
    alternatives = []
    for node in evaluated:
        scenario = node["immediate_scenario"]
        alternatives.append({
            "action": scenario["action"],
            "transfers": scenario["transfers"],
            "immediate_point_cost": scenario["point_cost"],
            "plan_value": round(node["plan_value"], 2),
            "five_gameweek_delta_vs_roll": round(node["plan_value"] - roll["plan_value"], 2),
        })
    gap = best["plan_value"] - evaluated[1]["plan_value"] if len(evaluated) > 1 else 0.0
    confidence = "high" if gap >= 4 else "moderate" if gap >= 1.5 else "low"
    return {
        "planning_method": "five_gameweek_receding_horizon",
        "horizon_events": list(range(event, event + horizon)),
        "recommend_only_next_action": True,
        "immediate_action": best["immediate_scenario"]["action"],
        "evaluated_free_transfers": free_transfers,
        "confidence": confidence,
        "five_gameweek_advantage_over_roll": round(best["plan_value"] - roll["plan_value"], 2),
        "roll_option_value": round(roll_option_value, 2),
        "conditional_branches": _conditional_branches(best["path"], event)[:3],
        "alternatives": alternatives,
        "path": best["path"],
        "assumptions": [
            "Future transfers are conditional branches, not commitments.",
            "The plan is rebuilt only after an explicit refresh.",
            "Future prices are held constant in this strawman planner.",
            "Recent confirmed transfers use profile-specific role and minutes scenarios.",
            "Expected minutes are not yet gameweek-specific within each profile scenario.",
        ],
        "immediate_scenario": best["immediate_scenario"],
    }


def _chip_inventory(bootstrap, manager, event):
    used = manager.get("chips_used") or []
    inventory = []
    for chip in bootstrap.get("chips", []):
        start = int(chip.get("start_event") or 1)
        stop = int(chip.get("stop_event") or 38)
        matching_use = next(
            (row for row in used if row.get("name") == chip.get("name") and start <= int(row.get("event") or 0) <= stop),
            None,
        )
        available = start <= event <= stop and matching_use is None
        if chip.get("name") == "freehit" and any(
            row.get("name") == "freehit" and int(row.get("event") or 0) == event - 1 for row in used
        ):
            available = False
        inventory.append({
            "id": chip.get("id"),
            "name": chip.get("name"),
            "label": _CHIP_LABELS.get(chip.get("name"), chip.get("name")),
            "start_event": start,
            "stop_event": stop,
            "available": available,
            "used_event": matching_use.get("event") if matching_use else None,
            "rule": _CHIP_RULES.get(chip.get("name"), ""),
        })
    return inventory


def _chip_recommendation(profile, no_chip_scenario, inventory, eligible, quotas, budget, club_limit, event, cache=None):
    squad = no_chip_scenario["squad"]
    lineup_view = _lineup_view(squad, profile)
    no_chip_event = lineup_view["projected_event_points_including_captain"]
    available = {row["name"]: row for row in inventory if row["available"]}
    candidates = []
    if "3xc" in available:
        marginal = lineup_view["captain"]["xp_1"]
        candidates.append({"chip": "3xc", "label": "Triple Captain", "marginal_value": round(marginal, 1), "horizon": 1})
    if "bboost" in available:
        marginal = sum(row["xp_1"] for row in lineup_view["bench"])
        candidates.append({"chip": "bboost", "label": "Bench Boost", "marginal_value": round(marginal, 1), "horizon": 1})
    if "wildcard" in available:
        wildcard_squad = _optimize_squad(
            eligible, quotas, budget, club_limit, profile, horizon=5, runs=3, steps=1400, cache=cache
        )
        marginal = (
            _central_points(wildcard_squad, 5, profile, cache=cache)
            - _central_points(squad, 5, profile, cache=cache)
            + float(no_chip_scenario.get("point_cost") or 0)
        )
        candidates.append({
            "chip": "wildcard",
            "label": "Wildcard",
            "marginal_value": round(marginal, 1),
            "horizon": 5,
            "_squad": wildcard_squad,
        })
    if "freehit" in available:
        freehit_squad = _optimize_squad(
            eligible, quotas, budget, club_limit, profile, horizon=1, runs=3, steps=1400, cache=cache
        )
        marginal = (
            _central_points(freehit_squad, 1, profile, cache=cache)
            - no_chip_event
            + float(no_chip_scenario.get("point_cost") or 0)
        )
        candidates.append({
            "chip": "freehit",
            "label": "Free Hit",
            "marginal_value": round(marginal, 1),
            "horizon": 1,
            "_squad": freehit_squad,
        })
    for candidate in candidates:
        candidate["threshold"] = _THRESHOLDS[profile][candidate["chip"]]
        # Issue #256 (part A) / #267: compare against the scarcity-adjusted bar, not the raw
        # constant -- see _season_stage_effective_threshold's docstring for why this can't be a
        # plain multiply. `threshold` itself is left unchanged in the payload (still the honest
        # profile-baseline constant SPECIFICATION.md's chip contract requires disclosing);
        # `effective_threshold` is the one actually decided against. Issue #267: scoped to this
        # candidate's *own* remaining half-season window (`available`, built above from
        # `inventory`), not a single whole-season cutoff -- see _chip_window_extra_caution's
        # docstring for why that matters (the GW20-reset case #256 missed entirely).
        window = available[candidate["chip"]]
        candidate["effective_threshold"] = round(
            _season_stage_effective_threshold(
                candidate["threshold"], event, window["start_event"], window["stop_event"]
            ),
            1,
        )
        candidate["value_above_threshold"] = round(
            candidate["marginal_value"] - candidate["effective_threshold"], 1
        )
    public = lambda row: {key: value for key, value in row.items() if not key.startswith("_")}
    best = max(candidates, key=lambda row: row["value_above_threshold"], default=None)
    if best and best["value_above_threshold"] > 0:
        result = {
            "action": "play",
            **public(best),
            "no_chip_projected_points": no_chip_event,
            "reason": f"{best['label']} clears the {profile} marginal-value threshold by {best['value_above_threshold']:.1f} points.",
            "alternatives": [public(row) for row in candidates],
        }
        if best.get("_squad") is not None:
            result["chip_squad"] = best["_squad"]
        return result
    nearest = max(candidates, key=lambda row: row["value_above_threshold"], default=None)
    return {
        "action": "hold",
        "chip": None,
        "label": "Hold all chips",
        "marginal_value": round(nearest["marginal_value"], 1) if nearest else 0.0,
        "no_chip_projected_points": no_chip_event,
        "reason": "No available chip clears this profile's marginal-value threshold versus the no-chip counterfactual.",
        "alternatives": [public(row) for row in candidates],
    }


def _exclusive_chip_scenario(
    chip,
    ordinary_recommendation,
    original_squad,
    profile,
    total_sale_budget,
    free_transfers,
    maximum_free_transfers,
    cache=None,
):
    """Return the sole actionable scenario for a transfer chip.

    Wildcard permanently adopts the optimized squad. Free Hit uses the optimized
    squad for this event and then restores the pre-deadline squad. The ordinary
    transfer path remains a labeled alternative, never a simultaneous action.
    """
    chip_name = chip["chip"]
    chip_squad = chip["chip_squad"]
    lineup = _lineup_view(chip_squad, profile)
    persists = chip_name == "wildcard"
    persistent_squad = chip_squad if persists else original_squad
    action = f"play_{chip_name}"
    return {
        "action": action,
        "chip": chip_name,
        "label": chip["label"],
        "reason": chip["reason"],
        "transfers": [],
        "transfer_count": 0,
        "point_cost": 0,
        "gross_gain_5gw": chip["marginal_value"],
        "net_gain_5gw": chip["marginal_value"],
        "bank_after": round(total_sale_budget - sum(row["price"] for row in chip_squad), 1),
        "free_transfers_next_event": min(maximum_free_transfers, free_transfers + 1),
        "profile_score": round(_squad_objective(chip_squad, profile, cache=cache), 2),
        "squad": chip_squad,
        "squad_persists": persists,
        "reverts_after_event": not persists,
        "persistent_squad_after_event": persistent_squad,
        "ordinary_alternative": ordinary_recommendation,
        **lineup,
    }


def build_transfer_decisions(
    bootstrap, fixtures, manager, generated_at, horizon=5, recent_transfers=None,
):
    """Build roll, transfer, and chip scenarios for the next official event."""
    event = _next_event_id(bootstrap)
    if event <= 1:
        return {"status": "waiting_for_gw2", "event": event, "reason": "Transfer recommendations begin at Gameweek 2."}
    if manager.get("connection_status") == "not_configured":
        return {
            "status": "manager_not_configured",
            "event": event,
            "reason": (
                "No public team ID is configured. Add your FPL team ID in the My Team "
                "profile form, then refresh."
            ),
        }
    if not manager.get("squad_publicly_available") or len(manager.get("squad", [])) != 15:
        return {
            "status": "manager_squad_unavailable",
            "event": event,
            "reason": "The latest published 15-player squad is required. Unpublished transfers are never inferred.",
        }
    projections = project_players(
        bootstrap,
        fixtures,
        horizon=horizon,
        start_event=event,
        recent_transfers=recent_transfers,
        as_of=generated_at,
    )
    role_transition_player_ids = sorted(
        row["id"] for row in projections if row["role_transition"]
    )
    teammate_transfer_impact_player_ids = sorted(
        row["id"] for row in projections if row["teammate_transfer_impact"]
    )
    projection_by_id = {row["id"]: row for row in projections}
    squad = _public_squad(manager, projection_by_id)
    quotas = _quotas(bootstrap)
    settings = bootstrap.get("game_settings", {})
    club_limit = int(settings.get("squad_team_limit") or 3)
    maximum_free_transfers = int(settings.get("max_extra_free_transfers") or 4) + 1
    confirmed_free_transfers = manager.get("confirmed_free_transfers")
    confirmed_free_transfers_event = manager.get("confirmed_free_transfers_event")
    if confirmed_free_transfers_event is not None and int(confirmed_free_transfers_event) != event:
        confirmed_free_transfers = None
    if confirmed_free_transfers is None:
        free_transfers = derive_free_transfers(
            event,
            manager.get("public_transfers", []),
            manager.get("chips_used", []),
            maximum_free_transfers,
        )
        free_transfer_source = "estimated_public_history"
    else:
        free_transfers = min(maximum_free_transfers, max(0, int(confirmed_free_transfers)))
        free_transfer_source = "confirmed_local"
    if not _structure_legal(squad, quotas, club_limit):
        return {"status": "manager_squad_invalid", "event": event, "reason": "The published squad could not be mapped to a legal current FPL squad."}
    bank = _number(manager.get("bank")) / 10
    eligible = [row for row in projections if row["can_select"] and row["xp_5"] > 0]
    inventory = _chip_inventory(bootstrap, manager, event)
    total_sale_budget = sum(row["selling_price"] for row in squad) + bank
    profiles = []
    for profile in ("conservative", "balanced", "aggressive"):
        # Issue #176: separate from build_multiweek_plan's own internal cache (that one memoizes
        # _planner_player_score, a different function entirely) -- this one is for
        # _profile_event_score, via _squad_objective/_event_lineup_schedule/_central_points, used
        # by everything else in this profile's scenario/chip evaluation below. Scoped per profile
        # (not shared across all three) since nothing here is reused across profiles anyway --
        # _profile_event_score's cache key already includes profile.
        event_score_cache = {}
        roll_candidate = {"squad": squad, "cash": bank, "transfers": []}
        singles = _candidate_moves(squad, eligible, bank, quotas, club_limit, profile, cache=event_score_cache)
        if not singles:
            return {"status": "scenario_unavailable", "event": event, "reason": "No legal single-transfer scenario could be constructed."}
        double = _best_double(singles, eligible, quotas, club_limit, profile, cache=event_score_cache)
        if double is None:
            return {"status": "scenario_unavailable", "event": event, "reason": "No legal double-transfer scenario could be constructed."}
        scenarios = [
            _scenario("roll", roll_candidate, squad, profile, free_transfers, maximum_free_transfers, cache=event_score_cache),
            _scenario("single_transfer", singles[0], squad, profile, free_transfers, maximum_free_transfers, cache=event_score_cache),
            _scenario("double_transfer", double, squad, profile, free_transfers, maximum_free_transfers, cache=event_score_cache),
        ]
        # Issue #158: metrics/evaluation_horizons for the visitor's own *declared* squad
        # (`squad`, identical across all three profiles -- unlike `recommendation` below, which
        # may end up describing a different, post-transfer squad), not whatever this profile ends
        # up recommending. Powers a personalized "Compare risk profiles" panel.
        profile_metrics = _profile_metrics_for_squad(squad, profile, event, cache=event_score_cache)
        multiweek_plan = build_multiweek_plan(
            scenarios, eligible, quotas, club_limit, profile, event,
            free_transfers, maximum_free_transfers, horizon=horizon,
        )
        ordinary_recommendation = dict(multiweek_plan.pop("immediate_scenario"))
        if ordinary_recommendation["action"] == "roll":
            ordinary_recommendation["reason"] = (
                f"Roll the transfer: the five-gameweek planner values the reachable options in Gameweek {event + 1} "
                "above the modeled immediate moves."
            )
        else:
            ordinary_recommendation["reason"] = (
                f"The five-gameweek planner prefers {ordinary_recommendation['action'].replace('_', ' ')} after hits, "
                "churn, and future flexibility are included."
            )
        # Issue #181: roll/single/double above get the full five-gameweek planner treatment
        # (build_multiweek_plan's beam search over future gameweeks, weighing hit cost against
        # retained flexibility). 3+ transfers this gameweek deliberately do not -- per
        # plans/issue-181-multi-transfer-search.md, this is scoped to the immediate gameweek only,
        # not fed into the planner's own continuation (which stays limited to roll/single/double,
        # a separate follow-on decision). Brute-forcing 3+ legs the way _best_double brute-forces 2
        # is computationally infeasible (see _beam_multi_transfer's docstring), so this uses a beam
        # search instead and compares purely on this gameweek's net_gain_5gw -- an immediate,
        # not full-future-flexibility-aware, comparison. That asymmetry is disclosed in the
        # resulting reason text below rather than left implicit.
        multi_leg_scenarios = []
        if maximum_free_transfers >= 3:
            beam_results = _beam_multi_transfer(
                squad, eligible, bank, quotas, club_limit, profile, maximum_free_transfers,
                cache=event_score_cache,
            )
            for leg_count in range(3, maximum_free_transfers + 1):
                candidate = beam_results.get(leg_count)
                if candidate is None:
                    continue
                multi_leg_scenarios.append(
                    _scenario(
                        "multi_transfer", candidate, squad, profile, free_transfers,
                        maximum_free_transfers, cache=event_score_cache,
                    )
                )
        best_multi_leg = max(multi_leg_scenarios, key=lambda row: row["net_gain_5gw"], default=None)
        # Issue #278: this override used to accept any positive margin, however small -- a -16
        # point hit for 5 simultaneous transfers could be triggered by an edge as thin as +0.1,
        # with no regard for how little of the season (and therefore how noisy the per-player
        # signal driving that edge) has actually been observed yet. required_margin converges to
        # 0 by ~Gameweek 6-7, restoring exactly today's behavior once the season has settled in;
        # see _multi_transfer_required_margin's docstring and plans/issue-278-multi-transfer-
        # caution.md for the full reasoning and real-data verification.
        required_margin = _multi_transfer_required_margin(event)
        if (
            best_multi_leg is not None
            and best_multi_leg["net_gain_5gw"] > ordinary_recommendation["net_gain_5gw"] + required_margin
        ):
            # Issue #266: `required_margin` itself, and how far above it the winning margin
            # actually was, are attached to the recommendation before it's overwritten below --
            # mirrors `_chip_recommendation`'s own `threshold`/`effective_threshold`/
            # `value_above_threshold` fields, which were already on the payload while this side's
            # equivalent was a bare local variable invisible to the frontend (confirmed while
            # investigating #266 -- see its plan doc's finding 4). Computed against the
            # *pre-override* `ordinary_recommendation`'s own net_gain_5gw, the same baseline the
            # `if` above just compared against, not the multi-leg scenario's own.
            margin_above_required = round(
                best_multi_leg["net_gain_5gw"] - ordinary_recommendation["net_gain_5gw"] - required_margin, 1
            )
            ordinary_recommendation = dict(best_multi_leg)
            ordinary_recommendation["reason"] = (
                f"Making {ordinary_recommendation['transfer_count']} transfers this gameweek projects the strongest "
                "five-gameweek net gain of any option modeled -- an immediate-horizon comparison only, unlike the "
                "five-gameweek planner's roll/single/double comparison above, which also weighs future flexibility."
            )
            ordinary_recommendation["required_margin"] = round(required_margin, 1)
            ordinary_recommendation["margin_above_required"] = margin_above_required
        scenarios = scenarios + multi_leg_scenarios
        chip = _chip_recommendation(
            profile, ordinary_recommendation, inventory, eligible, quotas, total_sale_budget, club_limit,
            event, cache=event_score_cache,
        )
        recommendation = ordinary_recommendation
        if chip.get("action") == "play" and chip.get("chip") in {"wildcard", "freehit"}:
            recommendation = _exclusive_chip_scenario(
                chip,
                ordinary_recommendation,
                squad,
                profile,
                total_sale_budget,
                free_transfers,
                maximum_free_transfers,
                cache=event_score_cache,
            )
            multiweek_plan = {
                **multiweek_plan,
                "planning_method": "exclusive_transfer_chip_vs_no_chip_plan",
                "immediate_action": recommendation["action"],
                "ordinary_immediate_action": ordinary_recommendation["action"],
                "conditional_branches": [],
                "path": [],
                "assumptions": list(multiweek_plan.get("assumptions") or []) + [
                    "Wildcard replaces the ordinary path and permanently adopts its optimized squad."
                    if chip["chip"] == "wildcard"
                    else "Free Hit replaces the ordinary path for this event and then restores the pre-deadline squad."
                ],
            }
        profiles.append({
            "id": profile,
            **_PROFILE_DEFINITIONS[profile],
            "recommendation": recommendation,
            "multiweek_plan": multiweek_plan,
            "scenarios": scenarios,
            "chip_recommendation": chip,
            **profile_metrics,
        })
    return {
        "status": "active",
        "event": event,
        "generated_at": generated_at,
        "free_transfers": free_transfers,
        "free_transfer_source": free_transfer_source,
        "maximum_free_transfers": maximum_free_transfers,
        "public_state_event": manager.get("current_event"),
        "role_transition_player_ids": role_transition_player_ids,
        "teammate_transfer_impact_player_ids": teammate_transfer_impact_player_ids,
        "state_warning": "Recommendations use the latest published deadline squad and cannot see unpublished in-Gameweek transfers.",
        "official_rules": {
            "source": _RULES_URL,
            "reviewed_at": generated_at,
            "free_transfer_per_gameweek": 1,
            "maximum_free_transfers": maximum_free_transfers,
            "extra_transfer_cost": 4,
            "transfers_cap": int(settings.get("transfers_cap") or 20),
            "chips_per_half": True,
        },
        "chip_inventory": inventory,
        "default_profile": "balanced",
        "profiles": profiles,
    }


def build_draft_decisions(
    bootstrap, fixtures, draft_squad_ids, generated_at, horizon=5, recent_transfers=None,
):
    """Personalized feedback on a manager's self-declared preseason draft squad (issue #61).

    Sibling to `build_transfer_decisions`, for the window before Gameweek 1's deadline where
    FPL's public API has no real picks to personalize against for anyone -- a manager can
    instead declare a draft squad through the dashboard's own UI and get feedback on it here.

    Reuses the same `_candidate_moves`/`_best_double` single/double-transfer search
    `build_transfer_decisions` uses for a real GW2+ entry, with free-transfer point costs
    disabled: nothing has been "spent" yet preseason, so a visitor can freely reshuffle their
    declared squad with no cost. `_scenario`'s point-cost math is `max(0, transfer_count -
    free_transfers) * 4` -- passing a free-transfer count at least as large as the squad size
    zeroes it out for every possible transfer count, without a second cost-accounting path.
    """
    event = _next_event_id(bootstrap)
    try:
        validate_draft_squad(bootstrap, draft_squad_ids)
    except ValueError as error:
        return {"status": "draft_squad_invalid", "event": event, "reason": str(error)}
    quotas = _quotas(bootstrap)
    settings = bootstrap.get("game_settings", {})
    club_limit = int(settings.get("squad_team_limit") or 3)
    budget = _number(settings.get("squad_total_spend"), 1000) / 10
    projections = project_players(
        bootstrap, fixtures, horizon=horizon, start_event=event,
        recent_transfers=recent_transfers, as_of=generated_at,
    )
    projection_by_id = {row["id"]: row for row in projections}
    squad = _draft_squad(draft_squad_ids, projection_by_id)
    if len(squad) != len(draft_squad_ids):
        return {
            "status": "draft_squad_invalid",
            "event": event,
            "reason": "One or more drafted players could not be matched to the current player catalog.",
        }
    bank = round(budget - sum(row["price"] for row in squad), 1)
    eligible = [row for row in projections if row["can_select"] and row["xp_5"] > 0]
    # No free-transfer budget exists before a squad has ever been submitted -- every candidate
    # move is reported with no point cost, so pass an unlimited free-transfer allowance into the
    # existing scenario accounting rather than adding a second, cost-free code path.
    unlimited_free_transfers = len(squad)
    # Issue #181: the search bound for multi-leg reshuffles below, distinct from
    # unlimited_free_transfers above -- that one is about *hit-cost accounting* (none applies
    # preseason), this one is about how many transfer legs are even worth searching. The official
    # maximum banked free transfers is the natural real-world ceiling (matches
    # build_transfer_decisions's own bound) rather than searching up to the full 15-player squad.
    max_search_legs = int(settings.get("max_extra_free_transfers") or 4) + 1
    profiles = []
    for profile in ("conservative", "balanced", "aggressive"):
        # Issue #176: see the matching comment in build_transfer_decisions -- separate from
        # build_multiweek_plan's own cache, this one is for _profile_event_score.
        event_score_cache = {}
        roll_candidate = {"squad": squad, "cash": bank, "transfers": []}
        singles = _candidate_moves(squad, eligible, bank, quotas, club_limit, profile, cache=event_score_cache)
        if not singles:
            return {"status": "scenario_unavailable", "event": event, "reason": "No legal single-transfer scenario could be constructed."}
        double = _best_double(singles, eligible, quotas, club_limit, profile, cache=event_score_cache)
        if double is None:
            return {"status": "scenario_unavailable", "event": event, "reason": "No legal double-transfer scenario could be constructed."}
        scenarios = [
            _scenario("roll", roll_candidate, squad, profile, unlimited_free_transfers, unlimited_free_transfers, cache=event_score_cache),
            _scenario("single_transfer", singles[0], squad, profile, unlimited_free_transfers, unlimited_free_transfers, cache=event_score_cache),
            _scenario("double_transfer", double, squad, profile, unlimited_free_transfers, unlimited_free_transfers, cache=event_score_cache),
        ]
        # Issue #181: no separate "planner vs immediate" comparison needed here, unlike
        # build_transfer_decisions -- build_draft_decisions never calls build_multiweek_plan, it
        # already just picks the single best scenario by net_gain_5gw below. Appending multi-leg
        # candidates to the same list before that pick means they compete on equal footing, no
        # extra comparison logic required.
        if max_search_legs >= 3:
            beam_results = _beam_multi_transfer(
                squad, eligible, bank, quotas, club_limit, profile, max_search_legs,
                cache=event_score_cache,
            )
            for leg_count in range(3, max_search_legs + 1):
                candidate = beam_results.get(leg_count)
                if candidate is None:
                    continue
                scenarios.append(
                    _scenario(
                        "multi_transfer", candidate, squad, profile, unlimited_free_transfers,
                        unlimited_free_transfers, cache=event_score_cache,
                    )
                )
        # Issue #158: metrics/evaluation_horizons for the visitor's own *declared* draft squad
        # (`squad`, identical across all three profiles), not whatever this profile ends up
        # recommending. Powers a personalized "Compare risk profiles" panel.
        profile_metrics = _profile_metrics_for_squad(squad, profile, event, cache=event_score_cache)
        recommendation = dict(max(scenarios, key=lambda row: row["net_gain_5gw"]))
        if recommendation["action"] == "roll":
            recommendation["reason"] = (
                "No suggested swap projects more five-gameweek points than your declared squad as-is."
            )
        elif recommendation["action"] == "multi_transfer":
            recommendation["reason"] = (
                f"Making {recommendation['transfer_count']} transfers projects the strongest five-gameweek "
                "improvement over your declared squad, with no cost before Gameweek 1."
            )
        else:
            recommendation["reason"] = (
                f"{recommendation['action'].replace('_', ' ').capitalize()} projects the strongest five-gameweek "
                "improvement over your declared squad, with no cost before Gameweek 1."
            )
        chip_recommendation = {
            "action": "hold",
            "chip": None,
            "label": "Chips unavailable before Gameweek 1",
            "marginal_value": 0.0,
            "no_chip_projected_points": recommendation["projected_event_points_including_captain"],
            "reason": "Chip decisions are evaluated for your real Gameweek 1 entry once the season starts.",
            "alternatives": [],
        }
        profiles.append({
            "id": profile,
            **_PROFILE_DEFINITIONS[profile],
            "recommendation": recommendation,
            "scenarios": scenarios,
            "chip_recommendation": chip_recommendation,
            **profile_metrics,
        })
    return {
        "status": "active",
        "event": event,
        "generated_at": generated_at,
        "draft": True,
        "bank": bank,
        "state_warning": (
            "This is feedback on your self-declared draft squad, not FPL's official picks -- FPL "
            "hides public picks until Gameweek 1's deadline passes. No transfer costs apply before "
            "the season starts; reshuffle your draft as many times as you like."
        ),
        "official_rules": {
            "source": _RULES_URL,
            "reviewed_at": generated_at,
            "free_transfer_per_gameweek": 1,
            "maximum_free_transfers": int(settings.get("max_extra_free_transfers") or 4) + 1,
            "extra_transfer_cost": 4,
            "transfers_cap": int(settings.get("transfers_cap") or 20),
            "chips_per_half": True,
        },
        "chip_inventory": [],
        "default_profile": "balanced",
        "profiles": profiles,
    }
