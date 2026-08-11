# Stale `dashboard.html` for no-`team_id` visitors after a code deploy (issue #120)

## Context

`_serve_dashboard` (`src/fpl_intel/server.py:1052`) has two paths, and they behave differently
with respect to code freshness:

- **No `team_id`** (`server.py:1062-1067`) reads the literal bytes of the `dashboard.html` file on
  disk and sends them as-is. That file is written exactly once per `/api/refresh` run, by
  `render_dashboard(state)` inside `publish_generation(...)` (`refresh.py:465-468`). Redeploying
  the app updates the Python image but never touches this file -- it stays baked with whatever
  template/CSS/JS code was live at the *last* refresh, however old that was.
- **`team_id` resolved** (cookie or `?team_id=`, `server.py:1089` / `server.py:1133`) calls
  `render_dashboard(state)` fresh on every request, reading `dashboard-state.json` and the
  currently-loaded `dashboard.py`/`dashboard.css`/`dashboard.js` module content. Always current.

This asymmetry caused a real incident: three merged PRs (#109, #111, #115) were live in the
deployed Railway code but invisible to any visitor without a saved team, until a human remembered
to run `/api/refresh` by hand.

## Structural findings before evaluating candidates

**`render_dashboard` is cheap and side-effect-free.** It does no I/O and no live API calls --
just `json.dumps(state)` plus four `str.replace()` calls against the in-memory
`_DASHBOARD_CSS`/`_DASHBOARD_JS` module constants (`dashboard.py:83-94`). Measured today:
`dashboard.css` + `dashboard.js` together are ~127KB of static text; `dashboard-state.json` is
comparable in scale to what the `team_id` path already reads and re-serializes on every request in
production right now. In other words: **candidate (a) below imposes no new cost** -- it's the
exact same per-request work already proven to hold up in production for every cookied visitor,
just extended to the visitors who currently skip it.

**The test suite currently treats `dashboard.html` as a hand-authored fixture, not a real
generated artifact.** Roughly 20 `setUp()` blocks across `tests/test_server.py` write literal
strings straight into `(self.root / "dashboard.html")` (e.g. `'<h1>Dashboard</h1>'`, or in
`ConnectionStatusResolutionTests`, a raw JSON blob standing in for the "pre-generated artifact")
and then assert the no-`team_id` response echoes that exact content back. This is a real,
mechanical migration cost for candidate (a): those fixtures would need to instead seed
`dashboard-state.json` with the equivalent data and let `render_dashboard` produce the HTML, since
the raw-file-echo behavior they depend on would no longer exist. It's grunt work, not a design
risk -- every one of those tests has an obvious equivalent under the new behavior.

## Candidate operationalizations

**(a) Always render fresh, drop the static-file path entirely.**
Delete the `if team_id is None: ... dashboard.read_text() ...` branch; route everyone through the
same `state = json.loads(dashboard-state.json...); render_dashboard(state)` call the `team_id`
path already uses (minus the per-team splicing, which only applies when a team is known). One code
path for every visitor, permanently -- this class of bug becomes structurally impossible, not just
patched. Cost: the ~20-test fixture migration above, plus removing the now-unused
`dashboard.html`-file publication step from `publish_generation`/`refresh.py` (or keeping it
written for backward compatibility/manual inspection but simply no longer reading it at serve
time -- worth deciding during implementation, not blocking here).

**(b) Keep serving the static file, but auto-regenerate it on boot if it predates the running
code.**
E.g. hash `dashboard.py`+`dashboard.css`+`dashboard.js`'s combined content (or use the git commit
SHA baked in at build time) and compare against a stamp recorded in the last-published generation;
if it doesn't match, re-run `render_dashboard(state)` from the existing `dashboard-state.json` and
republish before the server starts accepting traffic. Keeps the current per-request cost profile
(cheap file read) and requires no test-fixture migration. Real downsides: introduces a new piece of
state (the code-version stamp) that itself needs to be got right and kept in sync, adds boot-time
complexity, and leaves a structural gap it doesn't actually close -- if `dashboard-state.json` is
itself missing (fresh volume, pre-first-refresh), there's nothing to regenerate from, so the
`404` "Dashboard has not been generated" case still needs handling exactly as today. It also
doesn't fully satisfy the user's actual framing ("this is the same user flow" as the `team_id`
path) -- it's a parallel mechanism that produces the same *result*, not the same *path*.

**(c) Per-request version-stamp check with fallback to fresh render on mismatch.**
Same idea as (b) but checked on every request instead of once at boot, falling back to a fresh
render (effectively (a)) on a mismatch. This is strictly more complex than either (a) or (b) for
no real gain: it pays (a)'s per-request comparison cost *and* (b)'s version-stamp-maintenance cost,
while only saving a fresh render in the narrow window between a deploy and the next reboot's
regeneration -- a window (b) already closes at boot. Not worth carrying forward as a separate
option.

## Recommendation

**Build (a).** It's the simplest, it's the one that actually matches the user's own framing (same
code path as the `team_id` flow, not a parallel staleness-detection mechanism), it eliminates the
whole bug class rather than papering over it with a freshness check that could itself have edge
cases, and its added per-request cost is already validated in production today by the `team_id`
path. The only real cost -- migrating ~20 test fixtures from "hand-write `dashboard.html`" to
"hand-write `dashboard-state.json`" -- is mechanical, not risky, and is exactly the kind of
regression-test extension [[ship-issue]]'s step 3 already calls for.

(b) and (c) are declined -- not because they wouldn't work, but because they add real ongoing
complexity (a version-stamp mechanism that must itself stay correct) to solve a problem (a) solves
for free, given `render_dashboard`'s proven low cost.

### Declined-candidate text for `IMPLEMENTATION_PLAN.md`

```markdown
### Boot-time or per-request `dashboard.html` freshness stamp (issue #120)

Considered for closing the gap where redeploying the app leaves a stale, pre-generated
`dashboard.html` live for no-`team_id` visitors. Declined in favor of always rendering
`dashboard.html` fresh per-request (the same code path already used for `team_id`-resolved
visitors) -- `render_dashboard` was confirmed cheap and side-effect-free (no I/O, no live API
calls), so a version-stamp/staleness-detection mechanism would only add ongoing complexity (a
stamp that itself must stay correct, plus either a boot-time or per-request comparison) to solve a
problem the existing per-request-render path already solves for free. See
`plans/issue-120-stale-dashboard-html.md`.
```

## Open question for the user

None remaining on the direction -- (a) is a clear recommendation, not a close call. The one
implementation detail worth a quick confirm before shipping: should `publish_generation` stop
writing the `dashboard.html` file to disk at all (since nothing will read it at serve time), or
keep writing it for manual/debugging inspection even though the server ignores it? Leaning toward
keeping the write (cheap, useful for a human to eyeball what got generated) unless you'd rather
remove it entirely.
