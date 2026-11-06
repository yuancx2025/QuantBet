"""Run a config's grid of scenarios, save every output, and log the run.

Every run is appended to runs/experiment_log.jsonl, including failures to find anything. The log
is the record of how many things were tried, which is what a later result has to be judged against.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import subprocess
from pathlib import Path
from typing import Any

import polars as pl
import yaml

from quantbet_nba.analytics.metrics import summarize
from quantbet_nba.analytics.report import write_report
from quantbet_nba.config import ROOT, RUNS_DIR, SPIKE_FIXTURES, Splits
from quantbet_nba.data.store import GameStore
from quantbet_nba.engine.backtest import BacktestConfig, BacktestRun, run_backtest
from quantbet_nba.engine.replay import ExecutionConfig
from quantbet_nba.strategies.fixed_offset import FixedOffsetLadder


def expand_scenarios(config: dict[str, Any]) -> list[dict[str, Any]]:
    """Each `scenarios` entry maps a parameter to its values; expand each into combinations."""
    expanded = []
    for block in config["scenarios"]:
        names = sorted(block)
        for values in itertools.product(*(block[name] for name in names)):
            scenario = dict(zip(names, values, strict=True))
            if scenario not in expanded:
                expanded.append(scenario)
    return expanded


def scenario_name(scenario: dict[str, Any]) -> str:
    return "__".join(f"{key}-{scenario[key]}" for key in sorted(scenario))


def build_strategy(config: dict[str, Any], scenario: dict[str, Any]) -> FixedOffsetLadder:
    params = config["strategy"]
    if params["name"] != "fixed_offset":
        raise ValueError(f"unknown strategy {params['name']!r}")
    return FixedOffsetLadder(
        side=scenario["side"],
        offsets_cents=tuple(params["offsets_cents"]),
        size=float(scenario["size"]),
        hours_before=float(params["hours_before"]),
    )


def source_hash() -> str:
    """Identify the code, including uncommitted work, which a commit hash alone would miss."""
    digest = hashlib.sha256()
    for path in sorted((ROOT / "src").rglob("*.py")):
        digest.update(str(path.relative_to(ROOT)).encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _git(*args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def result_hash(run: BacktestRun) -> str:
    """Hash the decisions and fills, so that repeating a run can be checked exactly."""
    digest = hashlib.sha256()
    orders = run.orders.sort("game_id", "order_id") if run.orders.height else run.orders
    fills = run.fills.sort("game_id", "order_id", "ts_us") if run.fills.height else run.fills
    digest.update(orders.write_csv().encode())
    digest.update(fills.write_csv().encode())
    return digest.hexdigest()


def _fixture_ids() -> set[str]:
    if not SPIKE_FIXTURES.exists():
        return set()
    return {game["espn_id"] for game in json.loads(SPIKE_FIXTURES.read_text())}


def run_experiment(
    config_path: Path,
    data_root: Path,
    runs_dir: Path = RUNS_DIR,
    workers: int = 1,
    splits: Splits | None = None,
    log=print,
) -> Path:
    config = yaml.safe_load(config_path.read_text())
    if config["period"] == "holdout":
        raise PermissionError("The holdout is run once, after the strategy is frozen.")
    splits = splits or Splits.load()
    store = GameStore(data_root, config["season"], splits)
    execution = ExecutionConfig(**config.get("execution", {}))
    scenarios = expand_scenarios(config)
    config_hash = hashlib.sha256(json.dumps(config, sort_keys=True).encode()).hexdigest()
    stamp = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    out = runs_dir / f"{config['name']}-{stamp}-{config_hash[:8]}"
    out.mkdir(parents=True)
    fixtures = _fixture_ids()

    summaries = {}
    hashes = {}
    for index, scenario in enumerate(scenarios, start=1):
        name = scenario_name(scenario)
        log(f"[{index}/{len(scenarios)}] {name}")
        run = run_backtest(
            build_strategy(config, scenario),
            data_root,
            BacktestConfig(
                period=config["period"],
                season=config["season"],
                fill_model=scenario["fill_model"],
                execution=execution,
                max_portfolio_reserved=config.get("max_portfolio_reserved"),
                workers=workers,
            ),
            splits,
        )
        directory = out / name
        directory.mkdir()
        run.orders.write_parquet(directory / "orders.parquet")
        run.fills.write_parquet(directory / "fills.parquet")
        run.skips.write_parquet(directory / "skips.parquet")
        run.games.write_parquet(directory / "games.parquet")
        summary = summarize(run, fixtures)
        (directory / "summary.json").write_text(json.dumps(summary, indent=1))
        summaries[name] = {"scenario": scenario, "summary": summary}
        hashes[name] = result_hash(run)

    manifest = {
        "name": config["name"],
        "created_at": dt.datetime.now(dt.UTC).isoformat(),
        "config": config,
        "config_sha256": config_hash,
        "splits": splits.rules,
        "dataset_sha256": store.dataset_hash(),
        "source_sha256": source_hash(),
        "git_commit": _git("rev-parse", "HEAD"),
        "git_dirty": bool(_git("status", "--porcelain")),
        "scenario_result_sha256": hashes,
    }
    (out / "manifest.json").write_text(json.dumps(manifest, indent=1))
    write_report(out / "report.md", config, manifest, summaries)

    runs_dir.mkdir(parents=True, exist_ok=True)
    entry = {
        "created_at": manifest["created_at"],
        "name": config["name"],
        "run_dir": out.name,
        "period": config["period"],
        "n_scenarios": len(scenarios),
        "config_sha256": config_hash,
        "dataset_sha256": manifest["dataset_sha256"],
        "source_sha256": manifest["source_sha256"],
    }
    with (runs_dir / "experiment_log.jsonl").open("a") as handle:
        handle.write(json.dumps(entry) + "\n")
    log(f"wrote {out}")
    return out


def load_frames(directory: Path) -> dict[str, pl.DataFrame]:
    """Read one scenario's saved tables."""
    return {
        name: pl.read_parquet(directory / f"{name}.parquet")
        for name in ("orders", "fills", "skips", "games")
    }
