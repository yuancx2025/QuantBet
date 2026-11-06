"""Refresh separate bounded caches and build a self-contained offline dashboard."""

import argparse
import hashlib
import json
import shutil
import tempfile
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx

from .data import eligibility, normalize, perspectives

ROOT = Path(__file__).resolve().parents[3]
SOURCE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard"


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, indent=2, allow_nan=False)
    temporary.replace(path)


def validate_payload(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list):
        raise TypeError("missing events array")
    if not any(league.get("id") == "46" for league in payload.get("leagues", [])):
        raise ValueError("NBA league identity missing")
    if len(payload["events"]) >= 1000:
        raise ValueError("response at limit: completeness uncertain")


def fetch(client, day, cache, refresh):
    path = cache / f"{day}.json"
    if path.exists() and not refresh:
        envelope = json.loads(path.read_text())
        validate_payload(envelope["payload"])
        return envelope
    params = {"dates": day.replace("-", ""), "limit": 1000}
    for attempt in range(3):
        try:
            response = client.get(SOURCE, params=params)
            response.raise_for_status()
            payload = response.json()
            validate_payload(payload)
            envelope = {
                "source": SOURCE,
                "params": params,
                "fetched_at": datetime.now(UTC).isoformat(),
                "payload": payload,
                "sha256": hashlib.sha256(response.content).hexdigest(),
            }
            atomic_json(path, envelope)
            return envelope
        except (httpx.HTTPError, ValueError, TypeError):
            if attempt == 2:
                raise
            time.sleep(0.25 * (attempt + 1))


def collect(start, end, season, cache, refresh=False):
    games, excluded, failures, covered, seen, responses = {}, [], [], [], set(), []
    with httpx.Client(timeout=httpx.Timeout(15, connect=5)) as client:
        day = start
        while day <= end:
            key = day.isoformat()
            try:
                envelope = fetch(client, key, cache, refresh)
                covered.append(key)
                responses.append({k: v for k, v in envelope.items() if k != "payload"})
                for event in envelope["payload"]["events"]:
                    identity = str(event.get("id", "")) if isinstance(event, dict) else ""
                    try:
                        if not identity or identity in seen:
                            games.pop(identity, None)  # quarantine ambiguous IDs entirely
                            raise ValueError("missing/duplicate game ID")
                        seen.add(identity)
                        if reason := eligibility(event):
                            excluded.append(
                                {
                                    "id": identity,
                                    "date": key,
                                    "kind": "ineligible",
                                    "reason": reason,
                                }
                            )
                            continue
                        game = normalize(event, season)
                        if not start.isoformat() <= game["date"] <= end.isoformat():
                            raise ValueError("outside local-date interval")
                        game["provenance"] = {
                            "scoreboard_date": key,
                            "source": SOURCE,
                            "response_sha256": envelope["sha256"],
                            "fetched_at": envelope["fetched_at"],
                        }
                        games[identity] = game
                    except (KeyError, ValueError, TypeError, IndexError, OverflowError) as error:
                        excluded.append(
                            {
                                "id": identity,
                                "date": key,
                                "kind": "invalid",
                                "reason": f"{type(error).__name__}: {error}",
                            }
                        )
            except (httpx.HTTPError, ValueError, OSError, KeyError, TypeError) as error:
                failures.append({"date": key, "reason": f"{type(error).__name__}: {error}"})
            day += timedelta(days=1)
    return list(games.values()), {
        "season": season,
        "start": str(start),
        "as_of": str(end),
        "covered_dates": covered,
        "failed_dates": failures,
        "responses": responses,
        "exclusions": excluded,
        "partial": bool(failures or any(e["kind"] == "invalid" for e in excluded)),
        "accepted_games": len(games),
        "coverage_definition": "Every daily scoreboard requested; not an independent schedule audit. "
        "Ineligible games excluded; invalid records or failed dates mean partial.",
    }


def fixture():
    """Invented deterministic scores for software testing only; never season statistics."""
    games = []
    for season in (2026, 2027):
        for i in range(12):
            a = [24 + i % 5, 29 - i % 3, 20 + i % 7, 27 + i % 4]
            b = [28 - i % 4, 23 + i % 6, 29 - i % 5, 22 + i % 3]
            if sum(a) == sum(b):
                a[3] += 1
            games.append(
                {
                    "id": f"fixture-{season}-{i}",
                    "season": season,
                    "date": f"{season - 1}-10-{i + 20:02}",
                    "teams": [
                        {
                            "id": "13",
                            "name": "Los Angeles Lakers",
                            "side": "home" if i % 2 else "away",
                            "points": a,
                            "final": sum(a),
                        },
                        {
                            "id": "2",
                            "name": "Boston Celtics",
                            "side": "away" if i % 2 else "home",
                            "points": b,
                            "final": sum(b),
                        },
                    ],
                }
            )
    return games


