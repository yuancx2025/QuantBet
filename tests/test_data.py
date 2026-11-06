"""Source quirks and split enforcement, using small hand-written records."""

from __future__ import annotations

import datetime as dt

import pytest

from quantbet_nba.config import HoldoutAccessError, Splits, season_of, ticker_date
from quantbet_nba.data import espn, kalshi


def _ticker(date: dt.date) -> str:
    return f"KXNBAGAME-{date:%y%b%d}BOSNYK".upper()


def test_splits_follow_the_boundary_recorded_in_the_config():
    # The boundary lives in configs/splits.yaml and may move, so it is read rather than repeated.
    splits = Splits.load()
    first_holdout = dt.date.fromisoformat(splits.rules["holdout"]["first_ticker_date"])
    last_tuning = dt.date.fromisoformat(splits.rules["tuning_2026_27"]["last_ticker_date"])
    assert splits.period_of("KXNBAGAME-26MAR27LACIND") == "development"
    assert splits.period_of(_ticker(last_tuning)) == "tuning_2026_27"
    assert splits.period_of("KXNBAGAME-26NOV05MIAGSW") == "tuning_2026_27"
    with pytest.raises(HoldoutAccessError):
        splits.period_of(_ticker(first_holdout))
    with pytest.raises(HoldoutAccessError):
        splits.period_of(_ticker(first_holdout + dt.timedelta(days=60)))


def test_tuning_and_holdout_leave_no_gap():
    # A day in neither period would raise "outside every period" instead of reading as holdout.
    splits = Splits.load()
    first_holdout = dt.date.fromisoformat(splits.rules["holdout"]["first_ticker_date"])
    last_tuning = dt.date.fromisoformat(splits.rules["tuning_2026_27"]["last_ticker_date"])
    assert first_holdout == last_tuning + dt.timedelta(days=1)


def test_unreadable_tickers_fail_closed():
    with pytest.raises(ValueError):
        Splits.load().period_of("NOT-A-TICKER")
    with pytest.raises(ValueError):
        ticker_date("KXNBAGAME-26XXX05MIAGSW")


def test_game_before_any_configured_season_is_rejected():
    with pytest.raises(ValueError):
        Splits.load().period_of("KXNBAGAME-25APR15ATLORL")


def test_season_rolls_over_in_august():
    assert season_of(dt.date(2026, 7, 31)) == "2025-26"
    assert season_of(dt.date(2026, 8, 1)) == "2026-27"


def test_candle_field_variants_and_end_stamps():
    archive = {
        "end_period_ts": 1000,
        "yes_bid": {"close": "0.2100"},
        "yes_ask": {"close": "0.2200"},
        "price": {"low": "0.2000", "high": "0.2300"},
        "volume": "15.00",
    }
    live = {
        "end_period_ts": 1060,
        "yes_bid": {"close_dollars": "0.3100"},
        "yes_ask": {"close_dollars": "0.3200"},
        "price": {"low_dollars": None, "high_dollars": None},
        "volume_fp": "0",
    }
    table = kalshi.normalize_candles("M", [live, archive])
    assert table["end_us"].to_list() == [1_000_000_000, 1_060_000_000]
    assert table["bid_pips"].to_list() == [2100, 3100]
    assert table["ask_pips"].to_list() == [2200, 3200]
    assert table["trade_low_pips"].to_list() == [2000, None]
    assert table["volume"].to_list() == [15.0, 0.0]


def _trade(trade_id, price="0.5000", block=False, when="2026-03-28T01:42:54.127355Z"):
    return {
        "trade_id": trade_id,
        "created_time": when,
        "yes_price_dollars": price,
        "count_fp": "20.00",
        "is_block_trade": block,
        "taker_side": "no",
    }


