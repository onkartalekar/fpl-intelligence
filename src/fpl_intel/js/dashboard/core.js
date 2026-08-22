const state = JSON.parse(document.getElementById("dashboard-data").textContent);
const transfers = state.transfers || [];
const players = state.players || [];
const fixtures = state.fixtures || [];
const releaseNotes = state.release_notes || [];
let whatsNewFilter = "all";
const decision = state.decision_center || { status: "model_unavailable" };
const performance = state.model_performance || {
  status: "waiting_for_results",
  comparisons: [],
  summary: {},
  by_horizon: {},
  calibration: { recommendations: [] },
  team_performance: { comparisons: [] },
  player_performance: { comparisons: [] },
};
const byId = (id) => document.getElementById(id);
const esc = (value) =>
  String(value ?? "").replace(
    /[&<>"']/g,
    (char) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        char
      ],
  );
// Bug fix: player search used a plain substring match with no accent-folding, so a query could
// never match *through* an accented character in a name -- "Guehi" never matched "Guéhi" because
// the plain "e" you typed is never === the "é" in the name. A query that only needed the
// ASCII-prefix of a name (e.g. "guimar" against "Guimarães") looked like it worked by
// coincidence, since it never reached the accent at all. Strip diacritics from both sides before
// comparing so every query works the same regardless of where an accent falls in the name.
// Bug fix: NFD normalization only decomposes a base letter plus a combining mark (e.g.
// e + U+0301 -> "é"), so the .replace below only ever strips combining marks. Letters like
// "Ø", "Æ", "Œ", "ß", "Đ", "Ł" are single codepoints with no such decomposition -- NFD leaves
// them completely untouched, so "Ødegaard" never matched a search for "ode" even though the
// same fold above already fixed accented letters like the "é" in "Guéhi". Map those explicitly
// before the NFD pass runs.
const specialLetterFold = {
  ø: "o",
  Ø: "O",
  æ: "ae",
  Æ: "AE",
  œ: "oe",
  Œ: "OE",
  ß: "ss",
  đ: "d",
  Đ: "D",
  ł: "l",
  Ł: "L",
  ð: "d",
  Ð: "D",
  þ: "th",
  Þ: "Th",
};
const foldDiacritics = (value) =>
  String(value ?? "")
    .replace(/[øØæÆœŒßđĐłŁðÐþÞ]/g, (char) => specialLetterFold[char])
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "");
const pageSize = 20;
let page = 1;
let playerPage = 1;
let selectedBreakdownPlayerId = null;
let selectedRationaleMap = {};
const prefersReducedMotion = () =>
  window.matchMedia &&
  window.matchMedia("(prefers-reduced-motion: reduce)").matches;
const trustedLinkDomains = new Set(__TRUSTED_LINK_DOMAINS__);
function safeLink(url, label) {
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.toLowerCase().replace(/^www\./, "");
    if (parsed.protocol === "https:" && trustedLinkDomains.has(host))
      return `<a href="${esc(parsed.href)}" target="_blank" rel="noopener noreferrer">${esc(label)}</a>`;
  } catch (error) {}
  return `<span>${esc(label)} · invalid or untrusted source URL</span>`;
}
const titles = {
  overview:
    state.fpl.season_phase === "in_season"
      ? "Season overview"
      : "Preseason overview",
  decisions: "Decision Center",
  squad: "My Team",
  draft: "Draft Squad",
  profile: "My Profile",
  players: "Player Explorer",
  fixtures: "Fixtures",
  transfers: "Transfers & News",
  performance: "Model Performance",
  model: "Model Status",
  contact: "Contact Us",
  "whats-new": "What's New",
};
const seasonLabel = () =>
  state.fpl.ready_for_2026_27
    ? state.fpl.season_phase === "in_season"
      ? "2026/27 season active"
      : "2026/27 FPL feed ready"
    : "Waiting for 2026/27 FPL launch";
const timezoneLabel =
  state.timezone === "America/New_York"
    ? "Eastern Time (New York)"
    : state.timezone;
byId("topbar-timezone").textContent = timezoneLabel;
byId("topbar-risk").textContent =
  `${(state.profile && state.profile.risk_profile) || "balanced"} risk`;
function showView(name) {
  document
    .querySelectorAll(".view")
    .forEach((node) =>
      node.classList.toggle("active", node.id === `view-${name}`),
    );
  document.querySelectorAll("[data-view]").forEach((node) => {
    const active = node.dataset.view === name;
    node.classList.toggle("active", active);
    active
      ? node.setAttribute("aria-current", "page")
      : node.removeAttribute("aria-current");
  });
  byId("mobile-nav").value = name;
  byId("view-title").textContent = titles[name] || "FPL Intelligence";
  if (name === "transfers") applyFilters();
  if (name === "whats-new") renderWhatsNew();
  if (name === "draft") {
    renderDraftHealth();
    renderDraftPitch();
  }
  if (typeof syncMobileChrome === "function") syncMobileChrome(name);
  window.scrollTo({ top: 0, behavior: "smooth" });
}
document
  .querySelectorAll("[data-view]")
  .forEach((button) =>
    button.addEventListener("click", () => showView(button.dataset.view)),
  );
byId("mobile-nav").addEventListener("change", (event) =>
  showView(event.target.value),
);
function fmtDate(value, withTime = false) {
  if (!value) return "Not recorded";
  const options = withTime
    ? {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        timeZone: state.timezone,
        timeZoneName: "short",
      }
    : {
        month: "short",
        day: "numeric",
        year: "numeric",
        timeZone: state.timezone,
      };
  return new Intl.DateTimeFormat("en-US", options).format(new Date(value));
}
function deadlineText() {
  if (!state.fpl.next_deadline)
    return "Deadline appears when the 2026/27 feed launches.";
  const deadline = new Date(state.fpl.next_deadline);
  if (deadline <= Date.now())
    return `${state.fpl.next_event_name || "Deadline"} · ${fmtDate(state.fpl.next_deadline, true)} · Deadline passed. Refresh required to load the next deadline.`;
  const hours = Math.floor((deadline - Date.now()) / 3600000);
  const days = Math.floor(hours / 24);
  const remainder = hours % 24;
  return `${state.fpl.next_event_name || "Next deadline"} · ${fmtDate(state.fpl.next_deadline, true)} · ${days}d ${remainder}h remaining`;
}
