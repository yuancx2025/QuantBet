# QuantBet NBA

Research system testing whether resting Kalshi limit orders on NBA game-winner
markets (`KXNBAGAME`) fill below a team's true chance of winning, by more than fees.
It produces plans and backtests. It never places orders.

Status: Stage 0 spike complete, awaiting review. Findings are in `docs/decisions/`.

```bash
uv sync
uv run python scripts/stage0_spike.py all
```

Charts and the summary are written to `reports/stage0/`.
