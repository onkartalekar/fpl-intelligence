function captureWorkspaceContext() {
  const activeView =
    (document.querySelector(".view.active") || {}).id?.replace("view-", "") ||
    "squad";
  const activeProfile =
    (document.querySelector('[data-profile][aria-selected="true"]') || {})
      .dataset?.profile || null;
  const controlIds = [
    ...filterIds,
    "player-search",
    "player-club-filter",
    "player-position-filter",
    "player-sort",
    "fixture-gameweek",
    "fixture-club-filter",
  ];
  const controls = Object.fromEntries(
    controlIds
      .map((id) => [id, byId(id)?.value])
      .filter(([, value]) => value !== void 0),
  );
  sessionStorage.setItem(
    "fpl-workspace-context",
    JSON.stringify({ view: activeView, profile: activeProfile, controls }),
  );
}
function restoreWorkspaceContext() {
  let context = {};
  try {
    context = JSON.parse(
      sessionStorage.getItem("fpl-workspace-context") || "{}",
    );
  } catch (error) {
    sessionStorage.removeItem("fpl-workspace-context");
  }
  Object.entries(context.controls || {}).forEach(([id, value]) => {
    const control = byId(id);
    if (
      (control &&
        [...(control.options || [])].some(
          (option) => option.value === value,
        )) ||
      control?.type === "search"
    )
      control.value = value;
  });
  if (context.profile) {
    renderDecision(context.profile);
    renderWeeklyDecision(context.profile);
  }
  renderPlayers();
  renderFixtures();
  applyFilters();
  showView(titles[context.view] ? context.view : "squad");
}
function sourceSummary(payload) {
  const sources = payload.source_statuses || {};
  const mark = (value) =>
    value === "ok" ? "✓" : value === "error" ? "✕" : "not active";
  return `FPL ${mark(sources.fpl)} · My Team ${mark(sources.manager)} · Transfers ${mark(sources.transfers)} · Fixtures ${mark(sources.fixtures)}`;
}
// Issue #27: refreshing is an operator-only action (curl/a script using an env-var token) --
// no token is ever shipped to the browser, and there is no in-page "Refresh now" control
// anymore. `servedLive()` still distinguishes "opened as a static file" (file://, or the
// standalone dashboard.html) from "served over http(s) by the dashboard service", which the
// profile/draft-squad/reminder forms below use to decide whether their save requests can
// plausibly succeed.
function servedLive() {
  return location.protocol.startsWith("http");
}
function setupRefresh() {
  const message = byId("refresh-message");
  const sourceStatus = byId("refresh-source-status");
  const saved = sessionStorage.getItem("fpl-refresh-result");
  if (saved) {
    try {
      const result = JSON.parse(saved);
      message.textContent = `Updated ${fmtDate(result.generated_at, true)}`;
      sourceStatus.textContent = sourceSummary(result);
    } catch (error) {
      sessionStorage.removeItem("fpl-refresh-result");
    }
    return;
  }
  message.textContent = `Last refreshed ${fmtDate(state.generated_at, true)}`;
}
