"""Storage and validation for the "What's New" dashboard tab (issue #143).

Entries are dated, one per day something merged to `main`, generated daily by
`scripts/publish_release_notes.py` and pushed here via `POST /api/release-notes`
(`server.py`). Deliberately **not** part of the refresh pipeline's transactional
generation system (`generation.py`'s `resolve_artifact`/`publish_generation`) --
this data isn't produced by `refresh_dashboard.py`, so a direct path under
`data/` (matching `_profiles_db_path`'s own pattern in `server.py`) is simpler
and correct: there is no generation-pointer indirection to keep in sync with.

Storage is a single JSON file, `data/release-notes.json`, holding a list of
entries newest-first. Small, personal-alpha-scale data (a handful of entries a
month) -- no database needed, matching every other JSON snapshot this app
already writes (`dashboard-state.json`, `model-performance.json`).

Per issue #143's plan doc (`plans/issue-143-whats-new-tab.md`), this pass ships
the read-only tab only -- category taxonomy, search/filter, and the daily
generation job. Email subscription is deliberately out of scope here, tracked
as a separate follow-up issue.
"""

from datetime import date as _date
import json
from pathlib import Path


class ReleaseNotesValidationError(Exception):
    """Raised when a submitted release-notes entry payload fails validation."""


# The five buckets decided in the plan doc's UX-design pass (2026-08-11) -- every change in
# every entry must carry exactly one of these, including entries built by the daily job's
# template fallback (no LLM available), which needs its own deterministic assignment rule
# (see `scripts/publish_release_notes.py`'s `categorize_pr`) rather than leaving one blank.
CATEGORIES = ("Feature", "Fix", "Data", "Docs", "Chore")

_MAX_CHANGES_PER_ENTRY = 20
_MAX_HEADLINE_LENGTH = 200
_MAX_SUMMARY_LENGTH = 2000
_MAX_TITLE_LENGTH = 200
_MAX_DESCRIPTION_LENGTH = 500
_MAX_ENTRIES_KEPT = 366  # a little over a year of daily entries -- generous, not unbounded


def release_notes_path(root):
    return Path(root) / "data" / "release-notes.json"


def load_entries(root):
    """Return every stored entry, newest-first. `[]` if nothing has ever been published --
    the tab's own empty state handles that, not an error here (mirrors every other JSON
    snapshot reader in this codebase's "missing file means nothing generated yet" posture).
    """
    path = release_notes_path(root)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entries = payload.get("entries") if isinstance(payload, dict) else None
    return entries if isinstance(entries, list) else []


def _validate_change(raw, index):
    if not isinstance(raw, dict):
        raise ReleaseNotesValidationError(f"changes[{index}] must be an object")
    category = raw.get("category")
    if category not in CATEGORIES:
        raise ReleaseNotesValidationError(
            f"changes[{index}].category must be one of {', '.join(CATEGORIES)}"
        )
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > _MAX_TITLE_LENGTH:
        raise ReleaseNotesValidationError(f"changes[{index}].title is required")
    description = raw.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > _MAX_DESCRIPTION_LENGTH:
        raise ReleaseNotesValidationError(f"changes[{index}].description is required")
    return {"category": category, "title": title.strip(), "description": description.strip()}


def validate_entry_payload(payload):
    """Validate and normalize a `POST /api/release-notes` request body.

    Shape: `{"date": "YYYY-MM-DD", "headline": "...", "summary": "...", "changes": [...]}`.
    Raises `ReleaseNotesValidationError` with a specific, safe-to-log message on any problem --
    this endpoint is operator-only (gated by `X-Refresh-Token`, same as `/api/refresh`), not a
    public input surface, so unlike the visitor-facing validators elsewhere in this codebase
    (`_validate_profile_payload` etc.), a precise error message here is fine: only the daily job
    and the operator ever see it, never an anonymous caller probing for information.
    """
    if not isinstance(payload, dict):
        raise ReleaseNotesValidationError("payload must be an object")

    raw_date = payload.get("date")
    if not isinstance(raw_date, str):
        raise ReleaseNotesValidationError("date is required")
    try:
        _date.fromisoformat(raw_date)
    except ValueError as error:
        raise ReleaseNotesValidationError("date must be YYYY-MM-DD") from error

    headline = payload.get("headline")
    if not isinstance(headline, str) or not headline.strip() or len(headline) > _MAX_HEADLINE_LENGTH:
        raise ReleaseNotesValidationError("headline is required")

    summary = payload.get("summary")
    if not isinstance(summary, str) or not summary.strip() or len(summary) > _MAX_SUMMARY_LENGTH:
        raise ReleaseNotesValidationError("summary is required")

    changes = payload.get("changes")
    if not isinstance(changes, list) or not changes or len(changes) > _MAX_CHANGES_PER_ENTRY:
        raise ReleaseNotesValidationError(f"changes must be a non-empty list of at most {_MAX_CHANGES_PER_ENTRY}")

    cleaned_changes = [_validate_change(raw, index) for index, raw in enumerate(changes)]

    return {
        "date": raw_date,
        "headline": headline.strip(),
        "summary": summary.strip(),
        "changes": cleaned_changes,
    }


def upsert_entry(root, payload):
    """Validate `payload` and write it into `data/release-notes.json`, keyed by date.

    Idempotent by design: re-publishing the same date (e.g. the daily job retrying after a
    transient failure) overwrites that date's entry rather than duplicating it -- there is
    exactly one entry per date, ever. Returns the cleaned, stored entry.
    """
    cleaned = validate_entry_payload(payload)
    path = release_notes_path(root)
    entries = load_entries(root)
    entries = [entry for entry in entries if entry.get("date") != cleaned["date"]]
    entries.append(cleaned)
    entries.sort(key=lambda entry: entry.get("date", ""), reverse=True)
    entries = entries[:_MAX_ENTRIES_KEPT]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"entries": entries}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return cleaned


def render_entry_markdown(entry):
    """Render one entry as the Markdown archived to the git-tracked `release-notes/` folder
    (issue #143's plan doc, Candidate C3 -- "important piece of documentation" alongside the
    live tab, not a substitute for it). Deterministic, no I/O -- shared by the daily job and
    the bootstrap migration so both produce byte-identical formatting.
    """
    lines = [f"# {entry['date']} -- {entry['headline']}", "", entry["summary"], ""]
    for change in entry["changes"]:
        lines.append(f"- **[{change['category']}]** {change['title']} -- {change['description']}")
    lines.append("")
    return "\n".join(lines)
