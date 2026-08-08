# Extract CSS/JS out of the Python-embedded HTML template (issue #51)

## Context

Filed during the light/dark theme work (#48/#50): `dashboard.py`'s `_TEMPLATE` string contains ~28KB of CSS and ~70KB of JS, both hand-minified onto essentially single lines, which caused real friction during that PR (no syntax highlighting/linting, unreviewable diffs, a fragile regex-based test instead of a real CSS-aware one). Issue #51's own proposed direction was to have `server.py` serve CSS/JS as separate files alongside `dashboard.html`. That proposal turned out to rest on an inaccurate assumption about the server, and there's a better-shaped candidate it didn't consider — both found below.

## Structural findings before evaluating candidates

1. **The issue's core claim is wrong**: "`server.py` already serves files from the generation directory" is not true. `do_GET`'s path allowlist (`server.py`) is hardcoded to exactly `/`, `/dashboard.html`, `/api/status`, `/favicon.ico` -- everything else 404s. `resolve_artifact` is an internal helper those handlers call, not a general static-file route. Serving new asset paths would require new `do_GET` branches with correct `Content-Type` headers (the handler builds responses manually, no automatic MIME detection), and ideally resolving CSS/JS from the *same* generation directory as the `dashboard.html` being served, to avoid a stale-CSS-with-fresh-HTML mismatch across refreshes -- the app already has this generation-consistency model for JSON artifacts, so a naive implementation could easily skip it and introduce a new class of bug that doesn't exist today.
2. **The `__DASHBOARD_DATA__`/`__REFRESH_TOKEN__` substitutions are less entangled with the JS than the issue assumed.** `__DASHBOARD_DATA__` is substituted inside `render_dashboard()` at *generation* time, into its own `<script id="dashboard-data" type="application/json">` tag -- separate from the ~70KB application-logic `<script>` block that follows it. `__REFRESH_TOKEN__` stays a literal placeholder in the static file and is only substituted by `server.py` at *serve* time (`do_GET`), for the standalone-file case to correctly disable refresh. Neither depends on the logic script being inline; the issue's proposed "bootstrap script that sets `window.__DASHBOARD_DATA__`" workaround isn't needed.
3. **The generated file's portability is a documented, advertised property, not an implementation detail.** The README explicitly promotes "the standalone file remains available at `dashboard.html`" as a supported usage: copy or open that one file directly (styling and all interactive logic work; only the refresh button is disabled). Any candidate that makes `dashboard.html` depend on sibling files trades this away.

## Candidate operationalizations

### (a) Serve CSS/JS as separate files over HTTP (the issue's own proposal) -- considered, not recommended

Mocked up live: extracted the current CSS into a real `.css` file and the JS logic into a real `.js` file, rewired a copy of the generated HTML to reference them via `<link rel="stylesheet">`/`<script src>`, and served both through a plain HTTP server. Both loaded and ran correctly -- zero console errors, styling identical, interactive functions (`showView`, the gameweek nav, `setupThemeToggle`) all callable and working. So the mechanism itself is sound. But it costs real things finding (1) already identified: new `do_GET` routes and MIME handling to build and secure, a new generation-consistency risk that doesn't exist today, and it breaks the standalone-file portability property from finding (3) -- copying just `dashboard.html` elsewhere would silently lose styling and interactivity unless the sibling files travel with it. It also could not be verified for the `file://` (no-server, no-HTTP) case this session -- the Browser tool's file:// access was unavailable here. That specific gap is low-risk (regular `<link>`/`<script src>` loading of same-directory sibling files under `file://` is long-standing, ordinary browser behavior, unlike `fetch`/XHR/ES-module `import`, which do face `file://` CORS restrictions) but is asserted from general web-platform knowledge, not from a live check in this session, and should get one real manual check before anyone ships this path.

### (b) Keep CSS/JS as real source files, inline them at *generation* time -- recommended

Instead of changing what gets served, change where the source lives. Move the CSS and JS logic out of the `_TEMPLATE` Python string into real sibling files (e.g. `src/fpl_intel/dashboard.css`, `src/fpl_intel/dashboard.js`), and have `render_dashboard()` read and inline them into the `<style>`/`<script>` tags at generation time -- the same mechanism it already uses for `__DASHBOARD_DATA__`, just reading from a file instead of a Python string literal. The produced `dashboard.html` is byte-for-byte the same shape as today: one self-contained file.

This resolves every pain point from #51 with none of (a)'s costs:
- Real syntax highlighting, autocomplete, and `stylelint`/a JS linter, because editing happens in real `.css`/`.js` files.
- Real line-based `git diff` review on the actual source of a change.
- A CSS-aware test could parse the real file directly instead of regexing a Python string blob.
- Zero changes to `server.py` -- no new routes, no new MIME handling, no new generation-consistency risk, no new attack surface to secure.
- Zero risk to the standalone-file portability property -- `dashboard.html` stays exactly as self-contained as it is today, verified unaffected because nothing about the output changes, only where the input text comes from.

The only cost is `render_dashboard()` reading two more files off disk at generation time -- trivial, and arguably a smaller change than (a) despite delivering the same author-facing benefits.

## Recommendation

- **Build (b).** It fully addresses the friction that motivated #51, costs less to implement and review than (a), and -- unlike (a) -- has no impact on `server.py`'s security surface or the app's documented standalone-file behavior.
- **Decline (a).** Live-verified as mechanically workable, but its real costs (new server routes/MIME handling, a new generation-version-skew risk class, breaking a documented portability property) buy nothing that (b) doesn't already deliver for less.

## Drop-in text for IMPLEMENTATION_PLAN.md (if the decline is confirmed)

## Considered and declined -- serving CSS/JS as separate HTTP-served files (issue #51, 2026-08-08)

While addressing the CSS/JS-embedded-in-Python friction from #51 (surfaced during the #48/#50 theme work), serving them as separate files alongside `dashboard.html` via new `server.py` routes was considered and declined in favor of keeping them as real source files inlined into `dashboard.html` at generation time (same mechanism `__DASHBOARD_DATA__` already uses). The HTTP-serving approach was live-verified as mechanically workable (extracted CSS/JS loaded and ran correctly via `<link>`/`<script src>` over a real HTTP server, zero console errors) but was declined because `server.py`'s `do_GET` would need new routes and manual MIME-type handling it doesn't have today, it introduces a generation-version-skew risk class that doesn't exist today (a stale cached asset served against a newer `dashboard.html`), and it breaks the README's documented standalone-file usage (copying or opening just `dashboard.html` elsewhere, with styling and interactivity intact) unless sibling files travel with it. The chosen approach delivers the same author-facing tooling benefits (real syntax highlighting, real diffs, a real linter) with none of those costs, since the served artifact doesn't change at all.
