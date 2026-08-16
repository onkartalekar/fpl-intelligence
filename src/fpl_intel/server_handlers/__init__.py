"""Per-feature HTTP handler modules for `server.py`'s `DashboardHandler` (issue #210).

`server.py` kept absorbing every new feature's routing, request validation, and auth/rate-limit
wiring directly inside its one `DashboardHandler` class -- 351 to 2,104 lines in 20 days. The
domain layer (`transfer_decisions.py`, `profiles.py`, `release_notes.py`, ...) never had this
problem; this package gives the HTTP-facing layer the same one-module-per-concern shape.

Each module here exports:

- Any validation exception class(es) and payload-validation function(s) for its feature.
- `default_<feature>_action(root, ...)` -- the production write/read action, same DI-friendly
  shape `server.py` already used for every action before this split (`create_server`'s
  `*_action=None` parameters still override these exactly as before).
- `make_handle_<feature>(...)` -- a factory that closes over whatever per-server-instance
  dependencies (actions, limiters, tokens) the handler needs and returns a plain function taking
  `(self, ...)`. `create_server` assigns that function onto `DashboardHandler` as a class
  attribute (e.g. `DashboardHandler._handle_profile = profile.make_handle_profile(...)`), which
  Python treats as an ordinary bound method from then on -- the exact same closure-over-
  `create_server`'s-locals pattern this file already used for its `_default_*_action` factories,
  just relocated. `server.py` itself keeps only the `DashboardHandler` class shell, `do_GET`/
  `do_POST`'s routing table, and plumbing every handler needs (`_json`/`_send_html`, cookie/host/
  origin checks, `_resolve_team_lookup`/`_team_lookup_opted_out`).

No behavior changed by this split -- see issue #210.
"""
