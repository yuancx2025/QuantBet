"""Pull one season's raw responses, then normalize them into the replay dataset.

The universe comes from ESPN's fixture list, chosen by date and season type and never by outcome.
Each pull writes raw files atomically, so rerunning resumes where it stopped. Games that can't be
used are recorded in exclusions.parquet with a reason, never silently dropped.
"""

from __future__ import annotations

import datetime as dt
import shutil
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import polars as pl

from quantbet_nba.config import HoldoutAccessError, Splits, season_of, ticker_date
from quantbet_nba.data import espn, kalshi
from quantbet_nba.data.http import Fetcher, read_raw, write_raw
from quantbet_nba.data.store import GAME_SCHEMA, write_manifest
from quantbet_nba.engine.types import US_PER_SECOND

# Date ranges to scan. ESPN's season type then decides what counts as regular season.
SEASON_DATES = {"2025-26": (dt.date(2025, 10, 1), dt.date(2026, 4, 30))}

HOUR_US = 3600 * US_PER_SECOND
CANDLES_BEFORE_START = 4 * HOUR_US  # one hour of pre-plan volume for a plan at T-2h, plus room
TRADES_BEFORE_START = 3 * HOUR_US  # before the T-2h planning time, unlike the Stage 0 spike
AFTER_FINAL_PLAY = 15 * 60 * US_PER_SECOND


@dataclass
class PullPaths:
    root: Path

    def scoreboard(self, day: dt.date) -> Path:
        return self.root / "raw/espn/scoreboard" / f"{day:%Y%m%d}.json.gz"

    def summary(self, espn_id: str) -> Path:
        return self.root / "raw/espn/summary" / f"{espn_id}.json.gz"

    @property
    def archive(self) -> Path:
        return self.root / "raw/kalshi/archive_markets.json.gz"

    def candles(self, ticker: str) -> Path:
        return self.root / "raw/kalshi/candles" / f"{ticker}.json.gz"

    def trades(self, ticker: str) -> Path:
        return self.root / "raw/kalshi/trades" / f"{ticker}.json.gz"


