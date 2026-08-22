const draftQuotas = { GKP: 2, DEF: 5, MID: 5, FWD: 3 };
const draftSize = Object.values(draftQuotas).reduce(
  (sum, count) => sum + count,
  0,
);
const draftBudget = 100.0;
const draftClubLimit = 3;
let draftSelection =
  state.profile && Array.isArray(state.profile.draft_squad)
    ? state.profile.draft_squad.slice()
    : [];
function draftPlayerById(id) {
  return players.find((player) => player.id === id);
}
function draftTotals() {
  const squad = draftSelection.map(draftPlayerById).filter(Boolean);
  const spend = squad.reduce((sum, player) => sum + Number(player.price), 0);
  const positions = {};
  squad.forEach((player) => {
    positions[player.position_short] =
      (positions[player.position_short] || 0) + 1;
  });
  const clubs = {};
  squad.forEach((player) => {
    clubs[player.club] = (clubs[player.club] || 0) + 1;
  });
  return { squad, spend, positions, clubs };
}

// Issue #152 follow-up: adding a player used to append it to a separate flat "selected squad"
// list, entirely disconnected from the pitch view below it -- redundant, per direct user
// feedback ("seems redundant" to declare on top and see it again in the pitch). A newly added
// player now lands straight on the pitch (starting XI if there's legal room, else bench) or
// straight on the bench when removed from the XI, with no intermediate flat list at all.
function addDraftPlayer(id) {
  if (draftSelection.includes(id) || draftSelection.length >= draftSize) return;
  draftSelection = [...draftSelection, id];
  const player = draftPlayerById(id);
  const startingSquadById = Object.fromEntries(
    draftStartingIds.map((startId) => [startId, draftPlayerById(startId)]),
  );
  const counts = draftXiPositionCounts(draftStartingIds, startingSquadById);
  if (draftXiCanAdd(player.position_short, counts))
    draftStartingIds = [...draftStartingIds, id];
  renderDraftBuilder();
}
function removeDraftPlayer(id) {
  draftSelection = draftSelection.filter((existing) => existing !== id);
  draftStartingIds = draftStartingIds.filter((existing) => existing !== id);
  if (draftPendingSwapId === id) draftPendingSwapId = null;
  if (draftCaptainId === id) draftCaptainId = draftStartingIds[0] || null;
  if (
    draftViceId === id ||
    (draftCaptainId !== null && draftViceId === draftCaptainId)
  )
    draftViceId =
      draftStartingIds.find((existing) => existing !== draftCaptainId) || null;
  renderDraftBuilder();
}

const draftResultsPageSize = 10;
let draftResultsPage = 1;
function draftResultRows() {
  // Issue #239: see the identical comment in overview-transfers-players.js's renderPlayers --
  // `player.search_key` is precomputed server-side, so only the query is folded here.
  const query = foldDiacritics(
    byId("draft-search").value.trim().toLocaleLowerCase(),
  );
  const club = byId("draft-club-filter").value;
  const position = byId("draft-position-filter").value;
  const sort = byId("draft-sort").value;
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
  };
  rows.sort(comparisons[sort] || comparisons["price-desc"]);
  return rows;
}
// The "Add players" list sits beside the pitch instead of below a separate flat squad list, and
// is now genuinely paginated (10 per page) with a price sort, rather than silently truncating to
// the first 30 matches with no way to see the rest.
function renderDraftResultsList() {
  const rows = draftResultRows();
  const pages = Math.max(1, Math.ceil(rows.length / draftResultsPageSize));
  draftResultsPage = Math.min(Math.max(1, draftResultsPage), pages);
  const visible = rows.slice(
    (draftResultsPage - 1) * draftResultsPageSize,
    draftResultsPage * draftResultsPageSize,
  );
  byId("draft-results-count").textContent =
    `${rows.length} player${rows.length === 1 ? "" : "s"}`;
  byId("draft-results-page").textContent =
    `Page ${draftResultsPage} of ${pages}`;
  byId("draft-results-prev").disabled = draftResultsPage <= 1;
  byId("draft-results-next").disabled = draftResultsPage >= pages;
  const statuses = {
    a: "Available",
    d: "Doubtful",
    i: "Injured",
    s: "Suspended",
    u: "Unavailable",
    n: "Not available",
  };
  byId("draft-results-list").innerHTML = visible.length
    ? visible
        .map((player) => {
          const picked = draftSelection.includes(player.id);
          const full = draftSelection.length >= draftSize && !picked;
          return `<div class="decision-note draft-result-row"><div><strong>${esc(player.name)}</strong><span>${esc(player.position_short)} &middot; ${esc(player.club)} &middot; £${Number(player.price).toFixed(1)}m &middot; ${esc(statuses[player.status] || player.status || "Unknown")}</span></div><button type="button" class="reset-filters" data-add-id="${player.id}" ${picked || full ? "disabled" : ""}>${picked ? "Added" : "Add"}</button></div>`;
        })
        .join("")
    : '<div class="empty">No players match these filters.</div>';
  document
    .querySelectorAll("[data-add-id]")
    .forEach((button) =>
      button.addEventListener("click", () =>
        addDraftPlayer(Number(button.dataset.addId)),
      ),
    );
}

