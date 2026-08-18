const curatedTimezones = [
  "UTC",
  "America/New_York",
  "America/Chicago",
  "America/Denver",
  "America/Los_Angeles",
  "America/Sao_Paulo",
  "America/Mexico_City",
  "America/Toronto",
  "Europe/London",
  "Europe/Dublin",
  "Europe/Paris",
  "Europe/Berlin",
  "Europe/Madrid",
  "Europe/Rome",
  "Europe/Amsterdam",
  "Europe/Moscow",
  "Africa/Cairo",
  "Africa/Johannesburg",
  "Africa/Lagos",
  "Asia/Dubai",
  "Asia/Kolkata",
  "Asia/Shanghai",
  "Asia/Tokyo",
  "Asia/Singapore",
  "Australia/Sydney",
  "Pacific/Auckland",
];
// Issue-driven removal: the "Confirmed free transfers"/"Free transfers gameweek" override let a
// visitor correct the app's own estimate of their free-transfer balance (derive_free_transfers in
// transfer_decisions.py can be wrong for an unpublished in-Gameweek transfer FPL's public API
// doesn't expose yet) -- but per request, dropped from the form entirely. The backend fields stay
// nullable and untouched (profiles.py, server.py's /api/profile, transfer_decisions.py's fallback
// to the derived estimate when unset) -- this is a UI-only removal, not a schema change; simply
// never sending these two keys is already handled the same as an explicit null server-side.
function setupProfileForm() {
  const teamIdInput = byId("profile-team-id");
  const timezoneSelect = byId("profile-timezone");
  const riskSelect = byId("profile-risk");
  const saveButton = byId("profile-save");
  const message = byId("profile-message");
  const profile = state.profile || {};
  const currentTimezone =
    profile.timezone || state.timezone || "America/New_York";
  const timezoneOptions = curatedTimezones.includes(currentTimezone)
    ? curatedTimezones
    : [...curatedTimezones, currentTimezone];
  timezoneSelect.innerHTML = timezoneOptions
    .map((zone) => `<option value="${esc(zone)}">${esc(zone)}</option>`)
    .join("");
  teamIdInput.value = profile.team_id ?? "";
  timezoneSelect.value = currentTimezone;
  riskSelect.value = profile.risk_profile || "balanced";
  const controls = [teamIdInput, timezoneSelect, riskSelect, saveButton];
  if (!servedLive()) {
    controls.forEach((control) => (control.disabled = true));
    message.textContent =
      "Start the local dashboard service to edit your profile.";
    return;
  }
  byId("profile-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    message.textContent = "";
    const rawTeamId = teamIdInput.value.trim();
    if (!rawTeamId) {
      message.textContent = "Enter your FPL team ID to save settings.";
      return;
    }
    if (
      !/^[0-9]+$/.test(rawTeamId) ||
      Number(rawTeamId) < 1 ||
      Number(rawTeamId) > 99999999
    ) {
      message.textContent =
        "Enter a valid FPL team ID (a positive whole number).";
      return;
    }
    const payload = {
      team_id: Number(rawTeamId),
      timezone: timezoneSelect.value,
      risk_profile: riskSelect.value,
    };
    saveButton.disabled = true;
    message.textContent = "Saving…";
    try {
      const response = await fetch("/api/profile", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const responsePayload = await response
        .json()
        .catch(() => ({
          message: "Profile save returned an unreadable response.",
        }));
      if (!response.ok)
        throw new Error(
          responsePayload.message ||
            `Profile save failed with status ${response.status}`,
        );
      message.textContent = "Profile saved. Reloading…";
      window.setTimeout(() => window.location.reload(), 400);
    } catch (error) {
      saveButton.disabled = false;
      message.textContent = `Save failed: ${error.message}`;
    }
  });
}
function setupReminderForm() {
  const panel = byId("reminder-content");
  let showForm = false;
  function postReminder(payload) {
    return fetch("/api/reminder-opt-in", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).then(async (response) => {
      const responsePayload = await response
        .json()
        .catch(() => ({
          message: "Reminder update returned an unreadable response.",
        }));
      if (!response.ok)
        throw new Error(
          responsePayload.message ||
            `Reminder update failed with status ${response.status}`,
        );
      return responsePayload;
    });
  }
  function render() {
    const profile = state.profile || {};
    const status = profile.reminder_status || null;
    const teamId = profile.team_id;
    if (!teamId) {
      panel.innerHTML =
        '<div class="empty">Connect your FPL team ID above to set up deadline reminders.</div>';
      return;
    }
    if (status === "pending" && !showForm) {
      panel.innerHTML = `<div class="empty">Check your inbox at <strong>${esc(profile.reminder_pending_email || "the address you entered")}</strong> to confirm.</div><div style="display:flex;gap:14px;margin-top:10px;flex-wrap:wrap"><button id="reminder-resend" class="reset-filters" type="button">Resend or change</button><button id="reminder-cancel" class="reset-filters" type="button">Cancel</button></div><div id="reminder-message" class="refresh-message" role="status" aria-live="polite"></div>`;
      byId("reminder-resend").addEventListener("click", () => {
        showForm = true;
        render();
      });
      byId("reminder-cancel").addEventListener("click", async () => {
        const message = byId("reminder-message");
        message.textContent = "Cancelling…";
        try {
          await postReminder({ team_id: teamId, action: "decline" });
          message.textContent = "Cancelled. Reloading…";
          window.setTimeout(() => window.location.reload(), 400);
        } catch (error) {
          message.textContent = `Cancel failed: ${error.message}`;
        }
      });
      return;
    }
    if (status === "enabled" && !showForm) {
      panel.innerHTML = `<div class="empty">Reminders are on for <strong>${esc(profile.email || "")}</strong>, T-${esc(String(profile.reminder_lead_hours || 3))}h before each deadline.</div><div style="display:flex;gap:14px;margin-top:10px;flex-wrap:wrap"><button id="reminder-change" class="reset-filters" type="button">Change email or lead time</button><button id="reminder-disable" class="reset-filters" type="button">Disable</button></div><div id="reminder-message" class="refresh-message" role="status" aria-live="polite"></div>`;
      byId("reminder-change").addEventListener("click", () => {
        showForm = true;
        render();
      });
      byId("reminder-disable").addEventListener("click", async () => {
        const message = byId("reminder-message");
        message.textContent = "Disabling…";
        try {
          await postReminder({ team_id: teamId, action: "disable" });
          message.textContent = "Disabled. Reloading…";
          window.setTimeout(() => window.location.reload(), 400);
        } catch (error) {
          message.textContent = `Disable failed: ${error.message}`;
        }
      });
      return;
    }
    if (status === "declined" && !showForm) {
      panel.innerHTML =
        '<div class="empty">You have opted out of deadline reminders.</div><div style="margin-top:10px"><button id="reminder-reconsider" class="refresh-button" type="button">Reconsider</button></div>';
      byId("reminder-reconsider").addEventListener("click", () => {
        showForm = true;
        render();
      });
      return;
    }
    const currentEmail = profile.email || profile.reminder_pending_email || "";
    const currentLead = profile.reminder_lead_hours || 3;
    const cancelButtonHtml = status
      ? '<button id="reminder-back" class="reset-filters" type="button">Back</button>'
      : '<button id="reminder-no-thanks" class="reset-filters" type="button">No thanks</button>';
    let selectedLead = currentLead;
    panel.innerHTML = `<form id="reminder-form"><div class="profile-form-grid"><div class="field"><label for="reminder-email">Email</label><input id="reminder-email" type="email" placeholder="you@example.com" value="${esc(currentEmail)}"></div><div class="field"><label>Lead time</label><div id="reminder-lead-options" class="profile-options" style="margin-top:6px" role="radiogroup" aria-label="Reminder lead time">${[3, 12, 24].map((hours) => `<button id="reminder-lead-tab-${hours}" type="button" class="profile-option ${hours === currentLead ? "active" : ""}" role="radio" aria-checked="${hours === currentLead}" data-lead-hours="${hours}"><strong>T-${hours}h</strong><span>before deadline</span></button>`).join("")}</div></div></div><div style="display:flex;gap:14px;align-items:center;margin-top:12px;flex-wrap:wrap"><button id="reminder-save" class="refresh-button" type="submit">Get reminders</button>${cancelButtonHtml}</div><div id="reminder-message" class="refresh-message" role="status" aria-live="polite"></div></form>`;
    bindTabs("reminder-lead-options", "[data-lead-hours]", (button) => {
      selectedLead = Number(button.dataset.leadHours);
      byId("reminder-lead-options")
        .querySelectorAll("[data-lead-hours]")
        .forEach((option) => {
          const active = option === button;
          option.classList.toggle("active", active);
          option.setAttribute("aria-checked", String(active));
        });
    });
    const backButton = byId("reminder-back");
    if (backButton)
      backButton.addEventListener("click", () => {
        showForm = false;
        render();
      });
    const noThanksButton = byId("reminder-no-thanks");
    if (noThanksButton)
      noThanksButton.addEventListener("click", async () => {
        const message = byId("reminder-message");
        message.textContent = "Saving…";
        try {
          await postReminder({ team_id: teamId, action: "decline" });
          message.textContent = "Got it. Reloading…";
          window.setTimeout(() => window.location.reload(), 400);
        } catch (error) {
          message.textContent = `Save failed: ${error.message}`;
        }
      });
    if (!servedLive()) {
      byId("reminder-form")
        .querySelectorAll("button,input")
        .forEach((control) => (control.disabled = true));
      byId("reminder-message").textContent =
        "Start the local dashboard service to set up reminders.";
      return;
    }
    byId("reminder-form").addEventListener("submit", async (event) => {
      event.preventDefault();
      const message = byId("reminder-message");
      const email = byId("reminder-email").value.trim();
      if (!email || !email.includes("@")) {
        message.textContent = "Enter a valid email address.";
        return;
      }
      const leadHours = selectedLead;
      const saveButton = byId("reminder-save");
      saveButton.disabled = true;
      message.textContent = "Sending confirmation email…";
      try {
        await postReminder({
          team_id: teamId,
          action: "enable",
          email,
          lead_hours: leadHours,
        });
        message.textContent = "Check your inbox to confirm. Reloading…";
        window.setTimeout(() => window.location.reload(), 600);
      } catch (error) {
        saveButton.disabled = false;
        message.textContent = `Request failed: ${error.message}`;
      }
    });
  }
  render();
}
function setupContactForm() {
  const categorySelect = byId("contact-category");
  const emailInput = byId("contact-email");
  const messageInput = byId("contact-message");
  const saveButton = byId("contact-save");
  const message = byId("contact-message-status");
  const form = byId("contact-form");
  if (!servedLive()) {
    [categorySelect, emailInput, messageInput, saveButton].forEach(
      (control) => (control.disabled = true),
    );
    message.textContent =
      "Start the local dashboard service to send a message.";
    return;
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    message.textContent = "";
    const category = categorySelect.value;
    const text = messageInput.value.trim();
    const email = emailInput.value.trim();
    if (!text) {
      message.textContent = "Enter a message before sending.";
      return;
    }
    if (email && !email.includes("@")) {
      message.textContent = "Enter a valid email address, or leave it blank.";
      return;
    }
    const payload = { category, message: text };
    if (email) payload.reply_to = email;
    saveButton.disabled = true;
    message.textContent = "Sending…";
    try {
      const response = await fetch("/api/contact", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const responsePayload = await response
        .json()
        .catch(() => ({
          message: "Message send returned an unreadable response.",
        }));
      if (!response.ok)
        throw new Error(
          responsePayload.message ||
            `Message send failed with status ${response.status}`,
        );
      message.textContent =
        responsePayload.message || "Thanks -- your message has been received.";
      form.reset();
      categorySelect.value = "bug";
    } catch (error) {
      message.textContent = `Send failed: ${error.message}`;
    } finally {
      saveButton.disabled = false;
    }
  });
}
function renderLookupBanner() {
  const banner = byId("lookup-banner");
  const lookup = state.lookup;
  if (!lookup || !lookup.active) {
    banner.hidden = true;
    banner.innerHTML = "";
    return;
  }
  banner.hidden = false;
  const manager = state.manager || {};
  const teamLabel =
    lookup.status === "ok" && manager.team_name
      ? `${esc(manager.team_name)} (team ID ${esc(lookup.team_id)})`
      : `team ID ${esc(lookup.team_id)}`;
  if (lookup.status === "ok") {
    banner.innerHTML = `<strong>Viewing ${teamLabel} · one-off lookup</strong><a href="/">Back to your own team</a>`;
  } else if (lookup.status === "opted_out") {
    banner.innerHTML = `<strong>This manager has opted out of lookup recommendations.</strong><a href="/">Back to your own team</a>`;
  } else {
    banner.innerHTML = `<strong>Couldn't look up team ID ${esc(lookup.team_id)}</strong>Team not found, or the official FPL API is temporarily unavailable. <a href="/">Back to your own team</a>`;
  }
}
function setupTeamLookup() {
  const form = byId("team-lookup-form");
  const input = byId("team-lookup-input");
  const message = byId("team-lookup-message");
  const params = new URLSearchParams(window.location.search);
  const currentTeamId = params.get("team_id");
  if (currentTeamId) input.value = currentTeamId;
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    message.textContent = "";
    const raw = input.value.trim();
    if (
      !/^[0-9]{1,8}$/.test(raw) ||
      Number(raw) < 1 ||
      Number(raw) > 99999999
    ) {
      message.textContent =
        "Enter a valid FPL team ID (a positive whole number).";
      return;
    }
    const url = new URL(window.location.href);
    url.search = "";
    url.searchParams.set("team_id", raw);
    window.location.href = url.toString();
  });
}
function setupThemeToggle() {
  const button = byId("theme-toggle");
  const applyState = (theme) => {
    button.setAttribute("aria-checked", String(theme === "dark"));
    button.setAttribute(
      "aria-label",
      theme === "dark" ? "Dark theme" : "Light theme",
    );
  };
  applyState(document.documentElement.getAttribute("data-theme") || "dark");
  button.addEventListener("click", () => {
    const next =
      document.documentElement.getAttribute("data-theme") === "light"
        ? "dark"
        : "light";
    document.documentElement.setAttribute("data-theme", next);
    try {
      localStorage.setItem("fpl-theme", next);
    } catch (error) {}
    applyState(next);
  });
}