def test_trades_drop_blocks_and_duplicates_and_keep_exact_time():
    table = kalshi.normalize_trades("M", [_trade("a"), _trade("a"), _trade("b", block=True)])
    assert table.height == 1
    expected = dt.datetime(2026, 3, 28, 1, 42, 54, 127355, tzinfo=dt.UTC)
    assert table["ts_us"][0] == (expected - dt.datetime(1970, 1, 1, tzinfo=dt.UTC)) // dt.timedelta(
        microseconds=1
    )
    assert table["price_pips"][0] == 5000


def test_empty_responses_keep_their_schema():
    assert kalshi.normalize_trades("M", []).schema == kalshi.TRADE_SCHEMA
    assert kalshi.normalize_candles("M", []).schema == kalshi.CANDLE_SCHEMA


def _us(text):
    return espn.to_us(text)


def test_event_match_uses_the_eastern_date_not_utc():
    # 02:30 UTC on Oct 25 is still Oct 24 in the US, and the ticker uses the US date.
    events = {"KXNBAGAME-25OCT24PHXLAC": [], "KXNBAGAME-25OCT25PHXLAC": []}
    ticker, exact = kalshi.find_event("LAC", "PHX", _us("2025-10-25T02:30Z"), events)
    assert (ticker, exact) == ("KXNBAGAME-25OCT24PHXLAC", True)


def test_event_match_falls_back_to_a_neighbouring_date_and_flags_it():
    events = {"KXNBAGAME-25OCT25PHXLAC": []}
    ticker, exact = kalshi.find_event("LAC", "PHX", _us("2025-10-25T02:30Z"), events)
    assert (ticker, exact) == ("KXNBAGAME-25OCT25PHXLAC", False)


def test_event_match_finds_nothing_for_unknown_teams():
    assert kalshi.find_event("AAA", "BBB", _us("2025-10-25T02:30Z"), {}) == (None, False)


def test_settlement_reports_exceptional_results():
    market = {"ticker": "M", "result": "scalar", "settlement_value_dollars": "0.5000"}
    assert kalshi.market_outcome(market) == ("scalar", 5000)
    with pytest.raises(ValueError):
        kalshi.market_outcome({"ticker": "M", "result": "", "settlement_value_dollars": ""})


def test_espn_abbreviations_map_to_kalshi():
    assert [espn.kalshi_abbr(a) for a in ("GS", "NY", "SA", "NO", "UTAH", "WSH", "BOS")] == [
        "GSW", "NYK", "SAS", "NOP", "UTA", "WAS", "BOS",
    ]  # fmt: skip


def test_plays_follow_sequence_and_count_reversals():
    summary = {
        "plays": [
            {"wallclock": "2026-01-01T00:00:00Z"},
            {"wallclock": "2026-01-01T00:05:00Z"},
            {"wallclock": "2026-01-01T00:03:00Z"},
            {},
            {"wallclock": "2026-01-01T00:04:00Z"},
        ]
    }
    plays = espn.parse_plays(summary)
    assert plays["final_play_us"] == _us("2026-01-01T00:04:00Z")
    assert plays["latest_wallclock_us"] == _us("2026-01-01T00:05:00Z")
    assert plays["wallclock_reversals"] == 1
    assert espn.parse_plays({"plays": []}) is None


def test_scoreboard_keeps_placeholder_teams_so_they_can_be_excluded_with_a_reason():
    def competitor(side, team):
        return {"homeAway": side, "team": team, "score": "0"}

    payload = {
        "events": [
            {
                "id": "1",
                "date": "2025-10-02T00:00Z",
                "season": {"type": 2},
                "competitions": [
                    {
                        "status": {"type": {"completed": False}},
                        "competitors": [
                            competitor("home", {"abbreviation": "GS"}),
                            competitor("away", {"displayName": "TBD"}),
                        ],
                    }
                ],
            }
        ]
    }
    game = espn.parse_scoreboard(payload)[0]
    assert (game["home"], game["away"]) == ("GSW", "")
    assert kalshi.find_event(game["home"], game["away"], game["scheduled_us"], {}) == (None, False)
