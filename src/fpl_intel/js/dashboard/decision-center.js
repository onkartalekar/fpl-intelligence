const breakdownKeys = [
  "appearance",
  "attacking",
  "clean_sheet",
  "goals_conceded",
  "defensive_contribution",
  "saves",
  "bonus",
  "residual",
];
const breakdownLabels = [
  "Appear",
  "Attack",
  "Clean sheet",
  "Conceded",
  "Def. contrib.",
  "Saves",
  "Bonus",
  "Residual",
];
const breakdownBuckets = [
  { label: "Appearance", keys: ["appearance"], cls: "bucket-appearance" },
  { label: "Attacking", keys: ["attacking"], cls: "bucket-attacking" },
  { label: "Clean sheet", keys: ["clean_sheet"], cls: "bucket-clean-sheet" },
  {
    label: "Def./saves/bonus/residual",
    keys: ["defensive_contribution", "saves", "bonus", "residual"],
    cls: "bucket-other",
  },
];
function renderPlayerBreakdown(player) {
  selectedBreakdownPlayerId = player.id;
  const modelNotes = [
    player.uses_team_strength ? "fitted opponent model" : null,
    player.uses_recency_minutes ? "recency-weighted minutes" : null,
  ]
    .filter(Boolean)
    .join(" · ");
  byId("player-breakdown-name").textContent =
    `${player.name} · ${player.position_short} · ${player.club}${modelNotes ? ` · ${modelNotes}` : ""}`;
  const events = player.projection_events || [];
  const rows = player.component_xp || [];
  if (!events.length || !rows.length) {
    byId("player-breakdown-table").innerHTML =
      '<div class="empty">No per-event breakdown is available for this player.</div>';
    renderSelectionRationale(player);
    return;
  }
  const totals = events.map((event, index) => {
    const row = rows[index] || {};
    const modeled = Number(
      row.modeled_total_before_ep_next ??
        breakdownKeys.reduce((sum, key) => sum + Number(row[key] || 0), 0),
    );
    const adjustment = Number(row.ep_next_adjustment || 0);
    return Number(row.blended_total ?? modeled + adjustment);
  });
  const maxTotal = Math.max(5, ...totals.map((value) => Math.max(0, value)));
  const legend = breakdownBuckets
    .map(
      (bucket) =>
        `<span><i class="${bucket.cls}"></i>${esc(bucket.label)}</span>`,
    )
    .join("");
  const bars = events
    .map((event, index) => {
      const row = rows[index] || {};
      const blended = totals[index];
      const opponents = row.opponents || [];
      const chips = opponents.length
        ? opponents
            .map(
              (opponent) =>
                `<span class="difficulty d${opponent.difficulty}" title="${esc(opponent.club_short)}${opponent.is_home ? " (H)" : " (A)"}">${esc(opponent.club_short)}</span>`,
            )
            .join("")
        : '<span class="muted" style="font-size:11px">No fixture</span>';
      const segments = breakdownBuckets
        .map((bucket) => {
          const value = bucket.keys.reduce(
            (sum, key) => sum + Number(row[key] || 0),
            0,
          );
          const pct = maxTotal > 0 ? (Math.max(0, value) / maxTotal) * 100 : 0;
          return `<div class="breakdown-segment ${bucket.cls}" style="width:${pct.toFixed(1)}%" title="${esc(bucket.label)}: ${value.toFixed(2)}"></div>`;
        })
        .join("");
      return `<div class="breakdown-bar-row"><div class="breakdown-bar-gw">GW${event}<br>${chips}</div><div class="breakdown-bar-track">${segments}</div><div class="breakdown-bar-total">${blended.toFixed(2)}</div></div>`;
    })
    .join("");
  const exactTable = `<div class="breakdown-exact">${events
    .map((event, index) => {
      const row = rows[index] || {};
      const modeled = Number(
        row.modeled_total_before_ep_next ??
          breakdownKeys.reduce((sum, key) => sum + Number(row[key] || 0), 0),
      );
      const adjustment = Number(row.ep_next_adjustment || 0);
      const blended = totals[index];
      const opponentLabel = (row.opponents || []).length
        ? row.opponents
            .map(
              (opponent) =>
                `${opponent.club_short}${opponent.is_home ? " (H)" : " (A)"}`,
            )
            .join(", ")
        : "No fixture";
      const fields = breakdownKeys
        .map(
          (key, keyIndex) =>
            `<span>${breakdownLabels[keyIndex]}<b>${Number(row[key] || 0).toFixed(2)}</b></span>`,
        )
        .join("");
      return `<div class="breakdown-exact-row"><div class="breakdown-exact-gw">GW${event}<small class="muted">${esc(opponentLabel)}</small></div><div class="breakdown-exact-grid">${fields}<span>Modeled<b>${modeled.toFixed(2)}</b></span><span>ep_next adj.<b>${adjustment.toFixed(2)}</b></span><span>Final xPts<b class="total">${blended.toFixed(2)}</b></span></div></div>`;
    })
    .join("")}</div>`;
  byId("player-breakdown-table").innerHTML =
    `<div class="breakdown-legend">${legend}</div><div class="breakdown-bars">${bars}</div><p class="breakdown-note">Each bar is composed of named scoring components from official rate stats and fixture data, plus a shrunk over/under-performance residual. Bar segments show gross positive contribution only; the number shown is the true final total after the goals-conceded deduction, which can make it lower than the bar implies.</p><details class="model-disclosure"><summary>Show exact component values</summary>${exactTable}</details>`;
  renderSelectionRationale(player);
}
function renderSelectionRationale(player) {
  const alternatives = selectedRationaleMap[player.id] || [];
  byId("player-rationale").innerHTML = alternatives.length
    ? `<h3 style="font-size:14px;margin:0 0 8px">Why ${esc(player.name)} over other ${esc(player.position_short)} options</h3><div class="decision-list">${alternatives
        .map((alternative) => {
          const price =
            alternative.price_delta >= 0
              ? `+£${alternative.price_delta.toFixed(1)}m`
              : `-£${Math.abs(alternative.price_delta).toFixed(1)}m`;
          const xp =
            alternative.xp_5_delta >= 0
              ? `+${alternative.xp_5_delta.toFixed(1)} pts`
              : `${alternative.xp_5_delta.toFixed(1)} pts`;
          return `<div class="decision-row"><span>${esc(alternative.name)}<br><span class="muted">£${Number(alternative.price).toFixed(1)}m · ${Number(alternative.xp_5).toFixed(1)} xPts (5 GW)</span></span><b>${price} <span class="muted">vs this pick</span><br>${xp}</b></div>`;
        })
        .join(
          "",
        )}</div><p class="breakdown-note">These are the highest-projected ${esc(player.position_short)} players not in this squad. A higher xPts alternative that isn't selected was usually judged not worth its extra cost against the rest of the squad, or carries more minutes risk.</p>`
    : "";
}
function selectPlayerCard(player, options = {}) {
  document
    .querySelectorAll("[data-player-id].selected")
    .forEach((node) => node.classList.remove("selected"));
  document
    .querySelectorAll(`[data-player-id="${player.id}"]`)
    .forEach((node) => node.classList.add("selected"));
  renderPlayerBreakdown(player);
  const panel = byId("decision-section-breakdown");
  // Issue #242: an explicit tap on a player card/chip (options.scroll -- see
  // attachBreakdownHandlers below) opens the breakdown panel as a sheet on mobile, instead of
  // scrolling the page to an inline panel. attachBreakdownHandlers' own initial default-player
  // selection passes scroll:false specifically so it only populates the panel in place, both on
  // desktop and on mobile -- it must never pop a sheet open on page load.
  if (
    options.scroll &&
    typeof openContentSheet === "function" &&
    typeof isMobileShellBreakpoint === "function" &&
    isMobileShellBreakpoint()
  ) {
    if (panel) openContentSheet(panel, `${player.name} · ${player.position_short}`);
  } else if (options.scroll && panel) {
    panel.scrollIntoView({
      behavior: prefersReducedMotion() ? "auto" : "smooth",
      block: "nearest",
    });
  }
}
function attachBreakdownHandlers(squadPlayers, preferredDefault, rationaleMap) {
  selectedRationaleMap = rationaleMap || {};
  document.querySelectorAll("[data-player-id]").forEach((node) => {
    const id = Number(node.dataset.playerId);
    const player = (squadPlayers || []).find((row) => row.id === id);
    if (!player) return;
    node.addEventListener("click", () =>
      selectPlayerCard(player, { scroll: true }),
    );
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        selectPlayerCard(player, { scroll: true });
      }
    });
  });
  const target =
    (squadPlayers || []).find((row) => row.id === selectedBreakdownPlayerId) ||
    preferredDefault ||
    (squadPlayers || [])[0];
  if (target) selectPlayerCard(target, { scroll: false });
}
function bindTabs(containerId, selector, activate) {
  const container = byId(containerId);
  const tabs = [...container.querySelectorAll(selector)];
  tabs.forEach((tab) => {
    tab.addEventListener("click", () => activate(tab));
    tab.addEventListener("keydown", (event) => {
      if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key))
        return;
      event.preventDefault();
      const index = tabs.indexOf(tab);
      const targetIndex =
        event.key === "Home"
          ? 0
          : event.key === "End"
            ? tabs.length - 1
            : event.key === "ArrowRight"
              ? (index + 1) % tabs.length
              : (index - 1 + tabs.length) % tabs.length;
      const target = tabs[targetIndex];
      activate(target);
      const next = byId(target.id);
      if (next) next.focus();
    });
  });
}
function setupDecisionSubnav() {
  const nav = document.querySelector(".decision-subnav");
  if (!nav) return;
  const chips = [...nav.querySelectorAll("[data-scroll-to]")];
  const setActive = (chip) => {
    chips.forEach((c) => c.classList.remove("active"));
    if (chip) chip.classList.add("active");
  };
  let suppressUntil = 0;
  chips.forEach((chip) =>
    chip.addEventListener("click", () => {
      const target = byId(chip.dataset.scrollTo);
      if (!target) return;
      const collapsedAncestor = target.closest("details");
      if (collapsedAncestor) collapsedAncestor.open = true;
      setActive(chip);
      suppressUntil = Date.now() + 900;
      target.scrollIntoView({
        behavior: prefersReducedMotion() ? "auto" : "smooth",
        block: "start",
      });
    }),
  );
  if (!("IntersectionObserver" in window)) return;
  const watchedIds = [
    "decision-section-summary",
    "decision-section-weekly",
    "decision-section-profiles",
    "decision-section-xi",
    "decision-section-bench",
    "decision-section-squad",
  ];
  const sections = watchedIds.map((id) => byId(id)).filter(Boolean);
  const observer = new IntersectionObserver(
    (entries) => {
      if (Date.now() < suppressUntil) return;
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const activeChip = nav.querySelector(
          `[data-scroll-to="${entry.target.id}"]`,
        );
        if (!activeChip) return;
        setActive(activeChip);
      });
    },
    { rootMargin: "-140px 0px -70% 0px", threshold: 0 },
  );
  sections.forEach((section) => observer.observe(section));
}
function renderProfileRangeStrips(profiles, selectedId) {
  if (!profiles.length) {
    byId("profile-range-strips").innerHTML = "";
    return;
  }
  const metricsList = profiles.map((profile) => profile.metrics || {});
  const axisMin =
    Math.floor(
      Math.min(...metricsList.map((m) => Number(m.lower_5gw || 0))) / 25,
    ) * 25;
  const axisMax =
    Math.ceil(
      Math.max(...metricsList.map((m) => Number(m.upper_5gw || 0))) / 25,
    ) * 25;
  const span = Math.max(1, axisMax - axisMin);
  byId("profile-range-strips").innerHTML = profiles
    .map((profile) => {
      const m = profile.metrics || {};
      const lower = Number(m.lower_5gw || 0);
      const central = Number(m.central_5gw || 0);
      const upper = Number(m.upper_5gw || 0);
      const left = ((lower - axisMin) / span) * 100;
      const width = ((upper - lower) / span) * 100;
      const tick = ((central - axisMin) / span) * 100;
      const active = profile.id === selectedId;
      return `<div class="range-strip-row${active ? " active" : ""}"><span class="range-strip-label">${esc(profile.label)}</span><div class="range-strip-track"><div class="range-strip-fill" style="left:${left.toFixed(1)}%;width:${width.toFixed(1)}%"></div><div class="range-strip-tick" style="left:${tick.toFixed(1)}%"></div></div><span class="range-strip-value">${central.toFixed(1)}</span></div>`;
    })
    .join("");
}
function renderRotationPlan(selected, squad) {
  const panel = byId("decision-rotation-panel");
  const horizons = selected.evaluation_horizons || {};
  const horizon = horizons["5"] || horizons["3"] || horizons["1"];
  const rows = (horizon && horizon.event_lineups) || [];
  if (!rows.length) {
    panel.hidden = true;
    return;
  }
  panel.hidden = false;
  const byIdMap = new Map(
    (squad.players || []).map((player) => [player.id, player]),
  );
  const name = (id) => (byIdMap.get(id) || {}).name || "Unknown";
  const baseline = new Set(rows[0].lineup_player_ids || []);
  byId("decision-rotation").innerHTML = rows
    .map((row, index) => {
      const currentIds = new Set(row.lineup_player_ids || []);
      let changesText = "Baseline XI";
      if (index > 0) {
        const inNames = [...currentIds]
          .filter((id) => !baseline.has(id))
          .map(name);
        const outNames = [...baseline]
          .filter((id) => !currentIds.has(id))
          .map(name);
        const parts = [];
        if (inNames.length) parts.push(`In: ${inNames.join(", ")}`);
        if (outNames.length) parts.push(`Out: ${outNames.join(", ")}`);
        changesText = parts.length ? parts.join(" · ") : "Unchanged XI";
      }
      return `<div class="decision-row"><span><strong>GW${row.event} · ${esc(row.formation)}</strong><br><span class="muted">C ${esc(name(row.captain_id))} · VC ${esc(name(row.vice_captain_id))} · ${esc(changesText)}</span></span><b>${Number(row.central_points).toFixed(1)} pts<br><span class="muted">${Number(row.lower_points).toFixed(1)}–${Number(row.upper_points).toFixed(1)}</span></b></div>`;
    })
    .join("");
  byId("decision-rotation-meta").textContent =
    `Event-specific lineups · ${selected.label}`;
}
// Issue #158: the "Compare risk profiles" panel (options tabs + range strips + stat-tile
// comparison) used to be built inline inside renderDecision() from decision.profile_recommendations
// alone -- always a freshly optimized, generic squad, never personalized to a declared draft or
// real squad even once one exists. Split out so it can independently choose its data source:
// weekly.profiles (now carrying the same metrics/evaluation_horizons shape, see
// transfer_decisions.py's build_draft_decisions/build_transfer_decisions) once
// weekly.status==='active', falling back to the generic benchmark otherwise. Called from
// renderDecision() below so every existing call site keeps working unchanged.
function renderProfileComparison(profileId = null) {
  const weekly = decision.weekly_decisions || {};
  const personalized = weekly.status === "active";
  // Bug fix: decision-section-profiles lives, by default, inside <details id="decision-benchmark-
  // details"> -- fine while it only ever showed the generic benchmark, but that <details> collapses
  // shut (renderWeeklyDecision's weekly-priority demote) the moment personalized data exists, which
  // silently hid the one thing inside it that had just become personalized and relevant. Relocate
  // the actual section node itself (not a copy -- there's only ever one) into the always-visible
  // "Personalized weekly decision" section when personalized, and back to its usual spot inside the
  // benchmark details otherwise. Safe to move: every listener inside it is rebound fresh on each
  // render via bindTabs, nothing here depends on the node's position remaining stable, and
  // IntersectionObserver/scrollIntoView track the element by reference, not by DOM location.
  const profilesSection = byId("decision-section-profiles");
  const weeklyMount = byId("weekly-profile-comparison-mount");
  const homeAnchor = byId("decision-section-profiles-home");
  if (personalized && weeklyMount) weeklyMount.appendChild(profilesSection);
  else if (homeAnchor) homeAnchor.after(profilesSection);
  const benchmarkProfiles = decision.profile_recommendations || [];
  const legacy = {
    id: "balanced",
    label: "Balanced",
    summary: "Central projection baseline",
    risk_note: "Preliminary model uncertainty.",
    objective: "Central five-gameweek projection",
    metrics: {},
  };
  const availableProfiles = personalized
    ? weekly.profiles || []
    : benchmarkProfiles.length
      ? benchmarkProfiles
      : [legacy];
  byId("profile-comparison-heading").textContent = personalized
    ? "Compare risk profiles for your squad"
    : "Compare risk profiles";
  byId("profile-comparison-subtitle").textContent = personalized
    ? "Same 15 players throughout -- switching profiles changes captaincy and rotation assumptions, not who’s in your squad."
    : "Select a team to update the XI, bench, captaincy, and full squad below.";
  if (!availableProfiles.length) {
    byId("profile-options").innerHTML = "";
    byId("profile-range-strips").innerHTML = "";
    byId("profile-comparison").innerHTML =
      '<div class="empty">No risk-profile comparison is available.</div>';
    return;
  }
  const selectedId =
    profileId ||
    (personalized ? weekly.default_profile : decision.default_profile) ||
    "balanced";
  const selected =
    availableProfiles.find((row) => row.id === selectedId) ||
    availableProfiles[0];
  const metrics = selected.metrics || {};
  byId("profile-options").innerHTML = availableProfiles
    .map((profile) => {
      const profileMetrics = profile.metrics || {};
      return `<button id="profile-tab-${esc(profile.id)}" type="button" role="tab" aria-selected="${profile.id === selected.id}" aria-controls="profile-panel" tabindex="${profile.id === selected.id ? "0" : "-1"}" class="profile-option ${profile.id === selected.id ? "active" : ""}" data-profile="${esc(profile.id)}"><strong>${esc(profile.label)}</strong><span>${esc(profile.summary)}</span><span>1 / 3 / 5 GW central · ${Number(profileMetrics.central_1gw || 0).toFixed(1)} / ${Number(profileMetrics.central_3gw || 0).toFixed(1)} / ${Number(profileMetrics.central_5gw || 0).toFixed(1)} xPts</span><span>5-GW range ${Number(profileMetrics.lower_5gw || 0).toFixed(1)}–${Number(profileMetrics.upper_5gw || 0).toFixed(1)}</span></button>`;
    })
    .join("");
  byId("profile-panel").setAttribute(
    "aria-labelledby",
    `profile-tab-${selected.id}`,
  );
  bindTabs("profile-options", "[data-profile]", (button) => {
    renderDecision(button.dataset.profile);
    renderWeeklyDecision(button.dataset.profile);
  });
  renderProfileRangeStrips(availableProfiles, selected.id);
  // Personalized case: squad membership is identical across all three profiles (only captaincy/
  // rotation assumptions vary), so the benchmark's "changed players in/out" sentence would always
  // report zero changes -- replaced with a captaincy delta, sourced from each profile's own "roll"
  // scenario (the visitor's declared squad as-is, never a post-transfer squad) for player names.
  let explanationText;
  if (personalized) {
    const rollSquads = availableProfiles.map((profile) => {
      const roll = (profile.scenarios || []).find(
        (row) => row.action === "roll",
      );
      return (roll && roll.squad) || [];
    });
    const nameFor = (id) => {
      for (const squadRow of rollSquads) {
        const found = squadRow.find((player) => player.id === id);
        if (found) return found.name;
      }
      return "Unknown";
    };
    const captainByProfile = availableProfiles.map((profile) => ({
      label: profile.label,
      captainId: ((profile.evaluation_horizons || {})["1"] || {}).captain_id,
    }));
    const distinctCaptainIds = [
      ...new Set(
        captainByProfile.map((row) => row.captainId).filter((id) => id != null),
      ),
    ];
    explanationText =
      distinctCaptainIds.length <= 1
        ? `Same captain and lineup across all three profiles for Gameweek ${weekly.event || ""}.`
        : captainByProfile
            .map(
              (row) => `${row.label} captains ${esc(nameFor(row.captainId))}`,
            )
            .join("; ") + ".";
  } else {
    const comparison = selected.comparison_to_balanced || {};
    const changed = comparison.changed_players || {};
    const incoming = Array.isArray(changed) ? changed : changed.in || [];
    const outgoing = Array.isArray(changed) ? [] : changed.out || [];
    explanationText =
      selected.id === "balanced"
        ? "Default central-projection reference team."
        : `${comparison.shared_players ?? 0}/15 players shared with Balanced. ${incoming.length ? `In: ${incoming.join(", ")}. ` : ""}${outgoing.length ? `Out: ${outgoing.join(", ")}.` : ""}`;
  }
  byId("profile-comparison").innerHTML =
    `<div class="profile-stat"><b>${Number(metrics.central_1gw || 0).toFixed(1)}</b><span>1-GW modeled xPts</span></div><div class="profile-stat"><b>${Number(metrics.central_3gw || 0).toFixed(1)}</b><span>3-GW modeled xPts</span></div><div class="profile-stat"><b>${Number(metrics.central_5gw || 0).toFixed(1)}</b><span>5-GW modeled xPts</span></div><div class="profile-stat"><b>${Number(metrics.average_expected_minutes || 0).toFixed(0)}</b><span>Average expected minutes</span></div><div class="profile-stat"><b>${Number(metrics.average_ownership || 0).toFixed(1)}%</b><span>Average ownership</span></div><div class="profile-stat"><b>${Number(metrics.low_confidence_players || 0)}</b><span>Low-confidence players</span></div><div class="profile-explanation"><strong>${esc(selected.objective || selected.summary)}</strong><br><span class="muted">5-GW uncertainty range: ${Number(metrics.lower_5gw || 0).toFixed(1)}–${Number(metrics.upper_5gw || 0).toFixed(1)} xPts.</span><br>${esc(explanationText)}${selected.risk_note ? ` <span class="status-wait">Main trade-off: ${esc(selected.risk_note)}</span>` : ""}</div>`;
}
function renderDecision(profileId = null) {
  renderProfileComparison(profileId);
  const profiles = decision.profile_recommendations || [];
  const legacy = {
    id: "balanced",
    label: "Balanced",
    summary: "Central projection baseline",
    risk_note: "Preliminary model uncertainty.",
    objective: "Central five-gameweek projection",
    squad: decision.recommended_squad,
    captaincy: decision.captaincy || [],
    metrics: {},
  };
  const availableProfiles = profiles.length ? profiles : [legacy];
  const selectedId = profileId || decision.default_profile || "balanced";
  const selected =
    availableProfiles.find((row) => row.id === selectedId) ||
    availableProfiles[0];
  const active =
    decision.status === "active_preliminary" && selected && selected.squad;
  if (!active) {
    byId("decision-status").className = "status-wait";
    byId("decision-status").textContent = "Recommendation unavailable";
    byId("decision-summary").innerHTML =
      `<div class="empty">${esc(decision.reason || "Projection and optimization inputs are incomplete.")}</div>`;
    byId("decision-rotation-panel").hidden = true;
    [
      "recommended-xi",
      "recommended-bench",
      "captaincy-list",
      "decision-model",
      "recommended-squad",
      "decision-watchlist",
      "player-breakdown-table",
    ].forEach(
      (id) =>
        (byId(id).innerHTML =
          '<div class="empty">No modeled recommendation is available.</div>'),
    );
    byId("player-breakdown-name").textContent =
      "Select a player above to inspect its projection";
    return;
  }
  const squad = selected.squad;
  const captainId = squad.captain && squad.captain.id;
  const viceId = squad.vice_captain && squad.vice_captain.id;
  const card = (player, index = null) => {
    const role =
      player.id === captainId ? " (C)" : player.id === viceId ? " (VC)" : "";
    const reserve =
      player.position_short === "GKP" && index !== null
        ? "Reserve goalkeeper · "
        : index === null
          ? ""
          : `Bench ${index + 1} · `;
    return `<div class="recommendation-card ${player.id === captainId ? "captain" : ""}" data-player-id="${player.id}" tabindex="0" role="button" aria-label="Inspect ${esc(player.name)}'s scoring breakdown"><strong>${esc(player.name)}${role}</strong><span>${esc(player.position_short)} · ${esc(player.club)} · £${Number(player.price).toFixed(1)}m</span><span>${reserve}${Number(player.expected_minutes).toFixed(0)} expected min · ${esc(player.confidence)} confidence</span><span class="projection">${Number(player.xp_1).toFixed(1)} / ${Number(player.xp_3).toFixed(1)} / ${Number(player.xp_5).toFixed(1)} xPts</span><span>5-GW range ${Number(player.lower_5).toFixed(1)}–${Number(player.upper_5).toFixed(1)}</span></div>`;
  };
  const pitch = (lineup) =>
    ["FWD", "MID", "DEF", "GKP"]
      .map((position) => {
        const row = (lineup || []).filter(
          (player) => player.position_short === position,
        );
        return `<div class="pitch-row pitch-${position.toLowerCase()}">${row
          .map((player) => {
            const role =
              player.id === captainId ? "C" : player.id === viceId ? "VC" : "";
            return `<div class="pitch-player ${player.id === captainId ? "captain" : ""}" data-player-id="${player.id}" tabindex="0" role="button" title="${esc(player.name)} · ${esc(player.club)} · ${Number(player.expected_minutes).toFixed(0)} expected minutes"><strong>${esc(player.name)}${role ? ` (${role})` : ""}</strong><span>${esc(player.club)}</span><span class="projection projection-full">${Number(player.xp_1).toFixed(1)} / ${Number(player.xp_3).toFixed(1)} / ${Number(player.xp_5).toFixed(1)}</span><span class="projection projection-compact">${Number(player.xp_1).toFixed(1)} xPts</span></div>`;
          })
          .join("")}</div>`;
      })
      .join("");
  byId("decision-eyebrow").textContent =
    `Preliminary GW${decision.event || 1} benchmark`;
  byId("decision-heading").textContent =
    decision.event === 1 ? "Opening-squad decision" : "Fresh-squad benchmark";
  byId("decision-context").textContent =
    decision.event === 1
      ? "This is a reproducible preseason baseline, not a guarantee. Your unpublished draft is not inferred."
      : "This benchmark shows the best fresh squad for comparison; use the personalized weekly decision below for actual transfers.";
  byId("recommended-xi-heading").textContent =
    `Recommended GW${decision.event || 1} XI`;
  byId("decision-status").className = "status-good";
  byId("decision-status").textContent = `Active · ${selected.label} profile`;
  byId("decision-summary").innerHTML =
    `<div class="decision-metric"><b>£${Number(squad.cost).toFixed(1)}m</b><span>Squad cost</span></div><div class="decision-metric"><b>£${Number(squad.money_remaining).toFixed(1)}m</b><span>Money remaining</span></div><div class="decision-metric"><b>${Number(squad.projected_event_points_including_captain ?? squad.projected_gw1_points_including_captain).toFixed(1)}</b><span>Modeled GW${decision.event || 1} points, captain included</span></div><div class="decision-metric"><b>${esc(squad.captain.name)}</b><span>Captain · ${Number(squad.captain.xp_1).toFixed(1)} xPts before doubling</span></div><div class="decision-metric"><b>${esc(squad.vice_captain.name)}</b><span>Vice-captain</span></div>`;
  byId("recommended-formation").textContent =
    `${esc(squad.formation)} · ${esc(selected.label)} · xPts shown for 1 / 3 / 5 GWs`;
  byId("recommended-xi").setAttribute(
    "aria-label",
    `${squad.formation} recommended formation: ${(squad.starting_xi || []).map((player) => `${player.name}${player.id === captainId ? " captain" : player.id === viceId ? " vice-captain" : ""}`).join(", ")}`,
  );
  byId("recommended-xi").innerHTML = pitch(squad.starting_xi || []);
  byId("recommended-bench").innerHTML = (squad.bench || [])
    .map((player, index) => card(player, index))
    .join("");
  byId("recommended-squad").innerHTML = (squad.players || [])
    .map((player) => card(player))
    .join("");
  renderRotationPlan(selected, squad);
  byId("captaincy-list").innerHTML = (
    selected.captaincy ||
    decision.captaincy ||
    []
  )
    .map(
      (player, index) =>
        `<div class="decision-row"><span><strong>${index + 1}. ${esc(player.name)}</strong><br><span class="muted">${esc(player.club)} · ${Number(player.expected_minutes).toFixed(0)} expected min</span></span><b>${Number(player.xp_1).toFixed(1)} xPts</b></div>`,
    )
    .join("");
  const model = decision.model || {};
  const inputs = (model.inputs || [])
    .map(
      (item) =>
        `<div class="decision-note"><strong>Input</strong><br>${esc(item)}</div>`,
    )
    .join("");
  const limits = (model.limitations || [])
    .map(
      (item) =>
        `<div class="decision-note"><strong>Risk</strong><br>${esc(item)}</div>`,
    )
    .join("");
  byId("decision-model").innerHTML =
    `<div class="model-summary-grid"><div class="decision-note"><strong>${esc(model.name || "Projection model")} v${esc(model.version || "")}</strong><br>No betting odds. Generated ${esc(fmtDate(decision.generated_at, true))}.</div><div class="decision-note"><strong>${esc(selected.label)} objective</strong><br>${esc(selected.objective || selected.summary)}</div></div><details class="model-disclosure"><summary>Show model inputs and risks (${(model.inputs || []).length} inputs · ${(model.limitations || []).length} risks)</summary><div class="model-detail-grid">${inputs}${limits}</div></details>`;
  const squadIds = new Set((squad.players || []).map((player) => player.id));
  const watchlistData = decision.watchlist || {};
  const watchlistPlayers = [];
  const watchlistGroups = ["GKP", "DEF", "MID", "FWD"]
    .map((position) => {
      const entries = (watchlistData[position] || []).filter(
        (player) => !squadIds.has(player.id),
      );
      if (!entries.length) return "";
      watchlistPlayers.push(...entries);
      return `<div class="watchlist-group"><h3>${esc(position)}</h3><div class="recommendation-grid compact">${entries.map((player) => card(player)).join("")}</div></div>`;
    })
    .join("");
  byId("decision-watchlist").innerHTML =
    watchlistGroups ||
    '<div class="empty">Every top-projected option is already in this squad.</div>';
  attachBreakdownHandlers(
    squad.players.concat(watchlistPlayers),
    squad.captain,
    squad.selection_rationale,
  );
}
function updateDraftLock(weekly) {
  const note = byId("draft-locked-note");
  const editor = byId("draft-squad-editor");
  if (!note || !editor) return;
  const locked = weekly.status === "active" && !weekly.draft;
  editor.hidden = locked;
  note.hidden = !locked;
  if (locked)
    note.innerHTML =
      state.profile && state.profile.draft_squad
        ? "<strong>Your draft squad is no longer active</strong>Your real published squad is now driving recommendations, so the draft you declared above is no longer used."
        : "<strong>Draft squad declarations are preseason-only</strong>Your real published squad is now available, so a declared draft is no longer needed.";
}
// Issue #270: Wildcard/Free Hit evaluate a from-scratch, unconstrained squad rebuild that shares
// as few as 0 of the manager's real 15 players -- Bench Boost/Triple Captain (and the ordinary
// roll/transfer scenarios) evaluate the manager's real squad, incrementally or as-is. All of these
// used to render in the same visual language with nothing distinguishing which basis produced a
// given number; this is the shared wording for both the pitch view and the chip panel below.
function squadBasisLabel(chipName) {
  if (chipName === "wildcard")
    return "from-scratch rebuild: permanently replaces your entire squad";
  if (chipName === "freehit")
    return "from-scratch rebuild: replaces your squad for this gameweek only, then reverts";
  if (chipName === "bboost")
    return "your real squad as-is: scores your actual bench, no rebuild";
  if (chipName === "3xc")
    return "your real squad as-is: triples your actual captain's points, no rebuild";
  return "incremental: built from your real squad";
}
// Issue #272: renders one lineup (the recommendation, or any scenario a manager clicks to
// preview) onto the shared weekly-pitch/weekly-bench elements. `isPreview` swaps the heading for
// "Previewing: <label>" and reveals the "Back to recommendation" reset button -- the recommended
// lineup itself is never labeled as a preview, even when its own card is the one clicked.
// Issue #270: the meta line always names the lineup's basis (from-scratch rebuild vs incremental
// from the real squad) -- `lineupSource.chip` is only set on the object built by
// _exclusive_chip_scenario (transfer_decisions.py), never on an ordinary roll/transfer scenario.
function renderWeeklyLineup(lineupSource, headingText, metaLabel, isPreview) {
  const captainId = lineupSource.captain && lineupSource.captain.id;
  const viceId = lineupSource.vice_captain && lineupSource.vice_captain.id;
  const pitchHtml = ["FWD", "MID", "DEF", "GKP"]
    .map((position) => {
      const row = (lineupSource.starting_xi || []).filter(
        (player) => player.position_short === position,
      );
      return `<div class="pitch-row pitch-${position.toLowerCase()}">${row
        .map((player) => {
          const role =
            player.id === captainId ? "C" : player.id === viceId ? "VC" : "";
          return `<div class="pitch-player ${player.id === captainId ? "captain" : ""}" title="${esc(player.name)} · ${esc(player.club)}"><strong>${esc(player.name)}${role ? ` (${role})` : ""}</strong><span>${esc(player.club)}</span><span class="projection projection-full">${Number(player.xp_1).toFixed(1)} / ${Number(player.xp_3).toFixed(1)} / ${Number(player.xp_5).toFixed(1)}</span><span class="projection projection-compact">${Number(player.xp_1).toFixed(1)} xPts</span></div>`;
        })
        .join("")}</div>`;
    })
    .join("");
  byId("weekly-lineup").hidden = false;
  byId("weekly-lineup-heading").textContent = headingText;
  byId("weekly-lineup-meta").textContent =
    `${metaLabel} · ${squadBasisLabel(lineupSource.chip)}`;
  byId("weekly-lineup-reset").hidden = !isPreview;
  byId("weekly-pitch").setAttribute(
    "aria-label",
    `${lineupSource.formation} formation: ${(lineupSource.starting_xi || []).map((player) => `${player.name}${player.id === captainId ? " captain" : player.id === viceId ? " vice-captain" : ""}`).join(", ")}`,
  );
  byId("weekly-pitch").innerHTML = pitchHtml;
  byId("weekly-bench").innerHTML = (lineupSource.bench || [])
    .map(
      (player, index) =>
        `<div class="weekly-bench-card"><strong>${index + 1}. ${esc(player.name)}</strong><span>${esc(player.position_short)} · ${esc(player.club)}</span><span>${Number(player.xp_1).toFixed(1)} / ${Number(player.xp_3).toFixed(1)} / ${Number(player.xp_5).toFixed(1)} xPts</span></div>`,
    )
    .join("");
}
// Issue 272 (found while testing, pre-existing): `action` alone does not uniquely identify a
// scenario -- every multi-leg scenario (3, 4, and 5 transfers) shares the same "multi_transfer"
// action, differing only by transfer_count. The original `scenario.action === recommendation.
// action` check (used for the "recommended" card highlight) silently matched *all* multi-leg
// cards whenever a multi-leg scenario won, not just the actual recommended one -- confirmed live:
// with a 5-transfer recommendation, the 3-, 4-, and 5-transfer cards all lit up as "recommended"
// simultaneously. transfer_count is unique across the whole scenarios list (roll=0, single=1,
// double=2, and each multi-leg count is distinct), so matching on both together is exact.
function isSameScenario(a, b) {
  return !!a && !!b && a.action === b.action && a.transfer_count === b.transfer_count;
}
// Issue #272: wires each already-rendered .scenario-card up to preview that scenario's own XI
// on click (mirrors attachBreakdownHandlers' re-attach-after-every-render pattern above --
// weekly-scenarios' innerHTML is fully replaced on every renderWeeklyDecision call, so listeners
// attached here are never leaked, just re-created against the fresh nodes each time).
function attachScenarioCardHandlers(scenarios, recommendation, recommendationLabel, weekly, profileLabel) {
  // Issue #270: play_wildcard/play_freehit are the only other action strings a scenario object
  // can carry (_exclusive_chip_scenario, transfer_decisions.py) -- without these, labelFor fell
  // through to the raw action string whenever `recommendation` itself was a chip rebuild.
  const actionLabels = {
    roll: "Roll the transfer",
    single_transfer: "Make one transfer",
    double_transfer: "Make two transfers",
    play_wildcard: "Wildcard",
    play_freehit: "Free Hit",
  };
  const labelFor = (action, count) =>
    action === "multi_transfer"
      ? `Make ${count} transfers`
      : actionLabels[action] || action;
  const showRecommendation = () =>
    renderWeeklyLineup(
      recommendation,
      `Recommended GW${weekly.event} XI · ${recommendationLabel}`,
      `${recommendation.formation} · ${profileLabel}`,
      false,
    );
  const showScenario = (scenario) =>
    renderWeeklyLineup(
      scenario,
      `Previewing: ${labelFor(scenario.action, scenario.transfer_count)}`,
      `${scenario.formation} · ${profileLabel} · not the recommended action`,
      true,
    );
  const cards = [...document.querySelectorAll("#weekly-scenarios [data-scenario-index]")];
  const setActiveCard = (activeNode) => {
    cards.forEach((node) => node.classList.toggle("previewing", node === activeNode));
  };
  cards.forEach((node) => {
    const scenario = scenarios[Number(node.dataset.scenarioIndex)];
    if (!scenario) return;
    const select = () => {
      setActiveCard(node);
      if (isSameScenario(scenario, recommendation)) showRecommendation();
      else showScenario(scenario);
    };
    node.addEventListener("click", select);
    node.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        select();
      }
    });
    if (isSameScenario(scenario, recommendation)) setActiveCard(node);
  });
  byId("weekly-lineup-reset").onclick = () => {
    const recommendedCard = cards.find((node) =>
      isSameScenario(scenarios[Number(node.dataset.scenarioIndex)], recommendation),
    );
    if (recommendedCard) setActiveCard(recommendedCard);
    showRecommendation();
  };
}
function renderWeeklyDecision(profileId = null) {
  const weekly = decision.weekly_decisions || {};
  updateDraftLock(weekly);
  // Decision Center reorganization: whenever this section has a real, personalized recommendation
  // to show -- `weekly.status==='active'`, true both for a declared preseason draft (`weekly.draft`)
  // and for a real published squad once the season is under way -- the generic fresh-squad
  // benchmark below (summary + risk profiles + XI/captaincy + bench/model + squad detail, all one
  // unit: none of it is personalized) is no longer the most useful thing on the page. It collapses
  // into a single labeled reference and this section (plus the jump nav) moves ahead of it via
  // `.weekly-priority`'s flex `order` overrides (dashboard.css). NOT gated on `weekly.draft` alone
  // -- that flag goes false again post-GW1 even though a real squad's active recommendation is
  // exactly as much "more relevant than the generic benchmark" as the draft case was; only the
  // label text below still distinguishes "draft" from "real squad" phrasing.
  const weeklyPersonalized = weekly.status === "active";
  byId("decisions-content").classList.toggle(
    "weekly-priority",
    weeklyPersonalized,
  );
  const benchmarkDetails = byId("decision-benchmark-details");
  if (benchmarkDetails) {
    benchmarkDetails.open = !weeklyPersonalized;
    const benchmarkLabel = byId("decision-benchmark-details-label");
    if (benchmarkLabel)
      benchmarkLabel.textContent = !weeklyPersonalized
        ? "Preliminary recommendation"
        : weekly.draft
          ? "Reference: from-scratch squad (ignores your draft)"
          : "Reference: from-scratch squad (see your weekly decision above)";
  }
  const ids = [
    "weekly-summary",
    "weekly-recommendation",
    "weekly-scenarios",
    "weekly-chip",
    "weekly-rules",
  ];
  if (weekly.status !== "active") {
    byId("decision-section-weekly").classList.add("collapsed");
    byId("weekly-plan").hidden = true;
    byId("weekly-branches").innerHTML = "";
    byId("weekly-lineup").hidden = true;
    byId("weekly-profile-panel").hidden = true;
    byId("weekly-status").className = "status-wait";
    byId("weekly-status").textContent =
      weekly.status === "waiting_for_gw2"
        ? "Starts in Gameweek 2"
        : weekly.status === "manager_not_configured"
          ? "FPL team not connected"
          : weekly.status === "team_not_found"
            ? "Team not found"
            : weekly.status === "draft_squad_invalid"
              ? "Draft squad needs fixing"
              : "Published squad required";
    byId("weekly-inactive-reason").hidden = false;
    byId("weekly-inactive-reason").textContent =
      weekly.reason ||
      "Weekly recommendations activate after the first published squad is available.";
    byId("weekly-summary").innerHTML = "";
    byId("weekly-recommendation").innerHTML = "";
    byId("weekly-scenarios").innerHTML = "";
    byId("weekly-chip").innerHTML = "";
    byId("weekly-rules").innerHTML = "";
    return;
  }
  const profiles = weekly.profiles || [];
  const selectedId = profileId || weekly.default_profile || "balanced";
  const selected = profiles.find((row) => row.id === selectedId) || profiles[0];
  if (!selected) return;
  const recommendation = selected.recommendation;
  const plan = selected.multiweek_plan || {};
  // Issue #270 (bug fix): recommendation.action is "play_wildcard"/"play_freehit" whenever a
  // rebuild chip wins (_exclusive_chip_scenario, transfer_decisions.py) -- these weren't in this
  // map, so labelFor fell through to the raw action string and the pitch heading literally read
  // "Recommended GW<N> XI · play_wildcard" instead of "· Wildcard".
  const actionLabels = {
    roll: "Roll the transfer",
    single_transfer: "Make one transfer",
    double_transfer: "Make two transfers",
    play_wildcard: "Wildcard",
    play_freehit: "Free Hit",
  };
  const labelFor = (action, count) =>
    action === "multi_transfer"
      ? `Make ${count} transfers`
      : actionLabels[action] || action;
  const actionLabel = labelFor(
    recommendation.action,
    recommendation.transfer_count,
  );
  // Issue #270 (bug fix, found via live user feedback): recommendation.transfers is always []
  // for a chip play (_exclusive_chip_scenario, transfer_decisions.py sets "transfers": []
  // unconditionally -- a rebuild isn't itemized as swaps), which used to fall through to the
  // *same* "No transfer / Preserve the free transfer for the next deadline" fallback used for a
  // genuine roll. That's actively wrong information here -- your whole squad is being rebuilt,
  // not preserved. It also duplicated the chip's own reason sentence verbatim in two separate
  // cards (recommendation.reason is chip.reason, copied as-is by _exclusive_chip_scenario), so a
  // manager saw "Wildcard clears the balanced marginal-value threshold by 0.4 points." printed
  // twice on the same page. Both are fixed together here: recognize a chip play via
  // recommendation.chip and give it its own, non-duplicated summary instead of reusing either
  // the ordinary-transfer reason text or the no-transfer fallback.
  const isChipPlay = !!recommendation.chip;
  const chipPlaySummary = isChipPlay
    ? `<p>Full squad rebuild -- not itemized transfers. ${recommendation.reverts_after_event ? "Reverts to your real squad after this gameweek." : "Persists as your squad going forward."} See the chip panel below for why ${esc(recommendation.label)} was recommended.</p>`
    : `<p>${esc(recommendation.reason)}</p>`;
  const transferPairs = isChipPlay
    ? ""
    : (recommendation.transfers || [])
        .map(
          (move) =>
            `<div class="transfer-pair"><strong>${esc(move.out.name)} → ${esc(move.in.name)}</strong><span class="muted">Sell £${Number(move.out.selling_price).toFixed(1)}m · buy £${Number(move.in.price).toFixed(1)}m</span></div>`,
        )
        .join("") ||
      '<div class="transfer-pair"><strong>No transfer</strong><span class="muted">Preserve the free transfer for the next deadline.</span></div>';
  byId("decision-section-weekly").classList.remove("collapsed");
  byId("weekly-profile-panel").hidden = false;
  byId("weekly-inactive-reason").hidden = true;
  byId("weekly-heading").textContent = weekly.draft
    ? `Feedback on your declared Gameweek ${weekly.event} draft squad`
    : `Gameweek ${weekly.event} roll, transfer, and chip decision`;
  byId("weekly-status").className = "status-good";
  byId("weekly-status").textContent = weekly.draft
    ? `Draft feedback · ${selected.label}`
    : `Active · ${selected.label}`;
  // Bug fix: this section used to build its own separate Conservative/Balanced/Aggressive tab
  // strip here (id weekly-profile-options) -- a second, independent profile selector stacked
  // directly below the rich renderProfileComparison() panel above it (relocated into this same
  // section once personalized, see issue #158/#162), both driving the same three profiles.
  // renderProfileComparison()'s own tabs already call renderWeeklyDecision(profileId) on click
  // (and this function's own callers already pass a profileId through), so that panel already
  // fully serves as the selector for this section too -- removed the duplicate here rather than
  // keep two controls for one choice. weekly-profile-panel now points its aria-labelledby at the
  // surviving tab strip's button id instead of one that no longer exists.
  byId("weekly-profile-panel").setAttribute(
    "aria-labelledby",
    `profile-tab-${selected.id}`,
  );
  byId("weekly-summary").innerHTML = weekly.draft
    ? `<div class="decision-metric"><b>No cost</b><span>Every suggested change before Gameweek 1</span></div><div class="decision-metric"><b>£${Number(weekly.bank || 0).toFixed(1)}m</b><span>Unspent budget in your declared draft</span></div><div class="decision-metric"><b>£${Number(recommendation.bank_after).toFixed(1)}m</b><span>Bank after suggested change</span></div><div class="decision-metric"><b>${Number(recommendation.net_gain_5gw).toFixed(1)}</b><span>5-GW gain vs your declared draft</span></div>`
    : `<div class="decision-metric"><b>${weekly.free_transfers}</b><span>Free transfer${weekly.free_transfers === 1 ? "" : "s"} now · ${weekly.free_transfer_source === "confirmed_local" ? "confirmed locally" : "estimated from public history"}</span></div><div class="decision-metric"><b>${recommendation.free_transfers_next_event}</b><span>Available next GW</span></div><div class="decision-metric"><b>${Number(plan.five_gameweek_advantage_over_roll || 0).toFixed(1)}</b><span>5-GW planner edge over roll</span></div><div class="decision-metric"><b>${recommendation.point_cost ? `−${recommendation.point_cost}` : "0"}</b><span>Immediate transfer cost</span></div><div class="decision-metric"><b>£${Number(recommendation.bank_after).toFixed(1)}m</b><span>Bank after decision</span></div>`;
  byId("weekly-recommendation").innerHTML =
    `<strong>${esc(actionLabel)}</strong>${chipPlaySummary}${transferPairs}<span class="muted">Captain ${esc(recommendation.captain.name)} · vice-captain ${esc(recommendation.vice_captain.name)} · ${esc(recommendation.formation)} · ${Number(recommendation.projected_event_points_including_captain).toFixed(1)} modeled GW${weekly.event} points</span>`;
  // Issue #266: "what changed since last week" -- compares this profile's live recommendation
  // against what the most recent earlier checkpoint's own conditional_branches already
  // anticipated for this same gameweek (build_team_plan_diff, model_performance.py). Only
  // rendered when there's something to say; a profile with no matching prior checkpoint (a
  // cron-gap-missed deadline, or simply the first time this gameweek has ever been planned)
  // shows nothing here, never a fabricated "nothing changed."
  const diffEntry = (weekly.plan_diff?.profiles || []).find((row) => row.profile_id === selected.id);
  const diffNotes = [];
  if (diffEntry) {
    if (diffEntry.action_changed) {
      diffNotes.push(
        `Last week's plan expected ${esc(labelFor(diffEntry.prior_action, recommendation.transfer_count))} here; this week recommends ${esc(labelFor(diffEntry.current_action, recommendation.transfer_count))} instead.`,
      );
    }
    if (diffEntry.chip_now_recommended) {
      diffNotes.push(
        diffEntry.chip_signal_was_flagged
          ? `This chip signal for Gameweek ${weekly.event} was already flagged last week.`
          : `This chip signal for Gameweek ${weekly.event} is new since last week.`,
      );
    }
  }
  byId("weekly-plan-diff").hidden = diffNotes.length === 0;
  byId("weekly-plan-diff").innerHTML = diffNotes.map((note) => `<div>${note}</div>`).join("");
  byId("weekly-scenarios").innerHTML = (selected.scenarios || [])
    .map((scenario, index) => {
      const planned = (plan.alternatives || []).find(
        (item) => item.action === scenario.action,
      );
      const edgeLine = weekly.draft
        ? ""
        : scenario.action === "multi_transfer"
          ? '<span class="projection muted">Not evaluated by the 5-GW planner</span>'
          : `<span class="projection">${Number((planned || {}).five_gameweek_delta_vs_roll || 0).toFixed(1)} planner edge vs roll</span>`;
      const nextFtText = weekly.draft
        ? ""
        : ` · ${scenario.free_transfers_next_event} FT next GW`;
      // Issue #271: name the actual players, not just a count -- "make one transfer" alone gave
      // no basis to judge the recommendation against. scenario.transfers already carries this
      // (_move_record, transfer_decisions.py); it just wasn't rendered here before.
      const transferNames = (scenario.transfers || [])
        .map((move) => `<span>${esc(move.out.name)} → ${esc(move.in.name)}</span>`)
        .join("");
      const cardLabel = labelFor(scenario.action, scenario.transfer_count);
      return `<div class="scenario-card ${isSameScenario(scenario, recommendation) ? "recommended" : ""}" data-scenario-index="${index}" role="button" tabindex="0" aria-label="Preview the starting XI for: ${esc(cardLabel)}"><strong>${esc(cardLabel)}</strong><span class="previewing-badge">Previewing</span>${transferNames}<span>${scenario.transfer_count} transfer${scenario.transfer_count === 1 ? "" : "s"} · ${scenario.point_cost ? `−${scenario.point_cost} hit` : "no hit"}</span>${edgeLine}<span>${Number(scenario.net_gain_5gw).toFixed(1)} direct 5-GW net · £${Number(scenario.bank_after).toFixed(1)}m bank${nextFtText}</span></div>`;
    })
    .join("");
  const branches = plan.conditional_branches || [];
  byId("weekly-plan").hidden = !plan.planning_method;
  if (plan.planning_method) {
    byId("weekly-plan-confidence").textContent =
      `${esc(plan.confidence || "low")} confidence`;
    byId("weekly-plan-summary").innerHTML =
      `<div class="weekly-plan-stat"><b>${Number(plan.five_gameweek_advantage_over_roll || 0).toFixed(1)}</b><span>5-GW advantage over roll</span></div><div class="weekly-plan-stat"><b>${Number(plan.roll_option_value || 0).toFixed(1)}</b><span>Modeled value of the extra rolled transfer</span></div><div class="weekly-plan-stat"><b>${esc((plan.horizon_events || []).map((event) => `GW${event}`).join("–"))}</b><span>Receding planning horizon</span></div>`;
    byId("weekly-branches").innerHTML =
      branches
        .map((branch) => {
          const branchLabel = actionLabels[branch.action] || branch.action;
          // Issue #266: chip_signal has been computed since #256 (_conditional_branches,
          // transfer_decisions.py) but was never rendered here -- confirmed by grepping every
          // dashboard JS file for the field before this fix. Surfacing it here is also what makes
          // the "this chip signal was already flagged last week" week-over-week note meaningful:
          // without it, "already flagged" would refer to a signal a manager was never shown.
          const chipSignalNote = branch.chip_signal
            ? `<span class="status-wait">${esc(branch.chip_signal)}</span>`
            : "";
          return `<div class="conditional-branch"><strong>GW${branch.event}: ${esc(branchLabel)} · provisional</strong><span>${esc(branch.condition)}</span><span>${branch.point_cost ? `Potential −${branch.point_cost} hit · ` : ""}${branch.free_transfers_before} FT before · ${branch.free_transfers_next_event} FT next</span>${chipSignalNote}</div>`;
        })
        .join("") ||
      '<div class="empty">No future action clears the current hold path. Recalculate after the next explicit refresh.</div>';
    byId("weekly-plan-assumptions").innerHTML = (plan.assumptions || [])
      .map((item) => `<div class="decision-note">${esc(item)}</div>`)
      .join("");
  }
  // Issue #272: the pitch below used to only ever render `recommendation`'s own XI -- extracted
  // into a standalone function so a click on any scenario card (see attachScenarioCardHandlers
  // below) can render *that* scenario's XI instead, reusing the exact same markup/aria-label
  // logic rather than duplicating it. Every scenario already carries the same starting_xi/bench/
  // captain/vice_captain/formation shape as `recommendation` (both come from _lineup_view via
  // _scenario(), transfer_decisions.py) -- this only changes which one gets displayed.
  renderWeeklyLineup(
    recommendation,
    `Recommended GW${weekly.event} XI · ${actionLabel}`,
    `${recommendation.formation} · ${selected.label}`,
    false,
  );
  attachScenarioCardHandlers(selected.scenarios || [], recommendation, actionLabel, weekly, selected.label);
  const chip = selected.chip_recommendation || {};
  // Issue #256 (part A2): Wildcard's marginal_value is a 5-GW cumulative delta (it permanently
  // rebuilds the squad) while Free Hit/Bench Boost/Triple Captain's is a single-GW delta (their
  // effect reverts after one gameweek) -- transfer_decisions.py already tags every chip candidate
  // with its own horizon (1 or 5), but this panel used to print all four marginal_value numbers
  // side by side with no indication they're on different scales. Label each one explicitly rather
  // than let a 5-GW total and a 1-GW total read as directly comparable.
  const chipHorizonLabel = (horizon) =>
    horizon === 5 ? "cumulative, next 5 GWs" : "this gameweek only";
  const pickedHorizonLabel =
    chip.action === "play" && chip.horizon
      ? ` <span class="muted">(${chipHorizonLabel(chip.horizon)})</span>`
      : "";
  // Issue #270: disclose what kind of number marginal_value actually is -- Wildcard/Free Hit come
  // from an unconstrained from-scratch rebuild (_optimize_squad, no initial_squad), Bench Boost/
  // Triple Captain score the manager's real squad as-is. Both used to render identically with
  // nothing distinguishing the two computations. Worded per chip via squadBasisLabel and shown on
  // every alternative card below -- including the picked chip's own card, which already appears in
  // `alternatives` (unfiltered by transfer_decisions.py) -- so it isn't repeated a second time up
  // here too (caught via live user feedback: the two copies read as redundant, not reinforcing).
  // Issue #266: effective_threshold/value_above_threshold have been on this payload since #267
  // (the season-stage/scarcity-adjusted bar actually being compared against) but were never
  // rendered here -- confirmed by grepping every dashboard JS file for both fields before this
  // fix. Only the static threshold was ever shown, so a manager had no way to see "the bar came
  // down" (the exact story #266's amendment is about) even for *this* week's own numbers. Shown
  // only when it actually differs from the raw threshold -- late-season, once decay has settled
  // to ~0, the two are identical and a second identical number would just be noise.
  const effectiveThresholdNote = (item) =>
    item.effective_threshold != null && Math.abs(item.effective_threshold - item.threshold) >= 0.05
      ? `<br><span class="muted">Currently ${Number(item.effective_threshold).toFixed(1)} (adjusted for season stage/chip scarcity)</span>`
      : "";
  const valueAboveThresholdNote = (item) =>
    item.value_above_threshold != null
      ? `<br><span class="muted">${item.value_above_threshold >= 0 ? "Clears" : "Below"} by ${Number(item.value_above_threshold).toFixed(1)}</span>`
      : "";
  const alternatives = (chip.alternatives || [])
    .map(
      (item) =>
        `<div class="chip-alternative"><strong>${esc(item.label)}</strong><br>${Number(item.marginal_value).toFixed(1)} marginal xPts <span class="muted">(${chipHorizonLabel(item.horizon)})</span><br><span class="muted">${esc(squadBasisLabel(item.chip))}</span><br><span class="muted">Threshold ${Number(item.threshold).toFixed(1)}</span>${effectiveThresholdNote(item)}${valueAboveThresholdNote(item)}</div>`,
    )
    .join("");
  byId("weekly-chip").innerHTML =
    `<strong>${esc(chip.label || "Hold all chips")}</strong>${pickedHorizonLabel}<p>${esc(chip.reason || "No chip recommendation is available.")}</p><span class="muted">No-chip baseline: ${Number(chip.no_chip_projected_points || 0).toFixed(1)} projected GW${weekly.event} points</span>${alternatives ? `<div class="chip-alternatives">${alternatives}</div>` : ""}`;
  const inventory = (weekly.chip_inventory || [])
    .map(
      (item) =>
        `${item.label}: ${item.available ? "available" : item.used_event ? `used GW${item.used_event}` : `outside GW${item.start_event}–${item.stop_event}`}`,
    )
    .join(" · ");
  const rules = weekly.official_rules || {};
  const rulesPreview = weekly.draft
    ? `Once Gameweek 1 begins, one free transfer per Gameweek applies, up to ${esc(rules.maximum_free_transfers)} stored; each excess transfer costs ${esc(rules.extra_transfer_cost)} points.`
    : `<strong>Official rules reviewed</strong><br>One free transfer per Gameweek, up to ${esc(rules.maximum_free_transfers)} stored; each excess transfer costs ${esc(rules.extra_transfer_cost)} points. ${esc(inventory)}`;
  byId("weekly-rules").innerHTML =
    `${rulesPreview}<br>${safeLink(rules.source, "Official FPL rules")}<br><span class="status-wait">${esc(weekly.state_warning)}</span>`;
}
function renderManager() {
  const manager = state.manager || {
    connection_status: "not_configured",
    squad: [],
  };
  const value = (value) =>
    value === null || value === "" || value === false
      ? "Not yet available"
      : value;
  const money = (value) =>
    value === null || value === ""
      ? "Not yet available"
      : `£${(Number(value) / 10).toFixed(1)}m`;
  // The single source of the pitch's empty/placeholder state -- every early-return branch below
  // funnels through this instead of setting squad-pitch-empty's text inline, so the pitch can
  // never go stale independently of whichever message actually applies.
  const resetSquadPitch = (message) => {
    byId("squad-pitch").hidden = true;
    byId("squad-pitch").innerHTML = "";
    byId("squad-pitch-formation").textContent = "";
    byId("squad-bench").innerHTML = "";
    byId("squad-pitch-empty").hidden = false;
    byId("squad-pitch-empty").textContent = message;
  };
  if (manager.connection_status === "lookup_failed") {
    const failNote =
      "Team not found, or the official FPL API is temporarily unavailable. Check the team ID and try again.";
    byId("my-team-summary").innerHTML =
      `<div class="empty">${esc(failNote)}</div>`;
    resetSquadPitch("No public squad is connected.");
    return;
  }
  if (manager.connection_status === "not_configured") {
    const setupNote =
      "Enter your FPL team ID (from your FPL entry URL) in the Manager profile form on the My Profile view, then save.";
    byId("my-team-summary").innerHTML =
      `<div class="empty">No public team ID is configured. ${esc(setupNote)}</div>`;
    resetSquadPitch("No public squad is connected.");
    return;
  }
  byId("my-team-summary").innerHTML =
    `<div class="team-stat"><b>${esc(manager.team_name || "Unnamed team")}</b><span>${esc(manager.manager_name || "Manager")}</span></div><div class="team-stat"><b>${esc(manager.team_id)}</b><span>Team ID</span></div><div class="team-stat"><b>${esc(value(manager.overall_rank))}</b><span>Overall rank</span></div><div class="team-stat"><b>${esc(value(manager.overall_points))}</b><span>Points</span></div><div class="team-stat"><b>${esc(money(manager.team_value))}</b><span>Team value</span></div><div class="team-stat"><b>${esc(money(manager.bank))}</b><span>Bank</span></div>`;
  if (!manager.squad_publicly_available) {
    resetSquadPitch(
      "Public GW1 squad is hidden until the deadline. Pitch view appears once official FPL publishes the picks.",
    );
    return;
  }
  const positionShort = { 1: "GKP", 2: "DEF", 3: "MID", 4: "FWD" };
  const startingPlayers = manager.squad
    .filter((player) => player.position <= 11)
    .slice()
    .sort((a, b) => a.position - b.position);
  const benchPlayers = manager.squad
    .filter((player) => player.position > 11)
    .slice()
    .sort((a, b) => a.position - b.position);
  if (!startingPlayers.length) {
    resetSquadPitch("Pitch view appears once a public squad is connected.");
    return;
  }
  const counts = { DEF: 0, MID: 0, FWD: 0 };
  startingPlayers.forEach((player) => {
    const key = positionShort[player.element_type];
    if (key === "DEF" || key === "MID" || key === "FWD") counts[key] += 1;
  });
  const formationLabel = `${counts.DEF}-${counts.MID}-${counts.FWD}`;
  byId("squad-pitch-formation").textContent = formationLabel;
  byId("squad-pitch").setAttribute(
    "aria-label",
    `${formationLabel} formation: ${startingPlayers.map((player) => `${player.name}${player.is_captain ? " captain" : player.is_vice_captain ? " vice-captain" : ""}`).join(", ")}`,
  );
  byId("squad-pitch").innerHTML = ["FWD", "MID", "DEF", "GKP"]
    .map(
      (position) =>
        `<div class="pitch-row pitch-${position.toLowerCase()}">${startingPlayers
          .filter(
            (player) => positionShort[player.element_type] === position,
          )
          .map(
            (player) =>
              `<div class="pitch-player ${player.is_captain ? "captain" : ""}" title="${esc(player.name)}"><strong>${esc(player.name)}${player.is_captain ? " (C)" : player.is_vice_captain ? " (VC)" : ""}</strong><span>${esc(money(player.price))}</span></div>`,
          )
          .join("")}</div>`,
    )
    .join("");
  byId("squad-bench").innerHTML = benchPlayers
    .map(
      (player, index) =>
        `<div class="weekly-bench-card"><strong>${index + 1}. ${esc(player.name)}</strong><span>${esc(positionShort[player.element_type] || "Player")} · ${esc(money(player.price))}</span></div>`,
    )
    .join("");
  byId("squad-pitch-empty").hidden = true;
  byId("squad-pitch").hidden = false;
}
