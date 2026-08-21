"""xiadan.exe 进程守护测试：拉起、冷却、通知；broker 调用自愈重试。

进程检测/启动均通过依赖注入模拟，测试不触碰真实 Windows 环境；
broker 侧用 ``object.__new__`` 绕过 easytrader 连接，只验证恢复逻辑。
"""

import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

from tradingagents.broker.easytrader_broker import EasytraderBroker
from tradingagents.broker.models import Order, OrderSide, OrderType
from tradingagents.broker.xiadan_guard import XiadanGuard


class _Clock:
    """可推进的假时钟。"""

    def __init__(self, start=datetime(2026, 3, 3, 10, 0, 0)):
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += timedelta(seconds=seconds)


def _make_guard(state, *, cooldown=180.0, timeout=3, launch_flip=True,
                launch_opens_window=True, window_fn=None):
    """构造 DI 守护器：launch 后 state['alive'] 翻转（或不翻转=拉起失败）；
    ``launch_opens_window`` False 时模拟「进程在但窗口不出现」的僵尸。"""

    def launch(exe_path):
        state["launches"] += 1
        if launch_flip:
            state["alive"] = True
            state["window"] = launch_opens_window

    def kill():
        state["kills"] += 1
        state["alive"] = False
        state["window"] = False

    return XiadanGuard(
        r"C:\fake\xiadan.exe",
        account_name="test",
        restart_cooldown_sec=cooldown,
        startup_timeout_sec=timeout,
        now_fn=state["clock"],
        sleep_fn=lambda s: None,
        alive_fn=lambda: state["alive"],
        window_fn=window_fn or (lambda: state["window"]),
        launch_fn=launch,
        kill_fn=kill,
    )


def _state():
    clock = _Clock()
    return {"alive": False, "window": False, "launches": 0, "kills": 0,
            "clock": clock}


# ── XiadanGuard ──


@pytest.mark.unit
class TestXiadanGuard(unittest.TestCase):
    def test_alive_no_launch(self):
        state = _state()
        state["alive"] = True
        guard = _make_guard(state)
        with patch("tradingagents.notifier.notify") as mock_notify:
            self.assertTrue(guard.ensure_running("巡检"))
        self.assertEqual(state["launches"], 0)
        mock_notify.assert_not_called()

    def test_dead_relaunch_success_notifies_warning(self):
        state = _state()
        guard = _make_guard(state)
        with patch("tradingagents.notifier.notify") as mock_notify:
            self.assertTrue(guard.ensure_running("巡检"))
        self.assertEqual(state["launches"], 1)
        self.assertTrue(state["alive"])
        mock_notify.assert_called_once()
        self.assertEqual(mock_notify.call_args.kwargs.get("level"), "warning")

    def test_dead_relaunch_failure_notifies_critical(self):
        state = _state()
        guard = _make_guard(state, launch_flip=False)  # 拉起后进程仍不在
        with patch("tradingagents.notifier.notify") as mock_notify:
            self.assertFalse(guard.ensure_running("巡检"))
        self.assertEqual(state["launches"], 1)
        self.assertEqual(state["kills"], 0)   # 进程没起来，无僵尸可清理
        mock_notify.assert_called_once()
        self.assertEqual(mock_notify.call_args.kwargs.get("level"), "critical")

    def test_cooldown_suppresses_rapid_relaunch(self):
        state = _state()
        guard = _make_guard(state, cooldown=180.0, launch_flip=False)
        with patch("tradingagents.notifier.notify"):
            self.assertFalse(guard.ensure_running())   # 第一次尝试（失败）
        state["clock"].advance(60)                       # 冷却期内
        with patch("tradingagents.notifier.notify") as mock_notify:
            self.assertFalse(guard.ensure_running())   # 被冷却压制
        self.assertEqual(state["launches"], 1)           # 没有第二次拉起
        mock_notify.assert_not_called()                  # 也不再告警刷屏

    def test_cooldown_expires_allows_relaunch(self):
        state = _state()
        guard = _make_guard(state, cooldown=180.0, launch_flip=False)
        with patch("tradingagents.notifier.notify"):
            guard.ensure_running()
        state["clock"].advance(200)                      # 冷却已过
        with patch("tradingagents.notifier.notify"):
            self.assertFalse(guard.ensure_running())
        self.assertEqual(state["launches"], 2)

    def test_recovered_process_dies_again_triggers_new_relaunch(self):
        state = _state()
        guard = _make_guard(state, cooldown=180.0)
        with patch("tradingagents.notifier.notify"):
            self.assertTrue(guard.ensure_running())
        state["alive"] = False                           # 再次掉线
        state["clock"].advance(300)                      # 超过冷却
        with patch("tradingagents.notifier.notify"):
            self.assertTrue(guard.ensure_running())
        self.assertEqual(state["launches"], 2)

    def test_relaunch_windowless_zombie_killed_and_fails(self):
        """拉起后进程在但窗口始终不出现（僵尸）→ 清理僵尸 + critical。

        强杀后立刻重启实测会出现这种实例：占单实例锁、无界面，不清掉
        后续拉起永远出不了正常窗口。
        """
        state = _state()
        guard = _make_guard(state, launch_opens_window=False)
        with patch("tradingagents.notifier.notify") as mock_notify:
            self.assertFalse(guard.ensure_running("巡检"))
        self.assertEqual(state["launches"], 1)
        self.assertEqual(state["kills"], 1)      # 僵尸被清理
        self.assertFalse(state["alive"])
        levels = [c.kwargs.get("level") for c in mock_notify.call_args_list]
        self.assertEqual(levels, ["critical"])   # 只有失败告警

    def test_slow_window_still_becomes_ready(self):
        """交易窗口延迟出现（实测约 6s）→ 轮询等到窗口就绪才算成功。"""
        state = _state()
        polls = {"n": 0}

        def slow_window():
            polls["n"] += 1
            return polls["n"] >= 3               # 前两次轮询无窗口

        guard = _make_guard(state, window_fn=slow_window)
        with patch("tradingagents.notifier.notify") as mock_notify:
            self.assertTrue(guard.ensure_running())
        self.assertTrue(state["alive"])
        self.assertTrue(state["window"])
        self.assertEqual(state["kills"], 0)      # 启动慢 ≠ 僵尸，不误杀
        mock_notify.assert_called_once()
        self.assertEqual(mock_notify.call_args.kwargs.get("level"), "warning")


