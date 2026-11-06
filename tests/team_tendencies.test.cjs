const { test } = require("node:test");
const assert = require("node:assert/strict");
const { filterRows, summary, csv } = require("../dashboard/app.js");
const row = (game, date, h1, h2, final, side = "home", team = "13") => ({
  game,
  date,
  season: 2026,
  team,
  name: team === "13" ? "Lakers" : "Celtics",
  opponent: "Opponent",
  side,
  scored: 100 + final,
  allowed: 100,
  final,
  d: [h1, 0, h2, 0],
  c: [h1, h1, h1 + h2, h1 + h2],
  h1,
  h2,
  improvement: h2 - h1,
  ot: 0,
});
const rows = [
  row("a", "2025-10-21", -5, 10, 5),
  row("b", "2025-10-22", 5, -10, -5),
  row("c", "2025-10-23", 0, 4, 4, "away"),
  row("d", "2025-10-24", -10, 5, -5),
];
const f = { season: 2026, asof: "2025-10-24", venue: "all", recent: 0 };

test("cohort denominators, tied halftime separate and undefined unavailable", () => {
  const s = summary(rows);
  assert.deepEqual(s.comeback, { wins: 1, denominator: 2, rate: 0.5 });
  assert.deepEqual(s.retention, { wins: 0, denominator: 1, rate: 0 });
  assert.deepEqual(s.tied, { wins: 1, denominator: 1, rate: 1 });
  assert.equal(summary([rows[2]]).comeback.rate, null);
  assert.equal(summary([rows[0]]).retention.rate, null);
  assert.equal(s.breaks[1][0].count, 2);
  assert.equal(s.breaks[1][1].count, 1);
  assert.equal(s.breaks[1][2].count, 1);
});
test("empty filters, variance and no invented zeros", () => {
  const selected = filterRows(rows, { ...f, asof: "2025-10-01" });
  assert.equal(selected.length, 0);
  const s = summary(selected);
  assert.equal(s.h1, null);
  assert.equal(s.comeback.rate, null);
  assert.equal(s.sdH1, null);
  assert.equal(s.breaks[0][0].rate, null);
  assert.equal(summary([rows[0]]).sdH2, null);
});
test("filter before aggregate, venue and recent order", () => {
  const selected = filterRows(rows, { ...f, venue: "home", recent: 2 });
  assert.deepEqual(
    selected.map((r) => r.game),
    ["d", "b"],
  );
  assert.equal(summary(selected).h1, -2.5);
  assert.equal(summary(selected).h2, -2.5);
  assert.equal(summary(selected).improvement, 0);
});
test("future removal cannot change as-of profile or last N", () => {
  const fPast = { ...f, asof: "2025-10-22", recent: 1 };
  const original = summary(filterRows(rows, fPast));
  const future = row("e", "2026-01-01", 200, -100, 100);
  assert.deepEqual(original, summary(filterRows([...rows, future], fPast)));
  assert.deepEqual(original, summary(filterRows(rows.slice(0, 2), fPast)));
  assert.equal(filterRows(rows, { ...f, season: 2027 }).length, 0);
});
test("recent N is per team, independent-game IDs retained", () => {
  const opposite = rows.map((r) => ({
    ...r,
    team: "2",
    side: r.side === "home" ? "away" : "home",
  }));
  const selected = filterRows([...rows, ...opposite], { ...f, recent: 2 });
  assert.equal(selected.length, 4);
  assert.equal(new Set(selected.map((r) => r.game)).size, 2);
  assert.equal(filterRows(selected, f, "13").length, 2);
});
test("CSV exports exact filtered games, cumulative columns and escapes formulas", () => {
  const selected = filterRows(rows, { ...f, recent: 1 });
  const text = csv(selected);
  assert.equal(text.split("\r\n").length, 2);
  assert.match(text, /"d"/);
  assert.doesNotMatch(text, /"a"/);
  assert.match(text, /"q4","c1","c2","c3","c4"/);
  assert.match(
    csv([{ ...rows[0], opponent: '=HYPERLINK("bad")' }]),
    /'=HYPERLINK/,
  );
  assert.match(text, /"-10"/); // signed numeric margins stay numeric
});
test("distribution and sample SD use games as observations", () => {
  const s = summary([
    row("a", "2025-10-21", 0, 2, 2),
    row("b", "2025-10-22", 0, 4, 4),
  ]);
  assert.deepEqual(s.distribution, [2, 2.5, 3, 3.5, 4]);
  assert.equal(s.sdImprovement, Math.sqrt(2));
  assert.equal(s.n, 2);
});
