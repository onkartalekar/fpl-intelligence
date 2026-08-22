function renderOverview() {
  const summary = state.transfer_summary || {
    total: transfers.length,
    high: 0,
    medium: 0,
    low: 0,
    actionable: 0,
  };
  const relevantNew = transfers.filter(
    (row) =>
      ["high", "medium"].includes(row.fpl_relevance) &&
      row.freshness === "new_7d",
  ).length;
  byId("season-readiness").textContent = seasonLabel();
  byId("new-count").textContent = relevantNew;
  byId("high-count").textContent = summary.high || 0;
  byId("pending-count").textContent = summary.medium || 0;
  byId("deadline-status").textContent = deadlineText();
  if (state.fpl.season_phase === "in_season")
    byId("preseason-workflow").hidden = true;
  const attention = [];
  if ((state.manager || {}).connection_status === "not_configured")
    attention.push({
      level: "Setup",
      kind: "info",
      title: "Connect your FPL team",
      body: "Enter your FPL team ID in the Manager profile form on the My Profile view, then save.",
      action: "Open My Profile",
      view: "profile",
    });
  if (
    (state.manager || {}).connection_status !== "not_configured" &&
    !(state.profile || {}).reminder_status
  )
    attention.push({
      level: "Setup",
      kind: "info",
      title: "Get deadline reminders",
      body: "Get an email before each gameweek deadline with your recommended moves.",
      action: "Open My Profile",
      view: "profile",
    });
  if (!state.fpl.ready_for_2026_27)
    attention.push({
      level: "Monitor",
      kind: "info",
      title: "2026/27 FPL feed is not available yet",
      body: "Prices, positions, fixtures, and draft optimization remain locked.",
      action: "Review model status",
      view: "model",
    });
  else if (decision.status === "active_preliminary")
    attention.push({
      level: "Ready",
      kind: "ready",
      title: "Preliminary GW1 recommendation is available",
      body: "Official player and fixture data now powers a legal five-gameweek opening-squad baseline.",
      action: "Open Decision Center",
      view: "decisions",
    });
  else
    attention.push({
      level: "Review",
      kind: "info",
      title: "Target-season feed verified",
      body: "Official data is ready, but the recommendation model does not yet have complete inputs.",
      action: "Review model status",
      view: "model",
    });
  if (relevantNew)
    attention.push({
      level: "Review",
      kind: "",
      title: `${relevantNew} relevant changes this week`,
      body: "Review affected clubs and potential role changes before updating a draft.",
      action: "Open transfers",
      view: "transfers",
    });
  if (!relevantNew && state.fpl.ready_for_2026_27)
    attention.push({
      level: "Info",
      kind: "info",
      title: "No material transfer changes this week",
      body: "No immediate transfer-news review is required.",
    });
  byId("attention").innerHTML = attention
    .map(
      (item) =>
        `<div class="attention-item"><span class="severity ${item.kind}">${esc(item.level)}</span><div><strong>${esc(item.title)}</strong><span class="muted">${esc(item.body)}</span></div>${item.view ? `<button class="attention-action" data-go="${item.view}">${esc(item.action)}</button>` : ""}</div>`,
    )
    .join("");
  document
    .querySelectorAll("[data-go]")
    .forEach((button) =>
      button.addEventListener("click", () => showView(button.dataset.go)),
    );
  byId("freshness").innerHTML =
    `<dt>Last refreshed</dt><dd>${esc(fmtDate(state.generated_at, true))}</dd><dt>Timezone</dt><dd>${esc(timezoneLabel)}</dd><dt>FPL feed</dt><dd class="${state.fpl.ready_for_2026_27 ? "status-good" : "status-wait"}">${esc(seasonLabel())}</dd><dt>Official records</dt><dd>${summary.total || 0}</dd>`;
  renderChanges();
  const clubs = (state.club_summaries || [])
    .filter((item) => item.relevant_moves > 0)
    .slice(0, 10);
  byId("club-summary").innerHTML = clubs.length
    ? clubs
        .map(
          (item) =>
            `<button class="club-card" data-club="${esc(item.club)}"><strong>${esc(item.club)}</strong><small>${item.arrivals} in · ${item.departures} out · ${item.relevant_moves} relevant</small></button>`,
        )
        .join("")
    : '<div class="empty">No relevant club movements are available yet.</div>';
  document.querySelectorAll(".club-card").forEach((button) =>
    button.addEventListener("click", () => {
      byId("club-filter").value = button.dataset.club;
      page = 1;
      showView("transfers");
    }),
  );
}
function renderChanges() {
  const changes = state.changes_since_last_refresh || {};
  if (!changes.has_previous_snapshot) {
    byId("changes").innerHTML =
      '<div class="muted">Baseline snapshot recorded. Material changes will appear after the next refresh.</div>';
    return;
  }
  const rows = [];
  if (changes.new_confirmed_transfers)
    rows.push([
      "New confirmed movements",
      `+${changes.new_confirmed_transfers}`,
    ]);
  if (changes.new_fpl_players)
    rows.push(["New FPL players", `+${changes.new_fpl_players}`]);
  if (changes.club_mapping_changes)
    rows.push(["Club mappings changed", changes.club_mapping_changes]);
  if (changes.availability_changes)
    rows.push(["Availability changes", changes.availability_changes]);
  byId("changes").innerHTML = rows.length
    ? rows
        .map(
          (row) =>
            `<div class="change-row"><span>${esc(row[0])}</span><span class="change-value">${esc(row[1])}</span></div>`,
        )
        .join("")
    : '<div class="status-good">No material changes since your last refresh.</div>';
}
function populateFilters() {
  const clubs = (state.club_summaries || [])
    .map((item) => item.club)
    .filter(Boolean)
    .sort((a, b) => a.localeCompare(b));
  byId("club-filter").insertAdjacentHTML(
    "beforeend",
    clubs
      .map((club) => `<option value="${esc(club)}">${esc(club)}</option>`)
      .join(""),
  );
}
// Issue #232: a record's movement_type/movement_direction are stored relative to its
// premier_league_club -- the club whose transfer-centre playlist it came from -- but the club
// predicate below matches a record whether the selected club is its owner, its origin, or its
// destination. Reading the stored values while some *other* club is selected therefore answers
// the wrong club's question: Aston Villa's "Youri Tielemans, transfer-out" satisfied a Man Utd
// "Outgoing / Transfer out" filter even though he joined United.
//
// Deriving both from from_club/to_club against the selected club is the framing two other
// consumers already settled on for the same reason -- summarize_clubs (relevance.py) and
// build_gw_recommendations (recommendations.py), both of which explain that refresh.py's
// cross-source dedup keeps only one side's attribution, so which single movement_type survives
// is not reliable. from_club/to_club survive on every record regardless.
//
// Narrowing the club predicate to premier_league_club instead would be simpler but wrong: most
// intra-PL moves are reported by only one of the two clubs, so the buying club's arrivals would
// disappear from its own view entirely rather than merely being mislabelled.
const MIRRORED_MOVEMENT = {
  "transfer-in": "transfer-out",
  "transfer-out": "transfer-in",
  "loan-in": "loan-out",
  "loan-out": "loan-in",
};
function perspectiveOf(row, club) {
  const stored = {
    direction: row.movement_direction,
    movement: row.movement_type,
  };
  if (club === "all") return stored;
  const isOrigin = row.from_club === club;
  const isDestination = row.to_club === club;
  // Neither side is the selected club (it matched as premier_league_club only), or degenerately
  // both are -- either way the record's own framing is the only one available.
  if (isOrigin === isDestination) return stored;
  // "Released" is a departure that keeps its own direction bucket rather than folding into
  // Outgoing -- but only for the club doing the releasing. Where the write-up names where the
  // player actually went, the receiving club sees an ordinary arrival: Fulham released Harry
  // Wilson, who joined Leeds, and Leeds' Incoming should list him.
  if (isOrigin && stored.direction === "released") return stored;
  const direction = isOrigin ? "out" : "in";
  if (direction === stored.direction) return stored;
  // end-of-loan has no opposite type, so it keeps its movement and only flips direction.
  return {
    direction,
    movement: MIRRORED_MOVEMENT[row.movement_type] || row.movement_type,
  };
}
function filteredRows() {
  // Issue #239: this search box used to compare against a plain `.toLocaleLowerCase()` of
  // the row text -- no diacritic folding at all, so it couldn't match a special-lettered
  // name even after #238 fixed Player Explorer and Draft Squad's identical search boxes.
  // `row.search_key` is precomputed server-side (relevance.py's enrich_transfers) with a
  // real accent/special-letter fold; only the query -- typically plain ASCII -- needs
  // folding here.
  const query = foldDiacritics(
    byId("transfer-search").value.trim().toLocaleLowerCase(),
  );
  const club = byId("club-filter").value;
  const relevance = byId("relevance-filter").value;
  const direction = byId("direction-filter").value;
  const movement = byId("movement-filter").value;
  const freshness = byId("freshness-filter").value;
  return transfers.filter((row) => {
    const relevanceOk =
      relevance === "all" ||
      (relevance === "actionable" &&
        ["high", "medium"].includes(row.fpl_relevance)) ||
      row.fpl_relevance === relevance;
    const freshOk =
      freshness === "all" ||
      (freshness === "recent14" &&
        ["new_7d", "recent_14d"].includes(row.freshness)) ||
      row.freshness === freshness;
    const view = perspectiveOf(row, club);
    return (
      (!query || (row.search_key || "").includes(query)) &&
      (club === "all" ||
        row.premier_league_club === club ||
        row.from_club === club ||
        row.to_club === club) &&
      relevanceOk &&
      (direction === "all" || view.direction === direction) &&
      (movement === "all" || view.movement === movement) &&
      freshOk
    );
  });
}
const readable = {
  pending_new_season_fpl: "Awaiting 2026/27 FPL match",
  matched_current_fpl: "Matched to current 2026/27 FPL player",
  matched_prior_fpl: "Matched to prior FPL player",
  confirmed_first_party: "First-party confirmed",
  "transfer-in": "Transfer in",
  "transfer-out": "Transfer out",
  "loan-in": "Loan in",
  "loan-out": "Loan out",
  "player-released": "Released",
  "end-of-loan": "End of loan",
};
const label = (value) =>
  readable[value] || String(value || "Not recorded").replaceAll("_", " ");
