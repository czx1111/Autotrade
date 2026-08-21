"""A-share transaction cost model (per-trade fees).

A-share retail trading costs are three-line, two of which differ by side:

    fee            rate                    side        notes
    ------------   ----------------------  ---------   ------------------------
    佣金 commission  0.025% (万2.5), min ¥5   buy + sell  negotiable per account
    印花税 stamp tax  0.05% (万5)             sell only   halved 2023-08-28 (was 0.1%)
    过户费 transfer   0.001% (万0.1)          buy + sell  SH-market only in this model

The commission floor (¥5) and the sell-only stamp tax mean a round-trip's cost
is not symmetric, so fees are always computed per-fill, never per-order-pair.
Transfer fee historically applied to Shanghai shares only; SZSE abolished it in
2015 and BSE is ignored here, so the SH/SZ split is decided by the security's
board via :func:`tradingagents.dataflows.ashare_symbol_utils.parse_ashare_symbol`.

All amounts are CNY and rounded to 2 decimal places.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..dataflows.ashare_symbol_utils import parse_ashare_symbol

# Fee constants (rates as decimals; override per-account if your broker differs).
COMMISSION_RATE = 0.00025   # 万2.5
COMMISSION_MIN = 5.0        # ¥5 per side minimum
STAMP_TAX_RATE = 0.0005     # 万5, sell only
TRANSFER_FEE_RATE = 0.00001  # 万0.1, SH both sides


@dataclass(frozen=True)
class Fees:
    """Itemised fees for a single fill, all in CNY."""

    commission: float = 0.0
    stamp_tax: float = 0.0
    transfer_fee: float = 0.0

    @property
    def total(self) -> float:
        return round(self.commission + self.stamp_tax + self.transfer_fee, 2)


def is_sh_market(symbol: str) -> bool:
    """True when the symbol trades on the Shanghai Stock Exchange."""
    parsed = parse_ashare_symbol(symbol)
    return bool(parsed and parsed["exchange"] == "SH")


def calc_fees(
    symbol: str,
    side: str,
    price: float,
    quantity: int,
) -> Fees:
    """Compute the fees for one fill.

    ``side`` is ``"buy"`` or ``"sell"`` (case-insensitive). ``price`` is the
    per-share fill price in CNY and ``quantity`` the number of shares filled.
    """
    side = side.lower().strip()
    if side not in ("buy", "sell"):
        raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")

    turnover = float(price) * int(quantity)

    commission = max(COMMISSION_MIN, turnover * COMMISSION_RATE)

    stamp_tax = turnover * STAMP_TAX_RATE if side == "sell" else 0.0

    # SH transfer fee both sides; SZ/BJ zero in this model.
    transfer_fee = turnover * TRANSFER_FEE_RATE if is_sh_market(symbol) else 0.0

    return Fees(
        commission=round(commission, 2),
        stamp_tax=round(stamp_tax, 2),
        transfer_fee=round(transfer_fee, 2),
    )
