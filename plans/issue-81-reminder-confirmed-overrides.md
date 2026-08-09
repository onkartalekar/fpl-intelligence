# Issue #81: Apply saved confirmed-free-transfer/draft-squad overrides in the deadline reminder

## Context

Issue #81 claims this is small and self-contained with no open design questions: `scripts/send_deadline_reminder.py`'s `run()` calls `compute_manager_view(bootstrap, fixtures, [], generated_at, team["team_id"])` without passing `confirmed_free_transfers`, `confirmed_free_transfers_event`, or `draft_squad_ids`, while `server.py`'s `_default_team_view_action` already loads and passes all three from a saved profile. This note records the investigation that confirms the claim and states the exact change needed; per this repo's `plan-issue` convention, no candidates/recommendation-style doc is warranted.

## What was verified

- **`scripts/send_deadline_reminder.py`'s `run()`** (line 326): confirmed the call site is exactly as described — `compute_manager_view(bootstrap, fixtures, [], generated_at, team["team_id"])`, no override kwargs. `run()` already takes `root=ROOT` as a parameter, and `Path` is already imported (line 28) but `fpl_intel.profiles` is not.
- **`src/fpl_intel/server.py`'s `_default_team_view_action`** (lines 114-140): the pattern to copy already exists verbatim. It calls `profiles.load_profile(_profiles_db_path(root), team_id)`, then passes `confirmed_free_transfers=saved["confirmed_free_transfers"] if saved else None`, `confirmed_free_transfers_event=saved["confirmed_free_transfers_event"] if saved else None`, `draft_squad_ids=saved["draft_squad"] if saved else None` into `compute_manager_view`. `_profiles_db_path(root)` (line 143) is just `Path(root) / "data" / "profiles.db"` — trivially reproducible in the reminder script from its own existing `root` parameter, no new plumbing required.
- **`src/fpl_intel/profiles.py`'s `load_profile`** (line 79): confirmed the exact return shape via `_row_to_dict` — a dict with keys `confirmed_free_transfers`, `confirmed_free_transfers_event`, and `draft_squad` (already JSON-decoded to a list, or `None`) among others, or `None` itself if the team has never saved a profile. This matches what the issue and `server.py`'s usage both assume.
- **`src/fpl_intel/refresh.py`'s `compute_manager_view`** (line 464): signature already accepts `confirmed_free_transfers=None, confirmed_free_transfers_event=None, draft_squad_ids=None` as optional keyword arguments — no signature change needed, only the reminder script's call site.
- **`tests/test_send_deadline_reminder.py`**: existing `RunLoopTests` mock `sdr.compute_manager_view` via `patch.object(sdr, "compute_manager_view", return_value=...)` and assert on send behavior/log output; a new test would follow the same style, additionally mocking `fpl_intel.profiles.load_profile` (or patching `sdr.profiles.load_profile` once imported) and asserting the override kwargs reach `compute_manager_view` via `mock.call_args`.

## Confirmation: no open design questions

- No ambiguity about the "never-saved profile" case: `load_profile` already returns `None` for it, and the existing `if saved else None` pattern in `server.py` already handles that by passing all three overrides as `None` — i.e., unchanged behavior from today. The issue's own requirement ("A missing/never-saved profile should behave exactly as it does today") is satisfied by copying the existing pattern verbatim, not by writing new logic.
- No ambiguity about where `profiles.db` lives relative to the reminder script: `run()` already receives `root` (defaulting to `ROOT`, the repo root resolved at module load, same value `load_bootstrap_and_fixtures(root)` already uses), and `_profiles_db_path` in both `server.py` and `refresh.py` derive the path the same trivial way (`Path(root) / "data" / "profiles.db"`) — no new configuration or database-discovery logic needed.
- No signature or behavioral changes required anywhere except the one call site in `scripts/send_deadline_reminder.py`.

This is a mechanical wire-through with the pattern fully established elsewhere in the codebase — no genuine design choice to make.

## The exact change needed

In `scripts/send_deadline_reminder.py`:

1. Add an import: `from fpl_intel import profiles` (alongside the existing `fpl_intel.refresh`/`fpl_intel.recommendations` imports around lines 35-37).
2. In `run()`, inside the `for team in in_window_teams:` loop (line 325), before the `compute_manager_view` call, load the saved profile and pass its three fields through:

```python
saved = profiles.load_profile(root / "data" / "profiles.db", team["team_id"])
manager_view = compute_manager_view(
    bootstrap, fixtures, [], generated_at, team["team_id"],
    confirmed_free_transfers=saved["confirmed_free_transfers"] if saved else None,
    confirmed_free_transfers_event=saved["confirmed_free_transfers_event"] if saved else None,
    draft_squad_ids=saved["draft_squad"] if saved else None,
)
```

This mirrors `server.py`'s `_default_team_view_action` (lines 127-138) exactly, differing only in that the reminder script's `root` is already a plain parameter rather than something requiring `_profiles_db_path`'s own helper (though defining an equivalent tiny helper in the script, or importing `refresh._profiles_db_path`, would also be reasonable and is a purely cosmetic choice for the implementation pass, not a design question).

A new test in `tests/test_send_deadline_reminder.py`'s `RunLoopTests` class should mock `profiles.load_profile` (patched as `sdr.profiles.load_profile` once the import above is added) to return a profile with non-`None` `confirmed_free_transfers`/`confirmed_free_transfers_event`/`draft_squad`, mock `compute_manager_view`, and assert via `mock_compute.call_args.kwargs` that all three values were passed through — plus a companion test confirming a `None` profile (team never saved) still passes all three as `None`, matching today's behavior.

## Recommendation

Build as specified in the issue. No candidates to weigh; hand off directly to a `/ship-issue` pass for the actual code change and test.