function renderDraftBuilder() {
  const { squad, spend, positions, clubs } = draftTotals();
  const remaining = draftBudget - spend;
  byId("draft-count").textContent = `${squad.length} / ${draftSize} selected`;
  byId("draft-budget").textContent =
    `£${spend.toFixed(1)}m spent · £${remaining.toFixed(1)}m remaining`;
  byId("draft-quota").textContent = Object.entries(draftQuotas)
    .map(
      ([position, count]) => `${positions[position] || 0}/${count} ${position}`,
    )
    .join(" · ");
  const overClub = Object.entries(clubs)
    .filter(([, count]) => count > draftClubLimit)
    .map(([club]) => club);
  const warnings = byId("draft-warnings");
  if (overClub.length) {
    warnings.hidden = false;
    warnings.innerHTML = `<strong>Too many players from one club</strong>Max ${draftClubLimit} per club: ${esc(overClub.join(", "))}.`;
  } else if (remaining < -1e-9) {
    warnings.hidden = false;
    warnings.innerHTML = `<strong>Over budget</strong>Your selection costs £${spend.toFixed(1)}m, over the £${draftBudget.toFixed(1)}m budget.`;
  } else {
    warnings.hidden = true;
    warnings.innerHTML = "";
  }
  renderDraftResultsList();
  renderDraftPitch();
  const ready =
    squad.length === draftSize &&
    Object.entries(draftQuotas).every(
      ([position, count]) => (positions[position] || 0) === count,
    ) &&
    !overClub.length &&
    remaining >= -1e-9;
  byId("draft-save").disabled = !ready || !servedLive();
  byId("draft-clear").disabled =
    (!draftSelection.length && !(state.profile && state.profile.draft_squad)) ||
    !servedLive();
}
async function clearDraftSquad() {
  draftSelection = [];
  draftStartingIds = [];
  draftCaptainId = null;
  draftViceId = null;
  renderDraftBuilder();
  const message = byId("draft-message");
  const rawTeamId =
    byId("draft-team-id").value.trim() ||
    String((state.profile && state.profile.team_id) || "");
  if (!state.profile || !state.profile.draft_squad || !rawTeamId) {
    message.textContent = "";
    return;
  }
  byId("draft-clear").disabled = true;
  message.textContent = "Clearing saved draft…";
  try {
    const response = await fetch("/api/draft-squad", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ team_id: Number(rawTeamId), player_ids: null }),
    });
    const responsePayload = await response
      .json()
      .catch(() => ({ message: "Clear returned an unreadable response." }));
    if (!response.ok)
      throw new Error(
        responsePayload.message ||
          `Clear failed with status ${response.status}`,
      );
    message.textContent = "Draft cleared. Reloading…";
    window.setTimeout(() => window.location.reload(), 400);
  } catch (error) {
    byId("draft-clear").disabled = false;
    message.textContent = `Clear failed: ${error.message}`;
  }
}
function setupDraftSquad() {
  const clubs = [
    ...new Set(players.map((player) => player.club).filter(Boolean)),
  ].sort((a, b) => a.localeCompare(b));
  const positions = [
    ...new Set(players.map((player) => player.position).filter(Boolean)),
  ].sort((a, b) => a.localeCompare(b));
  byId("draft-club-filter").insertAdjacentHTML(
    "beforeend",
    clubs
      .map((club) => `<option value="${esc(club)}">${esc(club)}</option>`)
      .join(""),
  );
  byId("draft-position-filter").insertAdjacentHTML(
    "beforeend",
    positions
      .map(
        (position) =>
          `<option value="${esc(position)}">${esc(position)}</option>`,
      )
      .join(""),
  );
  byId("draft-team-id").value = (state.profile && state.profile.team_id) || "";
  [
    "draft-search",
    "draft-club-filter",
    "draft-position-filter",
    "draft-sort",
  ].forEach((id) =>
    byId(id).addEventListener(
      id === "draft-search" ? "input" : "change",
      () => {
        draftResultsPage = 1;
        renderDraftResultsList();
      },
    ),
  );
  byId("draft-results-prev").addEventListener("click", () => {
    draftResultsPage = Math.max(1, draftResultsPage - 1);
    renderDraftResultsList();
  });
  byId("draft-results-next").addEventListener("click", () => {
    draftResultsPage += 1;
    renderDraftResultsList();
  });
  byId("draft-clear").addEventListener("click", () => {
    // Issue #242: on mobile, a confirm sheet stands between the tap and the clear -- this wipes
    // the saved draft server-side with no undo, and there was no confirmation at all before this
    // (not even a browser confirm()). Desktop keeps the original immediate behavior.
    if (typeof openConfirmSheet === "function" && typeof isMobileShellBreakpoint === "function" && isMobileShellBreakpoint()) {
      openConfirmSheet({
        title: "Clear draft squad?",
        message: "This clears your saved 15-player draft. It can't be undone.",
        confirmLabel: "Clear draft",
        onConfirm: clearDraftSquad,
      });
      return;
    }
    clearDraftSquad();
  });
  const controls = [byId("draft-team-id"), byId("draft-save")];
  if (!servedLive()) {
    controls.forEach((control) => (control.disabled = true));
    byId("draft-message").textContent =
      "Start the local dashboard service to save a draft squad.";
  }
  byId("draft-save-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const message = byId("draft-message");
    const rawTeamId = byId("draft-team-id").value.trim();
    if (
      !/^[0-9]+$/.test(rawTeamId) ||
      Number(rawTeamId) < 1 ||
      Number(rawTeamId) > 99999999
    ) {
      message.textContent =
        "Enter a valid FPL team ID to save your draft squad.";
      return;
    }
    if (draftSelection.length !== draftSize) {
      message.textContent = `Select exactly ${draftSize} players before saving.`;
      return;
    }
    const saveButton = byId("draft-save");
    saveButton.disabled = true;
    message.textContent = "Saving…";
    const teamId = Number(rawTeamId);
    try {
      const response = await fetch("/api/draft-squad", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ team_id: teamId, player_ids: draftSelection }),
      });
      const responsePayload = await response
        .json()
        .catch(() => ({
          message: "Draft squad save returned an unreadable response.",
        }));
      if (!response.ok)
        throw new Error(
          responsePayload.message ||
            `Draft squad save failed with status ${response.status}`,
        );
      // Issue #152 follow-up: per live feedback, stay on this tab instead of reloading the whole
      // page -- the tab already shows draft health and the pitch itself, and there's already a link
      // into Decision Center for anyone who wants the full view. `/api/manager-view` (issue #125)
      // returns exactly the same `weekly_decisions`/`manager` the old full-page reload would have
      // picked up, as JSON, so the draft health/pitch panels can refresh in place.
      //
      // Issue #220 fix: this used to also force `draftPitchSeededFor=null` here, on every successful
      // save. That made `seedDraftPitch()` (below) reseed `draftStartingIds`/`draftCaptainId`/
      // `draftViceId` from the model's own recommendation unconditionally, discarding whatever
      // starting-XI/bench arrangement or captain/vice-captain the user had just set locally --
      // reported live as a vice-captain pick silently reverting the instant "Draft squad saved."
      // appeared, with no reload involved. `seedDraftPitch`'s own key (the saved squad's sorted
      // player-id set) already reseeds correctly on its own whenever the squad's membership actually
      // changes; forcing it open here on every save, even when membership didn't change, was the bug.
      try {
        const refreshResponse = await fetch(
          `/api/manager-view?team_id=${teamId}`,
        );
        const refreshPayload = await refreshResponse.json();
        if (refreshResponse.ok && refreshPayload.status === "ok") {
          decision.weekly_decisions = refreshPayload.weekly_decisions;
          state.manager = refreshPayload.manager;
          state.profile = state.profile || {};
          state.profile.team_id = teamId;
          state.profile.draft_squad = draftSelection.slice();
          message.textContent = "Draft squad saved.";
          renderDraftBuilder();
          renderDraftHealth();
          renderManager();
          renderWeeklyDecision();
          renderDecision();
        } else {
          message.textContent =
            "Draft squad saved. Reloading to refresh Decision Center…";
          window.setTimeout(() => window.location.reload(), 400);
        }
      } catch (refreshError) {
        message.textContent =
          "Draft squad saved. Reloading to refresh Decision Center…";
        window.setTimeout(() => window.location.reload(), 400);
      }
    } catch (error) {
      message.textContent = `Save failed: ${error.message}`;
    } finally {
      saveButton.disabled = false;
    }
  });
  renderDraftBuilder();
}

