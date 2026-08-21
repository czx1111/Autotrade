"""Data models shared by the broker abstraction and execution pipeline."""

from __future__ import annotations

import enum
from datetime import datetime

from pydantic import BaseModel, Field


class OrderSide(str, enum.Enum):
    BUY = "buy"
    SELL = "sell"


class OrderType(str, enum.Enum):
    LIMIT = "limit"          # 限价单 (A-share default; market orders are 限价=对手价 in practice)
    MARKET = "market"        # 市价单 (SZ only, and not for all boards)


class OrderStatus(str, enum.Enum):
    PENDING = "pending"      # submitted, not yet acknowledged
    ACCEPTED = "accepted"    # broker acknowledged, waiting to fill
    PARTIAL = "partial"      # partially filled
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class Order(BaseModel):
    """One order as the strategy intends it (pre-validation)."""
    symbol: str                       # bare code "600519" or suffixed
    side: OrderSide
    quantity: int                     # shares (not lots)
    price: float | None = None        # None + LIMIT -> reject; explicit price required on A-share
    order_type: OrderType = OrderType.LIMIT
    created_at: datetime = Field(default_factory=datetime.now)
    tag: str = ""                     # provenance: which decision produced this


class OrderResult(BaseModel):
    """Broker response for one submitted order."""
    order_id: str = ""                # broker-assigned id (paper broker mints one)
    status: OrderStatus
    filled_quantity: int = 0
    avg_fill_price: float | None = None
    message: str = ""
    submitted_order: Order | None = None


class Position(BaseModel):
    """One open position. ``available`` is shares not encumbered by T+1."""
    symbol: str
    name: str = ""
    quantity: int = 0
    available: int = 0                # sellable today (T+1 aware)
    avg_cost: float = 0.0             # per-share, CNY
    last_price: float = 0.0
    buy_date: str | None = None       # ISO date of the position open (T+1 tracking)

    @property
    def market_value(self) -> float:
        return self.quantity * self.last_price

    @property
    def unrealized_pnl(self) -> float:
        return (self.last_price - self.avg_cost) * self.quantity


class AccountInfo(BaseModel):
    """Account snapshot."""
    total_asset: float = 0.0          # 总资产
    available_cash: float = 0.0       # 可用资金
    market_value: float = 0.0         # 持仓市值
    frozen_cash: float = 0.0          # 冻结资金
    currency: str = "CNY"
    updated_at: datetime = Field(default_factory=datetime.now)


class TradeRecord(BaseModel):
    """A fill that actually happened (paper or live)."""
    trade_id: str = ""
    order_id: str = ""
    symbol: str
    name: str = ""
    side: OrderSide
    quantity: int
    price: float
    commission: float = 0.0           # 佣金 (双边)
    stamp_tax: float = 0.0            # 印花税 (卖出 0.05%)
    transfer_fee: float = 0.0         # 过户费 (沪市 0.001%)
    traded_at: datetime = Field(default_factory=datetime.now)
    mode: str = "paper"               # paper | qmt
