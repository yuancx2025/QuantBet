"""Stage 0 spike: chart Kalshi price against the score for 10 games from 2025-26.

Exploratory only. The real adapters arrive in Stage 1. Every response is cached under
data/raw/spike/, so reruns make no network calls.

Commands: select | fetch | analyze | chart | all
"""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import statistics
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import numpy as np
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw" / "spike"
OUT = ROOT / "reports" / "stage0"
SELECTED = ROOT / "data" / "spike_selected_games.json"

ESPN = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
KALSHI = "https://api.elections.kalshi.com/trade-api/v2"

# Evenly spaced dates across the 2025-26 regular season. Tuning data only: nothing here
# may touch 2026-27, which is the holdout.
SCAN_DATES = [
    "20251024", "20251107", "20251121", "20251205", "20251219", "20260102",
    "20260115", "20260130", "20260213", "20260227", "20260313", "20260327",
    "20260410",
]  # fmt: skip

# ESPN abbreviation -> Kalshi ticker abbreviation, where they differ.
ESPN_TO_KALSHI = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS"}

PRE_TIP = dt.timedelta(hours=3)
POST_END = dt.timedelta(minutes=15)
LAGS_MIN = range(-5, 6)
BIN_S = 5
EVENT_WINDOW_S = 120

client = httpx.Client(headers={"User-Agent": "quantbet-research"}, timeout=30)


@retry(
    retry=retry_if_exception_type((httpx.HTTPError,)),
    stop=stop_after_attempt(5),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    reraise=True,
)
def _download(url: str, params: dict[str, Any]) -> Any:
    resp = client.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


def get(url: str, pause: float = 0.1, **params: Any) -> Any:
    """GET with a raw on-disk cache. Raw responses are stored unmodified."""
    key = hashlib.sha256(f"{url}|{sorted(params.items())}".encode()).hexdigest()[:24]
    path = RAW / f"{key}.json"
    if path.exists():
        return json.loads(path.read_text())
    data = _download(url, params)
    RAW.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    time.sleep(pause)
    return data


def parse_ts(value: str) -> dt.datetime:
    return dt.datetime.fromisoformat(value)


def epoch(value: dt.datetime) -> int:
    return int(value.timestamp())


def dollars(block: dict[str, Any], name: str) -> float | None:
    """Read a price from a candle block. Historical uses `close`, live uses `close_dollars`."""
    raw = block.get(f"{name}_dollars", block.get(name))
    return None if raw in (None, "") else float(raw)


# --- ESPN ---------------------------------------------------------------------------


def espn_games(date: str) -> list[dict[str, Any]]:
    return get(f"{ESPN}/scoreboard", dates=date).get("events", [])


def espn_summary(event_id: str) -> dict[str, Any]:
    return get(f"{ESPN}/summary", event=event_id)


def moneylines(summary: dict[str, Any]) -> tuple[float | None, float | None]:
    picks = summary.get("pickcenter") or []
    if not picks:
        return None, None
    return (
        (picks[0].get("homeTeamOdds") or {}).get("moneyLine"),
        (picks[0].get("awayTeamOdds") or {}).get("moneyLine"),
    )


def game_stats(event: dict[str, Any]) -> dict[str, Any] | None:
    """Summarize one finished regular-season game, or return None if it doesn't qualify."""
    comp = event["competitions"][0]
    if event["season"]["type"] != 2 or not comp["status"]["type"]["completed"]:
        return None
    teams = {c["homeAway"]: c for c in comp["competitors"]}
    home_final, away_final = int(teams["home"]["score"]), int(teams["away"]["score"])
    summary = espn_summary(event["id"])
    plays = summary.get("plays") or []
    if not plays:
        return None

    home_won = home_final > away_final
    # Winner's largest deficit at any play, in points.
    worst = 0
    for play in plays:
        lead = play["homeScore"] - play["awayScore"]
        worst = min(worst, lead if home_won else -lead)

    ml_home, ml_away = moneylines(summary)
    winner_ml = ml_home if home_won else ml_away
    first_wall = next((p["wallclock"] for p in plays if p.get("wallclock")), None)
    return {
        "espn_id": event["id"],
        "date": event["date"],
        "home": teams["home"]["team"]["abbreviation"],
        "away": teams["away"]["team"]["abbreviation"],
        "home_final": home_final,
        "away_final": away_final,
        "margin": abs(home_final - away_final),
        "winner_max_deficit": -worst,
        "periods": max(p["period"]["number"] for p in plays),
        "winner_moneyline": winner_ml,
        "first_wallclock": first_wall,
        "neutral_site": comp.get("neutralSite", False),
    }


