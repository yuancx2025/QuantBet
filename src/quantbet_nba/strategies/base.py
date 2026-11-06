"""The interface a strategy implements. Models, when they exist, are just another strategy."""

from __future__ import annotations

from typing import TYPE_CHECKING

from quantbet_nba.engine.types import Fill, Quote, Trade

if TYPE_CHECKING:
    from quantbet_nba.engine.replay import Context


class Strategy:
    """Reacts to one game's replayed events.

    The engine deep-copies the strategy for each game. Games overlap in real time but replay
    one after another, so carrying state between them could leak future information.
    """

    def on_game_start(self, ctx: Context) -> None:
        """Called once, before any event. Usually schedules the planning time."""

    def on_quote(self, ctx: Context, quote: Quote) -> None:
        """Called when a candle's closing quote becomes visible."""

    def on_trade(self, ctx: Context, trade: Trade) -> None:
        """Called for each print, after that print has been matched against open orders."""

    def on_fill(self, ctx: Context, fill: Fill) -> None:
        """Called when one of this strategy's orders fills."""
