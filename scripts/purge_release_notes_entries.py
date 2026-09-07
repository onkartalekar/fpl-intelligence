#!/usr/bin/env python3
"""Delete named "What's New" entries from the live dashboard (issue #300).

Operator break-glass tool, not part of any schedule -- the counterpart to
`publish_release_notes.py` for the one job that one can't do: *removing* an already-published
entry. It exists for a one-time cleanup -- issue #300's self-perpetuating "we archived the
release notes" junk entries, generated daily before that issue filtered the workflow's own
archival PRs out of `fetch_merged_prs`. The junk archive Markdown under `release-notes/` is
deleted in git directly; this script is only for the other copy, the one on Railway's volume
that `POST /api/release-notes` is the sole writer of and that no git operation can reach.

`data/release-notes.json` lives on the Railway persistent volume, so -- exactly like
`trigger_scheduled_refresh.py` and every other GitHub-Actions/laptop-hosted script in this repo
(see ARCHITECTURE.md's "Why the workflows call back over HTTP") -- the only way to mutate it is
an HTTP call to the running server. This posts `{"delete": [...]}` to `/api/release-notes`,
which `release_notes_handlers.make_handle_release_notes` routes to `release_notes.delete_entries`.

Usage:

    FPL_INTEL_DASHBOARD_BASE_URL=https://... FPL_INTEL_REFRESH_TOKEN=... \
        python3 scripts/purge_release_notes_entries.py 2026-08-20 2026-08-22 2026-08-23

Configuration, environment-variable driven, same meaning and same values as
`trigger_scheduled_refresh.py`'s:

- `FPL_INTEL_REFRESH_TOKEN` (required unless --dry-run): the operator bearer token
  `/api/release-notes` requires (issue #27), same as the daily publish job.
- `FPL_INTEL_DASHBOARD_BASE_URL` (required unless --dry-run): the live dashboard's public origin.

`--dry-run` prints the request it would send and exits without contacting the server or needing
any secret.
"""

import argparse
from datetime import date as _date
import json
import os
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REFRESH_TOKEN_ENV_VAR = "FPL_INTEL_REFRESH_TOKEN"
DASHBOARD_BASE_URL_ENV_VAR = "FPL_INTEL_DASHBOARD_BASE_URL"

_REQUEST_TIMEOUT_SECONDS = 30


class ConfigError(RuntimeError):
    """Malformed or missing configuration. Messages never include the refresh token."""


def _dashboard_base_url():
    raw = os.environ.get(DASHBOARD_BASE_URL_ENV_VAR)
    if not raw or not raw.strip():
        raise ConfigError(f"{DASHBOARD_BASE_URL_ENV_VAR} is required and was not set.")
    return raw.strip().rstrip("/")


def _refresh_token():
    raw = os.environ.get(REFRESH_TOKEN_ENV_VAR)
    if not raw or not raw.strip():
        raise ConfigError(f"{REFRESH_TOKEN_ENV_VAR} is required and was not set.")
    return raw.strip()


def _clean_dates(raw_dates):
    """De-duplicate and sort `raw_dates`, rejecting anything that isn't a YYYY-MM-DD date -- the
    same shape check `release_notes.validate_delete_payload` applies server-side, done here too so
    a typo fails before any network call rather than as a 400."""
    cleaned = []
    for raw_date in raw_dates:
        try:
            _date.fromisoformat(raw_date)
        except ValueError as error:
            raise ConfigError(f"'{raw_date}' is not a valid YYYY-MM-DD date") from error
        cleaned.append(raw_date)
    if not cleaned:
        raise ConfigError("at least one date to delete is required")
    return sorted(set(cleaned))


def purge_entries(base_url, token, dates, timeout=_REQUEST_TIMEOUT_SECONDS):
    """POST `{"delete": dates}` to `/api/release-notes`. Returns the server's parsed JSON body
    (`{"status": "ok", "deleted": [...], "not_found": [...]}`). Raises RuntimeError with the
    server's own status/message on failure."""
    request = Request(
        f"{base_url}/api/release-notes",
        data=json.dumps({"delete": dates}).encode("utf-8"),
        method="POST",
        headers={"X-Refresh-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"purge request failed: HTTP {error.code} {body}") from error
    except URLError as error:
        raise RuntimeError(f"purge request failed: {error.reason}") from error


def run(dry_run, base_url, token, dates):
    """Core run, factored out of `main` so tests can drive it without argv/env parsing.
    `base_url`/`token` are None in `--dry-run` mode and are never read on that path."""
    if dry_run:
        print(f"dry-run: would POST /api/release-notes {{\"delete\": {json.dumps(dates)}}}")
        return 0

    result = purge_entries(base_url, token, dates)
    deleted = result.get("deleted") or []
    not_found = result.get("not_found") or []
    print(f"purged {len(deleted)} entr{'y' if len(deleted) == 1 else 'ies'}: {deleted or '(none)'}")
    if not_found:
        print(f"no stored entry for (already gone): {not_found}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "dates", nargs="+", metavar="YYYY-MM-DD",
        help="One or more entry dates to delete from the live dashboard.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the request that would be sent and exit, without contacting the server or "
             "requiring FPL_INTEL_DASHBOARD_BASE_URL/FPL_INTEL_REFRESH_TOKEN.",
    )
    args = parser.parse_args(argv)

    try:
        dates = _clean_dates(args.dates)
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 1

    base_url = token = None
    if not args.dry_run:
        try:
            base_url = _dashboard_base_url()
            token = _refresh_token()
        except ConfigError as error:
            print(f"Configuration error: {error}", file=sys.stderr)
            return 1

    try:
        return run(args.dry_run, base_url, token, dates)
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
