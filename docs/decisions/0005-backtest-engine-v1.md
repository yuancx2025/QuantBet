# 0005: Backtest engine v1

Date: 2026-11-06. Status: **implemented; the owner approved the scope.** The specific rules below
were chosen during the build and can be changed. Changing one after seeing results counts as a new
experiment in `runs/experiment_log.jsonl`.

## Scope

An event-driven replay in the style of QuantConnect, with no model required. A strategy reacts to
replayed quotes and trades; a model would just be another strategy.

v1 supports resting buy orders, cancels, and holding to settlement. It leaves out selling before
settlement and taking the ask, because replaying those fairly needs better quote data than one-minute
candles. Odds, Polymarket, and models A/B/C are also out of scope.

## Execution rules

| Rule | Fills when | Use |
|---|---|---|
| `touch_at_limit` | A print at or below the limit | Optimistic bound |
| `through_limit` | A print strictly below the limit | Default at one contract |
| `through_with_volume` | Volume strictly below the limit covers the size; rungs share each print, highest first | Capacity test |

- Fills are at the order's limit, not the print price. A resting maker gets its own price.
- At one contract, any print has at least one contract of volume, so `through_with_volume` differs
  from `through_limit` only when rungs compete for one small print. The training plan's conservative
  rule only becomes informative at larger sizes, which is why the config runs 1, 10, and 100.
- Orders activate 2 seconds after placement. Only prints strictly after activation count.
- An order that can't be settled by observed data is `unknown`, never `unfilled`. A fill seen inside
  partial coverage still counts, because a print below the limit proves it regardless of the gap.
- Block trades are removed. They didn't go through the book.
- The timeline ends at the final ESPN play, in play sequence rather than latest wallclock.
- Limits round down to a whole cent. Bids at or above the ask are rejected as marketable, as are bids
  outside 2-98 cents and bids on quotes more than 15 minutes old.

## What the proposals in 0003 became

This build uses two of them: it pulls trades for all games (#1), and it adds the size check and
sweep/drift tag (#2). It goes further than #1 in two ways the audit called for: both markets of each
game, and a window starting 3 hours before scheduled tipoff. The Stage 0 pull started 30 minutes
before first play, about 1 hour 45 minutes after the planning time.

The rest of 0003 keeps its recorded status.

## Fill tags

A fill is a **sweep** if the mid just before it and the mid 5 minutes later are both at least 5 cents
above the limit, and otherwise **drift**. It is **unknown** if either mid is unavailable. These tags
use later prices, so they are diagnostics and can't become pregame features. Minute-level mids can
misclassify fast repricing.

## Data

- Universe: ESPN regular-season fixtures by date, never chosen by outcome. Games that can't be used
  are written to `exclusions.parquet` with a reason.
- Stored in `~/QuantBetData/history/`, outside the project, as the recorder's output is. The Desktop
  may sync to cloud storage, and a stray conflict copy found in `.venv` suggests it does.
- The planning time uses a schedule downloaded after the games. Reports disclose this. Stage 0 found
  scheduled time runs 11-17 minutes ahead of first play.
- ESPN's play clock is wrong on some games: about 24 hours late on Oct 28-30, 2025, and 1 hour late on
  Mar 8, 2026, the daylight-saving change. Uncorrected, 18 replays ran past the real end of the game.
  Normalization removes the error in whole hours. The final play is checked against the last trade
  and the first play against the schedule, and the amounts are stored in `final_play_shift_us` and
  `first_play_shift_us`. This was decided before any full-season result was seen. A real first-play
  delay of over an hour would be mistaken for clock error, but the replay doesn't use the first play.
  The coverage report counts corrected games and flags any end that still looks implausible.
- 2026-27 tuning games aren't pulled yet. The archive ends in June 2026, so they need the live
  endpoints, whose field names differ. The candle adapter already accepts both forms.

## Enforcing the holdout

`configs/splits.yaml` is read rather than repeated, so moving the boundary needs no code change.
`Splits.period_of` raises for holdout games and for unreadable tickers. The pull calls it before
fetching a game's market data, the store calls it before every read, and the backtest refuses a
`holdout` period outright. There is deliberately no override yet; the frozen final test needs its own
audited path when the time comes.

## Reporting

- Headlines are gross, as decision 0004 requires, and each report says so first. Fee scenarios appear
  only as a labelled sensitivity. The 0.0175 rate remains unverified.
- Intervals come from resampling whole games, and are withheld below 20 games.
- Reports split fills at scheduled tipoff, to show how much rests on the unconfirmed assumption that
  orders survive it. Results are also shown without the 10 Stage 0 fixtures.
- Every run saves its config, data hash, source hash, and result hashes, and appends to
  `runs/experiment_log.jsonl`.

## Tooling

The command line runs through `scripts/quantbet.py` rather than an installed console command. On this
machine `uv` reinstalls the package with macOS's hidden flag on its `.pth` file, and Python 3.12 skips
hidden `.pth` files, so `import quantbet_nba` fails in `uv run` without the path set explicitly.

## First result

Run `fixed_offset_v1-20261106T202520Z-b75caeca`: 1,230 games in dataset `5ecb302573cb`, buying 5, 10,
and 15 cents below each market's mid, two hours before scheduled tipoff, and holding to settlement.
These results are gross, hypothetical, and from development data, and they come from 15 scenarios.

| Fill rule, both sides, one contract | Fills | Gross per filled | 95% interval | Gross P&L |
|---|---:|---:|---|---:|
| `through_limit` (default) | 5,392 | -1.48¢ | -2.48¢ to -0.56¢ | -$79.77 |
| `touch_at_limit` (optimistic) | 5,487 | -0.53¢ | -1.48¢ to +0.34¢ | -$28.99 |

This shows no edge for the fixed ladder. Only 71 of the 5,392 `through_limit` fills came before
scheduled tipoff, so nearly the whole result depends on orders surviving tipoff, which is unconfirmed.
A separate calculation outside the engine reproduced both rows exactly.

## Known limits

- Prints don't reveal queue position or our own market impact, so capacity results are estimates.
- The recorder's order books could validate these fill rules later, but recorded games are currently
  in the tuning and holdout periods, so only the tuning portion may be examined.
- The portfolio limit is applied after replay, which is exact only for strategies whose decisions
  don't depend on other games. Every v1 strategy qualifies.
- Whether Kalshi reserves funds for resting orders is unverified. `peak_reserved_dollars` assumes it
  does.
