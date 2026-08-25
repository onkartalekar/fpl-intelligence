// Getting Started onboarding flow (issue #257): a guided, advisory sequence through the five
// tabs a first-time visitor actually needs -- step 1 is Draft Squad in preseason or My Profile
// once the season is underway (see buildOnboardingSteps below), then Decision Center, Player
// Explorer, My Profile (optional), What's New -- laid over the existing shell rather than
// replacing any of it. See plans/issue-257-onboarding-wizard.md for the design decisions this
// implements:
//   - Advisory, not blocking (B2): nothing here ever disables a [data-view] nav button, matching
//     applyProfileGates()'s own precedent of swapping content rather than gating navigation.
//   - Progress persisted client-side (A3), in localStorage like the theme toggle
//     (profile-forms.js), not profiles.db -- onboarding-step-done is UI chrome, not squad/model
//     data, and step 1 happens before any team_id exists to key a server row on anyway.
//   - Draft Squad first, not My Profile: neither /api/profile nor /api/draft-squad validates
//     team_id against a real FPL account (both just persist to profiles.db), so a visitor can
//     save a placeholder team_id straight from this flow and come back to My Profile later.
//   - Step 1 itself is gated on the season, not hardcoded to Draft Squad: a saved draft squad
//     only ever feeds a recommendation while build_transfer_decisions still reports
//     "waiting_for_gw2" (transfer_decisions.py, event <= 1) -- state.fpl.season_phase is only
//     "preseason" under that exact same condition (summarize_bootstrap, fpl_data.py: "preseason"
//     iff next_event.id === 1). Once the season has moved past that window (in_season -- which is
//     already true today), Draft Squad "stops being used" per its own tab copy
//     (dashboard-shell.html's #draft-purpose-banner) and recommending it here would send a
//     first-time visitor to build a squad that feeds nothing. Step 1 becomes "connect your real
//     team" (My Profile) instead.
const ONBOARDING_STORAGE_KEY = "fpl-onboarding-progress";

function buildOnboardingSteps() {
  const preseason = (state.fpl || {}).season_phase === "preseason";
  const connectStep = preseason
    ? {
        id: "team",
        title: "Build a draft squad",
        view: "draft",
        tabLabel: "Draft Squad",
        cta: "Open Draft Squad",
        requiresTeam: true,
        why: "Fifteen legal players inside £100.0m. Nothing is published to FPL — this draft is the baseline every recommendation downstream is computed from.",
      }
    : {
        id: "team",
        title: "Connect your team",
        view: "profile",
        tabLabel: "My Profile",
        cta: "Open My Profile",
        requiresTeam: true,
        why: "Enter your FPL team ID (from your FPL entry URL) to see your real squad and unlock Decision Center. Draft Squad is a preseason-only tool — it isn't used once the season is underway.",
      };
  return [
    connectStep,
    {
      id: "read",
      title: "Read the weekly decision",
      view: "decisions",
      tabLabel: "Decision Center",
      cta: "Open Decision Center",
      why: "Decision Center returns a recommended XI, a captain, and the model's reasoning. It's a recommendation only — the change is still yours to make in FPL.",
    },
    {
      id: "research",
      title: "Check prices and confirmed news",
      view: "players",
      tabLabel: "Player Explorer",
      cta: "Open Player Explorer",
      why: "Player Explorer carries official prices, ownership and availability. Transfers & News carries confirmed first-party records, nothing speculative.",
    },
    {
      id: "tune",
      title: "Set your profile",
      view: "profile",
      tabLabel: "My Profile",
      optional: true,
      cta: "Open My Profile",
      why: "Timezone decides when deadline reminders arrive; risk profile decides how much variance the model accepts. Balanced is a reasonable default.",
    },
    {
      id: "rhythm",
      title: "Expect a weekly cadence",
      view: "whats-new",
      tabLabel: "What's New",
      cta: "Open What's New",
      why: "Data refreshes on a weekly cadence and What's New records what changed. This isn't a one-time setup.",
    },
  ];
}

function loadOnboardingProgress() {
  try {
    const raw = JSON.parse(localStorage.getItem(ONBOARDING_STORAGE_KEY) || "null");
    if (raw && Array.isArray(raw.done) && typeof raw.phase === "string" && typeof raw.focus === "string") {
      return raw;
    }
  } catch (error) {}
  return null;
}

