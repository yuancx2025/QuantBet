"""Project paths and the split rules in configs/splits.yaml, which is the record of the boundaries.

Any code path that reads or fetches a game goes through `Splits.period_of`. It fails closed: a
holdout game or an unreadable ticker raises rather than loading.
"""

from __future__ import annotations

import datetime as dt
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
SPLITS_FILE = ROOT / "configs" / "splits.yaml"
# Outside the project, like the recorder's output: the Desktop may sync to cloud storage, and a
# full season of raw responses reaches several gigabytes.
DATA_ROOT = Path(os.environ.get("QUANTBET_DATA_DIR", Path.home() / "QuantBetData")) / "history"
RUNS_DIR = ROOT / "runs"
REPORTS_DIR = ROOT / "reports"
SPIKE_FIXTURES = ROOT / "data" / "spike_selected_games.json"

_TICKER = re.compile(r"^KXNBAGAME-(\d{2})([A-Z]{3})(\d{2})")
_MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


class HoldoutAccessError(RuntimeError):
    """Raised when anything other than the frozen final test tries to read a holdout game."""


def ticker_date(event_ticker: str) -> dt.date:
    """The US local date in an event ticker, such as KXNBAGAME-26NOV05MIAGSW -> 2026-11-05."""
    match = _TICKER.match(event_ticker)
    if not match or match[2] not in _MONTHS:
        raise ValueError(f"cannot read a date from ticker {event_ticker!r}")
    return dt.date(2000 + int(match[1]), _MONTHS.index(match[2]) + 1, int(match[3]))


def season_of(date: dt.date) -> str:
    start = date.year if date.month >= 8 else date.year - 1
    return f"{start}-{(start + 1) % 100:02d}"


@dataclass(frozen=True)
class Splits:
    rules: dict[str, Any]

    @classmethod
    def load(cls, path: Path = SPLITS_FILE) -> Splits:
        return cls(yaml.safe_load(path.read_text()))

    def period_of(self, event_ticker: str) -> str:
        """Name the period a game belongs to. Raises for the holdout and for unknown games."""
        date = ticker_date(event_ticker)
        holdout = self.rules["holdout"]
        if date >= dt.date.fromisoformat(holdout["first_ticker_date"]):
            raise HoldoutAccessError(
                f"{event_ticker} is a holdout game (from {holdout['first_ticker_date']}). "
                "The holdout is run once, after the strategy is frozen."
            )
        season = season_of(date)
        for name, rule in self.rules.items():
            if name == "holdout" or rule["season"] != season:
                continue
            last = rule.get("last_ticker_date")
            if last is None or date <= dt.date.fromisoformat(last):
                return name
        raise ValueError(f"{event_ticker} ({date}) is outside every configured period")

    def allows(self, event_ticker: str, period: str) -> bool:
        return self.period_of(event_ticker) == period
