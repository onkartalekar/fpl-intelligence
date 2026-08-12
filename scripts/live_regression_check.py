#!/usr/bin/env python3
"""Functional regression checks against the real hosted Railway origin (issue #119).

Nothing else in this repo exercises the actual deployed app end-to-end: `tests/*.py` runs
in-process, and `.claude/skills/verify-dashboard/SKILL.md` targets a locally-started server. This
script is the live-environment counterpart -- see `plans/issue-119-live-regression-agent.md` for
the full design rationale, including why "the API response alone is enough" turned out to be
false for `/api/contact` specifically (its email-send failure is deliberately swallowed by issue
#110's durability backstop, so a broken SMTP config there returns `{"status": "ok"}` regardless --
exactly the class of bug that motivated this issue). Mirrors `trigger_scheduled_refresh.py`'s/
`send_deadline_reminder.py`'s shape: env-var-driven config, no secrets ever logged, `--dry-run`
skips the one side-effecting leg (the contact-form send) while still exercising every read/reject
path.

Configuration, entirely environment-variable driven:

- `FPL_INTEL_LIVE_CHECK_BASE_URL` (required): the live dashboard's public origin, e.g.
  `https://web-production-1b285.up.railway.app`. No safe default -- this script only makes sense
  pointed at a real deployment.
- `FPL_INTEL_LIVE_CHECK_PUBLIC_TEAM_ID` (optional, defaults to `364759`, the sample team already
  used throughout this repo's own test suite): a real, public FPL team ID used to exercise the
  "populated" side of issue #108's empty-state gating. Any real team ID works; the default is
  simply a known-good one already exercised elsewhere in this codebase.
- `FPL_INTEL_SERVER_SMTP_USER` / `FPL_INTEL_SERVER_SMTP_PASSWORD` (required unless `--dry-run`):
  the same credentials `reminder_confirmation.py` already uses to *send* the Contact Us
  notification -- `send_contact_email` sends it to this same account (see that module's
  docstring: "there is no separate operator recipient env var, by design"), so these same
  credentials also work to *read* it back over IMAP, verifying actual delivery rather than just
  the API's own response (see the plan doc's (a) finding for why that split is necessary). No new
  secret to provision.
- `FPL_INTEL_LIVE_CHECK_IMAP_HOST` (optional, default `imap.gmail.com`) /
  `FPL_INTEL_LIVE_CHECK_IMAP_PORT` (optional, default `993`).

Every write this script makes uses a reserved, obviously-synthetic team ID
(`SYNTHETIC_TEAM_ID_BASE` and up) well outside FPL's real ~8-digit team ID space, and the one real
email it sends has its body (not subject -- `compose_contact_email` always sets the Subject to
`"FPL Intelligence contact form: <category>"`, regardless of what's submitted) prefixed
`[live-regression-check]` so it's unmistakably distinguishable from real visitor traffic in the
operator's inbox and `contact-submissions.log`.

`--dry-run` skips only the `/api/contact` valid-submission-plus-IMAP-poll leg (the one real,
externally-visible side effect -- an actual email landing in a real inbox); every other check,
including `/api/contact`'s own input-validation rejection paths, still runs.
"""

import argparse
import imaplib
import json
import os
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


class ConfigError(RuntimeError):
    """Malformed or missing configuration. Messages never include credential values."""


class CheckFailure(RuntimeError):
    """One regression check failed. Message is safe to print -- never includes credentials."""


BASE_URL_ENV_VAR = "FPL_INTEL_LIVE_CHECK_BASE_URL"
PUBLIC_TEAM_ID_ENV_VAR = "FPL_INTEL_LIVE_CHECK_PUBLIC_TEAM_ID"
_DEFAULT_PUBLIC_TEAM_ID = 364759

