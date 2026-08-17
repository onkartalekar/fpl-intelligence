import unittest

from fpl_intel.sources.transfers import OFFICIAL_CLUB_DOMAINS, canonical_club, normalize_transfer


class CanonicalClubTests(unittest.TestCase):
    def test_reconciles_pl_transfer_centre_names_with_bootstrap_team_names(self):
        # (PL transfer-centre name, FPL bootstrap team name) pairs that
        # differ in spelling for the same club -- both sides of each pair
        # must canonicalize to the same string.
        pairs = [
            ("AFC Bournemouth", "Bournemouth"),
            ("Brighton & Hove Albion", "Brighton"),
            ("Ipswich", "Ipswich Town"),
            ("Leeds United", "Leeds"),
            ("Nottingham Forest", "Nott'm Forest"),
            ("Tottenham Hotspur", "Spurs"),
        ]
        for transfer_centre_name, bootstrap_name in pairs:
            with self.subTest(club=transfer_centre_name):
                self.assertEqual(canonical_club(transfer_centre_name), canonical_club(bootstrap_name))

    def test_is_case_and_whitespace_insensitive(self):
        self.assertEqual(canonical_club("  Arsenal "), canonical_club("arsenal"))


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
