"""SQLite-backed per-team profile storage, keyed by FPL team ID (issue #45).

No credential system: a manager's FPL data is already public via FPL's own
API, so the team ID itself is the tenant key -- see
plans/issue-45-per-team-profile-storage.md for the full design. This is
deliberately separate from `config/user-profile.json`, which keeps its own,
narrower role feeding `refresh.py`'s single-team forecast-accuracy history
tracking (see issue #64) -- unrelated to and untouched by this module.
"""

from contextlib import closing
from pathlib import Path
import json
import sqlite3


_DEFAULT_TIMEZONE = "America/New_York"
_DEFAULT_RISK_PROFILE = "balanced"


_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    team_id INTEGER PRIMARY KEY,
    timezone TEXT NOT NULL,
    risk_profile TEXT NOT NULL,
    confirmed_free_transfers INTEGER,
    confirmed_free_transfers_event INTEGER,
    email TEXT,
    draft_squad TEXT,
    opted_out INTEGER,
    pin_hash TEXT,
    goal TEXT,
    reminder_status TEXT,
    reminder_lead_hours INTEGER,
    reminder_pending_email TEXT,
    reminder_confirmation_token_hash TEXT,
    reminder_confirmation_expires_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_COLUMNS = (
    "team_id, timezone, risk_profile, confirmed_free_transfers, "
    "confirmed_free_transfers_event, email, draft_squad, opted_out, pin_hash, goal, "
    "reminder_status, reminder_lead_hours, reminder_pending_email, "
    "reminder_confirmation_token_hash, reminder_confirmation_expires_at, "
    "created_at, updated_at"
)

# Defaults used only when a team's very first row is created by the lookup opt-out endpoint
# (issue #62) rather than by a normal `/api/profile` save -- mirrors
# `server.py`'s `_DEFAULT_VISITOR_PROFILE`, since a manager may toggle opt-out before ever
# saving their own timezone/risk-profile preferences.
_DEFAULT_TIMEZONE = "America/New_York"
_DEFAULT_RISK_PROFILE = "balanced"
# `goal` (issue #78) is a manager's stated season objective -- unlike `timezone`/`risk_profile`
# it's nullable at the schema level (so pre-existing rows need no migration backfill), but every
# *read* still resolves to a non-null value via this default, applied in `_row_to_dict` below.
# Metadata only for now: `goal` does not drive `risk_profile` selection, model behavior, or any
# recommendation/copy -- it is purely stored and displayed on the profile view.
_DEFAULT_GOAL = "top_50k"


def _connect(db_path):
    """Open (creating if needed) the profiles database, with the schema applied."""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(_SCHEMA)
    return connection


def _row_to_dict(row):
    if row is None:
        return None
    return {
        "team_id": row[0],
        "timezone": row[1],
        "risk_profile": row[2],
        "confirmed_free_transfers": row[3],
        "confirmed_free_transfers_event": row[4],
        "email": row[5],
        "draft_squad": json.loads(row[6]) if row[6] else None,
        "opted_out": bool(row[7]) if row[7] is not None else None,
        "pin_hash": row[8],
        # Read-time default substitution (issue #78): resolves NULL to `_DEFAULT_GOAL` for rows
        # that predate this column, or were created by a path that never sets it
        # (`save_draft_squad`, `set_lookup_opt_out`), so every caller of `load_profile` always
        # sees a resolved goal without needing its own `or "top_50k"` fallback.
        "goal": row[9] or _DEFAULT_GOAL,
        # Issue #79: `reminder_status` is deliberately left as NULL/None (never defaulted, unlike
        # `goal` above) -- None means "never decided", a real, distinct state from any of
        # 'pending'/'enabled'/'declined', driving the tri-state attention-banner nudge in
        # `dashboard.js`. `reminder_lead_hours`/`reminder_pending_email`/the token columns are
        # similarly left as whatever is actually stored, with no read-time substitution.
        "reminder_status": row[10],
        "reminder_lead_hours": row[11],
        "reminder_pending_email": row[12],
        "reminder_confirmation_token_hash": row[13],
        "reminder_confirmation_expires_at": row[14],
        "created_at": row[15],
        "updated_at": row[16],
    }


def load_profile(db_path, team_id):
    """Return the saved profile for team_id, or None if it has never been saved."""
    with closing(_connect(db_path)) as connection:
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM profiles WHERE team_id = ?", (team_id,)
        ).fetchone()
    return _row_to_dict(row)