function whyMatters(row) {
  if (row.matched_fpl_element_id)
    return "Known FPL player changed club or squad context.";
  if (row.movement_direction === "in")
    return "Confirmed arrival may affect roles and starting minutes.";
  if (row.movement_direction === "out")
    return "Departure may change minutes for the remaining squad.";
  return "Retained as verified squad evidence.";
}
function renderFilterChips() {
  const values = [
    ["relevance-filter", byId("relevance-filter").selectedOptions[0].text],
    ["freshness-filter", byId("freshness-filter").selectedOptions[0].text],
  ];
  if (byId("transfer-search").value)
    values.unshift([
      "transfer-search",
      `Search: ${byId("transfer-search").value}`,
    ]);
  if (byId("club-filter").value !== "all")
    values.push(["club-filter", byId("club-filter").value]);
  if (byId("direction-filter").value !== "all")
    values.push([
      "direction-filter",
      byId("direction-filter").selectedOptions[0].text,
    ]);
  if (byId("movement-filter").value !== "all")
    values.push([
      "movement-filter",
      byId("movement-filter").selectedOptions[0].text,
    ]);
  byId("active-filters").innerHTML = values
    .map(
      (item) =>
        `<button class="filter-chip" data-clear="${item[0]}" type="button">${esc(item[1])} ×</button>`,
    )
    .join("");
  document.querySelectorAll("[data-clear]").forEach((button) =>
    button.addEventListener("click", () => {
      const id = button.dataset.clear;
      byId(id).value = id === "transfer-search" ? "" : "all";
      page = 1;
      applyFilters();
    }),
  );
  if (typeof syncFiltersTriggerBadge === "function") syncFiltersTriggerBadge();
}
const INSPECTOR_PLACEHOLDER =
  "Select a result to inspect its source, classification, and FPL reconciliation state.";
