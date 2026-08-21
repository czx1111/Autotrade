"""Broker abstraction for A-share trading: paper and miniQMT live accounts."""

from __future__ import annotations

from .base import BaseBroker
from .easytrader_broker import EasytraderBroker
from .fees import Fees, calc_fees
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
from .paper import PaperBroker, account_state_path
from .qmt import QmtBroker

__all__ = [
    "BaseBroker",
    "PaperBroker",
    "QmtBroker",
    "EasytraderBroker",
    "get_broker",
    "account_state_path",
    # models
    "AccountInfo",
    "Order",
    "OrderResult",
    "OrderSide",
    "OrderStatus",
    "OrderType",
    "Position",
    "TradeRecord",
    # fees
    "Fees",
    "calc_fees",
]


def get_broker(config: dict | None = None) -> BaseBroker:
    """Build the configured broker.

    Reads ``broker`` ("paper" | "qmt" | "easytrader") and its settings from
    the runtime config (or the account-level dict used by the auto trader):

    - ``paper``       — default; ``paper_initial_capital``; state file per
                        account (``account_name``) or explicit ``state_path``,
                        so multiple paper accounts never share one file
    - ``qmt``         — miniQMT; needs ``qmt_mini_path`` + ``qmt_account_id``
    - ``easytrader``  — 同花顺通用客户端 / 银河网页版; needs
                        ``easytrader_client``, ``easytrader_client_path`` and
                        (yh only) ``easytrader_user`` / ``easytrader_password``
    """
    if config is None:
        from ..dataflows.config import get_config

        config = get_config()

    broker = (config.get("broker") or "paper").lower().strip()
    if broker == "qmt":
        return QmtBroker(
            qmt_mini_path=config.get("qmt_mini_path"),
            account_id=config.get("qmt_account_id", ""),
        )

    if broker in ("easytrader", "easy"):
        return EasytraderBroker(
            client_type=config.get("easytrader_client", "universal"),
            client_path=config.get("easytrader_client_path"),
            user=config.get("easytrader_user"),
            password=config.get("easytrader_password"),
            account_name=config.get("account_name"),
        )

    if broker not in ("paper", ""):
        raise ValueError(
            f"Unknown broker {broker!r}; expected 'paper', 'qmt' or 'easytrader'"
        )

    state_path = config.get("state_path")
    account_name = config.get("account_name") or config.get("name")
    if state_path is None and account_name:
        state_path = account_state_path(str(account_name))
    return PaperBroker(
        initial_capital=float(config.get("paper_initial_capital", 1_000_000.0)),
        state_path=state_path,
        name=str(account_name or "paper"),
    )
