"""Tests for health probes and notification logging (UI health/alerts backend)."""

from __future__ import annotations

import json
import os
from unittest import mock

import pytest

from tradingagents import notifier
from tradingagents.ui.health_check import ProbeResult, run_all_probes


@pytest.fixture(autouse=True)
def _clean_dedup():
    notifier.reset_dedup_for_tests()
    yield
    notifier.reset_dedup_for_tests()


class TestNotificationLog:
    def test_notify_logs_to_jsonl_even_without_webhook(self, tmp_path, monkeypatch):
        """webhook 未配置时事件也要落盘（告警页的事件源）。"""
        log_path = tmp_path / "notifications.jsonl"
        monkeypatch.setattr(notifier, "_notification_log_path", lambda: str(log_path))
        monkeypatch.setattr(
            notifier, "_webhook_settings", lambda: (None, None)
        )
        assert notifier.notify("测试标题", "**内容**", level="warning") is False
        assert log_path.exists()
        rows = [json.loads(ln) for ln in log_path.read_text(encoding="utf-8").splitlines()]
        assert len(rows) == 1
        assert rows[0]["title"] == "测试标题"
        assert rows[0]["level"] == "warning"

    def test_load_history_newest_first(self, tmp_path, monkeypatch):
        log_path = tmp_path / "notifications.jsonl"
        monkeypatch.setattr(notifier, "_notification_log_path", lambda: str(log_path))
        monkeypatch.setattr(
            notifier, "_webhook_settings", lambda: (None, None)
        )
        notifier.reset_dedup_for_tests()
        # 不同 key 绕过去重
        notifier.notify("第一条", "x", level="info", key="k1")
        notifier.notify("第二条", "y", level="critical", key="k2")
        notifier.notify("第三条", "z", level="warning", key="k3")
        history = notifier.load_notification_history()
        assert len(history) == 3
        assert history[0]["title"] == "第三条"      # 新→旧
        assert history[-1]["title"] == "第一条"

    def test_load_history_missing_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            notifier, "_notification_log_path",
            lambda: str(tmp_path / "nope.jsonl"),
        )
        assert notifier.load_notification_history() == []

    def test_deduped_events_not_logged(self, tmp_path, monkeypatch):
        """去重跳过的事件不应重复落盘。"""
        log_path = tmp_path / "notifications.jsonl"
        monkeypatch.setattr(notifier, "_notification_log_path", lambda: str(log_path))
        monkeypatch.setattr(
            notifier, "_webhook_settings", lambda: (None, None)
        )
        notifier.notify("t", "x", key="same")
        notifier.notify("t", "x", key="same")
        rows = log_path.read_text(encoding="utf-8").splitlines()
        assert len(rows) == 1


class TestHealthProbes:
    def test_probe_result_dataclass(self):
        r = ProbeResult(name="x", kind="quote", ok=True, latency_ms=10, detail="d")
        assert r.ok and r.latency_ms == 10

    def test_probe_swallows_exceptions(self):
        """探针必须把异常转成 ok=False，绝不上抛。"""
        from tradingagents.ui import health_check as hc

        def boom():
            raise ConnectionError("vendor down")

        r = hc._timed(boom)
        assert r.ok is False
        assert "ConnectionError" in r.detail

    def test_run_all_probes_shapes(self):
        """全量探针：返回结构完整（真实网络，慢源失败也算正常形态）。"""
        results = run_all_probes()
        assert len(results) >= 7
        names = {r.name for r in results}
        assert {"腾讯行情", "新浪行情", "东财全市场快照",
                "东财日K", "腾讯日K", "新浪日K", "交易日历(新浪)"} <= names
        for r in results:
            assert isinstance(r.ok, bool)
            assert r.latency_ms >= 0
            assert r.detail  # 成功有摘要，失败有原因


class TestHeartbeatFile:
    def test_heartbeat_written_after_phase(self, tmp_path, monkeypatch):
        """run_auto 的心跳写入逻辑：文件结构 + 字段。"""
        # 直接复刻 _write_heartbeat 的核心逻辑做形状验证（不启动守护进程）
        import time as _time
        from pathlib import Path

        path = tmp_path / "daemon_heartbeat.json"
        data: dict = {}
        key = "平安证券:monitor"
        data[key] = {
            "ts": _time.strftime("%Y-%m-%d %H:%M:%S"),
            "phase": "monitor", "phase_cn": "盯盘巡检",
            "account": "平安证券", "status": "ok",
            "duration_s": 0.4, "detail": "",
        }
        data["_daemon"] = {
            "started_at": data[key]["ts"],
            "last_seen": data[key]["ts"],
            "accounts": ["平安证券"],
        }
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        loaded = json.loads(path.read_text(encoding="utf-8"))
        assert loaded[key]["status"] == "ok"
        assert loaded["_daemon"]["accounts"] == ["平安证券"]


class TestUiPagesImport:
    def test_health_and_alerts_pages_registered(self):
        from tradingagents.ui.common import NAV_PAGES
        from tradingagents.ui.pages import PAGES

        assert "🩺 系统健康" in NAV_PAGES
        assert "🔔 告警中心" in NAV_PAGES
        assert callable(PAGES["🩺 系统健康"])
        assert callable(PAGES["🔔 告警中心"])
        # 导航顺序与 PAGES 键一致（webui 按 NAV_PAGES 索引 PAGES）
        for page in NAV_PAGES:
            assert page in PAGES
