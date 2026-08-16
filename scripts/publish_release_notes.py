#!/usr/bin/env python3
"""Generate and publish one day's "What's New" entry (issue #143).

See plans/issue-143-whats-new-tab.md for the full design. Trigger-agnostic like
`send_deadline_reminder.py`/`live_regression_check.py`: this script takes no opinion on what
invokes it. Today it is invoked twice daily by `.github/workflows/release-notes.yml` (12:00 and
13:00 UTC, covering both EDT/EST 8 AM ET -- see `is_correct_scheduled_trigger` below for why two
triggers exist and how the "wrong" one for the current DST regime is expected to no-op, not fail).

What it does, each run:

1. Determines "yesterday" in `America/New_York` (`target_date`) -- the previous calendar day,
   ET, regardless of which of the two UTC cron triggers actually fired this run.
2. Lists every PR merged to `main` on that ET calendar day (`fetch_merged_prs`, via the GitHub
   REST search API). **Nothing merged -> exits quietly, no entry published** -- this is the
   expected, self-resolving no-op case the issue's own point 1 asks for, not a failure.
3. Generates the day's headline/summary/per-change copy, categorizing each change into one of
   `release_notes.CATEGORIES` (`Feature`/`Fix`/`Data`/`Docs`/`Chore`). Tries an LLM first
   (`FPL_INTEL_RELEASE_NOTES_LLM_*` env vars, provider-agnostic like `news_signals.py`'s existing
   pattern -- a **separate** credential from that module's `FPL_INTEL_LLM_*`, confirmed with the
   user 2026-08-11: Phase 5 has never been activated, so there's nothing to share). If the LLM is
   unconfigured or the call fails, falls back to a plain deterministic template built directly
   from each PR's title/body (`build_template_entry`) -- a real shipped day never silently gets
   zero entry just because generation failed (issue #143 plan doc, Candidate B2a).
4. `POST`s the entry to the live dashboard (`/api/release-notes`, gated by the same
   `FPL_INTEL_REFRESH_TOKEN` `/api/refresh` already requires) -- this is what the live "What's
   New" tab actually renders from. Required; a failure here fails the whole run.
5. Writes the same entry as Markdown to `release-notes/<date>.md`, in the working directory --
   the git-tracked documentation-history half of the plan's dual-write design (Candidate C3).
   Committing and pushing that file is this workflow's own job (`.github/workflows/
   release-notes.yml`), not this script's -- keeps this script pure-Python/stdlib and testable
   without shelling out to `git`.

Configuration, entirely environment-variable driven:

- `FPL_INTEL_DASHBOARD_BASE_URL` / `FPL_INTEL_REFRESH_TOKEN`: same meaning and same values as
  `send_deadline_reminder.py`'s -- the live dashboard's public origin, and the operator secret
  gating `/api/refresh`-class endpoints (reused here, not a new secret).
- `GITHUB_TOKEN` (optional but recommended -- GitHub Actions provides this automatically as
  `secrets.GITHUB_TOKEN`): raises the GitHub REST API's unauthenticated rate limit; the search
  call still works without it, just far more rate-limited.
- `GITHUB_REPOSITORY` (auto-provided by GitHub Actions, e.g. `"owner/repo"`): which repo to list
  merged PRs from. Required.
- `FPL_INTEL_RELEASE_NOTES_LLM_PROVIDER` (`"claude"` or `"openai_compatible"`) /
  `FPL_INTEL_RELEASE_NOTES_LLM_MODEL` / `FPL_INTEL_RELEASE_NOTES_LLM_API_KEY` /
  `FPL_INTEL_RELEASE_NOTES_LLM_API_BASE`: optional. Unset (or the call fails) -> the template
  fallback runs instead; never a hard failure.

`--dry-run` prints the generated entry (and, if applicable, why nothing was generated) instead of
publishing or writing anything.
"""

import argparse
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen
import zoneinfo

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from fpl_intel.release_notes import AUDIENCES, CATEGORIES, render_entry_markdown, validate_entry_payload  # noqa: E402