SMTP_USER_ENV_VAR = "FPL_INTEL_SERVER_SMTP_USER"
SMTP_PASSWORD_ENV_VAR = "FPL_INTEL_SERVER_SMTP_PASSWORD"
IMAP_HOST_ENV_VAR = "FPL_INTEL_LIVE_CHECK_IMAP_HOST"
IMAP_PORT_ENV_VAR = "FPL_INTEL_LIVE_CHECK_IMAP_PORT"
_DEFAULT_IMAP_HOST = "imap.gmail.com"
_DEFAULT_IMAP_PORT = 993

# Comfortably outside FPL's real team ID space (server.py's own `_TEAM_ID_RE` caps every team_id
# at 8 digits -- max 99,999,999 -- and real allocated IDs today are well under 11,000,000, FPL's
# approximate total manager count) so a human auditing profiles.db can immediately tell a
# synthetic row from a real visitor's, while every value here still validates as shape-legal.
# Distinct IDs per check that writes, so concurrent/adjacent runs (a scheduled run overlapping a
# manual workflow_dispatch) never collide on the same row.
SYNTHETIC_TEAM_ID_PROFILE = 90000001
SYNTHETIC_TEAM_ID_DRAFT_SQUAD = 90000002
SYNTHETIC_TEAM_ID_LOOKUP_OPT_OUT = 90000003
SYNTHETIC_TEAM_ID_REMINDER_OPT_IN = 90000004

_MARKER = "[live-regression-check]"
_EXPECTED_TABS = (
    "view-decisions", "view-squad", "view-profile", "view-players", "view-fixtures",
    "view-transfers", "view-performance", "view-model", "view-contact",
)
_IMAP_POLL_TIMEOUT_SECONDS = 120
_IMAP_POLL_INTERVAL_SECONDS = 5
_REQUEST_TIMEOUT_SECONDS = 30

# Each of these endpoints' own per-source CooldownLimiter check (server.py) runs BEFORE payload
# validation, unconditionally -- so two back-to-back calls to the *same* write endpoint from this
# script's one calling IP would otherwise have the second always 429 regardless of whether its
# payload is valid or invalid, never actually reaching the check it's meant to exercise. These
# mirror server.py's real cooldown constants (`_PROFILE_WRITE_COOLDOWN_SECONDS`,
# `_LOOKUP_OPT_OUT_COOLDOWN_SECONDS`, `_REMINDER_OPT_IN_COOLDOWN_SECONDS`) plus a small buffer, and
# are `time.sleep()`'d between same-endpoint calls within one check function below.
_PROFILE_WRITE_COOLDOWN_WAIT_SECONDS = 6  # server.py's _PROFILE_WRITE_COOLDOWN_SECONDS = 5
_LOOKUP_OPT_OUT_COOLDOWN_WAIT_SECONDS = 31  # server.py's _LOOKUP_OPT_OUT_COOLDOWN_SECONDS = 30
_REMINDER_OPT_IN_COOLDOWN_WAIT_SECONDS = 31  # server.py's _REMINDER_OPT_IN_COOLDOWN_SECONDS = 30


def _require_base_url():
    raw = os.environ.get(BASE_URL_ENV_VAR)
    if not raw or not raw.strip():
        raise ConfigError(f"{BASE_URL_ENV_VAR} is required (the live dashboard's public origin).")
    return raw.strip().rstrip("/")


def _public_team_id():
    raw = os.environ.get(PUBLIC_TEAM_ID_ENV_VAR)
    if not raw or not raw.strip():
        return _DEFAULT_PUBLIC_TEAM_ID
    try:
        return int(raw.strip())
    except ValueError as error:
        raise ConfigError(f"{PUBLIC_TEAM_ID_ENV_VAR} must be an integer team ID.") from error


def _require_smtp_credentials():
    user = os.environ.get(SMTP_USER_ENV_VAR)
    password = os.environ.get(SMTP_PASSWORD_ENV_VAR)
    if not user or not password:
        raise ConfigError(
            f"{SMTP_USER_ENV_VAR}/{SMTP_PASSWORD_ENV_VAR} are required (used to verify the "
            "Contact Us notification actually arrives via IMAP -- the same account "
            "reminder_confirmation.py already sends it to)."
        )
    return user, password