class Puller:
    def __init__(self, root: Path, kalshi_per_second: float = 8, espn_per_second: float = 3) -> None:
        self.paths = PullPaths(root)
        self.kalshi = Fetcher(kalshi_per_second)
        self.espn = Fetcher(espn_per_second)

    def _cached(self, path: Path, fetch) -> Any:
        if path.exists():
            return read_raw(path)["data"]
        request, data = fetch()
        write_raw(path, request, data)
        return data

    def scoreboard(self, day: dt.date) -> dict[str, Any]:
        def fetch():
            params = {"dates": f"{day:%Y%m%d}"}
            return {"path": "scoreboard", "params": params}, self.espn.get(
                f"{espn.BASE}/scoreboard", params
            )

        return self._cached(self.paths.scoreboard(day), fetch)

    def summary(self, espn_id: str) -> dict[str, Any]:
        def fetch():
            params = {"event": espn_id}
            return {"path": "summary", "params": params}, self.espn.get(
                f"{espn.BASE}/summary", params
            )

        return self._cached(self.paths.summary(espn_id), fetch)

    def archive(self) -> list[dict[str, Any]]:
        def fetch():
            markets: list[dict[str, Any]] = []
            cursor = ""
            while True:
                params: dict[str, Any] = {"series_ticker": kalshi.SERIES, "limit": 1000}
                if cursor:
                    params["cursor"] = cursor
                page = self.kalshi.get(f"{kalshi.BASE}/historical/markets", params)
                markets.extend(page.get("markets", []))
                cursor = page.get("cursor") or ""
                if not cursor or not page.get("markets"):
                    return {"path": "historical/markets", "series": kalshi.SERIES}, markets

        return self._cached(self.paths.archive, fetch)

    def candles(self, ticker: str, start_us: int, end_us: int) -> None:
        path = self.paths.candles(ticker)
        if path.exists():
            return
        params = {
            "start_ts": start_us // US_PER_SECOND,
            "end_ts": end_us // US_PER_SECOND,
            "period_interval": 1,
        }
        url = f"{kalshi.BASE}/historical/markets/{ticker}/candlesticks"
        data = self.kalshi.get(url, params)
        write_raw(path, {"path": "candlesticks", "ticker": ticker, "params": params}, data)

    def trades(self, ticker: str, start_us: int, end_us: int) -> None:
        path = self.paths.trades(ticker)
        if path.exists():
            return
        window = {"min_ts": start_us // US_PER_SECOND, "max_ts": end_us // US_PER_SECOND}
        trades: list[dict[str, Any]] = []
        cursor = ""
        while True:
            params: dict[str, Any] = {"ticker": ticker, "limit": 1000, **window}
            if cursor:
                params["cursor"] = cursor
            page = self.kalshi.get(f"{kalshi.BASE}/historical/trades", params)
            trades.extend(page.get("trades", []))
            cursor = page.get("cursor") or ""
            if not cursor or not page.get("trades"):
                break
        write_raw(path, {"path": "trades", "ticker": ticker, **window}, trades)


def _universe(puller: Puller, season: str) -> list[dict[str, Any]]:
    first, last = SEASON_DATES[season]
    games: dict[str, dict[str, Any]] = {}
    day = first
    while day <= last:
        for game in espn.parse_scoreboard(puller.scoreboard(day)):
            if game["season_type"] == espn.REGULAR_SEASON:
                games.setdefault(game["espn_id"], game)
        day += dt.timedelta(days=1)
    return sorted(games.values(), key=lambda g: (g["scheduled_us"], g["espn_id"]))


def _resolve_game(
    puller: Puller,
    game: dict[str, Any],
    events: dict[str, list[dict[str, Any]]],
    splits: Splits,
    season: str,
) -> tuple[dict[str, Any] | None, str]:
    """Map a fixture to its markets and check it. Returns (plan, exclusion reason)."""
    if not game["completed"] or game["home_final"] is None or game["away_final"] is None:
        return None, "not_completed"
    event, exact = kalshi.find_event(game["home"], game["away"], game["scheduled_us"], events)
    if event is None:
        return None, "no_kalshi_event"
    if season_of(ticker_date(event)) != season:
        return None, "ticker_outside_season"
    splits.period_of(event)  # raises on the holdout before any market data is fetched

    markets = {m["ticker"].rsplit("-", 1)[1]: m for m in events[event]}
    if game["home"] not in markets or game["away"] not in markets:
        return None, "missing_market"
    home, away = markets[game["home"]], markets[game["away"]]
    home_result, home_settlement = kalshi.market_outcome(home)
    away_result, away_settlement = kalshi.market_outcome(away)
    if home_result in ("yes", "no"):
        home_won = game["home_final"] > game["away_final"]
        if (home_result == "yes") != home_won or home_result == away_result:
            return None, "settlement_mismatch"

    plays = espn.parse_plays(puller.summary(game["espn_id"]))
    if plays is None:
        return None, "no_play_by_play"
    scheduled = game["scheduled_us"]
    return {
        **game,
        **plays,
        "game_id": event,
        "exact_ticker_date": exact,
        "home_market": home["ticker"],
        "away_market": away["ticker"],
        "home_result": home_result,
        "away_result": away_result,
        "home_settlement_pips": home_settlement,
        "away_settlement_pips": away_settlement,
        "replay_start_us": scheduled - CANDLES_BEFORE_START,
        "trade_start_us": scheduled - TRADES_BEFORE_START,
        "pull_end_us": plays["final_play_us"] + AFTER_FINAL_PLAY,
    }, ""


def _fetch_market_data(puller: Puller, plan: dict[str, Any]) -> None:
    for side in ("home", "away"):
        ticker = plan[f"{side}_market"]
        puller.candles(ticker, plan["replay_start_us"], plan["pull_end_us"])
        puller.trades(ticker, plan["trade_start_us"], plan["pull_end_us"])


def _normalize_game(root: Path, plan: dict[str, Any], season_dir: Path) -> dict[str, Any] | str:
    """Write one game's tables and return its row, or an exclusion reason."""
    paths = PullPaths(root)
    candle_frames, trade_frames = [], []
    coverage: dict[str, tuple[int, int] | None] = {}
    for side in ("home", "away"):
        ticker = plan[f"{side}_market"]
        candle_path = paths.candles(ticker)
        if not candle_path.exists():
            return "missing_candles"
        raw = read_raw(candle_path)["data"].get("candlesticks", [])
        candle_frames.append(kalshi.normalize_candles(ticker, raw))
        trade_path = paths.trades(ticker)
        if trade_path.exists():
            stored = read_raw(trade_path)
            trade_frames.append(kalshi.normalize_trades(ticker, stored["data"]))
            coverage[side] = (
                stored["request"]["min_ts"] * US_PER_SECOND,
                stored["request"]["max_ts"] * US_PER_SECOND,
            )
        else:
            coverage[side] = None
    candles = pl.concat(candle_frames)
    if candles.is_empty():
        return "empty_candles"

    (season_dir / "candles").mkdir(parents=True, exist_ok=True)
    (season_dir / "trades").mkdir(parents=True, exist_ok=True)
    event = plan["game_id"]
    candles.write_parquet(season_dir / "candles" / f"{event}.parquet")
    trades = pl.concat(trade_frames) if trade_frames else pl.DataFrame(schema=kalshi.TRADE_SCHEMA)
    trades.write_parquet(season_dir / "trades" / f"{event}.parquet")

    # Trading stops at settlement, so the last trade checks ESPN's clock for the game's end. The
    # trade window was pulled using the uncorrected time, which only makes it longer than needed.
    final_shift = 0 if trades.is_empty() else espn.clock_shift_us(
        plan["final_play_us"], trades["ts_us"].max()
    )
    # First plays follow the schedule by 10-20 minutes, so whole hours past it are clock error. Round
    # down, so that a real 40-minute delay isn't mistaken for a one-hour error.
    first_shift = max(plan["first_play_us"] - plan["scheduled_us"], 0) // espn.HOUR_US * espn.HOUR_US
    row = {name: plan.get(name) for name in GAME_SCHEMA}
    row.update(
        season=season_of(ticker_date(event)),
        scheduled_us=plan["scheduled_us"],
        first_play_us=plan["first_play_us"] - first_shift,
        final_play_us=plan["final_play_us"] - final_shift,
        first_play_shift_us=first_shift,
        final_play_shift_us=final_shift,
        final_play_lag_us=plan["latest_wallclock_us"] - plan["final_play_us"],
    )
    for side in ("home", "away"):
        window = coverage[side]
        row[f"{side}_trades_from_us"] = window[0] if window else None
        row[f"{side}_trades_to_us"] = window[1] if window else None
    return row


def pull_season(
    root: Path,
    season: str,
    limit: int | None = None,
    workers: int = 6,
    splits: Splits | None = None,
    log=print,
) -> dict[str, Any]:
    if season not in SEASON_DATES:
        raise ValueError(f"v1 pulls only {sorted(SEASON_DATES)}; 2026-27 tuning games use live data")
    splits = splits or Splits.load()
    puller = Puller(root)
    log(f"scanning fixtures for {season}")
    fixtures = _universe(puller, season)
    events = kalshi.group_events(puller.archive())
    log(f"{len(fixtures)} regular-season fixtures; archive has {len(events)} events")
    if limit:
        fixtures = fixtures[:: max(1, len(fixtures) // limit)][:limit]

    exclusions: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    plans: list[dict[str, Any]] = []

    def resolve_and_fetch(game: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any] | None, str]:
        plan, reason = _resolve_game(puller, game, events, splits, season)
        if plan is not None:
            _fetch_market_data(puller, plan)
        return game, plan, reason

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(resolve_and_fetch, game): game for game in fixtures}
        for done, future in enumerate(as_completed(futures), start=1):
            game = futures[future]
            try:
                _, plan, reason = future.result()
            except HoldoutAccessError:
                raise
            except Exception as error:  # noqa: BLE001 - one bad game is recorded, not fatal
                errors.append({"espn_id": game["espn_id"], "error": repr(error)[:300]})
                plan, reason = None, "fetch_error"
            if plan is not None:
                plans.append(plan)
            else:
                exclusions.append({"espn_id": game["espn_id"], "reason": reason})
            if done % 25 == 0 or done == len(fixtures):
                log(
                    f"{done}/{len(fixtures)} games; {puller.kalshi.calls} Kalshi and "
                    f"{puller.espn.calls} ESPN calls; {len(errors)} errors"
                )

    return normalize_season(root, season, plans, exclusions, errors, log)


def normalize_season(
    root: Path,
    season: str,
    plans: list[dict[str, Any]],
    exclusions: list[dict[str, str]],
    errors: list[dict[str, str]],
    log=print,
) -> dict[str, Any]:
    season_dir = root / "normalized" / season
    temporary = season_dir.with_name(f"{season}.building")
    if temporary.exists():
        shutil.rmtree(temporary)
    temporary.mkdir(parents=True)

    rows = []
    for plan in sorted(plans, key=lambda p: (p["scheduled_us"], p["game_id"])):
        outcome = _normalize_game(root, plan, temporary)
        if isinstance(outcome, str):
            exclusions.append({"espn_id": plan["espn_id"], "reason": outcome})
        else:
            rows.append(outcome)
    pl.DataFrame(rows, schema=GAME_SCHEMA).write_parquet(temporary / "games.parquet")
    pl.DataFrame(
        sorted(exclusions, key=lambda e: e["espn_id"]),
        schema={"espn_id": pl.String, "reason": pl.String},
    ).write_parquet(temporary / "exclusions.parquet")
    manifest = write_manifest(temporary)
    if errors:
        pl.DataFrame(errors).write_csv(temporary / "pull_errors.csv")

    # Swap in the finished dataset only after everything above has succeeded.
    if season_dir.exists():
        shutil.rmtree(season_dir)
    temporary.rename(season_dir)
    log(f"dataset {manifest['dataset_sha256'][:12]}: {len(rows)} games, {len(exclusions)} excluded")
    return {"games": len(rows), "excluded": len(exclusions), "errors": len(errors)}