# --- Selection ----------------------------------------------------------------------


def select_games() -> list[dict[str, Any]]:
    """Choose 10 games by fixed rules, so the sample isn't picked by looking at prices."""
    pool = []
    for date in SCAN_DATES:
        for event in espn_games(date):
            stats = game_stats(event)
            if stats:
                pool.append(stats)
    print(f"scanned {len(pool)} finished regular-season games on {len(SCAN_DATES)} dates")

    chosen: dict[str, dict[str, Any]] = {}

    def take(games: list[dict[str, Any]], role: str, count: int = 1) -> None:
        """Take the first eligible games, with at most two per calendar month for spread."""
        for game in games:
            if count == 0:
                return
            in_month = sum(c["date"][:7] == game["date"][:7] for c in chosen.values())
            if game["espn_id"] not in chosen and in_month < 2:
                chosen[game["espn_id"]] = {**game, "role": role}
                count -= 1

    def tip_hour_eastern(game: dict[str, Any]) -> float:
        tip = parse_ts(game["date"]).astimezone(ZoneInfo("America/New_York"))
        return tip.hour + tip.minute / 60

    take(sorted(pool, key=lambda g: -g["winner_max_deficit"]), "comeback", 2)
    take(sorted(pool, key=lambda g: -g["margin"]), "blowout", 2)
    take(sorted((g for g in pool if g["periods"] > 4), key=lambda g: -g["periods"]), "overtime")
    take(sorted(pool, key=tip_hour_eastern), "earliest tip time")
    take(sorted(pool, key=tip_hour_eastern, reverse=True), "latest tip time")
    take(sorted(pool, key=lambda g: -(g["winner_moneyline"] or -9999)), "biggest upset")
    take(sorted((g for g in pool if g["periods"] == 4), key=lambda g: g["margin"]), "close finish")
    median = statistics.median(g["margin"] for g in pool)
    take(sorted(pool, key=lambda g: abs(g["margin"] - median)), "typical margin")

    games = list(chosen.values())
    SELECTED.parent.mkdir(parents=True, exist_ok=True)
    SELECTED.write_text(json.dumps(games, indent=2))
    for game in games:
        print(
            f"  {game['role']:<20} {game['date'][:10]} {game['away']}@{game['home']} "
            f"{game['away_final']}-{game['home_final']} deficit={game['winner_max_deficit']} "
            f"periods={game['periods']} ml={game['winner_moneyline']}"
        )
    return games


# --- Kalshi -------------------------------------------------------------------------


def kalshi_events() -> dict[str, list[dict[str, Any]]]:
    """All archived KXNBAGAME markets, grouped by event ticker."""
    events: dict[str, list[dict[str, Any]]] = {}
    cursor = ""
    while True:
        params: dict[str, Any] = {"series_ticker": "KXNBAGAME", "limit": 1000}
        if cursor:
            params["cursor"] = cursor
        page = get(f"{KALSHI}/historical/markets", **params)
        for market in page.get("markets", []):
            events.setdefault(market["event_ticker"], []).append(market)
        cursor = page.get("cursor") or ""
        if not cursor or not page.get("markets"):
            return events


def kalshi_abbr(espn_abbr: str) -> str:
    return ESPN_TO_KALSHI.get(espn_abbr, espn_abbr)


def find_event(game: dict[str, Any], events: dict[str, list[dict[str, Any]]]) -> str | None:
    """Match on the two teams and a date within a day. Ticker dates are local, ESPN's are UTC."""
    home, away = kalshi_abbr(game["home"]), kalshi_abbr(game["away"])
    utc_date = parse_ts(game["date"]).date()
    for offset in (0, -1, 1):
        stamp = (utc_date + dt.timedelta(days=offset)).strftime("%y%b%d").upper()
        for suffix in (f"{away}{home}", f"{home}{away}"):
            ticker = f"KXNBAGAME-{stamp}{suffix}"
            if ticker in events:
                return ticker
    return None