def _request(method, path, base_url, headers=None, body=None):
    url = f"{base_url}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request_headers = {"Content-Type": "application/json"} if body is not None else {}
    request_headers.update(headers or {})
    request = Request(url, data=data, method=method, headers=request_headers)
    try:
        with urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def check_dashboard_shell(base_url):
    """GET / and /dashboard.html both load, and every one of the nine tabs is present."""
    for path in ("/", "/dashboard.html"):
        status, body = _request("GET", path, base_url)
        if status != 200:
            raise CheckFailure(f"GET {path} returned {status}, expected 200.")
        html = body.decode("utf-8", errors="replace")
        missing = [tab for tab in _EXPECTED_TABS if f'id="{tab}"' not in html]
        if missing:
            raise CheckFailure(f"GET {path} is missing expected tab(s): {', '.join(missing)}.")


def _extract_dashboard_data(html):
    match = re.search(
        r'<script id="dashboard-data" type="application/json">(.*?)</script>', html, re.DOTALL,
    )
    if not match:
        raise CheckFailure("Could not find the embedded __DASHBOARD_DATA__ script tag.")
    return json.loads(match.group(1))


def check_status_endpoint(base_url):
    """GET /api/status returns 200 with the documented shape."""
    status, body = _request("GET", "/api/status", base_url)
    if status != 200:
        raise CheckFailure(f"GET /api/status returned {status}, expected 200.")
    payload = json.loads(body)
    expected_keys = {"status", "refreshing", "generated_at", "fpl_status"}
    missing = expected_keys - set(payload.keys())
    if missing:
        raise CheckFailure(f"GET /api/status is missing key(s): {', '.join(sorted(missing))}.")


def check_refresh_requires_token(base_url):
    """POST /api/refresh without X-Refresh-Token is rejected -- never called WITH a valid token
    here (see the plan doc: it drives one real, globally-cooldown-gated shared refresh, and a
    routine check hitting it would compete with genuine operator use of that same resource)."""
    status, _ = _request("POST", "/api/refresh", base_url)
    if status != 403:
        raise CheckFailure(f"POST /api/refresh with no token returned {status}, expected 403.")


def check_empty_state_and_populated_gating(base_url, public_team_id):
    """Issue #108: no-team_id renders the empty/not_configured manager state; a real public
    team_id renders a populated one. Reads the embedded __DASHBOARD_DATA__ JSON directly rather
    than the static HTML markup, since the empty-state hidden/visible toggle happens client-side
    in dashboard.js, not in the server-rendered HTML itself."""
    status, body = _request("GET", "/", base_url)
    if status != 200:
        raise CheckFailure(f"GET / (no team_id) returned {status}, expected 200.")
    data = _extract_dashboard_data(body.decode("utf-8", errors="replace"))
    connection_status = (data.get("manager") or {}).get("connection_status")
    if connection_status != "not_configured":
        raise CheckFailure(
            f"GET / (no team_id) expected manager.connection_status == 'not_configured', "
            f"got {connection_status!r}."
        )

    status, body = _request("GET", f"/?team_id={public_team_id}", base_url)
    if status != 200:
        raise CheckFailure(f"GET /?team_id={public_team_id} returned {status}, expected 200.")
    data = _extract_dashboard_data(body.decode("utf-8", errors="replace"))
    connection_status = (data.get("manager") or {}).get("connection_status")
    if connection_status == "not_configured":
        raise CheckFailure(
            f"GET /?team_id={public_team_id} expected a populated manager.connection_status, "
            "still got 'not_configured'."
        )


