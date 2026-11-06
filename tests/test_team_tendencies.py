"""Deterministic contract and failure-path tests; never fit predictive models."""

import copy
import json
import re
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import httpx

from quantbet_nba.tendencies.build import collect, fetch, publish, save_refresh
from quantbet_nba.tendencies.data import normalize, perspectives


def event(a=None, b=None, identity="123"):
    a = a or [20, 30, 25, 25]
    b = b or [25, 20, 30, 20]
    return {
        "id": identity,
        "date": "2025-10-22T02:00Z",
        "season": {"type": 2, "year": 2026},
        "competitions": [
            {
                "type": {"id": "1"},
                "status": {"type": {"completed": True, "state": "post"}, "period": len(a)},
                "competitors": [
                    {
                        "id": identity,
                        "team": {"id": identity, "displayName": name},
                        "homeAway": side,
                        "score": str(sum(points)),
                        "linescores": [{"period": i + 1, "value": p} for i, p in enumerate(points)],
                    }
                    for identity, name, side, points in (
                        ("13", "Los Angeles Lakers", "home", a),
                        ("2", "Boston Celtics", "away", b),
                    )
                ],
            }
        ],
    }


def envelope(events):
    return {
        "payload": {"events": events, "leagues": [{"id": "46"}]},
        "sha256": "fixture-response-hash",
        "fetched_at": "2026-01-01T00:00Z",
    }


class Scores(unittest.TestCase):
    def test_quarter_cumulative_halves_and_signs(self):
        home, away = perspectives(normalize(event(), 2026))[::-1]
        self.assertEqual(home["d"], [-5, 10, -5, 5])
        self.assertEqual(home["c"], [-5, 5, 0, 5])
        self.assertEqual((home["h1"], home["h2"], home["improvement"]), (5, 0, -5))
        self.assertEqual(home["date"], "2025-10-21")  # ET, not UTC calendar day
        for key in ("final", "h1", "h2", "improvement"):
            self.assertEqual(home[key], -away[key])
        self.assertEqual(home["d"], [-v for v in away["d"]])
        self.assertEqual(home["c"], [-v for v in away["c"]])
        self.assertEqual(home["scored"], away["allowed"])
        self.assertEqual(home["side"], "home")
        self.assertEqual(away["side"], "away")

    def test_overtime_is_final_only(self):
        home = perspectives(
            normalize(event([20, 20, 30, 30, 10, 8], [25, 25, 25, 25, 10, 6]), 2026)
        )[1]
        self.assertEqual(home["d"], [-5, -5, 5, 5])
        self.assertEqual(home["h2"], 10)
        self.assertEqual(home["c"][3], 0)
        self.assertEqual(home["final"], 2)
        self.assertEqual(home["improvement"], 20)
        self.assertEqual(home["ot"], 2)

    def test_bad_periods_and_scores_rejected(self):
        for mutate in (
            lambda c: c["competitors"][0]["linescores"].pop(1),
            lambda c: c["competitors"][0]["linescores"][1].update(period=1),
            lambda c: c["competitors"][0]["linescores"][1].update(value=None),
            lambda c: c["competitors"][0]["linescores"][1].update(value=float("nan")),
            lambda c: c["competitors"][0]["linescores"][1].update(value=1.5),
            lambda c: c["competitors"][0].update(score="200"),
            lambda c: c["status"].update(period=5),
            lambda c: c["status"]["type"].update(completed=False),
            lambda c: c["status"]["type"].update(state="in"),
            lambda c: c["competitors"][0].update(id="999"),
            lambda c: c["competitors"][0]["team"].update(id="2"),
            lambda c: c["competitors"][0].update(homeAway="away"),
        ):
            with self.subTest(mutation=mutate):
                item = event()
                mutate(item["competitions"][0])
                with self.assertRaises((KeyError, ValueError, TypeError)):
                    normalize(item, 2026)

    def test_invalid_overtime_and_naive_date(self):
        with self.assertRaisesRegex(ValueError, "overtime"):
            normalize(event([20, 30, 25, 25, 5], [25, 20, 30, 20, 6]), 2026)
        item = event()
        item["date"] = "2025-10-22T02:00"
        with self.assertRaisesRegex(ValueError, "timezone"):
            normalize(item, 2026)

    def test_exhibition_championship_unknown_type_and_identity(self):
        for kind, message in (("4", "All-Star"), ("39", "Cup"), ("999", "unrecognized")):
            item = event()
            item["competitions"][0]["type"]["id"] = kind
            with self.assertRaisesRegex(ValueError, message):
                normalize(item, 2026)
        item = event()
        competitor = item["competitions"][0]["competitors"][0]
        competitor["id"] = competitor["team"]["id"] = "132374"
        with self.assertRaisesRegex(ValueError, "identity"):
            normalize(item, 2026)

    def test_season_type_and_year(self):
        for season in ({"type": 3, "year": 2026}, {"type": 2, "year": 2027}):
            item = event()
            item["season"] = season
            with self.assertRaisesRegex(ValueError, "season"):
                normalize(item, 2026)


