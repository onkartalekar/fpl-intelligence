# Issue #83 -- HTML/multimedia reminder email template (nice-to-have)

Researched 2026-08-09. Issue body: `send_email()` in
`scripts/send_deadline_reminder.py` (#55) currently sends plain text only.
The issue proposes a richer HTML layout -- possibly a pitch/formation
graphic reusing dashboard rendering logic, color-coded action badges, and
the three risk profiles as visually distinct cards -- via stdlib
`EmailMessage.add_alternative(html, subtype="html")`. Explicitly the
lowest-priority reminder follow-up, and explicitly open-ended: "the exact
layout is a product call."

## Context

Issue #55 shipped a working plain-text reminder. #83 is a pure
presentation upgrade over it -- nothing here is blocking, and the issue
itself says to sequence it after the functional gaps (profile schema,
self-serve opt-in, all-three-profiles, confirmed overrides) are closed.
This plan exists to settle the layout question in writing, per the
issue's own framing, before anyone spends implementation time on it.

## What the current code does

`compose_email()` (`scripts/send_deadline_reminder.py:256`) builds a
plain list of strings joined with `\n`, covering two states:

- `_compose_gw1_section()` (line 164): pre-GW2 opening-squad recommendation
  -- captain/vice-captain, `Starting XI (<formation>):` with one line per
  player, bench, top-5 captaincy options.
- `_compose_active_section()` (line 200): in-season transfer decision --
  reads a single profile out of `weekly["profiles"]` (`default_profile`,
  currently just `"balanced"` until the companion all-three-profiles issue
  lands), renders `action` (`roll` / `single_transfer` / `double_transfer`
  / `hold`, confirmed in `transfer_decisions.py`), transfers as
  `OUT: X -> IN: Y` lines, captain, projected points, net gain, and a
  cost/bank/free-transfers summary line.

`send_email()` (line 288) calls `message.set_content(body)` only -- a
single-part `text/plain` message, no alternative branch at all today.

**Mechanism check.** Ran a stdlib-only sanity script
(`email.message.EmailMessage`, no dependencies) confirming the issue's
proposed mechanism works exactly as described:

```python
msg.set_content("Plain text fallback")
msg.add_alternative("<html>...</html>", subtype="html")
# -> Content-Type: multipart/alternative, is_multipart: True
# -> parts: multipart/alternative, text/plain, text/html
# -> get_body(preferencelist=('html',)) resolves to the text/html part
# -> get_body(preferencelist=('plain',)) resolves to the text/plain part
```

`set_content()` must be called *before* `add_alternative()` -- that call
order is what makes `text/plain` the first (least-preferred, universal
fallback) part and `text/html` the second (most-preferred) part per
RFC 2046's ordering convention for `multipart/alternative`. This is a
correct, zero-new-dependency mechanism; nothing about the *sending* side
of the issue needs further validation. The open question is entirely
about what HTML to generate.

## Dashboard pitch rendering -- is it reusable?

`#weekly-pitch` / `.formation-pitch` (`dashboard.css:1056-1130`,
`dashboard.js` line 100's `weeklyPitch()`) renders the starting XI as:

- Outer `.formation-pitch`: `position: relative`, `display: flex;
  flex-direction: column; justify-content: space-around`, with a turf
  background built from `repeating-linear-gradient` + a diagonal
  `linear-gradient` stripe overlay, and `:before`/`:after` pseudo-elements
  drawing the halfway line and center circle.
- Four `.pitch-row` divs (one per `FWD/MID/DEF/GKP`), each
  `display: flex; justify-content: space-evenly`.
- `.pitch-player` cards: fixed `width: min(130px,19%)`, `border-radius`,
  `box-shadow`, captain state via a `border-color`/`box-shadow` glow.
- Every color in the above is a CSS custom property (`var(--pitch-line)`,
  `var(--pitch-turf-a)`, etc., defined once in `:root` and re-themed for
  light mode at line 86-101) -- there is no literal color anywhere in the
  pitch rules themselves.

**What's reusable:** the *grouping logic* -- four rows by position,
players left-to-right within a row, captain/vice-captain flagged inline
-- is a clean, already-correct piece of domain logic (`weeklyPitch()`'s
`['FWD','MID','DEF','GKP'].map(...)` reduction) that translates directly
into any markup shape, including a table.

**What's not reusable, and why it matters:** the CSS *mechanism* is
built entirely out of things email clients don't reliably support:

- **CSS custom properties (`var(--x)`)** -- not supported by Outlook
  desktop (Win32, still Word-engine-rendered) at all, and inconsistently
  honored elsewhere. Every `--pitch-*` value would need to become a
  literal hex color inlined per element.
- **Flexbox** -- Outlook desktop ignores `display: flex` entirely
  (falls back to block stacking, destroying the row layout); Gmail and
  Apple Mail are fine with it, but that's not the client that matters
  for this decision.
- **`:before`/`:after` pseudo-elements** -- effectively unsupported in
  email; the halfway line and center circle would just vanish, which is
  harmless (decorative only) but confirms the *decorative* layer of the
  design doesn't port at all, only the structural grouping does.
- **`box-shadow`, `border-radius` on gradient backgrounds, multi-layer
  `background`** -- partially supported (Apple Mail/Gmail fine, Outlook
  desktop mostly strips shadows and gradients, keeps flat backgrounds
  and simple borders).

Net: porting the dashboard's pitch CSS is not "reuse," it's a rewrite
that keeps only the row-by-position grouping idea and none of the actual
style rules. That's worth stating plainly rather than assuming a config
diff of the existing CSS would get most of the way there -- it wouldn't.

## Is inline SVG realistically safe in email?

Checked against the well-documented state of email-client SVG support,
which is genuinely bimodal, not universally bad:

- **Renders fine:** Apple Mail (WebKit-based, both macOS and iOS), Gmail
  web and mobile app, Yahoo Mail. These cover most personal/individual
  inboxes -- and this script's recipients are FPL managers using it for
  personal reminders (per `FPL_INTEL_REMINDER_TEAMS`), not a corporate
  distribution list, which shifts the client mix somewhat toward this
  group versus a typical B2B email audience.
- **Broken or stripped:** Outlook desktop (Win32, Word-rendering engine)
  is the standing, well-known exception -- it does not render inline
  `<svg>` at all. Outlook.com webmail and Outlook mobile are better but
  inconsistent across versions.
- **A specific implementation detail changes the failure mode:** an
  inline `<svg>...</svg>` tag that a hostile parser doesn't recognize is
  typically just dropped/ignored -- clean degradation, nothing shown
  where the diagram would have been. A `<img src="data:image/svg+xml;base64,...">`
  data-URI image, by contrast, risks showing a broken-image glyph or alt
  text in clients that block data-URI images (some historically have),
  which reads as more broken than "the diagram just isn't there." If SVG
  is used at all, it should be a raw inline `<svg>` tag, not a data-URI
  `<img>`, specifically because the failure mode is cleaner.

So: not "unsafe" in the sense of breaking the email, but a real,
non-trivial fraction of opens (anyone on Outlook desktop) get a message
with a silent gap where the graphic should be. That's a legitimate
design tradeoff to accept explicitly, not something to discover after
shipping.

## Candidate operationalizations

### (a) Table-based HTML email -- no pitch diagram, badges + profile cards

Old-school `<table>`/`<tr>`/`<td>` layout, every style as an inline
`style=""` attribute (not a `<style>` block, since some webmail clients
strip `<head>` entirely), literal hex colors (reusing the dashboard's
existing badge palette as fixed values -- `--badge-ready-bg #164b3a` /
`--badge-ready-fg #94efcb` for "roll", `--chip-hard-bg #573040` /
`--chip-hard-fg #ffc1cb` for a points-hit action, `--badge-info-bg
#203b59` / `--badge-info-fg #b9dcff` for informational rows -- picking
the dark-theme values since they read fine as a colored badge either
way, not because email respects a theme). No flexbox, no grid, no CSS
custom properties, no gradients.

Concrete mockup, using the real fields `_compose_active_section()`
already has on hand (`recommendation.action`, `.transfers[].out/in`,
`.captain`, `.point_cost`, `.bank_after`, `.free_transfers_next_event`):

```html
<table role="presentation" width="100%" cellpadding="0" cellspacing="0"
       style="font-family:Arial,Helvetica,sans-serif;max-width:560px">
  <tr><td style="padding:12px 16px;background:#0d1b2a">
    <span style="color:#94efcb;font-size:12px;font-weight:bold">
      GAMEWEEK 3 &middot; BALANCED PROFILE
    </span>
  </td></tr>
  <tr><td style="padding:16px">
    <table role="presentation" cellpadding="0" cellspacing="0">
      <tr><td style="background:#164b3a;color:#94efcb;font-weight:bold;
                     font-size:12px;padding:4px 10px;border-radius:4px">
        ROLL
      </td></tr>
    </table>
    <p style="margin:10px 0 0;font-size:14px;color:#1b1b1b">
      No transfer beats holding this gameweek given projected returns.
    </p>
  </td></tr>
  <tr><td style="padding:0 16px 16px">
    <table role="presentation" width="100%" cellpadding="8" cellspacing="0"
           style="border:1px solid #dde3ee;border-radius:6px">
      <tr>
        <td style="font-size:13px;color:#555">Captain</td>
        <td style="font-size:13px;font-weight:bold" align="right">Haaland</td>
      </tr>
      <tr>
        <td style="font-size:13px;color:#555">Projected pts (incl. captain)</td>
        <td style="font-size:13px;font-weight:bold" align="right">11.4</td>
      </tr>
      <tr>
        <td style="font-size:13px;color:#555">Bank after</td>
        <td style="font-size:13px" align="right">&pound;2.3m</td>
      </tr>
      <tr>
        <td style="font-size:13px;color:#555">Free transfers next GW</td>
        <td style="font-size:13px" align="right">1</td>
      </tr>
    </table>
  </td></tr>
</table>
```

A starting-XI section, if desired, becomes four `<tr>`s (one per
position) each holding one `<td>` per player -- the same grouping logic
as `weeklyPitch()`, just emitted as table cells instead of flex children:

```html
<tr>
  <td align="center" style="width:25%;padding:6px;background:#15583f;
                             color:#fff;border-radius:4px;font-size:12px">
    <strong>Haaland</strong> (C)<br><span style="opacity:.8">MCI</span>
  </td>
  <!-- ...one <td> per FWD, next <tr> for MID, etc. -->
</tr>
```

This does not visually look like a pitch (no green field, no
positioning by width/depth) -- it looks like a clean, color-blocked
roster grid. That's an honest tradeoff of this candidate, not a
half-implementation of (b).

**Reliability:** universal. This is the actual "email-safe HTML" industry
convention for exactly the reason it looks dated -- tables and inline
styles are the only subset every major client (including Outlook
desktop) has rendered consistently for two decades.

### (b) Inline-SVG pitch diagram -- degraded-but-not-broken in Outlook

Same table shell as (a) for the badges/cards/summary (still needed
regardless -- SVG can only replace the XI section, not the whole email),
plus a hand-built inline `<svg viewBox="0 0 400 500">` for the pitch:
`<rect>` for the turf, `<line>` for the halfway line, `<circle>` for the
center circle, and one `<g>`/`<rect>`+`<text>` group per player
positioned by literal x/y coordinates computed the same way
`weeklyPitch()` groups by position (four y-bands, players spread evenly
along x within each band -- the same layout math, just emitting SVG
coordinates instead of flexbox rows).

Renders well in Apple Mail and Gmail (a real majority of this script's
personal-use recipient base, per the client-support note above); is
silently absent in Outlook desktop specifically because the raw `<svg>`
tag degrades cleanly there (per the failure-mode note above) rather than
showing a broken-image icon. The table-based badge/summary content around
it is unaffected either way, so no Outlook user loses the actual
decision (action, captain, transfers) -- only the diagram.

**Reliability:** good-not-universal, with a known and named gap
(Outlook desktop), and a specific reason that gap degrades cleanly
instead of ugly.

### (c) Nicer plain-text formatting only -- no HTML

Better spacing, `-`-rule dividers between sections, consistent column
alignment for the summary line, maybe a `[ROLL]` / `[TRANSFER]` /
`[HOLD]` bracketed tag in place of the current lowercase
`action.replace("_", " ")`. Cheap, zero risk, addresses none of the
issue's actual visual ask (badges, cards, diagram) -- it's a fallback
polish item, not a competing design direction.

**Not actually a standalone candidate:** whichever of (a)/(b) is chosen,
the `text/plain` part of the `multipart/alternative` message is still
sent to every recipient as the fallback (that's the whole point of
`add_alternative`) and deserves this same polish regardless. (c) is
better framed as "do this to the plain-text part no matter which HTML
candidate ships" than as an alternative to (a)/(b).

## Recommendation

**Design: (a) table-based HTML as the primary build, with (b)'s SVG
pitch diagram as an explicitly optional, separately-scoped enhancement
layered on afterward -- not bundled into the same pass.**

Reasoning:

1. (a) alone already delivers the two clearest wins the issue asks for
   (color-coded action badges, per-profile cards) with zero rendering
   risk across clients, using the dashboard's own existing badge color
   palette (as literal hex values, not custom properties) so the visual
   language stays consistent with the dashboard without porting any of
   its actual (email-incompatible) CSS mechanism.
2. The pitch/formation graphic is the one piece of the issue that is
   explicitly hedged in the issue body itself ("could reuse... or a
   lightweight... image") and is also the one piece with a real,
   named cross-client gap (Outlook desktop). Shipping (a) first means
   the email is fully functional and consistent for 100% of recipients
   before any client-dependent piece is added.
3. If (b) is added afterward, it should degrade the way described above
   (raw inline `<svg>`, not a data-URI `<img>`) specifically so Outlook
   users get a slightly plainer email, not a visibly broken one.
4. (c)'s plain-text polish should happen as part of whichever pass
   touches `compose_email()`, since the `text/plain` part is sent
   unconditionally either way -- it's not a sequencing decision, just a
   line item within the (a) build.

**Sequencing: defer.** This remains the lowest-priority reminder
follow-up, and the issue's own dependency note says to sequence it after
the functional gaps (profile schema, self-serve opt-in, all-three-profiles,
confirmed overrides) land -- several of which change what
`compose_email()` has available to render (e.g. the companion "all three
profiles" issue directly changes candidate (a)'s per-profile-card section
from one card to three). Building the HTML template now risks reworking
it once those land. This plan documents the design so implementation is
a mechanical `/ship-issue` pass whenever it's prioritized -- not
resolving here whether that's soon.

**Scope for the eventual `/ship-issue` pass**, once picked up:

- Refactor `compose_email()` to return `(subject, text_body, html_body)`
  or similar; `send_email()` calls `set_content(text_body)` then
  `add_alternative(html_body, subtype="html")`, per the confirmed
  mechanism above.
- Build (a)'s table shell for both `_compose_gw1_section()`'s and
  `_compose_active_section()`'s states, reusing the same literal badge
  colors for `roll`/`single_transfer`/`double_transfer`/`hold` from
  `transfer_decisions.py`'s action vocabulary.
- Extend `--dry-run` to print (or optionally write to a file) the HTML
  part too, so a human can review a rendered preview without sending
  real mail -- today's dry-run only prints the plain-text body.
- Add a test asserting the sent message is `multipart/alternative` with
  both `text/plain` and `text/html` parts present (mirroring the sanity
  check above), not just that `send_content`/`add_alternative` were
  called.
- SVG pitch diagram (b): separate, explicitly optional follow-up scoped
  after (a) ships and is confirmed working, not bundled in.
