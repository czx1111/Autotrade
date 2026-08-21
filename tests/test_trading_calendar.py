"""Tests for the A-share trading calendar (holiday awareness + caching)."""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, datetime

import pytest

from tradingagents.dataflows import trading_calendar as tc


@pytest.fixture(autouse=True)
def _isolated_cache(tmp_path, monkeypatch):
    """Point the calendar at a temp cache and reset module state per test."""
    monkeypatch.setattr(tc, "_cache_path", lambda: os.path.join(tmp_path, "td.json"))
    tc.reset_cache_for_tests()
    yield
    tc.reset_cache_for_tests()


def _write_cache(path: str, days: list[str]) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"days": days}, fh)


class TestIsTradingDay:
    def test_holiday_blocked_with_calendar(self, monkeypatch):
        # 缓存覆盖 2026 国庆与前后交易日；网络刷新禁用（fetch 返回 None）
        monkeypatch.setattr(tc, "_fetch_trading_days", lambda: None)
        _write_cache(tc._cache_path(), [
            "2026-09-30", "2026-10-08", "2026-10-09",
        ])
        assert tc.is_trading_day(date(2026, 10, 1)) is False
        assert tc.is_trading_day(date(2026, 10, 8)) is True

    def test_weekday_fallback_without_calendar(self, monkeypatch):
        # 无缓存 + 网络失败 → 退化为工作日启发式（绝不抛异常）
        monkeypatch.setattr(tc, "_fetch_trading_days", lambda: None)
        assert tc.is_trading_day(date(2026, 8, 19)) is True    # 周三
        assert tc.is_trading_day(date(2026, 8, 22)) is False   # 周六

    def test_string_and_datetime_inputs(self, monkeypatch):
        monkeypatch.setattr(tc, "_fetch_trading_days", lambda: None)
        _write_cache(tc._cache_path(), ["2026-08-19"])
        assert tc.is_trading_day("2026-08-19") is True
        assert tc.is_trading_day("2026-08-19 10:30:00") is True
        assert tc.is_trading_day(datetime(2026, 8, 19, 15, 0)) is True


class TestRefreshLogic:
    def test_fetch_failure_keeps_stale_cache(self, monkeypatch):
        # 缓存过期但网络挂了：继续用旧缓存（可用性优先于新鲜度）
        _write_cache(tc._cache_path(), ["2026-08-19"])
        # 强制缓存过期：把 mtime 改到 30 天前
        old = datetime.now().timestamp() - 30 * 86400
        os.utime(tc._cache_path(), (old, old))
        monkeypatch.setattr(tc, "_fetch_trading_days", lambda: None)
        assert tc.is_trading_day(date(2026, 8, 19)) is True

    def test_fresh_fetch_replaces_cache(self, monkeypatch):
        monkeypatch.setattr(
            tc, "_fetch_trading_days",
            lambda: {"2026-10-08", "2026-10-09"},
        )
        assert tc.is_trading_day(date(2026, 10, 9)) is True
        # 拉取成功后应写盘缓存
        with open(tc._cache_path(), encoding="utf-8") as fh:
            saved = json.load(fh)
        assert "2026-10-08" in saved["days"]


class TestIsTradingTimeToday:
    def test_holiday_time_window_still_blocked(self, monkeypatch):
        # 国庆节当天上午 10 点：时段是交易时段，但日历不是交易日 → 拦截
        monkeypatch.setattr(tc, "_fetch_trading_days", lambda: None)
        _write_cache(tc._cache_path(), ["2026-09-30", "2026-10-08"])
        assert tc.is_trading_time_today(datetime(2026, 10, 1, 10, 0)) is False

    def test_trading_day_time_window_passes(self, monkeypatch):
        monkeypatch.setattr(tc, "_fetch_trading_days", lambda: None)
        _write_cache(tc._cache_path(), ["2026-08-19"])
        assert tc.is_trading_time_today(datetime(2026, 8, 19, 10, 0)) is True

    def test_lunch_break_blocked_on_trading_day(self, monkeypatch):
        monkeypatch.setattr(tc, "_fetch_trading_days", lambda: None)
        _write_cache(tc._cache_path(), ["2026-08-19"])
        assert tc.is_trading_time_today(datetime(2026, 8, 19, 12, 0)) is False


class TestNextTradingDay:
    def test_skips_holiday_block(self, monkeypatch):
        monkeypatch.setattr(tc, "_fetch_trading_days", lambda: None)
        _write_cache(tc._cache_path(), ["2026-09-30", "2026-10-08"])
        assert tc.next_trading_day(date(2026, 10, 1)) == date(2026, 10, 8)

    def test_no_next_day_within_lookahead(self, monkeypatch):
        monkeypatch.setattr(tc, "_fetch_trading_days", lambda: None)
        _write_cache(tc._cache_path(), ["2026-08-19"])
        assert tc.next_trading_day(date(2026, 8, 19), max_lookahead=3) is None
