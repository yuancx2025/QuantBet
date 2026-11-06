"""Builders for hand-written replay scenarios. Time is in seconds from the game's scheduled start."""

from __future__ import annotations

from quantbet_nba.engine.types import (
    US_PER_SECOND,
    GameBundle,
    GameInfo,
    MarketInfo,
    MarketOutcome,
    Quote,
    Trade,
)

HOME = "G-HOM"
AWAY = "G-AWY"
SCHEDULED = 1_000_000 * US_PER_SECOND


def at(seconds: float) -> int:
    return SCHEDULED + round(seconds * US_PER_SECOND)


def quote(seconds: float, market: str = HOME, bid: int | None = 50, ask: int | None = 52, volume=0):
    """Prices in cents."""
    return Quote(
        at(seconds),
        market,
        None if bid is None else bid * 100,
        None if ask is None else ask * 100,
        float(volume),
    )


def trade(seconds: float, cents: float, qty: float = 1, market: str = HOME) -> Trade:
    return Trade(at(seconds), market, round(cents * 100), float(qty))


def bundle(
    quotes=(),
    trades=(),
    home_result: str = "yes",
    home_settlement: int = 100,
    start: float = -4 * 3600,
    end: float = 3 * 3600,
    coverage: dict | str = "full",
    game_id: str = "G",
) -> GameBundle:
    away_settlement = 100 - home_settlement
    away_result = {"yes": "no", "no": "yes"}.get(home_result, home_result)
    window = (at(start), at(end))
    trade_coverage = {HOME: window, AWAY: window} if coverage == "full" else coverage
    return GameBundle(
        info=GameInfo(
            game_id,
            SCHEDULED,
            "HOM",
            "AWY",
            (MarketInfo(HOME, "HOM", True), MarketInfo(AWAY, "AWY", False)),
        ),
        start_us=at(start),
        end_us=at(end),
        quotes=sorted(quotes, key=lambda q: q.ts_us),
        trades=sorted(trades, key=lambda t: t.ts_us),
        outcomes={
            HOME: MarketOutcome(HOME, home_result, home_settlement * 100),
            AWAY: MarketOutcome(AWAY, away_result, away_settlement * 100),
        },
        trade_coverage=trade_coverage,
    )