class ConfigError(RuntimeError):
    """Malformed or missing configuration. Messages never include credential values."""


DASHBOARD_BASE_URL_ENV_VAR = "FPL_INTEL_DASHBOARD_BASE_URL"
REFRESH_TOKEN_ENV_VAR = "FPL_INTEL_REFRESH_TOKEN"
GITHUB_TOKEN_ENV_VAR = "GITHUB_TOKEN"
GITHUB_REPOSITORY_ENV_VAR = "GITHUB_REPOSITORY"

LLM_PROVIDER_ENV_VAR = "FPL_INTEL_RELEASE_NOTES_LLM_PROVIDER"
LLM_MODEL_ENV_VAR = "FPL_INTEL_RELEASE_NOTES_LLM_MODEL"
LLM_API_KEY_ENV_VAR = "FPL_INTEL_RELEASE_NOTES_LLM_API_KEY"
LLM_API_BASE_ENV_VAR = "FPL_INTEL_RELEASE_NOTES_LLM_API_BASE"
_CLAUDE_API_URL = "https://api.anthropic.com/v1/messages"
_CLAUDE_DEFAULT_MODEL = "claude-haiku-4-5-20251001"

_TIMEZONE = "America/New_York"
# Bug fix: the two cron strings this workflow actually schedules (release-notes.yml) -- compared
# against `github.event.schedule` (the exact cron GitHub Actions says fired this run), not against
# wall-clock time. See is_correct_scheduled_trigger's docstring for why.
_SCHEDULE_EDT_CRON = "0 12 * * *"  # nominally 8 AM EDT
_SCHEDULE_EST_CRON = "0 13 * * *"  # nominally 8 AM EST
_REQUEST_TIMEOUT_SECONDS = 30
_LLM_TIMEOUT_SECONDS = 60

_SYSTEM_PROMPT = (
    "You write short, upbeat, precise release notes for a fantasy football decision-support "
    "dashboard, from a list of merged pull requests. Output ONLY a single JSON object, no "
    "markdown fences, matching exactly this shape: "
    '{"headline": "one sentence synthesizing the day\'s theme across every change", '
    '"summary": "2-3 sentences, one coherent narrative, not a list", '
    '"changes": [{"category": "Feature|Fix|Data|Docs|Chore", "audience": "user|developer", '
    '"title": "one line, imperative or noun phrase", "description": "one line, plain language, '
    'no jargon"}]}. '
    "One changes[] entry per pull request given. category must be exactly one of the five listed "
    "-- never invent a new one. audience is a second, independent judgment for each change: "
    '"user" if a fantasy football manager using the dashboard would actually notice or care '
    "about this (a UI change, a new capability, a fix to something they'd have seen break, a "
    'change to the recommendations/data they see); "developer" if it is purely internal -- '
    "tests, CI, logging, refactors, internal tooling, documentation, or anything else a "
    "player-facing reader would never encounter. Judge audience per change, not by category -- "
    "a Fix or a Chore can be either. Never mention pull request numbers, internal file paths, or "
    "implementation detail a player-facing reader wouldn't care about."
)


def _require_dashboard_base_url():
    raw = os.environ.get(DASHBOARD_BASE_URL_ENV_VAR)
    if not raw or not raw.strip():
        raise ConfigError(f"{DASHBOARD_BASE_URL_ENV_VAR} is required (the live dashboard's public origin).")
    return raw.strip().rstrip("/")


def _require_refresh_token():
    raw = os.environ.get(REFRESH_TOKEN_ENV_VAR)
    if not raw:
        raise ConfigError(f"{REFRESH_TOKEN_ENV_VAR} is required.")
    return raw


def _require_repository():
    raw = os.environ.get(GITHUB_REPOSITORY_ENV_VAR)
    if not raw or "/" not in raw:
        raise ConfigError(f"{GITHUB_REPOSITORY_ENV_VAR} is required, e.g. 'owner/repo'.")
    return raw


