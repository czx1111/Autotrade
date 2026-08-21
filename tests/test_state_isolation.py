"""Tests for per-account paper state isolation and daemon process locks."""

from __future__ import annotations

import os
import tempfile
from datetime import datetime

import pytest

from tradingagents.broker import account_state_path, get_broker


class TestAccountStatePath:
    def test_per_account_files_differ(self):
        a = account_state_path("平安证券")
        b = account_state_path("中国银河证券")
        assert a != b
        assert os.path.basename(a) == "paper_state_平安证券.json"

    def test_cjk_name_preserved(self):
        # 中日韩字符是合法文件名成分（NTFS/ext4），不应被替换
        path = account_state_path("银河A1")
        assert "银河A1" in os.path.basename(path)

    def test_path_traversal_sanitized(self):
        path = account_state_path("../../etc/evil")
        assert ".." not in path
        assert os.path.basename(path).startswith("paper_state_")


class TestGetBrokerIsolation:
    def test_account_name_yields_isolated_state(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "tradingagents.broker.paper._DEFAULT_STATE_PATH",
            str(tmp_path / "paper_state.json"),
        )
        b1 = get_broker({"broker": "paper", "account_name": "平安证券"})
        b2 = get_broker({"broker": "paper", "account_name": "中国银河证券"})
        assert b1.state_path != b2.state_path
        assert b1.name == "平安证券"

    def test_explicit_state_path_wins(self, tmp_path, monkeypatch):
        explicit = str(tmp_path / "custom.json")
        broker = get_broker({
            "broker": "paper", "account_name": "x", "state_path": explicit,
        })
        assert broker.state_path == explicit

    def test_no_account_name_keeps_default(self, tmp_path, monkeypatch):
        default = str(tmp_path / "paper_state.json")
        monkeypatch.setattr(
            "tradingagents.broker.paper._DEFAULT_STATE_PATH", default,
        )
        broker = get_broker({"broker": "paper"})
        assert broker.state_path == default


class TestAccountProcessLock:
    def test_double_acquire_rejected(self):
        from tradingagents.process_lock import AccountProcessLock

        first = AccountProcessLock("lock-test-acct")
        assert first.acquire() is True
        try:
            second = AccountProcessLock("lock-test-acct")
            assert second.acquire() is False
            assert "禁止双开" in second.conflict_message
        finally:
            first.release()

    def test_release_allows_reacquire(self):
        from tradingagents.process_lock import AccountProcessLock

        lock = AccountProcessLock("lock-test-acct2")
        assert lock.acquire() is True
        lock.release()
        again = AccountProcessLock("lock-test-acct2")
        assert again.acquire() is True
        again.release()

    def test_different_accounts_do_not_conflict(self):
        from tradingagents.process_lock import AccountProcessLock

        a = AccountProcessLock("lock-test-acctA")
        b = AccountProcessLock("lock-test-acctB")
        assert a.acquire() is True
        try:
            assert b.acquire() is True
        finally:
            b.release()
            a.release()
