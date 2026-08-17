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
_DASHBOARD_JS = (_STATIC_DIR / "js" / "dashboard.js").read_text(encoding="utf-8")

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
