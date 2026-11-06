# 0002: Exclude one game from the holdout

Date: 2026-11-06.

During planning, prices and trades for `KXNBAGAME-26NOV05MIAGSW` (Miami at Golden State, 2026-11-05)
were inspected to estimate API volume. It's a 2026-27 game, and that season is the holdout.

**Decision:** exclude this game from the holdout and record the exclusion in `configs/splits.yaml`
when that file is created in Stage 1.

**Why:** the holdout is only valid if nothing about it was seen before the frozen test. One game has
little effect, but excluding it is cheap and keeps the rule simple.

All Stage 0 analysis uses 2025-26 games. The spec's Stage 0 instructions said to use 2026-27 games,
and this project does not follow that part.
