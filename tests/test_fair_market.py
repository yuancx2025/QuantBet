"""An engine oracle: on a fair (martingale) price, a resting bid has no gross edge of its own.

Optional stopping gives an exact expectation. If the fair price is p when a bid at L fills, the
bid is worth p - L. Requiring a print strictly below L makes p a little lower than L, so the
expected result is slightly negative and equals the average of (p - L) across fills. If the
engine's settled profit differs from that, its fill timing or settlement is wrong.
"""

from __future__ import annotations

import math
from statistics import NormalDist

import numpy as np
import pytest
from helpers import HOME, at, bundle, quote

from quantbet_nba.engine.fees import QuadraticFee
from quantbet_nba.engine.fills import ThroughLimit
from quantbet_nba.engine.portfolio import fill_pnl
from quantbet_nba.engine.replay import replay_game
from quantbet_nba.engine.types import OrderStatus, Trade, pips_to_dollars
from quantbet_nba.strategies.base import Strategy

STEPS = 240
SECONDS_PER_STEP = 60
OFFSETS = (0.05, 0.10, 0.15)
normal_cdf = np.vectorize(NormalDist().cdf)


class BidsBelowOpen(Strategy):
    def __init__(self, open_price):
        self.open_price = open_price

    def on_game_start(self, ctx):
        ctx.schedule(at(-7200), self._place)

    def _place(self, ctx):
        for offset in OFFSETS:
            ctx.buy_limit(HOME, self.open_price - offset)


def fair_paths(rng, n_games, p0):
    """P(win) from a Gaussian score walk: a Doob martingale that ends at the game's result."""
    start = math.sqrt(STEPS) * NormalDist().inv_cdf(p0)
    walk = start + np.cumsum(rng.standard_normal((n_games, STEPS)), axis=1)
    remaining = np.arange(STEPS - 1, 0, -1)
    return normal_cdf(walk[:, :-1] / np.sqrt(remaining)), walk[:, -1] > 0


@pytest.mark.parametrize("p0", [0.35, 0.5, 0.7])
def test_settled_profit_matches_the_fair_value_at_fill(p0):
    rng = np.random.default_rng(20261106)
    fair, wins = fair_paths(rng, 3000, p0)
    pregame = round(p0 * 100)
    strategy = BidsBelowOpen(p0)
    no_fee = QuadraticFee(0)

    per_game = []
    fair_edges = []
    n_fills = 0
    loser_rungs_unfilled = 0
    for path, won in zip(fair, wins, strict=True):
        cents = np.clip(np.round(path * 100), 1, 99).astype(int)
        trades = [
            Trade(at(SECONDS_PER_STEP * i), HOME, int(c) * 100, 1.0) for i, c in enumerate(cents)
        ]
        game = bundle(
            [quote(-7300, HOME, pregame - 1, pregame + 1)],
            trades,
            home_result="yes" if won else "no",
            home_settlement=100 if won else 0,
            end=SECONDS_PER_STEP * STEPS,
        )
        result = replay_game(game, strategy, ThroughLimit())
        per_game.append(sum(fill_pnl(f, game.outcomes[HOME], no_fee)[0] for f in result.fills))
        for fill in result.fills:
            step = round((fill.ts_us - at(0)) / (SECONDS_PER_STEP * 1_000_000))
            fair_edges.append(path[step] - pips_to_dollars(fill.price_pips))
        n_fills += len(result.fills)
        if not won:
            loser_rungs_unfilled += sum(o.status is OrderStatus.UNFILLED for o in result.orders)

    assert n_fills > 1000
    settled = sum(per_game) / n_fills
    expected = float(np.mean(fair_edges))
    standard_error = float(np.std(per_game, ddof=1)) * math.sqrt(len(per_game)) / n_fills
    assert abs(settled - expected) < 3 * standard_error, (
        f"settled {settled:+.4f} vs fair value at fill {expected:+.4f} (se {standard_error:.4f})"
    )
    assert expected < 0, "a print strictly below L must mean fair value below L, on average"

    # A price that ends near zero passes through every bid, so losers' rungs all but always fill.
    assert loser_rungs_unfilled <= 0.02 * len(OFFSETS) * int((~wins).sum())
