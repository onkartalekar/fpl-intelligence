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
import sqlite3


_SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    team_id INTEGER PRIMARY KEY,
    timezone TEXT NOT NULL,
    risk_profile TEXT NOT NULL,
    confirmed_free_transfers INTEGER,
    confirmed_free_transfers_event INTEGER,
    email TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_COLUMNS = (
    "team_id, timezone, risk_profile, confirmed_free_transfers, "
    "confirmed_free_transfers_event, email, created_at, updated_at"
)


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
        "created_at": row[6],
        "updated_at": row[7],
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
):
    """Create or update the saved profile for team_id. Returns the resulting row.

    `email` is never written here -- it stays whatever it already was (None for a
    brand-new row), populated only by #55's explicit reminder opt-in, never implied
    by saving other preferences.
    """
    with closing(_connect(db_path)) as connection:
        with connection:
            existing = connection.execute(
                "SELECT created_at, email FROM profiles WHERE team_id = ?", (team_id,)
            ).fetchone()
            created_at = existing[0] if existing else now
            email = existing[1] if existing else None
            connection.execute(
                """
                INSERT INTO profiles
                    (team_id, timezone, risk_profile, confirmed_free_transfers,
                     confirmed_free_transfers_event, email, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(team_id) DO UPDATE SET
                    timezone = excluded.timezone,
                    risk_profile = excluded.risk_profile,
                    confirmed_free_transfers = excluded.confirmed_free_transfers,
                    confirmed_free_transfers_event = excluded.confirmed_free_transfers_event,
                    updated_at = excluded.updated_at
                """,
                (
                    team_id, timezone, risk_profile, confirmed_free_transfers,
                    confirmed_free_transfers_event, email, created_at, now,
                ),
            )
    return load_profile(db_path, team_id)
