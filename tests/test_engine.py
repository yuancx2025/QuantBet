"""Order lifecycle, fill rules, fees, settlement, limits, and lookahead protection."""

from __future__ import annotations

import copy
from dataclasses import replace

import pytest
from helpers import AWAY, HOME, at, bundle, quote, trade

from quantbet_nba.engine.fees import QuadraticFee
from quantbet_nba.engine.fills import ThroughLimit, ThroughWithVolume, TouchAtLimit
from quantbet_nba.engine.portfolio import apply_portfolio_limit, fill_pnl
from quantbet_nba.engine.replay import Context, ExecutionConfig, replay_game
from quantbet_nba.engine.types import OrderStatus, Trade
from quantbet_nba.strategies.base import Strategy
from quantbet_nba.strategies.fixed_offset import FixedOffsetLadder


class Bids(Strategy):
    """Places given (market, price, size) orders at a given time."""

    def __init__(self, orders, when=-7200.0):
        self.orders, self.when = orders, when

    def on_game_start(self, ctx: Context) -> None:
        ctx.schedule(at(self.when), self._place)

    def _place(self, ctx: Context) -> None:
        for market, price, size in self.orders:
            ctx.buy_limit(market, price, size)


QUOTE = quote(-7300)
ONE_BID = Bids([(HOME, 0.45, 1)])


def test_print_exactly_at_limit_needs_the_optimistic_rule():
    game = bundle([QUOTE], [trade(0, 45)])
    assert replay_game(game, ONE_BID, TouchAtLimit()).orders[0].status is OrderStatus.FILLED
    assert replay_game(game, ONE_BID, ThroughLimit()).orders[0].status is OrderStatus.UNFILLED


def test_print_below_limit_fills_at_the_limit_not_the_print():
    result = replay_game(bundle([QUOTE], [trade(0, 13)]), ONE_BID, ThroughLimit())
    assert result.orders[0].status is OrderStatus.FILLED
    assert result.fills[0].price_pips == 4500


def test_print_before_activation_cannot_fill():
    config = ExecutionConfig(placement_latency_s=2)
    before = trade(-7199, 10)  # one second after placement
    boundary = trade(-7198, 10)  # exactly at activation
    after = trade(-7197, 10)
    assert replay_game(bundle([QUOTE], [before, boundary]), ONE_BID, ThroughLimit(), config).fills == []
    filled = replay_game(bundle([QUOTE], [before, after]), ONE_BID, ThroughLimit(), config)
    assert filled.fills[0].ts_us == after.ts_us


def test_trades_after_final_play_are_ignored():
    game = bundle([QUOTE], [trade(3 * 3600 + 1, 1)], end=3 * 3600)
    assert replay_game(game, ONE_BID, ThroughLimit()).orders[0].status is OrderStatus.UNFILLED


def test_volume_rule_fills_partially_and_accumulates():
    strategy = Bids([(HOME, 0.45, 10)])
    one_print = replay_game(bundle([QUOTE], [trade(0, 40, qty=4)]), strategy, ThroughWithVolume())
    assert one_print.orders[0].filled == 4
    assert one_print.orders[0].status is OrderStatus.PARTIAL

    two_prints = bundle([QUOTE], [trade(0, 40, qty=4), trade(1, 40, qty=6)])
    result = replay_game(two_prints, strategy, ThroughWithVolume())
    assert result.orders[0].status is OrderStatus.FILLED
    assert [f.qty for f in result.fills] == [4, 6]


def test_rungs_share_a_print_with_the_highest_limit_first():
    strategy = Bids([(HOME, 0.40, 5), (HOME, 0.45, 5)])
    result = replay_game(bundle([QUOTE], [trade(0, 30, qty=7)]), strategy, ThroughWithVolume())
    low, high = result.orders
    assert (high.filled, low.filled) == (5, 2)


def test_through_limit_ignores_print_size():
    strategy = Bids([(HOME, 0.40, 5), (HOME, 0.45, 5)])
    result = replay_game(bundle([QUOTE], [trade(0, 30, qty=1)]), strategy, ThroughLimit())
    assert all(o.status is OrderStatus.FILLED for o in result.orders)


