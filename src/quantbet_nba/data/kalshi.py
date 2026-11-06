"""Kalshi archive adapters: raw responses to clean tables, with the source quirks handled here.

Quirks from docs/decisions/0001-stage0-findings.md: field names differ by endpoint, candles are
end-stamped, a candle may have no trade prices when only quotes changed, and block trades did not
go through the book.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

import polars as pl

from quantbet_nba.data.espn import to_us
from quantbet_nba.engine.types import PIPS_PER_DOLLAR, US_PER_SECOND

BASE = "https://api.elections.kalshi.com/trade-api/v2"
SERIES = "KXNBAGAME"
EASTERN = ZoneInfo("America/New_York")

CANDLE_SCHEMA = {
    "market": pl.String,
    "end_us": pl.Int64,
    "bid_pips": pl.Int32,
    "ask_pips": pl.Int32,
    "trade_low_pips": pl.Int32,
    "trade_high_pips": pl.Int32,
    "volume": pl.Float64,
}
TRADE_SCHEMA = {
    "market": pl.String,
    "ts_us": pl.Int64,
    "price_pips": pl.Int32,
    "qty": pl.Float64,
    "trade_id": pl.String,
    "taker_side": pl.String,
}


def _pips(block: dict[str, Any] | None, name: str) -> int | None:
    """Read a dollar price. Archive candles use `close`, live ones `close_dollars`."""
    if not block:
        return None
    raw = block.get(f"{name}_dollars", block.get(name))
    return None if raw in (None, "") else round(float(raw) * PIPS_PER_DOLLAR)


def _volume(candle: dict[str, Any]) -> float:
    raw = candle.get("volume_fp", candle.get("volume"))
    return 0.0 if raw in (None, "") else float(raw)


def normalize_candles(market: str, candles: list[dict[str, Any]]) -> pl.DataFrame:
    rows = [
        {
            "market": market,
            "end_us": int(c["end_period_ts"]) * US_PER_SECOND,
            "bid_pips": _pips(c.get("yes_bid"), "close"),
            "ask_pips": _pips(c.get("yes_ask"), "close"),
            "trade_low_pips": _pips(c.get("price"), "low"),
            "trade_high_pips": _pips(c.get("price"), "high"),
            "volume": _volume(c),
        }
        for c in candles
    ]
    return pl.DataFrame(rows, schema=CANDLE_SCHEMA).unique(["market", "end_us"]).sort("end_us")


def normalize_trades(market: str, trades: list[dict[str, Any]]) -> pl.DataFrame:
    """Book prints only: block trades can't fill a resting order, so they are removed here."""
    rows = [
        {
            "market": market,
            "ts_us": to_us(t["created_time"]),
            "price_pips": round(float(t["yes_price_dollars"]) * PIPS_PER_DOLLAR),
            "qty": float(t["count_fp"]),
            "trade_id": t["trade_id"],
            "taker_side": t.get("taker_side"),
        }
        for t in trades
        if not t.get("is_block_trade")
    ]
    return pl.DataFrame(rows, schema=TRADE_SCHEMA).unique("trade_id").sort("ts_us", "trade_id")


def group_events(markets: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    events: dict[str, list[dict[str, Any]]] = {}
    for market in markets:
        events.setdefault(market["event_ticker"], []).append(market)
    return events


def find_event(
    home: str, away: str, scheduled_us: int, events: dict[str, list[dict[str, Any]]]
) -> tuple[str | None, bool]:
    """Find a game's event: (ticker, exact_date). Ticker dates are US local, ESPN's are UTC.

    The Eastern date is tried first and neighbours only as a fallback, so that the same two teams
    playing on consecutive nights can't be crossed. A fallback match is flagged for review.
    """
    moment = dt.datetime.fromtimestamp(scheduled_us / US_PER_SECOND, EASTERN).date()
    for offset in (0, -1, 1):
        stamp = (moment + dt.timedelta(days=offset)).strftime("%y%b%d").upper()
        for teams in (f"{away}{home}", f"{home}{away}"):
            ticker = f"{SERIES}-{stamp}{teams}"
            if ticker in events:
                return ticker, offset == 0
    return None, False


def market_outcome(market: dict[str, Any]) -> tuple[str, int]:
    """Return (result, settlement pips). Anything other than yes/no is an exceptional result."""
    raw = market.get("settlement_value_dollars")
    if raw in (None, ""):
        raise ValueError(f"{market['ticker']} has no settlement value")
    return str(market["result"]), round(float(raw) * PIPS_PER_DOLLAR)