// Issue #152: the draft tab's "draft health" summary reuses `build_draft_decisions`'s existing
// output (already computed server-side pre-GW1, see `refresh.py`'s `compute_manager_view`)
// rather than a new computation -- `draftRollScenario()` pulls the "roll" scenario specifically
// (not `weekly.recommendation`, which may reflect a *suggested* transfer) because it's the one
// scenario whose `squad` is guaranteed to be exactly the user's own declared 15, enriched with
// model projections. Hidden until a complete, legal draft has been saved
// (`weekly.draft && weekly.status==='active'`), since `build_draft_decisions` itself only
// computes anything once `validate_draft_squad` passes -- see
// plans/issue-152-preseason-draft-ui.md's "Structural constraint" section.
function draftRollScenario() {
  const weekly = decision.weekly_decisions || {};
  if (!weekly.draft || weekly.status !== "active") return null;
  const profiles = weekly.profiles || [];
  const selected =
    profiles.find((row) => row.id === (weekly.default_profile || "balanced")) ||
    profiles[0];
  if (!selected) return null;
  return (
    (selected.scenarios || []).find((scenario) => scenario.action === "roll") ||
    null
  );
}

// Bug fix: renderDraftPitchSaved can only render players present in `roll.squad` -- the
// server-computed projections for whatever 15 was saved *last*. A player added (or removed) from
// draftSelection since that save has no entry there at all, so they used to silently vanish from
// both the pitch and the bench instead of appearing without projections. Gating on this match
// means any local edit that diverges from the saved squad falls back to the no-projections
// builder view (which reads live off draftSelection/draftPlayerById and already handles
// add/remove/bench/start correctly) until the next save brings the two back in sync.
function draftSquadMatchesSaved(roll) {
  if (!roll) return false;
  const savedIds = new Set((roll.squad || []).map((player) => player.id));
  if (savedIds.size !== draftSelection.length) return false;
  return draftSelection.every((id) => savedIds.has(id));
}

