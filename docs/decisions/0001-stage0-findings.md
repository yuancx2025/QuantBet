# 0001: Stage 0 findings

Date: 2026-11-06. Sample: 10 games from the 2025-26 regular season, chosen by fixed rules in
`scripts/stage0_spike.py` before any prices were looked at. Reproduce with
`uv run python scripts/stage0_spike.py all`. Output goes to `reports/stage0/`.

Ten games can validate data and timing. They say nothing about whether an edge exists.

## Gate result: price reacts just after scoring

**Passed in 10 of 10 games, with no clock or time-zone bug.**

| Game | Role | Peak lag (min) | Corr at peak | Corr at -3 min | Trades (home market) |
|---|---|---|---|---|---|
| LAC@IND | comeback | +1 | 0.55 | -0.02 | 17,449 |
| DEN@ATL | comeback | 0 | 0.64 | 0.07 | 8,312 |
| MEM@UTA | blowout | +1 | 0.47 | 0.14 | 2,450 |
| UTA@MIN | blowout | +1 | 0.52 | 0.14 | 248 |
| SAS@NOP | overtime | +1 | 0.70 | -0.17 | 8,653 |
| MEM@ORL | early tip, neutral site | 0 | 0.67 | 0.10 | 20,921 |
| PHX@LAC | late tip | +1 | 0.46 | -0.02 | 2,160 |
| DAL@POR | upset | 0 | 0.58 | -0.07 | 11,569 |
| MIN@PHX | close finish | +1 | 0.67 | -0.05 | 11,832 |
| SAS@CLE | typical margin | +1 | 0.76 | 0.07 | 2,652 |

The test correlates 3-minute changes in margin and in log-odds price. An earlier 1-minute version
showed PHX@LAC peaking at -1 minute, but its correlation was weak at every lag (at most 0.20). That
was noise from a thin game, not a clock problem.

At 5-second resolution, 8 of 10 games peak at 0 to +5 seconds from ESPN's wallclock. UTA@MIN and
PHX@LAC have too little signal at that resolution.

Across 260 isolated baskets, price moves about 5.8¢ toward the scorer. It is 2.1¢ of the way there
at the wallclock and past half by +5 seconds. The slow drift of about 1¢ before the basket may be
anticipation or scoring runs; this sample can't separate them. The spec's assumed 10-second
play-by-play latency looks reasonable as a minimum.

## Findings that change the design

### Flash sweeps are common and fill resting bids

32 minutes across 10 games had trades at least 10¢ below the mid on both sides of that minute.
This count is rough: some are fast real repricing late in games, which minute-level mids can't
fully separate.

Clearest case: PHX@LAC, 2025-10-25 02:50:54 UTC. Five trades in one second took LAC from about 72¢
to 13¢ with the game tied. The mid was back near 72¢ a minute later, and LAC won by 27.

These are real prints, not bad data. Consequences:

- Fills come in two kinds: sweeps, where fair price barely moves, and drift, where the team really
  is losing. They should behave very differently, so Stage 4 tags and reports them separately.
- This is a candidate edge source the spec doesn't list, so it is added as its own hypothesis.
- Sweeps are small, roughly 100 contracts at a level in this example. "A trade printed below L" isn't
  enough for sizing, so the fill model also needs volume traded below L.

### Liquidity varies more by game than by month

Trades per home market ranged from 248 to 20,921 in this sample, and in-game volume from 85,000 to
5.3 million contracts. The full archive (both markets, whole market life) shows this is mostly
game-to-game spread:

| Month | Games | 25th percentile | Median | 75th percentile |
|---|---|---|---|---|
| Nov 2025 | 219 | 3.0M | 4.7M | 6.7M |
| Dec 2025 | 197 | 3.6M | 5.3M | 7.9M |
| Jan 2026 | 236 | 4.0M | 6.3M | 8.9M |
| Feb 2026 | 166 | 3.6M | 6.3M | 10.2M |
| Mar 2026 | 239 | 3.2M | 5.4M | 9.3M |
| May 2026 (playoffs) | 34 | 29.4M | 36.1M | 52.5M |

Within the regular season the median rose modestly, while games in the same month differ by 2-3x.
Playoff volume is far higher, which is another reason to keep playoffs out of the sample. October
2025 and April 2026 mix in other game types, so they are left out of this table.