def check_profile_endpoint(base_url):
    """POST /api/profile: a valid synthetic-team submission is accepted; a payload missing the
    required team_id is rejected."""
    status, body = _request(
        "POST", "/api/profile", base_url,
        body={
            "team_id": SYNTHETIC_TEAM_ID_PROFILE, "timezone": "America/New_York",
            "risk_profile": "balanced",
            "confirmed_free_transfers": None,
            "confirmed_free_transfers_event": None,
        },
    )
    if status != 200:
        raise CheckFailure(f"POST /api/profile (valid) returned {status}, expected 200: {body!r}")

    time.sleep(_PROFILE_WRITE_COOLDOWN_WAIT_SECONDS)
    status, _ = _request(
        "POST", "/api/profile", base_url,
        body={"team_id": None, "timezone": "America/New_York", "risk_profile": "balanced"},
    )
    if status != 400:
        raise CheckFailure(f"POST /api/profile (missing team_id) returned {status}, expected 400.")


def check_draft_squad_endpoint(base_url):
    """POST /api/draft-squad: clearing a draft (player_ids: null) is always valid, regardless of
    live squad-legality data, so it's a safe accept-path check with no legality dependency. An
    out-of-range team_id is rejected."""
    status, body = _request(
        "POST", "/api/draft-squad", base_url,
        body={"team_id": SYNTHETIC_TEAM_ID_DRAFT_SQUAD, "player_ids": None},
    )
    if status != 200:
        raise CheckFailure(f"POST /api/draft-squad (valid) returned {status}, expected 200: {body!r}")

    time.sleep(_PROFILE_WRITE_COOLDOWN_WAIT_SECONDS)
    status, _ = _request(
        "POST", "/api/draft-squad", base_url, body={"team_id": -1, "player_ids": None},
    )
    if status != 400:
        raise CheckFailure(f"POST /api/draft-squad (invalid team_id) returned {status}, expected 400.")


def check_lookup_opt_out_endpoint(base_url):
    """POST /api/lookup-opt-out: a valid first-claim PIN is accepted; a too-short PIN is
    rejected. Deliberately does not un-claim/reset the opt-out afterwards -- this endpoint's own
    per-source cooldown (30s) is far longer than the profile-write endpoints', so a same-run
    cleanup call would cost real wall-clock time for no benefit: the reserved synthetic team ID
    is never a real visitor's, so it staying opted-out between runs is harmless."""
    status, body = _request(
        "POST", "/api/lookup-opt-out", base_url,
        body={
            "team_id": SYNTHETIC_TEAM_ID_LOOKUP_OPT_OUT, "opted_out": True,
            "pin": "livecheck1",
        },
    )
    if status != 200:
        raise CheckFailure(f"POST /api/lookup-opt-out (valid) returned {status}, expected 200: {body!r}")

    time.sleep(_LOOKUP_OPT_OUT_COOLDOWN_WAIT_SECONDS)
    status, _ = _request(
        "POST", "/api/lookup-opt-out", base_url,
        body={"team_id": SYNTHETIC_TEAM_ID_LOOKUP_OPT_OUT, "opted_out": True, "pin": "abc"},
    )
    if status != 400:
        raise CheckFailure(f"POST /api/lookup-opt-out (short PIN) returned {status}, expected 400.")