def target_date(now=None):
    """The ET calendar date this run should publish for: yesterday, in `America/New_York` --
    matching the issue's own framing ("the previous day's merged changes"). Computed in ET, not
    UTC, so a run close to midnight ET still targets the correct day regardless of which of the
    two UTC cron triggers fired it.
    """
    now = now or datetime.now(zoneinfo.ZoneInfo(_TIMEZONE))
    return (now.date() - timedelta(days=1))


def is_correct_scheduled_trigger(schedule, now=None):
    """True only when `schedule` -- the exact cron string GitHub Actions fired this run from
    (`github.event.schedule`) -- is the one that's actually correct for today's DST regime.

    This script runs from two fixed-UTC cron triggers (12:00 and 13:00 UTC -- see the workflow
    file), because a single hardcoded UTC time can't stay correct for "8 AM ET" across DST
    transitions. The original version of this check compared the *actual* wall-clock hour at
    execution time against a fixed target hour (`now.hour == 8`) -- which broke silently:
    GitHub Actions `schedule` triggers are commonly delayed under platform load, sometimes by an
    hour or more, so a run that starts even a few minutes into the "9 AM" wall-clock hour failed
    that check on *both* daily triggers, not just the intentionally-wrong one. Confirmed live:
    every run reported success from 2026-08-12 onward, yet nothing was actually published --
    every single run had drifted past its target hour and silently no-op'd.

    Checking which cron the platform says fired this run against which cron is correct for
    today's real DST regime is delay-proof by construction: it doesn't matter whether the run
    started at 8:00 or 10:30, only whether `America/New_York` is presently observing daylight
    time. An unrecognized or empty `schedule` (a non-`schedule` trigger, e.g. `workflow_dispatch`
    without `--skip-hour-check`) is never treated as correct -- there's nothing to match it to.
    """
    now = now or datetime.now(zoneinfo.ZoneInfo(_TIMEZONE))
    daylight_saving = bool(now.dst())
    if schedule == _SCHEDULE_EDT_CRON:
        return daylight_saving
    if schedule == _SCHEDULE_EST_CRON:
        return not daylight_saving
    return False


def _et_day_bounds_in_utc(date):
    """Return (start, end) UTC datetimes spanning one full `America/New_York` calendar day --
    needed because GitHub's search API's `merged:` qualifier is UTC-based, and an ET day does not
    align with a UTC day (ET is 4-5 hours behind UTC), so a bare `merged:YYYY-MM-DD` query would
    miss or double-count PRs merged near ET midnight.
    """
    tz = zoneinfo.ZoneInfo(_TIMEZONE)
    start_local = datetime(date.year, date.month, date.day, 0, 0, 0, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return start_local.astimezone(zoneinfo.ZoneInfo("UTC")), end_local.astimezone(zoneinfo.ZoneInfo("UTC"))


def fetch_merged_prs(repository, date, github_token=None, timeout=_REQUEST_TIMEOUT_SECONDS):
    """List every PR merged to `repository` on `date` (an ET calendar day). Returns `[]` if none
    merged -- the expected, self-resolving no-op case, not an error.
    """
    start_utc, end_utc = _et_day_bounds_in_utc(date)
    query = (
        f"repo:{repository} is:pr is:merged "
        f"merged:{start_utc.strftime('%Y-%m-%dT%H:%M:%S')}..{end_utc.strftime('%Y-%m-%dT%H:%M:%S')}"
    )
    url = f"https://api.github.com/search/issues?q={quote(query)}&per_page=50&sort=created&order=asc"
    headers = {"Accept": "application/vnd.github+json"}
    if github_token:
        headers["Authorization"] = f"Bearer {github_token}"
    request = Request(url, headers=headers)
    with urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read())
    return payload.get("items", [])


