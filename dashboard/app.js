/* Pure scoring functions shared by the browser and dependency-free Node tests. */
(function (root) {
  "use strict";
  const average = (xs) =>
    xs.length ? xs.reduce((a, b) => a + b, 0) / xs.length : null;
  function quantile(xs, p) {
    if (!xs.length) return null;
    const values = [...xs].sort((a, b) => a - b),
      at = (values.length - 1) * p;
    return (
      values[Math.floor(at)] +
      (values[Math.ceil(at)] - values[Math.floor(at)]) * (at % 1)
    );
  }
  function filterRows(rows, f, team) {
    const filtered = rows
      .filter(
        (r) =>
          r.season === Number(f.season) &&
          r.date <= f.asof &&
          (f.venue === "all" || r.side === f.venue) &&
          (!team || r.team === team),
      )
      .sort(
        (a, b) => b.date.localeCompare(a.date) || b.game.localeCompare(a.game),
      );
    // Take the most recent N after applying date and venue, independently per team.
    const counts = new Map();
    return filtered.filter((r) => {
      const n = (counts.get(r.team) || 0) + 1;
      counts.set(r.team, n);
      return !Number(f.recent) || n <= Number(f.recent);
    });
  }
  function summary(rows) {
    const n = rows.length,
      mean = (key) => average(rows.map((r) => r[key]));
    const deficits = rows.filter((r) => r.h1 < 0),
      leads = rows.filter((r) => r.h1 > 0);
    const rate = (sample) => {
      const wins = sample.filter((r) => r.final > 0).length;
      return {
        wins,
        denominator: sample.length,
        rate: sample.length ? wins / sample.length : null,
      };
    };
    const sd = (key) =>
      n < 2
        ? null
        : Math.sqrt(
            rows.reduce((s, r) => s + (r[key] - mean(key)) ** 2, 0) / (n - 1),
          );
    return {
      n,
      wins: rows.filter((r) => r.final > 0).length,
      losses: rows.filter((r) => r.final < 0).length,
      h1: mean("h1"),
      h2: mean("h2"),
      improvement: mean("improvement"),
      scored: mean("scored"),
      allowed: mean("allowed"),
      final: mean("final"),
      sdH1: sd("h1"),
      sdH2: sd("h2"),
      sdImprovement: sd("improvement"),
      d: [0, 1, 2, 3].map((q) => average(rows.map((r) => r.d[q]))),
      c: [0, 1, 2, 3].map((q) => average(rows.map((r) => r.c[q]))),
      breaks: [0, 1, 2, 3].map((q) =>
        [-1, 0, 1].map((sign) => {
          const count = rows.filter((r) => Math.sign(r.c[q]) === sign).length;
          return { count, rate: n ? count / n : null };
        }),
      ),
      comeback: rate(deficits),
      retention: rate(leads),
      tied: rate(rows.filter((r) => r.h1 === 0)),
      distribution: [0, 0.25, 0.5, 0.75, 1].map((p) =>
        quantile(
          rows.map((r) => r.improvement),
          p,
        ),
      ),
    };
  }
  const columns = [
    "game",
    "date",
    "season",
    "team",
    "name",
    "opponent",
    "side",
    "scored",
    "allowed",
    "final",
    "q1",
    "q2",
    "q3",
    "q4",
    "c1",
    "c2",
    "c3",
    "c4",
    "h1",
    "h2",
    "improvement",
    "ot",
  ];
  function csv(rows) {
    // Neutralize spreadsheet formula prefixes in provider-supplied text fields.
    const cell = (value) =>
      '"' +
      String(
        typeof value === "string" && /^[=+\-@\t\r]/.test(value)
          ? "'" + value
          : value,
      ).replaceAll('"', '""') +
      '"';
    return [
      columns,
      ...rows.map((r) =>
        columns.map((k) =>
          /^q[1-4]$/.test(k)
            ? r.d[+k[1] - 1]
            : /^c[1-4]$/.test(k)
              ? r.c[+k[1] - 1]
              : r[k],
        ),
      ),
    ]
      .map((row) => row.map(cell).join(","))
      .join("\r\n");
  }
  const api = { average, quantile, filterRows, summary, csv };
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (typeof document === "undefined") return;
  root.Tendencies = api;
  const $ = (id) => document.getElementById(id);
  const esc = (value) =>
    String(value).replace(
      /[&<>"']/g,
      (c) =>
        ({
          "&": "&amp;",
          "<": "&lt;",
          ">": "&gt;",
          '"': "&quot;",
          "'": "&#39;",
        })[c],
    );
  const fmt = (n, sign = false) =>
    n === null || n === undefined
      ? "—"
      : (sign && n > 0 ? "+" : "") + n.toFixed(1);
  const pct = (n) => (n === null ? "—" : (100 * n).toFixed(1) + "%");
  const signed = (n) =>
    `<span class="${n > 0 ? "positive" : n < 0 ? "negative" : ""}">${fmt(n, true)}</span>`;
  let data;
  try {
    data = JSON.parse($("dataset").textContent);
    if (!data || !Array.isArray(data.rows)) throw Error("No dataset");
  } catch (error) {
    document.querySelectorAll("button,input,select").forEach((control) => {
      control.disabled = true;
    });
    $("notice").textContent =
      "Local data unavailable. Build the dashboard first; see docs/team-tendencies.md. " +
      error.message;
    return;
  }
  let sortKey = "h2",
    direction = -1,
    currentRows = [],
    currentFilter;
  function filters() {
    return Object.fromEntries(
      ["season", "team", "venue", "asof", "recent"].map((id) => [
        id,
        $(id).value,
      ]),
    );
  }
  function coverage() {
    return data.coverage.find((c) => c.season === Number($("season").value));
  }
  function setupSeason() {
    const manifest = coverage();
    $("asof").max = manifest?.as_of || "";
    $("asof").value = manifest?.as_of || "";
    $("asof").min = `${Number($("season").value) - 1}-10-01`;
    const teams = new Map(
      data.rows
        .filter((r) => r.season === Number($("season").value))
        .map((r) => [r.team, r.name]),
    );
    const previous = $("team").value;
    $("team").innerHTML = [...teams]
      .sort((a, b) => a[1].localeCompare(b[1]))
      .map(([id, name]) => `<option value="${esc(id)}">${esc(name)}</option>`)
      .join("");
    $("team").value = teams.has(previous)
      ? previous
      : teams.has("13")
        ? "13"
        : teams.keys().next().value || "";
    if (!teams.size)
      $("team").innerHTML = '<option value="">No teams available</option>';
  }
  function renderScatter(profiles) {
    if (!profiles.length) {
      $("scatter").innerHTML =
        '<p class="empty">No eligible games for these filters. Try an earlier season, later date or all venues.</p>';
      return;
    }
    const span =
      Math.max(
        5,
        ...profiles.flatMap((p) => [Math.abs(p.h1), Math.abs(p.h2)]),
      ) * 1.25;
    const x = (v) => 270 + (v / span) * 200,
      y = (v) => 210 - (v / span) * 155;
    let svg =
      '<svg viewBox="0 0 540 430" role="group" aria-label="League scatter. Horizontal axis first-half margin; vertical axis regulation second-half margin. Use team table for equivalent values.">';
    [-1, -0.5, 0, 0.5, 1].forEach((t) => {
      const v = span * t;
      svg += `<line class="${t === 0 ? "axis" : "grid"}" x1="70" x2="470" y1="${y(v)}" y2="${y(v)}"/><line class="${t === 0 ? "axis" : "grid"}" y1="55" y2="365" x1="${x(v)}" x2="${x(v)}"/><text x="56" y="${y(v) + 4}" text-anchor="end">${fmt(v)}</text><text x="${x(v)}" y="385" text-anchor="middle">${fmt(v)}</text>`;
    });
    svg +=
      '<text x="270" y="420" text-anchor="middle">H1 average margin →</text><text transform="translate(17,210) rotate(-90)" text-anchor="middle">H2 average margin →</text><text x="464" y="30" text-anchor="end">STRONG BOTH HALVES</text>';
    profiles.forEach((p) => {
      const selected = p.id === $("team").value;
      svg += `<g role="button" tabindex="0" data-team="${esc(p.id)}" class="${selected ? "selected" : ""}" aria-label="Select ${esc(p.name)}: H1 ${fmt(p.h1)}, H2 ${fmt(p.h2)}, ${p.n} games"><title>${esc(p.name)} · ${p.n} GP · H1 ${fmt(p.h1)} · H2 ${fmt(p.h2)}</title><circle class="point" cx="${x(p.h1)}" cy="${y(p.h2)}" r="${selected ? 8 : 6}"/>${selected ? `<text class="label" x="${x(p.h1) + (x(p.h1) > 270 ? -12 : 12)}" y="${y(p.h2) - 10}" text-anchor="${x(p.h1) > 270 ? "end" : "start"}">${esc(p.name)}</text>` : ""}</g>`;
    });
    $("scatter").innerHTML = svg + "</svg>";
  }
  function renderLeague(rows) {
    const grouped = new Map();
    rows.forEach((r) => {
      if (!grouped.has(r.team)) grouped.set(r.team, []);
      grouped.get(r.team).push(r);
    });
    const profiles = [...grouped].map(([id, games]) => ({
      id,
      name: games[0].name,
      ...summary(games),
    }));
    profiles.sort(
      (a, b) =>
        direction *
        (typeof a[sortKey] === "string"
          ? a[sortKey].localeCompare(b[sortKey])
          : a[sortKey] - b[sortKey]),
    );
    $("league-count").textContent =
      `${profiles.length} teams · ${new Set(rows.map((r) => r.game)).size} distinct games`;
    document.querySelector("#league-table tbody").innerHTML =
      profiles
        .map(
          (p) =>
            `<tr class="${p.id === $("team").value ? "active" : ""}"><td><button class="team-link" type="button" data-team="${esc(p.id)}" aria-pressed="${p.id === $("team").value}">${esc(p.name)}</button></td><td>${p.n}</td><td>${signed(p.h1)}</td><td>${signed(p.h2)}</td><td>${signed(p.improvement)}</td></tr>`,
        )
        .join("") || '<tr><td colspan="5">No teams match.</td></tr>';
    document.querySelectorAll("#league-table th").forEach((th) => {
      th.setAttribute(
        "aria-sort",
        th.dataset.key === sortKey
          ? direction === 1
            ? "ascending"
            : "descending"
          : "none",
      );
    });
    renderScatter(profiles);
  }
  function bars(values) {
    const max = Math.max(1, ...values.map(Math.abs));
    return values
      .map(
        (v, q) =>
          `<div class="bar-row"><span>Q${q + 1}</span><div class="bar-track" aria-hidden="true"><div class="bar-fill ${v < 0 ? "neg" : ""}" style="left:${v < 0 ? 50 - (Math.abs(v) / max) * 48 : 50}%;width:${(Math.abs(v) / max) * 48}%"></div></div><strong>${signed(v)}</strong></div>`,
      )
      .join("");
  }
  function histogram(rows) {
    const values = rows.map((r) => r.improvement),
      min = Math.min(...values),
      max = Math.max(...values);
    const width = Math.max(1, (max - min) / 10),
      bins = Array(10).fill(0);
    values.forEach((v) => bins[Math.min(9, Math.floor((v - min) / width))]++);
    return `<div class="distribution" role="img" aria-label="Distribution of H2 minus H1, from ${min} to ${max} points. Exact values in game history.">${bins.map((n, i) => `<div class="bin" style="height:${(n / Math.max(...bins)) * 100}%" title="${fmt(min + i * width)} to ${fmt(min + (i + 1) * width)}: ${n} games"></div>`).join("")}</div><div class="distribution-labels"><span>${min} pts</span><span>H2 − H1</span><span>${max} pts</span></div>`;
  }
  function renderTeam(rows) {
    const s = summary(rows),
      selected = $("team").selectedOptions[0]?.textContent;
    $("team-title").textContent = selected || "Team profile";
    $("sample-label").textContent =
      `${rows.length} games · ${$("venue").selectedOptions[0].textContent} · through ${$("asof").value} ET · ${$("recent").selectedOptions[0].textContent}`;
    $("export").disabled = $("snapshot").disabled = !rows.length;
    if (!rows.length) {
      $("team-content").innerHTML =
        '<p class="empty">No completed, validated games in this selection. Change the date, season or venue. Missing data never becomes zero.</p>';
    } else {
      const stat = (label, value, note) =>
        `<div class="stat"><label>${label}</label><strong>${value}</strong><small>${note}</small></div>`;
      const rate = (label, value, note) =>
        `<div class="rate"><div>${label}<small>${note}</small></div><div><b>${pct(value.rate)}</b><small>${value.wins} wins / ${value.denominator} games</small></div></div>`;
      $("team-content").innerHTML =
        `<div class="stats">${stat("Games / record", `${s.n} <span style="font-size:17px">/ ${s.wins}–${s.losses}</span>`, "Wins–losses include overtime")}${stat("First-half margin", signed(s.h1), `SD ${fmt(s.sdH1)} points`)}${stat("Second-half margin", signed(s.h2), `Regulation only · SD ${fmt(s.sdH2)}`)}${stat("H2 − H1 improvement", signed(s.improvement), "Descriptive difference, not a causal effect")}</div><div class="team-grid"><div class="panel"><h3>Quarter scoring margins</h3><p class="help">Within each quarter · mean d₁ … d₄</p>${bars(s.d)}<div class="split-stats"><span>Scored / game<b>${fmt(s.scored)}</b></span><span>Allowed / game<b>${fmt(s.allowed)}</b></span><span>Final margin<b>${signed(s.final)}</b></span></div></div><div class="panel"><h3>Cumulative quarter-end leads</h3><p class="help">Sum of quarter margins to each break · mean c₁ … c₄</p>${bars(s.c)}<p class="help">Q4 is the regulation lead; final margin above includes OT.</p></div><div class="panel"><h3>Where the team stands at each break</h3><p class="help">Counts and rates from cumulative leads. Denominator: ${s.n} games.</p><div class="table-wrap"><table><caption>Regulation quarter-end position</caption><thead><tr><th>Break</th><th>Behind</th><th>Tied</th><th>Ahead</th></tr></thead><tbody>${s.breaks.map((parts, q) => `<tr><td>Q${q + 1}</td>${parts.map((v) => `<td>${v.count} / ${s.n}<br>${pct(v.rate)}</td>`).join("")}</tr>`).join("")}</tbody></table></div></div><div class="panel"><h3>From halftime to the final buzzer</h3><p class="help">Final wins, including OT; separate halftime cohorts.</p>${rate("Comeback", s.comeback, "Halftime deficit (H1 < 0)")}${rate("Lead retention", s.retention, "Halftime lead (H1 > 0)")}${rate("Tied at halftime", s.tied, "H1 = 0 · separate cohort")}</div><div class="panel" style="grid-column:1/-1"><h3>Improvement varies from game to game</h3><p class="help">H2 − H1 distribution · one observation per game · sample SD ${fmt(s.sdImprovement)} points</p>${histogram(rows)}<div class="split-stats">${["Minimum", "25th percentile", "Median", "75th percentile", "Maximum"].map((label, i) => `<span>${label}<b>${signed(s.distribution[i])}</b></span>`).join("")}</div></div></div>`;
    }
    document.querySelector("#history tbody").innerHTML =
      rows
        .map(
          (r) =>
            `<tr><td>${esc(r.date)}</td><td>${esc(r.opponent)}</td><td>${esc(r.side)}</td><td>${r.scored}–${r.allowed}</td><td>${signed(r.final)}</td>${[...r.d, r.h1, r.h2, r.improvement].map((v) => `<td>${signed(v)}</td>`).join("")}<td>${r.ot}</td><td>${esc(r.game)}</td></tr>`,
        )
        .join("") ||
      '<tr><td colspan="14">No games match the filters.</td></tr>';
  }
  function render() {
    if (!$("asof").checkValidity()) {
      $("asof").reportValidity();
      return;
    }
    currentFilter = filters();
    const manifest = coverage(),
      partial = !manifest || manifest.partial;
    $("notice").className =
      "notice" + (partial || data.fixture ? " warning" : "");
    $("notice").textContent =
      (data.fixture ? "DETERMINISTIC FIXTURE — NOT REAL NBA DATA. " : "") +
      (partial
        ? "Partial / unavailable coverage. "
        : "Date interval fetched; see eligibility exclusions. ") +
      `${manifest?.accepted_games ?? 0} validated games in source interval; ${manifest?.failed_dates?.length ?? 0} failed dates; ${manifest?.exclusions?.length ?? 0} exclusions. Refreshed ${data.refreshed}.`;
    const leagueRows = filterRows(data.rows, currentFilter);
    currentRows = leagueRows.filter((r) => r.team === currentFilter.team);
    renderLeague(leagueRows);
    renderTeam(currentRows);
    $("provenance").innerHTML =
      `<p>Source: <a href="${esc(data.source)}" rel="noreferrer">ESPN NBA scoreboard</a>. Source date interval uses America/New_York game dates. Refresh timestamp describes this build, not live availability. Cached response acquisition times are recorded in the manifest.</p><p>Filters are applied before aggregation. Recent N is per team, after season/date/venue. A league dot is a team perspective; opposing perspectives are not independent samples. Export contains only selected-team filtered games.</p><pre>${esc(JSON.stringify({ refreshed: data.refreshed, fixture: data.fixture, sha256: data.sha256, coverage: manifest, exposure: data.exposure }, null, 2))}</pre>`;
  }
  function download(content, extension, type) {
    const blob = new Blob([content], { type }),
      url = URL.createObjectURL(blob),
      a = document.createElement("a");
    a.href = url;
    a.download = `quantbet-${data.fixture ? "FIXTURE-" : ""}${currentFilter.season}-${currentFilter.team}-${currentFilter.asof}.${extension}`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  }
  $("filters").addEventListener("submit", (e) => e.preventDefault());
  $("season").addEventListener("change", () => {
    setupSeason();
    render();
  });
  ["team", "venue", "asof", "recent"].forEach((id) =>
    $(id).addEventListener("change", render),
  );
  $("reset").addEventListener("click", () => {
    $("venue").value = "all";
    $("recent").value = "0";
    $("team").value = "13";
    setupSeason();
    render();
  });
  document
    .querySelector("#league-table thead")
    .addEventListener("click", (e) => {
      const th = e.target.closest("th");
      if (!th) return;
      direction =
        sortKey === th.dataset.key
          ? -direction
          : th.dataset.key === "name"
            ? 1
            : -1;
      sortKey = th.dataset.key;
      render();
    });
  function select(e) {
    const target = e.target.closest("[data-team]");
    if (!target) return;
    if (e.type === "keydown" && !["Enter", " "].includes(e.key)) return;
    if (e.type === "keydown") e.preventDefault();
    const id = target.dataset.team,
      wasScatter = !!target.closest("svg");
    $("team").value = id;
    render();
    // Preserve keyboard position when the graph/table is rebuilt.
    const container = wasScatter ? $("scatter") : $("league-table");
    [...container.querySelectorAll("[data-team]")]
      .find((node) => node.dataset.team === id)
      ?.focus();
  }
  ["scatter", "league-table"].forEach((id) => {
    $(id).addEventListener("click", select);
    $(id).addEventListener("keydown", (e) => {
      if (e.target.tagName.toLowerCase() !== "button") select(e);
    });
  });
  $("export").addEventListener("click", () =>
    download(csv(currentRows), "csv", "text/csv;charset=utf-8"),
  );
  $("snapshot").addEventListener("click", () =>
    download(
      JSON.stringify(
        {
          filters: currentFilter,
          rows: currentRows,
          source: data.source,
          refreshed: data.refreshed,
          fixture: data.fixture,
          coverage: coverage(),
          exposure: data.exposure,
          dataset_sha256: data.sha256,
        },
        null,
        2,
      ),
      "json",
      "application/json",
    ),
  );
  setupSeason();
  render();
})(typeof window === "undefined" ? globalThis : window);
