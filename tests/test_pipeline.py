"""End to end on a small made-up dataset: raw files -> dataset -> backtest -> saved run."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import polars as pl
import pytest
import yaml

from quantbet_nba.analytics.experiment import expand_scenarios, run_experiment
from quantbet_nba.analytics.metrics import block_stats, bootstrap_game_intervals, fill_phases
from quantbet_nba.analytics.tags import tag_fills
from quantbet_nba.config import HoldoutAccessError, Splits
from quantbet_nba.data.coverage import build_coverage
from quantbet_nba.data.espn import to_us
from quantbet_nba.data.http import write_raw
from quantbet_nba.data.pull import PullPaths, _resolve_game, normalize_season
from quantbet_nba.data.store import GameStore
from quantbet_nba.engine.backtest import BacktestConfig, run_backtest
from quantbet_nba.engine.fills import ThroughLimit
from quantbet_nba.engine.replay import replay_game
from quantbet_nba.engine.types import US_PER_SECOND
from quantbet_nba.strategies.fixed_offset import FixedOffsetLadder

HOUR = 3600 * US_PER_SECOND
MINUTE = 60 * US_PER_SECOND


def _iso(us: int) -> str:
    import datetime as dt

    return dt.datetime.fromtimestamp(us / US_PER_SECOND, dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _candle(end_us, bid, ask, low=None, volume=0):
    return {
        "end_period_ts": end_us // US_PER_SECOND,
        "yes_bid": {"close": f"{bid / 100:.4f}"},
        "yes_ask": {"close": f"{ask / 100:.4f}"},
        "price": {"low": None if low is None else f"{low / 100:.4f}"},
        "volume": str(volume),
    }


def _trade(trade_id, ts_us, cents, qty=5):
    return {
        "trade_id": trade_id,
        "created_time": _iso(ts_us),
        "yes_price_dollars": f"{cents / 100:.4f}",
        "count_fp": f"{qty:.2f}",
        "is_block_trade": False,
        "taker_side": "no",
    }


def make_game(
    root: Path, day: int, home_wins: bool, dip_cents: int, clock_error_us: int = 0,
    postgame_print: int | None = None,
):
    """One game: home is the 70c favorite and dips to `dip_cents` during play.

    `clock_error_us` makes ESPN's play times run late, as some real games do. `postgame_print` adds
    a home print 3 minutes after the true end of the game.
    """
    event = f"KXNBAGAME-26JAN{day:02d}AWYHOM"
    scheduled = to_us(f"2026-01-{day:02d}T23:00Z")
    final = scheduled + 3 * HOUR
    paths = PullPaths(root)
    home, away = f"{event}-HOM", f"{event}-AWY"
    plan = scheduled - 2 * HOUR

    home_candles = [
        _candle(plan - 30 * MINUTE, 69, 71, volume=100),
        _candle(plan - MINUTE, 69, 71, low=70, volume=50),
    ]
    away_candles = [_candle(plan - MINUTE, 29, 31, low=30, volume=40)]
    dip_time = scheduled + HOUR
    home_candles.append(_candle(dip_time + MINUTE, 69, 71, low=dip_cents, volume=5))
    home_trades = [_trade(f"{event}-h1", dip_time, dip_cents)]
    away_trades = [_trade(f"{event}-a1", dip_time + MINUTE, 31)]
    if not home_wins:
        home_trades.append(_trade(f"{event}-h2", final - MINUTE, 1))
    else:
        away_trades.append(_trade(f"{event}-a2", final - MINUTE, 1))
    if postgame_print is not None:
        home_trades.append(_trade(f"{event}-h3", final + 3 * MINUTE, postgame_print))

    window = {
        "min_ts": (scheduled - 3 * HOUR) // US_PER_SECOND,
        "max_ts": (final + clock_error_us + 15 * MINUTE) // US_PER_SECOND,
    }
    for ticker, candles, trades in ((home, home_candles, home_trades), (away, away_candles, away_trades)):
        write_raw(paths.candles(ticker), {"ticker": ticker}, {"candlesticks": candles})
        write_raw(paths.trades(ticker), {"ticker": ticker, **window}, trades)
    return {
        "espn_id": f"e{day}",
        "game_id": event,
        "scheduled_us": scheduled,
        "first_play_us": scheduled + 10 * MINUTE + clock_error_us,
        "final_play_us": final + clock_error_us,
        "latest_wallclock_us": final + clock_error_us,
        "wallclock_reversals": 0,
        "replay_start_us": scheduled - 4 * HOUR,
        "home": "HOM",
        "away": "AWY",
        "home_market": home,
        "away_market": away,
        "home_result": "yes" if home_wins else "no",
        "away_result": "no" if home_wins else "yes",
        "home_settlement_pips": 10_000 if home_wins else 0,
        "away_settlement_pips": 0 if home_wins else 10_000,
        "home_final": 100 if home_wins else 90,
        "away_final": 90 if home_wins else 100,
        "neutral_site": False,
        "exact_ticker_date": True,
    }


@pytest.fixture
def dataset(tmp_path):
    root = tmp_path / "data"
    plans = [
        make_game(root, 10, home_wins=True, dip_cents=60),  # favorite dips, then wins
        make_game(root, 11, home_wins=False, dip_cents=68),  # favorite loses outright
        make_game(root, 12, home_wins=True, dip_cents=69),  # favorite wins with no real dip
    ]
    exclusions = [{"espn_id": "e99", "reason": "no_kalshi_event"}]
    normalize_season(root, "2025-26", plans, exclusions, [], log=lambda *_: None)
    return root


def test_dataset_has_games_exclusions_and_a_manifest(dataset):
    store = GameStore(dataset, "2025-26")
    assert len(store.games("development")) == 3
    assert store.games("tuning_2026_27") == []
    assert store.exclusions()["reason"].to_list() == ["no_kalshi_event"]
    manifest = json.loads((store.season_dir / "manifest.json").read_text())
    assert manifest["dataset_sha256"] == store.dataset_hash()
    assert manifest["n_files"] == 8


def test_backtest_hand_computed_result(dataset):
    # Favorite rungs at 65, 60, 55 (mid 70).
    # Game 10: dip to 60 fills only 65 under "strictly below", which wins: +35.
    # Game 11: loses, price reaches 1, fills all three: -65 -60 -55 = -180.
    # Game 12: dip to 69 fills nothing.
    run = run_backtest(FixedOffsetLadder(side="favorite"), dataset, BacktestConfig())
    assert run.fills["gross_pnl"].sum() == pytest.approx(0.35 - 1.80)
    assert run.orders.height == 9
    assert run.orders["status"].value_counts().sort("status").to_dicts() == [
        {"status": "filled", "count": 4},
        {"status": "unfilled", "count": 5},
    ]
    assert run.skips.is_empty()
    assert run.orders["label_role"].unique().to_list() == ["favorite"]


def test_fill_rules_differ_as_designed(dataset):
    def count(rule):
        run = run_backtest(
            FixedOffsetLadder(side="favorite"), dataset, BacktestConfig(fill_model=rule)
        )
        return run.fills.height

    # Game 10's print exactly at 60 also fills the 60 rung under the optimistic rule.
    assert count("touch_at_limit") == count("through_limit") + 1


def test_block_stats_on_the_hand_computed_case(dataset):
    run = run_backtest(FixedOffsetLadder(side="favorite"), dataset, BacktestConfig())
    stats = block_stats(run.orders, run.fills)
    assert stats["contracts_submitted"] == 9
    assert stats["contracts_filled"] == 4
    assert stats["fill_rate"] == pytest.approx(4 / 9, abs=1e-3)
    assert stats["win_rate_when_filled"] == 0.25
    assert stats["games_with_orders"] == 3
    assert stats["games_with_fills"] == 2


def test_small_samples_report_no_interval_rather_than_a_falsely_precise_one(dataset):
    run = run_backtest(FixedOffsetLadder(side="favorite"), dataset, BacktestConfig())
    assert block_stats(run.orders, run.fills)["ci95_gross_per_filled"] is None


def test_fills_split_at_scheduled_tipoff(dataset):
    run = run_backtest(FixedOffsetLadder(side="favorite"), dataset, BacktestConfig())
    phases = fill_phases(run.fills, run.games)
    # Every dip in this dataset happens during play.
    assert phases["before_scheduled_tipoff"]["contracts_filled"] == 0
    assert phases["after_scheduled_tipoff"]["contracts_filled"] == 4
    assert phases["after_scheduled_tipoff"]["gross_pnl"] == pytest.approx(0.35 - 1.80)


def test_bootstrap_resamples_whole_games():
    # Identical games leave no uncertainty, however many rungs each one had.
    pnl = np.array([1.0, 1.0, 1.0])
    filled = np.array([2.0, 2.0, 2.0])
    submitted = np.array([4.0, 4.0, 4.0])
    intervals = bootstrap_game_intervals(pnl, filled, submitted)
    assert intervals["per_filled"] == [0.5, 0.5]
    # A varied sample's interval has to contain the point estimate.
    pnl = np.array([1.0, -1.0, 2.0, -2.0, 0.5] * 20)
    filled = np.ones(100)
    low, high = bootstrap_game_intervals(pnl, filled, filled * 2)["per_filled"]
    assert low < pnl.mean() < high


def test_sweep_and_drift_tags(dataset):
    store = GameStore(dataset, "2025-26")
    bundle = store.load(store.games("development")[0])
    # Add a quote five minutes after the dip that recovered, so the fill counts as a sweep.
    from quantbet_nba.engine.types import Quote

    dip = bundle.trades[0].ts_us
    market = bundle.trades[0].market
    bundle.quotes.append(Quote(dip - MINUTE, market, 6900, 7100))
    bundle.quotes.append(Quote(dip + 6 * MINUTE, market, 6900, 7100))
    bundle.quotes.sort(key=lambda q: q.ts_us)
    result = replay_game(bundle, FixedOffsetLadder(side="favorite"), ThroughLimit())
    assert set(tag_fills(result, bundle).values()) == {"sweep"}

    # With no later quote the tag can't be determined.
    bundle.quotes = [q for q in bundle.quotes if q.ts_us <= dip]
    result = replay_game(bundle, FixedOffsetLadder(side="favorite"), ThroughLimit())
    assert set(tag_fills(result, bundle).values()) == {"unknown"}


def test_coverage_report(dataset):
    report = build_coverage(GameStore(dataset, "2025-26"), "development")
    assert report["usable_games"] == 3
    assert report["exclusions"] == {"no_kalshi_event": 1}
    january = report["months"]["2026-01"]
    assert january["games_complete"] == 3
    assert january["markets_quoted_at_plan"] == 6
    assert report["candle_trade_check"]["low_differs"] == 0


@pytest.mark.parametrize("hours", [1, 24])
def test_late_espn_clock_is_corrected_and_postgame_prints_do_not_fill(tmp_path, hours):
    root = tmp_path / "data"
    # The 69c dip fills nothing. Left uncorrected, the 50c postgame print would fill two rungs.
    plan = make_game(root, 12, True, 69, clock_error_us=hours * HOUR, postgame_print=50)
    normalize_season(root, "2025-26", [plan], [], [], log=lambda *_: None)
    store = GameStore(root, "2025-26")
    game = store.games("development")[0]
    assert game["final_play_us"] == plan["scheduled_us"] + 3 * HOUR
    assert game["first_play_us"] == plan["scheduled_us"] + 10 * MINUTE
    assert game["final_play_shift_us"] == game["first_play_shift_us"] == hours * HOUR
    assert run_backtest(FixedOffsetLadder(side="favorite"), root, BacktestConfig()).fills.is_empty()

    flags = build_coverage(store, "development")["flags"]
    assert flags["final_play_clock_corrected"] == flags["first_play_clock_corrected"] == 1
    assert flags["implausible_game_length"] == flags["final_play_far_from_last_trade"] == 0


def test_correct_games_are_left_alone_and_coverage_catches_an_uncorrected_end(dataset):
    store = GameStore(dataset, "2025-26")
    assert all(g["final_play_shift_us"] == 0 for g in store.games("development"))
    assert build_coverage(store, "development")["flags"]["implausible_game_length"] == 0

    path = store.season_dir / "games.parquet"
    games = pl.read_parquet(path)
    games.with_columns(pl.col("final_play_us") + 24 * HOUR).write_parquet(path)
    flags = build_coverage(store, "development")["flags"]
    assert flags["implausible_game_length"] == flags["final_play_far_from_last_trade"] == 3


def _config(tmp_path, **overrides):
    config = {
        "name": "test",
        "season": "2025-26",
        "period": "development",
        "strategy": {"name": "fixed_offset", "offsets_cents": [5, 10, 15], "hours_before": 2},
        "execution": {"placement_latency_s": 2},
        "primary_scenario": {"fill_model": "through_limit", "side": "favorite", "size": 1},
        "scenarios": [
            {"fill_model": ["through_limit"], "side": ["favorite", "both"], "size": [1]},
            {"fill_model": ["through_limit"], "side": ["favorite"], "size": [1]},
        ],
    }
    config.update(overrides)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(config))
    return path


def test_expand_scenarios_removes_duplicates(tmp_path):
    config = yaml.safe_load(_config(tmp_path).read_text())
    assert len(expand_scenarios(config)) == 2


def test_shipped_config_expands_to_all_planned_scenarios():
    path = Path(__file__).parents[1] / "configs/strategies/fixed_offset.yaml"
    assert len(expand_scenarios(yaml.safe_load(path.read_text()))) == 15


def test_experiment_saves_outputs_and_logs_the_run(dataset, tmp_path):
    runs = tmp_path / "runs"
    out = run_experiment(_config(tmp_path), dataset, runs, log=lambda *_: None)
    manifest = json.loads((out / "manifest.json").read_text())
    assert len(manifest["scenario_result_sha256"]) == 2
    assert manifest["dataset_sha256"] == GameStore(dataset, "2025-26").dataset_hash()
    report = (out / "report.md").read_text()
    assert "Gross of fees" in report and "Order survival is assumed" in report
    assert (out / "fill_model-through_limit__side-favorite__size-1" / "fills.parquet").exists()
    log = (runs / "experiment_log.jsonl").read_text().splitlines()
    assert len(log) == 1 and json.loads(log[0])["n_scenarios"] == 2


def test_repeating_a_run_reproduces_it_exactly(dataset, tmp_path):
    first = run_experiment(_config(tmp_path), dataset, tmp_path / "a", log=lambda *_: None)
    second = run_experiment(_config(tmp_path), dataset, tmp_path / "b", log=lambda *_: None)
    read = lambda path: json.loads((path / "manifest.json").read_text())
    assert read(first)["scenario_result_sha256"] == read(second)["scenario_result_sha256"]
    name = "fill_model-through_limit__side-both__size-1"
    assert (first / name / "summary.json").read_text() == (second / name / "summary.json").read_text()


def test_parallel_and_serial_runs_agree(dataset):
    strategy = FixedOffsetLadder(side="both")
    serial = run_backtest(strategy, dataset, BacktestConfig(workers=1))
    parallel = run_backtest(strategy, dataset, BacktestConfig(workers=2))
    assert serial.orders.equals(parallel.orders)
    assert serial.fills.equals(parallel.fills)


def test_holdout_period_is_refused(dataset, tmp_path):
    with pytest.raises(PermissionError):
        run_experiment(_config(tmp_path, period="holdout"), dataset, tmp_path / "runs", log=lambda *_: None)
    with pytest.raises(PermissionError):
        run_backtest(FixedOffsetLadder(), dataset, BacktestConfig(period="holdout"))


def test_dataset_containing_a_holdout_game_fails_closed(dataset):
    store = GameStore(dataset, "2025-26")
    first_holdout = Splits.load().rules["holdout"]["first_ticker_date"]
    year, month, day = first_holdout.split("-")
    months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    ticker = f"KXNBAGAME-{year[2:]}{months[int(month) - 1]}{day}AWYHOM"

    path = store.season_dir / "games.parquet"
    games = pl.read_parquet(path)
    games.with_columns(pl.lit(ticker).alias("game_id")).write_parquet(path)
    with pytest.raises(HoldoutAccessError):
        store.games("development")
    with pytest.raises(HoldoutAccessError):
        store.load({"game_id": ticker})


class FakePuller:
    def __init__(self):
        self.fetched = []

    def summary(self, espn_id):
        self.fetched.append(espn_id)
        return {"plays": [{"wallclock": "2026-01-10T23:10:00Z"}, {"wallclock": "2026-01-11T01:30:00Z"}]}


def _fixture(home_final=100, away_final=90):
    return {
        "espn_id": "e1",
        "scheduled_us": to_us("2026-01-10T23:00Z"),
        "season_type": 2,
        "completed": True,
        "neutral_site": False,
        "home": "HOM",
        "away": "AWY",
        "home_final": home_final,
        "away_final": away_final,
    }


def _events(ticker="KXNBAGAME-26JAN10AWYHOM", home_result="yes", value="1.0000"):
    other = {"yes": "no", "no": "yes"}.get(home_result, home_result)
    other_value = f"{1 - float(value):.4f}"
    return {
        ticker: [
            {"ticker": f"{ticker}-HOM", "result": home_result, "settlement_value_dollars": value},
            {"ticker": f"{ticker}-AWY", "result": other, "settlement_value_dollars": other_value},
        ]
    }


def test_resolve_accepts_a_matching_game():
    plan, reason = _resolve_game(FakePuller(), _fixture(), _events(), Splits.load(), "2025-26")
    assert reason == ""
    assert plan["game_id"] == "KXNBAGAME-26JAN10AWYHOM"
    assert plan["exact_ticker_date"] is True
    # The trade window starts before the two-hour planning time, unlike the Stage 0 spike.
    assert plan["trade_start_us"] < plan["scheduled_us"] - 2 * HOUR


@pytest.mark.parametrize(
    ("fixture", "events", "reason"),
    [
        (_fixture(), {}, "no_kalshi_event"),
        (_fixture(home_final=None), _events(), "not_completed"),
        (_fixture(home_final=80), _events(), "settlement_mismatch"),
    ],
)
def test_resolve_records_why_a_game_was_left_out(fixture, events, reason):
    assert _resolve_game(FakePuller(), fixture, events, Splits.load(), "2025-26") == (None, reason)


def test_resolve_keeps_exceptional_settlements_for_accounting():
    events = _events(home_result="scalar", value="0.5000")
    plan, _ = _resolve_game(FakePuller(), _fixture(), events, Splits.load(), "2025-26")
    assert plan["home_result"] == "scalar" and plan["home_settlement_pips"] == 5000


def test_a_holdout_game_is_refused_before_any_market_data_is_fetched():
    first_holdout = Splits.load().rules["holdout"]["first_ticker_date"]
    year, month, day = first_holdout.split("-")
    months = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]
    ticker = f"KXNBAGAME-{year[2:]}{months[int(month) - 1]}{day}AWYHOM"
    fixture = {**_fixture(), "scheduled_us": to_us(f"{first_holdout}T23:00Z")}
    puller = FakePuller()
    with pytest.raises(HoldoutAccessError):
        _resolve_game(puller, fixture, _events(ticker), Splits.load(), "2026-27")
    assert puller.fetched == []


def test_missing_trade_file_becomes_no_coverage_not_empty_trades(tmp_path):
    root = tmp_path / "data"
    plan = make_game(root, 10, True, 60)
    PullPaths(root).trades(plan["away_market"]).unlink()
    normalize_season(root, "2025-26", [plan], [], [], log=lambda *_: None)
    store = GameStore(root, "2025-26")
    bundle = store.load(store.games("development")[0])
    assert bundle.trade_coverage[plan["away_market"]] is None
    assert bundle.trade_coverage[plan["home_market"]] is not None
    shutil.rmtree(root)
