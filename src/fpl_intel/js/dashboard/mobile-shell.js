// Issue #242: the shared mobile chrome -- a bottom tab bar plus one <dialog>-based bottom-sheet
// primitive reused by the "More" nav list, the evidence inspector, the Decision Center player
// breakdown, the transfers filter panel, and destructive-action confirmation. Everything here is
// progressive enhancement: `setupMobileShell()` (called once from gates-and-bootstrap.js) adds
// `js-mobile-shell` to <body>, which is what dashboard.css's mobile-shell rules key off of --
// without it (or above the 760px breakpoint, where those rules never apply) the page falls back
// to the `.mobile-nav-wrap` dropdown and the inline `.inspector`/`.filter-shell`/
// `.draft-builder-add-col` panels that already existed before this file did.

function isMobileShellBreakpoint() {
  return window.matchMedia("(max-width: 760px)").matches;
}

const primaryTabViews = ["squad", "decisions", "draft", "players"];
let activeViewName = "squad";
const openSheetStack = [];
const sheetReparentSlots = new Map();
let suppressNextPopstate = false;

function closeSheetInternal(dialogEl) {
  if (dialogEl.open) {
    if (typeof dialogEl.close === "function") dialogEl.close();
    else dialogEl.removeAttribute("open");
  }
  dialogEl.style.transform = "";
  const index = openSheetStack.indexOf(dialogEl);
  if (index !== -1) openSheetStack.splice(index, 1);
  const slot = sheetReparentSlots.get(dialogEl);
  if (slot) {
    slot.parent.insertBefore(slot.node, slot.nextSibling);
    sheetReparentSlots.delete(dialogEl);
  }
}
// User-initiated close (close button, backdrop tap, swipe-down, Escape): also consumes the
// history entry `openSheet()` pushed, so a later hardware/browser back doesn't land on a
// forward entry for a sheet that isn't open anymore.
function closeSheet(dialogEl) {
  if (!dialogEl.open) return;
  const wasTracked = openSheetStack.includes(dialogEl);
  closeSheetInternal(dialogEl);
  if (wasTracked) {
    suppressNextPopstate = true;
    history.back();
  }
}
function openSheet(dialogEl) {
  if (dialogEl.open) return;
  if (typeof dialogEl.showModal === "function") dialogEl.showModal();
  else dialogEl.setAttribute("open", "");
  openSheetStack.push(dialogEl);
  history.pushState({ mobileSheet: dialogEl.id }, "");
}
window.addEventListener("popstate", () => {
  if (suppressNextPopstate) {
    suppressNextPopstate = false;
    return;
  }
  // Hardware/browser back: the history entry is already consumed by the navigation itself, so
  // this only needs to run the close side effects, not push/pop history again.
  const top = openSheetStack[openSheetStack.length - 1];
  if (top) closeSheetInternal(top);
});
function attachSwipeToDismiss(handle, dialogEl) {
  let startY = null;
  handle.addEventListener(
    "touchstart",
    (event) => {
      startY = event.touches[0].clientY;
    },
    { passive: true },
  );
  handle.addEventListener(
    "touchmove",
    (event) => {
      if (startY == null) return;
      const delta = event.touches[0].clientY - startY;
      if (delta > 0) dialogEl.style.transform = `translateY(${delta}px)`;
    },
    { passive: true },
  );
  handle.addEventListener("touchend", (event) => {
    if (startY == null) return;
    const delta = event.changedTouches[0].clientY - startY;
    startY = null;
    if (delta > 60) closeSheet(dialogEl);
    else dialogEl.style.transform = "";
  });
}
function mountSheet(dialogEl) {
  dialogEl.addEventListener("cancel", (event) => {
    event.preventDefault();
    closeSheet(dialogEl);
  });
  dialogEl.addEventListener("click", (event) => {
    if (event.target === dialogEl) closeSheet(dialogEl);
  });
  dialogEl
    .querySelectorAll("[data-sheet-close]")
    .forEach((button) => button.addEventListener("click", () => closeSheet(dialogEl)));
  const handle = dialogEl.querySelector(".sheet-handle");
  if (handle) attachSwipeToDismiss(handle, dialogEl);
}