@pytest.mark.parametrize(
    ("price", "quotes", "reason"),
    [
        (0.52, [quote(-7300)], "marketable"),
        (0.60, [quote(-7300)], "marketable"),
        (0.01, [quote(-7300)], "price_out_of_range"),
        (0.99, [quote(-7300, ask=None)], "price_out_of_range"),
        (0.45, [], "no_quote"),
        (0.45, [quote(-9000)], "stale_quote"),
    ],
)
def test_invalid_orders_are_rejected_with_a_reason(price, quotes, reason):
    order = replay_game(bundle(quotes), Bids([(HOME, price, 1)]), ThroughLimit()).orders[0]
    assert (order.status, order.reason) == (OrderStatus.REJECTED, reason)


def test_limit_rounds_down_to_a_valid_tick():
    order = replay_game(bundle([QUOTE]), Bids([(HOME, 0.459, 1)]), ThroughLimit()).orders[0]
    assert order.limit_pips == 4500


def test_future_quote_is_not_visible_at_planning_time():
    # The candle covering the planning time ends after it, so it can't be used yet.
    order = replay_game(bundle([quote(-7190)]), ONE_BID, ThroughLimit()).orders[0]
    assert order.reason == "no_quote"


class CancelLater(Bids):
    def __init__(self, cancel_at):
        super().__init__([(HOME, 0.45, 1)])
        self.cancel_at = cancel_at

    def _place(self, ctx):
        super()._place(ctx)
        order = ctx.open_orders(HOME)[0]
        ctx.schedule(at(self.cancel_at), lambda c: c.cancel(order))


def test_cancel_only_takes_effect_after_latency():
    config = ExecutionConfig(cancel_latency_s=2)
    in_latency = bundle([QUOTE], [trade(1, 10)])
    result = replay_game(in_latency, CancelLater(0), ThroughLimit(), config)
    assert result.orders[0].status is OrderStatus.FILLED

    too_late = bundle([QUOTE], [trade(3, 10)])
    result = replay_game(too_late, CancelLater(0), ThroughLimit(), config)
    assert result.orders[0].status is OrderStatus.CANCELLED
    assert result.fills == []


def test_cancel_with_no_later_trades_still_closes_as_cancelled():
    result = replay_game(bundle([QUOTE]), CancelLater(0), ThroughLimit())
    assert result.orders[0].status is OrderStatus.CANCELLED


def test_missing_trade_data_is_unknown_not_unfilled():
    no_data = bundle([QUOTE], coverage={HOME: None, AWAY: None})
    order = replay_game(no_data, ONE_BID, ThroughLimit()).orders[0]
    assert (order.status, order.reason) == (OrderStatus.UNKNOWN, "missing_trade_coverage")


def test_partial_trade_coverage_is_unknown_when_no_fill_is_seen():
    late_start = {HOME: (at(-1800), at(3 * 3600)), AWAY: None}
    order = replay_game(bundle([QUOTE], coverage=late_start), ONE_BID, ThroughLimit()).orders[0]
    assert order.status is OrderStatus.UNKNOWN


def test_fill_seen_in_partial_coverage_still_counts():
    late_start = {HOME: (at(-1800), at(3 * 3600)), AWAY: None}
    game = bundle([QUOTE], [trade(0, 10)], coverage=late_start)
    assert replay_game(game, ONE_BID, ThroughLimit()).orders[0].status is OrderStatus.FILLED


def test_zero_trades_with_complete_coverage_is_a_real_nonfill():
    assert replay_game(bundle([QUOTE]), ONE_BID, ThroughLimit()).orders[0].status is OrderStatus.UNFILLED


