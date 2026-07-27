# In-UI manager profile create/edit (issue #12)

## Context

Today the manager profile is configured by hand: copy
`config/user-profile.example.json` to `config/user-profile.json` and edit
it. Issue #12 asks for a UI flow in the local dashboard that creates and
edits that file directly.

**Verified facts that shape the design:**

- Only three profile knobs are live today (`src/fpl_intel/refresh.py`):
  - `manager.timezone` (line 173, falls back to `America/New_York`; also
    written into `state["timezone"]` at line 327 and used by the JS
    `fmtDate`/`timezoneLabel`),
  - `manager.team_id` (line 249; gates `collect_public_manager` and the
    `{team_id}` source URLs),
  - `manager.confirmed_free_transfers` + `_event` (lines 267-272; consumed
    in `src/fpl_intel/transfer_decisions.py` lines 688-702, where the count
    is clamped to `min(maximum_free_transfers, max(0, int(value)))` and
    ignored when `_event` does not match the current event).
- `manager.risk_profile` is currently inert, but the dashboard JS already
  reads `decision.default_profile||'balanced'` (dashboard.py line 142) and
  `weekly.default_profile||'balanced'` (line 154), and the backend
  hardcodes `"default_profile": "balanced"` in
  `src/fpl_intel/recommendations.py` line 825 and
  `src/fpl_intel/transfer_decisions.py` line 793. Overriding those two keys
  from the profile in `refresh.py` makes `risk_profile` live for a few
  lines of code -- so it earns a place on the form.
- The remaining fields (`deadline_availability`,
  `weekly_time_budget_minutes`, `primary_goal`, `mini_leagues`,
  `experience.previous_entry_id`) are read by nothing in `src/`. They stay
  out of the form. The write path merges into the existing file, so any
  hand-entered reference-only values are preserved, not clobbered.
- The server (`src/fpl_intel/server.py`) is localhost-only
  (`create_server` raises unless `host == "127.0.0.1"`), rejects untrusted
  `Host` (421) and cross-origin POSTs (403), and protects `/api/refresh`
  with a `X-Refresh-Token` header checked via `secrets.compare_digest`.
  The token is injected into the served HTML by replacing
  `content="__REFRESH_TOKEN__"`. The standalone `dashboard.html` file
  (opened via `file://`) keeps the placeholder, and `setupRefresh`
  (dashboard.py line 169) disables the Refresh button when
  `!location.protocol.startsWith('http') || token.includes('__REFRESH_TOKEN__')`.
  The new profile form must degrade the same way.
- The dashboard is one giant template string (`_TEMPLATE` in
  `src/fpl_intel/dashboard.py`, 181 physical lines: CSS lines 21-27, DOM
  lines 30-61, JS lines 63-170). The My Team view is
  `<section id="view-squad">` on line 55; `renderManager()` is line 163.
- PR #1's in-UI reminder is the `Setup`-level attention item on line 74
  ("Connect your FPL team ... Copy config/user-profile.example.json ...",
  action "Open My Team" -> `squad` view) plus the `setupNote` inside
  `renderManager` (line 163). `transfer_decisions.py` lines 660-663 carry
  the same copy-the-file instruction in the `manager_not_configured`
  reason. No test asserts these exact sentences, so the copy can change to
  point at the new form.
- Tests: `tests/test_server.py` (token/origin/host/content-length/busy
  behavior against `create_server`), `tests/test_dashboard.py` (substring
  assertions against the rendered template), `tests/test_refresh.py`
  (profile fixtures with `team_id`/`confirmed_free_transfers`). Runner:
  `PYTHONPATH=src python3 -m unittest discover -s tests -v` (README line
  134).
- `config/user-profile.json` is gitignored; nothing generated is committed.

## Design

### Fields exposed by the form

| Field | Input | Live effect |
| --- | --- | --- |
| `manager.team_id` | numeric text input, blank allowed (writes `null`) | connects My Team |
| `manager.timezone` | `<select>` of a curated IANA list (plus the current value if not listed) | timestamps and `generated_at` |
| `manager.confirmed_free_transfers` | number 0-5, blank allowed | weekly decision free-transfer count |
| `manager.confirmed_free_transfers_event` | number 1-38, required iff the count is set | scopes the count to one gameweek |
| `manager.risk_profile` | `<select>`: conservative / balanced / aggressive | becomes the default selected profile tab (new wiring) |

Dropped from the form (left in the file untouched by merge-writes):
`deadline_availability`, `weekly_time_budget_minutes`, `primary_goal`,
`mini_leagues`, `experience.previous_entry_id`.

