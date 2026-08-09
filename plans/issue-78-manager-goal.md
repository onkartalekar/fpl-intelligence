# Add a manager `goal` field to the profile (issue #78)

Researched 2026-08-09. Issue: add a `goal` field (stated season target,
default top-50k) to the per-team profile schema and Manager Profile form,
raised while reviewing #55's deadline reminder against the originally
intended soft-registration workflow (team ID + goal -> opt into
reminders -> personalized emails). Two open questions the issue itself
flags: the exact option set, and whether `goal` does anything today or
is pure metadata.

## Context

`profiles.py`'s schema is `team_id, timezone, risk_profile,
confirmed_free_transfers, confirmed_free_transfers_event, email,
draft_squad, opted_out, pin_hash, created_at, updated_at`. `risk_profile`
(conservative/balanced/aggressive) already answers a related but distinct
question -- *how* the model should trade upside against downside on the
weekly transfer decision -- not a manager's actual finishing target.
Nothing in the schema captures the latter at all. The Manager Profile
form (`src/fpl_intel/dashboard.py`, `#profile-form`) has fields for team
ID, timezone, risk profile, and confirmed free transfers (count +
gameweek) only.

Issue #61's `draft_squad` column addition is the most recent precedent
for "add a column + form field" in this codebase and is explicitly
named in the issue body as the shape to mirror. It turns out to be a
partial match, not an exact one -- see the structural finding below.

## Structural finding: `goal` needs a read-time-default pattern this schema has never needed before

The issue asks for `goal` to be a nullable column with its default
("top-50k") applied at read time, "matching how `risk_profile`/
`timezone` defaults are already applied in `_default_visitor_profile_action`."
Reading that function and the schema closely shows the analogy is only
partial:

- `timezone` and `risk_profile` are `NOT NULL` at the schema level.
  `_default_visitor_profile_action`'s `_DEFAULT_VISITOR_PROFILE` merge
  only fires when **no row exists at all** (`saved is None`) -- for any
  row that *does* exist, `timezone`/`risk_profile` are guaranteed
  non-NULL by the schema constraint and by `_validate_profile_payload`
  requiring both on every `/api/profile` save. There is no "row exists
  but this column is NULL" case for either field today.
- `draft_squad`/`confirmed_free_transfers`/`email` are nullable, but
  their real "unset" value *is* `None` -- nothing defaults them to a
  non-null business value when NULL. They're preserve-only fields
  written by their own dedicated paths (`save_draft_squad`, the #55
  opt-in), never implied by an unrelated save.

`goal` is neither: it's nullable (per the issue's explicit ask, so
existing rows never need a migration backfill) *and* has a real non-null
default ("top_50k") that should show up in every read, including for
rows that predate this column or were created by a path that never sets
it (`save_draft_squad`, `set_lookup_opt_out`, both of which can create a
brand-new row before a manager ever touches the profile form). That
combination is new to this schema and needs its own read-time
substitution, not a reuse of the existing "no row at all" branch alone.

**Recommended fix, not just a caveat:** put the substitution in
`profiles.py`'s `_row_to_dict`, using a new `_DEFAULT_GOAL = "top_50k"`
constant alongside the existing `_DEFAULT_TIMEZONE`/`_DEFAULT_RISK_PROFILE`
module-level constants, e.g. `"goal": row[N] or _DEFAULT_GOAL`. This
makes `load_profile` itself always return a resolved goal, so every
caller (`server.py`'s two profile-reading actions, `refresh.py`'s
`compute_manager_view`, and any future reader such as a `goal`-aware
version of `scripts/send_deadline_reminder.py`) sees a consistently
defaulted value without each needing to remember its own `saved.get("goal")
or "top_50k"` fallback -- avoiding exactly the kind of duplicated-default
bug class this codebase already guards against by centralizing
`_DEFAULT_TIMEZONE`/`_DEFAULT_RISK_PROFILE` in one place. `server.py`'s
`_DEFAULT_VISITOR_PROFILE` dict (the *no-row-at-all* case, which never
touches `_row_to_dict`) should gain a matching `"goal": "top_50k"` entry
for symmetry, ideally importing the same constant rather than
re-literaling it.

Everywhere else, the `draft_squad`-column precedent (issue #61) *does*
transfer directly:

- `_SCHEMA`/`_COLUMNS` gain `goal TEXT` in the same nullable style as
  `draft_squad TEXT`.
- `save_profile` is the field's real write path (unlike `draft_squad`,
  which is deliberately *not* settable there) -- it needs a new `goal`
  parameter threaded into the `INSERT`'s column list and value tuple, and
  added to the `ON CONFLICT(team_id) DO UPDATE SET` clause alongside
  `timezone`/`risk_profile`/`confirmed_free_transfers*` (not treated as
  preserve-only like `email`/`draft_squad`, since the whole point of this
  issue is that the profile form can set it directly).
