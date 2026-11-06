"""Settlement accounting and portfolio-level capital limits.

Games replay independently, so the portfolio limit is applied afterwards, walking all orders
in time order. That is exact for strategies whose decisions don't depend on other games, and
every v1 strategy qualifies. Whether Kalshi reserves funds for resting orders is unverified;
this module conservatively assumes it does, from placement until the order closes.
"""

from __future__ import annotations

import heapq

from quantbet_nba.engine.fees import QuadraticFee
from quantbet_nba.engine.types import Fill, GameResult, MarketOutcome, OrderStatus, pips_to_dollars


def fill_pnl(fill: Fill, outcome: MarketOutcome, fee_model: QuadraticFee) -> tuple[float, float]:
    """Return (gross profit, fee) in dollars. Uses cash settlement, so nonbinary results work."""
    price = pips_to_dollars(fill.price_pips)
    gross = fill.qty * (pips_to_dollars(outcome.settlement_pips) - price)
    return gross, fee_model.fee(price, fill.qty)


def apply_portfolio_limit(
    results: list[GameResult], max_reserved: float | None
) -> tuple[list[GameResult], float]:
    """Reject orders that would exceed the limit. Returns results and peak reserved dollars."""
    entries = []
    for result in results:
        for order in result.orders:
            if order.status is OrderStatus.REJECTED:
                continue
            release = result.end_us
            if order.status is OrderStatus.CANCELLED and order.filled == 0 and order.cancel_us:
                release = min(order.cancel_us, result.end_us)
            entries.append((order.placed_us, order.game_id, order.order_id, release, order))
    entries.sort(key=lambda e: e[:3])

    reserved = peak = 0.0
    releases: list[tuple[int, int, float]] = []
    rejected: set[tuple[str, int]] = set()
    seq = 0
    for placed, _game, _order_id, release, order in entries:
        while releases and releases[0][0] <= placed:
            reserved -= heapq.heappop(releases)[2]
        cost = pips_to_dollars(order.limit_pips) * order.size
        if max_reserved is not None and reserved + cost > max_reserved:
            order.status, order.reason = OrderStatus.REJECTED, "portfolio_limit"
            order.filled = 0.0
            rejected.add((order.game_id, order.order_id))
            continue
        reserved += cost
        peak = max(peak, reserved)
        heapq.heappush(releases, (release, seq, cost))
        seq += 1

    if rejected:
        for result in results:
            result.fills = [
                f for f in result.fills if (f.game_id, f.order_id) not in rejected
            ]
    return results, peak