### New endpoint: POST /api/profile

In `create_server` (`src/fpl_intel/server.py`), following the
`/api/refresh` pattern exactly:

- New keyword arg `profile_action=None` appended to the `create_server`
  signature (existing positional/keyword callers unaffected); default is
  `lambda payload: _default_profile_action(root, payload)`.
- Routing: `do_POST` currently 404s any path other than `/api/refresh`;
  change the path dispatch to handle `/api/profile` too. All existing
  shared checks run first and unchanged: `_reject_untrusted_host` (421),
  Origin check (403), then per-route token check with
  `secrets.compare_digest(self.headers.get("X-Refresh-Token", ""), token)`
  (403). The same token protects both endpoints -- one secret, one meta
  tag, no new plumbing in the HTML.
- Content-Length: same validation as refresh (400 on malformed/negative)
  but with a 4096-byte cap (413) since this body carries real JSON.
- Body: parse JSON; non-object or unparseable -> 400
  `{"status": "error", "message": "Invalid profile payload"}`.
- Concurrency: acquire the existing `refresh_lock` non-blocking; if held
  (a refresh is running), return 409 busy with the same payload shape as
  refresh. This prevents a write racing `refresh.py`'s read of the file.
- Success: 200 `{"status": "ok", "profile": {<the five live fields>}}`.
- Failure inside the action: 500 with a generic message
  ("Profile update failed") -- never internal paths or raw input, matching
  `test_refresh_returns_generic_browser_error_without_internal_details`.

### Validation rules (`_validate_profile_payload` in server.py)

Reject with 400 and a fixed, input-free message when any rule fails:

- Top-level payload: object; only the keys `team_id`, `timezone`,
  `confirmed_free_transfers`, `confirmed_free_transfers_event`,
  `risk_profile` are allowed; unknown keys -> 400.
- `team_id`: `None`/`""` -> stored as `null`; otherwise must be an integer
  (or all-digit string) with `1 <= team_id <= 99_999_999` -- a positive
  integer, no floats, no signs.
- `timezone`: required string, length <= 64, must match
  `^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}$` (defense before lookup),
  and must be a member of `zoneinfo.available_timezones()` (598 zones on
  this platform). No free-text passthrough.
- `confirmed_free_transfers`: `None`/`""` -> `null`; else integer
  `0 <= n <= 5` (5 mirrors `max_extra_free_transfers + 1` used at
  transfer_decisions.py line 687).
- `confirmed_free_transfers_event`: required (integer 1-38) when the count
  is set; must be `null` when the count is `null`. Requiring the event
  prevents a stale confirmed count silently applying to every future
  gameweek (the code at transfer_decisions.py lines 690-691 only discards
  the count when `_event` is present and mismatched).
- `risk_profile`: one of `conservative`, `balanced`, `aggressive`.

### Write path (`_default_profile_action` in server.py)

1. Load existing `root / "config" / "user-profile.json"` if present (else
   start from `{"manager": {}, "experience": {}}` -- do NOT read the
   example file; it is a template, not state).
2. Merge: set only the five validated keys under `"manager"`; delete
   `confirmed_free_transfers`/`_event` keys when `null` rather than
   storing nulls; leave every other existing key byte-for-byte.
3. Atomic write: dump to `user-profile.json.tmp` in the same directory,
   `os.replace` onto the real path, `indent=2` + trailing newline to stay
   diff-friendly with the hand-edited format.
4. Return the sanitized live-field dict for the response body.

The endpoint does NOT trigger a refresh itself; the browser drives the
existing refresh flow afterwards (below), so the reload behavior,
busy-lock handling, and `sessionStorage` context preservation are exactly
the ones the Refresh button already exercises.

### Prefill: `state["profile"]` (refresh.py)

`refresh.py` adds to the state dict (near line 327):

```python
"profile": {
    "team_id": profile.get("manager", {}).get("team_id"),
    "timezone": timezone_name,
    "confirmed_free_transfers": profile.get("manager", {}).get("confirmed_free_transfers"),
    "confirmed_free_transfers_event": profile.get("manager", {}).get("confirmed_free_transfers_event"),
    "risk_profile": profile.get("manager", {}).get("risk_profile") or "balanced",
},
```

Whitelist only -- reference-only fields never enter the rendered HTML.
The JS must tolerate `state.profile` being absent (older state files).

### Making risk_profile live (refresh.py)

After `decision_center` is assembled (after line 303):

