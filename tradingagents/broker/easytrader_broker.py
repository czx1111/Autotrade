"""easytrader live broker — 实盘通道，无需券商量化权限。

对没有开通 miniQMT 权限的普通散户账号（如平安证券、银河证券），本模块通过
``easytrader`` 驱动券商交易通道下单：

- ``universal``: 同花顺通用客户端（xiadan.exe），先手工登录同花顺并绑定
  券商账号（平安/银河均可），本模块通过客户端自动化下单；
- ``yh``: 银河证券网页交易通道（easytrader 内置 ``yh_client``）。

**路径配置**：``client_path`` 接受同花顺安装目录或 xiadan.exe 的完整路径。
传入目录时自动在目录下查找 xiadan.exe，支持手动指定或 UI 自动检测。

easytrader 属于 UI/网页自动化，稳定性弱于 miniQMT：券商或同花顺升级可能
导致接口失效，因此所有字段读取都做了防御式处理，失败时抛出带指引的
RuntimeError 而不是裸 traceback。

**进程守护**（universal 模式）：所有客户端调用经 ``_invoke`` 包装——
调用失败且 xiadan.exe 进程已死时，自动拉起进程（xiadan 独立保存登录
会话）→ 重连 → 重试一次；进程存活时的失败原因不明，保持原有抛错/拒绝
语义。**下单是例外**：进程死在「委托已到柜台但回执未读到」的窗口时，
盲目重试会重复下单——因此 place_order 的自愈路径在重发前先核对当日
委托（``today_entrusts``），已有同参数挂单则认领，核对不了则拒绝重发
（宁可这一单错过，也不冒重复下单的险，钉钉会提醒人工确认）。
守护循环（run_auto）另通过 :meth:`health_check` 在盘中每分钟主动巡检；
broker 初始化时若 xiadan.exe 未运行也会先自动拉起（守护进程可先于
交易客户端启动）。

依赖（惰性导入，paper 模式无需安装）::

    pip install easytrader pywinauto   # universal 客户端还需安装同花顺 PC 客户端

本 broker 与 QmtBroker 一样是实盘：不模拟费用与 T+1，以柜台返回为准。
"""

from __future__ import annotations

import logging
import os
import time
from datetime import datetime

from ..dataflows.ashare_symbol_utils import normalize_ashare_symbol, parse_ashare_symbol
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

from .path_helper import resolve_ths_xiadan
from .xiadan_guard import XiadanGuard

# easytrader 不同客户端返回的字段名不统一，逐个别名取值。
_BALANCE_ALIASES = {
    "total_asset": ("总资产", "total_asset", "资产总额"),
    "available_cash": ("可用金额", "可用资金", "cash", "enable_balance"),
    "market_value": ("证券市值", "股票市值", "market_value"),
    "frozen_cash": ("冻结金额", "冻结资金", "frozen_cash"),
}


def _pick(data: dict, keys: tuple[str, ...], default=0.0):
    """从字典里按别名顺序取第一个非空值。"""
    if not isinstance(data, dict):
        return default
    for key in keys:
        value = data.get(key)
        if value not in (None, ""):
            try:
                return float(value)
            except (TypeError, ValueError):
                continue
    return default


# 重连重试参数（测试可 patch 加速）
_RECONNECT_ATTEMPTS = 2
_RECONNECT_RETRY_WAIT = 2.0
_RECONNECT_COOLDOWN = 30.0


