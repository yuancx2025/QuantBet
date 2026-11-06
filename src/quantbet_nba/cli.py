"""Commands: pull, coverage, backtest. Run through scripts/quantbet.py."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from quantbet_nba.config import DATA_ROOT, REPORTS_DIR, RUNS_DIR


def _pull(args: argparse.Namespace) -> None:
    from quantbet_nba.data.pull import pull_season

    if args.limit and args.data_root == DATA_ROOT:
        raise SystemExit("--limit writes a small dataset, so it also needs its own --data-root")
    pull_season(args.data_root, args.season, limit=args.limit, workers=args.workers)


def _coverage(args: argparse.Namespace) -> None:
    from quantbet_nba.data.coverage import build_coverage, coverage_markdown
    from quantbet_nba.data.store import GameStore

    report = build_coverage(GameStore(args.data_root, args.season), args.period)
    out = args.out / "coverage"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{args.season}-{args.period}.json").write_text(json.dumps(report, indent=1))
    path = out / f"{args.season}-{args.period}.md"
    path.write_text(coverage_markdown(report))
    print(coverage_markdown(report))
    print(f"wrote {path}")


def _backtest(args: argparse.Namespace) -> None:
    from quantbet_nba.analytics.experiment import run_experiment

    run_experiment(args.config, args.data_root, args.runs_dir, workers=args.workers)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="quantbet", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    def common(sub: argparse.ArgumentParser) -> None:
        sub.add_argument("--data-root", type=Path, default=DATA_ROOT)

    pull = commands.add_parser("pull", help="download a season and build the replay dataset")
    common(pull)
    pull.add_argument("--season", default="2025-26")
    pull.add_argument("--limit", type=int, help="only this many evenly spaced games")
    pull.add_argument("--workers", type=int, default=6)
    pull.set_defaults(run=_pull)

    coverage = commands.add_parser("coverage", help="report what the dataset covers")
    common(coverage)
    coverage.add_argument("--season", default="2025-26")
    coverage.add_argument("--period", default="development")
    coverage.add_argument("--out", type=Path, default=REPORTS_DIR)
    coverage.set_defaults(run=_coverage)

    backtest = commands.add_parser("backtest", help="run a strategy config's scenarios")
    common(backtest)
    backtest.add_argument("config", type=Path)
    backtest.add_argument("--runs-dir", type=Path, default=RUNS_DIR)
    backtest.add_argument("--workers", type=int, default=6)
    backtest.set_defaults(run=_backtest)
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.run(args)


if __name__ == "__main__":
    main()
