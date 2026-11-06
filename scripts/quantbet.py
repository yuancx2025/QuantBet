"""Run the QuantBet command line: `uv run python scripts/quantbet.py <pull|coverage|backtest>`.

A launcher rather than an installed console command, because the editable install's .pth file gets
macOS's hidden flag on this machine and Python 3.12 skips hidden .pth files.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quantbet_nba.cli import main

if __name__ == "__main__":
    main()
