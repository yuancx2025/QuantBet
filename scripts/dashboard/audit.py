"""Audit generated data against strict score and independent-game invariants."""

import argparse
import json
from collections import Counter
from pathlib import Path

from quantbet_nba.tendencies.build import atomic_json
from quantbet_nba.tendencies.data import perspectives


def audit(payload):
    seen = set()
    seasons = []
    for manifest in payload["coverage"]:
        games = [g for g in payload["games"] if g["season"] == manifest["season"]]
        teams = Counter()
        overtime = 0
        for game in games:
            key = game["id"]
            if key in seen:
                raise ValueError(f"Duplicate independent game: {key}")
            seen.add(key)
            for team in game["teams"]:
                if len(team["points"]) < 4 or sum(team["points"]) != team["final"]:
                    raise ValueError(f"Invalid score sum: {key}")
                teams[team["id"]] += 1
            a, b = perspectives(game)
            if any(a[k] != -b[k] for k in ("final", "h1", "h2", "improvement")):
                raise ValueError(f"Not antisymmetric: {key}")
            if a["c"] != [-v for v in b["c"]] or a["d"] != [-v for v in b["d"]]:
                raise ValueError(f"Quarter signs inconsistent: {key}")
            if a["h1"] + a["h2"] != a["c"][3]:
                raise ValueError(f"Halves inconsistent: {key}")
            overtime += a["ot"] > 0
        seasons.append(
            {
                "season": manifest["season"],
                "games": len(games),
                "team_game_counts": dict(sorted(teams.items())),
                "overtime_games": overtime,
                "covered_dates": len(manifest["covered_dates"]),
                "failed_dates": manifest["failed_dates"],
                "exclusion_counts": dict(Counter(e["reason"] for e in manifest["exclusions"])),
                "partial": manifest["partial"],
            }
        )
    return {
        "fixture": payload["fixture"],
        "dataset_sha256": payload["sha256"],
        "refreshed": payload["refreshed"],
        "seasons": seasons,
        "checks": "Unique IDs, period sums, opposing signs and halves verified for included games. "
        "Daily provider coverage, not an independent schedule reconciliation.",
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = audit(json.loads(args.manifest.read_text()))
    if args.output:
        atomic_json(args.output, report)
    for season in report["seasons"]:
        counts = season["team_game_counts"].values()
        print(
            f"{season['season'] - 1}–{str(season['season'])[2:]}: {season['games']} games, "
            f"{len(counts)} teams, appearances {min(counts, default=0)}–{max(counts, default=0)}, "
            f"OT {season['overtime_games']}, failed dates {len(season['failed_dates'])}, "
            f"partial={season['partial']}"
        )


if __name__ == "__main__":
    main()