function renderDraftHealth() {
  const roll = draftRollScenario();
  const empty = byId("draft-health-empty");
  const content = byId("draft-health-content");
  if (!roll) {
    empty.hidden = false;
    content.hidden = true;
    return;
  }
  empty.hidden = true;
  content.hidden = false;
  const squad = roll.squad || [];
  const totals = squad.reduce(
    (sum, player) => ({
      xp1: sum.xp1 + Number(player.xp_1 || 0),
      xp3: sum.xp3 + Number(player.xp_3 || 0),
      xp5: sum.xp5 + Number(player.xp_5 || 0),
    }),
    { xp1: 0, xp3: 0, xp5: 0 },
  );
  byId("draft-health-progression").innerHTML =
    `<div class="decision-metric"><b>${totals.xp1.toFixed(1)}</b><span>Modeled points, next GW</span></div><div class="decision-metric"><b>${totals.xp3.toFixed(1)}</b><span>Modeled points, 3-GW horizon</span></div><div class="decision-metric"><b>${totals.xp5.toFixed(1)}</b><span>Modeled points, 5-GW horizon</span></div>`;
  const statuses = {
    d: "Doubtful",
    i: "Injured",
    s: "Suspended",
    u: "Unavailable",
    n: "Not available",
  };
  const risky = squad.filter(
    (player) =>
      (player.status && player.status !== "a") || player.confidence === "low",
  );
  byId("draft-health-risks").innerHTML = risky.length
    ? risky
        .map(
          (player) =>
            `<div class="decision-note"><strong>${esc(player.name)}</strong><br>${esc(statuses[player.status] || "Low-confidence projection")} &middot; ${esc(player.club)}</div>`,
        )
        .join("")
    : '<div class="empty">No availability or confidence concerns flagged in your declared draft.</div>';
  const weekly = decision.weekly_decisions || {};
  const profiles = weekly.profiles || [];
  byId("draft-health-profiles").innerHTML = profiles
    .map((profile) => {
      const recommendation = profile.recommendation || {};
      return `<div class="decision-note"><strong>${esc(profile.label)} &middot; ${Number(recommendation.net_gain_5gw || 0).toFixed(1)} 5-GW edge</strong><br>${esc(recommendation.reason || "")}</div>`;
    })
    .join("");
}

