"""Report what the dataset does and doesn't cover, before any strategy is run on it."""

from __future__ import annotations

import datetime as dt
from collections import Counter
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from quantbet_nba.config import ticker_date
from quantbet_nba.data.store import GameStore
from quantbet_nba.engine.types import US_PER_SECOND

MINUTE_US = 60 * US_PER_SECOND
HOUR_US = 3600 * US_PER_SECOND
MIN_GAME_HOURS, MAX_GAME_HOURS = 1.75, 5.0
EASTERN = ZoneInfo("America/New_York")


def candle_trade_check(
    candles: pl.DataFrame, trades: pl.DataFrame, start_us: int, end_us: int
) -> dict[str, int]:
    """Compare candles with trades, minute by minute, inside a trade-coverage window.

    A candle's low should equal the lowest print in its minute. Trade rows are what decide fills,
    so a minute with a candle low but no trades would mean we are missing fills.
    """
    prices = (
        trades.with_columns(
            ((pl.col("ts_us") + MINUTE_US - 1) // MINUTE_US * MINUTE_US).alias("end_us")
        )
        .group_by("market", "end_us")
        .agg(pl.col("price_pips").min().alias("min_trade"))
    )
    inside = candles.filter(
        pl.col("trade_low_pips").is_not_null()
        & (pl.col("end_us") - MINUTE_US >= start_us)
        & (pl.col("end_us") <= end_us)
    )
    joined = inside.join(prices, on=["market", "end_us"], how="left")
    candle_keys = candles.select("market", "end_us")
    in_window = prices.filter(
        (pl.col("end_us") - MINUTE_US >= start_us) & (pl.col("end_us") <= end_us)
    )
    return {
        "minutes_compared": joined.filter(pl.col("min_trade").is_not_null()).height,
        "low_differs": joined.filter(
            pl.col("min_trade").is_not_null() & (pl.col("min_trade") != pl.col("trade_low_pips"))
        ).height,
        "candle_without_trades": joined.filter(pl.col("min_trade").is_null()).height,
        "trades_without_candle": in_window.join(
            candle_keys, on=["market", "end_us"], how="anti"
        ).height,
    }


def build_coverage(store: GameStore, period: str, plan_hours_before: float = 2.0) -> dict[str, Any]:
    games = store.games(period)
    plan_offset = round(plan_hours_before * HOUR_US)
    months: dict[str, Counter] = {}
    totals: Counter = Counter()
    check_totals: Counter = Counter()
    problem_games: list[dict[str, Any]] = []
    for game in games:
        month = ticker_date(game["game_id"]).strftime("%Y-%m")
        counter = months.setdefault(month, Counter())
        counter["games"] += 1
        plan = game["scheduled_us"] - plan_offset
        candles = store.candles(game["game_id"])
        trades = store.trades(game["game_id"])

        complete = True
        game_check: Counter = Counter()
        for side in ("home", "away"):
            market = game[f"{side}_market"]
            start, stop = game[f"{side}_trades_from_us"], game[f"{side}_trades_to_us"]
            covers = start is not None and start <= plan and stop >= game["final_play_us"]
            complete &= covers
            market_candles = candles.filter(pl.col("market") == market)
            prior = market_candles.filter(pl.col("end_us") <= plan).tail(1)
            quoted = (
                prior.height == 1
                and bool(prior["bid_pips"][0])
                and prior["ask_pips"][0] is not None
                and prior["ask_pips"][0] < 10_000
                and plan - prior["end_us"][0] <= 900 * US_PER_SECOND
            )
            counter["markets"] += 1
            counter["markets_quoted_at_plan"] += quoted
            counter["markets_full_trade_window"] += covers
            if start is not None:
                check = candle_trade_check(
                    market_candles, trades.filter(pl.col("market") == market), start, stop
                )
                game_check.update(check)
        counter["games_complete"] += complete
        counter["trades"] += trades.height
        counter["volume"] += int(trades["qty"].sum())
        totals["reversal_games"] += bool(game["wallclock_reversals"])
        totals["fuzzy_ticker_dates"] += not game["exact_ticker_date"]
        totals["nonbinary_settlements"] += game["home_result"] not in ("yes", "no")
        totals["final_play_lag_over_60s"] += game["final_play_lag_us"] > 60 * US_PER_SECOND
        totals["first_play_clock_corrected"] += bool(game["first_play_shift_us"])
        totals["final_play_clock_corrected"] += bool(game["final_play_shift_us"])
        # After correction, these should all be zero. A nonzero count needs investigating.
        length_h = (game["final_play_us"] - game["scheduled_us"]) / HOUR_US
        totals["implausible_game_length"] += not MIN_GAME_HOURS <= length_h <= MAX_GAME_HOURS
        if not trades.is_empty():
            gap_us = abs(int(trades["ts_us"].max()) - game["final_play_us"])
            totals["final_play_far_from_last_trade"] += gap_us > 30 * MINUTE_US
        check_totals.update(game_check)
        if game_check["low_differs"] or game_check["candle_without_trades"]:
            problem_games.append({"game_id": game["game_id"], **game_check})

    exclusions = store.exclusions()
    reasons = dict(exclusions.group_by("reason").len().sort("reason").iter_rows())
    return {
        "period": period,
        "generated_at": dt.datetime.now(dt.UTC).isoformat(),
        "dataset_sha256": store.dataset_hash(),
        "usable_games": len(games),
        "exclusions": reasons,
        "months": {month: dict(months[month]) for month in sorted(months)},
        "flags": dict(totals),
        "candle_trade_check": dict(check_totals),
        "games_with_candle_trade_differences": problem_games,
    }


def coverage_markdown(report: dict[str, Any]) -> str:
    lines = [
        f"# Coverage: {report['period']}",
        "",
        f"Dataset `{report['dataset_sha256'][:12]}`. {report['usable_games']} usable games.",
        "",
        "| Month | Games | Full trade window | Markets quoted at plan | Trades |",
        "|---|---:|---:|---:|---:|",
    ]
    for month, row in report["months"].items():
        lines.append(
            f"| {month} | {row['games']} | {row.get('games_complete', 0)} | "
            f"{row.get('markets_quoted_at_plan', 0)}/{row.get('markets', 0)} | "
            f"{row.get('trades', 0):,} |"
        )
    lines += ["", "## Exclusions", ""]
    lines += [f"- `{k}`: {v}" for k, v in report["exclusions"].items()] or ["- None"]
    lines += ["", "## Flags", ""]
    lines += [f"- `{k}`: {v}" for k, v in report["flags"].items()]
    lines += ["", "## Candles compared with trades", ""]
    lines += [f"- `{k}`: {v:,}" for k, v in report["candle_trade_check"].items()]
    return "\n".join(lines) + "\n"