- `save_draft_squad` and `set_lookup_opt_out` don't set `goal`, but their
  existing-row `SELECT` needs to also fetch it so their `INSERT`'s
  preserve-what-you-don't-touch branch carries it forward for an
  existing row -- same as they already do for `email`/`timezone`/
  `risk_profile`. For a genuinely brand-new row created by either path,
  it's fine to leave `goal` out of the `INSERT` column list entirely
  (`draft_squad` is already handled exactly this way in
  `set_lookup_opt_out`'s `INSERT`) -- the NULL that results is exactly
  what `_row_to_dict`'s new default substitution is for.

## Candidate operationalizations / Findings

### (1) Option set

The issue's own suggested set: "Top 10k" / "Top 50k" (default) / "Top
100k" / "Beat last season" / "Just have fun / no specific target." This
maps cleanly onto FPL's actual player-base shape and commonly-referenced
rank milestones (roughly 11M total entrants; top-10k and top-100k finishes
are the benchmarks the FPL community itself already organizes around --
e.g. FPL's own "Green Arrow"/overall-rank milestone culture), so it isn't
an arbitrary invented scale. Each option is also independently checkable
against data the model or a future feature already has or plausibly will
have (`overall_rank` from the FPL API for the numeric tiers; nothing to
check for the qualitative "beat last season"/"just for fun" options,
which is fine since neither claims to be measured).

Considered and rejected additions:
- **A numeric free-entry field** ("type your target rank") -- rejected.
  Free text defeats the point of a small fixed set (the issue explicitly
  asks for one), complicates validation, and produces a long tail of
  values nothing can meaningfully act on (an ML feature or copy template
  keyed on 5 known buckets is tractable; keyed on arbitrary integers, it
  isn't).
- **"Top 1k"** -- rejected. Realistic only for a vanishingly small
  fraction of managers; adds a sixth option for a target band the other
  four already imply is out of reach for "top_10k" selectors, without
  giving the model or copy anything a "top_10k" reader wouldn't already
  cover just as well.
- **"Mini-league only"** -- rejected as a separate option. Overlaps
  heavily with "just for fun" in what it would imply about model
  behavior (neither maps to any absolute-rank signal this app can check),
  and the issue didn't ask for it. Worth revisiting only if a future
  mini-league feature (not currently in this codebase or its issue
  backlog) needs to distinguish the two.

**Recommendation: adopt the issue's suggested set as-is**, stored as
short keys with human labels in the form, matching how `risk_profile`
stores `conservative`/`balanced`/`aggressive` rather than display text:

| Stored value | Form label |
|---|---|
| `top_10k` | Top 10k |
| `top_50k` (default) | Top 50k |
| `top_100k` | Top 100k |
| `beat_last_season` | Beat last season |
| `just_for_fun` | Just have fun / no specific target |

### (2) Does `goal` change any model or UI behavior today?

Three concrete hooks were evaluated, since "leave it purely
informational" should be a decision made after checking, not a default
taken because wiring something up looked hard.

**(a) Auto-derive a `risk_profile` suggestion from `goal` -- evaluated
and declined for now.** Mechanically easy (`goal -> risk_profile` is a
one-line lookup table), but it creates two dials that visibly disagree
with each other on the same form: `risk_profile` is already a directly
user-editable field sitting right next to where `goal` would go, and it
is not actually implied by `goal` in the way the mapping would assume.
A manager chasing top-10k who is already comfortably inside the top 10k
late in the season has good reason to turn *conservative* to protect
rank -- the opposite of what a naive `top_10k -> aggressive` mapping
would suggest, and this app has no rank-trajectory signal to know which
case applies. A "just for fun" manager might still want aggressive,
high-variance picks precisely because the stakes are low. Silently
overwriting or pre-selecting a field the manager can already set
directly, using an inference this app can't validate, is worse than
leaving the two fields independent. This is the strongest case in the
issue for *not* wiring goal into existing behavior yet -- not because
it's hard, but because it would substitute a guess for a field the
manager already controls precisely.

**(b) Drop a `goal`-aware line into `model.limitations` / the "Model
basis and risks" panel -- evaluated and declined, differently from how
#65 used this same hook.** #65's shadow-model plan added a line to this
list, but that list is uniform, non-personalized copy describing the
*model's own construction* ("expected minutes are inferred, not
predicted," "an ML minutes model is being evaluated in shadow") -- every
visitor sees the identical list regardless of who they are. Splicing in
a manager-specific "since you're chasing top 10k, ..." line would be a
new kind of content in that slot: personalized advisory copy, not model
epistemics. That's a bigger, differently-scoped change than "read a
field and print a string," and it's the kind of content this app has
so far kept out of `limitations` on purpose. Not ruled out forever, but
not a low-risk drop-in the way #65's line was.

**(c) Surface `goal` in the deadline reminder email (#55) -- checked
directly, and currently impossible regardless of any wiring choice
made here.** `scripts/send_deadline_reminder.py` never reads
`data/profiles.db` at all -- recipients and their settings come entirely
from the admin-configured `FPL_INTEL_REMINDER_TEAMS` environment
variable (see `plans/issue-55-deadline-email-reminder.md`'s re-plan
note: the self-serve, profile-driven opt-in flow was explicitly scoped
out of #55 and deferred to a future self-serve signup issue). There is
currently no code path that could read a manager's saved `goal` and put
it in an email even if this issue wired it up. This matches the issue
body's own framing ("once #79/#82 exist") -- confirmed here by reading
the actual reminder script rather than assumed.

## Recommendation

**Build the schema + form addition. Keep `goal` metadata-only for this
issue; do not auto-derive `risk_profile` from it, and do not splice it
into `model.limitations`.** Concretely:

1. `profiles.py`: add the `goal TEXT` column (nullable), the
   `_DEFAULT_GOAL = "top_50k"` constant, and the `_row_to_dict` read-time
   substitution described in the structural finding above. Thread `goal`
   through `save_profile` as a real, settable parameter (not
   preserve-only). Thread it through `save_draft_squad`'s and
   `set_lookup_opt_out`'s existing-row preservation the same way
   `timezone`/`risk_profile`/`email` already are.
2. `server.py`: add `"goal"` to `_ALLOWED_PROFILE_KEYS`, add
   `_ALLOWED_GOALS = {"top_10k", "top_50k", "top_100k",
   "beat_last_season", "just_for_fun"}` mirroring
   `_ALLOWED_RISK_PROFILES`, validate it the same way `risk_profile` is
   validated in `_validate_profile_payload`, pass it through
   `_default_profile_action`'s `profiles.save_profile(...)` call, and add
   `"goal": "top_50k"` to `_DEFAULT_VISITOR_PROFILE` and the returned
   dict in `_default_visitor_profile_action`.
3. `dashboard.py`: add a `<select id="profile-goal">` field to
   `#profile-form`'s `profile-form-grid`, with the five options above
   (`top_50k` selected by default), placed next to `profile-risk` since
   both are "how you want the model/this app to relate to your season,"
   even though they stay functionally independent per the recommendation
   above.
4. `dashboard.js`: wire `profile-goal` into `setupProfileForm()` the same
   way `risk-select` already is -- populate from `profile.goal`,
   default to `top_50k` when unset, include it in the `/api/profile`
   POST payload. No validation logic needed beyond "is one of the fixed
   `<option>` values" (the browser's own `<select>` already constrains
   this; server-side validation is the real gate, matching how
   `risk_profile`'s options are enforced).
5. `refresh.py`: add `"goal": profile.get("manager", {}).get("goal") or
   "top_50k"` to the `state["profile"]` dict it builds (same spot
   `risk_profile` is threaded through), so the dashboard can display a
   manager's stated goal on their own profile view without a second
   round trip. No behavior change -- display only.
6. Tests: mirror `tests/test_profiles.py`'s `draft_squad` coverage --
   save/load round trip, default substitution for an unset/pre-migration
   row, preservation across `save_draft_squad`/`set_lookup_opt_out`
   writes that don't touch `goal`. Mirror `tests/test_server.py`'s
   `risk_profile` validation coverage for `_ALLOWED_GOALS` rejection of
   invalid values.
7. Document the metadata-only status honestly in the code, not just this
   plan: a one-line docstring note on the `goal` column/field (matching
   how `draft_squad`'s and `email`'s docstrings already state their own
   scope precisely) saying it does not currently affect `risk_profile`
   selection or any model output -- so a future reader isn't left
   guessing whether it's wired up somewhere non-obvious.

**Explicitly out of scope for this issue** (both already covered above,
recorded here so `/ship-issue` doesn't accidentally scope-creep into
them): auto-deriving `risk_profile` from `goal` (declined above, not
deferred-and-forgotten -- if revisited, it needs a rank-trajectory signal
this app doesn't have before a mapping could be trusted), and any
reminder-email wiring (blocked on #79/#82's self-serve opt-in existing
at all, not on anything decided in this issue).

## If declined: not applicable

This issue's core ask (the column + form field) is recommended as a
straightforward build with no viable competing direction -- the only
real decisions were the option set and the wiring question, both
resolved above. No `IMPLEMENTATION_PLAN.md` "Considered and declined"
entry is needed for the build itself. If the maintainer wants either
declined behavior-wiring option reconsidered, that decision (and its
own "considered and declined" entry, if declined again) belongs in a
future issue once the missing prerequisite (a rank-trajectory signal
for (a), #79/#82 for (c)) actually exists.