// The formation/quota rules a starting XI must satisfy -- distinct from `draftQuotas` above,
// which is the 15-player *squad's* composition, not the 11-player XI's (plans/issue-152... :
// "a genuinely different rule set from validate_draft_squad's 15-player squad-composition
// check"). Enforced purely client-side (session-only, see the plan's Candidate 1 decision).
const draftXiMin = { GKP: 1, DEF: 3, MID: 2, FWD: 1 };
const draftXiMax = { GKP: 1, DEF: 5, MID: 5, FWD: 3 };
let draftStartingIds = [];
let draftCaptainId = null;
let draftViceId = null;
let draftPendingSwapId = null;
let draftPitchSeededFor = null;

// Build-phase guard, per live feedback: capping only GKP-at-1 and total-at-11 let the XI reach
// an illegal shape like 5 DEF/5 MID/0 FWD -- 11 players, both individually under their own max,
// but a legal XI needs at least 1 FWD, and by the time the 11th slot filled there was no room
// left for one. This checks not just "does this stay under max" but "would filling this slot
// make it structurally impossible to still reach every position's minimum" -- i.e. it reserves
// capacity for whatever's still required elsewhere before letting a slot go to something else.
function draftXiCanAdd(position, counts) {
  if ((counts[position] || 0) >= draftXiMax[position]) return false;
  const nextCounts = { ...counts, [position]: (counts[position] || 0) + 1 };
  const nextTotal = Object.values(nextCounts).reduce(
    (sum, count) => sum + count,
    0,
  );
  if (nextTotal > 11) return false;
  const stillNeeded = Object.keys(draftXiMin).reduce(
    (sum, pos) => sum + Math.max(0, draftXiMin[pos] - (nextCounts[pos] || 0)),
    0,
  );
  return nextTotal + stillNeeded <= 11;
}

function draftXiPositionCounts(ids, squadById) {
  const counts = { GKP: 0, DEF: 0, MID: 0, FWD: 0 };
  ids.forEach((id) => {
    const player = squadById[id];
    if (player)
      counts[player.position_short] = (counts[player.position_short] || 0) + 1;
  });
  return counts;
}

// Per live feedback: benching the only starting GKP left the XI with zero goalkeepers even
// though the squad always carries a second one on the bench -- the user had to separately find
// and click "Move to starting XI" on it themselves. Auto-promotes only when there's exactly one
// unambiguous replacement (true for GKP by construction: draftQuotas has GKP:2 and draftXiMax
// caps the XI at 1, so there's never more than one benched GKP to choose between). Positions with
// more than one candidate on the bench are deliberately left alone -- picking among several DEF/
// MID/FWD options is a real choice the user should make, not one to guess on their behalf.
function draftAutoFillAfterBench(benchedPosition, benchedId, squadById) {
  const counts = draftXiPositionCounts(draftStartingIds, squadById);
  if ((counts[benchedPosition] || 0) >= draftXiMin[benchedPosition]) return;
  const candidates = draftSelection
    .filter((id) => id !== benchedId && !draftStartingIds.includes(id))
    .map((id) => squadById[id])
    .filter((player) => player && player.position_short === benchedPosition);
  if (candidates.length === 1 && draftXiCanAdd(benchedPosition, counts))
    draftStartingIds = [...draftStartingIds, candidates[0].id];
}

// Seeds the session-only starting XI/captain/vice from the model's own already-computed best XI
// for the roll scenario (`_lineup_view`'s `starting_xi`/`captain`/`vice_captain`) -- a better
// starting point than reimplementing a second best-XI search in JS, and still fully "user
// manually designates their own XI" per the plan's decision, since this is only the initial seed
// the user then freely edits. Re-seeds (overwriting whatever build-phase XI/bench arrangement was
// in progress) only when this is the first render with a real, saved squad -- see the
// `draftPitchSeededFor` guard below. This is also what has to be left alone (not forced open) on
// every subsequent save for the *same* squad -- see the issue #220 fix note above, in
// `setupDraftSquad`'s save handler -- or a user's post-first-save C/VC/XI edits get silently
// reseeded back to the model's recommendation on every re-save.
function seedDraftPitch(roll) {
  const key = (roll.squad || [])
    .map((player) => player.id)
    .slice()
    .sort((a, b) => a - b)
    .join(",");
  if (draftPitchSeededFor === key) return;
  draftStartingIds = (roll.starting_xi || []).map((player) => player.id);
  draftCaptainId = roll.captain && roll.captain.id;
  draftViceId = roll.vice_captain && roll.vice_captain.id;
  draftPendingSwapId = null;
  draftPitchSeededFor = key;
}

