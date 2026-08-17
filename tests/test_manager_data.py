import unittest

from fpl_intel.sources.manager_data import collect_public_manager, summarize_manager


class ManagerDataTests(unittest.TestCase):
    def test_collects_registered_preseason_team_without_claiming_squad_access(self):
        responses = {
            "https://fantasy.premierleague.com/api/entry/364759/": {
                "id": 364759,
                "name": "BrunoMans",
                "player_first_name": "Onkar",
                "player_last_name": "Talekar",
                "current_event": None,
                "started_event": 1,
                "summary_overall_points": None,
                "summary_overall_rank": None,
            },
            "https://fantasy.premierleague.com/api/entry/364759/history/": {
                "current": [], "past": [], "chips": []
            },
            "https://fantasy.premierleague.com/api/entry/364759/transfers/": [],
        }
        calls = []

        def fetch_json(url):
            calls.append(url)
            return responses[url]

        raw = collect_public_manager(364759, fetch_json=fetch_json)
        summary = summarize_manager(raw, bootstrap={"elements": []})

        self.assertEqual(len(calls), 3)
        self.assertEqual(summary["team_id"], 364759)
        self.assertEqual(summary["team_name"], "BrunoMans")
        self.assertEqual(summary["manager_name"], "Onkar Talekar")
        self.assertEqual(summary["connection_status"], "registered_preseason")
        self.assertFalse(summary["squad_publicly_available"])
        self.assertEqual(summary["squad"], [])

    def test_maps_public_gameweek_picks_to_player_names(self):
        responses = {
            "https://fantasy.premierleague.com/api/entry/364759/": {
                "id": 364759, "name": "BrunoMans", "current_event": 1
            },
            "https://fantasy.premierleague.com/api/entry/364759/history/": {
                "current": [], "past": [], "chips": []
            },
            "https://fantasy.premierleague.com/api/entry/364759/transfers/": [],
            "https://fantasy.premierleague.com/api/entry/364759/event/1/picks/": {
                "active_chip": None,
                "entry_history": {"bank": 5, "value": 1005},
                "picks": [
                    {
                        "element": 10, "position": 1, "multiplier": 2,
                        "is_captain": True, "is_vice_captain": False,
                        "purchase_price": 73, "selling_price": 74,
                    }
                ],
            },
        }
        raw = collect_public_manager(364759, fetch_json=lambda url: responses[url])
        summary = summarize_manager(
            raw,
            bootstrap={"elements": [{"id": 10, "web_name": "Example", "team": 2, "element_type": 3, "now_cost": 75}]},
        )

        self.assertTrue(summary["squad_publicly_available"])
        self.assertEqual(summary["squad"][0]["name"], "Example")
        self.assertTrue(summary["squad"][0]["is_captain"])
        self.assertEqual(summary["team_value"], 1005)
        self.assertEqual(summary["bank"], 5)
        self.assertEqual(summary["squad"][0]["purchase_price"], 73)
        self.assertEqual(summary["squad"][0]["selling_price"], 74)


if __name__ == "__main__":
    unittest.main()
