"""Abstract broker interface.

The broker abstraction is what lets the execution pipeline talk to a paper
account today and a live miniQMT account tomorrow without changing its logic.
Every broker — simulated or real — exposes the same five capabilities:

- ``get_account``     — cash, market value, frozen funds
- ``get_positions``   — open positions keyed by symbol (T+1 aware)
- ``place_order``     — submit an order, return an ``OrderResult``
- ``cancel_order``    — attempt to cancel a resting order
- ``get_trades``      — filled trades (audit trail)

Brokers are deliberately *dumb* about strategy: they know nothing about venue
rules or risk limits — those live in ``tradingagents.rules`` and the execution
pipeline. A broker just moves money and shares faithfully (and, for the paper
broker, charges realistic fees).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .models import AccountInfo, Order, OrderResult, Position, TradeRecord


class BaseBroker(ABC):
    """Common interface for paper and live A-share brokers."""

    mode: str = "base"

    @abstractmethod
    def get_account(self) -> AccountInfo:
        """Return a snapshot of the account (cash, market value, frozen funds)."""

    @abstractmethod
    def get_positions(self) -> dict[str, Position]:
        """Return open positions keyed by symbol (bare six-digit code)."""

    @abstractmethod
    def place_order(
        self,
        order: Order,
        last_price: float | None = None,
    ) -> OrderResult:
        """Submit an order.

        ``last_price`` is the current market price, used by brokers that fill
        market orders or need a reference price to simulate a limit fill. The
        paper broker requires it for market orders; the QMT broker ignores it.
        """

    @abstractmethod
    def cancel_order(self, order_id: str) -> OrderResult:
        """Cancel a resting order by id."""

    @abstractmethod
    def get_trades(self, symbol: str | None = None) -> list[TradeRecord]:
        """Return filled trades, optionally filtered to one symbol."""

    # ── optional lifecycle hooks ──

    def health_check(self) -> bool:
        """进程/会话守护钩子（默认恒健康）。

        live broker 可实现为：检测交易客户端进程掉线 → 自动拉起并重连。
        run_auto 守护循环在交易日 08:45–15:30 每分钟调用一次；时段外
        不守护（用户可随时手动关闭客户端而不会被反复拉起）。
        """
        return True

    def sync(self) -> None:
        """Refresh local state from the source of truth (live brokers only).

        Paper brokers keep their own state and no-op here; a live broker pulls
        the latest account/positions from the venue.
        """

    def next_trading_day(self, today: str | None = None) -> None:
        """Advance settlement state to a new trading day (T+1 rollover).

        Called by the scheduler at market close so shares bought today become
        sellable tomorrow. Paper brokers implement this; live brokers derive
        availability from the venue and no-op.
        """

    def close(self) -> None:
        """Release any resources (sessions, file handles)."""
