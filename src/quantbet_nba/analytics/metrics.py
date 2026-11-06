"""Summarize a backtest. Uncertainty comes from resampling whole games, never rows.

Rungs in one game share its result and often fill together, so thousands of fills from a few
hundred games are not thousands of independent observations.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import polars as pl

from quantbet_nba.engine.backtest import BacktestRun
from quantbet_nba.engine.fees import FEE_SCENARIOS, QuadraticFee

N_BOOTSTRAP = 2000
BOOTSTRAP_SEED = 20261106
DECIDED = ("filled", "partial", "unfilled", "cancelled")
EVIDENCE_FLOOR_FILLS = 200
MIN_GAMES_FOR_INTERVAL = 20  # below this, a resampled interval looks far more precise than it is
PRICE_BANDS = [0.25, 0.50, 0.75]


def _interval(samples: np.ndarray) -> list[float]:
    low, high = np.nanpercentile(samples, [2.5, 97.5])
    return [round(float(low), 5), round(float(high), 5)]


def bootstrap_game_intervals(
    pnl: np.ndarray, filled: np.ndarray, submitted: np.ndarray, seed: int = BOOTSTRAP_SEED
) -> dict[str, list[float] | None]:
    """95% percentile intervals for profit per filled and per submitted contract."""
    if pnl.size == 0:
        return {"per_filled": None, "per_submitted": None}
    picks = np.random.default_rng(seed).integers(0, pnl.size, size=(N_BOOTSTRAP, pnl.size))
    total_pnl = pnl[picks].sum(axis=1)
    with np.errstate(divide="ignore", invalid="ignore"):
        per_filled = total_pnl / filled[picks].sum(axis=1)
        per_submitted = total_pnl / submitted[picks].sum(axis=1)
    return {"per_filled": _interval(per_filled), "per_submitted": _interval(per_submitted)}


def block_stats(orders: pl.DataFrame, fills: pl.DataFrame) -> dict[str, Any]:
    """Statistics for one slice of orders and the fills that belong to them."""
    decided = orders.filter(pl.col("status").is_in(DECIDED))
    submitted = float(decided["size"].sum()) if decided.height else 0.0
    filled_in_decided = float(decided["filled"].sum()) if decided.height else 0.0
    qty = float(fills["qty"].sum()) if fills.height else 0.0
    gross = float(fills["gross_pnl"].sum()) if fills.height else 0.0
    binary = fills.filter(pl.col("won").is_not_null())
    wins = float((binary["won"].cast(pl.Float64) * binary["qty"]).sum()) if binary.height else 0.0
    binary_qty = float(binary["qty"].sum()) if binary.height else 0.0
    average_price = float((fills["price"] * fills["qty"]).sum() / qty) if qty else None

    per_game = _per_game(orders, fills)
    if per_game.height >= MIN_GAMES_FOR_INTERVAL:
        intervals = bootstrap_game_intervals(
            per_game["pnl"].to_numpy(),
            per_game["filled"].to_numpy(),
            per_game["submitted"].to_numpy(),
        )
    else:
        intervals = {"per_filled": None, "per_submitted": None}
    win_rate = wins / binary_qty if binary_qty else None
    return {
        "games_with_orders": per_game.height,
        "games_with_fills": fills["game_id"].n_unique() if fills.height else 0,
        "orders_decided": decided.height,
        "orders_filled": fills.select("game_id", "order_id").unique().height if fills.height else 0,
        "contracts_submitted": submitted,
        "contracts_filled": qty,
        "fill_rate": round(filled_in_decided / submitted, 4) if submitted else None,
        "win_rate_when_filled": round(win_rate, 4) if win_rate is not None else None,
        "average_fill_price": round(average_price, 4) if average_price is not None else None,
        "win_rate_minus_price": (
            round(win_rate - average_price, 4)
            if win_rate is not None and average_price is not None
            else None
        ),
        "gross_pnl": round(gross, 2),
        "gross_per_filled": round(gross / qty, 5) if qty else None,
        "gross_per_submitted": round(gross / submitted, 5) if submitted else None,
        "ci95_gross_per_filled": intervals["per_filled"],
        "ci95_gross_per_submitted": intervals["per_submitted"],
    }


def _per_game(orders: pl.DataFrame, fills: pl.DataFrame) -> pl.DataFrame:
    live = orders.filter(pl.col("status") != "rejected")
    if not live.height:
        return pl.DataFrame(schema={"game_id": pl.String, "submitted": pl.Float64, "pnl": pl.Float64, "filled": pl.Float64})
    submitted = (
        live.filter(pl.col("status").is_in(DECIDED))
        .group_by("game_id")
        .agg(pl.col("size").sum().alias("submitted"))
    )
    results = fills.group_by("game_id").agg(
        pl.col("gross_pnl").sum().alias("pnl"), pl.col("qty").sum().alias("filled")
    )
    return (
        live.select("game_id")
        .unique()
        .join(submitted, on="game_id", how="left")
        .join(results, on="game_id", how="left")
        .fill_null(0.0)
        .sort("game_id")
    )


def fee_sensitivity(fills: pl.DataFrame) -> dict[str, dict[str, float | None]]:
    """What each unverified fee scenario would do to the gross result."""
    table = {}
    qty = float(fills["qty"].sum()) if fills.height else 0.0
    gross = float(fills["gross_pnl"].sum()) if fills.height else 0.0
    for name, rate in FEE_SCENARIOS.items():
        model = QuadraticFee(rate)
        fees = sum(
            model.fee(price, count)
            for price, count in fills.select("price", "qty").iter_rows()
        )
        table[name] = {
            "fee_rate": rate,
            "total_fees": round(fees, 2),
            "pnl_after_fees": round(gross - fees, 2),
            "per_filled_after_fees": round((gross - fees) / qty, 5) if qty else None,
        }
    return table


def max_drawdown(fills: pl.DataFrame, games: pl.DataFrame) -> float:
    """Largest fall in cumulative profit, settling each game at its final play."""
    if not fills.height:
        return 0.0
    per_game = fills.group_by("game_id").agg(pl.col("gross_pnl").sum())
    ordered = games.join(per_game, on="game_id").sort("end_us", "game_id")
    curve = np.cumsum(ordered["gross_pnl"].to_numpy())
    peak = np.maximum.accumulate(np.concatenate([[0.0], curve]))[1:]
    return round(float((peak - curve).max()), 2)


def fill_phases(fills: pl.DataFrame, games: pl.DataFrame) -> dict[str, dict[str, Any]]:
    """Split fills at scheduled tipoff, which sizes the unconfirmed order-survival assumption.

    If resting orders were cancelled at tipoff, every in-game fill here could not have happened.
    Removing those fills also changes which games lose money, so this is only a first indication.
    """
    if not fills.height:
        return {}
    timed = fills.join(games.select("game_id", "scheduled_us"), on="game_id", how="left")
    table = {}
    for phase, condition in (
        ("before_scheduled_tipoff", pl.col("ts_us") < pl.col("scheduled_us")),
        ("after_scheduled_tipoff", pl.col("ts_us") >= pl.col("scheduled_us")),
    ):
        subset = timed.filter(condition)
        qty = float(subset["qty"].sum()) if subset.height else 0.0
        gross = float(subset["gross_pnl"].sum()) if subset.height else 0.0
        table[phase] = {
            "contracts_filled": qty,
            "games": subset["game_id"].n_unique() if subset.height else 0,
            "gross_pnl": round(gross, 2),
            "gross_per_filled": round(gross / qty, 5) if qty else None,
        }
    return table


def _with_groups(orders: pl.DataFrame) -> pl.DataFrame:
    """Add reporting groups. Liquidity uses only volume visible before the order was placed."""
    per_game_volume = (
        orders.select("game_id", "market", "pre_volume_1h")
        .unique(["game_id", "market"])
        .group_by("game_id")
        .agg(pl.col("pre_volume_1h").sum().alias("game_pre_volume"))
    )
    if per_game_volume.height >= 4:
        cuts = np.quantile(per_game_volume["game_pre_volume"].to_numpy(), [0.25, 0.5, 0.75])
        per_game_volume = per_game_volume.with_columns(
            pl.col("game_pre_volume")
            .map_elements(
                lambda value: f"Q{int(np.searchsorted(cuts, value, side='left')) + 1}",
                return_dtype=pl.String,
            )
            .alias("liquidity_quartile")
        )
    else:
        per_game_volume = per_game_volume.with_columns(pl.lit("all").alias("liquidity_quartile"))

    def band(value: float) -> str:
        edges = [0.0, *PRICE_BANDS, 1.0]
        index = int(np.searchsorted(PRICE_BANDS, value, side="left"))
        return f"{edges[index]:.2f}-{edges[index + 1]:.2f}"

    return orders.join(
        per_game_volume.select("game_id", "liquidity_quartile"), on="game_id", how="left"
    ).with_columns(pl.col("limit").map_elements(band, return_dtype=pl.String).alias("price_band"))


def summarize(run: BacktestRun, fixture_espn_ids: set[str] | None = None) -> dict[str, Any]:
    orders, fills, games = run.orders, run.fills, run.games
    status_counts = dict(orders.group_by("status").len().sort("status").iter_rows()) if orders.height else {}
    rejected = orders.filter(pl.col("status") == "rejected")
    reasons = dict(rejected.group_by("reason").len().sort("reason").iter_rows()) if rejected.height else {}
    skips = dict(run.skips.group_by("reason").len().sort("reason").iter_rows()) if run.skips.height else {}
    whole = block_stats(orders, fills)
    summary = {
        "label": "GROSS of fees: limit orders are treated as free for testing (decision 0004)",
        "games_in_period": games.height,
        "order_status": status_counts,
        "rejection_reasons": reasons,
        "skip_reasons": skips,
        **whole,
        "reporting_floor_met": whole["orders_filled"] >= EVIDENCE_FLOOR_FILLS,
        "max_drawdown_gross": max_drawdown(fills, games),
        "peak_reserved_dollars": round(run.peak_reserved, 2),
        "fee_sensitivity": fee_sensitivity(fills),
        "breakdowns": {},
    }
    if not orders.height:
        return summary

    grouped = _with_groups(orders)
    keys = ["month", "liquidity_quartile", "price_band"]
    keys += sorted(c for c in orders.columns if c.startswith("label_"))
    joined_fills = fills.join(
        grouped.select("game_id", "order_id", *keys), on=["game_id", "order_id"], how="left"
    )
    for key in keys:
        rows = {}
        for value in sorted(v for v in grouped[key].unique().to_list() if v is not None):
            rows[str(value)] = block_stats(
                grouped.filter(pl.col(key) == value), joined_fills.filter(pl.col(key) == value)
            )
        summary["breakdowns"][key] = rows

    kinds = {}
    for kind in ("sweep", "drift", "unknown"):
        subset = fills.filter(pl.col("fill_kind") == kind)
        qty = float(subset["qty"].sum()) if subset.height else 0.0
        gross = float(subset["gross_pnl"].sum()) if subset.height else 0.0
        kinds[kind] = {
            "contracts_filled": qty,
            "games": subset["game_id"].n_unique() if subset.height else 0,
            "gross_pnl": round(gross, 2),
            "gross_per_filled": round(gross / qty, 5) if qty else None,
        }
    summary["fill_kinds"] = kinds
    summary["fill_phases"] = fill_phases(fills, games)

    if fixture_espn_ids:
        fixture_games = games.filter(pl.col("espn_id").is_in(list(fixture_espn_ids)))["game_id"]
        keep = ~pl.col("game_id").is_in(fixture_games.to_list())
        summary["excluding_stage0_fixtures"] = {
            "fixture_games_removed": fixture_games.len(),
            **block_stats(orders.filter(keep), fills.filter(keep)),
        }
    return summary
