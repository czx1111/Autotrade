"""A-share order validation rules (venue constraints, not preferences).

These encode what the exchange itself enforces — getting them wrong means the
order is rejected (or worse, filled at a ruinous price), so every order passes
through here before reaching any broker:

- T+1: shares bought today cannot be sold until the next trading day.
- Price limits: orders outside the day's limit band are rejected by the venue.
- Lot size: buys must be multiples of 100 shares (科创板 lot is 200).
- ST blacklist: strategy-level, off by default.
- Trading hours: continuous auction 09:30–11:30 / 13:00–15:00, plus the
  09:15–09:25 opening call auction and 14:57–15:00 closing call auction.

The validator is pure (no network): callers supply prev_close and ST status.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, time as dtime

from ..dataflows.ashare_symbol_utils import parse_ashare_symbol, price_limit_for

logger = logging.getLogger(__name__)


@dataclass
class OrderValidation:
    """Result of validating a prospective order."""
    ok: bool
    reason: str = ""
    adjusted_price: float | None = None     # clipped into the limit band
    adjusted_quantity: int | None = None    # rounded down to a valid lot
    violations: list[str] = field(default_factory=list)


# ── trading hours ────────────────────────────────────────────────────────────

# Continuous auction sessions (Asia/Shanghai local time).
_MORNING = (dtime(9, 30), dtime(11, 30))
_AFTERNOON = (dtime(13, 0), dtime(15, 0))

# Call auctions: orders are accepted but execution is batched.
_OPEN_CALL = (dtime(9, 15), dtime(9, 25))
_CLOSE_CALL = (dtime(14, 57), dtime(15, 0))

# Weekday check is the caller's job (holidays need a calendar; treat them as
# trading days and let the broker's rejection surface it, or gate on the
# scheduler which knows the calendar).


def trading_phase(now: datetime | None = None) -> str:
    """Classify ``now`` (Shanghai local) into a trading phase.

    Returns one of: ``pre_market``, ``open_call``, ``morning``, ``lunch_break``,
    ``afternoon``, ``close_call``, ``closed``.
    """
    now = now or datetime.now()
    t = now.time()

    if t < dtime(9, 15):
        return "pre_market"
    if _OPEN_CALL[0] <= t < _OPEN_CALL[1]:
        return "open_call"
    if _MORNING[0] <= t <= _MORNING[1]:
        return "morning"
    if _MORNING[1] < t < _AFTERNOON[0]:
        return "lunch_break"
    if _AFTERNOON[0] <= t < _CLOSE_CALL[0]:
        return "afternoon"
    if _CLOSE_CALL[0] <= t <= _CLOSE_CALL[1]:
        return "close_call"
    return "closed"


def is_trading_time(now: datetime | None = None) -> bool:
    """True during any session where an order can be accepted."""
    return trading_phase(now) in {"open_call", "morning", "afternoon", "close_call"}


# ── the validator ────────────────────────────────────────────────────────────

# 科创板 (688) minimum order is 200 shares and then increments of 1; all other
# boards trade in lots of 100. Sells below one lot are allowed when closing out
# an odd position (rules engines allow 卖出零股), but a strategy never needs to.
_BUY_LOT = {"main": 100, "chinext": 100, "star": 200, "bse": 100}
_STAR_MIN_INCREMENT = 1


class AShareTradingRules:
    """Validates and adjusts orders against A-share venue rules."""

    def __init__(
        self,
        st_blacklist: bool = True,
        price_limit_check: bool = True,
        t1_enforcement: bool = True,
    ):
        self.st_blacklist = st_blacklist
        self.price_limit_check = price_limit_check
        self.t1_enforcement = t1_enforcement

    # ── symbol-level checks ──

    def board_of(self, symbol: str) -> str | None:
        parsed = parse_ashare_symbol(symbol)
        return parsed["board"] if parsed else None

    def is_st(self, symbol: str, name: str | None = None) -> bool:
        """ST status from the security name (ST前缀 / *ST / 退市整理)."""
        if name:
            n = name.strip().upper()
            return n.startswith("ST") or n.startswith("*ST") or n.startswith("退")
        return False

    def check_st_blacklist(self, symbol: str, name: str | None = None) -> bool:
        """True when the symbol is blocked by the ST policy."""
        if not self.st_blacklist:
            return False
        return self.is_st(symbol, name)

    # ── order validation ──

    def validate_order(
        self,
        symbol: str,
        action: str,                # "buy" | "sell"
        price: float,
        quantity: int,
        *,
        prev_close: float | None = None,
        is_st: bool = False,
        name: str | None = None,
    ) -> OrderValidation:
        """Validate one order; return violations plus auto-adjusted values.

        Two distinct lists drive the result:

        - ``violations`` — hard rejections (invalid symbol/action, ST block,
          below minimum lot). Any entry here means ``ok=False``.
        - ``adjustments`` — soft auto-fixes (price clipped into the limit band,
          quantity rounded down to a valid lot). These do NOT fail the order;
          they are surfaced through ``adjusted_price``/``adjusted_quantity``
          and folded into ``reason`` only when nothing harder is reported.
        """
        v: list[str] = []
        adjustments: list[str] = []
        action = action.lower().strip()

        parsed = parse_ashare_symbol(symbol)
        if parsed is None:
            return OrderValidation(False, f"invalid A-share symbol: {symbol!r}")
        board = parsed["board"]

        if action not in ("buy", "sell"):
            return OrderValidation(False, f"invalid action: {action!r} (expected buy/sell)")

        if quantity <= 0:
            return OrderValidation(False, f"quantity must be positive, got {quantity}")

        # 1. ST blacklist (strategy-level, but reject loudly when enabled)
        if self.check_st_blacklist(symbol, name):
            return OrderValidation(
                False,
                f"{symbol} is ST/delisting-risk ({name or 'name unknown'}); blocked by st_blacklist policy",
            )

        # 2. Price-limit band (auto-clip, not a rejection)
        adj_price = price
        if self.price_limit_check and prev_close is not None and prev_close > 0:
            lower, upper = price_limit_for(parsed["code"], prev_close, is_st=is_st)
            if price > upper:
                adj_price = upper
                adjustments.append(f"price {price} above limit-up {upper}; clipped to {upper}")
            elif price < lower:
                adj_price = lower
                adjustments.append(f"price {price} below limit-down {lower}; clipped to {lower}")

        # 3. Lot size (auto-round down for buys, not a rejection)
        adj_qty = quantity
        lot = _BUY_LOT[board]
        if action == "buy":
            if quantity < lot:
                return OrderValidation(
                    False, f"buy quantity {quantity} below minimum lot {lot} for {parsed['board_name']}"
                )
            if board == "star":
                # 688: first 200 shares, then increments of 1
                if (quantity - 200) % _STAR_MIN_INCREMENT != 0:
                    pass  # increments of 1 are always satisfied
            elif quantity % lot != 0:
                adj_qty = (quantity // lot) * lot
                if adj_qty < lot:
                    return OrderValidation(
                        False, f"quantity {quantity} rounds below one lot ({lot})"
                    )
                adjustments.append(
                    f"buy quantity {quantity} not a multiple of {lot}; rounded down to {adj_qty}"
                )

        # 4. T+1 (sell side): caller supplies positions to check separately
        #    (needs buy-date info this signature doesn't carry).

        ok = not v
        reason = "; ".join(v) or ("adjusted: " + "; ".join(adjustments))
        return OrderValidation(
            ok, reason,
            adjusted_price=adj_price if adj_price != price else None,
            adjusted_quantity=adj_qty if adj_qty != quantity else None,
            violations=v,
        )

    # ── T+1 constraint ──

    def check_t1(
        self,
        symbol: str,
        quantity: int,
        positions: dict,   # {symbol: {"quantity": int, "available": int, ...}}
    ) -> OrderValidation:
        """Verify enough T+0-available shares exist to sell.

        ``positions[symbol]["available"]`` must already exclude shares bought
        today; brokers expose this as 可用数量 and the paper broker tracks it.
        """
        pos = positions.get(symbol)
        if pos is None:
            return OrderValidation(False, f"no position in {symbol} to sell")
        available = pos.get("available", pos.get("quantity", 0))
        if quantity > available:
            return OrderValidation(
                False,
                f"T+1: want to sell {quantity} of {symbol} but only {available} shares are sellable today",
            )
        return OrderValidation(True)

    # ── price-limit helpers exposed for UIs ──

    def limit_band(self, symbol: str, prev_close: float, is_st: bool = False) -> tuple[float, float]:
        from ..dataflows.ashare_symbol_utils import price_limit_for as _plf
        code = parse_ashare_symbol(symbol)["code"]
        return _plf(code, prev_close, is_st=is_st)
