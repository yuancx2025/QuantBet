# NBA team tendencies dashboard

This separate descriptive dashboard uses ESPN daily NBA scoreboards for **every date**
in the requested intervals, not the Stage 0 selected games. It does not run Stage 0,
fit A/B/C models, simulate trades, evaluate profitability or place orders. No shared
runtime dependencies were added. `scripts/stage0_spike.py` and its workflow are unchanged.

## Build, refresh and serve

From the checkout, with Python 3.12 and uv:

```sh
UV_CACHE_DIR=/workspace/.cache/uv uv sync --locked
UV_CACHE_DIR=/workspace/.cache/uv uv run python scripts/dashboard/build.py --as-of 2026-11-06
UV_CACHE_DIR=/workspace/.cache/uv uv run python scripts/dashboard/audit.py reports/team-tendencies/manifest.json --output reports/team-tendencies-audit.json
uv run python -m http.server 8000 --bind 127.0.0.1 --directory reports/team-tendencies
```

Open `http://127.0.0.1:8000`. The generated `index.html`, `app.js` and `style.css`
can also be opened directly or copied together for offline use. No CDN, browser API
calls, external fonts, secrets or server runtime are needed. Do not open the source
HTML in `dashboard/` expecting a loaded dataset; build the generated report first.

`--as-of` is required. It is an inclusive **America/New_York calendar date**, not the
machine clock. Current-season requests cover 2026-10-01 through that date; development
requests cover 2025-10-01 through `--development-as-of` (default 2026-04-12). Both end
arguments must fall within their named season. Future/scheduled games never enter
statistics. A same-day build is a snapshot at source acquisition, not a promise that
that day's games have finished. To include later finals, refresh again explicitly.

By default, successful responses are reused from the separate ignored cache
`data/team-tendencies/cache/`. Use `--refresh` to refetch **all** dates with bounded
15-second request / 5-second connect timeouts and at most three attempts per date.
There is one request sequence, no unbounded concurrency. Each response envelope records
URL, query, acquisition timestamp and raw-response SHA-256; source fields and event IDs
are preserved. Failed responses never replace cached responses. A malformed cached
response is reported as a failed date; `--refresh` can repair it. No cache is shared
with Stage 0. Run only one writer for a particular output/cache at a time.

A partial refresh exits nonzero and preserves an existing dashboard. Details go to the
ignored sibling `reports/team-tendencies-refresh-attempt.json`. If no build exists,
a first partial refresh produces a clearly marked partial/error dashboard, possibly
empty, and exits nonzero. To inspect a new partial result without replacing a usable
build, select a **new dedicated** directory with `--output reports/team-tendencies-partial`.
Build assets are staged before replacing the directory; `.previous` is a crash-recovery
backup, which must be inspected before another build. Do not use an output directory
for other files. Unowned nonempty directories are refused. Generated data and reports
stay ignored under existing `data/` and `reports/` rules.

For deterministic, clearly labeled software fixtures instead of provider data:

```sh
uv run python scripts/dashboard/build.py --as-of 2026-11-06 --fixture
uv run python -m http.server 8000 --bind 127.0.0.1 --directory reports/team-tendencies-fixture
```

Fixture output defaults to a separate directory. Its invented scores are **not NBA
season statistics**, do not establish real-data verification and do not expose actual
holdout labels. The default real-data build never silently substitutes fixtures.

## Interactions and exports

Season, venue, inclusive as-of date and recent N filter games **before** aggregation.
Recent N is per team, after venue and date; league teams can consequently have different
samples. Selecting a dot or a team in the sortable league table changes the deep dive,
not the league universe. The Lakers are the initial selection when available. The table
provides the same values as the scatter. Controls use labels, native keyboard support,
visible focus, column sort state and keyboard-selectable scatter points. Tables scroll
within their cards on narrow screens. Empty selections show no invented zero averages.

CSV exports exactly the selected team's filtered game rows, including game IDs, quarter
margins and cumulative leads; spreadsheet formula prefixes in text are neutralized.
JSON exports filtered rows, filters, coverage, source, build timestamp, dataset hash and
exposure boundary. The generated full manifest additionally contains canonical games,
source-response provenance and code/assets hash. Exports are client-side downloads.

## Metric contract

For the selected team, `dq = team quarter points − opponent quarter points`.
`cq = sum(d1…dq)` is the **cumulative lead**, distinct from a quarter margin.
`H1 = d1 + d2`; `H2 = d3 + d4`; improvement is `H2 − H1`. Q4 and H2 always
exclude overtime. Scored points, allowed points, final margin and W–L include all OT.
Opposing team perspectives are antisymmetric. One game is the independent sample unit;
the two perspectives are never described as independent observations.

Ahead/tied/behind rates use the sign of cumulative leads at Q1, Q2, Q3 and regulation
Q4, with all filtered team games as denominator. Comeback is **final wins / games with
H1 < 0**. Retention is **final wins / games with H1 > 0**. Tied halftime has a separate
count, denominator and final-win rate. A zero denominator is unavailable (`—`), never
0%. Histograms and min/25th/median/75th/max describe game-level improvement. Quantiles
use linear interpolation at `(n−1)p`; variability is sample SD with denominator `n−1`,
unavailable for fewer than two games. These are descriptive points, not possession
efficiency, opponent adjustment, causal halftime effects, calibrated probabilities,
validated predictions or evidence of profitability.

