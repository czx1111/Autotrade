"""持仓策略引擎：止损 / 止盈 / 移动止损 / 均线死叉 / 最大持有天数。

纯逻辑模块（无网络、无 IO）：输入持仓 + 现价 + 可选 K 线，输出卖出信号
列表，由 :mod:`tradingagents.monitor` 的盯盘循环消费并执行。

每账号可通过 accounts.json 的 ``strategy`` 字段覆盖默认参数::

    "strategy": {
        "stop_loss_pct": 0.07,       # 止损 -7%
        "take_profit_pct": 0.15,     # 止盈 +15%
        "trailing_stop_pct": 0.08,   # 移动止损：自持有期最高点回撤 8%
        "ma_cross_exit": false,      # MA5 下穿 MA10 卖出
        "max_hold_days": null        # 最大持有天数（自然日），null 不限
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # 仅类型提示，避免运行时依赖
    import pandas as pd


@dataclass
class StrategyConfig:
    """一个账号的退出策略参数。"""

    stop_loss_pct: float = 0.07
    take_profit_pct: float = 0.15
    trailing_stop_pct: float | None = 0.08
    ma_cross_exit: bool = False
    max_hold_days: int | None = None

    @classmethod
    def from_dict(cls, data: dict | None) -> "StrategyConfig":
        cfg = cls()
        if isinstance(data, dict):
            for key, value in data.items():
                if key in cls.__dataclass_fields__ and value is not None:
                    setattr(cfg, key, value)
        return cfg

    def stop_line(self, cost: float) -> float:
        return cost * (1.0 - self.stop_loss_pct)

    def target_line(self, cost: float) -> float:
        return cost * (1.0 + self.take_profit_pct)


@dataclass
class Signal:
    """一个卖出信号。direction 恒为 "sell"（保护性退出）。"""

    kind: str            # stop_loss | take_profit | trailing_stop | ma_cross | max_hold
    symbol: str
    price: float
    detail: str

    @property
    def direction(self) -> str:
        return "sell"


def _trailing_watermark(kline: "pd.DataFrame", buy_date: str | None) -> float:
    """持有期最高价：有买入日期取买入当日（含）之后的最高 high。

    - 用 ``>=`` 而非 ``>``：持有期含买入当天，否则会漏掉买入日高点
      （水位线被低估、移动止损偏晚）。
    - 买入日期晚于 K 线最后一根（行情未更新到买入日，如当日刚建仓而
      K 线源尚未生成当日 bar）时，退回最后一根的 high，绝不能回退到
      全窗口最高价——否则刚建仓就会拿建仓前的历史高点算回撤，误触发
      移动止损。
    """
    highs = kline["high"]
    if buy_date:
        try:
            holding = kline[kline["date"] >= str(buy_date)]
            if not holding.empty:
                highs = holding["high"]
            else:
                highs = kline["high"].tail(1)
        except Exception:
            pass
    return float(highs.max()) if len(highs) else 0.0


def evaluate_position(
    pos,                       # broker.Position（duck-typed：avg_cost/quantity/symbol/buy_date）
    price: float,
    kline: "pd.DataFrame | None" = None,
    hold_days: int | None = None,
    cfg: StrategyConfig | None = None,
) -> list[Signal]:
    """对单个持仓评估退出策略，返回触发的卖出信号（可能为多个，通常 0-1 个）。

    ``kline`` 需含 date/high/close 列（移动止损与均线叉需要）；``hold_days``
    为自然日持有天数（无则由调用方从 buy_date 计算）。
    """
    cfg = cfg or StrategyConfig()
    signals: list[Signal] = []
    if price <= 0 or pos.quantity <= 0:
        return signals

    cost = pos.avg_cost
    if cost <= 0:                       # 成本缺失（实盘偶发）：跳过成本类检查
        cost = price

    symbol = pos.symbol

    if price <= cfg.stop_line(cost):
        pct_off = (price / cost - 1.0) * 100.0
        signals.append(Signal(
            "stop_loss", symbol, price,
            f"现价 {price:.2f} 已跌破止损线 {cfg.stop_line(cost):.2f}"
            f"（成本 {cost:.2f}，浮亏 {pct_off:+.1f}%）",
        ))

    if price >= cfg.target_line(cost):
        pct_up = (price / cost - 1.0) * 100.0
        signals.append(Signal(
            "take_profit", symbol, price,
            f"现价 {price:.2f} 已达止盈线 {cfg.target_line(cost):.2f}"
            f"（成本 {cost:.2f}，浮盈 {pct_up:+.1f}%）",
        ))

    if cfg.trailing_stop_pct and kline is not None and not kline.empty:
        watermark = _trailing_watermark(kline, getattr(pos, "buy_date", None))
        drawdown_line = watermark * (1.0 - cfg.trailing_stop_pct)
        if watermark > 0 and price <= drawdown_line:
            signals.append(Signal(
                "trailing_stop", symbol, price,
                f"现价 {price:.2f} 自持有期高点 {watermark:.2f} 回撤"
                f" {cfg.trailing_stop_pct:.0%}（触发线 {drawdown_line:.2f}）",
            ))

    if cfg.ma_cross_exit and kline is not None and len(kline) >= 11:
        closes = kline["close"]
        ma5_prev = closes.iloc[-6:-1].mean()    # 上一根的 MA5
        ma10_prev = closes.iloc[-11:-1].mean()  # 上一根的 MA10
        ma5_now = closes.iloc[-5:].mean()
        ma10_now = closes.iloc[-10:].mean()
        if ma5_prev >= ma10_prev and ma5_now < ma10_now:
            signals.append(Signal(
                "ma_cross", symbol, price,
                f"MA5({ma5_now:.2f}) 下穿 MA10({ma10_now:.2f})，短期趋势转弱",
            ))

    if cfg.max_hold_days and hold_days is not None and hold_days >= cfg.max_hold_days:
        signals.append(Signal(
            "max_hold", symbol, price,
            f"已持有 {hold_days} 天 ≥ 上限 {cfg.max_hold_days} 天",
        ))

    return signals
