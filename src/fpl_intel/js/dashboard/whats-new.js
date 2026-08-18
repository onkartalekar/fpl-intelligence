function whatsNewFormattedDate(dateStr) {
  const [year, month, day] = dateStr.split("-").map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    timeZone: "UTC",
  }).format(date);
}
function whatsNewMatches(entry, query) {
  if (!query) return true;
  const haystack = [
    entry.headline,
    entry.summary,
    ...entry.changes.flatMap((change) => [
      change.title,
      change.description,
      change.category,
    ]),
  ]
    .join(" ")
    .toLowerCase();
  return haystack.includes(query.toLowerCase());
}
function whatsNewChangeRowHtml(change) {
  return `<div class="whats-new-change"><span class="whats-new-tag whats-new-tag-${esc(change.category.toLowerCase())}">${esc(change.category)}</span><div><strong>${esc(change.title)}</strong><div class="muted">${esc(change.description)}</div></div></div>`;
}
// Issue #196: split each entry's changes by who they're actually for, not by category -- a
// `Fix`/`Chore` category alone can't tell a user-visible change from an internal-only one (see
// release_notes_email.py's identical split for the full rationale, shared here so the dashboard's
// own "What's New" tab matches the email). `change.audience||'user'` defaults entries published
// before this field existed (all of history, by explicit decision -- not worth backfilling).
// Returns '' when the section has no matching changes, so an all-user-facing entry never renders
// an empty "Under the hood" heading.
function whatsNewSectionHtml(label, changes) {
  if (!changes.length) return "";
  return `<div class="whats-new-section-label">${esc(label)}</div>${changes.map(whatsNewChangeRowHtml).join("")}`;
}
function renderWhatsNew() {
  const query = (byId("whats-new-search").value || "").trim();
  const rows = releaseNotes.filter((entry) => {
    const changes =
      whatsNewFilter === "all"
        ? entry.changes
        : (entry.changes || []).filter(
            (change) => change.category === whatsNewFilter,
          );
    return changes.length > 0 && whatsNewMatches(entry, query);
  });
  byId("whats-new-count").textContent =
    `${rows.length} ${rows.length === 1 ? "entry" : "entries"}`;
  if (!rows.length) {
    byId("whats-new-entries").innerHTML = releaseNotes.length
      ? '<div class="empty">No release notes match this search or filter.</div>'
      : '<div class="empty">No release notes published yet -- check back after the next update.</div>';
    return;
  }
  byId("whats-new-entries").innerHTML = rows
    .map((entry, index) => {
      const changes =
        whatsNewFilter === "all"
          ? entry.changes
          : entry.changes.filter(
              (change) => change.category === whatsNewFilter,
            );
      const forYou = changes.filter(
        (change) => (change.audience || "user") === "user",
      );
      const underTheHood = changes.filter(
        (change) => (change.audience || "user") === "developer",
      );
      const changeRows =
        whatsNewSectionHtml("What's new for you", forYou) +
        whatsNewSectionHtml(
          "Under the hood (for the developer in you)",
          underTheHood,
        );
      return `<details class="whats-new-entry panel"${index === 0 ? " open" : ""}><summary><span class="whats-new-date">${esc(whatsNewFormattedDate(entry.date))}</span><span class="whats-new-headline">${esc(entry.headline)}</span></summary><p class="muted">${esc(entry.summary)}</p>${changeRows}</details>`;
    })
    .join("");
}
function setupWhatsNew() {
  byId("whats-new-search").addEventListener("input", renderWhatsNew);
  document.querySelectorAll("[data-whats-new-filter]").forEach((button) =>
    button.addEventListener("click", () => {
      whatsNewFilter = button.dataset.whatsNewFilter;
      document
        .querySelectorAll("[data-whats-new-filter]")
        .forEach((node) => node.classList.toggle("active", node === button));
      renderWhatsNew();
    }),
  );
  renderWhatsNew();
  setupWhatsNewSubscribe();
}
function setupWhatsNewSubscribe() {
  const emailInput = byId("whats-new-subscribe-email");
  const saveButton = byId("whats-new-subscribe-save");
  const message = byId("whats-new-subscribe-message");
  const form = byId("whats-new-subscribe-form");
  if (!servedLive()) {
    emailInput.disabled = true;
    saveButton.disabled = true;
    message.textContent = "Start the local dashboard service to subscribe.";
    return;
  }
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    message.textContent = "";
    const email = emailInput.value.trim();
    if (!email || !email.includes("@")) {
      message.textContent = "Enter a valid email address.";
      return;
    }
    saveButton.disabled = true;
    message.textContent = "Subscribing…";
    try {
      const response = await fetch("/api/release-notes-subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email }),
      });
      const responsePayload = await response
        .json()
        .catch(() => ({
          message: "Subscribe returned an unreadable response.",
        }));
      if (!response.ok)
        throw new Error(
          responsePayload.message ||
            `Subscribe failed with status ${response.status}`,
        );
      message.textContent =
        responsePayload.message || "Check your email to confirm.";
      form.reset();
    } catch (error) {
      message.textContent = `Subscribe failed: ${error.message}`;
    } finally {
      saveButton.disabled = false;
    }
  });
}
