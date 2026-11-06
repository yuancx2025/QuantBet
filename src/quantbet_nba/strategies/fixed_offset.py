"""The baseline: fixed bids below the Kalshi mid, placed once before the game, held through it."""

from __future__ import annotations

from dataclasses import dataclass, field

from quantbet_nba.engine.replay import Context
from quantbet_nba.engine.types import US_PER_SECOND, MarketInfo
from quantbet_nba.strategies.base import Strategy

SIDES = ("favorite", "underdog", "both")


@dataclass
class FixedOffsetLadder(Strategy):
    """Bid `offsets_cents` below the mid, `hours_before` hours before scheduled tipoff.

    `side` picks the team using the two markets' mids at planning time. Equal mids have no
    favorite, so that game is skipped rather than broken by an arbitrary rule. `both` bids on
    each team, which can fill both sides of one game.
    """

    side: str = "both"
    offsets_cents: tuple[int, ...] = field(default=(5, 10, 15))
    size: float = 1.0
    hours_before: float = 2.0

    def __post_init__(self) -> None:
        if self.side not in SIDES:
            raise ValueError(f"side must be one of {SIDES}, got {self.side!r}")

    def on_game_start(self, ctx: Context) -> None:
        offset = round(self.hours_before * 3600 * US_PER_SECOND)
        ctx.schedule(max(ctx.game.scheduled_start_us - offset, ctx.now), self._plan)

    def _plan(self, ctx: Context) -> None:
        mids = {}
        for market in ctx.game.markets:
            quote = ctx.quote(market.ticker)
            mids[market.ticker] = quote.mid if quote else None
        if any(mid is None for mid in mids.values()):
            for market in ctx.game.markets:
                ctx.skip(market.ticker, "no_two_sided_quote")
            return

        home = ctx.game.market_for(home=True)
        away = ctx.game.market_for(home=False)
        roles = self._roles(home, away, mids)
        if roles is None:
            for market in ctx.game.markets:
                ctx.skip(market.ticker, "no_favorite")
            return

        for market in ctx.game.markets:
            role = roles[market.ticker]
            if self.side != "both" and role != self.side:
                continue
            for cents in self.offsets_cents:
                ctx.buy_limit(
                    market.ticker,
                    price=mids[market.ticker] - cents / 100,
                    size=self.size,
                    labels={"offset_cents": str(cents), "role": role},
                )

    def _roles(
        self, home: MarketInfo, away: MarketInfo, mids: dict[str, float]
    ) -> dict[str, str] | None:
        if mids[home.ticker] == mids[away.ticker]:
            # `both` can still trade: the role label is only a description there.
            if self.side == "both":
                return {home.ticker: "even", away.ticker: "even"}
            return None
        favorite = home if mids[home.ticker] > mids[away.ticker] else away
        underdog = away if favorite is home else home
        return {favorite.ticker: "favorite", underdog.ticker: "underdog"}