## Eligibility, coverage and limitations

Source: `https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard`, queried
with `dates=YYYYMMDD&limit=1000`. Actual development and current-season responses were
inspected during implementation. The provider's season year is the ending year (2026
means 2025–26). Daily responses must identify NBA league 46. A response at the limit is
rejected as uncertain coverage. All dates, including no-game days, are accounted for.
Coverage means daily provider responses were fetched, **not** an independent schedule
reconciliation or contractual guarantee of provider completeness.

The provider labels All-Star exhibitions and the NBA Cup championship with season type
2. These are explicitly excluded using inspected competition IDs: `4` (All-Star) and
`39` (Cup championship, which does not count in regular-season standings). Type `1`
standard games, including Cup group/knockout games that count in standings, are accepted
only with season type 2 and the requested season year. Unknown competition types are
reported as invalid, not silently assumed eligible. Pre/postseason and unfinished games
are listed as ineligible. Known ineligible exclusions do not by themselves imply missing
eligible data; failed dates or malformed/ambiguous records set `partial=true`.

Validation requires final completed post status, two distinct NBA franchise IDs (ESPN
1–30) matching competitor IDs, opposing home/away sides, finite nonnegative integer
points, explicit consecutive integer period numbers from 1 through the final period,
and all regulation/OT sums equal to finals. Every OT must follow a tied score and final
results cannot be tied. Missing periods are **never** replaced with zero. Duplicate game
IDs within a season are quarantined entirely and listed as invalid. All exclusions have
IDs, dates and reasons in manifests; failed dates are explicit. Accepted games preserve
tipoff, teams, scores and response hashes/timestamps. As-of profiles use only filtered
local rows; removing future games cannot change earlier profiles. Provider corrections
to already completed games can change a rebuilt historical profile; this is a score
snapshot, not a reconstructed information-availability dataset.

Initial live validation on 2026-11-06 (explicit current boundary 2026-11-06):

| Season | Interval | Accepted games | Teams | Team appearances | OT games | Failed dates |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| 2025–26 development | 2025-10-01 to 2026-04-12 | 1,230 | 30 | 82 each | 54 | 0 |
| 2026–27 exploratory | 2026-10-01 to 2026-11-06 | 123 | 30 | 7–9 | 7 | 0 |

Development exclusions: 71 not regular season, 4 unfinished/cancelled listings, 4 All-Star
exhibitions and 1 Cup championship. Current exclusions: 67 not regular season and 9
not completed at acquisition. No malformed included games; both manifests report
`partial=false` after eligibility checks. Future refreshes may differ. These counts are
coverage validation, not a statement that current-day scheduled games are complete.

## Holdout exposure

The existing split policy reserves 2026–27 as untouched, with
`KXNBAGAME-26NOV05MIAGSW` already excluded in
[decision 0002](decisions/0002-holdout-exclusion.md). This requested current-season
exploration changes the practical exposure boundary: **all 123 accepted current-season
games from 2026-10-20 through the 2026-11-06 as-of snapshot are available to team-pattern
inspection and must conservatively be treated as exposed**. The generated manifest
records their ESPN IDs and as-of boundary alongside the original excluded ticker;
retaining that manifest is required for audit. Fetching raw current-season scoreboards
also exposes source material beyond the displayed regular-season subset; response
provenance and excluded IDs record that scope.

Code separation does **not** restore statistical independence or untouched status.
This document does not silently rewrite split configuration, rescind the existing
exclusion or declare the remaining season safe. A replacement final-test-period
choice and any split-policy revision remain for a later explicit research decision.
Do not use explored results to claim untouched final-test performance. No predictive
A/B/C model was trained or evaluated and no profit backtest was performed here.

## Validation commands

```sh
UV_CACHE_DIR=/workspace/.cache/uv uv run ruff check .
uv run python -m unittest discover -s tests -v
node tests/team_tendencies.test.cjs
node --check dashboard/app.js
uv run python scripts/dashboard/audit.py reports/team-tendencies/manifest.json --output reports/team-tendencies-audit.json
```

Tests use deterministic fixtures for quarter/cumulative distinction, halves, improvement,
OT, signs, tied halftime and denominators, missing/malformed periods, identities,
duplicates, eligibility, filtering, empty selections, future/as-of isolation, CSV content,
cache failures and preserving output on partial refresh. The live audit checks unique
game IDs, score sums, antisymmetric perspectives and half totals for all accepted games.

Browser QA limitation in this cloud environment: the browser inventory is available,
but the managed `sites-preview` relay timed out opening port 4173 on two attempts,
including after browser startup. Consequently visual rendering, click/keyboard flows,
actual download handling and narrow-screen layout are **not browser-verified**. Pure
filter/selection inputs, aggregation and CSV generation are covered by Node tests;
those tests do not replace browser QA. Review the responsive UI in a working local
browser before marking the PR ready. No hosting was created, deployed or merged.
