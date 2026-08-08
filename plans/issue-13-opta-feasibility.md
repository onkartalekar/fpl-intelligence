# Issue #13 -- Feasibility of using Opta Analyst (theanalyst.com) data

Researched 2026-07-27. Issue body is just the URL
https://theanalyst.com/competition/premier-league/stats -- "check
feasibility of using Opta Analyst data."

## Context

The Analyst (theanalyst.com, branded "Opta Analyst") is Stats Perform's
consumer-facing editorial/stats site, built on Opta event data. The
question is whether it could feed the projection model with advanced
metrics (npxG, xG-per-shot, chance creation, carries, etc.) beyond what
the official FPL API already provides.

The repo has an established bar for exactly this kind of proposal,
set by the "Considered and declined -- npxG / xT / SCA / GCA" entry and
the Phase 6 ICT Index investigation in IMPLEMENTATION_PLAN.md, plus
MODEL.md ("Every number a projection is built from is either an official
FPL field or a constant fitted from three seasons of historical FPL
data") and SPECIFICATION.md's source hierarchy (official FPL API first,
first-party sources only, free sources only, no betting odds). A new
data source must be:

- (a) freely available,
- (b) legally usable (ToS-compatible, not a scrape of a scrape-averse
  site),
- (c) available historically at per-player, per-gameweek granularity for
  ~3 seasons of backtesting, and
- (d) proven to improve out-of-sample backtest MAE before adoption
  (the ICT investigation's bar: >0.01 MAE improvement on a held-out
  split).

## Findings

All findings verified directly on 2026-07-27 unless marked otherwise.

**What the page is.** The stats page is a JavaScript-rendered WordPress
block app; the static HTML contains no data (confirmed by fetching it).
The data behind it loads from an undocumented JSON endpoint discovered
by reading the site's bundled JS
(`wp-content/plugins/the-analyst-sdapi/build/blocks/soccer/stats-page/view.js`):

    https://dataviz.theanalyst.com/project-data/soccer/{compSeasonUUID}/player-stats.json

(plus a sibling `team-stats.json`). Fetched a sample: ~2.9 MB, 605
players, sections `attack` (overall + nonPenalty), `possession`
(chanceCreation, passing), `carries`, `defending`, `goalkeeping`.
Per-player columns include `xg`, `np_xg`, `np_shots`, `xg_per_shot`,
`shot_conv`, minutes, apps -- so npxG and shot-quality data genuinely
beyond the FPL API's fields is present.

**Granularity: season aggregates only.** Every row is a season-to-date
total per player (apps, minutes, cumulative xG, etc.). There is no
per-gameweek or per-match breakdown in the feed. The sample fetched was
stamped `lastUpdated: 2026-05-26` -- final 2025-26 season totals.

**Historical depth: not usable.** The page's embedded state exposes only
the current competition-season UUID (2025/2026); prior-season UUIDs are
not discoverable from the page, and even if they were, prior seasons
would still be season-end aggregates -- useless for the backtest
harness, which needs per-player pre-origin-gameweek values with a
no-lookahead boundary (the same requirement the ICT investigation
enforced with its 180-pre-origin-minutes rule).

**No API, and actively scrape-averse.** There is no documented API, no
bulk download, no data-licensing route on the site. theanalyst.com's
robots.txt explicitly disallows the entire site (`Disallow: /`) to a
long list of automated agents including `Scrapy`, `anthropic-ai`,
`ClaudeBot`, `GPTBot`, `CCBot` and ~25 others -- a clear statement of
intent against automated collection. Using the undocumented
`project-data` endpoint would be scraping in all but name.

**Terms of use.** Stats Perform's Terms of Use
(statsperform.com/terms-of-use/) cover "www.statsperform.com ... and any
other websites owned and operated by Stats Perform", which includes
theanalyst.com (the site's own footer links to Stats Perform's
policies). Key clause: "Material on the Websites is solely provided to
you for personal, non-commercial use. Such material may not be copied,
reproduced, republished, uploaded, posted, transmitted, or distributed
in any way." A pipeline that periodically fetches, stores, and
transforms the feed is copying/reproduction under that clause. Stats
Perform's licensed product for this data is Opta Data Feeds -- a paid
commercial API.

**No free licensed route to Opta data anymore.** FBref (Sports
Reference) had carried Opta advanced stats since 2022, and the repo's
existing npxG entry (2026-07-26) noted "FBref carries it too." That is
now stale: Stats Perform terminated the agreement and required deletion
of the data, and Sports Reference removed all Opta-sourced advanced
stats from FBref and Stathead on 2026-01-20
(sports-reference.com/blog/2026/01/fbref-stathead-data-update/;
community coverage attributes the pull to Stats Perform protecting its
new FIFA World Cup data deal). There is currently no free, licensed
redistribution of Opta data for the Premier League. Understat's npxG
(its own model, not Opta) remains the free-to-view option already
recorded in the npxG entry, with the same unofficial-scrape caveats.

