# Page design, color scheme, and light/dark theme toggle (issue #48)

## Context

Issue #48 bundles three requests of very different shape: (1) a general page-design refresh, (2) a color-scheme revisit, and (3) a light/dark theme toggle. The entire UI is one template string in [dashboard.py](../src/fpl_intel/dashboard.py) with a single hardcoded dark theme: `html{color-scheme:dark}` plus one `:root` block of 12 variables (`--bg --nav --panel --panel2 --line --text --muted --accent --blue --warn --danger --low`). There is no light variant and no toggle mechanism anywhere.

## Structural findings (before evaluating candidates)

### 1. The color audit: 42 distinct values live outside the theme system

Counted directly from the template: **44 distinct hex literals + 10 distinct rgba literals**, versus only 12 `:root` variables. The literals fall into four buckets that need different treatment:

| Bucket | Examples | Light-mode behavior (verified live, below) |
|---|---|---|
| **Surface/panel variants** | `#0d1829`, `#0b1728`, `#132239→#0f1b30` gradient, `rgba(8,16,31,.97)` sticky bars, `#112b2a` hero | Stay dark while `--text` goes dark → **unreadable text** |
| **Self-contained fg/bg pairs** | severity badges (`#4c3f1c`/`#ffe39c` etc.), tags (`#164b3a`/`#8ff0c7` etc.), colored difficulty chips | Readable but stranded as dark-theme islands |
| **Pairs missing one half** | neutral difficulty chip `#263a57` (no explicit `color`, inherits `--text`) | **Unreadable** in light mode (dark-on-dark) |
| **Theme-invariant by design** | the formation pitch (`#15583f`/`#185f45` green stripes, white lines, `rgba(9,21,34,.92)` player cards) | Should *stay* green/dark in both themes — a real pitch isn't theme-dependent |

### 2. Live mockup: variables-only toggle demonstrably breaks the UI

Per the plan-issue mockup rule, overrode just the 12 `:root` variables with a light palette in the running dashboard (no literal fixes) and screenshotted each view:

- **My Team (mostly empty panels): looks deceptively fine.** This is the trap — a quick variables-only implementation would demo well on sparse views and ship broken.
- **Preseason overview: broken.** The attention panel keeps its hardcoded dark gradient while headings inherit the now-dark `--text` — "Needs attention" and every item title become effectively invisible. Severity badges stay dark-theme styled.
- **Fixtures: partially broken.** The neutral difficulty "3" chip renders dark text on `#263a57` — unreadable. Colored chips (1-2 green, 4-5 red) survive since they carry their own text colors.

Conclusion: the toggle is not a variable swap; it requires tokenizing all ~42 literals into semantic variables first, with the pitch explicitly marked theme-invariant.

### 3. Contrast audit: the current dark palette is objectively sound

WCAG contrast ratios computed for the 15 most-used fg/bg pairs: **every pair passes AA; 11 of 15 pass AAA.** Worst is `--low` on panel at 4.33:1, which is used only for de-emphasized large-ish values and is the *point* of that token. Details: text/bg 17.6, muted/bg 8.3, accent/panel 10.3, blue/panel 9.3, warn/panel 12.3, danger/panel 7.6, all badge/tag/chip pairs 6.8-8.4.

This is a decisive negative result for candidate (b) below: there is no objective defect motivating a palette overhaul.

## Candidate operationalizations

### (a) Light/dark theme toggle, done as a tokenization pass — BUILD

Scope, in dependency order:

1. **Tokenize.** Promote every literal into a semantic variable in `:root` (e.g. `--surface-inset`, `--badge-warn-bg`/`--badge-warn-fg`, `--chip-neutral-bg`/`--chip-neutral-fg`, `--sticky-bg`, `--hero-gradient-from/to`, `--glow-accent`). Pitch colors get variables too but are defined once, outside the themed blocks, documented as theme-invariant. Fix the neutral-difficulty-chip missing `color` while there (it's a latent bug even within dark mode discipline).
2. **Add the light palette** as a `[data-theme="light"]` override block on `:root`, and flip `color-scheme` accordingly. Light values chosen to preserve the same *semantic* relationships (accent stays green, warn stays amber, difficulty ramps keep direction) and must pass the same AA bar — rerun the contrast script from finding 3 against the light pairs before calling it done.
3. **Toggle + persistence.** A small control in the sidebar/topbar; `localStorage` for persistence; default follows `prefers-color-scheme` when unset; an inline script at the top of `<body>` applies the stored theme before first paint (no flash-of-wrong-theme — the page is one static HTML file, so this is a two-line script).
4. **Tests.** Extend `tests/test_dashboard.py`: assert the toggle control renders, the `[data-theme="light"]` block exists, and no raw hex literals remain in themed rules (a regex guard keeps future edits honest — with an explicit allowlist for the pitch's invariant block).
5. **Live verification in both themes** across the literal-heavy views (overview, fixtures, transfers, decision center with a configured profile), plus the `prefers-color-scheme` default via the browser tooling's color-scheme emulation.

This is one well-scoped `ship-issue` pass. It is bigger than a typical UI tweak (every color rule in the template gets touched) but it is mechanical, testable, and verifiable.

### (b) Color-scheme overhaul of the dark palette — DECLINE

Finding 3 removes the objective case: the current palette passes AA everywhere and AAA almost everywhere, and the palette is coherent (one accent family, consistent badge ramps). A redesign would be aesthetic churn with regression risk across 15 carefully-balanced pairs and zero measurable win. Note that (a)'s tokenization is exactly the enabling work a future palette change would need — after (a), swapping palettes is editing one block, so declining (b) now costs nothing later.

### (c) General page redesign — DEFER pending concrete direction

"Redesign" as filed has no target: no reference design, no named problem with the current layout, no prioritized pages. The recent, working pattern in this repo has been incremental per-issue UI improvements driven by specific friction (#22 profile-tab split, #23 default tab, #39 gameweek prev/next) — each shipped with a clear before/after. A wholesale redesign without a motivating problem statement inverts that: high effort, unverifiable outcome. Defer until there's concrete direction (reference screenshots, a specific complaint, or a prioritized view to rework), then scope it as its own issue per view.

## Recommendation

- **Build (a)** — the toggle with full tokenization — as the implementation for issue #48.
- **Decline (b)** — contrast audit shows nothing to fix; tokenization keeps the door open for free.
- **Defer (c)** — needs direction from the user before it's actionable; not blocked on anything technical.

## Drop-in text for IMPLEMENTATION_PLAN.md (if declines are confirmed)

## Considered and declined — dark-palette overhaul and general redesign (issue #48, 2026-08-08)

While adding the light/dark theme toggle (issue #48), two adjacent candidates were considered and declined:

- **Dark-palette overhaul.** A WCAG contrast audit of the 15 most-used foreground/background pairs found every pair passes AA and 11 of 15 pass AAA (worst: the deliberately de-emphasized `--low` token at 4.33:1). With no measurable defect, a palette redesign would be aesthetic churn with regression risk. The theme-toggle tokenization means any future palette change is a one-block edit, so nothing is foreclosed.
- **General page redesign.** Declined as unscoped: no reference design, named problem, or prioritized views were identified. Incremental, friction-driven UI issues (the pattern of #22/#23/#39) remain the preferred path; a broad redesign can be revisited if concrete direction emerges.