def test_settlement_win_loss_and_exceptional_cash():
    win = replay_game(bundle([QUOTE], [trade(0, 40)]), ONE_BID, ThroughLimit())
    lose = replay_game(bundle([QUOTE], [trade(0, 40)], home_result="no", home_settlement=0), ONE_BID, ThroughLimit())
    scalar = replay_game(bundle([QUOTE], [trade(0, 40)], home_result="scalar", home_settlement=50), ONE_BID, ThroughLimit())
    no_fee = QuadraticFee(0)
    assert fill_pnl(win.fills[0], win.outcomes[HOME], no_fee)[0] == pytest.approx(0.55)
    assert fill_pnl(lose.fills[0], lose.outcomes[HOME], no_fee)[0] == pytest.approx(-0.45)
    assert fill_pnl(scalar.fills[0], scalar.outcomes[HOME], no_fee)[0] == pytest.approx(0.05)
    assert not scalar.outcomes[HOME].binary


def test_quadratic_fee():
    assert QuadraticFee(0.0175).fee(0.5, 100) == pytest.approx(0.4375)
    assert QuadraticFee(0).fee(0.5, 100) == 0


def test_game_reserved_limit_rejects_the_excess_order():
    config = ExecutionConfig(max_game_reserved=0.50)
    result = replay_game(
        bundle([QUOTE]), Bids([(HOME, 0.45, 1), (HOME, 0.40, 1)]), ThroughLimit(), config
    )
    assert result.orders[0].status is OrderStatus.UNFILLED
    assert result.orders[1].reason == "game_limit"


def test_portfolio_limit_rejects_overlap_and_releases_after_game_end():
    def run(game_id, start_shift):
        quotes = [replace(quote(-7300), ts_us=at(-7300 + start_shift))]
        trades = [replace(trade(0, 10), ts_us=at(start_shift))]
        game = bundle(quotes, trades, start=-4 * 3600 + start_shift, end=3600 + start_shift, game_id=game_id)
        game = replace(game, info=replace(game.info, scheduled_start_us=at(start_shift)))
        return replay_game(game, Bids([(HOME, 0.45, 1)], when=-7200 + start_shift), ThroughLimit())

    first, overlapping, later = run("A", 0), run("B", 10), run("C", 4 * 3600)
    _, peak = apply_portfolio_limit([first, overlapping, later], max_reserved=0.50)
    assert first.orders[0].status is OrderStatus.FILLED
    assert overlapping.orders[0].reason == "portfolio_limit"
    assert overlapping.fills == []
    assert later.orders[0].status is OrderStatus.FILLED
    assert peak == pytest.approx(0.45)


def test_fill_callback_may_place_a_new_order():
    class Reload(Bids):
        def on_fill(self, ctx, fill):
            if fill.price_pips == 4500:
                ctx.buy_limit(HOME, 0.30, 1)

    quotes = [QUOTE, quote(-60)]  # the reload needs a recent quote at the time of the fill
    result = replay_game(bundle(quotes, [trade(0, 40), trade(10, 20)]), Reload([(HOME, 0.45, 1)]), ThroughLimit())
    assert [o.status for o in result.orders] == [OrderStatus.FILLED, OrderStatus.FILLED]
    assert [f.price_pips for f in result.fills] == [4500, 3000]


def test_strategy_context_exposes_no_outcome():
    assert not hasattr(bundle().info, "outcomes")
    assert not any("result" in name or "settle" in name for name in dir(bundle().info))


class Recorder(Strategy):
    """A reactive strategy: bids based on everything it has seen so far."""

    def __init__(self):
        self.next_id = 0

    def on_trade(self, ctx, trade_event):
        self.next_id += 1
        if self.next_id % 3 == 0 and ctx.quote(HOME):
            ctx.buy_limit(HOME, ctx.quote(HOME).mid - 0.03, 1, labels={"seen": str(self.next_id)})


def _decisions_until(result, cutoff):
    orders = [(o.placed_us, o.limit_pips, dict(o.labels)) for o in result.orders if o.placed_us <= cutoff]
    fills = [(f.ts_us, f.order_id, f.qty, f.price_pips) for f in result.fills if f.ts_us <= cutoff]
    return orders, fills


