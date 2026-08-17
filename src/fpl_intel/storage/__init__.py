"""Persistence: everything that reads or writes a file/database under the Railway persistent
volume (or a local `data/`/`config/` checkout) directly, outside the shared refresh's own
generation artifacts.

`profiles.py` (SQLite-backed per-team profiles, issue #45), `release_notes.py` (the "What's New"
tab's storage/validation, issue #143), `release_notes_subscribers.py` (its email-subscription
storage, double opt-in).
"""
