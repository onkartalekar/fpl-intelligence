"""Confirmed transfer normalization."""

from urllib.parse import urlparse

_REQUIRED = ("player", "from_club", "to_club", "announced_at", "source_url", "source_type")
_FIRST_PARTY_TYPES = {"official_premier_league", "official_club"}
_PREMIER_LEAGUE_DOMAINS = {"premierleague.com", "fantasy.premierleague.com"}
OFFICIAL_CLUB_DOMAINS = {
    "arsenal.com", "avfc.co.uk", "afcb.co.uk", "brentfordfc.com",
    "brightonandhovealbion.com", "ccfc.co.uk", "chelseafc.com",
    "cpfc.co.uk", "evertonfc.com", "fulhamfc.com", "wearehullcity.co.uk",
    "itfc.co.uk", "leedsunited.com", "liverpoolfc.com", "mancity.com",
    "manutd.com", "newcastleunited.com", "nottinghamforest.co.uk",
    "safc.com", "tottenhamhotspur.com",
}

# Normalizes club-name strings from different sources (the PL transfer
# centre's own names, the FPL bootstrap feed's shorter/different names,
# and free-text confirmed-transfer records) to a single comparable form.
CLUB_ALIASES = {
    "spurs": "tottenham hotspur",
    "brighton": "brighton & hove albion",
    "afc bournemouth": "bournemouth",
    "nott'm forest": "nottingham forest",
    "man utd": "manchester united",
    "man city": "manchester city",
    "newcastle": "newcastle united",
    "ipswich town": "ipswich",
    "leeds": "leeds united",
}


def canonical_club(name):
    key = (name or "").casefold().strip()
    return CLUB_ALIASES.get(key, key)


def is_trusted_https_url(url, trusted_domains):
    parsed = urlparse(url)
    hostname = (parsed.hostname or "").lower().rstrip(".")
    return (
        parsed.scheme == "https"
        and bool(hostname)
        and parsed.username is None
        and parsed.password is None
        and any(hostname == domain or hostname.endswith("." + domain) for domain in trusted_domains)
    )


def normalize_transfer(raw):
    missing = [field for field in _REQUIRED if not raw.get(field)]
    if missing:
        raise ValueError("Missing required transfer fields: " + ", ".join(missing))
    if raw["source_type"] not in _FIRST_PARTY_TYPES:
        raise ValueError("Transfer confirmation must use a first-party source")
    if raw["source_type"] == "official_premier_league" and not is_trusted_https_url(
        raw["source_url"], _PREMIER_LEAGUE_DOMAINS
    ):
        raise ValueError("Premier League source must use a trusted HTTPS URL")
    if raw["source_type"] == "official_club":
        expected_domain = raw.get("official_club_domain", "").lower()
        if expected_domain.startswith("www."):
            expected_domain = expected_domain[4:]
        if (
            expected_domain not in OFFICIAL_CLUB_DOMAINS
            or not is_trusted_https_url(raw["source_url"], {expected_domain})
        ):
            raise ValueError("Source URL does not match the declared official club domain")

    record = dict(raw)
    record["verification_status"] = "confirmed_first_party"
    record.setdefault("fpl_reconciliation_status", "pending_new_season_fpl")
    return record