```python
risk = profile.get("manager", {}).get("risk_profile")
if risk in {"conservative", "balanced", "aggressive"}:
    if decision_center.get("profile_recommendations"):
        decision_center["default_profile"] = risk
    weekly = decision_center.get("weekly_decisions")
    if isinstance(weekly, dict) and weekly.get("profiles"):
        weekly["default_profile"] = risk
```

No change to `recommendations.py`/`transfer_decisions.py` internals; the
JS default-tab selection picks it up unmodified. Also give the hardcoded
topbar tagline fragment `balanced risk` (dashboard.py line 37) a
`<span id="topbar-risk">balanced risk</span>` and set it from
`state.profile.risk_profile` next to the existing `topbar-timezone`
assignment (line 68).

### Form UI (dashboard.py)

Placement: the My Team view, `<section id="view-squad">` (line 55) --
this is where the not_configured state and the PR #1 reminder already
send the user. Append a new panel after the "Latest public squad" panel:

```html
<section class="panel" style="margin-top:14px" id="profile-settings">
  <div class="section-heading"><div><h2>Manager profile</h2>
  <span class="muted">Saved to config/user-profile.json on this machine · no password, no account access</span></div></div>
  <form id="profile-form">
    ... five .field blocks with ids:
    profile-team-id (input, inputmode="numeric"),
    profile-timezone (select),
    profile-risk (select),
    profile-free-transfers (input type="number" min="0" max="5"),
    profile-free-transfers-event (input type="number" min="1" max="38")
    <button id="profile-save" class="refresh-button" type="submit">Save and refresh</button>
    <div id="profile-message" class="refresh-message" role="status" aria-live="polite"></div>
  </form>
</section>
```

Reuse existing `.field`/`.refresh-button`/`.refresh-message` styles; a
small `.profile-form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:9px}`
(collapsing to `1fr` inside the existing 760px media block) is the only
new CSS.

New JS `setupProfileForm()` (called from the bootstrap line 170, before
`restoreWorkspaceContext()`):

- Curated timezone list as a JS const (about 25 common IANA zones across
  continents); if `state.profile.timezone` or `state.timezone` is not in
  the list, append it so the current value always round-trips.
- Prefill from `state.profile` with fallbacks (`timezone` ->
  `state.timezone` -> `America/New_York`; `risk_profile` -> `balanced`).
- Availability: the same predicate as `setupRefresh` --
  `location.protocol.startsWith('http') && token && !token.includes('__REFRESH_TOKEN__')`.
  When unavailable: disable every control and the save button, set
  `profile-message` to
  `Start the local dashboard service to edit your profile.` (mirrors the
  disabled-Refresh wording). Values still render read-only for reference.
- Client-side pre-validation mirroring the server rules (positive-integer
  team id, event required with count) with inline `profile-message`
  errors; the server remains the authority.
- Submit handler: `fetch('/api/profile', {method:'POST', headers:{'X-Refresh-Token':token,'Content-Type':'application/json'}, body: JSON.stringify(payload)})`;
  on non-OK show `payload.message`; on success set
  `profile-message` to `Profile saved. Refreshing...` and invoke the
  shared refresh routine.
- Refactor `setupRefresh`'s click handler body into
  `async function runRefresh()` used by both the Refresh button and the
  profile save flow, keeping the exact literal
  `captureWorkspaceContext();sessionStorage.setItem('fpl-refresh-result'`
  intact (test-asserted).

Copy updates pointing at the form instead of hand-editing:

- Attention item (line 74): body becomes
  `Enter your FPL team ID in the Manager profile form on the My Team view, then save.`
  (action stays `Open My Team` -> `squad`).
- `renderManager` `setupNote` (line 163): same reframed sentence; keep the
  hint that the ID comes from the FPL entry URL.
- `transfer_decisions.py` lines 660-663 (`manager_not_configured` reason):
  reword to `No public team ID is configured. Add your FPL team ID in the My Team profile form, then refresh.`
- README "Configure your manager" section: mention the in-UI form as the
  primary path (file editing still works), and move `risk_profile` from
  the recorded-only list to the live list.

### Standalone-file degradation summary

`render_dashboard` output keeps `content="__REFRESH_TOKEN__"`; only the
server GET injects the real token. Therefore in the standalone file the
availability predicate is false, the form is disabled with the
explanatory note, and no `/api/profile` call can ever be made -- same
mechanism, same UX as the disabled Refresh button.

## Hard constraints

Security invariants (all already enforced for /api/refresh; the new
endpoint must not weaken any):

- Server binds 127.0.0.1 only (`create_server` raises otherwise) --
  asserted by `test_server_rejects_non_loopback_binding`.