def test_deleting_future_events_does_not_change_earlier_decisions():
    quotes = [quote(-7300 + 60 * i, bid=50 - i % 5, ask=52 - i % 5, volume=i) for i in range(150)]
    trades = [trade(-7200 + 37 * i, 50 - (i % 17), qty=1 + i % 3) for i in range(300)]
    full = bundle(quotes, trades)
    cutoff = at(-7200 + 37 * 120)
    truncated = copy.deepcopy(full)
    truncated.quotes = [q for q in full.quotes if q.ts_us <= cutoff]
    truncated.trades = [t for t in full.trades if t.ts_us <= cutoff]

    full_result = replay_game(full, Recorder(), ThroughLimit())
    cut_result = replay_game(truncated, Recorder(), ThroughLimit())
    assert _decisions_until(full_result, cutoff) == _decisions_until(cut_result, cutoff)
    assert _decisions_until(full_result, cutoff)[1], "scenario must produce fills to be meaningful"


def test_changing_the_outcome_does_not_change_decisions():
    quotes = [QUOTE]
    trades = [trade(0, 40)]
    win = replay_game(bundle(quotes, trades), ONE_BID, ThroughLimit())
    loss = replay_game(bundle(quotes, trades, home_result="no", home_settlement=0), ONE_BID, ThroughLimit())
    assert win.fills == loss.fills


# --- The baseline strategy ------------------------------------------------------------------


def two_market_quotes(home=(60, 62), away=(38, 40)):
    return [quote(-7300, HOME, *home), quote(-7300, AWAY, *away)]


@pytest.mark.parametrize(
    ("side", "expected"),
    [("favorite", {HOME}), ("underdog", {AWAY}), ("both", {HOME, AWAY})],
)
def test_ladder_selects_side_from_planning_time_mids(side, expected):
    result = replay_game(bundle(two_market_quotes()), FixedOffsetLadder(side=side), ThroughLimit())
    assert {o.market for o in result.orders} == expected
    assert len(result.orders) == 3 * len(expected)


def test_ladder_prices_and_labels():
    result = replay_game(bundle(two_market_quotes()), FixedOffsetLadder(side="favorite"), ThroughLimit())
    assert sorted(o.limit_pips for o in result.orders) == [4600, 5100, 5600]
    assert {o.labels["role"] for o in result.orders} == {"favorite"}
    assert sorted(o.labels["offset_cents"] for o in result.orders) == ["10", "15", "5"]


def test_ladder_mid_between_cents_rounds_bid_down():
    result = replay_game(bundle(two_market_quotes(home=(60, 61))), FixedOffsetLadder(side="favorite", offsets_cents=(5,)), ThroughLimit())
    assert result.orders[0].limit_pips == 5500  # 0.605 - 0.05 = 0.555 -> 0.55


def test_ladder_skips_equal_mids_for_one_sided_variants_only():
    even = two_market_quotes(home=(49, 51), away=(49, 51))
    skipped = replay_game(bundle(even), FixedOffsetLadder(side="favorite"), ThroughLimit())
    assert skipped.orders == [] and {s.reason for s in skipped.skips} == {"no_favorite"}
    traded = replay_game(bundle(even), FixedOffsetLadder(side="both"), ThroughLimit())
    assert len(traded.orders) == 6


def test_ladder_skips_when_a_market_has_no_two_sided_quote():
    quotes = two_market_quotes(home=(0, 62))
    result = replay_game(bundle(quotes), FixedOffsetLadder(), ThroughLimit())
    assert result.orders == [] and {s.reason for s in result.skips} == {"no_two_sided_quote"}


def test_ladder_low_price_rung_is_rejected_but_others_trade():
    quotes = two_market_quotes(home=(90, 92), away=(8, 10))
    result = replay_game(bundle(quotes), FixedOffsetLadder(side="underdog"), ThroughLimit())
    reasons = {o.labels["offset_cents"]: o.reason for o in result.orders}
    assert reasons["5"] == "" and reasons["10"] == "price_out_of_range"


def test_block_style_trade_is_just_data_the_engine_never_sees_future_fields():
    # Trade records carry only what was printed.
    assert set(Trade.__slots__) == {"ts_us", "market", "price_pips", "qty", "trade_id"}