class EasytraderBroker(BaseBroker):
    """基于 easytrader 的实盘 broker（同花顺通用客户端 / 银河网页版）。"""

    mode = "easytrader"

    # 会话健康与重连节流（类级默认，实例操作覆盖；测试用 object.__new__
    # 构造时也能拿到安全默认值）。
    _session_ok: bool = True
    _last_reconnect_ts: float | None = None

    def __init__(
        self,
        client_type: str = "universal",
        client_path: str | None = None,
        user: str | None = None,
        password: str | None = None,
        **client_kwargs,
    ):
        """
        ``client_type``: ``"universal"``（同花顺客户端，任何券商均可）或
        ``"yh"``（银河网页版）。universal 需要 ``client_path`` 指向
        同花顺 ``xiadan.exe``；yh 需要 ``user`` / ``password``。
        """
        self.client_type = client_type.lower().strip()
        self.account_name = str(client_kwargs.pop("account_name", "") or "easytrader")
        self._connect_args = {
            "client_path": client_path,
            "user": user,
            "password": password,
            **client_kwargs,
        }
        self._exe_path: str | None = None      # universal 模式下由 _connect 解析
        self._guard: XiadanGuard | None = None
        # 仅 universal 模式有进程可守护（yh 是网页通道）。守护器在首次
        # connect 之前装配：easytrader connect 要求 xiadan.exe 已在运行，
        # 进程没开时先拉起（如守护进程先于客户端启动的早晨场景）。
        if self.client_type in ("universal", "ths", "同花顺") and client_path:
            exe_path = resolve_ths_xiadan(client_path)
            if exe_path and os.path.isfile(exe_path):
                self._exe_path = exe_path
                self._guard = XiadanGuard(
                    exe_path,
                    account_name=self.account_name,
                    restart_cooldown_sec=60.0,   # 防僵尸循环
                )
                if not self._guard.process_alive():
                    self._guard.ensure_running(context="broker初始化")
        self._client = self._connect(**self._connect_args)
        self._session_ok = True                # 连接成功即会话健康
        self._last_reconnect_ts = None

    # ── 连接 ──

    def _connect(self, *, client_path, user, password, **client_kwargs):
        self._exe_path = None
        try:
            import easytrader
        except ImportError as exc:
            raise RuntimeError(
                "easytrader is not installed. Install it with: "
                "pip install easytrader pywinauto"
            ) from exc

        if self.client_type in ("universal", "ths", "同花顺"):
            client = easytrader.use("universal_client")
            if not client_path:
                raise ValueError(
                    "universal client requires client_path (同花顺安装目录 "
                    "或 xiadan.exe 完整路径)"
                )
            # client_path 可以是安装目录，也可以是 xiadan.exe 的完整路径
            exe_path = resolve_ths_xiadan(client_path)
            if not os.path.isfile(exe_path):
                raise FileNotFoundError(
                    f"找不到同花顺 xiadan.exe：{exe_path}\n"
                    f"请确认安装目录正确（如 D:\\ths\\同花顺）"
                )
            self._exe_path = exe_path      # 供进程守护（XiadanGuard）使用
            client.connect(exe_path)
            # 通用客户端需要用户先在同花顺里登录券商账号；这里触发一次余额
            # 查询验证会话可用，失败则给出明确指引。
            try:
                client.balance
            except Exception as exc:
                raise RuntimeError(
                    "connected to the THS client but the session is not usable — "
                    "log in to your broker account in 同花顺 first "
                    f"({exc})"
                ) from exc
            return client

        if self.client_type in ("yh", "galaxy", "银河"):
            client = easytrader.use("yh_client")
            creds = {"user": user, "password": password}
            creds.update(client_kwargs)
            client.prepare(creds)
            return client

        raise ValueError(
            f"Unknown easytrader client_type {self.client_type!r}; "
            "expected 'universal' or 'yh'"
        )

    def _reconnect(self) -> None:
        """重建 easytrader 客户端会话（xiadan 被守护器拉起后调用）。"""
        self._client = self._connect(**self._connect_args)

    def _reconnect_with_retry(self, attempts: int | None = None,
                              wait_sec: float | None = None) -> None:
        """重连（带重试）：xiadan 刚拉起时交易窗口可能尚未就绪。

        成功置 ``_session_ok = True``；失败抛最后一个异常并置 False。
        参数默认取模块常量（测试可 patch 加速）。
        """
        attempts = _RECONNECT_ATTEMPTS if attempts is None else attempts
        wait_sec = _RECONNECT_RETRY_WAIT if wait_sec is None else wait_sec
        last_exc: Exception | None = None
        for i in range(max(1, attempts)):
            try:
                self._reconnect()
                self._session_ok = True
                return
            except Exception as exc:
                last_exc = exc
                self._session_ok = False
                logger.warning(
                    "[%s] reconnect attempt %d/%d failed: %s",
                    self.account_name, i + 1, attempts, exc,
                )
                if i < attempts - 1:
                    time.sleep(wait_sec)
        assert last_exc is not None
        raise last_exc

    def _try_reconnect(self, cooldown_sec: float | None = None) -> bool:
        """冷却受限的重连（防会话过期场景下的重连风暴）。

        冷却期内不重复尝试，直接返回当前会话状态。
        """
        cooldown_sec = _RECONNECT_COOLDOWN if cooldown_sec is None else cooldown_sec
        now = time.monotonic()
        if (
            self._last_reconnect_ts is not None
            and now - self._last_reconnect_ts < cooldown_sec
        ):
            return self._session_ok
        self._last_reconnect_ts = now
        try:
            self._reconnect_with_retry()
            return True
        except Exception:
            return False

    def _invoke(self, op):
        """执行一次客户端调用，失败时自愈（拉起进程 → 重连 → 重试一次）。

        - 会话已知不健康（上次失败）→ 先重连再调用；
        - 调用失败：进程死 → 守护器拉起；随后重连并重试一次；
        - 重连失败（如登录过期）→ 抛带指引的错误，等人工处理，
          下一次调用/守护巡检会再试（冷却 30s 防风暴）。

        读操作重试永远安全；下单不走这里（place_order 有防重复下单的
        专用恢复路径）。
        """
        guard = self._guard
        if guard is None:
            return op()                     # yh 网页通道无进程守护
        if not self._session_ok and not self._try_reconnect():
            raise RuntimeError(
                "easytrader 会话不可用：请检查同花顺客户端（xiadan.exe）登录状态"
            )
        try:
            return op()
        except Exception as exc:
            self._session_ok = False
            logger.warning(
                "[%s] client call failed (%s: %s) — recovering",
                self.account_name, type(exc).__name__, exc,
            )
            if not guard.process_alive():
                if not guard.ensure_running(context="调用自愈"):
                    raise RuntimeError(
                        "xiadan.exe 掉线且自动拉起失败，请检查交易客户端"
                    ) from exc
            if not self._try_reconnect():
                raise RuntimeError(
                    "easytrader 重连失败，会话可能已过期（需人工登录）"
                ) from exc
            logger.info(
                "[%s] session recovered — retrying the call once",
                self.account_name,
            )
            return op()

    # ── 进程守护 ──

    def health_check(self) -> bool:
        """守护巡检入口（run_auto 守护循环盘中每分钟调用）。

        xiadan 掉线 → 自动拉起并重连；会话不健康（上次调用失败）→
        尝试重连。恢复后返回 True。yh 网页通道无进程概念，恒 True。
        """
        guard = self._guard
        if guard is None:
            return True
        if not guard.process_alive():
            if not guard.ensure_running(context="守护巡检"):
                return False
            self._session_ok = False       # 新进程 = 新窗口，旧 client 引用必失效
        if self._session_ok:
            return True
        try:
            self._reconnect_with_retry()
            return True
        except Exception as exc:
            logger.exception(
                "[%s] xiadan relaunched but the session is unusable",
                self.account_name,
            )
            self._notify_session_unusable(exc)
            return False

    def _notify_session_unusable(self, exc: Exception) -> None:
        try:
            from ..notifier import notify

            notify(
                "xiadan.exe 已拉起但会话不可用",
                f"**账号**：{self.account_name}\n\n"
                f"**原因**：{type(exc).__name__}: {exc}\n\n"
                "**动作**：请在同花顺交易客户端里重新登录券商账号；登录后"
                "无需重启守护进程，下一轮调用会自动重连。",
                level="critical",
                key=f"xiadan-session:{self.account_name}",
            )
        except Exception:  # noqa: BLE001 — 通知失败不影响主流程
            logger.debug("session-unusable notify failed", exc_info=True)

    def _find_today_entrust(self, order: Order, price: float) -> str | None:
        """当日委托中查找同参数的有效挂单（下单自愈防重复的核心核对）。

        匹配 代码 + 价格 + 数量（+方向，字段存在时）；已撤/废单不算。
        返回匹配委托号，无匹配返回 None；查询失败向上抛（调用方应保守
        放弃重发而不是盲目重试）。
        """
        raw = self._invoke(lambda: self._client.today_entrusts) or []
        side_kw = "买" if order.side is OrderSide.BUY else "卖"
        for row in raw:
            if not isinstance(row, dict):
                continue
            code = str(row.get("证券代码") or row.get("stock_code") or "")
            if code != order.symbol:
                continue
            try:
                row_price = float(row.get("委托价格") or row.get("entrust_price") or 0)
                row_amount = int(float(row.get("委托数量") or row.get("entrust_amount") or 0))
            except (TypeError, ValueError):
                continue
            if abs(row_price - float(price)) > 0.005 or row_amount != order.quantity:
                continue
            side_raw = str(row.get("操作") or row.get("买卖方向") or "")
            if side_raw and side_kw not in side_raw:
                continue
            status = str(row.get("委托状态") or "")
            if any(kw in status for kw in ("撤", "废")):
                continue
            entrust_no = str(row.get("委托编号") or row.get("entrust_no") or "")
            if entrust_no:
                return entrust_no
        return None

    def _notify_unverified_order(self, order: Order, exc: Exception) -> None:
        """下单中断且无法核对柜台委托 → 提醒人工确认（防漏单/重复单）。"""
        try:
            from datetime import date

            from ..notifier import notify

            notify(
                "订单提交中断，需人工确认",
                f"**账号**：{self.account_name}\n\n"
                f"**订单**：{'买入' if order.side is OrderSide.BUY else '卖出'} "
                f"{order.symbol} ×{order.quantity} @ {order.price}\n\n"
                f"**原因**：xiadan 掉线自愈后无法核对当日委托"
                f"（{type(exc).__name__}: {exc}），为防重复下单未自动重发。\n\n"
                "**动作**：请在交易客户端「当日委托」里确认该笔是否已提交；"
                "未提交的话下一轮分析会重新决策。",
                level="critical",
                key=f"order-unverified:{self.account_name}:{order.symbol}:"
                    f"{date.today().isoformat()}",
            )
        except Exception:  # noqa: BLE001 — 通知失败不影响主流程
            logger.debug("unverified-order notify failed", exc_info=True)

    # ── 账户 / 持仓 ──

    def get_account(self) -> AccountInfo:
        try:
            balance = self._invoke(lambda: self._client.balance)
        except Exception as exc:
            raise RuntimeError(f"easytrader balance query failed: {exc}") from exc

        available = _pick(balance, _BALANCE_ALIASES["available_cash"])
        market_value = _pick(balance, _BALANCE_ALIASES["market_value"])
        total = _pick(balance, _BALANCE_ALIASES["total_asset"])
        if not total:
            total = available + market_value
        return AccountInfo(
            total_asset=total,
            available_cash=available,
            market_value=market_value,
            frozen_cash=_pick(balance, _BALANCE_ALIASES["frozen_cash"]),
            currency="CNY",
        )

    def get_positions(self) -> dict[str, Position]:
        try:
            raw = self._invoke(lambda: self._client.position) or []
        except Exception as exc:
            raise RuntimeError(f"easytrader position query failed: {exc}") from exc

        positions: dict[str, Position] = {}
        for row in raw:
            if not isinstance(row, dict):
                continue
            code = normalize_ashare_symbol(str(row.get("证券代码") or row.get("stock_code") or "")) or ""
            if not code:
                continue
            try:
                quantity = int(float(row.get("持仓数量") or row.get("股份余额") or row.get("volume") or 0))
            except (TypeError, ValueError):
                quantity = 0
            if quantity <= 0:
                continue
            try:
                available = int(float(row.get("可用余额") or row.get("可用数量") or row.get("enable_amount") or quantity))
            except (TypeError, ValueError):
                available = quantity
            positions[code] = Position(
                symbol=code,
                name=str(row.get("证券名称") or row.get("stock_name") or ""),
                quantity=quantity,
                available=available,
                avg_cost=_pick(row, ("成本价", "持仓成本", "avg_cost")),
                last_price=_pick(row, ("现价", "最新价", "last_price")),
            )
        return positions

    # ── 下单 / 撤单 ──

    def place_order(self, order: Order, last_price: float | None = None) -> OrderResult:
        if order.order_type is OrderType.MARKET:
            # easytrader 只支持限价单；市价意图转为对手价近似（用最新价）。
            price = float(last_price or 0.0)
            if price <= 0:
                return OrderResult(
                    status=OrderStatus.REJECTED,
                    message="market order needs last_price on easytrader",
                    submitted_order=order,
                )
        else:
            if order.price is None:
                return OrderResult(
                    status=OrderStatus.REJECTED,
                    message="limit order requires an explicit price",
                    submitted_order=order,
                )
            price = float(order.price)

        try:
            func = self._client.buy if order.side is OrderSide.BUY else self._client.sell
            result = func(order.symbol, price=price, amount=order.quantity)
        except Exception as exc:
            guard = self._guard
            if guard is None or guard.process_alive():
                return OrderResult(
                    status=OrderStatus.REJECTED,
                    message=f"easytrader order failed: {exc}",
                    submitted_order=order,
                )
            # xiadan 进程已死：拉起 + 重连后先核对当日委托再决定是否重发。
            # 委托可能已到柜台只是回执没读到——盲目重发会重复下单。
            logger.warning(
                "[%s] order submit failed and xiadan.exe is down "
                "(%s: %s) — recovering before any retry",
                self.account_name, type(exc).__name__, exc,
            )
            if not guard.ensure_running(context="下单自愈"):
                return OrderResult(
                    status=OrderStatus.REJECTED,
                    message=f"xiadan 掉线且自动拉起失败，订单未提交: {exc}",
                    submitted_order=order,
                )
            self._session_ok = False
            try:
                self._reconnect_with_retry()
            except Exception as exc2:
                self._notify_session_unusable(exc2)
                return OrderResult(
                    status=OrderStatus.REJECTED,
                    message=f"xiadan 已拉起但重连失败，订单未提交: {exc2}",
                    submitted_order=order,
                )
            try:
                existing = self._find_today_entrust(order, price)
            except Exception as exc2:
                self._notify_unverified_order(order, exc2)
                return OrderResult(
                    status=OrderStatus.REJECTED,
                    message=f"xiadan 已恢复但无法核对当日委托，为防重复下单未重发: {exc2}",
                    submitted_order=order,
                )
            if existing:
                logger.info(
                    "[%s] entrust %s already on venue — adopting it instead of resubmitting",
                    self.account_name, existing,
                )
                return OrderResult(
                    order_id=existing,
                    status=OrderStatus.ACCEPTED,
                    filled_quantity=0,
                    message="recovered: entrust already on venue",
                    submitted_order=order,
                )
            logger.info(
                "[%s] no matching entrust on venue — resubmitting once",
                self.account_name,
            )
            try:
                # 重连后 self._client 已是新对象，需重新解析下单函数
                func = self._client.buy if order.side is OrderSide.BUY else self._client.sell
                result = func(order.symbol, price=price, amount=order.quantity)
            except Exception as exc2:
                return OrderResult(
                    status=OrderStatus.REJECTED,
                    message=f"easytrader order retry failed: {exc2}",
                    submitted_order=order,
                )

        entrust_no = ""
        if isinstance(result, dict):
            entrust_no = str(result.get("entrust_no") or result.get("委托号") or "")
        if not entrust_no:
            return OrderResult(
                status=OrderStatus.REJECTED,
                message=f"no entrust_no in broker response: {result!r}",
                submitted_order=order,
            )
        return OrderResult(
            order_id=entrust_no,
            status=OrderStatus.ACCEPTED,
            filled_quantity=0,
            message="submitted via easytrader",
            submitted_order=order,
        )

    def cancel_order(self, order_id: str) -> OrderResult:
        try:
            result = self._invoke(lambda: self._client.cancel_entrust(order_id))
        except Exception as exc:
            return OrderResult(
                order_id=order_id,
                status=OrderStatus.REJECTED,
                message=f"easytrader cancel failed: {exc}",
            )
        ok = not isinstance(result, dict) or result.get("error") is None
        return OrderResult(
            order_id=order_id,
            status=OrderStatus.CANCELLED if ok else OrderStatus.REJECTED,
            message="cancel submitted" if ok else f"cancel failed: {result!r}",
        )

    # ── 成交查询 ──

    def get_trades(self, symbol: str | None = None) -> list[TradeRecord]:
        try:
            raw = self._invoke(lambda: self._client.today_trades) or []
        except Exception as exc:
            logger.warning("easytrader today_trades failed: %s", exc)
            return []

        trades: list[TradeRecord] = []
        target = normalize_ashare_symbol(symbol) if symbol else None
        for row in raw:
            if not isinstance(row, dict):
                continue
            code = normalize_ashare_symbol(str(row.get("证券代码") or row.get("stock_code") or "")) or ""
            if not code or (target and code != target):
                continue
            side_raw = str(row.get("买卖标志") or row.get("business_flag") or "买")
            side = OrderSide.SELL if ("卖" in side_raw or "sell" in side_raw.lower()) else OrderSide.BUY
            try:
                quantity = int(float(row.get("成交数量") or row.get("business_amount") or 0))
                price = float(row.get("成交价格") or row.get("business_price") or 0)
            except (TypeError, ValueError):
                continue
            if quantity <= 0:
                continue
            trades.append(
                TradeRecord(
                    trade_id=str(row.get("成交编号") or row.get("business_id") or ""),
                    order_id=str(row.get("合同编号") or row.get("entrust_no") or ""),
                    symbol=code,
                    side=side,
                    quantity=quantity,
                    price=price,
                    mode=self.mode,
                )
            )
        return trades

    # ── 生命周期 ──

    def sync(self) -> None:
        return None

    def next_trading_day(self, today: str | None = None) -> None:
        # T+1 可用数量以柜台返回为准。
        return None

    def close(self) -> None:
        # easytrader 无显式断连接口；保留钩子便于子类清理。
        return None
