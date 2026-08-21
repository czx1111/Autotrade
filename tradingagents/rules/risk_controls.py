"""Portfolio-level risk controls (strategy constraints, not venue rules).

Unlike trading_rules.py (which encodes what the exchange enforces), these are
the guardrails the account owner sets for the strategy itself:

- single-position cap: no stock may exceed N% of total portfolio value
- daily loss cap: stop opening new positions after the day's loss exceeds N%
- order budget: at most N orders per day (also keeps us below the exchange's
  programmatic-trading reporting thresholds for high-frequency patterns)
- concentration: max positions in one sector
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date

logger = logging.getLogger(__name__)


@dataclass
class RiskDecision:
    """Result of a risk check."""
    ok: bool
    reason: str = ""
    details: dict = field(default_factory=dict)


class RiskController:
    """Stateful per-day risk gatekeeper. Recreate or reset daily."""

    def __init__(
        self,
        max_single_position_pct: float = 0.20,
        max_daily_loss_pct: float = 0.03,
        max_orders_per_day: int = 10,
        max_sector_concentration_pct: float = 0.40,
    ):
        self.max_single_position_pct = max_single_position_pct
        self.max_daily_loss_pct = max_daily_loss_pct
        self.max_orders_per_day = max_orders_per_day
        self.max_sector_concentration_pct = max_sector_concentration_pct
        self._order_dates: dict[str, int] = {}    # date-str -> count

    # ── per-day counters ──

    def _today_key(self) -> str:
        return date.today().isoformat()

    def orders_today(self) -> int:
        return self._order_dates.get(self._today_key(), 0)

    def record_order(self) -> None:
        key = self._today_key()
        self._order_dates[key] = self._order_dates.get(key, 0) + 1
        # Keep only today's counter — old days expire naturally.
        self._order_dates = {k: v for k, v in self._order_dates.items() if k == key}

    # ── checks (each returns a decision; caller aggregates) ──

    def check_position_limit(
        self,
        symbol: str,
        order_value: float,
        total_portfolio_value: float,
        current_position_value: float = 0.0,
    ) -> RiskDecision:
        """New single-position value after this order must stay under the cap."""
        cap = total_portfolio_value * self.max_single_position_pct
        new_value = current_position_value + order_value
        if new_value > cap:
            return RiskDecision(
                False,
                f"{symbol} position would reach {new_value:,.0f} CNY "
                f"({new_value / total_portfolio_value:.1%}) exceeding the "
                f"{self.max_single_position_pct:.0%} single-position cap ({cap:,.0f} CNY)",
                details={"cap": cap, "would_be": new_value},
            )
        return RiskDecision(True, details={"cap": cap, "would_be": new_value})

    def check_daily_loss_limit(
        self,
        day_start_equity: float,
        current_equity: float,
    ) -> RiskDecision:
        """Block new buys once the day's drawdown breaches the limit."""
        if day_start_equity <= 0:
            return RiskDecision(True)
        loss_pct = (day_start_equity - current_equity) / day_start_equity
        if loss_pct >= self.max_daily_loss_pct:
            return RiskDecision(
                False,
                f"daily loss {loss_pct:.1%} has hit the {self.max_daily_loss_pct:.0%} limit; "
                f"no new positions today",
                details={"loss_pct": loss_pct},
            )
        return RiskDecision(True, details={"loss_pct": loss_pct})

    def check_order_budget(self) -> RiskDecision:
        used = self.orders_today()
        if used >= self.max_orders_per_day:
            return RiskDecision(
                False,
                f"order budget exhausted ({used}/{self.max_orders_per_day} today); "
                f"staying under exchange programmatic-trading thresholds",
            )
        return RiskDecision(True, details={"used": used, "limit": self.max_orders_per_day})

    def check_cash_reserve(
        self,
        order_value: float,
        available_cash: float,
        reserve_pct: float = 0.05,
    ) -> RiskDecision:
        """Keep a small cash buffer so fees never bounce an order."""
        if order_value > available_cash * (1 - reserve_pct):
            return RiskDecision(
                False,
                f"order {order_value:,.0f} CNY leaves less than the "
                f"{reserve_pct:.0%} cash reserve "
                f"(available {available_cash:,.0f})",
            )
        return RiskDecision(True)

    def check_concentration(
        self,
        sector_of_symbol: str,
        order_value: float,
        positions_by_sector: dict,   # {sector: value_cny}
        total_portfolio_value: float,
    ) -> RiskDecision:
        """Cap exposure to any one industry sector."""
        sector_value = positions_by_sector.get(sector_of_symbol, 0.0) + order_value
        pct = sector_value / total_portfolio_value if total_portfolio_value > 0 else 1.0
        if pct > self.max_sector_concentration_pct:
            return RiskDecision(
                False,
                f"sector '{sector_of_symbol}' would hold {pct:.1%} of the portfolio "
                f"(cap {self.max_sector_concentration_pct:.0%})",
                details={"sector": sector_of_symbol, "pct": pct},
            )
        return RiskDecision(True, details={"sector": sector_of_symbol, "pct": pct})
