#!/usr/bin/env python3
"""Strip leftover archival-bookkeeping changes from the live "What's New" entries (issue #303).

Operator break-glass tool, not part of any schedule -- the companion to
`purge_release_notes_entries.py`. That one deletes whole junk entries (issue #300's pure
self-archival days); this one reaches the other half of the mess: entries that mixed real
changelog content with a stray `[Chore]` "Archive ... release notes" change, generated daily
before issue #300 filtered `release-notes.yml`'s own archival PR out of `fetch_merged_prs`.
`delete_entries` is whole-entry-only, so the purge script can't touch those without destroying
the real content alongside them.

This POSTs `{"prune_archival_changes": true}` to `/api/release-notes`, which
`release_notes_handlers.make_handle_release_notes` routes to
`release_notes.prune_archival_changes`: it strips every `is_archival_bookkeeping_change` from
every stored entry, drops any entry left with zero changes, and returns
`{"removed": [{"date", "title"}, ...], "modified": [...dates], "emptied": [...dates]}`.

`data/release-notes.json` lives on the Railway persistent volume, so -- exactly like
`trigger_scheduled_refresh.py`, `purge_release_notes_entries.py`, and every other
GitHub-Actions/laptop-hosted script in this repo (see ARCHITECTURE.md's "Why the workflows call
back over HTTP") -- the only way to mutate it is an HTTP call to the running server.

Usage:

    FPL_INTEL_DASHBOARD_BASE_URL=https://... FPL_INTEL_REFRESH_TOKEN=... \
        python3 scripts/prune_release_notes_archival_changes.py

Configuration, environment-variable driven, same meaning and same values as
`purge_release_notes_entries.py`'s:

- `FPL_INTEL_REFRESH_TOKEN` (required unless --dry-run): the operator bearer token
  `/api/release-notes` requires (issue #27), same as the daily publish job.
- `FPL_INTEL_DASHBOARD_BASE_URL` (required unless --dry-run): the live dashboard's public origin.

`--dry-run` prints the request it would send and exits without contacting the server or needing
any secret. There is no GET-entries endpoint, so a dry run can't preview which changes match --
a live run prints back every removed change (date + title) plus the modified/emptied entry lists.
"""

import argparse
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


def prune_changes(base_url, token, timeout=_REQUEST_TIMEOUT_SECONDS):
    """POST `{"prune_archival_changes": true}` to `/api/release-notes`. Returns the server's
    parsed JSON body (`{"status": "ok", "removed": [{"date", "title"}, ...], "modified": [...],
    "emptied": [...]}`). Raises RuntimeError with the server's own status/message on failure."""
    request = Request(
        f"{base_url}/api/release-notes",
        data=json.dumps({"prune_archival_changes": True}).encode("utf-8"),
        method="POST",
        headers={"X-Refresh-Token": token, "Content-Type": "application/json"},
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return json.loads(response.read())
    except HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"prune request failed: HTTP {error.code} {body}") from error
    except URLError as error:
        raise RuntimeError(f"prune request failed: {error.reason}") from error


def run(dry_run, base_url, token):
    """Core run, factored out of `main` so tests can drive it without argv/env parsing.
    `base_url`/`token` are None in `--dry-run` mode and are never read on that path."""
    if dry_run:
        print('dry-run: would POST /api/release-notes {"prune_archival_changes": true}')
        return 0

    result = prune_changes(base_url, token)
    removed = result.get("removed") or []
    modified = result.get("modified") or []
    emptied = result.get("emptied") or []
    print(f"pruned {len(removed)} archival change{'' if len(removed) == 1 else 's'}")
    for item in removed:
        print(f"  - {item.get('date')}: {item.get('title')}")
    print(f"modified entries (kept, junk stripped): {modified or '(none)'}")
    print(f"emptied entries (deleted, nothing left): {emptied or '(none)'}")
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the request that would be sent and exit, without contacting the server or "
             "requiring FPL_INTEL_DASHBOARD_BASE_URL/FPL_INTEL_REFRESH_TOKEN.",
    )
    args = parser.parse_args(argv)

    base_url = token = None
    if not args.dry_run:
        try:
            base_url = _dashboard_base_url()
            token = _refresh_token()
        except ConfigError as error:
            print(f"Configuration error: {error}", file=sys.stderr)
            return 1

    try:
        return run(args.dry_run, base_url, token)
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
