import unittest

from fpl_intel.pl_transfers import parse_team_playlist


class PremierLeagueTransferPlaylistTests(unittest.TestCase):
    def test_parses_official_transfer_in_record(self):
        playlist = {
            "title": "Summer 2026 - Transfer Centre - Arsenal",
            "items": [
                {
                    "response": {
                        "title": "Piero Hincapie",
                        "description": "Bayer Leverkusen",
                        "date": "2026-02-10T14:07:00Z",
                        "tags": [{"label": "transfer-in"}],
                        "links": [
                            {
                                "promoUrl": "https://www.arsenal.com/news/piero-hincapie",
                                "linkText": "Details",
                            }
                        ],
                    }
                }
            ],
        }

        records = parse_team_playlist("Arsenal", playlist)

        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["player"], "Piero Hincapie")
        self.assertEqual(records[0]["from_club"], "Bayer Leverkusen")
        self.assertEqual(records[0]["to_club"], "Arsenal")
        self.assertEqual(records[0]["premier_league_club"], "Arsenal")
        self.assertEqual(records[0]["movement_type"], "transfer-in")
        self.assertEqual(records[0]["verification_status"], "confirmed_first_party")


if __name__ == "__main__":
    unittest.main()