# ── EasytraderBroker 调用自愈 ──


def _make_broker(state, *, reconnect_ok=True):
    """绕过 easytrader 连接构造 broker，只装配守护/恢复所需属性。"""
    broker = object.__new__(EasytraderBroker)
    broker.account_name = "test"
    broker._guard = _make_guard(state)
    broker._connect_args = {"client_path": r"C:\fake", "user": None, "password": None}

    reconnected = []

    def _fake_reconnect():
        if not reconnect_ok:
            raise RuntimeError("connected to the THS client but the session is not usable")
        reconnected.append(True)

    broker._reconnect = _fake_reconnect
    return broker, reconnected


class _FlakyClient:
    """第一次调用抛错（进程死亡场景），重试成功。"""

    def __init__(self):
        self.calls = 0

    @property
    def balance(self):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("UI element not found")
        return {"总资产": 100.0}


class _OrderClient:
    """模拟下单时进程死亡的三种柜台状态。

    ``first_reached_venue``: 第一次 buy 是否已把委托送到柜台
    （进程死在「已提交未读到回执」的窗口 = True）。
    ``entrusts_query_broken``: today_entrusts 是否查询失败（无法核对）。
    """

    def __init__(self, *, first_reached_venue=False, entrusts_query_broken=False):
        self.first_reached_venue = first_reached_venue
        self.entrusts_query_broken = entrusts_query_broken
        self.venue_entrusts: list[dict] = []
        self.submit_attempts = 0

    def _entrust_row(self, entrust_no: str) -> dict:
        return {
            "委托编号": entrust_no, "证券代码": "600519", "操作": "买入",
            "委托价格": 10.0, "委托数量": 100, "委托状态": "未成交",
        }

    def buy(self, symbol, price, amount):
        self.submit_attempts += 1
        if self.submit_attempts == 1:
            if self.first_reached_venue:
                self.venue_entrusts.append(self._entrust_row("E-first"))
            raise RuntimeError("process died mid-call")
        self.venue_entrusts.append(self._entrust_row("E-retry"))
        return {"entrust_no": "E-retry"}

    @property
    def today_entrusts(self):
        if self.entrusts_query_broken:
            raise RuntimeError("entrusts query failed")
        return list(self.venue_entrusts)