- Untrusted `Host` -> 421 before any token-bearing byte is served.
- Cross-origin POST -> 403 even with a valid token.
- Missing/wrong `X-Refresh-Token` -> 403 via `secrets.compare_digest`;
  the profile file must be untouched on every rejected request.
- Malformed Content-Length -> controlled 400; oversized body -> 413.
- Error responses carry fixed generic messages -- no filesystem paths, no
  echoed user input, no stack traces (mirrors
  `test_refresh_returns_generic_browser_error_without_internal_details`).
- No secrets in the profile file or in `state["profile"]`; no credentials,
  no account actions (Model Status "Account boundary" panel stays true).
- Writes only to `root / "config" / "user-profile.json"`, atomically; the
  path is server-fixed, never derived from the request.
- The standalone HTML keeps the `__REFRESH_TOKEN__` placeholder; the form
  is inert without the served token.

Existing test assertions that must survive verbatim:

- `tests/test_server.py`: entire `DashboardServerTests` suite -- the
  `create_server(self.root, host=..., port=0, token="test-token", refresh_action=...)`
  call signature, token injection (`content="test-token"`, no
  `__REFRESH_TOKEN__` remnant), 421/403/400/409/500 behaviors, exact busy
  payload `{"status": "busy", "message": "A refresh is already running"}`,
  and `_default_refresh_action` fresh-process behavior.
- `tests/test_dashboard.py`: all substring assertions, notably
  `id="refresh-now"`, `name="refresh-token"`, `id="refresh-message"`,
  `id="refresh-source-status"`, `id="my-team-summary"`, `id="squad-grid"`,
  `"Team ID"`, the subnav id-list literal, the aria-tabs literals, and
  `captureWorkspaceContext();sessionStorage.setItem('fpl-refresh-result'`
  (constrains the `runRefresh` refactor).
- `tests/test_refresh.py`: profile fixtures
  (`{"manager": {"team_id": 364759}}` and the
  `confirmed_free_transfers: 3` variant) must keep producing
  `state["manager"]["team_id"] == 364759`, `weekly["free_transfers"] == 3`,
  `free_transfer_source == "confirmed_local"`, and 3 weekly profiles --
  the new `state["profile"]` key and default_profile override must not
  disturb them (those fixtures set no `risk_profile`, so `default_profile`
  stays `"balanced"`).
- `tests/test_refresh_safety.py` and the lock semantics: `/api/profile`
  must use the in-process `refresh_lock` only; it must not touch
  `.refresh.lock`.

## New test assertions

`tests/test_server.py` -- new `ProfileEndpointTests` class (same
setUp/tearDown shape as `DashboardServerTests`, plus
`(self.root / "config").mkdir()`):

- POST `/api/profile` without token -> 403 and
  `config/user-profile.json` does not exist afterwards.
- POST with valid token but `Origin: https://attacker.example` -> 403,
  file untouched.
- Valid POST `{"team_id": 364759, "timezone": "America/New_York", "risk_profile": "balanced", "confirmed_free_transfers": null, "confirmed_free_transfers_event": null}`
  -> 200, `payload["status"] == "ok"`, file exists, parsed JSON has
  `manager.team_id == 364759`, and no `confirmed_free_transfers` key.
- Merge preserves reference-only fields: pre-write a file containing
  `"primary_goal": "overall_rank_below_50000"` and
  `"experience": {"previous_entry_id": 123}`; after a valid POST both
  survive unchanged.
- `team_id: "abc"` -> 400; `team_id: -5` -> 400; `team_id: 1.5` -> 400;
  file untouched; response contains no `"abc"` echo.
- `timezone: "Not/A_Zone"` -> 400; `timezone: "../etc/passwd"` -> 400.
- `risk_profile: "yolo"` -> 400.
- `confirmed_free_transfers: 3` without `_event` -> 400;
  `confirmed_free_transfers: 9` -> 400.
- Unknown key (`{"password": "x", ...}`) -> 400 and the word `password`
  never written to disk.
- Body over 4096 bytes -> 413.
- Non-JSON body -> 400 with `{"status": "error"}`.
- While `refresh_lock` is held (start a slow refresh via a threading.Event
  refresh_action) -> 409 busy.
- 500 path: `profile_action` raising `RuntimeError("/private/path secret")`
  -> generic message, `"private/path"` not in the response.

`tests/test_dashboard.py` -- extend the rendered-template assertions:

