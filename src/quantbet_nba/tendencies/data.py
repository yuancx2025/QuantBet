"""Strict ESPN scoreboard normalization. Games are the independent sample unit."""

import math
from datetime import datetime
from zoneinfo import ZoneInfo

# ESPN's stable NBA franchise IDs (not All-Star teams). Preserve IDs in exports.
NBA_IDS = frozenset(str(i) for i in range(1, 31))


def eligibility(event):
    """Known non-regular competitions are exclusions, not malformed score data."""
    if event["season"]["type"] != 2:
        return "not regular season"
    competition = event["competitions"][0]
    kind = competition["type"]["id"]
    if kind in {"4", "39"}:
        return "All-Star exhibition" if kind == "4" else "NBA Cup championship (not standings)"
    if kind != "1":
        raise ValueError("unrecognized competition type")
    if not competition["status"]["type"]["completed"]:
        return "not completed"
    return None


def normalize(event, season):
    """Reject uncertain data rather than fill missing quarters with zeros."""
    if reason := eligibility(event):
        raise ValueError(reason)
    if not isinstance(event.get("id"), str) or not event["id"].isdigit():
        raise ValueError("missing/invalid game ID")
    if event["season"]["type"] != 2 or event["season"]["year"] != season:
        raise ValueError("season/type mismatch")
    (competition,) = event["competitions"]
    status = competition["status"]
    if status["type"]["completed"] is not True or status["type"]["state"] != "post":
        raise ValueError("not final")
    periods = status["period"]
    if type(periods) is not int or periods < 4:
        raise ValueError("invalid final period")
    teams = competition["competitors"]
    if len(teams) != 2 or {t["homeAway"] for t in teams} != {"home", "away"}:
        raise ValueError("invalid home/away identities")
    result = []
    for team in sorted(teams, key=lambda t: t["homeAway"]):
        lines = team["linescores"]
        if any(type(p["period"]) is not int for p in lines) or [p["period"] for p in lines] != list(
            range(1, periods + 1)
        ):
            raise ValueError("missing/duplicate/unordered period")
        points = [p["value"] for p in lines]
        if any(
            type(p) not in (int, float) or not math.isfinite(p) or p < 0 or int(p) != p
            for p in points
        ):
            raise ValueError("invalid points")
        if not str(team["score"]).isdigit():
            raise ValueError("invalid final score")
        final = int(team["score"])
        if sum(points) != final:
            raise ValueError("period sum differs from final")
        identity = team["team"]
        if (
            not isinstance(identity["id"], str)
            or identity["id"] not in NBA_IDS
            or team["id"] != identity["id"]
            or not isinstance(identity["displayName"], str)
            or not identity["displayName"].strip()
        ):
            raise ValueError("missing team identity")
        result.append(
            {
                "id": str(identity["id"]),
                "name": identity["displayName"],
                "side": team["homeAway"],
                "points": points,
                "final": final,
            }
        )
    if result[0]["id"] == result[1]["id"] or result[0]["final"] == result[1]["final"]:
        raise ValueError("duplicate identity or tied final")
    for period in range(4, periods):
        if sum(result[0]["points"][:period]) != sum(result[1]["points"][:period]):
            raise ValueError("overtime started without tied preceding score")
    date = datetime.fromisoformat(event["date"])
    if date.tzinfo is None:
        raise ValueError("tipoff timezone missing")
    return {
        "id": str(event["id"]),
        "season": season,
        "date": date.astimezone(ZoneInfo("America/New_York")).date().isoformat(),
        "tipoff": event["date"],
        "teams": result,
    }


def perspectives(game):
    rows = []
    for team, opponent in (game["teams"], game["teams"][::-1]):
        d = [a - b for a, b in zip(team["points"][:4], opponent["points"][:4])]
        c = [sum(d[:q]) for q in range(1, 5)]
        rows.append(
            {
                "game": game["id"],
                "date": game["date"],
                "season": game["season"],
                "team": team["id"],
                "name": team["name"],
                "opponent": opponent["name"],
                "side": team["side"],
                "scored": team["final"],
                "allowed": opponent["final"],
                "final": team["final"] - opponent["final"],
                "d": d,
                "c": c,
                "h1": sum(d[:2]),
                "h2": sum(d[2:]),
                "improvement": sum(d[2:]) - sum(d[:2]),
                "ot": len(team["points"]) - 4,
            }
        )
    return rows