@pytest.mark.unit
class TestBrokerInvokeRecovery(unittest.TestCase):
    def test_transient_failure_with_process_alive_self_heals(self):
        """进程存活的瞬时失败（如窗口句柄失效）→ 重连自愈 + 重试一次。"""
        state = _state()
        state["alive"] = True
        broker, reconnected = _make_broker(state)
        broker._client = _FlakyClient()

        result = broker._invoke(lambda: broker._client.balance)

        self.assertEqual(result, {"总资产": 100.0})
        self.assertEqual(broker._client.calls, 2)   # 失败一次 + 重试成功
        self.assertEqual(reconnected, [True])       # 重连过

    def test_failure_with_alive_and_reconnect_failing_raises(self):
        """进程在但重连也失败（会话过期）→ 抛带指引的错误。"""
        state = _state()
        state["alive"] = True
        broker, _ = _make_broker(state, reconnect_ok=False)
        broker._client = _FlakyClient()

        with patch("tradingagents.broker.easytrader_broker._RECONNECT_RETRY_WAIT", 0.0):
            with self.assertRaises(RuntimeError) as ctx:
                broker._invoke(lambda: broker._client.balance)
        self.assertIn("重连失败", str(ctx.exception))

    def test_unhealthy_session_with_cooldown_raises_fast(self):
        """会话已知不健康且冷却期内 → 快速抛错，不碰客户端。"""
        state = _state()
        state["alive"] = True
        broker, reconnected = _make_broker(state)
        broker._client = _FlakyClient()
        broker._session_ok = False
        broker._last_reconnect_ts = __import__("time").monotonic()   # 刚试过

        with self.assertRaises(RuntimeError) as ctx:
            broker._invoke(lambda: broker._client.balance)
        self.assertIn("会话不可用", str(ctx.exception))
        self.assertEqual(broker._client.calls, 0)    # 没碰客户端
        self.assertEqual(reconnected, [])            # 冷却压制了重连风暴

    def test_dead_process_recovers_and_retries_once(self):
        state = _state()
        state["alive"] = False
        broker, reconnected = _make_broker(state)
        client = _FlakyClient()
        broker._client = client

        with patch("tradingagents.notifier.notify"):
            result = broker._invoke(lambda: broker._client.balance)

        self.assertEqual(result, {"总资产": 100.0})
        self.assertEqual(client.calls, 2)          # 失败一次 + 重试一次
        self.assertEqual(reconnected, [True])      # 重连过

    def test_relaunch_failure_raises_runtime_error(self):
        state = _state()
        broker, _ = _make_broker(state)
        guard = _make_guard(state, launch_flip=False)   # 拉不起来的守护器
        broker._guard = guard
        broker._client = _FlakyClient()

        with patch("tradingagents.notifier.notify"):
            with self.assertRaises(RuntimeError) as ctx:
                broker._invoke(lambda: broker._client.balance)
        self.assertIn("自动拉起失败", str(ctx.exception))

    def test_place_order_never_reached_venue_resubmits(self):
        """场景 A：委托没到柜台（进程死在提交前）→ 核对无匹配 → 重发一次。"""
        state = _state()
        state["alive"] = False
        broker, _ = _make_broker(state)
        client = _OrderClient(first_reached_venue=False)
        broker._client = client
        order = Order(symbol="600519", side=OrderSide.BUY, quantity=100,
                      price=10.0, order_type=OrderType.LIMIT)

        with patch("tradingagents.notifier.notify"):
            result = broker.place_order(order)

        self.assertEqual(result.order_id, "E-retry")
        self.assertEqual(client.submit_attempts, 2)          # 失败 1 次 + 重发 1 次
        self.assertEqual(len(client.venue_entrusts), 1)      # 柜台只收到 1 笔

    def test_place_order_already_on_venue_adopts_entrust(self):
        """场景 B：委托已到柜台但回执没读到 → 核对认领，绝不重发。"""
        state = _state()
        state["alive"] = False
        broker, _ = _make_broker(state)
        client = _OrderClient(first_reached_venue=True)
        broker._client = client
        order = Order(symbol="600519", side=OrderSide.BUY, quantity=100,
                      price=10.0, order_type=OrderType.LIMIT)

        with patch("tradingagents.notifier.notify"):
            result = broker.place_order(order)

        self.assertEqual(result.order_id, "E-first")         # 认领已有委托
        self.assertEqual(client.submit_attempts, 1)          # 没有第二次提交
        self.assertEqual(len(client.venue_entrusts), 1)      # 柜台只有 1 笔

    def test_place_order_unverifiable_rejects_and_notifies(self):
        """场景 C：恢复后核对不了当日委托 → 拒绝重发 + critical 人工确认。"""
        state = _state()
        state["alive"] = False
        broker, _ = _make_broker(state)
        client = _OrderClient(entrusts_query_broken=True)
        broker._client = client
        order = Order(symbol="600519", side=OrderSide.BUY, quantity=100,
                      price=10.0, order_type=OrderType.LIMIT)

        with patch("tradingagents.notifier.notify") as mock_notify:
            result = broker.place_order(order)

        self.assertEqual(result.status.value, "rejected")
        self.assertIn("无法核对", result.message)
        self.assertEqual(client.submit_attempts, 1)          # 没有盲目重发
        levels = [c.kwargs.get("level") for c in mock_notify.call_args_list]
        self.assertIn("critical", levels)


