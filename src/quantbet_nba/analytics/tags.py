"""Tag each fill as a sweep or as drift. A diagnostic only: it uses prices after the fill.

Rule, fixed before any full-season result was seen. Let m = 5 cents and h = 5 minutes.
- sweep: the mid just before the fill and the mid h later are both at least L + m. The print
  below L was a brief excursion and the market went back.
- drift: at least one of those mids is below L + m. The price really had moved down.
- unknown: either mid is unavailable, including when h runs past the final play.

Candle mids update once a minute, so a fast real repricing can look like a sweep and vice versa.
These tags can't be pregame features, because they depend on what happens after the fill.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right

from quantbet_nba.engine.types import US_PER_SECOND, GameBundle, GameResult

RECOVERY_MARGIN_PIPS = 500
RECOVERY_DELAY_US = 300 * US_PER_SECOND


def tag_fills(result: GameResult, bundle: GameBundle) -> dict[tuple[int, int], str]:
    """Map (order_id, fill time) to a tag."""
    mids: dict[str, list[tuple[int, float]]] = {}
    for quote in bundle.quotes:
        if quote.two_sided:
            mids.setdefault(quote.market, []).append(
                (quote.ts_us, (quote.bid_pips + quote.ask_pips) / 2)
            )
    times = {market: [ts for ts, _ in rows] for market, rows in mids.items()}

    tags = {}
    for fill in result.fills:
        rows, stamps = mids.get(fill.market), times.get(fill.market)
        kind = "unknown"
        if rows:
            before = bisect_right(stamps, fill.ts_us) - 1
            after = bisect_left(stamps, fill.ts_us + RECOVERY_DELAY_US)
            if before >= 0 and after < len(rows):
                floor = fill.price_pips + RECOVERY_MARGIN_PIPS
                kind = "sweep" if rows[before][1] >= floor and rows[after][1] >= floor else "drift"
        tags[(fill.order_id, fill.ts_us)] = kind
    return tags
