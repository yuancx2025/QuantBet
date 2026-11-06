"""Write a run's report. Assumptions come first, because they limit what any number here means."""

from __future__ import annotations

from pathlib import Path
from typing import Any

CAVEATS = [
    ("**Gross of fees.** Limit orders are treated as free for testing (decision 0004). Kalshi lists "
    "this series as charging maker fees, and the rate is unverified. These results are not net."),
    ("**Hypothetical fills.** There is no historical order book, so a fill means the stated rule "
    "was met by public prints. It doesn't reconstruct queue position or market impact."),
    ("**Order survival is assumed.** Whether a resting order survives tipoff is unconfirmed. If "
    "orders are cancelled at tipoff, the in-game fills reported here could not have happened."),
    "**Schedule is retrospective.** Planning time uses a schedule downloaded after the games.",
    ("**Tuning results.** These use development data and are not a holdout test. A positive number "
    "here is not evidence of an edge on its own; check the interval and the number of runs logged."),
]


def _cents(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:+.2f}¢"


def _pct(value: float | None) -> str:
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _interval(value: list[float] | None) -> str:
    if value is None:
        return "n/a (too few games)"
    return f"{value[0] * 100:+.2f}¢ to {value[1] * 100:+.2f}¢"


def _row(label: str, stats: dict[str, Any]) -> str:
    return (
        f"| {label} | {stats['games_with_fills']} | {stats['contracts_filled']:,.0f} | "
        f"{_pct(stats['fill_rate'])} | {_pct(stats['win_rate_when_filled'])} | "
        f"{_pct(stats['average_fill_price'])} | {_cents(stats['gross_per_filled'])} | "
        f"{_interval(stats['ci95_gross_per_filled'])} | ${stats['gross_pnl']:,.2f} |"
    )


HEADER = (
    "| {} | Games filled | Contracts filled | Fill rate | Win rate if filled | Avg price | "
    "Gross per filled | 95% CI (whole games) | Gross P&L |\n"
    "|---|---:|---:|---:|---:|---:|---:|---|---:|"
)


def write_report(
    path: Path,
    config: dict[str, Any],
    manifest: dict[str, Any],
    summaries: dict[str, dict[str, Any]],
) -> None:
    lines = [
        f"# Backtest: {config['name']}",
        "",
        (f"Period `{config['period']}`, {len(summaries)} scenarios. "
        f"Dataset `{manifest['dataset_sha256'][:12]}`, source `{manifest['source_sha256'][:12]}`, "
        f"commit `{manifest['git_commit'][:8] or 'none'}`"
        f"{' (uncommitted changes)' if manifest['git_dirty'] else ''}."),
        "",
        "## Read this first",
        "",
    ]
    lines += [f"- {caveat}" for caveat in CAVEATS]
    lines += ["", "## All scenarios", "", HEADER.format("Scenario")]
    lines += [_row(name.replace("__", " · "), item["summary"]) for name, item in summaries.items()]

    primary = config.get("primary_scenario")
    for name, item in summaries.items():
        if primary and item["scenario"] != primary:
            continue
        summary = item["summary"]
        lines += [
            "",
            f"## Detail: {name.replace('__', ' · ')}",
            "",
            (f"{summary['games_in_period']} games in period; {summary['games_with_orders']} had "
            f"orders. Reporting floor of 200 filled orders "
            f"{'met' if summary['reporting_floor_met'] else '**not met**'} "
            "(meeting it proves nothing by itself)."),
            (f"Order status: {summary['order_status']}. Rejections: {summary['rejection_reasons']}. "
            f"Skips: {summary['skip_reasons']}."),
            (f"Maximum drawdown {summary['max_drawdown_gross']:,.2f} dollars; peak reserved "
            f"{summary['peak_reserved_dollars']:,.2f} dollars, assuming resting orders reserve funds."),
            "",
            "**If the unverified fee scenarios applied**",
            "",
            "| Scenario | Rate | Total fees | P&L after fees | Per filled |",
            "|---|---:|---:|---:|---:|",
        ]
        for fee_name, fee in summary["fee_sensitivity"].items():
            lines.append(
                f"| {fee_name} | {fee['fee_rate']} | ${fee['total_fees']:,.2f} | "
                f"${fee['pnl_after_fees']:,.2f} | {_cents(fee['per_filled_after_fees'])} |"
            )
        for key, rows in summary["breakdowns"].items():
            title = key.removeprefix("label_").replace("_", " ")
            lines += ["", f"**By {title}**", "", HEADER.format(title.capitalize())]
            lines += [_row(value, stats) for value, stats in rows.items()]
        if "fill_kinds" in summary:
            lines += [
                "",
                "**Sweep or drift** (diagnostic; uses prices after the fill)",
                "",
                "| Kind | Games | Contracts | Gross per filled | Gross P&L |",
                "|---|---:|---:|---:|---:|",
            ]
            for kind, row in summary["fill_kinds"].items():
                lines.append(
                    f"| {kind} | {row['games']} | {row['contracts_filled']:,.0f} | "
                    f"{_cents(row['gross_per_filled'])} | ${row['gross_pnl']:,.2f} |"
                )
        if summary.get("fill_phases"):
            lines += [
                "",
                ("**Before or after scheduled tipoff** (if orders don't survive tipoff, the later "
                "row could not have happened)"),
                "",
                "| Phase | Games | Contracts | Gross per filled | Gross P&L |",
                "|---|---:|---:|---:|---:|",
            ]
            for phase, row in summary["fill_phases"].items():
                lines.append(
                    f"| {phase.replace('_', ' ')} | {row['games']} | "
                    f"{row['contracts_filled']:,.0f} | {_cents(row['gross_per_filled'])} | "
                    f"${row['gross_pnl']:,.2f} |"
                )
        if "excluding_stage0_fixtures" in summary:
            stats = summary["excluding_stage0_fixtures"]
            lines += [
                "",
                (f"**Without the {stats['fixture_games_removed']} Stage 0 fixture games**, which "
                "informed the hypotheses:"),
                "",
                HEADER.format("Sample"),
                _row("Excluding fixtures", stats),
            ]
    path.write_text("\n".join(lines) + "\n")