def check_reminder_opt_in_endpoint(base_url, imap_user):
    """POST /api/reminder-opt-in: a valid 'enable' submission is accepted or -- per the plan
    doc's (a) finding -- surfaces as an error response if the SMTP send itself fails (this
    endpoint propagates send failures, unlike /api/contact, so the API response alone is a
    meaningful check here). Points the confirmation email at our own controlled mailbox
    (`imap_user`, the same account already used to verify /api/contact's delivery) rather than an
    arbitrary address, so this check never emails a third party. An invalid action is rejected.
    """
    status, body = _request(
        "POST", "/api/reminder-opt-in", base_url,
        body={
            "team_id": SYNTHETIC_TEAM_ID_REMINDER_OPT_IN, "action": "enable",
            "email": imap_user, "lead_hours": 3,
        },
    )
    if status not in (200, 429, 502):
        # 429 (per-team confirm-send cooldown) and 502 (a real, currently-live SMTP failure --
        # exactly the failure mode this whole issue exists to catch) are both meaningful,
        # non-500 signals, not false failures of this check itself. Anything else is unexpected.
        raise CheckFailure(
            f"POST /api/reminder-opt-in (valid enable) returned {status}, expected 200/429/502: {body!r}"
        )
    if status == 502:
        raise CheckFailure(
            f"POST /api/reminder-opt-in (valid enable) reported an SMTP send failure: {body!r}"
        )
    # No same-run cleanup call: the endpoint's own per-source cooldown (30s) gates every action
    # uniformly before validation even runs (same reasoning as check_lookup_opt_out_endpoint
    # above), so a same-run "decline" would just cost an extra 30s wait for no real benefit --
    # the reserved synthetic team ID is never a real visitor's, so a lingering pending
    # confirmation is harmless.

    time.sleep(_REMINDER_OPT_IN_COOLDOWN_WAIT_SECONDS)
    status, _ = _request(
        "POST", "/api/reminder-opt-in", base_url,
        body={"team_id": SYNTHETIC_TEAM_ID_REMINDER_OPT_IN, "action": "not_a_real_action"},
    )
    if status != 400:
        raise CheckFailure(
            f"POST /api/reminder-opt-in (invalid action) returned {status}, expected 400."
        )


def check_contact_endpoint_rejects_invalid(base_url):
    """POST /api/contact: an invalid category is rejected. Runs unconditionally, including in
    --dry-run, since it has no side effect."""
    status, _ = _request(
        "POST", "/api/contact", base_url,
        body={"category": "not_a_real_category", "message": "test"},
    )
    if status != 400:
        raise CheckFailure(f"POST /api/contact (invalid category) returned {status}, expected 400.")


def _imap_poll_for_marker(imap_host, imap_port, imap_user, imap_password, marker, run_id):
    """Poll the mailbox over IMAP for an email containing `marker` and `run_id` in the body,
    within `_IMAP_POLL_TIMEOUT_SECONDS`. This is the only way to verify /api/contact's
    notification email actually arrived -- see the module docstring and plan doc for why the
    API's own response can't tell us this.

    Searches the BODY, not the Subject: `compose_contact_email` (`reminder_confirmation.py`)
    always sets the Subject to `"FPL Intelligence contact form: <category>"` -- the marker and
    run_id are only ever in the message body (the submitted "Message:" field), never the Subject.
    An earlier version of this function searched SUBJECT, which could never have matched
    regardless of how long it waited -- a real, confirmed false-negative in this check itself,
    not a delivery problem. Caught live: the notification genuinely arrived (visible in the real
    inbox) while this check still reported it missing.
    """
    deadline = time.monotonic() + _IMAP_POLL_TIMEOUT_SECONDS
    last_error = None
    while time.monotonic() < deadline:
        try:
            connection = imaplib.IMAP4_SSL(imap_host, imap_port)
            try:
                connection.login(imap_user, imap_password)
                connection.select("INBOX")
                status, message_ids = connection.search(None, "BODY", f'"{marker}"')
                if status == "OK":
                    for message_id in message_ids[0].split():
                        status, msg_data = connection.fetch(message_id, "(BODY[TEXT])")
                        if status != "OK" or not msg_data or not msg_data[0]:
                            continue
                        raw_body = msg_data[0][1]
                        decoded = raw_body.decode("utf-8", errors="replace")
                        if run_id in decoded:
                            return True
            finally:
                connection.logout()
        except (imaplib.IMAP4.error, OSError) as error:
            last_error = error
        time.sleep(_IMAP_POLL_INTERVAL_SECONDS)
    if last_error is not None:
        raise CheckFailure(f"IMAP poll for the Contact Us notification failed: {last_error!r}")
    return False


