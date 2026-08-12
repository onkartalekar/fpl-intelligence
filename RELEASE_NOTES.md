# FPL Intelligence: Alpha Release Notes

A feature inventory for people who already know FPL but have not seen
this tool. This describes what the dashboard does today, on `main` --
not what is planned or in progress. See `IMPLEMENTATION_PLAN.md` for
history and `MODEL.md`/`SPECIFICATION.md` for the underlying contract.

For **day-by-day, dated** release notes (issue #143) -- what shipped and
when, rather than this file's always-current snapshot -- see the live
dashboard's **What's New** tab, or the git-tracked history under
[`release-notes/`](release-notes/). This file's content became that
history's first dated entry (`release-notes/2026-08-10.md`) and
continues to describe the current feature set going forward; it isn't
replaced by the dated history, the two serve different purposes.

Each item below is tagged:

- **[Live]** -- drives what you see in the dashboard today.
- **[Experimental]** -- computed and visible, but does not drive any
  recommendation.
- **[Opt-in]** -- off by default, requires an explicit action to enable.

## What this is

A local, source-backed decision tool for the 2026/27 Fantasy Premier
League season. It reads your public FPL team ID the same way the
official site's "entry" pages are public, and writes nothing back --
no password, no account action, ever. Every number traces back to an
official FPL field, an official Premier League announcement, or a
formula documented in the dashboard's own Model Status view -- never a
tip sheet, a vibe, or betting odds.

## Preseason overview

- **[Live]** Feed readiness -- shows whether the official bootstrap,
  fixtures, and transfer-centre feeds are live for the new season yet,
  rather than guessing at a squad before real 2026/27 prices and
  fixtures exist.

## Decision Center

- **[Live]** Weekly recommendation -- a single roll/transfer/chip call
  for your actual squad and risk profile, scored against doing nothing.
- **[Live]** Five-gameweek planner -- the next move plus conditional
  future branches. Only the immediate action is a firm recommendation;
  the rest is scenario, not prophecy.
- **[Live]** Three risk profiles side by side -- conservative, balanced,
  aggressive, each with its own XI, bench, and captain.
- **[Live]** Recommended XI, bench order, and captaincy, with a
  five-gameweek rotation forecast for the chosen profile.
- **[Live]** Watchlist -- top projected players outside your squad,
  click-through to full scoring detail.
- **[Live]** Player scoring breakdown -- every projection decomposed
  into its named components (appearance, attacking, clean sheet, saves,
  bonus, residual). Nothing is a black-box number.

## My Team / My Profile

- **[Live]** No-signup team lookup -- view any public squad by team ID,
  no account created.
- **[Live]** Manager profile -- team ID, timezone, risk profile,
  confirmed free transfers. Saved locally, no password.
- **[Live]** Preseason draft squad -- declare a provisional squad and
  get personalized recommendations before GW1's official picks exist.
- **[Opt-in]** Opt-out lookup -- a manager can exclude their team from
  derived recommendations entirely.
- **[Opt-in]** Deadline email reminder -- recommendations emailed a
  configurable number of hours before deadline, across all three risk
  profiles, with your confirmed transfers and draft squad already
  applied. Sending runs on a scheduled job outside the dashboard itself.

## Player Explorer / Fixtures

- **[Live]** Player Explorer -- search, filter by club and position,
  sort by price or ownership. Official prices and availability status
  only, no projections mixed in.
- **[Live]** Fixtures -- kickoffs and official Fixture Difficulty
  Rating (1-5) by club, with prev/next gameweek navigation.

## Transfers & News

- **[Live]** Confirmed transfer feed -- origin/destination club,
  announcement timestamp, source URL, and FPL reconciliation status once
  the player appears in the official feed. Every record traces to a
  first-party club or Premier League announcement; rumors are excluded
  by policy, not just by taste.
- **[Live]** Transfer-triggered projection review -- a confirmed move
  flags the transferred player, their new role competitors, and the
  squad they left behind for re-scoring.

## Model Performance / Model Status

- **[Live]** Accuracy by horizon -- modeled vs. official points, error,
  and calibration, for your team, any tracked player, and league-wide
  history. Forecasts are frozen before a gameweek plays, then checked
  against official results after -- never rewritten with hindsight.
- **[Experimental]** Shadow models -- challenger approaches (currently
  an ML-based minutes/start-probability model) are scored every refresh
  alongside the live model, but never influence your recommendation
  until one earns its way in.
- **[Live]** Feed readiness and source registry -- which official feeds
  are live, and the full source hierarchy the system is allowed to use.

## How the numbers get made

No machine learning drives what you see, no betting odds anywhere, and
every projection can be traced back to a labeled formula component.

- Scoring: additive -- appearance + attacking + clean sheet + saves +
  bonus + residual, per player per fixture.
- Inputs: official FPL per-90 stats plus official fixture difficulty
  (1-5).
- Fitting: constants fitted from three prior seasons, backtested
  against a held-out season.
- Objective: top-50k finish, under 15 minutes of your time per week.

## What alpha deliberately does not do yet

- **No European or domestic-cup fixture congestion.** Difficulty
  ratings come from Premier League fixtures only. Rotation risk from cup
  ties isn't modeled explicitly.
- **No fitted team-strength model in production.** A more granular
  attack/defense rating was built and backtested, but didn't beat the
  simpler fixture-difficulty tables, so it stays in the code, switched
  off.
- **No social or predicted-lineup scraping.** Only official
  announcements and, optionally, high-confidence lineup reports feed the
  system, never general social chatter.
- **Refresh is manual by design** today, not a background poller. You
  (or the shared hosted instance) trigger it, so the source of every
  number's timestamp is always known.

## Feedback

If a recommendation looks wrong, the player breakdown will show exactly
which component drove it. File an issue at
[github.com/onkartalekar/fpl-intelligence/issues](https://github.com/onkartalekar/fpl-intelligence/issues)
to tell us if the model's wrong or the docs are.
