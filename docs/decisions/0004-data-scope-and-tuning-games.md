# 0004: Data scope, ESPN use, fee assumption, and 2026-27 tuning games

Date: 2026-11-06. Status: **accepted by the project owner.** This amends
`docs/resting-ladder-training-plan.md` and `docs/decisions/0002-holdout-exclusion.md` where they
conflict.

## ESPN data

The licensed-data requirement is relaxed. ESPN may be used for scores and play-by-play in this
research. No ESPN license was obtained, so raw data stays out of the repository, as `.gitignore`
already provides. Revisit this before any use beyond private research.

## Tuning games

2026-27 regular-season games with a Kalshi ticker date of `26DEC03` or earlier may be used for
tuning. Games from `26DEC04` onward remain the holdout. `configs/splits.yaml` is the record of this
boundary.

`KXNBAGAME-26NOV05MIAGSW`, the game excluded in 0002, now falls in the tuning set, so it no longer
needs a holdout exclusion.

The boundary was first set at `26NOV05` and moved the same day, before any recorded prices or sizes
had been looked at. Order-book recording started on 2026-11-06, so the earlier boundary would have
left the tuning set with no recorded books.

Tuning games before 2026-11-06 have trades and candles but no recorded books. Recordings of holdout
games may be checked for health only (message counts and sequence gaps).

## Fees

The owner chose to treat limit orders as free for testing, given the small intended size. Results
are therefore reported gross and must be labelled as such.

This is an assumption, not a finding. On 2026-11-06, `GET /series/KXNBAGAME` returned
`fee_type: quadratic_with_maker_fees` and `fee_multiplier: 1`. Secondary sources give the maker fee
as `0.0175 × C × P × (1-P)`, about 0.44¢ per contract at 50¢; the primary fee schedule has not been
read. A per-contract fee costs the same share of the stake at any size. Before results are presented
as net of costs, confirm the rate and add it back.

## Still open

Whether a resting order survives tipoff is still unconfirmed. Plans stay hypothetical until it is.