def check_contact_endpoint_delivery(base_url, imap_host, imap_port, imap_user, imap_password):
    """POST /api/contact with a valid, clearly-marked submission, then poll IMAP to confirm the
    operator notification actually arrived -- the one check in this script that verifies real
    email delivery, not just an API response (see module docstring: /api/contact's own response
    can never reveal an SMTP failure, by issue #110's deliberate design)."""
    run_id = f"run-{int(time.time())}"
    status, body = _request(
        "POST", "/api/contact", base_url,
        body={
            "category": "other",
            "message": f"{_MARKER} {run_id} -- automated live regression check, safe to ignore/delete.",
        },
    )
    if status != 200:
        raise CheckFailure(f"POST /api/contact (valid) returned {status}, expected 200: {body!r}")

    arrived = _imap_poll_for_marker(imap_host, imap_port, imap_user, imap_password, _MARKER, run_id)
    if not arrived:
        raise CheckFailure(
            f"Contact Us notification for {run_id!r} did not arrive within "
            f"{_IMAP_POLL_TIMEOUT_SECONDS}s -- the endpoint's own '{{\"status\": \"ok\"}}' response "
            "cannot detect this on its own (issue #110's durability backstop swallows send "
            "failures by design), which is exactly the class of bug this check exists to catch."
        )


def run(base_url, public_team_id, dry_run, imap_config=None):
    """Run every check, collecting failures rather than stopping at the first one, so one run
    reports the full picture. Returns (passed, failed) check-name lists."""
    checks = [
        ("dashboard_shell", lambda: check_dashboard_shell(base_url)),
        ("status_endpoint", lambda: check_status_endpoint(base_url)),
        ("refresh_requires_token", lambda: check_refresh_requires_token(base_url)),
        ("empty_state_and_populated_gating",
         lambda: check_empty_state_and_populated_gating(base_url, public_team_id)),
        ("profile_endpoint", lambda: check_profile_endpoint(base_url)),
        ("draft_squad_endpoint", lambda: check_draft_squad_endpoint(base_url)),
        ("lookup_opt_out_endpoint", lambda: check_lookup_opt_out_endpoint(base_url)),
        ("contact_endpoint_rejects_invalid", lambda: check_contact_endpoint_rejects_invalid(base_url)),
    ]
    if imap_config is not None:
        checks.append((
            "reminder_opt_in_endpoint",
            lambda: check_reminder_opt_in_endpoint(base_url, imap_config["user"]),
        ))
    if imap_config is not None and not dry_run:
        checks.append((
            "contact_endpoint_delivery",
            lambda: check_contact_endpoint_delivery(
                base_url, imap_config["host"], imap_config["port"],
                imap_config["user"], imap_config["password"],
            ),
        ))

    passed, failed = [], []
    for name, check in checks:
        try:
            check()
            passed.append(name)
            print(f"PASS: {name}")
        except (CheckFailure, HTTPError, URLError, OSError) as error:
            failed.append(name)
            print(f"FAIL: {name}: {error}", file=sys.stderr)
    return passed, failed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Skip only the /api/contact real-email-delivery check (its one side-effecting leg). "
             "Every other check, including /api/contact's own input-validation rejection, still runs.",
    )
    args = parser.parse_args(argv)

    try:
        base_url = _require_base_url()
        public_team_id = _public_team_id()
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 1

    imap_config = None
    try:
        imap_user, imap_password = _require_smtp_credentials()
        imap_config = {
            "user": imap_user,
            "password": imap_password,
            "host": os.environ.get(IMAP_HOST_ENV_VAR, "").strip() or _DEFAULT_IMAP_HOST,
            "port": int(os.environ.get(IMAP_PORT_ENV_VAR, "").strip() or _DEFAULT_IMAP_PORT),
        }
    except ConfigError as error:
        if args.dry_run:
            print(f"Note: {error} (continuing without IMAP-dependent checks in --dry-run)", file=sys.stderr)
        else:
            print(f"Configuration error: {error}", file=sys.stderr)
            return 1

    passed, failed = run(base_url, public_team_id, args.dry_run, imap_config=imap_config)
    print(f"{len(passed)} passed, {len(failed)} failed.")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
