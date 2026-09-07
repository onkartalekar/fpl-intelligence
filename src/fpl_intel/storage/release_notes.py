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

# Issue #196: a second, independent per-change signal alongside `category` -- who the change is
# actually for, not what kind of change it is. `category` alone couldn't answer this ("Fix" spans
# both a UI change a manager would notice and an internal-only logging tweak they never would),
# confirmed live on the 2026-08-15 entry. The LLM path (`build_llm_entry`) judges this per change
# as part of its generated JSON; the template fallback (`categorize_pr`) can't judge it
# dynamically and derives it deterministically from `category` instead -- see
# `scripts/publish_release_notes.py`'s `_categorize_audience`.
AUDIENCES = ("user", "developer")

# 20 looked generous on paper but wasn't: this repo's own real PR history includes a single day
# (2026-08-08) with 26 merged PRs, confirmed live when the initial historical backfill (issue
# #143) 400'd trying to publish it. 50 gives real headroom above the busiest day seen so far.
_MAX_CHANGES_PER_ENTRY = 50
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
    audience = raw.get("audience")
    if audience not in AUDIENCES:
        raise ReleaseNotesValidationError(
            f"changes[{index}].audience must be one of {', '.join(AUDIENCES)}"
        )
    title = raw.get("title")
    if not isinstance(title, str) or not title.strip() or len(title) > _MAX_TITLE_LENGTH:
        raise ReleaseNotesValidationError(f"changes[{index}].title is required")
    description = raw.get("description")
    if not isinstance(description, str) or not description.strip() or len(description) > _MAX_DESCRIPTION_LENGTH:
        raise ReleaseNotesValidationError(f"changes[{index}].description is required")
    return {
        "category": category, "audience": audience,
        "title": title.strip(), "description": description.strip(),
    }


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


_MAX_DELETE_DATES = _MAX_ENTRIES_KEPT  # a purge can't name more dates than can ever be stored


def validate_delete_payload(payload):
    """Validate a `POST /api/release-notes` *deletion* request body: `{"delete": ["YYYY-MM-DD",
    ...]}`. Returns the de-duplicated, sorted list of dates. Raises `ReleaseNotesValidationError`
    on any problem -- operator-only endpoint, same as `validate_entry_payload`, so precise
    messages are fine.

    Deletion shares the `/api/release-notes` path rather than getting its own -- the same way
    `/api/reminder-opt-in` carries both its "enable" and "disable" actions on one path. Its
    reason to exist is issue #300's one-time cleanup of the self-archival junk entries that same
    issue stops at the source (`publish_release_notes._is_own_archival_pr`).
    """
    if not isinstance(payload, dict):
        raise ReleaseNotesValidationError("payload must be an object")
    raw_dates = payload.get("delete")
    if not isinstance(raw_dates, list) or not raw_dates or len(raw_dates) > _MAX_DELETE_DATES:
        raise ReleaseNotesValidationError(
            f"delete must be a non-empty list of at most {_MAX_DELETE_DATES} dates"
        )
    for raw_date in raw_dates:
        if not isinstance(raw_date, str):
            raise ReleaseNotesValidationError("every delete entry must be a YYYY-MM-DD string")
        try:
            _date.fromisoformat(raw_date)
        except ValueError as error:
            raise ReleaseNotesValidationError(f"'{raw_date}' is not a valid YYYY-MM-DD date") from error
    return sorted(set(raw_dates))


def delete_entries(root, dates):
    """Remove the entries for `dates` from `data/release-notes.json`, in place. Returns
    `{"deleted": [...], "not_found": [...]}` -- which requested dates had a stored entry and
    which didn't, both sorted. A no-op (file missing, or none of `dates` present) is success,
    not an error: this is operator cleanup (issue #300), safe to re-run.
    """
    wanted = sorted(set(dates))
    entries = load_entries(root)
    present = {entry.get("date") for entry in entries}
    deleted = [wanted_date for wanted_date in wanted if wanted_date in present]
    not_found = [wanted_date for wanted_date in wanted if wanted_date not in present]
    if deleted:
        kept = [entry for entry in entries if entry.get("date") not in set(deleted)]
        kept.sort(key=lambda entry: entry.get("date", ""), reverse=True)
        path = release_notes_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"entries": kept}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return {"deleted": deleted, "not_found": not_found}


def is_archival_bookkeeping_change(change):
    """True for a `changes[]` item that is the daily job's own "we archived yesterday's entry"
    bookkeeping rather than a real shipped change -- issue #303.

    These accumulated before issue #300 filtered `release-notes.yml`'s own archival PR out of
    `fetch_merged_prs`: handed only "Archive <date> release notes" as a merged PR, the generator
    dutifully wrote a `Chore` change about archiving release notes. The exact wording varies
    ("Archive previous / old / past / outdated release notes", "Archive 2026-08-16 release
    notes", "Archive the latest release notes"), so this can't reuse
    `publish_release_notes._OWN_ARCHIVAL_PR_TITLE_RE` -- that matches the *PR* title's one fixed
    shape; this matches the looser space of change titles the generator actually produced.

    Deliberately tight, to never strip a real change that merely mentions archiving or release
    notes. Verified 2026-09-07 against the full live dataset: "Enhance team forecast archiving
    mechanism" (#286, a Fix), "Add fine-grained PAT for archival PR" (a Fix), "Fix release-notes
    workflow", "Launch the What's New tab for release notes" all survive. Requires all three:
    `Chore` category, title starts with "archive", title mentions a release note.
    """
    if not isinstance(change, dict) or change.get("category") != "Chore":
        return False
    title = (change.get("title") or "").strip().lower()
    return title.startswith("archive") and "release note" in title


def prune_archival_changes(root):
    """Strip every `is_archival_bookkeeping_change` from every stored entry in
    `data/release-notes.json`, in place -- issue #303's one-time cleanup of the junk changes
    left inside *mixed* days (real changelog content plus a stray archival bullet) after issue
    #300's whole-entry purge (`delete_entries`). An entry left with zero `changes[]` is dropped
    entirely, exactly as if it had been deleted outright.

    Returns `{"removed": [{"date", "title"}, ...], "modified": [...dates], "emptied": [...dates]}`
    -- `removed` names every change stripped (title included) so a one-shot operator run against
    production is auditable after the fact, since there is no GET endpoint to diff against and
    the match is a heuristic. All three lists are sorted by date. A no-op (file missing, nothing
    matches) is success -- operator cleanup, safe to re-run.
    """
    entries = load_entries(root)
    removed = []
    modified = []
    emptied = []
    kept = []
    for entry in entries:
        changes = entry.get("changes") or []
        surviving = [change for change in changes if not is_archival_bookkeeping_change(change)]
        if len(surviving) == len(changes):
            kept.append(entry)
            continue
        date = entry.get("date", "")
        removed.extend(
            {"date": date, "title": change.get("title", "")}
            for change in changes
            if is_archival_bookkeeping_change(change)
        )
        if surviving:
            modified.append(date)
            kept.append({**entry, "changes": surviving})
        else:
            emptied.append(date)
    if removed:
        kept.sort(key=lambda entry: entry.get("date", ""), reverse=True)
        path = release_notes_path(root)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"entries": kept}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    removed.sort(key=lambda item: item["date"])
    return {"removed": removed, "modified": sorted(modified), "emptied": sorted(emptied)}


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
