import unittest

from fpl_intel.relevance import enrich_transfers, summarize_clubs


class TransferRelevanceTests(unittest.TestCase):
    def setUp(self):
        self.bootstrap = {
            "elements": [
                {
                    "id": 10,
                    "first_name": "Known",
                    "second_name": "Player",
                    "web_name": "Known",
                }
            ]
        }

    def test_existing_fpl_player_is_high_relevance(self):
        rows = enrich_transfers(
            [
                {
                    "player": "Known Player",
                    "from_club": "Arsenal",
                    "to_club": "Porto",
                    "movement_type": "transfer-out",
                    "announced_at": "2026-07-17T12:00:00Z",
                }
            ],
            self.bootstrap,
            generated_at="2026-07-18T12:00:00Z",
        )

        self.assertEqual(rows[0]["fpl_relevance"], "high")
        self.assertEqual(rows[0]["movement_direction"], "out")
        self.assertEqual(rows[0]["freshness"], "new_7d")
        self.assertEqual(rows[0]["matched_fpl_element_id"], 10)
        self.assertEqual(rows[0]["fpl_reconciliation_status"], "matched_current_fpl")

    def test_new_arrival_is_medium_and_unknown_release_is_low(self):
        rows = enrich_transfers(
            [
                {
                    "player": "New Arrival",
                    "from_club": "Ajax",
                    "to_club": "Arsenal",
                    "movement_type": "transfer-in",
                    "announced_at": "2026-07-01T12:00:00Z",
                },
                {
                    "player": "Academy Player",
                    "from_club": "Arsenal",
                    "to_club": "Released",
                    "movement_type": "player-released",
                    "announced_at": "2026-06-01T12:00:00Z",
                },
            ],
            self.bootstrap,
            generated_at="2026-07-18T12:00:00Z",
        )

        self.assertEqual(rows[0]["fpl_relevance"], "medium")
        self.assertEqual(rows[0]["movement_direction"], "in")
        self.assertEqual(rows[1]["fpl_relevance"], "low")
        self.assertEqual(rows[1]["movement_direction"], "released")

    def test_matches_press_name_with_extra_bootstrap_surnames(self):
        # FPL's own record often carries additional family/maternal
        # surnames the transfer feed doesn't report -- e.g. Bruno
        # Guimaraes -> Newcastle -> Arsenal, bootstrap "second_name"
        # is "Guimarães Rodriguez Moura", web_name is "Bruno G.".
        bootstrap = {
            "elements": [
                {
                    "id": 452,
                    "first_name": "Bruno",
                    "second_name": "Guimarães Rodriguez Moura",
                    "web_name": "Bruno G.",
                }
            ]
        }

        rows = enrich_transfers(
            [
                {
                    "player": "Bruno Guimaraes",
                    "from_club": "Newcastle",
                    "to_club": "Arsenal",
                    "movement_type": "transfer-out",
                    "announced_at": "2026-07-23T12:00:00Z",
                }
            ],
            bootstrap,
            generated_at="2026-08-01T12:00:00Z",
        )

        self.assertEqual(rows[0]["matched_fpl_element_id"], 452)
        self.assertEqual(rows[0]["fpl_relevance"], "high")
        self.assertEqual(rows[0]["fpl_reconciliation_status"], "matched_current_fpl")

    def test_matches_non_decomposable_letters_transliterated_to_ascii(self):
        # "ø" has no NFKD decomposition into an ASCII base letter (unlike
        # e.g. "ã" -> "a"), so a naive accent-stripper drops it entirely
        # ("Nørgaard" -> "Nrgaard") instead of transliterating it.
        bootstrap = {
            "elements": [
                {"id": 21, "first_name": "Christian", "second_name": "Nørgaard", "web_name": "Nørgaard"}
            ]
        }

        rows = enrich_transfers(
            [
                {
                    "player": "Christian Norgaard",
                    "from_club": "Arsenal",
                    "to_club": "Everton",
                    "movement_type": "transfer-out",
                    "announced_at": "2026-07-23T12:00:00Z",
                }
            ],
            bootstrap,
            generated_at="2026-08-01T12:00:00Z",
        )

        self.assertEqual(rows[0]["matched_fpl_element_id"], 21)
        self.assertEqual(rows[0]["fpl_relevance"], "high")

    def test_ambiguous_primary_surname_across_two_players_is_not_matched(self):
        # Two different bootstrap players sharing a first name and primary
        # surname (differing only in an extra family surname) must not
        # produce a guessed match -- unmatched is the honest outcome.
        bootstrap = {
            "elements": [
                {"id": 1, "first_name": "Joao", "second_name": "Silva Santos", "web_name": "J.Silva"},
                {"id": 2, "first_name": "Joao", "second_name": "Silva Costa", "web_name": "Silva"},
            ]
        }

        rows = enrich_transfers(
            [
                {
                    "player": "Joao Silva",
                    "from_club": "Arsenal",
                    "to_club": "Porto",
                    "movement_type": "transfer-out",
                    "announced_at": "2026-07-23T12:00:00Z",
                }
            ],
            bootstrap,
            generated_at="2026-08-01T12:00:00Z",
        )

        self.assertIsNone(rows[0]["matched_fpl_element_id"])
        self.assertEqual(rows[0]["fpl_relevance"], "low")

    def test_player_genuinely_absent_from_bootstrap_stays_unmatched(self):
        # A confirmed transfer for a player who simply isn't in the FPL
        # database yet (a real data-lag case, not a matching bug) must
        # still come out unmatched -- no strategy should force a guess.
        rows = enrich_transfers(
            [
                {
                    "player": "Tynan Thompson",
                    "from_club": "Spurs",
                    "to_club": "Man Utd",
                    "movement_type": "transfer-in",
                    "announced_at": "2026-07-23T12:00:00Z",
                }
            ],
            self.bootstrap,
            generated_at="2026-08-01T12:00:00Z",
        )

        self.assertIsNone(rows[0]["matched_fpl_element_id"])
        self.assertEqual(rows[0]["fpl_relevance"], "medium")

    def test_normalizes_inconsistently_spelled_club_names_to_bootstrap_form(self):
        # Different clubs' own write-ups don't always spell an opposing
        # club's name the same way ("Brighton" vs. "Brighton & Hove
        # Albion") -- from_club/to_club must collapse to one consistent
        # name per club, matching the bootstrap feed's own team name,
        # so downstream club filters/summaries don't split one real club
        # across multiple spellings (see #35).
        bootstrap = {
            "elements": self.bootstrap["elements"],
            "teams": [{"id": 1, "name": "Brighton & Hove Albion"}],
        }

        rows = enrich_transfers(
            [
                {
                    "player": "Someone",
                    "from_club": "Brighton",
                    "to_club": "Porto",
                    "movement_type": "transfer-out",
                    "announced_at": "2026-07-23T12:00:00Z",
                }
            ],
            bootstrap,
            generated_at="2026-08-01T12:00:00Z",
        )

        self.assertEqual(rows[0]["from_club"], "Brighton & Hove Albion")
        self.assertEqual(rows[0]["to_club"], "Porto")

    def test_club_summary_counts_arrivals_departures_and_relevant_moves(self):
        bootstrap = {"teams": [{"id": 1, "name": "Arsenal"}]}
        rows = [
            {"from_club": "Ajax", "to_club": "Arsenal", "premier_league_club": "Arsenal", "fpl_relevance": "medium", "announced_at": "2026-07-01T12:00:00Z"},
            {"from_club": "Arsenal", "to_club": "Porto", "premier_league_club": "Arsenal", "fpl_relevance": "high", "announced_at": "2026-07-02T12:00:00Z"},
            {"from_club": "Arsenal", "to_club": "Released", "premier_league_club": "Arsenal", "fpl_relevance": "low", "announced_at": "2026-06-01T12:00:00Z"},
        ]

        summaries = summarize_clubs(rows, bootstrap)

        self.assertEqual(summaries[0]["club"], "Arsenal")
        self.assertEqual(summaries[0]["arrivals"], 1)
        self.assertEqual(summaries[0]["departures"], 2)
        self.assertEqual(summaries[0]["relevant_moves"], 2)

    def test_club_summary_credits_both_sides_of_a_merged_cross_club_move(self):
        # A move confirmed by both the selling and buying club's own
        # transfer-centre feeds gets merged into one record by refresh.py,
        # keeping only one club's premier_league_club/movement_direction
        # attribution (see #35) -- the summary must still credit both the
        # real origin club (a departure) and the real destination club (an
        # arrival) from from_club/to_club, not just the one that survived.
        bootstrap = {"teams": [{"id": 1, "name": "Newcastle"}, {"id": 2, "name": "Arsenal"}]}
        rows = [
            {
                "from_club": "Newcastle",
                "to_club": "Arsenal",
                "premier_league_club": "Newcastle",
                "movement_direction": "out",
                "fpl_relevance": "high",
                "announced_at": "2026-07-23T12:00:00Z",
            }
        ]

        summaries = summarize_clubs(rows, bootstrap)
        by_club = {item["club"]: item for item in summaries}

        self.assertEqual(by_club["Newcastle"]["departures"], 1)
        self.assertEqual(by_club["Newcastle"]["arrivals"], 0)
        self.assertEqual(by_club["Arsenal"]["arrivals"], 1)
        self.assertEqual(by_club["Arsenal"]["departures"], 0)
        self.assertEqual(by_club["Newcastle"]["relevant_moves"], 1)
        self.assertEqual(by_club["Arsenal"]["relevant_moves"], 1)

    def test_club_summary_ignores_non_pl_clubs_on_both_sides(self):
        # A move between two clubs neither of which is currently in the
        # Premier League (e.g. a lower-league loan) falls back to the
        # reporting club rather than being silently dropped.
        bootstrap = {"teams": [{"id": 1, "name": "Arsenal"}]}
        rows = [
            {
                "from_club": "Grimsby Town",
                "to_club": "Gillingham",
                "premier_league_club": "Arsenal",
                "fpl_relevance": "low",
                "announced_at": "2026-07-23T12:00:00Z",
            }
        ]

        summaries = summarize_clubs(rows, bootstrap)

        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["club"], "Arsenal")
        self.assertEqual(summaries[0]["arrivals"], 0)
        self.assertEqual(summaries[0]["departures"], 0)


if __name__ == "__main__":
    unittest.main()
