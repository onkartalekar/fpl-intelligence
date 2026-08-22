"""Self-contained local FPL preseason and in-season decision workspace."""

import json
from pathlib import Path

from .sources.transfers import OFFICIAL_CLUB_DOMAINS


_TRUSTED_LINK_DOMAINS = sorted(
    OFFICIAL_CLUB_DOMAINS | {"premierleague.com", "fantasy.premierleague.com"}
)

# The dashboard's HTML shell, CSS, and JS all live in their own real files
# (real syntax highlighting, real diffs, a real linter -- none of which apply
# to a giant string literal) and are assembled into the generated
# dashboard.html at generation time below, exactly as __DASHBOARD_DATA__
# already is -- the served artifact stays a single self-contained file, only
# the source representation changes. Grouped into templates/css/js/img
# subfolders by type (rather than sitting flat alongside this package's ~30
# .py modules) purely as source-tree organization -- these are still read
# once at import time and assembled via string substitution, never served as
# literal static files over HTTP (see IMPLEMENTATION_PLAN.md's "Considered
# and declined -- serving CSS/JS as separate HTTP-served files" for why), so
# the subfolder split has no effect on served behavior.
_STATIC_DIR = Path(__file__).resolve().parent
_TEMPLATE = (_STATIC_DIR / "templates" / "dashboard-shell.html").read_text(encoding="utf-8")
_DASHBOARD_CSS = (_STATIC_DIR / "css" / "dashboard.css").read_text(encoding="utf-8")

# Issue #223: dashboard.js itself (~125KB across ~90 top-level functions with no internal section
# structure) was split into these per-feature source files under js/dashboard/, concatenated here
# in this exact order before inlining -- a direct extension of the same "real files, concatenated
# and inlined at generation time" mechanism #51/#54 already established for CSS/JS as a whole,
# not a new one. Deliberately NOT real ES modules: every function below still shares mutable
# module-level state by design (globals like `state`, `decision`, `draftSelection`), so the
# concatenated result is exactly one script's worth of code, byte-for-byte identical to the
# pre-split single dashboard.js (verified via the same A/B render comparison this repo's other
# CSS/JS changes use).
#
# Concatenation order is NOT alphabetical -- it mirrors dependency order in the original single
# file and matters for correctness, not just readability:
#   1. core.js must load first: every other file's top-level code (event-listener wiring that
#      runs immediately, not deferred into a callback) reads `byId`/`esc`/`foldDiacritics`/the
#      shared `state`/`decision`/`performance` module state declared here. `const`/`let` bindings
#      are not hoisted the way `function` declarations are -- reading one before its declaring
#      file has run is a ReferenceError, not just a stale value.
#   2. gates-and-bootstrap.js must load last: its own top-level code is the final call sequence
#      (`setupThemeToggle(); ...; restoreWorkspaceContext();`) that actually invokes every other
#      file's setup/render functions -- it has to run only once every other file's own top-level
#      `const`/`let` declarations have already executed.
#   3. The 9 files in between only reference each other from inside function bodies (never from
#      their own top-level code), so their relative order among themselves doesn't affect
#      correctness -- ordered here to roughly match the original file's layout. mobile-shell.js
#      (issue #242) is the one exception worth calling out explicitly even though it doesn't
#      change the rule: its own top-level code is all `function` declarations (hoisted, so its
#      actual position in the concatenation doesn't matter for availability) plus module state
#      initialized from literals, never from another file's `const`/`let` -- placed right after
#      core.js only because it's chrome every other view-specific file's click handlers call into
#      (openContentSheet/openConfirmSheet/isMobileShellBreakpoint), not because load order
#      requires it there.
_DASHBOARD_JS_FILES = [
    "core.js",
    "mobile-shell.js",
    "overview-transfers-players.js",
    "whats-new.js",
    "fixtures.js",
    "decision-center.js",
    "model-performance.js",
    "workspace-context.js",
    "profile-forms.js",
    "draft-squad.js",
    "gates-and-bootstrap.js",
]
_DASHBOARD_JS = "".join(
    (_STATIC_DIR / "js" / "dashboard" / name).read_text(encoding="utf-8")
    for name in _DASHBOARD_JS_FILES
)

# The shell's theme-detection script is a second, separate inline <script> the shell carries
# beyond __DASHBOARD_JS__ -- it has to run synchronously before first paint (reads a stored
# preference or the OS media query and sets data-theme on <html> immediately) so the page never
# flashes the wrong theme before dashboard.js finishes loading. That's a constraint on *when* it
# runs, not on *where its source lives* -- same as dashboard.js itself, it's a real file inlined
# at generation time via its own placeholder.
_THEME_INIT_JS = (_STATIC_DIR / "js" / "theme-init.js").read_text(encoding="utf-8")

# Issue #216: a single brand-colored PNG (a mint "--accent" dot on the "--bg" navy square, see
# dashboard.css) reused for every icon a browser or crawler asks for -- /favicon.ico, /apple-
# touch-icon.png, and /apple-touch-icon-precomposed.png (server.py wires all three to it). Read
# once at import time as bytes, the same pattern as _DASHBOARD_CSS/_DASHBOARD_JS just above,
# just not decoded as text since it's binary.
APP_ICON_PNG = (_STATIC_DIR / "img" / "app-icon.png").read_bytes()


def render_dashboard(state):
    data = json.dumps(state, ensure_ascii=False).replace("</", "<\\/")
    trusted_domains = json.dumps(_TRUSTED_LINK_DOMAINS, ensure_ascii=True)
    return (
        _TEMPLATE.replace("__DASHBOARD_CSS__", _DASHBOARD_CSS)
        .replace("__THEME_INIT_JS__", _THEME_INIT_JS)
        # __DASHBOARD_JS__ must be substituted before __TRUSTED_LINK_DOMAINS__:
        # that placeholder lives inside dashboard.js's own content, not in
        # _TEMPLATE directly, so it only becomes replaceable after this line.
        .replace("__DASHBOARD_JS__", _DASHBOARD_JS)
        .replace("__DASHBOARD_DATA__", data)
        .replace("__TRUSTED_LINK_DOMAINS__", trusted_domains)
    )
