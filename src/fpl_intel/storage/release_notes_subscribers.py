"""SQLite-backed storage for the "What's New" tab's email subscription (issue #143), double
opt-in -- mirrors `profiles.py`'s schema-migration pattern (its own `_migrate_schema` docstring
explains the "OperationalError: no such column" incident that pattern exists to prevent), but a
separate database file: subscribers are keyed by email, not FPL team ID, and are otherwise
unrelated data -- there is no reason to couple them to `profiles.db`'s schema/migrations.

Double opt-in, matching `reminder_confirmation.py`'s existing confirm-by-link pattern (issue #79)
rather than trusting a submitted address outright: a bare single-opt-in text field would let
anyone enter a stranger's email and have this app send them mail with no verification, exactly
the abuse vector double opt-in exists to close everywhere else in this codebase. Nothing is
persisted as `confirmed` until the confirmation link is actually clicked.

Two distinct tokens, deliberately different storage:
- `confirm_token`: one-time, needed only once (embedded in the confirmation email, then compared
  against at click time) -- only its hash is ever stored, same as
  `reminder_confirmation_token_hash` in `profiles.py`.
- `unsubscribe_token`: reused in the footer of every future release-notes email sent to this
  address, so it must be retrievable in plaintext later, not just verifiable once -- stored as
  plaintext. Low blast radius if it leaked: its only capability is unsubscribing this one already-
  non-secret email address, nothing else.
"""

from contextlib import closing
from pathlib import Path
import sqlite3


_COLUMN_DEFS = [
    ("email", "TEXT PRIMARY KEY"),
    ("status", "TEXT NOT NULL"),  # 'pending' | 'confirmed'
    ("confirm_token_hash", "TEXT"),
    ("confirm_expires_at", "TEXT"),
    ("unsubscribe_token", "TEXT"),
    ("created_at", "TEXT NOT NULL"),
    ("updated_at", "TEXT NOT NULL"),
]

_SCHEMA = "CREATE TABLE IF NOT EXISTS release_notes_subscribers (\n" + ",\n".join(
    f"    {name} {declaration}" for name, declaration in _COLUMN_DEFS
) + "\n)"

_COLUMNS = ", ".join(name for name, _declaration in _COLUMN_DEFS)


def _migrate_schema(connection):
    existing_columns = {row[1] for row in connection.execute("PRAGMA table_info(release_notes_subscribers)")}
    for name, declaration in _COLUMN_DEFS:
        if name in existing_columns:
            continue
        connection.execute(f"ALTER TABLE release_notes_subscribers ADD COLUMN {name} {declaration}")


def _connect(db_path):
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(db_path, timeout=10)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(_SCHEMA)
    _migrate_schema(connection)
    return connection


def _row_to_dict(row):
    if row is None:
        return None
    return dict(zip((name for name, _declaration in _COLUMN_DEFS), row))


def load(db_path, email):
    """Return the stored row for `email` (already lowercased/stripped by the caller), or `None`
    if it's never been submitted."""
    with closing(_connect(db_path)) as connection:
        row = connection.execute(
            f"SELECT {_COLUMNS} FROM release_notes_subscribers WHERE email = ?", (email,),
        ).fetchone()
    return _row_to_dict(row)


def set_pending(db_path, email, confirm_token_hash, confirm_expires_at, now):
    """Create or refresh a pending subscription -- safe to call repeatedly for the same email
    (e.g. a visitor resubmitting the form): always issues a fresh token/expiry, overwriting any
    still-pending one from an earlier attempt. Never touches an already-`confirmed` row's status
    -- resubmitting the subscribe form for an address that's already confirmed is a harmless no-op
    on the confirmation-token fields, not a downgrade back to pending.
    """
    with closing(_connect(db_path)) as connection:
        with connection:
            existing = connection.execute(
                "SELECT status FROM release_notes_subscribers WHERE email = ?", (email,),
            ).fetchone()
            if existing and existing[0] == "confirmed":
                return
            connection.execute(
                f"""
                INSERT INTO release_notes_subscribers
                    (email, status, confirm_token_hash, confirm_expires_at, created_at, updated_at)
                VALUES (?, 'pending', ?, ?, ?, ?)
                ON CONFLICT(email) DO UPDATE SET
                    status = 'pending',
                    confirm_token_hash = excluded.confirm_token_hash,
                    confirm_expires_at = excluded.confirm_expires_at,
                    updated_at = excluded.updated_at
                """,
                (email, confirm_token_hash, confirm_expires_at, now, now),
            )


def confirm(db_path, email, unsubscribe_token, now):
    """Mark `email` confirmed and store its (plaintext, reused) unsubscribe token. Clears the
    now-spent confirm token/expiry -- the confirmation link is single-use, same posture as
    `profiles.confirm_reminder`."""
    with closing(_connect(db_path)) as connection:
        with connection:
            connection.execute(
                """
                UPDATE release_notes_subscribers
                SET status = 'confirmed', confirm_token_hash = NULL, confirm_expires_at = NULL,
                    unsubscribe_token = ?, updated_at = ?
                WHERE email = ?
                """,
                (unsubscribe_token, now, email),
            )


def unsubscribe(db_path, email):
    """Remove `email` entirely -- no lingering 'unsubscribed' row, matching how declining a
    reminder opt-in clears the confirmed email (`profiles.set_reminder_decision`'s
    `clear_email=True` path) rather than keeping a tombstone."""
    with closing(_connect(db_path)) as connection:
        with connection:
            connection.execute("DELETE FROM release_notes_subscribers WHERE email = ?", (email,))


def list_confirmed(db_path):
    """Every confirmed subscriber, for the send-on-publish step in `server.py`'s
    `_handle_release_notes`. Returns `[{"email", "unsubscribe_token"}, ...]`."""
    with closing(_connect(db_path)) as connection:
        rows = connection.execute(
            "SELECT email, unsubscribe_token FROM release_notes_subscribers WHERE status = 'confirmed'",
        ).fetchall()
    return [{"email": email, "unsubscribe_token": token} for email, token in rows]
