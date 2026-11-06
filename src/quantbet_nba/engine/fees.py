"""Fee scenarios. Kalshi's historical NBA maker rate is unverified, so nothing here is a fact.

Testing treats limit orders as free, so headline results are gross and labelled as such
(docs/decisions/0004-data-scope-and-tuning-games.md). The series reports
`quadratic_with_maker_fees`, and 0.0175 comes from secondary sources, so reports also show what
each scenario would cost. Confirm the rate before presenting any result as net of costs.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QuadraticFee:
    """fee = rate * contracts * P * (1 - P), with no rounding (per-fill rounding is rebated)."""

    rate: float = 0.0

    def fee(self, price: float, qty: float) -> float:
        return self.rate * qty * price * (1.0 - price)


FEE_SCENARIOS: dict[str, float] = {
    "gross": 0.0,
    "maker_0.0175": 0.0175,
    "double_0.035": 0.035,
}
