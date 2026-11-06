"""Replay one game's events in time order against a strategy.

Lookahead protection is structural: a strategy holds a Context, which exposes only what has
already been replayed, plus GameInfo, which has no result fields. Outcomes stay in the bundle
for accounting. Events tied on timestamp run in a fixed order: quotes, trades, then timers.
"""

from __future__ import annotations

import heapq
from bisect import bisect_right
from collections.abc import Callable
from dataclasses import dataclass
from itertools import count

from quantbet_nba.engine.fills import FillModel
from quantbet_nba.engine.types import (
    US_PER_SECOND,
    Fill,
    GameBundle,
    GameInfo,
    GameResult,
    Order,
    OrderStatus,
    Quote,
    Skip,
    dollars_to_pips,
    floor_to_tick,
    pips_to_dollars,
)
from quantbet_nba.strategies.base import Strategy


@dataclass(frozen=True)
class ExecutionConfig:
    placement_latency_s: float = 2.0
    cancel_latency_s: float = 2.0
    min_price: float = 0.02
    max_price: float = 0.98
    max_quote_age_s: float = 900.0
    max_game_reserved: float | None = None


class Context:
    """A strategy's only view of the replay."""

    def __init__(self, replay: _Replay) -> None:
        self._replay = replay

    @property
    def now(self) -> int:
        return self._replay.now

    @property
    def game(self) -> GameInfo:
        return self._replay.bundle.info

    def quote(self, market: str) -> Quote | None:
        """The latest quote already visible, or None. It may be stale; see quote_age_s."""
        return self._replay.latest_quote.get(market)

    def quote_age_s(self, market: str) -> float | None:
        quote = self.quote(market)
        return None if quote is None else (self.now - quote.ts_us) / US_PER_SECOND

    def volume(self, market: str, lookback_s: float) -> float:
        """Contracts traded in candles already visible within the lookback window."""
        return self._replay.visible_volume(market, lookback_s)

    def schedule(self, ts_us: int, callback: Callable[[Context], None]) -> None:
        if ts_us < self.now:
            raise ValueError("cannot schedule a callback in the past")
        self._replay.add_timer(ts_us, callback)

    def buy_limit(
        self,
        market: str,
        price: float,
        size: float = 1.0,
        labels: dict[str, str] | None = None,
    ) -> Order:
        """Submit a resting bid. Invalid orders come back with status REJECTED and a reason."""
        return self._replay.submit(market, price, size, labels or {})

    def cancel(self, order: Order) -> None:
        """Request a cancel. The order can still fill until the latency has elapsed."""
        if order.cancel_us is None and order.status is OrderStatus.OPEN:
            order.cancel_us = self.now + round(
                self._replay.config.cancel_latency_s * US_PER_SECOND
            )

    def skip(self, market: str, reason: str) -> None:
        self._replay.skips.append(Skip(self.game.game_id, market, self.now, reason))

    def open_orders(self, market: str | None = None) -> list[Order]:
        books = self._replay.books
        if market is not None:
            return list(books.get(market, ()))
        return [order for book in books.values() for order in book]


