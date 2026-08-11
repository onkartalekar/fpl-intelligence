# Issue #133 -- Pitch diagram truncation on mobile

## Context

Confirmed the issue's measurement: `.pitch-player` cards render at roughly 56-70px wide at a
375px viewport (`dashboard.css`'s `@media(max-width:760px)` block sizes them to `19.2%` of the
pitch container), truncating both name and the `"1gw / 3gw / 5gw"` projection string mid-number.

## Structural finding that resolves the issue's own open question

The issue names candidate (b) as "keep the compact card, but make the truncated info reachable on
tap/click ... confirm/extend that path works the same way on touch." Traced
`attachBreakdownHandlers` (`dashboard.js:43`): it already attaches a plain `click` (and
`keydown`-Enter/Space) listener to every `[data-player-id]` node -- and `.pitch-player` cards
already carry `data-player-id` (confirmed via the existing CSS selector
`.pitch-player[data-player-id]`, `dashboard.css:1318`). A `click` event fires from a tap on every
mobile browser (no hover-only dependency) -- so **tapping a truncated pitch card today already
opens the full per-player breakdown table**, scrolled into view, showing the exact 1/3/5-gameweek
numbers the card itself truncates. Candidate (b) isn't a direction to build -- it's already built.
Verified live with an actual tap simulation (not just checking the JS), see below.

This changes what's actually missing: not "a way to reach the full numbers" (exists), but (a) the
truncated text cuts off mid-number rather than ending cleanly, which reads as broken/lossy even
though the real data is one tap away, and (b) nothing on the card hints that tapping reveals more,
so a first-time mobile visitor has no reason to try.

## Recommendation

Two small, targeted changes, not a layout replacement (candidate (c), the full list-layout
rewrite, is disproportionate now that (b)'s "way to reach the data" turns out to already exist):

1. **Show one clean number instead of three truncated ones, below the mobile breakpoint.** The
   1-gameweek projection alone (`xp_1`), right-aligned, replaces the `"1gw / 3gw / 5gw"` string
   that currently truncates mid-digit. This is genuinely readable at the card's real width, and
   -- unlike today's arbitrary mid-string cut -- it's a deliberate, complete piece of information,
   with the 3/5-gameweek figures one tap away in the already-existing breakdown panel.
2. **A lightweight visual affordance that the card is tappable**, mobile-only: reuse the existing
   `:hover`/`.selected` visual language (a subtle border/background shift) as a static, always-on
   mobile treatment instead of a hover-only one, so a compact card reads as an interactive control
   rather than a truncated dead end.

## Not in scope

- Candidate (c) (full list-layout replacement) -- declined per the finding above; the "reach the
  data" problem this would solve is already solved by existing tap-through.
- The two lower-priority, unrelated findings from #116's audit (scroll affordance on the subnav
  tab bar and Model Performance's table) -- tracked on #116, not this issue.

## Dependency

None remaining.
