"""miniQMT live broker backed by the XtQuant SDK.

This broker routes orders to a real brokerage account through miniQMT
(迅投 miniQMT) using the vendor SDK ``xtquant``. It is the *only* component in
the A-share path that talks to real money, so it is deliberately defensive:

- ``xtquant`` is imported lazily — paper-only installs never pull the SDK.
- Every call that needs the client is guarded by a clear error when miniQMT
  is not running / not connected, rather than raising a raw SDK traceback.
- It maps our neutral ``Order`` model to ``order_stock`` and reads account /
  position state back through the async callback API.

**路径配置**：``qmt_mini_path`` 接受 QMT 安装目录（如
``D:\\国金证券QMT交易端``）或 userdata_mini 的完整路径。传入目录时自动在
目录下查找 userdata_mini 子目录。

Prerequisites (Windows-only):
    1. ``pip install "tradingagents[qmt]"`` (pulls ``xtquant``).
    2. A running miniQMT client with the userdata path passed as
       ``qmt_mini_path`` (e.g. ``D:\\国金证券QMT交易端\\userdata_mini``).
    3. A funded account with QMT permission, supplied as ``qmt_account_id``.

The live broker does not simulate fees or T+1: the venue enforces both and
returns the authoritative state, so we simply mirror it.
"""

from __future__ import annotations

import logging
import os
import threading
import time

from ..dataflows.ashare_symbol_utils import (
    normalize_ashare_symbol,
    parse_ashare_symbol,
    to_vendor_symbol,
)
from .base import BaseBroker
from .models import (
    AccountInfo,
    Order,
    OrderResult,
    OrderSide,
    OrderStatus,
    OrderType,
    Position,
    TradeRecord,
)

logger = logging.getLogger(__name__)

from .path_helper import resolve_qmt_userdata


class _XtQuantNotAvailableError(RuntimeError):
    """Raised when xtquant cannot be imported (not installed / non-Windows)."""


def _import_xtquant():
    """Import the XtQuant SDK and build the callback class lazily.

    Returns ``(xtconstant, XtQuantTrader, QmtCallback, StockAccount)``. The
    callback class subclasses ``XtQuantTraderCallback``, which only exists once
    the SDK is importable, so it is constructed here rather than at module load
    (paper-only installs must be able to import this module without xtquant).
    """
    try:
        from xtquant import xtconstant
        from xtquant.xttrader import XtQuantTrader, XtQuantTraderCallback
        from xtquant.xttype import StockAccount
    except ImportError as exc:
        raise _XtQuantNotAvailableError(
            "xtquant is not installed. Install it with: "
            "pip install \"tradingagents[qmt]\""
        ) from exc

    class QmtCallback(XtQuantTraderCallback):
        """Collects async account / position responses from the QMT trader.

        The XtQuant API is push-based: ``query_stock_asset`` etc. return ``-1`` /
        ``0`` synchronously and deliver the payload later through these callbacks.
        We stash the latest payload and expose a small wait-with-timeout helper so
        the synchronous broker methods can block briefly for the answer.
        """

        def __init__(self):
            self._lock = threading.Lock()
            self._asset = None
            self._positions = None
            self._orders = []
            self._last_error = ""

        def on_stock_asset(self, asset):
            with self._lock:
                self._asset = asset

        def on_stock_position(self, position):
            with self._lock:
                if self._positions is None:
                    self._positions = {}
                self._positions[position.stock_code] = position

        def on_stock_order(self, order):
            with self._lock:
                self._orders.append(order)

        def on_order_stock_async_response(self, response):
            with self._lock:
                self._last_error = ""
                if getattr(response, "error_msg", "") or getattr(response, "status", "") == "FAILED":
                    self._last_error = getattr(response, "error_msg", "order failed")

        def on_disconnected(self):
            logger.warning("QMT trader disconnected")

        def on_connected(self):
            logger.info("QMT trader connected")

    return xtconstant, XtQuantTrader, QmtCallback, StockAccount


