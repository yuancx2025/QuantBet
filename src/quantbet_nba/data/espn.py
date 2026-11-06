"""ESPN schedule and play-by-play, reduced to what the replay needs.

ESPN supplies the scheduled start, regular-season status, and the final play that ends the
timeline. Wallclocks are not in strict play order, so the first and final play follow play
sequence rather than sorted time, and reversals are counted for the coverage report.
"""

from __future__ import annotations

import datetime as dt
import itertools
from typing import Any

BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
REGULAR_SEASON = 2
HOUR_US = 3_600_000_000

# ESPN abbreviation -> Kalshi ticker abbreviation, where they differ.
TO_KALSHI = {"GS": "GSW", "NY": "NYK", "SA": "SAS", "NO": "NOP", "UTAH": "UTA", "WSH": "WAS"}


def kalshi_abbr(espn_abbr: str) -> str:
    return TO_KALSHI.get(espn_abbr, espn_abbr)


def to_us(value: str) -> int:
    """Exact microseconds since the epoch from an ISO timestamp, such as 2026-03-27T23:00Z."""
    parsed = dt.datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.UTC)
    return (parsed - dt.datetime(1970, 1, 1, tzinfo=dt.UTC)) // dt.timedelta(microseconds=1)


def parse_scoreboard(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """One record per scheduled game, with final scores kept only for the settlement check.

    Some entries, such as placeholders, have a team with no abbreviation. They keep an empty name,
    match no Kalshi event, and so appear in the exclusions rather than disappearing.
    """
    games = []
    for event in payload.get("events", []):
        competition = event["competitions"][0]
        teams = {c["homeAway"]: c for c in competition["competitors"]}
        if set(teams) != {"home", "away"}:
            continue
        games.append(
            {
                "espn_id": str(event["id"]),
                "scheduled_us": to_us(event["date"]),
                "season_type": event["season"]["type"],
                "completed": bool(competition["status"]["type"]["completed"]),
                "neutral_site": bool(competition.get("neutralSite", False)),
                "home": kalshi_abbr(teams["home"]["team"].get("abbreviation", "")),
                "away": kalshi_abbr(teams["away"]["team"].get("abbreviation", "")),
                "home_final": _score(teams["home"]),
                "away_final": _score(teams["away"]),
            }
        )
    return games


def _score(team: dict[str, Any]) -> int | None:
    value = team.get("score")
    return None if value in (None, "") else int(value)


def clock_shift_us(stamp_us: int, reference_us: int) -> int:
    """The whole hours by which an ESPN wallclock runs ahead of a trustworthy reference.

    ESPN stamped some 2025-26 plays exactly 24 hours late (Oct 28-30) or 1 hour late (Mar 8, the
    daylight-saving change). In that season, normal games end within 6 minutes of their last trade
    and shifted ones at least 58 minutes after it, so rounding to the hour separates them.
    """
    return max(round((stamp_us - reference_us) / HOUR_US), 0) * HOUR_US


def parse_plays(summary: dict[str, Any]) -> dict[str, Any] | None:
    """First and final play in sequence, or None when there is no usable play-by-play."""
    times = [to_us(p["wallclock"]) for p in summary.get("plays") or [] if p.get("wallclock")]
    if not times:
        return None
    return {
        "first_play_us": times[0],
        "final_play_us": times[-1],
        "n_plays": len(times),
        "wallclock_reversals": sum(b < a for a, b in itertools.pairwise(times)),
        "latest_wallclock_us": max(times),
    }
