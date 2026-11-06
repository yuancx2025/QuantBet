"""Core records shared by the engine, data adapters, and strategies.

Prices are stored as integer pips (1/10,000 of a dollar) so that "strictly below the limit"
is an exact comparison. Timestamps are integer microseconds since the Unix epoch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

PIPS_PER_DOLLAR = 10_000
TICK_PIPS = 100  # KXNBAGAME markets trade in whole cents ("linear_cent").
US_PER_SECOND = 1_000_000


def dollars_to_pips(price: float) -> int:
    """Convert dollars to pips, absorbing binary float noise such as 0.165 * 10000."""
    return round(price * PIPS_PER_DOLLAR)


def pips_to_dollars(pips: int) -> float:
    return pips / PIPS_PER_DOLLAR


def floor_to_tick(pips: int) -> int:
    """Round a buy limit down, so rounding never makes a bid more aggressive."""
    return pips - pips % TICK_PIPS


@dataclass(frozen=True, slots=True)
class Trade:
    """One public print, visible to the strategy at `ts_us`."""

    ts_us: int
    market: str
    price_pips: int
    qty: float
    trade_id: str = ""


@dataclass(frozen=True, slots=True)
class Quote:
    """A candle's closing quote. Candles are end-stamped: visible only at `ts_us`, the end."""

    ts_us: int
    market: str
    bid_pips: int | None
    ask_pips: int | None
    volume: float = 0.0

    @property
    def two_sided(self) -> bool:
        """A $0 bid or $1 ask means nobody is on that side of the book."""
        return (
            bool(self.bid_pips) and self.ask_pips is not None and self.ask_pips < PIPS_PER_DOLLAR
        )

    @property
    def bid(self) -> float | None:
        return None if self.bid_pips is None else pips_to_dollars(self.bid_pips)

    @property
    def ask(self) -> float | None:
        return None if self.ask_pips is None else pips_to_dollars(self.ask_pips)

    @property
    def mid(self) -> float | None:
        if not self.two_sided:
            return None
        return pips_to_dollars(self.bid_pips + self.ask_pips) / 2


@dataclass(frozen=True, slots=True)
class MarketInfo:
    ticker: str
    team: str
    is_home: bool


@dataclass(frozen=True, slots=True)
class GameInfo:
    """Everything a strategy may know about a game. Contains no result by construction.

    `scheduled_start_us` comes from a schedule downloaded after the game, so it may reflect
    revisions that were not known at planning time. Reports disclose this.
    """

    game_id: str
    scheduled_start_us: int
    home: str
    away: str
    markets: tuple[MarketInfo, ...]

    def market_for(self, *, home: bool) -> MarketInfo:
        return next(m for m in self.markets if m.is_home is home)


@dataclass(frozen=True, slots=True)
class MarketOutcome:
    """How a market settled. Hidden from strategies; used only for accounting."""

    ticker: str
    result: str  # "yes", "no", or an exceptional value such as "scalar"
    settlement_pips: int

    @property
    def binary(self) -> bool:
        return self.result in ("yes", "no")


@dataclass(slots=True)
class GameBundle:
    """One game's replay input. `trade_coverage` maps market -> complete window, or None."""

    info: GameInfo
    start_us: int
    end_us: int  # final play: nothing after this exists for the replay
    quotes: list[Quote]
    trades: list[Trade]
    outcomes: dict[str, MarketOutcome]
    trade_coverage: dict[str, tuple[int, int] | None]


class OrderStatus(StrEnum):
    FILLED = "filled"
    PARTIAL = "partial"
    UNFILLED = "unfilled"
    CANCELLED = "cancelled"
    UNKNOWN = "unknown"  # missing trade data: could not determine, never assumed unfilled
    REJECTED = "rejected"
    OPEN = "open"


@dataclass(slots=True)
class Order:
    order_id: int
    game_id: str
    market: str
    limit_pips: int
    size: float
    placed_us: int
    active_us: int
    labels: dict[str, str] = field(default_factory=dict)
    quote_bid_pips: int | None = None
    quote_ask_pips: int | None = None
    pre_volume_1h: float = 0.0
    filled: float = 0.0
    status: OrderStatus = OrderStatus.OPEN
    reason: str = ""
    cancel_us: int | None = None

    @property
    def remaining(self) -> float:
        return self.size - self.filled


@dataclass(frozen=True, slots=True)
class Fill:
    order_id: int
    game_id: str
    market: str
    ts_us: int
    qty: float
    price_pips: int


@dataclass(frozen=True, slots=True)
class Skip:
    """A decision not to submit, logged so that eligibility losses stay visible."""

    game_id: str
    market: str
    ts_us: int
    reason: str


@dataclass(slots=True)
class GameResult:
    info: GameInfo
    end_us: int
    orders: list[Order]
    fills: list[Fill]
    skips: list[Skip]
    outcomes: dict[str, MarketOutcome]