function draftPitchCardHtml(player) {
  const isCaptain = player.id === draftCaptainId;
  const isVice = player.id === draftViceId;
  const role = isCaptain ? "C" : isVice ? "VC" : "";
  return `<div class="pitch-player ${isCaptain ? "captain" : ""}" title="${esc(player.name)} · ${esc(player.club)}"><strong>${esc(player.name)}${role ? ` (${role})` : ""}</strong><span>${esc(player.club)}</span><span class="projection projection-full">${Number(player.xp_1).toFixed(1)} / ${Number(player.xp_3).toFixed(1)} / ${Number(player.xp_5).toFixed(1)}</span><span class="projection projection-compact">${Number(player.xp_1).toFixed(1)} xPts</span><div class="pitch-player-actions"><button type="button" data-draft-swap-out="${player.id}" ${draftPendingSwapId == null ? "disabled" : ""} title="Swap in the selected bench player">${draftPendingSwapId == null ? "Bench" : "Swap in"}</button><button type="button" data-draft-captain="${player.id}" ${isCaptain ? "disabled" : ""}>C</button><button type="button" data-draft-vice="${player.id}" ${isVice || isCaptain ? "disabled" : ""}>VC</button><button type="button" data-draft-remove="${player.id}" title="Remove from squad">Remove</button></div></div>`;
}

function draftBenchCardHtml(player, index) {
  const pending = player.id === draftPendingSwapId;
  return `<div class="weekly-bench-card ${pending ? "draft-bench-pending" : ""}"><strong>${index + 1}. ${esc(player.name)}</strong><span>${esc(player.position_short)} · ${esc(player.club)}</span><span>${Number(player.xp_1).toFixed(1)} / ${Number(player.xp_3).toFixed(1)} / ${Number(player.xp_5).toFixed(1)} xPts</span><div class="pitch-player-actions"><button type="button" data-draft-swap-in="${player.id}">${pending ? "Cancel swap" : "Move to starting XI"}</button><button type="button" data-draft-remove="${player.id}" title="Remove from squad">Remove</button></div></div>`;
}

function draftSwapLegal(squadById, outgoingId, incomingId) {
  const outgoing = squadById[outgoingId];
  const incoming = squadById[incomingId];
  if (!outgoing || !incoming) return false;
  if (outgoing.position_short === incoming.position_short) return true;
  const counts = draftXiPositionCounts(draftStartingIds, squadById);
  const afterOut = (counts[outgoing.position_short] || 0) - 1;
  const afterIn = (counts[incoming.position_short] || 0) + 1;
  return (
    afterOut >= draftXiMin[outgoing.position_short] &&
    afterIn <= draftXiMax[incoming.position_short]
  );
}

function draftFormationLabel(startingPlayers) {
  const counts = { DEF: 0, MID: 0, FWD: 0 };
  startingPlayers.forEach((player) => {
    const key = player.position_short;
    if (key === "DEF" || key === "MID" || key === "FWD") counts[key] += 1;
  });
  return `${counts.DEF}-${counts.MID}-${counts.FWD}`;
}