def kalshi_candles(ticker: str, start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
    data = get(
        f"{KALSHI}/historical/markets/{ticker}/candlesticks",
        start_ts=epoch(start),
        end_ts=epoch(end),
        period_interval=1,
    )
    return data.get("candlesticks", [])


def kalshi_trades(ticker: str, start: dt.datetime, end: dt.datetime) -> list[dict[str, Any]]:
    trades: list[dict[str, Any]] = []
    cursor = ""
    while True:
        params: dict[str, Any] = {
            "ticker": ticker,
            "limit": 1000,
            "min_ts": epoch(start),
            "max_ts": epoch(end),
        }
        if cursor:
            params["cursor"] = cursor
        page = get(f"{KALSHI}/historical/trades", pause=0.06, **params)
        trades.extend(page.get("trades", []))
        cursor = page.get("cursor") or ""
        if not cursor or not page.get("trades"):
            return trades


# --- Game assembly ------------------------------------------------------------------


@dataclass
class Game:
    meta: dict[str, Any]
    event_ticker: str
    home_market: dict[str, Any]
    away_market: dict[str, Any]
    plays: list[dict[str, Any]]
    candles: list[dict[str, Any]]
    away_candles: list[dict[str, Any]]
    trades: list[dict[str, Any]]
    tip: dt.datetime
    end: dt.datetime


def load_game(meta: dict[str, Any], events: dict[str, list[dict[str, Any]]]) -> Game:
    event_ticker = find_event(meta, events)
    if event_ticker is None:
        raise LookupError(f"no Kalshi event for {meta['away']}@{meta['home']} {meta['date']}")
    markets = {m["ticker"].rsplit("-", 1)[1]: m for m in events[event_ticker]}
    home_market = markets[kalshi_abbr(meta["home"])]
    away_market = markets[kalshi_abbr(meta["away"])]

    plays = [p for p in espn_summary(meta["espn_id"])["plays"] if p.get("wallclock")]
    tip, end = parse_ts(plays[0]["wallclock"]), parse_ts(plays[-1]["wallclock"])
    start, stop = tip - PRE_TIP, end + POST_END
    return Game(
        meta=meta,
        event_ticker=event_ticker,
        home_market=home_market,
        away_market=away_market,
        plays=plays,
        candles=kalshi_candles(home_market["ticker"], start, stop),
        away_candles=kalshi_candles(away_market["ticker"], start, stop),
        trades=kalshi_trades(home_market["ticker"], tip - dt.timedelta(minutes=30), stop),
        tip=tip,
        end=end,
    )


def load_all() -> list[Game]:
    events = kalshi_events()
    print(f"archive holds {len(events)} KXNBAGAME events")
    games = []
    for meta in json.loads(SELECTED.read_text()):
        game = load_game(meta, events)
        print(
            f"  {game.event_ticker}: {len(game.plays)} plays, {len(game.candles)} candles, "
            f"{len(game.trades)} trades"
        )
        games.append(game)
    return games


def mid_series(candles: list[dict[str, Any]]) -> tuple[np.ndarray, np.ndarray]:
    """Mid at each minute end, as (end_ts, mid). One-sided quotes become NaN."""
    ts, mids = [], []
    for candle in candles:
        bid = dollars(candle["yes_bid"], "close")
        ask = dollars(candle["yes_ask"], "close")
        ts.append(candle["end_period_ts"])
        mids.append((bid + ask) / 2 if bid is not None and ask is not None and bid > 0 else np.nan)
    return np.asarray(ts), np.asarray(mids, dtype=float)


def scoring_events(game: Game) -> tuple[np.ndarray, np.ndarray]:
    """Wall-clock epoch and home-team margin change for each scoring play."""
    ts, delta = [], []
    margin = 0
    for play in game.plays:
        new_margin = play["homeScore"] - play["awayScore"]
        if new_margin != margin:
            ts.append(epoch(parse_ts(play["wallclock"])))
            delta.append(new_margin - margin)
            margin = new_margin
    return np.asarray(ts), np.asarray(delta, dtype=float)


# --- Analysis -----------------------------------------------------------------------


def minute_xcorr(game: Game) -> dict[int, float]:
    """Correlation of margin change at t with mid change at t+lag, over the game window.

    Positive lag means price moves after the score. A peak at a clearly negative lag would
    point to a clock or time-zone bug.
    """
    candle_ts, mids = mid_series(game.candles)
    mask = (candle_ts >= epoch(game.tip)) & (candle_ts <= epoch(game.end))
    ts, mid = candle_ts[mask], mids[mask]
    score_ts, score_delta = scoring_events(game)
    margin_change = np.zeros(len(ts))
    for when, delta in zip(score_ts, score_delta, strict=True):
        idx = np.searchsorted(ts, when)  # candle whose end_ts is at or after the play
        if idx < len(ts):
            margin_change[idx] += delta
    mid_change = np.diff(mid, prepend=np.nan)

    out = {}
    for lag in LAGS_MIN:
        if lag >= 0:
            x, y = margin_change[: len(ts) - lag], mid_change[lag:]
        else:
            x, y = margin_change[-lag:], mid_change[:lag]
        ok = np.isfinite(x) & np.isfinite(y)
        out[lag] = float(np.corrcoef(x[ok], y[ok])[0, 1]) if ok.sum() > 10 else float("nan")
    return out


def trade_price_grid(game: Game) -> tuple[np.ndarray, np.ndarray]:
    """Last home-team YES trade price on a BIN_S grid, carried forward."""
    rows = sorted((parse_ts(t["created_time"]).timestamp(), float(t["yes_price_dollars"]))
                  for t in game.trades)
    start, stop = epoch(game.tip) - 60, epoch(game.end) + 60
    grid = np.arange(start, stop, BIN_S)
    prices = np.full(len(grid), np.nan)
    if not rows:
        return grid, prices
    trade_ts = np.asarray([r[0] for r in rows])
    trade_px = np.asarray([r[1] for r in rows])
    idx = np.searchsorted(trade_ts, grid, side="right") - 1
    valid = idx >= 0
    prices[valid] = trade_px[idx[valid]]
    return grid, prices


def event_study(game: Game) -> tuple[np.ndarray, np.ndarray, int]:
    """Average signed price path around scoring plays, relative to the play's wallclock.

    Scoring plays within the window of another are dropped, so each path reflects one basket.
    Late-game minutes are excluded because prices there sit near 0 or 1.
    """
    grid, prices = trade_price_grid(game)
    score_ts, score_delta = scoring_events(game)
    offsets = np.arange(-EVENT_WINDOW_S, EVENT_WINDOW_S + 1, BIN_S)
    paths = []
    for i, (when, delta) in enumerate(zip(score_ts, score_delta, strict=True)):
        before_ok = i == 0 or when - score_ts[i - 1] >= 30
        after_ok = i == len(score_ts) - 1 or score_ts[i + 1] - when >= 30
        if not (before_ok and after_ok):
            continue
        idx = np.searchsorted(grid, when + offsets)
        if idx[0] <= 0 or idx[-1] >= len(grid):
            continue
        base = prices[idx[0]]
        if not 0.1 < base < 0.9:
            continue
        paths.append(np.sign(delta) * (prices[idx] - base))
    if not paths:
        return offsets, np.full(len(offsets), np.nan), 0
    return offsets, np.nanmean(paths, axis=0), len(paths)


def half_rise_offset(offsets: np.ndarray, path: np.ndarray) -> float:
    """Seconds from the play's wallclock until the average path has made half its move."""
    if not np.isfinite(path).any():
        return float("nan")
    final = path[-1]
    if final <= 0:
        return float("nan")
    reached = np.nonzero(path >= final / 2)[0]
    return float(offsets[reached[0]]) if len(reached) else float("nan")


def candle_vs_trades(game: Game) -> dict[str, Any]:
    """Does the candle low match the lowest trade each minute? This backs the fill rule.

    Trades were only pulled from 30 minutes before tip, so earlier candles are skipped.
    """
    lows: dict[int, float] = {}
    for trade in game.trades:
        when = parse_ts(trade["created_time"]).timestamp()
        minute_end = int(np.ceil(when / 60) * 60)
        price = float(trade["yes_price_dollars"])
        lows[minute_end] = min(price, lows.get(minute_end, price))
    window_start = epoch(game.tip) - 30 * 60
    candle_minutes = {c["end_period_ts"] for c in game.candles}
    compared = mismatched = priced_without_trades = 0
    for candle in game.candles:
        minute_end = candle["end_period_ts"]
        if minute_end <= window_start:
            continue
        low = dollars(candle["price"], "low")
        if minute_end not in lows:
            priced_without_trades += low is not None
            continue
        compared += 1
        if low is None or abs(low - lows[minute_end]) > 1e-9:
            mismatched += 1
    return {
        "compared": compared,
        "mismatched": mismatched,
        "priced_without_trades": priced_without_trades,
        "trade_minutes_without_candle": len(set(lows) - candle_minutes),
    }


def change_xcorr(game: Game) -> dict[int, float]:
    """Correlation of 3-minute margin change with 3-minute price change, by lag in minutes.

    This is the Stage 0 gate. Longer changes work in thin games, where one-minute changes are
    mostly noise. Prices are in log-odds so that moves near 0 and 1 are comparable.
    """
    candle_ts, mids = mid_series(game.candles)
    grid = np.arange(epoch(game.tip), epoch(game.end), 60)
    idx = np.searchsorted(candle_ts, grid, side="right") - 1
    mid = np.where(idx >= 0, mids[np.maximum(idx, 0)], np.nan)
    clipped = np.clip(mid, 0.02, 0.98)
    log_odds = np.log(clipped / (1 - clipped))

    play_ts = np.asarray([epoch(parse_ts(p["wallclock"])) for p in game.plays])
    play_margin = np.asarray([p["homeScore"] - p["awayScore"] for p in game.plays], dtype=float)
    margin = play_margin[np.maximum(np.searchsorted(play_ts, grid, side="right") - 1, 0)]

    d_price, d_margin = log_odds[3:] - log_odds[:-3], margin[3:] - margin[:-3]
    out = {}
    for lag in range(-10, 11):
        if lag >= 0:
            x, y = d_margin[: len(d_margin) - lag], d_price[lag:]
        else:
            x, y = d_margin[-lag:], d_price[:lag]
        ok = np.isfinite(x) & np.isfinite(y)
        out[lag] = float(np.corrcoef(x[ok], y[ok])[0, 1]) if ok.sum() > 10 else float("nan")
    return out


def flash_sweeps(game: Game, threshold: float = 0.10) -> list[dict[str, Any]]:
    """Minutes where trades printed well below the mid on both sides of that minute.

    These are brief sweeps of the book, not lasting repricing. They matter because a resting
    bid can fill during one while the fair price barely moves.
    """
    sweeps = []
    for label, candles in (("home", game.candles), ("away", game.away_candles)):
        ts, mids = mid_series(candles)
        for i in range(1, len(candles) - 1):
            low = dollars(candles[i]["price"], "low")
            before, after = mids[i - 1], mids[i + 1]
            if low is None or not np.isfinite(before) or not np.isfinite(after):
                continue
            if not epoch(game.tip) <= ts[i] <= epoch(game.end):
                continue
            if min(before, after) - low >= threshold:
                sweeps.append({
                    "market": label,
                    "minute_end_utc": dt.datetime.fromtimestamp(int(ts[i]), dt.UTC).isoformat(),
                    "low": low,
                    "mid_before": round(float(before), 3),
                    "mid_after": round(float(after), 3),
                })
    return sweeps


def polymarket_compare(game: Game) -> dict[str, Any]:
    """Compare Polymarket's free 1-minute history with Kalshi's mid over the game."""
    meta = game.meta
    local_date = game.tip.astimezone(ZoneInfo("America/New_York")).date().isoformat()
    slug = f"nba-{kalshi_abbr(meta['away']).lower()}-{kalshi_abbr(meta['home']).lower()}-{local_date}"
    events = get("https://gamma-api.polymarket.com/events", slug=slug)
    if not events:
        return {"slug": slug, "found": False}
    market = events[0]["markets"][0]
    resolved = json.loads(market["outcomePrices"])
    tokens = json.loads(market["clobTokenIds"])
    home_won = meta["home_final"] > meta["away_final"]
    winner_idx = resolved.index(max(resolved, key=float))
    home_token = tokens[winner_idx if home_won else 1 - winner_idx]
    history = get(
        "https://clob.polymarket.com/prices-history",
        market=home_token,
        startTs=epoch(game.tip),
        endTs=epoch(game.end),
        fidelity=1,
    ).get("history", [])
    if len(history) < 30:
        return {"slug": slug, "found": True, "points": len(history)}

    pm_ts = np.asarray([h["t"] for h in history])
    pm_px = np.asarray([h["p"] for h in history], dtype=float)
    candle_ts, mids = mid_series(game.candles)
    idx = np.searchsorted(candle_ts, pm_ts, side="right") - 1
    kalshi = np.where(idx >= 0, mids[np.maximum(idx, 0)], np.nan)
    ok = np.isfinite(kalshi)
    gap = np.abs(pm_px[ok] - kalshi[ok])
    d_pm, d_kalshi = pm_px[3:] - pm_px[:-3], kalshi[3:] - kalshi[:-3]
    ok3 = np.isfinite(d_kalshi)
    return {
        "slug": slug,
        "found": True,
        "points": len(history),
        "median_step_s": float(np.median(np.diff(pm_ts))),
        "median_abs_gap_cents": round(float(np.median(gap) * 100), 2),
        "p95_abs_gap_cents": round(float(np.percentile(gap, 95) * 100), 2),
        "corr_3min_change": round(float(np.corrcoef(d_pm[ok3], d_kalshi[ok3])[0, 1]), 3),
    }


def analyze_game(game: Game) -> dict[str, Any]:
    meta = game.meta
    candle_ts, mids = mid_series(game.candles)
    away_ts, away_mids = mid_series(game.away_candles)
    in_game = (candle_ts >= epoch(game.tip)) & (candle_ts <= epoch(game.end))

    home_won = meta["home_final"] > meta["away_final"]
    settled_yes = game.home_market.get("result") == "yes"

    # Two-market consistency: home and away mids should sum to about $1.
    _, ai, bi = np.intersect1d(candle_ts, away_ts, return_indices=True)
    sums = mids[ai] + away_mids[bi]
    sums = sums[np.isfinite(sums)]

    spreads = [
        dollars(c["yes_ask"], "close") - dollars(c["yes_bid"], "close")
        for c, inside in zip(game.candles, in_game, strict=True)
        if inside and dollars(c["yes_ask"], "close") is not None
        and dollars(c["yes_bid"], "close") is not None
    ]

    gaps = np.diff(candle_ts)
    pre_tip = candle_ts < epoch(game.tip)
    xcorr = minute_xcorr(game)
    finite = {k: v for k, v in xcorr.items() if np.isfinite(v)}
    peak_lag = max(finite, key=lambda k: finite[k]) if finite else None
    gate = {k: v for k, v in change_xcorr(game).items() if np.isfinite(v)}
    gate_peak = max(gate, key=lambda k: gate[k])
    winner_candles = game.candles if home_won else game.away_candles
    winner_lows = [
        low
        for c in winner_candles
        if epoch(game.tip) <= c["end_period_ts"] <= epoch(game.end)
        and (low := dollars(c["price"], "low")) is not None
    ]
    return {
        "role": meta["role"],
        "event": game.event_ticker,
        "scheduled": meta["date"],
        "first_wallclock": game.tip.isoformat(),
        "last_wallclock": game.end.isoformat(),
        "tip_delay_min": (game.tip - parse_ts(meta["date"])).total_seconds() / 60,
        "game_minutes": (game.end - game.tip).total_seconds() / 60,
        "market_close": game.home_market["close_time"],
        "occurrence_datetime": game.home_market.get("occurrence_datetime"),
        "winner_matches_settlement": home_won == settled_yes,
        "ticker_order_away_home": game.event_ticker.endswith(
            kalshi_abbr(meta["away"]) + kalshi_abbr(meta["home"])
        ),
        "neutral_site": meta["neutral_site"],
        "pre_tip_mid": float(mids[pre_tip][-1]) if pre_tip.any() else None,
        "min_mid_in_game": float(np.nanmin(mids[in_game])),
        "max_mid_in_game": float(np.nanmax(mids[in_game])),
        "low_trade": min((float(t["yes_price_dollars"]) for t in game.trades), default=None),
        "median_spread": float(np.median(spreads)) if spreads else None,
        "candles": len(candle_ts),
        "minute_gaps": int((gaps > 60).sum()),
        "largest_gap_min": float(gaps.max() / 60) if len(gaps) else None,
        "in_game_volume": float(sum(
            float(c.get("volume_fp", c.get("volume", 0)))
            for c, inside in zip(game.candles, in_game, strict=True) if inside
        )),
        "home_plus_away_mid_median": float(np.median(sums)) if len(sums) else None,
        "xcorr_peak_lag_min": peak_lag,
        "xcorr": {str(k): round(v, 3) for k, v in xcorr.items()},
        "gate_peak_lag_min": gate_peak,
        "gate_peak_corr": round(gate[gate_peak], 3),
        "gate_corr_at_minus_3": round(gate[-3], 3),
        "candle_vs_trades": candle_vs_trades(game),
        "trades_home_market": len(game.trades),
        "winner_low_trade": min(winner_lows) if winner_lows else None,
        "flash_sweeps": flash_sweeps(game),
        "polymarket": polymarket_compare(game),
    }


def analyze(games: list[Game]) -> dict[str, Any]:
    rows = [analyze_game(game) for game in games]

    offsets = np.arange(-EVENT_WINDOW_S, EVENT_WINDOW_S + 1, BIN_S)
    weighted, total = np.zeros(len(offsets)), 0
    for game, row in zip(games, rows, strict=True):
        _, path, count = event_study(game)
        row["event_study_baskets"] = count
        row["half_rise_s"] = half_rise_offset(offsets, path)
        row["event_move_cents"] = float(path[-1] * 100) if count else None
        if count:
            weighted += np.nan_to_num(path) * count
            total += count
    pooled = weighted / total
    result = {
        "games": rows,
        "pooled_event_study": {
            "offset_s": offsets.tolist(),
            "signed_price_move_cents": np.round(pooled * 100, 3).tolist(),
            "baskets": total,
            "half_rise_s": half_rise_offset(offsets, pooled),
        },
    }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "summary.json").write_text(json.dumps(result, indent=2))

    print(f"\n{'game':<30}{'role':<20}{'gate lag':>9}{'corr':>6}{'at -3':>7}{'move':>7}"
          f"{'sweeps':>7}{'trades':>8}{'ok':>4}")
    for row in rows:
        print(f"{row['event']:<30}{row['role']:<20}{row['gate_peak_lag_min']:>+9}"
              f"{row['gate_peak_corr']:>6.2f}{row['gate_corr_at_minus_3']:>7.2f}"
              f"{(row['event_move_cents'] or 0):>6.1f}c{len(row['flash_sweeps']):>7}"
              f"{row['trades_home_market']:>8}"
              f"{'Y' if row['winner_matches_settlement'] else 'N':>4}")
    compared = sum(r["candle_vs_trades"]["compared"] for r in rows)
    mismatched = sum(r["candle_vs_trades"]["mismatched"] for r in rows)
    print(f"\ncandle low vs lowest trade: {mismatched} mismatches in {compared} minutes")
    print("\npolymarket vs kalshi in game:")
    for row in rows:
        print(f"  {row['event'][10:]:<22}{row['polymarket']}")
    for row in rows:
        for sweep in row["flash_sweeps"]:
            print(f"  sweep {row['event'][10:]:<22}{sweep}")
    study = result["pooled_event_study"]
    print(f"\npooled: {study['baskets']} baskets, half of move by {study['half_rise_s']}s")
    print("offset s -> cents:")
    for offset, move in zip(study["offset_s"][::4], study["signed_price_move_cents"][::4],
                            strict=True):
        print(f"  {offset:>5} {move:>7.2f}")
    return result


