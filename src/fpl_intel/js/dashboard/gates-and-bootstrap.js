// Issue #108: Decision Center and Model Performance are both personalized to a manager's own
// team and have nothing meaningful to show without one. Rather than gating inside each of their
// several render functions individually (renderDecision/renderWeeklyDecision for Decision
// Center; renderPerformance/renderShadowModels/renderTeamPerformance/renderTransferAdherence/
// renderPlayerPerformance for Model Performance -- separately, which is how the previous
// inconsistent, partial per-section messaging arose), this is a single gate applied once at the
// tab level, run right after the normal render pass: the normal render functions below still run
// and populate their elements as always, but a shared content wrapper around each tab's markup
// (#decisions-content /
// #performance-content) is hidden and a static empty-state panel (#decisions-empty-state /
// #performance-empty-state, dashboard.py) is shown in its place whenever no profile is resolved
// for this visitor. Uses the exact same signal already used at dashboard.js's renderManager(),
// renderWeeklyDecision()'s weekly-decision section, and renderTeamPerformance() -- so an explicit
// ?team_id= lookup of someone else's team (never 'not_configured'; see server.py's
// compute_manager_view/_serve_dashboard) still shows that team's real content, unchanged.
function applyProfileGates() {
  const gated = (state.manager || {}).connection_status === "not_configured";
  byId("decisions-content").hidden = gated;
  byId("decisions-empty-state").hidden = !gated;
  byId("performance-content").hidden = gated;
  byId("performance-empty-state").hidden = !gated;
}
setupThemeToggle();
setupRefresh();
populateFilters();
setupPlayerExplorer();
setupFixtures();
renderOverview();
renderDecision();
renderWeeklyDecision();
renderManager();
renderLookupBanner();
setupTeamLookup();
renderPerformance();
renderShadowModels();
renderTeamPerformance();
renderTransferAdherence();
renderPlayerPerformance();
renderModel();
applyProfileGates();
setupDecisionSubnav();
setupProfileForm();
setupReminderForm();
setupDraftSquad();
renderDraftHealth();
renderDraftPitch();
setupContactForm();
setupWhatsNew();
restoreWorkspaceContext();
setupMobileShell();
setupOnboarding();