class _Replay:
    def __init__(
        self,
        bundle: GameBundle,
        strategy: Strategy,
        fill_model: FillModel,
        config: ExecutionConfig,
    ) -> None:
        self.bundle = bundle
        self.strategy = strategy
        self.fill_model = fill_model
        self.config = config
        self.now = bundle.start_us
        self.latest_quote: dict[str, Quote] = {}
        self.books: dict[str, list[Order]] = {}  # open orders, best price first
        self.orders: list[Order] = []
        self.fills: list[Fill] = []
        self.skips: list[Skip] = []
        self.reserved = 0.0
        self._timers: list[tuple[int, int, Callable[[Context], None]]] = []
        self._timer_seq = count()
        self._order_seq = count()
        self._volume_ts: dict[str, list[int]] = {}
        self._volume_cum: dict[str, list[float]] = {}
        self.ctx = Context(self)

    # --- Strategy-facing helpers ---------------------------------------------------------

    def add_timer(self, ts_us: int, callback: Callable[[Context], None]) -> None:
        heapq.heappush(self._timers, (ts_us, next(self._timer_seq), callback))

    def visible_volume(self, market: str, lookback_s: float) -> float:
        times = self._volume_ts.get(market)
        if not times:
            return 0.0
        totals = self._volume_cum[market]
        cutoff = self.now - round(lookback_s * US_PER_SECOND)
        before = bisect_right(times, cutoff)
        return totals[-1] - (totals[before - 1] if before else 0.0)

    def submit(self, market: str, price: float, size: float, labels: dict[str, str]) -> Order:
        limit = floor_to_tick(dollars_to_pips(price))
        quote = self.latest_quote.get(market)
        order = Order(
            order_id=next(self._order_seq),
            game_id=self.bundle.info.game_id,
            market=market,
            limit_pips=limit,
            size=size,
            placed_us=self.now,
            active_us=self.now + round(self.config.placement_latency_s * US_PER_SECOND),
            labels=labels,
            quote_bid_pips=quote.bid_pips if quote else None,
            quote_ask_pips=quote.ask_pips if quote else None,
            pre_volume_1h=self.visible_volume(market, 3600),
        )
        self.orders.append(order)

        reason = self._rejection(order, quote)
        if reason:
            order.status, order.reason = OrderStatus.REJECTED, reason
            return order
        self.reserved += pips_to_dollars(limit) * size
        book = self.books.setdefault(market, [])
        book.append(order)
        book.sort(key=lambda o: (-o.limit_pips, o.order_id))
        return order

    def _rejection(self, order: Order, quote: Quote | None) -> str:
        cfg = self.config
        known = {m.ticker for m in self.bundle.info.markets}
        if order.market not in known:
            return "unknown_market"
        if order.size <= 0:
            return "invalid_size"
        if not dollars_to_pips(cfg.min_price) <= order.limit_pips <= dollars_to_pips(cfg.max_price):
            return "price_out_of_range"
        if quote is None:
            return "no_quote"
        if (self.now - quote.ts_us) / US_PER_SECOND > cfg.max_quote_age_s:
            return "stale_quote"
        if quote.ask_pips is not None and order.limit_pips >= quote.ask_pips:
            return "marketable"
        cost = pips_to_dollars(order.limit_pips) * order.size
        if cfg.max_game_reserved is not None and self.reserved + cost > cfg.max_game_reserved:
            return "game_limit"
        return ""

    # --- Event loop ----------------------------------------------------------------------

    def run(self) -> GameResult:
        bundle, strategy = self.bundle, self.strategy
        wants_quote = type(strategy).on_quote is not Strategy.on_quote
        wants_trade = type(strategy).on_trade is not Strategy.on_trade
        end, quotes, trades = bundle.end_us, bundle.quotes, bundle.trades
        n_quotes, n_trades = len(quotes), len(trades)
        q = t = 0
        strategy.on_game_start(self.ctx)

        while True:
            quote_ts = quotes[q].ts_us if q < n_quotes else None
            trade_ts = trades[t].ts_us if t < n_trades else None
            timer_ts = self._timers[0][0] if self._timers else None
            pending = [ts for ts in (quote_ts, trade_ts, timer_ts) if ts is not None]
            if not pending or min(pending) > end:
                break
            now = min(pending)
            self.now = now

            if quote_ts == now:
                quote = quotes[q]
                q += 1
                self._see_quote(quote)
                if wants_quote:
                    strategy.on_quote(self.ctx, quote)
            elif trade_ts == now:
                trade = trades[t]
                t += 1
                if trade.market in self.books:
                    self._match(trade)
                if wants_trade:
                    strategy.on_trade(self.ctx, trade)
            else:
                _, _, callback = heapq.heappop(self._timers)
                callback(self.ctx)

            # Nothing further can change the result of a passive strategy with no open orders.
            if not (self.books or self._timers or wants_quote or wants_trade):
                break

        self.now = end
        self._finalize()
        return GameResult(
            bundle.info, end, self.orders, self.fills, self.skips, bundle.outcomes
        )

    def _see_quote(self, quote: Quote) -> None:
        self.latest_quote[quote.market] = quote
        times = self._volume_ts.setdefault(quote.market, [])
        totals = self._volume_cum.setdefault(quote.market, [])
        times.append(quote.ts_us)
        totals.append((totals[-1] if totals else 0.0) + quote.volume)

    def _match(self, trade) -> None:
        crosses = self.fill_model.crosses
        limited = self.fill_model.limited_by_trade_qty
        budget = trade.qty
        book = self.books[trade.market]
        closed = False
        for order in list(book):  # a copy: on_fill may add orders to this book
            if order.cancel_us is not None and trade.ts_us >= order.cancel_us:
                order.status = OrderStatus.CANCELLED
                closed = True
                continue
            # Strictly after activation: a print at that instant can't be proven to reach us.
            if trade.ts_us <= order.active_us or not crosses(order.limit_pips, trade.price_pips):
                continue
            qty = min(order.remaining, budget) if limited else order.remaining
            if qty <= 0:
                continue
            if limited:
                budget -= qty
            order.filled += qty
            fill = Fill(
                order.order_id, order.game_id, order.market, trade.ts_us, qty, order.limit_pips
            )
            self.fills.append(fill)
            self.strategy.on_fill(self.ctx, fill)
            if order.remaining <= 0:
                order.status = OrderStatus.FILLED
                closed = True
        if closed:
            book[:] = [o for o in book if o.status is OrderStatus.OPEN]
            if not book:
                del self.books[trade.market]

    def _covered(self, market: str, start_us: int, end_us: int) -> bool:
        window = self.bundle.trade_coverage.get(market)
        return window is not None and window[0] <= start_us and end_us <= window[1]

    def _finalize(self) -> None:
        end = self.bundle.end_us
        for order in self.orders:
            if order.status is not OrderStatus.OPEN:
                # A fully filled order is settled regardless of later coverage. A cancel seen
                # during matching still needs the time before it to have been observable.
                if order.status is OrderStatus.CANCELLED and order.cancel_us is not None:
                    self._close_unfilled(order, min(order.cancel_us, end), OrderStatus.CANCELLED)
                continue
            if order.cancel_us is not None and order.cancel_us <= end:
                self._close_unfilled(order, order.cancel_us, OrderStatus.CANCELLED)
            else:
                self._close_unfilled(order, end, OrderStatus.UNFILLED)
        self.books.clear()

    def _close_unfilled(self, order: Order, until_us: int, status: OrderStatus) -> None:
        if not self._covered(order.market, order.active_us, until_us):
            order.status, order.reason = OrderStatus.UNKNOWN, "missing_trade_coverage"
        elif order.filled > 0 and status is OrderStatus.UNFILLED:
            order.status = OrderStatus.PARTIAL
        else:
            order.status = status


def replay_game(
    bundle: GameBundle,
    strategy: Strategy,
    fill_model: FillModel,
    config: ExecutionConfig | None = None,
) -> GameResult:
    return _Replay(bundle, strategy, fill_model, config or ExecutionConfig()).run()