# --- Charts -------------------------------------------------------------------------


def game_figure(game: Game, row: dict[str, Any]) -> go.Figure:
    meta = game.meta
    candle_ts, mids = mid_series(game.candles)
    times = [dt.datetime.fromtimestamp(t, dt.UTC) for t in candle_ts]
    plays = game.plays
    play_times = [parse_ts(p["wallclock"]) for p in plays]
    margins = [p["homeScore"] - p["awayScore"] for p in plays]

    fig = make_subplots(rows=2, cols=1, shared_xaxes=True, row_heights=[0.62, 0.38],
                        vertical_spacing=0.05)
    fig.add_trace(go.Scatter(x=times, y=mids, name=f"Kalshi mid, {meta['home']} wins",
                             line={"color": "#2563eb", "width": 1.8}), row=1, col=1)
    fig.add_trace(go.Scatter(x=play_times, y=margins, name=f"Margin ({meta['home']} minus "
                             f"{meta['away']})", line={"color": "#111827", "width": 1.2,
                                                       "shape": "hv"}), row=2, col=1)
    score_ts, score_delta = scoring_events(game)
    score_times = [dt.datetime.fromtimestamp(t, dt.UTC) for t in score_ts]
    score_margins = np.cumsum(score_delta)
    for sign, color, label in ((1, "#16a34a", meta["home"]), (-1, "#dc2626", meta["away"])):
        pick = np.sign(score_delta) == sign
        fig.add_trace(go.Scatter(
            x=[t for t, keep in zip(score_times, pick, strict=True) if keep],
            y=score_margins[pick], mode="markers", name=f"{label} scores",
            marker={"color": color, "size": 4}), row=2, col=1)
    fig.add_vline(x=game.tip, line={"color": "#9ca3af", "dash": "dot"})
    fig.update_yaxes(title_text="Price ($)", range=[0, 1], row=1, col=1)
    fig.update_yaxes(title_text="Points", row=2, col=1)
    fig.update_xaxes(title_text="UTC", row=2, col=1)
    fig.update_layout(
        title=(f"{meta['away']} {meta['away_final']} at {meta['home']} {meta['home_final']} · "
               f"{row['role']} · {game.event_ticker} · correlation peak at "
               f"{row['xcorr_peak_lag_min']} min"),
        height=520, template="plotly_white", margin={"l": 60, "r": 20, "t": 60, "b": 40},
        legend={"orientation": "h", "y": -0.14},
    )
    return fig