// Reparents an existing, already-wired DOM node (the evidence inspector, the Decision Center
// breakdown panel, the transfers filter panel) into the shared content sheet, then returns it to
// its original position when the sheet closes -- the node's own render logic and event listeners
// are untouched, since it's the same element, just moved.
function openContentSheet(node, title) {
  if (!node || !isMobileShellBreakpoint()) return;
  const dialogEl = byId("app-sheet");
  if (sheetReparentSlots.has(dialogEl)) closeSheetInternal(dialogEl);
  sheetReparentSlots.set(dialogEl, {
    node,
    parent: node.parentNode,
    nextSibling: node.nextSibling,
  });
  byId("app-sheet-title").textContent = title || "";
  const body = byId("app-sheet-body");
  body.innerHTML = "";
  body.appendChild(node);
  openSheet(dialogEl);
}

// A small destructive-action confirm sheet, mobile-only -- desktop keeps the immediate action
// unchanged (see draft-squad.js's clearDraftSquad and overview-transfers-players.js's
// reset-filters handler, both of which only route through this when isMobileShellBreakpoint()).
function openConfirmSheet(options) {
  const dialogEl = byId("confirm-sheet");
  byId("confirm-sheet-title").textContent = options.title;
  byId("confirm-sheet-message").textContent = options.message;
  const confirmButton = byId("confirm-sheet-confirm");
  confirmButton.textContent = options.confirmLabel;
  confirmButton.onclick = () => {
    closeSheet(dialogEl);
    options.onConfirm();
  };
  byId("confirm-sheet-cancel").onclick = () => closeSheet(dialogEl);
  openSheet(dialogEl);
}

function renderMoreSheetList() {
  const list = byId("nav-more-list");
  list.innerHTML = Object.keys(titles)
    .filter((name) => !primaryTabViews.includes(name))
    .map(
      (name) =>
        `<button type="button" data-view="${esc(name)}">${esc(titles[name])}</button>`,
    )
    .join("");
  list.querySelectorAll("[data-view]").forEach((button) =>
    button.addEventListener("click", () => {
      closeSheet(byId("nav-more-sheet"));
      showView(button.dataset.view);
    }),
  );
}
function syncMoreSheetActive() {
  document
    .querySelectorAll("#nav-more-list [data-view]")
    .forEach((button) =>
      button.classList.toggle("active", button.dataset.view === activeViewName),
    );
}

// Called from core.js's showView() on every navigation -- keeps the tab bar's active state, the
// "More" sheet's active row, and any open sheet in sync with whichever view is now on screen.
function syncMobileChrome(name) {
  activeViewName = name;
  document
    .querySelectorAll(".tabbar-item[data-view]")
    .forEach((button) => button.classList.toggle("active", button.dataset.view === name));
  byId("tabbar-more").classList.toggle("active", !primaryTabViews.includes(name));
  syncMoreSheetActive();
  document.querySelectorAll("dialog.sheet[open]").forEach((dialogEl) => closeSheet(dialogEl));
}

function setupMobileShell() {
  document.body.classList.add("js-mobile-shell");
  byId("bottom-tabbar").hidden = false;
  [byId("nav-more-sheet"), byId("app-sheet"), byId("confirm-sheet")].forEach(mountSheet);
  document
    .querySelectorAll(".tabbar-item[data-view]")
    .forEach((button) => button.addEventListener("click", () => showView(button.dataset.view)));
  byId("tabbar-more").addEventListener("click", () => {
    syncMoreSheetActive();
    openSheet(byId("nav-more-sheet"));
  });
  renderMoreSheetList();
  syncMobileChrome(activeViewName);

  // Transfers: the "Filters" trigger opens the whole filter panel (search, club/relevance
  // selects, "More filters", active-filter chips, reset) as a sheet -- one active-filter count
  // badge on the trigger itself so the sheet's contents don't need duplicating just to show it.
  const filtersTrigger = byId("filters-trigger");
  const filterShell = byId("filter-shell");
  if (filtersTrigger && filterShell) {
    filtersTrigger.addEventListener("click", () => openContentSheet(filterShell, "Filters"));
  }
}

// Keeps the "Filters" trigger's count badge in sync with #active-filters -- called from
// overview-transfers-players.js's renderFilterChips(), right after it repopulates #active-filters.
function syncFiltersTriggerBadge() {
  const badge = byId("filters-trigger-count");
  if (!badge) return;
  const count = byId("active-filters").children.length;
  badge.hidden = count === 0;
  badge.textContent = String(count);
}