class QmtBroker(BaseBroker):
    """Live broker for a miniQMT account."""

    mode = "qmt"

    def __init__(
        self,
        qmt_mini_path: str,
        account_id: str,
        account_type: str = "STOCK",
        session_id: int | None = None,
    ):
        if not qmt_mini_path:
            raise ValueError("qmt_mini_path is required for the QMT broker")

        # qmt_mini_path 可以是安装目录，也可以是 userdata_mini 的完整路径
        resolved = resolve_qmt_userdata(qmt_mini_path)
        if not resolved or not os.path.isdir(resolved):
            raise FileNotFoundError(
                f"找不到 miniQMT userdata_mini：{resolved}\n"
                f"请确认安装目录正确（如 D:\\国金证券QMT交易端）"
            )
        qmt_mini_path = resolved

        (
            self._xtconstant,
            self._XtQuantTrader,
            QmtCallback,
            self._StockAccount,
        ) = _import_xtquant()

        self._account = self._StockAccount(account_id, account_type)
        self._callback = QmtCallback()
        self._trader = self._XtQuantTrader(
            qmt_mini_path, session_id or int(time.time())
        )
        self._trader.register_callback(self._callback)
        self._trader.start()
        self._connected = self._trader.connect() == 0

        if not self._connected:
            logger.warning(
                "QMT connect() did not report success; ensure miniQMT is running "
                "and the userdata path is correct"
            )

    # ── helpers ──

    def _require_connected(self) -> None:
        if not self._connected:
            raise RuntimeError(
                "QMT broker is not connected — start the miniQMT client and "
                "check qmt_mini_path"
            )

    @staticmethod
    def _vendor_symbol(symbol: str) -> str:
        """Convert to the ``600519.SH`` form xtquant expects."""
        return to_vendor_symbol(symbol, vendor="qmt")

    # ── account / positions ──

    def get_account(self) -> AccountInfo:
        self._require_connected()
        self._callback._asset = None
        self._trader.query_stock_asset(self._account)

        asset = self._wait_for(lambda: self._callback._asset, "account asset")
        if asset is None:
            return AccountInfo(currency="CNY")

        # XtQuant asset fields: cash, frozen_cash, market_value, total_asset.
        return AccountInfo(
            total_asset=float(getattr(asset, "total_asset", 0.0) or 0.0),
            available_cash=float(getattr(asset, "cash", 0.0) or 0.0),
            market_value=float(getattr(asset, "market_value", 0.0) or 0.0),
            frozen_cash=float(getattr(asset, "frozen_cash", 0.0) or 0.0),
            currency="CNY",
        )

    def get_positions(self) -> dict[str, Position]:
        self._require_connected()
        self._callback._positions = {}
        self._trader.query_stock_positions(self._account)

        raw = self._wait_for(
            lambda: self._callback._positions if self._callback._positions else None,
            "positions",
            timeout=3.0,
        ) or {}

        positions: dict[str, Position] = {}
        for code, pos in raw.items():
            parsed = parse_ashare_symbol(code) or {}
            bare = parsed.get("code") or code
            volume = int(getattr(pos, "volume", 0) or 0)
            available = int(getattr(pos, "can_use_volume", 0) or 0)
            positions[bare] = Position(
                symbol=bare,
                quantity=volume,
                available=available,
                avg_cost=float(getattr(pos, "open_price", 0.0) or 0.0),
                last_price=float(getattr(pos, "market_value", 0.0) or 0.0) / volume if volume else 0.0,
            )
        return positions

    # ── order entry ──

    def place_order(
        self,
        order: Order,
        last_price: float | None = None,
    ) -> OrderResult:
        self._require_connected()

        symbol = self._vendor_symbol(order.symbol)
        side = order.side
        if side is OrderSide.BUY:
            order_type = self._xtconstant.STOCK_BUY
        else:
            order_type = self._xtconstant.STOCK_SELL

        # A-share venues want a price type; LIMIT -> FIX_PRICE, MARKET -> LATEST.
        if order.order_type is OrderType.MARKET:
            price_type = self._xtconstant.LATEST_PRICE
            price = 0.0
        else:
            if order.price is None:
                return OrderResult(
                    order_id="",
                    status=OrderStatus.REJECTED,
                    message="limit order requires an explicit price",
                    submitted_order=order,
                )
            price_type = self._xtconstant.FIX_PRICE
            price = float(order.price)

        order_id = self._trader.order_stock(
            self._account,
            symbol,
            order_type,
            order.quantity,
            price_type,
            price,
            "TradingAgents",
            order.tag or "",
        )

        if order_id is None or order_id < 0:
            return OrderResult(
                order_id="",
                status=OrderStatus.REJECTED,
                message=f"order_stock returned {order_id!r}: {self._callback._last_error or 'unknown error'}",
                submitted_order=order,
            )

        return OrderResult(
            order_id=str(order_id),
            status=OrderStatus.ACCEPTED,
            filled_quantity=0,
            message="submitted to miniQMT",
            submitted_order=order,
        )

    def cancel_order(self, order_id: str) -> OrderResult:
        self._require_connected()
        result = self._trader.cancel_order_stock(self._account, int(order_id))
        if result == 0:
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.CANCELLED,
                message="cancel submitted",
            )
        return OrderResult(
            order_id=order_id,
            status=OrderStatus.REJECTED,
            message=f"cancel_order_stock returned {result!r}",
        )

    # ── trades / lifecycle ──

    def get_trades(self, symbol: str | None = None) -> list[TradeRecord]:
        self._require_connected()
        self._callback._orders = []
        self._trader.query_stock_orders(self._account, cancelable_only=False)

        raw_orders = self._wait_for(
            lambda: self._callback._orders if self._callback._orders else None,
            "orders",
            timeout=3.0,
        ) or []

        trades: list[TradeRecord] = []
        for o in raw_orders:
            # Only filled orders become trades.
            if getattr(o, "order_status", None) not in (56, "56", "已成", "部成"):
                continue
            traded_volume = int(getattr(o, "traded_volume", 0) or 0)
            if traded_volume <= 0:
                continue
            code = parse_ashare_symbol(getattr(o, "stock_code", "") or "") or {}
            bare = code.get("code") or getattr(o, "stock_code", "")
            if symbol and bare != (normalize_ashare_symbol(symbol) or symbol):
                continue
            side = OrderSide.BUY if getattr(o, "order_type", 0) in (23, 25) else OrderSide.SELL
            trades.append(
                TradeRecord(
                    trade_id=str(getattr(o, "order_id", "")),
                    order_id=str(getattr(o, "order_id", "")),
                    symbol=bare,
                    side=side,
                    quantity=traded_volume,
                    price=float(getattr(o, "traded_price", 0.0) or 0.0),
                    mode=self.mode,
                )
            )
        return trades

    def sync(self) -> None:
        # Live broker is already authoritative on each query; nothing to cache.
        return None

    def next_trading_day(self, today: str | None = None) -> None:
        # T+1 availability comes from the venue (can_use_volume); no local state.
        return None

    def close(self) -> None:
        try:
            self._trader.stop()
        except Exception as exc:  # pragma: no cover - best effort
            logger.debug("QMT trader stop failed: %s", exc)

    # ── internal ──

    def _wait_for(self, predicate, what: str, timeout: float = 3.0, interval: float = 0.05):
        deadline = time.time() + timeout
        while time.time() < deadline:
            value = predicate()
            if value not in (None, ""):
                return value
            time.sleep(interval)
        logger.warning("Timed out waiting for %s from QMT", what)
        return None
