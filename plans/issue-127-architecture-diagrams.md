# Issue #127 -- Visual architecture documentation

## Context

Confirmed the issue's claim: zero Mermaid diagrams across `README.md`/`SPECIFICATION.md`/
`IMPLEMENTATION_PLAN.md`/`MODEL.md`. The system side (Railway server, refresh pipeline, two
GitHub Actions workflows, external dependencies) and the model pipeline (per the issue's own
"Decided" amendment) both need a diagram. Three open questions, addressed below.

## (1) Format/tooling: Mermaid vs. a dedicated C4 tool

**Mermaid.** Renders natively in GitHub's own Markdown preview -- zero extra tooling, zero build
step, works the instant the file is pushed. A dedicated C4 tool (Structurizr, PlantUML +
C4-PlantUML) gives stricter C4-level semantics but needs an external renderer just to view the
result, which this repo has no existing dependency on and no other doc requires. Every other
diagram-adjacent artifact in this repo (backtest baselines, model-coefficients tables) is
plain-text/JSON, consistent with a general house preference for zero-build-step artifacts over
tooling investment. Mermaid's `flowchart`/`graph` constructs (not its dedicated, stricter C4
extension, which GitHub doesn't render) can still express Context/Container-level boxes-and-arrows
faithfully -- labeled nodes grouped in `subgraph`s, labeled edges for each real call
(`--POST /api/refresh (X-Refresh-Token)-->`) -- without needing C4's formal notation to be useful.

## (2) Where it lives: new `ARCHITECTURE.md` vs. embedded in `README.md` vs. under `plans/`

**New top-level `ARCHITECTURE.md`.** `README.md`'s "Hosted deployment" section is already dense
prose describing the same territory; adding two large diagrams there would bury the section's
existing getting-started purpose under reference material a first-time reader doesn't need.
`plans/` is this repo's investigation/decision-record archive (one file per issue, dated,
superseded-in-place) -- not a fit for a *maintained, current* reference doc that should reflect
today's system, not a point-in-time investigation. A new top-level file matches
`MODEL.md`/`SPECIFICATION.md`/`RELEASE_NOTES.md`'s own precedent: each is its own focused,
top-level reference doc for one concern, linked from `README.md` rather than inlined into it.

## (3) How current it needs to stay: one-time snapshot vs. an explicit update convention

**A lightweight, explicit convention, not just a snapshot.** The issue's own evidence argues for
this directly: three issues this session alone (#101, #122, #125) each added a new cross-component
call, and #127 itself exists because nothing currently prompts a diagram update when that happens.
A snapshot documented "as of commit X" would already be stale by the time this ships (the very
session that motivated it added #105's `/api/reminder-teams` endpoint after the diagram's content
was first drafted). Concretely: add one checklist line to `ship-issue`'s own skill steps -- "if
this change adds/removes a cross-component call (a new endpoint, workflow, or external
dependency), update `ARCHITECTURE.md`" -- next to step 4's existing test-suite requirement, so
it's surfaced at the same point in the workflow every future cross-component change already goes
through. Not a CI-enforced check (no automated way to detect "this diff added a cross-component
call" without false positives/negatives that would erode trust in the gate) -- a documented
convention, matching how this repo already handles the doc-honesty problem for `RELEASE_NOTES.md`/
`MODEL.md` (issue #118, done by convention/review, not enforcement).

## Content plan

- `ARCHITECTURE.md`, two Mermaid diagrams:
  1. **System diagram** (Context/Container level): visitor browser, Railway server (with its
     endpoint groups), the Railway persistent volume, the two GitHub Actions workflows, and
     external dependencies (FPL API, transfer/news sources, SMTP) -- every edge labeled with the
     real mechanism (`POST /api/refresh (X-Refresh-Token)`, `GET /api/manager-view
     (X-Refresh-Token exemption)`, etc.), matching the issue's own explicit ask to avoid
     unlabeled arrows.
  2. **Model pipeline diagram** (nested Component-level, per the issue's "Decided" amendment):
     component scoring -> opponent strength -> expected minutes (with the shadow ML challenger
     branch) -> GW1-only `ep_next` blend -> minutes scenarios/uncertainty bands -> squad
     construction, plus the scaffolded-not-wired Phase 5 LLM extractor shown as a dashed/disabled
     branch, not a live step.
- `README.md`: one new line linking to `ARCHITECTURE.md`, next to the existing links to
  `MODEL.md`/`SPECIFICATION.md`.
- `ship-issue` skill: the one-line convention addition described above.

## Not in scope

- Auto-generating the diagram from code -- already declined in the issue itself.
- A stricter C4-notation tool -- declined in (1) above.

## Dependency

None remaining.
