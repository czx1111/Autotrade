"""Tests for the DingTalk notifier: delivery, signing, dedup, and hook wiring."""

from __future__ import annotations

import json
from unittest import mock

import pytest

from tradingagents import notifier
from tradingagents.notifier import notify


@pytest.fixture(autouse=True)
def _clean_dedup():
    notifier.reset_dedup_for_tests()
    yield
    notifier.reset_dedup_for_tests()


def _config(webhook="https://oapi.dingtalk.com/robot/send?access_token=t", secret=None):
    return mock.patch.object(
        notifier, "_webhook_settings", return_value=(webhook, secret)
    )


class TestNotifyDelivery:
    def test_sends_markdown_payload(self):
        with _config() as _, mock.patch("requests.post") as post:
            post.return_value.ok = True
            post.return_value.json.return_value = {"errcode": 0}
            assert notify("标题", "内容", level="critical") is True
            _, kwargs = post.call_args
            body = kwargs["json"]
            assert body["msgtype"] == "markdown"
            assert "🚨" in body["markdown"]["title"]
            assert "###" in body["markdown"]["text"]

    def test_noop_without_webhook(self):
        with _config(webhook=None):
            assert notify("标题", "内容") is False

    def test_failure_never_raises(self):
        with _config():
            with mock.patch("requests.post", side_effect=RuntimeError("net down")):
                assert notify("标题", "内容") is False

    def test_errcode_nonzero_is_failure(self):
        with _config():
            with mock.patch("requests.post") as post:
                post.return_value.ok = True
                post.return_value.json.return_value = {"errcode": 310000, "errmsg": "bad token"}
                assert notify("标题", "内容") is False

    def test_signed_webhook_appends_timestamp_and_sign(self):
        with _config(secret="SECtest"):
            with mock.patch("requests.post") as post:
                post.return_value.ok = True
                post.return_value.json.return_value = {"errcode": 0}
                assert notify("标题", "内容") is True
                url = post.call_args[0][0]
                assert "timestamp=" in url and "sign=" in url


class TestDedup:
    def test_same_key_suppressed_within_ttl(self):
        with _config():
            with mock.patch("requests.post") as post:
                post.return_value.ok = True
                post.return_value.json.return_value = {"errcode": 0}
                assert notify("a", "x", key="k1") is True
                assert notify("a", "x", key="k1") is False   # 去重
                assert notify("a", "x", key="k2") is True    # 不同 key 不受影响

    def test_no_key_always_sends(self):
        with _config():
            with mock.patch("requests.post") as post:
                post.return_value.ok = True
                post.return_value.json.return_value = {"errcode": 0}
                assert notify("a", "x") is True
                assert notify("a", "x") is True


class TestHookWiring:
    def test_daily_loss_breach_notifies(self):
        """日亏熔断触发 → critical 通知（含账号名）。"""
        from tradingagents.broker import PaperBroker
        from tradingagents.execution import OrderExecutor

        broker = PaperBroker(initial_capital=100_000.0, state_path=self._tmp())
        # 开盘权益 10 万，当前 9 万 → 亏 10% ≥ 3% 限额
        executor = OrderExecutor(
            broker, config={"account_name": "test-acct"},
        )
        executor.day_start_equity = 100_000.0
        with mock.patch.object(notifier, "notify", wraps=notifier.notify) as spy:
            checks = executor._run_risk_checks(
                code="600519", action="buy", order_value=5_000.0,
                total_value=90_000.0, current_position_value=0.0,
                available_cash=90_000.0, sector=None,
            )
        daily = [c for c in checks if c.name == "daily_loss"]
        assert daily and daily[0].passed is False
        assert spy.called
        assert spy.call_args.kwargs.get("level") == "critical"
        broker.close()

    def test_approval_queue_notifies(self):
        """大额订单入审批队列 → 通知带批准命令。"""
        from tradingagents.auto_trader import AutoTrader, PendingOrder

        entry = PendingOrder(
            id="n-1", symbol="600519", action="buy", quantity=100,
            estimate_value=130_000.0,
        )
        fake_self = type("T", (), {"account": type("A", (), {"name": "notify-test"})()})()
        with mock.patch.object(notifier, "notify", wraps=notifier.notify) as spy:
            AutoTrader._notify_approval(fake_self, entry)
        assert spy.called
        text = spy.call_args[0][1]
        assert "--approve" in text

    @staticmethod
    def _tmp() -> str:
        import tempfile, os
        path = os.path.join(tempfile.gettempdir(), f"notify_test_{os.getpid()}.json")
        if os.path.exists(path):
            os.remove(path)
        return path
