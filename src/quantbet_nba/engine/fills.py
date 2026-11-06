"""Historical execution assumptions for a resting buy order.

There is no historical order book, so a fill is always "filled under this rule". Every rule
fills at the order's own limit: a resting maker receives its price, not the print's price.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import ClassVar


@dataclass(frozen=True)
class FillModel:
    name: ClassVar[str]
    # When True, each print's quantity is a shared budget across this strategy's orders.
    limited_by_trade_qty: ClassVar[bool] = False

    def crosses(self, limit_pips: int, trade_pips: int) -> bool:
        raise NotImplementedError


@dataclass(frozen=True)
class TouchAtLimit(FillModel):
    """Optimistic: a print exactly at the limit fills us, as if we were first in the queue."""

    name: ClassVar[str] = "touch_at_limit"

    def crosses(self, limit_pips: int, trade_pips: int) -> bool:
        return trade_pips <= limit_pips


@dataclass(frozen=True)
class ThroughLimit(FillModel):
    """A print strictly below the limit fills the whole order.

    Price priority means any bid at the limit had to clear first. Reasonable at one contract,
    but it ignores available size, so use ThroughWithVolume to judge larger orders.
    """

    name: ClassVar[str] = "through_limit"

    def crosses(self, limit_pips: int, trade_pips: int) -> bool:
        return trade_pips < limit_pips


@dataclass(frozen=True)
class ThroughWithVolume(FillModel):
    """Only volume printed strictly below the limit can fill us, so partial fills can occur.

    Rungs in one market share each print's quantity, highest limit first. This is a capacity
    estimate; prints do not reconstruct the queue or our own market impact.
    """

    name: ClassVar[str] = "through_with_volume"
    limited_by_trade_qty: ClassVar[bool] = True

    def crosses(self, limit_pips: int, trade_pips: int) -> bool:
        return trade_pips < limit_pips


FILL_MODELS: dict[str, type[FillModel]] = {
    cls.name: cls for cls in (TouchAtLimit, ThroughLimit, ThroughWithVolume)
}