def study_figure(study: dict[str, Any]) -> go.Figure:
    fig = go.Figure(go.Scatter(x=study["offset_s"], y=study["signed_price_move_cents"],
                               line={"color": "#2563eb", "width": 2}, mode="lines+markers"))
    fig.add_vline(x=0, line={"color": "#9ca3af", "dash": "dot"})
    fig.add_hline(y=0, line={"color": "#e5e7eb"})
    fig.update_layout(
        title=f"Average price move around a scoring play ({study['baskets']} isolated baskets)",
        xaxis_title="Seconds from ESPN wallclock of the scoring play",
        yaxis_title="Move toward the scoring team (¢)",
        height=380, template="plotly_white",
    )
    return fig


def write_charts(games: list[Game], result: dict[str, Any]) -> Path:
    OUT.mkdir(parents=True, exist_ok=True)
    parts = [study_figure(result["pooled_event_study"]).to_html(
        full_html=False, include_plotlyjs=True)]
    for game, row in zip(games, result["games"], strict=True):
        parts.append(game_figure(game, row).to_html(full_html=False, include_plotlyjs=False))
    path = OUT / "index.html"
    path.write_text(
        "<!doctype html><html><head><meta charset='utf-8'><title>Stage 0 spike</title></head>"
        "<body style='font-family:sans-serif;max-width:1100px;margin:auto;background:#fff'>"
        "<h1>Stage 0: Kalshi price vs score, 2025–26</h1>" + "".join(parts) + "</body></html>"
    )
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["select", "fetch", "analyze", "chart", "all"])
    command = parser.parse_args().command

    if command in ("select", "all"):
        select_games()
    if command == "select":
        return
    games = load_all()
    if command == "fetch":
        return
    result = analyze(games)
    if command in ("chart", "all"):
        print(f"\ncharts: {write_charts(games, result)}")


if __name__ == "__main__":
    main()