// Once a complete, legal draft is saved, `renderDraftPitchSaved` takes over with real model
// projections and the swap-pending interaction (real FPL formation legality enforced on every
// swap). Before that, `renderDraftPitchBuilding` shows the same pitch/bench layout against
// whatever's been added so far (no projections -- see the "Live projections" decision), with
// simple, independent per-card Bench/Start buttons instead of a swap pair, since the squad isn't
// required to be exactly 11 starters while it's still being built.
// `draftSelection` may have moved on from `roll.squad` since the last save (a Remove click
// doesn't require an immediate re-save) -- filtering both starters and bench down to players
// still in `draftSelection` means a removal disappears from the pitch right away, using the
// same cached projections, rather than staying stale until the next save+reload.
function renderDraftPitchSaved(roll) {
  seedDraftPitch(roll);
  const squadById = {};
  (roll.squad || []).forEach((player) => {
    squadById[player.id] = player;
  });
  const currentIds = new Set(draftSelection);
  const startingPlayers = draftStartingIds
    .filter((id) => currentIds.has(id))
    .map((id) => squadById[id])
    .filter(Boolean);
  const benchPlayers = (roll.squad || []).filter(
    (player) =>
      currentIds.has(player.id) && !draftStartingIds.includes(player.id),
  );
  byId("draft-pitch-formation").textContent =
    `${draftFormationLabel(startingPlayers)} · xPts shown for 1 / 3 / 5 GWs`;
  byId("draft-pitch").setAttribute(
    "aria-label",
    `${draftFormationLabel(startingPlayers)} draft formation: ${startingPlayers.map((player) => `${player.name}${player.id === draftCaptainId ? " captain" : player.id === draftViceId ? " vice-captain" : ""}`).join(", ")}`,
  );
  byId("draft-pitch").innerHTML = ["FWD", "MID", "DEF", "GKP"]
    .map(
      (position) =>
        `<div class="pitch-row pitch-${position.toLowerCase()}">${startingPlayers
          .filter((player) => player.position_short === position)
          .map(draftPitchCardHtml)
          .join("")}</div>`,
    )
    .join("");
  byId("draft-bench").innerHTML = benchPlayers.map(draftBenchCardHtml).join("");
  document.querySelectorAll("[data-draft-swap-in]").forEach((button) =>
    button.addEventListener("click", () => {
      const id = Number(button.dataset.draftSwapIn);
      draftPendingSwapId = draftPendingSwapId === id ? null : id;
      renderDraftPitch();
    }),
  );
  document.querySelectorAll("[data-draft-swap-out]").forEach((button) => {
    if (button.disabled) return;
    const outId = Number(button.dataset.draftSwapOut);
    if (!draftSwapLegal(squadById, outId, draftPendingSwapId)) {
      button.disabled = true;
      button.title =
        "Swapping this player out would leave an illegal formation";
      return;
    }
    button.addEventListener("click", () => {
      const inId = draftPendingSwapId;
      draftStartingIds = draftStartingIds.map((id) =>
        id === outId ? inId : id,
      );
      if (draftCaptainId === outId)
        draftCaptainId =
          draftViceId && draftStartingIds.includes(draftViceId)
            ? draftViceId
            : draftStartingIds[0];
      if (
        draftViceId === outId ||
        (draftCaptainId !== null && draftViceId === draftCaptainId)
      )
        draftViceId =
          draftStartingIds.find((id) => id !== draftCaptainId) || null;
      draftPendingSwapId = null;
      renderDraftPitch();
    });
  });
  document.querySelectorAll("[data-draft-captain]").forEach((button) =>
    button.addEventListener("click", () => {
      const id = Number(button.dataset.draftCaptain);
      if (draftViceId === id) draftViceId = draftCaptainId;
      draftCaptainId = id;
      renderDraftPitch();
    }),
  );
  document.querySelectorAll("[data-draft-vice]").forEach((button) =>
    button.addEventListener("click", () => {
      const id = Number(button.dataset.draftVice);
      if (id === draftCaptainId) return;
      draftViceId = id;
      renderDraftPitch();
    }),
  );
  document
    .querySelectorAll("[data-draft-remove]")
    .forEach((button) =>
      button.addEventListener("click", () =>
        removeDraftPlayer(Number(button.dataset.draftRemove)),
      ),
    );
}

function draftPitchCardHtmlBuilding(player) {
  const isCaptain = player.id === draftCaptainId;
  const isVice = player.id === draftViceId;
  const role = isCaptain ? "C" : isVice ? "VC" : "";
  return `<div class="pitch-player ${isCaptain ? "captain" : ""}" title="${esc(player.name)} · ${esc(player.club)}"><strong>${esc(player.name)}${role ? ` (${role})` : ""}</strong><span>${esc(player.position_short)} · ${esc(player.club)}</span><div class="pitch-player-actions"><button type="button" data-draft-bench="${player.id}">Bench</button><button type="button" data-draft-captain="${player.id}" ${isCaptain ? "disabled" : ""}>C</button><button type="button" data-draft-vice="${player.id}" ${isVice || isCaptain ? "disabled" : ""}>VC</button><button type="button" data-draft-remove="${player.id}" title="Remove from squad">Remove</button></div></div>`;
}

function draftBenchCardHtmlBuilding(player, index) {
  const startingSquadById = Object.fromEntries(
    draftStartingIds.map((startId) => [startId, draftPlayerById(startId)]),
  );
  const counts = draftXiPositionCounts(draftStartingIds, startingSquadById);
  const canAdd = draftXiCanAdd(player.position_short, counts);
  const blocked = !canAdd;
  const reason =
    draftStartingIds.length >= 11
      ? "Bench a starter first -- the starting XI already has 11 players"
      : (counts[player.position_short] || 0) >=
          draftXiMax[player.position_short]
        ? `Only ${draftXiMax[player.position_short]} ${player.position_short} can start`
        : "Starting this player would leave no room for a required position -- bench someone from a position already at its minimum first";
  return `<div class="weekly-bench-card"><strong>${index + 1}. ${esc(player.name)}</strong><span>${esc(player.position_short)} · ${esc(player.club)}</span><div class="pitch-player-actions"><button type="button" data-draft-start="${player.id}" ${blocked ? "disabled" : ""} title="${blocked ? reason : ""}">Move to starting XI</button><button type="button" data-draft-remove="${player.id}" title="Remove from squad">Remove</button></div></div>`;
}