def list_team_ids(db_path):
    """Return every team ID with a saved profile, ascending.

    Used by `refresh.py`'s per-team `manager_picks` collection loop (issue #64) to discover which
    teams now have season-long forecast-accuracy tracking to maintain, beyond the single team
    `config/user-profile.json` used to hardcode.
    """
    with closing(_connect(db_path)) as connection:
        rows = connection.execute("SELECT team_id FROM profiles ORDER BY team_id").fetchall()
    return [row[0] for row in rows]


def save_profile(
    db_path,
    team_id,
    timezone,
    risk_profile,
    confirmed_free_transfers,
    confirmed_free_transfers_event,
    now,
    goal,
):
    """Create or update the saved profile for team_id. Returns the resulting row.

    `email` is never written here -- it stays whatever it already was (None for a
    brand-new row), populated only by #55's explicit reminder opt-in, never implied
    by saving other preferences. `draft_squad` (issue #61) is preserved the same way,
    written only by `save_draft_squad`, never implied by an unrelated profile save.

    `goal` (issue #78) is a real, settable field here, required the same way
    `timezone`/`risk_profile` already are -- unlike `email`/`draft_squad` above, the profile
    form is the intended write path for it, so every call always overwrites what was stored.
    Metadata only for now: does not drive `risk_profile` selection or any model output.
    """
    with closing(_connect(db_path)) as connection:
        with connection:
            existing = connection.execute(
                "SELECT created_at, email, draft_squad FROM profiles WHERE team_id = ?",
                (team_id,),
            ).fetchone()
            created_at = existing[0] if existing else now
            email = existing[1] if existing else None
            draft_squad = existing[2] if existing else None
            connection.execute(
                """
                INSERT INTO profiles
                    (team_id, timezone, risk_profile, confirmed_free_transfers,
                     confirmed_free_transfers_event, email, draft_squad, goal, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id) DO UPDATE SET
                    timezone = excluded.timezone,
                    risk_profile = excluded.risk_profile,
                    confirmed_free_transfers = excluded.confirmed_free_transfers,
                    confirmed_free_transfers_event = excluded.confirmed_free_transfers_event,
                    goal = excluded.goal,
                    updated_at = excluded.updated_at
                """,
                (
                    team_id, timezone, risk_profile, confirmed_free_transfers,
                    confirmed_free_transfers_event, email, draft_squad, goal, created_at, now,
                ),
            )
    return load_profile(db_path, team_id)


def save_draft_squad(db_path, team_id, draft_squad_ids, now):
    """Create or update the saved preseason draft squad for team_id (issue #61).

    Returns the resulting row. Mirrors `save_profile`'s preserve-what-you-don't-touch
    approach: only `draft_squad` is written here. Every other already-saved field is left
    exactly as it was; a brand-new row (no profile ever saved for this team_id) is seeded
    with the same defaults the dashboard already applies for an unconfigured visitor, so a
    manager can declare a draft squad before ever touching the profile form.

    `draft_squad_ids` is a list of 15 element IDs, or None to clear a previously saved
    draft (e.g. once the manager no longer wants the preseason feedback shown).

    `goal` (issue #78) is deliberately absent from the INSERT column list below, the same way
    `opted_out`/`pin_hash` already are -- an existing row's `goal` is preserved automatically by
    `ON CONFLICT DO UPDATE` (a column left out of the SET clause is never touched), and a
    brand-new row simply gets SQL NULL, which `_row_to_dict`'s read-time default resolves to
    `_DEFAULT_GOAL` on the next read. No explicit carry-forward needed.
    """
    with closing(_connect(db_path)) as connection:
        with connection:
            existing = connection.execute(
                """
                SELECT created_at, timezone, risk_profile, confirmed_free_transfers,
                       confirmed_free_transfers_event, email
                FROM profiles WHERE team_id = ?
                """,
                (team_id,),
            ).fetchone()
            created_at = existing[0] if existing else now
            timezone = existing[1] if existing else _DEFAULT_TIMEZONE
            risk_profile = existing[2] if existing else _DEFAULT_RISK_PROFILE
            confirmed_free_transfers = existing[3] if existing else None
            confirmed_free_transfers_event = existing[4] if existing else None
            email = existing[5] if existing else None
            connection.execute(
                """
                INSERT INTO profiles
                    (team_id, timezone, risk_profile, confirmed_free_transfers,
                     confirmed_free_transfers_event, email, draft_squad, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id) DO UPDATE SET
                    draft_squad = excluded.draft_squad,
                    updated_at = excluded.updated_at
                """,
                (
                    team_id, timezone, risk_profile, confirmed_free_transfers,
                    confirmed_free_transfers_event, email,
                    json.dumps(draft_squad_ids) if draft_squad_ids is not None else None,
                    created_at, now,
                ),
            )
    return load_profile(db_path, team_id)