**Uncertain / not fully verified:** whether prior-season compSeason
UUIDs exist behind some other endpoint; whether Stats Perform tolerates
low-volume personal use of the dataviz endpoint in practice. Neither
uncertainty changes the assessment below, because the granularity
problem is independent of both.

## Assessment against the adoption bar

| Criterion | Verdict | Why |
|---|---|---|
| (a) Freely available | Partial pass | The JSON feed is publicly reachable without auth, but only via an undocumented endpoint extracted from the site's JS bundle -- not an offered data product. |
| (b) Legally usable | Fail | Stats Perform ToS restrict material to personal, non-commercial viewing and prohibit copying/reproduction; robots.txt disallows automated agents site-wide. This is the most scrape-hostile source examined so far -- Stats Perform actively litigated its data out of FBref in Jan 2026. |
| (c) ~3 seasons of per-player per-gameweek history | Fail | Feed is season-aggregate only; only the current season's UUID is exposed. Even full access would not support the backtest harness's per-origin-gameweek, no-lookahead design. |
| (d) Proven out-of-sample MAE improvement | Not reachable | Cannot be tested without (c); no backtest is possible on season-end aggregates. Prior evidence (ICT, Phase 6) shows aggregate involvement stats largely restate what xG/xA already give the model. |

## Recommendation

**Decline.** Fails the adoption bar on two independent grounds, either
of which is sufficient:

1. **Provenance/legality** -- worse than the FBref/Understat option
   already declined on 2026-07-26. Stats Perform's ToS and robots.txt
   are explicitly hostile to automated reuse, and the company has just
   demonstrated (FBref, Jan 2026) that it enforces control over this
   exact data.
2. **Backtestability** -- season-aggregate current-season data cannot
   feed a model whose every adopted change must beat a 3-season
   per-gameweek out-of-sample backtest. Criterion (d) can never be
   evaluated.

No narrow viable path exists at the free tier: the only legitimate
route to Opta data is a paid Opta Data Feeds license, which violates
SPECIFICATION.md's free-sources-only budget. If advanced non-FPL
metrics are ever revisited, the already-recorded Understat npxG option
(own model, per-match history since 2014/15) dominates this one on
every criterion.

Also worth a one-line touch-up whenever the npxG entry is next edited:
its "FBref carries it too" claim is stale as of 2026-01-20.

## Decision confirmed and recorded (2026-08-08)

Decline confirmed. The text below has been added to
`IMPLEMENTATION_PLAN.md`'s "Considered and declined" section
(immediately after the npxG/xT/SCA/GCA entry), and that entry's stale
"FBref carries it too" note has been corrected in place. Issue #13
closes as a clean negative result; no code or model changes.

## If declined: text for IMPLEMENTATION_PLAN.md

To be added alongside the existing "Considered and declined" entry:

---

## Considered and declined -- Opta Analyst (theanalyst.com) as a data source (2026-07-27)

**Context:** GitHub issue #13 asked whether Stats Perform's Opta
Analyst site (theanalyst.com/competition/premier-league/stats) could
supply advanced Opta metrics beyond the official FPL fields. Researched
what the site actually exposes rather than guessing.

**Findings:**
- The stats page is a JS-rendered shell; its data loads from an
  undocumented JSON endpoint
  (`dataviz.theanalyst.com/project-data/soccer/{compSeasonUUID}/player-stats.json`)
  found by reading the site's JS bundle. The feed is real and rich --
  per-player npxG, xG-per-shot, shot conversion, chance creation,
  carries, defending, goalkeeping -- but every row is a
  **season-aggregate total**, with no per-gameweek or per-match
  breakdown, and only the current season's UUID is exposed. That alone
  makes it unusable for the backtest harness, which needs per-player
  pre-origin-gameweek values across ~3 seasons.
- Stats Perform's Terms of Use (covering theanalyst.com) limit material
  to "personal, non-commercial use" and prohibit copying, reproduction,
  and redistribution; theanalyst.com's robots.txt disallows the whole
  site to automated agents (Scrapy, GPTBot, ClaudeBot, CCBot, etc.).
  There is no documented API, bulk download, or free license -- the
  licensed product is the paid Opta Data Feeds API.
- The free licensed route that used to exist is gone: Stats Perform
  terminated FBref's Opta feed and had all Opta-sourced advanced stats
  removed from FBref/Stathead on 2026-01-20, so the npxG entry above's
  note that "FBref carries it too" is now stale. No free, licensed
  Opta redistribution currently exists for the Premier League.

**Decision: declined.** Fails the sourcing bar on two independent
grounds: (1) provenance -- an undocumented endpoint on a ToS-restricted,
robots-disallowed site owned by a company that actively pulled this
same data from FBref is a strictly worse dependency than the
FBref/Understat scraping already declined above; (2) backtestability --
season-aggregate current-season data can never be evaluated against the
project's out-of-sample MAE bar (SPECIFICATION.md's model-change rule),
so criterion (d) is unreachable even before the legal question. If
advanced non-FPL metrics are ever revisited, Understat npxG (recorded
above; per-match history since 2014/15, its own model rather than Opta)
dominates this option on every criterion.

---
