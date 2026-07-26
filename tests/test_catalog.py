import unittest

from fpl_intel.catalog import build_fixture_catalog, build_player_catalog


class PlayerCatalogTests(unittest.TestCase):
    def test_builds_searchable_player_prices_from_official_bootstrap(self):
        bootstrap = {
            "teams": [{"id": 1, "name": "Arsenal", "short_name": "ARS"}],
            "element_types": [{"id": 1, "singular_name": "Goalkeeper", "singular_name_short": "GKP"}],
            "elements": [
                {
                    "id": 1,
                    "first_name": "David",
                    "second_name": "Raya",
                    "web_name": "Raya",
                    "team": 1,
                    "element_type": 1,
                    "now_cost": 60,
                    "selected_by_percent": "26.9",
                    "status": "a",
                    "news": "",
                    "form": "0.0",
                    "total_points": 0,
                    "minutes": 0,
                    "starts": 0,
                }
            ],
        }

        players = build_player_catalog(bootstrap)

        self.assertEqual(players[0]["name"], "Raya")
        self.assertEqual(players[0]["full_name"], "David Raya")
        self.assertEqual(players[0]["club"], "Arsenal")
        self.assertEqual(players[0]["position"], "Goalkeeper")
        self.assertEqual(players[0]["price"], 6.0)
        self.assertEqual(players[0]["ownership"], 26.9)


class FixtureCatalogTests(unittest.TestCase):
    def test_maps_fixture_team_ids_to_names_and_difficulties(self):
        bootstrap = {
            "teams": [
                {"id": 1, "name": "Arsenal", "short_name": "ARS"},
                {"id": 7, "name": "Chelsea", "short_name": "CHE"},
            ]
        }
        fixtures = [
            {
                "id": 1, "event": 1, "team_h": 1, "team_a": 7,
                "kickoff_time": "2026-08-21T19:00:00Z",
                "team_h_difficulty": 2, "team_a_difficulty": 5,
                "finished": False, "started": False,
                "team_h_score": None, "team_a_score": None,
            }
        ]

        catalog = build_fixture_catalog(fixtures, bootstrap)

        self.assertEqual(catalog[0]["home_team"], "Arsenal")
        self.assertEqual(catalog[0]["away_team"], "Chelsea")
        self.assertEqual(catalog[0]["home_difficulty"], 2)
        self.assertEqual(catalog[0]["away_difficulty"], 5)
        self.assertEqual(catalog[0]["event"], 1)


if __name__ == "__main__":
    unittest.main()