# Deterministic categorization for the template fallback (no LLM available) -- title-keyword
# matching only, no extra per-PR API call for changed files. Order matters: first match wins.
_CATEGORY_KEYWORDS = (
    ("Fix", re.compile(r"\b(fix|bug|regression|broken)\b", re.IGNORECASE)),
    ("Docs", re.compile(r"\b(readme|docs?|plan doc|documentation)\b", re.IGNORECASE)),
    ("Chore", re.compile(r"\b(chore|test|tests|ci|cleanup|refactor|housekeeping|lint)\b", re.IGNORECASE)),
    ("Data", re.compile(r"\b(data|feed|reconcil|ingest|source)\b", re.IGNORECASE)),
)


def categorize_pr(pr):
    """Deterministic category assignment for one PR, used only by the template fallback (an LLM,
    when available, assigns categories itself as part of its generated output). `Feature` is the
    default when nothing more specific matches -- see `_CATEGORY_KEYWORDS` for the specific rules
    checked first, in order.
    """
    title = pr.get("title") or ""
    for category, pattern in _CATEGORY_KEYWORDS:
        if pattern.search(title):
            return category
    return "Feature"


# Issue #196: the template fallback has no LLM to judge audience per change, so unlike
# `build_llm_entry` it derives audience deterministically from `category` -- the same mapping
# the release-notes email used exclusively before this issue. Not a fully-informed per-change
# judgment (this is exactly the limitation issue #196 exists to fix on the LLM path), but a
# reasonable default when no LLM is configured at all.
_DEVELOPER_CATEGORIES = ("Docs", "Chore")


def categorize_audience(category):
    return "developer" if category in _DEVELOPER_CATEGORIES else "user"


_MARKDOWN_BULLET_RE = re.compile(r"^[-*]\s+")
_MARKDOWN_CHECKBOX_RE = re.compile(r"^\[[ xX]\]\s*")

# Just under release_notes.py's own server-side validation ceilings (200/500 chars,
# `_MAX_TITLE_LENGTH`/`_MAX_DESCRIPTION_LENGTH`) -- real PR titles/bodies in this repo regularly
# run longer than either limit (a PR's first substantive bullet alone was 537 chars, confirmed
# live), so the template fallback must truncate before POSTing, not just trust real-world PR
# copy to already fit. Deliberately set close to the server's real ceiling, not an arbitrary
# tighter number: the goal is "never get rejected," not "keep descriptions short" -- an earlier
# version of this constant (300) trimmed real detail out of descriptions that would have fit
# comfortably under the server's actual limit. Not imported from release_notes.py directly: those
# are that module's own private constants; a few characters of margin (10) covers the trailing
# "…" this truncation adds.
_MAX_TITLE_CHARS = 190
_MAX_DESCRIPTION_CHARS = 490


def _truncate(text, max_length):
    if len(text) <= max_length:
        return text
    return text[: max_length - 1].rstrip() + "…"


def _pr_description(pr):
    """First substantive line of a PR body, for the template fallback's per-change description.

    Every PR in this repo's own history opens with a `## Summary` (or similar) Markdown heading
    (`ship-issue`/`plan-issue`'s house style) -- a naive "first line of the first paragraph"
    extraction picks up that heading itself, not any real content. Skips blank lines, heading
    lines (`#...`), and horizontal rules, and strips a leading bullet/checkbox marker from
    whatever line it does land on. Truncated to `_MAX_DESCRIPTION_CHARS` -- see that constant's
    comment for why.
    """
    body = (pr.get("body") or "").strip()
    if not body:
        return "See the linked pull request for details."
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or line == "---":
            continue
        line = _MARKDOWN_BULLET_RE.sub("", line)
        line = _MARKDOWN_CHECKBOX_RE.sub("", line)
        if line:
            return _truncate(line, _MAX_DESCRIPTION_CHARS)
    return "See the linked pull request for details."