- `id="profile-settings"`, `id="profile-form"`, `id="profile-team-id"`,
  `id="profile-timezone"`, `id="profile-risk"`,
  `id="profile-free-transfers"`, `id="profile-free-transfers-event"`,
  `id="profile-save"`, `id="profile-message"` all present.
- `"fetch('/api/profile'"` and a single shared token read (the form must
  send `X-Refresh-Token`).
- `"Start the local dashboard service to edit your profile."` present
  (standalone degradation note).
- Not-configured copy points at the form: assert
  `"Manager profile form"` present and the old
  `"Copy config/user-profile.example.json"` absent from the template.
- `function runRefresh()` present and the preserved literal
  `captureWorkspaceContext();sessionStorage.setItem('fpl-refresh-result'`
  still present.

`tests/test_refresh.py` -- new cases:

- Profile file with `"risk_profile": "aggressive"` (plus the fields the
  existing GW2 fixture uses) -> `state["decision_center"]["default_profile"] == "aggressive"`
  and `state["decision_center"]["weekly_decisions"]["default_profile"] == "aggressive"`.
- `state["profile"]` contains exactly the five live keys; a profile file
  containing `primary_goal` does not leak it into `state["profile"]`.
- Missing profile file -> `state["profile"]["timezone"] == "America/New_York"`,
  `team_id is None`.

## Edit sequence

1. `src/fpl_intel/server.py`: add `_ALLOWED_RISK_PROFILES`,
   `_validate_profile_payload(payload)` (returns cleaned dict or raises
   `ProfileValidationError(message)`), `_default_profile_action(root, payload)`
   (merge + atomic write). Extend `create_server` with
   `profile_action=None`; in `do_POST`, restructure the path dispatch to
   route `/api/refresh` and `/api/profile` after the shared
   host/origin/token/content-length checks (per-route body-size caps:
   1024 refresh, 4096 profile).
2. `src/fpl_intel/refresh.py`: add the `state["profile"]` whitelist block
   and the `default_profile` risk override after `decision_center`
   assembly.
3. `src/fpl_intel/transfer_decisions.py` lines 660-663: reword the
   `manager_not_configured` reason to point at the My Team form.
4. `src/fpl_intel/dashboard.py`:
   a. CSS (line 22 + the 760px block on line 26): `.profile-form-grid`
      rules.
   b. DOM (line 55, `view-squad`): append the `profile-settings` panel
      with the form markup and ids above; wrap the tagline fragment on
      line 37 as `<span id="topbar-risk">balanced risk</span>`.
   c. JS: update attention-item body (line 74) and `renderManager`
      setupNote (line 163); set `topbar-risk` text next to the
      `topbar-timezone` assignment (line 68); extract `runRefresh()` from
      the click handler in `setupRefresh` (line 169); add
      `setupProfileForm()` (timezone list, prefill, availability gate,
      validation, submit -> POST -> `runRefresh()`); add
      `setupProfileForm();` to the bootstrap call chain (line 170).
5. Tests: new `ProfileEndpointTests` in `tests/test_server.py`; new
   assertions in `tests/test_dashboard.py`; new refresh cases in
   `tests/test_refresh.py` per the section above.
6. `README.md`: rewrite "Configure your manager" around the in-UI form
   (file editing remains a supported fallback); move `risk_profile` to the
   live-fields sentence; drop it from the recorded-only list.

## Verification

1. `PYTHONPATH=src python3 -m unittest discover -s tests -v` -- full suite
   green (this is the README-documented runner; no pytest config exists).
2. Manual smoke test on the branch:
   - `python3 scripts/refresh_dashboard.py` then
     `python3 scripts/start_dashboard.py --no-open`; open the printed URL.
   - My Team -> Manager profile form is enabled and prefilled; save a
     team id -> 200, page announces `Profile saved. Refreshing...`,
     reloads, My Team shows the connected entry, and
     `config/user-profile.json` on disk contains the merge (with any
     pre-existing reference-only fields intact).
   - Save an invalid team id (`abc`) -> inline error, no write.
   - Set `risk_profile` to aggressive, save -> after reload the
     Aggressive tab is preselected in both Decision Center tab groups and
     the topbar reads `aggressive risk`.
   - Open the raw `dashboard.html` file via `file://` -> form controls
     and save button disabled, note reads
     `Start the local dashboard service to edit your profile.`, Refresh
     button equally disabled.
   - `curl -s -X POST http://127.0.0.1:8877/api/profile -d '{}'` (no
     token) -> 403 and the profile file is unchanged.
3. `git status` -- confirm no generated files (`dashboard.html`,
   `data/dashboard-state.json`, `config/user-profile.json`) are staged.