function setupOnboarding() {
  const ONBOARDING_STEPS = buildOnboardingSteps();
  const connectStep = ONBOARDING_STEPS.find((s) => s.requiresTeam) || ONBOARDING_STEPS[0];
  const welcomeDialog = byId("onboarding-welcome");
  const entryButton = byId("onboarding-entry");
  const entryProgress = byId("onboarding-entry-progress");
  const tracker = byId("onboarding-tracker");
  const card = byId("onboarding-card");
  const pill = byId("onboarding-pill");

  const hasTeam = () => !!(state.profile && state.profile.team_id);

  const saved = loadOnboardingProgress();
  const onboarding = saved || {
    done: hasTeam() ? ["team"] : [],
    // Auto-open only for a genuinely unconfigured, never-seen-this-before visitor -- anyone who
    // already has a team, or has interacted with this flow before, gets the quiet pill instead.
    // Never re-nags once dismissed (see the plan doc's B2 decision).
    phase:
      !hasTeam() && (state.manager || {}).connection_status === "not_configured"
        ? "welcome"
        : "pill",
    focus: "team",
  };
  if (hasTeam() && onboarding.done.indexOf("team") === -1) {
    onboarding.done = onboarding.done.concat(["team"]);
  }

  function persist() {
    try {
      localStorage.setItem(ONBOARDING_STORAGE_KEY, JSON.stringify(onboarding));
    } catch (error) {}
  }
  function isDone(id) {
    return onboarding.done.indexOf(id) !== -1;
  }
  function allDone() {
    return onboarding.done.length === ONBOARDING_STEPS.length;
  }
  function currentStep() {
    return ONBOARDING_STEPS.find((s) => s.id === onboarding.focus) || ONBOARDING_STEPS[0];
  }
  // Marks a step done and moves focus to the next not-yet-done step (or stays on the last one
  // once everything's done) -- called on CTA click for every step except the `requiresTeam` one,
  // which only completes via an actual saved team_id (see saveTeamId below).
  function advance(id) {
    if (!isDone(id)) onboarding.done = onboarding.done.concat([id]);
    const next = ONBOARDING_STEPS.find((s) => !isDone(s.id));
    onboarding.focus = next ? next.id : ONBOARDING_STEPS[ONBOARDING_STEPS.length - 1].id;
    persist();
    render();
  }

  function renderWelcomeSteps() {
    byId("onboarding-welcome-steps").innerHTML = ONBOARDING_STEPS.map(
      (s, i) => `
      <div class="onboarding-step-row">
        <span class="onboarding-step-n">${i + 1}</span>
        <span class="onboarding-step-title">${esc(s.title)}</span>
        <span class="onboarding-step-tab">${esc(s.tabLabel)}${s.optional ? " · optional" : ""}</span>
      </div>`,
    ).join("");
  }

  function render() {
    const done = allDone();
    const cur = currentStep();
    const curDone = isDone(cur.id);

    entryButton.hidden = onboarding.phase === "welcome";
    entryProgress.textContent = done ? "Done" : `${onboarding.done.length}/5`;
    tracker.hidden = onboarding.phase === "welcome";
    card.hidden = onboarding.phase !== "card";
    pill.hidden = onboarding.phase !== "pill";

    if (onboarding.phase !== "card") return;

    byId("onboarding-card-kicker").textContent = done
      ? "Getting started"
      : `Step ${ONBOARDING_STEPS.indexOf(cur) + 1} of 5${cur.optional ? " · optional" : ""}`;
    byId("onboarding-card-title").textContent = done ? "You're set up" : cur.title;
    byId("onboarding-card-dots").innerHTML = ONBOARDING_STEPS.map((s) => {
      const cls = isDone(s.id) ? "done" : s.id === cur.id ? "current" : "";
      return `<button type="button" class="onboarding-dot ${cls}" data-step="${s.id}" title="${esc(s.title)}" aria-label="${esc(s.title)}"></button>`;
    }).join("");
    byId("onboarding-card-dots")
      .querySelectorAll("[data-step]")
      .forEach((dot) =>
        dot.addEventListener("click", () => {
          onboarding.focus = dot.dataset.step;
          persist();
          render();
        }),
      );

    const showTeamField = !!cur.requiresTeam && !hasTeam() && !done;
    byId("onboarding-team-field").hidden = !showTeamField;
    byId("onboarding-card-why").hidden = showTeamField;
    byId("onboarding-card-why").textContent = done
      ? "From here it's one visit a week: open Decision Center before the deadline, check prices and confirmed news if something looks off, then make the change in FPL yourself."
      : cur.why;

    const primaryButton = byId("onboarding-primary");
    const markDoneButton = byId("onboarding-mark-done");
    if (done) {
      primaryButton.textContent = "Close";
      primaryButton.onclick = () => {
        onboarding.phase = "pill";
        persist();
        render();
      };
      markDoneButton.hidden = true;
    } else if (showTeamField) {
      // Step 1 only completes via an actual saved team_id -- the primary button navigates so a
      // visitor can look around Draft Squad first, but doesn't mark the step done on its own.
      primaryButton.textContent = cur.cta;
      primaryButton.onclick = () => showView(cur.view);
      markDoneButton.hidden = true;
    } else {
      primaryButton.textContent = cur.cta;
      primaryButton.onclick = () => {
        showView(cur.view);
        advance(cur.id);
      };
      markDoneButton.hidden = curDone;
      markDoneButton.onclick = () => advance(cur.id);
    }

    byId("onboarding-pill-dots").innerHTML = ONBOARDING_STEPS.map(
      (s) => `<span class="onboarding-pill-dot${isDone(s.id) ? " done" : ""}"></span>`,
    ).join("");
    byId("onboarding-pill-label").textContent = done
      ? "Getting started · done"
      : `Getting started · ${onboarding.done.length} of 5`;
  }

  // Persists team_id via the lightest existing write path -- /api/draft-squad accepts a bare
  // {team_id, player_ids: null} (draft-squad.js's own "clear" shape) to register a team_id
  // without requiring a full 15-player squad, seeding sensible profiles.db defaults for
  // timezone/risk_profile (save_draft_squad, storage/profiles.py). Mirrors draft-squad.js's own
  // post-save refresh exactly, plus applyProfileGates() -- draft-squad.js's own success path
  // doesn't re-call that, leaving #decisions-empty-state stale after an in-place save; this flow
  // calls it explicitly rather than inheriting that gap.
  async function saveTeamId(teamId, placeholder) {
    const message = byId("onboarding-team-message");
    const saveButton = byId("onboarding-team-save");
    const skipButton = byId("onboarding-team-skip");
    saveButton.disabled = true;
    skipButton.disabled = true;
    message.textContent = "Saving…";
    try {
      const response = await fetch("/api/draft-squad", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ team_id: teamId, player_ids: null }),
      });
      const responsePayload = await response
        .json()
        .catch(() => ({ message: "Save returned an unreadable response." }));
      if (!response.ok) {
        throw new Error(responsePayload.message || `Save failed with status ${response.status}`);
      }
      try {
        const refreshResponse = await fetch(`/api/manager-view?team_id=${teamId}`);
        const refreshPayload = await refreshResponse.json();
        if (refreshResponse.ok && refreshPayload.status === "ok") {
          decision.weekly_decisions = refreshPayload.weekly_decisions;
          state.manager = refreshPayload.manager;
          state.profile = state.profile || {};
          state.profile.team_id = teamId;
          renderManager();
          renderWeeklyDecision();
          renderDecision();
          applyProfileGates();
        }
      } catch (refreshError) {}
      // Placeholder IDs are frequently not real FPL entries, so the live manager lookup above
      // often lands on "lookup_failed" rather than a working summary (compute_manager_view,
      // refresh.py) -- the message below deliberately promises only what actually happened
      // (the team_id is saved), not that Decision Center will show a live recommendation yet.
      message.textContent = placeholder
        ? `Saved as ${teamId}. Swap in your real FPL team ID any time from My Profile.`
        : "Saved.";
      showView(connectStep.view);
      advance(connectStep.id);
    } catch (error) {
      message.textContent = `Save failed: ${error.message}`;
    } finally {
      saveButton.disabled = false;
      skipButton.disabled = false;
    }
  }

  entryButton.addEventListener("click", () => {
    onboarding.phase = "card";
    persist();
    render();
  });
  pill.addEventListener("click", () => {
    onboarding.phase = "card";
    persist();
    render();
  });
  byId("onboarding-collapse").addEventListener("click", () => {
    onboarding.phase = "pill";
    persist();
    render();
  });
  byId("onboarding-start").addEventListener("click", () => {
    onboarding.phase = "card";
    onboarding.focus = connectStep.id;
    persist();
    closeSheet(welcomeDialog);
    showView(connectStep.view);
    render();
  });
  byId("onboarding-not-now").addEventListener("click", () => closeSheet(welcomeDialog));
  // Covers every dismissal path (X button, backdrop tap, swipe, Escape, browser back) via the
  // dialog's own native `close` event, rather than duplicating "Not now"'s handler on each one.
  welcomeDialog.addEventListener("close", () => {
    if (onboarding.phase === "welcome") {
      onboarding.phase = "pill";
      persist();
      render();
    }
  });

  const teamIdInput = byId("onboarding-team-id");
  teamIdInput.value = (state.profile && state.profile.team_id) || "";
  teamIdInput.addEventListener("input", () => {
    teamIdInput.value = teamIdInput.value.replace(/[^0-9]/g, "");
  });
  byId("onboarding-team-save").addEventListener("click", () => {
    const raw = teamIdInput.value.trim();
    const message = byId("onboarding-team-message");
    if (!/^[0-9]+$/.test(raw) || Number(raw) < 1 || Number(raw) > 99999999) {
      message.textContent = "Enter a valid FPL team ID.";
      return;
    }
    saveTeamId(Number(raw), false);
  });
  byId("onboarding-team-skip").addEventListener("click", () => {
    // Any number in range works as a storage key (draft_squad.py's validate_draft_squad_shape
    // doesn't check FPL for existence) -- this just avoids colliding with another visitor's own
    // placeholder by picking from a wide range, not a real identity of any kind.
    const placeholderId = 10000000 + Math.floor(Math.random() * 89999999);
    teamIdInput.value = String(placeholderId);
    saveTeamId(placeholderId, true);
  });

  byId("onboarding-start").textContent = `Start with ${connectStep.tabLabel}`;
  mountSheet(welcomeDialog);
  renderWelcomeSteps();
  render();
  if (onboarding.phase === "welcome") openSheet(welcomeDialog);
}