def build_template_entry(date, prs):
    """Plain, deterministic entry built directly from PR titles/bodies -- no LLM styling, but a
    complete, accurate entry every time. Used whenever the LLM path (`build_llm_entry`) is
    unconfigured or fails -- see the module docstring's point 3 and the plan doc's Candidate B2a.
    """
    changes = []
    for pr in prs:
        category = categorize_pr(pr)
        changes.append({
            "category": category,
            "audience": categorize_audience(category),
            "title": _truncate(pr.get("title") or "Untitled change", _MAX_TITLE_CHARS),
            "description": _pr_description(pr),
        })
    headline = (
        prs[0].get("title") if len(prs) == 1
        else f"{len(prs)} changes shipped"
    )
    summary = (
        f"One change shipped: {prs[0].get('title')}." if len(prs) == 1
        else f"{len(prs)} changes shipped today."
    )
    return {
        "date": date.isoformat(),
        "headline": _truncate(headline, _MAX_TITLE_CHARS),
        "summary": _truncate(summary, _MAX_DESCRIPTION_CHARS),
        "changes": changes,
    }


# Scaled by PR count, not a single fixed number -- a 2-PR day doesn't need much headroom, but
# _MAX_CHANGES_PER_ENTRY (release_notes.py, 50) means a busy day's response can legitimately run
# long: ~150 output tokens per change (category+title+description) is a reasonable estimate, plus
# a fixed allowance for the headline/summary and JSON structural overhead. A response cut off
# mid-JSON by an undersized limit fails json.loads below and falls back to the template --
# exactly the failure mode this sizing exists to avoid.
_TOKENS_PER_CHANGE = 150
_BASE_RESPONSE_TOKENS = 300


def _max_response_tokens(pr_count):
    return _BASE_RESPONSE_TOKENS + _TOKENS_PER_CHANGE * max(pr_count, 1)


