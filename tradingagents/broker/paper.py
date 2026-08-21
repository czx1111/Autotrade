"""Paper-trading broker: simulated A-share account with T+1 and real fees.

The paper broker is a faithful stand-in for a live account so the execution
pipeline and scheduler can be exercised end-to-end before any real money is
routed. It keeps an in-memory ledger (cash, positions, trades, orders) and
persists it to JSON so a restart does not lose the account.

Fill model
----------
Orders fill *immediately* at a deterministic price:

- LIMIT orders fill at their limit price (the execution pipeline prices them
  at the last market price, so this matches a marketable limit fill).
- MARKET orders fill at the ``last_price`` passed to :meth:`place_order`.

T+1 is honoured: shares bought today land in ``position.available`` only after
:meth:`next_trading_day` (or automatically when the first trade of a new date
arrives), so a same-day sell of a new buy is rejected at the execution layer.

Fees follow :mod:`tradingagents.broker.fees` — commission (min ¥5), sell-only
stamp tax, and SH transfer fee — and are deducted from cash on every fill.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
import uuid
from datetime import date, datetime

from ..dataflows.ashare_symbol_utils import normalize_ashare_symbol
from .base import BaseBroker
from .fees import calc_fees
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

_DEFAULT_STATE_PATH = os.path.join(
    os.path.expanduser("~"), ".tradingagents", "paper_state.json"
)


def account_state_path(account_name: str) -> str:
    """Per-account paper state file (``paper_state_<账号>.json``).

    多账号必须各持一份状态文件：共用一个文件时，两个账号先后保存会
    互相覆盖对方的持仓/现金。账号名保留中日韩文字（NTFS/ext4 均支持），
    其余文件系统敏感字符替换为 ``_``，防路径逃逸。
    """
    safe = re.sub(r"[^\w-]+", "_", account_name.strip()) or "default"
    base = os.path.dirname(_DEFAULT_STATE_PATH)
    return os.path.join(base, f"paper_state_{safe}.json")


class PaperBroker(BaseBroker):
    """Simulated A-share broker with T+1 tracking and realistic fees."""

    mode = "paper"

    def __init__(
        self,
        initial_capital: float = 1_000_000.0,
        state_path: str | None = None,
        name: str = "paper",
    ):
        self.initial_capital = float(initial_capital)
        self.state_path = state_path or _DEFAULT_STATE_PATH
        self.name = name
        self._lock = threading.RLock()

        self._cash = self.initial_capital
        self._positions: dict[str, Position] = {}
        self._trades: list[TradeRecord] = []
        self._orders: list[OrderResult] = []
        self._next_order_id = 1
        self._last_trade_date = date.today().isoformat()

        self._load()

    # ── persistence ───────────────────────────────────────────────────────

    def _load(self) -> None:
        """Restore state from disk if a previous run saved one."""
        if not os.path.exists(self.state_path):
            return
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read paper state %s: %s", self.state_path, exc)
            return

        self._cash = float(raw.get("cash", self.initial_capital))
        self._next_order_id = int(raw.get("next_order_id", 1))
        self._last_trade_date = raw.get("last_trade_date", date.today().isoformat())

        self._positions = {}
        for sym, pos in raw.get("positions", {}).items():
            self._positions[sym] = Position(**pos)

        self._trades = [TradeRecord(**t) for t in raw.get("trades", [])]
        self._orders = [
            OrderResult(
                status=OrderStatus(o["status"]),
                order_id=o.get("order_id", ""),
                filled_quantity=o.get("filled_quantity", 0),
                avg_fill_price=o.get("avg_fill_price"),
                message=o.get("message", ""),
            )
            for o in raw.get("orders", [])
        ]
        logger.info(
            "Loaded paper account: cash=%.2f, positions=%d, trades=%d",
            self._cash, len(self._positions), len(self._trades),
        )

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        state = {
            "cash": self._cash,
            "next_order_id": self._next_order_id,
            "last_trade_date": self._last_trade_date,
            "initial_capital": self.initial_capital,
            "positions": {
                sym: pos.model_dump(mode="json") for sym, pos in self._positions.items()
            },
            "trades": [t.model_dump(mode="json") for t in self._trades],
            "orders": [
                {
                    "order_id": o.order_id,
                    "status": o.status.value,
                    "filled_quantity": o.filled_quantity,
                    "avg_fill_price": o.avg_fill_price,
                    "message": o.message,
                }
                for o in self._orders
            ],
        }
        tmp = self.state_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, self.state_path)

    # ── account / positions ───────────────────────────────────────────────

    def get_account(self) -> AccountInfo:
        with self._lock:
            market_value = sum(p.market_value for p in self._positions.values())
            return AccountInfo(
                total_asset=round(self._cash + market_value, 2),
                available_cash=round(self._cash, 2),
                market_value=round(market_value, 2),
                frozen_cash=0.0,
                currency="CNY",
            )

    def get_positions(self) -> dict[str, Position]:
        with self._lock:
            return {sym: pos.model_copy() for sym, pos in self._positions.items()}

    def get_trades(self, symbol: str | None = None) -> list[TradeRecord]:
        with self._lock:
            if symbol is None:
                return list(self._trades)
            code = normalize_ashare_symbol(symbol) or symbol
            return [t for t in self._trades if t.symbol == code]

    def get_orders(self) -> list[OrderResult]:
        with self._lock:
            return list(self._orders)

    # ── order entry ───────────────────────────────────────────────────────

    def place_order(
        self,
        order: Order,
        last_price: float | None = None,
    ) -> OrderResult:
        code = normalize_ashare_symbol(order.symbol) or order.symbol

        # Determine the fill price, validating the order shape first.
        if order.order_type is OrderType.MARKET:
            if last_price is None:
                return OrderResult(
                    order_id="",
                    status=OrderStatus.REJECTED,
                    message="market order requires a last_price in paper mode",
                    submitted_order=order,
                )
            fill_price = float(last_price)
        else:  # LIMIT
            if order.price is None:
                return OrderResult(
                    order_id="",
                    status=OrderStatus.REJECTED,
                    message="limit order requires an explicit price",
                    submitted_order=order,
                )
            fill_price = float(order.price)

        if order.quantity <= 0:
            return OrderResult(
                order_id="",
                status=OrderStatus.REJECTED,
                message=f"quantity must be positive, got {order.quantity}",
                submitted_order=order,
            )

        with self._lock:
            self._maybe_rollover()
            order_id = self._mint_order_id()

            if order.side is OrderSide.BUY:
                result = self._execute_buy(code, order, fill_price, order_id)
            else:
                result = self._execute_sell(code, order, fill_price, order_id)

            self._orders.append(result)
            self._save()
            return result

    def _execute_buy(
        self, code: str, order: Order, fill_price: float, order_id: str
    ) -> OrderResult:
        qty = order.quantity
        gross = fill_price * qty
        fees = calc_fees(code, "buy", fill_price, qty)
        total_cost = gross + fees.total

        if total_cost > self._cash:
            affordable = int((self._cash - fees.total) // (fill_price * 100)) * 100
            affordable = max(0, affordable)
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                message=(
                    f"insufficient cash: order needs {total_cost:,.2f} CNY "
                    f"(available {self._cash:,.2f}); max affordable ~{affordable} shares"
                ),
                submitted_order=order,
            )

        # Update / open the position.
        pos = self._positions.get(code)
        if pos is None:
            pos = Position(
                symbol=code,
                name=order.tag or code,
                quantity=0,
                available=0,
                avg_cost=0.0,
                last_price=fill_price,
                buy_date=date.today().isoformat(),
            )
            self._positions[code] = pos

        prev_qty = pos.quantity
        new_qty = prev_qty + qty
        pos.avg_cost = round(
            (pos.avg_cost * prev_qty + gross) / new_qty, 4
        )
        pos.quantity = new_qty
        pos.last_price = fill_price
        # Shares bought today are NOT available until tomorrow (T+1), so
        # ``available`` stays put: a new position opens with 0 sellable, and
        # adding to an existing position leaves its pre-buy availability intact.
        pos.buy_date = date.today().isoformat()

        self._cash -= total_cost

        self._record_trade(code, order, fill_price, qty, fees, order_id)

        return OrderResult(
            order_id=order_id,
            status=OrderStatus.FILLED,
            filled_quantity=qty,
            avg_fill_price=fill_price,
            message=f"bought {qty} @ {fill_price:.2f}; fees {fees.total:.2f}",
            submitted_order=order,
        )

    def _execute_sell(
        self, code: str, order: Order, fill_price: float, order_id: str
    ) -> OrderResult:
        qty = order.quantity
        pos = self._positions.get(code)
        if pos is None or pos.quantity <= 0:
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                message=f"no position in {code} to sell",
                submitted_order=order,
            )
        if qty > pos.available:
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                message=(
                    f"T+1: cannot sell {qty} of {code} today — only "
                    f"{pos.available} shares are sellable (bought {pos.buy_date})"
                ),
                submitted_order=order,
            )

        gross = fill_price * qty
        fees = calc_fees(code, "sell", fill_price, qty)
        proceeds = gross - fees.total

        pos.quantity -= qty
        pos.available -= qty
        pos.last_price = fill_price
        if pos.quantity <= 0:
            del self._positions[code]

        self._cash += proceeds

        self._record_trade(code, order, fill_price, qty, fees, order_id)

        return OrderResult(
            order_id=order_id,
            status=OrderStatus.FILLED,
            filled_quantity=qty,
            avg_fill_price=fill_price,
            message=f"sold {qty} @ {fill_price:.2f}; fees {fees.total:.2f}",
            submitted_order=order,
        )

    def _record_trade(
        self,
        code: str,
        order: Order,
        fill_price: float,
        qty: int,
        fees,
        order_id: str,
    ) -> None:
        self._trades.append(
            TradeRecord(
                trade_id=f"t{order_id}",
                order_id=order_id,
                symbol=code,
                side=order.side,
                quantity=qty,
                price=fill_price,
                commission=fees.commission,
                stamp_tax=fees.stamp_tax,
                transfer_fee=fees.transfer_fee,
                mode=self.mode,
            )
        )

    # ── T+1 / settlement ──────────────────────────────────────────────────

    def _maybe_rollover(self) -> None:
        today = date.today().isoformat()
        if today != self._last_trade_date:
            self._rollover(today)

    def _rollover(self, today: str) -> None:
        for pos in self._positions.values():
            pos.available = pos.quantity  # all prior shares now sellable
        self._last_trade_date = today

    def next_trading_day(self, today: str | None = None) -> None:
        """Advance settlement: mark all held shares sellable (T+1 rollover)."""
        with self._lock:
            self._rollover(today or date.today().isoformat())
            self._save()

    def cancel_order(self, order_id: str) -> OrderResult:
        with self._lock:
            for o in self._orders:
                if o.order_id == order_id and o.status is not OrderStatus.FILLED:
                    o.status = OrderStatus.CANCELLED
                    self._save()
                    return OrderResult(
                        order_id=order_id, status=OrderStatus.CANCELLED,
                        message="cancelled",
                    )
        return OrderResult(
            order_id=order_id,
            status=OrderStatus.REJECTED,
            message="no resting order to cancel (paper broker fills immediately)",
        )

    def sync(self) -> None:
        """Paper broker is its own source of truth; nothing to pull."""

    def close(self) -> None:
        with self._lock:
            self._save()

    # ── internal helpers ──────────────────────────────────────────────────

    def _mint_order_id(self) -> str:
        oid = f"{self.name}-{self._next_order_id:06d}"
        self._next_order_id += 1
        return oid

    @property
    def cash(self) -> float:
        return self._cash