One November 2026 game had about 21M contracts, about 4x the November 2025 median, but a single game
can't establish a trend. Whether 2026-27 books are deeper needs a proper check that doesn't
contaminate the holdout, such as comparing volume only. Results should be split by game liquidity.

### Candles match trades exactly

Candle trade low equalled the lowest trade in all 1,595 minutes compared, and no trade fell in a
minute without a candle. One candle had a price with no matching trade in the pull; it may be an
edge of the time window, but that wasn't checked. So candles are enough to decide whether a price
printed below L. They don't give volume below L or the fill time within the minute.

### Polymarket is a free in-game reference

It returned 1-minute history on resolved games with no key, unlike the coarse data a GitHub issue
reported. It was found for 9 of 10 games.

- Median gap to Kalshi mid: 0.5-1.0¢. 95th percentile: 2-5¢.
- Correlation of 3-minute changes: 0.84-0.99 in 8 games, and 0.61 in PHX@LAC, the sweep game.
- Not found: UTA@MIN on 2025-11-07. The cause is unknown (no market, or a different slug).

Limits: it's another prediction market, not a sportsbook, and arbitrage links the two venues, so it
isn't independent truth. It's well suited to spotting Kalshi-only dislocations such as sweeps. What
its history price represents (mid or last trade) is unconfirmed, and its terms need your review.

## Open items

### Fees: partly answered

- `GET /series/KXNBAGAME` reports `fee_type: quadratic_with_maker_fees`, `fee_multiplier: 1`. Makers
  appear to pay, contrary to the spec's assumption.
- Kalshi's help center: maker fees apply only when a resting order executes, and cancelling is free.
- Kalshi's developer docs: fees are computed per fill to $0.000001, with rounding carried in a
  per-order accumulator and rebated. So rounding costs small orders less than feared.
- The rate is **not confirmed from a primary source**. `0.0175 × C × P × (1-P)` comes from secondary
  sites. Kalshi's fee schedule PDF couldn't be read from this environment.
- `GET /series/fee_changes` returned an empty list for this series, so the rate in force during
  2025-26 isn't established either.

### Does a pregame order survive tipoff: not confirmed

Kalshi's order panel help lists GTC as the default and "At event start" as a separate expiry option,
and quotes and trades run continuously through tipoff. This suggests orders survive, but the page
doesn't say so. A demo test or Kalshi support needs to confirm it.

### OddsPapi: not tested

It needs a free API key.

## Quirks for the Stage 1 adapters

**Kalshi**
- Field names differ by endpoint: `close`/`volume` in `/historical`, `close_dollars`/`volume_fp` live.
- Archived markets have no `occurrence_datetime`.
- `end_period_ts` marks the end of a candle, which covers the minute up to it. Using it as a start
  time would leak up to a minute of future data.
- No candle exists for a minute with no activity: up to 6 minutes in a row in-game in the thinnest
  game. Carry the last value forward and track its age.
- A candle can exist with null trade prices, when only quotes changed.
- Quotes after the final play are junk (IND's mid bounced to about 28¢ after it lost). End the
  timeline at the final play.
- Markets close 8-37 minutes after the last play.
- Ticker date is the US local date (`25OCT24` for a game on Oct 25 UTC). Team order is away then
  home, including at the neutral site.
- The archive has 31 team suffixes. `GUA` isn't an NBA team, so filter by game data.
- Home and away mids summed to $1.00-1.005.

**ESPN**
- Abbreviations differ: GS, NY, SA, NO, UTAH, WSH become GSW, NYK, SAS, NOP, UTA, WAS. GS, NY and
  WSH were checked only against the suffix list, not against a game in this sample.
- First play came 11-17 minutes after scheduled time, so scheduled time isn't tipoff.
- `pickcenter` had no moneyline in 4 of 10 games, all from Oct-Nov 2025. It can't be relied on as a
  pregame anchor.

**Settlement:** matched ESPN's winner in 10 of 10 games.

## Volume estimate

The median was about 8,500 trades per market from 30 minutes before tip, so about 9 pages. That
implies roughly 22,000 calls for all 2025-26 regular-season games, or under an hour within rate
limits and free. The trades before that window weren't measured.
