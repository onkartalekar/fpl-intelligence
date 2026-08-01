import unittest

from fpl_intel.transfers import OFFICIAL_CLUB_DOMAINS, normalize_transfer


class OfficialClubDomainsTests(unittest.TestCase):
    def test_covers_every_current_premier_league_club_domain(self):
        expected = {
            "arsenal.com", "avfc.co.uk", "afcb.co.uk", "brentfordfc.com",
            "brightonandhovealbion.com", "ccfc.co.uk", "chelseafc.com",
            "cpfc.co.uk", "evertonfc.com", "fulhamfc.com", "wearehullcity.co.uk",
            "itfc.co.uk", "leedsunited.com", "liverpoolfc.com", "mancity.com",
            "manutd.com", "newcastleunited.com", "nottinghamforest.co.uk",
            "safc.com", "tottenhamhotspur.com",
        }

        self.assertEqual(OFFICIAL_CLUB_DOMAINS, expected)

    def test_no_longer_trusts_clubs_outside_this_seasons_premier_league(self):
        self.assertNotIn("burnleyfootballclub.com", OFFICIAL_CLUB_DOMAINS)
        self.assertNotIn("whufc.com", OFFICIAL_CLUB_DOMAINS)
        self.assertNotIn("wolves.co.uk", OFFICIAL_CLUB_DOMAINS)
        self.assertNotIn("nufc.co.uk", OFFICIAL_CLUB_DOMAINS)


class NormalizeTransferTests(unittest.TestCase):
    def test_accepts_first_party_confirmed_transfer(self):
        record = normalize_transfer(
            {
                "player": "Example Player",
                "from_club": "Club A",
                "to_club": "Club B",
                "announced_at": "2026-07-18T12:00:00-04:00",
                "source_url": "https://www.premierleague.com/example",
                "source_type": "official_premier_league",
            }
        )

        self.assertEqual(record["verification_status"], "confirmed_first_party")
        self.assertEqual(record["fpl_reconciliation_status"], "pending_new_season_fpl")
        self.assertEqual(record["player"], "Example Player")

    def test_rejects_non_https_premier_league_source(self):
        with self.assertRaisesRegex(ValueError, "trusted HTTPS"):
            normalize_transfer(
                {
                    "player": "Example Player", "from_club": "A", "to_club": "B",
                    "announced_at": "2026-07-18T12:00:00Z",
                    "source_url": "javascript:alert(1)",
                    "source_type": "official_premier_league",
                }
            )

    def test_rejects_official_club_claim_when_url_does_not_match_declared_domain(self):
        with self.assertRaisesRegex(ValueError, "official club domain"):
            normalize_transfer(
                {
                    "player": "Rumoured Player",
                    "from_club": "Club A",
                    "to_club": "Club B",
                    "announced_at": "2026-07-18T12:00:00-04:00",
                    "source_url": "https://news.example/rumour",
                    "source_type": "official_club",
                    "official_club_domain": "clubb.example",
                }
            )


if __name__ == "__main__":
    unittest.main()
