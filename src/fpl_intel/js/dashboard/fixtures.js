function setupFixtures() {
  const events = [
    ...new Set(fixtures.map((row) => row.event).filter(Boolean)),
  ].sort((a, b) => a - b);
  byId("fixture-gameweek").innerHTML = events.length
    ? events
        .map((event) => `<option value="${event}">Gameweek ${event}</option>`)
        .join("")
    : '<option value="">No gameweeks</option>';
  const preferred = String(state.fpl.next_event_id || events[0] || "");
  if (events.map(String).includes(preferred))
    byId("fixture-gameweek").value = preferred;
  const clubs = [
    ...new Set(
      fixtures.flatMap((row) => [row.home_team, row.away_team]).filter(Boolean),
    ),
  ].sort((a, b) => a.localeCompare(b));
  byId("fixture-club-filter").insertAdjacentHTML(
    "beforeend",
    clubs
      .map((club) => `<option value="${esc(club)}">${esc(club)}</option>`)
      .join(""),
  );
  byId("fixture-gameweek-prev").addEventListener("click", () =>
    stepFixtureGameweek(-1),
  );
  byId("fixture-gameweek-next").addEventListener("click", () =>
    stepFixtureGameweek(1),
  );
  byId("fixture-club-filter").addEventListener("change", renderFixtures);
  renderFixtures();
}
function stepFixtureGameweek(delta) {
  const select = byId("fixture-gameweek");
  const options = [...select.options].filter((option) => option.value);
  if (!options.length) return;
  const index = options.findIndex((option) => option.value === select.value);
  const nextIndex = Math.min(
    options.length - 1,
    Math.max(0, (index < 0 ? 0 : index) + delta),
  );
  if (options[nextIndex].value === select.value) return;
  select.value = options[nextIndex].value;
  renderFixtures();
}
function renderFixtures() {
  const select = byId("fixture-gameweek");
  const options = [...select.options].filter((option) => option.value);
  const index = options.findIndex((option) => option.value === select.value);
  byId("fixture-gameweek-value").textContent = options.length
    ? (options[index] || options[0]).textContent
    : "No gameweeks";
  byId("fixture-gameweek-prev").disabled = !options.length || index <= 0;
  byId("fixture-gameweek-next").disabled =
    !options.length || index < 0 || index >= options.length - 1;
  const event = Number(select.value);
  const club = byId("fixture-club-filter").value;
  const rows = fixtures
    .filter(
      (row) =>
        (!event || row.event === event) &&
        (club === "all" || row.home_team === club || row.away_team === club),
    )
    .sort((a, b) =>
      String(a.kickoff_time || "").localeCompare(String(b.kickoff_time || "")),
    );
  byId("fixture-count").textContent =
    `${rows.length} fixture${rows.length === 1 ? "" : "s"}`;
  byId("fixture-results").innerHTML = rows.length
    ? rows
        .map((row) => {
          const result = row.finished
            ? `${row.home_score} – ${row.away_score}`
            : "vs";
          return `<div class="fixture-row"><span class="muted">${esc(fmtDate(row.kickoff_time, true))}</span><span class="fixture-team"><strong>${esc(row.home_team)}</strong><span class="difficulty d${esc(row.home_difficulty)}" title="Home difficulty">${esc(row.home_difficulty)}</span></span><strong>${esc(result)}</strong><span class="fixture-team away"><strong>${esc(row.away_team)}</strong><span class="difficulty d${esc(row.away_difficulty)}" title="Away difficulty">${esc(row.away_difficulty)}</span></span></div>`;
        })
        .join("")
    : '<div class="empty">No fixtures match this gameweek and club.</div>';
}
