"""挂单看护：成交确认与超时撤单。

easytrader / QMT 通道返回 ACCEPTED 只表示柜台已受理，不代表成交——
止损限价单可能因价格快速脱离而长期挂着无人过问。本模块把每笔已受理
的挂单登记到 ``<state_dir>/open_orders/<account>.json``（跨重启持久），
由每轮盯盘/盘中巡检对账：

- 当日成交回报里出现该委托号 → 记为已成交，移出看护；
- 挂单时长超过 ``timeout_min``（默认 15 分钟）仍未成交 → 自动撤单，
  并由调用方推送告警；撤单失败则保留继续重试并升级告警；
- 隔日挂单直接作废（A 股收盘后柜台自动撤单）。

不自动重挂：重挂价格需要新的决策依据，交给下一轮分析或人工处理。
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from .broker import BaseBroker, OrderStatus

logger = logging.getLogger(__name__)


@dataclass
class TrackedOrder:
    """一笔已受理、等待成交确认的挂单。"""

    order_id: str
    symbol: str
    action: str          # buy | sell
    quantity: int
    price: float
    tag: str = ""        # 来源标签（auto:buy / monitor:stop_loss / ...）
    placed_at: str = ""  # "%Y-%m-%d %H:%M:%S"


class OpenOrderTracker:
    """单账号挂单看护器：登记 → 对账（成交确认 / 超时撤单）。"""

    def __init__(
        self,
        account_name: str,
        broker: BaseBroker,
        path: str | Path | None = None,
        timeout_min: float = 15.0,
        now_fn=datetime.now,
    ):
        self.account = account_name
        self.broker = broker
        self.timeout_min = max(1.0, float(timeout_min))
        self._now_fn = now_fn
        if path is None:
            from .default_config import DEFAULT_CONFIG

            base = Path(DEFAULT_CONFIG.get("results_dir", ".")) / "open_orders"
            path = base / f"{account_name}.json"
        self.path = Path(path)

    # ── 登记 ──

    def track(
        self,
        order_id: str,
        symbol: str,
        action: str,
        quantity: int,
        price: float,
        tag: str = "",
    ) -> None:
        """executor 受理挂单后登记（幂等；无委托号不登记）。"""
        if not order_id:
            return
        orders = self._load()
        if any(o.order_id == order_id for o in orders):
            return
        orders.append(TrackedOrder(
            order_id=order_id, symbol=symbol, action=action,
            quantity=int(quantity), price=float(price or 0.0), tag=tag,
            placed_at=self._now_fn().strftime("%Y-%m-%d %H:%M:%S"),
        ))
        self._save(orders)
        logger.info(
            "[%s] tracking open order %s: %s %s x%d @%.2f",
            self.account, order_id, action, symbol, quantity, float(price or 0.0),
        )

    def pending(self) -> list[TrackedOrder]:
        """当前看护中的挂单（UI / 测试用）。"""
        return self._load()

    # ── 对账 ──

    def reconcile(self) -> list[dict]:
        """对账一次。

        返回事件列表，每项为纯字典（可直接序列化）：
        ``{"kind": filled|cancelled|cancel_failed|expired, ...}``。
        成交回报查询失败时不动看护列表（下轮再对）。
        """
        now = self._now_fn()
        today = now.strftime("%Y-%m-%d")
        orders = self._load()
        if not orders:
            return []

        try:
            trades = self.broker.get_trades()
        except Exception as exc:
            logger.warning(
                "[%s] open-order reconcile: get_trades failed: %s", self.account, exc,
            )
            return []
        filled_ids = {t.order_id for t in trades if t.order_id}

        events: list[dict] = []
        live: list[TrackedOrder] = []
        for o in orders:
            if not o.placed_at.startswith(today):
                # 隔日挂单：收盘后柜台已自动作废，无需撤单。
                events.append(_event(o, "expired"))
                continue
            if o.order_id in filled_ids:
                events.append(_event(o, "filled"))
                continue
            if (now - _parse_dt(o.placed_at)).total_seconds() / 60.0 < self.timeout_min:
                live.append(o)
                continue

            try:
                result = self.broker.cancel_order(o.order_id)
            except Exception as exc:
                logger.exception(
                    "[%s] cancel open order %s failed: %s", self.account, o.order_id, exc,
                )
                events.append(_event(o, "cancel_failed", error=str(exc)))
                live.append(o)
                continue
            if result.status is OrderStatus.CANCELLED:
                events.append(_event(o, "cancelled"))
            else:
                events.append(_event(o, "cancel_failed", error=result.message))
                live.append(o)

        self._save(live)
        return events

    # ── 持久化 ──

    def _load(self) -> list[TrackedOrder]:
        if not self.path.exists():
            return []
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            return [TrackedOrder(**item) for item in raw if isinstance(item, dict)]
        except (json.JSONDecodeError, OSError, TypeError, ValueError):
            logger.warning("open-order store %s unreadable — starting fresh", self.path)
            return []

    def _save(self, orders: list[TrackedOrder]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                json.dumps([asdict(o) for o in orders], ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except OSError:
            logger.warning("open-order store write failed", exc_info=True)


def _event(o: TrackedOrder, kind: str, error: str = "") -> dict:
    data = asdict(o)
    data["kind"] = kind
    if error:
        data["error"] = error[:200]
    return data


def _parse_dt(s: str) -> datetime:
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        # 时间戳损坏时不触发超时撤单（保守：继续看护），只按 0 龄处理。
        return datetime.now()