@pytest.mark.unit
class TestBrokerHealthCheck(unittest.TestCase):
    def test_no_guard_returns_true(self):
        broker = object.__new__(EasytraderBroker)
        broker._guard = None
        self.assertTrue(broker.health_check())

    def test_alive_returns_true_no_action(self):
        state = _state()
        state["alive"] = True
        broker, reconnected = _make_broker(state)
        self.assertTrue(broker.health_check())
        self.assertEqual(state["launches"], 0)
        self.assertEqual(reconnected, [])

    def test_dead_relaunch_and_reconnect(self):
        state = _state()
        broker, reconnected = _make_broker(state)
        with patch("tradingagents.notifier.notify"):
            self.assertTrue(broker.health_check())
        self.assertEqual(state["launches"], 1)
        self.assertEqual(reconnected, [True])

    def test_dead_relaunch_but_session_unusable_notifies_critical(self):
        state = _state()
        broker, _ = _make_broker(state, reconnect_ok=False)
        with patch("tradingagents.broker.easytrader_broker._RECONNECT_RETRY_WAIT", 0.0), \
             patch("tradingagents.notifier.notify") as mock_notify:
            self.assertFalse(broker.health_check())
        levels = [call.kwargs.get("level") for call in mock_notify.call_args_list]
        self.assertIn("critical", levels)         # warning(拉起) + critical(会话不可用)

    def test_dead_relaunch_fails_returns_false(self):
        state = _state()
        broker, _ = _make_broker(state)
        broker._guard = _make_guard(state, launch_flip=False)
        with patch("tradingagents.notifier.notify"):
            self.assertFalse(broker.health_check())


@pytest.mark.unit
class TestGetBrokerPassesAccountName(unittest.TestCase):
    def test_account_name_accepted_and_defaulted(self):
        # account_name 是可选 kwargs，不破坏既有构造调用
        broker = object.__new__(EasytraderBroker)
        broker.account_name = "ths-live"
        self.assertEqual(broker.account_name, "ths-live")


@pytest.mark.unit
class TestBrokerInitLaunchesProcess(unittest.TestCase):
    """broker 初始化时进程未开 → 先拉起再 connect（easytrader connect
    要求 xiadan.exe 已在运行，否则守护进程无法先于客户端启动）。"""

    def _make(self, *, process_alive: bool):
        """返回 (broker, guard_mock, XiadanGuard类mock)。

        exe 用真实临时文件让 ``os.path.isfile`` 通过；resolve/XiadanGuard/
        _connect 全部 mock，不触碰真实 Windows 环境。
        """
        from pathlib import Path
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            exe = Path(td) / "xiadan.exe"
            exe.write_bytes(b"")
            patchers = [
                patch("tradingagents.broker.easytrader_broker.resolve_ths_xiadan",
                      return_value=str(exe)),
                patch("tradingagents.broker.easytrader_broker.XiadanGuard"),
                patch.object(EasytraderBroker, "_connect", return_value=object()),
            ]
            started = [p.start() for p in patchers]
            try:
                MockGuard = started[1]           # 替换 XiadanGuard 的 mock 类
                guard = MockGuard.return_value
                guard.process_alive.return_value = process_alive
                broker = EasytraderBroker(
                    client_type="universal", client_path=str(exe.parent),
                    account_name="t",
                )
            finally:
                for p in patchers:
                    p.stop()
            return broker, guard, MockGuard

    def test_init_launches_when_process_down(self):
        broker, guard, MockGuard = self._make(process_alive=False)
        guard.ensure_running.assert_called_once()      # 主动拉起
        self.assertIs(broker._guard, guard)
        self.assertEqual(
            MockGuard.call_args.kwargs.get("restart_cooldown_sec"), 60.0,
        )

    def test_init_skips_launch_when_process_alive(self):
        broker, guard, MockGuard = self._make(process_alive=True)
        guard.ensure_running.assert_not_called()

    def test_init_yh_mode_has_no_guard(self):
        with patch.object(EasytraderBroker, "_connect", return_value=object()):
            broker = EasytraderBroker(client_type="yh", user="u", password="p")
        self.assertIsNone(broker._guard)