def load_pin_hash(db_path, team_id):
    """Return the stored `pin_hash` for team_id, or None if no PIN has ever been claimed
    (including when team_id has no row at all yet). Used by issue #62's opt-out endpoint to
    decide between a first-claim ("no PIN exists yet, any PIN meeting the shape rule sets it")
    and a re-claim (submitted PIN must match) without needing a full profile read.
    """
    with closing(_connect(db_path)) as connection:
        row = connection.execute(
            "SELECT pin_hash FROM profiles WHERE team_id = ?", (team_id,)
        ).fetchone()
    return row[0] if row else None


def set_lookup_opt_out(db_path, team_id, opted_out, pin_hash, now):
    """Create or update team_id's `opted_out` flag and `pin_hash` (issue #62).

    Deliberately independent of `save_profile`'s six live-manager-preference fields -- a
    team's first opt-out toggle can happen before that team has ever saved a profile at all,
    in which case a row is created here using the same defaults
    `server._default_visitor_profile_action` would otherwise synthesize on read. Only
    `opted_out`/`pin_hash`/`updated_at` are overwritten on an existing row; every other column
    (including `email`, never touched outside its own #55 opt-in) is preserved untouched.

    `goal` (issue #78), like `draft_squad` before it, is left out of the INSERT column list
    below entirely -- `ON CONFLICT DO UPDATE` never touches a column absent from its SET clause,
    so an existing row's `goal` survives this write untouched, and a brand-new row gets SQL
    NULL, resolved to `_DEFAULT_GOAL` by `_row_to_dict` on the next read.
    """
    with closing(_connect(db_path)) as connection:
        with connection:
            existing = connection.execute(
                "SELECT created_at, timezone, risk_profile, confirmed_free_transfers, "
                "confirmed_free_transfers_event, email FROM profiles WHERE team_id = ?",
                (team_id,),
            ).fetchone()
            if existing:
                (
                    created_at, timezone_value, risk_profile, confirmed_free_transfers,
                    confirmed_free_transfers_event, email,
                ) = existing
            else:
                created_at = now
                timezone_value = _DEFAULT_TIMEZONE
                risk_profile = _DEFAULT_RISK_PROFILE
                confirmed_free_transfers = None
                confirmed_free_transfers_event = None
                email = None
            connection.execute(
                """
                INSERT INTO profiles
                    (team_id, timezone, risk_profile, confirmed_free_transfers,
                     confirmed_free_transfers_event, email, opted_out, pin_hash,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id) DO UPDATE SET
                    opted_out = excluded.opted_out,
                    pin_hash = excluded.pin_hash,
                    updated_at = excluded.updated_at
                """,
                (
                    team_id, timezone_value, risk_profile, confirmed_free_transfers,
                    confirmed_free_transfers_event, email, 1 if opted_out else 0, pin_hash,
                    created_at, now,
                ),
            )
    return load_profile(db_path, team_id)


def set_reminder_pending(db_path, team_id, pending_email, lead_hours, token_hash, expires_at, now):
    """Record an in-flight reminder-enable request (issue #79).

    Called only after the caller (`server.py`) has already sent the confirmation email
    successfully -- SMTP send happens before this write, never after, so a pending row here
    always corresponds to an email that was actually dispatched. Sets `reminder_status` to
    `'pending'` and stores everything `confirm_reminder` later needs to validate and promote
    the request. `email` itself is deliberately untouched: it only ever holds a *confirmed*
    address, never this unconfirmed one. Mirrors `set_lookup_opt_out`'s create-or-preserve
    pattern -- a team's first reminder request can happen before that team has ever saved a
    profile at all.
    """
    with closing(_connect(db_path)) as connection:
        with connection:
            existing = connection.execute(
                "SELECT created_at, timezone, risk_profile, confirmed_free_transfers, "
                "confirmed_free_transfers_event, email FROM profiles WHERE team_id = ?",
                (team_id,),
            ).fetchone()
            if existing:
                (
                    created_at, timezone_value, risk_profile, confirmed_free_transfers,
                    confirmed_free_transfers_event, email,
                ) = existing
            else:
                created_at = now
                timezone_value = _DEFAULT_TIMEZONE
                risk_profile = _DEFAULT_RISK_PROFILE
                confirmed_free_transfers = None
                confirmed_free_transfers_event = None
                email = None
            connection.execute(
                """
                INSERT INTO profiles
                    (team_id, timezone, risk_profile, confirmed_free_transfers,
                     confirmed_free_transfers_event, email, reminder_status, reminder_lead_hours,
                     reminder_pending_email, reminder_confirmation_token_hash,
                     reminder_confirmation_expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id) DO UPDATE SET
                    reminder_status = 'pending',
                    reminder_lead_hours = excluded.reminder_lead_hours,
                    reminder_pending_email = excluded.reminder_pending_email,
                    reminder_confirmation_token_hash = excluded.reminder_confirmation_token_hash,
                    reminder_confirmation_expires_at = excluded.reminder_confirmation_expires_at,
                    updated_at = excluded.updated_at
                """,
                (
                    team_id, timezone_value, risk_profile, confirmed_free_transfers,
                    confirmed_free_transfers_event, email, lead_hours, pending_email,
                    token_hash, expires_at, created_at, now,
                ),
            )
    return load_profile(db_path, team_id)