function resetInspector() {
  const inspector = byId("inspector");
  inspector.className = "empty";
  inspector.textContent = INSPECTOR_PLACEHOLDER;
}
function applyFilters() {
  resetInspector();
  const rows = filteredRows();
  const pages = Math.max(1, Math.ceil(rows.length / pageSize));
  page = Math.min(page, pages);
  const visible = rows.slice((page - 1) * pageSize, page * pageSize);
  byId("result-count").textContent =
    `${rows.length} result${rows.length === 1 ? "" : "s"}`;
  byId("page-label").textContent = `Page ${page} of ${pages}`;
  byId("prev-page").disabled = page <= 1;
  byId("next-page").disabled = page >= pages;
  renderFilterChips();
  const feed = byId("feed");
  if (!visible.length) {
    feed.innerHTML =
      '<div class="empty">No confirmed transfers match these filters. Broaden the date or relevance setting.</div>';
    return;
  }
  feed.innerHTML = visible
    .map(
      (row) =>
        `<button class="transfer"><span><strong>${esc(row.player)}</strong><small>${esc(row.from_club)} → ${esc(row.to_club)} · ${esc(fmtDate(row.announced_at))}</small><span class="why">${esc(whyMatters(row))}</span><span class="tags"><span class="tag ${esc(row.fpl_relevance)}">${esc(label(row.fpl_relevance))} relevance</span><span class="tag">${esc(label(row.movement_type))}</span><span class="tag">${esc(label(row.fpl_reconciliation_status))}</span></span></span><span class="muted">${esc(row.premier_league_club)}</span></button>`,
    )
    .join("");
  feed
    .querySelectorAll(".transfer")
    .forEach((button, index) =>
      button.addEventListener("click", () => inspect(visible[index])),
    );
}
function inspect(row) {
  const links = (row.supporting_source_urls || [row.source_url])
    .filter(Boolean)
    .map((url, index) => safeLink(url, `Source ${index + 1}`))
    .join(" · ");
  byId("inspector").className = "";
  byId("inspector").innerHTML =
    `<dl><dt>Player</dt><dd>${esc(row.player)}</dd><dt>Movement</dt><dd>${esc(row.from_club)} → ${esc(row.to_club)}</dd><dt>Why it matters</dt><dd>${esc(whyMatters(row))}</dd><dt>PL club</dt><dd>${esc(row.premier_league_club)}</dd><dt>Announced</dt><dd>${esc(fmtDate(row.announced_at, true))}</dd><dt>FPL relevance</dt><dd>${esc(label(row.fpl_relevance))}</dd><dt>Verification</dt><dd>${esc(label(row.verification_status))}</dd><dt>FPL status</dt><dd>${esc(label(row.fpl_reconciliation_status))}</dd><dt>Evidence</dt><dd>${links}</dd></dl>`;
  // Issue #242: on mobile, the inspector opens as a sheet instead of just being written into the
  // always-visible side panel (still the desktop behavior, and the mobile no-JS fallback).
  if (typeof openContentSheet === "function" && typeof isMobileShellBreakpoint === "function" && isMobileShellBreakpoint())
    openContentSheet(byId("inspector"), "Evidence");
}
const filterIds = [
  "transfer-search",
  "club-filter",
  "relevance-filter",
  "direction-filter",
  "movement-filter",
  "freshness-filter",
];
filterIds.forEach((id) =>
  byId(id).addEventListener(
    id === "transfer-search" ? "input" : "change",
    () => {
      page = 1;
      applyFilters();
    },
  ),
);
function doResetFilters() {
  byId("transfer-search").value = "";
  byId("club-filter").value = "all";
  byId("relevance-filter").value = "actionable";
  byId("direction-filter").value = "all";
  byId("movement-filter").value = "all";
  byId("freshness-filter").value = "recent14";
  page = 1;
  applyFilters();
}
byId("reset-filters").addEventListener("click", () => {
  // Issue #242: on mobile, a confirm sheet stands between the tap and the reset -- fat fingers
  // near the edge of a small screen shouldn't be able to instantly wipe an in-progress filter
  // set. Desktop keeps the original immediate behavior.
  if (typeof openConfirmSheet === "function" && typeof isMobileShellBreakpoint === "function" && isMobileShellBreakpoint()) {
    openConfirmSheet({
      title: "Reset filters?",
      message: "This clears your search and every filter, back to the defaults.",
      confirmLabel: "Reset filters",
      onConfirm: doResetFilters,
    });
    return;
  }
  doResetFilters();
});
byId("prev-page").addEventListener("click", () => {
  page = Math.max(1, page - 1);
  applyFilters();
});
byId("next-page").addEventListener("click", () => {
  page += 1;
  applyFilters();
});
function setupPlayerExplorer() {
  const clubs = [
    ...new Set(players.map((player) => player.club).filter(Boolean)),
  ].sort((a, b) => a.localeCompare(b));
  const positions = [
    ...new Set(players.map((player) => player.position).filter(Boolean)),
  ].sort((a, b) => a.localeCompare(b));
  byId("player-club-filter").insertAdjacentHTML(
    "beforeend",
    clubs
      .map((club) => `<option value="${esc(club)}">${esc(club)}</option>`)
      .join(""),
  );
  byId("player-position-filter").insertAdjacentHTML(
    "beforeend",
    positions
      .map(
        (position) =>
          `<option value="${esc(position)}">${esc(position)}</option>`,
      )
      .join(""),
  );
  [
    "player-search",
    "player-club-filter",
    "player-position-filter",
    "player-sort",
  ].forEach((id) =>
    byId(id).addEventListener(
      id === "player-search" ? "input" : "change",
      () => {
        playerPage = 1;
        renderPlayers();
      },
    ),
  );
  byId("player-prev").addEventListener("click", () => {
    playerPage = Math.max(1, playerPage - 1);
    renderPlayers();
  });
  byId("player-next").addEventListener("click", () => {
    playerPage += 1;
    renderPlayers();
  });
  renderPlayers();
}
function renderPlayers() {
  // Issue #239: `player.search_key` is precomputed server-side (catalog.py's
  // build_player_catalog) with a real accent/special-letter fold, so the browser no longer
  // re-derives one from `player.name`/`full_name` on every keystroke -- only the (typically
  // plain-ASCII) query needs folding here.
  const query = foldDiacritics(
    byId("player-search").value.trim().toLocaleLowerCase(),
  );
  const club = byId("player-club-filter").value;
  const position = byId("player-position-filter").value;
  const sort = byId("player-sort").value;
  const rows = players.filter(
    (player) =>
      (!query || (player.search_key || "").includes(query)) &&
      (club === "all" || player.club === club) &&
      (position === "all" || player.position === position),
  );
  const comparisons = {
    name: (a, b) => (a.name || "").localeCompare(b.name || ""),
    "price-desc": (a, b) =>
      b.price - a.price || (a.name || "").localeCompare(b.name || ""),
    "price-asc": (a, b) =>
      a.price - b.price || (a.name || "").localeCompare(b.name || ""),
    "ownership-desc": (a, b) =>
      b.ownership - a.ownership || (a.name || "").localeCompare(b.name || ""),
  };
  rows.sort(comparisons[sort] || comparisons["price-desc"]);
  const pages = Math.max(1, Math.ceil(rows.length / 30));
  playerPage = Math.min(playerPage, pages);
  const visible = rows.slice((playerPage - 1) * 30, playerPage * 30);
  byId("player-count").textContent =
    `${rows.length} player${rows.length === 1 ? "" : "s"}`;
  byId("player-page").textContent = `Page ${playerPage} of ${pages}`;
  byId("player-prev").disabled = playerPage <= 1;
  byId("player-next").disabled = playerPage >= pages;
  const statuses = {
    a: "Available",
    d: "Doubtful",
    i: "Injured",
    s: "Suspended",
    u: "Unavailable",
    n: "Not available",
  };
  byId("player-results").innerHTML = visible.length
    ? visible
        .map(
          (player) =>
            // data-label (issue #242): column names the <thead> would otherwise supply, exposed
            // per-cell so the mobile @media(max-width:760px) block can hide the header row and
            // reflow .player-row into stacked label/value lines -- same cells, same <table>.
            `<tr class="player-row"><th scope="row"><strong>${esc(player.name)}</strong><small>${esc(player.full_name || "")}</small></th><td data-label="Club">${esc(player.club)}</td><td data-label="Position">${esc(player.position)}</td><td class="price" data-label="Price">£${Number(player.price).toFixed(1)}m</td><td data-label="Owned">${Number(player.ownership).toFixed(1)}%</td><td data-label="Status"><strong>${esc(statuses[player.status] || player.status || "Unknown")}</strong>${player.news ? `<small>${esc(player.news)}</small>` : ""}</td></tr>`,
        )
        .join("")
    : '<tr><td colspan="6"><div class="empty">No players match these filters.</div></td></tr>';
}