def _call_claude(prompt_body, api_key, model, timeout, max_tokens):
    body = json.dumps({
        "model": model or _CLAUDE_DEFAULT_MODEL,
        "max_tokens": max_tokens,
        "system": _SYSTEM_PROMPT,
        "messages": [{"role": "user", "content": prompt_body}],
    }).encode("utf-8")
    request = Request(
        _CLAUDE_API_URL, data=body,
        headers={"x-api-key": api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    content = payload.get("content") or []
    text_blocks = [block.get("text", "") for block in content if block.get("type") == "text"]
    return "".join(text_blocks) if text_blocks else None


def _call_openai_compatible(prompt_body, api_key, model, api_base, timeout, max_tokens):
    if not api_base or not model:
        return None
    body = json.dumps({
        "model": model,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": prompt_body},
        ],
    }).encode("utf-8")
    request = Request(
        api_base.rstrip("/") + "/chat/completions", data=body,
        headers={"Authorization": f"Bearer {api_key}", "content-type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    choices = payload.get("choices") or []
    return (choices[0].get("message") or {}).get("content") if choices else None


# Some models wrap JSON output in a markdown code fence even when the system prompt explicitly
# says not to (_SYSTEM_PROMPT: "Output ONLY a single JSON object, no markdown fences"). Stripped
# before parsing -- the parsed result still goes through the exact same category/shape validation
# either way, so this recovers one specific, harmless formatting quirk without weakening the
# "never trust unvalidated LLM output" posture.
_MARKDOWN_JSON_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def _strip_markdown_fence(raw_text):
    return _MARKDOWN_JSON_FENCE_RE.sub("", raw_text.strip()).strip()


def _build_prompt(prs):
    lines = [f"Pull request {index + 1}: {pr.get('title')}\n{( pr.get('body') or '').strip()}" for index, pr in enumerate(prs)]
    return "\n\n---\n\n".join(lines)


def build_llm_entry(date, prs, caller=None):
    """Attempt LLM-generated copy for `date`'s entry. Returns `None` on any failure (unconfigured,
    network error, malformed response, invalid category) -- callers must fall back to
    `build_template_entry`, never raise into the caller (module docstring, point 3 / plan doc
    Candidate B2a). `caller`, when given, replaces the real HTTPS call (tests only).
    """
    provider = os.environ.get(LLM_PROVIDER_ENV_VAR)
    api_key = os.environ.get(LLM_API_KEY_ENV_VAR)
    if not provider or not api_key:
        return None
    prompt_body = _build_prompt(prs)
    max_tokens = _max_response_tokens(len(prs))
    try:
        if caller is not None:
            raw_text = caller(prompt_body)
        elif provider == "claude":
            raw_text = _call_claude(
                prompt_body, api_key, os.environ.get(LLM_MODEL_ENV_VAR), _LLM_TIMEOUT_SECONDS, max_tokens,
            )
        elif provider == "openai_compatible":
            raw_text = _call_openai_compatible(
                prompt_body, api_key, os.environ.get(LLM_MODEL_ENV_VAR),
                os.environ.get(LLM_API_BASE_ENV_VAR), _LLM_TIMEOUT_SECONDS, max_tokens,
            )
        else:
            return None
    except (URLError, HTTPError, TimeoutError, OSError, ValueError):
        return None
    if not raw_text:
        return None
    try:
        parsed = json.loads(_strip_markdown_fence(raw_text))
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None
    changes = parsed.get("changes")
    if not isinstance(changes, list) or not changes:
        return None
    for change in changes:
        if not isinstance(change, dict):
            return None
        if change.get("category") not in CATEGORIES or change.get("audience") not in AUDIENCES:
            return None
    entry = {"date": date.isoformat(), "headline": parsed.get("headline"), "summary": parsed.get("summary"), "changes": changes}
    try:
        validate_entry_payload(entry)
    except Exception:
        return None
    return entry


def generate_entry(date, prs, llm_caller=None):
    """Build the day's entry: try the LLM path first, fall back to the template -- see module
    docstring point 3.
    """
    entry = build_llm_entry(date, prs, caller=llm_caller)
    if entry is not None:
        return entry, "llm"
    return build_template_entry(date, prs), "template"


def publish_entry(entry, dashboard_base_url, refresh_token, timeout=_REQUEST_TIMEOUT_SECONDS):
    """POST the entry to the live dashboard's /api/release-notes -- required; a failure here is a
    real failure of this run (module docstring, point 4).
    """
    request = Request(
        f"{dashboard_base_url}/api/release-notes",
        data=json.dumps(entry).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Refresh-Token": refresh_token},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return response.status, response.read()


def write_archive_file(entry, root=ROOT):
    """Write the entry's Markdown form to release-notes/<date>.md -- the git-tracked archival
    half of the dual-write design (module docstring, point 5). Committing/pushing this file is
    the workflow's own job, not this function's."""
    directory = Path(root) / "release-notes"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{entry['date']}.md"
    path.write_text(render_entry_markdown(entry), encoding="utf-8")
    return path


def run(dry_run, now=None, llm_caller=None, dashboard_base_url=None, refresh_token=None, root=ROOT):
    date = target_date(now)
    repository = _require_repository()
    prs = fetch_merged_prs(repository, date, github_token=os.environ.get(GITHUB_TOKEN_ENV_VAR))
    if not prs:
        print(f"checked: nothing merged on {date.isoformat()} (ET) -- no entry published")
        return 0

    entry, source = generate_entry(date, prs, llm_caller=llm_caller)
    print(f"generated {date.isoformat()}'s entry from {len(prs)} merged PR(s) via {source}")

    if dry_run:
        print(json.dumps(entry, indent=2))
        return 0

    publish_entry(entry, dashboard_base_url, refresh_token)
    path = write_archive_file(entry, root=root)
    print(f"published {date.isoformat()} and wrote {path}")
    return 0


def backfill(start_date, end_date, publish, dashboard_base_url=None, refresh_token=None, root=ROOT, llm_caller=None):
    """One-time historical seed, not part of the daily job: generate (and archive, and optionally
    publish) an entry for every ET calendar day in `[start_date, end_date]` that had at least one
    merged PR. Days with nothing merged are silently skipped, same no-op semantics as the daily
    run. Returns the list of dates an entry was actually written for.

    Unlike `run()`, this always writes the archive file regardless of `publish` -- backfilling
    history is meaningfully a git-archival operation first; publishing each entry live is an
    explicit opt-in (`publish=True`) on top of that, since it means one HTTP call per historical
    day against a real running server.
    """
    repository = _require_repository()
    written = []
    current = start_date
    while current <= end_date:
        prs = fetch_merged_prs(repository, current, github_token=os.environ.get(GITHUB_TOKEN_ENV_VAR))
        if prs:
            entry, source = generate_entry(current, prs, llm_caller=llm_caller)
            path = write_archive_file(entry, root=root)
            print(f"backfilled {current.isoformat()}: {len(prs)} PR(s) via {source} -> {path}")
            if publish:
                publish_entry(entry, dashboard_base_url, refresh_token)
                print(f"  published {current.isoformat()}")
            written.append(current)
        else:
            print(f"backfilled {current.isoformat()}: nothing merged -- skipped")
        current = current + timedelta(days=1)
    return written


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the generated entry instead of publishing/writing it. Does not require "
             "FPL_INTEL_DASHBOARD_BASE_URL/FPL_INTEL_REFRESH_TOKEN.",
    )
    parser.add_argument(
        "--skip-hour-check", action="store_true",
        help="Bypass is_correct_scheduled_trigger's DST-safe gate (for manual workflow_dispatch runs).",
    )
    parser.add_argument(
        "--schedule", default="",
        help="The exact cron expression GitHub Actions fired this run from (github.event.schedule). "
             "Checked against today's actual DST regime by is_correct_scheduled_trigger; empty/"
             "unrecognized is never treated as correct. Irrelevant with --skip-hour-check.",
    )
    parser.add_argument(
        "--backfill-start", metavar="YYYY-MM-DD",
        help="One-time historical seed mode: write (and, with --backfill-publish, publish) an "
             "entry for every ET day from this date through --backfill-end (default: yesterday) "
             "that had at least one merged PR. Bypasses the hour check and --dry-run entirely -- "
             "a distinct mode from the daily run, not a variant of it.",
    )
    parser.add_argument("--backfill-end", metavar="YYYY-MM-DD", help="Defaults to yesterday (ET).")
    parser.add_argument(
        "--backfill-publish", action="store_true",
        help="Also POST each backfilled entry to the live dashboard (requires "
             "FPL_INTEL_DASHBOARD_BASE_URL/FPL_INTEL_REFRESH_TOKEN). Without this, backfill only "
             "writes the git-tracked release-notes/ archive.",
    )
    args = parser.parse_args(argv)

    if args.backfill_start:
        try:
            start = date.fromisoformat(args.backfill_start)
            end = date.fromisoformat(args.backfill_end) if args.backfill_end else target_date()
        except ValueError as error:
            print(f"Configuration error: {error}", file=sys.stderr)
            return 1
        dashboard_base_url = refresh_token = None
        if args.backfill_publish:
            try:
                dashboard_base_url = _require_dashboard_base_url()
                refresh_token = _require_refresh_token()
            except ConfigError as error:
                print(f"Configuration error: {error}", file=sys.stderr)
                return 1
        try:
            written = backfill(
                start, end, args.backfill_publish,
                dashboard_base_url=dashboard_base_url, refresh_token=refresh_token,
            )
        except ConfigError as error:
            print(f"Configuration error: {error}", file=sys.stderr)
            return 1
        print(f"backfill complete: {len(written)} day(s) with entries between {start} and {end}")
        return 0

    if not args.skip_hour_check and not is_correct_scheduled_trigger(args.schedule):
        print("checked: this cron trigger does not match today's DST regime -- no action")
        return 0

    dashboard_base_url = refresh_token = None
    if not args.dry_run:
        try:
            dashboard_base_url = _require_dashboard_base_url()
            refresh_token = _require_refresh_token()
        except ConfigError as error:
            print(f"Configuration error: {error}", file=sys.stderr)
            return 1

    try:
        return run(args.dry_run, dashboard_base_url=dashboard_base_url, refresh_token=refresh_token)
    except ConfigError as error:
        print(f"Configuration error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