def confirm_reminder(db_path, team_id, now):
    """Promote a pending reminder confirmation to enabled (issue #79).

    Copies `reminder_pending_email` into `email`, sets `reminder_status='enabled'`, and clears
    the pending/token/expiry columns. Called only after `server.py` has already validated the
    raw token from the confirmation link against `reminder_confirmation_token_hash` with
    `secrets.compare_digest` and checked `reminder_confirmation_expires_at`. Returns None (and
    writes nothing) if `team_id` has no row, or has no pending confirmation to promote -- a
    stale or already-used link is inert, not an error state this function needs to distinguish
    further; the caller decides what to tell the visitor.
    """
    with closing(_connect(db_path)) as connection:
        with connection:
            existing = connection.execute(
                "SELECT reminder_pending_email FROM profiles WHERE team_id = ?", (team_id,)
            ).fetchone()
            if existing is None or existing[0] is None:
                return None
            connection.execute(
                """
                UPDATE profiles SET
                    email = reminder_pending_email,
                    reminder_status = 'enabled',
                    reminder_pending_email = NULL,
                    reminder_confirmation_token_hash = NULL,
                    reminder_confirmation_expires_at = NULL,
                    updated_at = ?
                WHERE team_id = ?
                """,
                (now, team_id),
            )
    return load_profile(db_path, team_id)


def set_reminder_decision(db_path, team_id, status, now, clear_email=False):
    """Create or update team_id's `reminder_status` to a terminal decision (issue #79).

    Used for both an explicit "no thanks" (`status='declined'`, never having been enabled) and
    a "disable" of a previously enabled reminder -- the same underlying write, distinguished
    only by `clear_email`: disabling additionally clears the confirmed `email`, so a later
    re-enable always re-proves ownership of whatever address is submitted next rather than
    silently inheriting trust from the old one, matching the plan's stated design. Always
    clears any in-flight pending-confirmation fields regardless of `clear_email` -- a
    decline/disable supersedes any outstanding confirmation link. `reminder_lead_hours` is
    deliberately left untouched either way (not included in the SET clause below), so a
    remembered lead-time choice survives to prefill a future re-enable. Mirrors
    `set_lookup_opt_out`'s create-or-preserve pattern.
    """
    with closing(_connect(db_path)) as connection:
        with connection:
            existing = connection.execute(
                "SELECT created_at, timezone, risk_profile, confirmed_free_transfers, "
                "confirmed_free_transfers_event, email FROM profiles WHERE team_id = ?",
                (team_id,),
            ).fetchone()
            if existing:
                (
                    created_at, timezone_value, risk_profile, confirmed_free_transfers,
                    confirmed_free_transfers_event, email,
                ) = existing
            else:
                created_at = now
                timezone_value = _DEFAULT_TIMEZONE
                risk_profile = _DEFAULT_RISK_PROFILE
                confirmed_free_transfers = None
                confirmed_free_transfers_event = None
                email = None
            if clear_email:
                email = None
            connection.execute(
                """
                INSERT INTO profiles
                    (team_id, timezone, risk_profile, confirmed_free_transfers,
                     confirmed_free_transfers_event, email, reminder_status,
                     reminder_pending_email, reminder_confirmation_token_hash,
                     reminder_confirmation_expires_at, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, ?, ?)
                ON CONFLICT(team_id) DO UPDATE SET
                    email = excluded.email,
                    reminder_status = excluded.reminder_status,
                    reminder_pending_email = NULL,
                    reminder_confirmation_token_hash = NULL,
                    reminder_confirmation_expires_at = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    team_id, timezone_value, risk_profile, confirmed_free_transfers,
                    confirmed_free_transfers_event, email, status, created_at, now,
                ),
            )
    return load_profile(db_path, team_id)
