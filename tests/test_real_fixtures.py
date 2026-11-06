"""Adapters against the 10 cached Stage 0 games. These test plumbing; profit here means nothing.

The games were selected on outcomes, and the raw cache isn't in Git, so these skip without it.
The spike fetched only home-market trades from 30 minutes before first play, which is exactly
the partial coverage the engine must mark as unknown.
"""

from __future__ import annotations

import datetime as dt
import sys

import polars as pl
import pytest

from quantbet_nba.config import ROOT
from quantbet_nba.data import espn, kalshi
from quantbet_nba.data.coverage import candle_trade_check
from quantbet_nba.data.store import build_bundle
from quantbet_nba.engine.fees import QuadraticFee
from quantbet_nba.engine.fills import ThroughLimit
from quantbet_nba.engine.portfolio import fill_pnl
from quantbet_nba.engine.replay import replay_game
from quantbet_nba.engine.types import US_PER_SECOND, OrderStatus
from quantbet_nba.strategies.fixed_offset import FixedOffsetLadder

CACHE = ROOT / "data" / "raw" / "spike"
pytestmark = pytest.mark.skipif(not CACHE.exists(), reason="Stage 0 raw cache is not present")


@pytest.fixture(scope="module")
def spike_games():
    sys.path.insert(0, str(ROOT / "scripts"))
    import stage0_spike as spike

    def refuse(url, params):
        raise RuntimeError(f"tests must not use the network: {url}")

    spike._download = refuse
    return spike.load_all()


def as_row(game) -> dict:
    meta = game.meta
    home, away = game.home_market, game.away_market
    scheduled = espn.to_us(meta["date"])
    first, final = espn.to_us(game.plays[0]["wallclock"]), espn.to_us(game.plays[-1]["wallclock"])
    trade_window = (first - 1800 * US_PER_SECOND, final + 900 * US_PER_SECOND)
    return {
        "game_id": game.event_ticker,
        "scheduled_us": scheduled,
        "final_play_us": final,
        "replay_start_us": first - 3 * 3600 * US_PER_SECOND,
        "home": espn.kalshi_abbr(meta["home"]),
        "away": espn.kalshi_abbr(meta["away"]),
        "home_market": home["ticker"],
        "away_market": away["ticker"],
        "home_result": kalshi.market_outcome(home)[0],
        "away_result": kalshi.market_outcome(away)[0],
        "home_settlement_pips": kalshi.market_outcome(home)[1],
        "away_settlement_pips": kalshi.market_outcome(away)[1],
        "home_trades_from_us": trade_window[0],
        "home_trades_to_us": trade_window[1],
        "away_trades_from_us": None,
        "away_trades_to_us": None,
    }


def tables(game):
    home, away = game.home_market["ticker"], game.away_market["ticker"]
    candles = pl.concat(
        [
            kalshi.normalize_candles(home, game.candles),
            kalshi.normalize_candles(away, game.away_candles),
        ]
    )
    return candles, kalshi.normalize_trades(home, game.trades)


@pytest.fixture(scope="module")
def bundles(spike_games):
    return {g.event_ticker: build_bundle(as_row(g), *tables(g)) for g in spike_games}


def test_every_fixture_settles_to_the_espn_winner(spike_games):
    for game in spike_games:
        home_won = game.meta["home_final"] > game.meta["away_final"]
        assert kalshi.market_outcome(game.home_market) == (("yes", 10_000) if home_won else ("no", 0))
        assert kalshi.market_outcome(game.away_market) == (("no", 0) if home_won else ("yes", 10_000))


def test_candles_agree_with_trades_as_stage0_reported(spike_games):
    total = {"minutes_compared": 0, "low_differs": 0, "trades_without_candle": 0}
    for game in spike_games:
        row = as_row(game)
        home = row["home_market"]
        candles, trades = tables(game)
        check = candle_trade_check(
            candles.filter(pl.col("market") == home),
            trades,
            row["home_trades_from_us"],
            row["home_trades_to_us"],
        )
        for key in total:
            total[key] += check[key]
    assert total["minutes_compared"] > 1500
    assert total["low_differs"] == 0
    assert total["trades_without_candle"] == 0


def test_planning_quotes_match_the_known_values(bundles):
    result = replay_game(
        bundles["KXNBAGAME-26MAR27LACIND"], FixedOffsetLadder(side="both"), ThroughLimit()
    )
    # IND was 21/22 and LAC 78/79 at planning time, from the readiness check on this cache.
    by_market = {o.market: o for o in result.orders}
    home = by_market["KXNBAGAME-26MAR27LACIND-IND"]
    away = by_market["KXNBAGAME-26MAR27LACIND-LAC"]
    assert (home.quote_bid_pips, home.quote_ask_pips) == (2100, 2200)
    assert (away.quote_bid_pips, away.quote_ask_pips) == (7800, 7900)
    assert home.labels["role"] == "underdog" and away.labels["role"] == "favorite"


def test_known_flash_sweep_fills_the_home_rungs(bundles):
    # Stage 0's clearest case: five trades took LAC from about 72c to 13c with the game tied.
    sweep = int(dt.datetime(2025, 10, 25, 2, 50, 54, tzinfo=dt.UTC).timestamp()) * US_PER_SECOND
    game = bundles["KXNBAGAME-25OCT24PHXLAC"]
    result = replay_game(game, FixedOffsetLadder(side="favorite"), ThroughLimit())
    assert all(o.status is OrderStatus.FILLED for o in result.orders)
    assert all(f.ts_us < sweep + US_PER_SECOND for f in result.fills)
    # Fills are at each limit, not at the 13c print.
    assert sorted(f.price_pips for f in result.fills) == [6300, 6800, 7300]
    # The home team won that game, so each rung settled at $1.
    outcome = game.outcomes[result.fills[0].market]
    assert all(fill_pnl(f, outcome, QuadraticFee(0))[0] > 0 for f in result.fills)


def test_rungs_without_full_trade_data_are_unknown_never_unfilled(bundles):
    statuses = {"away": set(), "home": set()}
    for game in bundles.values():
        result = replay_game(game, FixedOffsetLadder(side="both"), ThroughLimit())
        for order in result.orders:
            if order.status is OrderStatus.REJECTED:
                continue
            side = "home" if order.market == game.info.market_for(home=True).ticker else "away"
            statuses[side].add(order.status)
    # No away trades were fetched at all, so nothing there can be decided.
    assert statuses["away"] == {OrderStatus.UNKNOWN}
    # Home trades start after planning time: a seen print proves a fill; its absence proves nothing.
    assert OrderStatus.FILLED in statuses["home"]
    assert OrderStatus.UNFILLED not in statuses["home"]


def test_nothing_after_the_final_play_reaches_the_replay(bundles):
    for game in bundles.values():
        assert all(q.ts_us <= game.end_us for q in game.quotes)
        assert all(t.ts_us <= game.end_us for t in game.trades)
