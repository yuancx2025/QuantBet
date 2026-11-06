"""Run a strategy over a period's games and return its orders, fills, and per-game results."""

from __future__ import annotations

import copy
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from itertools import repeat
from pathlib import Path
from typing import Any

import polars as pl

from quantbet_nba.analytics.tags import tag_fills
from quantbet_nba.config import Splits, ticker_date
from quantbet_nba.data.store import GameStore
from quantbet_nba.engine.fills import FILL_MODELS
from quantbet_nba.engine.portfolio import apply_portfolio_limit
from quantbet_nba.engine.replay import ExecutionConfig, replay_game
from quantbet_nba.engine.types import GameResult, pips_to_dollars
from quantbet_nba.strategies.base import Strategy

ORDER_SCHEMA = {
    "game_id": pl.String,
    "order_id": pl.Int64,
    "market": pl.String,
    "month": pl.String,
    "limit": pl.Float64,
    "size": pl.Float64,
    "status": pl.String,
    "reason": pl.String,
    "filled": pl.Float64,
    "placed_us": pl.Int64,
    "active_us": pl.Int64,
    "quote_bid": pl.Float64,
    "quote_ask": pl.Float64,
    "pre_volume_1h": pl.Float64,
}
FILL_SCHEMA = {
    "game_id": pl.String,
    "order_id": pl.Int64,
    "market": pl.String,
    "ts_us": pl.Int64,
    "qty": pl.Float64,
    "price": pl.Float64,
    "settlement": pl.Float64,
    "won": pl.Int8,
    "gross_pnl": pl.Float64,
    "fill_kind": pl.String,
}
GAME_SCHEMA = {
    "game_id": pl.String,
    "espn_id": pl.String,
    "month": pl.String,
    "scheduled_us": pl.Int64,
    "end_us": pl.Int64,
}
SKIP_SCHEMA = {
    "game_id": pl.String,
    "market": pl.String,
    "ts_us": pl.Int64,
    "reason": pl.String,
}


@dataclass(frozen=True)
class BacktestConfig:
    period: str = "development"
    season: str = "2025-26"
    fill_model: str = "through_limit"
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)
    max_portfolio_reserved: float | None = None
    workers: int = 1


@dataclass
class BacktestRun:
    orders: pl.DataFrame
    fills: pl.DataFrame
    games: pl.DataFrame
    skips: pl.DataFrame
    peak_reserved: float


def _replay_one(
    game: dict[str, Any],
    data_root: Path,
    strategy: Strategy,
    config: BacktestConfig,
    splits: Splits,
) -> tuple[GameResult, dict[tuple[int, int], str]]:
    store = GameStore(data_root, config.season, splits)
    bundle = store.load(game)
    model = FILL_MODELS[config.fill_model]()
    result = replay_game(bundle, copy.deepcopy(strategy), model, config.execution)
    return result, tag_fills(result, bundle)


def _to_frames(
    results: list[GameResult],
    kinds: dict[tuple[str, int, int], str],
    games: list[dict[str, Any]],
) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    orders, fills, skips = [], [], []
    for result in results:
        game_id = result.info.game_id
        month = ticker_date(game_id).strftime("%Y-%m")
        for order in result.orders:
            orders.append(
                {
                    "game_id": game_id,
                    "order_id": order.order_id,
                    "market": order.market,
                    "month": month,
                    "limit": pips_to_dollars(order.limit_pips),
                    "size": order.size,
                    "status": order.status.value,
                    "reason": order.reason,
                    "filled": order.filled,
                    "placed_us": order.placed_us,
                    "active_us": order.active_us,
                    "quote_bid": _dollars(order.quote_bid_pips),
                    "quote_ask": _dollars(order.quote_ask_pips),
                    "pre_volume_1h": order.pre_volume_1h,
                    **{f"label_{key}": value for key, value in order.labels.items()},
                }
            )
        for fill in result.fills:
            outcome = result.outcomes[fill.market]
            price = pips_to_dollars(fill.price_pips)
            settlement = pips_to_dollars(outcome.settlement_pips)
            fills.append(
                {
                    "game_id": game_id,
                    "order_id": fill.order_id,
                    "market": fill.market,
                    "ts_us": fill.ts_us,
                    "qty": fill.qty,
                    "price": price,
                    "settlement": settlement,
                    "won": (1 if outcome.result == "yes" else 0) if outcome.binary else None,
                    "gross_pnl": fill.qty * (settlement - price),
                    "fill_kind": kinds.get((game_id, fill.order_id, fill.ts_us), "unknown"),
                }
            )
        for skip in result.skips:
            skips.append(
                {"game_id": game_id, "market": skip.market, "ts_us": skip.ts_us, "reason": skip.reason}
            )

    ends = {r.info.game_id: r.end_us for r in results}
    game_rows = [
        {
            "game_id": g["game_id"],
            "espn_id": g["espn_id"],
            "month": ticker_date(g["game_id"]).strftime("%Y-%m"),
            "scheduled_us": g["scheduled_us"],
            "end_us": ends[g["game_id"]],
        }
        for g in games
    ]
    return (
        pl.DataFrame(orders, schema_overrides=ORDER_SCHEMA, infer_schema_length=None)
        if orders
        else pl.DataFrame(schema=ORDER_SCHEMA),
        pl.DataFrame(fills, schema=FILL_SCHEMA),
        pl.DataFrame(game_rows, schema=GAME_SCHEMA),
        pl.DataFrame(skips, schema=SKIP_SCHEMA),
    )


def _dollars(pips: int | None) -> float | None:
    return None if pips is None else pips_to_dollars(pips)


def run_backtest(
    strategy: Strategy,
    data_root: Path,
    config: BacktestConfig | None = None,
    splits: Splits | None = None,
) -> BacktestRun:
    config = config or BacktestConfig()
    if config.fill_model not in FILL_MODELS:
        raise ValueError(f"unknown fill model {config.fill_model!r}; use {sorted(FILL_MODELS)}")
    splits = splits or Splits.load()
    if config.period == "holdout":
        raise PermissionError("The holdout is run once, after the strategy is frozen.")
    games = GameStore(data_root, config.season, splits).games(config.period)

    arguments = (games, repeat(data_root), repeat(strategy), repeat(config), repeat(splits))
    if config.workers > 1:
        with ProcessPoolExecutor(max_workers=config.workers) as pool:
            outcomes = list(pool.map(_replay_one, *arguments, chunksize=8))
    else:
        outcomes = [_replay_one(*args) for args in zip(*arguments, strict=False)]

    results = [result for result, _ in outcomes]
    kinds = {
        (result.info.game_id, order_id, ts): kind
        for result, tagged in outcomes
        for (order_id, ts), kind in tagged.items()
    }
    results, peak = apply_portfolio_limit(results, config.max_portfolio_reserved)
    orders, fills, game_table, skips = _to_frames(results, kinds, games)
    return BacktestRun(orders, fills, game_table, skips, peak)