class Refresh(unittest.TestCase):
    def test_duplicate_ids_quarantined(self):
        item = event()
        with (
            tempfile.TemporaryDirectory() as folder,
            patch(
                "quantbet_nba.tendencies.build.fetch",
                return_value=envelope([item, copy.deepcopy(item)]),
            ),
        ):
            games, manifest = collect(date(2025, 10, 21), date(2025, 10, 21), 2026, Path(folder))
        self.assertEqual(games, [])
        self.assertTrue(manifest["partial"])
        self.assertIn("duplicate", manifest["exclusions"][0]["reason"])

    def test_failed_dates_and_exclusions_explicit(self):
        invalid = event(identity="124")
        invalid["competitions"][0]["competitors"][0]["linescores"].pop()
        ineligible = event(identity="125")
        ineligible["season"]["type"] = 1
        with (
            tempfile.TemporaryDirectory() as folder,
            patch(
                "quantbet_nba.tendencies.build.fetch",
                side_effect=[
                    envelope([event(), invalid, ineligible]),
                    httpx.ConnectError("offline"),
                ],
            ),
        ):
            games, manifest = collect(date(2025, 10, 21), date(2025, 10, 22), 2026, Path(folder))
        self.assertEqual(len(games), 1)
        self.assertEqual(manifest["covered_dates"], ["2025-10-21"])
        self.assertEqual(manifest["failed_dates"][0]["date"], "2025-10-22")
        self.assertEqual([e["kind"] for e in manifest["exclusions"]], ["invalid", "ineligible"])
        self.assertTrue(manifest["partial"])

    def test_ineligible_is_not_missing_coverage(self):
        preseason = event()
        preseason["season"]["type"] = 1
        with (
            tempfile.TemporaryDirectory() as folder,
            patch("quantbet_nba.tendencies.build.fetch", return_value=envelope([preseason])),
        ):
            games, manifest = collect(date(2025, 10, 21), date(2025, 10, 21), 2026, Path(folder))
        self.assertFalse(manifest["partial"])
        self.assertEqual(games, [])
        self.assertEqual(len(manifest["exclusions"]), 1)

    def test_built_html_embeds_valid_safe_json_and_assets(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "dashboard"
            game = normalize(event(), 2026)
            game["teams"][0]["name"] = "</script><script>malicious()</script>"
            publish([game], [], output)
            html = (output / "index.html").read_text()
            match = re.search(
                r'<script id="dataset" type="application/json">(.*?)</script>', html, re.DOTALL
            )
            self.assertIsNotNone(match)
            embedded = json.loads(match[1])
            self.assertEqual(embedded["games"], [game])
            self.assertEqual(len(embedded["rows"]), 2)
            self.assertNotIn("<script>malicious", html)
            self.assertNotIn("__QUANTBET_DATA__", html)
            self.assertTrue((output / "app.js").exists())
            self.assertTrue((output / "style.css").exists())

    def test_refresh_never_overwrites_good_output(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "dashboard"
            publish([normalize(event(), 2026)], [], output)
            original = {p.name: p.read_bytes() for p in output.iterdir()}
            with self.assertRaisesRegex(SystemExit, "preserved"):
                save_refresh(
                    [], [{"partial": True, "failed_dates": [{"date": "2025-10-21"}]}], output
                )
            self.assertEqual(original, {p.name: p.read_bytes() for p in output.iterdir()})
            self.assertTrue((output.parent / "dashboard-refresh-attempt.json").exists())

    def test_first_failed_refresh_has_error_dashboard(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "dashboard"
            with self.assertRaisesRegex(SystemExit, "Partial/error"):
                save_refresh([], [{"season": 2026, "partial": True}], output)
            payload = json.loads((output / "manifest.json").read_text())
            self.assertEqual(payload["rows"], [])
            self.assertTrue(payload["coverage"][0]["partial"])

    def test_cache_is_separate_and_failed_refresh_preserves_response(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder)
            good = {"events": [event()], "leagues": [{"id": "46"}]}
            transport = httpx.MockTransport(lambda request: httpx.Response(200, json=good))
            with httpx.Client(transport=transport) as client:
                first = fetch(client, "2025-10-21", cache, True)
            with (
                httpx.Client(
                    transport=httpx.MockTransport(lambda request: httpx.Response(503))
                ) as client,
                patch("quantbet_nba.tendencies.build.time.sleep"),
            ):
                with self.assertRaises(httpx.HTTPStatusError):
                    fetch(client, "2025-10-21", cache, True)
                cached = fetch(client, "2025-10-21", cache, False)
            self.assertEqual(first, cached)
            self.assertIn("fetched_at", cached)
            self.assertIn("source", cached)
            self.assertTrue((cache / "2025-10-21.json").exists())

    def test_publish_failure_and_unowned_directory_preserved(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "dashboard"
            publish([normalize(event(), 2026)], [], output)
            before = (output / "index.html").read_bytes()
            with (
                patch(
                    "quantbet_nba.tendencies.build.shutil.copyfile",
                    side_effect=OSError("disk full"),
                ),
                self.assertRaises(OSError),
            ):
                publish([], [], output)
            self.assertEqual(before, (output / "index.html").read_bytes())
            unrelated = Path(folder) / "unrelated"
            unrelated.mkdir()
            (unrelated / "notes.txt").write_text("keep")
            with self.assertRaisesRegex(ValueError, "not an owned"):
                publish([], [], unrelated)
            self.assertEqual((unrelated / "notes.txt").read_text(), "keep")

    def test_bad_payload_not_cached(self):
        with (
            tempfile.TemporaryDirectory() as folder,
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda request: httpx.Response(200, json={"error": "oops"})
                )
            ) as client,
            patch("quantbet_nba.tendencies.build.time.sleep"),
        ):
            with self.assertRaises((ValueError, TypeError)):
                fetch(client, "2025-10-21", Path(folder), True)
            self.assertEqual(list(Path(folder).iterdir()), [])


if __name__ == "__main__":
    unittest.main()
