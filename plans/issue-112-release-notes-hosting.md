# Where to publish the alpha release notes (issue #112)

## Context

Issue #112 asks where a real, code-verified alpha feature inventory /
release notes should live in the repo. The issue's own body left the
`RELEASE_NOTES.md`-vs-`README.md`-section choice open; the user has since
narrowed the question further, proposing GitHub Pages specifically and
asking what else is worth weighing against it.

There is no release-notes or changelog file, GitHub Pages site, or
`docs/` folder anywhere in this repo today. The only top-level docs are
`README.md`, `MODEL.md`, `SPECIFICATION.md`, and `IMPLEMENTATION_PLAN.md`
-- all plain Markdown, rendered by GitHub's own file viewer, with no
build step. Confirmed directly:

- `gh api repos/onkartalekar/fpl-intelligence/pages` returns `404 Not
  Found` -- Pages has never been enabled on this repo.
- `gh repo view` confirms the repo is **public**, so GitHub Pages is free
  either way (no separate paid-plan requirement).
- `.github/workflows/` holds exactly one workflow (`deadline-reminder.yml`,
  issue #55), a scheduled job unrelated to any build/deploy pipeline.
- The app itself is intentionally dependency-free and low-infra: `##
  Dependencies` in `README.md` states the dashboard, refresh pipeline,
  and tests are stdlib-only Python; `plans/issue-27-cloud-hosting.md`
  explicitly ruled out heavier observability tooling (Sentry, Datadog) as
  "disproportionate at this scale" for a low-traffic personal tool, in
  favor of what Railway already gives for free.
- The dashboard is already hosted (issue #27, Railway) at a
  `*.up.railway.app` subdomain -- so a GitHub Pages site would be a
  *second*, unrelated public URL for this project, not the one alpha
  testers already have open when using the tool.

## One structural fact that shapes every candidate

Release notes are static, human-authored prose -- not app state, not
something `refresh_dashboard.py` regenerates. That means whichever
option is picked, the *authoring* step is the same (write Markdown/HTML
by hand, as this issue's own `## Request` says: "written and landed...
by hand, not tooling"). The candidates below differ only in **where that
already-written content is published and how discoverable it is**, not
in how much work it takes to write.

## Candidate options

**A. `RELEASE_NOTES.md` at the repo root, rendered by GitHub's own file
viewer.**
Zero new infrastructure -- exactly how `MODEL.md`/`SPECIFICATION.md`/
`IMPLEMENTATION_PLAN.md` already work today. Linkable from `README.md`'s
`## Current status` section. Diffable, version-controlled alongside the
code it describes, and edited in the same PR as the feature it documents
if wanted. Downside: plain GitHub Markdown rendering only -- no custom
layout, no Live/Experimental/Opt-in status chips or the kind of visual
structure the conversational draft used, just headings and text.

**B. GitHub Releases (the repo's native "Releases" feature), via `gh
release create --notes-file ...` against a tag.**
Semantically the closest fit to the literal phrase "release notes" --
GitHub gives this a dedicated tab, an automatic reverse-chronological
timeline, and users can "Watch" the repo for release notifications with
zero setup. No file to place or path to remember; no Pages/Actions
config. Weakest fit for this specific ask, though: issue #112's request
is a single **living feature inventory** ("what does the whole tool do
right now"), not a per-version changelog of what changed since last time
-- Releases is built for the latter shape (a list of deltas tied to
tags), and this project doesn't currently tag versions at all, so
adopting Releases would mean introducing versioning as a prerequisite
just to have something to attach notes to.

**C. GitHub Pages (`docs/` folder on `main`, or a `gh-pages` branch, with
Pages enabled in repo settings).**
The user's proposal. Gives a real standalone URL
(`onkartalekar.github.io/fpl-intelligence/`) and, since Pages can serve
raw HTML, could host something closer to the styled draft artifact
(status chips, sectioned layout) rather than plain Markdown. But it's a
second public surface to stand up and keep working:
- A `docs/`-on-`main` Pages site still needs Pages turned on once via
  repo Settings (a manual, one-time step outside `gh`/git, not
  automatable from this worktree) -- or a `gh-pages` branch, which adds
  an entirely separate branch to keep in sync, either by hand or via a
  new GitHub Actions build/deploy workflow.
- It creates a second URL a tester could land on that looks like "the
  app" but isn't -- the actual dashboard lives on Railway. For a small
  alpha group this is a minor confusion risk, not a blocker, but it's a
  cost the other two options don't carry.
- It's the only candidate that doesn't already have a working precedent
  in this repo. A/B use mechanisms (plain Markdown files, `gh release`)
  the project already relies on elsewhere; Pages would be new
  infrastructure for a repo whose stated posture (`README.md`'s
  `Dependencies` section, `plans/issue-27-cloud-hosting.md`'s explicit
  "not recommended at this scale" calls) has consistently been to avoid
  adding infrastructure the project's actual scale doesn't need yet.

## Recommendation

**Build A now: `RELEASE_NOTES.md` at the repo root**, linked from
`README.md`. It matches how every other doc in this repo already ships
(no new mechanism to learn or maintain), requires no manual GitHub
Settings step, and is the only option with zero setup cost before a
single word of the actual content gets written -- which is where this
issue's real work is. Losing the visual polish of the artifact draft
(status chips, colored sections) is an acceptable trade for a first
alpha version; Markdown headings and a consistent Live/Experimental/
Opt-in text convention (e.g. `**[Live]**`, `**[Experimental]**` prefixes)
carry the same information without new infrastructure.

**Decline C (GitHub Pages) for now**, not because it's a bad idea in the
abstract, but because it solves a problem this project doesn't have yet:
a large enough or public-enough audience that plain-Markdown-in-repo
stops being discoverable. Worth revisiting if the alpha group grows past
people who are already comfortable opening the GitHub repo, or if the
content genuinely needs interactivity/styling that Markdown can't carry
(the chips-and-sections layout the conversational draft used). Until
then it's the same category of premature infrastructure `plans/issue-27-
cloud-hosting.md` already declined for observability tooling.

**Decline B (GitHub Releases)** for this specific ask, since the
requested artifact is a living inventory, not a versioned changelog, and
this repo doesn't tag releases today. Worth reconsidering later,
independently of this issue, if the project ever adopts version tags for
other reasons -- at that point Releases could carry lightweight
per-version deltas *in addition to* `RELEASE_NOTES.md`'s always-current
snapshot, not instead of it.

## Drop-in text for `IMPLEMENTATION_PLAN.md`'s "Considered and declined" section

```markdown
## Considered and declined -- GitHub Pages / GitHub Releases for release notes (2026-08-10)

**Context:** issue #112 asked where to publish an alpha feature
inventory / release notes doc. The user specifically proposed GitHub
Pages; GitHub Releases was evaluated alongside it as the other
GitHub-native option. See `plans/issue-112-release-notes-hosting.md` for
full reasoning.

**Findings:**
- GitHub Pages was never enabled on this repo (`gh api
  repos/.../pages` returns 404) and would require either a one-time
  manual Settings step (`docs/`-on-`main`) or a new `gh-pages` branch
  plus build workflow -- new infrastructure this repo's stated posture
  (stdlib-only app, `plans/issue-27-cloud-hosting.md`'s explicit
  "not recommended at this scale" calls on observability tooling) has
  consistently avoided until the project's actual scale needs it. It
  would also stand up a second public URL distinct from the Railway-
  hosted dashboard (issue #27), a minor discoverability cost for a small
  alpha group.
- GitHub Releases is a strong semantic fit for the phrase "release
  notes" but a poor fit for this specific ask: issue #112 wants a living
  feature inventory (what the tool does right now), not a per-version
  changelog of deltas -- and this project doesn't tag versions today, so
  adopting Releases would mean introducing versioning as a prerequisite.

**Decision:** publish `RELEASE_NOTES.md` at the repo root instead (see
"## Recommendation" in `plans/issue-112-release-notes-hosting.md`) --
zero new infrastructure, matches how `MODEL.md`/`SPECIFICATION.md`/
`IMPLEMENTATION_PLAN.md` already ship. Revisit GitHub Pages if the alpha
audience outgrows "comfortable opening the GitHub repo," or if the
content needs interactivity/styling plain Markdown can't carry. Revisit
GitHub Releases independently if the project ever adopts version tags
for other reasons.
```
