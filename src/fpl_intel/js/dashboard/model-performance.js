function renderPerformance() {
  const summary = performance.summary || {};
  const calibration = performance.calibration || { recommendations: [] };
  const format = (value) =>
    value == null ? "Awaiting" : Number(value).toFixed(1);
  const coverage =
    summary.range_coverage == null
      ? "Awaiting"
      : `${(Number(summary.range_coverage) * 100).toFixed(0)}%`;
  const active = performance.status === "active";
  byId("performance-status").className = active ? "status-good" : "status-wait";
  byId("performance-status").textContent = active
    ? `${performance.completed_comparisons} completed comparison${performance.completed_comparisons === 1 ? "" : "s"}`
    : "Starts after Gameweek 1 is final";
  byId("performance-summary").innerHTML =
    `<div class="decision-metric"><b>${Number(performance.completed_comparisons || 0)}</b><span>Completed profile-horizon comparisons</span></div><div class="decision-metric"><b>${Number(performance.actual_events_collected || 0)}</b><span>Official Gameweeks collected</span></div><div class="decision-metric"><b>${format(summary.mae)}</b><span>Mean absolute error</span></div><div class="decision-metric"><b>${format(summary.bias)}</b><span>Bias · actual minus modeled</span></div><div class="decision-metric"><b>${coverage}</b><span>Uncertainty-range coverage</span></div>`;
  const horizons = performance.by_horizon || {};
  byId("performance-horizons").innerHTML = [1, 3, 5]
    .map((horizon) => {
      const row = horizons[String(horizon)] || {};
      return `<div class="performance-card"><b>${format(row.mae)}</b><span>${horizon}-GW MAE · ${Number(row.count || 0)} completed</span><span>Bias ${format(row.bias)} · coverage ${row.range_coverage == null ? "Awaiting" : `${(Number(row.range_coverage) * 100).toFixed(0)}%`}</span></div>`;
    })
    .join("");
  byId("performance-calibration").innerHTML =
    `<p class="${calibration.ready ? "status-good" : "status-wait"}">${esc(calibration.status || "Waiting for enough completed forecasts.")}</p>${(calibration.recommendations || []).map((item) => `<div class="decision-note">${esc(item)}</div>`).join("")}<div class="decision-note"><strong>Improvement policy</strong><br>Use error, bias, and interval coverage to review minutes, scoring-rate, and uncertainty assumptions. No weights are silently retuned from a small sample.</div>`;
  const comparisons = performance.comparisons || [];
  byId("performance-history").innerHTML = comparisons.length
    ? comparisons
        .slice(0, 50)
        .map((row) => {
          const error = Number(row.error);
          return `<tr class="performance-row"><th scope="row">GW${row.origin_event}</th><td>${esc(row.profile_label)}</td><td>${row.horizon} GW</td><td>${Number(row.modeled_points).toFixed(1)}</td><td>${Number(row.actual_points).toFixed(1)}</td><td class="${error > 0 ? "positive" : error < 0 ? "negative" : ""}">${error > 0 ? "+" : ""}${error.toFixed(1)}</td><td>${row.inside_range ? "Yes" : "No"}</td></tr>`;
        })
        .join("")
    : '<tr><td colspan="7"><div class="empty">No completed comparison yet. The current pre-GW1 forecast is stored locally and will be scored after official results are final.</div></td></tr>';
  byId("performance-method").textContent =
    performance.method ||
    "Frozen pre-event forecasts are compared with official FPL points.";
  const collectionErrors = performance.collection_errors || [];
  byId("performance-errors").innerHTML = collectionErrors
    .map(
      (error) =>
        `<div class="limitation-note"><strong>Result collection issue</strong>${esc(error.message || error)}</div>`,
    )
    .join("");
}
function renderShadowModels() {
  const shadowModels = performance.shadow_models || {};
  const versions = Object.keys(shadowModels);
  const statusEl = byId("shadow-models-status");
  const format = (value) =>
    value == null ? "Awaiting" : Number(value).toFixed(1);
  if (!versions.length) {
    statusEl.className = "status-wait";
    statusEl.textContent = "No shadow models tracked yet";
    byId("shadow-models-list").innerHTML =
      '<p class="empty">No experimental models are currently running in shadow.</p>';
    return;
  }
  statusEl.className = "status-good";
  statusEl.textContent = `${versions.length} shadow model${versions.length === 1 ? "" : "s"} tracked`;
  byId("shadow-models-list").innerHTML = versions
    .map((version) => {
      const model = shadowModels[version] || {};
      const summary = model.summary || {};
      const active = model.status === "active";
      const completed = Number(model.comparisons?.length || 0);
      const coverage =
        summary.range_coverage == null
          ? "Awaiting"
          : `${(Number(summary.range_coverage) * 100).toFixed(0)}%`;
      return `<div style="margin-top:12px"><div class="section-heading"><h3 style="font-size:14px;margin:0">${esc(version)}</h3><span class="${active ? "status-good" : "status-wait"}">${active ? `${completed} completed comparison${completed === 1 ? "" : "s"}` : "Awaiting completed Gameweeks"}</span></div><div class="performance-horizons"><div class="performance-card"><b>${format(summary.mae)}</b><span>Mean absolute error</span></div><div class="performance-card"><b>${format(summary.bias)}</b><span>Bias · actual minus modeled</span></div><div class="performance-card"><b>${coverage}</b><span>Uncertainty-range coverage</span></div></div></div>`;
    })
    .join("");
}
function renderTeamPerformance() {
  const manager = state.manager || { connection_status: "not_configured" };
  const teamPerformance = performance.team_performance || { comparisons: [] };
  const comparisons = teamPerformance.comparisons || [];
  const statusEl = byId("performance-team-status");
  const summaryEl = byId("performance-team-summary");
  const historyEl = byId("performance-team-history");
  const methodEl = byId("performance-team-method");
  if (manager.connection_status === "not_configured") {
    statusEl.className = "status-wait";
    statusEl.textContent = "FPL team not connected";
    summaryEl.innerHTML = "";
    historyEl.innerHTML =
      '<tr><td colspan="5"><div class="empty">Enter your FPL team ID in the Manager profile form on the My Profile view, then save.</div></td></tr>';
    methodEl.textContent = "";
    return;
  }
  if (!comparisons.length) {
    statusEl.className = "status-wait";
    summaryEl.innerHTML = "";
    if (!performance.actual_events_collected) {
      statusEl.textContent = "Waiting for completed Gameweeks";
      historyEl.innerHTML =
        '<tr><td colspan="5"><div class="empty">No Gameweek has finished yet. Team comparisons appear after official results are collected.</div></td></tr>';
    } else {
      statusEl.textContent = "Waiting for a frozen pre-deadline forecast";
      historyEl.innerHTML =
        '<tr><td colspan="5"><div class="empty">No pre-deadline player forecast was frozen for a finished Gameweek yet, so no team comparison can be shown. Results are never backfilled with hindsight lineups.</div></td></tr>';
    }
    methodEl.textContent = teamPerformance.method || "";
    return;
  }
  statusEl.className = "status-good";
  statusEl.textContent = `${comparisons.length} completed Gameweek${comparisons.length === 1 ? "" : "s"}`;
  const summary = teamPerformance.summary || {};
  const format = (value) =>
    value == null ? "Awaiting" : Number(value).toFixed(1);
  summaryEl.innerHTML = `<div class="decision-metric"><b>${Number(summary.count || 0)}</b><span>Completed Gameweeks</span></div><div class="decision-metric"><b>${format(summary.mae)}</b><span>Mean absolute error</span></div><div class="decision-metric"><b>${format(summary.bias)}</b><span>Bias · actual minus modeled</span></div>`;
  historyEl.innerHTML = comparisons
    .slice()
    .sort((a, b) => b.event - a.event)
    .map((row) => {
      const error = Number(row.error);
      return `<tr class="performance-row"><th scope="row">GW${row.event}</th><td>${Number(row.modeled_points).toFixed(1)}</td><td>${Number(row.actual_points).toFixed(1)}</td><td class="${error > 0 ? "positive" : error < 0 ? "negative" : ""}">${error > 0 ? "+" : ""}${error.toFixed(1)}</td><td>${row.inside_range ? "Yes" : "No"}</td></tr>`;
    })
    .join("");
  methodEl.textContent = teamPerformance.method || "";
}
function renderPlayerPerformance() {
  const playerPerformance = performance.player_performance || {
    comparisons: [],
  };
  const comparisons = playerPerformance.comparisons || [];
  const playerById = {};
  players.forEach((player) => {
    playerById[player.id] = player;
  });
  const label = (id) => (playerById[id] ? playerById[id].name : `Player ${id}`);
  const commentedIds = [...new Set(comparisons.map((row) => row.element_id))];
  const squad = (state.manager && state.manager.squad) || [];
  const squadIds = new Set(squad.map((player) => player.element_id));
  const byName = (a, b) => label(a).localeCompare(label(b));
  const squadForecastIds = commentedIds
    .filter((id) => squadIds.has(id))
    .sort(byName);
  const otherForecastIds = commentedIds
    .filter((id) => !squadIds.has(id))
    .sort(byName);
  const select = byId("performance-player-select");
  const previousValue = select.value;
  const optgroup = (title, ids) =>
    ids.length
      ? `<optgroup label="${esc(title)}">${ids.map((id) => `<option value="${id}">${esc(label(id))}</option>`).join("")}</optgroup>`
      : "";
  select.innerHTML =
    optgroup("My squad", squadForecastIds) +
    optgroup("All forecast players", otherForecastIds);
  if (!commentedIds.length) {
    byId("performance-player-history").innerHTML =
      '<tr><td colspan="5"><div class="empty">No frozen per-player forecasts have been compared with results yet.</div></td></tr>';
    byId("performance-player-summary").textContent = "";
    return;
  }
  const defaultId = squadForecastIds.length
    ? squadForecastIds[0]
    : otherForecastIds[0];
  let selected = Number(previousValue);
  if (!commentedIds.includes(selected)) selected = defaultId;
  select.value = String(selected);
  const renderRows = () => {
    const id = Number(select.value);
    const rows = comparisons
      .filter((row) => row.element_id === id)
      .slice()
      .sort((a, b) => b.event - a.event);
    byId("performance-player-history").innerHTML = rows.length
      ? rows
          .map((row) => {
            const error = Number(row.error);
            return `<tr class="performance-row"><th scope="row">GW${row.event}</th><td>${Number(row.modeled_points).toFixed(1)}</td><td>${Number(row.actual_points).toFixed(1)}</td><td class="${error > 0 ? "positive" : error < 0 ? "negative" : ""}">${error > 0 ? "+" : ""}${error.toFixed(1)}</td><td>${row.inside_range ? "Yes" : "No"}</td></tr>`;
          })
          .join("")
      : '<tr><td colspan="5"><div class="empty">No completed comparison yet for this player.</div></td></tr>';
    const count = rows.length;
    const mae = count
      ? rows.reduce((total, row) => total + Math.abs(row.error), 0) / count
      : null;
    const bias = count
      ? rows.reduce((total, row) => total + row.error, 0) / count
      : null;
    byId("performance-player-summary").textContent = count
      ? `${count} completed comparison${count === 1 ? "" : "s"} · MAE ${mae.toFixed(1)} · bias ${bias >= 0 ? "+" : ""}${bias.toFixed(1)}`
      : "No completed comparison yet for this player.";
  };
  select.onchange = renderRows;
  renderRows();
}
function renderModel() {
  byId("model-readiness").innerHTML =
    `<dt>User-facing state</dt><dd>${esc(seasonLabel())}</dd><dt>Raw status</dt><dd>${esc(state.fpl.season_status)}</dd><dt>Phase</dt><dd>${esc(state.fpl.season_phase || "feed_pending")}</dd><dt>Players</dt><dd>${state.fpl.player_count}</dd><dt>Clubs</dt><dd>${state.fpl.team_count}</dd><dt>Decision model</dt><dd class="${decision.status === "active_preliminary" ? "status-good" : "status-wait"}">${esc(decision.status)}</dd><dt>Projection version</dt><dd>${esc((decision.model || {}).version || "Not active")}</dd><dt>Generated</dt><dd>${esc(state.generated_at)}</dd>`;
  const sources = state.sources || [];
  byId("source-list").innerHTML = sources.length
    ? sources
        .map(
          (source) =>
            `<div class="source">${safeLink(source.url, source.name)}<span>Configured</span></div>`,
        )
        .join("")
    : '<div class="empty">No sources are registered.</div>';
}