def publish(games, manifests, output, demo=False):
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        manifest = output / "manifest.json"
        if (
            not manifest.exists()
            or json.loads(manifest.read_text()).get("application") != "quantbet-team-tendencies-v1"
        ):
            raise ValueError("Output is not an owned dashboard directory; choose a new empty path")
    payload = {
        "application": "quantbet-team-tendencies-v1",
        "rows": [r for g in games for r in perspectives(g)],
        "games": games,
        "coverage": manifests,
        "source": SOURCE,
        "refreshed": datetime.now(UTC).isoformat(),
        "fixture": demo,
        "exposure": {
            "purpose": "fixture software verification"
            if demo
            else "current-season exploratory analysis",
            "fixture": demo,
            "existing_exclusion": "KXNBAGAME-26NOV05MIAGSW",
            "game_ids": [g["id"] for g in games if g["season"] == 2027],
            "as_of": [m["as_of"] for m in manifests if m["season"] == 2027],
            "warning": "Explored games no longer untouched; replacement final test undecided.",
        },
    }
    payload["source_sha256"] = hashlib.sha256(
        b"".join(
            (ROOT / path).read_bytes()
            for path in (
                "src/quantbet_nba/tendencies/build.py",
                "src/quantbet_nba/tendencies/data.py",
                "dashboard/index.html",
                "dashboard/app.js",
                "dashboard/style.css",
            )
        )
    ).hexdigest()
    payload["sha256"] = hashlib.sha256(json.dumps(games, sort_keys=True).encode()).hexdigest()
    template = (ROOT / "dashboard/index.html").read_text()
    marker = '"__QUANTBET_DATA__"'
    if template.count(marker) != 1:
        raise ValueError("Dashboard template must contain exactly one dataset marker")
    html = template.replace(marker, json.dumps(payload).replace("<", "\\u003c"))
    # Stage all assets before swapping the directory. Restore the previous version on failure.
    # Output and backup must be on the same filesystem. Refresh is single-writer.
    staging = Path(tempfile.mkdtemp(prefix=".team-tendencies-", dir=output.parent))
    backup = output.with_name(output.name + ".previous")
    try:
        (staging / "index.html").write_text(html)
        for asset in ("app.js", "style.css"):
            shutil.copyfile(ROOT / "dashboard" / asset, staging / asset)
        atomic_json(staging / "manifest.json", payload)
        if backup.exists():
            raise FileExistsError(f"Recovery backup exists; inspect before refresh: {backup}")
        output.rename(backup)
        try:
            staging.rename(output)
        except OSError:
            backup.rename(output)
            raise
        shutil.rmtree(backup)
    finally:
        if staging.exists():
            shutil.rmtree(staging)


def save_refresh(games, manifests, output):
    """A partial refresh never replaces an existing usable build."""
    output.mkdir(parents=True, exist_ok=True)
    atomic_json(output.parent / (output.name + "-refresh-attempt.json"), manifests)
    if any(m["partial"] for m in manifests) and (output / "index.html").exists():
        raise SystemExit(
            "Refresh incomplete; preserved dashboard. See sibling refresh-attempt JSON"
        )
    publish(games, manifests, output)
    if any(m["partial"] for m in manifests):
        raise SystemExit("Partial/error dashboard built; see coverage details")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", type=date.fromisoformat, required=True)
    parser.add_argument("--development-as-of", type=date.fromisoformat, default=date(2026, 4, 12))
    parser.add_argument("--cache", type=Path, default=ROOT / "data/team-tendencies/cache")
    parser.add_argument(
        "--output",
        type=Path,
        help="Dedicated generated directory (must be empty or dashboard-owned)",
    )
    parser.add_argument("--refresh", action="store_true")
    parser.add_argument("--fixture", action="store_true")
    args = parser.parse_args()
    if not date(2026, 10, 1) <= args.as_of <= date(2027, 6, 30):
        parser.error("--as-of must be within 2026-27 season")
    if not date(2025, 10, 1) <= args.development_as_of <= date(2026, 6, 30):
        parser.error("--development-as-of must be within 2025-26 season")
    if args.output is None:
        args.output = ROOT / (
            "reports/team-tendencies-fixture" if args.fixture else "reports/team-tendencies"
        )
    if args.fixture:
        games = [
            g
            for g in fixture()
            if g["date"] <= str(args.as_of if g["season"] == 2027 else args.development_as_of)
        ]
        publish(
            games,
            [
                {
                    "season": s,
                    "as_of": str(args.as_of if s == 2027 else args.development_as_of),
                    "partial": True,
                    "accepted_games": sum(g["season"] == s for g in games),
                    "failed_dates": [],
                    "exclusions": [],
                    "note": "DETERMINISTIC FIXTURE — NOT REAL NBA DATA",
                }
                for s in (2026, 2027)
            ],
            args.output,
            True,
        )
        return
    games, manifests = [], []
    for season, end in ((2026, args.development_as_of), (2027, args.as_of)):
        found, manifest = collect(date(season - 1, 10, 1), end, season, args.cache, args.refresh)
        games.extend(found)
        manifests.append(manifest)
    save_refresh(games, manifests, args.output)


if __name__ == "__main__":
    main()
