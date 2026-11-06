"""Read and write the normalized dataset, and turn a game's tables into a replay bundle.

Layout under the data root, for one season:
  normalized/<season>/games.parquet        one row per usable game
  normalized/<season>/exclusions.parquet   every game left out, with a reason
  normalized/<season>/candles/<event>.parquet
  normalized/<season>/trades/<event>.parquet
  normalized/<season>/manifest.json        file hashes; identifies the dataset in each run

All reads check the split rules, so the holdout can't be loaded by accident.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import polars as pl

from quantbet_nba.config import Splits
from quantbet_nba.engine.types import (
    GameBundle,
    GameInfo,
    MarketInfo,
    MarketOutcome,
    Quote,
    Trade,
)

GAME_SCHEMA = {
    "game_id": pl.String,
    "espn_id": pl.String,
    "season": pl.String,
    "scheduled_us": pl.Int64,
    "first_play_us": pl.Int64,
    "final_play_us": pl.Int64,
    "replay_start_us": pl.Int64,
    "home": pl.String,
    "away": pl.String,
    "home_market": pl.String,
    "away_market": pl.String,
    "home_result": pl.String,
    "away_result": pl.String,
    "home_settlement_pips": pl.Int32,
    "away_settlement_pips": pl.Int32,
    "home_trades_from_us": pl.Int64,
    "home_trades_to_us": pl.Int64,
    "away_trades_from_us": pl.Int64,
    "away_trades_to_us": pl.Int64,
    "home_final": pl.Int32,
    "away_final": pl.Int32,
    "neutral_site": pl.Boolean,
    "exact_ticker_date": pl.Boolean,
    "wallclock_reversals": pl.Int32,
    "final_play_lag_us": pl.Int64,
    "first_play_shift_us": pl.Int64,
    "final_play_shift_us": pl.Int64,
}


def build_bundle(game: dict[str, Any], candles: pl.DataFrame, trades: pl.DataFrame) -> GameBundle:
    """Assemble one replay input. Nothing after the final play is included."""
    end = game["final_play_us"]
    home, away = game["home_market"], game["away_market"]
    info = GameInfo(
        game_id=game["game_id"],
        scheduled_start_us=game["scheduled_us"],
        home=game["home"],
        away=game["away"],
        markets=(MarketInfo(home, game["home"], True), MarketInfo(away, game["away"], False)),
    )

    # A candle with neither quote side tells us nothing, so the last real quote stays current.
    # Volume still has to count, so a side-less candle is only dropped when it also has no volume.
    quotes = [
        Quote(end_us, market, bid, ask, volume)
        for market, end_us, bid, ask, volume in candles.filter(
            pl.col("end_us") <= end
        )
        .sort("end_us", "market")
        .select("market", "end_us", "bid_pips", "ask_pips", "volume")
        .iter_rows()
        if bid is not None or ask is not None or volume
    ]
    trade_rows = [
        Trade(ts_us, market, price, qty, trade_id)
        for market, ts_us, price, qty, trade_id in trades.filter(pl.col("ts_us") <= end)
        .sort("ts_us", "trade_id")
        .select("market", "ts_us", "price_pips", "qty", "trade_id")
        .iter_rows()
    ]

    def window(side: str) -> tuple[int, int] | None:
        start, stop = game[f"{side}_trades_from_us"], game[f"{side}_trades_to_us"]
        return None if start is None or stop is None else (start, stop)

    return GameBundle(
        info=info,
        start_us=game["replay_start_us"],
        end_us=end,
        quotes=quotes,
        trades=trade_rows,
        outcomes={
            home: MarketOutcome(home, game["home_result"], game["home_settlement_pips"]),
            away: MarketOutcome(away, game["away_result"], game["away_settlement_pips"]),
        },
        trade_coverage={home: window("home"), away: window("away")},
    )


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_manifest(season_dir: Path) -> dict[str, Any]:
    """Hash every file so each run can name exactly which data it used."""
    files = {
        str(path.relative_to(season_dir)): sha256_file(path)
        for path in sorted(season_dir.rglob("*.parquet"))
    }
    combined = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    manifest = {"dataset_sha256": combined, "n_files": len(files), "files": files}
    (season_dir / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


class GameStore:
    def __init__(self, data_root: Path, season: str, splits: Splits | None = None) -> None:
        self.season_dir = data_root / "normalized" / season
        self.splits = splits or Splits.load()

    def _games_file(self) -> Path:
        path = self.season_dir / "games.parquet"
        if not path.exists():
            raise FileNotFoundError(f"no dataset at {path}. Run `scripts/quantbet.py pull` first.")
        return path

    def games(self, period: str) -> list[dict[str, Any]]:
        """Usable games in a period, by scheduled start then ID. Holdout rows raise."""
        table = pl.read_parquet(self._games_file()).sort("scheduled_us", "game_id")
        selected = []
        for row in table.iter_rows(named=True):
            if self.splits.period_of(row["game_id"]) == period:
                selected.append(row)
        return selected

    def load(self, game: dict[str, Any]) -> GameBundle:
        self.splits.period_of(game["game_id"])  # fails closed on the holdout
        return build_bundle(game, self.candles(game["game_id"]), self.trades(game["game_id"]))

    def candles(self, game_id: str) -> pl.DataFrame:
        self.splits.period_of(game_id)
        return pl.read_parquet(self.season_dir / "candles" / f"{game_id}.parquet")

    def trades(self, game_id: str) -> pl.DataFrame:
        self.splits.period_of(game_id)
        return pl.read_parquet(self.season_dir / "trades" / f"{game_id}.parquet")

    def exclusions(self) -> pl.DataFrame:
        return pl.read_parquet(self.season_dir / "exclusions.parquet")

    def dataset_hash(self) -> str:
        manifest = json.loads((self.season_dir / "manifest.json").read_text())
        return manifest["dataset_sha256"]
