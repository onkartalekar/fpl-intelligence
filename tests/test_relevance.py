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

    def test_club_summary_counts_arrivals_departures_and_relevant_moves(self):
        rows = [
            {"premier_league_club": "Arsenal", "movement_direction": "in", "fpl_relevance": "medium"},
            {"premier_league_club": "Arsenal", "movement_direction": "out", "fpl_relevance": "high"},
            {"premier_league_club": "Arsenal", "movement_direction": "released", "fpl_relevance": "low"},
        ]

        summaries = summarize_clubs(rows)

        self.assertEqual(summaries[0]["arrivals"], 1)
        self.assertEqual(summaries[0]["departures"], 2)
        self.assertEqual(summaries[0]["relevant_moves"], 2)


if __name__ == "__main__":
    unittest.main()
