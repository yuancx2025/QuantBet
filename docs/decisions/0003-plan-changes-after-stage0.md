# 0003: Proposed plan changes after Stage 0

Date: 2026-11-06. Status: **proposed, awaiting review.** Evidence is in
[0001](0001-stage0-findings.md).

| # | Current plan | Proposed | Why |
|---|---|---|---|
| 1 | Pull trades for fixtures plus about 50 games | Pull trades for all 2025-26 games | The fill model needs volume below L, which candles lack. It takes about 22,000 free calls, far below the earlier 175,000 estimate, which came from one busy 2026 game |
| 2 | One conservative fill rule | Add a size check: enough volume traded below L for the order. Tag each fill as sweep or drift | Sweeps are common and thin, and should behave differently from drift fills |
| 3 | Four candidate edge sources | Add a fifth: Kalshi sweeps fill resting bids below fair value | Seen in the sample but not yet tested. It counts as one more hypothesis in the multiple-testing log |
| 4 | Odds checkpoint leans toward the $119 month | Default to Polymarket for in-game grading, and buy only if an independent sportsbook check is judged worth it | Free, 1-minute cadence compared with 5-minute snapshots, and it tracks Kalshi closely. Sportsbook closing lines are still needed for the pregame anchor |
| 5 | Tuning results reported overall | Also report by month and by game liquidity | Games in the same month differ 2-3x in volume, so fills may depend on liquidity. Whether 2026-27 is deeper is unknown |
| 6 | Timeline ends at the last row | End at the final play | Quotes after the game are junk |

## Trade-offs to weigh

- **Proposal 4 changes what the headline metric means.** Entry quality would measure fills against
  another prediction market rather than sportsbooks. That detects Kalshi-only dislocations well, but
  it can't catch an error both venues share, so reports would need to say so.
- **Proposal 3 raises the false-positive risk,** since the idea came from looking at this sample.
  It must be judged on games outside these 10.