function renderDraftPitchBuilding() {
  const squadPlayers = draftSelection.map(draftPlayerById).filter(Boolean);
  byId("draft-pitch-formation").textContent = draftSelection.length
    ? `${draftFormationLabel(draftStartingIds.map(draftPlayerById).filter(Boolean))} so far`
    : "";
  if (!squadPlayers.length) {
    // Bug fix: this early return used to skip clearing #draft-bench entirely -- removing the very
    // last player left its stale bench card (and stale Remove-button listener, pointing at an id no
    // longer in draftSelection) on screen, making a second click on it a silent no-op. Both
    // containers must be cleared together whenever the squad is empty, not just the pitch.
    byId("draft-pitch").hidden = true;
    byId("draft-pitch").innerHTML = "";
    byId("draft-bench").innerHTML = "";
    return;
  }
  const startingPlayers = draftStartingIds.map(draftPlayerById).filter(Boolean);
  const benchPlayers = squadPlayers.filter(
    (player) => !draftStartingIds.includes(player.id),
  );
  byId("draft-pitch").hidden = false;
  byId("draft-pitch").setAttribute(
    "aria-label",
    `Draft pitch in progress: ${startingPlayers.map((player) => player.name).join(", ") || "no starters chosen yet"}`,
  );
  byId("draft-pitch").innerHTML = ["FWD", "MID", "DEF", "GKP"]
    .map(
      (position) =>
        `<div class="pitch-row pitch-${position.toLowerCase()}">${startingPlayers
          .filter((player) => player.position_short === position)
          .map(draftPitchCardHtmlBuilding)
          .join("")}</div>`,
    )
    .join("");
  byId("draft-bench").innerHTML = benchPlayers
    .map(draftBenchCardHtmlBuilding)
    .join("");
  document.querySelectorAll("[data-draft-bench]").forEach((button) =>
    button.addEventListener("click", () => {
      const id = Number(button.dataset.draftBench);
      const benched = draftPlayerById(id);
      draftStartingIds = draftStartingIds.filter((existing) => existing !== id);
      if (draftCaptainId === id) draftCaptainId = draftStartingIds[0] || null;
      if (
        draftViceId === id ||
        (draftCaptainId !== null && draftViceId === draftCaptainId)
      )
        draftViceId =
          draftStartingIds.find((existing) => existing !== draftCaptainId) ||
          null;
      if (benched) {
        const squadById = Object.fromEntries(
          draftSelection.map((playerId) => [
            playerId,
            draftPlayerById(playerId),
          ]),
        );
        draftAutoFillAfterBench(benched.position_short, id, squadById);
      }
      renderDraftPitch();
    }),
  );
  document.querySelectorAll("[data-draft-start]").forEach((button) => {
    if (button.disabled) return;
    button.addEventListener("click", () => {
      const id = Number(button.dataset.draftStart);
      draftStartingIds = [...draftStartingIds, id];
      renderDraftPitch();
    });
  });
  document.querySelectorAll("[data-draft-captain]").forEach((button) =>
    button.addEventListener("click", () => {
      const id = Number(button.dataset.draftCaptain);
      if (draftViceId === id) draftViceId = draftCaptainId;
      draftCaptainId = id;
      renderDraftPitch();
    }),
  );
  document.querySelectorAll("[data-draft-vice]").forEach((button) =>
    button.addEventListener("click", () => {
      const id = Number(button.dataset.draftVice);
      if (id === draftCaptainId) return;
      draftViceId = id;
      renderDraftPitch();
    }),
  );
  document
    .querySelectorAll("[data-draft-remove]")
    .forEach((button) =>
      button.addEventListener("click", () =>
        removeDraftPlayer(Number(button.dataset.draftRemove)),
      ),
    );
}

function renderDraftPitch() {
  const empty = byId("draft-pitch-empty");
  const roll = draftRollScenario();
  if (roll && draftSquadMatchesSaved(roll)) {
    empty.hidden = true;
    byId("draft-pitch").hidden = false;
    renderDraftPitchSaved(roll);
    return;
  }
  empty.hidden = !!draftSelection.length;
  renderDraftPitchBuilding();
}

